"""Incremental language-routing release; retain existing plans and task facts."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import deploy_fb_group_material_pool as previous

base, pool, delivery = previous.base, previous.pool, previous.delivery
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "c7489e1"
REMOTE_REF = "refs/heads/codex/fb-group-material-pool-20261026"
GROUP_UNIT = previous.UNIT
PYTHON = {"features/fb_video_publish/" + name + ".py" for name in
          ("group_language", "group_sources", "group_pool", "group_scheduler", "worker", "core", "client")}
STATIC = {"static/fb-post-material-pool." + suffix for suffix in ("html", "js", "css")}


def stage():
    changed = base.run("git", "-C", str(ROOT), "diff", "--name-only", BASELINE, "HEAD").splitlines()
    targets = []
    for name in changed:
        if name.startswith(("features/", "static/", "deploy/")) or name == "app.py":
            base.require(name in PYTHON | STATIC, "Runtime change outside language scope: " + name)
        if name not in PYTHON | STATIC:
            continue
        source = ROOT / name
        base.require(source.is_file(), "Missing release source: " + name)
        if name in PYTHON:
            compile(source.read_bytes(), name, "exec")
        old = pool.git_bytes(BASELINE, name)
        expected = hashlib.sha256(old).hexdigest() if old is not None else None
        destinations = [base.MAIN / name, base.FB / name]
        if name in STATIC:
            destinations.append(base.STATIC / Path(name).name)
        for target in destinations:
            base.require(base.sha(target) == expected, "Live baseline drift: " + str(target))
            targets.append((source, target, target.stat().st_mode & 0o777 if target.exists() else 0o644))
    base.require(targets, "No language routing changes")
    return targets


def facts(path, columns=None):
    if columns is None:
        with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND (name LIKE 'fb_video_%' OR name LIKE 'fb_group_%')")]
            columns = {name: [r[1] for r in conn.execute('PRAGMA table_info("' + name + '")')] for name in names}
    return pool.fact_snapshot(path, columns)


def check_facts(path, columns, before):
    base.require(facts(path, columns)[1] == before, "Existing video/group facts changed during migration")


def assert_window(now=None):
    delivery.assert_pool_window(now)
    current = (now or datetime.now(timezone.utc)).astimezone(delivery.BJ)
    from features.fb_video_publish.group_pool import random_times
    with base.database() as conn:
        conn.row_factory = sqlite3.Row
        schedules = conn.execute("SELECT * FROM fb_group_pool WHERE status='enabled'").fetchall()
        for row in schedules:
            for offset in (-1, 0, 1):
                day = current.date() + timedelta(days=offset)
                if row["schedule_mode"] == "fixed":
                    times = json.loads(row["times_json"])
                else:
                    plan = conn.execute("SELECT times_json FROM fb_group_pool_day WHERE tenant_key=? AND bj_date=? "
                                        "AND schedule_revision=?", (row["tenant_key"], day.isoformat(), row["schedule_revision"])).fetchone()
                    times = json.loads(plan[0]) if plan else random_times(row["tenant_key"], day.isoformat(), row["schedule_revision"], row["daily_batches"])
                for value in times:
                    hour, minute = map(int, value.split(":"))
                    slot = datetime.combine(day, datetime.min.time(), tzinfo=delivery.BJ).replace(hour=hour, minute=minute)
                    base.require(not -60 <= (slot - current).total_seconds() <= 300,
                                 "Enabled group pool has a nearby time point")


def saved_states():
    return {**pool.saved_states(), GROUP_UNIT: base.state(GROUP_UNIT)}


def quiesce(saved):
    if saved[GROUP_UNIT]["active"] == "active":
        subprocess.run(["systemctl", "stop", GROUP_UNIT], check=True)
    pool.quiesce(saved)


def resume(saved, *, main=True, group=True):
    delivery.resume(saved, restart_main=main)
    if group:
        pool.restore_states(saved, [GROUP_UNIT])


def apply(targets, commit):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base.BASE / "backups" / ("group-languages-" + stamp + "-" + commit[:8])
    backup.mkdir(parents=True, mode=0o700)
    saved = saved_states()
    base.require(saved[base.FB_UNIT]["active"] == "active" and saved[base.API_UNIT]["active"] == "active", "Unhealthy API services")
    manifest_path = backup / "manifest.json"
    manifest = {"commit": commit, "baseline": BASELINE, "states": saved, "phase": "prepared", "files": [],
                "migration_started": False, "reopen_started": False, "main_app_sha": base.sha(base.MAIN / "app.py")}
    for index, (source, target, mode) in enumerate(targets):
        old = backup / ("file-" + str(index)) if target.exists() else None
        if old:
            shutil.copy2(target, old)
        manifest["files"].append({"target": str(target), "backup": str(old) if old else None,
                                  "before": base.sha(target), "after": base.sha(source), "mode_before": mode})
    pool.json_save(manifest_path, manifest)
    modified = False
    try:
        assert_window()
        quiesce(saved)
        assert_window()
        base.snapshot_db(backup / "operational.sqlite3")
        (backup / "operational.sqlite3").chmod(0o600)
        columns, before = facts(base.DB)
        manifest.update(historical_columns=columns, historical_facts=before)
        # Rehearse the exact migration on a copy before installing runtime files.
        rehearsal = backup / "migration-rehearsal.sqlite3"
        shutil.copy2(backup / "operational.sqlite3", rehearsal)
        from features.fb_video_publish.core import Store
        Store(rehearsal)
        check_facts(rehearsal, columns, before)
        for (source, _, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(Path(item["target"])) == item["before"], "Target drift after drain")
            base.require(base.sha(source) == item["after"], "Release source changed")
            if item["backup"]:
                base.require(base.sha(Path(item["backup"])) == item["before"], "Backup drift")
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        manifest.update(phase="files_installed", migration_started=True)
        pool.json_save(manifest_path, manifest)
        subprocess.run(["runuser", "-u", "fb-auto-post", "--", "/usr/bin/python3", "-c",
                        "import sys;sys.path.insert(0,sys.argv[1]);from features.fb_video_publish.core import Store;Store(sys.argv[2])",
                        str(ROOT), str(base.DB)], check=True)
        check_facts(base.DB, columns, before)
        with base.database() as conn:
            base.require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite integrity failure")
            fields = {row[1] for row in conn.execute("PRAGMA table_info(fb_group_pool)")}
            base.require({"page_language_selection_json", "routing_policy_version"} <= fields, "Language schema missing")
            base.require(conn.execute("SELECT COUNT(*) FROM fb_group_pool WHERE routing_policy_version<>'group_pool_v1'").fetchone()[0] == 0,
                         "Migration unexpectedly changed an existing routing policy")
        base.require(base.sha(base.MAIN / "app.py") == manifest["main_app_sha"], "Main app changed")
        manifest.update(phase="migrated", reopen_started=True)
        pool.json_save(manifest_path, manifest)
        resume(saved)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed hash mismatch")
        manifest["phase"] = "deployed"
        pool.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path), "files": len(targets),
                          "historical_tables_preserved": len(before), "services_restored": True}), flush=True)
    except Exception:
        if manifest["migration_started"] or manifest["reopen_started"]:
            manifest["phase"] = "needs_recovery_compatible_code_retained"
            pool.json_save(manifest_path, manifest)
            print("Compatible code retained; inspect " + str(manifest_path), flush=True)
        else:
            if modified:
                for item in manifest["files"]:
                    target = Path(item["target"])
                    if item["backup"]:
                        base.atomic_bytes(Path(item["backup"]).read_bytes(), target, item["mode_before"])
                    else:
                        base.require(base.sha(target) in {None, item["after"]}, "Partial target drift")
                        target.unlink(missing_ok=True)
            resume(saved, main=modified)
            manifest["phase"] = "failed_original_restored"
            pool.json_save(manifest_path, manifest)
        raise


def rollback(path):
    manifest = json.loads(path.read_text())
    for item in manifest["files"]:
        if Path(item["target"]).suffix == ".py":
            base.require(base.sha(Path(item["target"])) == item["after"], "Compatible backend drift; reconcile first")
    saved = saved_states()
    assert_window()
    gate_change_started = False
    try:
        quiesce(saved)
        assert_window()
        gate_change_started = True
        base.atomic_bytes(b"[Service]\nEnvironment=FB_VIDEO_GROUP_POOL_ENABLED=0\n", previous.DROPIN)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "disable", GROUP_UNIT], check=True)
        resume(saved, group=False)
        manifest["functional_rollback"] = datetime.now(timezone.utc).isoformat()
        pool.json_save(path, manifest)
        print(json.dumps({"rolled_back": True, "compatible_code_tasks_links_and_database_retained": True}), flush=True)
    except Exception:
        if not gate_change_started:
            resume(saved, main=False)
            manifest["rollback_phase"] = "failed_original_services_restored"
        else:
            manifest["rollback_phase"] = "needs_recovery_compatible_code_retained"
        pool.json_save(path, manifest)
        print("Rollback interrupted; inspect " + str(path), flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    base.check_mount()
    if args.rollback:
        base.require(not args.apply, "Choose one operation")
        rollback(args.rollback)
        return
    commit = delivery.verified_commit(REMOTE_REF)
    targets = stage()
    assert_window()
    print(json.dumps({"preflight": True, "commit": commit, "targets": len(targets)}), flush=True)
    if args.apply:
        apply(targets, commit)


if __name__ == "__main__":
    main()

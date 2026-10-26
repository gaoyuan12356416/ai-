"""Reviewed GitHub release, additive migration and capability-only rollback."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import deploy_fb_page_list_performance as delivery

base, pool = delivery.base, delivery.pool_release
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "4a2f692"
REMOTE_REF = "refs/heads/codex/fb-group-material-pool-20261026"
UNIT = "fb-group-pool-scheduler.service"
DROPIN = Path("/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-group-material-pool.conf")
ENTRY = "fbPostMaterialPool"
PYTHON = {
    "features/fb_video_publish/group_sources.py", "features/fb_video_publish/group_pool.py",
    "features/fb_video_publish/group_scheduler.py", "features/fb_video_publish/core.py",
    "features/fb_video_publish/worker.py", "features/fb_video_publish/service.py",
    "features/fb_video_publish/client.py", "features/fb_video_publish/navigation.py",
    "features/fb_video_publish/identity_client.py", "features/fb_video_publish/identity_broker.py",
}
STATIC = {"static/fb-post-material-pool.html", "static/fb-post-material-pool.js", "static/fb-post-material-pool.css"}
SPECIAL = {"features/fb_auto_posts/service.py", "static/quick-nav.js", "scripts/fb_group_pool_scheduler.py", "deploy/" + UNIT}


def git_bytes(path):
    result = subprocess.run(["git", "-C", str(ROOT), "show", BASELINE + ":" + path], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def stage(staging):
    changed = base.run("git", "-C", str(ROOT), "diff", "--name-only", BASELINE, "HEAD").splitlines()
    for name in changed:
        if name.startswith(("features/", "static/", "deploy/")) or name == "app.py":
            base.require(name in PYTHON | STATIC | SPECIAL, "Runtime change outside reviewed scope: " + name)
    targets = []

    def add(source, target, expected):
        expected_hash = hashlib.sha256(expected).hexdigest() if expected is not None else None
        base.require(base.sha(target) == expected_hash, "Live baseline drift: " + str(target))
        base.require(source.is_file(), "Missing release source: " + str(source))
        if source.suffix == ".py":
            compile(source.read_bytes(), str(source), "exec")
        targets.append((source, target, target.stat().st_mode & 0o777 if target.exists() else 0o644))

    for name in sorted((PYTHON | STATIC | SPECIAL) & set(changed)):
        if name in PYTHON:
            destinations = [base.MAIN / name, base.FB / name]
        elif name in STATIC:
            destinations = [base.MAIN / name, base.FB / name, base.STATIC / Path(name).name]
        elif name == "static/quick-nav.js":
            destinations = [base.MAIN / name, base.STATIC / "quick-nav.js"]
        elif name == "deploy/" + UNIT:
            destinations = [Path("/etc/systemd/system") / UNIT]
        else:
            destinations = [base.FB / name]
        for target in destinations:
            add(ROOT / name, target, git_bytes(name))
    from features.fb_video_publish.navigation import ensure_navigation
    for index, target in enumerate([base.MAIN / "static/navigation.json", base.STATIC / "navigation.json"]):
        current = target.read_bytes()
        nav = ensure_navigation(json.loads(current.decode("utf-8-sig")))
        generated = staging / ("navigation-%d.json" % index)
        generated.write_text(json.dumps(nav, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        add(generated, target, current)
    gate = staging / "group-pool.conf"
    gate.write_text("[Service]\nEnvironment=FB_VIDEO_GROUP_POOL_ENABLED=1\n", encoding="utf-8")
    add(gate, DROPIN, None)
    base.require(any(target.name == UNIT for _, target, _ in targets), "New scheduler unit missing")
    return targets


def immutable_facts(path):
    # Effects collection may continue; only the changed module's historical
    # facts are compared, in one consistent SQLite read transaction.
    with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        facts = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fb_video_%' ORDER BY name"):
            base.require(name.replace("_", "").isalnum(), "Unexpected table")
            columns = [r[1] for r in conn.execute('PRAGMA table_info("' + name + '")')]
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid'):
                digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
                digest.update(b"\n")
                count += 1
            facts[name] = {"columns": columns, "count": count, "sha256": digest.hexdigest()}
        return facts


def check_frozen_facts(before, path):
    after = immutable_facts(path)
    base.require(all(after.get(name) == facts for name, facts in before.items()), "Existing FB records changed during migration")


_active_work = base.active_work


def active_work():
    busy, count = _active_work()
    if "fb-auto-post-effects.service" in busy:
        # Independent collector doesn't import changed files or call sidecar.
        for relative in ("scripts/fb_auto_post_effect_runner.py", "scripts/fb_auto_post_metric_runner.py",
                         "features/fb_auto_posts/effect_collector.py", "features/fb_auto_posts/effects.py"):
            base.require(base.sha(ROOT / relative) == base.sha(base.FB / relative), "Independent collector drift: " + relative)
        busy = [unit for unit in busy if unit != "fb-auto-post-effects.service"]
    return busy, count


base.active_work = active_work


def restore(saved, *, restart_main):
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    delivery.resume(saved, restart_main=restart_main)


def apply(targets, commit, staging):
    backup = base.BASE / "backups" / staging.name
    backup.mkdir(parents=True)
    saved = pool.saved_states()
    base.require(saved[base.FB_UNIT]["active"] == "active" and saved[base.API_UNIT]["active"] == "active", "API services must be healthy")
    manifest_path = backup / "manifest.json"
    manifest = {"commit": commit, "baseline": BASELINE, "states": saved, "files": [], "phase": "prepared",
                "reopen_started": False, "migration_started": False, "main_app_sha": base.sha(base.MAIN / "app.py")}
    for index, (source, target, mode) in enumerate(targets):
        destination = backup / ("file-%d" % index) if target.exists() else None
        if destination:
            shutil.copy2(target, destination)
        manifest["files"].append({"target": str(target), "backup": str(destination) if destination else None,
                                  "before": base.sha(target), "after": base.sha(source), "mode_before": mode})
    pool.json_save(manifest_path, manifest)
    modified = False
    try:
        delivery.assert_pool_window()
        pool.quiesce(saved)
        delivery.assert_pool_window()
        base.snapshot_db(backup / "operational.sqlite3")
        before = immutable_facts(base.DB)
        manifest["historical_facts"] = before
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["before"], "Target drift after drain")
            if item["backup"]:
                base.require(base.sha(Path(item["backup"])) == item["before"], "Backup drift after drain")
        for (source, _, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(source) == item["after"], "Staged source changed")
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        manifest.update(phase="files_installed", migration_started=True)
        pool.json_save(manifest_path, manifest)
        # The runtime user owns newly created SQLite WAL/SHM files as well.
        subprocess.run(["runuser", "-u", "fb-auto-post", "--", "/usr/bin/python3", "-c",
                        "import sys; sys.path.insert(0,sys.argv[1]); from features.fb_video_publish.core import Store; Store(sys.argv[2])",
                        str(ROOT), str(base.DB)], check=True)
        check_frozen_facts(before, base.DB)
        with base.database() as conn:
            base.require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite integrity failure")
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fb_group_%'")]
            base.require(tables, "Group-pool schema missing")
            for name in tables:
                base.require(name.replace("_", "").isalnum(), "Unexpected new table")
                base.require(conn.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] == 0, "Initial group pool must be empty")
        base.require(base.sha(base.MAIN / "app.py") == manifest["main_app_sha"], "Main app changed")
        manifest.update(phase="migrated", reopen_started=True)
        pool.json_save(manifest_path, manifest)
        restore(saved, restart_main=True)
        subprocess.run(["systemctl", "enable", "--now", UNIT], check=True)
        base.require(base.state(UNIT)["active"] == "active", "New scheduler not active")
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed file drift")
        manifest["phase"] = "deployed"
        pool.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path), "files": len(targets),
                          "historical_tables_preserved": len(before), "new_pool_empty_and_paused": True}), flush=True)
    except Exception:
        if manifest["migration_started"] or manifest["reopen_started"]:
            # Current schema/executors must stay compatible with all accepted work.
            manifest["phase"] = "needs_recovery_compatible_code_retained"
            pool.json_save(manifest_path, manifest)
            print("Compatible code retained; inspect " + str(manifest_path), flush=True)
        else:
            if modified:
                for item in manifest["files"]:
                    if item["backup"]:
                        base.atomic_bytes(Path(item["backup"]).read_bytes(), Path(item["target"]), item["mode_before"])
                    else:
                        target = Path(item["target"])
                        base.require(base.sha(target) in {None, item["after"]}, "Partial target drift")
                        target.unlink(missing_ok=True)
            restore(saved, restart_main=modified)
            manifest["phase"] = "failed_original_restored"
            pool.json_save(manifest_path, manifest)
        raise


def rollback(path):
    manifest = json.loads(path.read_text())
    # Functional rollback only: never restore SQLite, tokens, code or links.
    for item in manifest["files"]:
        if Path(item["target"]).suffix == ".py":
            base.require(base.sha(Path(item["target"])) == item["after"], "Compatible backend drift; reconcile before rollback")
    saved = pool.saved_states()
    delivery.assert_pool_window()
    subprocess.run(["systemctl", "stop", UNIT], check=True)
    try:
        pool.quiesce(saved)
        base.atomic_bytes(b"[Service]\nEnvironment=FB_VIDEO_GROUP_POOL_ENABLED=0\n", DROPIN)
        # Keep read-only records and the publisher's navigation permission.
        # Removing that permission would invalidate already accepted tasks.
        # The feature gate closes every new mutation and scheduler claim.
        subprocess.run(["systemctl", "disable", UNIT], check=True)
        restore(saved, restart_main=True)
        manifest["functional_rollback"] = datetime.now(timezone.utc).isoformat()
        pool.json_save(path, manifest)
        print(json.dumps({"rolled_back": True, "compatible_backend_and_all_records_retained": True}), flush=True)
    except Exception:
        print("Rollback requires recovery; inspect " + str(path), flush=True)
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
    staging = base.BASE / "staging" / ("group-pool-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:8])
    staging.mkdir(parents=True)
    targets = stage(staging)
    print(json.dumps({"preflight": True, "commit": commit, "targets": len(targets)}), flush=True)
    if args.apply:
        apply(targets, commit, staging)


if __name__ == "__main__":
    main()

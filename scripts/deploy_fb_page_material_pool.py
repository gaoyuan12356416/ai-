"""Scoped pool upgrade. GitHub first; never restore a database or immutable link."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import deploy_fb_video_publish as base
from patch_fb_page_material_pool_integration import patch_app

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "5e43e24"
MAIN_HASH = "4897589769737c9cc3a96cd7ff77182bf2ed5279d6970631fc86c1cb44fc4d3d"
POOL_UNIT = "fb-video-pool-scheduler.service"
WORKER = "fb-video-publish-worker.service"
PREPARE_TIMER = "fb-video-publish-prepare.timer"
DROPIN = Path("/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-video-pool.conf")
TIMERS = base.OLD_TIMERS + [PREPARE_TIMER]


def json_save(path, value):
    base.atomic_bytes((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode(), path, 0o600)


def fact_snapshot(path, columns=None):
    """Hash every pre-existing column, including manual records, without exposing data."""
    with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        if columns is None:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            columns = {n: [r[1] for r in conn.execute('PRAGMA table_info("' + n.replace('"', '""') + '")')]
                       for n in names}
        result = {}
        for name, fields in columns.items():
            quote = lambda x: '"' + x.replace('"', '""') + '"'
            primary = sorted((r[5], r[1]) for r in conn.execute("PRAGMA table_info(" + quote(name) + ")") if r[5])
            ordering = ",".join(quote(n) for _, n in primary) if primary else "rowid"
            digest, count = hashlib.sha256(), 0
            for row in conn.execute("SELECT " + ",".join(map(quote, fields)) + " FROM " + quote(name)
                                    + " ORDER BY " + ordering):
                digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
                digest.update(b"\n")
                count += 1
            result[name] = {"count": count, "sha256": digest.hexdigest()}
        return columns, result


def migrate_preserving(path):
    columns, before = fact_snapshot(path)
    from features.fb_video_publish.core import Store
    Store(path)
    base.require(fact_snapshot(path, columns)[1] == before, "Migration altered existing facts")
    with closing(sqlite3.connect(path)) as conn:
        base.require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
    return {"tables": len(before), "rows": sum(x["count"] for x in before.values()), "preserved": True}


def git_bytes(ref, relative):
    result = subprocess.run(["git", "-C", str(ROOT), "show", ref + ":" + relative],
                            capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else None


def stage_targets(staging):
    """Copy changed allowlisted files only, checking each live baseline."""
    base.require(base.sha(base.MAIN / "app.py") == MAIN_HASH, "Live main app drift; review new snapshot")
    changed = base.run("git", "-C", str(ROOT), "diff", "--name-only", BASELINE, "HEAD").splitlines()
    targets = []

    def add(source, target, expected, mode=0o644):
        actual = base.sha(target)
        digest = hashlib.sha256(expected).hexdigest() if expected is not None else None
        base.require(actual == digest, "Live baseline drift: " + str(target))
        targets.append((source, target, mode))

    for relative in changed:
        destinations = []
        if relative.startswith("features/fb_video_publish/") and relative.endswith(".py"):
            destinations = [base.MAIN / relative, base.FB / relative]
        elif relative == "features/fb_auto_posts/service.py":
            destinations = [base.FB / relative]
        elif relative == "scripts/fb_video_pool_scheduler.py":
            destinations = [base.FB / relative]
        elif relative in {"static/fb-pages.html", "static/fb-video-publish.html",
                          "static/fb-video-publish-records.html", "static/fb-video-publish.js",
                          "static/fb-video-publish.css"}:
            destinations = [base.MAIN / relative, base.FB / relative, base.STATIC / Path(relative).name]
        elif relative == "deploy/nginx-fb-video-publish.conf":
            destinations = [Path("/etc/nginx/default.d/fb-video-publish.conf")]
        elif relative == "deploy/" + POOL_UNIT:
            destinations = [Path("/etc/systemd/system") / POOL_UNIT]
        for target in destinations:
            add(ROOT / relative, target, git_bytes(BASELINE, relative))
    patched = staging / "app.py"
    patched.write_bytes(patch_app((base.MAIN / "app.py").read_bytes().decode()).encode())
    add(patched, base.MAIN / "app.py", (base.MAIN / "app.py").read_bytes())
    gate = staging / "pool.conf"
    gate.write_text("[Service]\nEnvironment=FB_VIDEO_V2_ENABLED=1\nEnvironment=FB_VIDEO_POOL_ENABLED=1\n",
                    encoding="utf-8")
    add(gate, DROPIN, None)
    base.require(any(t.name == POOL_UNIT for _, t, _ in targets), "Scheduler missing from committed release")
    import py_compile
    for source, _, _ in targets:
        if source.suffix == ".py":
            py_compile.compile(str(source), doraise=True)
    return targets


def in_flight():
    busy, count = base.active_work()
    with base.database() as conn:
        manual = conn.execute("""SELECT COUNT(*) FROM fb_video_task WHERE status='running'
            OR prepare_lease<>'' OR link_lease<>'' OR execution_lease<>''""").fetchone()[0]
    prep = base.state("fb-video-publish-prepare.service")["active"]
    if prep in {"active", "activating", "deactivating"}:
        busy.append("fb-video-publish-prepare.service")
    return busy, count + manual


def restore_states(saved, units):
    for unit in units:
        desired = saved[unit]
        if desired["enabled"] == "not-found" and not base.unit_present(unit):
            continue
        current = base.state(unit)
        if desired["active"] == "active" and current["active"] != "active":
            subprocess.run(["systemctl", "start", unit], check=True)
        elif desired["active"] != "active" and current["active"] == "active":
            subprocess.run(["systemctl", "stop", unit], check=True)
        current = base.state(unit)
        base.require(current["active"] == desired["active"], "Runtime state drift: " + unit)
        base.require(current["enabled"] == desired["enabled"], "Enabled state drift: " + unit)


def quiesce(saved):
    # Stop producers first. The sidecar stays alive until its in-flight requests finish.
    units = [u for u in TIMERS + [WORKER, POOL_UNIT] if saved[u]["active"] == "active"]
    if units:
        subprocess.run(["systemctl", "stop", *units], check=True)
    deadline = time.monotonic() + 120
    while True:
        busy, count = in_flight()
        if not busy and count == 0:
            break
        base.require(time.monotonic() < deadline, "Publisher still processing; retry after drain")
        time.sleep(3)
    subprocess.run(["systemctl", "stop", base.FB_UNIT], check=True)
    base.require(in_flight() == ([], 0), "Publisher work appeared during quiescence")


def saved_states():
    return {u: base.state(u) if base.unit_present(u) else {"active": "inactive", "enabled": "not-found"}
            for u in TIMERS + [WORKER, POOL_UNIT, base.FB_UNIT, base.API_UNIT]}


def rollback(manifest_path):
    """Close only the new capability; retain executors/schema for accepted tasks."""
    manifest = json.loads(manifest_path.read_text())
    static_restore = []
    for item in manifest["files"]:
        target = Path(item["target"])
        if target.suffix == ".py":
            base.require(base.sha(target) == item["after"],
                         "Compatible backend incomplete or changed; reconcile manifest before rollback: " + str(target))
        if target.suffix not in {".js", ".css", ".html"} or not item["backup"]:
            continue
        base.require(base.sha(target) in {item["before"], item["after"]}, "Rollback static drift")
        base.require(base.sha(Path(item["backup"])) == item["before"], "Rollback backup drift")
        static_restore.append(item)
    saved = saved_states()
    resumed = False
    try:
        quiesce(saved)
        base.atomic_bytes(b"[Service]\nEnvironment=FB_VIDEO_V2_ENABLED=0\nEnvironment=FB_VIDEO_POOL_ENABLED=0\n",
                          DROPIN)
        subprocess.run(["systemctl", "disable", POOL_UNIT], check=True)
        # Old static still uses supported legacy API; frozen V2/pool tasks remain executable.
        for item in static_restore:
            target = Path(item["target"])
            base.require(base.sha(target) in {item["before"], item["after"]}, "Rollback static drift")
            base.require(base.sha(Path(item["backup"])) == item["before"], "Rollback backup drift")
            base.atomic_bytes(Path(item["backup"]).read_bytes(), target, item["mode_before"])
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "start", base.FB_UNIT], check=True)
        base.health("http://127.0.0.1:18835/health")
        restore_states(saved, TIMERS + [WORKER])
        resumed = True
        manifest["functional_rollback"] = datetime.now(timezone.utc).isoformat()
        json_save(manifest_path, manifest)
        print(json.dumps({"rolled_back": True, "compatible_backend_and_all_facts_retained": True,
                          "scheduler_disabled": True}), flush=True)
    finally:
        if not resumed:
            print("Rollback incomplete; producers remain stopped where necessary. Inspect saved manifest: "
                  + str(manifest_path), flush=True)


def apply_release(staging, targets, commit):
    backup = base.BASE / "backups" / staging.name
    backup.mkdir(parents=True)
    saved = saved_states()
    manifest_path = backup / "manifest.json"
    manifest = {"commit": commit, "states": saved, "files": [], "phase": "before_quiesce"}
    for index, (source, target, mode) in enumerate(targets):
        destination = backup / ("file-" + str(index)) if target.exists() else None
        if destination:
            shutil.copy2(target, destination)
        manifest["files"].append({"target": str(target), "backup": str(destination) if destination else None,
                                  "before": base.sha(target), "after": base.sha(source), "mode": mode,
                                  "mode_before": target.stat().st_mode & 0o777 if target.exists() else mode})
    json_save(manifest_path, manifest)
    source_changed, reopened = False, False
    try:
        quiesce(saved)
        base.snapshot_db(backup / "operational.sqlite3")
        # Recheck every target after drain, not just before waiting.
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["before"], "Target changed during drain")
        for (source, _, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(source) == item["after"], "Release source changed during drain")
        manifest["phase"] = "quiesced"
        json_save(manifest_path, manifest)
        # ALTER TABLE commits may make the old positional INSERT incompatible
        # even if the following file copy never starts. Do not restart old code.
        source_changed = True
        manifest["migration"] = migrate_preserving(base.DB)
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        manifest["phase"] = "files_installed"
        json_save(manifest_path, manifest)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        subprocess.run(["systemctl", "restart", base.API_UNIT], check=True)
        base.health("http://127.0.0.1:8787/api/ui/topbar")
        subprocess.run(["systemctl", "start", base.FB_UNIT], check=True)
        base.health("http://127.0.0.1:18835/health")
        reopened = True
        restore_states(saved, TIMERS + [WORKER])
        subprocess.run(["systemctl", "enable", "--now", POOL_UNIT], check=True)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Deployed file hash mismatch")
        manifest["phase"] = "deployed"
        json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path),
                          "migration": manifest["migration"], "old_timer_states_preserved": True}), flush=True)
    except Exception:
        if not source_changed:
            # Before code switch, resuming the original services is safe.
            restore_states(saved, [base.FB_UNIT] + TIMERS + [WORKER, POOL_UNIT])
        elif reopened:
            # Never kill or roll back sources after accepted tasks may exist.
            restore_states(saved, TIMERS + [WORKER])
        manifest["failure_after_reopen"] = reopened
        json_save(manifest_path, manifest)
        print("Deployment requires inspection; compatible code/database retained: " + str(manifest_path), flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    base.check_mount()
    if args.rollback:
        rollback(args.rollback)
        return
    commit = base.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    base.require(not base.run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"),
                 "Dirty release checkout")
    base.require(base.run("git", "-C", str(ROOT), "branch", "-r", "--contains", commit), "Unverified GitHub commit")
    staging = base.BASE / "staging" / ("pool-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:8])
    staging.mkdir(parents=True)
    targets = stage_targets(staging)
    base.snapshot_db(staging / "rehearsal.sqlite3")
    migration = migrate_preserving(staging / "rehearsal.sqlite3")
    print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets),
                      "migration": migration, "in_flight": in_flight()}), flush=True)
    if args.apply:
        apply_release(staging, targets, commit)


if __name__ == "__main__":
    main()

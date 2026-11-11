#!/usr/bin/env python3
"""Switch only the independent notifier. Never restart or edit publishers."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/mnt/data-disk/x-material-shortage-alert")
CURRENT = Path("/opt/x-material-shortage-alert/current")
CONFIG = Path("/etc/x-material-shortage-alert.json")
TIMER = "x-material-shortage-alert.timer"
SERVICE = "x-material-shortage-alert.service"
FILES = ("features/__init__.py", "features/post_daily_report/__init__.py",
         "features/post_daily_report/delivery.py", "features/x_material_alert.py",
         "features/x_material_inventory.py", "scripts/x_material_shortage_alert.py")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def link(target):
    temporary = CURRENT.with_name("current.daily-new")
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT)


def publishers():
    return run("systemctl", "show", "x-post-automation.service", "x-auto-post-service.service",
               "x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer",
               "-p", "Id", "-p", "MainPID", "-p", "ActiveState")


def drain_notifier():
    run("systemctl", "stop", TIMER)
    for _ in range(45):
        state = run("systemctl", "show", SERVICE, "-p", "ActiveState", "--value")
        if state not in ("active", "activating", "deactivating"):
            return
        time.sleep(1)
    raise RuntimeError("existing_feishu_write_still_running_do_not_interrupt")


def restore(manifest):
    drain_notifier()
    link(manifest["previous_alert"])
    for target, saved in manifest["files"].items():
        shutil.copy2(saved, target)
    run("systemctl", "daemon-reload")
    if manifest["timer_active"]:
        run("systemctl", "start", TIMER)


def install(source):
    if run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("data_disk_unavailable")
    commit = run("git", "-C", str(source), "rev-parse", "HEAD")
    if run("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("source_is_dirty")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BASE / "backups" / (stamp + "-daily-" + commit[:8])
    backup.mkdir(parents=True)
    backup.chmod(0o700)
    release = BASE / "releases" / (commit + "-daily")
    release.mkdir(exist_ok=True)
    for name in FILES:
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != (source / name).read_bytes():
                raise RuntimeError("staged_release_content_changed")
        else:
            shutil.copy2(source / name, target)
    manifest = {"commit": commit, "backup": str(backup), "release": str(release),
                "previous_alert": str(CURRENT.resolve()), "publisher_before": publishers(),
                "publisher_runtime": str(Path("/opt/x-post-automation/current").resolve()),
                "timer_active": run("systemctl", "show", TIMER, "-p", "ActiveState", "--value") == "active",
                "files": {}, "sha256": {name: hashlib.sha256((release / name).read_bytes()).hexdigest() for name in FILES}}
    for path in [CONFIG, Path("/etc/systemd/system") / TIMER, Path("/etc/systemd/system") / SERVICE]:
        saved = backup / path.name
        shutil.copy2(path, saved)
        manifest["files"][str(path)] = str(saved)
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    config = json.loads(CONFIG.read_text())
    config.update(mode="daily_inventory", threshold_days=2)
    preview_config = backup / "preview-config.json"
    preview_config.write_text(json.dumps(config, indent=2))
    preview_config.chmod(0o600)
    # Verify the exact staged release against live read-only sources before switching.
    preview = run("/usr/bin/python3", str(release / "scripts/x_material_shortage_alert.py"), "--config", str(preview_config))
    (backup / "preview.json").write_text(preview)
    run("systemd-analyze", "verify", str(source / "deploy" / TIMER), str(source / "deploy" / SERVICE))
    try:
        drain_notifier()
        db = BASE / "delivery.sqlite3"
        if db.exists():
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(backup / "delivery.sqlite3") as saved:
                original.backup(saved)
        shutil.copy2(preview_config, CONFIG)
        for unit in (TIMER, SERVICE):
            shutil.copy2(source / "deploy" / unit, Path("/etc/systemd/system") / unit)
        link(release)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", TIMER)
    except Exception:
        restore(manifest)
        raise
    manifest["publisher_after"] = publishers()
    manifest["publisher_pids_unchanged"] = manifest["publisher_before"] == manifest["publisher_after"]
    manifest["timer_next"] = run("systemctl", "show", TIMER, "-p", "NextElapseUSecRealtime", "--value")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": "installed", "manifest": str(manifest_path), "commit": commit,
                      "publisher_pids_unchanged": manifest["publisher_pids_unchanged"], "next_trigger": manifest["timer_next"]}))


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback")
    args = parser.parse_args()
    if args.rollback:
        manifest = json.loads(Path(args.rollback).read_text())
        if str(CURRENT.resolve()) != manifest["release"]:
            raise RuntimeError("notifier_changed_review_scoped_rollback")
        restore(manifest)
        print(json.dumps({"status": "rolled_back", "delivery_ledger": "preserved"}))
    else:
        install(Path(__file__).resolve().parents[1])

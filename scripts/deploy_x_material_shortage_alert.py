#!/usr/bin/env python3
"""Install from a fetched Git commit; only the new notifier is started."""
import argparse
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/mnt/data-disk/x-material-shortage-alert")
X_CURRENT = Path("/opt/x-post-automation/current")
ALERT_CURRENT = Path("/opt/x-material-shortage-alert/current")
UNITS = ("x-material-shortage-alert.service", "x-material-shortage-alert.timer")
RUNNER = "scripts/x_post_schedule_runner.py"
X_FILES = (RUNNER, "features/x_posts/material_shortage.py")
ALERT_FILES = ("features/__init__.py", "features/x_accounts/__init__.py", "features/x_accounts/language.py",
               "features/x_posts/__init__.py", "features/x_posts/material_shortage.py",
               "features/post_daily_report/__init__.py", "features/post_daily_report/delivery.py",
               "features/x_material_alert.py", "scripts/x_material_shortage_alert.py")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".material-alert-new")
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    os.replace(temp, path)


def safe_disk():
    if run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("data_disk_unavailable")


def install(source, args):
    safe_disk()
    commit = run("git", "-C", str(source), "rev-parse", "HEAD")
    if run("git", "-C", str(source), "status", "--porcelain"):
        raise RuntimeError("source_checkout_is_dirty")
    previous = X_CURRENT.resolve()
    if digest(previous / RUNNER) != args.runner_before:
        raise RuntimeError("live_runner_changed_rebase_required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BASE / "backups" / (stamp + "-" + commit[:8])
    backup.mkdir(parents=True)
    release = BASE / "releases" / commit
    x_release = previous.parent / (stamp + "-material-alert-" + commit[:8])
    config_target = Path("/etc/x-material-shortage-alert.json")
    paths = [config_target] + [Path("/etc/systemd/system") / unit for unit in UNITS]
    manifest = {"commit": commit, "source": str(source), "previous_x": str(previous), "x_release": str(x_release),
                "previous_alert": str(ALERT_CURRENT.resolve()) if ALERT_CURRENT.exists() else None,
                "alert_release": str(release), "backup": str(backup), "files": {},
                "publisher_before": run("systemctl", "show", "x-post-automation.service", "x-auto-post-publisher.service", "-p", "Id", "-p", "MainPID", "-p", "ActiveState")}
    for path in paths:
        target = backup / path.name
        if path.exists():
            shutil.copy2(path, target)
            manifest["files"][str(path)] = str(target)
        else:
            manifest["files"][str(path)] = None
    shutil.copy2(previous / RUNNER, backup / "x_post_schedule_runner.py")
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    # Full copy retains every concurrently deployed component; only X_FILES change.
    shutil.copytree(previous, x_release, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for relative in X_FILES:
        target = x_release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    release.mkdir(parents=True)
    for relative in ALERT_FILES:
        original = source / relative
        if original.exists():
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    for path in [x_release / p for p in X_FILES] + [release / p for p in ALERT_FILES if (release / p).exists()]:
        py_compile.compile(str(path), doraise=True)
    config = json.loads(Path(args.config).read_text())
    config["start_at"] = datetime.now(timezone.utc).isoformat()
    config["state_dir"] = str(BASE)
    config_target.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    config_target.chmod(0o600)
    for unit in UNITS:
        shutil.copy2(source / "deploy" / unit, Path("/etc/systemd/system") / unit)
    run("systemd-analyze", "verify", *[str(Path("/etc/systemd/system") / u) for u in UNITS])
    if X_CURRENT.resolve() != previous or digest(previous / RUNNER) != args.runner_before:
        raise RuntimeError("concurrent_runtime_deployment_detected")
    link(ALERT_CURRENT, release)
    link(X_CURRENT, x_release)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", UNITS[1])
    manifest["activated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["publisher_after"] = run("systemctl", "show", "x-post-automation.service", "x-auto-post-publisher.service", "-p", "Id", "-p", "MainPID", "-p", "ActiveState")
    manifest["changed_sha256"] = {p: digest(x_release / p) for p in X_FILES}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": "installed", "manifest": str(backup / "manifest.json"), "commit": commit,
                      "publisher_pids_unchanged": manifest["publisher_before"] == manifest["publisher_after"]}))


def rollback(path):
    manifest = json.loads(Path(path).read_text())
    if str(X_CURRENT.resolve()) != manifest["x_release"]:
        raise RuntimeError("runtime_changed_restore_only_the_recorded_files_after_review")
    subprocess.run(["systemctl", "stop", UNITS[1]], check=True)
    # Do not kill an accepted Feishu write. Leave its receipt ledger intact.
    for _ in range(60):
        result = subprocess.run(["systemctl", "is-active", UNITS[0]], capture_output=True, text=True)
        if result.stdout.strip() not in ("active", "activating"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("notifier_still_running_preserved")
    subprocess.run(["systemctl", "disable", UNITS[1]], check=True)
    link(X_CURRENT, manifest["previous_x"])
    if manifest["previous_alert"]:
        link(ALERT_CURRENT, manifest["previous_alert"])
    elif ALERT_CURRENT.is_symlink():
        ALERT_CURRENT.unlink()
    for target, saved in manifest["files"].items():
        if saved:
            shutil.copy2(saved, target)
        elif Path(target).exists():
            Path(target).unlink()
    run("systemctl", "daemon-reload")
    print(json.dumps({"status": "rolled_back", "ledger_preserved": str(BASE / "delivery.sqlite3")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--runner-before")
    parser.add_argument("--rollback")
    args = parser.parse_args()
    if args.rollback:
        rollback(args.rollback)
    else:
        if not args.config or not args.runner_before:
            parser.error("--config and --runner-before are required")
        install(Path(__file__).resolve().parents[1], args)

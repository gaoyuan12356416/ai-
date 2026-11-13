#!/usr/bin/env python3
"""Restore this feature's code/config only. Never restore an older business ledger."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def run(*args):
    subprocess.run(args, check=True)


def main():
    manifest_path = Path(sys.argv[1]).resolve()
    allowed_backup = Path("/mnt/data-disk/codex-image-api/backups").resolve()
    if allowed_backup not in manifest_path.parents:
        raise SystemExit("manifest must belong to this service's backup directory")
    manifest = json.loads(manifest_path.read_text())
    allowed_files = {"/etc/systemd/system/codex-image-api.service", "/etc/systemd/system/codex-image-worker.service", "/etc/nginx/default.d/codex-image-api.conf", "/etc/codex-image-api.env"}
    if set(manifest["files"]) != allowed_files:
        raise SystemExit("unexpected rollback file scope")
    previous = manifest["previous_release"]
    if previous:
        previous = Path(previous).resolve()
        if Path("/opt/codex-image-api/releases").resolve() not in previous.parents or not previous.is_dir():
            raise SystemExit("previous release is unavailable")
    else:
        # Initial feature withdrawal must wait only for this worker's active images.
        db = sqlite3.connect("file:/mnt/data-disk/codex-image-api/state.sqlite3?mode=ro", uri=True)
        try:
            running = db.execute("SELECT COUNT(*) FROM images WHERE state='running'").fetchone()[0]
        finally:
            db.close()
        units = subprocess.check_output(["systemctl", "list-units", "--state=active,activating,deactivating", "--no-legend", "codex-image-job-*"], universal_newlines=True).strip()
        if running or units:
            raise SystemExit("this image service still has in-flight work; wait for its completion before initial withdrawal")
        run("systemctl", "disable", "--now", "codex-image-api.service", "codex-image-worker.service")
    for target, backup in manifest["files"].items():
        if backup:
            source = Path(backup).resolve()
            if source.parent != manifest_path.parent:
                raise SystemExit("backup path escaped manifest directory")
            shutil.copy2(source, target)
        elif Path(target).exists():
            Path(target).unlink()
    if previous:
        current = Path("/opt/codex-image-api/current")
        temporary = current.with_name("rollback.next")
        if temporary.is_symlink():
            temporary.unlink()
        temporary.symlink_to(previous)
        os.replace(temporary, current)
    run("nginx", "-t")
    run("systemctl", "daemon-reload")
    if previous:
        run("systemctl", "restart", "codex-image-api.service", "codex-image-worker.service")
    run("systemctl", "reload", "nginx")
    print("Code/config restored; current SQLite, receipts, credentials and COS objects preserved.")


if __name__ == "__main__":
    main()

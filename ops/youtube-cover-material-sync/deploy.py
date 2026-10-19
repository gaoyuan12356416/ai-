#!/usr/bin/env python3
"""Install/rollback only this independent service from a verified GitHub release."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess


UNITS = ("youtube-cover-material-sync.service", "youtube-cover-material-sync.timer")
ROOT = Path("/mnt/data-disk/deploy/youtube-cover-material-sync")
STATE = Path("/mnt/data-disk/youtube-cover-material-sync")
CURRENT = Path("/opt/youtube-cover-material-sync/current")


def call(*args):
    subprocess.run(args, check=True)


def check_disk():
    assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "/mnt/data-disk"], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def switch(target):
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    temp = CURRENT.with_name("next")
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    os.replace(str(temp), str(CURRENT))


def install():
    release = Path(__file__).resolve().parents[2]
    assert release.parent == ROOT / "releases" and len(release.name) == 40
    units_dir = Path("/etc/systemd/system")
    if CURRENT.exists() and not CURRENT.is_symlink():
        raise RuntimeError("current_not_symlink")
    backup = ROOT / "backups" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    meta = {"release": str(release), "previous": str(CURRENT.resolve()) if CURRENT.exists() else None,
            "files": {}, "installed_hashes": {}}
    for name in UNITS:
        path = units_dir / name
        meta["files"][name] = path.exists()
        if path.exists():
            shutil.copy2(path, backup / name)
        meta["installed_hashes"][name] = digest(release / "ops/youtube-cover-material-sync" / name)
    if (STATE / "sync.sqlite3").exists():
        source = sqlite3.connect((STATE / "sync.sqlite3").as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(backup / "sync.sqlite3"))
        source.backup(target)
        target.close()
        source.close()
    (backup / "rollback.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    switch(release)
    for name in UNITS:
        shutil.copy2(release / "ops/youtube-cover-material-sync" / name, units_dir / name)
    call("systemctl", "daemon-reload")
    call("systemd-analyze", "verify", *[str(units_dir / name) for name in UNITS])
    print(json.dumps({"installed": str(release), "backup": str(backup), "timer_started": False}))


def rollback(backup):
    backup = Path(backup).resolve()
    assert (ROOT / "backups").resolve() in backup.parents
    meta = json.loads((backup / "rollback.json").read_text())
    if CURRENT.exists() and str(CURRENT.resolve()) != meta["release"]:
        raise RuntimeError("newer_release_detected")
    for name, expected in meta["installed_hashes"].items():
        if digest(Path("/etc/systemd/system") / name) != expected:
            raise RuntimeError("unit_drift_detected")
    call("systemctl", "disable", "--now", UNITS[1])
    call("systemctl", "stop", UNITS[0])
    for name, existed in meta["files"].items():
        path = Path("/etc/systemd/system") / name
        if existed:
            shutil.copy2(backup / name, path)
        else:
            path.unlink()
    if meta["previous"]:
        switch(meta["previous"])
    elif CURRENT.is_symlink():
        CURRENT.unlink()
    call("systemctl", "daemon-reload")
    print(json.dumps({"rolled_back": str(backup), "state_preserved": str(STATE), "prior_timer_requires_reviewed_enable": bool(meta["previous"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback")
    args = parser.parse_args()
    check_disk()
    rollback(args.rollback) if args.rollback else install()

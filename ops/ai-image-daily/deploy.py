#!/usr/bin/env python3
"""Install this isolated report from an exact fetched Git commit; never start it."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


UNITS = ("ai-image-daily.service", "ai-image-daily.timer")
ROOT = Path("/mnt/data-disk/deploy/ai-image-daily")
STATE = Path("/mnt/data-disk/ai-image-daily-v3")
CURRENT = Path("/opt/ai-image-daily/current")
UNITS_DIR = Path("/etc/systemd/system")
MODULE = "ops/ai-image-daily"
DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"


def call(*args):
    subprocess.run(args, check=True)


def output(*args):
    return subprocess.check_output(args)


def check_disk():
    uuid = output("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk").decode().strip()
    if uuid != DISK_UUID:
        raise RuntimeError("data_disk_uuid_mismatch")
    # ProtectSystem=strict keeps the mount root read-only in the service while
    # ReadWritePaths grants the existing report state directory. Only a first
    # installation without STATE needs write access to the mount root itself.
    writable_target = STATE if STATE.exists() else Path("/mnt/data-disk")
    if not os.access(str(writable_target), os.W_OK):
        raise RuntimeError("data_disk_not_writable")
    if shutil.disk_usage("/mnt/data-disk").free < 1024 ** 3:
        raise RuntimeError("data_disk_free_space_below_1_gib")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def switch(target):
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    temporary = CURRENT.with_name("next")
    if temporary.is_symlink():
        temporary.unlink()
    elif temporary.exists():
        raise RuntimeError("unexpected_current_next_file")
    temporary.symlink_to(target)
    os.replace(str(temporary), str(CURRENT))


def validate_release(repo, expected_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise RuntimeError("expected_full_commit_required")
    release = Path(__file__).resolve().parents[2]
    if release.parent != ROOT / "releases" or release.name != expected_commit:
        raise RuntimeError("release_path_commit_mismatch")
    actual = output("git", "-C", str(repo), "rev-parse", expected_commit + "^{commit}").decode().strip()
    if actual != expected_commit:
        raise RuntimeError("git_commit_mismatch")
    tracked = output("git", "-C", str(repo), "ls-tree", "-r", "-z", expected_commit, "--", MODULE)
    files = {}
    for record in tracked.split(b"\0"):
        if not record:
            continue
        metadata, relative_raw = record.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        relative = relative_raw.decode()
        if mode not in ("100644", "100755") or kind != "blob":
            raise RuntimeError("release_non_regular_file")
        path = release / relative
        if path.is_symlink() or release not in path.resolve().parents:
            raise RuntimeError("release_file_outside_root")
        expected = hashlib.sha256(output("git", "-C", str(repo), "cat-file", "blob", blob)).hexdigest()
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError("release_file_differs_from_git:" + relative)
        files[relative] = expected
    required = {MODULE + "/" + name for name in UNITS}
    required.update({MODULE + "/deploy.py", MODULE + "/report.py"})
    if not required.issubset(files):
        raise RuntimeError("required_release_files_not_in_commit")
    # Check every Python source without generating files in the immutable release.
    for relative in files:
        if relative.endswith(".py"):
            compile((release / relative).read_bytes(), relative, "exec")
    return release, files


def install(repo, expected_commit):
    release, files = validate_release(repo, expected_commit)
    for name in UNITS:
        status = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        if status.stdout.strip() in ("active", "activating", "deactivating", "reloading"):
            raise RuntimeError("report_unit_must_be_idle_before_install:" + name)
    if CURRENT.exists() and not CURRENT.is_symlink():
        raise RuntimeError("current_not_symlink")
    if CURRENT.is_symlink() and not CURRENT.exists():
        raise RuntimeError("current_symlink_target_missing")
    # A first install has no CURRENT target yet. Validate the same units using
    # release paths in a private temporary directory before touching live units.
    validation = ROOT / (".verify-" + str(os.getpid()))
    validation.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        for name in UNITS:
            source = (release / MODULE / name).read_text(encoding="utf-8")
            (validation / name).write_text(source.replace(str(CURRENT), str(release)), encoding="utf-8")
        call("systemd-analyze", "verify", *[str(validation / name) for name in UNITS])
    finally:
        for name in UNITS:
            (validation / name).unlink(missing_ok=True)
        validation.rmdir()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = ROOT / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    meta = {
        "release": str(release), "commit": expected_commit,
        "previous": str(CURRENT.resolve()) if CURRENT.exists() else None,
        "files": {}, "installed_hashes": {}, "release_hashes": files,
        "state_small_files": [], "timer_previously_enabled": False,
    }
    for name in UNITS:
        path = UNITS_DIR / name
        if path.is_symlink():
            raise RuntimeError("unit_symlink_requires_review:" + name)
        meta["files"][name] = path.exists()
        if path.exists():
            shutil.copy2(path, backup / name)
        meta["installed_hashes"][name] = files[MODULE + "/" + name]
    enabled = subprocess.run(["systemctl", "is-enabled", UNITS[1]], capture_output=True, text=True)
    meta["timer_previously_enabled"] = enabled.returncode == 0
    # Diagnostic metadata only. Never overwrite report history or restore state
    # on code rollback, since a newer successful public report may exist.
    if STATE.exists():
        for path in sorted(STATE.glob("*.json")):
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024 ** 2:
                target = backup / "state-small-files" / path.name
                target.parent.mkdir(exist_ok=True, mode=0o700)
                shutil.copy2(path, target)
                meta["state_small_files"].append(path.name)
    (backup / "rollback.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    switch(release)
    for name in UNITS:
        shutil.copy2(release / MODULE / name, UNITS_DIR / name)
    call("systemctl", "daemon-reload")
    print(json.dumps({"installed": str(release), "commit": expected_commit,
                      "backup": str(backup), "timer_started": False}))


def rollback(backup):
    backup = Path(backup).resolve()
    if (ROOT / "backups").resolve() not in backup.parents:
        raise RuntimeError("backup_outside_expected_root")
    meta = json.loads((backup / "rollback.json").read_text(encoding="utf-8"))
    if not CURRENT.is_symlink() or str(CURRENT.resolve()) != meta["release"]:
        raise RuntimeError("current_release_drift_detected")
    for name in UNITS:
        path = UNITS_DIR / name
        if path.is_symlink() or not path.is_file() or digest(path) != meta["installed_hashes"][name]:
            raise RuntimeError("unit_drift_detected:" + name)
    call("systemctl", "disable", "--now", UNITS[1])
    call("systemctl", "stop", UNITS[0])
    for name in UNITS:
        path = UNITS_DIR / name
        if meta["files"][name]:
            shutil.copy2(backup / name, path)
        else:
            path.unlink()
    if meta["previous"]:
        switch(meta["previous"])
    else:
        CURRENT.unlink()
    call("systemctl", "daemon-reload")
    print(json.dumps({"rolled_back": str(backup), "state_preserved": str(STATE),
                      "public_report_preserved": True,
                      "prior_timer_requires_reviewed_enable": meta["timer_previously_enabled"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback", metavar="BACKUP_DIR")
    parser.add_argument("--repo", type=Path, help="server Git repository already fetched from GitHub")
    parser.add_argument("--expected-commit", help="exact 40-character GitHub commit")
    parser.add_argument("--check-disk", action="store_true")
    args = parser.parse_args()
    check_disk()
    if args.check_disk:
        if args.rollback or args.repo or args.expected_commit:
            parser.error("--check-disk must be used alone")
        return
    if args.rollback:
        if args.repo or args.expected_commit:
            parser.error("--rollback cannot be combined with installation arguments")
        rollback(args.rollback)
    else:
        if not args.repo or not args.expected_commit:
            parser.error("installation requires --repo and --expected-commit")
        install(args.repo, args.expected_commit)


if __name__ == "__main__":
    main()

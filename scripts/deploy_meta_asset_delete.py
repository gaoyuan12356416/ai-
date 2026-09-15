"""Selected-file deployment with a verified backup and fail-closed rollback.

Run only on the CPU server from a verified GitHub checkout. SQLite backups are
evidence/recovery assets and are NEVER restored by code rollback.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

LIVE = Path("/root/drama_material_service")
PUBLIC = Path("/usr/share/nginx/html")
DISK = Path("/mnt/data-disk")
ROOT = DISK / "meta-ad-asset-delete"
UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
SELECTED = ["app.py", "static/index.html", "static/quick-nav.js", "static/fb-post-ad-delete.html",
            "static/fb-post-ad-delete.js", "static/fb-post-ad-delete.css"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_disk():
    assert DISK.is_mount(), "data disk is not mounted"
    assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(DISK)], text=True).strip() == UUID


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".meta-assets-tmp")
    shutil.copyfile(str(source), str(tmp))
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(target))


def legacy_disabled(source):
    """Keep all old delete entry functions closed even after code rollback."""
    lines = source.splitlines(keepends=True)
    result, found = [], 0
    for line in lines:
        result.append(line)
        if any(line.startswith("def " + name + "(") for name in
                ("fb_post_ad_delete_create_preview", "fb_post_ad_delete_start", "fb_post_ad_delete_run")):
            assert line.rstrip().endswith(":")
            result.append('    raise RuntimeError("Meta deletion is disabled during rollback; do not use legacy execution")\n')
            found += 1
    assert found == 3, "rollback entry points drifted"
    result = "".join(result)
    compile(result, "rollback_app.py", "exec")
    return result


def prepare(release):
    validate_disk()
    release = release.resolve()
    assert ROOT in release.parents
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=str(release), text=True).strip() == ""
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(release), text=True).strip()
    baseline = json.loads((release / "doc/20260915.meta-drama-asset-delete/production-baseline.json").read_text())
    for rel in SELECTED:
        assert digest(LIVE / rel) == baseline[rel]["sha256"], "live drift: " + rel
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / ("backup-" + stamp + "-" + commit[:12])
    backup.mkdir(mode=0o700)
    plan = {"commit": commit, "release": str(release), "backup": str(backup), "files": [], "created_at": stamp}
    files = SELECTED + [str(p.relative_to(release)) for p in sorted((release / "features/fb_ad_asset_delete").glob("*.py"))]
    for rel in files:
        targets = [(LIVE / rel, "code/" + rel)]
        if rel.startswith("static/"):
            targets.append((PUBLIC / Path(rel).name, "public/" + Path(rel).name))
        for target, saved in targets:
            item = dict(source=rel, target=str(target), saved=saved, before=None, after=digest(release / rel))
            if target.exists():
                dest = backup / saved
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(target), str(dest))
                item["before"] = digest(dest)
            plan["files"].append(item)
    for path, name in ((LIVE / "data/drama_material_jobs.sqlite3", "legacy-jobs.sqlite3"),
                        (DISK / "fb-ad-asset-delete/tasks.sqlite3", "asset-jobs.sqlite3")):
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(str(backup / name))) as dst:
                src.backup(dst)
                assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    (backup / "unit.txt").write_text(subprocess.check_output(["systemctl", "cat", "drama-material-api.service"], text=True))
    (backup / "rollback_app.py").write_text(legacy_disabled((backup / "code/app.py").read_text()), encoding="utf-8")
    (backup / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    hashes = {str(p.relative_to(backup)): digest(p) for p in backup.rglob("*") if p.is_file()}
    (backup / "backup-sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    verify(backup)
    print(json.dumps({"backup": str(backup), "commit": commit, "files": len(plan["files"])}))


def verify(backup):
    assert ROOT in backup.resolve().parents
    for rel, expected in json.loads((backup / "backup-sha256.json").read_text()).items():
        assert digest(backup / rel) == expected, "backup damaged: " + rel


def switch(backup, rollback=False):
    validate_disk()
    backup = backup.resolve()
    verify(backup)
    plan = json.loads((backup / "plan.json").read_text())
    release = Path(plan["release"])
    if not rollback:
        for item in plan["files"]:
            target = Path(item["target"])
            assert (digest(target) if target.exists() else None) == item["before"], "live changed since backup: " + str(target)
            assert digest(release / item["source"]) == item["after"], "release changed"
    else:
        for item in plan["files"]:
            target = Path(item["target"])
            assert digest(target) == item["after"], "live changed since deploy; review before rollback: " + str(target)
    for item in plan["files"]:
        target = Path(item["target"])
        if rollback:
            if item["source"] == "app.py":
                source = backup / "rollback_app.py"
            elif item["before"] is not None:
                source = backup / item["saved"]
            else:
                continue  # preserve new package and all runtime ledger facts
        else:
            source = release / item["source"]
        atomic_copy(source, target)
    print(json.dumps({"action": "rollback_closed" if rollback else "applied", "commit": plan["commit"], "backup": str(backup)}))
    # Restart is deliberately separate so callers first drain active publishers.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "apply", "rollback", "verify"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.path)
    elif args.mode == "verify":
        verify(args.path)
        print("backup verified")
    else:
        switch(args.path, rollback=args.mode == "rollback")


if __name__ == "__main__":
    main()

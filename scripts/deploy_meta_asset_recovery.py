"""GitHub-first selected-file recovery rollout; retain all current task/index data."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from deploy_meta_asset_delete import LIVE, PUBLIC, DISK, ROOT, atomic_copy, digest, validate_disk

BASE = "e18d88fc3f87a039f7a6511443d95d1ca95c8a42"
BASELINE_HASHES = {}  # Exact overrides for reviewed, generated live assets.
FILES = ["features/fb_ad_asset_delete/" + x + ".py" for x in ("bridge", "graph", "service", "source", "store", "video_index")]
FILES += ["static/fb-post-ad-delete." + ext for ext in ("html", "js", "css")]


def idle():
    db = DISK / "fb-ad-asset-delete/tasks.sqlite3"
    with closing(sqlite3.connect(db.as_uri()+"?mode=ro", uri=True)) as conn:
        assert not conn.execute("SELECT 1 FROM fb_asset_delete_v2_jobs WHERE status IN ('running','previewing') LIMIT 1").fetchone(), "active asset job"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fb_asset_delete_v2_rechecks'").fetchone():
            assert not conn.execute("SELECT 1 FROM fb_asset_delete_v2_rechecks WHERE status='running' LIMIT 1").fetchone(), "active recheck"


def prepare(release):
    validate_disk()
    idle()
    release = release.resolve()
    assert ROOT in release.parents
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=release, text=True).strip()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=release, text=True).strip()
    files = []
    for rel in FILES:
        old = subprocess.run(["git", "show", BASE+":"+rel], cwd=release, capture_output=True)
        before = BASELINE_HASHES.get(rel, hashlib.sha256(old.stdout).hexdigest() if old.returncode == 0 else None)
        targets = [(LIVE/rel, "code/"+rel)]
        if rel.startswith("static/"):
            targets.append((PUBLIC/Path(rel).name, "public/"+Path(rel).name))
        for target, saved in targets:
            assert (digest(target) if target.exists() else None) == before, "baseline drift: "+str(target)
            files.append(dict(source=rel, target=str(target), saved=saved, before=before, after=digest(release/rel)))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT/("recovery-backup-"+stamp+"-"+commit[:12])
    backup.mkdir(mode=0o700)
    for item in files:
        if item["before"]:
            dst = backup/item["saved"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item["target"], dst)
    ledger = DISK/"fb-ad-asset-delete/tasks.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri()+"?mode=ro", uri=True)) as src, closing(sqlite3.connect(str(backup/"tasks.sqlite3"))) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    plan = dict(commit=commit, release=str(release), files=files, backup=str(backup))
    (backup/"plan.json").write_text(json.dumps(plan, indent=2))
    (backup/"hashes.json").write_text(json.dumps({str(p.relative_to(backup)):digest(p) for p in backup.rglob("*") if p.is_file()}, indent=2))
    print(json.dumps(dict(backup=str(backup), commit=commit, files=len(files))))


def switch(backup, rollback=False):
    validate_disk()
    idle()
    backup = backup.resolve()
    assert ROOT in backup.parents
    for rel, expected in json.loads((backup/"hashes.json").read_text()).items():
        assert digest(backup/rel) == expected, "backup drift"
    plan = json.loads((backup/"plan.json").read_text())
    for item in plan["files"]:
        target = Path(item["target"])
        assert (digest(target) if target.exists() else None) == item["after" if rollback else "before"], "live drift: "+str(target)
        assert digest(Path(plan["release"])/item["source"]) == item["after"], "release drift"
    for item in plan["files"]:
        if rollback and item["before"] is None:
            continue  # old adapter does not import the new module; keep data intact
        source = backup/item["saved"] if rollback else Path(plan["release"])/item["source"]
        atomic_copy(source, Path(item["target"]))
    print(json.dumps(dict(action="rollback" if rollback else "applied", **{k:plan[k] for k in ("commit","backup")})))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "apply", "rollback"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    prepare(args.path) if args.mode == "prepare" else switch(args.path, args.mode == "rollback")


if __name__ == "__main__":
    main()

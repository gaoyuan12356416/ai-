"""Six-file source-query repair; backup and restore code only, never the ledger."""
import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import deploy_fb_group_page_languages as rollout

base, pool, delivery = rollout.base, rollout.pool, rollout.delivery
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "b66e3cf"
REF = "refs/heads/codex/fb-group-query-repair-20261029"
FILES = ["features/fb_video_publish/" + x + ".py" for x in ("group_sources", "runtime_source", "query_broker")]


def restore(manifest):
    allowed = {str(root / name) for root in (base.MAIN, base.FB) for name in FILES}
    for item in manifest["files"]:
        target, backup = Path(item["target"]), Path(item["backup"])
        base.require(str(target) in allowed, "Unexpected target")
        base.require(base.sha(target) in {item["before"], item["after"]}, "Target drift")
        base.require(base.sha(backup) == item["before"], "Backup drift")
    for item in manifest["files"]:
        base.atomic_bytes(Path(item["backup"]).read_bytes(), Path(item["target"]), item["mode"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    base.check_mount()
    rollout.assert_window()
    if args.rollback:
        manifest = json.loads(args.rollback.read_text())
        saved = rollout.saved_states()
        try:
            rollout.quiesce(saved)
            restore(manifest)
        finally:
            rollout.resume(saved)
        print(json.dumps({"rolled_back": True, "database_preserved": True}))
        return
    commit = delivery.verified_commit(REF)
    targets = []
    for name in FILES:
        source = ROOT / name
        compile(source.read_bytes(), name, "exec")
        expected = hashlib.sha256(pool.git_bytes(BASELINE, name)).hexdigest()
        for root in (base.MAIN, base.FB):
            target = root / name
            base.require(base.sha(target) == expected, "Live source drift: " + str(target))
            targets.append((source, target))
    print(json.dumps({"preflight": True, "commit": commit, "files": len(targets)}), flush=True)
    if not args.apply:
        return
    backup = base.BASE / "backups" / ("group-query-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:8])
    backup.mkdir(mode=0o700)
    saved = rollout.saved_states()
    manifest = {"commit": commit, "states": saved, "files": [], "phase": "prepared"}
    for i, (source, target) in enumerate(targets):
        old = backup / ("file-" + str(i))
        shutil.copy2(target, old)
        manifest["files"].append({"target": str(target), "backup": str(old), "before": base.sha(target),
                                  "after": base.sha(source), "mode": target.stat().st_mode & 0o777})
    manifest_path = backup / "manifest.json"
    pool.json_save(manifest_path, manifest)
    modified = False
    try:
        rollout.quiesce(saved)
        rollout.assert_window()
        base.snapshot_db(backup / "operational.sqlite3")
        columns, facts = rollout.facts(base.DB)
        manifest["facts"] = facts
        for (source, target), item in zip(targets, manifest["files"]):
            base.require(base.sha(target) == item["before"] and base.sha(source) == item["after"], "Source drift during drain")
        modified = True
        for source, target in targets:
            base.atomic_bytes(source.read_bytes(), target, target.stat().st_mode & 0o777)
        rollout.check_facts(base.DB, columns, facts)
        rollout.resume(saved)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed source mismatch")
        manifest["phase"] = "deployed"
        pool.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path), "services_restored": True}), flush=True)
    except Exception:
        if modified:
            rollout.quiesce(saved)
            restore(manifest)
        rollout.resume(saved)
        manifest["phase"] = "failed_restored"
        pool.json_save(manifest_path, manifest)
        raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Exact original-task admission for 1389. Plan/apply never call X or create queues."""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_video_publish import recovery_1389 as recovery
from features.pipeline_dispatch.x_manual_recovery import fingerprint
from scripts.recover_x_unstarted_manual import connect


def code_proof():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "features/x_video_publish/recovery_1389.py", "features/x_posts/service.py",
        "scripts/recover_x_manual_1389.py")}


def plan(path):
    with contextlib.closing(connect(path)) as conn:
        conn.execute("BEGIN")
        recovery.require(recovery.existing(conn) is None, "admission_already_exists")
        evidence = recovery.validate(conn, initial=True)
        value = {"kind": "x_manual_1389_zero_write_admission", "version": 1,
            "db_path": str(Path(path).resolve()), "run_id": 1389,
            "evidence_sha256": fingerprint(evidence), "code_proof": code_proof()}
    return dict(value, plan_sha256=fingerprint(value))


def backup_database(path, directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / ("manual-1389-" + uuid.uuid4().hex + ".sqlite3")
    with os.fdopen(os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"):
        pass
    with contextlib.closing(connect(path)) as src, contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        recovery.require(dst.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return str(target), digest.hexdigest()


def apply(path, saved, expected_sha, backup_dir, actor, reason):
    unsigned = {k: v for k, v in saved.items() if k != "plan_sha256"}
    recovery.require(saved.get("kind") == "x_manual_1389_zero_write_admission"
        and saved.get("version") == 1 and saved.get("run_id") == 1389
        and saved.get("plan_sha256") == expected_sha == fingerprint(unsigned), "plan_hash_invalid")
    recovery.require(saved["db_path"] == str(Path(path).resolve())
        and saved["code_proof"] == code_proof() and actor.strip() and reason.strip(), "path_code_or_actor_invalid")
    with contextlib.closing(connect(path)) as conn:
        prior = recovery.existing(conn)
        if prior:
            recovery.require(prior["plan_sha256"] == expected_sha
                and prior["evidence_sha256"] == saved["evidence_sha256"], "other_admission_exists")
            return {"applied": False, "already_applied": True, "run_id": 1389,
                "plan_sha256": expected_sha, "platform_called": False}
        recovery.require(fingerprint(recovery.validate(conn, initial=True)) == saved["evidence_sha256"], "plan_state_drift")
    backup_path, backup_sha = backup_database(path, backup_dir)
    with contextlib.closing(connect(path, False)) as conn:
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            recovery.require(recovery.existing(conn) is None, "concurrent_admission_exists")
            evidence = recovery.validate(conn, initial=True)
            recovery.require(fingerprint(evidence) == saved["evidence_sha256"], "plan_state_drift")
            recovery.install(conn)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("INSERT INTO x_video_1389_admission VALUES(1389,?,?,?,?,?,?,?)",
                (expected_sha, fingerprint(evidence), json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                 backup_sha, actor, reason, timestamp))
            changed = conn.execute("UPDATE x_post_manual_run SET status='queued',error_code='',error_message='',"
                "started_at='',finished_at='',updated_at=? WHERE id=1389 AND status='failed_preflight'",
                (timestamp,)).rowcount
            recovery.require(changed == 1, "admission_update_conflict")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"applied": True, "run_id": 1389, "plan_sha256": expected_sha,
        "backup_path": backup_path, "backup_sha256": backup_sha,
        "platform_called": False, "queues_created": 0, "old_reservation_state": "released"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--db", required=True)
    for name in ("plan", "plan-sha256", "backup-dir", "actor", "reason"):
        parser.add_argument("--" + name)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as out:
        if args.action == "plan":
            result = plan(args.db)
        else:
            recovery.require(all((args.plan, args.plan_sha256, args.backup_dir, args.actor, args.reason)), "apply_arguments_required")
            result = apply(args.db, json.loads(Path(args.plan).read_text(encoding="utf-8")), args.plan_sha256,
                args.backup_dir, args.actor, args.reason)
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    print(json.dumps({"ok": True, "action": args.action, "run_id": 1389, "plan_sha256": result["plan_sha256"]}))


if __name__ == "__main__":
    main()

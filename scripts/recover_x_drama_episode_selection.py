#!/usr/bin/env python3
"""One audited retry of two exact zero-write drama selection rejections."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import fingerprint, identity
from scripts.recover_x_unstarted_manual import RecoveryRejected, connect, require, rows

TABLE = "x_post_schedule_episode_filter_retry_audit"
SCHEMA = """CREATE TABLE IF NOT EXISTS x_post_schedule_episode_filter_retry_audit (
    run_id INTEGER PRIMARY KEY, identity_sha256 TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    authorized_at TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL)"""
EXPECTED = {
    1029: ("2026-11-02", "16:28", "2026-11-03T10:53:01Z", "2026-11-03T10:53:01Z"),
    1031: ("2026-11-03", "08:44", "2026-11-03T10:52:38Z", "2026-11-03T10:52:39Z"),
}
ACCOUNTS = [16, 14, 15, 13, 5, 11, 7, 4, 2, 9, 3, 21, 22, 23, 20, 19,
    42, 43, 40, 41, 38, 37, 36, 33, 53, 52, 51, 35, 50, 49, 48, 44, 47, 45, 46]


def verify_repair(commit, service_path, repository):
    require(isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}", commit), "repair_commit_required")
    service = Path(service_path).absolute()
    repo = Path(repository).absolute()
    result = subprocess.run(["git", "-C", str(repo), "show", commit + ":features/x_posts/service.py"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    require(result.returncode == 0, "repair_commit_unavailable")
    blob = result.stdout
    require(all(marker in blob for marker in (
        b'if conn.execute("SELECT 1 FROM x_post_queue WHERE episode_key=? LIMIT 1",',
        b'"AND NOT EXISTS(SELECT 1 FROM x_post_queue q WHERE q.episode_key="',
    )), "repair_commit_missing_episode_filter")
    require(service.is_file() and service.read_bytes() == blob, "deployed_service_not_repair_commit")
    return {"commit": commit, "service_path": str(service), "service_resolved_path": str(service.resolve()),
        "repository": str(repo), "service_sha256": hashlib.sha256(blob).hexdigest()}


def existing(c, run_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    return c.execute("SELECT * FROM x_post_schedule_episode_filter_retry_audit WHERE run_id=?", (run_id,)).fetchone()


def snapshot(c, run_ids):
    require(run_ids and run_ids == sorted(set(run_ids)) and
        all(type(i) is int and i in EXPECTED for i in run_ids), "exact_episode_retry_ids_required")
    result = []
    for run_id in run_ids:
        found = rows(c, "SELECT * FROM x_post_schedule_run WHERE id=?", (run_id,))
        require(len(found) == 1, "schedule_run_missing")
        run = found[0]
        day, minute, attempted, finished = EXPECTED[run_id]
        require(run["source_type"] == "drama" and run["run_date"] == day and run["publish_time"] == minute and
            run["slot_key"] == "xpost:schedule:v1:drama:" + day + ":" + minute.replace(":", "") and
            run["config_version"] == 27 and run["expected_count"] == len(ACCOUNTS) and
            json.loads(run["account_ids_json"]) == ACCOUNTS and run["material_pool_snapshot_max_id"] is None,
            "episode_retry_frozen_identity_mismatch")
        require(run["status"] == "failed_preflight" and run["error_code"] == "x_post_drama_episode_already_used" and
            run["error_message"] == "短剧集数已被其他发布队列占用" and
            run["plan_attempted_at"] == attempted and run["finished_at"] == finished and
            not any(run[k] for k in ("started_at", "queued_count", "published_count", "failed_count", "unknown_count")),
            "episode_retry_not_exact_zero_write_rejection")
        require(not c.execute("SELECT 1 FROM x_post_queue WHERE schedule_run_id=?", (run_id,)).fetchone(),
            "schedule_queue_exists")
        require(not c.execute("SELECT 1 FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id "
            "WHERE q.schedule_run_id=?", (run_id,)).fetchone(), "schedule_log_exists")
        first = rows(c, "SELECT * FROM x_post_schedule_zero_write_resume_audit WHERE run_id=?", (run_id,))
        require(len(first) == 1 and first[0]["identity_sha256"] == fingerprint(identity(run)),
            "original_recovery_audit_mismatch")
        require(existing(c, run_id) is None, "episode_retry_already_authorized")
        result.append({"run_id": run_id, "identity_sha256": fingerprint(identity(run)),
            "previous_run": run, "original_recovery_audit": first[0],
            "queue_count": 0, "log_count": 0,
            "rejection_location": "create_schedule_plan.episode_key_guard_before_queue_insert",
            "normal_preflight_required": True})
    return result


def plan(path, run_ids, commit, service_path, repository):
    repair = verify_repair(commit, service_path, repository)
    with contextlib.closing(connect(path)) as c:
        c.execute("BEGIN")
        result = {"kind": "x_drama_episode_filter_retry", "version": 1,
            "db_path": str(Path(path).resolve()), "run_ids": sorted(run_ids), "repair_evidence": repair,
            "items": snapshot(c, sorted(run_ids))}
    result["plan_sha256"] = fingerprint(result)
    return result


def apply(path, saved, expected_sha, backup_dir, actor, reason):
    unsigned = {k: v for k, v in saved.items() if k != "plan_sha256"}
    require(saved.get("kind") == "x_drama_episode_filter_retry" and saved.get("version") == 1 and
        saved.get("plan_sha256") == expected_sha == fingerprint(unsigned), "plan_hash_mismatch")
    require(saved["db_path"] == str(Path(path).resolve()) and isinstance(actor, str) and actor.strip() and
        isinstance(reason, str) and reason.strip(), "path_actor_or_reason_invalid")
    repair = saved["repair_evidence"]
    def check_repair():
        require(verify_repair(repair["commit"], repair["service_path"], repair["repository"]) == repair,
            "repair_evidence_drift")
    with contextlib.closing(connect(path)) as c:
        receipts = [existing(c, run_id) for run_id in saved["run_ids"]]
        if receipts and all(r is not None and r["plan_sha256"] == expected_sha for r in receipts):
            return {"applied": False, "already_applied": True, "plan_sha256": expected_sha,
                "run_ids": saved["run_ids"], "platform_called": False}
        check_repair()
        require(snapshot(c, saved["run_ids"]) == saved["items"], "plan_state_drift")
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / ("drama-episode-filter-retry-" + uuid.uuid4().hex + ".sqlite3")
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"):
        pass
    with contextlib.closing(connect(path)) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
        require(destination.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    digest = hashlib.sha256()
    with backup.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(data)
    with contextlib.closing(connect(path, False)) as c:
        try:
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN IMMEDIATE")
            check_repair()
            require(snapshot(c, saved["run_ids"]) == saved["items"], "plan_state_drift")
            c.execute(SCHEMA)
            for action in ("UPDATE", "DELETE"):
                c.execute("CREATE TRIGGER IF NOT EXISTS episode_retry_audit_no_" + action.lower() +
                    " BEFORE " + action + " ON x_post_schedule_episode_filter_retry_audit "
                    "BEGIN SELECT RAISE(ABORT,'episode_retry_audit_append_only'); END")
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for item in saved["items"]:
                evidence = dict(item, repair_evidence=repair)
                c.execute("INSERT INTO x_post_schedule_episode_filter_retry_audit VALUES(?,?,?,?,?,?,?,?)",
                    (item["run_id"], item["identity_sha256"], expected_sha, actor, reason, now,
                     digest.hexdigest(), json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
                cursor = c.execute("UPDATE x_post_schedule_run SET status='claimed',plan_attempted_at='',"
                    "error_code='',error_message='',finished_at='',updated_at=?,lease_heartbeat_at=? "
                    "WHERE id=? AND status='failed_preflight' AND plan_attempted_at=?",
                    (now, now, item["run_id"], item["previous_run"]["plan_attempted_at"]))
                require(cursor.rowcount == 1, "schedule_compare_and_swap_failed")
            c.commit()
        except Exception:
            c.rollback()
            raise
    return {"applied": True, "plan_sha256": expected_sha, "run_ids": saved["run_ids"],
        "backup_path": str(backup), "backup_sha256": digest.hexdigest(), "platform_called": False,
        "queues_created": 0, "original_recovery_audit_modified": False, "frozen_configuration_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-ids")
    parser.add_argument("--deployed-commit")
    parser.add_argument("--service-path")
    parser.add_argument("--repository")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--backup-dir")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        if args.action == "plan":
            require(all((args.run_ids, args.deployed_commit, args.service_path, args.repository)), "plan_arguments_required")
            result = plan(args.db, [int(i) for i in args.run_ids.split(",")], args.deployed_commit, args.service_path, args.repository)
        else:
            require(all((args.plan, args.plan_sha256, args.backup_dir, args.actor, args.reason)), "apply_arguments_required")
            result = apply(args.db, json.loads(Path(args.plan).read_text(encoding="utf-8")),
                args.plan_sha256, args.backup_dir, args.actor, args.reason)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: result[key] for key in ("plan_sha256", "run_ids", "applied", "already_applied") if key in result}))


if __name__ == "__main__":
    main()

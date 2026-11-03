#!/usr/bin/env python3
"""Plan/apply exact 2026-11-02/03 zero-write schedule recovery, without calling X."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import TABLE, SCHEMA, fingerprint, identity
from scripts.recover_x_unstarted_manual import RecoveryRejected, connect, require, rows

INCIDENT_DATES = {"2026-11-02", "2026-11-03"}
ERRORS = {"x_upstream_error", "x_accounts_unavailable", "x_post_source_query_failed"}


def existing(c, run_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    return c.execute("SELECT * FROM x_post_schedule_zero_write_resume_audit WHERE run_id=?", (run_id,)).fetchone()


def snapshot(c, run_ids, allowed_dates):
    require(run_ids and len(run_ids) == len(set(run_ids)) and
        all(type(i) is int and i > 0 for i in run_ids), "exact_unique_run_ids_required")
    require(allowed_dates and set(allowed_dates) <= INCIDENT_DATES, "outside_incident_dates")
    result = []
    for run_id in run_ids:
        found = rows(c, "SELECT * FROM x_post_schedule_run WHERE id=?", (run_id,))
        require(len(found) == 1, "schedule_run_missing")
        run = found[0]
        require(run["run_date"] in allowed_dates and run["source_type"] in {"material", "drama"},
            "schedule_outside_authorized_scope")
        require(run["status"] == "failed_preflight" and run["error_code"] in ERRORS and
            not any(run[k] for k in ("started_at", "plan_attempted_at", "queued_count", "published_count",
                "failed_count", "unknown_count")), "schedule_not_zero_write_preflight_failure")
        require(not c.execute("SELECT 1 FROM x_post_queue WHERE schedule_run_id=?", (run_id,)).fetchone(),
            "schedule_queue_exists")
        require(not c.execute("SELECT 1 FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id "
            "WHERE q.schedule_run_id=?", (run_id,)).fetchone(), "schedule_log_exists")
        # A scheduled reservation is the durable queue/pool binding. Manual
        # reservations are a different owner; the normal planner checks them.
        accounts = json.loads(run["account_ids_json"])
        require(isinstance(accounts, list) and accounts and len(accounts) == len(set(accounts)) and
            all(type(i) is int and i > 0 for i in accounts) and len(accounts) == run["expected_count"],
            "schedule_frozen_account_scope_invalid")
        safe_accounts = rows(c, "SELECT id,x_user_id,status,publish_approved,drama_language,deleted_at "
            "FROM x_authorized_account WHERE id IN (" + ",".join("?" for _ in accounts) + ") ORDER BY id", accounts)
        require(len(safe_accounts) == len(accounts), "schedule_account_missing")
        eligible = [r["id"] for r in safe_accounts if r["status"] == "active" and
            r["publish_approved"] == 1 and not r["deleted_at"]]
        require(eligible, "schedule_all_accounts_unavailable")
        require(existing(c, run_id) is None, "schedule_already_authorized")
        result.append({"run_id": run_id, "identity_sha256": fingerprint(identity(run)),
            "run_sha256": fingerprint(run), "previous_run": run,
            "account_state": safe_accounts, "currently_available_account_ids": eligible,
            "currently_unavailable_account_ids": [i for i in accounts if i not in eligible],
            "queue_count": 0, "log_count": 0,
            "account_preflight_required": True})
    return result


def plan(path, run_ids, allowed_dates):
    with contextlib.closing(connect(path)) as c:
        c.execute("BEGIN")
        result = {"kind": "x_zero_write_schedule_resume", "version": 1,
            "db_path": str(Path(path).resolve()), "run_ids": sorted(run_ids),
            "allowed_dates": sorted(allowed_dates), "items": snapshot(c, sorted(run_ids), allowed_dates)}
    result["plan_sha256"] = fingerprint(result)
    return result


def apply(path, saved, expected_sha, backup_dir, actor, reason):
    unsigned = {k: v for k, v in saved.items() if k != "plan_sha256"}
    require(saved.get("kind") == "x_zero_write_schedule_resume" and saved.get("version") == 1 and
        saved.get("plan_sha256") == expected_sha == fingerprint(unsigned), "plan_hash_mismatch")
    require(saved["db_path"] == str(Path(path).resolve()) and str(actor).strip() and str(reason).strip(),
        "path_actor_or_reason_invalid")
    with contextlib.closing(connect(path)) as c:
        receipts = [existing(c, i) for i in saved["run_ids"]]
        if receipts and all(r is not None and r["plan_sha256"] == expected_sha for r in receipts):
            return {"applied": False, "already_applied": True, "plan_sha256": expected_sha,
                "run_ids": saved["run_ids"], "platform_called": False}
        require(snapshot(c, saved["run_ids"], saved["allowed_dates"]) == saved["items"], "plan_state_drift")
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / ("schedule-zero-write-" + uuid.uuid4().hex + ".sqlite3")
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
            require(snapshot(c, saved["run_ids"], saved["allowed_dates"]) == saved["items"], "plan_state_drift")
            c.execute(SCHEMA)
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for item in saved["items"]:
                c.execute("INSERT INTO x_post_schedule_zero_write_resume_audit VALUES(?,?,?,?,?,?,?,?)",
                    (item["run_id"], item["identity_sha256"], expected_sha, str(actor), str(reason), now,
                     digest.hexdigest(), json.dumps(item, ensure_ascii=False, sort_keys=True)))
                cursor = c.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',"
                    "finished_at='',updated_at=?,lease_heartbeat_at=? WHERE id=? AND status='failed_preflight'",
                    (now, now, item["run_id"]))
                require(cursor.rowcount == 1, "schedule_compare_and_swap_failed")
            c.commit()
        except Exception:
            c.rollback()
            raise
    return {"applied": True, "plan_sha256": expected_sha, "run_ids": saved["run_ids"],
        "backup_path": str(backup), "backup_sha256": digest.hexdigest(), "platform_called": False,
        "queues_created": 0, "frozen_configuration_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-ids")
    parser.add_argument("--dates")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--backup-dir")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        if args.action == "plan":
            require(args.run_ids and args.dates, "exact_scope_required")
            result = plan(args.db, [int(i) for i in args.run_ids.split(",")], args.dates.split(","))
        else:
            require(all((args.plan, args.plan_sha256, args.backup_dir, args.actor, args.reason)), "apply_arguments_required")
            result = apply(args.db, json.loads(Path(args.plan).read_text(encoding="utf-8")),
                args.plan_sha256, args.backup_dir, args.actor, args.reason)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: result[key] for key in ("plan_sha256", "run_ids", "applied", "already_applied") if key in result}))


if __name__ == "__main__":
    main()

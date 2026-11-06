"""Audited, exact-task recovery before selection; never publishes or resets receipts.

Dry run by default. Reuse the original task, run, account and template version.
Apply only after fixing the candidate condition and taking an online DB backup.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

EVENT = "operator_no_candidate_recovery"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def recover(auto_db, native_db, run_id, task_ids, *, operation_id, actor, reason, apply=False):
    ids = sorted(set(int(x) for x in task_ids))
    require(ids and len(ids) == len(task_ids), "unique explicit task IDs required")
    require(operation_id and actor and reason, "operation, actor and reason required")
    with contextlib.closing(sqlite3.connect(Path(auto_db).resolve().as_uri() +
            ("?mode=rw" if apply else "?mode=ro"), uri=True, timeout=15)) as c:
        c.row_factory = sqlite3.Row
        c.execute("ATTACH DATABASE ? AS native", (Path(native_db).resolve().as_uri() + "?mode=ro",))
        c.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            old_events = [json.loads(r[0]) for r in c.execute(
                "SELECT details_json FROM x_auto_event WHERE run_id=? AND event_type=?",
                (run_id, EVENT))]
            previous = [r for r in old_events if r.get("operation_id") == operation_id]
            if previous:
                require(len(previous) == 1 and previous[0]["task_ids"] == ids,
                        "operation ID already used for a different scope")
                c.rollback()
                return {"already_applied": True, "task_ids": ids, "publish_requests": 0}
            run = c.execute("SELECT * FROM x_auto_run WHERE id=?", (run_id,)).fetchone()
            require(run is not None and run["status"] != "canceled", "run unavailable")
            tasks = []
            for tid in ids:
                row = c.execute("SELECT * FROM x_auto_task WHERE id=? AND run_id=?", (tid, run_id)).fetchone()
                require(row is not None, "task does not belong to run")
                row = dict(row)
                require(row["status"] == "no_candidate" and row["error_code"] == "x_auto_no_eligible_material",
                        "task is no longer an unselected no-candidate task")
                empty = ("material_id", "content_id", "execution_run_id", "execution_queue_id",
                    "execution_log_id", "publish_id", "publish_url", "unknown_outcome",
                    "preparation_attempt_count", "publish_attempt_count", "claim_worker",
                    "claim_token", "lease_expires_at_utc", "reserved_at_utc", "bridged_at_utc",
                    "published_at_utc")
                require(not any(row[k] for k in empty) and not json.loads(row["selection_json"]),
                        "task has selection, publication or active claim evidence")
                require(not c.execute("SELECT 1 FROM x_auto_material_ledger WHERE task_id=?", (tid,)).fetchone(),
                        "task has a material reservation")
                key = "x-auto-task-" + str(tid)
                require(not c.execute("SELECT 1 FROM native.x_post_manual_run WHERE external_task_key=? OR external_task_key LIKE ?",
                    (key, key + "-%")).fetchone(), "native execution already exists")
                require(not c.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?",
                    ("x:auto:" + str(tid),)).fetchone(), "task is held")
                tasks.append(row)
            untouched = [dict(r) for r in c.execute("SELECT * FROM x_auto_task WHERE run_id=? ORDER BY id", (run_id,))
                         if r["id"] not in ids]
            proof = {"operation_id": operation_id, "actor": actor, "reason": reason,
                "task_ids": ids, "before_tasks": tasks, "before_run": dict(run),
                "untouched_sha256": hashlib.sha256(encoded(untouched).encode()).hexdigest()}
            if apply:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for tid in ids:
                    require(c.execute("UPDATE x_auto_task SET status='pending',error_code='',error_message='',"
                        "finished_at_utc='',next_attempt_at_utc='',updated_at=? WHERE id=? AND status='no_candidate'",
                        (now, tid)).rowcount == 1, "task state changed")
                    c.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) "
                        "VALUES(?,?,'task_no_candidate_requeued','no_candidate','pending',?,?,?)",
                        (run_id, tid, reason, encoded({"operation_id": operation_id, "actor": actor}), now))
                c.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=?", (now, run_id))
                c.execute("INSERT INTO x_auto_event(run_id,event_type,from_status,to_status,message,details_json,created_at) "
                    "VALUES(?, ?, ?, 'running', ?, ?, ?)", (run_id, EVENT, run["status"], reason, encoded(proof), now))
                after = [dict(r) for r in c.execute("SELECT * FROM x_auto_task WHERE run_id=? ORDER BY id", (run_id,))
                         if r["id"] not in ids]
                require(after == untouched, "unrelated tasks changed")
                c.commit()
            else:
                c.rollback()
            return {"already_applied": False, "applied": apply, "task_ids": ids,
                    "untouched_count": len(untouched), "untouched_sha256": proof["untouched_sha256"], "publish_requests": 0}
        except BaseException:
            c.rollback()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto-db", required=True)
    parser.add_argument("--native-db", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--task-ids", required=True, nargs="+", type=int)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    args = vars(parser.parse_args())
    print(json.dumps(recover(**args), ensure_ascii=False))

#!/usr/bin/env python3
"""Read-only CPU X drain evidence, including preserved historical unknown posts."""
import argparse
import json
import pathlib
import re
import socket
import sqlite3
import time

from maintenance import BASE, RUN_ID, TRIGGERS, atomic_write, run, storage_guard

CALLERS = ["x-post-daily.service", "x-post-manual.service", "x-post-schedule-claim.service",
           "x-post-schedule.service", "x-auto-post-scheduler.service", "x-auto-post-runner.service"]
DBS = {"x": "/var/lib/x-post-automation/accounts.sqlite3",
       "auto": "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3"}


def active(unit):
    return run(["systemctl", "is-active", unit], check=False).stdout.strip()


def db_snapshot():
    result = {}
    for name, path in DBS.items():
        connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            if name == "x":
                queries = {
                    "queue_status": "SELECT status,COUNT(*) AS count FROM x_post_queue GROUP BY status",
                    "active_queue": "SELECT id,status FROM x_post_queue WHERE status NOT IN ('failed','published','skipped','cancelled')",
                    "active_runs": "SELECT id,status FROM x_post_schedule_run WHERE status IN ('queued','running','planning','publishing')",
                    "active_manual": "SELECT id,status FROM x_post_manual_run WHERE status IN ('queued','running','planning','publishing')",
                    "historical_unknown_posts": "SELECT id,queue_id,status,unknown_outcome,error_code,started_at,published_at,updated_at FROM x_post_publish_log WHERE COALESCE(unknown_outcome,0) != 0",
                }
            else:
                queries = {
                    "task_status": "SELECT status,COUNT(*) AS count FROM x_auto_task GROUP BY status",
                    "claims": "SELECT id,status,claim_phase,lease_expires_at_utc FROM x_auto_task WHERE COALESCE(claim_token,'') != ''",
                    "active_tasks": "SELECT id,status FROM x_auto_task WHERE status NOT IN ('failed','published','no_candidate','cancelled','skipped','ready','queued')",
                    "unknown_tasks": "SELECT id,status,unknown_outcome FROM x_auto_task WHERE COALESCE(unknown_outcome,0) != 0",
                }
            result[name] = {key: [dict(row) for row in connection.execute(query)] for key, query in queries.items()}
        finally:
            connection.close()
    return result


def inspect():
    storage_guard()
    if socket.gethostname() != "VM-0-108-centos":
        raise RuntimeError("CPU-only drain check")
    state_path = BASE / "gates.json"
    gate = json.loads(state_path.read_text()) if state_path.exists() else {"groups": []}
    services = {unit: active(unit) for unit in CALLERS}
    triggers = {unit: active(unit) for unit in TRIGGERS["x"]}
    sockets = [line for line in run(["ss", "-Hntp", "state", "established"]).stdout.splitlines()
               if re.search(r":18820\b", line)]
    db = db_snapshot()
    reasons = []
    if "x" not in gate["groups"]:
        reasons.append("public X writes are not gated")
    if any(value in ("active", "activating") for value in triggers.values()):
        reasons.append("X timer/path triggers still active")
    if any(value in ("active", "activating") for value in services.values()):
        reasons.append("X callers have not naturally finished")
    if sockets:
        reasons.append("repair HTTP requests still connected")
    for group, keys in (("x", ("active_queue", "active_runs", "active_manual")),
                        ("auto", ("claims", "active_tasks", "unknown_tasks"))):
        for key in keys:
            if db[group][key]:
                reasons.append(group + "." + key + " is not empty")
    # Unknown post outcomes are neither repaired nor retried by this migration.
    # A pre-existing failed outcome is retained and listed as evidence.
    if any(row["status"] not in ("failed", "unknown", "needs_review")
           for row in db["x"]["historical_unknown_posts"]):
        reasons.append("unknown post is still in an active publication state")
    return {"run_id": RUN_ID, "checked_at_epoch": time.time(), "ready": not reasons,
            "blocking_reasons": reasons, "services": services, "triggers": triggers,
            "repair_connections": sockets, "databases": db,
            "historical_unknown_policy": "preserve exact ledger; no retry or reconciliation API call"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    result = inspect()
    if args.output:
        target = args.output.resolve()
        if BASE.resolve() not in target.parents:
            raise RuntimeError("evidence must stay within this run's private control directory")
        atomic_write(target, json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready"] else 2)


if __name__ == "__main__":
    main()

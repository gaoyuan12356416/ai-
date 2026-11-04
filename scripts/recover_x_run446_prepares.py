#!/usr/bin/env python3
"""Recover the two audited, zero-write Run 446 account-verification failures.

Only central preparation jobs 32665/32724 are rearmed. The standard coordinator
claim increments their generation; native queues, material choices and Auto
tasks are preserved. This is not a generic historical retry command.
"""
from __future__ import annotations

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
from scripts.recover_pipeline_x_preparation import RecoveryRejected, dumps, sha, require, read_connection

SCOPE = ((32665, 5819, 2740, 79, 1774), (32724, 5839, 2752, 82, 1794))
KIND = "x-run446-zero-write-prepares-v1"


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timezone_required")
    return result


def one(c, sql, params):
    rows = c.execute(sql, params).fetchall()
    require(len(rows) == 1, "exact_row_required")
    return dict(rows[0])


def inspect(c, n, a, clock):
    result = []
    for job_id, queue_id, task_id, account_id, run_id in SCOPE:
        key = "x:queue:" + str(queue_id)
        job = one(c, "SELECT * FROM pipeline_job WHERE source_key=?", (key,))
        require((job["id"], job["source_id"], job["channel"], job["family"], job["stage"], job["status"], job["error_code"]) ==
                (job_id, str(queue_id), "x", "queue", "prepare", "failed", "x_upstream_error"), "dispatch_state_changed")
        require(job["generation"] == 1 and not any(job[k] for k in ("external_intent", "hold_reason", "canonical_receipt")), "dispatch_write_or_hold")
        require(job["finished_at"] and parse_time(job["finished_at"]) < clock - dt.timedelta(seconds=120)
                and (not job["lease_expires_at"] or parse_time(job["lease_expires_at"]) < clock), "claim_not_settled")
        require(not any(json.loads(job["artifact_json"] or "{}").get(k) for k in ("url", "sha256", "size")), "artifact_exists")
        for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_receipt", "pipeline_resource", "pipeline_legacy_resource"):
            require(not c.execute("SELECT 1 FROM " + table + " WHERE job_id=? LIMIT 1", (job_id,)).fetchone(), "execution_evidence:" + table)
        require(not c.execute("SELECT 1 FROM pipeline_publication WHERE source_key=?", (key,)).fetchone(), "publication_exists")
        acks = [dict(r) for r in c.execute("SELECT * FROM pipeline_delivery WHERE job_id=?", (job_id,))]
        require(acks and all(json.loads(row["response_json"]).get("accepted") is True
                            and json.loads(row["response_json"]).get("status") == "failed" for row in acks), "failure_ack_missing")
        history = one(c, "SELECT * FROM pipeline_claim_history WHERE job_id=?", (job_id,))
        require(history["generation"] == job["generation"] and history["claim_hash"] == job["claim_hash"], "claim_history_changed")
        failure = c.execute("SELECT details_json FROM pipeline_event WHERE job_id=? AND event_type='failed' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        require(failure and json.loads(failure[0]).get("code") == "x_upstream_error" and json.loads(failure[0]).get("unknown") is False, "failure_not_definitive")

        queue = one(n, "SELECT * FROM x_post_queue WHERE id=?", (queue_id,))
        require((queue["account_id"], queue["manual_run_id"], queue["status"]) == (account_id, run_id, "queued"), "native_queue_changed")
        log = one(n, "SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue_id,))
        require(log["status"] == "reserved" and not any(log[k] for k in ("attempt_count", "unknown_outcome", "x_media_id", "x_post_id", "started_at", "published_at")), "native_write_evidence")
        for table in ("pipeline_x_claim", "pipeline_x_operation", "pipeline_x_checkpoint", "pipeline_canonical_hold"):
            require(not n.execute("SELECT 1 FROM " + table + " WHERE source_key=?", (key,)).fetchone(), "native_execution_evidence:" + table)
        require(not n.execute("SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?", (queue_id,)).fetchone(), "repost_evidence")
        account = one(n, "SELECT id,status,publish_approved,last_verified_at FROM x_authorized_account WHERE id=?", (account_id,))
        require(account["status"] == "active" and account["publish_approved"] == 1
                and parse_time(account["last_verified_at"]) > parse_time(job["finished_at"]), "account_not_verified_after_failure")
        parent = one(n, "SELECT id,trigger_source,external_task_key,template_ref,template_version FROM x_post_manual_run WHERE id=?", (run_id,))
        require((parent["trigger_source"], parent["external_task_key"], parent["template_ref"], parent["template_version"]) ==
                ("auto_template", "x-auto-task-" + str(task_id), "x-auto-template-4", 6), "native_parent_changed")
        task = one(a, "SELECT id,run_id,template_id,template_version,account_id,material_id,execution_run_id,execution_queue_id,status,unknown_outcome,publish_id FROM x_auto_task WHERE id=?", (task_id,))
        require((task["run_id"], task["template_id"], task["template_version"], str(task["account_id"]), task["execution_run_id"], task["execution_queue_id"]) ==
                (446, 4, 6, str(account_id), run_id, queue_id), "auto_identity_changed")
        require(task["status"] in {"ready", "retry_wait", "reconciling"} and not task["unknown_outcome"] and not task["publish_id"]
                and str(task["material_id"]) == str(queue["material_id"]), "auto_state_changed")
        stable_job = {k: v for k, v in job.items() if k not in ("updated_at", "source_status")}
        task.pop("status")  # The existing observer can harmlessly poll during recovery.
        result.append({"job_id": job_id, "queue_id": queue_id, "task_id": task_id, "account_id": account_id,
                       "generation": job["generation"], "job_sha256": sha(stable_job), "queue_sha256": sha(queue),
                       "log_sha256": sha(log), "task_sha256": sha(task), "parent_sha256": sha(parent),
                       "acks_sha256": sha(acks), "history_sha256": sha(history)})
    return result


def make_plan(central, native, auto, clock=None):
    clock = clock or now()
    with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(auto)) as a:
        c.execute("BEGIN"); n.execute("BEGIN"); a.execute("BEGIN")
        items = inspect(c, n, a, clock)
    plan = {"kind": KIND, "central": str(Path(central).resolve()), "native": str(Path(native).resolve()),
            "auto": str(Path(auto).resolve()), "items": items, "captured_at": clock.isoformat()}
    plan["plan_sha256"] = sha(plan)
    return plan


def apply_plan(plan, expected_sha, backup_dir, actor, clock=now):
    require(plan.get("kind") == KIND and plan.get("plan_sha256") == expected_sha == sha({k: v for k, v in plan.items() if k != "plan_sha256"}), "plan_hash_mismatch")
    require(str(actor).strip(), "actor_required")
    fresh = make_plan(plan["central"], plan["native"], plan["auto"], clock())
    require(fresh["items"] == plan["items"], "plan_state_drift")
    destination = Path(backup_dir); destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = destination / ("dispatch-before-" + uuid.uuid4().hex + ".sqlite3")
    fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    with contextlib.closing(read_connection(plan["central"])) as source, contextlib.closing(sqlite3.connect(str(backup))) as target:
        source.backup(target)
        require(target.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_check_failed")
    hasher = hashlib.sha256()
    with backup.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    with contextlib.closing(sqlite3.connect(plan["central"], timeout=10, isolation_level=None)) as c, contextlib.closing(read_connection(plan["native"])) as n, contextlib.closing(read_connection(plan["auto"])) as a:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL"); c.execute("BEGIN IMMEDIATE"); n.execute("BEGIN"); a.execute("BEGIN")
        try:
            require(inspect(c, n, a, clock()) == plan["items"], "plan_state_drift")
            stamp = clock().isoformat()
            for item in plan["items"]:
                # Same admission primitive as recover_pipeline_x_preparation:
                # clear stale claim ownership; next standard claim increments
                # generation and permanently fences the old worker.
                count = c.execute("UPDATE pipeline_job SET status='queued',started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',updated_at=? WHERE id=? AND stage='prepare' AND status='failed' AND generation=? AND error_code='x_upstream_error' AND external_intent=0", (stamp, item["job_id"], item["generation"])).rowcount
                require(count == 1, "rearm_conflict")
                audit = {"actor": str(actor), "plan_sha256": expected_sha, "backup_sha256": digest, "before": item,
                         "reason": "run446_verified_account_after_zero_write_upstream_failure", "native_modified": False, "auto_modified": False}
                c.execute("INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)", (item["job_id"], "audited_run446_prepare_recovery", dumps(audit), stamp))
            c.commit()
        except Exception:
            c.rollback(); raise
    return {"applied": True, "plan_sha256": expected_sha, "job_ids": [i["job_id"] for i in plan["items"]],
            "queue_ids": [i["queue_id"] for i in plan["items"]], "backup_path": str(backup), "backup_sha256": digest,
            "native_modified": False, "auto_modified": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "apply"))
    p.add_argument("--central", default="/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3")
    p.add_argument("--native", default="/var/lib/x-post-automation/accounts.sqlite3")
    p.add_argument("--auto", default="/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
    p.add_argument("--plan"); p.add_argument("--plan-sha256"); p.add_argument("--backup-dir"); p.add_argument("--actor")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as out:
        if args.action == "plan":
            result = make_plan(args.central, args.native, args.auto)
        else:
            require(all((args.plan, args.plan_sha256, args.backup_dir, args.actor)), "apply_arguments_required")
            result = apply_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")), args.plan_sha256, args.backup_dir, args.actor)
        out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n"); out.flush(); os.fsync(out.fileno())
    print(dumps({"ok": True, "action": args.action, "plan_sha256": result["plan_sha256"]}))


if __name__ == "__main__":
    try:
        main()
    except RecoveryRejected as exc:
        print(dumps({"ok": False, "code": str(exc)})); raise SystemExit(2)

#!/usr/bin/env python3
"""Close one of two audited media-only uncertainties; never retry the queue.

The original queue, attempts, operations and receipts remain failed/history.
This command does not rearm Auto task 2712: its original material was consumed
by a different confirmed publication. A later task remedy is a separate action.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

QUEUE, LOG, ACCOUNT, PARENT, JOB = 5393, 5327, 46, 1418, 31602
KEY = "x:queue:5393"
ERROR = "coordinator_unavailable"
CENTRAL_ERROR = "x_manual_parent_not_publishable"
NATIVE_OP_COUNT, CENTRAL_OP_COUNT = 5, 6
INCIDENTS = {
    5393: (5327, 46, 1418, 31602, "coordinator_unavailable", 5, 6),
    5857: (5777, 120, 1812, 32801, "coordinator_operation_failed", 191, 191),
}
AUDIT = "x_run446_media_close_audit"
EVENT = "operator_run446_media_only_closed"
KIND = "x_run446_media_only_close"
MEDIA = re.compile(r"x:POST:/2/media/upload(?:/initialize|/[A-Za-z0-9_.:-]+/append)\Z")
OP_FIELDS = ("operation_id", "generation", "operation", "request_sha256")


class RecoveryRejected(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise RecoveryRejected(message)


def use_incident(queue_id):
    """This maintenance CLI executes one fixed incident per process."""
    global QUEUE, LOG, ACCOUNT, PARENT, JOB, KEY, ERROR, CENTRAL_ERROR, NATIVE_OP_COUNT, CENTRAL_OP_COUNT
    require(queue_id in INCIDENTS, "queue_outside_fixed_scope")
    QUEUE = queue_id
    LOG, ACCOUNT, PARENT, JOB, ERROR, NATIVE_OP_COUNT, CENTRAL_OP_COUNT = INCIDENTS[queue_id]
    KEY = "x:queue:" + str(QUEUE)
    CENTRAL_ERROR = "x_manual_parent_not_publishable" if QUEUE == 5393 else ERROR


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def plan_hash(value):
    return digest({k: v for k, v in value.items() if k != "plan_sha256"})


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def stamp(value):
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timezone_required")
    return result


def rows(conn, query, values=()):
    return [dict(row) for row in conn.execute(query, values)]


def one(conn, query, values=()):
    found = rows(conn, query, values)
    require(len(found) == 1, "exact_row_missing")
    return found[0]


def connect(native, auto, central, *, writable=False):
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(Path(native).resolve().as_uri() + "?mode=" + mode,
                           uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ATTACH DATABASE ? AS dispatch", (Path(central).resolve().as_uri() + "?mode=" + mode,))
    # BEGIN IMMEDIATE requests a write lock on every attachment on some SQLite
    # versions. Attach rw for apply, but keep the evidence-only task store
    # protected against SQL writes with the connection authorizer.
    conn.execute("ATTACH DATABASE ? AS auto", (Path(auto).resolve().as_uri() + "?mode=" + mode,))
    if writable:
        conn.set_authorizer(lambda action, arg1, arg2, database, source:
                            sqlite3.SQLITE_DENY if database == "auto" and action != sqlite3.SQLITE_READ
                            else sqlite3.SQLITE_OK)
    else:
        conn.execute("PRAGMA query_only=ON")
    return conn


def existing(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (AUDIT,)).fetchone():
        return None
    found = rows(conn, "SELECT * FROM " + AUDIT + " WHERE queue_id=?", (QUEUE,))
    require(len(found) <= 1, "audit_identity_changed")
    return found[0] if found else None


def collect(conn):
    jobs = rows(conn, "SELECT * FROM dispatch.pipeline_job WHERE source_key=? ORDER BY id", (KEY,))
    require(len(jobs) == 2 and {j["stage"] for j in jobs} == {"prepare", "publish"}, "job_scope_changed")
    job_ids = [j["id"] for j in jobs]
    proof = {
        "queue": one(conn, "SELECT * FROM x_post_queue WHERE id=?", (QUEUE,)),
        "log": one(conn, "SELECT * FROM x_post_publish_log WHERE queue_id=?", (QUEUE,)),
        "parent": one(conn, "SELECT * FROM x_post_manual_run WHERE id=?", (PARENT,)),
        "siblings": rows(conn, "SELECT id FROM x_post_queue WHERE manual_run_id=? ORDER BY id", (PARENT,)),
        "jobs": jobs,
        "nodes": rows(conn, "SELECT node_id FROM dispatch.pipeline_node ORDER BY node_id"),
        "protected_tasks": rows(conn, "SELECT id,run_id,account_id,status,execution_run_id,execution_queue_id,execution_log_id,material_id,publish_id,unknown_outcome FROM auto.x_auto_task WHERE id IN (2712,2731,2780) ORDER BY id"),
    }
    for name, table in (("native_ops", "pipeline_x_operation"), ("native_checkpoints", "pipeline_x_checkpoint"),
                        ("claims", "pipeline_x_claim"), ("holds", "pipeline_canonical_hold")):
        proof[name] = rows(conn, "SELECT * FROM " + table + " WHERE source_key=? ORDER BY rowid", (KEY,))
    proof["reposts"] = rows(conn, "SELECT * FROM x_post_repost_ledger WHERE queue_id=?", (QUEUE,))
    proof["publications"] = rows(conn, "SELECT * FROM dispatch.pipeline_publication WHERE source_key=?", (KEY,))
    for name, table in (("central_ops", "pipeline_operation"), ("central_checkpoints", "pipeline_checkpoint"),
                        ("history", "pipeline_claim_history"), ("receipts", "pipeline_receipt"),
                        ("deliveries", "pipeline_delivery"), ("events", "pipeline_event"),
                        ("resources", "pipeline_resource"), ("legacy_resources", "pipeline_legacy_resource")):
        proof[name] = rows(conn, "SELECT * FROM dispatch." + table + " WHERE job_id IN (?,?) ORDER BY rowid", job_ids)
    return proof


def comparable(proof):
    value = json.loads(encoded(proof))
    # The coordinator refreshes these projection fields without changing an
    # execution. Preserve them in the audit but don't spuriously reject CAS.
    for job in value["jobs"]:
        for key in ("updated_at", "source_status", "source_meta_json"):
            job.pop(key, None)
    return value


def validate_native(proof, now):
    q, log, parent = proof["queue"], proof["log"], proof["parent"]
    require((q["id"], q["account_id"], q["manual_run_id"], q["status"], q["delivery_mode"])
            == (QUEUE, ACCOUNT, PARENT, "failed", "direct"), "queue_identity_changed")
    require((log["id"], log["account_id"], log["status"], log["unknown_outcome"], log["attempt_count"])
            == (LOG, ACCOUNT, "failed", 1, 1), "not_original_media_unknown")
    require(log["error_code"] == ERROR and not any(log.get(k) for k in
            ("x_media_id", "x_post_id", "x_post_url", "published_at")), "post_evidence_or_error_changed")
    require((parent["id"], parent["trigger_source"], parent["status"], parent["expected_count"],
             parent["queued_count"], parent["published_count"], parent["failed_count"], parent["unknown_count"])
            == (PARENT, "auto_template", "needs_review", 1, 1, 0, 0, 1)
            and proof["siblings"] == [{"id": QUEUE}], "parent_scope_changed")
    require({j["stage"]: j["status"] for j in proof["jobs"]} == {"prepare": "completed", "publish": "unknown"}, "job_state_changed")
    pub = next(j for j in proof["jobs"] if j["stage"] == "publish")
    require((pub["id"], pub["generation"], pub["source_id"], pub["channel"], pub["family"], pub["error_code"])
            == (JOB, 1, str(QUEUE), "x", "queue", CENTRAL_ERROR)
            and pub["external_intent"] == 1 and not pub["canonical_receipt"] and not pub["hold_reason"], "publish_identity_changed")
    claims = [r for r in proof["claims"] if r["stage"] == "publish"]
    history = [r for r in proof["history"] if r["job_id"] == JOB]
    require(len(claims) == len(history) == 1
            and (claims[0]["generation"], claims[0]["queue_id"], claims[0]["log_id"], claims[0]["claim_hash"])
            == (1, QUEUE, LOG, pub["claim_hash"])
            and (history[0]["generation"], history[0]["node_id"], history[0]["worker_id"], history[0]["claim_hash"])
            == (1, pub["node_id"], pub["worker_id"], pub["claim_hash"]), "original_claim_history_mismatch")
    require(all(not j["lease_expires_at"] or stamp(j["lease_expires_at"]) < now for j in proof["jobs"])
            and stamp(log["updated_at"]) < now - dt.timedelta(seconds=120), "execution_not_settled")
    require(not any(proof[k] for k in ("holds", "reposts", "publications", "legacy_resources")), "hold_or_publication_present")
    require(proof["resources"] == [{"resource_key": "x:account:" + str(ACCOUNT), "job_id": JOB,
            "generation": 1, "quarantined": 1, "legacy": 0}], "quarantine_scope_changed")
    native, central = proof["native_ops"], proof["central_ops"]
    require(len(native) == NATIVE_OP_COUNT and len(central) == CENTRAL_OP_COUNT, "operation_scope_changed")
    require(all(r["generation"] == 1 and MEDIA.fullmatch(r["operation"]) for r in native + central), "post_finalize_or_other_operation")
    native_set = {tuple(r[k] for k in OP_FIELDS) for r in native}
    central_set = {tuple(r[k] for k in OP_FIELDS) for r in central}
    require(native_set <= central_set and len(central_set - native_set) == CENTRAL_OP_COUNT - NATIVE_OP_COUNT, "operation_ledgers_disagree")
    require(all(row[2].endswith("/append") for row in central_set - native_set), "unapproved_operation_not_append")
    require(all(r["phase"] in {"x_preflight", "x_media_initialized"}
                for r in proof["native_checkpoints"] + proof["central_checkpoints"]), "later_checkpoint_present")
    require([r["id"] for r in proof["protected_tasks"]] == [2712, 2731, 2780], "run446_task_scope_missing")
    failures = [r for r in proof["events"] if r["job_id"] == JOB and r["event_type"] == "failed"]
    expected_failures = [ERROR, ERROR, CENTRAL_ERROR] if QUEUE == 5393 else [ERROR]
    require([json.loads(r["details_json"]).get("code") for r in failures] == expected_failures
            and all(json.loads(r["details_json"]).get("unknown") is True for r in failures), "original_unknown_event_missing")


def validate_evidence(evidence, proof, now):
    require(evidence.get("kind") == "x_run446_media_only_evidence" and evidence.get("queue_id") == QUEUE,
            "evidence_scope_invalid")
    require((evidence.get("worker_stopped") is True or evidence.get("worker_execution_stopped") is True)
            and evidence.get("historical_permit_guard_verified") is True,
            "worker_or_permit_guard_unproven")
    require(evidence.get("worker_proof") and evidence.get("historical_code_proof"), "worker_code_evidence_missing")
    nodes = evidence.get("nodes", [])
    require(sorted(n.get("node_id") for n in nodes) == [n["node_id"] for n in proof["nodes"]], "all_node_outboxes_required")
    jobs = {j["id"]: j for j in proof["jobs"]}
    failures = []
    for node in nodes:
        require(0 <= (now - stamp(node["checked_at"])).total_seconds() <= 900, "node_evidence_stale")
        require(node.get("complete_scope") is True, "node_receipt_scan_incomplete")
        for row in node.get("receipts", []):
            payload = row["payload"]
            require("claim_token" not in payload, "raw_claim_secret_forbidden")
            require(payload.get("job_id") in jobs and payload.get("generation") == 1
                    and row.get("acked_at"), "pending_or_other_receipt")
            require(not (payload.get("result") or {}).get("post_id")
                    and payload.get("phase") not in {"x_media_uploaded", "x_source_published", "x_target_reposted"}
                    and not (row["route"] == "jobs/complete" and payload["job_id"] == JOB), "later_or_success_receipt")
            delivery = [r for r in proof["deliveries"] if r["delivery_id"] == row["id"]]
            require(len(delivery) == 1 and delivery[0]["job_id"] == payload["job_id"]
                    and delivery[0]["generation"] == payload["generation"]
                    and delivery[0]["request_sha256"] == row["payload_sha256"]
                    and json.loads(delivery[0]["response_json"]).get("accepted") is True, "node_delivery_mismatch")
            if row["route"] == "jobs/fail" or payload.get("unknown_outcome"):
                pub = jobs[JOB]
                require(row["route"] == "jobs/fail" and payload["job_id"] == JOB
                        and payload.get("unknown_outcome") is True and payload.get("error_code") == ERROR
                        and payload.get("worker_id") == pub["worker_id"] and node["node_id"] == pub["node_id"]
                        and payload.get("claim_token_sha256") == pub["claim_hash"]
                        and delivery[0]["claim_hash"] == pub["claim_hash"], "original_failed_receipt_mismatch")
                failures.append(row)
    require(len(failures) == 1, "original_unknown_ack_required")


def make_plan(native, auto, central, evidence, *, now=None, queue_id=5393):
    use_incident(queue_id)
    now = now or now_utc()
    with contextlib.closing(connect(native, auto, central)) as conn:
        conn.execute("BEGIN")
        require(existing(conn) is None, "already_reconciled")
        proof = collect(conn)
        validate_native(proof, now)
        validate_evidence(evidence, proof, now)
    result = {"kind": KIND, "version": 1, "captured_at": now.isoformat(),
              "paths": {k: str(Path(v).resolve()) for k, v in (("native", native), ("auto", auto), ("central", central))},
              "proof": proof, "evidence": evidence, "evidence_sha256": digest(evidence),
              "effect": "close_old_queue_only_no_retry_no_task_change"}
    return dict(result, plan_sha256=plan_hash(result))


def backup_database(path, directory, label):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (label + "-" + uuid.uuid4().hex + ".sqlite3")
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with contextlib.closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            require(destination.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    os.chmod(target, 0o600)
    return {"path": str(target.resolve()), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def apply_plan(plan, backup_dir, actor, *, now=None):
    require(plan.get("kind") == KIND and plan.get("version") == 1
            and plan.get("plan_sha256") == plan_hash(plan)
            and plan.get("evidence_sha256") == digest(plan["evidence"]), "plan_or_evidence_hash_changed")
    use_incident(plan["proof"]["queue"]["id"])
    require(isinstance(actor, str) and actor.strip(), "actor_required")
    paths = plan["paths"]
    now = now or now_utc()
    with contextlib.closing(connect(**paths)) as conn:
        prior = existing(conn)
        if prior:
            require(prior["plan_sha256"] == plan["plan_sha256"], "different_reconciliation_exists")
            require(one(conn, "SELECT unknown_outcome,status,attempt_count FROM x_post_publish_log WHERE id=?", (LOG,))
                    == {"unknown_outcome": 0, "status": "failed", "attempt_count": 1}
                    and one(conn, "SELECT status FROM dispatch.pipeline_job WHERE id=?", (JOB,))["status"] == "failed"
                    and not rows(conn, "SELECT * FROM dispatch.pipeline_resource WHERE job_id=?", (JOB,)), "committed_reconciliation_drift")
            return {"already_applied": True, "queue_id": QUEUE, "platform_called": False, "task_modified": False}
    validate_evidence(plan["evidence"], plan["proof"], now)
    backups = {label: backup_database(paths[label], backup_dir, "run446-" + label) for label in ("native", "central", "auto")}
    timestamp = now.isoformat(timespec="seconds")
    with contextlib.closing(connect(**paths, writable=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require(existing(conn) is None, "concurrent_reconciliation")
            current = collect(conn)
            validate_native(current, now)
            validate_evidence(plan["evidence"], current, now)
            require(comparable(current) == comparable(plan["proof"]), "plan_state_drift")
            conn.execute("CREATE TABLE IF NOT EXISTS " + AUDIT + " (queue_id INTEGER PRIMARY KEY CHECK(queue_id IN (5393,5857)),plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)")
            for action in ("UPDATE", "DELETE"):
                conn.execute("CREATE TRIGGER IF NOT EXISTS " + AUDIT + "_no_" + action.lower() + " BEFORE " + action
                             + " ON " + AUDIT + " BEGIN SELECT RAISE(ABORT,'run446_reconciliation_audit_immutable'); END")
            conn.execute("INSERT INTO " + AUDIT + " VALUES(?,?,?,?)", (QUEUE, plan["plan_sha256"],
                         encoded({"plan": plan, "actor": actor, "backups": backups, "finding": "media_upload_stopped_before_post_intent"}), timestamp))
            require(conn.execute("UPDATE x_post_publish_log SET unknown_outcome=0,updated_at=? WHERE id=? AND queue_id=? AND status='failed' AND unknown_outcome=1 AND attempt_count=1 AND error_code=?",
                                 (timestamp, LOG, QUEUE, ERROR)).rowcount == 1, "native_cas_failed")
            require(conn.execute("UPDATE x_post_manual_run SET status='completed_with_errors',failed_count=1,unknown_count=0,finished_at=?,updated_at=? WHERE id=? AND status='needs_review' AND expected_count=1 AND queued_count=1 AND published_count=0 AND failed_count=0 AND unknown_count=1",
                                 (timestamp, timestamp, PARENT)).rowcount == 1, "parent_cas_failed")
            pub = next(j for j in current["jobs"] if j["id"] == JOB)
            meta = json.loads(pub["source_meta_json"])
            meta["unknown_outcome"] = False
            require(conn.execute("UPDATE dispatch.pipeline_job SET status='failed',source_status='failed',source_meta_json=?,finished_at=?,updated_at=? WHERE id=? AND status='unknown' AND generation=1 AND error_code=? AND hold_reason=''",
                                 (encoded(meta), timestamp, timestamp, JOB, CENTRAL_ERROR)).rowcount == 1, "central_cas_failed")
            require(conn.execute("DELETE FROM dispatch.pipeline_resource WHERE resource_key=? AND job_id=? AND generation=1 AND quarantined=1 AND legacy=0", ("x:account:" + str(ACCOUNT), JOB)).rowcount == 1, "quarantine_cas_failed")
            conn.execute("INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                         (JOB, EVENT, encoded({"plan_sha256": plan["plan_sha256"], "queue_id": QUEUE,
                          "actor": actor, "unknown_reconciled": True, "old_queue_retried": False, "attempts_preserved": 1}), timestamp))
            require(rows(conn, "SELECT id,run_id,account_id,status,execution_run_id,execution_queue_id,execution_log_id,material_id,publish_id,unknown_outcome FROM auto.x_auto_task WHERE id IN (2712,2731,2780) ORDER BY id")
                    == current["protected_tasks"], "task_modified_unexpectedly")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"already_applied": False, "queue_id": QUEUE, "log_id": LOG, "job_id": JOB,
            "status": "known_media_upload_failure", "old_queue_retried": False, "task_modified": False,
            "platform_called": False, "attempt_count": 1, "plan_sha256": plan["plan_sha256"], "backups": backups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--queue-id", type=int, choices=tuple(INCIDENTS), default=5393)
    for key in ("native", "auto", "central", "evidence", "plan", "backup-dir", "actor"):
        parser.add_argument("--" + key)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as output:
        if args.action == "plan":
            require(all((args.native, args.auto, args.central, args.evidence)), "plan_arguments_required")
            result = make_plan(args.native, args.auto, args.central, json.loads(Path(args.evidence).read_text(encoding="utf-8")), queue_id=args.queue_id)
        else:
            require(all((args.plan, args.backup_dir, args.actor)), "apply_arguments_required")
            result = apply_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")), args.backup_dir, args.actor)
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"ok": True, "action": args.action, "queue_id": QUEUE, "platform_called": False, "task_modified": False}))


if __name__ == "__main__":
    try:
        main()
    except RecoveryRejected as exc:
        print(json.dumps({"ok": False, "code": str(exc)}))
        raise SystemExit(2)

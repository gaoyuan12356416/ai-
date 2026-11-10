"""Correct X Auto state from immutable canonical Post receipts, without X calls.

The canonical database is always opened read-only. Only the exact selected Auto
tasks, their material-ledger status, aggregate run state and audit events change.
No publisher, token module, schema migration or recovery endpoint is invoked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA = "x-auto-confirmed-receipts-v1"
TASK_FIELDS = (
    "id", "run_id", "template_id", "template_version", "account_id", "material_id",
    "content_id", "execution_run_id", "execution_queue_id", "execution_log_id",
    "body_template", "body_sha256", "status", "publish_id", "publish_url",
    "unknown_outcome", "publish_attempt_count", "preparation_attempt_count",
    "published_at_utc", "finished_at_utc", "updated_at", "error_code",
)
TERMINAL = {"published", "failed", "canceled", "no_candidate", "skipped"}


class ReceiptReconciliationError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _connect(path, *, writable=False):
    resolved = Path(path).resolve(strict=True)
    conn = sqlite3.connect(resolved.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _require(condition, message):
    if not condition:
        raise ReceiptReconciliationError(message)


def _ids(task_ids):
    values = [int(value) for value in task_ids]
    _require(bool(values) and len(values) <= 100 and min(values) > 0 and len(values) == len(set(values)),
             "select between 1 and 100 distinct positive task IDs")
    return sorted(values)


def _inspect(auto, native, task_id):
    task = auto.execute("SELECT * FROM x_auto_task WHERE id=?", (task_id,)).fetchone()
    _require(task is not None, "Auto task missing")
    task = dict(task)
    _require(task["status"] in {"failed", "unknown", "published"}, "task is not an inactive terminal/review record")
    _require(not task["claim_token"] and not task["claim_worker"] and not task["lease_expires_at_utc"], "active or stale claim must be handled by its owner")
    run = auto.execute("SELECT * FROM x_auto_run WHERE id=?", (task["run_id"],)).fetchone()
    _require(run is not None and run["template_id"] == task["template_id"] and run["template_version"] == task["template_version"], "Auto run identity mismatch")
    queue = native.execute("SELECT * FROM x_post_queue WHERE id=?", (task["execution_queue_id"],)).fetchone()
    _require(queue is not None, "canonical queue missing")
    logs = native.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchall()
    _require(len(logs) == 1, "canonical receipt must be unique")
    log = logs[0]
    manual = native.execute("SELECT * FROM x_post_manual_run WHERE id=?", (queue["manual_run_id"],)).fetchone()
    _require(manual is not None and manual["id"] == task["execution_run_id"], "canonical execution run mismatch")
    _require(manual["trigger_source"] == "auto_template"
             and manual["external_task_key"] == "x-auto-task-" + str(task_id)
             and manual["template_ref"] == "x-auto-template-" + str(task["template_id"])
             and manual["template_version"] == task["template_version"], "canonical template envelope mismatch")
    _require(json.loads(manual["account_ids_json"]) == [int(task["account_id"])]
             and json.loads(manual["material_ids_json"]) == [str(task["material_id"])], "canonical account/material envelope mismatch")
    _require(str(queue["account_id"]) == str(task["account_id"])
             and str(log["account_id"]) == str(task["account_id"])
             and str(queue["material_id"]) == str(task["material_id"])
             and str(queue["content_id"]) == str(task["content_id"]), "canonical queue identity mismatch")
    _require(native.execute("SELECT count(*) FROM x_post_queue WHERE manual_run_id=?", (manual["id"],)).fetchone()[0] == 1,
             "canonical Auto run must contain exactly one queue")
    _require(task["body_template"] == manual["body_template"] == queue["body_template"]
             and hashlib.sha256(task["body_template"].encode("utf-8")).hexdigest() == task["body_sha256"],
             "frozen body template mismatch")
    _require(queue["status"] == "published" and log["status"] == "published" and not log["unknown_outcome"],
             "canonical publication is not confirmed")
    post_id = str(log["x_post_id"] or "")
    _require(bool(re.fullmatch(r"[1-9][0-9]{0,24}", post_id)), "canonical Post ID missing or invalid")
    url = urlsplit(log["x_post_url"] or "")
    _require(url.scheme == "https" and url.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
             and not url.username and not url.password and url.port in {None, 443}
             and url.path.rstrip("/").endswith("/status/" + post_id), "canonical Post URL does not match receipt")
    published = str(log["published_at"] or "")
    parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None and log["attempt_count"] > 0, "canonical publication timestamp/attempt evidence missing")
    published = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    _require(not task["publish_id"] or str(task["publish_id"]) == post_id, "Auto Post ID conflicts with canonical receipt")
    _require(task["status"] != "published" or str(task["publish_id"]) == post_id, "published Auto task lacks matching Post ID")
    _require(not task["execution_log_id"] or task["execution_log_id"] == log["id"], "Auto log identity conflicts with canonical receipt")
    reservation = auto.execute("SELECT * FROM x_auto_material_ledger WHERE task_id=?", (task_id,)).fetchone()
    _require(reservation is not None and reservation["canonical_queue_id"] == queue["id"]
             and str(reservation["material_id"]) == str(task["material_id"])
             and str(reservation["content_id"]) == str(task["content_id"])
             and reservation["run_id"] == task["run_id"] and reservation["template_id"] == task["template_id"],
             "Auto material reservation mismatch")
    prior = {key: task[key] for key in TASK_FIELDS}
    receipt = {
        "task_id": task_id, "auto_run_id": task["run_id"], "execution_run_id": manual["id"],
        "queue_id": queue["id"], "log_id": log["id"], "account_id": str(task["account_id"]),
        "material_id": str(task["material_id"]), "post_id": post_id,
        "post_url": log["x_post_url"], "published_at_utc": published,
        "native_attempt_count": log["attempt_count"],
    }
    return {"task_id": task_id, "task_sha256": _sha(prior), "receipt_sha256": _sha(receipt),
            "receipt": receipt, "previous": {key: task[key] for key in ("status", "unknown_outcome", "error_code", "publish_attempt_count", "preparation_attempt_count", "published_at_utc", "finished_at_utc")}}


def build_plan(auto_db, native_db, task_ids):
    ids = _ids(task_ids)
    with closing(_connect(auto_db)) as auto, closing(_connect(native_db)) as native:
        auto.execute("BEGIN")
        native.execute("BEGIN")
        items = [_inspect(auto, native, task_id) for task_id in ids]
    plan = {"schema": SCHEMA, "auto_db": str(Path(auto_db).resolve()), "native_db": str(Path(native_db).resolve()),
            "task_ids": ids, "items": items}
    plan["plan_sha256"] = _sha(plan)
    return plan


def _event(conn, *, task_id, run_id, previous, target, now, details, event_type):
    conn.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (run_id, task_id, event_type, previous, target, "Confirmed canonical receipt; no publication request", _json(details), now))


def _create_verified_backup(auto_db, backup):
    # Reserve the file exclusively with private permissions before SQLite opens
    # it, including when the process umask permits public files.
    try:
        fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        raise ReceiptReconciliationError("backup already exists; use a new backup directory without reapplying there") from None
    os.close(fd)
    with closing(_connect(auto_db)) as source, closing(sqlite3.connect(str(backup))) as target:
        source.backup(target, pages=256, sleep=0.01)
        _require(target.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup quick_check failed; application aborted")


def apply_plan(plan, auto_db, native_db, backup_dir):
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    _require(plan.get("schema") == SCHEMA and plan.get("plan_sha256") == _sha(payload), "plan digest mismatch")
    _require(plan["auto_db"] == str(Path(auto_db).resolve()) and plan["native_db"] == str(Path(native_db).resolve()), "plan database path mismatch")
    ids = _ids(plan["task_ids"])
    _require([item["task_id"] for item in plan["items"]] == ids, "plan task selection mismatch")
    destination = Path(backup_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = destination / "x-auto.before.sqlite3"
    _create_verified_backup(auto_db, backup)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed, already = [], []
    with closing(_connect(auto_db, writable=True)) as auto, closing(_connect(native_db)) as native:
        native.execute("BEGIN")
        auto.execute("BEGIN IMMEDIATE")
        try:
            current = [_inspect(auto, native, task_id) for task_id in ids]
            for expected, actual in zip(plan["items"], current):
                _require(actual["receipt_sha256"] == expected["receipt_sha256"], "canonical receipt changed since plan")
                if actual["previous"]["status"] == "published":
                    _require(not actual["previous"]["unknown_outcome"], "published task still has conflicting unknown marker")
                    already.append(actual["task_id"])
                    continue
                _require(actual["task_sha256"] == expected["task_sha256"], "Auto task changed since plan")
                receipt = actual["receipt"]
                auto.execute("UPDATE x_auto_task SET status='published',execution_log_id=?,publish_id=?,publish_url=?,published_at_utc=?,unknown_outcome=0,error_code='',error_message='',claim_phase='',next_attempt_at_utc='',updated_at=?,finished_at_utc=? WHERE id=?",
                             (receipt["log_id"], receipt["post_id"], receipt["post_url"], receipt["published_at_utc"], now, receipt["published_at_utc"], actual["task_id"]))
                auto.execute("UPDATE x_auto_material_ledger SET last_task_status='published',updated_at=? WHERE task_id=?", (now, actual["task_id"]))
                _event(auto, task_id=actual["task_id"], run_id=receipt["auto_run_id"], previous=actual["previous"]["status"], target="published", now=now,
                       details={"plan_sha256": plan["plan_sha256"], "previous": actual["previous"], "receipt": receipt}, event_type="task_confirmed_receipt_reconciled")
                changed.append(actual["task_id"])
            for run_id in sorted({item["receipt"]["auto_run_id"] for item in current if item["task_id"] in changed}):
                run = auto.execute("SELECT status FROM x_auto_run WHERE id=?", (run_id,)).fetchone()
                tasks = auto.execute("SELECT status,unknown_outcome FROM x_auto_task WHERE run_id=?", (run_id,)).fetchall()
                # A run canceled by the operator stays canceled; task truth is still corrected.
                if run["status"] == "canceled":
                    continue
                all_terminal = all(row["status"] in TERMINAL for row in tasks)
                if not all_terminal:
                    target = "running"
                else:
                    failed = sum(row["status"] in {"failed", "canceled"} or bool(row["unknown_outcome"]) for row in tasks)
                    target = "completed" if not failed else "failed" if failed == len(tasks) else "partial_failed"
                if target != run["status"]:
                    auto.execute("UPDATE x_auto_run SET status=?,updated_at=?,finished_at_utc=CASE WHEN ?='running' THEN '' ELSE COALESCE(NULLIF(finished_at_utc,''),?) END,error_code=CASE WHEN ?='completed' THEN '' ELSE error_code END,error_message=CASE WHEN ?='completed' THEN '' ELSE error_message END WHERE id=?",
                                 (target, now, target, now, target, target, run_id))
                    _event(auto, task_id=None, run_id=run_id, previous=run["status"], target=target, now=now,
                           details={"plan_sha256": plan["plan_sha256"]}, event_type="run_confirmed_receipt_reconciled")
            auto.commit()
        except BaseException:
            auto.rollback()
            raise
    result = {"plan_sha256": plan["plan_sha256"], "changed_task_ids": changed, "already_published_task_ids": already,
              "backup": str(backup), "at": now, "publish_requests": 0}
    (destination / "result.json").write_text(_json(result) + "\n", encoding="utf-8")
    return result

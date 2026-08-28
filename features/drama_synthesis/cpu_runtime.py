"""Small CPU-side ledger and lease-fenced writes for remote media execution.

This module never imports the application or sends notifications.  GPU media
files and its completed-result manifest remain owned by the GPU worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Mapping
from urllib.parse import urlsplit

from .core import DramaSynthesisError, complete_recipe_in_transaction


RUNTIME_TABLE = "drama_material_job_remote_runtime"
ACTIVE_STATUSES = frozenset(("queued", "validating", "downloading", "processing_cover", "rendering"))


class LeaseLostError(DramaSynthesisError):
    def __init__(self, message="任务执行权已变更，旧执行已停止回填"):
        super().__init__("drama_job_lease_lost", message, 409)


class RemoteStateConflict(DramaSynthesisError):
    def __init__(self, message="制作任务记录不一致，已停止重复提交"):
        super().__init__("drama_remote_state_conflict", message, 409)


@dataclass(frozen=True)
class LeaseIdentity:
    worker_id: str
    attempt: int

    def __post_init__(self):
        if not isinstance(self.worker_id, str) or not self.worker_id:
            raise ValueError("worker_id is required")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")

    def as_dict(self):
        return {"worker_id": self.worker_id, "attempt": self.attempt}


def _lease(value):
    if value is None or isinstance(value, LeaseIdentity):
        return value
    if isinstance(value, Mapping):
        return LeaseIdentity(value.get("worker_id"), value.get("attempt"))
    raise ValueError("invalid lease identity")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _row(cursor):
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((column[0] for column in cursor.description), value))


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def guard_current_lease(conn, job_id, lease, *, allow_done=False):
    """Validate a write inside its caller's BEGIN IMMEDIATE transaction.

    ``None`` is reserved for evidence-based legacy reconciliation.  It cannot
    take over any running lease.  Completed jobs reject progress/failure writes.
    """
    if not conn.in_transaction:
        raise RuntimeError("lease guard requires the caller's write transaction")
    identity = _lease(lease)
    job = _row(conn.execute("SELECT * FROM drama_material_job WHERE job_id=?", (str(job_id),)))
    if job is None:
        raise LeaseLostError("任务已不存在，已停止回填")
    owned = None
    if _table_exists(conn, "drama_material_job_worker_lease"):
        owned = _row(conn.execute(
            "SELECT worker_id,attempt,status FROM drama_material_job_worker_lease WHERE job_id=?",
            (str(job_id),),
        ))
    if identity is None:
        if owned and owned["status"] == "running":
            raise LeaseLostError()
    else:
        if not owned or owned["worker_id"] != identity.worker_id or int(owned["attempt"]) != identity.attempt:
            raise LeaseLostError()
        if owned["status"] != "running" and not (allow_done and job["status"] == "done" and owned["status"] == "done"):
            raise LeaseLostError()
    if job["status"] == "done" and not allow_done:
        raise LeaseLostError("任务已完成，已忽略迟到的进度或失败状态")
    return job


def ensure_runtime_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS drama_material_job_remote_runtime(
               job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
               payload_json TEXT NOT NULL, snapshot_json TEXT NOT NULL DEFAULT '{}',
               generation INTEGER NOT NULL DEFAULT 0,
               resume_requested_generation INTEGER NOT NULL DEFAULT 0,
               first_started_at TEXT NOT NULL DEFAULT '',
               created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
           )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(drama_material_job_remote_runtime)")}
    if "resume_requested_generation" not in columns:
        conn.execute("ALTER TABLE drama_material_job_remote_runtime ADD COLUMN resume_requested_generation INTEGER NOT NULL DEFAULT 0")


def _runtime_row(conn, job_id):
    if not _table_exists(conn, RUNTIME_TABLE):
        return None
    return _row(conn.execute("SELECT * FROM drama_material_job_remote_runtime WHERE job_id=?", (str(job_id),)))


def _decode(value):
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("not an object")
        return decoded
    except (ValueError, TypeError):
        raise RemoteStateConflict("制作任务的持久记录无法校验，已停止重复提交") from None


def get_remote_payload(db_path, job_id):
    """Internal-only frozen input; never include this object in a browser DTO."""
    conn = _connect(db_path)
    try:
        existing = _runtime_row(conn, job_id)
        return _decode(existing["payload_json"]) if existing else None
    finally:
        conn.close()


def get_remote_status(db_path, job_id):
    conn = _connect(db_path)
    try:
        existing = _runtime_row(conn, job_id)
        if not existing:
            return None
        snapshot = _decode(existing["snapshot_json"])
        snapshot.update({
            "job_id": str(job_id), "fingerprint": existing["fingerprint"],
            "generation": existing["generation"], "first_started_at": existing["first_started_at"],
        })
        return snapshot
    finally:
        conn.close()


def get_remote_resume_intent(db_path, job_id):
    conn = _connect(db_path)
    try:
        existing = _runtime_row(conn, job_id)
        generation = int((existing or {}).get("resume_requested_generation") or 0)
        return generation or None
    finally:
        conn.close()


def request_remote_resume(db_path, job_id, expected_generation):
    """Record only an explicit operator retry, bounded to one GPU generation."""
    if type(expected_generation) is not int or expected_generation < 1:
        raise RemoteStateConflict("制作执行代次无效，无法申请恢复")
    conn = _connect(db_path)
    try:
        ensure_runtime_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        job = guard_current_lease(conn, job_id, None)
        existing = _runtime_row(conn, job_id)
        if not existing or int(existing["generation"]) != expected_generation:
            raise RemoteStateConflict("制作执行代次已变化，请刷新任务状态")
        snapshot = _decode(existing["snapshot_json"])
        if job["status"] != "failed":
            raise RemoteStateConflict("任务尚未失败，不应提交恢复执行")
        if snapshot.get("status") not in {"failed", "recovery_required"} or snapshot.get("connection_state") == "reconnecting":
            conn.commit()
            return None
        pending = int(existing["resume_requested_generation"])
        if pending and pending != expected_generation:
            raise RemoteStateConflict("已有其他执行代次的恢复申请，请先同步状态")
        conn.execute(
            "UPDATE drama_material_job_remote_runtime SET resume_requested_generation=?,updated_at_utc=? WHERE job_id=? AND generation=?",
            (expected_generation, _now(), str(job_id), expected_generation),
        )
        conn.commit()
        return expected_generation
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remember_remote_submission(db_path, job_id, payload, lease):
    from .async_runtime import render_fingerprint

    if not isinstance(payload, Mapping) or str(payload.get("job_id") or "") != str(job_id):
        raise RemoteStateConflict()
    fingerprint = render_fingerprint(payload)
    encoded = _json(dict(payload))
    conn = _connect(db_path)
    try:
        ensure_runtime_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        guard_current_lease(conn, job_id, lease)
        existing = _runtime_row(conn, job_id)
        if existing:
            saved = _decode(existing["payload_json"])
            if existing["fingerprint"] != fingerprint or render_fingerprint(saved) != fingerprint:
                raise RemoteStateConflict()
            conn.commit()
            return saved
        now = _now()
        snapshot = {
            "job_id": str(job_id), "fingerprint": fingerprint, "generation": 0,
            "status": "queued", "stage": "submitting", "metrics": {},
            "started_at": "", "heartbeat_at": "", "last_progress_at": "",
        }
        conn.execute(
            """INSERT INTO drama_material_job_remote_runtime
                   (job_id,fingerprint,payload_json,snapshot_json,created_at_utc,updated_at_utc)
                   VALUES(?,?,?,?,?,?)""",
            (str(job_id), fingerprint, encoded, _json(snapshot), now, now),
        )
        conn.commit()
        return json.loads(encoded)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_remote_status(db_path, job_id, snapshot, lease):
    if not isinstance(snapshot, Mapping) or str(snapshot.get("job_id") or "") != str(job_id):
        raise RemoteStateConflict()
    keys = (
        "job_id", "fingerprint", "generation", "status", "stage", "started_at",
        "heartbeat_at", "last_progress_at", "created_at", "completed_at", "error",
        "connection_state", "error_code", "stalled", "result",
    )
    safe = {key: snapshot[key] for key in keys if key in snapshot}
    safe["metrics"] = dict(snapshot.get("metrics") or snapshot.get("progress") or {})
    if safe.get("status") != "completed":
        safe.pop("result", None)
    generation = safe.get("generation", 0)
    if type(generation) is not int or generation < 0:
        raise RemoteStateConflict()
    conn = _connect(db_path)
    try:
        ensure_runtime_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        job = guard_current_lease(conn, job_id, lease, allow_done=True)
        existing = _runtime_row(conn, job_id)
        if not existing or existing["fingerprint"] != str(safe.get("fingerprint") or ""):
            raise RemoteStateConflict()
        previous = _decode(existing["snapshot_json"])
        if generation < int(existing["generation"]):
            raise RemoteStateConflict("制作节点返回了旧执行代次，已停止回填")
        if job["status"] == "done" or previous.get("status") == "completed":
            conn.commit()
            return previous
        first_started = existing["first_started_at"] or str(safe.get("started_at") or "")
        conn.execute(
            """UPDATE drama_material_job_remote_runtime SET snapshot_json=?,generation=?,
                   first_started_at=?,updated_at_utc=?,
                   resume_requested_generation=CASE WHEN resume_requested_generation>0 AND ? > resume_requested_generation
                       THEN 0 ELSE resume_requested_generation END WHERE job_id=?""",
            (_json(safe), generation, first_started, _now(), generation, str(job_id)),
        )
        conn.commit()
        return safe
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fenced_update_job(db_path, job_id, changes, lease):
    allowed = {"status", "progress", "progress_detail", "error_message"}
    if not changes or not set(changes).issubset(allowed) or changes.get("status") == "done":
        raise ValueError("unsupported fenced update; use atomic_complete_job for completion")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        guard_current_lease(conn, job_id, lease)
        fields = sorted(changes)
        conn.execute(
            "UPDATE drama_material_job SET " + ",".join(key + "=?" for key in fields) + ",updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            tuple(changes[key] for key in fields) + (str(job_id),),
        )
        job = _row(conn.execute("SELECT * FROM drama_material_job WHERE job_id=?", (str(job_id),)))
        conn.commit()
        return job
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _output_url(value):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("invalid URL")
    except ValueError:
        raise DramaSynthesisError("drama_remote_output_invalid", "成片地址无法校验，已停止回填", 502) from None
    return str(value)


def _public_job(job, result):
    value = dict(job)
    value["outputs"] = _decode(job.get("outputs_json") or "{}")
    value["advanced_options"] = _decode(job.get("advanced_options_json") or "{}")
    if result.get("output_random_template_url"):
        value["output_random_template_url"] = result["output_random_template_url"]
    return value


def atomic_complete_job(db_path, job_id, result, lease, *, expected_recipe_sha256=""):
    """Commit the immutable recipe result and business completion together.

    This function sends no notification.  Its caller may notify after commit;
    A first success supersedes an earlier failure notification.  Repeated
    success consumption preserves the first completion and success notice.
    """
    if not isinstance(result, Mapping) or str(result.get("job_id") or "") != str(job_id):
        raise RemoteStateConflict("成片任务身份不一致，已停止回填")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = guard_current_lease(conn, job_id, lease, allow_done=True)
        outputs = _decode(job.get("outputs_json") or "{}")
        selected = {
            "concat_video": "output_video_url", "no_bgm_video": "output_video_no_bgm_url",
            "cover_16x9": "cover_16x9_url",
        }
        random_selected = bool(outputs.get("random_template_video", outputs.get("random_template", False)))
        if not any(outputs.get(key) for key in selected) and not random_selected:
            raise RemoteStateConflict("任务没有已选产物，无法确认完成")
        updates = {}
        for output, field in selected.items():
            if not outputs.get(output):
                continue
            url = _output_url(result.get(field) or job.get(field))
            if job.get(field) and job[field] != url:
                raise RemoteStateConflict("任务已有不同的成片地址，已停止覆盖")
            updates[field] = url
        if random_selected:
            actual = str(result.get("random_template_recipe_sha256") or "")
            if expected_recipe_sha256 and actual != expected_recipe_sha256:
                raise RemoteStateConflict("成片与冻结配方不一致，已停止回填")
            complete_recipe_in_transaction(
                conn, str(job_id), output_url=str(result.get("output_random_template_url") or ""),
                output_sha256=str(result.get("random_template_output_sha256") or ""),
                output_profile=str(result.get("random_template_output_profile") or ""),
                recipe_sha256=actual,
            )
        if job["status"] == "done":
            conn.commit()
            return _public_job(job, result)
        if job.get("completion_notified_at"):
            previous_runtime = _runtime_row(conn, job_id)
            if previous_runtime:
                previous_snapshot = _decode(previous_runtime["snapshot_json"])
                previous_snapshot["prior_failure_notified_at"] = job["completion_notified_at"]
                conn.execute(
                    "UPDATE drama_material_job_remote_runtime SET snapshot_json=? WHERE job_id=?",
                    (_json(previous_snapshot), str(job_id)),
                )
        fields = sorted(updates)
        clauses = [field + "=?" for field in fields]
        clauses.extend((
            "status='done'", "progress=100", "progress_detail='全部产物已生成'", "error_message=''",
            "completion_notified_at=''", "completion_notification_error=''",
            "finished_at=CASE WHEN TRIM(COALESCE(finished_at,''))='' THEN ? ELSE finished_at END",
            "updated_at=CURRENT_TIMESTAMP",
        ))
        conn.execute(
            "UPDATE drama_material_job SET " + ",".join(clauses) + " WHERE job_id=?",
            tuple(updates[field] for field in fields) + (_now(), str(job_id)),
        )
        completed = _row(conn.execute("SELECT * FROM drama_material_job WHERE job_id=?", (str(job_id),)))
        conn.commit()
        return _public_job(completed, result)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

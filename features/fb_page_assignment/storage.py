"""Durable assignment results, a MySQL outbox, and send-once alert state.

Store.save(task_id, external_task_id, rows, http_status, response, alert_reasons)
freezes a request before a response is returned. All Store reads return decoded
dicts; pending_sync() and pending_alerts() return lists of those dicts.
sync_task(task_id, Writer) returns True on a successful write and False on a
deferred/failed write. Failures remain durable and use bounded exponential
backoff. It never reruns selection.

Before sending an alert call claim_alert(task_id); only a non-None result owns
the send. Finish with mark_alert(task_id, 'sent'|'failed'|'unknown', ...).
Call recover_inflight_alerts() ONCE at process startup: interrupted sends become
unknown and are not automatically replayed. Notification delivery is separate
from assignment success. MySQL DDL belongs only in the deployment schema.sql.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone


TABLE = "`ads_ai`.`fb_drama_page_assignment_log`"
ENV_PREFIX = "FB_PAGE_ASSIGNMENT_MYSQL_WRITE_"
ALERT_STATES = frozenset({"none", "pending", "sending", "sent", "failed", "unknown"})
_WRITER_LOCK = threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _mysql_time(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def _safe_error(exc):
    # Never persist exception messages: a driver can include SQL or credentials.
    name = type(exc).__name__
    code = exc.args[0] if getattr(exc, "args", ()) else None
    return "%s:%s" % (name, code) if isinstance(code, int) else name


class PayloadConflictError(ValueError):
    """An immutable request/item identity was reused with different facts."""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_lock = threading.Lock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fb_page_assignment_requests (
                    task_id TEXT PRIMARY KEY,
                    external_task_id TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    initial_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    synced_revision INTEGER NOT NULL DEFAULT 0,
                    sync_attempts INTEGER NOT NULL DEFAULT 0,
                    sync_next_at REAL NOT NULL DEFAULT 0,
                    last_sync_error TEXT NOT NULL DEFAULT '',
                    alert_reasons_json TEXT NOT NULL,
                    alert_status TEXT NOT NULL DEFAULT 'none',
                    feishu_message_id TEXT NOT NULL DEFAULT '',
                    alert_error_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_fb_assignment_sync
                    ON fb_page_assignment_requests(sync_next_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_fb_assignment_alert
                    ON fb_page_assignment_requests(alert_status, created_at);
                CREATE INDEX IF NOT EXISTS idx_fb_assignment_external
                    ON fb_page_assignment_requests(external_task_id, created_at);
            """)
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for column, key in (("rows_json", "rows"), ("response_json", "response"),
                            ("alert_reasons_json", "alert_reasons")):
            result[key] = json.loads(result.pop(column))
        result["sync_status"] = ("synced" if result["synced_revision"] >= result["revision"] else "pending")
        return result

    def get(self, task_id):
        with self._connect() as db:
            return self._decode(db.execute(
                "SELECT * FROM fb_page_assignment_requests WHERE task_id=?", (task_id,),
            ).fetchone())

    def save(self, task_id, external_task_id, rows, http_status, response, alert_reasons):
        task_id, external_task_id = str(task_id), str(external_task_id or "")
        if not task_id or len(task_id) > 64 or not task_id.isascii():
            raise ValueError("task_id must be 1 to 64 ASCII characters")
        if len(external_task_id) > 128:
            raise ValueError("external_task_id exceeds 128 characters")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("rows must contain at most 100 items")
        if not isinstance(response, dict):
            raise ValueError("response must be an object")
        # Serialize now, before any mutation, to reject non-JSON data and detach
        # caller-owned objects from the frozen assignment.
        reasons = list(dict.fromkeys(str(reason) for reason in (alert_reasons or []) if reason))
        frozen_rows = json.loads(_json(rows))
        frozen_response = json.loads(_json(response))
        fingerprint = _hash({"external_task_id": external_task_id, "rows": frozen_rows,
                             "http_status": int(http_status), "response": frozen_response,
                             "alert_reasons": reasons})
        if not frozen_rows:
            frozen_rows = [{"item_index": 0, "drama_id": None, "page_id": None,
                            "status": "error", "selection_stage": "",
                            "error_code": str(frozen_response.get("error_code") or frozen_response.get("code") or "request_error"),
                            "error_message": str(frozen_response.get("message") or ""),
                            "diagnostics": {"request_error": frozen_response}}]
        else:
            for index, row in enumerate(frozen_rows, 1):
                if not isinstance(row, dict):
                    raise ValueError("each row must be an object")
                row["item_index"] = 0 if len(frozen_rows) == 1 and row.get("drama_id") is None else index
        now = _utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT initial_hash FROM fb_page_assignment_requests WHERE task_id=?", (task_id,)).fetchone()
            if previous:
                if previous["initial_hash"] != fingerprint:
                    raise PayloadConflictError("task_id reused with changed results")
            else:
                db.execute("""INSERT INTO fb_page_assignment_requests
                    (task_id,external_task_id,rows_json,http_status,response_json,
                     initial_hash,created_at,updated_at,alert_reasons_json,alert_status)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, external_task_id, _json(frozen_rows), int(http_status),
                     _json(frozen_response), fingerprint, now, now, _json(reasons),
                     "pending" if reasons else "none"))
        return self.get(task_id)

    def pending_sync(self, limit=20):
        with self._connect() as db:
            return [self._decode(row) for row in db.execute("""
                SELECT * FROM fb_page_assignment_requests
                WHERE synced_revision < revision AND sync_next_at <= ?
                ORDER BY created_at,task_id LIMIT ?""", (time.time(), max(1, min(int(limit), 100))))]

    def _sync_failed(self, task_id, exc):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM fb_page_assignment_requests WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return
            reasons = json.loads(row["alert_reasons_json"])
            is_new = "log_sync_pending" not in reasons
            if is_new:
                reasons.append("log_sync_pending")
            status = row["alert_status"]
            if status == "none":
                status = "pending"
            attempts = row["sync_attempts"] + 1
            delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
            db.execute("""UPDATE fb_page_assignment_requests SET sync_attempts=?,
                sync_next_at=?,last_sync_error=?,alert_reasons_json=?,alert_status=?,
                revision=revision+?,updated_at=? WHERE task_id=?""",
                (attempts, time.time() + delay, _safe_error(exc), _json(reasons), status,
                 int(is_new), _utc_now(), task_id))

    def sync_task(self, task_id, writer):
        # Both request-thread and background writes take this lock. The writer
        # has a second process-wide lock for callers using multiple Store objects.
        with self._sync_lock:
            request = self.get(task_id)
            if request is None:
                raise KeyError(task_id)
            if request["sync_status"] == "synced":
                return True
            if request["sync_next_at"] > time.time():
                return False
            try:
                writer.write_task(request)
            except Exception as exc:
                self._sync_failed(task_id, exc)
                return False
            with self._connect() as db:
                db.execute("""UPDATE fb_page_assignment_requests
                    SET synced_revision=MAX(synced_revision,?),sync_attempts=0,
                        sync_next_at=0,last_sync_error=''
                    WHERE task_id=?""", (request["revision"], task_id))
            return True

    def defer_sync(self, task_id, exc):
        """Keep frozen results pending after an outbox coordination exception.

        This may itself raise if local storage is unavailable. The caller must
        retain the original result and log that local failure without attempting
        to freeze a different payload under the same task ID.
        """
        self._sync_failed(task_id, exc)
        return self.get(task_id)

    def pending_alerts(self, limit=20):
        with self._connect() as db:
            return [self._decode(row) for row in db.execute("""
                SELECT * FROM fb_page_assignment_requests WHERE alert_status='pending'
                ORDER BY created_at,task_id LIMIT ?""", (max(1, min(int(limit), 100)),))]

    def claim_alert(self, task_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE fb_page_assignment_requests
                SET alert_status='sending',revision=revision+1,updated_at=?,sync_next_at=0
                WHERE task_id=? AND alert_status='pending'""", (_utc_now(), task_id)).rowcount
            row = db.execute("SELECT * FROM fb_page_assignment_requests WHERE task_id=?", (task_id,)).fetchone() if changed else None
        return self._decode(row)

    def mark_alert(self, task_id, status, message_id="", error_code=""):
        if status not in ALERT_STATES:
            raise ValueError("invalid alert status")
        if status == "sent" and not message_id:
            raise ValueError("sent alert requires a message receipt")
        if len(str(message_id)) > 128 or len(str(error_code)) > 96:
            raise ValueError("alert receipt fields exceed storage limits")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM fb_page_assignment_requests WHERE task_id=?", (task_id,)).fetchone()
            if previous is None:
                raise KeyError(task_id)
            if previous["alert_status"] == "sent" and (
                status, str(message_id), str(error_code)
            ) != (previous["alert_status"], previous["feishu_message_id"], previous["alert_error_code"]):
                raise ValueError("a confirmed delivery receipt cannot be overwritten")
            if previous["alert_status"] in {"sent", "unknown"} and status in {"none", "pending", "sending"}:
                raise ValueError("a delivered or uncertain alert cannot be automatically replayed")
            if (previous["alert_status"], previous["feishu_message_id"], previous["alert_error_code"]) != (status, str(message_id), str(error_code)):
                db.execute("""UPDATE fb_page_assignment_requests
                    SET alert_status=?,feishu_message_id=?,alert_error_code=?,
                        revision=revision+1,updated_at=?,sync_next_at=0 WHERE task_id=?""",
                    (status, str(message_id), str(error_code), _utc_now(), task_id))
        return self.get(task_id)

    def recover_inflight_alerts(self):
        with self._connect() as db:
            return db.execute("""UPDATE fb_page_assignment_requests
                SET alert_status='unknown',alert_error_code='delivery_interrupted',
                    revision=revision+1,updated_at=?,sync_next_at=0
                WHERE alert_status='sending'""", (_utc_now(),)).rowcount


class Writer:
    """A fixed-table MySQL 5.7 writer; never creates tables or runs reports.

    Only the exact target primary key is read, inside the write transaction, to
    verify immutable-result identity after an uncertain earlier write. Ordinary
    SQL inspection belongs on the 63350 read-only endpoint.
    """

    def __init__(self, env=None, *, connection_factory=None):
        env = os.environ if env is None else env
        self.config = {
            "host": env.get(ENV_PREFIX + "HOST", "101.32.56.53"),
            "port": int(env.get(ENV_PREFIX + "PORT", "63353")),
            "database": env.get(ENV_PREFIX + "DATABASE", "ads_ai"),
            "user": env.get(ENV_PREFIX + "USER", ""),
            "password": env.get(ENV_PREFIX + "PASSWORD", ""),
        }
        if (self.config["host"], self.config["port"], self.config["database"]) != ("101.32.56.53", 63353, "ads_ai"):
            raise ValueError("assignment log writer target must be the dedicated ads_ai write endpoint")
        if not self.config["user"] or not self.config["password"]:
            raise ValueError("assignment log writer credentials are not configured")
        self.connection_factory = connection_factory

    def _connect(self):
        connect = self.connection_factory
        if connect is None:
            import pymysql
            connect = pymysql.connect
        return connect(**self.config, charset="utf8mb4", autocommit=False,
                       connect_timeout=3, read_timeout=5, write_timeout=5)

    @staticmethod
    def _record(request, row):
        immutable = {
            "task_id": request["task_id"], "item_index": int(row["item_index"]),
            "external_task_id": request["external_task_id"], "pool_group_id": 62,
            "drama_id": row.get("drama_id"), "page_id": row.get("page_id"),
            "status": str(row.get("status") or "error"),
            "selection_stage": str(row.get("selection_stage") or ""),
            "error_code": str(row.get("error_code") or ""),
            "error_message": str(row.get("error_message") or ""),
            "diagnostics_json": _json(row.get("diagnostics") or {}),
            "http_status": int(request["http_status"]), "created_at": request["created_at"],
        }
        record = dict(immutable, payload_hash=_hash(immutable))
        record.update(revision=int(request["revision"]), alert_status=request["alert_status"],
                      alert_reasons_json=_json(request["alert_reasons"]),
                      feishu_message_id=request["feishu_message_id"],
                      alert_error_code=request["alert_error_code"], updated_at=request["updated_at"])
        record["created_at"] = _mysql_time(record["created_at"])
        record["updated_at"] = _mysql_time(record["updated_at"])
        return record

    def write_task(self, request):
        records = [self._record(request, row) for row in request["rows"]]
        with _WRITER_LOCK:
            connection = self._connect()
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    for record in records:
                        identity = (record["task_id"], record["item_index"])
                        cursor.execute("SELECT payload_hash,revision FROM " + TABLE +
                                       " WHERE task_id=%s AND item_index=%s FOR UPDATE", identity)
                        existing = cursor.fetchone()
                        if existing is not None:
                            previous_hash, previous_revision = ((existing["payload_hash"], existing["revision"])
                                                                if isinstance(existing, dict) else existing)
                            if previous_hash != record["payload_hash"]:
                                raise PayloadConflictError("assignment log key conflicts with frozen facts")
                            if int(previous_revision) < record["revision"]:
                                cursor.execute("UPDATE " + TABLE + """ SET revision=%s,
                                    alert_status=%s,alert_reasons_json=%s,feishu_message_id=%s,
                                    alert_error_code=%s,updated_at=%s
                                    WHERE task_id=%s AND item_index=%s AND revision<%s""",
                                    (record["revision"], record["alert_status"], record["alert_reasons_json"],
                                     record["feishu_message_id"], record["alert_error_code"], record["updated_at"],
                                     *identity, record["revision"]))
                            continue
                        columns = tuple(record)
                        cursor.execute("INSERT INTO " + TABLE + " (" + ",".join("`" + key + "`" for key in columns) +
                                       ") VALUES (" + ",".join("%s" for _ in columns) + ")",
                                       tuple(record[key] for key in columns))
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            finally:
                connection.close()

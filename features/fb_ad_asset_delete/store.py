"""Durable, fail-closed ledger for the version 2 Meta asset deletion flow.

The caller owns authorization and Graph I/O. A successful ``claim_object`` must
commit before any Graph write; failures from this module must stop new writes.
Unknown outcomes retain a global fence until an explicit read-only reconciliation
provides deletion proof. Opening a Store never starts or resumes a worker.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


PHASES = ("creative", "ad", "video")
OBJECT_STATUSES = frozenset(("pending", "blocked", "deleted", "already_deleted",
                             "failed", "unknown", "in_progress"))
JOB_STATUSES = frozenset(("previewing", "ready", "failed", "running", "completed",
                          "partial", "interrupted"))
SUCCESS_STATUSES = frozenset(("deleted", "already_deleted"))
_SECRET_KEYS = frozenset(("access_token", "refresh_token", "client_secret",
                          "app_secret", "authorization", "cookie", "password", "token", "page_access_token"))
_CREDENTIAL_FIELDS = frozenset(("delete_mode", "credential_kind", "credential_page_id", "credential_row_id",
    "credential_fb_user_id", "credential_user_id", "credential_relation", "credential_lookup", "credential_lookup_message"))
_VIDEO_ACCOUNT_MODE = "ad_account_video"
_VIDEO_CREDENTIAL_FIELDS = _CREDENTIAL_FIELDS | frozenset(("delete_account_id", "delete_endpoint",
    "account_id", "video_id", "source_row_id", "source_row_ids", "source_user_id", "source_user_ids",
    "ad_id", "ad_ids", "credential_source_row_id", "credential_source_row_ids", "credential_ad_id",
    "credential_ad_ids", "credential_product_id", "credential_source_user_id",
    "credential_publish_queue_id", "credential_default_token"))
_TABLE = "fb_asset_delete_v2_"
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_rechecks (
        operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_jobs(job_id),
        preview_id TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
        owner_pid INTEGER NOT NULL, owner_start TEXT NOT NULL, metadata TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_jobs (
        job_id TEXT PRIMARY KEY, preview_id TEXT NOT NULL, actor TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        metadata TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_objects (
        job_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_jobs(job_id),
        object_key TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL,
        status TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
        result TEXT NOT NULL, run_id TEXT, attempt_id TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, object_key))""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_runs (
        run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_jobs(job_id),
        preview_id TEXT NOT NULL, actor TEXT NOT NULL, phases TEXT NOT NULL,
        status TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_start TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, summary TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_jobs(job_id),
        object_key TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_runs(run_id),
        status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        result TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_object_locks (
        object_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_receipts (
        object_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT,
        attempt_id TEXT, status TEXT NOT NULL, result TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
        action TEXT NOT NULL, created_at TEXT NOT NULL, details TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_video_accounts (
        job_id TEXT NOT NULL, object_key TEXT NOT NULL, account_id TEXT NOT NULL,
        status TEXT NOT NULL, ordinal INTEGER NOT NULL, run_id TEXT,
        account_attempt_id TEXT, result TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, object_key, account_id),
        FOREIGN KEY (job_id, object_key) REFERENCES fb_asset_delete_v2_objects(job_id, object_key))""",
    """CREATE TABLE IF NOT EXISTS fb_asset_delete_v2_video_account_attempts (
        account_attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
        object_key TEXT NOT NULL, account_id TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES fb_asset_delete_v2_runs(run_id),
        context TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, result TEXT NOT NULL,
        FOREIGN KEY (job_id, object_key, account_id)
            REFERENCES fb_asset_delete_v2_video_accounts(job_id, object_key, account_id))""",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_jobs_actor ON fb_asset_delete_v2_jobs(actor, created_at)",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_runs_job ON fb_asset_delete_v2_runs(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_objects_key ON fb_asset_delete_v2_objects(object_key, status)",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_attempts_job ON fb_asset_delete_v2_attempts(job_id, object_key)",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_video_accounts_pair ON fb_asset_delete_v2_video_accounts(object_key, account_id, status)",
    "CREATE INDEX IF NOT EXISTS fb_asset_delete_v2_video_account_attempts_job ON fb_asset_delete_v2_video_account_attempts(job_id, object_key, account_id)",
)


class StoreError(RuntimeError):
    def __init__(self, message, code="invalid_state"):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(value, name):
    if isinstance(value, bool) or value is None or not str(value).strip():
        raise StoreError("%s is required" % name, "invalid_input")
    return str(value).strip()


def _json(value):
    def validate(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in _SECRET_KEYS:
                    raise StoreError("Secret fields cannot be written to the ledger", "unsafe_metadata")
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
    try:
        validate(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError("Ledger metadata must be finite JSON data", "invalid_input") from exc


def _process_start(pid):
    """Return (process exists, start identity); unknown inspection fails closed."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return (False, None) if ctypes.get_last_error() == 87 else (None, None)
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return None, None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return True, "windows:%s" % created
        finally:
            kernel.CloseHandle(handle)
    try:
        # Field 22 is the start time in clock ticks; the command may contain ')'.
        stat = Path("/proc/%s/stat" % pid).read_text(encoding="utf-8")
    except FileNotFoundError:
        if Path("/proc").is_dir():
            return False, None
        return None, None
    except OSError:
        return None, None
    try:
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X", "x"):
            return False, None
        started = fields[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return True, "linux:%s:%s" % (boot, started)
    except (OSError, IndexError, ValueError):
        return None, None


def _owner_alive(pid, identity):
    exists, actual = _process_start(pid)
    if exists is False:
        return False
    if actual and identity:
        return actual == identity
    return True


class Store:
    def __init__(self, path):
        if str(path) == ":memory:":
            raise StoreError("Deletion tasks require a durable database path", "invalid_input")
        self.path = str(Path(path).resolve())
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
        except OSError as exc:
            raise StoreError("Cannot open deletion ledger: %s" % exc, "ledger_error") from exc

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def _transaction(self, write=True):
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise StoreError("Deletion ledger transaction failed: %s" % exc, "ledger_error") from exc
        except BaseException:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _audit(conn, job_id, action, details):
        conn.execute("INSERT INTO fb_asset_delete_v2_audit(job_id,action,created_at,details) VALUES (?,?,?,?)",
                     (job_id, action, _now(), _json(details)))

    @staticmethod
    def _require(conn, table, column, value):
        row = conn.execute("SELECT * FROM " + _TABLE + table + " WHERE " + column + "=?", (value,)).fetchone()
        if row is None:
            raise StoreError("%s was not found" % table.rstrip("s"), "not_found")
        return row

    @staticmethod
    def _object(conn, job_id, key):
        row = conn.execute("SELECT * FROM fb_asset_delete_v2_objects WHERE job_id=? AND object_key=?",
                           (job_id, key)).fetchone()
        if row is None:
            raise StoreError("Object is not in the frozen preview", "not_found")
        return row

    @staticmethod
    def _object_data(row):
        data = json.loads(row["payload"])
        data.update(key=row["object_key"], kind=row["kind"], object_id=row["object_id"],
                    status=row["status"], result=json.loads(row["result"]),
                    updated_at=row["updated_at"], last_run_id=row["run_id"], attempt_id=row["attempt_id"])
        return data

    @staticmethod
    def _run_data(row, duplicate=False):
        data = dict(row)
        data["phases"] = json.loads(data["phases"])
        data["summary"] = json.loads(data["summary"])
        data["duplicate"] = duplicate
        return data

    def _job_data(self, conn, row, include_objects=True, include_runs=True):
        data = json.loads(row["metadata"])
        for name in ("job_id", "preview_id", "actor", "status", "created_at", "updated_at"):
            data[name] = row[name]
        data["schema_version"] = 2
        counts = conn.execute("SELECT kind,status,COUNT(*) AS n FROM fb_asset_delete_v2_objects WHERE job_id=? GROUP BY kind,status",
                              (row["job_id"],)).fetchall()
        data["object_counts"] = {kind: {} for kind in PHASES}
        for count in counts:
            data["object_counts"][count["kind"]][count["status"]] = count["n"]
        if include_objects:
            data["objects"] = [self._object_data(item) for item in conn.execute(
                "SELECT * FROM fb_asset_delete_v2_objects WHERE job_id=? ORDER BY ordinal", (row["job_id"],))]
            accounts = {}
            for item in conn.execute("SELECT * FROM fb_asset_delete_v2_video_accounts WHERE job_id=? ORDER BY object_key,ordinal", (row["job_id"],)):
                accounts.setdefault(item["object_key"], []).append(self._video_account_data(item))
            for obj in data["objects"]:
                if obj["kind"] == "video":
                    obj["video_account_results"] = accounts.get(obj["key"], [])
        if include_runs:
            data["runs"] = [self._run_data(item) for item in conn.execute(
                "SELECT * FROM fb_asset_delete_v2_runs WHERE job_id=? ORDER BY created_at", (row["job_id"],))]
        return data

    @staticmethod
    def _put_objects(conn, job_id, objects):
        if not isinstance(objects, list):
            raise StoreError("objects must be a list", "invalid_input")
        seen = set()
        for ordinal, raw in enumerate(objects):
            if not isinstance(raw, dict):
                raise StoreError("Each object must be a dictionary", "invalid_input")
            item = dict(raw)
            kind = item.get("kind")
            object_id = _id(item.get("object_id"), "object_id")
            key = "%s:%s" % (kind, object_id)
            status = item.get("status", "pending")
            if kind not in PHASES or item.get("key", key) != key or key in seen or status not in OBJECT_STATUSES:
                raise StoreError("Invalid or duplicated preview object", "invalid_input")
            seen.add(key)
            result = item.pop("result", {})
            for field in ("key", "kind", "object_id", "status", "updated_at", "last_run_id", "attempt_id", "run_id"):
                item.pop(field, None)
            conn.execute("""INSERT INTO fb_asset_delete_v2_objects
                (job_id,object_key,kind,object_id,status,ordinal,payload,result,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                         (job_id, key, kind, object_id, status, ordinal, _json(item), _json(result), _now()))

    def create_job(self, job):
        data = dict(job)
        job_id = _id(data.pop("job_id", None), "job_id")
        preview_id = _id(data.pop("preview_id", None), "preview_id")
        actor = _id(data.pop("actor", None), "actor")
        status = data.pop("status", "ready")
        if status not in JOB_STATUSES or status == "running":
            raise StoreError("Invalid initial job status", "invalid_input")
        objects = data.pop("objects", [])
        for name in ("created_at", "updated_at", "object_counts", "runs", "schema_version"):
            data.pop(name, None)
        for name in ("ids", "products", "dramas", "blockers"):
            data.setdefault(name, [])
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM fb_asset_delete_v2_jobs WHERE job_id=?", (job_id,)).fetchone():
                raise StoreError("Job already exists", "conflict")
            conn.execute("INSERT INTO fb_asset_delete_v2_jobs VALUES (?,?,?,?,?,?,?)",
                         (job_id, preview_id, actor, status, now, now, _json(data)))
            self._put_objects(conn, job_id, objects)
            self._audit(conn, job_id, "job_created", {"actor": actor, "preview_id": preview_id})
            return self._job_data(conn, self._require(conn, "jobs", "job_id", job_id))

    def update_job(self, job_id, **fields):
        with self._transaction() as conn:
            row = self._require(conn, "jobs", "job_id", job_id)
            started = conn.execute("SELECT 1 FROM fb_asset_delete_v2_runs WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
            if any(name in fields for name in ("job_id", "actor", "created_at", "runs", "object_counts")):
                raise StoreError("Job identity and execution records are immutable")
            if started and any(name in fields for name in ("objects", "preview_id", "input_type", "ids", "products", "dramas", "status")):
                raise StoreError("The executed preview and its results are immutable", "preview_frozen")
            requested_status = "status" in fields
            status = fields.pop("status", row["status"])
            if status not in JOB_STATUSES or (requested_status and status == "running"):
                raise StoreError("Use claim_run to start a task", "invalid_state")
            preview_id = _id(fields.pop("preview_id", row["preview_id"]), "preview_id")
            if "objects" in fields:
                objects = fields.pop("objects")
                conn.execute("DELETE FROM fb_asset_delete_v2_objects WHERE job_id=?", (job_id,))
                self._put_objects(conn, job_id, objects)
            data = json.loads(row["metadata"])
            data.update(fields)
            conn.execute("UPDATE fb_asset_delete_v2_jobs SET preview_id=?,status=?,updated_at=?,metadata=? WHERE job_id=?",
                         (preview_id, status, _now(), _json(data), job_id))
            self._audit(conn, job_id, "job_updated", {"status": status})
            return self._job_data(conn, self._require(conn, "jobs", "job_id", job_id))

    def get_job(self, job_id, include_objects=True):
        with self._transaction(False) as conn:
            return self._job_data(conn, self._require(conn, "jobs", "job_id", job_id), include_objects)

    def list_jobs(self, actor=None, limit=30):
        limit = max(1, min(int(limit), 500))
        with self._transaction(False) as conn:
            if actor is None:
                rows = conn.execute("SELECT * FROM fb_asset_delete_v2_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM fb_asset_delete_v2_jobs WHERE actor=? ORDER BY created_at DESC LIMIT ?",
                                    (str(actor), limit)).fetchall()
            return [self._job_data(conn, row, False, False) for row in rows]

    def claim_run(self, job_id, preview_id, actor, phases, request_id):
        job_id, preview_id, actor, request_id = (_id(value, name) for value, name in
            ((job_id, "job_id"), (preview_id, "preview_id"), (actor, "actor"), (request_id, "request_id")))
        if not isinstance(phases, (list, tuple)) or not phases or any(phase not in PHASES for phase in phases):
            raise StoreError("Choose one or more valid deletion phases", "invalid_input")
        phases = [phase for phase in PHASES if phase in phases]
        encoded_phases = _json(phases)
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM fb_asset_delete_v2_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if (existing["job_id"], existing["preview_id"], existing["actor"], existing["phases"]) != (job_id, preview_id, actor, encoded_phases):
                    raise StoreError("request_id was used with different execution parameters", "request_conflict")
                return self._run_data(existing, True)
            job = self._require(conn, "jobs", "job_id", job_id)
            if job["preview_id"] != preview_id:
                raise StoreError("Preview changed; refresh the task before execution", "preview_mismatch")
            if job["status"] not in ("ready", "completed", "partial", "interrupted"):
                raise StoreError("Task cannot start from its current state", "job_not_ready")
            if json.loads(job["metadata"]).get("recheck", {}).get("status") == "running":
                raise StoreError("Read-only recheck is still running", "job_not_ready")
            exists, identity = _process_start(os.getpid())
            if exists is not True or not identity:
                raise StoreError("Cannot establish worker process identity", "owner_identity_unavailable")
            run_id, now = uuid.uuid4().hex, _now()
            conn.execute("INSERT INTO fb_asset_delete_v2_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, request_id, job_id, preview_id, actor, encoded_phases, "running", os.getpid(), identity, now, now, "{}"))
            conn.execute("UPDATE fb_asset_delete_v2_jobs SET status='running',updated_at=? WHERE job_id=?", (now, job_id))
            self._audit(conn, job_id, "run_claimed", {"run_id": run_id, "actor": actor, "phases": phases, "request_id": request_id})
            return self._run_data(self._require(conn, "runs", "run_id", run_id))

    def claim_recheck(self, job_id, preview_id, actor, request_id, total):
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM fb_asset_delete_v2_rechecks WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (existing["job_id"], existing["preview_id"], existing["actor"]) != (job_id, preview_id, actor):
                    raise StoreError("Recheck request conflicts", "request_conflict")
                return dict(job_id=job_id, operation_id=existing["operation_id"], status=existing["status"], duplicate=True, read_only=True)
            job = self._require(conn, "jobs", "job_id", job_id)
            if job["preview_id"] != preview_id:
                raise StoreError("Preview mismatch", "preview_mismatch")
            data = json.loads(job["metadata"])
            if job["status"] not in ("ready", "completed", "partial", "interrupted") or data.get("recheck", {}).get("status") == "running":
                raise StoreError("Task is busy", "job_not_ready")
            exists, identity = _process_start(os.getpid())
            if exists is not True or not identity:
                raise StoreError("Cannot establish process identity", "owner_identity_unavailable")
            op = uuid.uuid4().hex
            check = dict(operation_id=op, status="running", checked=0, total=total,
                         step="正在重新核验固定清单；此操作不会执行删除", updated_at=_now())
            conn.execute("INSERT INTO fb_asset_delete_v2_rechecks VALUES (?,?,?,?,?,?,?,?,?)",
                         (op, request_id, job_id, preview_id, actor, "running", os.getpid(), identity, _json(check)))
            data["recheck"] = check
            conn.execute("UPDATE fb_asset_delete_v2_jobs SET metadata=?,updated_at=? WHERE job_id=?", (_json(data), _now(), job_id))
            self._audit(conn, job_id, "recheck_claimed", dict(operation_id=op, request_id=request_id, actor=actor, total=total))
            return dict(job_id=job_id, operation_id=op, status="running", duplicate=False, read_only=True)

    def update_recheck(self, operation_id, **fields):
        with self._transaction() as conn:
            row = self._require(conn, "rechecks", "operation_id", operation_id)
            if row["status"] != "running":
                raise StoreError("Recheck is no longer running")
            check = json.loads(row["metadata"])
            check.update(fields, updated_at=_now())
            if check["status"] not in ("running", "completed", "interrupted"):
                raise StoreError("Invalid recheck status")
            job = self._require(conn, "jobs", "job_id", row["job_id"])
            data = json.loads(job["metadata"])
            if data.get("recheck", {}).get("operation_id") != operation_id:
                raise StoreError("Recheck has been superseded")
            data["recheck"] = check
            conn.execute("UPDATE fb_asset_delete_v2_rechecks SET status=?,metadata=? WHERE operation_id=?",
                         (check["status"], _json(check), operation_id))
            conn.execute("UPDATE fb_asset_delete_v2_jobs SET metadata=?,updated_at=? WHERE job_id=?", (_json(data), _now(), row["job_id"]))
            if check["status"] != "running":
                self._audit(conn, row["job_id"], "recheck_finished", check)

    def recheck_object(self, operation_id, key, status, result):
        if status not in ("pending", "blocked", "already_deleted"):
            raise StoreError("Read-only recheck cannot delete or retry an object")
        if status == "already_deleted" and not result.get("confirmed_deleted"):
            raise StoreError("Deletion proof required")
        with self._transaction() as conn:
            check = self._require(conn, "rechecks", "operation_id", operation_id)
            job = self._require(conn, "jobs", "job_id", check["job_id"])
            if check["status"] != "running" or job["status"] == "running" or job["preview_id"] != check["preview_id"]:
                raise StoreError("Recheck lost its claim")
            obj = self._object(conn, check["job_id"], key)
            if obj["status"] != "blocked":
                raise StoreError("Only blocked objects can be rechecked")
            payload = json.loads(obj["payload"])
            payload["reason"] = result.get("message", "") if status == "blocked" else ""
            # Keep frozen IDs, attempts, original DELETE results in attempts, and global fences.
            conn.execute("UPDATE fb_asset_delete_v2_objects SET status=?,result=?,payload=?,updated_at=? WHERE job_id=? AND object_key=?",
                         (status, _json(result), _json(payload), _now(), check["job_id"], key))
            self._audit(conn, check["job_id"], "object_rechecked", dict(operation_id=operation_id, key=key,
                before=json.loads(obj["result"]), status=status, result=result))

    def recover_rechecks(self):
        with self._transaction(False) as conn:
            rows = conn.execute("SELECT * FROM fb_asset_delete_v2_rechecks WHERE status='running'").fetchall()
        for row in rows:
            if not _owner_alive(row["owner_pid"], row["owner_start"]):
                self.update_recheck(row["operation_id"], status="interrupted",
                    step="核验进程已中断，进度保留，请人工重新核验；未自动恢复删除")

    def claim_object(self, job_id, key, run_id):
        with self._transaction() as conn:
            run = self._require(conn, "runs", "run_id", run_id)
            row = self._object(conn, job_id, key)
            if run["job_id"] != job_id or run["status"] != "running" or row["kind"] not in json.loads(run["phases"]):
                raise StoreError("Object does not belong to this active run")
            direct_video = row["kind"] == "video" and row["status"] == "blocked"
            if row["status"] not in ("pending", "failed") and not direct_video:
                return dict(self._object_data(row), claimed=False)
            receipt = conn.execute("SELECT * FROM fb_asset_delete_v2_receipts WHERE object_key=?", (key,)).fetchone()
            if receipt is not None:
                proof = {"reason": "previous_task_deleted", "source_job_id": receipt["job_id"],
                         "source_attempt_id": receipt["attempt_id"], "proof": json.loads(receipt["result"])}
                conn.execute("UPDATE fb_asset_delete_v2_objects SET status='already_deleted',result=?,updated_at=? WHERE job_id=? AND object_key=?",
                             (_json(proof), _now(), job_id, key))
                self._audit(conn, job_id, "object_skipped_with_proof", {"key": key, "run_id": run_id, **proof})
                return dict(self._object_data(self._object(conn, job_id, key)), claimed=False)
            lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
            if lock is not None:
                result = {"reason": "unknown_fence" if lock["status"] == "unknown" else "object_locked",
                          "source_job_id": lock["job_id"], "source_run_id": lock["run_id"]}
                status = "unknown" if lock["status"] == "unknown" else row["status"]
                owner = self._object(conn, lock["job_id"], key)
                if status == "unknown" and self._is_account_video(conn, owner):
                    row = self._ensure_video_accounts(conn, row)
                    result = dict(json.loads(row["result"]), **result)
                conn.execute("UPDATE fb_asset_delete_v2_objects SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=?",
                             (status, _json(result), _now(), job_id, key))
                self._audit(conn, job_id, "object_fenced", {"key": key, "run_id": run_id, **result})
                return dict(self._object_data(self._object(conn, job_id, key)), claimed=False, reason=result["reason"])
            attempt_id, now = uuid.uuid4().hex, _now()
            conn.execute("INSERT INTO fb_asset_delete_v2_attempts VALUES (?,?,?,?,?,?,?,?)",
                         (attempt_id, job_id, key, run_id, "in_progress", now, None, "{}"))
            conn.execute("INSERT INTO fb_asset_delete_v2_object_locks VALUES (?,?,?,?,?,?)",
                         (key, job_id, run_id, attempt_id, "in_progress", now))
            payload = json.loads(row["payload"])
            previous = {"status": row["status"], "reason": payload.get("reason", ""), "result": json.loads(row["result"])} if direct_video else None
            payload["reason"] = ""
            conn.execute("UPDATE fb_asset_delete_v2_objects SET status='in_progress',result='{}',payload=?,run_id=?,attempt_id=?,updated_at=? WHERE job_id=? AND object_key=?",
                         (_json(payload), run_id, attempt_id, now, job_id, key))
            self._audit(conn, job_id, "object_claimed", {"key": key, "run_id": run_id, "attempt_id": attempt_id,
                **({"video_direct": True, "previous": previous} if direct_video else {})})
            return dict(self._object_data(self._object(conn, job_id, key)), claimed=True)

    def record_object_credential(self, job_id, key, run_id, context):
        """Persist the selected identity before DELETE, never its secret Token."""
        if not isinstance(context, dict) or set(context) - _CREDENTIAL_FIELDS:
            raise StoreError("Unexpected credential audit fields", "unsafe_metadata")
        encoded = _json(context)
        with self._transaction() as conn:
            row = self._object(conn, job_id, key)
            run = self._require(conn, "runs", "run_id", run_id)
            lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
            if row["run_id"] != run_id or row["status"] != "in_progress" or run["status"] != "running" or lock is None or lock["attempt_id"] != row["attempt_id"] or lock["status"] != "in_progress":
                raise StoreError("Only the active claim may select credentials", "claim_conflict")
            attempt = self._require(conn, "attempts", "attempt_id", row["attempt_id"])
            if attempt["result"] != "{}":
                if attempt["result"] == encoded:
                    return
                raise StoreError("A claimed attempt cannot change its selected identity", "credential_conflict")
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET result=? WHERE attempt_id=?", (encoded, row["attempt_id"]))
            conn.execute("UPDATE fb_asset_delete_v2_objects SET result=?,updated_at=? WHERE job_id=? AND object_key=?", (encoded, _now(), job_id, key))
            self._audit(conn, job_id, "object_credential_selected", {"key": key, "run_id": run_id,
                "attempt_id": row["attempt_id"], "credential": context})

    @staticmethod
    def _video_account_ids(row):
        values = json.loads(row["payload"]).get("account_ids")
        if row["kind"] != "video" or not isinstance(values, list) or not values:
            raise StoreError("Frozen video account IDs are required", "invalid_input")
        accounts = [_id(value, "account_id") for value in values]
        if any(not value.isascii() or not value.isdecimal() for value in accounts):
            raise StoreError("Frozen video account IDs must be numeric IDs without act_", "invalid_input")
        return list(dict.fromkeys(accounts))

    @staticmethod
    def _video_account_data(row):
        return dict(account_id=row["account_id"], status=row["status"], result=json.loads(row["result"]),
                    account_attempt_id=row["account_attempt_id"], run_id=row["run_id"], updated_at=row["updated_at"])

    def _video_account_results(self, conn, job_id, key):
        return [self._video_account_data(row) for row in conn.execute(
            "SELECT * FROM fb_asset_delete_v2_video_accounts WHERE job_id=? AND object_key=? ORDER BY ordinal", (job_id, key))]

    def video_account_results(self, job_id, key):
        with self._transaction(False) as conn:
            row = self._object(conn, job_id, key)
            if row["kind"] != "video":
                raise StoreError("Account deletion progress belongs to a video", "invalid_input")
            return self._video_account_results(conn, job_id, key)

    def _is_account_video(self, conn, row):
        return row["kind"] == "video" and (json.loads(row["result"]).get("delete_mode") == _VIDEO_ACCOUNT_MODE or
            conn.execute("SELECT 1 FROM fb_asset_delete_v2_video_accounts WHERE job_id=? AND object_key=? LIMIT 1",
                         (row["job_id"], row["object_key"])).fetchone() is not None)

    def _active_video_parent(self, conn, job_id, key, run_id):
        row = self._object(conn, job_id, key)
        run = self._require(conn, "runs", "run_id", run_id)
        lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
        if (row["kind"] != "video" or row["status"] != "in_progress" or row["run_id"] != run_id or
                run["job_id"] != job_id or run["status"] != "running" or "video" not in json.loads(run["phases"]) or
                lock is None or (lock["job_id"], lock["run_id"], lock["attempt_id"], lock["status"]) !=
                (job_id, run_id, row["attempt_id"], "in_progress")):
            raise StoreError("Only the active parent video claim may process its accounts", "claim_conflict")
        return row

    def _refresh_video_result(self, conn, row, extra=None):
        result = json.loads(row["result"])
        result.update(extra or {})
        result.update(delete_mode=_VIDEO_ACCOUNT_MODE, delete_scope=_VIDEO_ACCOUNT_MODE,
                      video_id=row["object_id"], account_results=self._video_account_results(conn, row["job_id"], row["object_key"]))
        conn.execute("UPDATE fb_asset_delete_v2_objects SET result=?,updated_at=? WHERE job_id=? AND object_key=?",
                     (_json(result), _now(), row["job_id"], row["object_key"]))
        return self._object(conn, row["job_id"], row["object_key"])

    def _ensure_video_accounts(self, conn, row):
        accounts = self._video_account_ids(row)
        existing = self._video_account_results(conn, row["job_id"], row["object_key"])
        if existing and [item["account_id"] for item in existing] != accounts:
            raise StoreError("Frozen video account progress does not match its preview", "preview_frozen")
        if not existing:
            now = _now()
            for ordinal, account in enumerate(accounts):
                conn.execute("INSERT INTO fb_asset_delete_v2_video_accounts VALUES (?,?,?,?,?,?,?,?,?)",
                             (row["job_id"], row["object_key"], account, "pending", ordinal, None, None, "{}", now))
            self._audit(conn, row["job_id"], "video_accounts_initialized", dict(key=row["object_key"], account_ids=accounts))
        return self._refresh_video_result(conn, row)

    def begin_video_accounts(self, job_id, key, run_id):
        """Commit the complete account scope before even looking up credentials."""
        with self._transaction() as conn:
            row = self._active_video_parent(conn, job_id, key, run_id)
            row = self._ensure_video_accounts(conn, row)
            return dict(self._object_data(row), video_account_results=self._video_account_results(conn, job_id, key))

    def _video_account(self, conn, row, account_id):
        account = _id(account_id, "account_id")
        if account not in self._video_account_ids(row):
            raise StoreError("Account is not in the frozen video preview", "preview_frozen")
        child = conn.execute("SELECT * FROM fb_asset_delete_v2_video_accounts WHERE job_id=? AND object_key=? AND account_id=?",
                             (row["job_id"], row["object_key"], account)).fetchone()
        if child is None:
            raise StoreError("Initialize frozen video accounts before processing them", "invalid_state")
        return child

    @staticmethod
    def _video_context(context, account_id, video_id):
        if not isinstance(context, dict) or set(context) - _VIDEO_CREDENTIAL_FIELDS:
            raise StoreError("Unexpected video credential audit fields", "unsafe_metadata")
        if (context.get("delete_mode") != _VIDEO_ACCOUNT_MODE or
                context.get("delete_account_id", account_id) != account_id or
                context.get("account_id", account_id) != account_id or
                context.get("video_id", video_id) != video_id or
                context.get("delete_endpoint", "act_" + account_id + "/advideos") != "act_" + account_id + "/advideos" or
                context.get("credential_kind", "user") != "user"):
            raise StoreError("Video credentials do not match this account operation", "credential_conflict")
        return _json(context)

    @staticmethod
    def _account_outcome(result, account_id, video_id, require_absent=False):
        if (not isinstance(result, dict) or result.get("delete_mode") != _VIDEO_ACCOUNT_MODE or
                result.get("delete_scope") != _VIDEO_ACCOUNT_MODE or result.get("account_id") != account_id or
                result.get("video_id") != video_id):
            raise StoreError("Video outcome must identify its exact account and video", "invalid_input")
        if require_absent:
            proof = result.get("proof")
            if (result.get("confirmed_absent") is not True or not isinstance(proof, dict) or
                    proof.get("account_id") != account_id or proof.get("video_id") != video_id or proof.get("complete") is not True):
                raise StoreError("Complete account-specific absence proof is required", "proof_required")
        return _json(result)

    def claim_video_account(self, job_id, key, run_id, account_id, context):
        """A returned claim is the durable authorization for one account DELETE."""
        with self._transaction() as conn:
            row = self._active_video_parent(conn, job_id, key, run_id)
            child = self._video_account(conn, row, account_id)
            account = child["account_id"]
            encoded = self._video_context(context, account, row["object_id"])
            if child["status"] == "in_progress":
                attempt = self._require(conn, "video_account_attempts", "account_attempt_id", child["account_attempt_id"])
                if attempt["context"] != encoded:
                    raise StoreError("An account attempt cannot change its selected identity", "credential_conflict")
            if child["status"] not in ("pending", "failed"):
                return dict(self._video_account_data(child), claimed=False)
            pair_key = "video_account:%s:%s" % (account, row["object_id"])
            receipt = conn.execute("SELECT * FROM fb_asset_delete_v2_receipts WHERE object_key=?", (pair_key,)).fetchone()
            if receipt is not None:
                result = dict(delete_mode=_VIDEO_ACCOUNT_MODE, delete_scope=_VIDEO_ACCOUNT_MODE, account_id=account,
                    video_id=row["object_id"], success=True, reason="previous_account_deleted", source_job_id=receipt["job_id"],
                    source_account_attempt_id=receipt["attempt_id"], proof=json.loads(receipt["result"]))
                conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET status='already_deleted',result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                             (_json(result), _now(), job_id, key, account))
                self._refresh_video_result(conn, row)
                self._audit(conn, job_id, "video_account_skipped_with_proof", dict(key=key, run_id=run_id, **result))
                return dict(self._video_account_data(self._video_account(conn, row, account)), claimed=False)
            attempt_id, now = uuid.uuid4().hex, _now()
            result = dict(context, account_id=account, video_id=row["object_id"], delete_scope=_VIDEO_ACCOUNT_MODE)
            conn.execute("INSERT INTO fb_asset_delete_v2_video_account_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (attempt_id, job_id, key, account, run_id, encoded, "in_progress", now, None, _json(result)))
            conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET status='in_progress',run_id=?,account_attempt_id=?,result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                         (run_id, attempt_id, _json(result), now, job_id, key, account))
            self._refresh_video_result(conn, row)
            self._audit(conn, job_id, "video_account_claimed", dict(key=key, account_id=account, run_id=run_id,
                account_attempt_id=attempt_id, credential=context))
            return dict(self._video_account_data(self._video_account(conn, row, account)), claimed=True)

    @staticmethod
    def _deferred_video_rejection(result):
        """Only a definite invalid-ID response may wait for a shared read."""
        if not isinstance(result, dict) or result.get("needs_account_verification") is not True:
            return False
        detail = result.get("detail")
        status = detail.get("http_status") if isinstance(detail, dict) else None
        error = result.get("delete_error")
        return (result.get("code") == "100" and isinstance(result.get("message"), str) and
            re.fullmatch(r"(?:\(#100\)\s*)?Param video_id is not a valid video ID\.?",
                         result["message"].strip(), re.IGNORECASE) is not None and
            isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 500 and
            status not in (301, 302, 307, 308) and result.get("success") is not True and
            result.get("confirmed_absent") is not True and isinstance(result.get("checked_at"), str) and
            bool(result["checked_at"]) and isinstance(error, dict) and
            error == {field: result.get(field) for field in ("code", "message", "detail", "checked_at")})

    def defer_video_account_verification(self, job_id, key, run_id, account_id, account_attempt_id, result):
        """Persist a rejected DELETE before batching its optional account read.

        Keep the claim active, so another task cannot retry while verification
        runs. Recovery can distinguish this definite rejection from an in-flight
        request whose outcome is unknown.
        """
        if not self._deferred_video_rejection(result):
            raise StoreError("Only an explicit invalid Video ID rejection can defer verification", "invalid_input")
        with self._transaction() as conn:
            row = self._active_video_parent(conn, job_id, key, run_id)
            child = self._video_account(conn, row, account_id)
            account = child["account_id"]
            self._account_outcome(result, account, row["object_id"])
            attempt = self._require(conn, "video_account_attempts", "account_attempt_id", account_attempt_id)
            if ((attempt["job_id"], attempt["object_key"], attempt["account_id"], attempt["run_id"]) !=
                    (job_id, key, account, run_id) or
                    (child["run_id"], child["account_attempt_id"], child["status"], attempt["status"]) !=
                    (run_id, account_attempt_id, "in_progress", "in_progress")):
                raise StoreError("Only the active account attempt may defer verification", "claim_conflict")
            context = json.loads(attempt["context"])
            if (any(result.get(field) != value for field, value in context.items()) or
                    any(field in result and result[field] != context.get(field)
                        for field in _VIDEO_CREDENTIAL_FIELDS - {"account_id", "video_id"})):
                raise StoreError("Deferred rejection cannot replace its selected identity", "credential_conflict")
            encoded = _json(result)
            previous = json.loads(attempt["result"])
            if previous.get("needs_account_verification") is True:
                if attempt["result"] != encoded:
                    raise StoreError("A deferred DELETE rejection is immutable", "claim_conflict")
                return self._video_account_data(child)
            now = _now()
            conn.execute("UPDATE fb_asset_delete_v2_video_account_attempts SET result=? WHERE account_attempt_id=?",
                         (encoded, account_attempt_id))
            conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                         (encoded, now, job_id, key, account))
            self._refresh_video_result(conn, row)
            self._audit(conn, job_id, "video_account_verification_deferred", dict(key=key, account_id=account,
                run_id=run_id, account_attempt_id=account_attempt_id, result=result))
            return self._video_account_data(self._video_account(conn, row, account))

    def finish_video_account(self, job_id, key, run_id, account_id, account_attempt_id, status, result):
        if status not in ("deleted", "already_deleted", "failed", "blocked", "unknown"):
            raise StoreError("Invalid account video outcome", "invalid_input")
        with self._transaction() as conn:
            row = self._object(conn, job_id, key)
            child = self._video_account(conn, row, account_id)
            account = child["account_id"]
            self._account_outcome(result, account, row["object_id"])
            if status in SUCCESS_STATUSES and result.get("success") is not True:
                self._account_outcome(result, account, row["object_id"], require_absent=True)
            attempt = self._require(conn, "video_account_attempts", "account_attempt_id", account_attempt_id)
            if (attempt["job_id"], attempt["object_key"], attempt["account_id"], attempt["run_id"]) != (job_id, key, account, run_id):
                raise StoreError("Account attempt does not match its frozen claim", "claim_conflict")
            previous = json.loads(attempt["result"])
            if self._deferred_video_rejection(previous):
                if status in SUCCESS_STATUSES:
                    self._account_outcome(result, account, row["object_id"], require_absent=True)
                if result.get("delete_error") != previous["delete_error"]:
                    raise StoreError("Verification cannot replace the original DELETE rejection", "claim_conflict")
                result = dict(result)
                result.pop("needs_account_verification", None)
            context = json.loads(attempt["context"])
            if any(field in result and result[field] != context.get(field) for field in _VIDEO_CREDENTIAL_FIELDS -
                   {"account_id", "video_id"} if field in result):
                raise StoreError("Account outcome cannot replace its selected identity", "credential_conflict")
            result = dict(context, **result)
            encoded = _json(result)
            if child["account_attempt_id"] == account_attempt_id and child["status"] == status:
                if attempt["result"] != encoded:
                    raise StoreError("A completed account attempt is immutable", "claim_conflict")
                return self._video_account_data(child)
            self._active_video_parent(conn, job_id, key, run_id)
            if (child["run_id"], child["account_attempt_id"], child["status"], attempt["status"]) != (run_id, account_attempt_id, "in_progress", "in_progress"):
                raise StoreError("Only the active account attempt may finish", "claim_conflict")
            now = _now()
            conn.execute("UPDATE fb_asset_delete_v2_video_account_attempts SET status=?,finished_at=?,result=? WHERE account_attempt_id=?",
                         (status, now, encoded, account_attempt_id))
            conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                         (status, encoded, now, job_id, key, account))
            if status in SUCCESS_STATUSES:
                self._receipt(conn, "video_account:%s:%s" % (account, row["object_id"]), job_id, run_id, account_attempt_id, status, encoded)
            self._refresh_video_result(conn, row)
            self._audit(conn, job_id, "video_account_finished", dict(key=key, account_id=account, run_id=run_id,
                account_attempt_id=account_attempt_id, status=status, result=result))
            return self._video_account_data(self._video_account(conn, row, account))

    @staticmethod
    def _account_parent_status(results):
        statuses = {item["status"] for item in results}
        if statuses & {"in_progress", "unknown"}:
            return "unknown"
        return "deleted" if results and statuses <= SUCCESS_STATUSES else "failed"

    def finish_object(self, job_id, key, run_id, status, result):
        if status not in ("deleted", "already_deleted", "failed", "blocked", "unknown"):
            raise StoreError("Invalid object outcome", "invalid_input")
        encoded = _json(result)
        with self._transaction() as conn:
            row = self._object(conn, job_id, key)
            run = self._require(conn, "runs", "run_id", run_id)
            account_mode = self._is_account_video(conn, row) or (isinstance(result, dict) and result.get("delete_mode") == _VIDEO_ACCOUNT_MODE)
            if account_mode:
                if row["kind"] != "video" or result.get("delete_mode") != _VIDEO_ACCOUNT_MODE:
                    raise StoreError("Account video results must retain their deletion scope", "invalid_input")
                accounts = self._video_account_results(conn, job_id, key)
                actual = self._account_parent_status(accounts)
                if status != actual and not (status in SUCCESS_STATUSES and actual in SUCCESS_STATUSES):
                    raise StoreError("Parent outcome cannot overwrite its account progress", "claim_conflict")
                if any(item["status"] == "in_progress" for item in accounts):
                    raise StoreError("Finish or recover each in-flight account before its parent", "claim_conflict")
                result = dict(result, account_results=accounts, video_id=row["object_id"],
                              delete_scope=_VIDEO_ACCOUNT_MODE, success=status in SUCCESS_STATUSES)
                encoded = _json(result)
            if row["run_id"] == run_id and row["status"] == status:
                return self._object_data(row)
            if row["run_id"] != run_id or row["status"] != "in_progress" or run["status"] != "running":
                raise StoreError("Only the active claim may persist its outcome", "claim_conflict")
            lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
            if lock is None or lock["attempt_id"] != row["attempt_id"] or lock["status"] != "in_progress":
                raise StoreError("Object claim fence does not match", "claim_conflict")
            now = _now()
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET status=?,finished_at=?,result=? WHERE attempt_id=?",
                         (status, now, encoded, row["attempt_id"]))
            conn.execute("UPDATE fb_asset_delete_v2_objects SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=?",
                         (status, encoded, now, job_id, key))
            if status in SUCCESS_STATUSES and not account_mode:
                self._receipt(conn, key, job_id, run_id, row["attempt_id"], status, encoded)
            if status == "unknown":
                conn.execute("UPDATE fb_asset_delete_v2_object_locks SET status='unknown',updated_at=? WHERE object_key=?", (now, key))
            else:
                conn.execute("DELETE FROM fb_asset_delete_v2_object_locks WHERE object_key=? AND attempt_id=?", (key, row["attempt_id"]))
            self._audit(conn, job_id, "object_finished", {"key": key, "run_id": run_id, "attempt_id": row["attempt_id"], "status": status, "result": result})
            return self._object_data(self._object(conn, job_id, key))

    @staticmethod
    def _receipt(conn, key, job_id, run_id, attempt_id, status, encoded):
        conn.execute("""INSERT INTO fb_asset_delete_v2_receipts VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(object_key) DO UPDATE SET job_id=excluded.job_id,
            run_id=excluded.run_id,attempt_id=excluded.attempt_id,status=excluded.status,
            result=excluded.result,updated_at=excluded.updated_at""",
                     (key, job_id, run_id, attempt_id, status, encoded, _now()))

    def reconcile_object(self, job_id, key, status, result):
        if status not in ("deleted", "already_deleted", "unknown"):
            raise StoreError("Reconciliation never resets an unknown object for retry", "invalid_input")
        if status in SUCCESS_STATUSES and not (isinstance(result, dict) and
                (result.get("proof") or result.get("confirmed_deleted") is True or result.get("success") is True)):
            raise StoreError("Explicit deletion proof is required to release the fence", "proof_required")
        encoded = _json(result)
        with self._transaction() as conn:
            row = self._object(conn, job_id, key)
            if self._is_account_video(conn, row) or (isinstance(result, dict) and
                    (result.get("delete_mode") == _VIDEO_ACCOUNT_MODE or result.get("delete_scope") == _VIDEO_ACCOUNT_MODE)):
                raise StoreError("Account video outcomes require account-specific reconciliation", "account_reconciliation_required")
            if row["status"] in SUCCESS_STATUSES:
                return self._object_data(row)
            if row["status"] != "unknown":
                raise StoreError("Only unknown objects need reconciliation")
            lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
            if lock is not None and lock["status"] == "in_progress":
                raise StoreError("Cannot reconcile an active Graph request", "claim_conflict")
            if status == "unknown":
                conn.execute("UPDATE fb_asset_delete_v2_objects SET result=?,updated_at=? WHERE job_id=? AND object_key=?",
                             (encoded, _now(), job_id, key))
                self._audit(conn, job_id, "reconciliation_unknown", {"key": key, "result": result})
            else:
                self._receipt(conn, key, job_id, row["run_id"], row["attempt_id"], status, encoded)
                affected = conn.execute("SELECT * FROM fb_asset_delete_v2_objects WHERE object_key=? AND status='unknown'", (key,)).fetchall()
                for item in affected:
                    conn.execute("UPDATE fb_asset_delete_v2_objects SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=?",
                                 (status, encoded, _now(), item["job_id"], key))
                    if item["attempt_id"]:
                        conn.execute("UPDATE fb_asset_delete_v2_attempts SET status=?,finished_at=?,result=? WHERE attempt_id=? AND status='unknown'",
                                     (status, _now(), encoded, item["attempt_id"]))
                    self._audit(conn, item["job_id"], "object_reconciled", {"key": key, "source_job_id": job_id, "status": status, "result": result})
                conn.execute("DELETE FROM fb_asset_delete_v2_object_locks WHERE object_key=? AND status='unknown'", (key,))
            return self._object_data(self._object(conn, job_id, key))

    def reconcile_video_account(self, job_id, key, account_id, status, result):
        """Only an exact, complete account absence proof can settle an unknown."""
        if status not in ("deleted", "already_deleted", "unknown"):
            raise StoreError("Reconciliation cannot retry an unknown account", "invalid_input")
        with self._transaction() as conn:
            row = self._object(conn, job_id, key)
            child = self._video_account(conn, row, account_id)
            account = child["account_id"]
            self._account_outcome(result, account, row["object_id"], require_absent=status in SUCCESS_STATUSES)
            lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (key,)).fetchone()
            if lock is not None and lock["status"] == "in_progress":
                raise StoreError("Cannot reconcile an active video DELETE", "claim_conflict")
            if child["status"] in SUCCESS_STATUSES:
                return dict(self._object_data(row), video_account_results=self._video_account_results(conn, job_id, key))
            if child["status"] != "unknown" or row["status"] != "unknown":
                raise StoreError("Only unknown account attempts require reconciliation", "invalid_state")
            if status in SUCCESS_STATUSES:
                self._receipt(conn, "video_account:%s:%s" % (account, row["object_id"]), job_id,
                              child["run_id"], child["account_attempt_id"], status, _json(result))
                children = conn.execute("SELECT * FROM fb_asset_delete_v2_video_accounts WHERE object_key=? AND account_id=? AND status='unknown'",
                                        (key, account)).fetchall()
            else:
                children = [child]
            for item in children:
                updated = dict(json.loads(item["result"]), **result)
                updated["requires_reconciliation"] = status == "unknown"
                encoded, now = _json(updated), _now()
                conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                             (status, encoded, now, item["job_id"], key, account))
                if item["account_attempt_id"]:
                    conn.execute("UPDATE fb_asset_delete_v2_video_account_attempts SET status=?,finished_at=?,result=? WHERE account_attempt_id=? AND status='unknown'",
                                 (status, now, encoded, item["account_attempt_id"]))
                self._audit(conn, item["job_id"], "video_account_reconciled", dict(key=key, account_id=account,
                    source_job_id=job_id, status=status, result=result, account_attempt_id=item["account_attempt_id"]))
            # Settle the lock holder first, then release tasks that only waited
            # for that parent fence. No proof is copied to a different account.
            parents = conn.execute("SELECT * FROM fb_asset_delete_v2_objects WHERE object_key=? AND status='unknown'", (key,)).fetchall()
            parents.sort(key=lambda item: item["job_id"] != (lock["job_id"] if lock else job_id))
            for parent in parents:
                if not self._is_account_video(conn, parent):
                    continue
                self._settle_video_parent(conn, parent, "account_read_only_reconciliation", extra={"checked_at": result.get("checked_at", _now())})
            return dict(self._object_data(self._object(conn, job_id, key)), video_account_results=self._video_account_results(conn, job_id, key))

    def _settle_video_parent(self, conn, row, reason, extra=None):
        results = self._video_account_results(conn, row["job_id"], row["object_key"])
        status = self._account_parent_status(results)
        lock = conn.execute("SELECT * FROM fb_asset_delete_v2_object_locks WHERE object_key=?", (row["object_key"],)).fetchone()
        owns_lock = lock is not None and (lock["job_id"], lock["attempt_id"]) == (row["job_id"], row["attempt_id"])
        if lock is not None and not owns_lock:
            status = "unknown"
        result = dict(json.loads(row["result"]), **(extra or {}))
        result.update(delete_mode=_VIDEO_ACCOUNT_MODE, delete_scope=_VIDEO_ACCOUNT_MODE, video_id=row["object_id"],
                      account_results=results, reason=reason, requires_reconciliation=status == "unknown", success=status in SUCCESS_STATUSES)
        encoded, now = _json(result), _now()
        conn.execute("UPDATE fb_asset_delete_v2_objects SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=?",
                     (status, encoded, now, row["job_id"], row["object_key"]))
        if row["attempt_id"]:
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET status=?,finished_at=?,result=? WHERE attempt_id=? AND status IN ('in_progress','unknown')",
                         (status, now, encoded, row["attempt_id"]))
        if owns_lock:
            if status == "unknown":
                conn.execute("UPDATE fb_asset_delete_v2_object_locks SET status='unknown',updated_at=? WHERE object_key=?", (now, row["object_key"]))
            else:
                conn.execute("DELETE FROM fb_asset_delete_v2_object_locks WHERE object_key=? AND attempt_id=?", (row["object_key"], row["attempt_id"]))
        self._audit(conn, row["job_id"], "video_accounts_aggregated", dict(key=row["object_key"], reason=reason, status=status, result=result))
        return status

    def _fence_video_accounts(self, conn, row, reason):
        children = conn.execute("SELECT * FROM fb_asset_delete_v2_video_accounts WHERE job_id=? AND object_key=? AND status='in_progress'",
                                (row["job_id"], row["object_key"])).fetchall()
        for child in children:
            saved = json.loads(child["result"])
            rejected = self._deferred_video_rejection(saved)
            status = "failed" if rejected else "unknown"
            result = dict(saved, reason=reason, requires_reconciliation=not rejected)
            if rejected:
                result.pop("needs_account_verification", None)
                result["verification_interrupted"] = True
            encoded, now = _json(result), _now()
            conn.execute("UPDATE fb_asset_delete_v2_video_account_attempts SET status=?,finished_at=?,result=? WHERE account_attempt_id=? AND status='in_progress'",
                         (status, now, encoded, child["account_attempt_id"]))
            conn.execute("UPDATE fb_asset_delete_v2_video_accounts SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=? AND account_id=?",
                         (status, encoded, now, row["job_id"], row["object_key"], child["account_id"]))
            action = "video_account_verification_interrupted" if rejected else "video_account_inflight_unknown"
            self._audit(conn, row["job_id"], action, dict(key=row["object_key"],
                account_id=child["account_id"], account_attempt_id=child["account_attempt_id"], reason=reason))
        return self._settle_video_parent(conn, row, reason)

    def _fence_inflight(self, conn, run_id, reason):
        rows = conn.execute("SELECT * FROM fb_asset_delete_v2_objects WHERE run_id=? AND status='in_progress'", (run_id,)).fetchall()
        unknown = 0
        for row in rows:
            if self._is_account_video(conn, row):
                unknown += self._fence_video_accounts(conn, row, reason) == "unknown"
                continue
            selected = {k: v for k, v in json.loads(row["result"]).items() if k in _CREDENTIAL_FIELDS}
            now, encoded = _now(), _json(dict(selected, reason=reason, requires_reconciliation=True))
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET status='unknown',finished_at=?,result=? WHERE attempt_id=? AND status='in_progress'",
                         (now, encoded, row["attempt_id"]))
            conn.execute("UPDATE fb_asset_delete_v2_objects SET status='unknown',result=?,updated_at=? WHERE job_id=? AND object_key=?",
                         (encoded, now, row["job_id"], row["object_key"]))
            conn.execute("UPDATE fb_asset_delete_v2_object_locks SET status='unknown',updated_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
            self._audit(conn, row["job_id"], "inflight_became_unknown", {"key": row["object_key"], "run_id": run_id, "reason": reason})
            unknown += 1
        return unknown

    def finish_run(self, run_id, status, summary=None):
        if status not in ("completed", "partial", "interrupted", "failed"):
            raise StoreError("Invalid run outcome", "invalid_input")
        with self._transaction() as conn:
            run = self._require(conn, "runs", "run_id", run_id)
            if run["status"] != "running":
                return self._run_data(run, True)
            self._fence_inflight(conn, run_id, "run_ended_without_definite_object_result")
            phases = json.loads(run["phases"])
            placeholders = ",".join("?" for _ in phases)
            incomplete = conn.execute("SELECT COUNT(*) FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind IN (" + placeholders + ") AND status NOT IN ('deleted','already_deleted')",
                                      (run["job_id"], *phases)).fetchone()[0]
            if status == "completed" and incomplete:
                status = "partial"
            now = _now()
            conn.execute("UPDATE fb_asset_delete_v2_runs SET status=?,updated_at=?,summary=? WHERE run_id=?",
                         (status, now, _json(summary or {}), run_id))
            conn.execute("UPDATE fb_asset_delete_v2_jobs SET status=?,updated_at=? WHERE job_id=?", (status, now, run["job_id"]))
            self._audit(conn, run["job_id"], "run_finished", {"run_id": run_id, "status": status, "summary": summary or {}})
            return self._run_data(self._require(conn, "runs", "run_id", run_id))

    def recover_interrupted(self, is_owner_alive=None):
        """Fence dead workers only; callback accepts (pid, start_identity).

        Unknown/failed liveness checks never authorize recovery. Untouched rows
        remain pending; a caller must explicitly create a new run to resume.
        """
        alive = is_owner_alive or _owner_alive
        counts = {"interrupted_runs": 0, "unknown_objects": 0, "live_runs": 0}
        with self._transaction() as conn:
            runs = conn.execute("SELECT * FROM fb_asset_delete_v2_runs WHERE status='running'").fetchall()
            for run in runs:
                if alive(run["owner_pid"], run["owner_start"]) is not False:
                    counts["live_runs"] += 1
                    continue
                counts["unknown_objects"] += self._fence_inflight(conn, run["run_id"], "worker_exited_with_unknown_outcome")
                now = _now()
                conn.execute("UPDATE fb_asset_delete_v2_runs SET status='interrupted',updated_at=? WHERE run_id=?", (now, run["run_id"]))
                conn.execute("UPDATE fb_asset_delete_v2_jobs SET status='interrupted',updated_at=? WHERE job_id=?", (now, run["job_id"]))
                self._audit(conn, run["job_id"], "run_interrupted", {"run_id": run["run_id"], "owner_pid": run["owner_pid"], "owner_start": run["owner_start"]})
                counts["interrupted_runs"] += 1
        return counts

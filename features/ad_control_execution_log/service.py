"""Pure helpers and the ``ads_ai`` persistence adapter for ad control.

The module deliberately has no dependency on the monolithic ``app`` module.
Callers pass database connection settings and keep SQLite as an outbox/fallback.
"""

import ast
import json
import os
import re
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone


WRITER_HOST = "101.32.56.53"
WRITER_PORT = 63353
READER_HOST = "101.32.56.53"
READER_PORT = 63350
WRITER_DATABASE = "ads_ai"
DEFAULT_TABLE = "ad_control_action_log"
QUALIFIED_TABLE = "`ads_ai`.`ad_control_action_log`"
MAX_CONNECT_TIMEOUT_SECONDS = 3
MAX_IO_TIMEOUT_SECONDS = 5
MAX_PAYLOAD_BYTES = 512 * 1024
WRITE_RATE_PER_SECOND = 1.0
WRITE_BURST = 2.0
WRITER_LOCK_FILE = (
    "/var/lock/ad_control_action_log_writer.lock"
    if os.name == "posix"
    else os.path.join(tempfile.gettempdir(), "ad_control_action_log_writer.lock")
)
RETRYABLE_GRAPH_CODES = {1, 2, 4, 17, 32, 613}
RETRYABLE_GRAPH_SUBCODES = {5044001}
TERMINAL_SKIP_REASONS = {"not_active", "already_paused", "not_pause_target"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_WRITE_LOCK = threading.Lock()


class ActionLogSafetyError(RuntimeError):
    """Raised when the dedicated action-log database boundary is violated."""


def normalize_account(value):
    return str(value or "").strip().replace("act_", "").replace("ACT_", "")


def balanced_execution_items(items, max_total=200, max_per_account=20):
    """Return a deterministic, fair batch with a per-account safety cap."""
    max_total = max(1, int(max_total or 1))
    max_per_account = max(1, int(max_per_account or 1))
    grouped = OrderedDict()
    ordered = sorted(
        list(items or []),
        key=lambda item: (
            normalize_account(item.get("account_id")),
            str(item.get("campaign_id") or item.get("object_id") or ""),
        ),
    )
    for item in ordered:
        grouped.setdefault(normalize_account(item.get("account_id")), []).append(item)
    selected = []
    offsets = {account_id: 0 for account_id in grouped}
    while len(selected) < max_total:
        progressed = False
        for account_id, account_items in grouped.items():
            offset = offsets[account_id]
            if offset >= len(account_items) or offset >= max_per_account:
                continue
            selected.append(account_items[offset])
            offsets[account_id] += 1
            progressed = True
            if len(selected) >= max_total:
                break
        if not progressed:
            break
    return selected


def _json_error_payload(reason):
    text = str(reason or "").strip()
    if not text:
        return {}
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                nested = payload.get("error")
                return nested if isinstance(nested, dict) else payload
        except Exception:
            try:
                payload = ast.literal_eval(candidate)
                if isinstance(payload, dict):
                    nested = payload.get("error")
                    return nested if isinstance(nested, dict) else payload
            except Exception:
                continue
    return {}


def graph_error_details(reason):
    payload = _json_error_payload(reason)
    text = str(reason or "")
    code = payload.get("code")
    subcode = payload.get("error_subcode")
    try:
        code = int(code) if code not in (None, "") else None
    except Exception:
        code = None
    try:
        subcode = int(subcode) if subcode not in (None, "") else None
    except Exception:
        subcode = None
    lower = text.lower()
    retryable = (
        code in RETRYABLE_GRAPH_CODES
        or subcode in RETRYABLE_GRAPH_SUBCODES
        or "rate limit" in lower
        or "request limit" in lower
        or "temporarily unavailable" in lower
        or "timed out" in lower
        or "timeout" in lower
        or "connection reset" in lower
        or "bad gateway" in lower
        or "service unavailable" in lower
        or "gateway timeout" in lower
        or "internal server error" in lower
        or bool(re.search(r"(?:http(?: status)?[ =:]*)?5\d\d\b", lower))
    )
    rate_limited = (
        code in {4, 17, 32, 613}
        or subcode in RETRYABLE_GRAPH_SUBCODES
        or "rate limit" in lower
        or "request limit" in lower
    )
    return {
        "retryable": bool(retryable),
        "rate_limited": bool(rate_limited),
        "error_code": code,
        "error_subcode": subcode,
        "error_type": str(payload.get("type") or ""),
        "error_message": str(payload.get("message") or text),
    }


def enrich_error_result(result):
    result = dict(result or {})
    result.update(graph_error_details(result.get("reason")))
    return result


def execution_summary(results, matched_count=0, requested_count=0, preview_error_count=0):
    results = list(results or [])
    matched_count = max(0, int(matched_count or 0))
    requested_count = max(0, int(requested_count or 0))
    deferred_count = max(0, matched_count - requested_count) + sum(
        1 for item in results if item.get("status") == "deferred"
    )
    retryable_error_count = sum(
        1 for item in results
        if item.get("status") == "error" and bool(item.get("retryable"))
    )
    permanent_error_count = sum(
        1 for item in results
        if item.get("status") == "error" and not bool(item.get("retryable"))
    )
    terminal_skip_count = sum(
        1 for item in results
        if item.get("status") == "skipped" and str(item.get("reason") or "") in TERMINAL_SKIP_REASONS
    )
    blocked_count = sum(
        1 for item in results
        if item.get("status") == "skipped" and str(item.get("reason") or "") not in TERMINAL_SKIP_REASONS
    )
    remaining_count = deferred_count + retryable_error_count
    if permanent_error_count or blocked_count:
        run_status = "blocked"
    elif remaining_count or int(preview_error_count or 0):
        run_status = "partial"
    else:
        run_status = "executed"
    return {
        "run_status": run_status,
        "deferred_count": deferred_count,
        "remaining_count": remaining_count,
        "retryable_error_count": retryable_error_count,
        "permanent_error_count": permanent_error_count,
        "terminal_skip_count": terminal_skip_count,
        "blocked_count": blocked_count,
        "preview_error_count": max(0, int(preview_error_count or 0)),
    }


def sanitize_json(value):
    """Remove credential-shaped fields before durable logging."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"access_token", "authorization", "password", "secret", "client_secret"}:
                clean[key] = "[REDACTED]"
            else:
                clean[key] = sanitize_json(item)
        return clean
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


def reason_summary(results):
    counts = {}
    for item in results or []:
        reason = str(item.get("reason") or "").strip()
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _identifier(value):
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError("invalid mysql identifier")
    return "`%s`" % text


def _bounded_int(value, default, maximum):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(1, min(parsed, int(maximum)))


def validate_target(config, role, table=DEFAULT_TABLE):
    """Fail closed unless a connection matches the dedicated log boundary."""
    config = dict(config or {})
    role = str(role or "").strip().lower()
    if role not in {"reader", "writer"}:
        raise ActionLogSafetyError("invalid ad-control action-log connection role")
    host = str(config.get("host") or "").strip()
    port = int(config.get("port") or 0)
    database = str(config.get("database") or "").strip()
    user = str(config.get("user") or "").strip()
    expected_host = WRITER_HOST if role == "writer" else READER_HOST
    expected_port = WRITER_PORT if role == "writer" else READER_PORT
    if host != expected_host or port != expected_port:
        raise ActionLogSafetyError(
            "ad-control action-log %s endpoint must be %s:%s" % (
                role, expected_host, expected_port,
            )
        )
    if database != WRITER_DATABASE:
        raise ActionLogSafetyError("ad-control action-log database must be ads_ai")
    if str(table or "").strip() != DEFAULT_TABLE:
        raise ActionLogSafetyError(
            "ad-control action-log table must be ad_control_action_log"
        )
    if not user:
        raise ActionLogSafetyError("ad-control action-log database user is required")
    config.update({
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "connect_timeout": _bounded_int(
            config.get("connect_timeout"), 3, MAX_CONNECT_TIMEOUT_SECONDS
        ),
        "read_timeout": _bounded_int(
            config.get("read_timeout"), 5, MAX_IO_TIMEOUT_SECONDS
        ),
        "write_timeout": _bounded_int(
            config.get("write_timeout"), 5, MAX_IO_TIMEOUT_SECONDS
        ),
    })
    return config


def _connect(config, role, table=DEFAULT_TABLE):
    import pymysql

    config = validate_target(config, role, table)
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=str(config.get("password") or ""),
        database=config["database"],
        charset="utf8mb4",
        connect_timeout=config["connect_timeout"],
        read_timeout=config["read_timeout"],
        write_timeout=config["write_timeout"],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


LOG_COLUMNS = [
    "action_id", "preview_id", "binding_id", "rule_id", "event_key", "source_type",
    "actor_user_id", "product", "action", "object_level", "run_status", "runner_reason",
    "dry_run", "scanned_count", "candidate_count", "matched_count", "batch_planned_count",
    "deferred_count", "requested_count", "success_count", "skipped_count", "error_count",
    "retryable_error_count", "blocked_count", "remaining_count", "criteria_json", "results_json",
    "reason_summary_json", "log_version", "created_at", "updated_at",
]


def normalize_record(record):
    record = dict(record or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    criteria = sanitize_json(record.pop("criteria", {}))
    results = sanitize_json(record.pop("results", []))
    normalized = {key: record.get(key, "") for key in LOG_COLUMNS}
    for key in (
        "dry_run", "scanned_count", "candidate_count", "matched_count", "batch_planned_count",
        "deferred_count", "requested_count", "success_count", "skipped_count", "error_count",
        "retryable_error_count", "blocked_count", "remaining_count", "log_version",
    ):
        normalized[key] = max(0, int(normalized.get(key) or 0))
    normalized["criteria_json"] = json.dumps(criteria, ensure_ascii=False, separators=(",", ":"))
    normalized["results_json"] = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
    normalized["reason_summary_json"] = json.dumps(reason_summary(results), ensure_ascii=False, separators=(",", ":"))
    normalized["created_at"] = str(normalized.get("created_at") or now)
    normalized["updated_at"] = str(normalized.get("updated_at") or now)
    normalized["log_version"] = int(normalized.get("log_version") or 1)
    return normalized


def _enforce_payload_limit(record):
    payload_bytes = sum(
        len(str(record.get(key) or "").encode("utf-8"))
        for key in ("criteria_json", "results_json", "reason_summary_json")
    )
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise ActionLogSafetyError(
            "ad-control action-log JSON payload exceeds %s bytes" % MAX_PAYLOAD_BYTES
        )


def _acquire_interprocess_write_slot():
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(WRITER_LOCK_FILE, flags, 0o600)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise ActionLogSafetyError("ad-control writer lock must be a regular file")
    if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
        os.close(fd)
        raise ActionLogSafetyError("ad-control writer lock has an unexpected owner")
    handle = os.fdopen(fd, "r+", encoding="ascii")
    fcntl = None
    try:
        try:
            import fcntl as fcntl_module

            fcntl = fcntl_module
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Production is Linux; the in-process lock remains active on Windows tests.
            pass
        except (BlockingIOError, OSError):
            raise ActionLogSafetyError("ad-control action-log global writer is busy")
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
            last_refill = float(state.get("last_refill") or 0.0)
            tokens = float(state.get("tokens") or 0.0)
        except Exception:
            last_refill = 0.0
            tokens = 0.0
        now = time.time()
        if last_refill <= 0:
            tokens = WRITE_BURST
        else:
            tokens = min(
                WRITE_BURST,
                tokens + max(0.0, now - last_refill) * WRITE_RATE_PER_SECOND,
            )
        if tokens < 1.0:
            raise ActionLogSafetyError(
                "ad-control action-log writer exceeded burst 2 / average 1 qps"
            )
        tokens -= 1.0
        handle.seek(0)
        handle.truncate(0)
        handle.write(json.dumps({"last_refill": now, "tokens": tokens}))
        handle.flush()
        os.fsync(handle.fileno())
        return handle, fcntl
    except Exception:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        handle.close()
        raise


def _release_interprocess_write_slot(handle, fcntl):
    if handle is None:
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    handle.close()


def _serialized_write(config, table, callback):
    """Execute one bounded statement with Linux host-wide concurrency and rate 1."""
    validate_target(config, "writer", table)
    if not _WRITE_LOCK.acquire(blocking=False):
        raise ActionLogSafetyError("ad-control action-log writer is busy")
    conn = None
    lock_handle = None
    fcntl = None
    try:
        lock_handle, fcntl = _acquire_interprocess_write_slot()
        conn = _connect(config, "writer", table)
        with conn.cursor() as cursor:
            return callback(cursor)
    finally:
        if conn is not None:
            conn.close()
        _release_interprocess_write_slot(lock_handle, fcntl)
        _WRITE_LOCK.release()


def upsert_action(config, record, table=DEFAULT_TABLE):
    config = validate_target(config, "writer", table)
    record = normalize_record(record)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(record.get("action_id") or "")):
        raise ActionLogSafetyError("invalid ad-control action_id")
    _enforce_payload_limit(record)
    column_sql = ",".join(_identifier(key) for key in LOG_COLUMNS)
    placeholders = ",".join(["%s"] * len(LOG_COLUMNS))
    updates = ",".join(
        "%s=VALUES(%s)" % (_identifier(key), _identifier(key))
        for key in LOG_COLUMNS if key not in {"action_id", "created_at"}
    )
    sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
        QUALIFIED_TABLE, column_sql, placeholders, updates,
    )
    _serialized_write(
        config,
        table,
        lambda cursor: cursor.execute(sql, [record[key] for key in LOG_COLUMNS]),
    )
    return record


def update_runner_status(config, action_id, event_key, status, reason, remaining_count, table=DEFAULT_TABLE):
    config = validate_target(config, "writer", table)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(action_id or "")):
        raise ActionLogSafetyError("invalid ad-control action_id")
    sql = "UPDATE %s SET event_key=%%s,run_status=%%s,runner_reason=%%s,remaining_count=%%s,updated_at=UTC_TIMESTAMP() WHERE action_id=%%s LIMIT 1" % QUALIFIED_TABLE
    def execute(cursor):
        cursor.execute(
            sql,
            (
                event_key or "",
                status or "",
                reason or "",
                max(0, int(remaining_count or 0)),
                action_id,
            ),
        )
        return int(cursor.rowcount or 0)

    return _serialized_write(config, table, execute)


def _decode_row(row, include_results=False):
    item = dict(row or {})
    for key in ("created_at", "updated_at"):
        value = item.get(key)
        if hasattr(value, "strftime"):
            item[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif value is not None:
            item[key] = str(value)
    for source, target, default in (
        ("criteria_json", "criteria", {}),
        ("reason_summary_json", "reason_summary", []),
    ):
        try:
            item[target] = json.loads(item.pop(source, "") or "")
        except Exception:
            item[target] = default
    if include_results:
        try:
            item["results"] = json.loads(item.pop("results_json", "") or "[]")
        except Exception:
            item["results"] = []
    else:
        item.pop("results_json", None)
        item["results"] = []
    item["level"] = item.pop("object_level", "campaign")
    item["binding_id"] = item.get("binding_id") or ""
    item["dry_run"] = bool(item.get("dry_run"))
    item["log_store"] = "ads_ai"
    return item


def list_actions(config, filters=None, limit=50, table=DEFAULT_TABLE):
    config = validate_target(config, "reader", table)
    filters = dict(filters or {})
    where = []
    params = []
    for key in ("product", "binding_id", "action"):
        value = str(filters.get(key) or "").strip()
        if value:
            where.append("%s=%%s" % _identifier(key))
            params.append(value)
    if filters.get("date_from"):
        where.append("created_at>=%s")
        params.append(str(filters["date_from"]))
    if filters.get("date_to"):
        where.append("created_at<=%s")
        params.append(str(filters["date_to"]))
    selected = [key for key in LOG_COLUMNS if key != "results_json"]
    sql = "SELECT %s FROM %s%s ORDER BY created_at DESC LIMIT %%s" % (
        ",".join(_identifier(key) for key in selected),
        QUALIFIED_TABLE,
        (" WHERE " + " AND ".join(where)) if where else "",
    )
    params.append(max(1, min(200, int(limit or 50))))
    conn = _connect(config, "reader", table)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return [_decode_row(row, include_results=False) for row in cursor.fetchall()]
    finally:
        conn.close()


def fetch_action(config, action_id, table=DEFAULT_TABLE):
    config = validate_target(config, "reader", table)
    sql = "SELECT %s FROM %s WHERE action_id=%%s LIMIT 1" % (
        ",".join(_identifier(key) for key in LOG_COLUMNS), QUALIFIED_TABLE,
    )
    conn = _connect(config, "reader", table)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(action_id or ""),))
            row = cursor.fetchone()
            return _decode_row(row, include_results=True) if row else None
    finally:
        conn.close()

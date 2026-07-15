"""Pure helpers and the ``ads_ai`` persistence adapter for ad control.

The module deliberately has no dependency on the monolithic ``app`` module.
Callers pass database connection settings and keep SQLite as an outbox/fallback.
"""

import ast
import json
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone


DEFAULT_TABLE = "ad_control_action_log"
RETRYABLE_GRAPH_CODES = {1, 2, 4, 17, 32, 613}
RETRYABLE_GRAPH_SUBCODES = {5044001}
TERMINAL_SKIP_REASONS = {"not_active", "already_paused", "not_pause_target"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_TABLE_READY = set()
_TABLE_LOCK = threading.Lock()


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


def _connect(config):
    import pymysql

    return pymysql.connect(
        host=str(config.get("host") or "127.0.0.1"),
        port=int(config.get("port") or 3306),
        user=str(config.get("user") or ""),
        password=str(config.get("password") or ""),
        database=str(config.get("database") or "ads_ai"),
        charset="utf8mb4",
        connect_timeout=int(config.get("connect_timeout") or 5),
        read_timeout=int(config.get("read_timeout") or 8),
        write_timeout=int(config.get("write_timeout") or 8),
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def table_ddl(database="ads_ai", table=DEFAULT_TABLE):
    qualified = "%s.%s" % (_identifier(database), _identifier(table))
    return """
CREATE TABLE IF NOT EXISTS {qualified} (
  action_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  preview_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  binding_id VARCHAR(128) NOT NULL DEFAULT '',
  rule_id VARCHAR(128) NOT NULL DEFAULT '',
  event_key VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  source_type VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'api',
  actor_user_id VARCHAR(128) NOT NULL DEFAULT '',
  product VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  action VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  object_level VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'campaign',
  run_status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  runner_reason VARCHAR(255) NOT NULL DEFAULT '',
  dry_run TINYINT(1) UNSIGNED NOT NULL DEFAULT 0,
  scanned_count INT UNSIGNED NOT NULL DEFAULT 0,
  candidate_count INT UNSIGNED NOT NULL DEFAULT 0,
  matched_count INT UNSIGNED NOT NULL DEFAULT 0,
  batch_planned_count INT UNSIGNED NOT NULL DEFAULT 0,
  deferred_count INT UNSIGNED NOT NULL DEFAULT 0,
  requested_count INT UNSIGNED NOT NULL DEFAULT 0,
  success_count INT UNSIGNED NOT NULL DEFAULT 0,
  skipped_count INT UNSIGNED NOT NULL DEFAULT 0,
  error_count INT UNSIGNED NOT NULL DEFAULT 0,
  retryable_error_count INT UNSIGNED NOT NULL DEFAULT 0,
  blocked_count INT UNSIGNED NOT NULL DEFAULT 0,
  remaining_count INT UNSIGNED NOT NULL DEFAULT 0,
  criteria_json MEDIUMTEXT NOT NULL,
  results_json MEDIUMTEXT NOT NULL,
  reason_summary_json TEXT NOT NULL,
  log_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (action_id),
  KEY idx_acl_created (created_at, action_id),
  KEY idx_acl_product_created (product, created_at, action_id),
  KEY idx_acl_binding_created (binding_id, created_at, action_id),
  KEY idx_acl_event_created (event_key, created_at, action_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
""".format(qualified=qualified)


def ensure_table(config, table=DEFAULT_TABLE):
    key = (str(config.get("host") or ""), str(config.get("database") or "ads_ai"), str(table))
    if key in _TABLE_READY:
        return
    with _TABLE_LOCK:
        if key in _TABLE_READY:
            return
        conn = _connect(config)
        try:
            with conn.cursor() as cursor:
                cursor.execute(table_ddl(config.get("database") or "ads_ai", table))
            _TABLE_READY.add(key)
        finally:
            conn.close()


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


def upsert_action(config, record, table=DEFAULT_TABLE):
    ensure_table(config, table)
    record = normalize_record(record)
    column_sql = ",".join(_identifier(key) for key in LOG_COLUMNS)
    placeholders = ",".join(["%s"] * len(LOG_COLUMNS))
    updates = ",".join(
        "%s=VALUES(%s)" % (_identifier(key), _identifier(key))
        for key in LOG_COLUMNS if key not in {"action_id", "created_at"}
    )
    sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
        _identifier(table), column_sql, placeholders, updates,
    )
    conn = _connect(config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, [record[key] for key in LOG_COLUMNS])
    finally:
        conn.close()
    return record


def update_runner_status(config, action_id, event_key, status, reason, remaining_count, table=DEFAULT_TABLE):
    ensure_table(config, table)
    sql = "UPDATE %s SET event_key=%%s,run_status=%%s,runner_reason=%%s,remaining_count=%%s,updated_at=UTC_TIMESTAMP() WHERE action_id=%%s" % _identifier(table)
    conn = _connect(config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (event_key or "", status or "", reason or "", max(0, int(remaining_count or 0)), action_id))
            return int(cursor.rowcount or 0)
    finally:
        conn.close()


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
    ensure_table(config, table)
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
        _identifier(table),
        (" WHERE " + " AND ".join(where)) if where else "",
    )
    params.append(max(1, min(200, int(limit or 50))))
    conn = _connect(config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return [_decode_row(row, include_results=False) for row in cursor.fetchall()]
    finally:
        conn.close()


def fetch_action(config, action_id, table=DEFAULT_TABLE):
    ensure_table(config, table)
    sql = "SELECT %s FROM %s WHERE action_id=%%s LIMIT 1" % (
        ",".join(_identifier(key) for key in LOG_COLUMNS), _identifier(table),
    )
    conn = _connect(config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(action_id or ""),))
            row = cursor.fetchone()
            return _decode_row(row, include_results=True) if row else None
    finally:
        conn.close()

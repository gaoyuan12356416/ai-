"""Validation, message rendering, and a SQLite outbox for status broadcasts.

This module deliberately has no HTTP, MySQL, or Feishu dependency.  The HTTP
handler owns the 32 KiB request-body limit and Bearer authentication; the
worker owns optimizer/email/open-id resolution and network delivery.
"""

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import unicodedata


MAX_REQUEST_BYTES = 32 * 1024
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
PAYLOAD_FIELDS = (
    "resource_id",
    "resource_name",
    "task_start_time",
    "drama_dubbing_type",
    "task_type",
    "original_material_name",
    "material_name",
    "language",
    "final_status",
    "optimizer_name",
)

_FIELD_LIMITS = {
    "resource_id": 128,
    "resource_name": 255,
    "drama_dubbing_type": 64,
    "task_type": 64,
    "original_material_name": 255,
    "material_name": 255,
    "language": 100,
    "final_status": 64,
    "optimizer_name": 100,
}
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RFC3339_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt]"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?"
    r"(?P<zone>[Zz]|[+-]\d{2}:\d{2})$"
)
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+"
    r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_FEISHU_OPEN_ID_RE = re.compile(r"(?i)\bou_[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_NAMED_SECRET_RE = re.compile(
    r"""(?ix)
    (
      ["']?
      (?:access[_-]?token|refresh[_-]?token|tenant[_-]?access[_-]?token|
         token|authorization|open[_-]?id|email)
      ["']?\s*[:=]\s*["']?
    )
    ([^"',;\s}\]]+)
    """
)
_RESULT_FIELDS = frozenset(
    ("admin_user_id", "masked_email", "feishu_message_id", "failure_code")
)
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_FALLBACK_REASON_LABELS = {
    "optimizer_name_missing": "接口未提供优化师名称",
    "admin_user_not_found": "优化师名称未匹配到 admin_users.username",
    "optimizer_not_found": "优化师名称未匹配到 admin_users.username",
    "optimizer_ambiguous": "优化师名称匹配到多个 admin_users 用户",
    "email_missing": "admin_user_group 未配置可用邮箱",
    "optimizer_email_missing": "admin_user_group 未配置可用邮箱",
    "optimizer_email_ambiguous": "admin_user_group 存在多个不同邮箱",
    "feishu_user_not_found": "邮箱未匹配到飞书用户",
    "feishu_user_ambiguous": "邮箱匹配到多个飞书用户",
    "private_send_failed": "优化师已匹配，但飞书私聊发送失败",
    "mapping_unavailable": "优化师映射服务暂时不可用",
    "mapping_not_configured": "优化师映射服务未配置",
    "mapping_invalid": "优化师映射数据无效",
    "internal_error": "播报处理发生内部异常",
    "optimizer_not_matched": "未匹配到对应优化师",
    "mapping_error": "优化师映射过程异常",
}


class MaterialStatusError(RuntimeError):
    """Stable error suitable for translation into an API response."""

    def __init__(self, code, message, status=400):
        self.code = str(code or "material_status_error")
        self.status = int(status or 400)
        super().__init__(redact_sensitive_text(message, limit=500))


def redact_sensitive_text(value, limit=500):
    """Return one-line diagnostics without complete credentials or identities."""

    text = str(value or "").replace("\x00", " ")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _NAMED_SECRET_RE.sub(
        lambda match: match.group(1) + "[redacted]", text
    )
    text = _EMAIL_RE.sub("[email redacted]", text)
    text = _FEISHU_OPEN_ID_RE.sub("[open_id redacted]", text)
    return text[: max(1, int(limit))]


def validate_bearer_authorization(authorization_header, expected_token):
    """Constant-time validation for the handler's independent Bearer token."""

    expected = str(expected_token or "")
    supplied = str(authorization_header or "")
    if not expected or any(char.isspace() for char in expected):
        return False
    if not supplied.startswith("Bearer "):
        return False
    candidate = supplied[7:]
    if not candidate or any(char.isspace() for char in candidate):
        return False
    try:
        candidate_bytes = candidate.encode("ascii")
        expected_bytes = expected.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(candidate_bytes, expected_bytes)


def validate_idempotency_key(value):
    """Validate and return the caller-supplied idempotency key."""

    if not isinstance(value, str):
        raise MaterialStatusError(
            "idempotency_key_required",
            "Idempotency-Key 请求头必填",
            400,
        )
    key = value.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise MaterialStatusError(
            "invalid_idempotency_key",
            "Idempotency-Key 必须为 8 至 128 位字母、数字、点、下划线、冒号或连字符",
            400,
        )
    return key


def _normalize_text(value, field, allow_empty=False):
    if not isinstance(value, str):
        raise MaterialStatusError(
            "invalid_payload",
            "%s 必须为字符串" % field,
            422,
        )
    text = unicodedata.normalize("NFC", value).strip()
    if not text and not allow_empty:
        raise MaterialStatusError(
            "invalid_payload",
            "%s 不能为空" % field,
            422,
        )
    if len(text) > _FIELD_LIMITS[field]:
        raise MaterialStatusError(
            "invalid_payload",
            "%s 长度不能超过 %d 个字符" % (field, _FIELD_LIMITS[field]),
            422,
        )
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise MaterialStatusError(
            "invalid_payload",
            "%s 只能包含单行可显示文本" % field,
            422,
        )
    return text


def _parse_rfc3339(value):
    if not isinstance(value, str):
        raise MaterialStatusError(
            "invalid_payload",
            "task_start_time 必须为 RFC3339 字符串",
            422,
        )
    text = value.strip()
    match = _RFC3339_RE.fullmatch(text)
    if not match:
        raise MaterialStatusError(
            "invalid_payload",
            "task_start_time 必须为带时区的 RFC3339 时间",
            422,
        )
    parsed_text = (
        match.group("date")
        + "T"
        + match.group("time")
        + (match.group("fraction") or "")
        + ("+00:00" if match.group("zone").lower() == "z" else match.group("zone"))
    )
    try:
        parsed = datetime.fromisoformat(parsed_text)
    except ValueError:
        raise MaterialStatusError(
            "invalid_payload",
            "task_start_time 不是有效日期时间",
            422,
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaterialStatusError(
            "invalid_payload",
            "task_start_time 必须包含时区",
            422,
        )
    # RFC3339 offsets are bounded to 23:59 syntactically, while datetime may
    # reject the impossible values.  Store a single instant representation so
    # semantically equivalent offsets have the same payload hash.
    utc_value = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def normalize_payload(payload):
    """Validate the exact ten-field contract and return canonical values."""

    if not isinstance(payload, dict):
        raise MaterialStatusError(
            "invalid_payload",
            "请求体必须为 JSON 对象",
            422,
        )
    actual = set(payload)
    expected = set(PAYLOAD_FIELDS)
    missing = sorted(expected.difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing or unknown:
        details = []
        if missing:
            details.append("缺少字段: %s" % ", ".join(missing))
        if unknown:
            details.append("未知字段: %s" % ", ".join(unknown))
        raise MaterialStatusError(
            "invalid_payload",
            "；".join(details),
            422,
        )

    normalized = {}
    for field in PAYLOAD_FIELDS:
        if field == "task_start_time":
            normalized[field] = _parse_rfc3339(payload[field])
        else:
            normalized[field] = _normalize_text(
                payload[field],
                field,
                allow_empty=(field == "optimizer_name"),
            )
    return normalized


def canonical_payload_json(payload):
    """Return deterministic UTF-8 JSON for hashing and durable storage."""

    normalized = normalize_payload(payload)
    ordered = {field: normalized[field] for field in PAYLOAD_FIELDS}
    return json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(payload):
    """Return the SHA-256 hash of the normalized payload."""

    canonical = canonical_payload_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_ip(value):
    text = str(value or "").strip()
    if not text or "," in text:
        return ""
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return ""


def extract_audit_source_ip(peer_ip, x_real_ip=""):
    """Extract an audit IP without making an authorization decision.

    ``X-Real-IP`` is trusted only when the TCP peer is exactly the local
    reverse proxy.  A remote caller cannot spoof this audit field.
    """

    peer = _normalize_ip(peer_ip)
    if peer in ("127.0.0.1", "::1"):
        forwarded = _normalize_ip(x_real_ip)
        if forwarded:
            return forwarded
    return peer or "unknown"


def _display_shanghai_time(rfc3339_utc):
    parsed = datetime.fromisoformat(str(rfc3339_utc).replace("Z", "+00:00"))
    local = parsed.astimezone(_SHANGHAI_TZ)
    timespec = "microseconds" if local.microsecond else "seconds"
    rendered = local.isoformat(timespec=timespec)
    return rendered + "（Asia/Shanghai，UTC+08:00）"


def format_event_id(event_id):
    """Return the stable public event identifier used in responses/messages."""

    if isinstance(event_id, bool):
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400)
    try:
        parsed = int(event_id)
    except (TypeError, ValueError, OverflowError):
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400) from None
    if parsed <= 0:
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400)
    return "MSE-%010d" % parsed


def _event_line(event_id):
    if event_id in (None, ""):
        return []
    return ["事件编号：%s" % format_event_id(event_id)]


def _payload_message_lines(payload, event_id=None):
    item = normalize_payload(payload)
    optimizer = item["optimizer_name"] or "（未提供）"
    lines = [
        "资源ID：%s" % item["resource_id"],
        "资源名：%s" % item["resource_name"],
        "任务开始时间：%s" % _display_shanghai_time(item["task_start_time"]),
        "剧集配音类型：%s" % item["drama_dubbing_type"],
        "任务类型：%s" % item["task_type"],
        "素材原始名：%s" % item["original_material_name"],
        "素材名：%s" % item["material_name"],
        "语种：%s" % item["language"],
        "最终状态：%s" % item["final_status"],
        "优化师名称：%s" % optimizer,
    ]
    if event_id not in (None, ""):
        lines.extend([""] + _event_line(event_id))
    return lines


def format_private_message(payload, event_id=None):
    """Render the exact plain-text private-chat broadcast."""

    return "\n".join(
        ["【素材任务最终状态播报】", ""]
        + _payload_message_lines(payload, event_id)
    )


def format_fallback_message(
    payload,
    reason_code="optimizer_not_matched",
    reason_text="",
    event_id=None,
):
    """Render a group fallback with a safe, actionable mapping reason."""

    code = _safe_error_code(reason_code or "optimizer_not_matched")
    reason = _FALLBACK_REASON_LABELS.get(code)
    if not reason:
        reason = (
            redact_sensitive_text(reason_text, limit=200)
            or "未匹配到对应优化师"
        )
    lines = [
        "【⚠️ 素材任务播报未能私聊】",
        "",
        "失败原因：%s" % code,
        "说明：%s" % reason,
        "",
    ]
    lines.extend(_payload_message_lines(payload, event_id))
    lines.append(
        "请检查优化师名称、admin_users.username、admin_user_group.email 及飞书用户映射。"
    )
    return "\n".join(lines)


def sanitize_result_details(details):
    """Allow only non-secret, already-masked delivery metadata."""

    if details in (None, {}):
        return {}
    if not isinstance(details, dict):
        raise MaterialStatusError(
            "invalid_result_details",
            "投递结果必须为对象",
            400,
        )
    unknown = sorted(set(details).difference(_RESULT_FIELDS))
    if unknown:
        raise MaterialStatusError(
            "invalid_result_details",
            "投递结果包含禁止字段: %s" % ", ".join(unknown),
            400,
        )
    result = {}
    if "admin_user_id" in details and details["admin_user_id"] not in (None, ""):
        value = str(details["admin_user_id"]).strip()
        if (
            len(value) > 64
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
        ):
            raise MaterialStatusError(
                "invalid_result_details",
                "admin_user_id 无效",
                400,
            )
        result["admin_user_id"] = value
    if "masked_email" in details and details["masked_email"] not in (None, ""):
        value = str(details["masked_email"]).strip()
        if (
            len(value) > 128
            or "@" not in value
            or "*" not in value.split("@", 1)[0]
            or _EMAIL_RE.fullmatch(value)
        ):
            raise MaterialStatusError(
                "invalid_result_details",
                "masked_email 必须是已遮蔽的邮箱",
                400,
            )
        result["masked_email"] = value
    if (
        "feishu_message_id" in details
        and details["feishu_message_id"] not in (None, "")
    ):
        value = str(details["feishu_message_id"]).strip()
        if not _SAFE_MESSAGE_ID_RE.fullmatch(value):
            raise MaterialStatusError(
                "invalid_result_details",
                "feishu_message_id 无效",
                400,
            )
        result["feishu_message_id"] = value
    if "failure_code" in details and details["failure_code"] not in (None, ""):
        value = str(details["failure_code"]).strip()
        if not _SAFE_ERROR_CODE_RE.fullmatch(value):
            raise MaterialStatusError(
                "invalid_result_details",
                "failure_code 无效",
                400,
            )
        result["failure_code"] = value
    return result


def _utc_datetime(clock):
    value = clock()
    if not isinstance(value, datetime):
        raise RuntimeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_storage(db_path):
    """Create the additive outbox and optimizer-cache tables if absent."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS material_status_broadcast_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_ip TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN (
                        'queued','processing','retry',
                        'delivered','dead_letter'
                    )),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_attempt_at TEXT NOT NULL,
                lease_id TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                delivery_kind TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                delivered_at TEXT NOT NULL DEFAULT '',
                dead_lettered_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_material_status_broadcast_claim
            ON material_status_broadcast_outbox(
                status, next_attempt_at, lease_expires_at, id
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS material_status_optimizer_cache (
                optimizer_name TEXT PRIMARY KEY,
                admin_user_id TEXT NOT NULL,
                email TEXT NOT NULL,
                refreshed_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _positive_event_id(value):
    if isinstance(value, bool):
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400) from None
    if parsed <= 0:
        raise MaterialStatusError("invalid_event_id", "事件编号无效", 400)
    return parsed


def _safe_error_code(value):
    text = str(value or "delivery_failed").strip()
    if not _SAFE_ERROR_CODE_RE.fullmatch(text):
        return "delivery_failed"
    return text


def _decode_row(row):
    if row is None:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json"))
    except (TypeError, ValueError):
        item["payload"] = {}
        item.pop("payload_json", None)
    try:
        item["result"] = json.loads(item.pop("result_json"))
    except (TypeError, ValueError):
        item["result"] = {}
        item.pop("result_json", None)
    return item


def normalize_optimizer_cache_entry(optimizer_name, admin_user_id, email):
    name = str(optimizer_name or "").strip()
    user_id = str(admin_user_id or "").strip()
    address = str(email or "").strip()
    if (
        not name
        or len(name) > _FIELD_LIMITS["optimizer_name"]
        or any(unicodedata.category(char) == "Cc" for char in name)
    ):
        raise MaterialStatusError(
            "invalid_optimizer_cache_entry",
            "optimizer cache name is invalid",
            400,
        )
    if not user_id.isdigit():
        raise MaterialStatusError(
            "invalid_optimizer_cache_entry",
            "optimizer cache admin user id is invalid",
            400,
        )
    if len(address) > 254 or not _EMAIL_RE.fullmatch(address):
        raise MaterialStatusError(
            "invalid_optimizer_cache_entry",
            "optimizer cache email is invalid",
            400,
        )
    return {
        "optimizer_name": name,
        "admin_user_id": user_id,
        "email": address,
    }


class MaterialStatusOptimizerCache:
    """Persistent last-known-good optimizer-to-email mapping cache."""

    def __init__(self, db_path, clock=None):
        self.db_path = Path(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        ensure_storage(self.db_path)

    def get(self, optimizer_name):
        name = str(optimizer_name or "").strip()
        if not name:
            return None
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT optimizer_name,admin_user_id,email,refreshed_at
                FROM material_status_optimizer_cache
                WHERE optimizer_name=?
                """,
                (name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "matched": True,
            "admin_user_id": str(row["admin_user_id"]),
            "email": str(row["email"]),
            "refreshed_at": str(row["refreshed_at"]),
        }

    def upsert(self, optimizer_name, admin_user_id, email):
        entry = normalize_optimizer_cache_entry(
            optimizer_name,
            admin_user_id,
            email,
        )
        refreshed_at = _utc_iso(_utc_datetime(self.clock))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO material_status_optimizer_cache(
                    optimizer_name,admin_user_id,email,refreshed_at
                ) VALUES(?,?,?,?)
                """,
                (
                    entry["optimizer_name"],
                    entry["admin_user_id"],
                    entry["email"],
                    refreshed_at,
                ),
            )
            conn.commit()
        return self.get(entry["optimizer_name"])

    def replace_all(self, entries):
        normalized = {}
        for raw_entry in entries or ():
            entry = normalize_optimizer_cache_entry(
                (raw_entry or {}).get("optimizer_name"),
                (raw_entry or {}).get("admin_user_id"),
                (raw_entry or {}).get("email"),
            )
            previous = normalized.get(entry["optimizer_name"])
            if previous is not None and previous != entry:
                raise MaterialStatusError(
                    "invalid_optimizer_cache_entry",
                    "optimizer cache contains conflicting names",
                    400,
                )
            normalized[entry["optimizer_name"]] = entry
        refreshed_at = _utc_iso(_utc_datetime(self.clock))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM material_status_optimizer_cache")
            conn.executemany(
                """
                INSERT INTO material_status_optimizer_cache(
                    optimizer_name,admin_user_id,email,refreshed_at
                ) VALUES(?,?,?,?)
                """,
                [
                    (
                        entry["optimizer_name"],
                        entry["admin_user_id"],
                        entry["email"],
                        refreshed_at,
                    )
                    for entry in normalized.values()
                ],
            )
            conn.commit()
        return len(normalized)

    def count(self):
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM material_status_optimizer_cache"
            ).fetchone()
        return int(row["count"] if row is not None else 0)


class MaterialStatusOutbox:
    """Atomic SQLite outbox with leases, retries, and terminal states."""

    def __init__(self, db_path, clock=None):
        self.db_path = Path(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        ensure_storage(self.db_path)

    def get(self, event_id):
        event_id = _positive_event_id(event_id)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (event_id,),
            ).fetchone()
        return _decode_row(row)

    def enqueue(
        self,
        idempotency_key,
        payload,
        max_attempts=5,
        source_ip="",
    ):
        key = validate_idempotency_key(idempotency_key)
        normalized = normalize_payload(payload)
        canonical = canonical_payload_json(normalized)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            attempts = int(max_attempts)
        except (TypeError, ValueError, OverflowError):
            raise MaterialStatusError(
                "invalid_max_attempts",
                "max_attempts 必须为 1 至 20 的整数",
                400,
            ) from None
        if isinstance(max_attempts, bool) or attempts < 1 or attempts > 20:
            raise MaterialStatusError(
                "invalid_max_attempts",
                "max_attempts 必须为 1 至 20 的整数",
                400,
            )
        audit_ip = _normalize_ip(source_ip)
        now = _utc_iso(_utc_datetime(self.clock))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM material_status_broadcast_outbox
                WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["payload_hash"]), digest):
                    conn.rollback()
                    raise MaterialStatusError(
                        "idempotency_conflict",
                        "同一 Idempotency-Key 已对应不同请求体",
                        409,
                    )
                conn.commit()
                item = _decode_row(existing)
                item["created"] = False
                return item
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO material_status_broadcast_outbox(
                        idempotency_key,payload_hash,payload_json,source_ip,
                        status,attempt_count,max_attempts,next_attempt_at,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,'queued',0,?,?,?,?)
                    """,
                    (
                        key,
                        digest,
                        canonical,
                        audit_ip,
                        attempts,
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                raise MaterialStatusError(
                    "outbox_conflict",
                    "播报事件写入冲突",
                    409,
                ) from None
            event_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (event_id,),
            ).fetchone()
            conn.commit()
        item = _decode_row(row)
        item["created"] = True
        return item

    def claim_next(self, lease_seconds=60):
        try:
            lease_seconds = int(lease_seconds)
        except (TypeError, ValueError, OverflowError):
            raise MaterialStatusError(
                "invalid_lease",
                "lease_seconds 必须为 5 至 3600 的整数",
                400,
            ) from None
        if isinstance(lease_seconds, bool) or not 5 <= lease_seconds <= 3600:
            raise MaterialStatusError(
                "invalid_lease",
                "lease_seconds 必须为 5 至 3600 的整数",
                400,
            )
        now_dt = _utc_datetime(self.clock)
        now = _utc_iso(now_dt)
        expires = _utc_iso(now_dt + timedelta(seconds=lease_seconds))
        lease_id = secrets.token_hex(16)
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE material_status_broadcast_outbox
                SET status='dead_letter',
                    lease_id='',
                    lease_expires_at='',
                    dead_lettered_at=?,
                    last_error_code=CASE
                        WHEN last_error_code='' THEN 'lease_expired'
                        ELSE last_error_code
                    END,
                    last_error_message=CASE
                        WHEN last_error_message='' THEN '处理租约过期且重试次数已耗尽'
                        ELSE last_error_message
                    END,
                    updated_at=?
                WHERE status='processing'
                  AND lease_expires_at!=''
                  AND lease_expires_at<=?
                  AND attempt_count>=max_attempts
                """,
                (now, now, now),
            )
            row = conn.execute(
                """
                SELECT * FROM material_status_broadcast_outbox
                WHERE attempt_count < max_attempts
                  AND (
                    (status IN ('queued','retry') AND next_attempt_at<=?)
                    OR
                    (status='processing' AND lease_expires_at!=''
                        AND lease_expires_at<=?)
                  )
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE material_status_broadcast_outbox
                SET status='processing',
                    attempt_count=attempt_count+1,
                    lease_id=?,
                    lease_expires_at=?,
                    claimed_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (lease_id, expires, now, now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()
            conn.commit()
        return _decode_row(claimed)

    def _processing_row(self, conn, event_id, lease_id):
        event_id = _positive_event_id(event_id)
        supplied_lease = str(lease_id or "")
        row = conn.execute(
            "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
            (event_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "processing"
            or not supplied_lease
            or not hmac.compare_digest(str(row["lease_id"]), supplied_lease)
        ):
            raise MaterialStatusError(
                "outbox_lease_conflict",
                "事件不存在、未处理中或租约已失效",
                409,
            )
        return row

    def schedule_retry(
        self,
        event_id,
        lease_id,
        error_code,
        error_message,
        delay_seconds=60,
        result=None,
    ):
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError, OverflowError):
            raise MaterialStatusError(
                "invalid_retry_delay",
                "delay_seconds 必须为 0 至 86400 的整数",
                400,
            ) from None
        if isinstance(delay_seconds, bool) or not 0 <= delay <= 86400:
            raise MaterialStatusError(
                "invalid_retry_delay",
                "delay_seconds 必须为 0 至 86400 的整数",
                400,
            )
        safe_result = sanitize_result_details(result)
        safe_code = _safe_error_code(error_code)
        safe_message = redact_sensitive_text(error_message, limit=500)
        now_dt = _utc_datetime(self.clock)
        now = _utc_iso(now_dt)
        retry_at = _utc_iso(now_dt + timedelta(seconds=delay))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._processing_row(conn, event_id, lease_id)
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "dead_letter" if exhausted else "retry"
            conn.execute(
                """
                UPDATE material_status_broadcast_outbox
                SET status=?,
                    next_attempt_at=?,
                    lease_id='',
                    lease_expires_at='',
                    result_json=?,
                    last_error_code=?,
                    last_error_message=?,
                    dead_lettered_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    retry_at,
                    json.dumps(
                        safe_result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    safe_code,
                    safe_message,
                    now if exhausted else "",
                    now,
                    row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()
            conn.commit()
        return _decode_row(updated)

    def mark_delivered(
        self,
        event_id,
        lease_id,
        delivery_kind="private",
        result=None,
        metadata=None,
    ):
        kind = str(delivery_kind or "").strip()
        if kind not in ("private", "fallback"):
            raise MaterialStatusError(
                "invalid_delivery_kind",
                "delivery_kind 必须为 private 或 fallback",
                400,
            )
        if result is not None and metadata is not None:
            raise MaterialStatusError(
                "invalid_result_details",
                "result 与 metadata 不能同时提供",
                400,
            )
        safe_result = sanitize_result_details(
            result if result is not None else metadata
        )
        now = _utc_iso(_utc_datetime(self.clock))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._processing_row(conn, event_id, lease_id)
            conn.execute(
                """
                UPDATE material_status_broadcast_outbox
                SET status='delivered',
                    delivery_kind=?,
                    result_json=?,
                    lease_id='',
                    lease_expires_at='',
                    last_error_code='',
                    last_error_message='',
                    delivered_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    kind,
                    json.dumps(
                        safe_result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                    row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()
            conn.commit()
        return _decode_row(updated)

    def mark_dead_letter(
        self,
        event_id,
        lease_id,
        error_code,
        error_message,
        result=None,
    ):
        safe_result = sanitize_result_details(result)
        safe_code = _safe_error_code(error_code)
        safe_message = redact_sensitive_text(error_message, limit=500)
        now = _utc_iso(_utc_datetime(self.clock))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._processing_row(conn, event_id, lease_id)
            conn.execute(
                """
                UPDATE material_status_broadcast_outbox
                SET status='dead_letter',
                    result_json=?,
                    lease_id='',
                    lease_expires_at='',
                    last_error_code=?,
                    last_error_message=?,
                    dead_lettered_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(
                        safe_result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    safe_code,
                    safe_message,
                    now,
                    now,
                    row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM material_status_broadcast_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()
            conn.commit()
        return _decode_row(updated)

    # Compact aliases for worker code while keeping explicit names above.
    claim = claim_next
    retry = schedule_retry
    delivered = mark_delivered
    dead_letter = mark_dead_letter

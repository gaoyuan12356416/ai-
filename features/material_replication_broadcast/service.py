"""Pure validation, frozen message rendering, and the replication outbox.

No network operations belong here.  A worker resolves one private recipient,
freezes it, and durably calls ``begin_send`` before performing network I/O.
Rows returned by this module are INTERNAL: recipient identifiers must never
be copied into public API responses, logs, or diagnostic messages.
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

from features.material_status_broadcast.service import (
    MaterialStatusError as ReplicationError,
    redact_sensitive_text,
    validate_bearer_authorization,
    validate_idempotency_key,
)


MAX_REQUEST_BYTES = 32768
MAX_FEISHU_REQUEST_BYTES = 131072
MAX_MESSAGE_BYTES = MAX_FEISHU_REQUEST_BYTES
SAFE_RETRY_WINDOW_SECONDS = 3300
SEND_LEASE_SECONDS = 300
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
OUTBOX_TABLE = "material_replication_broadcast_outbox"
PAYLOAD_FIELDS = ("event_type", "editor_username", "items")
ITEM_FIELDS = (
    "resource_id", "resource_name", "original_material_id",
    "original_material_name",
)
EVENT_TYPES = ("replication_started", "replication_failed")
REPLICATION_LANGUAGES = (
    "西班牙语", "法语", "阿拉伯语", "俄语", "葡萄牙语", "日语", "繁体中文",
    "泰语", "印度尼西亚语", "德语", "越南语", "意大利语", "土耳其语",
    "波兰语", "罗马尼亚语", "捷克语", "韩语",
)
_MAX_SQLITE_ID = 9223372036854775807
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_FORBIDDEN_TEXT_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))
_FALLBACK_REASON_LABELS = {
    "editor_username_missing": "接口未提供剪辑用户名",
    "editor_name_missing": "接口未提供剪辑用户名",
    "optimizer_name_missing": "接口未提供剪辑用户名",
    "admin_user_not_found": "剪辑用户名未匹配到 admin_users.username",
    "editor_not_found": "剪辑用户名未匹配到 admin_users.username",
    "optimizer_not_found": "剪辑用户名未匹配到 admin_users.username",
    "editor_ambiguous": "剪辑用户名匹配到多个 admin_users 用户",
    "optimizer_ambiguous": "剪辑用户名匹配到多个 admin_users 用户",
    "email_missing": "admin_user_group 未配置可用邮箱",
    "editor_email_missing": "admin_user_group 未配置可用邮箱",
    "optimizer_email_missing": "admin_user_group 未配置可用邮箱",
    "editor_email_ambiguous": "admin_user_group 存在多个不同邮箱",
    "optimizer_email_ambiguous": "admin_user_group 存在多个不同邮箱",
    "feishu_user_not_found": "邮箱未匹配到飞书用户",
    "feishu_user_ambiguous": "邮箱匹配到多个飞书用户",
    "private_send_failed": "剪辑用户已匹配，但飞书私聊发送失败",
    "message_send_failed": "私聊投递已明确失败，请检查机器人可用范围及消息发送权限",
    "user_lookup_failed": "飞书用户查询失败，请检查应用通讯录查询权限及可用范围",
    "user_lookup_invalid_response": "飞书用户查询返回异常数据，请联系管理员检查用户查询接口",
    "feishu_lookup_unavailable": "飞书用户查询服务暂不可用，请检查网络连接和飞书应用凭据",
    "feishu_user_lookup_unavailable": "飞书用户查询服务暂不可用，请检查网络连接和飞书应用凭据",
    "feishu_receive_id_missing": "未取得有效的飞书收件人标识，请检查剪辑用户邮箱与飞书用户映射",
    "feishu_message_uuid_invalid": "消息幂等标识无效，请联系管理员检查播报消息生成逻辑",
    "feishu_send_unavailable": "飞书发送结果暂无法确认，请联系管理员核查消息投递记录",
    "message_send_invalid_response": "飞书未返回有效的投递确认，请联系管理员核查消息投递记录",
    "message_send_unknown": "飞书发送结果暂无法确认，请联系管理员核查消息投递记录",
    "delivery_failed": "飞书投递失败，请联系管理员检查投递配置和应用权限",
    "mapping_unavailable": "剪辑用户映射服务暂时不可用",
    "mapping_not_configured": "剪辑用户映射服务未配置",
    "mapping_invalid": "剪辑用户映射数据无效",
    "internal_error": "播报处理发生内部异常",
}


def _invalid(message):
    raise ReplicationError("invalid_payload", message, 422)


def _exact_fields(value, expected, label):
    if not isinstance(value, dict):
        _invalid("%s 必须为 JSON 对象" % label)
    missing = set(expected).difference(value)
    unknown = set(value).difference(expected)
    if missing:
        _invalid("%s 缺少字段: %s" % (label, ", ".join(sorted(missing))))
    if unknown:
        # Do not reflect untrusted field names, which may themselves be secrets.
        _invalid("%s 包含未知字段" % label)


def _text(value, field, limit, allow_empty=False):
    if not isinstance(value, str):
        _invalid("%s 必须为字符串" % field)
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in _FORBIDDEN_TEXT_CATEGORIES
           for char in normalized):
        _invalid("%s 只能包含单行可显示文本" % field)
    normalized = normalized.strip()
    if not normalized and not allow_empty:
        _invalid("%s 不能为空" % field)
    if len(normalized) > limit:
        _invalid("%s 长度不能超过 %d 个字符" % (field, limit))
    return normalized


def normalize_payload(payload):
    """Validate the entire batch; preserve item/language order and duplicates."""

    _exact_fields(payload, PAYLOAD_FIELDS, "请求体")
    event_type = _text(payload["event_type"], "event_type", 64)
    if event_type not in EVENT_TYPES:
        _invalid("event_type 必须为 replication_started 或 replication_failed")
    editor = _text(payload["editor_username"], "editor_username", 100, True)
    items = payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        _invalid("items 必须为包含 1 至 50 项的数组")
    expected = ITEM_FIELDS + (("failed_languages",) if event_type == "replication_failed" else ())
    normalized_items = []
    for index, raw in enumerate(items):
        label = "items[%d]" % index
        _exact_fields(raw, expected, label)
        item = {}
        for field in ITEM_FIELDS:
            limit = 128 if field.endswith("_id") else 255
            item[field] = _text(raw[field], "%s.%s" % (label, field), limit)
        if event_type == "replication_failed":
            languages = raw["failed_languages"]
            if not isinstance(languages, list) or not 1 <= len(languages) <= 32:
                _invalid("%s.failed_languages 必须为包含 1 至 32 项的数组" % label)
            item["failed_languages"] = [
                _text(language, "%s.failed_languages[%d]" % (label, number), 100)
                for number, language in enumerate(languages)
            ]
        normalized_items.append(item)
    return {"event_type": event_type, "editor_username": editor, "items": normalized_items}


def canonical_payload_json(payload):
    return json.dumps(normalize_payload(payload), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def payload_hash(payload):
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReplicationError("invalid_batch_id", "批次编号无效", 400)
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ReplicationError("invalid_batch_id", "批次编号无效", 400)
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise ReplicationError("invalid_batch_id", "批次编号无效", 400) from None
    if not 1 <= number <= _MAX_SQLITE_ID:
        raise ReplicationError("invalid_batch_id", "批次编号无效", 400)
    return number


def format_batch_id(batch_id):
    return "MRB-%010d" % _positive_id(batch_id)


def format_private_message(payload, batch_id=None):
    """Render one batch, never one message per material or per language."""

    payload = normalize_payload(payload)
    started = payload["event_type"] == "replication_started"
    lines = [
        "【自动复刻任务已发起】" if started else "【自动复刻失败】",
        "",
        "以下素材已自动发起复刻任务：" if started else "以下素材自动复刻失败：",
    ]
    for index, item in enumerate(payload["items"], 1):
        lines.extend([
            "", "%d. 资源ID：%s" % (index, item["resource_id"]),
            "   资源名：%s" % item["resource_name"],
            "   原始素材ID：%s" % item["original_material_id"],
            "   原始素材名：%s" % item["original_material_name"],
        ])
        if not started:
            lines.append("   失败语种：%s" % "、".join(item["failed_languages"]))
    if started:
        lines.extend(["", "素材语种包含：%s。" % "、".join(REPLICATION_LANGUAGES)])
    else:
        lines.extend(["", "备注：复刻失败一般是算法失败，重试基本也不会成功。"])
    if batch_id is not None:
        lines.extend(["", "批次编号：%s" % format_batch_id(batch_id)])
    return "\n".join(lines)


def _safe_code(code):
    text = str(code or "delivery_error")
    safe = redact_sensitive_text(text, limit=500)
    if safe != text or not _SAFE_CODE_RE.fullmatch(text):
        return "delivery_error"
    return text


def _safe_diagnostic(value, limit=500, targets=()):
    text = str(value or "")
    for target in targets:
        if target:
            text = text.replace(str(target), "[recipient redacted]")
    text = "".join(
        " " if unicodedata.category(char) in _FORBIDDEN_TEXT_CATEGORIES else char
        for char in text
    )
    return redact_sensitive_text(text, limit=limit)


def format_fallback_message(private_text, reason_code="editor_not_found", reason_text="",
                            editor_username=""):
    """Prefix the already-frozen private body without ever re-rendering it."""

    if not isinstance(private_text, str) or not private_text:
        raise ReplicationError("invalid_private_text", "冻结的私聊消息无效", 400)
    code = _safe_code(reason_code)
    reason = _FALLBACK_REASON_LABELS.get(code) or _safe_diagnostic(reason_text, 200)
    reason = reason or "未匹配到对应剪辑用户"
    username = _safe_diagnostic(editor_username, 100) or "（空）"
    return "\n".join([
        "【⚠️ 自动复刻播报未能私聊】", "",
        "收到的 username：%s" % username,
        "失败原因：%s" % code, "说明：%s" % reason, "", private_text,
    ])


def _serialized_request_size(message, receive_id, message_uuid):
    # Match requests' json= encoding: inner Unicode content, then outer
    # ensure_ascii=True and the default separators (including their spaces).
    content = json.dumps({"text": message}, ensure_ascii=False)
    request = {"receive_id": receive_id, "msg_type": "text", "content": content,
               "uuid": message_uuid}
    return len(json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def validate_message_size(payload_or_private_text):
    """Reject before insertion using the largest permitted fallback envelope.

    Non-BMP characters require twelve ASCII bytes in requests' outer JSON.
    Reserve the full 128-character receiver, 50-character UUID, 64-character
    reason code, and 200-character reason, plus the maximum SQLite batch ID.
    """

    if isinstance(payload_or_private_text, dict):
        private_text = format_private_message(payload_or_private_text, _MAX_SQLITE_ID)
    elif isinstance(payload_or_private_text, str):
        private_text = payload_or_private_text
        try:
            private_text.encode("utf-8")
        except UnicodeEncodeError:
            _invalid("消息包含无效的 Unicode 字符")
    else:
        _invalid("消息必须为有效请求体或冻结文本")
    if isinstance(payload_or_private_text, dict):
        maximum_username = payload_or_private_text["editor_username"]
    else:
        maximum_username = "\U0001f680" * 100
    maximum_text = format_fallback_message(
        private_text, "r" * 64, "\U0001f680" * 200, maximum_username,
    )
    size = _serialized_request_size(maximum_text, "\U0001f680" * 128, "u" * 50)
    if size > MAX_FEISHU_REQUEST_BYTES:
        raise ReplicationError("message_too_large", "飞书消息超过大小限制，请减少批次素材或语种数量", 413)
    return size


def _utc_datetime(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_int(value, minimum, maximum, code, label):
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or (isinstance(value, str) and not re.fullmatch(r"[0-9]+", value))):
        raise ReplicationError(code, "%s 必须为 %d 至 %d 的整数" % (label, minimum, maximum), 400)
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise ReplicationError(code, "%s 无效" % label, 400) from None
    if not minimum <= number <= maximum:
        raise ReplicationError(code, "%s 必须为 %d 至 %d 的整数" % (label, minimum, maximum), 400)
    return number


def _target(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or any(char.isspace() or unicodedata.category(char) in _FORBIDDEN_TEXT_CATEGORIES
                   for char in value)):
        raise ReplicationError("invalid_receive_id", "投递目标标识无效", 400)
    return value


def _audit_ip(value):
    try:
        return ipaddress.ip_address(str(value or "").strip()).compressed
    except ValueError:
        return ""


def _connect(path):
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


@contextlib.contextmanager
def _transaction(path):
    with contextlib.closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def ensure_storage(db_path):
    """Add only the replication table/indexes; do not migrate legacy queues."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _transaction(path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS material_replication_broadcast_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_ip TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','processing','retry','delivered',
                                     'dead_letter','delivery_unknown')),
                phase TEXT NOT NULL DEFAULT 'private' CHECK(phase IN ('private','fallback')),
                private_text TEXT NOT NULL DEFAULT '',
                private_receive_id TEXT NOT NULL DEFAULT '',
                private_uuid TEXT NOT NULL DEFAULT '',
                fallback_text TEXT NOT NULL DEFAULT '',
                fallback_receive_id TEXT NOT NULL DEFAULT '',
                fallback_uuid TEXT NOT NULL DEFAULT '',
                fallback_reason_code TEXT NOT NULL DEFAULT '',
                fallback_reason_text TEXT NOT NULL DEFAULT '',
                uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0,1)),
                first_uncertain_at TEXT NOT NULL DEFAULT '',
                send_previous_uncertain INTEGER NOT NULL DEFAULT 0
                    CHECK(send_previous_uncertain IN (0,1)),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
                max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts BETWEEN 1 AND 20),
                next_attempt_at TEXT NOT NULL,
                lease_id TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                delivery_kind TEXT NOT NULL DEFAULT '',
                feishu_message_id TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                delivered_at TEXT NOT NULL DEFAULT '',
                dead_lettered_at TEXT NOT NULL DEFAULT '',
                delivery_unknown_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_material_replication_due
            ON material_replication_broadcast_outbox(status,next_attempt_at,id)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_material_replication_lease
            ON material_replication_broadcast_outbox(status,lease_expires_at)
        """)


def _decode_row(row):
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["uncertain"] = bool(item["uncertain"])
    item["send_previous_uncertain"] = bool(item["send_previous_uncertain"])
    phase = item["phase"]
    item["receive_id"] = item[phase + "_receive_id"]
    item["message_uuid"] = item[phase + "_uuid"]
    item["message_text"] = item[phase + "_text"]
    item["batch_id"] = format_batch_id(item["id"])
    item["message_id"] = item["feishu_message_id"]
    return item


class ReplicationOutbox:
    """Separate durable idempotency, pinned recipients, and fail-closed leases."""

    def __init__(self, db_path, clock=None):
        self.db_path = Path(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        ensure_storage(self.db_path)

    @staticmethod
    def _row(connection, batch_id):
        return connection.execute(
            "SELECT * FROM material_replication_broadcast_outbox WHERE id=?",
            (_positive_id(batch_id),),
        ).fetchone()

    def get(self, batch_id):
        with contextlib.closing(_connect(self.db_path)) as connection:
            return _decode_row(self._row(connection, batch_id))

    def enqueue(self, key, payload, source_ip="", max_attempts=5):
        key = validate_idempotency_key(key)
        normalized = normalize_payload(payload)
        canonical = canonical_payload_json(normalized)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        attempts = _bounded_int(max_attempts, 1, 20, "invalid_max_attempts", "max_attempts")
        with _transaction(self.db_path) as connection:
            existing = connection.execute(
                "SELECT * FROM material_replication_broadcast_outbox WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["payload_hash"], digest):
                    raise ReplicationError("idempotency_conflict", "同一 Idempotency-Key 已对应不同请求体", 409)
                item = _decode_row(existing)
                item["created"] = False
                return item
            # Existing keys retain their original frozen body even if a later
            # template changes. Preflight only NEW batches, before any insert.
            validate_message_size(normalized)
            now = _iso(_utc_datetime(self.clock))
            cursor = connection.execute("""
                INSERT INTO material_replication_broadcast_outbox(
                    idempotency_key,payload_hash,payload_json,source_ip,max_attempts,
                    next_attempt_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (key, digest, canonical, _audit_ip(source_ip), attempts, now, now, now))
            batch_id = cursor.lastrowid
            private_text = format_private_message(normalized, batch_id)
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET private_text=?,private_uuid=? WHERE id=?
            """, (private_text, "mrb-%d-private" % batch_id, batch_id))
            item = _decode_row(self._row(connection, batch_id))
            item["created"] = True
            return item

    def claim_next(self, lease_seconds=SEND_LEASE_SECONDS):
        seconds = _bounded_int(lease_seconds, 5, 3600, "invalid_lease", "lease_seconds")
        with _transaction(self.db_path) as connection:
            now_dt = _utc_datetime(self.clock)
            now = _iso(now_dt)
            cutoff = _iso(now_dt - timedelta(seconds=SAFE_RETRY_WINDOW_SECONDS))
            # Never steal a live lease. Reconcile exhausted/crashed work before
            # selecting a new attempt, including retries whose due time is later.
            inactive = """(status IN ('queued','retry') OR
                (status='processing' AND lease_expires_at!='' AND lease_expires_at<=?))"""
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET status='delivery_unknown',lease_id='',lease_expires_at='',
                    delivery_unknown_at=?,updated_at=?,last_error_code=CASE
                        WHEN first_uncertain_at='' OR first_uncertain_at<=?
                        THEN 'delivery_window_expired' ELSE 'attempts_exhausted' END,
                    last_error_message='发送结果无法确认，已停止自动投递和群兜底'
                WHERE """ + inactive + """ AND uncertain=1
                  AND (attempt_count>=max_attempts OR first_uncertain_at=''
                       OR first_uncertain_at<=?)
            """, (now, now, cutoff, now, cutoff))
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET status='dead_letter',lease_id='',lease_expires_at='',
                    dead_lettered_at=?,updated_at=?,
                    last_error_code=CASE WHEN last_error_code=''
                        THEN 'attempts_exhausted' ELSE last_error_code END,
                    last_error_message=CASE WHEN last_error_message=''
                        THEN '处理次数已耗尽，且没有未确认发送' ELSE last_error_message END
                WHERE """ + inactive + """ AND uncertain=0 AND attempt_count>=max_attempts
            """, (now, now, now))
            row = connection.execute("""
                SELECT * FROM material_replication_broadcast_outbox
                WHERE attempt_count<max_attempts AND (
                    (status IN ('queued','retry') AND next_attempt_at<=?) OR
                    (status='processing' AND lease_expires_at!='' AND lease_expires_at<=?)
                ) ORDER BY next_attempt_at ASC,id ASC LIMIT 1
            """, (now, now)).fetchone()
            if row is None:
                return None
            lease_id = secrets.token_hex(16)
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET status='processing',attempt_count=attempt_count+1,
                    lease_id=?,lease_expires_at=?,claimed_at=?,updated_at=?,
                    send_previous_uncertain=uncertain
                WHERE id=?
            """, (lease_id, _iso(now_dt + timedelta(seconds=seconds)), now, now, row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    def _processing_row(self, connection, batch_id, lease_id):
        now_dt = _utc_datetime(self.clock)
        row = self._row(connection, batch_id)
        if (row is None or row["status"] != "processing" or not isinstance(lease_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", lease_id)
                or not hmac.compare_digest(row["lease_id"], lease_id)
                or not row["lease_expires_at"] or row["lease_expires_at"] <= _iso(now_dt)):
            raise ReplicationError("outbox_lease_conflict", "批次不存在、未处理中或租约已失效", 409)
        return row, now_dt

    @staticmethod
    def _uncertainty_expired(row, now_dt):
        return bool(row["uncertain"]) and (
            not row["first_uncertain_at"] or row["first_uncertain_at"] <= _iso(
                now_dt - timedelta(seconds=SAFE_RETRY_WINDOW_SECONDS)))

    def freeze_target(self, batch_id, lease_id, receive_id):
        receive_id = _target(receive_id)
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            if row["phase"] != "private":
                raise ReplicationError("outbox_phase_conflict", "批次已冻结为群兜底阶段", 409)
            if row["private_receive_id"] and row["private_receive_id"] != receive_id:
                raise ReplicationError("target_frozen", "私聊投递目标已冻结，禁止重新绑定", 409)
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET private_receive_id=?,updated_at=? WHERE id=?
            """, (receive_id, _iso(now_dt), row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    def begin_send(self, batch_id, lease_id):
        expired = False
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            phase = row["phase"]
            if not row[phase + "_receive_id"] or not row[phase + "_uuid"] or not row[phase + "_text"]:
                raise ReplicationError("target_not_frozen", "发送前必须冻结投递目标和消息", 409)
            if self._uncertainty_expired(row, now_dt):
                self._terminal(connection, row, now_dt, "delivery_unknown",
                               "delivery_window_expired", "发送确认窗口已过，已停止自动投递和群兜底")
                expired = True
            else:
                previous_uncertain = bool(row["uncertain"])
                first = row["first_uncertain_at"] if previous_uncertain else _iso(now_dt)
                connection.execute("""
                    UPDATE material_replication_broadcast_outbox
                    SET uncertain=1,first_uncertain_at=?,send_previous_uncertain=?,
                        lease_expires_at=?,updated_at=? WHERE id=?
                """, (first, int(previous_uncertain),
                      _iso(now_dt + timedelta(seconds=SEND_LEASE_SECONDS)), _iso(now_dt), row["id"]))
                result = _decode_row(self._row(connection, row["id"]))
                result["previous_uncertain"] = previous_uncertain
        if expired:
            # Raise only AFTER committing the terminal state.
            raise ReplicationError("delivery_window_expired", "发送确认窗口已过，已停止自动投递和群兜底", 409)
        return result

    def clear_known_failure(self, batch_id, lease_id, previous_uncertain):
        if not isinstance(previous_uncertain, bool):
            raise ReplicationError("invalid_uncertainty", "发送确认标记必须为布尔值", 400)
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            # Persisted history protects against a stale/incorrect caller flag.
            if not previous_uncertain and not row["send_previous_uncertain"]:
                connection.execute("""
                    UPDATE material_replication_broadcast_outbox
                    SET uncertain=0,first_uncertain_at='',updated_at=? WHERE id=?
                """, (_iso(now_dt), row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    def prepare_fallback(self, batch_id, lease_id, receive_id, reason_code, reason_text=""):
        receive_id = _target(receive_id)
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            if row["uncertain"]:
                raise ReplicationError("outbox_uncertain", "私聊发送结果尚未确认，禁止群兜底", 409)
            if row["phase"] != "private":
                raise ReplicationError("outbox_phase_conflict", "群兜底已冻结，禁止重置重试次数或投递目标", 409)
            targets = (row["private_receive_id"], receive_id)
            code = _safe_code(reason_code)
            if any(target and target in code for target in targets):
                code = "delivery_error"
            reason = _safe_diagnostic(reason_text, 200, targets)
            payload = json.loads(row["payload_json"])
            fallback = format_fallback_message(
                row["private_text"], code, reason, payload.get("editor_username", ""),
            )
            message_uuid = "mrb-%d-fallback" % row["id"]
            if _serialized_request_size(fallback, receive_id, message_uuid) > MAX_FEISHU_REQUEST_BYTES:
                raise ReplicationError("message_too_large", "飞书消息超过大小限制", 413)
            now = _iso(now_dt)
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET phase='fallback',fallback_text=?,fallback_receive_id=?,fallback_uuid=?,
                    fallback_reason_code=?,fallback_reason_text=?,status='queued',
                    attempt_count=0,next_attempt_at=?,lease_id='',lease_expires_at='',
                    send_previous_uncertain=0,last_error_code=?,last_error_message=?,updated_at=?
                WHERE id=?
            """, (fallback, receive_id, message_uuid, code, reason, now, code, reason, now, row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    @staticmethod
    def _safe_errors(row, code, message):
        targets = (row["private_receive_id"], row["fallback_receive_id"])
        safe_code = _safe_code(code)
        if any(target and target in safe_code for target in targets):
            safe_code = "delivery_error"
        return safe_code, _safe_diagnostic(message, 500, targets)

    def _terminal(self, connection, row, now_dt, status, code, message):
        code, message = self._safe_errors(row, code, message)
        if status == "dead_letter" and row["uncertain"]:
            status = "delivery_unknown"
        now = _iso(now_dt)
        unknown = status == "delivery_unknown"
        connection.execute("""
            UPDATE material_replication_broadcast_outbox
            SET status=?,lease_id='',lease_expires_at='',last_error_code=?,last_error_message=?,
                dead_lettered_at=?,delivery_unknown_at=?,updated_at=?,uncertain=?,first_uncertain_at=?
            WHERE id=?
        """, (status, code, message, "" if unknown else now, now if unknown else "", now,
              1 if unknown else int(row["uncertain"]),
              (row["first_uncertain_at"] or now) if unknown else row["first_uncertain_at"], row["id"]))

    def retry(self, batch_id, lease_id, code, message, delay_seconds=60):
        delay = _bounded_int(delay_seconds, 0, 86400, "invalid_retry_delay", "delay_seconds")
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            exhausted = row["attempt_count"] >= row["max_attempts"]
            expired = self._uncertainty_expired(row, now_dt)
            if exhausted or expired:
                status = "delivery_unknown" if row["uncertain"] else "dead_letter"
                self._terminal(connection, row, now_dt, status, code, message)
            else:
                safe_code, safe_message = self._safe_errors(row, code, message)
                connection.execute("""
                    UPDATE material_replication_broadcast_outbox
                    SET status='retry',next_attempt_at=?,lease_id='',lease_expires_at='',
                        last_error_code=?,last_error_message=?,updated_at=? WHERE id=?
                """, (_iso(now_dt + timedelta(seconds=delay)), safe_code, safe_message,
                      _iso(now_dt), row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    def delivered(self, batch_id, lease_id, message_id):
        if (not isinstance(message_id, str) or not _SAFE_MESSAGE_ID_RE.fullmatch(message_id)
                or redact_sensitive_text(message_id, limit=500) != message_id):
            raise ReplicationError("invalid_message_id", "飞书消息编号无效", 400)
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            if not row[row["phase"] + "_receive_id"]:
                raise ReplicationError("target_not_frozen", "确认投递前必须冻结投递目标", 409)
            if any(target and target in message_id for target in
                   (row["private_receive_id"], row["fallback_receive_id"])):
                raise ReplicationError("invalid_message_id", "飞书消息编号无效", 400)
            now = _iso(now_dt)
            connection.execute("""
                UPDATE material_replication_broadcast_outbox
                SET status='delivered',delivery_kind=phase,feishu_message_id=?,
                    uncertain=0,lease_id='',lease_expires_at='',last_error_code='',
                    last_error_message='',delivered_at=?,updated_at=? WHERE id=?
            """, (message_id, now, now, row["id"]))
            return _decode_row(self._row(connection, row["id"]))

    def dead_letter(self, batch_id, lease_id, code, message):
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            self._terminal(connection, row, now_dt, "dead_letter", code, message)
            return _decode_row(self._row(connection, row["id"]))

    def unknown(self, batch_id, lease_id, code, message):
        with _transaction(self.db_path) as connection:
            row, now_dt = self._processing_row(connection, batch_id, lease_id)
            self._terminal(connection, row, now_dt, "delivery_unknown", code, message)
            return _decode_row(self._row(connection, row["id"]))

    claim = claim_next

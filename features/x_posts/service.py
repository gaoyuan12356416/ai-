"""Queue, redirect, media and X API support for a single X Post canary.

The caller must invoke :func:`publish_canary` while the X account sidecar holds
``publish_credentials(...)``.  This module never persists or returns an access
token and all HTTP collaborators are injectable for offline tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2116/view"
X_API_BASE_URL = "https://api.x.com"
DEFAULT_PUBLIC_ROOT = "/mnt/data-disk/x-post-automation/s2l"
DEFAULT_SHORT_BASE_URL = "https://ai.yingliangads.com/s2l"
DEFAULT_MAX_MEDIA_BYTES = 512 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024

QUEUE_FIELDS = (
    "account_id",
    "account_username",
    "source_date",
    "material_id",
    "content_id",
    "material_url",
    "material_name",
    "material_language",
    "drama_name",
    "tag",
    "description",
    "page_name",
    "page_id",
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(access_token|refresh_token|client_secret|code_verifier|authorization)"
        r"(\s*[:=]\s*)([^\s,;&]+)"
    ),
)


class XPostError(RuntimeError):
    """Stable, secret-safe error passed across the internal sidecar boundary."""

    def __init__(self, code, message, status=400, unknown_outcome=False):
        self.code = _clean_token(code, "error code", 64)
        self.status = int(status or 400)
        self.unknown_outcome = bool(unknown_outcome)
        super().__init__(redact_text(message, 500))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact_text(value, limit=1000):
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)\\bbearer"):
            text = pattern.sub("Bearer [redacted]", text)
        else:
            text = pattern.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", text)
    return text.strip()[: max(1, int(limit))]


def _clean_token(value, label, limit=128):
    value = str(value or "").strip()
    if not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError("invalid %s" % label)
    return value


def _clean_text(value, label, limit=500, forbidden=""):
    value = str(value or "").strip()
    if not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    if any(char in value for char in forbidden):
        raise XPostError("invalid_request", "%s包含保留分隔符" % label, 400)
    return value


def _positive_int(value, label):
    if isinstance(value, bool):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise XPostError("invalid_request", "%s无效" % label, 400) from None
    if parsed <= 0 or parsed > 9223372036854775807:
        raise XPostError("invalid_request", "%s无效" % label, 400)
    return parsed


def build_w2a_url(params):
    """Build the exact Dramawave W2A attribution URL with fixed field order."""
    if not isinstance(params, dict):
        raise XPostError("invalid_request", "W2A参数必须是对象", 400)
    required = {
        "username", "timestamp", "material_language", "drama_name", "tag",
        "log_id", "page_name", "page_id", "material_name", "material_id",
        "queue_id", "content_id",
    }
    missing = sorted(required.difference(params))
    unknown = sorted(set(params).difference(required))
    if missing or unknown:
        raise XPostError("invalid_request", "W2A参数字段不完整或包含未知字段", 400)

    username = str(params["username"] or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
        raise XPostError("invalid_request", "X用户名无效", 400)
    timestamp = _positive_int(params["timestamp"], "时间戳")
    log_id = _positive_int(params["log_id"], "日志ID")
    language = _clean_text(params["material_language"], "素材语言", 32, "*[]")
    drama_name = _clean_text(params["drama_name"], "剧名", 255, "*[]")
    tag = _clean_text(params["tag"], "标签", 255, "*[]")
    page_name = _clean_text(params["page_name"], "page名", 255)
    page_id = _clean_token(params["page_id"], "page_id", 64)
    material_name = _clean_text(params["material_name"], "素材名", 255)
    material_id = _clean_token(params["material_id"], "素材ID", 128)
    queue_id = _positive_int(params["queue_id"], "队列ID")
    content_id = _clean_token(params["content_id"], "content_id", 128)

    campaign = "yingliang_post_CLV_VL_%s*%snone%s*%s*%s*%s" % (
        username, timestamp, language, drama_name, tag, log_id,
    )
    query = urllib.parse.urlencode(
        (
            ("c", campaign),
            ("af_adset", page_name),
            ("af_adset_id", page_id),
            ("af_ad", "%s_contentid[%s]" % (material_name, content_id)),
            ("af_ad_id", material_id),
            ("af_channel", "AIpost"),
            ("af_c_id", str(queue_id)),
            ("af_dp", content_id),
        ),
        quote_via=urllib.parse.quote,
        safe="*",
    )
    return W2A_BASE_URL + "?" + query


def _validate_w2a_url(url):
    parsed = urllib.parse.urlsplit(str(url or ""))
    base = urllib.parse.urlsplit(W2A_BASE_URL)
    if (
        parsed.scheme != "https" or parsed.hostname != base.hostname or parsed.port is not None
        or parsed.username is not None or parsed.password is not None or parsed.path != base.path
        or parsed.fragment
    ):
        raise XPostError("invalid_short_link_target", "短链目标不是允许的W2A地址", 400)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    expected = ["c", "af_adset", "af_adset_id", "af_ad", "af_ad_id", "af_channel", "af_c_id", "af_dp"]
    if [key for key, _value in pairs] != expected or any(not value for _key, value in pairs):
        raise XPostError("invalid_short_link_target", "W2A参数不完整", 400)
    if dict(pairs).get("af_channel") != "AIpost":
        raise XPostError("invalid_short_link_target", "W2A渠道无效", 400)
    return str(url)


def _build_short_url(short_base_url, log_id):
    log_id = _positive_int(log_id, "日志ID")
    parsed = urllib.parse.urlsplit(str(short_base_url or "").rstrip("/"))
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
    ):
        raise XPostError("invalid_short_base_url", "短链基础地址无效", 500)
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return "%s/%s.html" % (base, log_id)


def build_post_text(short_url, description):
    parsed = urllib.parse.urlsplit(str(short_url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise XPostError("invalid_request", "短链无效", 400)
    description = str(description or "").strip()
    if not description or "\x00" in description or len(description) > 10000:
        raise XPostError("invalid_request", "剧描述无效", 400)
    # X shortens an HTTPS URL to a fixed t.co length.  The description uses a
    # conservative subset of twitter-text weighting: common Latin/punctuation
    # is weight 1 and every other code point is weight 2.  This can under-use a
    # few characters, but will not knowingly exceed the 280 weighted limit.
    remaining = 280 - 23 - 1  # complete first-line URL plus newline

    def char_weight(char):
        value = ord(char)
        if value <= 0x10FF or 0x2000 <= value <= 0x200D or 0x2010 <= value <= 0x201F or 0x2032 <= value <= 0x2037:
            return 1
        return 2

    total = sum(char_weight(char) for char in description)
    if total <= remaining:
        rendered = description
    else:
        ellipsis = "…"
        budget = remaining - char_weight(ellipsis)
        selected = []
        used = 0
        for char in description:
            weight = char_weight(char)
            if used + weight > budget:
                break
            selected.append(char)
            used += weight
        rendered = "".join(selected).rstrip() + ellipsis
    if not rendered.strip():
        raise XPostError("invalid_request", "剧描述截断后为空", 400)
    return str(short_url) + "\n" + rendered


def write_short_redirect(public_root, log_id, long_url):
    """Atomically create an immutable ``<log_id>.html`` redirect page."""
    target = _validate_w2a_url(long_url)
    log_id = _positive_int(log_id, "日志ID")
    configured_root = Path(public_root).expanduser()
    if configured_root.exists() and configured_root.is_symlink():
        raise XPostError("short_link_write_failed", "短链目录不能是符号链接", 500)
    configured_root.mkdir(parents=True, exist_ok=True)
    root = configured_root.resolve()
    if not root.is_dir():
        raise XPostError("short_link_write_failed", "短链目录无效", 500)
    try:
        os.chmod(root, 0o755)
    except OSError as exc:
        raise XPostError("short_link_write_failed", "短链目录权限设置失败: %s" % exc, 500) from None
    destination = root / (str(log_id) + ".html")
    escaped = html.escape(target, quote=True)
    js_target = json.dumps(target, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    payload = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<meta http-equiv=\"Cache-Control\" content=\"no-store\">"
        "<meta http-equiv=\"refresh\" content=\"0;url=%s\">"
        "<link rel=\"canonical\" href=\"%s\"><title>Redirecting</title>"
        "<script>location.replace(%s);</script></head>"
        "<body><a rel=\"noreferrer\" href=\"%s\">Continue</a></body></html>\n"
        % (escaped, escaped, js_target, escaped)
    ).encode("utf-8")
    if destination.exists():
        if destination.is_symlink():
            raise XPostError("short_link_conflict", "短链文件不能是符号链接", 409)
        try:
            if destination.is_file() and destination.read_bytes() == payload:
                try:
                    os.chmod(destination, 0o644)
                except OSError as exc:
                    raise XPostError("short_link_write_failed", "短链文件权限设置失败: %s" % exc, 500) from None
                return destination
        except OSError:
            pass
        raise XPostError("short_link_conflict", "该日志ID的短链已存在且目标不同", 409)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % destination.name, dir=str(root))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o644)
        except OSError:
            pass
        os.replace(temporary, destination)
    except OSError as exc:
        raise XPostError("short_link_write_failed", "短链页面写入失败: %s" % exc, 500) from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_storage(db_path):
    """Create only additive X Post tables and indexes; safe to call repeatedly."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS x_post_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                account_id INTEGER NOT NULL,
                account_username TEXT NOT NULL,
                source_date TEXT NOT NULL,
                material_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                material_url TEXT NOT NULL,
                material_name TEXT NOT NULL,
                material_language TEXT NOT NULL,
                drama_name TEXT NOT NULL,
                tag TEXT NOT NULL,
                description TEXT NOT NULL,
                page_name TEXT NOT NULL,
                page_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS x_post_publish_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER NOT NULL UNIQUE,
                account_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'reserved',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                long_url TEXT NOT NULL DEFAULT '',
                short_url TEXT NOT NULL DEFAULT '',
                post_text TEXT NOT NULL DEFAULT '',
                x_media_id TEXT NOT NULL DEFAULT '',
                x_post_id TEXT NOT NULL DEFAULT '',
                x_post_url TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                unknown_outcome INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(queue_id) REFERENCES x_post_queue(id)
            );
            CREATE INDEX IF NOT EXISTS idx_x_post_queue_status ON x_post_queue(status,created_at,id);
            CREATE INDEX IF NOT EXISTS idx_x_post_log_status ON x_post_publish_log(status,created_at,id);
            CREATE INDEX IF NOT EXISTS idx_x_post_log_account ON x_post_publish_log(account_id,created_at,id);
            """
        )
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(x_post_queue)")}
        if "account_username" not in queue_columns:
            conn.execute("ALTER TABLE x_post_queue ADD COLUMN account_username TEXT NOT NULL DEFAULT ''")
        conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _row_dict(row):
    return dict(row) if row is not None else None


class XPostStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        ensure_storage(self.db_path)

    def _queue_payload(self, payload):
        if not isinstance(payload, dict):
            raise XPostError("invalid_request", "发布候选必须是对象", 400)
        result = {}
        result["account_id"] = _positive_int(payload.get("account_id"), "account_id")
        username = str(payload.get("account_username", "") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
            raise XPostError("invalid_request", "account_username无效", 400)
        result["account_username"] = username
        source_date = str(payload.get("source_date", "") or "").strip()
        try:
            datetime.strptime(source_date, "%Y-%m-%d")
        except ValueError:
            raise XPostError("invalid_request", "source_date必须为YYYY-MM-DD", 400) from None
        result["source_date"] = source_date
        for field in QUEUE_FIELDS[3:]:
            limit = 4096 if field in {"material_url", "description"} else 500
            result[field] = _clean_text(payload.get(field), field, limit)
        material = urllib.parse.urlsplit(result["material_url"])
        if material.scheme != "https" or not material.hostname or material.username or material.password or material.fragment:
            raise XPostError("invalid_media_url", "素材地址必须是HTTPS URL", 400)
        default_key = "xpost:%s:%s:%s" % (source_date, result["account_id"], result["material_id"])
        key = str(payload.get("idempotency_key", "") or default_key).strip()
        if not key or len(key) > 200 or any(ord(char) < 33 for char in key):
            raise XPostError("invalid_request", "idempotency_key无效", 400)
        result["idempotency_key"] = key
        return result

    def enqueue(self, payload):
        values = self._queue_payload(payload)
        timestamp = utc_now()
        columns = ("idempotency_key",) + QUEUE_FIELDS
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_queue WHERE idempotency_key=?", (values["idempotency_key"],)
            ).fetchone()
            if existing:
                for field in columns:
                    if str(existing[field]) != str(values[field]):
                        conn.rollback()
                        raise XPostError("x_post_idempotency_conflict", "幂等键已对应其他发布候选", 409)
                conn.commit()
                item = _row_dict(existing)
                item["created"] = False
                return item
            placeholders = ",".join("?" for _field in columns)
            cursor = conn.execute(
                "INSERT INTO x_post_queue(%s,status,created_at,updated_at) VALUES(%s,'queued',?,?)"
                % (",".join(columns), placeholders),
                tuple(values[field] for field in columns) + (timestamp, timestamp),
            )
            conn.commit()
            item = self.get_queue(cursor.lastrowid)
            item["created"] = True
            return item

    def get_queue(self, queue_id):
        queue_id = _positive_int(queue_id, "queue_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
        if not row:
            raise XPostError("x_post_queue_not_found", "发布队列记录不存在", 404)
        return _row_dict(row)

    def get_log(self, log_id):
        log_id = _positive_int(log_id, "log_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
        if not row:
            raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
        return _row_dict(row)

    def reserve_log(self, queue_id):
        queue_id = _positive_int(queue_id, "queue_id")
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            queue = conn.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
            if not queue:
                conn.rollback()
                raise XPostError("x_post_queue_not_found", "发布队列记录不存在", 404)
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue_id,)).fetchone()
            created = False
            if not row:
                cursor = conn.execute(
                    "INSERT INTO x_post_publish_log(queue_id,account_id,status,created_at,updated_at) "
                    "VALUES(?,?,'reserved',?,?)",
                    (queue_id, queue["account_id"], timestamp, timestamp),
                )
                row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (cursor.lastrowid,)).fetchone()
                created = True
            conn.commit()
        item = _row_dict(row)
        item["created"] = created
        return item

    def prepare_log(self, log_id, long_url, short_url, post_text):
        log_id = _positive_int(log_id, "log_id")
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] != "reserved":
                same = row["long_url"] == long_url and row["short_url"] == short_url and row["post_text"] == post_text
                if not same:
                    conn.rollback()
                    raise XPostError("x_post_log_conflict", "发布日志已进入执行阶段", 409)
            else:
                conn.execute(
                    "UPDATE x_post_publish_log SET long_url=?,short_url=?,post_text=?,updated_at=? WHERE id=?",
                    (long_url, short_url, post_text, timestamp, log_id),
                )
            conn.commit()
        return self.get_log(log_id)

    def mark_publishing(self, log_id):
        log_id = _positive_int(log_id, "log_id")
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] == "published":
                conn.commit()
                return _row_dict(row)
            if row["status"] != "reserved":
                conn.rollback()
                code = "x_post_unknown_outcome" if row["unknown_outcome"] else "x_post_retry_requires_review"
                raise XPostError(code, "发布日志已执行，禁止自动重复发帖", 409, bool(row["unknown_outcome"]))
            if not row["long_url"] or not row["short_url"] or not row["post_text"]:
                conn.rollback()
                raise XPostError("x_post_log_not_prepared", "发布日志尚未准备完成", 409)
            conn.execute(
                "UPDATE x_post_publish_log SET status='media_uploading',attempt_count=attempt_count+1,"
                "started_at=?,error_code='',error_message='',unknown_outcome=0,updated_at=? WHERE id=?",
                (timestamp, timestamp, log_id),
            )
            conn.execute("UPDATE x_post_queue SET status='publishing',updated_at=? WHERE id=?", (timestamp, row["queue_id"]))
            conn.commit()
        return self.get_log(log_id)

    def mark_media_uploaded(self, log_id, media_id):
        media_id = _clean_token(media_id, "media id", 128)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                "UPDATE x_post_publish_log SET status='post_creating',x_media_id=?,updated_at=? "
                "WHERE id=? AND status='media_uploading'",
                (media_id, timestamp, _positive_int(log_id, "log_id")),
            )
            if cursor.rowcount != 1:
                raise XPostError("x_post_state_conflict", "发布日志状态冲突", 409)
            conn.commit()
        return self.get_log(log_id)

    def mark_published(self, log_id, media_id, post_id, post_url):
        log_id = _positive_int(log_id, "log_id")
        media_id = _clean_token(media_id, "media id", 128)
        post_id = _clean_token(post_id, "post id", 128)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] == "published":
                if row["x_post_id"] != post_id:
                    conn.rollback()
                    raise XPostError("x_post_log_conflict", "发布日志已对应其他Post", 409)
                conn.commit()
                return _row_dict(row)
            if row["status"] != "post_creating":
                conn.rollback()
                raise XPostError("x_post_state_conflict", "发布日志状态冲突", 409)
            conn.execute(
                "UPDATE x_post_publish_log SET status='published',x_media_id=?,x_post_id=?,x_post_url=?,"
                "published_at=?,error_code='',error_message='',unknown_outcome=0,updated_at=? WHERE id=?",
                (media_id, post_id, str(post_url), timestamp, timestamp, log_id),
            )
            conn.execute("UPDATE x_post_queue SET status='published',updated_at=? WHERE id=?", (timestamp, row["queue_id"]))
            conn.commit()
        return self.get_log(log_id)

    def mark_failed(self, log_id, error_code, error_message, unknown_outcome=False):
        log_id = _positive_int(log_id, "log_id")
        code = _clean_token(error_code or "x_post_failed", "error code", 64)
        message = redact_text(error_message, 500)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] == "published":
                conn.commit()
                return _row_dict(row)
            conn.execute(
                "UPDATE x_post_publish_log SET status='failed',error_code=?,error_message=?,unknown_outcome=?,updated_at=? WHERE id=?",
                (code, message, 1 if unknown_outcome else 0, timestamp, log_id),
            )
            conn.execute("UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=?", (timestamp, row["queue_id"]))
            conn.commit()
        return self.get_log(log_id)


class HttpResponse:
    """Small response wrapper used by the injectable HTTP client contract."""

    def __init__(self, status, headers=None, body=b"", stream=None):
        self.status = int(status)
        self.headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        self.body = bytes(body or b"")
        self._stream = stream

    def iter_bytes(self, chunk_size=64 * 1024):
        if self._stream is not None:
            while True:
                chunk = self._stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk
            return
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        if self._stream is not None:
            self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


class UrllibHttpClient:
    """No-redirect stdlib HTTP transport. Tests should inject a fake instead."""

    def __init__(self):
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self, method, url, headers=None, body=None, timeout=30, stream=False,
        max_response_bytes=MAX_HTTP_RESPONSE_BYTES,
    ):
        request = urllib.request.Request(str(url), data=body, method=str(method), headers=headers or {})
        try:
            response = self.opener.open(request, timeout=timeout)
            if stream:
                return HttpResponse(response.status, response.headers, stream=response)
            try:
                raw = response.read(int(max_response_bytes) + 1)
                if len(raw) > int(max_response_bytes):
                    raise XPostError("http_response_too_large", "HTTP响应过大", 502)
                return HttpResponse(response.status, response.headers, raw)
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(int(max_response_bytes) + 1)
                if len(raw) > int(max_response_bytes):
                    raw = raw[: int(max_response_bytes)]
                return HttpResponse(exc.code, exc.headers, raw)
            finally:
                exc.close()


def _allowed_host(hostname, allowed_hosts):
    hostname = str(hostname or "").lower().rstrip(".")
    for raw in allowed_hosts or ():
        allowed = str(raw or "").strip().lower().rstrip(".")
        if not allowed:
            continue
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif secrets.compare_digest(hostname, allowed):
            return True
    return False


def download_media(
    url, destination, allowed_hosts, max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout=30, http_client=None,
):
    """Download one HTTPS video after strict host, type and byte-count checks."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.fragment or parsed.port not in {None, 443}
    ):
        raise XPostError("invalid_media_url", "素材地址必须是允许域名的HTTPS URL", 400)
    if not _allowed_host(parsed.hostname, allowed_hosts):
        raise XPostError("media_host_not_allowed", "素材域名不在允许列表", 400)
    max_bytes = _positive_int(max_bytes, "素材大小上限")
    timeout = max(1, min(_positive_int(timeout, "下载超时"), 120))
    client = http_client or UrllibHttpClient()
    try:
        response = client.request(
            "GET", str(url), headers={"Accept": "video/*,application/octet-stream;q=0.8"},
            timeout=timeout, stream=True, max_response_bytes=max_bytes,
        )
    except XPostError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise XPostError("media_download_failed", "素材下载网络失败: %s" % exc, 502) from None
    with response:
        if response.status != 200:
            raise XPostError("media_download_failed", "素材下载失败(HTTP %s)" % response.status, 502)
        length = response.headers.get("content-length", "").strip()
        if length:
            try:
                declared = int(length)
            except ValueError:
                raise XPostError("invalid_media_response", "素材Content-Length无效", 502) from None
            if declared <= 0 or declared > max_bytes:
                raise XPostError("media_too_large", "素材大小超过限制", 413)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        path_suffix = Path(parsed.path).suffix.lower()
        if content_type == "application/octet-stream" and path_suffix in {".mp4", ".mov", ".webm"}:
            content_type = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}[path_suffix]
        if not content_type.startswith("video/"):
            raise XPostError("invalid_media_type", "素材响应不是视频", 415)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(".%s.%s.part" % (destination.name, secrets.token_hex(8)))
        size = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as handle:
                for chunk in response.iter_bytes():
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise XPostError("invalid_media_response", "素材响应分片无效", 502)
                    size += len(chunk)
                    if size > max_bytes:
                        raise XPostError("media_too_large", "素材大小超过限制", 413)
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size <= 0:
                raise XPostError("invalid_media_response", "素材为空", 502)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {
        "path": destination,
        "size": size,
        "sha256": digest.hexdigest(),
        "media_type": content_type,
    }


def _frame_rate(value):
    value = str(value or "").strip()
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else 0.0
        return float(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return 0.0


def probe_media(path, max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout=30, runner=None):
    """Fail closed unless ffprobe confirms the X canary video contract."""
    path = Path(path)
    try:
        file_size = path.stat().st_size
    except OSError:
        raise XPostError("invalid_media", "素材文件不存在", 400) from None
    if file_size <= 0 or file_size > _positive_int(max_bytes, "素材大小上限"):
        raise XPostError("media_too_large", "素材为空或超过512MB限制", 413)
    run = runner or subprocess.run
    ffprobe_bin = str(os.environ.get("X_POST_FFPROBE_BIN", "ffprobe") or "ffprobe").strip()
    if not ffprobe_bin or "\x00" in ffprobe_bin:
        raise XPostError("media_probe_failed", "ffprobe路径配置无效", 500)
    command = [
        ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
    ]
    try:
        completed = run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=max(1, min(int(timeout), 120)), check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise XPostError("media_probe_failed", "ffprobe执行失败: %s" % exc, 422) from None
    if int(getattr(completed, "returncode", 1)) != 0:
        raise XPostError("media_probe_failed", "ffprobe未能解析素材", 422)
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or ""))
    except (ValueError, json.JSONDecodeError):
        raise XPostError("media_probe_failed", "ffprobe响应无效", 422) from None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    streams = streams if isinstance(streams, list) else []
    videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audios = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if len(videos) != 1 or not audios:
        raise XPostError("invalid_media_codec", "素材必须包含一个H264视频流和AAC音频流", 422)
    video = videos[0]
    if str(video.get("codec_name", "")).lower() != "h264" or str(video.get("pix_fmt", "")).lower() != "yuv420p":
        raise XPostError("invalid_media_codec", "素材视频必须为H264/yuv420p", 422)
    field_order = str(video.get("field_order", "") or "").strip().lower()
    if field_order and field_order != "progressive":
        raise XPostError("invalid_media_scan", "素材视频必须为逐行扫描", 422)
    if any(
        str(audio.get("codec_name", "")).lower() != "aac"
        or str(audio.get("profile", "") or "").strip().lower() not in {"", "lc"}
        for audio in audios
    ):
        raise XPostError("invalid_media_codec", "素材音频必须为AAC-LC", 422)
    try:
        width = int(video.get("width", 0) or 0)
        height = int(video.get("height", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        width = height = 0
    ratio = float(width) / float(height) if width > 0 and height > 0 else 0.0
    if width < 32 or height < 32 or width > 1280 or height > 1280 or ratio < (1.0 / 3.0) or ratio > 3.0:
        raise XPostError("invalid_media_dimensions", "素材分辨率或宽高比不符合X要求", 422)
    fps = _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if fps <= 0 or fps > 60.0:
        raise XPostError("invalid_media_frame_rate", "素材帧率必须大于0且不超过60fps", 422)
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_value = format_data.get("duration") or video.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if duration < 0.5 or duration > 140.0:
        raise XPostError("invalid_media_duration", "素材时长必须为0.5至140秒", 422)
    return {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "width": width,
        "height": height,
        "frame_rate": fps,
        "duration": duration,
        "size": file_size,
    }


def _json_response(response, expected_status, operation, unknown_on_success_shape=False):
    raw = response.body or b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        if response.status == expected_status and unknown_on_success_shape:
            raise XPostError("x_post_outcome_unknown", "%s响应无法确认" % operation, 502, True) from None
        raise XPostError("x_upstream_error", "%s返回非JSON响应" % operation, 502) from None
    if response.status != expected_status:
        detail = ""
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("title") or payload.get("message") or payload.get("code") or ""
        raise XPostError(
            "x_upstream_error", "%s失败(HTTP %s): %s" % (operation, response.status, redact_text(detail, 160)),
            502, unknown_on_success_shape and response.status >= 500,
        )
    if not isinstance(payload, dict):
        raise XPostError("x_upstream_error", "%s响应结构无效" % operation, 502, unknown_on_success_shape)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        detail = errors[0] if errors else ""
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("message") or detail.get("title") or "upstream errors"
        raise XPostError(
            "x_post_outcome_unknown" if unknown_on_success_shape else "x_upstream_error",
            "%s响应包含错误: %s" % (operation, redact_text(detail, 160)), 502,
            unknown_on_success_shape,
        )
    return payload


def _multipart_segment(segment_index, chunk):
    boundary = "xpost-%s" % secrets.token_hex(16)
    prefix = (
        "--%s\r\nContent-Disposition: form-data; name=\"segment_index\"\r\n\r\n%s\r\n"
        "--%s\r\nContent-Disposition: form-data; name=\"media\"; filename=\"segment-%04d.bin\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
        % (boundary, segment_index, boundary, segment_index)
    ).encode("ascii")
    suffix = ("\r\n--%s--\r\n" % boundary).encode("ascii")
    return boundary, prefix + bytes(chunk) + suffix


class XApiClient:
    """X v2 chunked media upload plus ``POST /2/tweets`` client."""

    def __init__(
        self, http_client=None, sleeper=None, timeout=30, chunk_bytes=DEFAULT_CHUNK_BYTES,
        max_status_polls=60,
    ):
        self.http = http_client or UrllibHttpClient()
        self.sleeper = sleeper or time.sleep
        self.timeout = max(1, min(int(timeout), 120))
        self.chunk_bytes = max(1, min(int(chunk_bytes), 16 * 1024 * 1024))
        self.max_status_polls = max(1, min(int(max_status_polls), 120))

    def _request(self, method, path, access_token, body=None, content_type=None, expected=200, operation="X API", unknown=False):
        if not access_token:
            raise XPostError("x_token_missing", "X Access Token缺失", 409)
        headers = {"Authorization": "Bearer " + str(access_token), "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.http.request(
                method, X_API_BASE_URL + path, headers=headers, body=body, timeout=self.timeout,
                stream=False, max_response_bytes=MAX_HTTP_RESPONSE_BYTES,
            )
        except XPostError as exc:
            if unknown:
                raise XPostError("x_post_outcome_unknown", str(exc), exc.status, True) from None
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise XPostError(
                "x_post_outcome_unknown" if unknown else "x_upstream_error",
                "%s网络失败: %s" % (operation, exc), 502, unknown,
            ) from None
        return _json_response(response, expected, operation, unknown_on_success_shape=unknown)

    def upload_media(self, access_token, path, media_type="video/mp4", media_category="tweet_video"):
        path = Path(path)
        size = path.stat().st_size
        if size <= 0:
            raise XPostError("invalid_media", "待上传素材为空", 400)
        total_segments = (size + self.chunk_bytes - 1) // self.chunk_bytes
        if total_segments > 1000:
            raise XPostError("media_too_large", "素材分片数超过X上限", 413)
        init_body = json.dumps(
            {"media_type": str(media_type), "total_bytes": size, "media_category": str(media_category)},
            separators=(",", ":"),
        ).encode("utf-8")
        initialized = self._request(
            "POST", "/2/media/upload/initialize", access_token, init_body, "application/json",
            operation="X媒体初始化",
        )
        data = initialized.get("data") if isinstance(initialized.get("data"), dict) else {}
        media_id = str(data.get("id", "") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", media_id):
            raise XPostError("x_upstream_error", "X媒体初始化未返回有效media id", 502)
        with path.open("rb") as handle:
            for segment_index in range(total_segments):
                chunk = handle.read(self.chunk_bytes)
                boundary, multipart = _multipart_segment(segment_index, chunk)
                self._request(
                    "POST", "/2/media/upload/%s/append" % urllib.parse.quote(media_id, safe=""),
                    access_token, multipart, "multipart/form-data; boundary=%s" % boundary,
                    operation="X媒体分片上传",
                )
        finalized = self._request(
            "POST", "/2/media/upload/%s/finalize" % urllib.parse.quote(media_id, safe=""),
            access_token, body=b"", operation="X媒体完成上传",
        )
        final_data = finalized.get("data") if isinstance(finalized.get("data"), dict) else {}
        processing = final_data.get("processing_info") if isinstance(final_data.get("processing_info"), dict) else None
        if processing is not None:
            processing = self._wait_for_media(access_token, media_id, processing)
        return {"media_id": media_id, "processing_info": processing or {}, "initialize": data}

    def _wait_for_media(self, access_token, media_id, processing):
        for _attempt in range(self.max_status_polls):
            state = str(processing.get("state", "") or "").lower()
            if state == "succeeded":
                return processing
            if state == "failed":
                raise XPostError("x_media_processing_failed", "X媒体处理失败", 502)
            if state not in {"pending", "in_progress"}:
                raise XPostError("x_upstream_error", "X媒体处理状态无效", 502)
            try:
                wait_seconds = int(processing.get("check_after_secs", 1) or 1)
            except (TypeError, ValueError, OverflowError):
                wait_seconds = 1
            self.sleeper(max(0, min(wait_seconds, 30)))
            query = urllib.parse.urlencode({"media_id": media_id, "command": "STATUS"})
            status_payload = self._request(
                "GET", "/2/media/upload?" + query, access_token, operation="X媒体状态查询",
            )
            status_data = status_payload.get("data") if isinstance(status_payload.get("data"), dict) else {}
            processing = status_data.get("processing_info") if isinstance(status_data.get("processing_info"), dict) else {}
        raise XPostError("x_media_processing_timeout", "X媒体处理超时", 504)

    def create_post(self, access_token, text, media_id):
        body = json.dumps(
            {"text": str(text), "media": {"media_ids": [str(media_id)]}},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        payload = self._request(
            "POST", "/2/tweets", access_token, body, "application/json", expected=201,
            operation="X Post创建", unknown=True,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        post_id = str(data.get("id", "") or "")
        if not re.fullmatch(r"[0-9]{1,32}", post_id):
            raise XPostError("x_post_outcome_unknown", "X Post创建响应缺少ID", 502, True)
        return {"post_id": post_id, "data": data}

    def publish(self, access_token, text, media_path, media_type="video/mp4"):
        media = self.upload_media(access_token, media_path, media_type=media_type)
        post = self.create_post(access_token, text, media["media_id"])
        return {"media_id": media["media_id"], "post_id": post["post_id"], "data": post["data"]}


def _result_from_log(row):
    return {
        "log_id": int(row["id"]),
        "status": row["status"],
        "short_url": row["short_url"],
        "post_id": row["x_post_id"],
        "post_url": row["x_post_url"],
        "preview_url": row["x_post_url"],
        "unknown_outcome": bool(row["unknown_outcome"]),
    }


def publish_canary(
    *, db_path, queue_id, account, access_token, public_root, short_base_url,
    allowed_media_hosts, http_client=None, sleeper=None, timeout=30,
    max_media_bytes=DEFAULT_MAX_MEDIA_BYTES,
):
    """Publish one queued canary. Must run inside the sidecar account lock."""
    if not isinstance(account, dict):
        raise XPostError("invalid_request", "X账号资料无效", 400)
    account_id = _positive_int(account.get("id"), "account_id")
    username = str(account.get("username", "") or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
        raise XPostError("invalid_request", "X账号用户名无效", 400)
    if not access_token:
        raise XPostError("x_token_missing", "X Access Token缺失", 409)
    store = XPostStore(db_path)
    queue = store.get_queue(queue_id)
    if int(queue["account_id"]) != account_id:
        raise XPostError("x_post_account_mismatch", "发布队列与X账号不匹配", 409)
    if not secrets.compare_digest(str(queue["account_username"]), username):
        raise XPostError("x_post_account_mismatch", "发布队列用户名与X账号不匹配", 409)
    log = store.reserve_log(queue["id"])
    if log["status"] == "published":
        return _result_from_log(log)
    if log["status"] != "reserved":
        unknown = bool(log["unknown_outcome"]) or log["status"] == "post_creating"
        code = "x_post_unknown_outcome" if unknown else "x_post_retry_requires_review"
        raise XPostError(code, "发布日志已执行，禁止自动重复发帖", 409, unknown)

    if log["long_url"]:
        long_url = log["long_url"]
        short_url = log["short_url"]
        post_text = log["post_text"]
    else:
        long_url = build_w2a_url(
            {
                "username": queue["account_username"],
                "timestamp": int(time.time()),
                "material_language": queue["material_language"],
                "drama_name": queue["drama_name"],
                "tag": queue["tag"],
                "log_id": log["id"],
                "page_name": queue["page_name"],
                "page_id": queue["page_id"],
                "material_name": queue["material_name"],
                "material_id": queue["material_id"],
                "queue_id": queue["id"],
                "content_id": queue["content_id"],
            }
        )
        short_url = _build_short_url(short_base_url, log["id"])
        post_text = build_post_text(short_url, queue["description"])
        log = store.prepare_log(log["id"], long_url, short_url, post_text)
    write_short_redirect(public_root, log["id"], long_url)

    work_root = Path(public_root).resolve().parent / "media-work"
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="log-%s-" % log["id"], dir=str(work_root)))
    try:
        media = download_media(
            queue["material_url"], work_dir / "material.mp4", allowed_media_hosts,
            max_bytes=max_media_bytes, timeout=timeout, http_client=http_client,
        )
        probe_media(media["path"], max_bytes=max_media_bytes, timeout=timeout)
        store.mark_publishing(log["id"])
        x_client = XApiClient(http_client=http_client, sleeper=sleeper, timeout=timeout)
        uploaded = x_client.upload_media(access_token, media["path"], media_type=media["media_type"])
        store.mark_media_uploaded(log["id"], uploaded["media_id"])
        created = x_client.create_post(access_token, post_text, uploaded["media_id"])
        post_url = "https://x.com/%s/status/%s" % (username, created["post_id"])
        published = store.mark_published(log["id"], uploaded["media_id"], created["post_id"], post_url)
        return _result_from_log(published)
    except XPostError as exc:
        failed = store.mark_failed(log["id"], exc.code, str(exc), exc.unknown_outcome)
        raise XPostError(exc.code, str(exc), exc.status, bool(failed["unknown_outcome"])) from None
    except Exception as exc:
        store.mark_failed(log["id"], "x_post_internal_error", str(exc), False)
        raise XPostError("x_post_internal_error", "发布处理失败: %s" % exc, 500) from None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _env_int(name, default, minimum=1, maximum=2 ** 63 - 1):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


def publish_canary_post(candidate, account, access_token, **overrides):
    """Convenience wrapper that enqueues a candidate then publishes it once."""
    if not isinstance(candidate, dict):
        raise XPostError("invalid_request", "发布候选必须是对象", 400)
    candidate = dict(candidate)
    db_path = overrides.pop(
        "db_path",
        os.environ.get("X_POST_DB_PATH") or os.environ.get("X_ACCOUNTS_DB") or "/var/lib/x-post-automation/accounts.sqlite3",
    )
    public_root = overrides.pop("public_root", os.environ.get("X_POST_PUBLIC_ROOT", DEFAULT_PUBLIC_ROOT))
    short_base_url = overrides.pop(
        "short_base_url", os.environ.get("X_POST_SHORT_BASE_URL", DEFAULT_SHORT_BASE_URL)
    )
    allowed = overrides.pop("allowed_media_hosts", None)
    if allowed is None:
        allowed = [item.strip() for item in os.environ.get("X_POST_MEDIA_ALLOWED_HOSTS", "").split(",") if item.strip()]
    if not allowed:
        raise XPostError("media_allowlist_not_configured", "素材域名允许列表未配置", 503)
    timeout = overrides.pop("timeout", _env_int("X_POST_HTTP_TIMEOUT_SECONDS", 30, 1, 120))
    max_bytes = overrides.pop(
        "max_media_bytes", _env_int("X_POST_MAX_MEDIA_BYTES", DEFAULT_MAX_MEDIA_BYTES, 1)
    )
    http_client = overrides.pop("http_client", None)
    sleeper = overrides.pop("sleeper", None)
    if overrides:
        raise XPostError("invalid_request", "未知发布配置: %s" % ",".join(sorted(overrides)), 400)
    queue = XPostStore(db_path).enqueue(candidate)
    return publish_canary(
        db_path=db_path,
        queue_id=queue["id"],
        account=account,
        access_token=access_token,
        public_root=public_root,
        short_base_url=short_base_url,
        allowed_media_hosts=allowed,
        http_client=http_client,
        sleeper=sleeper,
        timeout=timeout,
        max_media_bytes=max_bytes,
    )

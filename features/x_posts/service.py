"""Queue, redirect, media and X API support for a single X Post canary.

The caller must invoke :func:`publish_canary` while the X account sidecar holds
``publish_credentials(...)``.  This module never persists or returns an access
token and all HTTP collaborators are injectable for offline tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import http.client
import json
import math
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
from datetime import datetime, timedelta, timezone
from pathlib import Path


W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2116/view"
X_API_BASE_URL = "https://api.x.com"
DEFAULT_PUBLIC_ROOT = "/mnt/data-disk/x-post-automation/s2l"
DEFAULT_SHORT_BASE_URL = "https://ai.yingliangads.com/s2l"
DEFAULT_STORAGE_MOUNT_ROOT = "/mnt/data-disk"
DEFAULT_STORAGE_ROOT = "/mnt/data-disk/x-post-automation"
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

QUEUE_LEDGER_FIELDS = (
    "run_id",
    "run_date",
    "material_key",
    "candidate_rank",
    "spend",
    "preflight_sha256",
    "preflight_size",
    "facebook_violation_count",
    "tiktok_violation_count",
    "twitter_violation_count",
    "resource_audit_count",
    "dangerous_tag_count",
)

COMPLIANCE_COUNT_FIELDS = (
    "facebook_violation_count",
    "tiktok_violation_count",
    "twitter_violation_count",
    "resource_audit_count",
    "dangerous_tag_count",
)

COMPLIANCE_FIELD_ALIASES = {
    "facebook_violation_count": ("facebook_violation_count", "facebook_violations"),
    "tiktok_violation_count": ("tiktok_violation_count", "tiktok_violations"),
    "twitter_violation_count": ("twitter_violation_count", "twitter_violations"),
    "resource_audit_count": ("resource_audit_count", "resource_audit_violations"),
    "dangerous_tag_count": ("dangerous_tag_count", "dangerous_tags"),
}

BEIJING_TZ = timezone(timedelta(hours=8))

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
        parsed.scheme != "https"
        or parsed.hostname != "ai.yingliangads.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/s2l"
        or parsed.query
        or parsed.fragment
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


def _validate_post_storage_layout(
    public_root,
    *,
    mount_root=DEFAULT_STORAGE_MOUNT_ROOT,
    storage_root=DEFAULT_STORAGE_ROOT,
):
    """Resolve the fixed production layout and prove it is still on the mount."""
    mount = Path(mount_root)
    storage = Path(storage_root)
    public = Path(public_root)
    if not all(path.is_absolute() for path in (mount, storage, public)):
        raise XPostError("x_post_storage_unavailable", "X Post存储路径必须为绝对路径", 503)
    if not mount.exists() or not mount.is_dir() or mount.is_symlink():
        raise XPostError("x_post_storage_unavailable", "X Post数据盘挂载点无效", 503)
    if not os.path.ismount(str(mount)):
        raise XPostError("x_post_storage_unavailable", "X Post数据盘未挂载", 503)
    for path in (storage, public):
        if not path.exists() or not path.is_dir() or path.is_symlink():
            raise XPostError("x_post_storage_unavailable", "X Post存储目录无效", 503)
    try:
        mount_resolved = mount.resolve(strict=True)
        storage_resolved = storage.resolve(strict=True)
        public_resolved = public.resolve(strict=True)
        devices = {
            mount_resolved.stat().st_dev,
            storage_resolved.stat().st_dev,
            public_resolved.stat().st_dev,
        }
    except OSError:
        raise XPostError("x_post_storage_unavailable", "X Post存储目录无法解析", 503) from None
    expected_public = storage_resolved / "s2l"
    media_work = storage_resolved / "media-work"
    if (
        storage_resolved.parent != mount_resolved
        or public_resolved != expected_public
        or public_resolved.parent != storage_resolved
        or len(devices) != 1
    ):
        raise XPostError("x_post_storage_unavailable", "X Post存储目录不符合固定布局", 503)
    if (
        not media_work.exists()
        or not media_work.is_dir()
        or media_work.is_symlink()
    ):
        raise XPostError("x_post_storage_unavailable", "X Post媒体工作目录无效", 503)
    try:
        media_work_resolved = media_work.resolve(strict=True)
        if (
            media_work_resolved.parent != storage_resolved
            or media_work_resolved.stat().st_dev != mount_resolved.stat().st_dev
        ):
            raise XPostError(
                "x_post_storage_unavailable", "X Post媒体工作目录不在数据盘", 503
            )
    except OSError:
        raise XPostError("x_post_storage_unavailable", "X Post媒体工作目录无法解析", 503) from None
    return {
        "mount": mount_resolved,
        "storage": storage_resolved,
        "public": public_resolved,
        "media_work": media_work_resolved,
    }


def _fsync_directory(path):
    """Persist directory metadata on POSIX; Windows has no directory fsync."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_existing_short_redirect(path):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        else:
            os.chmod(path, 0o644)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def write_short_redirect(public_root, log_id, long_url, *, durable_storage=None):
    """Atomically create an immutable ``<log_id>.html`` redirect page."""
    target = _validate_w2a_url(long_url)
    log_id = _positive_int(log_id, "日志ID")
    if durable_storage is not None:
        if not isinstance(durable_storage, dict):
            raise XPostError("x_post_storage_unavailable", "X Post持久存储配置无效", 503)
        layout = _validate_post_storage_layout(
            public_root,
            mount_root=durable_storage.get("mount_root", DEFAULT_STORAGE_MOUNT_ROOT),
            storage_root=durable_storage.get("storage_root", DEFAULT_STORAGE_ROOT),
        )
        root = layout["public"]
    else:
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
                _sync_existing_short_redirect(destination)
                return destination
        except OSError as exc:
            raise XPostError(
                "short_link_write_failed",
                "短链文件持久化失败: %s" % exc,
                500,
            ) from None
        raise XPostError("short_link_conflict", "该日志ID的短链已存在且目标不同", 409)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % destination.name, dir=str(root))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o644)
            else:
                os.chmod(temporary, 0o644)
            os.fsync(handle.fileno())
        if durable_storage is not None:
            refreshed = _validate_post_storage_layout(
                public_root,
                mount_root=durable_storage.get("mount_root", DEFAULT_STORAGE_MOUNT_ROOT),
                storage_root=durable_storage.get("storage_root", DEFAULT_STORAGE_ROOT),
            )
            if refreshed["public"] != root:
                raise XPostError("x_post_storage_unavailable", "X Post存储挂载身份已变化", 503)
        os.replace(temporary, destination)
        _fsync_directory(root)
    except XPostError:
        raise
    except OSError as exc:
        raise XPostError("short_link_write_failed", "短链页面写入失败: %s" % exc, 500) from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def preflight_post_storage(
    public_root,
    *,
    mount_root=DEFAULT_STORAGE_MOUNT_ROOT,
    storage_root=DEFAULT_STORAGE_ROOT,
    minimum_free_bytes=(DEFAULT_MAX_MEDIA_BYTES * 3) + (64 * 1024 * 1024),
):
    """Fail closed before a daily plan if durable redirect storage is unsafe."""
    layout = _validate_post_storage_layout(
        public_root,
        mount_root=mount_root,
        storage_root=storage_root,
    )
    storage_resolved = layout["storage"]
    public_resolved = layout["public"]
    try:
        free_bytes = int(shutil.disk_usage(storage_resolved).free)
    except OSError:
        raise XPostError("x_post_storage_unavailable", "X Post存储空间无法读取", 503) from None
    if free_bytes < _positive_int(minimum_free_bytes, "存储可用空间下限"):
        raise XPostError("x_post_storage_unavailable", "X Post数据盘可用空间不足", 503)

    for probe_root in (public_resolved, layout["media_work"]):
        source = probe_root / (".preflight-%s.tmp" % secrets.token_hex(12))
        destination = probe_root / (".preflight-%s.ok" % secrets.token_hex(12))
        file_descriptor = None
        directory_descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            file_descriptor = os.open(str(source), flags, 0o600)
            os.write(file_descriptor, b"x-post-storage-preflight\n")
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.replace(source, destination)
            if destination.read_bytes() != b"x-post-storage-preflight\n":
                raise OSError("storage probe content mismatch")
            destination.unlink()
            if os.name != "nt":
                directory_descriptor = os.open(str(probe_root), os.O_RDONLY)
                os.fsync(directory_descriptor)
        except OSError:
            raise XPostError(
                "x_post_storage_unavailable",
                "X Post存储未通过原子写入检查",
                503,
            ) from None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            for path in (source, destination):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    return {"ready": True, "mounted": True, "atomic_write": True}


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _date_value(value, label):
    value = str(value or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise XPostError("invalid_request", "%s必须为YYYY-MM-DD" % label, 400) from None
    return value


def _beijing_today():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def _legacy_run_date(created_at):
    value = str(created_at or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        raise XPostError(
            "x_post_storage_conflict",
            "历史X发布队列缺少可解析的创建时间，迁移已中止",
            500,
        ) from None


def normalize_material_key(value, error_code="invalid_request"):
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise XPostError(error_code, "素材ID必须为正十进制整数", 500 if error_code != "invalid_request" else 400)
    parsed = int(raw)
    if parsed <= 0 or parsed > 9223372036854775807:
        raise XPostError(error_code, "素材ID超出允许范围", 500 if error_code != "invalid_request" else 400)
    return str(parsed)


def _nonnegative_int(value, label, default=0):
    if value in (None, ""):
        return int(default)
    if isinstance(value, bool):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise XPostError("invalid_request", "%s无效" % label, 400) from None
    if parsed < 0 or parsed > 2147483647:
        raise XPostError("invalid_request", "%s无效" % label, 400)
    return parsed


def _nonnegative_float(value, label, default=0.0):
    if value in (None, ""):
        return float(default)
    if isinstance(value, bool):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise XPostError("invalid_request", "%s无效" % label, 400) from None
    if parsed < 0 or not math.isfinite(parsed):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    return parsed


def _compliance_counts(payload, require_all=False):
    """Normalize compliance evidence without treating missing values as clean."""
    if "compliance_counts" in payload:
        compliance = payload.get("compliance_counts")
        if not isinstance(compliance, dict):
            raise XPostError("invalid_request", "compliance_counts必须为对象", 400)
    else:
        compliance = {}
    result = {}
    for field, aliases in COMPLIANCE_FIELD_ALIASES.items():
        supplied = []
        for container in (payload, compliance):
            for alias in aliases:
                if alias not in container:
                    continue
                raw_value = container.get(alias)
                if raw_value in (None, ""):
                    raise XPostError("invalid_request", "%s缺少明确证据" % field, 400)
                supplied.append(_nonnegative_int(raw_value, field))
        if not supplied:
            if require_all:
                raise XPostError("invalid_request", "%s缺少明确证据" % field, 400)
            result[field] = 0
            continue
        if len(set(supplied)) != 1:
            raise XPostError("invalid_request", "%s证据冲突" % field, 400)
        result[field] = supplied[0]
    return result


def ensure_storage(db_path):
    """Create and migrate the additive X Post ledger.

    The migration deliberately fails closed before unique indexes are created
    when legacy rows would violate the global material or account/day guards.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect(path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_daily_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL UNIQUE,
                    source_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_count INTEGER NOT NULL DEFAULT 3,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
            CREATE TABLE IF NOT EXISTS x_post_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                run_id INTEGER,
                run_date TEXT NOT NULL DEFAULT '',
                material_key TEXT NOT NULL DEFAULT '',
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
                candidate_rank INTEGER NOT NULL DEFAULT 0,
                spend REAL NOT NULL DEFAULT 0,
                preflight_sha256 TEXT NOT NULL DEFAULT '',
                preflight_size INTEGER NOT NULL DEFAULT 0,
                facebook_violation_count INTEGER NOT NULL DEFAULT 0,
                tiktok_violation_count INTEGER NOT NULL DEFAULT 0,
                twitter_violation_count INTEGER NOT NULL DEFAULT 0,
                resource_audit_count INTEGER NOT NULL DEFAULT 0,
                dangerous_tag_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES x_post_daily_run(id)
            )
                """
            )
            conn.execute(
                """
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
            )
                """
            )
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(x_post_queue)")}
            additive_columns = {
                "account_username": "TEXT NOT NULL DEFAULT ''",
                "run_id": "INTEGER",
                "run_date": "TEXT NOT NULL DEFAULT ''",
                "material_key": "TEXT NOT NULL DEFAULT ''",
                "candidate_rank": "INTEGER NOT NULL DEFAULT 0",
                "spend": "REAL NOT NULL DEFAULT 0",
                "preflight_sha256": "TEXT NOT NULL DEFAULT ''",
                "preflight_size": "INTEGER NOT NULL DEFAULT 0",
                "facebook_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "tiktok_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "twitter_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "resource_audit_count": "INTEGER NOT NULL DEFAULT 0",
                "dangerous_tag_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additive_columns.items():
                if name not in queue_columns:
                    conn.execute("ALTER TABLE x_post_queue ADD COLUMN %s %s" % (name, definition))

            legacy_rows = conn.execute(
                "SELECT id,material_id,material_key,run_date,created_at FROM x_post_queue ORDER BY id"
            ).fetchall()
            for row in legacy_rows:
                material_key = normalize_material_key(
                    row["material_id"],
                    error_code="x_post_storage_conflict",
                )
                existing_material_key = str(row["material_key"] or "").strip()
                if existing_material_key and normalize_material_key(
                    existing_material_key,
                    error_code="x_post_storage_conflict",
                ) != material_key:
                    raise XPostError(
                        "x_post_storage_conflict",
                        "历史X发布队列material_key与素材ID不一致，迁移已中止",
                        500,
                    )
                run_date = str(row["run_date"] or "").strip() or _legacy_run_date(row["created_at"])
                try:
                    _date_value(run_date, "run_date")
                except XPostError:
                    raise XPostError(
                        "x_post_storage_conflict",
                        "历史X发布队列run_date无效，迁移已中止",
                        500,
                    ) from None
                conn.execute(
                    "UPDATE x_post_queue SET material_key=?,run_date=? WHERE id=?",
                    (material_key, run_date, row["id"]),
                )

            duplicate_material = conn.execute(
                "SELECT material_key,COUNT(*) AS total FROM x_post_queue "
                "WHERE material_key<>'' GROUP BY material_key HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
            if duplicate_material:
                raise XPostError(
                    "x_post_storage_conflict",
                    "历史X发布队列存在重复素材%s，迁移已中止" % duplicate_material["material_key"],
                    500,
                )
            duplicate_account_day = conn.execute(
                "SELECT account_id,run_date,COUNT(*) AS total FROM x_post_queue "
                "WHERE run_date<>'' GROUP BY account_id,run_date HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
            if duplicate_account_day:
                raise XPostError(
                    "x_post_storage_conflict",
                    "历史X发布队列存在同账号同日重复，迁移已中止",
                    500,
                )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_material_key "
                "ON x_post_queue(material_key) WHERE material_key<>''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_account_run_date "
                "ON x_post_queue(account_id,run_date) WHERE run_date<>''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_run ON x_post_queue(run_id,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_status ON x_post_queue(status,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_log_status ON x_post_publish_log(status,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_log_account ON x_post_publish_log(account_id,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_run_status ON x_post_daily_run(status,run_date,id)"
            )
            # SQLite cannot add a FOREIGN KEY to a legacy table with ALTER
            # TABLE. These triggers preserve the same run_id integrity for
            # both newly-created and migrated queue schemas.
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_x_post_queue_run_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.run_id IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM x_post_daily_run WHERE id=NEW.run_id)
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue run_id missing');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_x_post_queue_run_update
                BEFORE UPDATE OF run_id ON x_post_queue
                WHEN NEW.run_id IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM x_post_daily_run WHERE id=NEW.run_id)
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue run_id missing');
                END
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
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

    def _queue_payload(
        self, payload, run_date=None, candidate_rank=None, require_compliance=False
    ):
        if not isinstance(payload, dict):
            raise XPostError("invalid_request", "发布候选必须是对象", 400)
        result = {}
        result["account_id"] = _positive_int(payload.get("account_id"), "account_id")
        username = str(payload.get("account_username", "") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
            raise XPostError("invalid_request", "account_username无效", 400)
        result["account_username"] = username
        result["source_date"] = _date_value(payload.get("source_date"), "source_date")
        for field in QUEUE_FIELDS[3:]:
            limit = 4096 if field in {"material_url", "description"} else 500
            result[field] = _clean_text(payload.get(field), field, limit)
        material = urllib.parse.urlsplit(result["material_url"])
        if material.scheme != "https" or not material.hostname or material.username or material.password or material.fragment:
            raise XPostError("invalid_media_url", "素材地址必须是HTTPS URL", 400)
        material_key = normalize_material_key(result["material_id"])
        supplied_material_key = payload.get("material_key")
        if supplied_material_key not in (None, ""):
            supplied_material_key = normalize_material_key(supplied_material_key)
            if supplied_material_key != material_key:
                raise XPostError("invalid_request", "material_key与material_id不一致", 400)
        result["material_key"] = material_key
        result["run_date"] = _date_value(
            run_date if run_date is not None else (payload.get("run_date") or _beijing_today()),
            "run_date",
        )
        raw_run_id = payload.get("run_id")
        result["run_id"] = _positive_int(raw_run_id, "run_id") if raw_run_id not in (None, "") else None
        rank_value = candidate_rank if candidate_rank is not None else payload.get("candidate_rank")
        result["candidate_rank"] = _nonnegative_int(rank_value, "candidate_rank", 0)
        result["spend"] = _nonnegative_float(payload.get("spend"), "spend", 0)
        preflight_sha256 = str(payload.get("preflight_sha256", "") or "").strip().lower()
        result["preflight_size"] = _nonnegative_int(
            payload.get("preflight_size"), "preflight_size", 0
        )
        if preflight_sha256 and not re.fullmatch(r"[0-9a-f]{64}", preflight_sha256):
            raise XPostError("invalid_request", "preflight_sha256无效", 400)
        if require_compliance and (not preflight_sha256 or result["preflight_size"] <= 0):
            raise XPostError("invalid_request", "每日计划缺少完整媒体预检指纹", 400)
        result["preflight_sha256"] = preflight_sha256
        result.update(_compliance_counts(payload, require_all=require_compliance))
        default_key = "xpost:%s:%s:%s" % (
            result["source_date"],
            result["account_id"],
            result["material_key"],
        )
        key = str(payload.get("idempotency_key", "") or default_key).strip()
        if not key or len(key) > 200 or any(ord(char) < 33 for char in key):
            raise XPostError("invalid_request", "idempotency_key无效", 400)
        result["idempotency_key"] = key
        return result

    def enqueue(self, payload):
        values = self._queue_payload(payload)
        timestamp = utc_now()
        columns = ("idempotency_key",) + QUEUE_LEDGER_FIELDS + QUEUE_FIELDS
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_queue WHERE idempotency_key=?", (values["idempotency_key"],)
            ).fetchone()
            if existing:
                # Preserve the historical one-off canary contract. Derived
                # ledger fields are compared only when the caller supplied
                # them, so a migrated published canary remains replayable on a
                # later calendar day without another X write.
                comparison_fields = list(("idempotency_key", "material_key") + QUEUE_FIELDS)
                for field in (
                    "run_id",
                    "run_date",
                    "candidate_rank",
                    "spend",
                    "preflight_sha256",
                    "preflight_size",
                ):
                    if field in payload and payload.get(field) not in (None, ""):
                        comparison_fields.append(field)
                compliance_payload = payload.get("compliance_counts")
                compliance_payload = (
                    compliance_payload if isinstance(compliance_payload, dict) else {}
                )
                for field, aliases in COMPLIANCE_FIELD_ALIASES.items():
                    if any(alias in payload or alias in compliance_payload for alias in aliases):
                        comparison_fields.append(field)
                for field in comparison_fields:
                    if str(existing[field]) != str(values[field]):
                        conn.rollback()
                        raise XPostError("x_post_idempotency_conflict", "幂等键已对应其他发布候选", 409)
                conn.commit()
                item = _row_dict(existing)
                item["created"] = False
                return item
            if conn.execute(
                "SELECT id FROM x_post_queue WHERE material_key=?",
                (values["material_key"],),
            ).fetchone():
                conn.rollback()
                raise XPostError("x_post_material_already_used", "该素材已被X发布队列占用", 409)
            if conn.execute(
                "SELECT id FROM x_post_queue WHERE account_id=? AND run_date=?",
                (values["account_id"], values["run_date"]),
            ).fetchone():
                conn.rollback()
                raise XPostError("x_post_account_day_already_reserved", "该X账号当日已有发布队列", 409)
            placeholders = ",".join("?" for _field in columns)
            try:
                cursor = conn.execute(
                    "INSERT INTO x_post_queue(%s,status,created_at,updated_at) VALUES(%s,'queued',?,?)"
                    % (",".join(columns), placeholders),
                    tuple(values[field] for field in columns) + (timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError("x_post_storage_conflict", "X发布队列唯一约束冲突", 409) from exc
            conn.commit()
            item = self.get_queue(cursor.lastrowid)
            item["created"] = True
            return item

    def create_daily_plan(self, run_date, source_date, candidates):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        if (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - datetime.strptime(source_date, "%Y-%m-%d").date()
        ).days != 1:
            raise XPostError("invalid_request", "source_date必须是run_date前一天", 400)
        if not isinstance(candidates, list) or len(candidates) != 3:
            raise XPostError("x_post_daily_candidate_shortage", "每日计划必须一次提交三个候选", 409)
        prepared = []
        account_ids = set()
        material_keys = set()
        for index, candidate in enumerate(candidates, 1):
            payload = dict(candidate) if isinstance(candidate, dict) else candidate
            if isinstance(payload, dict) and _date_value(payload.get("source_date"), "source_date") != source_date:
                raise XPostError("invalid_request", "候选source_date与每日批次不一致", 400)
            values = self._queue_payload(
                payload,
                run_date=run_date,
                candidate_rank=index,
                require_compliance=True,
            )
            values["idempotency_key"] = "xpost:daily:%s:%s" % (run_date, values["account_id"])
            if values["account_id"] in account_ids:
                raise XPostError("invalid_request", "每日计划账号必须互不相同", 400)
            if values["material_key"] in material_keys:
                raise XPostError("invalid_request", "每日计划素材必须互不相同", 400)
            if any(values[field] != 0 for field in COMPLIANCE_COUNT_FIELDS):
                raise XPostError("invalid_request", "每日计划候选存在违规或危险标签计数", 400)
            account_ids.add(values["account_id"])
            material_keys.add(values["material_key"])
            prepared.append(values)

        timestamp = utc_now()
        columns = ("idempotency_key",) + QUEUE_LEDGER_FIELDS + QUEUE_FIELDS
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_run = conn.execute(
                "SELECT * FROM x_post_daily_run WHERE run_date=?",
                (run_date,),
            ).fetchone()
            run_id = None
            if existing_run:
                if str(existing_run["source_date"]) != source_date or int(existing_run["expected_count"]) != 3:
                    conn.rollback()
                    raise XPostError("x_post_daily_run_exists", "该日期已存在不同的X发布批次", 409)
                existing_queues = conn.execute(
                    "SELECT * FROM x_post_queue WHERE run_id=? ORDER BY candidate_rank,id",
                    (existing_run["id"],),
                ).fetchall()
                if len(existing_queues) == 3:
                    conn.commit()
                    item = _row_dict(existing_run)
                    item["queues"] = [_row_dict(row) for row in existing_queues]
                    item["created"] = False
                    return item
                if existing_queues or existing_run["status"] != "failed_preflight":
                    conn.rollback()
                    raise XPostError("x_post_storage_conflict", "已有每日批次队列数量异常", 500)
                run_id = int(existing_run["id"])

            for values in prepared:
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError("x_post_material_already_used", "候选素材已被X发布队列占用", 409)
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE account_id=? AND run_date=?",
                    (values["account_id"], run_date),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_account_day_already_reserved",
                        "候选X账号当日已有发布队列",
                        409,
                    )

            if run_id is None:
                cursor = conn.execute(
                    "INSERT INTO x_post_daily_run("
                    "run_date,source_date,status,expected_count,queued_count,started_at,created_at,updated_at"
                    ") VALUES(?,?,'queued',3,3,?,?,?)",
                    (run_date, source_date, timestamp, timestamp, timestamp),
                )
                run_id = int(cursor.lastrowid)
            else:
                conn.execute(
                    "UPDATE x_post_daily_run SET status='queued',queued_count=3,published_count=0,"
                    "failed_count=0,unknown_count=0,error_code='',error_message='',started_at=?,"
                    "finished_at='',updated_at=? WHERE id=? AND status='failed_preflight'",
                    (timestamp, timestamp, run_id),
                )
            queue_ids = []
            placeholders = ",".join("?" for _field in columns)
            try:
                for values in prepared:
                    values["run_id"] = run_id
                    queue_cursor = conn.execute(
                        "INSERT INTO x_post_queue(%s,status,created_at,updated_at) "
                        "VALUES(%s,'queued',?,?)" % (",".join(columns), placeholders),
                        tuple(values[field] for field in columns) + (timestamp, timestamp),
                    )
                    queue_ids.append(int(queue_cursor.lastrowid))
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError("x_post_storage_conflict", "每日X发布计划唯一约束冲突", 409) from exc
            conn.commit()
        item = self.get_run(run_id)
        item["queues"] = [self.get_queue(queue_id) for queue_id in queue_ids]
        item["created"] = True
        return item

    def get_queue(self, queue_id):
        queue_id = _positive_int(queue_id, "queue_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
        if not row:
            raise XPostError("x_post_queue_not_found", "发布队列记录不存在", 404)
        return _row_dict(row)

    def get_run(self, run_id):
        run_id = _positive_int(run_id, "run_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_daily_run WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise XPostError("x_post_run_not_found", "每日发布批次不存在", 404)
        return _row_dict(row)

    def get_run_by_date(self, run_date):
        run_date = _date_value(run_date, "run_date")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_daily_run WHERE run_date=?", (run_date,)).fetchone()
        return _row_dict(row)

    def record_run_failure(self, run_date, source_date, error_code, error_message):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        if (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - datetime.strptime(source_date, "%Y-%m-%d").date()
        ).days != 1:
            raise XPostError("invalid_request", "source_date必须是run_date前一天", 400)
        try:
            code = _clean_token(error_code or "x_post_daily_preflight_failed", "error code", 64)
        except ValueError:
            raise XPostError("invalid_request", "error_code无效", 400) from None
        message = redact_text(error_message, 500)
        if not message:
            message = "X每日发布预检失败"
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_daily_run WHERE run_date=?",
                (run_date,),
            ).fetchone()
            if existing:
                if str(existing["source_date"]) != source_date:
                    conn.rollback()
                    raise XPostError("x_post_daily_run_exists", "该日期已存在不同来源日期的批次", 409)
                queue_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM x_post_queue WHERE run_id=?",
                        (existing["id"],),
                    ).fetchone()[0]
                )
                if queue_count:
                    conn.commit()
                    item = _row_dict(existing)
                    item["recorded"] = False
                    return item
                if (
                    existing["status"] == "failed_preflight"
                    and existing["error_code"] == code
                    and existing["error_message"] == message
                ):
                    conn.commit()
                    item = _row_dict(existing)
                    item["recorded"] = False
                    return item
                conn.execute(
                    "UPDATE x_post_daily_run SET status='failed_preflight',queued_count=0,"
                    "published_count=0,failed_count=0,unknown_count=0,error_code=?,error_message=?,"
                    "finished_at=?,updated_at=? WHERE id=?",
                    (code, message, timestamp, timestamp, existing["id"]),
                )
                run_id = int(existing["id"])
                recorded = True
            else:
                cursor = conn.execute(
                    "INSERT INTO x_post_daily_run("
                    "run_date,source_date,status,expected_count,queued_count,error_code,error_message,"
                    "started_at,finished_at,created_at,updated_at"
                    ") VALUES(?,?,'failed_preflight',3,0,?,?,?,?,?,?)",
                    (
                        run_date,
                        source_date,
                        code,
                        message,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                run_id = int(cursor.lastrowid)
                recorded = True
            conn.commit()
        item = self.get_run(run_id)
        item["recorded"] = recorded
        return item

    def query_material_keys(self, material_keys):
        if not isinstance(material_keys, list) or not material_keys or len(material_keys) > 1000:
            raise XPostError(
                "invalid_request",
                "material_keys必须是包含1到1000项的数组",
                400,
            )
        normalized = []
        seen = set()
        for value in material_keys:
            material_key = normalize_material_key(value)
            if material_key not in seen:
                seen.add(material_key)
                normalized.append(material_key)
        placeholders = ",".join("?" for _item in normalized)
        with contextlib.closing(_connect(self.db_path)) as conn:
            occupied = {
                str(row["material_key"])
                for row in conn.execute(
                    "SELECT material_key FROM x_post_queue WHERE material_key IN (%s)" % placeholders,
                    tuple(normalized),
                ).fetchall()
            }
        return [material_key for material_key in normalized if material_key in occupied]

    @staticmethod
    def _pagination(payload):
        payload = payload if isinstance(payload, dict) else {}
        page = _positive_int(payload.get("page", 1), "page")
        page_size = _positive_int(payload.get("page_size", 20), "page_size")
        if page_size > 100:
            raise XPostError("invalid_request", "page_size不能超过100", 400)
        return page, page_size

    def query_logs(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        page, page_size = self._pagination(payload)
        clauses = []
        values = []
        run_date = str(payload.get("run_date", "") or "").strip()
        if run_date:
            clauses.append("q.run_date=?")
            values.append(_date_value(run_date, "run_date"))
        source_date = str(payload.get("source_date", "") or "").strip()
        if source_date:
            clauses.append("q.source_date=?")
            values.append(_date_value(source_date, "source_date"))
        raw_account_id = payload.get("account_id")
        if raw_account_id not in (None, ""):
            clauses.append("q.account_id=?")
            values.append(_positive_int(raw_account_id, "account_id"))
        status = str(payload.get("status", "") or "").strip()
        if status:
            allowed_statuses = {
                "queued", "reserved", "publishing", "media_uploading",
                "post_creating", "published", "failed",
            }
            if status not in allowed_statuses:
                raise XPostError("invalid_request", "status筛选值无效", 400)
            clauses.append("COALESCE(l.status,q.status)=?")
            values.append(status)
        material_id = str(payload.get("material_id", "") or "").strip()
        if material_id:
            clauses.append("q.material_key=?")
            values.append(normalize_material_key(material_id))
        if "unknown_outcome" in payload and payload.get("unknown_outcome") not in (None, ""):
            raw_unknown = payload.get("unknown_outcome")
            if isinstance(raw_unknown, bool):
                unknown_outcome = 1 if raw_unknown else 0
            elif str(raw_unknown).strip() in {"0", "1"}:
                unknown_outcome = int(str(raw_unknown).strip())
            else:
                raise XPostError("invalid_request", "unknown_outcome必须为0或1", 400)
            clauses.append(
                "CASE WHEN l.status='post_creating' OR COALESCE(l.unknown_outcome,0)=1 "
                "THEN 1 ELSE 0 END=?"
            )
            values.append(unknown_outcome)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        select = (
            "SELECT q.id AS queue_id,q.run_id,q.run_date,q.source_date,q.account_id,"
            "q.account_username,q.page_name,q.page_id,q.material_id,q.material_name,q.content_id,"
            "q.material_language,q.drama_name,q.tag,q.candidate_rank,q.spend,"
            "q.facebook_violation_count,q.tiktok_violation_count,q.twitter_violation_count,"
            "q.resource_audit_count,q.dangerous_tag_count,q.status AS queue_status,"
            "l.id AS log_id,COALESCE(l.status,q.status) AS status,COALESCE(l.attempt_count,0) AS attempt_count,"
            "CASE WHEN l.status='post_creating' OR COALESCE(l.unknown_outcome,0)=1 "
            "THEN 1 ELSE 0 END AS unknown_outcome,COALESCE(l.short_url,'') AS short_url,"
            "COALESCE(l.x_post_id,'') AS post_id,COALESCE(l.x_post_url,'') AS preview_url,"
            "COALESCE(l.error_code,'') AS error_code,COALESCE(l.error_message,'') AS error_message,"
            "COALESCE(l.started_at,'') AS started_at,COALESCE(l.published_at,'') AS published_at,"
            "q.created_at,q.updated_at FROM x_post_queue q "
            "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id"
        )
        offset = (page - 1) * page_size
        with contextlib.closing(_connect(self.db_path)) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue q "
                    "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id" + where,
                    tuple(values),
                ).fetchone()[0]
            )
            rows = conn.execute(
                select + where + " ORDER BY q.id DESC LIMIT ? OFFSET ?",
                tuple(values) + (page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = _row_dict(row)
            item["unknown_outcome"] = bool(item["unknown_outcome"])
            item["error_message"] = redact_text(item["error_message"], 500)
            items.append(item)
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": (total + page_size - 1) // page_size,
            },
        }

    def query_runs(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        page, page_size = self._pagination(payload)
        clauses = []
        values = []
        for field in ("run_date", "source_date"):
            raw = str(payload.get(field, "") or "").strip()
            if raw:
                clauses.append("%s=?" % field)
                values.append(_date_value(raw, field))
        status = str(payload.get("status", "") or "").strip()
        if status:
            allowed = {
                "queued", "running", "stopped", "failed_preflight", "completed",
                "completed_with_errors", "needs_review",
            }
            if status not in allowed:
                raise XPostError("invalid_request", "status筛选值无效", 400)
            clauses.append("status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * page_size
        with contextlib.closing(_connect(self.db_path)) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_daily_run" + where,
                    tuple(values),
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT * FROM x_post_daily_run" + where + " ORDER BY run_date DESC,id DESC LIMIT ? OFFSET ?",
                tuple(values) + (page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = _row_dict(row)
            item["error_message"] = redact_text(item["error_message"], 500)
            items.append(item)
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": (total + page_size - 1) // page_size,
            },
        }

    @staticmethod
    def _sync_run(conn, queue_id, timestamp):
        queue = conn.execute("SELECT run_id FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
        if not queue or not queue["run_id"]:
            return
        run_id = int(queue["run_id"])
        counts = conn.execute(
            """
            SELECT
                COUNT(q.id) AS queued_count,
                SUM(CASE WHEN l.status='published' THEN 1 ELSE 0 END) AS published_count,
                SUM(CASE WHEN l.status='failed' AND COALESCE(l.unknown_outcome,0)=0 THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN COALESCE(l.unknown_outcome,0)=1 OR l.status='post_creating' THEN 1 ELSE 0 END) AS unknown_count,
                SUM(CASE WHEN COALESCE(l.attempt_count,0)>0 OR q.status='publishing' THEN 1 ELSE 0 END) AS started_count,
                SUM(CASE WHEN l.error_code='x_post_rate_limited' THEN 1 ELSE 0 END) AS rate_limited_count
            FROM x_post_queue q
            LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
            WHERE q.run_id=?
            """,
            (run_id,),
        ).fetchone()
        run = conn.execute("SELECT * FROM x_post_daily_run WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise XPostError("x_post_storage_conflict", "发布队列关联批次不存在", 500)
        queued_count = int(counts["queued_count"] or 0)
        published_count = int(counts["published_count"] or 0)
        failed_count = int(counts["failed_count"] or 0)
        unknown_count = int(counts["unknown_count"] or 0)
        terminal_count = published_count + failed_count + unknown_count
        expected_count = int(run["expected_count"])
        if unknown_count:
            status = "needs_review"
        elif int(counts["rate_limited_count"] or 0):
            status = "stopped"
        elif terminal_count >= expected_count and published_count == expected_count:
            status = "completed"
        elif terminal_count >= expected_count:
            status = "completed_with_errors"
        elif int(counts["started_count"] or 0):
            status = "running"
        else:
            status = "queued"
        finished_at = timestamp if status in {"completed", "completed_with_errors", "needs_review", "stopped"} else ""
        conn.execute(
            "UPDATE x_post_daily_run SET status=?,queued_count=?,published_count=?,failed_count=?,"
            "unknown_count=?,finished_at=?,updated_at=? WHERE id=?",
            (
                status,
                queued_count,
                published_count,
                failed_count,
                unknown_count,
                finished_at,
                timestamp,
                run_id,
            ),
        )

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
            self._sync_run(conn, row["queue_id"], timestamp)
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
            row = conn.execute("SELECT queue_id FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
            self._sync_run(conn, row["queue_id"], timestamp)
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
            self._sync_run(conn, row["queue_id"], timestamp)
            conn.commit()
        return self.get_log(log_id)

    def mark_post_commit_unknown(
        self, log_id, media_id, post_id, post_url, error_message
    ):
        """Persist the known Post identity when the final ledger commit fails."""
        log_id = _positive_int(log_id, "log_id")
        media_id = _clean_token(media_id, "media id", 128)
        post_id = _clean_token(post_id, "post id", 128)
        post_url = str(post_url or "")
        message = redact_text(error_message, 500)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] == "published":
                if row["x_post_id"] != post_id:
                    conn.rollback()
                    raise XPostError(
                        "x_post_log_conflict", "发布日志已对应其他Post", 409, True
                    )
                conn.commit()
                return _row_dict(row)
            if row["status"] != "post_creating":
                conn.rollback()
                raise XPostError(
                    "x_post_state_conflict",
                    "Post已创建但发布日志状态冲突",
                    409,
                    True,
                )
            conn.execute(
                "UPDATE x_post_publish_log SET status='failed',x_media_id=?,x_post_id=?,"
                "x_post_url=?,error_code='x_post_outcome_unknown',error_message=?,"
                "unknown_outcome=1,updated_at=? WHERE id=?",
                (media_id, post_id, post_url, message, timestamp, log_id),
            )
            conn.execute(
                "UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=?",
                (timestamp, row["queue_id"]),
            )
            self._sync_run(conn, row["queue_id"], timestamp)
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
            # A handled X response (including an explicit 4xx/429 during Post
            # creation) is a known failure. Only the caller can mark the
            # outcome unknown. A process crash leaves ``post_creating`` intact,
            # and _sync_run/replay already treats that residual state as
            # unknown without rewriting an explicit response.
            unknown_outcome = bool(unknown_outcome)
            conn.execute(
                "UPDATE x_post_publish_log SET status='failed',error_code=?,error_message=?,unknown_outcome=?,updated_at=? WHERE id=?",
                (code, message, 1 if unknown_outcome else 0, timestamp, log_id),
            )
            conn.execute("UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=?", (timestamp, row["queue_id"]))
            self._sync_run(conn, row["queue_id"], timestamp)
            conn.commit()
        return self.get_log(log_id)

    def mark_failed_if_reserved(self, log_id, error_code, error_message):
        """Persist a known pre-X failure without overwriting a started attempt."""
        log_id = _positive_int(log_id, "log_id")
        code = _clean_token(error_code or "x_post_failed", "error code", 64)
        message = redact_text(error_message, 500)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
            if row["status"] != "reserved":
                conn.commit()
                return _row_dict(row)
            conn.execute(
                "UPDATE x_post_publish_log SET status='failed',error_code=?,"
                "error_message=?,unknown_outcome=0,updated_at=? WHERE id=? AND status='reserved'",
                (code, message, timestamp, log_id),
            )
            conn.execute(
                "UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=?",
                (timestamp, row["queue_id"]),
            )
            self._sync_run(conn, row["queue_id"], timestamp)
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
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ) as exc:
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
                try:
                    for chunk in response.iter_bytes():
                        if not isinstance(chunk, (bytes, bytearray)):
                            raise XPostError("invalid_media_response", "素材响应分片无效", 502)
                        size += len(chunk)
                        if size > max_bytes:
                            raise XPostError("media_too_large", "素材大小超过限制", 413)
                        handle.write(chunk)
                        digest.update(chunk)
                except XPostError:
                    raise
                except (
                    urllib.error.URLError,
                    http.client.HTTPException,
                    TimeoutError,
                    OSError,
                ) as exc:
                    raise XPostError(
                        "media_download_failed",
                        "素材下载响应中断: %s" % exc,
                        502,
                    ) from None
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
    ffprobe_bin = str(
        os.environ.get("X_POST_FFPROBE_BIN", "/usr/bin/ffprobe")
        or "/usr/bin/ffprobe"
    ).strip()
    if (
        not ffprobe_bin
        or "\x00" in ffprobe_bin
        or not (Path(ffprobe_bin).is_absolute() or ffprobe_bin.startswith("/"))
    ):
        raise XPostError("media_probe_failed", "ffprobe路径配置无效", 500)
    command = [
        ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
    ]
    try:
        completed = run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1, min(int(timeout), 120)),
            check=False,
            close_fds=True,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
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


def _is_x_rate_limit_payload(payload):
    if not isinstance(payload, dict):
        return False
    candidates = [payload]
    errors = payload.get("errors")
    if isinstance(errors, list):
        candidates.extend(item for item in errors if isinstance(item, dict))
    allowed_types = {
        "https://api.x.com/2/problems/usage-capped",
        "https://api.x.com/2/problems/rate-limit-exceeded",
    }
    for item in candidates:
        if str(item.get("type", "") or "").rstrip("/") in allowed_types:
            return True
        if str(item.get("code", "") or "") == "88":
            return True
    return False


def _json_response(response, expected_status, operation, unknown_on_success_shape=False):
    raw = response.body or b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        if response.status == 429:
            raise XPostError(
                "x_post_rate_limited",
                "%s触发X限流" % operation,
                429,
                False,
            ) from None
        if (
            unknown_on_success_shape
            and (response.status == expected_status or response.status >= 500)
        ):
            raise XPostError("x_post_outcome_unknown", "%s响应无法确认" % operation, 502, True) from None
        raise XPostError("x_upstream_error", "%s返回非JSON响应" % operation, 502) from None
    if response.status == 429 or _is_x_rate_limit_payload(payload):
        raise XPostError(
            "x_post_rate_limited",
            "%s触发X限流或用量上限" % operation,
            429,
            False,
        )
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
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
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
    storage_guard=None, durable_storage=None,
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

    work_dir = None
    confirmed_post = None
    confirmed_post_url = ""
    confirmed_media_id = ""
    try:
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

        if callable(storage_guard):
            storage_guard()
        write_short_redirect(
            public_root,
            log["id"],
            long_url,
            durable_storage=durable_storage,
        )

        if durable_storage is not None:
            layout = _validate_post_storage_layout(
                public_root,
                mount_root=durable_storage.get("mount_root", DEFAULT_STORAGE_MOUNT_ROOT),
                storage_root=durable_storage.get("storage_root", DEFAULT_STORAGE_ROOT),
            )
            work_root = layout["media_work"]
            if (
                work_root.resolve(strict=True).parent != layout["storage"]
                or work_root.stat().st_dev != layout["storage"].stat().st_dev
            ):
                raise XPostError("x_post_storage_unavailable", "X Post媒体工作目录无效", 503)
        else:
            work_root = Path(public_root).resolve().parent / "media-work"
            work_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="log-%s-" % log["id"], dir=str(work_root)))

        media = download_media(
            queue["material_url"], work_dir / "material.mp4", allowed_media_hosts,
            max_bytes=max_media_bytes, timeout=timeout, http_client=http_client,
        )
        expected_sha256 = str(queue.get("preflight_sha256", "") or "").lower()
        expected_size = int(queue.get("preflight_size", 0) or 0)
        if queue.get("run_id") and (
            not expected_sha256
            or expected_size <= 0
            or not secrets.compare_digest(expected_sha256, str(media["sha256"]).lower())
            or expected_size != int(media["size"])
        ):
            raise XPostError(
                "media_preflight_changed",
                "素材内容与建计划前的预检指纹不一致",
                409,
            )
        probe_media(media["path"], max_bytes=max_media_bytes, timeout=timeout)
        if callable(storage_guard):
            storage_guard()
        store.mark_publishing(log["id"])
        x_client = XApiClient(http_client=http_client, sleeper=sleeper, timeout=timeout)
        uploaded = x_client.upload_media(access_token, media["path"], media_type=media["media_type"])
        if callable(storage_guard):
            storage_guard()
        store.mark_media_uploaded(log["id"], uploaded["media_id"])
        created = x_client.create_post(access_token, post_text, uploaded["media_id"])
        post_url = "https://x.com/%s/status/%s" % (username, created["post_id"])
        confirmed_post = created["post_id"]
        confirmed_post_url = post_url
        confirmed_media_id = uploaded["media_id"]
        published = store.mark_published(log["id"], uploaded["media_id"], created["post_id"], post_url)
        return _result_from_log(published)
    except XPostError as exc:
        if confirmed_post is not None:
            try:
                recovered = store.mark_post_commit_unknown(
                    log["id"],
                    confirmed_media_id,
                    confirmed_post,
                    confirmed_post_url,
                    "Post已创建，但最终发布日志写入失败: %s" % exc,
                )
                if recovered["status"] == "published":
                    return _result_from_log(recovered)
            except Exception:
                # The existing post_creating state is itself a durable
                # no-retry marker if the reconciliation write is unavailable.
                pass
            raise XPostError(
                "x_post_outcome_unknown",
                "X已返回Post ID，但最终发布日志写入失败，请人工核对",
                503,
                True,
            ) from None
        failed = store.mark_failed(log["id"], exc.code, str(exc), exc.unknown_outcome)
        raise XPostError(exc.code, str(exc), exc.status, bool(failed["unknown_outcome"])) from None
    except Exception as exc:
        if confirmed_post is not None:
            try:
                recovered = store.mark_post_commit_unknown(
                    log["id"],
                    confirmed_media_id,
                    confirmed_post,
                    confirmed_post_url,
                    "Post已创建，但最终发布日志写入异常: %s" % exc,
                )
                if recovered["status"] == "published":
                    return _result_from_log(recovered)
            except Exception:
                pass
            raise XPostError(
                "x_post_outcome_unknown",
                "X已返回Post ID，但最终发布日志写入异常，请人工核对",
                503,
                True,
            ) from None
        current = store.get_log(log["id"])
        unknown = bool(current["unknown_outcome"]) or current["status"] == "post_creating"
        code = "x_post_outcome_unknown" if unknown else "x_post_internal_error"
        failed = store.mark_failed(log["id"], code, str(exc), unknown)
        raise XPostError(
            code,
            "发布结果未知，请人工核对" if unknown else "发布处理失败: %s" % exc,
            503 if unknown else 500,
            bool(failed["unknown_outcome"]),
        ) from None
    finally:
        if work_dir is not None:
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

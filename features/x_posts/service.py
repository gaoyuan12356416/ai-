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
DEFAULT_SHORT_BASE_URL = "https://gy.g2flow.com/s2l"
DEFAULT_STORAGE_MOUNT_ROOT = "/mnt/data-disk"
DEFAULT_STORAGE_ROOT = "/mnt/data-disk/x-post-automation"
DEFAULT_MAX_MEDIA_BYTES = 512 * 1024 * 1024
STANDARD_MAX_DURATION_SECONDS = 140.0
# X's Premium product contract currently permits videos up to four hours on
# supported clients.  The v2 media API documents its own tighter 512 MiB byte
# ceiling but no separate duration ceiling for ``amplify_video``; a production
# canary also confirmed a raw 763.938-second upload and Post readback.  Keep the
# entitlement token-scoped and the API byte/codec gates unchanged.
PREMIUM_MAX_DURATION_SECONDS = 4.0 * 60.0 * 60.0
STANDARD_MEDIA_CATEGORY = "tweet_video"
PREMIUM_MEDIA_CATEGORY = "amplify_video"
MEDIA_CATEGORIES = frozenset(
    {STANDARD_MEDIA_CATEGORY, PREMIUM_MEDIA_CATEGORY}
)
PREMIUM_SUBSCRIPTION_TYPES = frozenset(
    {"basic", "premium", "premium_plus"}
)
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
SQLITE_QUERY_BATCH_SIZE = 900
MAX_DAILY_BATCH_SIZE = 50
MAX_SCHEDULE_ACCOUNTS = 50
MAX_RANDOM_DAILY_COUNT = 24
RANDOM_PUBLISH_MIN_GAP_MINUTES = 60
MAX_DRAMA_POOL_BATCH_DELETE_SIZE = 100
MAX_DRAMA_POOL_REPLAY_SIZE = 100
MAX_MANUAL_PUBLISH_SIZE = 50
MANUAL_TRIGGER_SOURCE = "manual"
AUTO_TEMPLATE_TRIGGER_SOURCE = "auto_template"
MANUAL_TRIGGER_SOURCES = frozenset(
    {MANUAL_TRIGGER_SOURCE, AUTO_TEMPLATE_TRIGGER_SOURCE}
)
AUTO_TEMPLATE_MAX_DURATION_SECONDS = 600.0
SCHEDULE_TIMEZONE = "Asia/Shanghai"
SCHEDULE_SOURCE_TYPES = frozenset({"material", "drama"})
SCHEDULE_MODES = frozenset({"fixed", "random"})
DRAMA_REPLAY_REASON = "operator_full_replay_v1"
DRAMA_POOL_DELETABLE_STATUSES = frozenset(
    {"pending", "validation_failed"}
)
DRAMA_POOL_DETERMINISTIC_REJECTION_CODES = frozenset(
    {
        "drama_episode_gap",
        "drama_episode_url_ambiguous",
        "drama_id_invalid",
        "drama_metadata_ambiguous",
        "drama_no_free_episodes",
        "drama_not_found",
        "drama_progress_invalid",
        "drama_resource_invalid",
        "invalid_media_duration",
        "invalid_media_frame_rate",
        "invalid_media_scan",
        "invalid_media_type",
        "invalid_media_url",
        "media_host_not_allowed",
        "media_too_large",
        "source_not_repairable",
        "x_post_daily_copy_validation_failed",
    }
)
PRE_X_RECOVERABLE_ERROR_CODES = frozenset(
    {
        "invalid_short_base_url",
    }
)
FAILED_PREFLIGHT_RECOVERY_REASON = "operator_same_day_compensation_v1"
FAILED_PREFLIGHT_RECOVERABLE_ERROR_CODES = frozenset(
    {
        "x_token_missing",
        "x_upstream_error",
    }
)
DRAMA_POOL_RETRYABLE_VALIDATION_CODES = frozenset(
    {"x_long_video_requires_premium"}
)
FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON = (
    "operator_same_day_corrective_retry_v1"
)
FAILED_PREFLIGHT_CORRECTIVE_ERROR_MESSAGES = {
    "x_post_pool_invalid_response": (
        "Material pool FIFO order is invalid",
    ),
    "x_post_schedule_preflight_failed": (
        "read-only candidate query failed: OperationalError",
    ),
    "x_long_video_requires_premium": (
        "Videos longer than 140 seconds require a token-confirmed X Premium subscription",
    ),
}
FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON = (
    "operator_same_day_verified_repair_retry_v1"
)
FAILED_PREFLIGHT_VERIFIED_REPAIR_ERROR_MESSAGES = {
    "x_post_media_repair_invalid_response": (
        "unassigned Premium drama routing failed: "
        "X media repair probe does not meet the X video contract",
    ),
}
FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON = (
    "operator_same_day_codefix_compensation_v1"
)
FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON = (
    "operator_same_day_drama_capability_fallback_v1"
)
FAILED_PREFLIGHT_DRAMA_CAPABILITY_ERROR_MESSAGES = {
    "x_long_video_requires_premium": (
        "Videos longer than 140 seconds require a token-confirmed X Premium subscription",
    ),
}
FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON = (
    "operator_same_day_preflight_token_refresh_v1"
)
FAILED_PREFLIGHT_TOKEN_REFRESH_ERROR_MESSAGES = {
    "x_account_not_publishable": (
        "X账号当前不可用于手动发布",
    ),
}
FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON = (
    "operator_same_day_transient_media_retry_v1"
)
FAILED_PREFLIGHT_TRANSIENT_MEDIA_ERROR_MESSAGES = {
    "media_download_failed": (
        "素材下载响应中断:",
        "素材下载网络失败:",
        "素材下载失败(HTTP ",
    ),
}

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
    "catchup_run_id",
    "schedule_run_id",
    "manual_run_id",
    "run_date",
    "source_type",
    "body_template",
    "material_key",
    "episode_key",
    "drama_replay_generation",
    "pool_item_id",
    "drama_pool_item_id",
    "pool_created_at",
    "drama_pool_created_at",
    "episode_number",
    "name_tag",
    "candidate_rank",
    "spend",
    "original_material_url",
    "media_repair_trigger_code",
    "media_repair_job_key",
    "media_repair_profile",
    "media_repair_source_sha256",
    "preflight_sha256",
    "preflight_size",
    "preflight_duration",
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

# X keeps these historical validation results as audit evidence, but they no
# longer make a material unavailable or change newest-first ordering.
NONBLOCKING_MATERIAL_VALIDATION_CODES = frozenset(
    {
        "material_has_violation",
        "material_source_tag_unsafe",
        "material_tag_unsafe",
        "x_long_video_requires_premium",
    }
)
_NONBLOCKING_MATERIAL_VALIDATION_SQL = "(" + ",".join(
    "'%s'" % code for code in sorted(NONBLOCKING_MATERIAL_VALIDATION_CODES)
) + ")"

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
        or parsed.hostname != "gy.g2flow.com"
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


def _tweet_char_weight(char):
    value = ord(char)
    if (
        value <= 0x10FF
        or 0x2000 <= value <= 0x200D
        or 0x2010 <= value <= 0x201F
        or 0x2032 <= value <= 0x2037
    ):
        return 1
    return 2


X_POST_HASHTAGS = "#shortdrama #shortfilms #tvdrama #aidrama #dramawave"
DEFAULT_MATERIAL_POST_TEMPLATE = (
    "🎬 {{drama_name}}\n"
    "{{desc}}\n\n"
    + X_POST_HASHTAGS
)
DEFAULT_DRAMA_POST_TEMPLATE = (
    "🎬 {{drama_name}}\n"
    "Episode {{episode_number}}\n"
    "{{desc}}\n\n"
    + X_POST_HASHTAGS
)
POST_TEMPLATE_MACRO_RE = re.compile(r"\{\{([a-z_]+)\}\}")
POST_TEMPLATE_ALLOWED_MACROS = frozenset(
    {"drama_name", "episode_number", "desc", "url"}
)


def _default_post_template(source_type):
    source_type = _schedule_source_type(source_type)
    return (
        DEFAULT_DRAMA_POST_TEMPLATE
        if source_type == "drama"
        else DEFAULT_MATERIAL_POST_TEMPLATE
    )


def _normalize_post_template(value, source_type):
    source_type = _schedule_source_type(source_type)
    if value in (None, ""):
        value = _default_post_template(source_type)
    template = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not template or len(template) > 2000:
        raise XPostError(
            "invalid_post_template",
            "X Post描述模板不能为空且不能超过2000个字符",
            400,
        )
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in template):
        raise XPostError("invalid_post_template", "X Post描述模板包含无效字符", 400)
    macros = POST_TEMPLATE_MACRO_RE.findall(template)
    unmatched = POST_TEMPLATE_MACRO_RE.sub("", template)
    if "{{" in unmatched or "}}" in unmatched:
        raise XPostError(
            "invalid_post_template",
            "X Post描述模板包含不完整或格式无效的宏",
            400,
        )
    unknown = sorted(set(macros) - POST_TEMPLATE_ALLOWED_MACROS)
    if unknown:
        raise XPostError(
            "invalid_post_template",
            "X Post描述模板包含不支持的宏: %s" % "、".join(unknown),
            400,
        )
    required = {"drama_name", "desc"}
    if source_type == "drama":
        required.add("episode_number")
    missing = sorted(required - set(macros))
    if missing:
        raise XPostError(
            "invalid_post_template",
            "X Post描述模板缺少必需宏: %s" % "、".join(missing),
            400,
        )
    repeated = sorted(macro for macro in set(macros) if macros.count(macro) > 1)
    if repeated:
        raise XPostError(
            "invalid_post_template",
            "X Post描述模板宏不能重复: %s" % "、".join(repeated),
            400,
        )
    if source_type == "material" and "episode_number" in macros:
        raise XPostError(
            "invalid_post_template",
            "素材池模板不支持episode_number宏",
            400,
        )
    return template


def _tweet_text_weight(value):
    return sum(_tweet_char_weight(char) for char in str(value or ""))


def _normalize_post_field(value, label, maximum):
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 for char in normalized)
    ):
        raise XPostError("invalid_request", "%s无效" % label, 400)
    return normalized


def _render_post_text(
    short_url,
    drama_name,
    description,
    episode_number=None,
    body_template=None,
):
    """Render a validated frozen template while truncating only ``desc``."""
    parsed = urllib.parse.urlsplit(str(short_url or ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise XPostError("invalid_request", "短链无效", 400)
    normalized_name = _normalize_post_field(drama_name, "剧名", 500)
    normalized_description = _normalize_post_field(description, "剧描述", 10000)
    source_type = "drama" if episode_number is not None else "material"
    template = _normalize_post_template(body_template, source_type)
    substitutions = {
        "drama_name": normalized_name,
        "url": str(short_url),
    }
    if episode_number is not None:
        substitutions["episode_number"] = str(_positive_int(
            episode_number,
            "episode_number",
        ))
    before_description, after_description = template.split("{{desc}}", 1)
    for macro, replacement in substitutions.items():
        marker = "{{%s}}" % macro
        before_description = before_description.replace(marker, replacement)
        after_description = after_description.replace(marker, replacement)
    mandatory_weight = (
        _tweet_text_weight(before_description)
        + _tweet_text_weight(after_description)
    )
    remaining = 280 - mandatory_weight
    if remaining < 1:
        raise XPostError("x_post_copy_too_long", "X Post固定文案超过字数限制", 409)
    description_weight = _tweet_text_weight(normalized_description)
    if description_weight <= remaining:
        rendered_description = normalized_description
    else:
        ellipsis = "…"
        budget = remaining - _tweet_char_weight(ellipsis)
        if budget < 1:
            raise XPostError("x_post_copy_too_long", "X Post没有可用的描述空间", 409)
        selected = []
        used = 0
        for char in normalized_description:
            weight = _tweet_char_weight(char)
            if used + weight > budget:
                break
            selected.append(char)
            used += weight
        rendered_description = "".join(selected).rstrip() + ellipsis
    if not rendered_description.strip(" …"):
        raise XPostError("x_post_copy_too_long", "X Post描述截断后为空", 409)
    return before_description + rendered_description + after_description


def build_post_text(short_url, drama_name, description, body_template=None):
    return _render_post_text(
        short_url,
        drama_name,
        description,
        body_template=body_template,
    )


def build_drama_episode_post_text(
    short_url,
    sub_num,
    drama_name,
    description,
    body_template=None,
):
    return _render_post_text(
        short_url,
        drama_name,
        description,
        episode_number=sub_num,
        body_template=body_template,
    )


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


def _catchup_reason(value):
    reason = str(value or "").strip()
    if reason != "scope_expansion_v1":
        raise XPostError(
            "invalid_request",
            "补发原因必须为scope_expansion_v1",
            400,
        )
    return reason


def _configured_account_scope(values):
    if not isinstance(values, (list, tuple)):
        raise XPostError(
            "invalid_request",
            "configured_account_ids必须为有序数组",
            400,
        )
    normalized = tuple(
        _positive_int(value, "configured_account_ids")
        for value in values
    )
    if (
        not normalized
        or len(normalized) > MAX_DAILY_BATCH_SIZE
        or len(set(normalized)) != len(normalized)
    ):
        raise XPostError(
            "invalid_request",
            "configured_account_ids必须包含1到%s个互异正整数"
            % MAX_DAILY_BATCH_SIZE,
            400,
        )
    return normalized


def _stored_account_ids(value, expected_count=None):
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise XPostError(
            "x_post_storage_conflict",
            "补发批次账号快照无效",
            500,
        ) from None
    try:
        normalized = _configured_account_scope(decoded)
    except XPostError:
        raise XPostError(
            "x_post_storage_conflict",
            "补发批次账号快照无效",
            500,
        ) from None
    if expected_count is not None and len(normalized) != int(expected_count):
        raise XPostError(
            "x_post_storage_conflict",
            "补发批次账号快照数量异常",
            500,
        )
    return normalized


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
    """Normalize audit-only X compliance evidence."""
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


def _material_validation_is_blocking(error_code):
    return bool(error_code) and error_code not in NONBLOCKING_MATERIAL_VALIDATION_CODES


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
                CREATE TABLE IF NOT EXISTS x_post_catchup_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_run_id INTEGER NOT NULL UNIQUE,
                    catchup_key TEXT NOT NULL UNIQUE,
                    run_date TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    reason TEXT NOT NULL
                        CHECK(reason='scope_expansion_v1'),
                    account_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_count INTEGER NOT NULL,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_run_id) REFERENCES x_post_daily_run(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_material_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_key TEXT NOT NULL UNIQUE,
                    material_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unpublished'
                        CHECK(status IN ('unpublished','published')),
                    published_at TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_message TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_manual_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    trigger_source TEXT NOT NULL DEFAULT 'manual'
                        CHECK(trigger_source IN ('manual','auto_template')),
                    external_task_key TEXT NOT NULL DEFAULT '',
                    template_ref TEXT NOT NULL DEFAULT '',
                    template_version INTEGER NOT NULL DEFAULT 0
                        CHECK(template_version>=0),
                    body_template_sha256 TEXT NOT NULL DEFAULT '',
                    run_date TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    material_ids_json TEXT NOT NULL,
                    body_template TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN (
                            'queued','running','completed',
                            'completed_with_errors','needs_review',
                            'stopped','failed_preflight'
                        )),
                    expected_count INTEGER NOT NULL,
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
                catchup_run_id INTEGER,
                manual_run_id INTEGER,
                run_date TEXT NOT NULL DEFAULT '',
                material_key TEXT NOT NULL DEFAULT '',
                pool_item_id INTEGER,
                pool_created_at TEXT NOT NULL DEFAULT '',
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
                original_material_url TEXT NOT NULL DEFAULT '',
                media_repair_trigger_code TEXT NOT NULL DEFAULT '',
                media_repair_job_key TEXT NOT NULL DEFAULT '',
                media_repair_profile TEXT NOT NULL DEFAULT '',
                media_repair_source_sha256 TEXT NOT NULL DEFAULT '',
                preflight_sha256 TEXT NOT NULL DEFAULT '',
                preflight_size INTEGER NOT NULL DEFAULT 0,
                preflight_duration REAL NOT NULL DEFAULT 0,
                facebook_violation_count INTEGER NOT NULL DEFAULT 0,
                tiktok_violation_count INTEGER NOT NULL DEFAULT 0,
                twitter_violation_count INTEGER NOT NULL DEFAULT 0,
                resource_audit_count INTEGER NOT NULL DEFAULT 0,
                dangerous_tag_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES x_post_daily_run(id),
                FOREIGN KEY(catchup_run_id) REFERENCES x_post_catchup_run(id),
                FOREIGN KEY(manual_run_id) REFERENCES x_post_manual_run(id),
                FOREIGN KEY(pool_item_id) REFERENCES x_post_material_pool(id)
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_config (
                    source_type TEXT PRIMARY KEY
                        CHECK(source_type IN ('material','drama')),
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(enabled IN (0,1)),
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    account_ids_json TEXT NOT NULL DEFAULT '[]',
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    schedule_mode TEXT NOT NULL DEFAULT 'fixed'
                        CHECK(schedule_mode IN ('fixed','random')),
                    random_daily_count INTEGER NOT NULL DEFAULT 0
                        CHECK(random_daily_count BETWEEN 0 AND 24),
                    random_effective_date TEXT NOT NULL DEFAULT '',
                    body_template TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_key TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL
                        CHECK(source_type IN ('material','drama')),
                    run_date TEXT NOT NULL,
                    publish_time TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    config_version INTEGER NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    schedule_mode TEXT NOT NULL DEFAULT 'fixed'
                        CHECK(schedule_mode IN ('fixed','random')),
                    body_template TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_count INTEGER NOT NULL,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type,run_date,publish_time)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_recovery_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_compensation_v1'),
                    actor TEXT NOT NULL,
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL,
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id) REFERENCES x_post_schedule_run(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_corrective_retry_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    initial_recovery_audit_id INTEGER NOT NULL,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_corrective_retry_v1'),
                    actor TEXT NOT NULL,
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL,
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id) REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(initial_recovery_audit_id)
                        REFERENCES x_post_schedule_recovery_audit(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_verified_repair_retry_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    initial_recovery_audit_id INTEGER NOT NULL,
                    corrective_retry_audit_id INTEGER NOT NULL,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_verified_repair_retry_v1'),
                    actor TEXT NOT NULL,
                    verified_repair_job_key TEXT NOT NULL
                        CHECK(length(verified_repair_job_key)=64),
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL
                        CHECK(previous_error_code='x_post_media_repair_invalid_response'),
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id) REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(initial_recovery_audit_id)
                        REFERENCES x_post_schedule_recovery_audit(id),
                    FOREIGN KEY(corrective_retry_audit_id)
                        REFERENCES x_post_schedule_corrective_retry_audit(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_codefix_compensation_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_schedule_run_id INTEGER NOT NULL UNIQUE,
                    compensation_schedule_run_id INTEGER NOT NULL UNIQUE,
                    verified_repair_retry_audit_id INTEGER NOT NULL,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_codefix_compensation_v1'),
                    actor TEXT NOT NULL,
                    deployed_commit TEXT NOT NULL
                        CHECK(length(deployed_commit)=40),
                    verified_repair_job_key TEXT NOT NULL
                        CHECK(length(verified_repair_job_key)=64),
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL
                        CHECK(previous_error_code='x_post_media_repair_invalid_response'),
                    previous_error_message TEXT NOT NULL,
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(original_schedule_run_id)
                        REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(compensation_schedule_run_id)
                        REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(verified_repair_retry_audit_id)
                        REFERENCES x_post_schedule_verified_repair_retry_audit(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_drama_capability_recovery_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_drama_capability_fallback_v1'),
                    actor TEXT NOT NULL,
                    deployed_commit TEXT NOT NULL
                        CHECK(length(deployed_commit)=40),
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL
                        CHECK(previous_error_code='x_long_video_requires_premium'),
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id)
                        REFERENCES x_post_schedule_run(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_token_refresh_recovery_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    drama_capability_recovery_audit_id INTEGER NOT NULL UNIQUE,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_preflight_token_refresh_v1'),
                    actor TEXT NOT NULL,
                    deployed_commit TEXT NOT NULL
                        CHECK(length(deployed_commit)=40),
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL
                        CHECK(previous_error_code='x_account_not_publishable'),
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id)
                        REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(drama_capability_recovery_audit_id)
                        REFERENCES x_post_schedule_drama_capability_recovery_audit(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_transient_media_recovery_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_run_id INTEGER NOT NULL UNIQUE,
                    token_refresh_recovery_audit_id INTEGER NOT NULL UNIQUE,
                    recovery_reason TEXT NOT NULL
                        CHECK(recovery_reason='operator_same_day_transient_media_retry_v1'),
                    actor TEXT NOT NULL,
                    deployed_commit TEXT NOT NULL
                        CHECK(length(deployed_commit)=40),
                    previous_status TEXT NOT NULL
                        CHECK(previous_status='failed_preflight'),
                    previous_error_code TEXT NOT NULL
                        CHECK(previous_error_code='media_download_failed'),
                    previous_error_message TEXT NOT NULL,
                    previous_started_at TEXT NOT NULL DEFAULT '',
                    previous_finished_at TEXT NOT NULL DEFAULT '',
                    validated_queue_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_queue_count=0),
                    validated_log_count INTEGER NOT NULL DEFAULT 0
                        CHECK(validated_log_count=0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id)
                        REFERENCES x_post_schedule_run(id),
                    FOREIGN KEY(token_refresh_recovery_audit_id)
                        REFERENCES x_post_schedule_token_refresh_recovery_audit(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_schedule_random_plan (
                    source_type TEXT NOT NULL
                        CHECK(source_type IN ('material','drama')),
                    run_date TEXT NOT NULL,
                    config_version INTEGER NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    body_template TEXT NOT NULL,
                    publish_times_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_type,run_date)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_drama_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_id TEXT NOT NULL UNIQUE,
                    app_id INTEGER NOT NULL DEFAULT 1479,
                    drama_name TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    labels TEXT NOT NULL DEFAULT '',
                    name_tag TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending','active','completed',
                            'validation_failed','needs_review'
                        )),
                    free_episode_count INTEGER NOT NULL DEFAULT 0,
                    next_sub_number INTEGER NOT NULL DEFAULT 1,
                    published_episode_count INTEGER NOT NULL DEFAULT 0,
                    replay_generation INTEGER NOT NULL DEFAULT 1
                        CHECK(replay_generation>0),
                    assigned_account_id INTEGER NOT NULL DEFAULT 0
                        CHECK(assigned_account_id>=0),
                    assigned_at TEXT NOT NULL DEFAULT '',
                    assigned_source_queue_id INTEGER,
                    priority_at TEXT NOT NULL DEFAULT '',
                    priority_by_user_id TEXT NOT NULL DEFAULT '',
                    priority_by_name TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_message TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_post_drama_replay_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_item_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL,
                    from_generation INTEGER NOT NULL
                        CHECK(from_generation>0),
                    to_generation INTEGER NOT NULL
                        CHECK(to_generation=from_generation+1),
                    from_status TEXT NOT NULL,
                    from_free_episode_count INTEGER NOT NULL
                        CHECK(from_free_episode_count>0),
                    from_next_sub_number INTEGER NOT NULL
                        CHECK(from_next_sub_number>1),
                    from_published_episode_count INTEGER NOT NULL
                        CHECK(from_published_episode_count>0),
                    from_assigned_account_id INTEGER NOT NULL
                        CHECK(from_assigned_account_id>0),
                    from_assigned_at TEXT NOT NULL DEFAULT '',
                    from_assigned_source_queue_id INTEGER,
                    actor_user_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    reason TEXT NOT NULL
                        CHECK(reason='operator_full_replay_v1'),
                    created_at TEXT NOT NULL,
                    UNIQUE(pool_item_id,to_generation),
                    FOREIGN KEY(pool_item_id)
                        REFERENCES x_post_drama_pool(id)
                )
                """
            )
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(x_post_queue)")}
            additive_columns = {
                "account_username": "TEXT NOT NULL DEFAULT ''",
                "run_id": "INTEGER",
                "catchup_run_id": "INTEGER",
                "schedule_run_id": "INTEGER",
                "manual_run_id": "INTEGER",
                "run_date": "TEXT NOT NULL DEFAULT ''",
                "source_type": "TEXT NOT NULL DEFAULT 'material'",
                "body_template": "TEXT NOT NULL DEFAULT ''",
                "material_key": "TEXT NOT NULL DEFAULT ''",
                "episode_key": "TEXT NOT NULL DEFAULT ''",
                "drama_replay_generation": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(drama_replay_generation>=0)"
                ),
                "pool_item_id": "INTEGER",
                "drama_pool_item_id": "INTEGER",
                "pool_created_at": "TEXT NOT NULL DEFAULT ''",
                "drama_pool_created_at": "TEXT NOT NULL DEFAULT ''",
                "episode_number": "INTEGER NOT NULL DEFAULT 0",
                "name_tag": "TEXT NOT NULL DEFAULT ''",
                "candidate_rank": "INTEGER NOT NULL DEFAULT 0",
                "spend": "REAL NOT NULL DEFAULT 0",
                "original_material_url": "TEXT NOT NULL DEFAULT ''",
                "media_repair_trigger_code": "TEXT NOT NULL DEFAULT ''",
                "media_repair_job_key": "TEXT NOT NULL DEFAULT ''",
                "media_repair_profile": "TEXT NOT NULL DEFAULT ''",
                "media_repair_source_sha256": "TEXT NOT NULL DEFAULT ''",
                "preflight_sha256": "TEXT NOT NULL DEFAULT ''",
                "preflight_size": "INTEGER NOT NULL DEFAULT 0",
                "preflight_duration": "REAL NOT NULL DEFAULT 0",
                "facebook_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "tiktok_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "twitter_violation_count": "INTEGER NOT NULL DEFAULT 0",
                "resource_audit_count": "INTEGER NOT NULL DEFAULT 0",
                "dangerous_tag_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additive_columns.items():
                if name not in queue_columns:
                    conn.execute("ALTER TABLE x_post_queue ADD COLUMN %s %s" % (name, definition))

            manual_run_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_manual_run)"
                )
            }
            manual_run_additive_columns = {
                "trigger_source": (
                    "TEXT NOT NULL DEFAULT 'manual' "
                    "CHECK(trigger_source IN ('manual','auto_template'))"
                ),
                "external_task_key": "TEXT NOT NULL DEFAULT ''",
                "template_ref": "TEXT NOT NULL DEFAULT ''",
                "template_version": (
                    "INTEGER NOT NULL DEFAULT 0 CHECK(template_version>=0)"
                ),
                "body_template_sha256": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in manual_run_additive_columns.items():
                if name not in manual_run_columns:
                    conn.execute(
                        "ALTER TABLE x_post_manual_run ADD COLUMN %s %s"
                        % (name, definition)
                    )

            schedule_config_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_schedule_config)"
                )
            }
            if "body_template" not in schedule_config_columns:
                conn.execute(
                    "ALTER TABLE x_post_schedule_config "
                    "ADD COLUMN body_template TEXT NOT NULL DEFAULT ''"
                )
            schedule_config_additive_columns = {
                "schedule_mode": (
                    "TEXT NOT NULL DEFAULT 'fixed' "
                    "CHECK(schedule_mode IN ('fixed','random'))"
                ),
                "random_daily_count": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(random_daily_count BETWEEN 0 AND 24)"
                ),
                "random_effective_date": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in schedule_config_additive_columns.items():
                if name not in schedule_config_columns:
                    conn.execute(
                        "ALTER TABLE x_post_schedule_config ADD COLUMN %s %s"
                        % (name, definition)
                    )
            schedule_run_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_schedule_run)"
                )
            }
            if "body_template" not in schedule_run_columns:
                conn.execute(
                    "ALTER TABLE x_post_schedule_run "
                    "ADD COLUMN body_template TEXT NOT NULL DEFAULT ''"
                )
            if "schedule_mode" not in schedule_run_columns:
                conn.execute(
                    "ALTER TABLE x_post_schedule_run "
                    "ADD COLUMN schedule_mode TEXT NOT NULL DEFAULT 'fixed' "
                    "CHECK(schedule_mode IN ('fixed','random'))"
                )

            drama_pool_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_drama_pool)"
                )
            }
            drama_pool_additive_columns = {
                "assigned_account_id": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(assigned_account_id>=0)"
                ),
                "assigned_at": "TEXT NOT NULL DEFAULT ''",
                "assigned_source_queue_id": "INTEGER",
                "replay_generation": (
                    "INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(replay_generation>0)"
                ),
                "priority_at": "TEXT NOT NULL DEFAULT ''",
                "priority_by_user_id": "TEXT NOT NULL DEFAULT ''",
                "priority_by_name": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in drama_pool_additive_columns.items():
                if name not in drama_pool_columns:
                    conn.execute(
                        "ALTER TABLE x_post_drama_pool ADD COLUMN %s %s"
                        % (name, definition)
                    )

            conn.execute(
                "UPDATE x_post_queue SET drama_replay_generation=1 "
                "WHERE source_type='drama' "
                "AND drama_replay_generation=0"
            )
            migration_timestamp = utc_now()
            for source_type in sorted(SCHEDULE_SOURCE_TYPES):
                conn.execute(
                    "INSERT OR IGNORE INTO x_post_schedule_config("
                    "source_type,enabled,timezone,account_ids_json,publish_times_json,"
                    "schedule_mode,random_daily_count,random_effective_date,"
                    "body_template,version,created_at,updated_at"
                    ") VALUES(?,0,?,'[]','[]','fixed',0,'',?,1,?,?)",
                    (
                        source_type,
                        SCHEDULE_TIMEZONE,
                        _default_post_template(source_type),
                        migration_timestamp,
                        migration_timestamp,
                    ),
                )
                conn.execute(
                    "UPDATE x_post_schedule_config SET body_template=? "
                    "WHERE source_type=? AND body_template=''",
                    (_default_post_template(source_type), source_type),
                )

            legacy_rows = conn.execute(
                "SELECT id,source_type,material_id,material_key,episode_key,"
                "content_id,episode_number,drama_replay_generation,"
                "run_date,created_at "
                "FROM x_post_queue ORDER BY id"
            ).fetchall()
            for row in legacy_rows:
                source_type = str(row["source_type"] or "material").strip()
                if source_type == "drama":
                    content_id = _clean_token(
                        row["content_id"], "content_id", 128
                    )
                    episode_number = _positive_int(
                        row["episode_number"], "episode_number"
                    )
                    replay_generation = _positive_int(
                        row["drama_replay_generation"],
                        "drama_replay_generation",
                    )
                    expected_episode_key = _drama_episode_key(
                        content_id,
                        episode_number,
                        replay_generation,
                    )
                    if str(row["episode_key"] or "") != expected_episode_key:
                        raise XPostError(
                            "x_post_storage_conflict",
                            "历史X短剧队列episode_key不一致，迁移已中止",
                            500,
                        )
                    material_key = ""
                elif source_type == "material":
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
                else:
                    raise XPostError(
                        "x_post_storage_conflict",
                        "历史X发布队列source_type无效，迁移已中止",
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
                normalized_values = (source_type, material_key, run_date)
                stored_values = (
                    str(row["source_type"] or ""),
                    str(row["material_key"] or ""),
                    str(row["run_date"] or ""),
                )
                if stored_values != normalized_values:
                    conn.execute(
                        "UPDATE x_post_queue SET source_type=?,material_key=?,"
                        "run_date=? WHERE id=?",
                        normalized_values + (row["id"],),
                    )

            # The legacy short-drama scheduler could spread consecutive
            # episodes of one drama across multiple accounts.  Preserve every
            # historical queue/log row, but deterministically bind the
            # unfinished drama to the account that owns its earliest confirmed
            # episode.  If no episode was confirmed, the earliest reservation
            # remains the fail-closed owner.
            drama_rows = conn.execute(
                "SELECT id,content_id,replay_generation,"
                "assigned_account_id,assigned_at,"
                "assigned_source_queue_id FROM x_post_drama_pool "
                "ORDER BY created_at,id"
            ).fetchall()
            for pool in drama_rows:
                canonical = conn.execute(
                    "SELECT q.id,q.account_id,q.created_at "
                    "FROM x_post_queue q "
                    "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                    "WHERE q.source_type='drama' AND ("
                    "q.drama_pool_item_id=? OR "
                    "(q.drama_pool_item_id IS NULL AND q.content_id=?)"
                    ") AND q.drama_replay_generation=? "
                    "ORDER BY CASE WHEN q.status='published' "
                    "AND COALESCE(l.status,'')='published' "
                    "AND COALESCE(l.unknown_outcome,0)=0 "
                    "THEN 0 ELSE 1 END,"
                    "q.episode_number,q.created_at,q.id LIMIT 1",
                    (
                        pool["id"],
                        pool["content_id"],
                        pool["replay_generation"],
                    ),
                ).fetchone()
                current_owner = int(pool["assigned_account_id"] or 0)
                if canonical is None:
                    if current_owner:
                        raise XPostError(
                            "x_post_storage_conflict",
                            "短剧池存在没有队列依据的账号绑定，迁移已中止",
                            500,
                        )
                    continue
                canonical_owner = int(canonical["account_id"])
                if current_owner not in (0, canonical_owner):
                    raise XPostError(
                        "x_post_storage_conflict",
                        "短剧池账号绑定与首个发布账号冲突，迁移已中止",
                        500,
                    )
                if (
                    current_owner == 0
                    or not str(pool["assigned_at"] or "")
                    or pool["assigned_source_queue_id"] is None
                ):
                    conn.execute(
                        "UPDATE x_post_drama_pool SET assigned_account_id=?,"
                        "assigned_at=?,assigned_source_queue_id=? WHERE id=?",
                        (
                            canonical_owner,
                            str(canonical["created_at"] or migration_timestamp),
                            int(canonical["id"]),
                            int(pool["id"]),
                        ),
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
                "WHERE run_date<>'' AND schedule_run_id IS NULL "
                "AND manual_run_id IS NULL "
                "GROUP BY account_id,run_date HAVING COUNT(*)>1 LIMIT 1"
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
            conn.execute("DROP INDEX IF EXISTS ux_x_post_queue_account_run_date")
            conn.execute(
                "DROP INDEX IF EXISTS ux_x_post_queue_legacy_account_run_date"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_account_run_date "
                "ON x_post_queue(account_id,run_date) "
                "WHERE run_date<>'' AND schedule_run_id IS NULL "
                "AND manual_run_id IS NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_schedule_account "
                "ON x_post_queue(schedule_run_id,account_id) "
                "WHERE schedule_run_id IS NOT NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_manual_account "
                "ON x_post_queue(manual_run_id,account_id) "
                "WHERE manual_run_id IS NOT NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_episode_key "
                "ON x_post_queue(episode_key) WHERE episode_key<>''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_x_post_queue_pool_item_id "
                "ON x_post_queue(pool_item_id) WHERE pool_item_id IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_run ON x_post_queue(run_id,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_catchup "
                "ON x_post_queue(catchup_run_id,candidate_rank,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_schedule "
                "ON x_post_queue(schedule_run_id,candidate_rank,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_queue_manual "
                "ON x_post_queue(manual_run_id,candidate_rank,id)"
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_catchup_status "
                "ON x_post_catchup_run(status,run_date,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_pool_fifo "
                "ON x_post_material_pool(status,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_run_status "
                "ON x_post_schedule_run(status,run_date,publish_time,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_recovery_created "
                "ON x_post_schedule_recovery_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_corrective_created "
                "ON x_post_schedule_corrective_retry_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_verified_repair_created "
                "ON x_post_schedule_verified_repair_retry_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_codefix_comp_created "
                "ON x_post_schedule_codefix_compensation_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_drama_cap_recovery_created "
                "ON x_post_schedule_drama_capability_recovery_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_token_refresh_recovery_created "
                "ON x_post_schedule_token_refresh_recovery_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_schedule_transient_media_recovery_created "
                "ON x_post_schedule_transient_media_recovery_audit(created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_manual_run_status "
                "ON x_post_manual_run(status,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_manual_run_source_status "
                "ON x_post_manual_run(trigger_source,status,created_at,id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_x_post_manual_run_auto_external_task "
                "ON x_post_manual_run(external_task_key) "
                "WHERE trigger_source='auto_template'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_drama_pool_fifo "
                "ON x_post_drama_pool(status,created_at,id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_x_post_drama_pool_active_account "
                "ON x_post_drama_pool(assigned_account_id) "
                "WHERE assigned_account_id>0 "
                "AND status IN ('pending','active','needs_review') "
                "AND next_sub_number<=free_episode_count"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_drama_pool_assignment "
                "ON x_post_drama_pool(assigned_account_id,status,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_drama_pool_priority "
                "ON x_post_drama_pool(assigned_account_id,status,priority_at,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_post_drama_replay_audit_pool "
                "ON x_post_drama_replay_audit(pool_item_id,to_generation,id)"
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
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_x_post_queue_catchup_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.catchup_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_catchup_run
                       WHERE id=NEW.catchup_run_id
                         AND run_date=NEW.run_date
                         AND source_date=NEW.source_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue catchup_run_id missing or mismatched');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_x_post_queue_catchup_update
                BEFORE UPDATE OF catchup_run_id,run_date,source_date ON x_post_queue
                WHEN NEW.catchup_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_catchup_run
                       WHERE id=NEW.catchup_run_id
                         AND run_date=NEW.run_date
                         AND source_date=NEW.source_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue catchup_run_id missing or mismatched');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_schedule_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_schedule_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.schedule_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_schedule_run
                       WHERE id=NEW.schedule_run_id
                         AND source_type=NEW.source_type
                         AND run_date=NEW.run_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue schedule_run_id missing or mismatched');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_schedule_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_schedule_update
                BEFORE UPDATE OF schedule_run_id,source_type,run_date ON x_post_queue
                WHEN NEW.schedule_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_schedule_run
                       WHERE id=NEW.schedule_run_id
                         AND source_type=NEW.source_type
                         AND run_date=NEW.run_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue schedule_run_id missing or mismatched');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_manual_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_manual_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.manual_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_manual_run
                       WHERE id=NEW.manual_run_id
                         AND run_date=NEW.run_date
                         AND source_date=NEW.source_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue manual_run_id missing or mismatched');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_manual_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_manual_update
                BEFORE UPDATE OF manual_run_id,run_date,source_date ON x_post_queue
                WHEN NEW.manual_run_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1
                        FROM x_post_manual_run
                       WHERE id=NEW.manual_run_id
                         AND run_date=NEW.run_date
                         AND source_date=NEW.source_date
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue manual_run_id missing or mismatched');
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS trg_x_post_manual_run_identity_update"
            )
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_manual_run_identity_update
                BEFORE UPDATE OF idempotency_key,trigger_source,
                    external_task_key,template_ref,template_version,
                    body_template_sha256,run_date,source_date,
                    account_ids_json,material_ids_json,body_template,
                    actor_user_id,actor_name
                ON x_post_manual_run
                WHEN NEW.idempotency_key<>OLD.idempotency_key
                  OR NEW.trigger_source<>OLD.trigger_source
                  OR NEW.external_task_key<>OLD.external_task_key
                  OR NEW.template_ref<>OLD.template_ref
                  OR NEW.template_version<>OLD.template_version
                  OR NEW.body_template_sha256<>OLD.body_template_sha256
                  OR NEW.run_date<>OLD.run_date
                  OR NEW.source_date<>OLD.source_date
                  OR NEW.account_ids_json<>OLD.account_ids_json
                  OR NEW.material_ids_json<>OLD.material_ids_json
                  OR NEW.body_template<>OLD.body_template
                  OR NEW.actor_user_id<>OLD.actor_user_id
                  OR NEW.actor_name<>OLD.actor_name
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_manual_run identity is immutable');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_batch_parent_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_batch_parent_insert
                BEFORE INSERT ON x_post_queue
                WHEN (
                    (NEW.run_id IS NOT NULL)
                    + (NEW.catchup_run_id IS NOT NULL)
                    + (NEW.schedule_run_id IS NOT NULL)
                    + (NEW.manual_run_id IS NOT NULL)
                ) > 1
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue has multiple batch parents');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_batch_parent_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_batch_parent_update
                BEFORE UPDATE OF run_id,catchup_run_id,schedule_run_id,
                    manual_run_id ON x_post_queue
                WHEN (
                    (NEW.run_id IS NOT NULL)
                    + (NEW.catchup_run_id IS NOT NULL)
                    + (NEW.schedule_run_id IS NOT NULL)
                    + (NEW.manual_run_id IS NOT NULL)
                ) > 1
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue has multiple batch parents');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_drama_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_drama_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.source_type='drama'
                  AND (
                      NEW.drama_pool_item_id IS NULL
                      OR NEW.episode_number <= 0
                      OR NEW.episode_key=''
                      OR NOT EXISTS(
                          SELECT 1 FROM x_post_drama_pool
                           WHERE id=NEW.drama_pool_item_id
                             AND content_id=NEW.content_id
                             AND replay_generation=
                                 NEW.drama_replay_generation
                             AND assigned_account_id IN (0,NEW.account_id)
                      )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue drama binding invalid');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_drama_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_drama_update
                BEFORE UPDATE OF source_type,drama_pool_item_id,content_id,
                    episode_number,episode_key,drama_replay_generation,
                    account_id ON x_post_queue
                WHEN (OLD.source_type='drama' OR NEW.source_type='drama')
                  AND (
                      NEW.source_type<>'drama'
                      OR
                      NEW.drama_pool_item_id IS NULL
                      OR NEW.episode_number <= 0
                      OR NEW.episode_key=''
                      OR NOT EXISTS(
                          SELECT 1 FROM x_post_drama_pool
                           WHERE id=NEW.drama_pool_item_id
                             AND content_id=NEW.content_id
                             AND replay_generation=
                                 NEW.drama_replay_generation
                             AND assigned_account_id IN (0,NEW.account_id)
                      )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue drama binding invalid');
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_queue_drama_assignment_source_delete"
            )
            conn.execute(
                """
                CREATE TRIGGER
                    trg_x_post_queue_drama_assignment_source_delete
                BEFORE DELETE ON x_post_queue
                WHEN EXISTS(
                    SELECT 1 FROM x_post_drama_pool
                     WHERE assigned_source_queue_id=OLD.id
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_pool assignment source immutable'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_assignment_immutable"
            )
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_drama_pool_assignment_immutable
                BEFORE UPDATE OF assigned_account_id,assigned_at,
                    assigned_source_queue_id ON x_post_drama_pool
                WHEN OLD.assigned_account_id>0
                  AND (
                      NEW.assigned_account_id<>OLD.assigned_account_id
                      OR NEW.assigned_at<>OLD.assigned_at
                      OR NEW.assigned_source_queue_id
                         IS NOT OLD.assigned_source_queue_id
                  )
                  AND NOT (
                      NEW.replay_generation=OLD.replay_generation+1
                      AND NEW.assigned_account_id=0
                      AND NEW.assigned_at=''
                      AND NEW.assigned_source_queue_id IS NULL
                      AND EXISTS(
                          SELECT 1 FROM x_post_drama_replay_audit a
                           WHERE a.pool_item_id=OLD.id
                             AND a.content_id=OLD.content_id
                             AND a.from_generation=
                                 OLD.replay_generation
                             AND a.to_generation=
                                 NEW.replay_generation
                             AND a.from_status=OLD.status
                             AND a.from_free_episode_count=
                                 OLD.free_episode_count
                             AND a.from_next_sub_number=
                                 OLD.next_sub_number
                             AND a.from_published_episode_count=
                                 OLD.published_episode_count
                             AND a.from_assigned_account_id=
                                 OLD.assigned_account_id
                             AND a.from_assigned_at=OLD.assigned_at
                             AND a.from_assigned_source_queue_id
                                 IS OLD.assigned_source_queue_id
                             AND a.reason='operator_full_replay_v1'
                      )
                  )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_pool assignment immutable'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_replay_generation_guard"
            )
            conn.execute(
                """
                CREATE TRIGGER
                    trg_x_post_drama_pool_replay_generation_guard
                BEFORE UPDATE OF replay_generation ON x_post_drama_pool
                WHEN NEW.replay_generation<>OLD.replay_generation
                  AND NOT (
                      NEW.replay_generation=OLD.replay_generation+1
                      AND NEW.status='pending'
                      AND NEW.next_sub_number=1
                      AND NEW.published_episode_count=0
                      AND NEW.assigned_account_id=0
                      AND NEW.assigned_at=''
                      AND NEW.assigned_source_queue_id IS NULL
                      AND NEW.completed_at=''
                      AND NEW.last_error_code=''
                      AND NEW.last_error_message=''
                      AND EXISTS(
                          SELECT 1 FROM x_post_drama_replay_audit a
                           WHERE a.pool_item_id=OLD.id
                             AND a.content_id=OLD.content_id
                             AND a.from_generation=
                                 OLD.replay_generation
                             AND a.to_generation=
                                 NEW.replay_generation
                             AND a.from_status=OLD.status
                             AND a.from_free_episode_count=
                                 OLD.free_episode_count
                             AND a.from_next_sub_number=
                                 OLD.next_sub_number
                             AND a.from_published_episode_count=
                                 OLD.published_episode_count
                             AND a.from_assigned_account_id=
                                 OLD.assigned_account_id
                             AND a.from_assigned_at=OLD.assigned_at
                             AND a.from_assigned_source_queue_id
                                 IS OLD.assigned_source_queue_id
                             AND a.reason='operator_full_replay_v1'
                      )
                  )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_pool replay generation invalid'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_replay_audit_immutable_update"
            )
            conn.execute(
                """
                CREATE TRIGGER
                    trg_x_post_drama_replay_audit_immutable_update
                BEFORE UPDATE ON x_post_drama_replay_audit
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_replay_audit immutable'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_replay_audit_immutable_delete"
            )
            conn.execute(
                """
                CREATE TRIGGER
                    trg_x_post_drama_replay_audit_immutable_delete
                BEFORE DELETE ON x_post_drama_replay_audit
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_replay_audit immutable'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_assignment_evidence"
            )
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_drama_pool_assignment_evidence
                BEFORE UPDATE OF assigned_account_id,assigned_at,
                    assigned_source_queue_id ON x_post_drama_pool
                WHEN (
                    NEW.assigned_account_id=0
                    AND (
                        NEW.assigned_at<>''
                        OR NEW.assigned_source_queue_id IS NOT NULL
                    )
                ) OR (
                    NEW.assigned_account_id>0
                    AND (
                        NEW.assigned_at=''
                        OR NEW.assigned_source_queue_id IS NULL
                        OR NOT EXISTS(
                            SELECT 1 FROM x_post_queue q
                             WHERE q.id=NEW.assigned_source_queue_id
                               AND q.source_type='drama'
                               AND q.account_id=NEW.assigned_account_id
                               AND q.drama_replay_generation=
                                   NEW.replay_generation
                               AND (
                                   q.drama_pool_item_id=NEW.id
                                   OR (
                                       q.drama_pool_item_id IS NULL
                                       AND q.content_id=NEW.content_id
                                   )
                               )
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_pool assignment evidence invalid'
                    );
                END
                """
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_assignment_insert_evidence"
            )
            conn.execute(
                """
                CREATE TRIGGER
                    trg_x_post_drama_pool_assignment_insert_evidence
                BEFORE INSERT ON x_post_drama_pool
                WHEN (
                    NEW.assigned_account_id<>0
                    OR NEW.assigned_at<>''
                    OR NEW.assigned_source_queue_id IS NOT NULL
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'x_post_drama_pool insert assignment invalid'
                    );
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_pool_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_pool_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.pool_item_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1 FROM x_post_material_pool
                       WHERE id=NEW.pool_item_id
                         AND material_key=NEW.material_key
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue pool binding invalid');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_pool_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_pool_update
                BEFORE UPDATE OF pool_item_id,material_key ON x_post_queue
                WHEN NEW.pool_item_id IS NOT NULL
                  AND NOT EXISTS(
                      SELECT 1 FROM x_post_material_pool
                       WHERE id=NEW.pool_item_id
                         AND material_key=NEW.material_key
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue pool binding invalid');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_pool_required_insert")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_pool_required_insert
                BEFORE INSERT ON x_post_queue
                WHEN NEW.pool_item_id IS NULL
                  AND EXISTS(
                      SELECT 1 FROM x_post_material_pool
                       WHERE material_key=NEW.material_key
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue pool binding required');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_queue_pool_required_update")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_queue_pool_required_update
                BEFORE UPDATE OF pool_item_id,material_key ON x_post_queue
                WHEN NEW.pool_item_id IS NULL
                  AND EXISTS(
                      SELECT 1 FROM x_post_material_pool
                       WHERE material_key=NEW.material_key
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_queue pool binding required');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_pool_queue_guard")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_pool_queue_guard
                BEFORE INSERT ON x_post_material_pool
                WHEN EXISTS(
                    SELECT 1 FROM x_post_queue
                     WHERE material_key=NEW.material_key
                )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_material_pool material occupied');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_pool_delete_guard")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_pool_delete_guard
                BEFORE DELETE ON x_post_material_pool
                WHEN EXISTS(
                    SELECT 1 FROM x_post_queue
                     WHERE pool_item_id=OLD.id
                        OR material_key=OLD.material_key
                )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_material_pool item occupied');
                END
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_x_post_drama_pool_delete_guard")
            conn.execute(
                """
                CREATE TRIGGER trg_x_post_drama_pool_delete_guard
                BEFORE DELETE ON x_post_drama_pool
                WHEN OLD.status NOT IN ('pending','validation_failed')
                  OR EXISTS(
                      SELECT 1 FROM x_post_queue
                       WHERE drama_pool_item_id=OLD.id
                          OR (
                              source_type='drama'
                              AND content_id=OLD.content_id
                          )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'x_post_drama_pool item occupied');
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


def _schedule_source_type(value):
    source_type = str(value or "").strip().lower()
    if source_type not in SCHEDULE_SOURCE_TYPES:
        raise XPostError("invalid_request", "排程来源无效", 400)
    return source_type


def _schedule_publish_time(value):
    publish_time = str(value or "").strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", publish_time):
        raise XPostError("invalid_request", "发布时间必须为HH:MM", 400)
    return publish_time


def _schedule_account_ids(values, *, allow_empty=False):
    if not isinstance(values, list):
        raise XPostError("invalid_request", "account_ids必须是数组", 400)
    if (not allow_empty and not values) or len(values) > MAX_SCHEDULE_ACCOUNTS:
        raise XPostError(
            "invalid_request",
            "account_ids必须包含1到%s个账号" % MAX_SCHEDULE_ACCOUNTS,
            400,
        )
    normalized = []
    seen = set()
    for raw in values:
        account_id = _positive_int(raw, "account_id")
        if account_id in seen:
            raise XPostError("invalid_request", "account_ids不能重复", 400)
        seen.add(account_id)
        normalized.append(account_id)
    return normalized


def _manual_material_ids(values):
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_MANUAL_PUBLISH_SIZE
    ):
        raise XPostError(
            "invalid_request",
            "material_ids必须包含1到%s个素材" % MAX_MANUAL_PUBLISH_SIZE,
            400,
        )
    normalized = []
    seen = set()
    for raw in values:
        material_id = normalize_material_key(raw)
        if material_id in seen:
            raise XPostError(
                "invalid_request",
                "material_ids不能重复",
                400,
            )
        seen.add(material_id)
        normalized.append(material_id)
    return normalized


def _manual_idempotency_key(value):
    try:
        return _clean_token(value, "manual idempotency key", 200)
    except ValueError:
        raise XPostError(
            "invalid_request",
            "idempotency_key无效",
            400,
        ) from None


def _manual_trigger_source(value):
    source = str(value or MANUAL_TRIGGER_SOURCE).strip().lower()
    if source not in MANUAL_TRIGGER_SOURCES:
        raise XPostError(
            "invalid_request",
            "trigger_source invalid",
            400,
        )
    return source


def _auto_provenance_token(value, label):
    try:
        return _clean_token(value, label, 200)
    except ValueError:
        raise XPostError(
            "invalid_request",
            "%s invalid" % label,
            400,
        ) from None


def _schedule_publish_times(values, *, allow_empty=False):
    if not isinstance(values, list):
        raise XPostError("invalid_request", "publish_times必须是数组", 400)
    if (not allow_empty and not values) or len(values) > 24:
        raise XPostError(
            "invalid_request",
            "publish_times必须包含1到24个时间点",
            400,
        )
    normalized = [_schedule_publish_time(value) for value in values]
    if len(set(normalized)) != len(normalized):
        raise XPostError("invalid_request", "publish_times不能重复", 400)
    return sorted(normalized)


def _schedule_mode(value):
    mode = str(value or "fixed").strip().lower()
    if mode not in SCHEDULE_MODES:
        raise XPostError(
            "invalid_schedule_mode",
            "自动发布模式必须是fixed或random",
            400,
        )
    return mode


def _schedule_random_daily_count(value, *, allow_zero=False):
    if isinstance(value, bool):
        raise XPostError(
            "invalid_random_daily_count",
            "每日随机发布次数必须是1到24",
            400,
        )
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        raise XPostError(
            "invalid_random_daily_count",
            "每日随机发布次数必须是1到24",
            400,
        ) from None
    minimum = 0 if allow_zero else 1
    if count < minimum or count > MAX_RANDOM_DAILY_COUNT:
        raise XPostError(
            "invalid_random_daily_count",
            "每日随机发布次数必须是1到24",
            400,
        )
    return count


def _generate_random_publish_times(
    count,
    *,
    previous_times=(),
    forbidden_times=(),
):
    """Generate one immutable Beijing-day plan with bounded spacing."""

    normalized_count = _schedule_random_daily_count(count)
    previous = list(previous_times)
    forbidden = {
        _schedule_publish_time(value) for value in forbidden_times
    }
    upper = 1439 - (normalized_count - 1) * (
        RANDOM_PUBLISH_MIN_GAP_MINUTES - 1
    )
    rng = secrets.SystemRandom()
    for _attempt in range(1024):
        compressed = sorted(
            rng.sample(range(upper + 1), normalized_count)
        )
        minute_values = [
            value
            + index * (RANDOM_PUBLISH_MIN_GAP_MINUTES - 1)
            for index, value in enumerate(compressed)
        ]
        if any(value % 60 == 0 for value in minute_values):
            continue
        result = [
            "%02d:%02d" % divmod(value, 60)
            for value in minute_values
        ]
        if result == previous or forbidden.intersection(result):
            continue
        return result
    raise XPostError(
        "x_post_random_plan_generation_failed",
        "无法生成满足间隔与账号冲突要求的随机发布时间",
        500,
    )


def _json_array(value, label):
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise XPostError(
            "x_post_storage_conflict",
            "%s存储格式无效" % label,
            500,
        ) from None
    if not isinstance(parsed, list):
        raise XPostError(
            "x_post_storage_conflict",
            "%s存储格式无效" % label,
            500,
        )
    return parsed


def _drama_content_id(value):
    content_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", content_id):
        raise XPostError("invalid_request", "短剧ID无效", 400)
    return content_id


def _drama_episode_key(content_id, episode_number, replay_generation=1):
    content_id = _drama_content_id(content_id)
    episode_number = _positive_int(episode_number, "episode_number")
    replay_generation = _positive_int(
        replay_generation,
        "drama_replay_generation",
    )
    if replay_generation == 1:
        return "%s:%s" % (content_id, episode_number)
    return "%s:replay%s:%s" % (
        content_id,
        replay_generation,
        episode_number,
    )


def _drama_pool_item_ids(values):
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_DRAMA_POOL_BATCH_DELETE_SIZE
    ):
        raise XPostError(
            "invalid_request",
            "pool_item_ids必须是包含1到%s项的数组"
            % MAX_DRAMA_POOL_BATCH_DELETE_SIZE,
            400,
        )
    normalized = []
    seen = set()
    for raw in values:
        pool_item_id = _positive_int(raw, "pool_item_id")
        if pool_item_id in seen:
            raise XPostError(
                "invalid_request",
                "pool_item_ids不能重复",
                400,
            )
        seen.add(pool_item_id)
        normalized.append(pool_item_id)
    return normalized


def _drama_pool_delete_block_reason(status, has_history):
    if has_history:
        return "已有发布队列或历史，不能删除"
    if str(status or "").strip().lower() not in DRAMA_POOL_DELETABLE_STATUSES:
        return "仅待发布或不可用且无发布历史的短剧可以删除"
    return ""


def _schedule_next_due(account_ids, publish_times, enabled, now=None):
    if not enabled or not account_ids or not publish_times:
        return ""
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    else:
        current = current.astimezone(BEIJING_TZ)
    for day_offset in (0, 1):
        target_date = current.date() + timedelta(days=day_offset)
        for publish_time in publish_times:
            hour, minute = (int(part) for part in publish_time.split(":"))
            candidate = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                minute,
                tzinfo=BEIJING_TZ,
            )
            if candidate > current:
                return candidate.isoformat(timespec="minutes")
    return ""


class XPostStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        ensure_storage(self.db_path)

    @staticmethod
    def _schedule_config_item(row, now=None):
        if not row:
            raise XPostError(
                "x_post_schedule_not_found",
                "X自动发布设置不存在",
                404,
            )
        item = _row_dict(row)
        account_ids = _schedule_account_ids(
            _json_array(item.pop("account_ids_json"), "account_ids"),
            allow_empty=True,
        )
        publish_times = _schedule_publish_times(
            _json_array(item.pop("publish_times_json"), "publish_times"),
            allow_empty=True,
        )
        schedule_mode = _schedule_mode(item.get("schedule_mode", "fixed"))
        random_daily_count = _schedule_random_daily_count(
            item.get("random_daily_count", 0),
            allow_zero=True,
        )
        random_effective_date = str(
            item.get("random_effective_date", "") or ""
        ).strip()
        if random_effective_date:
            random_effective_date = _date_value(
                random_effective_date,
                "random_effective_date",
            )
        item["enabled"] = bool(item["enabled"])
        item["account_ids"] = account_ids
        item["publish_times"] = publish_times
        item["schedule_mode"] = schedule_mode
        item["random_daily_count"] = random_daily_count
        item["random_effective_date"] = random_effective_date
        item["body_template"] = _normalize_post_template(
            item.get("body_template"),
            item["source_type"],
        )
        item["supported_macros"] = ["drama_name"]
        if item["source_type"] == "drama":
            item["supported_macros"].append("episode_number")
        item["supported_macros"].extend(["desc", "url"])
        item["posts_per_day"] = (
            len(account_ids)
            * (
                random_daily_count
                if schedule_mode == "random"
                else len(publish_times)
            )
            if item["enabled"]
            else 0
        )
        item["next_due_at"] = (
            ""
            if schedule_mode == "random"
            else _schedule_next_due(
                account_ids,
                publish_times,
                item["enabled"],
                now=now,
            )
        )
        item["random_daily_plans"] = []
        return item

    @staticmethod
    def _random_schedule_plan_item(row):
        if not row:
            return None
        item = _row_dict(row)
        item["source_type"] = _schedule_source_type(item["source_type"])
        item["run_date"] = _date_value(item["run_date"], "run_date")
        item["config_version"] = _positive_int(
            item["config_version"],
            "config_version",
        )
        item["account_ids"] = _schedule_account_ids(
            _json_array(item.pop("account_ids_json"), "account_ids")
        )
        item["body_template"] = _normalize_post_template(
            item.get("body_template"),
            item["source_type"],
        )
        item["publish_times"] = _schedule_publish_times(
            _json_array(item.pop("publish_times_json"), "publish_times")
        )
        return item

    def _ensure_random_schedule_plan(
        self,
        conn,
        config_row,
        run_date,
        timestamp,
        *,
        replace_future=False,
    ):
        config = self._schedule_config_item(config_row)
        normalized_date = _date_value(run_date, "run_date")
        if (
            not config["enabled"]
            or config["schedule_mode"] != "random"
            or config["random_daily_count"] < 1
            or not config["random_effective_date"]
            or normalized_date < config["random_effective_date"]
        ):
            return None
        existing = conn.execute(
            "SELECT * FROM x_post_schedule_random_plan "
            "WHERE source_type=? AND run_date=?",
            (config["source_type"], normalized_date),
        ).fetchone()
        if existing is not None and not replace_future:
            return self._random_schedule_plan_item(existing)
        if existing is not None:
            started = conn.execute(
                "SELECT 1 FROM x_post_schedule_run "
                "WHERE source_type=? AND run_date=? LIMIT 1",
                (config["source_type"], normalized_date),
            ).fetchone()
            if started is not None:
                return self._random_schedule_plan_item(existing)
        previous = conn.execute(
            "SELECT * FROM x_post_schedule_random_plan "
            "WHERE source_type=? AND run_date<? "
            "ORDER BY run_date DESC LIMIT 1",
            (config["source_type"], normalized_date),
        ).fetchone()
        previous_times = (
            self._random_schedule_plan_item(previous)["publish_times"]
            if previous is not None
            else []
        )
        forbidden_times = set()
        other_rows = conn.execute(
            "SELECT * FROM x_post_schedule_config "
            "WHERE source_type<>? AND enabled=1",
            (config["source_type"],),
        ).fetchall()
        for other_row in other_rows:
            other = self._schedule_config_item(other_row)
            if not set(other["account_ids"]).intersection(
                config["account_ids"]
            ):
                continue
            if other["schedule_mode"] == "fixed":
                forbidden_times.update(other["publish_times"])
                continue
            other_plan = conn.execute(
                "SELECT * FROM x_post_schedule_random_plan "
                "WHERE source_type=? AND run_date=?",
                (other["source_type"], normalized_date),
            ).fetchone()
            if other_plan is not None:
                forbidden_times.update(
                    self._random_schedule_plan_item(other_plan)[
                        "publish_times"
                    ]
                )
        publish_times = _generate_random_publish_times(
            config["random_daily_count"],
            previous_times=previous_times,
            forbidden_times=forbidden_times,
        )
        values = (
            int(config["version"]),
            json.dumps(config["account_ids"], separators=(",", ":")),
            config["body_template"],
            json.dumps(publish_times, separators=(",", ":")),
            timestamp,
        )
        if existing is None:
            conn.execute(
                "INSERT INTO x_post_schedule_random_plan("
                "source_type,run_date,config_version,account_ids_json,"
                "body_template,publish_times_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    config["source_type"],
                    normalized_date,
                    *values,
                ),
            )
        else:
            conn.execute(
                "UPDATE x_post_schedule_random_plan SET "
                "config_version=?,account_ids_json=?,body_template=?,"
                "publish_times_json=?,created_at=? "
                "WHERE source_type=? AND run_date=?",
                (
                    *values,
                    config["source_type"],
                    normalized_date,
                ),
            )
        row = conn.execute(
            "SELECT * FROM x_post_schedule_random_plan "
            "WHERE source_type=? AND run_date=?",
            (config["source_type"], normalized_date),
        ).fetchone()
        return self._random_schedule_plan_item(row)

    def ensure_random_schedule_plans(self, run_dates):
        if not isinstance(run_dates, list):
            raise XPostError(
                "invalid_request",
                "随机计划日期必须是数组",
                400,
            )
        normalized_dates = sorted(
            {_date_value(value, "run_date") for value in run_dates}
        )
        if len(normalized_dates) > 7:
            raise XPostError(
                "invalid_request",
                "单次最多生成7天随机计划",
                400,
            )
        timestamp = utc_now()
        results = []
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            configs = conn.execute(
                "SELECT * FROM x_post_schedule_config "
                "WHERE enabled=1 AND schedule_mode='random' "
                "ORDER BY source_type"
            ).fetchall()
            for normalized_date in normalized_dates:
                for config in configs:
                    item = self._ensure_random_schedule_plan(
                        conn,
                        config,
                        normalized_date,
                        timestamp,
                    )
                    if item is not None:
                        results.append(item)
            conn.commit()
        return sorted(
            results,
            key=lambda item: (item["run_date"], item["source_type"]),
        )

    def get_schedule_config(self, source_type, now=None):
        source_type = _schedule_source_type(source_type)
        current = now or datetime.now(BEIJING_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TZ)
        else:
            current = current.astimezone(BEIJING_TZ)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM x_post_schedule_config WHERE source_type=?",
                (source_type,),
            ).fetchone()
            item = self._schedule_config_item(row, now=current)
            if item["schedule_mode"] == "random":
                dates = [
                    current.date().isoformat(),
                    (current.date() + timedelta(days=1)).isoformat(),
                ]
                placeholders = ",".join("?" for _value in dates)
                plans = conn.execute(
                    "SELECT * FROM x_post_schedule_random_plan "
                    "WHERE source_type=? AND run_date IN (%s) "
                    "ORDER BY run_date" % placeholders,
                    (source_type, *dates),
                ).fetchall()
                item["random_daily_plans"] = [
                    self._random_schedule_plan_item(plan) for plan in plans
                ]
        if item["schedule_mode"] == "random" and item["enabled"]:
            for plan in item["random_daily_plans"]:
                for publish_time in plan["publish_times"]:
                    hour, minute = (
                        int(part) for part in publish_time.split(":")
                    )
                    plan_date = datetime.strptime(
                        plan["run_date"], "%Y-%m-%d"
                    ).date()
                    candidate = datetime(
                        plan_date.year,
                        plan_date.month,
                        plan_date.day,
                        hour,
                        minute,
                        tzinfo=BEIJING_TZ,
                    )
                    if candidate > current:
                        item["next_due_at"] = candidate.isoformat(
                            timespec="minutes"
                        )
                        return item
        return item

    def scheduled_account_ids(
        self,
        *,
        enabled_only=True,
        include_nonterminal_runs=False,
    ):
        clauses = " WHERE enabled=1" if enabled_only else ""
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT account_ids_json FROM x_post_schedule_config"
                + clauses
                + " ORDER BY source_type"
            ).fetchall()
            if include_nonterminal_runs:
                rows = list(rows) + list(
                    conn.execute(
                        "SELECT account_ids_json FROM x_post_schedule_run "
                        "WHERE status IN ('claimed','queued','running') "
                        "ORDER BY run_date,publish_time,source_type,id"
                    ).fetchall()
                )
        account_ids = []
        seen = set()
        for row in rows:
            values = _schedule_account_ids(
                _json_array(row["account_ids_json"], "account_ids"),
                allow_empty=True,
            )
            for account_id in values:
                if account_id not in seen:
                    seen.add(account_id)
                    account_ids.append(account_id)
        return account_ids

    def save_schedule_config(
        self,
        source_type,
        payload,
        actor=None,
        eligible_account_ids=None,
        now=None,
    ):
        source_type = _schedule_source_type(source_type)
        if not isinstance(payload, dict):
            raise XPostError("invalid_request", "自动发布设置必须是对象", 400)
        if payload.get("timezone", SCHEDULE_TIMEZONE) != SCHEDULE_TIMEZONE:
            raise XPostError(
                "invalid_request",
                "自动发布时区固定为Asia/Shanghai",
                400,
            )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise XPostError("invalid_request", "enabled必须是布尔值", 400)
        account_ids = _schedule_account_ids(
            payload.get("account_ids"),
            allow_empty=not enabled,
        )
        publish_times = _schedule_publish_times(
            payload.get("publish_times"),
            allow_empty=True,
        )
        schedule_mode = _schedule_mode(
            payload.get("schedule_mode", "fixed")
        )
        random_daily_count = _schedule_random_daily_count(
            payload.get("random_daily_count", 0),
            allow_zero=not enabled or schedule_mode == "fixed",
        )
        if schedule_mode == "random" and publish_times:
            raise XPostError(
                "x_post_random_times_must_be_empty",
                "随机发布模式不能同时设置固定发布时间",
                400,
            )
        if schedule_mode == "fixed":
            random_daily_count = 0
        if enabled and not account_ids:
            raise XPostError(
                "invalid_request",
                "启用自动发布时必须选择账号",
                400,
            )
        if enabled and schedule_mode == "fixed" and not publish_times:
            raise XPostError(
                "invalid_request",
                "启用固定时间发布时必须设置发布时间",
                400,
            )
        if (
            enabled
            and schedule_mode == "random"
            and random_daily_count < 1
        ):
            raise XPostError(
                "invalid_random_daily_count",
                "启用随机发布时必须设置每天1到24次",
                400,
            )
        if enabled and eligible_account_ids is not None:
            eligible = set(
                _schedule_account_ids(
                    list(eligible_account_ids),
                    allow_empty=True,
                )
            )
            missing = [
                account_id
                for account_id in account_ids
                if account_id not in eligible
            ]
            if missing:
                raise XPostError(
                    "x_account_not_publishable",
                    "所选X账号当前不可用于发布",
                    409,
                )
        expected_version = _positive_int(payload.get("version"), "version")
        actor = actor if isinstance(actor, dict) else {}
        updated_by_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        updated_by_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        if any(
            ord(char) < 32
            for value in (updated_by_user_id, updated_by_name)
            for char in value
        ):
            raise XPostError("invalid_request", "修改人信息无效", 400)
        timestamp = utc_now()
        current_time = now or datetime.now(BEIJING_TZ)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=BEIJING_TZ)
        else:
            current_time = current_time.astimezone(BEIJING_TZ)
        protected_times = set()
        protected_cursor = (
            current_time - timedelta(seconds=90)
        ).replace(second=0, microsecond=0)
        protected_end = current_time.replace(second=0, microsecond=0)
        while protected_cursor <= protected_end:
            if 0 <= (
                current_time - protected_cursor
            ).total_seconds() <= 90:
                protected_times.add(
                    protected_cursor.strftime("%H:%M")
                )
            protected_cursor += timedelta(minutes=1)
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM x_post_schedule_config WHERE source_type=?",
                (source_type,),
            ).fetchone()
            if not current:
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_not_found",
                    "X自动发布设置不存在",
                    404,
                )
            if int(current["version"]) != expected_version:
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_version_conflict",
                    "自动发布设置已被其他人修改，请刷新后重试",
                    409,
                )
            current_item = self._schedule_config_item(
                current,
                now=current_time,
            )
            body_template = (
                current_item["body_template"]
                if "body_template" not in payload
                else _normalize_post_template(
                    payload.get("body_template"),
                    source_type,
                )
            )
            current_mode = current_item["schedule_mode"]
            current_random_count = current_item["random_daily_count"]
            settings_changed = (
                bool(current_item["enabled"]) != enabled
                or list(current_item["account_ids"]) != account_ids
                or list(current_item["publish_times"]) != publish_times
                or current_mode != schedule_mode
                or current_random_count != random_daily_count
                or current_item["body_template"] != body_template
            )
            protected_schedule_times = set(
                current_item["publish_times"]
                if current_item["enabled"]
                else []
            ).union(publish_times if enabled else [])
            current_plan = conn.execute(
                "SELECT publish_times_json "
                "FROM x_post_schedule_random_plan "
                "WHERE source_type=? AND run_date=?",
                (source_type, current_time.date().isoformat()),
            ).fetchone()
            if current_plan is not None:
                protected_schedule_times.update(
                    _schedule_publish_times(
                        _json_array(
                            current_plan["publish_times_json"],
                            "publish_times",
                        )
                    )
                )
            if (
                settings_changed
                and protected_schedule_times.intersection(
                    protected_times
                )
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_slot_in_progress",
                    "当前发布时间点正在冻结或执行，请在90秒窗口结束后再修改配置",
                    409,
                )
            if enabled:
                if source_type == "drama":
                    placeholders = ",".join(
                        "?" for _item in account_ids
                    )
                    missing_owner = conn.execute(
                        "SELECT content_id,assigned_account_id "
                        "FROM x_post_drama_pool "
                        "WHERE status IN ('pending','active','needs_review') "
                        "AND assigned_account_id>0 "
                        "AND next_sub_number<=free_episode_count "
                        "AND assigned_account_id NOT IN (%s) "
                        "ORDER BY created_at,id LIMIT 1" % placeholders,
                        tuple(account_ids),
                    ).fetchone()
                    if missing_owner:
                        conn.rollback()
                        raise XPostError(
                            "x_post_drama_owner_not_configured",
                            "短剧%s尚未发完，必须保留绑定账号%s"
                            % (
                                missing_owner["content_id"],
                                missing_owner["assigned_account_id"],
                            ),
                            409,
                        )
                other = conn.execute(
                    "SELECT * FROM x_post_schedule_config "
                    "WHERE source_type<>? AND enabled=1",
                    (source_type,),
                ).fetchone()
                if other:
                    other_item = self._schedule_config_item(
                        other,
                        now=current_time,
                    )
                    other_accounts = set(
                        other_item["account_ids"]
                    )
                    other_times = set(other_item["publish_times"])
                    if (
                        schedule_mode == "fixed"
                        and other_item["schedule_mode"] == "random"
                    ):
                        tomorrow = (
                            current_time.date() + timedelta(days=1)
                        ).isoformat()
                        other_plans = conn.execute(
                            "SELECT publish_times_json "
                            "FROM x_post_schedule_random_plan "
                            "WHERE source_type=? AND run_date IN (?,?)",
                            (
                                other_item["source_type"],
                                current_time.date().isoformat(),
                                tomorrow,
                            ),
                        ).fetchall()
                        for plan in other_plans:
                            other_times.update(
                                _schedule_publish_times(
                                    _json_array(
                                        plan["publish_times_json"],
                                        "publish_times",
                                    )
                                )
                            )
                    if (
                        schedule_mode == "fixed"
                        and other_accounts.intersection(account_ids)
                        and other_times.intersection(publish_times)
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_schedule_collision",
                            "同一X账号不能在素材池和短剧池配置相同发布时间",
                            409,
                        )
            tomorrow_date = (
                current_time.date() + timedelta(days=1)
            ).isoformat()
            random_effective_date = ""
            if schedule_mode == "random":
                random_effective_date = (
                    tomorrow_date
                    if settings_changed
                    or not current_item["random_effective_date"]
                    else current_item["random_effective_date"]
                )
            cursor = conn.execute(
                "UPDATE x_post_schedule_config SET enabled=?,timezone=?,"
                "account_ids_json=?,publish_times_json=?,schedule_mode=?,"
                "random_daily_count=?,random_effective_date=?,body_template=?,"
                "version=version+1,"
                "updated_by_user_id=?,updated_by_name=?,updated_at=? "
                "WHERE source_type=? AND version=?",
                (
                    1 if enabled else 0,
                    SCHEDULE_TIMEZONE,
                    json.dumps(account_ids, separators=(",", ":")),
                    json.dumps(publish_times, separators=(",", ":")),
                    schedule_mode,
                    random_daily_count,
                    random_effective_date,
                    body_template,
                    updated_by_user_id,
                    updated_by_name,
                    timestamp,
                    source_type,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_version_conflict",
                    "自动发布设置已被其他人修改，请刷新后重试",
                    409,
                )
            if settings_changed:
                conn.execute(
                    "DELETE FROM x_post_schedule_random_plan "
                    "WHERE source_type=? AND run_date>=? "
                    "AND NOT EXISTS("
                    "SELECT 1 FROM x_post_schedule_run r "
                    "WHERE r.source_type=x_post_schedule_random_plan.source_type "
                    "AND r.run_date=x_post_schedule_random_plan.run_date)",
                    (source_type, tomorrow_date),
                )
            row = conn.execute(
                "SELECT * FROM x_post_schedule_config WHERE source_type=?",
                (source_type,),
            ).fetchone()
            if schedule_mode == "random" and enabled:
                self._ensure_random_schedule_plan(
                    conn,
                    row,
                    tomorrow_date,
                    timestamp,
                )
            conn.commit()
        return self.get_schedule_config(source_type, now=current_time)

    def due_schedule_slots(self, now=None, grace_seconds=90):
        current = now or datetime.now(BEIJING_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TZ)
        else:
            current = current.astimezone(BEIJING_TZ)
        if isinstance(grace_seconds, bool):
            raise XPostError(
                "invalid_request",
                "grace_seconds无效",
                400,
            )
        try:
            grace_seconds = int(grace_seconds)
        except (TypeError, ValueError, OverflowError):
            raise XPostError(
                "invalid_request",
                "grace_seconds无效",
                400,
            ) from None
        if grace_seconds < 0 or grace_seconds > 300:
            raise XPostError(
                "invalid_request",
                "grace_seconds无效",
                400,
            )
        earliest = current - timedelta(seconds=grace_seconds)
        cursor = earliest.replace(second=0, microsecond=0)
        final_minute = current.replace(second=0, microsecond=0)
        slots = []
        while cursor <= final_minute:
            late_seconds = (current - cursor).total_seconds()
            if 0 <= late_seconds <= grace_seconds:
                slots.append(
                    (
                        cursor.date().isoformat(),
                        cursor.strftime("%H:%M"),
                    )
                )
            cursor += timedelta(minutes=1)
        terminal_statuses = {
            "completed",
            "completed_with_errors",
            "failed_preflight",
            "needs_review",
            "stopped",
        }
        terminal_status_values = tuple(sorted(terminal_statuses))
        current_slot_keys = set(slots)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            configs = conn.execute(
                "SELECT * FROM x_post_schedule_config "
                "WHERE enabled=1 ORDER BY source_type"
            ).fetchall()
            plan_dates = [
                current.date().isoformat(),
                (current.date() + timedelta(days=1)).isoformat(),
            ]
            for plan_date in plan_dates:
                for row in configs:
                    if _schedule_mode(row["schedule_mode"]) == "random":
                        self._ensure_random_schedule_plan(
                            conn,
                            row,
                            plan_date,
                            timestamp,
                        )
            random_plan_rows = conn.execute(
                "SELECT * FROM x_post_schedule_random_plan "
                "WHERE run_date IN (?,?)",
                tuple(plan_dates),
            ).fetchall()
            random_plans = {
                (str(row["source_type"]), str(row["run_date"])):
                    self._random_schedule_plan_item(row)
                for row in random_plan_rows
            }
            for run_date, publish_time in slots:
                for row in configs:
                    config = self._schedule_config_item(row, now=current)
                    schedule_mode = config["schedule_mode"]
                    slot_config = config
                    if schedule_mode == "fixed":
                        if publish_time not in config["publish_times"]:
                            continue
                    else:
                        slot_config = random_plans.get(
                            (config["source_type"], run_date)
                        )
                        if (
                            not slot_config
                            or publish_time
                            not in slot_config["publish_times"]
                        ):
                            continue
                    slot_key = "xpost:schedule:v1:%s:%s:%s" % (
                        config["source_type"],
                        run_date,
                        publish_time.replace(":", ""),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO x_post_schedule_run("
                        "slot_key,source_type,run_date,publish_time,timezone,"
                        "config_version,account_ids_json,schedule_mode,"
                        "body_template,status,"
                        "expected_count,queued_count,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,0,?,?)",
                        (
                            slot_key,
                            config["source_type"],
                            run_date,
                            publish_time,
                            SCHEDULE_TIMEZONE,
                            int(slot_config["config_version"])
                            if schedule_mode == "random"
                            else int(config["version"]),
                            json.dumps(
                                slot_config["account_ids"],
                                separators=(",", ":"),
                            ),
                            schedule_mode,
                            slot_config["body_template"],
                            len(slot_config["account_ids"]),
                            timestamp,
                            timestamp,
                        ),
                    )
            current_run_date = current.date().isoformat()
            stale_rows = conn.execute(
                "SELECT id,run_date,publish_time FROM x_post_schedule_run "
                "WHERE run_date<? AND status NOT IN (?,?,?,?,?)",
                (
                    current_run_date,
                    *terminal_status_values,
                ),
            ).fetchall()
            stale_run_ids = [
                int(row["id"])
                for row in stale_rows
                if (
                    str(row["run_date"]),
                    str(row["publish_time"]),
                )
                not in current_slot_keys
            ]
            if stale_run_ids:
                stale_placeholders = ",".join(
                    "?" for _run_id in stale_run_ids
                )
                stale_message = "冻结批次跨日未完成，已停止自动处理"
                conn.execute(
                    "UPDATE x_post_drama_pool SET status='needs_review',"
                    "last_checked_at=?,"
                    "last_error_code=CASE WHEN last_error_code='' "
                    "THEN 'x_post_schedule_stale_claim' "
                    "ELSE last_error_code END,"
                    "last_error_message=CASE WHEN last_error_message='' "
                    "THEN ? ELSE last_error_message END,updated_at=? "
                    "WHERE status<>'completed' AND id IN ("
                    "SELECT DISTINCT q.drama_pool_item_id "
                    "FROM x_post_queue q "
                    "WHERE q.source_type='drama' "
                    "AND q.drama_pool_item_id IS NOT NULL "
                    "AND q.schedule_run_id IN (%s))"
                    % stale_placeholders,
                    (
                        timestamp,
                        stale_message,
                        timestamp,
                        *stale_run_ids,
                    ),
                )
                conn.execute(
                    "UPDATE x_post_schedule_run SET "
                    "status=CASE WHEN unknown_count>0 "
                    "THEN 'needs_review' ELSE 'stopped' END,"
                    "error_code=CASE WHEN error_code='' "
                    "THEN 'x_post_schedule_stale_claim' "
                    "ELSE error_code END,"
                    "error_message=CASE WHEN error_message='' "
                    "THEN ? ELSE error_message END,"
                    "finished_at=CASE WHEN finished_at='' "
                    "THEN ? ELSE finished_at END,updated_at=? "
                    "WHERE id IN (%s)" % stale_placeholders,
                    (
                        stale_message,
                        timestamp,
                        timestamp,
                        *stale_run_ids,
                    ),
                )
            scope_clauses = ["run_date=?"]
            scope_values = [current_run_date]
            for run_date, publish_time in slots:
                if run_date == current_run_date:
                    continue
                scope_clauses.append(
                    "(run_date=? AND publish_time=?)"
                )
                scope_values.extend((run_date, publish_time))
            scoped_runs_sql = (
                "SELECT * FROM x_post_schedule_run "
                "WHERE (%s) AND status NOT IN (?,?,?,?,?) "
                "ORDER BY run_date,publish_time,source_type,id"
                % " OR ".join(scope_clauses)
            )
            rows = conn.execute(
                scoped_runs_sql,
                (*scope_values, *terminal_status_values),
            ).fetchall()
            conn.commit()
        rows = sorted(
            rows,
            key=lambda row: (
                0
                if (
                    str(row["run_date"]),
                    str(row["publish_time"]),
                )
                in current_slot_keys
                else 1,
                str(row["run_date"]),
                str(row["publish_time"]),
                str(row["source_type"]),
                int(row["id"]),
            ),
        )[:100]
        items = []
        for row in rows:
            account_ids = _schedule_account_ids(
                _json_array(row["account_ids_json"], "account_ids")
            )
            if len(account_ids) != int(row["expected_count"]):
                raise XPostError(
                    "x_post_storage_conflict",
                    "X定时发布冻结批次账号数量不一致",
                    500,
                )
            items.append(
                {
                    "source_type": str(row["source_type"]),
                    "run_date": str(row["run_date"]),
                    "publish_time": str(row["publish_time"]),
                    "timezone": str(row["timezone"]),
                    "version": int(row["config_version"]),
                    "account_ids": account_ids,
                    "schedule_mode": _schedule_mode(
                        row["schedule_mode"]
                    ),
                    "body_template": _normalize_post_template(
                        row["body_template"],
                        row["source_type"],
                    ),
                    "slot_key": str(row["slot_key"]),
                    "frozen": True,
                }
            )
        return {"items": items, "checked_at": current.isoformat(timespec="seconds")}

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
        original_material_url = str(
            payload.get("original_material_url", "") or ""
        ).strip()
        repair_trigger_code = str(
            payload.get("media_repair_trigger_code", "") or ""
        ).strip()
        repair_job_key = str(payload.get("media_repair_job_key", "") or "").strip()
        repair_profile = str(payload.get("media_repair_profile", "") or "").strip()
        repair_source_sha256 = str(
            payload.get("media_repair_source_sha256", "") or ""
        ).strip().lower()
        repair_values = (
            original_material_url,
            repair_trigger_code,
            repair_job_key,
            repair_profile,
            repair_source_sha256,
        )
        if any(repair_values):
            if not all(repair_values):
                raise XPostError(
                    "invalid_request",
                    "媒体修复审计字段必须完整提供",
                    400,
                )
            original = urllib.parse.urlsplit(original_material_url)
            if (
                len(original_material_url) > 4096
                or any(ord(char) < 32 for char in original_material_url)
                or original.scheme != "https"
                or not original.hostname
                or original.username
                or original.password
                or original.fragment
                or original_material_url == result["material_url"]
            ):
                raise XPostError(
                    "invalid_request",
                    "媒体修复原始素材地址无效",
                    400,
                )
            if repair_trigger_code not in {
                "invalid_media_codec",
                "invalid_media_dimensions",
                "invalid_media_duration",
            }:
                raise XPostError(
                    "invalid_request",
                    "媒体修复触发原因无效",
                    400,
                )
            try:
                repair_job_key = _clean_token(
                    repair_job_key, "media repair job key", 200
                )
                repair_profile = _clean_token(
                    repair_profile, "media repair profile", 64
                )
            except ValueError:
                raise XPostError(
                    "invalid_request",
                    "媒体修复标识无效",
                    400,
                ) from None
            if not re.fullmatch(r"[0-9a-f]{64}", repair_source_sha256):
                raise XPostError(
                    "invalid_request",
                    "媒体修复源文件指纹无效",
                    400,
                )
        result["original_material_url"] = original_material_url
        result["media_repair_trigger_code"] = repair_trigger_code
        result["media_repair_job_key"] = repair_job_key
        result["media_repair_profile"] = repair_profile
        result["media_repair_source_sha256"] = repair_source_sha256
        source_type = str(payload.get("source_type", "material") or "").strip().lower()
        if source_type not in SCHEDULE_SOURCE_TYPES:
            raise XPostError("invalid_request", "source_type无效", 400)
        result["source_type"] = source_type
        result["body_template"] = _normalize_post_template(
            payload.get("body_template"),
            source_type,
        )
        if source_type == "material":
            material_key = normalize_material_key(result["material_id"])
            supplied_material_key = payload.get("material_key")
            if supplied_material_key not in (None, ""):
                supplied_material_key = normalize_material_key(supplied_material_key)
                if supplied_material_key != material_key:
                    raise XPostError(
                        "invalid_request",
                        "material_key与material_id不一致",
                        400,
                    )
            result["material_key"] = material_key
            result["episode_key"] = ""
            result["episode_number"] = 0
            result["drama_replay_generation"] = 0
            result["name_tag"] = ""
        else:
            try:
                content_id = _clean_token(
                    result["content_id"], "content_id", 128
                )
                material_id = _clean_token(
                    result["material_id"], "material_id", 128
                )
            except ValueError:
                raise XPostError(
                    "invalid_request",
                    "短剧集数发布身份无效",
                    400,
                ) from None
            result["content_id"] = content_id
            result["material_id"] = material_id
            episode_number = _positive_int(
                payload.get("episode_number"), "episode_number"
            )
            replay_generation = _positive_int(
                payload.get("drama_replay_generation", 1),
                "drama_replay_generation",
            )
            expected_episode_key = _drama_episode_key(
                content_id,
                episode_number,
                replay_generation,
            )
            supplied_episode_key = str(
                payload.get("episode_key", expected_episode_key) or ""
            ).strip()
            if supplied_episode_key != expected_episode_key:
                raise XPostError(
                    "invalid_request",
                    "episode_key与短剧集数不一致",
                    400,
                )
            result["material_key"] = ""
            result["episode_key"] = supplied_episode_key
            result["episode_number"] = episode_number
            result["drama_replay_generation"] = replay_generation
            result["name_tag"] = _clean_text(
                payload.get("name_tag"), "name_tag", 500
            )
        result["run_date"] = _date_value(
            run_date if run_date is not None else (payload.get("run_date") or _beijing_today()),
            "run_date",
        )
        raw_run_id = payload.get("run_id")
        result["run_id"] = _positive_int(raw_run_id, "run_id") if raw_run_id not in (None, "") else None
        raw_catchup_run_id = payload.get("catchup_run_id")
        result["catchup_run_id"] = (
            _positive_int(raw_catchup_run_id, "catchup_run_id")
            if raw_catchup_run_id not in (None, "")
            else None
        )
        raw_schedule_run_id = payload.get("schedule_run_id")
        result["schedule_run_id"] = (
            _positive_int(raw_schedule_run_id, "schedule_run_id")
            if raw_schedule_run_id not in (None, "")
            else None
        )
        raw_manual_run_id = payload.get("manual_run_id")
        result["manual_run_id"] = (
            _positive_int(raw_manual_run_id, "manual_run_id")
            if raw_manual_run_id not in (None, "")
            else None
        )
        if sum(
            value is not None
            for value in (
                result["run_id"],
                result["catchup_run_id"],
                result["schedule_run_id"],
                result["manual_run_id"],
            )
        ) > 1:
            raise XPostError(
                "invalid_request",
                "发布队列不能同时关联多个批次",
                400,
            )
        raw_pool_item_id = payload.get("pool_item_id")
        result["pool_item_id"] = (
            _positive_int(raw_pool_item_id, "pool_item_id")
            if raw_pool_item_id not in (None, "")
            else None
        )
        pool_created_at = str(payload.get("pool_created_at", "") or "").strip()
        if (
            len(pool_created_at) > 64
            or any(ord(char) < 32 for char in pool_created_at)
            or (result["pool_item_id"] is None and pool_created_at)
            or (result["pool_item_id"] is not None and not pool_created_at)
        ):
            raise XPostError("invalid_request", "pool_created_at无效", 400)
        result["pool_created_at"] = pool_created_at
        raw_drama_pool_item_id = payload.get("drama_pool_item_id")
        result["drama_pool_item_id"] = (
            _positive_int(raw_drama_pool_item_id, "drama_pool_item_id")
            if raw_drama_pool_item_id not in (None, "")
            else None
        )
        drama_pool_created_at = str(
            payload.get("drama_pool_created_at", "") or ""
        ).strip()
        if (
            len(drama_pool_created_at) > 64
            or any(ord(char) < 32 for char in drama_pool_created_at)
            or (
                result["drama_pool_item_id"] is None
                and drama_pool_created_at
            )
            or (
                result["drama_pool_item_id"] is not None
                and not drama_pool_created_at
            )
        ):
            raise XPostError(
                "invalid_request",
                "drama_pool_created_at无效",
                400,
            )
        result["drama_pool_created_at"] = drama_pool_created_at
        if source_type == "material":
            if result["drama_pool_item_id"] is not None:
                raise XPostError(
                    "invalid_request",
                    "素材队列不能绑定短剧池",
                    400,
                )
        elif result["pool_item_id"] is not None or result["drama_pool_item_id"] is None:
            raise XPostError(
                "invalid_request",
                "短剧队列必须且只能绑定短剧池",
                400,
            )
        rank_value = candidate_rank if candidate_rank is not None else payload.get("candidate_rank")
        result["candidate_rank"] = _nonnegative_int(rank_value, "candidate_rank", 0)
        result["spend"] = _nonnegative_float(payload.get("spend"), "spend", 0)
        preflight_sha256 = str(payload.get("preflight_sha256", "") or "").strip().lower()
        result["preflight_size"] = _nonnegative_int(
            payload.get("preflight_size"), "preflight_size", 0
        )
        result["preflight_duration"] = _nonnegative_float(
            payload.get("preflight_duration"), "preflight_duration", 0
        )
        if preflight_sha256 and not re.fullmatch(r"[0-9a-f]{64}", preflight_sha256):
            raise XPostError("invalid_request", "preflight_sha256无效", 400)
        if require_compliance and (not preflight_sha256 or result["preflight_size"] <= 0):
            raise XPostError("invalid_request", "每日计划缺少完整媒体预检指纹", 400)
        result["preflight_sha256"] = preflight_sha256
        result.update(_compliance_counts(payload, require_all=require_compliance))
        if source_type == "material":
            # Preserve the original canary idempotency identity so historical
            # published rows remain replayable without another X write.
            default_key = "xpost:%s:%s:%s" % (
                result["source_date"],
                result["account_id"],
                result["material_key"],
            )
        else:
            default_key = "xpost:drama:%s:%s:%s" % (
                result["source_date"],
                result["account_id"],
                result["episode_key"],
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
                comparison_fields = list(
                    (
                        "idempotency_key",
                        "source_type",
                        "material_key",
                        "episode_key",
                        "drama_replay_generation",
                    )
                    + QUEUE_FIELDS
                )
                for field in (
                    "run_id",
                    "catchup_run_id",
                    "schedule_run_id",
                    "manual_run_id",
                    "run_date",
                    "pool_item_id",
                    "drama_pool_item_id",
                    "pool_created_at",
                    "drama_pool_created_at",
                    "episode_number",
                    "name_tag",
                    "candidate_rank",
                    "spend",
                    "original_material_url",
                    "media_repair_trigger_code",
                    "media_repair_job_key",
                    "media_repair_profile",
                    "media_repair_source_sha256",
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
            if values["source_type"] == "material":
                pool = conn.execute(
                    "SELECT * FROM x_post_material_pool WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone()
                if values["pool_item_id"] is None:
                    if pool:
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_occupied",
                            "该素材已进入素材池，发布队列必须绑定对应素材池记录",
                            409,
                        )
                elif (
                    not pool
                    or int(pool["id"]) != values["pool_item_id"]
                    or pool["status"] != "unpublished"
                    or str(pool["created_at"]) != values["pool_created_at"]
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_pool_item_unavailable",
                        "素材池记录不存在、已发布、已变更或与素材不一致",
                        409,
                    )
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_material_already_used",
                        "该素材已被X发布队列占用",
                        409,
                    )
            else:
                drama = conn.execute(
                    "SELECT * FROM x_post_drama_pool WHERE id=?",
                    (values["drama_pool_item_id"],),
                ).fetchone()
                if (
                    not drama
                    or str(drama["content_id"]) != values["content_id"]
                    or str(drama["created_at"]) != values["drama_pool_created_at"]
                    or int(drama["replay_generation"])
                    != int(values["drama_replay_generation"])
                    or drama["status"] in {
                        "completed",
                        "validation_failed",
                        "needs_review",
                    }
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_unavailable",
                        "短剧池记录不存在、已完成、不可用或已变更",
                        409,
                    )
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE episode_key=?",
                    (values["episode_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_episode_already_used",
                        "该短剧集数已被X发布队列占用",
                        409,
                    )
            if (
                values["schedule_run_id"] is None
                and values["manual_run_id"] is None
                and conn.execute(
                "SELECT id FROM x_post_queue "
                "WHERE account_id=? AND run_date=? "
                "AND schedule_run_id IS NULL AND manual_run_id IS NULL",
                (values["account_id"], values["run_date"]),
                ).fetchone()
            ):
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

    def add_pool_materials(
        self,
        material_ids,
        actor=None,
        validation_checks=None,
    ):
        if (
            not isinstance(material_ids, list)
            or not material_ids
            or len(material_ids) > 100
        ):
            raise XPostError(
                "invalid_request",
                "material_ids必须是包含1到100项的数组",
                400,
            )
        requested_count = len(material_ids)
        normalized = []
        seen = set()
        skipped_items = []
        duplicate_input_count = 0
        for raw in material_ids:
            key = normalize_material_key(raw)
            if key in seen:
                duplicate_input_count += 1
                skipped_items.append(
                    {
                        "material_id": key,
                        "code": "x_post_pool_material_duplicate_input",
                        "message": "本次提交中素材ID重复，已跳过",
                    }
                )
                continue
            seen.add(key)
            normalized.append(key)
        if validation_checks is None:
            checks_by_material = {
                material_id: (
                    "material_validation_pending",
                    "素材尚未完成X发布标准校验，当前不可发布",
                )
                for material_id in normalized
            }
        else:
            if not isinstance(validation_checks, list):
                raise XPostError(
                    "invalid_request",
                    "validation_checks必须是数组",
                    400,
                )
            checks_by_material = {}
            for raw in validation_checks:
                if not isinstance(raw, dict):
                    raise XPostError(
                        "invalid_request",
                        "validation_check必须是对象",
                        400,
                    )
                check_material_id = normalize_material_key(raw.get("material_id"))
                if check_material_id not in seen:
                    raise XPostError(
                        "invalid_request",
                        "validation_check素材ID无效",
                        400,
                    )
                raw_code = str(raw.get("error_code", "") or "").strip()
                if raw_code:
                    try:
                        error_code = _clean_token(
                            raw_code, "validation error code", 64
                        )
                    except ValueError:
                        raise XPostError(
                            "invalid_request",
                            "validation_check错误码无效",
                            400,
                        ) from None
                    error_message = redact_text(
                        raw.get("error_message")
                        or "素材未通过X发布标准校验",
                        500,
                    )
                else:
                    error_code = ""
                    error_message = ""
                check_value = (
                    error_code,
                    error_message,
                )
                if (
                    check_material_id in checks_by_material
                    and checks_by_material[check_material_id] != check_value
                ):
                    raise XPostError(
                        "invalid_request",
                        "重复素材的validation_check结果不一致",
                        400,
                    )
                checks_by_material[check_material_id] = check_value
            if set(checks_by_material) != seen:
                raise XPostError(
                    "invalid_request",
                    "validation_checks未覆盖全部素材ID",
                    400,
                )
        actor = actor if isinstance(actor, dict) else {}
        created_by_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        created_by_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        for value, label in (
            (created_by_user_id, "created_by_user_id"),
            (created_by_name, "created_by_name"),
        ):
            if any(ord(char) < 32 for char in value):
                raise XPostError("invalid_request", "%s无效" % label, 400)

        timestamp = utc_now()
        created_ids = []
        created_material_ids = []
        already_in_pool_count = 0
        already_used_count = 0
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for material_key_value in normalized:
                if conn.execute(
                    "SELECT id FROM x_post_material_pool WHERE material_key=?",
                    (material_key_value,),
                ).fetchone():
                    already_in_pool_count += 1
                    skipped_items.append(
                        {
                            "material_id": material_key_value,
                            "code": "x_post_pool_material_already_exists",
                            "message": "素材已在X素材池中",
                        }
                    )
                    continue
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE material_key=?",
                    (material_key_value,),
                ).fetchone():
                    already_used_count += 1
                    skipped_items.append(
                        {
                            "material_id": material_key_value,
                            "code": "x_post_pool_material_already_used",
                            "message": "素材已有X发布历史，已跳过",
                        }
                    )
                    continue
                try:
                    cursor = conn.execute(
                        "INSERT INTO x_post_material_pool("
                        "material_key,material_id,status,created_by_user_id,created_by_name,"
                        "last_checked_at,last_error_code,last_error_message,"
                        "created_at,updated_at"
                        ") VALUES(?,?,'unpublished',?,?,?,?,?,?,?)",
                        (
                            material_key_value,
                            material_key_value,
                            created_by_user_id,
                            created_by_name,
                            timestamp,
                            checks_by_material[material_key_value][0],
                            checks_by_material[material_key_value][1],
                            timestamp,
                            timestamp,
                        ),
                    )
                    created_ids.append(int(cursor.lastrowid))
                    created_material_ids.append(material_key_value)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    raise XPostError(
                        "x_post_storage_conflict",
                        "素材池唯一约束冲突，请重试",
                        409,
                    ) from exc
            if created_ids:
                rows = conn.execute(
                    "SELECT * FROM x_post_material_pool WHERE id IN (%s) ORDER BY id"
                    % ",".join("?" for _item in created_ids),
                    tuple(created_ids),
                ).fetchall()
            else:
                rows = []
            conn.commit()
        created_items = [_row_dict(row) for row in rows]
        return {
            "items": created_items,
            "requested_count": requested_count,
            "unique_count": len(normalized),
            "added_count": len(created_items),
            "created_count": len(created_items),
            "skipped_count": len(skipped_items),
            "duplicate_input_count": duplicate_input_count,
            "already_in_pool_count": already_in_pool_count,
            "already_used_count": already_used_count,
            "skipped_items": skipped_items,
            "available_count": sum(
                1
                for material_id in created_material_ids
                if not _material_validation_is_blocking(
                    checks_by_material[material_id][0]
                )
            ),
            "validation_failed_count": sum(
                1
                for material_id in created_material_ids
                if _material_validation_is_blocking(
                    checks_by_material[material_id][0]
                )
            ),
        }

    def available_pool_items(self, limit=50):
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise XPostError("invalid_request", "limit无效", 400) from None
        if limit <= 0 or limit > 1000:
            raise XPostError("invalid_request", "limit必须在1到1000之间", 400)
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT p.id,p.material_key,p.material_id,p.created_at "
                "FROM x_post_material_pool p "
                "WHERE p.status='unpublished' "
                "AND (p.last_error_code='' OR p.last_error_code IN %s) "
                "AND NOT EXISTS(SELECT 1 FROM x_post_queue q "
                "WHERE q.pool_item_id=p.id OR q.material_key=p.material_key) "
                "ORDER BY p.created_at DESC,p.id DESC LIMIT ?"
                % _NONBLOCKING_MATERIAL_VALIDATION_SQL,
                (limit,),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def record_pool_checks(self, checks):
        if not isinstance(checks, list) or not checks or len(checks) > 100:
            raise XPostError(
                "invalid_request",
                "checks必须是包含1到100项的数组",
                400,
            )
        normalized = []
        seen = set()
        for raw in checks:
            if not isinstance(raw, dict):
                raise XPostError("invalid_request", "check必须是对象", 400)
            pool_item_id = _positive_int(raw.get("pool_item_id"), "pool_item_id")
            if pool_item_id in seen:
                raise XPostError("invalid_request", "pool_item_id重复", 400)
            seen.add(pool_item_id)
            raw_code = str(raw.get("error_code", "") or "").strip()
            try:
                code = _clean_token(raw_code, "error code", 64) if raw_code else ""
            except ValueError:
                raise XPostError("invalid_request", "error_code无效", 400) from None
            message = redact_text(raw.get("error_message", ""), 500) if code else ""
            normalized.append((pool_item_id, code, message))
        timestamp = utc_now()
        updated = 0
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for pool_item_id, code, message in normalized:
                row = conn.execute(
                    "SELECT id,status,material_key FROM x_post_material_pool WHERE id=?",
                    (pool_item_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    raise XPostError(
                        "x_post_pool_item_not_found",
                        "素材池记录不存在",
                        404,
                    )
                if row["status"] != "unpublished" or conn.execute(
                    "SELECT 1 FROM x_post_queue "
                    "WHERE pool_item_id=? OR material_key=?",
                    (pool_item_id, row["material_key"]),
                ).fetchone():
                    continue
                cursor = conn.execute(
                    "UPDATE x_post_material_pool SET last_checked_at=?,last_error_code=?,"
                    "last_error_message=?,updated_at=? WHERE id=? AND status='unpublished'",
                    (timestamp, code, message, timestamp, pool_item_id),
                )
                updated += int(cursor.rowcount or 0)
            conn.commit()
        return {"updated_count": updated}

    def query_pool(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        page, page_size = self._pagination(payload)
        availability_sql = (
            "CASE "
            "WHEN p.status='published' THEN 'published' "
            "WHEN q.id IS NOT NULL AND "
            "(COALESCE(l.unknown_outcome,0)=1 OR l.status='post_creating') "
            "THEN 'needs_review' "
            "WHEN q.id IS NOT NULL AND COALESCE(l.status,q.status)='failed' "
            "THEN 'failed' "
            "WHEN q.id IS NOT NULL THEN 'occupied' "
            "WHEN p.last_error_code<>'' AND p.last_error_code NOT IN %s "
            "THEN 'validation_failed' "
            "ELSE 'available' END"
            % _NONBLOCKING_MATERIAL_VALIDATION_SQL
        )
        clauses = []
        values = []
        status = str(payload.get("status", "") or "").strip().lower()
        if status:
            if status not in {"unpublished", "published"}:
                raise XPostError("invalid_request", "status筛选值无效", 400)
            clauses.append("p.status=?")
            values.append(status)
        availability = str(payload.get("availability", "") or "").strip().lower()
        if availability:
            if availability not in {
                "available",
                "validation_failed",
                "occupied",
                "failed",
                "needs_review",
                "published",
            }:
                raise XPostError("invalid_request", "availability筛选值无效", 400)
            clauses.append("(%s)=?" % availability_sql)
            values.append(availability)
        material_id = str(payload.get("material_id", "") or "").strip()
        if material_id:
            clauses.append("p.material_key=?")
            values.append(normalize_material_key(material_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        join_sql = (
            " FROM x_post_material_pool p "
            "LEFT JOIN x_post_queue q "
            "ON q.pool_item_id=p.id OR q.material_key=p.material_key "
            "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id"
        )
        select_sql = (
            "SELECT p.id,p.material_key,p.material_id,p.status,p.published_at,"
            "p.last_checked_at,p.last_error_code,p.last_error_message,"
            "p.created_by_user_id,p.created_by_name,p.created_at,p.updated_at,"
            "q.id AS queue_id,q.run_id,q.run_date,q.account_id,"
            "q.account_username,q.status AS queue_status,"
            "COALESCE(l.status,'') AS publish_status,"
            "COALESCE(l.unknown_outcome,0) AS unknown_outcome,"
            "COALESCE(l.x_post_url,'') AS preview_url,"
            "COALESCE(l.error_code,'') AS publish_error_code,"
            "COALESCE(l.error_message,'') AS publish_error_message,"
            + availability_sql
            + " AS availability"
            + join_sql
        )
        offset = (page - 1) * page_size
        with contextlib.closing(_connect(self.db_path)) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*)" + join_sql + where,
                    tuple(values),
                ).fetchone()[0]
            )
            rows = conn.execute(
                select_sql
                + where
                + " ORDER BY p.created_at DESC,p.id DESC LIMIT ? OFFSET ?",
                tuple(values) + (page_size, offset),
            ).fetchall()
            summary = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN p.status='unpublished' THEN 1 ELSE 0 END) AS unpublished,"
                "SUM(CASE WHEN p.status='published' THEN 1 ELSE 0 END) AS published,"
                "SUM(CASE WHEN p.status='unpublished' AND q.id IS NULL "
                "AND (p.last_error_code='' OR p.last_error_code IN %s) "
                "THEN 1 ELSE 0 END) AS available,"
                "SUM(CASE WHEN p.status='unpublished' AND q.id IS NOT NULL "
                "THEN 1 ELSE 0 END) AS occupied"
                % _NONBLOCKING_MATERIAL_VALIDATION_SQL
                + join_sql
            ).fetchone()
        items = []
        for row in rows:
            item = _row_dict(row)
            item["unknown_outcome"] = bool(item["unknown_outcome"])
            item["last_error_message"] = redact_text(item["last_error_message"], 500)
            item["publish_error_message"] = redact_text(
                item["publish_error_message"], 500
            )
            items.append(item)
        return {
            "items": items,
            "summary": {
                key: int(summary[key] or 0)
                for key in ("total", "unpublished", "published", "available", "occupied")
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": (total + page_size - 1) // page_size,
            },
        }

    def delete_pool_material(self, pool_item_id):
        pool_item_id = _positive_int(pool_item_id, "pool_item_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_material_pool WHERE id=?",
                (pool_item_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError(
                    "x_post_pool_item_not_found",
                    "素材池记录不存在",
                    404,
                )
            if row["status"] == "published":
                conn.rollback()
                raise XPostError(
                    "x_post_pool_item_published",
                    "已发布素材必须保留审计记录",
                    409,
                )
            if conn.execute(
                "SELECT 1 FROM x_post_queue "
                "WHERE pool_item_id=? OR material_key=?",
                (pool_item_id, row["material_key"]),
            ).fetchone():
                conn.rollback()
                raise XPostError(
                    "x_post_pool_item_occupied",
                    "已被发布队列占用的素材不能删除",
                    409,
                )
            conn.execute("DELETE FROM x_post_material_pool WHERE id=?", (pool_item_id,))
            conn.commit()
        item = _row_dict(row)
        item["deleted"] = True
        return item

    @staticmethod
    def _drama_validation_check(raw, expected_content_id):
        if not isinstance(raw, dict):
            raise XPostError(
                "invalid_request",
                "短剧校验结果必须是对象",
                400,
            )
        content_id = _drama_content_id(raw.get("content_id"))
        if content_id != expected_content_id:
            raise XPostError(
                "invalid_request",
                "短剧校验结果与短剧ID不一致",
                400,
            )
        raw_code = str(raw.get("error_code", "") or "").strip()
        if raw_code:
            try:
                error_code = _clean_token(
                    raw_code,
                    "drama validation error code",
                    64,
                )
            except ValueError:
                raise XPostError(
                    "invalid_request",
                    "短剧校验错误码无效",
                    400,
                ) from None
            return {
                "content_id": content_id,
                "status": "validation_failed",
                "error_code": error_code,
                "error_message": redact_text(
                    raw.get("error_message") or "短剧未通过X发布校验",
                    500,
                ),
                "drama_name": "",
                "description": "",
                "language": "",
                "labels": "",
                "name_tag": "",
                "free_episode_count": 0,
            }
        drama_name = _clean_text(
            raw.get("drama_name"),
            "drama_name",
            500,
        )
        description = _clean_text(
            re.sub(r"\s+", " ", str(raw.get("description") or "")).strip(),
            "description",
            10000,
        )
        language = _clean_text(
            raw.get("language"),
            "language",
            64,
        )
        labels_value = raw.get("labels")
        if isinstance(labels_value, list):
            labels_value = ",".join(
                str(value or "").strip()
                for value in labels_value
                if str(value or "").strip()
            )
        labels = str(labels_value or "").strip()
        if len(labels) > 1000 or any(ord(char) < 32 for char in labels):
            raise XPostError("invalid_request", "labels无效", 400)
        name_tag = _clean_text(raw.get("name_tag"), "name_tag", 500)
        free_episode_count = _positive_int(
            raw.get("free_episode_count"),
            "free_episode_count",
        )
        if free_episode_count > 10000:
            raise XPostError(
                "invalid_request",
                "免费剧集数超过支持范围",
                400,
            )
        build_drama_episode_post_text(
            "https://gy.g2flow.com/s2l/1.html",
            1,
            drama_name,
            description,
        )
        return {
            "content_id": content_id,
            "status": "pending",
            "error_code": "",
            "error_message": "",
            "drama_name": drama_name,
            "description": description,
            "language": language,
            "labels": labels,
            "name_tag": name_tag,
            "free_episode_count": free_episode_count,
        }

    def add_drama_pool_items(
        self,
        drama_ids,
        validation_checks,
        actor=None,
    ):
        if not isinstance(drama_ids, list) or not drama_ids or len(drama_ids) > 100:
            raise XPostError(
                "invalid_request",
                "drama_ids必须是包含1到100项的数组",
                400,
            )
        if (
            not isinstance(validation_checks, list)
            or len(validation_checks) != len(drama_ids)
        ):
            raise XPostError(
                "invalid_request",
                "validation_checks必须与drama_ids逐项对应",
                400,
            )
        content_ids = []
        seen = set()
        for raw in drama_ids:
            content_id = _drama_content_id(raw)
            if content_id in seen:
                raise XPostError(
                    "x_post_drama_pool_item_exists",
                    "本次提交包含重复短剧%s" % content_id,
                    409,
                )
            seen.add(content_id)
            content_ids.append(content_id)
        checks = {}
        for raw in validation_checks:
            content_id = _drama_content_id(
                raw.get("content_id") if isinstance(raw, dict) else ""
            )
            if content_id not in seen or content_id in checks:
                raise XPostError(
                    "invalid_request",
                    "短剧校验结果ID无效或重复",
                    400,
                )
            checks[content_id] = self._drama_validation_check(
                raw,
                content_id,
            )
        if set(checks) != seen:
            raise XPostError(
                "invalid_request",
                "短剧校验结果未覆盖全部短剧ID",
                400,
            )
        actor = actor if isinstance(actor, dict) else {}
        created_by_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        created_by_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        timestamp = utc_now()
        created_ids = []
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for content_id in content_ids:
                if conn.execute(
                    "SELECT 1 FROM x_post_drama_pool WHERE content_id=?",
                    (content_id,),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_exists",
                        "短剧%s已在短剧池中" % content_id,
                        409,
                    )
                if conn.execute(
                    "SELECT 1 FROM x_post_queue "
                    "WHERE source_type='drama' AND content_id=?",
                    (content_id,),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_already_used",
                        "短剧%s已有X发布历史，不能重新入池" % content_id,
                        409,
                    )
            try:
                for content_id in content_ids:
                    check = checks[content_id]
                    cursor = conn.execute(
                        "INSERT INTO x_post_drama_pool("
                        "content_id,app_id,drama_name,description,language,"
                        "labels,name_tag,status,free_episode_count,"
                        "next_sub_number,published_episode_count,last_checked_at,"
                        "last_error_code,last_error_message,created_by_user_id,"
                        "created_by_name,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            content_id,
                            1479,
                            check["drama_name"],
                            check["description"],
                            check["language"],
                            check["labels"],
                            check["name_tag"],
                            check["status"],
                            check["free_episode_count"],
                            1,
                            0,
                            timestamp,
                            check["error_code"],
                            check["error_message"],
                            created_by_user_id,
                            created_by_name,
                            timestamp,
                            timestamp,
                        ),
                    )
                    created_ids.append(int(cursor.lastrowid))
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_exists",
                    "短剧池唯一约束冲突",
                    409,
                ) from exc
            rows = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id IN (%s) "
                "ORDER BY created_at,id"
                % ",".join("?" for _item in created_ids),
                tuple(created_ids),
            ).fetchall()
            conn.commit()
        return {
            "items": [
                {
                    "id": int(row["id"]),
                    "content_id": str(row["content_id"]),
                    "status": str(row["status"]),
                    "free_episode_count": int(
                        row["free_episode_count"] or 0
                    ),
                    "last_error_code": str(
                        row["last_error_code"] or ""
                    ),
                    "last_error_message": redact_text(
                        row["last_error_message"],
                        500,
                    ),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ],
            "created_count": len(rows),
            "available_count": sum(
                row["status"] == "pending" for row in rows
            ),
            "validation_failed_count": sum(
                row["status"] == "validation_failed" for row in rows
            ),
        }

    @staticmethod
    def _drama_assignment_candidates(
        conn,
        account_ids,
        limit,
        premium_account_ids=None,
    ):
        account_ids = _schedule_account_ids(account_ids)
        premium_account_ids = (
            set(_schedule_account_ids(premium_account_ids))
            if premium_account_ids
            else set()
        )
        if not premium_account_ids.issubset(set(account_ids)):
            raise XPostError(
                "invalid_request",
                "Premium账号必须属于当前短剧发布账号范围",
                400,
            )
        if limit < len(account_ids):
            raise XPostError(
                "invalid_request",
                "短剧池扫描上限不能小于发布账号数量",
                400,
            )
        placeholders = ",".join("?" for _item in account_ids)
        foreign_owner = conn.execute(
            "SELECT content_id,assigned_account_id "
            "FROM x_post_drama_pool "
            "WHERE status IN ('pending','active') "
            "AND last_error_code='' "
            "AND next_sub_number<=free_episode_count "
            "AND assigned_account_id>0 "
            "AND assigned_account_id NOT IN (%s) "
            "ORDER BY created_at,id LIMIT 1" % placeholders,
            tuple(account_ids),
        ).fetchone()
        if foreign_owner:
            raise XPostError(
                "x_post_drama_owner_not_configured",
                "短剧%s绑定的账号%s未包含在当前发布设置中"
                % (
                    foreign_owner["content_id"],
                    foreign_owner["assigned_account_id"],
                ),
                409,
            )
        owned_rows = conn.execute(
            "SELECT * FROM x_post_drama_pool "
            "WHERE status IN ('pending','active') "
            "AND last_error_code='' "
            "AND free_episode_count>0 "
            "AND next_sub_number<=free_episode_count "
            "AND assigned_account_id IN (%s) "
            "ORDER BY created_at,id" % placeholders,
            tuple(account_ids),
        ).fetchall()
        owned_by_account = {}
        for row in owned_rows:
            owner_id = int(row["assigned_account_id"])
            if owner_id in owned_by_account:
                raise XPostError(
                    "x_post_storage_conflict",
                    "同一X账号绑定了多部未完成短剧",
                    500,
                )
            owned_by_account[owner_id] = row
        free_account_ids = [
            account_id
            for account_id in account_ids
            if account_id not in owned_by_account
        ]
        unassigned_limit = max(0, limit)
        unassigned_rows = conn.execute(
            "SELECT * FROM x_post_drama_pool "
            "WHERE status IN ('pending','active') "
            "AND last_error_code IN ('','x_long_video_requires_premium') "
            "AND free_episode_count>0 "
            "AND next_sub_number<=free_episode_count "
            "AND assigned_account_id=0 "
            "ORDER BY CASE WHEN priority_at<>'' THEN 0 ELSE 1 END,"
            "priority_at DESC,created_at DESC,id DESC LIMIT ?",
            (unassigned_limit,),
        ).fetchall()
        selected_by_account = {}
        remaining_accounts = list(free_account_ids)
        for row in unassigned_rows:
            if not remaining_accounts:
                break
            if str(row["last_error_code"] or "") in (
                DRAMA_POOL_RETRYABLE_VALIDATION_CODES
            ):
                account_id = next(
                    (
                        value
                        for value in remaining_accounts
                        if value in premium_account_ids
                    ),
                    None,
                )
                if account_id is None:
                    continue
            else:
                account_id = remaining_accounts[0]
            selected_by_account[account_id] = row
            remaining_accounts.remove(account_id)
        assignments = []
        for account_id in account_ids:
            row = owned_by_account.get(account_id)
            if row is None:
                row = selected_by_account.get(account_id)
            if row is None:
                break
            item = _row_dict(row)
            item["candidate_account_id"] = account_id
            assignments.append(item)
        return assignments

    def available_drama_pool_items(
        self,
        limit=50,
        account_ids=None,
        premium_account_ids=None,
    ):
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise XPostError("invalid_request", "limit无效", 400) from None
        if limit <= 0 or limit > 1000:
            raise XPostError(
                "invalid_request",
                "limit必须在1到1000之间",
                400,
            )
        with contextlib.closing(_connect(self.db_path)) as conn:
            blocked = conn.execute(
                "SELECT id,content_id FROM x_post_drama_pool "
                "WHERE status='needs_review' "
                "ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if blocked:
                raise XPostError(
                    "x_post_drama_pool_needs_review",
                    "短剧%s存在待人工确认的发布结果，已暂停后续短剧发布"
                    % blocked["content_id"],
                    409,
                    True,
                )
            if account_ids is not None:
                return self._drama_assignment_candidates(
                    conn,
                    account_ids,
                    limit,
                    premium_account_ids=premium_account_ids,
                )
            rows = conn.execute(
                "SELECT id,content_id,next_sub_number,created_at,"
                "assigned_account_id,assigned_at,assigned_source_queue_id "
                "FROM x_post_drama_pool "
                "WHERE status IN ('pending','active') "
                "AND last_error_code IN ('','x_long_video_requires_premium') "
                "AND free_episode_count>0 "
                "AND next_sub_number<=free_episode_count "
                "ORDER BY CASE WHEN priority_at<>'' THEN 0 ELSE 1 END,"
                "priority_at DESC,created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def record_drama_pool_checks(self, checks, validate_only=False):
        if not isinstance(checks, list) or not checks or len(checks) > 100:
            raise XPostError(
                "invalid_request",
                "checks must contain 1 to 100 drama pool checks",
                400,
            )
        if not isinstance(validate_only, bool):
            raise XPostError(
                "invalid_request",
                "validate_only must be a boolean",
                400,
            )
        normalized = []
        seen = set()
        for raw in checks:
            if not isinstance(raw, dict):
                raise XPostError(
                    "invalid_request",
                    "drama pool check must be an object",
                    400,
                )
            pool_item_id = _positive_int(
                raw.get("pool_item_id"),
                "pool_item_id",
            )
            if pool_item_id in seen:
                raise XPostError(
                    "invalid_request",
                    "pool_item_id must be unique",
                    400,
                )
            seen.add(pool_item_id)
            content_id = _drama_content_id(raw.get("content_id"))
            raw_code = str(raw.get("error_code", "") or "").strip()
            if raw_code:
                try:
                    code = _clean_token(
                        raw_code,
                        "drama pool error code",
                        64,
                    )
                except ValueError:
                    raise XPostError(
                        "invalid_request",
                        "error_code is invalid",
                        400,
                    ) from None
                if code not in (
                    DRAMA_POOL_DETERMINISTIC_REJECTION_CODES
                    | DRAMA_POOL_RETRYABLE_VALIDATION_CODES
                ):
                    raise XPostError(
                        "invalid_request",
                        "error_code is not a deterministic drama rejection",
                        400,
                    )
                expected_error_code = ""
                expected_episode_number = 0
                message = redact_text(
                    raw.get("error_message")
                    or "Drama episode did not pass X media preflight",
                    500,
                )
            else:
                try:
                    expected_error_code = _clean_token(
                        raw.get("expected_error_code"),
                        "expected drama pool error code",
                        64,
                    )
                except ValueError:
                    raise XPostError(
                        "invalid_request",
                        "expected_error_code is invalid",
                        400,
                    ) from None
                if (
                    expected_error_code
                    not in DRAMA_POOL_DETERMINISTIC_REJECTION_CODES
                ):
                    raise XPostError(
                        "invalid_request",
                        "expected_error_code is not a deterministic drama rejection",
                        400,
                    )
                expected_episode_number = _positive_int(
                    raw.get("expected_episode_number"),
                    "expected_episode_number",
                )
                code = ""
                message = ""
            normalized.append(
                (
                    pool_item_id,
                    content_id,
                    code,
                    message,
                    expected_error_code,
                    expected_episode_number,
                )
            )

        timestamp = utc_now()
        updated = 0
        validated = 0
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for (
                pool_item_id,
                content_id,
                code,
                message,
                expected_error_code,
                expected_episode_number,
            ) in normalized:
                row = conn.execute(
                    "SELECT id,content_id,status,assigned_account_id,"
                    "next_sub_number,last_error_code "
                    "FROM x_post_drama_pool WHERE id=?",
                    (pool_item_id,),
                ).fetchone()
                if not row or str(row["content_id"]) != content_id:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_unavailable",
                        "Drama pool check does not match the stored drama",
                        409,
                    )
                has_history = bool(
                    conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE drama_pool_item_id=? OR "
                        "(source_type='drama' AND content_id=?) LIMIT 1",
                        (pool_item_id, content_id),
                    ).fetchone()
                )
                if int(row["assigned_account_id"] or 0) > 0 or has_history:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_bound",
                        "A bound drama cannot be rejected or reassigned",
                        409,
                    )
                if not code:
                    if (
                        str(row["status"]) != "validation_failed"
                        or str(row["last_error_code"] or "")
                        != expected_error_code
                        or int(row["next_sub_number"] or 0)
                        != expected_episode_number
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_drama_pool_revalidation_conflict",
                            "Drama pool state changed before successful revalidation",
                            409,
                        )
                    validated += 1
                    if validate_only:
                        continue
                    cursor = conn.execute(
                        "UPDATE x_post_drama_pool SET status='pending',"
                        "last_checked_at=?,last_error_code='',"
                        "last_error_message='',updated_at=? "
                        "WHERE id=? AND content_id=? "
                        "AND status='validation_failed' "
                        "AND assigned_account_id=0 "
                        "AND next_sub_number=? AND last_error_code=?",
                        (
                            timestamp,
                            timestamp,
                            pool_item_id,
                            content_id,
                            expected_episode_number,
                            expected_error_code,
                        ),
                    )
                    if int(cursor.rowcount or 0) != 1:
                        conn.rollback()
                        raise XPostError(
                            "x_post_drama_pool_revalidation_conflict",
                            "Drama pool state changed before successful revalidation",
                            409,
                        )
                    updated += 1
                    continue
                if str(row["status"]) == "completed":
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_unavailable",
                        "Completed drama cannot be rejected",
                        409,
                    )
                validated += 1
                if validate_only:
                    continue
                if code in DRAMA_POOL_RETRYABLE_VALIDATION_CODES:
                    cursor = conn.execute(
                        "UPDATE x_post_drama_pool SET last_checked_at=?,"
                        "last_error_code=?,last_error_message=?,updated_at=? "
                        "WHERE id=? AND assigned_account_id=0 "
                        "AND status IN ('pending','active')",
                        (
                            timestamp,
                            code,
                            message,
                            timestamp,
                            pool_item_id,
                        ),
                    )
                    updated += int(cursor.rowcount or 0)
                    continue
                cursor = conn.execute(
                    "UPDATE x_post_drama_pool SET status='validation_failed',"
                    "priority_at='',priority_by_user_id='',priority_by_name='',"
                    "last_checked_at=?,last_error_code=?,"
                    "last_error_message=?,updated_at=? "
                    "WHERE id=? AND assigned_account_id=0 "
                    "AND status IN ('pending','active','validation_failed',"
                    "'needs_review')",
                    (
                        timestamp,
                        code,
                        message,
                        timestamp,
                        pool_item_id,
                    ),
                )
                updated += int(cursor.rowcount or 0)
            conn.commit()
        return {
            "updated_count": updated,
            "validated_count": validated,
            "validate_only": validate_only,
        }

    def query_drama_pool(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        page, page_size = self._pagination(payload)
        clauses = []
        values = []
        content_id = str(
            payload.get("drama_id", payload.get("content_id", "")) or ""
        ).strip()
        if content_id:
            clauses.append("p.content_id=?")
            values.append(_drama_content_id(content_id))
        status = str(payload.get("status", "") or "").strip()
        if status:
            if status not in {
                "pending",
                "active",
                "completed",
                "validation_failed",
                "needs_review",
            }:
                raise XPostError(
                    "invalid_request",
                    "短剧状态筛选值无效",
                    400,
                )
            clauses.append("p.status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * page_size
        select_sql = (
            "SELECT p.*,"
            "COALESCE(aq.account_username,'') "
            "AS assigned_account_username,"
            "COALESCE(q.account_id,0) AS last_account_id,"
            "COALESCE(q.account_username,'') AS last_account_username,"
            "COALESCE(l.x_post_url,'') AS last_post_url,"
            "COALESCE(l.error_code,'') AS last_publish_error_code,"
            "COALESCE(l.error_message,'') AS last_publish_error_message,"
            "(SELECT COUNT(*) FROM x_post_queue qh "
            "WHERE qh.drama_pool_item_id=p.id OR "
            "(qh.source_type='drama' AND qh.content_id=p.content_id)"
            ") AS queue_count "
            "FROM x_post_drama_pool p "
            "LEFT JOIN x_post_queue aq "
            "ON aq.id=p.assigned_source_queue_id "
            "LEFT JOIN x_post_queue q ON q.id=("
            "SELECT q2.id FROM x_post_queue q2 "
            "WHERE q2.drama_pool_item_id=p.id "
            "AND q2.drama_replay_generation=p.replay_generation "
            "ORDER BY q2.episode_number DESC,q2.id DESC LIMIT 1"
            ") "
            "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id"
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_drama_pool p" + where,
                    tuple(values),
                ).fetchone()[0]
            )
            rows = conn.execute(
                select_sql
                + where
                + " ORDER BY CASE WHEN p.assigned_account_id=0 "
                "AND p.status IN ('pending','active') "
                "AND p.last_error_code='' "
                "AND p.free_episode_count>0 "
                "AND p.next_sub_number<=p.free_episode_count "
                "AND p.priority_at<>'' THEN 0 ELSE 1 END,"
                "p.priority_at DESC,p.created_at DESC,p.id DESC "
                "LIMIT ? OFFSET ?",
                tuple(values) + (page_size, offset),
            ).fetchall()
            summary = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,"
                "SUM(published_episode_count) AS published_episodes,"
                "SUM(CASE WHEN status IN ('pending','active') "
                "THEN MAX(free_episode_count-published_episode_count,0) "
                "ELSE 0 END) AS remaining_episodes "
                "FROM x_post_drama_pool"
            ).fetchone()
        items = []
        for row in rows:
            item = _row_dict(row)
            item["queue_count"] = int(item["queue_count"] or 0)
            item["has_history"] = item["queue_count"] > 0
            item["delete_block_reason"] = _drama_pool_delete_block_reason(
                item["status"],
                item["has_history"],
            )
            item["deletable"] = not bool(item["delete_block_reason"])
            item["remaining_episode_count"] = max(
                int(item["free_episode_count"] or 0)
                - int(item["published_episode_count"] or 0),
                0,
            )
            item["next_sub_num"] = int(item["next_sub_number"] or 0)
            item["last_error_message"] = redact_text(
                item["last_error_message"],
                500,
            )
            item["last_publish_error_message"] = redact_text(
                item["last_publish_error_message"],
                500,
            )
            items.append(item)
        return {
            "items": items,
            "summary": {
                "total": int(summary["total"] or 0),
                "active": int(summary["active"] or 0),
                "published_episodes": int(
                    summary["published_episodes"] or 0
                ),
                "remaining_episodes": int(
                    summary["remaining_episodes"] or 0
                ),
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": (total + page_size - 1) // page_size,
            },
        }

    def set_drama_pool_priority(
        self,
        pool_item_id,
        high_priority,
        actor=None,
    ):
        pool_item_id = _positive_int(pool_item_id, "pool_item_id")
        if not isinstance(high_priority, bool):
            raise XPostError(
                "invalid_request",
                "high_priority必须是布尔值",
                400,
            )
        actor = actor if isinstance(actor, dict) else {}
        actor_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        actor_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        if (
            not actor_user_id
            or not actor_name
            or any(ord(char) < 32 for char in actor_user_id + actor_name)
        ):
            raise XPostError(
                "invalid_request",
                "短剧高优操作人无效",
                400,
            )
        # Priority ordering must preserve consecutive operator clicks that can
        # occur within the same second; the general ledger timestamp is only
        # second-granular for historical compatibility.
        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id=?",
                (pool_item_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_not_found",
                    "短剧池记录不存在",
                    404,
                )
            eligible = bool(
                str(row["status"]) in {"pending", "active"}
                and int(row["assigned_account_id"] or 0) == 0
                and not str(row["last_error_code"] or "")
                and int(row["free_episode_count"] or 0) > 0
                and int(row["next_sub_number"] or 0)
                <= int(row["free_episode_count"] or 0)
            )
            if high_priority and not eligible:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_priority_conflict",
                    "仅未分配、校验正常且仍有免费集数的短剧可设置高优",
                    409,
                )
            if high_priority:
                cursor = conn.execute(
                    "UPDATE x_post_drama_pool SET priority_at=?,"
                    "priority_by_user_id=?,priority_by_name=?,updated_at=? "
                    "WHERE id=? AND assigned_account_id=0 "
                    "AND status IN ('pending','active') "
                    "AND last_error_code='' "
                    "AND free_episode_count>0 "
                    "AND next_sub_number<=free_episode_count",
                    (
                        timestamp,
                        actor_user_id,
                        actor_name,
                        timestamp,
                        pool_item_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE x_post_drama_pool SET priority_at='',"
                    "priority_by_user_id='',priority_by_name='',updated_at=? "
                    "WHERE id=?",
                    (timestamp, pool_item_id),
                )
            if int(cursor.rowcount or 0) != 1:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_priority_conflict",
                    "短剧状态已变化，请刷新后重试",
                    409,
                )
            conn.commit()
        result = self.query_drama_pool(
            {"drama_id": str(row["content_id"]), "page": 1, "page_size": 1}
        )
        if not result["items"]:
            raise XPostError(
                "x_post_storage_conflict",
                "短剧高优结果无法读取",
                500,
            )
        return result["items"][0]

    def query_drama_pool_episodes(self, pool_item_id, payload=None):
        pool_item_id = _positive_int(pool_item_id, "pool_item_id")
        payload = payload if isinstance(payload, dict) else {}
        page, page_size = self._pagination(payload)
        with contextlib.closing(_connect(self.db_path)) as conn:
            pool = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id=?",
                (pool_item_id,),
            ).fetchone()
            if not pool:
                raise XPostError(
                    "x_post_drama_pool_item_not_found",
                    "短剧池记录不存在",
                    404,
                )
            rows = conn.execute(
                "SELECT q.id AS queue_id,q.episode_number,q.account_id,"
                "q.account_username,q.status AS queue_status,"
                "q.drama_replay_generation,"
                "COALESCE(r.run_date,'') AS run_date,"
                "COALESCE(r.publish_time,'') AS publish_time,"
                "COALESCE(l.status,'') AS publish_status,"
                "COALESCE(l.x_post_url,'') AS post_url,"
                "COALESCE(l.error_code,'') AS error_code,"
                "COALESCE(l.error_message,'') AS error_message,"
                "COALESCE(l.unknown_outcome,0) AS unknown_outcome "
                "FROM x_post_queue q "
                "LEFT JOIN x_post_schedule_run r ON r.id=q.schedule_run_id "
                "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.drama_pool_item_id=? "
                "AND q.drama_replay_generation=? "
                "ORDER BY q.episode_number,q.id",
                (pool_item_id, pool["replay_generation"]),
            ).fetchall()
        by_episode = {int(row["episode_number"]): _row_dict(row) for row in rows}
        total = int(pool["free_episode_count"] or 0)
        start = (page - 1) * page_size + 1
        end = min(total, start + page_size - 1)
        items = []
        for episode_number in range(start, end + 1):
            item = by_episode.get(episode_number)
            if item is None:
                item = {
                    "queue_id": None,
                    "episode_number": episode_number,
                    "drama_replay_generation": int(
                        pool["replay_generation"]
                    ),
                    "account_id": 0,
                    "account_username": "",
                    "queue_status": "pending",
                    "run_date": "",
                    "publish_time": "",
                    "publish_status": "",
                    "post_url": "",
                    "error_code": "",
                    "error_message": "",
                    "unknown_outcome": 0,
                }
            item["unknown_outcome"] = bool(item["unknown_outcome"])
            item["error_message"] = redact_text(
                item["error_message"],
                500,
            )
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

    def delete_drama_pool_items(self, pool_item_ids):
        pool_item_ids = _drama_pool_item_ids(pool_item_ids)
        placeholders = ",".join("?" for _item in pool_item_ids)
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id IN (%s)"
                % placeholders,
                tuple(pool_item_ids),
            ).fetchall()
            rows_by_id = {
                int(row["id"]): row
                for row in rows
            }
            if len(rows_by_id) != len(pool_item_ids):
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_not_found",
                    "部分短剧池记录不存在，未删除任何短剧",
                    404,
                )
            content_ids = [
                str(rows_by_id[pool_item_id]["content_id"])
                for pool_item_id in pool_item_ids
            ]
            content_placeholders = ",".join("?" for _item in content_ids)
            occupied = conn.execute(
                "SELECT 1 FROM x_post_queue "
                "WHERE drama_pool_item_id IN (%s) OR "
                "(source_type='drama' AND content_id IN (%s)) "
                "LIMIT 1"
                % (placeholders, content_placeholders),
                tuple(pool_item_ids) + tuple(content_ids),
            ).fetchone()
            if occupied:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_occupied",
                    "所选短剧中存在发布队列或历史，未删除任何短剧",
                    409,
                )
            if any(
                str(rows_by_id[pool_item_id]["status"] or "").lower()
                not in DRAMA_POOL_DELETABLE_STATUSES
                for pool_item_id in pool_item_ids
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_occupied",
                    "所选短剧包含发布中、已完成或待核查记录，未删除任何短剧",
                    409,
                )
            try:
                cursor = conn.execute(
                    "DELETE FROM x_post_drama_pool WHERE id IN (%s)"
                    % placeholders,
                    tuple(pool_item_ids),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_occupied",
                    "所选短剧已被发布任务占用，未删除任何短剧",
                    409,
                ) from exc
            if int(cursor.rowcount or 0) != len(pool_item_ids):
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "短剧池批量删除数量异常，未删除任何短剧",
                    409,
                )
            conn.commit()
        items = []
        for pool_item_id in pool_item_ids:
            row = rows_by_id[pool_item_id]
            items.append(
                {
                    "id": int(row["id"]),
                    "content_id": str(row["content_id"]),
                    "deleted": True,
                }
            )
        return {
            "items": items,
            "deleted_count": len(items),
        }

    def delete_drama_pool_item(self, pool_item_id):
        pool_item_id = _positive_int(pool_item_id, "pool_item_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id=?",
                (pool_item_id,),
            ).fetchone()
        self.delete_drama_pool_items([pool_item_id])
        if row is None:
            raise XPostError(
                "x_post_drama_pool_item_not_found",
                "短剧池记录不存在",
                404,
            )
        item = _row_dict(row)
        item["deleted"] = True
        return item

    def reset_drama_pool_for_replay(
        self,
        pool_item_ids,
        *,
        actor,
        reason,
        expected_snapshots,
        validate_only=True,
    ):
        pool_item_ids = _drama_pool_item_ids(pool_item_ids)
        if len(pool_item_ids) > MAX_DRAMA_POOL_REPLAY_SIZE:
            raise XPostError(
                "invalid_request",
                "Too many drama pool items were requested for replay",
                400,
            )
        if reason != DRAMA_REPLAY_REASON:
            raise XPostError(
                "invalid_request",
                "Drama replay reason is not the approved policy version",
                400,
            )
        if not isinstance(validate_only, bool):
            raise XPostError(
                "invalid_request",
                "validate_only must be a boolean",
                400,
            )
        actor = actor if isinstance(actor, dict) else {}
        actor_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        actor_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        if (
            not actor_user_id
            or not actor_name
            or any(
                ord(char) < 32
                for value in (actor_user_id, actor_name)
                for char in value
            )
        ):
            raise XPostError(
                "invalid_request",
                "Drama replay actor is incomplete",
                400,
            )
        if not isinstance(expected_snapshots, list):
            raise XPostError(
                "invalid_request",
                "expected_snapshots must be a list",
                400,
            )
        expected_by_id = {}
        for raw in expected_snapshots:
            if not isinstance(raw, dict):
                raise XPostError(
                    "invalid_request",
                    "Replay snapshot must be an object",
                    400,
                )
            pool_item_id = _positive_int(
                raw.get("pool_item_id"),
                "pool_item_id",
            )
            if pool_item_id in expected_by_id:
                raise XPostError(
                    "invalid_request",
                    "Replay snapshots must be unique",
                    400,
                )
            expected_by_id[pool_item_id] = {
                "content_id": _drama_content_id(
                    raw.get("content_id")
                ),
                "status": str(raw.get("status", "") or "").strip(),
                "replay_generation": _positive_int(
                    raw.get("replay_generation"),
                    "replay_generation",
                ),
                "free_episode_count": _positive_int(
                    raw.get("free_episode_count"),
                    "free_episode_count",
                ),
                "published_episode_count": _nonnegative_int(
                    raw.get("published_episode_count"),
                    "published_episode_count",
                ),
                "next_sub_number": _positive_int(
                    raw.get("next_sub_number"),
                    "next_sub_number",
                ),
                "assigned_account_id": _positive_int(
                    raw.get("assigned_account_id"),
                    "assigned_account_id",
                ),
            }
        if set(expected_by_id) != set(pool_item_ids):
            raise XPostError(
                "invalid_request",
                "Replay snapshots do not match the selected drama rows",
                400,
            )

        timestamp = utc_now()
        placeholders = ",".join("?" for _item in pool_item_ids)
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT id,status FROM x_post_schedule_run "
                "WHERE source_type='drama' "
                "AND status IN ('claimed','queued','running') "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if running:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_replay_run_in_progress",
                    "A drama schedule run is still active",
                    409,
                )
            unsettled = conn.execute(
                "SELECT q.id FROM x_post_queue q "
                "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.source_type='drama' AND ("
                "q.status IN ('queued','reserved','publishing') "
                "OR COALESCE(l.unknown_outcome,0)=1"
                ") ORDER BY q.id LIMIT 1"
            ).fetchone()
            if unsettled:
                conn.rollback()
                raise XPostError(
                    "x_post_drama_replay_queue_in_progress",
                    "A drama queue is unsettled",
                    409,
                )
            rows = conn.execute(
                "SELECT * FROM x_post_drama_pool WHERE id IN (%s)"
                % placeholders,
                tuple(pool_item_ids),
            ).fetchall()
            rows_by_id = {int(row["id"]): row for row in rows}
            if set(rows_by_id) != set(pool_item_ids):
                conn.rollback()
                raise XPostError(
                    "x_post_drama_pool_item_not_found",
                    "A selected drama pool row does not exist",
                    404,
                )

            results = []
            for pool_item_id in pool_item_ids:
                row = rows_by_id[pool_item_id]
                expected = expected_by_id[pool_item_id]
                actual = {
                    "content_id": str(row["content_id"]),
                    "status": str(row["status"]),
                    "replay_generation": int(
                        row["replay_generation"]
                    ),
                    "free_episode_count": int(
                        row["free_episode_count"]
                    ),
                    "published_episode_count": int(
                        row["published_episode_count"]
                    ),
                    "next_sub_number": int(row["next_sub_number"]),
                    "assigned_account_id": int(
                        row["assigned_account_id"]
                    ),
                }
                if actual != expected:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_replay_snapshot_conflict",
                        "Drama replay state changed after operator review",
                        409,
                    )
                if (
                    actual["published_episode_count"] <= 0
                    or actual["assigned_account_id"] <= 0
                    or str(row["status"]) not in {
                        "active",
                        "completed",
                    }
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_replay_not_eligible",
                        "Only confirmed previously published dramas can "
                        "start a replay generation",
                        409,
                    )
                queue_rows = conn.execute(
                    "SELECT q.id,q.status,l.status AS log_status,"
                    "q.episode_number,"
                    "COALESCE(l.unknown_outcome,0) AS unknown_outcome,"
                    "COALESCE(l.x_post_id,'') AS x_post_id,"
                    "COALESCE(l.x_post_url,'') AS x_post_url "
                    "FROM x_post_queue q "
                    "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                    "WHERE q.source_type='drama' "
                    "AND q.drama_pool_item_id=? "
                    "AND q.drama_replay_generation=? "
                    "ORDER BY q.episode_number,q.id",
                    (
                        pool_item_id,
                        actual["replay_generation"],
                    ),
                ).fetchall()
                if (
                    len(queue_rows)
                    != actual["published_episode_count"]
                    or [
                        int(queue["episode_number"])
                        for queue in queue_rows
                    ]
                    != list(
                        range(
                            1,
                            actual["published_episode_count"] + 1,
                        )
                    )
                    or actual["next_sub_number"]
                    != actual["published_episode_count"] + 1
                    or actual["published_episode_count"]
                    > actual["free_episode_count"]
                    or any(
                        str(queue["status"]) != "published"
                        or str(queue["log_status"] or "") != "published"
                        or int(queue["unknown_outcome"] or 0) != 0
                        or not str(queue["x_post_id"] or "")
                        or not str(queue["x_post_url"] or "")
                        for queue in queue_rows
                    )
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_replay_history_conflict",
                        "Drama replay requires complete confirmed history "
                        "with no failed or ambiguous queue",
                        409,
                    )
                to_generation = actual["replay_generation"] + 1
                results.append(
                    {
                        "pool_item_id": pool_item_id,
                        "content_id": str(row["content_id"]),
                        "from_generation": actual[
                            "replay_generation"
                        ],
                        "to_generation": to_generation,
                        "previous_published_episode_count": actual[
                            "published_episode_count"
                        ],
                        "previous_assigned_account_id": actual[
                            "assigned_account_id"
                        ],
                    }
                )
                if validate_only:
                    continue
                conn.execute(
                    "INSERT INTO x_post_drama_replay_audit("
                    "pool_item_id,content_id,from_generation,to_generation,"
                    "from_status,from_free_episode_count,"
                    "from_next_sub_number,"
                    "from_published_episode_count,"
                    "from_assigned_account_id,from_assigned_at,"
                    "from_assigned_source_queue_id,actor_user_id,"
                    "actor_name,reason,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pool_item_id,
                        str(row["content_id"]),
                        actual["replay_generation"],
                        to_generation,
                        str(row["status"]),
                        actual["free_episode_count"],
                        actual["next_sub_number"],
                        actual["published_episode_count"],
                        actual["assigned_account_id"],
                        str(row["assigned_at"] or ""),
                        row["assigned_source_queue_id"],
                        actor_user_id,
                        actor_name,
                        reason,
                        timestamp,
                    ),
                )
                cursor = conn.execute(
                    "UPDATE x_post_drama_pool SET "
                    "replay_generation=?,status='pending',"
                    "next_sub_number=1,published_episode_count=0,"
                    "assigned_account_id=0,assigned_at='',"
                    "assigned_source_queue_id=NULL,completed_at='',"
                    "last_checked_at=?,last_error_code='',"
                    "last_error_message='',updated_at=? "
                    "WHERE id=? AND replay_generation=? "
                    "AND assigned_account_id=?",
                    (
                        to_generation,
                        timestamp,
                        timestamp,
                        pool_item_id,
                        actual["replay_generation"],
                        actual["assigned_account_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_replay_snapshot_conflict",
                        "Drama replay state changed during reset",
                        409,
                    )
            if validate_only:
                conn.rollback()
            else:
                conn.commit()
        return {
            "items": results,
            "reset_count": 0 if validate_only else len(results),
            "validated_count": len(results),
            "validate_only": validate_only,
            "reason": reason,
        }

    @staticmethod
    def _manual_run_item(row):
        if not row:
            raise XPostError(
                "x_post_manual_run_not_found",
                "X手动发布任务不存在",
                404,
            )
        item = _row_dict(row)
        item["trigger_source"] = _manual_trigger_source(
            item.get("trigger_source")
        )
        item["account_ids"] = _schedule_account_ids(
            _json_array(item.pop("account_ids_json"), "account_ids"),
        )
        item["material_ids"] = _manual_material_ids(
            _json_array(item.pop("material_ids_json"), "material_ids"),
        )
        item["body_template"] = _normalize_post_template(
            item.get("body_template"),
            "material",
        )
        item["error_message"] = redact_text(item.get("error_message"), 500)
        return item

    def get_manual_run(
        self,
        run_id,
        trigger_source=MANUAL_TRIGGER_SOURCE,
    ):
        run_id = _positive_int(run_id, "run_id")
        trigger_source = _manual_trigger_source(trigger_source)
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE id=? AND trigger_source=?",
                (run_id, trigger_source),
            ).fetchone()
            queues = conn.execute(
                "SELECT q.id,q.manual_run_id,q.run_date,q.source_date,"
                "q.account_id,q.account_username,q.material_id,"
                "q.candidate_rank,q.status AS queue_status,"
                # The runner resumes from the queue state, while the log state
                # below supplies the no-retry marker.  A crash during media
                # upload or Post creation therefore remains ``publishing`` and
                # is stopped for review instead of becoming an unparseable
                # sidecar response or being published a second time.
                "q.status AS status,"
                "CASE WHEN l.status='post_creating' "
                "OR COALESCE(l.unknown_outcome,0)=1 "
                "THEN 1 ELSE 0 END AS unknown_outcome,"
                "COALESCE(l.id,0) AS log_id,"
                "COALESCE(l.x_post_id,'') AS post_id,"
                "COALESCE(l.x_post_url,'') AS preview_url,"
                "COALESCE(l.error_code,'') AS error_code,"
                "COALESCE(l.error_message,'') AS error_message,"
                "q.created_at,q.updated_at "
                "FROM x_post_queue q "
                "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.manual_run_id=? "
                "ORDER BY q.candidate_rank,q.id",
                (run_id,),
            ).fetchall()
            conn.commit()
        item = self._manual_run_item(row)
        item["queues"] = []
        for queue in queues:
            queue_item = _row_dict(queue)
            queue_item["unknown_outcome"] = bool(
                queue_item["unknown_outcome"]
            )
            queue_item["error_message"] = redact_text(
                queue_item["error_message"],
                500,
            )
            item["queues"].append(queue_item)
        return item

    def create_manual_run(
        self,
        material_ids,
        account_ids,
        idempotency_key,
        actor=None,
    ):
        material_ids = _manual_material_ids(material_ids)
        account_ids = _schedule_account_ids(account_ids)
        if len(material_ids) != len(account_ids):
            raise XPostError(
                "x_post_manual_scope_mismatch",
                "手动发布的素材数必须与目标账号数一致",
                400,
            )
        idempotency_key = _manual_idempotency_key(idempotency_key)
        actor = actor if isinstance(actor, dict) else {}
        actor_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        actor_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        if (
            not actor_user_id
            or not actor_name
            or any(ord(char) < 32 for char in actor_user_id + actor_name)
        ):
            raise XPostError(
                "invalid_request",
                "手动发布操作人无效",
                400,
            )
        run_date = _beijing_today()
        source_date = (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - timedelta(days=1)
        ).isoformat()
        timestamp = utc_now()
        accounts_json = json.dumps(account_ids, separators=(",", ":"))
        materials_json = json.dumps(material_ids, separators=(",", ":"))
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_manual_run WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                same = bool(
                    str(existing["trigger_source"])
                    == MANUAL_TRIGGER_SOURCE
                    and str(existing["run_date"]) == run_date
                    and str(existing["source_date"]) == source_date
                    and str(existing["account_ids_json"]) == accounts_json
                    and str(existing["material_ids_json"]) == materials_json
                    and str(existing["actor_user_id"]) == actor_user_id
                )
                if not same:
                    conn.rollback()
                    raise XPostError(
                        "x_post_idempotency_conflict",
                        "手动发布幂等键已对应其他任务",
                        409,
                    )
                run_id = int(existing["id"])
                conn.commit()
                result = self.get_manual_run(run_id)
                result["created"] = False
                return result

            placeholders = ",".join("?" for _item in material_ids)
            in_pool = conn.execute(
                "SELECT material_key FROM x_post_material_pool "
                "WHERE material_key IN (%s) LIMIT 1" % placeholders,
                tuple(material_ids),
            ).fetchone()
            already_used = conn.execute(
                "SELECT material_key FROM x_post_queue "
                "WHERE material_key IN (%s) LIMIT 1" % placeholders,
                tuple(material_ids),
            ).fetchone()
            if in_pool or already_used:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_material_unavailable",
                    "所选素材已进入素材池或已被发布队列占用",
                    409,
                )
            config = conn.execute(
                "SELECT body_template FROM x_post_schedule_config "
                "WHERE source_type='material'",
            ).fetchone()
            if not config:
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_not_found",
                    "素材发布文案设置不存在",
                    404,
                )
            body_template = _normalize_post_template(
                config["body_template"],
                "material",
            )
            try:
                cursor = conn.execute(
                    "INSERT INTO x_post_manual_run("
                    "idempotency_key,trigger_source,run_date,source_date,"
                    "account_ids_json,"
                    "material_ids_json,body_template,actor_user_id,actor_name,"
                    "status,expected_count,created_at,updated_at"
                    ") VALUES(?,? ,?,?,?,?,?,?,?,'queued',?,?,?)",
                    (
                        idempotency_key,
                        MANUAL_TRIGGER_SOURCE,
                        run_date,
                        source_date,
                        accounts_json,
                        materials_json,
                        body_template,
                        actor_user_id,
                        actor_name,
                        len(account_ids),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "手动发布任务唯一约束冲突",
                    409,
                ) from exc
            run_id = int(cursor.lastrowid)
            conn.commit()
        result = self.get_manual_run(run_id)
        result["created"] = True
        return result

    def create_auto_template_run(
        self,
        material_id,
        account_id,
        external_task_key,
        template_ref,
        template_version,
        body_template,
        actor=None,
    ):
        material_ids = _manual_material_ids([material_id])
        account_ids = _schedule_account_ids([account_id])
        external_task_key = _auto_provenance_token(
            external_task_key,
            "external_task_key",
        )
        template_ref = _auto_provenance_token(
            template_ref,
            "template_ref",
        )
        template_version = _positive_int(
            template_version,
            "template_version",
        )
        body_template = _normalize_post_template(
            body_template,
            "material",
        )
        body_template_sha256 = hashlib.sha256(
            body_template.encode("utf-8")
        ).hexdigest()
        actor = actor if isinstance(actor, dict) else {}
        actor_user_id = str(actor.get("user_id", "") or "").strip()[:255]
        actor_name = str(
            actor.get("name", "") or actor.get("email", "") or ""
        ).strip()[:255]
        if (
            not actor_user_id
            or not actor_name
            or any(ord(char) < 32 for char in actor_user_id + actor_name)
        ):
            raise XPostError(
                "invalid_request",
                "auto template actor invalid",
                400,
            )
        run_date = _beijing_today()
        source_date = (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - timedelta(days=1)
        ).isoformat()
        timestamp = utc_now()
        accounts_json = json.dumps(account_ids, separators=(",", ":"))
        materials_json = json.dumps(material_ids, separators=(",", ":"))
        idempotency_key = "xpost:auto-template:v1:%s" % hashlib.sha256(
            external_task_key.encode("utf-8")
        ).hexdigest()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE external_task_key=?",
                (external_task_key,),
            ).fetchone()
            if existing:
                same = bool(
                    str(existing["trigger_source"])
                    == AUTO_TEMPLATE_TRIGGER_SOURCE
                    and str(existing["idempotency_key"]) == idempotency_key
                    and str(existing["account_ids_json"]) == accounts_json
                    and str(existing["material_ids_json"]) == materials_json
                    and str(existing["template_ref"]) == template_ref
                    and int(existing["template_version"])
                    == template_version
                    and str(existing["body_template"])
                    == body_template
                    and str(existing["body_template_sha256"])
                    == body_template_sha256
                    and str(existing["actor_user_id"]) == actor_user_id
                )
                if not same:
                    conn.rollback()
                    raise XPostError(
                        "x_post_auto_template_idempotency_conflict",
                        "auto template task key already identifies another run",
                        409,
                    )
                run_id = int(existing["id"])
                conn.commit()
                result = self.get_manual_run(
                    run_id,
                    AUTO_TEMPLATE_TRIGGER_SOURCE,
                )
                result["created"] = False
                return result

            idempotency_conflict = conn.execute(
                "SELECT 1 FROM x_post_manual_run WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if idempotency_conflict:
                conn.rollback()
                raise XPostError(
                    "x_post_auto_template_idempotency_conflict",
                    "auto template task key hash conflicts with another run",
                    409,
                )

            material_key = material_ids[0]
            in_pool = conn.execute(
                "SELECT 1 FROM x_post_material_pool "
                "WHERE material_key=? LIMIT 1",
                (material_key,),
            ).fetchone()
            already_used = conn.execute(
                "SELECT 1 FROM x_post_queue "
                "WHERE material_key=? LIMIT 1",
                (material_key,),
            ).fetchone()
            if in_pool or already_used:
                conn.rollback()
                raise XPostError(
                    "x_post_auto_template_material_unavailable",
                    "selected material is already reserved by the X ledger or pool",
                    409,
                )
            try:
                cursor = conn.execute(
                    "INSERT INTO x_post_manual_run("
                    "idempotency_key,trigger_source,external_task_key,"
                    "template_ref,template_version,body_template_sha256,"
                    "run_date,source_date,account_ids_json,material_ids_json,"
                    "body_template,actor_user_id,actor_name,status,"
                    "expected_count,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',1,?,?)",
                    (
                        idempotency_key,
                        AUTO_TEMPLATE_TRIGGER_SOURCE,
                        external_task_key,
                        template_ref,
                        template_version,
                        body_template_sha256,
                        run_date,
                        source_date,
                        accounts_json,
                        materials_json,
                        body_template,
                        actor_user_id,
                        actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "auto template run unique constraint conflict",
                    409,
                ) from exc
            run_id = int(cursor.lastrowid)
            conn.commit()
        result = self.get_manual_run(
            run_id,
            AUTO_TEMPLATE_TRIGGER_SOURCE,
        )
        result["created"] = True
        return result

    def claim_manual_run(
        self,
        trigger_source=MANUAL_TRIGGER_SOURCE,
    ):
        trigger_source = _manual_trigger_source(trigger_source)
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE trigger_source=? "
                "AND status IN ('queued','running') "
                "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,"
                "created_at,id LIMIT 1",
                (trigger_source,),
            ).fetchone()
            if not row:
                conn.commit()
                return {"found": False, "run": None}
            run_id = int(row["id"])
            if str(row["status"]) == "running":
                interrupted = conn.execute(
                    "SELECT q.id,COALESCE(l.status,'') AS log_status,"
                    "COALESCE(l.unknown_outcome,0) AS unknown_outcome "
                    "FROM x_post_queue q "
                    "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                    "WHERE q.manual_run_id=? AND q.status='publishing' "
                    "ORDER BY q.candidate_rank,q.id LIMIT 1",
                    (run_id,),
                ).fetchone()
                if interrupted:
                    unknown = bool(interrupted["unknown_outcome"]) or str(
                        interrupted["log_status"]
                    ) == "post_creating"
                    status = "needs_review" if unknown else "stopped"
                    code = (
                        "x_post_unknown_outcome"
                        if unknown
                        else (
                            "x_post_auto_template_interrupted"
                            if trigger_source
                            == AUTO_TEMPLATE_TRIGGER_SOURCE
                            else "x_post_manual_interrupted"
                        )
                    )
                    message = (
                        "An X Post creation was interrupted and requires review"
                        if unknown
                        else "An X publish was interrupted before a confirmed Post result"
                    )
                    conn.execute(
                        "UPDATE x_post_manual_run SET status=?,"
                        "unknown_count=CASE WHEN ?=1 AND unknown_count<1 "
                        "THEN 1 ELSE unknown_count END,error_code=?,"
                        "error_message=?,finished_at=?,updated_at=? "
                        "WHERE id=? AND status='running'",
                        (
                            status,
                            1 if unknown else 0,
                            code,
                            message,
                            timestamp,
                            timestamp,
                            run_id,
                        ),
                    )
                    conn.commit()
                    return {
                        "found": True,
                        "run": self.get_manual_run(
                            run_id,
                            trigger_source,
                        ),
                    }
            if str(row["status"]) == "queued":
                cursor = conn.execute(
                    "UPDATE x_post_manual_run SET status='running',"
                    "started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,"
                    "updated_at=? WHERE id=? AND status='queued'",
                    (timestamp, timestamp, run_id),
                )
                if int(cursor.rowcount or 0) != 1:
                    conn.rollback()
                    raise XPostError(
                        "x_post_storage_conflict",
                        "手动发布任务领取冲突",
                        409,
                    )
            conn.commit()
        return {
            "found": True,
            "run": self.get_manual_run(run_id, trigger_source),
        }

    def recover_auto_template_run(self, run_id):
        """Terminalize one stranded auto publish without selecting another run.

        The caller must separately prove that the account's in-process publish
        lock is free.  This transaction then rechecks the exact canonical run
        and installs a durable no-republish fence for a still-active exact
        queue, including the pre-request ``queued`` and log ``reserved``
        windows.  It never republishes or claims a different task.
        """
        run_id = _positive_int(run_id, "run_id")
        timestamp = utc_now()
        recovered = False
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE id=? AND trigger_source=?",
                (run_id, AUTO_TEMPLATE_TRIGGER_SOURCE),
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_run_not_found",
                    "X auto template run does not exist",
                    404,
                )
            queue_rows = conn.execute(
                "SELECT q.*,l.id AS log_id,COALESCE(l.status,'') AS log_status,"
                "COALESCE(l.unknown_outcome,0) AS log_unknown_outcome "
                "FROM x_post_queue q "
                "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.manual_run_id=? ORDER BY q.candidate_rank,q.id LIMIT 2",
                (run_id,),
            ).fetchall()
            if len(queue_rows) > 1:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "auto template run has multiple queues",
                    500,
                )
            if str(row["status"]) in {"queued", "running"} and queue_rows:
                queue = queue_rows[0]
                log_status = str(queue["log_status"] or "")
                unknown = bool(queue["log_unknown_outcome"]) or (
                    log_status == "post_creating"
                )
                if unknown:
                    cursor = conn.execute(
                        "UPDATE x_post_manual_run SET status='needs_review',"
                        "unknown_count=CASE WHEN unknown_count<1 THEN 1 "
                        "ELSE unknown_count END,error_code='x_post_unknown_outcome',"
                        "error_message=?,finished_at=?,updated_at=? "
                        "WHERE id=? AND trigger_source=? "
                        "AND status IN ('queued','running')",
                        (
                            "An X Post creation was interrupted and requires review",
                            timestamp,
                            timestamp,
                            run_id,
                            AUTO_TEMPLATE_TRIGGER_SOURCE,
                        ),
                    )
                    recovered = int(cursor.rowcount or 0) == 1
                elif str(queue["status"] or "") == "published" or (
                    log_status == "published"
                ):
                    # A confirmed Post is authoritative. Its normal completion
                    # transaction also synchronizes the parent run; recovery
                    # must never replace it with a failure fence.
                    pass
                elif str(queue["status"] or "") not in {
                    "queued",
                    "reserved",
                    "publishing",
                    "failed",
                }:
                    conn.rollback()
                    raise XPostError(
                        "x_post_storage_conflict",
                        "auto template queue recovery state is invalid",
                        500,
                    )
                else:
                    code = "x_post_auto_template_interrupted"
                    message = (
                        "An X publish was interrupted before a confirmed Post result"
                    )
                    log_id = int(queue["log_id"] or 0)
                    if log_id:
                        conn.execute(
                            "UPDATE x_post_publish_log SET status='failed',"
                            "error_code=?,error_message=?,unknown_outcome=0,"
                            "updated_at=? WHERE id=? "
                            "AND status IN ('reserved','media_uploading','failed')",
                            (code, message, timestamp, log_id),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO x_post_publish_log("
                            "queue_id,account_id,status,error_code,error_message,"
                            "unknown_outcome,created_at,updated_at"
                            ") VALUES(?,?,'failed',?,?,0,?,?)",
                            (
                                int(queue["id"]),
                                int(queue["account_id"]),
                                code,
                                message,
                                timestamp,
                                timestamp,
                            ),
                        )
                    conn.execute(
                        "UPDATE x_post_queue SET status='failed',updated_at=? "
                        "WHERE id=? AND status IN ('queued','reserved','publishing','failed')",
                        (timestamp, int(queue["id"])),
                    )
                    cursor = conn.execute(
                        "UPDATE x_post_manual_run SET status='stopped',"
                        "queued_count=1,published_count=0,failed_count=1,"
                        "unknown_count=0,error_code=?,error_message=?,"
                        "finished_at=?,updated_at=? WHERE id=? AND trigger_source=? "
                        "AND status IN ('queued','running')",
                        (
                            code,
                            message,
                            timestamp,
                            timestamp,
                            run_id,
                            AUTO_TEMPLATE_TRIGGER_SOURCE,
                        ),
                    )
                    recovered = int(cursor.rowcount or 0) == 1
            conn.commit()
        return {
            "recovered": recovered,
            "run": self.get_manual_run(
                run_id,
                AUTO_TEMPLATE_TRIGGER_SOURCE,
            ),
        }

    def assert_auto_template_publishable(self, queue_id, log_id):
        """Recheck the auto recovery fence while the account lock is held."""
        queue_id = _positive_int(queue_id, "queue_id")
        log_id = _positive_int(log_id, "log_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT q.id,q.status AS queue_status,q.manual_run_id,"
                "mr.trigger_source,mr.status AS run_status,l.id AS log_id,"
                "l.status AS log_status,COALESCE(l.unknown_outcome,0) "
                "AS unknown_outcome FROM x_post_queue q "
                "JOIN x_post_manual_run mr ON mr.id=q.manual_run_id "
                "JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.id=? AND l.id=?",
                (queue_id, log_id),
            ).fetchone()
        if (
            not row
            or str(row["trigger_source"]) != AUTO_TEMPLATE_TRIGGER_SOURCE
            or str(row["run_status"]) != "running"
            or str(row["queue_status"]) not in {"queued", "reserved"}
            or str(row["log_status"]) != "reserved"
            or bool(row["unknown_outcome"])
        ):
            raise XPostError(
                "x_post_auto_template_recovery_fenced",
                "auto template publish was stopped by canonical recovery",
                409,
            )
        return True

    def active_manual_account_ids(self):
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT account_ids_json FROM x_post_manual_run "
                "WHERE status IN ('queued','running') ORDER BY id"
            ).fetchall()
        result = []
        seen = set()
        for row in rows:
            for account_id in _schedule_account_ids(
                _json_array(row["account_ids_json"], "account_ids")
            ):
                if account_id not in seen:
                    seen.add(account_id)
                    result.append(account_id)
        return result

    def record_manual_failure(
        self,
        run_id,
        error_code,
        error_message,
        trigger_source=MANUAL_TRIGGER_SOURCE,
    ):
        run_id = _positive_int(run_id, "run_id")
        trigger_source = _manual_trigger_source(trigger_source)
        try:
            code = _clean_token(
                error_code
                or (
                    "x_post_auto_template_preflight_failed"
                    if trigger_source == AUTO_TEMPLATE_TRIGGER_SOURCE
                    else "x_post_manual_preflight_failed"
                ),
                "error code",
                64,
            )
        except ValueError:
            raise XPostError("invalid_request", "error_code无效", 400) from None
        message = redact_text(
            error_message or "X手动发布预检失败",
            500,
        )
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE id=? AND trigger_source=?",
                (run_id, trigger_source),
            ).fetchone()
            if not row:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_run_not_found",
                    "X手动发布任务不存在",
                    404,
                )
            queue_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue WHERE manual_run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if queue_count:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_plan_exists",
                    "手动发布任务已生成队列，不能记录为预检失败",
                    409,
                )
            if str(row["status"]) == "failed_preflight":
                conn.commit()
                result = self.get_manual_run(run_id, trigger_source)
                result["recorded"] = False
                return result
            if str(row["status"]) not in {"queued", "running"}:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_run_terminal",
                    "手动发布任务已结束",
                    409,
                )
            conn.execute(
                "UPDATE x_post_manual_run SET status='failed_preflight',"
                "queued_count=0,published_count=0,failed_count=0,"
                "unknown_count=0,error_code=?,error_message=?,"
                "finished_at=?,updated_at=? WHERE id=?",
                (code, message, timestamp, timestamp, run_id),
            )
            conn.commit()
        result = self.get_manual_run(run_id, trigger_source)
        result["recorded"] = True
        return result

    def create_manual_plan(
        self,
        run_id,
        candidates,
        trigger_source=MANUAL_TRIGGER_SOURCE,
    ):
        run_id = _positive_int(run_id, "run_id")
        trigger_source = _manual_trigger_source(trigger_source)
        if not isinstance(candidates, list):
            raise XPostError("invalid_request", "candidates必须是数组", 400)
        frozen = self.get_manual_run(run_id, trigger_source)
        account_ids = list(frozen["account_ids"])
        material_ids = list(frozen["material_ids"])
        if trigger_source == AUTO_TEMPLATE_TRIGGER_SOURCE and (
            len(account_ids) != 1
            or len(material_ids) != 1
            or len(candidates) != 1
        ):
            raise XPostError(
                "x_post_auto_template_scope_mismatch",
                "auto template execution requires exactly one account and material",
                409,
            )
        if len(candidates) != len(account_ids):
            raise XPostError(
                "x_post_manual_candidate_shortage",
                "手动发布候选数量与冻结账号数量不一致",
                409,
            )
        prepared = []
        seen_materials = set()
        for index, candidate in enumerate(candidates, 1):
            if not isinstance(candidate, dict):
                raise XPostError("invalid_request", "candidate必须是对象", 400)
            payload = dict(candidate)
            payload.update(
                {
                    "source_type": "material",
                    "body_template": frozen["body_template"],
                    "manual_run_id": run_id,
                }
            )
            values = self._queue_payload(
                payload,
                run_date=frozen["run_date"],
                candidate_rank=index,
                require_compliance=True,
            )
            if (
                trigger_source == AUTO_TEMPLATE_TRIGGER_SOURCE
                and (
                    float(values["preflight_duration"]) <= 0
                    or float(values["preflight_duration"])
                    > AUTO_TEMPLATE_MAX_DURATION_SECONDS
                )
            ):
                raise XPostError(
                    "x_post_auto_template_duration_exceeded",
                    "automatic X materials cannot exceed 600 seconds",
                    409,
                )
            if values["source_date"] != frozen["source_date"]:
                raise XPostError(
                    "x_post_manual_source_mismatch",
                    "手动发布候选来源日期与冻结任务不一致",
                    409,
                )
            if values["account_id"] != account_ids[index - 1]:
                raise XPostError(
                    "x_post_manual_account_mismatch",
                    "手动发布候选账号顺序与冻结任务不一致",
                    409,
                )
            if values["pool_item_id"] is not None:
                raise XPostError(
                    "x_post_manual_pool_forbidden",
                    "手动发布候选不能绑定素材池记录",
                    409,
                )
            if values["material_key"] in seen_materials:
                raise XPostError(
                    "invalid_request",
                    "手动发布候选素材不能重复",
                    400,
                )
            seen_materials.add(values["material_key"])
            values["idempotency_key"] = "xpost:%s:v1:%s:%s" % (
                (
                    "auto-template"
                    if trigger_source == AUTO_TEMPLATE_TRIGGER_SOURCE
                    else "manual"
                ),
                run_id,
                values["account_id"],
            )
            prepared.append(values)
        if seen_materials != set(material_ids):
            raise XPostError(
                "x_post_manual_material_mismatch",
                "手动发布候选素材与冻结任务不一致",
                409,
            )

        timestamp = utc_now()
        columns = ("idempotency_key",) + QUEUE_LEDGER_FIELDS + QUEUE_FIELDS
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM x_post_manual_run "
                "WHERE id=? AND trigger_source=?",
                (run_id, trigger_source),
            ).fetchone()
            if not run:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_run_not_found",
                    "X手动发布任务不存在",
                    404,
                )
            stored_accounts = _schedule_account_ids(
                _json_array(run["account_ids_json"], "account_ids")
            )
            stored_materials = _manual_material_ids(
                _json_array(run["material_ids_json"], "material_ids")
            )
            if stored_accounts != account_ids or stored_materials != material_ids:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "手动发布冻结任务在建队列前发生变化",
                    500,
                )
            existing_queues = conn.execute(
                "SELECT * FROM x_post_queue WHERE manual_run_id=? "
                "ORDER BY candidate_rank,id",
                (run_id,),
            ).fetchall()
            if existing_queues:
                expected = [
                    (values["account_id"], values["material_key"])
                    for values in prepared
                ]
                actual = [
                    (int(row["account_id"]), str(row["material_key"]))
                    for row in existing_queues
                ]
                if actual != expected:
                    conn.rollback()
                    raise XPostError(
                        "x_post_manual_plan_exists",
                        "手动发布任务已存在不同的冻结队列",
                        409,
                    )
                conn.commit()
                result = self.get_manual_run(run_id, trigger_source)
                result["created"] = False
                return result
            if str(run["status"]) not in {"queued", "running"}:
                conn.rollback()
                raise XPostError(
                    "x_post_manual_run_terminal",
                    "手动发布任务已结束，不能再生成队列",
                    409,
                )

            material_placeholders = ",".join("?" for _item in material_ids)
            if conn.execute(
                "SELECT 1 FROM x_post_material_pool "
                "WHERE material_key IN (%s) LIMIT 1"
                % material_placeholders,
                tuple(material_ids),
            ).fetchone():
                conn.rollback()
                raise XPostError(
                    "x_post_manual_material_unavailable",
                    "所选素材已进入素材池",
                    409,
                )
            if conn.execute(
                "SELECT 1 FROM x_post_queue "
                "WHERE material_key IN (%s) LIMIT 1"
                % material_placeholders,
                tuple(material_ids),
            ).fetchone():
                conn.rollback()
                raise XPostError(
                    "x_post_material_already_used",
                    "所选素材已被其他发布队列占用",
                    409,
                )
            account_placeholders = ",".join("?" for _item in account_ids)
            if conn.execute(
                "SELECT 1 FROM x_post_publish_log l "
                "JOIN x_post_queue q ON q.id=l.queue_id "
                "WHERE q.account_id IN (%s) "
                "AND (COALESCE(l.unknown_outcome,0)=1 "
                "OR l.status='post_creating') LIMIT 1"
                % account_placeholders,
                tuple(account_ids),
            ).fetchone():
                conn.rollback()
                raise XPostError(
                    "x_post_unknown_outcome",
                    "所选账号存在待核对发布结果，已停止手动发布",
                    409,
                    True,
                )
            conn.execute(
                "UPDATE x_post_manual_run SET status='running',"
                "queued_count=?,published_count=0,failed_count=0,"
                "unknown_count=0,error_code='',error_message='',"
                "started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,"
                "finished_at='',updated_at=? WHERE id=?",
                (len(prepared), timestamp, timestamp, run_id),
            )
            placeholders = ",".join("?" for _field in columns)
            try:
                for values in prepared:
                    conn.execute(
                        "INSERT INTO x_post_queue("
                        + ",".join(columns)
                        + ",status,created_at,updated_at) VALUES("
                        + placeholders
                        + ",'queued',?,?)",
                        tuple(values[field] for field in columns)
                        + (timestamp, timestamp),
                    )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "手动发布原子建队列唯一约束冲突",
                    409,
                ) from exc
            conn.commit()
        result = self.get_manual_run(run_id, trigger_source)
        result["created"] = True
        return result

    def get_schedule_run(self, run_id):
        run_id = _positive_int(run_id, "run_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM x_post_schedule_run WHERE id=?",
                (run_id,),
            ).fetchone()
        if not row:
            raise XPostError(
                "x_post_schedule_run_not_found",
                "X定时发布批次不存在",
                404,
            )
        item = _row_dict(row)
        item["account_ids"] = _schedule_account_ids(
            _json_array(item.pop("account_ids_json"), "account_ids"),
            allow_empty=True,
        )
        item["body_template"] = _normalize_post_template(
            item.get("body_template"),
            item["source_type"],
        )
        return item

    def query_schedule_plan(self, source_type, run_date, publish_time):
        source_type = _schedule_source_type(source_type)
        run_date = _date_value(run_date, "run_date")
        publish_time = _schedule_publish_time(publish_time)
        run_fields = (
            "id",
            "slot_key",
            "source_type",
            "run_date",
            "publish_time",
            "timezone",
            "config_version",
            "account_ids_json",
            "schedule_mode",
            "body_template",
            "status",
            "expected_count",
            "queued_count",
            "published_count",
            "failed_count",
            "unknown_count",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        queue_fields = (
            "id",
            "schedule_run_id",
            "source_type",
            "run_date",
            "source_date",
            "account_id",
            "candidate_rank",
            "episode_number",
            "status",
            "unknown_outcome",
            "created_at",
            "updated_at",
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN")
            run = conn.execute(
                "SELECT %s FROM x_post_schedule_run "
                "WHERE source_type=? AND run_date=? AND publish_time=?"
                % ",".join(run_fields),
                (source_type, run_date, publish_time),
            ).fetchone()
            queues = (
                conn.execute(
                    "SELECT q.id,q.schedule_run_id,q.source_type,q.run_date,"
                    "q.source_date,q.account_id,q.candidate_rank,"
                    "q.episode_number,q.status,"
                    "CASE WHEN l.status='post_creating' "
                    "OR COALESCE(l.unknown_outcome,0)=1 "
                    "THEN 1 ELSE 0 END AS unknown_outcome,"
                    "q.created_at,q.updated_at "
                    "FROM x_post_queue q "
                    "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                    "WHERE q.schedule_run_id=? "
                    "ORDER BY q.candidate_rank,q.id",
                    (run["id"],),
                ).fetchall()
                if run
                else []
            )
            conn.commit()
        if not run:
            return {"found": False, "run": None, "queues": []}
        run_item = {field: run[field] for field in run_fields}
        run_item["account_ids"] = _schedule_account_ids(
            _json_array(
                run_item.pop("account_ids_json"),
                "account_ids",
            ),
            allow_empty=True,
        )
        run_item["error_message"] = redact_text(
            run_item["error_message"],
            500,
        )
        return {
            "found": True,
            "run": run_item,
            "queues": [
                {
                    field: (
                        bool(row[field])
                        if field == "unknown_outcome"
                        else row[field]
                    )
                    for field in queue_fields
                }
                for row in queues
            ],
        }

    def record_schedule_failure(
        self,
        source_type,
        run_date,
        publish_time,
        config_version,
        account_ids,
        error_code,
        error_message,
        *,
        drama_pool_item_id=None,
        content_id="",
    ):
        source_type = _schedule_source_type(source_type)
        run_date = _date_value(run_date, "run_date")
        publish_time = _schedule_publish_time(publish_time)
        config_version = _positive_int(config_version, "config_version")
        account_ids = _schedule_account_ids(account_ids)
        binding_requested = (
            drama_pool_item_id is not None
            or bool(str(content_id or "").strip())
        )
        bound_pool_item_id = None
        bound_content_id = ""
        if binding_requested:
            if (
                source_type != "drama"
                or drama_pool_item_id is None
                or not str(content_id or "").strip()
            ):
                raise XPostError(
                    "invalid_request",
                    "短剧失败记录必须同时携带短剧池ID和短剧ID",
                    400,
                )
            bound_pool_item_id = _positive_int(
                drama_pool_item_id,
                "drama_pool_item_id",
            )
            bound_content_id = _drama_content_id(content_id)
        try:
            code = _clean_token(
                error_code or "x_post_schedule_preflight_failed",
                "error code",
                64,
            )
        except ValueError:
            raise XPostError(
                "invalid_request",
                "error_code无效",
                400,
            ) from None
        message = redact_text(
            error_message or "X定时发布预检失败",
            500,
        )
        timestamp = utc_now()
        slot_key = "xpost:schedule:v1:%s:%s:%s" % (
            source_type,
            run_date,
            publish_time.replace(":", ""),
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if bound_pool_item_id is not None:
                bound_pool = conn.execute(
                    "SELECT id,content_id,assigned_account_id "
                    "FROM x_post_drama_pool WHERE id=?",
                    (bound_pool_item_id,),
                ).fetchone()
                if (
                    not bound_pool
                    or str(bound_pool["content_id"]) != bound_content_id
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_item_unavailable",
                        "短剧失败记录与短剧池记录不一致",
                        409,
                    )

                if (
                    int(bound_pool["assigned_account_id"] or 0) > 0
                    and int(bound_pool["assigned_account_id"])
                    not in account_ids
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_failure_scope_mismatch",
                        "Drama pool owner is outside the failed schedule scope",
                        409,
                    )

            def mark_drama_pool_failure():
                if bound_pool_item_id is None:
                    return
                has_history = bool(
                    conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE drama_pool_item_id=? OR "
                        "(source_type='drama' AND content_id=?) LIMIT 1",
                        (bound_pool_item_id, bound_content_id),
                    ).fetchone()
                )
                if (
                    int(bound_pool["assigned_account_id"] or 0) <= 0
                    and not has_history
                ):
                    return
                conn.execute(
                    "UPDATE x_post_drama_pool SET status='needs_review',"
                    "last_checked_at=?,last_error_code=?,"
                    "last_error_message=?,updated_at=? "
                    "WHERE id=? AND status<>'completed'",
                    (
                        timestamp,
                        code,
                        message,
                        timestamp,
                        bound_pool_item_id,
                    ),
                )

            existing = conn.execute(
                "SELECT * FROM x_post_schedule_run "
                "WHERE source_type=? AND run_date=? AND publish_time=?",
                (source_type, run_date, publish_time),
            ).fetchone()
            if existing:
                queue_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM x_post_queue "
                        "WHERE schedule_run_id=?",
                        (existing["id"],),
                    ).fetchone()[0]
                )
                if queue_count:
                    if bound_pool_item_id is not None and not conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE schedule_run_id=? AND source_type='drama' "
                        "AND drama_pool_item_id=? AND content_id=? LIMIT 1",
                        (
                            existing["id"],
                            bound_pool_item_id,
                            bound_content_id,
                        ),
                    ).fetchone():
                        conn.rollback()
                        raise XPostError(
                            "x_post_schedule_failure_scope_mismatch",
                            "Drama pool item does not belong to this "
                            "schedule run",
                            409,
                        )
                    if bound_pool_item_id is not None and (
                        int(existing["config_version"]) != config_version
                        or _schedule_account_ids(
                            _json_array(
                                existing["account_ids_json"],
                                "account_ids",
                            )
                        )
                        != account_ids
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_schedule_run_exists",
                            "该时间点已存在不同范围的发布批次",
                            409,
                        )
                    mark_drama_pool_failure()
                    conn.commit()
                    item = self.get_schedule_run(existing["id"])
                    item["recorded"] = False
                    return item
                if (
                    int(existing["config_version"]) != config_version
                    or _schedule_account_ids(
                        _json_array(
                            existing["account_ids_json"],
                            "account_ids",
                        )
                    )
                    != account_ids
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_run_exists",
                        "该时间点已存在不同范围的发布批次",
                        409,
                    )
                conn.execute(
                    "UPDATE x_post_schedule_run SET status='failed_preflight',"
                    "queued_count=0,published_count=0,failed_count=0,"
                    "unknown_count=0,error_code=?,error_message=?,"
                    "finished_at=?,updated_at=? WHERE id=?",
                    (
                        code,
                        message,
                        timestamp,
                        timestamp,
                        existing["id"],
                    ),
                )
                run_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO x_post_schedule_run("
                    "slot_key,source_type,run_date,publish_time,timezone,"
                    "config_version,account_ids_json,status,expected_count,"
                    "queued_count,error_code,error_message,started_at,"
                    "finished_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,'failed_preflight',?,0,?,?,?,?,?,?)",
                    (
                        slot_key,
                        source_type,
                        run_date,
                        publish_time,
                        SCHEDULE_TIMEZONE,
                        config_version,
                        json.dumps(account_ids, separators=(",", ":")),
                        len(account_ids),
                        code,
                        message,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                run_id = int(cursor.lastrowid)
            mark_drama_pool_failure()
            conn.commit()
        item = self.get_schedule_run(run_id)
        item["recorded"] = True
        return item

    def recover_failed_preflight_schedule_run(
        self,
        run_id,
        expected_error_code,
        *,
        reason,
        actor,
        verified_repair_job_key="",
        deployed_commit="",
        compensation_publish_time="",
        validate_only=False,
        now=None,
    ):
        """Re-arm one exact same-day zero-write preflight failure.

        The original terminal evidence is copied into an append-only audit row
        before the frozen schedule run returns to ``claimed``.  The method does
        not select candidates, create queues, or call X.
        """
        run_id = _positive_int(run_id, "run_id")
        if not isinstance(validate_only, bool):
            raise XPostError(
                "invalid_request",
                "validate_only must be a boolean",
                400,
            )
        try:
            expected_error_code = _clean_token(
                expected_error_code,
                "expected error code",
                64,
            )
            reason = _clean_token(reason, "recovery reason", 128)
            actor = _clean_token(actor, "recovery actor", 128)
        except ValueError:
            raise XPostError(
                "invalid_request",
                "Failed-preflight recovery arguments are invalid",
                400,
            ) from None
        initial_recovery = reason == FAILED_PREFLIGHT_RECOVERY_REASON
        corrective_recovery = (
            reason == FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON
        )
        verified_repair_recovery = (
            reason == FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON
        )
        codefix_compensation = (
            reason == FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON
        )
        drama_capability_recovery = (
            reason == FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON
        )
        token_refresh_recovery = (
            reason == FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON
        )
        transient_media_recovery = (
            reason == FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON
        )
        verified_repair_job_key = str(
            verified_repair_job_key or ""
        ).strip().lower()
        deployed_commit = str(deployed_commit or "").strip().lower()
        try:
            normalized_compensation_time = (
                _schedule_publish_time(compensation_publish_time)
                if codefix_compensation
                else ""
            )
        except XPostError:
            normalized_compensation_time = ""
        if not (
            (
                initial_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_RECOVERABLE_ERROR_CODES
            )
            or (
                corrective_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_CORRECTIVE_ERROR_MESSAGES
            )
            or (
                verified_repair_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_VERIFIED_REPAIR_ERROR_MESSAGES
                and re.fullmatch(
                    r"[a-f0-9]{64}",
                    verified_repair_job_key,
                )
            )
            or (
                codefix_compensation
                and expected_error_code
                in FAILED_PREFLIGHT_VERIFIED_REPAIR_ERROR_MESSAGES
                and re.fullmatch(r"[a-f0-9]{64}", verified_repair_job_key)
                and re.fullmatch(r"[a-f0-9]{40}", deployed_commit)
                and bool(normalized_compensation_time)
            )
            or (
                drama_capability_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_DRAMA_CAPABILITY_ERROR_MESSAGES
                and re.fullmatch(r"[a-f0-9]{40}", deployed_commit)
            )
            or (
                token_refresh_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_TOKEN_REFRESH_ERROR_MESSAGES
                and re.fullmatch(r"[a-f0-9]{40}", deployed_commit)
            )
            or (
                transient_media_recovery
                and expected_error_code
                in FAILED_PREFLIGHT_TRANSIENT_MEDIA_ERROR_MESSAGES
                and re.fullmatch(r"[a-f0-9]{40}", deployed_commit)
            )
        ):
            raise XPostError(
                "x_post_failed_preflight_recovery_not_allowed",
                "This failed preflight is not eligible for guarded recovery",
                409,
            )

        current = now or datetime.now(BEIJING_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TZ)
        else:
            current = current.astimezone(BEIJING_TZ)
        current_date = current.date().isoformat()
        timestamp = utc_now()

        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM x_post_schedule_run WHERE id=?",
                (run_id,),
            ).fetchone()
            if not run:
                conn.rollback()
                raise XPostError(
                    "x_post_schedule_run_not_found",
                    "X schedule run was not found",
                    404,
                )

            account_ids = _schedule_account_ids(
                _json_array(run["account_ids_json"], "account_ids")
            )
            queue_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue "
                    "WHERE schedule_run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            log_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_publish_log l "
                    "JOIN x_post_queue q ON q.id=l.queue_id "
                    "WHERE q.schedule_run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            prior_audit = conn.execute(
                "SELECT id FROM x_post_schedule_recovery_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            corrective_audit = conn.execute(
                "SELECT id FROM x_post_schedule_corrective_retry_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            verified_repair_audit = conn.execute(
                "SELECT id,verified_repair_job_key "
                "FROM x_post_schedule_verified_repair_retry_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            codefix_compensation_audit = conn.execute(
                "SELECT id FROM x_post_schedule_codefix_compensation_audit "
                "WHERE original_schedule_run_id=?",
                (run_id,),
            ).fetchone()
            drama_capability_audit = conn.execute(
                "SELECT id FROM x_post_schedule_drama_capability_recovery_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            token_refresh_audit = conn.execute(
                "SELECT id FROM x_post_schedule_token_refresh_recovery_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            transient_media_audit = conn.execute(
                "SELECT id FROM x_post_schedule_transient_media_recovery_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            previous_error_message = str(run["error_message"] or "")
            corrective_message_matches = bool(
                corrective_recovery
                and any(
                    fragment in previous_error_message
                    for fragment in FAILED_PREFLIGHT_CORRECTIVE_ERROR_MESSAGES.get(
                        expected_error_code,
                        (),
                    )
                )
            )
            verified_repair_message_matches = bool(
                (verified_repair_recovery or codefix_compensation)
                and any(
                    fragment in previous_error_message
                    for fragment in FAILED_PREFLIGHT_VERIFIED_REPAIR_ERROR_MESSAGES.get(
                        expected_error_code,
                        (),
                    )
                )
            )
            drama_capability_message_matches = bool(
                drama_capability_recovery
                and any(
                    fragment in previous_error_message
                    for fragment in FAILED_PREFLIGHT_DRAMA_CAPABILITY_ERROR_MESSAGES.get(
                        expected_error_code,
                        (),
                    )
                )
            )
            token_refresh_message_matches = bool(
                token_refresh_recovery
                and any(
                    fragment in previous_error_message
                    for fragment in FAILED_PREFLIGHT_TOKEN_REFRESH_ERROR_MESSAGES.get(
                        expected_error_code,
                        (),
                    )
                )
            )
            transient_media_message_matches = bool(
                transient_media_recovery
                and any(
                    fragment in previous_error_message
                    for fragment in FAILED_PREFLIGHT_TRANSIENT_MEDIA_ERROR_MESSAGES.get(
                        expected_error_code,
                        (),
                    )
                )
            )

            scheduled_at = datetime.strptime(
                "%s %s" % (run["run_date"], run["publish_time"]),
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=BEIJING_TZ)
            compensation_scheduled_at = None
            target_run = None
            if codefix_compensation:
                compensation_scheduled_at = datetime.strptime(
                    "%s %s"
                    % (run["run_date"], normalized_compensation_time),
                    "%Y-%m-%d %H:%M",
                ).replace(tzinfo=BEIJING_TZ)
                target_run = conn.execute(
                    "SELECT id FROM x_post_schedule_run "
                    "WHERE source_type=? AND run_date=? AND publish_time=?",
                    (
                        run["source_type"],
                        run["run_date"],
                        normalized_compensation_time,
                    ),
                ).fetchone()
            conflict = bool(
                str(run["run_date"]) != current_date
                or scheduled_at > current
                or str(run["status"]) != "failed_preflight"
                or str(run["error_code"]) != expected_error_code
                or int(run["expected_count"] or 0) != len(account_ids)
                or int(run["queued_count"] or 0) != 0
                or int(run["published_count"] or 0) != 0
                or int(run["failed_count"] or 0) != 0
                or int(run["unknown_count"] or 0) != 0
                or queue_count != 0
                or log_count != 0
                or (
                    (verified_repair_recovery or codefix_compensation)
                    and str(run["source_type"]) != "drama"
                )
                or (
                    (
                        drama_capability_recovery
                        or token_refresh_recovery
                        or transient_media_recovery
                    )
                    and str(run["source_type"]) != "drama"
                )
                or (
                    initial_recovery
                    and (
                        prior_audit is not None
                        or corrective_audit is not None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is not None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                    )
                )
                or (
                    corrective_recovery
                    and (
                        prior_audit is None
                        or corrective_audit is not None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is not None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                        or not corrective_message_matches
                    )
                )
                or (
                    verified_repair_recovery
                    and (
                        prior_audit is None
                        or corrective_audit is None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is not None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                        or not verified_repair_message_matches
                    )
                )
                or (
                    codefix_compensation
                    and (
                        prior_audit is None
                        or corrective_audit is None
                        or verified_repair_audit is None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is not None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                        or not verified_repair_message_matches
                        or str(
                            verified_repair_audit[
                                "verified_repair_job_key"
                            ]
                        )
                        != verified_repair_job_key
                        or normalized_compensation_time
                        == str(run["publish_time"])
                        or compensation_scheduled_at > current
                        or target_run is not None
                    )
                )
                or (
                    drama_capability_recovery
                    and (
                        prior_audit is not None
                        or corrective_audit is not None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is not None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                        or not drama_capability_message_matches
                    )
                )
                or (
                    token_refresh_recovery
                    and (
                        prior_audit is not None
                        or corrective_audit is not None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is None
                        or token_refresh_audit is not None
                        or transient_media_audit is not None
                        or not token_refresh_message_matches
                    )
                )
                or (
                    transient_media_recovery
                    and (
                        prior_audit is not None
                        or corrective_audit is not None
                        or verified_repair_audit is not None
                        or codefix_compensation_audit is not None
                        or drama_capability_audit is None
                        or token_refresh_audit is None
                        or transient_media_audit is not None
                        or not transient_media_message_matches
                    )
                )
            )

            mode = _schedule_mode(run["schedule_mode"])
            if mode == "random":
                plan_row = conn.execute(
                    "SELECT * FROM x_post_schedule_random_plan "
                    "WHERE source_type=? AND run_date=?",
                    (run["source_type"], run["run_date"]),
                ).fetchone()
                plan = (
                    self._random_schedule_plan_item(plan_row)
                    if plan_row
                    else None
                )
                conflict = conflict or not bool(
                    plan
                    and int(plan["config_version"])
                    == int(run["config_version"])
                    and plan["account_ids"] == account_ids
                    and str(run["publish_time"]) in plan["publish_times"]
                    and str(plan["body_template"])
                    == str(run["body_template"])
                )
            else:
                config_row = conn.execute(
                    "SELECT * FROM x_post_schedule_config "
                    "WHERE source_type=?",
                    (run["source_type"],),
                ).fetchone()
                config = (
                    self._schedule_config_item(config_row, now=current)
                    if config_row
                    else None
                )
                conflict = conflict or not bool(
                    config
                    and config["enabled"]
                    and config["schedule_mode"] == "fixed"
                    and int(config["version"])
                    == int(run["config_version"])
                    and config["account_ids"] == account_ids
                    and str(run["publish_time"])
                    in config["publish_times"]
                    and str(config["body_template"])
                    == str(run["body_template"])
                )

            placeholders = ",".join("?" for _item in account_ids)
            accounts = conn.execute(
                "SELECT id,status,publish_approved,token_store_key "
                "FROM x_authorized_account WHERE id IN (%s)" % placeholders,
                tuple(account_ids),
            ).fetchall()
            ready_ids = {
                int(row["id"])
                for row in accounts
                if str(row["status"]) == "active"
                and int(row["publish_approved"] or 0) == 1
                and bool(str(row["token_store_key"] or "").strip())
            }
            conflict = conflict or ready_ids != set(account_ids)
            unresolved = conn.execute(
                "SELECT 1 FROM x_post_publish_log l "
                "JOIN x_post_queue q ON q.id=l.queue_id "
                "WHERE q.account_id IN (%s) "
                "AND (COALESCE(l.unknown_outcome,0)=1 "
                "OR l.status='post_creating') LIMIT 1" % placeholders,
                tuple(account_ids),
            ).fetchone()
            conflict = conflict or unresolved is not None

            if conflict:
                conn.rollback()
                raise XPostError(
                    "x_post_failed_preflight_recovery_conflict",
                    "Run, account, plan, queue, or audit state is not an exact zero-write failure",
                    409,
                )

            result = {
                "run_id": run_id,
                "source_type": str(run["source_type"]),
                "run_date": str(run["run_date"]),
                "publish_time": str(run["publish_time"]),
                "expected_count": int(run["expected_count"]),
                "expected_error_code": expected_error_code,
                "reason": reason,
                "recovery_mode": (
                    "initial"
                    if initial_recovery
                    else (
                        "corrective"
                        if corrective_recovery
                        else (
                            "verified_repair"
                            if verified_repair_recovery
                            else (
                                "codefix_compensation"
                                if codefix_compensation
                                else (
                                    "drama_capability_fallback"
                                    if drama_capability_recovery
                                    else (
                                        "preflight_token_refresh"
                                        if token_refresh_recovery
                                        else "transient_media_retry"
                                    )
                                )
                            )
                        )
                    )
                ),
                "initial_recovery_audit_id": (
                    int(prior_audit["id"])
                    if prior_audit is not None
                    else None
                ),
                "corrective_retry_audit_id": (
                    int(corrective_audit["id"])
                    if corrective_audit is not None
                    else None
                ),
                "verified_repair_job_key": (
                    verified_repair_job_key
                    if (
                        verified_repair_recovery
                        or codefix_compensation
                    )
                    else ""
                ),
                "deployed_commit": (
                    deployed_commit
                    if (
                        codefix_compensation
                        or drama_capability_recovery
                        or token_refresh_recovery
                        or transient_media_recovery
                    )
                    else ""
                ),
                "compensation_publish_time": (
                    normalized_compensation_time
                    if codefix_compensation
                    else ""
                ),
                "actor": actor,
                "validated_queue_count": queue_count,
                "validated_log_count": log_count,
                "validate_only": validate_only,
                "validated_count": 1,
                "updated_count": 0,
            }
            if validate_only:
                conn.rollback()
                return result

            if codefix_compensation:
                compensation_slot_key = (
                    "xpost:schedule:v1:%s:%s:%s"
                    % (
                        str(run["source_type"]),
                        str(run["run_date"]),
                        normalized_compensation_time.replace(":", ""),
                    )
                )
                cursor = conn.execute(
                    "INSERT INTO x_post_schedule_run("
                    "slot_key,source_type,run_date,publish_time,timezone,"
                    "config_version,account_ids_json,schedule_mode,"
                    "body_template,status,expected_count,queued_count,"
                    "published_count,failed_count,unknown_count,"
                    "created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,0,0,0,0,?,?)",
                    (
                        compensation_slot_key,
                        str(run["source_type"]),
                        str(run["run_date"]),
                        normalized_compensation_time,
                        SCHEDULE_TIMEZONE,
                        int(run["config_version"]),
                        str(run["account_ids_json"]),
                        str(run["schedule_mode"]),
                        str(run["body_template"]),
                        int(run["expected_count"]),
                        timestamp,
                        timestamp,
                    ),
                )
                compensation_run_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO x_post_schedule_codefix_compensation_audit("
                    "original_schedule_run_id,compensation_schedule_run_id,"
                    "verified_repair_retry_audit_id,recovery_reason,actor,"
                    "deployed_commit,verified_repair_job_key,"
                    "previous_status,previous_error_code,"
                    "previous_error_message,validated_queue_count,"
                    "validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        compensation_run_id,
                        int(verified_repair_audit["id"]),
                        reason,
                        actor,
                        deployed_commit,
                        verified_repair_job_key,
                        str(run["status"]),
                        str(run["error_code"]),
                        redact_text(run["error_message"], 500),
                        queue_count,
                        log_count,
                        timestamp,
                    ),
                )
                conn.commit()
                result["updated_count"] = 1
                result["compensation_run_id"] = compensation_run_id
                return result

            audit_values = (
                run_id,
                reason,
                actor,
                str(run["status"]),
                str(run["error_code"]),
                redact_text(run["error_message"], 500),
                str(run["started_at"]),
                str(run["finished_at"]),
                queue_count,
                log_count,
                timestamp,
            )
            if transient_media_recovery:
                conn.execute(
                    "INSERT INTO x_post_schedule_transient_media_recovery_audit("
                    "schedule_run_id,token_refresh_recovery_audit_id,"
                    "recovery_reason,actor,deployed_commit,previous_status,"
                    "previous_error_code,previous_error_message,"
                    "previous_started_at,previous_finished_at,"
                    "validated_queue_count,validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(token_refresh_audit["id"]),
                        reason,
                        actor,
                        deployed_commit,
                        str(run["status"]),
                        str(run["error_code"]),
                        redact_text(run["error_message"], 500),
                        str(run["started_at"]),
                        str(run["finished_at"]),
                        queue_count,
                        log_count,
                        timestamp,
                    ),
                )
            elif token_refresh_recovery:
                conn.execute(
                    "INSERT INTO x_post_schedule_token_refresh_recovery_audit("
                    "schedule_run_id,drama_capability_recovery_audit_id,"
                    "recovery_reason,actor,deployed_commit,previous_status,"
                    "previous_error_code,previous_error_message,"
                    "previous_started_at,previous_finished_at,"
                    "validated_queue_count,validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(drama_capability_audit["id"]),
                        reason,
                        actor,
                        deployed_commit,
                        str(run["status"]),
                        str(run["error_code"]),
                        redact_text(run["error_message"], 500),
                        str(run["started_at"]),
                        str(run["finished_at"]),
                        queue_count,
                        log_count,
                        timestamp,
                    ),
                )
            elif drama_capability_recovery:
                conn.execute(
                    "INSERT INTO x_post_schedule_drama_capability_recovery_audit("
                    "schedule_run_id,recovery_reason,actor,deployed_commit,"
                    "previous_status,previous_error_code,"
                    "previous_error_message,previous_started_at,"
                    "previous_finished_at,validated_queue_count,"
                    "validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        reason,
                        actor,
                        deployed_commit,
                        str(run["status"]),
                        str(run["error_code"]),
                        redact_text(run["error_message"], 500),
                        str(run["started_at"]),
                        str(run["finished_at"]),
                        queue_count,
                        log_count,
                        timestamp,
                    ),
                )
            elif initial_recovery:
                conn.execute(
                    "INSERT INTO x_post_schedule_recovery_audit("
                    "schedule_run_id,recovery_reason,actor,previous_status,"
                    "previous_error_code,previous_error_message,"
                    "previous_started_at,previous_finished_at,"
                    "validated_queue_count,validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    audit_values,
                )
            elif corrective_recovery:
                conn.execute(
                    "INSERT INTO x_post_schedule_corrective_retry_audit("
                    "schedule_run_id,initial_recovery_audit_id,"
                    "recovery_reason,actor,previous_status,"
                    "previous_error_code,previous_error_message,"
                    "previous_started_at,previous_finished_at,"
                    "validated_queue_count,validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(prior_audit["id"]),
                    )
                    + audit_values[1:],
                )
            else:
                conn.execute(
                    "INSERT INTO x_post_schedule_verified_repair_retry_audit("
                    "schedule_run_id,initial_recovery_audit_id,"
                    "corrective_retry_audit_id,recovery_reason,actor,"
                    "verified_repair_job_key,previous_status,"
                    "previous_error_code,previous_error_message,"
                    "previous_started_at,previous_finished_at,"
                    "validated_queue_count,validated_log_count,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(prior_audit["id"]),
                        int(corrective_audit["id"]),
                        reason,
                        actor,
                        verified_repair_job_key,
                    )
                    + audit_values[3:],
                )
            cursor = conn.execute(
                "UPDATE x_post_schedule_run SET status='claimed',"
                "queued_count=0,published_count=0,failed_count=0,"
                "unknown_count=0,error_code='',error_message='',"
                "started_at='',finished_at='',updated_at=? "
                "WHERE id=? AND status='failed_preflight' "
                "AND error_code=? AND queued_count=0 "
                "AND published_count=0 AND failed_count=0 "
                "AND unknown_count=0",
                (timestamp, run_id, expected_error_code),
            )
            if int(cursor.rowcount or 0) != 1:
                conn.rollback()
                raise XPostError(
                    "x_post_failed_preflight_recovery_conflict",
                    "Failed-preflight recovery state changed during the transaction",
                    409,
                )
            conn.commit()
            result["updated_count"] = 1
            return result

    def create_schedule_plan(
        self,
        source_type,
        run_date,
        publish_time,
        config_version,
        candidates,
        premium_account_ids=None,
    ):
        source_type = _schedule_source_type(source_type)
        run_date = _date_value(run_date, "run_date")
        publish_time = _schedule_publish_time(publish_time)
        config_version = _positive_int(config_version, "config_version")
        if not isinstance(candidates, list):
            raise XPostError("invalid_request", "candidates必须是数组", 400)
        frozen = self.query_schedule_plan(
            source_type,
            run_date,
            publish_time,
        )
        if frozen["found"]:
            frozen_run = frozen["run"]
            if int(frozen_run["config_version"]) != config_version:
                raise XPostError(
                    "x_post_schedule_run_exists",
                    "该时间点已存在其他版本的冻结计划",
                    409,
                )
            account_ids = list(frozen_run["account_ids"])
            schedule_mode = _schedule_mode(
                frozen_run.get("schedule_mode", "fixed")
            )
            body_template = _normalize_post_template(
                frozen_run.get("body_template"),
                source_type,
            )
        else:
            config = self.get_schedule_config(source_type)
            schedule_mode = config["schedule_mode"]
            slot = config
            if schedule_mode == "random":
                with contextlib.closing(_connect(self.db_path)) as conn:
                    plan_row = conn.execute(
                        "SELECT * FROM x_post_schedule_random_plan "
                        "WHERE source_type=? AND run_date=?",
                        (source_type, run_date),
                    ).fetchone()
                slot = self._random_schedule_plan_item(plan_row)
            if not config["enabled"] or not slot:
                raise XPostError(
                    "x_post_schedule_config_changed",
                    "自动发布设置已变更，本时间点不再创建新队列",
                    409,
                )
            account_ids = list(slot["account_ids"])
            body_template = slot["body_template"]
            valid_version = (
                int(slot["config_version"])
                if schedule_mode == "random"
                else int(config["version"])
            )
            if (
                valid_version != config_version
                or publish_time not in slot["publish_times"]
            ):
                raise XPostError(
                    "x_post_schedule_config_changed",
                    "自动发布设置已变更，本时间点不再创建新队列",
                    409,
                )
        if len(candidates) != len(account_ids):
            raise XPostError(
                "x_post_schedule_candidate_shortage",
                "定时发布计划候选数量与账号数量不一致",
                409,
            )
        prepared = []
        seen_accounts = set()
        publication_keys = set()
        for index, candidate in enumerate(candidates, 1):
            payload = dict(candidate) if isinstance(candidate, dict) else candidate
            if not isinstance(payload, dict):
                raise XPostError("invalid_request", "candidate必须是对象", 400)
            payload["source_type"] = source_type
            payload["body_template"] = body_template
            values = self._queue_payload(
                payload,
                run_date=run_date,
                candidate_rank=index,
                require_compliance=True,
            )
            if values["account_id"] != account_ids[index - 1]:
                raise XPostError(
                    "x_post_schedule_account_mismatch",
                    "候选账号顺序与自动发布设置不一致",
                    409,
                )
            if values["account_id"] in seen_accounts:
                raise XPostError(
                    "invalid_request",
                    "定时发布计划账号不能重复",
                    400,
                )
            publication_key = (
                values["material_key"]
                if source_type == "material"
                else values["episode_key"]
            )
            if publication_key in publication_keys:
                raise XPostError(
                    "invalid_request",
                    "定时发布计划内容不能重复",
                    400,
                )
            if source_type == "material" and values["pool_item_id"] is None:
                raise XPostError(
                    "x_post_pool_required",
                    "素材定时发布候选必须来自素材池",
                    409,
                )
            if source_type == "drama" and values["drama_pool_item_id"] is None:
                raise XPostError(
                    "x_post_drama_pool_required",
                    "短剧定时发布候选必须来自短剧池",
                    409,
                )
            values["idempotency_key"] = (
                "xpost:schedule:v1:%s:%s:%s:%s"
                % (
                    source_type,
                    run_date,
                    publish_time.replace(":", ""),
                    values["account_id"],
                )
            )
            seen_accounts.add(values["account_id"])
            publication_keys.add(publication_key)
            prepared.append(values)

        timestamp = utc_now()
        slot_key = "xpost:schedule:v1:%s:%s:%s" % (
            source_type,
            run_date,
            publish_time.replace(":", ""),
        )
        columns = ("idempotency_key",) + QUEUE_LEDGER_FIELDS + QUEUE_FIELDS
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_schedule_run "
                "WHERE source_type=? AND run_date=? AND publish_time=?",
                (source_type, run_date, publish_time),
            ).fetchone()
            if existing:
                existing_account_ids = _schedule_account_ids(
                    _json_array(
                        existing["account_ids_json"],
                        "account_ids",
                    )
                )
                if (
                    int(existing["config_version"]) != config_version
                    or existing_account_ids != account_ids
                    or int(existing["expected_count"]) != len(account_ids)
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_run_exists",
                        "该时间点已存在不同范围的冻结计划",
                        409,
                    )
                existing_queues = conn.execute(
                    "SELECT * FROM x_post_queue WHERE schedule_run_id=? "
                    "ORDER BY candidate_rank,id",
                    (existing["id"],),
                ).fetchall()
                if existing_queues:
                    expected = [
                        (
                            values["account_id"],
                            values["material_key"],
                            values["episode_key"],
                        )
                        for values in prepared
                    ]
                    actual = [
                        (
                            int(row["account_id"]),
                            str(row["material_key"]),
                            str(row["episode_key"]),
                        )
                        for row in existing_queues
                    ]
                    if actual != expected:
                        conn.rollback()
                        raise XPostError(
                            "x_post_schedule_run_exists",
                            "该时间点已存在不同的发布计划",
                            409,
                        )
                    conn.commit()
                    item = self.get_schedule_run(existing["id"])
                    item["queues"] = [
                        _row_dict(row) for row in existing_queues
                    ]
                    item["created"] = False
                    return item
                if existing["status"] == "failed_preflight":
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_run_exists",
                        "已失败批次不能补建发布计划",
                        409,
                    )
                if existing["status"] != "claimed":
                    conn.rollback()
                    raise XPostError(
                        "x_post_storage_conflict",
                        "已有时间点批次缺少冻结队列",
                        500,
                    )
                schedule_run_id = int(existing["id"])
            else:
                current_config = conn.execute(
                    "SELECT * FROM x_post_schedule_config "
                    "WHERE source_type=?",
                    (source_type,),
                ).fetchone()
                valid_current_slot = False
                if current_config and bool(current_config["enabled"]):
                    current_mode = _schedule_mode(
                        current_config["schedule_mode"]
                    )
                    if current_mode == "fixed":
                        valid_current_slot = bool(
                            schedule_mode == "fixed"
                            and int(current_config["version"])
                            == config_version
                            and _schedule_account_ids(
                                _json_array(
                                    current_config["account_ids_json"],
                                    "account_ids",
                                )
                            )
                            == account_ids
                            and publish_time
                            in _schedule_publish_times(
                                _json_array(
                                    current_config["publish_times_json"],
                                    "publish_times",
                                )
                            )
                        )
                    else:
                        plan_row = conn.execute(
                            "SELECT * FROM x_post_schedule_random_plan "
                            "WHERE source_type=? AND run_date=?",
                            (source_type, run_date),
                        ).fetchone()
                        plan = self._random_schedule_plan_item(plan_row)
                        valid_current_slot = bool(
                            schedule_mode == "random"
                            and plan
                            and plan["config_version"] == config_version
                            and plan["account_ids"] == account_ids
                            and publish_time in plan["publish_times"]
                        )
                if not valid_current_slot:
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_config_changed",
                        "自动发布设置已变更，本时间点不再创建新队列",
                        409,
                    )
                schedule_run_id = None

            placeholders_accounts = ",".join("?" for _item in account_ids)
            unresolved = conn.execute(
                "SELECT 1 FROM x_post_publish_log l "
                "JOIN x_post_queue q ON q.id=l.queue_id "
                "WHERE q.account_id IN (%s) "
                "AND (COALESCE(l.unknown_outcome,0)=1 "
                "OR l.status='post_creating') LIMIT 1"
                % placeholders_accounts,
                tuple(account_ids),
            ).fetchone()
            if unresolved:
                conn.rollback()
                raise XPostError(
                    "x_post_unknown_outcome",
                    "所选账号存在待核对发布结果，已暂停后续自动发布",
                    409,
                    True,
                )

            if source_type == "material":
                expected_pools = conn.execute(
                    "SELECT p.* FROM x_post_material_pool p "
                    "WHERE p.status='unpublished' "
                    "AND (p.last_error_code='' OR p.last_error_code IN %s) "
                    "AND NOT EXISTS("
                    "SELECT 1 FROM x_post_queue q "
                    "WHERE q.pool_item_id=p.id "
                    "OR q.material_key=p.material_key"
                    ") "
                    "ORDER BY p.created_at DESC,p.id DESC LIMIT ?"
                    % _NONBLOCKING_MATERIAL_VALIDATION_SQL,
                    (len(prepared),),
                ).fetchall()
                expected_pool_ids = [
                    int(pool["id"]) for pool in expected_pools
                ]
                actual_pool_ids = [
                    int(values["pool_item_id"]) for values in prepared
                ]
                if (
                    len(actual_pool_ids) != len(expected_pool_ids)
                    or set(actual_pool_ids) != set(expected_pool_ids)
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_pool_fifo_conflict",
                        "素材计划必须使用当前素材池最新的可用记录",
                        409,
                    )
                for values in prepared:
                    pool = conn.execute(
                        "SELECT * FROM x_post_material_pool WHERE id=?",
                        (values["pool_item_id"],),
                    ).fetchone()
                    if (
                        not pool
                        or pool["status"] != "unpublished"
                        or str(pool["material_key"]) != values["material_key"]
                        or str(pool["created_at"]) != values["pool_created_at"]
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_unavailable",
                            "候选素材池记录已发布、已变更或不可用",
                            409,
                        )
                    if conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE pool_item_id=? OR material_key=?",
                        (
                            values["pool_item_id"],
                            values["material_key"],
                        ),
                    ).fetchone():
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_occupied",
                            "候选素材已被其他发布队列占用",
                            409,
                        )
            else:
                blocked = conn.execute(
                    "SELECT id,content_id FROM x_post_drama_pool "
                    "WHERE status='needs_review' "
                    "ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if blocked:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_pool_needs_review",
                        "短剧%s存在待人工确认的发布结果，已暂停后续短剧发布"
                        % blocked["content_id"],
                        409,
                        True,
                    )
                assignments = self._drama_assignment_candidates(
                    conn,
                    account_ids,
                    1000,
                    premium_account_ids=premium_account_ids,
                )
                if len(assignments) != len(prepared):
                    conn.rollback()
                    raise XPostError(
                        "x_post_schedule_drama_shortage",
                        "短剧池中没有足够的未绑定短剧供全部账号发布",
                        409,
                    )
                expected_by_pool = {
                    int(item["id"]): item for item in assignments
                }
                actual_pool_ids = [
                    int(values["drama_pool_item_id"])
                    for values in prepared
                ]
                assignment_conflict = bool(
                    len(actual_pool_ids) != len(expected_by_pool)
                    or len(set(actual_pool_ids)) != len(actual_pool_ids)
                    or set(actual_pool_ids) != set(expected_by_pool)
                )
                for values in prepared:
                    pool_id = int(values["drama_pool_item_id"])
                    expected = expected_by_pool.get(pool_id)
                    if not expected:
                        assignment_conflict = True
                        continue
                    owner_id = int(expected["assigned_account_id"] or 0)
                    if (
                        int(values["episode_number"])
                        != int(expected["next_sub_number"])
                        or (
                            owner_id > 0
                            and int(values["account_id"]) != owner_id
                        )
                    ):
                        assignment_conflict = True
                if assignment_conflict:
                    conn.rollback()
                    raise XPostError(
                        "x_post_drama_assignment_conflict",
                        "短剧候选与账号固定归属或新剧入池顺序不一致",
                        409,
                    )
                pool_by_id = expected_by_pool
                for values in prepared:
                    pool = pool_by_id.get(
                        int(values["drama_pool_item_id"])
                    )
                    if (
                        not pool
                        or str(pool["content_id"]) != values["content_id"]
                        or str(pool["created_at"])
                        != values["drama_pool_created_at"]
                        or int(pool["replay_generation"])
                        != int(values["drama_replay_generation"])
                        or int(values["episode_number"])
                        > int(pool["free_episode_count"])
                        or int(pool["assigned_account_id"] or 0)
                        not in (0, int(values["account_id"]))
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_drama_pool_item_unavailable",
                            "短剧池记录或免费剧集范围已变更",
                            409,
                        )
                    if conn.execute(
                        "SELECT 1 FROM x_post_queue WHERE episode_key=?",
                        (values["episode_key"],),
                    ).fetchone():
                        conn.rollback()
                        raise XPostError(
                            "x_post_drama_episode_already_used",
                            "短剧集数已被其他发布队列占用",
                            409,
                        )

            if schedule_run_id is None:
                cursor = conn.execute(
                    "INSERT INTO x_post_schedule_run("
                    "slot_key,source_type,run_date,publish_time,timezone,"
                    "config_version,account_ids_json,schedule_mode,"
                    "body_template,status,expected_count,"
                    "queued_count,started_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                    (
                        slot_key,
                        source_type,
                        run_date,
                        publish_time,
                        SCHEDULE_TIMEZONE,
                        config_version,
                        json.dumps(account_ids, separators=(",", ":")),
                        schedule_mode,
                        body_template,
                        len(prepared),
                        len(prepared),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                schedule_run_id = int(cursor.lastrowid)
            else:
                conn.execute(
                    "UPDATE x_post_schedule_run SET status='queued',"
                    "config_version=?,account_ids_json=?,expected_count=?,"
                    "queued_count=?,published_count=0,failed_count=0,"
                    "unknown_count=0,error_code='',error_message='',"
                    "started_at=?,finished_at='',updated_at=? "
                    "WHERE id=? AND status='claimed'",
                    (
                        config_version,
                        json.dumps(account_ids, separators=(",", ":")),
                        len(prepared),
                        len(prepared),
                        timestamp,
                        timestamp,
                        schedule_run_id,
                    ),
                )
            queue_ids = []
            placeholders = ",".join("?" for _field in columns)
            try:
                for values in prepared:
                    values["schedule_run_id"] = schedule_run_id
                    cursor = conn.execute(
                        "INSERT INTO x_post_queue("
                        + ",".join(columns)
                        + ",status,created_at,updated_at"
                        ") VALUES("
                        + placeholders
                        + ",'queued',?,?)",
                        tuple(values[field] for field in columns)
                        + (timestamp, timestamp),
                    )
                    queue_ids.append(int(cursor.lastrowid))
                    if source_type == "material":
                        conn.execute(
                            "UPDATE x_post_material_pool SET "
                            "last_checked_at=?,last_error_code='',"
                            "last_error_message='',updated_at=? "
                            "WHERE id=? AND status='unpublished'",
                            (
                                timestamp,
                                timestamp,
                                values["pool_item_id"],
                            ),
                        )
                    else:
                        pool = pool_by_id[
                            int(values["drama_pool_item_id"])
                        ]
                        if int(pool["assigned_account_id"] or 0) == 0:
                            assignment_cursor = conn.execute(
                                "UPDATE x_post_drama_pool SET status='active',"
                                "assigned_account_id=?,assigned_at=?,"
                                "assigned_source_queue_id=?,drama_name=?,"
                                "description=?,language=?,labels=?,name_tag=?,"
                                "priority_at='',priority_by_user_id='',"
                                "priority_by_name='',"
                                "last_checked_at=?,last_error_code='',"
                                "last_error_message='',updated_at=? "
                                "WHERE id=? AND assigned_account_id=0 "
                                "AND replay_generation=?",
                                (
                                    values["account_id"],
                                    timestamp,
                                    int(cursor.lastrowid),
                                    values["drama_name"],
                                    values["description"],
                                    values["material_language"],
                                    values["tag"],
                                    values["name_tag"],
                                    timestamp,
                                    timestamp,
                                    values["drama_pool_item_id"],
                                    values["drama_replay_generation"],
                                ),
                            )
                            if assignment_cursor.rowcount != 1:
                                raise XPostError(
                                    "x_post_drama_assignment_conflict",
                                    "短剧已被其他账号绑定",
                                    409,
                                )
                        else:
                            conn.execute(
                                "UPDATE x_post_drama_pool SET status='active',"
                                "drama_name=?,description=?,language=?,labels=?,"
                                "name_tag=?,last_checked_at=?,last_error_code='',"
                                "last_error_message='',updated_at=? WHERE id=?",
                                (
                                    values["drama_name"],
                                    values["description"],
                                    values["material_language"],
                                    values["tag"],
                                    values["name_tag"],
                                    timestamp,
                                    timestamp,
                                    values["drama_pool_item_id"],
                                ),
                            )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "X定时发布计划唯一约束冲突",
                    409,
                ) from exc
            conn.commit()
        item = self.get_schedule_run(schedule_run_id)
        item["queues"] = [
            self.get_queue(queue_id) for queue_id in queue_ids
        ]
        item["created"] = True
        return item

    def create_daily_plan(
        self, run_date, source_date, candidates, require_pool=False
    ):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        if (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - datetime.strptime(source_date, "%Y-%m-%d").date()
        ).days != 1:
            raise XPostError("invalid_request", "source_date必须是run_date前一天", 400)
        batch_size = len(candidates) if isinstance(candidates, list) else 0
        if batch_size < 1 or batch_size > MAX_DAILY_BATCH_SIZE:
            raise XPostError(
                "x_post_daily_candidate_shortage",
                "每日计划必须一次提交1到%s个候选" % MAX_DAILY_BATCH_SIZE,
                409,
            )
        prepared = []
        account_ids = set()
        material_keys = set()
        pool_item_ids = set()
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
            if require_pool and values["pool_item_id"] is None:
                raise XPostError(
                    "x_post_pool_required",
                    "正式每日计划候选必须来自素材池",
                    409,
                )
            if values["pool_item_id"] is not None:
                if values["pool_item_id"] in pool_item_ids:
                    raise XPostError("invalid_request", "每日计划素材池记录必须互不相同", 400)
                pool_item_ids.add(values["pool_item_id"])
            account_ids.add(values["account_id"])
            material_keys.add(values["material_key"])
            prepared.append(values)
        if pool_item_ids and len(pool_item_ids) != len(prepared):
            raise XPostError(
                "invalid_request",
                "每日计划候选必须全部来自素材池或全部不关联素材池",
                400,
            )

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
                if (
                    str(existing_run["source_date"]) != source_date
                    or int(existing_run["expected_count"]) != batch_size
                ):
                    conn.rollback()
                    raise XPostError("x_post_daily_run_exists", "该日期已存在不同的X发布批次", 409)
                existing_queues = conn.execute(
                    "SELECT * FROM x_post_queue WHERE run_id=? ORDER BY candidate_rank,id",
                    (existing_run["id"],),
                ).fetchall()
                if len(existing_queues) == batch_size:
                    expected = [
                        (
                            int(values["account_id"]),
                            values["material_key"],
                            values["pool_item_id"],
                        )
                        for values in prepared
                    ]
                    actual = [
                        (
                            int(row["account_id"]),
                            str(row["material_key"]),
                            int(row["pool_item_id"])
                            if row["pool_item_id"] is not None
                            else None,
                        )
                        for row in existing_queues
                    ]
                    if actual != expected:
                        conn.rollback()
                        raise XPostError(
                            "x_post_daily_run_exists",
                            "该日期已存在不同的X发布批次",
                            409,
                        )
                    conn.commit()
                    item = _row_dict(existing_run)
                    item["queues"] = [_row_dict(row) for row in existing_queues]
                    item["created"] = False
                    return item
                if existing_queues or existing_run["status"] != "failed_preflight":
                    conn.rollback()
                    raise XPostError("x_post_storage_conflict", "已有每日批次队列数量异常", 500)
                run_id = int(existing_run["id"])

            previous_pool_order = None
            for values in prepared:
                if values["pool_item_id"] is not None:
                    pool = conn.execute(
                        "SELECT * FROM x_post_material_pool WHERE id=?",
                        (values["pool_item_id"],),
                    ).fetchone()
                    if not pool:
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_not_found",
                            "候选素材池记录不存在",
                            404,
                        )
                    if (
                        pool["status"] != "unpublished"
                        or str(pool["material_key"]) != values["material_key"]
                        or str(pool["created_at"]) != values["pool_created_at"]
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_unavailable",
                            "候选素材池记录已发布、已变更或与素材不一致",
                            409,
                        )
                    if conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE pool_item_id=? OR material_key=?",
                        (values["pool_item_id"], values["material_key"]),
                    ).fetchone():
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_occupied",
                            "候选素材池记录已被发布队列占用",
                            409,
                        )
                    pool_order = (str(pool["created_at"]), int(pool["id"]))
                    if previous_pool_order is not None and pool_order >= previous_pool_order:
                        conn.rollback()
                        raise XPostError(
                            "invalid_request",
                            "每日计划必须按素材池创建时间倒序提交",
                            400,
                        )
                    previous_pool_order = pool_order
                elif conn.execute(
                    "SELECT 1 FROM x_post_material_pool WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_pool_item_occupied",
                        "候选素材已进入素材池，正式队列必须绑定对应素材池记录",
                        409,
                    )
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
                    ") VALUES(?,?,'queued',?,?,?,?,?)",
                    (
                        run_date,
                        source_date,
                        batch_size,
                        batch_size,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                run_id = int(cursor.lastrowid)
            else:
                conn.execute(
                    "UPDATE x_post_daily_run SET status='queued',expected_count=?,queued_count=?,published_count=0,"
                    "failed_count=0,unknown_count=0,error_code='',error_message='',started_at=?,"
                    "finished_at='',updated_at=? WHERE id=? AND status='failed_preflight'",
                    (batch_size, batch_size, timestamp, timestamp, run_id),
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
                    if values["pool_item_id"] is not None:
                        conn.execute(
                            "UPDATE x_post_material_pool SET last_checked_at=?,"
                            "last_error_code='',last_error_message='',updated_at=? "
                            "WHERE id=? AND status='unpublished'",
                            (timestamp, timestamp, values["pool_item_id"]),
                        )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError("x_post_storage_conflict", "每日X发布计划唯一约束冲突", 409) from exc
            conn.commit()
        item = self.get_run(run_id)
        item["queues"] = [self.get_queue(queue_id) for queue_id in queue_ids]
        item["created"] = True
        return item

    @staticmethod
    def _catchup_parent_context(
        conn,
        run_date,
        source_date,
        parent_run_id,
        configured_account_ids,
        exclude_catchup_run_id=None,
    ):
        parent = conn.execute(
            "SELECT * FROM x_post_daily_run WHERE id=?",
            (parent_run_id,),
        ).fetchone()
        if (
            not parent
            or str(parent["run_date"]) != run_date
            or str(parent["source_date"]) != source_date
            or str(parent["status"]) != "completed"
            or int(parent["expected_count"]) != 3
            or int(parent["queued_count"]) != 3
            or int(parent["published_count"]) != 3
            or int(parent["failed_count"]) != 0
            or int(parent["unknown_count"]) != 0
        ):
            raise XPostError(
                "x_post_catchup_parent_not_ready",
                "原每日批次必须为无失败、无未知结果的3/3已完成批次",
                409,
            )
        parent_queues = conn.execute(
            """
            SELECT q.account_id,q.status AS queue_status,
                   l.status AS log_status,COALESCE(l.unknown_outcome,0) AS unknown_outcome
              FROM x_post_queue q
              LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
             WHERE q.run_id=?
             ORDER BY q.candidate_rank,q.id
            """,
            (parent_run_id,),
        ).fetchall()
        parent_accounts = tuple(int(row["account_id"]) for row in parent_queues)
        if (
            len(parent_queues) != 3
            or len(set(parent_accounts)) != 3
            or any(account_id not in configured_account_ids for account_id in parent_accounts)
            or any(
                str(row["queue_status"]) != "published"
                or str(row["log_status"]) != "published"
                or int(row["unknown_outcome"] or 0) != 0
                for row in parent_queues
            )
        ):
            raise XPostError(
                "x_post_catchup_parent_not_ready",
                "原每日批次队列必须全部确认发布且属于当前配置",
                409,
            )
        if exclude_catchup_run_id is None:
            occupied_rows = conn.execute(
                "SELECT account_id FROM x_post_queue WHERE run_date=?",
                (run_date,),
            ).fetchall()
        else:
            occupied_rows = conn.execute(
                "SELECT account_id FROM x_post_queue WHERE run_date=? "
                "AND (catchup_run_id IS NULL OR catchup_run_id<>?)",
                (run_date, exclude_catchup_run_id),
            ).fetchall()
        occupied_accounts = {
            int(row["account_id"])
            for row in occupied_rows
        }
        missing_accounts = tuple(
            account_id
            for account_id in configured_account_ids
            if account_id not in occupied_accounts
        )
        return parent, parent_accounts, missing_accounts

    @staticmethod
    def _catchup_item(row):
        item = _row_dict(row)
        if item is None:
            return None
        item["account_ids"] = list(
            _stored_account_ids(
                item.pop("account_ids_json", ""),
                item.get("expected_count"),
            )
        )
        item["batch_kind"] = "catchup"
        return item

    @staticmethod
    def _catchup_key(run_date, parent_run_id, reason):
        return "xpost:catchup:%s:%s:%s" % (
            reason,
            run_date,
            parent_run_id,
        )

    def get_catchup_run(self, catchup_run_id):
        catchup_run_id = _positive_int(catchup_run_id, "catchup_run_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM x_post_catchup_run WHERE id=?",
                (catchup_run_id,),
            ).fetchone()
        if not row:
            raise XPostError(
                "x_post_catchup_run_not_found",
                "补发批次不存在",
                404,
            )
        return self._catchup_item(row)

    def query_catchup_plan(
        self,
        run_date,
        parent_run_id,
        reason="scope_expansion_v1",
    ):
        run_date = _date_value(run_date, "run_date")
        parent_run_id = _positive_int(parent_run_id, "parent_run_id")
        reason = _catchup_reason(reason)
        run_fields = (
            "id",
            "parent_run_id",
            "catchup_key",
            "run_date",
            "source_date",
            "reason",
            "account_ids_json",
            "status",
            "expected_count",
            "queued_count",
            "published_count",
            "failed_count",
            "unknown_count",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        queue_fields = (
            "id",
            "run_id",
            "catchup_run_id",
            "run_date",
            "source_date",
            "account_id",
            "candidate_rank",
            "status",
            "created_at",
            "updated_at",
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN")
            run_row = conn.execute(
                "SELECT %s FROM x_post_catchup_run "
                "WHERE parent_run_id=? AND run_date=? AND reason=?"
                % ",".join(run_fields),
                (parent_run_id, run_date, reason),
            ).fetchone()
            if run_row:
                queue_rows = conn.execute(
                    "SELECT %s FROM x_post_queue WHERE catchup_run_id=? "
                    "ORDER BY candidate_rank,id"
                    % ",".join(queue_fields),
                    (run_row["id"],),
                ).fetchall()
            else:
                queue_rows = []
            conn.commit()
        if not run_row:
            return {"found": False, "run": None, "queues": []}
        run_item = self._catchup_item(run_row)
        return {
            "found": True,
            "run": run_item,
            "queues": [
                dict(_row_dict(row), batch_kind="catchup")
                for row in queue_rows
            ],
        }

    def create_catchup_plan(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        candidates,
        configured_account_ids,
        require_pool=True,
    ):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        parent_run_id = _positive_int(parent_run_id, "parent_run_id")
        reason = _catchup_reason(reason)
        configured_account_ids = _configured_account_scope(
            configured_account_ids
        )
        if (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - datetime.strptime(source_date, "%Y-%m-%d").date()
        ).days != 1:
            raise XPostError(
                "invalid_request",
                "source_date必须是run_date前一天",
                400,
            )
        batch_size = len(candidates) if isinstance(candidates, list) else 0
        if batch_size < 1 or batch_size > MAX_DAILY_BATCH_SIZE:
            raise XPostError(
                "x_post_catchup_candidate_shortage",
                "补发计划必须一次提交1到%s个候选"
                % MAX_DAILY_BATCH_SIZE,
                409,
            )

        prepared = []
        account_ids = set()
        material_keys = set()
        pool_item_ids = set()
        for index, candidate in enumerate(candidates, 1):
            payload = (
                dict(candidate)
                if isinstance(candidate, dict)
                else candidate
            )
            if (
                isinstance(payload, dict)
                and _date_value(
                    payload.get("source_date"),
                    "source_date",
                )
                != source_date
            ):
                raise XPostError(
                    "invalid_request",
                    "候选source_date与补发批次不一致",
                    400,
                )
            values = self._queue_payload(
                payload,
                run_date=run_date,
                candidate_rank=index,
                require_compliance=True,
            )
            values["run_id"] = None
            values["catchup_run_id"] = None
            values["idempotency_key"] = (
                "xpost:catchup:v1:%s:%s"
                % (run_date, values["account_id"])
            )
            if values["account_id"] in account_ids:
                raise XPostError(
                    "invalid_request",
                    "补发计划账号必须互不相同",
                    400,
                )
            if values["material_key"] in material_keys:
                raise XPostError(
                    "invalid_request",
                    "补发计划素材必须互不相同",
                    400,
                )
            if require_pool and values["pool_item_id"] is None:
                raise XPostError(
                    "x_post_pool_required",
                    "正式补发计划候选必须来自素材池",
                    409,
                )
            if values["pool_item_id"] is not None:
                if values["pool_item_id"] in pool_item_ids:
                    raise XPostError(
                        "invalid_request",
                        "补发计划素材池记录必须互不相同",
                        400,
                    )
                pool_item_ids.add(values["pool_item_id"])
            account_ids.add(values["account_id"])
            material_keys.add(values["material_key"])
            prepared.append(values)
        if pool_item_ids and len(pool_item_ids) != len(prepared):
            raise XPostError(
                "invalid_request",
                "补发计划候选必须全部来自素材池或全部不关联素材池",
                400,
            )

        timestamp = utc_now()
        columns = (
            ("idempotency_key",)
            + QUEUE_LEDGER_FIELDS
            + QUEUE_FIELDS
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_run = conn.execute(
                "SELECT * FROM x_post_catchup_run WHERE parent_run_id=?",
                (parent_run_id,),
            ).fetchone()
            exclude_id = (
                int(existing_run["id"])
                if existing_run
                else None
            )
            _parent, _parent_accounts, missing_accounts = (
                self._catchup_parent_context(
                    conn,
                    run_date,
                    source_date,
                    parent_run_id,
                    configured_account_ids,
                    exclude_catchup_run_id=exclude_id,
                )
            )
            requested_accounts = tuple(
                int(values["account_id"])
                for values in prepared
            )
            if (
                not missing_accounts
                or requested_accounts != missing_accounts
                or batch_size != len(missing_accounts)
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_catchup_scope_mismatch",
                    "补发候选账号必须严格等于当前配置减去当日已有队列",
                    409,
                )
            expected_key = self._catchup_key(
                run_date,
                parent_run_id,
                reason,
            )
            account_ids_json = json.dumps(
                list(missing_accounts),
                separators=(",", ":"),
            )
            catchup_run_id = None
            if existing_run:
                stored_accounts = _stored_account_ids(
                    existing_run["account_ids_json"],
                    existing_run["expected_count"],
                )
                if (
                    str(existing_run["catchup_key"]) != expected_key
                    or str(existing_run["run_date"]) != run_date
                    or str(existing_run["source_date"]) != source_date
                    or str(existing_run["reason"]) != reason
                    or stored_accounts != missing_accounts
                    or int(existing_run["expected_count"])
                    != batch_size
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_catchup_run_exists",
                        "该每日批次已存在不同范围的补发批次",
                        409,
                    )
                existing_queues = conn.execute(
                    "SELECT * FROM x_post_queue "
                    "WHERE catchup_run_id=? "
                    "ORDER BY candidate_rank,id",
                    (existing_run["id"],),
                ).fetchall()
                if len(existing_queues) == batch_size:
                    expected = [
                        (
                            int(values["account_id"]),
                            values["material_key"],
                            values["pool_item_id"],
                        )
                        for values in prepared
                    ]
                    actual = [
                        (
                            int(row["account_id"]),
                            str(row["material_key"]),
                            int(row["pool_item_id"])
                            if row["pool_item_id"] is not None
                            else None,
                        )
                        for row in existing_queues
                    ]
                    if actual != expected:
                        conn.rollback()
                        raise XPostError(
                            "x_post_catchup_run_exists",
                            "该每日批次已存在不同的补发计划",
                            409,
                        )
                    conn.commit()
                    item = self._catchup_item(existing_run)
                    item["queues"] = [
                        _row_dict(row)
                        for row in existing_queues
                    ]
                    item["created"] = False
                    return item
                if (
                    existing_queues
                    or existing_run["status"] != "failed_preflight"
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_storage_conflict",
                        "已有补发批次队列数量异常",
                        500,
                    )
                catchup_run_id = int(existing_run["id"])

            previous_pool_order = None
            for values in prepared:
                if values["pool_item_id"] is not None:
                    pool = conn.execute(
                        "SELECT * FROM x_post_material_pool WHERE id=?",
                        (values["pool_item_id"],),
                    ).fetchone()
                    if not pool:
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_not_found",
                            "候选素材池记录不存在",
                            404,
                        )
                    if (
                        pool["status"] != "unpublished"
                        or str(pool["material_key"])
                        != values["material_key"]
                        or str(pool["created_at"])
                        != values["pool_created_at"]
                    ):
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_unavailable",
                            "候选素材池记录已发布、已变更或与素材不一致",
                            409,
                        )
                    if conn.execute(
                        "SELECT 1 FROM x_post_queue "
                        "WHERE pool_item_id=? OR material_key=?",
                        (
                            values["pool_item_id"],
                            values["material_key"],
                        ),
                    ).fetchone():
                        conn.rollback()
                        raise XPostError(
                            "x_post_pool_item_occupied",
                            "候选素材池记录已被发布队列占用",
                            409,
                        )
                    pool_order = (
                        str(pool["created_at"]),
                        int(pool["id"]),
                    )
                    if (
                        previous_pool_order is not None
                        and pool_order >= previous_pool_order
                    ):
                        conn.rollback()
                        raise XPostError(
                            "invalid_request",
                            "补发计划必须按素材池创建时间倒序提交",
                            400,
                        )
                    previous_pool_order = pool_order
                elif conn.execute(
                    "SELECT 1 FROM x_post_material_pool "
                    "WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_pool_item_occupied",
                        "候选素材已进入素材池，正式队列必须绑定对应素材池记录",
                        409,
                    )
                if conn.execute(
                    "SELECT id FROM x_post_queue WHERE material_key=?",
                    (values["material_key"],),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_material_already_used",
                        "候选素材已被X发布队列占用",
                        409,
                    )
                if conn.execute(
                    "SELECT id FROM x_post_queue "
                    "WHERE account_id=? AND run_date=?",
                    (values["account_id"], run_date),
                ).fetchone():
                    conn.rollback()
                    raise XPostError(
                        "x_post_account_day_already_reserved",
                        "候选X账号当日已有发布队列",
                        409,
                    )

            if catchup_run_id is None:
                cursor = conn.execute(
                    "INSERT INTO x_post_catchup_run("
                    "parent_run_id,catchup_key,run_date,source_date,"
                    "reason,account_ids_json,status,expected_count,"
                    "queued_count,started_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
                    (
                        parent_run_id,
                        expected_key,
                        run_date,
                        source_date,
                        reason,
                        account_ids_json,
                        batch_size,
                        batch_size,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                catchup_run_id = int(cursor.lastrowid)
            else:
                conn.execute(
                    "UPDATE x_post_catchup_run SET status='queued',"
                    "expected_count=?,queued_count=?,published_count=0,"
                    "failed_count=0,unknown_count=0,error_code='',"
                    "error_message='',started_at=?,finished_at='',"
                    "updated_at=? WHERE id=? AND status='failed_preflight'",
                    (
                        batch_size,
                        batch_size,
                        timestamp,
                        timestamp,
                        catchup_run_id,
                    ),
                )

            queue_ids = []
            placeholders = ",".join("?" for _field in columns)
            try:
                for values in prepared:
                    values["catchup_run_id"] = catchup_run_id
                    queue_cursor = conn.execute(
                        "INSERT INTO x_post_queue("
                        "%s,status,created_at,updated_at"
                        ") VALUES(%s,'queued',?,?)"
                        % (",".join(columns), placeholders),
                        tuple(values[field] for field in columns)
                        + (timestamp, timestamp),
                    )
                    queue_ids.append(int(queue_cursor.lastrowid))
                    if values["pool_item_id"] is not None:
                        conn.execute(
                            "UPDATE x_post_material_pool "
                            "SET last_checked_at=?,last_error_code='',"
                            "last_error_message='',updated_at=? "
                            "WHERE id=? AND status='unpublished'",
                            (
                                timestamp,
                                timestamp,
                                values["pool_item_id"],
                            ),
                        )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise XPostError(
                    "x_post_storage_conflict",
                    "补发计划唯一约束冲突",
                    409,
                ) from exc
            conn.commit()
        item = self.get_catchup_run(catchup_run_id)
        item["queues"] = [
            self.get_queue(queue_id)
            for queue_id in queue_ids
        ]
        item["created"] = True
        return item

    def record_catchup_failure(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        expected_missing_count,
        configured_account_ids,
        error_code,
        error_message,
    ):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        parent_run_id = _positive_int(parent_run_id, "parent_run_id")
        reason = _catchup_reason(reason)
        expected_missing_count = _positive_int(
            expected_missing_count,
            "expected_missing_count",
        )
        if expected_missing_count > MAX_DAILY_BATCH_SIZE:
            raise XPostError(
                "invalid_request",
                "expected_missing_count不能超过%s"
                % MAX_DAILY_BATCH_SIZE,
                400,
            )
        configured_account_ids = _configured_account_scope(
            configured_account_ids
        )
        if (
            datetime.strptime(run_date, "%Y-%m-%d").date()
            - datetime.strptime(source_date, "%Y-%m-%d").date()
        ).days != 1:
            raise XPostError(
                "invalid_request",
                "source_date必须是run_date前一天",
                400,
            )
        try:
            code = _clean_token(
                error_code or "x_post_catchup_preflight_failed",
                "error code",
                64,
            )
        except ValueError:
            raise XPostError(
                "invalid_request",
                "error_code无效",
                400,
            ) from None
        message = redact_text(error_message, 500)
        if not message:
            message = "X补发批次预检失败"
        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM x_post_catchup_run WHERE parent_run_id=?",
                (parent_run_id,),
            ).fetchone()
            exclude_id = int(existing["id"]) if existing else None
            _parent, _parent_accounts, missing_accounts = (
                self._catchup_parent_context(
                    conn,
                    run_date,
                    source_date,
                    parent_run_id,
                    configured_account_ids,
                    exclude_catchup_run_id=exclude_id,
                )
            )
            if (
                not missing_accounts
                or len(missing_accounts) != expected_missing_count
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_catchup_scope_mismatch",
                    "补发失败记录数量与当日缺失账号范围不一致",
                    409,
                )
            expected_key = self._catchup_key(
                run_date,
                parent_run_id,
                reason,
            )
            account_ids_json = json.dumps(
                list(missing_accounts),
                separators=(",", ":"),
            )
            if existing:
                stored_accounts = _stored_account_ids(
                    existing["account_ids_json"],
                    existing["expected_count"],
                )
                if (
                    str(existing["catchup_key"]) != expected_key
                    or str(existing["run_date"]) != run_date
                    or str(existing["source_date"]) != source_date
                    or str(existing["reason"]) != reason
                    or stored_accounts != missing_accounts
                    or int(existing["expected_count"])
                    != expected_missing_count
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_catchup_run_exists",
                        "该每日批次已存在不同范围的补发批次",
                        409,
                    )
                queue_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM x_post_queue "
                        "WHERE catchup_run_id=?",
                        (existing["id"],),
                    ).fetchone()[0]
                )
                if queue_count:
                    conn.commit()
                    item = self._catchup_item(existing)
                    item["recorded"] = False
                    return item
                if (
                    existing["status"] == "failed_preflight"
                    and existing["error_code"] == code
                    and existing["error_message"] == message
                ):
                    conn.commit()
                    item = self._catchup_item(existing)
                    item["recorded"] = False
                    return item
                conn.execute(
                    "UPDATE x_post_catchup_run "
                    "SET status='failed_preflight',queued_count=0,"
                    "published_count=0,failed_count=0,unknown_count=0,"
                    "error_code=?,error_message=?,finished_at=?,updated_at=? "
                    "WHERE id=?",
                    (
                        code,
                        message,
                        timestamp,
                        timestamp,
                        existing["id"],
                    ),
                )
                catchup_run_id = int(existing["id"])
                recorded = True
            else:
                cursor = conn.execute(
                    "INSERT INTO x_post_catchup_run("
                    "parent_run_id,catchup_key,run_date,source_date,"
                    "reason,account_ids_json,status,expected_count,"
                    "queued_count,error_code,error_message,started_at,"
                    "finished_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,'failed_preflight',?,0,"
                    "?,?,?,?,?,?)",
                    (
                        parent_run_id,
                        expected_key,
                        run_date,
                        source_date,
                        reason,
                        account_ids_json,
                        expected_missing_count,
                        code,
                        message,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                catchup_run_id = int(cursor.lastrowid)
                recorded = True
            conn.commit()
        item = self.get_catchup_run(catchup_run_id)
        item["recorded"] = recorded
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

    def query_daily_plan(self, run_date):
        """Return one atomic, identity-only snapshot for daily-run recovery."""
        run_date = _date_value(run_date, "run_date")
        run_fields = (
            "id",
            "run_date",
            "source_date",
            "status",
            "expected_count",
            "queued_count",
            "published_count",
            "failed_count",
            "unknown_count",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        queue_fields = (
            "id",
            "run_id",
            "run_date",
            "source_date",
            "account_id",
            "candidate_rank",
            "status",
            "created_at",
            "updated_at",
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            # Keep the run and queue reads in one SQLite snapshot. This route is
            # deliberately read-only and never returns copy, URLs, or log data.
            conn.execute("BEGIN")
            run_row = conn.execute(
                "SELECT %s FROM x_post_daily_run WHERE run_date=?"
                % ",".join(run_fields),
                (run_date,),
            ).fetchone()
            if run_row:
                queue_rows = conn.execute(
                    "SELECT %s FROM x_post_queue WHERE run_id=? "
                    "ORDER BY candidate_rank,id"
                    % ",".join(queue_fields),
                    (run_row["id"],),
                ).fetchall()
            else:
                queue_rows = []
            conn.commit()
        if not run_row:
            return {"found": False, "run": None, "queues": []}
        return {
            "found": True,
            "run": {field: run_row[field] for field in run_fields},
            "queues": [
                {field: row[field] for field in queue_fields}
                for row in queue_rows
            ],
        }

    def record_run_failure(
        self,
        run_date,
        source_date,
        error_code,
        error_message,
        expected_count,
    ):
        run_date = _date_value(run_date, "run_date")
        source_date = _date_value(source_date, "source_date")
        expected_count = _positive_int(expected_count, "expected_count")
        if expected_count > MAX_DAILY_BATCH_SIZE:
            raise XPostError(
                "invalid_request",
                "expected_count不能超过%s" % MAX_DAILY_BATCH_SIZE,
                400,
            )
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
                if (
                    str(existing["source_date"]) != source_date
                    or int(existing["expected_count"]) != expected_count
                ):
                    conn.rollback()
                    raise XPostError(
                        "x_post_daily_run_exists",
                        "该日期已存在不同范围的X发布批次",
                        409,
                    )
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
                    ") VALUES(?,?,'failed_preflight',?,0,?,?,?,?,?,?)",
                    (
                        run_date,
                        source_date,
                        expected_count,
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

    def query_material_keys(self, material_keys, *, include_pool=False):
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
        occupied = set()
        with contextlib.closing(_connect(self.db_path)) as conn:
            for offset in range(0, len(normalized), SQLITE_QUERY_BATCH_SIZE):
                batch = normalized[offset : offset + SQLITE_QUERY_BATCH_SIZE]
                placeholders = ",".join("?" for _item in batch)
                occupied.update(
                    str(row["material_key"])
                    for row in conn.execute(
                        "SELECT material_key FROM x_post_queue WHERE material_key IN (%s)"
                        % placeholders,
                        tuple(batch),
                    ).fetchall()
                )
                if include_pool:
                    occupied.update(
                        str(row["material_key"])
                        for row in conn.execute(
                            "SELECT material_key FROM x_post_material_pool "
                            "WHERE material_key IN (%s)" % placeholders,
                            tuple(batch),
                        ).fetchall()
                    )
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
            if re.fullmatch(r"[0-9]+", material_id):
                clauses.append("q.material_key=?")
                values.append(normalize_material_key(material_id))
            else:
                clauses.append("q.content_id=?")
                values.append(_drama_content_id(material_id))
        source_type = str(payload.get("source_type", "") or "").strip()
        if source_type:
            clauses.append("q.source_type=?")
            values.append(_schedule_source_type(source_type))
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
            "SELECT q.id AS queue_id,q.run_id,q.catchup_run_id,"
            "q.schedule_run_id,q.manual_run_id,"
            "CASE WHEN q.run_id IS NOT NULL THEN 'daily' "
            "WHEN q.catchup_run_id IS NOT NULL THEN 'catchup' "
            "WHEN q.schedule_run_id IS NOT NULL THEN 'schedule' "
            "WHEN q.manual_run_id IS NOT NULL "
            "AND COALESCE(mr.trigger_source,'manual')='auto_template' "
            "THEN 'auto_template' "
            "WHEN q.manual_run_id IS NOT NULL THEN 'manual' "
            "ELSE 'canary' END AS batch_kind,"
            "q.source_type,q.run_date,q.source_date,q.account_id,"
            "q.pool_item_id,q.pool_created_at,q.drama_pool_item_id,"
            "q.drama_pool_created_at,q.episode_number,q.episode_key,q.name_tag,"
            "q.account_username,q.page_name,q.page_id,q.material_id,q.material_name,q.content_id,"
            "q.material_language,q.drama_name,q.tag,q.candidate_rank,q.spend,"
            "q.media_repair_trigger_code,q.media_repair_job_key,q.media_repair_profile,"
            "q.facebook_violation_count,q.tiktok_violation_count,q.twitter_violation_count,"
            "q.resource_audit_count,q.dangerous_tag_count,q.status AS queue_status,"
            "l.id AS log_id,COALESCE(l.status,q.status) AS status,COALESCE(l.attempt_count,0) AS attempt_count,"
            "CASE WHEN l.status='post_creating' OR COALESCE(l.unknown_outcome,0)=1 "
            "THEN 1 ELSE 0 END AS unknown_outcome,COALESCE(l.short_url,'') AS short_url,"
            "COALESCE(l.x_post_id,'') AS post_id,COALESCE(l.x_post_url,'') AS preview_url,"
            "COALESCE(l.error_code,'') AS error_code,COALESCE(l.error_message,'') AS error_message,"
            "COALESCE(l.started_at,'') AS started_at,COALESCE(l.published_at,'') AS published_at,"
            "q.created_at,q.updated_at FROM x_post_queue q "
            "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
            "LEFT JOIN x_post_manual_run mr ON mr.id=q.manual_run_id"
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
        queue = conn.execute(
            "SELECT run_id,catchup_run_id,schedule_run_id,manual_run_id "
            "FROM x_post_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not queue:
            return
        if sum(
            value is not None
            for value in (
                queue["run_id"],
                queue["catchup_run_id"],
                queue["schedule_run_id"],
                queue["manual_run_id"],
            )
        ) > 1:
            raise XPostError(
                "x_post_storage_conflict",
                "发布队列关联了多个批次",
                500,
            )
        if queue["catchup_run_id"]:
            table_name = "x_post_catchup_run"
            queue_column = "catchup_run_id"
            batch_id = int(queue["catchup_run_id"])
        elif queue["run_id"]:
            table_name = "x_post_daily_run"
            queue_column = "run_id"
            batch_id = int(queue["run_id"])
        elif queue["schedule_run_id"]:
            table_name = "x_post_schedule_run"
            queue_column = "schedule_run_id"
            batch_id = int(queue["schedule_run_id"])
        elif queue["manual_run_id"]:
            table_name = "x_post_manual_run"
            queue_column = "manual_run_id"
            batch_id = int(queue["manual_run_id"])
        else:
            return
        counts = conn.execute(
            (
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
            WHERE q.%s=?
            """
                % queue_column
            ),
            (batch_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM %s WHERE id=?" % table_name,
            (batch_id,),
        ).fetchone()
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
        elif (
            table_name == "x_post_schedule_run"
            and str(run["source_type"]) == "drama"
            and failed_count > 0
        ):
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
            "UPDATE %s SET status=?,queued_count=?,published_count=?,failed_count=?,"
            "unknown_count=?,finished_at=?,updated_at=? WHERE id=?"
            % table_name,
            (
                status,
                queued_count,
                published_count,
                failed_count,
                unknown_count,
                finished_at,
                timestamp,
                batch_id,
            ),
        )

    @staticmethod
    def _mark_drama_episode_published(conn, queue_id, timestamp):
        queue = conn.execute(
            "SELECT source_type,drama_pool_item_id,content_id,"
            "episode_number,episode_key,drama_replay_generation,"
            "account_id "
            "FROM x_post_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not queue or queue["source_type"] != "drama":
            return
        pool = conn.execute(
            "SELECT * FROM x_post_drama_pool WHERE id=?",
            (queue["drama_pool_item_id"],),
        ).fetchone()
        expected_key = _drama_episode_key(
            queue["content_id"],
            queue["episode_number"],
            queue["drama_replay_generation"],
        )
        if (
            not pool
            or str(pool["content_id"]) != str(queue["content_id"])
            or str(queue["episode_key"]) != expected_key
            or int(pool["replay_generation"])
            != int(queue["drama_replay_generation"])
            or int(pool["assigned_account_id"] or 0)
            != int(queue["account_id"])
        ):
            raise XPostError(
                "x_post_storage_conflict",
                "发布队列与短剧池记录不一致",
                500,
                True,
            )
        episode_number = int(queue["episode_number"])
        next_sub_number = int(pool["next_sub_number"])
        if episode_number < next_sub_number:
            return
        if episode_number != next_sub_number:
            raise XPostError(
                "x_post_storage_conflict",
                "短剧发布进度不是连续集数",
                500,
                True,
            )
        published_count = int(pool["published_episode_count"]) + 1
        next_number = episode_number + 1
        completed = next_number > int(pool["free_episode_count"])
        cursor = conn.execute(
            "UPDATE x_post_drama_pool SET status=?,next_sub_number=?,"
            "published_episode_count=?,completed_at=?,last_checked_at=?,"
            "last_error_code='',last_error_message='',updated_at=? "
            "WHERE id=? AND replay_generation=? "
            "AND next_sub_number=?",
            (
                "completed" if completed else "active",
                next_number,
                published_count,
                timestamp if completed else "",
                timestamp,
                timestamp,
                pool["id"],
                queue["drama_replay_generation"],
                episode_number,
            ),
        )
        if cursor.rowcount != 1:
            raise XPostError(
                "x_post_storage_conflict",
                "短剧发布进度写入冲突",
                500,
                True,
            )

    @staticmethod
    def _mark_drama_needs_review(
        conn,
        queue_id,
        timestamp,
        error_code,
        error_message,
    ):
        queue = conn.execute(
            "SELECT source_type,drama_pool_item_id FROM x_post_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if (
            not queue
            or queue["source_type"] != "drama"
            or queue["drama_pool_item_id"] is None
        ):
            return
        conn.execute(
            "UPDATE x_post_drama_pool SET status='needs_review',"
            "last_checked_at=?,last_error_code=?,last_error_message=?,"
            "updated_at=? WHERE id=? AND status<>'completed'",
            (
                timestamp,
                str(error_code or "x_post_failed")[:64],
                redact_text(error_message, 500),
                timestamp,
                queue["drama_pool_item_id"],
            ),
        )

    @staticmethod
    def _mark_pool_published(conn, queue_id, timestamp):
        queue = conn.execute(
            "SELECT pool_item_id,material_key FROM x_post_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if not queue or queue["pool_item_id"] is None:
            return
        pool = conn.execute(
            "SELECT id,material_key,status,published_at FROM x_post_material_pool WHERE id=?",
            (queue["pool_item_id"],),
        ).fetchone()
        if not pool or str(pool["material_key"]) != str(queue["material_key"]):
            raise XPostError(
                "x_post_storage_conflict",
                "发布队列与素材池记录不一致",
                500,
                True,
            )
        if pool["status"] == "published" and pool["published_at"]:
            return
        cursor = conn.execute(
            "UPDATE x_post_material_pool SET status='published',published_at=?,"
            "last_checked_at=?,last_error_code='',last_error_message='',updated_at=? "
            "WHERE id=? AND status='unpublished'",
            (timestamp, timestamp, timestamp, pool["id"]),
        )
        if cursor.rowcount != 1:
            raise XPostError(
                "x_post_storage_conflict",
                "素材池发布状态写入冲突",
                500,
                True,
            )

    def get_log(self, log_id):
        log_id = _positive_int(log_id, "log_id")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM x_post_publish_log WHERE id=?", (log_id,)).fetchone()
        if not row:
            raise XPostError("x_post_log_not_found", "发布日志不存在", 404)
        return _row_dict(row)

    @staticmethod
    def _assert_drama_queue_assignment(conn, queue):
        if not queue or str(queue["source_type"] or "") != "drama":
            return
        pool = None
        if queue["drama_pool_item_id"] is not None:
            pool = conn.execute(
                "SELECT id,content_id,replay_generation,"
                "assigned_account_id "
                "FROM x_post_drama_pool WHERE id=?",
                (queue["drama_pool_item_id"],),
            ).fetchone()
        if pool is None:
            pool = conn.execute(
                "SELECT id,content_id,replay_generation,"
                "assigned_account_id "
                "FROM x_post_drama_pool WHERE content_id=?",
                (queue["content_id"],),
            ).fetchone()
        if (
            not pool
            or str(pool["content_id"]) != str(queue["content_id"])
            or int(pool["replay_generation"])
            != int(queue["drama_replay_generation"])
            or int(pool["assigned_account_id"] or 0) <= 0
            or int(pool["assigned_account_id"]) != int(queue["account_id"])
        ):
            raise XPostError(
                "x_post_drama_account_binding_conflict",
                "短剧发布队列与固定发布账号不一致，已阻止发布",
                409,
            )

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
            if not row or str(row["status"]) != "published":
                self._assert_drama_queue_assignment(conn, queue)
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
            queue = conn.execute(
                "SELECT * FROM x_post_queue WHERE id=?",
                (row["queue_id"],),
            ).fetchone()
            self._assert_drama_queue_assignment(conn, queue)
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
                self._mark_pool_published(conn, row["queue_id"], timestamp)
                self._mark_drama_episode_published(
                    conn,
                    row["queue_id"],
                    timestamp,
                )
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
            self._mark_pool_published(conn, row["queue_id"], timestamp)
            self._mark_drama_episode_published(
                conn,
                row["queue_id"],
                timestamp,
            )
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
            self._mark_drama_needs_review(
                conn,
                row["queue_id"],
                timestamp,
                "x_post_outcome_unknown",
                message,
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
            self._mark_drama_needs_review(
                conn,
                row["queue_id"],
                timestamp,
                code,
                message,
            )
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
            self._mark_drama_needs_review(
                conn,
                row["queue_id"],
                timestamp,
                code,
                message,
            )
            self._sync_run(conn, row["queue_id"], timestamp)
            conn.commit()
        return self.get_log(log_id)

    def recover_pre_x_schedule_failure(
        self,
        queue_id,
        expected_error_code,
        *,
        validate_only=False,
    ):
        """Requeue one exact, proven pre-X drama failure without publishing."""
        queue_id = _positive_int(queue_id, "queue_id")
        if not isinstance(validate_only, bool):
            raise XPostError(
                "invalid_request",
                "validate_only must be a boolean",
                400,
            )
        try:
            expected_error_code = _clean_token(
                expected_error_code,
                "expected error code",
                64,
            )
        except ValueError:
            raise XPostError(
                "invalid_request",
                "expected_error_code is invalid",
                400,
            ) from None
        if expected_error_code not in PRE_X_RECOVERABLE_ERROR_CODES:
            raise XPostError(
                "x_post_pre_x_recovery_not_allowed",
                "This failure code is not eligible for guarded recovery",
                409,
            )

        timestamp = utc_now()
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            queue = conn.execute(
                "SELECT * FROM x_post_queue WHERE id=?",
                (queue_id,),
            ).fetchone()
            log = conn.execute(
                "SELECT * FROM x_post_publish_log WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            run = (
                conn.execute(
                    "SELECT * FROM x_post_schedule_run WHERE id=?",
                    (queue["schedule_run_id"],),
                ).fetchone()
                if queue and queue["schedule_run_id"] is not None
                else None
            )
            pool = (
                conn.execute(
                    "SELECT * FROM x_post_drama_pool WHERE id=?",
                    (queue["drama_pool_item_id"],),
                ).fetchone()
                if queue and queue["drama_pool_item_id"] is not None
                else None
            )
            binding_queue = (
                conn.execute(
                    "SELECT id,source_type,drama_pool_item_id,content_id,"
                    "drama_replay_generation,account_id,episode_number,"
                    "status "
                    "FROM x_post_queue WHERE id=?",
                    (pool["assigned_source_queue_id"],),
                ).fetchone()
                if pool and pool["assigned_source_queue_id"] is not None
                else None
            )
            expected_slot_key = (
                "xpost:schedule:v1:%s:%s:%s"
                % (
                    str(run["source_type"]),
                    str(run["run_date"]),
                    str(run["publish_time"]).replace(":", ""),
                )
                if run
                else ""
            )
            try:
                frozen_account_ids = (
                    _stored_account_ids(
                        run["account_ids_json"],
                        run["expected_count"],
                    )
                    if run
                    else ()
                )
            except XPostError:
                frozen_account_ids = ()
            conflict = (
                not queue
                or not log
                or not run
                or not pool
                or not binding_queue
                or str(queue["source_type"]) != "drama"
                or str(run["source_type"]) != "drama"
                or str(run["timezone"]) != SCHEDULE_TIMEZONE
                or str(run["slot_key"]) != expected_slot_key
                or str(queue["run_date"]) != str(run["run_date"])
                or not frozen_account_ids
                or str(queue["status"]) != "failed"
                or int(queue["candidate_rank"] or 0) != 1
                or str(log["status"]) != "failed"
                or int(log["account_id"] or 0)
                != int(queue["account_id"])
                or str(log["error_code"]) != expected_error_code
                or int(log["attempt_count"] or 0) != 0
                or int(log["unknown_outcome"] or 0) != 0
                or any(
                    str(log[field] or "")
                    for field in (
                        "long_url",
                        "short_url",
                        "post_text",
                        "x_media_id",
                        "x_post_id",
                        "x_post_url",
                        "started_at",
                        "published_at",
                    )
                )
                or str(run["status"]) != "stopped"
                or int(run["expected_count"] or 0)
                != int(run["queued_count"] or 0)
                or int(run["published_count"] or 0) != 0
                or int(run["failed_count"] or 0) != 1
                or int(run["unknown_count"] or 0) != 0
                or str(pool["status"]) != "needs_review"
                or str(pool["last_error_code"]) != expected_error_code
                or int(pool["assigned_account_id"] or 0)
                != int(queue["account_id"])
                or int(pool["assigned_source_queue_id"] or 0) <= 0
                or int(binding_queue["id"]) == queue_id
                or str(binding_queue["source_type"]) != "drama"
                or int(binding_queue["drama_pool_item_id"] or 0)
                != int(pool["id"])
                or str(binding_queue["content_id"])
                != str(pool["content_id"])
                or int(binding_queue["account_id"] or 0)
                != int(queue["account_id"])
                or str(binding_queue["status"]) != "published"
                or int(binding_queue["episode_number"] or 0)
                >= int(queue["episode_number"] or 0)
                or str(pool["content_id"])
                != str(queue["content_id"])
                or int(pool["replay_generation"] or 0)
                != int(queue["drama_replay_generation"] or 0)
                or int(binding_queue["drama_replay_generation"] or 0)
                != int(queue["drama_replay_generation"] or 0)
                or int(pool["next_sub_number"] or 0)
                != int(queue["episode_number"] or 0)
            )
            if conflict:
                conn.rollback()
                raise XPostError(
                    "x_post_pre_x_recovery_conflict",
                    "Queue, log, run, or drama state is not an exact pre-X failure",
                    409,
                )

            siblings = conn.execute(
                "SELECT q.id,q.status,q.candidate_rank,q.account_id,"
                "q.source_type,l.id AS log_id "
                "FROM x_post_queue q "
                "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
                "WHERE q.schedule_run_id=? ORDER BY q.candidate_rank,q.id",
                (run["id"],),
            ).fetchall()
            if (
                len(siblings) != int(run["expected_count"])
                or int(siblings[0]["id"]) != queue_id
                or int(siblings[0]["log_id"] or 0) != int(log["id"])
                or [
                    int(row["candidate_rank"] or 0)
                    for row in siblings
                ]
                != list(range(1, len(siblings) + 1))
                or tuple(
                    int(row["account_id"] or 0)
                    for row in siblings
                )
                != frozen_account_ids
                or any(
                    str(row["source_type"]) != "drama"
                    for row in siblings
                )
                or any(
                    str(row["status"]) != "queued"
                    or row["log_id"] is not None
                    for row in siblings[1:]
                )
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_pre_x_recovery_conflict",
                    "Frozen schedule siblings changed before recovery",
                    409,
                )

            result = {
                "queue_id": queue_id,
                "log_id": int(log["id"]),
                "schedule_run_id": int(run["id"]),
                "drama_pool_item_id": int(pool["id"]),
                "expected_error_code": expected_error_code,
                "validate_only": validate_only,
                "validated_count": 1,
                "updated_count": 0,
            }
            if validate_only:
                conn.commit()
                return result

            log_cursor = conn.execute(
                "UPDATE x_post_publish_log SET status='reserved',"
                "long_url='',short_url='',post_text='',x_media_id='',"
                "x_post_id='',x_post_url='',error_code='',error_message='',"
                "unknown_outcome=0,started_at='',published_at='',updated_at=? "
                "WHERE id=? AND status='failed' AND attempt_count=0 "
                "AND unknown_outcome=0 AND error_code=?",
                (
                    timestamp,
                    log["id"],
                    expected_error_code,
                ),
            )
            queue_cursor = conn.execute(
                "UPDATE x_post_queue SET status='queued',updated_at=? "
                "WHERE id=? AND status='failed'",
                (timestamp, queue_id),
            )
            pool_cursor = conn.execute(
                "UPDATE x_post_drama_pool SET status='active',"
                "last_checked_at=?,last_error_code='',last_error_message='',"
                "updated_at=? WHERE id=? AND status='needs_review' "
                "AND assigned_account_id=? AND next_sub_number=? "
                "AND last_error_code=?",
                (
                    timestamp,
                    timestamp,
                    pool["id"],
                    queue["account_id"],
                    queue["episode_number"],
                    expected_error_code,
                ),
            )
            if (
                int(log_cursor.rowcount or 0) != 1
                or int(queue_cursor.rowcount or 0) != 1
                or int(pool_cursor.rowcount or 0) != 1
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_pre_x_recovery_conflict",
                    "Pre-X recovery state changed during the transaction",
                    409,
                )
            self._sync_run(conn, queue_id, timestamp)
            updated_run = conn.execute(
                "SELECT status,failed_count,unknown_count "
                "FROM x_post_schedule_run WHERE id=?",
                (run["id"],),
            ).fetchone()
            if (
                not updated_run
                or str(updated_run["status"]) != "queued"
                or int(updated_run["failed_count"] or 0) != 0
                or int(updated_run["unknown_count"] or 0) != 0
            ):
                conn.rollback()
                raise XPostError(
                    "x_post_pre_x_recovery_conflict",
                    "Frozen schedule did not return to queued state",
                    409,
                )
            conn.commit()
            result["updated_count"] = 1
            return result


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


def _account_has_premium_video_entitlement(account):
    subscription_type = (
        str(account.get("subscription_type", "") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if subscription_type == "premiumplus":
        subscription_type = "premium_plus"
    return subscription_type in PREMIUM_SUBSCRIPTION_TYPES


def probe_media(
    path,
    max_bytes=DEFAULT_MAX_MEDIA_BYTES,
    timeout=30,
    runner=None,
    max_duration_seconds=STANDARD_MAX_DURATION_SECONDS,
):
    """Fail closed unless ffprobe confirms the X canary video contract."""
    try:
        duration_limit = float(max_duration_seconds)
    except (TypeError, ValueError, OverflowError):
        raise XPostError(
            "invalid_configuration", "X video duration policy is invalid", 500
        ) from None
    if duration_limit not in {
        STANDARD_MAX_DURATION_SECONDS,
        PREMIUM_MAX_DURATION_SECONDS,
    }:
        raise XPostError(
            "invalid_configuration", "X video duration policy is invalid", 500
        )
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
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_value = format_data.get("duration")
    if not duration_value and len(videos) == 1:
        duration_value = videos[0].get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0.5:
        raise XPostError(
            "invalid_media_duration",
            "Video duration must be at least 0.5 seconds",
            422,
        )
    if duration > duration_limit:
        if duration_limit == STANDARD_MAX_DURATION_SECONDS:
            raise XPostError(
                "x_long_video_requires_premium",
                "Videos longer than 140 seconds require a token-confirmed X Premium subscription",
                422,
            )
        raise XPostError(
            "invalid_media_duration",
            "Premium X video duration must not exceed 4 hours",
            422,
        )
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
        media_category = str(media_category or "").strip()
        if media_category not in MEDIA_CATEGORIES:
            raise XPostError(
                "invalid_request", "X media category is invalid", 400
            )
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
            if queue.get("source_type") == "drama":
                post_text = build_drama_episode_post_text(
                    short_url,
                    queue.get("episode_number"),
                    queue.get("drama_name"),
                    queue["description"],
                    queue.get("body_template"),
                )
            else:
                post_text = build_post_text(
                    short_url,
                    queue.get("drama_name"),
                    queue["description"],
                    queue.get("body_template"),
                )
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
        if (
            queue.get("run_id")
            or queue.get("catchup_run_id")
            or queue.get("schedule_run_id")
            or queue.get("manual_run_id")
        ) and (
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
        premium_video_eligible = _account_has_premium_video_entitlement(
            account
        )
        duration_limit = (
            PREMIUM_MAX_DURATION_SECONDS
            if premium_video_eligible
            else STANDARD_MAX_DURATION_SECONDS
        )
        media_probe = probe_media(
            media["path"],
            max_bytes=max_media_bytes,
            timeout=timeout,
            max_duration_seconds=duration_limit,
        )
        expected_duration = float(
            queue.get("preflight_duration", 0) or 0
        )
        if expected_duration > 0 and abs(
            expected_duration - float(media_probe["duration"])
        ) > 0.05:
            raise XPostError(
                "media_preflight_changed",
                "素材时长与建计划前的预检记录不一致",
                409,
            )
        media_category = (
            PREMIUM_MEDIA_CATEGORY
            if float(media_probe["duration"])
            > STANDARD_MAX_DURATION_SECONDS
            else STANDARD_MEDIA_CATEGORY
        )
        if callable(storage_guard):
            storage_guard()
        store.mark_publishing(log["id"])
        x_client = XApiClient(http_client=http_client, sleeper=sleeper, timeout=timeout)
        uploaded = x_client.upload_media(
            access_token,
            media["path"],
            media_type=media["media_type"],
            media_category=media_category,
        )
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

"""Fail-closed core for the TikTok organic Post publishing pool.

Security boundaries:

* account list/read paths consume metadata-only loaders;
* an access token is fetched for one exact account only inside
  ``SnapshotAccountSource.publish_credentials``;
* credential and claim wrappers redact their secret values from ``repr``;
* no access token is persisted in the SQLite ledger tables;
* a remote ``publish_id`` moves a queue into reconcile-only state;
* unknown outcomes are terminal and are never selected by ``claim_due``;
* all three live gates default to closed.

The module is intentionally independent from ``app.py`` and from any concrete
TikTok API client so it can be tested without network or production data.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .links import (
    TT_SHORT_LINK_MAX_LOCAL_ID,
    TT_SHORT_LINK_NAMESPACE,
    build_short_url,
    short_link_id,
    validate_short_url,
    validate_w2a_url,
)


UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_CAPTION_CHARS = 2200
MAX_EVENT_MESSAGE_CHARS = 500
MAX_ACCOUNT_SETTINGS_BATCH = 50
FIXED_CAPTION_TEMPLATE = (
    "Watch the full story in the app 🎬\n\n"
    "Drama ID: {{contect_id}}\n\n"
    "Visit my profile → Open the link → Search the Drama ID → Watch now."
)

PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)
QUEUE_STATUSES = frozenset(
    {
        "scheduled",
        "claimed",
        "publishing",
        "reconciling",
        "published",
        "failed",
        "canceled",
        "missed",
        "blocked_compliance",
        "unknown",
    }
)
POOL_STATUSES = frozenset({"available", "reserved", "published", "canceled"})
RECURRING_POOL_STATUSES = frozenset(
    {"available", "reserved", "consumed", "canceled"}
)
MATERIAL_INTAKE_STATUSES = frozenset(
    {"queued", "preparing", "retry_wait", "ready", "failed", "canceled"}
)
DIRECT_TEST_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "ready",
        "publishing",
        "reconciling",
        "published",
        "failed",
        "unknown",
        "canceled",
    }
)
DIRECT_MATERIAL_BLOCKING_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "ready",
        "publishing",
        "reconciling",
        "unknown",
    }
)
DIRECT_ACCOUNT_BLOCKING_STATUSES = frozenset(
    {"publishing", "reconciling", "unknown"}
)
SCHEDULE_RUN_STATUSES = frozenset(
    {
        "claimed",
        "preflight_failed",
        "scheduled",
        "publishing",
        "reconciling",
        "published",
        "failed",
        "canceled",
        "missed",
        "blocked_compliance",
        "unknown",
    }
)
ACTIVE_QUEUE_STATUSES = frozenset(
    {"scheduled", "claimed", "publishing", "reconciling", "unknown"}
)
ACTIVE_SCHEDULE_RUN_STATUSES = frozenset(
    {"claimed", "scheduled", "publishing", "reconciling", "unknown"}
)
TERMINAL_SCHEDULE_RUN_STATUSES = SCHEDULE_RUN_STATUSES.difference(
    ACTIVE_SCHEDULE_RUN_STATUSES
)
TERMINAL_QUEUE_STATUSES = frozenset(
    {"published", "failed", "canceled", "missed", "blocked_compliance", "unknown"}
)
SENSITIVE_KEY_FRAGMENTS = (
    "access_token",
    "accesstoken",
    "authorization",
    "bearer",
    "client_secret",
    "refresh_token",
    "secret",
)

_MATERIAL_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
# TikTok currently returns Direct Post IDs such as
# ``v_pub_url~v2-1.7668584571734657042``. Keep this contract aligned with
# the GPU worker so a successfully initialized post is never stranded only
# because the CPU ledger rejects an upstream-safe identifier.
_PUBLISH_ID_RE = re.compile(r"\A[A-Za-z0-9._~:+/-]{1,512}\Z")
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_GPU_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{11,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")
_SINGLE_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")
_URL_PLACEHOLDER = "{url}"
_DESC_PLACEHOLDER = "{desc}"
_MAX_TT_SHORT_URL = build_short_url(
    TT_SHORT_LINK_NAMESPACE + TT_SHORT_LINK_MAX_LOCAL_ID
)
_PUBLISH_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_SHANGHAI_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def redact_text(value: Any, limit: int = MAX_EVENT_MESSAGE_CHARS) -> str:
    """Return a bounded message with common credential shapes removed."""

    text = str(value or "")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|client[_-]?secret)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = "".join(
        char for char in text if char in "\n\t" or ord(char) >= 32
    )
    return text[: max(0, int(limit))]


class TTPostError(RuntimeError):
    """Stable, secret-safe error returned by the core."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_post_error")[:96]
        self.status = int(status)
        self.message = redact_text(message)
        super().__init__(self.message)

    def __repr__(self) -> str:
        return "TTPostError(code=%r, status=%r)" % (self.code, self.status)


class AccountSourceError(TTPostError):
    pass


def _required_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > int(limit) or "\x00" in text:
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    return text


def _optional_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > int(limit) or "\x00" in text:
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    return text


def _normalize_description(value: Any) -> str:
    """Normalize one authoritative drama description without expanding macros."""

    raw = str(value or "")
    if "\x00" in raw:
        raise ValueError("description contains NUL")
    text = re.sub(r"\s+", " ", raw).strip()
    if len(text) > 4096 or any(ord(char) < 32 for char in text):
        raise ValueError("description is invalid")
    return text


def _material_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _MATERIAL_ID_RE.fullmatch(text):
        raise TTPostError("invalid_material_id", "素材ID必须是1到19位正整数", 400)
    return text


def _account_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ACCOUNT_ID_RE.fullmatch(text):
        raise TTPostError("invalid_account_id", "TikTok账号ID无效", 400)
    return text


def _positive_int(value: Any, label: str, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TTPostError("invalid_request", "%s无效" % label, 400) from None
    if result <= 0 or result > int(maximum):
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    return result


def _nonnegative_int(
    value: Any,
    label: str,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or (
            isinstance(value, float)
            and (not math.isfinite(value) or not value.is_integer())
        )
    ):
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TTPostError("invalid_request", "%s无效" % label, 400) from None
    if result < 0 or result > int(maximum):
        raise TTPostError("invalid_request", "%s无效" % label, 400)
    return result


def _publish_time(value: Any) -> str:
    text = str(value or "").strip()
    if not _PUBLISH_TIME_RE.fullmatch(text):
        raise TTPostError(
            "invalid_publish_time",
            "每日发布时间必须是严格的HH:MM格式",
            400,
        )
    return text


def _publish_times(value: Any) -> List[str]:
    if (
        isinstance(value, (str, bytes, bytearray, Mapping))
        or not isinstance(value, Iterable)
    ):
        raise TTPostError(
            "invalid_publish_times",
            "每日发布时间必须是列表",
            400,
        )
    raw_items = list(value)
    if len(raw_items) > 24:
        raise TTPostError(
            "invalid_publish_times",
            "每日发布时间最多24个",
            400,
        )
    normalized = [_publish_time(item) for item in raw_items]
    if len(set(normalized)) != len(normalized):
        raise TTPostError(
            "invalid_publish_times",
            "每日发布时间不能重复",
            400,
        )
    return sorted(normalized)


def _shanghai_date(value: Any) -> str:
    text = str(value or "").strip()
    if not _SHANGHAI_DATE_RE.fullmatch(text):
        raise TTPostError(
            "invalid_shanghai_date",
            "上海日期必须是严格的YYYY-MM-DD格式",
            400,
        )
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise TTPostError(
            "invalid_shanghai_date",
            "上海日期无效",
            400,
        ) from None
    if parsed.strftime("%Y-%m-%d") != text:
        raise TTPostError(
            "invalid_shanghai_date",
            "上海日期无效",
            400,
        )
    return text


def _scheduled_slot_utc(shanghai_date: str, publish_time: str) -> str:
    local_value = datetime.strptime(
        "%s %s" % (shanghai_date, publish_time),
        "%Y-%m-%d %H:%M",
    ).replace(tzinfo=BEIJING_TZ)
    return _iso_utc(local_value.astimezone(UTC), "排期UTC时间")


def _exact_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise TTPostError("invalid_request", "%s必须显式填写布尔值" % label, 400)
    return value


def _parse_datetime(
    value: Any,
    label: str,
    *,
    naive_timezone: timezone,
) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if not text:
            raise TTPostError("invalid_time", "%s不能为空" % label, 400)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            raise TTPostError("invalid_time", "%s格式无效" % label, 400) from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=naive_timezone)
    try:
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise TTPostError("invalid_time", "%s超出范围" % label, 400) from None


def _iso_utc(value: Any, label: str = "UTC时间") -> str:
    result = _parse_datetime(value, label, naive_timezone=UTC)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_datetime(value: Any, label: str = "UTC时间") -> datetime:
    return _parse_datetime(value, label, naive_timezone=UTC).replace(microsecond=0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def beijing_to_utc(value: Any) -> str:
    """Convert an explicit Beijing wall-clock value to canonical UTC."""

    result = _parse_datetime(value, "北京时间", naive_timezone=BEIJING_TZ)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _https_url(value: Any, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
        or any(ord(char) < 32 for char in text)
    ):
        raise TTPostError("invalid_request", "%s必须是安全HTTPS地址" % label, 400)
    return text


@dataclass(frozen=True)
class SafeAccount:
    """Metadata-only account DTO suitable for APIs and UI rendering."""

    account_id: str
    username: str
    display_name: str
    avatar_url: str
    status: str
    publish_eligible: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SafeAccount":
        if not isinstance(raw, Mapping):
            raise AccountSourceError(
                "tt_account_metadata_invalid",
                "TikTok账号元数据无效",
                503,
            )
        account_id = _account_id(raw.get("account_id"))
        username = _optional_text(raw.get("username"), "TikTok用户名", 128)
        display_name = _optional_text(
            raw.get("display_name") or username,
            "TikTok显示名称",
            255,
        )
        avatar_url = _https_url(
            raw.get("avatar_url"),
            "TikTok头像",
            allow_empty=True,
        )
        status = _required_text(raw.get("status"), "TikTok账号状态", 64)
        publish_eligible = _exact_bool(
            raw.get("publish_eligible"),
            "账号可发布状态",
        )
        return cls(
            account_id=account_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
            status=status,
            publish_eligible=publish_eligible,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "username": self.username,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "status": self.status,
            "publish_eligible": self.publish_eligible,
        }


class PublishCredentials:
    """Ephemeral credentials whose repr never exposes the access token."""

    __slots__ = ("account", "_access_token", "_active")

    def __init__(self, account: SafeAccount, access_token: str):
        self.account = account
        self._access_token = access_token
        self._active = True

    def reveal_access_token(self) -> str:
        if not self._active or not self._access_token:
            raise AccountSourceError(
                "tt_access_token_context_closed",
                "TikTok发布凭据上下文已关闭",
                409,
            )
        return self._access_token

    def close(self) -> None:
        self._access_token = ""
        self._active = False

    def __enter__(self) -> "PublishCredentials":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "PublishCredentials(account_id=%r, access_token=<redacted>, active=%r)"
            % (self.account.account_id, self._active)
        )


class SnapshotAccountSource:
    """Account source with physically separate metadata and token loaders.

    ``list_loader`` and ``account_loader`` must project metadata columns only.
    ``token_loader`` is invoked only by ``publish_credentials`` for one exact
    account ID and must return ``{"account_id": ..., "access_token": ...}``.
    """

    def __init__(
        self,
        list_loader: Callable[[], Iterable[Mapping[str, Any]]],
        account_loader: Callable[[str], Optional[Mapping[str, Any]]],
        token_loader: Callable[[str], Optional[Mapping[str, Any]]],
    ):
        if not all(callable(item) for item in (list_loader, account_loader, token_loader)):
            raise ValueError("account source loaders must be callable")
        self._list_loader = list_loader
        self._account_loader = account_loader
        self._token_loader = token_loader

    def list_safe_accounts(self) -> List[SafeAccount]:
        try:
            rows = self._list_loader()
            items = [SafeAccount.from_mapping(row) for row in rows]
        except TTPostError:
            raise
        except Exception:
            raise AccountSourceError(
                "tt_account_source_unavailable",
                "TikTok账号列表暂不可用",
                503,
            ) from None
        seen = set()
        for item in items:
            if item.account_id in seen:
                raise AccountSourceError(
                    "tt_account_metadata_ambiguous",
                    "TikTok账号元数据存在重复记录",
                    503,
                )
            seen.add(item.account_id)
        return items

    def get_safe_account(self, account_id: Any) -> SafeAccount:
        normalized = _account_id(account_id)
        try:
            raw = self._account_loader(normalized)
        except Exception:
            raise AccountSourceError(
                "tt_account_source_unavailable",
                "TikTok账号资料暂不可用",
                503,
            ) from None
        if raw is None:
            raise AccountSourceError(
                "tt_account_not_found",
                "TikTok账号不存在",
                404,
            )
        item = SafeAccount.from_mapping(raw)
        if not secrets.compare_digest(item.account_id, normalized):
            raise AccountSourceError(
                "tt_account_metadata_mismatch",
                "TikTok账号资料与请求账号不一致",
                409,
            )
        return item

    @contextlib.contextmanager
    def publish_credentials(
        self,
        account_id: Any,
    ) -> Iterator[PublishCredentials]:
        normalized = _account_id(account_id)
        account = self.get_safe_account(normalized)
        if account.status != "active" or not account.publish_eligible:
            raise AccountSourceError(
                "tt_account_not_publishable",
                "TikTok账号当前不可发布",
                409,
            )
        raw: Optional[Mapping[str, Any]] = None
        token = ""
        credentials: Optional[PublishCredentials] = None
        try:
            try:
                raw = self._token_loader(normalized)
            except Exception:
                raise AccountSourceError(
                    "tt_access_token_unavailable",
                    "TikTok发布凭据暂不可用",
                    503,
                ) from None
            if not isinstance(raw, Mapping):
                raise AccountSourceError(
                    "tt_access_token_missing",
                    "TikTok发布凭据缺失",
                    409,
                )
            token_account_id = _account_id(raw.get("account_id"))
            if not secrets.compare_digest(token_account_id, normalized):
                raise AccountSourceError(
                    "tt_access_token_account_mismatch",
                    "TikTok发布凭据与请求账号不一致",
                    409,
                )
            token_value = raw.get("access_token")
            if (
                not isinstance(token_value, str)
                or not token_value
                or len(token_value) > 16384
                or any(ord(char) < 32 for char in token_value)
            ):
                raise AccountSourceError(
                    "tt_access_token_missing",
                    "TikTok发布凭据缺失",
                    409,
                )
            token = token_value
            credentials = PublishCredentials(account, token)
            yield credentials
        finally:
            if credentials is not None:
                credentials.close()
            token = ""
            raw = None


@dataclass(frozen=True)
class MaterialResolution:
    material_id: str
    content_id: str
    media_url: str

    @classmethod
    def from_mapping(
        cls,
        material_id: Any,
        raw: Mapping[str, Any],
    ) -> "MaterialResolution":
        normalized_material_id = _material_id(material_id)
        if not isinstance(raw, Mapping):
            raise TTPostError(
                "tt_material_resolution_invalid",
                "素材解析结果无效",
                503,
            )
        returned_material_id = _material_id(
            raw.get("material_id", normalized_material_id)
        )
        if not secrets.compare_digest(returned_material_id, normalized_material_id):
            raise TTPostError(
                "tt_material_resolution_mismatch",
                "素材解析结果与请求素材不一致",
                409,
            )
        content_id = str(raw.get("content_id") or "").strip()
        if not _CONTENT_ID_RE.fullmatch(content_id):
            raise TTPostError(
                "tt_content_id_invalid",
                "素材对应的content_id无效",
                409,
            )
        media_url = _https_url(raw.get("media_url"), "素材视频地址")
        return cls(normalized_material_id, content_id, media_url)


def resolve_material(
    resolver: Any,
    material_id: Any,
) -> MaterialResolution:
    """Resolve one material through an injected callable or ``resolve`` method."""

    normalized = _material_id(material_id)
    try:
        if callable(resolver):
            raw = resolver(normalized)
        elif callable(getattr(resolver, "resolve", None)):
            raw = resolver.resolve(normalized)
        else:
            raise TypeError("resolver is not callable")
    except TTPostError:
        raise
    except Exception:
        raise TTPostError(
            "tt_material_resolver_unavailable",
            "素材解析服务暂不可用",
            503,
        ) from None
    if isinstance(raw, MaterialResolution):
        if not secrets.compare_digest(raw.material_id, normalized):
            raise TTPostError(
                "tt_material_resolution_mismatch",
                "素材解析结果与请求素材不一致",
                409,
            )
        return raw
    return MaterialResolution.from_mapping(normalized, raw)


def render_caption_template(
    template: Any,
    content_id: Any,
    *,
    url: Any = None,
    description: Any = None,
    defer_url: bool = False,
    defer_description: bool = False,
    max_chars: int = MAX_CAPTION_CHARS,
) -> str:
    """Render the user's caption at queue-freeze time.

    ``{{contect_id}}`` is supported exactly as supplied by the product owner.
    ``{{content_id}}`` is accepted as a correctly-spelled compatibility alias.
    ``{url}`` is replaced by the immutable per-queue TT short URL. Material
    intake may explicitly defer that one macro until the queue identity exists.
    ``{desc}`` is the normalized drama description frozen with the material.
    Unknown placeholders fail closed and internal line breaks are unchanged.
    """

    text = str(template or "")
    if not text.strip() or "\x00" in text or len(text) > 20000:
        raise TTPostError("invalid_caption_template", "发布描述模板无效", 400)
    normalized_content_id = str(content_id or "").strip()
    if not _CONTENT_ID_RE.fullmatch(normalized_content_id):
        raise TTPostError("tt_content_id_invalid", "content_id无效", 400)
    matches = list(_PLACEHOLDER_RE.finditer(text))
    placeholders = [match.group(1).strip() for match in matches]
    if not placeholders:
        raise TTPostError(
            "caption_content_id_required",
            "发布描述模板必须包含{{contect_id}}",
            400,
        )
    remainder = _PLACEHOLDER_RE.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        raise TTPostError(
            "caption_placeholder_invalid",
            "发布描述模板包含不完整占位符",
            400,
        )
    unknown = sorted(
        {name for name in placeholders if name not in {"contect_id", "content_id"}}
    )
    if unknown:
        raise TTPostError(
            "caption_placeholder_invalid",
            "发布描述模板包含未知占位符",
            400,
        )
    single_placeholders = [
        match.group(1)
        for match in _SINGLE_PLACEHOLDER_RE.finditer(text)
    ]
    single_remainder = _SINGLE_PLACEHOLDER_RE.sub("", remainder)
    if "{" in single_remainder or "}" in single_remainder:
        raise TTPostError(
            "caption_placeholder_invalid",
            "发布描述模板包含不完整占位符",
            400,
        )
    unknown_single = sorted(
        {name for name in single_placeholders if name not in {"url", "desc"}}
    )
    if unknown_single:
        raise TTPostError(
            "caption_placeholder_invalid",
            "发布描述模板包含未知占位符",
            400,
        )
    has_url = "url" in single_placeholders
    has_description = "desc" in single_placeholders
    normalized_url = ""
    if has_url and not defer_url:
        try:
            normalized_url = validate_short_url(url)
        except Exception:
            raise TTPostError(
                "caption_url_required",
                "发布描述模板中的{url}必须绑定有效TikTok短链",
                400,
            ) from None
    normalized_description = ""
    if has_description and not defer_description:
        try:
            normalized_description = _normalize_description(description)
        except ValueError:
            normalized_description = ""
        if not normalized_description:
            raise TTPostError(
                "caption_desc_required",
                "发布描述模板中的{desc}必须绑定有效剧描述",
                400,
            )
    rendered_base = _PLACEHOLDER_RE.sub(
        lambda match: normalized_content_id
        if match.group(1).strip() in {"contect_id", "content_id"}
        else match.group(0),
        text,
    )

    def render_single(match: re.Match, *, measuring: bool) -> str:
        name = match.group(1)
        if name == "url":
            if defer_url:
                return _MAX_TT_SHORT_URL if measuring else match.group(0)
            return normalized_url
        if name == "desc":
            if defer_description:
                return match.group(0)
            return normalized_description
        return match.group(0)

    rendered = _SINGLE_PLACEHOLDER_RE.sub(
        lambda match: render_single(match, measuring=False),
        rendered_base,
    ).strip()
    measured = _SINGLE_PLACEHOLDER_RE.sub(
        lambda match: render_single(match, measuring=True),
        rendered_base,
    ).strip()
    try:
        rendered_units = len(measured.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        rendered_units = int(max_chars) + 1
    if not rendered or rendered_units > int(max_chars):
        raise TTPostError(
            "caption_length_invalid",
            "发布描述渲染后为空或超过长度限制",
            400,
        )
    return rendered


def caption_uses_url_macro(template: Any) -> bool:
    """Return whether the exact supported single-brace URL macro is present."""

    return any(
        match.group(1) == "url"
        for match in _SINGLE_PLACEHOLDER_RE.finditer(str(template or ""))
    )


def caption_uses_desc_macro(template: Any) -> bool:
    """Return whether the exact supported drama-description macro is present."""

    return any(
        match.group(1) == "desc"
        for match in _SINGLE_PLACEHOLDER_RE.finditer(str(template or ""))
    )


def render_fixed_caption(content_id: Any) -> str:
    """Render the one product-approved caption for a resolved drama."""

    return render_caption_template(FIXED_CAPTION_TEMPLATE, content_id)


@dataclass(frozen=True)
class TTPostAccountSettings:
    """Account-level defaults frozen into each new TikTok Post queue item."""

    privacy_level: str
    allow_comment: bool
    allow_duet: bool
    allow_stitch: bool
    brand_content_toggle: bool
    brand_organic_toggle: bool
    is_aigc: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TTPostAccountSettings":
        if not isinstance(raw, Mapping):
            raise TTPostError("invalid_account_settings", "个号发布设置必须是对象", 400)
        required = {
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "brand_content_toggle",
            "brand_organic_toggle",
            "is_aigc",
        }
        missing = sorted(required.difference(raw))
        unknown = sorted(set(raw).difference(required))
        if missing or unknown:
            raise TTPostError(
                "invalid_account_settings",
                "个号发布设置字段不完整或包含未知字段",
                400,
            )
        privacy_level = str(raw.get("privacy_level") or "").strip()
        if privacy_level not in PRIVACY_LEVELS:
            raise TTPostError("invalid_privacy_level", "隐私级别无效", 400)
        return cls(
            privacy_level=privacy_level,
            allow_comment=_exact_bool(raw.get("allow_comment"), "评论开关"),
            allow_duet=_exact_bool(raw.get("allow_duet"), "合拍开关"),
            allow_stitch=_exact_bool(raw.get("allow_stitch"), "拼接开关"),
            brand_content_toggle=_exact_bool(
                raw.get("brand_content_toggle"),
                "品牌内容开关",
            ),
            brand_organic_toggle=_exact_bool(
                raw.get("brand_organic_toggle"),
                "自有品牌开关",
            ),
            is_aigc=_exact_bool(raw.get("is_aigc"), "AI内容声明"),
        )

    @property
    def commercial_disclosure(self) -> bool:
        return bool(self.brand_content_toggle or self.brand_organic_toggle)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "privacy_level": self.privacy_level,
            "allow_comment": self.allow_comment,
            "allow_duet": self.allow_duet,
            "allow_stitch": self.allow_stitch,
            "brand_content_toggle": self.brand_content_toggle,
            "brand_organic_toggle": self.brand_organic_toggle,
            "commercial_disclosure": self.commercial_disclosure,
            "is_aigc": self.is_aigc,
        }


@dataclass(frozen=True)
class TTPostPolicy:
    """Explicit TikTok privacy, interaction, disclosure and consent settings."""

    privacy_level: str
    allow_comment: bool
    allow_duet: bool
    allow_stitch: bool
    brand_content_toggle: bool
    brand_organic_toggle: bool
    user_consent: bool
    consent_version: str
    consented_at_utc: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TTPostPolicy":
        if not isinstance(raw, Mapping):
            raise TTPostError("invalid_post_policy", "发布策略必须是对象", 400)
        required = {
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "brand_content_toggle",
            "brand_organic_toggle",
            "user_consent",
            "consent_version",
            "consented_at",
        }
        missing = sorted(required.difference(raw))
        unknown = sorted(set(raw).difference(required))
        if missing or unknown:
            raise TTPostError(
                "invalid_post_policy",
                "发布策略字段不完整或包含未知字段",
                400,
            )
        privacy_level = str(raw.get("privacy_level") or "").strip()
        if privacy_level not in PRIVACY_LEVELS:
            raise TTPostError("invalid_privacy_level", "隐私级别无效", 400)
        allow_comment = _exact_bool(raw.get("allow_comment"), "评论开关")
        allow_duet = _exact_bool(raw.get("allow_duet"), "合拍开关")
        allow_stitch = _exact_bool(raw.get("allow_stitch"), "拼接开关")
        brand_content_toggle = _exact_bool(
            raw.get("brand_content_toggle"),
            "品牌内容开关",
        )
        brand_organic_toggle = _exact_bool(
            raw.get("brand_organic_toggle"),
            "自有品牌开关",
        )
        user_consent = _exact_bool(raw.get("user_consent"), "用户授权")
        if not user_consent:
            raise TTPostError(
                "tt_post_consent_required",
                "必须取得并显式记录用户授权后才能排期",
                409,
            )
        consent_version = _required_text(
            raw.get("consent_version"),
            "授权版本",
            64,
        )
        consented_at_utc = beijing_to_utc(raw.get("consented_at"))
        return cls(
            privacy_level=privacy_level,
            allow_comment=allow_comment,
            allow_duet=allow_duet,
            allow_stitch=allow_stitch,
            brand_content_toggle=brand_content_toggle,
            brand_organic_toggle=brand_organic_toggle,
            user_consent=user_consent,
            consent_version=consent_version,
            consented_at_utc=consented_at_utc,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "privacy_level": self.privacy_level,
            "allow_comment": self.allow_comment,
            "allow_duet": self.allow_duet,
            "allow_stitch": self.allow_stitch,
            "brand_content_toggle": self.brand_content_toggle,
            "brand_organic_toggle": self.brand_organic_toggle,
            "user_consent": self.user_consent,
            "consent_version": self.consent_version,
            "consented_at_utc": self.consented_at_utc,
        }


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class LiveGates:
    """Three independent production gates; every gate defaults closed."""

    live_enabled: bool = False
    direct_audit_approved: bool = False
    url_property_verified: bool = False

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "LiveGates":
        source = os.environ if environ is None else environ
        return cls(
            live_enabled=_env_flag(source, "TT_POST_LIVE_ENABLED"),
            direct_audit_approved=_env_flag(
                source,
                "TT_POST_DIRECT_AUDIT_APPROVED",
            ),
            url_property_verified=_env_flag(
                source,
                "TT_POST_URL_PROPERTY_VERIFIED",
            ),
        )

    @property
    def is_open(self) -> bool:
        return bool(
            self.live_enabled
            and self.direct_audit_approved
            and self.url_property_verified
        )

    def as_dict(self) -> Dict[str, bool]:
        return {
            "live_enabled": bool(self.live_enabled),
            "direct_audit_approved": bool(self.direct_audit_approved),
            "url_property_verified": bool(self.url_property_verified),
        }

    def assert_open(self) -> None:
        closed = [
            name
            for name, enabled in (
                ("live", self.live_enabled),
                ("direct_audit", self.direct_audit_approved),
                ("url_property", self.url_property_verified),
            )
            if not enabled
        ]
        if closed:
            raise TTPostError(
                "tt_post_live_gate_closed",
                "TikTok正式发布门禁未全部开启: %s" % ",".join(closed),
                409,
            )


class QueueClaim:
    """Lease claim wrapper with a redacted claim token."""

    __slots__ = ("queue", "_claim_token")

    def __init__(self, queue: Mapping[str, Any], claim_token: str):
        self.queue = dict(queue)
        self._claim_token = claim_token

    @property
    def queue_id(self) -> int:
        return int(self.queue["id"])

    def reveal_claim_token(self) -> str:
        if not self._claim_token:
            raise TTPostError(
                "tt_post_claim_closed",
                "发布认领凭据已关闭",
                409,
            )
        return self._claim_token

    def __repr__(self) -> str:
        return "QueueClaim(queue_id=%r, claim_token=<redacted>)" % self.queue_id


class RecurringExecutionClaim:
    """Per-run execution lease whose fencing token never enters public DTOs."""

    __slots__ = ("run", "_execution_token")

    def __init__(self, run: Mapping[str, Any], execution_token: str):
        self.run = dict(run)
        self._execution_token = execution_token

    @property
    def run_id(self) -> int:
        return int(self.run["id"])

    def reveal_execution_token(self) -> str:
        if not self._execution_token:
            raise TTPostError(
                "tt_post_recurring_execution_closed",
                "每日发布运行执行租约已关闭",
                409,
            )
        return self._execution_token

    def __repr__(self) -> str:
        return (
            "RecurringExecutionClaim("
            "run_id=%r, execution_token=<redacted>)"
        ) % self.run_id


class MaterialIntakeClaim:
    """Preparation lease wrapper whose fencing token stays out of public DTOs."""

    __slots__ = ("item", "_claim_token")

    def __init__(self, item: Mapping[str, Any], claim_token: str):
        self.item = dict(item)
        self._claim_token = str(claim_token)

    @property
    def intake_id(self) -> int:
        return int(self.item["id"])

    def reveal_claim_token(self) -> str:
        if not self._claim_token:
            raise TTPostError(
                "tt_post_material_intake_claim_closed",
                "素材预制作认领凭据已关闭",
                409,
            )
        return self._claim_token

    def __repr__(self) -> str:
        return (
            "MaterialIntakeClaim("
            "intake_id=%r, claim_token=<redacted>)"
        ) % self.intake_id


class DirectTestClaim:
    """Direct-test lease wrapper whose fencing token stays private."""

    __slots__ = ("item", "_claim_token")

    def __init__(self, item: Mapping[str, Any], claim_token: str):
        self.item = dict(item)
        self._claim_token = str(claim_token)

    @property
    def direct_test_id(self) -> int:
        return int(self.item["id"])

    def reveal_claim_token(self) -> str:
        if not self._claim_token:
            raise TTPostError(
                "tt_post_direct_test_claim_closed",
                "立即测试发布认领凭据已关闭",
                409,
            )
        return self._claim_token

    def __repr__(self) -> str:
        return (
            "DirectTestClaim("
            "direct_test_id=%r, claim_token=<redacted>)"
        ) % self.direct_test_id


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_storage(db_path: Any) -> None:
    """Create the legacy ledger plus the additive recurring-publish tables."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tt_post_material_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','reserved','published','canceled')),
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tt_post_account_setting (
                    account_id TEXT PRIMARY KEY,
                    privacy_level TEXT NOT NULL
                        CHECK(privacy_level IN (
                            'PUBLIC_TO_EVERYONE',
                            'MUTUAL_FOLLOW_FRIENDS',
                            'FOLLOWER_OF_CREATOR',
                            'SELF_ONLY'
                        )),
                    allow_comment INTEGER NOT NULL
                        CHECK(allow_comment IN (0,1)),
                    allow_duet INTEGER NOT NULL
                        CHECK(allow_duet IN (0,1)),
                    allow_stitch INTEGER NOT NULL
                        CHECK(allow_stitch IN (0,1)),
                    brand_content_toggle INTEGER NOT NULL
                        CHECK(brand_content_toggle IN (0,1)),
                    brand_organic_toggle INTEGER NOT NULL
                        CHECK(brand_organic_toggle IN (0,1)),
                    is_aigc INTEGER NOT NULL CHECK(is_aigc IN (0,1)),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tt_post_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    pool_item_id INTEGER NOT NULL UNIQUE,
                    material_id TEXT NOT NULL UNIQUE,
                    content_id TEXT NOT NULL,
                    media_url TEXT NOT NULL,
                    source_media_url TEXT NOT NULL DEFAULT '',
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL,
                    account_username TEXT NOT NULL DEFAULT '',
                    account_display_name TEXT NOT NULL DEFAULT '',
                    creator_nickname_snapshot TEXT NOT NULL DEFAULT '',
                    creator_username_snapshot TEXT NOT NULL DEFAULT '',
                    creator_info_hash TEXT NOT NULL DEFAULT '',
                    creator_info_synced_at_utc TEXT NOT NULL DEFAULT '',
                    scheduled_at_utc TEXT NOT NULL,
                    caption_template TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    short_link_id INTEGER NOT NULL DEFAULT 0
                        CHECK(short_link_id>=0),
                    short_url TEXT NOT NULL DEFAULT '',
                    long_url TEXT NOT NULL DEFAULT '',
                    privacy_level TEXT NOT NULL,
                    allow_comment INTEGER NOT NULL CHECK(allow_comment IN (0,1)),
                    allow_duet INTEGER NOT NULL CHECK(allow_duet IN (0,1)),
                    allow_stitch INTEGER NOT NULL CHECK(allow_stitch IN (0,1)),
                    brand_content_toggle INTEGER NOT NULL
                        CHECK(brand_content_toggle IN (0,1)),
                    brand_organic_toggle INTEGER NOT NULL
                        CHECK(brand_organic_toggle IN (0,1)),
                    user_consent INTEGER NOT NULL CHECK(user_consent=1),
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    is_aigc INTEGER NOT NULL DEFAULT 0 CHECK(is_aigc IN (0,1)),
                    publish_mode TEXT NOT NULL DEFAULT 'hold'
                        CHECK(publish_mode IN ('hold','direct_post')),
                    gpu_job_id TEXT NOT NULL DEFAULT '',
                    prepared_output_sha256 TEXT NOT NULL DEFAULT '',
                    prepared_output_size INTEGER NOT NULL DEFAULT 0
                        CHECK(prepared_output_size>=0),
                    prepared_duration_sec REAL NOT NULL DEFAULT 0
                        CHECK(prepared_duration_sec>=0),
                    source_trim_tail_seconds REAL NOT NULL DEFAULT 0
                        CHECK(source_trim_tail_seconds>=0),
                    status TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK(status IN (
                            'scheduled','claimed','publishing','reconciling',
                            'published','failed','canceled','missed',
                            'blocked_compliance','unknown'
                        )),
                    claim_worker TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    publish_id TEXT NOT NULL DEFAULT '',
                    publish_url TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    unknown_outcome INTEGER NOT NULL DEFAULT 0
                        CHECK(unknown_outcome IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(pool_item_id) REFERENCES tt_post_material_pool(id),
                    UNIQUE(account_id,scheduled_at_utc)
                );

                CREATE TABLE IF NOT EXISTS tt_post_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER,
                    pool_item_id INTEGER,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(queue_id) REFERENCES tt_post_queue(id),
                    FOREIGN KEY(pool_item_id) REFERENCES tt_post_material_pool(id)
                );

                CREATE TABLE IF NOT EXISTS tt_post_daily_schedule (
                    account_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(enabled IN (0,1)),
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
                    user_consent INTEGER NOT NULL CHECK(user_consent=1),
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tt_post_auto_publish_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    version INTEGER NOT NULL CHECK(version>0),
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(enabled IN (0,1)),
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    account_ids_json TEXT NOT NULL DEFAULT '[]',
                    caption_template TEXT NOT NULL,
                    user_consent INTEGER NOT NULL DEFAULT 0
                        CHECK(user_consent IN (0,1)),
                    consent_version TEXT NOT NULL DEFAULT '',
                    consented_at_utc TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tt_post_recurring_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    source_media_url TEXT NOT NULL,
                    prepared_media_url TEXT NOT NULL,
                    gpu_job_id TEXT NOT NULL,
                    prepared_output_sha256 TEXT NOT NULL,
                    prepared_output_size INTEGER NOT NULL
                        CHECK(prepared_output_size>0),
                    prepared_duration_sec REAL NOT NULL
                        CHECK(prepared_duration_sec>0),
                    source_trim_tail_seconds REAL NOT NULL DEFAULT 0
                        CHECK(source_trim_tail_seconds>=0),
                    preparation_profile TEXT NOT NULL,
                    caption_template TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    is_aigc INTEGER NOT NULL DEFAULT 0
                        CHECK(is_aigc IN (0,1)),
                    user_consent INTEGER NOT NULL CHECK(user_consent=1),
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN (
                            'available','reserved','consumed','canceled'
                        )),
                    run_id INTEGER,
                    queue_id INTEGER,
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reserved_at_utc TEXT NOT NULL DEFAULT '',
                    consumed_at_utc TEXT NOT NULL DEFAULT '',
                    canceled_at_utc TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES tt_post_schedule_run(id),
                    FOREIGN KEY(queue_id) REFERENCES tt_post_queue(id)
                );

                CREATE TABLE IF NOT EXISTS tt_post_material_intake (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    material_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    source_media_url TEXT NOT NULL,
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    gpu_job_id TEXT NOT NULL UNIQUE,
                    source_trim_tail_seconds REAL NOT NULL DEFAULT 0
                        CHECK(source_trim_tail_seconds>=0),
                    preparation_profile TEXT NOT NULL,
                    caption_template TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    is_aigc INTEGER NOT NULL DEFAULT 0
                        CHECK(is_aigc IN (0,1)),
                    user_consent INTEGER NOT NULL DEFAULT 1
                        CHECK(user_consent=1),
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN (
                            'queued','preparing','retry_wait','ready',
                            'failed','canceled'
                        )),
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(attempt_count>=0),
                    next_attempt_at_utc TEXT NOT NULL DEFAULT '',
                    claim_worker TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                    prepared_media_url TEXT NOT NULL DEFAULT '',
                    prepared_output_sha256 TEXT NOT NULL DEFAULT '',
                    prepared_output_size INTEGER NOT NULL DEFAULT 0
                        CHECK(prepared_output_size>=0),
                    prepared_duration_sec REAL NOT NULL DEFAULT 0
                        CHECK(prepared_duration_sec>=0),
                    recurring_pool_id INTEGER UNIQUE,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at_utc TEXT NOT NULL DEFAULT '',
                    ready_at_utc TEXT NOT NULL DEFAULT '',
                    failed_at_utc TEXT NOT NULL DEFAULT '',
                    canceled_at_utc TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(recurring_pool_id)
                        REFERENCES tt_post_recurring_pool(id),
                    CHECK(
                        (status='ready' AND recurring_pool_id IS NOT NULL)
                        OR
                        (status<>'ready' AND recurring_pool_id IS NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS tt_post_direct_test (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    source_media_url TEXT NOT NULL,
                    prepared_media_url TEXT NOT NULL DEFAULT '',
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    account_username TEXT NOT NULL DEFAULT '',
                    account_display_name TEXT NOT NULL DEFAULT '',
                    creator_nickname_snapshot TEXT NOT NULL DEFAULT '',
                    creator_username_snapshot TEXT NOT NULL DEFAULT '',
                    creator_info_hash TEXT NOT NULL DEFAULT '',
                    creator_info_synced_at_utc TEXT NOT NULL DEFAULT '',
                    gpu_job_id TEXT NOT NULL UNIQUE,
                    source_trim_tail_seconds REAL NOT NULL DEFAULT 0
                        CHECK(source_trim_tail_seconds>=0),
                    preparation_profile TEXT NOT NULL,
                    prepared_output_sha256 TEXT NOT NULL DEFAULT '',
                    prepared_output_size INTEGER NOT NULL DEFAULT 0
                        CHECK(prepared_output_size>=0),
                    prepared_duration_sec REAL NOT NULL DEFAULT 0
                        CHECK(prepared_duration_sec>=0),
                    caption_template TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    short_link_id INTEGER NOT NULL DEFAULT 0
                        CHECK(short_link_id>=0),
                    short_url TEXT NOT NULL DEFAULT '',
                    long_url TEXT NOT NULL DEFAULT '',
                    privacy_level TEXT NOT NULL
                        CHECK(privacy_level IN (
                            'PUBLIC_TO_EVERYONE',
                            'MUTUAL_FOLLOW_FRIENDS',
                            'FOLLOWER_OF_CREATOR',
                            'SELF_ONLY'
                        )),
                    allow_comment INTEGER NOT NULL
                        CHECK(allow_comment IN (0,1)),
                    allow_duet INTEGER NOT NULL
                        CHECK(allow_duet IN (0,1)),
                    allow_stitch INTEGER NOT NULL
                        CHECK(allow_stitch IN (0,1)),
                    brand_content_toggle INTEGER NOT NULL
                        CHECK(brand_content_toggle IN (0,1)),
                    brand_organic_toggle INTEGER NOT NULL
                        CHECK(brand_organic_toggle IN (0,1)),
                    is_aigc INTEGER NOT NULL DEFAULT 0
                        CHECK(is_aigc IN (0,1)),
                    user_consent INTEGER NOT NULL CHECK(user_consent=1),
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    config_version INTEGER NOT NULL DEFAULT 0
                        CHECK(config_version>=0),
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN (
                            'queued','preparing','ready','publishing',
                            'reconciling','published','failed','unknown',
                            'canceled'
                        )),
                    preparation_attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(preparation_attempt_count>=0),
                    publish_attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(publish_attempt_count>=0),
                    claim_phase TEXT NOT NULL DEFAULT ''
                        CHECK(claim_phase IN ('','prepare','publish')),
                    claim_worker TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                    publish_id TEXT NOT NULL DEFAULT '',
                    publish_url TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    unknown_outcome INTEGER NOT NULL DEFAULT 0
                        CHECK(unknown_outcome IN (0,1)),
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at_utc TEXT NOT NULL DEFAULT '',
                    prepared_at_utc TEXT NOT NULL DEFAULT '',
                    publish_started_at_utc TEXT NOT NULL DEFAULT '',
                    published_at_utc TEXT NOT NULL DEFAULT '',
                    failed_at_utc TEXT NOT NULL DEFAULT '',
                    canceled_at_utc TEXT NOT NULL DEFAULT '',
                    CHECK(
                        (status='preparing' AND claim_phase='prepare')
                        OR
                        (status='publishing' AND claim_phase='publish')
                        OR
                        (
                            status NOT IN ('preparing','publishing')
                            AND claim_phase=''
                        )
                    ),
                    CHECK(
                        (
                            short_link_id=0 AND short_url='' AND long_url=''
                        )
                        OR
                        (short_link_id>0 AND short_url<>'')
                    )
                );

                CREATE TABLE IF NOT EXISTS tt_post_schedule_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL UNIQUE,
                    trigger_type TEXT NOT NULL
                        CHECK(trigger_type IN ('auto','manual')),
                    account_id TEXT NOT NULL,
                    shanghai_date TEXT NOT NULL,
                    publish_time TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    config_version INTEGER NOT NULL DEFAULT 0
                        CHECK(config_version>=0),
                    manual_request_key TEXT NOT NULL DEFAULT '',
                    pool_item_id INTEGER NOT NULL,
                    queue_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'claimed'
                        CHECK(status IN (
                            'claimed','preflight_failed','scheduled',
                            'publishing','reconciling','published','failed',
                            'canceled','missed','blocked_compliance','unknown'
                        )),
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at_utc TEXT NOT NULL,
                    bound_at_utc TEXT NOT NULL DEFAULT '',
                    finished_at_utc TEXT NOT NULL DEFAULT '',
                    execution_token TEXT NOT NULL DEFAULT '',
                    execution_lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(pool_item_id)
                        REFERENCES tt_post_recurring_pool(id),
                    FOREIGN KEY(queue_id) REFERENCES tt_post_queue(id)
                );

                CREATE INDEX IF NOT EXISTS idx_tt_post_queue_due
                    ON tt_post_queue(status,scheduled_at_utc,id);
                CREATE INDEX IF NOT EXISTS idx_tt_post_queue_lease
                    ON tt_post_queue(status,lease_expires_at_utc,id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_queue_publish_id
                    ON tt_post_queue(publish_id) WHERE publish_id<>'';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_queue_gpu_job
                    ON tt_post_queue(gpu_job_id) WHERE gpu_job_id<>'';
                CREATE INDEX IF NOT EXISTS idx_tt_post_event_queue
                    ON tt_post_event(queue_id,id);
                CREATE INDEX IF NOT EXISTS idx_tt_post_recurring_pool_fifo
                    ON tt_post_recurring_pool(account_id,status,created_at,id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_recurring_pool_run
                    ON tt_post_recurring_pool(run_id) WHERE run_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_recurring_pool_queue
                    ON tt_post_recurring_pool(queue_id) WHERE queue_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_recurring_pool_gpu_job
                    ON tt_post_recurring_pool(gpu_job_id);
                CREATE INDEX IF NOT EXISTS idx_tt_post_material_intake_due
                    ON tt_post_material_intake(
                        status,next_attempt_at_utc,lease_expires_at_utc,
                        created_at,id
                    );
                CREATE INDEX IF NOT EXISTS idx_tt_post_material_intake_account
                    ON tt_post_material_intake(account_id,status,created_at,id);
                CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_prepare
                    ON tt_post_direct_test(
                        status,claim_phase,lease_expires_at_utc,created_at,id
                    );
                CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_publish
                    ON tt_post_direct_test(
                        status,claim_phase,lease_expires_at_utc,
                        prepared_at_utc,id
                    );
                CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_material
                    ON tt_post_direct_test(material_id,status,updated_at,id);
                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_tt_post_direct_test_active_material
                    ON tt_post_direct_test(material_id)
                    WHERE status IN (
                        'queued','preparing','ready','publishing',
                        'reconciling','unknown'
                    );
                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_tt_post_direct_test_publish_id
                    ON tt_post_direct_test(publish_id)
                    WHERE publish_id<>'';
                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_tt_post_direct_test_short_link
                    ON tt_post_direct_test(short_link_id)
                    WHERE short_link_id>0;
                CREATE INDEX IF NOT EXISTS idx_tt_post_schedule_run_account
                    ON tt_post_schedule_run(account_id,status,scheduled_at_utc,id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_schedule_run_manual
                    ON tt_post_schedule_run(manual_request_key)
                    WHERE manual_request_key<>'';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_schedule_run_auto_slot
                    ON tt_post_schedule_run(
                        account_id,shanghai_date,publish_time
                    ) WHERE trigger_type='auto';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_schedule_run_queue
                    ON tt_post_schedule_run(queue_id) WHERE queue_id IS NOT NULL;
                """
            )
            schedule_run_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(tt_post_schedule_run)"
                ).fetchall()
            }
            if "execution_token" not in schedule_run_columns:
                conn.execute(
                    """
                    ALTER TABLE tt_post_schedule_run
                    ADD COLUMN execution_token TEXT NOT NULL DEFAULT ''
                    """
                )
            if "execution_lease_expires_at_utc" not in schedule_run_columns:
                conn.execute(
                    """
                    ALTER TABLE tt_post_schedule_run
                    ADD COLUMN execution_lease_expires_at_utc
                        TEXT NOT NULL DEFAULT ''
                    """
                )
            additive_columns = {
                "tt_post_material_intake": {
                    "material_tag": "TEXT NOT NULL DEFAULT ''",
                },
                "tt_post_recurring_pool": {
                    "material_name": "TEXT NOT NULL DEFAULT ''",
                    "drama_name": "TEXT NOT NULL DEFAULT ''",
                    "material_language": "TEXT NOT NULL DEFAULT ''",
                    "material_tag": "TEXT NOT NULL DEFAULT ''",
                    "description": "TEXT NOT NULL DEFAULT ''",
                },
                "tt_post_queue": {
                    "material_name": "TEXT NOT NULL DEFAULT ''",
                    "drama_name": "TEXT NOT NULL DEFAULT ''",
                    "material_language": "TEXT NOT NULL DEFAULT ''",
                    "material_tag": "TEXT NOT NULL DEFAULT ''",
                    "description": "TEXT NOT NULL DEFAULT ''",
                    "short_link_id": "INTEGER NOT NULL DEFAULT 0",
                    "short_url": "TEXT NOT NULL DEFAULT ''",
                    "long_url": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table_name, definitions in additive_columns.items():
                existing_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(%s)" % table_name
                    ).fetchall()
                }
                for column_name, definition in definitions.items():
                    if column_name not in existing_columns:
                        conn.execute(
                            "ALTER TABLE %s ADD COLUMN %s %s"
                            % (table_name, column_name, definition)
                        )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_tt_post_schedule_run_recovery
                ON tt_post_schedule_run(
                    status,queue_id,execution_lease_expires_at_utc,
                    scheduled_at_utc,id
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_queue_short_link
                ON tt_post_queue(short_link_id) WHERE short_link_id>0
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _safe_event_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, 1000)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower().replace("-", "_")
            if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
                raise TTPostError(
                    "sensitive_event_payload",
                    "事件详情禁止包含凭据字段",
                    500,
                )
            result[key] = _safe_event_value(raw_value)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_event_value(item) for item in value]
    return redact_text(value, 1000)


def _public_queue(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result.pop("claim_token", None)
    for field in (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "brand_content_toggle",
        "brand_organic_toggle",
        "user_consent",
        "is_aigc",
        "unknown_outcome",
    ):
        result[field] = bool(result.get(field))
    return result


def _public_account_settings(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    for field in (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "brand_content_toggle",
        "brand_organic_toggle",
        "is_aigc",
    ):
        result[field] = bool(result.get(field))
    result["commercial_disclosure"] = bool(
        result.get("brand_content_toggle")
        or result.get("brand_organic_toggle")
    )
    result["configured"] = True
    return result


def _public_daily_schedule(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    result["user_consent"] = bool(result.get("user_consent"))
    try:
        result["publish_times"] = json.loads(
            str(result.pop("publish_times_json", "[]"))
        )
    except (TypeError, ValueError):
        raise TTPostError(
            "tt_post_schedule_storage_invalid",
            "每日发布排期存储内容无效",
            500,
        ) from None
    if not isinstance(result["publish_times"], list):
        raise TTPostError(
            "tt_post_schedule_storage_invalid",
            "每日发布排期存储内容无效",
            500,
        )
    return result


def _default_daily_schedule(account_id: str) -> Dict[str, Any]:
    return {
        "account_id": account_id,
        "enabled": False,
        "timezone": "Asia/Shanghai",
        "publish_times": [],
        "version": 0,
        "user_consent": False,
        "consent_version": "",
        "consented_at_utc": "",
        "created_by_user_id": "",
        "created_by_name": "",
        "updated_by_user_id": "",
        "updated_by_name": "",
        "created_at": "",
        "updated_at": "",
    }


def _public_auto_publish_config(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result.pop("id", None)
    result["enabled"] = bool(result.get("enabled"))
    result["user_consent"] = bool(result.get("user_consent"))
    for source, target in (
        ("publish_times_json", "publish_times"),
        ("account_ids_json", "account_ids"),
    ):
        try:
            parsed = json.loads(str(result.pop(source, "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise TTPostError(
                "tt_post_auto_config_storage_invalid",
                "自动发布配置存储内容无效",
                500,
            ) from None
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise TTPostError(
                "tt_post_auto_config_storage_invalid",
                "自动发布配置存储内容无效",
                500,
            )
        result[target] = parsed
    result["legacy_review_required"] = False
    result["legacy_schedule_mode"] = "atomic"
    result["legacy_publish_times_by_account"] = {}
    result["legacy_membership_mode"] = "atomic"
    return result


def _public_recurring_pool(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result["is_aigc"] = bool(result.get("is_aigc"))
    result["user_consent"] = bool(result.get("user_consent"))
    return result


def _public_material_intake(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result.pop("claim_token", None)
    result.pop("lease_expires_at_utc", None)
    result["is_aigc"] = bool(result.get("is_aigc"))
    result["user_consent"] = bool(result.get("user_consent"))
    return result


def _public_direct_test(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result.pop("claim_token", None)
    result.pop("lease_expires_at_utc", None)
    for field in (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "brand_content_toggle",
        "brand_organic_toggle",
        "is_aigc",
        "user_consent",
        "unknown_outcome",
    ):
        result[field] = bool(result.get(field))
    result["commercial_disclosure"] = bool(
        result.get("brand_content_toggle")
        or result.get("brand_organic_toggle")
    )
    return result


def _public_schedule_run(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result.pop("execution_token", None)
    result.pop("execution_lease_expires_at_utc", None)
    return result


class TTPostStore:
    """Transactional TikTok Post material pool and queue ledger."""

    def __init__(
        self,
        db_path: Any,
        *,
        now_fn: Callable[[], datetime] = utc_now,
    ):
        self.db_path = str(db_path)
        self._now_fn = now_fn
        ensure_storage(self.db_path)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _now_iso(self) -> str:
        return _iso_utc(self._now_fn(), "当前时间")

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        event_type: str,
        created_at: str,
        queue_id: Optional[int] = None,
        pool_item_id: Optional[int] = None,
        from_status: str = "",
        to_status: str = "",
        message: str = "",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        event_type = _required_text(event_type, "事件类型", 96)
        safe_details = _safe_event_value(dict(details or {}))
        conn.execute(
            """
            INSERT INTO tt_post_event(
                queue_id,pool_item_id,event_type,from_status,to_status,
                message,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                queue_id,
                pool_item_id,
                event_type,
                str(from_status or "")[:32],
                str(to_status or "")[:32],
                redact_text(message),
                json.dumps(
                    safe_details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at,
            ),
        )

    def get_account_settings(
        self,
        account_id: Any,
        *,
        required: bool = False,
    ) -> Optional[Dict[str, Any]]:
        normalized = _account_id(account_id)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_account_setting WHERE account_id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            if required:
                raise TTPostError(
                    "tt_account_settings_required",
                    "请先在TT个号管理中完成该账号的发布设置",
                    409,
                )
            return None
        return _public_account_settings(row)

    def list_account_settings(self) -> List[Dict[str, Any]]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tt_post_account_setting ORDER BY account_id"
            ).fetchall()
        return [_public_account_settings(row) for row in rows]

    def save_account_settings(
        self,
        account_id: Any,
        settings: Any,
        *,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.save_account_settings_batch(
            [
                {
                    "account_id": account_id,
                    "settings": settings,
                    "expected_version": expected_version,
                }
            ]
        )[0]

    def save_account_settings_batch(
        self,
        updates: Any,
    ) -> List[Dict[str, Any]]:
        if (
            isinstance(updates, (str, bytes, bytearray, Mapping))
            or not isinstance(updates, Iterable)
        ):
            raise TTPostError(
                "invalid_batch_targets",
                "批量个号目标必须是列表",
                400,
            )
        raw_updates = list(updates)
        if (
            not raw_updates
            or len(raw_updates) > MAX_ACCOUNT_SETTINGS_BATCH
        ):
            raise TTPostError(
                "invalid_batch_targets",
                "批量个号数量必须在1到%d之间"
                % MAX_ACCOUNT_SETTINGS_BATCH,
                400,
            )

        normalized_updates = []
        seen_account_ids = set()
        for raw_update in raw_updates:
            if not isinstance(raw_update, Mapping) or set(raw_update) != {
                "account_id",
                "settings",
                "expected_version",
            }:
                raise TTPostError(
                    "invalid_batch_targets",
                    "批量个号目标字段无效",
                    400,
                )
            normalized_account_id = _account_id(raw_update.get("account_id"))
            if normalized_account_id in seen_account_ids:
                raise TTPostError(
                    "invalid_batch_targets",
                    "批量个号目标不能重复",
                    400,
                )
            seen_account_ids.add(normalized_account_id)
            raw_settings = raw_update.get("settings")
            normalized_settings = (
                raw_settings
                if isinstance(raw_settings, TTPostAccountSettings)
                else TTPostAccountSettings.from_mapping(raw_settings)
            )
            expected_version = raw_update.get("expected_version")
            if expected_version is not None and (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 0
            ):
                raise TTPostError(
                    "invalid_account_settings_version",
                    "个号发布设置版本无效",
                    400,
                )
            normalized_updates.append(
                (
                    normalized_account_id,
                    normalized_settings,
                    expected_version,
                )
            )

        timestamp = self._now_iso()
        with self._transaction() as conn:
            current_rows = {}
            for normalized_account_id, _, expected_version in normalized_updates:
                current = conn.execute(
                    "SELECT * FROM tt_post_account_setting WHERE account_id=?",
                    (normalized_account_id,),
                ).fetchone()
                current_rows[normalized_account_id] = current
                if current is None:
                    if expected_version not in (None, 0):
                        raise TTPostError(
                            "tt_account_settings_version_conflict",
                            "个号发布设置已被其他操作更新，请刷新后重试",
                            409,
                        )
                elif (
                    expected_version is not None
                    and expected_version != int(current["version"])
                ):
                    raise TTPostError(
                        "tt_account_settings_version_conflict",
                        "个号发布设置已被其他操作更新，请刷新后重试",
                        409,
                    )

            for normalized_account_id, normalized_settings, _ in normalized_updates:
                current = current_rows[normalized_account_id]
                if current is None:
                    conn.execute(
                        """
                        INSERT INTO tt_post_account_setting(
                            account_id,privacy_level,allow_comment,allow_duet,
                            allow_stitch,brand_content_toggle,
                            brand_organic_toggle,is_aigc,version,
                            created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            normalized_account_id,
                            normalized_settings.privacy_level,
                            int(normalized_settings.allow_comment),
                            int(normalized_settings.allow_duet),
                            int(normalized_settings.allow_stitch),
                            int(normalized_settings.brand_content_toggle),
                            int(normalized_settings.brand_organic_toggle),
                            int(normalized_settings.is_aigc),
                            1,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE tt_post_account_setting
                        SET privacy_level=?,allow_comment=?,allow_duet=?,
                            allow_stitch=?,brand_content_toggle=?,
                            brand_organic_toggle=?,is_aigc=?,version=?,
                            updated_at=?
                        WHERE account_id=?
                        """,
                        (
                            normalized_settings.privacy_level,
                            int(normalized_settings.allow_comment),
                            int(normalized_settings.allow_duet),
                            int(normalized_settings.allow_stitch),
                            int(normalized_settings.brand_content_toggle),
                            int(normalized_settings.brand_organic_toggle),
                            int(normalized_settings.is_aigc),
                            int(current["version"]) + 1,
                            timestamp,
                            normalized_account_id,
                        ),
                    )

            rows = []
            for normalized_account_id, _, _ in normalized_updates:
                row = conn.execute(
                    "SELECT * FROM tt_post_account_setting WHERE account_id=?",
                    (normalized_account_id,),
                ).fetchone()
                if row is None:
                    raise TTPostError(
                        "tt_account_settings_write_failed",
                        "批量个号发布设置保存失败",
                        500,
                    )
                rows.append(_public_account_settings(row))
        return rows

    @staticmethod
    def _legacy_auto_publish_config(
        conn: sqlite3.Connection,
    ) -> Dict[str, Any]:
        """Build the version-zero compatibility view without writing a row."""

        rows = conn.execute(
            """
            SELECT * FROM tt_post_daily_schedule
            ORDER BY account_id
            """
        ).fetchall()
        all_schedules = [_public_daily_schedule(row) for row in rows]
        enabled_schedules = [
            schedule for schedule in all_schedules if schedule["enabled"]
        ]
        # During the rolling migration, enabled legacy rows remain the active
        # membership. If every historical row is paused, preserve those rows as
        # the selected-but-paused membership so the new UI does not silently
        # forget the last configured accounts when the global switch is off.
        schedules = enabled_schedules or all_schedules
        projected_enabled = bool(enabled_schedules)
        account_ids = [
            _account_id(schedule["account_id"])
            for schedule in schedules
        ]
        times_by_account = {
            str(schedule["account_id"]): [
                _publish_time(value)
                for value in schedule["publish_times"]
            ]
            for schedule in schedules
        }
        distinct_time_sets = {
            tuple(values) for values in times_by_account.values()
        }
        legacy_mixed = len(distinct_time_sets) > 1
        publish_times = (
            []
            if legacy_mixed
            else list(next(iter(distinct_time_sets), ()))
        )
        consent_source = schedules[0] if schedules else {}
        return {
            "version": 0,
            "enabled": projected_enabled,
            "timezone": "Asia/Shanghai",
            "publish_times": publish_times,
            "account_ids": account_ids,
            "caption_template": FIXED_CAPTION_TEMPLATE,
            "user_consent": bool(consent_source.get("user_consent")),
            "consent_version": str(
                consent_source.get("consent_version", "") or ""
            ),
            "consented_at_utc": str(
                consent_source.get("consented_at_utc", "") or ""
            ),
            "created_by_user_id": "",
            "created_by_name": "",
            "updated_by_user_id": "",
            "updated_by_name": "",
            "created_at": "",
            "updated_at": "",
            "legacy_review_required": legacy_mixed,
            "legacy_schedule_mode": "mixed" if legacy_mixed else "uniform",
            "legacy_publish_times_by_account": times_by_account,
            "legacy_membership_mode": (
                "enabled"
                if enabled_schedules
                else "paused"
                if all_schedules
                else "empty"
            ),
        }

    def get_auto_publish_config(self) -> Dict[str, Any]:
        """Return the atomic UI config or a read-only legacy projection."""

        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_auto_publish_config WHERE id=1"
            ).fetchone()
            if row is None:
                return self._legacy_auto_publish_config(conn)
        return _public_auto_publish_config(row)

    def save_auto_publish_config(
        self,
        *,
        expected_version: Any,
        enabled: Any,
        timezone: Any = None,
        publish_times: Any = None,
        account_ids: Any = None,
        caption_template: Any = None,
        user_consent: Any = None,
        consent_version: Any = None,
        consented_at: Any = None,
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        """Atomically save caption, accounts and automatic schedule settings."""

        normalized_version = _nonnegative_int(
            expected_version,
            "自动发布配置版本",
            2**31 - 1,
        )
        normalized_enabled = _exact_bool(enabled, "自动发布启用状态")
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        timestamp = self._now_iso()

        with self._transaction() as conn:
            stored = conn.execute(
                "SELECT * FROM tt_post_auto_publish_config WHERE id=1"
            ).fetchone()
            current = (
                _public_auto_publish_config(stored)
                if stored is not None
                else self._legacy_auto_publish_config(conn)
            )
            if normalized_version != int(current["version"]):
                raise TTPostError(
                    "tt_post_auto_config_version_conflict",
                    "自动发布配置已被其他操作更新，请刷新后重试",
                    409,
                )
            if bool(current.get("legacy_review_required")):
                if publish_times is None:
                    raise TTPostError(
                        "tt_post_auto_config_legacy_review_required",
                        "旧自动排期时间不一致，首次保存必须明确提交统一时间",
                        409,
                    )
                if normalized_enabled:
                    raise TTPostError(
                        "tt_post_auto_config_legacy_review_required",
                        "旧自动排期时间不一致，请先保存停用的统一配置后再启用",
                        409,
                    )

            if timezone is None:
                normalized_timezone = str(current["timezone"])
            else:
                normalized_timezone = str(timezone or "").strip()
            if normalized_timezone != "Asia/Shanghai":
                raise TTPostError(
                    "invalid_timezone",
                    "自动发布时区必须是Asia/Shanghai",
                    400,
                )

            normalized_times = (
                list(current["publish_times"])
                if publish_times is None
                else _publish_times(publish_times)
            )
            if account_ids is None:
                normalized_account_ids = list(current["account_ids"])
            else:
                if (
                    isinstance(account_ids, (str, bytes, bytearray, Mapping))
                    or not isinstance(account_ids, Iterable)
                ):
                    raise TTPostError(
                        "invalid_auto_publish_accounts",
                        "自动发布账号必须是列表",
                        400,
                    )
                raw_account_ids = list(account_ids)
                if len(raw_account_ids) > MAX_ACCOUNT_SETTINGS_BATCH:
                    raise TTPostError(
                        "invalid_auto_publish_accounts",
                        "自动发布账号最多%d个"
                        % MAX_ACCOUNT_SETTINGS_BATCH,
                        400,
                    )
                normalized_account_ids = []
                seen_account_ids = set()
                for value in raw_account_ids:
                    normalized_account_id = _account_id(value)
                    if normalized_account_id in seen_account_ids:
                        raise TTPostError(
                            "invalid_auto_publish_accounts",
                            "自动发布账号不能重复",
                            400,
                        )
                    seen_account_ids.add(normalized_account_id)
                    normalized_account_ids.append(normalized_account_id)

            normalized_template = (
                str(current["caption_template"])
                if caption_template is None
                else _required_text(
                    caption_template,
                    "发布描述模板",
                    20000,
                )
            )
            # Validate every supported macro now; the real values remain
            # frozen per direct-test/queue row later.
            render_caption_template(
                normalized_template,
                "TT_CONFIG",
                url=_MAX_TT_SHORT_URL,
                description="Drama description",
            )

            normalized_user_consent = (
                bool(current["user_consent"])
                if user_consent is None
                else _exact_bool(user_consent, "自动发布用户授权")
            )
            if consent_version is None:
                normalized_consent_version = str(
                    current["consent_version"] or ""
                )
            else:
                normalized_consent_version = _optional_text(
                    consent_version,
                    "自动发布确认版本",
                    128,
                )
            if consented_at is None:
                normalized_consented_at = str(
                    current["consented_at_utc"] or ""
                )
            else:
                normalized_consented_at = _iso_utc(
                    consented_at,
                    "自动发布确认时间",
                )

            if normalized_enabled and not normalized_times:
                raise TTPostError(
                    "tt_post_auto_config_times_required",
                    "启用自动发布前至少需要设置一个时间点",
                    400,
                )
            if normalized_enabled and not normalized_account_ids:
                raise TTPostError(
                    "tt_post_auto_config_accounts_required",
                    "启用自动发布前至少需要选择一个TikTok账号",
                    400,
                )
            if normalized_enabled and normalized_account_ids and (
                not normalized_user_consent
                or not normalized_consent_version
                or not normalized_consented_at
            ):
                raise TTPostError(
                    "tt_post_consent_required",
                    "保存自动发布账号前必须记录用户授权",
                    409,
                )

            old_account_ids = set(current["account_ids"])
            new_account_ids = set(normalized_account_ids)
            new_version = int(current["version"]) + 1
            times_json = json.dumps(
                normalized_times,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            account_ids_json = json.dumps(
                normalized_account_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if stored is None:
                conn.execute(
                    """
                    INSERT INTO tt_post_auto_publish_config(
                        id,version,enabled,timezone,publish_times_json,
                        account_ids_json,caption_template,user_consent,
                        consent_version,consented_at_utc,
                        created_by_user_id,created_by_name,
                        updated_by_user_id,updated_by_name,created_at,updated_at
                    ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_version,
                        int(normalized_enabled),
                        normalized_timezone,
                        times_json,
                        account_ids_json,
                        normalized_template,
                        int(normalized_user_consent),
                        normalized_consent_version,
                        normalized_consented_at,
                        normalized_actor_id,
                        normalized_actor_name,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                updated = conn.execute(
                    """
                    UPDATE tt_post_auto_publish_config
                    SET version=?,enabled=?,timezone=?,publish_times_json=?,
                        account_ids_json=?,caption_template=?,user_consent=?,
                        consent_version=?,consented_at_utc=?,
                        updated_by_user_id=?,updated_by_name=?,updated_at=?
                    WHERE id=1 AND version=?
                    """,
                    (
                        new_version,
                        int(normalized_enabled),
                        normalized_timezone,
                        times_json,
                        account_ids_json,
                        normalized_template,
                        int(normalized_user_consent),
                        normalized_consent_version,
                        normalized_consented_at,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        int(current["version"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise TTPostError(
                        "tt_post_auto_config_version_conflict",
                        "自动发布配置已被其他操作更新，请刷新后重试",
                        409,
                    )

            for normalized_account_id in normalized_account_ids:
                schedule = conn.execute(
                    """
                    SELECT * FROM tt_post_daily_schedule
                    WHERE account_id=?
                    """,
                    (normalized_account_id,),
                ).fetchone()
                if schedule is None:
                    # A disabled atomic config may remember trusted account
                    # membership before any publish consent exists. Do not
                    # fabricate a legacy schedule whose schema means consent=1.
                    if not normalized_user_consent:
                        continue
                    conn.execute(
                        """
                        INSERT INTO tt_post_daily_schedule(
                            account_id,enabled,timezone,publish_times_json,
                            version,user_consent,consent_version,
                            consented_at_utc,created_by_user_id,
                            created_by_name,updated_by_user_id,
                            updated_by_name,created_at,updated_at
                        ) VALUES(?,?,'Asia/Shanghai',?,1,1,?,?,?,?,?,?,?,?)
                        """,
                        (
                            normalized_account_id,
                            int(normalized_enabled),
                            times_json,
                            normalized_consent_version,
                            normalized_consented_at,
                            normalized_actor_id,
                            normalized_actor_name,
                            normalized_actor_id,
                            normalized_actor_name,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    schedule_user_consent = int(
                        normalized_user_consent
                        or bool(schedule["user_consent"])
                    )
                    schedule_consent_version = (
                        normalized_consent_version
                        if normalized_user_consent
                        else str(schedule["consent_version"] or "")
                    )
                    schedule_consented_at = (
                        normalized_consented_at
                        if normalized_user_consent
                        else str(schedule["consented_at_utc"] or "")
                    )
                    conn.execute(
                        """
                        UPDATE tt_post_daily_schedule
                        SET enabled=?,timezone='Asia/Shanghai',
                            publish_times_json=?,version=?,user_consent=?,
                            consent_version=?,consented_at_utc=?,
                            updated_by_user_id=?,updated_by_name=?,updated_at=?
                        WHERE account_id=?
                        """,
                        (
                            int(normalized_enabled),
                            times_json,
                            int(schedule["version"]) + 1,
                            schedule_user_consent,
                            schedule_consent_version,
                            schedule_consented_at,
                            normalized_actor_id,
                            normalized_actor_name,
                            timestamp,
                            normalized_account_id,
                        ),
                    )

            for removed_account_id in sorted(
                old_account_ids.difference(new_account_ids)
            ):
                schedule = conn.execute(
                    """
                    SELECT * FROM tt_post_daily_schedule
                    WHERE account_id=?
                    """,
                    (removed_account_id,),
                ).fetchone()
                if schedule is not None and bool(schedule["enabled"]):
                    conn.execute(
                        """
                        UPDATE tt_post_daily_schedule
                        SET enabled=0,version=?,updated_by_user_id=?,
                            updated_by_name=?,updated_at=?
                        WHERE account_id=?
                        """,
                        (
                            int(schedule["version"]) + 1,
                            normalized_actor_id,
                            normalized_actor_name,
                            timestamp,
                            removed_account_id,
                        ),
                    )

            result = conn.execute(
                "SELECT * FROM tt_post_auto_publish_config WHERE id=1"
            ).fetchone()
        return _public_auto_publish_config(result)

    def get_daily_schedule(self, account_id: Any) -> Dict[str, Any]:
        normalized = _account_id(account_id)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_daily_schedule WHERE account_id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return _default_daily_schedule(normalized)
        return _public_daily_schedule(row)

    def list_daily_schedules(self) -> List[Dict[str, Any]]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tt_post_daily_schedule ORDER BY account_id"
            ).fetchall()
        return [_public_daily_schedule(row) for row in rows]

    def disable_daily_schedule(
        self,
        account_id: Any,
        *,
        expected_version: Any,
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        """Disable one schedule without inventing a new publishing consent."""

        normalized_account_id = _account_id(account_id)
        normalized_version = _nonnegative_int(
            expected_version,
            "每日排期版本",
            2**31 - 1,
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        timestamp = self._now_iso()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM tt_post_daily_schedule WHERE account_id=?",
                (normalized_account_id,),
            ).fetchone()
            if current is None:
                if normalized_version != 0:
                    raise TTPostError(
                        "tt_post_schedule_version_conflict",
                        "每日发布排期已被其他操作更新，请刷新后重试",
                        409,
                    )
                # There is nothing to stop. Do not fabricate a consent-bearing
                # row merely to represent the already-disabled default state.
                return _default_daily_schedule(normalized_account_id)
            if normalized_version != int(current["version"]):
                raise TTPostError(
                    "tt_post_schedule_version_conflict",
                    "每日发布排期已被其他操作更新，请刷新后重试",
                    409,
                )
            if not bool(current["enabled"]):
                return _public_daily_schedule(current)
            updated = conn.execute(
                """
                UPDATE tt_post_daily_schedule
                SET enabled=0,version=?,updated_by_user_id=?,
                    updated_by_name=?,updated_at=?
                WHERE account_id=? AND enabled=1 AND version=?
                """,
                (
                    int(current["version"]) + 1,
                    normalized_actor_id,
                    normalized_actor_name,
                    timestamp,
                    normalized_account_id,
                    int(current["version"]),
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_schedule_version_conflict",
                    "每日发布排期已被其他操作更新，请刷新后重试",
                    409,
                )
            row = conn.execute(
                "SELECT * FROM tt_post_daily_schedule WHERE account_id=?",
                (normalized_account_id,),
            ).fetchone()
        return _public_daily_schedule(row)

    def save_daily_schedule(
        self,
        account_id: Any,
        publish_times: Any,
        *,
        enabled: Any,
        expected_version: Any,
        consent_version: Any,
        consented_at: Any,
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        normalized_account_id = _account_id(account_id)
        normalized_times = _publish_times(publish_times)
        normalized_enabled = _exact_bool(enabled, "每日排期启用状态")
        if normalized_enabled and not normalized_times:
            raise TTPostError(
                "tt_post_schedule_times_required",
                "启用每日发布前至少需要设置一个时间点",
                400,
            )
        normalized_version = _nonnegative_int(
            expected_version,
            "每日排期版本",
            2**31 - 1,
        )
        normalized_consent_version = _required_text(
            consent_version,
            "每日排期确认版本",
            128,
        )
        normalized_consented_at = _iso_utc(
            consented_at,
            "每日排期确认时间",
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        timestamp = self._now_iso()
        times_json = json.dumps(
            normalized_times,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM tt_post_daily_schedule WHERE account_id=?",
                (normalized_account_id,),
            ).fetchone()
            if normalized_enabled:
                occupied_rows = conn.execute(
                    """
                    SELECT account_id,publish_times_json
                    FROM tt_post_daily_schedule
                    WHERE enabled=1 AND account_id<>?
                    """,
                    (normalized_account_id,),
                ).fetchall()
                requested_times = set(normalized_times)
                for occupied in occupied_rows:
                    try:
                        occupied_times = set(
                            json.loads(
                                str(occupied["publish_times_json"] or "[]")
                            )
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raise TTPostError(
                            "tt_post_schedule_storage_invalid",
                            "每日发布时间配置损坏，请先修复现有排期",
                            500,
                        ) from None
                    if requested_times.intersection(occupied_times):
                        raise TTPostError(
                            "tt_post_schedule_time_conflict",
                            "该发布时间已被其他 TikTok 个号占用，请选择其他时间",
                            409,
                        )
            if current is None:
                if normalized_version != 0:
                    raise TTPostError(
                        "tt_post_schedule_version_conflict",
                        "每日发布排期已被其他操作更新，请刷新后重试",
                        409,
                    )
                conn.execute(
                    """
                    INSERT INTO tt_post_daily_schedule(
                        account_id,enabled,timezone,publish_times_json,version,
                        user_consent,consent_version,consented_at_utc,
                        created_by_user_id,created_by_name,
                        updated_by_user_id,updated_by_name,created_at,updated_at
                    ) VALUES(?,?,'Asia/Shanghai',?,1,1,?,?,?,?,?,?,?,?)
                    """,
                    (
                        normalized_account_id,
                        int(normalized_enabled),
                        times_json,
                        normalized_consent_version,
                        normalized_consented_at,
                        normalized_actor_id,
                        normalized_actor_name,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                if normalized_version != int(current["version"]):
                    raise TTPostError(
                        "tt_post_schedule_version_conflict",
                        "每日发布排期已被其他操作更新，请刷新后重试",
                        409,
                    )
                conn.execute(
                    """
                    UPDATE tt_post_daily_schedule
                    SET enabled=?,timezone='Asia/Shanghai',
                        publish_times_json=?,version=?,
                        user_consent=1,consent_version=?,consented_at_utc=?,
                        updated_by_user_id=?,updated_by_name=?,updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        int(normalized_enabled),
                        times_json,
                        int(current["version"]) + 1,
                        normalized_consent_version,
                        normalized_consented_at,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        normalized_account_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM tt_post_daily_schedule WHERE account_id=?",
                (normalized_account_id,),
            ).fetchone()
        return _public_daily_schedule(row)

    def create_direct_test(
        self,
        material_id: Any,
        account_id: Any,
        content_id: Any,
        source_media_url: Any,
        *,
        idempotency_key: Any,
        gpu_job_id: Any,
        source_trim_tail_seconds: Any,
        preparation_profile: Any,
        caption_template: Any,
        caption: Any,
        short_link_id: Any,
        short_url: Any,
        settings: Any,
        consent_version: Any,
        consented_at: Any,
        config_version: Any,
        material_name: Any = "",
        drama_name: Any = "",
        material_language: Any = "",
        material_tag: Any = "",
        description: Any = "",
        account_username: Any = "",
        account_display_name: Any = "",
        creator_nickname_snapshot: Any = "",
        creator_username_snapshot: Any = "",
        creator_info_hash: Any = "",
        creator_info_synced_at: Any = "",
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        """Create one repeatable test attempt without touching a material pool."""

        normalized_material_id = _material_id(material_id)
        normalized_account_id = _account_id(account_id)
        normalized_content_id = str(content_id or "").strip()
        if not _CONTENT_ID_RE.fullmatch(normalized_content_id):
            raise TTPostError(
                "tt_content_id_invalid",
                "素材对应的content_id无效",
                400,
            )
        normalized_source_url = _https_url(
            source_media_url,
            "素材源视频地址",
        )
        normalized_idempotency_key = _required_text(
            idempotency_key,
            "立即测试发布幂等键",
            255,
        )
        normalized_gpu_job_id = str(gpu_job_id or "").strip()
        if not _GPU_JOB_ID_RE.fullmatch(normalized_gpu_job_id):
            raise TTPostError(
                "invalid_gpu_job_id",
                "TT GPU任务ID无效",
                400,
            )
        try:
            normalized_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT源视频裁剪参数无效",
                400,
            ) from None
        if (
            not math.isfinite(normalized_trim)
            or normalized_trim < 0
            or normalized_trim >= 86400
        ):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT源视频裁剪参数无效",
                400,
            )
        normalized_trim = round(normalized_trim, 6)
        normalized_profile = _required_text(
            preparation_profile,
            "TT成片配置版本",
            128,
        )
        try:
            normalized_description = _normalize_description(description)
        except ValueError:
            raise TTPostError("invalid_request", "素材描述无效", 400) from None
        normalized_template = _required_text(
            caption_template,
            "发布描述模板",
            20000,
        )
        normalized_short_link_id = _nonnegative_int(
            short_link_id,
            "TikTok短链ID",
        )
        normalized_short_url = str(short_url or "").strip()
        if caption_uses_url_macro(normalized_template):
            if normalized_short_link_id <= 0:
                raise TTPostError(
                    "caption_url_required",
                    "发布描述中的{url}必须绑定独立测试短链",
                    400,
                )
            try:
                expected_short_url = build_short_url(
                    normalized_short_link_id
                )
                normalized_short_url = validate_short_url(
                    normalized_short_url
                )
            except Exception as exc:
                raise TTPostError(
                    str(getattr(exc, "code", "tt_short_url_invalid")),
                    str(exc),
                    int(getattr(exc, "status", 400)),
                ) from None
            if not secrets.compare_digest(
                normalized_short_url,
                expected_short_url,
            ):
                raise TTPostError(
                    "tt_short_link_identity_mismatch",
                    "TikTok测试短链与短链ID不匹配",
                    409,
                )
        elif normalized_short_link_id or normalized_short_url:
            raise TTPostError(
                "tt_short_link_not_required",
                "发布描述没有{url}时不能绑定测试短链",
                400,
            )
        normalized_caption = str(caption or "")
        expected_caption = render_caption_template(
            normalized_template,
            normalized_content_id,
            url=normalized_short_url or None,
            description=normalized_description,
        )
        if not secrets.compare_digest(
            normalized_caption.encode("utf-8"),
            expected_caption.encode("utf-8"),
        ):
            raise TTPostError(
                "tt_post_caption_mismatch",
                "发布描述与已冻结素材及短链不匹配",
                409,
            )
        normalized_settings = (
            settings
            if isinstance(settings, TTPostAccountSettings)
            else TTPostAccountSettings.from_mapping(settings)
        )
        normalized_consent_version = _required_text(
            consent_version,
            "发布确认版本",
            128,
        )
        normalized_consented_at = _iso_utc(
            consented_at,
            "发布确认时间",
        )
        normalized_config_version = _nonnegative_int(
            config_version,
            "自动发布配置版本",
            2**31 - 1,
        )
        normalized_material_name = _optional_text(
            material_name,
            "素材名称",
            500,
        )
        normalized_drama_name = _optional_text(
            drama_name,
            "短剧名称",
            500,
        )
        normalized_language = _optional_text(
            material_language,
            "素材语言",
            32,
        )
        normalized_tag = _optional_text(material_tag, "素材标签", 255)
        normalized_account_username = _optional_text(
            account_username,
            "TikTok用户名",
            128,
        )
        normalized_account_display_name = _optional_text(
            account_display_name,
            "TikTok显示名",
            255,
        )
        normalized_creator_nickname = _optional_text(
            creator_nickname_snapshot,
            "TikTok创作者昵称快照",
            255,
        )
        normalized_creator_username = _optional_text(
            creator_username_snapshot,
            "TikTok创作者用户名快照",
            128,
        )
        normalized_creator_hash = _optional_text(
            creator_info_hash,
            "TikTok创作者信息哈希",
            64,
        ).lower()
        if normalized_creator_hash and not _SHA256_RE.fullmatch(
            normalized_creator_hash
        ):
            raise TTPostError(
                "invalid_creator_info_hash",
                "TikTok创作者信息哈希无效",
                400,
            )
        normalized_creator_synced_at = (
            _iso_utc(
                creator_info_synced_at,
                "TikTok创作者信息同步时间",
            )
            if creator_info_synced_at not in (None, "")
            else ""
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        frozen_payload = {
            "account_display_name": normalized_account_display_name,
            "account_id": normalized_account_id,
            "account_username": normalized_account_username,
            "caption": normalized_caption,
            "caption_template": normalized_template,
            "config_version": normalized_config_version,
            "consent_version": normalized_consent_version,
            "consented_at_utc": normalized_consented_at,
            "content_id": normalized_content_id,
            "creator_info_hash": normalized_creator_hash,
            "creator_info_synced_at_utc": normalized_creator_synced_at,
            "creator_nickname_snapshot": normalized_creator_nickname,
            "creator_username_snapshot": normalized_creator_username,
            "description": normalized_description,
            "drama_name": normalized_drama_name,
            "gpu_job_id": normalized_gpu_job_id,
            "material_id": normalized_material_id,
            "material_language": normalized_language,
            "material_name": normalized_material_name,
            "material_tag": normalized_tag,
            "preparation_profile": normalized_profile,
            "settings": normalized_settings.as_dict(),
            "short_link_id": normalized_short_link_id,
            "short_url": normalized_short_url,
            "source_media_url": normalized_source_url,
            "source_trim_tail_seconds": normalized_trim,
        }
        request_sha256 = hashlib.sha256(
            json.dumps(
                frozen_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        timestamp = self._now_iso()
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM tt_post_direct_test
                WHERE idempotency_key=?
                """,
                (normalized_idempotency_key,),
            ).fetchone()
            if existing is not None:
                if secrets.compare_digest(
                    str(existing["request_sha256"]),
                    request_sha256,
                ):
                    return _public_direct_test(existing)
                raise TTPostError(
                    "tt_post_direct_test_idempotency_conflict",
                    "立即测试发布幂等键已用于不同请求",
                    409,
                )
            direct_statuses = sorted(DIRECT_MATERIAL_BLOCKING_STATUSES)
            direct_placeholders = ",".join("?" for _ in direct_statuses)
            if conn.execute(
                """
                SELECT 1 FROM tt_post_direct_test
                WHERE material_id=? AND status IN (%s)
                LIMIT 1
                """
                % direct_placeholders,
                (normalized_material_id, *direct_statuses),
            ).fetchone():
                raise TTPostError(
                    "tt_post_direct_test_active",
                    "This material already has an active or unknown direct test",
                    409,
                )
            queue_statuses = sorted(ACTIVE_QUEUE_STATUSES)
            queue_placeholders = ",".join("?" for _ in queue_statuses)
            if conn.execute(
                """
                SELECT 1 FROM tt_post_queue
                WHERE material_id=? AND status IN (%s)
                LIMIT 1
                """
                % queue_placeholders,
                (normalized_material_id, *queue_statuses),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_publish_active",
                    "This material already has an active or unknown publish",
                    409,
                )
            if conn.execute(
                """
                SELECT 1
                FROM tt_post_recurring_pool AS pool
                LEFT JOIN tt_post_schedule_run AS run
                  ON run.id=pool.run_id
                WHERE pool.material_id=?
                  AND (
                    pool.status='reserved'
                    OR (run.status='claimed' AND run.queue_id IS NULL)
                  )
                LIMIT 1
                """,
                (normalized_material_id,),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_publish_active",
                    "This material is reserved by an active recurring publish",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_direct_test(
                        idempotency_key,request_sha256,material_id,account_id,
                        content_id,source_media_url,material_name,drama_name,
                        material_language,material_tag,description,
                        account_username,account_display_name,
                        creator_nickname_snapshot,creator_username_snapshot,
                        creator_info_hash,creator_info_synced_at_utc,gpu_job_id,
                        source_trim_tail_seconds,preparation_profile,
                        caption_template,caption,short_link_id,short_url,
                        privacy_level,allow_comment,allow_duet,allow_stitch,
                        brand_content_toggle,brand_organic_toggle,is_aigc,
                        user_consent,consent_version,consented_at_utc,
                        config_version,status,created_by_user_id,
                        created_by_name,updated_by_user_id,updated_by_name,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                        ?,?,?,?,?,?,?,1,?,?,?,'queued',?,?,?,?,?,?)
                    """,
                    (
                        normalized_idempotency_key,
                        request_sha256,
                        normalized_material_id,
                        normalized_account_id,
                        normalized_content_id,
                        normalized_source_url,
                        normalized_material_name,
                        normalized_drama_name,
                        normalized_language,
                        normalized_tag,
                        normalized_description,
                        normalized_account_username,
                        normalized_account_display_name,
                        normalized_creator_nickname,
                        normalized_creator_username,
                        normalized_creator_hash,
                        normalized_creator_synced_at,
                        normalized_gpu_job_id,
                        normalized_trim,
                        normalized_profile,
                        normalized_template,
                        normalized_caption,
                        normalized_short_link_id,
                        normalized_short_url,
                        normalized_settings.privacy_level,
                        int(normalized_settings.allow_comment),
                        int(normalized_settings.allow_duet),
                        int(normalized_settings.allow_stitch),
                        int(normalized_settings.brand_content_toggle),
                        int(normalized_settings.brand_organic_toggle),
                        int(normalized_settings.is_aigc),
                        normalized_consent_version,
                        normalized_consented_at,
                        normalized_config_version,
                        normalized_actor_id,
                        normalized_actor_name,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc).lower()
                if "gpu_job_id" in message:
                    code = "tt_post_direct_test_gpu_job_conflict"
                    safe_message = "TT GPU任务ID已用于其他立即测试发布"
                elif "short_link_id" in message:
                    code = "tt_post_direct_test_short_link_conflict"
                    safe_message = "TikTok测试短链ID发生冲突，请换新操作键重试"
                elif "material_id" in message:
                    code = "tt_post_direct_test_active"
                    safe_message = (
                        "This material already has an active or unknown direct test"
                    )
                else:
                    code = "tt_post_direct_test_conflict"
                    safe_message = "立即测试发布发生唯一性冲突，请刷新后重试"
                raise TTPostError(code, safe_message, 409) from None
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _public_direct_test(row)

    def get_direct_test(self, direct_test_id: Any) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_direct_test_not_found",
                "立即测试发布记录不存在",
                404,
            )
        return _public_direct_test(row)

    def get_direct_test_by_idempotency_key(
        self,
        idempotency_key: Any,
        *,
        required: bool = False,
    ) -> Optional[Dict[str, Any]]:
        normalized_key = _required_text(
            idempotency_key,
            "立即测试发布幂等键",
            255,
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT * FROM tt_post_direct_test
                WHERE idempotency_key=?
                """,
                (normalized_key,),
            ).fetchone()
        if row is None:
            if required:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            return None
        return _public_direct_test(row)

    def list_direct_tests(
        self,
        *,
        account_id: Any = None,
        material_id: Any = None,
        status: Any = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_limit = _positive_int(limit, "立即测试发布列表数量", 1000)
        normalized_offset = _nonnegative_int(
            offset,
            "立即测试发布列表偏移",
            2**31 - 1,
        )
        clauses = []
        params: List[Any] = []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(_account_id(account_id))
        if material_id is not None:
            clauses.append("material_id=?")
            params.append(_material_id(material_id))
        if status is not None:
            normalized_status = str(status or "").strip()
            if normalized_status not in DIRECT_TEST_STATUSES:
                raise TTPostError(
                    "invalid_direct_test_status",
                    "立即测试发布状态无效",
                    400,
                )
            clauses.append("status=?")
            params.append(normalized_status)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend((normalized_limit, normalized_offset))
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tt_post_direct_test%s
                ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?
                """
                % where_sql,
                params,
            ).fetchall()
        return [_public_direct_test(row) for row in rows]

    @staticmethod
    def _assert_direct_test_claim(
        row: sqlite3.Row,
        claim_token: Any,
        *,
        phase: str,
        allowed_statuses: Sequence[str],
        now_iso: str,
    ) -> None:
        supplied = str(claim_token or "")
        stored = str(row["claim_token"] or "")
        if (
            not supplied
            or not stored
            or not secrets.compare_digest(supplied, stored)
            or str(row["claim_phase"]) != phase
            or str(row["status"]) not in set(allowed_statuses)
        ):
            raise TTPostError(
                "tt_post_direct_test_claim_invalid",
                "立即测试发布认领无效或状态已变更",
                409,
            )
        if (
            row["lease_expires_at_utc"]
            and str(row["lease_expires_at_utc"]) <= now_iso
        ):
            raise TTPostError(
                "tt_post_direct_test_claim_expired",
                "立即测试发布认领已过期",
                409,
            )

    def claim_direct_test_prepare(
        self,
        worker_id: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 900,
        limit: int = 10,
    ) -> List[DirectTestClaim]:
        worker = str(worker_id or "").strip()
        if not _WORKER_ID_RE.fullmatch(worker):
            raise TTPostError("invalid_worker_id", "预制作执行器ID无效", 400)
        normalized_lease = _positive_int(
            lease_seconds,
            "预制作认领时长",
            86400,
        )
        normalized_limit = _positive_int(limit, "预制作认领数量", 100)
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease)
        )
        claims: List[DirectTestClaim] = []
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='queued',claim_phase='',claim_worker='',
                    claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_direct_prepare_lease_expired',
                    error_message='预制作租约过期，已安全重新排队',
                    updated_at=?
                WHERE status='preparing' AND claim_phase='prepare'
                  AND lease_expires_at_utc<>''
                  AND lease_expires_at_utc<=?
                """,
                (now_iso, now_iso),
            )
            candidates = conn.execute(
                """
                SELECT * FROM tt_post_direct_test
                WHERE status='queued'
                ORDER BY created_at,id LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
            for row in candidates:
                token = secrets.token_urlsafe(32)
                updated = conn.execute(
                    """
                    UPDATE tt_post_direct_test
                    SET status='preparing',claim_phase='prepare',
                        claim_worker=?,claim_token=?,lease_expires_at_utc=?,
                        preparation_attempt_count=
                            preparation_attempt_count+1,
                        claimed_at_utc=?,error_code='',error_message='',
                        failed_at_utc='',updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (
                        worker,
                        token,
                        lease_iso,
                        now_iso,
                        now_iso,
                        int(row["id"]),
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed = conn.execute(
                    "SELECT * FROM tt_post_direct_test WHERE id=?",
                    (int(row["id"]),),
                ).fetchone()
                claims.append(DirectTestClaim(_public_direct_test(claimed), token))
        return claims

    def _renew_direct_test_claim(
        self,
        direct_test_id: Any,
        claim_token: Any,
        *,
        phase: str,
        status: str,
        now: Optional[Any],
        lease_seconds: int,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        normalized_lease = _positive_int(
            lease_seconds,
            "立即测试发布认领时长",
            86400,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease)
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            self._assert_direct_test_claim(
                row,
                claim_token,
                phase=phase,
                allowed_statuses=(status,),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET lease_expires_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (lease_iso, now_iso, normalized_id),
            )
            renewed = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(renewed)

    def renew_direct_test_prepare(
        self,
        direct_test_id: Any,
        claim_token: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 900,
    ) -> Dict[str, Any]:
        return self._renew_direct_test_claim(
            direct_test_id,
            claim_token,
            phase="prepare",
            status="preparing",
            now=now,
            lease_seconds=lease_seconds,
        )

    def complete_direct_test_prepare(
        self,
        direct_test_id: Any,
        claim_token: Any,
        *,
        gpu_job_id: Any,
        prepared_media_url: Any,
        prepared_output_sha256: Any,
        prepared_output_size: Any,
        prepared_duration_sec: Any,
        source_trim_tail_seconds: Any,
        preparation_profile: Any,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        normalized_gpu_job_id = str(gpu_job_id or "").strip()
        if not _GPU_JOB_ID_RE.fullmatch(normalized_gpu_job_id):
            raise TTPostError("invalid_gpu_job_id", "TT GPU任务ID无效", 400)
        normalized_url = _https_url(
            prepared_media_url,
            "TT最终成片地址",
        )
        normalized_sha = str(prepared_output_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(normalized_sha):
            raise TTPostError(
                "invalid_prepared_output_sha",
                "TT最终成片SHA256无效",
                400,
            )
        normalized_size = _positive_int(
            prepared_output_size,
            "TT最终成片大小",
        )
        try:
            normalized_duration = float(prepared_duration_sec)
            normalized_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            ) from None
        if (
            not math.isfinite(normalized_duration)
            or normalized_duration <= 0
            or normalized_duration > 86400
            or not math.isfinite(normalized_trim)
            or normalized_trim < 0
            or normalized_trim >= normalized_duration
        ):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            )
        normalized_duration = round(normalized_duration, 6)
        normalized_trim = round(normalized_trim, 6)
        normalized_profile = _required_text(
            preparation_profile,
            "TT成片配置版本",
            128,
        )
        artifact = (
            normalized_url,
            normalized_sha,
            normalized_size,
            normalized_duration,
        )
        now_iso = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            frozen_matches = (
                secrets.compare_digest(
                    str(row["gpu_job_id"]),
                    normalized_gpu_job_id,
                )
                and secrets.compare_digest(
                    str(row["preparation_profile"]),
                    normalized_profile,
                )
                and float(row["source_trim_tail_seconds"]) == normalized_trim
            )
            if not frozen_matches:
                raise TTPostError(
                    "tt_post_direct_test_artifact_mismatch",
                    "预制作结果与已冻结立即测试身份不一致",
                    409,
                )
            if secrets.compare_digest(
                str(row["source_media_url"]),
                normalized_url,
            ):
                raise TTPostError(
                    "tt_prepared_media_matches_source",
                    "TT最终成片地址不能与源素材地址相同",
                    409,
                )
            if str(row["status"]) == "ready":
                stored_artifact = (
                    str(row["prepared_media_url"]),
                    str(row["prepared_output_sha256"]),
                    int(row["prepared_output_size"]),
                    float(row["prepared_duration_sec"]),
                )
                if stored_artifact == artifact:
                    return _public_direct_test(row)
                raise TTPostError(
                    "tt_post_direct_test_completion_conflict",
                    "立即测试成片已完成且结果不同",
                    409,
                )
            self._assert_direct_test_claim(
                row,
                claim_token,
                phase="prepare",
                allowed_statuses=("preparing",),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='ready',prepared_media_url=?,
                    prepared_output_sha256=?,prepared_output_size=?,
                    prepared_duration_sec=?,claim_phase='',claim_worker='',
                    claim_token='',lease_expires_at_utc='',error_code='',
                    error_message='',prepared_at_utc=?,failed_at_utc='',
                    updated_at=?
                WHERE id=? AND status='preparing' AND claim_phase='prepare'
                  AND claim_token=?
                """,
                (
                    normalized_url,
                    normalized_sha,
                    normalized_size,
                    normalized_duration,
                    now_iso,
                    now_iso,
                    normalized_id,
                    str(claim_token or ""),
                ),
            )
            completed = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(completed)

    def fail_direct_test_prepare(
        self,
        direct_test_id: Any,
        claim_token: Any,
        *,
        error_code: Any,
        error_message: Any,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        normalized_code = _required_text(
            error_code,
            "预制作错误码",
            96,
        )
        normalized_message = _required_text(
            redact_text(error_message),
            "预制作错误说明",
            500,
        )
        now_iso = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if (
                str(row["status"]) == "failed"
                and str(row["error_code"]) == normalized_code
                and str(row["error_message"]) == normalized_message
            ):
                return _public_direct_test(row)
            self._assert_direct_test_claim(
                row,
                claim_token,
                phase="prepare",
                allowed_statuses=("preparing",),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='failed',claim_phase='',claim_worker='',
                    claim_token='',lease_expires_at_utc='',error_code=?,
                    error_message=?,failed_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (
                    normalized_code,
                    normalized_message,
                    now_iso,
                    now_iso,
                    normalized_id,
                ),
            )
            failed = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(failed)

    def quarantine_expired_direct_test_publishes(
        self,
        *,
        now: Optional[Any] = None,
    ) -> int:
        """Move every expired in-flight direct publish to reconcile-only unknown."""

        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "current time",
        )
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='unknown',unknown_outcome=1,claim_phase='',
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_direct_publish_lease_expired',
                    error_message='Direct publish lease expired; manual reconciliation is required',
                    failed_at_utc=?,updated_at=?
                WHERE status='publishing' AND claim_phase='publish'
                  AND lease_expires_at_utc<>''
                  AND lease_expires_at_utc<=?
                """,
                (now_iso, now_iso, now_iso),
            )
            return int(updated.rowcount)

    def claim_direct_test_publish(
        self,
        direct_test_id: Any,
        worker_id: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 300,
    ) -> DirectTestClaim:
        """Claim one exact ready test; stale publish leases become unknown."""

        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        worker = str(worker_id or "").strip()
        if not _WORKER_ID_RE.fullmatch(worker):
            raise TTPostError("invalid_worker_id", "发布执行器ID无效", 400)
        normalized_lease = _positive_int(
            lease_seconds,
            "发布认领时长",
            86400,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease)
        )
        stale_quarantined = False
        with self._transaction() as conn:
            stale = conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='unknown',unknown_outcome=1,claim_phase='',
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_direct_publish_lease_expired',
                    error_message='发布执行中租约过期，结果需要人工核对',
                    failed_at_utc=?,updated_at=?
                WHERE id=? AND status='publishing' AND claim_phase='publish'
                  AND lease_expires_at_utc<>''
                  AND lease_expires_at_utc<=?
                """,
                (now_iso, now_iso, normalized_id, now_iso),
            )
            stale_quarantined = stale.rowcount == 1
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if stale_quarantined:
                claimed = None
            elif str(row["status"]) != "ready":
                raise TTPostError(
                    "tt_post_direct_test_not_ready",
                    "立即测试发布尚未完成成片或状态已变更",
                    409,
                )
            else:
                direct_account_statuses = sorted(
                    DIRECT_ACCOUNT_BLOCKING_STATUSES
                )
                direct_account_placeholders = ",".join(
                    "?" for _ in direct_account_statuses
                )
                if conn.execute(
                    """
                    SELECT 1 FROM tt_post_direct_test
                    WHERE account_id=? AND id<>? AND status IN (%s)
                    LIMIT 1
                    """
                    % direct_account_placeholders,
                    (
                        str(row["account_id"]),
                        normalized_id,
                        *direct_account_statuses,
                    ),
                ).fetchone():
                    raise TTPostError(
                        "tt_post_account_publish_busy",
                        "This TikTok account has an active or unknown direct test",
                        409,
                    )
                queue_statuses = sorted(ACTIVE_QUEUE_STATUSES)
                queue_placeholders = ",".join(
                    "?" for _ in queue_statuses
                )
                if conn.execute(
                    """
                    SELECT 1 FROM tt_post_queue
                    WHERE account_id=? AND status IN (%s)
                    LIMIT 1
                    """
                    % queue_placeholders,
                    (str(row["account_id"]), *queue_statuses),
                ).fetchone():
                    raise TTPostError(
                        "tt_post_account_publish_busy",
                        "This TikTok account has an active or unknown queue",
                        409,
                    )
                run_statuses = sorted(ACTIVE_SCHEDULE_RUN_STATUSES)
                run_placeholders = ",".join("?" for _ in run_statuses)
                if conn.execute(
                    """
                    SELECT 1 FROM tt_post_schedule_run
                    WHERE account_id=? AND status IN (%s)
                    LIMIT 1
                    """
                    % run_placeholders,
                    (str(row["account_id"]), *run_statuses),
                ).fetchone():
                    raise TTPostError(
                        "tt_post_account_publish_busy",
                        "This TikTok account has an active or unknown recurring run",
                        409,
                    )
                token = secrets.token_urlsafe(32)
                updated = conn.execute(
                    """
                    UPDATE tt_post_direct_test
                    SET status='publishing',claim_phase='publish',
                        claim_worker=?,claim_token=?,lease_expires_at_utc=?,
                        publish_attempt_count=publish_attempt_count+1,
                        claimed_at_utc=?,publish_started_at_utc=?,
                        error_code='',error_message='',unknown_outcome=0,
                        failed_at_utc='',updated_at=?
                    WHERE id=? AND status='ready'
                    """,
                    (
                        worker,
                        token,
                        lease_iso,
                        now_iso,
                        now_iso,
                        now_iso,
                        normalized_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise TTPostError(
                        "tt_post_direct_test_claim_conflict",
                        "立即测试发布已被其他执行器认领",
                        409,
                    )
                claimed = conn.execute(
                    "SELECT * FROM tt_post_direct_test WHERE id=?",
                    (normalized_id,),
                ).fetchone()
        if stale_quarantined:
            raise TTPostError(
                "tt_post_direct_test_outcome_unknown",
                "发布执行租约已过期，结果需要人工核对",
                409,
            )
        return DirectTestClaim(_public_direct_test(claimed), token)

    def renew_direct_test_publish(
        self,
        direct_test_id: Any,
        claim_token: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 300,
    ) -> Dict[str, Any]:
        return self._renew_direct_test_claim(
            direct_test_id,
            claim_token,
            phase="publish",
            status="publishing",
            now=now,
            lease_seconds=lease_seconds,
        )

    def prepare_direct_test_short_link(
        self,
        direct_test_id: Any,
        publish_claim_token: Any,
        long_url: Any,
    ) -> Dict[str, Any]:
        """Freeze one W2A wrapper target under the active publish lease."""

        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        try:
            normalized_long_url = validate_w2a_url(long_url)
        except Exception as exc:
            raise TTPostError(
                str(getattr(exc, "code", "tt_short_link_target_invalid")),
                str(exc),
                int(getattr(exc, "status", 400)),
            ) from None
        now_iso = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            self._assert_direct_test_claim(
                row,
                publish_claim_token,
                phase="publish",
                allowed_statuses=("publishing",),
                now_iso=now_iso,
            )
            if (
                int(row["short_link_id"] or 0) <= 0
                or not str(row["short_url"] or "")
            ):
                raise TTPostError(
                    "tt_short_link_not_required",
                    "立即测试发布描述没有待准备的短链",
                    409,
                )
            existing = str(row["long_url"] or "")
            if existing:
                if not secrets.compare_digest(existing, normalized_long_url):
                    raise TTPostError(
                        "tt_short_link_target_conflict",
                        "立即测试短链目标已冻结且不同",
                        409,
                    )
                return _public_direct_test(row)
            updated = conn.execute(
                """
                UPDATE tt_post_direct_test
                SET long_url=?,updated_at=?
                WHERE id=? AND status='publishing'
                  AND claim_phase='publish' AND claim_token=?
                  AND long_url=''
                """,
                (
                    normalized_long_url,
                    now_iso,
                    normalized_id,
                    str(publish_claim_token or ""),
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_short_link_target_conflict",
                    "立即测试短链目标已被其他操作变更",
                    409,
                )
            prepared = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(prepared)

    def record_direct_test_publish_id(
        self,
        direct_test_id: Any,
        publish_claim_token: Any,
        publish_id: Any,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Persist the remote ID and permanently enter reconcile-only mode."""

        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if str(row["status"]) == "reconciling":
                if secrets.compare_digest(str(row["publish_id"]), remote_id):
                    return _public_direct_test(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已冻结结果不一致",
                    409,
                )
            self._assert_direct_test_claim(
                row,
                publish_claim_token,
                phase="publish",
                allowed_statuses=("publishing",),
                now_iso=now_iso,
            )
            if int(row["short_link_id"] or 0) > 0 and not str(
                row["long_url"] or ""
            ):
                raise TTPostError(
                    "tt_short_link_target_required",
                    "TikTok init前必须先冻结立即测试短链目标",
                    409,
                )
            try:
                updated = conn.execute(
                    """
                    UPDATE tt_post_direct_test
                    SET status='reconciling',publish_id=?,claim_phase='',
                        claim_worker='',claim_token='',
                        lease_expires_at_utc='',unknown_outcome=0,
                        error_code='',error_message='',updated_at=?
                    WHERE id=? AND status='publishing'
                      AND claim_phase='publish' AND claim_token=?
                    """,
                    (
                        remote_id,
                        now_iso,
                        normalized_id,
                        str(publish_claim_token or ""),
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id已绑定其他立即测试发布",
                    409,
                ) from None
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_direct_test_claim_invalid",
                    "立即测试发布认领无效或状态已变更",
                    409,
                )
            recorded = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(recorded)

    def recover_direct_test_publish_id(
        self,
        direct_test_id: Any,
        publish_id: Any,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Recover a GPU-ledger publish ID after CPU acknowledgement loss."""

        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            stored_id = str(row["publish_id"] or "")
            if str(row["status"]) == "reconciling":
                if secrets.compare_digest(stored_id, remote_id):
                    return _public_direct_test(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已恢复结果不一致",
                    409,
                )
            if str(row["status"]) not in {"unknown", "publishing"}:
                raise TTPostError(
                    "tt_post_direct_publish_recovery_not_allowed",
                    "当前立即测试状态不允许恢复publish_id",
                    409,
                )
            if stored_id and not secrets.compare_digest(stored_id, remote_id):
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已冻结结果不一致",
                    409,
                )
            try:
                updated = conn.execute(
                    """
                    UPDATE tt_post_direct_test
                    SET status='reconciling',publish_id=?,unknown_outcome=0,
                        claim_phase='',claim_worker='',claim_token='',
                        lease_expires_at_utc='',error_code='',error_message='',
                        updated_at=?
                    WHERE id=? AND status IN ('unknown','publishing')
                    """,
                    (remote_id, now_iso, normalized_id),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id已绑定其他立即测试发布",
                    409,
                ) from None
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_direct_publish_recovery_not_allowed",
                    "当前立即测试状态不允许恢复publish_id",
                    409,
                )
            recovered = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(recovered)

    def fail_direct_test_publish(
        self,
        direct_test_id: Any,
        publish_claim_token: Any,
        *,
        error_code: Any,
        error_message: Any,
        publish_was_not_created: Any,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        normalized_code = _required_text(error_code, "发布错误码", 96)
        normalized_message = _required_text(
            redact_text(error_message),
            "发布错误说明",
            500,
        )
        known_safe = _exact_bool(
            publish_was_not_created,
            "远端未创建发布结果确认",
        )
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if str(row["status"]) in {
                "published",
                "failed",
                "unknown",
                "canceled",
            }:
                return _public_direct_test(row)
            if row["publish_id"] or str(row["status"]) == "reconciling":
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "已取得publish_id的立即测试只能执行核对",
                    409,
                )
            self._assert_direct_test_claim(
                row,
                publish_claim_token,
                phase="publish",
                allowed_statuses=("publishing",),
                now_iso=now_iso,
            )
            target_status = "failed" if known_safe else "unknown"
            stored_code = (
                normalized_code
                if known_safe
                else "tt_post_direct_outcome_unknown"
            )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status=?,unknown_outcome=?,claim_phase='',
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code=?,error_message=?,failed_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (
                    target_status,
                    0 if known_safe else 1,
                    stored_code,
                    normalized_message,
                    now_iso,
                    now_iso,
                    normalized_id,
                ),
            )
            failed = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(failed)

    def reconcile_direct_test_published(
        self,
        direct_test_id: Any,
        publish_id: Any,
        *,
        publish_url: Any = "",
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        normalized_url = _https_url(
            publish_url,
            "TikTok Post地址",
            allow_empty=True,
        )
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if str(row["status"]) == "published":
                if secrets.compare_digest(str(row["publish_id"]), remote_id):
                    return _public_direct_test(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已发布结果不一致",
                    409,
                )
            if (
                str(row["status"]) != "reconciling"
                or not secrets.compare_digest(
                    str(row["publish_id"]),
                    remote_id,
                )
            ):
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "立即测试没有可核对的TikTok publish_id",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='published',publish_url=?,unknown_outcome=0,
                    error_code='',error_message='',published_at_utc=?,
                    failed_at_utc='',updated_at=?
                WHERE id=? AND status='reconciling' AND publish_id=?
                """,
                (
                    normalized_url,
                    now_iso,
                    now_iso,
                    normalized_id,
                    remote_id,
                ),
            )
            published = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(published)

    def reconcile_direct_test_failed(
        self,
        direct_test_id: Any,
        publish_id: Any,
        *,
        remote_status: Any,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        normalized_remote_status = str(remote_status or "").strip().lower()
        if normalized_remote_status not in {"failed", "publish_failed"}:
            raise TTPostError(
                "invalid_remote_publish_status",
                "TikTok远端发布失败状态无效",
                400,
            )
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        message = "TikTok远端核对明确返回发布失败（%s）" % (
            normalized_remote_status
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if (
                str(row["status"]) == "failed"
                and str(row["error_code"])
                == "tt_post_direct_remote_publish_failed"
                and secrets.compare_digest(str(row["publish_id"]), remote_id)
            ):
                return _public_direct_test(row)
            if (
                str(row["status"]) != "reconciling"
                or not secrets.compare_digest(
                    str(row["publish_id"]),
                    remote_id,
                )
            ):
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "立即测试没有可核对的TikTok publish_id",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='failed',unknown_outcome=0,
                    error_code='tt_post_direct_remote_publish_failed',
                    error_message=?,failed_at_utc=?,updated_at=?
                WHERE id=? AND status='reconciling' AND publish_id=?
                """,
                (message, now_iso, now_iso, normalized_id, remote_id),
            )
            failed = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(failed)

    def cancel_direct_test(
        self,
        direct_test_id: Any,
        *,
        reason: Any,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(
            direct_test_id,
            "立即测试发布记录ID",
        )
        normalized_reason = _required_text(
            redact_text(reason),
            "取消原因",
            500,
        )
        now_iso = _iso_utc(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_direct_test_not_found",
                    "立即测试发布记录不存在",
                    404,
                )
            if str(row["status"]) == "canceled":
                return _public_direct_test(row)
            if str(row["status"]) not in {"queued", "ready"}:
                raise TTPostError(
                    "tt_post_direct_test_cancel_not_allowed",
                    "当前立即测试发布状态不允许取消",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_direct_test
                SET status='canceled',claim_phase='',claim_worker='',
                    claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_direct_test_canceled',
                    error_message=?,canceled_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (normalized_reason, now_iso, now_iso, normalized_id),
            )
            canceled = conn.execute(
                "SELECT * FROM tt_post_direct_test WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_direct_test(canceled)

    def get_material_publication_states(
        self,
        material_ids: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """Aggregate legacy and direct-test publication history by material."""

        if (
            isinstance(material_ids, (str, bytes, bytearray, Mapping))
            or not isinstance(material_ids, Iterable)
        ):
            raise TTPostError(
                "invalid_material_ids",
                "素材ID必须是列表",
                400,
            )
        normalized_ids = []
        seen_ids = set()
        for value in list(material_ids):
            normalized = _material_id(value)
            if normalized not in seen_ids:
                seen_ids.add(normalized)
                normalized_ids.append(normalized)
        if len(normalized_ids) > 1000:
            raise TTPostError(
                "invalid_material_ids",
                "一次最多查询1000个素材ID",
                400,
            )
        aggregates: Dict[str, Dict[str, Any]] = {
            material_id: {
                "material_id": material_id,
                "publication_state": "unpublished",
                "publication_status": "unpublished",
                "publish_count": 0,
                "unknown_count": 0,
                "attempt_count": 0,
                "latest_published_at_utc": "",
                "latest_publish_id": "",
                "latest_publish_url": "",
                "latest_status_at_utc": "",
            }
            for material_id in normalized_ids
        }
        if not normalized_ids:
            return aggregates
        placeholders = ",".join("?" for _ in normalized_ids)
        params = tuple(normalized_ids) + tuple(normalized_ids)
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT material_id,status,unknown_outcome,publish_id,
                    publish_url,updated_at AS status_at_utc,
                    CASE WHEN status='published' THEN updated_at ELSE '' END
                        AS published_at_utc,
                    'legacy_queue' AS source
                FROM tt_post_queue
                WHERE material_id IN (%s)
                UNION ALL
                SELECT material_id,status,unknown_outcome,publish_id,
                    publish_url,updated_at AS status_at_utc,
                    CASE WHEN status='published'
                        THEN CASE WHEN published_at_utc<>''
                            THEN published_at_utc ELSE updated_at END
                        ELSE '' END AS published_at_utc,
                    'direct_test' AS source
                FROM tt_post_direct_test
                WHERE material_id IN (%s)
                """
                % (placeholders, placeholders),
                params,
            ).fetchall()
        uncertain_statuses = {"publishing", "reconciling", "unknown"}
        for row in rows:
            material_id = str(row["material_id"])
            aggregate = aggregates[material_id]
            aggregate["attempt_count"] += 1
            status_at = str(row["status_at_utc"] or "")
            if status_at > aggregate["latest_status_at_utc"]:
                aggregate["latest_status_at_utc"] = status_at
            status = str(row["status"])
            if status == "published":
                aggregate["publish_count"] += 1
                published_at = str(row["published_at_utc"] or "")
                if published_at >= aggregate["latest_published_at_utc"]:
                    aggregate["latest_published_at_utc"] = published_at
                    aggregate["latest_publish_id"] = str(
                        row["publish_id"] or ""
                    )
                    aggregate["latest_publish_url"] = str(
                        row["publish_url"] or ""
                    )
            elif bool(row["unknown_outcome"]) or status in uncertain_statuses:
                aggregate["unknown_count"] += 1
        for aggregate in aggregates.values():
            if aggregate["publish_count"]:
                aggregate["publication_state"] = "published"
                aggregate["publication_status"] = "published"
            elif aggregate["unknown_count"]:
                aggregate["publication_state"] = "unknown"
                aggregate["publication_status"] = "unknown"
        return aggregates

    def get_material_publication_state(
        self,
        material_id: Any,
    ) -> Dict[str, Any]:
        normalized = _material_id(material_id)
        return self.get_material_publication_states([normalized])[normalized]

    def add_material_intake(
        self,
        material_id: Any,
        account_id: Any,
        content_id: Any,
        source_media_url: Any,
        *,
        idempotency_key: Any,
        gpu_job_id: Any,
        source_trim_tail_seconds: Any,
        preparation_profile: Any,
        caption_template: Any,
        caption: Any,
        consent_version: Any,
        consented_at: Any,
        is_aigc: Any,
        material_name: Any = "",
        drama_name: Any = "",
        material_language: Any = "",
        material_tag: Any = "",
        description: Any = "",
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        """Persist one validated material before any remote preparation work."""

        normalized_material_id = _material_id(material_id)
        normalized_account_id = _account_id(account_id)
        normalized_content_id = str(content_id or "").strip()
        if not _CONTENT_ID_RE.fullmatch(normalized_content_id):
            raise TTPostError(
                "tt_content_id_invalid",
                "素材对应的content_id无效",
                400,
            )
        normalized_source_url = _https_url(
            source_media_url,
            "素材源视频地址",
        )
        normalized_idempotency_key = _required_text(
            idempotency_key,
            "素材入池幂等键",
            255,
        )
        normalized_gpu_job_id = str(gpu_job_id or "").strip()
        if not _GPU_JOB_ID_RE.fullmatch(normalized_gpu_job_id):
            raise TTPostError(
                "invalid_gpu_job_id",
                "TT GPU任务ID无效",
                400,
            )
        try:
            normalized_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT源视频裁剪参数无效",
                400,
            ) from None
        if (
            not math.isfinite(normalized_trim)
            or normalized_trim < 0
            or normalized_trim >= 86400
        ):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT源视频裁剪参数无效",
                400,
            )
        normalized_trim = round(normalized_trim, 6)
        normalized_profile = _required_text(
            preparation_profile,
            "TT成片配置版本",
            128,
        )
        try:
            normalized_description = _normalize_description(description)
        except ValueError:
            raise TTPostError("invalid_request", "素材描述无效", 400) from None
        normalized_template = str(caption_template or "")
        normalized_caption = str(caption or "")
        expected_caption = render_caption_template(
            normalized_template,
            normalized_content_id,
            description=normalized_description,
            defer_url=True,
        )
        if not secrets.compare_digest(
            normalized_caption.encode("utf-8"),
            expected_caption.encode("utf-8"),
        ):
            raise TTPostError(
                "tt_post_caption_mismatch",
                "发布描述与素材content_id不匹配",
                409,
            )
        normalized_consent_version = _required_text(
            consent_version,
            "发布确认版本",
            128,
        )
        normalized_consented_at = _iso_utc(
            consented_at,
            "发布确认时间",
        )
        normalized_is_aigc = _exact_bool(
            is_aigc,
            "AI生成内容标记",
        )
        normalized_material_name = _optional_text(
            material_name,
            "素材名称",
            500,
        )
        normalized_drama_name = _optional_text(
            drama_name,
            "短剧名称",
            500,
        )
        normalized_language = _optional_text(
            material_language,
            "素材语言",
            32,
        )
        normalized_tag = _optional_text(
            material_tag,
            "素材标签",
            255,
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        frozen_payload = {
            "account_id": normalized_account_id,
            "caption": normalized_caption,
            "caption_template": normalized_template,
            "consent_version": normalized_consent_version,
            "consented_at_utc": normalized_consented_at,
            "content_id": normalized_content_id,
            "description": normalized_description,
            "drama_name": normalized_drama_name,
            "gpu_job_id": normalized_gpu_job_id,
            "is_aigc": bool(normalized_is_aigc),
            "material_id": normalized_material_id,
            "material_language": normalized_language,
            "material_name": normalized_material_name,
            "material_tag": normalized_tag,
            "preparation_profile": normalized_profile,
            "source_media_url": normalized_source_url,
            "source_trim_tail_seconds": normalized_trim,
        }
        request_sha256 = hashlib.sha256(
            json.dumps(
                frozen_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        timestamp = self._now_iso()
        with self._transaction() as conn:
            existing_by_key = conn.execute(
                """
                SELECT * FROM tt_post_material_intake
                WHERE idempotency_key=?
                """,
                (normalized_idempotency_key,),
            ).fetchone()
            if existing_by_key is not None:
                if secrets.compare_digest(
                    str(existing_by_key["request_sha256"]),
                    request_sha256,
                ):
                    return _public_material_intake(existing_by_key)
                raise TTPostError(
                    "tt_post_material_intake_idempotency_conflict",
                    "素材入池幂等键已用于不同请求",
                    409,
                )

            existing_by_material = conn.execute(
                """
                SELECT * FROM tt_post_material_intake
                WHERE material_id=?
                """,
                (normalized_material_id,),
            ).fetchone()
            if existing_by_material is not None:
                if secrets.compare_digest(
                    str(existing_by_material["request_sha256"]),
                    request_sha256,
                ):
                    return _public_material_intake(existing_by_material)
                raise TTPostError(
                    "tt_post_material_intake_conflict",
                    "素材已入池且冻结信息不同",
                    409,
                )

            if conn.execute(
                """
                SELECT 1 FROM tt_post_recurring_pool
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_material_pool
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_queue
                WHERE material_id=?
                LIMIT 1
                """,
                (
                    normalized_material_id,
                    normalized_material_id,
                    normalized_material_id,
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_already_used",
                    "素材已存在于排期、发布池或发布历史中",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_material_intake(
                        idempotency_key,request_sha256,material_id,account_id,
                        content_id,source_media_url,material_name,drama_name,
                        material_language,material_tag,description,gpu_job_id,
                        source_trim_tail_seconds,preparation_profile,
                        caption_template,caption,consent_version,
                        consented_at_utc,is_aigc,user_consent,status,
                        created_by_user_id,created_by_name,
                        updated_by_user_id,updated_by_name,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'queued',
                        ?,?,?,?,?,?)
                    """,
                    (
                        normalized_idempotency_key,
                        request_sha256,
                        normalized_material_id,
                        normalized_account_id,
                        normalized_content_id,
                        normalized_source_url,
                        normalized_material_name,
                        normalized_drama_name,
                        normalized_language,
                        normalized_tag,
                        normalized_description,
                        normalized_gpu_job_id,
                        normalized_trim,
                        normalized_profile,
                        normalized_template,
                        normalized_caption,
                        normalized_consent_version,
                        normalized_consented_at,
                        int(normalized_is_aigc),
                        normalized_actor_id,
                        normalized_actor_name,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_material_intake_conflict",
                    "素材入池发生唯一性冲突，请刷新后重试",
                    409,
                ) from None
            row = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _public_material_intake(row)

    def get_material_intake(self, intake_id: Any) -> Dict[str, Any]:
        normalized_id = _positive_int(intake_id, "素材入池记录ID")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_material_intake_not_found",
                "素材入池记录不存在",
                404,
            )
        return _public_material_intake(row)

    def list_material_intakes(
        self,
        *,
        account_id: Any = None,
        status: Any = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_limit = _positive_int(
            limit,
            "素材入池列表数量",
            1000,
        )
        normalized_offset = _nonnegative_int(
            offset,
            "素材入池列表偏移",
            2**31 - 1,
        )
        clauses = []
        params: List[Any] = []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(_account_id(account_id))
        if status is not None:
            normalized_status = str(status or "").strip()
            if normalized_status not in MATERIAL_INTAKE_STATUSES:
                raise TTPostError(
                    "invalid_material_intake_status",
                    "素材预制作状态无效",
                    400,
                )
            clauses.append("status=?")
            params.append(normalized_status)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend((normalized_limit, normalized_offset))
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tt_post_material_intake%s
                ORDER BY created_at,id LIMIT ? OFFSET ?
                """
                % where_sql,
                params,
            ).fetchall()
        return [_public_material_intake(row) for row in rows]

    @staticmethod
    def _assert_material_intake_claim(
        row: sqlite3.Row,
        claim_token: Any,
        *,
        now_iso: str,
    ) -> None:
        supplied = str(claim_token or "")
        stored = str(row["claim_token"] or "")
        lease_expires = str(row["lease_expires_at_utc"] or "")
        if (
            not supplied
            or not stored
            or not secrets.compare_digest(supplied, stored)
            or str(row["status"]) != "preparing"
            or not lease_expires
            or lease_expires <= now_iso
        ):
            raise TTPostError(
                "tt_post_material_intake_claim_invalid",
                "素材预制作认领无效或已过期",
                409,
            )

    def claim_material_intake(
        self,
        worker_id: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 120,
    ) -> Optional[MaterialIntakeClaim]:
        worker = str(worker_id or "").strip()
        if not _WORKER_ID_RE.fullmatch(worker):
            raise TTPostError(
                "invalid_worker_id",
                "素材预制作执行器ID无效",
                400,
            )
        normalized_lease = _positive_int(
            lease_seconds,
            "素材预制作认领时长",
            10800,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease)
        )
        claim_token = secrets.token_urlsafe(32)
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT candidate.*
                FROM tt_post_material_intake candidate
                WHERE (
                        candidate.status='queued'
                        OR (
                            candidate.status='retry_wait'
                            AND (
                                candidate.next_attempt_at_utc=''
                                OR candidate.next_attempt_at_utc<=?
                            )
                        )
                        OR (
                            candidate.status='preparing'
                            AND candidate.lease_expires_at_utc<>''
                            AND candidate.lease_expires_at_utc<=?
                        )
                    )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM tt_post_material_intake prior
                        WHERE prior.account_id=candidate.account_id
                          AND prior.status IN (
                              'queued','preparing','retry_wait'
                          )
                          AND (
                              prior.created_at<candidate.created_at
                              OR (
                                  prior.created_at=candidate.created_at
                                  AND prior.id<candidate.id
                              )
                          )
                  )
                ORDER BY candidate.created_at,candidate.id
                LIMIT 1
                """,
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE tt_post_material_intake
                SET status='preparing',attempt_count=attempt_count+1,
                    next_attempt_at_utc='',claim_worker=?,claim_token=?,
                    lease_expires_at_utc=?,claimed_at_utc=?,
                    error_code='',error_message='',updated_at=?
                WHERE id=?
                  AND (
                    status='queued'
                    OR (
                        status='retry_wait'
                        AND (
                            next_attempt_at_utc=''
                            OR next_attempt_at_utc<=?
                        )
                    )
                    OR (
                        status='preparing'
                        AND lease_expires_at_utc<>''
                        AND lease_expires_at_utc<=?
                    )
                  )
                """,
                (
                    worker,
                    claim_token,
                    lease_iso,
                    now_iso,
                    now_iso,
                    int(row["id"]),
                    now_iso,
                    now_iso,
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_material_intake_claim_busy",
                    "素材预制作任务已被其他执行器领取",
                    409,
                )
            claimed = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (int(row["id"]),),
            ).fetchone()
            return MaterialIntakeClaim(
                _public_material_intake(claimed),
                claim_token,
            )

    def renew_material_intake(
        self,
        intake_id: Any,
        claim_token: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 120,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(intake_id, "素材入池记录ID")
        normalized_lease = _positive_int(
            lease_seconds,
            "素材预制作认领时长",
            10800,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease)
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_material_intake_not_found",
                    "素材入池记录不存在",
                    404,
                )
            self._assert_material_intake_claim(
                row,
                claim_token,
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_material_intake
                SET lease_expires_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (lease_iso, now_iso, normalized_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_material_intake(refreshed)

    def complete_material_intake(
        self,
        intake_id: Any,
        claim_token: Any,
        *,
        gpu_job_id: Any,
        prepared_media_url: Any,
        prepared_output_sha256: Any,
        prepared_output_size: Any,
        prepared_duration_sec: Any,
        preparation_profile: Any,
        source_trim_tail_seconds: Any,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(intake_id, "素材入池记录ID")
        normalized_gpu_job_id = str(gpu_job_id or "").strip()
        if not _GPU_JOB_ID_RE.fullmatch(normalized_gpu_job_id):
            raise TTPostError(
                "invalid_gpu_job_id",
                "TT GPU任务ID无效",
                400,
            )
        normalized_url = _https_url(
            prepared_media_url,
            "TT最终成片地址",
        )
        normalized_sha = str(prepared_output_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(normalized_sha):
            raise TTPostError(
                "invalid_prepared_output_sha",
                "TT最终成片SHA256无效",
                400,
            )
        normalized_size = _positive_int(
            prepared_output_size,
            "TT最终成片大小",
        )
        try:
            normalized_duration = float(prepared_duration_sec)
            normalized_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            ) from None
        if (
            not math.isfinite(normalized_duration)
            or normalized_duration <= 0
            or normalized_duration > 86400
            or not math.isfinite(normalized_trim)
            or normalized_trim < 0
            or normalized_trim >= normalized_duration
        ):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            )
        normalized_duration = round(normalized_duration, 6)
        normalized_trim = round(normalized_trim, 6)
        normalized_profile = _required_text(
            preparation_profile,
            "TT成片配置版本",
            128,
        )
        timestamp = self._now_iso()
        artifact_values = (
            normalized_url,
            normalized_sha,
            normalized_size,
            normalized_duration,
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_material_intake_not_found",
                    "素材入池记录不存在",
                    404,
                )
            frozen_matches = (
                secrets.compare_digest(
                    str(row["gpu_job_id"]),
                    normalized_gpu_job_id,
                )
                and secrets.compare_digest(
                    str(row["preparation_profile"]),
                    normalized_profile,
                )
                and float(row["source_trim_tail_seconds"])
                == normalized_trim
            )
            if not frozen_matches:
                raise TTPostError(
                    "tt_post_material_intake_artifact_mismatch",
                    "预制作结果与已冻结素材身份不一致",
                    409,
                )
            if secrets.compare_digest(
                str(row["source_media_url"]),
                normalized_url,
            ):
                raise TTPostError(
                    "tt_prepared_media_matches_source",
                    "TT最终成片地址不能与源素材地址相同",
                    409,
                )
            if str(row["status"]) == "ready":
                stored_values = (
                    str(row["prepared_media_url"]),
                    str(row["prepared_output_sha256"]),
                    int(row["prepared_output_size"]),
                    float(row["prepared_duration_sec"]),
                )
                if stored_values == artifact_values:
                    return _public_material_intake(row)
                raise TTPostError(
                    "tt_post_material_intake_completion_conflict",
                    "素材已完成且成片信息不同",
                    409,
                )
            self._assert_material_intake_claim(
                row,
                claim_token,
                now_iso=timestamp,
            )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_recurring_pool
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_material_pool
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_queue
                WHERE material_id=?
                LIMIT 1
                """,
                (
                    str(row["material_id"]),
                    str(row["material_id"]),
                    str(row["material_id"]),
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_already_used",
                    "素材在预制作期间已进入其他排期或发布历史",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_recurring_pool(
                        material_id,account_id,content_id,source_media_url,
                        material_name,drama_name,material_language,material_tag,
                        description,prepared_media_url,gpu_job_id,
                        prepared_output_sha256,
                        prepared_output_size,prepared_duration_sec,
                        source_trim_tail_seconds,preparation_profile,
                        caption_template,caption,consent_version,
                        consented_at_utc,is_aigc,user_consent,status,
                        created_by_user_id,created_by_name,
                        updated_by_user_id,updated_by_name,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'available',
                        ?,?,?,?,?,?)
                    """,
                    (
                        str(row["material_id"]),
                        str(row["account_id"]),
                        str(row["content_id"]),
                        str(row["source_media_url"]),
                        str(row["material_name"]),
                        str(row["drama_name"]),
                        str(row["material_language"]),
                        str(row["material_tag"]),
                        str(row["description"]),
                        normalized_url,
                        normalized_gpu_job_id,
                        normalized_sha,
                        normalized_size,
                        normalized_duration,
                        normalized_trim,
                        normalized_profile,
                        str(row["caption_template"]),
                        str(row["caption"]),
                        str(row["consent_version"]),
                        str(row["consented_at_utc"]),
                        int(row["is_aigc"]),
                        str(row["created_by_user_id"]),
                        str(row["created_by_name"]),
                        str(row["updated_by_user_id"]),
                        str(row["updated_by_name"]),
                        str(row["created_at"]),
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_material_intake_completion_conflict",
                    "素材写入可发布池时发生唯一性冲突",
                    409,
                ) from None
            recurring_pool_id = int(cursor.lastrowid)
            updated = conn.execute(
                """
                UPDATE tt_post_material_intake
                SET status='ready',prepared_media_url=?,
                    prepared_output_sha256=?,prepared_output_size=?,
                    prepared_duration_sec=?,recurring_pool_id=?,
                    next_attempt_at_utc='',claim_worker='',claim_token='',
                    lease_expires_at_utc='',error_code='',error_message='',
                    ready_at_utc=?,failed_at_utc='',updated_at=?
                WHERE id=? AND status='preparing' AND claim_token=?
                """,
                (
                    normalized_url,
                    normalized_sha,
                    normalized_size,
                    normalized_duration,
                    recurring_pool_id,
                    timestamp,
                    timestamp,
                    normalized_id,
                    str(claim_token or ""),
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_material_intake_claim_invalid",
                    "素材预制作认领无效或已变更",
                    409,
                )
            completed = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_material_intake(completed)

    def fail_material_intake(
        self,
        intake_id: Any,
        claim_token: Any,
        *,
        error_code: Any,
        error_message: Any,
        retry_at: Any = None,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(intake_id, "素材入池记录ID")
        normalized_error_code = _required_text(
            error_code,
            "预制作错误码",
            96,
        )
        normalized_error_message = redact_text(error_message)
        timestamp = self._now_iso()
        should_retry = retry_at not in (None, "")
        normalized_retry_at = (
            _iso_utc(retry_at, "下次预制作时间")
            if should_retry
            else ""
        )
        if should_retry and normalized_retry_at <= timestamp:
            raise TTPostError(
                "invalid_retry_time",
                "下次预制作时间必须晚于当前时间",
                400,
            )
        target_status = "retry_wait" if should_retry else "failed"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_material_intake_not_found",
                    "素材入池记录不存在",
                    404,
                )
            if (
                str(row["status"]) == target_status
                and str(row["error_code"]) == normalized_error_code
                and str(row["error_message"]) == normalized_error_message
                and str(row["next_attempt_at_utc"])
                == normalized_retry_at
            ):
                return _public_material_intake(row)
            self._assert_material_intake_claim(
                row,
                claim_token,
                now_iso=timestamp,
            )
            conn.execute(
                """
                UPDATE tt_post_material_intake
                SET status=?,next_attempt_at_utc=?,claim_worker='',
                    claim_token='',lease_expires_at_utc=?,error_code=?,
                    error_message=?,failed_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (
                    target_status,
                    normalized_retry_at,
                    "",
                    normalized_error_code,
                    normalized_error_message,
                    "" if should_retry else timestamp,
                    timestamp,
                    normalized_id,
                ),
            )
            failed = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?",
                (normalized_id,),
            ).fetchone()
        return _public_material_intake(failed)

    def add_recurring_material(
        self,
        material_id: Any,
        account_id: Any,
        content_id: Any,
        source_media_url: Any,
        prepared_media_url: Any,
        *,
        gpu_job_id: Any,
        prepared_output_sha256: Any,
        prepared_output_size: Any,
        prepared_duration_sec: Any,
        source_trim_tail_seconds: Any,
        preparation_profile: Any,
        caption_template: Any,
        caption: Any,
        consent_version: Any,
        consented_at: Any,
        is_aigc: Any,
        material_name: Any = "",
        drama_name: Any = "",
        material_language: Any = "",
        material_tag: Any = "",
        description: Any = "",
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        normalized_material_id = _material_id(material_id)
        normalized_account_id = _account_id(account_id)
        normalized_content_id = str(content_id or "").strip()
        if not _CONTENT_ID_RE.fullmatch(normalized_content_id):
            raise TTPostError(
                "tt_content_id_invalid",
                "素材对应的content_id无效",
                400,
            )
        normalized_source_url = _https_url(
            source_media_url,
            "素材源视频地址",
        )
        normalized_prepared_url = _https_url(
            prepared_media_url,
            "最终成片地址",
        )
        if secrets.compare_digest(
            normalized_source_url,
            normalized_prepared_url,
        ):
            raise TTPostError(
                "tt_prepared_media_matches_source",
                "TT最终成片地址不能与源素材地址相同",
                409,
            )
        normalized_gpu_job_id = str(gpu_job_id or "").strip()
        if not _GPU_JOB_ID_RE.fullmatch(normalized_gpu_job_id):
            raise TTPostError("invalid_gpu_job_id", "TT GPU任务ID无效", 400)
        normalized_sha = str(prepared_output_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(normalized_sha):
            raise TTPostError(
                "invalid_prepared_output_sha",
                "TT最终成片SHA256无效",
                400,
            )
        normalized_size = _positive_int(
            prepared_output_size,
            "TT最终成片大小",
        )
        try:
            normalized_duration = float(prepared_duration_sec)
            normalized_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            ) from None
        if (
            not math.isfinite(normalized_duration)
            or normalized_duration <= 0
            or normalized_duration > 86400
            or not math.isfinite(normalized_trim)
            or normalized_trim < 0
            or normalized_trim >= normalized_duration
        ):
            raise TTPostError(
                "invalid_prepared_media_metrics",
                "TT最终成片时长或裁剪参数无效",
                400,
            )
        normalized_duration = round(normalized_duration, 6)
        normalized_trim = round(normalized_trim, 6)
        normalized_profile = _required_text(
            preparation_profile,
            "TT成片配置版本",
            128,
        )
        try:
            normalized_description = _normalize_description(description)
        except ValueError:
            raise TTPostError("invalid_request", "素材描述无效", 400) from None
        normalized_template = str(caption_template or "")
        normalized_caption = str(caption or "")
        expected_caption = render_caption_template(
            normalized_template,
            normalized_content_id,
            description=normalized_description,
            defer_url=True,
        )
        if not secrets.compare_digest(
            normalized_caption.encode("utf-8"),
            expected_caption.encode("utf-8"),
        ):
            raise TTPostError(
                "tt_post_caption_mismatch",
                "发布描述与素材content_id不匹配",
                409,
            )
        normalized_consent_version = _required_text(
            consent_version,
            "发布确认版本",
            128,
        )
        normalized_consented_at = _iso_utc(
            consented_at,
            "发布确认时间",
        )
        normalized_is_aigc = _exact_bool(is_aigc, "AI生成内容标记")
        normalized_material_name = _optional_text(
            material_name,
            "素材名称",
            500,
        )
        normalized_drama_name = _optional_text(
            drama_name,
            "短剧名称",
            500,
        )
        normalized_language = _optional_text(
            material_language,
            "素材语言",
            32,
        )
        normalized_tag = _optional_text(
            material_tag,
            "素材标签",
            255,
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        frozen_values = (
            normalized_account_id,
            normalized_content_id,
            normalized_source_url,
            normalized_material_name,
            normalized_drama_name,
            normalized_language,
            normalized_tag,
            normalized_description,
            normalized_prepared_url,
            normalized_gpu_job_id,
            normalized_sha,
            normalized_size,
            normalized_duration,
            normalized_trim,
            normalized_profile,
            normalized_template,
            normalized_caption,
            normalized_consent_version,
            normalized_consented_at,
            int(normalized_is_aigc),
        )
        timestamp = self._now_iso()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tt_post_recurring_pool WHERE material_id=?",
                (normalized_material_id,),
            ).fetchone()
            if existing is not None:
                existing_values = (
                    str(existing["account_id"]),
                    str(existing["content_id"]),
                    str(existing["source_media_url"]),
                    str(existing["material_name"]),
                    str(existing["drama_name"]),
                    str(existing["material_language"]),
                    str(existing["material_tag"]),
                    str(existing["description"]),
                    str(existing["prepared_media_url"]),
                    str(existing["gpu_job_id"]),
                    str(existing["prepared_output_sha256"]),
                    int(existing["prepared_output_size"]),
                    float(existing["prepared_duration_sec"]),
                    float(existing["source_trim_tail_seconds"]),
                    str(existing["preparation_profile"]),
                    str(existing["caption_template"]),
                    str(existing["caption"]),
                    str(existing["consent_version"]),
                    str(existing["consented_at_utc"]),
                    int(existing["is_aigc"]),
                )
                if existing_values == frozen_values:
                    return _public_recurring_pool(existing)
                raise TTPostError(
                    "tt_post_recurring_material_conflict",
                    "该素材已存在于每日发布池且冻结信息不同",
                    409,
                )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_material_intake
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_material_pool
                WHERE material_id=?
                UNION ALL
                SELECT 1 FROM tt_post_queue
                WHERE material_id=?
                LIMIT 1
                """,
                (
                    normalized_material_id,
                    normalized_material_id,
                    normalized_material_id,
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_already_used",
                    "素材已存在于一次性排期或发布历史中",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_recurring_pool(
                        material_id,account_id,content_id,source_media_url,
                        material_name,drama_name,material_language,material_tag,
                        description,prepared_media_url,gpu_job_id,
                        prepared_output_sha256,
                        prepared_output_size,prepared_duration_sec,
                        source_trim_tail_seconds,preparation_profile,
                        caption_template,caption,consent_version,
                        consented_at_utc,is_aigc,user_consent,status,
                        created_by_user_id,created_by_name,
                        updated_by_user_id,updated_by_name,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'available',
                        ?,?,?,?,?,?)
                    """,
                    (
                        normalized_material_id,
                        *frozen_values,
                        normalized_actor_id,
                        normalized_actor_name,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_recurring_material_conflict",
                    "每日发布素材发生唯一性冲突，请刷新后重试",
                    409,
                ) from None
            row = conn.execute(
                "SELECT * FROM tt_post_recurring_pool WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _public_recurring_pool(row)

    def list_recurring_materials(
        self,
        *,
        account_id: Any = None,
        status: Any = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_limit = _positive_int(limit, "每日素材列表数量", 1000)
        normalized_offset = _nonnegative_int(
            offset,
            "每日素材列表偏移",
            2**31 - 1,
        )
        clauses = []
        params: List[Any] = []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(_account_id(account_id))
        if status is not None:
            normalized_status = str(status or "").strip()
            if normalized_status not in RECURRING_POOL_STATUSES:
                raise TTPostError(
                    "invalid_recurring_pool_status",
                    "每日素材池状态无效",
                    400,
                )
            clauses.append("status=?")
            params.append(normalized_status)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend((normalized_limit, normalized_offset))
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tt_post_recurring_pool%s
                ORDER BY created_at,id LIMIT ? OFFSET ?
                """
                % where_sql,
                params,
            ).fetchall()
        return [_public_recurring_pool(row) for row in rows]

    def count_recurring_materials(
        self,
        *,
        account_id: Any = None,
        status: Any = None,
    ) -> int:
        clauses = []
        params: List[Any] = []
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(_account_id(account_id))
        if status is not None:
            normalized_status = str(status or "").strip()
            if normalized_status not in RECURRING_POOL_STATUSES:
                raise TTPostError(
                    "invalid_recurring_pool_status",
                    "每日素材池状态无效",
                    400,
                )
            clauses.append("status=?")
            params.append(normalized_status)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM tt_post_recurring_pool%s"
                % where_sql,
                params,
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _recurring_run_result(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Dict[str, Any]:
        result = _public_schedule_run(row)
        pool = conn.execute(
            "SELECT * FROM tt_post_recurring_pool WHERE id=?",
            (int(row["pool_item_id"]),),
        ).fetchone()
        if pool is None:
            raise TTPostError(
                "tt_post_recurring_storage_invalid",
                "每日发布运行记录缺少素材",
                500,
            )
        result["pool_item"] = _public_recurring_pool(pool)
        return result

    @staticmethod
    def _assert_same_run_request(
        row: sqlite3.Row,
        *,
        run_key: str,
        trigger_type: str,
        account_id: str,
        shanghai_date: str,
        publish_time: str,
        scheduled_at_utc: str,
        config_version: int,
        manual_request_key: str,
    ) -> None:
        if trigger_type == "manual":
            if (
                str(row["trigger_type"]) != "manual"
                or str(row["account_id"]) != account_id
                or str(row["manual_request_key"]) != manual_request_key
                or str(row["run_key"]) != run_key
            ):
                raise TTPostError(
                    "tt_post_schedule_run_idempotency_conflict",
                    "每日发布运行幂等键已用于不同请求",
                    409,
                )
            # The API assigns a slot from server time. A browser/network retry
            # may arrive in a later minute, so the stored slot is authoritative
            # for the same manual request key.
            return
        frozen = (
            str(row["trigger_type"]),
            str(row["account_id"]),
            str(row["shanghai_date"]),
            str(row["publish_time"]),
            str(row["scheduled_at_utc"]),
            int(row["config_version"]),
            str(row["manual_request_key"]),
        )
        requested = (
            trigger_type,
            account_id,
            shanghai_date,
            publish_time,
            scheduled_at_utc,
            config_version,
            manual_request_key,
        )
        if frozen != requested:
            raise TTPostError(
                "tt_post_schedule_run_idempotency_conflict",
                "每日发布运行幂等键已用于不同请求",
                409,
            )
        if str(row["run_key"]) != run_key:
            raise TTPostError(
                "tt_post_schedule_run_idempotency_conflict",
                "每日发布运行幂等键已用于不同请求",
                409,
            )

    def claim_recurring_run(
        self,
        run_key: Any,
        trigger_type: Any,
        account_id: Any,
        shanghai_date: Any,
        publish_time: Any,
        scheduled_at_utc: Any,
        *,
        config_version: Any,
        manual_request_key: Any = "",
    ) -> Dict[str, Any]:
        normalized_run_key = _required_text(
            run_key,
            "每日发布运行键",
            255,
        )
        normalized_trigger = str(trigger_type or "").strip()
        if normalized_trigger not in {"auto", "manual"}:
            raise TTPostError(
                "invalid_schedule_trigger",
                "每日发布触发类型无效",
                400,
            )
        normalized_account_id = _account_id(account_id)
        normalized_date = _shanghai_date(shanghai_date)
        normalized_time = _publish_time(publish_time)
        normalized_scheduled_at = _iso_utc(
            scheduled_at_utc,
            "每日发布排期UTC时间",
        )
        if normalized_scheduled_at != _scheduled_slot_utc(
            normalized_date,
            normalized_time,
        ):
            raise TTPostError(
                "tt_post_schedule_slot_mismatch",
                "上海日期时间与排期UTC时间不匹配",
                400,
            )
        normalized_config_version = _nonnegative_int(
            config_version,
            "每日排期版本",
            2**31 - 1,
        )
        normalized_manual_key = _optional_text(
            manual_request_key,
            "手动发布请求键",
            255,
        )
        if normalized_trigger == "manual" and not normalized_manual_key:
            raise TTPostError(
                "tt_post_manual_request_key_required",
                "手动发布必须提供请求幂等键",
                400,
            )
        if normalized_trigger == "auto" and normalized_manual_key:
            raise TTPostError(
                "invalid_schedule_trigger",
                "自动发布不能携带手动请求键",
                400,
            )
        timestamp = self._now_iso()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE run_key=?",
                (normalized_run_key,),
            ).fetchone()
            if existing is None and normalized_manual_key:
                existing = conn.execute(
                    """
                    SELECT * FROM tt_post_schedule_run
                    WHERE manual_request_key=?
                    """,
                    (normalized_manual_key,),
                ).fetchone()
            if existing is not None:
                self._assert_same_run_request(
                    existing,
                    run_key=normalized_run_key,
                    trigger_type=normalized_trigger,
                    account_id=normalized_account_id,
                    shanghai_date=normalized_date,
                    publish_time=normalized_time,
                    scheduled_at_utc=normalized_scheduled_at,
                    config_version=normalized_config_version,
                    manual_request_key=normalized_manual_key,
                )
                return self._recurring_run_result(conn, existing)

            if normalized_trigger == "auto":
                schedule = conn.execute(
                    """
                    SELECT * FROM tt_post_daily_schedule
                    WHERE account_id=?
                    """,
                    (normalized_account_id,),
                ).fetchone()
                if (
                    schedule is None
                    or not bool(schedule["enabled"])
                    or int(schedule["version"]) != normalized_config_version
                    or normalized_time
                    not in json.loads(str(schedule["publish_times_json"]))
                ):
                    raise TTPostError(
                        "tt_post_schedule_not_current",
                        "每日发布排期未启用或版本已变化",
                        409,
                    )

            direct_account_statuses = sorted(
                DIRECT_ACCOUNT_BLOCKING_STATUSES
            )
            direct_account_placeholders = ",".join(
                "?" for _ in direct_account_statuses
            )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_direct_test
                WHERE account_id=? AND status IN (%s)
                LIMIT 1
                """
                % direct_account_placeholders,
                (
                    normalized_account_id,
                    *direct_account_statuses,
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_account_publish_busy",
                    "This TikTok account has an active or unknown direct test",
                    409,
                )

            placeholders = ",".join("?" for _ in ACTIVE_QUEUE_STATUSES)
            if conn.execute(
                """
                SELECT 1 FROM tt_post_queue
                WHERE account_id=? AND status IN (%s)
                LIMIT 1
                """
                % placeholders,
                (normalized_account_id, *sorted(ACTIVE_QUEUE_STATUSES)),
            ).fetchone():
                raise TTPostError(
                    "tt_post_account_publish_busy",
                    "该TikTok账号已有活跃发布队列",
                    409,
                )
            placeholders = ",".join(
                "?" for _ in ACTIVE_SCHEDULE_RUN_STATUSES
            )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_schedule_run
                WHERE account_id=? AND status IN (%s)
                LIMIT 1
                """
                % placeholders,
                (
                    normalized_account_id,
                    *sorted(ACTIVE_SCHEDULE_RUN_STATUSES),
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_account_publish_busy",
                    "该TikTok账号已有活跃每日发布运行",
                    409,
                )
            pool = conn.execute(
                """
                SELECT * FROM tt_post_recurring_pool
                WHERE account_id=? AND status='available'
                ORDER BY created_at,id
                LIMIT 1
                """,
                (normalized_account_id,),
            ).fetchone()
            if pool is None:
                raise TTPostError(
                    "tt_post_recurring_pool_empty",
                    "该TikTok账号没有可用的每日发布素材",
                    409,
                )
            direct_material_statuses = sorted(
                DIRECT_MATERIAL_BLOCKING_STATUSES
            )
            direct_material_placeholders = ",".join(
                "?" for _ in direct_material_statuses
            )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_direct_test
                WHERE material_id=? AND status IN (%s)
                LIMIT 1
                """
                % direct_material_placeholders,
                (
                    str(pool["material_id"]),
                    *direct_material_statuses,
                ),
            ).fetchone():
                raise TTPostError(
                    "tt_post_direct_test_active",
                    "The FIFO material has an active or unknown direct test",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_schedule_run(
                        run_key,trigger_type,account_id,shanghai_date,
                        publish_time,scheduled_at_utc,config_version,
                        manual_request_key,pool_item_id,status,
                        created_at,updated_at,claimed_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,?,?)
                    """,
                    (
                        normalized_run_key,
                        normalized_trigger,
                        normalized_account_id,
                        normalized_date,
                        normalized_time,
                        normalized_scheduled_at,
                        normalized_config_version,
                        normalized_manual_key,
                        int(pool["id"]),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_schedule_run_conflict",
                    "每日发布运行发生唯一性冲突，请刷新后重试",
                    409,
                ) from None
            run_id = int(cursor.lastrowid)
            updated = conn.execute(
                """
                UPDATE tt_post_recurring_pool
                SET status='reserved',run_id=?,updated_at=?,
                    reserved_at_utc=?
                WHERE id=? AND status='available' AND run_id IS NULL
                """,
                (run_id, timestamp, timestamp, int(pool["id"])),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_recurring_pool_conflict",
                    "每日发布素材已被其他运行领取",
                    409,
                )
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (run_id,),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    def get_recurring_run(self, run_id: Any) -> Dict[str, Any]:
        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            return self._recurring_run_result(conn, row)

    def get_recurring_run_by_key(self, run_key: Any) -> Dict[str, Any]:
        normalized_run_key = _required_text(
            run_key,
            "每日发布运行键",
            255,
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE run_key=?",
                (normalized_run_key,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            return self._recurring_run_result(conn, row)

    def get_recurring_run_by_queue_id(self, queue_id: Any) -> Dict[str, Any]:
        """Return the durable recurring run bound to one exact queue."""

        normalized_queue_id = _positive_int(
            queue_id,
            "一次性发布队列ID",
        )
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT * FROM tt_post_schedule_run
                WHERE queue_id=?
                """,
                (normalized_queue_id,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "该队列没有每日发布运行记录",
                    404,
                )
            return self._recurring_run_result(conn, row)

    @staticmethod
    def _assert_recurring_execution(
        run: sqlite3.Row,
        execution_token: Any,
        *,
        now_iso: str,
    ) -> None:
        supplied = str(execution_token or "")
        stored = str(run["execution_token"] or "")
        lease_expires = str(
            run["execution_lease_expires_at_utc"] or ""
        )
        if (
            not supplied
            or not stored
            or not secrets.compare_digest(supplied, stored)
            or str(run["status"]) != "claimed"
            or run["queue_id"] is not None
            or not lease_expires
            or lease_expires <= now_iso
        ):
            raise TTPostError(
                "tt_post_recurring_execution_invalid",
                "每日发布运行执行租约无效或已过期",
                409,
            )

    def acquire_recurring_execution(
        self,
        run_id: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 120,
    ) -> RecurringExecutionClaim:
        """Atomically lease one claimed/unbound run without locking other runs."""

        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        normalized_lease_seconds = _positive_int(
            lease_seconds,
            "每日发布执行租约时长",
            600,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease_seconds)
        )
        execution_token = secrets.token_urlsafe(32)
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            if str(run["status"]) != "claimed" or run["queue_id"] is not None:
                raise TTPostError(
                    "tt_post_recurring_execution_not_claimable",
                    "每日发布运行当前不需要执行租约",
                    409,
                )
            stored_token = str(run["execution_token"] or "")
            stored_expiry = str(
                run["execution_lease_expires_at_utc"] or ""
            )
            if (
                stored_token
                and stored_expiry
                and stored_expiry > now_iso
            ):
                raise TTPostError(
                    "tt_post_recurring_execution_busy",
                    "每日发布运行正在由另一执行者处理",
                    409,
                )
            updated = conn.execute(
                """
                UPDATE tt_post_schedule_run
                SET execution_token=?,execution_lease_expires_at_utc=?,
                    updated_at=?
                WHERE id=? AND status='claimed' AND queue_id IS NULL
                  AND (
                    execution_token=''
                    OR execution_lease_expires_at_utc=''
                    OR execution_lease_expires_at_utc<=?
                  )
                """,
                (
                    execution_token,
                    lease_iso,
                    now_iso,
                    normalized_run_id,
                    now_iso,
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_recurring_execution_busy",
                    "每日发布运行正在由另一执行者处理",
                    409,
                )
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            return RecurringExecutionClaim(
                self._recurring_run_result(conn, row),
                execution_token,
            )

    def renew_recurring_execution(
        self,
        run_id: Any,
        execution_token: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 120,
    ) -> Dict[str, Any]:
        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        normalized_lease_seconds = _positive_int(
            lease_seconds,
            "每日发布执行租约时长",
            600,
        )
        current = _utc_datetime(
            now if now is not None else self._now_fn(),
            "当前时间",
        )
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(
            current + timedelta(seconds=normalized_lease_seconds)
        )
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            self._assert_recurring_execution(
                run,
                execution_token,
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_schedule_run
                SET execution_lease_expires_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (lease_iso, now_iso, normalized_run_id),
            )
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    def yield_recurring_execution(
        self,
        run_id: Any,
        execution_token: Any,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Relinquish one owned lease while preserving its durable reservation."""

        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        now_iso = _iso_utc(
            _utc_datetime(
                now if now is not None else self._now_fn(),
                "当前时间",
            )
        )
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            self._assert_recurring_execution(
                run,
                execution_token,
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_schedule_run
                SET execution_token='',execution_lease_expires_at_utc='',
                    updated_at=?
                WHERE id=?
                """,
                (now_iso, normalized_run_id),
            )
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    def list_claimed_unbound_recurring_runs(
        self,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        normalized_limit = _positive_int(
            limit,
            "待恢复每日发布运行数量",
            100,
        )
        now_iso = self._now_iso()
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tt_post_schedule_run
                WHERE status='claimed' AND queue_id IS NULL
                  AND (
                    execution_token=''
                    OR execution_lease_expires_at_utc=''
                    OR execution_lease_expires_at_utc<=?
                  )
                ORDER BY scheduled_at_utc,id
                LIMIT ?
                """,
                (now_iso, normalized_limit),
            ).fetchall()
            return [
                self._recurring_run_result(conn, row)
                for row in rows
            ]

    def recurring_recovery_backlog(self) -> Dict[str, Any]:
        now_iso = self._now_iso()
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS deferred_count,
                    MIN(scheduled_at_utc) AS oldest_deferred_at_utc
                FROM tt_post_schedule_run
                WHERE status='claimed' AND queue_id IS NULL
                  AND (
                    execution_token=''
                    OR execution_lease_expires_at_utc=''
                    OR execution_lease_expires_at_utc<=?
                  )
                """,
                (now_iso,),
            ).fetchone()
        return {
            "deferred_count": int(row["deferred_count"] or 0),
            "oldest_deferred_at_utc": str(
                row["oldest_deferred_at_utc"] or ""
            ),
        }

    def release_recurring_preflight(
        self,
        run_id: Any,
        *,
        error_code: Any,
        error_message: Any,
        execution_token: Any = "",
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        normalized_error_code = _required_text(
            error_code,
            "预检错误码",
            96,
        )
        normalized_error_message = redact_text(error_message)
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        timestamp = self._now_iso()
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            if run["status"] == "preflight_failed":
                return self._recurring_run_result(conn, run)
            self._assert_recurring_execution(
                run,
                execution_token,
                now_iso=timestamp,
            )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_queue
                WHERE idempotency_key=?
                LIMIT 1
                """,
                (str(run["run_key"]),),
            ).fetchone():
                raise TTPostError(
                    "tt_post_preflight_release_invalid",
                    "每日发布运行已有持久队列，不能释放预检素材",
                    409,
                )
            updated = conn.execute(
                """
                UPDATE tt_post_recurring_pool
                SET status='available',run_id=NULL,updated_by_user_id=?,
                    updated_by_name=?,updated_at=?,reserved_at_utc=''
                WHERE id=? AND status='reserved' AND run_id=?
                    AND queue_id IS NULL
                """,
                (
                    normalized_actor_id,
                    normalized_actor_name,
                    timestamp,
                    int(run["pool_item_id"]),
                    normalized_run_id,
                ),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_recurring_pool_conflict",
                    "每日发布素材预检释放失败",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_schedule_run
                SET status='preflight_failed',error_code=?,error_message=?,
                    updated_at=?,finished_at_utc=?,
                    execution_token='',execution_lease_expires_at_utc=''
                WHERE id=?
                """,
                (
                    normalized_error_code,
                    normalized_error_message,
                    timestamp,
                    timestamp,
                    normalized_run_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    @staticmethod
    def _sync_recurring_rows(
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        pool: sqlite3.Row,
        queue: sqlite3.Row,
        timestamp: str,
    ) -> None:
        queue_status = str(queue["status"])
        if queue_status not in QUEUE_STATUSES:
            raise TTPostError(
                "tt_post_queue_status_invalid",
                "一次性发布队列状态无效",
                500,
            )
        terminal = queue_status in TERMINAL_QUEUE_STATUSES
        if pool["status"] == "consumed" and not terminal:
            raise TTPostError(
                "tt_post_recurring_storage_invalid",
                "每日发布素材已消费但队列仍处于活跃状态",
                500,
            )
        pool_status = "consumed" if terminal else "reserved"
        conn.execute(
            """
            UPDATE tt_post_recurring_pool
            SET status=?,queue_id=?,updated_at=?,
                consumed_at_utc=CASE WHEN ? THEN ? ELSE consumed_at_utc END
            WHERE id=?
            """,
            (
                pool_status,
                int(queue["id"]),
                timestamp,
                int(terminal),
                timestamp,
                int(pool["id"]),
            ),
        )
        conn.execute(
            """
            UPDATE tt_post_schedule_run
            SET queue_id=?,status=?,error_code=?,error_message=?,
                updated_at=?,
                execution_token='',execution_lease_expires_at_utc='',
                bound_at_utc=CASE
                    WHEN bound_at_utc='' THEN ? ELSE bound_at_utc END,
                finished_at_utc=CASE
                    WHEN ? THEN ? ELSE '' END
            WHERE id=?
            """,
            (
                int(queue["id"]),
                queue_status,
                str(queue["error_code"] or "")[:96],
                redact_text(queue["error_message"]),
                timestamp,
                timestamp,
                int(terminal),
                timestamp,
                int(run["id"]),
            ),
        )

    def bind_recurring_queue(
        self,
        run_id: Any,
        queue_id: Any,
        *,
        execution_token: Any = "",
    ) -> Dict[str, Any]:
        normalized_run_id = _positive_int(run_id, "每日发布运行ID")
        normalized_queue_id = _positive_int(queue_id, "一次性发布队列ID")
        timestamp = self._now_iso()
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "每日发布运行不存在",
                    404,
                )
            queue = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized_queue_id,),
            ).fetchone()
            if queue is None:
                raise TTPostError(
                    "tt_post_queue_not_found",
                    "TikTok发布队列不存在",
                    404,
                )
            pool = conn.execute(
                "SELECT * FROM tt_post_recurring_pool WHERE id=?",
                (int(run["pool_item_id"]),),
            ).fetchone()
            if pool is None:
                raise TTPostError(
                    "tt_post_recurring_storage_invalid",
                    "每日发布运行记录缺少素材",
                    500,
                )
            if run["queue_id"] is not None and int(run["queue_id"]) != normalized_queue_id:
                raise TTPostError(
                    "tt_post_recurring_queue_conflict",
                    "每日发布运行已绑定其他队列",
                    409,
                )
            if (
                str(queue["account_id"]) != str(run["account_id"])
                or str(queue["material_id"]) != str(pool["material_id"])
                or str(queue["idempotency_key"]) != str(run["run_key"])
                or str(queue["scheduled_at_utc"])
                != str(run["scheduled_at_utc"])
            ):
                raise TTPostError(
                    "tt_post_recurring_queue_mismatch",
                    "每日发布运行与一次性队列冻结身份不匹配",
                    409,
                )
            if run["queue_id"] is None:
                self._assert_recurring_execution(
                    run,
                    execution_token,
                    now_iso=timestamp,
                )
            if (
                pool["status"] not in {"reserved", "consumed"}
                or int(pool["run_id"] or 0) != normalized_run_id
            ):
                raise TTPostError(
                    "tt_post_recurring_pool_conflict",
                    "每日发布素材与运行绑定关系无效",
                    409,
                )
            self._sync_recurring_rows(conn, run, pool, queue, timestamp)
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (normalized_run_id,),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    def sync_recurring_from_queue(self, queue_id: Any) -> Dict[str, Any]:
        normalized_queue_id = _positive_int(queue_id, "一次性发布队列ID")
        timestamp = self._now_iso()
        with self._transaction() as conn:
            queue = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized_queue_id,),
            ).fetchone()
            if queue is None:
                raise TTPostError(
                    "tt_post_queue_not_found",
                    "TikTok发布队列不存在",
                    404,
                )
            run = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE queue_id=?",
                (normalized_queue_id,),
            ).fetchone()
            if run is None:
                raise TTPostError(
                    "tt_post_schedule_run_not_found",
                    "该队列没有每日发布运行记录",
                    404,
                )
            pool = conn.execute(
                "SELECT * FROM tt_post_recurring_pool WHERE id=?",
                (int(run["pool_item_id"]),),
            ).fetchone()
            if pool is None:
                raise TTPostError(
                    "tt_post_recurring_storage_invalid",
                    "每日发布运行记录缺少素材",
                    500,
                )
            self._sync_recurring_rows(conn, run, pool, queue, timestamp)
            row = conn.execute(
                "SELECT * FROM tt_post_schedule_run WHERE id=?",
                (int(run["id"]),),
            ).fetchone()
            return self._recurring_run_result(conn, row)

    def add_material(
        self,
        material_id: Any,
        *,
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        normalized = _material_id(material_id)
        actor_user_id = _optional_text(actor_user_id, "操作人ID", 128)
        actor_name = _optional_text(actor_name, "操作人名称", 255)
        timestamp = self._now_iso()
        with self._transaction() as conn:
            if conn.execute(
                """
                SELECT 1 FROM tt_post_material_intake
                WHERE material_id=?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_already_exists",
                    "素材已存在于TikTok发布池或历史中",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_material_pool(
                        material_id,status,created_by_user_id,created_by_name,
                        created_at,updated_at
                    ) VALUES(?,'available',?,?,?,?)
                    """,
                    (
                        normalized,
                        actor_user_id,
                        actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_material_already_exists",
                    "素材已存在于TikTok发布池或历史中",
                    409,
                ) from None
            pool_id = int(cursor.lastrowid)
            self._event(
                conn,
                event_type="material_added",
                pool_item_id=pool_id,
                created_at=timestamp,
                to_status="available",
                details={"material_id": normalized, "actor_user_id": actor_user_id},
            )
            row = conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE id=?",
                (pool_id,),
            ).fetchone()
        return dict(row)

    def ensure_material_for_recurring(
        self,
        material_id: Any,
        recurring_pool_id: Any,
        *,
        actor_user_id: str = "",
        actor_name: str = "",
    ) -> Dict[str, Any]:
        """Bridge a reserved recurring row, preserving pre-intake legacy rows."""

        normalized_material_id = _material_id(material_id)
        normalized_recurring_id = _positive_int(
            recurring_pool_id,
            "每日发布素材池记录ID",
        )
        normalized_actor_id = _optional_text(
            actor_user_id,
            "操作人ID",
            128,
        )
        normalized_actor_name = _optional_text(
            actor_name,
            "操作人名称",
            255,
        )
        timestamp = self._now_iso()
        with self._transaction() as conn:
            recurring = conn.execute(
                """
                SELECT * FROM tt_post_recurring_pool
                WHERE id=?
                """,
                (normalized_recurring_id,),
            ).fetchone()
            if (
                recurring is None
                or str(recurring["material_id"]) != normalized_material_id
                or str(recurring["status"]) != "reserved"
            ):
                raise TTPostError(
                    "tt_post_recurring_material_bridge_invalid",
                    "每日发布素材尚未被当前运行安全预留",
                    409,
                )
            intake = conn.execute(
                """
                SELECT * FROM tt_post_material_intake
                WHERE material_id=?
                """,
                (normalized_material_id,),
            ).fetchone()
            if (
                intake is not None
                and (
                    str(intake["status"]) != "ready"
                    or int(intake["recurring_pool_id"] or 0)
                    != normalized_recurring_id
                )
            ):
                raise TTPostError(
                    "tt_post_material_intake_bridge_invalid",
                    "每日发布素材缺少已完成的预制作入池记录",
                    409,
                )
            existing = conn.execute(
                """
                SELECT * FROM tt_post_material_pool
                WHERE material_id=?
                """,
                (normalized_material_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) == "available":
                    return dict(existing)
                raise TTPostError(
                    "tt_post_material_bridge_conflict",
                    "每日发布素材的一次性队列桥接状态冲突",
                    409,
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO tt_post_material_pool(
                        material_id,status,created_by_user_id,created_by_name,
                        created_at,updated_at
                    ) VALUES(?,'available',?,?,?,?)
                    """,
                    (
                        normalized_material_id,
                        normalized_actor_id,
                        normalized_actor_name,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_material_bridge_conflict",
                    "每日发布素材的一次性队列桥接发生唯一性冲突",
                    409,
                ) from None
            pool_id = int(cursor.lastrowid)
            self._event(
                conn,
                event_type="recurring_material_bridge_created",
                pool_item_id=pool_id,
                created_at=timestamp,
                to_status="available",
                details={
                    "material_id": normalized_material_id,
                    "recurring_pool_id": normalized_recurring_id,
                    "actor_user_id": normalized_actor_id,
                },
            )
            row = conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE id=?",
                (pool_id,),
            ).fetchone()
        return dict(row)

    def get_material(self, pool_item_id: Any) -> Dict[str, Any]:
        pool_id = _positive_int(pool_item_id, "素材池记录ID")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE id=?",
                (pool_id,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_pool_item_not_found",
                "TikTok素材池记录不存在",
                404,
            )
        return dict(row)

    def list_materials(self) -> List[Dict[str, Any]]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tt_post_material_pool ORDER BY created_at,id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_material_by_material_id(self, material_id: Any) -> Dict[str, Any]:
        normalized = _material_id(material_id)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE material_id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_pool_item_not_found",
                "TikTok素材池记录不存在",
                404,
            )
        return dict(row)

    def freeze_queue(
        self,
        pool_item_id: Any,
        account: SafeAccount,
        scheduled_beijing: Any,
        caption_template: Any,
        policy: Any,
        material_resolver: Any,
        *,
        idempotency_key: str = "",
        is_aigc: bool = False,
        publish_mode: str = "hold",
        account_display_name: str = "",
        creator_nickname: str = "",
        creator_username: str = "",
        creator_info_hash: str = "",
        creator_info_synced_at: str = "",
        gpu_job_id: str = "",
        source_media_url: str = "",
        prepared_output_sha256: str = "",
        prepared_output_size: int = 0,
        prepared_duration_sec: float = 0.0,
        source_trim_tail_seconds: float = 0.0,
        recurring_run_id: Any = None,
        recurring_execution_token: Any = "",
        material_name: Any = "",
        drama_name: Any = "",
        material_language: Any = "",
        material_tag: Any = "",
        description: Any = "",
    ) -> Dict[str, Any]:
        """Resolve the material and freeze all mutable publish inputs."""

        pool_id = _positive_int(pool_item_id, "素材池记录ID")
        if not isinstance(account, SafeAccount):
            raise TTPostError("invalid_account", "TikTok安全账号DTO无效", 400)
        if account.status != "active" or not account.publish_eligible:
            raise TTPostError(
                "tt_account_not_publishable",
                "TikTok账号当前不可用于发布",
                409,
            )
        scheduled_at_utc = beijing_to_utc(scheduled_beijing)
        normalized_policy = (
            policy if isinstance(policy, TTPostPolicy) else TTPostPolicy.from_mapping(policy)
        )
        normalized_is_aigc = _exact_bool(is_aigc, "AI内容声明")
        normalized_publish_mode = str(publish_mode or "").strip()
        if normalized_publish_mode not in {"hold", "direct_post"}:
            raise TTPostError("invalid_publish_mode", "TikTok发布模式无效", 400)
        frozen_account_display_name = _optional_text(
            account_display_name or account.display_name,
            "TikTok账号显示名称",
            255,
        )
        frozen_creator_nickname = _optional_text(
            creator_nickname,
            "TikTok Creator昵称",
            255,
        )
        frozen_creator_username = _optional_text(
            creator_username,
            "TikTok Creator用户名",
            255,
        )
        frozen_creator_hash = str(creator_info_hash or "").strip().lower()
        if frozen_creator_hash and not _SHA256_RE.fullmatch(frozen_creator_hash):
            raise TTPostError(
                "invalid_creator_info_hash",
                "TikTok Creator能力指纹无效",
                400,
            )
        frozen_creator_synced_at = (
            _iso_utc(creator_info_synced_at, "TikTok Creator确认时间")
            if creator_info_synced_at
            else ""
        )
        frozen_gpu_job_id = str(gpu_job_id or "").strip()
        if frozen_gpu_job_id and not _GPU_JOB_ID_RE.fullmatch(frozen_gpu_job_id):
            raise TTPostError("invalid_gpu_job_id", "TT GPU任务ID无效", 400)
        frozen_source_url = (
            _https_url(source_media_url, "源素材视频地址")
            if source_media_url
            else ""
        )
        frozen_output_sha = str(prepared_output_sha256 or "").strip().lower()
        if frozen_output_sha and not _SHA256_RE.fullmatch(frozen_output_sha):
            raise TTPostError(
                "invalid_prepared_output_sha",
                "TT最终成片指纹无效",
                400,
            )
        if isinstance(prepared_output_size, bool):
            raise TTPostError("invalid_prepared_output_size", "TT最终成片大小无效", 400)
        try:
            frozen_output_size = int(prepared_output_size)
            frozen_duration = float(prepared_duration_sec)
            frozen_trim = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError(
                "invalid_prepared_media_metadata",
                "TT最终成片元数据无效",
                400,
            ) from None
        if (
            frozen_output_size < 0
            or frozen_duration < 0
            or frozen_trim < 0
            or frozen_duration != frozen_duration
            or frozen_trim != frozen_trim
        ):
            raise TTPostError(
                "invalid_prepared_media_metadata",
                "TT最终成片元数据无效",
                400,
            )
        timestamp = self._now_iso()

        with contextlib.closing(_connect(self.db_path)) as read_conn:
            pool = read_conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE id=?",
                (pool_id,),
            ).fetchone()
        if pool is None:
            raise TTPostError(
                "tt_post_pool_item_not_found",
                "TikTok素材池记录不存在",
                404,
            )
        resolution = resolve_material(material_resolver, pool["material_id"])
        if frozen_source_url and secrets.compare_digest(
            resolution.media_url,
            frozen_source_url,
        ):
            raise TTPostError(
                "tt_prepared_media_matches_source",
                "TT最终成片地址不能与源素材地址相同",
                409,
            )
        frozen_caption_template = str(caption_template or "").strip()
        frozen_material_name = _optional_text(
            material_name,
            "素材名称",
            500,
        )
        frozen_drama_name = _optional_text(
            drama_name,
            "短剧名称",
            500,
        )
        frozen_material_language = _optional_text(
            material_language,
            "素材语言",
            32,
        )
        frozen_material_tag = _optional_text(
            material_tag,
            "素材标签",
            255,
        )
        try:
            frozen_description = _normalize_description(description)
        except ValueError:
            raise TTPostError("invalid_request", "素材描述无效", 400) from None
        has_url_macro = caption_uses_url_macro(frozen_caption_template)
        if has_url_macro and not all(
            (
                frozen_material_name,
                frozen_drama_name,
                frozen_material_language,
                frozen_material_tag,
                account.username,
                frozen_account_display_name,
            )
        ):
            raise TTPostError(
                "tt_post_link_metadata_incomplete",
                "{url}短链所需的TikTok归因信息不完整",
                409,
            )
        frozen_short_link_id = short_link_id(pool_id) if has_url_macro else 0
        frozen_short_url = (
            build_short_url(frozen_short_link_id) if has_url_macro else ""
        )
        caption = render_caption_template(
            frozen_caption_template,
            resolution.content_id,
            url=frozen_short_url if has_url_macro else None,
            description=frozen_description,
        )
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            normalized_key = "tt-post:%s:%s:%s" % (
                pool_id,
                account.account_id,
                scheduled_at_utc,
            )
        normalized_key = _required_text(normalized_key, "幂等键", 255)
        has_recurring_run = recurring_run_id not in (None, "")
        normalized_execution_token = str(
            recurring_execution_token or ""
        )
        if has_recurring_run != bool(normalized_execution_token):
            raise TTPostError(
                "tt_post_recurring_execution_invalid",
                "每日发布队列必须同时提供运行ID和执行租约",
                409,
            )
        normalized_recurring_run_id = (
            _positive_int(recurring_run_id, "每日发布运行ID")
            if has_recurring_run
            else None
        )

        with self._transaction() as conn:
            if normalized_recurring_run_id is not None:
                recurring_run = conn.execute(
                    "SELECT * FROM tt_post_schedule_run WHERE id=?",
                    (normalized_recurring_run_id,),
                ).fetchone()
                if recurring_run is None:
                    raise TTPostError(
                        "tt_post_schedule_run_not_found",
                        "每日发布运行不存在",
                        404,
                    )
                self._assert_recurring_execution(
                    recurring_run,
                    normalized_execution_token,
                    now_iso=self._now_iso(),
                )
                recurring_pool = conn.execute(
                    """
                    SELECT * FROM tt_post_recurring_pool
                    WHERE id=?
                    """,
                    (int(recurring_run["pool_item_id"]),),
                ).fetchone()
                if (
                    recurring_pool is None
                    or str(recurring_pool["status"]) != "reserved"
                    or int(recurring_pool["run_id"] or 0)
                    != normalized_recurring_run_id
                    or recurring_pool["queue_id"] is not None
                    or str(recurring_pool["material_id"])
                    != str(resolution.material_id)
                    or str(recurring_run["run_key"]) != normalized_key
                    or str(recurring_run["account_id"])
                    != str(account.account_id)
                    or str(recurring_run["scheduled_at_utc"])
                    != scheduled_at_utc
                ):
                    raise TTPostError(
                        "tt_post_recurring_execution_fence",
                        "每日发布运行、素材和冻结队列身份不一致",
                        409,
                    )
            existing = conn.execute(
                "SELECT * FROM tt_post_queue WHERE idempotency_key=?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["pool_item_id"]) == pool_id
                    and secrets.compare_digest(
                        str(existing["account_id"]),
                        account.account_id,
                    )
                    and str(existing["scheduled_at_utc"]) == scheduled_at_utc
                    and str(existing["content_id"]) == resolution.content_id
                    and str(existing["caption_template"])
                    == frozen_caption_template
                    and str(existing["caption"]) == caption
                    and str(existing["material_name"]) == frozen_material_name
                    and str(existing["drama_name"]) == frozen_drama_name
                    and str(existing["material_language"])
                    == frozen_material_language
                    and str(existing["material_tag"]) == frozen_material_tag
                    and str(existing["description"]) == frozen_description
                    and int(existing["short_link_id"] or 0)
                    == frozen_short_link_id
                    and str(existing["short_url"]) == frozen_short_url
                    and str(existing["privacy_level"])
                    == normalized_policy.privacy_level
                    and bool(existing["allow_comment"])
                    == normalized_policy.allow_comment
                    and bool(existing["allow_duet"])
                    == normalized_policy.allow_duet
                    and bool(existing["allow_stitch"])
                    == normalized_policy.allow_stitch
                    and bool(existing["brand_content_toggle"])
                    == normalized_policy.brand_content_toggle
                    and bool(existing["brand_organic_toggle"])
                    == normalized_policy.brand_organic_toggle
                    and bool(existing["user_consent"])
                    == normalized_policy.user_consent
                    and str(existing["consent_version"])
                    == normalized_policy.consent_version
                    and str(existing["consented_at_utc"])
                    == normalized_policy.consented_at_utc
                    and bool(existing["is_aigc"]) == normalized_is_aigc
                    and str(existing["publish_mode"])
                    == normalized_publish_mode
                ):
                    return _public_queue(existing)
                raise TTPostError(
                    "tt_post_idempotency_conflict",
                    "幂等键已用于不同TikTok排期",
                    409,
                )

            current_pool = conn.execute(
                "SELECT * FROM tt_post_material_pool WHERE id=?",
                (pool_id,),
            ).fetchone()
            if current_pool is None:
                raise TTPostError(
                    "tt_post_pool_item_not_found",
                    "TikTok素材池记录不存在",
                    404,
                )
            if current_pool["status"] != "available":
                raise TTPostError(
                    "tt_post_pool_item_unavailable",
                    "TikTok素材已冻结或已有发布历史",
                    409,
                )
            if conn.execute(
                "SELECT 1 FROM tt_post_queue WHERE material_id=?",
                (resolution.material_id,),
            ).fetchone():
                raise TTPostError(
                    "tt_post_material_already_used",
                    "素材已有TikTok排期或发布历史",
                    409,
                )
            if conn.execute(
                """
                SELECT 1 FROM tt_post_queue
                WHERE account_id=? AND scheduled_at_utc=?
                """,
                (account.account_id, scheduled_at_utc),
            ).fetchone():
                raise TTPostError(
                    "tt_post_account_time_conflict",
                    "同一TikTok账号在该时间点已有排期",
                    409,
                )

            try:
                insert_columns = (
                    "idempotency_key",
                    "pool_item_id",
                    "material_id",
                    "content_id",
                    "media_url",
                    "source_media_url",
                    "material_name",
                    "drama_name",
                    "material_language",
                    "material_tag",
                    "description",
                    "account_id",
                    "account_username",
                    "account_display_name",
                    "creator_nickname_snapshot",
                    "creator_username_snapshot",
                    "creator_info_hash",
                    "creator_info_synced_at_utc",
                    "scheduled_at_utc",
                    "caption_template",
                    "caption",
                    "short_link_id",
                    "short_url",
                    "long_url",
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "user_consent",
                    "consent_version",
                    "consented_at_utc",
                    "is_aigc",
                    "publish_mode",
                    "gpu_job_id",
                    "prepared_output_sha256",
                    "prepared_output_size",
                    "prepared_duration_sec",
                    "source_trim_tail_seconds",
                    "status",
                    "created_at",
                    "updated_at",
                )
                insert_values = (
                    normalized_key,
                    pool_id,
                    resolution.material_id,
                    resolution.content_id,
                    resolution.media_url,
                    frozen_source_url,
                    frozen_material_name,
                    frozen_drama_name,
                    frozen_material_language,
                    frozen_material_tag,
                    frozen_description,
                    account.account_id,
                    account.username,
                    frozen_account_display_name,
                    frozen_creator_nickname,
                    frozen_creator_username,
                    frozen_creator_hash,
                    frozen_creator_synced_at,
                    scheduled_at_utc,
                    frozen_caption_template,
                    caption,
                    frozen_short_link_id,
                    frozen_short_url,
                    "",
                    normalized_policy.privacy_level,
                    int(normalized_policy.allow_comment),
                    int(normalized_policy.allow_duet),
                    int(normalized_policy.allow_stitch),
                    int(normalized_policy.brand_content_toggle),
                    int(normalized_policy.brand_organic_toggle),
                    int(normalized_policy.user_consent),
                    normalized_policy.consent_version,
                    normalized_policy.consented_at_utc,
                    int(normalized_is_aigc),
                    normalized_publish_mode,
                    frozen_gpu_job_id,
                    frozen_output_sha,
                    frozen_output_size,
                    frozen_duration,
                    frozen_trim,
                    "scheduled",
                    timestamp,
                    timestamp,
                )
                cursor = conn.execute(
                    "INSERT INTO tt_post_queue(%s) VALUES(%s)"
                    % (
                        ",".join(insert_columns),
                        ",".join("?" for _item in insert_columns),
                    ),
                    insert_values,
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_storage_conflict",
                    "TikTok排期发生唯一性冲突，请刷新后重试",
                    409,
                ) from None
            queue_id = int(cursor.lastrowid)
            updated = conn.execute(
                """
                UPDATE tt_post_material_pool
                SET status='reserved',updated_at=?
                WHERE id=? AND status='available'
                """,
                (timestamp, pool_id),
            )
            if updated.rowcount != 1:
                raise TTPostError(
                    "tt_post_storage_conflict",
                    "TikTok素材状态已变更",
                    409,
                )
            self._event(
                conn,
                event_type="queue_frozen",
                queue_id=queue_id,
                pool_item_id=pool_id,
                created_at=timestamp,
                from_status="",
                to_status="scheduled",
                details={
                    "material_id": resolution.material_id,
                    "content_id": resolution.content_id,
                    "account_id": account.account_id,
                    "scheduled_at_utc": scheduled_at_utc,
                },
            )
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (queue_id,),
            ).fetchone()
        return _public_queue(row)

    def get_queue(self, queue_id: Any) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_queue_not_found",
                "TikTok发布队列不存在",
                404,
            )
        return _public_queue(row)

    def list_queues(self) -> List[Dict[str, Any]]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tt_post_queue ORDER BY scheduled_at_utc,id"
            ).fetchall()
        return [_public_queue(row) for row in rows]

    def list_publish_tasks(
        self,
        *,
        material_id: Any = None,
        account_id: Any = None,
        status: Any = None,
        task_type: Any = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return queue and direct-test tasks from one read snapshot."""

        normalized_limit = _positive_int(limit, "发布任务列表数量", 100)
        normalized_offset = _nonnegative_int(
            offset,
            "发布任务列表偏移",
            2**31 - 1,
        )
        normalized_material = (
            _material_id(material_id) if material_id not in (None, "") else ""
        )
        normalized_account = (
            _account_id(account_id) if account_id not in (None, "") else ""
        )
        normalized_type = str(task_type or "all").strip().lower()
        if normalized_type not in {"all", "automatic", "direct_test"}:
            raise TTPostError(
                "invalid_request",
                "发布任务类型无效",
                400,
            )
        normalized_status = str(status or "").strip().lower()
        allowed_statuses = {
            "",
            "scheduled",
            "queued",
            "preparing",
            "ready",
            "claimed",
            "processing_download",
            "publishing",
            "reconciling",
            "published",
            "failed",
            "needs_review",
            "unknown",
            "missed",
            "hold",
            "blocked_compliance",
            "canceled",
            "cancelled",
        }
        if normalized_status not in allowed_statuses:
            raise TTPostError(
                "invalid_request",
                "发布任务状态无效",
                400,
            )

        clauses: List[str] = []
        params: List[Any] = []
        if normalized_material:
            clauses.append("material_id=?")
            params.append(normalized_material)
        if normalized_account:
            clauses.append("account_id=?")
            params.append(normalized_account)
        if normalized_type != "all":
            clauses.append("task_type=?")
            params.append(normalized_type)
        if normalized_status == "scheduled":
            clauses.append("status_group='scheduled'")
        elif normalized_status == "processing_download":
            clauses.append("status_group='processing'")
        elif normalized_status in {"needs_review", "unknown"}:
            clauses.append("status_group='needs_review'")
        elif normalized_status in {"hold", "blocked_compliance"}:
            clauses.append("raw_status='blocked_compliance'")
        elif normalized_status in {"canceled", "cancelled"}:
            clauses.append("raw_status='canceled'")
        elif normalized_status:
            clauses.append("raw_status=?")
            params.append(normalized_status)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        task_cte = """
            WITH publish_tasks AS (
                SELECT
                    'automatic' AS task_type,
                    id AS task_id,
                    scheduled_at_utc AS task_at_utc,
                    material_id,
                    account_id,
                    status AS raw_status,
                    unknown_outcome
                FROM tt_post_queue
                UNION ALL
                SELECT
                    'direct_test' AS task_type,
                    id AS task_id,
                    created_at AS task_at_utc,
                    material_id,
                    account_id,
                    status AS raw_status,
                    unknown_outcome
                FROM tt_post_direct_test
            ), classified AS (
                SELECT
                    *,
                    CASE
                        WHEN unknown_outcome=1 OR raw_status='unknown'
                            THEN 'needs_review'
                        WHEN raw_status='published' THEN 'published'
                        WHEN task_type='automatic' AND raw_status='scheduled'
                            THEN 'scheduled'
                        WHEN (
                            task_type='automatic'
                            AND raw_status IN (
                                'claimed','publishing','reconciling'
                            )
                        ) OR (
                            task_type='direct_test'
                            AND raw_status IN (
                                'queued','preparing','ready',
                                'publishing','reconciling'
                            )
                        ) THEN 'processing'
                        ELSE 'other'
                    END AS status_group
                FROM publish_tasks
            )
        """
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN")
            summary_row = conn.execute(
                task_cte
                + """
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(status_group='scheduled'),0) AS scheduled,
                        COALESCE(SUM(status_group='processing'),0) AS processing,
                        COALESCE(SUM(status_group='needs_review'),0)
                            AS needs_review,
                        COALESCE(SUM(status_group='published'),0) AS published
                    FROM classified
                """
                + where_sql,
                params,
            ).fetchone()
            refs = conn.execute(
                task_cte
                + """
                    SELECT
                        task_type,task_id,task_at_utc,raw_status,status_group
                    FROM classified
                """
                + where_sql
                + """
                    ORDER BY task_at_utc DESC,task_type,task_id DESC
                    LIMIT ? OFFSET ?
                """,
                [*params, normalized_limit, normalized_offset],
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for ref in refs:
                is_direct = str(ref["task_type"]) == "direct_test"
                table = (
                    "tt_post_direct_test" if is_direct else "tt_post_queue"
                )
                row = conn.execute(
                    "SELECT * FROM %s WHERE id=?" % table,
                    (int(ref["task_id"]),),
                ).fetchone()
                if row is None:
                    raise TTPostError(
                        "tt_post_storage_conflict",
                        "发布任务读取期间发生变化，请刷新后重试",
                        409,
                    )
                items.append(
                    {
                        "task_type": str(ref["task_type"]),
                        "task_id": int(ref["task_id"]),
                        "task_key": "%s:%s"
                        % (str(ref["task_type"]), int(ref["task_id"])),
                        "task_at_utc": str(ref["task_at_utc"]),
                        "raw_status": str(ref["raw_status"]),
                        "status_group": str(ref["status_group"]),
                        "item": (
                            _public_direct_test(row)
                            if is_direct
                            else _public_queue(row)
                        ),
                    }
                )
        summary = dict(summary_row or {})
        return {
            "items": items,
            "total": int(summary.get("total") or 0),
            "summary": {
                "total": int(summary.get("total") or 0),
                "scheduled": int(summary.get("scheduled") or 0),
                "processing": int(summary.get("processing") or 0),
                "needs_review": int(summary.get("needs_review") or 0),
                "published": int(summary.get("published") or 0),
            },
        }

    def get_queue_by_idempotency_key(self, idempotency_key: Any) -> Dict[str, Any]:
        normalized = _required_text(idempotency_key, "幂等键", 255)
        with contextlib.closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE idempotency_key=?",
                (normalized,),
            ).fetchone()
        if row is None:
            raise TTPostError(
                "tt_post_queue_not_found",
                "TikTok发布队列不存在",
                404,
            )
        return _public_queue(row)

    def list_reconciling(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        normalized_limit = _positive_int(limit, "核对任务数量", 100)
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tt_post_queue
                WHERE status='reconciling' AND publish_id<>''
                ORDER BY updated_at,id
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        return [_public_queue(row) for row in rows]

    @staticmethod
    def _assert_claim(
        row: sqlite3.Row,
        claim_token: Any,
        *,
        allowed_statuses: Sequence[str],
        now_iso: Optional[str] = None,
    ) -> None:
        supplied = str(claim_token or "")
        stored = str(row["claim_token"] or "")
        if (
            not supplied
            or not stored
            or not secrets.compare_digest(supplied, stored)
            or row["status"] not in set(allowed_statuses)
        ):
            raise TTPostError(
                "tt_post_claim_invalid",
                "TikTok发布认领无效或状态已变更",
                409,
            )
        if (
            now_iso is not None
            and row["lease_expires_at_utc"]
            and str(row["lease_expires_at_utc"]) <= now_iso
        ):
            raise TTPostError(
                "tt_post_claim_expired",
                "TikTok发布认领已过期",
                409,
            )

    def claim_due(
        self,
        worker_id: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 300,
        grace_seconds: int = 90,
        limit: int = 10,
    ) -> List[QueueClaim]:
        """Claim due rows, terminalize missed rows and quarantine stale publishing."""

        worker = str(worker_id or "").strip()
        if not _WORKER_ID_RE.fullmatch(worker):
            raise TTPostError("invalid_worker_id", "发布执行器ID无效", 400)
        lease_seconds = _positive_int(lease_seconds, "认领时长", 86400)
        if isinstance(grace_seconds, bool):
            raise TTPostError("invalid_request", "补偿窗口无效", 400)
        try:
            grace_seconds = int(grace_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TTPostError("invalid_request", "补偿窗口无效", 400) from None
        if grace_seconds < 0 or grace_seconds > 86400:
            raise TTPostError("invalid_request", "补偿窗口无效", 400)
        limit = _positive_int(limit, "认领数量", 100)
        current = _utc_datetime(now if now is not None else self._now_fn(), "当前时间")
        now_iso = _iso_utc(current)
        cutoff_iso = _iso_utc(current - timedelta(seconds=grace_seconds))
        lease_iso = _iso_utc(current + timedelta(seconds=lease_seconds))
        claims: List[QueueClaim] = []

        with self._transaction() as conn:
            stale_publishing = conn.execute(
                """
                SELECT * FROM tt_post_queue
                WHERE status='publishing'
                  AND lease_expires_at_utc<>''
                  AND lease_expires_at_utc<=?
                ORDER BY id
                """,
                (now_iso,),
            ).fetchall()
            for row in stale_publishing:
                conn.execute(
                    """
                    UPDATE tt_post_queue
                    SET status='unknown',unknown_outcome=1,
                        claim_worker='',claim_token='',lease_expires_at_utc='',
                        error_code='tt_post_publish_lease_expired',
                        error_message='发布执行中租约过期，结果需要人工核对',
                        updated_at=?
                    WHERE id=? AND status='publishing'
                    """,
                    (now_iso, row["id"]),
                )
                self._event(
                    conn,
                    event_type="publish_outcome_unknown",
                    queue_id=int(row["id"]),
                    pool_item_id=int(row["pool_item_id"]),
                    created_at=now_iso,
                    from_status="publishing",
                    to_status="unknown",
                    message="发布执行中租约过期，结果需要人工核对",
                )

            missed = conn.execute(
                """
                SELECT * FROM tt_post_queue
                WHERE scheduled_at_utc<?
                  AND (
                    status='scheduled'
                    OR (
                        status='claimed'
                        AND lease_expires_at_utc<>''
                        AND lease_expires_at_utc<=?
                    )
                  )
                ORDER BY scheduled_at_utc,id
                """,
                (cutoff_iso, now_iso),
            ).fetchall()
            for row in missed:
                updated = conn.execute(
                    """
                    UPDATE tt_post_queue
                    SET status='missed',claim_worker='',claim_token='',
                        lease_expires_at_utc='',
                        error_code='tt_post_schedule_missed',
                        error_message='发布时间已超过允许窗口',
                        updated_at=?
                    WHERE id=? AND status=?
                    """,
                    (now_iso, row["id"], row["status"]),
                )
                if updated.rowcount != 1:
                    continue
                conn.execute(
                    """
                    UPDATE tt_post_material_pool
                    SET status='canceled',updated_at=?
                    WHERE id=? AND status='reserved'
                    """,
                    (now_iso, row["pool_item_id"]),
                )
                self._event(
                    conn,
                    event_type="queue_missed",
                    queue_id=int(row["id"]),
                    pool_item_id=int(row["pool_item_id"]),
                    created_at=now_iso,
                    from_status=str(row["status"]),
                    to_status="missed",
                )

            candidates = conn.execute(
                """
                SELECT * FROM tt_post_queue
                WHERE scheduled_at_utc>=?
                  AND scheduled_at_utc<=?
                  AND (
                    status='scheduled'
                    OR (
                        status='claimed'
                        AND lease_expires_at_utc<>''
                        AND lease_expires_at_utc<=?
                    )
                  )
                ORDER BY scheduled_at_utc,id
                LIMIT ?
                """,
                (cutoff_iso, now_iso, now_iso, limit),
            ).fetchall()
            for row in candidates:
                claim_token = secrets.token_urlsafe(32)
                prior_status = str(row["status"])
                updated = conn.execute(
                    """
                    UPDATE tt_post_queue
                    SET status='claimed',claim_worker=?,claim_token=?,
                        lease_expires_at_utc=?,attempt_count=attempt_count+1,
                        error_code='',error_message='',updated_at=?
                    WHERE id=? AND status=?
                    """,
                    (
                        worker,
                        claim_token,
                        lease_iso,
                        now_iso,
                        row["id"],
                        prior_status,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed = conn.execute(
                    "SELECT * FROM tt_post_queue WHERE id=?",
                    (row["id"],),
                ).fetchone()
                self._event(
                    conn,
                    event_type="queue_claimed",
                    queue_id=int(row["id"]),
                    pool_item_id=int(row["pool_item_id"]),
                    created_at=now_iso,
                    from_status=prior_status,
                    to_status="claimed",
                    details={
                        "worker_id": worker,
                        "lease_expires_at_utc": lease_iso,
                        "attempt_count": int(claimed["attempt_count"]),
                    },
                )
                claims.append(QueueClaim(_public_queue(claimed), claim_token))
        return claims

    def renew_claim(
        self,
        queue_id: Any,
        claim_token: Any,
        *,
        now: Optional[Any] = None,
        lease_seconds: int = 300,
    ) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        lease_seconds = _positive_int(lease_seconds, "认领时长", 86400)
        current = _utc_datetime(now if now is not None else self._now_fn(), "当前时间")
        now_iso = _iso_utc(current)
        lease_iso = _iso_utc(current + timedelta(seconds=lease_seconds))
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("claimed", "publishing"),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET lease_expires_at_utc=?,updated_at=?
                WHERE id=?
                """,
                (lease_iso, now_iso, normalized),
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def prepare_short_link(
        self,
        queue_id: Any,
        claim_token: Any,
        long_url: Any,
    ) -> Dict[str, Any]:
        """Freeze the exact W2A target before the remote publish boundary."""

        normalized = _positive_int(queue_id, "发布队列ID")
        try:
            normalized_long_url = validate_w2a_url(long_url)
        except Exception as exc:
            raise TTPostError(
                str(getattr(exc, "code", "tt_short_link_target_invalid")),
                str(exc),
                int(getattr(exc, "status", 400)),
            ) from None
        now_iso = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError(
                    "tt_post_queue_not_found",
                    "TikTok发布队列不存在",
                    404,
                )
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("claimed",),
                now_iso=now_iso,
            )
            if (
                int(row["short_link_id"] or 0) <= 0
                or not str(row["short_url"] or "")
            ):
                raise TTPostError(
                    "tt_short_link_not_required",
                    "TikTok发布描述没有待准备的短链",
                    409,
                )
            existing = str(row["long_url"] or "")
            if existing:
                if not secrets.compare_digest(existing, normalized_long_url):
                    raise TTPostError(
                        "tt_short_link_target_conflict",
                        "TikTok短链目标已冻结且不同",
                        409,
                    )
                return _public_queue(row)
            conn.execute(
                """
                UPDATE tt_post_queue
                SET long_url=?,updated_at=?
                WHERE id=? AND status='claimed' AND claim_token=?
                """,
                (
                    normalized_long_url,
                    now_iso,
                    normalized,
                    str(claim_token or ""),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def begin_publish(
        self,
        queue_id: Any,
        claim_token: Any,
        gates: Optional[LiveGates] = None,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Cross the final local boundary immediately before network publishing."""

        live_gates = LiveGates() if gates is None else gates
        if not isinstance(live_gates, LiveGates):
            raise TTPostError("invalid_live_gates", "正式发布门禁无效", 500)
        live_gates.assert_open()
        return self._begin_publish_authorized(
            queue_id,
            claim_token,
            now=now,
        )

    def begin_manual_canary_publish(
        self,
        queue_id: Any,
        claim_token: Any,
        identity: Mapping[str, Any],
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Cross the network boundary for one explicitly bound private canary."""

        required = {
            "canary_id",
            "account_id",
            "pool_id",
            "material_id",
            "content_id",
            "gpu_job_id",
            "output_sha256",
            "output_size",
            "profile",
        }
        if not isinstance(identity, Mapping) or set(identity) != required:
            raise TTPostError(
                "tt_post_manual_canary_identity_invalid",
                "一次性私密测试身份无效",
                500,
            )
        return self._begin_publish_authorized(
            queue_id,
            claim_token,
            now=now,
            manual_canary_identity=dict(identity),
        )

    def _begin_publish_authorized(
        self,
        queue_id: Any,
        claim_token: Any,
        *,
        now: Optional[Any] = None,
        manual_canary_identity: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("claimed",),
                now_iso=now_iso,
            )
            if manual_canary_identity is not None:
                identity = manual_canary_identity
                run = conn.execute(
                    """
                    SELECT * FROM tt_post_schedule_run
                    WHERE queue_id=?
                    """,
                    (normalized,),
                ).fetchone()
                pool = (
                    conn.execute(
                        "SELECT * FROM tt_post_recurring_pool WHERE id=?",
                        (int(run["pool_item_id"]),),
                    ).fetchone()
                    if run is not None
                    else None
                )
                enabled_schedule = conn.execute(
                    """
                    SELECT 1 FROM tt_post_daily_schedule
                    WHERE account_id=? AND enabled=1
                    LIMIT 1
                    """,
                    (str(identity.get("account_id") or ""),),
                ).fetchone()
                canary_id = str(identity.get("canary_id") or "")
                expected_prefix = "tt-post:manual-canary:v1:%s:%s:" % (
                    canary_id,
                    str(identity.get("account_id") or ""),
                )
                try:
                    exact_match = bool(
                        run is not None
                        and pool is not None
                        and enabled_schedule is None
                        and str(run["trigger_type"]) == "manual"
                        and str(run["run_key"]).startswith(expected_prefix)
                        and str(row["publish_mode"]) == "direct_post"
                        and str(row["privacy_level"]) == "SELF_ONLY"
                        and not bool(row["allow_comment"])
                        and not bool(row["allow_duet"])
                        and not bool(row["allow_stitch"])
                        and not bool(row["brand_content_toggle"])
                        and not bool(row["brand_organic_toggle"])
                        and str(row["account_id"])
                        == str(identity.get("account_id") or "")
                        and int(pool["id"])
                        == int(identity.get("pool_id") or 0)
                        and str(pool["material_id"])
                        == str(identity.get("material_id") or "")
                        and str(pool["content_id"])
                        == str(identity.get("content_id") or "")
                        and str(pool["gpu_job_id"])
                        == str(identity.get("gpu_job_id") or "")
                        and str(pool["prepared_output_sha256"]).lower()
                        == str(
                            identity.get("output_sha256") or ""
                        ).lower()
                        and int(pool["prepared_output_size"])
                        == int(identity.get("output_size") or 0)
                        and str(pool["preparation_profile"])
                        == str(identity.get("profile") or "")
                    )
                except (TypeError, ValueError, OverflowError):
                    exact_match = False
                if not exact_match:
                    raise TTPostError(
                        "tt_post_manual_canary_identity_mismatch",
                        "一次性私密测试身份与冻结队列不一致",
                        409,
                    )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='publishing',updated_at=?
                WHERE id=? AND status='claimed'
                """,
                (now_iso, normalized),
            )
            self._event(
                conn,
                event_type="publish_started",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="claimed",
                to_status="publishing",
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def block_compliance(
        self,
        queue_id: Any,
        claim_token: Any,
        *,
        reason: str = "TikTok正式发布合规门禁未全部开放",
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Stop a due task before any Direct Post init request can occur."""

        normalized = _positive_int(queue_id, "发布队列ID")
        safe_reason = _required_text(redact_text(reason), "合规阻断原因", 500)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] == "blocked_compliance":
                return _public_queue(row)
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("claimed",),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='blocked_compliance',
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_blocked_compliance',error_message=?,
                    updated_at=?
                WHERE id=? AND status='claimed'
                """,
                (safe_reason, now_iso, normalized),
            )
            self._event(
                conn,
                event_type="queue_blocked_compliance",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="claimed",
                to_status="blocked_compliance",
                message=safe_reason,
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def cancel_queue(
        self,
        queue_id: Any,
        *,
        reason: str,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        safe_reason = _required_text(redact_text(reason), "取消原因", 500)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] == "canceled":
                return _public_queue(row)
            if row["status"] not in {
                "scheduled",
                "claimed",
                "blocked_compliance",
            }:
                raise TTPostError(
                    "tt_post_cancel_not_allowed",
                    "当前发布状态不允许取消",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='canceled',claim_worker='',claim_token='',
                    lease_expires_at_utc='',error_code='tt_post_canceled',
                    error_message=?,updated_at=?
                WHERE id=?
                """,
                (safe_reason, now_iso, normalized),
            )
            conn.execute(
                """
                UPDATE tt_post_material_pool
                SET status='canceled',updated_at=?
                WHERE id=? AND status='reserved'
                """,
                (now_iso, row["pool_item_id"]),
            )
            self._event(
                conn,
                event_type="queue_canceled",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status=str(row["status"]),
                to_status="canceled",
                message=safe_reason,
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def record_publish_id(
        self,
        queue_id: Any,
        claim_token: Any,
        publish_id: Any,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Persist the remote ID and permanently switch to reconcile-only mode."""

        normalized = _positive_int(queue_id, "发布队列ID")
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] in {"reconciling", "published"}:
                if secrets.compare_digest(str(row["publish_id"]), remote_id):
                    return _public_queue(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已冻结结果不一致",
                    409,
                )
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("publishing",),
                now_iso=now_iso,
            )
            try:
                conn.execute(
                    """
                    UPDATE tt_post_queue
                    SET status='reconciling',publish_id=?,
                        claim_worker='',claim_token='',lease_expires_at_utc='',
                        updated_at=?
                    WHERE id=? AND status='publishing'
                    """,
                    (remote_id, now_iso, normalized),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id已被其他队列记录",
                    409,
                ) from None
            self._event(
                conn,
                event_type="publish_id_recorded",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="publishing",
                to_status="reconciling",
                details={"publish_id": remote_id},
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def recover_publish_id_from_gpu_ledger(
        self,
        queue_id: Any,
        publish_id: Any,
        *,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Recover a lost remote ID without invoking Direct Post init again.

        This transition is intentionally limited to a manual reconcile path.
        An unknown/review row, or a publishing row whose GPU ledger already
        contains the remote ID, can move to reconcile-only state without
        invoking Direct Post init again.
        """

        normalized = _positive_int(queue_id, "发布队列ID")
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["publish_id"]:
                if (
                    row["status"] in {"reconciling", "published"}
                    and secrets.compare_digest(str(row["publish_id"]), remote_id)
                ):
                    return _public_queue(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已冻结结果不一致",
                    409,
                )
            if row["status"] not in {
                "unknown",
                "needs_review",
                "publishing",
            }:
                raise TTPostError(
                    "tt_post_manual_recovery_only",
                    "只有结果未知或发布中任务允许从GPU账本恢复publish_id",
                    409,
                )
            try:
                conn.execute(
                    """
                    UPDATE tt_post_queue
                    SET status='reconciling',publish_id=?,unknown_outcome=0,
                        claim_worker='',claim_token='',lease_expires_at_utc='',
                        error_code='',error_message='',updated_at=?
                    WHERE id=? AND publish_id=''
                    """,
                    (remote_id, now_iso, normalized),
                )
            except sqlite3.IntegrityError:
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id已被其他队列记录",
                    409,
                ) from None
            self._event(
                conn,
                event_type="publish_id_recovered_from_gpu_ledger",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status=str(row["status"]),
                to_status="reconciling",
                details={"publish_id": remote_id, "recovery_mode": "manual_reconcile"},
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def reconcile_published(
        self,
        queue_id: Any,
        publish_id: Any,
        *,
        publish_url: str = "",
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        safe_url = _https_url(publish_url, "TikTok Post地址", allow_empty=True)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] == "published":
                if secrets.compare_digest(str(row["publish_id"]), remote_id):
                    return _public_queue(row)
                raise TTPostError(
                    "tt_post_publish_id_conflict",
                    "TikTok publish_id与已发布结果不一致",
                    409,
                )
            if row["status"] != "reconciling" or not secrets.compare_digest(
                str(row["publish_id"]),
                remote_id,
            ):
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "队列没有可核对的TikTok publish_id",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='published',publish_url=?,unknown_outcome=0,
                    error_code='',error_message='',updated_at=?
                WHERE id=? AND status='reconciling'
                """,
                (safe_url, now_iso, normalized),
            )
            conn.execute(
                """
                UPDATE tt_post_material_pool
                SET status='published',updated_at=?
                WHERE id=? AND status='reserved'
                """,
                (now_iso, row["pool_item_id"]),
            )
            self._event(
                conn,
                event_type="publish_reconciled",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="reconciling",
                to_status="published",
                details={"publish_id": remote_id, "publish_url": safe_url},
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def reconcile_failed(
        self,
        queue_id: Any,
        publish_id: Any,
        *,
        remote_status: Any,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Terminalize an explicit remote failure without allowing another init."""

        normalized = _positive_int(queue_id, "发布队列ID")
        remote_id = str(publish_id or "").strip()
        if not _PUBLISH_ID_RE.fullmatch(remote_id):
            raise TTPostError("invalid_publish_id", "TikTok publish_id无效", 400)
        normalized_remote_status = str(remote_status or "").strip().lower()
        if normalized_remote_status not in {"failed", "publish_failed"}:
            raise TTPostError(
                "invalid_remote_publish_status",
                "TikTok远端发布失败状态无效",
                400,
            )
        message = "TikTok远端核对明确返回发布失败（%s）" % normalized_remote_status
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if (
                row["status"] == "failed"
                and row["error_code"] == "tt_post_remote_publish_failed"
                and secrets.compare_digest(str(row["publish_id"]), remote_id)
            ):
                return _public_queue(row)
            if row["status"] != "reconciling" or not secrets.compare_digest(
                str(row["publish_id"]),
                remote_id,
            ):
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "队列没有可核对的TikTok publish_id",
                    409,
                )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='failed',unknown_outcome=0,
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_remote_publish_failed',
                    error_message=?,updated_at=?
                WHERE id=? AND status='reconciling' AND publish_id=?
                """,
                (message, now_iso, normalized, remote_id),
            )
            conn.execute(
                """
                UPDATE tt_post_material_pool
                SET status='canceled',updated_at=?
                WHERE id=? AND status='reserved'
                """,
                (now_iso, row["pool_item_id"]),
            )
            self._event(
                conn,
                event_type="publish_reconciled_failed",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="reconciling",
                to_status="failed",
                message=message,
                details={
                    "publish_id": remote_id,
                    "remote_status": normalized_remote_status,
                    "error_code": "tt_post_remote_publish_failed",
                },
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def mark_unknown(
        self,
        queue_id: Any,
        claim_token: Any,
        *,
        reason: str,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        normalized = _positive_int(queue_id, "发布队列ID")
        safe_reason = _required_text(redact_text(reason), "未知结果原因", 500)
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] == "unknown":
                return _public_queue(row)
            if row["publish_id"]:
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "已取得publish_id的队列只能执行核对",
                    409,
                )
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("publishing",),
                now_iso=now_iso,
            )
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status='unknown',unknown_outcome=1,
                    claim_worker='',claim_token='',lease_expires_at_utc='',
                    error_code='tt_post_outcome_unknown',error_message=?,
                    updated_at=?
                WHERE id=? AND status='publishing'
                """,
                (safe_reason, now_iso, normalized),
            )
            self._event(
                conn,
                event_type="publish_outcome_unknown",
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status="publishing",
                to_status="unknown",
                message=safe_reason,
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def mark_failed(
        self,
        queue_id: Any,
        claim_token: Any,
        *,
        error_code: str,
        error_message: str,
        publish_was_not_created: bool,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Finish a known failure; uncertain publishing is forced to unknown."""

        normalized = _positive_int(queue_id, "发布队列ID")
        code = _required_text(error_code, "错误码", 96)
        message = _required_text(redact_text(error_message), "错误说明", 500)
        known_safe = _exact_bool(
            publish_was_not_created,
            "远端未创建发布结果确认",
        )
        now_iso = _iso_utc(now if now is not None else self._now_fn(), "当前时间")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise TTPostError("tt_post_queue_not_found", "TikTok发布队列不存在", 404)
            if row["status"] in TERMINAL_QUEUE_STATUSES:
                return _public_queue(row)
            if row["publish_id"] or row["status"] == "reconciling":
                raise TTPostError(
                    "tt_post_reconcile_only",
                    "已取得publish_id的队列只能执行核对",
                    409,
                )
            self._assert_claim(
                row,
                claim_token,
                allowed_statuses=("claimed", "publishing"),
                now_iso=now_iso,
            )
            target_status = "failed" if known_safe else "unknown"
            unknown = 0 if known_safe else 1
            stored_code = code if known_safe else "tt_post_outcome_unknown"
            conn.execute(
                """
                UPDATE tt_post_queue
                SET status=?,unknown_outcome=?,claim_worker='',claim_token='',
                    lease_expires_at_utc='',error_code=?,error_message=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    target_status,
                    unknown,
                    stored_code,
                    message,
                    now_iso,
                    normalized,
                ),
            )
            if known_safe:
                conn.execute(
                    """
                    UPDATE tt_post_material_pool
                    SET status='canceled',updated_at=?
                    WHERE id=? AND status='reserved'
                    """,
                    (now_iso, row["pool_item_id"]),
                )
            self._event(
                conn,
                event_type=(
                    "publish_failed"
                    if known_safe
                    else "publish_outcome_unknown"
                ),
                queue_id=normalized,
                pool_item_id=int(row["pool_item_id"]),
                created_at=now_iso,
                from_status=str(row["status"]),
                to_status=target_status,
                message=message,
                details={"error_code": stored_code},
            )
            updated = conn.execute(
                "SELECT * FROM tt_post_queue WHERE id=?",
                (normalized,),
            ).fetchone()
        return _public_queue(updated)

    def list_events(
        self,
        *,
        queue_id: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        params: Tuple[Any, ...] = ()
        where = ""
        if queue_id is not None:
            normalized = _positive_int(queue_id, "发布队列ID")
            where = " WHERE queue_id=?"
            params = (normalized,)
        with contextlib.closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tt_post_event%s ORDER BY id" % where,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError):
                item["details"] = {}
                item.pop("details_json", None)
            result.append(item)
        return result

"""Fail-closed core for the TikTok organic Post publishing pool.

Security boundaries:

* account list/read paths consume metadata-only loaders;
* an access token is fetched for one exact account only inside
  ``SnapshotAccountSource.publish_credentials``;
* credential and claim wrappers redact their secret values from ``repr``;
* no access token is persisted in the three SQLite tables;
* a remote ``publish_id`` moves a queue into reconcile-only state;
* unknown outcomes are terminal and are never selected by ``claim_due``;
* all three live gates default to closed.

The module is intentionally independent from ``app.py`` and from any concrete
TikTok API client so it can be tested without network or production data.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


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
_PUBLISH_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,256}$")
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_GPU_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{11,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")


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
    max_chars: int = MAX_CAPTION_CHARS,
) -> str:
    """Render the user's caption at queue-freeze time.

    ``{{contect_id}}`` is supported exactly as supplied by the product owner.
    ``{{content_id}}`` is accepted as a correctly-spelled compatibility alias.
    Unknown placeholders fail closed.
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
    rendered = _PLACEHOLDER_RE.sub(
        lambda match: normalized_content_id
        if match.group(1).strip() in {"contect_id", "content_id"}
        else match.group(0),
        text,
    ).strip()
    try:
        rendered_units = len(rendered.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        rendered_units = int(max_chars) + 1
    if not rendered or rendered_units > int(max_chars):
        raise TTPostError(
            "caption_length_invalid",
            "发布描述渲染后为空或超过长度限制",
            400,
        )
    return rendered


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


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_storage(db_path: Any) -> None:
    """Create the four-table TikTok Post ledger and account settings."""

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
        frozen_caption_template = str(caption_template or "").strip()
        caption = render_caption_template(
            frozen_caption_template,
            resolution.content_id,
        )
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            normalized_key = "tt-post:%s:%s:%s" % (
                pool_id,
                account.account_id,
                scheduled_at_utc,
            )
        normalized_key = _required_text(normalized_key, "幂等键", 255)

        with self._transaction() as conn:
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

        This transition is intentionally limited to a manual reconcile path:
        only an unknown/review row with no stored publish ID can move back to
        reconcile-only state.
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
            if row["status"] not in {"unknown", "needs_review"}:
                raise TTPostError(
                    "tt_post_manual_recovery_only",
                    "只有结果未知任务允许从GPU账本恢复publish_id",
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

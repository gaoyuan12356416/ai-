"""CPU-side TikTok Post service, account source, and GPU client.

The module keeps four boundaries explicit:

* account-list SQL projects metadata only and never names ``access_token``;
* one exact token row is read only inside ``publish_credentials``;
* the GPU receives an AES-GCM credential envelope, never a raw token;
* Direct Post init is unreachable unless all three production gates are open,
  except for one exact expiring SELF_ONLY canary target.

The HTTP surface is loopback-only and is intended to sit behind the authenticated
AI backend.  It never writes to the source MySQL databases.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from features.tt_gpu.credentials import seal_access_token
from features.x_posts.selector import (
    CandidateQueryError,
    CandidateSelectionError,
    DEFAULT_SCHEMA as DEFAULT_MATERIAL_SCHEMA,
    DramawaveCandidateSelector,
    PoolCandidateRejection,
    connect_read_only,
    material_key,
    shanghai_now,
)

from .core import (
    AccountSourceError,
    BEIJING_TZ,
    FIXED_CAPTION_TEMPLATE,
    LiveGates,
    MAX_ACCOUNT_SETTINGS_BATCH,
    MAX_DAILY_PUBLISH_COUNT,
    MaterialResolution,
    SafeAccount,
    SnapshotAccountSource,
    TTPostAccountSettings,
    TTPostError,
    TTPostPolicy,
    TTPostStore,
    beijing_to_utc,
    caption_uses_desc_macro,
    caption_uses_code_macro,
    caption_uses_url_macro,
    normalize_drama_language,
    redact_text,
    render_fixed_caption,
    render_caption_template,
)
from .links import (
    TTPostLinkError,
    build_short_url,
    build_w2a_url,
    direct_test_short_link_id,
    validate_short_url,
    validate_w2a_url,
    write_short_redirect,
)
from .code_routes import (
    RedisRESPClient,
    TTCodeRouteError,
    TTCodeRouteResolver,
)


UTC = timezone.utc
DEFAULT_ACCOUNT_MYSQL_HOST = "101.32.56.53"
DEFAULT_ACCOUNT_MYSQL_PORT = 63350
DEFAULT_ACCOUNT_MYSQL_DATABASE = "ads_ai"
DEFAULT_ACCOUNT_TABLE = "tiktok_personal_account_snapshot"
DEFAULT_CPU_HOST = "127.0.0.1"
DEFAULT_CPU_PORT = 18829
DEFAULT_GPU_URL = "http://127.0.0.1:18830"
DEFAULT_DB_PATH = "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3"
DEFAULT_SHORT_LINK_ROOT = "/mnt/data-disk/tt-post-publisher/s2l"
DEFAULT_CODE_REDIS_HOST = "127.0.0.1"
DEFAULT_CODE_REDIS_PORT = 6381
DEFAULT_CODE_REDIS_TIMEOUT = 0.2
DEFAULT_GRACE_SECONDS = 600
DEFAULT_LEASE_SECONDS = 300
CLAIM_LEASE_BUFFER_SECONDS = 60
DEFAULT_RECURRING_EXECUTION_LEASE_SECONDS = 120
DEFAULT_PREPARATION_LEASE_SECONDS = 120
MAX_PREPARATION_LEASE_SECONDS = 600
DEFAULT_PREPARATION_MAX_ATTEMPTS = 5
MAX_ACCOUNT_ROWS = 1000
MAX_HTTP_BODY_BYTES = 256 * 1024
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
TOKEN_MIN_VALIDITY_SECONDS = 300
CAPTION_DRAMA_LINE_RE = re.compile(r"(?m)^[ \t]*Drama ID:[ \t]*(\S+)[ \t]*$")
SAFE_INTERNAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
ACCOUNT_SETTINGS_VALUE_FIELDS = frozenset(
    {
        "privacy_level",
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "commercial_disclosure",
        "brand_organic_toggle",
        "brand_content_toggle",
        "is_aigc",
        "drama_language",
    }
)
PRIVACY_LEVEL_ORDER = (
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
)
CREATOR_INFO_BATCH_WORKERS = 4
TT_MAX_MATERIAL_DURATION_SECONDS = 3600
DEFAULT_RUNNER_KICK_PATH = "/run/tt-post/manual-kick"
DEFAULT_PREPARATION_KICK_PATH = "/run/tt-post/prepare-kick"
MANUAL_CANARY_ACKNOWLEDGEMENT = (
    "I_ACCEPT_ONE_SHOT_PRIVATE_TIKTOK_CANARY_20260731"
)

TERMINAL_PREPARATION_ERROR_CODES = frozenset(
    {
        "invalid_gpu_job_id",
        "invalid_prepared_media_metadata",
        "invalid_prepared_media_metrics",
        "invalid_prepared_output_sha",
        "tt_account_not_found",
        "tt_content_id_mismatch",
        "tt_interaction_not_allowed",
        "tt_prepared_media_duration_invalid",
        "tt_prepared_media_fingerprint_invalid",
        "tt_prepared_media_identity_mismatch",
        "tt_prepared_media_metadata_invalid",
        "tt_prepared_media_profile_mismatch",
        "tt_privacy_not_allowed",
    }
)


# This statement is deliberately metadata-only.  Do not add token predicates,
# token aliases, SELECT *, or token-derived expressions to it.
ACCOUNT_LIST_SQL = """
SELECT
  source_account_id,
  main_account_id,
  external_account_id,
  account_name,
  account_link,
  fan_count,
  token_status,
  account_status,
  token_expires_time,
  last_token_checked_time,
  disable_publish,
  has_metric_snapshot,
  is_active,
  last_seen_at,
  updated_at
FROM `ads_ai`.`tiktok_personal_account_snapshot`
WHERE is_active = 1
  AND account_status = 2
  AND token_status = 2
  AND disable_publish = 0
  AND token_expires_time > %s
ORDER BY source_account_id
LIMIT 1001
"""


# Exact metadata recheck before an execution-context credential read.
ACCOUNT_METADATA_SQL = """
SELECT
  source_account_id,
  main_account_id,
  external_account_id,
  account_name,
  account_link,
  fan_count,
  token_status,
  account_status,
  token_expires_time,
  last_token_checked_time,
  disable_publish,
  has_metric_snapshot,
  is_active,
  last_seen_at,
  updated_at
FROM `ads_ai`.`tiktok_personal_account_snapshot`
WHERE source_account_id = %s
  AND is_active = 1
  AND account_status = 2
  AND token_status = 2
  AND disable_publish = 0
  AND token_expires_time > %s
LIMIT 2
"""


# This is the only statement allowed to select the credential.  It is always
# parameterized by one exact primary-key identity inside publish_credentials.
ACCOUNT_TOKEN_SQL = """
SELECT source_account_id, access_token
FROM `ads_ai`.`tiktok_personal_account_snapshot`
WHERE source_account_id = %s
  AND is_active = 1
  AND account_status = 2
  AND token_status = 2
  AND disable_publish = 0
  AND token_expires_time > %s
  AND access_token IS NOT NULL
  AND OCTET_LENGTH(access_token) > 0
LIMIT 2
"""


class TTPostServiceError(TTPostError):
    """Stable loopback-service failure."""


class GPUClientError(TTPostServiceError):
    """Secret-safe GPU error with a remote-creation certainty marker."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 502,
        *,
        unknown_outcome: bool = False,
        publish_was_not_created: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ):
        self.unknown_outcome = bool(unknown_outcome)
        self.publish_was_not_created = bool(publish_was_not_created)
        raw_details = details if isinstance(details, Mapping) else {}
        self.details = {}
        for key in (
            "publish_id",
            "state",
            "log_id",
            "upstream_code",
            "upstream_message",
            "received_at",
        ):
            if raw_details.get(key) not in (None, ""):
                self.details[key] = str(
                    raw_details.get(key) or ""
                )[:512]
        http_status = raw_details.get("upstream_http_status")
        if isinstance(http_status, int) and 100 <= http_status <= 599:
            self.details["upstream_http_status"] = http_status
        if type(raw_details.get("message_redacted")) is bool:
            self.details["message_redacted"] = raw_details[
                "message_redacted"
            ]
        super().__init__(code, message, status)

    def __repr__(self) -> str:
        return (
            "GPUClientError(code=%r, status=%r, unknown_outcome=%r)"
            % (self.code, self.status, self.unknown_outcome)
        )


def _exact_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise TTPostServiceError("invalid_request", "%s必须显式填写" % label, 400)
    return value


def _positive_decimal(value: Any, label: str, maximum_digits: int = 30) -> str:
    text = str(value or "").strip()
    if (
        not re.fullmatch(r"[1-9][0-9]*", text)
        or len(text) > int(maximum_digits)
    ):
        raise TTPostServiceError("invalid_request", "%s无效" % label, 400)
    return text


def _positive_int(value: Any, label: str, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        raise TTPostServiceError("invalid_request", "%s无效" % label, 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TTPostServiceError("invalid_request", "%s无效" % label, 400) from None
    if result <= 0 or result > int(maximum):
        raise TTPostServiceError("invalid_request", "%s无效" % label, 400)
    return result


def _account_settings_version(value: Any, maximum: int = 2**31 - 1) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        result = int(value)
    else:
        raise TTPostServiceError(
            "invalid_account_settings_version",
            "个号发布设置版本无效",
            400,
        )
    if result < 0 or result > int(maximum):
        raise TTPostServiceError(
            "invalid_account_settings_version",
            "个号发布设置版本无效",
            400,
        )
    return result


def _batch_account_ids(value: Any) -> List[str]:
    if not isinstance(value, list):
        raise TTPostServiceError(
            "invalid_batch_targets",
            "批量个号目标必须是列表",
            400,
        )
    if not value or len(value) > MAX_ACCOUNT_SETTINGS_BATCH:
        raise TTPostServiceError(
            "invalid_batch_targets",
            "批量个号数量必须在1到%d之间"
            % MAX_ACCOUNT_SETTINGS_BATCH,
            400,
        )
    result = []
    seen = set()
    for raw_account_id in value:
        account_id = _positive_decimal(raw_account_id, "TikTok账号ID")
        if account_id in seen:
            raise TTPostServiceError(
                "invalid_batch_targets",
                "批量个号目标不能重复",
                400,
            )
        seen.add(account_id)
        result.append(account_id)
    return result


def _auto_config_account_ids(value: Any) -> List[str]:
    """Normalize the complete auto-publish membership, including an empty set."""

    if not isinstance(value, list) or len(value) > MAX_ACCOUNT_SETTINGS_BATCH:
        raise TTPostServiceError(
            "invalid_auto_publish_accounts",
            "自动发布账号必须是最多50个账号的列表",
            400,
        )
    result = []
    seen = set()
    for raw_account_id in value:
        account_id = _positive_decimal(raw_account_id, "TikTok账号ID")
        if account_id in seen:
            raise TTPostServiceError(
                "invalid_auto_publish_accounts",
                "自动发布账号不能重复",
                400,
            )
        seen.add(account_id)
        result.append(account_id)
    return result


def _assigned_auto_account_id(
    material_id: str,
    account_ids: Sequence[Any],
) -> str:
    """Choose one saved auto account deterministically for a material."""

    normalized = [
        _positive_decimal(value, "TikTok账号ID")
        for value in account_ids
    ]
    if not normalized:
        raise TTPostServiceError(
            "tt_post_auto_accounts_required",
            "自动发布配置至少需要一个账号才能加入素材",
            409,
        )
    seed = "%s|%s" % (material_id, ",".join(normalized))
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return normalized[int.from_bytes(digest[:8], "big") % len(normalized)]


def _batch_targets(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise TTPostServiceError(
            "invalid_batch_targets",
            "批量个号目标必须是列表",
            400,
        )
    if not value or len(value) > MAX_ACCOUNT_SETTINGS_BATCH:
        raise TTPostServiceError(
            "invalid_batch_targets",
            "批量个号数量必须在1到%d之间"
            % MAX_ACCOUNT_SETTINGS_BATCH,
            400,
        )
    result = []
    seen = set()
    for raw_target in value:
        if not isinstance(raw_target, Mapping) or set(raw_target) != {
            "source_account_id",
            "expected_version",
        }:
            raise TTPostServiceError(
                "invalid_batch_targets",
                "批量个号目标字段无效",
                400,
            )
        account_id = _positive_decimal(
            raw_target.get("source_account_id"),
            "TikTok账号ID",
        )
        if account_id in seen:
            raise TTPostServiceError(
                "invalid_batch_targets",
                "批量个号目标不能重复",
                400,
            )
        seen.add(account_id)
        result.append(
            {
                "source_account_id": account_id,
                "expected_version": _account_settings_version(
                    raw_target.get("expected_version")
                ),
            }
        )
    return result


def _bounded_text(
    value: Any,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    text = str(value or "").strip()
    if (
        (not allow_empty and not text)
        or len(text) > int(maximum)
        or "\x00" in text
        or any(ord(char) < 32 and char not in "\r\n\t" for char in text)
    ):
        raise TTPostServiceError("invalid_request", "%s无效" % label, 400)
    return text


def _safe_https_url(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 4096)
    parsed = urllib.parse.urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise TTPostServiceError("invalid_request", "%s必须是HTTPS地址" % label, 400)
    return text


def _utc_iso(value: Any) -> str:
    return beijing_to_utc(value)


def _now_utc(now_fn: Callable[[], datetime]) -> datetime:
    current = now_fn()
    if not isinstance(current, datetime):
        raise TTPostServiceError("clock_invalid", "系统时间不可用", 500)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _minimum_token_expiry_ns(
    now_fn: Callable[[], datetime],
    *,
    minimum_seconds: int = TOKEN_MIN_VALIDITY_SECONDS,
) -> int:
    current = _now_utc(now_fn)
    return int((current + timedelta(seconds=int(minimum_seconds))).timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class SnapshotMySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str = DEFAULT_ACCOUNT_MYSQL_DATABASE

    def validate(self) -> None:
        if (
            self.host != DEFAULT_ACCOUNT_MYSQL_HOST
            or int(self.port) != DEFAULT_ACCOUNT_MYSQL_PORT
            or self.database != DEFAULT_ACCOUNT_MYSQL_DATABASE
            or not str(self.user or "").strip()
            or self.password == ""
        ):
            raise AccountSourceError(
                "tt_account_source_config_invalid",
                "TikTok账号只读数据源配置无效",
                500,
            )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "SnapshotMySQLConfig":
        source = os.environ if environ is None else environ
        raw_port = str(source.get("TT_POST_MYSQL_PORT", DEFAULT_ACCOUNT_MYSQL_PORT))
        try:
            port = int(raw_port)
        except ValueError:
            port = 0
        return cls(
            host=str(
                source.get("TT_POST_MYSQL_HOST", DEFAULT_ACCOUNT_MYSQL_HOST)
            ).strip(),
            port=port,
            user=str(source.get("TT_POST_MYSQL_USER", "")).strip(),
            password=str(source.get("TT_POST_MYSQL_PASSWORD", "")),
            database=str(
                source.get(
                    "TT_POST_ACCOUNT_MYSQL_DATABASE",
                    DEFAULT_ACCOUNT_MYSQL_DATABASE,
                )
            ).strip(),
        )


class MySQLSnapshotAccountRepository:
    """Read-only account repository with physically separated token SQL."""

    def __init__(
        self,
        config: SnapshotMySQLConfig,
        *,
        connection_factory: Optional[Callable[[SnapshotMySQLConfig], Any]] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        verify_identity: bool = True,
    ):
        config.validate()
        self.config = config
        self._connection_factory = connection_factory
        self._now_fn = now_fn
        self._verify_identity = bool(verify_identity)

    def __repr__(self) -> str:
        return (
            "MySQLSnapshotAccountRepository(host=%r, port=%r, database=%r, "
            "password=<redacted>)"
            % (self.config.host, self.config.port, self.config.database)
        )

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(self.config)
        try:
            import pymysql
        except ImportError:
            raise AccountSourceError(
                "tt_account_source_driver_missing",
                "TikTok账号只读驱动不可用",
                500,
            ) from None
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=5,
                read_timeout=15,
                write_timeout=5,
            )
        except Exception:
            raise AccountSourceError(
                "tt_account_source_unavailable",
                "TikTok账号只读数据源暂不可用",
                503,
            ) from None

    @staticmethod
    def _close(value: Any) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            close()

    def _rows(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        statement = str(sql or "").lstrip()
        if not re.match(r"(?is)^SELECT\b", statement):
            raise AccountSourceError(
                "tt_account_query_denied",
                "TikTok账号数据源仅允许只读查询",
                500,
            )
        connection = self._connect()
        cursor = None
        try:
            cursor = connection.cursor()
            if self._verify_identity:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT DATABASE() AS db_name, @@read_only AS read_only"
                )
                identity = cursor.fetchone() or {}
                if (
                    str(identity.get("db_name") or "") != self.config.database
                    or int(identity.get("read_only") or 0) != 1
                ):
                    raise AccountSourceError(
                        "tt_account_source_identity_invalid",
                        "TikTok账号只读数据源身份校验失败",
                        503,
                    )
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except TTPostError:
            raise
        except Exception:
            raise AccountSourceError(
                "tt_account_source_unavailable",
                "TikTok账号只读查询暂不可用",
                503,
            ) from None
        finally:
            self._close(cursor)
            self._close(connection)

    @staticmethod
    def _public_row(raw: Mapping[str, Any]) -> Dict[str, Any]:
        account_id = _positive_decimal(
            raw.get("source_account_id"),
            "TikTok账号ID",
        )
        if (
            int(raw.get("is_active") or 0) != 1
            or int(raw.get("account_status") or 0) != 2
            or int(raw.get("token_status") or 0) != 2
            or int(raw.get("disable_publish") or 0) != 0
        ):
            raise AccountSourceError(
                "tt_account_metadata_invalid",
                "TikTok账号候选状态不一致",
                503,
            )
        external_id = _bounded_text(
            raw.get("external_account_id"),
            "TikTok外部账号ID",
            512,
            allow_empty=True,
        )
        main_id = _bounded_text(
            raw.get("main_account_id"),
            "TikTok主账号ID",
            255,
            allow_empty=True,
        )
        account_name = _bounded_text(
            raw.get("account_name") or external_id or main_id,
            "TikTok账号名称",
            255,
            allow_empty=True,
        )
        account_link = _bounded_text(
            raw.get("account_link"),
            "TikTok账号主页",
            512,
            allow_empty=True,
        )
        if account_link:
            parsed = urllib.parse.urlsplit(account_link)
            if parsed.scheme != "https" or not parsed.hostname:
                account_link = ""
        try:
            fan_count = max(0, int(raw.get("fan_count") or 0))
            expires_ns = int(raw.get("token_expires_time") or 0)
            checked_ns = int(raw.get("last_token_checked_time") or 0)
        except (TypeError, ValueError, OverflowError):
            raise AccountSourceError(
                "tt_account_metadata_invalid",
                "TikTok账号候选数据无效",
                503,
            ) from None
        return {
            "source_account_id": account_id,
            "account_id": account_id,
            "main_account_id": main_id,
            "external_account_id": external_id,
            "username": external_id or main_id,
            "display_name": account_name,
            "account_name": account_name,
            "account_link": account_link,
            "avatar_url": "",
            "fan_count": fan_count,
            "token_status": 2,
            "account_status": 2,
            "token_expires_time": expires_ns,
            "last_token_checked_time": checked_ns,
            "last_seen_at": str(raw.get("last_seen_at") or "")[:64],
            "disable_publish": 0,
            "is_active": 1,
            "status": "active",
            "publish_eligible": True,
            "eligibility_reason": "数据库候选条件已通过，仍需TikTok实时确认",
        }

    @staticmethod
    def _safe_account_mapping(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "account_id": row["account_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "avatar_url": row["avatar_url"],
            "status": row["status"],
            "publish_eligible": row["publish_eligible"],
        }

    def list_public_accounts(self) -> List[Dict[str, Any]]:
        minimum = _minimum_token_expiry_ns(self._now_fn)
        rows = self._rows(ACCOUNT_LIST_SQL, (minimum,))
        if len(rows) > MAX_ACCOUNT_ROWS:
            raise AccountSourceError(
                "tt_account_source_too_large",
                "TikTok账号候选数量超过安全上限",
                503,
            )
        items = [self._public_row(row) for row in rows]
        identities = [item["source_account_id"] for item in items]
        if len(set(identities)) != len(identities):
            raise AccountSourceError(
                "tt_account_metadata_ambiguous",
                "TikTok账号候选存在重复身份",
                503,
            )
        return items

    def get_public_account(self, source_account_id: Any) -> Dict[str, Any]:
        normalized = _positive_decimal(source_account_id, "TikTok账号ID")
        minimum = _minimum_token_expiry_ns(self._now_fn)
        rows = self._rows(ACCOUNT_METADATA_SQL, (normalized, minimum))
        if len(rows) != 1:
            raise AccountSourceError(
                "tt_account_not_found",
                "TikTok账号不存在或不满足候选条件",
                404,
            )
        item = self._public_row(rows[0])
        if not secrets.compare_digest(item["source_account_id"], normalized):
            raise AccountSourceError(
                "tt_account_metadata_mismatch",
                "TikTok账号资料与请求账号不一致",
                409,
            )
        return item

    def _load_token(self, source_account_id: str) -> Optional[Mapping[str, Any]]:
        normalized = _positive_decimal(source_account_id, "TikTok账号ID")
        minimum = _minimum_token_expiry_ns(self._now_fn)
        rows = self._rows(ACCOUNT_TOKEN_SQL, (normalized, minimum))
        if len(rows) != 1:
            return None
        row = rows[0]
        return {
            "account_id": str(row.get("source_account_id") or ""),
            "access_token": row.get("access_token"),
        }

    def as_account_source(self) -> SnapshotAccountSource:
        def list_loader() -> Iterable[Mapping[str, Any]]:
            return [
                self._safe_account_mapping(item)
                for item in self.list_public_accounts()
            ]

        def account_loader(account_id: str) -> Mapping[str, Any]:
            return self._safe_account_mapping(self.get_public_account(account_id))

        return SnapshotAccountSource(
            list_loader=list_loader,
            account_loader=account_loader,
            token_loader=self._load_token,
        )


class _TTDramawaveCandidateSelector(DramawaveCandidateSelector):
    """Keep shared safety checks while applying TikTok's duration ceiling."""

    def _pool_drama_rows(
        self,
        content_id: str,
        language: str,
    ) -> List[Dict[str, Any]]:
        """Collapse TT description whitespace before shared validation only."""

        rows = super()._pool_drama_rows(content_id, language)
        normalized_rows: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_description = item.get("drama_description")
            if isinstance(raw_description, bytes):
                try:
                    raw_description = raw_description.decode(
                        "utf-8",
                        errors="strict",
                    )
                except UnicodeDecodeError:
                    pass
            if isinstance(raw_description, str):
                item["drama_description"] = re.sub(
                    r"\s+",
                    " ",
                    raw_description,
                ).strip()
            normalized_rows.append(item)
        return normalized_rows

    def _pool_material_rows(self, material_id: str) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                CAST(cs.id AS CHAR) AS material_id,
                cs.product AS product,
                cs.type AS material_type,
                cs.is_delete AS is_delete,
                cs.url AS material_url,
                cs.name AS material_name,
                cs.language AS material_language,
                cs.data_source_id AS content_id,
                cs.tag_name AS source_tag_name,
                cs.video_duration AS video_duration
             FROM `{schema}`.ads_custom_source cs
             WHERE cs.id = %s
             LIMIT 2
        """.format(schema=self.schema)
        cursor = None
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql, (material_id,))
            rows = [dict(row) for row in cursor.fetchall()]
        except Exception:
            raise CandidateQueryError(
                "read-only TikTok material query failed"
            ) from None
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

        if not rows:
            raise PoolCandidateRejection(
                "material_not_found",
                "素材ID不存在",
            )
        if len(rows) != 1:
            raise PoolCandidateRejection(
                "material_identity_ambiguous",
                "素材身份数据不唯一",
            )

        row = rows[0]
        try:
            material_type = int(row.get("material_type"))
        except (TypeError, ValueError, OverflowError):
            material_type = -1
        if material_type != 2:
            raise PoolCandidateRejection(
                "material_type_not_video",
                "该素材不是视频",
            )

        try:
            is_delete = int(row.get("is_delete"))
        except (TypeError, ValueError, OverflowError):
            is_delete = 1
        if is_delete != 0:
            raise PoolCandidateRejection(
                "material_deleted",
                "该素材已删除",
            )

        try:
            duration = float(row.get("video_duration"))
        except (TypeError, ValueError, OverflowError):
            duration = float("nan")
        if (
            not math.isfinite(duration)
            or duration <= 0
            or duration > TT_MAX_MATERIAL_DURATION_SECONDS
        ):
            raise PoolCandidateRejection(
                "material_duration_out_of_range",
                "TT素材时长必须大于0秒且不超过%d秒"
                % TT_MAX_MATERIAL_DURATION_SECONDS,
            )
        return rows


def _select_tt_pool_candidates(
    connection: Any,
    pool_items: Iterable[Dict[str, Any]],
    source_date: str,
    *,
    limit: int,
    schema: str,
    now: datetime,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return _TTDramawaveCandidateSelector(
        connection,
        schema=schema,
        now=now,
    ).select_pool(
        pool_items,
        source_date,
        limit=limit,
    )


class DramawaveMaterialResolver:
    """Resolve one manual material through TikTok's strict Dramawave validator."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        schema: str = DEFAULT_MATERIAL_SCHEMA,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not callable(connection_factory):
            raise ValueError("material connection factory must be callable")
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(schema or "")):
            raise ValueError("material schema is invalid")
        self._connection_factory = connection_factory
        self.schema = str(schema)
        self._now_fn = now_fn

    def resolve(self, material_id: Any) -> Dict[str, Any]:
        try:
            normalized = material_key(material_id)
        except CandidateSelectionError:
            raise TTPostServiceError(
                "invalid_material_id",
                "素材ID必须是正整数",
                400,
            ) from None
        current = shanghai_now(self._now_fn())
        source_date = (current.date() - timedelta(days=1)).isoformat()
        connection = None
        try:
            connection = self._connection_factory()
            selected, rejections = _select_tt_pool_candidates(
                connection,
                [
                    {
                        "id": 1,
                        "material_id": normalized,
                        "created_at": current.astimezone(UTC).isoformat(),
                    }
                ],
                source_date,
                limit=1,
                schema=self.schema,
                now=current,
            )
        except CandidateQueryError:
            raise TTPostServiceError(
                "tt_material_query_unavailable",
                "Dramawave素材只读查询暂不可用",
                503,
            ) from None
        except CandidateSelectionError:
            raise TTPostServiceError(
                "tt_material_validation_failed",
                "Dramawave素材严格校验失败",
                409,
            ) from None
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        if rejections or len(selected) != 1:
            rejection = rejections[0] if rejections else {}
            error_code = str(
                rejection.get("error_code") or "tt_material_not_eligible"
            )[:96]
            raise TTPostServiceError(
                error_code,
                str(rejection.get("error_message") or "素材不满足Dramawave发布条件")[:500],
                404 if error_code == "material_not_found" else 409,
            )
        candidate = dict(selected[0])
        if str(candidate.get("material_id") or "") != normalized:
            raise TTPostServiceError(
                "tt_material_identity_mismatch",
                "Dramawave素材身份校验失败",
                409,
            )
        source_url = _safe_https_url(
            candidate.get("material_url"),
            "Dramawave素材地址",
        )
        resolution = MaterialResolution.from_mapping(
            normalized,
            {
                "material_id": normalized,
                "content_id": candidate.get("content_id"),
                "media_url": source_url,
            },
        )
        return {
            "material_id": resolution.material_id,
            "content_id": resolution.content_id,
            "media_url": resolution.media_url,
            "source_media_url": source_url,
            "material_name": str(candidate.get("material_name") or "")[:500],
            "drama_name": str(candidate.get("drama_name") or "")[:500],
            "material_language": str(candidate.get("material_language") or "")[:32],
            "material_tag": str(candidate.get("tag") or "")[:255],
            "description": str(candidate.get("description") or ""),
        }


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(
                fragment in lowered
                for fragment in (
                    "access_token",
                    "refresh_token",
                    "authorization",
                    "client_secret",
                )
            ):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


class GPUClient:
    """Strict CPU client for the loopback GPU TT sidecar."""

    __slots__ = (
        "base_url",
        "_internal_token",
        "_seal_key",
        "timeout",
        "prepare_timeout",
        "_connection_factory",
    )

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        seal_key: Any,
        *,
        timeout: int = 120,
        prepare_timeout: Optional[int] = None,
        connection_factory: Optional[Callable[[str, int, int], Any]] = None,
    ):
        parsed = urllib.parse.urlsplit(str(base_url or "").rstrip("/"))
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port != 18830
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise TTPostServiceError(
                "tt_gpu_url_invalid",
                "TT GPU服务必须使用127.0.0.1:18830",
                500,
            )
        token = str(internal_token or "")
        if len(token) < 32 or len(token) > 512 or any(ord(char) < 33 for char in token):
            raise TTPostServiceError(
                "tt_gpu_bearer_invalid",
                "TT GPU专用内部凭据未配置",
                500,
            )
        try:
            timeout = int(timeout)
        except (TypeError, ValueError, OverflowError):
            timeout = 0
        if timeout < 1 or timeout > 3600:
            raise TTPostServiceError(
                "tt_gpu_timeout_invalid",
                "TT GPU超时配置无效",
                500,
            )
        try:
            prepare_timeout = int(
                timeout if prepare_timeout is None else prepare_timeout
            )
        except (TypeError, ValueError, OverflowError):
            prepare_timeout = 0
        if prepare_timeout < timeout or prepare_timeout > 10800:
            raise TTPostServiceError(
                "tt_gpu_prepare_timeout_invalid",
                "TT GPU prepare timeout is invalid",
                500,
            )
        self.base_url = "http://127.0.0.1:18830"
        self._internal_token = token
        self._seal_key = seal_key
        self.timeout = timeout
        self.prepare_timeout = prepare_timeout
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return (
            "GPUClient(base_url=%r, internal_token=<redacted>, "
            "seal_key=<redacted>, timeout=%r, prepare_timeout=%r)"
            % (self.base_url, self.timeout, self.prepare_timeout)
        )

    def _connection(self, timeout: int) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory("127.0.0.1", 18830, timeout)
        return http.client.HTTPConnection("127.0.0.1", 18830, timeout=timeout)

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        write_may_have_happened: bool = False,
    ) -> Dict[str, Any]:
        if (
            not re.fullmatch(
                r"/internal/tt-post/(?:creator-info|prepare|publish|canary-publish|reconcile)",
                path,
            )
            or not isinstance(payload, Mapping)
            or _contains_sensitive_key(payload)
        ):
            raise GPUClientError(
                "tt_gpu_request_invalid",
                "TT GPU请求无效",
                500,
                publish_was_not_created=True,
            )
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_HTTP_BODY_BYTES:
            raise GPUClientError(
                "tt_gpu_request_too_large",
                "TT GPU请求超过安全上限",
                500,
                publish_was_not_created=True,
            )
        request_timeout = (
            self.prepare_timeout
            if path == "/internal/tt-post/prepare"
            else self.timeout
        )
        connection = self._connection(request_timeout)
        response = None
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": "Bearer " + self._internal_token,
                    "Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                raise GPUClientError(
                    "tt_gpu_response_too_large",
                    "TT GPU响应超过安全上限",
                    502,
                    unknown_outcome=write_may_have_happened,
                )
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                raise GPUClientError(
                    "tt_gpu_response_invalid",
                    "TT GPU响应格式无效",
                    502,
                    unknown_outcome=write_may_have_happened,
                ) from None
            if not isinstance(decoded, dict) or _contains_sensitive_key(decoded):
                raise GPUClientError(
                    "tt_gpu_response_invalid",
                    "TT GPU响应包含无效字段",
                    502,
                    unknown_outcome=write_may_have_happened,
                )
            status = int(getattr(response, "status", 0) or 0)
            if not 200 <= status < 300:
                explicit_unknown = decoded.get("unknown_outcome")
                explicit_not_created = decoded.get("publish_was_not_created")
                code = str(
                    decoded.get("code")
                    or decoded.get("error")
                    or "tt_gpu_error"
                )[:96]
                details = (
                    decoded.get("details")
                    if isinstance(decoded.get("details"), Mapping)
                    else {}
                )
                known_not_created_codes = {
                    "invalid_request",
                    "prepared_artifact_not_found",
                    "publish_idempotency_conflict",
                    "tt_media_profile_not_direct_post_eligible",
                    "tt_publish_compliance_gate_closed",
                    "tt_publish_url_property_mismatch",
                    "tt_manual_canary_closed",
                    "tt_manual_canary_target_mismatch",
                    "tt_manual_canary_artifact_mismatch",
                    "tt_upstream_rejected",
                    "credential_envelope_invalid",
                    "credential_envelope_expired",
                    "credential_binding_mismatch",
                }
                unknown = (
                    bool(explicit_unknown)
                    if type(explicit_unknown) is bool
                    else bool(
                        write_may_have_happened
                        and code not in known_not_created_codes
                        and code != "tt_publish_reconcile_required"
                    )
                )
                not_created = (
                    bool(explicit_not_created)
                    if type(explicit_not_created) is bool
                    else code in known_not_created_codes
                )
                if not_created:
                    unknown = False
                raise GPUClientError(
                    code,
                    str(
                        decoded.get("message")
                        or decoded.get("error_message")
                        or "TT GPU请求失败"
                    )[:500],
                    status,
                    unknown_outcome=unknown,
                    publish_was_not_created=not_created,
                    details=details,
                )
            item = decoded.get("item", decoded)
            if not isinstance(item, dict):
                raise GPUClientError(
                    "tt_gpu_response_invalid",
                    "TT GPU响应缺少结果对象",
                    502,
                    unknown_outcome=write_may_have_happened,
                )
            return dict(item)
        except GPUClientError:
            raise
        except (OSError, socket.error, http.client.HTTPException):
            raise GPUClientError(
                "tt_gpu_unreachable",
                "TT GPU服务暂不可用",
                502,
                unknown_outcome=write_may_have_happened,
                publish_was_not_created=not write_may_have_happened,
            ) from None
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def _envelope(
        self,
        access_token: str,
        *,
        job_id: str,
        source_account_id: str,
        operation: str,
    ) -> str:
        try:
            return seal_access_token(
                self._seal_key,
                access_token,
                job_id=job_id,
                source_account_id=source_account_id,
                operation=operation,
                ttl_seconds=120,
            )
        except Exception:
            raise GPUClientError(
                "tt_gpu_credential_envelope_failed",
                "TT GPU短时凭据封装失败",
                500,
                publish_was_not_created=True,
            ) from None

    def creator_info(
        self,
        *,
        job_id: str,
        source_account_id: str,
        access_token: str,
    ) -> Dict[str, Any]:
        envelope = self._envelope(
            access_token,
            job_id=job_id,
            source_account_id=source_account_id,
            operation="creator_info",
        )
        return self._post(
            "/internal/tt-post/creator-info",
            {
                "job_id": job_id,
                "source_account_id": source_account_id,
                "credential_envelope": envelope,
            },
        )

    def prepare(
        self,
        *,
        job_id: str,
        material: Mapping[str, Any],
        source_trim_tail_seconds: float,
        expected_profile: str,
    ) -> Dict[str, Any]:
        return self._post(
            "/internal/tt-post/prepare",
            {
                "job_id": job_id,
                "content_id": material.get("content_id"),
                "expected_profile": expected_profile,
                "source_url": material.get("source_media_url")
                or material.get("media_url"),
                "source_trim_tail_seconds": source_trim_tail_seconds,
            },
        )

    def publish(
        self,
        *,
        job_id: str,
        source_account_id: str,
        access_token: str,
        queue: Mapping[str, Any],
        manual_canary: bool = False,
        manual_canary_id: str = "",
    ) -> Dict[str, Any]:
        envelope = self._envelope(
            access_token,
            job_id=job_id,
            source_account_id=source_account_id,
            operation=(
                "canary_publish"
                if manual_canary
                else "publish"
            ),
        )
        payload = {
            "job_id": job_id,
            "source_account_id": source_account_id,
            "credential_envelope": envelope,
            "title": queue.get("caption"),
            "privacy_level": queue.get("privacy_level"),
            "disable_comment": not bool(queue.get("allow_comment")),
            "disable_duet": not bool(queue.get("allow_duet")),
            "disable_stitch": not bool(queue.get("allow_stitch")),
            "brand_content_toggle": bool(
                queue.get("brand_content_toggle")
            ),
            "brand_organic_toggle": bool(
                queue.get("brand_organic_toggle")
            ),
            "is_aigc": bool(queue.get("is_aigc")),
        }
        if manual_canary:
            payload["manual_canary_id"] = str(manual_canary_id or "")
            payload["material_id"] = str(queue.get("material_id") or "")
        return self._post(
            (
                "/internal/tt-post/canary-publish"
                if manual_canary
                else "/internal/tt-post/publish"
            ),
            payload,
            write_may_have_happened=True,
        )

    def reconcile(
        self,
        *,
        job_id: str,
        source_account_id: str,
        access_token: str,
    ) -> Dict[str, Any]:
        envelope = self._envelope(
            access_token,
            job_id=job_id,
            source_account_id=source_account_id,
            operation="reconcile",
        )
        return self._post(
            "/internal/tt-post/reconcile",
            {
                "job_id": job_id,
                "source_account_id": source_account_id,
                "credential_envelope": envelope,
            },
        )


def _job_id(prefix: str, identity: Any) -> str:
    safe_identity = re.sub(r"[^A-Za-z0-9_-]+", "-", str(identity or ""))[:48]
    return "%s-%s-%s" % (prefix, safe_identity or "item", secrets.token_hex(8))


def _required_gpu_job_id(value: Any) -> str:
    job_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{11,127}", job_id):
        raise TTPostServiceError(
            "tt_gpu_job_id_missing",
            "冻结任务缺少稳定TT GPU任务ID",
            409,
        )
    return job_id


def _strict_env_bool(source: Mapping[str, str], name: str) -> bool:
    raw = str(source.get(name, "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise TTPostServiceError(
        "tt_post_manual_canary_config_invalid",
        "%s must be 0 or 1" % name,
        500,
    )


@dataclass(frozen=True)
class ManualPublishCanary:
    """Fail-closed permit for one exact operator-triggered private post."""

    enabled: bool = False
    acknowledged: bool = False
    canary_id: str = ""
    expires_at_utc: str = ""
    account_id: str = ""
    pool_id: int = 0
    material_id: str = ""
    content_id: str = ""
    gpu_job_id: str = ""
    output_sha256: str = ""
    output_size: int = 0
    profile: str = ""

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "ManualPublishCanary":
        source = os.environ if environ is None else environ
        enabled = _strict_env_bool(
            source,
            "TT_POST_MANUAL_CANARY_ENABLED",
        )
        if not enabled:
            return cls()
        acknowledgement = str(
            source.get("TT_POST_MANUAL_CANARY_ACKNOWLEDGEMENT", "")
            or ""
        )
        acknowledged = secrets.compare_digest(
            acknowledgement,
            MANUAL_CANARY_ACKNOWLEDGEMENT,
        )
        try:
            canary_id = str(
                source.get("TT_POST_MANUAL_CANARY_ID", "") or ""
            ).strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{8,80}", canary_id):
                raise ValueError("canary ID")
            expires_at_raw = str(
                source.get(
                    "TT_POST_MANUAL_CANARY_EXPIRES_AT_UTC",
                    "",
                )
                or ""
            ).strip()
            expires_at = datetime.fromisoformat(
                expires_at_raw.replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                raise ValueError("expiry")
            expires_at = expires_at.astimezone(UTC)
            now = datetime.now(UTC)
            if expires_at > now + timedelta(hours=24):
                raise ValueError("expiry")
            account_id = _positive_decimal(
                source.get("TT_POST_MANUAL_CANARY_ACCOUNT_ID"),
                "TT manual canary account ID",
            )
            pool_id = _positive_int(
                source.get("TT_POST_MANUAL_CANARY_POOL_ID"),
                "TT manual canary pool ID",
            )
            material_id = _positive_decimal(
                source.get("TT_POST_MANUAL_CANARY_MATERIAL_ID"),
                "TT manual canary material ID",
                19,
            )
            content_id = str(
                source.get("TT_POST_MANUAL_CANARY_CONTENT_ID", "") or ""
            ).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", content_id):
                raise ValueError("content ID")
            gpu_job_id = _required_gpu_job_id(
                source.get("TT_POST_MANUAL_CANARY_GPU_JOB_ID")
            )
            output_sha256 = str(
                source.get("TT_POST_MANUAL_CANARY_OUTPUT_SHA256", "") or ""
            ).strip().lower()
            if not re.fullmatch(r"[a-f0-9]{64}", output_sha256):
                raise ValueError("output SHA-256")
            output_size = _positive_int(
                source.get("TT_POST_MANUAL_CANARY_OUTPUT_SIZE"),
                "TT manual canary output size",
                4 * 1024 * 1024 * 1024,
            )
            profile = str(
                source.get("TT_POST_MANUAL_CANARY_PROFILE", "") or ""
            ).strip()
            if (
                not profile
                or len(profile) > 128
                or not re.fullmatch(r"[A-Za-z0-9._-]+", profile)
            ):
                raise ValueError("profile")
        except (TTPostError, ValueError):
            raise TTPostServiceError(
                "tt_post_manual_canary_config_invalid",
                "TT manual canary target configuration is invalid",
                500,
            ) from None
        if not acknowledged:
            raise TTPostServiceError(
                "tt_post_manual_canary_config_invalid",
                "TT manual canary acknowledgement is invalid",
                500,
            )
        return cls(
            enabled=True,
            acknowledged=True,
            canary_id=canary_id,
            expires_at_utc=expires_at.isoformat().replace("+00:00", "Z"),
            account_id=account_id,
            pool_id=pool_id,
            material_id=material_id,
            content_id=content_id,
            gpu_job_id=gpu_job_id,
            output_sha256=output_sha256,
            output_size=output_size,
            profile=profile,
        )

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.acknowledged)

    def is_active(self, now: Optional[datetime] = None) -> bool:
        if not self.ready or not self.expires_at_utc:
            return False
        try:
            expires_at = datetime.fromisoformat(
                self.expires_at_utc.replace("Z", "+00:00")
            ).astimezone(UTC)
            current = (
                datetime.now(UTC)
                if now is None
                else _now_utc(lambda: now)
            )
        except (TypeError, ValueError, OverflowError):
            return False
        return bool(current < expires_at)

    def allows_manual_account(
        self,
        trigger_type: Any,
        account_id: Any,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        return bool(
            self.is_active(now)
            and str(trigger_type or "") == "manual"
            and secrets.compare_digest(
                self.account_id,
                str(account_id or ""),
            )
        )

    def matches_pool(
        self,
        account_id: Any,
        pool: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        if not self.is_active(now) or not isinstance(pool, Mapping):
            return False
        try:
            return bool(
                secrets.compare_digest(
                    self.account_id,
                    str(account_id or ""),
                )
                and int(pool.get("id") or 0) == self.pool_id
                and secrets.compare_digest(
                    self.material_id,
                    str(pool.get("material_id") or ""),
                )
                and secrets.compare_digest(
                    self.content_id,
                    str(pool.get("content_id") or ""),
                )
                and secrets.compare_digest(
                    self.gpu_job_id,
                    str(pool.get("gpu_job_id") or ""),
                )
                and secrets.compare_digest(
                    self.output_sha256,
                    str(pool.get("prepared_output_sha256") or "").lower(),
                )
                and int(pool.get("prepared_output_size") or 0)
                == self.output_size
                and secrets.compare_digest(
                    self.profile,
                    str(pool.get("preparation_profile") or ""),
                )
            )
        except (TypeError, ValueError, OverflowError):
            return False

    def public_state(
        self,
        *,
        ready: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        active = self.is_active(now)
        return {
            "enabled": bool(active),
            "ready": bool(active and ready),
            "privacy_level": "SELF_ONLY",
            "test_bypass": bool(active),
        }

    def identity(self) -> Dict[str, Any]:
        if not self.ready:
            raise TTPostServiceError(
                "tt_post_manual_canary_config_invalid",
                "TT manual canary configuration is invalid",
                500,
            )
        return {
            "canary_id": self.canary_id,
            "account_id": self.account_id,
            "pool_id": self.pool_id,
            "material_id": self.material_id,
            "content_id": self.content_id,
            "gpu_job_id": self.gpu_job_id,
            "output_sha256": self.output_sha256,
            "output_size": self.output_size,
            "profile": self.profile,
        }


def _creator_info_hash(creator: Mapping[str, Any]) -> str:
    canonical = {
        "comment_disabled": bool(creator.get("comment_disabled")),
        "creator_nickname": str(creator.get("creator_nickname") or ""),
        "creator_username": str(creator.get("creator_username") or ""),
        "duet_disabled": bool(creator.get("duet_disabled")),
        "max_video_post_duration_sec": int(
            creator.get("max_video_post_duration_sec") or 0
        ),
        "privacy_level_options": list(creator.get("privacy_level_options") or []),
        "stitch_disabled": bool(creator.get("stitch_disabled")),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _caption_template_from_frozen(caption: Any, content_id: Any) -> str:
    """Recover the template used by the legacy rendered-caption API."""

    text = _bounded_text(caption, "发布描述", 2200)
    normalized_content = _bounded_text(content_id, "Drama ID", 128)
    matches = CAPTION_DRAMA_LINE_RE.findall(text)
    if matches != [normalized_content]:
        raise TTPostServiceError(
            "caption_content_id_required",
            "发布描述必须保留唯一且准确的Drama ID行",
            400,
        )
    return CAPTION_DRAMA_LINE_RE.sub(
        "Drama ID: {{contect_id}}",
        text,
        count=1,
    )


def _caption_from_submission(
    payload: Mapping[str, Any],
    content_id: Any,
    *,
    description: Any = None,
    defer_description: bool = False,
) -> Tuple[str, str]:
    """Normalize editable templates while preserving legacy callers."""

    raw_template = payload.get("caption_template")
    raw_caption = payload.get("caption_text")
    if "caption_template" in payload:
        template = _bounded_text(
            raw_template,
            "发布描述模板",
            20000,
        )
        caption = render_caption_template(
            template,
            content_id,
            description=description,
            defer_url=True,
            defer_description=defer_description,
            defer_code=True,
        )
        if raw_caption not in (None, ""):
            submitted = _bounded_text(raw_caption, "发布描述", 2200)
            if not (
                defer_description and caption_uses_desc_macro(template)
            ):
                if not secrets.compare_digest(
                    submitted.encode("utf-8"),
                    caption.encode("utf-8"),
                ):
                    raise TTPostServiceError(
                        "tt_caption_template_render_mismatch",
                        "发布描述与模板按当前Drama ID渲染后的内容不一致",
                        400,
                    )
        return template, caption

    if raw_caption in (None, ""):
        return FIXED_CAPTION_TEMPLATE, render_fixed_caption(content_id)

    caption = _bounded_text(raw_caption, "发布描述", 2200)
    template = _caption_template_from_frozen(caption, content_id)
    rendered = render_caption_template(
        template,
        content_id,
        description=description,
        defer_url=True,
        defer_description=defer_description,
        defer_code=True,
    )
    if not secrets.compare_digest(
        rendered.encode("utf-8"),
        caption.encode("utf-8"),
    ):
        raise TTPostServiceError(
            "tt_caption_template_render_mismatch",
            "发布描述与Drama ID模板渲染结果不一致",
            400,
        )
    return template, rendered


def _normalized_creator_info(raw: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TTPostServiceError(
            "tt_creator_info_invalid",
            "TikTok账号实时信息无效",
            502,
        )
    nickname = _bounded_text(
        raw.get("creator_nickname"),
        "TikTok昵称",
        255,
        allow_empty=True,
    )
    username = _bounded_text(
        raw.get("creator_username"),
        "TikTok用户名",
        255,
        allow_empty=True,
    )
    if not nickname and not username:
        raise TTPostServiceError(
            "tt_creator_info_invalid",
            "TikTok未返回可确认的账号身份",
            502,
        )
    options = raw.get("privacy_level_options")
    if (
        not isinstance(options, list)
        or not options
        or any(
            not isinstance(item, str)
            or item
            not in {
                "PUBLIC_TO_EVERYONE",
                "MUTUAL_FOLLOW_FRIENDS",
                "FOLLOWER_OF_CREATOR",
                "SELF_ONLY",
            }
            for item in options
        )
    ):
        raise TTPostServiceError(
            "tt_creator_info_invalid",
            "TikTok未返回有效隐私选项",
            502,
        )
    try:
        maximum_duration = int(raw.get("max_video_post_duration_sec") or 0)
    except (TypeError, ValueError, OverflowError):
        maximum_duration = 0
    if maximum_duration <= 0 or maximum_duration > 3600:
        raise TTPostServiceError(
            "tt_creator_info_invalid",
            "TikTok视频时长能力无效",
            502,
        )
    result = {
        "creator_nickname": nickname,
        "creator_username": username,
        "creator_avatar_url": "",
        "privacy_level_options": list(dict.fromkeys(options)),
        "comment_disabled": _exact_bool(
            raw.get("comment_disabled"),
            "TikTok评论能力",
        ),
        "duet_disabled": _exact_bool(
            raw.get("duet_disabled"),
            "TikTok Duet能力",
        ),
        "stitch_disabled": _exact_bool(
            raw.get("stitch_disabled"),
            "TikTok Stitch能力",
        ),
        "max_video_post_duration_sec": maximum_duration,
    }
    avatar = str(raw.get("creator_avatar_url") or "").strip()
    if avatar:
        try:
            parsed_avatar = urllib.parse.urlsplit(avatar)
            if parsed_avatar.query or parsed_avatar.fragment:
                raise TTPostServiceError(
                    "tt_creator_avatar_signed_url_omitted",
                    "TikTok头像签名地址不向前端透传",
                    400,
                )
            result["creator_avatar_url"] = _safe_https_url(avatar, "TikTok头像")
        except TTPostError:
            result["creator_avatar_url"] = ""
    return result


class TTPostService:
    """Business facade used by both the AI-backend proxy and the runner."""

    def __init__(
        self,
        store: TTPostStore,
        account_repository: MySQLSnapshotAccountRepository,
        material_resolver: DramawaveMaterialResolver,
        gpu_client: GPUClient,
        *,
        gates: Optional[LiveGates] = None,
        manual_canary: Optional[ManualPublishCanary] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        source_trim_tail_seconds: float = 4.333333,
        media_profile_version: str = "tt-post-hevc-720x1280-v2",
        runner_kick_path: str = DEFAULT_RUNNER_KICK_PATH,
        preparation_kick_path: str = DEFAULT_PREPARATION_KICK_PATH,
        short_link_root: Any = DEFAULT_SHORT_LINK_ROOT,
        code_resolver: Optional[TTCodeRouteResolver] = None,
    ):
        self.store = store
        self.account_repository = account_repository
        self.account_source = account_repository.as_account_source()
        self.material_resolver = material_resolver
        self.gpu_client = gpu_client
        self.gates = LiveGates.from_env() if gates is None else gates
        self.manual_canary = (
            ManualPublishCanary.from_env()
            if manual_canary is None
            else manual_canary
        )
        if not isinstance(self.manual_canary, ManualPublishCanary):
            raise TTPostServiceError(
                "tt_post_manual_canary_config_invalid",
                "TT manual canary configuration is invalid",
                500,
            )
        self._now_fn = now_fn
        try:
            trim_seconds = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            trim_seconds = -1.0
        if (
            trim_seconds < 0
            or trim_seconds > 60
            or trim_seconds != trim_seconds
        ):
            raise TTPostServiceError(
                "tt_source_trim_invalid",
                "TT源素材去尾秒数配置无效",
                500,
            )
        self.source_trim_tail_seconds = round(trim_seconds, 6)
        self.media_profile_version = _bounded_text(
            media_profile_version,
            "TT媒体制作版本",
            128,
        )
        self.short_link_root = str(short_link_root or "").strip()
        self.code_resolver = code_resolver or TTCodeRouteResolver(
            self.store.db_path,
            lock=self.store.code_route_lock,
        )
        self.store.code_route_invalidator = (
            lambda _code: self.code_resolver.rotate_namespace()
        )
        normalized_kick_path = str(runner_kick_path or "").strip()
        if (
            os.name == "nt"
            and normalized_kick_path == DEFAULT_RUNNER_KICK_PATH
        ):
            normalized_kick_path = ""
        if normalized_kick_path:
            kick_path = Path(normalized_kick_path)
            if not kick_path.is_absolute():
                raise TTPostServiceError(
                    "tt_runner_kick_path_invalid",
                    "TT手动发布唤醒路径必须是绝对路径",
                    500,
                )
            if os.name != "nt" and not str(kick_path).startswith(
                "/run/tt-post/"
            ):
                raise TTPostServiceError(
                    "tt_runner_kick_path_invalid",
                    "TT手动发布唤醒路径必须位于/run/tt-post",
                    500,
                )
        self.runner_kick_path = normalized_kick_path
        normalized_prepare_kick = str(preparation_kick_path or "").strip()
        if (
            os.name == "nt"
            and normalized_prepare_kick == DEFAULT_PREPARATION_KICK_PATH
        ):
            normalized_prepare_kick = ""
        if normalized_prepare_kick:
            prepare_kick = Path(normalized_prepare_kick)
            if not prepare_kick.is_absolute():
                raise TTPostServiceError(
                    "tt_prepare_kick_path_invalid",
                    "TT preparation runner wakeup path must be absolute",
                    500,
                )
            if os.name != "nt" and not str(prepare_kick).startswith(
                "/run/tt-post/"
            ):
                raise TTPostServiceError(
                    "tt_prepare_kick_path_invalid",
                    "TT preparation runner wakeup path must be under /run/tt-post",
                    500,
                )
        self.preparation_kick_path = normalized_prepare_kick

    def _gates(self) -> Dict[str, bool]:
        return self.gates.as_dict()

    def resolve_code_route(self, query: Any, source: Any) -> Dict[str, Any]:
        try:
            return self.code_resolver.resolve(query, source)
        except TTCodeRouteError as exc:
            raise TTPostServiceError(exc.code, str(exc), exc.status) from None

    def _prepare_published_code_route_invalidation(
        self,
        queue: Mapping[str, Any],
    ) -> Optional[str]:
        if str(queue.get("status") or "") == "published":
            return self.code_resolver.prepare_latest_invalidation(
                queue.get("content_id")
            )
        return None

    def _freeze_queue_and_invalidate_code(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self.store.freeze_queue(*args, **kwargs)

    def _reconcile_published_with_cache(
        self,
        queue_id: Any,
        publish_id: Any,
        *,
        publish_url: Any = "",
    ) -> Dict[str, Any]:
        with self.store.code_route_lock:
            queue = self.store.reconcile_published(
                queue_id,
                publish_id,
                publish_url=publish_url,
            )
            old_cache_key = (
                self._prepare_published_code_route_invalidation(queue)
            )
        # Redis invalidation performs best-effort network I/O and must never
        # extend the shared SQLite mutation critical section.
        self.code_resolver.complete_invalidation(old_cache_key)
        return queue

    def _is_manual_canary_queue(
        self,
        queue: Mapping[str, Any],
    ) -> bool:
        if (
            not self.manual_canary.is_active(_now_utc(self._now_fn))
            or not isinstance(queue, Mapping)
            or str(queue.get("publish_mode") or "") != "direct_post"
            or str(queue.get("privacy_level") or "") != "SELF_ONLY"
            or bool(queue.get("allow_comment"))
            or bool(queue.get("allow_duet"))
            or bool(queue.get("allow_stitch"))
            or bool(queue.get("brand_content_toggle"))
            or bool(queue.get("brand_organic_toggle"))
        ):
            return False
        try:
            schedule = self.store.get_daily_schedule(
                queue.get("account_id")
            )
            if bool(schedule.get("enabled")):
                return False
            run = self.store.get_recurring_run_by_queue_id(queue.get("id"))
        except TTPostError:
            return False
        return bool(
            str(run.get("trigger_type") or "") == "manual"
            and str(run.get("run_key") or "").startswith(
                "tt-post:manual-canary:v1:%s:%s:"
                % (
                    self.manual_canary.canary_id,
                    self.manual_canary.account_id,
                )
            )
            and self.manual_canary.matches_pool(
                queue.get("account_id"),
                run.get("pool_item") or {},
                now=_now_utc(self._now_fn),
            )
            and secrets.compare_digest(
                str(queue.get("material_id") or ""),
                self.manual_canary.material_id,
            )
            and secrets.compare_digest(
                str(queue.get("content_id") or ""),
                self.manual_canary.content_id,
            )
            and secrets.compare_digest(
                str(queue.get("gpu_job_id") or ""),
                self.manual_canary.gpu_job_id,
            )
            and secrets.compare_digest(
                str(queue.get("prepared_output_sha256") or "").lower(),
                self.manual_canary.output_sha256,
            )
            and int(queue.get("prepared_output_size") or 0)
            == self.manual_canary.output_size
        )

    @staticmethod
    def _manual_canary_policy(
        pool_item: Mapping[str, Any],
    ) -> TTPostPolicy:
        return TTPostPolicy(
            privacy_level="SELF_ONLY",
            allow_comment=False,
            allow_duet=False,
            allow_stitch=False,
            brand_content_toggle=False,
            brand_organic_toggle=False,
            user_consent=True,
            consent_version=str(pool_item.get("consent_version") or ""),
            consented_at_utc=str(pool_item.get("consented_at_utc") or ""),
        )

    def _claim_lease_seconds(self) -> int:
        try:
            gpu_timeout = int(self.gpu_client.timeout)
        except (AttributeError, TypeError, ValueError, OverflowError):
            gpu_timeout = DEFAULT_LEASE_SECONDS
        return max(
            DEFAULT_LEASE_SECONDS,
            min(gpu_timeout, 3600) + CLAIM_LEASE_BUFFER_SECONDS,
        )

    @staticmethod
    def _scheduled_account_placeholder(account_id: str) -> Dict[str, Any]:
        """Return a credential-free management row for an unavailable account."""

        return {
            "source_account_id": account_id,
            "account_id": account_id,
            "status": "unavailable",
            "publish_eligible": False,
            "management_only": True,
            "eligibility_reason": (
                "该账号已不在当前安全发布候选列表中；"
                "只能查看并停用已有每日排期"
            ),
        }

    def accounts(self) -> Dict[str, Any]:
        items = []
        listed_account_ids = set()
        auto_config = self.store.get_auto_publish_config()
        auto_account_ids = {
            str(value) for value in auto_config.get("account_ids") or []
        }
        account_source_available = True
        try:
            source_accounts = self.account_repository.list_public_accounts()
        except AccountSourceError:
            # Fail closed: source failure must never make an account look
            # publishable, but local schedules must remain visible so an
            # operator can stop them.
            account_source_available = False
            source_accounts = []
        for account in source_accounts:
            item = dict(account)
            account_id = str(account["source_account_id"])
            item["account_settings"] = (
                self.store.get_account_settings(account_id)
                or {"configured": False, "drama_language": "en"}
            )
            items.append(item)
            listed_account_ids.add(account_id)
        for schedule in self.store.list_daily_schedules():
            account_id = str(schedule.get("account_id") or "")
            if not account_id or account_id in listed_account_ids:
                continue
            item = self._scheduled_account_placeholder(account_id)
            item["account_settings"] = (
                self.store.get_account_settings(account_id)
                or {"configured": False, "drama_language": "en"}
            )
            items.append(item)
            listed_account_ids.add(account_id)
        for item in items:
            account_id = str(item.get("source_account_id") or "")
            selected = account_id in auto_account_ids
            configured = bool(
                isinstance(item.get("account_settings"), Mapping)
                and item["account_settings"].get("configured")
            )
            if not selected:
                state = "not_selected"
            elif not auto_config.get("enabled"):
                state = "paused"
            elif (
                item.get("publish_eligible") is False
                or item.get("status") == "unavailable"
                or not configured
            ):
                state = "attention_required"
            else:
                state = "active"
            item["auto_publish_selected"] = selected
            item["auto_publish_state"] = state
            item["auto_publish_config_version"] = int(
                auto_config.get("version") or 0
            )
        result = {
            "items": items,
            "gates": self._gates(),
            "account_source_available": account_source_available,
            "auto_publish_config": auto_config,
        }
        if not account_source_available:
            result["warning"] = (
                "TikTok账号数据源暂不可用；仅显示本地已有排期，"
                "所有账号均不可发布"
            )
        return result

    def account_settings(self) -> Dict[str, Any]:
        return self.accounts()

    def auto_config_get(self) -> Dict[str, Any]:
        """Return the one saved template, schedule and account membership."""

        return {
            "item": self.store.get_auto_publish_config(),
            "gates": self._gates(),
        }

    def auto_config_save(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError(
                "invalid_request",
                "自动发布配置请求必须是对象",
                400,
            )
        allowed = {
            "expected_version",
            "enabled",
            "timezone",
            "publish_times",
            "schedule_mode",
            "random_daily_count",
            "source_account_ids",
            "caption_template",
            "consent",
        }
        required = allowed - {
            "consent",
            "schedule_mode",
            "random_daily_count",
        }
        if set(payload).difference(allowed) or not required.issubset(payload):
            raise TTPostServiceError(
                "invalid_request",
                "自动发布配置字段不完整或包含未知字段",
                400,
            )
        expected_version = payload.get("expected_version")
        if (
            type(expected_version) is not int
            or expected_version < 0
            or expected_version > 2**31 - 1
        ):
            raise TTPostServiceError(
                "tt_post_auto_config_version_required",
                "自动发布配置版本必须是非负整数",
                400,
            )
        enabled = _exact_bool(payload.get("enabled"), "自动发布开关")
        if str(payload.get("timezone") or "") != "Asia/Shanghai":
            raise TTPostServiceError(
                "tt_timezone_invalid",
                "自动发布只接受Asia/Shanghai时区",
                400,
            )
        publish_times = payload.get("publish_times")
        if (
            not isinstance(publish_times, list)
            or len(publish_times) > MAX_DAILY_PUBLISH_COUNT
        ):
            raise TTPostServiceError(
                "invalid_publish_times",
                "固定自动发布时间必须是最多24项的数组",
                400,
            )
        schedule_mode = str(
            payload.get("schedule_mode", "fixed") or ""
        ).strip().lower()
        if schedule_mode not in {"fixed", "random"}:
            raise TTPostServiceError(
                "invalid_schedule_mode",
                "自动发布模式必须是fixed或random",
                400,
            )
        random_daily_count = payload.get("random_daily_count", 0)
        if (
            type(random_daily_count) is not int
            or random_daily_count < 0
            or random_daily_count > MAX_DAILY_PUBLISH_COUNT
        ):
            raise TTPostServiceError(
                "invalid_random_daily_count",
                "每日随机发布次数必须是0到24的整数",
                400,
            )
        if schedule_mode == "random" and publish_times:
            raise TTPostServiceError(
                "tt_post_random_times_must_be_empty",
                "随机发布模式不能同时设置固定发布时间",
                400,
            )
        if enabled and schedule_mode == "fixed" and not publish_times:
            raise TTPostServiceError(
                "tt_post_auto_config_times_required",
                "启用固定自动发布前至少设置一个时间点",
                400,
            )
        if (
            enabled
            and schedule_mode == "random"
            and random_daily_count < 1
        ):
            raise TTPostServiceError(
                "invalid_random_daily_count",
                "启用随机自动发布前必须设置每天1到24次",
                400,
            )
        account_ids = _auto_config_account_ids(
            payload.get("source_account_ids")
        )
        if enabled and not account_ids:
            raise TTPostServiceError(
                "tt_post_auto_accounts_required",
                "启用自动发布前至少选择一个TikTok账号",
                400,
            )
        caption_template = _bounded_text(
            payload.get("caption_template"),
            "发布描述模板",
            20000,
        )
        # Syntax and UTF-16 sizing are checked now; material-specific {desc}
        # is still rendered and frozen when a material enters a pool.
        render_caption_template(
            caption_template,
            "VALID001",
            defer_url=True,
            defer_description=True,
            defer_code=True,
        )
        # A pure stop must not require the operator to create a new consent
        # record. The UI still submits an explicit accepted=false placeholder
        # when disabling; ignore it and let the core preserve prior consent.
        consent = self._consent_from_payload(payload) if enabled else None

        # Configuration membership is metadata-only. Volatile account state
        # and TikTok Creator Info are publish-time capabilities and must not
        # block saving edited configuration. Persisted local settings remain
        # the trust record for newly added account IDs.
        current_config = self.store.get_auto_publish_config()
        if expected_version != int(current_config.get("version") or 0):
            raise TTPostServiceError(
                "tt_post_auto_config_version_conflict",
                "Automatic publish config changed; refresh and retry",
                409,
            )
        existing_ids = {
            str(value)
            for value in current_config.get("account_ids") or []
        }
        ids_to_validate = [
            account_id
            for account_id in account_ids
            if account_id not in existing_ids
        ]
        for account_id in ids_to_validate:
            self.store.get_account_settings(account_id, required=True)

        if enabled:
            for account_id in account_ids:
                if self.manual_canary.allows_manual_account(
                    "manual",
                    account_id,
                    now=_now_utc(self._now_fn),
                ):
                    raise TTPostServiceError(
                        "tt_post_manual_canary_schedule_locked",
                        "一次性私密测试期间不能启用该账号自动发布",
                        409,
                    )

        saved = self.store.save_auto_publish_config(
            expected_version=expected_version,
            enabled=enabled,
            timezone="Asia/Shanghai",
            publish_times=publish_times,
            schedule_mode=schedule_mode,
            random_daily_count=random_daily_count,
            account_ids=account_ids,
            caption_template=caption_template,
            user_consent=(consent or {}).get("accepted"),
            consent_version=(consent or {}).get("version"),
            consented_at=(consent or {}).get("accepted_at"),
        )
        return {"item": saved, "gates": self._gates()}

    def _creator_info_for_account(
        self,
        account_id: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        account = self.account_repository.get_public_account(account_id)
        job_id = _job_id("ttcreator", account_id)
        with self.account_source.publish_credentials(account_id) as credentials:
            raw = self.gpu_client.creator_info(
                job_id=job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        item = raw.get("creator_info", raw)
        return account, _normalized_creator_info(item)

    def creator_info(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        _, item = self._creator_info_for_account(account_id)
        return {"item": item, "gates": self._gates()}

    @staticmethod
    def _common_creator_capabilities(
        creators: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        if not creators:
            raise TTPostServiceError(
                "invalid_batch_targets",
                "批量个号目标不能为空",
                400,
            )
        privacy_sets = [
            set(item.get("privacy_level_options") or [])
            for item in creators
        ]
        privacy_options = [
            value
            for value in PRIVACY_LEVEL_ORDER
            if all(value in options for options in privacy_sets)
        ]
        positive_durations = [
            int(item.get("max_video_post_duration_sec") or 0)
            for item in creators
            if int(item.get("max_video_post_duration_sec") or 0) > 0
        ]
        return {
            "account_count": len(creators),
            "privacy_level_options": privacy_options,
            "comment_disabled": not all(
                item.get("comment_disabled") is False for item in creators
            ),
            "duet_disabled": not all(
                item.get("duet_disabled") is False for item in creators
            ),
            "stitch_disabled": not all(
                item.get("stitch_disabled") is False for item in creators
            ),
            "max_video_post_duration_sec": (
                min(positive_durations) if positive_durations else 0
            ),
        }

    def _batch_creator_info(
        self,
        account_ids: Sequence[str],
    ) -> Dict[str, Any]:
        results: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        worker_count = min(CREATOR_INFO_BATCH_WORKERS, len(account_ids))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self._creator_info_for_account,
                    account_id,
                ): account_id
                for account_id in account_ids
            }
            for future in as_completed(futures):
                account_id = futures[future]
                try:
                    results[account_id] = future.result()
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    status = (
                        exc.status
                        if isinstance(exc, TTPostError)
                        and exc.status in (400, 404, 409)
                        else 502
                    )
                    raise TTPostServiceError(
                        "tt_batch_creator_info_failed",
                        "TikTok账号%s实时能力检测失败" % account_id,
                        status,
                    ) from None

        items = []
        creators = []
        for account_id in account_ids:
            account, creator = results[account_id]
            item = dict(account)
            item["account_settings"] = (
                self.store.get_account_settings(account_id)
                or {"configured": False, "drama_language": "en"}
            )
            item["creator_info"] = creator
            items.append(item)
            creators.append(creator)
        return {
            "items": items,
            "common_capabilities": self._common_creator_capabilities(creators),
            "gates": self._gates(),
        }

    def account_settings_batch_creator_info(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "source_account_ids"
        }:
            raise TTPostServiceError(
                "invalid_batch_targets",
                "批量能力检测请求字段无效",
                400,
            )
        return self._batch_creator_info(
            _batch_account_ids(payload.get("source_account_ids"))
        )

    def _preparation_job_id(
        self,
        resolved: Mapping[str, Any],
    ) -> str:
        return (
            "ttpreview-"
            + hashlib.sha256(
                (
                    str(resolved["material_id"])
                    + "|"
                    + str(resolved["content_id"])
                    + "|"
                    + hashlib.sha256(
                        str(resolved["source_media_url"]).encode("utf-8")
                    ).hexdigest()
                    + "|"
                    + self.media_profile_version
                    + "|"
                    + str(self.source_trim_tail_seconds)
                ).encode("utf-8")
            ).hexdigest()[:36]
        )

    def _prepare_resolved(
        self,
        resolved: Mapping[str, Any],
        *,
        gpu_job_id: str = "",
    ) -> Dict[str, Any]:
        resolved = dict(resolved)
        job_id = gpu_job_id or self._preparation_job_id(resolved)
        prepared = self.gpu_client.prepare(
            job_id=job_id,
            material=resolved,
            source_trim_tail_seconds=self.source_trim_tail_seconds,
            expected_profile=self.media_profile_version,
        )
        returned_job_id = str(prepared.get("job_id") or "").strip()
        returned_content = str(prepared.get("content_id") or "").strip()
        if (
            not secrets.compare_digest(returned_job_id, job_id)
            or returned_content != resolved["content_id"]
        ):
            raise TTPostServiceError(
                "tt_prepared_media_identity_mismatch",
                "TT最终成片身份与源素材不一致",
                409,
            )
        returned_profile = str(prepared.get("profile") or "").strip()
        if not secrets.compare_digest(
            returned_profile,
            self.media_profile_version,
        ):
            raise TTPostServiceError(
                "tt_prepared_media_profile_mismatch",
                "TT最终成片制作版本与当前任务版本不一致",
                409,
            )
        final_url = _safe_https_url(
            prepared.get("output_url")
            or prepared.get("prepared_media_url")
            or prepared.get("final_media_url"),
            "TT最终成片地址",
        )
        if secrets.compare_digest(
            final_url,
            str(resolved["source_media_url"]),
        ):
            raise TTPostServiceError(
                "tt_prepared_media_matches_source",
                "TT最终成片地址不能与源素材地址相同",
                409,
            )
        output_sha = str(prepared.get("output_sha256") or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", output_sha):
            raise TTPostServiceError(
                "tt_prepared_media_fingerprint_invalid",
                "TT最终成片指纹无效",
                502,
            )
        try:
            output_size = int(prepared.get("output_size"))
        except (TypeError, ValueError, OverflowError):
            output_size = 0
        probe = (
            prepared.get("probe")
            if isinstance(prepared.get("probe"), Mapping)
            else {}
        )
        try:
            duration = float(probe.get("duration") or 0)
        except (TypeError, ValueError, OverflowError):
            duration = 0
        if output_size <= 0 or duration <= 0:
            raise TTPostServiceError(
                "tt_prepared_media_metadata_invalid",
                "TT最终成片元数据无效",
                502,
            )
        result = dict(resolved)
        result.update(
            {
                "gpu_job_id": job_id,
                "media_url": final_url,
                "prepared_media_url": final_url,
                "final_media_url": final_url,
                "output_sha256": output_sha,
                "sha256": output_sha,
                "output_size": output_size,
                "media_size_bytes": output_size,
                "duration_sec": duration,
                "final_duration_sec": duration,
                "source_trim_tail_seconds": self.source_trim_tail_seconds,
                "profile": returned_profile[:128],
                "media_profile_version": self.media_profile_version,
                "status": str(prepared.get("status") or "ready")[:64],
                "status_label": str(
                    prepared.get("status_label") or "最终成片已准备"
                )[:128],
            }
        )
        return result

    def _resolve_and_prepare(
        self,
        material_id: Any,
        *,
        gpu_job_id: str = "",
    ) -> Dict[str, Any]:
        return self._prepare_resolved(
            self.material_resolver.resolve(material_id),
            gpu_job_id=gpu_job_id,
        )

    def material_preview(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        material_id = _positive_decimal(payload.get("material_id"), "素材ID", 19)
        resolved = dict(self.material_resolver.resolve(material_id))
        publication = self.store.get_material_publication_state(material_id)
        resolved.update(
            {
                "status": "validated",
                "status_label": "素材校验通过，可加入素材池",
                "preparation_status": "not_started",
                "publish_ready": False,
                **publication,
            }
        )
        return {
            "item": resolved,
            "gates": self._gates(),
        }

    def direct_tests_list(
        self,
        query: Mapping[str, Sequence[str]],
    ) -> Dict[str, Any]:
        try:
            page = int(self._query_first(query, "page", "1"))
            page_size = int(self._query_first(query, "page_size", "20"))
        except ValueError:
            raise TTPostServiceError(
                "invalid_request", "分页参数无效", 400
            ) from None
        if page < 1 or page_size < 1 or page_size > 100:
            raise TTPostServiceError("invalid_request", "分页参数无效", 400)
        account_id = self._query_first(query, "source_account_id")
        material_id = self._query_first(query, "material_id")
        status = self._query_first(query, "status")
        rows = self.store.list_direct_tests(
            account_id=account_id or None,
            material_id=material_id or None,
            status=status or None,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {
            "items": [self._direct_test_api_item(row) for row in rows],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "has_more": len(rows) == page_size,
            },
            "gates": self._gates(),
        }

    def direct_test_create(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        required = {
            "source_account_id",
            "material_id",
            "expected_config_version",
            "idempotency_key",
            "consent",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise TTPostServiceError(
                "invalid_request",
                "立即测试发布字段不完整或包含未知字段",
                400,
            )
        account_id = _positive_decimal(
            payload.get("source_account_id"), "TikTok账号ID"
        )
        material_id = _positive_decimal(
            payload.get("material_id"), "素材ID", 19
        )
        request_key = _bounded_text(
            payload.get("idempotency_key"), "立即测试发布幂等键", 255
        )
        expected_version = payload.get("expected_config_version")
        if type(expected_version) is not int or expected_version <= 0:
            raise TTPostServiceError(
                "tt_post_auto_config_version_required",
                "请先保存自动发布配置",
                409,
            )
        # Consent is part of the immutable request identity. Parsing it here is
        # deterministic and local, so an exact replay still skips gates and all
        # account/material/TikTok network dependencies.
        consent = self._consent_from_payload(payload)
        existing_by_key = self.store.get_direct_test_by_idempotency_key(
            request_key
        )
        if existing_by_key is not None:
            if (
                str(existing_by_key.get("account_id") or "") == account_id
                and str(existing_by_key.get("material_id") or "")
                == material_id
                and int(existing_by_key.get("config_version") or 0)
                == expected_version
                and secrets.compare_digest(
                    str(existing_by_key.get("consent_version") or ""),
                    consent["version"],
                )
                and secrets.compare_digest(
                    str(existing_by_key.get("consented_at_utc") or ""),
                    consent["accepted_at"],
                )
            ):
                return {
                    "item": self._direct_test_api_item(existing_by_key),
                    "preparation_wakeup_requested": False,
                    "preparation_timer_fallback_seconds": 60,
                    "gates": self._gates(),
                }
            raise TTPostServiceError(
                "tt_post_direct_test_idempotency_conflict",
                "立即测试发布幂等键已用于不同请求",
                409,
            )
        if not self.gates.is_open:
            raise TTPostServiceError(
                "tt_post_live_gates_closed",
                "发布门禁尚未全部开放，本次未创建测试任务",
                409,
            )
        config = self.store.get_auto_publish_config()
        if expected_version != int(config.get("version") or 0):
            raise TTPostServiceError(
                "tt_post_auto_config_version_conflict",
                "自动发布配置已变化，请刷新后重试",
                409,
            )
        account = self.account_repository.get_public_account(account_id)
        saved_settings = self.store.get_account_settings(
            account_id,
            required=True,
        )
        settings = TTPostAccountSettings.from_mapping(
            {
                key: saved_settings[key]
                for key in (
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                    "drama_language",
                )
            }
        )
        if (
            settings.privacy_level != "PUBLIC_TO_EVERYONE"
            or not settings.allow_comment
        ):
            raise TTPostServiceError(
                "tt_post_direct_test_public_comment_required",
                "立即测试账号必须已保存为所有人可见且允许评论",
                409,
            )
        creator = self.creator_info(
            {"source_account_id": account_id}
        )["item"]
        self._assert_creator_settings(creator, settings)
        resolved = dict(self.material_resolver.resolve(material_id))

        # A same-key retry is allowed. A different active or unknown attempt
        # for the same material is fail-closed until it reaches a clear result.
        for existing in self.store.list_direct_tests(
            material_id=material_id,
            limit=1000,
        ):
            if str(existing.get("status") or "") in {
                "queued",
                "preparing",
                "ready",
                "publishing",
                "reconciling",
                "unknown",
            }:
                raise TTPostServiceError(
                    "tt_post_direct_test_active",
                    "该素材仍有未明确结束的测试任务，请先等待或核对",
                    409,
                )
        for queue in self.store.list_queues():
            if (
                str(queue.get("material_id") or "") == material_id
                and str(queue.get("status") or "")
                in {"scheduled", "claimed", "publishing", "reconciling", "unknown"}
            ):
                raise TTPostServiceError(
                    "tt_post_material_publish_active",
                    "该素材正被自动发布或等待核对，暂不能同时测试",
                    409,
                )

        identity = "%s|%s|%s" % (account_id, material_id, request_key)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        gpu_job_id = "tttest-" + digest[:48]
        caption_template = str(config.get("caption_template") or "")
        if caption_uses_code_macro(caption_template):
            raise TTPostServiceError(
                "tt_post_code_macro_queue_only",
                "{code} is available only for formal TT queue publishing",
                409,
            )
        if caption_uses_url_macro(caption_template):
            link_id = direct_test_short_link_id(identity)
            short_url = build_short_url(link_id)
        else:
            link_id = 0
            short_url = ""
        caption = render_caption_template(
            caption_template,
            resolved["content_id"],
            url=short_url or None,
            description=resolved.get("description"),
        )
        synced_at = _now_utc(self._now_fn).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        item = self.store.create_direct_test(
            material_id,
            account_id,
            resolved["content_id"],
            resolved["source_media_url"],
            idempotency_key=request_key,
            gpu_job_id=gpu_job_id,
            source_trim_tail_seconds=self.source_trim_tail_seconds,
            preparation_profile=self.media_profile_version,
            caption_template=caption_template,
            caption=caption,
            short_link_id=link_id,
            short_url=short_url,
            settings=settings,
            consent_version=consent["version"],
            consented_at=consent["accepted_at"],
            config_version=expected_version,
            material_name=resolved.get("material_name"),
            drama_name=resolved.get("drama_name"),
            material_language=resolved.get("material_language"),
            material_tag=resolved.get("material_tag"),
            description=resolved.get("description"),
            account_username=(
                account.get("username")
                or account.get("account_username")
                or creator.get("creator_username")
                or ""
            ),
            account_display_name=(
                account.get("display_name")
                or account.get("account_name")
                or creator.get("creator_nickname")
                or ""
            ),
            creator_nickname_snapshot=creator.get("creator_nickname"),
            creator_username_snapshot=creator.get("creator_username"),
            creator_info_hash=_creator_info_hash(creator),
            creator_info_synced_at=synced_at,
        )
        kicked = self._kick_preparation_runner()
        return {
            "item": self._direct_test_api_item(item),
            "preparation_wakeup_requested": kicked,
            "preparation_timer_fallback_seconds": 60,
            "gates": self._gates(),
        }

    def _consent_from_payload(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        consent = payload.get("consent")
        if not isinstance(consent, Mapping):
            raise TTPostServiceError(
                "tt_post_consent_required",
                "发布确认信息不完整",
                400,
            )
        accepted = _exact_bool(consent.get("accepted"), "发布确认")
        if not accepted:
            raise TTPostServiceError(
                "tt_post_consent_required",
                "必须核对当前任务并显式确认后才能排期",
                409,
            )
        return {
            "accepted": accepted,
            "version": _bounded_text(
                consent.get("version"),
                "发布确认版本",
                64,
            ),
            "accepted_at": _utc_iso(consent.get("accepted_at")),
        }

    @staticmethod
    def _account_settings_from_payload(
        payload: Mapping[str, Any],
    ) -> TTPostAccountSettings:
        allowed = set(ACCOUNT_SETTINGS_VALUE_FIELDS).union(
            {
                "source_account_id",
                "expected_version",
            }
        )
        if set(payload).difference(allowed):
            raise TTPostServiceError(
                "invalid_request",
                "个号发布设置包含未知字段",
                400,
            )
        commercial = _exact_bool(
            payload.get("commercial_disclosure"),
            "商业内容披露",
        )
        brand_organic = _exact_bool(
            payload.get("brand_organic_toggle"),
            "自有品牌披露",
        )
        brand_content = _exact_bool(
            payload.get("brand_content_toggle"),
            "第三方品牌披露",
        )
        if commercial != bool(brand_organic or brand_content):
            raise TTPostServiceError(
                "tt_commercial_disclosure_invalid",
                "商业内容披露选项不一致",
                400,
            )
        return TTPostAccountSettings.from_mapping(
            {
                "privacy_level": payload.get("privacy_level"),
                "allow_comment": _exact_bool(
                    payload.get("allow_comment"),
                    "评论开关",
                ),
                "allow_duet": _exact_bool(
                    payload.get("allow_duet"),
                    "Duet开关",
                ),
                "allow_stitch": _exact_bool(
                    payload.get("allow_stitch"),
                    "Stitch开关",
                ),
                "brand_content_toggle": brand_content,
                "brand_organic_toggle": brand_organic,
                "is_aigc": _exact_bool(
                    payload.get("is_aigc"),
                    "AI内容声明",
                ),
                "drama_language": normalize_drama_language(
                    payload.get("drama_language", "en")
                ),
            }
        )

    @staticmethod
    def _automatic_policy_from_account_settings(
        settings: TTPostAccountSettings,
        consent: Mapping[str, Any],
    ) -> TTPostPolicy:
        """Build an automatic queue policy with disclosure always disabled."""

        return TTPostPolicy.from_mapping(
            {
                "privacy_level": settings.privacy_level,
                "allow_comment": settings.allow_comment,
                "allow_duet": settings.allow_duet,
                "allow_stitch": settings.allow_stitch,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "user_consent": consent.get("accepted"),
                "consent_version": consent.get("version"),
                "consented_at": consent.get("accepted_at"),
            }
        )

    @staticmethod
    def _assert_creator_settings(
        creator: Mapping[str, Any],
        settings: Any,
    ) -> None:
        if settings.privacy_level not in set(creator["privacy_level_options"]):
            raise TTPostServiceError(
                "tt_privacy_not_allowed",
                "所选隐私级别不在TikTok实时允许范围内",
                409,
            )
        for enabled, disabled_field, label in (
            (settings.allow_comment, "comment_disabled", "评论"),
            (settings.allow_duet, "duet_disabled", "Duet"),
            (settings.allow_stitch, "stitch_disabled", "Stitch"),
        ):
            if enabled and creator.get(disabled_field) is not False:
                raise TTPostServiceError(
                    "tt_interaction_not_allowed",
                    "TikTok实时能力不允许开启%s" % label,
                    409,
                )

    @classmethod
    def _assert_creator_policy(
        cls,
        creator: Mapping[str, Any],
        policy: TTPostPolicy,
    ) -> None:
        cls._assert_creator_settings(creator, policy)

    def account_settings_save(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError("invalid_request", "请求体必须是对象", 400)
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        settings = self._account_settings_from_payload(payload)
        raw_expected_version = payload.get("expected_version")
        if raw_expected_version in (None, ""):
            raise TTPostServiceError(
                "invalid_account_settings_version",
                "个号发布设置版本不能为空",
                400,
            )
        expected_version = _account_settings_version(raw_expected_version)
        account = self.account_repository.get_public_account(account_id)
        creator_result = self.creator_info({"source_account_id": account_id})
        creator = creator_result["item"]
        self._assert_creator_settings(creator, settings)
        saved = self.store.save_account_settings(
            account_id,
            settings,
            expected_version=expected_version,
        )
        item = dict(account)
        item["account_settings"] = saved
        item["creator_info"] = creator
        return {"item": item, "gates": self._gates()}

    def account_settings_batch_save(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError("invalid_request", "请求体必须是对象", 400)
        allowed = set(ACCOUNT_SETTINGS_VALUE_FIELDS).union({"targets"})
        if set(payload).difference(allowed):
            raise TTPostServiceError(
                "invalid_request",
                "批量个号发布设置包含未知字段",
                400,
            )
        targets = _batch_targets(payload.get("targets"))
        settings_payload = {
            key: payload.get(key) for key in ACCOUNT_SETTINGS_VALUE_FIELDS
        }
        settings_payload.update(
            {
                "source_account_id": targets[0]["source_account_id"],
                "expected_version": targets[0]["expected_version"],
            }
        )
        settings = self._account_settings_from_payload(settings_payload)
        account_ids = [target["source_account_id"] for target in targets]
        detected = self._batch_creator_info(account_ids)
        detected_by_id = {
            str(item["source_account_id"]): item
            for item in detected["items"]
        }
        for account_id in account_ids:
            self._assert_creator_settings(
                detected_by_id[account_id]["creator_info"],
                settings,
            )

        saved_items = self.store.save_account_settings_batch(
            [
                {
                    "account_id": target["source_account_id"],
                    "settings": settings,
                    "expected_version": target["expected_version"],
                }
                for target in targets
            ]
        )
        saved_by_id = {
            str(item["account_id"]): item for item in saved_items
        }
        items = []
        for account_id in account_ids:
            item = dict(detected_by_id[account_id])
            item["account_settings"] = saved_by_id[account_id]
            items.append(item)
        return {
            "items": items,
            "saved_count": len(items),
            "common_capabilities": detected["common_capabilities"],
            "gates": self._gates(),
        }

    @staticmethod
    def _query_first(
        query: Mapping[str, Sequence[str]],
        name: str,
        default: str = "",
    ) -> str:
        values = query.get(name, ())
        return str(values[0] if values else default).strip()

    @staticmethod
    def _recurring_pool_api_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["material_language"] = str(
            result.get("material_language") or "en"
        )
        result["source_account_id"] = str(result.get("account_id") or "")
        result["caption_text"] = str(result.get("caption") or "")
        result["duration_sec"] = float(
            result.get("prepared_duration_sec") or 0
        )
        result["preparation_status"] = "ready"
        result["publish_ready"] = True
        result["pool_item_type"] = "ready"
        return result

    @staticmethod
    def _material_intake_api_item(
        item: Mapping[str, Any],
    ) -> Dict[str, Any]:
        result = dict(item)
        preparation_status = str(
            result.get("preparation_status")
            or result.get("status")
            or "queued"
        )
        result["preparation_status"] = preparation_status
        result["material_language"] = str(
            result.get("material_language") or "en"
        )
        result["source_account_id"] = str(result.get("account_id") or "")
        result["caption_text"] = str(result.get("caption") or "")
        result["duration_sec"] = float(
            result.get("prepared_duration_sec") or 0
        )
        result["publish_ready"] = preparation_status == "ready"
        result["pool_item_type"] = "intake"
        result["pool_item_id"] = result.get("recurring_pool_id")
        result["retryable"] = preparation_status == "failed"
        return result

    @staticmethod
    def _direct_test_api_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        status = str(result.get("status") or "queued")
        result["direct_test_id"] = result.get("id")
        result["source_account_id"] = str(result.get("account_id") or "")
        result["caption_text"] = str(result.get("caption") or "")
        result["duration_sec"] = float(
            result.get("prepared_duration_sec") or 0
        )
        result["preparation_status"] = (
            status if status in {"queued", "preparing", "failed", "canceled"}
            else "ready"
        )
        result["publication_status"] = (
            "published"
            if status == "published"
            else "unknown"
            if status == "unknown"
            else "unpublished"
        )
        result["publish_ready"] = status == "ready"
        result["task_type"] = "direct_test"
        return result

    @staticmethod
    def _recurring_run_api_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result.pop("manual_request_key", None)
        result.pop("execution_token", None)
        result.pop("execution_lease_expires_at_utc", None)
        result["run_id"] = result.get("id")
        result["source_account_id"] = str(result.get("account_id") or "")
        pool = result.get("pool_item")
        if isinstance(pool, Mapping):
            result["pool_item"] = TTPostService._recurring_pool_api_item(pool)
            result["material_id"] = str(pool.get("material_id") or "")
            result["content_id"] = str(pool.get("content_id") or "")
        return result

    def _next_schedule_at(
        self,
        schedule: Mapping[str, Any],
    ) -> str:
        if not schedule.get("enabled"):
            return ""
        publish_times = schedule.get("publish_times")
        if not isinstance(publish_times, list) or not publish_times:
            return ""
        now_shanghai = _now_utc(self._now_fn).astimezone(BEIJING_TZ)
        for offset in range(0, 3):
            local_day = (now_shanghai + timedelta(days=offset)).date()
            for publish_time in sorted(str(item) for item in publish_times):
                try:
                    hour, minute = (int(part) for part in publish_time.split(":"))
                    candidate = datetime(
                        local_day.year,
                        local_day.month,
                        local_day.day,
                        hour,
                        minute,
                        tzinfo=BEIJING_TZ,
                    )
                except (TypeError, ValueError):
                    continue
                if candidate > now_shanghai:
                    return candidate.astimezone(UTC).isoformat().replace(
                        "+00:00",
                        "Z",
                    )
        return ""

    def _schedule_api_item(
        self,
        schedule: Mapping[str, Any],
    ) -> Dict[str, Any]:
        item = dict(schedule)
        account_id = str(item.get("account_id") or "")
        item["source_account_id"] = account_id
        publish_times = (
            list(item.get("publish_times") or [])
            if isinstance(item.get("publish_times"), list)
            else []
        )
        item["publish_times"] = publish_times
        item["publish_time"] = publish_times[0] if publish_times else ""
        item["configured"] = int(item.get("version") or 0) > 0
        item["next_run_at"] = self._next_schedule_at(item)
        saved_settings = (
            self.store.get_account_settings(account_id)
            if account_id
            else None
        )
        drama_language = normalize_drama_language(
            (saved_settings or {}).get("drama_language", "en")
        )
        item["drama_language"] = drama_language
        item["available_material_count"] = (
            self.store.count_recurring_materials(
                status="available",
                drama_language=drama_language,
            )
            if account_id and saved_settings is not None
            else 0
        )
        item["manual_available_material_count"] = (
            self.store.count_recurring_materials(
                account_id=account_id,
                status="available",
            )
            if account_id
            else 0
        )
        next_available = (
            self.store.list_recurring_materials(
                account_id=account_id,
                status="available",
                limit=1,
            )
            if account_id
            else []
        )
        manual_canary_ready = bool(
            not item.get("enabled")
            and next_available
            and self.manual_canary.matches_pool(
                account_id,
                next_available[0],
                now=_now_utc(self._now_fn),
            )
        )
        item["manual_canary"] = self.manual_canary.public_state(
            ready=manual_canary_ready,
            now=_now_utc(self._now_fn),
        )
        item["manual_canary_ready"] = manual_canary_ready
        active_manual_canary_account = bool(
            account_id
            and account_id == self.manual_canary.account_id
            and self.manual_canary.is_active(_now_utc(self._now_fn))
        )
        item["can_publish_now"] = bool(
            manual_canary_ready
            if active_manual_canary_account
            else (
                item["manual_available_material_count"] > 0
                and self.gates.is_open
            )
        )
        return item

    def material_pool_list(
        self,
        query: Mapping[str, Sequence[str]],
    ) -> Dict[str, Any]:
        try:
            page = int(self._query_first(query, "page", "1"))
            page_size = int(self._query_first(query, "page_size", "20"))
        except ValueError:
            raise TTPostServiceError(
                "invalid_request",
                "分页参数无效",
                400,
            ) from None
        if page < 1 or page_size < 1 or page_size > 100:
            raise TTPostServiceError("invalid_request", "分页参数无效", 400)
        account_filter = self._query_first(query, "source_account_id")
        material_filter = self._query_first(query, "material_id")
        status_filter = self._query_first(query, "status")

        def load_all(loader: Any) -> List[Dict[str, Any]]:
            loaded: List[Dict[str, Any]] = []
            offset = 0
            while True:
                batch = loader(limit=1000, offset=offset)
                loaded.extend(batch)
                if len(batch) < 1000:
                    return loaded
                offset += len(batch)

        intake_rows = load_all(self.store.list_material_intakes)
        recurring_rows = load_all(self.store.list_recurring_materials)
        recurring_by_id = {
            int(row["id"]): row
            for row in recurring_rows
        }
        linked_pool_ids = {
            int(row.get("recurring_pool_id") or 0)
            for row in intake_rows
            if int(row.get("recurring_pool_id") or 0) > 0
        }
        rows = []
        for intake_row in intake_rows:
            item = self._material_intake_api_item(intake_row)
            recurring = recurring_by_id.get(
                int(intake_row.get("recurring_pool_id") or 0)
            )
            if recurring is not None:
                preparation_account_id = str(
                    item.get("source_account_id") or ""
                )
                publish_account_id = str(
                    recurring.get("account_id") or ""
                )
                item["preparation_account_id"] = preparation_account_id
                item["account_id"] = publish_account_id
                item["source_account_id"] = publish_account_id
                if publish_account_id != preparation_account_id:
                    item["account_name_snapshot"] = ""
                item["status"] = str(recurring.get("status") or "")
                item["run_id"] = recurring.get("run_id")
                item["queue_id"] = recurring.get("queue_id")
            rows.append(item)
        rows.extend(
            self._recurring_pool_api_item(row)
            for row in recurring_rows
            if int(row.get("id") or 0) not in linked_pool_ids
        )
        if account_filter:
            normalized_account_filter = _positive_decimal(
                account_filter,
                "TikTok账号ID",
            )
            rows = [
                row
                for row in rows
                if str(row.get("source_account_id") or "")
                == normalized_account_filter
            ]
        if material_filter:
            material_id = _positive_decimal(
                material_filter,
                "素材ID",
                19,
            )
            rows = [
                row for row in rows
                if str(row.get("material_id") or "") == material_id
            ]
        if status_filter:
            rows = [
                row
                for row in rows
                if (
                    str(row.get("preparation_status") or "") == status_filter
                    or str(row.get("status") or "") == status_filter
                )
            ]
        material_ids = list(
            dict.fromkeys(
                str(row.get("material_id") or "")
                for row in rows
                if str(row.get("material_id") or "")
            )
        )
        publication_states: Dict[str, Dict[str, Any]] = {}
        for index in range(0, len(material_ids), 1000):
            publication_states.update(
                self.store.get_material_publication_states(
                    material_ids[index : index + 1000]
                )
            )
        for row in rows:
            row.update(
                publication_states.get(
                    str(row.get("material_id") or ""),
                    {
                        "publication_status": "unpublished",
                        "publish_count": 0,
                        "unknown_count": 0,
                    },
                )
            )
        rows.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                int(row.get("id") or 0),
            ),
            reverse=True,
        )
        total = len(rows)
        start = (page - 1) * page_size
        preparation_counts = {
            status: sum(
                str(row.get("preparation_status") or "") == status
                for row in rows
            )
            for status in (
                "queued",
                "preparing",
                "retry_wait",
                "ready",
                "failed",
                "canceled",
            )
        }
        return {
            "items": rows[start : start + page_size],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
            },
            "summary": {
                "total": total,
                **preparation_counts,
                "available": sum(
                    bool(row.get("publish_ready"))
                    and str(row.get("status") or "") == "available"
                    for row in rows
                ),
                "reserved": sum(
                    str(row.get("status") or "") == "reserved"
                    for row in rows
                ),
                "consumed": sum(
                    str(row.get("status") or "") == "consumed"
                    for row in rows
                ),
                "published": sum(
                    str(row.get("publication_status") or "") == "published"
                    for row in rows
                ),
                "unpublished": sum(
                    str(row.get("publication_status") or "") == "unpublished"
                    for row in rows
                ),
                "unknown_publication": sum(
                    str(row.get("publication_status") or "") == "unknown"
                    for row in rows
                ),
            },
            "gates": self._gates(),
        }

    def material_pool_add(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError("invalid_request", "请求体必须是对象", 400)
        allowed = {
            "idempotency_key",
            "source_account_id",
            "material_id",
            "content_id",
            "expected_config_version",
            "caption_template",
            "caption_text",
            "consent",
        }
        if set(payload).difference(allowed):
            raise TTPostServiceError(
                "invalid_request",
                "素材入池请求包含未知字段",
                400,
            )
        idempotency_key = _bounded_text(
            payload.get("idempotency_key"),
            "幂等键",
            255,
        )
        material_id = _positive_decimal(
            payload.get("material_id"),
            "素材ID",
            19,
        )
        expected_config_version = payload.get("expected_config_version")
        auto_config = None
        account = None
        if expected_config_version is not None:
            auto_config = self.store.get_auto_publish_config()
            if (
                type(expected_config_version) is not int
                or expected_config_version <= 0
                or expected_config_version
                != int(auto_config.get("version") or 0)
            ):
                raise TTPostServiceError(
                    "tt_post_auto_config_version_conflict",
                    "自动发布配置已变化，请刷新并保存后再加入素材",
                    409,
                )
            configured_account_ids = [
                str(value) for value in auto_config.get("account_ids") or []
            ]
            supplied_account_id = payload.get("source_account_id")
            if supplied_account_id in (None, ""):
                account_id = _assigned_auto_account_id(
                    material_id,
                    configured_account_ids,
                )
            else:
                account_id = _positive_decimal(
                    supplied_account_id,
                    "TikTok账号ID",
                )
            if account_id not in set(configured_account_ids):
                raise TTPostServiceError(
                    "tt_post_auto_account_not_selected",
                    "该TikTok账号尚未加入自动发布配置",
                    409,
                )
        else:
            account_id = _positive_decimal(
                payload.get("source_account_id"),
                "TikTok账号ID",
            )
            account = self.account_repository.get_public_account(account_id)
        requested_content_id = _bounded_text(
            payload.get("content_id"),
            "Drama ID",
            128,
        )
        consent = self._consent_from_payload(payload)
        saved_settings = self.store.get_account_settings(
            account_id,
            required=True,
        )
        settings = TTPostAccountSettings.from_mapping(
            {
                key: saved_settings[key]
                for key in (
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                    "drama_language",
                )
            }
        )
        resolved = dict(self.material_resolver.resolve(material_id))
        if resolved["content_id"] != requested_content_id:
            raise TTPostServiceError(
                "tt_content_id_mismatch",
                "页面Drama ID与素材真实映射不一致",
                409,
            )
        if auto_config is None:
            # Compatibility for the previous version during a rolling deploy.
            # The new UI always supplies expected_config_version and therefore
            # always uses the atomic saved template/account membership.
            caption_template, caption = _caption_from_submission(
                payload,
                resolved["content_id"],
                description=resolved.get("description"),
            )
        else:
            caption_template = str(auto_config.get("caption_template") or "")
            caption = render_caption_template(
                caption_template,
                resolved["content_id"],
                description=resolved.get("description"),
                defer_url=True,
                defer_code=True,
            )
        submitted_template = payload.get("caption_template")
        submitted_caption = payload.get("caption_text")
        if auto_config is not None and submitted_template not in (None, "") and not secrets.compare_digest(
            str(submitted_template).encode("utf-8"),
            caption_template.encode("utf-8"),
        ):
            raise TTPostServiceError(
                "tt_post_auto_config_version_conflict",
                "页面描述模板不是当前已保存版本",
                409,
            )
        if auto_config is not None and submitted_caption not in (None, "") and not secrets.compare_digest(
            str(submitted_caption).strip().encode("utf-8"),
            caption.encode("utf-8"),
        ):
            raise TTPostServiceError(
                "tt_caption_template_render_mismatch",
                "页面描述与当前已保存模板不一致",
                409,
            )
        item = self.store.add_material_intake(
            material_id,
            account_id,
            resolved["content_id"],
            resolved["source_media_url"],
            idempotency_key=idempotency_key,
            gpu_job_id=self._preparation_job_id(resolved),
            source_trim_tail_seconds=self.source_trim_tail_seconds,
            preparation_profile=self.media_profile_version,
            caption_template=caption_template,
            caption=caption,
            consent_version=consent["version"],
            consented_at=consent["accepted_at"],
            is_aigc=settings.is_aigc,
            material_name=resolved.get("material_name"),
            drama_name=resolved.get("drama_name"),
            material_language=resolved.get("material_language"),
            material_tag=resolved.get("material_tag"),
            description=resolved.get("description"),
        )
        result = self._material_intake_api_item(item)
        result["account_name_snapshot"] = str(
            (account or {}).get("display_name")
            or (account or {}).get("account_name")
            or account_id
        )
        kicked = self._kick_preparation_runner()
        return {
            "item": result,
            "available_material_count": self.store.count_recurring_materials(
                status="available",
                drama_language=resolved.get("material_language"),
            ),
            "preparation_wakeup_requested": kicked,
            "preparation_timer_fallback_seconds": 60,
            "gates": self._gates(),
        }

    def preparation_claim(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "worker_id",
            "lease_seconds",
        }:
            raise TTPostServiceError(
                "invalid_request",
                "后台制作领取请求字段无效",
                400,
            )
        worker_id = _bounded_text(
            payload.get("worker_id"),
            "后台制作 worker ID",
            128,
        )
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", worker_id):
            raise TTPostServiceError(
                "invalid_request",
                "后台制作 worker ID 无效",
                400,
            )
        lease_seconds = _positive_int(
            payload.get("lease_seconds"),
            "后台制作租约",
            MAX_PREPARATION_LEASE_SECONDS,
        )
        claim = self.store.claim_material_intake(
            worker_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return {"item": None, "gates": self._gates()}
        return {
            "item": self._material_intake_api_item(claim.item),
            "claim_token": claim.reveal_claim_token(),
            "gates": self._gates(),
        }

    def preparation_renew(
        self,
        intake_id: Any,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "claim_token",
            "lease_seconds",
        }:
            raise TTPostServiceError(
                "invalid_request",
                "后台制作续租请求字段无效",
                400,
            )
        normalized_id = _positive_int(
            intake_id,
            "后台制作任务 ID",
        )
        claim_token = _bounded_text(
            payload.get("claim_token"),
            "后台制作领取凭据",
            512,
        )
        lease_seconds = _positive_int(
            payload.get("lease_seconds"),
            "后台制作租约",
            MAX_PREPARATION_LEASE_SECONDS,
        )
        item = self.store.renew_material_intake(
            normalized_id,
            claim_token,
            lease_seconds=lease_seconds,
        )
        return {
            "item": self._material_intake_api_item(item),
            "gates": self._gates(),
        }

    def _preparation_retry_at(
        self,
        attempt_count: int,
    ) -> str:
        exponent = max(0, min(int(attempt_count) - 1, 6))
        delay_seconds = min(1800, 30 * (2**exponent))
        digest = hashlib.sha256(
            ("%s|%s" % (attempt_count, self.media_profile_version)).encode(
                "utf-8"
            )
        ).digest()
        jitter = int.from_bytes(digest[:2], "big") % max(1, delay_seconds)
        return (
            _now_utc(self._now_fn)
            + timedelta(seconds=delay_seconds + jitter)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def preparation_process(
        self,
        intake_id: Any,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "claim_token"
        }:
            raise TTPostServiceError(
                "invalid_request",
                "后台制作执行请求字段无效",
                400,
            )
        normalized_id = _positive_int(
            intake_id,
            "后台制作任务 ID",
        )
        claim_token = _bounded_text(
            payload.get("claim_token"),
            "后台制作领取凭据",
            512,
        )
        intake: Optional[Dict[str, Any]] = None
        try:
            intake = self.store.renew_material_intake(
                normalized_id,
                claim_token,
                lease_seconds=DEFAULT_PREPARATION_LEASE_SECONDS,
            )
            prepared = self._prepare_resolved(
                {
                    "material_id": str(intake["material_id"]),
                    "content_id": str(intake["content_id"]),
                    "source_media_url": str(intake["source_media_url"]),
                    "media_url": str(intake["source_media_url"]),
                },
                gpu_job_id=str(intake["gpu_job_id"]),
            )
            duration = float(prepared.get("duration_sec") or 0)
            if duration <= 0:
                raise TTPostServiceError(
                    "tt_prepared_media_duration_invalid",
                    "最终成片时长无效",
                    409,
                )
            item = self.store.complete_material_intake(
                normalized_id,
                claim_token,
                gpu_job_id=prepared["gpu_job_id"],
                prepared_media_url=prepared["prepared_media_url"],
                prepared_output_sha256=prepared["output_sha256"],
                prepared_output_size=prepared["output_size"],
                prepared_duration_sec=prepared["duration_sec"],
                source_trim_tail_seconds=prepared[
                    "source_trim_tail_seconds"
                ],
                preparation_profile=(
                    prepared.get("profile") or self.media_profile_version
                ),
            )
            return {
                "item": self._material_intake_api_item(item),
                "gates": self._gates(),
            }
        except TTPostError as exc:
            if intake is None:
                raise
            attempt_count = int(intake.get("attempt_count") or 1)
            retryable = (
                exc.code not in TERMINAL_PREPARATION_ERROR_CODES
                and exc.status >= 500
                and attempt_count < DEFAULT_PREPARATION_MAX_ATTEMPTS
            )
            failed = self.store.fail_material_intake(
                normalized_id,
                claim_token,
                error_code=exc.code,
                error_message=str(exc),
                retry_at=(
                    self._preparation_retry_at(attempt_count)
                    if retryable
                    else None
                ),
            )
            return {
                "item": self._material_intake_api_item(failed),
                "processing_error": {
                    "code": exc.code,
                    "retryable": retryable,
                },
                "gates": self._gates(),
            }
        except Exception as exc:
            if intake is None:
                raise
            attempt_count = int(intake.get("attempt_count") or 1)
            retryable = attempt_count < DEFAULT_PREPARATION_MAX_ATTEMPTS
            failed = self.store.fail_material_intake(
                normalized_id,
                claim_token,
                error_code="tt_preparation_unexpected",
                error_message=type(exc).__name__,
                retry_at=(
                    self._preparation_retry_at(attempt_count)
                    if retryable
                    else None
                ),
            )
            return {
                "item": self._material_intake_api_item(failed),
                "processing_error": {
                    "code": "tt_preparation_unexpected",
                    "retryable": retryable,
                },
                "gates": self._gates(),
            }

    def direct_preparation_claim(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "worker_id",
            "lease_seconds",
        }:
            raise TTPostServiceError(
                "invalid_request", "立即测试预制作领取字段无效", 400
            )
        worker_id = _bounded_text(
            payload.get("worker_id"), "后台制作worker ID", 128
        )
        lease_seconds = _positive_int(
            payload.get("lease_seconds"),
            "后台制作租约",
            MAX_PREPARATION_LEASE_SECONDS,
        )
        claims = self.store.claim_direct_test_prepare(
            worker_id,
            lease_seconds=lease_seconds,
            limit=1,
        )
        if not claims:
            return {"item": None, "gates": self._gates()}
        claim = claims[0]
        return {
            "item": self._direct_test_api_item(claim.item),
            "claim_token": claim.reveal_claim_token(),
            "gates": self._gates(),
        }

    def direct_preparation_renew(
        self,
        direct_test_id: Any,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "claim_token",
            "lease_seconds",
        }:
            raise TTPostServiceError(
                "invalid_request", "立即测试预制作续租字段无效", 400
            )
        item = self.store.renew_direct_test_prepare(
            _positive_int(direct_test_id, "立即测试任务ID"),
            _bounded_text(payload.get("claim_token"), "后台制作领取凭据", 512),
            lease_seconds=_positive_int(
                payload.get("lease_seconds"),
                "后台制作租约",
                MAX_PREPARATION_LEASE_SECONDS,
            ),
        )
        return {"item": self._direct_test_api_item(item), "gates": self._gates()}

    def direct_preparation_process(
        self,
        direct_test_id: Any,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {"claim_token"}:
            raise TTPostServiceError(
                "invalid_request", "立即测试预制作执行字段无效", 400
            )
        normalized_id = _positive_int(direct_test_id, "立即测试任务ID")
        token = _bounded_text(
            payload.get("claim_token"), "后台制作领取凭据", 512
        )
        claimed = None
        try:
            claimed = self.store.renew_direct_test_prepare(
                normalized_id,
                token,
                lease_seconds=DEFAULT_PREPARATION_LEASE_SECONDS,
            )
            account_id = str(claimed["account_id"])
            self.account_repository.get_public_account(account_id)
            saved_settings = self.store.get_account_settings(
                account_id,
                required=True,
            )
            settings = TTPostAccountSettings.from_mapping(
                {
                    key: saved_settings[key]
                    for key in (
                        "privacy_level",
                        "allow_comment",
                        "allow_duet",
                        "allow_stitch",
                        "brand_content_toggle",
                        "brand_organic_toggle",
                        "is_aigc",
                        "drama_language",
                    )
                }
            )
            creator = self.creator_info(
                {"source_account_id": account_id}
            )["item"]
            self._assert_creator_settings(creator, settings)
            prepared = self._prepare_resolved(
                {
                    "material_id": str(claimed["material_id"]),
                    "content_id": str(claimed["content_id"]),
                    "source_media_url": str(claimed["source_media_url"]),
                    "media_url": str(claimed["source_media_url"]),
                },
                gpu_job_id=str(claimed["gpu_job_id"]),
            )
            duration = float(prepared.get("duration_sec") or 0)
            maximum_duration = int(
                creator.get("max_video_post_duration_sec") or 0
            )
            if duration <= 0 or duration > maximum_duration:
                raise TTPostServiceError(
                    "tt_prepared_media_duration_invalid",
                    "最终成片时长不满足目标账号实时限制",
                    409,
                )
            item = self.store.complete_direct_test_prepare(
                normalized_id,
                token,
                gpu_job_id=prepared["gpu_job_id"],
                prepared_media_url=prepared["prepared_media_url"],
                prepared_output_sha256=prepared["output_sha256"],
                prepared_output_size=prepared["output_size"],
                prepared_duration_sec=prepared["duration_sec"],
                source_trim_tail_seconds=prepared[
                    "source_trim_tail_seconds"
                ],
                preparation_profile=(
                    prepared.get("profile") or self.media_profile_version
                ),
            )
            kicked = self._kick_runner()
            return {
                "item": self._direct_test_api_item(item),
                "publish_wakeup_requested": kicked,
                "gates": self._gates(),
            }
        except TTPostError as exc:
            if claimed is None:
                raise
            failed = self.store.fail_direct_test_prepare(
                normalized_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
            )
            return {
                "item": self._direct_test_api_item(failed),
                "processing_error": {
                    "code": exc.code,
                    "retryable": False,
                },
                "gates": self._gates(),
            }
        except Exception as exc:
            if claimed is None:
                raise
            failed = self.store.fail_direct_test_prepare(
                normalized_id,
                token,
                error_code="tt_post_direct_prepare_unexpected",
                error_message="立即测试预制作发生未预期错误（%s）"
                % type(exc).__name__,
            )
            return {
                "item": self._direct_test_api_item(failed),
                "processing_error": {
                    "code": "tt_post_direct_prepare_unexpected",
                    "retryable": False,
                },
                "gates": self._gates(),
            }

    def schedule_get(
        self,
        query: Mapping[str, Sequence[str]],
    ) -> Dict[str, Any]:
        account_id = _positive_decimal(
            self._query_first(query, "source_account_id"),
            "TikTok账号ID",
        )
        item = self._schedule_api_item(
            self.store.get_daily_schedule(account_id)
        )
        return {"item": item, "gates": self._gates()}

    def schedule_save(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError("invalid_request", "请求体必须是对象", 400)
        allowed = {
            "source_account_id",
            "enabled",
            "publish_time",
            "publish_times",
            "timezone",
            "expected_version",
            "consent",
        }
        if set(payload).difference(allowed):
            raise TTPostServiceError(
                "invalid_request",
                "每日发布设置包含未知字段",
                400,
            )
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        enabled = _exact_bool(payload.get("enabled"), "每日发布启用状态")
        expected_version = payload.get("expected_version")
        if (
            type(expected_version) is not int
            or expected_version < 0
            or expected_version > 2**31 - 1
        ):
            raise TTPostServiceError(
                "tt_post_schedule_version_required",
                "每日发布设置版本必须是非负整数",
                400,
            )
        if not enabled:
            saved = self.store.disable_daily_schedule(
                account_id,
                expected_version=expected_version,
            )
            return {
                "item": self._schedule_api_item(saved),
                "gates": self._gates(),
            }
        if str(payload.get("timezone") or "") != "Asia/Shanghai":
            raise TTPostServiceError(
                "tt_timezone_invalid",
                "每日发布只接受Asia/Shanghai",
                400,
            )
        if "publish_times" in payload:
            publish_times = payload.get("publish_times")
        else:
            publish_times = [payload.get("publish_time")]
        if not isinstance(publish_times, list):
            raise TTPostServiceError(
                "invalid_publish_times",
                "每日发布时间必须是列表",
                400,
            )
        consent = self._consent_from_payload(payload)
        self.account_repository.get_public_account(account_id)
        if self.manual_canary.allows_manual_account(
            "manual",
            account_id,
            now=_now_utc(self._now_fn),
        ):
            raise TTPostServiceError(
                "tt_post_manual_canary_schedule_locked",
                "一次性私密测试有效期间不能启用每日自动发布",
                409,
            )
        saved_settings = self.store.get_account_settings(
            account_id,
            required=True,
        )
        creator = self.creator_info(
            {"source_account_id": account_id}
        )["item"]
        settings = TTPostAccountSettings.from_mapping(
            {
                key: saved_settings[key]
                for key in (
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                    "drama_language",
                )
            }
        )
        self._assert_creator_settings(creator, settings)
        saved = self.store.save_daily_schedule(
            account_id,
            publish_times,
            enabled=True,
            expected_version=expected_version,
            consent_version=consent["version"],
            consented_at=consent["accepted_at"],
        )
        return {
            "item": self._schedule_api_item(saved),
            "gates": self._gates(),
        }

    def _sync_recurring_queue_if_present(
        self,
        queue: Mapping[str, Any],
    ) -> None:
        try:
            self.store.sync_recurring_from_queue(queue.get("id"))
        except TTPostError as exc:
            if exc.code != "tt_post_schedule_run_not_found":
                raise

    def _execute_recurring_run(
        self,
        *,
        run_key: str,
        trigger_type: str,
        account_id: str,
        shanghai_date: str,
        publish_time: str,
        scheduled_at_utc: str,
        config_version: int,
        manual_request_key: str = "",
    ) -> Dict[str, Any]:
        claimed: Optional[Dict[str, Any]] = None
        execution_token = ""
        manual_canary_account = self.manual_canary.allows_manual_account(
            trigger_type,
            account_id,
            now=_now_utc(self._now_fn),
        ) and str(run_key or "").startswith(
            "tt-post:manual-canary:v1:%s:%s:"
            % (
                self.manual_canary.canary_id,
                self.manual_canary.account_id,
            )
        )
        excluded_canary_pool_id = (
            self.manual_canary.pool_id
            if trigger_type == "auto"
            and self.manual_canary.is_active(_now_utc(self._now_fn))
            else None
        )

        def terminal_or_bound(
            run: Mapping[str, Any],
        ) -> Optional[Dict[str, Any]]:
            if run.get("queue_id"):
                queue = self.store.get_queue(run["queue_id"])
                self._sync_recurring_queue_if_present(queue)
                return self._recurring_run_api_item(
                    self.store.get_recurring_run(run["id"])
                )
            if run.get("status") != "claimed":
                return self._recurring_run_api_item(run)
            return None

        def acquire_and_recover(
            run: Mapping[str, Any],
        ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
            nonlocal execution_token
            execution = self.store.acquire_recurring_execution(
                run["id"],
                lease_seconds=DEFAULT_RECURRING_EXECUTION_LEASE_SECONDS,
            )
            execution_token = execution.reveal_execution_token()
            owned_run = dict(execution.run)
            try:
                recovered_queue = self.store.get_queue_by_idempotency_key(
                    str(owned_run["run_key"])
                )
            except TTPostError as exc:
                if exc.code != "tt_post_queue_not_found":
                    raise
                return owned_run, None
            bound = self.store.bind_recurring_queue(
                owned_run["id"],
                recovered_queue["id"],
                execution_token=execution_token,
            )
            execution_token = ""
            return owned_run, self._recurring_run_api_item(bound)

        try:
            try:
                existing = self.store.get_recurring_run_by_key(run_key)
            except TTPostError as exc:
                if exc.code != "tt_post_schedule_run_not_found":
                    raise
                existing = None
            if existing is not None:
                # A frozen automatic run keeps its original config version
                # even if an operator edits the schedule during recovery.
                existing_version = (
                    int(existing.get("config_version") or 0)
                    if trigger_type == "auto"
                    else config_version
                )
                claimed = self.store.claim_recurring_run(
                    run_key,
                    trigger_type,
                    account_id,
                    shanghai_date,
                    publish_time,
                    scheduled_at_utc,
                    config_version=existing_version,
                    manual_request_key=manual_request_key,
                    excluded_pool_item_id=excluded_canary_pool_id,
                    required_preparation_profile=self.media_profile_version,
                )
                resumed = terminal_or_bound(claimed)
                if resumed is not None:
                    return resumed
                try:
                    self.store.get_queue_by_idempotency_key(
                        str(claimed["run_key"])
                    )
                except TTPostError as exc:
                    if exc.code != "tt_post_queue_not_found":
                        raise
                else:
                    claimed, resumed = acquire_and_recover(claimed)
                    if resumed is not None:
                        return resumed

            if not self.gates.is_open and not manual_canary_account:
                raise TTPostServiceError(
                    "tt_post_live_gates_closed",
                    "发布门禁尚未全部开放，本次未消费素材",
                    409,
                )
            if (
                manual_canary_account
                and self.store.get_daily_schedule(account_id).get(
                    "enabled"
                )
            ):
                raise TTPostServiceError(
                    "tt_post_manual_canary_schedule_locked",
                    "一次性私密测试要求每日自动排期保持关闭",
                    409,
                )
            account = self.account_repository.get_public_account(account_id)
            safe_account = SafeAccount.from_mapping(
                self.account_repository._safe_account_mapping(account)
            )
            saved_settings = self.store.get_account_settings(
                account_id,
                required=True,
            )
            settings = TTPostAccountSettings.from_mapping(
                {
                    key: saved_settings[key]
                    for key in (
                        "privacy_level",
                        "allow_comment",
                        "allow_duet",
                        "allow_stitch",
                        "brand_content_toggle",
                        "brand_organic_toggle",
                        "is_aigc",
                        "drama_language",
                    )
                }
            )
            creator = self.creator_info(
                {"source_account_id": account_id}
            )["item"]
            if not manual_canary_account:
                self._assert_creator_settings(creator, settings)

            if claimed is None:
                claimed = self.store.claim_recurring_run(
                    run_key,
                    trigger_type,
                    account_id,
                    shanghai_date,
                    publish_time,
                    scheduled_at_utc,
                    config_version=config_version,
                    manual_request_key=manual_request_key,
                    excluded_pool_item_id=excluded_canary_pool_id,
                    required_preparation_profile=self.media_profile_version,
                )
                resumed = terminal_or_bound(claimed)
                if resumed is not None:
                    return resumed
            if not execution_token:
                claimed, resumed = acquire_and_recover(claimed)
                if resumed is not None:
                    return resumed
            pool_item = claimed["pool_item"]
            manual_canary_run = bool(
                manual_canary_account
                and self.manual_canary.matches_pool(
                    account_id,
                    pool_item,
                    now=_now_utc(self._now_fn),
                )
            )
            if manual_canary_account and not manual_canary_run:
                raise TTPostServiceError(
                    "tt_post_manual_canary_target_mismatch",
                    "下一条素材与一次性私密测试目标不一致",
                    409,
                )
            if not self.gates.is_open and not manual_canary_run:
                raise TTPostServiceError(
                    "tt_post_live_gates_closed",
                    "发布门禁尚未全部开放，本次未消费素材",
                    409,
                )
            duration = float(pool_item.get("prepared_duration_sec") or 0)
            maximum_duration = int(
                creator.get("max_video_post_duration_sec") or 0
            )
            if duration <= 0 or duration > maximum_duration:
                raise TTPostServiceError(
                    "tt_prepared_media_duration_invalid",
                    "最终成片时长不满足目标账号实时限制",
                    409,
                )
            consent = {
                "accepted": True,
                "version": str(pool_item.get("consent_version") or ""),
                "accepted_at": str(
                    pool_item.get("consented_at_utc") or ""
                ),
            }
            policy = (
                self._manual_canary_policy(pool_item)
                if manual_canary_run
                else self._automatic_policy_from_account_settings(
                    settings,
                    consent,
                )
            )
            self._assert_creator_policy(creator, policy)
            claimed = self.store.renew_recurring_execution(
                claimed["id"],
                execution_token,
                lease_seconds=DEFAULT_RECURRING_EXECUTION_LEASE_SECONDS,
            )
            creator_hash = _creator_info_hash(creator)
            creator_synced_at = _now_utc(self._now_fn).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
            legacy_pool = self._ensure_pool_item(
                str(pool_item["material_id"]),
                recurring_pool_id=pool_item["id"],
            )
            queue = self._freeze_queue_and_invalidate_code(
                legacy_pool["id"],
                safe_account,
                str(claimed["scheduled_at_utc"]),
                str(pool_item["caption_template"]),
                policy,
                lambda _material_id: {
                    "material_id": str(pool_item["material_id"]),
                    "content_id": str(pool_item["content_id"]),
                    "media_url": str(pool_item["prepared_media_url"]),
                },
                idempotency_key=run_key,
                is_aigc=bool(pool_item.get("is_aigc")),
                publish_mode="direct_post",
                account_display_name=str(
                    account.get("display_name")
                    or account.get("account_name")
                    or ""
                ),
                creator_nickname=creator["creator_nickname"],
                creator_username=creator["creator_username"],
                creator_info_hash=creator_hash,
                creator_info_synced_at=creator_synced_at,
                gpu_job_id=str(pool_item["gpu_job_id"]),
                source_media_url=str(pool_item["source_media_url"]),
                prepared_output_sha256=str(
                    pool_item["prepared_output_sha256"]
                ),
                prepared_output_size=int(
                    pool_item["prepared_output_size"]
                ),
                prepared_duration_sec=duration,
                source_trim_tail_seconds=float(
                    pool_item["source_trim_tail_seconds"]
                ),
                recurring_run_id=claimed["id"],
                recurring_execution_token=execution_token,
                material_name=str(pool_item.get("material_name") or ""),
                drama_name=str(pool_item.get("drama_name") or ""),
                material_language=str(
                    pool_item.get("material_language") or ""
                ),
                material_tag=str(pool_item.get("material_tag") or ""),
                description=str(pool_item.get("description") or ""),
            )
            bound = self.store.bind_recurring_queue(
                claimed["id"],
                queue["id"],
                execution_token=execution_token,
            )
            execution_token = ""
            return self._recurring_run_api_item(bound)
        except TTPostError as exc:
            if (
                claimed is not None
                and claimed.get("status") == "claimed"
                and not execution_token
            ):
                try:
                    claimed, resumed = acquire_and_recover(claimed)
                    if resumed is not None:
                        return resumed
                except TTPostError:
                    # Another live owner must keep its fencing lease. The
                    # retry path will revisit the durable reservation.
                    pass
            if (
                claimed is not None
                and claimed.get("status") == "claimed"
                and execution_token
            ):
                try:
                    self.store.release_recurring_preflight(
                        claimed["id"],
                        error_code=exc.code,
                        error_message=str(exc),
                        execution_token=execution_token,
                    )
                except TTPostError:
                    try:
                        self.store.yield_recurring_execution(
                            claimed["id"],
                            execution_token,
                        )
                    except TTPostError:
                        pass
            raise

    def schedules_due(
        self,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = {} if payload is None else payload
        if not isinstance(body, Mapping) or not set(body).issubset({"limit"}):
            raise TTPostServiceError(
                "invalid_request",
                "每日发布领取请求不接受业务字段",
                400,
            )
        limit = _positive_int(
            body.get("limit", 100),
            "每日任务上限",
            100,
        )
        now_utc = _now_utc(self._now_fn)
        now_shanghai = now_utc.astimezone(BEIJING_TZ)
        items = []
        processed_run_keys = set()

        def skipped_item(
            *,
            account_id: str,
            publish_time: str,
            exc: TTPostError,
            run_key: str = "",
        ) -> Dict[str, Any]:
            result = {
                "source_account_id": account_id,
                "publish_time": publish_time,
                "status": "skipped",
                "error_code": exc.code,
                "error_message": redact_text(str(exc)),
            }
            if run_key:
                result["run_key"] = run_key
            return result

        # Snapshot and durably reserve every currently due slot before any
        # recovery path can enter creator-info. Merely listing old recovery
        # rows is local; `_execute_recurring_run` is the first live boundary.
        today = now_shanghai.date()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        self.store.ensure_random_daily_plans(
            [today.isoformat(), tomorrow.isoformat()]
        )
        random_plans = {
            (
                str(item.get("account_id") or ""),
                str(item.get("shanghai_date") or ""),
            ): item
            for item in self.store.list_random_daily_plans(
                shanghai_dates=[
                    yesterday.isoformat(),
                    today.isoformat(),
                    tomorrow.isoformat(),
                ]
            )
        }
        due_slots = []
        for schedule in self.store.list_daily_schedules():
            if not schedule.get("enabled"):
                continue
            account_id = str(schedule.get("account_id") or "")
            for day_offset in (0, -1):
                local_day = (
                    now_shanghai + timedelta(days=day_offset)
                ).date()
                shanghai_date = local_day.isoformat()
                if str(schedule.get("schedule_mode") or "fixed") == "random":
                    plan = random_plans.get((account_id, shanghai_date))
                    publish_times = (
                        list(plan.get("publish_times") or [])
                        if isinstance(plan, Mapping)
                        else []
                    )
                    config_version = (
                        int(plan.get("config_version") or 0)
                        if isinstance(plan, Mapping)
                        else 0
                    )
                else:
                    publish_times = list(
                        schedule.get("publish_times") or []
                    )
                    config_version = int(
                        schedule.get("version") or 0
                    )
                for publish_time in sorted(
                    str(value)
                    for value in publish_times
                ):
                    try:
                        hour, minute = (
                            int(part)
                            for part in publish_time.split(":")
                        )
                        local_slot = datetime(
                            local_day.year,
                            local_day.month,
                            local_day.day,
                            hour,
                            minute,
                            tzinfo=BEIJING_TZ,
                        )
                    except (TypeError, ValueError):
                        continue
                    slot_utc = local_slot.astimezone(UTC)
                    age_seconds = (now_utc - slot_utc).total_seconds()
                    if 0 <= age_seconds <= DEFAULT_GRACE_SECONDS:
                        due_slots.append(
                            (
                                slot_utc,
                                account_id,
                                shanghai_date,
                                publish_time,
                                config_version,
                            )
                        )

        preclaimed_run_keys = set()
        preclaim_errors: Dict[str, TTPostError] = {}
        if self.gates.is_open:
            for (
                slot_utc,
                account_id,
                shanghai_date,
                publish_time,
                config_version,
            ) in sorted(due_slots):
                scheduled_at_utc = slot_utc.isoformat().replace(
                    "+00:00",
                    "Z",
                )
                run_key = "tt-post:auto:v1:%s:%s:%s" % (
                    account_id,
                    shanghai_date,
                    publish_time.replace(":", ""),
                )
                try:
                    self.store.get_recurring_run_by_key(run_key)
                except TTPostError as exc:
                    if exc.code != "tt_post_schedule_run_not_found":
                        raise
                else:
                    continue
                try:
                    self.store.claim_recurring_run(
                        run_key,
                        "auto",
                        account_id,
                        shanghai_date,
                        publish_time,
                        scheduled_at_utc,
                        config_version=config_version,
                        excluded_pool_item_id=(
                            self.manual_canary.pool_id
                            if self.manual_canary.is_active(now_utc)
                            else None
                        ),
                        required_preparation_profile=(
                            self.media_profile_version
                        ),
                    )
                    preclaimed_run_keys.add(run_key)
                except TTPostError as exc:
                    preclaim_errors[run_key] = exc

        # First recover a run that was durably reserved by an earlier process
        # but not yet bound to its legacy queue. This closes both crash gaps:
        # claim->freeze and freeze->bind.
        for pending in self.store.list_claimed_unbound_recurring_runs(
            limit=limit
        ):
            if len(items) >= limit:
                break
            run_key = str(pending.get("run_key") or "")
            processed_run_keys.add(run_key)
            account_id = str(pending.get("account_id") or "")
            publish_time = str(pending.get("publish_time") or "")
            try:
                scheduled = datetime.fromisoformat(
                    str(pending.get("scheduled_at_utc") or "").replace(
                        "Z",
                        "+00:00",
                    )
                ).astimezone(UTC)
                if (
                    (now_utc - scheduled).total_seconds()
                    > DEFAULT_GRACE_SECONDS
                ):
                    execution = self.store.acquire_recurring_execution(
                        pending["id"],
                        lease_seconds=(
                            DEFAULT_RECURRING_EXECUTION_LEASE_SECONDS
                        ),
                    )
                    execution_token = (
                        execution.reveal_execution_token()
                    )
                    try:
                        recovered_queue = (
                            self.store.get_queue_by_idempotency_key(
                                run_key
                            )
                        )
                    except TTPostError as exc:
                        if exc.code != "tt_post_queue_not_found":
                            raise
                        released = self.store.release_recurring_preflight(
                            pending["id"],
                            error_code="tt_post_recurring_run_expired",
                            error_message=(
                                "每日发布运行恢复时已超过600秒安全窗口"
                            ),
                            execution_token=execution_token,
                        )
                        items.append(
                            self._recurring_run_api_item(released)
                        )
                    else:
                        bound = self.store.bind_recurring_queue(
                            pending["id"],
                            recovered_queue["id"],
                            execution_token=execution_token,
                        )
                        items.append(
                            self._recurring_run_api_item(bound)
                        )
                    continue
                item = self._execute_recurring_run(
                    run_key=run_key,
                    trigger_type=str(pending.get("trigger_type") or ""),
                    account_id=account_id,
                    shanghai_date=str(
                        pending.get("shanghai_date") or ""
                    ),
                    publish_time=publish_time,
                    scheduled_at_utc=str(
                        pending.get("scheduled_at_utc") or ""
                    ),
                    config_version=int(
                        pending.get("config_version") or 0
                    ),
                    manual_request_key=str(
                        pending.get("manual_request_key") or ""
                    ),
                )
                items.append(item)
            except (TTPostError, ValueError, TypeError) as exc:
                if isinstance(exc, TTPostError):
                    items.append(
                        skipped_item(
                            account_id=account_id,
                            publish_time=publish_time,
                            exc=exc,
                            run_key=run_key,
                        )
                    )
                else:
                    items.append(
                        {
                            "source_account_id": account_id,
                            "publish_time": publish_time,
                            "status": "skipped",
                            "error_code": "tt_post_schedule_run_invalid",
                            "error_message": (
                                "每日发布运行时间字段无效"
                            ),
                            "run_key": run_key,
                        }
                    )

        for (
            slot_utc,
            account_id,
            shanghai_date,
            publish_time,
            config_version,
        ) in sorted(due_slots):
            if len(items) >= limit:
                break
            scheduled_at_utc = slot_utc.isoformat().replace(
                "+00:00",
                "Z",
            )
            run_key = "tt-post:auto:v1:%s:%s:%s" % (
                account_id,
                shanghai_date,
                publish_time.replace(":", ""),
            )
            if run_key in processed_run_keys:
                continue
            try:
                self.store.get_recurring_run_by_key(run_key)
            except TTPostError as exc:
                if exc.code != "tt_post_schedule_run_not_found":
                    raise
            else:
                if run_key not in preclaimed_run_keys:
                    continue
            if run_key in preclaim_errors:
                items.append(
                    skipped_item(
                        account_id=account_id,
                        publish_time=publish_time,
                        exc=preclaim_errors[run_key],
                        run_key=run_key,
                    )
                )
                continue
            try:
                item = self._execute_recurring_run(
                    run_key=run_key,
                    trigger_type="auto",
                    account_id=account_id,
                    shanghai_date=shanghai_date,
                    publish_time=publish_time,
                    scheduled_at_utc=scheduled_at_utc,
                    config_version=config_version,
                )
                items.append(item)
            except TTPostError as exc:
                items.append(
                    skipped_item(
                        account_id=account_id,
                        publish_time=publish_time,
                        exc=exc,
                        run_key=run_key,
                    )
                )
        backlog = self.store.recurring_recovery_backlog()
        deferred_count = int(backlog["deferred_count"])
        oldest_deferred_at_utc = str(
            backlog["oldest_deferred_at_utc"] or ""
        )
        for (
            slot_utc,
            account_id,
            shanghai_date,
            publish_time,
            _config_version,
        ) in due_slots:
            run_key = "tt-post:auto:v1:%s:%s:%s" % (
                account_id,
                shanghai_date,
                publish_time.replace(":", ""),
            )
            try:
                self.store.get_recurring_run_by_key(run_key)
            except TTPostError as exc:
                if exc.code != "tt_post_schedule_run_not_found":
                    raise
                deferred_count += 1
                candidate = slot_utc.isoformat().replace("+00:00", "Z")
                if (
                    not oldest_deferred_at_utc
                    or candidate < oldest_deferred_at_utc
                ):
                    oldest_deferred_at_utc = candidate
        return {
            "items": items,
            "current_shanghai_minute": "%s %s"
            % (
                now_shanghai.strftime("%Y-%m-%d"),
                now_shanghai.strftime("%H:%M"),
            ),
            "grace_seconds": DEFAULT_GRACE_SECONDS,
            "deferred_count": deferred_count,
            "oldest_deferred_at_utc": oldest_deferred_at_utc,
            "gates": self._gates(),
        }

    def _kick_runner(self) -> bool:
        if not self.runner_kick_path:
            return False
        try:
            Path(self.runner_kick_path).touch(exist_ok=True)
            return True
        except OSError:
            return False

    def _kick_preparation_runner(self) -> bool:
        if not self.preparation_kick_path:
            return False
        try:
            Path(self.preparation_kick_path).touch(exist_ok=True)
            return True
        except OSError:
            return False

    def run_now(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "source_account_id",
            "idempotency_key",
        }:
            raise TTPostServiceError(
                "invalid_request",
                "立即发布请求字段无效",
                400,
            )
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        request_key = _bounded_text(
            payload.get("idempotency_key"),
            "立即发布幂等键",
            255,
        )
        now_shanghai = _now_utc(self._now_fn).astimezone(BEIJING_TZ)
        publish_time = now_shanghai.strftime("%H:%M")
        shanghai_date = now_shanghai.strftime("%Y-%m-%d")
        scheduled_at_utc = now_shanghai.replace(
            second=0,
            microsecond=0,
        ).astimezone(UTC).isoformat().replace("+00:00", "Z")
        manual_canary_account = self.manual_canary.allows_manual_account(
            "manual",
            account_id,
            now=_now_utc(self._now_fn),
        )
        run_key = (
            "tt-post:manual-canary:v1:%s:%s:%s"
            % (
                self.manual_canary.canary_id,
                account_id,
                hashlib.sha256(
                    request_key.encode("utf-8")
                ).hexdigest()[:32],
            )
            if manual_canary_account
            else "tt-post:manual:v1:%s:%s"
            % (
                account_id,
                hashlib.sha256(
                    request_key.encode("utf-8")
                ).hexdigest()[:32],
            )
        )
        schedule = self.store.get_daily_schedule(account_id)
        item = self._execute_recurring_run(
            run_key=run_key,
            trigger_type="manual",
            account_id=account_id,
            shanghai_date=shanghai_date,
            publish_time=publish_time,
            scheduled_at_utc=scheduled_at_utc,
            config_version=int(schedule.get("version") or 0),
            manual_request_key=request_key,
        )
        kicked = bool(item.get("queue_id")) and self._kick_runner()
        return {
            "item": item,
            "runner_wakeup_requested": kicked,
            "runner_timer_fallback_seconds": 60,
            "gates": self._gates(),
        }

    def _ensure_pool_item(
        self,
        material_id: str,
        *,
        recurring_pool_id: Any = None,
    ) -> Dict[str, Any]:
        if recurring_pool_id not in (None, ""):
            return self.store.ensure_material_for_recurring(
                material_id,
                recurring_pool_id,
            )
        try:
            return self.store.add_material(material_id)
        except TTPostError as exc:
            if exc.code != "tt_post_material_already_exists":
                raise
        item = self.store.get_material_by_material_id(material_id)
        if item.get("status") != "available":
            raise TTPostServiceError(
                "tt_post_material_already_used",
                "素材已有TikTok排期或发布历史",
                409,
            )
        return item

    def _queue_api_item(
        self,
        queue: Mapping[str, Any],
        *,
        gates: LiveGates,
    ) -> Dict[str, Any]:
        item = dict(queue)
        item["queue_id"] = item.get("id")
        item["source_account_id"] = item.get("account_id")
        item["creator_username_snapshot"] = (
            item.get("creator_username_snapshot")
            or item.get("account_username")
        )
        item["account_name_snapshot"] = (
            item.get("creator_nickname_snapshot")
            or item.get("account_display_name")
            or item.get("account_username")
        )
        item["scheduled_at"] = item.get("scheduled_at_utc")
        item["timezone"] = "Asia/Shanghai"
        item["caption_text"] = item.get("caption")
        item["commercial_disclosure"] = bool(
            item.get("brand_content_toggle") or item.get("brand_organic_toggle")
        )
        if item.get("status") == "canceled":
            item["queue_status"] = "canceled"
            item["status"] = "cancelled"
        elif item.get("status") == "blocked_compliance":
            item["queue_status"] = "blocked_compliance"
            item["status"] = "hold"
        item["publish_mode"] = str(item.get("publish_mode") or "hold")
        manual_canary = self._is_manual_canary_queue(queue)
        item["manual_canary"] = manual_canary
        if (
            item["publish_mode"] != "direct_post"
            or (not gates.is_open and not manual_canary)
        ):
            item["publish_mode"] = "hold"
        return item

    def _publish_task_api_item(
        self,
        task: Mapping[str, Any],
    ) -> Dict[str, Any]:
        task_type = str(task.get("task_type") or "")
        source = task.get("item")
        if not isinstance(source, Mapping):
            raise TTPostServiceError(
                "tt_post_storage_conflict",
                "发布任务读取结果无效",
                500,
            )
        if task_type == "direct_test":
            item = self._direct_test_api_item(source)
            item["scheduled_at"] = str(
                task.get("task_at_utc") or item.get("created_at") or ""
            )
            item["scheduled_at_utc"] = item["scheduled_at"]
            item["timezone"] = "Asia/Shanghai"
            item["account_name_snapshot"] = (
                item.get("creator_nickname_snapshot")
                or item.get("account_display_name")
                or item.get("account_username")
            )
            item["publish_mode"] = "direct_post"
        elif task_type == "automatic":
            item = self._queue_api_item(source, gates=self.gates)
        else:
            raise TTPostServiceError(
                "tt_post_storage_conflict",
                "发布任务类型无效",
                500,
            )
        item["task_type"] = task_type
        item["task_id"] = int(task.get("task_id") or 0)
        item["task_key"] = str(task.get("task_key") or "")
        item["task_at_utc"] = str(task.get("task_at_utc") or "")
        item["raw_status"] = str(task.get("raw_status") or "")
        item["status_group"] = str(task.get("status_group") or "")
        item["task_label"] = (
            "立即测试" if task_type == "direct_test" else "自动/排期发布"
        )
        return item

    def _existing_queue(
        self,
        *,
        idempotency_key: str,
        account_id: str,
        material_id: str,
        content_id: str,
        scheduled_at_utc: str,
        caption_template: Optional[str],
        caption: str,
        consent: Mapping[str, Any],
        submitted_caption: Any = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            existing = self.store.get_queue_by_idempotency_key(idempotency_key)
        except TTPostError as exc:
            if exc.code == "tt_post_queue_not_found":
                return None
            raise
        expected: Dict[str, Any] = {
            "account_id": account_id,
            "material_id": material_id,
            "content_id": content_id,
            "scheduled_at_utc": scheduled_at_utc,
            "user_consent": bool(consent.get("accepted")),
            "consent_version": str(consent.get("version") or ""),
            "consented_at_utc": str(consent.get("accepted_at") or ""),
        }
        if caption_template is not None:
            expected["caption_template"] = caption_template
        if any(existing.get(key) != value for key, value in expected.items()):
            raise TTPostServiceError(
                "tt_post_idempotency_conflict",
                "幂等键已用于不同TikTok排期",
                409,
            )
        if submitted_caption not in (None, ""):
            normalized_submitted = _bounded_text(
                submitted_caption,
                "发布描述",
                2200,
            )
            acceptable_captions = [str(existing.get("caption") or "")]
            if caption_template is not None:
                # A formal queue freezes deferred macros such as ``{code}``
                # after allocating its immutable route. An exact HTTP retry
                # still carries the pre-freeze caption text, so accept either
                # that deterministic candidate or the frozen caption.
                acceptable_captions.append(str(caption or ""))
            if not any(
                secrets.compare_digest(
                    candidate.encode("utf-8"),
                    normalized_submitted.encode("utf-8"),
                )
                for candidate in acceptable_captions
            ):
                raise TTPostServiceError(
                    "tt_post_idempotency_conflict",
                    "幂等键已用于不同TikTok排期",
                    409,
                )
        legacy_deferred_caption = bool(
            caption_template is not None
            and secrets.compare_digest(
                str(existing.get("caption") or "").encode("utf-8"),
                str(caption or "").encode("utf-8"),
            )
            and (
                (
                    caption_uses_desc_macro(caption_template)
                    and not str(existing.get("description") or "")
                )
                or (
                    caption_uses_url_macro(caption_template)
                    and not str(existing.get("short_url") or "")
                )
            )
        )
        if legacy_deferred_caption:
            return existing
        expected_caption = caption
        if caption_template is not None:
            expected_caption = render_caption_template(
                caption_template,
                content_id,
                url=existing.get("short_url"),
                description=existing.get("description"),
                code=existing.get("code"),
            )
        if not secrets.compare_digest(
            str(existing.get("caption") or "").encode("utf-8"),
            str(expected_caption or "").encode("utf-8"),
        ):
            raise TTPostServiceError(
                "tt_post_idempotency_conflict",
                "幂等键已用于不同TikTok排期",
                409,
            )
        return existing

    def queue_create(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TTPostServiceError("invalid_request", "请求体必须是对象", 400)
        idempotency_key = _bounded_text(
            payload.get("idempotency_key"),
            "幂等键",
            255,
        )
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        material_id = _positive_decimal(payload.get("material_id"), "素材ID", 19)
        requested_content_id = _bounded_text(
            payload.get("content_id"),
            "Drama ID",
            128,
        )
        scheduled_at_utc = _utc_iso(payload.get("scheduled_at"))
        if str(payload.get("timezone") or "") != "Asia/Shanghai":
            raise TTPostServiceError(
                "tt_timezone_invalid",
                "TikTok排期只接受Asia/Shanghai",
                400,
            )
        submitted_caption = payload.get("caption_text")
        if "caption_template" in payload:
            candidate_template, candidate_caption = _caption_from_submission(
                payload,
                requested_content_id,
                defer_description=True,
            )
        elif submitted_caption not in (None, ""):
            candidate_template = None
            candidate_caption = _bounded_text(
                submitted_caption,
                "发布描述",
                2200,
            )
        else:
            candidate_template = FIXED_CAPTION_TEMPLATE
            candidate_caption = render_fixed_caption(requested_content_id)
        consent = self._consent_from_payload(payload)

        requested_mode = str(payload.get("publish_mode") or "").strip()
        if requested_mode not in {"hold", "direct_post"}:
            raise TTPostServiceError(
                "invalid_publish_mode",
                "TikTok发布模式无效",
                400,
            )
        frozen_mode = (
            "direct_post"
            if requested_mode == "direct_post" and self.gates.is_open
            else "hold"
        )
        existing = self._existing_queue(
            idempotency_key=idempotency_key,
            account_id=account_id,
            material_id=material_id,
            content_id=requested_content_id,
            scheduled_at_utc=scheduled_at_utc,
            caption_template=candidate_template,
            caption=candidate_caption,
            consent=consent,
            submitted_caption=submitted_caption,
        )
        if existing is not None:
            return {
                "item": self._queue_api_item(existing, gates=self.gates),
                "gates": self._gates(),
            }
        if (
            datetime.fromisoformat(scheduled_at_utc.replace("Z", "+00:00"))
            <= _now_utc(self._now_fn) + timedelta(seconds=60)
        ):
            raise TTPostServiceError(
                "tt_schedule_too_soon",
                "发布时间必须晚于当前时间",
                400,
            )
        account = self.account_repository.get_public_account(account_id)
        safe_account = SafeAccount.from_mapping(
            self.account_repository._safe_account_mapping(account)
        )
        saved_settings = self.store.get_account_settings(
            account_id,
            required=True,
        )
        settings = TTPostAccountSettings.from_mapping(
            {
                key: saved_settings[key]
                for key in (
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                    "drama_language",
                )
            }
        )
        policy = self._automatic_policy_from_account_settings(
            settings,
            consent,
        )
        is_aigc = settings.is_aigc

        creator_result = self.creator_info({"source_account_id": account_id})
        creator = creator_result["item"]
        self._assert_creator_policy(creator, policy)
        creator_hash = _creator_info_hash(creator)
        creator_synced_at = _now_utc(self._now_fn).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        prepared = self._resolve_and_prepare(material_id)
        gpu_job_id = prepared["gpu_job_id"]
        if prepared["content_id"] != requested_content_id:
            raise TTPostServiceError(
                "tt_content_id_mismatch",
                "页面Drama ID与素材真实映射不一致",
                409,
            )
        caption_template, caption = _caption_from_submission(
            payload,
            requested_content_id,
            description=prepared.get("description"),
        )
        maximum_duration = int(
            creator.get("max_video_post_duration_sec") or 0
        )
        duration = float(
            prepared.get("duration_sec")
            or prepared.get("final_duration_sec")
            or 0
        )
        if duration <= 0 or duration > maximum_duration:
            raise TTPostServiceError(
                "tt_prepared_media_duration_invalid",
                "最终成片时长不满足目标账号实时限制",
                409,
            )
        pool = self._ensure_pool_item(material_id)
        queue = self._freeze_queue_and_invalidate_code(
            pool["id"],
            safe_account,
            scheduled_at_utc,
            caption_template,
            policy,
            lambda _material_id: {
                "material_id": prepared["material_id"],
                "content_id": prepared["content_id"],
                "media_url": prepared["prepared_media_url"],
            },
            idempotency_key=idempotency_key,
            is_aigc=is_aigc,
            publish_mode=frozen_mode,
            account_display_name=account["display_name"],
            creator_nickname=creator["creator_nickname"],
            creator_username=creator["creator_username"],
            creator_info_hash=creator_hash,
            creator_info_synced_at=creator_synced_at,
            gpu_job_id=gpu_job_id,
            source_media_url=prepared["source_media_url"],
            prepared_output_sha256=prepared["output_sha256"],
            prepared_output_size=prepared["output_size"],
            prepared_duration_sec=prepared["duration_sec"],
            source_trim_tail_seconds=prepared[
                "source_trim_tail_seconds"
            ],
            material_name=str(prepared.get("material_name") or ""),
            drama_name=str(prepared.get("drama_name") or ""),
            material_language=str(
                prepared.get("material_language") or ""
            ),
            material_tag=str(prepared.get("material_tag") or ""),
            description=str(prepared.get("description") or ""),
        )
        return {
            "item": self._queue_api_item(queue, gates=self.gates),
            "gates": self._gates(),
        }

    def queue_list(self, query: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
        def first(name: str, default: str = "") -> str:
            values = query.get(name, ())
            return str(values[0] if values else default).strip()

        try:
            page = int(first("page", "1"))
            page_size = int(first("page_size", "20"))
        except ValueError:
            raise TTPostServiceError("invalid_request", "分页参数无效", 400) from None
        if page < 1 or page_size < 1 or page_size > 100:
            raise TTPostServiceError("invalid_request", "分页参数无效", 400)
        material_filter = first("material_id")
        account_filter = first("source_account_id")
        status_filter = first("status")
        rows = self.store.list_queues()
        if material_filter:
            normalized = _positive_decimal(material_filter, "素材ID", 19)
            rows = [row for row in rows if row.get("material_id") == normalized]
        if account_filter:
            normalized = _positive_decimal(account_filter, "TikTok账号ID")
            rows = [row for row in rows if row.get("account_id") == normalized]
        if status_filter:
            aliases = {
                "cancelled": "canceled",
                "hold": "blocked_compliance",
                "needs_review": "unknown",
            }
            normalized_status = aliases.get(status_filter, status_filter)
            rows = [row for row in rows if row.get("status") == normalized_status]
        total = len(rows)
        start = (page - 1) * page_size
        items = [
            self._queue_api_item(row, gates=self.gates)
            for row in rows[start : start + page_size]
        ]
        processing = {
            "claimed",
            "publishing",
            "reconciling",
        }
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
            },
            "summary": {
                "total": total,
                "scheduled": sum(row["status"] == "scheduled" for row in rows),
                "processing": sum(row["status"] in processing for row in rows),
                "needs_review": sum(
                    bool(row.get("unknown_outcome")) for row in rows
                ),
                "published": sum(row["status"] == "published" for row in rows),
            },
            "gates": self._gates(),
        }

    def publish_tasks_list(
        self,
        query: Mapping[str, Sequence[str]],
    ) -> Dict[str, Any]:
        """List automatic and direct-test tasks in one read-only view."""

        def first(name: str, default: str = "") -> str:
            values = query.get(name, ())
            return str(values[0] if values else default).strip()

        try:
            page = int(first("page", "1"))
            page_size = int(first("page_size", "20"))
        except ValueError:
            raise TTPostServiceError(
                "invalid_request",
                "分页参数无效",
                400,
            ) from None
        if page < 1 or page_size < 1 or page_size > 100:
            raise TTPostServiceError("invalid_request", "分页参数无效", 400)
        result = self.store.list_publish_tasks(
            material_id=first("material_id") or None,
            account_id=first("source_account_id") or None,
            status=first("status") or None,
            task_type=first("task_type", "all"),
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        total = int(result.get("total") or 0)
        return {
            "items": [
                self._publish_task_api_item(task)
                for task in result.get("items", [])
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
            },
            "summary": dict(result.get("summary") or {}),
            "gates": self._gates(),
        }

    def queue_cancel(
        self,
        queue_id: Any,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = {} if payload is None else payload
        reason = str(body.get("reason") or "由AI后台操作人员取消")[:500]
        queue = self.store.cancel_queue(queue_id, reason=reason)
        self._sync_recurring_queue_if_present(queue)
        return {
            "item": self._queue_api_item(queue, gates=self.gates),
            "gates": self._gates(),
        }

    def events(self, queue_id: Any) -> Dict[str, Any]:
        return {
            "items": self.store.list_events(queue_id=queue_id),
            "gates": self._gates(),
        }

    def claim_due(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        worker_id = _bounded_text(payload.get("worker_id"), "runner ID", 128)
        grace_seconds = int(payload.get("grace_seconds", DEFAULT_GRACE_SECONDS))
        if grace_seconds != DEFAULT_GRACE_SECONDS:
            raise TTPostServiceError(
                "tt_runner_grace_invalid",
                "TT发布宽限窗口必须固定为600秒",
                400,
            )
        limit = _positive_int(payload.get("limit", 20), "领取数量", 100)
        claims = self.store.claim_due(
            worker_id,
            lease_seconds=DEFAULT_LEASE_SECONDS,
            grace_seconds=DEFAULT_GRACE_SECONDS,
            limit=limit,
        )
        for stored_queue in self.store.list_queues():
            if str(stored_queue.get("status") or "") in {
                "published",
                "failed",
                "canceled",
                "missed",
                "blocked_compliance",
                "unknown",
            }:
                self._sync_recurring_queue_if_present(stored_queue)
        items = []
        for claim in claims:
            queue = claim.queue
            manual_canary = self._is_manual_canary_queue(queue)
            if (
                queue.get("publish_mode") != "direct_post"
                or (not self.gates.is_open and not manual_canary)
            ):
                blocked = self.store.block_compliance(
                    claim.queue_id,
                    claim.reveal_claim_token(),
                )
                self._sync_recurring_queue_if_present(blocked)
                items.append(
                    {
                        "queue": self._queue_api_item(
                            blocked,
                            gates=self.gates,
                        )
                    }
                )
                continue
            items.append(
                {
                    "queue": self._queue_api_item(queue, gates=self.gates),
                    "claim_token": claim.reveal_claim_token(),
                }
            )
        return {"items": items, "gates": self._gates()}

    def _creator_recheck(
        self,
        account_id: str,
        gpu_job_id: str,
        queue_id: int,
        claim_token: str,
    ) -> Dict[str, Any]:
        with self.account_source.publish_credentials(account_id) as credentials:
            self.store.renew_claim(
                queue_id,
                claim_token,
                lease_seconds=self._claim_lease_seconds(),
            )
            raw = self.gpu_client.creator_info(
                job_id=gpu_job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        return _normalized_creator_info(raw.get("creator_info", raw))

    def _prepare_queue_short_link(
        self,
        queue: Mapping[str, Any],
        claim_token: str,
    ) -> Dict[str, Any]:
        """Persist and materialize the immutable redirect before Direct Post."""

        current = dict(queue)
        short_url = str(current.get("short_url") or "")
        if not short_url:
            if int(current.get("short_link_id") or 0) or str(
                current.get("long_url") or ""
            ):
                raise TTPostServiceError(
                    "tt_short_link_state_invalid",
                    "TikTok短链冻结状态不完整",
                    409,
                )
            return current
        try:
            normalized_short_url = validate_short_url(short_url)
            expected_short_url = build_short_url(
                current.get("short_link_id")
            )
            if (
                not secrets.compare_digest(
                    normalized_short_url,
                    expected_short_url,
                )
                or normalized_short_url
                not in str(current.get("caption") or "")
            ):
                raise TTPostLinkError(
                    "tt_short_link_state_invalid",
                    "TikTok短链与冻结描述不一致",
                    409,
                )
            if not str(current.get("long_url") or ""):
                if str(current.get("code") or ""):
                    route = self.store.get_code_route_for_queue(current["id"])
                    long_url = validate_w2a_url(route.get("long_url"))
                else:
                    # Queues frozen before the code-route migration have no
                    # route row. Preserve their immutable caption and legacy
                    # AIpost link behavior instead of failing a pending post.
                    long_url = build_w2a_url(
                        {
                            "username": (
                                current.get("creator_username_snapshot")
                                or current.get("account_username")
                            ),
                            "timestamp": int(_now_utc(self._now_fn).timestamp()),
                            "material_language": current.get("material_language"),
                            "drama_name": current.get("drama_name"),
                            "tag": current.get("material_tag"),
                            "link_id": current.get("short_link_id"),
                            "page_name": (
                                current.get("creator_nickname_snapshot")
                                or current.get("account_display_name")
                                or current.get("account_username")
                            ),
                            "page_id": current.get("account_id"),
                            "material_name": current.get("material_name"),
                            "material_id": current.get("material_id"),
                            "queue_id": current.get("id"),
                            "content_id": current.get("content_id"),
                            "channel": "AIpost",
                        }
                    )
                current = self.store.prepare_short_link(
                    current["id"],
                    claim_token,
                    long_url,
                )
            write_short_redirect(
                self.short_link_root,
                current["short_link_id"],
                current["long_url"],
            )
        except TTPostLinkError as exc:
            raise TTPostServiceError(
                exc.code,
                str(exc),
                exc.status,
            ) from None
        return dict(current)

    def _record_remote_publish_id_or_unknown(
        self,
        queue_id: int,
        claim_token: str,
        publish_id: str,
    ) -> Dict[str, Any]:
        """Persist an initialized remote ID or fail closed to reconciliation."""

        try:
            return self.store.record_publish_id(
                queue_id,
                claim_token,
                publish_id,
            )
        except Exception as exc:
            # Once the GPU reports a publish ID, Direct Post may already exist.
            # Any CPU-side validation or storage failure must prevent another
            # init, including unexpected database/runtime failures.
            error_code = str(
                getattr(
                    exc,
                    "code",
                    "tt_post_publish_id_persistence_failed",
                )
                or "tt_post_publish_id_persistence_failed"
            )[:128]
            return self.store.mark_unknown(
                queue_id,
                claim_token,
                reason=(
                    "TT GPU publish_id could not be persisted; "
                    "manual reconcile is required (%s)" % error_code
                ),
            )

    def direct_publish_claim(
        self,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "worker_id",
            "lease_seconds",
            "limit",
        }:
            raise TTPostServiceError(
                "invalid_request", "立即测试发布领取字段无效", 400
            )
        worker_id = _bounded_text(
            payload.get("worker_id"), "发布执行器ID", 128
        )
        lease_seconds = _positive_int(
            payload.get("lease_seconds"), "发布认领时长", 3600
        )
        limit = _positive_int(payload.get("limit"), "发布认领数量", 10)
        self.store.quarantine_expired_direct_test_publishes()
        items = []
        for row in self.store.list_direct_tests(status="ready", limit=limit):
            try:
                claim = self.store.claim_direct_test_publish(
                    row["id"],
                    worker_id,
                    lease_seconds=lease_seconds,
                )
            except TTPostError as exc:
                if exc.code in {
                    "tt_post_direct_test_not_ready",
                    "tt_post_direct_test_claim_conflict",
                    "tt_post_account_publish_busy",
                }:
                    continue
                raise
            items.append(
                {
                    "item": self._direct_test_api_item(claim.item),
                    "claim_token": claim.reveal_claim_token(),
                }
            )
        return {"items": items, "gates": self._gates()}

    def _direct_test_prepare_short_link(
        self,
        item: Mapping[str, Any],
        claim_token: str,
    ) -> Dict[str, Any]:
        current = dict(item)
        short_url = str(current.get("short_url") or "")
        if not short_url:
            return current
        try:
            if not secrets.compare_digest(
                validate_short_url(short_url),
                build_short_url(current.get("short_link_id")),
            ):
                raise TTPostLinkError(
                    "tt_short_link_state_invalid",
                    "立即测试短链身份不一致",
                    409,
                )
            if not str(current.get("long_url") or ""):
                long_url = build_w2a_url(
                    {
                        "username": (
                            current.get("creator_username_snapshot")
                            or current.get("account_username")
                        ),
                        "timestamp": int(_now_utc(self._now_fn).timestamp()),
                        "material_language": current.get("material_language"),
                        "drama_name": current.get("drama_name"),
                        "tag": current.get("material_tag"),
                        "link_id": current.get("short_link_id"),
                        "page_name": (
                            current.get("creator_nickname_snapshot")
                            or current.get("account_display_name")
                            or current.get("account_username")
                        ),
                        "page_id": current.get("account_id"),
                        "material_name": current.get("material_name"),
                        "material_id": current.get("material_id"),
                        "queue_id": current.get("id"),
                        "content_id": current.get("content_id"),
                        "channel": "AIpost",
                    }
                )
                current = self.store.prepare_direct_test_short_link(
                    current["id"],
                    claim_token,
                    long_url,
                )
            write_short_redirect(
                self.short_link_root,
                current["short_link_id"],
                current["long_url"],
            )
        except TTPostLinkError as exc:
            raise TTPostServiceError(exc.code, str(exc), exc.status) from None
        return dict(current)

    def direct_publish_claimed(
        self,
        direct_test_id: Any,
        claim_token: Any,
    ) -> Dict[str, Any]:
        normalized_id = _positive_int(direct_test_id, "立即测试任务ID")
        token = _bounded_text(claim_token, "发布领取凭据", 512)
        item = self.store.get_direct_test(normalized_id)
        if str(item.get("status") or "") == "unknown":
            raise TTPostServiceError(
                "tt_post_unknown_no_retry",
                "未知结果的立即测试禁止重新发布，只能核对",
                409,
            )
        if str(item.get("status") or "") != "publishing":
            raise TTPostServiceError(
                "tt_post_direct_test_not_claimed",
                "立即测试任务没有有效发布认领",
                409,
            )
        if not self.gates.is_open:
            final = self.store.fail_direct_test_publish(
                normalized_id,
                token,
                error_code="tt_post_live_gates_closed",
                error_message="发布门禁尚未全部开放",
                publish_was_not_created=True,
            )
            return {"item": self._direct_test_api_item(final), "gates": self._gates()}
        account_id = str(item["account_id"])
        gpu_job_id = _required_gpu_job_id(item.get("gpu_job_id"))
        settings = TTPostAccountSettings.from_mapping(
            {
                key: item[key]
                for key in (
                    "privacy_level",
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                )
            }
        )
        try:
            with self.account_source.publish_credentials(account_id) as credentials:
                self.store.renew_direct_test_publish(
                    normalized_id,
                    token,
                    lease_seconds=self._claim_lease_seconds(),
                )
                creator_raw = self.gpu_client.creator_info(
                    job_id=gpu_job_id,
                    source_account_id=account_id,
                    access_token=credentials.reveal_access_token(),
                )
            creator = _normalized_creator_info(
                creator_raw.get("creator_info", creator_raw)
            )
            self._assert_creator_settings(creator, settings)
            if not secrets.compare_digest(
                _creator_info_hash(creator),
                str(item.get("creator_info_hash") or ""),
            ):
                raise TTPostServiceError(
                    "tt_creator_info_changed",
                    "TikTok账号实时能力与测试创建时的快照不一致",
                    409,
                )
            duration = float(item.get("prepared_duration_sec") or 0)
            if duration <= 0 or duration > int(
                creator.get("max_video_post_duration_sec") or 0
            ):
                raise TTPostServiceError(
                    "tt_prepared_media_duration_invalid",
                    "最终成片时长不满足目标账号实时限制",
                    409,
                )
            self.store.renew_direct_test_publish(
                normalized_id,
                token,
                lease_seconds=self._claim_lease_seconds(),
            )
            item = self._direct_test_prepare_short_link(item, token)
        except TTPostError as exc:
            failed = self.store.fail_direct_test_publish(
                normalized_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
                publish_was_not_created=True,
            )
            return {"item": self._direct_test_api_item(failed), "gates": self._gates()}

        try:
            with self.account_source.publish_credentials(account_id) as credentials:
                self.store.renew_direct_test_publish(
                    normalized_id,
                    token,
                    lease_seconds=self._claim_lease_seconds(),
                )
                result = self.gpu_client.publish(
                    job_id=gpu_job_id,
                    source_account_id=account_id,
                    access_token=credentials.reveal_access_token(),
                    queue=item,
                )
        except GPUClientError as exc:
            recovered_publish_id = str(exc.details.get("publish_id") or "")
            if exc.code == "tt_publish_reconcile_required" and recovered_publish_id:
                final = self.store.record_direct_test_publish_id(
                    normalized_id,
                    token,
                    recovered_publish_id,
                )
            else:
                final = self.store.fail_direct_test_publish(
                    normalized_id,
                    token,
                    error_code=exc.code,
                    error_message=str(exc),
                    publish_was_not_created=(
                        bool(exc.publish_was_not_created)
                        and not bool(exc.unknown_outcome)
                    ),
                )
            return {"item": self._direct_test_api_item(final), "gates": self._gates()}
        except TTPostError as exc:
            failed = self.store.fail_direct_test_publish(
                normalized_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
                publish_was_not_created=True,
            )
            return {"item": self._direct_test_api_item(failed), "gates": self._gates()}
        publish_id = str(result.get("publish_id") or "").strip()
        if not publish_id:
            final = self.store.fail_direct_test_publish(
                normalized_id,
                token,
                error_code="tt_post_gpu_publish_id_missing",
                error_message="TT GPU返回结果缺少publish_id",
                publish_was_not_created=False,
            )
        else:
            try:
                final = self.store.record_direct_test_publish_id(
                    normalized_id,
                    token,
                    publish_id,
                )
            except TTPostError:
                # The GPU ledger is now authoritative; never attempt init again.
                final = self.store.fail_direct_test_publish(
                    normalized_id,
                    token,
                    error_code="tt_post_publish_id_persistence_failed",
                    error_message="GPU已返回publish_id但CPU落账失败，需要人工核对",
                    publish_was_not_created=False,
                )
        return {"item": self._direct_test_api_item(final), "gates": self._gates()}

    def direct_reconciling(self, limit: Any = 100) -> Dict[str, Any]:
        rows = self.store.list_direct_tests(
            status="reconciling",
            limit=_positive_int(limit, "立即测试核对数量", 100),
        )
        return {
            "items": [self._direct_test_api_item(row) for row in rows],
            "gates": self._gates(),
        }

    def direct_reconcile(self, direct_test_id: Any) -> Dict[str, Any]:
        normalized_id = _positive_int(direct_test_id, "立即测试任务ID")
        item = self.store.get_direct_test(normalized_id)
        if str(item.get("status") or "") == "published":
            return {"item": self._direct_test_api_item(item), "gates": self._gates()}
        if str(item.get("status") or "") not in {"reconciling", "unknown"}:
            raise TTPostServiceError(
                "tt_post_reconcile_only",
                "立即测试没有可核对的TikTok发布结果",
                409,
            )
        account_id = str(item["account_id"])
        gpu_job_id = _required_gpu_job_id(item.get("gpu_job_id"))
        with self.account_source.publish_credentials(account_id) as credentials:
            result = self.gpu_client.reconcile(
                job_id=gpu_job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        returned_publish_id = str(result.get("publish_id") or "").strip()
        if not returned_publish_id:
            raise TTPostServiceError(
                "tt_post_gpu_ledger_publish_id_missing",
                "GPU账本没有可恢复的TikTok publish_id",
                409,
            )
        if str(item.get("status") or "") == "unknown":
            item = self.store.recover_direct_test_publish_id(
                normalized_id,
                returned_publish_id,
            )
        elif not secrets.compare_digest(
            str(item.get("publish_id") or ""), returned_publish_id
        ):
            raise TTPostServiceError(
                "tt_post_publish_id_conflict",
                "GPU账本publish_id与CPU冻结记录不一致",
                409,
            )
        remote_status = str(
            result.get("state") or result.get("remote_status") or ""
        ).lower()
        if remote_status in {"published", "publish_complete"}:
            item = self.store.reconcile_direct_test_published(
                normalized_id,
                returned_publish_id,
                publish_url=str(result.get("publish_url") or ""),
            )
        elif remote_status in {"failed", "publish_failed"}:
            item = self.store.reconcile_direct_test_failed(
                normalized_id,
                returned_publish_id,
                remote_status=remote_status,
            )
        return {
            "item": self._direct_test_api_item(item),
            "remote_status": remote_status,
            "gates": self._gates(),
        }

    def publish_claimed(
        self,
        queue_id: Any,
        claim_token: Any,
    ) -> Dict[str, Any]:
        normalized_queue_id = _positive_int(queue_id, "发布队列ID")
        token = _bounded_text(claim_token, "认领凭据", 512)
        queue = self.store.get_queue(normalized_queue_id)
        if queue.get("status") == "unknown" or queue.get("unknown_outcome"):
            raise TTPostServiceError(
                "tt_post_unknown_no_retry",
                "未知结果任务禁止重新发布",
                409,
            )
        if queue.get("publish_id") or queue.get("status") == "reconciling":
            raise TTPostServiceError(
                "tt_post_reconcile_only",
                "已取得publish_id的任务只能核对",
                409,
            )
        manual_canary = self._is_manual_canary_queue(queue)
        if (
            queue.get("publish_mode") != "direct_post"
            or (not self.gates.is_open and not manual_canary)
        ):
            blocked = self.store.block_compliance(
                normalized_queue_id,
                token,
            )
            self._sync_recurring_queue_if_present(blocked)
            return {
                "item": self._queue_api_item(blocked, gates=self.gates),
                "gates": self._gates(),
            }
        account_id = str(queue["account_id"])
        try:
            gpu_job_id = _required_gpu_job_id(queue.get("gpu_job_id"))
        except TTPostError:
            failed = self.store.mark_failed(
                normalized_queue_id,
                token,
                error_code="tt_gpu_job_id_missing",
                error_message="冻结任务缺少稳定TT GPU任务ID",
                publish_was_not_created=True,
            )
            self._sync_recurring_queue_if_present(failed)
            return {
                "item": self._queue_api_item(failed, gates=self.gates),
                "gates": self._gates(),
            }
        try:
            creator = self._creator_recheck(
                account_id,
                gpu_job_id,
                normalized_queue_id,
                token,
            )
            policy = TTPostPolicy(
                privacy_level=str(queue["privacy_level"]),
                allow_comment=bool(queue["allow_comment"]),
                allow_duet=bool(queue["allow_duet"]),
                allow_stitch=bool(queue["allow_stitch"]),
                brand_content_toggle=False,
                brand_organic_toggle=False,
                user_consent=True,
                consent_version=str(queue["consent_version"]),
                consented_at_utc=str(queue["consented_at_utc"]),
            )
            self._assert_creator_policy(creator, policy)
            if not secrets.compare_digest(
                _creator_info_hash(creator),
                str(queue.get("creator_info_hash") or ""),
            ):
                raise TTPostServiceError(
                    "tt_creator_info_changed",
                    "TikTok账号实时能力与排期冻结快照不一致",
                    409,
                )
            self.store.renew_claim(
                normalized_queue_id,
                token,
                lease_seconds=self._claim_lease_seconds(),
            )
            queue = self._prepare_queue_short_link(queue, token)
        except TTPostError as exc:
            failed = self.store.mark_failed(
                normalized_queue_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
                publish_was_not_created=True,
            )
            self._sync_recurring_queue_if_present(failed)
            return {
                "item": self._queue_api_item(failed, gates=self.gates),
                "gates": self._gates(),
            }

        try:
            with self.account_source.publish_credentials(account_id) as credentials:
                self.store.renew_claim(
                    normalized_queue_id,
                    token,
                    lease_seconds=self._claim_lease_seconds(),
                )
                publishing = (
                    self.store.begin_manual_canary_publish(
                        normalized_queue_id,
                        token,
                        self.manual_canary.identity(),
                    )
                    if manual_canary
                    else self.store.begin_publish(
                        normalized_queue_id,
                        token,
                        self.gates,
                    )
                )
                result = self.gpu_client.publish(
                    job_id=gpu_job_id,
                    source_account_id=account_id,
                    access_token=credentials.reveal_access_token(),
                    queue=publishing,
                    manual_canary=manual_canary,
                    manual_canary_id=(
                        self.manual_canary.canary_id
                        if manual_canary
                        else ""
                    ),
                )
        except GPUClientError as exc:
            recovered_publish_id = str(exc.details.get("publish_id") or "")
            if (
                exc.code == "tt_publish_reconcile_required"
                and recovered_publish_id
            ):
                final = self._record_remote_publish_id_or_unknown(
                    normalized_queue_id,
                    token,
                    recovered_publish_id,
                )
            elif exc.unknown_outcome or not exc.publish_was_not_created:
                final = self.store.mark_unknown(
                    normalized_queue_id,
                    token,
                    reason=str(exc),
                )
            else:
                final = self.store.mark_failed(
                    normalized_queue_id,
                    token,
                    error_code=exc.code,
                    error_message=str(exc),
                    publish_was_not_created=True,
                )
            self._sync_recurring_queue_if_present(final)
            return {
                "item": self._queue_api_item(final, gates=self.gates),
                "gates": self._gates(),
            }
        except TTPostError as exc:
            failed = self.store.mark_failed(
                normalized_queue_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
                publish_was_not_created=True,
            )
            self._sync_recurring_queue_if_present(failed)
            return {
                "item": self._queue_api_item(failed, gates=self.gates),
                "gates": self._gates(),
            }
        publish_id = str(result.get("publish_id") or "").strip()
        if not publish_id:
            final = self.store.mark_unknown(
                normalized_queue_id,
                token,
                reason="TT GPU返回结果缺少publish_id",
            )
        else:
            final = self._record_remote_publish_id_or_unknown(
                normalized_queue_id,
                token,
                publish_id,
            )
            if final.get("status") == "reconciling":
                remote_status = str(
                    result.get("state")
                    or result.get("remote_status")
                    or ""
                ).lower()
                if remote_status in {"published", "publish_complete"}:
                    final = self._reconcile_published_with_cache(
                        normalized_queue_id,
                        publish_id,
                        publish_url=str(result.get("publish_url") or ""),
                    )
        self._sync_recurring_queue_if_present(final)
        return {
            "item": self._queue_api_item(final, gates=self.gates),
            "gates": self._gates(),
        }

    def reconciling(self, limit: Any = 100) -> Dict[str, Any]:
        rows = self.store.list_reconciling(
            limit=_positive_int(limit, "核对任务数量", 100)
        )
        return {
            "items": [
                self._queue_api_item(row, gates=self.gates) for row in rows
            ],
            "gates": self._gates(),
        }

    def reconcile(self, queue_id: Any) -> Dict[str, Any]:
        normalized_queue_id = _positive_int(queue_id, "发布队列ID")
        queue = self.store.get_queue(normalized_queue_id)
        if queue.get("status") == "published":
            self._sync_recurring_queue_if_present(queue)
            return {
                "item": self._queue_api_item(queue, gates=self.gates),
                "gates": self._gates(),
            }
        if queue.get("status") != "reconciling" or not queue.get("publish_id"):
            raise TTPostServiceError(
                "tt_post_reconcile_only",
                "任务没有可核对的TikTok publish_id",
                409,
            )
        account_id = str(queue["account_id"])
        gpu_job_id = _required_gpu_job_id(queue.get("gpu_job_id"))
        with self.account_source.publish_credentials(account_id) as credentials:
            result = self.gpu_client.reconcile(
                job_id=gpu_job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        remote_status = str(
            result.get("state") or result.get("remote_status") or ""
        ).lower()
        returned_publish_id = str(result.get("publish_id") or "")
        if not secrets.compare_digest(
            returned_publish_id,
            str(queue["publish_id"]),
        ):
            raise TTPostServiceError(
                "tt_post_publish_id_conflict",
                "GPU账本publish_id与CPU冻结记录不一致",
                409,
            )
        if remote_status in {"published", "publish_complete"}:
            queue = self._reconcile_published_with_cache(
                normalized_queue_id,
                str(queue["publish_id"]),
                publish_url=str(result.get("publish_url") or ""),
            )
        elif remote_status in {"failed", "publish_failed"}:
            queue = self.store.reconcile_failed(
                normalized_queue_id,
                str(queue["publish_id"]),
                remote_status=remote_status,
            )
        self._sync_recurring_queue_if_present(queue)
        return {
            "item": self._queue_api_item(queue, gates=self.gates),
            "remote_status": remote_status,
            "gates": self._gates(),
        }

    def manual_reconcile(self, queue_id: Any) -> Dict[str, Any]:
        """Human-triggered reconcile, including safe unknown-ID recovery."""

        normalized_queue_id = _positive_int(queue_id, "发布队列ID")
        queue = self.store.get_queue(normalized_queue_id)
        if queue.get("status") not in {
            "unknown",
            "publishing",
            "reconciling",
            "published",
        }:
            raise TTPostServiceError(
                "tt_post_manual_reconcile_not_allowed",
                "当前任务不允许人工核对",
                409,
            )
        if queue.get("status") == "published":
            self._sync_recurring_queue_if_present(queue)
            return {
                "item": self._queue_api_item(queue, gates=self.gates),
                "gates": self._gates(),
            }
        account_id = str(queue["account_id"])
        gpu_job_id = _required_gpu_job_id(queue.get("gpu_job_id"))
        with self.account_source.publish_credentials(account_id) as credentials:
            result = self.gpu_client.reconcile(
                job_id=gpu_job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        returned_publish_id = str(result.get("publish_id") or "").strip()
        if not returned_publish_id:
            raise TTPostServiceError(
                "tt_post_gpu_ledger_publish_id_missing",
                "GPU账本没有可恢复的TikTok publish_id",
                409,
            )
        if queue.get("status") in {"unknown", "publishing"}:
            queue = self.store.recover_publish_id_from_gpu_ledger(
                normalized_queue_id,
                returned_publish_id,
            )
        elif not secrets.compare_digest(
            str(queue.get("publish_id") or ""),
            returned_publish_id,
        ):
            raise TTPostServiceError(
                "tt_post_publish_id_conflict",
                "GPU账本publish_id与CPU冻结记录不一致",
                409,
            )
        remote_status = str(
            result.get("state") or result.get("remote_status") or ""
        ).lower()
        if remote_status in {"published", "publish_complete"}:
            queue = self._reconcile_published_with_cache(
                normalized_queue_id,
                returned_publish_id,
                publish_url=str(result.get("publish_url") or ""),
            )
        elif remote_status in {"failed", "publish_failed"}:
            queue = self.store.reconcile_failed(
                normalized_queue_id,
                returned_publish_id,
                remote_status=remote_status,
            )
        self._sync_recurring_queue_if_present(queue)
        return {
            "item": self._queue_api_item(queue, gates=self.gates),
            "remote_status": remote_status,
            "gates": self._gates(),
        }


def _required_env(source: Mapping[str, str], name: str) -> str:
    value = str(source.get(name, ""))
    if not value:
        raise TTPostServiceError(
            "tt_post_config_missing",
            "TT Post服务配置不完整",
            500,
        )
    return value


def build_service_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> TTPostService:
    source = os.environ if environ is None else environ
    mysql = SnapshotMySQLConfig.from_env(source)
    account_repository = MySQLSnapshotAccountRepository(mysql)
    material_database = str(
        source.get("TT_POST_MATERIAL_MYSQL_DATABASE", DEFAULT_MATERIAL_SCHEMA)
    ).strip()
    material_resolver = DramawaveMaterialResolver(
        lambda: connect_read_only(
            host=mysql.host,
            port=mysql.port,
            user=mysql.user,
            password=mysql.password,
            database=material_database,
            connect_timeout=5,
            read_timeout=30,
        ),
        schema=material_database,
    )
    internal_token = _required_env(source, "TT_POST_INTERNAL_TOKEN")
    gpu_token = _required_env(source, "TT_POST_GPU_INTERNAL_TOKEN")
    if secrets.compare_digest(internal_token, gpu_token):
        raise TTPostServiceError(
            "tt_post_bearer_reuse_denied",
            "CPU与GPU内部凭据必须独立",
            500,
        )
    gpu = GPUClient(
        str(source.get("TT_POST_GPU_URL", DEFAULT_GPU_URL)),
        gpu_token,
        _required_env(source, "TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64"),
        timeout=int(source.get("TT_POST_GPU_TIMEOUT", "300")),
        prepare_timeout=int(
            source.get("TT_POST_GPU_PREPARE_TIMEOUT", "9000")
        ),
    )
    db_path = str(source.get("TT_POST_DB_PATH", DEFAULT_DB_PATH)).strip()
    if not Path(db_path).is_absolute():
        raise TTPostServiceError(
            "tt_post_db_path_invalid",
            "TT Post账本路径必须是绝对路径",
            500,
        )
    redis_host = str(
        source.get("TT_POST_CODE_REDIS_HOST", DEFAULT_CODE_REDIS_HOST)
    ).strip()
    try:
        redis_port = int(
            source.get("TT_POST_CODE_REDIS_PORT", str(DEFAULT_CODE_REDIS_PORT))
        )
        redis_timeout = float(
            source.get(
                "TT_POST_CODE_REDIS_TIMEOUT_SECONDS",
                str(DEFAULT_CODE_REDIS_TIMEOUT),
            )
        )
    except (TypeError, ValueError, OverflowError):
        raise TTPostServiceError(
            "tt_post_code_redis_config_invalid",
            "TT code Redis configuration is invalid",
            500,
        ) from None
    if (
        redis_host not in {"127.0.0.1", "::1"}
        or redis_port < 1
        or redis_port > 65535
        or redis_timeout <= 0
        or redis_timeout > 5
    ):
        raise TTPostServiceError(
            "tt_post_code_redis_config_invalid",
            "TT code Redis configuration is invalid",
            500,
        )
    code_route_lock = threading.RLock()
    store = TTPostStore(db_path, code_route_lock=code_route_lock)
    code_resolver = TTCodeRouteResolver(
        db_path,
        redis_client=RedisRESPClient(
            redis_host,
            redis_port,
            redis_timeout,
        ),
        lock=code_route_lock,
    )
    return TTPostService(
        store,
        account_repository,
        material_resolver,
        gpu,
        gates=LiveGates.from_env(source),
        manual_canary=ManualPublishCanary.from_env(source),
        source_trim_tail_seconds=source.get(
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS",
            "4.333333",
        ),
        media_profile_version=str(
            source.get(
                "TT_POST_MEDIA_PROFILE_VERSION",
                "tt-post-hevc-720x1280-v2",
            )
        ),
        runner_kick_path=str(
            source.get(
                "TT_POST_RUNNER_KICK_PATH",
                DEFAULT_RUNNER_KICK_PATH,
            )
        ),
        preparation_kick_path=str(
            source.get(
                "TT_POST_PREPARE_RUNNER_KICK_PATH",
                DEFAULT_PREPARATION_KICK_PATH,
            )
        ),
        short_link_root=str(
            source.get(
                "TT_POST_SHORT_LINK_ROOT",
                DEFAULT_SHORT_LINK_ROOT,
            )
        ),
        code_resolver=code_resolver,
    )


class TTPostHTTPServer(ThreadingHTTPServer):
    """Loopback server carrying a service instance and one dedicated bearer."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        service: TTPostService,
        internal_token: str,
    ):
        host, _port = server_address
        if host not in SAFE_INTERNAL_HOSTS:
            raise TTPostServiceError(
                "tt_post_listen_host_invalid",
                "TT Post服务只能监听loopback",
                500,
            )
        token = str(internal_token or "")
        if len(token) < 32 or len(token) > 512:
            raise TTPostServiceError(
                "tt_post_internal_bearer_invalid",
                "TT Post内部凭据未配置",
                500,
            )
        self.tt_service = service
        self.internal_token = token
        super().__init__(server_address, TTPostRequestHandler)


class TTPostRequestHandler(BaseHTTPRequestHandler):
    """Secret-silent JSON handler for UI-proxy and runner contracts."""

    server: TTPostHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        peer = str(self.client_address[0] if self.client_address else "")
        if peer not in {"127.0.0.1", "::1"}:
            self._json(403, {"code": "tt_post_loopback_required", "message": "拒绝访问"})
            return False
        supplied = str(self.headers.get("Authorization") or "")
        prefix = "Bearer "
        if not supplied.startswith(prefix) or not secrets.compare_digest(
            supplied[len(prefix) :],
            self.server.internal_token,
        ):
            self._json(
                403,
                {"code": "tt_post_internal_required", "message": "拒绝访问"},
            )
            return False
        return True

    def _body(self) -> Dict[str, Any]:
        raw_length = str(self.headers.get("Content-Length") or "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_HTTP_BODY_BYTES:
            raise TTPostServiceError(
                "invalid_request",
                "请求体超过安全上限",
                413,
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            raise TTPostServiceError("invalid_request", "请求体不是有效JSON", 400) from None
        if not isinstance(payload, dict) or _contains_sensitive_key(payload):
            raise TTPostServiceError("invalid_request", "请求体包含无效字段", 400)
        return payload

    def _dispatch(self) -> Mapping[str, Any]:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        service = self.server.tt_service
        if self.command == "GET" and path == "/health":
            return {"ok": True, "gates": service.gates.as_dict()}
        if not self._authorized():
            raise PermissionError
        if self.command == "GET" and path == "/internal/tt-posts/code-resolve":
            query_params = urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
            )
            if (
                parsed.fragment
                or set(query_params) != {"query", "source"}
                or len(query_params["query"]) != 1
                or len(query_params["source"]) != 1
            ):
                raise TTPostServiceError(
                    "invalid_request",
                    "TT code resolve query is invalid",
                    400,
                )
            return service.resolve_code_route(
                query_params["query"][0],
                query_params["source"][0],
            )
        if self.command == "GET" and path == "/api/admin/tt-posts/accounts":
            return service.accounts()
        if self.command == "GET" and path == "/api/admin/tt-posts/auto-config":
            return service.auto_config_get()
        if self.command == "POST" and path == "/api/admin/tt-posts/auto-config":
            return service.auto_config_save(self._body())
        if (
            self.command == "GET"
            and path == "/api/admin/tt-posts/account-settings"
        ):
            return service.account_settings()
        if self.command == "POST" and path == "/api/admin/tt-posts/creator-info":
            return service.creator_info(self._body())
        if (
            self.command == "POST"
            and path == "/api/admin/tt-posts/account-settings/creator-info"
        ):
            return service.creator_info(self._body())
        if (
            self.command == "POST"
            and path
            == "/api/admin/tt-posts/account-settings/batch/creator-info"
        ):
            return service.account_settings_batch_creator_info(self._body())
        if (
            self.command == "POST"
            and path == "/api/admin/tt-posts/account-settings/batch"
        ):
            return service.account_settings_batch_save(self._body())
        if (
            self.command == "POST"
            and path == "/api/admin/tt-posts/account-settings"
        ):
            return service.account_settings_save(self._body())
        creator = re.fullmatch(
            r"/api/admin/tt-posts/accounts/([1-9][0-9]*)/creator-info",
            path,
        )
        if self.command == "POST" and creator:
            payload = self._body()
            supplied = payload.get("source_account_id")
            if supplied not in (None, "", creator.group(1)):
                raise TTPostServiceError(
                    "tt_account_metadata_mismatch",
                    "请求账号身份不一致",
                    409,
                )
            return service.creator_info(
                {"source_account_id": creator.group(1)}
            )
        if self.command == "POST" and path in {
            "/api/admin/tt-posts/materials/preview",
            "/api/admin/tt-posts/materials/prepare",
        }:
            return service.material_preview(self._body())
        if self.command == "GET" and path == "/api/admin/tt-posts/material-pool":
            return service.material_pool_list(
                urllib.parse.parse_qs(parsed.query)
            )
        if self.command == "POST" and path == "/api/admin/tt-posts/material-pool":
            return service.material_pool_add(self._body())
        if self.command == "GET" and path == "/api/admin/tt-posts/direct-tests":
            return service.direct_tests_list(
                urllib.parse.parse_qs(parsed.query)
            )
        if self.command == "POST" and path == "/api/admin/tt-posts/test-publish":
            return service.direct_test_create(self._body())
        if (
            self.command == "POST"
            and path == "/internal/tt-posts/preparations/claim"
        ):
            return service.preparation_claim(self._body())
        preparation_renew = re.fullmatch(
            r"/internal/tt-posts/preparations/([1-9][0-9]*)/renew",
            path,
        )
        if self.command == "POST" and preparation_renew:
            return service.preparation_renew(
                preparation_renew.group(1),
                self._body(),
            )
        preparation_process = re.fullmatch(
            r"/internal/tt-posts/preparations/([1-9][0-9]*)/process",
            path,
        )
        if self.command == "POST" and preparation_process:
            return service.preparation_process(
                preparation_process.group(1),
                self._body(),
            )
        if (
            self.command == "POST"
            and path == "/internal/tt-posts/direct-tests/preparations/claim"
        ):
            return service.direct_preparation_claim(self._body())
        direct_preparation_renew = re.fullmatch(
            r"/internal/tt-posts/direct-tests/preparations/([1-9][0-9]*)/renew",
            path,
        )
        if self.command == "POST" and direct_preparation_renew:
            return service.direct_preparation_renew(
                direct_preparation_renew.group(1),
                self._body(),
            )
        direct_preparation_process = re.fullmatch(
            r"/internal/tt-posts/direct-tests/preparations/([1-9][0-9]*)/process",
            path,
        )
        if self.command == "POST" and direct_preparation_process:
            return service.direct_preparation_process(
                direct_preparation_process.group(1),
                self._body(),
            )
        if self.command == "GET" and path == "/api/admin/tt-posts/schedule":
            return service.schedule_get(
                urllib.parse.parse_qs(parsed.query)
            )
        if self.command == "POST" and path == "/api/admin/tt-posts/schedule":
            return service.schedule_save(self._body())
        if self.command == "POST" and path == "/api/admin/tt-posts/run-now":
            return service.run_now(self._body())
        if self.command == "GET" and path == "/api/admin/tt-posts/tasks":
            return service.publish_tasks_list(
                urllib.parse.parse_qs(parsed.query)
            )
        if self.command == "GET" and path == "/api/admin/tt-posts/queue":
            return service.queue_list(urllib.parse.parse_qs(parsed.query))
        if self.command == "POST" and path == "/api/admin/tt-posts/queue":
            return service.queue_create(self._body())
        cancel = re.fullmatch(
            r"/api/admin/tt-posts/queue/([1-9][0-9]*)/cancel",
            path,
        )
        if self.command == "POST" and cancel:
            return service.queue_cancel(cancel.group(1), self._body())
        manual_reconcile = re.fullmatch(
            r"/api/admin/tt-posts/queue/([1-9][0-9]*)/reconcile",
            path,
        )
        if self.command == "POST" and manual_reconcile:
            self._body()
            return service.manual_reconcile(manual_reconcile.group(1))
        if self.command == "GET" and path == "/api/admin/tt-posts/events":
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get("queue_id") or []
            return service.events(values[0] if values else "")
        queue_events = re.fullmatch(
            r"/api/admin/tt-posts/queue/([1-9][0-9]*)/events",
            path,
        )
        if self.command == "GET" and queue_events:
            return service.events(queue_events.group(1))
        if self.command == "POST" and path == "/internal/tt-posts/claim":
            return service.claim_due(self._body())
        if (
            self.command == "POST"
            and path == "/internal/tt-posts/direct-tests/claim"
        ):
            return service.direct_publish_claim(self._body())
        if (
            self.command == "GET"
            and path == "/internal/tt-posts/direct-tests/reconciling"
        ):
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get("limit") or ["100"]
            return service.direct_reconciling(values[0])
        if (
            self.command == "POST"
            and path == "/internal/tt-posts/schedules/due"
        ):
            return service.schedules_due(self._body())
        if self.command == "GET" and path == "/internal/tt-posts/reconciling":
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get("limit") or ["100"]
            return service.reconciling(values[0])
        publish = re.fullmatch(
            r"/internal/tt-posts/queue/([1-9][0-9]*)/publish",
            path,
        )
        if self.command == "POST" and publish:
            payload = self._body()
            return service.publish_claimed(
                publish.group(1),
                payload.get("claim_token"),
            )
        direct_publish = re.fullmatch(
            r"/internal/tt-posts/direct-tests/([1-9][0-9]*)/publish",
            path,
        )
        if self.command == "POST" and direct_publish:
            payload = self._body()
            return service.direct_publish_claimed(
                direct_publish.group(1),
                payload.get("claim_token"),
            )
        reconcile = re.fullmatch(
            r"/internal/tt-posts/queue/([1-9][0-9]*)/reconcile",
            path,
        )
        if self.command == "POST" and reconcile:
            self._body()
            return service.reconcile(reconcile.group(1))
        direct_reconcile = re.fullmatch(
            r"/internal/tt-posts/direct-tests/([1-9][0-9]*)/reconcile",
            path,
        )
        if self.command == "POST" and direct_reconcile:
            self._body()
            return service.direct_reconcile(direct_reconcile.group(1))
        raise TTPostServiceError("not_found", "接口不存在", 404)

    def _handle(self) -> None:
        try:
            payload = self._dispatch()
            self._json(200, payload)
        except PermissionError:
            return
        except TTPostError as exc:
            self._json(
                exc.status,
                {
                    "code": exc.code,
                    "message": str(exc),
                },
            )
        except Exception:
            self._json(
                500,
                {
                    "code": "tt_post_internal_error",
                    "message": "TT Post服务内部错误",
                },
            )

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def serve(environ: Optional[Mapping[str, str]] = None) -> None:
    source = os.environ if environ is None else environ
    host = str(source.get("TT_POST_SERVICE_HOST", DEFAULT_CPU_HOST)).strip()
    try:
        port = int(source.get("TT_POST_SERVICE_PORT", DEFAULT_CPU_PORT))
    except (TypeError, ValueError):
        port = 0
    if host not in SAFE_INTERNAL_HOSTS or port < 1 or port > 65535:
        raise TTPostServiceError(
            "tt_post_listen_config_invalid",
            "TT Post监听配置无效",
            500,
        )
    server = TTPostHTTPServer(
        (host, port),
        build_service_from_env(source),
        _required_env(source, "TT_POST_INTERNAL_TOKEN"),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()

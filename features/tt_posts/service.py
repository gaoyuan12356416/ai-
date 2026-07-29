"""CPU-side TikTok Post service, account source, and GPU client.

The module keeps four boundaries explicit:

* account-list SQL projects metadata only and never names ``access_token``;
* one exact token row is read only inside ``publish_credentials``;
* the GPU receives an AES-GCM credential envelope, never a raw token;
* Direct Post init is unreachable unless all three production gates are open.

The HTTP surface is loopback-only and is intended to sit behind the authenticated
AI backend.  It never writes to the source MySQL databases.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import time
import urllib.parse
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
    connect_read_only,
    material_key,
    select_pool_candidates,
    shanghai_now,
)

from .core import (
    AccountSourceError,
    FIXED_CAPTION_TEMPLATE,
    LiveGates,
    MaterialResolution,
    SafeAccount,
    SnapshotAccountSource,
    TTPostAccountSettings,
    TTPostError,
    TTPostPolicy,
    TTPostStore,
    beijing_to_utc,
    redact_text,
    render_fixed_caption,
    render_caption_template,
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
DEFAULT_GRACE_SECONDS = 600
DEFAULT_LEASE_SECONDS = 300
MAX_ACCOUNT_ROWS = 1000
MAX_HTTP_BODY_BYTES = 256 * 1024
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
TOKEN_MIN_VALIDITY_SECONDS = 300
CAPTION_DRAMA_LINE_RE = re.compile(r"(?m)^[ \t]*Drama ID:[ \t]*(\S+)[ \t]*$")
SAFE_INTERNAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


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
        self.details = {
            key: str(raw_details.get(key) or "")[:512]
            for key in ("publish_id", "state", "log_id")
            if raw_details.get(key) not in (None, "")
        }
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


class DramawaveMaterialResolver:
    """Resolve one manual material through the strict X Dramawave validator."""

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
            selected, rejections = select_pool_candidates(
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
            raise TTPostServiceError(
                str(rejection.get("error_code") or "tt_material_not_eligible")[:96],
                str(rejection.get("error_message") or "素材不满足Dramawave发布条件")[:500],
                409,
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
            "description": str(candidate.get("description") or "")[:4096],
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
        "_connection_factory",
    )

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        seal_key: Any,
        *,
        timeout: int = 120,
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
        self.base_url = "http://127.0.0.1:18830"
        self._internal_token = token
        self._seal_key = seal_key
        self.timeout = timeout
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return (
            "GPUClient(base_url=%r, internal_token=<redacted>, "
            "seal_key=<redacted>, timeout=%r)"
            % (self.base_url, self.timeout)
        )

    def _connection(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory("127.0.0.1", 18830, self.timeout)
        return http.client.HTTPConnection("127.0.0.1", 18830, timeout=self.timeout)

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        write_may_have_happened: bool = False,
    ) -> Dict[str, Any]:
        if (
            not re.fullmatch(r"/internal/tt-post/(?:creator-info|prepare|publish|reconcile)", path)
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
        connection = self._connection()
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
    ) -> Dict[str, Any]:
        return self._post(
            "/internal/tt-post/prepare",
            {
                "job_id": job_id,
                "content_id": material.get("content_id"),
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
    ) -> Dict[str, Any]:
        envelope = self._envelope(
            access_token,
            job_id=job_id,
            source_account_id=source_account_id,
            operation="publish",
        )
        return self._post(
            "/internal/tt-post/publish",
            {
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
            },
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
        caption = render_caption_template(template, content_id)
        if raw_caption not in (None, ""):
            submitted = _bounded_text(raw_caption, "发布描述", 2200)
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
    rendered = render_caption_template(template, content_id)
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
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        source_trim_tail_seconds: float = 4.333333,
        media_profile_version: str = "tt-post-outro-20260729-v1",
    ):
        self.store = store
        self.account_repository = account_repository
        self.account_source = account_repository.as_account_source()
        self.material_resolver = material_resolver
        self.gpu_client = gpu_client
        self.gates = LiveGates.from_env() if gates is None else gates
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

    def _gates(self) -> Dict[str, bool]:
        return self.gates.as_dict()

    def accounts(self) -> Dict[str, Any]:
        items = []
        for account in self.account_repository.list_public_accounts():
            item = dict(account)
            item["account_settings"] = (
                self.store.get_account_settings(account["source_account_id"])
                or {"configured": False}
            )
            items.append(item)
        return {
            "items": items,
            "gates": self._gates(),
        }

    def account_settings(self) -> Dict[str, Any]:
        return self.accounts()

    def creator_info(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        account_id = _positive_decimal(
            payload.get("source_account_id"),
            "TikTok账号ID",
        )
        self.account_repository.get_public_account(account_id)
        job_id = _job_id("ttcreator", account_id)
        with self.account_source.publish_credentials(account_id) as credentials:
            raw = self.gpu_client.creator_info(
                job_id=job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        item = raw.get("creator_info", raw)
        return {"item": _normalized_creator_info(item), "gates": self._gates()}

    def _resolve_and_prepare(
        self,
        material_id: Any,
        *,
        gpu_job_id: str = "",
    ) -> Dict[str, Any]:
        resolved = self.material_resolver.resolve(material_id)
        job_id = gpu_job_id or (
            "ttpreview-"
            + hashlib.sha256(
                (
                    resolved["material_id"]
                    + "|"
                    + resolved["content_id"]
                    + "|"
                    + hashlib.sha256(
                        resolved["source_media_url"].encode("utf-8")
                    ).hexdigest()
                    + "|"
                    + self.media_profile_version
                    + "|"
                    + str(self.source_trim_tail_seconds)
                ).encode("utf-8")
            ).hexdigest()[:36]
        )
        prepared = self.gpu_client.prepare(
            job_id=job_id,
            material=resolved,
            source_trim_tail_seconds=self.source_trim_tail_seconds,
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
        final_url = _safe_https_url(
            prepared.get("output_url")
            or prepared.get("prepared_media_url")
            or prepared.get("final_media_url"),
            "TT最终成片地址",
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
                "profile": str(prepared.get("profile") or "")[:128],
                "media_profile_version": self.media_profile_version,
                "status": str(prepared.get("status") or "ready")[:64],
                "status_label": str(
                    prepared.get("status_label") or "最终成片已准备"
                )[:128],
            }
        )
        return result

    def material_preview(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        material_id = _positive_decimal(payload.get("material_id"), "素材ID", 19)
        return {
            "item": self._resolve_and_prepare(material_id),
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
        allowed = {
            "source_account_id",
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "commercial_disclosure",
            "brand_organic_toggle",
            "brand_content_toggle",
            "is_aigc",
            "expected_version",
        }
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
            }
        )

    @staticmethod
    def _policy_from_account_settings(
        settings: TTPostAccountSettings,
        consent: Mapping[str, Any],
    ) -> TTPostPolicy:
        return TTPostPolicy.from_mapping(
            {
                "privacy_level": settings.privacy_level,
                "allow_comment": settings.allow_comment,
                "allow_duet": settings.allow_duet,
                "allow_stitch": settings.allow_stitch,
                "brand_content_toggle": settings.brand_content_toggle,
                "brand_organic_toggle": settings.brand_organic_toggle,
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

    def _ensure_pool_item(self, material_id: str) -> Dict[str, Any]:
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

    @staticmethod
    def _queue_api_item(
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
        if item["publish_mode"] != "direct_post" or not gates.is_open:
            item["publish_mode"] = "hold"
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
            "caption": caption,
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
        caption_template, caption = _caption_from_submission(
            payload,
            requested_content_id,
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
                )
            }
        )
        policy = self._policy_from_account_settings(settings, consent)
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
        queue = self.store.freeze_queue(
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

    def queue_cancel(
        self,
        queue_id: Any,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = {} if payload is None else payload
        reason = str(body.get("reason") or "由AI后台操作人员取消")[:500]
        queue = self.store.cancel_queue(queue_id, reason=reason)
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
        items = []
        for claim in claims:
            queue = claim.queue
            if queue.get("publish_mode") != "direct_post" or not self.gates.is_open:
                blocked = self.store.block_compliance(
                    claim.queue_id,
                    claim.reveal_claim_token(),
                )
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
    ) -> Dict[str, Any]:
        with self.account_source.publish_credentials(account_id) as credentials:
            raw = self.gpu_client.creator_info(
                job_id=gpu_job_id,
                source_account_id=account_id,
                access_token=credentials.reveal_access_token(),
            )
        return _normalized_creator_info(raw.get("creator_info", raw))

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
        if queue.get("publish_mode") != "direct_post" or not self.gates.is_open:
            blocked = self.store.block_compliance(
                normalized_queue_id,
                token,
            )
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
            return {
                "item": self._queue_api_item(failed, gates=self.gates),
                "gates": self._gates(),
            }
        try:
            creator = self._creator_recheck(account_id, gpu_job_id)
            policy = TTPostPolicy(
                privacy_level=str(queue["privacy_level"]),
                allow_comment=bool(queue["allow_comment"]),
                allow_duet=bool(queue["allow_duet"]),
                allow_stitch=bool(queue["allow_stitch"]),
                brand_content_toggle=bool(queue["brand_content_toggle"]),
                brand_organic_toggle=bool(queue["brand_organic_toggle"]),
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
        except TTPostError as exc:
            failed = self.store.mark_failed(
                normalized_queue_id,
                token,
                error_code=exc.code,
                error_message=str(exc),
                publish_was_not_created=True,
            )
            return {
                "item": self._queue_api_item(failed, gates=self.gates),
                "gates": self._gates(),
            }

        publishing = self.store.begin_publish(
            normalized_queue_id,
            token,
            self.gates,
        )
        try:
            with self.account_source.publish_credentials(account_id) as credentials:
                result = self.gpu_client.publish(
                    job_id=gpu_job_id,
                    source_account_id=account_id,
                    access_token=credentials.reveal_access_token(),
                    queue=publishing,
                )
        except GPUClientError as exc:
            recovered_publish_id = str(exc.details.get("publish_id") or "")
            if (
                exc.code == "tt_publish_reconcile_required"
                and recovered_publish_id
            ):
                final = self.store.record_publish_id(
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
            return {
                "item": self._queue_api_item(final, gates=self.gates),
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
            final = self.store.record_publish_id(
                normalized_queue_id,
                token,
                publish_id,
            )
            remote_status = str(
                result.get("state") or result.get("remote_status") or ""
            ).lower()
            if remote_status in {"published", "publish_complete"}:
                final = self.store.reconcile_published(
                    normalized_queue_id,
                    publish_id,
                    publish_url=str(result.get("publish_url") or ""),
                )
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
            queue = self.store.reconcile_published(
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
            "reconciling",
            "published",
        }:
            raise TTPostServiceError(
                "tt_post_manual_reconcile_not_allowed",
                "当前任务不允许人工核对",
                409,
            )
        if queue.get("status") == "published":
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
        if queue.get("status") == "unknown":
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
            queue = self.store.reconcile_published(
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
    )
    db_path = str(source.get("TT_POST_DB_PATH", DEFAULT_DB_PATH)).strip()
    if not Path(db_path).is_absolute():
        raise TTPostServiceError(
            "tt_post_db_path_invalid",
            "TT Post账本路径必须是绝对路径",
            500,
        )
    return TTPostService(
        TTPostStore(db_path),
        account_repository,
        material_resolver,
        gpu,
        gates=LiveGates.from_env(source),
        source_trim_tail_seconds=source.get(
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS",
            "4.333333",
        ),
        media_profile_version=str(
            source.get(
                "TT_POST_MEDIA_PROFILE_VERSION",
                "tt-post-outro-20260729-v1",
            )
        ),
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
        if self.command == "GET" and path == "/api/admin/tt-posts/accounts":
            return service.accounts()
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
        reconcile = re.fullmatch(
            r"/internal/tt-posts/queue/([1-9][0-9]*)/reconcile",
            path,
        )
        if self.command == "POST" and reconcile:
            self._body()
            return service.reconcile(reconcile.group(1))
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

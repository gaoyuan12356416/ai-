"""Fail-closed adapter for the dedicated ads_ai YouTube ledger."""

from __future__ import annotations

import re
import stat
import threading
import json
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests

from .core import (
    CANARY_APP_ID, CANARY_CHANNEL_LOCAL_ID, CANARY_OPERATION_ID, DramaSynthesisError,
)

TABLE_BY_KIND = {
    "video": "ads_youtube_videos",
    "comment": "ads_youtube_comments",
    "publish_log": "ads_youtube_publish_log",
}
ALLOWED_ACTIONS = frozenset({"select", "insert", "update"})
EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
VIDEO_PAYLOAD_KEYS = frozenset(
    {
        "publish_id", "video_id", "app_id", "channel_local_id", "operator_user_id",
        "job_id", "content_id", "source_kind", "source_url", "title",
        "description_rendered", "privacy_status", "published_at_utc",
    }
)
ENTITY_PAYLOAD_KEYS = {
    "video": VIDEO_PAYLOAD_KEYS,
    "comment": frozenset(
        {
            "publish_id", "video_id", "comment_id", "channel_local_id",
            "operator_user_id", "comment_text", "published_at_utc",
        }
    ),
    "publish_log": VIDEO_PAYLOAD_KEYS,
}
TABLE_TO_KIND = {table: kind for kind, table in TABLE_BY_KIND.items()}
WRITER_HEALTH_CONTRACT = "drama-youtube-writer-preflight-v2"


def validate_writer_health(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"ok", "contract", "schema", "writer_identity", "writable", "schema_verified", "indexes_verified", "grant_fingerprint"}
    if (
        not isinstance(value, Mapping) or set(value) != required
        or value.get("ok") is not True or value.get("contract") != WRITER_HEALTH_CONTRACT
        or value.get("schema") != "ads_ai"
        or value.get("writer_identity") != "drama_youtube_writer@43.166.187.96"
        or any(value.get(key) is not True for key in ("writable", "schema_verified", "indexes_verified"))
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("grant_fingerprint") or ""))
    ):
        raise DramaSynthesisError("youtube_sync_health_invalid", "YouTube统一记录服务身份、表结构或权限未通过预检", 503)
    return dict(value)


def read_secure_owned_file(path_text: str, *, max_bytes: int) -> bytes:
    """Read one exact-0600 regular file owned by the current process user."""

    path = Path(str(path_text or ""))
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("secure credential path is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise RuntimeError("secure credential file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > int(max_bytes):
            raise RuntimeError("secure credential file is invalid")
        if os.name != "nt":
            if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
                raise RuntimeError("secure credential file owner or mode is unsafe")
        chunks = []
        remaining = int(max_bytes) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > int(max_bytes):
            raise RuntimeError("secure credential file is invalid")
        return value
    finally:
        os.close(descriptor)


def _valid_publish_id(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 2_147_483_647


def _valid_int(value: Any, *, low: int, high: int = 2_147_483_647) -> bool:
    return type(value) is int and low <= value <= high


def _valid_operator_user_id(value: Any, *, canary: bool = False) -> bool:
    if (type(value) is not str or len(value) > 128
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)):
        return False
    return not canary or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is not None


def _valid_utc(value: Any) -> bool:
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_video_payload(payload: Mapping[str, Any]) -> None:
    source_url = payload.get("source_url")
    parsed = urlsplit(source_url) if type(source_url) is str else None
    valid_url = bool(
        parsed and len(source_url) <= 4096 and parsed.scheme == "https" and parsed.hostname
        and not parsed.username and not parsed.password and not parsed.fragment
    )
    title = payload.get("title")
    description = payload.get("description_rendered")
    content_id = payload.get("content_id")
    if not (
        _valid_int(payload.get("app_id"), low=1)
        and _valid_int(payload.get("channel_local_id"), low=1)
        and _valid_operator_user_id(payload.get("operator_user_id"), canary="canary_operation_id" in payload)
        and type(payload.get("job_id")) is str and JOB_ID_RE.fullmatch(payload["job_id"])
        and type(content_id) is str and 1 <= len(content_id) <= 256
        and payload.get("source_kind") in {"concat_video", "no_bgm_video", "random_template"}
        and valid_url
        and type(title) is str and 1 <= len(title) <= 100
        and type(description) is str and bool(description) and len(description.encode("utf-8")) <= 5000
        and (
            payload.get("privacy_status") == "public" and "canary_operation_id" not in payload
            or payload.get("privacy_status") == "unlisted"
            and payload.get("canary_operation_id") == CANARY_OPERATION_ID
            and payload.get("app_id") == int(CANARY_APP_ID)
            and payload.get("channel_local_id") == int(CANARY_CHANNEL_LOCAL_ID)
        )
        and _valid_utc(payload.get("published_at_utc"))
    ):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)


def _validate_comment_payload(payload: Mapping[str, Any]) -> None:
    comment = payload.get("comment_text")
    if not (
        _valid_int(payload.get("channel_local_id"), low=1)
        and _valid_operator_user_id(payload.get("operator_user_id"), canary="canary_operation_id" in payload)
        and type(comment) is str and 1 <= len(comment) <= 1000
        and _valid_utc(payload.get("published_at_utc"))
        and (
            "canary_operation_id" not in payload
            or payload.get("canary_operation_id") == CANARY_OPERATION_ID
            and payload.get("channel_local_id") == int(CANARY_CHANNEL_LOCAL_ID)
        )
    ):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)


def _expected_external_id(entity_kind: str, payload: Mapping[str, Any]) -> str:
    if not _valid_publish_id(payload.get("publish_id")):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    video_id = payload.get("video_id")
    if type(video_id) is not str or not VIDEO_ID_RE.fullmatch(video_id):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    if entity_kind == "video":
        return video_id
    if entity_kind == "publish_log":
        return str(payload["publish_id"])
    comment_id = payload.get("comment_id")
    if type(comment_id) is not str or not EXTERNAL_ID_RE.fullmatch(comment_id):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    return comment_id


def validate_entity_payload(entity_kind: str, external_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    required = ENTITY_PAYLOAD_KEYS.get(entity_kind)
    if required is not None and isinstance(payload, Mapping) and "canary_operation_id" in payload:
        required = required | {"canary_operation_id"}
        if payload.get("canary_operation_id") != CANARY_OPERATION_ID:
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    if (
        required is None or type(external_id) is not str
        or not isinstance(payload, Mapping) or set(payload.keys()) != required
    ):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    expected = _expected_external_id(entity_kind, payload)
    if external_id != expected:
        raise DramaSynthesisError("youtube_sync_identity_mismatch", "YouTube统一记录身份不匹配", 409)
    if entity_kind in {"video", "publish_log"}:
        _validate_video_payload(payload)
    else:
        _validate_comment_payload(payload)
    return dict(payload)


def validate_external_id(entity_kind: str, external_id: str) -> None:
    if type(external_id) is not str:
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    if entity_kind == "video" and VIDEO_ID_RE.fullmatch(external_id):
        return
    if entity_kind == "comment" and EXTERNAL_ID_RE.fullmatch(external_id):
        return
    if entity_kind == "publish_log" and re.fullmatch(r"[1-9][0-9]{0,18}", external_id):
        return
    raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)


class ControlledRPCExecutor:
    """Send a fixed operation envelope to the dedicated loopback ledger RPC."""

    def __init__(self, url: str, credential_file: str, *, timeout: int = 15, session_factory=requests.Session):
        parsed = urlsplit(str(url or "").strip())
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or port != 18837 or parsed.path != "/v1/youtube-sync"
        ):
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步配置无效", 503)
        try:
            raw = read_secure_owned_file(credential_file, max_bytes=4096)
        except RuntimeError:
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步凭据未配置或权限不安全", 503) from None
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            token = ""
        if not 32 <= len(token) <= 4096 or any(char.isspace() for char in token):
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步凭据无效", 503)
        self.url = parsed.geturl()
        self.token = token
        self.timeout = max(3, min(int(timeout), 60))
        self.session_factory = session_factory

    def health(self) -> Mapping[str, Any]:
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.get(
                "http://127.0.0.1:18837/health", headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException:
            raise DramaSynthesisError("youtube_sync_health_unavailable", "YouTube统一记录服务只读预检不可用", 503) from None
        finally:
            session.close()
        if response.status_code != 200:
            raise DramaSynthesisError("youtube_sync_health_unavailable", "YouTube统一记录服务只读预检未通过", 503)
        try:
            value = response.json()
        except ValueError:
            value = None
        return validate_writer_health(value)

    def __call__(self, action: str, table: str, external_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_controlled_operation(action, table)
        entity_kind = TABLE_TO_KIND[table]
        validate_external_id(entity_kind, external_id)
        if not isinstance(payload, Mapping):
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
        if action == "select":
            if payload:
                raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
            safe_payload = {}
        else:
            safe_payload = validate_entity_payload(entity_kind, external_id, payload)
        if action != "select" and not safe_payload:
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
        session = self.session_factory()
        session.trust_env = False
        try:
            response = session.post(
                self.url,
                json={"action": action, "table": table, "external_id": str(external_id), "payload": safe_payload},
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise DramaSynthesisError("youtube_sync_unavailable", "YouTube统一记录同步暂不可用", 503) from None
        finally:
            session.close()
        if 300 <= response.status_code < 400:
            raise DramaSynthesisError("youtube_sync_redirect_denied", "YouTube统一记录同步拒绝重定向", 503)
        if response.status_code in {401, 403}:
            raise DramaSynthesisError("youtube_sync_auth_failed", "YouTube统一记录同步认证失败", 503)
        if response.status_code != 200:
            raise DramaSynthesisError("youtube_sync_unavailable", "YouTube统一记录同步暂不可用", 503)
        try:
            result = response.json()
        except ValueError:
            result = None
        if not isinstance(result, Mapping):
            raise DramaSynthesisError("youtube_sync_response_invalid", "YouTube统一记录同步响应无效", 503)
        if action == "select" and not isinstance(result.get("found"), bool):
            raise DramaSynthesisError("youtube_sync_response_invalid", "YouTube统一记录同步响应无效", 503)
        if action != "select" and result.get("idempotent_success") is not True:
            raise DramaSynthesisError("youtube_sync_response_invalid", "YouTube统一记录同步响应无效", 503)
        return dict(result)


def build_unified_youtube_writer_from_env(env: Mapping[str, str] = os.environ, *, session_factory=requests.Session):
    url = str(env.get("DRAMA_YOUTUBE_UNIFIED_RPC_URL", "") or "").strip()
    credential_file = str(env.get("DRAMA_YOUTUBE_UNIFIED_RPC_CREDENTIAL_FILE", "") or "").strip()
    if not url and not credential_file:
        return UnifiedYouTubeWriter(None)
    if not url or not credential_file:
        raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步配置必须成对提供", 503)
    try:
        timeout = int(env.get("DRAMA_YOUTUBE_UNIFIED_RPC_TIMEOUT", "15") or 15)
    except (TypeError, ValueError):
        timeout = 15
    return UnifiedYouTubeWriter(ControlledRPCExecutor(url, credential_file, timeout=timeout, session_factory=session_factory))


class UnifiedYouTubeWriter:
    """Call a controlled primary-side RPC; never accept SQL from callers."""

    _gate = threading.BoundedSemaphore(1)

    def __init__(self, executor: Callable[[str, str, str, Mapping[str, Any]], Mapping[str, Any]] | None):
        self.executor = executor

    def preflight(self) -> Mapping[str, Any]:
        health = getattr(self.executor, "health", None)
        if not callable(health):
            raise DramaSynthesisError("youtube_sync_health_unavailable", "YouTube统一记录服务未配置只读预检", 503)
        return validate_writer_health(health())

    def sync(self, entity_kind: str, external_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        table = TABLE_BY_KIND.get(str(entity_kind))
        if table is None:
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
        safe_payload = validate_entity_payload(str(entity_kind), external_id, payload)
        if self.executor is None:
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步尚未配置", 503)
        with self._gate:
            existing = self.executor("select", table, external_id, {})
            if not isinstance(existing, Mapping) or "found" not in existing:
                raise DramaSynthesisError("youtube_sync_failed", "YouTube统一记录同步失败", 503)
            action = "update" if bool(existing.get("found")) else "insert"
            result = self.executor(action, table, external_id, safe_payload)
        if not isinstance(result, Mapping) or not bool(result.get("idempotent_success")):
            raise DramaSynthesisError("youtube_sync_failed", "YouTube统一记录同步失败", 503)
        return result


def validate_controlled_operation(action: str, table: str) -> None:
    if action not in ALLOWED_ACTIONS or table not in set(TABLE_BY_KIND.values()):
        raise DramaSynthesisError("youtube_sync_operation_forbidden", "YouTube统一记录操作被拒绝", 403)


def run_sync_outbox_once(store, writer: UnifiedYouTubeWriter, worker_id: str, *, canary_task_id: int | None = None):
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if canary_task_id is None:
        item = store.claim_youtube_sync(worker_id, expiry)
    else:
        item = store.claim_youtube_sync(worker_id, expiry, canary_task_id=canary_task_id)
    if item is None:
        return {"status": "no_pending", "claimed": False}
    try:
        payload = json.loads(item["payload_json"])
        if not isinstance(payload, Mapping):
            raise DramaSynthesisError("youtube_sync_payload_invalid", "YouTube统一记录待同步数据无效", 409)
        writer.sync(item["entity_kind"], item["external_id"], payload)
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=True)
        return {"status": "synced", "claimed": True, "outbox_id": item["id"]}
    except DramaSynthesisError as exc:
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=False, code=exc.code, message="YouTube统一记录同步失败")
        return {"status": "failed", "claimed": True, "outbox_id": item["id"], "code": exc.code}
    except (ValueError, TypeError, UnicodeDecodeError):
        code = "youtube_sync_payload_invalid"
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=False, code=code, message="YouTube统一记录待同步数据无效")
        return {"status": "failed", "claimed": True, "outbox_id": item["id"], "code": code}
    except Exception:
        code = "youtube_sync_failed"
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=False, code=code, message="YouTube统一记录同步失败")
        return {"status": "failed", "claimed": True, "outbox_id": item["id"], "code": code}


__all__ = ["ALLOWED_ACTIONS", "ENTITY_PAYLOAD_KEYS", "TABLE_BY_KIND", "ControlledRPCExecutor", "UnifiedYouTubeWriter", "build_unified_youtube_writer_from_env", "read_secure_owned_file", "run_sync_outbox_once", "validate_controlled_operation", "validate_entity_payload"]

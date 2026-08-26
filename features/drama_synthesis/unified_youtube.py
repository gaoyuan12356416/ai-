"""Fail-closed adapter for the externally owned unified YouTube ledger."""

from __future__ import annotations

import re
import threading
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests

from .core import DramaSynthesisError

TABLE_BY_KIND = {
    "video": "ads_youtube_videos",
    "comment": "ads_youtube_comments",
    "publish_log": "ads_youtube_publish_log",
}
ALLOWED_ACTIONS = frozenset({"select", "insert", "update"})
EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")
ENTITY_PAYLOAD_KEYS = {
    "video": frozenset({"publish_id", "video_id"}),
    "comment": frozenset({"publish_id", "video_id", "comment_id"}),
    "publish_log": frozenset({"publish_id", "video_id"}),
}
TABLE_TO_KIND = {table: kind for kind, table in TABLE_BY_KIND.items()}


def _valid_publish_id(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 9_223_372_036_854_775_807


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
    if (
        required is None or type(external_id) is not str
        or not isinstance(payload, Mapping) or set(payload.keys()) != required
    ):
        raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
    expected = _expected_external_id(entity_kind, payload)
    if external_id != expected:
        raise DramaSynthesisError("youtube_sync_identity_mismatch", "YouTube统一记录身份不匹配", 409)
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
    """Send a fixed operation envelope to an owner-provided ledger RPC."""

    def __init__(self, url: str, credential_file: str, *, timeout: int = 15, session_factory=requests.Session):
        parsed = urlsplit(str(url or "").strip())
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or (parsed.scheme == "http" and not loopback)
        ):
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步配置无效", 503)
        path = Path(str(credential_file or ""))
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步凭据未配置", 503)
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步凭据权限不安全", 503)
        raw = path.read_bytes()
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            token = ""
        if not 16 <= len(token) <= 4096 or any(char.isspace() for char in token):
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步凭据无效", 503)
        self.url = parsed.geturl()
        self.token = token
        self.timeout = max(3, min(int(timeout), 60))
        self.session_factory = session_factory

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
    if not url or not credential_file:
        return UnifiedYouTubeWriter(None)
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


def run_sync_outbox_once(store, writer: UnifiedYouTubeWriter, worker_id: str):
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    item = store.claim_youtube_sync(worker_id, expiry)
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


__all__ = ["ALLOWED_ACTIONS", "ENTITY_PAYLOAD_KEYS", "TABLE_BY_KIND", "ControlledRPCExecutor", "UnifiedYouTubeWriter", "build_unified_youtube_writer_from_env", "run_sync_outbox_once", "validate_controlled_operation", "validate_entity_payload"]

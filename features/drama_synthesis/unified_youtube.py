"""Fail-closed adapter for the externally owned unified YouTube ledger."""

from __future__ import annotations

import re
import threading
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .core import DramaSynthesisError

TABLE_BY_KIND = {
    "video": "ads_youtube_videos",
    "comment": "ads_youtube_comments",
    "publish_log": "ads_youtube_publish_log",
}
ALLOWED_ACTIONS = frozenset({"select", "insert", "update"})
EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")


class UnifiedYouTubeWriter:
    """Call a controlled primary-side RPC; never accept SQL from callers."""

    _gate = threading.BoundedSemaphore(1)

    def __init__(self, executor: Callable[[str, str, str, Mapping[str, Any]], Mapping[str, Any]] | None):
        self.executor = executor

    def sync(self, entity_kind: str, external_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        table = TABLE_BY_KIND.get(str(entity_kind))
        if table is None or not EXTERNAL_ID_RE.fullmatch(str(external_id or "")) or not isinstance(payload, Mapping):
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
        if self.executor is None:
            raise DramaSynthesisError("youtube_sync_not_configured", "YouTube统一记录同步尚未配置", 503)
        safe_payload = {str(key): value for key, value in payload.items() if str(key) in {"publish_id", "video_id", "comment_id"}}
        if not safe_payload:
            raise DramaSynthesisError("youtube_sync_contract_invalid", "YouTube统一记录合同无效", 409)
        with self._gate:
            existing = self.executor("select", table, str(external_id), {})
            if not isinstance(existing, Mapping) or "found" not in existing:
                raise DramaSynthesisError("youtube_sync_failed", "YouTube统一记录同步失败", 503)
            action = "update" if bool(existing.get("found")) else "insert"
            result = self.executor(action, table, str(external_id), safe_payload)
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
        writer.sync(item["entity_kind"], item["external_id"], payload)
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=True)
        return {"status": "synced", "claimed": True, "outbox_id": item["id"]}
    except DramaSynthesisError as exc:
        store.finish_youtube_sync(item["id"], worker_id=worker_id, lease_generation=item["lease_generation"], success=False, code=exc.code, message=str(exc))
        return {"status": "failed", "claimed": True, "outbox_id": item["id"], "code": exc.code}


__all__ = ["ALLOWED_ACTIONS", "TABLE_BY_KIND", "UnifiedYouTubeWriter", "run_sync_outbox_once", "validate_controlled_operation"]

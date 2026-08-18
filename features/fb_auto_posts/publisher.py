"""Token-shuffled Graph video publisher with an explicit unknown-outcome fence."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Protocol

import requests

from .core import FBAutoPostStore
from .repositories import PageCredential, PagePoolRepository


@dataclass(frozen=True)
class GraphResult:
    kind: str  # success, definite_failure, credential_failure, unknown
    post_id: str = ""
    error_code: str = ""
    message: str = ""
    trace_id: str = ""


class GraphTransport(Protocol):
    def publish_video(self, page_id: str, credential: PageCredential, media_url: str, message: str) -> GraphResult: ...
    def reconcile_video(self, object_id: str, credential: PageCredential) -> GraphResult: ...


class RequestsGraphTransport:
    _CREDENTIAL_ERROR_CODES = {"10", "102", "190", "200"}

    def __init__(self, *, api_version: str = "v22.0", timeout_seconds: int = 120):
        # The 1200-second execute/reconcile lease is sized for at most eight
        # credentials at 120 seconds each.  Reject a larger per-request timeout
        # so configuration drift cannot invalidate that lease budget.
        if not re.fullmatch(r"v[0-9]{2}\.[0-9]", api_version) or not 10 <= int(timeout_seconds) <= 120:
            raise ValueError("invalid Graph transport configuration")
        self.api_version, self.timeout_seconds = api_version, int(timeout_seconds)

    @staticmethod
    def _safe_error(value: Any) -> tuple[str, str]:
        if not isinstance(value, Mapping):
            return "fb_graph_request_rejected", "Meta明确拒绝了发布请求"
        error = value.get("error") if isinstance(value.get("error"), Mapping) else {}
        code = str(error.get("code") or "request_rejected")
        safe_code = "fb_graph_" + re.sub(r"[^a-z0-9]+", "_", code.lower())[:40]
        return safe_code, "Meta明确拒绝了发布请求，可尝试同Page的其他可用授权"

    def publish_video(self, page_id: str, credential: PageCredential, media_url: str, message: str) -> GraphResult:
        session = requests.Session(); session.trust_env = False
        try:
            response = session.post(
                f"https://graph.facebook.com/{self.api_version}/{page_id}/videos",
                data={"file_url": media_url, "description": message, "access_token": credential.token},
                timeout=self.timeout_seconds,
                allow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "yingliang-fb-auto-post/1"},
            )
        except requests.RequestException:
            return GraphResult("unknown", error_code="fb_graph_network_outcome_unknown", message="Meta请求连接中断，发布结果待人工确认")
        finally:
            session.close()
        try:
            payload = response.json()
        except ValueError:
            return GraphResult("unknown", error_code="fb_graph_response_outcome_unknown", message="Meta响应无法确认，发布结果待人工确认")
        trace_id = str(response.headers.get("x-fb-trace-id") or "")[:128]
        post_id = str(payload.get("id") or "").strip() if isinstance(payload, Mapping) else ""
        # Any returned id is authoritative success evidence, even if the HTTP status is unusual.
        if post_id:
            return GraphResult("success", post_id=post_id, trace_id=trace_id)
        if 200 <= int(response.status_code) < 300:
            return GraphResult("unknown", error_code="fb_graph_id_missing", message="Meta成功响应缺少发布ID，结果待人工确认")
        code, message_text = self._safe_error(payload)
        if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
            trace_id = str(payload["error"].get("fbtrace_id") or trace_id)[:128]
        return GraphResult("definite_failure", error_code=code, message=message_text, trace_id=trace_id)

    def reconcile_video(self, object_id: str, credential: PageCredential) -> GraphResult:
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", str(object_id or "")):
            return GraphResult("definite_failure", error_code="fb_graph_object_id_invalid", message="Graph对象ID无效")
        session = requests.Session(); session.trust_env = False
        try:
            response = session.get(f"https://graph.facebook.com/{self.api_version}/{object_id}", params={"fields": "id,status", "access_token": credential.token}, timeout=self.timeout_seconds, allow_redirects=False, headers={"Accept": "application/json", "User-Agent": "yingliang-fb-auto-post/1"})
        except requests.RequestException:
            return GraphResult("unknown", post_id=object_id, error_code="fb_graph_reconcile_transient", message="Meta处理状态暂时无法确认")
        finally:
            session.close()
        try: payload = response.json()
        except ValueError: return GraphResult("unknown", post_id=object_id, error_code="fb_graph_reconcile_transient", message="Meta处理状态暂时无法确认")
        trace_id = str(response.headers.get("x-fb-trace-id") or "")[:128]
        if not 200 <= response.status_code < 300:
            code, message = self._safe_error(payload)
            graph_code = ""
            if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
                graph_code = str(payload["error"].get("code") or "")
                trace_id = str(payload["error"].get("fbtrace_id") or trace_id)[:128]
            error_subcode = ""
            if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
                error_subcode = str(payload["error"].get("error_subcode") or "")
            if graph_code in self._CREDENTIAL_ERROR_CODES or (graph_code == "100" and error_subcode == "33"):
                return GraphResult("credential_failure", post_id=object_id, error_code=code, message="当前授权被Meta明确拒绝对账，可尝试同Page的其他可用授权", trace_id=trace_id)
            return GraphResult("unknown", post_id=object_id, error_code=code, message="Meta对账响应无法明确判断，稍后重试", trace_id=trace_id)
        status = payload.get("status") if isinstance(payload, Mapping) else None
        flattened = json.dumps(status, ensure_ascii=True, sort_keys=True).lower() if status is not None else ""
        if any(marker in flattened for marker in ('"failed"', '"error"')):
            return GraphResult("definite_failure", post_id=object_id, error_code="fb_graph_video_processing_failed", message="Meta视频处理失败", trace_id=trace_id)
        if any(marker in flattened for marker in ('"ready"', '"published"')):
            return GraphResult("success", post_id=object_id, trace_id=trace_id)
        return GraphResult("unknown", post_id=object_id, error_code="fb_graph_video_processing", message="Meta视频仍在处理中", trace_id=trace_id)


class AutoPostExecutor:
    def __init__(self, store: FBAutoPostStore, pages: PagePoolRepository, graph: GraphTransport, *, live_enabled: bool = False, rng: random.Random | None = None, min_request_interval_seconds: float = 0.5):
        self.store, self.pages, self.graph = store, pages, graph
        self.live_enabled, self.rng = live_enabled is True, rng or random.SystemRandom()
        self.min_request_interval_seconds = max(0.0, min(float(min_request_interval_seconds), 10.0))
        self._rate_lock, self._last_request_at = threading.Lock(), 0.0

    def _rate_limit(self) -> None:
        with self._rate_lock:
            wait = self.min_request_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def execute_next(self, worker_id: str, lease_seconds: int = 1200) -> Dict[str, Any]:
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed", "claimed": False}
        task = self.store.claim_next(worker_id, lease_seconds)
        if task is None:
            return {"ok": True, "status": "no_pending", "claimed": False}
        if not task.get("prepared_media_url") or task.get("media_url") != task.get("prepared_media_url") or task.get("prepared_media_url") == task.get("source_media_url"):
            return self.store.complete_task(int(task["id"]), {"status": "failed", "error_code": "fb_auto_prepared_media_required", "error_message": "任务缺少独立GPU成片，已禁止发布", "definite_attempts": 0})
        credentials = self.pages.eligible_credentials(task["page_id"])
        self.rng.shuffle(credentials)
        if not credentials:
            outcome = {"status": "failed", "error_code": "fb_page_missing_eligible_token", "error_message": "执行时Page已无可用授权", "definite_attempts": 0}
            return self.store.complete_task(int(task["id"]), outcome)
        definite = 0
        last = GraphResult("definite_failure", error_code="fb_graph_all_tokens_rejected", message="同Page的可用授权均被Meta明确拒绝")
        for sequence, credential in enumerate(credentials, 1):
            self._rate_limit()
            result = self.graph.publish_video(task["page_id"], credential, task["media_url"], task["message_text"])
            if result.kind == "success":
                return self.store.complete_submitted_with_attempt(int(task["id"]), sequence, credential_id=credential.credential_id, fb_user_id=credential.fb_user_id, graph_post_id=result.post_id, trace_id=result.trace_id, definite_attempts=definite)
            if result.kind == "unknown":
                self.store.record_attempt(int(task["id"]), sequence, credential_id=credential.credential_id, fb_user_id=credential.fb_user_id, result_kind="unknown", error_code=result.error_code, trace_id=result.trace_id)
                return self.store.complete_task(int(task["id"]), {"status": "unknown", "error_code": result.error_code, "error_message": result.message, "definite_attempts": definite})
            definite += 1
            self.store.record_attempt(int(task["id"]), sequence, credential_id=credential.credential_id, fb_user_id=credential.fb_user_id, result_kind="definite_failure", error_code=result.error_code, trace_id=result.trace_id)
            last = result
        return self.store.complete_task(int(task["id"]), {"status": "failed", "error_code": last.error_code, "error_message": "同Page的所有可用授权均被Meta明确拒绝", "definite_attempts": definite})

    def reconcile_next(self, worker_id: str, lease_seconds: int = 1200) -> Dict[str, Any]:
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed", "claimed": False}
        task = self.store.claim_submitted(worker_id, lease_seconds)
        if task is None:
            return {"ok": True, "status": "no_submitted", "claimed": False}
        credentials = self.pages.eligible_credentials(task["page_id"])
        if not credentials:
            return self.store.reconcile_task(int(task["id"]), "submitted", error_code="fb_page_missing_eligible_token", error_message="暂无授权可查询Meta处理状态")
        self.rng.shuffle(credentials)
        rejected = 0
        for credential in credentials:
            self._rate_limit()
            result = self.graph.reconcile_video(task["graph_post_id"], credential)
            if result.kind == "success":
                return self.store.reconcile_task(int(task["id"]), "published")
            if result.kind == "definite_failure" and result.error_code == "fb_graph_video_processing_failed":
                return self.store.reconcile_task(int(task["id"]), "failed_without_retry", error_code=result.error_code, error_message=result.message)
            if result.kind == "credential_failure":
                rejected += 1
                continue
            if result.kind == "definite_failure":
                return self.store.reconcile_task(int(task["id"]), "unknown", error_code=result.error_code or "fb_graph_reconcile_unavailable", error_message=result.message or "Meta对象无法继续自动对账，请人工确认")
            return self.store.reconcile_task(int(task["id"]), "submitted", error_code=result.error_code or "fb_graph_reconcile_transient", error_message=result.message or "Meta处理状态暂时无法确认")
        if rejected == len(credentials):
            return self.store.reconcile_task(
                int(task["id"]),
                "unknown",
                error_code="fb_graph_reconcile_all_credentials_rejected",
                error_message="所有可用授权均被Meta明确拒绝对账，已停止自动处理，请人工确认",
            )
        return self.store.reconcile_task(int(task["id"]), "submitted", error_code="fb_graph_reconcile_transient", error_message="Meta处理状态暂时无法确认")


__all__ = ["AutoPostExecutor", "GraphResult", "RequestsGraphTransport"]

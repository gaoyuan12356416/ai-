"""Prepare-only client for the isolated FB Page random-overlay GPU service."""

from __future__ import annotations

import http.client
import json
import math
import re
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from .core import FBAutoPostStore, StoreError
from .validation import valid_internal_bearer


PROFILE = "tt-post-random-overlay-h264-720x1280-v3"


class GPUPrepareError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 502):
        self.code, self.status = code, status
        super().__init__(message)


def _https(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise GPUPrepareError("fb_auto_prepared_response_invalid", "GPU成片地址无效")
    return text


class GPUPrepareClient:
    def __init__(self, token: str, *, port: int = 18836, timeout: int = 9000, connection_factory=None):
        if not valid_internal_bearer(token) or port != 18836 or not 60 <= int(timeout) <= 10800:
            raise ValueError("invalid FB prepare-only GPU configuration")
        self._token, self.port, self.timeout, self._connection_factory = token, port, int(timeout), connection_factory

    def prepare(self, *, job_id: str, content_id: str, source_url: str, video_template: str) -> Dict[str, Any]:
        if not re.fullmatch(r"fb-page-[a-f0-9]{48}", job_id) or video_template != "random_overlay":
            raise GPUPrepareError("fb_auto_gpu_request_invalid", "视频制作请求无效", 400)
        source = _https(source_url)
        body = json.dumps({"job_id": job_id, "content_id": str(content_id), "source_url": source, "source_trim_tail_seconds": 0, "video_template": video_template, "expected_profile": PROFILE}, sort_keys=True, separators=(",", ":")).encode()
        connection = self._connection_factory("127.0.0.1", self.port, self.timeout) if self._connection_factory else http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request("POST", "/internal/fb-page-media/prepare", body=body, headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json", "Connection": "close"})
            response = connection.getresponse(); raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise GPUPrepareError("fb_auto_gpu_response_too_large", "GPU响应超过安全上限")
            try: decoded = json.loads(raw.decode()) if raw else {}
            except (UnicodeError, ValueError): raise GPUPrepareError("fb_auto_prepared_response_invalid", "GPU响应格式无效") from None
            if not 200 <= int(response.status) < 300:
                raise GPUPrepareError(str(decoded.get("code") or "fb_auto_gpu_prepare_failed")[:96], "GPU视频制作失败", int(response.status))
        except GPUPrepareError:
            raise
        except (OSError, http.client.HTTPException):
            raise GPUPrepareError("fb_auto_gpu_unavailable", "GPU视频制作服务暂不可用") from None
        finally:
            connection.close()
        item = decoded.get("item", decoded) if isinstance(decoded, Mapping) else {}
        if not isinstance(item, Mapping) or str(item.get("job_id") or "") != job_id or str(item.get("content_id") or "") != str(content_id) or str(item.get("profile") or "") != PROFILE:
            raise GPUPrepareError("fb_auto_prepared_identity_mismatch", "GPU成片身份或版本不一致")
        output = _https(item.get("output_url") or item.get("prepared_media_url"))
        sha = str(item.get("output_sha256") or "").lower()
        probe = item.get("probe") if isinstance(item.get("probe"), Mapping) else {}
        try: size, duration = int(item.get("output_size") or 0), float(probe.get("duration") or 0)
        except (TypeError, ValueError, OverflowError): size, duration = 0, 0
        if output == source or not re.fullmatch(r"[a-f0-9]{64}", sha) or size <= 0 or not math.isfinite(duration) or not 0 < duration <= 3600:
            raise GPUPrepareError("fb_auto_prepared_response_invalid", "GPU成片指纹或媒体元数据无效")
        return {"media_url": output, "sha256": sha, "size_bytes": size, "duration_seconds": duration, "profile": PROFILE}


class PrepareExecutor:
    def __init__(self, store: FBAutoPostStore, gpu: GPUPrepareClient, *, live_enabled: bool):
        self.store, self.gpu, self.live_enabled = store, gpu, live_enabled is True

    def prepare_next(self, worker_id: str, lease_seconds: int = 10200) -> Dict[str, Any]:
        if not self.live_enabled:
            return {"ok": True, "status": "live_gate_closed", "claimed": False}
        task = self.store.claim_prepare_next(worker_id, lease_seconds)
        if task is None:
            return {"ok": True, "status": "no_planned", "claimed": False}
        try:
            prepared = self.gpu.prepare(job_id=task["gpu_job_id"], content_id=task["content_id"], source_url=task["source_media_url"], video_template=task["video_template"])
            return self.store.complete_prepare(int(task["id"]), prepared)
        except GPUPrepareError as exc:
            permanent = {
                "fb_auto_gpu_request_invalid", "fb_auto_prepared_identity_mismatch",
                "fb_auto_prepared_response_invalid", "invalid_request",
                "invalid_configuration", "fb_gpu_job_conflict",
                "fb_gpu_output_contract_invalid",
            }
            if exc.code not in permanent and (exc.code == "fb_auto_gpu_unavailable" or exc.status >= 500):
                return self.store.defer_prepare(int(task["id"]), exc.code, str(exc), delay_seconds=300)
            return self.store.fail_prepare(int(task["id"]), exc.code, str(exc))


__all__ = ["GPUPrepareClient", "GPUPrepareError", "PROFILE", "PrepareExecutor"]

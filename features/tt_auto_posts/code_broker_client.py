"""Secret-safe client for the loopback automatic-post code broker."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

import requests

from .validation import valid_internal_bearer


DEFAULT_BROKER_URL = "http://127.0.0.1:18832"
MAX_RESPONSE_BYTES = 64 * 1024
_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")


class AutoCodeBrokerClientError(RuntimeError):
    def __init__(self, code: Any, message: Any, status: int = 503):
        normalized = str(code or "tt_auto_code_service_unavailable")
        self.code = (
            normalized
            if re.fullmatch(r"[a-z0-9_]{1,96}", normalized)
            else "tt_auto_code_service_unavailable"
        )
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 503
        text = str(message or "").strip()
        super().__init__(text[:500] if text else "自动发布四位码服务暂不可用")


class AutoCodeBrokerClient:
    def __init__(
        self,
        service_url: Any,
        internal_token: Any,
        *,
        timeout_seconds: float = 5.0,
        session: Any = requests,
    ):
        url = str(service_url or "").strip().rstrip("/")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            port = None
        token = str(internal_token or "")
        if (
            url != DEFAULT_BROKER_URL
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port != 18832
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not valid_internal_bearer(token)
        ):
            raise AutoCodeBrokerClientError(
                "tt_auto_code_service_not_configured",
                "自动发布四位码服务配置无效",
                500,
            )
        self.service_url = url
        self.internal_token = token
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        self.session = session

    def _post(self, path: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            response = self.session.post(
                self.service_url + path,
                headers={
                    "X-Internal-Token": self.internal_token,
                    "Content-Type": "application/json",
                },
                json=dict(payload),
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise AutoCodeBrokerClientError(
                "tt_auto_code_service_unavailable",
                "自动发布四位码服务暂不可用",
                503,
            ) from None
        if response.is_redirect or len(response.content) > MAX_RESPONSE_BYTES:
            raise AutoCodeBrokerClientError(
                "tt_auto_code_response_invalid", "自动发布四位码服务响应无效", 502
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, Mapping):
            raise AutoCodeBrokerClientError(
                "tt_auto_code_response_invalid", "自动发布四位码服务响应无效", 502
            )
        if response.status_code != 200 or body.get("ok") is not True:
            raise AutoCodeBrokerClientError(
                body.get("error"),
                body.get("message"),
                int(response.status_code),
            )
        route = body.get("route")
        if not isinstance(route, Mapping):
            raise AutoCodeBrokerClientError(
                "tt_auto_code_response_invalid", "自动发布四位码服务响应无效", 502
            )
        return dict(route)

    def freeze_route(
        self,
        task_id: Any,
        *,
        content_id: Any,
        long_url: Any,
        created_at: Any,
    ) -> str:
        route = self._post(
            "/internal/tt-auto-code-routes/freeze",
            {
                "task_id": task_id,
                "content_id": content_id,
                "long_url": long_url,
                "created_at": created_at,
            },
        )
        code = str(route.get("code") or "")
        if int(route.get("task_id") or 0) != int(task_id) or not _CODE_RE.fullmatch(code):
            raise AutoCodeBrokerClientError(
                "tt_auto_code_response_invalid", "自动发布四位码服务响应无效", 502
            )
        return code

    def set_state(self, task_id: Any, *, state: Any, updated_at: Any) -> Dict[str, Any]:
        return self._post(
            "/internal/tt-auto-code-routes/%d/state" % int(task_id),
            {"state": state, "updated_at": updated_at},
        )


__all__ = [
    "AutoCodeBrokerClient",
    "AutoCodeBrokerClientError",
    "DEFAULT_BROKER_URL",
]

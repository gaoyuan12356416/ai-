"""Narrow loopback bridge from the X auto control plane to the X publisher.

The bridge deliberately exposes only safe account facts, material occupancy,
and source-gated ``auto_template`` execution envelopes.  It never reads X
credentials and it never falls back to the human-manual endpoints.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import requests

from .validation import valid_internal_bearer


DEFAULT_X_POST_INTERNAL_URL = "http://127.0.0.1:8810"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,96}$")
_MATERIAL_ID = re.compile(r"^[1-9][0-9]{0,18}$")


class XPostBridgeError(RuntimeError):
    def __init__(
        self,
        code: Any,
        message: Any,
        status: int = 503,
        *,
        unknown_outcome: bool = False,
    ):
        normalized = str(code or "x_auto_x_bridge_failed").strip().lower()
        self.code = (
            normalized
            if _SAFE_ERROR_CODE.fullmatch(normalized)
            else "x_auto_x_bridge_failed"
        )
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 503
        self.unknown_outcome = bool(unknown_outcome)
        super().__init__(_safe_message(message))


def _safe_message(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if (
        not text
        or len(text) > 500
        or any(
            marker in lowered
            for marker in (
                "access_token",
                "refresh_token",
                "authorization:",
                "bearer ",
                "client_secret",
                "internal_token",
            )
        )
        or re.search(r"[A-Za-z0-9_-]{80,}", text)
    ):
        return "X publishing bridge request failed"
    return text


def _canonical_material_ids(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not _MATERIAL_ID.fullmatch(text):
            raise XPostBridgeError("invalid_request", "material ID is invalid", 400)
        normalized = str(int(text))
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    if len(result) > 1000:
        raise XPostBridgeError("invalid_request", "too many material IDs", 400)
    return result


class XPostAutoBridgeClient:
    """Strict client for the dedicated X ``auto-template`` internal routes."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int = 120,
        publish_timeout: int = 9000,
        session: Optional[requests.Session] = None,
    ):
        normalized_url = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            normalized_url != DEFAULT_X_POST_INTERNAL_URL
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port != 8810
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise XPostBridgeError(
                "x_auto_x_bridge_url_invalid",
                "X publishing bridge must use the fixed loopback endpoint",
                500,
            )
        if not valid_internal_bearer(token):
            raise XPostBridgeError(
                "x_auto_x_bridge_token_invalid",
                "X publishing bridge bearer is invalid",
                500,
            )
        try:
            normalized_timeout = int(timeout)
        except (TypeError, ValueError, OverflowError):
            normalized_timeout = 0
        if not 5 <= normalized_timeout <= 600:
            raise XPostBridgeError(
                "x_auto_x_bridge_timeout_invalid",
                "X publishing bridge timeout is invalid",
                500,
            )
        try:
            normalized_publish_timeout = int(publish_timeout)
        except (TypeError, ValueError, OverflowError):
            normalized_publish_timeout = 0
        if not 600 <= normalized_publish_timeout <= 10200:
            raise XPostBridgeError(
                "x_auto_x_bridge_timeout_invalid",
                "X publishing timeout is invalid",
                500,
            )
        self.base_url = normalized_url
        self._token = str(token)
        self.timeout = normalized_timeout
        self.publish_timeout = normalized_publish_timeout
        self._session = session

    def _post(
        self,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        unknown_on_transport: bool = False,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_path = str(path or "")
        if not re.fullmatch(r"/internal/posts/auto-template/[a-z0-9_./-]+", normalized_path):
            raise XPostBridgeError(
                "x_auto_x_bridge_route_invalid",
                "X publishing bridge route is invalid",
                500,
            )
        body = dict(payload or {})
        session = self._session or requests.Session()
        owned = self._session is None
        if owned:
            session.trust_env = False
        try:
            response = session.post(
                self.base_url + normalized_path,
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=body,
                timeout=self.timeout if timeout is None else int(timeout),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise XPostBridgeError(
                "x_auto_x_bridge_unavailable",
                "X publishing bridge is unavailable",
                503,
                unknown_outcome=unknown_on_transport,
            ) from None
        finally:
            if owned:
                session.close()
        raw = bytes(response.content or b"")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise XPostBridgeError(
                "x_auto_x_bridge_response_too_large",
                "X publishing bridge response is too large",
                502,
                unknown_outcome=unknown_on_transport,
            )
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid",
                "X publishing bridge returned invalid JSON",
                502,
                unknown_outcome=unknown_on_transport,
            ) from None
        if not isinstance(decoded, Mapping):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid",
                "X publishing bridge returned an invalid object",
                502,
                unknown_outcome=unknown_on_transport,
            )
        if not 200 <= int(response.status_code) < 300:
            raise XPostBridgeError(
                decoded.get("code") or decoded.get("error"),
                decoded.get("message") or decoded.get("error_message"),
                int(response.status_code),
                unknown_outcome=bool(decoded.get("unknown_outcome")),
            )
        item = decoded.get("item")
        if item is not None and not isinstance(item, Mapping):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid",
                "X publishing bridge item is invalid",
                502,
                unknown_outcome=unknown_on_transport,
            )
        return dict(decoded)

    def accounts(self) -> list[Dict[str, Any]]:
        result = self._post("/internal/posts/auto-template/accounts")
        items = result.get("items")
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid", "X account list is invalid", 502
            )
        return [dict(item) for item in items]

    def verify_account(
        self,
        account_id: Any,
        *,
        only_refresh_required: bool = False,
        preserve_transient_status: bool = False,
        require_publish_approved: bool = False,
    ) -> Dict[str, Any]:
        account = int(account_id)
        if account <= 0:
            raise XPostBridgeError("invalid_request", "account ID is invalid", 400)
        result = self._post(
            f"/internal/posts/auto-template/accounts/{account}/verify",
            {
                "only_refresh_required": bool(only_refresh_required),
                "preserve_transient_status": bool(preserve_transient_status),
                "require_publish_approved": bool(require_publish_approved),
            }
            if any(
                (
                    only_refresh_required,
                    preserve_transient_status,
                    require_publish_approved,
                )
            )
            else {},
        )
        return dict(result.get("item") or {})

    def unavailable_material_ids(self, material_ids: Sequence[Any]) -> list[str]:
        normalized = _canonical_material_ids(material_ids)
        if not normalized:
            return []
        result = self._post(
            "/internal/posts/auto-template/material-keys/query",
            {"material_keys": normalized},
        )
        item = dict(result.get("item") or {})
        returned = item.get("material_keys")
        if not isinstance(returned, list):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid", "material occupancy is invalid", 502
            )
        unavailable = set(_canonical_material_ids(returned))
        return [value for value in normalized if value in unavailable]

    def create_run(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        result = self._post(
            "/internal/posts/auto-template/runs/create",
            payload,
            unknown_on_transport=True,
        )
        return dict(result.get("item") or {})

    def query_run(self, run_id: Any) -> Dict[str, Any]:
        run = int(run_id)
        if run <= 0:
            raise XPostBridgeError("invalid_request", "execution run ID is invalid", 400)
        result = self._post(
            f"/internal/posts/auto-template/runs/{run}/query"
        )
        return dict(result.get("item") or {})

    def recover_run(self, run_id: Any) -> Dict[str, Any]:
        """Recover only one exact stranded auto run; never claim or republish."""

        run = int(run_id)
        if run <= 0:
            raise XPostBridgeError("invalid_request", "execution run ID is invalid", 400)
        result = self._post(
            f"/internal/posts/auto-template/runs/{run}/recover"
        )
        item = result.get("item")
        if (
            not isinstance(item, Mapping)
            or type(item.get("busy")) is not bool
            or type(item.get("recovered")) is not bool
            or not isinstance(item.get("run"), Mapping)
        ):
            raise XPostBridgeError(
                "x_auto_x_bridge_response_invalid",
                "exact X recovery response is invalid",
                502,
            )
        return {
            "busy": item["busy"],
            "recovered": item["recovered"],
            "run": dict(item["run"]),
        }

    def create_plan(self, run_id: Any, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        result = self._post(
            "/internal/posts/auto-template/plan",
            {"run_id": int(run_id), "candidates": [dict(candidate)]},
            unknown_on_transport=True,
        )
        return dict(result.get("item") or {})

    def record_failure(self, run_id: Any, code: Any, message: Any) -> Dict[str, Any]:
        result = self._post(
            "/internal/posts/auto-template/runs/record-failure",
            {
                "run_id": int(run_id),
                "error_code": str(code or "x_auto_execution_failed")[:64],
                "error_message": _safe_message(message)[:240],
            },
            unknown_on_transport=True,
        )
        return dict(result.get("item") or {})

    def publish_queue(self, queue_id: Any) -> Dict[str, Any]:
        queue = int(queue_id)
        if queue <= 0:
            raise XPostBridgeError("invalid_request", "queue ID is invalid", 400)
        result = self._post(
            f"/internal/posts/auto-template/queue/{queue}/publish",
            unknown_on_transport=True,
            timeout=self.publish_timeout,
        )
        return dict(result.get("item") or {})

    def storage_preflight(self) -> Dict[str, Any]:
        result = self._post("/internal/posts/auto-template/storage/preflight")
        return dict(result.get("item") or {})


class XPostMaterialHistory:
    """Selector adapter backed by canonical X queue/pool occupancy."""

    def __init__(self, client: XPostAutoBridgeClient):
        self.client = client

    def seen_material_ids(self, material_ids: Sequence[str]) -> Iterable[str]:
        return self.client.unavailable_material_ids(material_ids)


__all__ = [
    "DEFAULT_X_POST_INTERNAL_URL",
    "XPostAutoBridgeClient",
    "XPostBridgeError",
    "XPostMaterialHistory",
]

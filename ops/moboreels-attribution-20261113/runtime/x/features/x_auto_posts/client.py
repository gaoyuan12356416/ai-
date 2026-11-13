"""Secret-safe main-API client for the loopback X auto-post sidecar."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

import requests

from .validation import valid_internal_bearer


X_AUTO_ADMIN_PREFIX = "/api/admin/x-auto-publish"
DEFAULT_SERVICE_URL = "http://127.0.0.1:18833"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_SENSITIVE_KEYS = {
    "accesstoken",
    "refreshtoken",
    "authorization",
    "credential",
    "credentials",
    "claimtoken",
    "internaltoken",
    "clientsecret",
    "password",
}

_GET_ROUTES = {
    f"{X_AUTO_ADMIN_PREFIX}/accounts": set(),
    f"{X_AUTO_ADMIN_PREFIX}/templates": {
        "status",
        "q",
        "limit",
        "offset",
    },
    f"{X_AUTO_ADMIN_PREFIX}/runs": {
        "template_id",
        "trigger_type",
        "status",
        "from",
        "to",
        "limit",
        "offset",
    },
}


class XAutoPostAdminClientError(RuntimeError):
    """Public-safe error raised by the loopback client."""

    def __init__(self, code: Any, message: Any, status: int = 503):
        normalized = str(code or "x_auto_post_service_unavailable")
        self.code = (
            normalized
            if re.fullmatch(r"[a-z0-9_]{1,80}", normalized)
            else "x_auto_post_service_unavailable"
        )
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 503
        super().__init__(_safe_message(message))


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _normalized_key(key) in _SENSITIVE_KEYS
            or contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_key(item) for item in value)
    return False


def _safe_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        return "X Post 自动发布服务暂不可用"
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in (
            "access_token",
            "access token",
            "refresh_token",
            "authorization:",
            "bearer ",
            "client_secret",
            "claim_token",
        )
    ) or re.search(
        r"(?:[A-Za-z0-9_-]{64,}|(?:[A-Za-z0-9_-]+\.){2}[A-Za-z0-9_-]+)",
        text,
    ):
        return "X Post 自动发布服务请求失败"
    return text


def safe_public_message(value: Any) -> str:
    """Return a bounded public error message without credential-like text."""

    return _safe_message(value)


def _public_payload(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or contains_sensitive_key(value):
        raise XAutoPostAdminClientError(
            "x_auto_post_unsafe_response",
            "X Post 自动发布服务返回了非公开字段",
            502,
        )
    return dict(value)


def _route_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in _GET_ROUTES or bool(
            re.fullmatch(
                rf"{re.escape(X_AUTO_ADMIN_PREFIX)}/(?:templates|runs)/[1-9][0-9]*",
                path,
            )
        )
    if method != "POST":
        return False
    if path == f"{X_AUTO_ADMIN_PREFIX}/templates":
        return True
    if re.fullmatch(
        rf"{re.escape(X_AUTO_ADMIN_PREFIX)}/accounts/[1-9][0-9]*/verify",
        path,
    ):
        return True
    return bool(
        re.fullmatch(
            rf"{re.escape(X_AUTO_ADMIN_PREFIX)}/templates/[1-9][0-9]*(?:/(?:copy|enable|disable|preview|run-now))?",
            path,
        )
    )


def parse_admin_query(path: str, raw_query: Any) -> Dict[str, str]:
    if path in _GET_ROUTES:
        allowed = _GET_ROUTES[path]
    elif re.fullmatch(
        rf"{re.escape(X_AUTO_ADMIN_PREFIX)}/(?:templates|runs)/[1-9][0-9]*",
        path,
    ):
        allowed = set()
    else:
        raise XAutoPostAdminClientError(
            "x_auto_post_route_not_allowed",
            "X Post 自动发布服务路由无效",
            500,
        )
    parsed = parse_qs(str(raw_query or ""), keep_blank_values=True)
    if set(parsed) - allowed:
        raise XAutoPostAdminClientError("invalid_request", "查询参数无效", 400)
    result: Dict[str, str] = {}
    for key, values in parsed.items():
        if len(values) != 1 or not str(values[0]).strip():
            raise XAutoPostAdminClientError("invalid_request", "查询参数无效", 400)
        result[key] = str(values[0]).strip()
    return result


def request_admin(
    method: Any,
    path: Any,
    *,
    payload: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    source = os.environ if environ is None else environ
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    if not _route_allowed(normalized_method, normalized_path):
        raise XAutoPostAdminClientError(
            "x_auto_post_route_not_allowed",
            "X Post 自动发布服务路由无效",
            500,
        )

    service_url = str(
        source.get("X_AUTO_POST_ADMIN_SERVICE_URL", DEFAULT_SERVICE_URL) or ""
    ).strip().rstrip("/")
    parsed_base = urlsplit(service_url)
    try:
        port = parsed_base.port
    except ValueError:
        port = None
    token = str(source.get("X_AUTO_POST_INTERNAL_TOKEN", "") or "")
    if (
        service_url != DEFAULT_SERVICE_URL
        or parsed_base.scheme != "http"
        or parsed_base.hostname != "127.0.0.1"
        or port != 18833
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.path not in ("", "/")
        or parsed_base.query
        or parsed_base.fragment
        or not valid_internal_bearer(token)
    ):
        raise XAutoPostAdminClientError(
            "x_auto_post_service_not_configured",
            "X Post 自动发布服务尚未完成内部配置",
            503,
        )
    if payload is not None and (
        not isinstance(payload, Mapping) or contains_sensitive_key(payload)
    ):
        raise XAutoPostAdminClientError("invalid_request", "请求包含无效字段", 400)
    allowed_query = _GET_ROUTES.get(normalized_path, set()) if normalized_method == "GET" else set()
    if set(dict(query or {})) - allowed_query:
        raise XAutoPostAdminClientError("invalid_request", "查询参数无效", 400)
    safe_query: Dict[str, str] = {}
    for key, value in dict(query or {}).items():
        normalized_key = str(key or "").strip()
        if (
            not normalized_key
            or isinstance(value, (dict, list, tuple))
            or contains_sensitive_key({normalized_key: value})
        ):
            raise XAutoPostAdminClientError("invalid_request", "查询参数无效", 400)
        safe_query[normalized_key] = str(value)
    try:
        timeout = int(source.get("X_AUTO_POST_ADMIN_TIMEOUT", "120") or "120")
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(5, min(timeout, 600))
    if normalized_path.endswith("/preview"):
        timeout = min(timeout, 120)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.request(
            normalized_method,
            service_url + normalized_path,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=UTF-8",
            },
            params=safe_query or None,
            json=dict(payload) if payload is not None else None,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise XAutoPostAdminClientError(
            "x_auto_post_service_unavailable",
            "X Post 自动发布服务暂不可用",
            503,
        ) from None
    finally:
        session.close()
    content = bytes(response.content or b"")
    if len(content) > MAX_RESPONSE_BYTES:
        raise XAutoPostAdminClientError(
            "x_auto_post_response_too_large",
            "X Post 自动发布服务响应超过安全上限",
            502,
        )
    try:
        decoded = json.loads(content.decode("utf-8")) if content else {}
    except (UnicodeError, ValueError):
        raise XAutoPostAdminClientError(
            "x_auto_post_invalid_response",
            "X Post 自动发布服务响应无效",
            502,
        ) from None
    if not isinstance(decoded, Mapping):
        raise XAutoPostAdminClientError(
            "x_auto_post_invalid_response",
            "X Post 自动发布服务响应无效",
            502,
        )
    if not 200 <= int(response.status_code) < 300:
        code = str(decoded.get("code") or decoded.get("error") or "")
        raise XAutoPostAdminClientError(
            code or "x_auto_post_service_error",
            decoded.get("message") or decoded.get("error_message"),
            int(response.status_code),
        )
    return _public_payload(decoded)


def error_payload(exc: BaseException) -> tuple[int, Dict[str, Any]]:
    code = str(
        getattr(exc, "code", "x_auto_post_service_unavailable")
        or "x_auto_post_service_unavailable"
    )
    if not re.fullmatch(r"[a-z0-9_]{1,80}", code):
        code = "x_auto_post_service_unavailable"
    status = getattr(exc, "status", 503)
    if not isinstance(status, int) or not 400 <= status <= 599:
        status = 503
    return status, {"ok": False, "error": code, "code": code, "message": _safe_message(exc)}


__all__ = [
    "X_AUTO_ADMIN_PREFIX",
    "XAutoPostAdminClientError",
    "contains_sensitive_key",
    "error_payload",
    "parse_admin_query",
    "request_admin",
    "safe_public_message",
]

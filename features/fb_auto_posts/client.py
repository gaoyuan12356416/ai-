"""Secret-safe main API client for the loopback FB auto-post sidecar."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

import requests

from .validation import valid_internal_bearer


FB_AUTO_ADMIN_PREFIX = "/api/admin/fb-auto-publish"
DEFAULT_SERVICE_URL = "http://127.0.0.1:18835"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SENSITIVE = {"accesstoken", "pageaccesstoken", "authorization", "credential", "credentials", "internaltoken", "password", "token"}


class FBAutoPostAdminClientError(RuntimeError):
    def __init__(self, code: Any, message: Any, status: int = 503, conflicts: Any = None):
        normalized = str(code or "fb_auto_post_service_unavailable")
        self.code = normalized if re.fullmatch(r"[a-z0-9_]{1,80}", normalized) else "fb_auto_post_service_unavailable"
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 503
        self.conflicts = conflicts if isinstance(conflicts, list) else []
        super().__init__(safe_message(message))


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_key(k) in SENSITIVE or contains_sensitive_key(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_key(item) for item in value)
    return False


def safe_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500 or re.search(r"(?i)(access.?token|bearer |password|[A-Za-z0-9_-]{64,})", text):
        return "FB Page自动发布服务请求失败"
    return text


GET_ROUTES = {
    f"{FB_AUTO_ADMIN_PREFIX}/groups": set(),
    f"{FB_AUTO_ADMIN_PREFIX}/templates": {"status", "q", "limit", "offset"},
    f"{FB_AUTO_ADMIN_PREFIX}/runs": {"limit", "offset"},
}


def route_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in GET_ROUTES or bool(re.fullmatch(re.escape(FB_AUTO_ADMIN_PREFIX) + r"/(?:templates|runs)/[1-9][0-9]*", path))
    return method == "POST" and bool(re.fullmatch(re.escape(FB_AUTO_ADMIN_PREFIX) + r"/templates(?:/[1-9][0-9]*(?:/(?:enable|disable|run-now))?)?", path))


def parse_admin_query(path: str, raw_query: Any) -> Dict[str, str]:
    allowed = GET_ROUTES.get(path)
    if allowed is None:
        if re.fullmatch(re.escape(FB_AUTO_ADMIN_PREFIX) + r"/(?:templates|runs)/[1-9][0-9]*", path):
            allowed = set()
        else:
            raise FBAutoPostAdminClientError("fb_auto_post_route_not_allowed", "FB自动发布路由无效", 500)
    parsed = parse_qs(str(raw_query or ""), keep_blank_values=True)
    if set(parsed) - allowed or any(len(values) != 1 for values in parsed.values()):
        raise FBAutoPostAdminClientError("invalid_request", "查询参数无效", 400)
    return {key: str(values[0]).strip() for key, values in parsed.items()}


def request_admin(method: Any, path: Any, *, payload: Optional[Mapping[str, Any]] = None, query: Optional[Mapping[str, Any]] = None, actor: Optional[Mapping[str, Any]] = None, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    source = os.environ if environ is None else environ
    method, path = str(method or "").upper(), str(path or "")
    if not route_allowed(method, path):
        raise FBAutoPostAdminClientError("fb_auto_post_route_not_allowed", "FB自动发布路由无效", 500)
    base = str(source.get("FB_AUTO_POST_ADMIN_SERVICE_URL", DEFAULT_SERVICE_URL) or "").rstrip("/")
    parsed = urlsplit(base)
    try: port = parsed.port
    except ValueError: port = None
    bearer = str(source.get("FB_AUTO_POST_INTERNAL_TOKEN", "") or "")
    if base != DEFAULT_SERVICE_URL or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port != 18835 or parsed.path not in ("", "/") or parsed.username or parsed.password or parsed.query or parsed.fragment or not valid_internal_bearer(bearer):
        raise FBAutoPostAdminClientError("fb_auto_post_service_not_configured", "FB自动发布服务尚未完成内部配置", 503)
    if payload is not None and (not isinstance(payload, Mapping) or contains_sensitive_key(payload)):
        raise FBAutoPostAdminClientError("invalid_request", "请求包含无效字段", 400)
    if actor is not None and (not isinstance(actor, Mapping) or contains_sensitive_key(actor)):
        raise FBAutoPostAdminClientError("invalid_request", "操作人范围无效", 400)
    headers = {"Authorization": "Bearer " + bearer, "Accept": "application/json", "Content-Type": "application/json; charset=UTF-8"}
    if actor is not None:
        headers["X-FB-Auto-Actor"] = json.dumps(dict(actor), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    session = requests.Session(); session.trust_env = False
    timeout = 30 if path.endswith("/run-now") else (900 if path.endswith("/enable") else 120)
    try:
        response = session.request(method, base + path, params=dict(query or {}) or None, json=dict(payload) if payload is not None else None, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        raise FBAutoPostAdminClientError("fb_auto_post_service_unavailable", "FB Page自动发布服务暂不可用", 503) from None
    finally:
        session.close()
    raw = bytes(response.content or b"")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FBAutoPostAdminClientError("fb_auto_post_response_too_large", "服务响应超过安全上限", 502)
    try: decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, ValueError):
        raise FBAutoPostAdminClientError("fb_auto_post_invalid_response", "服务响应无效", 502) from None
    if not isinstance(decoded, Mapping) or contains_sensitive_key(decoded):
        raise FBAutoPostAdminClientError("fb_auto_post_unsafe_response", "服务返回了非公开字段", 502)
    if not 200 <= response.status_code < 300:
        raise FBAutoPostAdminClientError(decoded.get("code") or decoded.get("error"), decoded.get("message"), response.status_code, decoded.get("conflicts"))
    return dict(decoded)


def error_payload(exc: BaseException) -> tuple[int, Dict[str, Any]]:
    status = getattr(exc, "status", 503)
    code = str(getattr(exc, "code", "fb_auto_post_service_unavailable"))
    payload: Dict[str, Any] = {"ok": False, "error": code, "code": code, "message": safe_message(exc)}
    conflicts = getattr(exc, "conflicts", [])
    if conflicts and not contains_sensitive_key(conflicts): payload["conflicts"] = conflicts
    return status if isinstance(status, int) and 400 <= status <= 599 else 503, payload


__all__ = ["FB_AUTO_ADMIN_PREFIX", "FBAutoPostAdminClientError", "error_payload", "parse_admin_query", "request_admin"]

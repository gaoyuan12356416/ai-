"""Loopback client used by the AI backend to call the X OAuth sidecar."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


SAFE_ERROR_CODES = {
    "invalid_request",
    "x_account_not_found",
    "x_accounts_unavailable",
    "x_identity_mismatch",
    "x_internal_auth_failed",
    "x_oauth_not_configured",
    "x_token_missing",
    "x_token_revoked",
    "x_upstream_error",
}


class XAccountsClientError(RuntimeError):
    def __init__(self, code="x_accounts_unavailable", message="X账号服务暂不可用", status_code=503):
        safe_code = str(code or "x_accounts_unavailable")
        if safe_code not in SAFE_ERROR_CODES:
            safe_code = "x_accounts_unavailable"
        super().__init__(str(message or "X账号服务暂不可用"))
        self.code = safe_code
        self.status_code = int(status_code or 503)


_BASE_URL = "http://127.0.0.1:8810"
_INTERNAL_TOKEN = ""
_TIMEOUT = 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def configure_x_accounts_client(base_url, internal_token, timeout=30):
    global _BASE_URL, _INTERNAL_TOKEN, _TIMEOUT
    parsed = urllib.parse.urlsplit(str(base_url or "").rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid X sidecar base URL")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("X sidecar base URL must use loopback")
    _BASE_URL = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    _INTERNAL_TOKEN = str(internal_token or "")
    try:
        parsed_timeout = int(timeout)
    except (TypeError, ValueError):
        parsed_timeout = 30
    _TIMEOUT = max(1, min(parsed_timeout, 120))


def _request(path, method="GET", payload=None):
    if not _INTERNAL_TOKEN:
        raise XAccountsClientError("x_accounts_unavailable", "X账号服务内部鉴权未配置", 503)
    body = None
    headers = {
        "Authorization": "Bearer " + _INTERNAL_TOKEN,
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(_BASE_URL + path, data=body, method=method, headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=_TIMEOUT) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise XAccountsClientError("x_accounts_unavailable", "X账号服务响应过大", 502)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except XAccountsClientError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(64 * 1024)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}
        finally:
            exc.close()
        code = str(data.get("error", "x_accounts_unavailable") or "x_accounts_unavailable")
        if code not in SAFE_ERROR_CODES:
            code = "x_accounts_unavailable"
        message = str(data.get("message", "") or "X账号服务请求失败")
        status = exc.code if exc.code in {400, 404, 409, 429, 502, 503} else 503
        raise XAccountsClientError(code, message, status) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise XAccountsClientError("x_accounts_unavailable", "X账号服务暂不可用", 503) from None


def get_x_accounts_config():
    return _request("/internal/config")


def list_x_accounts():
    return _request("/internal/accounts")


def start_x_authorization(actor):
    safe_actor = {
        "user_id": str((actor or {}).get("user_id", "") or "")[:255],
        "name": str((actor or {}).get("name", "") or "")[:255],
        "email": str((actor or {}).get("email", "") or "")[:255],
        "role": str((actor or {}).get("role", "user") or "user")[:32],
    }
    return _request("/internal/authorize", method="POST", payload={"actor": safe_actor})


def verify_x_account(account_id):
    account_id = str(account_id or "")
    if not account_id.isdigit():
        raise XAccountsClientError("invalid_request", "X账号记录ID无效", 400)
    return _request("/internal/accounts/%s/verify" % account_id, method="POST", payload={})

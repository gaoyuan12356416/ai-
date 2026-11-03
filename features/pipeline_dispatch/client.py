"""Authenticated coordinator client. Mutations are never retried implicitly."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class CoordinatorError(RuntimeError):
    def __init__(self, code="coordinator_unavailable", status=503):
        self.code, self.status = code, status
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CoordinatorClient:
    PREFIX = "/internal/pipeline/"
    ROUTES = frozenset({"nodes/enroll", "nodes/register", "workers/heartbeat", "jobs/claim", "jobs/reserve", "jobs/heartbeat", "jobs/begin-operation", "jobs/checkpoint", "jobs/complete", "jobs/fail", "workers/drain"})

    def __init__(self, base_url, token, *, timeout=10, opener=None):
        parsed = urlsplit(str(base_url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("invalid coordinator origin")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote coordinator requires TLS or an SSH loopback tunnel")
        if len(str(token or "")) < 32:
            raise ValueError("node authentication token is not configured")
        self.base_url, self.token, self.timeout = str(base_url).rstrip("/"), str(token), max(1, min(int(timeout), 30))
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def post(self, route, payload):
        route = str(route).removeprefix(self.PREFIX)
        if route not in self.ROUTES:
            raise ValueError("coordinator route is not allowed")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        request = urllib.request.Request(self.base_url + self.PREFIX + route, data=body, method="POST", headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=max(self.timeout, 120) if route in {"jobs/reserve", "jobs/begin-operation", "jobs/checkpoint", "jobs/complete", "jobs/fail"} else self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise CoordinatorError("coordinator_response_too_large", 502)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise CoordinatorError("coordinator_response_invalid", 502)
                return result
        except urllib.error.HTTPError as exc:
            code = "coordinator_http_" + str(exc.code)
            try:
                error = json.loads(exc.read(65536))
                candidate = error.get("code") or error.get("error")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,100}", candidate):
                    code = candidate
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
            raise CoordinatorError(code, exc.code) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise CoordinatorError() from None

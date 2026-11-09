"""Fixed loopback JSON transport, without proxies, redirects or credential logging."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class Error(Exception):
    def __init__(self, code, message='', status=503):
        self.code, self.status = code, status
        super().__init__(message or code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def rpc(base, path, token='', *, payload=None, method=None, cookie='', timeout=30):
    if base not in {'http://127.0.0.1:8810', 'http://127.0.0.1:8787', 'http://127.0.0.1:8821',
                    'http://127.0.0.1:18843', 'http://127.0.0.1:8898'} or not path.startswith('/') or path.startswith('//'):
        raise ValueError('invalid_loopback_route')
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if cookie:
        headers['Cookie'] = cookie
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload, ensure_ascii=False).encode()
    req = Request(base + path, data=body, headers=headers, method=method or ('POST' if body is not None else 'GET'))
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except HTTPError as exc:
        try:
            data = json.loads(exc.read(65536))
        except (ValueError, OSError):
            data = {}
        code = str(data.get('error') or data.get('code') or 'service_error')[:96]
        message = str(data.get('message') or code)[:500]
        raise Error(code, message, exc.code) from None
    except (OSError, URLError, ValueError):
        raise Error('service_unavailable', '服务暂不可用，请稍后重试', 503) from None

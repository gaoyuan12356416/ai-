"""Bounded, redirect-free loopback calls. Never serialize credentials."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from .errors import PublishingError

MAX_BYTES = 2 * 1024 * 1024

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def request(url, token, payload, *, timeout=90):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or len(token) < 32):
        raise PublishingError('service_not_configured', '发布服务内部连接未配置', 503)
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(body) > MAX_BYTES:
        raise PublishingError('request_too_large', '请求超过 2 MiB', 413)
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with OPENER.open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError()
            result = json.loads(raw.decode('utf-8'))
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read(65536).decode('utf-8'))
            code = data.get('code') or data.get('error') or 'service_unavailable'
            message = data.get('message') or '发布服务暂不可用'
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code):
                raise ValueError()
            status = exc.code if 400 <= exc.code <= 599 else 503
        except (ValueError, TypeError, UnicodeError, AttributeError):
            code, message, status = 'service_unavailable', '发布服务暂不可用', 503
        finally:
            exc.close()
        raise PublishingError(code, str(message)[:400], status,
                              accepted=False if status < 500 else None) from None
    except (ValueError, TypeError, OSError, urllib.error.URLError):
        raise PublishingError('service_unavailable', '发布服务暂不可用，请保留原操作并查询结果', 503, accepted=None) from None

def sidecar(method, path, actor, payload=None, query=None):
    from features.x_accounts import client
    return request(client._BASE_URL + '/internal/video-publish/dispatch', client._INTERNAL_TOKEN,
                   {'method': method, 'path': path, 'actor': actor, 'payload': payload or {}, 'query': query or {}})

def broker(kind, payload, token):
    if kind not in {'identity', 'source'}:
        raise PublishingError('invalid_request', '内部请求无效', 400)
    url = 'http://127.0.0.1:8787/internal/x-video-publish/' + kind
    if kind != 'identity':
        return request(url, token, payload)
    # This endpoint only reads the current owner/permission record. Retrying it
    # cannot duplicate a publication. Never apply this policy to dispatch,
    # credential permits, source operations, or any platform write.
    deadline = time.monotonic() + 12
    delays = (0.25, 0.5, 1, 2, 3)
    for attempt in range(len(delays) + 1):
        try:
            return request(url, token, payload, timeout=min(3, max(.1, deadline - time.monotonic())))
        except PublishingError as exc:
            remaining = deadline - time.monotonic()
            if (exc.status not in (502, 503, 504)
                    or exc.code not in ('service_unavailable', 'identity_unavailable')
                    or attempt == len(delays) or remaining <= delays[attempt]):
                raise
            time.sleep(delays[attempt])

"""Independent local read-only API; existing main API remains the auth authority."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .routes import dispatch, read_snapshot, DEFAULT_ROOT


def internal_token():
    for line in Path('/etc/x-tag-publisher.env').read_text().splitlines():
        if line.startswith('X_AUTO_POST_INTERNAL_TOKEN='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def internal_profiles(handler):
    try:
        token = internal_token()
        if not token or not hmac.compare_digest(handler.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
            return handler.send_json(401, {'message': '内部凭证无效'})
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 16384:
            return handler.send_json(400, {'message': '查询参数无效'})
        try:
            payload = json.loads(handler.rfile.read(size))
        except ValueError:
            return handler.send_json(400, {'message': '查询参数无效'})
        ids = payload.get('x_user_ids') if isinstance(payload, dict) else None
        if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(v, str) or not re.fullmatch(r'[0-9]{1,32}', v) for v in ids):
            return handler.send_json(400, {'message': '频道参数无效'})
        data, meta = read_snapshot(os.environ.get('X_TAG_REVENUE_ROOT', DEFAULT_ROOT), ids)
        return handler.send_json(200, dict(data=data, meta=meta))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return handler.send_json(503, {'message': '画像数据库暂不可用'})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def authorize(cookie):
    if not cookie or len(cookie) > 16384:
        return 401, []
    request = urllib.request.Request('http://127.0.0.1:8787/api/admin/x-accounts',
                                     headers={'Cookie': cookie, 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as upstream:
            data = json.loads(upstream.read(8 * 1024 * 1024))
        if not isinstance(data.get('items'), list):
            return 503, []
        return 200, data['items']
    except urllib.error.HTTPError as exc:
        return (exc.code if exc.code in (401,403) else 503), []
    except (OSError, ValueError):
        return 503, []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/healthz':
            return self.send_json(200, {'ok': True, 'service': 'x-channel-tag-revenue'})
        if not parsed.path.startswith('/api/x-channel-tag-revenue'):
            return self.send_json(404, {'message': '未找到接口'})
        dispatch(self, parsed, authorize, os.environ.get('X_TAG_REVENUE_ROOT', DEFAULT_ROOT))

    def do_POST(self):
        if self.path != '/internal/x-channel-tag-revenue':
            return self.send_json(404, {'message': '未找到接口'})
        self.connection.settimeout(30)
        internal_profiles(self)


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1',8896), Handler).serve_forever()

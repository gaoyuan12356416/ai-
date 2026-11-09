"""Independent local read-only API; existing main API remains the auth authority."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .routes import dispatch, DEFAULT_ROOT


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


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1',8896), Handler).serve_forever()

"""Independent read-only drain status, using the existing admin Cookie session."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from .drain_diagnostics import read_snapshot


def authenticate(cookie):
    if not cookie or len(cookie) > 8192:
        return 401
    connection = http.client.HTTPConnection('127.0.0.1', 8787, timeout=5)
    try:
        connection.request('GET', '/api/ui/topbar', headers={'Cookie': cookie, 'Connection': 'close'})
        response = connection.getresponse()
        body = response.read(512 * 1024 + 1)
        if response.status in (401, 403):
            return 401
        if response.status != 200 or len(body) > 512 * 1024:
            return 503
        auth = json.loads(body)
        user = auth.get('user') or {}
        if not auth.get('authenticated') or not user.get('user_id'):
            return 401
        return 200 if user.get('is_admin') is True else 403
    except (OSError, ValueError, http.client.HTTPException):
        return 503
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'ok': True})
        if self.path != '/api/pipeline-monitor/drain-diagnostics':
            return self.reply(404, {'error': 'not_found'})
        status = authenticate(self.headers.get('Cookie', ''))
        if status != 200:
            return self.reply(status, {'error': 'admin_session_required' if status in (401, 403) else 'session_unavailable'})
        return self.reply(200, read_snapshot())


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18961), Handler).serve_forever()

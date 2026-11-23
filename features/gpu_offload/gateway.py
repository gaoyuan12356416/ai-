"""Authenticate every read at the existing API, then serve a scoped snapshot."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import threading
import time
from urllib.parse import urlsplit

from .cache import Response, SingleFlightCache, annotated, cache_key
from .reports import PATHS
from .monitor_snapshots import MonitorSnapshots

MONITOR_PATHS = frozenset('/api/pipeline-monitor/' + name for name in ('summary', 'tasks', 'workers'))
FORWARD_HEADERS = {'content-type', 'content-disposition', 'x-ai-data-time', 'x-ai-data-version', 'x-ai-stale', 'set-cookie'}


class Denied(Exception):
    def __init__(self, status, code): self.status, self.code = status, code


def navigation_allowed(actor, config, item_key):
    def permitted(module):
        return actor.get('role') == 'admin' or bool(actor.get('permissions', {}).get(module))
    if not isinstance(config, list): raise ValueError('navigation_config_invalid')
    for group in config:
        if not isinstance(group, dict) or not isinstance(group.get('items', []), list): continue
        for item in group.get('items', []):
            if not isinstance(item, dict) or item.get('key') != item_key: continue
            if group.get('enabled') is False or item.get('enabled') is False: continue
            if (group.get('adminOnly') or item.get('adminOnly')) and actor.get('role') != 'admin': continue
            if any(node.get('module') and not permitted(node['module']) for node in (group, item)): continue
            return True
    return False


def authenticated_actor(payload):
    actor = payload.get('user')
    if not payload.get('authenticated') or not isinstance(actor, dict): raise Denied(401, 'cookie_auth_required')
    if any(not str(actor.get(k) or '').strip() for k in ('tenant_key', 'user_id')): raise Denied(401, 'cookie_auth_required')
    if actor.get('role') not in ('admin', 'user') or not isinstance(actor.get('permissions'), dict):
        raise Denied(403, 'permission_denied')
    return {k: actor[k] for k in ('tenant_key', 'user_id', 'role', 'permissions')}


def request(port, target, *, cookie='', token='', body=None, timeout=10, limit=64*1024*1024):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    headers = {'Connection': 'close', 'Accept': 'application/json'}
    if cookie: headers['Cookie'] = cookie
    if token: headers['Authorization'] = 'Bearer ' + token
    if body is not None: headers['Content-Type'] = 'application/json'
    try:
        connection.request('GET' if body is None else 'POST', target, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(limit + 1)
        if len(data) > limit: raise RuntimeError('response_too_large')
        values = {k.title(): v for k, v in response.getheaders() if k.lower() in FORWARD_HEADERS}
        return Response(response.status, values, data)
    finally: connection.close()


class Gateway(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, config_path, token):
        super().__init__(address, Handler)
        self.config_path, self.token = Path(config_path), token
        self.report_cache, self.monitor_cache = SingleFlightCache(), SingleFlightCache(ttl=10, max_bytes=32*1024*1024)
        self.monitor_snapshots = MonitorSnapshots(self.monitor_cache)
        self.monitor_snapshots.start()

    def server_close(self):
        if hasattr(self, 'monitor_snapshots'): self.monitor_snapshots.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_): pass

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def reply(self, value):
        self.response_status = value.status
        self.send_response(value.status)
        for key, data in value.headers.items():
            if key.lower() not in ('content-length', 'connection', 'cache-control', 'vary'):
                self.send_header(key, data)
        self.send_header('Content-Length', str(len(value.body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Cookie')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers(); self.wfile.write(value.body); self.close_connection = True

    def error(self, status, code):
        return self.reply(Response(status, {'Content-Type': 'application/json; charset=utf-8'},
                                   json.dumps({'error': code, 'message': '数据暂不可用，请稍后刷新；未知数据不按零统计。'}, ensure_ascii=False).encode()))

    def do_GET(self):
        started = time.monotonic()
        route, outcome, path = 'none', 'failed', ''
        self.response_status = 500
        try:
            path = urlsplit(self.path).path
            if path not in PATHS | MONITOR_PATHS or len(self.path) > 16000: return self.error(404, 'not_found')
            config = json.loads(self.server.config_path.read_text())
            cookie = self.headers.get('Cookie', '')
            if not cookie or len(cookie) > 16384: raise Denied(401, 'cookie_auth_required')
            # No caller Authorization, actor, tenant or role headers are forwarded.
            auth = request(config['api_port'], '/api/auth/status', cookie=cookie, timeout=5, limit=256*1024)
            if auth.status != 200: raise Denied(auth.status if auth.status in (401, 403) else 503, 'authentication_unavailable')
            actor = authenticated_actor(json.loads(auth.body))
            if path in MONITOR_PATHS:
                route = 'cpu-monitor'
                # Existing owner mapping remains authoritative for non-admins.
                # These requests continue through the original per-request check.
                if actor['role'] != 'admin' or not config.get('monitor_cache', False):
                    return self.reply(request(config['api_port'], self.path, cookie=cookie))
                key = cache_key(actor, self.path, 'monitor-v1')
                target, api_port, config_path = self.path, config['api_port'], self.server.config_path
                def load_monitor():
                    value = request(api_port, target, cookie=cookie, timeout=10, limit=4*1024*1024)
                    return Response(value.status, dict(value.headers, **{'X-AI-Data-Time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}), value.body)
                value, stale, hit = self.server.monitor_cache.get(key, load_monitor, wait=12)
                def authorized_refresh():
                    latest_config = json.loads(config_path.read_text())
                    if not latest_config.get('monitor_cache') or not latest_config.get('monitor_prefetch'):
                        raise Denied(503, 'monitor_prefetch_disabled')
                    latest = request(api_port, '/api/auth/status', cookie=cookie, timeout=5, limit=256*1024)
                    if latest.status != 200: raise Denied(401, 'authentication_unavailable')
                    current = authenticated_actor(json.loads(latest.body))
                    if current['role'] != 'admin' or cache_key(current, target, 'monitor-v1') != key:
                        raise Denied(403, 'permission_changed')
                    refresh_started = time.monotonic()
                    refreshed = load_monitor()
                    print(json.dumps({'kind': 'monitor-refresh', 'path': path, 'status': refreshed.status,
                                      'milliseconds': round((time.monotonic()-refresh_started)*1000)}), flush=True)
                    return refreshed
                if value.status == 200 and config.get('monitor_prefetch', False):
                    self.server.monitor_snapshots.touch(key, authorized_refresh)
            else:
                if actor['role'] != 'admin' and not actor['permissions'].get('social_analytics'):
                    raise Denied(403, 'permission_denied')
                navigation = json.loads(Path(config['navigation_path']).read_text(encoding='utf-8'))
                if not navigation_allowed(actor, navigation, 'socialAnalytics'):
                    raise Denied(403, 'navigation_item_unavailable')
                percentage = min(100, max(0, int(config.get('report_percentage', 0))))
                if random.randrange(100) >= percentage:
                    route, outcome = 'cpu-origin', 'forwarded'
                    return self.reply(request(config['api_port'], self.path, cookie=cookie, timeout=40))
                route = 'hk-snapshot'
                version = json.loads(Path(config['sync_state_path']).read_text())['version']
                key = cache_key(actor, self.path, version)
                def load_report():
                    body = json.dumps({'actor': actor, 'target': self.path}, separators=(',', ':')).encode()
                    return request(config['reader_port'], '/v1/query', token=self.server.token, body=body, timeout=40)
                value, stale, hit = self.server.report_cache.get(key, load_report)
                stale = stale or value.headers.get('X-Ai-Stale') == 'true'
            outcome = 'stale' if stale else 'hit' if hit else 'miss'
            return self.reply(annotated(value, stale=stale, hit=hit, backend=route))
        except Denied as exc: return self.error(exc.status, exc.code)
        except (BrokenPipeError, ConnectionResetError): self.close_connection = True
        except Exception: return self.error(503, 'snapshot_unavailable')
        finally:
            # Excludes query parameters, cookies, identifiers and report content.
            print(json.dumps({'kind': 'read', 'route': route, 'outcome': outcome, 'status': self.response_status,
                              'path': path if path in PATHS | MONITOR_PATHS else 'unsupported',
                              'milliseconds': round((time.monotonic()-started)*1000)}), flush=True)


def main():
    server = Gateway(('127.0.0.1', int(os.environ.get('OFFLOAD_GATEWAY_PORT', '19962'))),
                     os.environ['OFFLOAD_GATEWAY_CONFIG'], os.environ['OFFLOAD_INTERNAL_TOKEN'])
    def stop(*_): threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()

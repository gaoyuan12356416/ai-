"""Render the existing report contract exclusively from immutable snapshots."""
import io
import json
from urllib.parse import urlsplit

from .cache import Response
from features.social_analytics import routes, warehouse_reader

PATHS = frozenset('/api/social-analytics/' + name for name in ('options', 'report', 'export.csv'))


def validate_target(target):
    if not isinstance(target, str) or len(target) > 16000:
        raise ValueError('invalid_target')
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in PATHS:
        raise ValueError('unsupported_query')
    return parsed


def validate_actor(actor):
    if not isinstance(actor, dict) or actor.get('role') not in ('user', 'admin'):
        raise ValueError('invalid_actor')
    if any(not isinstance(actor.get(k), str) or not actor[k].strip() for k in ('tenant_key', 'user_id')):
        raise ValueError('invalid_actor')
    if actor.get('_system_report') or actor.get('auth_type') == 'api_token':
        raise ValueError('invalid_actor')


class Capture:
    command = 'GET'
    close_connection = False

    def __init__(self, actor):
        self.actor, self.status, self.headers, self.wfile = actor, 500, {}, io.BytesIO()

    def _social_analytics_actor(self): return self.actor
    def send_response(self, status): self.status = status
    def send_header(self, name, value): self.headers[name] = value
    def end_headers(self): pass


def render(root, actor, target, snapshot=None):
    validate_actor(actor)
    parsed = validate_target(target)
    snapshot = snapshot or warehouse_reader.current(root)
    manifest, _ = snapshot
    handler = Capture(actor)

    def reply(handler, status, payload, **unused):
        handler.status = status
        handler.headers['Content-Type'] = 'application/json; charset=utf-8'
        if status == 200:
            payload.setdefault('meta', {}).update(data_time=manifest['generated_at'],
                                                 warehouse_version=manifest['version'])
        handler.wfile.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())

    # No SQL configuration, application object, credentials, or live SQLite source
    # is provided. This mandatory root selects the immutable reader in all routes.
    routes.dispatch(handler, parsed, {'SOCIAL_ANALYTICS_WAREHOUSE_ROOT': str(root),
                                     'SOCIAL_ANALYTICS_FROZEN_SNAPSHOT': snapshot, 'json_response': reply})
    handler.headers.update({'X-AI-Data-Time': manifest['generated_at'], 'X-AI-Data-Version': manifest['version']})
    return Response(handler.status, handler.headers, handler.wfile.getvalue())

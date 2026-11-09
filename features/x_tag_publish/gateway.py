"""Cookie-authenticated public gateway for the isolated lane."""
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from .policy import ROOT, lane_paths, portrait_snapshot, Decision
from .transport import Error, rpc
from features.x_tag_revenue.tags import localize_audit

AUTO = '/api/admin/x-tag-auto-publish'
POST = '/api/admin/x-tag-posts'
OLD_AUTO = '/api/admin/x-auto-publish'
NAV_POOL = 'xPostMaterialPool'


def read_env(path):
    result = {}
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        if raw and not raw.startswith('#') and '=' in raw:
            k, v = raw.split('=', 1)
            result[k] = v.strip().strip('"').strip("'")
    result.update(os.environ)
    return result


def authorize(cookie, probe):
    if not cookie or len(cookie) > 16384:
        raise Error('cookie_auth_required', '请先登录', 401)
    auth = rpc('http://127.0.0.1:8787', '/api/auth/status', cookie=cookie)
    user = auth.get('user') or {}
    if not auth.get('authenticated') or not user.get('user_id') or not user.get('tenant_key'):
        raise Error('cookie_auth_required', '请先登录', 401)
    # The original route evaluates the current navigation and module ACL.
    rpc('http://127.0.0.1:8787', probe, cookie=cookie)
    return {k: user.get(k, '') for k in ('user_id', 'tenant_key', 'name', 'email', 'role')}


def post_rpc(path, payload):
    return rpc('http://127.0.0.1:8821', '/internal/posts/' + path,
               os.environ['X_INTERNAL_TOKEN'], payload=payload, timeout=120)


def initial_checks(material_ids):
    from features.x_posts.selector import select_pool_candidates, previous_source_date
    from scripts.x_post_schedule_runner import ScheduleConfig, _open_source_connection, _close_source_connection
    if not isinstance(material_ids, list) or not 1 <= len(material_ids) <= 100:
        raise Error('invalid_request', '一次请输入 1–100 个素材 ID', 400)
    if any(not re.fullmatch(r'[1-9][0-9]*', str(v)) for v in material_ids):
        raise Error('invalid_request', '素材 ID 无效', 400)
    stamp = datetime.now(timezone.utc).isoformat()
    pool = [dict(id=i, material_id=str(v), created_at=stamp) for i, v in enumerate(material_ids, 1)]
    connection = None
    try:
        config = ScheduleConfig.from_env()
        connection = _open_source_connection(config, None)
        selected, failures = select_pool_candidates(connection, pool, previous_source_date(),
            limit=len(pool), schema=config.mysql_database, allow_images=True)
        result = {str(v): dict(material_id=str(v), error_code='', error_message='') for v in material_ids}
        for item in failures:
            key = str(item.get('material_id') or '')
            if key in result:
                result[key].update(error_code=item.get('error_code'), error_message=item.get('error_message'))
        selected_ids = {str(item['material_id']) for item in selected}
        for key, item in result.items():
            if key not in selected_ids and not item.get('error_code'):
                item.update(error_code='material_validation_incomplete', error_message='素材资料尚未完整校验')
        return list(result.values())
    except Error:
        raise
    except Exception:
        return [dict(material_id=str(v), error_code='material_validation_unavailable',
                     error_message='素材资料暂时无法校验，排期前会重新校验') for v in material_ids]
    finally:
        if connection is not None:
            _close_source_connection(connection)


def resolve_attribution(body):
    from features.x_posts.attribution import Resolver, AttributionError
    from features.social_analytics.warehouse_refresh import Reader
    env = read_env('/root/drama_material_service/.env')
    reader = Reader(env)
    schema = env.get('ADMIN_MAPPING_MYSQL_DATABASE', '')
    if not re.fullmatch('[A-Za-z0-9_]+', schema):
        raise AttributionError('x_attribution_replica_invalid', 503)
    def ids(email):
        sql = "SELECT DISTINCT HEX(JSON_OBJECT('value',sub_user_id)) FROM `" + schema + \
            "`.admin_user_group WHERE BINARY LOWER(TRIM(email))=BINARY CONVERT(0x" + email.encode().hex() + " USING utf8mb4) LIMIT 2"
        return [r['value'] for r in reader.query([sql], timeout=30)]
    def emails(sub_id):
        sql = "SELECT DISTINCT HEX(JSON_OBJECT('value',LOWER(TRIM(email)))) FROM `" + schema + \
            "`.admin_user_group WHERE sub_user_id=" + str(int(sub_id)) + ' LIMIT 101'
        values = [r['value'] for r in reader.query([sql], timeout=30)]
        if len(values) > 100:
            raise AttributionError('x_attribution_default_owner_ambiguous')
        return values
    paths = lane_paths()
    resolver = Resolver(paths['X_POST_DB_PATH'], paths['X_AUTO_POST_DB_PATH'],
        env.get('JOB_DB_PATH', '/root/drama_material_service/data/drama_material_jobs.sqlite3'), ids, emails)
    return resolver.resolve(body['queue_id'], body['log_id'])


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def enrich_logs(result):
    paths = lane_paths()
    with closing(readonly(paths['X_POST_DB_PATH'])) as db, closing(readonly(paths['X_AUTO_POST_DB_PATH'])) as auto:
        for row in result.get('items', []):
            qid = row.get('queue_id')
            audit = db.execute('SELECT selection_json FROM x_tag_queue_selection WHERE queue_id=?', (qid,)).fetchone()
            if audit:
                row['tag_profile'] = localize_audit(json.loads(audit[0]))
            else:
                task = auto.execute('SELECT selection_json FROM x_auto_task WHERE execution_queue_id=?', (qid,)).fetchone()
                if task:
                    row['tag_profile'] = localize_audit(json.loads(task[0]).get('tag_profile'))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        if self.command == 'DELETE' and not int(self.headers.get('Content-Length', '0')):
            return {}
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise Error('json_content_type_required', '请使用 JSON 请求', 415)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 2 * 1024 * 1024:
                raise ValueError()
            data = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise Error('invalid_request', '请求参数无效', 400) from None

    def dispatch(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == '/healthz' and self.command == 'GET':
            return 200, dict(ok=True, service='x-tag-publish-gateway')
        if path == '/internal/x-tag/attribution' and self.command == 'POST':
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + os.environ['X_INTERNAL_TOKEN']):
                raise Error('permission_denied', '', 403)
            body = self.body()
            if set(body) != {'queue_id', 'log_id'}:
                raise Error('invalid_request', '', 400)
            return 200, dict(context=resolve_attribution(body))
        auto_match = re.fullmatch(re.escape(AUTO) + r'/(?:accounts(?:/[1-9][0-9]*/verify)?|templates(?:/[1-9][0-9]*(?:/(?:copy|enable|disable|preview|run-now))?)?|runs(?:/[1-9][0-9]*)?)', path)
        post_match = re.fullmatch(re.escape(POST) + r'/(?:logs|runs|profiles|material-pool(?:/(?:account-options|schedule|manual-publish|manual-runs/[1-9][0-9]*|[1-9][0-9]*))?)', path)
        if not auto_match and not post_match:
            raise Error('not_found', '', 404)
        probe = (OLD_AUTO + ('/runs?limit=1' if '/runs' in path else '/templates?limit=1')) if auto_match else \
                ('/api/admin/x-posts/logs?page_size=1' if path.endswith(('/logs', '/runs')) else '/api/admin/x-posts/material-pool?page_size=1')
        try:
            actor = authorize(self.headers.get('Cookie', ''), probe)
        except Error as exc:
            if path == POST + '/profiles' and exc.status == 403:
                actor = authorize(self.headers.get('Cookie', ''), OLD_AUTO + '/templates?limit=1')
            else:
                raise
        body = {}
        if self.command in ('POST', 'PUT', 'DELETE'):
            source = self.headers.get('Origin') or self.headers.get('Referer') or ''
            if source:
                origin = urlsplit(source)
                if (origin.scheme, origin.netloc) != ('https', 'ai.yingliangads.com'):
                    raise Error('same_origin_required', '', 403)
            body = self.body()
        if auto_match:
            if self.command not in ('GET', 'POST'):
                raise Error('method_not_allowed', '', 405)
            if self.command == 'POST' and '/templates' in path:
                body['_actor'] = {k: actor[k] for k in ('user_id', 'name', 'tenant_key')}
            target = path.replace(AUTO, OLD_AUTO, 1) + ('?' + parsed.query if parsed.query else '')
            result = rpc('http://127.0.0.1:18843', target, os.environ['X_AUTO_POST_INTERNAL_TOKEN'],
                         method=self.command, payload=body if self.command == 'POST' else None, timeout=120)
            return (202 if path.endswith('/run-now') else 200), result
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()):
            raise Error('invalid_request', '', 400)
        payload = {k: v[0] for k, v in query.items()}
        payload.update(actor=actor, scope='all', navigation_item=NAV_POOL)
        suffix = path[len(POST) + 1:]
        if suffix in ('logs', 'runs'):
            payload.pop('navigation_item')
        if self.command == 'GET':
            if suffix == 'profiles':
                accounts = post_rpc('material-pool/account-options', payload)['items']
                snapshot = portrait_snapshot()
                with closing(readonly(lane_paths()['X_POST_DB_PATH'])) as db:
                    counts = {str(r[0]): r[1] for r in db.execute(
                        "SELECT account_id,COUNT(*) FROM x_post_queue WHERE status='published' GROUP BY account_id")}
                return 200, dict(meta=snapshot[1], items=[dict(account_id=a['id'], username=a.get('username'),
                    published_count=counts.get(str(a['id']), 0),
                    profile=Decision(a, ['preview'], snapshot).audit(None)) for a in accounts])
            routes = {'logs': 'logs/query', 'runs': 'runs/query', 'material-pool': 'material-pool/query',
                      'material-pool/account-options': 'material-pool/account-options',
                      'material-pool/schedule': 'material-pool/schedule/query'}
            target = routes.get(suffix)
            match = re.fullmatch(r'material-pool/manual-runs/([1-9][0-9]*)', suffix)
            if match:
                target = 'manual-runs/' + match.group(1) + '/query'
            if not target:
                raise Error('not_found', '', 404)
            result = post_rpc(target, payload)
            return 200, enrich_logs(result) if suffix == 'logs' else result
        if self.command == 'POST' and suffix == 'material-pool':
            ids = body.get('material_ids')
            payload.update(material_ids=ids, validation_checks=initial_checks(ids))
            return 200, post_rpc('material-pool/add', payload)
        if self.command == 'PUT' and suffix == 'material-pool/schedule':
            payload['settings'] = body
            return 200, post_rpc('material-pool/schedule/save', payload)
        if self.command == 'POST' and suffix == 'material-pool/manual-publish':
            allowed = {'material_ids', 'account_ids', 'idempotency_key', 'publish_mode', 'scheduled_at', 'random_template_enabled'}
            if set(body) - allowed:
                raise Error('invalid_request', '', 400)
            payload.update(body)
            return 202, post_rpc('manual-runs/create', payload)
        if self.command == 'DELETE' and re.fullmatch(r'material-pool/[1-9][0-9]*', suffix):
            return 200, post_rpc(suffix + '/delete', payload)
        raise Error('method_not_allowed', '', 405)

    def handle_request(self):
        try:
            status, payload = self.dispatch()
        except Error as exc:
            status, payload = exc.status, dict(error=exc.code, code=exc.code, message=str(exc))
        except Exception as exc:
            # Deliberately avoid paths, source SQL, request bodies and credentials.
            print('tag_gateway_error:' + type(exc).__name__, flush=True)
            status, payload = 503, dict(error='tag_service_unavailable', message='画像发布服务暂不可用，请稍后重试')
        self.send_json(status, payload)

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


if __name__ == '__main__':
    lane_paths()
    ThreadingHTTPServer(('127.0.0.1', 8898), Handler).serve_forever()

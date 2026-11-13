"""New posting process; shared account records are strictly read-only here.

The existing OAuth owner remains the sole token refresher. All queue, dedupe,
limits, attribution, random-template preparations and execution state are local.
"""
import json
import os
import re
import sqlite3
import signal
import threading
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .policy import initialize_ids, lane_paths, canonical


AUDIT_DDL = '''CREATE TABLE IF NOT EXISTS x_tag_queue_selection(
 queue_id INTEGER PRIMARY KEY, material_id TEXT NOT NULL, account_id INTEGER NOT NULL,
 selection_json TEXT NOT NULL, FOREIGN KEY(queue_id) REFERENCES x_post_queue(id))'''


def install(runtime):
    from features.x_posts import service
    from .pool import install_reservation_policy
    from .transport import rpc
    paths = lane_paths()
    if runtime.POST_DB_PATH.resolve() != paths['X_POST_DB_PATH']:
        raise RuntimeError('tag_lane_post_path_mismatch')
    runtime.ensure_storage = lambda: None

    def account_db():
        db = sqlite3.connect(runtime.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db
    runtime.db_connect = account_db

    def verify(account_id, actor, scope='mine', *, only_refresh_required=False,
               preserve_transient_status=False, require_publish_approved=False, allow_token_refresh=True):
        # Original sidecar's per-account lock serializes refresh and credential rotation.
        payload = dict(actor=actor, scope=scope, only_refresh_required=bool(only_refresh_required),
                       preserve_transient_status=bool(preserve_transient_status))
        try:
            result = rpc('http://127.0.0.1:8810', '/internal/accounts/%s/verify' % int(account_id),
                         os.environ['X_TAG_ACCOUNT_BROKER_TOKEN'], payload=payload, timeout=120)
            account = result['item']
            if require_publish_approved and not account.get('publish_approved'):
                raise runtime.ServiceError('x_account_publish_not_approved', '该 X 账号尚未允许发布', 409)
            return account
        except Exception as exc:
            raise runtime.ServiceError(getattr(exc, 'code', 'x_accounts_unavailable'),
                                       str(exc), getattr(exc, 'status', 503)) from None
    runtime.verify_account = verify
    # Read-only credentials use the original atomic token files. This process cannot
    # authorize, refresh, replace or delete them (also enforced by systemd).
    install_reservation_policy(service)
    service.XPostStore(paths['X_POST_DB_PATH'])
    initialize_ids(paths['X_POST_DB_PATH'])
    with closing(sqlite3.connect(str(paths['X_POST_DB_PATH']))) as db, db:
        db.execute(AUDIT_DDL)
    original_plan = runtime.create_post_schedule_plan_request

    def create_plan(payload):
        payload = dict(payload)
        candidates, audits = [], {}
        for raw in payload.get('candidates', []):
            item = dict(raw)
            audit = item.pop('tag_profile', None)
            if isinstance(audit, dict):
                audits[(int(item['account_id']), str(item['material_id']))] = audit
            candidates.append(item)
        payload['candidates'] = candidates
        result = original_plan(payload)
        with closing(sqlite3.connect(str(paths['X_POST_DB_PATH']), timeout=30)) as db, db:
            for queue in result.get('queues', []):
                actual = db.execute('SELECT account_id,material_id FROM x_post_queue WHERE id=?', (queue['id'],)).fetchone()
                audit = audits.get((actual[0], str(actual[1]))) if actual else None
                if audit:
                    db.execute('INSERT OR IGNORE INTO x_tag_queue_selection VALUES(?,?,?,?)',
                               (queue['id'], str(actual[1]), actual[0], canonical(audit)))
        return result
    runtime.create_post_schedule_plan_request = create_plan

    from .hit_timing import assert_due
    original_publish = runtime.publish_queue_request
    def publish_when_due(queue_id, *args, **kwargs):
        assert_due(paths['X_AUTO_POST_DB_PATH'].as_uri(), service.XPostStore(paths['X_POST_DB_PATH']), queue_id)
        return original_publish(queue_id, *args, **kwargs)
    runtime.publish_queue_request = publish_when_due

    from features.x_posts import attribution
    def attribution_resolver(queue_id, log_id):
        result = rpc('http://127.0.0.1:8898', '/internal/x-tag/attribution', runtime.INTERNAL_TOKEN,
                     payload=dict(queue_id=queue_id, log_id=log_id), timeout=30)
        return attribution.validate_context(result['context'])
    attribution.resolve_via_loopback = attribution_resolver
    from .dispatch_native import install_attribution
    install_attribution(attribution_resolver)


def serve():
    from features.x_accounts import oauth_service as runtime
    install(runtime)

    class Handler(runtime.Handler):
        def do_GET(self):
            if urlsplit(self.path).path == '/health':
                return self.send_json(200, dict(ok=True, service='x-tag-publisher', isolated=True))
            return self.send_json(404, dict(error='not_found'))

        def do_POST(self):
            path = urlsplit(self.path).path
            if path.startswith('/internal/pipeline-dispatch/x/'):
                from .dispatch_native import handle
                return handle(self, runtime, path)
            # No OAuth, account mutation, video workspace or
            # drama pool routes are exposed by this independent material lane.
            if not path.startswith('/internal/posts/') or '/drama-pool/' in path or path.endswith('/canary'):
                return self.send_json(404, dict(error='not_found'))
            return super().do_POST()
    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True
    server = Server(('127.0.0.1', 8821), Handler)
    stopping = threading.Event()
    def shutdown(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    serve()

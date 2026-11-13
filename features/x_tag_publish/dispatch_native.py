"""Authenticated canonical bridge for the tag-owned ledger and attribution."""
import os
import secrets
import threading

ID_BASE = 9000000000000
_bridge = None
_lock = threading.Lock()


ACK_TABLE = 'pipeline_x_unentered_failure_ack'


def acknowledge_unentered_failure(bridge, job, claim, receipt):
    """Only an operator-audited, exact unentered claim may ACK without native fail."""
    from features.pipeline_dispatch.x_prepost_recovery import authorization
    if job.get('stage') != 'publish' or receipt.get('unknown_outcome', False):
        return False
    with bridge.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(ACK_TABLE,)).fetchone():
            return False
        saved = conn.execute('SELECT * FROM '+ACK_TABLE+' WHERE source_key=? AND generation=?',
                             (job['source_key'],job['generation'])).fetchone()
        if (not saved or not secrets.compare_digest(saved['claim_hash'],bridge.claim_hash(claim))
                or receipt.get('error_code') != saved['error_code']):
            return False
        for table in ('pipeline_x_claim','pipeline_x_operation','pipeline_x_checkpoint'):
            if conn.execute('SELECT 1 FROM '+table+' WHERE source_key=? AND generation>=? LIMIT 1',
                            (job['source_key'],job['generation'])).fetchone():
                return False
        queue = conn.execute('SELECT * FROM x_post_queue WHERE id=?',(int(job['source_id']),)).fetchone()
        log = conn.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(int(job['source_id']),)).fetchone()
        return bool(queue and log and authorization(conn,queue,log,job))


def handle(handler, runtime, path):
    global _bridge
    from features.pipeline_dispatch import x_native
    from features.pipeline_dispatch.adapters import AdapterError
    expected = os.environ.get('PIPELINE_X_CANONICAL_TOKEN', '')
    supplied = handler.headers.get('Authorization', '').removeprefix('Bearer ')
    if (os.environ.get('PIPELINE_X_CANONICAL_BRIDGE_ENABLED') != '1'
            or len(expected) < 32 or not secrets.compare_digest(supplied, expected)):
        return handler.send_json(403, {'code': 'pipeline_x_bridge_disabled_or_unauthorized'})
    try:
        payload = handler.read_json(2 * 1024 * 1024)
        job = payload.get('job', {})
        x_native.XNativeBridge.identity(job)
        if int(job['source_id']) < ID_BASE:
            raise AdapterError('x_tag_source_namespace_invalid')
        with _lock:
            if _bridge is None:
                from .hit_timing import assert_due
                from .policy import lane_paths
                class HitBridge(x_native.XNativeBridge):
                    def reserve(self, job, claim):
                        if job['stage'] == 'publish':
                            assert_due(lane_paths()['X_AUTO_POST_DB_PATH'].as_uri(), self.store, job['source_id'])
                        return super().reserve(job, claim)

                    def fail(self, job, claim, receipt):
                        with self._source_lock(job):
                            if acknowledge_unentered_failure(self,job,claim,receipt):
                                return {'canonical_no_write': True}
                            return super().fail(job,claim,receipt)

                    def begin_operation(self, job, claim, operation):
                        assert_due(lane_paths()['X_AUTO_POST_DB_PATH'].as_uri(), self.store, job['source_id'])
                        return super().begin_operation(job, claim, operation)
                _bridge = HitBridge(runtime)
        result = _bridge.dispatch(path.rsplit('/', 1)[-1], payload)
        return handler.send_json(200, result)
    except Exception as exc:
        # Preserve only frame locations and exception type, never values/source.
        import logging
        import traceback
        from pathlib import Path
        stack = ' > '.join('%s:%s:%s' % (Path(f.filename).name, f.lineno, f.name)
                           for f in traceback.extract_tb(exc.__traceback__)[-12:])
        logging.getLogger(__name__).error('x_tag_dispatch_failed type=%s stack=%s',type(exc).__name__,stack)
        return handler.send_json(409, {'code': getattr(exc, 'code', 'x_tag_dispatch_failed')})


def install_attribution(resolver):
    from features.pipeline_dispatch import x_native
    from features.x_posts.attribution import validate_context

    def resolve(queue_id, log):
        return validate_context(resolver(queue_id, log['id']), log)
    x_native._resolve_attribution = resolve

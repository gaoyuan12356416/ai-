"""Authenticated canonical bridge for the tag-owned ledger and attribution."""
import os
import secrets
import threading

ID_BASE = 9000000000000
_bridge = None
_lock = threading.Lock()


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

                    def begin_operation(self, job, claim, operation):
                        assert_due(lane_paths()['X_AUTO_POST_DB_PATH'].as_uri(), self.store, job['source_id'])
                        return super().begin_operation(job, claim, operation)
                _bridge = HitBridge(runtime)
        result = _bridge.dispatch(path.rsplit('/', 1)[-1], payload)
        return handler.send_json(200, result)
    except Exception as exc:
        return handler.send_json(409, {'code': getattr(exc, 'code', 'x_tag_dispatch_failed')})


def install_attribution(resolver):
    from features.pipeline_dispatch import x_native
    from features.x_posts.attribution import validate_context

    def resolve(queue_id, log):
        return validate_context(resolver(queue_id, log['id']), log)
    x_native._resolve_attribution = resolve

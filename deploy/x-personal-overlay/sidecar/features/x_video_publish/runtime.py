"""Loopback routes owned by the token-holding X sidecar.

Backend requests can manage personal batches; only the separate daily runner
credential can tick pools. Neither route accepts a publisher or arbitrary URL.
"""
import threading

from .errors import PublishingError
from .gateway import read_json
from .service import PublishingService
from .scheduler import PoolScheduler

_lock = threading.Lock()
_instances = {}


def services(oauth):
    key = (str(oauth.POST_DB_PATH), id(oauth))
    with _lock:
        if key not in _instances:
            service = PublishingService(oauth)
            _instances[key] = (service, PoolScheduler(service._pool(), enabled=service.enabled))
        return _instances[key]


def dispatch(handler, oauth, path):
    from features.x_posts.service import XPostError
    schedules = {'/internal/video-publish/pool/start', '/internal/video-publish/pool/tick'}
    if path not in schedules | {'/internal/video-publish/dispatch'}:
        return handler.send_json(404, {'code': 'not_found', 'accepted': False})
    forwarded = any(k.lower() == 'forwarded' or k.lower().startswith('x-forwarded-')
                    or k.lower() == 'x-real-ip' for k in handler.headers.keys())
    required_role = 'daily' if path in schedules else 'backend'
    if (forwarded or len(handler.headers.get_all('Authorization', [])) != 1
            or handler.internal_role() != required_role):
        handler.close_connection = True
        return handler.send_json(403, {'code': 'x_internal_auth_failed', 'message': '内部鉴权失败', 'accepted': False})
    try:
        payload = read_json(handler, internal=True)
        service, scheduler = services(oauth)
        if path in schedules:
            if set(payload) != {'worker_id'}:
                raise PublishingError('invalid_request', '调度请求字段无效', 400)
            method = scheduler.start if path.endswith('/start') else scheduler.tick
            result = method(payload['worker_id'])
        else:
            if (set(payload) != {'method', 'path', 'actor', 'payload', 'query'}
                    or payload['method'] not in {'GET', 'POST'}
                    or not isinstance(payload['path'], str)
                    or any(not isinstance(payload[k], dict) for k in ('actor', 'payload', 'query'))):
                raise PublishingError('invalid_request', '内部请求字段无效', 400)
            result = service.dispatch(payload['method'], payload['path'], payload['actor'], payload['payload'], payload['query'])
            if payload['method'] == 'POST' and hasattr(oauth, 'safe_record_event'):
                # Keep the real operator, never the account owner or a synthetic admin.
                oauth.safe_record_event('x_platform_publish_change', 'completed', payload['actor'],
                                        error_code=payload['path'][:64])
        handler.send_json(200, result)
    except (PublishingError, XPostError, oauth.ServiceError) as exc:
        handler.close_connection = True
        # All expected failures occur before the core transaction commits. A
        # committed idempotent operation is always returned by its existing key.
        result = {'code': exc.code, 'error': exc.code, 'message': str(exc), 'accepted': False}
        if getattr(exc, 'accepted', False) is None:
            result.pop('accepted')
        handler.send_json(getattr(exc, 'status', 503), result)
    except Exception:
        handler.close_connection = True
        # An unexpected error after commit cannot promise that nothing happened.
        handler.send_json(503, {'code': 'service_unavailable', 'message': '服务暂不可用，请保留原操作并查询结果'})

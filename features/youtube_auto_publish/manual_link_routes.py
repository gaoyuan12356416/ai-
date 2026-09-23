"""Manual links share the existing authenticated YouTube route boundary."""
import re
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs

from .manual_links import DramaCatalog, ManualLinks
from .runtime import readonly_runner, attribution_user_resolver
from .templates import WorkflowError

_service = None
_lock = threading.Lock()


def service(app):
    global _service
    with _lock:
        if _service is None:
            runtime = SimpleNamespace(**app)
            workflow = app['get_youtube_auto_service']()
            _service = ManualLinks(runtime.JOB_DB_PATH, runtime.DRAMA_SHORT_LINK_PUBLISHER,
                                   workflow.channel_directory, DramaCatalog(readonly_runner(runtime), runtime.DB_NAME),
                                   attribution_user_resolver(runtime))
        return _service


def dispatch(handler, parsed, actor, app):
    path = parsed.path[len('/api/youtube-auto-publish/short-links'):]
    reply = lambda status, body: app['json_response'](handler, status, body, no_store=True)
    lookup = re.fullmatch(r'/([0-9a-fA-F-]{36})', path)
    if not ((handler.command=='GET' and (path in ('/channels','/dramas') or lookup))
            or (handler.command=='POST' and path=='')):
        handler.close_connection = True
        return reply(404, {'error':'not_found'})
    payload = handler._youtube_auto_json(32768) if handler.command=='POST' else None
    if handler.command=='POST' and payload is None:
        return
    try:
        links = service(app)
        if handler.command=='POST':
            value = links.create(actor, payload)
        else:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            if path=='/channels':
                value = links.channels.options(actor, refresh=query.get('refresh',['0'])[0]=='1')
            elif path=='/dramas':
                value = links.catalog.search(query.get('search',[''])[0], int(query.get('page',['1'])[0]))
            else:
                value = links.get(actor, lookup.group(1))
        return reply(200, value)
    except WorkflowError as exc:
        return reply(exc.status, {'error':exc.code,'message':str(exc)})
    except ValueError:
        return reply(400, {'error':'invalid_request','message':'请求参数无效'})
    except Exception:
        return reply(503, {'error':'manual_link_unavailable','message':'短链服务暂不可用，请稍后重试'})

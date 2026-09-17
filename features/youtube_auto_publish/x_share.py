"""Cookie-scoped bridge from an existing public YouTube task to X link posts."""
import re
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlencode

import requests

from features.x_accounts import client as x_client
from features.x_accounts.youtube_share_text import DEFAULT_TEMPLATE, LIMIT, MACROS, canonical_url, preview
from .templates import WorkflowError


def source_context(service, actor, task_id, *, require_public=True):
    with service.db() as connection:
        _, body = service._row(connection, task_id, actor)
        ledger = service._ledger(body, connection) or {}
    video_id = str(ledger.get('video_id') or '')
    if require_public and (ledger.get('video_state') != 'published' or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id)):
        raise WorkflowError('youtube_not_public', '视频尚未确认公开，暂不能转发到 X', 409)
    channel = body.get('channel') or {}
    channel_id = str(ledger.get('channel_id') or channel.get('channel_id') or '')
    if require_public and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        raise WorkflowError('youtube_identity_missing', '视频频道信息不完整，请先核实任务', 409)
    source = {
        'task_id': task_id, 'video_id': video_id,
        'youtube_url': canonical_url(video_id) if re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) else '',
        'title': str(ledger.get('title') or body.get('title') or ''),
        'channel_name': str(channel.get('name') or channel.get('channel_name') or ''),
        'channel_url': 'https://www.youtube.com/channel/' + channel_id if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id) else '',
        'thumbnail_url': 'https://i.ytimg.com/vi/' + video_id + '/hqdefault.jpg' if re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) else '',
    }
    material = body.get('material') or {}
    values = {key: source.get(key, '') for key, _ in MACROS}
    # Reuse this task's frozen promotional link; never reserve or rebuild a link here.
    values.update(short_url=str(material.get('macro_url') or ''),
                  name=str(material.get('macro_name') or ''), desc=str(material.get('macro_desc') or ''))
    return source, values, ledger, channel_id


def verify_public(app, ledger, channel_id, video_id, *, repository=None, client=None, session_factory=requests.Session):
    """Refresh credentials and perform reads only; never modify the source video."""
    from features.drama_synthesis.youtube import YouTubeCredentialRepository, YouTubeHTTPClient
    from .runtime import readonly_runner
    repository = repository or YouTubeCredentialRepository(readonly_runner(app), schema=app.DB_NAME)
    credential = repository.credential(
        app_id=str(ledger.get('app_id') or ''), channel_local_id=str(ledger.get('channel_local_id') or ''),
        account_id=str(ledger.get('youtube_account_id') or ''), expected_channel_id=channel_id,
    )
    client = client or YouTubeHTTPClient(timeout=30)
    token = client.refresh_access_token(credential)
    client.verify_channel_identity(token, channel_id)
    session = session_factory()
    session.trust_env = False
    try:
        response = session.get(
            'https://www.googleapis.com/youtube/v3/videos?' + urlencode({'part': 'snippet,status', 'id': video_id}),
            headers={'Authorization': 'Bearer ' + token}, timeout=(10, 30), allow_redirects=False,
        )
        data = response.json() if response.status_code == 200 else {}
        items = data.get('items') if isinstance(data, dict) else None
        item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
        if item.get('id') != video_id or (item.get('snippet') or {}).get('channelId') != channel_id:
            raise WorkflowError('youtube_source_unknown', '暂时无法确认原视频，请核实 YouTube 视频后再转发', 409)
        status = item.get('status') or {}
        if status.get('privacyStatus') != 'public' or status.get('uploadStatus') != 'processed':
            raise WorkflowError('youtube_not_public', 'YouTube 视频当前尚未公开或处理完成，不能转发', 409)
        return time.time()
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        raise WorkflowError('youtube_source_unavailable', 'YouTube 视频状态查询失败，请稍后重试', 503) from None
    finally:
        session.close()


def _selection(payload):
    ids = payload.get('account_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        raise WorkflowError('invalid_request', '请选择 1 至 20 个不同的 X 账号', 400)
    operation_id = payload.get('operation_id')
    try:
        valid_operation = isinstance(operation_id, str) and str(uuid.UUID(operation_id)) == operation_id.lower()
    except (ValueError, AttributeError):
        valid_operation = False
    if not valid_operation:
        raise WorkflowError('invalid_request', '操作标识无效，请重新打开转发弹窗', 400)
    template = payload.get('description_template')
    if not isinstance(template, str) or len(template) > 5000:
        raise WorkflowError('invalid_request', '描述模板无效', 400)
    return sorted(ids), operation_id.lower(), template


def handle(service, app, actor, task_id, action, payload=None, run_id='', *, x_request=None, source_check=None):
    x_request = x_request or x_client.youtube_share_request
    source_check = source_check or verify_public
    scope = 'all' if actor.get('role') == 'admin' else 'mine'
    # Permission is checked even for polling, after deletion or status changes.
    source, values, ledger, channel_id = source_context(service, actor, task_id, require_public=action not in ('run', 'create'))
    if action == 'run':
        return x_request('run', actor, scope=scope, task_id=task_id, run_id=run_id)
    if action == 'options':
        records = x_client.query_x_accounts(actor, scope=scope)
        history = x_request('query', actor, scope=scope, task_id=task_id, video_id=source['video_id']).get('history', [])
        blocked = {}
        for run in history:
            for item in run.get('items', []):
                if item.get('status') in ('queued', 'publishing', 'published', 'unknown_outcome'):
                    blocked[int(item['account_id'])] = item
        accounts = []
        for account in records.get('items', []):
            account_id = int(account['id'])
            reason = ''
            if account.get('status') != 'active': reason = '账号授权不可用，请先重新授权或校验'
            elif account.get('publish_approved') is not True: reason = '该账号未允许发布'
            elif account_id in blocked:
                reason = {'published': '此视频已转发', 'unknown_outcome': '上次发送结果待核实，禁止重复发送'}.get(blocked[account_id]['status'], '此视频正在转发')
            accounts.append({'id': account_id, 'username': str(account.get('username') or ''),
                             'name': str(account.get('name') or account.get('display_name') or ''),
                             'selectable': not reason, 'block_reason': reason})
        return {'source': source, 'macros': [{'key': k, 'label': label, 'value': values[k]} for k, label in MACROS],
                'default_template': DEFAULT_TEMPLATE, 'accounts': accounts, 'history': history, 'limit': LIMIT}
    if action == 'preview':
        return preview((payload or {}).get('description_template'), values)
    ids, operation_id, template = _selection(payload or {})
    previous = x_request('query', actor, scope=scope, task_id=task_id, video_id=source['video_id'], operation_id=operation_id).get('run')
    if previous:
        if sorted(previous.get('account_ids', [])) != ids or previous.get('description_template') != template:
            raise WorkflowError('operation_conflict', '此操作已提交，请先查看原转发结果', 409)
        return {'run': previous}
    # Re-read and verify both durable source identity and live public visibility.
    source, values, ledger, channel_id = source_context(service, actor, task_id)
    rendered = preview(template, values)
    if not rendered['valid']:
        raise WorkflowError('invalid_description', '；'.join(rendered['errors']), 400)
    verified_at = source_check(app, ledger, channel_id, source['video_id'])
    return x_request('create', actor, scope=scope, task_id=task_id, video_id=source['video_id'],
                     youtube_url=source['youtube_url'], title=source['title'], text=rendered['text'],
                     description_template=template, operation_id=operation_id, account_ids=ids, source_verified_at=verified_at)


def dispatch(handler, parsed, actor, env):
    match = re.fullmatch(r'/api/youtube-auto-publish/tasks/([0-9a-f]{32})/x-share(?:/(preview)|/runs/([0-9a-f]{32}))?', parsed.path)
    if not match:
        return False
    respond = lambda status, body: env['json_response'](handler, status, body, no_store=True)
    session = env['load_session'](handler._cookies().get(env['SESSION_COOKIE_NAME'], '')) or {}
    if not env['has_module_permission'](session, 'x_accounts'):
        handler.close_connection = True
        respond(403, {'error': 'permission_denied', 'message': '需要 X 账号模块权限才能转发', 'module': 'x_accounts'})
        return True
    action = 'run' if match.group(3) else 'preview' if match.group(2) else 'options' if handler.command == 'GET' else 'create'
    if handler.command != ('POST' if action in ('create', 'preview') else 'GET'):
        handler.close_connection = True
        respond(405, {'error': 'method_not_allowed'})
        return True
    payload = handler._youtube_auto_json(32 * 1024) if handler.command == 'POST' else None
    if handler.command == 'POST' and payload is None:
        return True
    try:
        result = handle(env['get_youtube_auto_service'](), SimpleNamespace(**env), actor, match.group(1), action, payload, match.group(3) or '')
        respond(202 if action == 'create' else 200, result)
    except WorkflowError as exc:
        respond(exc.status, {'error': exc.code, 'message': str(exc)})
    except x_client.XAccountsClientError as exc:
        respond(exc.status_code, {'error': exc.code, 'message': str(exc)})
    except Exception:
        # OAuth/SQL errors may contain secrets. Never forward raw exceptions.
        respond(503, {'error': 'youtube_share_unavailable', 'message': '转发服务暂不可用，请保留弹窗并稍后核对结果'})
    return True

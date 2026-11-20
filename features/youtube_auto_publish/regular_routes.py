"""Cookie-authorized ordinary video actions in the existing YouTube namespace."""
import os
import re
from urllib.parse import parse_qs

from .regular import review_token, service_for
from .templates import WorkflowError, render

PREFIX = '/api/youtube-auto-publish/regular'


def edit_title(pub, actor, task_id, body):
    with pub.db(True) as db:
        row, current = pub._row(db, task_id, actor)
        if (not current.get('regular_media') or pub._actor(actor) != (row['tenant'], row['owner'])
                or row['state'] != 'review' or not body.get('review_token') or review_token(current) != body['review_token']):
            raise WorkflowError('regular_review_changed', '任务或发布安排已更新，请重新查看', 409)
        title = body.get('title', '')
        value = render(title, current['material'], 'title')
        current.update(title=value, title_template=title)
        pub._save(db, current)
    return pub.get_task(actor, task_id)


def stream_preview(handler, regular, actor, material, sha):
    info = regular.stored(actor, material, sha)
    if not info.get('snapshot'):
        raise WorkflowError('regular_preview_missing', '该素材使用原定稿预览', 404)
    path = regular.root / (sha + '.video')
    try:
        f = path.open('rb')
    except FileNotFoundError:
        raise WorkflowError('regular_preview_expired', '临时预览已清理，请重新选择该素材', 410) from None
    with f:
        size = os.fstat(f.fileno()).st_size
        if size != info['size_bytes']:
            raise WorkflowError('regular_preview_changed', '预览文件已变化，请重新选择', 409)
        start, end, status = 0, size - 1, 200
        requested = handler.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', requested)
            if match and any(match.groups()):
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(end, int(right)) if right else end
                else:
                    start = max(0, size - int(right))
            if not match or not any(match.groups()) or not 0 <= start <= end < size:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{size}')
                handler.send_header('Content-Length', '0')
                handler.end_headers()
                return
            status = 206
        handler.send_response(status)
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('Content-Type', 'video/mp4')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('ETag', '"' + sha + '"')
        if status == 206:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        left = end - start + 1
        handler.send_header('Content-Length', str(left))
        handler.end_headers()
        f.seek(start)
        try:
            while left:
                data = f.read(min(1024 * 1024, left))
                if not data:
                    break
                handler.wfile.write(data)
                left -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            return


def dispatch(handler, parsed, actor, app):
    path = parsed.path[len(PREFIX):]
    result = app['json_response']
    try:
        workflow = app['get_youtube_auto_service']()
        regular = service_for(workflow)
        material = re.fullmatch(r'/materials/([1-9][0-9]{0,18})', path)
        preview = re.fullmatch(r'/previews/([1-9][0-9]{0,18})/([0-9a-f]{64})', path)
        edit = re.fullmatch(r'/tasks/([0-9a-f]{32})/title', path)
        if handler.command == 'GET' and material:
            query = parse_qs(parsed.query)
            value = regular.describe(actor, material[1], handler.headers.get('Cookie', ''),
                                     refresh=query.get('refresh') == ['1'], poll=query.get('poll') == ['1'])
        elif handler.command == 'GET' and preview:
            return stream_preview(handler, regular, actor, *preview.groups())
        elif handler.command == 'POST' and (path in ('/titles', '/default-channel', '/tasks') or edit):
            body = handler._youtube_auto_json(32768)
            if body is None:
                return
            if not isinstance(body, dict):
                raise WorkflowError('regular_payload_invalid', '请求格式无效')
            if path == '/titles':
                value = regular.titles(actor, body)
            elif path == '/default-channel':
                value = regular.set_default(actor, body.get('material_id'), body.get('channel_id'))
            elif edit:
                value = edit_title(workflow, actor, edit[1], body)
            else:
                value = regular.prepare(actor, body)
        else:
            raise WorkflowError('not_found', '页面不存在', 404)
        result(handler, 200, value, no_store=True)
    except WorkflowError as exc:
        result(handler, exc.status, {'error': exc.code, 'message': str(exc)}, no_store=True)
    except Exception as exc:
        err = app.get('DramaSynthesisError')
        if err and isinstance(exc, err):
            result(handler, exc.status, {'error': exc.code, 'message': str(exc)}, no_store=True)
        else:
            result(handler, 503, {'error': 'regular_unavailable', 'message': '普通视频工作台暂不可用，请重试此条素材'}, no_store=True)

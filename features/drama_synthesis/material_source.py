"""Explicit upload of completed synthesis outputs to the shared material library."""
from contextlib import closing
import hashlib
import logging
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

from features.video_editing import VideoEditingError, require
from features.video_editing.material_source import MaterialSource, PRODUCT_ID, CATEGORY
from features.video_editing.store import Store, owner
from features.drama_synthesis.core import DramaSynthesisError


KINDS = {'concat_video': '合集视频', 'no_bgm_video': '去 BGM 视频', 'random_template': '随机模板视频'}
DEFAULT_DB = '/mnt/data-disk/drama-material-source/upload.sqlite3'


class UploadStore(Store):
    """Reuse the existing outbox worker with an independent, two-table ledger."""
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS ve_material_upload(
              id TEXT PRIMARY KEY, output_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL UNIQUE,
              job_id TEXT NOT NULL, material_kind TEXT NOT NULL, source_url TEXT NOT NULL,
              owner TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              UNIQUE(job_id,material_kind,source_url));
            CREATE INDEX IF NOT EXISTS synthesis_upload_job ON ve_material_upload(job_id);
            CREATE INDEX IF NOT EXISTS synthesis_upload_status ON ve_material_upload(status,created_at);
            CREATE TABLE IF NOT EXISTS ve_lease(name TEXT PRIMARY KEY,holder TEXT NOT NULL,expires_at INTEGER NOT NULL);
            ''')


def check_access(job, session):
    require(job, 'drama_job_not_found', '任务不存在', 404)
    user_id = str(session.get('user_id') or '')
    open_id = str(session.get('open_id') or '')
    require(session.get('role') == 'admin' or
            (user_id and user_id == str(job.get('creator_user_id') or '')) or
            (open_id and open_id == str(job.get('creator_open_id') or '')),
            'permission_denied', '只能上传自己创建的合成任务', 403)


class SynthesisMaterialSource(MaterialSource):
    def __init__(self, app, path=None, catalog=None, http=None):
        self.app = app
        store = UploadStore(path or os.environ.get('DRAMA_MATERIAL_SOURCE_DB', DEFAULT_DB))
        super().__init__(SimpleNamespace(app=app, store=store), catalog=catalog, http=http)

    def records(self, job_id):
        rows, _ = self.store.list('material_upload', 'job_id=?', (job_id,), limit=30)
        return [dict(self.dto(row), material_kind=row['material_kind'], source_url=row['source_url']) for row in rows]

    def validate_url(self, url):
        parsed = urlsplit(url)
        domain = str(getattr(self.app, 'COS_DOMAIN', '') or '').strip().strip('/')
        domain = urlsplit(domain if '://' in domain else 'https://' + domain).netloc
        public = urlsplit(str(getattr(self.app, 'PUBLIC_BASE_URL', '') or ''))
        allowed = bool(domain and parsed.netloc == domain) or (
            bool(public.netloc) and parsed.netloc == public.netloc and
            parsed.path.startswith(public.path.rstrip('/') + '/'))
        require(parsed.scheme == 'https' and not parsed.username and not parsed.password and
                not parsed.query and not parsed.fragment and allowed,
                'material_public_url_invalid', '合成视频的公开地址无效', 409)

    def enqueue(self, job, session, kind):
        check_access(job, session)
        require(job.get('status') == 'done', 'drama_job_not_completed', '请等待合成完成后上传', 409)
        require(kind in KINDS, 'material_kind_invalid', '请选择合成视频产物', 400)
        _, url = self.app.drama_youtube_source(job, kind)
        self.validate_url(url)
        sign = hashlib.sha256(('drama-synthesis-material-source-v1\0' + job['job_id'] + '\0' + kind + '\0' + url).encode()).hexdigest()
        record_id = sign
        existing = self.store.get('material_upload', record_id)
        if existing:
            return dict(self.dto(existing), material_kind=kind)
        country, language = self._catalog().drama(str(job['app_id']), str(job['content_id']))
        initiator = self._catalog().initiator(session.get('email'))
        actor = {'user_id': session.get('user_id') or session.get('open_id'), 'tenant_key': session.get('tenant_key', '')}
        name = str(job.get('drama_name') or job['content_id']).strip()[:160]
        name += '_' + job['job_id'] + '_' + kind + '.mp4'
        request = {'app_id': PRODUCT_ID, 'country': country, 'language': language,
                   'content_id': str(job['content_id']), 'content_sign': sign, 'url': url,
                   'name': name, 'user_id': 248, 'initiator': initiator, 'category': CATEGORY}
        data = {'request': request, 'attempts': 0, 'receipt_id': '', 'material_id': '',
                'error': '', 'next_check_at': 0, 'source_app_id': str(job['app_id'])}
        with self.store.transaction() as db:
            existing = self.store.get('material_upload', record_id, db)
            if existing:
                return dict(self.dto(existing), material_kind=kind)
            self.store.insert('material_upload', {
                'id': record_id, 'output_id': record_id, 'task_id': record_id,
                'job_id': job['job_id'], 'material_kind': kind, 'source_url': url,
                'owner': owner(actor), 'status': 'queued', 'data': data}, db)
        return dict(self.dto(self.store.get('material_upload', record_id)), material_kind=kind)

    def ensure_public_copy(self, row):
        """Synthesis already publishes immutable public videos; validate in place."""
        url = row['data']['request']['url']
        self.validate_url(url)
        current = self.app.fetch_job_row(row['job_id'])
        require(current and current.get('status') == 'done',
                'material_task_changed', '合成任务已删除或状态已变化', 409)
        _, current_url = self.app.drama_youtube_source(current, row['material_kind'])
        require(current_url == url, 'material_output_changed', '合成视频已变化，请刷新任务', 409)
        session = self.http or requests.Session()
        if self.http is None:
            session.trust_env = False
        try:
            response = session.head(url, allow_redirects=False, timeout=(5, 30))
            require(response.status_code == 200 and int(response.headers.get('Content-Length', '0')) > 0,
                    'material_public_unreadable', '合成视频暂时无法公开读取', 503)
            identity = {'size_bytes': int(response.headers['Content-Length']),
                        'etag': str(response.headers.get('ETag') or ''),
                        'sha256': str(response.headers.get('x-cos-meta-sha256') or '')}
            previous = row['data'].get('public_identity')
            require(not previous or previous == identity, 'material_output_changed', '合成视频文件已变化，请核实产物', 409)
            if not previous:
                self.change(row, row['status'], public_identity=identity)
        finally:
            if self.http is None:
                session.close()


_service = None
_lock = threading.Lock()


def get_service(app):
    global _service
    with _lock:
        if _service is None:
            _service = SynthesisMaterialSource(app)
    return _service


def decorate(job, app):
    # Auxiliary upload history must not break rendering/downloads if unavailable.
    try:
        job['material_source_uploads'] = get_service(app).records(job['job_id'])
    except Exception:
        logging.warning('synthesis material upload history unavailable')
        job['material_source_uploads'] = None
    return job


def handle(handler, method, app):
    match = re.fullmatch(r'/api/drama-material/jobs/([0-9a-f]{32})/material-source', urlsplit(handler.path).path)
    if not match:
        return False
    if not handler._require_cookie_module('drama_synthesis'):
        return True
    if method == 'POST' and not handler._require_same_origin_json():
        return True
    try:
        session = handler._session() or {}
        job = app.fetch_job_row(match.group(1))
        check_access(job, session)
        material = get_service(app)
        if method == 'GET':
            result = {'items': material.records(job['job_id'])}
        else:
            require(0 <= int(handler.headers.get('Content-Length', '0') or 0) <= 4096,
                    'request_too_large', '上传请求过大', 413)
            body = handler._read_json()
            require(isinstance(body, dict), 'request_invalid', '请求须为 JSON 对象')
            result = {'item': material.enqueue(job, session, body.get('material_kind'))}
        app.json_response(handler, 200 if method == 'GET' else 202, result, no_store=True)
    except VideoEditingError as exc:
        app.json_response(handler, exc.status, {'code': exc.code, 'error': exc.message}, no_store=True)
    except DramaSynthesisError as exc:
        app.json_response(handler, exc.status, {'code': exc.code, 'error': str(exc)}, no_store=True)
    except (ValueError, TypeError):
        app.json_response(handler, 400, {'code': 'request_invalid', 'error': '上传请求参数无效'}, no_store=True)
    except Exception:
        logging.exception('synthesis material-source request failed')
        app.json_response(handler, 503, {'code': 'material_source_unavailable', 'error': '素材入库暂不可用，请稍后刷新核实'}, no_store=True)
    return True

"""Ordinary-video preparation on top of the existing approved-cover publisher."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from . import regular_media, regular_titles
from .templates import WorkflowError

_MID = re.compile(r'[1-9][0-9]{0,18}')
_START_LOCK = threading.Lock()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def identity(material):
    return tuple(str(material.get(k) or '') for k in ('id', 'url', 'content_id', 'app_id', 'language'))


def review_token(body):
    current = body['versions'][-1]
    value = [body['id'], body['regular_media'], current['number'], current['asset_id'], body['title'],
             body['description'], body['comment'], body['channel']['channel_id'],
             body.get('publish_at', ''), body.get('schedule_version', 0)]
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def service_for(workflow):
    with _START_LOCK:
        if not hasattr(workflow, '_regular_publication'):
            workflow._regular_publication = RegularPublication(workflow)
        return workflow._regular_publication


class RegularPublication:
    def __init__(self, workflow, *, evidence=regular_media.editor_evidence,
                 stage=regular_media.stage_snapshot, generate=regular_titles.generate):
        self.workflow = workflow
        self.evidence, self.stage, self.generate = evidence, stage, generate
        self.root = workflow.root / 'regular-previews'
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='youtube-preview')
        self.pending = {}
        self.lock = threading.Lock()
        self.last_sweep = 0
        with workflow.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS youtube_regular_material(
                    tenant TEXT NOT NULL, owner TEXT NOT NULL, material_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    item TEXT NOT NULL, seen REAL NOT NULL, PRIMARY KEY(tenant,owner,material_id,fingerprint));
                CREATE INDEX IF NOT EXISTS youtube_regular_seen ON youtube_regular_material(seen);
                CREATE INDEX IF NOT EXISTS youtube_regular_sha_seen ON youtube_regular_material(fingerprint,seen);
                CREATE TABLE IF NOT EXISTS youtube_regular_channel(
                    tenant TEXT NOT NULL, owner TEXT NOT NULL, product TEXT NOT NULL, drama TEXT NOT NULL,
                    channel_local_id TEXT NOT NULL, PRIMARY KEY(tenant,owner,product,drama));
            ''')

    def _catalog(self, material_id):
        if not _MID.fullmatch(str(material_id)):
            raise WorkflowError('regular_material_invalid', '请输入有效素材 ID')
        return self.workflow.source.get(str(material_id))

    @staticmethod
    def product(material):
        return ('moboreels' if material.get('promotion_provider') == 'moboreels'
                else str(material.get('app_id') or ''))

    def default(self, actor, material):
        key = (*self.workflow._actor(actor), self.product(material), str(material.get('content_id') or ''))
        with self.workflow.db() as db:
            row = db.execute('SELECT channel_local_id FROM youtube_regular_channel '
                             'WHERE tenant=? AND owner=? AND product=? AND drama=?', key).fetchone()
        return row[0] if row else ''

    def set_default(self, actor, material_id, channel_id):
        material = self._catalog(material_id)
        drama = str(material.get('content_id') or '')
        if not drama:
            raise WorkflowError('regular_drama_required', '请先关联原剧，当前素材仍可手选发行频道', 409)
        channel_id = str(channel_id or '')
        if channel_id:
            self.workflow._template_channel(actor, channel_id)
        key = (*self.workflow._actor(actor), self.product(material), drama)
        with self.workflow.db(True) as db:
            if channel_id:
                db.execute('INSERT INTO youtube_regular_channel VALUES(?,?,?,?,?) '
                           'ON CONFLICT(tenant,owner,product,drama) DO UPDATE SET channel_local_id=excluded.channel_local_id',
                           (*key, channel_id))
            else:
                db.execute('DELETE FROM youtube_regular_channel WHERE tenant=? AND owner=? AND product=? AND drama=?', key)
        return {'channel_id': channel_id}

    def _save(self, actor, material, media):
        w, h, ms = (int(media[k]) for k in ('width', 'height', 'duration_ms'))
        size = int(media.get('size_bytes') or media.get('size') or 0)
        if min(w, h, ms, size) <= 0 or not regular_media.SHA.fullmatch(str(media['sha256'])):
            raise WorkflowError('regular_media_invalid', '当前定稿的视频元数据无效', 409)
        expected = regular_media.editor_object_sha(material['url'])
        if expected and expected != media['sha256']:
            raise WorkflowError('regular_editor_changed', '正式素材文件与定稿版本不符，请核对该条素材', 409)
        item = {k: media.get(k) for k in ('sha256', 'task_id', 'version', 'snapshot', 'evidence')}
        item.update(width=w, height=h, duration_ms=ms, size_bytes=size, material_id=str(material['id']),
                    source_identity=identity(material), url=material['url'])
        stamp = self.workflow._actor(actor)
        with self.workflow.db(True) as db:
            db.execute('INSERT INTO youtube_regular_material VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(tenant,owner,material_id,fingerprint) DO UPDATE SET item=excluded.item,seen=excluded.seen',
                       (*stamp, item['material_id'], item['sha256'], canonical(item), time.time()))
        return item

    def stored(self, actor, material_id, sha):
        if not _MID.fullmatch(str(material_id)) or not regular_media.SHA.fullmatch(str(sha or '')):
            raise WorkflowError('regular_preview_required', '请先选择素材并完成成片预览', 409)
        with self.workflow.db() as db:
            row = db.execute('SELECT item FROM youtube_regular_material WHERE tenant=? AND owner=? AND material_id=? '
                             'AND fingerprint=?', (*self.workflow._actor(actor), str(material_id), sha)).fetchone()
        if row is None:
            raise WorkflowError('regular_preview_missing', '未找到当前账号的成片预览，请重新选材', 404)
        return json.loads(row[0])

    def _sweep(self):
        now = time.time()
        if now - self.last_sweep < 3600:
            return
        self.last_sweep = now
        # Only discard reproducible previews after inactivity. Never evict a file
        # referenced by a preparation still waiting for review, upload or recovery.
        with self.workflow.db(True) as db:
            cutoff = now - 7 * 86400
            if db.execute('SELECT 1 FROM youtube_regular_material WHERE seen<? LIMIT 1', (cutoff,)).fetchone():
                has_ledger = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='drama_youtube_publish'").fetchone()
                # Scan active preparations once per sweep. The preparation row stays
                # 'uploading' after publication; only the native ledger knows the outcome.
                sql = ("SELECT DISTINCT json_extract(p.body,'$.regular_media.sha256') AS sha FROM youtube_auto_preparation p "
                       "WHERE p.state<>'cancelled' AND json_type(p.body,'$.regular_media')='object'")
                if has_ledger:
                    sql += (" AND NOT EXISTS(SELECT 1 FROM drama_youtube_publish l WHERE l.preparation_id=p.id "
                            "AND l.workflow='reviewed_thumbnail' AND l.unknown_outcome=0 "
                            "AND (l.video_state='published' OR l.schedule_status='cancelled'))")
                query = ('SELECT fingerprint FROM youtube_regular_material WHERE fingerprint NOT IN '
                         '(SELECT sha FROM (' + sql + ') WHERE sha IS NOT NULL) '
                         'GROUP BY fingerprint HAVING MAX(seen)<? LIMIT 64')
                for row in db.execute(query, (cutoff,)).fetchall():
                    sha = row[0]
                    (self.root / (sha + '.video')).unlink(missing_ok=True)
                    db.execute('DELETE FROM youtube_regular_material WHERE fingerprint=? AND seen<?', (sha, cutoff))
        if self.root.is_dir():
            for partial in self.root.glob('.probe-*'):
                if partial.stat().st_mtime < now - 86400:
                    partial.unlink(missing_ok=True)

    def _legacy(self, actor, material, refresh):
        key = (*self.workflow._actor(actor), str(material['id']), material['url'])
        with self.lock:
            pending = self.pending.get(key)
            future = pending[0] if pending else None
            if future and future.done():
                del self.pending[key]
                return future.result()
            if not future and not refresh:
                with self.workflow.db() as db:
                    rows = db.execute('SELECT item FROM youtube_regular_material WHERE tenant=? AND owner=? AND material_id=? '
                                      'AND seen>? ORDER BY seen DESC LIMIT 8', (*key[:3], time.time() - 86400)).fetchall()
                for row in rows:
                    item = json.loads(row[0])
                    if (item.get('snapshot') and tuple(item['source_identity']) == identity(material)
                            and (self.root / (item['sha256'] + '.video')).is_file()):
                        return item
            if not future:
                # A closed browser can leave a completed Future. Successful results
                # are durable, and discarded failures can be retried by selecting again.
                for closed in [k for k, (done, _, started) in self.pending.items()
                               if done.done() and (time.monotonic() - started > 3600 or len(self.pending) > 128)]:
                    self.pending.pop(closed)
                if sum(not running.done() for running, _, _ in self.pending.values()) >= 10:
                    raise WorkflowError('regular_probe_busy', '其他素材正在核对，可稍后重试此条', 503)
                def probe():
                    self._sweep()
                    info = self.stage(material, self.root, self.workflow.source.allowed_hosts)
                    return self._save(actor, material, info)
                self.pending[key] = (self.pool.submit(probe), material, time.monotonic())
        return None

    @staticmethod
    def preview_url(item):
        if item['snapshot']:
            return f"/api/youtube-auto-publish/regular/previews/{item['material_id']}/{item['sha256']}"
        if not re.fullmatch(r'at_[0-9a-f]{32}', str(item.get('task_id') or '')):
            raise WorkflowError('regular_editor_changed', '定稿作品编号无效，请核对该素材', 409)
        return f"/api/ai-video-editing/tasks/{item['task_id']}/media?v={item['sha256']}"

    def describe(self, actor, material_id, cookie, *, refresh=False, poll=False):
        if poll and not refresh:
            scope = (*self.workflow._actor(actor), str(material_id))
            with self.lock:
                key = next((k for k in self.pending if k[:3] == scope), None)
                if key is not None:
                    pending, material, _ = self.pending[key]
                    if not pending.done():
                        return {'status': 'checking', 'material': material}
                    del self.pending[key]
            if key is not None:
                return self._ready(actor, material, pending.result())
        material = self._catalog(material_id)
        # Editor receipts carry signed-in source evidence. Existing library material
        # outside that workflow is independently identified by its downloaded bytes.
        try:
            media = self.evidence(str(material_id), cookie)
        except WorkflowError:
            if '/ai-video-editing/youtube-final/v1/' in material['url']:
                raise
            media = None
        if media:
            if (str(media.get('content_id')) != str(material.get('content_id')) or media.get('url') != material['url']
                    or str(media.get('language') or '') != str(material.get('language') or '')
                    or (self.product(material) == 'moboreels') != (media.get('product') == 'moboreels')):
                raise WorkflowError('regular_editor_changed', '定稿与正式素材的地址、原剧或语言发生变化，请核对素材', 409)
            item = self._save(actor, material, media)
        else:
            if regular_media.editor_object_sha(material['url']):
                raise WorkflowError('regular_editor_receipt_required',
                                    '这条定稿尚未读到版本回执，请用入库账号重新登录并核对该素材', 409)
            item = self._legacy(actor, material, refresh)
        if not item:
            return {'status': 'checking', 'material': material}
        return self._ready(actor, material, item)

    def _ready(self, actor, material, item):
        preview = self.preview_url(item)
        return dict(status='ready', material=material, media={k: item.get(k) for k in (
                    'sha256', 'task_id', 'version', 'width', 'height', 'duration_ms')},
                    preview_url=preview, clip_evidence=bool(item.get('evidence')),
                    shorts=item['width'] <= item['height'] and item['duration_ms'] <= 180000,
                    default_channel=self.default(actor, material))

    def titles(self, actor, body):
        channel_id = str(body.get('channel_id') or '').strip()
        if not channel_id:
            raise WorkflowError('regular_title_channel', '请先选择发布频道，再生成对应语言的标题')
        template = self.workflow.get_channel_template(actor, channel_id)['template']
        media = self.stored(actor, body.get('material_id'), body.get('sha256'))
        material = self._catalog(media['material_id'])
        if tuple(media['source_identity']) != identity(material):
            raise WorkflowError('regular_media_changed', '素材已更新，请重新选择', 409)
        language = regular_titles.title_language(template.get('language'), material.get('language'))
        prompt = regular_titles.title_prompt(body.get('mode'), material, media.get('evidence'),
                                            body.get('synopsis', ''), body.get('clip_notes', ''), language['language'])
        return {**self.generate(prompt, self.workflow.root), **language}

    def prepare(self, actor, body):
        item = self.stored(actor, body.get('material_id'), body.get('sha256'))
        if item['width'] <= item['height'] and item['duration_ms'] <= 180000:
            raise WorkflowError('regular_would_be_short', '这条竖屏或方形视频不超过 3 分钟，请调整成片后再发普通视频', 409)
        media = {k: item[k] for k in ('material_id', 'sha256', 'size_bytes', 'width', 'height', 'duration_ms', 'task_id', 'version',
                                      'source_identity', 'snapshot')}
        media['preview_url'] = self.preview_url(item)
        return self.workflow.create_task(actor, body, regular_media=media)

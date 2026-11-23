"""Authenticated smart-edit analysis receipts and plans. No extra web listener."""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
import tempfile
import threading
import time
import uuid

from .smart_common import SmartError, require, encode
from .smart_media import analyze_file, vision_service
from .smart_model import model_config
from .smart_plans import build_plans


def owner(actor):
    require(isinstance(actor, dict) and str(actor.get('user_id', '')).strip(),
            'authentication_required', '请先登录', 401)
    return encode([str(actor.get('tenant_key') or ''), str(actor['user_id'])])


def connect(runtime):
    db = sqlite3.connect(str(runtime.db_path), timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def transaction(runtime):
    with closing(connect(runtime)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


LOG = logging.getLogger(__name__)
LEASE = 90
JOB_ID = re.compile(r'sa_[a-f0-9]{32}')
_lock = threading.Lock()
_threads = {}


def setup(runtime):
    with closing(connect(runtime)) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS ai_edit_smart_analysis(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            sources TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', lease TEXT NOT NULL DEFAULT '',
            expires INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL, updated INTEGER NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS ai_edit_smart_fingerprint ON ai_edit_smart_analysis(owner,fingerprint,created)')
        db.execute('CREATE INDEX IF NOT EXISTS ai_edit_smart_pending ON ai_edit_smart_analysis(status,expires,created)')
    from . import source_jobs
    source_jobs.setup(runtime)


def snapshot(asset):
    sid = asset['id']
    duration = asset.get('duration_ms')
    sha = asset.get('sha256')
    require(type(duration) is int and duration >= 34, 'smart_duration_invalid', '素材缺少有效时长或不足一帧', 409)
    require(isinstance(sha, str) and re.fullmatch(r'[0-9a-fA-F]{64}', sha), 'smart_source_not_ready',
            '素材缺少已校验的内容指纹', 409)
    require(type(asset.get('has_audio')) is bool, 'smart_source_not_ready', '素材缺少已校验的音轨信息', 409)
    return {'source_id': sid, 'title': str(asset.get('name') or sid), 'series_code': asset.get('series_code') or '',
            'duration_ms': duration, '_stamp': sha.lower(), 'candidates': [], 'engine': '', 'warnings': []}


def source_snapshots(runtime, actor, ids):
    require(isinstance(ids, list) and 1 <= len(ids) <= 20 and all(isinstance(s, str) and
            re.fullmatch(r'[A-Za-z0-9_-]{1,128}', s) for s in ids) and len(set(ids)) == len(ids),
            'smart_sources_invalid', '请选择 1 至 20 条不同的已就绪素材')
    return [snapshot(runtime.asset(sid, actor)) for sid in ids]


def dto(record):
    return {'analysis_id': record['id'], 'status': record['status'], 'error': record['error'],
            'sources': [{k: v for k, v in s.items() if not k.startswith('_')} for s in json.loads(record['sources'])],
            'created_at': record['created'], 'updated_at': record['updated']}


def submit(runtime, actor, body):
    require(isinstance(body, dict) and set(body) == {'source_ids'}, 'smart_sources_invalid', '请提交素材编号列表')
    snapshots = source_snapshots(runtime, actor, body['source_ids'])
    fingerprint = hashlib.sha256(encode(['smart-v2-emotion', model_config(), [(s['source_id'], s['_stamp'], s['series_code'])
                                                                     for s in snapshots]]).encode()).hexdigest()
    now = int(time.time())
    who = owner(actor)
    with transaction(runtime) as db:
        previous = db.execute("""SELECT * FROM ai_edit_smart_analysis WHERE owner=? AND fingerprint=?
                                AND (status IN ('queued','running') OR (status='succeeded' AND updated>?))
                                ORDER BY created DESC LIMIT 1""", (who, fingerprint, now - 86400)).fetchone()
        if previous:
            degraded = previous['status'] == 'succeeded' and any(model_config()[:2]) and any(
                s.get('engine') != 'multimodal' for s in json.loads(previous['sources']))
            if not degraded or previous['updated'] > now - 120:
                return dto(previous)
        count = db.execute("SELECT COUNT(*) FROM ai_edit_smart_analysis WHERE owner=? AND status IN ('queued','running')",
                           (who,)).fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_source_batches'").fetchone():
            count += db.execute("SELECT count(*) FROM studio_source_batches WHERE owner=? AND status='preparing'",(who,)).fetchone()[0]
        require(count < 3, 'smart_queue_full', '已有三个素材分析任务待处理，请等待其中一项完成', 429)
        jid = 'sa_' + uuid.uuid4().hex
        db.execute("""INSERT INTO ai_edit_smart_analysis (id,owner,fingerprint,status,sources,created,updated)
                      VALUES(?,?,?,'queued',?,?,?)""", (jid, who, fingerprint, encode(snapshots), now, now))
        return dto(db.execute('SELECT * FROM ai_edit_smart_analysis WHERE id=?', (jid,)).fetchone())


def receipt(runtime, actor, jid):
    require(isinstance(jid, str) and JOB_ID.fullmatch(jid), 'smart_analysis_not_found', '分析记录不存在', 404)
    with closing(connect(runtime)) as db:
        row = db.execute('SELECT * FROM ai_edit_smart_analysis WHERE id=? AND owner=?', (jid, owner(actor))).fetchone()
    require(row is not None, 'smart_analysis_not_found', '分析记录不存在', 404)
    frozen = json.loads(row['sources'])
    current = source_snapshots(runtime, actor, [s['source_id'] for s in frozen])
    require(all(s['_stamp'] == c['_stamp'] and s['duration_ms'] == c['duration_ms']
                for s, c in zip(frozen, current)), 'smart_source_changed', '素材版本或归属已变化，请重新分析', 409)
    return dto(row)


def process_source(runtime, actor, snap, workdir):
    asset = runtime.asset(snap['source_id'], actor)
    if asset.get('_cos'):
        from .cloud_media import materialize
        materialize(runtime, asset)
    current = snapshot(asset)
    require(current['_stamp'] == snap['_stamp'] and current['duration_ms'] == snap['duration_ms'],
            'smart_source_changed', '素材版本已变化，请重新分析', 409)
    path = Path(asset['path'])
    require(path.is_absolute() and path.is_file(), 'smart_source_not_ready', '素材本机文件尚未就绪', 409)
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    require(digest.hexdigest() == snap['_stamp'], 'smart_source_changed', '文件内容与素材指纹不符，请重新入库', 409)
    from .source_jobs import vision as shared_vision
    vision = shared_vision if any(model_config()[:2]) else None
    return analyze_file(path, snap['duration_ms'], asset['has_audio'], workdir, vision=vision)


def assemble(runtime, actor, snapshots):
    """Publish an ordered parent receipt from completed content-cache children."""
    require(snapshots and all(s.get('engine') for s in snapshots),'smart_analysis_not_ready','所选素材尚未全部解析完成',409)
    fingerprint = hashlib.sha256(encode(['smart-v2-emotion',model_config(),[(s['source_id'],s['_stamp'],s['series_code']) for s in snapshots]]).encode()).hexdigest()
    now=int(time.time());who=owner(actor)
    with transaction(runtime) as db:
        prior=db.execute("SELECT * FROM ai_edit_smart_analysis WHERE owner=? AND fingerprint=? AND status='succeeded' AND updated>? ORDER BY created DESC LIMIT 1",(who,fingerprint,now-86400)).fetchone()
        if prior:
            degraded=any(model_config()[:2]) and any(s.get('engine')!='multimodal' for s in json.loads(prior['sources']))
            if not degraded or prior['updated']>now-120:
                return dto(prior)
        jid='sa_'+uuid.uuid4().hex
        db.execute("INSERT INTO ai_edit_smart_analysis(id,owner,fingerprint,status,sources,created,updated) VALUES(?,?,?,'succeeded',?,?,?)",(jid,who,fingerprint,encode(snapshots),now,now))
        return dto(db.execute('SELECT * FROM ai_edit_smart_analysis WHERE id=?',(jid,)).fetchone())


def claim(runtime):
    now = int(time.time())
    with transaction(runtime) as db:
        if db.execute("SELECT 1 FROM ai_edit_smart_analysis WHERE status='running' AND expires>? LIMIT 1", (now,)).fetchone():
            return None
        db.execute("""UPDATE ai_edit_smart_analysis SET status='failed',error='素材分析多次中断，请重新提交',
                      lease='',expires=0,updated=? WHERE status='running' AND expires<=? AND attempts>=3""", (now, now))
        row = db.execute("""SELECT * FROM ai_edit_smart_analysis WHERE status='queued' OR (status='running' AND expires<=?)
                          ORDER BY created,id LIMIT 1""", (now,)).fetchone()
        if not row:
            return None
        token = uuid.uuid4().hex
        db.execute("""UPDATE ai_edit_smart_analysis SET status='running',lease=?,expires=?,attempts=attempts+1,
                      updated=? WHERE id=?""", (token, now + LEASE, now, row['id']))
        tenant, user = json.loads(row['owner'])
        return row['id'], token, {'tenant_key': tenant, 'user_id': user}, json.loads(row['sources'])


def execute_one(runtime):
    job = claim(runtime)
    if not job:
        return False
    jid, token, actor, snapshots = job
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(LEASE // 3):
            try:
                with closing(connect(runtime)) as db:
                    if db.execute("UPDATE ai_edit_smart_analysis SET expires=? WHERE id=? AND lease=?",
                                  (int(time.time()) + LEASE, jid, token)).rowcount != 1:
                        return
            except Exception:
                LOG.warning('smart analysis heartbeat delayed for %s', jid)
    pulse = threading.Thread(target=heartbeat, name='smart-edit-heartbeat', daemon=True)
    pulse.start()
    status, error = 'succeeded', ''
    try:
        from .source_jobs import collect
        pending = [s for s in snapshots if not s.get('engine')]
        rows = collect(runtime, actor, pending, retry=True)
        for snap, row in zip(pending, rows):
            snap['status'] = row['status']
            snap['error'] = row['error']
            if row['status'] == 'succeeded':
                snap.update(json.loads(row['result']))
            else:
                status = 'failed'
                error = '部分素材解析失败，请重试失败项'
        with closing(connect(runtime)) as db:
            if db.execute('UPDATE ai_edit_smart_analysis SET sources=?,updated=? WHERE id=? AND lease=?',
                          (encode(snapshots), int(time.time()), jid, token)).rowcount != 1:
                return True
    except Exception as exc:
        status = 'failed'
        error = str(exc)[:250] if isinstance(exc, SmartError) else '素材分析未能完成，请稍后重试'
        LOG.warning('smart analysis %s failed (%s)', jid, getattr(exc, 'code', type(exc).__name__))
    finally:
        stopped.set()
        pulse.join(2)
    with closing(connect(runtime)) as db:
        db.execute('UPDATE ai_edit_smart_analysis SET status=?,error=?,expires=0,lease=\'\',updated=? WHERE id=? AND lease=?',
                   (status, error, int(time.time()), jid, token))
    return True


def wake(runtime):
    path = str(runtime.db_path)
    def consume():
        try:
            while execute_one(runtime):
                pass
        except Exception as exc:
            LOG.warning('smart analysis dispatcher unavailable (%s)', type(exc).__name__)
        finally:
            with _lock:
                _threads.pop(path, None)
    with _lock:
        if path not in _threads:
            runner = threading.Thread(target=consume, name='video-edit-smart', daemon=True)
            _threads[path] = runner
            runner.start()


def dispatch(runtime, actor, method, path, body):
    owner(actor)
    setup(runtime)
    if path.startswith('/api/ai-video-editing/'):
        path = path[len('/api/ai-video-editing'):]
    if method == 'POST' and path == '/analyses':
        item = submit(runtime, actor, body)
        if item['status'] in ('queued', 'running'):
            wake(runtime)
        return 202, item
    if method == 'GET' and path.startswith('/analyses/'):
        item = receipt(runtime, actor, path[len('/analyses/'):])
        if item['status'] in ('queued', 'running'):
            wake(runtime)
        return 200, item
    if method == 'POST' and path == '/plans':
        item = receipt(runtime, actor, body.get('analysis_id'))
        require(item['status'] == 'succeeded', 'smart_analysis_not_ready', '请等待素材分析成功后生成方案', 409)
        return 200, build_plans(item['sources'], body)
    raise SmartError('not_found', '接口不存在', 404)


def serve_worker(runtime, stop=None):
    """Optional standalone consumer; pass the same independent Runtime as HTTP."""
    setup(runtime)
    stop = stop if stop is not None else threading.Event()
    while not stop.is_set():
        if not execute_one(runtime):
            stop.wait(5)

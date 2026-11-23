"""Owner-scoped, content-addressed analysis work shared by ordered selections."""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .smart_common import encode

LOG = logging.getLogger(__name__)
VERSION = 'smart-v2-emotion/source-v1'
LEASE = 120
_vision = threading.Lock()
_metrics = threading.local()
_threads = {}
_lock = threading.Lock()


def setup(rt):
    from .smart_api import connect
    with closing(connect(rt)) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS editor_source_jobs(
          id TEXT PRIMARY KEY,owner TEXT NOT NULL,fingerprint TEXT NOT NULL,
          asset_id TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
          lease TEXT NOT NULL DEFAULT '',expires REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,
          started REAL NOT NULL DEFAULT 0,finished REAL NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS editor_source_identity ON editor_source_jobs(owner,fingerprint,created);
          CREATE INDEX IF NOT EXISTS editor_source_pending ON editor_source_jobs(status,expires,created);''')
        if 'timings' not in {r[1] for r in db.execute('PRAGMA table_info(editor_source_jobs)')}:
            db.execute("ALTER TABLE editor_source_jobs ADD COLUMN timings TEXT NOT NULL DEFAULT '{}'")


def fingerprint(asset):
    from .smart_model import model_config
    return hashlib.sha256(encode([VERSION, model_config(), asset['sha256'].lower(),
                                 asset['duration_ms'], asset['has_audio']]).encode()).hexdigest()


def legacy_result(db, who, snap, now):
    """Reuse verified, same-engine pre-upgrade receipts without changing history."""
    from .smart_model import model_config
    config = model_config()
    for row in db.execute("SELECT fingerprint,sources,updated FROM ai_edit_smart_analysis WHERE owner=? AND status='succeeded' AND updated>? ORDER BY updated DESC LIMIT 50", (who, now - 86400)):
        sources = json.loads(row['sources'])
        key = hashlib.sha256(encode(['smart-v2-emotion', config,
                [(s['source_id'], s['_stamp'], s['series_code']) for s in sources]]).encode()).hexdigest()
        if key != row['fingerprint']:
            continue
        for s in sources:
            if s.get('_stamp') == snap['_stamp'] and s.get('duration_ms') == snap['duration_ms'] and s.get('engine'):
                if any(config[:2]) and s['engine'] != 'multimodal' and row['updated'] <= now - 120:
                    continue
                return {k: v for k, v in s.items() if k not in ('source_id','title','series_code','duration_ms','_stamp','status','error')}
    return None


def enqueue(rt, actor, asset_id, retry=False):
    from . import smart_api
    from .smart_model import model_config
    asset = rt.asset(asset_id, actor)
    snap = smart_api.snapshot(asset)
    key, now, who = fingerprint(asset), time.time(), smart_api.owner(actor)
    with smart_api.transaction(rt) as db:
        row = db.execute('SELECT * FROM editor_source_jobs WHERE owner=? AND fingerprint=? ORDER BY created DESC LIMIT 1', (who, key)).fetchone()
        if row:
            if retry and row['status'] == 'failed':
                db.execute("UPDATE editor_source_jobs SET status='queued',error='',attempts=0,lease='',expires=0,updated=? WHERE id=? AND status='failed'", (now,row['id']))
                row = db.execute('SELECT * FROM editor_source_jobs WHERE id=?',(row['id'],)).fetchone()
            result = json.loads(row['result'])
            ttl = 120 if any(model_config()[:2]) and result.get('engine') != 'multimodal' else 86400
            if row['status'] != 'succeeded' or row['updated'] > now - ttl:
                return dict(row)
        result = legacy_result(db, who, snap, now)
        jid = 'sj_' + uuid.uuid4().hex
        db.execute('INSERT INTO editor_source_jobs(id,owner,fingerprint,asset_id,snapshot,status,result,created,updated,finished) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (jid,who,key,asset_id,encode(snap),'succeeded' if result else 'queued',encode(result or {}),now,now,now if result else 0))
        return dict(db.execute('SELECT * FROM editor_source_jobs WHERE id=?',(jid,)).fetchone())


def read(rt, actor, jid):
    from .smart_api import connect, owner
    from .runtime import check
    with closing(connect(rt)) as db:
        row = db.execute('SELECT * FROM editor_source_jobs WHERE id=? AND owner=?',(jid,owner(actor))).fetchone()
    check(row, 'source_job_missing', '解析记录不存在', 404)
    rt.asset(row['asset_id'], actor)
    return dict(row)


def claim(rt):
    from .smart_api import transaction
    now = time.time()
    with transaction(rt) as db:
        if db.execute("SELECT count(*) FROM editor_source_jobs WHERE status='running' AND expires>?",(now,)).fetchone()[0] >= 2:
            return None
        db.execute("UPDATE editor_source_jobs SET status='failed',error='解析多次中断，请重试该素材',lease='',expires=0,updated=? WHERE status='running' AND expires<? AND attempts>=3",(now,now))
        row = db.execute("SELECT * FROM editor_source_jobs WHERE status='queued' OR (status='running' AND expires<? AND attempts<3) ORDER BY created,id LIMIT 1",(now,)).fetchone()
        if not row:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE editor_source_jobs SET status='running',lease=?,expires=?,attempts=attempts+1,started=?,updated=? WHERE id=?",(token,now+LEASE,now,now,row['id']))
        return dict(row,lease=token)


def vision(sheets):
    from .smart_media import vision_service
    waiting=time.monotonic()
    with _vision:
        began=time.monotonic();_metrics.wait_ms=round((began-waiting)*1000)
        try:return vision_service(sheets)
        finally:_metrics.model_ms=round((time.monotonic()-began)*1000)


def notify(rt):
    for name in ('_selection_wake','_studio_wake','_source_wake'):
        event = getattr(rt, name, None)
        if event:
            event.set()


def execute_one(rt):
    from . import smart_api
    job = claim(rt)
    if not job:
        return False
    tenant,user = json.loads(job['owner'])
    actor = {'tenant_key':tenant,'user_id':user}
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(20):
            try:
                with closing(smart_api.connect(rt)) as db:
                    changed = db.execute("UPDATE editor_source_jobs SET expires=? WHERE id=? AND lease=? AND status='running'",(time.time()+LEASE,job['id'],job['lease'])).rowcount
                if not changed:
                    return
            except Exception:
                LOG.warning('Source heartbeat delayed %s',job['id'])
    beat = threading.Thread(target=heartbeat,daemon=True)
    beat.start()
    result,status,error = {},'succeeded',''
    began=time.monotonic();_metrics.wait_ms=0;_metrics.model_ms=0
    try:
        root = Path(os.environ.get('AI_VIDEO_EDITING_SMART_WORK_ROOT') or (Path(rt.db_path).parent/'smart-work'))
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(prefix=job['id']+'-',dir=root) as folder:
            result = smart_api.process_source(rt,actor,json.loads(job['snapshot']),folder)
    except Exception as exc:
        status = 'failed'
        error = getattr(exc,'message','') or ('素材解析未完成，请重试该素材')
        LOG.warning('Source analysis failed %s %s',job['id'],type(exc).__name__)
    finally:
        stop.set(); beat.join(1)
    timings=result.pop('_timings',{})
    timings.update(queue_ms=round(max(0,time.time()-job['created']-(time.monotonic()-began))*1000),total_ms=round((time.monotonic()-began)*1000),model_wait_ms=_metrics.wait_ms,model_call_ms=_metrics.model_ms)
    with closing(smart_api.connect(rt)) as db:
        db.execute("UPDATE editor_source_jobs SET status=?,result=?,error=?,lease='',expires=0,updated=?,finished=?,timings=? WHERE id=? AND lease=?",(status,encode(result),error[:400],time.time(),time.time(),encode(timings),job['id'],job['lease']))
    notify(rt)
    return True


def start(rt):
    setup(rt)
    with _lock:
        key = str(rt.db_path)
        if key in _threads:
            return
        rt._source_wake = threading.Event()
        def loop():
            while True:
                try:
                    if execute_one(rt):
                        continue
                except Exception:
                    LOG.exception('Source dispatcher delayed')
                rt._source_wake.wait(1)
                rt._source_wake.clear()
        _threads[key] = [threading.Thread(target=loop,name='editor-source-'+str(i),daemon=True) for i in range(2)]
        for thread in _threads[key]:
            thread.start()


def collect(rt, actor, snapshots, retry=False):
    """Synchronous parent worker; expensive work is still bounded by DB claims."""
    jobs = [enqueue(rt,actor,s['source_id'],retry=retry) for s in snapshots]
    while True:
        rows = [read(rt,actor,j['id']) for j in jobs]
        if all(r['status'] in ('succeeded','failed') for r in rows):
            return rows
        if not execute_one(rt):
            time.sleep(.1)

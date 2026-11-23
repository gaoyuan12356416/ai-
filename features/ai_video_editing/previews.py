"""Private, optional poster/proxy derivatives; never on the creation critical path."""
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time

from .runtime import Runtime, check, ident, owner, PREFIX

PROFILE='preview-854-h264-v1'
LOG=logging.getLogger(__name__)


def root(rt):
    return Path(os.environ.get('AI_VIDEO_EDITING_PREVIEW_ROOT') or (rt.root.parent/'ai-video-editing-preview')).resolve()


def setup(rt):
    with rt.db(True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS editor_previews(
          id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,owner TEXT NOT NULL,sha256 TEXT NOT NULL,profile TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',poster INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
          priority INTEGER NOT NULL DEFAULT 0,lease TEXT NOT NULL DEFAULT '',expires REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,not_before REAL NOT NULL DEFAULT 0,
          UNIQUE(asset_id,sha256,profile))''')


def decorations(rt, aid):
    try:
        with rt.db() as db:
            row=db.execute('SELECT * FROM editor_previews WHERE asset_id=? AND profile=? ORDER BY created DESC LIMIT 1',(aid,PROFILE)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        row=None
    if not row:
        return {'poster_url':None,'proxy_url':None,'preview_status':'not_requested','preview_error':''}
    base=PREFIX+'/assets/'+aid
    return {'poster_url':base+'/thumbnail?v='+row['id'] if row['poster'] else None,
            'proxy_url':base+'/preview?v='+row['id'] if row['status']=='ready' else None,
            'preview_status':row['status'],'preview_error':row['error']}


def metadata(rt, actor, aid):
    rt.asset(aid,actor)
    return decorations(rt,aid)


def request(rt, actor, aid, priority=0, retry=False):
    asset=rt.asset(aid,actor);now=time.time()
    with rt.db(True) as db:
        db.execute('INSERT OR IGNORE INTO editor_previews(id,asset_id,owner,sha256,profile,priority,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                   (ident('pv'),aid,owner(actor),asset['sha256'],PROFILE,priority,now,now))
        db.execute('UPDATE editor_previews SET priority=max(priority,?) WHERE asset_id=? AND sha256=? AND profile=?',(priority,aid,asset['sha256'],PROFILE))
        if retry:
            db.execute("UPDATE editor_previews SET status='queued',error='',attempts=0,not_before=0,updated=? WHERE asset_id=? AND sha256=? AND profile=? AND status='failed'",(now,aid,asset['sha256'],PROFILE))
    return decorations(rt,aid)


def file(rt,actor,aid,kind):
    asset=rt.asset(aid,actor)
    with rt.db() as db:
        row=db.execute('SELECT * FROM editor_previews WHERE asset_id=? AND owner=? AND sha256=? AND profile=?',(aid,owner(actor),asset['sha256'],PROFILE)).fetchone()
    check(row and ((kind=='thumbnail' and row['poster']) or (kind=='preview' and row['status']=='ready')),'preview_not_ready','预览尚未就绪，可先播放原片',409)
    name=row['id']+('.jpg' if kind=='thumbnail' else '.mp4')
    path=root(rt)/name
    check(path.is_file(),'preview_missing','预览文件暂不可用，请重试准备预览',404)
    return path,('image/jpeg' if kind=='thumbnail' else 'video/mp4'),row['id']+('-poster' if kind=='thumbnail' else '-proxy')


def claim(rt):
    now=time.time()
    with rt.db(True) as db:
        if db.execute("SELECT 1 FROM editor_previews WHERE status='running' AND expires>? LIMIT 1",(now,)).fetchone():
            return None
        db.execute("UPDATE editor_previews SET status='failed',error='预览准备多次中断，可重试；原片仍可使用',updated=? WHERE status='running' AND expires<? AND attempts>=3",(now,now))
        row=db.execute("SELECT * FROM editor_previews WHERE (status='queued' AND not_before<=?) OR (status='running' AND expires<? AND attempts<3) ORDER BY poster,priority DESC,created LIMIT 1",(now,now)).fetchone()
        if not row:
            return None
        lease=ident('lease')
        db.execute("UPDATE editor_previews SET status='running',lease=?,expires=?,attempts=attempts+1,updated=? WHERE id=?",(lease,now+120,now,row['id']))
        return dict(row,lease=lease)


def process(rt,job):
    tenant,user=json.loads(job['owner']);actor={'tenant_key':tenant,'user_id':user}
    asset=rt.asset(job['asset_id'],actor);source=Path(asset['path'])
    if not source.is_file():
        with rt.db(True) as db:
            active=db.execute("SELECT 1 FROM editor_source_jobs WHERE asset_id=? AND status IN ('queued','running') LIMIT 1",(job['asset_id'],)).fetchone()
            waiting=bool(active) or time.time()-job['created']<60
            db.execute("UPDATE editor_previews SET status=?,error=?,lease='',expires=0,attempts=max(0,attempts-1),not_before=?,updated=? WHERE id=? AND lease=?",
                       ('queued' if waiting else 'failed','' if waiting else '原片尚未缓存，解析后可重试预览',time.time()+5,time.time(),job['id'],job['lease']))
        return
    folder=root(rt);folder.mkdir(parents=True,exist_ok=True,mode=0o750)
    temp=folder/(job['id']+'-'+job['lease']+'.mp4')
    poster=folder/(job['id']+'.jpg');video=folder/(job['id']+'.mp4')
    done=threading.Event()
    def heartbeat():
        while not done.wait(20):
            try:
                with rt.db(True) as db:
                    if not db.execute("UPDATE editor_previews SET expires=? WHERE id=? AND lease=?",(time.time()+120,job['id'],job['lease'])).rowcount:return
            except Exception:LOG.warning('Preview heartbeat delayed %s',job['id'])
    pulse=threading.Thread(target=heartbeat,daemon=True);pulse.start()
    began=time.monotonic()
    try:
        digest=hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        check(digest.hexdigest()==job['sha256'],'preview_source_changed','原片指纹已变化',409)
        ffmpeg=os.environ.get('AI_VIDEO_EDITING_FFMPEG','ffmpeg')
        base=[ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-threads','2','-filter_threads','1']
        if not poster.exists():
            part=folder/(job['id']+'-'+job['lease']+'.jpg')
            try:
                subprocess.run(base+['-ss',str(min(.1,asset['duration_ms']/2000)),'-i',str(source),'-an','-vf',"scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",'-frames:v','1',str(part)],check=True,capture_output=True,timeout=60)
                os.replace(part,poster);os.chmod(poster,0o640)
            finally:
                part.unlink(missing_ok=True)
            with rt.db(True) as db:
                db.execute("UPDATE editor_previews SET poster=1,status='queued',lease='',expires=0,attempts=0,updated=? WHERE id=? AND lease=?",(time.time(),job['id'],job['lease']))
            return
        subprocess.run(base+['-i',str(source),'-map','0:v:0','-map','0:a:0?',
             '-vf',"scale='min(854,iw)':'min(854,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
             '-fpsmax','30','-c:v','libx264','-preset','veryfast','-crf','28','-maxrate','1200k','-bufsize','2400k',
             '-pix_fmt','yuv420p','-c:a','aac','-b:a','64k','-threads','2','-movflags','+faststart','-map_metadata','-1',str(temp)],
             check=True,capture_output=True,timeout=1800)
        from .runtime import probe
        actual=probe(temp)
        check(abs(actual['duration_ms']-asset['duration_ms'])<=max(150,asset['duration_ms']*.001),'preview_duration','预览时间轴与原片不一致',422)
        with rt.db(True) as db:
            valid=db.execute("SELECT 1 FROM editor_previews WHERE id=? AND lease=? AND status='running'",(job['id'],job['lease'])).fetchone()
            check(valid,'preview_lease_lost','预览任务已转交',409)
            os.replace(temp,video);os.chmod(video,0o640)
            db.execute("UPDATE editor_previews SET status='ready',error='',lease='',expires=0,updated=? WHERE id=? AND lease=?",(time.time(),job['id'],job['lease']))
        LOG.info('Preview ready id=%s elapsed_ms=%d bytes=%d',job['id'],(time.monotonic()-began)*1000,video.stat().st_size)
    except Exception as exc:
        LOG.warning('Preview failed %s %s',job['id'],type(exc).__name__)
        with rt.db(True) as db:
            db.execute("UPDATE editor_previews SET status='failed',error=?,lease='',expires=0,updated=? WHERE id=? AND lease=?",(getattr(exc,'message','轻量预览暂未生成，可播放原片'),time.time(),job['id'],job['lease']))
    finally:
        done.set();pulse.join(1);temp.unlink(missing_ok=True)


def main():
    rt=Runtime(os.environ.get('AI_VIDEO_EDITING_ROOT','/mnt/data-disk/ai-video-editing'))
    setup(rt)
    stopping=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    while not stopping.is_set():
        try:
            job=claim(rt)
            if job:process(rt,job);continue
        except Exception:LOG.exception('Preview worker delayed')
        stopping.wait(1)


if __name__=='__main__':
    main()

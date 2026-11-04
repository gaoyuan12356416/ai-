"""Audited 2026-11-04 recovery: task 1147, existing upload session only.

Dry run by default. The original quarantined channel remains fenced throughout the same-session operation.
Other native rows and dispatch worker processes are never claimed or restarted.
"""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback

ROOT=Path('/root/drama_material_service')
BASE=Path('/mnt/data-disk/pipeline-dispatch')
DB=ROOT/'data/drama_material_jobs.sqlite3'
TASK=1147
JOB=33382
SESSION_SHA='086e69e94f1adb4171535ecb727f5a95d58b85d2d2e43d0fd232eb801a697985'
MEDIA_SHA='feaac98d85771247b21897d97d1cfae5b2fb97c0f5ac72d72a3f4763c3c8e7c3'
KEY='youtube:publish:1147'
WORKER='operator:youtube:1147:20261104'

def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def environment(unit):
    pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip()
    os.environ.update({k.decode():v.decode() for k,v in (p.split(b'=',1) for p in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in p)})

def main():
    continuing=False
    assert '--continue-video' not in sys.argv, 'this audit is for a partial session only'
    environment('pipeline-youtube-canonical.service')
    sys.path.insert(0,str(ROOT))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import app
        from features.youtube_auto_publish.runtime import readonly_runner
        from features.youtube_auto_publish.engine import ReviewedYouTubePublishEngine,ReviewedYouTubeHTTPClient
        from features.drama_synthesis.youtube import YouTubeCredentialRepository
        from features.drama_synthesis.core import DramaSynthesisStore
    class ExistingSessionClient(ReviewedYouTubeHTTPClient):
        def begin_resumable(self,*args,**kwargs):
            raise RuntimeError('replacement_session_forbidden')
        def upload(self,session_uri,*args,**kwargs):
            assert hashlib.sha256(session_uri.encode()).hexdigest()==SESSION_SHA
            return super().upload(session_uri,*args,**kwargs)
    client=ExistingSessionClient(timeout=120)
    store=DramaSynthesisStore(DB)
    original=store.youtube_task(TASK)
    assert original['workflow']=='reviewed_thumbnail' and original['channel_id']=='UCtlgk3SMqM4IpG9fB5dE--w'
    assert original['status']=='unknown' and original['unknown_outcome']==1 and not original['lease_owner']
    assert original['pipeline_dispatch_key']==KEY and original['video_attempt_count']==1
    assert original['source_sha256']==MEDIA_SHA and original['source_size']==267016229
    assert original['video_id']==('JUI00EaF1Ew' if continuing else '')
    assert not original['comment_attempt_count'] and not original['comment_id'] and not original['publish_at']
    assert hashlib.sha256(original['resumable_session_uri'].encode()).hexdigest()==SESSION_SHA
    with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as d:
        row=d.execute('SELECT status,generation,source_key FROM pipeline_job WHERE id=?',(JOB,)).fetchone()
        assert row==('unknown',1,KEY)
        assert d.execute('SELECT 1 FROM pipeline_resource WHERE job_id=? AND quarantined=1',(JOB,)).fetchone()
    probe=client.query_upload(original['resumable_session_uri'],original['source_size'])
    if continuing:
        assert probe=={'state':'submitted','video_id':original['video_id'],'next_byte':original['source_size']}
        repository=YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME)
        credential=repository.credential(app_id=original['app_id'],channel_local_id=original['channel_local_id'],account_id=original['youtube_account_id'],expected_channel_id=original['channel_id'])
        token=client.refresh_access_token(credential);client.verify_channel_identity(token,original['channel_id'])
        state=client.read_reviewed_video_state(token,original['video_id'],expected_channel_id=original['channel_id'])
        assert state['state']=='succeeded' and state['visibility']=='private' and not state['preserved_status'].get('publishAt')
    else:
        assert probe=={'state':'resume','next_byte':168296448},'upload_readback_changed'
    print(json.dumps({'at':now(),'task_id':TASK,'original_session_sha256':SESSION_SHA,'readback':probe,'apply':'--apply' in sys.argv}),flush=True)
    if '--apply' not in sys.argv:return
    # Both normal workers and the operator obey the original YouTube limit.
    deadline=time.monotonic()+600
    while True:
        with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as d:
            config=json.loads(d.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
            reserved=continuing and config['channel_limits']['youtube']==1 and (BASE/'youtube-queue-recovery-20261104T094659Z/manual-slot.json').is_file()
            assert d.execute('SELECT 1 FROM pipeline_resource WHERE job_id=? AND quarantined=1',(JOB,)).fetchone(),'original_channel_fence_required'
            resource='youtube:channel:'+original['channel_id']
            assert not d.execute("SELECT 1 FROM pipeline_job WHERE channel='youtube' AND stage='publish' AND status='running' AND resource_keys_json LIKE ?",('%'+resource+'%',)).fetchone(),'same_channel_inflight'
            assert config['channel_limits']['youtube']>0,'youtube_capacity_required'
            running=d.execute("SELECT count(*) FROM pipeline_job WHERE channel='youtube' AND stage='publish' AND status='running'").fetchone()[0]
        if running < (2 if reserved else config['channel_limits']['youtube']):break
        assert time.monotonic()<deadline,'youtube_slots_not_drained'
        time.sleep(10)
    backup=BASE/('youtube-session-1147-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    with contextlib.closing(store._connect()) as source,contextlib.closing(sqlite3.connect(backup/'native-before.sqlite3')) as dest:
        source.backup(dest,pages=1000)
    (backup/'native-before.sqlite3').chmod(0o600)
    class TargetStore(DramaSynthesisStore):
        def claim_reviewed_youtube(self,worker_id,expires):
            assert worker_id==WORKER
            with contextlib.closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                r=dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(TASK,)).fetchone())
                assert r['pipeline_dispatch_key']==KEY and r['video_attempt_count']==1
                assert r['source_sha256']==MEDIA_SHA and hashlib.sha256(r['resumable_session_uri'].encode()).hexdigest()==SESSION_SHA
                assert r['lease_owner'] in ('',WORKER)
                assert not r['comment_attempt_count'] and not r['comment_id']
                if r['status']=='unknown':
                    assert r==original,'original_row_changed'
                else:
                    assert r['status'] in ('submitted','processing') and not r['unknown_outcome']
                c.execute("UPDATE drama_youtube_publish SET status=?,video_state=?,unknown_outcome=0,lease_owner=?,lease_generation=lease_generation+1,lease_expires_at_utc=?,error_code='',error_message='',updated_at_utc=? WHERE id=?",('processing' if r['video_id'] else 'uploading','submitted' if r['video_id'] else 'uploading',worker_id,expires,now(),TASK))
                c.execute('INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)',(TASK,'operator_existing_session','resume',json.dumps({'session_sha256':SESSION_SHA,'media_sha256':MEDIA_SHA,'readback':probe,'central_job':JOB}),now()))
                c.commit()
                return dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(TASK,)).fetchone())
    storage=Path(os.environ.get('YOUTUBE_AUTO_STORAGE_ROOT','/mnt/data-disk/youtube-auto-publish'))
    target=TargetStore(DB)
    class DiagnosticEngine(ReviewedYouTubePublishEngine):
        def _failure(self,*args,**kwargs):
            if kwargs.get('internal'):
                kind,value,tb=sys.exc_info()
                print(json.dumps({'internal_type':kind.__name__ if kind else '', 'frames':[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(tb)]}),flush=True)
            return super()._failure(*args,**kwargs)
    engine=DiagnosticEngine(target,YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME),client,
        work_root=backup/'media',approved_cover_root=storage/'assets',
        allowed_source_hosts=tuple(x.strip().lower() for x in os.environ.get('DRAMA_YOUTUBE_SOURCE_HOSTS','').split(',') if x.strip()),media_executor=None)
    deadline=time.monotonic()+1200
    result={}
    while time.monotonic()<deadline:
        result=engine.run_once(WORKER)
        current=target.youtube_task(TASK)
        print(json.dumps({'at':now(),'result':result,'video_id':current['video_id'],'phase':current['reviewed_phase'],'error_code':current['error_code']}),flush=True)
        (backup/'result.json').write_text(json.dumps({'at':now(),'result':result,'video_id':current['video_id'],'phase':current['reviewed_phase'],'error_code':current['error_code']},indent=2))
        assert current['video_attempt_count']==1 and hashlib.sha256(current['resumable_session_uri'].encode()).hexdigest()==SESSION_SHA
        if not result['ok'] or current['status']=='published':break
        assert current['status'] in ('submitted','processing')
        time.sleep(15)
    assert result.get('ok') and target.youtube_task(TASK)['status']=='published','existing_session_recovery_incomplete'
    with contextlib.closing(target._connect()) as c:
        c.execute("UPDATE drama_youtube_publish SET pipeline_dispatch_key='',lease_owner='',lease_expires_at_utc='' WHERE id=? AND status='published' AND unknown_outcome=0 AND pipeline_dispatch_key=?",(TASK,KEY))
        c.commit()
    print(json.dumps({'completed':True,'task_id':TASK,'video_id':target.youtube_task(TASK)['video_id'],'backup':str(backup),'new_sessions':0}),flush=True)

if __name__=='__main__':main()

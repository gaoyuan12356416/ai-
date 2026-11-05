"""Explicit recovery: original empty upload session, or never-started overdue task.

No replacement upload sessions. No resets of existing comment attempts. The
normal channel mutex is held only while this exact task actually executes.
"""
import argparse,contextlib,datetime as dt,hashlib,io,json,os,secrets,sqlite3,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path('/root/drama_material_service');BASE=Path('/mnt/data-disk/pipeline-dispatch');DB=ROOT/'data/drama_material_jobs.sqlite3'
def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def expiry():return (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=180)).isoformat(timespec='seconds').replace('+00:00','Z')
def connect(path=DB):
    c=sqlite3.connect(path,timeout=15);c.row_factory=sqlite3.Row;return c
def backup(folder):
    folder.mkdir(mode=0o700)
    for source,name in ((DB,'native'),(BASE/'dispatch.sqlite3','dispatch')):
        with contextlib.closing(connect(source)) as c,contextlib.closing(sqlite3.connect(folder/(name+'-before.sqlite3'))) as d:c.backup(d,pages=1000)
        (folder/(name+'-before.sqlite3')).chmod(0o600)
def immediate(c,task):
    old=task['publish_at'];assert old and old<now(),'task_not_overdue'
    c.execute("UPDATE drama_youtube_publish SET publish_at='',schedule_status='none',schedule_command='',schedule_command_status='',schedule_confirmed_publish_at='',schedule_next_check_at='',updated_at_utc=? WHERE id=?",(now(),task['id']))
    row=c.execute('SELECT body FROM youtube_auto_preparation WHERE id=?',(task['preparation_id'],)).fetchone()
    if row:
        body=json.loads(row[0]);body.update(publish_at='',schedule_state='none',schedule_control=None,updated_at=now())
        c.execute('UPDATE youtube_auto_preparation SET body=?,updated_at=? WHERE id=?',(json.dumps(body,ensure_ascii=False,sort_keys=True),now(),task['preparation_id']))
    c.execute('INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)',(task['id'],'operator_overdue_resume','immediate',json.dumps({'original_publish_at':old,'reason':'operator_requested_recovery'}),now()))
def main():
    p=argparse.ArgumentParser();p.add_argument('--task-id',type=int,required=True);p.add_argument('--empty-session',action='store_true');p.add_argument('--apply',action='store_true');a=p.parse_args()
    mainpid=subprocess.check_output(['systemctl','show','youtube-auto-publish-worker.service','-p','MainPID','--value'],text=True).strip()
    os.environ.update(dict(x.decode().split('=',1) for x in Path('/proc/'+mainpid+'/environ').read_bytes().split(b'\0') if b'=' in x))
    sys.path.insert(0,str(ROOT))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import app
        from features.youtube_auto_publish.runtime import readonly_runner
        from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient,ReviewedYouTubePublishEngine
        from features.drama_synthesis.youtube import YouTubeCredentialRepository
        from features.drama_synthesis.core import DramaSynthesisStore
    store=DramaSynthesisStore(DB);task=store.youtube_task(a.task_id);key='youtube:publish:'+str(a.task_id)
    assert task and task['workflow']=='reviewed_thumbnail' and not task['lease_owner']
    assert not task['video_id'] and not task['comment_id'] and task['comment_attempt_count']==0
    assert task['publish_at'] and task['publish_at']<now()
    with contextlib.closing(connect(BASE/'dispatch.sqlite3')) as c:
        job=dict(c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage='publish'",(key,)).fetchone())
        assert job['status']!='running'
    session_sha=hashlib.sha256(task['resumable_session_uri'].encode()).hexdigest()
    class OriginalSession(ReviewedYouTubeHTTPClient):
        def begin_resumable(self,*args,**kwargs):raise RuntimeError('replacement_session_forbidden')
        def upload(self,session_uri,*args,**kwargs):
            assert hashlib.sha256(session_uri.encode()).hexdigest()==session_sha
            return super().upload(session_uri,*args,**kwargs)
    client=OriginalSession(timeout=120)
    if a.empty_session:
        assert task['status']=='unknown' and task['unknown_outcome']==1 and task['video_attempt_count']==1 and task['resumable_session_uri']
        assert task['pipeline_dispatch_key']==key and job['status']=='unknown'
        with contextlib.closing(connect('/mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3')) as c:
            assert not c.execute('SELECT 1 FROM receipt_outbox WHERE claim_key=? AND acked_at IS NULL',('['+str(job['id'])+','+str(job['generation'])+']',)).fetchone(),'original_receipts_still_pending'
        probe=client.query_upload(task['resumable_session_uri'],task['source_size'])
        assert probe=={'state':'resume','next_byte':0},'original_session_state_changed'
    else:
        assert task['status'] in {'queued','schedule_missed'} and not task['video_attempt_count'] and not task['resumable_session_uri'] and not task['unknown_outcome']
        assert job['generation']==0 and not job['external_intent'] and not job['claim_hash']
        probe={'state':'never_started'}
    print(json.dumps({'task_id':a.task_id,'probe':probe,'apply':a.apply}),flush=True)
    if not a.apply:return
    assert subprocess.check_output(['findmnt','-no','UUID','/mnt/data-disk'],text=True).strip()=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    folder=BASE/('youtube-overdue-'+str(a.task_id)+'-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup(folder)
    if not a.empty_session:
        with contextlib.closing(connect()) as c:
            c.execute('BEGIN IMMEDIATE');assert dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(a.task_id,)).fetchone())==task
            immediate(c,task)
            c.execute("UPDATE drama_youtube_publish SET status='queued',video_state='queued',error_code='',error_message='' WHERE id=?",(a.task_id,));c.commit()
        print(json.dumps({'task_id':a.task_id,'ready_immediate':True,'backup':str(folder)}));return
    worker='operator:youtube-session:'+str(a.task_id)+':'+secrets.token_hex(4)
    central_gen=job['generation']+1;channel='youtube:channel:'+task['channel_id']
    with contextlib.closing(connect(BASE/'dispatch.sqlite3')) as c:
        c.execute('BEGIN IMMEDIATE');assert dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(job['id'],)).fetchone())==job
        assert not c.execute('SELECT 1 FROM pipeline_resource WHERE resource_key=? AND job_id<>?',(channel,job['id'])).fetchone(),'channel_currently_executing'
        assert not c.execute('SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=? AND job_id<>?',(channel,job['id'])).fetchone(),'channel_has_active_legacy_writer'
        claim_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        c.execute("UPDATE pipeline_job SET status='running',generation=?,worker_id=?,claim_hash=?,lease_expires_at=?,started_at=?,updated_at=? WHERE id=?",(central_gen,worker,claim_hash,expiry(),now(),now(),job['id']))
        c.execute('INSERT INTO pipeline_claim_history VALUES(?,?,?,?,?)',(job['id'],central_gen,job['node_id'],worker,claim_hash))
        c.execute('INSERT OR REPLACE INTO pipeline_resource VALUES(?,?,?,0,0)',(channel,job['id'],central_gen))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(job['id'],'operator_original_session_reserved',json.dumps({'task_id':a.task_id,'session_sha256':session_sha,'offset':0}),now()));c.commit()
    stop=threading.Event();heartbeat_error=[]
    class TargetStore(DramaSynthesisStore):
        def claim_reviewed_youtube(self,worker_id,expires):
            assert worker_id==worker and not heartbeat_error
            with contextlib.closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE');r=dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(a.task_id,)).fetchone())
                assert r['pipeline_dispatch_key']==key and r['video_attempt_count']==1
                assert r['source_sha256']==task['source_sha256'] and hashlib.sha256(r['resumable_session_uri'].encode()).hexdigest()==session_sha
                assert r['lease_owner'] in ('',worker) and not r['comment_attempt_count'] and not r['comment_id']
                if r['status']=='unknown':
                    assert r==task,'native_row_changed';immediate(c,r)
                else:assert r['status'] in ('submitted','processing') and not r['unknown_outcome']
                c.execute("UPDATE drama_youtube_publish SET status=?,video_state=?,unknown_outcome=0,lease_owner=?,lease_generation=lease_generation+1,lease_expires_at_utc=?,error_code='',error_message='',updated_at_utc=? WHERE id=?",('processing' if r['video_id'] else 'uploading','submitted' if r['video_id'] else 'uploading',worker,expires,now(),a.task_id));c.commit()
                return dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(a.task_id,)).fetchone())
    target=TargetStore(DB)
    def heartbeat():
        while not stop.wait(15):
            try:
                with contextlib.closing(connect(BASE/'dispatch.sqlite3')) as c:
                    changed=c.execute("UPDATE pipeline_job SET lease_expires_at=?,updated_at=? WHERE id=? AND status='running' AND generation=? AND worker_id=?",(expiry(),now(),job['id'],central_gen,worker)).rowcount;c.commit();assert changed==1
                current=target.youtube_task(a.task_id)
                if current['lease_owner']==worker:target.renew_youtube_lease(a.task_id,worker_id=worker,lease_generation=current['lease_generation'],lease_expires_at_utc=expiry())
            except Exception as e:heartbeat_error.append(type(e).__name__);return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        engine=ReviewedYouTubePublishEngine(target,YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME),client,
            work_root=folder/'media',approved_cover_root=Path('/mnt/data-disk/youtube-auto-publish/assets'),
            allowed_source_hosts=tuple(x.strip() for x in os.environ.get('DRAMA_YOUTUBE_SOURCE_HOSTS','').split(',') if x.strip()),media_executor=None)
        for _ in range(120):
            result=engine.run_once(worker);current=target.youtube_task(a.task_id)
            safe={'at':now(),'task_id':a.task_id,'result':result,'video_id':current['video_id'],'comment_id':current['comment_id'],'phase':current['reviewed_phase'],'error_code':current['error_code']}
            (folder/'result.json').write_text(json.dumps(safe));print(json.dumps(safe),flush=True)
            assert current['video_attempt_count']==1
            if not result['ok'] or current['status']=='published':break
            assert current['status'] in ('submitted','processing');time.sleep(15)
    finally:
        stop.set();thread.join(timeout=20)
        with contextlib.closing(connect(BASE/'dispatch.sqlite3')) as c:
            c.execute("UPDATE pipeline_job SET status='unknown',lease_expires_at='',updated_at=? WHERE id=? AND generation=? AND worker_id=?",(now(),job['id'],central_gen,worker))
            c.execute('UPDATE pipeline_resource SET quarantined=1 WHERE job_id=?',(job['id'],));c.commit()
    current=target.youtube_task(a.task_id)
    assert current['status']=='published' and current['comment_status'] in ('published','skipped'),'original_session_recovery_incomplete'
    print(json.dumps({'completed':True,'task_id':a.task_id,'video_id':current['video_id'],'comment_id':current['comment_id'],'backup':str(folder),'new_sessions':0}),flush=True)

if __name__=='__main__':main()

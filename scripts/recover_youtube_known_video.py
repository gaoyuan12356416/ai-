"""Operator recovery of an exact existing video; upload methods are disabled."""
import argparse,contextlib,datetime as dt,hashlib,io,json,os,sqlite3,subprocess,sys,time,traceback
from pathlib import Path

ROOT=Path('/root/drama_material_service')
BASE=Path('/mnt/data-disk/pipeline-dispatch')
DB=ROOT/'data/drama_material_jobs.sqlite3'
def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def main():
    p=argparse.ArgumentParser();p.add_argument('--task-id',type=int,required=True);p.add_argument('--video-id',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--record-missing',action='store_true');args=p.parse_args()
    assert not (args.apply and args.record_missing),'choose_recovery_or_readback_annotation'
    task_id=args.task_id;key='youtube:publish:'+str(task_id);worker='operator:existing-video:'+str(task_id)
    pid=subprocess.check_output(['systemctl','show','pipeline-youtube-canonical','-p','MainPID','--value'],text=True).strip()
    os.environ.update({k.decode():v.decode() for k,v in (x.split(b'=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)})
    sys.path.insert(0,str(ROOT))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import app
        from features.youtube_auto_publish.runtime import readonly_runner
        from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient,ReviewedYouTubePublishEngine
        from features.drama_synthesis.youtube import YouTubeCredentialRepository
        from features.drama_synthesis.core import DramaSynthesisStore
    class ReadRepository(YouTubeCredentialRepository):
        def credential(self,**kwargs):
            for attempt in range(2):
                try:return super().credential(**kwargs)
                except Exception as exc:
                    if attempt or getattr(exc,'code','') not in ('material_query_failed','material_query_busy'):raise
                    time.sleep(1)
    class ExistingVideoClient(ReviewedYouTubeHTTPClient):
        def begin_resumable(self,*a,**kw):raise RuntimeError('new_video_forbidden')
        def upload(self,*a,**kw):raise RuntimeError('video_upload_forbidden')
        def query_upload(self,*a,**kw):raise RuntimeError('upload_path_forbidden')
    store=DramaSynthesisStore(DB);original=store.youtube_task(task_id)
    assert original and original['workflow']=='reviewed_thumbnail' and original['video_id']==args.video_id
    assert original['status']=='unknown' and original['unknown_outcome']==1 and not original['lease_owner']
    assert original['pipeline_dispatch_key']==key and original['video_attempt_count']==1
    assert not original['comment_attempt_count'] and not original['comment_id'] and not original['publish_at']
    with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as d:
        job=d.execute("SELECT id,status,generation FROM pipeline_job WHERE source_key=? AND stage='publish'",(key,)).fetchone()
        assert job and job[1]=='unknown'
        assert d.execute('SELECT 1 FROM pipeline_resource WHERE job_id=? AND quarantined=1',(job[0],)).fetchone()
    repo=ReadRepository(readonly_runner(app),schema=app.DB_NAME);client=ExistingVideoClient(timeout=30)
    credential=repo.credential(app_id=original['app_id'],channel_local_id=original['channel_local_id'],account_id=original['youtube_account_id'],expected_channel_id=original['channel_id'])
    token=client.refresh_access_token(credential);client.verify_channel_identity(token,original['channel_id'])
    try:
        state=client.read_reviewed_video_state(token,args.video_id,expected_channel_id=original['channel_id'])
    except Exception as exc:
        if not args.record_missing or getattr(exc,'code','')!='youtube_video_reconcile_unknown':raise
        with contextlib.closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE');r=dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(task_id,)).fetchone())
            assert r==original,'original_row_changed'
            c.execute('UPDATE drama_youtube_publish SET error_code=?,error_message=?,updated_at_utc=? WHERE id=?',('youtube_video_reconcile_unknown','YouTube 当前未返回原视频，已保留视频 ID 和上传会话，暂停重新上传并等待平台核对。',now(),task_id))
            c.execute('INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)',(task_id,'operator_owner_readback','still_unknown',json.dumps({'video_id':args.video_id,'previous_error':original['error_code'],'central_job':job[0],'new_uploads':0}),now()));c.commit()
        print(json.dumps({'task_id':task_id,'video_id':args.video_id,'still_unknown':True,'annotation_saved':True,'new_uploads':0}),flush=True);return
    assert not args.record_missing,'video_now_visible_use_normal_verified_recovery'
    assert state['state']=='succeeded' and state['visibility'] in ('public','private') and not state['preserved_status'].get('publishAt')
    if state['visibility']=='public':
        assert original['thumbnail_status']=='succeeded' and original['processing_status']=='succeeded' and original['public_status'] in ('running','unknown','succeeded')
    print(json.dumps({'at':now(),'task_id':task_id,'video_id':args.video_id,'readback':state,'apply':args.apply}),flush=True)
    if not args.apply:return
    deadline=time.monotonic()+600
    while True:
        with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as d:
            cfg=json.loads(d.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
            assert cfg['channel_limits']['youtube']==1,'reserve_one_operator_slot_first'
            running=d.execute("SELECT count(*) FROM pipeline_job WHERE channel='youtube' AND stage='publish' AND status='running'").fetchone()[0]
        if running<2:break
        assert time.monotonic()<deadline,'existing_jobs_not_drained';time.sleep(5)
    backup=BASE/('youtube-video-recovery-'+str(task_id)+'-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir(mode=0o700)
    with contextlib.closing(store._connect()) as src,contextlib.closing(sqlite3.connect(backup/'native-before.sqlite3')) as dst:src.backup(dst,pages=1000)
    (backup/'native-before.sqlite3').chmod(0o600)
    (backup/'readback.json').write_text(json.dumps({'video_id':args.video_id,'state':state,'at':now()},indent=2))
    class TargetStore(DramaSynthesisStore):
        def claim_reviewed_youtube(self,worker_id,expires):
            assert worker_id==worker
            with contextlib.closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE');r=dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(task_id,)).fetchone())
                assert r==original,'original_row_changed'
                c.execute("UPDATE drama_youtube_publish SET status='processing',video_state='submitted',unknown_outcome=0,lease_owner=?,lease_generation=lease_generation+1,lease_expires_at_utc=?,error_code='',error_message='',updated_at_utc=? WHERE id=?",(worker,expires,now(),task_id))
                c.execute('INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)',(task_id,'operator_existing_video','owner_readback',json.dumps({'video_id':args.video_id,'state':state['state'],'visibility':state['visibility'],'central_job':job[0]}),now()))
                c.commit();return dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=?',(task_id,)).fetchone())
    class DiagnosticEngine(ReviewedYouTubePublishEngine):
        def _failure(self,*a,**kw):
            if kw.get('internal'):
                kind,value,tb=sys.exc_info();print(json.dumps({'internal_type':kind.__name__ if kind else '', 'frames':[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(tb)]}),flush=True)
            return super()._failure(*a,**kw)
    target=TargetStore(DB);storage=Path(os.environ.get('YOUTUBE_AUTO_STORAGE_ROOT','/mnt/data-disk/youtube-auto-publish'))
    engine=DiagnosticEngine(target,repo,client,work_root=backup/'media',approved_cover_root=storage/'assets',allowed_source_hosts=tuple(x for x in os.environ['DRAMA_YOUTUBE_SOURCE_HOSTS'].split(',') if x),media_executor=None)
    result=engine.run_once(worker);current=target.youtube_task(task_id)
    report={'at':now(),'task_id':task_id,'video_id':current['video_id'],'status':current['status'],'error_code':current['error_code'],'result':result,'new_video_uploads':0,'backup':str(backup)}
    (backup/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
    assert current['video_id']==args.video_id and current['video_attempt_count']==1
    assert result['ok'] and current['status']=='published','existing_video_recovery_incomplete'
    with contextlib.closing(target._connect()) as c:
        c.execute("UPDATE drama_youtube_publish SET pipeline_dispatch_key='',lease_owner='',lease_expires_at_utc='' WHERE id=? AND status='published' AND unknown_outcome=0 AND pipeline_dispatch_key=?",(task_id,key));c.commit()
if __name__=='__main__':main()

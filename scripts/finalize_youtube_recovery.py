"""Read original YouTube receipts, then sync only the six authorized tasks."""
import argparse,contextlib,datetime as dt,hashlib,io,json,os,sqlite3,subprocess,sys
from pathlib import Path
import requests
ROOT=Path('/root/drama_material_service');BASE=Path('/mnt/data-disk/pipeline-dispatch');IDS=(1155,1156,1157,1158,1159,1160)
def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

def main():
    p=argparse.ArgumentParser();p.add_argument('--sync',action='store_true');a=p.parse_args()
    pid=subprocess.check_output(['systemctl','show','youtube-auto-publish-worker.service','-p','MainPID','--value'],text=True).strip()
    os.environ.update(dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x))
    sys.path.insert(0,str(ROOT))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import app
        from features.youtube_auto_publish.runtime import readonly_runner
        from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient
        from features.drama_synthesis.youtube import YouTubeCredentialRepository
        from features.drama_synthesis.core import DramaSynthesisStore
        from features.drama_synthesis.unified_youtube import build_unified_youtube_writer_from_env,run_sync_outbox_once
    class TargetSyncStore(DramaSynthesisStore):
        def claim_youtube_sync(self,worker_id,lease_expires_at_utc,**_):
            with contextlib.closing(self._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute("SELECT o.* FROM drama_youtube_sync_outbox o JOIN drama_youtube_publish p ON p.id=o.publish_id WHERE p.id IN (1155,1156,1157,1158,1159,1160) AND p.status='published' AND p.video_state='published' AND p.unknown_outcome=0 AND o.status IN ('pending','failed') AND o.lease_owner='' ORDER BY o.id LIMIT 1").fetchone()
                if r is None:c.commit();return None
                c.execute("UPDATE drama_youtube_sync_outbox SET status='syncing',lease_owner=?,lease_expires_at_utc=?,lease_generation=lease_generation+1,attempt_count=attempt_count+1,updated_at_utc=? WHERE id=?",(worker_id,lease_expires_at_utc,now(),r['id']));c.commit()
                return dict(c.execute('SELECT * FROM drama_youtube_sync_outbox WHERE id=?',(r['id'],)).fetchone())
    store=TargetSyncStore(ROOT/'data/drama_material_jobs.sqlite3');repo=YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME);client=ReviewedYouTubeHTTPClient(timeout=30)
    report={'checked_at':now(),'tasks':[],'platform_writes':0,'sync_requested':a.sync}
    for ident in IDS:
        task=store.youtube_task(ident)
        assert task['status']=='published' and task['video_state']=='published' and task['comment_status']=='published' and not task['lease_owner'] and not task['unknown_outcome']
        assert task['video_attempt_count']==task['comment_attempt_count']==1
        if ident in (1155,1156,1160):assert task['video_id']=={1155:'mUK8xIMVMGA',1156:'z3-IVGLn1sA',1160:'E04JET-c2PI'}[ident]
        credential=repo.credential(app_id=task['app_id'],channel_local_id=task['channel_local_id'],account_id=task['youtube_account_id'],expected_channel_id=task['channel_id'])
        token=client.refresh_access_token(credential);client.verify_channel_identity(token,task['channel_id'])
        proof=client.read_reviewed_video_state(token,task['video_id'],expected_channel_id=task['channel_id'])
        assert proof['visibility']=='public' and proof['state']=='succeeded'
        matches=[];page='';total=0
        with requests.Session() as http:
            http.trust_env=False
            for _ in range(10):
                response=http.get('https://www.googleapis.com/youtube/v3/commentThreads',headers={'Authorization':'Bearer '+token},params={'part':'snippet','videoId':task['video_id'],'maxResults':100,'textFormat':'plainText','pageToken':page},timeout=30,allow_redirects=False)
                assert response.status_code==200,'comments_readback_failed'
                payload=response.json()
                for item in payload.get('items',[]):
                    top=item['snippet']['topLevelComment'];snippet=top['snippet'];total+=1
                    if snippet.get('authorChannelId',{}).get('value')==task['channel_id'] and snippet.get('textOriginal','').strip()==task['comment_text'].strip():matches.append(top['id'])
                page=payload.get('nextPageToken','')
                if not page:break
            assert not page,'comment_pagination_incomplete'
        assert matches==[task['comment_id']],'comment_receipt_or_duplicate_conflict'
        report['tasks'].append({k:task[k] for k in ('id','preparation_id','title','channel_id','video_id','comment_id','video_attempt_count','comment_attempt_count','initial_publish_at','video_published_at_utc','comment_published_at_utc')})
        report['tasks'][-1].update(platform_visibility=proof['visibility'],platform_published_at=proof.get('published_at'),matching_owner_comments=len(matches),total_comments=total,original_session_sha256=hashlib.sha256(task['resumable_session_uri'].encode()).hexdigest())
    folder=BASE/('youtube-final-readback-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(mode=0o700)
    if a.sync:
        with contextlib.closing(store._connect()) as c,contextlib.closing(sqlite3.connect(folder/'native-before.sqlite3')) as b:c.backup(b,pages=1000)
        writer=build_unified_youtube_writer_from_env();writer.preflight();report['sync_results']=[]
        for _ in range(19):
            result=run_sync_outbox_once(store,writer,'operator:youtube-final:'+str(os.getpid()));report['sync_results'].append(result)
            assert result['status'] in ('synced','no_pending'),result.get('code','sync_failed')
            if not result['claimed']:break
    with contextlib.closing(store._connect()) as c:
        report['outbox']=[dict(r) for r in c.execute('SELECT publish_id,entity_kind,external_id,status,error_code FROM drama_youtube_sync_outbox WHERE publish_id IN (1155,1156,1157,1158,1159,1160) ORDER BY publish_id,entity_kind')]
    if a.sync:assert len(report['outbox'])==18 and all(r['status']=='synced' for r in report['outbox'])
    with sqlite3.connect(BASE/'dispatch.sqlite3') as c:
        c.row_factory=sqlite3.Row
        report['dispatch']=[{k:r[k] for k in ('id','source_id','status','generation','error_code')} for r in c.execute("SELECT * FROM pipeline_job WHERE source_key IN ('youtube:publish:1155','youtube:publish:1156','youtube:publish:1157','youtube:publish:1158','youtube:publish:1159','youtube:publish:1160') AND stage='publish'")]
    assert len(report['dispatch'])==6 and all(r['status']=='published' for r in report['dispatch'])
    path=folder/'verification.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
    print(json.dumps({'evidence':str(path),**report},ensure_ascii=False),flush=True)

if __name__=='__main__':main()

"""Undo only our verified duplicate comment; retain the earlier owner comment."""
import argparse,contextlib,datetime as dt,io,json,os,sqlite3,subprocess,sys,time
from pathlib import Path
import requests
ROOT=Path('/root/drama_material_service');BASE=Path('/mnt/data-disk/pipeline-dispatch')
OURS='Ugz6EWkka4RWMVdPlDx4AaABAg';EARLIER='UgziFIRP12CBbiFzNsF4AaABAg'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--confirm-only');args=parser.parse_args()
    if args.confirm_only:
        prior=Path(args.confirm_only).resolve();assert prior.is_relative_to(BASE)
        intent=json.loads(prior.read_text());assert intent['delete_only']==OURS and intent['retain']==EARLIER
    pid=subprocess.check_output(['systemctl','show','youtube-auto-publish-worker.service','-p','MainPID','--value'],text=True).strip()
    os.environ.update(dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x));sys.path.insert(0,str(ROOT))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import app
        from features.youtube_auto_publish.runtime import readonly_runner
        from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient
        from features.drama_synthesis.youtube import YouTubeCredentialRepository
        from features.drama_synthesis.core import _comment_sync_payload,utc_now
    db=ROOT/'data/drama_material_jobs.sqlite3'
    c=sqlite3.connect(db,timeout=20);c.row_factory=sqlite3.Row
    task=dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=1155').fetchone())
    assert task['video_id']=='mUK8xIMVMGA' and task['status']=='published' and task['comment_id']==OURS and task['comment_attempt_count']==1 and not task['lease_owner']
    assert c.execute("SELECT 1 FROM drama_youtube_publish_event WHERE task_id=1155 AND phase='comment' AND outcome='published' AND safe_detail=? AND created_at_utc='2026-11-05T02:31:26Z'",(OURS,)).fetchone()
    out=dict(c.execute("SELECT * FROM drama_youtube_sync_outbox WHERE publish_id=1155 AND entity_kind='comment' AND external_id=?",(OURS,)).fetchone())
    assert out['status']=='pending' and not out['lease_owner']
    repo=YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME);api=ReviewedYouTubeHTTPClient(timeout=30)
    token=api.refresh_access_token(repo.credential(app_id=task['app_id'],channel_local_id=task['channel_local_id'],account_id=task['youtube_account_id'],expected_channel_id=task['channel_id']));api.verify_channel_identity(token,task['channel_id'])
    folder=BASE/('youtube-comment-correction-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(mode=0o700)
    with sqlite3.connect(folder/'native-before.sqlite3') as b:c.backup(b,pages=1000)
    with requests.Session() as http:
        http.trust_env=False;headers={'Authorization':'Bearer '+token}
        r=http.get('https://www.googleapis.com/youtube/v3/commentThreads',headers=headers,params={'part':'snippet','videoId':task['video_id'],'maxResults':100,'textFormat':'plainText'},timeout=30,allow_redirects=False)
        assert r.status_code==200;data=r.json();assert not data.get('nextPageToken')
        matches={}
        for item in data.get('items',[]):
            top=item['snippet']['topLevelComment'];s=top['snippet']
            if item['snippet'].get('videoId')==task['video_id'] and s.get('authorChannelId',{}).get('value')==task['channel_id'] and s.get('textOriginal','').strip()==task['comment_text'].strip():matches[top['id']]=s['publishedAt']
        assert matches.get(EARLIER)=='2026-11-05T02:25:09Z'
        if not args.confirm_only:
            assert matches=={OURS:'2026-11-05T02:31:25Z',EARLIER:'2026-11-05T02:25:09Z'}
            (folder/'intent.json').write_text(json.dumps({'delete_only':OURS,'retain':EARLIER,'video':task['video_id'],'proof':matches}))
            r=http.delete('https://www.googleapis.com/youtube/v3/comments',params={'id':OURS},headers=headers,timeout=30,allow_redirects=False)
            (folder/'delete-response.json').write_text(json.dumps({'http_status':r.status_code}))
            assert r.status_code in (204,404),'comment_delete_requires_readback'
        # Read propagation can lag the DELETE receipt. Never send a second DELETE.
        for attempt in range(8):
            r=http.get('https://www.googleapis.com/youtube/v3/comments',params={'part':'snippet','id':OURS+','+EARLIER,'fields':'items(id,snippet(publishedAt))'},headers={**headers,'Cache-Control':'no-cache'},timeout=30,allow_redirects=False)
            if r.status_code==200 and [x['id'] for x in r.json().get('items',[])]==[EARLIER]:break
            time.sleep(2)
        else:raise RuntimeError('comment_delete_unconfirmed')
    c.execute('BEGIN IMMEDIATE');assert dict(c.execute('SELECT * FROM drama_youtube_publish WHERE id=1155').fetchone())==task
    assert dict(c.execute('SELECT * FROM drama_youtube_sync_outbox WHERE id=?',(out['id'],)).fetchone())==out
    c.execute("UPDATE drama_youtube_publish SET comment_id=?,comment_published_at_utc=?,updated_at_utc=? WHERE id=1155",(EARLIER,matches[EARLIER],utc_now()))
    c.execute("UPDATE drama_youtube_sync_outbox SET external_id=?,payload_json=?,updated_at_utc=? WHERE id=?",(EARLIER,json.dumps(_comment_sync_payload(task,EARLIER,matches[EARLIER]),ensure_ascii=False,sort_keys=True),utc_now(),out['id']))
    detail={'removed_recovery_comment':OURS,'retained_existing_comment':EARLIER,'published_at':matches[EARLIER]}
    c.execute('INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(1155,?,?,?,?)',('comment_readback','duplicate_recovery_undone',json.dumps(detail),utc_now()));c.commit();c.close()
    (folder/'result.json').write_text(json.dumps(detail));print(json.dumps({'backup':str(folder),**detail}),flush=True)
if __name__=='__main__':main()

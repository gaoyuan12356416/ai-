"""Repair only the eight evidenced transport failures from this deployment.

Six unstarted dispatch admissions resume their unchanged native tasks. Two X
media-only interruptions become definitive failures, never a retry or a Post.
Preserve original attempts, operations, generations, receipts and artifacts.
"""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_posts.service import XPostStore
from features.pipeline_dispatch.x_prepost_recovery import no_post_operation
from scripts.deploy_publish_queue_isolation import backup_db,DBS,save

UNSTARTED={33554:('fb',29670),33562:('fb',29814),33564:('fb',29813),
           34127:('tt_auto',4698),34136:('tt_auto',4707),34137:('tt_auto',4708)}
MEDIA_ONLY={34253:6163,34256:6157}
WINDOW=('2026-11-05T04:11:19','2026-11-05T04:28:12')

def connect(path,readonly=True):
    c=sqlite3.connect('file:'+str(path)+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=10)
    c.row_factory=sqlite3.Row;return c

def proof(d,x,natives):
    result={}
    for jid in [*UNSTARTED,*MEDIA_ONLY]:
        j=dict(d.execute('SELECT * FROM pipeline_job WHERE id=?',(jid,)).fetchone())
        assert j['error_code']=='canonical_bridge_unavailable'
        assert WINDOW[0]<=j['updated_at']<=WINDOW[1]
        assert not j['canonical_receipt'] and not j['hold_reason']
        assert not d.execute('SELECT 1 FROM pipeline_publication WHERE job_id=?',(jid,)).fetchone()
        if jid in UNSTARTED:
            lane,identity=UNSTARTED[jid];table='fb_auto_task' if lane=='fb' else 'tt_auto_task'
            r=dict(natives[lane].execute('SELECT * FROM '+table+' WHERE id=?',(identity,)).fetchone())
            assert j['source_key']==('fb:auto:' if lane=='fb' else 'tt:auto:')+str(identity)
            assert j['status']=='failed' and not j['external_intent']
            for t in ['pipeline_operation','pipeline_checkpoint','pipeline_receipt']:
                assert not d.execute('SELECT 1 FROM '+t+' WHERE job_id=?',(jid,)).fetchone()
            assert r['status']==('ready' if lane=='fb' else 'pending')
            assert not any(r.get(k) for k in ['unknown_outcome','graph_post_id','publish_id','lease_owner','lease_expires_at_utc'])
            result[jid]={'job':j,'native':r,'action':'resume_unstarted'}
        else:
            qid=MEDIA_ONLY[jid];key='x:queue:'+str(qid)
            q=dict(x.execute('SELECT * FROM x_post_queue WHERE id=?',(qid,)).fetchone())
            l=dict(x.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(qid,)).fetchone())
            assert j['source_key']==key and j['status']=='unknown' and j['generation']==1
            assert q['status']==l['status']=='failed' and l['error_code']=='canonical_bridge_unavailable'
            assert l['unknown_outcome']==1 and l['attempt_count']==1
            assert not any(l.get(k) for k in ['x_post_id','x_media_id','published_at'])
            ops=[dict(r) for r in d.execute('SELECT * FROM pipeline_operation WHERE job_id=?',(jid,))]
            native_ops=[dict(r) for r in x.execute('SELECT * FROM pipeline_x_operation WHERE source_key=?',(key,))]
            assert ops and native_ops and all(no_post_operation(r['operation']) for r in ops+native_ops)
            cps=[dict(r) for r in x.execute('SELECT * FROM pipeline_x_checkpoint WHERE source_key=?',(key,))]
            assert all(r['phase'] in ('x_preflight','x_media_initialized') for r in cps)
            relay=x.execute('SELECT * FROM x_post_repost_ledger WHERE queue_id=?',(qid,)).fetchone()
            relay=dict(relay) if relay else None
            assert not relay or not any(relay.get(k) for k in ['source_post_id','repost_id','source_published_at','reposted_at','repost_attempt_count'])
            result[jid]={'job':j,'queue':q,'log':l,'relay':relay,'operations':ops,'native_operations':native_ops,'checkpoints':cps,'action':'confirm_media_only_failure'}
    return result

def main(directory=None):
    apply='--apply' in sys.argv
    directory=Path(directory or '/mnt/data-disk/publish-queue-isolation-20261105/switch-repair')
    with contextlib.ExitStack() as stack:
        d=stack.enter_context(contextlib.closing(connect(DBS['dispatch'],not apply)))
        x=stack.enter_context(contextlib.closing(connect(DBS['x'],not apply)))
        natives={k:stack.enter_context(contextlib.closing(connect(DBS[k]))) for k in ('fb','tt_auto')}
        checked=proof(d,x,natives)
        print(json.dumps({k:v['action'] for k,v in checked.items()}),flush=True)
        if not apply:return
        directory.mkdir(mode=0o700,exist_ok=False)
        for name in ('dispatch','x'):backup_db(Path(DBS[name]),directory/(name+'-before.sqlite3'))
        save(directory/'proof.json',checked)
        stamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
        # No work can claim either unknown/failed job; validate again while both
        # owning DBs are locked. Commit native evidence before dispatch projection.
        x.execute('BEGIN IMMEDIATE');d.execute('BEGIN IMMEDIATE')
        checked=proof(d,x,natives)
        for jid,qid in MEDIA_ONLY.items():
            l=checked[jid]['log'];message='视频上传因服务切换中断；操作账本确认未发帖，成片与原尝试保留'
            x.execute("UPDATE x_post_publish_log SET unknown_outcome=0,error_code='upload_interrupted_no_post',error_message=?,updated_at=? WHERE id=?",(message,stamp,l['id']))
            if checked[jid]['relay']:
                x.execute("UPDATE x_post_repost_ledger SET unknown_outcome=0,error_code='upload_interrupted_no_post',error_message=?,updated_at=? WHERE queue_id=?",(message,stamp,qid))
            XPostStore._sync_run(x,qid,stamp)
            d.execute("UPDATE pipeline_job SET status='failed',error_code='upload_interrupted_no_post',error_message=?,finished_at=?,updated_at=? WHERE id=?",(message,stamp,stamp,jid))
            for table in ('pipeline_resource','pipeline_legacy_resource'):d.execute('DELETE FROM '+table+' WHERE job_id=?',(jid,))
        for jid in UNSTARTED:
            state='ready' if checked[jid]['job']['stage']=='publish' else 'queued'
            d.execute("UPDATE pipeline_job SET status=?,node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',error_code='',error_message='',updated_at=? WHERE id=?",(state,stamp,jid))
        for jid,item in checked.items():
            details={'action':item['action'],'previous_status':item['job']['status'],'generation':item['job']['generation'],'proof_file':str(directory/'proof.json'),'external_post_performed':False}
            d.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(jid,'queue_isolation_switch_repaired',json.dumps(details),stamp))
        x.commit();d.commit()
        after=[dict(r) for r in d.execute('SELECT id,source_key,status,generation,error_code FROM pipeline_job WHERE id IN ('+','.join('?' for _ in checked)+')',list(checked))]
        save(directory/'result.json',after);print(json.dumps(after),flush=True)

if __name__=='__main__':
    if '--late-media-only' in sys.argv:
        UNSTARTED={};MEDIA_ONLY={34285:6154}
        WINDOW=('2026-11-05T04:44:00','2026-11-05T04:50:10')
        main('/mnt/data-disk/publish-queue-isolation-20261105/switch-repair-late')
    else:main()

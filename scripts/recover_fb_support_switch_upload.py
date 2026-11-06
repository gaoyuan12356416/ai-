"""Exact recovery of X 6553 interrupted by the FB-monitor deployment switch.

No platform request. Prove only INIT/APPEND permits and an acknowledged failed
CPU execution, then authorize one next generation with the original artifact.
The old unknown receipt, attempts and operations remain immutable evidence.
"""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_prepost_recovery import TABLE,digest,encoded,queue_hash,log_hash,no_post_operation
from scripts.deploy_fb_publish_reliability import backup_database

JOB,QUEUE,KEY=35396,6553,'x:queue:6553'
CENTRAL=Path('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')
NATIVE=Path('/var/lib/x-post-automation/accounts.sqlite3')
OUTBOX=Path('/mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3')
FOLDER=Path('/mnt/data-disk/fb-publish-reliability-20261106/deployment-20261106T031939Z/x-6553-recovery')


def connect(path,write=False):
    c=sqlite3.connect(path.resolve().as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=10)
    c.row_factory=sqlite3.Row
    return c


def proof(c,n,o):
    j=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(JOB,)).fetchone())
    q=dict(n.execute('SELECT * FROM x_post_queue WHERE id=?',(QUEUE,)).fetchone())
    l=dict(n.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(QUEUE,)).fetchone())
    assert (j['source_key'],j['stage'],j['status'],j['generation'],j['node_id'])==(KEY,'publish','unknown',1,'cpu-main')
    assert j['error_code']==l['error_code']=='coordinator_unavailable'
    assert not j['hold_reason'] and not j['canonical_receipt']
    assert q['status']==l['status']=='failed' and q['delivery_mode']=='direct'
    assert l['unknown_outcome']==1 and l['attempt_count']==1
    assert not any(l[k] for k in ('x_post_id','x_media_id','published_at'))
    assert not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?',(KEY,)).fetchone()
    assert not n.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?',(KEY,)).fetchone()
    assert not n.execute('SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?',(QUEUE,)).fetchone()
    assert not n.execute('SELECT 1 FROM '+TABLE+' WHERE source_key=?',(KEY,)).fetchone()
    native_ops=[dict(r) for r in n.execute('SELECT * FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id',(KEY,))]
    central_ops=[dict(r) for r in c.execute('SELECT * FROM pipeline_operation WHERE job_id=? ORDER BY operation_id',(JOB,))]
    assert native_ops and len(native_ops)==len(central_ops)
    assert all(no_post_operation(r['operation']) and r['generation']==1 for r in native_ops+central_ops)
    assert [{k:r[k] for k in ('operation_id','generation','operation','request_sha256')} for r in native_ops]==[{k:r[k] for k in ('operation_id','generation','operation','request_sha256')} for r in central_ops]
    for table,db,column,identity in [('pipeline_x_checkpoint',n,'source_key',KEY),('pipeline_checkpoint',c,'job_id',JOB)]:
        assert all(r[0] in ('x_preflight','x_media_initialized') for r in db.execute('SELECT phase FROM '+table+' WHERE '+column+'=?',(identity,)))
    receipts=[]
    for r in o.execute('SELECT id,route,payload,acked_at FROM receipt_outbox WHERE json_extract(payload,\'$.job_id\')=?',(JOB,)):
        p=json.loads(r['payload']);assert r['acked_at'] is not None and p['generation']==1
        assert r['route'] in ('jobs/checkpoint','jobs/fail')
        if r['route']=='jobs/checkpoint':assert p['phase']=='x_media_initialized'
        else:assert p['error_code']=='coordinator_unavailable'
        receipts.append(dict(r))
    assert any(r['route']=='jobs/fail' for r in receipts)
    # The source's only admitted owner has already acknowledged failure and
    # moved to another job; expiry by itself is never used as proof.
    worker=c.execute('SELECT last_seen_at,observation_json FROM pipeline_worker WHERE node_id=? AND worker_id=?',('cpu-main',j['worker_id'])).fetchone()
    assert worker and json.loads(worker['observation_json']).get('phase')=='idle'
    assert 0 <= (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(worker['last_seen_at'])).total_seconds() < 90
    claim=dict(n.execute("SELECT * FROM pipeline_x_claim WHERE source_key=? AND stage='publish' AND generation=1",(KEY,)).fetchone())
    desc=json.loads(claim['descriptor_json'])
    assert desc['kind']=='x_publish' and desc['request']['body']==l['post_text']
    assert desc['account_resource']=='x:account:'+str(q['account_id'])
    artifact=desc['artifact'];assert artifact.get('sha256') and artifact.get('size')
    prepared=[dict(r) for r in n.execute('SELECT * FROM pipeline_x_artifact WHERE queue_id=?',(QUEUE,))]
    random=[dict(r) for r in n.execute('SELECT * FROM x_post_random_template_preparation WHERE queue_id=?',(QUEUE,))]
    assert prepared or random
    if not q.get('random_template_enabled'):
        assert len(prepared)==1 and json.loads(prepared[0]['artifact_json'])==artifact
    for field,table in [('manual_run_id','x_post_manual_run'),('schedule_run_id','x_post_schedule_run'),('run_id','x_post_daily_run'),('catchup_run_id','x_post_catchup_run')]:
        if q.get(field):
            parent=n.execute('SELECT * FROM '+table+' WHERE id=?',(q[field],)).fetchone()
            assert parent and parent['status'] not in ('cancelled','canceled','stopped')
            if parent['status']=='needs_review':
                # This single-task parent was marked for review by this exact
                # acknowledged failure. Do not lift unrelated parent's holds.
                assert field=='manual_run_id' and q[field]==2228 and parent['unknown_count']==1
                assert [r[0] for r in n.execute('SELECT id FROM x_post_queue WHERE manual_run_id=?',(2228,))]==[QUEUE]
    return {'job':j,'queue':q,'log':l,'native_ops':native_ops,'central_ops':central_ops,'acked_receipts':receipts,
            'claim':claim,'artifact':artifact,'prepared':prepared,'random':random}


def main(apply=False):
    with contextlib.ExitStack() as stack:
        c=stack.enter_context(contextlib.closing(connect(CENTRAL,apply)))
        n=stack.enter_context(contextlib.closing(connect(NATIVE,apply)))
        o=stack.enter_context(contextlib.closing(connect(OUTBOX)))
        checked=proof(c,n,o)
        if not apply:
            print(json.dumps({'queue_id':QUEUE,'verified_only_media_initialize_append':True,'operations':len(checked['native_ops']),'owner_failure_acked':True,'original_artifact_sha256':checked['artifact']['sha256']}));return
        FOLDER.mkdir(mode=0o700,exist_ok=False)
        backup_database(CENTRAL,FOLDER/'dispatch-before.sqlite3')
        backup_database(NATIVE,FOLDER/'native-before.sqlite3')
        n.execute('BEGIN IMMEDIATE');c.execute('BEGIN IMMEDIATE')
        checked=proof(c,n,o)
        (FOLDER/'proof.json').write_text(json.dumps(checked,ensure_ascii=False,indent=2)+'\n')
        (FOLDER/'proof.json').chmod(0o600)
        now=dt.datetime.now(dt.timezone.utc).isoformat();q,l,j=checked['queue'],checked['log'],checked['job']
        plan_hash=digest(checked)
        n.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (KEY,'publish',1,2,1,queue_hash(q),log_hash(l),digest(checked['artifact']),plan_hash,'armed','fb-support-deployment-recovery',now,'',encoded(checked)))
        n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",(now,QUEUE))
        n.execute("UPDATE x_post_publish_log SET status='reserved',unknown_outcome=0,updated_at=? WHERE id=?",(now,l['id']))
        from features.x_posts.service import XPostStore
        XPostStore._sync_run(n,QUEUE,now)
        meta=json.loads(j['source_meta_json']);meta.update(reconcile_only=False,legacy_inflight=False)
        c.execute("UPDATE pipeline_job SET status='ready',external_intent=0,started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',source_meta_json=?,updated_at=? WHERE id=? AND status='unknown' AND generation=1",(encoded(meta),now,JOB))
        for table in ('pipeline_resource','pipeline_legacy_resource'):
            c.execute('DELETE FROM '+table+' WHERE job_id=?',(JOB,))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
            (JOB,'audited_media_only_switch_recovery',encoded({'plan_sha256':plan_hash,'proof_file':str(FOLDER/'proof.json'),'original_unknown_receipt_retained':True,'from_generation':1,'to_generation':2,'artifact_sha256':checked['artifact']['sha256'],'no_post_or_finalize_intent':True}),now))
        # Native arming first is safe: dispatch remains unknown until the next
        # commit. If interrupted, preserve the backup/proof; never rerun blindly.
        n.commit();c.commit()
        result={'armed':True,'queue_id':QUEUE,'job_id':JOB,'next_generation':2,'backup':str(FOLDER),'plan_sha256':plan_hash,'platform_called':False}
        (FOLDER/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');main(p.parse_args().apply)

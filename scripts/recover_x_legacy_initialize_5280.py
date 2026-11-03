#!/usr/bin/env python3
"""One original legacy initialize-403 failure, with no invented dispatch history.

This deliberately separate incident tool only admits queue 5280. Original
native evidence, account, body, recipe, artifact and attempt 1 are preserved;
the next real pipeline claim is generation 1 and initialize consumes attempt 2.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import TABLE, SCHEMA, digest, encoded, queue_hash, log_hash
from scripts.recover_x_prepost_failures import backup, writable, stable_job, media_facts
from scripts.recover_pipeline_x_prepared_publish import artifact_from_receipt
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, date, document_hash, utcnow

EXACT = dict(queue_id=5280, log_id=5229, account_id=49, username='Vani4ka94', x_user_id='330544925',
             manual_run_id=1325, job_id=7818, run_date='2026-11-02',
             failed_at='2026-11-01T19:17:39Z',
             error_sha256='791af13c90e5f4081f86601c1a5733a9da655d2bae87bdf647d2974c03c36f07',
             artifact_sha256='4f5a2046536eaa4b158af9e114ab109d77ece30a2f0ca9e52c3e40e8147e036b', artifact_size=135115095,
             artifact_duration=220.002)
JOURNAL = 'pipeline_x_legacy_initialize_recovery'
EVENT = 'audited_legacy_initialize_403_recovery'
HOLD = 'operator_legacy_initialize_recovery_pending'
JOURNAL_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+JOURNAL+' (queue_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,proof_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)'


def exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def native_facts(n):
    qid=EXACT['queue_id'];key='x:queue:'+str(qid)
    found=rows(n,'SELECT * FROM x_post_queue WHERE id=?',(qid,));require(len(found)==1,'queue_missing');q=found[0]
    found=rows(n,'SELECT * FROM x_post_publish_log WHERE queue_id=?',(qid,));require(len(found)==1,'log_not_unique');log=found[0]
    require((q['account_id'],q['account_username'],q['manual_run_id'],q['run_date'],log['id'])==
            (EXACT['account_id'],EXACT['username'],EXACT['manual_run_id'],EXACT['run_date'],EXACT['log_id']), 'exact_legacy_identity_changed')
    require(q['delivery_mode']=='direct' and q['source_type']=='material' and q['random_template_enabled']==1, 'legacy_queue_kind_changed')
    require(log['account_id']==q['account_id'] and log['attempt_count']==1 and not any(log[k] for k in ('unknown_outcome','x_media_id','x_post_id','published_at')),
            'attempt_unknown_or_success_evidence')
    require(log['error_code']=='x_upstream_error' and hashlib.sha256(log['error_message'].encode()).hexdigest()==EXACT['error_sha256']
            and log['started_at']==EXACT['failed_at'] and all(log[k] for k in ('long_url','short_url','post_text')), 'original_initialize_failure_changed')
    require(not n.execute("SELECT 1 FROM x_post_publish_log WHERE id<>? AND account_id=? AND post_text=? AND (unknown_outcome<>0 OR x_post_id<>'' OR published_at<>'' OR status IN ('post_creating','published'))",
                          (log['id'],q['account_id'],log['post_text'])).fetchone(),'same_body_success_or_unknown_evidence')
    for table,column,value in (('pipeline_x_claim','source_key',key),('pipeline_x_operation','source_key',key),
            ('pipeline_x_checkpoint','source_key',key),('pipeline_canonical_hold','source_key',key),('x_post_repost_ledger','queue_id',qid)):
        require(not n.execute('SELECT 1 FROM '+table+' WHERE '+column+'=?',(value,)).fetchone(),'native_execution_or_hold_evidence')
    account=rows(n,'SELECT id,x_user_id,username,status,publish_approved FROM x_authorized_account WHERE id=?',(q['account_id'],))
    require(len(account)==1 and account[0]['x_user_id']==EXACT['x_user_id'] and account[0]['username']==EXACT['username']
            and account[0]['status']=='active' and account[0]['publish_approved']==1,'account_identity_or_approval_changed')
    parent=rows(n,'SELECT * FROM x_post_manual_run WHERE id=?',(q['manual_run_id'],))
    require(len(parent)==1 and parent[0]['trigger_source']=='auto_template' and parent[0]['status'] not in ('cancelled','canceled','stopped','needs_review')
            and not parent[0]['unknown_count'] and not parent[0]['published_count']
            and n.execute('SELECT count(*) FROM x_post_queue WHERE manual_run_id=?',(q['manual_run_id'],)).fetchone()[0]==1,'legacy_parent_changed')
    parent={k:v for k,v in parent[0].items() if k not in ('status','updated_at','finished_at','queued_count','failed_count','published_count','unknown_count','error_code','error_message')}
    media=media_facts(n,qid);prep=media['x_post_random_template_preparation'];require(len(prep)==1 and prep[0]['status']=='ready','frozen_artifact_missing')
    request,result=json.loads(prep[0]['request_json']),json.loads(prep[0]['result_json']);artifact=artifact_from_receipt(request,result)
    require(request['queue_id']==qid and request['content_id']==str(q['content_id']) and request['source_url']==q['material_url']
            and request['source_sha256']==q['preflight_sha256'] and request['source_size']==q['preflight_size']
            and abs(float(request['source_duration'])-float(q['preflight_duration']))<=.05,'frozen_source_changed')
    require(artifact['sha256']==EXACT['artifact_sha256'] and artifact['size']==EXACT['artifact_size']
            and artifact['duration_seconds']==EXACT['artifact_duration'],'exact_final_artifact_changed')
    attribution=rows(n,'SELECT * FROM x_post_link_attribution WHERE queue_id=?',(qid,)) if exists(n,'x_post_link_attribution') else []
    frozen_log={k:v for k,v in log.items() if k not in ('status','updated_at')}
    proof=dict(queue_sha256=queue_hash(q),log=frozen_log,media=media,attribution=attribution,parent=parent,account=account[0])
    return dict(queue=q,log=log,artifact=artifact,media=media,proof_sha256=digest(proof),proof=proof)


def snapshot(c,n,now):
    facts=native_facts(n);q,log=facts['queue'],facts['log'];key='x:queue:'+str(q['id'])
    require(q['status']=='failed' and log['status']=='failed' and q['updated_at']==EXACT['failed_at'] and log['updated_at']==EXACT['failed_at'], 'legacy_failure_state_changed')
    require(date(log['updated_at'])<=now-dt.timedelta(seconds=120),'failure_not_settled')
    require(not exists(n,TABLE) or not n.execute('SELECT 1 FROM '+TABLE+' WHERE source_key=?',(key,)).fetchone(),'native_recovery_already_exists')
    jobs=rows(c,'SELECT * FROM pipeline_job WHERE source_key=?',(key,));require(len(jobs)==1,'legacy_dispatch_scope_changed');j=jobs[0]
    require((j['id'],j['source_id'],j['channel'],j['family'],j['stage'],j['status'],j['generation'])==
            (EXACT['job_id'],str(q['id']),'x','queue','publish','failed',0), 'legacy_job_not_unclaimed')
    require(j['hold_reason']=='adapter_unavailable' and not any(j[k] for k in ('error_code','external_intent','canonical_receipt','claim_hash','node_id','worker_id','lease_expires_at')),
            'legacy_dispatch_failure_changed')
    meta=json.loads(j['source_meta_json'])
    require(meta.get('canonical_fingerprint')==canonical_fingerprint(q,'id') and not any(meta.get(k) for k in ('unknown_outcome','legacy_inflight','publish_id'))
            and meta.get('source_attempt_count')==1 and meta.get('reconcile_only') is True
            and meta.get('execution_available') is False,'legacy_projection_identity_changed')
    require(json.loads(j['resource_keys_json'])==['x:account:'+str(q['account_id'])],'legacy_account_resource_changed')
    central_artifact=json.loads(j['artifact_json']);artifact=facts['artifact']
    # This exact pre-dispatch import omitted duration/recipe. Restore only the
    # projection of the already validated original object; archive the old JSON.
    require(set(central_artifact)=={'url','sha256','size','mime','duration_seconds','profile'}
            and central_artifact['duration_seconds']==0
            and all(central_artifact[k]==artifact[k] for k in ('url','sha256','size','mime','profile')),'legacy_artifact_projection_changed')
    for table in ('pipeline_claim_history','pipeline_operation','pipeline_checkpoint','pipeline_receipt','pipeline_delivery','pipeline_resource','pipeline_legacy_resource'):
        require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id=?',(j['id'],)).fetchone(),'legacy_dispatch_execution_evidence')
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?',(key,)).fetchone(),'publication_evidence')
    events=rows(c,'SELECT * FROM pipeline_event WHERE job_id=? ORDER BY id',(j['id'],))
    require(events and all(e['event_type']=='offered' for e in events),'legacy_execution_history_changed')
    require(not exists(c,JOURNAL) or not c.execute('SELECT 1 FROM '+JOURNAL+' WHERE queue_id=?',(q['id'],)).fetchone(),'legacy_recovery_already_recorded')
    return dict(queue_id=q['id'],log_id=log['id'],source_key=key,job_id=j['id'],scope_job_ids=[j['id']],stage='publish',from_generation=0,to_generation=1,
                prior_attempts=1,prior_queue=q,prior_log=log,prior_job=dict(j),events=events,native_facts=facts,artifact_sha256=digest(artifact))


def make_plan(central,native,now=None):
    now=now or utcnow()
    with contextlib.closing(read_connection(central)) as c,contextlib.closing(read_connection(native)) as n:
        c.execute('BEGIN');n.execute('BEGIN');item=snapshot(c,n,now)
        nodes=[r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]
    require(nodes,'node_inventory_missing')
    plan=dict(kind='x_legacy_initialize_5280',version=1,central=str(Path(central).resolve()),native=str(Path(native).resolve()),item=item,nodes=nodes,captured_at=now.isoformat())
    plan['plan_sha256']=document_hash(plan);return plan


def validate_plan(plan):
    require(plan.get('kind')=='x_legacy_initialize_5280' and plan.get('version')==1 and document_hash(plan)==plan['plan_sha256'],'plan_changed')
    require((plan['item']['queue_id'],plan['item']['log_id'],plan['item']['job_id'])==(EXACT['queue_id'],EXACT['log_id'],EXACT['job_id']), 'exact_incident_required')


def node_evidence(plan,node_id,outbox,now=None):
    validate_plan(plan);require(node_id in plan['nodes'],'node_not_in_plan');matched=[]
    with contextlib.closing(read_connection(outbox)) as c:
        for row in c.execute('SELECT id,route,payload,acked_at FROM receipt_outbox'):
            body=json.loads(row['payload'])
            if str(body.get('job_id'))==str(plan['item']['job_id']):
                matched.append(dict(receipt_id=row['id'],route=row['route'],payload_sha256=digest(body),acked=row['acked_at'] is not None))
    return dict(plan_sha256=plan['plan_sha256'],node_id=node_id,scope_job_ids=[plan['item']['job_id']],captured_at=(now or utcnow()).isoformat(),receipts=matched)


def apply_plan(plan,evidence,backup_dir,actor,*,confirm_account_unlocked=False,now=None,stop_after=None):
    now=now or utcnow();validate_plan(plan);item=plan['item'];key=item['source_key']
    require(confirm_account_unlocked is True and str(actor).strip(),'explicit_unlock_confirmation_required')
    require(sorted(e['node_id'] for e in evidence)==plan['nodes'],'node_evidence_scope')
    for e in evidence:
        require(e['plan_sha256']==plan['plan_sha256'] and e['scope_job_ids']==[item['job_id']]
                and 0<=(now-date(e['captured_at'])).total_seconds()<=120 and not e['receipts'],'legacy_node_evidence_not_absent_or_fresh')
    backups=[backup(plan['central'],backup_dir,'legacy-central'),backup(plan['native'],backup_dir,'legacy-native')]
    with contextlib.closing(writable(plan['central'])) as c,contextlib.closing(writable(plan['native'])) as n:
        c.execute(JOURNAL_SCHEMA);n.execute(SCHEMA)
        journal=c.execute('SELECT * FROM '+JOURNAL+' WHERE queue_id=?',(item['queue_id'],)).fetchone()
        require(not journal or journal['plan_sha256']==plan['plan_sha256'],'different_legacy_recovery_exists')
        if journal and journal['state']=='committed':
            current=c.execute('SELECT status,generation FROM pipeline_job WHERE id=?',(item['job_id'],)).fetchone()
            return dict(already_applied=True,queue_id=item['queue_id'],current_status=current['status'],current_generation=current['generation'],
                        plan_sha256=plan['plan_sha256'],backups=backups,platform_called=False)
        if not journal:
            c.execute('BEGIN IMMEDIATE');n.execute('BEGIN')
            try:
                require(snapshot(c,n,now)==item,'plan_state_drift')
                audit=dict(plan_sha256=plan['plan_sha256'],actor=actor,confirmed_unlocked=True,evidence_kind='original_native_initialize_403',original=item,backups=backups)
                c.execute('INSERT INTO '+JOURNAL+' VALUES(?,?,?,?,?,?)',(item['queue_id'],plan['plan_sha256'],'staged',encoded(audit),now.isoformat(),now.isoformat()))
                c.execute('UPDATE pipeline_job SET hold_reason=? WHERE id=? AND generation=0 AND status=\'failed\'',(HOLD,item['job_id']))
                c.commit();n.rollback()
            except BaseException:c.rollback();n.rollback();raise
        if stop_after=='central_staged':raise RecoveryRejected('injected_stop_after_stage')
        n.execute('BEGIN IMMEDIATE')
        try:
            facts=native_facts(n);require(facts['proof_sha256']==item['native_facts']['proof_sha256'],'native_frozen_evidence_changed')
            auth=n.execute('SELECT * FROM '+TABLE+' WHERE source_key=?',(key,)).fetchall()
            if not auth:
                require(facts['queue']==item['prior_queue'] and facts['log']==item['prior_log'],'native_state_drift')
                n.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,'publish',0,1,1,queue_hash(facts['queue']),log_hash(facts['log']),item['artifact_sha256'],plan['plan_sha256'],'armed',actor,now.isoformat(),'',encoded(item)))
                n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",(now.isoformat(),item['queue_id']))
                n.execute("UPDATE x_post_publish_log SET status='reserved',updated_at=? WHERE id=?",(now.isoformat(),item['log_id']))
                from features.x_posts.service import XPostStore
                XPostStore._sync_run(n,item['queue_id'],now.isoformat())
            else:
                require(len(auth)==1 and auth[0]['plan_sha256']==plan['plan_sha256'] and auth[0]['state']=='armed'
                        and facts['queue']['status']=='queued' and facts['log']['status']=='reserved','legacy_authorization_changed')
            n.commit()
        except BaseException:n.rollback();raise
        if stop_after=='native_armed':raise RecoveryRejected('injected_stop_after_native')
        c.execute('BEGIN IMMEDIATE')
        try:
            current=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(item['job_id'],)).fetchone())
            require(stable_job(current)==dict(stable_job(item['prior_job']),hold_reason=HOLD),'central_state_drift')
            meta=json.loads(current['source_meta_json']);meta.update(reconcile_only=False,legacy_inflight=False,execution_available=True)
            changed=c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',started_at='',finished_at='',source_meta_json=?,artifact_json=?,updated_at=? WHERE id=? AND status='failed' AND generation=0",
                              (encoded(meta),encoded(item['native_facts']['artifact']),now.isoformat(),item['job_id'])).rowcount
            require(changed==1,'central_admission_compare_failed')
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(item['job_id'],EVENT,encoded(dict(plan_sha256=plan['plan_sha256'],actor=actor,prior_attempts=1,next_generation=1,proof_kind='legacy_native_initialize_403')),now.isoformat()))
            c.execute('UPDATE '+JOURNAL+" SET state='committed',updated_at=? WHERE queue_id=?",(now.isoformat(),item['queue_id']));c.commit()
        except BaseException:c.rollback();raise
    return dict(queue_id=item['queue_id'],plan_sha256=plan['plan_sha256'],next_generation=1,preserved_attempt_count=1,backups=backups,platform_called=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('plan','node-evidence','apply'))
    for name in ('central','native','plan','node-id','outbox','backup-dir','actor'):p.add_argument('--'+name)
    p.add_argument('--node-evidence',nargs='+');p.add_argument('--confirm-account-unlocked',action='store_true');p.add_argument('--output',required=True);a=p.parse_args()
    with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as output:
        if a.action=='plan':result=make_plan(a.central,a.native)
        else:
            plan=json.loads(Path(a.plan).read_text(encoding='utf-8'))
            if a.action=='node-evidence':result=node_evidence(plan,a.node_id,a.outbox)
            else:result=apply_plan(plan,[json.loads(Path(x).read_text(encoding='utf-8')) for x in a.node_evidence],a.backup_dir,a.actor,confirm_account_unlocked=a.confirm_account_unlocked)
        output.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n');output.flush();os.fsync(output.fileno())
    print(json.dumps(dict(ok=True,action=a.action,plan_sha256=result['plan_sha256'])))


if __name__=='__main__':
    try:main()
    except RecoveryRejected as exc:print(json.dumps(dict(ok=False,code=str(exc))));raise SystemExit(2)

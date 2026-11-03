#!/usr/bin/env python3
"""Exact zero-write attribution failures: read-only diagnosis and central admission.

Native queues, logs, attempts, links and prepared media are never modified.
Resolver evidence comes from the existing read-only internal endpoint; no short
redirect is created and no platform request is made by this utility.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_prepost_recovery import digest, encoded
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.x_posts.attribution import validate_context, build_url
from scripts.recover_x_prepost_failures import media_facts, stable_job, backup, writable, node_evidence, validate_nodes
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, date, document_hash, utcnow

QUEUE_IDS = (5399, 5407, 5451, 5486, 5544, 5550, 5552)
# A later failure is a separate, exact admission, not a license to reset any
# subsequent attempt on this queue. Keep the original six incidents unchanged.
EXACT_FAILURES = {5451: {
    'log_id': 5384, 'prepare_job_id': 31671, 'publish_job_id': 32082,
    'publish_generation': 1, 'finished_at': '2026-11-03T10:54:02.093+00:00',
    'account_id': 95, 'manual_run_id': 1477,
}}
DAYS = ('2026-11-02', '2026-11-03')
ERROR = 'x_attribution_resolution_unavailable'
EVENT = 'audited_zero_write_attribution_recovery'


def snapshot(c, n, qid, now):
    require(qid in QUEUE_IDS, 'exact_incident_ids_required')
    key = 'x:queue:' + str(qid)
    queues = rows(n, 'SELECT * FROM x_post_queue WHERE id=?', (qid,))
    require(len(queues)==1, 'queue_missing'); q=queues[0]
    require(q['status']=='queued' and q.get('delivery_mode','direct')=='direct', 'native_queue_not_unstarted')
    logs=rows(n,'SELECT * FROM x_post_publish_log WHERE queue_id=?',(qid,))
    require(len(logs)==1,'native_log_not_unique'); log=logs[0]
    require(log['status']=='reserved' and not any(log[k] for k in ('attempt_count','unknown_outcome','x_media_id','x_post_id','published_at','started_at','post_text','long_url','short_url')), 'native_publish_or_link_evidence')
    require(not n.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?',(key,)).fetchone(), 'native_hold')
    require(not n.execute('SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?',(qid,)).fetchone(), 'relay_evidence')
    require(not n.execute('SELECT 1 FROM pipeline_x_operation WHERE source_key=?',(key,)).fetchone(),'native_operation_evidence')
    require(not n.execute('SELECT 1 FROM x_post_link_attribution WHERE queue_id=?',(qid,)).fetchone(),'attribution_already_frozen')
    jobs=rows(c,'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage',(key,))
    require(len(jobs)==2 and {j['stage'] for j in jobs}=={'prepare','publish'},'dispatch_stages_changed')
    prep,pub=({j['stage']:j for j in jobs}[s] for s in ('prepare','publish'))
    if qid in EXACT_FAILURES:
        require({'log_id': log['id'], 'prepare_job_id': prep['id'], 'publish_job_id': pub['id'],
                 'publish_generation': pub['generation'], 'finished_at': pub['finished_at'],
                 'account_id': q['account_id'], 'manual_run_id': q['manual_run_id']}
                == EXACT_FAILURES[qid], 'exact_failure_identity_changed')
    require(prep['status']=='completed' and pub['status']=='failed' and pub['generation']>0
            and pub['error_code']==ERROR and not pub['canonical_receipt'], 'dispatch_not_exact_failure')
    due=date(json.loads(pub['source_meta_json']).get('cutover_due_at') or pub['scheduled_at'])
    require((due+dt.timedelta(hours=8)).date().isoformat() in DAYS,'outside_incident_dates')
    require(pub['finished_at'] and date(pub['finished_at'])<=now-dt.timedelta(seconds=120),'failure_not_settled')
    ids=[j['id'] for j in jobs];marks=','.join('?' for _ in ids)
    for j in jobs:
        meta=json.loads(j['source_meta_json'])
        require((j['channel'],j['family'],j['source_id'])==('x','queue',str(qid))
                and meta.get('canonical_fingerprint')==canonical_fingerprint(q,'id'), 'native_frozen_identity_changed')
        require(not j['external_intent'] and not j['hold_reason'] and (not j['lease_expires_at'] or date(j['lease_expires_at'])<=now)
                and not any(meta.get(k) for k in ('unknown_outcome','reconcile_only','source_attempt_count','publish_id')), 'dispatch_write_hold_or_lease')
    for table in ('pipeline_operation','pipeline_resource','pipeline_legacy_resource'):
        require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id IN ('+marks+')',ids).fetchone(),'dispatch_operation_or_active_fence')
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?',(key,)).fetchone(),'publication_evidence')
    for table in ('pipeline_checkpoint','pipeline_receipt'):
        require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id=?',(pub['id'],)).fetchone(),'publish_receipt_evidence')
    checkpoints=rows(n,'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase',(key,))
    require(all(r['phase']=='x_preflight' for r in checkpoints),'native_later_checkpoint')
    claims=rows(n,'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation',(key,))
    require(claims and all(r['stage']=='prepare' and r['queue_id']==qid and r['log_id']==log['id'] for r in claims),'native_publish_claim_or_identity')
    history=rows(c,'SELECT * FROM pipeline_claim_history WHERE job_id IN ('+marks+') ORDER BY job_id,generation',ids)
    for j in jobs:
        previous=[r for r in history if r['job_id']==j['id']]
        require([r['generation'] for r in previous]==list(range(1,j['generation']+1)) and previous
                and (previous[-1]['claim_hash'],previous[-1]['node_id'],previous[-1]['worker_id'])
                ==(j['claim_hash'],j['node_id'],j['worker_id']), 'claim_history_missing_or_changed')
    h={(r['job_id'],r['generation']):r for r in history}
    require((pub['id'],pub['generation']) in h,'publish_history_missing')
    require(all((prep['id'],r['generation']) in h and h[(prep['id'],r['generation'])]['claim_hash']==r['claim_hash'] for r in claims),'native_claim_history_conflict')
    events=rows(c,"SELECT * FROM pipeline_event WHERE job_id IN ("+marks+") AND event_type='failed' ORDER BY id",ids)
    require(all(json.loads(e['details_json']).get('unknown') is False for e in events),'historical_unknown')
    require(any(e['job_id']==pub['id'] and json.loads(e['details_json']).get('code')==ERROR for e in events),'known_failure_missing')
    media=media_facts(n,qid)
    if q.get('random_template_enabled'):
        prepared=media['x_post_random_template_preparation']
        require(len(prepared)==1 and prepared[0]['status']=='ready','prepared_artifact_missing')
        from scripts.recover_pipeline_x_prepared_publish import artifact_from_receipt
        request=json.loads(prepared[0]['request_json'])
        artifact=artifact_from_receipt(request,json.loads(prepared[0]['result_json']))
        sources=media['pipeline_x_artifact'];require(len(sources)==1,'source_artifact_missing')
        source=json.loads(sources[0]['artifact_json'])
        require(request['queue_id']==qid and request.get('content_id')==str(q['content_id'])
                and sources[0]['source_url']==q['material_url']
                and request['source_url']==source.get('url')
                and request['source_sha256']==source.get('sha256')==q['preflight_sha256']
                and request['source_size']==source.get('size')==q['preflight_size'], 'native_final_source_mismatch')
    else:
        source=media['pipeline_x_artifact'];require(len(source)==1,'source_artifact_missing')
        artifact=json.loads(source[0]['artifact_json'])
    completed=json.loads(prep['artifact_json'])
    # Central artifacts also retain renderer receipt fields. Compare every
    # canonical artifact field, and bind the entire central row in the proof.
    require(artifact.get('sha256') and artifact.get('size') and all(completed.get(k)==v for k,v in artifact.items()),'completed_artifact_changed')
    parents=[]
    for field,table in (('manual_run_id','x_post_manual_run'),('schedule_run_id','x_post_schedule_run'),('run_id','x_post_daily_run'),('catchup_run_id','x_post_catchup_run')):
        if q.get(field):
            parent=rows(n,'SELECT * FROM '+table+' WHERE id=?',(q[field],))
            require(len(parent)==1 and parent[0]['status'] not in ('cancelled','canceled','stopped','needs_review'),'parent_held')
            parents.append({k:v for k,v in parent[0].items() if k not in ('status','updated_at','finished_at','published_count','failed_count','unknown_count')})
    require(len(parents)==1,'parent_not_unique')
    related={t:rows(c,'SELECT * FROM '+t+' WHERE job_id IN ('+marks+') ORDER BY job_id',ids) for t in ('pipeline_checkpoint','pipeline_receipt','pipeline_delivery')}
    proof={'queue':q,'log':log,'jobs':[stable_job(j) for j in jobs],'claims':claims,'history':history,'events':events,'media':media,'parents':parents,'checkpoints':checkpoints,'related':related}
    return {'queue_id':qid,'log_id':log['id'],'source_key':key,'job_id':pub['id'],'from_generation':pub['generation'],'to_generation':pub['generation']+1,
            'node_id':pub['node_id'],'scope_job_ids':ids,'prior_job':stable_job(pub),'proof_sha256':digest(proof),'artifact_sha256':digest(artifact)}


def make_plan(central,native,queue_ids,now=None):
    now=now or utcnow()
    require(queue_ids and len(set(queue_ids))==len(queue_ids) and set(queue_ids)<=set(QUEUE_IDS),'exact_incident_ids_required')
    with contextlib.closing(read_connection(central)) as c,contextlib.closing(read_connection(native)) as n:
        c.execute('BEGIN');n.execute('BEGIN')
        items=[snapshot(c,n,qid,now) for qid in sorted(queue_ids)]
        nodes=[r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]
    require(nodes,'node_inventory_missing')
    plan={'kind':'x_zero_write_attribution_recovery','version':1,'central':str(Path(central).resolve()),'native':str(Path(native).resolve()),'items':items,'nodes':nodes,'captured_at':now.isoformat()}
    plan['plan_sha256']=document_hash(plan);return plan


def resolve_evidence(plan,token,now=None,opener=urllib.request.urlopen):
    validate_plan(plan)
    require(len(str(token))>=32,'internal_auth_missing')
    now=now or utcnow();items=[]
    with contextlib.closing(read_connection(plan['native'])) as n:
        for item in plan['items']:
            request=urllib.request.Request('http://127.0.0.1:8787/internal/x-posts/attribution/resolve',data=encoded({'queue_id':item['queue_id'],'log_id':item['log_id']}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
            result={'queue_id':item['queue_id'],'log_id':item['log_id'],'status':0,'valid':False}
            try:
                try:
                    with opener(request,timeout=30) as response:status=response.status;raw=response.read(65537)
                except urllib.error.HTTPError as exc:status=exc.code;raw=exc.read(65537)
                result['status']=status;require(len(raw)<=65536,'resolver_response_too_large')
                body=json.loads(raw)
                if status==200:
                    log=n.execute('SELECT * FROM x_post_publish_log WHERE id=? AND queue_id=?',(item['log_id'],item['queue_id'])).fetchone()
                    require(log is not None,'native_log_missing')
                    context=validate_context(body['context'],dict(log));build_url(context,dict(log))
                    result.update(valid=True,context_sha256=digest(context))
                else:
                    code=body.get('error') or body.get('code')
                    result['error']=code if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,119}',code) else 'resolver_http_error'
            except Exception as exc:
                code=getattr(exc,'code','resolver_unavailable')
                result['error']=code if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,119}',code) else 'resolver_unavailable'
            items.append(result)
    return {'plan_sha256':plan['plan_sha256'],'captured_at':now.isoformat(),'items':items,'readonly':True}


def validate_plan(plan):
    require(plan.get('kind')=='x_zero_write_attribution_recovery' and plan.get('version')==1
            and document_hash(plan)==plan['plan_sha256'],'plan_hash_changed')
    ids=[item['queue_id'] for item in plan['items']]
    require(ids and len(set(ids))==len(ids) and set(ids)<=set(QUEUE_IDS),'exact_incident_ids_required')


def apply_plan(plan,evidence,resolution,backup_dir,actor,now=None):
    now=now or utcnow()
    validate_plan(plan)
    require(str(actor).strip(),'actor_required');validate_nodes(plan,evidence,now)
    require(resolution.get('plan_sha256')==plan['plan_sha256'] and 0<=(now-date(resolution['captured_at'])).total_seconds()<=120,'resolver_evidence_stale')
    require(sorted((r['queue_id'],r['log_id']) for r in resolution['items'])==sorted((i['queue_id'],i['log_id']) for i in plan['items'])
            and all(r['status']==200 and r['valid'] and re.fullmatch(r'[a-f0-9]{64}',r.get('context_sha256','')) for r in resolution['items']), 'resolver_not_verified')
    saved_backup=backup(plan['central'],backup_dir,'attribution-central')
    with contextlib.closing(writable(plan['central'])) as c,contextlib.closing(read_connection(plan['native'])) as n:
        c.execute('BEGIN IMMEDIATE');n.execute('BEGIN')
        try:
            for item in plan['items']:
                previous=c.execute('SELECT details_json FROM pipeline_event WHERE job_id=? AND event_type=?',(item['job_id'],EVENT)).fetchall()
                if any(json.loads(r[0]).get('plan_sha256')==plan['plan_sha256'] for r in previous):continue
                require(snapshot(c,n,item['queue_id'],now)==item,'plan_state_drift')
                changed=c.execute("UPDATE pipeline_job SET status='ready',node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',updated_at=? WHERE id=? AND generation=? AND status='failed' AND error_code=? AND external_intent=0",
                                  (now.isoformat(),item['job_id'],item['from_generation'],ERROR)).rowcount
                require(changed==1,'recovery_compare_and_swap_failed')
                audit={'plan_sha256':plan['plan_sha256'],'actor':str(actor),'previous_execution':item['prior_job'],'proof_sha256':item['proof_sha256'],'backup_path':saved_backup,'resolver_context_sha256':next(r['context_sha256'] for r in resolution['items'] if r['queue_id']==item['queue_id'])}
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(item['job_id'],EVENT,encoded(audit),now.isoformat()))
            c.commit()
        except BaseException:c.rollback();raise
    return {'plan_sha256':plan['plan_sha256'],'queue_ids':[i['queue_id'] for i in plan['items']],'backup_path':saved_backup,'native_modified':False,'platform_called':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('plan','resolve-evidence','node-evidence','apply'))
    for name in ('central','native','plan','node-id','outbox','resolution','backup-dir','actor','token-pid'):p.add_argument('--'+name)
    p.add_argument('--queue-ids',nargs='+',type=int);p.add_argument('--node-evidence',nargs='+');p.add_argument('--output',required=True)
    a=p.parse_args()
    with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as output:
        if a.action=='plan':result=make_plan(a.central,a.native,a.queue_ids)
        else:
            plan=json.loads(Path(a.plan).read_text(encoding='utf-8'))
            if a.action=='node-evidence':result=node_evidence(plan,a.node_id,a.outbox)
            elif a.action=='resolve-evidence':
                env=os.environ
                if a.token_pid:
                    require(str(a.token_pid).isdigit() and int(a.token_pid)>1,'token_pid_invalid')
                    env=dict(x.split('=',1) for x in Path('/proc/'+a.token_pid+'/environ').read_text().split('\0') if '=' in x)
                token=(env.get('X_INTERNAL_TOKEN') or env.get('X_POST_AUTOMATION_INTERNAL_TOKEN') or '').strip()
                result=resolve_evidence(plan,token)
            else:result=apply_plan(plan,[json.loads(Path(x).read_text(encoding='utf-8')) for x in a.node_evidence],json.loads(Path(a.resolution).read_text(encoding='utf-8')),a.backup_dir,a.actor)
        output.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n');output.flush();os.fsync(output.fileno())
    print(json.dumps({'ok':True,'action':a.action,'plan_sha256':result['plan_sha256']}))


if __name__=='__main__':
    try:main()
    except RecoveryRejected as exc:print(json.dumps({'ok':False,'code':str(exc)}));raise SystemExit(2)

#!/usr/bin/env python3
"""Plan/apply exact known X failures before Post intent; no platform requests.

Native and dispatch commits are journaled separately. The intermediate central
hold is not claimable, so an interrupted apply is safe to resume with the same
plan. Original attempts, operations, receipts, media, body and account survive.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_prepost_recovery import (
    TABLE, SCHEMA, digest, encoded, queue_hash, log_hash, no_post_operation,
)
from scripts.recover_pipeline_x_preparation import (
    RecoveryRejected, date, document_hash, read_connection, require, rows, utcnow,
)

JOURNAL = 'pipeline_x_prepost_recovery_apply'
JOURNAL_SCHEMA = '''CREATE TABLE IF NOT EXISTS pipeline_x_prepost_recovery_apply (
 source_key TEXT NOT NULL,stage TEXT NOT NULL,from_generation INTEGER NOT NULL,
 plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,proof_json TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(source_key,stage,from_generation))'''
KNOWN_CODES = frozenset(('x_upstream_error', 'media_download_failed',
    'media_download_incomplete', 'youtube_source_download_failed',
    'canonical_bridge_unavailable', 'coordinator_unavailable', 'x_dispatch_failed',
    'x_attribution_resolution_unavailable', 'media_transcode_failed'))


def stable_job(row):
    return {k: v for k, v in row.items() if k not in ('updated_at', 'source_status', 'source_meta_json')}


def media_facts(conn, queue_id):
    return {table: rows(conn, 'SELECT * FROM '+table+' WHERE queue_id=?', (queue_id,))
            for table in ('pipeline_x_artifact', 'x_post_random_template_preparation')}


def snapshot(c, n, queue_id, stage, days, now):
    key = 'x:queue:' + str(queue_id)
    qs = rows(n, 'SELECT * FROM x_post_queue WHERE id=?', (queue_id,))
    require(len(qs) == 1, 'queue_missing'); q = qs[0]
    require(q['status'] == 'failed' and q.get('delivery_mode', 'direct') == 'direct', 'queue_not_known_direct_failure')
    logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (queue_id,))
    require(len(logs) == 1, 'one_native_log_required'); log = logs[0]
    require(log['status'] == 'failed' and not any(log[k] for k in ('unknown_outcome', 'x_post_id', 'x_media_id', 'published_at')),
            'unknown_or_published_native_result')
    require(log['error_code'] in KNOWN_CODES, 'native_failure_not_recoverable')
    require(log['attempt_count'] == 0 if stage == 'prepare' else log['attempt_count'] >= 0, 'prepare_has_publish_attempt')
    for table, col in [('pipeline_canonical_hold', 'source_key'), ('x_post_repost_ledger', 'queue_id')]:
        require(not n.execute('SELECT 1 FROM '+table+' WHERE '+col+'=?', (key if col=='source_key' else queue_id,)).fetchone(), 'native_hold_or_relay')
    jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage', (key,))
    chosen = [j for j in jobs if j['stage'] == stage]
    require(len(chosen) == 1, 'dispatch_job_missing'); job = chosen[0]
    require(job['status'] == 'failed' and job['generation'] > 0 and job['error_code'] in KNOWN_CODES
            and not job['hold_reason'] and not job['canonical_receipt'], 'dispatch_not_known_failure')
    original = date(json.loads(job['source_meta_json']).get('cutover_due_at') or job['scheduled_at'])
    require((original + dt.timedelta(hours=8)).date().isoformat() in days, 'outside_authorized_dates')
    require(job['finished_at'] and date(job['finished_at']) <= now-dt.timedelta(seconds=120), 'failure_not_settled')
    require(all(j['status'] in ('failed','completed') and (not j['lease_expires_at'] or date(j['lease_expires_at']) <= now)
                for j in jobs), 'source_inflight_or_unknown')
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone(), 'published_evidence')
    ids = [j['id'] for j in jobs]
    params = ','.join('?' for _ in ids)
    for table in ('pipeline_resource', 'pipeline_legacy_resource'):
        require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id IN ('+params+')', ids).fetchone(), 'source_resource_held')
    native_ops = rows(n, 'SELECT * FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id', (key,))
    central_ops = rows(c, 'SELECT * FROM pipeline_operation WHERE job_id IN ('+params+') ORDER BY operation_id', ids)
    require(all(no_post_operation(o['operation']) for o in native_ops+central_ops), 'post_or_finalize_intent')
    if stage == 'prepare':
        require(not native_ops and not central_ops and not log['attempt_count'] and not job['external_intent'], 'prepare_has_external_intent')
    # Match every native permit to the coordinator's immutable operation fact.
    ops_by_id = {o['operation_id']:o for o in central_ops}
    require(all(o['operation_id'] in ops_by_id and all(o[k] == ops_by_id[o['operation_id']][k]
                for k in ('generation','operation','request_sha256')) for o in native_ops), 'operation_ledgers_disagree')
    checkpoints = rows(n, 'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase', (key,))
    require(all(r['phase'] in ('x_preflight','x_media_initialized') for r in checkpoints), 'native_later_checkpoint')
    central_cp = rows(c, 'SELECT * FROM pipeline_checkpoint WHERE job_id IN ('+params+') ORDER BY checkpoint_id', ids)
    require(all(r['phase'] in ('x_preflight','x_media_initialized') for r in central_cp), 'central_later_checkpoint')
    failures = rows(c, "SELECT * FROM pipeline_event WHERE job_id=? AND event_type='failed' ORDER BY id", (job['id'],))
    require(failures and json.loads(failures[-1]['details_json']).get('unknown') is False
            and json.loads(failures[-1]['details_json']).get('code') == job['error_code'], 'known_failure_fact_missing')
    all_failures = rows(c, "SELECT * FROM pipeline_event WHERE job_id IN ("+params+") AND event_type='failed' ORDER BY id", ids)
    require(all(json.loads(r['details_json']).get('unknown') is False for r in all_failures), 'historical_unknown_requires_separate_reconciliation')
    claims = rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation', (key,))
    history = rows(c, 'SELECT * FROM pipeline_claim_history WHERE job_id IN ('+params+') ORDER BY job_id,generation', ids)
    history_by_key = {(r['job_id'],r['generation']):r for r in history}
    bystage = {j['stage']:j for j in jobs}
    require((job['id'],job['generation']) in history_by_key, 'claim_history_missing')
    for r in claims:
        require(r['stage'] in bystage, 'claim_stage_missing')
        old = history_by_key.get((bystage[r['stage']]['id'],r['generation']))
        require(old and old['claim_hash'] == r['claim_hash'] and r['queue_id'] == queue_id
                and r['log_id'] in (0,log['id']), 'claim_identity_conflict')
    media = media_facts(n, queue_id)
    artifact = json.loads(job['artifact_json'])
    if stage == 'publish':
        prior = [json.loads(r['descriptor_json']) for r in claims if r['stage']=='publish']
        require(prior, 'prior_publish_descriptor_missing')
        desc = prior[-1]
        # The canonical claim freezes the actual final render. The central
        # projection can still contain its pre-render input artifact.
        artifact = desc.get('artifact', {})
        require(desc.get('kind') == 'x_publish' and artifact.get('sha256')
                and desc.get('request',{}).get('body') == log['post_text']
                and desc.get('account_resource') == 'x:account:'+str(q['account_id']), 'frozen_publish_descriptor_missing')
        require(all(log[k] for k in ('long_url','short_url','post_text')), 'unfrozen_post_body')
        require(prior and all(p.get('artifact') == artifact and p.get('request') == desc['request'] for p in prior), 'prior_publish_descriptor_changed')
        if q.get('random_template_enabled'):
            prepared = media['x_post_random_template_preparation']
            require(len(prepared)==1 and prepared[0]['status']=='ready', 'frozen_render_missing')
            request, result = json.loads(prepared[0]['request_json']), json.loads(prepared[0]['result_json'])
            from features.x_posts.random_template import validate_receipt
            validate_receipt(request, result)
            expected = {'url':result['output_url'],'sha256':result['output_sha256'],'size':result['output_size'],
                        'mime':'video/mp4','duration_seconds':result['probe']['duration'],'profile':result['profile'],'recipe':result['recipe']}
            require(artifact==expected, 'frozen_render_changed')
        elif media['pipeline_x_artifact']:
            require(len(media['pipeline_x_artifact'])==1 and json.loads(media['pipeline_x_artifact'][0]['artifact_json'])==artifact, 'frozen_artifact_changed')
        else:
            require(artifact.get('url')==q['material_url'] and artifact.get('sha256')==q['preflight_sha256']
                    and artifact.get('size')==q['preflight_size'], 'frozen_artifact_missing')
    parent = []
    for field, table in [('manual_run_id','x_post_manual_run'),('schedule_run_id','x_post_schedule_run'),('run_id','x_post_daily_run'),('catchup_run_id','x_post_catchup_run')]:
        if q.get(field):
            found = rows(n,'SELECT * FROM '+table+' WHERE id=?',(q[field],))
            require(len(found)==1 and found[0]['status'] not in ('cancelled','canceled','stopped','needs_review'), 'parent_not_recoverable')
            parent.append({k:v for k,v in found[0].items() if k not in ('status','updated_at','published_count','failed_count','unknown_count','finished_at','error_code','error_message')})
    require(len(parent)==1, 'parent_not_unique')
    frozen = {'queue':q, 'log':log, 'jobs':[stable_job(j) for j in jobs], 'native_ops':native_ops,
              'central_ops':central_ops, 'checkpoints':checkpoints, 'central_checkpoints':central_cp,
              'claims':claims, 'history':history, 'parent':parent, 'failures':all_failures,'media':media}
    return {'queue_id':queue_id,'source_key':key,'job_id':job['id'],'stage':stage,'from_generation':job['generation'],
            'to_generation':job['generation']+1,'prior_attempts':log['attempt_count'],'node_id':job['node_id'],
            'proof_sha256':digest(frozen),'queue_sha256':queue_hash(q),'log_sha256':log_hash(log),
            'artifact_sha256':digest(artifact),'scope_job_ids':ids,'prior_log':log,
            'prior_job':stable_job(job),'media_facts':media,'operations_count':len(native_ops),'authorized_day':(original+dt.timedelta(hours=8)).date().isoformat()}


def make_plan(central, native, queue_ids, stage, days, now=None):
    now = now or utcnow()
    require(stage in ('prepare','publish') and 0 < len(queue_ids) <= 200 and len(set(queue_ids))==len(queue_ids), 'scope_invalid')
    require(days and all(dt.date.fromisoformat(d).isoformat()==d for d in days), 'dates_required')
    with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n:
        c.execute('BEGIN'); n.execute('BEGIN')
        items = [snapshot(c,n,q,stage,days,now) for q in queue_ids]
        nodes = [r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]
    require(nodes, 'node_inventory_missing')
    p={'kind':'x_known_prepost_recovery','version':1,'central':str(Path(central).resolve()),'native':str(Path(native).resolve()),
       'days':days,'stage':stage,'items':items,'nodes':nodes,'captured_at':now.isoformat()}
    p['plan_sha256']=document_hash(p)
    return p


def node_evidence(plan, node_id, outbox_path, now=None):
    now=now or utcnow(); require(node_id in plan['nodes'],'node_not_in_plan')
    ids=sorted({i for item in plan['items'] for i in item['scope_job_ids']})
    pending=[];success=[];failed=[];unknown=[]
    with contextlib.closing(read_connection(outbox_path)) as c:
        for row in c.execute('SELECT id,route,payload,acked_at FROM receipt_outbox'):
            p=json.loads(row['payload'])
            if p.get('job_id') not in ids:continue
            evidence={'job_id':p['job_id'],'generation':p.get('generation'),'receipt_sha256':digest(p),'route':row['route']}
            if row['acked_at'] is None:pending.append(evidence)
            if p.get('unknown_outcome') is True:unknown.append(evidence)
            if p.get('phase') in ('x_media_uploaded','x_source_published','x_target_reposted') or (p.get('result') or {}).get('post_id'):
                success.append(evidence)
            if row['route']=='jobs/fail' and p.get('unknown_outcome') is False and row['acked_at'] is not None:
                failed.append(dict(evidence,error_code=p.get('error_code')))
    return {'node_id':node_id,'plan_sha256':plan['plan_sha256'],'captured_at':now.isoformat(),'scope_job_ids':ids,
            'pending_receipts':pending,'later_or_success_receipts':success,'known_failed_receipts':failed,'unknown_receipts':unknown}


def validate_nodes(plan, evidence, now):
    require(sorted(x['node_id'] for x in evidence)==plan['nodes'],'node_evidence_scope')
    ids=sorted({i for item in plan['items'] for i in item['scope_job_ids']})
    for e in evidence:
        require(e['plan_sha256']==plan['plan_sha256'] and e['scope_job_ids']==ids
                and 0 <= (now-date(e['captured_at'])).total_seconds() <= 120, 'node_evidence_stale_or_changed')
        require(not e['pending_receipts'] and not e['later_or_success_receipts'] and not e['unknown_receipts'],'pending_unknown_or_success_receipt')
    bynode={e['node_id']:e for e in evidence}
    for item in plan['items']:
        require(any(r['job_id']==item['job_id'] and r['generation']==item['from_generation'] and r['error_code']==item['prior_job']['error_code']
                for r in bynode[item['node_id']]['known_failed_receipts']), 'terminal_failure_receipt_not_acked')


def backup(path, directory, label):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/(label+'-'+uuid.uuid4().hex+'.sqlite3')
    with os.fdopen(os.open(str(target),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb'):pass
    with contextlib.closing(read_connection(path)) as src,contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst); require(dst.execute('PRAGMA quick_check').fetchall()==[('ok',)],'backup_integrity_failed')
    return str(target)


def writable(path):
    c=sqlite3.connect(str(path),timeout=10,isolation_level=None);c.row_factory=sqlite3.Row
    c.execute('PRAGMA synchronous=FULL');return c


def apply_plan(plan, evidence, backup_dir, actor, *, now=None, stop_after=None):
    now=now or utcnow()
    require(plan['kind']=='x_known_prepost_recovery' and plan['version']==1 and document_hash(plan)==plan['plan_sha256'], 'plan_hash_changed')
    require(bool(str(actor).strip()),'actor_required');validate_nodes(plan,evidence,now)
    backups=[backup(plan['central'],backup_dir,'dispatch'),backup(plan['native'],backup_dir,'native')]
    applied=[]
    with contextlib.closing(writable(plan['central'])) as c, contextlib.closing(writable(plan['native'])) as n:
        c.execute(JOURNAL_SCHEMA);n.execute(SCHEMA)
        for item in plan['items']:
            key=(item['source_key'],item['stage'],item['from_generation'])
            journal=c.execute('SELECT * FROM '+JOURNAL+' WHERE source_key=? AND stage=? AND from_generation=?',key).fetchone()
            require(not journal or journal['plan_sha256']==plan['plan_sha256'],'different_recovery_already_recorded')
            if journal and journal['state']=='committed':
                applied.append({'queue_id':item['queue_id'],'already_applied':True});continue
            if not journal:
                c.execute('BEGIN IMMEDIATE');n.execute('BEGIN')
                try:
                    fresh=snapshot(c,n,item['queue_id'],item['stage'],plan['days'],now)
                    require(fresh==item,'plan_state_drift')
                    c.execute('INSERT INTO '+JOURNAL+' VALUES(?,?,?,?,?,?,?,?)',(*key,plan['plan_sha256'],'staged',encoded(item),now.isoformat(),now.isoformat()))
                    c.execute("UPDATE pipeline_job SET hold_reason='operator_prepost_recovery_pending' WHERE id=? AND status='failed' AND generation=?",(item['job_id'],item['from_generation']))
                    c.commit();n.rollback()
                except BaseException:c.rollback();n.rollback();raise
            if stop_after=='central_staged':raise RecoveryRejected('injected_stop_after_stage')
            n.execute('BEGIN IMMEDIATE')
            try:
                require(media_facts(n,item['queue_id'])==item['media_facts'],'native_media_drift')
                auth=n.execute('SELECT * FROM '+TABLE+' WHERE source_key=? AND stage=? AND from_generation=?',key).fetchone()
                if not auth:
                    q=dict(n.execute('SELECT * FROM x_post_queue WHERE id=?',(item['queue_id'],)).fetchone())
                    log=dict(n.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(item['queue_id'],)).fetchone())
                    require(queue_hash(q)==item['queue_sha256'] and log==item['prior_log'] and q['status']=='failed','native_state_drift')
                    require(not any(not no_post_operation(o[0]) for o in n.execute('SELECT operation FROM pipeline_x_operation WHERE source_key=?',(item['source_key'],))), 'post_intent_appeared')
                    n.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (*key,item['to_generation'],item['prior_attempts'],item['queue_sha256'],item['log_sha256'],item['artifact_sha256'],
                         plan['plan_sha256'],'armed',str(actor),now.isoformat(),'',encoded(item)))
                    n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",(now.isoformat(),item['queue_id']))
                    n.execute("UPDATE x_post_publish_log SET status='reserved',updated_at=? WHERE queue_id=?",(now.isoformat(),item['queue_id']))
                    from features.x_posts import service as posts
                    store=posts.XPostStore.__new__(posts.XPostStore)
                    store._sync_run(n,item['queue_id'],now.isoformat())
                    if q.get('manual_run_id'):
                        # A zero-upload-attempt preparation was already
                        # planned. Its auto parent remains a running plan,
                        # while _sync_run otherwise labels it unstarted.
                        n.execute("UPDATE x_post_manual_run SET status='running',updated_at=? WHERE id=? AND trigger_source='auto_template' AND status='queued'",
                                  (now.isoformat(),q['manual_run_id']))
                else:
                    require(auth['plan_sha256']==plan['plan_sha256'] and auth['state']=='armed','native_recovery_drift')
                    q=dict(n.execute('SELECT * FROM x_post_queue WHERE id=?',(item['queue_id'],)).fetchone())
                    log=dict(n.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(item['queue_id'],)).fetchone())
                    expected_log=dict(item['prior_log'],status='reserved',updated_at=log['updated_at'])
                    require(q['status']=='queued' and queue_hash(q)==item['queue_sha256'] and log==expected_log,'armed_native_state_drift')
                n.commit()
            except BaseException:n.rollback();raise
            if stop_after=='native_armed':raise RecoveryRejected('injected_stop_after_native')
            c.execute('BEGIN IMMEDIATE')
            try:
                current=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(item['job_id'],)).fetchone())
                previous=item['prior_job']; expected=dict(previous,hold_reason='operator_prepost_recovery_pending')
                require(stable_job(current)==expected,'central_state_drift')
                meta=json.loads(current['source_meta_json']);meta.update(reconcile_only=False,legacy_inflight=False)
                c.execute("UPDATE pipeline_job SET status=?,hold_reason='',external_intent=0,started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',source_meta_json=?,updated_at=? WHERE id=?",
                    ('ready' if item['stage']=='publish' else 'queued',encoded(meta),now.isoformat(),item['job_id']))
                c.execute("INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",(item['job_id'],'audited_prepost_recovery',encoded({'plan_sha256':plan['plan_sha256'],'actor':actor,'from_generation':item['from_generation'],'prior_attempts':item['prior_attempts'],'proof_sha256':item['proof_sha256']}),now.isoformat()))
                c.execute("UPDATE "+JOURNAL+" SET state='committed',updated_at=? WHERE source_key=? AND stage=? AND from_generation=?",(now.isoformat(),*key));c.commit()
            except BaseException:c.rollback();raise
            applied.append({'queue_id':item['queue_id'],'stage':item['stage'],'next_generation':item['to_generation'],'preserved_attempt_count':item['prior_attempts']})
    return {'applied':applied,'backups':backups,'plan_sha256':plan['plan_sha256'],'platform_called':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','node-evidence','apply'])
    p.add_argument('--central');p.add_argument('--native');p.add_argument('--queue-ids',nargs='+',type=int)
    p.add_argument('--stage',choices=['prepare','publish'],default='publish');p.add_argument('--days',nargs='+')
    p.add_argument('--plan');p.add_argument('--node-id');p.add_argument('--outbox');p.add_argument('--node-evidence',nargs='+')
    p.add_argument('--backup-dir');p.add_argument('--actor');p.add_argument('--output',required=True);a=p.parse_args()
    # Reserve the evidence destination before any mutation. Never overwrite
    # another operator's plan or discover an unwritable output after apply.
    with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as f:
        if a.action=='plan':result=make_plan(a.central,a.native,a.queue_ids,a.stage,a.days)
        else:
            plan=json.loads(Path(a.plan).read_text(encoding='utf-8'))
            if a.action=='node-evidence':result=node_evidence(plan,a.node_id,a.outbox)
            else:result=apply_plan(plan,[json.loads(Path(x).read_text(encoding='utf-8')) for x in a.node_evidence],a.backup_dir,a.actor)
        f.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n');f.flush();os.fsync(f.fileno())
    print(json.dumps({'ok':True,'action':a.action,'plan_sha256':result['plan_sha256']}))


if __name__=='__main__':
    try:main()
    except RecoveryRejected as e:print(json.dumps({'ok':False,'code':str(e)}));raise SystemExit(2)

"""Audit and resume explicitly selected X uploads with no Post operation.

This is an operator command, not an automatic unknown retry policy. It requires
matching coordinator/native ledgers and acknowledged terminal receipts from all
registered nodes. Full prior facts remain in both journals. Only one following
generation of each original queue is authorized; runtime permission, artifact,
identity, lease and dedupe checks remain responsible for execution.
"""
import argparse
import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import (
    TABLE as AUTH, SCHEMA as AUTH_SCHEMA, digest, encoded, queue_hash, log_hash, no_post_operation,
)

AUDIT = 'pipeline_x_media_interrupted_recovery'
SCHEMA = '''CREATE TABLE IF NOT EXISTS pipeline_x_media_interrupted_recovery (
 job_id INTEGER NOT NULL,generation INTEGER NOT NULL,plan_sha256 TEXT NOT NULL,
 state TEXT NOT NULL,proof_json TEXT NOT NULL,actor TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(job_id,generation))'''
HOLD = 'operator_verified_media_only_recovery'


def require(value, code):
    if not value: raise ValueError(code)


def connect(path, readonly=True):
    c = sqlite3.connect('file:'+str(path)+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=30)
    c.row_factory=sqlite3.Row
    return c


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql,args)]


def one(c,sql,args=()):
    result=rows(c,sql,args);require(len(result)==1,'unique_row_required');return result[0]


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()


def exists(c,table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()


def collect(c,n,job_id):
    job=one(c,'SELECT * FROM pipeline_job WHERE id=?',(job_id,))
    key=job['source_key']; qid=int(job['source_id'])
    p={'job':job,'queue':one(n,'SELECT * FROM x_post_queue WHERE id=?',(qid,)),
       'log':one(n,'SELECT * FROM x_post_publish_log WHERE queue_id=?',(qid,)),
       'prepare':one(c,"SELECT * FROM pipeline_job WHERE source_key=? AND stage='prepare'",(key,))}
    for name,table,order in [('operations','pipeline_operation','operation_id'),('checkpoints','pipeline_checkpoint','checkpoint_id'),
                             ('deliveries','pipeline_delivery','delivery_id'),('receipts','pipeline_receipt','receipt_id'),
                             ('history','pipeline_claim_history','generation'),('events','pipeline_event','id'),
                             ('resources','pipeline_resource','resource_key'),('legacy_resources','pipeline_legacy_resource','resource_key')]:
        p[name]=rows(c,'SELECT * FROM '+table+' WHERE job_id=? ORDER BY '+order,(job_id,))
    p['publications']=rows(c,'SELECT * FROM pipeline_publication WHERE source_key=?',(key,))
    for name,table,order in [('native_operations','pipeline_x_operation','operation_id'),('native_checkpoints','pipeline_x_checkpoint','generation,phase'),
                             ('native_claims','pipeline_x_claim','stage,generation'),('holds','pipeline_canonical_hold','source_key')]:
        p[name]=rows(n,'SELECT * FROM '+table+' WHERE source_key=? ORDER BY '+order,(key,))
    p['reposts']=rows(n,'SELECT * FROM x_post_repost_ledger WHERE queue_id=?',(qid,))
    p['media']={table:rows(n,'SELECT * FROM '+table+' WHERE queue_id=?',(qid,))
                for table in ('pipeline_x_artifact','x_post_random_template_preparation')}
    return p


def stable(p):
    p=copy.deepcopy(p)
    for name in ('job','prepare'):
        for key in ('updated_at','source_status','source_meta_json'):p[name].pop(key,None)
    return p


def validate(p):
    j,q,l=p['job'],p['queue'],p['log'];g=j['generation']
    require(j['channel']=='x' and j['stage']=='publish' and j['status']=='unknown' and g>=1
            and not j['hold_reason'] and not j['canonical_receipt'],'not_terminal_unknown')
    require(p['prepare']['status']=='completed','preparation_not_complete')
    require(q['status']=='failed' and q['delivery_mode']=='direct' and l['status']=='failed'
            and l['unknown_outcome']==1 and l['attempt_count']>=1
            and not any(l[k] for k in ('x_media_id','x_post_id','published_at')),'native_unknown_changed')
    require(json.loads(j['source_meta_json'])['canonical_fingerprint']==canonical_fingerprint(q,'id'), 'canonical_identity_changed')
    require(not any(p[k] for k in ('publications','reposts','holds','legacy_resources')),'other_hold_or_publication')
    require(all(r['quarantined']==1 and r['generation']==g and r['legacy']==0 for r in p['resources']),'active_resource')
    require(p['operations'] and p['native_operations'] and all(no_post_operation(r['operation']) for r in p['operations']+p['native_operations']),
            'post_or_finalize_intent')
    fields=('operation_id','generation','operation','request_sha256')
    # Every native permit must be present centrally. A central permit can precede
    # a failed native RPC, but neither ledger may contain a Post/FINALIZE permit.
    require({tuple(r[k] for k in fields) for r in p['native_operations']}.issubset(
            {tuple(r[k] for k in fields) for r in p['operations']}),'operation_ledgers_disagree')
    require(all(r['phase'] in ('x_preflight','x_media_initialized') for r in p['checkpoints']+p['native_checkpoints']), 'later_checkpoint')
    claims=[r for r in p['native_claims'] if r['stage']=='publish' and r['generation']==g]
    require(len(claims)==1 and claims[0]['claim_hash']==j['claim_hash'],'claim_identity_changed')
    desc=json.loads(claims[0]['descriptor_json']);artifact=desc['artifact']
    require(desc['kind']=='x_publish' and desc['request']['body']==l['post_text']
            and desc['account_resource']=='x:account:'+str(q['account_id'])
            and artifact.get('sha256') and artifact.get('size')
            and all(l[k] for k in ('long_url','short_url','post_text')),'frozen_publication_changed')
    if q['random_template_enabled']:
        prepared=p['media']['x_post_random_template_preparation']
        require(len(prepared)==1 and prepared[0]['status']=='ready','render_not_ready')
        request,result=json.loads(prepared[0]['request_json']),json.loads(prepared[0]['result_json'])
        from features.x_posts.random_template import validate_receipt
        validate_receipt(request,result)
        require(artifact=={'url':result['output_url'],'sha256':result['output_sha256'],'size':result['output_size'],
                'mime':'video/mp4','duration_seconds':result['probe']['duration'],'profile':result['profile'],'recipe':result['recipe']},'render_changed')
    elif p['media']['pipeline_x_artifact']:
        require(len(p['media']['pipeline_x_artifact'])==1 and json.loads(p['media']['pipeline_x_artifact'][0]['artifact_json'])==artifact,'artifact_changed')
    else:
        require((artifact.get('url'),artifact.get('sha256'),artifact.get('size'))==(q['material_url'],q['preflight_sha256'],q['preflight_size']),'artifact_missing')
    return artifact


def validate_nodes(plan, evidence):
    require(sorted(e['node_id'] for e in evidence)==plan['nodes'],'node_inventory_incomplete')
    for item in plan['items']:
        p=item['proof'];j=p['job']; terminal=[]
        deliveries={r['delivery_id']:r for r in p['deliveries']}
        for e in evidence:
            for r in e['receipts']:
                x=r['payload']
                if x['job_id']!=j['id']:continue
                require(r['acked_at'] is not None and x['generation']==j['generation']
                        and x['claim_token_sha256']==j['claim_hash'],'pending_or_conflicting_receipt')
                require(r['route'] in ('jobs/fail','jobs/checkpoint'),'later_success_receipt')
                d=deliveries.get(r['id'])
                require(d and d['request_sha256']==r['payload_sha256'],'receipt_not_acknowledged_centrally')
                if r['route']=='jobs/fail':
                    require(e['node_id']==j['node_id'] and x['worker_id']==j['worker_id'] and x.get('unknown_outcome') is True
                            and x.get('error_code')==j['error_code'] and json.loads(d['response_json'])=={'accepted':True,'status':'unknown'},'terminal_receipt_changed')
                    terminal.append(r)
                else:
                    require(x.get('phase') in ('x_preflight','x_media_initialized'),'later_node_checkpoint')
        require(len(terminal)==1,'terminal_receipt_missing_or_ambiguous')


def make_plan(central,natives,job_ids,evidence):
    with contextlib.closing(connect(central)) as c:
        c.execute('BEGIN')
        plan={'central':central,'nodes':[r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')],
              'items':[],'captured_at':now(),'kind':'explicit_x_media_only_recovery'}
        require(job_ids and len(set(job_ids))==len(job_ids),'scope_required')
        for job_id in job_ids:
            j=one(c,'SELECT source_id FROM pipeline_job WHERE id=?',(job_id,))
            native=natives['tag' if int(j['source_id'])>=9000000000000 else 'x']
            with contextlib.closing(connect(native)) as n:
                n.execute('BEGIN');p=collect(c,n,job_id);artifact=validate(p)
                require(not (exists(n,AUTH) and n.execute('SELECT 1 FROM '+AUTH+' WHERE source_key=?',(p['job']['source_key'],)).fetchone()),'prior_authorization_exists')
                parent=one(n,'SELECT status FROM x_post_manual_run WHERE id=?',(p['queue']['manual_run_id'],))
                require(parent['status'] not in ('canceled','cancelled','stopped'),'parent_stopped')
            plan['items'].append({'native':native,'proof':p,'artifact_sha256':digest(artifact)})
    validate_nodes(plan,evidence)
    plan['evidence']=evidence;plan['plan_sha256']=digest(plan)
    return plan


def check_plan(plan):
    unsigned=dict(plan);unsigned.pop('plan_sha256',None)
    require(digest(unsigned)==plan['plan_sha256'],'plan_changed')
    validate_nodes(plan,plan['evidence'])


def backup(path,directory,label):
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/(label+'.sqlite3')
    require(not target.exists(),'backup_exists')
    temporary=target.with_suffix('.sqlite3.tmp')
    with contextlib.closing(connect(path)) as c,contextlib.closing(sqlite3.connect(temporary)) as dest:
        c.backup(dest)
        require(dest.execute('PRAGMA quick_check').fetchone()[0]=='ok','backup_integrity_failed')
    temporary.chmod(0o600);temporary.replace(target)
    return str(target)


def check_current(saved,current,armed=False,held=False):
    current=copy.deepcopy(current)
    if held:
        require(current['job']['hold_reason']==HOLD,'recovery_hold_changed')
        current['job']['hold_reason']=saved['job']['hold_reason']
    if armed:
        require(current['queue']['status']=='queued' and current['log']['status']=='reserved'
                and current['log']['unknown_outcome']==0,'armed_state_changed')
        for k in ('status','updated_at'): current['queue'][k]=saved['queue'][k]
        for k in ('status','unknown_outcome','updated_at'):current['log'][k]=saved['log'][k]
    require(stable(current)==stable(saved),'execution_facts_changed')


def apply(plan,backup_dir,actor,stop_after=None):
    check_plan(plan);require(actor.strip(),'actor_required');timestamp=now()
    # One backup per owning DB before any mutation. On resume, original backups
    # are retained; never copy them over live history or later receipts.
    for i,path in enumerate(sorted({plan['central']}|{x['native'] for x in plan['items']})):
        target=Path(backup_dir)/(str(i)+'.sqlite3')
        if not target.exists():backup(path,Path(backup_dir),str(i))
    completed=[]
    for item in plan['items']:
        p=item['proof'];j,q,l=p['job'],p['queue'],p['log'];key=j['source_key'];g=j['generation']
        with contextlib.closing(connect(plan['central'],False)) as c,contextlib.closing(connect(item['native'],False)) as n:
            c.execute('BEGIN IMMEDIATE');c.execute(SCHEMA)
            audit=rows(c,'SELECT * FROM '+AUDIT+' WHERE job_id=? AND generation=?',(j['id'],g))
            if audit:
                require(audit[0]['plan_sha256']==plan['plan_sha256'],'different_recovery_plan')
                if audit[0]['state']=='committed':c.rollback();completed.append(j['id']);continue
            else:
                n.execute('BEGIN');check_current(p,collect(c,n,j['id']));n.rollback()
                c.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?,?,?,?)',(j['id'],g,plan['plan_sha256'],'staged',encoded(item),actor,timestamp))
                c.execute('UPDATE pipeline_job SET hold_reason=? WHERE id=?',(HOLD,j['id']))
            c.commit()
            if stop_after=='staged':raise RuntimeError('injected_after_stage')
            c.execute('BEGIN IMMEDIATE');n.execute('BEGIN IMMEDIATE');n.execute(AUTH_SCHEMA);n.execute(SCHEMA)
            auth=rows(n,'SELECT * FROM '+AUTH+' WHERE source_key=? AND stage=? AND from_generation=?',(key,'publish',g))
            check_current(p,collect(c,n,j['id']),armed=bool(auth),held=True)
            if auth:
                require(auth[0]['plan_sha256']==plan['plan_sha256'] and auth[0]['state']=='armed','authorization_changed')
            else:
                n.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?,?,?,?)',(j['id'],g,plan['plan_sha256'],'armed',encoded(item),actor,timestamp))
                n.execute('INSERT INTO '+AUTH+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key,'publish',g,g+1,l['attempt_count'],queue_hash(q),log_hash(l),item['artifact_sha256'],plan['plan_sha256'],
                     'armed',actor,timestamp,'',encoded({'reconciliation':AUDIT,'original_proof':p})))
                n.execute("UPDATE x_post_publish_log SET status='reserved',unknown_outcome=0,updated_at=? WHERE id=?",(timestamp,l['id']))
                n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",(timestamp,q['id']))
                from features.x_posts.service import XPostStore
                XPostStore._sync_run(n,q['id'],timestamp)
            n.commit();c.rollback()
            if stop_after=='armed':raise RuntimeError('injected_after_arm')
            c.execute('BEGIN IMMEDIATE');n.execute('BEGIN IMMEDIATE')
            check_current(p,collect(c,n,j['id']),armed=True,held=True)
            from features.pipeline_dispatch.x_prepost_recovery import authorization
            require(authorization(n,one(n,'SELECT * FROM x_post_queue WHERE id=?',(q['id'],)),
                                  one(n,'SELECT * FROM x_post_publish_log WHERE id=?',(l['id'],))), 'runtime_authorization_invalid')
            meta=json.loads(one(c,'SELECT source_meta_json FROM pipeline_job WHERE id=?',(j['id'],))['source_meta_json'])
            meta.update(reconcile_only=False,legacy_inflight=False,unknown_outcome=False)
            c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',external_intent=0,started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',source_meta_json=?,updated_at=? WHERE id=?",(encoded(meta),timestamp,j['id']))
            # Only quarantined resources of this audited, terminal execution;
            # the previous full rows remain in both recovery journals.
            c.execute('DELETE FROM pipeline_resource WHERE job_id=? AND generation=? AND quarantined=1 AND legacy=0',(j['id'],g))
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                (j['id'],'audited_media_only_recovery',encoded({'plan_sha256':plan['plan_sha256'],'from_generation':g,'to_generation':g+1,
                 'prior_attempt_count':l['attempt_count'],'prior_unknown':True,'actor':actor}),timestamp))
            c.execute("UPDATE "+AUDIT+" SET state='committed',updated_at=? WHERE job_id=? AND generation=?",(timestamp,j['id'],g))
            c.commit();n.rollback();completed.append(j['id'])
    return {'restored_job_ids':completed,'platform_called':False,'plan_sha256':plan['plan_sha256']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('plan','apply'));p.add_argument('--scope');p.add_argument('--plan');p.add_argument('--evidence',nargs='+')
    p.add_argument('--backup-dir');p.add_argument('--actor');p.add_argument('--output',required=True)
    a=p.parse_args()
    if a.action=='plan':
        scope=json.loads(Path(a.scope).read_text());e=[json.loads(Path(f).read_text(encoding='utf-8-sig')) for f in a.evidence]
        result=make_plan(scope['central'],scope['natives'],scope['job_ids'],e)
    else:result=apply(json.loads(Path(a.plan).read_text()),a.backup_dir,a.actor)
    with os.fdopen(os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'ok':True,'action':a.action,'count':len(result.get('items',result.get('restored_job_ids',[])))}))


if __name__=='__main__':main()

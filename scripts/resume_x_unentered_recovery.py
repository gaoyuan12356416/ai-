"""Audited continuation after an armed recovery never entered its native bridge.

Only explicit failed publish jobs, with zero current-generation native claims,
operations, checkpoints or receipts, can receive one new generation. Prior
permissions and failures remain in journals. No network or platform calls.
"""
import argparse
import contextlib
import copy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import recover_x_media_interrupted as original
from features.pipeline_dispatch.x_prepost_recovery import TABLE,authorization,digest,encoded

AUDIT='pipeline_x_unentered_recovery'
SCHEMA='''CREATE TABLE IF NOT EXISTS pipeline_x_unentered_recovery (
 job_id INTEGER NOT NULL,generation INTEGER NOT NULL,plan_sha256 TEXT NOT NULL,
 state TEXT NOT NULL,proof_json TEXT NOT NULL,actor TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(job_id,generation))'''
HOLD='operator_verified_unentered_recovery'
ACK='pipeline_x_unentered_failure_ack'
ACK_SCHEMA='''CREATE TABLE IF NOT EXISTS pipeline_x_unentered_failure_ack (
 source_key TEXT NOT NULL,generation INTEGER NOT NULL,claim_hash TEXT NOT NULL,
 error_code TEXT NOT NULL,proof_json TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(source_key,generation))'''
require=original.require


def collect(c,n,jid):
    p=original.collect(c,n,jid)
    p['authorizations']=original.rows(n,'SELECT * FROM '+TABLE+' WHERE source_key=? ORDER BY from_generation',(p['job']['source_key'],))
    return p


def validate(p):
    j,q,l=p['job'],p['queue'],p['log'];g=j['generation']
    require(j['channel']=='x' and j['stage']=='publish' and j['status']=='failed' and g>=2
            and j['error_code']=='x_tag_dispatch_failed' and not j['external_intent']
            and not j['hold_reason'] and not j['canonical_receipt'],'not_unentered_failure')
    require(p['prepare']['status']=='completed','prepare_changed')
    require(q['status']=='queued' and l['status']=='reserved' and not l['unknown_outcome']
            and not any(l[k] for k in ('x_media_id','x_post_id','published_at')),'native_changed')
    require(not any(p[k] for k in ('publications','reposts','holds','resources','legacy_resources')),'active_or_fenced')
    for name in ('operations','checkpoints','receipts','native_operations','native_checkpoints'):
        require(not any(r['generation']>=g for r in p[name]),'current_generation_'+name)
    for d in p['deliveries']:
        if d['generation']>=g:
            response=json.loads(d['response_json'])
            require(d['generation']==g and d['claim_hash']==j['claim_hash'] and response.get('accepted') is True
                    and response.get('status')=='failed','non_failure_delivery')
    require(not any(r['stage']=='publish' and r['generation']>=g for r in p['native_claims']),'native_claim_exists')
    auth=[a for a in p['authorizations'] if a['state']=='armed']
    require(len(auth)==1 and auth[0]['to_generation']==g and auth[0]['prior_attempts']==l['attempt_count'],'permission_changed')
    require(all(original.no_post_operation(r['operation']) for r in p['operations']+p['native_operations']),'post_intent')
    return auth[0]


def plan(scope):
    out={'central':scope['central'],'items':[],'kind':'explicit_unentered_continuation','utc':original.now()}
    with contextlib.closing(original.connect(scope['central'])) as c:
        for jid in scope['job_ids']:
            with contextlib.closing(original.connect(scope['natives']['tag'])) as n:
                p=collect(c,n,jid);validate(p)
                require(authorization(n,p['queue'],p['log'],p['job']),'runtime_permission_invalid')
                out['items'].append({'native':scope['natives']['tag'],'proof':p})
    out['plan_sha256']=digest(out);return out


def stage_receipt_ack(p,actor,backup_dir):
    unsigned=dict(p);unsigned.pop('plan_sha256');require(digest(unsigned)==p['plan_sha256'],'plan_changed')
    require(actor.strip(),'actor_required')
    for i,path in enumerate(sorted({x['native'] for x in p['items']})):
        if not (Path(backup_dir)/(str(i)+'.sqlite3')).exists():original.backup(path,Path(backup_dir),str(i))
    for item in p['items']:
        saved=item['proof'];j=saved['job'];validate(saved)
        with contextlib.closing(original.connect(p['central'])) as c,contextlib.closing(original.connect(item['native'],False)) as n:
            c.execute('BEGIN');n.execute('BEGIN IMMEDIATE')
            require(original.stable(collect(c,n,j['id']))==original.stable(saved),'execution_facts_changed')
            n.execute(ACK_SCHEMA)
            old=n.execute('SELECT * FROM '+ACK+' WHERE source_key=? AND generation=?',(j['source_key'],j['generation'])).fetchone()
            if old:
                require(old['claim_hash']==j['claim_hash'] and old['error_code']==j['error_code'],'ack_authorization_changed')
            else:
                n.execute('INSERT INTO '+ACK+' VALUES(?,?,?,?,?,?)',(j['source_key'],j['generation'],j['claim_hash'],j['error_code'],encoded({'actor':actor,'proof':saved,'plan_sha256':p['plan_sha256']}),original.now()))
            n.commit();c.rollback()
    return {'receipt_ack_authorized_job_ids':[i['proof']['job']['id'] for i in p['items']],'platform_called':False}


def matches(saved,current,held=False,armed=False):
    current=copy.deepcopy(current)
    if held:
        require(current['job']['hold_reason']==HOLD,'hold_changed')
        current['job']['hold_reason']=saved['job']['hold_reason']
    if armed:
        old=validate(saved)
        prior=[r for r in current['authorizations'] if r['from_generation']==old['from_generation']]
        new=[r for r in current['authorizations'] if r['from_generation']==saved['job']['generation']]
        require(len(prior)==len(new)==1 and prior[0]['state']=='superseded_unentered' and new[0]['state']=='armed','arm_changed')
        require(new[0]['to_generation']==saved['job']['generation']+1 and new[0]['artifact_sha256']==old['artifact_sha256']
                and new[0]['prior_attempts']==old['prior_attempts'],'new_permission_changed')
        current['authorizations']=saved['authorizations']
    require(original.stable(current)==original.stable(saved),'execution_facts_changed')


def apply(p,folder,actor,stop_after=None):
    unsigned=dict(p);unsigned.pop('plan_sha256');require(digest(unsigned)==p['plan_sha256'],'plan_changed')
    require(actor.strip(),'actor_required');folder=Path(folder)
    for item in p['items']:
        j=item['proof']['job']
        require(len([d for d in item['proof']['deliveries'] if d['generation']==j['generation']])==1,'current_failure_receipt_not_acknowledged')
    for i,path in enumerate(sorted({p['central']}|{x['native'] for x in p['items']})):
        if not (folder/(str(i)+'.sqlite3')).exists():original.backup(path,folder,str(i))
    done=[]
    for item in p['items']:
        saved=item['proof'];j=saved['job'];g=j['generation'];old=validate(saved);timestamp=original.now()
        with contextlib.closing(original.connect(p['central'],False)) as c,contextlib.closing(original.connect(item['native'],False)) as n:
            c.execute(SCHEMA);c.commit()
            audit=c.execute('SELECT * FROM '+AUDIT+' WHERE job_id=? AND generation=?',(j['id'],g)).fetchone()
            if audit:
                require(audit['plan_sha256']==p['plan_sha256'],'different_plan')
                if audit['state']=='committed':done.append(j['id']);continue
            else:
                c.execute('BEGIN');n.execute('BEGIN');matches(saved,collect(c,n,j['id']));n.rollback();c.rollback()
                c.execute('BEGIN IMMEDIATE');original.check_job(j,original.one(c,'SELECT * FROM pipeline_job WHERE id=?',(j['id'],)))
                c.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?,?,?,?)',(j['id'],g,p['plan_sha256'],'staged',encoded(item),actor,timestamp))
                c.execute('UPDATE pipeline_job SET hold_reason=? WHERE id=?',(HOLD,j['id']));c.commit()
            if stop_after=='staged':raise RuntimeError('injected_staged')
            c.execute('BEGIN');n.execute('BEGIN IMMEDIATE');n.execute(SCHEMA)
            armed=n.execute('SELECT * FROM '+AUDIT+' WHERE job_id=? AND generation=?',(j['id'],g)).fetchone()
            matches(saved,collect(c,n,j['id']),held=True,armed=bool(armed))
            if not armed:
                changed=n.execute('UPDATE '+TABLE+" SET state='superseded_unentered' WHERE source_key=? AND stage='publish' AND from_generation=? AND state='armed'",(j['source_key'],old['from_generation'])).rowcount
                require(changed==1,'old_authorization_changed')
                new=dict(old,from_generation=g,to_generation=g+1,plan_sha256=p['plan_sha256'],actor=actor,created_at=timestamp,proof_json=encoded({'prior_authorization':old,'unentered_generation':g,'proof':saved}))
                n.execute('INSERT INTO '+TABLE+' ('+','.join(new)+') VALUES('+','.join('?' for _ in new)+')',tuple(new.values()))
                n.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?,?,?,?)',(j['id'],g,p['plan_sha256'],'armed',encoded(item),actor,timestamp))
            require(authorization(n,saved['queue'],saved['log'],dict(j,generation=g+1)),'next_generation_permission_invalid')
            n.commit();c.rollback()
            if stop_after=='armed':raise RuntimeError('injected_armed')
            c.execute('BEGIN');n.execute('BEGIN');matches(saved,collect(c,n,j['id']),held=True,armed=True);c.rollback();n.rollback()
            c.execute('BEGIN IMMEDIATE')
            current=original.one(c,'SELECT * FROM pipeline_job WHERE id=?',(j['id'],));require(current['hold_reason']==HOLD,'hold_changed')
            current['hold_reason']=j['hold_reason'];original.check_job(j,current)
            c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',updated_at=? WHERE id=?",(timestamp,j['id']))
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(j['id'],'audited_unentered_continuation',encoded({'from_generation':g,'to_generation':g+1,'plan_sha256':p['plan_sha256'],'prior_attempt_count':old['prior_attempts'],'actor':actor}),timestamp))
            c.execute("UPDATE "+AUDIT+" SET state='committed',updated_at=? WHERE job_id=? AND generation=?",(timestamp,j['id'],g));c.commit();done.append(j['id'])
    return {'continued_job_ids':done,'platform_called':False,'plan_sha256':p['plan_sha256']}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('plan','apply','stage-receipt-ack'));p.add_argument('--scope');p.add_argument('--plan');p.add_argument('--backup-dir');p.add_argument('--actor');p.add_argument('--output',required=True);a=p.parse_args()
    if a.action=='plan': result=plan(json.loads(Path(a.scope).read_text()))
    elif a.action=='stage-receipt-ack': result=stage_receipt_ack(json.loads(Path(a.plan).read_text()),a.actor,a.backup_dir)
    else: result=apply(json.loads(Path(a.plan).read_text()),a.backup_dir,a.actor)
    with open(a.output,'x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    Path(a.output).chmod(0o600);print(json.dumps({'ok':True,'count':len(result.get('items',result.get('continued_job_ids',[])))}))

if __name__=='__main__':main()

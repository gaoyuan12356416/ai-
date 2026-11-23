"""Resume an explicitly selected terminal claim that never entered native publishing.

Keep the original queue, log, attempt count and previous recovery authorizations.
An unused authorization is superseded for exactly one following generation.
No platform, token, account or service operation is performed here.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import recover_x_prepost_failures as base
from features.pipeline_dispatch.x_prepost_recovery import authorization, no_post_operation

TABLE = 'pipeline_x_unentered_recovery'
HOLD = 'operator_unentered_publish_recovery'
DDL = ('CREATE TABLE IF NOT EXISTS '+TABLE+' (job_id INTEGER NOT NULL,generation INTEGER NOT NULL,'
       'plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,proof_json TEXT NOT NULL,actor TEXT NOT NULL,'
       'updated_at TEXT NOT NULL,PRIMARY KEY(job_id,generation))')


def snapshot(c, n, job_id):
    def one(db, sql, args):
        found = base.rows(db, sql, args)
        base.require(len(found) == 1, 'unique_row_required')
        return found[0]
    j = one(c, 'SELECT * FROM pipeline_job WHERE id=?', (job_id,))
    key, gen = j['source_key'], j['generation']
    base.require((j['channel'], j['family'], j['stage'], j['status']) == ('x','queue','publish','failed')
                 and gen > 0 and j['error_code'] in base.KNOWN_CODES
                 and not any(j[k] for k in ('external_intent','hold_reason','canonical_receipt')),
                 'not_unentered_failure')
    q = one(n, 'SELECT * FROM x_post_queue WHERE id=?', (int(j['source_id']),))
    l = one(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (q['id'],))
    base.require(q['status']=='queued' and q['delivery_mode']=='direct' and l['status']=='reserved'
                 and not any(l[k] for k in ('unknown_outcome','x_media_id','x_post_id','published_at')),
                 'native_not_untouched')
    for table in ('pipeline_x_claim','pipeline_x_operation'):
        suffix = " AND stage='publish'" if table.endswith('claim') else ''
        base.require(not n.execute('SELECT 1 FROM '+table+' WHERE source_key=? AND generation>=?'+suffix,
                                    (key,gen)).fetchone(), 'native_current_claim_entered')
    base.require(not c.execute('SELECT 1 FROM pipeline_operation WHERE job_id=? AND generation>=?',
                               (job_id,gen)).fetchone(), 'current_operation_present')
    for table in ('pipeline_resource','pipeline_legacy_resource'):
        base.require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id IN '
                                   '(SELECT id FROM pipeline_job WHERE source_key=?)',(key,)).fetchone(),
                     'resource_held')
    for db,table in ((c,'pipeline_publication'),(n,'pipeline_canonical_hold')):
        base.require(not db.execute('SELECT 1 FROM '+table+' WHERE source_key=?',(key,)).fetchone(),
                     'publication_or_hold')
    base.require(not n.execute('SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?',(q['id'],)).fetchone(),
                 'relay_present')
    jobs = base.rows(c,'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage',(key,))
    base.require(len(jobs)==2 and all(x['status']=='completed' for x in jobs if x['stage']=='prepare'),
                 'preparation_not_complete')
    native_ops = base.rows(n,'SELECT * FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id',(key,))
    central_ops = base.rows(c,'SELECT * FROM pipeline_operation WHERE job_id=? ORDER BY operation_id',(job_id,))
    base.require(all(no_post_operation(x['operation']) for x in native_ops+central_ops),'post_intent_present')
    fields=('operation_id','generation','operation','request_sha256')
    base.require({tuple(x[k] for k in fields) for x in native_ops}.issubset(
                 {tuple(x[k] for k in fields) for x in central_ops}), 'operation_identity_changed')
    checkpoints=base.rows(n,'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase',(key,))
    base.require(all(x['phase'] in ('x_preflight','x_media_initialized') for x in checkpoints),'later_checkpoint')
    auth = authorization(n,q,l,j) if l['attempt_count'] else None
    base.require(bool(auth) if l['attempt_count'] else not native_ops and not central_ops,
                 'unused_authorization_required')
    parent=one(n,'SELECT * FROM x_post_manual_run WHERE id=?',(q['manual_run_id'],))
    base.require(parent['status']=='running' and not parent['unknown_count'] and not parent['error_code'],
                 'parent_not_running')
    history=base.rows(c,'SELECT * FROM pipeline_claim_history WHERE job_id=? ORDER BY generation',(job_id,))
    base.require(history and history[-1]['generation']==gen and history[-1]['claim_hash']==j['claim_hash'],
                 'claim_identity_changed')
    delivery=base.rows(c,'SELECT * FROM pipeline_delivery WHERE job_id=? AND generation=?',(job_id,gen))
    base.require(any(x['claim_hash']==j['claim_hash'] and json.loads(x['response_json'])==
                     {'accepted':True,'status':'failed'} for x in delivery),'terminal_ack_missing')
    return dict(job_id=job_id,source_key=key,queue_id=q['id'],from_generation=gen,to_generation=gen+1,
                scope_job_ids=[x['id'] for x in jobs],node_id=j['node_id'],
                prior_job=base.stable_job(j),queue=q,log=l,authorization=dict(auth) if auth else None,
                history=history,delivery=delivery,native_ops=native_ops,central_ops=central_ops,
                checkpoints=checkpoints)


def check_origin(auto, queue_id):
    with contextlib.closing(base.read_connection(auto)) as c:
        found=base.rows(c,'SELECT r.id,r.status,r.error_code FROM x_auto_task t '
                         'JOIN x_auto_run r ON r.id=t.run_id WHERE t.execution_queue_id=?',(queue_id,))
    base.require(len(found)==1,'auto_origin_missing')
    origin=found[0]
    base.require(origin['status'] not in ('canceled','cancelled','stopped')
                 and origin['error_code']!='operator_terminated','auto_origin_terminated')
    return origin


def make_plan(central,native,job_ids,auto=None):
    with contextlib.closing(base.read_connection(central)) as c, contextlib.closing(base.read_connection(native)) as n:
        c.execute('BEGIN');n.execute('BEGIN')
        items=[snapshot(c,n,j) for j in job_ids]
    p=dict(kind='x_unentered_publish_recovery',central=str(central),native=str(native),items=items,
           nodes=sorted({h['node_id'] for i in items for h in i['history']}),
           media_only_reconciliation=True,captured_at=base.utcnow().isoformat())
    if auto:
        p['auto']=str(auto)
        p['origins']={str(i['queue_id']):check_origin(auto,i['queue_id']) for i in items}
    p['plan_sha256']=base.document_hash(p)
    return p


def apply_plan(plan,evidence,directory,actor):
    base.require(plan['kind']=='x_unentered_publish_recovery' and
                 plan['plan_sha256']==base.document_hash(plan) and actor,'plan_changed')
    base.validate_nodes(plan,evidence,base.utcnow())
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    backup=directory/('unentered-before-'+plan['plan_sha256']+'.json')
    if not backup.exists():
        with os.fdopen(os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f:
            json.dump(plan,f);f.flush();os.fsync(f.fileno())
    result=[]
    with contextlib.closing(base.writable(plan['central'])) as c, contextlib.closing(base.writable(plan['native'])) as n:
        c.execute(DDL)
        for item in plan['items']:
            jid,gen=item['job_id'],item['from_generation'];now=base.utcnow().isoformat()
            if plan.get('auto'):check_origin(plan['auto'],item['queue_id'])
            old=c.execute('SELECT * FROM '+TABLE+' WHERE job_id=? AND generation=?',(jid,gen)).fetchone()
            base.require(not old or old['plan_sha256']==plan['plan_sha256'],'other_recovery')
            if old and old['state']=='committed':
                result.append(jid);continue
            if not old:
                c.execute('BEGIN IMMEDIATE');n.execute('BEGIN')
                try:
                    base.require(snapshot(c,n,jid)==item,'state_changed')
                    c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?)',
                              (jid,gen,plan['plan_sha256'],'staged',base.encoded(item),actor,now))
                    c.execute('UPDATE pipeline_job SET hold_reason=? WHERE id=?',(HOLD,jid))
                    c.commit();n.rollback()
                except BaseException:c.rollback();n.rollback();raise
            auth=item['authorization']
            if auth:
                n.execute('BEGIN IMMEDIATE')
                try:
                    existing=n.execute('SELECT * FROM '+base.TABLE+' WHERE source_key=? AND stage=? AND from_generation=?',
                                        (item['source_key'],'publish',gen)).fetchone()
                    if not existing:
                        current=n.execute('SELECT * FROM '+base.TABLE+' WHERE source_key=? AND stage=? AND from_generation=?',
                                           (item['source_key'],'publish',auth['from_generation'])).fetchone()
                        base.require(dict(current)==auth,'authorization_changed')
                        n.execute('UPDATE '+base.TABLE+" SET state='superseded_unentered' WHERE source_key=? AND stage=? AND from_generation=?",
                                  (item['source_key'],'publish',auth['from_generation']))
                        new=dict(auth,from_generation=gen,to_generation=gen+1,plan_sha256=plan['plan_sha256'],
                                 actor=actor,created_at=now,proof_json=base.encoded(item))
                        cols=list(new)
                        n.execute('INSERT INTO '+base.TABLE+' ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',
                                  [new[k] for k in cols])
                    else:base.require(existing['plan_sha256']==plan['plan_sha256'] and existing['state']=='armed','authorization_changed')
                    n.commit()
                except BaseException:n.rollback();raise
            c.execute('BEGIN IMMEDIATE')
            try:
                current=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(jid,)).fetchone())
                base.require(base.stable_job(current)==dict(item['prior_job'],hold_reason=HOLD),'claim_changed')
                c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',node_id='',worker_id='',claim_hash='',"
                          "lease_expires_at='',started_at='',finished_at='',updated_at=? WHERE id=?",(now,jid))
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                          (jid,'audited_unentered_publish_recovery',base.encoded(dict(plan_sha256=plan['plan_sha256'],
                           actor=actor,prior_generation=gen,prior_attempts=item['log']['attempt_count'])),now))
                c.execute('UPDATE '+TABLE+" SET state='committed',updated_at=? WHERE job_id=? AND generation=?",(now,jid,gen))
                c.commit()
            except BaseException:c.rollback();raise
            result.append(jid)
    return dict(restored_job_ids=result,backup=str(backup),platform_called=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','apply']);p.add_argument('--central');p.add_argument('--native')
    p.add_argument('--auto',help='Original Auto ledger; required when planning')
    p.add_argument('--job-ids',type=int,nargs='+');p.add_argument('--plan');p.add_argument('--evidence',nargs='+')
    p.add_argument('--backup-dir');p.add_argument('--actor');p.add_argument('--output',required=True);a=p.parse_args()
    if a.action=='plan':base.require(bool(a.auto),'auto_ledger_required')
    with open(a.output,'x') as f:
        result=(make_plan(a.central,a.native,a.job_ids,a.auto) if a.action=='plan' else
                apply_plan(json.loads(Path(a.plan).read_text()),[json.loads(Path(x).read_text()) for x in a.evidence],a.backup_dir,a.actor))
        json.dump(result,f)
    print(json.dumps({'ok':True,'count':len(result.get('items',result.get('restored_job_ids',[])))}))


if __name__=='__main__':main()

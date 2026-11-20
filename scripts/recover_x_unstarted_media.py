"""Resume explicitly scoped X tasks which have no external attempt or intent."""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_media_interrupted import connect, rows, one, require, digest, backup
from features.x_posts.service import XPostStore

TABLE = 'pipeline_x_unstarted_media_recovery'
SCHEMA = ('CREATE TABLE IF NOT EXISTS ' + TABLE +
          '(source_key TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,proof_json TEXT NOT NULL,created_at TEXT NOT NULL)')


def collect(c, n, job_id):
    job = one(c, 'SELECT * FROM pipeline_job WHERE id=?', (job_id,))
    key, qid = job['source_key'], int(job['source_id'])
    p = {'job':job, 'queue':one(n, 'SELECT * FROM x_post_queue WHERE id=?', (qid,)),
         'logs':rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (qid,)),
         'selected':rows(n, 'SELECT * FROM pipeline_native_media_decision WHERE source_key=?', (key,))}
    p['operations'] = rows(c, 'SELECT * FROM pipeline_operation WHERE job_id IN (SELECT id FROM pipeline_job WHERE source_key=?)', (key,))
    p['native_operations'] = rows(n, 'SELECT * FROM pipeline_x_operation WHERE source_key=?', (key,))
    p['publications'] = rows(c, 'SELECT * FROM pipeline_publication WHERE source_key=?', (key,))
    p['holds'] = rows(n, 'SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (key,))
    p['resources'] = rows(c, 'SELECT * FROM pipeline_resource WHERE job_id=?', (job_id,))
    p['legacy_resources'] = rows(c, 'SELECT * FROM pipeline_legacy_resource WHERE job_id=?', (job_id,))
    p['reposts'] = rows(n, 'SELECT * FROM x_post_repost_ledger WHERE queue_id=?', (qid,))
    return p


def validate(p):
    j, q = p['job'], p['queue']
    require(j['channel']=='x' and j['family']=='queue' and j['status']=='failed'
            and not j['external_intent'] and not j['hold_reason'], 'dispatch_not_unstarted_failure')
    require(q['status']=='failed', 'native_not_failed')
    require(not any(p[k] for k in ('operations','native_operations','publications','holds','resources','legacy_resources','reposts')),
            'external_intent_or_resource_present')
    if j['stage']=='prepare':
        require(j['generation']==0 and not p['logs'], 'preparation_has_attempt')
        require(len(p['selected'])==1 and p['selected'][0]['status']=='skipped'
                and p['selected'][0]['kind']=='deadline_original'
                and p['selected'][0]['reason'] in ('invalid_media_codec','invalid_media_dimensions'), 'not_repairable_source')
        return 'native_preparation'
    require(j['stage']=='publish' and j['error_code']=='coordinator_operation_failed', 'not_unentered_publish')
    require(len(p['logs'])==1, 'publish_log_missing')
    l=p['logs'][0]
    require(l['status']=='failed' and not any(l[k] for k in ('attempt_count','unknown_outcome','x_media_id','x_post_id','published_at')),
            'publish_attempt_present')
    return 'first_publish'


def stable(p):
    p=json.loads(json.dumps(p))
    for k in ('updated_at','source_status','source_meta_json'):p['job'].pop(k,None)
    return p


def plan(scope, central, natives):
    result={'central':central,'items':[],'excluded':[]}
    with contextlib.closing(connect(central)) as c:
        for item in scope:
            for job in rows(c, "SELECT id,status FROM pipeline_job WHERE source_key=? AND status='failed'", (item['source_key'],)):
                native=natives['tag' if int(item['source_id'])>=9000000000000 else 'x']
                with contextlib.closing(connect(native)) as n:
                    p=collect(c,n,job['id'])
                    try:action=validate(p)
                    except ValueError as exc:
                        result['excluded'].append({'job_id':job['id'],'reason':str(exc)})
                        continue
                    result['items'].append({'native':native,'proof':p,'action':action})
    result['plan_sha256']=digest(result)
    return result


def apply(plan, directory):
    unsigned=dict(plan);unsigned.pop('plan_sha256')
    require(digest(unsigned)==plan['plan_sha256'], 'plan_changed')
    require(Path('/run/pipeline-service-drain/x-post-automation.service.json').exists()
            or Path('/run/pipeline-service-drain/pipeline-coordinator.service.json').exists(), 'x_admission_not_held')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for idx,path in enumerate(sorted({plan['central']}|{i['native'] for i in plan['items']})):
        if not (directory/(str(idx)+'.sqlite3')).exists():backup(path,directory,str(idx))
    completed=[]
    for item in plan['items']:
        saved=item['proof'];job=saved['job'];key=job['source_key'];qid=int(job['source_id'])
        with contextlib.closing(connect(plan['central'],False)) as c, contextlib.closing(connect(item['native'],False)) as n:
            c.execute(SCHEMA);c.commit();n.execute(SCHEMA);n.commit()
            done=c.execute('SELECT plan_sha256 FROM '+TABLE+' WHERE source_key=?',(key,)).fetchone()
            if done:
                require(done[0]==plan['plan_sha256'],'other_recovery');completed.append(job['id']);continue
            n.execute('BEGIN IMMEDIATE');c.execute('BEGIN IMMEDIATE')
            current=collect(c,n,job['id']);validate(current)
            require(stable(current)==stable(saved),'execution_facts_changed')
            now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
            values=(key,plan['plan_sha256'],json.dumps(saved,ensure_ascii=False),now)
            n.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)',values)
            n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",(now,qid))
            if item['action']=='first_publish':
                n.execute("UPDATE x_post_publish_log SET status='reserved',updated_at=? WHERE queue_id=?",(now,qid))
            XPostStore._sync_run(n,qid,now)
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?)',values)
            c.execute("UPDATE pipeline_job SET status=?,hold_reason='',started_at='',finished_at='',lease_expires_at='',node_id='',worker_id='',claim_hash='',error_code='',error_message='',updated_at=? WHERE id=?",
                      ('queued' if job['stage']=='prepare' else 'ready',now,job['id']))
            if job['stage']=='prepare':
                c.execute("UPDATE pipeline_media_gate SET phase='native',error='',retry_at='' WHERE source_key=?",(key,))
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                      (job['id'],'audited_unstarted_media_recovery',json.dumps({'plan_sha256':plan['plan_sha256'],'action':item['action'],'prior_generation':job['generation']}),now))
            n.commit();c.commit();completed.append(job['id'])
    return {'restored_job_ids':completed,'platform_called':False,'plan_sha256':plan['plan_sha256']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','apply']);p.add_argument('--scope');p.add_argument('--central');p.add_argument('--x');p.add_argument('--tag')
    p.add_argument('--plan');p.add_argument('--backup-dir');p.add_argument('--output',required=True);a=p.parse_args()
    result=(plan(json.loads(Path(a.scope).read_text()),a.central,{'x':a.x,'tag':a.tag}) if a.action=='plan'
            else apply(json.loads(Path(a.plan).read_text()),a.backup_dir))
    with Path(a.output).open('x') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps({'count':len(result.get('items',result.get('restored_job_ids',[]))),'excluded':result.get('excluded',[])}))


if __name__=='__main__':main()

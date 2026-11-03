#!/usr/bin/env python3
"""Exact third incident recovery: zero plan writes, no assumption about old errno."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import identity,fingerprint
from scripts.recover_pipeline_x_preparation import RecoveryRejected,read_connection,require,rows
from scripts.recover_x_prepost_failures import backup,writable
from scripts.recover_x_schedule_scope_churn import verify_runner

FIRST='x_post_schedule_zero_write_resume_audit'
SECOND='x_post_schedule_scope_churn_resume_audit'
TABLE='x_post_schedule_read_preflight_resume_audit'
SCHEMA='''CREATE TABLE IF NOT EXISTS x_post_schedule_read_preflight_resume_audit (
 run_id INTEGER PRIMARY KEY,failed_at TEXT NOT NULL,identity_sha256 TEXT NOT NULL,
 plan_sha256 TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,authorized_at TEXT NOT NULL,
 backup_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL)'''
FILES=('scripts/x_post_schedule_runner.py','features/x_posts/drama_selector.py',
       'features/x_posts/selector.py','features/x_posts/read_failures.py')
EXACT={
 1029:dict(failed_at='2026-11-03T11:59:14Z',error_code='x_post_drama_query_failed',snapshot=None,
     identity_sha256='4ee516ba46b0b9fa059e9e0611d47899eee15314d610da4dc3440e3bf2790ab7',
     run_sha256='a8b66715f6b09c41fc50605e244e64facb2dd1b00303eb023317a6aaf6af5e34',
     audits={FIRST:'8771e758888223a13eedf749050544bb906fc0d2231995593c6d025acc844260'}),
 1034:dict(failed_at='2026-11-03T11:57:50Z',error_code='x_upstream_error',snapshot=4444,
     identity_sha256='5c33a45241b45be3552f6b42c9df18e4f7c8f8c65065c2d070039fe01c493a41',
     run_sha256='aeeca26185ef2c663f3abe32ac170484c18afbd281003d2e0b915f451e440da3',
     audits={FIRST:'13f5bb4717fcaca7d2d90d3c2d1eab53b00ab17c5a5f9a4e0853767533d112c5',
             SECOND:'ca1aa5392a20cbbd04d8cb996b756fa5b5bb35fb1107e6f4fb429be889d1c914'}),
}


def audit(c,table,rid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return []
    return rows(c,'SELECT * FROM '+table+' WHERE run_id=?',(rid,))


def verify_deployed(commit,runner_path,repository):
    result=verify_runner(commit,runner_path,repository);root=Path(runner_path).absolute().parent.parent;files={}
    for name in FILES:
        blob=subprocess.run(['git','-C',str(repository),'show',commit+':'+name],capture_output=True,timeout=15,check=False)
        require(blob.returncode==0,'repair_file_missing');path=root/name
        require(path.is_file() and path.read_bytes()==blob.stdout,'deployed_read_repair_changed')
        if name==FILES[0]:require(b'def _readonly_preflight_call(' in blob.stdout and b'waiting_preflight' in blob.stdout,'read_retry_fix_missing')
        files[name]=hashlib.sha256(blob.stdout).hexdigest()
    result['files']=files;return result


def snapshot(c,ids):
    require(ids and len(ids)==len(set(ids)) and all(type(i) is int and i in EXACT for i in ids),'exact_incident_scope_required')
    result=[]
    for rid in sorted(ids):
        anchor=EXACT[rid];found=rows(c,'SELECT * FROM x_post_schedule_run WHERE id=?',(rid,));require(len(found)==1,'schedule_missing');run=found[0]
        require(run['status']=='failed_preflight' and run['error_code']==anchor['error_code']
                and run['finished_at']==run['updated_at']==anchor['failed_at']
                and run['material_pool_snapshot_max_id']==anchor['snapshot']
                and not any(run[k] for k in ('started_at','plan_attempted_at','queued_count','published_count','failed_count','unknown_count')),'not_exact_zero_write_failure')
        require(fingerprint(run)==anchor['run_sha256'] and fingerprint(identity(run))==anchor['identity_sha256'],'original_failure_or_identity_changed')
        require(not c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?',(rid,)).fetchone(),'schedule_queue_exists')
        require(not audit(c,TABLE,rid),'third_incident_recovery_exists')
        previous={}
        for table in (FIRST,SECOND):
            found=audit(c,table,rid)
            if table in anchor['audits']:
                require(len(found)==1 and fingerprint(found[0])==anchor['audits'][table],'previous_recovery_audit_changed');previous[table]=found[0]
            else:require(not found,'unexpected_previous_recovery')
        require(previous[FIRST]['identity_sha256']==anchor['identity_sha256'],'original_authorization_identity_changed')
        result.append(dict(run_id=rid,previous_run=run,previous_audits=previous,identity_sha256=anchor['identity_sha256'],queue_count=0))
    return result


def plan(path,ids,commit,runner_path,repository):
    repair=verify_deployed(commit,runner_path,repository)
    with contextlib.closing(read_connection(path)) as c:
        c.execute('BEGIN');result=dict(kind='x_schedule_read_preflight_exact_resume',version=1,db_path=str(Path(path).resolve()),run_ids=sorted(ids),items=snapshot(c,ids),repair_evidence=repair)
    result['plan_sha256']=fingerprint(result);return result


def apply(path,saved,backup_dir,actor,reason):
    require(saved.get('kind')=='x_schedule_read_preflight_exact_resume' and saved.get('version')==1
            and saved['plan_sha256']==fingerprint({k:v for k,v in saved.items() if k!='plan_sha256'}),'plan_hash_mismatch')
    require(saved['db_path']==str(Path(path).resolve()) and str(actor).strip() and str(reason).strip(),'path_actor_reason_invalid')
    require(saved['run_ids'] and set(saved['run_ids'])<=set(EXACT),'exact_incident_scope_required')
    repair=saved['repair_evidence']
    def check():require(verify_deployed(repair['commit'],repair['runner_path'],repair['repository'])==repair,'repair_evidence_changed')
    with contextlib.closing(read_connection(path)) as c:
        previous=[audit(c,TABLE,rid) for rid in saved['run_ids']]
        if all(len(p)==1 and p[0]['plan_sha256']==saved['plan_sha256'] for p in previous):
            return dict(already_applied=True,run_ids=saved['run_ids'],plan_sha256=saved['plan_sha256'],platform_called=False)
        check();require(snapshot(c,saved['run_ids'])==saved['items'],'plan_state_drift')
    target=backup(path,backup_dir,'schedule-read-preflight');sha=hashlib.sha256()
    with open(target,'rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):sha.update(data)
    with contextlib.closing(writable(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            check();require(snapshot(c,saved['run_ids'])==saved['items'],'plan_state_drift');c.execute(SCHEMA)
            now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            for item in saved['items']:
                c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?)',(item['run_id'],item['previous_run']['finished_at'],item['identity_sha256'],saved['plan_sha256'],actor,reason,now,sha.hexdigest(),json.dumps(dict(item,repair_evidence=repair,backup_path=target),ensure_ascii=False,sort_keys=True)))
                changed=c.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',finished_at='',updated_at=?,lease_heartbeat_at=? WHERE id=? AND status='failed_preflight' AND plan_attempted_at='' AND finished_at=?",(now,now,item['run_id'],item['previous_run']['finished_at'])).rowcount
                require(changed==1,'schedule_compare_failed')
            c.commit()
        except BaseException:c.rollback();raise
    return dict(applied=True,run_ids=saved['run_ids'],plan_sha256=saved['plan_sha256'],backup_path=target,backup_sha256=sha.hexdigest(),platform_called=False,queues_created=0,previous_audits_modified=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('plan','apply'))
    for name in ('db','run-ids','repair-commit','runner-path','repository','plan','backup-dir','actor','reason'):p.add_argument('--'+name)
    p.add_argument('--output',required=True);a=p.parse_args()
    with os.fdopen(os.open(a.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
        result=plan(a.db,[int(i) for i in a.run_ids.split(',')],a.repair_commit,a.runner_path,a.repository) if a.action=='plan' else apply(a.db,json.loads(Path(a.plan).read_text(encoding='utf-8')),a.backup_dir,a.actor,a.reason)
        json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    print(json.dumps({k:result[k] for k in ('run_ids','plan_sha256','applied','already_applied') if k in result}))


if __name__=='__main__':main()

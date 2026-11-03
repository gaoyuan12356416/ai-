#!/usr/bin/env python3
"""Second, separately audited recovery of exact zero-write scope-churn failures."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import TABLE as FIRST_TABLE, identity, fingerprint
from scripts.recover_pipeline_x_preparation import RecoveryRejected, read_connection, require, rows
from scripts.recover_x_prepost_failures import backup, writable

EXACT = {
    1034: dict(publish_time='08:45',failed_at='2026-11-03T11:44:28Z',
        identity_sha256='5c33a45241b45be3552f6b42c9df18e4f7c8f8c65065c2d070039fe01c493a41',
        run_sha256='0f28b856313a1e66d0463a49622d9b15b2b3cdd2f2da9b667214fa38a31c1641',
        first_audit_sha256='13f5bb4717fcaca7d2d90d3c2d1eab53b00ab17c5a5f9a4e0853767533d112c5'),
    1036: dict(publish_time='12:41',failed_at='2026-11-03T11:41:47Z',
        identity_sha256='2028d029e81de6fe75ff362fccf330a34f6b824dd1f367f9a2e929fe02e25663',
        run_sha256='edc95563c44a684635fe3dd07f284ab5b8eb7e070bc3932805b6643b93c80ed9',
        first_audit_sha256='ad4e9a2e919e84ee49f5adbd869d140d1a7aa5556b0dc2c5f9a10da5f328249b'),
}
TABLE = 'x_post_schedule_scope_churn_resume_audit'
SCHEMA = '''CREATE TABLE IF NOT EXISTS x_post_schedule_scope_churn_resume_audit (
 run_id INTEGER PRIMARY KEY,failed_at TEXT NOT NULL,identity_sha256 TEXT NOT NULL,
 first_audit_sha256 TEXT NOT NULL,plan_sha256 TEXT NOT NULL,actor TEXT NOT NULL,
 reason TEXT NOT NULL,authorized_at TEXT NOT NULL,backup_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL)'''


def existing(c,rid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return None
    row=c.execute('SELECT * FROM '+TABLE+' WHERE run_id=?',(rid,)).fetchone()
    return dict(row) if row else None


def verify_runner(commit,runner_path,repository):
    require(isinstance(commit,str) and re.fullmatch(r'[a-f0-9]{40}',commit),'repair_commit_required')
    result=subprocess.run(['git','-C',str(repository),'show',commit+':scripts/x_post_schedule_runner.py'],capture_output=True,timeout=15,check=False)
    require(result.returncode==0,'repair_commit_unavailable')
    blob=result.stdout
    require(all(marker in blob for marker in (b'class ScheduleAccountScopeChanged',b'def _wait_for_account_scope(',b'if planning_passes >= 3:',b'except ScheduleAccountScopeChanged as exc:',b'retry_next_tick=waiting')),'repair_commit_missing_scope_wait')
    path=Path(runner_path).absolute()
    require(path.is_file() and path.read_bytes()==blob,'deployed_runner_not_repair_commit')
    return dict(commit=commit,runner_path=str(path),runner_resolved_path=str(path.resolve()),repository=str(Path(repository).absolute()),runner_sha256=hashlib.sha256(blob).hexdigest())


def snapshot(c,run_ids):
    require(run_ids and len(run_ids)==len(set(run_ids)) and all(type(rid) is int and rid in EXACT for rid in run_ids),'exact_incident_scope_required')
    items=[]
    for rid in sorted(run_ids):
        anchor=EXACT[rid];found=rows(c,'SELECT * FROM x_post_schedule_run WHERE id=?',(rid,))
        require(len(found)==1,'schedule_missing');run=found[0]
        require(run['source_type']=='material' and run['run_date']=='2026-11-03' and run['publish_time']==anchor['publish_time']
                and run['status']=='failed_preflight' and run['error_code']=='x_post_schedule_account_state_changed'
                and run['finished_at']==run['updated_at']==anchor['failed_at'] and run['material_pool_snapshot_max_id']==4444
                and not any(run[k] for k in ('started_at','plan_attempted_at','queued_count','published_count','failed_count','unknown_count')),'not_exact_zero_write_scope_failure')
        require(fingerprint(run)==anchor['run_sha256'] and fingerprint(identity(run))==anchor['identity_sha256'],'original_failure_or_identity_changed')
        require(not c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?',(rid,)).fetchone(),'schedule_queue_exists')
        first=rows(c,'SELECT * FROM '+FIRST_TABLE+' WHERE run_id=?',(rid,))
        require(len(first)==1 and fingerprint(first[0])==anchor['first_audit_sha256'] and first[0]['identity_sha256']==anchor['identity_sha256'],'first_recovery_audit_changed')
        require(existing(c,rid) is None,'second_recovery_already_recorded')
        accounts=json.loads(run['account_ids_json'])
        require(accounts and len(accounts)==len(set(accounts))==run['expected_count'],'frozen_account_scope_invalid')
        state=rows(c,'SELECT id,x_user_id,status,publish_approved,drama_language,deleted_at FROM x_authorized_account WHERE id IN ('+','.join('?' for _ in accounts)+') ORDER BY id',accounts)
        require(len(state)==len(accounts) and any(a['status']=='active' and a['publish_approved']==1 and not a['deleted_at'] for a in state),'accounts_unavailable')
        items.append(dict(run_id=rid,previous_run=run,first_audit=first[0],first_audit_sha256=anchor['first_audit_sha256'],identity_sha256=anchor['identity_sha256'],account_state=state,queue_count=0))
    return items


def plan(path,run_ids,commit,runner_path,repository):
    repair=verify_runner(commit,runner_path,repository)
    with contextlib.closing(read_connection(path)) as c:
        c.execute('BEGIN')
        result=dict(kind='x_schedule_scope_second_resume',version=1,db_path=str(Path(path).resolve()),run_ids=sorted(run_ids),items=snapshot(c,run_ids),repair_evidence=repair)
    result['plan_sha256']=fingerprint(result);return result


def apply(path,saved,backup_dir,actor,reason):
    require(saved.get('kind')=='x_schedule_scope_second_resume' and saved.get('version')==1
            and fingerprint({k:v for k,v in saved.items() if k!='plan_sha256'})==saved['plan_sha256'],'plan_hash_mismatch')
    require(saved['db_path']==str(Path(path).resolve()) and str(actor).strip() and str(reason).strip(),'path_actor_reason_invalid')
    require(saved['run_ids'] and set(saved['run_ids'])<=set(EXACT),'exact_incident_scope_required')
    repair=saved['repair_evidence']
    def check_repair():
        require(verify_runner(repair['commit'],repair['runner_path'],repair['repository'])==repair,'repair_evidence_changed')
    with contextlib.closing(read_connection(path)) as c:
        previous=[existing(c,rid) for rid in saved['run_ids']]
        if all(a and a['plan_sha256']==saved['plan_sha256'] for a in previous):
            return dict(already_applied=True,run_ids=saved['run_ids'],plan_sha256=saved['plan_sha256'],platform_called=False)
        check_repair();require(snapshot(c,saved['run_ids'])==saved['items'],'plan_state_drift')
    target=backup(path,backup_dir,'schedule-scope-second');sha=hashlib.sha256()
    with open(target,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block)
    with contextlib.closing(writable(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            check_repair();require(snapshot(c,saved['run_ids'])==saved['items'],'plan_state_drift');c.execute(SCHEMA)
            now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            for item in saved['items']:
                c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?)',(item['run_id'],item['previous_run']['finished_at'],item['identity_sha256'],item['first_audit_sha256'],saved['plan_sha256'],actor,reason,now,sha.hexdigest(),json.dumps(dict(item,repair_evidence=repair,backup_path=target),ensure_ascii=False,sort_keys=True)))
                changed=c.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',finished_at='',updated_at=?,lease_heartbeat_at=? WHERE id=? AND status='failed_preflight' AND plan_attempted_at='' AND finished_at=?",(now,now,item['run_id'],item['previous_run']['finished_at'])).rowcount
                require(changed==1,'schedule_compare_failed')
            c.commit()
        except BaseException:c.rollback();raise
    return dict(applied=True,run_ids=saved['run_ids'],plan_sha256=saved['plan_sha256'],backup_path=target,backup_sha256=sha.hexdigest(),platform_called=False,queues_created=0,first_audit_modified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('plan','apply'))
    for name in ('db','run-ids','repair-commit','runner-path','repository','plan','backup-dir','actor','reason'):parser.add_argument('--'+name)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    with os.fdopen(os.open(args.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
        if args.action=='plan':result=plan(args.db,[int(i) for i in args.run_ids.split(',')],args.repair_commit,args.runner_path,args.repository)
        else:result=apply(args.db,json.loads(Path(args.plan).read_text(encoding='utf-8')),args.backup_dir,args.actor,args.reason)
        json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    print(json.dumps({k:result[k] for k in ('run_ids','plan_sha256','applied','already_applied') if k in result}))


if __name__=='__main__':main()

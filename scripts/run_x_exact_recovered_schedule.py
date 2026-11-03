#!/usr/bin/env python3
"""One audited original drama run while the unrelated old scanner drains."""
import argparse
import contextlib
from dataclasses import replace
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import fingerprint,identity,authorized_run_ids
from scripts.recover_pipeline_x_preparation import read_connection,require,RecoveryRejected
from scripts.recover_x_schedule_read_preflight import audit,verify_deployed,FIRST,EPISODE,TABLE as READ_AUDIT
from scripts.x_post_daily_runner import process_lock
from scripts.x_post_schedule_runner import ScheduleConfig,ScheduleSidecarClient,execute_schedule_tick,pipeline_dispatch_enabled

OLD_PID=1478253
OLD_INVOCATION='329559794abd4c6293cce18757516db5'
OLD_START='2026-11-03T11:57:10Z'
OLD_RUNNER_COMMIT='43b2aa9d598fba80ef93cde0c7e1ef38794ed78f'
STATE_ROOT=Path('/mnt/data-disk/x-publish-recovery-20261103/targeted-planner')
EXACT={
 1029:dict(day='2026-11-02',minute='16:28',grant_table=READ_AUDIT,
   identity_sha256='4ee516ba46b0b9fa059e9e0611d47899eee15314d610da4dc3440e3bf2790ab7',
   previous_failure_sha256='a8b66715f6b09c41fc50605e244e64facb2dd1b00303eb023317a6aaf6af5e34',
   first_sha256='8771e758888223a13eedf749050544bb906fc0d2231995593c6d025acc844260',
   grant_sha256='a527d6821ecfa35d4c7267562051a712efc3e994f2aa723910a0ac3dfa668426'),
 1031:dict(day='2026-11-03',minute='08:44',grant_table=EPISODE,
   identity_sha256='a364aea4261cc7d0285aec2f8eea1400cbfe27b78dfcdf83aeade386f981baa9',
   previous_failure_sha256='60f447c9aef471a794aa6f01ff49fe7f53cd916d7689244a2d55423906b8ebd0',
   first_sha256='cb7bdd8e35ab0fc306e6a8524245f405a3be536f86372d28e97a329600979f1c'),
}


def unit(name):
    result=subprocess.run(['systemctl','show',name,'-p','MainPID','-p','ActiveState','-p','SubState','-p','InvocationID'],capture_output=True,text=True,timeout=10,check=True)
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)


def owner_state(*,executing=False):
    service=unit('x-post-schedule.service');timer=unit('x-post-schedule.timer')
    same_invocation=service.get('InvocationID')==OLD_INVOCATION
    live=int(service.get('MainPID') or 0)==OLD_PID and same_invocation and service.get('ActiveState') in ('active','activating')
    ended=(executing and int(service.get('MainPID') or 0)==0
           and service.get('InvocationID') in ('',OLD_INVOCATION)
           and service.get('ActiveState') in ('inactive','failed'))
    require(live or ended,'old_scanner_changed_use_normal_timer')
    if executing:require(timer.get('ActiveState')=='inactive' and timer.get('SubState')=='dead','schedule_timer_must_be_paused')
    return dict(pid=OLD_PID,invocation=OLD_INVOCATION,started_at=OLD_START)


def snapshot(c,rid):
    require(type(rid) is int and rid in EXACT,'exact_target_required');anchor=EXACT[rid]
    row=c.execute('SELECT * FROM x_post_schedule_run WHERE id=?',(rid,)).fetchone();require(row is not None,'run_missing');run=dict(row)
    require(run['status']=='claimed' and run['source_type']=='drama' and run['run_date']==anchor['day'] and run['publish_time']==anchor['minute']
            and run['config_version']==27 and run['material_pool_snapshot_max_id'] is None
            and not any(run[k] for k in ('plan_attempted_at','started_at','finished_at','error_code','error_message','queued_count','published_count','failed_count','unknown_count')),'target_not_unstarted_claim')
    require(fingerprint(identity(run))==anchor['identity_sha256'] and rid in authorized_run_ids(c),'frozen_identity_or_admission_changed')
    require(not c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?',(rid,)).fetchone(),'target_queue_exists')
    first=audit(c,FIRST,rid);grants=audit(c,anchor['grant_table'],rid)
    require(len(first)==len(grants)==1 and fingerprint(first[0])==anchor['first_sha256'],'original_audit_changed')
    grant=grants[0];proof=json.loads(grant['evidence_json']);previous=proof['previous_run']
    require(fingerprint(previous)==anchor['previous_failure_sha256'] and previous['status']=='failed_preflight'
            and grant['identity_sha256']==anchor['identity_sha256'] and grant['authorized_at']>OLD_START,'terminal_path_evidence_missing')
    if 'grant_sha256' in anchor:require(fingerprint(grant)==anchor['grant_sha256'],'recovery_grant_changed')
    # Any newer heartbeat means another planner already started this target.
    require(run['updated_at']==run['lease_heartbeat_at']==grant['authorized_at'],'target_already_planning')
    if rid==1029:
        prior_audits=proof.get('previous_audits',{})
        require(set(prior_audits)=={FIRST,EPISODE} and
                all(audit(c,t,rid)==[a] for t,a in prior_audits.items()),'previous_audit_chain_changed')
        require(OLD_START<previous['finished_at']<grant['authorized_at'] and not previous['plan_attempted_at'],'old_target_not_in_terminal_return_path')
        mode='old_invocation_known_failure_then_return'
    else:
        require(proof.get('original_recovery_audit')==first[0],'previous_audit_chain_changed')
        require(previous['updated_at']==previous['finished_at']<OLD_START,'target_not_terminal_before_old_due_snapshot')
        prior=c.execute('SELECT * FROM x_post_schedule_run WHERE id=1029').fetchone()
        queues=c.execute('SELECT id,account_id FROM x_post_queue WHERE schedule_run_id=1029 ORDER BY id').fetchall()
        require(prior and queues and prior['plan_attempted_at'] and prior['queued_count']==len(queues),'1029_must_have_real_queues_first')
        mode='terminal_before_old_invocation_and_rearmed_after_snapshot'
    return dict(run_id=rid,run=run,grant=grant,grant_sha256=fingerprint(grant),first_audit=first[0],previous_failure=previous,old_path_proof=mode)


def plan(path,rid,commit,runner_path,repository,*,owner_check=owner_state):
    repair=verify_deployed(commit,runner_path,repository)
    runner=Path(runner_path).read_bytes();require(b'def _targeted_schedule_lock(' in runner,'shared_target_mutex_not_deployed')
    # This exact old version records a preflight failure and returns from the
    # per-identity worker; it has no continuation which can write a plan.
    old=subprocess.run(['git','-C',str(repository),'cat-file','-e',OLD_RUNNER_COMMIT+':scripts/x_post_schedule_runner.py'],capture_output=True,check=False)
    require(old.returncode==0,'audited_old_runner_source_missing')
    owner=owner_check()
    with contextlib.closing(read_connection(path)) as c:
        c.execute('BEGIN');item=snapshot(c,rid)
    result=dict(kind='x_exact_recovered_schedule_tick',version=1,db_path=str(Path(path).resolve()),item=item,repair=repair,old_owner=owner,old_runner_commit=OLD_RUNNER_COMMIT)
    result['plan_sha256']=fingerprint(result);return result


def due_identity(item):
    r=item['run']
    return dict(source_type=r['source_type'],run_date=r['run_date'],publish_time=r['publish_time'],timezone=r['timezone'],
                version=r['config_version'],account_ids=json.loads(r['account_ids_json']),schedule_mode=r['schedule_mode'],
                body_template=r['body_template'],slot_key=r['slot_key'],frozen=True)


class TargetClient:
    def __init__(self,client,item,guard,initial_check):
        self.client,self.item,self.guard=client,item,guard;self.identity=due_identity(item)
        self.initial_check=initial_check;self.first_query=True
    def due_schedules(self,*args,**kwargs):
        self.guard();return [dict(self.identity)]  # Do not claim any other slot.
    def publish_queue(self,*args,**kwargs):raise RecoveryRejected('legacy_publish_forbidden')
    def query_schedule_plan(self,path,identity):
        self.guard()
        if self.first_query:
            # The normal worker already holds the shared target mutex here.
            self.initial_check();self.first_query=False
        return self.client.query_schedule_plan(path,identity)
    def create_schedule_plan(self,path,payload):
        self.guard()
        require(all(payload.get(k)==self.identity[k] for k in ('source_type','run_date','publish_time','version')),'plan_target_changed')
        return self.client.create_schedule_plan(path,payload)
    def __getattr__(self,name):
        value=getattr(self.client,name)
        if not callable(value):return value
        def guarded(*args,**kwargs):self.guard();return value(*args,**kwargs)
        return guarded


def run(saved,actor,*,config=None,client=None,owner_check=owner_state,lock_factory=process_lock,tick=execute_schedule_tick,state_root=STATE_ROOT):
    require(saved.get('kind')=='x_exact_recovered_schedule_tick' and saved.get('version')==1
            and saved['plan_sha256']==fingerprint({k:v for k,v in saved.items() if k!='plan_sha256'}),'plan_changed')
    require(str(actor).strip() and pipeline_dispatch_enabled(),'actor_or_dispatch_required')
    rid=saved['item']['run_id'];require(rid in EXACT,'exact_target_required')
    config=config or ScheduleConfig.from_env();config.validate()
    require(config.lock_path=='/run/x-post-daily/schedule.lock','normal_lock_path_changed')
    repair=saved['repair'];require(verify_deployed(repair['commit'],repair['runner_path'],repair['repository'])==repair,'deployed_runtime_changed')
    require(owner_check()==saved['old_owner'],'old_scanner_changed_use_normal_timer')
    owner_check(executing=True)
    state_root=Path(state_root);state_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock_factory(str(Path(config.lock_path).parent/'schedule-targeted-operator.lock')) as held:
        require(held is not None,'another_targeted_operator_running')
        owner_check(executing=True)
        with contextlib.closing(read_connection(saved['db_path'])) as c:
            c.execute('BEGIN');require(snapshot(c,rid)==saved['item'],'target_state_drift')
        # A root maintenance process must leave the shared lock writable by the
        # normal service user. Match the existing global lock's owner/mode.
        target_lock=Path(config.lock_path).parent/('schedule-targeted-%s.lock'%rid)
        if os.name=='posix':
            original=Path(config.lock_path).stat()
            fd=os.open(target_lock,os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            try:
                if os.geteuid()==0:os.fchown(fd,original.st_uid,original.st_gid)
            finally:os.close(fd)
        marker=state_root/('run-%s-%s.json'%(rid,saved['item']['grant_sha256']))
        evidence=dict(plan=saved,actor=actor,started_at=dt.datetime.now(dt.timezone.utc).isoformat(),phase='invoked')
        with os.fdopen(os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
            json.dump(evidence,stream,ensure_ascii=False,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        def guard():
            require(pipeline_dispatch_enabled(),'dispatch_disabled')
            owner_check(executing=True)
        def initial_check():
            with contextlib.closing(read_connection(saved['db_path'])) as c:
                c.execute('BEGIN');require(snapshot(c,rid)==saved['item'],'target_state_drift_inside_mutex')
        result=tick(replace(config,batch_workers=1),sidecar=TargetClient(client or ScheduleSidecarClient(config.internal_url,config.internal_token,timeout=config.internal_timeout),saved['item'],guard,initial_check))
        require(result.get('processed_count',0)<=1,'unexpected_target_count')
        evidence.update(phase='returned',result=result,finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        report=marker.with_suffix('.result.json')
        with os.fdopen(os.open(report,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
            json.dump(evidence,stream,ensure_ascii=False,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    return dict(run_id=rid,marker=str(marker),report=str(report),result=result,legacy_publish_called=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('plan','run'))
    for name in ('db','run-id','commit','runner-path','repository','plan','actor'):p.add_argument('--'+name)
    p.add_argument('--output',required=True);a=p.parse_args()
    with os.fdopen(os.open(a.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
        result=plan(a.db,int(a.run_id),a.commit,a.runner_path,a.repository) if a.action=='plan' else run(json.loads(Path(a.plan).read_text(encoding='utf-8')),a.actor)
        json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n')
    print(json.dumps({'action':a.action,'output':a.output}))


if __name__=='__main__':main()

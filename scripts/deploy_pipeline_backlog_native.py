"""Narrow CPU native overlay. Prepare only until an operator invokes apply.

No coordinator, node, environment, capacity, or business-row writes. Apply
requires channel claims already stopped, and stops only the named sidecars.
Xauto stays stopped until a separate release command verifies disposal journal.
Run with Python 3.9 or newer; source is a fetched GitHub repository, no checkout.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

BASE = '95fdaa4c4db0ed19a465cb126d539274e95ea918'
DATA = Path('/mnt/data-disk/pipeline-dispatch')
RUNTIME = Path('/mnt/data-disk/pipeline-dispatch-runtime')
PUBLISHER_SHA = 'da713109c6630fea1df7bba314053ee9f1ad9b837207a93ebf9a8785719a6afb'
SPECS = {
 'x': {'root': '/opt/x-post-automation/current', 'units': ['x-auto-post-service.service', 'x-post-automation.service'],
       'ports': [18833, 8810], 'files': ['features/x_posts/service.py', 'features/x_auto_posts/core.py', 'features/pipeline_dispatch/backlog_contract.py'],
       'triggers': ['x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-runner.path', 'x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer', 'x-post-manual.path', 'x-video-pool-scheduler.timer'],
       'ledgers': ['/var/lib/x-post-automation/accounts.sqlite3', '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3']},
 'tt_auto': {'root': '/opt/tt-auto-post/current', 'units': ['tt-auto-post-service.service'], 'ports': [18831],
       'files': ['features/tt_auto_posts/core.py', 'features/pipeline_dispatch/backlog_contract.py'],
       'triggers': ['tt-auto-post-runner.timer', 'tt-auto-post-scheduler.timer', 'tt-auto-post-reconcile.timer'],
       'ledgers': ['/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3']},
}

def need(value, code):
    if not value: raise RuntimeError(code)

def run(args, *, cwd=None, data=None, timeout=60):
    p = subprocess.run(list(map(str,args)), cwd=cwd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode: raise RuntimeError('command_failed:'+str(args[0])+':'+str(p.returncode))
    return p.stdout

def show(unit, field): return run(['systemctl','show',unit,'-p',field,'--value']).decode().strip()
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def parsed(value): return dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
def save(path, value):
    path=Path(path); temporary=path.with_suffix('.next')
    temporary.write_text(json.dumps(value,indent=2)+'\n'); temporary.chmod(0o600); os.replace(temporary,path)

def tree(root):
    result={}
    for current,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in ('.git','__pycache__'))
        for name in [*dirs,*sorted(files)]:
            p=Path(current)/name; rel=p.relative_to(root).as_posix()
            if name.endswith('.pyc'): continue
            if p.is_symlink(): result[rel]={'link':os.readlink(p)}
            elif p.is_file(): result[rel]={'sha256':digest(p)}
    return result

def source_check(repo, commit):
    need(re.fullmatch('[a-f0-9]{40}',commit),'exact_commit_required')
    need(run(['git','cat-file','-t',commit],cwd=repo).strip()==b'commit','commit_missing')
    origin=run(['git','remote','get-url','origin'],cwd=repo).decode().strip()
    need(origin.startswith(('git@github.com:','https://github.com/','ssh://git@github.com/')),'github_origin_required')
    need(run(['git','for-each-ref','--contains',commit,'--format=%(refname)','refs/remotes/origin'],cwd=repo).strip(),'commit_not_in_fetched_origin')

def runtime_helper(repo, commit, folder):
    # Copy the GitHub utility to the private stage, without changing Git HEAD.
    path=folder/'deploy_pipeline_runtime.py'
    path.write_bytes(run(['git','show',commit+':scripts/deploy_pipeline_runtime.py'],cwd=repo))
    spec=importlib.util.spec_from_file_location('narrow_runtime',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def prepare(repo, commit):
    source_check(repo,commit)
    need(run(['findmnt','-n','-o','UUID','--target','/mnt/data-disk']).decode().strip()=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8','data_mount_mismatch')
    folder=RUNTIME/('backlog-native-'+commit[:10]);folder.mkdir(mode=0o700)
    (folder/'backups').mkdir(mode=0o700)
    helper=runtime_helper(repo,commit,folder)
    manifest={'version':1,'commit':commit,'base':BASE,'status':'preparing','created_at':now(),'lanes':{}}
    save(folder/'manifest.json',manifest)
    for lane,spec in SPECS.items():
        root=Path(spec['root']);previous=root.resolve()
        need(root.is_symlink() and previous==RUNTIME/'native-95fdaa4c4d'/'candidates'/lane,'unreviewed_runtime_root:'+lane)
        before=tree(previous);candidate=folder/'candidates'/lane;backup=folder/'backups'/lane
        backup.mkdir(parents=True);shutil.copytree(previous,candidate,symlinks=True,ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
        helper.preserve_tree_owners(previous,candidate)
        entry={'runtime_root':str(root),'previous_root':str(previous),'symlink':True,'candidate':str(candidate),'files':{},'baseline':{},'before_tree':before,'status':'preparing'}
        for relative in spec['files']:
            live=root/relative;target=candidate/relative;existed=live.is_file()
            entry['baseline'][relative]={'sha256':digest(live)} if existed else {'missing':True}
            target.parent.mkdir(parents=True,exist_ok=True)
            if existed:
                saved=backup/relative;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(live,saved)
                patch=run(['git','diff','--binary','--full-index',BASE,commit,'--',relative],cwd=repo)
                need(bool(patch),'expected_patch_missing:'+relative)
                raw=target.read_bytes();target.write_bytes(raw.replace(b'\r\n',b'\n'))
                command=['git','-c','core.autocrlf=false','-c','core.eol=lf','apply']
                run([*command,'--check','--whitespace=nowarn','-'],cwd=candidate,data=patch)
                run([*command,'--whitespace=nowarn','-'],cwd=candidate,data=patch)
            else:
                need(relative=='features/pipeline_dispatch/backlog_contract.py','unexpected_new_file')
                target.write_bytes(run(['git','show',commit+':'+relative],cwd=repo));target.chmod(0o644)
            compile(target.read_bytes(),relative,'exec')
            entry['files'][relative]={'existed':existed,'before_sha256':digest(live) if existed else '', 'after_sha256':digest(target),'strategy':'strict_95_to_commit_patch' if existed else 'new'}
        need(tree(previous)==before,'live_changed_during_prepare:'+lane)
        after=tree(candidate)
        need({k for k in set(before)|set(after) if before.get(k)!=after.get(k)}==set(spec['files']),'unexpected_candidate_delta:'+lane)
        if lane=='tt_auto':need(digest(candidate/'features/tt_auto_posts/publisher.py')==PUBLISHER_SHA,'tt_publisher_changed')
        entry.update(after_tree=after,status='prepared');manifest['lanes'][lane]=entry;save(folder/'manifest.json',manifest)
    manifest['status']='prepared';save(folder/'manifest.json',manifest)
    return {'folder':str(folder),'commit':commit,'status':'prepared','changed':{k:list(v['files']) for k,v in manifest['lanes'].items()}}

def db(path):
    c=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=10);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c

def admission(lane):
    channel='x' if lane=='x' else 'tt'
    with contextlib.closing(db(DATA/'dispatch.sqlite3')) as c:
        cfg=json.loads(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
        need(channel not in cfg['enabled_channels'],'channel_claims_still_enabled')
        active=[dict(r) for r in c.execute("SELECT * FROM pipeline_job WHERE channel=? AND status='running'",(channel,))]
        if lane=='x':need(not active,'x_node_work_still_running')
        else:
            need(all(j['family']=='auto' and j['stage']=='prepare' and not j['external_intent'] for j in active),'tt_publish_or_other_work_still_running')
            with contextlib.closing(db(SPECS[lane]['ledgers'][0])) as n:
                native=list(n.execute("SELECT * FROM tt_auto_task WHERE status IN ('selecting','preparing','publishing')"))
                need({str(r['id']) for r in native}=={j['source_id'] for j in active},'tt_native_not_exact_node_owned')
                for j in active:
                    task=next(r for r in native if str(r['id'])==j['source_id'])
                    bound=n.execute("SELECT * FROM pipeline_tt_claim WHERE source_key=? AND stage='prepare' AND generation=?",(j['source_key'],j['generation'])).fetchone()
                    need(task['status']=='preparing' and task['claim_phase']=='prepare' and not task['unknown_outcome'] and not task['publish_id'] and not task['publish_attempt_count'],'tt_native_execution_not_prepare')
                    need(bound and bound['claim_hash']==j['claim_hash'] and bound['native_token']==task['claim_token'] and not bound['receipt_json'],'tt_native_binding_mismatch')
                    need(parsed(j['lease_expires_at'])>dt.datetime.now(dt.timezone.utc),'tt_central_lease_expired')
                    worker=c.execute('SELECT * FROM pipeline_worker WHERE node_id=? AND worker_id=?',(j['node_id'],j['worker_id'])).fetchone()
                    need(worker and (dt.datetime.now(dt.timezone.utc)-parsed(worker['last_seen_at'])).total_seconds()<45,'tt_node_heartbeat_stale')
                    need(not n.execute('SELECT 1 FROM pipeline_tt_operation WHERE source_key=? LIMIT 1',(j['source_key'],)).fetchone(),'tt_native_publish_operation_present')
        return [{'id':j['id'],'source_key':j['source_key'],'generation':j['generation'],'node_id':j['node_id']} for j in active]

def accepted(port):return run(['ss','-Htn','state','established','sport','=',':'+str(port)]).strip()

def apply(folder,lane):
    folder=Path(folder).resolve();manifest=json.loads((folder/'manifest.json').read_text());entry=manifest['lanes'][lane];spec=SPECS[lane]
    need(entry['status']=='prepared','lane_not_prepared')
    need(tree(Path(entry['previous_root']))==entry['before_tree'],'runtime_tree_drift')
    need(tree(Path(entry['candidate']))==entry['after_tree'],'candidate_tree_drift')
    preserved=admission(lane)
    audit_path=folder/('apply-'+lane+'.json');need(not audit_path.exists(),'audit_already_exists')
    audit={'at':now(),'lane':lane,'commit':manifest['commit'],'preserved_prepare':preserved,'triggers':{},'rules':[],'stopped':[]}
    for unit in spec['triggers']:
        if show(unit,'LoadState')!='not-found':audit['triggers'][unit]=show(unit,'ActiveState')
    save(audit_path,audit)
    rules=[];swapped=False
    try:
        for unit in audit['triggers']:run(['systemctl','stop',unit])
        # A metadata caller already accepted by native must finish naturally.
        for port in spec['ports']:
            listeners=run(['ss','-Hltn','sport','=',':'+str(port)]).decode().splitlines()
            need(listeners and all('127.0.0.1:'+str(port) in s for s in listeners),'listener_not_loopback')
            rule=['OUTPUT','-d','127.0.0.1','-p','tcp','--dport',str(port),'-m','conntrack','--ctstate','NEW','-m','comment','--comment','backlog-'+lane+'-'+manifest['commit'][:10],'-j','REJECT','--reject-with','tcp-reset']
            run(['iptables','-w','5','-I',*rule]);rules.append(rule);audit['rules']=rules[:];save(audit_path,audit)
        deadline=time.monotonic()+45
        while any(accepted(p) for p in spec['ports']) and time.monotonic()<deadline:time.sleep(1)
        need(not any(accepted(p) for p in spec['ports']),'accepted_native_requests_not_drained')
        admission(lane)
        if lane=='x':
            with contextlib.closing(db(spec['ledgers'][0])) as c:
                need(not c.execute("SELECT 1 FROM x_post_queue WHERE status='publishing' LIMIT 1").fetchone(),'x_queue_publishing')
                need(not c.execute("SELECT 1 FROM x_post_publish_log WHERE status IN ('media_uploading','post_creating','repost_creating','publishing') LIMIT 1").fetchone(),'x_log_publishing')
            with contextlib.closing(db(spec['ledgers'][1])) as c:need(not c.execute("SELECT 1 FROM x_auto_task WHERE status IN ('selecting','preparing','publishing') LIMIT 1").fetchone(),'x_auto_planner_active')
            # These two are metadata-only HTTP callers. The accepted request
            # is already gone; stop their idle/retry wrapper, never a handler.
            for caller in ['x-auto-post-runner.service','x-auto-post-scheduler.service']:
                need(show(caller,'LoadState')=='loaded','metadata_caller_missing')
                run(['systemctl','stop',caller]);need(show(caller,'MainPID')=='0' and show(caller,'ActiveState')=='inactive','metadata_caller_not_stopped')
        for unit in spec['units']:
            run(['systemctl','stop',unit]);need(show(unit,'ActiveState')=='inactive','native_not_stopped');audit['stopped'].append(unit);save(audit_path,audit)
        need(tree(Path(entry['previous_root']))==entry['before_tree'],'runtime_changed_before_swap')
        path=folder/'deploy_pipeline_runtime.py';specmod=importlib.util.spec_from_file_location('narrow_apply',path);helper=importlib.util.module_from_spec(specmod);specmod.loader.exec_module(helper)
        helper.apply(folder,lane,{lane:{'units':spec['units'],'ledgers':spec['ledgers']}});swapped=True
        audit['swapped']=True;save(audit_path,audit)
        start=['x-post-automation.service'] if lane=='x' else spec['units']
        for unit in start:run(['systemctl','start','--no-block',unit])
        for unit in start:
            deadline=time.monotonic()+45
            while show(unit,'MainPID') in ('','0') and time.monotonic()<deadline:time.sleep(.5)
            need(show(unit,'MainPID') not in ('','0'),'new_native_no_pid')
        audit['status']='xauto_stopped_pending_disposal' if lane=='x' else 'applied'
    finally:
        # apply() can fail after an atomic symlink swap but before its audit
        # write. Never accidentally restart the planner in that partial case.
        swapped=swapped or Path(spec['root']).resolve()==Path(entry['candidate']).resolve()
        failed_rules=[]
        for rule in reversed(rules):
            try:run(['iptables','-w','5','-D',*rule])
            except RuntimeError:failed_rules.append(rule)
        audit['rules_removed_at']=now();audit['rule_cleanup_failed']=failed_rules;save(audit_path,audit)
        need(not failed_rules,'temporary_admission_rule_cleanup_failed')
        if not swapped:
            # No code changed: restore only the exact services/triggers stopped here.
            for unit in audit['stopped']:run(['systemctl','start','--no-block',unit])
        for unit,state in audit['triggers'].items():
            if state=='active' and (not swapped or lane!='x' or not unit.startswith('x-auto-post-')):run(['systemctl','start',unit])
    for unit in (['x-post-automation.service'] if lane=='x' else spec['units']):
        deadline=time.monotonic()+60
        while show(unit,'ActiveState')=='activating' and time.monotonic()<deadline:time.sleep(.5)
        need(show(unit,'ActiveState')=='active','new_native_not_active')
    for port in ([8810] if lane=='x' else spec['ports']):
        need(run(['ss','-Hltn','sport','=',':'+str(port)]).strip(),'new_native_not_listening')
    if lane=='tt_auto':need(digest(Path(spec['root'])/'features/tt_auto_posts/publisher.py')==PUBLISHER_SHA,'publisher_changed')
    audit['completed_at']=now();save(audit_path,audit)
    return {'status':audit['status'],'audit':str(audit_path),'nodes_restarted':False,'coordinator_changed':False}

def release_xauto(folder,plan_sha):
    folder=Path(folder).resolve();audit_path=folder/'apply-x.json';audit=json.loads(audit_path.read_text())
    need(audit.get('status')=='xauto_stopped_pending_disposal','xauto_not_pending')
    need(re.fullmatch('[a-f0-9]{64}',plan_sha),'plan_hash_required')
    with contextlib.closing(db(DATA/'dispatch.sqlite3')) as c:
        counts=dict(c.execute("SELECT action,count(*) FROM pipeline_backlog_disposition WHERE plan_sha256=? AND state='committed' GROUP BY action",(plan_sha,)))
        need(counts=={'resume':281,'archive':1175},'exact_disposal_not_complete')
        need(c.execute("SELECT count(*) FROM pipeline_backlog_apply_journal WHERE plan_sha256=? AND phase='completed'",(plan_sha,)).fetchone()[0]==1456,'disposal_journal_incomplete')
    run(['systemctl','start','x-auto-post-service.service'])
    need(show('x-auto-post-service.service','ActiveState')=='active','xauto_not_active')
    for unit,state in audit['triggers'].items():
        if state=='active' and unit.startswith('x-auto-post-'):run(['systemctl','start',unit])
    audit.update(status='released_after_disposal',released_at=now(),disposal_plan_sha256=plan_sha);save(audit_path,audit)
    return {'status':audit['status'],'audit':str(audit_path)}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','apply','release-xauto']);p.add_argument('--source');p.add_argument('--commit');p.add_argument('--folder');p.add_argument('--lane',choices=list(SPECS));p.add_argument('--plan-sha256');a=p.parse_args()
    if a.action=='prepare':need(a.source and a.commit,'source_commit_required');result=prepare(Path(a.source),a.commit)
    elif a.action=='apply':need(a.folder and a.lane,'folder_lane_required');result=apply(a.folder,a.lane)
    else:need(a.folder and a.plan_sha256,'folder_plan_required');result=release_xauto(a.folder,a.plan_sha256)
    print(json.dumps(result))

if __name__=='__main__':main()

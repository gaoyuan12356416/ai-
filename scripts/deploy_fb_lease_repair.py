"""Guarded, GitHub-derived FB lease repair overlay; business ledgers never roll back."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

from scripts.deploy_pipeline_runtime import atomic_copy,preserve_tree_owners,verify_source

BASE='17a9645'
NODE_BASE='179596a9699393cd377c0e06a0bec94e171f14cd'
NODE_FILES=['features/fb_gpu/job_lock.py','features/fb_gpu/render_process.py','features/fb_gpu/prepare_worker.py',
            'features/pipeline_dispatch/worker.py','features/pipeline_dispatch/executors.py']
CENTRAL_FILES=['features/pipeline_dispatch/core.py','features/pipeline_dispatch/service.py']
OVERRIDE='99-fb-lease-receipt-20261104.conf'

def run(args,cwd=None):
    r=subprocess.run([str(x) for x in args],cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    if r.returncode:raise RuntimeError('command_failed:'+str(args[0])+':'+str(r.returncode))
    return r.stdout.decode().strip()

def show(unit,key):return run(['systemctl','show',unit,'-p',key,'--value'])
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,data):path.write_text(json.dumps(data,indent=2));path.chmod(0o600)
def unit_files(unit):
    paths=[show(unit,'FragmentPath'),*show(unit,'DropInPaths').split()]
    return {p:sha(p) for p in paths if p}
def prepare(source,commit,folder,host):
    commit=verify_source(source,commit)
    mount=Path('/mnt/data-disk' if host=='cpu' else '/data')
    assert folder.is_relative_to(mount),'runtime_root_required'
    if host=='cpu':assert mount.is_mount() and run(['findmnt','-n','-o','UUID',mount])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    folder.mkdir(parents=True,exist_ok=False,mode=0o755)
    node='pipeline-node-'+('cpu-main' if host=='cpu' else 'hk-gpu')+'.service'
    lanes={'node':(Path(show(node,'WorkingDirectory')),node,NODE_FILES)}
    if host=='cpu':lanes['coordinator']=(Path('/opt/pipeline-dispatch/current'),'pipeline-coordinator.service',CENTRAL_FILES)
    manifest={'commit':commit,'host':host,'source':str(source),'status':'prepared','lanes':{}}
    for name,(root,unit,files) in lanes.items():
        candidate=folder/name;shutil.copytree(root.resolve(),candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
        preserve_tree_owners(root.resolve(),candidate)
        record={'unit':unit,'root':str(root),'previous':str(root.resolve()),'candidate':str(candidate),
                'unit_files':unit_files(unit),'exec_start':show(unit,'ExecStart'),'files':{}}
        manifest['lanes'][name]=record
        for relative in files:
            base=NODE_BASE if relative.endswith('/executors.py') else BASE
            old=subprocess.run(['git','show',base+':'+relative],cwd=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            live=root/relative;target=candidate/relative
            assert target.resolve().is_relative_to(candidate.resolve()),'overlay_escape'
            if relative.endswith('/executors.py') and host=='cpu':assert sha(live)=='599485473e70ace13d5bbac5ae238a3ae0866988b838782797a621fd0b69bcc0','unreviewed_cpu_executor_baseline'
            elif old.returncode==0:assert live.read_bytes().replace(b'\r\n',b'\n')==old.stdout,'unreviewed_live_baseline:'+relative
            else:assert not live.exists(),'new_file_collision:'+relative
            before=sha(live) if live.exists() else ''
            if relative.endswith('/executors.py'):
                # CPU already has the independent 10500ac X segment change;
                # preserve each host's existing behavior and apply only our
                # two FB executor hunks from the reviewed node baseline.
                patch=subprocess.check_output(['git','diff',NODE_BASE,commit,'--',relative],cwd=source)
                result=subprocess.run(['git','apply','--unsafe-paths','-'],cwd=candidate,input=patch,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                assert result.returncode==0,'fb_executor_overlay_failed'
            else:atomic_copy(source/relative,target)
            compile(target.read_bytes(),relative,'exec')
            record['files'][relative]={'before':before,'after':sha(target)}
        if name=='node':
            python=re.search(r'path=([^ ;]+)',record['exec_start']).group(1)
            record['python']=python
            override=Path('/etc/systemd/system')/(unit+'.d')/OVERRIDE
            assert not override.exists(),'override_already_exists'
            record['override']=str(override)
            # Validate imports as the actual unprivileged node user.
            run(['runuser','-u',show(unit,'User') or 'root','--',python,'-c',
                 'from features.pipeline_dispatch.worker import run_node; from features.fb_gpu.job_lock import exclusive_job'],cwd=candidate)
    save(folder/'manifest.json',manifest)
    print(json.dumps({'prepared':True,'commit':commit,'lanes':list(lanes)}))

def idle_nodes(manifest):
    unit=manifest['lanes']['node']['unit']
    assert show(unit,'ActiveState')=='inactive' and show(unit,'MainPID')=='0','node_not_drained'

def switch_coordinator(item,target):
    # Only accepted coordinator requests drain. Render/upload workers continue
    # heartbeats after a short bounded gate; no global queue quiet period.
    unit=item['unit'];root=Path(item['root']);assert root.is_symlink()
    marker='fb-lease-coordinator-'+str(os.getpid())
    rule=['-p','tcp','--dport','18846','-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
    run(['systemd-run','--quiet','--unit='+marker,'--on-active=75s','--','/usr/sbin/iptables','-D','INPUT',*rule])
    gated=False;temporary=root.with_name(root.name+'.fb-lease-next')
    try:
        run(['iptables','-I','INPUT','1',*rule]);gated=True
        pid=show(unit,'MainPID');deadline=time.monotonic()+30
        while True:
            sockets=[r for r in run(['ss','-Hntp']).splitlines() if ('pid='+pid+',') in r]
            listeners=[r for r in run(['ss','-Hlntp']).splitlines() if ('pid='+pid+',') in r]
            threads=len(list(Path('/proc',pid,'task').iterdir()))
            if not sockets and sum(int(r.split()[1]) for r in listeners)==0 and threads<=2:break
            assert time.monotonic()<deadline,'accepted_coordinator_requests_not_drained'
            time.sleep(.25)
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(target);os.replace(temporary,root)
        run(['systemctl','restart',unit]);assert show(unit,'ActiveState')=='active'
    finally:
        if temporary.is_symlink():temporary.unlink()
        if gated:run(['iptables','-D','INPUT',*rule])
        run(['systemctl','stop',marker+'.timer'])


def apply(folder):
    manifest=json.loads((folder/'manifest.json').read_text());assert manifest['status'] in ('prepared','partial')
    for name,item in manifest['lanes'].items():
        if item.get('applied'):continue
        root=Path(item['root']);assert str(root.resolve())==item['previous'],'release_drift'
        assert unit_files(item['unit'])==item['unit_files'],'unit_drift'
        for relative,r in item['files'].items():
            live=root/relative;assert (sha(live) if live.exists() else '')==r['before'],'live_drift'
            assert sha(Path(item['candidate'])/relative)==r['after'],'candidate_drift'
    # Save before the first switch so rollback is available after an interrupted apply.
    manifest['status']='applying';save(folder/'manifest.json',manifest)
    if 'coordinator' in manifest['lanes'] and not manifest['lanes']['coordinator'].get('applied'):
        item=manifest['lanes']['coordinator'];switch_coordinator(item,item['candidate'])
        item['applied']=True;save(folder/'manifest.json',manifest)
    if show(manifest['lanes']['node']['unit'],'ActiveState')!='inactive':
        manifest['status']='partial';save(folder/'manifest.json',manifest)
        print(json.dumps({'coordinator_applied':manifest['host']=='cpu','node_draining':True}));return
    idle_nodes(manifest)
    item=manifest['lanes']['node'];override=Path(item['override']);override.parent.mkdir(parents=True,exist_ok=True)
    override.write_text('[Service]\nWorkingDirectory='+item['candidate']+'\nExecStart=\nExecStart='+item['python']+' '+item['candidate']+'/scripts/pipeline_worker.py\n');override.chmod(0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','start',item['unit']]);assert show(item['unit'],'ActiveState')=='active'
    manifest['status']='applied';save(folder/'manifest.json',manifest);print(json.dumps({'applied':True,'host':manifest['host'],'commit':manifest['commit']}))

def rollback(folder):
    manifest=json.loads((folder/'manifest.json').read_text());idle_nodes(manifest)
    node=manifest['lanes']['node'];override=Path(node['override'])
    if override.exists():
        assert show(node['unit'],'WorkingDirectory')==node['candidate'],'node_changed_since_deploy'
        override.unlink()
    if 'coordinator' in manifest['lanes']:
        item=manifest['lanes']['coordinator'];root=Path(item['root']);assert str(root.resolve())==item['candidate'],'coordinator_changed_since_deploy'
        switch_coordinator(item,item['previous'])
    run(['systemctl','daemon-reload']);run(['systemctl','start',node['unit']])
    manifest['status']='rolled_back';save(folder/'manifest.json',manifest)
    print(json.dumps({'rolled_back':True,'host':manifest['host'],'business_ledgers_preserved':True}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback']);p.add_argument('--source',type=Path);p.add_argument('--commit');p.add_argument('--folder',type=Path,required=True);p.add_argument('--host',choices=['cpu','hk']);a=p.parse_args()
    if a.action=='prepare':prepare(a.source.resolve(),a.commit,a.folder.resolve(),a.host)
    elif a.action=='apply':apply(a.folder)
    else:rollback(a.folder)

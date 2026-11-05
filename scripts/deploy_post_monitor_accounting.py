"""Scoped GitHub-first overlay; drain coordinator HTTP only, never workers."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

ROOT=Path('/mnt/data-disk/post-monitor-accounting-20261105')
GIT=Path('/mnt/data-disk/pipeline-dispatch/github')
COORD=Path('/opt/pipeline-dispatch/current')
REPORT=Path('/opt/post-daily-report/current')
UNIT='pipeline-coordinator.service'
DROPIN=Path('/etc/systemd/system/post-daily-report.service.d/40-monitor-readonly.conf')
STATIC=[Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')]
ASSETS=['pipeline-monitor.html','pipeline-monitor.css','pipeline-monitor.js']


def run(args,timeout=60,**kwargs):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,**kwargs)
    if result.returncode:raise RuntimeError('command_failed:'+str(args[0])+':'+str(result.returncode))
    return result.stdout.strip()


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def swap(link,target):
    temporary=link.with_name(link.name+'.accounting-next')
    if temporary.exists() or temporary.is_symlink():raise RuntimeError('temporary_link_conflict')
    temporary.symlink_to(target);os.replace(temporary,link)


def atomic_copy(source,target):
    temporary=target.with_name(target.name+'.accounting-next')
    if temporary.exists():raise RuntimeError('temporary_file_conflict')
    shutil.copy2(source,temporary);os.replace(temporary,target)


def service_pids():
    units=[r.split()[0] for r in run(['systemctl','list-units','--type=service','--state=running','--no-legend','--plain']).splitlines()]
    return {u:run(['systemctl','show',u,'-p','MainPID','--value']) for u in units
            if u.startswith(('pipeline-','fb-','tt-','x-','drama-material-api'))}


def handlers(pid):
    sockets=[r for r in run(['ss','-Hntp']).splitlines() if ('pid='+pid+',') in r]
    listeners=[r for r in run(['ss','-Hlntp']).splitlines() if ('pid='+pid+',') in r]
    return len(list(Path('/proc/'+pid+'/task').iterdir())),len(sockets),sum(int(r.split()[1]) for r in listeners)


def switch_coordinator(target,record):
    previous=str(COORD.resolve());pid=run(['systemctl','show',UNIT,'-p','MainPID','--value'])
    marker='post-monitor-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S%f')
    rule=['-p','tcp','--dport','18846','-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
    watchdog=marker+'-watchdog';gated=False;stopped=False;changed=False;proof=[];began=time.monotonic()
    run(['systemd-run','--quiet','--unit='+watchdog,'--on-active=75s','--','/usr/sbin/iptables','-D','INPUT']+rule)
    try:
        run(['iptables','-I','INPUT','1']+rule);gated=True;deadline=time.monotonic()+25
        while True:
            state=handlers(pid);proof.append(state)
            if state==(2,0,0):
                time.sleep(.25)
                if handlers(pid)==state:break
            if time.monotonic()>deadline:raise RuntimeError('control_requests_not_drained')
            time.sleep(.5)
        run(['systemctl','stop',UNIT],timeout=30);stopped=True
        assert run(['systemctl','show',UNIT,'-p','MainPID','--value'])=='0'
        assert str(COORD.resolve())==previous,'concurrent_runtime_switch'
        swap(COORD,target);changed=True
        run(['systemctl','start',UNIT],timeout=15)
        assert run(['systemctl','is-active',UNIT])=='active'
    except BaseException:
        if changed:
            run(['systemctl','stop',UNIT],timeout=30);swap(COORD,previous)
        if stopped:run(['systemctl','start',UNIT],timeout=15)
        raise
    finally:
        if gated:run(['iptables','-D','INPUT']+rule)
        subprocess.run(['systemctl','stop',watchdog+'.timer'],capture_output=True,timeout=10)
        record.write_text(json.dumps({'http_drain':proof,'gate_seconds':time.monotonic()-began,'rule_removed':marker not in run(['iptables','-S','INPUT'])},indent=2))


def health():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));deadline=time.monotonic()+12
    while True:
        try:
            with opener.open('http://127.0.0.1:18846/health',timeout=2) as r:
                if r.status==200:return
        except OSError:pass
        if time.monotonic()>deadline:raise RuntimeError('coordinator_health_failed')
        time.sleep(.25)


def rollback(manifest):
    m=json.loads(manifest.read_text());backup=manifest.parent
    # Never roll back a later deployment, nor any ledger/receipt/config facts.
    if str(COORD.resolve())==m['new_coordinator']:
        switch_coordinator(m['old_coordinator'],backup/'rollback-drain.json');health()
    elif str(COORD.resolve())!=m['old_coordinator']:raise RuntimeError('later_coordinator_deployment_preserved')
    if str(REPORT.resolve())==m['new_report']:swap(REPORT,m['old_report'])
    elif str(REPORT.resolve())!=m['old_report']:raise RuntimeError('later_report_deployment_preserved')
    for entry in m['static']:
        path=Path(entry['path'])
        if digest(path)==entry['new_sha256']:atomic_copy(backup/entry['backup'],path)
        elif digest(path)!=entry['old_sha256']:raise RuntimeError('later_static_change_preserved')
    if DROPIN.exists() and digest(DROPIN)==m.get('dropin_sha256'):
        if m['had_dropin']:atomic_copy(backup/'old-dropin.conf',DROPIN)
        else:DROPIN.unlink()
    run(['systemctl','daemon-reload'])
    print(json.dumps({'status':'rolled_back','manifest':str(manifest)}))


def deploy(sha,baseline):
    assert re.fullmatch('[0-9a-f]{40}',sha)
    assert run(['findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    env=dict(os.environ,GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    run(['git','-C',str(GIT),'fetch','origin',sha],env=env)
    assert run(['git','-C',str(GIT),'rev-parse','FETCH_HEAD'])==sha
    expected=json.loads(baseline.read_text(encoding='utf-8-sig'))['hashes']
    for path,value in expected.items():assert digest(path)==value,'affected_file_changed:'+path
    assert run(['systemctl','show','post-daily-report.service','-p','ActiveState','--value'])=='inactive','report_currently_running'
    assert run(['systemctl','is-active','post-daily-report.timer'])=='active'
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/('deployment-'+tag);backup.mkdir(parents=True,mode=0o700)
    source=backup/'github';source.mkdir()
    archive=subprocess.run(['git','-C',str(GIT),'archive',sha],capture_output=True,check=True).stdout
    subprocess.run(['tar','-x','-C',str(source)],input=archive,check=True)
    old_coord,old_report=str(COORD.resolve()),str(REPORT.resolve())
    new_coord,new_report=backup/'coordinator',backup/'report'
    ignore=shutil.ignore_patterns('__pycache__','.git')
    shutil.copytree(old_coord,new_coord,symlinks=True,ignore=ignore)
    shutil.copytree(old_report,new_report,symlinks=True,ignore=ignore)
    for runtime in (new_coord,new_report):
        for relative in ('features','features/post_daily_report','features/pipeline_dispatch','scripts'):
            assert not (runtime/relative).is_symlink(),'overlay_must_not_write_through_runtime_symlink'
    # Deliberately do not copy the Git checkout's execution/service/core code.
    for runtime in (new_coord,new_report):
        package=runtime/'features/post_daily_report';package.mkdir(parents=True,exist_ok=True)
        for p in (source/'features/post_daily_report').iterdir():
            if p.is_file():shutil.copy2(p,package/p.name)
    for name in ('views.py','queue_states.py'):
        shutil.copy2(source/'features/pipeline_dispatch'/name,new_coord/'features/pipeline_dispatch'/name)
    shutil.copy2(source/'scripts/post_daily_report.py',new_report/'scripts/post_daily_report.py')
    before=service_pids()
    manifest={'commit':sha,'old_coordinator':old_coord,'new_coordinator':str(new_coord),'old_report':old_report,'new_report':str(new_report),'static':[], 'had_dropin':DROPIN.exists(),'service_pids_before':before}
    for index,root in enumerate(STATIC):
        for name in ASSETS:
            saved=f'static-{index}-{name}';shutil.copy2(root/name,backup/saved)
            manifest['static'].append({'path':str(root/name),'backup':saved,'old_sha256':digest(root/name),'new_sha256':digest(source/'static'/name)})
    if DROPIN.exists():shutil.copy2(DROPIN,backup/'old-dropin.conf')
    dropin='[Service]\nReadWritePaths=-/mnt/data-disk/pipeline-dispatch\nReadOnlyPaths=-/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3\n'
    manifest['dropin_sha256']=hashlib.sha256(dropin.encode()).hexdigest()
    path=backup/'manifest.json';path.write_text(json.dumps(manifest,indent=2))
    command=f"#!/bin/sh\nset -eu\nexec /usr/bin/python3 '{source}/scripts/deploy_post_monitor_accounting.py' --rollback '{path}'\n"
    (backup/'rollback.sh').write_text(command);(backup/'rollback.sh').chmod(0o700)
    run(['/opt/pipeline-dispatch/coordinator-venv/bin/python','-B','-c','from features.pipeline_dispatch import views; from features.post_daily_report.report import collect_report'],cwd=new_coord)
    for path_,value in expected.items():assert digest(path_)==value,'affected_file_changed_before_switch:'+path_
    assert str(COORD.resolve())==old_coord and str(REPORT.resolve())==old_report,'concurrent_release_preserved'
    try:
        switch_coordinator(str(new_coord),backup/'http-drain.json');health()
        assert str(REPORT.resolve())==old_report
        swap(REPORT,str(new_report))
        DROPIN.parent.mkdir(parents=True,exist_ok=True);DROPIN.write_text(dropin)
        run(['systemctl','daemon-reload'])
        for entry in manifest['static']:
            target=Path(entry['path']);assert digest(target)==entry['old_sha256'],'concurrent_static_change'
            atomic_copy(source/'static'/target.name,target)
    except BaseException:
        rollback(path);raise
    after=service_pids()
    result={'status':'deployed','commit':sha,'backup':str(backup),'manifest':str(path),'rollback':str(backup/'rollback.sh'),
            'service_pids_before':before,'service_pids_after':after,'other_services_unchanged':all(after.get(k)==v for k,v in before.items() if k!=UNIT),
            'timer_active':run(['systemctl','is-active','post-daily-report.timer']),
            'static_verified':all(digest(e['path'])==e['new_sha256'] for e in manifest['static'])}
    (backup/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--commit');parser.add_argument('--baseline',type=Path);parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    if args.rollback:rollback(args.rollback)
    else:deploy(args.commit,args.baseline)

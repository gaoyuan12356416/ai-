"""Scoped gateway/static deployment. Existing Tag claims drain without a restart."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
POINTER=Path('/opt/x-tag-publisher/current')
GATEWAY='x-tag-gateway.service'
OWNER='x-tag-post.service'
BASE='defd5a8851a587b2a6c5b9177f8613f55b33299c57f9d983967031e352e085bf'
FILES=['features/x_tag_publish/gateway.py','features/x_tag_publish/run_control.py',
       'static/x-tag-auto-publish-runs.html','static/x-tag-auto-publish-runs.js']


def run(*args):return subprocess.check_output(args,text=True).strip()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
def state(unit):return dict(line.split('=',1) for line in run('systemctl','show',unit,'-p','MainPID','-p','ActiveState').splitlines())
def save(folder,record):
    tmp=folder/'record.tmp';tmp.write_text(json.dumps(record,indent=2)+'\n');tmp.replace(folder/'record.json')
def ready(seconds=30):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8898/healthz',timeout=2) as response:
                if response.status==200:return
        except OSError:pass
        time.sleep(.5)
    raise RuntimeError('gateway_not_ready')


def resume_downloads(paused):
    for pid,start in paused:
        try:
            if Path('/proc/%d/stat'%pid).read_text().split()[21]==start:
                os.kill(pid,signal.SIGCONT)
        except ProcessLookupError:pass
        except FileNotFoundError:pass


def suspend_downloads(jobs):
    """Pause only local isolated GET workers; preserve TCP, memory and leases.

    Native intents are checked again after the process is stopped, closing the
    race between its last heartbeat and an upload. Any upload keeps draining.
    """
    paused=[]
    try:
        with closing(sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro',uri=True)) as c, \
             closing(sqlite3.connect('file:/mnt/data-disk/x-tag-publisher/posts.sqlite3?mode=ro',uri=True)) as p:
            c.row_factory=sqlite3.Row
            for job in jobs:
                row=c.execute('SELECT j.*,w.pid,w.observation_json FROM pipeline_job j JOIN pipeline_worker w '
                              'ON w.node_id=j.node_id AND w.worker_id=j.worker_id WHERE j.id=?',(job['id'],)).fetchone()
                if (not row or row['node_id']!='cpu-main' or row['external_intent']
                    or json.loads(row['observation_json']).get('phase')!='download'
                    or (datetime.fromisoformat(row['lease_expires_at'].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()<35):
                    raise RuntimeError('not_an_unstarted_local_download')
                pid=int(row['pid']);proc=Path('/proc/%d'%pid)
                command=(proc/'cmdline').read_bytes()
                if pid<=1 or b'multiprocessing.spawn' not in command or b'--multiprocessing-fork' not in command:
                    raise RuntimeError('not_an_isolated_worker')
                if c.execute('SELECT count(*) FROM pipeline_worker WHERE pid=?',(pid,)).fetchone()[0]!=1:
                    raise RuntimeError('worker_not_isolated')
                if not any(str(fd.resolve()).endswith('/source.download.tmp') for fd in (proc/'fd').iterdir()):
                    raise RuntimeError('download_file_not_open')
                start=(proc/'stat').read_text().split()[21]
                os.kill(pid,signal.SIGSTOP);paused.append((pid,start))
                for _ in range(50):
                    if (proc/'stat').read_text().split()[2]=='T':break
                    time.sleep(.01)
                else:raise RuntimeError('worker_not_suspended')
                log=p.execute('SELECT status,attempt_count,unknown_outcome,x_post_id FROM x_post_publish_log WHERE queue_id=?',
                              (job['source_id'],)).fetchone()
                if log!=('reserved',0,0,'') or p.execute('SELECT 1 FROM pipeline_x_operation WHERE source_key=? LIMIT 1',
                                                       ('x:queue:'+str(job['source_id']),)).fetchone():
                    raise RuntimeError('native_write_already_started')
            return paused
    except BaseException as exc:
        resume_downloads(paused)
        if isinstance(exc,(OSError,ValueError,RuntimeError,sqlite3.Error)):
            return []
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('apply','rollback'))
    parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();folder=args.directory.resolve()
    if not str(folder).startswith('/mnt/data-disk/x-tag-run-terminate-20261117/'):
        raise RuntimeError('invalid_deployment_directory')
    if run('findmnt','-n','-o','UUID','--target','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_mount_invalid')
    rollback=args.action=='rollback'
    live=POINTER.resolve()
    if rollback:
        record=json.loads((folder/'record.json').read_text())
        if str(live)!=record['runtime']:raise RuntimeError('runtime_changed')
        with closing(sqlite3.connect('file:/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3?mode=ro',uri=True)) as c:
            if c.execute("SELECT 1 FROM x_auto_task t JOIN x_auto_run r ON r.id=t.run_id WHERE r.status='canceled' "
                         "AND t.status NOT IN ('canceled','published','failed','skipped','no_candidate') LIMIT 1").fetchone():
                raise RuntimeError('termination_receipts_still_pending_keep_gateway_guard')
        for entry in record['files']:
            if digest(Path(entry['path']))!=entry['after']:raise RuntimeError('concurrent_file_change:'+entry['path'])
    else:
        if digest(live/FILES[0])!=BASE:raise RuntimeError('gateway_baseline_changed')
        folder.mkdir(parents=True,exist_ok=False)
        record={'runtime':str(live),'source':str(ROOT),'revision':run('git','-C',str(ROOT),'rev-parse','HEAD'),
                'started_at':datetime.now(timezone.utc).isoformat(),'state':'prepared','files':[]}
        for root in (live,Path('/root/drama_material_service')):
            for name in FILES:
                dest=root/name;backup=folder/'files'/str(dest).lstrip('/')
                before=digest(dest)
                if before:
                    backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,backup)
                record['files'].append(dict(path=str(dest),source=name,before=before,after=digest(ROOT/name),backup=str(backup)))
        for name in FILES[2:]:
            dest=Path('/usr/share/nginx/html')/Path(name).name;backup=folder/'files'/str(dest).lstrip('/')
            backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,backup)
            record['files'].append(dict(path=str(dest),source=name,before=digest(dest),after=digest(ROOT/name),backup=str(backup)))
        record['services_before']={u:state(u) for u in (OWNER,'x-tag-auto.service','pipeline-coordinator.service',GATEWAY)}
        save(folder,record)
    # Use the installed admission barrier, which preserves every existing
    # segmented upload and scopes new admissions to this Tag lane only.
    barrier=runpy.run_path('/opt/x-upload-continuity/current/scripts/pipeline_service_drain.py')
    hold=barrier['DIRECTORY']/(OWNER+'.json')
    if hold.exists():raise RuntimeError('another_deployment_owns_tag_hold')
    barrier['write_hold'](OWNER)
    paused=[]
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(SystemExit('deployment_stopped')))
    try:
        record['state']='draining';save(folder,record)
        previous=None
        while True:
            # Only publication calls the gateway's origin/attribution brokers.
            # Existing preparation and Auto selection use the still-running
            # native services and need not drain for a gateway-only switch.
            jobs=[job for job in barrier['pending'](OWNER) if job['stage']=='publish']
            signature=[(j['id'],j['generation']) for j in jobs]
            if signature!=previous:print(json.dumps({'pending':jobs}),flush=True);previous=signature
            if not jobs:break
            paused=suspend_downloads(jobs)
            if paused:
                record['paused_download_pids']=[pid for pid,_ in paused];save(folder,record)
                print(json.dumps({'temporarily_paused_download_pids':record['paused_download_pids']}),flush=True)
                break
            time.sleep(2)
        if POINTER.resolve()!=live:raise RuntimeError('runtime_changed_during_drain')
        for entry in record['files']:
            if digest(Path(entry['path']))!=entry['after' if rollback else 'before']:
                raise RuntimeError('concurrent_file_change:'+entry['path'])
        # Drain existing gateway RPCs too. Native uploads are already finished.
        connection_deadline=time.monotonic()+5
        while run('bash','-c',"ss -Htn state established '( sport = :8898 )' | wc -l")!='0':
            if paused and time.monotonic()>connection_deadline:raise RuntimeError('gateway_requests_still_active')
            time.sleep(.2)
        for entry in record['files']:
            dest=Path(entry['path']);dest.parent.mkdir(parents=True,exist_ok=True)
            source=Path(entry['backup']) if rollback else ROOT/entry['source']
            if rollback and not entry['before']:
                dest.unlink(missing_ok=True)
            else:
                temp=dest.with_name(dest.name+'.terminate-new');shutil.copy2(source,temp);temp.chmod(0o644);temp.replace(dest)
        run('systemctl','restart',GATEWAY)
        ready(10 if paused else 30)
        resume_downloads(paused);paused=[]
        record['state']='rolled_back' if rollback else 'active'
        record['services_after']={u:state(u) for u in record['services_before']}
        for unit in (OWNER,'x-tag-auto.service','pipeline-coordinator.service'):
            if record['services_after'][unit]!=record['services_before'][unit]:
                record.setdefault('concurrent_service_changes',[]).append(unit)
        record['finished_at']=datetime.now(timezone.utc).isoformat();save(folder,record)
        print(json.dumps({'state':record['state'],'revision':record['revision'],'services':record['services_after']}),flush=True)
    finally:
        resume_downloads(paused)
        # A failed gateway start must keep new upload admissions held.
        ready()
        barrier['release'](OWNER)


if __name__=='__main__':main()

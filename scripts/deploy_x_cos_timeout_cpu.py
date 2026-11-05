"""Update one verified X media module; drain preparation workers gracefully.

No unit restart, publication worker signal, queue reset or ledger rollback.
The supervisor's spawn replacements import the new module after their original
work finishes. SIGTERM is verified here as the worker's cooperative drain.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

BEFORE='ee834fae0045213c817710baf504901a20bf0573aee5d2b4169d18a61eceb7a7'
RELATIVE='features/x_posts/media_repair.py'

def digest(data):return hashlib.sha256(data).hexdigest()
def require(value,code):
    if not value:raise RuntimeError(code)

def deploy(repository,folder):
    repository=Path(repository).resolve();folder=Path(folder)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True).strip()
    new=subprocess.check_output(['git','show',commit+':'+RELATIVE],cwd=repository)
    require(b'COS_HTTP_TIMEOUT_SECONDS = 120' in new and b'Timeout=COS_HTTP_TIMEOUT_SECONDS' in new,'committed_patch_missing')
    pid=int(subprocess.check_output(['systemctl','show','pipeline-node-cpu-main.service','-p','MainPID','--value'],text=True))
    root=(Path('/proc')/str(pid)/'cwd').resolve();target=root/RELATIVE
    require(str(root)=='/mnt/data-disk/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/node','runtime_changed')
    worker=(root/'features/pipeline_dispatch/worker.py').read_bytes()
    require(b'signal.signal(sig, lambda *_: stopping.set())' in worker and b'while not stopping.is_set():' in worker and b'multiprocessing.get_context("spawn")' in worker,'cooperative_spawn_drain_not_verified')
    old=target.read_bytes();require(digest(old)==BEFORE,'module_changed_or_already_deployed')
    folder.mkdir(mode=0o700,parents=True,exist_ok=False)
    (folder/'media_repair.py.before').write_bytes(old)
    with sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        candidates=[dict(r) for r in c.execute("SELECT w.worker_id,w.pid FROM pipeline_worker w WHERE w.node_id='cpu-main' AND w.stage='prepare' AND w.draining=0 AND NOT EXISTS(SELECT 1 FROM pipeline_job j WHERE j.worker_id=w.worker_id AND j.node_id=w.node_id AND j.status='running' AND j.channel<>'x')")]
    children=[]
    for child in candidates:
        proc=Path('/proc')/str(child['pid'])
        try:stat=(proc/'stat').read_text().split()
        except FileNotFoundError:continue
        if int(stat[3])==pid:children.append(dict(child,start_ticks=stat[21]))
    manifest=dict(commit=commit,runtime=str(root),file=RELATIVE,before=BEFORE,after=digest(new),node_pid=pid,drain_candidates=children,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),queues_reset=False,publication_workers_signaled=False)
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
    temp=target.with_name(target.name+'.x-cos-timeout-next');require(not temp.exists(),'temporary_file_exists')
    st=target.stat()
    with temp.open('xb') as stream:stream.write(new);stream.flush();os.fsync(stream.fileno())
    os.chmod(temp,st.st_mode);os.chown(temp,st.st_uid,st.st_gid)
    require(target.read_bytes()==old,'module_changed_during_backup');os.replace(temp,target)
    subprocess.run([sys.executable,'-c',"import pathlib;compile(pathlib.Path('"+RELATIVE+"').read_text(),'media_repair.py','exec')"],cwd=root,check=True)
    drained=[]
    for child in children:
        try:
            stat=(Path('/proc')/str(child['pid'])/'stat').read_text().split()
            if int(stat[3])==pid and stat[21]==child['start_ticks']:
                os.kill(child['pid'],signal.SIGTERM);drained.append(child)
        except ProcessLookupError:pass
        except FileNotFoundError:pass
    manifest.update(drained=drained,status='staged_with_cooperative_worker_drain')
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest

def rollback(folder):
    folder=Path(folder);manifest=json.loads((folder/'manifest.json').read_text())
    pid=int(subprocess.check_output(['systemctl','show','pipeline-node-cpu-main.service','-p','MainPID','--value'],text=True))
    root=(Path('/proc')/str(pid)/'cwd').resolve();require(str(root)==manifest['runtime'],'runtime_changed')
    target=root/manifest['file'];old=(folder/'media_repair.py.before').read_bytes()
    require(digest(old)==manifest['before'] and digest(target.read_bytes())==manifest['after'],'module_changed_do_not_overwrite')
    worker=(root/'features/pipeline_dispatch/worker.py').read_bytes()
    require(b'signal.signal(sig, lambda *_: stopping.set())' in worker and b'multiprocessing.get_context("spawn")' in worker,'cooperative_drain_not_verified')
    temp=target.with_name(target.name+'.x-cos-timeout-rollback');require(not temp.exists(),'rollback_temporary_file_exists')
    st=target.stat();temp.write_bytes(old);os.chmod(temp,st.st_mode);os.chown(temp,st.st_uid,st.st_gid);os.replace(temp,target)
    drained=[]
    with sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro',uri=True) as c:
        candidates=c.execute("SELECT w.pid FROM pipeline_worker w WHERE w.node_id='cpu-main' AND w.stage='prepare' AND w.draining=0 AND NOT EXISTS(SELECT 1 FROM pipeline_job j WHERE j.node_id=w.node_id AND j.worker_id=w.worker_id AND j.status='running' AND j.channel<>'x')").fetchall()
    for (child,) in candidates:
        try:
            stat=(Path('/proc')/str(child)/'stat').read_text().split()
            if int(stat[3])==pid:os.kill(child,signal.SIGTERM);drained.append(child)
        except (FileNotFoundError,ProcessLookupError):pass
    result=dict(status='code_rollback_staged',drained=drained,ledgers_restored=False)
    (folder/'rollback.json').write_text(json.dumps(result,indent=2));return result

if __name__=='__main__':
    print(json.dumps(rollback(sys.argv[2]) if sys.argv[1]=='--rollback' else deploy(sys.argv[1],sys.argv[2])))

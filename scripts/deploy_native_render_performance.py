"""Activate only future native-render subprocesses; never signal any service.

Install additive modules and backward-compatible helpers first, then atomically
replace native_pipeline.py. A running render has already imported its helpers
and retains its CUDA modules. Rollback restores only that entry point, leaving
compatible dependencies available to already-started new renders.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BASE = '68407465a58d99f9dd2c9a14da000f167115f804'
PREFIX = 'features/random_gpu/'
SUPPORT = ['cpu_mux.py','native_resize.py','native_resize.cu','gpu_mux.py','native_video.py']
ENTRY = 'native_pipeline.py'


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args])


def digest(data):return hashlib.sha256(data).hexdigest()


def atomic(path,data):
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            os.fchmod(handle.fileno(),0o644)
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def processes(runtime):
    rows=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            args=(p/'cmdline').read_bytes().split(b'\0')
            if not any(b'pipeline_worker.py' in a or b'features.random_gpu.native_pipeline'==a for a in args):continue
            if Path(os.readlink(p/'cwd')).resolve()!=runtime:continue
            rows.append(dict(pid=int(p.name),start_ticks=(p/'stat').read_text().split()[21]))
        except (OSError,ProcessLookupError):pass
    return rows


def apply(source,runtime,backup):
    sha=git(source,'rev-parse','HEAD').decode().strip()
    if git(source,'status','--porcelain').strip():raise ValueError('source_not_clean')
    remote=git(source,'remote','get-url','origin').decode().strip()
    if remote not in ('https://github.com/gaoyuan12356416/ai-.git','git@github.com:gaoyuan12356416/ai-.git'):
        raise ValueError('source_not_github_repo')
    # An origin ref fetched on this server must contain the staged revision.
    if not git(source,'branch','-r','--contains',sha).strip():raise ValueError('source_not_fetched_from_remote')
    files={}
    for name in SUPPORT+[ENTRY]:
        rel=PREFIX+name;target=runtime/rel;new=(source/rel).read_bytes()
        if target.is_symlink():raise ValueError('runtime_module_is_symlink')
        old=target.read_bytes() if target.exists() else None
        baseline=subprocess.run(['git','-C',str(source),'show',BASE+':'+rel],capture_output=True)
        expected=baseline.stdout if baseline.returncode==0 else None
        if old!=expected:raise ValueError('runtime_module_changed:'+name)
        files[name]={'before':digest(old) if old is not None else None,'after':digest(new)}
    backup.mkdir(parents=True,exist_ok=False)
    for name in SUPPORT+[ENTRY]:
        path=runtime/PREFIX/name
        if path.exists():(backup/name).write_bytes(path.read_bytes())
    record=dict(source=str(source),runtime=str(runtime),commit=sha,base=BASE,files=files,
                started_utc=datetime.now(timezone.utc).isoformat(),processes_before=processes(runtime),status='backed_up')
    manifest=backup/'manifest.json';manifest.write_text(json.dumps(record,indent=2))
    for name in SUPPORT+[ENTRY]:
        atomic(runtime/PREFIX/name,(source/PREFIX/name).read_bytes())
    for name in files:
        if digest((runtime/PREFIX/name).read_bytes())!=files[name]['after']:raise ValueError('installed_hash_mismatch')
    record.update(status='active_for_new_subprocesses',processes_after=processes(runtime),
                  activated_utc=datetime.now(timezone.utc).isoformat())
    manifest.write_text(json.dumps(record,indent=2));print(json.dumps(record,indent=2))


def rollback(backup):
    manifest=backup/'manifest.json';record=json.loads(manifest.read_text())
    target=Path(record['runtime'])/PREFIX/ENTRY
    if digest(target.read_bytes())!=record['files'][ENTRY]['after']:
        raise ValueError('entry_changed_since_deployment')
    old=(backup/ENTRY).read_bytes()
    if digest(old)!=record['files'][ENTRY]['before']:raise ValueError('backup_hash_mismatch')
    atomic(target,old)
    record.update(status='rolled_back_for_new_subprocesses',rollback_utc=datetime.now(timezone.utc).isoformat())
    manifest.write_text(json.dumps(record,indent=2));print(json.dumps(record,indent=2))


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['apply','rollback'])
    p.add_argument('--source',type=Path);p.add_argument('--runtime',type=Path);p.add_argument('--backup',type=Path,required=True)
    a=p.parse_args()
    if a.action=='apply':apply(a.source.resolve(),a.runtime.resolve(),a.backup.resolve())
    else:rollback(a.backup.resolve())


if __name__=='__main__':main()

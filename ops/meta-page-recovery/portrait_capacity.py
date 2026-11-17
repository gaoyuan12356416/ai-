"""Scoped capacity for the observed 3995-Page portrait collection; no restart."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

UNIT='fb-page-portrait-refresh.service'
DROP=Path('/etc/systemd/system/fb-page-portrait-refresh.service.d/meta-page-recovery-capacity.conf')
RUNTIME=Path('/run/systemd/system.control/fb-page-portrait-refresh.service.d/50-MemoryMax.conf')
BODY='[Service]\nMemoryMax=3G\nTimeoutStartSec=2h\n'


def run(*args):return subprocess.check_output(args,text=True).strip()


def apply(receipt,commit):
    source=Path(__file__).resolve().parents[2]
    if run('git','-C',str(source),'rev-parse','HEAD')!=commit:raise RuntimeError('source_commit_mismatch')
    if not run('git','-C',str(source),'ls-remote','origin','refs/heads/codex/meta-page-recovery-20261117').startswith(commit+'\t'):
        raise RuntimeError('github_version_mismatch')
    if receipt.exists():raise RuntimeError('capacity_receipt_exists')
    receipt.mkdir(parents=True,mode=0o700)
    previous=DROP.read_text() if DROP.exists() else None
    state=dict(commit=commit,previous=previous,body=BODY)
    (receipt/'manifest.json').write_text(json.dumps(state,indent=2))
    shutil.copy2(__file__,receipt/'portrait_capacity.py')
    DROP.parent.mkdir(parents=True,exist_ok=True);DROP.write_text(BODY)
    subprocess.check_call(['systemctl','daemon-reload'])
    # set-property changes the existing cgroup without restarting its owner.
    # Its runtime overlay is separately backed up and restored after collection.
    state['runtime_previous']=RUNTIME.read_text() if RUNTIME.exists() else None
    (receipt/'manifest.json').write_text(json.dumps(state,indent=2))
    subprocess.check_call(['systemctl','set-property','--runtime',UNIT,'MemoryMax=3G'])
    state['runtime_sha256']=hashlib.sha256(RUNTIME.read_bytes()).hexdigest() if RUNTIME.exists() else None
    (receipt/'manifest.json').write_text(json.dumps(state,indent=2))
    print(run('systemctl','show',UNIT,'-p','MemoryMax','-p','MemoryCurrent','-p','TimeoutStartUSec','-p','MainPID'))


def cleanup_runtime(receipt):
    state=json.loads((receipt/'manifest.json').read_text())
    if RUNTIME.exists() and state.get('runtime_sha256'):
        if hashlib.sha256(RUNTIME.read_bytes()).hexdigest()!=state['runtime_sha256']:raise RuntimeError('runtime_capacity_changed')
        if state['runtime_previous'] is None:RUNTIME.unlink()
        else:RUNTIME.write_text(state['runtime_previous'])
        subprocess.check_call(['systemctl','daemon-reload'])


def rollback(receipt):
    # Only this affected oneshot drains. Never terminate a collection or restore
    # old profiles/configuration/receipts to make rollback appear complete.
    while run('systemctl','show',UNIT,'-p','ActiveState','--value') in {'active','activating','deactivating'}:time.sleep(5)
    state=json.loads((receipt/'manifest.json').read_text())
    if DROP.exists() and DROP.read_text()!=state['body']:raise RuntimeError('capacity_dropin_changed')
    cleanup_runtime(receipt)
    if state['previous'] is None:
        if DROP.exists():DROP.unlink()
    else:DROP.write_text(state['previous'])
    subprocess.check_call(['systemctl','daemon-reload'])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['apply','cleanup-runtime','rollback'])
    parser.add_argument('--receipt',required=True,type=Path);parser.add_argument('--commit');args=parser.parse_args()
    if args.command=='apply':apply(args.receipt,args.commit)
    elif args.command=='cleanup-runtime':cleanup_runtime(args.receipt)
    else:rollback(args.receipt)

#!/usr/bin/env python3
"""Repair only the read-only portrait API and its loading/error UI."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

SOURCE=Path(__file__).resolve().parents[1]
ROOT=Path('/mnt/data-disk/x-channel-tag-revenue')
MAIN=Path('/root/drama_material_service')
WEB=Path('/usr/share/nginx/html')
BACKUPS=ROOT/'reader-fix-backups'
DROPIN=Path('/etc/systemd/system/x-channel-tag-revenue.service.d/portrait-memory.conf')
UNIT='x-channel-tag-revenue.service'


def cmd(*args,timeout=90):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout).stdout.strip()


def sha(data):return hashlib.sha256(data).hexdigest()


def file_sha(path):return sha(path.read_bytes()) if path.is_file() else None


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.portrait-next');temp.write_bytes(data)
    temp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644);temp.replace(path)


def save(path,data):atomic(path,json.dumps(data,indent=2).encode())


def parents():
    return cmd('systemctl','show','drama-material-api.service','x-post-automation.service',
               'x-auto-post-service.service','x-tag-post.service','x-tag-auto.service','x-tag-gateway.service',
               '-p','Id,MainPID,ActiveState')


def restart():
    cmd('systemctl','daemon-reload')
    cmd('systemctl','restart',UNIT)
    for _ in range(10):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8896/healthz',timeout=3) as response:
                if response.status==200:return
        except OSError:pass
        time.sleep(.5)
    raise RuntimeError('portrait_api_unhealthy')


def rollback(backup):
    backup=Path(backup).resolve()
    if backup.parent!=BACKUPS.resolve():raise RuntimeError('invalid_backup')
    info=json.loads((backup/'deployment.json').read_text())
    for name,row in info['files'].items():
        path=Path(name)
        if file_sha(path)==row['before']:continue
        if file_sha(path)!=row['after']:raise RuntimeError('later_change:'+name)
        if row['before'] is None:path.unlink()
        else:atomic(path,Path(row['backup']).read_bytes())
    restart()
    save(backup/'rollback.json',dict(status='code_only',business_data_preserved=True))
    print(json.dumps(dict(status='rolled_back',backup=str(backup))))


def install():
    if cmd('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('data_disk_missing')
    commit=cmd('git','-C',str(SOURCE),'rev-parse','HEAD')
    if cmd('git','-C',str(SOURCE),'status','--porcelain') or not cmd('git','-C',str(SOURCE),'branch','-r','--contains',commit):raise RuntimeError('verified_github_commit_required')
    targets={}
    for relative,destinations in [
        ('features/x_tag_revenue/routes.py',[MAIN/'features/x_tag_revenue/routes.py']),
        ('static/x-tag-revenue.js',[MAIN/'static/x-tag-revenue.js',WEB/'x-tag-revenue.js'])]:
        data=(SOURCE/relative).read_bytes()
        previous=subprocess.run(['git','-C',str(SOURCE),'show','HEAD^:'+relative],check=True,capture_output=True).stdout
        for target in destinations:
            if file_sha(target) not in (sha(previous),sha(data)):raise RuntimeError('concurrent_file_change:'+str(target))
            targets[target]=data
    dropin=(SOURCE/'deploy/x-channel-tag-revenue-memory.conf').read_bytes()
    if DROPIN.exists() and DROPIN.read_bytes()!=dropin:raise RuntimeError('concurrent_memory_configuration')
    targets[DROPIN]=dropin
    version=sha((SOURCE/'static/x-tag-revenue.js').read_bytes())[:12]
    for path in [MAIN/'static/x-account-list.html',WEB/'x-account-list.html']:
        data,count=re.subn(r'(/x-tag-revenue\.js)(?:\?[^"\']*)?',r'\1?v='+version,path.read_text())
        if count!=1:raise RuntimeError('portrait_script_reference_changed')
        targets[path]=data.encode()
    backup=BACKUPS/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');backup.mkdir(parents=True,mode=0o700)
    info=dict(status='prepared',commit=commit,backup=str(backup),files={},parents_before=parents())
    for i,(path,data) in enumerate(targets.items()):
        saved=backup/str(i)
        if path.exists():shutil.copy2(path,saved)
        info['files'][str(path)]=dict(before=file_sha(path),after=sha(data),backup=str(saved))
    save(backup/'deployment.json',info)
    try:
        for path,data in targets.items():
            if file_sha(path)!=info['files'][str(path)]['before']:raise RuntimeError('concurrent_file_change:'+str(path))
            atomic(path,data)
        restart()
        info.update(status='active',parents_after=parents(),activated_at=datetime.now(timezone.utc).isoformat())
        info['publishing_parents_unchanged']=info['parents_before']==info['parents_after']
        info['memory_max']=cmd('systemctl','show',UNIT,'-p','MemoryMax','--value')
        save(backup/'deployment.json',info)
    except Exception:
        rollback(backup)
        raise
    print(json.dumps({k:info[k] for k in ('status','commit','backup','publishing_parents_unchanged','memory_max')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('install','rollback'));parser.add_argument('--backup');args=parser.parse_args()
    install() if args.action=='install' else rollback(args.backup)

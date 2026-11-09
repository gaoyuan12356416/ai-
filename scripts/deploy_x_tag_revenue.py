#!/usr/bin/env python3
"""Scoped GitHub-first install/rollback; publishing services and ledgers are untouched."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from features.x_tag_revenue.integration import patch_html, unpatch_html

ROOT = Path('/mnt/data-disk/x-channel-tag-revenue')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
UNITS = ('x-channel-tag-revenue-refresh.service', 'x-channel-tag-revenue-refresh.timer', 'x-channel-tag-revenue.service')
MODULES = ['features/x_tag_revenue/' + name + '.py' for name in ('__init__','core','refresh','routes','http_server','integration')]
ASSETS = ['static/x-tag-revenue.css','static/x-tag-revenue.js']


def command(*args, timeout=90, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('command_failed:' + args[0] + ':' + result.stderr[-500:])
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def atomic_bytes(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tag-revenue-next')
    tmp.write_bytes(data)
    if path.exists(): shutil.copymode(path,tmp)
    else: tmp.chmod(0o644)
    tmp.replace(path)


def targets():
    result={str(MAIN/name):(SOURCE/name).read_bytes() for name in MODULES+ASSETS}
    result.update({str(WEB/Path(name).name):(SOURCE/name).read_bytes() for name in ASSETS})
    result.update({str(Path('/etc/systemd/system')/name):(SOURCE/'deploy'/name).read_bytes() for name in UNITS})
    result['/etc/nginx/default.d/x-channel-tag-revenue.conf']=(SOURCE/'deploy/nginx/x-channel-tag-revenue.conf').read_bytes()
    for path,patch in [(MAIN/'static/x-account-list.html',patch_html),(WEB/'x-account-list.html',patch_html)]:
        result[str(path)]=patch(path.read_text(encoding='utf-8')).encode('utf-8')
    return result


def health():
    for _ in range(20):
        import urllib.request
        try:
            with urllib.request.urlopen('http://127.0.0.1:8896/healthz',timeout=3) as response:
                if response.status == 200: return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('api_health_failed')


def install():
    if command('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    commit=command('git','-C',str(SOURCE),'rev-parse','HEAD')
    if command('git','-C',str(SOURCE),'status','--porcelain') or not command('git','-C',str(SOURCE),'branch','-r','--contains',commit):
        raise RuntimeError('verified_github_source_required')
    baseline=json.loads((SOURCE/'deploy/x-tag-revenue-baseline.json').read_text())
    for name,expected in baseline.items():
        if sha(name) != expected: raise RuntimeError('live_baseline_changed:'+name)
    prepared=targets()
    backup=ROOT/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True)
    info=dict(commit=commit,source=str(SOURCE),backup=str(backup),files={},status='prepared',
              existing_pids_before=command('systemctl','show','drama-material-api.service','x-post-automation.service','drama-youtube-publish-worker.service','drama-material-job-worker.service','-p','Id,MainPID'))
    for n,(target,data) in enumerate(prepared.items()):
        saved=backup/str(n)
        if Path(target).exists(): shutil.copy2(target,saved)
        info['files'][target]=dict(before=sha(target),after=hashlib.sha256(data).hexdigest(),backup=str(saved))
    record=backup/'deployment.json'; record.write_text(json.dumps(info,indent=2))
    try:
        # New independent collector is installed and warmed before enabling the page.
        for target,data in prepared.items():
            if '/features/x_tag_revenue/' in target: atomic_bytes(target,data)
        refreshed=command('/usr/bin/python3','-B','-m','features.x_tag_revenue.refresh',timeout=900) if Path.cwd()==MAIN else command(
            '/usr/bin/python3','-B','-c','import os,runpy,sys;os.chdir('+repr(str(MAIN))+');sys.path.insert(0,'+repr(str(MAIN))+');runpy.run_module("features.x_tag_revenue.refresh",run_name="__main__")',timeout=900)
        info['refresh']=json.loads(refreshed.splitlines()[-1])
        for target,data in prepared.items(): atomic_bytes(target,data)
        command('systemctl','daemon-reload')
        command('systemctl','enable','--now',UNITS[2])
        health()
        command('nginx','-t')
        command('systemctl','reload','nginx')
        command('systemctl','enable','--now',UNITS[1])
        info['existing_pids_after']=command('systemctl','show','drama-material-api.service','x-post-automation.service','drama-youtube-publish-worker.service','drama-material-job-worker.service','-p','Id,MainPID')
        info['existing_service_pids_unchanged']=info['existing_pids_before']==info['existing_pids_after']
        info['status']='active';record.write_text(json.dumps(info,indent=2))
    except Exception:
        record.write_text(json.dumps(info,indent=2))
        rollback(backup)
        raise
    print(json.dumps(info,ensure_ascii=False))


def rollback(backup):
    backup=Path(backup).resolve()
    if backup.parent != (ROOT/'backups').resolve(): raise RuntimeError('invalid_backup')
    info=json.loads((backup/'deployment.json').read_text())
    restored={}
    shared={str(MAIN/'static/x-account-list.html'):unpatch_html,str(WEB/'x-account-list.html'):unpatch_html}
    for target,row in info['files'].items():
        actual=sha(target)
        if actual == row['before']: continue
        if row['before'] is not None and sha(row['backup']) != row['before']: raise RuntimeError('backup_changed')
        if actual == row['after']:
            restored[target]=Path(row['backup']).read_bytes() if row['before'] is not None else None
        elif target in shared:
            restored[target]=shared[target](Path(target).read_text(encoding='utf-8')).encode('utf-8')
        else: raise RuntimeError('rollback_target_changed:'+target)
    command('systemctl','disable','--now',UNITS[1],check=False)
    command('systemctl','stop',UNITS[0],check=False)
    command('systemctl','disable','--now',UNITS[2],check=False)
    for target,data in restored.items():
        if data is None: Path(target).unlink(missing_ok=True)
        else: atomic_bytes(target,data)
    command('systemctl','daemon-reload')
    command('nginx','-t')
    command('systemctl','reload','nginx')
    info['status']='rolled_back'; (backup/'rollback.json').write_text(json.dumps(info,indent=2))
    print(json.dumps(dict(status='rolled_back',backup=str(backup))))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','rollback']);parser.add_argument('--backup')
    args=parser.parse_args()
    install() if args.action=='install' else rollback(args.backup)

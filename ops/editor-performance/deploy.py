"""Exact GitHub release, SQLite online backup and idle-only editor switch.

Default rollback retains the new source consumers and all durable business data.
Use --full only to restore the old baseline after incompatible work has finished.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import time

DATA=Path('/mnt/data-disk');ROOT=DATA/'editor-performance-deploy'
CURRENT=Path('/opt/ai-video-editing/current');DB=DATA/'ai-video-editing/editor.sqlite3'
UNIT='ai-video-editing.service';PREVIEW='ai-video-editing-preview.service'
STATIC=(Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html'))
NGINX=Path('/etc/nginx/default.d/ai-video-editing.conf')

def cmd(*args):return subprocess.check_output(list(map(str,args))).decode().strip()
def blob(repo,version,path):
    r=subprocess.run(['git','-C',str(repo),'show',version+':'+path],capture_output=True)
    return r.stdout if r.returncode==0 else None
def sha(data):return hashlib.sha256(data).hexdigest() if data is not None else None
def read(path):return path.read_bytes() if path.exists() else None
def atomic(path,data):
    if data is None:path.unlink(missing_ok=True);return
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.editor-next-'+path.name);tmp.write_bytes(data);tmp.replace(path)
def link(target):
    tmp=CURRENT.with_name('.editor-next');tmp.symlink_to(target);tmp.replace(CURRENT)
def idle(pid,full=False):
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks={'studio_runs':"status IN ('analyzing','preparing') AND lease<>''",'ai_edit_smart_analysis':"status='running'",
                'catalog_imports':"status='downloading'",'ai_youtube_final_material':"lease<>''",'transcript_jobs':"status='running'",
                'editor_source_jobs':"status='running'"}
        if full:
            checks.update(editor_source_jobs="status IN ('queued','running')",editor_previews="status IN ('queued','running')",
                          studio_source_batches="status IN ('queued','preparing')",studio_runs="status IN ('waiting_sources','needs_attention') OR (status IN ('analyzing','preparing') AND lease<>'')")
        for table,expr in checks.items():
            if table in tables and db.execute('SELECT 1 FROM '+table+' WHERE '+expr+' LIMIT 1').fetchone():
                raise RuntimeError('Affected work is active; service left running: '+table)
    if subprocess.run(['pgrep','-P',str(pid)],capture_output=True).returncode==0:raise RuntimeError('Editor child is active; left running')
    if cmd('ss','-Htn','state','established','sport','=',':18983'):raise RuntimeError('Editor HTTP request is active; left running')

def switch(record,folder,rollback=False,full=False):
    expected=Path(record['release'] if rollback else record['previous'])
    target=Path(record['previous'] if full else record['compatibility']) if rollback else Path(record['release'])
    if CURRENT.resolve()!=expected:raise RuntimeError('Concurrent release change')
    changes=[]
    for item in record['files']:
        if rollback and not full and item['kind']!='static':continue
        path=Path(item['path']);before=folder/item['before'] if item['before'] else None
        old=before.read_bytes() if before else None;new=(folder/item['after']).read_bytes()
        wanted=new if rollback else old
        if read(path)!=wanted:raise RuntimeError('Concurrent file change: '+str(path))
        changes.append((path,old if rollback else new,new if rollback else old))
    pid=int(cmd('systemctl','show',UNIT,'-p','MainPID','--value'));stopped=False;switched=False
    if pid<=0 or cmd('systemctl','is-active',UNIT)!='active':raise RuntimeError('Editor service is not stable')
    try:
        os.kill(pid,signal.SIGSTOP);stopped=True
        idle(pid,full)
        if CURRENT.resolve()!=expected:raise RuntimeError('Concurrent release switch')
        for path,data,_ in changes:atomic(path,data)
        try:
            if any(p==NGINX for p,_,_ in changes):cmd('nginx','-t')
        except Exception:
            for path,_,previous in reversed(changes):atomic(path,previous)
            raise
        link(target);switched=True
        record['state']='switching';atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
        os.kill(pid,signal.SIGTERM)
    finally:
        if stopped:os.kill(pid,signal.SIGCONT)
    if not switched:return
    deadline=time.monotonic()+15
    while Path('/proc/'+str(pid)).exists() and time.monotonic()<deadline:time.sleep(.1)
    if Path('/proc/'+str(pid)).exists():raise RuntimeError('Old idle editor has not exited; do not force kill')
    if full:
        # Stop the still-loaded idle unit before its removed file is reloaded.
        cmd('systemctl','stop',PREVIEW)
        subprocess.run(['systemctl','disable',PREVIEW],check=False,capture_output=True)
    cmd('systemctl','daemon-reload');cmd('systemctl','restart',UNIT)
    if not rollback:
        cmd('systemctl','enable','--now',PREVIEW)
    if any(p==NGINX for p,_,_ in changes):cmd('systemctl','reload','nginx')
    record.update(state='full_rollback' if full else 'compatible_rollback' if rollback else 'active',active_release=str(target))
    atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
    print(json.dumps({'state':record['state'],'release':str(target),'manifest':str(folder/'manifest.json')}))

def prepare_release(repo,base,version,previous):
    release=ROOT/'releases'/version
    if release.exists():raise RuntimeError('Release already exists; inspect its manifest')
    shutil.copytree(previous,release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in cmd('git','-C',repo,'diff','--name-only',base,version,'--','features/ai_video_editing').splitlines():
        path=Path(name.removeprefix('features/'));data=blob(repo,version,name)
        if read(previous/path)!=blob(repo,base,name):raise RuntimeError('Live module differs from baseline: '+name)
        if data is not None:compile(data,name,'exec')
        atomic(release/path,data)
    return release

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['install','rollback']);p.add_argument('--repo',type=Path);p.add_argument('--version');p.add_argument('--base');p.add_argument('--compat-version');p.add_argument('--manifest',type=Path);p.add_argument('--full',action='store_true');p.add_argument('--workers',type=int,choices=[1,2,3],default=1)
    a=p.parse_args()
    if cmd('findmnt','-no','UUID','--target',DATA)!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('Unexpected data mount')
    if a.action=='rollback':return switch(json.loads(a.manifest.read_text()),a.manifest.parent,True,a.full)
    if a.manifest:return switch(json.loads(a.manifest.read_text()),a.manifest.parent)
    repo=a.repo
    observed=json.loads((repo/'ops/editor-performance/static-baseline.json').read_text())
    if cmd('git','-C',repo,'rev-parse','HEAD')!=a.version or cmd('git','-C',repo,'status','--porcelain'):raise RuntimeError('Need a clean exact checkout')
    for version in (a.version,a.compat_version):
        if not cmd('git','-C',repo,'branch','-r','--contains',version):raise RuntimeError('Version was not fetched from GitHub')
    previous=CURRENT.resolve()
    if previous.name!=a.base:raise RuntimeError('Production baseline changed')
    folder=ROOT/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');folder.mkdir(parents=True,mode=0o700)
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(folder/'before-editor.sqlite3')) as dst:src.backup(dst)
    (folder/'before-editor.sqlite3').chmod(0o600)
    shutil.copytree(previous/'ai_video_editing',folder/'ai_video_editing',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('/etc/systemd/system/ai-video-editing.service','/etc/ai-video-editing/runtime.env'):
        if Path(name).is_file():shutil.copyfile(name,folder/Path(name).name);(folder/Path(name).name).chmod(0o600)
    compatibility=prepare_release(repo,a.base,a.compat_version,previous)
    release=prepare_release(repo,a.base,a.version,previous)
    record={'version':a.version,'base':a.base,'compat_version':a.compat_version,'previous':str(previous),'release':str(release),'compatibility':str(compatibility),'state':'prepared','files':[],'workers':a.workers}
    changes=[]
    for name in cmd('git','-C',repo,'diff','--name-only',a.base,a.version,'--','static').splitlines():
        if not Path(name).name.startswith('video-editing-ai'):raise RuntimeError('Unexpected static scope')
        before=blob(repo,a.base,name);after=blob(repo,a.version,name)
        for root in STATIC:
            path=root/Path(name).name
            actual=read(path)
            if actual!=before and sha(actual)!=observed.get(name):raise RuntimeError('Static baseline changed: '+str(path))
            changes.append((path,actual,after,'static'))
    if read(NGINX)!=blob(repo,a.base,'deploy/ai-video-editing-nginx.conf'):raise RuntimeError('Nginx baseline changed')
    changes.append((NGINX,read(NGINX),blob(repo,a.version,'deploy/ai-video-editing-nginx.conf'),'nginx'))
    preview_unit=Path('/etc/systemd/system')/PREVIEW
    if preview_unit.exists():raise RuntimeError('Preview worker already installed; inspect first')
    changes.append((preview_unit,None,blob(repo,a.version,'deploy/'+PREVIEW),'unit'))
    config=Path('/etc/systemd/system/ai-video-editing.service.d/40-source-workers.conf')
    if config.exists():raise RuntimeError('Worker override already exists; inspect first')
    changes.append((config,None,('[Service]\nEnvironment=AI_VIDEO_EDITING_IMPORT_WORKERS=%s\n'%a.workers).encode(),'unit'))
    proxy=DATA/'ai-video-editing-preview';proxy.mkdir(exist_ok=True,mode=0o2750)
    os.chown(proxy,0,grp.getgrnam('nginx').gr_gid);proxy.chmod(0o2750)
    for i,(path,before,after,kind) in enumerate(changes):
        if before is not None:(folder/f'{i}.before').write_bytes(before)
        (folder/f'{i}.after').write_bytes(after)
        record['files'].append({'path':str(path),'kind':kind,'old_sha256':sha(before),'new_sha256':sha(after),'before':f'{i}.before' if before is not None else None,'after':f'{i}.after'})
    atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
    switch(record,folder)


if __name__=='__main__':main()

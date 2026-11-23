"""Stage exact GitHub modules and safely switch only the independent editor."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import time

CPU_FILES=['cloud_media.py','http_api.py','output_storage.py','runtime.py','studio.py',
           'transcript_jobs.py','youtube_material.py','youtube_material_io.py']
GPU_FILES=['output_storage.py','render_worker.py']


def cmd(*args):return subprocess.check_output(list(map(str,args))).decode().strip()
def blob(repo,version,name):
    result=subprocess.run(['git','-C',str(repo),'show',version+':features/ai_video_editing/'+name],capture_output=True)
    return result.stdout if result.returncode==0 else None
def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.next');temporary.write_bytes(data);temporary.chmod(0o600);temporary.replace(path)
def save(path,record):atomic(path,json.dumps(record,indent=2).encode())
def link(path,target):
    temporary=path.with_name(path.name+'.next');temporary.symlink_to(target);temporary.replace(path)


def switch(record,manifest,rollback=False):
    role=record['role'];current=Path(record['current'])
    expected=Path(record['release'] if rollback else record['previous'])
    target=Path(record['previous'] if rollback else record['release'])
    if current.resolve()!=expected:raise RuntimeError('Concurrent release change')
    unit='ai-video-editing.service' if role=='cpu' else 'ai-video-editing-render.service'
    pid=int(cmd('systemctl','show',unit,'-p','MainPID','--value'))
    if pid<=0:raise RuntimeError('Expected service is not active')
    stopped=False
    try:
        os.kill(pid,signal.SIGSTOP);stopped=True
        if role=='cpu':
            spec=importlib.util.spec_from_file_location('editor_previous_deploy',Path(record['repo'])/'ops/editor-performance/deploy.py')
            helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
            helper.idle(pid)
        else:
            # A frozen worker cannot claim a job between the idle check and TERM.
            work=Path('/data/ai-video-editing/work')
            if list(work.glob('render-*')) or subprocess.run(['pgrep','-P',str(pid)],capture_output=True).returncode==0:
                raise RuntimeError('GPU editor is working; leave it running and retry this manifest')
        if current.resolve()!=expected:raise RuntimeError('Concurrent release switch')
        if role=='gpu' and not rollback:
            config=Path('/etc/systemd/system/ai-video-editing-render.service.d/60-output-cos.conf')
            wanted=(Path(record['repo'])/'ops/editor-gpu-cos/render-cos.conf').read_bytes()
            if config.exists() and config.read_bytes()!=wanted:raise RuntimeError('Concurrent GPU output configuration')
            atomic(config,wanted)
        link(current,target)
        record['state']='switching';save(manifest,record)
        os.kill(pid,signal.SIGTERM)
    finally:
        if stopped:os.kill(pid,signal.SIGCONT)
    deadline=time.monotonic()+20
    while Path('/proc/'+str(pid)).exists() and time.monotonic()<deadline:time.sleep(.2)
    if Path('/proc/'+str(pid)).exists():raise RuntimeError('Old process did not exit; no forced kill')
    cmd('systemctl','daemon-reload')
    cmd('systemctl','restart',unit)
    if cmd('systemctl','is-active',unit)!='active':raise RuntimeError('New service is not active')
    record.update(state='rolled_back' if rollback else 'active',active_release=str(target))
    save(manifest,record)
    print(json.dumps({'state':record['state'],'manifest':str(manifest),'pid':cmd('systemctl','show',unit,'-p','MainPID','--value')}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','resume','rollback'])
    p.add_argument('--role',choices=['cpu','gpu']);p.add_argument('--repo',type=Path)
    p.add_argument('--base');p.add_argument('--version');p.add_argument('--manifest',type=Path)
    a=p.parse_args()
    if a.action in ('resume','rollback'):
        record=json.loads(a.manifest.read_text())
        if a.action=='rollback' and record['role']=='cpu':
            raise RuntimeError('Keep the COS-compatible reader; rollback the GPU writer only. Never restore the database.')
        return switch(record,a.manifest,a.action=='rollback')
    if not all([a.role,a.repo,a.base,a.version]):raise RuntimeError('Role, repo, base and version required')
    if cmd('git','-C',a.repo,'rev-parse','HEAD')!=a.version or cmd('git','-C',a.repo,'status','--porcelain'):
        raise RuntimeError('Need a clean exact GitHub checkout')
    if not cmd('git','-C',a.repo,'branch','-r','--contains',a.version):raise RuntimeError('Version is not on fetched GitHub branch')
    if a.role=='cpu':
        if cmd('findmnt','-no','UUID','--target','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
            raise RuntimeError('Data mount unavailable')
        root=Path('/mnt/data-disk/editor-gpu-cos');current=Path('/opt/ai-video-editing/current')
        module=Path('ai_video_editing');names=CPU_FILES
    else:
        root=Path('/data/ai-video-editing/gpu-cos');current=Path('/data/ai-video-editing/current')
        module=Path('features/ai_video_editing');names=GPU_FILES
    previous=current.resolve()
    for name in names:
        actual=(previous/module/name).read_bytes() if (previous/module/name).exists() else None
        if actual!=blob(a.repo,a.base,name):raise RuntimeError('Live module differs: '+name)
    folder=root/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder.mkdir(parents=True,mode=0o700)
    target=root/'releases'/a.version
    shutil.copytree(previous,target,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in names:
        data=blob(a.repo,a.version,name);compile(data,name,'exec');atomic(target/module/name,data)
    if a.role=='cpu':
        with closing(sqlite3.connect('file:/mnt/data-disk/ai-video-editing/editor.sqlite3?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(folder/'editor-before.sqlite3')) as dest:source.backup(dest)
        (folder/'editor-before.sqlite3').chmod(0o600)
    record={'role':a.role,'version':a.version,'base':a.base,'repo':str(a.repo),'current':str(current),
            'previous':str(previous),'release':str(target),'state':'prepared','modules':names}
    manifest=folder/'manifest.json';save(manifest,record)
    print('prepared_manifest='+str(manifest),flush=True)
    switch(record,manifest)


if __name__=='__main__':main()

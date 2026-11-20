"""Narrow editor release switch. Rollback changes code only, never business data."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import time

DATA = Path('/mnt/data-disk')
CURRENT = Path('/opt/ai-video-editing/current')
UNIT = 'ai-video-editing.service'
DB = DATA/'ai-video-editing/editor.sqlite3'
STATIC = (Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html'))


def cmd(*args):
    return subprocess.check_output(list(map(str, args))).decode().strip()


def atomic(path, data):
    temp = path.with_name('.transcript-'+path.name)
    temp.write_bytes(data)
    temp.replace(path)


def link(target):
    temp = CURRENT.with_name('.transcript-next')
    temp.symlink_to(target)
    temp.replace(CURRENT)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def idle(pid, rollback=False):
    # The old service has daemon workers and a 30s stop timeout. Freeze it only
    # for this idle check + atomic switch, so no new job can enter the check/
    # stop gap. Never stop a process which owns an in-flight operation.
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks = {'studio_runs':"status IN ('analyzing','preparing') AND lease<>''",
                  'ai_edit_smart_analysis':"status='running'",
                  'catalog_imports':"status='downloading'",
                  'ai_youtube_final_material':"lease<>''",
                  'transcript_jobs':"status='running'"}
        if rollback:
            # The prior release has no caption-job consumer. Finish queued
            # caption reads before rolling back; keep their durable receipts.
            checks['transcript_jobs'] = "status IN ('queued','running')"
        for table, expr in checks.items():
            if table in tables and db.execute('SELECT 1 FROM '+table+' WHERE '+expr+' LIMIT 1').fetchone():
                raise RuntimeError('Editor work is active; left running: '+table)
    children = subprocess.run(['pgrep','-P',str(pid)], capture_output=True)
    if children.returncode == 0:
        raise RuntimeError('Editor child process is active; left running')
    if cmd('ss','-Htn','state','established','sport','=',':18983'):
        raise RuntimeError('Editor HTTP request is active; left running')


def switch(record, installing, folder):
    expected = Path(record['previous'] if installing else record['release'])
    target = Path(record['release'] if installing else record['previous'])
    if CURRENT.resolve() != expected:
        raise RuntimeError('Current editor release changed; inspect before switching')
    plan = []
    for item in record['static']:
        path = Path(item['path'])
        wanted = item['old' if installing else 'new']
        if digest(path.read_bytes()) != wanted:
            raise RuntimeError('Static file changed concurrently: '+str(path))
        plan.append((path, (folder/item['after' if installing else 'before']).read_bytes()))
    pid = int(cmd('systemctl','show',UNIT,'-p','MainPID','--value'))
    if pid <= 0 or cmd('systemctl','is-active',UNIT) != 'active':
        raise RuntimeError('Editor service is not stable')
    stopped = False
    switched = False
    try:
        os.kill(pid, signal.SIGSTOP)
        stopped = True
        idle(pid, rollback=not installing)
        if CURRENT.resolve() != expected:
            raise RuntimeError('Concurrent release switch')
        for path, data in plan:
            atomic(path, data)
        link(target)
        switched = True
        record['state'] = 'switching'
        atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
        # TERM is queued while the idle process is stopped. On CONT the default
        # TERM action exits before any application thread can claim another job.
        os.kill(pid, signal.SIGTERM)
    finally:
        if stopped:
            os.kill(pid, signal.SIGCONT)
    if not switched:
        return
    deadline = time.monotonic()+15
    while Path('/proc/'+str(pid)).exists() and time.monotonic()<deadline:
        time.sleep(.1)
    if Path('/proc/'+str(pid)).exists():
        raise RuntimeError('Old idle editor did not exit; inspect, do not force kill')
    cmd('systemctl','restart',UNIT)
    record.update(state='active' if installing else 'rolled_back', pid=int(cmd('systemctl','show',UNIT,'-p','MainPID','--value')))
    atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
    print(json.dumps({'state':record['state'],'release':str(target),'backup':str(folder),'unit':UNIT}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('install','rollback'))
    parser.add_argument('--repo',type=Path)
    parser.add_argument('--version')
    parser.add_argument('--base')
    parser.add_argument('--manifest',type=Path)
    args = parser.parse_args()
    if cmd('findmnt','-no','UUID','--target',DATA) != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Incorrect data mount')
    if args.action == 'rollback':
        record = json.loads(args.manifest.read_text())
        return switch(record,False,args.manifest.parent)
    repo, version, base = args.repo, args.version, args.base
    if cmd('git','-C',repo,'rev-parse','HEAD') != version or cmd('git','-C',repo,'status','--porcelain'):
        raise RuntimeError('Expected a clean exact GitHub checkout')
    if not cmd('git','-C',repo,'branch','-r','--contains',version):
        raise RuntimeError('Version has not been fetched from GitHub')
    previous = CURRENT.resolve()
    if not previous.name.endswith(base):
        raise RuntimeError('Production baseline changed; review its concurrent changes')
    names = cmd('git','-C',repo,'diff','--name-only',base,version).splitlines()
    package, static = [], []
    for name in names:
        if name.startswith('features/ai_video_editing/'):
            path = Path(name.removeprefix('features/'))
            before = subprocess.run(['git','-C',str(repo),'show',base+':'+name],capture_output=True)
            live = previous/path
            if (live.read_bytes() if live.exists() else None) != (before.stdout if before.returncode == 0 else None):
                raise RuntimeError('Live module differs from baseline: '+name)
            data = (repo/name).read_bytes()
            compile(data,name,'exec')
            package.append((path,data))
        elif name == 'static/video-editing-ai-language.js':
            before = subprocess.check_output(['git','-C',str(repo),'show',base+':'+name])
            data = (repo/name).read_bytes()
            for root in STATIC:
                path = root/Path(name).name
                if path.read_bytes() != before:
                    raise RuntimeError('Live static file differs from baseline: '+str(path))
                static.append((path,before,data))
    if not package:
        raise RuntimeError('No editor changes')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = DATA/'editor-transcript-deploy/backups'/stamp
    folder.mkdir(parents=True,mode=0o700)
    release = DATA/'editor-transcript-deploy/releases'/version
    shutil.copytree(previous,release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for path,data in package:
        atomic(release/path,data)
    shutil.copytree(previous/'ai_video_editing',folder/'ai_video_editing',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for path in ('/etc/systemd/system/ai-video-editing.service','/etc/ai-video-editing/runtime.env'):
        if Path(path).is_file():
            shutil.copyfile(path,folder/Path(path).name)
            (folder/Path(path).name).chmod(0o600)
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(str(folder/'before-editor.sqlite3'))) as dst:
            src.backup(dst)
    (folder/'before-editor.sqlite3').chmod(0o600)
    record = {'version':version,'base':base,'previous':str(previous),'release':str(release),'static':[], 'state':'prepared'}
    for i,(path,before,after) in enumerate(static):
        (folder/f'{i}.before').write_bytes(before)
        (folder/f'{i}.after').write_bytes(after)
        record['static'].append({'path':str(path),'old':digest(before),'new':digest(after),'before':f'{i}.before','after':f'{i}.after'})
    atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
    switch(record,True,folder)


if __name__ == '__main__':
    main()

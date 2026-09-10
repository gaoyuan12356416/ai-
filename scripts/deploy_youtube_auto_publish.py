#!/usr/bin/env python3
"""Narrow CPU release from a verified GitHub archive; no external test posts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT=Path('/root/drama_material_service')
PUBLIC=Path('/usr/share/nginx/html')
BASE=Path('/mnt/data-disk/deploy/youtube-auto-publish')
FILES=['app.py','features/drama_synthesis/core.py','features/drama_synthesis/youtube.py',
       'features/drama_synthesis/unified_youtube.py','features/drama_synthesis/unified_youtube_rpc.py',
       'features/youtube_auto_publish/__init__.py','features/youtube_auto_publish/templates.py',
       'features/youtube_auto_publish/source.py','features/youtube_auto_publish/service.py',
       'features/youtube_auto_publish/runtime.py','features/youtube_auto_publish/engine.py',
       'static/navigation.json','static/quick-nav.js','static/youtube-publish.html',
       'static/youtube-publish.css','static/youtube-publish.js','scripts/youtube_auto_publish_worker.py',
       'deploy/live_feature_guard.json']
SERVICES=['drama-youtube-unified-writer.service','drama-material-api.service','drama-youtube-publish-worker.service']
NEW='youtube-auto-publish-worker.service'

def run(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT).strip()
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write_json(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def healthy():
    with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status',timeout=8) as response:
        if response.status!=200 or not isinstance(json.load(response),dict):raise RuntimeError('API health failed')

def backup_one(path,backup,records):
    path=Path(path);exists=path.exists();saved=backup/'files'/str(path).lstrip('/')
    record={'path':str(path),'exists':exists,'mode':path.stat().st_mode&0o777 if exists else None}
    if exists:
        saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved);record['sha256']=digest(path)
    records.append(record)

def install(source,target,mode=0o644):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.youtube-new')
    shutil.copyfile(source,temp);os.chmod(temp,mode);os.replace(temp,target)

def rollback(backup):
    manifest=json.loads((backup/'manifest.json').read_text())
    subprocess.run(['systemctl','disable','--now',NEW],capture_output=True)
    for unit in SERVICES[1:]:subprocess.run(['systemctl','stop',unit],check=True)
    for record in reversed(manifest['files']):
        target=Path(record['path'])
        if record['exists']:install(backup/'files'/str(target).lstrip('/'),target,record['mode'])
        elif target.exists():target.unlink()
    run('systemctl','daemon-reload')
    for unit in SERVICES:run('systemctl','restart',unit)
    run('nginx','-t');run('systemctl','reload','nginx')
    healthy();print(json.dumps({'rollback':'complete','backup':str(backup),'database':'retained'}))

def main():
    p=argparse.ArgumentParser();p.add_argument('--commit');p.add_argument('--rollback');a=p.parse_args()
    if a.rollback:
        backup=Path(a.rollback).resolve()
        if not backup.is_relative_to(BASE/'backups'):raise RuntimeError('Invalid rollback path')
        rollback(backup);return
    stage=Path(__file__).resolve().parents[1]
    if not a.commit or len(a.commit)!=40 or (stage/'.github-verified-commit').read_text().strip()!=a.commit:
        raise RuntimeError('Only an exact GitHub-fetched commit may deploy')
    if run('findmnt','-n','-o','UUID','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('Data mount mismatch')
    expected=json.loads((stage/'deploy/youtube-auto-live-baseline.json').read_text())
    for name,record in expected.items():
        if digest(ROOT/name)!=record['sha256']:raise RuntimeError('Live file changed since baseline: '+name)
    public_expected=json.loads((stage/'deploy/youtube-auto-public-baseline.json').read_text())
    for name,sha in public_expected.items():
        if digest(PUBLIC/name)!=sha:
            raise RuntimeError('Public static file has independent changes: '+name)
    writer=Path('/opt/drama-youtube-unified-writer/current').resolve()
    for name in ('unified_youtube.py','unified_youtube_rpc.py'):
        if (writer/'features/drama_synthesis'/name).read_bytes().replace(b'\r\n',b'\n')!=(ROOT/'features/drama_synthesis'/name).read_bytes().replace(b'\r\n',b'\n'):
            raise RuntimeError('Writer has independent changes: '+name)
    for name in FILES:
        if name.endswith('.py'):py_compile.compile(str(stage/name),doraise=True)
    db=ROOT/'data/drama_material_jobs.sqlite3'
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
        active=c.execute("SELECT COUNT(*) FROM drama_youtube_publish WHERE status NOT IN ('published','failed','unknown','partial_failed','cancelled') OR comment_status='publishing'").fetchone()[0]
        counts=c.execute('SELECT status,comment_status,count(*) FROM drama_youtube_publish GROUP BY status,comment_status').fetchall()
        if active:raise RuntimeError('Existing YouTube upload/comment is active; retry later')
    healthy()
    backup=BASE/'backups'/(time.strftime('%Y%m%d-%H%M%S')+'-'+a.commit[:12]);backup.mkdir(parents=True,exist_ok=False)
    records=[]
    targets=[ROOT/name for name in FILES]+[PUBLIC/Path(name).name for name in FILES if name.startswith('static/')]
    targets += [writer/'features/drama_synthesis/unified_youtube.py',writer/'features/drama_synthesis/unified_youtube_rpc.py',
                Path('/etc/systemd/system')/NEW,Path('/etc/systemd/system/drama-material-api.service.d/96-youtube-auto-publish.conf'),
                Path('/etc/youtube-auto-publish.env'),Path('/etc/youtube-auto-publish/material-source.sql'),
                Path('/etc/nginx/default.d/youtube-auto-publish.conf')]
    for target in targets:backup_one(target,backup,records)
    manifest={'commit':a.commit,'files':records,'writer_root':str(writer),'ledger_counts_before':counts,'stage':str(stage)}
    write_json(backup/'manifest.json',manifest)
    with sqlite3.connect(db) as src,sqlite3.connect(backup/'drama_material_jobs.sqlite3') as dst:src.backup(dst)
    os.chmod(backup/'drama_material_jobs.sqlite3',0o600)
    # Stop the old claimant before modifying its code. Preserve every ledger row.
    run('systemctl','stop','drama-youtube-publish-worker.service')
    try:
        for name in FILES:install(stage/name,ROOT/name)
        for name in FILES:
            if name.startswith('static/'):
                source=stage/'deploy/youtube-auto-public-navigation.json' if name=='static/navigation.json' else stage/name
                install(source,PUBLIC/Path(name).name)
        for name in ('unified_youtube.py','unified_youtube_rpc.py'):
            install(stage/'features/drama_synthesis'/name,writer/'features/drama_synthesis'/name)
        storage=Path('/mnt/data-disk/youtube-auto-publish')
        for name in ('assets','generation','media'):(storage/name).mkdir(parents=True,exist_ok=True)
        os.chmod(storage,0o700)
        env=Path('/etc/youtube-auto-publish.env');sql=Path('/etc/youtube-auto-publish/material-source.sql')
        if env.exists() or sql.exists():raise RuntimeError('Existing feature configuration requires explicit preservation')
        install(stage/'deploy/youtube-auto-publish.env.example',env,0o600)
        install(stage/'deploy/youtube-auto-material-source.sql.example',sql,0o600)
        install(stage/'deploy/youtube-auto-publish-api.conf','/etc/systemd/system/drama-material-api.service.d/96-youtube-auto-publish.conf')
        install(stage/'deploy/youtube-auto-publish-worker.service',Path('/etc/systemd/system')/NEW)
        install(stage/'deploy/youtube-auto-publish-nginx.conf','/etc/nginx/default.d/youtube-auto-publish.conf')
        run('nginx','-t')
        guard=run('/usr/bin/python3',str(ROOT/'scripts/verify_live_feature_guard.py'),'--root',str(ROOT),'--public-root',str(PUBLIC))
        (backup/'feature-guard.txt').write_text(guard)
        run('systemctl','daemon-reload')
        for unit in SERVICES:run('systemctl','restart',unit)
        for _ in range(12):
            try:healthy();break
            except Exception:time.sleep(1)
        else:raise RuntimeError('API did not become healthy')
        run('systemctl','enable','--now',NEW)
        run('systemctl','reload','nginx')
        time.sleep(4)
        states={unit:run('systemctl','is-active',unit) for unit in SERVICES+[NEW]}
        if any(value!='active' for value in states.values()):raise RuntimeError('Service not active')
        readback={name:digest(ROOT/name) for name in FILES}
        if any(readback[name]!=digest(stage/name) for name in FILES):raise RuntimeError('Installed source mismatch')
        result={'commit':a.commit,'backup':str(backup),'stage':str(stage),'services':states,'sha256':readback,'material_sql':'unconfigured'}
        write_json(backup/'result.json',result);write_json(BASE/'last-release.json',result)
        print(json.dumps(result))
    except BaseException:
        rollback(backup);raise

if __name__=='__main__':main()

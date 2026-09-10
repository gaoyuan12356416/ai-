"""Exact two-file deployment; graceful worker reload and preserved alert outbox."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

ROOT=Path('/root/drama_material_service')
BASE=Path('/mnt/data-disk/deploy/youtube-auto-publish')
UNIT='youtube-auto-publish-worker.service'
FILES={'features/youtube_auto_publish/failure_notifications.py':None,
       'scripts/youtube_auto_publish_worker.py':'fd9e990f72a9700417f88b1c7b3c5437de117136f61b53736bf267840cbc2a28'}
def run(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=30).strip()
def sha(path):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('Symlink target refused')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
def install(source,target):
    target=Path(target);temp=target.with_name(target.name+'.failure-alert-new')
    shutil.copyfile(source,temp);os.chmod(temp,0o644);os.replace(temp,target)
def idle():
    with sqlite3.connect('file:'+str(ROOT/'data/drama_material_jobs.sqlite3')+'?mode=ro',uri=True) as db:
        prep=db.execute("SELECT count(*) FROM youtube_auto_preparation WHERE state IN ('queued_generation','generating','enqueue_pending') OR lease_until>strftime('%s','now')").fetchone()[0]
        publish=db.execute("SELECT count(*) FROM drama_youtube_publish WHERE workflow='reviewed_thumbnail' AND (status NOT IN ('published','failed','unknown','partial_failed','cancelled') OR comment_status='publishing')").fetchone()[0]
    if prep or publish:raise RuntimeError('An active user operation is running; leave worker unchanged')
def reload_worker():
    old=run('systemctl','show',UNIT,'-p','MainPID','--value')
    if old=='0' or run('systemctl','show',UNIT,'-p','Restart','--value')!='always':raise RuntimeError('Managed active restart-always worker required')
    # SIGTERM only requests STOP at the loop boundary. Unlike stop/restart, it
    # does not start systemd TimeoutStopSec that could kill a newly claimed job.
    run('systemctl','kill','--kill-who=main','--signal=SIGTERM',UNIT)
    for _ in range(45):
        pid=run('systemctl','show',UNIT,'-p','MainPID','--value')
        if pid not in ('0',old) and run('systemctl','is-active',UNIT)=='active':return {'old_pid':old,'new_pid':pid}
        time.sleep(1)
    raise RuntimeError('Graceful reload pending; do not terminate an active job')
def main():
    p=argparse.ArgumentParser();p.add_argument('--commit');p.add_argument('--rollback',type=Path);p.add_argument('--check',action='store_true');a=p.parse_args()
    if run('findmnt','-n','-o','UUID','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('Data mount mismatch')
    stage=Path(__file__).resolve().parents[1]
    import fcntl
    with (BASE/'failure-alert-deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.rollback:
            backup=a.rollback.resolve()
            if (BASE/'backups').resolve() not in backup.parents:raise RuntimeError('Invalid rollback path')
            manifest=json.loads((backup/'manifest.json').read_text())
            if manifest.get('kind')!='youtube-failure-alerts' or set(manifest['files'])!=set(FILES):raise RuntimeError('Invalid manifest')
            for name,row in manifest['files'].items():
                if sha(ROOT/name)!=row['installed']:raise RuntimeError('Newer live change exists')
                if row['before'] is not None and sha(backup/name)!=row['before']:raise RuntimeError('Invalid backup bytes')
            idle()
            for name,row in reversed(list(manifest['files'].items())):
                if row['before'] is None:(ROOT/name).unlink()
                else:install(backup/name,ROOT/name)
            result=reload_worker();print(json.dumps({'rollback':str(backup),'worker':result,'outbox':'retained'}));return
        if not a.commit or len(a.commit)!=40 or (stage/'.github-verified-commit').read_text().strip()!=a.commit:raise RuntimeError('Verified GitHub commit required')
        for name,expected in FILES.items():
            if sha(ROOT/name)!=expected or not (stage/name).is_file():raise RuntimeError('Baseline drift: '+name)
        idle()
        if a.check:print(json.dumps({'check':'passed','files':2}));return
        backup=BASE/'backups'/('failure-alerts-'+time.strftime('%Y%m%d-%H%M%S')+'-'+a.commit[:12]);backup.mkdir(parents=True)
        manifest={'kind':'youtube-failure-alerts','commit':a.commit,'files':{}}
        for name,expected in FILES.items():
            if expected is not None:
                saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,saved)
            manifest['files'][name]={'before':expected,'installed':sha(stage/name)}
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        for name,expected in FILES.items():
            if sha(ROOT/name)!=expected:raise RuntimeError('Baseline changed before install')
            install(stage/name,ROOT/name)
        for name,row in manifest['files'].items():
            if sha(ROOT/name)!=row['installed']:raise RuntimeError('Installed bytes differ')
        result={'commit':a.commit,'backup':str(backup),'worker':reload_worker(),'files':manifest['files'],'database':'publishing unchanged; separate failure outbox'}
        (backup/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
if __name__=='__main__':main()

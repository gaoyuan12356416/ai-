"""GitHub-first scoped install/activation/rollback; business facts are retained."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from datetime import datetime,timezone
from contextlib import closing

ROOT=Path('/mnt/data-disk/meta-page-recovery')
CURRENT=Path('/opt/meta-page-recovery/current')
UNITS=Path('/etc/systemd/system')
NAMES=['meta-page-recovery-scan.service','meta-page-recovery-scan.timer','meta-page-recovery-worker.service',
       'meta-page-recovery-report.service','meta-page-recovery-report.timer']
SCAN_CALENDAR='*-*-* 00,04,08,12,16,20:05:00 Asia/Shanghai'
REPORT_CALENDAR='*-*-* 10:00:00 Asia/Shanghai'
PROFILE_DROP=UNITS/'fb-page-portrait-refresh.service.d/meta-page-recovery.conf'


def run(*args):return subprocess.check_output(args,text=True).strip()
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def unit_files():
    common='[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nWorkingDirectory=/opt/meta-page-recovery/current\nUMask=0077\n'
    return {
        NAMES[0]:common+'Type=oneshot\nExecStart=/usr/bin/python3 -m features.meta_page_recovery scan\nTimeoutStartSec=infinity\n',
        NAMES[1]:'[Unit]\nDescription=Four-hour Meta Page recovery scan\n[Timer]\nOnCalendar='+SCAN_CALENDAR+'\nPersistent=false\nUnit='+NAMES[0]+'\n[Install]\nWantedBy=timers.target\n',
        NAMES[2]:common+'Type=simple\nExecStart=/usr/bin/python3 -m features.meta_page_recovery work\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=1800\n[Install]\nWantedBy=multi-user.target\n',
        NAMES[3]:common+'Type=oneshot\nExecStart=/usr/bin/python3 -m features.meta_page_recovery report\nTimeoutStartSec=120\n',
        NAMES[4]:'[Unit]\nDescription=Daily 10:00 Meta Page recovery report\n[Timer]\nOnCalendar='+REPORT_CALENDAR+'\nPersistent=false\nUnit='+NAMES[3]+'\n[Install]\nWantedBy=timers.target\n',
    }


def unit_state(name,command):
    return subprocess.run(['systemctl',command,name],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True).stdout.strip() or 'not-found'


def build_release(source,release,profile):
    release.mkdir(parents=True,mode=0o755)
    for package in ('meta_page_recovery','fb_page_portrait','fb_page_assignment'):
        shutil.copytree(source/'features'/package,release/'features'/package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # shared() prepends the current production dependency owner. It requires a
    # regular package path, not Python's append-only namespace _NamespacePath.
    (release/'features/__init__.py').write_text('"""Scoped Meta recovery runtime packages."""\n',encoding='utf-8')
    for path in profile.glob('*.py'):
        if path.name not in {'classification.py','collect.py','build.py','refresh.py','storage.py'}:
            shutil.copy2(path,release/'features/fb_page_portrait'/path.name)


def install(source,commit,deployment):
    if run('findmnt','-no','UUID','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('data_disk_unavailable')
    if run('git','-C',str(source),'rev-parse','HEAD')!=commit:raise RuntimeError('source_commit_mismatch')
    if run('git','-C',str(source),'status','--porcelain','--untracked-files=no'):raise RuntimeError('source_not_clean')
    remote=run('git','-C',str(source),'ls-remote','origin','refs/heads/codex/meta-page-recovery-20261117')
    if not remote.startswith(commit+'\t'):raise RuntimeError('github_commit_not_verified')
    if deployment.exists():raise RuntimeError('deployment_already_exists')
    deployment.mkdir(parents=True,mode=0o700)
    shutil.copy2(__file__,deployment/'deploy.py')
    previous={}
    for name in NAMES:
        path=UNITS/name
        previous[str(path)]=path.read_text() if path.exists() else None
    previous[str(PROFILE_DROP)]=PROFILE_DROP.read_text() if PROFILE_DROP.exists() else None
    manifest=dict(commit=commit,at=datetime.now(timezone.utc).isoformat(),previous_files=previous,
                  previous_current=str(CURRENT.resolve()) if CURRENT.exists() else None,
                  previous_states={name:dict(enabled=unit_state(name,'is-enabled'),active=unit_state(name,'is-active')) for name in NAMES},
                  profile_baseline={},activated=False)
    if (ROOT/'state.sqlite3').exists():
        with closing(sqlite3.connect('file:'+str(ROOT/'state.sqlite3')+'?mode=ro',uri=True)) as live, closing(sqlite3.connect(deployment/'state-before.sqlite3')) as backup:
            live.backup(backup)
    profile=Path('/opt/fb-page-portraits/current/features/fb_page_portrait')
    expected={'classification.py':'f7a41e254b8c8295b3cb5655f5976a96a68c4c15','collect.py':'45ddb9902a208a384b8047ca4c71b36a921058c6',
              'storage.py':'b449ab307c7c00d549ea2c9d42b818336868dca1'}
    for name,blob in expected.items():
        if run('git','hash-object',str(profile/name))!=blob:raise RuntimeError('profile_baseline_changed_'+name)
    for name in ('classification.py','collect.py','refresh.py','storage.py'):
        p=profile/name;manifest['profile_baseline'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
        shutil.copy2(p,deployment/('profile-before-'+name))
    save(deployment/'manifest.json',manifest)
    release=ROOT/'releases'/commit
    if release.exists():raise RuntimeError('release_exists')
    build_release(source,release,profile)
    # Preserve the complete deployed profile reader's unchanged helper files.
    # This is a refresh-only release; no main/FB publishing owner is replaced.
    subprocess.check_call(['/usr/bin/python3','-m','compileall','-q',str(release/'features')])
    subprocess.check_call(['/usr/bin/python3','-c','import features.fb_page_portrait.collect; import features.meta_page_recovery.engine'],cwd=release)
    CURRENT.parent.mkdir(parents=True,exist_ok=True)
    temp=CURRENT.with_name('current.next');temp.symlink_to(release);temp.replace(CURRENT)
    for name,body in unit_files().items():
        (UNITS/name).write_text(body,encoding='utf-8')
    PROFILE_DROP.parent.mkdir(parents=True,exist_ok=True)
    PROFILE_DROP.write_text('[Service]\nWorkingDirectory=/opt/meta-page-recovery/current\n',encoding='utf-8')
    env=Path('/etc/meta-page-recovery.env')
    if not env.exists():
        env.write_text('META_PAGE_RECOVERY_GRAPH_VERSION=v25.0\nMETA_PAGE_RECOVERY_TOKEN_USER_IDS=803,804\n',encoding='utf-8');env.chmod(0o600)
    subprocess.check_call(['systemctl','daemon-reload'])
    print(json.dumps(dict(installed=True,activated=False,commit=commit,deployment=str(deployment))))


def activate(deployment):
    manifest=json.loads((deployment/'manifest.json').read_text())
    if CURRENT.resolve()!=ROOT/'releases'/manifest['commit']:raise RuntimeError('release_drift')
    # The first scan, before starting the worker, only creates the full baseline.
    subprocess.check_call(['/usr/bin/python3','-m','features.meta_page_recovery','scan'],cwd=CURRENT)
    import sqlite3
    with closing(sqlite3.connect(ROOT/'state.sqlite3')) as db:
        if not db.execute("SELECT value FROM metadata WHERE key='baseline_at'").fetchone():raise RuntimeError('baseline_not_established')
        if db.execute('SELECT error FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()[0]:raise RuntimeError('baseline_scan_failed')
    subprocess.check_call(['systemctl','enable','--now',NAMES[1],NAMES[2],NAMES[4]])
    manifest['activated']=True;save(deployment/'manifest.json',manifest)


def switch(deployment):
    """Activate an existing installation without a baseline scan or job reset."""
    manifest=json.loads((deployment/'manifest.json').read_text())
    if CURRENT.resolve()!=ROOT/'releases'/manifest['commit']:raise RuntimeError('release_drift')
    with closing(sqlite3.connect('file:'+str(ROOT/'state.sqlite3')+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT value FROM metadata WHERE key='baseline_at'").fetchone():raise RuntimeError('baseline_missing')
    subprocess.check_call(['systemctl','stop',NAMES[1]])
    # SIGTERM drains the worker's actual per-account operation, preserving all
    # intents/receipts. No publishing owner or portrait collector is restarted.
    subprocess.check_call(['systemctl','restart',NAMES[2]])
    subprocess.check_call(['systemctl','enable','--now',NAMES[1],NAMES[4]])
    manifest.update(activated=True,activation_kind='schedule_update')
    save(deployment/'manifest.json',manifest)
    print(json.dumps(dict(activated=True,baseline_retained=True,scan=SCAN_CALENDAR,report=REPORT_CALENDAR)))


def rollback(deployment):
    manifest=json.loads((deployment/'manifest.json').read_text())
    if CURRENT.resolve()!=ROOT/'releases'/manifest['commit']:raise RuntimeError('newer_release_present')
    subprocess.check_call(['systemctl','disable','--now',NAMES[1]])
    subprocess.check_call(['systemctl','disable','--now',NAMES[4]])
    subprocess.check_call(['systemctl','disable','--now',NAMES[2]])
    for name,body in manifest['previous_files'].items():
        path=Path(name)
        if body is None:
            if path.exists():path.unlink()
        else:path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body,encoding='utf-8')
    if manifest['previous_current']:
        temp=CURRENT.with_name('current.rollback');temp.symlink_to(manifest['previous_current']);temp.replace(CURRENT)
    elif CURRENT.is_symlink():CURRENT.unlink()
    subprocess.check_call(['systemctl','daemon-reload'])
    if manifest.get('activation_kind')=='schedule_update':
        states=manifest.get('previous_states',{})
        if states.get(NAMES[4],{}).get('enabled') not in ('enabled','enabled-runtime'):
            # Going back to the hourly sender must not replay unsent daily cards.
            with closing(sqlite3.connect(ROOT/'state.sqlite3')) as db, db:
                db.execute("UPDATE notifications SET status='recorded' WHERE run_id LIKE 'daily-%' AND status IN ('pending','failed')")
        for name in (NAMES[2],NAMES[1],NAMES[4]):
            if states.get(name,{}).get('enabled') in ('enabled','enabled-runtime'):
                subprocess.check_call(['systemctl','enable',name])
            if states.get(name,{}).get('active')=='active':
                subprocess.check_call(['systemctl','start',name])
    print(json.dumps({'rolled_back':True,'business_state_retained':str(ROOT)}))


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['install','activate','switch','rollback']);p.add_argument('--source',type=Path);p.add_argument('--commit');p.add_argument('--deployment',required=True,type=Path);a=p.parse_args()
    if a.command=='install':install(a.source,a.commit,a.deployment)
    elif a.command=='activate':activate(a.deployment)
    elif a.command=='switch':switch(a.deployment)
    else:rollback(a.deployment)


if __name__=='__main__':main()

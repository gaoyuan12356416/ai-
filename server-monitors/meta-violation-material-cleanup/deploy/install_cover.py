#!/usr/bin/env python3
"""Install an exact GitHub release; never restore an old operational ledger."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path('/mnt/data-disk/meta-violation-material-cleanup')
WEB = Path('/root/drama_material_service')
SHARED = Path('/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3')
MODULE = Path('server-monitors/meta-violation-material-cleanup')
STORE = Path('features/fb_ad_asset_delete/store.py')
BASE_STORE = '5dd9211758d96c7b3c780c90b1287526f23de3b205b88c8d6ca860126b78adae'
UNITS = ('meta-cover-scan.service','meta-cover-scan.timer','meta-cover-worker.service',
         'meta-cover-notify.service','meta-cover-notify.timer')
OLD_UNITS = ('meta-violation-material-cleanup.service','meta-violation-material-cleanup.timer')


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024*1024), b''):
            value.update(part)
    return value.hexdigest()


def atomic(source, target):
    temp = target.with_name('.' + target.name + '.cover-install')
    shutil.copy2(str(source), str(temp))
    os.replace(str(temp), str(target))


def disk():
    uuid = subprocess.check_output(['findmnt','-n','-o','UUID','--target','/mnt/data-disk'], text=True).strip()
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8' or shutil.disk_usage('/mnt/data-disk').free < 2*1024**3:
        raise RuntimeError('data_disk_unavailable')


def idle():
    for unit in (OLD_UNITS[0], 'meta-cover-worker.service','meta-cover-scan.service'):
        result = subprocess.run(['systemctl','is-active','--quiet',unit])
        if result.returncode == 0:
            raise RuntimeError('service_active:' + unit)
    with closing(sqlite3.connect(SHARED.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute("SELECT 1 FROM fb_asset_delete_v2_runs WHERE status='running' LIMIT 1").fetchone():
            raise RuntimeError('shared_delete_run_active')


def snapshot(source, target):
    if source.exists():
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(str(target))) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup_integrity_failed')


def prepare(release):
    disk(); idle()
    release = release.resolve()
    if ROOT/'releases' not in release.parents:
        raise RuntimeError('invalid_release_path')
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=release,text=True).strip()
    if subprocess.check_output(['git','status','--porcelain'],cwd=release,text=True).strip():
        raise RuntimeError('release_dirty')
    if digest(WEB/STORE) not in (BASE_STORE,digest(release/STORE)):
        raise RuntimeError('web_store_baseline_changed')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT/'backups'/('cover-'+stamp+'-'+commit[:12])
    backup.mkdir(parents=True,mode=0o700)
    shutil.copy2(str(ROOT/'config.json'),str(backup/'config.json'))
    shutil.copy2(str(WEB/STORE),str(backup/'web-store.py'))
    state = {}
    for unit in UNITS + OLD_UNITS:
        path = Path('/etc/systemd/system')/unit
        if path.exists():
            shutil.copy2(str(path),str(backup/unit))
        state[unit] = {field:subprocess.run(['systemctl',field,unit],capture_output=True,text=True).stdout.strip()
                       for field in ('is-enabled','is-active')}
    snapshot(SHARED,backup/'shared-tasks.sqlite3')
    for source,name in ((ROOT/'runs.sqlite3','legacy-runs.sqlite3'),
                        (ROOT/'cover.sqlite3','cover.sqlite3'),
                        (ROOT/'cover-audit/runs.sqlite3','cover-audit.sqlite3')):
        snapshot(source,backup/name)
    plan = {'release':str(release),'commit':commit,'previous_release':str((ROOT/'current').resolve()),
            'backup':str(backup),'unit_state':state,'web_store_before':digest(WEB/STORE)}
    (backup/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    (backup/'hashes.json').write_text(json.dumps({p.name:digest(p) for p in backup.iterdir() if p.is_file()},indent=2))
    print(json.dumps({'backup':str(backup),'commit':commit}))


def switch(backup, rollback=False):
    disk()
    backup = backup.resolve()
    if ROOT/'backups' not in backup.parents:
        raise RuntimeError('invalid_backup_path')
    plan = json.loads((backup/'plan.json').read_text())
    for name,expected in json.loads((backup/'hashes.json').read_text()).items():
        if digest(backup/name) != expected:
            raise RuntimeError('backup_changed')
    if rollback:
        run(['systemctl','disable','--now','meta-cover-scan.timer','meta-cover-notify.timer','meta-cover-worker.service'])
        run(['systemctl','stop','meta-cover-scan.service','meta-cover-notify.service'])
    idle()
    run(['systemctl','disable','--now',OLD_UNITS[1]])
    release = Path(plan['release'])
    if ROOT/'releases' not in release.resolve().parents:
        raise RuntimeError('invalid_release_path')
    if (subprocess.check_output(['git','rev-parse','HEAD'],cwd=release,text=True).strip() != plan['commit'] or
            subprocess.check_output(['git','status','--porcelain'],cwd=release,text=True).strip()):
        raise RuntimeError('release_changed_since_backup')
    config = json.loads((backup/'config.json').read_text())
    config.update(live_enabled=False,cover_live_enabled=not rollback,
                  execution_workers=32,execution_per_account=2,execution_max_rps=24,
                  execution_batch_size=64,execution_context_ttl_seconds=30)
    if not rollback:
        if digest(WEB/STORE) not in (plan['web_store_before'],digest(release/STORE)):
            raise RuntimeError('web_store_changed_since_backup')
        atomic(release/STORE,WEB/STORE)
        for unit in UNITS:
            atomic(release/MODULE/'deploy'/unit,Path('/etc/systemd/system')/unit)
    # Keep the additive shared-store fencing adapter on rollback: the older web
    # adapter cannot see unresolved cover reservations. All ledgers stay latest.
    temp = ROOT/'.cover-config.json'
    temp.write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
    os.chmod(str(temp),0o600)
    os.replace(str(temp),str(ROOT/'config.json'))
    target = plan['previous_release'] if rollback else plan['release']
    link = ROOT/'.current-cover'
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    os.replace(str(link),str(ROOT/'current'))
    run(['systemctl','daemon-reload'])
    if not rollback:
        run(['bash',str(release/'scripts/safe_restart_drama_api.sh')])
    print(json.dumps({'action':'rollback' if rollback else 'applied','commit':plan['commit'],
                      'backup':str(backup),'writes_started':False,'ledgers_restored':False}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('prepare','apply','rollback'))
    parser.add_argument('path',type=Path)
    args=parser.parse_args()
    prepare(args.path) if args.mode=='prepare' else switch(args.path,args.mode=='rollback')

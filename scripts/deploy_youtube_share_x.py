#!/usr/bin/env python3
"""Narrow, GitHub-first deployment; existing YouTube/X ledgers are preserved."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import time
import urllib.request

MAIN = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
CURRENT = Path('/opt/x-post-automation/current')
OLD_RELEASE = Path('/mnt/data-disk/x-post-automation/releases/5f1d88455fb22634d27898f6762f9b2cd3768f70')
STORAGE = Path('/mnt/data-disk/x-post-automation/youtube-shares')
API, SIDE = 'drama-material-api.service', 'x-post-automation.service'
TIMERS = ['x-post-manual.timer', 'x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer']
MAIN_FILES = {
    'app.py': 'b3bc2dd43cedbe1b622fcdd2039c78a45724fd29c619cf171dec0e2db27cc4cd',
    'features/youtube_auto_publish/service.py': 'b01fa9447097b2df14977e717e34d18388d9cfa68df4d73cbe097eaa9133d9c3',
    'features/youtube_auto_publish/x_share.py': None,
    'features/x_accounts/client.py': 'a3d10ec8e87fd7dbe3dbe82ebaa6b2dd54e8db6eeba79e5a4776f742a472cb52',
    'features/x_accounts/youtube_share_text.py': None,
    'static/youtube-publish.html': '95db6a72fc5d736c0fc6c926dc4263c9071e6c302359d265a87f44e1104fdf06',
    'static/youtube-publish.js': 'd8464929a84f2a96f425256cea1d2b5f60f972f07004f826606a3fc8941c1b51',
    'static/youtube-publish.css': '616e90998295e62f278b4aaa77e83c946e750e45e8bd3f3bf8d6b3f8907843cf',
}
SIDE_FILES = {
    'features/x_accounts/oauth_service.py': '7ce8ee323de50de66ca93389650c9e99175863fa4a71430e455b9d1614080b6e',
    'features/x_accounts/client.py': MAIN_FILES['features/x_accounts/client.py'],
    'features/x_accounts/youtube_shares.py': None,
    'features/x_accounts/youtube_share_text.py': None,
}


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def install(source, target, mode=0o644):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.share-x-new')
    shutil.copyfile(source, temp)
    os.chmod(temp, mode)
    os.replace(temp, target)


def switch(target):
    link = CURRENT.with_name('current.youtube-share-new')
    if link.is_symlink(): link.unlink()
    link.symlink_to(target)
    os.replace(link, CURRENT)


def healthy():
    for _ in range(25):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                assert response.status == 200
            with urllib.request.urlopen('http://127.0.0.1:8810/health', timeout=2) as response:
                assert response.status == 200
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError('API/sidecar failed health check')


def assert_idle():
    for timer in TIMERS:
        state = run('systemctl', 'show', timer.replace('.timer', '.service'), '--property=ActiveState', '--value')
        if state in ('active', 'activating', 'deactivating'):
            raise RuntimeError('Publisher is working; leave it running: ' + timer)
    with sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro', uri=True) as db:
        if db.execute("SELECT 1 FROM x_post_publish_log WHERE status NOT IN ('published','failed','reserved') LIMIT 1").fetchone():
            raise RuntimeError('X publish attempt is in flight; do not interrupt')
    ledger = STORAGE / 'ledger.sqlite3'
    if ledger.exists():
        # A rollback must not abandon accepted shares or stop an active write.
        with sqlite3.connect('file:' + str(ledger) + '?mode=ro', uri=True) as db:
            for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                columns = [r[1] for r in db.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')]
                duplicate_filter = ' AND duplicate_of IS NULL' if 'duplicate_of' in columns else ''
                if 'status' in columns and db.execute('SELECT 1 FROM "' + table.replace('"', '""') + '" WHERE status IN (\'queued\',\'publishing\',\'running\')' + duplicate_filter + ' LIMIT 1').fetchone():
                    raise RuntimeError('Accepted YouTube shares are still pending; wait before restart/rollback')


def backup_db(source, destination):
    if not Path(source).is_file(): return
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst, pages=512, sleep=0.01)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database backup failed integrity check')
    os.chmod(destination, 0o600)


def restore(backup, manifest):
    for row in manifest['files']:
        target = Path(row['target'])
        if row['before'] is None:
            if target.exists(): target.unlink()
        else:
            saved = backup / 'files' / row['target'].lstrip('/')
            if sha(saved) != row['before']: raise RuntimeError('Backup integrity mismatch')
            install(saved, target, row['mode'])
    switch(Path(manifest['old_release']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk is not the expected mount')
    os.umask(0o077)
    with (BASE / 'youtube-share-x-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        timers = [unit for unit in TIMERS if run('systemctl', 'show', unit, '--property=ActiveState', '--value') == 'active']
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents: raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest.get('kind') != 'youtube-share-x' or CURRENT.resolve() != Path(manifest['new_release']):
                raise RuntimeError('Rollback does not match active release')
            if any(sha(row['target']) != row['after'] for row in manifest['files']):
                raise RuntimeError('Newer main code exists; do not overwrite')
            assert_idle()
            try:
                if timers: run('systemctl', 'stop', *timers)
                assert_idle()
                run('systemctl', 'stop', API)
                assert_idle()
                run('systemctl', 'stop', SIDE)
                restore(backup, manifest)
            finally:
                try:
                    run('systemctl', 'start', SIDE, API)
                    healthy()
                finally:
                    if timers: run('systemctl', 'start', *timers)
            print(json.dumps({'rollback': str(backup), 'ledgers': 'retained'}))
            return
        healthy()
        stage = Path(__file__).resolve().parents[1]
        if not args.commit or len(args.commit) != 40 or (stage / '.github-verified-commit').read_text().strip() != args.commit:
            raise RuntimeError('Must deploy an exact GitHub-fetched commit')
        inputs = [(stage / name, MAIN / name, expected) for name, expected in MAIN_FILES.items()]
        inputs += [(stage / name, PUBLIC / Path(name).name, expected) for name, expected in MAIN_FILES.items() if name.startswith('static/')]
        def baseline():
            if CURRENT.resolve() != OLD_RELEASE: raise RuntimeError('Sidecar baseline changed')
            for source, target, expected in inputs:
                if not source.is_file() or sha(target) != expected:
                    raise RuntimeError('Main baseline changed: ' + str(target))
            for name, expected in SIDE_FILES.items():
                if sha(OLD_RELEASE / name) != expected: raise RuntimeError('Sidecar code changed: ' + name)
        baseline()
        for file in set([p[0] for p in inputs] + [stage / name for name in SIDE_FILES]):
            if file.suffix == '.py': compile(file.read_bytes(), str(file), 'exec')
        assert_idle()
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'main_targets': len(inputs), 'sidecar_targets': len(SIDE_FILES)}))
            return
        if shutil.disk_usage(BASE).free < 2 * (MAIN / 'data/drama_material_jobs.sqlite3').stat().st_size + 512 * 1024 * 1024:
            raise RuntimeError('Insufficient backup space')
        backup = BASE / 'backups' / ('youtube-share-x-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        release = OLD_RELEASE.parent / (args.commit + '-youtube-share-x')
        if release.exists(): raise RuntimeError('New release directory already exists')
        shutil.copytree(OLD_RELEASE, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in SIDE_FILES: install(stage / name, release / name)
        for path in [release, release / 'features', release / 'features/x_accounts']: os.chmod(path, 0o755)
        user = pwd.getpwnam('x-post-automation')
        STORAGE.mkdir(mode=0o700, exist_ok=True)
        if STORAGE.is_symlink() or STORAGE.resolve().parent != Path('/mnt/data-disk/x-post-automation').resolve():
            raise RuntimeError('Invalid share storage path')
        os.chown(STORAGE, user.pw_uid, user.pw_gid); os.chmod(STORAGE, 0o700)
        run('runuser', '-u', 'x-post-automation', '--', '/usr/bin/python3', '-c',
            'import sys;sys.dont_write_bytecode=True;sys.path.insert(0,' + repr(str(release)) + ');from features.x_accounts.youtube_shares import *')
        records = []
        for source, target, _ in inputs:
            saved = backup / 'files' / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(target, saved)
            records.append({'target': str(target), 'before': sha(target), 'after': sha(source), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644})
        manifest = {'kind': 'youtube-share-x', 'commit': args.commit, 'files': records,
                    'old_release': str(OLD_RELEASE), 'new_release': str(release), 'timers': timers}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        changed = False
        try:
            if timers: run('systemctl', 'stop', *timers)
            assert_idle(); baseline()
            backup_db(MAIN / 'data/drama_material_jobs.sqlite3', backup / 'jobs.sqlite3')
            backup_db('/var/lib/x-post-automation/accounts.sqlite3', backup / 'x-accounts.sqlite3')
            backup_db(STORAGE / 'ledger.sqlite3', backup / 'youtube-shares.sqlite3')
            run('systemctl', 'stop', API)
            assert_idle()
            run('systemctl', 'stop', SIDE)
            baseline()
            changed = True
            for (source, target, _), row in zip(inputs, records): install(source, target, row['mode'])
            switch(release)
            run('systemctl', 'start', SIDE, API)
            healthy()
            for row in records:
                if sha(row['target']) != row['after']: raise RuntimeError('Installed hash mismatch')
        except BaseException:
            try:
                if changed:
                    run('systemctl', 'stop', API, SIDE)
                    restore(backup, manifest)
            finally:
                run('systemctl', 'start', SIDE, API)
                healthy()
            raise
        finally:
            if timers: run('systemctl', 'start', *timers)
        result = {'commit': args.commit, 'backup': str(backup), 'sidecar_release': str(release),
                  'services': {unit: run('systemctl', 'show', unit, '--property=ActiveState', '--value') for unit in [API, SIDE, 'youtube-auto-publish-worker.service']},
                  'timers_restored': timers, 'sha256': {r['target']: sha(r['target']) for r in records}}
        (backup / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


if __name__ == '__main__':
    main()

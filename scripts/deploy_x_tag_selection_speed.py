#!/usr/bin/env python3
"""Stage, gracefully drain, activate, or roll back only the tag control API."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

TRIGGERS = ('x-tag-auto-runner.timer', 'x-tag-auto-runner.path')
API = 'x-tag-auto.service'
RUNNER = 'x-tag-auto-runner.service'
PROTECTED = ('x-tag-post.service', 'x-post-automation.service',
             'pipeline-coordinator.service', 'pipeline-cos-native-x-tag.service')
BASE_FILES = ('features/x_auto_posts/service.py', 'features/x_auto_posts/core.py',
              'features/x_auto_posts/publisher.py', 'features/x_auto_posts/selection_recovery.py',
              'features/x_auto_posts/repositories.py', 'features/x_tag_publish/policy.py',
              'features/x_tag_publish/selection.py')
DROPINS = {API: Path('/etc/systemd/system/x-tag-auto.service.d/80-selection-speed.conf'),
           RUNNER: Path('/etc/systemd/system/x-tag-auto-runner.service.d/80-selection-speed.conf')}

def show(unit, prop):
    return subprocess.check_output(['systemctl', 'show', unit, '-p', prop, '--value'], text=True).strip()

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def systemctl(*args): subprocess.run(['systemctl', *args], check=True)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('prepare', 'drain', 'activate', 'rollback', 'verify'))
    p.add_argument('--deployment', type=Path, required=True)
    args = p.parse_args()
    directory = args.deployment.resolve()
    assert directory.is_relative_to('/mnt/data-disk/x-tag-selection-speed-20261118')
    manifest_path = directory / 'manifest.json'
    source = Path(__file__).resolve().parents[1]
    if args.action == 'prepare':
        uuid = subprocess.check_output(['findmnt', '-no', 'UUID', '/mnt/data-disk'], text=True).strip()
        assert uuid == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
        assert shutil.disk_usage('/mnt/data-disk').free > 5 * 1024**3
        directory.mkdir(parents=True, exist_ok=False)
        base = Path('/opt/x-tag-publisher/current').resolve(strict=True)
        manifest = {'source': str(source), 'base': str(base),
            'commit': subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),
            'base_hashes': {f: sha(base / f) for f in BASE_FILES},
            'triggers': {u: show(u, 'ActiveState') for u in TRIGGERS},
            'protected_pids': {u: show(u, 'MainPID') for u in PROTECTED},
            'api_before': show(API, 'MainPID'), 'dropins': {}}
        assert show(API, 'ActiveState') == 'active'
        assert show(API, 'TimeoutStopUSec') == 'infinity'
        for unit, path in DROPINS.items():
            old = directory / (unit + '.before.conf')
            if path.exists(): shutil.copy2(path, old)
            manifest['dropins'][unit] = {'path': str(path), 'before': str(old) if old.exists() else None}
        dbpath = '/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'
        with sqlite3.connect('file:' + dbpath + '?mode=ro', uri=True) as db:
            with sqlite3.connect(str(directory / 'auto-before.sqlite3')) as copy:
                db.backup(copy, pages=1024)
            manifest['event_floor'] = db.execute('SELECT max(id) FROM x_auto_event').fetchone()[0]
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(json.dumps({'prepared': str(manifest_path)})); return
    manifest = json.loads(manifest_path.read_text())
    assert str(source) == manifest['source']
    if args.action == 'drain':
        for unit in TRIGGERS: systemctl('stop', unit)
        # SIGTERM reaches the existing graceful HTTP shutdown: non-daemon
        # handlers finish their full selection/preparation request naturally.
        systemctl('stop', '--no-block', API)
        print(json.dumps({'draining': API, 'protected': list(PROTECTED)})); return
    if args.action in ('activate', 'rollback'):
        assert show(API, 'MainPID') == '0', 'control_api_still_draining'
        assert show(RUNNER, 'MainPID') == '0', 'runner_still_finishing'
        assert all(show(u, 'MainPID') == pid for u,pid in manifest['protected_pids'].items()), 'protected_process_changed'
        if args.action == 'activate':
            base = Path(manifest['base'])
            assert Path('/opt/x-tag-publisher/current').resolve() == base
            assert all(sha(base / f) == h for f,h in manifest['base_hashes'].items()), 'base_changed'
            settings = {
                # EnvironmentFile overrides Environment, so append the scoped
                # non-secret file after the original publisher environment.
                API: '[Service]\nEnvironmentFile=' + str(source / 'deploy/x-tag-selection-speed.env') + '\nEnvironment=X_TAG_SELECTION_BASE=' + str(base) + '\nExecStart=\nExecStart=/usr/bin/python3 ' + str(source / 'scripts/x_tag_selection_service.py') + '\n',
                RUNNER: '[Service]\nEnvironmentFile=' + str(source / 'deploy/x-tag-selection-speed.env') + '\n'}
            for unit, data in settings.items():
                target = DROPINS[unit]; target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix('.new'); tmp.write_text(data); tmp.chmod(0o644); tmp.replace(target)
                manifest['dropins'][unit]['after_sha256'] = sha(target)
            manifest_path.write_text(json.dumps(manifest, indent=2))
        else:
            for item in manifest['dropins'].values():
                path = Path(item['path'])
                assert sha(path) == item['after_sha256'], 'dropin_changed'
                if item['before']: shutil.copy2(item['before'], path)
                else: path.unlink()
        systemctl('daemon-reload')
        systemctl('start', API)
        assert show(API, 'ActiveState') == 'active'
        for attempt in range(20):
            try:
                with urllib.request.urlopen('http://127.0.0.1:18843/health', timeout=2) as response:
                    assert response.status == 200
                break
            except OSError:
                if attempt == 19: raise
                time.sleep(.5)
        for unit, state in manifest['triggers'].items():
            if state == 'active': systemctl('start', unit)
        print(json.dumps({'action': args.action, 'api_pid': show(API, 'MainPID')})); return
    print(json.dumps({'api': show(API, 'ActiveState'),
        'triggers': {u: show(u, 'ActiveState') for u in TRIGGERS},
        'protected_pids_unchanged': {u: show(u, 'MainPID') == pid for u,pid in manifest['protected_pids'].items()},
        'commit': manifest['commit']}))

if __name__ == '__main__': main()

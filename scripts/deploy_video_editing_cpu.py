"""Install only the editor's CPU files into the verified composite runtime.

Run from a clean GitHub checkout. The caller pins live app and navigation hashes.
Rollback after any editor task was accepted means disabling intake, not removing
the mixed GPU reader or restoring database snapshots.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

from patch_video_editing import patch_app, patch_nav

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/video-editing')
CONFIG = Path('/etc/video-editing')
UNIT = 'drama-material-api.service'
PROTECTED = ('drama-material-job-worker.service', 'drama-youtube-unified-writer.service',
             'youtube-auto-publish-worker.service')


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(path.read_bytes()) if path.exists() else None


def atomic(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.ve-new')
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def require_idle():
    conn = sqlite3.connect('file:' + str(ROOT / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True)
    try:
        for table in ('drama_material_job', 'drama_screenshot_job'):
            count = conn.execute("SELECT count(*) FROM " + table + " WHERE status NOT IN ('done','failed')").fetchone()[0]
            if count:
                raise RuntimeError('Wait for existing work before API restart: ' + table)
    finally:
        conn.close()


def healthy():
    for attempt in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Main API health verification failed')


def prepare(stage, expected_app, expected_nav):
    plans = {}
    def add(path, data, expected=None, guard_new=False, mode=0o644):
        old = sha(path)
        if (expected is not None and old != expected) or (guard_new and old is not None and old != digest(data)):
            raise RuntimeError('Unreviewed live file drift: ' + str(path))
        if path.is_symlink():
            raise RuntimeError('Unexpected deployment symlink: ' + str(path))
        if path.suffix == '.py':
            compile(data, str(path), 'exec')
        plans[path] = {'old': old, 'new': digest(data), 'data': data,
                       'mode': (path.stat().st_mode & 0o777) if path.exists() else mode}
    app = ROOT / 'app.py'
    add(app, patch_app(app.read_text(encoding='utf-8')).encode('utf-8'), expected_app)
    for nav in (ROOT / 'static/quick-nav.js', PUBLIC / 'quick-nav.js'):
        add(nav, patch_nav(nav.read_text(encoding='utf-8')).encode('utf-8'), expected_nav)
    for source in sorted((stage / 'features/video_editing').glob('*.py')):
        add(ROOT / 'features/video_editing' / source.name, source.read_bytes(), guard_new=True)
    for name in ('video-editing.html', 'video-editing.js', 'video-editing.css'):
        data = (stage / 'static' / name).read_bytes()
        add(ROOT / 'static' / name, data, guard_new=True)
        add(PUBLIC / name, data, guard_new=True)
    add(ROOT / 'scripts/video_editing_worker.py', (stage / 'scripts/video_editing_worker.py').read_bytes(), guard_new=True)
    add(Path('/etc/systemd/system/video-editing-worker.service'),
        (stage / 'deploy/video-editing-worker.service').read_bytes(), guard_new=True)
    add(Path('/etc/systemd/system/drama-material-api.service.d/99-zzzz-video-editing.conf'),
        b'[Service]\nEnvironmentFile=/etc/video-editing/video-editing.env\n', guard_new=True)
    config = b'''VIDEO_EDITING_DB=/mnt/data-disk/video-editing/state.sqlite3
VIDEO_EDITING_COS_PREFIX=video-editing/v1
VIDEO_EDITING_GPU_URL=http://127.0.0.1:18788
VIDEO_EDITING_ENABLED=1
VIDEO_EDITING_DISABLED_FILE=/etc/video-editing/intake.disabled
'''
    add(CONFIG / 'video-editing.env', config, guard_new=True, mode=0o600)
    return plans


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-app', required=True)
    parser.add_argument('--expected-nav', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('CPU data disk mount mismatch')
    stage = Path(__file__).resolve().parents[1]
    commit = run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    if run('git', '-C', str(stage), 'status', '--porcelain'):
        raise RuntimeError('Release source is not clean')
    plans = prepare(stage, args.expected_app, args.expected_nav)
    if not args.apply:
        print(json.dumps({'preflight': 'passed', 'commit': commit, 'files': {
            str(path): {'before': row['old'], 'after': row['new']} for path, row in plans.items()}}))
        return
    import fcntl
    BASE.mkdir(parents=True, exist_ok=True)
    with (BASE / 'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_idle()
        protected = {u: run('systemctl', 'show', u, '-p', 'MainPID', '-p', 'ActiveState') for u in PROTECTED}
        backup = BASE / 'backups' / (time.strftime('%Y%m%dT%H%M%S') + '-' + commit[:12])
        backup.mkdir(parents=True)
        manifest = {'commit': commit, 'files': {}, 'status': 'prepared'}
        for path, row in plans.items():
            if row['old']:
                saved = backup / 'files' / path.relative_to('/')
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
                if sha(saved) != row['old']:
                    raise RuntimeError('Backup verification failed')
            manifest['files'][str(path)] = {k: v for k, v in row.items() if k != 'data'}
        manifest_path = backup / 'manifest.json'
        atomic(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
        db = ROOT / 'data/drama_material_jobs.sqlite3'
        with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as source:
            with sqlite3.connect(str(backup / 'existing-jobs-evidence.sqlite3')) as destination:
                source.backup(destination)
        for path, row in plans.items():
            if sha(path) != row['old']:
                raise RuntimeError('Live file changed before install: ' + str(path))
        atomic(CONFIG / 'intake.disabled', b'Pending isolated production acceptance\n', 0o600)
        installed = []
        try:
            run('systemctl', 'stop', UNIT)
            require_idle()
            for path, row in plans.items():
                atomic(path, row['data'], row['mode'])
                installed.append(path)
                if sha(path) != row['new']:
                    raise RuntimeError('Installed file hash mismatch')
            run('systemctl', 'daemon-reload')
            run('systemctl', 'start', UNIT)
            healthy()
        except BaseException:
            # New intake is still disabled; no editor execution is admitted.
            for path in reversed(installed):
                row = plans[path]
                if row['old']:
                    atomic(path, (backup / 'files' / path.relative_to('/')).read_bytes(), row['mode'])
                else:
                    path.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')
            run('systemctl', 'start', UNIT)
            healthy()
            manifest['status'] = 'failed_restored'
            atomic(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
            raise
        for unit, before in protected.items():
            if run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState') != before:
                raise RuntimeError('Unrelated service identity changed: ' + unit)
        manifest['status'] = 'installed_intake_disabled'
        atomic(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
        print(json.dumps({'installed': commit, 'backup': str(backup), 'intake': 'disabled',
                          'api': run('systemctl', 'is-active', UNIT)}))


if __name__ == '__main__':
    main()

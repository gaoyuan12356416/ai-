"""Install only the video-editing material-source change from a clean GitHub commit.

Dry-run is the default. Full rollback is allowed only before any upload ledger row
exists; after the first click, disable new intake and keep the worker reconciling.
No database or original COS object is ever restored by this installer.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


BASE_COMMIT = 'fd85308518c49678e5f62a8fee6cd7b886212499'
RELEASE_REF = 'refs/heads/codex/video-editing-material-source-20261023'
STAGE = Path(__file__).resolve().parents[1]
LIVE = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
DATA = Path('/mnt/data-disk/video-editing')
CONFIG = Path('/etc/video-editing/video-editing.env')
UNIT_FILE = Path('/etc/systemd/system/video-editing-material-source.service')
INTAKE_MARKER = Path('/etc/video-editing/material-source.disabled')
API_UNIT = 'drama-material-api.service'
WORKER_UNIT = 'video-editing-material-source.service'
PROTECTED_UNITS = (
    'video-editing-worker.service', 'drama-material-job-worker.service',
    'drama-youtube-unified-writer.service', 'youtube-auto-publish-worker.service',
)
PACKAGE_FILES = (
    'features/video_editing/routes.py',
    'features/video_editing/service.py',
    'features/video_editing/store.py',
    'features/video_editing/material_source.py',
    'scripts/video_editing_material_source_worker.py',
)
STATIC_FILES = ('video-editing.css', 'video-editing.js', 'video-editing.html')
CONFIG_VALUES = {
    'VIDEO_EDITING_MATERIAL_COS_BUCKET': 'advertising-1306474899',
    'VIDEO_EDITING_MATERIAL_COS_REGION': 'ap-hongkong',
    'VIDEO_EDITING_MATERIAL_COS_DOMAIN': 'advertising-1306474899.cos.ap-hongkong.myqcloud.com',
    'VIDEO_EDITING_MATERIAL_COS_PREFIX': 'video-editing/material-source/v1',
    'VIDEO_EDITING_MATERIAL_SOURCE_DISABLED_FILE': str(INTAKE_MARKER),
}


def command(*args, timeout=60):
    return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=timeout).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    return sha(path.read_bytes()) if path.exists() else None


def atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('Unexpected symlink: ' + str(path))
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + '.material-source-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_data_disk(require_space=True):
    uuid = command('findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk').decode()
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('CPU data-disk mount mismatch')
    if require_space and shutil.disk_usage('/mnt/data-disk').free < 2 * 1024**3:
        raise RuntimeError('Insufficient free data-disk space')


def candidate_commit():
    commit = command('git', '-C', str(STAGE), 'rev-parse', 'HEAD').decode()
    if command('git', '-C', str(STAGE), 'status', '--porcelain', '--untracked-files=all'):
        raise RuntimeError('Release source is not a clean GitHub checkout')
    if subprocess.call(('git', '-C', str(STAGE), 'merge-base', '--is-ancestor', BASE_COMMIT, commit)):
        raise RuntimeError('Release does not descend from the live editor version')
    origin = command('git', '-C', str(STAGE), 'remote', 'get-url', 'origin').decode()
    if origin not in {'git@github.com:gaoyuan12356416/ai-.git',
                      'https://github.com/gaoyuan12356416/ai-.git'}:
        raise RuntimeError('Release checkout is not the expected GitHub repository')
    remote = command('git', '-C', str(STAGE), 'ls-remote', 'origin', RELEASE_REF, timeout=45).decode().split()
    if len(remote) != 2 or remote[0] != commit or remote[1] != RELEASE_REF:
        raise RuntimeError('Release HEAD is not the pushed GitHub ref')
    return commit


def old_git_bytes(relative):
    result = subprocess.run(('git', '-C', str(STAGE), 'show', BASE_COMMIT + ':' + relative),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout if result.returncode == 0 else None


def configured_bytes():
    if not CONFIG.is_file() or CONFIG.is_symlink():
        raise RuntimeError('Existing video-editing environment is missing or linked')
    content = CONFIG.read_text(encoding='utf-8')
    present = {}
    for line in content.splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key in CONFIG_VALUES:
            if key in present:
                raise RuntimeError('Duplicate material-source setting: ' + key)
            present[key] = value.strip().strip('"\'')
    for key, expected in CONFIG_VALUES.items():
        if key in present and present[key] != expected:
            raise RuntimeError('Unreviewed material-source setting: ' + key)
    missing = [key + '=' + value for key, value in CONFIG_VALUES.items() if key not in present]
    if not missing:
        return CONFIG.read_bytes()
    return (content.rstrip('\n') + '\n\n# Material-source public COS copy.\n'
            + '\n'.join(missing) + '\n').encode('utf-8')


def build_plan():
    plan = []

    def add(target, source, expected_old, mode=0o644):
        if target.is_symlink():
            raise RuntimeError('Unexpected deployment symlink: ' + str(target))
        actual = file_sha(target)
        expected = sha(expected_old) if expected_old is not None else None
        if actual != expected:
            raise RuntimeError('Unreviewed live file drift: ' + str(target))
        if target.suffix == '.py':
            compile(source, str(target), 'exec')
        plan.append({'path': str(target), 'before': actual, 'after': sha(source),
                     'mode': target.stat().st_mode & 0o777 if target.exists() else mode,
                     'data': source})

    for relative in PACKAGE_FILES:
        add(LIVE / relative, (STAGE / relative).read_bytes(), old_git_bytes(relative))
    for name in STATIC_FILES:
        relative = 'static/' + name
        source = (STAGE / relative).read_bytes()
        old = old_git_bytes(relative)
        add(LIVE / relative, source, old)
        add(PUBLIC / name, source, old)
    add(UNIT_FILE, (STAGE / 'deploy/video-editing-material-source.service').read_bytes(), None)
    add(CONFIG, configured_bytes(), CONFIG.read_bytes(), 0o600)
    return plan


def ledger_rows():
    db = DATA / 'state.sqlite3'
    with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ve_material_upload'").fetchone()
        return connection.execute('SELECT count(*) FROM ve_material_upload').fetchone()[0] if table else 0


def service_state():
    return {unit: command('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState').decode()
            for unit in PROTECTED_UNITS}


def active_state(properties):
    return next((line.split('=', 1)[1] for line in properties.splitlines()
                 if line.startswith('ActiveState=')), '')


def health(require_material_route=True):
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status != 200 or not isinstance(json.load(response), dict):
                    continue
            try:
                with urllib.request.urlopen('http://127.0.0.1:8787/api/video-editing/config', timeout=2):
                    pass
                continue
            except urllib.error.HTTPError as error:
                with error:
                    if error.code != 401 or not isinstance(json.load(error), dict):
                        continue
            if not require_material_route:
                return
            probe = urllib.request.Request(
                'http://127.0.0.1:8787/api/video-editing/tasks/ve_' + '0' * 32 + '/material-source',
                data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
            try:
                urllib.request.urlopen(probe, timeout=2)
            except urllib.error.HTTPError as error:
                with error:
                    if error.code == 401 and isinstance(json.load(error), dict):
                        return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError('Main API health verification failed')


def manifest_write(path, manifest):
    atomic(path, json.dumps(manifest, indent=2, sort_keys=True).encode('utf-8'), 0o600)


def restore(manifest_path, manifest):
    files = manifest['files']
    backup = Path(manifest_path).parent
    candidates = []
    # Validate and save the installed candidate before taking the API offline.
    # A failed preflight must leave the running service untouched.
    for index, row in enumerate(files):
        path = Path(row['path'])
        if path.is_symlink() or file_sha(path) != row['after']:
            raise RuntimeError('Current file changed after install: ' + str(path))
        if row['before'] is not None and file_sha(backup / row['backup']) != row['before']:
            raise RuntimeError('Backup checksum mismatch: ' + str(path))
        saved = backup / ('rollback-candidate-' + str(index))
        shutil.copy2(path, saved)
        if file_sha(saved) != row['after']:
            raise RuntimeError('Candidate snapshot mismatch: ' + str(path))
        candidates.append((row, saved))
    atomic(INTAKE_MARKER, b'Material-source rollback in progress\n', 0o644)
    # Stopping the API drains or terminates requests that passed the marker
    # before it appeared. The ledger check is safe only after this boundary.
    command('systemctl', 'stop', API_UNIT)
    try:
        if ledger_rows():
            command('systemctl', 'start', API_UNIT)
            health()
            raise RuntimeError('Upload ledger is nonempty; keep the worker for reconciliation')
        command('systemctl', 'disable', '--now', WORKER_UNIT)
        for row in reversed(files):
            path = Path(row['path'])
            if row['before'] is None:
                path.unlink()
            else:
                atomic(path, (backup / row['backup']).read_bytes(), row['mode'])
        command('systemctl', 'daemon-reload')
        command('systemctl', 'start', API_UNIT)
        health(require_material_route=False)
        manifest['status'] = 'restored_no_uploads'
        manifest_write(manifest_path, manifest)
    except BaseException:
        if not ledger_rows():
            try:
                for row, saved in candidates:
                    atomic(Path(row['path']), saved.read_bytes(), row['mode'])
                command('systemctl', 'daemon-reload')
                command('systemctl', 'enable', '--now', WORKER_UNIT)
                command('systemctl', 'restart', API_UNIT)
                health()
                manifest['status'] = 'rollback_failed_candidate_restored'
            except BaseException:
                manifest['status'] = 'rollback_failed_manual_recovery_needed'
            manifest_write(manifest_path, manifest)
        raise


def apply(commit, plan):
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if INTAKE_MARKER.exists() or ledger_rows():
            raise RuntimeError('Material-source intake is disabled or upload ledger already has rows')
        protected = service_state()
        backup = DATA / 'backups' / ('material-source-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + commit[:12])
        backup.mkdir(mode=0o700)
        manifest = {'commit': commit, 'base_commit': BASE_COMMIT, 'status': 'prepared',
                    'protected': protected, 'files': []}
        for index, row in enumerate(plan):
            entry = {key: value for key, value in row.items() if key != 'data'}
            if row['before'] is not None:
                saved = backup / ('file-' + str(index))
                shutil.copy2(row['path'], saved)
                if file_sha(saved) != row['before']:
                    raise RuntimeError('Backup verification failed: ' + row['path'])
                entry['backup'] = saved.name
            manifest['files'].append(entry)
        with sqlite3.connect('file:' + str(DATA / 'state.sqlite3') + '?mode=ro', uri=True) as source:
            with sqlite3.connect(str(backup / 'state-before.sqlite3')) as destination:
                source.backup(destination)
        manifest_path = backup / 'manifest.json'
        manifest_write(manifest_path, manifest)
        installed = []
        try:
            for row in plan:
                if file_sha(Path(row['path'])) != row['before']:
                    raise RuntimeError('Live file changed before installation: ' + row['path'])
            # Keep the new button hidden until the API and independent worker pass health.
            backend = [row for row in plan if not Path(row['path']).name.startswith('video-editing.')
                       or Path(row['path']) == UNIT_FILE or Path(row['path']) == CONFIG]
            frontend = [row for row in plan if row not in backend]
            for row in backend:
                atomic(Path(row['path']), row['data'], row['mode'])
                installed.append(row)
            command('systemctl', 'daemon-reload')
            command('systemctl', 'restart', API_UNIT)
            health()
            command('systemctl', 'enable', '--now', WORKER_UNIT)
            time.sleep(2)
            if (command('systemctl', 'is-active', WORKER_UNIT).decode() != 'active' or
                    int(command('systemctl', 'show', WORKER_UNIT, '-p', 'MainPID', '--value')) <= 0):
                raise RuntimeError('Material-source worker did not start')
            for row in frontend:
                atomic(Path(row['path']), row['data'], row['mode'])
                installed.append(row)
            for row in plan:
                if file_sha(Path(row['path'])) != row['after']:
                    raise RuntimeError('Installed hash mismatch: ' + row['path'])
            after = service_state()
            manifest['protected_after'] = after
            if any(active_state(after[unit]) != active_state(before)
                   for unit, before in protected.items()):
                raise RuntimeError('Unrelated worker active state changed')
            manifest['status'] = 'installed'
            manifest_write(manifest_path, manifest)
            print(json.dumps({'commit': commit, 'backup': str(backup), 'status': 'installed',
                              'api': command('systemctl', 'is-active', API_UNIT).decode(),
                              'worker': command('systemctl', 'is-active', WORKER_UNIT).decode()}))
        except BaseException:
            atomic(INTAKE_MARKER, b'Material-source deployment needs reconciliation\n', 0o644)
            command('systemctl', 'stop', API_UNIT)
            try:
                if ledger_rows() == 0:
                    # Restore exactly the paths already written, including unit/config.
                    subprocess.run(('systemctl', 'disable', '--now', WORKER_UNIT),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    for row in reversed(installed):
                        path = Path(row['path'])
                        entry = next(item for item in manifest['files'] if item['path'] == row['path'])
                        if row['before'] is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic(path, (backup / entry['backup']).read_bytes(), row['mode'])
                    command('systemctl', 'daemon-reload')
                    command('systemctl', 'restart', API_UNIT)
                    health(require_material_route=False)
                    manifest['status'] = 'failed_restored'
                else:
                    manifest['status'] = 'failed_preserved_for_reconciliation'
                    command('systemctl', 'start', API_UNIT)
                    health()
            except BaseException:
                # A second failure must not silently leave a mixed code version.
                # Rebuild the complete candidate with intake disabled if possible.
                try:
                    for row in plan:
                        atomic(Path(row['path']), row['data'], row['mode'])
                    command('systemctl', 'daemon-reload')
                    command('systemctl', 'enable', '--now', WORKER_UNIT)
                    command('systemctl', 'start', API_UNIT)
                    health()
                    manifest['status'] = 'failed_candidate_restored_intake_disabled'
                except BaseException:
                    manifest['status'] = 'failed_manual_recovery_needed'
            manifest_write(manifest_path, manifest)
            raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--rollback', metavar='MANIFEST')
    args = parser.parse_args()
    require_data_disk(require_space=not bool(args.rollback))
    if args.rollback:
        import fcntl
        manifest_path = Path(args.rollback).resolve()
        if DATA / 'backups' not in manifest_path.parents or manifest_path.name != 'manifest.json':
            raise RuntimeError('Rollback manifest must be in the video-editing backup directory')
        manifest = json.loads(manifest_path.read_text())
        if manifest['status'] != 'installed':
            raise RuntimeError('Backup is not an installed release')
        with (DATA / 'deployment.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restore(manifest_path, manifest)
        print(json.dumps({'rollback': 'restored_no_uploads', 'backup': str(manifest_path.parent)}))
        return
    commit = candidate_commit()
    plan = build_plan()
    print(json.dumps({'commit': commit, 'base_commit': BASE_COMMIT, 'preflight': 'passed',
                      'files': [{'path': row['path'], 'before': row['before'], 'after': row['after']}
                                for row in plan]}))
    if args.apply:
        apply(commit, plan)


if __name__ == '__main__':
    main()

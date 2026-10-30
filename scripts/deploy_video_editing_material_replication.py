#!/usr/bin/env python3
"""Scoped CPU rollout from an exact pushed checkout; default is read-only check.

Install with the new feature closed, import/reconcile historical receipts, then
explicitly enable. Rollback restores files, never SQLite business records.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BASE = '120f03167f7fe76531baf6617a7e4f905e7d73be'
REF = 'refs/heads/codex/video-editing-material-replication-20261030'
LIVE = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
DATA = Path('/mnt/data-disk/video-editing')
DB = DATA / 'state.sqlite3'
CONFIG = Path('/etc/video-editing/video-editing.env')
SECRET_ENV = Path('/etc/drama-material-service/a-group-expansion.env')
MARKER = Path('/etc/video-editing/material-replication.disabled')
MARKER_TEXT = b'material replication rollout: intake disabled\n'
UNIT = Path('/etc/systemd/system/video-editing-material-replication.service')
API = 'drama-material-api.service'
WORKER = 'video-editing-material-replication.service'
PROTECTED = ('video-editing-worker.service', 'video-editing-material-source.service')
FILES = (
    'features/video_editing/routes.py',
    'features/video_editing/service.py',
    'features/video_editing/store.py',
    'features/video_editing/material_replication.py',
    'scripts/video_editing_material_replication_worker.py',
    'scripts/import_video_editing_material_replication_history.py',
)
STATIC = ('video-editing.html', 'video-editing.js', 'video-editing.css')
ENV_VALUES = {
    'VIDEO_EDITING_MATERIAL_REPLICATION_ENABLED': '1',
    'VIDEO_EDITING_MATERIAL_REPLICATION_DISABLED_FILE': str(MARKER),
}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args, timeout=60, binary=False, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, **kwargs)
    # Command output can contain credentials. Never include it in exceptions.
    require(result.returncode == 0, 'command_failed:' + Path(str(args[0])).name)
    return result.stdout if binary else result.stdout.decode('utf-8').strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    path = Path(path)
    require(not path.is_symlink(), 'symlink_refused:' + str(path))
    return digest(path.read_bytes()) if path.exists() else None


def atomic(path, data, mode=0o644):
    path = Path(path)
    require(not path.is_symlink(), 'symlink_refused:' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.replication-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, data):
    atomic(path, json.dumps(data, sort_keys=True, indent=2).encode(), 0o600)


def storage(require_space=True):
    require(run('findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk') ==
            '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'data_disk_mismatch')
    require(DATA.is_dir() and not DATA.is_symlink(), 'data_directory_missing_or_linked')
    require(not (DATA / 'backups').is_symlink(), 'backup_directory_linked')
    if require_space:
        require(shutil.disk_usage('/mnt/data-disk').free >= 2 * 1024**3,
                'insufficient_data_disk_space')
    require(DB.is_file() and not DB.is_symlink(), 'database_missing_or_linked')


def published(expected):
    require(re.fullmatch(r'[0-9a-f]{40}', expected or ''), 'exact_commit_required')
    rev = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    require(rev == expected, 'checkout_commit_mismatch')
    require(not run('git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=all'),
            'dirty_release_checkout')
    require(run('git', '-C', str(ROOT), 'remote', 'get-url', 'origin') in {
        'git@github.com:gaoyuan12356416/ai-.git',
        'https://github.com/gaoyuan12356416/ai-.git'}, 'unexpected_repository')
    require(run('git', '-C', str(ROOT), 'ls-remote', 'origin', REF).split() == [rev, REF],
            'commit_not_pushed_to_release_ref')
    run('git', '-C', str(ROOT), 'merge-base', '--is-ancestor', BASE, rev)
    return rev


def git_bytes(rev, relative, optional=False):
    result = subprocess.run(('git', '-C', str(ROOT), 'show', rev + ':' + relative),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if optional and result.returncode:
        return None
    require(result.returncode == 0, 'source_missing:' + relative)
    return result.stdout


def target_sources():
    result = [(LIVE / name, name) for name in FILES]
    result += [(directory / name, 'static/' + name) for name in STATIC
               for directory in (LIVE / 'static', PUBLIC)]
    result.append((UNIT, 'deploy/video-editing-material-replication.service'))
    return result


def configured_bytes():
    require(CONFIG.is_file() and not CONFIG.is_symlink(), 'editor_config_missing_or_linked')
    text = CONFIG.read_text(encoding='utf-8')
    seen = set()
    lines = []
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip() if '=' in line else ''
        if key in ENV_VALUES:
            require(key not in seen, 'duplicate_replication_setting:' + key)
            seen.add(key)
            lines.append(key + '=' + ENV_VALUES[key])
        else:
            lines.append(line)
    lines += [key + '=' + value for key, value in ENV_VALUES.items() if key not in seen]
    return ('\n'.join(lines).rstrip('\n') + '\n').encode('utf-8')


def secret_preflight():
    require(SECRET_ENV.is_file() and not SECRET_ENV.is_symlink(), 'worker_secret_env_missing_or_linked')
    stat = SECRET_ENV.stat()
    require(stat.st_uid == 0 and stat.st_mode & 0o777 == 0o600, 'worker_secret_env_permissions')
    # Only stat the secret file: deployment never reads token bytes.
    api_env = run('systemctl', 'show', API, '-p', 'EnvironmentFiles', '--value')
    require(str(CONFIG) in api_env, 'api_missing_editor_environment')
    require(str(SECRET_ENV) not in api_env, 'api_must_not_read_worker_secret_env')


def build_plan(commit, overrides=None):
    overrides = overrides or {}
    allowed = {str(path) for path, _ in target_sources()}
    require(set(overrides) <= allowed, 'unexpected_before_hash_path')
    plan = []
    for target, source in target_sources():
        content = git_bytes(commit, source)
        old = git_bytes(BASE, source, optional=True)
        before = digest(old) if old is not None else None
        if str(target) in overrides:
            before = overrides[str(target)]
            require(before is None or re.fullmatch(r'[0-9a-f]{64}', before), 'invalid_before_hash')
        require(file_sha(target) == before, 'live_file_drift:' + str(target))
        if target.suffix == '.py':
            compile(content, str(target), 'exec')
        if target == UNIT:
            require(('EnvironmentFile=' + str(SECRET_ENV)).encode() in content,
                    'worker_secret_environment_missing')
        plan.append({'path': str(target), 'source': source, 'before': before,
                     'after': digest(content), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644,
                     'data': content})
    config = configured_bytes()
    plan.append({'path': str(CONFIG), 'source': None, 'before': file_sha(CONFIG),
                 'after': digest(config), 'mode': CONFIG.stat().st_mode & 0o777,
                 'install_mode': 0o600, 'data': config})
    require(not MARKER.exists() or file_sha(MARKER) == digest(MARKER_TEXT), 'foreign_replication_marker')
    return plan


def state(unit):
    value = run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID', '-p', 'UnitFileState')
    return dict(line.split('=', 1) for line in value.splitlines() if '=' in line)


def protected_state():
    return {unit: state(unit) for unit in PROTECTED}


def check_protected(before):
    after = protected_state()
    for unit in PROTECTED:
        for key in ('ActiveState', 'MainPID'):
            require(after[unit].get(key) == before[unit].get(key), 'protected_worker_changed:' + unit)
    return after


def backup_database(destination):
    with closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=20)) as source:
        with closing(sqlite3.connect(str(destination))) as target:
            source.backup(target)
    destination.chmod(0o600)
    integrity(destination)


def integrity(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=20)) as db:
        require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'sqlite_integrity_failed')


def table_hashes(path, tables=None):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=20)) as db:
        db.execute('BEGIN')
        names = tables if tables is not None else [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for name in names:
            require(re.fullmatch(r'[A-Za-z0-9_]+', name), 'unexpected_sqlite_table')
            columns = [row[1] for row in db.execute('PRAGMA table_info("' + name + '")')]
            order = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            checksum = hashlib.sha256()
            count = 0
            for row in db.execute('SELECT * FROM "' + name + '" ORDER BY ' + order):
                checksum.update(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=repr).encode())
                count += 1
            result[name] = {'rows': count, 'sha256': checksum.hexdigest()}
        return result


def migrate(path):
    code = ('import sys;sys.path.insert(0,sys.argv[1]);'
            'from features.video_editing.store import Store;Store(sys.argv[2])')
    run(sys.executable, '-B', '-c', code, str(ROOT), str(path),
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    integrity(path)


def rehearse(backup):
    original = backup / 'state-before.sqlite3'
    rehearsal = backup / 'migration-rehearsal.sqlite3'
    backup_database(original)
    shutil.copy2(original, rehearsal)
    before = table_hashes(rehearsal)
    migrate(rehearsal)
    require(table_hashes(rehearsal, before) == before, 'migration_changed_existing_ledger')
    require('ve_material_replication' in table_hashes(rehearsal), 'replication_table_missing')
    return {'backup_sha256': file_sha(original), 'existing_tables': before, 'result': 'passed'}


def health():
    for _ in range(15):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError('api_health_failed')


def close_gate():
    if not MARKER.exists():
        atomic(MARKER, MARKER_TEXT)
    require(MARKER.is_file() and not MARKER.is_symlink(), 'replication_marker_invalid')


def restore_api(before):
    if before.get('ActiveState') == 'active':
        run('systemctl', 'restart', API)
        health()
    else:
        run('systemctl', 'stop', API)


def verify_backup(rows, backup, *, installed=True):
    allowed = {str(path) for path, _ in target_sources()} | {str(CONFIG)}
    require(len({row['path'] for row in rows}) == len(rows), 'duplicate_manifest_path')
    for row in rows:
        require(row['path'] in allowed, 'manifest_path_outside_allowlist')
        path = Path(row['path'])
        if installed:
            require(file_sha(path) == row['after'], 'installed_file_changed:' + row['path'])
        if row['before'] is not None:
            require(re.fullmatch(r'file-[0-9]+', row.get('backup', '')), 'invalid_backup_name')
            require(file_sha(backup / row['backup']) == row['before'], 'backup_checksum_mismatch:' + row['path'])


def restore_files(rows, backup):
    verify_backup(rows, backup)
    for row in reversed(rows):
        path = Path(row['path'])
        if row['before'] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, (backup / row['backup']).read_bytes(), row['mode'])


def apply(commit, plan):
    protected = protected_state()
    api_before = state(API)
    require(api_before.get('ActiveState') in {'active', 'inactive'}, 'api_state_not_stable')
    backup = DATA / 'backups' / ('material-replication-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
                               + '-' + commit[:12])
    backup.mkdir(parents=True, mode=0o700)
    manifest_path = backup / 'manifest.json'
    manifest = {'commit': commit, 'base': BASE, 'status': 'prepared', 'files': [],
                'api_before': api_before, 'worker_before': state(WORKER), 'protected_before': protected,
                'marker_before': file_sha(MARKER)}
    for index, row in enumerate(plan):
        require(file_sha(row['path']) == row['before'], 'live_file_changed_before_backup:' + row['path'])
        item = {key: value for key, value in row.items() if key != 'data'}
        if row['before'] is not None:
            saved = backup / ('file-' + str(index))
            shutil.copy2(row['path'], saved)
            saved.chmod(0o600)
            require(file_sha(saved) == row['before'], 'backup_checksum_mismatch:' + row['path'])
            item['backup'] = saved.name
        manifest['files'].append(item)
    manifest['migration_rehearsal'] = rehearse(backup)
    save(manifest_path, manifest)
    installed = []
    try:
        close_gate()
        for row in plan:
            require(file_sha(row['path']) == row['before'], 'live_file_changed_before_install:' + row['path'])
        for row in plan:
            atomic(row['path'], row['data'], row.get('install_mode', row['mode']))
            installed.append(next(item for item in manifest['files'] if item['path'] == row['path']))
        migrate(DB)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', API)
        health()
        run('systemctl', 'enable', '--now', WORKER)
        time.sleep(1)
        worker = state(WORKER)
        require(worker.get('ActiveState') == 'active' and int(worker.get('MainPID', '0')) > 0,
                'replication_worker_not_running')
        verify_backup(manifest['files'], backup)
        manifest['protected_after'] = check_protected(protected)
        manifest['status'] = 'installed_disabled'
        save(manifest_path, manifest)
        return {'status': manifest['status'], 'commit': commit, 'manifest': str(manifest_path)}
    except BaseException:
        close_gate()
        try:
            verify_backup(installed, backup)
            # A failure before unit installation must still restore earlier files.
            # systemctl disable rejects a genuinely absent unit, so do not call it.
            if UNIT.exists():
                run('systemctl', 'daemon-reload')
                run('systemctl', 'disable', '--now', WORKER)
            elif state(WORKER).get('ActiveState') == 'active':
                run('systemctl', 'stop', WORKER)
            run('systemctl', 'stop', API)
            restore_files(installed, backup)
            run('systemctl', 'daemon-reload')
            restore_api(api_before)
            check_protected(protected)
            manifest['status'] = 'failed_files_restored_ledger_preserved'
        except BaseException:
            manifest['status'] = 'failed_manual_recovery_required_intake_closed'
        save(manifest_path, manifest)
        raise


def load_manifest(path):
    path = Path(path).resolve()
    require((DATA / 'backups').resolve() in path.parents and path.name == 'manifest.json',
            'manifest_outside_backup_directory')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    require(manifest.get('status') in {'installed_disabled', 'enabled'}, 'manifest_not_installed')
    require({row['path'] for row in manifest['files']} ==
            {str(target) for target, _ in target_sources()} | {str(CONFIG)}, 'incomplete_manifest')
    return path, manifest


def enable(manifest_path, manifest):
    verify_backup(manifest['files'], manifest_path.parent)
    # Reconcile a lost acknowledgement after unlink but before manifest write.
    require(file_sha(MARKER) in {digest(MARKER_TEXT), None}, 'foreign_replication_marker')
    secret_preflight()
    worker = state(WORKER)
    require(worker.get('ActiveState') == 'active' and int(worker.get('MainPID', '0')) > 0,
            'replication_worker_not_running')
    health()
    check_protected(manifest['protected_before'])
    # Caller completes historical import + reuse verification before this explicit phase.
    MARKER.unlink(missing_ok=True)
    manifest['status'] = 'enabled'
    manifest['enabled_at'] = int(time.time())
    save(manifest_path, manifest)
    return {'status': 'enabled', 'commit': manifest['commit'], 'manifest': str(manifest_path)}


def rollback(manifest_path, manifest):
    verify_backup(manifest['files'], manifest_path.parent)
    protected = protected_state()
    close_gate()
    try:
        run('systemctl', 'disable', '--now', WORKER)
        run('systemctl', 'stop', API)
        restore_files(manifest['files'], manifest_path.parent)
        run('systemctl', 'daemon-reload')
        restore_api(manifest['api_before'])
        check_protected(protected)
        integrity(DB)
        manifest['status'] = 'rolled_back_ledger_preserved'
    except BaseException:
        manifest['status'] = 'rollback_failed_manual_recovery_required_intake_closed'
        save(manifest_path, manifest)
        raise
    save(manifest_path, manifest)
    return {'status': manifest['status'], 'manifest': str(manifest_path)}


@contextmanager
def deployment_lock():
    import fcntl
    with (DATA / 'deployment.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--enable', metavar='MANIFEST')
    action.add_argument('--rollback', metavar='MANIFEST')
    parser.add_argument('--commit', help='exact 40-character pushed commit; required for check/apply')
    parser.add_argument('--expected-before', type=Path,
                        help='reviewed JSON absolute-target -> SHA256/null overrides; allowlisted paths only')
    args = parser.parse_args(argv)
    storage(require_space=not bool(args.rollback or args.enable))
    if args.rollback or args.enable:
        path, manifest = load_manifest(args.rollback or args.enable)
        with deployment_lock():
            result = rollback(path, manifest) if args.rollback else enable(path, manifest)
    else:
        commit = published(args.commit)
        secret_preflight()
        overrides = json.loads(args.expected_before.read_text(encoding='utf-8')) if args.expected_before else {}
        plan = build_plan(commit, overrides)
        result = {'status': 'preflight_passed', 'commit': commit,
                  'files': [{key: row[key] for key in ('path', 'before', 'after')} for row in plan]}
        if args.apply:
            with deployment_lock():
                result = apply(commit, plan)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only our controlled error strings are safe; subprocess output is never printed.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print(json.dumps({'status': 'failed', 'error': message}), file=sys.stderr)
        sys.exit(1)

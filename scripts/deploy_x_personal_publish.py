#!/usr/bin/env python3
"""Stage and activate a reviewed overlay on the live composite, never publish X posts.

The baseline and every overlay byte must be committed. Database backups are
rehearsal/recovery evidence only: rollback deliberately never restores a ledger.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
BASELINE = SOURCE / 'deploy/x-personal-baseline.json'
OVERLAY = SOURCE / 'deploy/x-personal-overlay'
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
SERVICES = ['drama-material-api.service', 'x-post-automation.service', 'x-auto-post-service.service']
TRIGGERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer',
            'x-post-daily.timer', 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer',
            'x-auto-post-metric.timer', 'x-auto-post-runner.path']
WORKERS = [unit.replace('.timer', '.service') for unit in TRIGGERS if unit.endswith('.timer')]
POOL = 'x-video-pool-scheduler.service'
LOCKS = ['/run/x-post-daily/runner.lock', '/run/x-auto-post/scheduler.lock']
ENV_FILE = Path('/etc/x-post-automation.env')
POOL_ENV = Path('/etc/x-video-publish.env')
POOL_UNIT = Path('/etc/systemd/system') / POOL
DEFAULT_PATHS = {
    'current': '/opt/x-post-automation/current', 'data': '/mnt/data-disk/x-post-automation',
    'main': '/root/drama_material_service', 'public': '/usr/share/nginx/html',
    'post_db': '/var/lib/x-post-automation/accounts.sqlite3',
    'auto_db': '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3',
    'youtube_db': '/mnt/data-disk/x-post-automation/youtube-shares/ledger.sqlite3',
    'tokens': '/var/lib/x-post-automation/tokens',
    'system': '/',
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, timeout=240, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if check and result.returncode:
        # Commands may touch credential-bearing config: never echo their output.
        raise RuntimeError('command failed: ' + ' '.join(str(value) for value in args[:3]))
    return result.stdout.strip()


def digest(path, mode='raw'):
    if not path.exists():
        return None
    data = path.read_bytes()
    if mode == 'lf':
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def relative(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and value and '..' not in path.parts
            and '\\' not in value and ':' not in value, 'unsafe overlay path')
    return Path(value)


def load_baseline():
    config = json.loads(BASELINE.read_text())
    require(config.get('version') == 1, 'unsupported baseline')
    config['paths'] = {**DEFAULT_PATHS, **config.get('paths', {})}
    require(config['paths'].get('main_db'), 'baseline must name main_db')
    require(all(Path(v).is_absolute() for v in config['paths'].values()), 'absolute paths required')
    require({'main', 'sidecar', 'public'} <= set(config['files']) <= {'main', 'sidecar', 'public', 'system'}, 'invalid overlay groups')
    for group, files in config['files'].items():
        require(isinstance(files, dict) and files, 'empty overlay group')
        for name, old_hash in files.items():
            relative(name)
            require(old_hash is None or re.fullmatch('[a-f0-9]{64}', old_hash), 'invalid baseline hash')
        actual = {p.relative_to(OVERLAY / group).as_posix() for p in (OVERLAY / group).rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        require(actual == set(files), 'baseline/overlay manifest differs: ' + group)
    return config


def source_commit():
    require(not run('git', '-C', str(SOURCE), 'status', '--porcelain'), 'source checkout is dirty')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    require(run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit), 'commit is not on a fetched remote branch')
    for path in [BASELINE, Path(__file__), *OVERLAY.rglob('*'), SOURCE / 'deploy/x-video-pool-scheduler.service']:
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            run('git', '-C', str(SOURCE), 'ls-files', '--error-unmatch', str(path.relative_to(SOURCE)))
    return commit


def preflight_disk(config):
    require(run('findmnt', '-no', 'UUID', '/mnt/data-disk') == UUID, 'data disk mount mismatch')
    require(Path(config['paths']['data']).resolve().is_relative_to(Path('/mnt/data-disk')), 'data path escaped mount')
    require(shutil.disk_usage(config['paths']['data']).free > 5 * 1024**3, 'less than 5 GiB free')


@contextlib.contextmanager
def deployment_lock(config):
    """Serialize stage/cutover/rollback for this deployment on the data disk."""
    import fcntl
    preflight_disk(config)
    path = Path(config['paths']['data']) / '.x-personal-deploy.lock'
    require(not path.is_symlink(), 'deployment lock must not be a symlink')
    with path.open('a+b') as handle:
        path.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another personal X deployment operation is running') from None
        yield


def roots(config):
    return {group: Path(config['paths']['current' if group == 'sidecar' else group]) for group in config['files']}


def verify_baseline(config):
    for group, root in roots(config).items():
        for name, expected in config['files'][group].items():
            require(digest(root / relative(name), config.get('hash_mode', 'raw')) == expected,
                    'live baseline drift: ' + str(root / name))


def atomic_copy(source, target, *, preserve_owner=True):
    target.parent.mkdir(parents=True, exist_ok=True)
    require(not target.is_symlink(), 'refusing to overwrite symlink: ' + str(target))
    previous = target.stat() if target.exists() else None
    temp = target.with_name(target.name + '.x-personal-next')
    require(not temp.exists() and not temp.is_symlink(), 'stale copy temporary file')
    shutil.copy2(source, temp)
    if previous is not None and preserve_owner:
        os.chown(temp, previous.st_uid, previous.st_gid)
        temp.chmod(previous.st_mode & 0o7777)
    temp.replace(target)


def save(manifest):
    path = Path(manifest['backup']) / 'manifest.json'
    temp = path.with_suffix('.json.next')
    temp.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def snapshot(source, target):
    require(source.exists(), 'database missing: ' + str(source))
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=1000, sleep=0.01)
    target.chmod(0o600)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def database_facts(path, projection=None, *, tables=None, user_startup=False):
    """Hash only original columns, so additive defaults do not alter old evidence."""
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        conn.execute('PRAGMA query_only=ON')
        require(conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'database quick_check failed')
        require(not conn.execute('PRAGMA foreign_key_check').fetchall(), 'database foreign_key_check failed')
        if projection is None:
            projection = {name: [r[1] for r in conn.execute('PRAGMA table_info(' + quote(name) + ')')]
                          for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
                          if tables is None or name in tables}
        if user_startup:
            # Existing startup refreshes updated_at unconditionally. Keep every
            # other user field (including session-sourced avatars) under guard.
            projection = {name: [c for c in columns if not (name == 'drama_admin_user' and c == 'updated_at')]
                          for name, columns in projection.items()}
        facts = {}
        for table, columns in projection.items():
            # Order by the complete projection also works for WITHOUT ROWID tables.
            fields = ','.join(quote(c) for c in columns)
            checksum, count = hashlib.sha256(), 0
            for row in conn.execute('SELECT ' + fields + ' FROM ' + quote(table) + ' ORDER BY ' + fields):
                values = [({'blob': v.hex()} if isinstance(v, bytes) else v) for v in row]
                if user_startup and table == 'drama_admin_user' and values[columns.index('role')] == 'admin':
                    index = columns.index('permissions_json')
                    permissions = json.loads(values[index])
                    require(isinstance(permissions, dict), 'invalid admin permission map')
                    if 'x_video_publish' in permissions:
                        require(permissions['x_video_publish'] is True, 'unexpected personal publishing admin grant')
                        permissions.pop('x_video_publish')
                    # Only the new admin module grant is expected from startup.
                    values[index] = json.dumps(permissions, sort_keys=True, separators=(',', ':'))
                checksum.update(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode() + b'\n')
                count += 1
            facts[table] = {'count': count, 'sha256': checksum.hexdigest()}
        return {'columns': projection, 'tables': facts}


def db_paths(config):
    result, resolved = {}, set()
    for key in ('post_db', 'accounts_db', 'auto_db', 'main_db', 'youtube_db'):
        value = config['paths'].get(key)
        if value and str(Path(value).resolve()) not in resolved:
            result[key] = Path(value)
            resolved.add(str(Path(value).resolve()))
    return result


def token_facts(config):
    root = Path(config['paths']['tokens'])
    require(root.is_dir(), 'token directory missing')
    return {p.relative_to(root).as_posix(): {'sha256': digest(p), 'mode': p.stat().st_mode & 0o7777,
            'uid': p.stat().st_uid, 'gid': p.stat().st_gid}
            for p in sorted(root.rglob('*')) if p.is_file()}


def external_groups(config):
    return [group for group in ('main', 'public', 'system') if group in config['files']]


def reload_nginx(config):
    if config['files'].get('system'):
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')


def unit_state(unit):
    data = run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'LoadState')
    return dict(line.split('=', 1) for line in data.splitlines() if '=' in line)


def is_busy(unit):
    return unit_state(unit).get('ActiveState') in {'active', 'activating', 'deactivating'}


def wait_idle(check, label, timeout=180):
    deadline = time.monotonic() + timeout
    while check():
        require(time.monotonic() < deadline, label + ' still active; aborted without killing work')
        time.sleep(2)


def inflight(config):
    checks = [('post_db', 'x_post_queue', 'status', ['publishing']),
              ('youtube_db', 'youtube_share_item', 'status', ['queued', 'publishing'])]
    for key, table, column, states in checks:
        path = Path(config['paths'][key])
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            if conn.execute('SELECT 1 FROM ' + quote(table) + ' WHERE ' + quote(column) + ' IN (' + ','.join('?' for _ in states) + ') LIMIT 1', states).fetchone():
                return True
    return False


def personal_rows(config):
    with contextlib.closing(sqlite3.connect(Path(config['paths']['post_db']).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_video_publish_task'").fetchone():
            return 0
        return conn.execute('SELECT COUNT(*) FROM x_video_publish_task').fetchone()[0]


def restore_trigger_states(states):
    for unit, previous in states.items():
        if previous.get('ActiveState') == 'active':
            run('systemctl', 'start', unit)
        elif is_busy(unit):
            run('systemctl', 'stop', unit)
    for unit, previous in states.items():
        require(unit_state(unit).get('ActiveState') == previous.get('ActiveState'), 'trigger restore failed: ' + unit)


@contextlib.contextmanager
def paused(config, manifest):
    import fcntl
    # Long GPU preparation must not hold unrelated Auto/metric timers paused.
    # Wait for a quiet window first, then drain the short trigger-stop race.
    drain_timeout = int(config.get('drain_timeout_seconds', 180))
    require(180 <= drain_timeout <= 7500, 'invalid worker drain timeout')
    wait_idle(lambda: any(is_busy(u) for u in WORKERS), 'publisher worker', timeout=drain_timeout)
    states = {unit: unit_state(unit) for unit in TRIGGERS}
    manifest['trigger_states'] = states
    save(manifest)
    handles = []
    try:
        active = [u for u, state in states.items() if state.get('ActiveState') == 'active']
        if active:
            run('systemctl', 'stop', *active)
        wait_idle(lambda: any(is_busy(u) for u in WORKERS), 'publisher worker', timeout=180)
        for value in LOCKS:
            path = Path(value)
            require(path.parent.is_dir(), 'shared lock directory missing')
            handle = path.open('a+b')
            handles.append(handle)
            def locked():
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return False
                except BlockingIOError:
                    return True
            wait_idle(locked, 'shared publishing lock')
        wait_idle(lambda: inflight(config), 'X / YouTube share request')
        yield
    finally:
        for handle in handles:
            try:
                saved, current = os.fstat(handle.fileno()), Path(handle.name).stat()
                if (saved.st_dev, saved.st_ino) != (current.st_dev, current.st_ino):
                    manifest['status'] = 'recovery_required'
                    manifest['recovery_reason'] = 'shared lock inode changed'
            except OSError:
                manifest['status'] = 'recovery_required'
                manifest['recovery_reason'] = 'shared lock path disappeared'
        for handle in reversed(handles):
            handle.close()
        # A failed safe rollback must keep claimers paused, not run mixed code.
        if manifest.get('status') != 'recovery_required':
            restore_trigger_states(states)
            manifest['triggers_restored'] = True
            save(manifest)
        else:
            save(manifest)
            raise RuntimeError('recovery required; original triggers remain paused')


def switch(config, target):
    link = Path(config['paths']['current'])
    require(link.is_symlink(), 'current release must be a symlink')
    temp = link.with_name(link.name + '.personal-next')
    require(not temp.exists() and not temp.is_symlink(), 'stale release switch')
    temp.symlink_to(target)
    temp.replace(link)


def backup_file(path, backup):
    value = {'exists': path.exists(), 'sha256': digest(path)}
    require(not path.is_symlink(), 'external target is a symlink: ' + str(path))
    if value['exists']:
        stat = path.stat()
        value.update(mode=stat.st_mode & 0o7777, uid=stat.st_uid, gid=stat.st_gid)
        target = backup / 'files' / path.as_posix().lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return value


def backup_units(manifest):
    """Capture unit fragments/drop-ins for evidence without restoring unrelated files."""
    directory = Path(manifest['backup']) / 'service-units'
    directory.mkdir(mode=0o700)
    for unit in [*SERVICES, *TRIGGERS, *WORKERS, POOL]:
        # systemctl cat reads both vendor fragments and all effective drop-ins.
        content = run('systemctl', 'cat', unit, check=False)
        if content:
            target = directory / (unit + '.effective.txt')
            target.write_text(content + '\n')
            target.chmod(0o600)
        environment_files = run('systemctl', 'show', unit, '-p', 'EnvironmentFiles', '--value', check=False)
        for name in re.findall(r'(?:^|\s)(/[^\s]+)\s+\(ignore_errors=', environment_files):
            original = Path(name)
            if original.is_file():
                target = directory / 'environment-files' / name.lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
                target.chmod(0o600)
        for target in [Path('/etc/systemd/system') / unit, Path('/etc/systemd/system') / (unit + '.d')]:
            if target.exists() or target.is_symlink():
                saved = directory / target.name
                if target.is_dir():
                    shutil.copytree(target, saved, symlinks=True, dirs_exist_ok=True)
                elif target.is_symlink():
                    if not saved.exists() and not saved.is_symlink():
                        saved.symlink_to(os.readlink(target))
                else:
                    shutil.copy2(target, saved)


def set_env(path, key, value):
    body = path.read_text() if path.exists() else ''
    pattern = r'(?m)^' + re.escape(key) + r'=.*$'
    require(len(re.findall(pattern, body)) <= 1, 'duplicate configuration key: ' + key)
    body = re.sub(pattern, key + '=' + value, body) if re.search(pattern, body) else body.rstrip('\n') + '\n' + key + '=' + value + '\n'
    temp = path.with_name(path.name + '.personal-next')
    require(not temp.exists(), 'stale environment temporary file')
    temp.write_text(body)
    temp.chmod(path.stat().st_mode & 0o7777 if path.exists() else 0o600)
    if path.exists():
        os.chown(temp, path.stat().st_uid, path.stat().st_gid)
    temp.replace(path)


def health(config, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            require(all(unit_state(u).get('ActiveState') == 'active' for u in SERVICES), 'service inactive')
            for port, path in [(8810, '/health'), (18833, '/health'), (8787, '/api/ui/topbar')]:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=3) as response:
                        body = response.read(65536)
                        if port == 8810:
                            require(body.strip() == b'ok', 'sidecar unhealthy')
                        elif port == 18833:
                            require(json.loads(body).get('ok') is True, 'Auto unhealthy')
                except urllib.error.HTTPError as exc:
                    require(port == 8787 and exc.code in {401, 403}, 'HTTP health failed')
            return
        except (OSError, ValueError, RuntimeError):
            time.sleep(1)
    raise RuntimeError('service health timeout')


def stage(config):
    commit = source_commit()
    preflight_disk(config)
    verify_baseline(config)
    require(all(unit_state(u).get('ActiveState') == 'active' for u in SERVICES), 'required service is not active')
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-personal-' + commit[:10]
    data = Path(config['paths']['data'])
    backup, release = data / 'backups' / stamp, data / 'releases' / stamp
    backup.mkdir(mode=0o700)
    previous = Path(config['paths']['current']).resolve(strict=True)
    shutil.copytree(previous, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in config['files']['sidecar']:
        atomic_copy(OVERLAY / 'sidecar' / name, release / name)
    staged_overlay = backup / 'overlay'
    shutil.copytree(OVERLAY, staged_overlay, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(SOURCE / 'deploy/x-video-pool-scheduler.service', backup / 'pool-scheduler.service')
    manifest = dict(version=1, commit=commit, backup=str(backup), release=str(release), previous_release=str(previous),
                    status='staging', baseline=config, overlay_hashes={group: {name: digest(staged_overlay / group / name)
                    for name in files} for group, files in config['files'].items()},
                    pool_unit_hash=digest(backup / 'pool-scheduler.service'))
    save(manifest)
    rehearsal = backup / 'rehearsal'
    rehearsal.mkdir(mode=0o700)
    copied_post = rehearsal / 'posts.sqlite3'
    snapshot(Path(config['paths']['post_db']), copied_post)
    before = database_facts(copied_post)
    program = ('import sys; from features.x_posts.service import ensure_storage; '
               'from features.x_video_publish.core import PersonalStore; '
               'from features.x_video_publish.pool import install_schema; '
               'ensure_storage(sys.argv[1]); PersonalStore(sys.argv[1]); '
               'ensure_storage(sys.argv[1]); store=PersonalStore(sys.argv[1]); '
               'conn=store.connect(); install_schema(conn); install_schema(conn); conn.close()')
    with (rehearsal / 'migration.log').open('w') as output:
        subprocess.run([sys.executable, '-c', program, str(copied_post)], cwd=release,
                       check=True, stdout=output, stderr=output, timeout=180)
    require(database_facts(copied_post, before['columns']) == before, 'migration changed original data')
    manifest['migration_legacy_unchanged'] = True
    manifest['rehearsal_projection'] = before
    for group, files in config['files'].items():
        for name in files:
            if name.endswith('.py'):
                compile((staged_overlay / group / name).read_text(encoding='utf-8-sig'), name, 'exec')
    manifest['status'] = 'staged'
    save(manifest)
    print(json.dumps({k: manifest[k] for k in ('status', 'commit', 'backup', 'release')}), flush=True)


def load_manifest(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    require(Path(manifest['backup']).resolve() == path.resolve(), 'manifest location mismatch')
    return manifest


def verify_staged(manifest):
    backup = Path(manifest['backup'])
    require(digest(backup / 'pool-scheduler.service') == manifest['pool_unit_hash'], 'staged unit drift')
    for group, files in manifest['overlay_hashes'].items():
        for name, expected in files.items():
            require(digest(backup / 'overlay' / group / name) == expected, 'staged overlay drift')
            if group == 'sidecar':
                require(digest(Path(manifest['release']) / name) == expected, 'staged release drift')


def restore_files(manifest):
    for name, facts in manifest['external_files'].items():
        target = Path(name)
        if facts['exists']:
            atomic_copy(Path(manifest['backup']) / 'files' / name.lstrip('/'), target, preserve_owner=False)
            target.chmod(facts['mode'])
            os.chown(target, facts['uid'], facts['gid'])
        elif target.exists():
            require(not target.is_symlink() and target.is_file(), 'unexpected rollback target')
            target.unlink()


def restore_pool_state(manifest):
    previous = manifest['pool_state']
    run('systemctl', 'daemon-reload')
    if previous.get('UnitFileState') == 'enabled':
        run('systemctl', 'enable', POOL)
    else:
        run('systemctl', 'disable', POOL, check=False)
    if previous.get('ActiveState') == 'active':
        run('systemctl', 'start', POOL)


def restore_code(manifest):
    config = manifest['baseline']
    run('systemctl', 'stop', POOL, check=False)
    if manifest['pool_state'].get('UnitFileState') != 'enabled':
        run('systemctl', 'disable', POOL, check=False)
    run('systemctl', 'stop', SERVICES[0])
    wait_idle(lambda: inflight(config), 'X / YouTube share before rollback')
    require(personal_rows(config) == 0, 'personal tasks exist: keep new executor and reconcile; no destructive rollback')
    for unit in reversed(SERVICES[1:]):
        run('systemctl', 'stop', unit)
    restore_files(manifest)
    switch(config, Path(manifest['previous_release']))
    run('systemctl', 'daemon-reload')
    for unit in SERVICES:
        run('systemctl', 'start', unit)
    health(config)
    reload_nginx(config)
    restore_pool_state(manifest)


def activate(manifest, enable_publishing=False, enable_pool=False):
    config = manifest['baseline']
    preflight_disk(config)
    require(manifest['status'] == 'staged', 'only a staged release may activate')
    require(not enable_pool or enable_publishing, 'pool scheduler requires publishing enabled')
    require(Path(config['paths']['current']).resolve() == Path(manifest['previous_release']), 'current release drift')
    verify_staged(manifest)
    verify_baseline(config)
    manifest['service_states'] = {u: unit_state(u) for u in SERVICES}
    require(all(s.get('ActiveState') == 'active' for s in manifest['service_states'].values()), 'service state drift')
    manifest['pool_state'] = unit_state(POOL)
    require(manifest['pool_state'].get('ActiveState') != 'active', 'existing personal scheduler active; review separately')
    backup = Path(manifest['backup'])
    with paused(config, manifest):
        require(Path(config['paths']['current']).resolve() == Path(manifest['previous_release']),
                'current release changed during drain; restage on the new composite')
        verify_baseline(config)
        require(personal_rows(config) == 0, 'personal tasks already exist before first rollout')
        targets = [Path(config['paths'][g]) / n for g in external_groups(config) for n in config['files'][g]]
        targets += [ENV_FILE, POOL_ENV, POOL_UNIT]
        manifest['external_files'] = {str(p): backup_file(p, backup) for p in targets}
        backup_units(manifest)
        # Take the large main database online, before the short service cutover.
        snapshot(Path(config['paths']['main_db']), backup / 'main_db.sqlite3')
        main_tables = config.get('main_invariant_tables', ['drama_admin_user'])
        main_before = database_facts(Path(config['paths']['main_db']), tables=main_tables, user_startup=True)
        stopped = False
        try:
            # Close browser mutation entry first, then recheck the independent worker.
            run('systemctl', 'stop', SERVICES[0])
            stopped = True
            wait_idle(lambda: inflight(config), 'final X / YouTube share request')
            for unit in reversed(SERVICES[1:]):
                run('systemctl', 'stop', unit)
            manifest['tokens_before'] = token_facts(config)
            with tarfile.open(backup / 'tokens.tar', 'w') as archive:
                archive.add(config['paths']['tokens'], arcname='tokens', recursive=True)
            (backup / 'tokens.tar').chmod(0o600)
            manifest['database_before'] = {}
            for key, path in db_paths(config).items():
                if key == 'main_db':
                    manifest['database_before'][key] = main_before
                    continue
                snapshot(path, backup / (key + '.sqlite3'))
                manifest['database_before'][key] = database_facts(path)
            manifest['status'] = 'activating'
            save(manifest)
            for group in external_groups(config):
                for name in config['files'][group]:
                    atomic_copy(backup / 'overlay' / group / name, Path(config['paths'][group]) / name)
            set_env(ENV_FILE, 'X_VIDEO_PUBLISH_ENABLED', '0')
            set_env(POOL_ENV, 'X_VIDEO_PUBLISH_POOL_SCHEDULER_ENABLED', '1' if enable_pool else '0')
            atomic_copy(backup / 'pool-scheduler.service', POOL_UNIT)
            switch(config, Path(manifest['release']))
            run('systemctl', 'daemon-reload')
            if config['files'].get('system'):
                run('nginx', '-t')
            for unit in SERVICES:
                run('systemctl', 'start', unit)
            health(config)
            for key, path in db_paths(config).items():
                before = manifest['database_before'][key]
                require(database_facts(path, before['columns'], user_startup=key == 'main_db') == before,
                        'legacy ledger changed: ' + key)
            require(token_facts(config) == manifest['tokens_before'], 'token fingerprints changed')
            require(personal_rows(config) == 0, 'unexpected personal tasks created')
            reload_nginx(config)
            if enable_publishing:
                wait_idle(lambda: inflight(config), 'X / YouTube share before feature enable')
                set_env(ENV_FILE, 'X_VIDEO_PUBLISH_ENABLED', '1')
                run('systemctl', 'restart', 'x-post-automation.service')
                # Requires= stops Auto with the sidecar; restarting the dependency
                # does not restart its dependent unit automatically.
                run('systemctl', 'start', 'x-auto-post-service.service')
                health(config)
            if enable_pool:
                run('systemctl', 'enable', POOL)
                run('systemctl', 'start', POOL)
                require(unit_state(POOL).get('ActiveState') == 'active', 'personal pool scheduler failed')
            manifest.update(status='deployed', publishing_enabled=enable_publishing, pool_scheduler_enabled=enable_pool,
                            legacy_ledgers_unchanged=True, tokens_unchanged=True, no_personal_tasks_created=True)
            save(manifest)
        except BaseException:
            if stopped:
                try:
                    restore_code(manifest)
                    manifest['status'] = 'rolled_back'
                except BaseException:
                    manifest['status'] = 'recovery_required'
                    # Contain new creation while preserving every existing ledger.
                    set_env(ENV_FILE, 'X_VIDEO_PUBLISH_ENABLED', '0')
            save(manifest)
            raise
    print(json.dumps({k: manifest[k] for k in ('status', 'commit', 'backup', 'release')}), flush=True)


def rollback(manifest):
    config = manifest['baseline']
    preflight_disk(config)
    require(Path(config['paths']['current']).resolve() == Path(manifest['release']), 'rollback release differs')
    require(personal_rows(config) == 0, 'personal tasks exist: disable new entry and reconcile first')
    with paused(config, manifest):
        manifest['status'] = 'rolling_back'
        save(manifest)
        try:
            restore_code(manifest)
            manifest['status'] = 'rolled_back'
        except BaseException:
            manifest['status'] = 'recovery_required'
            save(manifest)
            raise
        save(manifest)
    print('Code/configuration restored; live databases and tokens preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--stage', action='store_true')
    actions.add_argument('--activate', type=Path)
    actions.add_argument('--rollback', type=Path)
    parser.add_argument('--enable-publishing', action='store_true')
    parser.add_argument('--enable-pool-scheduler', action='store_true')
    args = parser.parse_args()
    if args.stage:
        config = load_baseline()
        with deployment_lock(config):
            stage(config)
    elif args.activate:
        manifest = load_manifest(args.activate)
        with deployment_lock(manifest['baseline']):
            activate(manifest, args.enable_publishing, args.enable_pool_scheduler)
    else:
        manifest = load_manifest(args.rollback)
        with deployment_lock(manifest['baseline']):
            rollback(manifest)


if __name__ == '__main__':
    main()

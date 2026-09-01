#!/usr/bin/env python3
"""CPU-only migration operator. Mutating phases require explicit gate evidence.

Never stops source-host services, changes Nginx/cron, imports app.py, creates
business jobs, or starts the existing CPU workers on 8790/8795/8798.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
from mount_guard import DISK, check_mount

BASE = DISK / 'codex-workers/us-migrated'
PATHS = {
    'screenshot-public': ('/usr/share/nginx/html/drama-screenshot-materials', BASE / 'storage/drama-screenshot-materials'),
    'drama-public': ('/usr/share/nginx/html/drama-materials', BASE / 'storage/drama-materials'),
    'ad-public': ('/usr/share/nginx/html/ad-materials', BASE / 'storage/ad-materials'),
    'drama-work': ('/root/drama_material_jobs', BASE / 'storage/drama-material-jobs'),
    'ad-work': ('/root/ad_material_tasks', BASE / 'storage/ad-material-tasks'),
}
IMAGE_PATH_KEYS = ('screenshot-public', 'drama-public', 'drama-work')
SCOPES = {
    'all': PATHS,
    'images': {key: PATHS[key] for key in IMAGE_PATH_KEYS},
}
SCREENSHOT_JOBS = Path('/root/drama_screenshot_jobs')
SLOTS = {
    'screenshot-primary': (18795, 'codex-screenshot-migrated-primary.service'),
    'screenshot-burst': (18798, 'codex-screenshot-migrated-burst.service'),
    'cover': (18790, 'codex-cover-migrated.service'),
}
OLD_CPU_UNITS = ('codex-cover-generator.service', 'codex-screenshot-batch.service', 'codex-screenshot-batch-burst.service')
SOURCE_UNITS = ('codex-cover-generator.service', 'codex-screenshot-batch.service',
                'codex-screenshot-batch-burst.service', 'codex-screenshot-square.service',
                'codex-screenshot-landscape.service', 'codex-screenshot-portrait.service')
DB = '/root/drama_material_service/data/drama_material_jobs.sqlite3'


def command(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def system_property(unit, name):
    value = command(['systemctl', 'show', unit, '-p' + name]).strip()
    prefix = name + '='
    if not value.startswith(prefix):
        raise RuntimeError('unexpected systemd property response')
    return value[len(prefix):]


def validate_run_id(value):
    if not re.fullmatch(r'gpu-service-migration-20260828T[0-9]{4}', value):
        raise ValueError('unexpected migration run id')
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(str(temporary), 'w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(path))


def digest(path):
    value = hashlib.sha256()
    with open(str(path), 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def tree_manifest(root):
    root = Path(root)
    values = {}
    for directory, folders, files in os.walk(str(root), followlinks=False):
        for name in list(folders):
            path = Path(directory) / name
            if path.is_symlink():
                values[str(path.relative_to(root))] = {'link': os.readlink(str(path))}
                folders.remove(name)
        for name in files:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            values[relative] = ({'link': os.readlink(str(path))} if path.is_symlink() else
                                {'bytes': path.stat().st_size, 'sha256': digest(path)})
    return values


def compare_manifests(source, target):
    return sorted(name for name, value in source.items() if target.get(name) != value)


def compare_exact_manifests(source, target):
    return sorted(name for name in set(source) | set(target)
                  if source.get(name) != target.get(name))


def scope_paths(scope):
    try:
        return SCOPES[scope]
    except KeyError:
        raise RuntimeError('unsupported CPU migration scope')


def scoped_evidence(root, filename, scope):
    if scope == 'all':
        return root / filename
    head, separator, tail = str(filename).partition('.')
    return root / (head + '-' + scope + separator + tail)


def verify_screenshot_jobs_link():
    if not SCREENSHOT_JOBS.is_symlink():
        raise RuntimeError('images scope requires the existing screenshot jobs data-disk symlink')
    resolved = SCREENSHOT_JOBS.resolve(strict=True)
    disk = DISK.resolve(strict=True)
    if not resolved.is_dir() or (resolved != disk and disk not in resolved.parents):
        raise RuntimeError('screenshot jobs symlink does not resolve inside the approved data disk')
    check_mount([str(SCREENSHOT_JOBS), str(resolved)])
    return {'path': str(SCREENSHOT_JOBS), 'target': os.readlink(str(SCREENSHOT_JOBS)),
            'realpath': str(resolved), 'preserved': True}


def archive_target_conflicts(root, key, target, source_manifest, target_manifest):
    """Archive target-only/different bytes before an exact image-scope copy.

    The conflict archive is content addressed and opened exclusively. Existing
    evidence is verified and never overwritten.
    """
    rejected = {name: value for name, value in target_manifest.items()
                if source_manifest.get(name) != value}
    if not rejected:
        return {'entries': 0, 'archive': None}
    canonical = json.dumps(rejected, sort_keys=True, separators=(',', ':')).encode()
    archive = root / 'conflicts' / key / hashlib.sha256(canonical).hexdigest()
    payload = archive / 'payload'
    archive.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(str(archive), 0o700)
    payload.mkdir(mode=0o700, exist_ok=True)
    os.chmod(str(payload), 0o700)
    for relative, record in rejected.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts:
            raise RuntimeError('unsafe target conflict path')
        source = target / relative_path
        if 'link' in record:
            if not source.is_symlink() or os.readlink(str(source)) != record['link']:
                raise RuntimeError('target symlink changed during conflict archive')
            continue
        destination = payload / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if (destination.is_symlink() or digest(destination) != record['sha256'] or
                    destination.stat().st_size != record['bytes']):
                raise RuntimeError('existing target conflict archive differs')
            continue
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(destination), flags, 0o600)
        try:
            with os.fdopen(fd, 'wb') as output, open(str(source), 'rb') as input_file:
                fd = None
                shutil.copyfileobj(input_file, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        if digest(destination) != record['sha256'] or destination.stat().st_size != record['bytes']:
            raise RuntimeError('target conflict changed while being archived')
    manifest = archive / 'manifest.json'
    if manifest.exists():
        if json.loads(manifest.read_text()) != rejected:
            raise RuntimeError('existing target conflict manifest differs')
    else:
        write_json(manifest, rejected)
    return {'entries': len(rejected), 'archive': str(archive)}


def validate_target(target):
    target = Path(target)
    resolved = target.resolve()
    if BASE not in resolved.parents:
        raise RuntimeError('target escaped migration base')
    check_mount([str(target.parent)])


def verify_frozen_source(package):
    manifest = json.loads((package / 'source-manifest.json').read_text())
    for entry in manifest['files']:
        path = package / entry['file']
        if path.resolve().parent != (package / 'runtime').resolve():
            raise RuntimeError('invalid frozen source manifest path')
        if digest(path) != entry['sha256']:
            raise RuntimeError('frozen sidecar hash mismatch: %s' % entry['file'])
    return manifest


def proof(path, run_id, source_stopped=False, scope='all'):
    data = json.loads(Path(path).read_text())
    if data.get('run_id') != run_id or data.get('authorized_by_parent') is not True:
        raise RuntimeError('parent authorization is missing or belongs to another run')
    if not data.get('writes_fenced') or not data.get('cron_fenced') or not data.get('test_api_fenced'):
        raise RuntimeError('production, cron, and test API admission must all be fenced')
    age = time.time() - float(data.get('observed_at_epoch', 0))
    if age < -5 or age > 300:
        raise RuntimeError('maintenance evidence is not fresh')
    if scope == 'images' and data.get('scope') != 'images':
        raise RuntimeError('maintenance evidence is not restricted to images scope')
    if source_stopped:
        states = data.get('source_units', {})
        for unit in SOURCE_UNITS:
            row = states.get(unit, {})
            if scope == 'images':
                if (row.get('active') not in ('active', 'inactive') or
                        row.get('children') != 0 or row.get('requests') != 0):
                    raise RuntimeError('image source is not idle behind the stopped tunnels: %s' % unit)
            elif (row.get('active') != 'inactive' or
                  row.get('enabled') not in ('disabled', 'masked') or row.get('children') != 0):
                raise RuntimeError('source business service has not been proven stopped: %s' % unit)
        if data.get('source_tunnels_stopped') is not True:
            raise RuntimeError('old shared and burst source tunnels must be stopped')
    return data


def status_counts(db_path):
    result = {}
    uri = 'file:%s?mode=ro' % db_path
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.execute('PRAGMA query_only=ON')
        for table in ('drama_screenshot_job', 'drama_material_job', 'drama_material_job_worker_lease',
                      'ad_material_task', 'ad_material_asset'):
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists:
                result[table] = dict(connection.execute('SELECT status,COUNT(*) FROM ' + table + ' GROUP BY status'))
    return result


def assert_drained(include_drama=False, scope='all'):
    result = status_counts(DB)
    test_db = '/root/drama_material_service_test/data/drama_material_jobs.sqlite3'
    combined = [result] + ([status_counts(test_db)] if os.path.isfile(test_db) else [])
    for counts in combined:
        if any(n for s, n in counts.get('drama_screenshot_job', {}).items() if s not in ('done', 'failed')):
            raise RuntimeError('screenshot jobs have not drained')
        if scope != 'images':
            if any(counts.get('ad_material_task', {}).get(s, 0) for s in ('generating_demand', 'generating_material')):
                raise RuntimeError('ad material tasks have not drained')
            if counts.get('ad_material_asset', {}).get('regenerating', 0):
                raise RuntimeError('ad material regeneration has not drained')
        if include_drama and any(n for s, n in counts.get('drama_material_job', {}).items() if s not in ('done', 'failed', 'cancelled')):
            raise RuntimeError('drama jobs must finish before moving their public/work directories')
        if include_drama and counts.get('drama_material_job_worker_lease', {}).get('running', 0):
            raise RuntimeError('drama worker lease must be released before moving its files')
    connections = command(['ss', '-Hntp'])
    ports = '8790|8795|8798|18790|18795|18798' if scope == 'images' else \
            '8790|8795|8798|18790|18795|18796|18797|18798'
    if re.search(r':(?:' + ports + r')\b', connections):
        raise RuntimeError('affected worker TCP connections have not drained')
    for unit in OLD_CPU_UNITS + tuple(value[1] for value in SLOTS.values()
                                      if Path('/etc/systemd/system/' + value[1]).exists()):
        pid = system_property(unit, 'MainPID')
        if pid and pid != '0':
            process_status = Path('/proc/' + pid + '/status').read_text()
            match = re.search(r'^Threads:\s+(\d+)', process_status, re.M)
            if not match or int(match.group(1)) > 1:
                raise RuntimeError('existing CPU worker is handling a request: %s' % unit)
    return result


def snapshot(root, run_id, scope='all'):
    paths = scope_paths(scope)
    record = {'run_id': run_id, 'epoch': time.time(), 'hostname': socket.gethostname(),
              'mount': command(['findmnt', '-rn', '-o', 'SOURCE,TARGET,UUID,OPTIONS', '-T', str(DISK)]),
              'df': command(['df', '-B1', '/', str(DISK)]), 'status_counts': status_counts(DB),
              'scope': scope, 'paths': {}}
    for key, (source, target) in paths.items():
        record['paths'][key] = {'source': source, 'realpath': str(Path(source).resolve()),
                                'target': str(target), 'exists': Path(source).exists()}
    record['units'] = {unit: command(['systemctl', 'show', unit, '-pActiveState', '-pUnitFileState', '-pMainPID'])
                       for unit in OLD_CPU_UNITS}
    if scope == 'images':
        record['screenshot_jobs_link'] = verify_screenshot_jobs_link()
    write_json(scoped_evidence(root, 'audit.json', scope), record)
    return record


def backup(root, run_id, scope='all'):
    manifest = scoped_evidence(root, 'backup-manifest.json', scope)
    if manifest.exists():
        raise RuntimeError('original backup already exists; do not replace it')
    private = root / 'private'
    private.mkdir(mode=0o700, exist_ok=True)
    paths = {'/root/drama_material_service/.env', '/etc/drama-synthesis/cpu.env'}
    for unit in OLD_CPU_UNITS + ('drama-material-api.service', 'drama-material-job-worker.service',
                                  'drama-material-api-test.service', 'ad-material-frontend-test.service'):
        for key in ('FragmentPath', 'DropInPaths'):
            paths.update(system_property(unit, key).split())
    paths = sorted(path for path in paths if path and os.path.isfile(path))
    archive = scoped_evidence(private, 'cpu-config-before.tar.gz', scope)
    command(['tar', '-czf', archive, '-C', '/'] + [p.lstrip('/') for p in paths])
    os.chmod(str(archive), 0o600)
    db_backup = scoped_evidence(private, 'drama_material_jobs.sqlite3', scope)
    with sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=5) as source:
        with sqlite3.connect(str(db_backup)) as destination:
            source.backup(destination, pages=128, sleep=0.05)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('online SQLite backup failed quick_check')
    os.chmod(str(db_backup), 0o600)
    result = {'run_id': run_id, 'epoch': time.time(), 'configuration_paths': paths,
              'archive_sha256': digest(archive), 'sqlite_sha256': digest(db_backup), 'quick_check': 'ok'}
    write_json(manifest, result)
    snapshot(root, run_id, scope=scope)
    return result


def precopy(root, scope='all'):
    paths = scope_paths(scope)
    screenshot_jobs = verify_screenshot_jobs_link() if scope == 'images' else None
    BASE.mkdir(parents=True, exist_ok=True)
    os.chmod(str(BASE), 0o755)
    (BASE / 'storage').mkdir(mode=0o755, exist_ok=True)
    if shutil.disk_usage(str(DISK)).free < 10 * 1024 ** 3:
        raise RuntimeError('less than 10 GiB free on CPU data disk')
    reports = {}
    for key, (source, target) in paths.items():
        if Path(source).is_symlink():
            if Path(source).resolve() != target.resolve():
                raise RuntimeError('source is an unexpected existing symlink: %s' % source)
            reports[key] = {'already_on_target': True}
            continue
        if not Path(source).is_dir():
            raise RuntimeError('expected source directory missing: %s' % source)
        validate_target(target)
        target.mkdir(mode=0o755, exist_ok=True)
        before = tree_manifest(source)
        conflict = {'entries': 0, 'archive': None}
        if scope == 'images':
            conflict = archive_target_conflicts(
                root, key, target, before, tree_manifest(target))
            command(['rsync', '-aH', '--numeric-ids', '--delete-after',
                     source + '/', str(target) + '/'])
            differences = compare_exact_manifests(before, tree_manifest(target))
        else:
            command(['rsync', '-aH', '--numeric-ids', source + '/', str(target) + '/'])
            differences = compare_manifests(before, tree_manifest(target))
        write_json(scoped_evidence(root, 'manifest-' + key + '.json', scope), before)
        reports[key] = {'files': len(before), 'bytes': sum(v.get('bytes', 0) for v in before.values()),
                        'differing_files': differences, 'phase': 'precopy-not-final',
                        'conflict_archive': conflict}
    write_json(scoped_evidence(root, 'precopy.json', scope),
               {'scope': scope, 'paths': reports,
                'screenshot_jobs_link': screenshot_jobs})
    return reports


def exchange(first, second):
    library = ctypes.CDLL(None, use_errno=True)
    args = (-100, os.fsencode(str(first)), -100, os.fsencode(str(second)), 2)
    function = getattr(library, 'renameat2', None)
    if function:
        code = function(*args)
    else:
        number = {'x86_64': 316, 'aarch64': 276}.get(platform.machine())
        if number is None:
            raise RuntimeError('atomic directory exchange is unsupported on this architecture')
        code = library.syscall(number, *args)
    if code:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def cutover_storage(root, run_id, scope='all'):
    paths = scope_paths(scope)
    assert_drained(include_drama=True, scope=scope)
    results = precopy(root, scope=scope)
    if any(r.get('differing_files') for r in results.values()):
        raise RuntimeError('source changed during final copy; storage not switched')
    for key, (source_text, target) in paths.items():
        source = Path(source_text)
        backup = source.with_name(source.name + '.pre-' + run_id)
        temporary = source.with_name(source.name + '.link-' + run_id)
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise RuntimeError('unexpected source symlink')
            if temporary.is_dir() and not temporary.is_symlink() and not backup.exists():
                os.rename(str(temporary), str(backup))
            continue
        if backup.exists() or temporary.exists() or temporary.is_symlink():
            raise RuntimeError('storage rollback path already exists; inspect journal before retry')
        switch_evidence = scoped_evidence(root, 'switch-' + key + '.json', scope)
        write_json(switch_evidence, {
            'source': str(source), 'target': str(target), 'backup': str(backup),
            'temporary': str(temporary), 'state': 'prepared', 'run_id': run_id})
        os.symlink(str(target), str(temporary))
        try:
            exchange(source, temporary)
        except Exception:
            temporary.unlink()
            raise
        os.rename(str(temporary), str(backup))
        check_mount([str(source)])
        write_json(switch_evidence, {
            'source': str(source), 'target': str(target), 'backup': str(backup),
            'state': 'switched', 'run_id': run_id})
    return {'scope': scope, 'switched': list(paths),
            'root_rollback_copies_retained': True,
            'screenshot_jobs_link': verify_screenshot_jobs_link() if scope == 'images' else None}


def install(package, expected_commit, root):
    if not re.fullmatch(r'[0-9a-f]{40}', expected_commit):
        raise RuntimeError('exact GitHub commit is required')
    actual = command(['git', '-C', str(package), 'rev-parse', 'HEAD']).strip()
    if actual != expected_commit:
        raise RuntimeError('checkout does not match expected GitHub commit')
    if command(['git', '-C', str(package), 'status', '--porcelain', '--', '.']).strip():
        raise RuntimeError('CPU migration package checkout is dirty')
    verify_frozen_source(package)
    release = BASE / 'releases' / expected_commit
    release.mkdir(parents=True, exist_ok=True)
    for _, unit in SLOTS.values():
        if (Path('/etc/systemd/system') / unit).exists():
            raise RuntimeError('new unit name is already installed; do not overwrite silently')
    logrotate = Path('/etc/logrotate.d/codex-cpu-migrated')
    if logrotate.exists():
        raise RuntimeError('migration logrotate configuration already exists')
    for name in ('mount_guard.py', 'cover_entrypoint.py', 'source-manifest.json', 'run.sh'):
        target = release / name
        if target.exists() and digest(target) != digest(package / name):
            raise RuntimeError('immutable release file differs')
        shutil.copy2(str(package / name), str(target))
    (release / 'runtime').mkdir(exist_ok=True)
    for entry in json.loads((package / 'source-manifest.json').read_text())['files']:
        target = release / entry['file']
        if target.exists() and digest(target) != entry['sha256']:
            raise RuntimeError('immutable runtime differs')
        shutil.copy2(str(package / entry['file']), str(target))
    for slot, (_, unit) in SLOTS.items():
        for name in ('jobs', 'cache', 'workspace', 'tmp', 'xdg-cache', 'codex-home'):
            path = BASE / slot / name
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(str(BASE / slot), 0o700)
            os.chmod(str(path), 0o700)
        destination = Path('/etc/systemd/system') / unit
        value = (package / 'units' / unit).read_text().replace('@RELEASE@', str(release))
        destination.write_text(value)
        os.chmod(str(destination), 0o644)
    shutil.copy2(str(package / 'logrotate.conf'), str(logrotate))
    os.chmod(str(logrotate), 0o644)
    command(['systemctl', 'daemon-reload'])
    write_json(root / 'installed.json', {'commit': expected_commit, 'release': str(release),
                                      'units': [v[1] for v in SLOTS.values()], 'started': False})
    return {'release': str(release), 'started': False}


def start_units(scope='all'):
    paths = scope_paths(scope)
    assert_drained(scope=scope)
    check_mount([source for source, _ in paths.values()])
    if scope == 'images':
        verify_screenshot_jobs_link()
    for port, unit in SLOTS.values():
        with socket.socket() as test_socket:
            test_socket.bind(('127.0.0.1', port))
        if system_property(unit, 'ActiveState') == 'active':
            raise RuntimeError('new unit was already active before handoff')
    for _, unit in SLOTS.values():
        command(['systemctl', 'enable', '--now', unit])
    return {'started': [v[1] for v in SLOTS.values()]}


def get_body(url, limit=16 * 1024 * 1024):
    with urllib.request.urlopen(url, timeout=15) as response:
        body = response.read(limit + 1)
        if response.status != 200 or len(body) > limit:
            raise RuntimeError('HTTP artifact verification failed')
        return body


def verify(root, scope='all'):
    paths = scope_paths(scope)
    check_mount([source for source, _ in paths.values()])
    if scope == 'images':
        verify_screenshot_jobs_link()
    listeners = command(['ss', '-Hlntp'])
    result = {'units': {}, 'public_samples': [], 'existing_cpu_units': {}, 'status_counts': status_counts(DB)}
    for slot, (port, unit) in SLOTS.items():
        pid = system_property(unit, 'MainPID')
        matching = [line for line in listeners.splitlines() if re.search(r':%d\b' % port, line)]
        if pid == '0' or len(matching) != 1 or ('pid=%s,' % pid) not in matching[0] or 'sshd' in matching[0]:
            raise RuntimeError('production port is not owned by the expected local unit: %s' % unit)
        health = json.loads(get_body('http://127.0.0.1:%d/healthz' % port, 65536))
        if health.get('status') != 'ok':
            raise RuntimeError('worker health is not ok')
        result['units'][unit] = {'pid': pid, 'port': port, 'health': health}
    audit_path = scoped_evidence(root, 'audit.json', scope)
    audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
    for unit in OLD_CPU_UNITS:
        pid = system_property(unit, 'MainPID')
        old = re.search(r'^MainPID=(\d+)$', audit.get('units', {}).get(unit, ''), re.M)
        result['existing_cpu_units'][unit] = {'pid': pid, 'original_pid': old.group(1) if old else None}
        if not old or pid != old.group(1) or pid == '0':
            raise RuntimeError('an existing CPU worker PID changed; review before accepting migration')
    for key, base_url, ports in (
            ('screenshot-public', 'https://ai.yingliangads.com/drama-screenshot-materials', (18795, 18798)),
            ('drama-public', 'https://ai.yingliangads.com/drama-materials', (18790,))):
        source, target = paths[key]
        samples = [path for path in sorted(target.rglob('*')) if path.is_file() and path.suffix.lower() in ('.jpg', '.png', '.webp')][:3]
        if not samples:
            raise RuntimeError('no existing image artifact available for verification')
        for path in samples:
            relative = str(path.relative_to(target)).replace(os.sep, '/')
            encoded = urllib.parse.quote(relative)
            expected = digest(path)
            urls = [base_url + '/' + encoded] + ['http://127.0.0.1:%d/files/%s' % (port, encoded) for port in ports]
            for url in urls:
                if hashlib.sha256(get_body(url)).hexdigest() != expected:
                    raise RuntimeError('artifact bytes changed across public/files endpoint: %s' % relative)
            result['public_samples'].append({'relative_path': relative, 'sha256': expected, 'endpoints_checked': len(urls)})
    return result


def rollback_storage(root, run_id, scope='all'):
    paths = scope_paths(scope)
    assert_drained(include_drama=True, scope=scope)
    for key, (source_text, target) in paths.items():
        source = Path(source_text)
        backup = source.with_name(source.name + '.pre-' + run_id)
        if not source.is_symlink() or source.resolve() != target.resolve() or not backup.is_dir() or backup.is_symlink():
            raise RuntimeError('rollback paths do not match recorded storage state')
        baseline = json.loads(scoped_evidence(
            root, 'manifest-' + key + '.json', scope).read_text())
        if tree_manifest(target) != baseline or tree_manifest(backup) != baseline:
            raise RuntimeError('new data exists; leave data on disk and use code-only rollback')
    for source_text, _ in paths.values():
        source = Path(source_text)
        exchange(source, source.with_name(source.name + '.pre-' + run_id))
    return {'scope': scope, 'restored_root_paths': list(paths),
            'data_disk_copies_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('audit', 'backup', 'precopy', 'install', 'cutover-storage', 'start', 'verify', 'stop', 'rollback-storage'))
    parser.add_argument('--run-id', required=True, type=validate_run_id)
    parser.add_argument('--expected-commit')
    parser.add_argument('--authorization')
    parser.add_argument('--scope', choices=tuple(SCOPES), default='all')
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != 'VM-0-108-centos':
        raise RuntimeError('this operator must run as root on the designated CPU host')
    check_mount()
    root = DISK / 'migrations' / args.run_id / 'cpu'
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(str(root), 0o700)
    package = Path(__file__).resolve().parent
    if args.phase == 'audit':
        result = snapshot(root, args.run_id, scope=args.scope)
    elif args.phase == 'backup':
        result = backup(root, args.run_id, scope=args.scope)
    elif args.phase == 'precopy':
        result = precopy(root, scope=args.scope)
    elif args.phase == 'verify':
        result = verify(root, scope=args.scope)
    elif args.phase == 'install':
        if not args.authorization:
            raise RuntimeError('parent install authorization file is required')
        data = json.loads(Path(args.authorization).read_text())
        if data.get('run_id') != args.run_id or data.get('install_authorized_by_parent') is not True:
            raise RuntimeError('parent install authorization missing')
        result = install(package, args.expected_commit or '', root)
    else:
        if not args.authorization:
            raise RuntimeError('fresh parent maintenance authorization is required')
        proof(args.authorization, args.run_id,
              source_stopped=(args.phase == 'start' or
                              (args.scope == 'images' and args.phase == 'cutover-storage')),
              scope=args.scope)
        if args.phase == 'cutover-storage':
            result = cutover_storage(root, args.run_id, scope=args.scope)
        elif args.phase == 'start':
            result = start_units(scope=args.scope)
        elif args.phase == 'stop':
            assert_drained(scope=args.scope)
            for _, unit in SLOTS.values():
                command(['systemctl', 'disable', '--now', unit])
            result = {'stopped': [v[1] for v in SLOTS.values()]}
        else:
            result = rollback_storage(root, args.run_id, scope=args.scope)
    write_json(scoped_evidence(root, 'last-' + args.phase + '.json', args.scope), result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

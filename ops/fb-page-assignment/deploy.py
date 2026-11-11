"""Install an exact GitHub commit; preserve task logs and pending delivery on rollback.

Run on the CPU host after fetching the reviewed commit. This installer never
copies the shared backend, restarts publishers, or drops a database table.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request


DATA = Path('/mnt/data-disk/fb-page-assignment')
MOUNT = Path('/mnt/data-disk')
MOUNT_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
CURRENT = Path('/opt/fb-page-assignment/current')
ENV = Path('/etc/fb-page-assignment.env')
UNIT = Path('/etc/systemd/system/fb-page-assignment.service')
NGINX = Path('/etc/nginx/default.d/fb-page-assignment.conf')
TABLE = 'fb_drama_page_assignment_log'
SERVICE = UNIT.name
ROUTE = '/api/integrations/v1/drama-page-assignments'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, data):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def inside(path, root):
    return path == root or root in path.parents


def mounted():
    need(run('findmnt', '-no', 'UUID', str(MOUNT)) == MOUNT_UUID, 'data_mount_identity_mismatch')
    need(os.access(MOUNT, os.W_OK), 'data_mount_not_writable')
    need(shutil.disk_usage(MOUNT).free > 256 * 1024 * 1024, 'data_mount_space_insufficient')


def environment(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def service_state():
    def check(verb):
        return subprocess.run(['systemctl', verb, SERVICE], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True).stdout.strip()
    return {'active': check('is-active') == 'active', 'enabled': check('is-enabled') == 'enabled'}


def backup(deployment, target, preserve_if_new=False):
    need(not target.is_symlink(), 'unexpected_config_symlink:' + str(target))
    item = {'target': str(target), 'before': None, 'before_hash': None,
            'preserve_if_new': preserve_if_new, 'installed_hash': None}
    if target.exists():
        relative = Path('backup') / str(target).lstrip('/')
        (deployment / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, deployment / relative)
        item.update(before=str(relative), before_hash=digest(target))
    return item


def set_pointer(target):
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    temporary = CURRENT.with_name('current.next')
    need(not temporary.exists() and not temporary.is_symlink(), 'temporary_pointer_exists')
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT)


def unchanged(item):
    target = Path(item['target'])
    actual = digest(target) if target.exists() else None
    need(actual == item['before_hash'], 'concurrent_config_change:' + str(target))


def health(timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8943/healthz', timeout=2) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get('ok') is True:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('assignment_service_not_ready')


def unauthorized_probe():
    request = urllib.request.Request('http://127.0.0.1:8943' + ROUTE, data=b'{}',
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        payload = json.load(exc)
        need(exc.code == 401 and set(payload) == {'task_id', 'results'} and
             payload['results'] == [], 'unauthorized_contract_mismatch')
        return
    raise RuntimeError('unauthorized_request_accepted')


def write_environment(source_env):
    shared = environment(source_env)
    values = environment(ENV)
    for key, value in shared.items():
        if key.startswith('ADMIN_MAPPING_MYSQL_') or key in {'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'DRAMA_JOB_DB_PATH'}:
            values.setdefault(key, value)
    need(str(values.get('ADMIN_MAPPING_MYSQL_HOST')) == '101.32.56.53' and
         str(values.get('ADMIN_MAPPING_MYSQL_PORT')) == '63350', 'reader_endpoint_invalid')
    need(values.get('ADMIN_MAPPING_MYSQL_USER') and values.get('ADMIN_MAPPING_MYSQL_PASSWORD'),
         'reader_credentials_missing')
    values.setdefault('DRAMA_JOB_DB_PATH', '/root/drama_material_service/data/drama_material_jobs.sqlite3')
    values.setdefault('FB_PAGE_ASSIGNMENT_API_TOKEN', secrets.token_urlsafe(32))
    values.setdefault('FB_PAGE_ASSIGNMENT_SHARED_ROOT', '/root/drama_material_service')
    values.setdefault('FB_PAGE_ASSIGNMENT_MYSQL_WRITE_HOST', '101.32.56.53')
    values.setdefault('FB_PAGE_ASSIGNMENT_MYSQL_WRITE_PORT', '63353')
    values.setdefault('FB_PAGE_ASSIGNMENT_MYSQL_WRITE_DATABASE', 'ads_ai')
    values.setdefault('FB_PAGE_ASSIGNMENT_MYSQL_WRITE_USER', values['ADMIN_MAPPING_MYSQL_USER'])
    values.setdefault('FB_PAGE_ASSIGNMENT_MYSQL_WRITE_PASSWORD', values['ADMIN_MAPPING_MYSQL_PASSWORD'])
    need((values['FB_PAGE_ASSIGNMENT_MYSQL_WRITE_HOST'], values['FB_PAGE_ASSIGNMENT_MYSQL_WRITE_PORT'],
          values['FB_PAGE_ASSIGNMENT_MYSQL_WRITE_DATABASE']) == ('101.32.56.53', '63353', 'ads_ai'),
         'writer_endpoint_invalid')
    need(all('\n' not in str(v) and '\r' not in str(v) for v in values.values()), 'multiline_environment_value')
    temporary = ENV.with_name(ENV.name + '.next')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(''.join(key + '=' + str(value) + '\n' for key, value in sorted(values.items())))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, ENV)
    return values


def validate_ddl(sql):
    plain = re.sub(r'/\*.*?\*/', '', sql, flags=re.S)
    plain = re.sub(r'^\s*--.*$', '', plain, flags=re.M).strip()
    need(re.match(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?ads_ai`?\.`?' + TABLE +
                  r'`?\s*\(', plain, flags=re.I) is not None, 'unexpected_ddl_target')
    need(';' not in plain.rstrip(';') and not re.search(r'\b(DROP|ALTER|TRUNCATE|RENAME)\b', plain, re.I),
         'unexpected_ddl_statement')
    return plain


def create_table(schema, values):
    import pymysql
    sql = validate_ddl(schema.read_text(encoding='utf-8'))
    prefix = 'FB_PAGE_ASSIGNMENT_MYSQL_WRITE_'
    with pymysql.connect(host=values[prefix + 'HOST'], port=int(values[prefix + 'PORT']),
                         user=values[prefix + 'USER'], password=values[prefix + 'PASSWORD'],
                         database='ads_ai', charset='utf8mb4', connect_timeout=3,
                         read_timeout=10, write_timeout=10, autocommit=True) as db:
        with db.cursor() as cursor:
            cursor.execute('SELECT DATABASE(),@@read_only')
            need(cursor.fetchone() == ('ads_ai', 0), 'ddl_endpoint_identity_mismatch')
            cursor.execute(sql)


def rollback(deployment, *, failed_install=False):
    manifest = json.loads((deployment / 'manifest.json').read_text(encoding='utf-8'))
    release = Path(manifest['release'])
    need(inside(release, DATA / 'releases'), 'invalid_rollback_release')
    if CURRENT.is_symlink():
        current = str(CURRENT.resolve())
        need(current in {str(release), manifest.get('previous_pointer')}, 'newer_release_preserved')
    for item in manifest['files']:
        target = Path(item['target'])
        need(target in {ENV, UNIT, NGINX}, 'invalid_rollback_file')
        if target.exists():
            allowed = {value for value in (item.get('installed_hash'), item['before_hash']) if value}
            need(digest(target) in allowed,
                 'newer_file_preserved:' + str(target))
    subprocess.run(['systemctl', 'stop', SERVICE], check=False)
    subprocess.run(['systemctl', 'disable', SERVICE], check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    for item in reversed(manifest['files']):
        target = Path(item['target'])
        if item['before']:
            shutil.copy2(deployment / item['before'], target)
        elif not item['preserve_if_new']:
            target.unlink(missing_ok=True)
    if manifest['previous_pointer']:
        set_pointer(manifest['previous_pointer'])
    elif CURRENT.is_symlink():
        CURRENT.unlink()
    run('systemctl', 'daemon-reload')
    if manifest['previous_service']['enabled']:
        run('systemctl', 'enable', SERVICE)
    if manifest['previous_service']['active']:
        run('systemctl', 'start', SERVICE)
        health()
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')
    manifest['rolled_back_at'] = datetime.now(timezone.utc).isoformat()
    manifest['rollback_after_failure'] = failed_install
    atomic_json(deployment / 'manifest.json', manifest)
    print(json.dumps({'status': 'rolled_back', 'deployment': str(deployment),
                      'database_logs_and_outbox': 'preserved'}))


def deploy(args):
    source = Path(args.source).resolve()
    commit = args.commit
    need(re.fullmatch(r'[0-9a-f]{40}', commit) is not None, 'full_commit_required')
    need(run('git', '-C', str(source), 'rev-parse', 'HEAD') == commit, 'source_commit_mismatch')
    need(not run('git', '-C', str(source), 'status', '--porcelain'), 'source_worktree_not_clean')
    origin = run('git', '-C', str(source), 'remote', 'get-url', 'origin')
    need(origin in {'git@github.com:gaoyuan12356416/ai-.git', 'https://github.com/gaoyuan12356416/ai-.git',
                    'git@github.com:gaoyuan12356416/ai-', 'https://github.com/gaoyuan12356416/ai-'},
         'unexpected_source_repository')
    need(run('git', '-C', str(source), 'for-each-ref', '--format=%(refname)', '--contains', commit,
             'refs/remotes/origin'), 'commit_not_in_fetched_origin')
    deployment = Path(args.deployment).resolve()
    need(inside(deployment, MOUNT) and deployment != MOUNT, 'deployment_outside_data_mount')
    need(not deployment.exists(), 'deployment_already_exists')
    release = DATA / 'releases' / commit
    need(not release.exists(), 'release_already_exists')
    need(not CURRENT.exists() or CURRENT.is_symlink(), 'current_must_be_symlink')
    deployment.mkdir(parents=True, mode=0o700)
    os.chmod(deployment, 0o700)
    release.mkdir(parents=True, mode=0o755)
    tracked = run('git', '-C', str(source), 'ls-files', 'features/fb_page_assignment').splitlines()
    need('features/fb_page_assignment/server.py' in tracked, 'server_not_in_commit')
    tracked.append('features/__init__.py')
    hashes = {}
    for relative in tracked:
        destination = release / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
        hashes[relative] = digest(source / relative)
        need(digest(destination) == hashes[relative], 'release_copy_hash_mismatch')
    run('/usr/bin/python3', '-m', 'compileall', '-q', str(release / 'features/fb_page_assignment'))
    manifest = {'commit': commit, 'source': str(source), 'release': str(release),
                'created_at': datetime.now(timezone.utc).isoformat(), 'hashes': hashes,
                'previous_pointer': str(CURRENT.resolve()) if CURRENT.is_symlink() else None,
                'previous_service': service_state(),
                'files': [backup(deployment, ENV, True), backup(deployment, UNIT), backup(deployment, NGINX)]}
    atomic_json(deployment / 'manifest.json', manifest)
    try:
        unchanged(manifest['files'][0])
        values = write_environment(Path(args.source_env))
        manifest['files'][0]['installed_hash'] = digest(ENV)
        atomic_json(deployment / 'manifest.json', manifest)
        if args.create_table:
            create_table(release / 'features/fb_page_assignment/schema.sql', values)
            manifest['table_created_or_verified'] = 'ads_ai.' + TABLE
        set_pointer(release)
        unchanged(manifest['files'][1])
        shutil.copy2(source / 'deploy/fb-page-assignment.service', UNIT)
        manifest['files'][1]['installed_hash'] = digest(UNIT)
        atomic_json(deployment / 'manifest.json', manifest)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', SERVICE)
        run('systemctl', 'restart', SERVICE)
        health()
        unauthorized_probe()
        unchanged(manifest['files'][2])
        shutil.copy2(source / 'deploy/nginx/fb-page-assignment.conf', NGINX)
        manifest['files'][2]['installed_hash'] = digest(NGINX)
        atomic_json(deployment / 'manifest.json', manifest)
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        manifest['activated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(deployment / 'manifest.json', manifest)
    except Exception:
        rollback(deployment, failed_install=True)
        raise
    print(json.dumps({'status': 'active', 'commit': commit, 'deployment': str(deployment),
                      'release': str(release), 'service': SERVICE, 'secret_file': str(ENV),
                      'route': ROUTE}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--commit')
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--source-env', default='/root/drama_material_service/.env')
    parser.add_argument('--create-table', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    need(os.geteuid() == 0, 'root_required')
    mounted()
    deployment = Path(args.deployment).resolve()
    need(inside(deployment, MOUNT) and deployment != MOUNT, 'deployment_outside_data_mount')
    if args.rollback:
        rollback(deployment)
    else:
        need(args.source and args.commit, 'source_and_commit_required')
        deploy(args)


if __name__ == '__main__':
    main()

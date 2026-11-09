#!/usr/bin/env python3
"""Upgrade only the tag-pool allocator; preserve all services, schedules and ledgers."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from features.x_tag_publish.staging import build, digest, REGRESSION_FILES
from scripts.deploy_x_tag_publish import CURRENT, ROOT, LEGACY_SOURCE, ENV, cmd, health

SERVICES = ('drama-material-api.service', 'x-post-automation.service', 'x-auto-post-service.service',
            'x-tag-post.service', 'x-tag-auto.service', 'x-tag-gateway.service')
TABLES = {'posts.sqlite3': ('x_post_queue', 'x_post_publish_log', 'x_post_material_pool', 'x_post_manual_run'),
          'auto/x-auto-post.sqlite3': ('x_auto_template', 'x_auto_run', 'x_auto_task', 'x_auto_material_ledger')}
ALLOWED = {'features/x_tag_publish/pool.py', 'features/x_tag_publish/staging.py',
           'deploy/x-post-schedule.env.example'} | {'scripts/' + name for name in REGRESSION_FILES}


def utc():
    return datetime.now(timezone.utc).isoformat()


def runtime_files(path):
    files = json.loads((path / 'tag-runtime-manifest.json').read_text())['files']
    for name, expected in files.items():
        if digest(path / name) != expected:
            raise RuntimeError('runtime_file_changed:' + name)
    return files


def state():
    result = {}
    for name, tables in TABLES.items():
        with closing(sqlite3.connect((ROOT / name).as_uri() + '?mode=ro', uri=True)) as db:
            result[name] = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in tables}
            if name == 'posts.sqlite3':
                result['schedules'] = db.execute('SELECT source_type,enabled,account_ids_json,publish_times_json FROM x_post_schedule_config ORDER BY source_type').fetchall()
    return result


def require_empty(value):
    # This release must preserve the user's explicitly requested empty setup.
    # A concurrent user creation aborts this deployment; it is never deleted.
    if any(count for name in TABLES for count in value[name].values()):
        raise RuntimeError('new_lane_has_business_data:preserve_and_reassess_switch')
    if any(enabled or json.loads(accounts) or json.loads(times) for _, enabled, accounts, times in value['schedules']):
        raise RuntimeError('new_lane_config_created:preserve_and_reassess_switch')


def services():
    return cmd('systemctl', 'show', *SERVICES, '-p', 'Id,MainPID,ActiveState,Result')


def pool_state():
    return cmd('systemctl', 'show', 'x-tag-pool.service', 'x-tag-pool.timer',
               '-p', 'Id,ActiveState,Result,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic,NextElapseUSecRealtime')


@contextmanager
def pool_lock():
    import fcntl
    # Existing lane lock, not an old-lane lock or a new persistent gate.
    with Path('/run/x-tag-post/schedule.lock').open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def point_to(release):
    temporary = CURRENT.with_name('current.pool-fix-next')
    temporary.symlink_to(release)
    temporary.replace(CURRENT)


def install():
    if cmd('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    commit = cmd('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if cmd('git', '-C', str(SOURCE), 'status', '--porcelain') or not cmd('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    previous = CURRENT.resolve(strict=True)
    before_files = runtime_files(previous)
    release = ROOT / 'releases' / commit
    if not release.exists():
        build(LEGACY_SOURCE, SOURCE, release)
    after_files = runtime_files(release)
    for name in ('features/x_tag_publish/pool.py', 'features/x_tag_publish/staging.py'):
        if after_files.get(name) != digest(SOURCE / name):
            raise RuntimeError('staged_source_mismatch:' + name)
    changed = sorted(name for name in set(before_files) | set(after_files) if before_files.get(name) != after_files.get(name))
    if not set(changed) <= ALLOWED or 'features/x_tag_publish/pool.py' not in changed:
        raise RuntimeError('unexpected_runtime_scope:' + json.dumps(changed))
    backup = ROOT / 'pool-fix-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    cmd(sys.executable, '-B', str(SOURCE / 'scripts/test_x_tag_regression.py'), '--runtime', str(release),
        '--output', str(backup / 'tests'), timeout=180)
    summary = json.loads((backup / 'tests/regression-results.json').read_text())
    print(json.dumps({'phase': 'server_tests_passed', 'count': summary['count'], 'backup': str(backup)}), flush=True)
    info = dict(commit=commit, previous=str(previous), release=str(release), backup=str(backup),
                changed=changed, tests=summary, status='prepared')
    record = backup / 'deployment.json'
    with pool_lock():
        if CURRENT.resolve() != previous:
            raise RuntimeError('concurrent_release_switch')
        info['state_before'] = state()
        require_empty(info['state_before'])
        info['services_before'] = services()
        info['pool_before'] = pool_state()
        for port, path in ((8821, '/health'), (18843, '/health'), (8898, '/healthz')):
            health(port, path)
        for name in TABLES:
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect((ROOT / name).as_uri() + '?mode=ro', uri=True)) as source_db:
                with closing(sqlite3.connect(saved)) as backup_db:
                    source_db.backup(backup_db)
        shutil.copy2(ENV, backup / 'publisher.env')
        (backup / 'publisher.env').chmod(0o600)
        for name in changed:
            if (previous / name).is_file():
                saved = backup / 'code' / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(previous / name, saved)
        record.write_text(json.dumps(info, indent=2))
        try:
            # Only pool.py changes executable behavior. It is imported by each
            # new pool oneshot. Parent daemons and all other runtime code remain
            # byte-identical, so no service restart/reload is needed.
            point_to(release)
            info['activated_at'] = utc()
            info['state_after'] = state()
            require_empty(info['state_after'])
            info['services_after'] = services()
            if info['services_before'] != info['services_after']:
                raise RuntimeError('parent_service_state_changed')
            info['pool_after'] = pool_state()
            info['status'] = 'active_for_next_pool_tick'
        except Exception:
            if CURRENT.resolve() == release:
                point_to(previous)
            info['status'] = 'reverted_code_only'
            record.write_text(json.dumps(info, indent=2))
            raise
        record.write_text(json.dumps(info, indent=2))
    print(json.dumps({key: info[key] for key in ('status', 'commit', 'release', 'backup', 'activated_at')}), flush=True)


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != (ROOT / 'pool-fix-backups').resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    with pool_lock():
        current = CURRENT.resolve()
        if current not in (Path(info['release']), Path(info['previous'])):
            raise RuntimeError('later_release_present')
        runtime_files(Path(info['previous']))
        if current != Path(info['previous']):
            point_to(Path(info['previous']))
    print(json.dumps(dict(status='rolled_back_code_only', current=str(CURRENT.resolve()),
                         ledgers_and_config_preserved=True, services_restarted=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)

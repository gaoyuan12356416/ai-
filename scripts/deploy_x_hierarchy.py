#!/usr/bin/env python3
"""Scoped hierarchy upgrade. Never reset configurations, reservations or receipts."""
import argparse
from contextlib import closing, contextmanager, ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_tag_pool_fix import runtime_files, state, TABLES
from features.x_tag_revenue.refresh import refresh, WAREHOUSE_ROOT
from features.x_tag_revenue.taxonomy import VERSION

PORTRAITS = Path('/mnt/data-disk/x-channel-tag-revenue')
BACKUPS = lane.ROOT / 'hierarchy-backups'
API = 'x-channel-tag-revenue.service'
TRIGGERS = ('x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer',
            'x-tag-auto-runner.path', 'x-tag-pool.timer')
WORKERS = ('x-tag-auto-scheduler.service', 'x-tag-auto-runner.service', 'x-tag-pool.service')
LOCKS = ('/run/x-tag-auto/scheduler.lock', '/run/x-tag-auto/runner.lock',
         '/run/x-tag-post/schedule.lock')
HEALTH = Path('/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py')
NAV = re.compile(r'(/quick-nav\.js)(?:\?[^"\']*)?')


def record(path, value):
    lane.atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def active(name):
    return lane.cmd('systemctl', 'is-active', name, check=False) in ('active', 'activating')


def mount_check():
    if lane.cmd('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')


def source_commit():
    commit = lane.cmd('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if lane.cmd('git', '-C', str(SOURCE), 'status', '--porcelain') or not lane.cmd('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    return commit


def validate_tree(data):
    def visit(nodes, total):
        if sum(row['revenue_cents'] for row in nodes) != total:
            raise RuntimeError('hierarchy_income_not_conserved')
        shares = [row.get('share_bp') for row in nodes]
        if shares and all(value is not None for value in shares) and sum(shares) != 10000:
            raise RuntimeError('hierarchy_share_not_conserved')
        for row in nodes:
            if 'children' in row:
                visit(row['children'], row['revenue_cents'])
            elif sum(drama['allocated_cents'] for drama in row['dramas']) != row['revenue_cents']:
                raise RuntimeError('hierarchy_drama_income_not_conserved')
    if data['meta']['hierarchy_version'] != VERSION:
        raise RuntimeError('hierarchy_version_mismatch')
    for profile in data['accounts'].values():
        visit(profile['types'], profile['revenue_cents'])
        if sum(tag['revenue_cents'] for tag in profile['tags']) != profile['revenue_cents']:
            raise RuntimeError('legacy_income_changed')


def targets(release, previous):
    result = {}
    for path in (SOURCE / 'features/x_tag_revenue').glob('*'):
        if path.suffix not in ('.py', '.json'):
            continue
        target = lane.MAIN / 'features/x_tag_revenue' / path.name
        old = previous / 'features/x_tag_revenue' / path.name
        if lane.sha(target) not in (lane.sha(old), lane.sha(path)):
            raise RuntimeError('concurrent_portrait_code_change:' + str(target))
        result[str(target)] = path.read_bytes()
    previous_source = lane.ROOT / 'sources' / previous.name
    for root in (lane.MAIN / 'static', lane.WEB):
        for path in (release / 'static').glob('x-tag-*'):
            if not path.is_file():
                continue
            target = root / path.name
            old = previous / 'static' / path.name
            if not old.exists() and path.name.startswith('x-tag-revenue.'):
                old = previous_source / 'static' / path.name
                if not old.is_file():
                    raise RuntimeError('previous_portrait_asset_required:' + path.name)
            content = path.read_bytes()
            if path.suffix == '.html' and target.exists():
                live = target.read_text()
                # The separately maintained shared navigation stays at its live version.
                refs = NAV.findall(live)
                old_text = old.read_text() if old.exists() else ''
                if NAV.sub(r'\1', live) not in (NAV.sub(r'\1', old_text), NAV.sub(r'\1', content.decode())):
                    raise RuntimeError('concurrent_tag_page_change:' + str(target))
                nav = NAV.search(live)
                if refs and nav:
                    content = NAV.sub(lambda _: nav.group(), content.decode()).encode()
            elif lane.sha(target) not in (lane.sha(old), lane.sha(path)):
                raise RuntimeError('concurrent_tag_asset_change:' + str(target))
            result[str(target)] = content
        channel = root / 'x-account-list.html'
        content = channel.read_text()
        # Patch just this existing column and asset references, preserving other edits.
        content = content.replace('历史标签收入', '历史三级画像')
        for asset in ('x-tag-revenue.js', 'x-tag-revenue.css'):
            pattern = r'(/' + re.escape(asset) + r')(?:\?[^"\']*)?'
            content, count = re.subn(pattern, r'\1?v=' + lane.sha(SOURCE / 'static' / asset)[:12], content)
            if count != 1:
                raise RuntimeError('portrait_asset_anchor_changed:' + asset)
        result[str(channel)] = content.encode()
    return {path: content for path, content in result.items()
            if lane.sha(path) != hashlib.sha256(content).hexdigest()}


def stage():
    mount_check()
    commit = source_commit()
    previous = lane.CURRENT.resolve(strict=True)
    before = runtime_files(previous)
    release = lane.ROOT / 'releases' / commit
    if not release.exists():
        lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    after = runtime_files(release)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    allowed = ('features/x_tag_publish/', 'features/x_tag_revenue/', 'static/x-tag-', 'scripts/test_')
    if any(not name.startswith(allowed) and name != 'static/quick-nav.js' for name in changed):
        raise RuntimeError('unexpected_runtime_scope:' + json.dumps(changed))
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    lane.cmd(sys.executable, '-B', str(SOURCE / 'scripts/test_x_tag_regression.py'),
             '--runtime', str(release), '--output', str(backup / 'tests'), timeout=300)
    tests = json.loads((backup / 'tests/regression-results.json').read_text())
    if not tests['passed']:
        raise RuntimeError('regression_failed')
    print(json.dumps(dict(phase='server_tests_passed', count=tests['count'], backup=str(backup))), flush=True)
    staging = backup / 'portrait-next'
    refreshed = refresh(staging, WAREHOUSE_ROOT, str(lane.MAIN / '.env'))
    if refreshed['status'] != 'ok':
        raise RuntimeError('recomputed_portrait_required')
    pointer = json.loads((staging / 'current.json').read_text())
    validate_tree(json.loads((staging / pointer['file']).read_text()))
    plan = targets(release, previous)
    info = dict(status='staged', commit=commit, hierarchy_version=VERSION, backup=str(backup),
                previous_current=str(previous), release=str(release), runtime_changed=changed,
                tests=tests, refresh=refreshed, files={})
    for index, (target, content) in enumerate(plan.items()):
        next_file = backup / 'next' / str(index)
        lane.atomic(next_file, content)
        info['files'][target] = dict(before=lane.sha(target), after=hashlib.sha256(content).hexdigest(),
                                    next=str(next_file), backup=str(backup / 'before' / str(index)))
    record(backup / 'deployment.json', info)
    print(json.dumps(dict(status='staged', backup=str(backup), commit=commit,
                          files=len(plan), refresh=refreshed)), flush=True)


@contextmanager
def lock_file(path):
    # Reuse each existing transient lock. Never clear a lock or a business lease.
    with Path(path).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def switch_window(backup, wait_seconds):
    triggers = {name: active(name) for name in TRIGGERS}
    record(backup / 'switch-triggers.json', triggers)
    lane.cmd(sys.executable, str(HEALTH), 'snapshot', '--lane', 'x',
             '--output', str(backup / 'schedulers-before.json'), timeout=60)
    try:
        selected = [name for name, running in triggers.items() if running]
        if selected:
            lane.cmd('systemctl', 'stop', *selected)
        deadline = time.monotonic() + wait_seconds
        while any(active(name) for name in WORKERS):
            if time.monotonic() >= deadline:
                raise RuntimeError('affected_worker_still_running:preserved_retry_later')
            time.sleep(2)
        with ExitStack() as stack:
            for path in LOCKS:
                stack.enter_context(lock_file(path))
            stack.enter_context(lock_file(PORTRAITS / 'refresh.lock'))
            yield
    finally:
        selected = [name for name, running in triggers.items() if running]
        if selected:
            lane.cmd('systemctl', 'start', *selected)
        # The shared health registry currently lists original X units, not tag
        # units. Inspect those read-only; restore only the affected tag triggers.
        lane.cmd(sys.executable, str(HEALTH), 'snapshot', '--lane', 'x',
                 '--output', str(backup / 'schedulers-after.json'), timeout=60)
        record(backup / 'tag-schedulers-after.json', dict(
            triggers={name: active(name) for name in TRIGGERS},
            units=lane.cmd('systemctl', 'show', *TRIGGERS, *WORKERS,
                           '-p', 'Id,ActiveState,SubState,Result,NextElapseUSecRealtime,ExecMainStartTimestamp')))
        if any(not active(name) for name in selected):
            raise RuntimeError('affected_trigger_not_restored')


def switch_current(path):
    temp = lane.CURRENT.with_name('current.hierarchy-next')
    temp.unlink(missing_ok=True)
    temp.symlink_to(path)
    temp.replace(lane.CURRENT)


def reload_services():
    # The automatic service drains request threads on SIGTERM. Existing gateway
    # releases do not, so allow their short control requests to finish first.
    # No post publisher, manual worker, original flow or metric collector restarts.
    for service, port, endpoint in [('x-tag-auto.service', 18843, '/health'),
                                     ('x-tag-gateway.service', 8898, '/healthz'),
                                     (API, 8896, '/healthz')]:
        if service == 'x-tag-gateway.service':
            deadline = time.monotonic() + 120
            while lane.cmd('ss', '-Hnt', 'state', 'established', '( sport = :8898 )'):
                if time.monotonic() >= deadline:
                    raise RuntimeError('gateway_request_in_flight:preserved')
                time.sleep(1)
        lane.cmd('systemctl', 'restart', service, timeout=180)
        lane.health(port, endpoint)


def snapshot_databases(backup):
    for name in TABLES:
        target = backup / 'databases' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect((lane.ROOT / name).as_uri() + '?mode=ro', uri=True)) as db:
            with closing(sqlite3.connect(target)) as saved:
                db.backup(saved)


def restore_files(info):
    if str(lane.CURRENT.resolve()) not in (info['release'], info['previous_current']):
        raise RuntimeError('later_release_present')
    for target, row in info['files'].items():
        if lane.sha(target) not in (row['before'], row['after']):
            raise RuntimeError('later_file_change:' + target)
    for target, row in info['files'].items():
        if row['before'] is None:
            Path(target).unlink(missing_ok=True)
        else:
            lane.atomic(target, Path(row['backup']).read_bytes())
    before = info['previous_pointer']
    lane.atomic(PORTRAITS / before['file'], (Path(info['backup']) / 'portrait-before.json').read_bytes())
    record(PORTRAITS / 'current.json', before)
    record(PORTRAITS / 'status.json', info['previous_status'])
    switch_current(info['previous_current'])
    reload_services()


def activate(backup, wait_seconds):
    mount_check()
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    if source_commit() != info['commit'] or info['status'] != 'staged':
        raise RuntimeError('staged_source_required')
    runtime_files(Path(info['release']))
    # Required by the existing durable-storage validator. Missing in the initial
    # empty-lane bootstrap; creating it does not recreate or replay any task.
    info['media_work_preexisting'] = (lane.ROOT / 'media-work').exists()
    lane.ensure_media_work()
    record(backup / 'deployment.json', info)
    with switch_window(backup, wait_seconds):
        if str(lane.CURRENT.resolve()) != info['previous_current']:
            raise RuntimeError('concurrent_release_switch')
        info['legacy_pids_before'] = lane.pid_state()
        info['post_pid_before'] = lane.cmd('systemctl', 'show', 'x-tag-post.service', '-p', 'MainPID', '--value')
        info['state_before'] = state()
        # Configurations created by the user remain valid. There is no empty-DB gate.
        snapshot_databases(backup)
        info['previous_pointer'] = json.loads((PORTRAITS / 'current.json').read_text())
        info['previous_status'] = json.loads((PORTRAITS / 'status.json').read_text())
        shutil.copy2(PORTRAITS / info['previous_pointer']['file'], backup / 'portrait-before.json')
        for target, row in info['files'].items():
            if lane.sha(target) != row['before'] or lane.sha(row['next']) != row['after']:
                raise RuntimeError('concurrent_file_change:' + target)
            if Path(target).exists():
                saved = Path(row['backup'])
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
        record(backup / 'deployment.json', info)
        try:
            for target, row in info['files'].items():
                lane.atomic(target, Path(row['next']).read_bytes())
            pointer = json.loads((backup / 'portrait-next/current.json').read_text())
            lane.atomic(PORTRAITS / pointer['file'], (backup / 'portrait-next' / pointer['file']).read_bytes())
            pointer['previous'] = info['previous_pointer']['file']
            record(PORTRAITS / 'current.json', pointer)
            lane.atomic(PORTRAITS / 'status.json', (backup / 'portrait-next/status.json').read_bytes())
            switch_current(info['release'])
            reload_services()
            info['legacy_pids_after'] = lane.pid_state()
            info['post_pid_after'] = lane.cmd('systemctl', 'show', 'x-tag-post.service', '-p', 'MainPID', '--value')
            info['legacy_pids_unchanged'] = info['legacy_pids_before'] == info['legacy_pids_after']
            info['post_pid_unchanged'] = info['post_pid_before'] == info['post_pid_after']
            info['state_after'] = state()
            info['status'] = 'active'
            info['activated_at'] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            restore_files(info)
            info.update(status='reverted_code_only', error=type(exc).__name__)
            raise
        finally:
            record(backup / 'deployment.json', info)
    print(json.dumps({key: info[key] for key in ('status', 'commit', 'backup', 'release', 'legacy_pids_unchanged')}), flush=True)


def rollback(backup, wait_seconds):
    mount_check()
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    with switch_window(backup, wait_seconds):
        restore_files(info)
        record(backup / 'rollback.json', dict(status='rolled_back_code_only', business_databases_preserved=True))
    print(json.dumps(dict(status='rolled_back_code_only', backup=str(backup), business_databases_preserved=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage', 'activate', 'rollback'))
    parser.add_argument('--backup')
    parser.add_argument('--wait-seconds', type=int, default=1200)
    args = parser.parse_args()
    if args.action == 'stage':
        stage()
    elif args.action == 'activate':
        activate(args.backup, args.wait_seconds)
    else:
        rollback(args.backup, args.wait_seconds)

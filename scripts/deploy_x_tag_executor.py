#!/usr/bin/env python3
"""Keep the profile selector through the independent executor's claim boundary."""
import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_hierarchy import mount_check, source_commit, lock_file, record, active, switch_current
from scripts.deploy_x_tag_pool_fix import runtime_files, state

BACKUPS = lane.ROOT / 'executor-profile-backups'
TRIGGERS = ('x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path')
WORKERS = ('x-tag-auto-scheduler.service', 'x-tag-auto-runner.service')


@contextmanager
def window(backup, wait_seconds):
    before = {name: active(name) for name in TRIGGERS}
    selected = [name for name, running in before.items() if running]
    record(backup / 'triggers-before.json', before)
    try:
        if selected:
            lane.cmd('systemctl', 'stop', *selected)
        deadline = time.monotonic() + wait_seconds
        while any(active(name) for name in WORKERS):
            if time.monotonic() >= deadline:
                raise RuntimeError('current_auto_worker_preserved:retry_after_completion')
            time.sleep(2)
        with ExitStack() as stack:
            for path in ('/run/x-tag-auto/scheduler.lock', '/run/x-tag-auto/runner.lock'):
                stack.enter_context(lock_file(path))
            yield
    finally:
        if selected:
            lane.cmd('systemctl', 'start', *selected)
        after = {name: active(name) for name in TRIGGERS}
        record(backup / 'triggers-after.json', after)
        if any(not after[name] for name in selected):
            raise RuntimeError('affected_trigger_not_restored')


def restart_auto():
    # The existing server waits for its in-flight non-daemon HTTP threads.
    lane.cmd('systemctl', 'restart', 'x-tag-auto.service', timeout=300)
    lane.health(18843, '/health')


def install(wait_seconds):
    mount_check()
    commit = source_commit()
    previous = lane.CURRENT.resolve(strict=True)
    before = runtime_files(previous)
    release = lane.ROOT / 'releases' / commit
    if not release.exists():
        lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    after = runtime_files(release)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    allowed = {'features/x_auto_posts/publisher.py', 'features/x_tag_publish/staging.py'}
    if not set(changed) <= allowed or 'features/x_auto_posts/publisher.py' not in changed:
        raise RuntimeError('unexpected_executor_runtime_scope:' + json.dumps(changed))
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    lane.cmd(sys.executable, '-B', str(SOURCE / 'scripts/test_x_tag_regression.py'),
             '--runtime', str(release), '--output', str(backup / 'tests'), timeout=300)
    tests = json.loads((backup / 'tests/regression-results.json').read_text())
    if not tests['passed']:
        raise RuntimeError('executor_regression_failed')
    info = dict(commit=commit, previous=str(previous), release=str(release), backup=str(backup),
                changed=changed, tests=tests, status='staged')
    record(backup / 'deployment.json', info)
    print(json.dumps(dict(status='staged', backup=str(backup), tests=tests['count'])), flush=True)
    with window(backup, wait_seconds):
        if lane.CURRENT.resolve() != previous:
            raise RuntimeError('concurrent_runtime_change')
        info['state_before'] = state()
        info['legacy_before'] = lane.pid_state()
        try:
            switch_current(release)
            restart_auto()
            info['state_after'] = state()
            info['legacy_after'] = lane.pid_state()
            info['legacy_pids_unchanged'] = info['legacy_before'] == info['legacy_after']
            info.update(status='active', activated_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            switch_current(previous)
            restart_auto()
            info['status'] = 'reverted_code_only'
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
    with window(backup, wait_seconds):
        if str(lane.CURRENT.resolve()) not in (info['release'], info['previous']):
            raise RuntimeError('later_release_present')
        runtime_files(Path(info['previous']))
        switch_current(info['previous'])
        restart_auto()
    print(json.dumps(dict(status='rolled_back_code_only', backup=str(backup), business_databases_preserved=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--backup')
    parser.add_argument('--wait-seconds', type=int, default=1200)
    args = parser.parse_args()
    install(args.wait_seconds) if args.action == 'install' else rollback(args.backup, args.wait_seconds)

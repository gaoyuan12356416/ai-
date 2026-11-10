#!/usr/bin/env python3
"""GitHub-first ads_ai materialization and scoped reader/UI/runtime rollout."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_hierarchy import mount_check, source_commit, lock_file, record, switch_current, switch_window, validate_tree
from scripts.deploy_x_tag_pool_fix import runtime_files, state
from scripts.deploy_x_tag_revenue_read import restart as restart_api
from features.x_tag_revenue.storage import ProfileWriter, Store, projections, runtime_env
from features.social_analytics.warehouse_refresh import Reader, environment
from features.x_tag_revenue.integration import patch_columns

ROOT = Path('/mnt/data-disk/x-channel-tag-revenue')
BACKUPS = ROOT / 'database-backups'
BASE = '0b8f2efb969585a17518894d9d4ebd5df5d46eda'
CODE = ('storage.py', 'routes.py', 'refresh.py', 'integration.py', 'http_server.py')
UNCHANGED = ('drama-material-api.service', 'x-post-automation.service', 'x-auto-post-service.service', 'x-tag-post.service')


def pids():
    return lane.cmd('systemctl', 'show', *UNCHANGED, '-p', 'Id,MainPID,ActiveState')


def info_at(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    if source_commit() != info['commit']:
        raise RuntimeError('source_version_changed')
    return backup, info


def prepare():
    mount_check(); commit = source_commit()
    previous = lane.CURRENT.resolve(strict=True)
    release = lane.ROOT / 'releases' / commit
    if not release.exists():
        lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    before, after = runtime_files(previous), runtime_files(release)
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    # The active daemon predates the already deployed bd788728 preview UI;
    # staging also carries those reviewed static-only transformations forward.
    allowed = {'features/x_tag_publish/' + n for n in ('policy.py', 'pool.py', 'gateway.py', 'staging.py', 'transport.py')}
    allowed |= {'features/x_tag_revenue/' + n for n in CODE}
    allowed |= {'static/' + n for n in ('x-tag-revenue.js', 'x-tag-revenue.css', 'x-tag-publish-ui.js', 'x-tag-auto-publish-templates.js')}
    allowed |= {'static/' + n for n in ('x-tag-auto-publish-templates.html', 'x-tag-auto-publish-template.html', 'x-tag-auto-publish-runs.html', 'x-tag-material-pool.html', 'x-tag-post-logs.html')}
    if not set(changed) <= allowed:
        raise RuntimeError('unexpected_runtime_scope:' + json.dumps(changed))
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    lane.cmd(sys.executable, '-B', str(SOURCE / 'scripts/test_x_tag_regression.py'), '--runtime', str(release),
             '--output', str(backup / 'regression'), timeout=300)
    tests = json.loads((backup / 'regression/regression-results.json').read_text())
    lane.cmd(sys.executable, '-B', str(SOURCE / 'scripts/test_x_tag_profile_storage.py'), timeout=60)
    if not tests['passed']:
        raise RuntimeError('runtime_regression_failed')
    targets = {}
    for name in CODE:
        relative = 'features/x_tag_revenue/' + name
        target = lane.MAIN / relative
        if name != 'storage.py':
            original = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + relative], check=True, capture_output=True).stdout
            if target.read_bytes() != original:
                raise RuntimeError('concurrent_reader_change:' + name)
        elif target.exists():
            raise RuntimeError('profile_storage_already_installed')
        targets[target] = (SOURCE / relative).read_bytes()
    for root in (lane.WEB, lane.MAIN / 'static'):
        for asset in ('x-tag-revenue.js', 'x-tag-revenue.css'):
            original = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':static/' + asset], check=True, capture_output=True).stdout
            if (root / asset).read_bytes() != original:
                raise RuntimeError('concurrent_asset_change:' + asset)
            targets[root / asset] = (SOURCE / 'static' / asset).read_bytes()
        page = root / 'x-account-list.html'
        text = patch_columns(page.read_text())
        for asset in ('x-tag-revenue.js', 'x-tag-revenue.css'):
            text, count = re.subn(r'(/' + re.escape(asset) + r')(?:\?[^"\']*)?', r'\1?v=' + lane.sha(SOURCE / 'static' / asset)[:12], text)
            if count != 1:
                raise RuntimeError('asset_reference_changed')
        targets[page] = text.encode()
    targets[Path('/etc/systemd/system/x-channel-tag-revenue.service.d/portrait-memory.conf')] = b'[Service]\nMemoryMax=256M\n'
    info = dict(commit=commit, previous=str(previous), release=str(release), backup=str(backup),
                changed=changed, tests=tests, status='prepared', files={}, original_pids=pids())
    for i, (path, content) in enumerate(targets.items()):
        saved, pending = backup / f'{i}.before', backup / f'{i}.next'
        if path.exists():
            shutil.copy2(path, saved)
        pending.write_bytes(content)
        info['files'][str(path)] = dict(before=lane.sha(path), after=lane.sha(pending), backup=str(saved), next=str(pending))
    record(backup / 'deployment.json', info)
    # Only this derived collector lock is needed for initial SQL publication.
    with lock_file(ROOT / 'refresh.lock'):
        pointer = json.loads((ROOT / 'current.json').read_text())
        source = (ROOT / pointer['file']).resolve()
        if source.parent != (ROOT / 'snapshots').resolve():
            raise RuntimeError('invalid_snapshot_path')
        data = json.loads(source.read_text())
        validate_tree(data)
        shutil.copy2(source, backup / 'portrait-before.json')
        env = environment(lane.MAIN / '.env')
        with closing(ProfileWriter(env)) as writer:
            writer.install_schema()
            info['database'] = writer.publish(data, Reader(env))
        actual, meta = Store(env).load(view='tree')
        if actual['accounts'] != {k: projections(p)[1] for k, p in data['accounts'].items()}:
            raise RuntimeError('tree_projection_mismatch')
        info['version'] = meta['version']
    record(backup / 'deployment.json', info)
    print(json.dumps(dict(status='prepared', backup=str(backup), database=info['database'], tests=tests['count'] + 9)), flush=True)


def install_ui(backup):
    mount_check(); backup, info = info_at(backup)
    if info['status'] != 'prepared':
        raise RuntimeError('prepared_state_required')
    with lock_file(ROOT / 'refresh.lock'):
        for target, row in info['files'].items():
            if lane.sha(target) != row['before'] or lane.sha(row['next']) != row['after']:
                raise RuntimeError('concurrent_file_change:' + target)
        try:
            for target, row in info['files'].items():
                lane.atomic(target, Path(row['next']).read_bytes())
            restart_api()
            info.update(status='reader_active', reader_activated_at=datetime.now(timezone.utc).isoformat(), pids_after_reader=pids())
            record(backup / 'deployment.json', info)
        except Exception:
            restore_files(info)
            restart_api()
            raise
    print(json.dumps(dict(status=info['status'], backup=str(backup), original_pids_unchanged=info['original_pids'] == pids())), flush=True)


def restart_publishers():
    lane.cmd('systemctl', 'restart', 'x-tag-auto.service', timeout=300)
    lane.health(18843, '/health')
    deadline = time.monotonic() + 120
    while lane.cmd('ss', '-Hnt', 'state', 'established', '( sport = :8898 )'):
        if time.monotonic() >= deadline:
            raise RuntimeError('gateway_request_in_flight')
        time.sleep(1)
    lane.cmd('systemctl', 'restart', 'x-tag-gateway.service', timeout=60)
    lane.health(8898, '/healthz')


def install_runtime(backup, wait_seconds):
    mount_check(); backup, info = info_at(backup)
    if info['status'] != 'reader_active':
        raise RuntimeError('database_reader_required')
    runtime_files(Path(info['release']))
    with switch_window(backup, wait_seconds):
        if str(lane.CURRENT.resolve()) != info['previous']:
            raise RuntimeError('concurrent_runtime_change')
        info['state_before'] = state()
        try:
            switch_current(info['release'])
            restart_publishers()
            info.update(status='active', state_after=state(), original_pids_unchanged=info['original_pids'] == pids(),
                        activated_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            switch_current(info['previous']); restart_publishers()
            raise
        finally:
            record(backup / 'deployment.json', info)
    print(json.dumps(dict(status=info['status'], backup=str(backup), original_pids_unchanged=info['original_pids_unchanged'])), flush=True)


def restore_files(info):
    for target, row in info['files'].items():
        if lane.sha(target) not in (row['before'], row['after']):
            raise RuntimeError('later_file_change:' + target)
    for target, row in info['files'].items():
        if row['before'] is None:
            Path(target).unlink(missing_ok=True)
        else:
            lane.atomic(target, Path(row['backup']).read_bytes())


def rollback(backup, wait_seconds):
    mount_check(); backup, info = info_at(backup)
    with switch_window(backup, wait_seconds):
        if str(lane.CURRENT.resolve()) not in (info['previous'], info['release']):
            raise RuntimeError('later_release_present')
        if str(lane.CURRENT.resolve()) == info['release']:
            switch_current(info['previous']); restart_publishers()
        restore_files(info); restart_api()
    record(backup / 'rollback.json', dict(status='code_only', database_and_publishing_records_preserved=True))
    print(json.dumps(dict(status='rolled_back_code_only', backup=str(backup))))


def probe():
    from features.x_tag_publish.policy import portrait_snapshot
    data, meta = portrait_snapshot(['63027281'])
    if meta.get('unavailable'):
        raise RuntimeError('profile_internal_read_unavailable')
    print(json.dumps(dict(status='ok', version=meta['version'], accounts=len(data['accounts']),
                         summary_bytes=len(json.dumps(data).encode()), no_full_snapshot_cache=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'reader', 'runtime', 'rollback', 'probe'))
    parser.add_argument('--backup'); parser.add_argument('--wait-seconds', type=int, default=1200)
    args = parser.parse_args()
    if args.action == 'prepare': prepare()
    elif args.action == 'probe': probe()
    elif args.action == 'reader': install_ui(args.backup)
    elif args.action == 'runtime': install_runtime(args.backup, args.wait_seconds)
    else: rollback(args.backup, args.wait_seconds)

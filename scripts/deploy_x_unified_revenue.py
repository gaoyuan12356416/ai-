#!/usr/bin/env python3
"""GitHub-first, scoped migration to one atomic portrait/statistics manifest."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
MAIN = Path('/root/drama_material_service')
ROOT = Path('/mnt/data-disk/x-channel-tag-revenue')
BACKUPS = ROOT / 'unified-backups'
CACHE = Path('/mnt/data-disk/x-account-operating-stats/current.json')
MODULES = ['features/x_account_stats/' + name + '.py' for name in ('__init__', 'service', 'integration')]
MODULES += ['features/x_tag_revenue/' + name + '.py' for name in ('core', 'refresh', 'statistics', 'serving', 'routes')]
MODULES += ['scripts/refresh_x_account_operating_stats.py']
TIMERS = ('x-account-operating-stats.timer', 'x-channel-tag-revenue-refresh.timer')
SCHEDULER = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def cmd(*args, timeout=60, check=True):
    done = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and done.returncode:
        raise RuntimeError('command_failed:' + args[0] + ':' + done.stderr[-300:])
    return done.stdout.strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.unified-next')
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(path)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def overlay():
    # Validate against current live dependencies, not a dated monolithic branch.
    sys.path.insert(0, str(MAIN))
    import features.x_tag_revenue, features.x_account_stats
    features.x_tag_revenue.__path__.insert(0, str(SOURCE / 'features/x_tag_revenue'))
    features.x_account_stats.__path__.insert(0, str(SOURCE / 'features/x_account_stats'))


def verify_source():
    if cmd('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_unavailable')
    revision = cmd('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if cmd('git', '-C', str(SOURCE), 'status', '--porcelain') or not cmd('git', '-C', str(SOURCE), 'branch', '-r', '--contains', revision):
        raise RuntimeError('clean_pushed_commit_required')
    return revision


def prepare(directory):
    revision = verify_source()
    directory = Path(directory).resolve()
    if directory == ROOT.resolve() or Path('/mnt/data-disk') not in directory.parents:
        raise RuntimeError('isolated_data_disk_directory_required')
    overlay()
    from features.x_tag_revenue.refresh import refresh, WAREHOUSE_ROOT
    result = refresh(directory, WAREHOUSE_ROOT, str(MAIN / '.env'), prepare_only=True)
    save(directory / 'prepared.json', dict(commit=revision, result=result))
    print(json.dumps({'prepared': str(directory), **result}), flush=True)


def service_states():
    return {name: cmd('systemctl', 'show', name, '-p', 'ActiveState,MainPID,UnitFileState') for name in (
        'drama-material-api.service', 'drama-material-job-worker.service', 'drama-youtube-publish-worker.service',
        'x-post-automation.service', 'x-tag-post.service', 'x-auto-post-service.service', 'x-tag-auto.service')}


def restart_readers():
    # Preserve the installed distributed-upload drain barrier on the main API.
    cmd('systemctl', 'restart', 'drama-material-api.service', timeout=3600)
    cmd('systemctl', 'restart', 'x-channel-tag-revenue.service', timeout=90)
    for url in ('http://127.0.0.1:8787/api/ui/topbar', 'http://127.0.0.1:8896/healthz'):
        for attempt in range(30):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('reader_health_failed')


def wait_collectors():
    for name in ('x-account-operating-stats.service', 'x-channel-tag-revenue-refresh.service'):
        for _ in range(360):
            state = cmd('systemctl', 'show', name, '-p', 'ActiveState', '--value')
            if state in ('inactive', 'failed'):
                break
            time.sleep(2)
        else:
            raise RuntimeError('collector_still_running:' + name)


def scheduler_snapshot(backup):
    for lane in ('youtube', 'coordinator'):
        cmd('/usr/bin/python3', SCHEDULER, 'snapshot', '--lane', lane, '--output', str(backup / (lane + '-before.json')), timeout=90)


def scheduler_restore(backup):
    for lane in ('youtube', 'coordinator'):
        cmd('/usr/bin/python3', SCHEDULER, 'restore', '--baseline', str(backup / (lane + '-before.json')),
            '--wait-seconds', '25', '--output', str(backup / (lane + '-after.json')), timeout=90)


def install(directory):
    revision = verify_source()
    directory = Path(directory).resolve()
    prepared = json.loads((directory / 'prepared.json').read_text())
    if prepared['commit'] != revision:
        raise RuntimeError('prepared_commit_mismatch')
    head = json.loads((directory / 'current.json').read_text())
    data = json.loads((directory / head['file']).read_text())
    if data['meta']['version'] != prepared['result']['version'] or not data.get('operating_stats'):
        raise RuntimeError('prepared_snapshot_mismatch')
    if (datetime.now(timezone.utc) - datetime.fromisoformat(data['meta']['source_generated_at'])).total_seconds() > 7200:
        raise RuntimeError('prepared_source_stale')
    overlay()
    from features.x_account_stats.integration import patch_html
    from features.x_tag_revenue.storage import ProfileWriter
    from features.x_tag_revenue.serving import publish
    from features.social_analytics.warehouse_refresh import Reader, environment
    baseline = json.loads((SOURCE / 'deploy/x-unified-revenue-baseline.json').read_text())
    targets = {}
    for relative in MODULES + ['static/x-tag-revenue.js']:
        source = (SOURCE / relative).read_bytes()
        dest = MAIN / relative
        if relative in baseline and dest.exists() and sha(dest.read_bytes()) not in (baseline[relative], sha(source)):
            raise RuntimeError('concurrent_source_change:' + relative)
        if relative not in baseline and dest.exists() and dest.read_bytes() != source:
            raise RuntimeError('unexpected_existing_module:' + relative)
        targets[dest] = source
    targets[Path('/usr/share/nginx/html/x-tag-revenue.js')] = (SOURCE / 'static/x-tag-revenue.js').read_bytes()
    for dest in (MAIN / 'static/x-account-list.html', Path('/usr/share/nginx/html/x-account-list.html')):
        targets[dest] = patch_html(dest.read_text(), revision[:12]).encode()
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    state = dict(commit=revision, prepared=str(directory), status='prepared', files={}, services_before=service_states(), timers={})
    for name in TIMERS:
        state['timers'][name] = dict(enabled=cmd('systemctl', 'is-enabled', name, check=False),
                                    active=cmd('systemctl', 'is-active', name, check=False))
    for index, dest in enumerate([*targets, CACHE]):
        original = backup / str(index)
        if dest.exists():
            shutil.copy2(dest, original)
        state['files'][str(dest)] = dict(backup=str(original), before=sha(dest.read_bytes()) if dest.exists() else None,
            after=sha(targets[dest]) if dest in targets else None,
            symlink=str(dest.readlink()) if dest.is_symlink() else None)
    save(backup / 'deployment.json', state)
    scheduler_snapshot(backup)
    print(json.dumps(dict(stage='backup_ready', backup=str(backup))), flush=True)
    for name in TIMERS:
        cmd('systemctl', 'stop', name)
    wait_collectors()
    # Code-only first; existing cache and page continue to work during the drain.
    for dest, payload in targets.items():
        if '/static/' in str(dest) or str(dest).startswith('/usr/share/nginx/'):
            continue
        if (sha(dest.read_bytes()) if dest.exists() else None) != state['files'][str(dest)]['before']:
            raise RuntimeError('concurrent_file_change:' + str(dest))
        atomic(dest, payload)
    state['status'] = 'code_installed'
    save(backup / 'deployment.json', state)
    restart_readers()
    scheduler_restore(backup)
    env = environment(str(MAIN / '.env'))
    with closing(ProfileWriter(env)) as writer:
        proof = writer.publish(data, Reader(env))
    publish(ROOT, data, proof)
    previous = json.loads((ROOT / 'current.json').read_text())
    filename = 'snapshots/' + data['meta']['version'] + '.json'
    save(ROOT / filename, data)
    save(ROOT / 'current.json', dict(file=filename, version=data['meta']['version'], previous=previous.get('file')))
    # One pointer controls both columns. No second copy can drift or be zeroed.
    temp = CACHE.with_name('current.unified-next')
    temp.unlink(missing_ok=True)
    temp.symlink_to(ROOT / 'serving/current.json')
    temp.replace(CACHE)
    save(ROOT / 'status.json', dict(status='ok', attempted_at=datetime.now(timezone.utc).isoformat()))
    for dest, payload in targets.items():
        if '/static/' in str(dest) or str(dest).startswith('/usr/share/nginx/'):
            if (sha(dest.read_bytes()) if dest.exists() else None) != state['files'][str(dest)]['before']:
                raise RuntimeError('concurrent_static_change:' + str(dest))
            atomic(dest, payload)
    cmd('systemctl', 'disable', 'x-account-operating-stats.timer')
    cmd('systemctl', 'enable', '--now', 'x-channel-tag-revenue-refresh.timer')
    state.update(status='active', version=data['meta']['version'], proof=proof, services_after=service_states())
    save(backup / 'deployment.json', state)
    print(json.dumps(dict(status='active', commit=revision, backup=str(backup), version=state['version'])), flush=True)


def rollback(directory):
    backup = Path(directory).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup_path')
    state = json.loads((backup / 'deployment.json').read_text())
    for name in TIMERS:
        cmd('systemctl', 'stop', name)
    wait_collectors()
    for name, row in state['files'].items():
        dest = Path(name)
        if dest == CACHE:
            if row['symlink']:
                tmp = dest.with_name('current.rollback-next'); tmp.symlink_to(row['symlink']); tmp.replace(dest)
            elif row['before']:
                atomic(dest, Path(row['backup']).read_bytes())
            continue
        current = sha(dest.read_bytes()) if dest.exists() else None
        if current == row['before']:
            continue
        if current != row['after']:
            raise RuntimeError('later_change:' + name)
        if row['before'] is None:
            dest.unlink()
        else:
            atomic(dest, Path(row['backup']).read_bytes())
    restart_readers()
    scheduler_restore(backup)
    for name, before in state['timers'].items():
        cmd('systemctl', 'enable' if before['enabled'] == 'enabled' else 'disable', name)
        if before['active'] == 'active':
            cmd('systemctl', 'start', name)
    save(backup / 'rollback.json', dict(status='rolled_back', snapshots_preserved=True))
    print(json.dumps(dict(status='rolled_back', backup=str(backup))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'install', 'rollback'))
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    globals()[args.action](args.directory)

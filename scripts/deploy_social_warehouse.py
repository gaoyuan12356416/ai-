#!/usr/bin/env python3
"""Install/rollback only the report reader, static assets and hourly timer."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/social-analytics-warehouse')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
DROPIN = Path('/etc/systemd/system/drama-material-api.service.d/social-analytics-warehouse.conf')
MODULES = ['features/social_analytics/' + name + '.py'
           for name in ('routes', 'report', 'warehouse_reader', 'warehouse_refresh', 'warehouse_schema')]
ASSETS = ['static/social-analytics.html', 'static/social-analytics.js']
UNITS = ['social-analytics-warehouse-refresh.service', 'social-analytics-warehouse-refresh.timer']
DROPIN_TEXT = '[Service]\nEnvironment=SOCIAL_ANALYTICS_WAREHOUSE_ROOT=' + str(ROOT) + '\n'


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError('command_failed:' + args[0])
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def targets():
    result = {str(MAIN / name): str(SOURCE / name) for name in MODULES + ASSETS}
    result.update({str(WEB / Path(name).name): str(SOURCE / name) for name in ASSETS})
    result.update({str(Path('/etc/systemd/system') / unit): str(SOURCE / 'deploy' / unit) for unit in UNITS})
    result[str(DROPIN)] = None
    return result


def atomic_copy(source, target):
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.warehouse-next')
    shutil.copy2(source, temporary)
    temporary.replace(path)


def install():
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    commit = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if command('git', '-C', str(SOURCE), 'status', '--porcelain'):
        raise RuntimeError('source_dirty')
    if not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('source_not_from_github')
    baseline = json.loads((SOURCE / 'deploy/social-analytics-warehouse-baseline.json').read_text())
    for name, expected in baseline.items():
        if sha(MAIN / name) != expected:
            raise RuntimeError('live_baseline_changed:' + name)
    for name in ASSETS:
        if sha(WEB / Path(name).name) != baseline[name]:
            raise RuntimeError('public_baseline_changed:' + name)
    manifest = json.loads((ROOT / 'current.json').read_text())
    if set(manifest['tables']) != {'social_analytics_' + suffix for suffix in (
            'youtube_daily', 'x_daily', 'facebook_daily', 'link_catalog', 'publication')}:
        raise RuntimeError('verified_snapshot_required')
    backup = ROOT / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True)
    files = {}
    for index, (target, source) in enumerate(targets().items()):
        saved = backup / str(index)
        if Path(target).exists():
            shutil.copy2(target, saved)
        after = sha(source) if source else hashlib.sha256(DROPIN_TEXT.encode()).hexdigest()
        files[target] = dict(before=sha(target), after=after, backup=str(saved), source=source)
    data = dict(commit=commit, source=str(SOURCE), files=files, backup=str(backup), status='prepared')
    (backup / 'deployment.json').write_text(json.dumps(data, indent=2))
    try:
        for target, source in targets().items():
            if source:
                atomic_copy(source, target)
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        DROPIN.write_text(DROPIN_TEXT)
        command('/usr/bin/python3', '-B', '-c', 'import sys;sys.path.insert(0,' + repr(str(MAIN)) + ');import features.social_analytics.warehouse_reader')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'drama-material-api.service')
        command('systemctl', 'is-active', 'drama-material-api.service')
        command('systemctl', 'enable', '--now', UNITS[1])
        data['status'] = 'active'
        (backup / 'deployment.json').write_text(json.dumps(data, indent=2))
    except Exception:
        rollback(backup)
        raise
    print(json.dumps(dict(status='active', commit=commit, backup=str(backup))))


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != (ROOT / 'backups').resolve():
        raise RuntimeError('invalid_backup_path')
    data = json.loads((backup / 'deployment.json').read_text())
    for target, info in data['files'].items():
        if target not in targets():
            raise RuntimeError('unexpected_rollback_target')
        if sha(target) not in {info['before'], info['after']}:
            raise RuntimeError('rollback_target_changed:' + target)
        if info['before'] is not None and sha(info['backup']) != info['before']:
            raise RuntimeError('backup_checksum_mismatch')
    subprocess.run(['systemctl', 'disable', '--now', UNITS[1]], capture_output=True, timeout=30)
    for target, info in data['files'].items():
        if info['before'] is None:
            Path(target).unlink(missing_ok=True)
        else:
            atomic_copy(info['backup'], target)
    command('systemctl', 'daemon-reload')
    command('systemctl', 'restart', 'drama-material-api.service')
    print(json.dumps(dict(status='rolled_back', backup=str(backup))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)

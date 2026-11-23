#!/usr/bin/env python3
"""Install only the standalone daily reader/sender; do not send or restart jobs."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    root = Path(args.folder).resolve()
    if not root.is_relative_to(Path('/mnt/data-disk')):
        raise ValueError('data_disk_required')
    uuid = run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk')
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('data_disk_mount_mismatch')
    sha = run('git', 'rev-parse', 'HEAD', cwd=source)
    if run('git', 'status', '--porcelain', cwd=source):
        raise ValueError('source_not_clean')
    unit = Path('/etc/systemd/system/moboreels-social-daily-report.service')
    current = Path('/opt/moboreels-social-daily-report/current')
    if run('systemctl', 'show', '-p', 'ActiveState', '--value', unit.name) in ('active', 'activating'):
        raise ValueError('daily_sender_is_running')
    root.mkdir(parents=True, exist_ok=False)
    backup = root / 'backup'
    backup.mkdir()
    previous = str(current.resolve()) if current.exists() else None
    old_unit = unit.exists()
    if old_unit:
        shutil.copy2(unit, backup / unit.name)
    release = root / 'release'
    (release / 'features').mkdir(parents=True)
    for package in ('moboreels_library', 'moboreels_daily_report'):
        shutil.copytree(source / 'features' / package, release / 'features' / package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (release / 'scripts').mkdir()
    (release / 'tests').mkdir()
    shutil.copy2(source / 'scripts/moboreels_social_daily_report.py', release / 'scripts')
    shutil.copy2(source / 'tests/test_moboreels_daily_report.py', release / 'tests')
    subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_moboreels_daily_report.py', '-v'],
                   cwd=release, check=True)
    rollback = ["#!/bin/sh", 'set -eu',
        "state=$(systemctl show -p ActiveState --value moboreels-social-daily-report.service)",
        '[ "$state" != active ] && [ "$state" != activating ] || { echo "Wait for this daily sender to finish"; exit 1; }']
    if previous:
        rollback.append('ln -sfn ' + previous + ' ' + str(current))
    else:
        rollback.append('test ! -L ' + str(current) + ' || unlink ' + str(current))
    if old_unit:
        rollback.append('cp ' + str(backup / unit.name) + ' ' + str(unit))
    else:
        rollback.append('rm -f ' + str(unit))
    rollback += ['systemctl daemon-reload', 'echo "Code restored; daily receipts and source jobs preserved. Pause the Codex schedule separately."']
    (root / 'rollback.sh').write_text('\n'.join(rollback) + '\n')
    manifest = dict(sha=sha, source=str(source), release=str(release), previous=previous,
                    unit_existed=old_unit, backup=str(backup), state='/mnt/data-disk/moboreels-social-daily-report',
                    rollback='sh ' + str(root / 'rollback.sh'), files={})
    for path in release.rglob('*.py'):
        manifest['files'][str(path.relative_to(release))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    Path(manifest['state']).mkdir(exist_ok=True, mode=0o700)
    current.parent.mkdir(exist_ok=True, parents=True)
    tmp = current.with_name('current.new')
    tmp.symlink_to(release)
    os.replace(tmp, current)
    shutil.copy2(source / 'deploy' / unit.name, unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print(json.dumps(dict(status='installed_without_send', **manifest), ensure_ascii=False))


if __name__ == '__main__':
    main()

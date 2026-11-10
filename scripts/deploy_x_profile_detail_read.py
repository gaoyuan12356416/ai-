#!/usr/bin/env python3
"""Deploy only the detail projection query; leave publishing processes alone."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_x_tag_revenue_read import cmd, atomic, save, file_sha, sha, parents, restart

BASE = '79b2b601349bdc2045ab5a1ca3611ec996808dd0'
RELATIVE = 'features/x_tag_revenue/storage.py'
TARGET = Path('/root/drama_material_service') / RELATIVE
BACKUPS = Path('/mnt/data-disk/x-channel-tag-revenue/detail-query-backups')


def drained_restart():
    deadline = time.monotonic() + 60
    while cmd('ss', '-Hnt', 'state', 'established', '( sport = :8896 )'):
        if time.monotonic() >= deadline:
            raise RuntimeError('profile_readers_in_flight')
        time.sleep(1)
    restart()


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    if file_sha(TARGET) != info['after']:
        raise RuntimeError('later_storage_change')
    atomic(TARGET, (backup / 'storage.py').read_bytes())
    drained_restart()
    print(json.dumps(dict(status='rolled_back_code_only', backup=str(backup))))


def install():
    if cmd('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    commit = cmd('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if cmd('git', '-C', str(SOURCE), 'status', '--porcelain') or not cmd('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('github_commit_required')
    before = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + RELATIVE], check=True, capture_output=True).stdout
    if TARGET.read_bytes() != before:
        raise RuntimeError('concurrent_storage_change')
    content = (SOURCE / RELATIVE).read_bytes()
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(TARGET, backup / 'storage.py')
    info = dict(commit=commit, before=sha(before), after=sha(content), backup=str(backup), parents_before=parents())
    save(backup / 'deployment.json', info)
    try:
        atomic(TARGET, content)
        drained_restart()
    except Exception:
        atomic(TARGET, before)
        raise
    info.update(status='active', parents_after=parents())
    info['publishing_pids_unchanged'] = info['parents_before'] == info['parents_after']
    save(backup / 'deployment.json', info)
    print(json.dumps({k: info[k] for k in ('status', 'commit', 'backup', 'publishing_pids_unchanged')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('install', 'rollback')); parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)

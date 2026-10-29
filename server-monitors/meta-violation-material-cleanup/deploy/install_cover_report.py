#!/usr/bin/env python3
"""Narrow report deployment using install_cover.py's consistent backup plan.

No worker, API, execution configuration, or execution ledger mutation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from install_cover import ROOT, MODULE, atomic, digest, disk, run


def apply(backup, rollback=False):
    disk()
    backup = backup.resolve()
    if ROOT / 'backups' not in backup.parents:
        raise RuntimeError('invalid_backup_path')
    plan = json.loads((backup / 'plan.json').read_text())
    for name, expected in json.loads((backup / 'hashes.json').read_text()).items():
        if digest(backup / name) != expected:
            raise RuntimeError('backup_changed')
    current = (ROOT / 'current').resolve()
    expected = Path(plan['release'] if rollback else plan['previous_release'])
    if current != expected:
        raise RuntimeError('release_changed_since_backup')
    # A loaded old worker would continue issuing its old per-batch notices.
    # Never implicitly resume a worker that was already stopped or failed.
    for unit in ('meta-cover-worker.service', 'meta-cover-scan.service', 'meta-cover-notify.service'):
        state = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if state not in ('inactive', 'failed'):
            raise RuntimeError('service_not_idle:' + unit)
    release = Path(plan['previous_release'] if rollback else plan['release'])
    if ROOT / 'releases' not in release.resolve().parents:
        raise RuntimeError('invalid_release_path')
    if not rollback:
        if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=release, text=True).strip() != plan['commit']:
            raise RuntimeError('release_commit_changed')
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=release, text=True).strip():
            raise RuntimeError('release_dirty')
    run(['systemctl', 'stop', 'meta-cover-notify.timer'])
    for unit in ('meta-cover-notify.service', 'meta-cover-notify.timer'):
        atomic((backup if rollback else release / MODULE / 'deploy') / unit,
               Path('/etc/systemd/system') / unit)
    link = ROOT / '.current-cover-report'
    if link.is_symlink():
        link.unlink()
    link.symlink_to(release)
    os.replace(str(link), str(ROOT / 'current'))
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'meta-cover-notify.timer'])
    print(json.dumps({'action': 'report-rollback' if rollback else 'report-applied',
                      'release': str(release), 'backup': str(backup),
                      'worker_restarted': False, 'ledgers_restored': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('apply', 'rollback'))
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    apply(args.backup, args.mode == 'rollback')

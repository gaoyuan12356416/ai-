#!/usr/bin/env python3
"""Switch only the independent report release; preserve publisher and report history."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/mnt/data-disk/post-daily-report')
CURRENT = Path('/opt/post-daily-report/current')
BASELINE = '33f6e906e1274cc835371027940059b18e3d4414'
HASHES = {
    'features/post_daily_report/fb.py': '40d7d1d8b5319810c39881fddb06df93b6109e515c8e3ee7b929c484cd9539fa',
    'features/post_daily_report/report.py': '39d2ccda928419b1d08a706c068d0dee907f2b378869cd3b28bafd25d727f736',
}
UNIT = Path('/etc/systemd/system/post-daily-report.service')
UNIT_SHA = '7013272c6f5473a702f027b7591905fb4f35c1851da108a98f64c685f78d3b86'
TIMER = Path('/etc/systemd/system/post-daily-report.timer')
TIMER_SHA = 'ef92230619044e8d6dfe80bef9411c64a0054432f63aaeedc6d1895f601c9971'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('report_data_disk_unavailable')
    if not re.fullmatch(r'[a-f0-9]{40}', ROOT.name) or ROOT.parent != STATE / 'releases':
        raise RuntimeError('candidate_release_path_invalid')
    previous = CURRENT.resolve(strict=True)
    if previous.name != BASELINE:
        raise RuntimeError('report_release_drift')
    for relative, expected in HASHES.items():
        if sha(previous / relative) != expected:
            raise RuntimeError('report_source_drift:' + relative)
    if sha(UNIT) != UNIT_SHA or sha(TIMER) != TIMER_SHA:
        raise RuntimeError('report_unit_drift')
    if shutil.disk_usage(STATE).free < 256 * 1024 * 1024:
        raise RuntimeError('report_data_disk_space_low')
    unchanged = [str(p.relative_to(previous)) for p in (previous / 'features/post_daily_report').glob('*.py')
                 if str(p.relative_to(previous)) not in HASHES]
    unchanged += ['scripts/post_daily_report.py', 'scripts/post_daily_report_correction.py']
    for relative in unchanged:
        if sha(previous / relative) != sha(ROOT / relative):
            raise RuntimeError('unrelated_report_source_changed:' + relative)
    print(json.dumps({'status': 'validated', 'previous_release': str(previous), 'release': str(ROOT)}), flush=True)
    if not args.apply:
        return
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run('systemctl', 'show', 'post-daily-report.service', '-p', 'ActiveState', '--value') not in ('inactive', 'failed'):
            raise RuntimeError('report_service_busy')
        if CURRENT.resolve(strict=True) != previous:
            raise RuntimeError('report_release_changed_before_lock')
        backup = STATE / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-fb-superseded')
        backup.mkdir(exist_ok=False)
        for relative in HASHES:
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous / relative, target)
        shutil.copy2(UNIT, backup / UNIT.name)
        shutil.copy2(TIMER, backup / TIMER.name)
        with closing(sqlite3.connect((STATE / 'delivery.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(str(backup / 'delivery.sqlite3'))) as dest:
                source.backup(dest, pages=256, sleep=.02)
        active = run('systemctl', 'show', 'post-daily-report.timer', '-p', 'ActiveState', '--value') == 'active'
        manifest = {'previous_release': str(previous), 'release': str(ROOT), 'backup': str(backup),
                    'timer_was_active': active, 'publisher_units_changed': False, 'publisher_data_modified': False,
                    'report_unit_sha256': sha(UNIT), 'report_timer_sha256': sha(TIMER),
                    'original_delivery_sha256': sha(STATE / 'delivery.sqlite3')}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        rollback = '#!/bin/bash\nset -euo pipefail\n' \
            + 'exec 9>' + shlex.quote(str(STATE / 'run.lock')) + '\nflock -n 9\n' \
            + 'test "$(systemctl show post-daily-report.service -p ActiveState --value)" = inactive\n' \
            + 'systemctl stop post-daily-report.timer\n' \
            + ('trap "systemctl start post-daily-report.timer" EXIT\n' if active else '') \
            + 'ln -s ' + shlex.quote(str(previous)) + ' /opt/post-daily-report/current.fb-rollback-new\n' \
            + 'mv -Tf /opt/post-daily-report/current.fb-rollback-new /opt/post-daily-report/current\n'
        (backup / 'rollback.sh').write_text(rollback, encoding='utf-8')
        print(json.dumps({'status': 'backed_up', 'backup': str(backup)}), flush=True)
        link = CURRENT.with_name('current.fb-superseded-new')
        if active:
            subprocess.check_call(['systemctl', 'stop', 'post-daily-report.timer'])
        try:
            link.symlink_to(ROOT)
            os.replace(link, CURRENT)
            if CURRENT.resolve() != ROOT or sha(UNIT) != UNIT_SHA or sha(TIMER) != TIMER_SHA:
                raise RuntimeError('installed_report_mismatch')
        except Exception:
            restore = CURRENT.with_name('current.fb-superseded-rollback')
            restore.symlink_to(previous)
            os.replace(restore, CURRENT)
            raise
        finally:
            if active:
                subprocess.check_call(['systemctl', 'start', 'post-daily-report.timer'])
        manifest['status'] = 'installed_no_send'
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print(json.dumps(manifest), flush=True)


if __name__ == '__main__':
    main()

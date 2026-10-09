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
BASELINE = '9b9baf7882bcce3a9e0b199c3a8697d0027d322a'
HASHES = {
    'features/post_daily_report/fb.py': '16629c813c98378d5f96ccbdacef5c540915decb31f4a518b3e40bb547138787',
    'features/post_daily_report/report.py': 'c9fc1d989ca18a4bdc95a7bd96063e0037973fdf92bf55f1db0911c78757d4da',
    'scripts/post_daily_report_correction.py': '89527f50300efef7f8a9059b2e20ba407e133a77a069e3313eb1fbf81c37565f',
}
UNIT = Path('/etc/systemd/system/post-daily-report.service')
UNIT_SHA = '7013272c6f5473a702f027b7591905fb4f35c1851da108a98f64c685f78d3b86'


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
    if sha(UNIT) != UNIT_SHA:
        raise RuntimeError('report_unit_drift')
    print(json.dumps({'status': 'validated', 'previous_release': str(previous), 'release': str(ROOT)}), flush=True)
    if not args.apply:
        return
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run('systemctl', 'show', 'post-daily-report.service', '-p', 'ActiveState', '--value') not in ('inactive', 'failed'):
            raise RuntimeError('report_service_busy')
        backup = STATE / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-fb-actual')
        backup.mkdir(exist_ok=False)
        for relative in HASHES:
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous / relative, target)
        shutil.copy2(UNIT, backup / UNIT.name)
        with closing(sqlite3.connect((STATE / 'delivery.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(str(backup / 'delivery.sqlite3'))) as dest:
                source.backup(dest, pages=256, sleep=.02)
        active = run('systemctl', 'show', 'post-daily-report.timer', '-p', 'ActiveState', '--value') == 'active'
        manifest = {'previous_release': str(previous), 'release': str(ROOT), 'backup': str(backup),
                    'timer_was_active': active, 'publisher_units_changed': False, 'publisher_data_modified': False,
                    'report_unit_sha256': sha(UNIT), 'original_delivery_sha256': sha(STATE / 'delivery.sqlite3')}
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
        link = CURRENT.with_name('current.fb-actual-new')
        if active:
            subprocess.check_call(['systemctl', 'stop', 'post-daily-report.timer'])
        try:
            link.symlink_to(ROOT)
            os.replace(link, CURRENT)
            if CURRENT.resolve() != ROOT or sha(UNIT) != UNIT_SHA:
                raise RuntimeError('installed_report_mismatch')
        except Exception:
            restore = CURRENT.with_name('current.fb-actual-rollback')
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

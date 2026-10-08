#!/usr/bin/env python3
"""Install the report-only X accounting fix, preserving every publisher unit.

Requires the audited prior report release. Does not run a publisher, initialize
publisher schemas, send Feishu messages, or restore publisher databases.
"""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/mnt/data-disk/post-daily-report')
CURRENT = Path('/opt/post-daily-report/current')
BASELINE = '8a0aad86d96722d61094176ee2f7be62de8f944e'
HASHES = {
    'features/post_daily_report/x.py': 'c17eee5b9f1e224402473f13355cbe662d813ca61f187f79c7fdb7905f46b493',
    'features/post_daily_report/report.py': 'b560856341c13b92eec88f9633c2a73000df517fe65fbab8bced066147294d74',
    'scripts/post_daily_report_correction.py': '2b9fbe972143eda5718bacd11c1eb24b3e08de027606aedffd9c65c97631ab44',
    'deploy/post-daily-report.service': '8a3314300f5d6820858faddb9023984b51ef20be6519bb6bf8ade1c60ee922fb',
}
UNIT = Path('/etc/systemd/system/post-daily-report.service')


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('report_data_disk_unavailable')
    previous = CURRENT.resolve(strict=True)
    if previous.name != BASELINE:
        raise RuntimeError('report_release_drift')
    for relative, expected in HASHES.items():
        if sha(previous / relative) != expected:
            raise RuntimeError('report_source_drift:' + relative)
    if sha(UNIT) != HASHES['deploy/post-daily-report.service']:
        raise RuntimeError('report_unit_drift')
    subprocess.check_call(['systemd-analyze','verify',str(ROOT/'deploy/post-daily-report.service')])
    print(json.dumps({'status':'validated','previous_release':str(previous),'release':str(ROOT)}),flush=True)
    if not args.apply:
        return
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run('systemctl','show','post-daily-report.service','-p','ActiveState','--value') not in ('inactive','failed'):
            raise RuntimeError('report_service_busy')
        backup = STATE/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-x-cost')
        backup.mkdir(exist_ok=False)
        shutil.copy2(UNIT, backup / UNIT.name)
        for name, path in (('delivery', STATE/'delivery.sqlite3'),
                ('x','/var/lib/x-post-automation/accounts.sqlite3'),
                ('x_auto','/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3')):
            with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as source:
                source.execute('PRAGMA query_only=ON')
                with closing(sqlite3.connect(str(backup/(name+'.sqlite3')))) as dest:
                    source.backup(dest,pages=256,sleep=.02)
        active = run('systemctl','show','post-daily-report.timer','-p','ActiveState','--value') == 'active'
        manifest = {'previous_release':str(previous),'release':str(ROOT),'backup':str(backup),
            'timer_was_active':active,'publisher_units_changed':False,'publisher_data_modified':False}
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        # A saved command is reviewable and restores code only, never ledger data.
        (backup/'rollback.sh').write_text('#!/bin/bash\nset -euo pipefail\n'
            'systemctl stop post-daily-report.timer\n'
            'ln -s '+str(previous)+' /opt/post-daily-report/rollback-link\n'
            'mv -Tf /opt/post-daily-report/rollback-link /opt/post-daily-report/current\n'
            'cp '+str(backup/UNIT.name)+' '+str(UNIT)+'\n'
            'systemctl daemon-reload\n'+('systemctl start post-daily-report.timer\n' if active else ''))
        print(json.dumps({'status':'backed_up','backup':str(backup)}),flush=True)
        if active:
            subprocess.check_call(['systemctl','stop','post-daily-report.timer'])
        try:
            shutil.copy2(ROOT/'deploy/post-daily-report.service', UNIT)
            link = CURRENT.with_name('current.x-cost-new')
            link.symlink_to(ROOT)
            os.replace(link, CURRENT)
            subprocess.check_call(['systemctl','daemon-reload'])
            if sha(UNIT) != sha(ROOT/'deploy/post-daily-report.service') or CURRENT.resolve() != ROOT:
                raise RuntimeError('installed_report_mismatch')
        except Exception:
            link = CURRENT.with_name('current.x-cost-rollback')
            link.symlink_to(previous)
            os.replace(link,CURRENT)
            shutil.copy2(backup/UNIT.name,UNIT)
            subprocess.check_call(['systemctl','daemon-reload'])
            raise
        finally:
            if active:
                subprocess.check_call(['systemctl','start','post-daily-report.timer'])
        manifest['status']='installed_no_send'
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        print(json.dumps(manifest),flush=True)


if __name__ == '__main__': main()

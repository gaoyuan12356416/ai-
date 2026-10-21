#!/usr/bin/env python3
"""Narrow GitHub-first daily-report release; preserve the formal delivery ledger."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
STATE = Path('/mnt/data-disk/youtube-publisher-daily-report')
LINK = Path('/opt/youtube-publisher-daily-report/current')
BASELINE = '9b4f453620e04ad9c7a35eeedd82ae114842029b'
FILES = (
    'features/social_daily_report/report.py',
    'features/social_daily_report/collector.py',
    'features/social_analytics/routes.py',
    'features/social_analytics/backend_scope.py',
    'features/social_analytics/facebook.py',
    'features/social_analytics/report.py',
    'features/post_daily_report/delivery.py',
    'scripts/social_publisher_daily_report.py',
    'scripts/social_daily_private_preview.py',
)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def switch(target):
    temporary = LINK.with_name('current.owner-card-next')
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError('pending_release_link_exists')
    temporary.symlink_to(target)
    temporary.replace(LINK)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha')
    parser.add_argument('--activate')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    if sum(bool(x) for x in (args.sha, args.activate, args.rollback)) != 1:
        raise ValueError('one_operation_required')
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    if shutil.disk_usage(STATE).free < 1024 ** 3:
        raise RuntimeError('insufficient_data_disk')
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if command('systemctl', 'show', 'youtube-publisher-daily-report.service', '-p', 'ActiveState', '--value') != 'inactive':
            raise RuntimeError('daily_report_service_busy')
        if args.activate or args.rollback:
            manifest = Path(args.activate or args.rollback)
            state = json.loads(manifest.read_text())
            expected = state['previous'] if args.activate else state['release']
            target = state['release'] if args.activate else state['previous']
            if str(LINK.resolve()) != expected:
                raise RuntimeError('current_release_changed')
            hashes = state['hashes'] if args.activate else state['previous_hashes']
            for relative, checksum in hashes.items():
                if digest(Path(target) / relative) != checksum:
                    raise RuntimeError('release_file_changed:' + relative)
            switch(target)
            state['status'] = 'active' if args.activate else 'rolled_back'
            manifest.write_text(json.dumps(state, indent=2) + '\n')
            print(json.dumps({'status': state['status'], 'current': str(LINK.resolve()), 'manifest': str(manifest)}))
            return
        sha = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
        if sha != args.sha or len(sha) != 40:
            raise RuntimeError('source_sha_mismatch')
        if command('git', '-C', str(SOURCE), 'status', '--porcelain'):
            raise RuntimeError('dirty_source')
        if not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', sha):
            raise RuntimeError('source_not_on_remote')
        previous = LINK.resolve()
        previous_hashes = {}
        for relative in FILES:
            old = subprocess.run(['git', '-C', str(SOURCE), 'show', BASELINE + ':' + relative], capture_output=True)
            path = previous / relative
            if old.returncode:
                if path.exists():
                    raise RuntimeError('unexpected_existing_file:' + relative)
            else:
                checksum = hashlib.sha256(old.stdout.replace(b'\r\n', b'\n')).hexdigest()
                if digest(path) != checksum:
                    raise RuntimeError('live_baseline_drift:' + relative)
                previous_hashes[relative] = checksum
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-owner-card'
        backup = STATE / 'backups' / stamp
        backup.mkdir(mode=0o700)
        source_db = sqlite3.connect((STATE / 'delivery.sqlite3').as_uri() + '?mode=ro', uri=True)
        target_db = sqlite3.connect(backup / 'delivery.sqlite3')
        try:
            source_db.backup(target_db)
        finally:
            source_db.close(); target_db.close()
        release = STATE / 'releases' / (sha + '-owner-card')
        shutil.copytree(previous, release, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for relative in FILES:
            shutil.copy2(SOURCE / relative, release / relative)
        command('python3', '-m', 'py_compile', *(str(release / relative) for relative in FILES))
        state = dict(status='staged', sha=sha, previous=str(previous), release=str(release), backup=str(backup),
                     previous_hashes=previous_hashes, hashes={r: digest(release / r) for r in FILES})
        manifest = backup / 'deployment.json'
        manifest.write_text(json.dumps(state, indent=2) + '\n')
        print(json.dumps(dict(status='staged', release=str(release), manifest=str(manifest))))


if __name__ == '__main__':
    main()

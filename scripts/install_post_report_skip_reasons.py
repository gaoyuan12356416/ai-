#!/usr/bin/env python3
"""Stage a pinned report-only overlay; preserve publishers and sent reports."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

LINK = Path('/opt/post-daily-report/current')
STATE = Path('/mnt/data-disk/post-daily-report')
BASELINE = {
    'features/post_daily_report/presentation.py': '8480ee40fec2145f40284ddbe14af4ee438094e7eac62cd2c03157f12773aaca',
    'features/post_daily_report/x.py': '1ee1db6dc58695207d19260ecd1a86a5fca755f62a8cbc493da843d344e3ae10',
    'features/post_daily_report/fb.py': '87871ba25e9145d6e3814a81d6f0cea61df0815ebc9354185b9c48e846b59756',
}
OVERLAY = list(BASELINE) + ['tests/test_post_daily_report_fb.py',
                          'tests/test_post_daily_report_presentation.py']

TESTS = ['tests.test_post_daily_report_read_recovery', 'tests.test_post_daily_report',
         'tests.test_post_daily_report_presentation', 'tests.test_post_daily_report_x.XCollectorTests',
         'tests.test_post_daily_report_x_profile_targets.ProfileTargetTests',
         'tests.test_post_daily_report_fb.FBReportTests', 'tests.test_post_daily_report_correction']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt_digest():
    result = hashlib.sha256()
    for path in sorted((STATE / 'reports').rglob('*')) + [STATE / 'delivery.sqlite3']:
        if path.is_file():
            result.update(str(path.relative_to(STATE)).encode())
            result.update(path.read_bytes())
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    disk = subprocess.check_output(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk'], text=True).strip()
    if disk != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk missing')
    if shutil.disk_usage('/mnt/data-disk').free < 1024 ** 3:
        raise RuntimeError('insufficient data disk space')
    git = ['git', '--git-dir=' + str(STATE / 'repo.git')]
    commit = subprocess.check_output(git + ['rev-parse', args.commit + '^{commit}'], text=True).strip()
    deployment = Path('/mnt/data-disk/post-daily-recovery-20261120') / commit[:12]
    deployment.mkdir(parents=True, exist_ok=True)
    release = deployment / 'report'
    manifest_path = deployment / 'manifest.json'
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = LINK.resolve()
        for rel, expected in BASELINE.items():
            if digest(previous / rel) != expected:
                raise RuntimeError('live baseline changed: ' + rel)
        receipts = receipt_digest()
        units = subprocess.check_output(['systemctl', 'cat', 'post-daily-report.service', 'post-daily-report.timer'])
        if not manifest_path.exists():
            shutil.copytree(previous, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git'))
            (deployment / 'units-before.txt').write_bytes(units)
            for rel in OVERLAY:
                target = release / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(git + ['show', commit + ':' + rel]))
            result = subprocess.run(['python3', '-m', 'unittest', *TESTS, '-q'], cwd=release,
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True, text=True)
            (deployment / 'tests.log').write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError('candidate tests failed; see tests.log')
            manifest = {'commit': commit, 'previous': str(previous), 'release': str(release),
                        'before': BASELINE, 'after': {rel: digest(release / rel) for rel in OVERLAY},
                        'receipt_digest': receipts, 'status': 'staged'}
            rollback = '''#!/bin/sh
set -eu
exec 9>>/mnt/data-disk/post-daily-report/run.lock
flock -n 9
test "$(readlink -f /opt/post-daily-report/current)" = '%s'
ln -s '%s' /opt/post-daily-report/.rollback-recovery
mv -Tf /opt/post-daily-report/.rollback-recovery /opt/post-daily-report/current
''' % (release, previous)
            (deployment / 'rollback.sh').write_text(rollback)
            (deployment / 'rollback.sh').chmod(0o700)
        else:
            manifest = json.loads(manifest_path.read_text())
            if manifest['previous'] != str(previous):
                raise RuntimeError('previous release changed')
            for rel, expected in manifest['after'].items():
                if digest(release / rel) != expected:
                    raise RuntimeError('candidate changed: ' + rel)
        if args.apply:
            acceptance = json.loads((deployment / 'acceptance.json').read_text())
            if acceptance.get('commit') != commit or not acceptance.get('sandbox_preview_passed'):
                raise RuntimeError('candidate preview has not passed')
            if receipts != manifest['receipt_digest']:
                raise RuntimeError('new delivery since staging')
            temporary = LINK.with_name('.read-recovery-' + commit[:12])
            temporary.symlink_to(release)
            os.replace(temporary, LINK)
            manifest['status'] = 'active'
        assert receipt_digest() == receipts
        assert subprocess.check_output(['systemctl', 'cat', 'post-daily-report.service', 'post-daily-report.timer']) == units
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(json.dumps(manifest))


if __name__ == '__main__':
    main()

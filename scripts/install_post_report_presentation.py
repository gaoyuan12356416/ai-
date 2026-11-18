#!/usr/bin/env python3
"""Stage a GitHub-verified presentation overlay and switch only the daily report."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

LINK = Path('/opt/post-daily-report/current')
STATE = Path('/mnt/data-disk/post-daily-report')
BASELINE = {
    'features/post_daily_report/report.py': '1951d1d90ce3bd5194b809171cbb70778cca7cc016612f6c556d965254979d66',
    'scripts/post_daily_report_correction.py': '41cac001181011afcb5aaf69c33e09bfa4b371b4fe41a793931cb8d1a16f83ec',
    'scripts/post_daily_report.py': '5174c064c922f72df0d6b245e6d85d07803a4f0b89ef991a7cac1fd4154bcf42',
}
OVERLAY = ['features/post_daily_report/report.py', 'features/post_daily_report/presentation.py',
           'scripts/post_daily_report_correction.py', 'tests/test_post_daily_report.py',
           'tests/test_post_daily_report_fb.py', 'tests/test_post_daily_report_presentation.py']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipts_digest():
    digestor = hashlib.sha256()
    for path in sorted((STATE / 'reports').rglob('*')):
        if path.is_file():
            digestor.update(str(path.relative_to(STATE)).encode())
            digestor.update(path.read_bytes())
    digestor.update((STATE / 'delivery.sqlite3').read_bytes())
    return digestor.hexdigest()


VALIDATE = r'''
import hashlib,json,re,sys
from pathlib import Path
from features.post_daily_report.report import build_card,build_compact_card,preview_html
folder=Path(sys.argv[1]); output=Path(sys.argv[2]); checks=[]
for source in sorted(folder.glob('*/report.json')):
    report=json.loads(source.read_text())
    before=json.dumps(report,sort_keys=True)
    card=build_card(report)
    def size(card):
        return len(json.dumps({'receive_id':'oc_'+'x'*32,'msg_type':'interactive','content':json.dumps(card,ensure_ascii=False,separators=(',',':')),'uuid':'0'*36},ensure_ascii=False).encode())
    if size(card)>29000: card=build_compact_card(report)
    assert size(card)<=29000, (source,size(card))
    assert json.dumps(report,sort_keys=True)==before
    assert not any(x in json.dumps(card,ensure_ascii=False) for x in ['抖音国际版','脸书','推特'])
    checks.append({'date':report['date'],'bytes':size(card)})
    (output/'card.json').write_text(json.dumps(card,ensure_ascii=False,indent=2))
    (output/'preview.html').write_text(preview_html(report,card))
assert checks
print(json.dumps({'archived_reports_validated':len(checks),'max_bytes':max(r['bytes'] for r in checks),'latest':checks[-1]}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if subprocess.check_output(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk'], text=True).strip() != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk missing')
    source = Path(args.source).resolve()
    repository = STATE / 'repo.git'
    commit = subprocess.check_output(['git', '--git-dir=' + str(repository), 'rev-parse', args.commit + '^{commit}'], text=True).strip()
    for rel in OVERLAY:
        expected = subprocess.check_output(['git', '--git-dir=' + str(repository), 'show', commit + ':' + rel])
        if (source / rel).read_bytes() != expected:
            raise RuntimeError('source differs from fetched commit: ' + rel)
    deployment = Path('/mnt/data-disk/post-daily-polish-20261118') / commit[:12]
    deployment.mkdir(parents=True, exist_ok=True)
    release = deployment / 'report'
    manifest_path = deployment / 'manifest.json'
    with (STATE / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = LINK.resolve()
        for rel, expected in BASELINE.items():
            if digest(previous / rel) != expected:
                raise RuntimeError('live baseline changed: ' + rel)
        original_digest = receipts_digest()
        units = subprocess.check_output(['systemctl', 'cat', 'post-daily-report.service', 'post-daily-report.timer'])
        if not manifest_path.exists():
            shutil.copytree(previous, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git'))
            for rel in OVERLAY:
                target = release / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, target)
            with sqlite3.connect('file:' + str(STATE / 'delivery.sqlite3') + '?mode=ro', uri=True) as old:
                with sqlite3.connect(str(deployment / 'delivery-backup.sqlite3')) as backup:
                    old.backup(backup)
            (deployment / 'units-before.txt').write_bytes(units)
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            for pattern in ['test_post_daily_report.py', 'test_post_daily_report_presentation.py', 'test_post_daily_report_correction.py']:
                result = subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-p', pattern, '-q'],
                                        cwd=release, env=environment, capture_output=True, text=True)
                (deployment / (pattern + '.log')).write_text(result.stdout + result.stderr)
                if result.returncode:
                    raise RuntimeError('candidate test failed: ' + pattern)
            result = subprocess.run(['python3', '-c', VALIDATE, str(STATE / 'reports'), str(deployment)],
                                    cwd=release, env=environment, capture_output=True, text=True)
            (deployment / 'validation.log').write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError('archive render failed; see validation.log')
            validation = json.loads(result.stdout)
            manifest = {'commit': commit, 'previous': str(previous), 'release': str(release),
                        'before': BASELINE, 'after': {rel: digest(release / rel) for rel in OVERLAY},
                        'receipt_digest': original_digest, 'validation': validation, 'status': 'staged'}
            manifest_path.write_text(json.dumps(manifest, indent=2))
            rollback = '''#!/bin/sh
set -eu
exec 9>>/mnt/data-disk/post-daily-report/run.lock
flock -n 9
test "$(readlink -f /opt/post-daily-report/current)" = '%s'
tmp=/opt/post-daily-report/.rollback-polish-$$
ln -s '%s' "$tmp"
mv -Tf "$tmp" /opt/post-daily-report/current
readlink -f /opt/post-daily-report/current
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
            if original_digest != manifest['receipt_digest']:
                raise RuntimeError('new delivery since staging; review before switching')
            temp = LINK.with_name('.presentation-' + commit[:12])
            temp.symlink_to(release)
            os.replace(temp, LINK)
            manifest['status'] = 'active'
        assert receipts_digest() == original_digest
        assert subprocess.check_output(['systemctl', 'cat', 'post-daily-report.service', 'post-daily-report.timer']) == units
        manifest['timer'] = subprocess.check_output(['systemctl', 'show', 'post-daily-report.timer', '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'NextElapseUSecRealtime'], text=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(json.dumps(manifest))


if __name__ == '__main__':
    main()

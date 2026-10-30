"""Deploy only the FB report classification and accepted link-type filters."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/mnt/data-disk/social-fb-pool-breakdown-20261030')
MAIN = Path('/root/drama_material_service')
LINK = Path('/opt/youtube-publisher-daily-report/current')
FILES = ('features/social_analytics/facebook.py', 'features/social_analytics/report.py')
BEFORE = ('58ff73828eeb62a4558119226bb860bef3cdcfab16349fc1866761fd6e5324a9',
          '63e3682cb5d946c28f5f06586b9ceabaa2587269640894841b950be44ee7c3d5')
DAILY_REPORT = '4055fda2350e030f6416649063e2faaa1f0f4484270471dd34fe45c22e33c30e'
OLD = 'if filters["link_type"] - {"auto", "manual", "auto_template", "system"}:'
NEW = 'if filters["link_type"] - {"auto", "manual", "auto_template", "system", "pool", "group_pool"}:'


def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_file(source, target):
    temporary = target.with_name(target.name + '.pool-breakdown-next')
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['stage', 'apply', 'rollback'])
    args = parser.parse_args()
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    if shutil.disk_usage(STATE.parent).free < 1024**3:
        raise RuntimeError('insufficient_space')
    STATE.mkdir(exist_ok=True, mode=0o700)
    manifest = STATE / 'deployment.json'
    with Path('/mnt/data-disk/youtube-publisher-daily-report/run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'stage':
            if manifest.exists():
                raise RuntimeError('already_staged')
            commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
            if not run('git', '-C', str(ROOT), 'branch', '-r', '--contains', commit):
                raise RuntimeError('source_not_on_github')
            if run('git', '-C', str(ROOT), 'status', '--porcelain'):
                raise RuntimeError('dirty_source')
            previous = LINK.resolve()
            if [sha(MAIN / name) for name in FILES] != list(BEFORE):
                raise RuntimeError('main_baseline_changed')
            if [sha(previous / name) for name in FILES] != [BEFORE[0], DAILY_REPORT]:
                raise RuntimeError('daily_baseline_changed')
            release = previous.parent / (commit[:12] + '-fb-pool-breakdown')
            shutil.copytree(previous, release, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(ROOT / FILES[0], release / FILES[0])
            # The independent daily report has a different base. Preserve it.
            report = (release / FILES[1]).read_text()
            if report.count(OLD) != 1:
                raise RuntimeError('daily_filter_not_unique')
            (release / FILES[1]).write_text(report.replace(OLD, NEW))
            for name in FILES:
                backup = STATE / 'before' / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(MAIN / name, backup)
                run('python3', '-m', 'py_compile', str(ROOT / name), str(release / name))
            source = sqlite3.connect('file:/mnt/data-disk/social-analytics/attribution.sqlite3?mode=ro', uri=True)
            target = sqlite3.connect(STATE / 'attribution.snapshot.sqlite3')
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            state = dict(commit=commit, previous=str(previous), release=str(release),
                         before=list(BEFORE), after=[sha(ROOT / name) for name in FILES],
                         daily_after=[sha(release / name) for name in FILES], status='staged')
        else:
            state = json.loads(manifest.read_text())
            forward = args.action == 'apply'
            if str(LINK.resolve()) not in {state['previous'], state['release']}:
                raise RuntimeError('release_pointer_changed')
            for i, name in enumerate(FILES):
                if sha(MAIN / name) not in {state['before'][i], state['after'][i]}:
                    raise RuntimeError('live_file_changed')
            if forward:
                evidence = json.loads((STATE / 'canary.json').read_text())
                if evidence['status'] != 'complete' or evidence['after'] != state['after']:
                    raise RuntimeError('canary_not_verified')
            source_root = ROOT if forward else STATE / 'before'
            expected = state['after'] if forward else state['before']
            if [sha(source_root / name) for name in FILES] != expected:
                raise RuntimeError('source_changed')
            target = Path(state['release'] if forward else state['previous'])
            expected_daily = state['daily_after'] if forward else [BEFORE[0], DAILY_REPORT]
            if [sha(target / name) for name in FILES] != expected_daily:
                raise RuntimeError('daily_release_changed')
            for name in FILES:
                replace_file(source_root / name, MAIN / name)
            temporary = LINK.with_name('current.pool-breakdown-next')
            temporary.symlink_to(target)
            os.replace(temporary, LINK)
            state['status'] = 'active' if forward else 'rolled_back'
        manifest.write_text(json.dumps(state, indent=2) + '\n')
        print(json.dumps(state))


if __name__ == '__main__':
    main()

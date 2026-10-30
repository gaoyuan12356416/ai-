"""Code-only overlay; report-owned additive evidence, no publisher writes."""
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
STATE = Path('/mnt/data-disk/social-fb-pool-owner-20261030')
LINK = Path('/opt/youtube-publisher-daily-report/current')
MAIN = Path('/root/drama_material_service/features/social_analytics/facebook.py')
RELATIVE = 'features/social_analytics/facebook.py'
BASELINE = 'b3436a2abe725a460d30c064fff13395372abc43eb12c4ecf11232f3acd9c3f4'


def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def switch(target):
    temporary = LINK.with_name('current.pool-owner-next')
    temporary.symlink_to(target)
    os.replace(temporary, LINK)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['stage', 'apply', 'rollback'])
    args = parser.parse_args()
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    if shutil.disk_usage(STATE.parent).free < 1024**3:
        raise RuntimeError('insufficient_space')
    STATE.mkdir(exist_ok=True, mode=0o700)
    manifest_path = STATE / 'deployment.json'
    with Path('/mnt/data-disk/youtube-publisher-daily-report/run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'stage':
            if manifest_path.exists():
                raise RuntimeError('deployment_already_staged')
            commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
            if not run('git', '-C', str(ROOT), 'branch', '-r', '--contains', commit):
                raise RuntimeError('source_not_on_github')
            if run('git', '-C', str(ROOT), 'status', '--porcelain'):
                raise RuntimeError('dirty_source')
            previous = LINK.resolve()
            if sha(MAIN) != BASELINE or sha(previous / RELATIVE) != BASELINE:
                raise RuntimeError('live_baseline_changed')
            release = previous.parent / (commit[:12] + '-fb-pool-owner')
            shutil.copytree(previous, release, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(ROOT / RELATIVE, release / RELATIVE)
            run('python3', '-m', 'py_compile', str(release / RELATIVE))
            shutil.copy2(MAIN, STATE / 'facebook.before.py')
            source = sqlite3.connect('file:/mnt/data-disk/social-analytics/attribution.sqlite3?mode=ro', uri=True)
            target = sqlite3.connect(STATE / 'attribution.before.sqlite3')
            try:
                source.backup(target)
            finally:
                source.close(); target.close()
            state = dict(commit=commit, previous=str(previous), release=str(release),
                         before=BASELINE, after=sha(release / RELATIVE), status='staged')
        else:
            state = json.loads(manifest_path.read_text())
            forward = args.action == 'apply'
            expected = state['previous'] if forward else state['release']
            checksum = state['before'] if forward else state['after']
            if str(LINK.resolve()) != expected or sha(MAIN) != checksum:
                raise RuntimeError('live_state_changed')
            if forward:
                evidence = json.loads((STATE / 'canary.json').read_text())
                if evidence['status'] != 'complete' or evidence['code_sha256'] != state['after']:
                    raise RuntimeError('canary_not_verified')
                # Populate the additive reporting table before the new reader
                # is loaded. The regular collector uses its existing lock.
                run('python3', str(Path(state['release']) / 'scripts/social_facebook_attribution.py'))
            target = Path(state['release'] if forward else state['previous'])
            source_file = target / RELATIVE
            if sha(source_file) != (state['after'] if forward else state['before']):
                raise RuntimeError('release_changed')
            temporary = MAIN.with_name('facebook.py.pool-owner-next')
            shutil.copy2(source_file, temporary)
            os.replace(temporary, MAIN)
            switch(target)
            state['status'] = 'active' if forward else 'rolled_back'
        manifest_path.write_text(json.dumps(state, indent=2) + '\n')
        print(json.dumps(state))


if __name__ == '__main__':
    main()

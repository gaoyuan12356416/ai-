"""Deploy only manual release-time changes, retaining live composite overlays."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
BASE = '9a172eef'
ROOT = Path('/mnt/data-disk/x-manual-release-time-20261117')
POINTER = Path('/opt/x-post-automation/current')
MAIN = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
CODE = ('features/x_posts/selector.py', 'features/x_video_publish/core.py',
        'features/x_video_publish/sources.py', 'features/x_video_publish/service.py',
        'features/x_video_publish/identity_broker.py')
ASSETS = ('static/x-video-publish.js', 'static/x-video-account-settings.js', 'static/x-video-publish.html')
OWNERS = ('x-post-automation.service', 'drama-material-api.service')
DEPENDENTS = ('drama-material-job-worker.service', 'drama-youtube-publish-worker.service')
TRIGGERS = ('x-post-manual.timer', 'x-post-schedule.timer', 'x-post-schedule-claim.timer',
            'x-post-daily.timer', 'x-auto-post-scheduler.timer')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True, **kwargs).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(folder, record):
    path = folder / 'record.next'
    path.write_text(json.dumps(record, indent=2) + '\n')
    path.chmod(0o600)
    path.replace(folder / 'record.json')


def states(units):
    return {u: run('systemctl', 'show', u, '-p', 'ActiveState', '--value').decode().strip() for u in units}


def scheduler(folder, action, prefix=''):
    for lane in ('x', 'youtube'):
        args = [sys.executable, HEALTH, action, '--lane', lane, '--output', folder / (prefix + lane + '-' + action + '.json')]
        if action == 'restore':
            args += ['--baseline', folder / (prefix + lane + '-snapshot.json'), '--wait-seconds', '25']
        result = subprocess.run(list(map(str, args)), capture_output=True)
        (folder / (prefix + lane + '-' + action + '.log')).write_bytes(result.stdout + result.stderr)
        assert result.returncode == 0, 'scheduler_unhealthy:' + lane + ':' + action


def patch_files(target):
    patch = run('git', '-C', SOURCE, 'diff', '--binary', BASE, 'HEAD', '--', *CODE)
    run('git', 'apply', '--unsafe-paths', '--directory=' + str(target), input=patch)


def token_fingerprint():
    return {p.name: [sha(p), p.stat().st_mode & 0o777, p.stat().st_uid, p.stat().st_gid]
            for p in Path('/var/lib/x-post-automation/tokens').glob('*') if p.is_file()}


def prepare():
    assert run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk').decode().strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', SOURCE, 'status', '--porcelain'), 'dirty_source'
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD').decode().strip()
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'source_not_pushed'
    folder = ROOT / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + commit[:8])
    folder.mkdir(parents=True)
    (folder / 'backup').mkdir(mode=0o700)
    live = POINTER.resolve(strict=True)
    candidate = folder / 'candidate'
    run('cp', '-a', '--reflink=auto', live, candidate)
    record = dict(status='preparing', commit=commit, before=str(live), after=str(candidate), files={}, runtime_before={})
    for relative in CODE:
        record['runtime_before'][relative] = sha(live / relative)
    patch_files(candidate)
    staged_main = folder / 'main'
    for relative in CODE:
        target = staged_main / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MAIN / relative, target)
    patch_files(staged_main)
    # Preserve the verified public/main UI as the active source for future releases.
    for relative in ASSETS:
        approved_before = run('git', '-C', SOURCE, 'show', BASE + ':' + relative)
        for root in (MAIN, PUBLIC):
            target = root / (relative if root == MAIN else Path(relative).name)
            assert target.read_bytes() == approved_before, 'concurrent_ui_change:' + str(target)
        shutil.copy2(SOURCE / relative, candidate / relative)
    for relative in CODE + ASSETS:
        destinations = [MAIN / relative]
        if relative in ASSETS:
            destinations.append(PUBLIC / Path(relative).name)
        for target in destinations:
            backup = folder / 'backup' / ('file-' + str(len(record['files'])))
            shutil.copy2(target, backup)
            staged = (staged_main if relative in CODE else SOURCE) / relative
            record['files'][str(target)] = dict(backup=str(backup), source=str(staged), before=sha(target), after=sha(staged))
    run(sys.executable, '-m', 'py_compile', *(candidate / p for p in CODE), *(staged_main / p for p in CODE))
    tests = folder / 'tests'
    run('cp', '-a', '--reflink=auto', candidate, tests)
    shutil.copy2(SOURCE / 'scripts/test_x_manual_release_time.py', tests / 'scripts/test_x_manual_release_time.py')
    result = subprocess.run([sys.executable, '-m', 'unittest', 'scripts.test_x_manual_release_time', '-v'], cwd=tests,
                            env=dict(os.environ, PYTHONPATH=str(tests)), capture_output=True)
    (folder / 'tests.log').write_bytes(result.stdout + result.stderr)
    assert result.returncode == 0, 'candidate_tests_failed'
    with closing(sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro', uri=True)) as db, \
         closing(sqlite3.connect(folder / 'backup/posts.sqlite3')) as backup:
        db.backup(backup)
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    (folder / 'backup/posts.sqlite3').chmod(0o600)
    record['tokens_before'] = token_fingerprint()
    record['status'] = 'prepared'
    save(folder, record)
    print(json.dumps(dict(prepared=str(folder), tests='13 passed', commit=commit)), flush=True)


def atomic_file(target, source):
    stat = target.stat()
    temp = target.with_name(target.name + '.manual-time-next')
    temp.write_bytes(source.read_bytes())
    os.chmod(temp, stat.st_mode & 0o777)
    os.chown(temp, stat.st_uid, stat.st_gid)
    temp.replace(target)


def install(record, reverse):
    temp = POINTER.with_name('current.manual-time-next')
    temp.symlink_to(record['before' if reverse else 'after'])
    temp.replace(POINTER)
    for target, info in record['files'].items():
        atomic_file(Path(target), Path(info['backup' if reverse else 'source']))


def ready(baseline):
    for unit, port in (('drama-material-api.service', 8787), ('x-post-automation.service', 8810)):
        if baseline.get(unit) != 'active':
            continue
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=3) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)


def activate(folder, reverse=False):
    record = json.loads((folder / 'record.json').read_text())
    assert str(POINTER.resolve()) == record['after' if reverse else 'before'], 'concurrent_runtime_switch'
    for target, info in record['files'].items():
        assert sha(Path(target)) == info['after' if reverse else 'before'], 'concurrent_file_change:' + target
    if not reverse:
        for relative, digest in record['runtime_before'].items():
            assert sha(POINTER / relative) == digest, 'concurrent_runtime_file_change:' + relative
    prefix = 'rollback-' if reverse else ''
    current = states(OWNERS + TRIGGERS + DEPENDENTS)
    # A stopped deployment controller may leave the normal ExecStop barrier
    # running. Resume only after that barrier and any concurrent owner switch
    # have finished; never replace or terminate either service's drain helper.
    assert not any(current[u] in {'activating', 'deactivating'} for u in OWNERS), 'owner_switch_in_progress'
    resuming = not reverse and record['status'] == 'draining'
    if resuming:
        baseline = dict(record['units_before'])
        assert all(current[u] == 'active' for u in OWNERS if baseline.get(u) == 'deactivating'), 'concurrent_owner_not_ready'
        for unit, state in current.items():
            if state == 'active' or baseline.get(unit) == 'deactivating':
                baseline[unit] = state
    else:
        scheduler(folder, 'snapshot', prefix)
        baseline = current
    record[prefix + 'units_before'] = baseline
    record['status'] = prefix + 'draining'
    save(folder, record)
    triggers = [u for u in TRIGGERS if baseline[u] == 'active']
    if triggers:
        run('systemctl', 'stop', *triggers)
    print(json.dumps(dict(status=record['status'], folder=str(folder))), flush=True)
    switched = False
    install_started = False
    try:
        # Existing ExecStop drain remains authoritative, including segmented uploads.
        for unit in OWNERS:
            if baseline[unit] == 'active':
                run('systemctl', 'stop', unit)
        assert str(POINTER.resolve()) == record['after' if reverse else 'before'], 'concurrent_runtime_switch'
        for target, info in record['files'].items():
            assert sha(Path(target)) == info['after' if reverse else 'before'], 'concurrent_file_change:' + target
        install_started = True
        install(record, reverse)
        for unit in reversed(OWNERS):
            if baseline[unit] == 'active':
                run('systemctl', 'start', unit)
        ready(baseline)
        switched = True
    except BaseException:
        for unit in OWNERS:
            if baseline[unit] == 'active':
                run('systemctl', 'stop', unit)
        if install_started:
            install(record, not reverse)
        for unit in reversed(OWNERS):
            if baseline[unit] == 'active':
                run('systemctl', 'start', unit)
        ready(baseline)
        raise
    finally:
        for unit in DEPENDENTS + tuple(triggers):
            if baseline[unit] == 'active':
                run('systemctl', 'start', unit)
        scheduler(folder, 'restore', prefix)
        record[prefix + 'units_after'] = states(OWNERS + TRIGGERS + DEPENDENTS)
        record['status'] = ('rolled_back' if reverse else 'active') if switched else 'switch_failed'
        save(folder, record)
    for target, info in record['files'].items():
        assert sha(Path(target)) == info['before' if reverse else 'after']
    print(json.dumps(dict(status=record['status'], folder=str(folder), commit=record['commit'])), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('directory', nargs='?', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    else:
        assert args.directory is not None
        activate(args.directory, args.action == 'rollback')

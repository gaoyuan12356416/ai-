"""Install one X Tag catalog change, preserving publishing workers and data."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/opt/x-tag-publisher/current')
FILE = 'features/x_auto_posts/repositories.py'
BEFORE = '039fa9f4301e452465fc8cf1a3c029098ffde57092277e14d165a68fdc1b4808'
UNIT = 'x-tag-auto.service'
DB = Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
TRIGGERS = ['x-tag-auto-runner.timer', 'x-tag-auto-scheduler.timer', 'x-tag-auto-runner.path']
PUBLISHERS = ['x-tag-post.service', 'pipeline-cos-native-x-tag.service', 'pipeline-coordinator.service']


def run(*args, timeout=60):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=timeout).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(folder):
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to('/mnt/data-disk') and shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3
    assert not run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    assert digest(ROOT / FILE) == BEFORE, 'live_source_changed'
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    shutil.copy2(ROOT / FILE, folder / 'repositories.before.py')
    shutil.copy2(SOURCE / FILE, folder / 'repositories.next.py')
    candidate = folder / 'candidate'
    shutil.copytree(ROOT.resolve(), candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    shutil.copy2(SOURCE / FILE, candidate / FILE)
    for name, args in [('test_x_pending_launch.py', []), ('test_drama_launch_range.py', ['--platform', 'x'])]:
        shutil.copy2(SOURCE / 'scripts' / name, candidate / 'scripts' / name)
        result = subprocess.run([sys.executable, '-B', 'scripts/' + name, *args], cwd=candidate,
                                text=True, capture_output=True, timeout=90)
        (folder / (name + '.txt')).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
    with closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as src, \
         closing(sqlite3.connect(folder / 'auto-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'auto-before.sqlite3').chmod(0o600)
    meta = (ROOT / FILE).stat()
    save(folder / 'manifest.json', dict(commit=commit, status='prepared', root=str(ROOT.resolve()),
        before=BEFORE, after=digest(SOURCE / FILE), mode=meta.st_mode & 0o777,
        uid=meta.st_uid, gid=meta.st_gid))
    print(json.dumps(dict(status='prepared', commit=commit, tests='8 passed')))


def switch(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    assert str(ROOT.resolve()) == info['root'], 'preserve_later_runtime'
    expected = info['after'] if rollback else info['before']
    target = info['before'] if rollback else info['after']
    assert digest(ROOT / FILE) == expected, 'preserve_later_source'
    source = folder / ('repositories.before.py' if rollback else 'repositories.next.py')
    assert digest(source) == target
    action = 'rollback' if rollback else 'apply'
    run(sys.executable, HEALTH, 'snapshot', '--lane', 'x', '--output', folder / (action + '-schedulers-before.json'))
    info['triggers'] = {u: run('systemctl', 'show', u, '-p', 'ActiveState', '--value') for u in TRIGGERS}
    info['publisher_pids'] = {u: run('systemctl', 'show', u, '-p', 'MainPID', '--value') for u in PUBLISHERS}
    info['old_pid'] = run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value')
    path = ROOT / FILE
    temp = path.with_name(path.name + '.pending-launch-next')
    temp.write_bytes(source.read_bytes())
    os.chmod(temp, info['mode'])
    os.chown(temp, info['uid'], info['gid'])
    os.replace(temp, path)
    # Normal SIGTERM waits for accepted Auto requests. No publisher is stopped.
    run('systemctl', 'restart', '--no-block', UNIT)
    info.update(status='switch_requested', action=action)
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='switch_requested', action=action)))


def verify(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    action = info['action']
    assert run('systemctl', 'show', UNIT, '-p', 'ActiveState', '--value') == 'active'
    assert run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value') not in ('0', info['old_pid'])
    run('curl', '--max-time', '5', '-fsS', 'http://127.0.0.1:18843/health')
    run(sys.executable, HEALTH, 'restore', '--baseline', folder / (action + '-schedulers-before.json'),
        '--wait-seconds', '25', '--output', folder / (action + '-schedulers-after.json'))
    for unit, prior in info['triggers'].items():
        if prior == 'active':
            if run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') != 'active':
                run('systemctl', 'start', unit)
            assert run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') == 'active'
    assert digest(ROOT / FILE) == info['before' if action == 'rollback' else 'after']
    current = {u: run('systemctl', 'show', u, '-p', 'MainPID', '--value') for u in PUBLISHERS}
    assert current == info['publisher_pids'], 'publisher_changed_during_deployment'
    info['status'] = 'rolled_back' if action == 'rollback' else 'active'
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status=info['status'], commit=info['commit'], publisher_pids=current,
                          real_test_posts=0, historical_replays=0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback', 'verify'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare': prepare(folder)
    elif args.action == 'verify': verify(folder)
    else: switch(folder, args.action == 'rollback')

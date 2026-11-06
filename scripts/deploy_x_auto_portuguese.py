"""Overlay the catalog reader and restart only the idle X Auto owner.

Deploy from a clean, fetched GitHub checkout. Rollback restores code only;
publication ledgers, account snapshots and recovery events always stay current.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
CURRENT = Path('/opt/x-post-automation/current')
ROOT = Path('/mnt/data-disk/x-post-automation')
REL = 'features/x_auto_posts/repositories.py'
HEALTH_TOOL = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
AUTO_DB = Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3')
NATIVE_DB = Path('/var/lib/x-post-automation/accounts.sqlite3')
TRIGGERS = ('x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-runner.path')
OWNER = 'x-auto-post-service.service'
EXPECTED = '7c38d0feec5bbeb39e95b97b1496d3eca2f1ea487770e7bb7fe3428679320a19'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(folder, manifest):
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')


def backup_db(source, target):
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)


def atomic_copy(source, target):
    temp = target.with_name(target.name + '.portuguese-next')
    shutil.copy2(source, temp)
    os.chmod(temp, target.stat().st_mode)
    os.chown(temp, target.stat().st_uid, target.stat().st_gid)
    os.replace(temp, target)


def deploy(rollback=None):
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(ROOT).free > 2 * 1024**3
    if rollback:
        folder = Path(rollback).resolve()
        manifest = json.loads((folder / 'manifest.json').read_text())
        target = Path(manifest['target'])
        assert CURRENT.resolve() == Path(manifest['runtime']) and digest(target) == manifest['after_sha']
        candidate = folder / 'repositories.before.py'
    else:
        assert not run('git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=no')
        commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
        assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
        runtime = CURRENT.resolve()
        target = runtime / REL
        assert digest(target) == EXPECTED, 'live catalog reader changed; inspect before deploying'
        candidate = SOURCE / REL
        compile(candidate.read_bytes(), REL, 'exec')
        folder = ROOT / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-pt-alias-' + commit[:8])
        folder.mkdir(mode=0o700)
        shutil.copy2(target, folder / 'repositories.before.py')
        manifest = {'commit': commit, 'runtime': str(runtime), 'target': str(target),
                    'before_sha': digest(target), 'after_sha': digest(candidate), 'phase': 'prepared'}
        save(folder, manifest)
    suffix = 'rollback' if rollback else 'deploy'
    baseline = folder / (suffix + '-schedulers-before.json')
    run('python3', HEALTH_TOOL, 'snapshot', '--lane', 'x', '--output', str(baseline))
    states = {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') for unit in TRIGGERS}
    manifest[suffix + '_trigger_states'] = states
    save(folder, manifest)
    try:
        for unit, state in states.items():
            if state == 'active':
                run('systemctl', 'stop', unit)
        # Runner locks prevent new timer executions; active work must finish
        # naturally. No publisher, queue, token or unrelated lane is stopped.
        with contextlib.ExitStack() as stack:
            for path in ('/run/x-auto-post/runner.lock', '/run/x-auto-post/scheduler.lock'):
                handle = stack.enter_context(open(path, 'a'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.closing(sqlite3.connect(AUTO_DB.as_uri() + '?mode=ro', uri=True)) as c:
                active = c.execute("SELECT count(*) FROM x_auto_task WHERE claim_token<>'' OR status IN ('selecting','preparing','publishing','reconciling')").fetchone()[0]
                assert active == 0, 'X Auto has active tasks; retry deployment after they finish'
            if not rollback:
                backup_db(AUTO_DB, folder / 'auto.before.sqlite3')
                backup_db(NATIVE_DB, folder / 'native.before.sqlite3')
            assert digest(target) == (manifest['after_sha'] if rollback else manifest['before_sha'])
            run('systemctl', 'stop', OWNER)
            atomic_copy(candidate, target)
            run('systemctl', 'start', OWNER)
            for attempt in range(30):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18833/health', timeout=2) as r:
                        assert json.load(r)['ok'] is True
                    break
                except Exception:
                    if attempt == 29:
                        raise
                    time.sleep(1)
            assert digest(target) == digest(candidate)
            manifest['phase'] = 'rolled_back' if rollback else 'active'
            save(folder, manifest)
    finally:
        for unit, state in states.items():
            if state == 'active':
                subprocess.run(['systemctl', 'start', unit], check=True)
        result = run('python3', HEALTH_TOOL, 'restore', '--baseline', str(baseline), '--wait-seconds', '25',
                     '--output', str(folder / (suffix + '-schedulers-after.json')))
        manifest[suffix + '_scheduler_result'] = result
        save(folder, manifest)
    return {'folder': str(folder), 'phase': manifest['phase'], 'commit': manifest['commit'],
            'target': str(target), 'sha256': digest(target)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback')
    print(json.dumps(deploy(parser.parse_args().rollback)), flush=True)

#!/usr/bin/env python3
"""Overlay the metric-read fix on a hash-verified X runtime; retain ledgers."""

import argparse
import ast
import contextlib
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

DATA = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
CORE = 'features/x_auto_posts/core.py'
BASE_SHA = '0525bb6553cb0ab5365797a593781ca93d03c54c301ab5b6dd75b6974215c914'
FILES = [CORE, 'scripts/test_x_auto_metric_query_limits.py',
         'scripts/install_x_auto_metric_query_limits.py',
         'doc/x-publish-recovery-20261010/README.md']
AUTO_UNITS = ['x-auto-post-runner.timer', 'x-auto-post-scheduler.timer',
              'x-auto-post-metric.timer', 'x-auto-post-runner.path']
AUTO_WORKERS = ['x-auto-post-runner.service', 'x-auto-post-scheduler.service',
                'x-auto-post-metric.service']
POOL_UNITS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer']
SERVICE = 'x-auto-post-service.service'
DBS = {
    'auto': Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3'),
    'posts': Path('/var/lib/x-post-automation/accounts.sqlite3'),
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state(unit):
    return output('systemctl', 'show', unit, '-p', 'ActiveState', '--value')


def switch(path):
    temporary = CURRENT.with_name('current.metric-query-new')
    temporary.symlink_to(path)
    temporary.replace(CURRENT)


def ready():
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:18833/health', timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError('X Auto readiness failed')


def facts(path):
    result = {}
    with contextlib.closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            if table.startswith('x_auto_metric_'):
                continue  # Cache is backed up; publication facts are checked separately.
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid'):
                digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode())
                digest.update(b'\n')
                count += 1
            result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def token_facts():
    return {p.name: [sha(p), p.stat().st_mode & 0o777, p.stat().st_uid, p.stat().st_gid]
            for p in Path('/var/lib/x-post-automation/tokens').glob('*.json')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--expected-release')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    if output('findmnt', '-no', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk is not the expected mount')
    if shutil.disk_usage(DATA).free < 2 * 1024 ** 3:
        raise RuntimeError('insufficient backup space')
    if args.rollback:
        backup = Path(args.rollback)
        manifest = json.loads((backup / 'manifest.json').read_text())
        previous = Path(manifest['previous_release'])
        if CURRENT.resolve() != Path(manifest['release']):
            raise RuntimeError('rollback current-release mismatch')
        release = previous
    else:
        if not args.commit or len(args.commit) != 40 or any(c not in '0123456789abcdef' for c in args.commit):
            raise ValueError('a full GitHub commit is required')
        previous = Path(args.expected_release).resolve()
        if CURRENT.resolve() != previous or sha(previous / CORE) != BASE_SHA:
            raise RuntimeError('live baseline changed')
        repo = DATA / 'metric-query-deploy.git'
        if not repo.exists():
            run('git', 'init', '--bare', str(repo), stdout=subprocess.DEVNULL)
        env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes')
        run('git', '--git-dir=' + str(repo), 'fetch', '--depth=1',
            'git@github.com:gaoyuan12356416/ai-.git', args.commit, env=env)
        if output('git', '--git-dir=' + str(repo), 'rev-parse', 'FETCH_HEAD') != args.commit:
            raise RuntimeError('GitHub commit mismatch')
        release = DATA / 'releases' / (args.commit + '-metric-query')
        shutil.copytree(previous, release, symlinks=True)
        for name in FILES:
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(['git', '--git-dir=' + str(repo), 'show', args.commit + ':' + name]))
            os.chmod(path, 0o644)
            if name.endswith('.py'):
                ast.parse(path.read_text(), filename=name)
        run('python3', '-m', 'unittest', 'scripts.test_x_auto_metric_query_limits',
            'scripts.test_x_auto_post_store', 'scripts.test_x_auto_post_metrics',
            'scripts.test_x_auto_post_selector', '-q', cwd=release)
        backup = DATA / 'maintenance' / ('20261010-metric-query-' + args.commit[:8])
        backup.mkdir(mode=0o700)
        manifest = {'commit': args.commit, 'previous_release': str(previous),
                    'release': str(release), 'backup': str(backup)}
    before_units = {u: state(u) for u in AUTO_UNITS + POOL_UNITS}
    active = [u for u in AUTO_UNITS if before_units[u] == 'active']
    manifest['units_before_operation'] = before_units
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    stopped = switched = False
    try:
        if active:
            run('systemctl', 'stop', *active)
        deadline = time.monotonic() + 45
        while any(state(u) in {'active', 'activating', 'deactivating'} for u in AUTO_WORKERS):
            if time.monotonic() >= deadline:
                raise RuntimeError('workers did not drain; no process interrupted')
            time.sleep(1)
        with open('/run/x-post-daily/runner.lock', 'a+b') as lock, open('/run/x-auto-post/scheduler.lock', 'a+b') as scheduler:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(scheduler, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run('systemctl', 'stop', SERVICE)
            stopped = True
            expected_current = Path(manifest['release']) if args.rollback else previous
            if CURRENT.resolve() != expected_current:
                raise RuntimeError('runtime changed during drain')
            before = {name: facts(path) for name, path in DBS.items()}
            tokens = token_facts()
            if not args.rollback:
                manifest['publication_facts_before'] = before
                manifest['token_facts_before'] = tokens
                for name, path in DBS.items():
                    target = backup / (name + '-before.sqlite3')
                    with contextlib.closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as source, contextlib.closing(sqlite3.connect(str(target))) as dest:
                        source.backup(dest)
                        if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError('backup integrity failed')
                    os.chmod(target, 0o600)
                (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            switch(release)
            switched = True
            run('systemctl', 'start', SERVICE)
            ready()
            if {name: facts(path) for name, path in DBS.items()} != before or token_facts() != tokens:
                raise RuntimeError('publication or token state changed during deployment')
            if any(state(u) != before_units[u] for u in POOL_UNITS):
                raise RuntimeError('pool timer state changed during code deployment')
            manifest['status'] = 'rolled_back' if args.rollback else 'deployed'
            manifest['installed_core_sha256'] = sha(release / CORE)
    except BaseException:
        if switched:
            switch(expected_current)
        if stopped:
            run('systemctl', 'restart', SERVICE)
            ready()
        manifest['last_operation_failed'] = True
        raise
    finally:
        if active:
            run('systemctl', 'start', *active)
        manifest['units_after_operation'] = {u: state(u) for u in AUTO_UNITS + POOL_UNITS}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: manifest[k] for k in ['commit', 'previous_release', 'release', 'backup', 'status', 'installed_core_sha256']}))


if __name__ == '__main__':
    main()

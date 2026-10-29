#!/usr/bin/env python3
"""Scoped GitHub-first OAuth ownership release; never restore live credentials/data."""
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
ROOT = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')
TOKENS = Path('/var/lib/x-post-automation/tokens')
MODULE = 'features/x_accounts/oauth_service.py'
PAGE = 'static/x-accounts.html'
HTML_TARGETS = [Path('/root/drama_material_service/static/x-accounts.html'), Path('/usr/share/nginx/html/x-accounts.html')]
TRIGGERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer',
            'x-post-daily.timer', 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer',
            'x-auto-post-metric.timer', 'x-auto-post-runner.path', 'x-api-credit-monitor.timer']
WORKERS = [u.replace('.timer', '.service') for u in TRIGGERS if u.endswith('.timer')]
POOL = 'x-video-pool-scheduler.service'
SIDE = 'x-post-automation.service'
AUTO = 'x-auto-post-service.service'


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(map(str, args[:3])))
    return result.stdout.strip()


def active(unit):
    return run('systemctl', 'show', unit, '-p', 'ActiveState', '--value')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def atomic_copy(source, target):
    stat = target.stat()
    temp = target.with_name(target.name + '.reauth-next')
    shutil.copy2(source, temp)
    os.chown(temp, stat.st_uid, stat.st_gid)
    temp.chmod(stat.st_mode & 0o7777)
    temp.replace(target)


def switch(target):
    temp = CURRENT.with_name('reauth-next')
    temp.symlink_to(target)
    temp.replace(CURRENT)


def ready(port):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError('service not ready: %d' % port)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(ROOT).free > 2 * 1024**3
    original = json.loads((args.rollback / 'manifest.json').read_text()) if args.rollback else None
    baseline = CURRENT.resolve()
    if original:
        assert str(baseline) == original['new_release'], 'another release is current; reconcile before rollback'
        release = Path(original['previous_release'])
        assert release.is_dir()
        commit = original['commit']
    else:
        assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
        commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
        assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
        assert digest(CURRENT / MODULE) == '6ede434ef5168da21adffae56c11b49640a160c96086118d6981cd9b462f9e45', 'OAuth source drift'
        assert all(digest(p) == 'fe51bdd772dc00a803ebc411edc95c8f5566937587288316536205c6de3bf471' for p in [CURRENT / PAGE, *HTML_TARGETS]), 'HTML source drift'
        compile((SOURCE / MODULE).read_text(), MODULE, 'exec')
        release = ROOT / 'releases' / (commit + '-reauth-ownership')
        assert not release.exists(), 'existing release: reconcile its manifest before retry'
    if not args.apply:
        print(json.dumps({'status': 'preflight_ok', 'commit': commit, 'rollback': bool(original)}))
        return
    os.umask(0o077)
    backup = ROOT / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-reauth-ownership')
    backup.mkdir(mode=0o700)
    manifest = {'commit': commit, 'previous_release': str(baseline), 'new_release': str(release),
                'trigger_states': {u: active(u) for u in TRIGGERS}, 'pool_state': active(POOL),
                'auto_state': active(AUTO), 'status': 'preparing'}
    save(backup / 'manifest.json', manifest)
    if not original:
        shutil.copytree(baseline, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # Releases must remain traversable by the service user despite the
        # private backup umask. Preserve the baseline directory mode.
        release.chmod(baseline.stat().st_mode & 0o7777)
        for relative in (MODULE, PAGE):
            shutil.copy2(SOURCE / relative, release / relative)
    handles = []
    switched = stopped = False
    try:
        run('systemctl', 'stop', *[u for u, state in manifest['trigger_states'].items() if state == 'active'])
        if manifest['pool_state'] == 'active':
            run('systemctl', 'stop', POOL)
        deadline = time.monotonic() + 900
        print(json.dumps({'status': 'draining', 'backup': str(backup)}), flush=True)
        while any(active(u) in {'active', 'activating', 'deactivating'} for u in WORKERS):
            if time.monotonic() > deadline:
                raise RuntimeError('workers still running; no cutover performed')
            time.sleep(2)
        for path in ['/run/x-post-daily/runner.lock', '/run/x-auto-post/scheduler.lock']:
            handle = open(path, 'a+b')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        with contextlib.closing(sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'").fetchone()[0] == 0
        assert CURRENT.resolve() == baseline, 'concurrent deployment'
        run('systemctl', 'stop', SIDE)
        stopped = True
        shutil.copytree(TOKENS, backup / 'tokens')
        token_before = {p.name: (digest(p), p.stat().st_mode, p.stat().st_uid, p.stat().st_gid) for p in TOKENS.glob('*.json')}
        with contextlib.closing(sqlite3.connect(DB)) as conn, contextlib.closing(sqlite3.connect(backup / 'accounts.sqlite3')) as dst:
            conn.backup(dst)
            assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            manifest['ledger_counts'] = {t: conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                                        for t in ['x_post_queue', 'x_post_publish_log', 'x_post_repost_ledger']}
        db_before = digest(DB)
        shutil.copy2('/etc/x-post-automation.env', backup / 'production.env')
        for index, target in enumerate(HTML_TARGETS):
            shutil.copy2(target, backup / ('page-%d.html' % index))
            atomic_copy(args.rollback / ('page-%d.html' % index) if original else SOURCE / PAGE, target)
        switch(release)
        switched = True
        assert digest(DB) == db_before
        assert token_before == {p.name: (digest(p), p.stat().st_mode, p.stat().st_uid, p.stat().st_gid) for p in TOKENS.glob('*.json')}
        manifest['data_preserved_at_cutover'] = True
        run('systemctl', 'start', SIDE)
        ready(8810)
        if manifest['auto_state'] == 'active':
            run('systemctl', 'start', AUTO)
            ready(18833)
        manifest['status'] = 'ready'
        save(backup / 'manifest.json', manifest)
    finally:
        for handle in handles:
            handle.close()
        if manifest['status'] == 'ready' or not switched:
            if not switched:
                for index, target in enumerate(HTML_TARGETS):
                    saved = backup / ('page-%d.html' % index)
                    if saved.exists():
                        atomic_copy(saved, target)
            if stopped and not switched:
                run('systemctl', 'start', SIDE)
                ready(8810)
                if manifest['auto_state'] == 'active':
                    run('systemctl', 'start', AUTO)
                    ready(18833)
            if manifest['pool_state'] == 'active':
                run('systemctl', 'start', POOL)
            for unit, state in manifest['trigger_states'].items():
                if state == 'active':
                    run('systemctl', 'start', unit)
        save(backup / 'manifest.json', manifest)
    manifest['status'] = 'complete'
    manifest['trigger_states_after'] = {u: active(u) for u in TRIGGERS}
    assert manifest['trigger_states_after'] == manifest['trigger_states']
    save(backup / 'manifest.json', manifest)
    print(json.dumps({'status': 'complete', 'backup': str(backup), 'release': str(release), 'commit': commit}))


if __name__ == '__main__':
    main()

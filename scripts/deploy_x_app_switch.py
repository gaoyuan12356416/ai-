#!/usr/bin/env python3
"""Switch the production X application after draining publishers.

Run from a clean GitHub-fetched checkout. Private JSON supplies the credential
file, expected old/new Client ID hashes, label and verified Feishu destination.
No credentials or account identifiers are emitted or committed.
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
ROOT = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
MONITOR = Path('/mnt/data-disk/x-api-credit-monitor')
ENV = Path('/etc/x-post-automation.env')
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')
TOKENS = Path('/var/lib/x-post-automation/tokens')
FILES = ['features/x_accounts/oauth_service.py', 'features/x_posts/account_blockers.py',
         'static/x-accounts.html', 'static/x-account-list.html']
TRIGGERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer',
            'x-post-daily.timer', 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer',
            'x-auto-post-metric.timer', 'x-auto-post-runner.path', 'x-api-credit-monitor.timer']
WORKERS = [u.replace('.timer', '.service') for u in TRIGGERS if u.endswith('.timer')]
POOL = 'x-video-pool-scheduler.service'


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(map(str, args[:3])))
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + '\n')
    path.chmod(0o600)


def active(unit):
    return run('systemctl', 'show', unit, '-p', 'ActiveState', '--value')


def atomic_copy(source, target):
    previous = target.stat() if target.exists() else None
    temp = target.with_name(target.name + '.app-switch-next')
    shutil.copy2(source, temp)
    if previous:
        os.chown(temp, previous.st_uid, previous.st_gid)
        temp.chmod(previous.st_mode & 0o7777)
    temp.replace(target)


def switch(link, target):
    temp = link.with_name(link.name + '.app-switch-next')
    temp.symlink_to(target)
    temp.replace(link)


def environment():
    pid = int(run('systemctl', 'show', 'x-post-automation.service', '-p', 'MainPID', '--value'))
    return dict(value.decode().split('=', 1) for value in Path('/proc/%d/environ' % pid).read_bytes().split(b'\0') if b'=' in value)


def stage(config, commit):
    release = ROOT / 'releases' / (commit + '-app-switch')
    assert not release.exists(), 'release already exists; inspect existing receipt before retry'
    shutil.copytree(CURRENT.resolve(), release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for rel in FILES:
        shutil.copy2(SOURCE / rel, release / rel)
    monitor = MONITOR / 'releases' / (commit + '-app-switch')
    shutil.copytree((MONITOR / 'current').resolve(), monitor, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ['x_app_credit_monitor.py', 'x_live_credit_monitor.py']:
        shutil.copy2(SOURCE / 'scripts' / name, monitor / 'scripts' / name)
    for rel in [*FILES[:2], 'scripts/x_app_credit_monitor.py', 'scripts/x_live_credit_monitor.py']:
        compile((SOURCE / rel).read_text(), rel, 'exec')
    return release, monitor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(ROOT).free > 2 * 1024**3
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    env = environment()
    assert hashlib.sha256(env['X_CLIENT_ID'].encode()).hexdigest() == config['old_client_id_sha256']
    creds = json.loads(Path(config['credentials_path']).read_text())
    assert hashlib.sha256(creds['oauth2_client_id'].encode()).hexdigest() == config['new_client_id_sha256']
    assert config['old_client_id_sha256'] != config['new_client_id_sha256']
    for path, expected in config['baseline'].items():
        assert digest(path) == expected, 'live source changed: ' + path
    if not args.apply:
        print(json.dumps({'status': 'preflight_ok', 'commit': commit}))
        return
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'backups' / (stamp + '-app-switch')
    backup.mkdir(mode=0o700)
    manifest = {'status': 'preparing', 'backup': str(backup), 'commit': commit,
                'previous_release': str(CURRENT.resolve()),
                'previous_monitor': str((MONITOR / 'current').resolve()),
                'trigger_states': {u: active(u) for u in TRIGGERS}, 'pool_state': active(POOL)}
    save(backup / 'manifest.json', manifest)
    release, monitor = stage(config, commit)
    manifest.update(new_release=str(release), new_monitor=str(monitor))
    save(backup / 'manifest.json', manifest)
    handles = []
    switched = False
    try:
        run('systemctl', 'stop', *[u for u, state in manifest['trigger_states'].items() if state == 'active'])
        if manifest['pool_state'] == 'active':
            run('systemctl', 'stop', POOL)
        deadline = time.monotonic() + 900
        while any(active(u) in {'active', 'activating', 'deactivating'} for u in WORKERS):
            if time.monotonic() > deadline:
                raise RuntimeError('publisher still running; no cutover performed')
            time.sleep(2)
        for path in ['/run/x-post-daily/runner.lock', '/run/x-auto-post/scheduler.lock']:
            handle = open(path, 'a+b')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        # Reject active uploads; historical unknown records remain untouched.
        with contextlib.closing(sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'").fetchone()[0] == 0
        run('systemctl', 'stop', 'x-post-automation.service')
        for name, path in [('production.env', ENV), ('monitor-config.json', MONITOR / 'config.json'),
                           ('monitor-state.json', MONITOR / 'data/state.json')]:
            if path.exists():
                shutil.copy2(path, backup / name)
                (backup / name).chmod(0o600)
        shutil.copytree(TOKENS, backup / 'tokens')
        with contextlib.closing(sqlite3.connect(DB)) as conn, contextlib.closing(sqlite3.connect(backup / 'accounts.sqlite3')) as dst:
            conn.backup(dst)
            counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ['x_post_queue', 'x_post_publish_log', 'x_post_repost_ledger']}
            manifest['ledger_counts_before'] = counts
            for path in TOKENS.glob('*.json'):
                token = json.loads(path.read_text())
                assert token.get('oauth_client_id_sha256', config['old_client_id_sha256']) == config['old_client_id_sha256']
                token['oauth_client_id_sha256'] = config['old_client_id_sha256']
                temp = backup / 'token-next.json'
                save(temp, token)
                atomic_copy(temp, path)
                temp.unlink()
            manifest['accounts_pending'] = conn.execute("UPDATE x_authorized_account SET status='reauth_required' WHERE status='active'").rowcount
            conn.execute('DELETE FROM x_oauth_state')
            conn.commit()
        replacements = {'X_CLIENT_ID': creds['oauth2_client_id'], 'X_CLIENT_SECRET': creds['oauth2_client_secret'],
                        'X_BILLING_BEARER_TOKEN': creds['bearer_token'], 'X_OAUTH_APP_NAME': config['app_name'],
                        'X_REQUIRE_APP_BOUND_TOKENS': '1'}
        lines = [line for line in ENV.read_text().splitlines() if line.partition('=')[0] not in replacements]
        for key, value in replacements.items():
            assert '\n' not in value and '\r' not in value
            lines.append(key + '=' + json.dumps(value, ensure_ascii=False))
        next_env = backup / 'production-next.env'
        next_env.write_text('\n'.join(lines) + '\n'); next_env.chmod(0o600)
        atomic_copy(next_env, ENV)
        monitor_config = json.loads((MONITOR / 'config.json').read_text())
        monitor_config.update(expected_client_id_sha256=config['new_client_id_sha256'],
                              threshold_usd='10', app_name=config['app_name'])
        monitor_config['feishu'].update(config['feishu_recipient'])
        save(MONITOR / 'config.json', monitor_config)
        save(MONITOR / 'data/state.json', {'version': 1, 'scope': 'production', 'previous_state_backup': str(backup / 'monitor-state.json')})
        for rel in FILES:
            main = Path('/root/drama_material_service') / rel
            if main.exists() and rel != 'features/x_accounts/oauth_service.py':
                dst = backup / 'main' / rel; dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(main, dst); atomic_copy(SOURCE / rel, main)
            if rel.startswith('static/'):
                public = Path('/usr/share/nginx/html') / Path(rel).name
                dst = backup / 'public' / public.name; dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(public, dst); atomic_copy(SOURCE / rel, public)
        switch(CURRENT, release)
        switch(MONITOR / 'current', monitor)
        switched = True
        manifest['status'] = 'switched_verifying'
        save(backup / 'manifest.json', manifest)
        run('systemctl', 'start', 'x-post-automation.service', 'x-auto-post-service.service')
        run('systemctl', 'restart', 'drama-material-api.service')
        for _ in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8810/health', timeout=2) as response:
                    assert response.status == 200
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError('sidecar healthcheck failed; publishers held')
        assert hashlib.sha256(environment()['X_CLIENT_ID'].encode()).hexdigest() == config['new_client_id_sha256']
        with contextlib.closing(sqlite3.connect(DB)) as conn:
            assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert counts == {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in counts}
        manifest['status'] = 'complete'
    finally:
        for handle in reversed(handles):
            handle.close()
        if manifest['status'] == 'complete' or not switched:
            # Before mutation, an interrupted drain is safe to resume. A failure
            # after stopping OAuth requires inspection, so keep triggers held.
            if manifest['status'] == 'complete' or active('x-post-automation.service') == 'active':
                for unit, state in manifest['trigger_states'].items():
                    if state == 'active':
                        run('systemctl', 'start', unit)
                if manifest['pool_state'] == 'active':
                    run('systemctl', 'start', POOL)
                manifest['triggers_restored'] = True
        save(backup / 'manifest.json', manifest)
        print(json.dumps(manifest))


if __name__ == '__main__':
    main()

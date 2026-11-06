#!/usr/bin/env python3
"""Install or roll back only this independently deployed statistics service."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.request

BASE = Path('/mnt/data-disk/dramawave-w2a-stats')
CURRENT = Path('/opt/dramawave-w2a-stats/current')
CONFIG = Path('/etc/dramawave-w2a-stats.json')
TOKEN = Path('/etc/dramawave-w2a-stats.token')
MYSQL_ENV = Path('/etc/dramawave-w2a-stats.mysql.env')
NGINX = Path('/etc/nginx/default.d/dramawave-w2a-stats.conf')
UNITS = ('dramawave-w2a-stats.service', 'dramawave-w2a-stats-refresh.service',
         'dramawave-w2a-stats-refresh.timer')


def run(args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def service_state(unit):
    return {key: run(['systemctl', 'is-' + key, unit], False).returncode == 0
            for key in ('active', 'enabled')}


def refresh_running():
    state = run(['systemctl', 'show', UNITS[1], '--property=ActiveState', '--value'], False).stdout.strip()
    return state in ('active', 'activating', 'reloading', 'deactivating')


def safe_write(path, text, mode=0o600):
    temp = path.with_name(path.name + '.new')
    fd = os.open(str(temp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(temp, mode)
    os.replace(temp, path)


def check_disk():
    actual = run(['findmnt', '-no', 'UUID', '--target', '/mnt/data-disk']).stdout.strip()
    if actual != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Expected data disk is not mounted')


def main_pid():
    return run(['systemctl', 'show', 'drama-material-api.service',
                '--property=MainPID', '--value']).stdout.strip()


def set_current(target):
    temp = CURRENT.with_name('current.next')
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    os.replace(temp, CURRENT)


def backup(release):
    folder = BASE / 'backups' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True, mode=0o700)
    files = [CONFIG, NGINX] + [Path('/etc/systemd/system') / unit for unit in UNITS]
    manifest = {'release': str(release), 'previous_current': str(CURRENT.resolve()) if CURRENT.is_symlink() else None,
                'files': {}, 'units': {unit: service_state(unit) for unit in UNITS},
                'main_api_pid_before': main_pid()}
    for index, path in enumerate(files):
        name = '%d-%s' % (index, path.name)
        manifest['files'][str(path)] = name if path.exists() else None
        if path.exists():
            shutil.copy2(path, folder / name)
    safe_write(folder / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
    return folder, manifest


def provision_secrets():
    if not TOKEN.exists():
        safe_write(TOKEN, secrets.token_urlsafe(32) + '\n')
    if not MYSQL_ENV.exists():
        values = {}
        for line in Path('/root/drama_material_service/.env').read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        secret = {name: values.get('ADMIN_MAPPING_MYSQL_' + suffix, '')
                  for name, suffix in [('W2A_MYSQL_USER', 'USER'), ('W2A_MYSQL_PASSWORD', 'PASSWORD')]}
        if not all(secret.values()):
            raise RuntimeError('Existing source credentials are unavailable')
        safe_write(MYSQL_ENV, ''.join(key + '=' + json.dumps(value) + '\n'
                                     for key, value in secret.items()))


def install(release):
    release = release.resolve()
    releases = Path('/opt/dramawave-w2a-stats/releases').resolve()
    if release.parent != releases or not (release / 'w2a_stats.py').is_file():
        raise RuntimeError('Install requires an exact staged release')
    check_disk()
    state = BASE / 'state'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder, manifest = backup(release)
    # Print this before any activation so an operator always has the rollback path.
    print(json.dumps({'backup': str(folder)}), flush=True)
    try:
        provision_secrets()
        if not CONFIG.exists():
            safe_write(CONFIG, (release / 'config.example.json').read_text())
        for unit in UNITS:
            shutil.copyfile(release / 'deploy' / unit, Path('/etc/systemd/system') / unit)
        shutil.copyfile(release / 'deploy' / NGINX.name, NGINX)
        run(['nginx', '-t'])
        # Preserve an existing refresh: let its source read complete before switching.
        run(['systemctl', 'stop', UNITS[2]], False)
        deadline = time.monotonic() + 610
        while refresh_running():
            if time.monotonic() > deadline:
                raise RuntimeError('Existing scoped refresh is still active; no forced termination')
            time.sleep(1)
        set_current(release)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', UNITS[0], UNITS[2]])
        run(['systemctl', 'restart', UNITS[0]])
        for attempt in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8850/healthz', timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError('Dedicated API did not become healthy')
        run(['systemctl', 'start', UNITS[1]])
        run(['systemctl', 'start', UNITS[2]])
        run(['systemctl', 'reload', 'nginx'])
        result = {'backup': str(folder), 'release': str(release),
                  'main_api_pid_before': manifest['main_api_pid_before'],
                  'main_api_pid_after': main_pid(),
                  'units': {unit: service_state(unit) for unit in UNITS},
                  'rollback': '/usr/bin/python3 %s/deploy.py rollback --backup %s' % (release, folder)}
        safe_write(folder / 'installed.json', json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    except Exception:
        rollback(folder)
        raise


def rollback(folder):
    check_disk()
    folder = folder.resolve()
    if folder.parent != (BASE / 'backups').resolve():
        raise RuntimeError('Unexpected backup location')
    manifest = json.loads((folder / 'manifest.json').read_text())
    run(['systemctl', 'stop', UNITS[2]], False)
    # Allow an in-progress read to finish; never kill or overwrite its snapshot.
    deadline = time.monotonic() + 610
    while refresh_running():
        if time.monotonic() > deadline:
            raise RuntimeError('Refresh still active; retry rollback after it finishes')
        time.sleep(1)
    run(['systemctl', 'stop', UNITS[0]], False)
    for unit in (UNITS[0], UNITS[2]):
        run(['systemctl', 'disable', unit], False)
    for path_value, saved in manifest['files'].items():
        path = Path(path_value)
        if saved:
            shutil.copy2(folder / saved, path)
        elif path.exists():
            path.unlink()
    if manifest['previous_current']:
        set_current(manifest['previous_current'])
    elif CURRENT.is_symlink():
        CURRENT.unlink()
    run(['systemctl', 'daemon-reload'])
    for unit in (UNITS[0], UNITS[2]):
        if manifest['units'][unit]['enabled']:
            run(['systemctl', 'enable', unit])
        if manifest['units'][unit]['active']:
            run(['systemctl', 'start', unit])
    run(['nginx', '-t'])
    run(['systemctl', 'reload', 'nginx'])
    print(json.dumps({'rolled_back': str(folder), 'snapshots_and_credentials_preserved': True}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('install').add_argument('--release', type=Path, required=True)
    sub.add_parser('rollback').add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this deployment helper as root')
    if args.action == 'install':
        install(args.release)
    else:
        rollback(args.backup)

"""Exact two-file release; preserve databases and gracefully roll the auto worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
API = 'drama-material-api.service'
WORKER = 'youtube-auto-publish-worker.service'
EXPECTED = {
    'features/drama_synthesis/youtube.py': '49ea8347ccbd866e7670eb1b762c9fe6ecb5c08ca9052792a4f995c6f88032cf',
    'features/youtube_auto_publish/channels.py': '69a4484d0f9c278a87058014ac720303fe6773ad4843498f62869b38ce3b659e',
}


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(path, data, mode):
    temp = path.with_name(path.name + '.auth-retry-new')
    temp.write_bytes(data)
    os.chmod(temp, mode)
    os.replace(temp, path)


def healthy():
    for _ in range(25):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def roll_worker():
    old = run('systemctl', 'show', WORKER, '-p', 'MainPID', '--value')
    if old == '0' or run('systemctl', 'show', WORKER, '-p', 'Restart', '--value') != 'always':
        raise RuntimeError('Auto worker must be active with Restart=always')
    run('systemctl', 'kill', '--kill-who=main', '--signal=TERM', WORKER)
    # The main loop stops claiming, then lets existing preparation complete.
    # Never signal children or use systemctl stop's forced timeout.
    return old


def rollback(backup):
    backup = backup.resolve()
    if (BASE/'backups').resolve() not in backup.parents:
        raise RuntimeError('Backup outside intended directory')
    manifest = json.loads((backup/'manifest.json').read_text())
    if set(manifest['files']) != set(EXPECTED):
        raise RuntimeError('Unexpected rollback scope')
    for rel, entry in manifest['files'].items():
        if sha(ROOT/rel) != entry['new'] or sha(backup/rel) != entry['old']:
            raise RuntimeError('Rollback refused: later file drift or backup corruption')
    run('systemctl', 'stop', API)
    try:
        for rel, entry in manifest['files'].items():
            install(ROOT/rel, (backup/rel).read_bytes(), entry['mode'])
    finally:
        run('systemctl', 'start', API)
    healthy()
    print(json.dumps({'rollback': str(backup), 'worker_old_pid': roll_worker(), 'database': 'retained'}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    if args.rollback:
        rollback(args.rollback)
        return
    stage = Path(__file__).resolve().parents[1]
    commit = run('git', '-C', str(stage), 'rev-parse', 'HEAD')
    if run('git', '-C', str(stage), 'status', '--porcelain'):
        raise RuntimeError('Release checkout must be clean')
    plans = {}
    for rel, old in EXPECTED.items():
        if sha(ROOT/rel) != old:
            raise RuntimeError('Live file drift: ' + rel)
        data = (stage/rel).read_bytes()
        compile(data, rel, 'exec')
        plans[rel] = dict(old=old, new=sha(stage/rel), mode=(ROOT/rel).stat().st_mode & 0o777)
    if run('systemctl', 'is-active', API) != 'active' or run('systemctl', 'is-active', WORKER) != 'active':
        raise RuntimeError('API and auto worker must be active')
    if run('systemctl', 'show', WORKER, '-p', 'Restart', '--value') != 'always':
        raise RuntimeError('Auto worker rolling restart unsupported')
    if args.check:
        print(json.dumps({'preflight': 'passed', 'commit': commit, 'files': list(plans)}))
        return
    backup = BASE/'backups'/('channel-auth-retry-' + time.strftime('%Y%m%d-%H%M%S') + '-' + commit[:12])
    backup.mkdir(parents=True)
    for rel, entry in plans.items():
        saved = backup/rel
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/rel, saved)
        if sha(saved) != entry['old']:
            raise RuntimeError('Backup checksum mismatch')
    (backup/'manifest.json').write_text(json.dumps(dict(commit=commit, files=plans), indent=2))
    with sqlite3.connect('file:'+str(ROOT/'data/drama_material_jobs.sqlite3')+'?mode=ro', uri=True) as source, sqlite3.connect(str(backup/'before.sqlite3')) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise RuntimeError('Database backup verification failed')
    for rel, entry in plans.items():
        if sha(ROOT/rel) != entry['old']:
            raise RuntimeError('Pre-install file drift')
    run('systemctl', 'stop', API)
    try:
        for rel, entry in plans.items():
            install(ROOT/rel, (stage/rel).read_bytes(), entry['mode'])
            if sha(ROOT/rel) != entry['new']:
                raise RuntimeError('Installed checksum mismatch')
    except BaseException:
        for rel, entry in plans.items():
            install(ROOT/rel, (backup/rel).read_bytes(), entry['mode'])
        raise
    finally:
        run('systemctl', 'start', API)
    healthy()
    print(json.dumps({'deployed': commit, 'backup': str(backup), 'api': 'active',
        'worker_old_pid': roll_worker(), 'database': 'retained'}))


if __name__ == '__main__':
    main()

"""Run from the exact GitHub worktree on CPU; main publishing PIDs stay intact."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/mnt/data-disk/youtube-material-cards')
UNIT = Path('/etc/systemd/system/youtube-material-cards.service')
NGINX = Path('/etc/nginx/default.d/youtube-material-cards.conf')
ASSETS = ('youtube-publish.js', 'youtube-publish.html')
HASHES = ('66ca2bb497777af95c43b77c7d27ab45c5f334bd2c1a1d8d297dd26444f6ba7f',
          '2e3e359edf17aa666b65bc2af3969d26d4b76a5ced05d2dd2a6e95156cfe910a')


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def pids():
    return {name: run('systemctl', 'show', name, '-p', 'MainPID', '--value')
            for name in ('drama-material-api.service', 'youtube-auto-publish-worker.service')}


def main():
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    assert commit == sys.argv[1]
    assert not run('git', '-C', str(ROOT), 'status', '--porcelain')
    backup = BASE / 'backups' / commit[:12]
    assert not backup.exists() and not UNIT.exists() and not NGINX.exists()
    before = pids()
    assert all(value != '0' for value in before.values())
    targets = []
    for directory in ('/root/drama_material_service/static', '/usr/share/nginx/html'):
        for name, expected in zip(ASSETS, HASHES):
            path = Path(directory) / name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Concurrent asset change: ' + str(path)
            targets.append(path)
    backup.mkdir(parents=True)
    files = []
    for index, target in enumerate(targets):
        saved = backup / (str(index) + '-' + target.name)
        shutil.copy2(target, saved)
        files.append(dict(target=str(target), backup=str(saved)))
    manifest = dict(commit=commit, release=str(ROOT), before=before, files=files, status='backed_up')
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    current = Path('/opt/youtube-material-cards/current')
    current.parent.mkdir(parents=True, exist_ok=True)
    assert not current.exists()
    current.symlink_to(ROOT, target_is_directory=True)
    shutil.copy2(ROOT / 'ops/youtube-material-cards/youtube-material-cards.service', UNIT)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', 'youtube-material-cards.service')
    assert run('systemctl', 'is-active', 'youtube-material-cards.service') == 'active'
    # Wait for startup only, then verify unauthorized reads are rejected.
    import time
    for attempt in range(20):
        try:
            urlopen('http://127.0.0.1:8851/api/youtube-auto-publish/material-cards', timeout=6)
            raise AssertionError('Unauthenticated read accepted')
        except HTTPError as error:
            assert error.code == 401
            error.close()
            break
        except OSError:
            if attempt == 19:
                raise
            time.sleep(.25)
    shutil.copy2(ROOT / 'ops/youtube-material-cards/nginx.conf', NGINX)
    try:
        run('nginx', '-t')
    except Exception:
        NGINX.unlink()
        raise
    run('systemctl', 'reload', 'nginx')
    for target in targets:
        temp = target.with_suffix(target.suffix + '.material-cards-new')
        shutil.copy2(ROOT / 'static' / target.name, temp)
        temp.replace(target)
    run('systemctl', 'enable', 'youtube-material-cards.service')
    assert pids() == before, 'Publishing service PID changed'
    manifest.update(status='active', after=pids())
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()

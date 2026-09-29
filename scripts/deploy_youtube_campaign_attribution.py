"""Deploy only the read-only analytics matcher from a clean GitHub checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
REL = 'features/youtube_analytics/report.py'
EXPECTED = 'f23fbdbcfab566782a41f05e0bb802e8b35c027e345750a8f7324b2ef2bd56ae'
UNIT = 'drama-material-api.service'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(path, data, mode):
    temp = path.with_name(path.name + '.campaign-attribution-new')
    temp.write_bytes(data)
    os.chmod(temp, mode)
    os.replace(temp, path)


def healthy():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def rollback(backup):
    backup = backup.resolve()
    if (BASE/'backups').resolve() not in backup.parents:
        raise RuntimeError('Invalid backup path')
    manifest = json.loads((backup/'manifest.json').read_text())
    target = ROOT/REL
    if manifest['target'] != str(target) or sha(target) != manifest['new']:
        raise RuntimeError('Rollback refused: target or installed content drift')
    saved = backup/'report.py'
    if sha(saved) != manifest['old']:
        raise RuntimeError('Backup content drift')
    install(target, saved.read_bytes(), manifest['mode'])
    run('systemctl', 'restart', UNIT)
    healthy()
    print(json.dumps(dict(rollback='complete', backup=str(backup), database='retained')))


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
        raise RuntimeError('Release checkout is not clean')
    target = ROOT/REL
    if sha(target) != EXPECTED:
        raise RuntimeError('Live report file drift')
    data = (stage/REL).read_bytes()
    compile(data, REL, 'exec')
    if shutil.disk_usage(BASE).free < 64*1024*1024:
        raise RuntimeError('Insufficient disk space')
    if args.check:
        print(json.dumps(dict(preflight='passed', commit=commit, files=1)))
        return
    backup = BASE/'backups'/('campaign-attribution-'+time.strftime('%Y%m%d-%H%M%S')+'-'+commit[:12])
    backup.mkdir(parents=True)
    shutil.copy2(target, backup/'report.py')
    if sha(backup/'report.py') != EXPECTED:
        raise RuntimeError('Backup mismatch')
    manifest = dict(commit=commit, target=str(target), old=EXPECTED,
                    new=hashlib.sha256(data).hexdigest(), mode=target.stat().st_mode & 0o777)
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    if sha(target) != EXPECTED:
        raise RuntimeError('Pre-install file drift')
    try:
        install(target, data, manifest['mode'])
        if sha(target) != manifest['new']:
            raise RuntimeError('Installed checksum mismatch')
        run('systemctl', 'restart', UNIT)
        healthy()
    except BaseException as error:
        current = sha(target)
        if current == manifest['new']:
            rollback(backup)
        elif current != manifest['old']:
            raise RuntimeError('Automatic rollback refused: concurrent file drift') from error
        raise
    print(json.dumps(dict(deployed=commit, backup=str(backup), files=1,
                          api=run('systemctl','is-active',UNIT), database='unchanged')))


if __name__ == '__main__':
    main()

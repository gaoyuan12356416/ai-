"""Scoped GitHub-first catalog update; restart only the main query API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
BASE = '679695f'
FILES = ['features/x_video_publish/sources.py', 'static/x-video-publish.js',
         'static/x-video-publish.html']
API = 'drama-material-api.service'
PUBLISHERS = ['x-post-automation.service', 'x-auto-post-service.service']


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(source, target):
    target = Path(target)
    stat = target.stat()
    temporary = target.with_name(target.name + '.multi-id.tmp')
    shutil.copyfile(source, temporary)
    os.chmod(temporary, stat.st_mode)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, target)


def health():
    for attempt in range(20):
        try:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar', timeout=3) as response:
                    assert response.status == 200
            except urllib.error.HTTPError as error:
                assert error.code in (401, 403)
            assert run('systemctl', 'is-active', API) == 'active'
            return
        except (OSError, AssertionError, subprocess.CalledProcessError):
            time.sleep(1)
    raise RuntimeError('API health check failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    pids = {unit: run('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in PUBLISHERS}
    if args.rollback:
        manifest = json.loads(args.rollback.read_text())
        for entry in manifest['files']:
            assert digest(Path(entry['target']).read_bytes()) == entry['after'], 'Changed since deployment: ' + entry['target']
            assert digest(Path(entry['backup']).read_bytes()) == entry['before']
        for entry in manifest['files']:
            atomic(entry['backup'], entry['target'])
        run('systemctl', 'restart', API)
        health()
        manifest['status'] = 'rolled_back'
        args.rollback.write_text(json.dumps(manifest, indent=2) + '\n')
    else:
        commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
        assert not run('git', '-C', str(ROOT), 'status', '--porcelain')
        remote = run('git', '-C', str(ROOT), 'ls-remote', 'origin', 'refs/heads/codex/x-material-multi-id-search-20261104').split()
        assert remote[0] == commit, 'Source must match pushed GitHub branch'
        current = Path('/opt/x-post-automation/current').resolve()
        targets = []
        for name in FILES:
            baseline = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE + ':' + name])
            paths = [Path('/root/drama_material_service') / name, current / name]
            if name.startswith('static/'):
                paths.append(Path('/usr/share/nginx/html') / Path(name).name)
            for path in paths:
                assert path.read_bytes() == baseline, 'Live baseline drift: ' + str(path)
                targets.append((name, path))
        compile((ROOT / FILES[0]).read_bytes(), FILES[0], 'exec')
        run('node', '--check', str(ROOT / FILES[1]))
        backup = Path('/mnt/data-disk/x-post-automation/backups') / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-multi-id-' + commit[:8])
        backup.mkdir(mode=0o700)
        manifest = {'commit': commit, 'runtime': str(current), 'backup': str(backup), 'status': 'prepared', 'files': []}
        for index, (name, target) in enumerate(targets):
            saved = backup / str(index)
            shutil.copy2(target, saved)
            manifest['files'].append({'relative': name, 'target': str(target), 'backup': str(saved),
                                      'before': digest(saved.read_bytes()), 'after': digest((ROOT / name).read_bytes())})
        record = backup / 'manifest.json'
        record.write_text(json.dumps(manifest, indent=2) + '\n')
        try:
            assert Path('/opt/x-post-automation/current').resolve() == current
            for entry in manifest['files']:
                assert digest(Path(entry['target']).read_bytes()) == entry['before'], 'Concurrent file change'
            for entry in manifest['files']:
                atomic(ROOT / entry['relative'], entry['target'])
            run('systemctl', 'restart', API)
            health()
            for entry in manifest['files']:
                assert digest(Path(entry['target']).read_bytes()) == entry['after']
            manifest['status'] = 'deployed'
        except Exception:
            for entry in manifest['files']:
                if digest(Path(entry['target']).read_bytes()) == entry['after']:
                    atomic(entry['backup'], entry['target'])
            run('systemctl', 'restart', API)
            manifest['status'] = 'failed_restored'
            raise
        finally:
            record.write_text(json.dumps(manifest, indent=2) + '\n')
    assert pids == {unit: run('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in PUBLISHERS}, 'Publisher PID changed'
    print(json.dumps({'status': manifest['status'], 'commit': manifest['commit'], 'backup': manifest['backup'], 'publisher_pids_preserved': True}))


if __name__ == '__main__':
    main()

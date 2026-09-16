#!/usr/bin/env python3
"""Install the verified material-picker patch; restart only the API."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
SQL = Path('/etc/youtube-auto-publish/material-source.sql')
API = 'drama-material-api.service'
UNITS = [API, 'youtube-auto-publish-worker.service', 'drama-youtube-publish-worker.service', 'drama-youtube-unified-writer.service']
EXPECTED = {
    'features/youtube_auto_publish/source.py': '56579ce71c60cd1698c273b61950ef6d2a20a130660986e893c3652d94bbe5eb',
    'features/youtube_auto_publish/service.py': '25a78a447ad2b6ba4b5184fcdadc16c1c567c4d3cf9c8e8b1dad37f5384363e4',
    'static/youtube-publish.js': '2cf78171931dac6bedd59dafdcc95cfd0cafb95f42f3fe20d8b2b27a93572aff',
    'static/youtube-publish.css': 'adf1820ff8a44d1adb33e04115b88c65d566ca0e41da68225792f924fd8d0207',
    'static/youtube-publish.html': 'ed4740a4d0d74cb41b7ddb1435d8a9c7e55e917b9a94993e207cf87bde5443e9',
}
SQL_SHA = '2c37b57fc45e2f01baee1fe87bcdc87a932dacad62c9a3ad2cc7f347bb9ae356'
APP_SHA = '2a8c6034ce798aab6c77b5e4dfdb07f06ceac4891f412c660a05133f00a097f3'
OLD_CALL = 'result = service.list_materials(actor, search=query.get("search", [""])[0][:200], refresh=query.get("refresh", ["0"])[0] == "1")'
NEW_CALL = OLD_CALL[:-1] + ', uploader_id=query.get("uploader_id", [""])[0])'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(Path(path).read_bytes())


def snapshot():
    return {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID') for unit in UNITS}


def healthy():
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def install(data, target, metadata):
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.picker-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), metadata['uid'], metadata['gid'])
            os.chmod(temporary, metadata['mode'])
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inputs(stage):
    live = (ROOT / 'app.py').read_bytes()
    assert digest(live) == APP_SHA, 'Live app changed; review before deployment'
    assert live.count(OLD_CALL.encode()) == 1 and (stage / 'app.py').read_text().count(NEW_CALL) == 1
    app = live.replace(OLD_CALL.encode(), NEW_CALL.encode())
    ast.parse(app)
    rows = [(SQL, (stage / 'deploy/youtube-auto-material-source.sql').read_bytes(), SQL_SHA),
            (ROOT / 'app.py', app, APP_SHA)]
    for rel, baseline in EXPECTED.items():
        data = (stage / rel).read_bytes()
        if rel.endswith('.py'):
            ast.parse(data)
        rows.append((ROOT / rel, data, baseline))
        if rel.startswith('static/'):
            rows.append((PUBLIC / Path(rel).name, data, baseline))
    for target, _, expected in rows:
        assert not target.is_symlink() and sha(target) == expected, 'Live baseline changed: ' + str(target)
    return rows


def restore(backup, manifest, partial=False):
    expected_targets = {str(ROOT / rel) for rel in EXPECTED} | {str(PUBLIC / Path(rel).name) for rel in EXPECTED if rel.startswith('static/')} | {str(ROOT / 'app.py'), str(SQL)}
    records = manifest['files']
    assert len(records) == 10 and {row['target'] for row in records} == expected_targets
    for row in records:
        allowed = {row['installed_sha256'], row['sha256']} if partial else {row['installed_sha256']}
        assert sha(row['target']) in allowed, 'Newer live change exists: ' + row['target']
        assert sha(backup / 'files' / row['target'].lstrip('/')) == row['sha256']
    for row in records:
        install((backup / 'files' / row['target'].lstrip('/')).read_bytes(), row['target'], row)
    run('systemctl', 'restart', API)
    healthy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--commit')
    group.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stage = Path(__file__).resolve().parents[1]
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert BASE.is_dir() and shutil.disk_usage(BASE).free > 64 * 1024 * 1024
    import fcntl
    with (BASE / 'material-picker-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup = args.rollback.resolve(strict=True)
            assert (BASE / 'backups').resolve() in backup.parents
            manifest = json.loads((backup / 'manifest.json').read_text())
            assert manifest['kind'] == 'youtube-material-picker'
            assert (stage / '.github-verified-commit').read_text().strip() == manifest['commit']
            if args.check:
                for row in manifest['files']:
                    assert sha(row['target']) == row['installed_sha256']
                    assert sha(backup / 'files' / row['target'].lstrip('/')) == row['sha256']
                print(json.dumps({'rollback_check': 'passed', 'backup': str(backup)}))
                return
            restore(backup, manifest)
            print(json.dumps({'rollback': str(backup), 'services': snapshot(), 'database': 'untouched'}))
            return
        assert re.fullmatch('[a-f0-9]{40}', args.commit or '')
        assert (stage / '.github-verified-commit').read_text().strip() == args.commit
        rows = inputs(stage)
        before = snapshot()
        healthy()
        if args.check:
            print(json.dumps({'check': 'passed', 'files': len(rows), 'services': before}))
            return
        backup = BASE / 'backups' / ('material-picker-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(mode=0o700)
        records = []
        for target, data, expected in rows:
            saved = backup / 'files' / str(target).lstrip('/')
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            st = target.stat()
            records.append(dict(target=str(target), sha256=expected, installed_sha256=digest(data),
                                mode=st.st_mode & 0o7777, uid=st.st_uid, gid=st.st_gid))
        manifest = dict(kind='youtube-material-picker', commit=args.commit, files=records, services_before=before)
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        inputs(stage)  # Recheck all originals after taking the backup.
        try:
            for (target, data, _), record in zip(rows, records):
                install(data, target, record)
            for row in records:
                assert sha(row['target']) == row['installed_sha256']
            run('systemctl', 'restart', API)
            healthy()
            after = snapshot()
            assert all(before[u] == after[u] for u in UNITS if u != API), 'Unexpected worker state change'
        except BaseException:
            restore(backup, manifest, partial=True)
            raise
        result = dict(commit=args.commit, backup=str(backup), services=after,
                      files=len(rows), database='untouched', workers='unchanged',
                      sha256={row['target']: sha(row['target']) for row in records})
        (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))


if __name__ == '__main__':
    main()

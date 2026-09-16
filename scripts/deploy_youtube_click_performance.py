#!/usr/bin/env python3
"""Install eight verified performance files, preserving publication state."""
import argparse
import ast
import hashlib
import json
import re
import shutil
from pathlib import Path
import time

from deploy_youtube_material_picker import run, digest, sha, snapshot, healthy, install

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
NGINX = Path('/etc/nginx/default.d/youtube-auto-publish.conf')
API = 'drama-material-api.service'
APP_SHA = '46ffa7d71e113e2e98b189cf50a649b8d098be9cfb045818fc9ec08647b66a5e'
ROUTE_SHA = '6050b15d7be8a885e0dacbd291b99347eb4e582b660d82068c5bb88d56402fac'
NGINX_SHA = '151cc548c584471735b1e7ef76c39ea64dd6bee611181278b778c13186b01855'
EXPECTED = {
    'features/youtube_auto_publish/service.py':'7cdb085fd5873e912ffb15eedb1dbc7910f162df76fd2c404aceeae2ca4e9c8e',
    'features/youtube_auto_publish/covers.py':None,
    'static/youtube-publish.js':'8bb7f66c22e8b9b33ae3eb945880276db0ae607c397cdec490b5493dc136963d',
    'static/youtube-publish.html':'5e9bf88b0652f2faabab6d6c3f6fe79cfeed479ae527bc2542abbf03826bf79f',
}


def method(source):
    start = source.index('    def _dispatch_youtube_auto_publish(')
    end = source.index('\n    def ', start + 1)
    return start, end, source[start:end]


def current_sha(target):
    target = Path(target)
    assert not target.is_symlink(), 'Unexpected symlink: ' + str(target)
    return sha(target) if target.exists() else None


def inputs(stage):
    live = (ROOT / 'app.py').read_bytes()
    assert digest(live) == APP_SHA, 'API changed; review the new baseline first'
    source = live.decode()
    start, end, old = method(source)
    assert digest(old.encode()) == ROUTE_SHA
    replacement = method((stage / 'app.py').read_text())[2]
    app = (source[:start] + replacement + source[end:]).encode()
    ast.parse(app)
    rows = [(ROOT / 'app.py', app, APP_SHA), (NGINX, (stage / 'deploy/youtube-auto-publish-nginx.conf').read_bytes(), NGINX_SHA)]
    for rel, expected in EXPECTED.items():
        data = (stage / rel).read_bytes()
        if rel.endswith('.py'): ast.parse(data)
        rows.append((ROOT / rel, data, expected))
        if rel.startswith('static/'):
            rows.append((PUBLIC / Path(rel).name, data, expected))
    for target, data, expected in rows:
        assert current_sha(target) == expected, 'Live file changed: ' + str(target)
    return rows


def verify_backup(backup, manifest, partial=False):
    targets = {str(ROOT / rel) for rel in EXPECTED} | {str(PUBLIC / Path(rel).name) for rel in EXPECTED if rel.startswith('static/')} | {str(ROOT / 'app.py'), str(NGINX)}
    assert len(manifest['files']) == 8 and {row['target'] for row in manifest['files']} == targets
    for row in manifest['files']:
        allowed = {row['sha256'], row['installed_sha256']} if partial else {row['installed_sha256']}
        assert current_sha(row['target']) in allowed, 'Newer change exists: ' + row['target']
        if row['sha256'] is not None:
            assert sha(backup / 'files' / row['target'].lstrip('/')) == row['sha256']


def restore(backup, manifest, partial=False):
    verify_backup(backup, manifest, partial)
    for row in manifest['files']:
        target = Path(row['target'])
        if row['sha256'] is None:
            if target.exists(): target.unlink()
        else:
            install((backup / 'files' / row['target'].lstrip('/')).read_bytes(), target, row)
    run('nginx', '-t')
    run('systemctl', 'restart', API)
    healthy()
    run('systemctl', 'reload', 'nginx')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--commit')
    group.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stage = Path(__file__).resolve().parents[1]
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert BASE.is_dir() and shutil.disk_usage(BASE).free > 128*1024*1024
    import fcntl
    with (BASE / 'click-performance-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup = args.rollback.resolve(strict=True)
            assert (BASE / 'backups').resolve() in backup.parents
            manifest = json.loads((backup / 'manifest.json').read_text())
            assert manifest['kind'] == 'youtube-click-performance'
            assert (stage / '.github-verified-commit').read_text().strip() == manifest['commit']
            verify_backup(backup, manifest)
            if not args.check: restore(backup, manifest)
            print(json.dumps({'rollback_check' if args.check else 'rollback':'passed','backup':str(backup)}))
            return
        assert re.fullmatch('[a-f0-9]{40}', args.commit or '')
        assert (stage / '.github-verified-commit').read_text().strip() == args.commit
        rows = inputs(stage)
        before = snapshot()
        healthy()
        if args.check:
            print(json.dumps({'check':'passed','files':len(rows),'services':before})); return
        backup = BASE / 'backups' / ('click-performance-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(mode=0o700)
        records = []
        for target, data, expected in rows:
            if expected is not None:
                saved = backup / 'files' / str(target).lstrip('/')
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                st = target.stat()
                metadata = dict(mode=st.st_mode & 0o7777, uid=st.st_uid, gid=st.st_gid)
            else:
                metadata = dict(mode=0o644, uid=0, gid=0)
            records.append(dict(target=str(target), sha256=expected, installed_sha256=digest(data), **metadata))
        manifest = dict(kind='youtube-click-performance', commit=args.commit, files=records, services_before=before)
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        inputs(stage)
        try:
            # Helper module before the main entrypoint; each write is atomic.
            ordered = sorted(zip(rows,records), key=lambda pair: pair[0][0] == ROOT / 'app.py')
            for (target,data,_),record in ordered: install(data,target,record)
            for row in records: assert sha(row['target']) == row['installed_sha256']
            run('nginx', '-t')
            run('systemctl', 'restart', API)
            healthy()
            run('systemctl', 'reload', 'nginx')
            after = snapshot()
            assert all(before[u] == after[u] for u in before if u != API), 'Worker state changed'
        except BaseException:
            restore(backup,manifest,partial=True)
            raise
        result = dict(commit=args.commit,backup=str(backup),files=len(rows),services=after,database='untouched',workers='unchanged',nginx='reloaded')
        (backup / 'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))


if __name__ == '__main__': main()

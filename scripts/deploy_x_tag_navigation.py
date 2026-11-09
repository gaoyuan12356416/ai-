#!/usr/bin/env python3
"""GitHub-first static navigation update; no publisher or configuration writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
ROOTS = (Path('/usr/share/nginx/html'), Path('/root/drama_material_service/static'))
BACKUPS = Path('/mnt/data-disk/x-tag-publisher/navigation-backups')
BASE_SHA = '69f8f3bc001187b3c8a0118b59470576a9cbb866e85d3a62dba05c186f73205f'


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def parents():
    return command('systemctl', 'show', 'drama-material-api.service', 'x-post-automation.service',
                   'x-auto-post-service.service', 'x-tag-post.service', 'x-tag-auto.service',
                   'x-tag-gateway.service', '-p', 'Id,MainPID,ActiveState')


def atomic(path, data):
    mode = path.stat().st_mode & 0o777
    temp = path.with_name(path.name + '.tag-nav-next')
    temp.write_bytes(data)
    temp.chmod(mode)
    temp.replace(path)


def install():
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    commit = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if command('git', '-C', str(SOURCE), 'status', '--porcelain') or not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    script = (SOURCE / 'static/quick-nav.js').read_bytes()
    version = sha(script)[:12]
    targets = {}
    for root in ROOTS:
        path = root / 'quick-nav.js'
        if sha(path.read_bytes()) not in (BASE_SHA, sha(script)):
            raise RuntimeError('navigation_changed_since_review:' + str(path))
        targets[path] = script
        # Invalidate the shared navigation asset only; preserve every page's body.
        for path in root.glob('*.html'):
            original = path.read_text(encoding='utf-8')
            updated = re.sub(r'(/quick-nav\.js)(?:\?[^"\']*)?', r'\1?v=' + version, original)
            if updated != original:
                targets[path] = updated.encode('utf-8')
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True)
    info = dict(commit=commit, version=version, backup=str(backup), status='prepared', files={}, parents_before=parents())
    for index, (path, data) in enumerate(targets.items()):
        saved = backup / str(index)
        shutil.copy2(path, saved)
        info['files'][str(path)] = dict(before=sha(path.read_bytes()), after=sha(data), backup=str(saved))
    record = backup / 'deployment.json'
    record.write_text(json.dumps(info, indent=2))
    try:
        for path, data in targets.items():
            if sha(path.read_bytes()) != info['files'][str(path)]['before']:
                raise RuntimeError('concurrent_static_change:' + str(path))
            atomic(path, data)
        info['parents_after'] = parents()
        info['parents_unchanged'] = info['parents_before'] == info['parents_after']
        info['status'] = 'active'
        record.write_text(json.dumps(info, indent=2))
    except Exception:
        rollback(backup)
        raise
    print(json.dumps(dict(commit=commit, backup=str(backup), version=version,
                         files=len(targets), parents_unchanged=info['parents_unchanged'], status='active')))


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    for name, row in info['files'].items():
        path = Path(name)
        if path.parent not in ROOTS:
            raise RuntimeError('invalid_target')
        current = sha(path.read_bytes())
        if current == row['before']:
            continue
        if current != row['after']:
            raise RuntimeError('later_static_change:' + name)
        atomic(path, Path(row['backup']).read_bytes())
    print(json.dumps(dict(status='rolled_back_static_only', backup=str(backup))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)

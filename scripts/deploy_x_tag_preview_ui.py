#!/usr/bin/env python3
"""Deploy only tag-preview static assets and their cache references."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_hierarchy import source_commit, mount_check, record
from scripts.deploy_x_tag_navigation import ROOTS, parents, atomic, sha

BACKUPS = lane.ROOT / 'preview-ui-backups'
ASSETS = ('x-tag-publish-ui.js', 'x-tag-auto-publish-templates.js')


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


def install():
    mount_check()
    commit = source_commit()
    previous = lane.CURRENT.resolve(strict=True)
    release = lane.ROOT / 'releases' / commit
    if not release.exists():
        lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    targets = {}
    for root in ROOTS:
        for name in ASSETS:
            path = root / name
            data = (release / 'static' / name).read_bytes()
            if sha(path.read_bytes()) not in (sha((previous / 'static' / name).read_bytes()), sha(data)):
                raise RuntimeError('concurrent_preview_asset_change:' + str(path))
            targets[path] = data
        for path in root.glob('x-tag-*.html'):
            original = path.read_text()
            updated = original
            for name in ASSETS:
                version = sha((release / 'static' / name).read_bytes())[:12]
                updated = re.sub(r'(/' + re.escape(name) + r')(?:\?[^"\']*)?', r'\1?v=' + version, updated)
            if updated != original:
                targets[path] = updated.encode()
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    info = dict(commit=commit, backup=str(backup), status='prepared', files={}, parents_before=parents())
    for index, (path, data) in enumerate(targets.items()):
        saved = backup / str(index)
        shutil.copy2(path, saved)
        info['files'][str(path)] = dict(before=sha(path.read_bytes()), after=sha(data), backup=str(saved))
    record(backup / 'deployment.json', info)
    try:
        for path, data in targets.items():
            if sha(path.read_bytes()) != info['files'][str(path)]['before']:
                raise RuntimeError('concurrent_static_change:' + str(path))
            atomic(path, data)
        info.update(status='active', parents_after=parents())
        info['parents_unchanged'] = info['parents_before'] == info['parents_after']
        record(backup / 'deployment.json', info)
    except Exception:
        rollback(backup)
        raise
    print(json.dumps(dict(status='active', commit=commit, backup=str(backup), files=len(targets), parents_unchanged=info['parents_unchanged'])))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('install','rollback'))
    parser.add_argument('--backup')
    args=parser.parse_args()
    install() if args.action=='install' else rollback(args.backup)

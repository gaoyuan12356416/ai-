#!/usr/bin/env python3
"""Deploy the tag account-search editor only, without restarting publishers."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from scripts.deploy_x_tag_navigation import ROOTS, parents, atomic, sha

BACKUPS = lane.ROOT / 'account-search-backups'
ASSETS = ('x-tag-auto-publish-template.js', 'x-tag-auto-publish-template.html')
MANIFEST = 'tag-runtime-manifest.json'


def mount_check():
    if lane.cmd('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')


def source_commit():
    commit = lane.cmd('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if lane.cmd('git', '-C', str(SOURCE), 'status', '--porcelain') or not lane.cmd('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    return commit


def record(path, value):
    lane.atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve():
        raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    previous = Path(info['previous'])
    if previous.parent != (lane.ROOT / 'releases').resolve():
        raise RuntimeError('invalid_previous_release')
    roots = (*ROOTS, previous / 'static')
    for name, row in info['files'].items():
        path = Path(name)
        if path != previous / MANIFEST and (path.parent not in roots or path.name not in ASSETS):
            raise RuntimeError('invalid_target:' + name)
        saved = Path(row['backup'])
        if saved.parent != backup or sha(saved.read_bytes()) != row['before']:
            raise RuntimeError('backup_checksum_mismatch:' + name)
        current = sha(path.read_bytes())
        if current == row['before']:
            continue
        if current != row['after']:
            raise RuntimeError('later_static_change:' + name)
        atomic(path, saved.read_bytes())
        if sha(path.read_bytes()) != row['before']:
            raise RuntimeError('rollback_checksum_mismatch:' + name)
    info['status'] = 'rolled_back_static_only'
    record(backup / 'deployment.json', info)
    print(json.dumps(dict(status=info['status'], backup=str(backup))))


def install():
    mount_check()
    commit = source_commit()
    previous = lane.CURRENT.resolve(strict=True)
    release = lane.ROOT / 'releases' / commit
    if not release.exists():
        lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    manifest = json.loads((release / MANIFEST).read_text())
    data = {name: (release / 'static' / name).read_bytes() for name in ASSETS}
    baseline = {name: sha((previous / 'static' / name).read_bytes()) for name in ASSETS}
    targets, expected = {}, {}
    for name, content in data.items():
        if sha(content) != manifest['files']['static/' + name]:
            raise RuntimeError('staged_asset_checksum_mismatch:' + name)
    for root in (*ROOTS, previous / 'static'):
        for name in ASSETS:
            path = root / name
            current = sha(path.read_bytes())
            if current not in (baseline[name], sha(data[name])):
                raise RuntimeError('concurrent_account_search_asset_change:' + str(path))
            targets[path] = data[name]
            expected[path] = current
    manifest_path = previous / MANIFEST
    original_manifest = manifest_path.read_bytes()
    updated_manifest = json.loads(original_manifest)
    for name, content in data.items():
        updated_manifest['files']['static/' + name] = sha(content)
    targets[manifest_path] = json.dumps(updated_manifest, indent=2).encode()
    expected[manifest_path] = sha(original_manifest)
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True, mode=0o700)
    info = dict(commit=commit, previous=str(previous), backup=str(backup),
                status='prepared', files={}, parents_before=parents())
    for index, (path, content) in enumerate(targets.items()):
        saved = backup / str(index)
        shutil.copy2(path, saved)
        if sha(saved.read_bytes()) != expected[path]:
            raise RuntimeError('concurrent_static_change:' + str(path))
        info['files'][str(path)] = dict(before=expected[path],
                                      after=sha(content), backup=str(saved))
    record(backup / 'deployment.json', info)
    try:
        if lane.CURRENT.resolve(strict=True) != previous:
            raise RuntimeError('concurrent_runtime_release_change')
        for path, content in targets.items():
            if sha(path.read_bytes()) != info['files'][str(path)]['before']:
                raise RuntimeError('concurrent_static_change:' + str(path))
            atomic(path, content)
            if sha(path.read_bytes()) != info['files'][str(path)]['after']:
                raise RuntimeError('installed_asset_checksum_mismatch:' + str(path))
        info.update(status='active', parents_after=parents())
        info['parents_unchanged'] = info['parents_before'] == info['parents_after']
        record(backup / 'deployment.json', info)
        if not info['parents_unchanged']:
            raise RuntimeError('parent_service_state_changed')
    except Exception:
        rollback(backup)
        raise
    print(json.dumps(dict(status='active', commit=commit, backup=str(backup),
                         files=len(targets), parents_unchanged=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.action == 'rollback' and not args.backup:
        parser.error('--backup is required for rollback')
    install() if args.action == 'install' else rollback(args.backup)

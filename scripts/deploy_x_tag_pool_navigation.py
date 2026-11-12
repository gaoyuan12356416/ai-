#!/usr/bin/env python3
"""Static-only repair of the generated profile pool's navigation permission key."""
import argparse
import json
from pathlib import Path
import shutil

from deploy_x_profile_access_fix import SOURCE, CURRENT, MAIN, WEB, atomic, command, parents, record, sha

OLD = b'xPostMaterialPool'
NEW = b'xTagMaterialPool'
OLD_MESSAGE = '当前账号没有 Post 素材池权限'.encode()
NEW_MESSAGE = '当前账号没有标签画像素材池权限'.encode()


def transform(data):
    if data.count(OLD) != 2 or NEW in data or data.count(OLD_MESSAGE) != 1:
        raise RuntimeError('profile_pool_navigation_anchors_changed')
    return data.replace(OLD, NEW).replace(OLD_MESSAGE, NEW_MESSAGE)


def install(root):
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    if not str(root).startswith('/mnt/data-disk/') or root.exists():
        raise RuntimeError('new_data_disk_deployment_directory_required')
    commit = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if command('git', '-C', str(SOURCE), 'status', '--porcelain') or not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    runtime = CURRENT.resolve(strict=True)
    prepared = {}
    for directory in (WEB, MAIN / 'static', runtime / 'static'):
        path = directory / 'x-tag-material-pool.html'
        original = path.read_bytes()
        prepared[path] = (sha(original), transform(original))
    root.mkdir(parents=True)
    info = dict(commit=commit, runtime=str(runtime), status='prepared', files={}, parents_before=parents())
    for index, (path, (before, data)) in enumerate(prepared.items()):
        saved = root / str(index)
        shutil.copy2(path, saved)
        if sha(saved.read_bytes()) != before:
            raise RuntimeError('concurrent_file_change_before_backup:' + str(path))
        info['files'][str(path)] = dict(before=before, after=sha(data), backup=str(saved))
    manifest = root / 'deployment.json'
    record(manifest, info)
    try:
        if CURRENT.resolve() != runtime:
            raise RuntimeError('profile_runtime_changed')
        for path, (before, data) in prepared.items():
            if sha(path.read_bytes()) != before:
                raise RuntimeError('concurrent_file_change:' + str(path))
            atomic(path, data)
        info.update(status='active', parents_after=parents())
        info['publishing_parents_unchanged'] = info['parents_before'] == info['parents_after']
        record(manifest, info)
    except Exception:
        rollback(root)
        raise
    print(json.dumps(dict(commit=commit, status='active', backup=str(root), files=len(prepared),
                         publishing_parents_unchanged=info['publishing_parents_unchanged'])))


def rollback(root):
    info = json.loads((root / 'deployment.json').read_text())
    restore = []
    for name, item in info['files'].items():
        path = Path(name)
        current = sha(path.read_bytes())
        if current == item['before']:
            continue
        if current != item['after']:
            raise RuntimeError('later_file_change:' + name)
        restore.append((path, Path(item['backup']).read_bytes()))
    for path, data in restore:
        atomic(path, data)
    info['status'] = 'code_rolled_back'
    record(root / 'deployment.json', info)
    print(json.dumps(dict(status=info['status'], backup=str(root), files=len(restore))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    target = Path(args.root).resolve()
    install(target) if args.action == 'install' else rollback(target)

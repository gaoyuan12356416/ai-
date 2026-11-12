#!/usr/bin/env python3
"""Repair profile gateway access and stale navigation without restarting publishers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
CURRENT = Path('/opt/x-tag-publisher/current')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
BASE_GATEWAY = '8c0a12c591079559053613528ba54b948e55a51ff8f0a0aacae5a2485cf93826'
BASE_NAV = 'd64e9c9ccd3156d924c33dda211aac39cf322a2290bbddc0984b0915adfe59f9'
BASE_UI = {
    '7ef836e3025b7c8f472ddc1d744b560baee63f0b7e5362ba1f24834625b565ba',
    '947f6da432338a34d47455206faa944f548b3d0987eba39922615ff37f619096',
}
RETIRED_LINK = ",['/x-auto-publish-templates.html','原自动发布']".encode()
PARENTS = ('drama-material-api.service', 'x-post-automation.service',
           'x-auto-post-service.service', 'x-tag-post.service', 'x-tag-auto.service')


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    stat = path.stat()
    temp = path.with_name(path.name + '.profile-access-next')
    temp.write_bytes(data)
    temp.chmod(stat.st_mode & 0o777)
    os.chown(temp, stat.st_uid, stat.st_gid)
    temp.replace(path)


def record(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def parents():
    return command('systemctl', 'show', *PARENTS, '-p', 'Id,MainPID,ActiveState')


def health():
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8898/healthz', timeout=2) as response:
                if response.status == 200 and json.load(response).get('ok'):
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError('profile_gateway_health_failed')


def replace_asset_versions(data, versions):
    """Replace asset query strings only, retaining original HTML bytes/newlines."""
    for name, version in versions.items():
        pattern = rb'(/' + re.escape(name.encode()) + rb')(?:\?[^"\'<>\s]*)?'
        data = re.sub(pattern, lambda match: match.group(1) + b'?v=' + version.encode(), data)
    return data


def prepare_targets(runtime):
    targets = {}
    before = {}
    gateway = (SOURCE / 'features/x_tag_publish/gateway.py').read_bytes()
    compile(gateway, 'gateway.py', 'exec')
    if not (runtime / 'features/x_tag_publish/gateway.py').is_file():
        raise RuntimeError('profile_gateway_missing')
    for path in (runtime / 'features/x_tag_publish/gateway.py', MAIN / 'features/x_tag_publish/gateway.py'):
        if not path.exists():
            continue
        original = path.read_bytes()
        if sha(original) not in (BASE_GATEWAY, sha(gateway)):
            raise RuntimeError('gateway_changed_since_review:' + str(path))
        if original != gateway:
            targets[path] = gateway
            before[path] = sha(original)
    nav = (SOURCE / 'static/quick-nav.js').read_bytes()
    nav_version = sha(nav)[:12]
    for root in (WEB, MAIN / 'static', runtime / 'static'):
        path = root / 'quick-nav.js'
        original = path.read_bytes()
        if sha(original) not in (BASE_NAV, sha(nav)):
            raise RuntimeError('navigation_changed_since_review:' + str(path))
        if original != nav:
            targets[path] = nav
            before[path] = sha(original)
        path = root / 'x-tag-publish-ui.js'
        original = path.read_bytes()
        if sha(original) not in BASE_UI | {sha((SOURCE / 'static/x-tag-publish-ui.js').read_bytes())}:
            raise RuntimeError('profile_ui_changed_since_review:' + str(path))
        if original.count(RETIRED_LINK) > 1:
            raise RuntimeError('profile_ui_link_ambiguous:' + str(path))
        updated = original.replace(RETIRED_LINK, b'')
        if original != updated:
            targets[path] = updated
            before[path] = sha(original)
        versions = {'quick-nav.js': nav_version, 'x-tag-publish-ui.js': sha(updated)[:12]}
        for path in root.glob('*.html'):
            original = path.read_bytes()
            updated = replace_asset_versions(original, versions)
            if updated != original:
                targets[path] = updated
                before[path] = sha(original)
    return targets, before, nav_version


def install(root):
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    if not str(root).startswith('/mnt/data-disk/') or root.exists():
        raise RuntimeError('new_data_disk_deployment_directory_required')
    commit = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if command('git', '-C', str(SOURCE), 'status', '--porcelain') or not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('verified_github_source_required')
    runtime = CURRENT.resolve(strict=True)
    targets, before, version = prepare_targets(runtime)
    root.mkdir(parents=True)
    info = dict(commit=commit, runtime=str(runtime), navigation_version=version,
                created_at=datetime.now(timezone.utc).isoformat(), status='prepared',
                parents_before=parents(), files={})
    for index, (path, data) in enumerate(targets.items()):
        saved = root / str(index)
        shutil.copy2(path, saved)
        if sha(saved.read_bytes()) != before[path]:
            raise RuntimeError('concurrent_file_change_before_backup:' + str(path))
        info['files'][str(path)] = dict(before=sha(saved.read_bytes()), after=sha(data), backup=str(saved))
    manifest = root / 'deployment.json'
    record(manifest, info)
    try:
        if CURRENT.resolve() != runtime:
            raise RuntimeError('profile_runtime_changed')
        for path, data in targets.items():
            if sha(path.read_bytes()) != info['files'][str(path)]['before']:
                raise RuntimeError('concurrent_file_change:' + str(path))
            atomic(path, data)
        command('systemctl', 'restart', 'x-tag-gateway.service')
        health()
        info.update(status='active', parents_after=parents())
        info['publishing_parents_unchanged'] = info['parents_before'] == info['parents_after']
        record(manifest, info)
    except Exception:
        rollback(root)
        raise
    print(json.dumps(dict(commit=commit, status=info['status'], backup=str(root),
                         files=len(targets), navigation_version=version,
                         publishing_parents_unchanged=info['publishing_parents_unchanged'])))


def rollback(root):
    info = json.loads((root / 'deployment.json').read_text())
    if str(CURRENT.resolve()) != info['runtime']:
        raise RuntimeError('profile_runtime_changed')
    restore = []
    for name, row in info['files'].items():
        path = Path(name)
        current = sha(path.read_bytes())
        if current == row['before']:
            continue
        if current != row['after']:
            raise RuntimeError('later_file_change:' + name)
        restore.append((path, Path(row['backup']).read_bytes()))
    for path, data in restore:
        atomic(path, data)
    command('systemctl', 'restart', 'x-tag-gateway.service')
    health()
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

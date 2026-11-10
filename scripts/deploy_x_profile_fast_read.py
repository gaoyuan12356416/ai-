#!/usr/bin/env python3
"""Publish verified disk indexes and switch only the profile read service/UI."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_x_hierarchy import mount_check, source_commit, lock_file, validate_tree
from scripts.deploy_x_tag_revenue_read import atomic, save, file_sha, sha, parents, restart
from scripts.deploy_x_profile_detail_read import drained_restart
from features.x_tag_revenue.storage import ProfileWriter
from features.x_tag_revenue.serving import publish, Store
from features.x_tag_revenue.integration import patch_fast_load
from features.social_analytics.warehouse_refresh import Reader, environment, atomic_json

BASE = '2e18a62c7f9ee566a5db80f0a5accbc05a184464'
ROOT = Path('/mnt/data-disk/x-channel-tag-revenue')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
BACKUPS = ROOT / 'fast-read-backups'


def restore(info):
    for name, row in info['files'].items():
        if file_sha(Path(name)) not in (row['before'], row['after']):
            raise RuntimeError('later_file_change:' + name)
    for name, row in info['files'].items():
        if row['before'] is None:
            Path(name).unlink(missing_ok=True)
        else:
            atomic(Path(name), Path(row['backup']).read_bytes())
    drained_restart()


def install():
    mount_check(); commit = source_commit()
    targets = {}
    for relative in ['features/x_tag_revenue/' + n for n in ('storage.py', 'serving.py', 'routes.py', 'refresh.py', 'integration.py')] + ['static/x-tag-revenue.js']:
        content = (SOURCE / relative).read_bytes()
        paths = [MAIN / relative] + ([WEB / 'x-tag-revenue.js'] if relative.startswith('static/') else [])
        old = None if relative.endswith('/serving.py') else subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + relative], check=True, capture_output=True).stdout
        for target in paths:
            if file_sha(target) != (sha(old) if old is not None else None):
                raise RuntimeError('concurrent_profile_change:' + str(target))
            targets[target] = content
    for root in (MAIN / 'static', WEB):
        page = root / 'x-account-list.html'
        text = patch_fast_load(page.read_text())
        text, count = re.subn(r'(/x-tag-revenue\.js)(?:\?[^"\']*)?', r'\1?v=' + sha((SOURCE / 'static/x-tag-revenue.js').read_bytes())[:12], text)
        if count != 1:
            raise RuntimeError('profile_asset_reference_changed')
        targets[page] = text.encode()
    backup = BACKUPS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    info = dict(commit=commit, backup=str(backup), status='prepared', files={}, parents_before=parents())
    for i, (path, content) in enumerate(targets.items()):
        saved = backup / str(i)
        if path.exists(): shutil.copy2(path, saved)
        info['files'][str(path)] = dict(before=file_sha(path), after=sha(content), backup=str(saved))
    save(backup / 'deployment.json', info)
    with lock_file(ROOT / 'refresh.lock'):
        previous = json.loads((ROOT / 'current.json').read_text())
        data = json.loads((ROOT / previous['file']).read_text())
        validate_tree(data)
        info['source_version'] = data['meta']['version']
        info['source_pointer'] = previous
        shutil.copy2(ROOT / previous['file'], backup / 'source-profile.json')
        now = datetime.now(timezone.utc)
        data['meta'] = dict(data['meta'], version=now.strftime('%Y%m%dT%H%M%S%fZ'),
                            generated_at=now.isoformat(), projection_only=True)
        env = environment(MAIN / '.env')
        with closing(ProfileWriter(env)) as writer:
            info['database'] = writer.publish(data, Reader(env))
        info['serving'] = publish(ROOT, data, info['database'])
        # Keep the export/rollback artifact aligned with the new SQL generation.
        filename = 'snapshots/' + data['meta']['version'] + '.json'
        atomic_json(ROOT / filename, data)
        atomic_json(ROOT / 'current.json', dict(file=filename, version=data['meta']['version'], previous=previous['file']))
        atomic_json(ROOT / 'status.json', dict(status='ok', attempted_at=now.isoformat(), mode='verified_projection_refresh'))
        check, meta = Store(ROOT).load(view='summary')
        if meta['version'] != data['meta']['version'] or sum(p['revenue_cents'] for p in check['accounts'].values()) != info['database']['revenue']:
            raise RuntimeError('profile_serving_verification_failed')
        save(backup / 'deployment.json', info)
        try:
            for path, content in targets.items():
                if file_sha(path) != info['files'][str(path)]['before']:
                    raise RuntimeError('concurrent_file_change:' + str(path))
                atomic(path, content)
            drained_restart()
            info.update(status='active', activated_at=datetime.now(timezone.utc).isoformat(), parents_after=parents())
            info['publishing_pids_unchanged'] = info['parents_before'] == info['parents_after']
            save(backup / 'deployment.json', info)
        except Exception:
            restore(info)
            raise
    print(json.dumps({k: info[k] for k in ('status', 'commit', 'backup', 'publishing_pids_unchanged')}), flush=True)


def rollback(backup):
    mount_check()
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve(): raise RuntimeError('invalid_backup')
    info = json.loads((backup / 'deployment.json').read_text())
    with lock_file(ROOT / 'refresh.lock'): restore(info)
    save(backup / 'rollback.json', dict(status='code_only', profiles_and_publishing_records_preserved=True))
    print(json.dumps(dict(status='rolled_back_code_only', backup=str(backup))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('install', 'rollback')); parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action == 'install' else rollback(args.backup)

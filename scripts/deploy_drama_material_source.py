#!/usr/bin/env python3
"""GitHub-first, scoped composite deployment; rollback preserves upload receipts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.patch_drama_material_source import patch_app, patch_html

LIVE = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html/index.html')
DATA = Path('/mnt/data-disk/drama-material-source')
API = 'drama-material-api.service'
WORKER = 'drama-material-source.service'
BASE_APP = '7f863ca1094dac54352485484dc40feda280adf579a77dbcfd8bae0d810ec6c7'
BASE_HTML = '4ef6284b79abc495a27dcfd97f00f59c64f59ea96942efe958183cc69c85e121'
BASE_RUNTIME = 'b2723e89478e30159800799cc186b0fb406828bff18df6ac342a6787e88cbf99'
FILES = ['features/drama_synthesis/material_source.py', 'scripts/drama_material_source_worker.py']


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(Path(path).read_bytes()) if Path(path).exists() else None


def require(value, message):
    if not value:
        raise RuntimeError(message)


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.synthesis-upload.tmp')
    with temporary.open('wb') as out:
        out.write(data); out.flush(); os.fsync(out.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def idle_api():
    path = LIVE / 'data/drama_material_jobs.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        for table in ('drama_material_job', 'drama_screenshot_job'):
            count = db.execute("SELECT COUNT(*) FROM " + table + " WHERE status NOT IN ('done','failed')").fetchone()[0]
            require(count == 0, 'wait_for_active_jobs:' + table)


def plan():
    require(sha(LIVE / 'app.py') == BASE_APP, 'main_api_drift_requires_review')
    require(sha(LIVE / 'static/index.html') == BASE_HTML and sha(PUBLIC) == BASE_HTML, 'html_drift_requires_review')
    # Reused contract must match the locally tested implementation.
    for name in ('features/video_editing/material_source.py', 'features/video_editing/store.py'):
        require((LIVE / name).read_bytes() == (ROOT / name).read_bytes(), 'shared_contract_drift:' + name)
    source = (ROOT / 'static/drama-material-source.js').read_text()
    app = patch_app((LIVE / 'app.py').read_text()).encode()
    html = patch_html((LIVE / 'static/index.html').read_text(), source).encode()
    compile(app, str(LIVE / 'app.py'), 'exec')
    changes = [(LIVE / 'app.py', app), (LIVE / 'static/index.html', html), (PUBLIC, html)]
    for target in (LIVE / 'static/drama-job-runtime.js', PUBLIC.parent / 'drama-job-runtime.js'):
        require(sha(target) == BASE_RUNTIME, 'runtime_js_drift_requires_review')
        changes.append((target, (ROOT / 'static/drama-job-runtime.js').read_bytes()))
    for name in FILES:
        require(not (LIVE / name).exists(), 'new_target_already_exists:' + name)
        data = (ROOT / name).read_bytes()
        compile(data, name, 'exec')
        changes.append((LIVE / name, data))
    unit = Path('/etc/systemd/system') / WORKER
    require(not unit.exists(), 'worker_unit_already_exists')
    changes.append((unit, (ROOT / 'deploy' / WORKER).read_bytes()))
    return changes


def restore(manifest_path):
    path = Path(manifest_path)
    manifest = json.loads(path.read_text())
    rows = manifest['files']
    for row in rows:
        require(sha(row['path']) in {row['after'], row['before']}, 'rollback_target_changed:' + row['path'])
        if row['before']:
            require(sha(path.parent / row['backup']) == row['before'], 'backup_checksum_mismatch')
    idle_api()
    run('systemctl', 'disable', '--now', WORKER)
    for row in reversed(rows):
        target = Path(row['path'])
        if row['before']:
            atomic(target, (path.parent / row['backup']).read_bytes(), row['mode'])
        elif target.exists():
            target.unlink()
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', API)
    require(run('systemctl', 'is-active', API) == 'active', 'api_restart_failed')
    manifest['status'] = 'rolled_back_code_only'
    atomic(path, json.dumps(manifest, indent=2).encode(), 0o600)
    print(json.dumps({'rollback': str(path), 'api': 'active', 'upload_ledger': 'preserved'}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    require(run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'data_disk_not_mounted')
    if args.rollback:
        return restore(args.rollback)
    require(args.commit and run('git', '-C', str(ROOT), 'rev-parse', 'HEAD') == args.commit, 'exact_commit_required')
    require(not run('git', '-C', str(ROOT), 'status', '--porcelain'), 'dirty_release_checkout')
    ref = 'refs/heads/codex/drama-custom-material-upload-20261103'
    require(run('git', '-C', str(ROOT), 'ls-remote', 'origin', ref).split() == [args.commit, ref], 'commit_not_pushed')
    changes = plan()
    idle_api()
    if not args.apply:
        print(json.dumps({'status': 'ready', 'files': [str(p) for p, _ in changes]}))
        return
    backup = DATA / 'deployments' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + args.commit[:12])
    backup.mkdir(parents=True, mode=0o700)
    rows = []
    for index, (target, data) in enumerate(changes):
        before = sha(target)
        mode = target.stat().st_mode & 0o777 if before else 0o644
        saved = 'file-' + str(index)
        if before:
            shutil.copy2(target, backup / saved)
            require(sha(backup / saved) == before, 'backup_checksum_mismatch')
        rows.append({'path': str(target), 'before': before, 'after': digest(data), 'backup': saved, 'mode': mode})
    manifest = {'commit': args.commit, 'status': 'prepared', 'files': rows}
    manifest_path = backup / 'manifest.json'
    atomic(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
    for row in rows:
        require(sha(row['path']) == row['before'], 'file_changed_during_backup')
    for row, (_, data) in zip(rows, changes):
        atomic(row['path'], data, row['mode'])
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', WORKER)
    run('systemctl', 'restart', API)
    require(run('systemctl', 'is-active', API) == 'active', 'api_restart_failed')
    require(run('systemctl', 'is-active', WORKER) == 'active', 'upload_worker_start_failed')
    require(all(sha(row['path']) == row['after'] for row in rows), 'deployed_hash_mismatch')
    manifest['status'] = 'installed'
    atomic(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
    print(json.dumps({'status': 'installed', 'commit': args.commit, 'rollback_manifest': str(manifest_path)}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Enable an already verified cache on a live pull node, with natural draining.

Defaults to a read-only preflight. Does not change capacities, code releases,
jobs, leases, receipts, or platform state. Requires the audited pull worker's
SIGTERM drain implementation and an infinite systemd stop timeout.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_install import private_json, plan, absolute, command

# This release's parent and children treat SIGTERM as stop-after-current-job.
# Add other releases only after reviewing their complete shutdown path.
DRAIN_RELEASES = {"95fdaa4c4db0ed19a465cb126d539274e95ea918"}


def verify_drain_contract(info, props, argv):
    if info['sha'] not in DRAIN_RELEASES:
        raise ValueError('drain_release_not_reviewed')
    if props.get('KillMode') != 'mixed' or props.get('TimeoutStopUSec') != 'infinity':
        raise ValueError('unsafe_stop_contract')
    if props.get('ActiveState') != 'active' or int(props.get('MainPID', 0)) < 1:
        raise ValueError('node_not_active')
    if info['release'] + '/scripts/pipeline_worker.py' not in argv:
        raise ValueError('runtime_manifest_mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--cache-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    spec = private_json(args.manifest); info = plan(spec)
    if spec['backend'] != 'gpu': raise ValueError('gpu_node_required')
    cache = str(absolute(args.cache_root))
    props = dict(line.split('=', 1) for line in command(['systemctl', 'show', info['unit'],
        '-p', 'MainPID', '-p', 'KillMode', '-p', 'TimeoutStopUSec', '-p', 'ActiveState']).splitlines())
    pid = int(props['MainPID']); proc = Path('/proc') / str(pid)
    verify_drain_contract(info, props, proc.joinpath('cmdline').read_bytes().decode().strip('\0').split('\0'))
    live_env = dict(row.split('=', 1) for row in proc.joinpath('environ').read_bytes().decode().split('\0') if '=' in row)
    if live_env.get('PIPELINE_NODE_ID') != info['node_id'] or live_env.get('PIPELINE_RENDER_BACKEND') != 'gpu':
        raise ValueError('runtime_identity_mismatch')
    if live_env.get('RANDOM_GPU_ASSET_CACHE_ROOT'):
        raise ValueError('cache_already_configured')
    # Pass only non-secret catalog configuration to the service-user preflight.
    keys = ('X_POST_RANDOM_TEMPLATE_ASSET_ROOT', 'X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256',
        'FB_PAGE_GPU_RANDOM_OVERLAY_ROOT', 'FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256',
        'TT_POST_GPU_RANDOM_OVERLAY_ROOT', 'TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256',
        'RANDOM_OVERLAY_ASSET_CATALOGS')
    env = {k: live_env[k] for k in keys if k in live_env}
    env.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', RANDOM_GPU_ASSET_CACHE_ROOT=cache)
    code = ('import sys,json; sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + '); '
        'from features.random_gpu.asset_cache import validate_configured_cache; '
        'print(json.dumps(validate_configured_cache()))')
    checked = json.loads(command(['runuser', '-u', 'pipeline-worker', '--',
        info['venv'] + '/bin/python', '-c', code], env=env))
    dropin = Path('/etc/systemd/system') / (info['unit'] + '.d') / '30-rgba-asset-cache.conf'
    if dropin.exists() or dropin.is_symlink(): raise ValueError('dropin_already_exists')
    result = {'node_id': info['node_id'], 'runtime_sha': info['sha'], 'old_main_pid': pid,
        'cache': checked, 'apply': args.apply, 'dropin': str(dropin)}
    if not args.apply:
        print(json.dumps(result)); return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(info['state']) / 'maintenance' / ('rgba-cache-' + stamp)
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = Path(args.manifest)
    original = manifest.read_bytes()
    (backup / 'install-manifest.before.json').write_bytes(original)
    (backup / 'install-manifest.before.json').chmod(0o600)
    result.update(backup=str(backup), manifest_sha256_before=hashlib.sha256(original).hexdigest(),
        child_pids_before=proc.joinpath('task', str(pid), 'children').read_text().split())
    (backup / 'audit.json').write_text(json.dumps(result, indent=2))
    # Persist in both the running service's drop-in and future installer input.
    spec.setdefault('environment', {})['RANDOM_GPU_ASSET_CACHE_ROOT'] = cache
    temporary = manifest.with_name(manifest.name + '.rgba-new')
    with temporary.open('x') as handle:
        os.chmod(temporary, 0o600)
        handle.write(json.dumps(spec, indent=2) + '\n'); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, manifest)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    with dropin.open('x') as handle:
        handle.write('[Service]\nEnvironment=RANDOM_GPU_ASSET_CACHE_ROOT=' + cache + '\n')
        handle.flush(); os.fsync(handle.fileno())
    dropin.chmod(0o644)
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'restart', '--no-block', info['unit']])
    result['status'] = 'draining_current_jobs_then_restart'
    (backup / 'audit.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__': main()

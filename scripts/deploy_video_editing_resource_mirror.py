#!/usr/bin/env python3
"""Scoped resource/mirror rollout. Check is read-only; restore reopens admission.

Run from the clean, pushed feature checkout on either host. GPU also requires
--gpu-release /data/drama-synthesis-gpu/releases/<pushed GPU SHA>. Sequence:
CPU prepare, GPU prepare/apply, CPU apply, GPU restore, CPU restore. Abort is
safe only before that host's code install begins. After install, complete the
compatible rollout; never restore a database or remove v2/profile readers.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FEATURE_REF = 'refs/heads/codex/video-editing-resource-mirror-20261029'
GPU_REF = 'refs/heads/codex/video-editing-resource-mirror-gpu-20261029'
CPU_BASELINE = 'd043f804d201e8568b6a499bedfac4bb1d6c3765'
GPU_BASELINE = '05f48e2175c691534a88549a694cfe9d4cc6739d'
CPU_ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
CPU_DATA = Path('/mnt/data-disk/video-editing')
CPU_DB = CPU_DATA / 'state.sqlite3'
GPU_BASE = Path('/data/drama-synthesis-gpu')
API = 'drama-material-api.service'
CPU_WORKERS = ('video-editing-worker.service', 'video-editing-material-source.service')
OBSERVED = ('drama-material-job-worker.service', 'drama-youtube-unified-writer.service',
            'youtube-auto-publish-worker.service')
MARKER = Path('/etc/video-editing/intake.disabled')
MATERIAL_MARKER = Path('/etc/video-editing/material-source.disabled')
MARKER_TEXT = b'resource mirror rollout 20261029\n'
CPU_FILES = tuple('features/video_editing/' + name + '.py' for name in
                  ('catalog', 'uploads', 'service', 'store', 'routes', 'material_source', 'recipe', 'projects_v2'))
STATIC_FILES = ('video-editing.html', 'video-editing.js', 'video-editing.css')
GPU_FILES = tuple('features/video_editing/' + name + '.py' for name in ('recipe', 'render', 'gpu_integration'))
CPU_OVERLAY_HASHES = {
    'features/video_editing/service.py': '249aed74c84336766f561eb8e608b8ca9d8f4678eaf337fa13090637f118388a',
    'features/video_editing/recipe.py': '5da685a3d320b17351f6ca58d612cca27524166bdc76d472c7d510e2a0a9137a',
}
LEDGERS = ('ve_task', 've_output', 've_material_upload')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args, timeout=60, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, **kwargs)
    require(result.returncode == 0, 'command_failed:' + str(args[0]))
    return result.stdout.strip()


def prop(unit, key):
    return run('systemctl', 'show', unit, '-p', key, '--value')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    path = Path(path)
    require(not path.is_symlink(), 'file_symlink:' + str(path))
    return digest(path.read_bytes()) if path.exists() else None


def atomic(path, data, mode=0o644):
    path = Path(path)
    require(not path.is_symlink(), 'file_symlink:' + str(path))
    temporary = path.with_name(path.name + '.resource-mirror-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True, indent=2).encode(), 0o600)


def load(path):
    return json.loads(Path(path).read_text())


def published(root, ref):
    rev = run('git', '-C', str(root), 'rev-parse', 'HEAD')
    require(re.fullmatch('[0-9a-f]{40}', rev), 'invalid_commit')
    require(not run('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'), 'dirty_checkout')
    origin = run('git', '-C', str(root), 'remote', 'get-url', 'origin')
    require(origin in {'git@github.com:gaoyuan12356416/ai-.git', 'https://github.com/gaoyuan12356416/ai-.git'},
            'unexpected_repository')
    require(run('git', '-C', str(root), 'ls-remote', 'origin', ref).split() == [rev, ref], 'unpublished_commit')
    return rev


def cpu_storage():
    require(run('findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk') ==
            '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'data_disk_mismatch')
    require(shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3, 'insufficient_data_disk_space')


def baseline_hash(name):
    if name in CPU_OVERLAY_HASHES: return CPU_OVERLAY_HASHES[name]
    if name == 'features/video_editing/projects_v2.py': return None
    result = subprocess.run(('git', '-C', str(ROOT), 'show', CPU_BASELINE + ':' + name),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    require(result.returncode == 0, 'baseline_source_missing:' + name)
    return digest(result.stdout)


def cpu_plan(*, allow_installed=False):
    rows = []
    targets = [(CPU_ROOT / name, name) for name in CPU_FILES]
    targets += [(directory / name, 'static/' + name) for name in STATIC_FILES
                for directory in (CPU_ROOT / 'static', PUBLIC)]
    for target, name in targets:
        source = ROOT / name
        before, after, actual = baseline_hash(name), file_sha(source), file_sha(target)
        require(after is not None, 'candidate_file_missing:' + name)
        require(actual == before or allow_installed and actual == after, 'live_file_drift:' + str(target))
        if name.endswith('.py'): compile(source.read_text(encoding='utf-8'), str(source), 'exec')
        rows.append({'path': str(target), 'source': name, 'before': before, 'after': after,
                     'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644})
    return rows


def cpu_counts(path=None):
    with closing(sqlite3.connect('file:' + str(path or CPU_DB) + '?mode=ro', uri=True, timeout=5)) as db:
        return {'editing': db.execute("SELECT count(*) FROM ve_task WHERE status IN "
                    "('queued','resume_queued','submitting','queued_gpu','running')").fetchone()[0],
                'material_writes': db.execute("SELECT count(*) FROM ve_material_upload WHERE status IN "
                    "('queued','copying','submitting')").fetchone()[0]}


def cpu_idle(timeout):
    deadline = time.monotonic() + timeout
    while True:
        counts = cpu_counts()
        if not any(counts.values()): return counts
        require(time.monotonic() < deadline, 'editing_work_not_drained_no_interruption')
        time.sleep(1)


def evidence(path=None):
    result = {}
    with closing(sqlite3.connect('file:' + str(path or CPU_DB) + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('BEGIN')
        for table in LEDGERS:
            checksum = hashlib.sha256(); count = 0
            for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id'):
                checksum.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()); count += 1
            result[table] = {'rows': count, 'sha256': checksum.hexdigest()}
    return result


def backup_database(destination):
    with closing(sqlite3.connect('file:' + str(CPU_DB) + '?mode=ro', uri=True, timeout=5)) as source:
        with closing(sqlite3.connect(str(destination))) as target: source.backup(target)
    os.chmod(destination, 0o600)


def migrate(path):
    code = ('import sys;sys.path.insert(0,sys.argv[1]);'
            'from features.video_editing.store import Store;Store(sys.argv[2])')
    run(sys.executable, '-c', code, str(ROOT), str(path), timeout=60,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))


def marker_state(paths):
    return {str(path): {'exists': path.exists(), 'sha256': file_sha(path)} for path in paths}


def close_markers(control):
    for name, prior in control['markers'].items():
        path = Path(name)
        if prior['exists']:
            require(file_sha(path) == prior['sha256'], 'preexisting_marker_changed')
        elif path.exists():
            require(path.read_bytes() == MARKER_TEXT, 'foreign_intake_marker')
        else:
            require(path.parent.is_dir() and not path.parent.is_symlink(), 'marker_directory_unavailable')
            atomic(path, MARKER_TEXT)


def verify_markers(control):
    for name, prior in control['markers'].items():
        expected = prior['sha256'] if prior['exists'] else digest(MARKER_TEXT)
        require(file_sha(name) == expected, 'rollout_marker_changed')


def restore_markers(control):
    # Missing markers created by this rollout are already in their intended
    # restored state (e.g. the acknowledgement was lost). Foreign files remain protected.
    for name, prior in control['markers'].items():
        actual = file_sha(name)
        require(actual == prior['sha256'] if prior['exists'] else actual in {None, digest(MARKER_TEXT)},
                'rollout_marker_changed')
    for name, prior in control['markers'].items():
        if not prior['exists']: Path(name).unlink(missing_ok=True)


def observed_warnings(control):
    return ['unrelated_pid_changed:' + unit for unit, pid in control.get('observed', {}).items()
            if prop(unit, 'MainPID') != pid]


def api_ready(timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen('http://127.0.0.1:8787/api/video-editing/config', timeout=2)
        except urllib.error.HTTPError as exc:
            require(exc.code == 401, 'unexpected_api_auth_response'); return
        except (urllib.error.URLError, OSError):
            require(time.monotonic() < deadline, 'api_not_ready'); time.sleep(.5)
        else:
            raise RuntimeError('api_missing_authentication_gate')


def resume_units(control):
    for unit, state in control['units'].items():
        if state == 'active': run('systemctl', 'start', unit)
        require(prop(unit, 'ActiveState') == state, 'service_state_not_restored:' + unit)


def cpu(args, rev):
    cpu_storage()
    backup = CPU_DATA / 'backups' / ('resource-mirror-' + rev[:12])
    if args.phase == 'check':
        rows = cpu_plan(allow_installed=True)
        return {'ok': True, 'host': 'cpu', 'phase': 'check', 'release': rev, 'backup': str(backup),
                'files': len(rows), 'jobs': cpu_counts(), 'markers': marker_state((MARKER, MATERIAL_MARKER)),
                'prepared': (backup / 'prepare.json').is_file(), 'applied': (backup / 'apply.json').is_file()}
    if args.phase == 'prepare':
        require(not (backup / 'install.started').exists(), 'install_already_started_use_apply_or_restore')
        cpu_plan()
        if not backup.exists():
            backup.mkdir(parents=True, mode=0o700)
            save(backup / 'control.json', {'release': rev, 'markers': marker_state((MARKER, MATERIAL_MARKER)),
                'units': {unit: prop(unit, 'ActiveState') for unit in CPU_WORKERS}, 'api_state': prop(API, 'ActiveState'),
                'observed': {unit: prop(unit, 'MainPID') for unit in OBSERVED}})
        control = load(backup / 'control.json'); require(control['release'] == rev, 'release_control_mismatch')
        close_markers(control); cpu_idle(args.drain_timeout)
        for unit in CPU_WORKERS:
            if prop(unit, 'ActiveState') == 'active': run('systemctl', 'stop', unit)
            require(prop(unit, 'MainPID') == '0', 'editing_worker_not_stopped')
        cpu_idle(0)
        if not (backup / 'before.json').exists(): save(backup / 'before.json', evidence())
        require(evidence() == load(backup / 'before.json'), 'editing_ledger_changed_during_prepare')
    else:
        require((backup / 'prepare.json').is_file() or args.phase == 'abort' and (backup / 'control.json').is_file(),
                'prepare_proof_missing')
        control = load(backup / 'control.json'); require(control['release'] == rev, 'release_control_mismatch')
        if args.phase in {'restore', 'abort'} and (backup / (args.phase + '.json')).exists():
            return load(backup / (args.phase + '.json'))
        if args.phase == 'apply': verify_markers(control)
        if args.phase == 'apply':
            require(all(prop(unit, 'MainPID') == '0' for unit in CPU_WORKERS), 'editing_workers_not_stopped')
            cpu_idle(0)
            before = load(backup / 'before.json')
            require(evidence() == before, 'business_ledger_changed_before_install')
            if (backup / 'apply.json').exists():
                require(all(file_sha(row['path']) == row['after'] for row in load(backup / 'files.json')), 'installed_file_mismatch')
                if control['api_state'] == 'active': api_ready()
                return load(backup / 'apply.json')
            if not (backup / 'files.json').exists():
                rows = cpu_plan()
                for index, row in enumerate(rows):
                    row['backup'] = str(index) + '.file'
                    if row['before'] is not None: atomic(backup / row['backup'], Path(row['path']).read_bytes(), 0o600)
                save(backup / 'files.json', rows)
            if not (backup / 'migration.checked.json').exists():
                backup_database(backup / 'evidence.sqlite3')
                shutil.copy2(backup / 'evidence.sqlite3', backup / 'migration-rehearsal.sqlite3')
                migrate(backup / 'migration-rehearsal.sqlite3'); migrate(backup / 'migration-rehearsal.sqlite3')
                require(evidence(backup / 'migration-rehearsal.sqlite3') == before, 'migration_changed_business_records')
                save(backup / 'migration.checked.json', {'business_records_unchanged': True})
            rows = load(backup / 'files.json')
            for row in rows:
                require(file_sha(ROOT / row['source']) == row['after'] and file_sha(row['path']) in {row['before'], row['after']},
                        'install_source_or_target_changed')
            save(backup / 'install.started', {'release': rev})
            if prop(API, 'ActiveState') == 'active': run('systemctl', 'stop', API)
            require(prop(API, 'MainPID') == '0', 'api_not_stopped')
            for row in rows: atomic(row['path'], (ROOT / row['source']).read_bytes(), row['mode'])
            migrate(CPU_DB)
            require(evidence() == before, 'migration_changed_business_records')
            if control['api_state'] == 'active': run('systemctl', 'start', API); api_ready()
            require(all(file_sha(row['path']) == row['after'] for row in rows), 'installed_file_mismatch')
        elif args.phase == 'restore':
            require((backup / 'apply.json').is_file(), 'apply_proof_missing')
            require(all(file_sha(row['path']) == row['after'] for row in load(backup / 'files.json')), 'installed_file_mismatch')
            if control['api_state'] == 'active': api_ready()
            resume_units(control); restore_markers(control)
        elif args.phase == 'abort':
            require(not (backup / 'install.started').exists(), 'install_started_retain_v2_readers_and_complete_rollout')
            resume_units(control); restore_markers(control)
    result = {'ok': True, 'host': 'cpu', 'phase': args.phase, 'release': rev, 'backup': str(backup),
              'warnings': observed_warnings(control)}
    save(backup / (args.phase + '.json'), result)
    return result


def gpu_guard(candidate):
    spec = importlib.util.spec_from_file_location('resource_mirror_gpu_guard', candidate / 'scripts/deploy_video_editing_gpu.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def preserved_gpu_ledger(before, after):
    return all(after.get('hashes', {}).get(key) == value for key, value in before.get('hashes', {}).items())


def ensure_gpu_tunnel(guard):
    # Starting an inactive tunnel is safe even while the healthy worker serves
    # other jobs; a lost acknowledgement must never require restarting it.
    if guard.property_value(guard.TUNNEL, 'ActiveState') != 'active':
        guard.run('systemctl', 'start', guard.TUNNEL)
    require(guard.property_value(guard.TUNNEL, 'ActiveState') == 'active', 'gpu_tunnel_unavailable')


def gpu(args, rev):
    candidate = Path(args.gpu_release)
    require(candidate.is_dir() and not candidate.is_symlink() and candidate.resolve() == candidate and
            candidate.parent == GPU_BASE / 'releases' and re.fullmatch('[0-9a-f]{40}', candidate.name), 'invalid_gpu_release_path')
    gpu_rev = published(candidate, GPU_REF)
    require(candidate.name == gpu_rev, 'gpu_release_path_commit_mismatch')
    run('git', '-C', str(candidate), 'merge-base', '--is-ancestor', GPU_BASELINE, gpu_rev)
    changed = set(run('git', '-C', str(candidate), 'diff', '--name-only', GPU_BASELINE, gpu_rev).splitlines())
    allowed = set(GPU_FILES) | {'scripts/deploy_video_editing_resource_mirror.py'}
    require(set(GPU_FILES) <= changed and all(name in allowed or name.startswith('doc/20261029.video-editing-resource-mirror/')
                                            for name in changed), 'gpu_composite_unrelated_changes')
    require(all(file_sha(candidate / name) == file_sha(ROOT / name) for name in GPU_FILES), 'gpu_feature_source_mismatch')
    backup = GPU_BASE / 'deploy-backups' / ('resource-mirror-' + gpu_rev[:12])
    guard = gpu_guard(candidate)
    current = GPU_BASE / 'current'
    require(current.is_symlink(), 'gpu_current_link_missing')
    live = current.resolve()
    require(live in {GPU_BASE / 'releases' / GPU_BASELINE, candidate}, 'gpu_live_release_drift')
    try:
        health = guard.health(); guard.check_health(health, live.name)
    except Exception:
        # A previous apply may have stopped the worker before its SSH caller
        # disconnected. Only that durable, preflighted install can resume it.
        require(args.phase == 'apply' and (backup / 'install.started').is_file() and
                guard.property_value(guard.WORKER, 'MainPID') == '0', 'gpu_health_unavailable')
        health = None
    if args.phase == 'check':
        env = guard.effective_env(); snapshot = guard.ledger_snapshot(env['DRAMA_WORK_ROOT'])
        return {'ok': True, 'host': 'gpu', 'phase': 'check', 'feature_commit': rev, 'gpu_commit': gpu_rev,
                'backup': str(backup), 'busy': snapshot['busy'], 'marker_exists': MARKER.exists(),
                'prepared': (backup / 'prepare.json').is_file(), 'applied': (backup / 'apply.json').is_file()}
    if args.phase == 'prepare':
        require(live.name == GPU_BASELINE and not (backup / 'install.started').exists(), 'gpu_install_already_started')
        if not backup.exists():
            markers = marker_state((MARKER,))
            guard.backup_configuration(backup)
            save(backup / 'control.json', {'release': gpu_rev, 'feature_commit': rev, 'markers': markers,
                                         'old_link': str(live), 'health': health})
        control = load(backup / 'control.json')
        close_markers(control)
        env = guard.effective_env()
        guard.wait_for_idle(env['DRAMA_WORK_ROOT'], args.drain_timeout)
        guard.wait_for_media_idle(env, max(3, args.drain_timeout))
    else:
        require((backup / 'prepare.json').exists() or args.phase == 'abort' and (backup / 'control.json').exists(),
                'gpu_prepare_proof_missing')
        control = load(backup / 'control.json')
        require(control['release'] == gpu_rev and control['feature_commit'] == rev, 'gpu_control_commit_mismatch')
        if args.phase in {'restore', 'abort'} and (backup / (args.phase + '.json')).exists():
            return load(backup / (args.phase + '.json'))
        if args.phase == 'apply': verify_markers(control)
        if args.phase == 'apply':
            if (backup / 'apply.json').exists():
                require(health and live == candidate and all(health.get(key) == control['health'].get(key) for key in guard.SAFE_HEALTH),
                        'applied_gpu_release_or_health_changed')
                ensure_gpu_tunnel(guard)
                return load(backup / 'apply.json')
            started = (backup / 'install.started').exists()
            if not started:
                import pwd
                env = guard.effective_env(); work_root = env['DRAMA_WORK_ROOT']
                before = guard.wait_for_idle(work_root, args.drain_timeout)
                guard.wait_for_media_idle(env, max(3, args.drain_timeout))
                identity = pwd.getpwnam('drama-synthesis-gpu')
                candidate_env = dict(env, DRAMA_GPU_RELEASE_SHA=gpu_rev, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
                preflight = guard.run(str(GPU_BASE / 'runtime/current/bin/python'),
                    str(candidate / 'scripts/check_drama_synthesis_gpu_runtime.py'), '--check-app-import',
                    env=candidate_env, cwd=candidate, user=identity.pw_uid, group=identity.pw_gid, timeout=300)
                require(json.loads(preflight).get('ok') is True, 'gpu_preflight_failed')
                save(backup / 'before.json', {'ledger': before, 'health': control['health'], 'release': live.name,
                                            'work_root': work_root, 'preflight_ok': True})
            else:
                previous = load(backup / 'before.json')
                require(previous.get('preflight_ok') is True, 'gpu_preflight_proof_missing')
                before, work_root = previous['ledger'], previous['work_root']
            # Reconcile an installed/running candidate before considering any
            # restart; lost acknowledgements must not interrupt new legacy jobs.
            if health and live == candidate:
                after = health
            else:
                if health:
                    env = guard.effective_env()
                    guard.wait_for_idle(work_root, args.drain_timeout)
                    guard.wait_for_media_idle(env, max(3, args.drain_timeout))
                    guard.wait_for_idle(work_root, 0)
                    guard.wait_for_media_idle(env, 0, quiet_seconds=0)
                    save(backup / 'install.started', {'release': gpu_rev})
                    guard.run('systemctl', 'stop', '--no-block', guard.WORKER); guard.wait_worker_stopped()
                snapshot = guard.ledger_snapshot(work_root)
                require(not snapshot['busy'] and preserved_gpu_ledger(before, snapshot), 'gpu_ledger_changed_before_switch')
                guard.atomic(guard.ENV_FILE, guard.merge_environment(guard.ENV_FILE.read_bytes(), {'DRAMA_GPU_RELEASE_SHA': gpu_rev}))
                if current.resolve() != candidate: guard.switch_link(candidate)
                guard.run('systemctl', 'start', guard.WORKER, timeout=360)
                guard.run('systemctl', 'start', guard.TUNNEL)
                after = guard.wait_for_health(gpu_rev)
            require(all(after.get(key) == control['health'].get(key) for key in guard.SAFE_HEALTH), 'unrelated_gpu_health_changed')
            require(after.get('video_editing_profile') == control['health'].get('video_editing_profile') and
                    after.get('video_editing_probe_concurrency') == control['health'].get('video_editing_probe_concurrency') and
                    after.get('video_editing_enabled') is False, 'gpu_editor_contract_or_admission_changed')
            require(preserved_gpu_ledger(before, guard.ledger_snapshot(work_root)), 'gpu_old_ledger_changed')
            ensure_gpu_tunnel(guard)
        elif args.phase == 'restore':
            require((backup / 'apply.json').exists() and live == candidate, 'gpu_apply_proof_missing')
            require(all(health.get(key) == control['health'].get(key) for key in guard.SAFE_HEALTH), 'unrelated_gpu_health_changed')
            restore_markers(control)
            require(guard.health().get('video_editing_enabled') == control['health'].get('video_editing_enabled'), 'gpu_admission_not_restored')
        elif args.phase == 'abort':
            require(not (backup / 'install.started').exists(), 'gpu_install_started_retain_v2_readers_and_complete_rollout')
            restore_markers(control)
    result = {'ok': True, 'host': 'gpu', 'phase': args.phase, 'feature_commit': rev,
              'gpu_commit': gpu_rev, 'backup': str(backup)}
    save(backup / (args.phase + '.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('cpu', 'gpu'), required=True)
    parser.add_argument('--phase', choices=('check', 'prepare', 'apply', 'restore', 'abort'), default='check')
    parser.add_argument('--gpu-release')
    parser.add_argument('--drain-timeout', type=int, default=45)
    args = parser.parse_args()
    require(0 <= args.drain_timeout <= 1800, 'invalid_drain_timeout')
    rev = published(ROOT, FEATURE_REF)
    if args.host == 'gpu': require(args.gpu_release, 'gpu_release_required')
    print(json.dumps(cpu(args, rev) if args.host == 'cpu' else gpu(args, rev), sort_keys=True))


if __name__ == '__main__':
    main()

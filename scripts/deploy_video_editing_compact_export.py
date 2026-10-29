"""Scoped compact-export rollout. Default check is read-only; ledgers are never restored."""
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
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GPU_BASELINE = 'a0c5cd914472e7c80422ee0ef8d1a356cb7ea64c'
FEATURE_REF = 'refs/heads/codex/video-editing-compact-export-20261029'
GPU_REF = 'refs/heads/codex/video-editing-compact-gpu-20261029'
GPU_FILES = ('features/video_editing/recipe.py', 'features/video_editing/render.py',
             'features/video_editing/gpu_integration.py')
CPU_HASHES = {
    'features/video_editing/recipe.py': 'ab29d67e3ef58b1ec4c8dc8442958140f7aa39c310bb2bfdd53ab446a216c2ec',
    'features/video_editing/service.py': 'bcc7d83149f1723b7a3dcd1294c76a552a11f4de80c8b6209bf4447670531f85',
}
CPU_ROOT = Path('/root/drama_material_service')
CPU_DATA = Path('/mnt/data-disk/video-editing')
CPU_DB = CPU_DATA / 'state.sqlite3'
MARKER = Path('/etc/video-editing/intake.disabled')
MARKER_TEXT = b'compact export rollout 20261029\n'
CLAIMS = ('drama-material-job-worker.service', 'video-editing-worker.service')
PRESERVED = ('drama-youtube-unified-writer.service', 'youtube-auto-publish-worker.service',
             'video-editing-material-source.service')
API = 'drama-material-api.service'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args, timeout=60, **kwargs):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=timeout, **kwargs)
    if p.returncode:
        raise RuntimeError('command failed: ' + args[0])
    return p.stdout.strip()


def prop(unit, key):
    return run('systemctl', 'show', unit, '-p', key, '--value')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path, data, mode=0o644):
    path = Path(path)
    require(not path.is_symlink(), 'linked target: ' + str(path))
    temp = path.with_name(path.name + '.compact-new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def save(path, value):
    atomic(path, json.dumps(value, indent=2, sort_keys=True).encode(), 0o600)


def published(root, ref):
    rev = run('git', '-C', str(root), 'rev-parse', 'HEAD')
    require(re.fullmatch('[0-9a-f]{40}', rev), 'invalid commit')
    require(not run('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'), 'dirty staged checkout')
    remote = run('git', '-C', str(root), 'ls-remote', 'origin', ref)
    require(remote.split()[:1] == [rev], 'checkout differs from GitHub branch')
    return rev


def cpu_storage():
    require(run('findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk') ==
            '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'data disk mismatch')
    require(shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3, 'insufficient data disk space')


def cpu_counts():
    counts = {}
    for path, sql, name in [
        (CPU_ROOT / 'data/drama_material_jobs.sqlite3',
         "select count(*) from drama_material_job where status not in ('done','failed')", 'drama'),
        (CPU_DB, "select count(*) from ve_task where status not in ('completed','failed','cancelled')", 'editing')]:
        with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=5)) as db:
            counts[name] = db.execute(sql).fetchone()[0]
    return counts


def cpu_idle(timeout):
    end = time.monotonic() + timeout
    while True:
        counts = cpu_counts()
        if not any(counts.values()): return counts
        require(time.monotonic() < end, 'CPU jobs have not drained; nothing was interrupted')
        time.sleep(2)


def cpu_evidence():
    out = {}
    with closing(sqlite3.connect('file:' + str(CPU_DB) + '?mode=ro', uri=True, timeout=5)) as db:
        for table in ('ve_task', 've_output', 've_material_upload'):
            digest = hashlib.sha256(); count = 0
            for row in db.execute('select * from ' + table + ' order by id'):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode())
                count += 1
            out[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return out


def check_preserved(control):
    for unit, pid in control['preserved'].items():
        require(prop(unit, 'MainPID') == pid, 'unrelated service PID changed: ' + unit)


def api_ready(timeout=45):
    end = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen('http://127.0.0.1:8787/api/video-editing/config', timeout=2)
        except urllib.error.HTTPError as exc:
            require(exc.code == 401, 'unexpected unauthenticated API response')
            return
        except (urllib.error.URLError, OSError):
            require(time.monotonic() < end, 'API did not become ready')
            time.sleep(.5)
        else:
            raise RuntimeError('API unexpectedly allowed unauthenticated request')


def rollback_default(args, rev):
    """Keep both profile readers; switch only future CPU submissions back to v1."""
    cpu_storage()
    require(not MARKER.exists(), 'intake marker already exists; resolve the unfinished rollout first')
    config = Path('/etc/video-editing/video-editing.env')
    require(config.is_file() and not config.is_symlink(), 'video editing configuration missing')
    original = config.read_bytes()
    lines = original.decode().splitlines()
    values = [line.split('=', 1)[1] for line in lines if line.startswith('VIDEO_EDITING_EXPORT_PROFILE=')]
    require(len(values) <= 1, 'duplicate default export profile setting')
    backup = CPU_DATA / 'backups' / ('compact-default-v1-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(mode=0o700, parents=True)
    atomic(backup / 'video-editing.env', original, 0o600)
    control = {'preserved': {u: prop(u, 'MainPID') for u in PRESERVED},
               'worker_state': prop('video-editing-worker.service', 'ActiveState')}
    save(backup / 'control.json', control)
    atomic(MARKER, MARKER_TEXT)
    cpu_idle(args.drain_timeout)
    run('systemctl', 'stop', 'video-editing-worker.service')
    require(prop('video-editing-worker.service', 'MainPID') == '0', 'editing worker did not stop')
    before = cpu_evidence(); save(backup / 'before.json', before)
    run('systemctl', 'stop', API)
    lines = [line for line in lines if not line.startswith('VIDEO_EDITING_EXPORT_PROFILE=')]
    atomic(config, ('\n'.join(lines + ['VIDEO_EDITING_EXPORT_PROFILE=video-editing-720-v1']) + '\n').encode(), 0o600)
    run('systemctl', 'start', API); api_ready()
    require(cpu_evidence() == before, 'business records changed during default rollback')
    if control['worker_state'] == 'active': run('systemctl', 'start', 'video-editing-worker.service')
    require(prop('video-editing-worker.service', 'ActiveState') == control['worker_state'], 'editing worker state mismatch')
    check_preserved(control); MARKER.unlink()
    result = {'ok': True, 'phase': 'rollback-default', 'feature_commit': rev, 'backup': str(backup),
              'default_profile': 'video-editing-720-v1', 'compatible_readers_retained': True}
    save(backup / 'result.json', result)
    return result


def cpu(args, rev):
    cpu_storage()
    if args.phase == 'rollback-default': return rollback_default(args, rev)
    backup = CPU_DATA / 'backups' / ('compact-export-' + rev[:12])
    if args.phase == 'check':
        for name, digest in CPU_HASHES.items():
            require(sha(CPU_ROOT / name) == digest, 'CPU baseline mismatch: ' + name)
        return {'host': 'cpu', 'phase': 'check', 'release': rev, 'jobs': cpu_counts(),
                'backup': str(backup), 'marker_exists': MARKER.exists()}
    if args.phase == 'prepare':
        require(not backup.exists() and not MARKER.exists(), 'rollout backup or intake marker already exists')
        for name, digest in CPU_HASHES.items():
            require(sha(CPU_ROOT / name) == digest, 'CPU baseline mismatch: ' + name)
        backup.mkdir(mode=0o700, parents=True)
        control = {'claims': {u: prop(u, 'ActiveState') for u in CLAIMS},
                   'preserved': {u: prop(u, 'MainPID') for u in PRESERVED},
                   'api_state': prop(API, 'ActiveState')}
        save(backup / 'control.json', control)
        MARKER.parent.mkdir(exist_ok=True); MARKER.parent.chmod(0o755)
        atomic(MARKER, MARKER_TEXT)
        cpu_idle(args.drain_timeout)
        for unit in CLAIMS:
            if control['claims'][unit] == 'active': run('systemctl', 'stop', unit)
            require(prop(unit, 'MainPID') == '0', 'claim worker did not stop')
        cpu_idle(0); check_preserved(control)
        save(backup / 'before.json', cpu_evidence())
    else:
        require(backup.is_dir(), 'prepare backup missing')
        control = json.loads((backup / 'control.json').read_text())
        require(MARKER.read_bytes() == MARKER_TEXT, 'rollout intake marker mismatch')
        check_preserved(control)
        require(all(prop(u, 'MainPID') == '0' for u in CLAIMS), 'claims must remain stopped')
        if args.phase == 'apply':
            cpu_idle(0)
            require(cpu_evidence() == json.loads((backup / 'before.json').read_text()), 'business records changed during rollout')
            rows = []
            for index, (name, digest) in enumerate(CPU_HASHES.items()):
                target = CPU_ROOT / name
                require(sha(target) == digest, 'CPU baseline mismatch: ' + name)
                compile((ROOT / name).read_text(encoding='utf-8'), str(ROOT / name), 'exec')
                old = backup / ('file-' + str(index)); shutil.copy2(target, old)
                rows.append({'path': str(target), 'source': name, 'backup': str(old),
                             'old_sha': digest, 'new_sha': sha(ROOT / name)})
            save(backup / 'files.json', rows)
            with closing(sqlite3.connect('file:' + str(CPU_DB) + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(str(backup / 'evidence.sqlite3'))) as destination: source.backup(destination)
            run('systemctl', 'stop', API)
            for row in rows: atomic(row['path'], (ROOT / row['source']).read_bytes())
            run('systemctl', 'start', API); api_ready()
            require(cpu_evidence() == json.loads((backup / 'before.json').read_text()), 'business records changed during code install')
            for row in rows: require(sha(row['path']) == row['new_sha'], 'installed file mismatch')
        elif args.phase == 'restore':
            require((backup / 'apply.json').is_file(), 'CPU apply proof missing')
            for unit, state in control['claims'].items():
                if state == 'active': run('systemctl', 'start', unit)
                require(prop(unit, 'ActiveState') == state, 'claim state was not restored')
            MARKER.unlink()
        elif args.phase == 'abort':
            require(not (backup / 'files.json').exists(), 'code install has begun; retain compatible readers and use documented rollback')
            for unit, state in control['claims'].items():
                if state == 'active': run('systemctl', 'start', unit)
            MARKER.unlink()
        else:
            raise RuntimeError('unsupported CPU phase')
        check_preserved(control)
    result = {'ok': True, 'host': 'cpu', 'phase': args.phase, 'release': rev, 'backup': str(backup)}
    save(backup / (args.phase + '.json'), result)
    return result


def gpu_guard(candidate):
    spec = importlib.util.spec_from_file_location('compact_gpu_guard', candidate / 'scripts/deploy_video_editing_gpu.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def gpu(args, rev):
    import pwd
    base = Path('/data/drama-synthesis-gpu')
    candidate = Path(args.gpu_release).resolve()
    require(candidate.parent == base / 'releases' and re.fullmatch('[0-9a-f]{40}', candidate.name), 'invalid GPU release path')
    gpu_rev = published(candidate, GPU_REF)
    require(candidate.name == gpu_rev, 'GPU checkout path/commit mismatch')
    require((base / 'current').resolve() == base / 'releases' / GPU_BASELINE, 'GPU baseline changed')
    changed = set(run('git', '-C', str(candidate), 'diff', '--name-only', GPU_BASELINE, gpu_rev).splitlines())
    allowed = set(GPU_FILES) | {'scripts/deploy_video_editing_compact_export.py'}
    require(set(GPU_FILES) <= changed and all(f in allowed or f.startswith('doc/20261029.video-editing-compact-export/')
                                            for f in changed), 'GPU composite contains unrelated changes')
    for name in GPU_FILES:
        require(sha(candidate / name) == sha(ROOT / name), 'GPU source differs from feature commit: ' + name)
    guard = gpu_guard(candidate)
    env = guard.effective_env(); health = guard.health()
    guard.check_health(health, GPU_BASELINE)
    require(health.get('video_editing_probe_concurrency') == 3 and health.get('render_concurrency') == 1,
            'shared concurrency changed')
    snapshot = guard.ledger_snapshot(env['DRAMA_WORK_ROOT'])
    if args.phase == 'check':
        return {'ok': True, 'host': 'gpu', 'phase': 'check', 'feature_commit': rev,
                'gpu_commit': gpu_rev, 'busy': snapshot['busy'], 'marker_exists': MARKER.exists()}
    require(args.phase == 'apply', 'GPU supports only check/apply')
    require(not MARKER.exists(), 'GPU intake marker already exists')
    before = guard.wait_for_idle(env['DRAMA_WORK_ROOT'], args.drain_timeout)
    guard.wait_for_media_idle(env, max(3, args.drain_timeout))
    identity = pwd.getpwnam('drama-synthesis-gpu')
    candidate_env = dict(env, DRAMA_GPU_RELEASE_SHA=gpu_rev, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    preflight = guard.run(str(base / 'runtime/current/bin/python'),
        str(candidate / 'scripts/check_drama_synthesis_gpu_runtime.py'), '--check-app-import',
        env=candidate_env, cwd=candidate, user=identity.pw_uid, group=identity.pw_gid, timeout=300)
    require(json.loads(preflight).get('ok') is True, 'GPU candidate preflight failed')
    backup = base / 'deploy-backups' / ('compact-export-' + gpu_rev[:12])
    guard.backup_configuration(backup)
    save(backup / 'before.json', {'release': GPU_BASELINE, 'health': health, 'ledger': before})
    atomic(MARKER, MARKER_TEXT)
    guard.wait_for_idle(env['DRAMA_WORK_ROOT'], 0)
    guard.wait_for_media_idle(env, 3)
    guard.run('systemctl', 'stop', '--no-block', guard.WORKER); guard.wait_worker_stopped()
    require(guard.ledger_snapshot(env['DRAMA_WORK_ROOT'])['hashes'] == before['hashes'], 'GPU ledger changed before switch')
    guard.atomic(guard.ENV_FILE, guard.merge_environment(guard.ENV_FILE.read_bytes(), {'DRAMA_GPU_RELEASE_SHA': gpu_rev}))
    guard.switch_link(candidate)
    guard.run('systemctl', 'start', guard.WORKER, timeout=360)
    guard.run('systemctl', 'start', guard.TUNNEL)
    # The reusable guard's previous-health branch hard-codes v1; compare its
    # legacy fields explicitly while permitting the new editor profile.
    new = guard.wait_for_health(gpu_rev)
    require(all(new.get(k) == health.get(k) for k in guard.SAFE_HEALTH), 'unrelated GPU health changed')
    # This existing health label describes the original transport and remains
    # hard-coded in the unchanged shared worker. Recipe support is validated by
    # the exact three source hashes and candidate preflight, not this label.
    require(new.get('video_editing_profile') == health.get('video_editing_profile') and
            new.get('video_editing_probe_concurrency') == 3 and new.get('video_editing_enabled') is False,
            'new GPU profile or intake state mismatch')
    require(guard.ledger_snapshot(env['DRAMA_WORK_ROOT'])['hashes'] == before['hashes'], 'GPU ledger changed after restart')
    require(guard.property_value(guard.TUNNEL, 'ActiveState') == 'active', 'GPU tunnel unavailable')
    MARKER.unlink()
    require(guard.health().get('video_editing_enabled') is True, 'GPU editing intake did not reopen')
    result = {'ok': True, 'host': 'gpu', 'phase': 'apply', 'feature_commit': rev, 'gpu_commit': gpu_rev,
              'backup': str(backup), 'ledger_unchanged': True, 'render_concurrency': 1, 'probe_concurrency': 3}
    save(backup / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('cpu', 'gpu'), required=True)
    parser.add_argument('--phase', choices=('check', 'prepare', 'apply', 'restore', 'abort', 'rollback-default'), default='check')
    parser.add_argument('--gpu-release')
    parser.add_argument('--drain-timeout', type=int, default=45)
    args = parser.parse_args()
    require(0 <= args.drain_timeout <= 1800, 'invalid drain timeout')
    rev = published(ROOT, FEATURE_REF)
    if args.host == 'gpu': require(args.gpu_release, '--gpu-release is required')
    result = cpu(args, rev) if args.host == 'cpu' else gpu(args, rev)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()

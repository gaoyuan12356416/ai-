#!/usr/bin/env python3
"""Switch the reviewed HK GPU lease overlay to a verified clean release.

Plan is read-only. Apply/rollback NEVER stop or drain a service: the user must
first pause work and the operator must observe an already inactive, empty unit.
This one-time bridge preserves the exact known overlay as the rollback target.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_install import command, plan, private_json, unit_text

BASE_SHA = '179596a9699393cd377c0e06a0bec94e171f14cd'
OVERLAY_SHA = 'f3bd38531f6da7e11fbd853444b7d12d95f50ff2'
UNIT = 'pipeline-node-hk-gpu.service'
STATE = Path('/data/pipeline-dispatch')
SYSTEMD = Path('/etc/systemd/system')
RUNTIME = Path('/data/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d')
SOURCE = Path(str(RUNTIME) + '-source')
NODE = RUNTIME / 'node'
BASE = Path('/opt/pipeline-dispatch/releases') / BASE_SHA
PYTHON = Path('/opt/pipeline-dispatch/venvs') / BASE_SHA / 'bin/python'
OLD_ENV = Path('/opt/pipeline-dispatch/staged/hk-gpu') / BASE_SHA / 'node.env'
CACHE_NAME = '30-rgba-asset-cache.conf'
LEASE_NAME = '99-fb-lease-receipt-20261104.conf'
BASE_UNIT_SHA = 'c536f4d244d2e830df9bef84a0000961e3e66f35a0f70008022e3e39f04724c9'
CACHE_SHA = '7e0625ef86ce75aebd2007f0df89637313df20557bd18bf0369115af3af622d8'
FILES = {
    'features/fb_gpu/job_lock.py': '18151e009aa45b7607479adb79b9711b08728722910ada922fe7473ba045a0d9',
    'features/fb_gpu/render_process.py': 'cac9ecce3ef12dfa90327a7655f0445f2048ceca6f1ec5de8fb02c463231b190',
    'features/fb_gpu/prepare_worker.py': '2424f8ced78d9b276c91a2e43b5c959d9ea0023766f699f07dcf48f665ae03c8',
    'features/pipeline_dispatch/worker.py': '25ccd40ac0b3693a2d8bfad6d8edc7f1980ca968e4bcc16a58fbcac85353273c',
    'features/pipeline_dispatch/executors.py': '36ef4b600521ef2f4c19c04100bd40723f1b800c1f2c7901c183bca9bb428f90',
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path')
    require(path.is_file(), 'required_file_missing')
    return path.read_bytes()


def paths():
    return SYSTEMD / UNIT, SYSTEMD / (UNIT + '.d') / CACHE_NAME, SYSTEMD / (UNIT + '.d') / LEASE_NAME


def lease_bytes():
    return ('[Service]\nWorkingDirectory=' + str(NODE) + '\nExecStart=\nExecStart='
            + str(PYTHON) + ' ' + str(NODE / 'scripts/pipeline_worker.py') + '\n').encode()


def clean_github(root, sha):
    require(command(['git', '-C', root, 'rev-parse', 'HEAD']) == sha, 'git_commit_mismatch')
    require(not command(['git', '-C', root, 'status', '--porcelain']), 'git_dirty')
    origin = command(['git', '-C', root, 'remote', 'get-url', 'origin'])
    require(origin in {'git@github.com:gaoyuan12356416/ai-.git',
                       'https://github.com/gaoyuan12356416/ai-.git'}, 'github_origin_mismatch')


def properties():
    keys = ['ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup',
            'FragmentPath', 'DropInPaths', 'WorkingDirectory', 'KillMode', 'TimeoutStopUSec']
    argv = ['systemctl', 'show', UNIT]
    for key in keys:
        argv.extend(['-p', key])
    return dict(row.split('=', 1) for row in command(argv).splitlines() if '=' in row)


def no_cgroup_processes(group, *, root=Path('/sys/fs/cgroup')):
    # After a clean stop systemd can forget ControlGroup; still inspect the
    # known slice path. The fixed base unit has no custom Slice directive.
    group = group or '/system.slice/' + UNIT
    require(group.startswith('/') and '..' not in group.split('/'), 'cgroup_path_invalid')
    if (root / 'cgroup.controllers').is_file():
        mount = root
    elif (root / 'systemd').is_dir():
        mount = root / 'systemd'
    else:
        raise ValueError('systemd_cgroup_mount_missing')
    target = mount / group.lstrip('/')
    require(not any(p.is_symlink() for p in (target, *target.parents)), 'cgroup_symlink')
    if not target.exists():
        return
    files = list(target.rglob('cgroup.procs')) + list(target.rglob('tasks'))
    require(bool(files), 'cgroup_process_inventory_missing')
    require(all(not p.read_text().strip() for p in files), 'unit_cgroup_not_empty')


def assert_idle():
    props = properties()
    require(props.get('ActiveState') == 'inactive' and props.get('SubState') == 'dead', 'unit_not_inactive')
    require(props.get('MainPID') == '0' and props.get('ControlPID') == '0', 'unit_has_process')
    rows = command(['systemctl', 'list-jobs', '--no-legend', '--plain']).splitlines()
    require(not any(len(row.split()) >= 2 and row.split()[1] == UNIT for row in rows), 'unit_has_pending_job')
    no_cgroup_processes(props.get('ControlGroup', ''))
    return props


def verify_layout(props, *, overlay):
    base, cache, lease = paths()
    require(props.get('FragmentPath') == str(base), 'unit_fragment_changed')
    expected = {str(cache), str(lease)} if overlay else {str(cache)}
    require(set(props.get('DropInPaths', '').split()) == expected, 'unit_dropins_changed')
    require(props.get('KillMode') == 'mixed' and props.get('TimeoutStopUSec') == 'infinity', 'unit_drain_contract_changed')


def verify_overlay_files():
    manifest_bytes = regular(RUNTIME / 'manifest.json')
    manifest = json.loads(manifest_bytes)
    require(manifest.get('status') == 'applied' and manifest.get('commit') == OVERLAY_SHA
            and manifest.get('source') == str(SOURCE) and manifest.get('host') == 'hk'
            and set(manifest.get('lanes', {})) == {'node'}, 'overlay_manifest_changed')
    item = manifest.get('lanes', {}).get('node', {})
    require(item.get('candidate') == str(NODE) and item.get('unit') == UNIT
            and item.get('python') == str(PYTHON) and item.get('previous') == str(BASE), 'overlay_layout_changed')
    require(set(item.get('files', {})) == set(FILES), 'overlay_file_set_changed')
    clean_github(SOURCE, OVERLAY_SHA)
    clean_github(BASE, BASE_SHA)
    for relative, expected in FILES.items():
        require(item['files'][relative].get('after') == expected, 'overlay_record_changed')
        require(digest(regular(NODE / relative)) == expected
                and digest(regular(SOURCE / relative)) == expected, 'overlay_file_changed')
    # The runtime is a copied tree, not itself Git. Check every baseline tracked
    # file too, so a second unrecorded overlay cannot be silently discarded.
    tracked = command(['git', '-C', BASE, 'ls-files']).splitlines()
    require(bool(tracked), 'baseline_inventory_empty')
    for relative in tracked:
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'tracked_path_invalid')
        if relative not in FILES:
            require(regular(NODE / relative) == regular(BASE / relative), 'unrecorded_runtime_change')
    expected_python = {name for name in tracked if name.endswith('.py')} | set(FILES)
    require({str(p.relative_to(NODE)).replace('\\', '/') for p in NODE.rglob('*.py')}
            == expected_python, 'unrecorded_runtime_python')
    return manifest_bytes


def verified_candidate(current, candidate):
    old, new = private_json(current), private_json(candidate)
    before, after = plan(old), plan(new)
    require(before['node_id'] == 'hk-gpu' and old['backend'] == new['backend'] == 'gpu', 'hk_gpu_only')
    require(before['sha'] == BASE_SHA and new.get('gpu_native') is True, 'release_profile_mismatch')
    for key in ('node_id', 'state', 'unit', 'root'):
        require(before[key] == after[key], 'node_identity_changed')
    require(after['state'] == str(STATE) and after['sha'] not in {BASE_SHA, OVERLAY_SHA}, 'candidate_identity_invalid')
    require(Path(current).resolve() != Path(candidate).resolve(), 'candidate_is_current')
    clean_github(Path(after['release']), after['sha'])
    require(regular(Path(after['staging']) / '.verified').decode() == after['sha'], 'candidate_not_verified')
    candidate_unit = regular(Path(after['staging']) / UNIT)
    require(candidate_unit == unit_text(new, after).encode(), 'candidate_unit_changed')
    regular(Path(after['staging']) / 'node.env')
    require((Path(after['venv']) / 'bin/python').is_file(), 'candidate_python_missing')
    return after, candidate_unit


def inspect(current, candidate):
    after, candidate_unit = verified_candidate(current, candidate)
    runtime_manifest = verify_overlay_files()
    base, cache, lease = paths()
    require(digest(regular(base)) == BASE_UNIT_SHA, 'base_unit_changed')
    require(digest(regular(cache)) == CACHE_SHA, 'cache_dropin_changed')
    require(regular(lease) == lease_bytes(), 'lease_dropin_changed')
    props = properties()
    verify_layout(props, overlay=True)
    require(props.get('WorkingDirectory') == str(NODE), 'runtime_working_directory_changed')
    snapshot = {'install-manifest.before.json': regular(current),
                'install-manifest.after.json': regular(candidate),
                'node.service.before': regular(base), 'node.service.after': candidate_unit,
                'node.env.before': regular(OLD_ENV),
                'node.env.after': regular(Path(after['staging']) / 'node.env'),
                'cache.conf.before': regular(cache),
                'lease.conf.before': regular(lease), 'runtime-manifest.before.json': runtime_manifest}
    snapshot.update({'overlay/' + name: regular(NODE / name) for name in FILES})
    return after, snapshot


def atomic(path, data, mode):
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'write_symlink')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.gpu-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup_path(folder, *, exists):
    folder = Path(folder)
    require(folder.parent == STATE / 'maintenance' and folder.name.startswith('gpu-full-offload-'), 'backup_path_invalid')
    require(not any(p.is_symlink() for p in (folder, *folder.parents)), 'backup_symlink')
    require(folder.exists() == exists, 'backup_existence_mismatch')
    if exists:
        require(folder.is_dir() and (os.name == 'nt' or not folder.stat().st_mode & 0o077), 'backup_permissions')
    return folder


def save_audit(folder, audit):
    atomic(folder / 'audit.json', (json.dumps(audit, indent=2) + '\n').encode(), 0o600)


def assert_authorized(paused):
    require(paused, 'user_pause_confirmation_required')
    require(os.name == 'posix' and os.geteuid() == 0, 'linux_root_required')


def assert_started(directory, python, *, overlay):
    props = properties()
    require(props.get('ActiveState') == 'active' and int(props.get('MainPID', '0')) > 0, 'unit_not_active_after_start')
    verify_layout(props, overlay=overlay)
    pid = props['MainPID']
    require(os.readlink('/proc/' + pid + '/cwd') == str(directory), 'runtime_cwd_mismatch')
    argv = Path('/proc/' + pid + '/cmdline').read_bytes().decode().split('\0')
    require(argv[:2] == [str(python), str(Path(directory) / 'scripts/pipeline_worker.py')], 'runtime_argv_mismatch')


def apply(current, candidate, folder, *, paused=False):
    assert_authorized(paused)
    assert_idle()
    after, snapshot = inspect(current, candidate)
    folder = backup_path(folder, exists=False)
    folder.mkdir(mode=0o700, parents=True)
    for name, data in snapshot.items():
        target = folder / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic(target, data, 0o600)
    audit = dict(version=1, status='prepared', current=str(current), candidate=str(candidate),
                 candidate_sha=after['sha'], previous_overlay_sha=OVERLAY_SHA,
                 user_pause_confirmed=True,
                 created_at=datetime.now(timezone.utc).isoformat(),
                 hashes={name:digest(data) for name, data in snapshot.items()})
    save_audit(folder, audit)
    # Re-read the exact state after backup, before the first live mutation.
    assert_idle()
    latest, live = inspect(current, candidate)
    require(latest == after and live == snapshot, 'state_changed_during_backup')
    audit['status'] = 'applying'
    save_audit(folder, audit)
    base, cache, lease = paths()
    atomic(base, snapshot['node.service.after'], 0o644)
    atomic(current, snapshot['install-manifest.after.json'], 0o600)
    lease.unlink()  # Exact reviewed drop-in only; the asset-cache drop-in stays.
    command(['systemctl', 'daemon-reload'])
    try:
        # Refuse a concurrent external service start instead of issuing restart.
        assert_idle()
        props = properties()
        verify_layout(props, overlay=False)
        require(props.get('WorkingDirectory') == after['release'], 'new_unit_not_effective')
        command(['systemctl', 'start', UNIT])
        audit['status'] = 'start_requested'
        save_audit(folder, audit)
        assert_started(after['release'], after['venv'] + '/bin/python', overlay=False)
        audit['status'] = 'started'
        save_audit(folder, audit)
    except BaseException:
        audit['status'] = 'needs_inspection'
        save_audit(folder, audit)
        raise
    return {'status': audit['status'], 'candidate_sha': after['sha'], 'backup': str(folder)}


def rollback(current, folder, *, paused=False):
    assert_authorized(paused)
    assert_idle()
    folder = backup_path(folder, exists=True)
    audit = private_json(folder / 'audit.json')
    require(audit.get('version') == 1 and audit.get('previous_overlay_sha') == OVERLAY_SHA
            and audit.get('current') == str(current), 'backup_identity_mismatch')
    expected = {'install-manifest.before.json', 'install-manifest.after.json',
                'node.service.before', 'node.service.after', 'node.env.before', 'node.env.after',
                'cache.conf.before', 'lease.conf.before', 'runtime-manifest.before.json'} | {'overlay/' + name for name in FILES}
    require(set(audit.get('hashes', {})) == expected, 'backup_file_set_changed')
    snapshot = {name: regular(folder / name) for name in expected}
    require(all(digest(data) == audit['hashes'][name] for name, data in snapshot.items()), 'backup_file_changed')
    require(digest(snapshot['node.service.before']) == BASE_UNIT_SHA
            and digest(snapshot['cache.conf.before']) == CACHE_SHA
            and snapshot['lease.conf.before'] == lease_bytes(), 'backup_baseline_changed')
    for name, expected_hash in FILES.items():
        require(digest(snapshot['overlay/' + name]) == expected_hash, 'backup_overlay_changed')
    require(verify_overlay_files() == snapshot['runtime-manifest.before.json'], 'rollback_runtime_changed')
    base, cache, lease = paths()
    require(regular(cache) == snapshot['cache.conf.before'] and regular(OLD_ENV) == snapshot['node.env.before'], 'rollback_preserved_config_changed')
    # Supports an interrupted switch, but never overwrites an unrelated change.
    require(regular(base) in (snapshot['node.service.before'], snapshot['node.service.after']), 'rollback_unit_drift')
    require(regular(current) in (snapshot['install-manifest.before.json'], snapshot['install-manifest.after.json']), 'rollback_manifest_drift')
    require(not lease.exists() or regular(lease) == snapshot['lease.conf.before'], 'rollback_dropin_drift')
    props = properties()
    # An interruption between unlink/write and daemon-reload leaves systemd's
    # cached layout at either exact endpoint, independently of disk existence.
    # Disk files above must individually match an audited endpoint. Unknown
    # loaded drop-ins remain a refusal, and the restored state is strict below.
    try:
        verify_layout(props, overlay=True)
    except ValueError:
        verify_layout(props, overlay=False)
    assert_idle()
    audit['status'] = 'rolling_back'
    save_audit(folder, audit)
    try:
        atomic(base, snapshot['node.service.before'], 0o644)
        atomic(current, snapshot['install-manifest.before.json'], 0o600)
        atomic(lease, snapshot['lease.conf.before'], 0o644)
        command(['systemctl', 'daemon-reload'])
        assert_idle()
        props = properties()
        verify_layout(props, overlay=True)
        require(props.get('WorkingDirectory') == str(NODE), 'restored_unit_not_effective')
        command(['systemctl', 'start', UNIT])
        assert_started(NODE, PYTHON, overlay=True)
        audit['status'] = 'rolled_back'
        save_audit(folder, audit)
    except BaseException:
        audit['status'] = 'needs_inspection'
        save_audit(folder, audit)
        raise
    return {'status': 'rolled_back', 'overlay_sha': OVERLAY_SHA, 'backup': str(folder)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'rollback'])
    parser.add_argument('--current', required=True, type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--user-paused-confirmed', action='store_true',
                        help='operator attests the user already paused and confirmed; does not pause or stop anything')
    args = parser.parse_args()
    if args.action == 'rollback':
        require(args.backup is not None, 'backup_required')
        result = rollback(args.current, args.backup, paused=args.user_paused_confirmed)
    else:
        require(args.candidate is not None, 'candidate_required')
        if args.action == 'plan':
            after, _ = inspect(args.current, args.candidate)
            result = {'action': 'plan', 'candidate_sha': after['sha'], 'overlay_sha': OVERLAY_SHA,
                      'service_state': properties().get('ActiveState'), 'mutations': False}
        else:
            require(args.backup is not None, 'backup_required')
            result = apply(args.current, args.candidate, args.backup, paused=args.user_paused_confirmed)
    print(json.dumps(result))


if __name__ == '__main__':
    main()

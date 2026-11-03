#!/usr/bin/env python3
"""Stage an already verified CPU node release behind a natural drain.

Default is read-only. The only activation writes are the CPU unit, its private
installation manifest, and a private backup/audit. Existing node identity,
credentials, state, capacities, task leases, and GPU services are preserved.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_install import private_json, plan, command
from scripts.activate_pipeline_gpu_offload import DRAIN_WORKER_SHAS


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key in result:
            raise ValueError('environment_syntax')
        if value.startswith(('"', "'")):
            words = shlex.split(value)
            if len(words) != 1:
                raise ValueError('environment_syntax')
            value = words[0]
        result[key] = value
    return result


def validate_identity(old, new, before, after):
    if old.get('backend') != 'cpu' or new.get('backend') != 'cpu':
        raise ValueError('cpu_only')
    # This deployment changes code only. In particular it cannot introduce a
    # new identity, backend, coordinator URL, enrollment or capacity override.
    if {key: value for key, value in old.items() if key != 'sha'} != {
        key: value for key, value in new.items() if key != 'sha'
    }:
        raise ValueError('manifest_configuration_changed')
    for key in ('node_id', 'state', 'unit', 'root'):
        if before[key] != after[key]:
            raise ValueError('node_identity_changed')


def validate_environment(before, after):
    previous = environment(Path(before['staging']) / 'node.env')
    candidate = environment(Path(after['staging']) / 'node.env')
    old_ca, new_ca = previous.get('SSL_CERT_FILE'), candidate.get('SSL_CERT_FILE')
    if old_ca != new_ca:
        # The installer relocates certifi into each release's venv. Trust
        # contents must remain identical even when this generated path moves.
        if (not old_ca or not new_ca or not old_ca.startswith(before['venv'] + '/')
                or not new_ca.startswith(after['venv'] + '/') or digest(old_ca) != digest(new_ca)):
            raise ValueError('tls_trust_changed')
        candidate['SSL_CERT_FILE'] = old_ca
    if previous != candidate:
        raise ValueError('node_environment_changed')
    if (previous.get('PIPELINE_NODE_ID') != before['node_id']
            or previous.get('PIPELINE_RENDER_BACKEND') != 'cpu'
            or previous.get('PIPELINE_NODE_STATE_ROOT') != before['state']
            or previous.get('PIPELINE_NODE_CONFIG') != before['state'] + '/credentials.json'):
        raise ValueError('node_environment_identity_mismatch')
    return previous


def validate_release(info, run=command):
    root = Path(info['release'])
    if digest(root / 'features/pipeline_dispatch/worker.py') not in DRAIN_WORKER_SHAS:
        raise ValueError('unreviewed_drain_worker')
    if run(['git', '-C', root, 'rev-parse', 'HEAD']) != info['sha'] or run(['git', '-C', root, 'status', '--porcelain']):
        raise ValueError('release_not_clean')
    if (Path(info['staging']) / '.verified').read_text() != info['sha']:
        raise ValueError('release_not_verified')


def validate_dependencies(info, run=command):
    # Import the exact candidate with its own interpreter, then exercise the
    # transport through mocks. Neither command initializes a publisher/service
    # nor sends a request to a platform or coordinator.
    check = (
        "import inspect,sys;sys.path.insert(0,sys.argv[1]);"
        "from features.pipeline_dispatch.executors import NodeExecutors;"
        "from features.x_posts.service import XApiClient;"
        "from features.x_posts.media_limits import MAX_UPLOAD_SEGMENTS;"
        "assert callable(NodeExecutors.x_publish);"
        "assert 'chunk_bytes' in inspect.signature(XApiClient).parameters;"
        "assert isinstance(MAX_UPLOAD_SEGMENTS,int) and MAX_UPLOAD_SEGMENTS>0"
    )
    python = str(Path(info['venv']) / 'bin/python')
    base = ['runuser', '-u', 'pipeline-worker', '--', python, '-B']
    run(base + ['-c', check, info['release']], timeout=60)
    run(base + ['-m', 'unittest', 'scripts.test_pipeline_x_upload_transport'], cwd=info['release'], timeout=60)


def _private_write(path, data):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def activate(current, candidate, *, apply=False, run=command,
             systemd_root=Path('/etc/systemd/system'), proc_root=Path('/proc')):
    old, new = private_json(current), private_json(candidate)
    before, after = plan(old), plan(new)
    validate_identity(old, new, before, after)
    for info in (before, after):
        validate_release(info, run)
    expected_env = validate_environment(before, after)
    unit = systemd_root / before['unit']
    target = Path(after['staging']) / after['unit']
    if unit.read_bytes() != (Path(before['staging']) / before['unit']).read_bytes():
        raise ValueError('live_unit_differs_from_manifest')
    for path in (unit, target):
        entries = set(path.read_text().splitlines())
        if not {'KillMode=mixed', 'TimeoutStopSec=infinity'}.issubset(entries):
            raise ValueError('unsafe_unit_drain')
    def runtime():
        return dict(line.split('=', 1) for line in run([
            'systemctl', 'show', before['unit'], '-p', 'ActiveState', '-p', 'MainPID',
            '-p', 'KillMode', '-p', 'TimeoutStopUSec']).splitlines())
    props = runtime()
    if props.get('ActiveState') != 'active' or props.get('KillMode') != 'mixed' or props.get('TimeoutStopUSec') != 'infinity':
        raise ValueError('runtime_not_active_or_safe')
    pid = int(props['MainPID'])
    process = proc_root / str(pid)
    if before['release'] + '/scripts/pipeline_worker.py' not in process.joinpath('cmdline').read_bytes().decode().split('\0'):
        raise ValueError('runtime_manifest_mismatch')
    actual_env = dict(item.split('=', 1) for item in process.joinpath('environ').read_bytes().decode().split('\0') if '=' in item)
    if any(actual_env.get(key) != value for key, value in expected_env.items()):
        raise ValueError('runtime_environment_changed')
    credentials = Path(before['state']) / 'credentials.json'
    credential_hash = digest(credentials)
    validate_dependencies(after, run)
    report = {'apply': apply, 'node_id': before['node_id'], 'previous_sha': before['sha'],
              'candidate_sha': after['sha'], 'old_main_pid': pid,
              'children_before': process.joinpath('task', str(pid), 'children').read_text().split(),
              'dependencies_verified': True, 'capacities_changed': False, 'credentials_changed': False}
    if not apply:
        return report
    if before['sha'] == after['sha']:
        return {**report, 'status': 'already_current'}
    # A drain/deploy initiated elsewhere must win over this stale preparation.
    if runtime() != props or digest(credentials) != credential_hash or unit.read_bytes() != (Path(before['staging']) / before['unit']).read_bytes():
        raise ValueError('runtime_changed_before_activation')
    backup = Path(before['state']) / 'maintenance' / ('cpu-transport-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for source, name in ((Path(current), 'install-manifest.before.json'), (unit, 'node.service.before'),
                         (Path(before['staging']) / 'node.env', 'node.env.before')):
        _private_write(backup / name, source.read_bytes())
    report['backup'] = str(backup)
    _private_write(backup / 'audit.before.json', (json.dumps(report, indent=2) + '\n').encode())
    temporary = Path(str(current) + '.cpu-transport-new')
    _private_write(temporary, (json.dumps(new, indent=2) + '\n').encode())
    run(['install', '-m', '0644', str(target), str(unit)])
    os.replace(temporary, current)
    run(['systemctl', 'daemon-reload'])
    # SIGTERM only reaches the reviewed parent first (KillMode=mixed). It asks
    # each child to finish its current job and joins every child without timeout.
    # systemd starts the candidate after that parent exits, not before.
    run(['systemctl', 'restart', '--no-block', before['unit']])
    report['status'] = 'draining_current_jobs_then_start_verified_release'
    report['credentials_changed'] = digest(credentials) != credential_hash
    _private_write(backup / 'audit.after.json', (json.dumps(report, indent=2) + '\n').encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(activate(args.current, args.candidate, apply=args.apply)))


if __name__ == '__main__':
    main()

"""Scoped monitor overlay preserving the production coordinator stop barrier.

Run from a fetched exact GitHub checkout. Existing worker claims and publication
facts are retained. Segmented uploads need permits between HTTP requests; idle handlers do not
permit bypassing the existing admission and claim drain protection.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UNIT = 'pipeline-coordinator.service'
POINTER = Path('/opt/pipeline-dispatch/current')
MODULES = ('features/pipeline_dispatch/queue_states.py', 'features/pipeline_dispatch/views.py')
ASSETS = ('pipeline-monitor.js', 'pipeline-monitor.html')
STATIC_ROOTS = (Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html'))
OVERRIDE = Path('/run/systemd/system') / (UNIT + '.d') / '99-monitor-http-switch.conf'
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
PID_UNITS = ('drama-material-api.service', 'x-post-automation.service', 'x-auto-post-service.service',
             'x-tag-post.service', 'x-tag-auto.service', 'pipeline-node-cpu-main.service')


def run(*args, timeout=45):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(folder, record):
    target = folder / 'deployment.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(target)


def replace_bytes(target, data):
    temporary = target.with_name(target.name + '.monitor-new')
    temporary.write_bytes(data)
    shutil.copymode(target, temporary)
    temporary.replace(target)


def pointer(target):
    temporary = POINTER.with_name(POINTER.name + '.monitor-new')
    temporary.symlink_to(target)
    temporary.replace(POINTER)


def pids():
    return {unit: run('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in PID_UNITS}


def prepare(folder, previous):
    assert run('findmnt', '-rn', '-o', 'UUID', '-T', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', str(ROOT), 'status', '--porcelain')
    assert not folder.exists()
    current = POINTER.resolve(strict=True)
    expected = lambda rel: subprocess.check_output(['git', '-C', str(ROOT), 'show', previous + ':' + rel])
    for rel in MODULES:
        assert (current / rel).read_bytes() == expected(rel), 'concurrent_monitor_module_change:' + rel
    for base in STATIC_ROOTS:
        for name in ASSETS:
            assert (base / name).read_bytes() == expected('static/' + name), 'concurrent_monitor_asset_change:' + str(base / name)
    folder.mkdir(parents=True, mode=0o755)
    record = {'sha': run('git', '-C', str(ROOT), 'rev-parse', 'HEAD'), 'previous_sha': previous,
              'previous_runtime': str(current), 'candidate': str(folder / 'candidate'), 'state': 'preparing',
              'static': [], 'module_before': {rel: sha(current / rel) for rel in MODULES}}
    run('cp', '-a', '--reflink=auto', str(current), record['candidate'])
    candidate = Path(record['candidate'])
    for rel in (*MODULES, *(str(Path('static') / name) for name in ASSETS),
                'scripts/test_pipeline_queue_states.py', 'scripts/test_pipeline_selection_wait_ui.js'):
        shutil.copy2(ROOT / rel, candidate / rel)
    for index, base in enumerate(STATIC_ROOTS):
        for name in ASSETS:
            backup = folder / 'backup' / str(index) / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / name, backup)
            record['static'].append({'target': str(base / name), 'backup': str(backup), 'before': sha(base / name)})
    run('/opt/pipeline-dispatch/coordinator-venv/bin/python', '-m', 'py_compile',
        *(str(candidate / rel) for rel in MODULES))
    run('python3', HEALTH, 'snapshot', '--lane', 'coordinator', '--output', str(folder / 'schedulers-before.json'))
    record['state'] = 'prepared'
    save(folder, record)
    print(json.dumps({'state': record['state'], 'directory': str(folder), 'sha': record['sha']}), flush=True)


def switch(folder, rollback=False):
    record = json.loads((folder / 'deployment.json').read_text())
    expected = record['candidate'] if rollback else record['previous_runtime']
    target = record['previous_runtime'] if rollback else record['candidate']
    assert str(POINTER.resolve(strict=True)) == expected, 'concurrent_runtime_switch'
    assert not OVERRIDE.exists()
    assert not Path('/run/pipeline-service-drain/pipeline-coordinator.service.json').exists(), 'coordinator_already_draining'
    if not rollback:
        for rel, digest in record['module_before'].items():
            assert sha(Path(expected) / rel) == digest, 'concurrent_monitor_module_change'
        for entry in record['static']:
            assert sha(Path(entry['target'])) == entry['before'], 'concurrent_monitor_asset_change'
    old_pid = run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value')
    original_pids = pids()
    config_hash = sha(Path('/etc/pipeline-coordinator.json'))
    # An idle HTTP interval is not an idle segmented upload: its next segment
    # still needs a fresh permit. Retain the existing service claim barrier.
    assert 'pipeline_service_drain.py stop --unit=' + UNIT in run('systemctl', 'show', UNIT, '-p', 'ExecStop')
    began = time.monotonic()
    changed = False
    record['state'] = 'draining'
    save(folder, record)
    try:
        run('systemctl', 'stop', UNIT, timeout=86400)
        assert run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value') == '0'
        pointer(target)
        changed = True
        run('systemctl', 'start', UNIT, timeout=90)
    except BaseException:
        if changed:
            run('systemctl', 'stop', UNIT, timeout=86400)
            pointer(expected)
        run('systemctl', 'start', UNIT, timeout=90)
        raise
    finally:
        record['service_switch_seconds'] = round(time.monotonic() - began, 3)
        save(folder, record)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10
    while True:
        try:
            with opener.open('http://127.0.0.1:18846/health', timeout=2) as response:
                assert response.status == 200
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    for entry in record['static']:
        source = Path(entry['backup']) if rollback else ROOT / 'static' / Path(entry['target']).name
        replace_bytes(Path(entry['target']), source.read_bytes())
    run('python3', HEALTH, 'restore', '--baseline', str(folder / 'schedulers-before.json'),
        '--wait-seconds', '25', '--output', str(folder / 'schedulers-after.json'))
    assert config_hash == sha(Path('/etc/pipeline-coordinator.json'))
    assert not OVERRIDE.exists()
    record.update(state='rolled_back' if rollback else 'active', old_pid=old_pid,
                  new_pid=run('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'),
                  other_pids_unchanged=original_pids == pids(), finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
    save(folder, record)
    print(json.dumps({key: record[key] for key in ('state', 'sha', 'service_switch_seconds', 'other_pids_unchanged')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'activate', 'rollback'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--previous-sha')
    args = parser.parse_args()
    folder = Path(args.directory)
    if args.action == 'prepare':
        prepare(folder, args.previous_sha)
    else:
        switch(folder, rollback=args.action == 'rollback')

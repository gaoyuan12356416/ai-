"""GitHub-first tag integration; drain only tag owners and coordinator RPCs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-tag-pipeline')
TAG = Path('/opt/x-tag-publisher/current')
COORD = Path('/opt/pipeline-dispatch/current')
TAG_ENV = Path('/etc/x-tag-publisher.env')
COORD_ENV = Path('/etc/pipeline-coordinator.env')
CONFIG = Path('/etc/pipeline-coordinator.json')
OVERRIDE = Path('/etc/systemd/system/pipeline-coordinator.service.d/70-x-tag-pipeline.conf')
PYTHON = '/opt/pipeline-dispatch/coordinator-venv/bin/python'
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
ID_BASE = 9000000000000
TRIGGERS = ('x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path',
            'x-tag-pool.timer', 'x-tag-manual.timer')
RUNNERS = ('x-tag-auto-runner.service', 'x-tag-auto-scheduler.service', 'x-tag-pool.service', 'x-tag-manual.service')


def run(args, timeout=60, check=True, **kwargs):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if check and p.returncode:
        raise RuntimeError('command_failed:' + args[0] + ':' + str(p.returncode) + ':' + p.stderr[-600:])
    return p.stdout.strip()


def emit(event, **values):
    print(json.dumps(dict(event=event, at=datetime.now(timezone.utc).isoformat(), **values)), flush=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def read_env(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            result[k] = v.strip().strip('"').strip("'")
    return result


def update_env(data, values):
    lines = data.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split('=', 1)[0]
        if key in values:
            lines[i] = key + '=' + values[key]
            seen.add(key)
    lines.extend(k + '=' + v for k, v in values.items() if k not in seen)
    return '\n'.join(lines) + '\n'


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nxt = path.with_name(path.name + '.tag-pipeline-next')
    nxt.write_bytes(data if isinstance(data, bytes) else data.encode())
    nxt.chmod(mode)
    nxt.replace(path)


def link(path, target):
    nxt = path.with_name(path.name + '.tag-pipeline-next')
    nxt.symlink_to(target)
    nxt.replace(path)


def state(unit):
    return dict(line.split('=', 1) for line in run(['systemctl', 'show', unit,
                '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState']).splitlines())


def db(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def health(port):
    deadline = time.monotonic() + 20
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/health', timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError('health_failed:' + str(port))
        time.sleep(.5)


def save(info):
    atomic(Path(info['backup']) / 'deployment.json', json.dumps(info, indent=2))


def stage():
    commit = run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'])
    if run(['git', '-C', str(SOURCE), 'status', '--porcelain']):
        raise RuntimeError('source_checkout_dirty')
    baseline = json.loads((SOURCE / 'deploy/x-tag-pipeline-baseline.json').read_text())
    roots = {'tag': TAG.resolve(), 'coordinator': COORD.resolve()}
    for name, expected in baseline['files'].items():
        label, rel = name.split('/', 1)
        if digest(roots[label] / rel) != expected:
            raise RuntimeError('affected_runtime_changed:' + name)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'deployments' / (stamp + '-' + commit[:12])
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    candidates = {}
    for name, old in roots.items():
        candidate = ROOT / 'releases' / commit / name
        candidate.mkdir(parents=True, exist_ok=False)
        for folder in ('features', 'scripts', 'static'):
            if (old / folder).exists():
                shutil.copytree(old / folder, candidate / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        module = candidate / 'features/x_tag_publish'
        module.mkdir(exist_ok=True)
        for filename in ('__init__.py', 'dispatch.py', 'dispatch_native.py'):
            shutil.copy2(SOURCE / 'features/x_tag_publish' / filename, module / filename)
        if name == 'tag':
            shutil.copy2(SOURCE / 'features/x_tag_publish/runtime.py', module / 'runtime.py')
        shutil.copy2(SOURCE / 'scripts/test_x_tag_dispatch.py', candidate / 'scripts/test_x_tag_dispatch.py')
        tests = 'scripts.test_x_tag_dispatch.DispatchTests' if name == 'coordinator' else 'scripts.test_x_tag_dispatch.NativeTests'
        p = subprocess.run([PYTHON, '-m', 'unittest', tests, '-v'], cwd=candidate, capture_output=True, text=True)
        atomic(backup / (name + '-tests.log'), p.stdout + p.stderr)
        if p.returncode:
            raise RuntimeError(name + '_tests_failed')
        run([PYTHON, '-m', 'compileall', '-q', 'features/x_tag_publish'], cwd=candidate)
        candidates[name] = str(candidate)
    files = {}
    for i, path in enumerate((TAG_ENV, COORD_ENV, CONFIG, OVERRIDE)):
        dest = backup / ('file-' + str(i))
        if path.exists():
            shutil.copy2(path, dest)
            dest.chmod(0o600)
        files[str(path)] = dict(before=digest(path), backup=str(dest), mode=path.stat().st_mode & 0o777 if path.exists() else 0o644)
    tag_env = read_env(TAG_ENV)
    pid = state('x-post-automation.service')['MainPID']
    production = dict(line.split('=', 1) for line in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in line)
    catalog = production.get('X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256', '')
    if not re.fullmatch('[a-f0-9]{64}', catalog):
        raise RuntimeError('x_catalog_missing')
    token = secrets.token_urlsafe(48)
    draft_tag = update_env(TAG_ENV.read_text(), {'PIPELINE_X_DISPATCH_ENABLED': '1',
        'PIPELINE_X_CANONICAL_BRIDGE_ENABLED': '1', 'PIPELINE_X_CANONICAL_TOKEN': token,
        'X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256': catalog})
    draft_coord = update_env(COORD_ENV.read_text(), {'PIPELINE_X_TAG_CANONICAL_TOKEN': token,
        'PIPELINE_X_TAG_AUTO_TOKEN': tag_env['X_AUTO_POST_INTERNAL_TOKEN']})
    cfg = json.loads(CONFIG.read_text())
    cfg['x_tag'] = {'paths': {'x': tag_env['X_POST_DB_PATH'], 'x_auto': tag_env['X_AUTO_POST_DB_PATH']},
                    'post_token_env': 'PIPELINE_X_TAG_CANONICAL_TOKEN', 'auto_token_env': 'PIPELINE_X_TAG_AUTO_TOKEN'}
    atomic(backup / 'tag.env.next', draft_tag)
    atomic(backup / 'coordinator.env.next', draft_coord)
    atomic(backup / 'coordinator.json.next', json.dumps(cfg, indent=2))
    inspection = '''import json,sys
from features.pipeline_dispatch.service import build_registry
from features.x_tag_publish.dispatch import extend_registry
cfg=json.load(open(sys.argv[1]))
r=extend_registry(build_registry(cfg),cfg)
counts={}
for key in [('x','tag_queue'),('x','tag_auto')]:
 rows=r.adapters[key].scan(5000)
 counts[':'.join(key)]=len(rows)
 for row in rows:
  assert r._adapter(row) is r.adapters[key]
print(json.dumps(counts))
'''
    preflight = run([PYTHON, '-c', inspection, str(backup / 'coordinator.json.next')],
                    cwd=candidates['coordinator'], env=dict(os.environ, **read_env(backup / 'coordinator.env.next')))
    atomic(backup / 'read-only-adapter-preflight.json', preflight)
    override = '[Service]\nExecStart=\nExecStart=' + PYTHON + ' -m features.x_tag_publish.dispatch --config /etc/pipeline-coordinator.json\nReadWritePaths=/mnt/data-disk/x-tag-publisher\n'
    atomic(backup / 'coordinator.unit.next', override)
    for name, path in [('tag-posts', tag_env['X_POST_DB_PATH']), ('tag-auto', tag_env['X_AUTO_POST_DB_PATH']), ('coordinator', cfg['db_path'])]:
        with closing(db(path)) as src, closing(sqlite3.connect(backup / (name + '.sqlite3'))) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup_integrity_failed')
        (backup / (name + '.sqlite3')).chmod(0o600)
    run([PYTHON, HEALTH, 'snapshot', '--lane', 'coordinator', '--output', str(backup / 'schedulers-before.json')])
    info = dict(commit=commit, backup=str(backup), roots={k: str(v) for k, v in roots.items()}, candidates=candidates,
                files=files, triggers={u: state(u) for u in TRIGGERS}, status='staged', catalog=catalog,
                original_pids={u: state(u)['MainPID'] for u in ('drama-material-api.service', 'x-post-automation.service', 'pipeline-node-cpu-main.service')})
    save(info)
    emit('staged', commit=commit, backup=str(backup), tests=8)


def wait_idle(units, timeout=10800):
    deadline, last = time.monotonic() + timeout, 0
    while True:
        active = {u: state(u) for u in units if state(u)['MainPID'] != '0'}
        if not active:
            return
        if time.monotonic() > deadline:
            raise RuntimeError('inflight_kept_waiting:' + ','.join(active))
        if time.monotonic() - last > 30:
            emit('draining_tag_requests', units=list(active))
            last = time.monotonic()
        time.sleep(2)


def restore_triggers(info):
    for unit, before in info['triggers'].items():
        if before['ActiveState'] == 'active':
            run(['systemctl', 'start', unit])


def switch_coordinator(info, callback):
    """Use the established short HTTP-only gate; running node work keeps leases."""
    pid = state('pipeline-coordinator.service')['MainPID']
    marker = 'x-tag-pipeline-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    rule = ['-p', 'tcp', '--dport', '18846', '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'DROP']
    watchdog = marker + '-gate'
    run(['systemd-run', '--quiet', '--unit=' + watchdog, '--on-active=75s', '--', '/usr/sbin/iptables', '-D', 'INPUT', *rule])
    gated = False
    began = time.monotonic()
    try:
        run(['iptables', '-I', 'INPUT', '1', *rule])
        gated = True
        deadline = time.monotonic() + 25
        while True:
            threads = len(list(Path('/proc/' + pid + '/task').iterdir()))
            sockets = [x for x in run(['ss', '-Hntp']).splitlines() if ('pid=' + pid + ',') in x]
            listeners = [x for x in run(['ss', '-Hlntp']).splitlines() if ('pid=' + pid + ',') in x]
            if threads == 2 and not sockets and not sum(int(x.split()[1]) for x in listeners):
                break
            if time.monotonic() > deadline:
                raise RuntimeError('coordinator_http_not_drained')
            time.sleep(.25)
        run(['systemctl', 'stop', 'pipeline-coordinator.service'], timeout=30)
        callback()
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'start', 'pipeline-coordinator.service'], timeout=20)
    finally:
        if gated:
            run(['iptables', '-D', 'INPUT', *rule])
        run(['systemctl', 'stop', watchdog + '.timer'], check=False)
    health(18846)
    info['coordinator_gate_seconds'] = round(time.monotonic() - began, 3)
    emit('coordinator_switched', seconds=info['coordinator_gate_seconds'])


def activate(info):
    if info['status'] != 'staged':
        raise RuntimeError('deployment_not_staged')
    for path, row in info['files'].items():
        if digest(path) != row['before']:
            raise RuntimeError('configuration_changed:' + path)
    if str(TAG.resolve()) != info['roots']['tag'] or str(COORD.resolve()) != info['roots']['coordinator']:
        raise RuntimeError('live_release_changed')
    backup = Path(info['backup'])
    run(['systemctl', 'stop', *TRIGGERS])
    info['status'] = 'draining'; save(info)
    run(['systemctl', 'stop', '--no-block', 'x-tag-auto.service'])
    wait_idle(('x-tag-auto.service', *RUNNERS))
    run(['systemctl', 'stop', '--no-block', 'x-tag-post.service'])
    wait_idle(('x-tag-post.service',))
    atomic(TAG_ENV, (backup / 'tag.env.next').read_bytes(), info['files'][str(TAG_ENV)]['mode'])
    link(TAG, info['candidates']['tag'])
    run(['systemctl', 'start', 'x-tag-post.service']); health(8821)
    run(['systemctl', 'start', 'x-tag-auto.service']); health(18843)
    info['status'] = 'tag_active'; save(info)
    def apply():
        atomic(COORD_ENV, (backup / 'coordinator.env.next').read_bytes(), info['files'][str(COORD_ENV)]['mode'])
        atomic(CONFIG, (backup / 'coordinator.json.next').read_bytes(), info['files'][str(CONFIG)]['mode'])
        atomic(OVERRIDE, (backup / 'coordinator.unit.next').read_bytes(), 0o644)
        link(COORD, info['candidates']['coordinator'])
    switch_coordinator(info, apply)
    info['applied_hashes'] = {path: digest(path) for path in info['files']}
    save(info)
    restore_triggers(info)
    run([PYTHON, HEALTH, 'restore', '--baseline', str(backup / 'schedulers-before.json'),
         '--wait-seconds', '25', '--output', str(backup / 'schedulers-after.json')], timeout=45)
    info['status'] = 'active'
    info['activated_at'] = datetime.now(timezone.utc).isoformat()
    info['triggers_after'] = {u: state(u) for u in TRIGGERS}
    info['original_pids_after'] = {u: state(u)['MainPID'] for u in info['original_pids']}
    save(info)
    emit('active', commit=info['commit'], backup=info['backup'], triggers=info['triggers_after'])


def rollback(info):
    """Keep claimed/unknown work intact. Only unclaimed offers can be parked."""
    backup = Path(info['backup'])
    if str(TAG.resolve()) != info['candidates']['tag'] or str(COORD.resolve()) != info['candidates']['coordinator']:
        raise RuntimeError('rollback_release_changed')
    for path, expected in info.get('applied_hashes', {}).items():
        if digest(path) != expected:
            raise RuntimeError('rollback_configuration_changed:' + path)
    cfg = json.loads(CONFIG.read_text())
    with closing(db(cfg['db_path'])) as c:
        rows = c.execute("SELECT id,status,generation FROM pipeline_job WHERE channel='x' AND CAST(source_id AS INTEGER)>=? AND (status IN ('running','unknown','legacy_running') OR (generation>0 AND status IN ('queued','ready')))", (ID_BASE,)).fetchall()
    if rows:
        raise RuntimeError('rollback_waits_for_claimed_or_unknown_tag_tasks:' + str(len(rows)))
    run(['systemctl', 'stop', *TRIGGERS])
    run(['systemctl', 'stop', '--no-block', 'x-tag-auto.service'])
    wait_idle(('x-tag-auto.service', *RUNNERS))
    # Park only offers that have never been claimed, in one claim-excluding transaction.
    with closing(sqlite3.connect(cfg['db_path'], timeout=10)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM pipeline_job WHERE channel='x' AND CAST(source_id AS INTEGER)>=? AND (status IN ('running','unknown','legacy_running') OR (generation>0 AND status IN ('queued','ready'))) LIMIT 1", (ID_BASE,)).fetchone():
            raise RuntimeError('rollback_claim_race;tag_triggers_parked')
        c.execute("UPDATE pipeline_job SET status='backlog_paused',hold_reason='x_tag_rollback' WHERE channel='x' AND CAST(source_id AS INTEGER)>=? AND generation=0 AND status IN ('queued','ready')", (ID_BASE,))
    def restore_coord():
        for path in (COORD_ENV, CONFIG, OVERRIDE):
            row = info['files'][str(path)]
            if row['before'] is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, Path(row['backup']).read_bytes(), row['mode'])
        link(COORD, info['roots']['coordinator'])
    switch_coordinator(info, restore_coord)
    run(['systemctl', 'stop', '--no-block', 'x-tag-post.service'])
    wait_idle(('x-tag-post.service',))
    row = info['files'][str(TAG_ENV)]
    atomic(TAG_ENV, Path(row['backup']).read_bytes(), row['mode'])
    link(TAG, info['roots']['tag'])
    run(['systemctl', 'start', 'x-tag-post.service']); health(8821)
    run(['systemctl', 'start', 'x-tag-auto.service']); health(18843)
    restore_triggers(info)
    info['status'] = 'rolled_back_code_business_facts_retained'; save(info)
    emit('rolled_back', backup=info['backup'], database_restore=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage', 'activate', 'rollback'))
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.action == 'stage':
        stage()
    else:
        path = Path(args.backup).resolve()
        path.relative_to(ROOT / 'deployments')
        data = json.loads((path / 'deployment.json').read_text())
        try:
            (activate if args.action == 'activate' else rollback)(data)
        except Exception as exc:
            data['last_error'] = str(exc); save(data)
            emit('needs_attention', phase=data['status'], error=str(exc), backup=str(path))
            raise

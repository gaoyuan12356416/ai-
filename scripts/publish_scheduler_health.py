"""Scoped systemd scheduling checks. Never publishes or changes business state.

snapshot/check are read-only. restore only starts daemons/timers which were
active in the supplied pre-deployment snapshot; it never runs a oneshot job.
"""
import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import sys


LANES = {
    'fb': {
        'daemons': ['fb-auto-post-service.service', 'fb-video-pool-scheduler.service',
                    'fb-group-pool-scheduler.service', 'fb-video-publish-worker.service'],
        'timers': {'fb-auto-post-scheduler.timer': 180, 'fb-auto-post-plan.timer': 300,
                   'fb-auto-post-reconcile.timer': 900}},
    'x': {
        'daemons': ['x-post-automation.service', 'x-auto-post-service.service', 'x-video-pool-scheduler.service'],
        'timers': {'x-post-schedule.timer': 180, 'x-post-schedule-claim.timer': 180,
                   'x-post-manual.timer': 120, 'x-auto-post-scheduler.timer': 180}},
    'tt': {
        'daemons': ['tt-post-service.service', 'tt-auto-post-service.service'],
        'timers': {'tt-auto-post-scheduler.timer': 180, 'tt-post-runner.timer': 180,
                   'tt-post-prepare.timer': 180}},
    'youtube': {'daemons': ['drama-material-api.service', 'youtube-auto-publish-worker.service'], 'timers': {}},
    'coordinator': {'daemons': ['pipeline-coordinator.service', 'pipeline-node-cpu-main.service'], 'timers': {}},
}
PROPERTIES = ['Id', 'LoadState', 'ActiveState', 'SubState', 'Type', 'MainPID', 'Result',
              'UnitFileState', 'NRestarts', 'ExecMainStatus', 'ExecMainExitTimestampMonotonic',
              'ActiveEnterTimestampMonotonic', 'NextElapseUSecRealtime', 'NextElapseUSecMonotonic']
PROPERTIES += ['RequiredBy', 'TriggeredBy']


def roles(lanes):
    result = {}
    for lane in lanes:
        for unit in LANES[lane]['daemons']:
            result[unit] = {'kind': 'daemon', 'lane': lane}
        for unit, age in LANES[lane]['timers'].items():
            result[unit] = {'kind': 'timer', 'lane': lane, 'max_age': age}
            result[unit[:-6] + '.service'] = {'kind': 'oneshot', 'lane': lane, 'max_age': age,
                                            'timer': unit}
    return result


def show(units):
    command = ['systemctl', 'show', *units, *['--property=' + p for p in PROPERTIES]]
    process = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if process.returncode:
        raise RuntimeError('systemctl_show_failed')
    result = {}
    for block in process.stdout.strip().split('\n\n'):
        row = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if row.get('Id'):
            result[row['Id']] = row
    return result


def evaluate(specs, states, now):
    checks = []
    for unit, spec in specs.items():
        row = states.get(unit, {})
        state, kind = row.get('ActiveState'), spec['kind']
        code, ok = 'ok', True
        if row.get('LoadState') != 'loaded':
            code, ok = 'unit_missing', False
        elif kind == 'daemon':
            if state != 'active' or row.get('SubState') != 'running' or int(row.get('MainPID') or 0) <= 0:
                code, ok = 'daemon_not_running', False
        elif kind == 'timer':
            next_real = row.get('NextElapseUSecRealtime', '')
            next_mono = row.get('NextElapseUSecMonotonic', '')
            if state != 'active':
                code, ok = 'timer_not_active', False
            elif row.get('SubState') != 'running' and next_real in ('', 'n/a') and next_mono in ('', '0', 'n/a'):
                code, ok = 'timer_without_next_trigger', False
        elif kind == 'path':
            if state != 'active':
                code, ok = 'path_not_active', False
        elif state in ('active', 'activating'):
            code = 'in_flight'
        elif state == 'failed' or row.get('Result') != 'success' or row.get('ExecMainStatus') != '0':
            code, ok = 'last_run_failed', False
        else:
            ended = int(row.get('ExecMainExitTimestampMonotonic') or 0) / 1000000
            if ended <= 0 or now - ended > spec['max_age']:
                code, ok = 'no_recent_success', False
            else:
                code = 'completed_waiting_next_trigger'
        checks.append({'unit': unit, 'lane': spec['lane'], 'kind': kind, 'ok': ok, 'code': code,
                       'active_state': state or 'unknown', 'sub_state': row.get('SubState', ''),
                       'main_pid': row.get('MainPID', ''), 'restarts': row.get('NRestarts', ''),
                       'next_trigger': row.get('NextElapseUSecRealtime', '')})
    return checks


def collect(lanes, previous=None):
    specs = roles(lanes)
    states = show(list(specs))
    checks = evaluate(specs, states, time.monotonic())
    result = {'schema_version': 2, 'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'lanes': lanes, 'ok': all(x['ok'] for x in checks), 'checks': checks, 'states': states}
    if 'fb' in lanes:
        if __package__:
            from .fb_short_link_health import collect_progress
        else:
            from fb_short_link_health import collect_progress
        result['fb_short_links'] = collect_progress(previous=(previous or {}).get('fb_short_links'))
        result['ok'] = result['ok'] and result['fb_short_links']['ok']
    return result


def restore_targets(snapshot):
    specs = snapshot.get('specs') or roles(snapshot['lanes'])
    # Parent daemons precede their helpers; timers are restored without starting
    # their target oneshot directly. Paused services stay paused.
    return [u for u, spec in specs.items() if spec['kind'] != 'oneshot'
            and snapshot['states'].get(u, {}).get('ActiveState') in ('active', 'activating')]


def deployment_snapshot(lanes):
    """Include actual dependent services and their timers/paths, not only a list of timers."""
    specs = roles(lanes)
    states = {}
    pending = list(specs)
    while pending:
        batch = show(pending)
        missing = set(pending) - set(batch)
        if missing:
            raise RuntimeError('deployment_snapshot_incomplete')
        states.update(batch)
        pending = []
        for unit, row in batch.items():
            if unit not in specs:
                kind = 'timer' if unit.endswith('.timer') else 'path' if unit.endswith('.path') else 'oneshot' if row.get('Type') == 'oneshot' else 'daemon'
                specs[unit] = {'kind': kind, 'lane': lanes[0]}
            for dependent in (row.get('RequiredBy', '') + ' ' + row.get('TriggeredBy', '')).split():
                if dependent.endswith(('.service', '.timer', '.path')) and dependent not in states and dependent not in pending:
                    pending.append(dependent)
    return {'schema_version': 2, 'lanes': lanes, 'states': states, 'specs': specs,
            'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}


def restore_snapshot(baseline, wait_seconds=25):
    targets = restore_targets(baseline)
    states = show(targets) if targets else {}
    started, errors = [], []
    for unit in targets:
        if states.get(unit, {}).get('ActiveState') in ('active', 'activating'):
            continue
        try:
            # Preserve explicitly inactive dependencies, including helpers that
            # a parent's Wants= would otherwise start during a deployment.
            reply = subprocess.run(['systemctl', '--job-mode=ignore-dependencies', 'start', unit],
                                   capture_output=True, timeout=30)
            if reply.returncode:
                errors.append(unit)
            else:
                started.append(unit)
        except (OSError, subprocess.TimeoutExpired):
            errors.append(unit)
    specs = baseline.get('specs') or roles(baseline['lanes'])
    deadline = time.monotonic() + wait_seconds
    while True:
        states = show(targets) if targets else {}
        checks = evaluate({u: specs[u] for u in targets}, states, time.monotonic())
        if all(x['ok'] for x in checks) or time.monotonic() >= deadline:
            break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return {'ok': not errors and all(x['ok'] for x in checks), 'lanes': baseline['lanes'],
            'started': started, 'restore_errors': errors, 'checks': checks,
            'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}


@contextlib.contextmanager
def deployment_guard(lanes, directory):
    directory = Path(directory)
    baseline = deployment_snapshot(lanes)
    save(directory / 'support-before.json', baseline)
    failed = False
    try:
        yield baseline
    except BaseException:
        failed = True
        raise
    finally:
        try:
            result = restore_snapshot(baseline)
        except Exception as exc:
            result = {'ok': False, 'restore_error': type(exc).__name__}
        result['deployment_failed'] = failed
        save(directory / 'support-restored.json', result)
        if not result['ok']:
            print('publish_support_restore_failed: ' + str(directory), file=sys.stderr)
            if not failed:
                raise RuntimeError('publish_support_restore_failed')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'snapshot', 'restore', 'run'])
    parser.add_argument('--lane', choices=[*LANES, 'all'], default='all')
    parser.add_argument('--baseline')
    parser.add_argument('--output')
    parser.add_argument('--wait-seconds', type=int, default=0)
    parser.add_argument('--audit-dir')
    # A command follows -- for the scoped deployment wrapper.
    argv = sys.argv[1:]
    command = argv[argv.index('--') + 1:] if '--' in argv else []
    if '--' in argv:
        argv = argv[:argv.index('--')]
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 45:
        parser.error('wait-seconds must be 0..45')
    lanes = list(LANES) if args.lane == 'all' else [args.lane]
    if args.action == 'run':
        if not command or not args.audit_dir or args.lane == 'all':
            parser.error('run requires a scoped lane, audit-dir and -- COMMAND')
        try:
            with deployment_guard(lanes, args.audit_dir):
                return subprocess.run(command, check=True).returncode
        except subprocess.CalledProcessError as exc:
            return exc.returncode
    if args.action == 'snapshot':
        result = deployment_snapshot(lanes)
        if not args.output:
            parser.error('snapshot requires output')
        save(args.output, result)
        print(json.dumps({'snapshot': args.output, 'units': len(result['states'])}))
        return 0
    errors = []
    if args.action == 'restore':
        if not args.baseline:
            parser.error('restore requires a saved pre-deployment baseline')
        baseline = json.loads(Path(args.baseline).read_text())
        result = restore_snapshot(baseline, args.wait_seconds)
        if args.output:
            save(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 1
    previous = None
    if args.output:
        try:
            previous = json.loads(Path(args.output).read_text())
        except (OSError, ValueError):
            pass
    deadline = time.monotonic() + args.wait_seconds
    while True:
        result = collect(lanes, previous)
        if result['ok'] or time.monotonic() >= deadline or args.action == 'snapshot':
            break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    if errors:
        result.update(ok=False, restore_errors=errors)
    if args.output:
        save(args.output, result)
    print(json.dumps({'ok': result['ok'], 'lanes': lanes, 'checks': len(result['checks']),
                      'problems': [x for x in result['checks'] if not x['ok']],
                      'fb_short_links': {k: v for k, v in result.get('fb_short_links', {}).items() if k != 'pending_since'},
                      'output': args.output, 'restore_errors': errors}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

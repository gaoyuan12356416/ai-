"""Scoped systemd scheduling checks. Never publishes or changes business state.

snapshot/check are read-only. restore only starts daemons/timers which were
active in the supplied pre-deployment snapshot; it never runs a oneshot job.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import time


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


def collect(lanes):
    specs = roles(lanes)
    states = show(list(specs))
    checks = evaluate(specs, states, time.monotonic())
    return {'schema_version': 1, 'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'lanes': lanes, 'ok': all(x['ok'] for x in checks), 'checks': checks, 'states': states}


def restore_targets(snapshot):
    specs = roles(snapshot['lanes'])
    # Parent daemons precede their helpers; timers are restored without starting
    # their target oneshot directly. Paused services stay paused.
    return [u for u, spec in specs.items() if spec['kind'] != 'oneshot'
            and snapshot['states'].get(u, {}).get('ActiveState') == 'active']


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'snapshot', 'restore'])
    parser.add_argument('--lane', choices=[*LANES, 'all'], default='all')
    parser.add_argument('--baseline')
    parser.add_argument('--output')
    parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 45:
        parser.error('wait-seconds must be 0..45')
    lanes = list(LANES) if args.lane == 'all' else [args.lane]
    errors = []
    if args.action == 'restore':
        if not args.baseline:
            parser.error('restore requires a saved pre-deployment baseline')
        baseline = json.loads(Path(args.baseline).read_text())
        lanes = baseline['lanes']
        for unit in restore_targets(baseline):
            result = subprocess.run(['systemctl', 'start', unit], capture_output=True, timeout=30)
            if result.returncode:
                errors.append(unit)
    deadline = time.monotonic() + args.wait_seconds
    while True:
        result = collect(lanes)
        if result['ok'] or time.monotonic() >= deadline or args.action == 'snapshot':
            break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    if errors:
        result.update(ok=False, restore_errors=errors)
    if args.output:
        save(args.output, result)
    print(json.dumps({'ok': result['ok'], 'lanes': lanes, 'checks': len(result['checks']),
                      'problems': [x for x in result['checks'] if not x['ok']],
                      'output': args.output, 'restore_errors': errors}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

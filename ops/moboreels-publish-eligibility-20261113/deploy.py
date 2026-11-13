"""GitHub-first code-only rollout. No ledger migration, replay or platform writes."""
import argparse
from contextlib import closing
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

OPS = 'ops/moboreels-publish-eligibility-20261113'
OLD = Path('/mnt/data-disk/moboreels-library/deployments/attribution-20261113T033327Z')
GROUPS = {'x': ['x'], 'fb': ['fb'], 'tt': ['tt', 'tt_auto'], 'youtube': ['main']}


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def config():
    with closing(sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro', uri=True, timeout=5)) as conn:
        version, raw = conn.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
    return dict(json.loads(raw), version=version)


def specs():
    original = json.loads((OLD / 'manifest.json').read_text())
    result = {}
    for group, lanes in GROUPS.items():
        checks, units, inactive = [], [], []
        for lane in lanes:
            spec = original['phases'][lane]['spec']
            # The legacy FB metric SQLite was independently migrated to MySQL.
            # This code-only release touches neither metric store nor schema;
            # all task and lease checks remain on their actual owner ledgers.
            checks += [row for row in spec['checks'] if row['name'] != 'metric_task_table_must_be_empty']
            units += spec.get('restart_services', [])
            inactive += spec.get('must_be_inactive', [])
        result[group] = {'checks': checks, 'restart_services': sorted(set(units)), 'must_be_inactive': sorted(set(inactive))}
    return result


def current_fences(d, state, group, installed=False):
    for lane in GROUPS[group]:
        root = Path(state['roots'][lane]['logical'])
        expected = state['candidates'][lane] if installed and lane != 'main' else state['roots'][lane]['resolved']
        d.require(str(root.resolve()) == expected, 'Runtime changed: ' + lane)
        if lane != 'main':
            wanted = state['candidate_trees'][lane] if installed else state['original_trees'][lane]
            d.require(d.tree_digest(expected) == wanted, 'Runtime tree changed: ' + lane)
    for row in state['files']:
        if row['lane'] in GROUPS[group]: d.fence(row, installed=installed)


def stage(d, release, sha):
    plan = json.loads((release / OPS / 'manifest.json').read_text())
    directory = d.DATA / 'deployments' / ('eligibility-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))
    d.require(not directory.exists(), 'Deployment exists')
    for lane, row in plan['roots'].items():
        d.require(str(Path(row['logical']).resolve()) == row['resolved'], 'Baseline pointer changed: ' + lane)
    files = []
    for row in plan['files']:
        targets = [Path(plan['roots'][row['lane']]['logical']) / row['relative']]
        if row['lane'] == 'main' and row['relative'].startswith('static/'):
            targets.append(Path('/usr/share/nginx/html') / Path(row['relative']).name)
        for target in targets:
            entry = dict(row, target=str(target), source=str(release / row['source']), phase=row['lane'])
            d.fence(entry)
            d.require(d.digest(entry['source']) == row['after'], 'Source hash changed')
            if target.suffix == '.py': compile(Path(entry['source']).read_bytes(), str(target), 'exec')
            entry['mode'] = target.stat().st_mode & 0o777 if target.exists() else 0o644
            files.append(entry)
    directory.mkdir(mode=0o700); (directory / 'private').mkdir(mode=0o700)
    state = {'commit': sha, 'release': str(release), 'roots': plan['roots'], 'files': files,
             'candidates': {}, 'original_trees': {}, 'candidate_trees': {}, 'groups': {}, 'specs': specs()}
    candidate_base = Path('/mnt/data-disk/moboreels-eligibility-releases') / sha
    candidate_base.mkdir(parents=True, mode=0o755, exist_ok=True)
    for lane in ('x', 'fb', 'tt', 'tt_auto'):
        old = Path(plan['roots'][lane]['resolved']); candidate = candidate_base / lane
        d.require(not candidate.exists(), 'Candidate already exists')
        before = d.tree_digest(old)
        shutil.copytree(old, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        os.chmod(candidate, 0o755)
        for entry in files:
            if entry['lane'] == lane: d.atomic_copy(entry['source'], candidate / entry['relative'], entry['mode'])
        d.require(d.tree_digest(old) == before, 'Runtime changed during copy')
        state['candidates'][lane] = str(candidate)
        state['original_trees'][lane] = before
        state['candidate_trees'][lane] = d.tree_digest(candidate)
    for index, entry in enumerate(files):
        if entry['before'] is not None:
            backup = directory / 'private' / ('%03d-' % index + Path(entry['relative']).name)
            shutil.copy2(entry['target'], backup); os.chmod(backup, 0o600)
            d.require(d.digest(backup) == entry['before'], 'Backup differs')
            entry['backup'] = str(backup)
    d.save(directory / 'manifest.json', state)
    print(json.dumps({'status': 'staged', 'deployment': str(directory), 'files': len(files)}))


def pause(d, lanes, directory, state, group):
    d.require(group not in state['groups'], 'Original group receipt already exists')
    current_fences(d, state, group)
    baseline = directory / 'private' / (group + '-scheduler-before.json')
    d.run(sys.executable, d.HEALTH, 'snapshot', '--lane', group, '--output', baseline)
    before = config()
    row = {'status': 'pausing', 'before': before, 'baseline': str(baseline), 'paused_by_us': group in before['enabled_channels']}
    state['groups'][group] = row; d.save(directory / 'manifest.json', state)
    if row['paused_by_us']:
        changed = lanes.set_lane_scoped(group, False, before['version'], True, directory / 'private' / (group + '-pause.json'))
        row['pause_version'] = changed['version']
    triggers = [name for name in state['specs'][group]['must_be_inactive'] if name.endswith(('.timer', '.path'))]
    if triggers: d.run('systemctl', 'stop', *triggers)
    row['status'] = 'draining'; d.save(directory / 'manifest.json', state)
    print(json.dumps({'group': group, 'status': 'draining', 'pause_version': row.get('pause_version')}))


def busy(d, directory, state, group):
    d.busy(state['specs'][group], directory, group)


def activate(d, directory, state, group):
    d.require(state['groups'][group]['status'] == 'draining', 'Group not draining')
    busy(d, directory, state, group)
    current_fences(d, state, group)
    state['groups'][group]['status'] = 'installing'; d.save(directory / 'manifest.json', state)
    for lane in GROUPS[group]:
        if lane == 'main':
            for entry in state['files']:
                if entry['lane'] == 'main': d.atomic_copy(entry['source'], entry['target'], entry['mode'])
        else:
            d.switch(state['roots'][lane]['logical'], state['candidates'][lane], state['roots'][lane]['resolved'])
    for unit in state['specs'][group]['restart_services']:
        d.run('systemctl', 'restart', unit); d.run('systemctl', 'is-active', unit)
    current_fences(d, state, group, installed=True)
    state['groups'][group]['status'] = 'activated'; d.save(directory / 'manifest.json', state)
    print(json.dumps({'group': group, 'status': 'activated'}))


def resume(d, lanes, directory, state, group, reviewed_version=None):
    row = state['groups'][group]
    d.require(row['status'] in ('activated', 'draining'), 'Resume only verified activated or unmodified draining group')
    d.run(sys.executable, d.HEALTH, 'restore', '--baseline', row['baseline'], '--wait-seconds', '25',
          '--output', directory / 'private' / (group + '-scheduler-after.json'))
    if row['paused_by_us']:
        lanes.reopen_original_lane(directory / 'private' / (group + '-pause.json'), directory / 'private' / (group + '-resume.json'), reviewed_current_version=reviewed_version)
    row['resumed'] = True; d.save(directory / 'manifest.json', state)
    print(json.dumps({'group': group, 'status': row['status'], 'resumed': True}))


def rollback_pause(d, lanes, directory, state, group):
    row = state['groups'][group]
    d.require(row['status'] == 'activated' and row.get('resumed'), 'Rollback-pause requires a resumed release')
    d.require('rollback' not in row, 'Reconcile existing rollback receipt')
    current_fences(d, state, group, installed=True)
    baseline = directory / 'private' / (group + '-rollback-scheduler-before.json')
    d.run(sys.executable, d.HEALTH, 'snapshot', '--lane', group, '--output', baseline)
    before = config()
    receipt = {'baseline': str(baseline), 'paused_by_us': group in before['enabled_channels']}
    row['rollback'] = receipt; d.save(directory / 'manifest.json', state)
    if receipt['paused_by_us']:
        lanes.set_lane_scoped(group, False, before['version'], True, directory / 'private' / (group + '-rollback-pause.json'))
    triggers = [name for name in state['specs'][group]['must_be_inactive'] if name.endswith(('.timer', '.path'))]
    if triggers: d.run('systemctl', 'stop', *triggers)
    receipt['status'] = 'draining'; d.save(directory / 'manifest.json', state)
    print(json.dumps({'group': group, 'status': 'rollback-draining'}))


def rollback(d, lanes, directory, state, group, reviewed_version=None):
    row = state['groups'][group]
    # Also supports a partial activation while its original deployment drain is still held.
    recovery = row.get('rollback') or row
    d.require(not recovery.get('resumed') and recovery.get('status') in ('draining', 'installing', 'activated'), 'Drain before rollback')
    d.require(group not in config()['enabled_channels'], 'Lane must remain drained')
    busy(d, directory, state, group)
    for lane in GROUPS[group]:
        if lane != 'main':
            current = str(Path(state['roots'][lane]['logical']).resolve())
            d.require(current in (state['candidates'][lane], state['roots'][lane]['resolved']), 'Unrelated deployment; stop rollback')
            d.require(d.tree_digest(state['roots'][lane]['resolved']) == state['original_trees'][lane], 'Original tree changed')
            d.require(d.tree_digest(state['candidates'][lane]) == state['candidate_trees'][lane], 'Candidate changed')
    for entry in state['files']:
        if entry['lane'] in GROUPS[group]:
            actual = d.digest(entry['target']) if Path(entry['target']).is_file() else None
            d.require(actual in (entry['before'], entry['after']), 'Unrelated file edit; stop rollback')
            if entry['before'] is not None: d.require(d.digest(entry['backup']) == entry['before'], 'Backup differs')
    for lane in GROUPS[group]:
        if lane != 'main':
            if str(Path(state['roots'][lane]['logical']).resolve()) == state['candidates'][lane]:
                d.switch(state['roots'][lane]['logical'], state['roots'][lane]['resolved'], state['candidates'][lane])
        else:
            for entry in state['files']:
                if entry['lane'] != 'main': continue
                if entry['before'] is None:
                    Path(entry['target']).unlink(missing_ok=True)
                else: d.atomic_copy(entry['backup'], entry['target'], entry['mode'])
    for unit in state['specs'][group]['restart_services']:
        d.run('systemctl', 'restart', unit); d.run('systemctl', 'is-active', unit)
    current_fences(d, state, group)
    d.run(sys.executable, d.HEALTH, 'restore', '--baseline', recovery['baseline'], '--wait-seconds', '25',
          '--output', directory / 'private' / (group + '-rollback-scheduler-after.json'))
    if recovery['paused_by_us']:
        suffix = '-rollback-pause.json' if row.get('rollback') else '-pause.json'
        lanes.reopen_original_lane(directory / 'private' / (group + suffix),
            directory / 'private' / (group + '-rollback-resume.json'), reviewed_current_version=reviewed_version)
    recovery.update(status='rolled_back', resumed=True); row['status'] = 'rolled_back'
    d.save(directory / 'manifest.json', state)
    print(json.dumps({'group': group, 'status': 'rolled_back'}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha'); parser.add_argument('action', choices=('stage', 'pause', 'busy', 'activate', 'resume', 'rollback-pause', 'rollback'))
    parser.add_argument('--deployment', type=Path); parser.add_argument('--group', choices=tuple(GROUPS))
    parser.add_argument('--reviewed-version', type=int)
    args = parser.parse_args(); release = Path(__file__).resolve().parents[2]
    d = module(release / 'ops/moboreels-attribution-20261113/deploy.py', 'original_deploy')
    d.verify(release, args.sha)
    if args.action == 'stage': return stage(d, release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory.parent == d.DATA / 'deployments' and directory.name.startswith('eligibility-'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['commit'] == args.sha and state['release'] == str(release), 'Deployment release differs')
    lanes = module(release / OPS / 'lanes.py', 'scoped_lanes')
    if args.action == 'pause': pause(d, lanes, directory, state, args.group)
    elif args.action == 'busy':
        busy(d, directory, state, args.group); print(json.dumps({'group': args.group, 'busy': False}))
    elif args.action == 'activate': activate(d, directory, state, args.group)
    elif args.action == 'resume': resume(d, lanes, directory, state, args.group, args.reviewed_version)
    elif args.action == 'rollback-pause': rollback_pause(d, lanes, directory, state, args.group)
    elif args.action == 'rollback': rollback(d, lanes, directory, state, args.group, args.reviewed_version)


if __name__ == '__main__': main()

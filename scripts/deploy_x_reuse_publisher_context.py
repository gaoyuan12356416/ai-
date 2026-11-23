"""Scoped GitHub-first code switch; recovery keeps original task and execution keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-reuse-read-context-20261123')
DEPLOY = ROOT/'deployment'
BASE = 'ea9cc3cf'
ROOTS = {'tag': Path('/opt/x-tag-publisher/current'), 'broker': Path('/opt/x-material-effects/current')}
FILES = {'tag': ['features/x_material_effects/reuse.py', 'features/x_accounts/oauth_service.py', 'features/x_posts/service.py'],
         'broker': ['features/x_material_effects/reuse.py']}
TAG_UNITS = ['x-tag-post.service', 'x-tag-auto.service', 'x-tag-auto-runner.timer',
             'x-tag-auto-scheduler.timer', 'x-tag-auto-runner.path', 'x-tag-manual.timer',
             'x-tag-pool.timer', 'x-tag-metric.timer']
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def states():
    result = {}
    for unit in TAG_UNITS:
        values = run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID',
                     '-p', 'Result', '-p', 'NextElapseUSecRealtime', '-p', 'ExecMainStartTimestamp')
        result[unit] = dict(line.split('=', 1) for line in values.splitlines())
    return result


def atomic(path, content, row):
    temporary = path.with_name(path.name+'.reuse-context-next')
    temporary.write_bytes(content)
    os.chown(temporary, row['uid'], row['gid'])
    os.chmod(temporary, row['mode'])
    os.replace(temporary, path)


def prepare():
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    scope = json.loads((ROOT/'recovery-scope.json').read_text())
    assert len(scope['tasks']) == 250
    DEPLOY.mkdir(mode=0o700)
    info = dict(commit=commit, source=str(SOURCE), roots={k: str(p.resolve()) for k, p in ROOTS.items()},
                units=states(), files=[], stage='prepared')
    for lane, names in FILES.items():
        for name in names:
            live = ROOTS[lane]/name
            before = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE+':'+name])
            assert live.read_bytes() == before, 'concurrent_live_change:'+str(live)
            content = (SOURCE/name).read_bytes()
            compile(content, name, 'exec')
            target = DEPLOY/'before'/lane/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, target)
            after = DEPLOY/'after'/lane/name
            after.parent.mkdir(parents=True, exist_ok=True)
            after.write_bytes(content)
            stat = live.stat()
            info['files'].append(dict(path=str(live), lane=lane, name=name, before=digest(live), after=digest(after),
                                      backup=str(target), next=str(after), uid=stat.st_uid, gid=stat.st_gid, mode=stat.st_mode & 0o777))
    run(sys.executable, HEALTH, 'snapshot', '--lane', 'x', '--output', DEPLOY/'schedulers-before.json')
    save(DEPLOY/'manifest.json', info)
    print(json.dumps(dict(stage='prepared', commit=commit, files=len(info['files']))))


def activate(rollback=False):
    info = json.loads((DEPLOY/'manifest.json').read_text())
    for lane, path in ROOTS.items():
        assert str(path.resolve()) == info['roots'][lane], 'runtime_moved:'+lane
    for row in info['files']:
        assert digest(row['path']) == row['after' if rollback else 'before'], 'concurrent_live_change:'+row['path']
    # Broker first: the new context action is additive and safe for old publishers.
    for lane in (('tag',) if rollback else ('broker', 'tag')):
        for row in info['files']:
            if row['lane'] == lane:
                atomic(Path(row['path']), Path(row['backup' if rollback else 'next']).read_bytes(), row)
        if lane == 'broker':
            run('systemctl', 'restart', 'x-material-effects-api.service')
    info['prior_pid'] = run('systemctl', 'show', 'x-tag-post.service', '-p', 'MainPID', '--value')
    info['stage'] = 'rollback_restart_pending' if rollback else 'restart_pending'
    save(DEPLOY/'manifest.json', info)
    run('systemctl', 'restart', '--no-block', 'x-tag-post.service')
    print(json.dumps(dict(stage=info['stage'], prior_pid=info['prior_pid'], preserved='all tasks, queues, grants and receipts')))


def finish():
    info = json.loads((DEPLOY/'manifest.json').read_text())
    current = states()['x-tag-post.service']
    assert current['ActiveState'] == 'active' and current['MainPID'] not in ('0', info['prior_pid']), 'publisher_still_draining'
    if info['stage'] == 'rollback_restart_pending':
        # Keep the additive context route until every new-code publisher has exited.
        for row in info['files']:
            if row['lane'] == 'broker':
                assert digest(row['path']) == row['after'], 'concurrent_broker_change'
                atomic(Path(row['path']), Path(row['backup']).read_bytes(), row)
        run('systemctl', 'restart', 'x-material-effects-api.service')
    run('systemctl', 'start', 'x-tag-auto.service')
    for unit, before in info['units'].items():
        if unit.endswith(('.timer', '.path')) and before['ActiveState'] == 'active':
            run('systemctl', 'start', unit)
    run(sys.executable, HEALTH, 'restore', '--baseline', DEPLOY/'schedulers-before.json',
        '--wait-seconds', '25', '--output', DEPLOY/'schedulers-after.json')
    after = states()
    assert after['x-tag-auto.service']['ActiveState'] == 'active'
    save(DEPLOY/'tag-schedulers-after.json', after)
    info['stage'] = 'active' if info['stage'] == 'restart_pending' else 'rolled_back'
    save(DEPLOY/'manifest.json', info)
    print(json.dumps(dict(stage=info['stage'], publisher_pid=current['MainPID'], selector_pid=after['x-tag-auto.service']['MainPID'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'activate', 'finish', 'rollback'])
    args = parser.parse_args()
    if args.action == 'prepare': prepare()
    elif args.action == 'finish': finish()
    else: activate(rollback=args.action == 'rollback')

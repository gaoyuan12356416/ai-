"""GitHub-first narrow TT/coordinator overlay, preserving all live modules."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/mnt/data-disk/tt-metric-wait-20261113')
GIT = Path('/mnt/data-disk/pipeline-dispatch/github')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
NATIVE = 'features/pipeline_dispatch/tt_native.py'
FILES = {'tt': [NATIVE], 'coordinator': [NATIVE, 'features/pipeline_dispatch/core.py', 'features/pipeline_dispatch/service.py']}
BASE = {NATIVE: '7e094aff2cc2259ec386fcb5b22b7db25d24fec72ff4bf75589710b7dbfc2e97',
        'features/pipeline_dispatch/core.py': '9332fee03e37256b6a43f144022088b4c2536208a51b55628a5f27b7f7b98363',
        'features/pipeline_dispatch/service.py': 'cad9384e42ad8a52baa5ae0f16349d73b2cdf19a1679391500120cf044440793'}
LINKS = {'tt': Path('/opt/tt-auto-post/current'), 'coordinator': Path('/opt/pipeline-dispatch/current')}
UNITS = {'tt': ('tt-auto-post-service.service', 18831), 'coordinator': ('pipeline-coordinator.service', 18846)}
REPORT_FILES = ['features/post_daily_report/' + name for name in ('plans.py', 'target_plans.py', 'x.py')]


def run(args, timeout=60, **kw):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
    if result.returncode:
        raise RuntimeError('command_failed:' + str(args[0]) + ':' + str(result.returncode) + ':' + result.stderr[-500:])
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def hashes(root):
    return {str(p.relative_to(root)): digest(p) for p in Path(root).rglob('*.py') if not p.is_symlink() and '__pycache__' not in p.parts}


def swap(link, target):
    next_link = link.with_name(link.name + '.tt-metric-next')
    assert not next_link.exists() and not next_link.is_symlink()
    next_link.symlink_to(target); os.replace(next_link, link)


def stage(commit, report_commit):
    assert run(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3
    folder = ROOT / ('deployment-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    folder.mkdir(parents=True, mode=0o700)
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    assert 'gaoyuan12356416/ai-' in run(['git', '-C', str(GIT), 'remote', 'get-url', 'origin'])
    for revision in (commit, report_commit):
        assert len(revision) == 40 and all(x in '0123456789abcdef' for x in revision)
        run(['git', '-C', str(GIT), 'fetch', 'origin', revision], env=env)
        assert run(['git', '-C', str(GIT), 'rev-parse', 'FETCH_HEAD']) == revision
    source = folder / 'source'; source.mkdir()
    archive = subprocess.check_output(['git', '-C', str(GIT), 'archive', commit])
    subprocess.run(['tar', '-x', '-C', str(source)], input=archive, check=True)
    report = folder / 'report-overlay'; report.mkdir()
    archive = subprocess.check_output(['git', '-C', str(GIT), 'archive', report_commit, *REPORT_FILES])
    subprocess.run(['tar', '-x', '-C', str(report)], input=archive, check=True)
    manifest = {'commit': commit, 'report_commit': report_commit, 'folder': str(folder), 'runtimes': {}, 'status': 'staged'}
    for lane, link in LINKS.items():
        original, candidate = link.resolve(), folder / lane
        for f in FILES[lane]:
            assert digest(original / f) == BASE[f], 'unreviewed_runtime:' + lane + ':' + f
        before = hashes(original)
        shutil.copytree(original, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        changed = FILES[lane] + (REPORT_FILES if lane == 'coordinator' else [])
        for f in FILES[lane]: shutil.copy2(source / f, candidate / f)
        if lane == 'coordinator':
            for f in REPORT_FILES: shutil.copy2(report / f, candidate / f)
        after = hashes(candidate)
        assert {k:v for k,v in before.items() if k not in changed} == {k:v for k,v in after.items() if k not in changed}
        interpreter = '/usr/bin/python3' if lane == 'tt' else '/opt/pipeline-dispatch/coordinator-venv/bin/python'
        run([interpreter, '-B', '-c', 'from features.pipeline_dispatch import tt_native, core, service'], cwd=candidate)
        if lane == 'coordinator':
            run([interpreter, '-B', '-c', 'from features.post_daily_report import plans, target_plans, x'], cwd=candidate)
        rollback = original
        if lane == 'coordinator':
            # Keep the separately authorized X report fix if only TT is rolled back.
            rollback = folder / 'coordinator-tt-rollback'
            shutil.copytree(candidate, rollback, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
            for f in FILES[lane]: shutil.copy2(original / f, rollback / f)
        manifest['runtimes'][lane] = {'old': str(original), 'new': str(candidate), 'rollback': str(rollback), 'before': before, 'after': after}
    path = folder / 'manifest.json'; write(path, manifest)
    (folder / 'rollback.sh').write_text('#!/bin/sh\nset -eu\nexec /usr/bin/python3 ' + str(source / 'scripts/deploy_tt_metric_wait.py') + ' rollback --manifest ' + str(path) + '\n')
    (folder / 'rollback.sh').chmod(0o700)
    print(json.dumps({'manifest': str(path), 'status': 'staged', 'commit': commit}))


def inflight(pid, port):
    rows = run(['ss', '-Hntp']).splitlines()
    active = sum(1 for row in rows if ('pid=' + pid + ',') in row and len(row.split()) > 4 and row.split()[3].endswith(':' + str(port)))
    rows = run(['ss', '-Hlntp']).splitlines()
    queued = sum(int(row.split()[1]) for row in rows if ('pid=' + pid + ',') in row and len(row.split()) > 4 and row.split()[3].endswith(':' + str(port)))
    return active, queued


def switch(lane, target, folder):
    unit, port = UNITS[lane]; link = LINKS[lane]; previous = link.resolve()
    pid = run(['systemctl', 'show', unit, '-p', 'MainPID', '--value']); assert pid != '0'
    marker = 'tt-metric-' + lane + '-' + str(int(time.time()))
    rule = ['-p', 'tcp', '--dport', str(port), '-m', 'conntrack', '--ctstate', 'NEW', '-m', 'comment', '--comment', marker, '-j', 'DROP']
    run(['systemd-run', '--quiet', '--unit='+marker, '--on-active=100s', '--', '/usr/sbin/iptables', '-D', 'INPUT', *rule])
    gated = stopped = changed = False
    try:
        run(['iptables', '-I', 'INPUT', '1', *rule]); gated = True
        deadline = time.monotonic() + 25
        while inflight(pid, port) != (0,0):
            if time.monotonic() > deadline: raise RuntimeError('http_drain_timeout:' + lane)
            time.sleep(.3)
        assert link.resolve() == previous, 'concurrent_runtime_switch'
        run(['systemctl', 'stop', unit], timeout=40); stopped = True
        assert run(['systemctl', 'show', unit, '-p', 'MainPID', '--value']) == '0'
        swap(link, target); changed = True
        run(['systemctl', 'start', unit], timeout=20)
        assert run(['systemctl', 'is-active', unit]) == 'active'
    except BaseException:
        if changed:
            run(['systemctl', 'stop', unit], timeout=40); swap(link, previous)
        if stopped: run(['systemctl', 'start', unit], timeout=20)
        raise
    finally:
        if gated: run(['iptables', '-D', 'INPUT', *rule])
        subprocess.run(['systemctl', 'stop', marker+'.timer'], capture_output=True)
        write(folder / ('switch-'+lane+'.json'), {'old': str(previous), 'new': str(link.resolve()), 'rule_removed': marker not in run(['iptables','-S','INPUT'])})


def activate(path, rollback=False):
    manifest = json.loads(Path(path).read_text()); folder = Path(manifest['folder'])
    for lane, data in manifest['runtimes'].items():
        assert str(LINKS[lane].resolve()) == data['new' if rollback else 'old'], 'runtime_pointer_changed'
        assert hashes(LINKS[lane].resolve()) == data['after' if rollback else 'before'], 'runtime_source_changed'
        run(['/usr/bin/python3', HEALTH, 'snapshot', '--lane', lane, '--output', str(folder / ('schedulers-before-'+lane+'.json'))])
    for lane in ('tt', 'coordinator'):
        switch(lane, manifest['runtimes'][lane]['rollback' if rollback else 'new'], folder)
        run(['/usr/bin/python3', HEALTH, 'restore', '--baseline', str(folder / ('schedulers-before-'+lane+'.json')), '--wait-seconds', '25', '--output', str(folder / ('schedulers-after-'+lane+'.json'))], timeout=45)
    manifest['status'] = 'rolled_back' if rollback else 'active'; write(path, manifest)
    print(json.dumps({'status': manifest['status'], 'manifest': path, 'commit': manifest['commit']}))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('action', choices=['stage','activate','rollback']);p.add_argument('--commit');p.add_argument('--report-commit');p.add_argument('--manifest');a=p.parse_args()
    stage(a.commit,a.report_commit) if a.action=='stage' else activate(a.manifest,a.action=='rollback')

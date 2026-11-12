"""GitHub-first TT report overlay. Stage separately; never send a report."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

from deploy_post_monitor_accounting import run, digest, swap, health, service_pids

ROOT = Path('/mnt/data-disk/tt-report-target-plans-20261112')
GIT = Path('/mnt/data-disk/pipeline-dispatch/github')
LINKS = {'report': Path('/opt/post-daily-report/current'),
         'coordinator': Path('/opt/pipeline-dispatch/current')}
UNIT = 'pipeline-coordinator.service'
SCHEDULERS = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
FILES = tuple('features/post_daily_report/' + name for name in ('tt.py', 'plans.py', 'target_plans.py'))
BASELINE = {
    'report': {'tt.py': '3a6f92c751e6106c7a988958ed0ae345a17048cb7e1068830de7d13e10738700',
               'plans.py': '0eec26eee5545eea6b60d711212ac9fab66e61e20efa86c18537c0dc8a37a0a6'},
    'coordinator': {'tt.py': '3a6f92c751e6106c7a988958ed0ae345a17048cb7e1068830de7d13e10738700',
                    'plans.py': 'e08c7a846a4106bd275df391e79b309c97a16984376426c55d92cb2eea99de4d'},
}


def check_disk():
    assert run(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert os.access('/mnt/data-disk', os.W_OK), 'data_disk_not_writable'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024 ** 3, 'data_disk_space_low'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def hashes(root):
    return {name: digest(root / name) if (root / name).exists() else None for name in FILES}


def verify_expected(root, expected):
    assert hashes(root) == expected, 'affected_files_changed:' + str(root)


def source_hashes(root):
    """Include unchanged modules so staging cannot silently revert live overlays."""
    return {str(path.relative_to(root)): digest(path) for path in root.rglob('*.py')
            if not path.is_symlink() and '__pycache__' not in path.parts}


@contextmanager
def report_lock():
    with Path('/mnt/data-disk/post-daily-report/run.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(['systemctl', 'show', 'post-daily-report.service', '-p', 'ActiveState', '--value']) == 'inactive', 'report_running'
        yield


def stage(commit):
    assert re.fullmatch('[0-9a-f]{40}', commit or ''), 'full_commit_required'
    check_disk()
    assert all(link.is_symlink() for link in LINKS.values()), 'release_symlink_required'
    old = {name: link.resolve() for name, link in LINKS.items()}
    for name, root in old.items():
        for leaf, expected in BASELINE[name].items():
            assert digest(root / 'features/post_daily_report' / leaf) == expected, 'unreviewed_baseline:' + name + ':' + leaf
        assert not (root / FILES[2]).exists(), 'target_plan_fix_already_present'
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    remote = run(['git', '-C', str(GIT), 'remote', 'get-url', 'origin'])
    remote = remote[:-4] if remote.endswith('.git') else remote
    assert remote in (
        'git@github.com:gaoyuan12356416/ai-', 'https://github.com/gaoyuan12356416/ai-'), 'unexpected_git_remote'
    run(['git', '-C', str(GIT), 'fetch', 'origin', commit], env=env)
    assert run(['git', '-C', str(GIT), 'rev-parse', 'FETCH_HEAD']) == commit
    folder = ROOT / ('deployment-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True, mode=0o700)
    source = folder / 'source'
    source.mkdir()
    archive = subprocess.run(['git', '-C', str(GIT), 'archive', commit], capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(source)], input=archive, check=True)
    manifest = {'commit': commit, 'source': str(source), 'runtimes': {}, 'status': 'staged'}
    for name, original in old.items():
        candidate = folder / name
        before = source_hashes(original)
        shutil.copytree(original, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        for relative in FILES:
            target = candidate / relative
            assert not target.is_symlink() and all(not p.is_symlink() for p in target.parents if p != candidate.parent), 'symlink_overlay_rejected'
            shutil.copy2(source / relative, target)
        after = source_hashes(candidate)
        assert {k: v for k, v in before.items() if k not in FILES} == {k: v for k, v in after.items() if k not in FILES}, 'unrelated_source_changed'
        manifest['runtimes'][name] = {'old': str(original), 'new': str(candidate),
                                      'old_hashes': hashes(original), 'new_hashes': hashes(candidate),
                                      'source_hashes': before}
        interpreter = '/opt/pipeline-dispatch/coordinator-venv/bin/python' if name == 'coordinator' else '/usr/bin/python3'
        run([interpreter, '-B', '-c', 'from features.post_daily_report import tt, plans, target_plans'], cwd=candidate)
    path = folder / 'manifest.json'
    write_json(path, manifest)
    rollback_command = '/usr/bin/python3 ' + shlex.quote(str(source / 'scripts/deploy_tt_report_target_plans.py')) + ' rollback --manifest ' + shlex.quote(str(path))
    (folder / 'rollback.sh').write_text('#!/bin/sh\nset -eu\nexec ' + rollback_command + '\n')
    (folder / 'rollback.sh').chmod(0o700)
    print(json.dumps({'status': 'staged', 'commit': commit, 'manifest': str(path),
                      'report': manifest['runtimes']['report']['new'],
                      'coordinator': manifest['runtimes']['coordinator']['new']}))


def http_inflight(pid):
    rows = run(['ss', '-Hntp']).splitlines()
    active = sum(1 for row in rows if ('pid=' + pid + ',') in row
                 and len(row.split()) > 4 and row.split()[3].endswith(':18846'))
    listeners = run(['ss', '-Hlntp']).splitlines()
    queued = sum(int(row.split()[1]) for row in listeners if ('pid=' + pid + ',') in row
                 and len(row.split()) > 4 and row.split()[3].endswith(':18846'))
    return active, queued


def switch_coordinator(target, record):
    """Drain only coordinator HTTP; background collector has graceful SIGTERM."""
    link = LINKS['coordinator']
    previous = str(link.resolve())
    pid = run(['systemctl', 'show', UNIT, '-p', 'MainPID', '--value'])
    assert pid != '0', 'coordinator_not_running'
    marker = 'tt-report-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S%f')
    rule = ['-p', 'tcp', '--dport', '18846', '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'DROP']
    watchdog = marker + '-watchdog'
    gated = stopped = changed = False
    observations = []
    started = time.monotonic()
    run(['systemd-run', '--quiet', '--unit=' + watchdog, '--on-active=100s', '--', '/usr/sbin/iptables', '-D', 'INPUT'] + rule)
    try:
        run(['iptables', '-I', 'INPUT', '1'] + rule)
        gated = True
        deadline = time.monotonic() + 25
        while True:
            state = http_inflight(pid)
            observations.append(state)
            if state == (0, 0):
                time.sleep(.5)
                if http_inflight(pid) == (0, 0):
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError('coordinator_http_not_drained')
            time.sleep(.5)
        assert str(link.resolve()) == previous, 'concurrent_coordinator_switch'
        run(['systemctl', 'stop', UNIT], timeout=50)
        stopped = True
        assert run(['systemctl', 'show', UNIT, '-p', 'MainPID', '--value']) == '0'
        assert str(link.resolve()) == previous, 'concurrent_coordinator_switch'
        swap(link, target)
        changed = True
        run(['systemctl', 'start', UNIT], timeout=20)
        assert run(['systemctl', 'is-active', UNIT]) == 'active'
    except BaseException:
        if changed:
            run(['systemctl', 'stop', UNIT], timeout=50)
            assert str(link.resolve()) == str(target), 'later_coordinator_switch_preserved'
            swap(link, previous)
        if stopped:
            run(['systemctl', 'start', UNIT], timeout=20)
        raise
    finally:
        if gated:
            # The watchdog may already have removed the exact rule on a slow
            # startup failure; absence is the desired final state in either case.
            subprocess.run(['iptables', '-D', 'INPUT'] + rule, capture_output=True, timeout=10)
        subprocess.run(['systemctl', 'stop', watchdog + '.timer'], capture_output=True, timeout=10)
        removed = marker not in run(['iptables', '-S', 'INPUT'])
        write_json(record, {'observations': observations, 'seconds': time.monotonic() - started,
                            'rule_removed': removed})
        assert removed, 'coordinator_http_gate_not_removed'


def scheduler_snapshot(folder, prefix):
    run(['/usr/bin/python3', SCHEDULERS, 'check', '--lane', 'coordinator', '--output', str(folder / (prefix + '-health.json'))])
    baseline = folder / (prefix + '-schedulers.json')
    run(['/usr/bin/python3', SCHEDULERS, 'snapshot', '--lane', 'coordinator', '--output', str(baseline)])
    return baseline


def restore_schedulers(baseline, output):
    run(['/usr/bin/python3', SCHEDULERS, 'restore', '--baseline', str(baseline),
         '--wait-seconds', '25', '--output', str(output)], timeout=45)


def load_manifest(path):
    path = path.resolve()
    assert ROOT in path.parents and path.name == 'manifest.json', 'unexpected_manifest_path'
    return json.loads(path.read_text()), path.parent


def activate(path):
    check_disk()
    manifest, folder = load_manifest(path)
    with report_lock():
        for name, entry in manifest['runtimes'].items():
            assert str(LINKS[name].resolve()) == entry['old'], 'concurrent_release:' + name
            verify_expected(Path(entry['old']), entry['old_hashes'])
            verify_expected(Path(entry['new']), entry['new_hashes'])
            assert source_hashes(Path(entry['old'])) == entry['source_hashes'], 'live_source_changed:' + name
        baseline = scheduler_snapshot(folder, 'before')
        before = service_pids()
        try:
            switch_coordinator(manifest['runtimes']['coordinator']['new'], folder / 'activate-drain.json')
            health()
            restore_schedulers(baseline, folder / 'schedulers-after.json')
            assert str(LINKS['report'].resolve()) == manifest['runtimes']['report']['old']
            swap(LINKS['report'], manifest['runtimes']['report']['new'])
        except BaseException:
            for name in ('report', 'coordinator'):
                entry = manifest['runtimes'][name]
                if str(LINKS[name].resolve()) == entry['new']:
                    if name == 'coordinator':
                        switch_coordinator(entry['old'], folder / 'failed-activate-drain.json')
                        health()
                    else:
                        swap(LINKS[name], entry['old'])
            restore_schedulers(baseline, folder / 'failed-activate-schedulers.json')
            raise
        after = service_pids()
        result = {'status': 'activated', 'commit': manifest['commit'], 'manifest': str(path),
                  'service_pids_before': before, 'service_pids_after': after,
                  'report_timer': run(['systemctl', 'is-active', 'post-daily-report.timer']),
                  'next_report': run(['systemctl', 'show', 'post-daily-report.timer', '-p', 'NextElapseUSecRealtime', '--value']),
                  'rollback': str(folder / 'rollback.sh')}
        write_json(folder / 'activation.json', result)
        print(json.dumps(result))


def rollback(path):
    check_disk()
    manifest, folder = load_manifest(path)
    with report_lock():
        for name, entry in manifest['runtimes'].items():
            assert str(LINKS[name].resolve()) in (entry['old'], entry['new']), 'later_release_preserved:' + name
            verify_expected(Path(entry['old']), entry['old_hashes'])
        coordinator = manifest['runtimes']['coordinator']
        if str(LINKS['coordinator'].resolve()) == coordinator['new']:
            baseline = scheduler_snapshot(folder, 'rollback-before')
            switch_coordinator(coordinator['old'], folder / 'rollback-drain.json')
            health()
            restore_schedulers(baseline, folder / 'rollback-schedulers-after.json')
        report = manifest['runtimes']['report']
        if str(LINKS['report'].resolve()) == report['new']:
            swap(LINKS['report'], report['old'])
        print(json.dumps({'status': 'rolled_back', 'manifest': str(path)}))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'activate', 'rollback'))
    parser.add_argument('--commit')
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    if args.action == 'stage':
        if not args.commit:
            parser.error('stage requires --commit')
        stage(args.commit)
    else:
        if args.manifest is None:
            parser.error(args.action + ' requires --manifest')
        (activate if args.action == 'activate' else rollback)(args.manifest)

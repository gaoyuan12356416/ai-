"""Exact GitHub commit switch after draining coordinator HTTP handlers only."""
import contextlib
import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import requests

SHA = ''
OLD = ''
ROOT = pathlib.Path('/mnt/data-disk/pipeline-dispatch')
REPO = ROOT / 'github'
DB = ROOT / 'dispatch.sqlite3'
PYTHON = '/opt/pipeline-dispatch/coordinator-venv/bin/python'
SERVICE = 'pipeline-coordinator.service'


def run(args, timeout=60, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError('command_failed:' + args[0] + ':' + str(result.returncode))
    return result.stdout.strip()


def configuration():
    with contextlib.closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        return dict(json.loads(row[1]), version=row[0])


def handlers_drained(pid):
    tasks = list(pathlib.Path('/proc/' + pid + '/task').iterdir())
    # main + collector; every HTTP handler has its own additional thread.
    sockets = [r for r in run(['ss', '-Hntp']).splitlines() if ('pid=' + pid + ',') in r]
    listeners = [r for r in run(['ss', '-Hlntp']).splitlines() if ('pid=' + pid + ',') in r]
    queued = sum(int(r.split()[1]) for r in listeners)
    return {'threads': len(tasks), 'connected_sockets': len(sockets), 'accept_queue': queued}


def main():
    global SHA, OLD
    assert len(sys.argv) == 3, 'usage: deploy_pipeline_coordinator_guarded.py TARGET_SHA EXPECTED_CURRENT_SHA'
    SHA, OLD = sys.argv[1:]
    assert all(re.fullmatch('[a-f0-9]{40}', value) for value in (SHA, OLD))
    assert ROOT.parent.is_mount()
    assert not run(['git', '-C', str(REPO), 'status', '--porcelain'])
    assert run(['git', '-C', str(REPO), 'rev-parse', 'HEAD']) == OLD
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    run(['git', '-C', str(REPO), 'fetch', '--depth=100', 'origin', SHA], env=env)
    assert run(['git', '-C', str(REPO), 'rev-parse', 'FETCH_HEAD']) == SHA
    tag = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / ('backup-coordinator-' + tag)
    backup.mkdir(mode=0o700)
    checked = backup / 'source-check'
    run(['git', '-C', str(REPO), 'worktree', 'add', '--detach', str(checked), SHA])
    modules = ['scripts.test_pipeline_youtube_owned_resume', 'scripts.test_pipeline_youtube_canonical_service',
               'scripts.test_pipeline_backlog_native', 'scripts.test_pipeline_backlog_projection',
               'scripts.test_pipeline_backlog_release', 'scripts.test_pipeline_backlog_dispose',
               'scripts.test_pipeline_x_storage_initialization', 'scripts.test_pipeline_capacity_modes',
               'scripts.test_pipeline_failed_receipt_ack', 'scripts.test_pipeline_capacity_samples',
               'scripts.test_pipeline_node_capability_policy']
    tests = subprocess.run([sys.executable, '-m', 'unittest', *modules], cwd=checked,
                           capture_output=True, text=True, timeout=60)
    (backup / 'targeted-tests.log').write_text(tests.stdout + tests.stderr)
    assert tests.returncode == 0, 'linux_targeted_tests_failed'
    run([PYTHON, '-m', 'compileall', '-q', 'features/pipeline_dispatch'], cwd=checked)
    for filename in ('/etc/pipeline-coordinator.json', '/etc/systemd/system/pipeline-coordinator.service'):
        destination = backup / pathlib.Path(filename).name
        shutil.copy2(filename, destination)
        destination.chmod(0o600)
    with contextlib.closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as source:
        with contextlib.closing(sqlite3.connect(backup / 'dispatch-before.sqlite3')) as destination:
            source.backup(destination)
            assert destination.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    (backup / 'dispatch-before.sqlite3').chmod(0o600)
    before = configuration()
    pid = run(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value'])
    main_pid = run(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'])
    marker = 'codex-pipeline-coordinator-' + tag
    rule = ['-p', 'tcp', '--dport', '18846', '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'DROP']
    watchdog = 'pipeline-coordinator-gate-' + tag.lower()
    # A fail-safe removes our own narrowly scoped rule if this process is lost.
    run(['systemd-run', '--quiet', '--unit=' + watchdog, '--on-active=75s', '--',
         '/usr/sbin/iptables', '-D', 'INPUT'] + rule)
    gated = False
    changed = False
    evidence = []
    began = time.monotonic()
    try:
        run(['iptables', '-I', 'INPUT', '1'] + rule)
        gated = True
        deadline = time.monotonic() + 25
        while True:
            item = handlers_drained(pid)
            evidence.append(item)
            if item == {'threads': 2, 'connected_sockets': 0, 'accept_queue': 0}:
                time.sleep(.25)
                second = handlers_drained(pid)
                evidence.append(second)
                if second == item:
                    break
            assert time.monotonic() < deadline, 'coordinator_control_requests_not_drained'
            time.sleep(.5)
        (backup / 'http-drain-proof.json').write_text(json.dumps(evidence, indent=2))
        assert run(['git', '-C', str(REPO), 'rev-parse', 'HEAD']) == OLD, 'release_changed_during_preflight'
        assert run(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value']) == pid, 'coordinator_changed_during_drain'
        run(['systemctl', 'stop', SERVICE], timeout=30)
        assert run(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value']) == '0'
        run(['git', '-C', str(REPO), 'checkout', '--detach', SHA], timeout=10)
        changed = True
        run(['systemctl', 'start', SERVICE], timeout=15)
        assert run(['systemctl', 'is-active', SERVICE]) == 'active'
    except BaseException:
        # Roll back code only. All current leases, publication facts and config stay.
        if changed:
            run(['systemctl', 'stop', SERVICE], timeout=30)
            run(['git', '-C', str(REPO), 'checkout', '--detach', OLD], timeout=10)
        run(['systemctl', 'start', SERVICE], timeout=15)
        raise
    finally:
        if gated:
            run(['iptables', '-D', 'INPUT'] + rule)
        subprocess.run(['systemctl', 'stop', watchdog + '.timer'], capture_output=True, timeout=10)
    elapsed = round(time.monotonic() - began, 3)
    (backup / 'gate-result.json').write_text(json.dumps({'gate_seconds': elapsed, 'gate_removed': True}))
    with requests.Session() as http:
        http.trust_env = False
        deadline = time.monotonic() + 10
        while True:
            try:
                health = http.get('http://127.0.0.1:18846/health', timeout=2)
                if health.status_code == 200:
                    break
            except requests.RequestException:
                pass
            assert time.monotonic() < deadline, 'coordinator_health_not_ready'
            time.sleep(.25)
        assert health.status_code == 200
    after = configuration()
    report = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'previous': OLD, 'commit': SHA, 'backup': str(backup), 'linux_targeted_test_modules': modules,
              'http_drain': evidence, 'gate_seconds': elapsed,
              'gate_removed': marker not in run(['iptables', '-S', 'INPUT']),
              'health_status': health.status_code, 'before': before, 'after': after,
              'old_pid': pid, 'new_pid': run(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value']),
              'main_api_pid_unchanged': main_pid == run(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'])}
    (backup / 'deployment-result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()

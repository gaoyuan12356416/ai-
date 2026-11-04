"""Deploy monitor-only files from an exact GitHub commit; preserve live overlays.

Default dry run. HTTP gate drains coordinator handlers, not business workers.
Rollback restores only these files; never restores the live database/config.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import types
import urllib.request

ROOT = Path('/mnt/data-disk/pipeline-dispatch')
REPO = ROOT / 'github'
DB = ROOT / 'dispatch.sqlite3'
SERVICE = 'pipeline-coordinator.service'
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
MODULES = ('features/pipeline_dispatch/queue_states.py', 'features/pipeline_dispatch/views.py')
ASSETS = ('pipeline-monitor.js', 'pipeline-monitor.css', 'pipeline-monitor.html')


def run(args, timeout=30, **kwargs):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if p.returncode: raise RuntimeError('command_failed:' + args[0] + ':' + str(p.returncode))
    return p.stdout.strip()


def digest(data): return hashlib.sha256(data).hexdigest()


def pid(service): return run(['systemctl', 'show', service, '-p', 'MainPID', '--value'])


def config_fingerprint():
    with contextlib.closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as c:
        row = c.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
    return {'version': row[0], 'sha256': digest(row[1].encode())}


def drain_state(process):
    sockets = [s for s in run(['ss', '-Hntp']).splitlines() if ('pid=' + process + ',') in s]
    listeners = [s for s in run(['ss', '-Hlntp']).splitlines() if ('pid=' + process + ',') in s]
    return [len(list(Path('/proc/' + process + '/task').iterdir())), len(sockets), sum(int(s.split()[1]) for s in listeners)]


def atomic(path, data):
    if data is None:
        path.unlink(missing_ok=True)
        return
    temp = path.with_name(path.name + '.queue-states-tmp')
    with temp.open('xb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.chmod(temp, path.stat().st_mode if path.exists() else 0o644)
    os.replace(temp, path)


def restart_with_files(replacements, originals, runtime, tag):
    old_pid = pid(SERVICE)
    marker = 'queue-states-' + tag
    rule = ['-p', 'tcp', '--dport', '18846', '-m', 'conntrack', '--ctstate', 'NEW', '-m', 'comment', '--comment', marker, '-j', 'DROP']
    timer = 'queue-states-gate-' + tag.lower()
    run(['systemd-run', '--quiet', '--unit=' + timer, '--on-active=75s', '--', '/usr/sbin/iptables', '-D', 'INPUT'] + rule)
    gated = stopped = False
    proof = []
    began = time.monotonic()
    try:
        run(['iptables', '-I', 'INPUT', '1'] + rule); gated = True
        deadline = time.monotonic() + 25
        while True:
            state = drain_state(old_pid); proof.append(state)
            if state == [2, 0, 0]:
                time.sleep(.25)
                state = drain_state(old_pid); proof.append(state)
                if state == [2, 0, 0]: break
            if time.monotonic() >= deadline: raise RuntimeError('http_handlers_not_drained')
            time.sleep(.5)
        # Prevent replacing another release that arrived during the drain.
        assert Path('/opt/pipeline-dispatch/current').resolve() == runtime
        for path, data in originals.items():
            assert (path.read_bytes() if path.exists() else None) == data, 'live_files_changed_during_drain'
        run(['systemctl', 'stop', SERVICE], timeout=20); stopped = True
        assert pid(SERVICE) == '0'
        for path, data in replacements.items(): atomic(path, data)
        # Remove only these modules' stale bytecode, not any other runtime file.
        for name in MODULES:
            for cached in (runtime / name).parent.joinpath('__pycache__').glob(Path(name).stem + '.*.pyc'):
                cached.unlink()
        run(['systemctl', 'start', SERVICE], timeout=15)
        assert run(['systemctl', 'is-active', SERVICE]) == 'active'
    except BaseException:
        if stopped:
            run(['systemctl', 'stop', SERVICE], timeout=20)
            for path, data in originals.items(): atomic(path, data)
            for name in MODULES:
                for cached in (runtime / name).parent.joinpath('__pycache__').glob(Path(name).stem + '.*.pyc'): cached.unlink()
            run(['systemctl', 'start', SERVICE], timeout=15)
        raise
    finally:
        if gated: run(['iptables', '-D', 'INPUT'] + rule)
        subprocess.run(['systemctl', 'stop', timer + '.timer'], capture_output=True, timeout=10)
    deadline = time.monotonic() + 10
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:18846/health', timeout=2) as response:
                assert response.status == 200
            break
        except Exception:
            if time.monotonic() >= deadline: raise
            time.sleep(.25)
    return {'http_drain': proof, 'gate_seconds': round(time.monotonic()-began, 3), 'old_pid': old_pid, 'new_pid': pid(SERVICE),
            'gate_removed': marker not in run(['iptables', '-S', 'INPUT']), 'health_status': 200}


def shadow_check(runtime, replacement):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime))
    for name in MODULES:
        module_name = name[:-3].replace('/', '.')
        if module_name.endswith('.views'): module_name = 'features.pipeline_dispatch.queue_states_shadow_views'
        m = types.ModuleType(module_name); m.__package__ = 'features.pipeline_dispatch'
        sys.modules[module_name] = m
        exec(compile(replacement[runtime / name], name, 'exec'), m.__dict__)
    class ReadStore:
        path = DB
        def clock(self): return dt.datetime.now(dt.timezone.utc)
        def connect(self):
            c = sqlite3.connect(DB.as_uri()+'?mode=ro', uri=True, timeout=5)
            c.row_factory = sqlite3.Row; c.execute('PRAGMA query_only=ON'); return c
    actor = {'is_admin': True, 'user_id': 'admin', 'allowed_channels': ['x','fb','tt','youtube'], 'allowed_modules': []}
    began = time.monotonic()
    result = m.query(ReadStore(), 'summary', actor, {})
    assert result['queue_state_version'] == 1
    return {'seconds': round(time.monotonic()-began,3), 'totals': result['totals']}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sha'); p.add_argument('--expected-sha'); p.add_argument('--rollback'); p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    assert ROOT.parent.is_mount() and run(['findmnt', '-n', '-o', 'UUID', '--target', str(ROOT.parent)]) == UUID
    runtime = Path('/opt/pipeline-dispatch/current').resolve()
    assert str(runtime).startswith('/mnt/data-disk/pipeline-dispatch-runtime/')
    replacements = {}; expected = {}; report = {'apply': args.apply, 'runtime': str(runtime)}
    if args.rollback:
        backup = Path(args.rollback).resolve()
        assert backup.parent == ROOT and backup.name.startswith('queue-states-')
        manifest = json.loads((backup / 'manifest.json').read_text())
        assert manifest['runtime'] == str(runtime)
        for row in manifest['files']:
            path = Path(row['path']); current = path.read_bytes() if path.exists() else None
            assert current is not None and digest(current) == row['deployed_sha256'], 'rollback_live_file_changed'
            expected[path] = current
            replacements[path] = (backup / row['backup']).read_bytes() if row['existed'] else None
        report['rollback'] = str(backup)
    else:
        assert all(re.fullmatch('[0-9a-f]{40}', s or '') for s in (args.sha, args.expected_sha))
        env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
        for sha in (args.sha, args.expected_sha): run(['git','-C',str(REPO),'fetch','origin',sha],timeout=120,env=env)
        def content(sha, name): return subprocess.check_output(['git','-C',str(REPO),'show',sha+':'+name])
        targets = [(runtime/name,name) for name in MODULES] + [(root/name,'static/'+name) for root in (Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')) for name in ASSETS]
        for path, name in targets:
            replacements[path] = content(args.sha, name)
            expected[path] = None if name == MODULES[0] else content(args.expected_sha, name)
            assert (path.read_bytes() if path.exists() else None) == expected[path], 'unexpected_live_file:'+str(path)
        report.update(sha=args.sha, expected_sha=args.expected_sha, shadow=shadow_check(runtime,replacements))
    if not args.apply:
        print(json.dumps(report)); return
    tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / ('queue-states-' + tag); backup.mkdir(mode=0o700)
    shutil.copy2(__file__, backup/'deploy.py')
    files = []
    for i,(path,data) in enumerate(expected.items()):
        name = 'original-'+str(i)
        if data is not None: (backup/name).write_bytes(data)
        files.append({'path':str(path),'backup':name,'existed':data is not None,'deployed_sha256':digest(replacements[path]) if replacements[path] is not None else None})
    (backup/'manifest.json').write_text(json.dumps({'runtime':str(runtime),'files':files},indent=2))
    services = ('drama-material-api.service','pipeline-node-cpu-main.service')
    unchanged_pids = {s:pid(s) for s in services}
    before = config_fingerprint()
    other_hashes = {p:digest((runtime/p).read_bytes()) for p in ('features/pipeline_dispatch/core.py','features/pipeline_dispatch/service.py')}
    report.update(restart_with_files(replacements,expected,runtime,tag))
    report.update(backup=str(backup),configuration_before=before,configuration_after=config_fingerprint(),
                  unrelated_pids_unchanged={s:pid(s)==v for s,v in unchanged_pids.items()},
                  unrelated_runtime_unchanged=all(digest((runtime/p).read_bytes())==h for p,h in other_hashes.items()))
    for name in ASSETS:
        request=urllib.request.Request('https://ai.yingliangads.com/'+name+'?queue_states='+tag,headers={'Cache-Control':'no-cache'})
        with urllib.request.urlopen(request,timeout=20) as response:
            assert response.read()==replacements[Path('/usr/share/nginx/html')/name], 'public_static_mismatch'
    report['public_static_verified']=True
    (backup/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__ == '__main__': main()

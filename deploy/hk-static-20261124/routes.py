#!/usr/bin/env python3
"""Publish or withdraw the two owned nginx includes without restarting nginx."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from nginx_config import render


BASE = Path('/var/www/ai-hk-static')
HTTP = Path('/etc/nginx/conf.d/ai-hk-static-canary.conf')
SERVER = Path('/etc/nginx/default.d/ai-hk-static-canary.conf')
STATE = BASE / 'state' / 'route.json'


def replace(path, data):
    if data is None:
        if path.exists():
            path.unlink()
        return
    with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.hk-static-', delete=False) as tmp:
        tmp.write(data)
        os.chmod(tmp.name, 0o644)
    os.replace(tmp.name, str(path))


def fingerprint(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def installed(path):
    return path.read_bytes() if path.exists() else None


def run(*cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-1500:] or 'command failed')
    return result.stdout


def check_unrelated_configs():
    """Graceful reload also reads other includes. Detect edits since the active workers started."""
    master = int(run('systemctl', 'show', 'nginx', '-p', 'MainPID', '--value').strip())
    pids = Path('/proc/%s/task/%s/children' % (master, master)).read_text().split()
    starts = []
    for pid in pids:
        proc = Path('/proc') / pid
        try:
            cmd = (proc / 'cmdline').read_bytes()
            if b'worker process' in cmd and b'shutting down' not in cmd:
                starts.append(float((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]))
        except FileNotFoundError:
            continue
    if not starts:
        raise RuntimeError('no active nginx workers found')
    boot = int(re.search(r'^btime (\d+)$', Path('/proc/stat').read_text(), re.M)[1])
    started = boot + min(starts) / os.sysconf('SC_CLK_TCK')
    dump = run('nginx', '-T').decode('utf-8', 'replace')
    newer = [name for name in re.findall(r'^# configuration file (/[^\n]+):$', dump, re.M)
             if Path(name) not in (HTTP, SERVER) and Path(name).stat().st_mtime > started + 1]
    if newer:
        raise RuntimeError('another nginx edit is pending: ' + ', '.join(newer))


def configure(stage):
    if stage == 'suspend':
        (STATE.parent / 'suspended').touch()
        print(json.dumps({'suspended': True}))
        return
    root = BASE / 'current'
    manifest = json.loads((root / 'manifest.json').read_text())
    prior = json.loads(STATE.read_text()) if STATE.exists() else {}
    before = {HTTP: installed(HTTP), SERVER: installed(SERVER)}
    if any(fingerprint(value) != prior.get('files', {}).get(str(path)) for path, value in before.items()):
        raise ValueError('an include changed outside this release; preserve it')
    if stage == 'check':
        run('nginx', '-t')
        print(json.dumps({'ownership': 'ok', 'stage': prior.get('stage', 'off'),
                          'suspended': (STATE.parent / 'suspended').exists()}))
        return
    if prior.get('stage', 'off') == stage:
        print(json.dumps({'stage': stage, 'unchanged': True}))
        return
    check_unrelated_configs()
    if stage == 'off':
        desired = {SERVER: None, HTTP: None}
    else:
        http, server = render(manifest, int(stage))
        desired = {HTTP: http.encode(), SERVER: server.encode()}
    previous_info = installed(STATE)
    try:
        for path, value in desired.items():
            if before[path] != value:
                replace(path, value)
        run('nginx', '-t')
        record = {'stage': stage, 'release': root.resolve().name,
                  'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'files': {str(path): fingerprint(value) for path, value in desired.items()}}
        run('systemctl', 'reload', 'nginx')
        replace(STATE, json.dumps(record, indent=2).encode())
    except Exception:
        for path, value in before.items():
            replace(path, value)
        run('nginx', '-t')
        # The previously active production configuration owns all unrelated services.
        run('systemctl', 'reload', 'nginx')
        replace(STATE, previous_info)
        raise
    print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=('check', 'suspend', 'off', '0', '10', '50', '100'))
    args = parser.parse_args()
    STATE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (STATE.parent / 'route.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        configure(args.stage)

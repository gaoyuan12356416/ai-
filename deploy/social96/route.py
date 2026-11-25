"""Gracefully switch only the two named Nginx paths. Keep the live gateway as backup."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROUTES = Path('/etc/nginx/default.d/ai-backend-offload.conf')
UPSTREAM = Path('/etc/nginx/conf.d/social96-report-read.conf')
STATE = Path('/mnt/data-disk/gpu-backend-offload-20261123/social96')
OLD = b'proxy_pass http://127.0.0.1:19964;'
NEW = b'proxy_pass http://social96_report_read;\n    proxy_next_upstream error;\n    proxy_next_upstream_tries 2;'
POOL = b'upstream social96_report_read {\n    server 127.0.0.1:19990 max_fails=1 fail_timeout=5s;\n    server 127.0.0.1:19964 backup;\n}\n'
PATHS = (b'/api/social-analytics/report', b'/api/social-analytics/export.csv')

def transform(content, enable):
    old, new = (OLD, NEW) if enable else (NEW, OLD)
    for path in PATHS:
        regex = rb'(location\s+=\s+' + re.escape(path) + rb'\s*\{)([^{}]*)(\})'
        found = list(re.finditer(regex, content))
        if len(found) != 1 or found[0][2].count(old) != 1 or found[0][2].count(b'proxy_pass ') != 1:
            raise ValueError('live route changed: ' + path.decode())
        first, last = found[0].span(2)
        content = content[:first] + found[0][2].replace(old, new) + content[last:]
    return content

def atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.social96-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if path.exists():
            s = path.stat()
            os.chown(tmp, s.st_uid, s.st_gid)
            os.chmod(tmp, s.st_mode & 0o777)
            for key in os.listxattr(str(path)):
                value = os.getxattr(str(path), key)
                if key not in os.listxattr(tmp) or os.getxattr(tmp, key) != value:
                    os.setxattr(tmp, key, value)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def verify():
    subprocess.run(['nginx', '-t'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('check', 'enable', 'rollback-check', 'rollback'))
    action = p.parse_args().action
    enable = action in ('enable', 'check')
    with (STATE / 'nginx-change.lock').open('ab') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        orig = ROUTES.read_bytes()
        planned = transform(orig, enable)
        assert transform(planned, not enable) == orig
        if not enable or UPSTREAM.exists():
            assert UPSTREAM.is_file() and UPSTREAM.read_bytes() == POOL
        if action in ('check', 'rollback-check'):
            verify()
            print(json.dumps({'action': action, 'paths': len(PATHS)}))
            return
        if enable:
            subprocess.run(['systemctl','is-active','--quiet','ai-backend-social96-compute'],check=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = STATE / ('nginx-before-' + stamp)
        backup.write_bytes(orig); backup.chmod(0o600)
        existed = UPSTREAM.exists()
        if not existed: atomic(UPSTREAM, POOL)
        installed = False
        try:
            assert ROUTES.read_bytes() == orig, 'another deployment edited the routes'
            atomic(ROUTES, planned); installed = True
            verify()
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True, timeout=20)
        except BaseException:
            if installed and ROUTES.read_bytes() == planned:
                atomic(ROUTES, orig)
                verify()
                subprocess.run(['systemctl','reload','nginx'],check=True,timeout=20)
            if not existed and UPSTREAM.read_bytes() == POOL: UPSTREAM.unlink()
            raise
        print(json.dumps({'action': action, 'paths': len(PATHS), 'backup': str(backup), 'at': stamp}))

if __name__ == '__main__':
    main()

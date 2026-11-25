"""Apply the report's gzip settings to the exact read locations in the offload gateway."""
import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROUTES = Path('/etc/nginx/default.d/ai-backend-offload.conf')
SETTINGS = b'    gzip on;\n    gzip_types application/json text/csv;\n    gzip_min_length 512;\n    gzip_comp_level 5;\n    gzip_vary on;\n    gzip_proxied any;\n'


def transform(data, enable):
    for name in ('options', 'report', 'export.csv'):
        start = ('location = /api/social-analytics/' + name + ' {\n').encode()
        if data.count(start) != 1:
            raise ValueError('unexpected gateway location: ' + name)
        offset = data.index(start) + len(start)
        block = data[offset:data.index(b'}', offset)]
        if enable:
            if b'gzip' in block:
                raise ValueError('compression already configured: ' + name)
            data = data[:offset] + SETTINGS + data[offset:]
        else:
            if not block.startswith(SETTINGS):
                raise ValueError('compression configuration changed: ' + name)
            data = data[:offset] + data[offset + len(SETTINGS):]
    return data


def replace(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
    try:
        shutil.copy2(path, tmp)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'enable', 'rollback'))
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    old = ROUTES.read_bytes()
    enable = args.action != 'rollback'
    new = transform(old, enable)
    if transform(new, not enable) != old:
        raise ValueError('gateway configuration cannot be restored')
    if args.action == 'check':
        print('three report locations verified')
    else:
        if enable:
            with args.backup.open('xb') as target:
                target.write(old)
        if ROUTES.read_bytes() != old:
            raise RuntimeError('concurrent gateway edit')
        replace(ROUTES, new)
        try:
            subprocess.run(['nginx', '-t'], check=True)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        except Exception:
            if ROUTES.read_bytes() == new:
                replace(ROUTES, old)
                subprocess.run(['nginx', '-t'], check=True)
                subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            raise

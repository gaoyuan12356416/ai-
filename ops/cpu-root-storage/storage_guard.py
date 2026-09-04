#!/usr/bin/env python3
"""Fail closed before attribution Python/SQLite can use a root-backed temp dir."""
import json
import os
import subprocess
import tempfile

ROOT = '/mnt/data-disk'
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
TEMP = ROOT + '/dramawave-attribution-comparison/tmp'


def verify(path=TEMP, minimum=3 * 1024 ** 3):
    if not os.path.ismount(ROOT):
        raise RuntimeError('data disk is not mounted')
    actual = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '-T', path], timeout=10).decode().strip()
    if actual != UUID or os.stat(path).st_dev == os.stat('/').st_dev:
        raise RuntimeError('unexpected filesystem for data path')
    if os.path.commonpath([os.path.realpath(path), ROOT]) != ROOT:
        raise RuntimeError('data path escapes data disk')
    s = os.statvfs(path)
    if s.f_bavail * s.f_frsize < minimum:
        raise RuntimeError('insufficient data disk free space')
    fd, name = tempfile.mkstemp(prefix='.storage-probe-', dir=path)
    try:
        os.write(fd, b'ok')
        os.fsync(fd)
    finally:
        os.close(fd)
        os.unlink(name)


if __name__ == '__main__':
    verify()
    print(json.dumps({'ok': True, 'temporary_directory': TEMP}))

#!/usr/bin/env python3
"""Fail closed unless CPU media paths resolve onto the approved data disk."""
import argparse
import os
from pathlib import Path
import subprocess

DISK = Path('/mnt/data-disk')
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


def check_mount(paths=()):
    row = subprocess.check_output(
        ['findmnt', '-rn', '-o', 'TARGET,UUID,OPTIONS', '-T', str(DISK)], text=True
    ).strip().split()
    if len(row) != 3 or row[0] != str(DISK) or row[1] != UUID or 'rw' not in row[2].split(','):
        raise RuntimeError('approved CPU data disk is not mounted read-write')
    if not os.path.ismount(str(DISK)) or not os.access(str(DISK), os.W_OK):
        raise RuntimeError('CPU data disk is not a writable mount point')
    for value in paths:
        path = Path(value)
        real = path.resolve(strict=True)
        if real != DISK and DISK not in real.parents:
            raise RuntimeError('media path is not on CPU data disk: %s' % path)
        if os.stat(str(real)).st_dev != os.stat(str(DISK)).st_dev:
            raise RuntimeError('media path has an unexpected mounted device: %s' % path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', action='append', default=[])
    check_mount(parser.parse_args().path)

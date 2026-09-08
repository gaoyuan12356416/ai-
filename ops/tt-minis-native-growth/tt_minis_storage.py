#!/usr/bin/env python3
"""Fail-closed CPU-host TT Minis storage and consistent analysis snapshots."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

MOUNT = Path('/mnt/data-disk')
EXPECTED_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
STORAGE = MOUNT / 'tt-minis-storage'
CACHE_DB = MOUNT / 'projects/codex_test/data/tt_minis_multi_dim_dashboard_tti_app_revenue_cache.sqlite3'


def prepare_storage(cache_path=CACHE_DB, reserve_bytes=1024**3):
    actual = subprocess.check_output(
        ['findmnt', '-no', 'UUID', '-T', str(MOUNT)], timeout=5
    ).decode().strip()
    if not os.path.ismount(str(MOUNT)) or actual != EXPECTED_UUID:
        raise RuntimeError('TT Minis data disk missing or UUID mismatch; root fallback forbidden')
    if not os.access(str(MOUNT), os.W_OK):
        raise RuntimeError('TT Minis data disk is not writable')
    if shutil.disk_usage(str(MOUNT)).free < reserve_bytes:
        raise RuntimeError('TT Minis data disk has insufficient reserved free space')
    resolved = Path(cache_path).resolve()
    if MOUNT not in resolved.parents:
        raise RuntimeError('TT Minis database must resolve onto the data disk')
    # Check the nearest existing ancestor as nested mounts can escape the device.
    ancestor = resolved
    while not ancestor.exists():
        ancestor = ancestor.parent
    if ancestor.stat().st_dev != MOUNT.stat().st_dev:
        raise RuntimeError('TT Minis database device mismatch')
    tmp = STORAGE / 'tmp'
    tmp.mkdir(parents=True, exist_ok=True)
    if tmp.resolve().stat().st_dev != MOUNT.stat().st_dev:
        raise RuntimeError('TT Minis temporary directory device mismatch')
    os.environ['TMPDIR'] = str(tmp)
    os.environ['SQLITE_TMPDIR'] = str(tmp)
    tempfile.tempdir = str(tmp)
    return resolved


def snapshot():
    source = prepare_storage()
    if not source.is_file():
        raise RuntimeError('Primary cache missing; do not create an empty database')
    prepare_storage(reserve_bytes=source.stat().st_size + 2 * 1024**3)
    directory = STORAGE / 'analysis-snapshots'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('tt_minis_cache_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8] + '.sqlite3')
    partial = target.with_suffix('.partial')
    prepare_storage(partial)
    # Online SQLite backup is consistent while the dashboard continues writing.
    # Use the CLI so the helper also works on the host's Python 3.6.
    subprocess.run(['sqlite3', '-readonly', str(source), '.timeout 5000',
                    ".backup '" + str(partial) + "'"], check=True, timeout=180)
    with sqlite3.connect('file:' + str(partial) + '?mode=ro', uri=True) as conn:
        result = conn.execute('PRAGMA quick_check(1)').fetchone()[0]
    if result != 'ok':
        raise RuntimeError('Snapshot validation failed; partial preserved: ' + str(partial))
    os.replace(str(partial), str(target))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'snapshot', 'exec'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    path = prepare_storage()
    if args.action == 'snapshot':
        print(snapshot())
    elif args.action == 'exec':
        command = args.command
        if command and command[0] == '--':
            command = command[1:]
        if not command:
            parser.error('exec requires a command')
        os.execvp(command[0], command)
    else:
        print(json.dumps({'ok': True, 'cache': str(path), 'tmp': os.environ['TMPDIR']}))


if __name__ == '__main__':
    main()

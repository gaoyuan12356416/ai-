#!/usr/bin/env python3
"""Explicit allowlist, copy/hash/recheck, atomic exchange; never move live DBs."""
import argparse
import ctypes
import datetime
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from storage_guard import ROOT, verify

BASE = ROOT + '/root-storage-20260904'
COLD = [
    '/tmp/tt_minis_cache_weekly_20260814.sqlite3',
    '/root/.cache/whisper',
    '/root/drama_material_service/backups',
    '/root/backups/drama_material_service',
]


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def manifest(path):
    p = Path(os.path.realpath(path))
    items = [p] + sorted(p.rglob('*')) if p.is_dir() else [p]
    result = {}
    for f in items:
        s = f.lstat()
        key = str(f.relative_to(p)) if f != p else '.'
        entry = [s.st_mode, s.st_uid, s.st_gid, s.st_mtime_ns]
        if stat.S_ISLNK(s.st_mode):
            entry += ['link', os.readlink(str(f))]
        elif stat.S_ISREG(s.st_mode):
            entry += ['file', s.st_size, digest(str(f))]
        elif stat.S_ISDIR(s.st_mode):
            entry += ['directory']
        else:
            raise RuntimeError('special file not allowed: ' + str(f))
        result[key] = entry
    return result


def open_users(source):
    found = []
    for p in glob.glob('/proc/[0-9]*'):
        if int(p.rsplit('/', 1)[1]) == os.getpid():
            continue
        try:
            candidates = []
            for f in glob.glob(p + '/fd/*') + [p + '/cwd']:
                try:
                    candidates.append(os.readlink(f))
                except OSError:
                    pass
            with open(p + '/maps') as f:
                candidates += [l.split()[-1] for l in f if '/' in l]
            if any(s == source or s.startswith(source + '/') for s in candidates):
                found.append(int(p.rsplit('/', 1)[1]))
        except FileNotFoundError:
            pass
        except PermissionError:
            raise RuntimeError('cannot inspect active file handles')
    return found


def exchange(a, b):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if fn(-100, os.fsencode(a), -100, os.fsencode(b), 2) != 0:
        raise OSError(ctypes.get_errno(), 'atomic path exchange failed')


def receipt(path, value):
    temp = path + '.new'
    with open(temp, 'w') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def run(source, apply):
    destination = BASE + '/data' + source
    if os.path.islink(source):
        if os.path.realpath(source) != destination:
            raise RuntimeError('unexpected existing symlink: ' + source)
        print(json.dumps({'source': source, 'status': 'already_migrated'}), flush=True)
        return
    if not os.path.exists(source):
        raise RuntimeError('missing source: ' + source)
    users = open_users(source)
    before = manifest(source)
    if users:
        raise RuntimeError('source in use by pids: ' + str(users))
    if max(v[3] for v in before.values()) > (time.time() - 86400) * 10 ** 9:
        raise RuntimeError('source changed within 24 hours: ' + source)
    size = sum(v[5] for v in before.values() if v[4] == 'file')
    print(json.dumps({'source': source, 'bytes': size, 'entries': len(before), 'apply': apply}), flush=True)
    if not apply:
        return
    verify(ROOT, max(5 * 1024 ** 3, size * 2))
    os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
    if os.path.lexists(destination):
        raise RuntimeError('destination exists, inspect before resuming: ' + destination)
    subprocess.check_call(['rsync', '-aHAX', '--numeric-ids', source, os.path.dirname(destination) + '/'])
    copied = manifest(destination)
    if copied != before or manifest(source) != before or open_users(source):
        raise RuntimeError('source changed or copy verification failed')
    if source.endswith('.sqlite3'):
        conn = sqlite3.connect('file:' + destination + '?mode=ro&immutable=1', uri=True)
        try:
            if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('SQLite quick_check failed')
        finally:
            conn.close()
    audit = BASE + '/audit/' + source.strip('/').replace('/', '_') + '.json'
    os.makedirs(os.path.dirname(audit), mode=0o700, exist_ok=True)
    record = {'source': source, 'destination': destination, 'bytes': size,
              'manifest': before, 'time': datetime.datetime.now().isoformat(), 'phase': 'verified_copy'}
    receipt(audit, record)
    staged = source + '.migration-swap-20260904'
    if os.path.lexists(staged):
        raise RuntimeError('staging path exists; manual recovery required')
    os.symlink(destination, staged)
    exchange(source, staged)
    record['phase'] = 'exchanged'
    receipt(audit, record)
    if manifest(staged) != before or manifest(source) != before or open_users(staged):
        exchange(source, staged)
        os.unlink(staged)
        raise RuntimeError('post-switch validation failed; original path restored')
    # Exact allowlisted original has been copied and hashed twice. Data remains
    # complete at destination; removing only this exchanged copy frees root.
    assert source in COLD or source.startswith('/tmp/feishu-images/')
    assert staged == source + '.migration-swap-20260904'
    assert not os.path.islink(staged)
    if os.path.isdir(staged):
        shutil.rmtree(staged)
    else:
        os.unlink(staged)
    record['phase'] = 'complete'
    receipt(audit, record)
    print(json.dumps({'source': source, 'status': 'complete', 'bytes_freed': size, 'audit': audit}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--attachments', action='store_true')
    args = parser.parse_args()
    verify(ROOT)
    import fcntl
    lock = open('/run/lock/cpu-root-storage.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.attachments:
        # Freeze only historical regular files, never the active directory or
        # today's downloads. Keep current bot tasks and new attachments intact.
        sources = [p for p in sorted(glob.glob('/tmp/feishu-images/*'))
                   if not os.path.islink(p) and os.path.isfile(p)
                   and os.stat(p).st_mtime < time.time() - 7 * 86400]
    else:
        sources = COLD
    for source in sources:
        run(source, args.apply)


if __name__ == '__main__':
    main()

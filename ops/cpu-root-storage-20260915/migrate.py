#!/usr/bin/env python3
"""Audited, operator-quiesced CPU storage migration. Python 3.6 compatible."""
import argparse
import ctypes
import datetime
import fcntl
import glob
import hashlib
import json
import os
import posixpath
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time

BASE = '/mnt/data-disk/root-storage-20260915'
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
SOURCES = (
    '/usr/share/nginx/html/reports/ai-game-performance',
    '/usr/share/nginx/html/drama-materials.pre-gpu-service-migration-20260828T1502',
    '/usr/share/nginx/html/drama-screenshot-materials.pre-gpu-service-migration-20260828T1502',
    '/root/.local', '/root/.codex', '/root/miniconda3', '/root/codex_test',
    '/root/drama_material_service', '/root/drama_material_service_test',
    '/opt', '/var/lib/mysql', '/var/lib/docker',
    '/usr/share/nginx/html/ad-materials', '/usr/share/nginx/html/codex-downloads',
    '/usr/share/nginx/html/produced-ad-videos', '/root/ffmpeg-static',
)


def emit(**value):
    print(json.dumps(value, sort_keys=True), flush=True)


def stamp():
    return datetime.datetime.now().isoformat()


def write_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = path + '.new'
    with open(temp, 'w') as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def verify_disk(minimum=5 * 1024 ** 3):
    root = '/mnt/data-disk'
    if not os.path.ismount(root):
        raise RuntimeError('data disk is not mounted')
    actual = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '-T', root], timeout=10).decode().strip()
    if actual != UUID or os.stat(root).st_dev == os.stat('/').st_dev:
        raise RuntimeError('wrong backing filesystem')
    v = os.statvfs(root)
    if v.f_bavail * v.f_frsize < minimum:
        raise RuntimeError('insufficient available space')
    fd, p = tempfile.mkstemp(prefix='.root-storage-probe-', dir=root)
    try:
        os.write(fd, b'ok')
        os.fsync(fd)
    finally:
        os.close(fd)
        os.unlink(p)


def paths(source):
    if source not in SOURCES or posixpath.normpath(source) != source:
        raise RuntimeError('source is outside the exact allowlist')
    destination = BASE + '/rootfs' + source
    original = source + '.root-storage-20260915-original'
    backup = BASE + '/backups' + source
    audit = BASE + '/audit/' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.json'
    return destination, original, backup, audit


def parents(destination, source):
    os.makedirs(BASE, mode=0o755, exist_ok=True)
    # Mirror ancestor traversal permissions, including mysql/nginx access.
    parts = source.strip('/').split('/')[:-1]
    root = destination[:-len(source)]
    os.makedirs(root, mode=0o700 if root.endswith('/backups') else 0o755, exist_ok=True)
    if root.endswith('/backups'):
        os.chmod(root, 0o700)
    for i in range(1, len(parts) + 1):
        src = '/' + '/'.join(parts[:i])
        dst = root + src
        if not os.path.lexists(dst):
            os.mkdir(dst, stat.S_IMODE(os.stat(src).st_mode))
            os.chown(dst, os.stat(src).st_uid, os.stat(src).st_gid)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def manifest(source):
    result = {}
    cache = {}
    allow_devices = source.endswith('/var/lib/docker') or source.endswith('/var/lib/docker.root-storage-20260915-original')
    walk = [source]
    if os.path.isdir(source) and not os.path.islink(source):
        for root, dirs, files in os.walk(source, followlinks=False):
            walk.extend(os.path.join(root, n) for n in dirs + files)
    for path in sorted(walk):
        s = os.lstat(path)
        key = os.path.relpath(path, source)
        item = {'mode': s.st_mode, 'uid': s.st_uid, 'gid': s.st_gid, 'mtime_ns': s.st_mtime_ns}
        if stat.S_ISLNK(s.st_mode):
            item['link'] = os.readlink(path)
        elif stat.S_ISREG(s.st_mode):
            inode = (s.st_dev, s.st_ino)
            if inode not in cache:
                cache[inode] = digest(path)
            item.update(size=s.st_size, sha256=cache[inode])
        elif stat.S_ISDIR(s.st_mode):
            pass
        elif stat.S_ISSOCK(s.st_mode) or stat.S_ISFIFO(s.st_mode):
            item['special'] = True
        elif allow_devices and (stat.S_ISCHR(s.st_mode) or stat.S_ISBLK(s.st_mode)):
            # Container rootfs/overlay entries include device and whiteout nodes.
            # Compare metadata only: never open a device node to hash its contents.
            item['device'] = [os.major(s.st_rdev), os.minor(s.st_rdev)]
        else:
            raise RuntimeError('unsupported device file: ' + path)
        result[key] = item
    return result


def users(source):
    found = []
    for proc in glob.glob('/proc/[0-9]*'):
        pid = int(proc.rsplit('/', 1)[1])
        if pid == os.getpid():
            continue
        hits = []
        try:
            comm = open(proc + '/comm').read().strip()
            for kind, path in [('cwd', proc + '/cwd'), ('exe', proc + '/exe')] + [('fd', f) for f in glob.glob(proc + '/fd/*')]:
                try:
                    target = os.readlink(path)
                    if target == source or target.startswith(source + '/'):
                        hits.append(kind)
                except FileNotFoundError:
                    pass
            with open(proc + '/maps') as f:
                if any(source + '/' in line for line in f):
                    hits.append('mapped')
            if hits:
                found.append({'pid': pid, 'comm': comm, 'references': sorted(set(hits))})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def sync(source, destination, delete=False, presync=False):
    cmd = ['rsync', '-aHAX', '--numeric-ids', '--one-file-system']
    if delete:
        cmd.append('--delete')
    cmd += [source.rstrip('/') + '/', destination.rstrip('/') + '/']
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    if p.returncode not in ([0, 24] if presync else [0]):
        raise RuntimeError('rsync failed {}: {}'.format(p.returncode, p.stderr.decode(errors='replace')[:2000]))


def sqlite_checks(source):
    results = []
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            p = os.path.join(root, name)
            if os.path.islink(p) or not os.path.isfile(p) or not name.endswith(('.sqlite', '.sqlite3', '.db')):
                continue
            with open(p, 'rb') as f:
                if f.read(16) != b'SQLite format 3\x00':
                    continue
            conn = sqlite3.connect('file:' + p + '?mode=ro', uri=True, timeout=5)
            started = time.time()
            conn.set_progress_handler(lambda: int(time.time() - started > 90), 10000)
            try:
                rows = conn.execute('PRAGMA quick_check(3)').fetchall()
                if rows != [('ok',)]:
                    raise RuntimeError('SQLite integrity failed: ' + p + ' ' + repr(rows))
                results.append({'path': os.path.relpath(p, source), 'quick_check': 'ok'})
            finally:
                conn.close()
    return results


def exchange(a, b):
    fn = ctypes.CDLL(None, use_errno=True).renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if fn(-100, os.fsencode(a), -100, os.fsencode(b), 2):
        raise OSError(ctypes.get_errno(), 'atomic exchange failed')


def operation(source, action):
    destination, original, backup, audit = paths(source)
    record = json.load(open(audit)) if os.path.isfile(audit) else {'source': source, 'destination': destination, 'original': original, 'backup': backup, 'created_at': stamp()}
    if action == 'inspect':
        emit(source=source, destination=destination, phase=record.get('phase'), linked=os.path.islink(source), users=users(source))
        return
    verify_disk()
    if action == 'presync':
        if os.path.islink(source) or not os.path.isdir(source) or os.stat(source).st_dev != os.stat('/').st_dev:
            raise RuntimeError('source must be an existing root-backed directory')
        if os.path.lexists(destination) and not os.path.isfile(audit):
            raise RuntimeError('unowned destination already exists')
        parents(destination, source)
        record['phase'] = 'presync_started'
        write_json(audit, record)
        sync(source, destination, delete=True, presync=True)
        record.update(phase='presynced', presynced_at=stamp())
        write_json(audit, record)
    elif action == 'cutover':
        if record.get('phase') != 'presynced' or os.path.islink(source) or os.path.lexists(original):
            raise RuntimeError('invalid cutover state')
        active = users(source)
        if active:
            raise RuntimeError('source has active references: ' + json.dumps(active))
        checks = sqlite_checks(source)
        sync(source, destination, delete=True)
        before = manifest(source)
        copied = manifest(destination)
        if before != copied:
            differences = [k for k in set(before) | set(copied) if before.get(k) != copied.get(k)]
            raise RuntimeError('manifest mismatch: ' + repr(differences[:15]))
        if users(source) or manifest(source) != before:
            raise RuntimeError('source changed after verification')
        record.update(phase='verified_copy', verified_at=stamp(), entries=len(before), logical_bytes=sum(v.get('size', 0) for v in before.values()), sqlite_checks=checks)
        write_json(audit + '.manifest.json', before)
        write_json(audit, record)
        os.symlink(destination, original)
        exchange(source, original)
        record.update(phase='cutover', cutover_at=stamp())
        write_json(audit, record)
        if os.path.realpath(source) != destination or os.stat(source).st_dev != os.stat('/mnt/data-disk').st_dev:
            exchange(source, original)
            os.unlink(original)
            record['phase'] = 'presynced'
            write_json(audit, record)
            raise RuntimeError('post-cutover path verification failed; rolled back')
    elif action == 'finalize':
        if record.get('phase') not in ('cutover', 'backup_verified'):
            raise RuntimeError('source is not awaiting finalization')
        if not os.path.islink(source) or os.path.realpath(source) != destination or os.path.islink(original):
            raise RuntimeError('unexpected source/original path')
        if os.stat(original).st_dev != os.stat('/').st_dev or users(original):
            raise RuntimeError('original still in use or wrong filesystem')
        before = json.load(open(audit + '.manifest.json'))
        if manifest(original) != before:
            raise RuntimeError('original changed after cutover; preserve for reconciliation')
        parents(backup, source)
        sync(original, backup, delete=True)
        if manifest(backup) != before:
            raise RuntimeError('rollback backup verification failed')
        record.update(phase='backup_verified', backup_verified_at=stamp())
        write_json(audit, record)
        # The only deletion target is the exact exchanged, hashed original.
        assert original == source + '.root-storage-20260915-original'
        assert source in SOURCES and not os.path.islink(original)
        assert os.path.realpath(source) == destination
        if users(original):
            raise RuntimeError('original became active before final cleanup')
        shutil.rmtree(original)
        record.update(phase='complete', completed_at=stamp())
        write_json(audit, record)
    elif action == 'rollback':
        if record.get('phase') not in ('cutover', 'backup_verified', 'complete'):
            raise RuntimeError('no cutover to roll back')
        if not os.path.islink(source) or os.path.realpath(source) != destination or users(destination):
            raise RuntimeError('quiesce destination users before rollback')
        needed = subprocess.check_output(['du', '-s', '-B1', destination]).decode().split()[0]
        if int(needed) + 2 * 1024**3 > shutil.disk_usage('/').free:
            raise RuntimeError('insufficient root capacity for rollback')
        # Copy current live state back, not the older snapshot, after quiescing.
        os.makedirs(original, exist_ok=True)
        sync(destination, original, delete=True)
        if manifest(original) != manifest(destination):
            raise RuntimeError('rollback copy mismatch')
        exchange(source, original)
        if not os.path.islink(original) or os.readlink(original) != destination:
            raise RuntimeError('unexpected exchanged rollback link')
        os.unlink(original)
        record.update(phase='rolled_back', rolled_back_at=stamp())
        write_json(audit, record)
    emit(source=source, phase=record.get('phase'), audit=audit, logical_bytes=record.get('logical_bytes'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect', 'presync', 'cutover', 'finalize', 'rollback'])
    p.add_argument('source', choices=SOURCES)
    args = p.parse_args()
    with open('/run/lock/cpu-root-storage-20260915.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation(args.source, args.action)


if __name__ == '__main__':
    main()

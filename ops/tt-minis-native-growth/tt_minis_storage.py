#!/usr/bin/env python3
"""Fail-closed CPU-host TT Minis storage and consistent analysis snapshots."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
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
LEASE_SECONDS = 24 * 3600
RETAIN_LATEST = 2
MANAGED_BUDGET_BYTES = 40 * 1024**3
REFERENCE_ROOTS = [Path('/root/codex_test'), Path('/etc/cron.d'),
                   Path('/etc/systemd/system'), Path('/var/spool/cron')]
SNAPSHOT_NAME = re.compile(r'tt_minis_cache_[A-Za-z0-9_]+\.sqlite3')


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


@contextmanager
def lifecycle_lock():
    # Linux host only. A kernel lock is released even after process termination.
    import fcntl
    with (STORAGE / '.snapshot-lifecycle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_registry(registry):
    path = STORAGE / 'snapshot-registry.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(str(tmp), str(path))


def load_registry():
    path = STORAGE / 'snapshot-registry.json'
    if not path.exists():
        return {'version': 1, 'snapshots': {}}
    registry = json.loads(path.read_text(encoding='utf-8'))
    if registry.get('version') != 1 or not isinstance(registry.get('snapshots'), dict):
        raise RuntimeError('Invalid snapshot registry; refusing lifecycle changes')
    return registry


def file_identity(path):
    st = path.stat()
    return {'device': st.st_dev, 'inode': st.st_ino, 'bytes': st.st_size,
            'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns}


def source_revision(source):
    parts = []
    for path in (source, Path(str(source) + '-wal'), Path(str(source) + '-journal')):
        parts.append(file_identity(path) if path.exists() else None)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def snapshot_path(name):
    directory = STORAGE / 'analysis-snapshots'
    path = directory / name
    if (SNAPSHOT_NAME.fullmatch(name) is None or path.is_symlink()
            or path.resolve().parent != directory.resolve()):
        raise RuntimeError('Unsafe snapshot path')
    return path


def referenced_snapshots():
    """Code/config references are pins. Unknown/oversized scans fail closed."""
    refs = set()
    excluded = {'.git', '__pycache__', 'node_modules', 'venv', '.venv', 'data', 'logs'}
    count = 0
    for root in REFERENCE_ROOTS:
        if not root.exists():
            continue
        for directory, dirs, files in os.walk(str(root), onerror=lambda error: (_ for _ in ()).throw(error)):
            dirs[:] = [d for d in dirs if d not in excluded]
            for name in files:
                path = Path(directory) / name
                if path.suffix not in ('.py', '.sh', '.conf', '.service', '.timer', '.toml', ''):
                    continue
                if path.is_symlink():
                    # Configuration symlinks are common; read the actual content.
                    if not path.exists():
                        continue
                if path.suffix == '':
                    with path.open('rb') as stream:
                        magic = stream.read(4)
                    if magic.startswith((b'\x7fELF', b'MZ')):
                        continue  # Executable binaries such as ffmpeg are not source/config.
                count += 1
                if count > 20000 or path.stat().st_size > 4 * 1024**2:
                    raise RuntimeError('Reference scan limit reached; cleanup refused: ' + str(path))
                refs.update(SNAPSHOT_NAME.findall(path.read_text(encoding='utf-8', errors='replace')))
    return refs


def open_snapshot_inodes():
    opened = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    st = fd.stat()
                    opened.add((st.st_dev, st.st_ino))
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
    return opened


def protections(registry, now):
    protected = {name: 'source/config reference' for name in referenced_snapshots()}
    all_paths = sorted((STORAGE / 'analysis-snapshots').glob('*.sqlite3'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    for path in all_paths[:RETAIN_LATEST]:
        protected[path.name] = 'latest two snapshots'
    opened = open_snapshot_inodes()
    for path in all_paths:
        st = path.stat()
        if (st.st_dev, st.st_ino) in opened:
            protected[path.name] = 'open file descriptor'
    for name, record in registry['snapshots'].items():
        if record.get('pin'):
            protected[name] = 'explicit pin: ' + record['pin']
        elif record.get('lease_until', 0) > now:
            protected[name] = 'active lease'
    return protected


def cleanup_managed(registry, apply=False, required_bytes=0, now=None):
    now = time.time() if now is None else now
    protected = protections(registry, now)
    records = registry['snapshots']
    existing = [(name, rec, snapshot_path(name)) for name, rec in records.items()
                if snapshot_path(name).exists()]
    total = sum(path.stat().st_size for _, _, path in existing)
    removed = []
    for name, rec, path in sorted(existing, key=lambda item: item[1]['created_at']):
        # Retain two recent snapshots plus independently protected active inputs.
        # Do not accumulate another seven days of idle full-database copies.
        if name in protected:
            continue
        if file_identity(path) != rec['identity']:
            protected[name] = 'identity changed'
            continue
        size = path.stat().st_size
        if apply:
            st = path.stat()
            if (st.st_dev, st.st_ino) in open_snapshot_inodes():
                protected[name] = 'opened before cleanup'
                continue
            if file_identity(path) != rec['identity']:
                protected[name] = 'identity changed before cleanup'
                continue
            path.unlink()
            del records[name]
        total -= size
        removed.append({'name': name, 'bytes': size})
    if apply:
        save_registry(registry)
    return {'apply': apply, 'removed': removed, 'managed_bytes_after': total,
            'budget_bytes': MANAGED_BUDGET_BYTES, 'protected': protected}


def snapshot(owner='interactive-analysis'):
    source = prepare_storage()
    if not source.is_file():
        raise RuntimeError('Primary cache missing; do not create an empty database')
    with lifecycle_lock():
        registry = load_registry()
        revision = source_revision(source)
        now = time.time()
        for name, rec in sorted(registry['snapshots'].items(), key=lambda item: item[1]['created_at'], reverse=True):
            target = snapshot_path(name)
            if (rec.get('source_revision') == revision and target.is_file()
                    and rec['identity'] == file_identity(target)):
                rec.update(last_used_at=now, lease_until=max(rec.get('lease_until', 0), now + LEASE_SECONDS))
                rec['owners'] = sorted(set(rec.get('owners', []) + [owner]))
                save_registry(registry)
                return target
        report = cleanup_managed(registry, apply=True, required_bytes=source.stat().st_size)
        if report['managed_bytes_after'] + source.stat().st_size > MANAGED_BUDGET_BYTES:
            raise RuntimeError('Managed snapshot budget exhausted; release leases/pins or reuse an existing snapshot')
        target = create_snapshot(source)
        # Never associate a moving source with a reusable stable revision.
        stable = revision if source_revision(source) == revision else None
        registry['snapshots'][target.name] = {'created_at': now, 'last_used_at': now,
            'lease_until': now + LEASE_SECONDS, 'owners': [owner],
            'source_revision': stable, 'identity': file_identity(target)}
        save_registry(registry)
        return target


def create_snapshot(source):
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


def retire_candidates(plan, apply=False):
    """Only an explicitly supplied, audited legacy inventory can retire old copies."""
    with lifecycle_lock():
        registry = load_registry()
        protected = protections(registry, time.time())
        eligible, blocked = [], []
        for item in plan['candidates']:
            path = snapshot_path(item['name'])
            reason = protected.get(path.name)
            if str(path) != item['path']:
                raise RuntimeError('Candidate path mismatch')
            if not path.exists():
                blocked.append({'name': path.name, 'reason': 'already absent'})
                continue
            st = path.stat()
            if (st.st_ino != item['inode'] or st.st_size != item['bytes']
                    or int(st.st_mtime) != item['mtime'] or st.st_nlink != 1):
                reason = 'audited identity changed'
            if reason:
                blocked.append({'name': path.name, 'reason': reason})
            else:
                eligible.append(item)
        # A second FD scan immediately precedes unlink while lifecycle lock is held.
        opened = open_snapshot_inodes() if apply else set()
        removed = []
        for item in eligible:
            path = snapshot_path(item['name'])
            st = path.stat()
            if (st.st_ino != item['inode'] or st.st_size != item['bytes']
                    or int(st.st_mtime) != item['mtime'] or st.st_nlink != 1):
                blocked.append({'name': path.name, 'reason': 'identity changed before cleanup'})
                continue
            if (st.st_dev, st.st_ino) in opened:
                blocked.append({'name': path.name, 'reason': 'opened before cleanup'})
                continue
            if apply:
                path.unlink()
                registry['snapshots'].pop(path.name, None)
            removed.append(item)
        if apply:
            save_registry(registry)
        return {'apply': apply, 'retired': removed, 'blocked': blocked,
                'allocated_bytes': sum(item['allocated'] for item in removed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'snapshot', 'exec', 'prune', 'retire', 'lease', 'pin', 'unpin'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    path = prepare_storage()
    if args.action == 'snapshot':
        options = argparse.ArgumentParser()
        options.add_argument('--owner', default='interactive-analysis')
        print(snapshot(options.parse_args(args.command).owner))
    elif args.action in ('prune', 'retire'):
        options = argparse.ArgumentParser()
        options.add_argument('--apply', action='store_true')
        if args.action == 'retire':
            options.add_argument('--manifest', required=True)
        opts = options.parse_args(args.command)
        if args.action == 'retire':
            report = retire_candidates(json.loads(Path(opts.manifest).read_text(encoding='utf-8')), opts.apply)
        else:
            with lifecycle_lock():
                report = cleanup_managed(load_registry(), opts.apply)
        print(json.dumps(report, sort_keys=True))
    elif args.action in ('lease', 'pin', 'unpin'):
        options = argparse.ArgumentParser()
        options.add_argument('snapshot')
        options.add_argument('--owner', required=True)
        options.add_argument('--hours', type=float, default=24)
        opts = options.parse_args(args.command)
        name = Path(opts.snapshot).name
        with lifecycle_lock():
            registry = load_registry()
            rec = registry['snapshots'].get(name)
            if not rec or rec['identity'] != file_identity(snapshot_path(name)):
                raise RuntimeError('Snapshot is not registered or identity changed')
            if args.action == 'pin':
                rec['pin'] = opts.owner
            elif args.action == 'unpin':
                rec.pop('pin', None)
            else:
                if not 0 < opts.hours <= 24 * 30:
                    raise RuntimeError('Lease must be between 0 and 720 hours')
                rec['lease_until'] = max(rec.get('lease_until', 0), time.time() + opts.hours * 3600)
            rec['owners'] = sorted(set(rec.get('owners', []) + [opts.owner]))
            save_registry(registry)
        print(json.dumps({'ok': True, 'snapshot': name, 'action': args.action}))
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

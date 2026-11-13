"""One immutable SQLite snapshot pair per explicitly named recovery batch."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def write_audit(path, value):
    """Durable row-level audit, reusable only when the exact preimage agrees."""
    path = Path(path)
    content = canonical(value).encode()
    if path.exists():
        require(path.read_bytes() == content, 'batch_audit_changed')
        return
    temporary = path.with_suffix(path.suffix + '.partial')
    with temporary.open('wb') as stream:
        os.chmod(temporary, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def fingerprint(path):
    state = Path(path).stat()
    return {'size': state.st_size, 'mtime_ns': state.st_mtime_ns, 'inode': state.st_ino}


def snapshot(source, target):
    temporary = target.with_suffix('.sqlite3.partial')
    require(not target.exists() and not temporary.exists(), 'unfinished_batch_snapshot_requires_review')
    with contextlib.closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'backup_integrity_failed')
    os.chmod(temporary, 0o600)
    checksum = hashlib.sha256()
    with temporary.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    os.replace(temporary, target)
    return {'sha256': checksum.hexdigest(), 'stat': fingerprint(target)}


@contextlib.contextmanager
def batch_backup(directory, batch_id, native, central, scope):
    """Hold the batch lock through its task transaction; never recopy completed DBs."""
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', batch_id or ''), 'explicit_backup_batch_required')
    folder = Path(directory).resolve() / batch_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = {'batch_id': batch_id, 'scope': scope,
                'sources': {'fb.sqlite3': str(Path(native).resolve()), 'dispatch.sqlite3': str(Path(central).resolve())}}
    with (folder / '.batch.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            manifest_path = folder / 'batch.json'
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                require(manifest['identity'] == identity, 'backup_batch_identity_changed')
                require(set(manifest['files']) == set(identity['sources']), 'backup_batch_files_incomplete')
                for name, evidence in manifest['files'].items():
                    require(fingerprint(folder / name) == evidence['stat'], 'backup_batch_file_changed')
            else:
                # A partial first preparation is retained for inspection, never
                # overwritten or misrepresented as a complete rollback snapshot.
                files = {name: snapshot(source, folder / name) for name, source in identity['sources'].items()}
                manifest = {'identity': identity, 'files': files}
                write_audit(manifest_path, manifest)
            yield folder
        finally:
            if os.name == 'nt':
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

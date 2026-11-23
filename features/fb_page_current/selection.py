"""Derived current-version cache for the existing Page selector."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .common import ROOT, chunks, encoded, scope_hash

FILE = ROOT / 'selection' / 'current.sqlite3'


def project(profile):
    result = {k: profile.get(k) for k in ('page_id', 'status', 'audiences')}
    if result['audiences']:
        result['audiences'] = [dict(a) for a in result['audiences']]
        for audience in result['audiences']:
            if audience.get('children'):
                audience['children'] = [
                    {k: v for k, v in theme.items() if k != 'dramas'}
                    for theme in audience['children']
                ]
    return result


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True, timeout=2)


def inspect(path=FILE):
    if path.is_file():
        with closing(connect(path)) as db:
            return json.loads(db.execute('SELECT payload FROM head').fetchone()[0])


class Builder:
    def __init__(self, meta, proof, path=FILE):
        self.path, self.meta, self.proof = path, meta, proof
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.building-', suffix='.sqlite3', dir=path.parent)
        os.close(fd)
        self.temp = Path(name)
        self.db = sqlite3.connect(self.temp)
        self.db.execute('CREATE TABLE head(payload TEXT NOT NULL,scope TEXT NOT NULL,n INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE pages(page_id TEXT PRIMARY KEY,projection TEXT NOT NULL) WITHOUT ROWID')
        self.seen = set()

    def add(self, pid, profile, verified_digest):
        if profile['page_id'] != pid or self.proof['payload_hashes'].get(pid) != verified_digest:
            raise ValueError('unverified selection source')
        self.db.execute('INSERT INTO pages VALUES(?,?)', (pid, encoded(project(profile))))
        self.seen.add(pid)

    def finish(self):
        if (self.seen != set(self.proof['payload_hashes']) or len(self.seen) != self.proof['verified_rows']
                or self.meta['version'] != self.proof['version']):
            raise ValueError('selection scope mismatch')
        self.db.execute('INSERT INTO head VALUES(?,?,?)', (encoded(self.meta), scope_hash(self.seen), len(self.seen)))
        self.db.commit()
        if self.db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise RuntimeError('selection cache database corrupt')
        self.db.close()
        with (self.path.parent / 'selection-publish.lock').open('a') as lock:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                old = inspect(self.path)
            except (ValueError, TypeError, sqlite3.DatabaseError):
                old = None
            if old is None or old['version'] <= self.meta['version']:
                self.temp.replace(self.path)
                if os.name == 'posix':
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)

    def close(self):
        self.db.close()
        self.temp.unlink(missing_ok=True)


@contextmanager
def alongside(meta, proof, path):
    builder = None
    def deferred(exc):
        print(encoded(dict(event='selection_cache_deferred', version=meta['version'], error=type(exc).__name__)), flush=True)
    try:
        try:
            builder = Builder(meta, proof, path)
        except Exception as exc:
            deferred(exc)

        def observe(pid, profile, digest):
            nonlocal builder
            if builder is not None:
                try:
                    builder.add(pid, profile, digest)
                except Exception as exc:
                    deferred(exc)
                    builder.close()
                    builder = None

        yield observe
        if builder is not None:
            try:
                builder.finish()
            except Exception as exc:
                deferred(exc)
    finally:
        if builder is not None:
            builder.close()


def load(version, ids, path=FILE):
    try:
        if not path.is_file():
            return None
        with closing(connect(path)) as db:
            db.execute('BEGIN')
            meta = json.loads(db.execute('SELECT payload FROM head').fetchone()[0])
            if meta['version'] != version:
                return None
            found = {}
            for batch in chunks(ids, 100):
                marks = ','.join('?' for _ in batch)
                for pid, value in db.execute(f'SELECT page_id,projection FROM pages WHERE page_id IN ({marks})', batch):
                    found[pid] = json.loads(value)
            return found, meta, []
    except (OSError, sqlite3.Error, KeyError, ValueError, TypeError):
        return None

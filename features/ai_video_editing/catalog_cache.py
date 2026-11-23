"""Small durable cache for browsing metadata; never stores video download URLs."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import copy
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

LOG = logging.getLogger(__name__)
FIELDS = {'id', 'key', 'number', 'name', 'subtitle', 'cover', 'app', 'country', 'language', 'episodes'}


class BrowseCache:
    fresh_seconds = 300
    stale_seconds = 86400

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.entries = {}
        self.pending = {}
        self.errors = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='editor-catalog-refresh')
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS browse_cache(key TEXT PRIMARY KEY, updated REAL NOT NULL, data TEXT NOT NULL)')
            self.path.chmod(0o600)

    def close(self):
        self.pool.shutdown(wait=True)

    def _read(self, key):
        with self.lock:
            entry = self.entries.get(key)
        if entry is None and self.path:
            try:
                with closing(sqlite3.connect(self.path, timeout=1)) as db:
                    row = db.execute('SELECT updated,data FROM browse_cache WHERE key=?', (key,)).fetchone()
                if row:
                    entry = (row[0], json.loads(row[1]))
                    with self.lock:
                        self.entries[key] = entry
            except (sqlite3.Error, ValueError):
                LOG.warning('Catalog metadata cache read unavailable')
        return entry

    def _load(self, key, loader, future):
        try:
            loaded = loader()
            # Positive allowlist keeps expiring/signed video addresses out of disk and browser caches.
            data = {k: v for k, v in loaded.items() if k in {'product', 'key', 'page', 'has_more'}}
            data['items'] = [{k: v for k, v in row.items() if k in FIELDS} for row in loaded['items']]
            entry = (time.time(), data)
            with self.lock:
                if len(self.entries) >= 512:
                    oldest = min(self.entries, key=lambda k: self.entries[k][0])
                    self.entries.pop(oldest, None)
                self.entries[key] = entry
                self.errors.pop(key, None)
            if self.path:
                try:
                    with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                        db.execute('INSERT OR REPLACE INTO browse_cache VALUES(?,?,?)', (key, entry[0], json.dumps(data, ensure_ascii=False)))
                        db.execute('DELETE FROM browse_cache WHERE key IN (SELECT key FROM browse_cache ORDER BY updated DESC LIMIT -1 OFFSET 512)')
                except sqlite3.Error:
                    LOG.warning('Catalog metadata cache persistence unavailable')
            future.set_result(entry)
        except Exception as exc:
            with self.lock:
                self.errors[key] = (time.time(), getattr(exc, 'message', '暂时无法更新剧库'))
                if len(self.errors) > 512:
                    self.errors.pop(next(iter(self.errors)))
            future.set_exception(exc)
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def _begin(self, key, loader, background):
        with self.lock:
            if key in self.pending:
                return self.pending[key]
            if background and (len(self.pending) >= 16 or time.time()-self.errors.get(key, (0, ''))[0] < 30):
                return None
            future = Future()
            self.pending[key] = future
        if background:
            self.pool.submit(self._load, key, loader, future)
        else:
            self._load(key, loader, future)
        return future

    def get(self, key, loader, refresh=False):
        entry = self._read(key)
        age = max(0, time.time()-entry[0]) if entry else self.stale_seconds+1
        if not refresh and entry and age <= self.stale_seconds:
            stale = age >= self.fresh_seconds
            if stale:
                self._begin(key, loader, background=True)
            state = 'stale' if stale else 'fresh'
        else:
            entry = self._begin(key, loader, background=False).result()
            state = 'updated'
        with self.lock:
            pending = key in self.pending
            error = self.errors.get(key, (0, ''))[1]
        return dict(copy.deepcopy(entry[1]), cache={
            'state': state, 'updated_at': entry[0], 'refreshing': pending,
            'error': error if state == 'stale' else '',
        })

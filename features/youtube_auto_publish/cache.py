"""Bounded in-process read cache. Never used to authorize a write."""
import copy
import threading
import time
from collections import OrderedDict

from .templates import WorkflowError


class ReadCache:
    def __init__(self, *, ttl=300, max_age=900, capacity=32, workers=2, clock=time.monotonic):
        self.ttl, self.max_age, self.capacity = ttl, max_age, capacity
        self.workers, self.clock = workers, clock
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.running = set()

    def read(self, key, loader, *, refresh=False):
        with self.lock:
            now = self.clock()
            entry = self.entries.get(key, {})
            age = max(0, now - entry['at']) if 'at' in entry else None
            due = refresh or age is None or age >= self.ttl
            cooling = now < entry.get('retry_at', 0)
            if due and not cooling and key not in self.running and len(self.running) < self.workers:
                self.running.add(key)
                threading.Thread(target=self._load, args=(key, loader), daemon=True, name='youtube-read-cache').start()
            running = key in self.running
            if key in self.entries:
                self.entries.move_to_end(key)
            if entry.get('error') and (age is None or age > self.max_age) and not running:
                raise WorkflowError('material_query_failed', entry['error'], 503)
            rows = entry.get('value', []) if age is not None and age <= self.max_age else []
            # A cold request waiting for a free worker should be polled too.
            return copy.deepcopy(rows), dict(age_seconds=round(age, 1) if age is not None else None,
                ttl_seconds=self.ttl, stale=age is not None and age >= self.ttl,
                refreshing=running or (due and not cooling), error=entry.get('error', ''))

    def _load(self, key, loader):
        try:
            value = loader()
            entry = dict(value=copy.deepcopy(value), at=self.clock())
        except Exception:
            with self.lock:
                entry = dict(self.entries.get(key, {}))
            entry.update(error='素材更新失败，请稍后刷新；提交时仍会重新核验素材。', retry_at=self.clock()+30)
        with self.lock:
            self.entries[key] = entry
            self.entries.move_to_end(key)
            self.running.discard(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)

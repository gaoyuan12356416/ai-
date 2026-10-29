"""Non-sensitive execution measurements; no business state or replay."""
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
import threading
import time

from .runtime import CST


class Timings:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.values = {}

    def add(self, name, seconds):
        seconds = max(0., seconds)
        with self.lock:
            item = self.values.setdefault(name, {'count': 0, 'total_seconds': 0., 'max_seconds': 0.})
            item['count'] += 1
            item['total_seconds'] += seconds
            item['max_seconds'] = max(item['max_seconds'], seconds)

    @contextmanager
    def measure(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.add(name, self.clock() - started)

    def snapshot(self):
        with self.lock:
            return {name: dict(count=item['count'],
                total_seconds=round(item['total_seconds'], 6),
                mean_seconds=round(item['total_seconds'] / item['count'], 6),
                max_seconds=round(item['max_seconds'], 6)) for name, item in self.values.items()}


def timed(name):
    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            started = self.clock()
            result = None
            try:
                result = method(self, *args, **kwargs)
                return result
            finally:
                seconds = self.clock() - started
                self.timings.add(name, seconds)
                if name == 'ad_attempt' and isinstance(result, dict):
                    self.timings.add('ad_attempt_' + result['status'], seconds)
        return call
    return decorate


def record_cycle(root, metrics):
    folder = Path(root) / 'performance'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(CST).strftime('%Y%m%d') + '.jsonl')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(metrics, ensure_ascii=False, separators=(',', ':')) + '\n')


def idle_delay(result, has_ready):
    """Drain real work immediately; back off busy dependencies, then idle."""
    if not has_ready:
        return 30
    if any(n for status, n in result.get('tasks', {}).items() if status not in ('pending', 'not_claimed')):
        return 0
    return 2

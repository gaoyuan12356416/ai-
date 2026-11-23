"""Refresh only recently viewed monitor snapshots, with one background read."""
from pathlib import Path
import threading
import time


class HostPressure:
    def __init__(self): self.previous = None

    def __call__(self):
        try:
            values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(values), values[3] + values[4]
            before, self.previous = self.previous, (total, idle)
            if before is None or total <= before[0]: return False
            busy = 1-(idle-before[1])/(total-before[0])
            available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
            return busy < 0.75 and available >= 8*1024**3
        except (OSError, ValueError, StopIteration): return False


class MonitorSnapshots:
    def __init__(self, cache, *, clock=time.monotonic, pressure=None, interval=4, active_seconds=40):
        self.cache, self.clock = cache, clock
        self.pressure = pressure or HostPressure()
        self.interval, self.active_seconds = interval, active_seconds
        self.jobs, self.lock, self.stopping = {}, threading.Lock(), threading.Event()
        self.thread = None

    def touch(self, key, refresh):
        now = self.clock()
        with self.lock:
            if key in self.jobs:
                self.jobs[key].update(touched=now, refresh=refresh)
            else:
                if len(self.jobs) >= 128:
                    oldest = min(self.jobs, key=lambda k: self.jobs[k]['touched'])
                    self.jobs.pop(oldest)
                self.jobs[key] = {'touched': now, 'due': now+self.interval, 'refresh': refresh}

    def tick(self):
        now = self.clock()
        with self.lock:
            for key in list(self.jobs):
                if now-self.jobs[key]['touched'] > self.active_seconds: self.jobs.pop(key)
            due = [(key, job) for key, job in self.jobs.items() if job['due'] <= now]
            if not due: return
            key, job = min(due, key=lambda item: item[1]['due'])
            refresh = job['refresh']
        if not self.pressure(): return
        succeeded = False
        try:
            value, stale, _ = self.cache.get(key, refresh, wait=12, refresh=True)
            succeeded = value.status == 200 and not stale
        except Exception: pass
        with self.lock:
            if key in self.jobs:
                # A failed/revoked read does not create a refresh loop.
                if not succeeded: self.jobs.pop(key)
                else: self.jobs[key]['due'] = self.clock()+self.interval

    def start(self):
        def run():
            while not self.stopping.wait(1): self.tick()
        self.thread = threading.Thread(target=run, name='local-monitor-snapshot', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread: self.thread.join(timeout=15)

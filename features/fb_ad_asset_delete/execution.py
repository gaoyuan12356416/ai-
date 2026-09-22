"""Process-wide deletion budgets; account locks also span separate UI jobs."""
from contextlib import contextmanager
import threading
import time

from .core import AssetError


class ExecutionBudget:
    def __init__(self, concurrency=8):
        self.concurrency = concurrency
        self.slots = threading.BoundedSemaphore(concurrency)
        self.mutex = threading.Lock()
        self.accounts = {}
        self.cooldowns = {}
        self.global_until = 0.0

    @staticmethod
    def check(stop):
        if stop.is_set():
            raise AssetError("execution_stopped", "执行已停止，保留已完成结果", 409)

    @contextmanager
    def account_scope(self, accounts, stop):
        keys = sorted(set(accounts))
        locks, held = [], []
        with self.mutex:
            for key in keys:
                entry = self.accounts.setdefault(key, [threading.Lock(), 0])
                entry[1] += 1
                locks.append((key, entry[0]))
        try:
            for key, lock in locks:
                while not lock.acquire(timeout=0.1):
                    self.check(stop)
                held.append(lock)
                self.check(stop)
            yield
        finally:
            for lock in reversed(held):
                lock.release()
            with self.mutex:
                for key, lock in locks:
                    self.accounts[key][1] -= 1
                    if not self.accounts[key][1]:
                        del self.accounts[key]

    def wait(self, accounts, stop):
        delayed = False
        while True:
            self.check(stop)
            with self.mutex:
                remaining = max([self.global_until] + [self.cooldowns.get(a, 0) for a in accounts]) - time.monotonic()
            if remaining <= 0:
                return delayed
            delayed = True
            stop.wait(min(remaining, 0.25))

    @contextmanager
    def operation(self, accounts, stop):
        # Wait for an account cooldown before occupying global execution capacity.
        self.wait(accounts, stop)
        while not self.slots.acquire(timeout=0.1):
            self.check(stop)
        try:
            self.wait(accounts, stop)
            yield
        finally:
            self.slots.release()

    def observe(self, accounts, metadata):
        """Conservative pacing from sanitized Meta headers; never replay writes."""
        def numbers(value, key):
            if isinstance(value, dict):
                for name, item in value.items():
                    if name == key and isinstance(item, (int, float)) and not isinstance(item, bool):
                        yield item
                    else:
                        yield from numbers(item, key)
            elif isinstance(value, list):
                for item in value:
                    yield from numbers(item, key)

        usage = metadata.get("usage") or {}
        error = metadata.get("error") or {}
        code = str(error.get("code", ""))
        limited = metadata.get("http_status") == 429 or code in {"4", "17", "32", "613", "80004"}
        percentages = [n for key in ("call_count", "total_cputime", "total_time", "acc_id_util_pct") for n in numbers(usage, key)]
        percent = max(percentages or [0])
        regain = max(list(numbers(usage, "estimated_time_to_regain_access")) or [0]) * 60
        delay = max(60 if limited else 30 if percent >= 95 else 1 if percent >= 80 else 0,
                    min(86400, regain), min(86400, metadata.get("retry_after_seconds") or 0))
        if not delay:
            return
        app_usage = usage.get("x-app-usage") or {}
        app_high = any(n >= 80 for key in ("call_count", "total_cputime", "total_time") for n in numbers(app_usage, key))
        global_limit = code in {"4", "17", "613"} or app_high or not accounts
        with self.mutex:
            until = time.monotonic() + delay
            if global_limit:
                self.global_until = max(self.global_until, until)
            else:
                for account in accounts:
                    self.cooldowns[account] = max(self.cooldowns.get(account, 0), until)


EXECUTION_BUDGET = ExecutionBudget(8)
SOURCE_READ_SLOTS = threading.BoundedSemaphore(4)

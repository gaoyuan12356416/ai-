"""Bounded, process-local read cache with coalescing; never cache failures."""
import copy
import json
import threading
import time
from collections import OrderedDict

from .report import ReportError

MAX_ENTRIES = 32
MAX_BYTES = 32 * 1024 * 1024
MAX_INFLIGHT = 16
_lock = threading.Lock()
_cache = OrderedDict()
_inflight = {}
_bytes = 0


class Flight:
    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None


def clear():
    """Clear completed cached reads only; do not cancel running collectors."""
    global _bytes
    with _lock:
        _cache.clear()
        _bytes = 0


def get_or_load(key, loader, *, deadline, ttl=300):
    global _bytes
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            _cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        flight = _inflight.get(key)
        owner = flight is None
        if owner:
            if len(_inflight) >= MAX_INFLIGHT:
                raise ReportError("效果查询繁忙，请稍后重试；本次不按零统计", 503)
            flight = _inflight[key] = Flight()
    if not owner:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not flight.done.wait(remaining):
            raise ReportError("效果查询等待超时，请稍后重试；本次不按零统计", 503)
        if flight.error is not None:
            raise flight.error
        return copy.deepcopy(flight.value)
    try:
        value = loader()
        # Serialized size is a conservative bound on payload bytes; cap both
        # entry count and retained payload, not merely the number of SQL keys.
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        with _lock:
            flight.value = copy.deepcopy(value)
            if size <= MAX_BYTES:
                previous = _cache.pop(key, None)
                if previous:
                    _bytes -= previous[2]
                _cache[key] = (time.monotonic(), flight.value, size)
                _bytes += size
                while len(_cache) > MAX_ENTRIES or _bytes > MAX_BYTES:
                    _bytes -= _cache.popitem(last=False)[1][2]
        return value
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        with _lock:
            _inflight.pop(key, None)
            flight.done.set()

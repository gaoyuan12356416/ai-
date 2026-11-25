"""Bounded actor/version cache with one refresh and backoff per request key."""
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def cache_key(actor, target, version):
    # Include the complete current permission scope, even for administrators.
    scope = {k: actor.get(k) for k in ('tenant_key', 'user_id', 'role', 'permissions', 'channels', 'fb_owner_user_id')}
    parsed = urlsplit(target)
    parameters = sorted(parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=720), key=lambda item: item[0])
    canonical = [parsed.path, parameters]
    return hashlib.sha256(json.dumps([scope, canonical, version], sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class Unavailable(Exception):
    pass


class SingleFlightCache:
    def __init__(self, ttl=300, max_bytes=128*1024*1024, max_entries=128, clock=time.monotonic):
        self.ttl, self.max_bytes, self.max_entries, self.clock = ttl, max_bytes, max_entries, clock
        self.entries, self.failures, self.pending = OrderedDict(), OrderedDict(), {}
        self.size, self.lock = 0, threading.Lock()

    def get(self, key, loader, wait=40, refresh=False):
        deadline = self.clock() + wait
        while True:
            with self.lock:
                now = self.clock()
                entry = self.entries.get(key)
                if entry:
                    self.entries.move_to_end(key)
                    if not refresh and now - entry[0] <= self.ttl:
                        return entry[1], False, True
                if now < self.failures.get(key, 0):
                    if entry: return entry[1], True, True
                    raise Unavailable('refresh_backoff')
                if key in self.pending:
                    if entry: return entry[1], True, True
                    event = self.pending[key]
                else:
                    event = self.pending[key] = threading.Event()
                    break
            remaining = deadline - self.clock()
            if remaining <= 0 or not event.wait(remaining):
                raise Unavailable('refresh_in_progress')
        try:
            value = loader()
            if value.status >= 500: raise Unavailable('source_unavailable')
            with self.lock:
                if value.status == 200 and len(value.body) <= self.max_bytes:
                    old = self.entries.pop(key, None)
                    if old: self.size -= len(old[1].body)
                    self.entries[key] = (self.clock(), value)
                    self.size += len(value.body)
                    while self.size > self.max_bytes or len(self.entries) > self.max_entries:
                        _, removed = self.entries.popitem(last=False)
                        self.size -= len(removed[1].body)
                self.failures.pop(key, None)
            return value, False, False
        except Exception:
            with self.lock:
                self.failures[key] = self.clock() + 30
                self.failures.move_to_end(key)
                while len(self.failures) > self.max_entries: self.failures.popitem(last=False)
            if entry: return entry[1], True, True
            raise
        finally:
            with self.lock:
                self.pending.pop(key).set()


def annotated(value, *, stale=False, hit=False, backend='hk-snapshot'):
    headers = dict(value.headers)
    headers.update({'X-AI-Cache': 'hit' if hit else 'miss', 'X-AI-Stale': str(bool(stale)).lower(),
                    'X-AI-Backend': backend, 'Cache-Control': 'no-store', 'Vary': 'Cookie'})
    body = value.body
    if headers.get('Content-Type', '').startswith('application/json') and value.status == 200:
        payload = json.loads(body)
        meta = payload.setdefault('meta', {})
        meta.update(offload_backend=backend, offload_cache_stale=bool(stale), offload_cache_hit=bool(hit))
        data_time = headers.get('X-AI-Data-Time') or headers.get('X-Ai-Data-Time')
        if data_time: meta['data_time'] = data_time
        if stale:
            message = '查询服务暂不可用，当前显示最近成功快照；请核对数据时间，缺失数据不按零统计。'
            warnings = payload.setdefault('warnings', [])
            if message not in warnings: warnings.append(message)
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    return Response(value.status, headers, body)

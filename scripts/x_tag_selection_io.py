"""Bounded, process-local read reuse for the independent X tag selector.

Only candidate discovery is cached. Blacklists, publication history, local
reservations and the strict final material resolver keep their live reads.
"""
from collections import OrderedDict
from concurrent.futures import Future
import copy
import threading
import time


class CursorLease:
    def __init__(self, cursor, lease):
        self.cursor, self.lease = cursor, lease

    def __getattr__(self, name):
        value = getattr(self.cursor, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except Exception:
                self.lease.broken = True
                raise
        return call

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ConnectionLease:
    def __init__(self, pool, connection, born):
        self.pool, self.connection, self.born = pool, connection, born
        self.broken, self.closed = False, False

    def cursor(self, *args, **kwargs):
        try:
            return CursorLease(self.connection.cursor(*args, **kwargs), self)
        except Exception:
            self.broken = True
            raise

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pool.release(self)


class ReadConnectionPool:
    def __init__(self, factory, *, capacity=4, idle_seconds=30, lifetime=300,
                 acquire_timeout=30, clock=time.monotonic):
        self.factory, self.capacity = factory, capacity
        self.idle_seconds, self.lifetime = idle_seconds, lifetime
        self.acquire_timeout, self.clock = acquire_timeout, clock
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock, self.idle = threading.Lock(), []
        self.created = self.reused = self.discarded = 0

    def __call__(self):
        if not self.slots.acquire(timeout=self.acquire_timeout):
            raise TimeoutError('selection_read_pool_busy')
        try:
            now = self.clock()
            with self.lock:
                entry = self.idle.pop() if self.idle else None
            if entry is not None:
                connection, born, returned = entry
                if now - returned <= self.idle_seconds and now - born <= self.lifetime:
                    try:
                        # Never reconnect implicitly or retry a failed query.
                        connection.ping(reconnect=False)
                    except Exception:
                        self._discard(connection)
                    else:
                        self.reused += 1
                        return ConnectionLease(self, connection, born)
                else:
                    self._discard(connection)
            connection = self.factory()
            self.created += 1
            return ConnectionLease(self, connection, self.clock())
        except BaseException:
            self.slots.release()
            raise

    def _discard(self, connection):
        self.discarded += 1
        try:
            connection.close()
        except Exception:
            pass

    def release(self, lease):
        try:
            if lease.broken or self.clock() - lease.born > self.lifetime:
                self._discard(lease.connection)
            else:
                with self.lock:
                    self.idle.append((lease.connection, lease.born, self.clock()))
        finally:
            self.slots.release()


class CandidateReadCache:
    """Coalesce identical concurrent reads; keep small results for five seconds."""
    def __init__(self, *, ttl=5, capacity=128, max_rows=5000, clock=time.monotonic):
        self.ttl, self.capacity, self.max_rows, self.clock = ttl, capacity, max_rows, clock
        self.lock, self.values, self.pending = threading.Lock(), OrderedDict(), {}
        self.hits = self.loads = 0

    def get(self, key, loader):
        with self.lock:
            cached = self.values.get(key)
            if cached is not None and self.clock() < cached[0]:
                self.values.move_to_end(key)
                self.hits += 1
                return copy.deepcopy(cached[1])
            self.values.pop(key, None)
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
            else:
                self.hits += 1
        if not owner:
            return copy.deepcopy(future.result())
        try:
            value = loader()
            self.loads += 1
            with self.lock:
                if len(value) <= self.max_rows:
                    self.values[key] = (self.clock() + self.ttl, copy.deepcopy(value))
                    while len(self.values) > self.capacity:
                        self.values.popitem(last=False)
            future.set_result(value)
            return copy.deepcopy(value)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)


def install(service):
    """Inject only this process's existing read dependencies before building it."""
    original_connect = service.connect_read_only
    original_repository = service.ReadOnlyMySQLRepository
    pool_lock, pools = threading.Lock(), {}
    cache = CandidateReadCache()

    def connect(**kwargs):
        # Pool identity includes credentials in memory only, never in output.
        key = tuple(sorted(kwargs.items()))
        with pool_lock:
            pool = pools.get(key)
            if pool is None:
                pool = pools[key] = ReadConnectionPool(lambda: original_connect(**kwargs))
        return pool()

    class CandidateRepository(original_repository):
        def list_drama_rows(self, **kwargs):
            from features.hit_publish import FUTURE_TIMES
            key = ('dramas', self.schema, self.app_id,
                   tuple(sorted(FUTURE_TIMES.get().items())),
                   tuple((k, tuple(v) if isinstance(v, (list, tuple)) else v)
                         for k, v in sorted(kwargs.items())))
            return cache.get(key, lambda: super(CandidateRepository, self).list_drama_rows(**kwargs))

        def list_material_rows(self, **kwargs):
            key = ('materials', self.schema, self.product, self.material_data_source,
                   tuple(sorted(kwargs.items())))
            return cache.get(key, lambda: super(CandidateRepository, self).list_material_rows(**kwargs))

    from features.x_tag_publish import policy
    original_labels = policy.catalog_labels
    def labels(source, dramas):
        key = ('labels', source.schema,
               tuple((row.content_id, row.language) for row in dramas))
        return cache.get(key, lambda: original_labels(source, dramas))

    service.connect_read_only = connect
    service.ReadOnlyMySQLRepository = CandidateRepository
    policy.catalog_labels = labels
    return pools, cache

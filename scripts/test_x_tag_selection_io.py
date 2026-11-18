import concurrent.futures
import contextvars
import sys
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from x_tag_selection_io import ReadConnectionPool, CandidateReadCache, install


class Connection:
    def __init__(self):
        self.closed = False
        self.fail = False
        self.dead = False
    def ping(self, reconnect):
        assert reconnect is False
        if self.dead: raise OSError('stale')
    def close(self): self.closed = True
    def cursor(self, *args): return self
    def execute(self, *args):
        if self.fail: raise OSError('query failed')
        return 1


class PoolTests(unittest.TestCase):
    def test_reuse_does_not_close_connection_and_double_close_is_safe(self):
        made = []
        def factory():
            made.append(Connection()); return made[-1]
        pool = ReadConnectionPool(factory, capacity=1)
        a = pool(); a.close(); a.close()
        b = pool()
        self.assertIs(a.connection, b.connection)
        self.assertFalse(b.connection.closed)
        b.close()
        self.assertEqual((pool.created, pool.reused), (1, 1))

    def test_failed_query_is_not_retried_and_connection_is_discarded(self):
        pool = ReadConnectionPool(Connection, capacity=1)
        a = pool(); a.connection.fail = True
        with self.assertRaises(OSError): a.cursor().execute('SELECT 1')
        a.close()
        self.assertTrue(a.connection.closed)
        b = pool(); self.assertIsNot(a.connection, b.connection); b.close()

    def test_dead_idle_connection_is_replaced_before_query(self):
        pool = ReadConnectionPool(Connection, capacity=1)
        a = pool(); a.connection.dead = True; a.close()
        b = pool(); self.assertTrue(a.connection.closed); b.close()
        self.assertEqual(pool.created, 2)

    def test_factory_failure_releases_capacity(self):
        calls = []
        def factory():
            calls.append(1)
            if len(calls) == 1: raise OSError('connect')
            return Connection()
        pool = ReadConnectionPool(factory, capacity=1, acquire_timeout=.1)
        with self.assertRaises(OSError): pool()
        pool().close()

    def test_idle_and_total_lifetime_expire(self):
        now = [0]
        pool = ReadConnectionPool(Connection, capacity=1, idle_seconds=5, lifetime=8, clock=lambda: now[0])
        a = pool(); a.close(); now[0] = 6
        b = pool(); self.assertTrue(a.connection.closed)
        now[0] = 15; b.close(); self.assertTrue(b.connection.closed)

    def test_four_connections_are_never_shared_between_eight_threads(self):
        pool = ReadConnectionPool(Connection, capacity=4)
        active, lock = set(), threading.Lock()
        def work(_):
            c = pool()
            with lock:
                self.assertNotIn(id(c.connection), active)
                active.add(id(c.connection)); self.assertLessEqual(len(active), 4)
            time.sleep(.02)
            with lock: active.remove(id(c.connection))
            c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(work, range(32)))
        self.assertEqual(pool.created, 4)
        self.assertEqual(pool.reused, 28)


class CacheTests(unittest.TestCase):
    def test_concurrent_same_read_is_loaded_once_and_results_are_independent(self):
        cache = CandidateReadCache()
        barrier = threading.Barrier(4)
        loads = []
        def load(): loads.append(1); time.sleep(.03); return [{'id': 7}]
        def work(_): barrier.wait(); return cache.get('same', load)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            values = list(executor.map(work, range(4)))
        self.assertEqual(len(loads), 1)
        values[0][0]['id'] = 8
        self.assertEqual(values[1][0]['id'], 7)
        self.assertEqual(cache.get('same', load), [{'id': 7}])

    def test_expiry_capacity_and_large_result(self):
        now = [0]; cache = CandidateReadCache(ttl=5, capacity=1, max_rows=2, clock=lambda: now[0])
        self.assertEqual(cache.get('a', lambda:[1]), [1])
        now[0]=6
        self.assertEqual(cache.get('a', lambda:[2]), [2])
        cache.get('b', lambda:[3]); self.assertNotIn('a', cache.values)
        cache.get('big', lambda:[1,2,3]); self.assertNotIn('big', cache.values)

    def test_failed_load_is_not_cached(self):
        cache = CandidateReadCache()
        def fail(): raise ValueError('query failed')
        with self.assertRaises(ValueError): cache.get('a', fail)
        self.assertEqual(cache.get('a', lambda:[2]), [2])

    def test_integration_keeps_blacklists_fresh_and_separates_future_scope(self):
        future = contextvars.ContextVar('future', default={})
        class Repository:
            schema, app_id, material_data_source, product = 'ads', 1479, 1, 'Dramawave'
            def __init__(self): self.reads = self.blacks = 0
            def list_drama_rows(self, **kw): self.reads += 1; return [dict(future.get())]
            def list_material_rows(self, **kw): self.reads += 1; return [{'id':1}]
            def blacklist_snapshot(self): self.blacks += 1; return self.blacks
        policy = types.SimpleNamespace(catalog_labels=lambda source, rows: {})
        service = types.SimpleNamespace(connect_read_only=lambda **kw: Connection(), ReadOnlyMySQLRepository=Repository)
        with patch.dict(sys.modules, {'features':types.ModuleType('features'),
                'features.x_tag_publish':types.SimpleNamespace(policy=policy),
                'features.hit_publish':types.SimpleNamespace(FUTURE_TIMES=future)}):
            pools, cache = install(service)
            repo = service.ReadOnlyMySQLRepository()
            repo.list_material_rows(content_id='a', language='en')
            repo.list_material_rows(content_id='a', language='en')
            self.assertEqual(repo.reads, 1)
            self.assertEqual((repo.blacklist_snapshot(),repo.blacklist_snapshot()), (1,2))
            self.assertEqual(repo.list_drama_rows(now_epoch=100), [{}])
            token = future.set({'a:en':200})
            self.assertEqual(repo.list_drama_rows(now_epoch=100), [{'a:en':200}])
            future.reset(token)
            self.assertEqual(repo.list_drama_rows(now_epoch=101), [{}])
            for _ in range(3): service.connect_read_only(host='replica', port=63350).close()
            self.assertEqual(sum(p.created for p in pools.values()), 1)


if __name__ == '__main__': unittest.main()

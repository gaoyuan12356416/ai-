"""Directory-only cache behavior; all reads use deterministic local test doubles."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.moboreels_library.catalog import Catalog
from features.moboreels_library.db import DatabaseError

try:
    import fcntl
except ImportError:
    sys.modules["fcntl"] = SimpleNamespace()
    try:
        from features.moboreels_library.service import Application
    finally:
        del sys.modules["fcntl"]
else:
    from features.moboreels_library.service import Application


class FakeDatabase:
    def __init__(self):
        self.calls = []
        self.details = 0
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail = False

    def list_dramas(self, params):
        with self.lock:
            self.calls.append(deepcopy(params))
            version = len(self.calls)
        self.entered.set()
        if self.block and not self.release.wait(3):
            raise RuntimeError("test did not release the database read")
        if self.fail:
            raise DatabaseError("test read failed")
        return {"list": [{"story_id": "42", "story_name": "version %d" % version, "lang_id": 3}],
                "total": 1, "filters": {"languages": [3]}}

    def get_drama(self, story_id):
        self.details += 1
        return {"story_id": story_id, "lang_id": 3}


class CatalogCacheTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.catalog = Catalog(SimpleNamespace(), self.db)

    def parallel_reads(self, params, count=6):
        self.db.block = True
        barrier = threading.Barrier(count)

        def read():
            barrier.wait(3)
            return self.catalog.list(params)

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(read) for _ in range(count)]
            self.assertTrue(self.db.entered.wait(3))
            # The SQL stand-in remains blocked while the released request threads join it.
            time.sleep(0.05)
            self.db.release.set()
            return [future.result(3) for future in futures]

    def test_concurrent_identical_reads_execute_one_database_query(self):
        results = self.parallel_reads({"q": "drama"})
        self.assertEqual(len(self.db.calls), 1)
        self.assertTrue(all(result == results[0] for result in results))
        results[0]["list"][0]["promotion_status"] = "succeeded"
        self.assertNotIn("promotion_status", results[1]["list"][0])

    def test_first_and_cached_results_are_deeply_detached(self):
        first = self.catalog.list({})
        first["list"][0]["promotion_status"] = "user-a-only"
        first["filters"]["languages"].append(99)
        cached = self.catalog.list({})
        self.assertNotIn("promotion_status", cached["list"][0])
        self.assertEqual(cached["filters"]["languages"], [3])
        cached["list"][0]["story_name"] = "mutated"
        self.assertEqual(self.catalog.list({})["list"][0]["story_name"], "version 1")
        self.assertEqual(len(self.db.calls), 1)

    def test_ttl_expires_at_thirty_seconds_after_success(self):
        with patch("features.moboreels_library.catalog.time.monotonic", return_value=100):
            self.catalog.list({})
        with patch("features.moboreels_library.catalog.time.monotonic", return_value=129.999):
            self.catalog.list({})
        self.assertEqual(len(self.db.calls), 1)
        with patch("features.moboreels_library.catalog.time.monotonic", return_value=130):
            self.catalog.list({})
        self.assertEqual(len(self.db.calls), 2)

    def test_failed_reads_are_not_cached_and_can_retry(self):
        self.db.fail = True
        with self.assertRaises(DatabaseError):
            self.catalog.list({})
        self.db.fail = False
        self.assertEqual(self.catalog.list({})["list"][0]["story_name"], "version 2")
        self.catalog.list({})
        self.assertEqual(len(self.db.calls), 2)

    def test_failed_concurrent_read_releases_waiters_and_allows_next_attempt(self):
        self.db.fail = True
        with self.assertRaises(DatabaseError):
            self.parallel_reads({})
        self.assertEqual(len(self.db.calls), 1)
        self.db.fail = False
        self.catalog.list({})
        self.assertEqual(len(self.db.calls), 2)

    def test_sync_generation_changes_invalidate_previous_entries(self):
        for generation in (("run", "running"), ("run", "succeeded"), ("run", "running")):
            self.catalog.list({}, cache_generation=generation)
            self.catalog.list({}, cache_generation=generation)
        self.assertEqual(len(self.db.calls), 3)

    def test_old_generation_inflight_result_does_not_repopulate_new_generation(self):
        original = self.db.list_dramas
        old_entered, old_release = threading.Event(), threading.Event()

        def delayed(params):
            result = original(params)
            if len(self.db.calls) == 1:
                old_entered.set()
                old_release.wait(3)
            return result

        self.db.list_dramas = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.catalog.list, {}, "old")
            self.assertTrue(old_entered.wait(3))
            current = self.catalog.list({}, cache_generation="new")
            old_release.set()
            self.assertEqual(old.result(3)["list"][0]["story_name"], "version 1")
        self.assertEqual(self.catalog.list({}, cache_generation="new"), current)
        self.assertEqual(len(self.db.calls), 2)

    def test_equivalent_parameters_share_cache_but_filters_remain_isolated(self):
        self.catalog.list({"page_index": "01", "page_size": ["50"], "status": "all", "search": " drama ", "lang_id": "3"})
        self.catalog.list({"page_index": 1, "is_show": "-1", "q": ["drama"], "lang_id": 3})
        self.assertEqual(len(self.db.calls), 1)
        for params in ({"is_show": 0}, {"lang_id": -1}, {"local_type": 2}, {"audio_type": 1},
                       {"page_index": 2}, {"page_size": 100}, {"q": "other"}, {}):
            self.catalog.list(params)
        self.assertEqual(len(self.db.calls), 9)
        self.catalog.list({"status": [], "q": [], "search": "ignored"})
        self.assertEqual(len(self.db.calls), 9)

    def test_invalid_parameters_never_hit_cache_or_database(self):
        self.catalog.list({})
        for params in ({"page_size": 101}, {"is_show": "bad"}, {"lang_id": 32768},
                       {"q": "a" * 201}, {"refresh": "bad"}, {"page_index": "bad"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.catalog.list(params)
        self.assertEqual(len(self.db.calls), 1)

    def test_forced_refresh_bypasses_cache_and_coalesces(self):
        self.catalog.list({})
        results = self.parallel_reads({"refresh": "1"})
        self.assertEqual(len(self.db.calls), 2)
        self.assertTrue(all(r["list"][0]["story_name"] == "version 2" for r in results))
        self.assertEqual(self.catalog.list({}), results[0])
        self.assertEqual(len(self.db.calls), 2)

    def test_lru_retains_recent_entry_and_bounds_cache(self):
        for page in range(1, 65):
            self.catalog.list({"page_index": page})
        self.catalog.list({"page_index": 1})
        self.catalog.list({"page_index": 65})
        self.catalog.list({"page_index": 1})
        self.assertEqual(len(self.db.calls), 65)
        self.catalog.list({"page_index": 2})
        self.assertEqual(len(self.db.calls), 66)

    def test_details_are_never_cached(self):
        self.catalog.detail("42")
        self.catalog.detail("42")
        self.assertEqual(self.db.details, 2)

    def test_service_adds_personal_status_after_cache_and_passes_sync_generation(self):
        run = {"run_id": "run", "status": "succeeded", "resume_count": 0}
        sync = SimpleNamespace(latest=lambda: dict(run))
        promotions = SimpleNamespace(recover=lambda: None,
                                     statuses=lambda actor, stories: {"42": actor["user_id"]})
        app = Application(SimpleNamespace(institution_user_id="institution"), db=self.db,
                          provider=object(), sync=sync, promotions=promotions)

        def request(user):
            return app.dispatch({"method": "GET", "path": "/dramas", "actor":
                                 {"tenant_key": "tenant", "user_id": user, "name": user}})[1]

        first = request("user-a")
        second = request("user-b")
        self.assertEqual(first["list"][0]["promotion_status"], "user-a")
        self.assertEqual(second["list"][0]["promotion_status"], "user-b")
        self.assertEqual(len(self.db.calls), 1)
        run["status"] = "running"
        request("user-a")
        run["status"] = "failed"
        request("user-a")
        run["resume_count"] = 1
        request("user-a")
        self.assertEqual(len(self.db.calls), 4)


if __name__ == "__main__":
    unittest.main()

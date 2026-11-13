import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from features.fb_auto_posts.core import StoreError
from features.fb_auto_posts.metric_mysql import GatedMetricReader
from scripts.fb_auto_metric_broker import WindowBroker


class Reader:
    def __init__(self):
        self.version = 1
        self.probes = self.aggregates = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.fail = self.missing = False

    def window_generations(self, product, platform, days, **kwargs):
        self.probes += 1
        return [] if self.missing else [dict(kind="generation", day=day, id=self.version, status="ready") for day in days]

    def window(self, product, platform, days, **kwargs):
        self.aggregates += 1
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("test reader timed out")
        if self.fail:
            raise StoreError("fb_auto_metric_query_timeout", "timeout", 503)
        return [dict(kind="generation", day=day, id=self.version, status="ready") for day in days] + [
            dict(kind="row", content_id="Aa", material_id="1", spend="0.000000000001", af_revenue0=str(self.version))]


class BrokerTests(unittest.TestCase):
    def test_concurrent_identical_windows_share_one_read(self):
        reader = Reader(); reader.release.clear()
        broker = WindowBroker(reader)
        with ThreadPoolExecutor(max_workers=6) as executor:
            tasks = [executor.submit(broker.get, "Dramawave", 0, ["2026-11-12"]) for _ in range(6)]
            self.assertTrue(reader.entered.wait(2))
            deadline = time.monotonic() + 2
            while broker.state()["shared_requests"] < 5 and time.monotonic() < deadline:
                time.sleep(0.005)
            reader.release.set()
            results = [task.result(2) for task in tasks]
        self.assertEqual(reader.probes, 1)
        self.assertEqual(reader.aggregates, 1)
        self.assertEqual(broker.state()["shared_requests"], 5)
        self.assertTrue(all(result == results[0] for result in results))

    def test_cache_revalidates_ready_generation_and_preserves_amounts(self):
        reader = Reader(); broker = WindowBroker(reader)
        first = broker.get("Dramawave", 0, ["2026-11-12"])
        self.assertEqual(first, broker.get("Dramawave", 0, ["2026-11-12"]))
        self.assertEqual((reader.probes, reader.aggregates), (2, 1))
        reader.version = 2
        updated = json.loads(broker.get("Dramawave", 0, ["2026-11-12"]))
        self.assertEqual(updated["rows"][0]["id"], 2)
        self.assertEqual(updated["rows"][1]["spend"], "0.000000000001")
        self.assertEqual(updated["rows"][1]["af_revenue0"], "2")
        reader.missing = True
        with self.assertRaises(StoreError) as raised:
            broker.get("Dramawave", 0, ["2026-11-12"])
        self.assertEqual(raised.exception.code, "fb_auto_metric_window_not_ready")

    def test_pointer_change_during_cold_read_caches_actual_snapshot(self):
        reader = Reader(); broker = WindowBroker(reader)
        original = reader.window
        def updated(*args, **kwargs):
            reader.version = 2
            return original(*args, **kwargs)
        reader.window = updated
        first = broker.get("Dramawave", 0, ["2026-11-12"])
        self.assertEqual(first, broker.get("Dramawave", 0, ["2026-11-12"]))
        self.assertEqual(reader.aggregates, 1)

    def test_errors_do_not_poison_retry_or_leave_slots_occupied(self):
        reader = Reader(); reader.fail = True
        broker = WindowBroker(reader)
        with self.assertRaises(StoreError):
            broker.get("Dramawave", 0, ["2026-11-12"])
        self.assertEqual(broker.state()["active_reads"], 0)
        self.assertEqual(broker.state()["pending_windows"], 0)
        reader.fail = False
        self.assertTrue(json.loads(broker.get("Dramawave", 0, ["2026-11-12"]))["ok"])

    def test_distinct_windows_queue_with_only_two_active_reads(self):
        reader = Reader(); reader.release.clear()
        broker = WindowBroker(reader)
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks = [executor.submit(broker.get, "Dramawave", 0, [day])
                     for day in ("2026-11-10", "2026-11-11", "2026-11-12")]
            deadline = time.monotonic() + 2
            while broker.state()["pending_windows"] < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(broker.state()["active_reads"], 2)
            reader.release.set()
            self.assertTrue(all(json.loads(task.result(3))["ok"] for task in tasks))
        self.assertEqual(reader.aggregates, 3)

    def test_large_result_serves_without_unbounded_cache(self):
        reader = Reader(); broker = WindowBroker(reader, cache_bytes=1)
        self.assertTrue(json.loads(broker.get("Dramawave", 0, ["2026-11-12"]))["ok"])
        self.assertEqual(broker.state()["cached_windows"], 0)

    def test_query_retries_share_total_deadline(self):
        reader = GatedMetricReader({})
        clock = [100.0]
        def failed(statements, *, timeout, execution_ms):
            clock[0] += timeout
            raise StoreError("fb_auto_metric_query_timeout", "", 503)
        with patch("features.fb_auto_posts.metric_mysql.time.monotonic", side_effect=lambda: clock[0]), \
                patch("features.fb_auto_posts.metric_mysql.time.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)), \
                patch.object(reader, "_query_once", side_effect=failed) as query:
            with self.assertRaises(StoreError):
                reader.query([], deadline=155)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(clock[0], 155)


if __name__ == "__main__":
    unittest.main()

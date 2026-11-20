"""A timed-out monitor client must not multiply background scans."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.pipeline_dispatch import views
from features.pipeline_dispatch.core import DispatchError


class MonitorSingleflightTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(path="singleflight-test")
        self.actor = {"user_id": "alice", "tenant_key": "team"}
        views.invalidate_cache(self.store)

    def test_duplicate_scan_is_rejected_then_uses_complete_cache(self):
        entered, finish = threading.Event(), threading.Event()
        replies = []

        def scan(*args):
            entered.set()
            self.assertTrue(finish.wait(3))
            return {"items": [{"count": 125}]}

        with patch.object(views, "_query_snapshot", side_effect=scan) as compute:
            thread = threading.Thread(target=lambda: replies.append(
                views.query(self.store, "summary", self.actor, {})))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(DispatchError) as caught:
                    views.query(self.store, "summary", self.actor, {})
                self.assertEqual(caught.exception.code, "monitor_refresh_in_progress")
            finally:
                finish.set()
                thread.join(3)
            result = views.query(self.store, "summary", self.actor, {})
            self.assertEqual(result, replies[0])
            result["items"].clear()
            self.assertEqual(len(replies[0]["items"]), 1)
            self.assertEqual(compute.call_count, 1)

    def test_failed_scan_releases_slot_and_actors_have_separate_cache(self):
        with patch.object(views, "_query_snapshot", side_effect=RuntimeError("read failed")):
            with self.assertRaises(RuntimeError):
                views.query(self.store, "tasks", self.actor, {})
        with patch.object(views, "_query_snapshot", return_value={"items": []}) as compute:
            views.query(self.store, "tasks", self.actor, {})
            views.query(self.store, "tasks", dict(self.actor, user_id="bob"), {})
            self.assertEqual(compute.call_count, 2)


if __name__ == "__main__":
    unittest.main()

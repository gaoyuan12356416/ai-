#!/usr/bin/env python3
"""Run details remain readable after prolonged canonical-result polling."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.core import XAutoPostStore
from features.x_auto_posts.publisher import AutoLiveGates
from features.x_auto_posts.service import MAX_RESPONSE_BYTES, RUN_EVENT_LIMIT, XAutoPostService
from scripts.test_x_auto_post_service import Clock, FakeBridge, FakeExecutor, FakeSelector
from scripts.test_x_auto_post_validation import valid_payload


class RunDetailSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.store = XAutoPostStore(Path(self.temp.name) / "auto.sqlite3", now_fn=self.clock)
        bridge = FakeBridge()
        sample = bridge.accounts_by_id["101"]
        bridge.accounts_by_id = {
            str(i): dict(sample, id=i, x_user_id="x-" + str(i), username="account" + str(i))
            for i in range(101, 186)
        }
        self.service = XAutoPostService(
            self.store, bridge, FakeSelector(), FakeExecutor(AutoLiveGates(True, True, True)),
            now_fn=self.clock, runner_kick_path=Path(self.temp.name) / "kick",
        )
        template = self.service.create_template(valid_payload(account_ids=list(bridge.accounts_by_id)))["template"]
        self.run_id = self.service.run_now(template["id"], {
            "expected_version": 1, "confirmed": True, "idempotency_key": "detail-size-0001",
        })["run_id"]

    def test_29513_events_keep_all_85_tasks_and_fit_response_limit(self):
        with contextlib.closing(sqlite3.connect(self.store.db_path)) as conn:
            existing = conn.execute("SELECT count(*) FROM x_auto_event WHERE run_id=?", (self.run_id,)).fetchone()[0]
            conn.executemany(
                "INSERT INTO x_auto_event(run_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                ((self.run_id, "task_claimed_reconcile", '{"worker_id":"reconcile-worker","generation":1}',
                  "2026-11-04T08:00:00Z") for _ in range(29513 - existing)),
            )
            task_id = conn.execute("SELECT min(id) FROM x_auto_task WHERE run_id=?", (self.run_id,)).fetchone()[0]
            conn.execute("UPDATE x_auto_task SET status='published',publish_id='12345',publish_url='https://x.com/account101/status/12345' WHERE id=?", (task_id,))
            conn.execute("UPDATE x_auto_task SET status='failed',error_code='x_post_account_needs_review',error_message='Account needs review',execution_queue_id=5393,execution_log_id=5327 WHERE id=?", (task_id + 1,))
            conn.commit()
            last_id = conn.execute("SELECT max(id) FROM x_auto_event WHERE run_id=?", (self.run_id,)).fetchone()[0]
        with patch.object(self.store, "list_events", side_effect=AssertionError("must not load full audit trail")):
            payload = self.service.run(self.run_id)
        self.assertEqual(len(payload["tasks"]), 85)
        self.assertEqual(payload["tasks"], payload["account_tasks"])
        self.assertEqual(payload["tasks"][0]["publish_url"], "https://x.com/account101/status/12345")
        self.assertEqual(payload["tasks"][1]["error_code"], "x_post_account_needs_review")
        self.assertEqual(payload["tasks"][1]["execution_queue_id"], 5393)
        self.assertEqual(payload["tasks"][1]["execution_log_id"], 5327)
        self.assertEqual(payload["events_total"], 29513)
        self.assertEqual(payload["events_limit"], RUN_EVENT_LIMIT)
        self.assertTrue(payload["events_has_more"])
        self.assertEqual([event["id"] for event in payload["events"]], list(range(last_id - RUN_EVENT_LIMIT + 1, last_id + 1)))
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), MAX_RESPONSE_BYTES)
        self.assertEqual(len(self.store.list_events(run_id=self.run_id)), 29513)

    def test_small_history_is_complete_and_large_event_details_are_explicitly_bounded(self):
        before = self.service.run(self.run_id)
        self.assertFalse(before["events_has_more"])
        self.assertEqual(before["events_total"], len(before["events"]))
        large_details = json.dumps({"diagnostics": "长" * 10000}, ensure_ascii=False)
        with contextlib.closing(sqlite3.connect(self.store.db_path)) as conn:
            conn.execute("INSERT INTO x_auto_event(run_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                         (self.run_id, "operator_review", large_details, "2026-11-04T08:00:00Z"))
            conn.commit()
        event = self.service.run(self.run_id)["events"][-1]
        self.assertEqual(event["event_type"], "operator_review")
        self.assertTrue(event["details_truncated"])
        self.assertIn("审计记录", event["details"]["summary"])
        self.assertEqual(self.store.list_events(run_id=self.run_id)[-1]["details"], json.loads(large_details))


if __name__ == "__main__":
    unittest.main()

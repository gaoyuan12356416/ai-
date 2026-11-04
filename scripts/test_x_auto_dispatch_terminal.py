"""A distributed zero-write failure must not remain in Auto's reconcile loop."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_auto_posts.publisher import read_dispatch_queue_state
from scripts import test_x_auto_post_publisher as fixtures


class DispatchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dispatch.sqlite3"
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript("""
                CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,status TEXT,
                  generation INTEGER,external_intent INTEGER,error_code TEXT,error_message TEXT,
                  canonical_receipt TEXT,updated_at TEXT);
                CREATE TABLE pipeline_operation(job_id INTEGER,generation INTEGER,operation TEXT);
                CREATE TABLE pipeline_publication(job_id INTEGER);
                CREATE TABLE pipeline_receipt(job_id INTEGER);
                CREATE TABLE pipeline_delivery(job_id INTEGER,response_json TEXT);
                INSERT INTO pipeline_job VALUES(1,'x:queue:9002','prepare','failed',1,0,
                  'x_account_not_publishable','','','2026-11-04T02:20:48Z');
            """)

    def change(self, sql):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute(sql)

    def read(self):
        return read_dispatch_queue_state(self.path, 9002)

    def test_failed_ack_is_not_a_publication_receipt(self):
        self.change("INSERT INTO pipeline_delivery VALUES(1,'{\"accepted\":true,\"status\":\"failed\"}')")
        state = self.read()
        self.assertTrue(state["known_failure"])
        self.assertEqual(state["error_code"], "x_account_not_publishable")
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT status FROM pipeline_job").fetchone()[0], "failed")

    def test_any_historical_operation_preserves_unknown_reconciliation(self):
        self.change("UPDATE pipeline_job SET generation=3")
        self.change("INSERT INTO pipeline_operation VALUES(1,1,'x:POST:/2/media/upload/initialize')")
        self.assertFalse(self.read()["known_failure"])

    def test_publication_or_success_receipt_rejects_known_failure(self):
        for table in ("pipeline_publication", "pipeline_receipt"):
            with self.subTest(table=table):
                self.change("INSERT INTO " + table + " VALUES(1)")
                self.assertFalse(self.read()["known_failure"])
                self.change("DELETE FROM " + table)

    def test_unknown_and_intent_remain_unresolved(self):
        self.change("UPDATE pipeline_job SET status='unknown'")
        self.assertFalse(self.read()["known_failure"])
        self.change("UPDATE pipeline_job SET status='failed',external_intent=1")
        self.assertFalse(self.read()["known_failure"])

    def test_newer_publish_stage_wins_over_failed_preparation(self):
        self.change("INSERT INTO pipeline_job VALUES(2,'x:queue:9002','publish','running',2,0,'','','','later')")
        state = self.read()
        self.assertFalse(state["known_failure"])
        self.assertEqual(state["job_id"], 2)

    def test_completed_prepare_receipt_does_not_hide_zero_write_publish_failure(self):
        self.change("UPDATE pipeline_job SET status='completed'")
        self.change("INSERT INTO pipeline_receipt VALUES(1)")
        self.change("INSERT INTO pipeline_job VALUES(2,'x:queue:9002','publish','failed',1,0,'x_account_not_publishable','','','later')")
        self.assertTrue(self.read()["known_failure"])

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            read_dispatch_queue_state(missing, 9002)
        self.assertFalse(missing.exists())


class AutoDispatchTerminalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.XAutoPostPublisherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.task = self.fixture._ready()
        self.bridge = fixtures.FakeBridge(self.task)
        self.executor = self.fixture._executor(self.bridge)
        self.executor.dispatch_enabled = True
        self.executor.dispatch_state_reader = lambda _queue: {
            "source_key": "x:queue:9002", "job_id": 32490, "stage": "prepare",
            "generation": 1, "status": "failed", "known_failure": True,
            "error_code": "x_account_not_publishable", "error_message": "", "updated_at": "fixed",
        }

    def run_one(self):
        result = self.executor.execute_next("dispatch-observer")
        self.assertEqual(self.bridge.publish_calls, 0)
        self.assertEqual(self.bridge.record_calls, 0)
        return result["task"]

    def test_zero_write_failure_finishes_task_and_run_with_original_error(self):
        task = self.run_one()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error_code"], "x_account_not_publishable")
        self.assertFalse(task["unknown_outcome"])
        self.assertEqual(self.fixture.store.get_run(self.task.run_id).status, "failed")
        self.assertTrue(self.fixture.store.material_is_reserved("123456"))

    def test_coordinator_evidence_unavailable_keeps_conservative_observation(self):
        for exc in (FileNotFoundError("missing"), PermissionError("denied"), sqlite3.OperationalError("locked")):
            with self.subTest(error=type(exc).__name__):
                self.executor.dispatch_state_reader = mock.Mock(side_effect=exc)
                task = self.run_one()
                self.assertEqual(task["status"], "retry_wait")
                self.fixture.clock.value += fixtures.timedelta(minutes=1)

    def test_wrong_queue_evidence_never_terminalizes(self):
        self.executor.dispatch_state_reader = lambda _queue: {"source_key": "x:queue:other", "known_failure": True}
        self.assertEqual(self.run_one()["status"], "retry_wait")

    def test_canonical_unknown_wins_over_coordinator_failure(self):
        self.bridge.queue_unknown = True
        task = self.run_one()
        self.assertEqual(task["status"], "retry_wait")
        self.assertTrue(task["unknown_outcome"])

    def test_canonical_success_wins_over_coordinator_failure(self):
        self.bridge.queue_status = "published"
        task = self.run_one()
        self.assertEqual(task["status"], "published")
        self.assertEqual(task["publish_id"], "1900000000000000000")

    def test_upload_in_progress_is_not_a_zero_write_failure(self):
        self.bridge.queue_status = "publishing"
        self.assertEqual(self.run_one()["status"], "retry_wait")


if __name__ == "__main__":
    unittest.main()

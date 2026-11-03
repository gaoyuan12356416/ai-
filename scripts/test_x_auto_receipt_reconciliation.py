"""Confirmed receipt repair is exact, atomic, idempotent and publication-free."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.receipt_reconciliation import (
    ReceiptReconciliationError, apply_plan, build_plan,
)
from scripts import test_x_auto_post_publisher as fixtures


class ReceiptReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.XAutoPostPublisherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        task = self.fixture._ready()
        body = task.body_template
        self.task = self.store.transition_task(task.id, "failed", updates={
            "unknown_outcome": True, "publish_attempt_count": 1, "preparation_attempt_count": 2,
            "error_code": "x_auto_publish_outcome_unknown",
        })
        self.store.set_run_status(task.run_id, "failed")
        self.native = Path(self.fixture.temp.name) / "native.sqlite3"
        with sqlite3.connect(self.native) as c:
            c.executescript("""
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,manual_run_id INTEGER,account_id INTEGER,
                    material_id TEXT,content_id TEXT,body_template TEXT,status TEXT);
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,account_id INTEGER,
                    status TEXT,unknown_outcome INTEGER,x_post_id TEXT,x_post_url TEXT,published_at TEXT,attempt_count INTEGER);
                CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,trigger_source TEXT,external_task_key TEXT,
                    template_ref TEXT,template_version INTEGER,account_ids_json TEXT,material_ids_json TEXT,body_template TEXT);
            """)
            c.execute("INSERT INTO x_post_queue VALUES(9002,9001,640,'123456','DramaOne',?,'published')", (body,))
            c.execute("INSERT INTO x_post_publish_log VALUES(9003,9002,640,'published',0,'1900000000000000000','https://x.com/example/status/1900000000000000000','2026-08-11T10:30:00Z',1)")
            c.execute("INSERT INTO x_post_manual_run VALUES(9001,'auto_template',?,?,?,'[640]','[\"123456\"]',?)",
                      ("x-auto-task-" + str(task.id), "x-auto-template-" + str(task.template_id), task.template_version, body))

    def plan(self):
        return build_plan(self.store.db_path, self.native, [self.task.id])

    def apply(self, plan, suffix="backup"):
        return apply_plan(plan, self.store.db_path, self.native, Path(self.fixture.temp.name) / suffix)

    def mutate_native(self, sql, params=()):
        with sqlite3.connect(self.native) as c:
            c.execute(sql, params)

    def test_exact_confirmed_receipt_corrects_auto_only_and_is_idempotent(self):
        native_before = self.native.read_bytes()
        plan = self.plan()
        self.assertEqual(self.store.get_task(self.task.id).status, "failed")
        result = self.apply(plan)
        actual = self.store.get_task(self.task.id)
        self.assertEqual(actual.status, "published")
        self.assertEqual(actual.publish_id, "1900000000000000000")
        self.assertEqual(actual.published_at_utc, "2026-08-11T10:30:00+00:00")
        self.assertFalse(actual.unknown_outcome)
        with self.store._reader() as c:
            counts = c.execute("SELECT publish_attempt_count,preparation_attempt_count FROM x_auto_task WHERE id=?", (actual.id,)).fetchone()
        self.assertEqual(tuple(counts), (1, 2))
        self.assertEqual(self.store.get_run(actual.run_id).status, "completed")
        self.assertEqual(result["publish_requests"], 0)
        self.assertEqual(self.native.read_bytes(), native_before)
        with sqlite3.connect(result["backup"]) as c:
            self.assertEqual(c.execute("SELECT status FROM x_auto_task WHERE id=?", (actual.id,)).fetchone()[0], "failed")
            self.assertEqual(c.execute("PRAGMA quick_check").fetchall(), [("ok",)])
        if os.name != "nt":
            self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(result["backup"]).parent.stat().st_mode & 0o777, 0o700)
        again = self.apply(plan, "backup-again")
        self.assertEqual(again["changed_task_ids"], [])
        self.assertEqual(again["already_published_task_ids"], [actual.id])
        with self.store._reader() as c:
            events = c.execute("SELECT details_json FROM x_auto_event WHERE event_type='task_confirmed_receipt_reconciled'").fetchall()
        self.assertEqual(len(events), 1)
        audit = json.loads(events[0][0])
        self.assertEqual(audit["previous"]["unknown_outcome"], 1)
        self.assertEqual(audit["receipt"]["queue_id"], 9002)

    def test_existing_backup_directory_blocks_reapplication_without_more_writes(self):
        plan = self.plan()
        self.apply(plan)
        with self.store._reader() as c:
            before = c.execute("SELECT count(*) FROM x_auto_event").fetchone()[0]
        with self.assertRaisesRegex(ReceiptReconciliationError, "backup already exists"):
            self.apply(plan)
        with self.store._reader() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_auto_event").fetchone()[0], before)

    def test_running_run_receives_finish_time_when_reconciliation_completes_it(self):
        with self.store._transaction() as c:
            c.execute("UPDATE x_auto_run SET status='running',finished_at_utc='' WHERE id=?", (self.task.run_id,))
        result = self.apply(self.plan())
        actual = self.store.get_run(self.task.run_id)
        self.assertEqual(actual.status, "completed")
        self.assertEqual(actual.finished_at_utc, result["at"])

    def test_nonterminal_sibling_keeps_run_running_and_clears_stale_finish_time(self):
        sibling = self.store.create_task(run_id=self.task.run_id, account_id="641", language="en",
                                        account_snapshot_version=1, account_snapshot={"username": "another"})
        before = self.store.get_task(sibling.id)
        self.apply(self.plan())
        actual = self.store.get_run(self.task.run_id)
        self.assertEqual(actual.status, "running")
        self.assertEqual(actual.finished_at_utc, "")
        self.assertEqual(self.store.get_task(sibling.id), before)

    def test_unknown_canonical_receipt_is_never_corrected_or_replayed(self):
        self.mutate_native("UPDATE x_post_publish_log SET unknown_outcome=1")
        with self.assertRaisesRegex(ReceiptReconciliationError, "not confirmed"):
            self.plan()
        self.assertEqual(self.store.get_task(self.task.id).status, "failed")

    def test_every_frozen_identity_must_match(self):
        cases = [
            ("UPDATE x_post_queue SET account_id=999", "identity mismatch"),
            ("UPDATE x_post_queue SET material_id='other'", "identity mismatch"),
            ("UPDATE x_post_queue SET content_id='other'", "identity mismatch"),
            ("UPDATE x_post_manual_run SET external_task_key='another'", "envelope mismatch"),
            ("UPDATE x_post_manual_run SET template_version=99", "envelope mismatch"),
            ("UPDATE x_post_manual_run SET material_ids_json='[\"other\"]'", "envelope mismatch"),
            ("UPDATE x_post_publish_log SET account_id=999", "identity mismatch"),
            ("UPDATE x_post_queue SET body_template='another'", "body template mismatch"),
            ("UPDATE x_post_publish_log SET x_post_url='https://x.com/a/status/999'", "URL does not match"),
            ("UPDATE x_post_publish_log SET x_post_id=''", "Post ID missing"),
        ]
        baseline = self.native.read_bytes()
        for sql, error in cases:
            with self.subTest(sql=sql):
                self.mutate_native(sql)
                with self.assertRaisesRegex(ReceiptReconciliationError, error):
                    self.plan()
                self.native.write_bytes(baseline)

    def test_plan_drift_rejected_before_any_task_write(self):
        plan = self.plan()
        with self.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET error_code='changed' WHERE id=?", (self.task.id,))
        with self.assertRaisesRegex(ReceiptReconciliationError, "task changed"):
            self.apply(plan)
        self.assertEqual(self.store.get_task(self.task.id).status, "failed")

    def test_canonical_drift_rejected_before_any_task_write(self):
        plan = self.plan()
        self.mutate_native("UPDATE x_post_publish_log SET status='failed',unknown_outcome=1")
        with self.assertRaisesRegex(ReceiptReconciliationError, "not confirmed"):
            self.apply(plan)
        self.assertEqual(self.store.get_task(self.task.id).status, "failed")

    def test_active_claim_is_not_overwritten(self):
        with self.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET claim_token='live',claim_worker='worker' WHERE id=?", (self.task.id,))
        with self.assertRaisesRegex(ReceiptReconciliationError, "claim"):
            self.plan()

    def test_multiple_native_receipts_are_rejected(self):
        self.mutate_native("INSERT INTO x_post_publish_log SELECT 9004,queue_id,account_id,status,unknown_outcome,x_post_id,x_post_url,published_at,attempt_count FROM x_post_publish_log")
        with self.assertRaisesRegex(ReceiptReconciliationError, "unique"):
            self.plan()

    def test_changed_plan_digest_is_rejected(self):
        plan = self.plan()
        plan["task_ids"].append(99)
        with self.assertRaisesRegex(ReceiptReconciliationError, "digest"):
            self.apply(plan)

    def test_duplicate_selection_rejected(self):
        with self.assertRaisesRegex(ReceiptReconciliationError, "distinct"):
            build_plan(self.store.db_path, self.native, [self.task.id, self.task.id])


class LateDistributedReceiptTests(unittest.TestCase):
    def test_unknown_parent_stays_read_only_until_native_success_arrives(self):
        fixture = fixtures.XAutoPostPublisherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = fixture._ready()
        bridge = fixtures.FakeBridge(task)
        bridge.queue_status = "failed"
        bridge.queue_unknown = True
        bridge.query_run = lambda _run_id: bridge._run(status="needs_review")
        bridge.recover_run = lambda _run_id: self.fail("must not invoke recovery")
        executor = fixture._executor(bridge)
        executor.dispatch_enabled = True
        first = executor.execute_next("observer")
        self.assertEqual(first["task"]["status"], "retry_wait")
        self.assertTrue(first["task"]["unknown_outcome"])
        with fixture.store._reader() as c:
            phase = c.execute("SELECT claim_phase FROM x_auto_task WHERE id=?", (task.id,)).fetchone()[0]
        self.assertEqual(phase, "reconcile")
        bridge.queue_status = "published"
        bridge.queue_unknown = False
        fixture.clock.value += timedelta(minutes=6)
        second = executor.execute_next("observer")
        self.assertEqual(second["task"]["status"], "published")
        self.assertEqual(bridge.publish_calls, 0)
        self.assertEqual(bridge.record_calls, 0)


if __name__ == "__main__":
    unittest.main()

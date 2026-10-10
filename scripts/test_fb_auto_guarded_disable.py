"""Atomic task preservation for schedule cutovers; no network or live state."""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.service import Handler, Runtime
from scripts.test_fb_auto_store import Materials, Pages
from scripts.test_fb_auto_validation import payload


class GuardedDisableTests(unittest.TestCase):
    cutoff = "2026-08-17T16:00:00+00:00"
    future = "2026-08-18T02:30:00+00:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 8, 17, 2, 30, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(self.tmp.name) / "guard.sqlite3", now_fn=lambda: self.now)
        self.actor = ActorScope("u", "test", False, "248")
        self.template = self.store.create_template(payload(), self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(self.template["id"], True, self.actor, 1)

    def snapshot(self):
        with self.store.connect() as conn:
            return tuple(conn.iterdump())

    def disable(self, **kwargs):
        return self.store.set_template_status(self.template["id"], False, self.actor, 1, preserve_unsubmitted_before_utc=self.cutoff, **kwargs)

    def seed_task(self):
        run = self.store.create_run(self.template["id"], "manual:fixture", "manual", self.actor, Pages(), Materials())
        with self.store.connect() as conn:
            task_id = conn.execute("SELECT id FROM fb_auto_task WHERE run_id=? AND status='planned'", (run["run_id"],)).fetchone()[0]
            conn.execute("UPDATE fb_auto_run SET trigger_type='auto' WHERE id=?", (run["run_id"],))
            conn.execute("UPDATE fb_auto_task SET planned_publish_at_utc=? WHERE id=?", (self.future, task_id))
        return run["run_id"], task_id

    def test_all_protected_task_states_reject_without_any_writes(self):
        run_id, task_id = self.seed_task()
        cases = [("auto", status, self.future) for status in ("preparing", "running")]
        cases += [("auto", status, when) for status in ("queued", "planned", "ready") for when in ("2026-08-17T02:30:00+00:00", "2026-08-16T23:00:00+00:00", "", "invalid")]
        cases += [("manual", status, self.future) for status in ("queued", "planned", "preparing", "ready")]
        for trigger, status, when in cases:
            with self.subTest(trigger=trigger, status=status, when=when):
                with self.store.connect() as conn:
                    conn.execute("UPDATE fb_auto_run SET trigger_type=? WHERE id=?", (trigger, run_id))
                    conn.execute("UPDATE fb_auto_task SET status=?,planned_publish_at_utc=? WHERE id=?", (status, when, task_id))
                before = self.snapshot()
                with self.assertRaises(StoreError) as caught:
                    self.disable()
                self.assertEqual((caught.exception.code, caught.exception.status), ("fb_auto_disable_unsubmitted_conflict", 409))
                self.assertEqual(self.snapshot(), before)

    def test_due_slot_guards_reject_without_cancelling_or_releasing_leases(self):
        queued = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="guarded-disable-due-0001")
        cases = [("manual", status, self.future) for status in ("pending", "preparing")]
        cases += [("auto", "preparing", self.future)]
        cases += [("auto", "pending", when) for when in ("2026-08-17T02:30:00+00:00", "", "invalid")]
        for trigger, status, when in cases:
            with self.subTest(trigger=trigger, status=status, when=when):
                with self.store.connect() as conn:
                    conn.execute("UPDATE fb_auto_due_slot SET trigger_type=?,status=?,planned_publish_at_utc=?,lease_owner='original-worker',lease_expires_at_utc=? WHERE id=?", (trigger, status, when, self.future, queued["due_slot_id"]))
                before = self.snapshot()
                with self.assertRaises(StoreError) as caught:
                    self.disable()
                self.assertEqual((caught.exception.code, caught.exception.status), ("fb_auto_disable_unsubmitted_conflict", 409))
                self.assertEqual(self.snapshot(), before)

    def test_future_planned_ready_and_pending_auto_work_remain_unchanged(self):
        run_id, task_id = self.seed_task()
        queued = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="guarded-disable-future-0001")
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_due_slot SET trigger_type='auto',planned_publish_at_utc=? WHERE id=?", (self.cutoff, queued["due_slot_id"]))
        for status in ("planned", "ready"):
            with self.subTest(status=status):
                self.store.set_template_status(self.template["id"], True, self.actor, 1)
                with self.store.connect() as conn:
                    conn.execute("UPDATE fb_auto_task SET status=?,planned_publish_at_utc=? WHERE id=?", (status, self.cutoff, task_id))
                    task_before = tuple(conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (task_id,)).fetchone())
                    due_before = tuple(conn.execute("SELECT * FROM fb_auto_due_slot WHERE id=?", (queued["due_slot_id"],)).fetchone())
                    run_before = tuple(conn.execute("SELECT * FROM fb_auto_run WHERE id=?", (run_id,)).fetchone())
                disabled = self.disable()
                self.assertEqual((disabled["status"], disabled["version"]), ("disabled", 1))
                with self.store.connect() as conn:
                    self.assertEqual(tuple(conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()), task_before)
                    self.assertEqual(tuple(conn.execute("SELECT * FROM fb_auto_due_slot WHERE id=?", (queued["due_slot_id"],)).fetchone()), due_before)
                    self.assertEqual(tuple(conn.execute("SELECT * FROM fb_auto_run WHERE id=?", (run_id,)).fetchone()), run_before)
                self.assertIsNone(self.store.claim_prepare_next("disabled-prepare"))
                self.assertIsNone(self.store.claim_next("disabled-publish"))
                self.assertIsNone(self.store.claim_due_slot("disabled-plan"))

    def test_cutoff_requires_timezone_and_is_disable_only(self):
        for invalid in ("", "bad", "2026-08-17", "2026-08-17T16:00:00", 123, {}, False):
            with self.subTest(invalid=invalid):
                before = self.snapshot()
                with self.assertRaises(StoreError) as caught:
                    self.store.set_template_status(self.template["id"], False, self.actor, 1, preserve_unsubmitted_before_utc=invalid)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StoreError) as caught:
            self.store.set_template_status(self.template["id"], True, self.actor, 1, preserve_unsubmitted_before_utc=self.cutoff)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.set_template_status(self.template["id"], False, self.actor, 1, preserve_unsubmitted_before_utc="2026-08-18T00:00:00+08:00")["status"], "disabled")

    def test_concurrent_manual_enqueue_waits_for_atomic_disable_then_rejects(self):
        other = FBAutoPostStore(self.store.path, now_fn=lambda: self.now)
        attempted, outcomes = threading.Event(), []
        original_connect = other.connect
        original_row = self.store._template_row
        workers = []

        def tracked_connect():
            conn = original_connect()
            conn.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
            return conn

        def enqueue():
            try:
                other.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="guarded-disable-racing-0001")
                outcomes.append("unexpected-success")
            except StoreError as error:
                outcomes.append(error.code)

        def locked_row(conn, template_id, actor):
            row = original_row(conn, template_id, actor)
            if not workers:
                self.assertTrue(conn.in_transaction)
                worker = threading.Thread(target=enqueue)
                workers.append(worker)
                worker.start()
                self.assertTrue(attempted.wait(2), "concurrent enqueue did not reach BEGIN IMMEDIATE")
            return row

        try:
            with patch.object(other, "connect", tracked_connect), patch.object(self.store, "_template_row", locked_row):
                self.assertEqual(self.disable()["status"], "disabled")
                workers[0].join(3)
        finally:
            for worker in workers:
                worker.join(3)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(outcomes, ["fb_auto_manual_template_disabled"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_due_slot").fetchone()[0], 0)

    def request(self, action, body):
        token = "x" * 32
        runtime = Runtime(self.store, object(), object(), SimpleNamespace(live_enabled=True), SimpleNamespace(), token)
        handler = object.__new__(Handler)
        handler.path = "/api/admin/fb-auto-publish/templates/1/" + action
        handler.command = "POST"
        handler.headers = {"Authorization": "Bearer " + token}
        handler.runtime = runtime
        actor = {"user_id": self.actor.user_id, "name": self.actor.name, "is_admin": self.actor.is_admin, "owner_user_id": self.actor.owner_user_id}
        handler.read_json = lambda: {**json.loads(json.dumps(body)), "_actor": actor}
        responses = []
        handler.send_json = lambda status, data: responses.append((status, data))
        handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_handler_forwards_guard_and_refuses_enable_or_null(self):
        self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="guarded-handler-manual-0001")
        before = self.snapshot()
        status, body = self.request("disable", {"expected_version": 1, "preserve_unsubmitted_before_utc": self.cutoff})
        self.assertEqual((status, body["code"]), (409, "fb_auto_disable_unsubmitted_conflict"))
        self.assertEqual(self.snapshot(), before)
        for action, cutoff in (("enable", self.cutoff), ("disable", None), ("disable", "2026-08-17T16:00:00")):
            with self.subTest(action=action, cutoff=cutoff):
                self.assertEqual(self.request(action, {"expected_version": 1, "preserve_unsubmitted_before_utc": cutoff})[0], 400)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.request("disable", {"expected_version": 1})[0], 200)
        with self.store.connect() as conn:
            self.assertEqual(tuple(conn.execute("SELECT status,error_code FROM fb_auto_due_slot").fetchone()), ("failed", "fb_auto_manual_template_disabled"))


if __name__ == "__main__":
    unittest.main()

"""Canonical X planner holds preserve reservations and all claim paths."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.core import XAutoPostStoreError
from features.x_auto_posts.service import XAutoPostRequestHandler, AutoPostServiceError
from scripts import test_x_auto_post_publisher as fixtures


class AutoHoldTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.XAutoPostPublisherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.task, self.clock = self.fixture.store, self.fixture.task, self.fixture.clock

    def test_pending_hold_blocks_both_claim_paths_without_dispatch_mode(self):
        self.assertTrue(self.store.pause_pipeline_task(self.task.id, self.clock())["held"])
        self.store.pause_pipeline_task(self.task.id, self.clock())
        self.assertEqual(self.store.get_task(self.task.id).status, "pending")
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "0"}):
            self.assertIsNone(self.store.claim_next_executable_task(worker_id="old-runner", lease_seconds=120))
            self.assertIsNone(self.store.claim_next_pending_task(worker_id="old-selector", lease_seconds=120))
        with self.assertRaises(XAutoPostStoreError):
            self.store.pause_pipeline_task(self.task.id, self.clock() + dt.timedelta(seconds=1))

    def test_reserved_hold_preserves_material_and_does_not_block_new_same_account(self):
        token, reserved = self.fixture._reserve()
        self.store.release_task_claim(reserved.id, token)
        before = self.store.get_task_reservation(reserved.id)
        self.store.pause_pipeline_task(reserved.id, self.clock())
        self.assertEqual(self.store.get_task_reservation(reserved.id), before)
        self.clock.value += dt.timedelta(seconds=1)
        run = self.store.create_run(run_key="manual:after-cutover", template_id=self.fixture.template.id,
            template_version=self.fixture.template.version, trigger_type="manual", scheduled_at_utc=self.clock(),
            shanghai_date="2026-08-11", publish_time="19:01", blacklist_snapshot={"sha256": "a" * 64}, actor=self.fixture.actor)
        task = self.store.create_task(run_id=run.id, account_id="640", language="en",
            account_snapshot_version=1, account_snapshot={"username": "example"})
        claim = self.store.claim_next_executable_task(worker_id="next", lease_seconds=120)
        self.assertEqual(claim.task.id, task.id)

    def test_active_and_attempted_or_bridged_tasks_cannot_be_paused(self):
        token, task = self.fixture._reserve()
        with self.assertRaises(XAutoPostStoreError): self.store.pause_pipeline_task(task.id, self.clock())
        self.store.release_task_claim(task.id, token)
        for assignment in ("preparation_attempt_count=1", "publish_attempt_count=1", "execution_queue_id=9", "unknown_outcome=1"):
            with self.store._transaction() as conn:
                conn.execute("UPDATE x_auto_task SET " + assignment + " WHERE id=?", (task.id,))
            with self.assertRaises(XAutoPostStoreError): self.store.pause_pipeline_task(task.id, self.clock())
            with self.store._transaction() as conn:
                conn.execute("UPDATE x_auto_task SET preparation_attempt_count=0,publish_attempt_count=0,execution_queue_id=NULL,unknown_outcome=0 WHERE id=?", (task.id,))

    def test_pause_rpc_requires_original_internal_token_switch_and_exact_identity(self):
        body = {"job": {"channel": "x", "family": "auto", "stage": "prepare", "source_id": str(self.task.id),
            "source_key": "x:auto:" + str(self.task.id)}, "cutoff": self.clock().isoformat()}
        handler = XAutoPostRequestHandler.__new__(XAutoPostRequestHandler)
        handler.command, handler.path = "POST", "/internal/pipeline-dispatch/x-auto/pause"
        handler.server = SimpleNamespace(x_auto_service=SimpleNamespace(store=self.store), internal_token="internal-token")
        handler.client_address = ("127.0.0.1", 123)
        def dispatch(value, bearer="internal-token"):
            raw = json.dumps(value).encode()
            handler.headers = {"Authorization": "Bearer " + bearer, "Content-Length": str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            return handler._dispatch()
        with mock.patch.dict(os.environ, {"PIPELINE_X_CANONICAL_BRIDGE_ENABLED": "0"}):
            with self.assertRaises(AutoPostServiceError): dispatch(body)
        with mock.patch.dict(os.environ, {"PIPELINE_X_CANONICAL_BRIDGE_ENABLED": "1"}):
            self.assertEqual(dispatch(body, "wrong")[0], 403)
            with self.assertRaises(AutoPostServiceError): dispatch({**body, "job": {**body["job"], "source_key": "x:auto:999"}})
            self.assertEqual(dispatch(body)[0], 200)


if __name__ == "__main__":
    unittest.main()

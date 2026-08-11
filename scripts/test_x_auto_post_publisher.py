#!/usr/bin/env python3
"""Crash/retry contracts for the isolated X auto-template executor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.core import AuditActor, XAutoPostStore  # noqa: E402
from features.x_auto_posts.publisher import (  # noqa: E402
    AutoLiveGates,
    AutoPostExecutionError,
    AutoPostExecutor,
)
from features.x_auto_posts.x_sidecar import XPostBridgeError  # noqa: E402


UTC = timezone.utc


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 11, 11, 0, tzinfo=UTC)

    def __call__(self):
        return self.value


class DummySelector:
    source = None
    metrics = None
    history = None
    material_validator = None
    product = "Dramawave"
    app_id = "dramawave"
    material_data_source = "Dramawave"


class FakeBridge:
    def __init__(self, task):
        self.task = task
        self.queue_status = "queued"
        self.queue_unknown = False
        self.record_error = None
        self.publish_error = None
        self.has_queue = True
        self.recover_status = "running"
        self.recover_busy = False
        self.publish_calls = 0
        self.query_calls = 0
        self.record_calls = 0

    def _run(self, *, status="running", with_queue=None):
        if with_queue is None:
            with_queue = self.has_queue
        queues = []
        if with_queue:
            queue = {
                "id": 9002,
                "manual_run_id": 9001,
                "queue_status": self.queue_status,
                "status": self.queue_status,
                "unknown_outcome": self.queue_unknown,
                "preflight_duration": 120.0,
            }
            if self.queue_status == "published":
                queue.update(
                    {
                        "log_id": 9003,
                        "post_id": "1900000000000000000",
                        "preview_url": "https://x.com/example/status/1900000000000000000",
                    }
                )
            elif self.queue_status == "failed":
                queue.update(
                    {
                        "error_code": "x_post_media_failed",
                        "error_message": "canonical X queue failed",
                    }
                )
            queues.append(queue)
        return {
            "id": 9001,
            "trigger_source": "auto_template",
            "external_task_key": "x-auto-task-%d" % self.task.id,
            "template_ref": "x-auto-template-%d" % self.task.template_id,
            "template_version": self.task.template_version,
            "account_ids": [int(self.task.account_id)],
            "material_ids": [str(self.task.material_id)],
            "body_template": self.task.body_template,
            "status": status,
            "source_date": "2026-08-10",
            "queues": queues,
        }

    def record_failure(self, run_id, error_code, error_message):
        self.record_calls += 1
        if self.record_error is not None:
            raise self.record_error
        return self._run(status="failed_preflight", with_queue=False)

    def publish_queue(self, queue_id):
        self.publish_calls += 1
        if self.publish_error is not None:
            raise self.publish_error
        self.queue_status = "publishing"
        self.queue_unknown = True
        return {"status": "publishing"}

    def query_run(self, run_id):
        self.query_calls += 1
        return self._run()

    def recover_run(self, run_id):
        return {
            "busy": self.recover_busy,
            "recovered": self.recover_status in {"stopped", "needs_review"},
            "run": self._run(status=self.recover_status),
        }


class XAutoPostPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = MutableClock()
        self.store = XAutoPostStore(
            Path(self.temp.name).resolve() / "x-auto.sqlite3",
            now_fn=self.clock,
        )
        self.actor = AuditActor("803", "operator")
        template = self.store.create_template(
            name="English auto",
            config={
                "account_ids": ["640"],
                "language": "en",
                "body_template": "{{drama_name}}\n{{desc}}\n{{url}}",
                "platform": 0,
                "metric_window_days": 7,
            },
            actor=self.actor,
            confirmation={"accepted": True},
        )
        self.template = self.store.set_template_enabled(
            template.id,
            True,
            expected_version=template.version,
            actor=self.actor,
        )
        self.run = self.store.create_run(
            run_key="manual:test-publisher",
            template_id=self.template.id,
            template_version=self.template.version,
            trigger_type="manual",
            scheduled_at_utc=self.clock().isoformat(),
            shanghai_date="2026-08-11",
            publish_time="19:00",
            blacklist_snapshot={"sha256": "a" * 64},
            actor=self.actor,
        )
        self.task = self.store.create_task(
            run_id=self.run.id,
            account_id="640",
            language="en",
            account_snapshot_version=1,
            account_snapshot={"username": "example"},
        )

    def _executor(self, bridge, *, gates=None):
        return AutoPostExecutor(
            self.store,
            DummySelector(),
            bridge,
            lambda task, run: {},
            lambda task, account, candidate: {"preflight_duration": 120},
            gates=gates or AutoLiveGates(True, True, True),
            now_fn=self.clock,
            lease_seconds=120,
        )

    def _reserve(self):
        claim = self.store.claim_next_executable_task(
            worker_id="publisher-test",
            lease_seconds=120,
            now=self.clock(),
        )
        self.assertIsNotNone(claim)
        token = claim.reveal_claim_token()
        self.store.reserve_material(
            task_id=self.task.id,
            material_id="123456",
            content_id="DramaOne",
            selection={
                "material": {
                    "material_id": "123456",
                    "video_duration": 120.0,
                }
            },
            claim_token=token,
        )
        return token, self.store.get_task(self.task.id)

    def _preparing(self):
        token, task = self._reserve()
        task = self.store.transition_task(
            task.id,
            "preparing",
            expected_statuses={"reserved"},
            claim_token=token,
            updates={"execution_run_id": 9001},
        )
        return token, task

    def _ready(self):
        token, task = self._reserve()
        self.store.confirm_material_reservation(
            task.id,
            9002,
            claim_token=token,
        )
        return self.store.transition_task(
            task.id,
            "ready",
            expected_statuses={"reserved"},
            claim_token=token,
            updates={
                "execution_run_id": 9001,
                "execution_queue_id": 9002,
                "selected_duration_sec": 120.0,
                "claim_phase": "",
            },
        )

    def test_recorded_preflight_failure_releases_account_and_provisional_material(self):
        token, task = self._preparing()
        bridge = FakeBridge(task)
        result = self._executor(bridge)._retry_or_fail(
            task,
            "prepare",
            AutoPostExecutionError(
                "x_auto_media_invalid", "media preflight rejected", 409
            ),
            token,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.execution_run_id, 9001)
        self.assertFalse(self.store.material_is_reserved("123456"))
        self.assertEqual(bridge.record_calls, 1)
        self.assertIsNone(
            self.store.claim_next_executable_task(
                worker_id="nothing-left", lease_seconds=120, now=self.clock()
            )
        )

    def test_failure_record_outage_keeps_task_and_material_retryable(self):
        token, task = self._preparing()
        bridge = FakeBridge(task)
        bridge.record_error = XPostBridgeError(
            "x_auto_x_bridge_unavailable", "bridge unavailable", 503
        )
        result = self._executor(bridge)._retry_or_fail(
            task,
            "prepare",
            AutoPostExecutionError(
                "x_auto_media_invalid", "media preflight rejected", 409
            ),
            token,
        )
        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.execution_run_id, 9001)
        self.assertTrue(self.store.material_is_reserved("123456"))

    def test_unknown_publish_is_reconciled_without_second_publish(self):
        task = self._ready()
        bridge = FakeBridge(task)
        first = self._executor(bridge).execute_next("publisher-1")
        self.assertTrue(first["claimed"])
        self.assertEqual(first["task"]["status"], "retry_wait")
        self.assertTrue(first["task"]["unknown_outcome"])
        self.assertEqual(bridge.publish_calls, 1)

        bridge.queue_status = "published"
        bridge.queue_unknown = False
        self.clock.value += timedelta(minutes=6)
        second = self._executor(bridge).execute_next("reconciler-1")
        self.assertTrue(second["claimed"])
        self.assertEqual(second["task"]["status"], "published")
        self.assertEqual(bridge.publish_calls, 1)
        self.assertEqual(second["task"]["execution_log_id"], 9003)
        self.assertEqual(second["task"]["publish_id"], "1900000000000000000")

    def test_transport_unknown_is_persisted_then_canonical_failure_terminates(self):
        task = self._ready()
        bridge = FakeBridge(task)
        bridge.publish_error = XPostBridgeError(
            "x_auto_x_bridge_unavailable",
            "publish response lost",
            503,
            unknown_outcome=True,
        )
        first = self._executor(bridge).execute_next("publisher-unknown")
        self.assertEqual(first["task"]["status"], "retry_wait")
        self.assertTrue(first["task"]["unknown_outcome"])
        self.assertEqual(
            first["task"]["error_code"], "x_auto_publish_outcome_unknown"
        )
        self.assertEqual(bridge.publish_calls, 1)

        bridge.queue_status = "failed"
        bridge.publish_error = None
        self.clock.value += timedelta(minutes=6)
        second = self._executor(bridge).execute_next("reconciler-failed")
        self.assertEqual(second["task"]["status"], "failed")
        self.assertTrue(second["task"]["unknown_outcome"])
        self.assertEqual(bridge.publish_calls, 1)

    def test_closed_live_gates_do_not_claim_or_publish(self):
        bridge = FakeBridge(self.task)
        result = self._executor(
            bridge, gates=AutoLiveGates(False, False, False)
        ).execute_next("held-worker")
        self.assertFalse(result["claimed"])
        self.assertEqual(result["held"], "live_gates_closed")
        self.assertEqual(self.store.get_task(self.task.id).status, "pending")
        self.assertEqual(bridge.publish_calls, 0)

    def test_closed_live_gates_reconcile_ready_queue_without_publishing(self):
        task = self._ready()
        bridge = FakeBridge(task)
        bridge.queue_status = "queued"
        bridge.recover_status = "stopped"
        result = self._executor(
            bridge, gates=AutoLiveGates(False, False, False)
        ).execute_next("closed-gate-reconciler")
        self.assertTrue(result["claimed"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(bridge.publish_calls, 0)

    def test_closed_live_gates_terminalize_unqueued_canonical_run(self):
        _token, task = self._preparing()
        self.clock.value += timedelta(minutes=3)
        bridge = FakeBridge(task)
        bridge.has_queue = False
        result = self._executor(
            bridge, gates=AutoLiveGates(False, False, False)
        ).execute_next("closed-gate-unqueued")
        self.assertTrue(result["claimed"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(result["task"]["execution_run_id"], 9001)
        self.assertFalse(self.store.material_is_reserved("123456"))
        self.assertEqual(bridge.record_calls, 1)
        self.assertEqual(bridge.publish_calls, 0)


if __name__ == "__main__":
    unittest.main()

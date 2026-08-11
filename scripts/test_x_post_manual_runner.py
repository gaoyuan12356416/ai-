#!/usr/bin/env python3
"""Offline orchestration tests for durable manual X material runs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_x_post_schedule_runner import make_config  # noqa: E402
from scripts.x_post_daily_runner import SidecarError  # noqa: E402
from scripts.x_post_manual_runner import (  # noqa: E402
    ManualRunError,
    _manual_identity,
    execute_manual_tick,
)


def queue(queue_id, run_id, account_id, rank, status="queued", unknown=False):
    return {
        "id": queue_id,
        "manual_run_id": run_id,
        "account_id": account_id,
        "candidate_rank": rank,
        "status": status,
        "unknown_outcome": unknown,
    }


def manual_run(queues=None):
    return {
        "id": 71,
        "trigger_source": "manual",
        "run_date": "2026-08-11",
        "source_date": "2026-08-10",
        "account_ids": [11, 12],
        "material_ids": ["501", "502"],
        "body_template": "{{drama_name}}\n{{desc}}\n{{url}}",
        "status": "running",
        "expected_count": 2,
        "queues": list(queues or []),
    }


class FakeManualSidecar:
    def __init__(self, run=None):
        self.run = run
        self.calls = []
        self.publish_errors = {}
        self.plan_error = None

    def claim(self):
        self.calls.append(("claim",))
        return self.run

    def preflight_storage(self, path):
        self.calls.append(("storage", path))
        return {"ready": True}

    def verify_account(self, account_id):
        self.calls.append(("verify", account_id))
        return {
            "id": account_id,
            "username": "account%s" % account_id,
            "x_user_id": "x%s" % account_id,
            "display_name": "Account %s" % account_id,
            "status": "active",
            "publish_eligible": True,
            "subscription_type": "premium",
            "subscription_verified": True,
            "long_video_publish_eligible": True,
            "long_video_eligible": True,
        }

    def create_plan(self, run_id, candidates):
        self.calls.append(
            ("plan", run_id, [item["material_id"] for item in candidates])
        )
        if self.plan_error is not None:
            raise self.plan_error
        planned = manual_run(
            [queue(801, run_id, 11, 1), queue(802, run_id, 12, 2)]
        )
        return planned

    def record_failure(self, run_id, code, message):
        self.calls.append(("failure", run_id, code, message))
        failed = manual_run()
        failed["status"] = "failed_preflight"
        failed["error_code"] = code
        failed["error_message"] = message
        return failed

    def publish_queue(self, path_template, queue_id):
        self.calls.append(("publish", queue_id))
        if queue_id in self.publish_errors:
            raise self.publish_errors[queue_id]
        return {
            "status": "published",
            "log_id": 9000 + queue_id,
            "preview_url": "https://x.com/example/status/%s" % queue_id,
        }


def planned_candidates():
    return [
        {"account_id": 11, "material_id": "501"},
        {"account_id": 12, "material_id": "502"},
    ]


class XPostManualRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = make_config(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, sidecar):
        # ScheduleConfig's fixed Linux data-disk path is covered by the
        # schedule-runner configuration tests.  These orchestration tests use
        # an isolated temporary work directory on every OS, so mock only that
        # outer validation boundary instead of touching production paths.
        with mock.patch(
            "scripts.x_post_manual_runner.ScheduleConfig.validate"
        ):
            return execute_manual_tick(self.config, sidecar=sidecar)

    def test_no_pending_run_performs_no_preflight_or_publish(self):
        sidecar = FakeManualSidecar(None)
        result = self.execute(sidecar)
        self.assertEqual(result, {"status": "no_pending"})
        self.assertEqual(sidecar.calls, [("claim",)])

    def test_whole_batch_preflights_before_atomic_plan_then_publishes_in_order(self):
        sidecar = FakeManualSidecar(manual_run())
        with mock.patch(
            "scripts.x_post_manual_runner._manual_candidates",
            return_value=planned_candidates(),
        ) as preflight:
            result = self.execute(sidecar)

        self.assertEqual(result["manual_run_id"], 71)
        self.assertEqual(result["attempted_count"], 2)
        self.assertFalse(result["resumed_existing_plan"])
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["claim", "storage", "verify", "verify", "storage", "plan", "publish", "publish"],
        )
        preflight.assert_called_once()
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [801, 802],
        )

    def test_existing_frozen_queues_resume_without_source_or_media_preflight(self):
        sidecar = FakeManualSidecar(
            manual_run([queue(801, 71, 11, 1), queue(802, 71, 12, 2)])
        )
        with mock.patch(
            "scripts.x_post_manual_runner._manual_candidates"
        ) as preflight:
            result = self.execute(sidecar)
        preflight.assert_not_called()
        self.assertTrue(result["resumed_existing_plan"])
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["claim", "publish", "publish"],
        )

    def test_resumed_inflight_queue_stops_without_retrying_x_write(self):
        sidecar = FakeManualSidecar(
            manual_run(
                [
                    queue(801, 71, 11, 1, status="publishing", unknown=True),
                    queue(802, 71, 12, 2),
                ]
            )
        )
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["results"][0]["unknown_outcome"])
        self.assertEqual(sidecar.calls, [("claim",)])

    def test_known_failure_continues_but_rate_limit_stops_remaining_accounts(self):
        queues = [queue(801, 71, 11, 1), queue(802, 71, 12, 2)]
        sidecar = FakeManualSidecar(manual_run(queues))
        sidecar.publish_errors[801] = SidecarError(
            "invalid_media_type", "known rejection", 409
        )
        result = self.execute(sidecar)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(
            [call[1] for call in sidecar.calls if call[0] == "publish"],
            [801, 802],
        )

        rate_limited = FakeManualSidecar(manual_run(queues))
        rate_limited.publish_errors[801] = SidecarError(
            "x_post_rate_limited", "rate limited", 429
        )
        result = self.execute(rate_limited)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            [call[1] for call in rate_limited.calls if call[0] == "publish"],
            [801],
        )

    def test_preflight_failure_is_recorded_before_any_queue_or_x_write(self):
        sidecar = FakeManualSidecar(manual_run())
        with mock.patch(
            "scripts.x_post_manual_runner._manual_candidates",
            side_effect=ManualRunError(
                "one item failed compliance",
                "x_post_manual_source_preflight_failed",
            ),
        ):
            result = self.execute(sidecar)
        self.assertEqual(result["status"], "failed_preflight")
        self.assertTrue(result["failure_recorded"])
        self.assertNotIn("plan", [call[0] for call in sidecar.calls])
        self.assertNotIn("publish", [call[0] for call in sidecar.calls])
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["claim", "storage", "verify", "verify", "failure"],
        )

    def test_unknown_atomic_plan_result_is_not_reclassified_as_preflight_failure(self):
        sidecar = FakeManualSidecar(manual_run())
        sidecar.plan_error = SidecarError(
            "x_publish_unknown",
            "plan response lost",
            503,
            unknown_outcome=True,
        )
        with mock.patch(
            "scripts.x_post_manual_runner._manual_candidates",
            return_value=planned_candidates(),
        ):
            with self.assertRaises(SidecarError):
                self.execute(sidecar)
        self.assertNotIn("failure", [call[0] for call in sidecar.calls])
        self.assertNotIn("publish", [call[0] for call in sidecar.calls])

    def test_manual_response_parser_rejects_bool_counts_and_mismatched_parent(self):
        invalid_count = manual_run()
        invalid_count["expected_count"] = True
        with self.assertRaises(SidecarError):
            _manual_identity(invalid_count)

        invalid_parent = manual_run([queue(801, 99, 11, 1)])
        with self.assertRaises(SidecarError):
            _manual_identity(invalid_parent)


if __name__ == "__main__":
    unittest.main()

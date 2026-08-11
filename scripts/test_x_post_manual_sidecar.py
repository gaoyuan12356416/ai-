#!/usr/bin/env python3
"""Focused fail-closed tests for the X manual-publish sidecar boundary."""

from __future__ import annotations

import unittest
from unittest import mock

from features.x_accounts import oauth_service as service
from features.x_posts import XPostError


class FakeStore:
    def __init__(self):
        self.candidates = None
        self.queue = {
            "id": 81,
            "run_id": None,
            "catchup_run_id": None,
            "schedule_run_id": None,
            "manual_run_id": 17,
            "account_id": 202,
        }

    def get_manual_run(self, run_id):
        return {
            "id": int(run_id),
            "account_ids": [202],
            "material_ids": ["501"],
            "body_template": "{{drama_name}}\n{{desc}}\n{{url}}",
            "status": "running",
            "expected_count": 1,
            "queues": [],
        }

    def create_manual_plan(self, run_id, candidates):
        self.candidates = list(candidates)
        return {"id": int(run_id), "queues": []}

    def get_queue(self, queue_id):
        return dict(self.queue, id=int(queue_id))

    def reserve_log(self, queue_id):
        return {
            "id": 91,
            "status": "published",
            "short_url": "https://ai.yingliangads.com/s2l/91.html",
            "x_post_id": "12345",
            "x_post_url": "https://x.com/account202/status/12345",
        }


class XPostManualSidecarTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.api_patch = mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, lambda _path: self.store, None),
        )
        self.api_patch.start()

    def tearDown(self):
        self.api_patch.stop()

    @staticmethod
    def account(long_video=True):
        return {
            "id": 202,
            "username": "current_account",
            "x_user_id": "x-202",
            "display_name": "Current Account",
            "status": "active",
            "publish_eligible": True,
            "long_video_publish_eligible": bool(long_video),
        }

    def test_public_manual_run_is_an_explicit_allowlist(self):
        result = service._public_manual_run(
            {
                "id": 17,
                "account_ids": [202],
                "material_ids": ["501"],
                "status": "queued",
                "idempotency_key": "secret-idempotency",
                "body_template": "secret-template",
                "actor_user_id": "secret-actor",
                "future_secret": "must-not-leak",
                "queues": [
                    {
                        "id": 81,
                        "manual_run_id": 17,
                        "account_id": 202,
                        "material_id": "501",
                        "status": "queued",
                        "material_url": "https://secret.example/video.mp4",
                    }
                ],
            }
        )
        self.assertEqual(result["id"], 17)
        self.assertNotIn("idempotency_key", result)
        self.assertNotIn("body_template", result)
        self.assertNotIn("future_secret", result)
        self.assertNotIn("material_url", result["queues"][0])

    def test_plan_rechecks_account_and_overwrites_untrusted_identity(self):
        candidate = {
            "material_id": "501",
            "preflight_duration": 30.0,
            "account_id": 999,
            "account_username": "forged",
            "page_name": "Forged",
            "page_id": "forged-id",
        }
        with mock.patch.object(
            service,
            "_manual_publish_accounts",
            return_value=[self.account()],
        ), mock.patch.object(service, "preflight_post_storage_request") as storage:
            service.create_post_manual_plan_request(
                {"run_id": 17, "candidates": [candidate]}
            )
        trusted = self.store.candidates[0]
        self.assertEqual(trusted["account_id"], 202)
        self.assertEqual(trusted["account_username"], "current_account")
        self.assertEqual(trusted["page_name"], "Current Account")
        self.assertEqual(trusted["page_id"], "x-202")
        storage.assert_called_once_with(1)

    def test_plan_rejects_long_video_without_current_token_entitlement(self):
        with mock.patch.object(
            service,
            "_manual_publish_accounts",
            return_value=[self.account(long_video=False)],
        ):
            with self.assertRaises(service.ServiceError) as captured:
                service.create_post_manual_plan_request(
                    {
                        "run_id": 17,
                        "candidates": [
                            {"material_id": "501", "preflight_duration": 141.0}
                        ],
                    }
                )
        self.assertEqual(captured.exception.code, "x_long_video_requires_premium")
        self.assertIsNone(self.store.candidates)

    def test_daily_bearer_can_publish_only_frozen_manual_account(self):
        published = service.publish_queue_request(
            81,
            [999],
            allow_manual=True,
        )
        self.assertEqual(published["status"], "published")

        with self.assertRaises(service.ServiceError) as denied:
            service.publish_queue_request(81, [999], allow_manual=False)
        self.assertEqual(denied.exception.code, "x_daily_account_scope_denied")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Focused sidecar contract tests for same-day X account catch-up plans."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from features.x_accounts import oauth_service as service
from features.x_posts import XPostError


def parent_plan(account_ids=(101, 102, 103)):
    queues = [
        {
            "id": 20 + rank,
            "run_id": 4,
            "catchup_run_id": None,
            "batch_kind": "daily",
            "run_date": "2026-07-27",
            "source_date": "2026-07-26",
            "account_id": account_id,
            "candidate_rank": rank,
            "status": "published",
            "created_at": "2026-07-27T02:01:03Z",
            "updated_at": "2026-07-27T02:03:16Z",
        }
        for rank, account_id in enumerate(account_ids, 1)
    ]
    return {
        "found": True,
        "run": {
            "id": 4,
            "run_date": "2026-07-27",
            "source_date": "2026-07-26",
            "status": "completed",
            "expected_count": len(account_ids),
            "queued_count": len(account_ids),
            "published_count": len(account_ids),
            "failed_count": 0,
            "unknown_count": 0,
        },
        "queues": queues,
    }


def catchup_run(account_ids=(104, 105, 106), status="queued", with_queues=True):
    queues = [
        {
            "id": 40 + rank,
            "run_id": None,
            "catchup_run_id": 9,
            "run_date": "2026-07-27",
            "source_date": "2026-07-26",
            "account_id": account_id,
            "candidate_rank": rank,
            "status": "queued",
        }
        for rank, account_id in enumerate(account_ids, 1)
    ]
    return {
        "id": 9,
        "parent_run_id": 4,
        "batch_kind": "catchup",
        "reason": "scope_expansion_v1",
        "account_ids": list(account_ids),
        "run_date": "2026-07-27",
        "source_date": "2026-07-26",
        "status": status,
        "expected_count": len(account_ids),
        "queued_count": len(account_ids) if with_queues else 0,
        "published_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
        "queues": queues if with_queues else [],
    }


class FakeStore:
    def __init__(self):
        self.daily = parent_plan()
        self.catchup = {"found": False, "run": None, "queues": []}
        self.created_args = None
        self.failure_args = None
        self.queue = None
        self.reserve_calls = []

    def query_daily_plan(self, run_date):
        self.queried_run_date = run_date
        return self.daily

    def query_catchup_plan(self, run_date, parent_run_id, reason="scope_expansion_v1"):
        self.queried_catchup = (run_date, parent_run_id, reason)
        return self.catchup

    def create_catchup_plan(self, *args, **kwargs):
        self.created_args = (args, kwargs)
        return catchup_run()

    def record_catchup_failure(self, *args):
        self.failure_args = args
        result = catchup_run(status="failed_preflight", with_queues=False)
        result["recorded"] = True
        return result

    def get_queue(self, queue_id):
        return dict(self.queue, id=int(queue_id))

    def reserve_log(self, queue_id):
        self.reserve_calls.append(int(queue_id))
        return {
            "id": 77,
            "status": "published",
            "short_url": "https://gy.g2flow.com/s2l/77.html",
            "x_post_id": "2081561021546696798",
            "x_post_url": (
                "https://x.com/catchup_account/status/2081561021546696798"
            ),
        }


class CatchupSidecarTest(unittest.TestCase):
    def setUp(self):
        service.DAILY_ACCOUNT_IDS = (101, 102, 103, 104, 105, 106)
        service.INTERNAL_TOKEN = "backend-token"
        service.DAILY_INTERNAL_TOKEN = "daily-token"
        self.store = FakeStore()
        self.store_factory = mock.Mock(return_value=self.store)
        self.api_patch = mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, self.store_factory, None),
        )
        self.api_patch.start()

    def tearDown(self):
        self.api_patch.stop()

    @staticmethod
    def query_payload():
        return {
            "run_date": "2026-07-27",
            "parent_run_id": 4,
            "reason": "scope_expansion_v1",
        }

    @staticmethod
    def candidate(account_id):
        return {"account_id": account_id, "material_id": str(7000 + account_id)}

    def account(self, account_id):
        return {
            "id": account_id,
            "username": "catchup_%s" % account_id,
            "display_name": "Catchup %s" % account_id,
            "x_user_id": str(2000000000000000000 + account_id),
            "status": "active",
            "publish_eligible": True,
        }

    def test_query_calculates_missing_scope_only_from_configured_ids(self):
        result = service.query_catchup_plan_request(
            self.query_payload(),
            service.DAILY_ACCOUNT_IDS,
        )
        self.assertEqual(result["missing_account_ids"], [104, 105, 106])
        self.assertEqual(result["parent_run_id"], 4)
        self.assertEqual(result["reason"], "scope_expansion_v1")
        self.assertFalse(result["found"])
        self.assertEqual(
            self.store.queried_catchup,
            ("2026-07-27", 4, "scope_expansion_v1"),
        )

    def test_create_requires_exact_missing_order_and_active_accounts(self):
        payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "candidates": [
                self.candidate(account_id)
                for account_id in (104, 105, 106)
            ],
        }
        with mock.patch.object(
            service,
            "find_account",
            side_effect=lambda account_id: self.account(account_id),
        ) as account_mock, mock.patch.object(
            service,
            "preflight_post_storage_request",
            return_value={"ready": True},
        ) as storage_mock:
            result = service.create_catchup_plan_request(
                payload,
                service.DAILY_ACCOUNT_IDS,
                require_pool=True,
            )
        self.assertEqual(result["batch_kind"], "catchup")
        self.assertEqual(result["missing_account_ids"], [104, 105, 106])
        self.assertEqual(
            [item["catchup_run_id"] for item in result["queues"]],
            [9, 9, 9],
        )
        self.assertEqual(
            [item["batch_kind"] for item in result["queues"]],
            ["catchup", "catchup", "catchup"],
        )
        self.assertEqual(account_mock.call_count, 3)
        storage_mock.assert_called_once_with(3)
        args, kwargs = self.store.created_args
        self.assertEqual(args[:4], (
            "2026-07-27",
            "2026-07-26",
            4,
            "scope_expansion_v1",
        ))
        self.assertEqual(
            [item["account_id"] for item in args[4]],
            [104, 105, 106],
        )
        self.assertEqual(args[5], service.DAILY_ACCOUNT_IDS)
        self.assertEqual(kwargs, {"require_pool": True})
        self.assertNotIn("token", json.dumps(result).lower())

    def test_create_rejects_client_selected_subset_before_account_or_storage(self):
        payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "candidates": [self.candidate(104), self.candidate(105)],
        }
        with mock.patch.object(service, "find_account") as account_mock, mock.patch.object(
            service, "preflight_post_storage_request"
        ) as storage_mock:
            with self.assertRaises(service.ServiceError) as captured:
                service.create_catchup_plan_request(
                    payload,
                    service.DAILY_ACCOUNT_IDS,
                    require_pool=True,
                )
        self.assertEqual(captured.exception.code, "x_daily_account_scope_denied")
        account_mock.assert_not_called()
        storage_mock.assert_not_called()
        self.assertIsNone(self.store.created_args)

    def test_create_rejects_non_publishable_target_before_reservation(self):
        payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "candidates": [
                self.candidate(account_id)
                for account_id in (104, 105, 106)
            ],
        }

        def account(account_id):
            item = self.account(account_id)
            if account_id == 105:
                item.update({"status": "refresh_required", "publish_eligible": False})
            return item

        with mock.patch.object(
            service, "find_account", side_effect=account
        ), mock.patch.object(
            service, "preflight_post_storage_request"
        ) as storage_mock:
            with self.assertRaises(service.ServiceError) as captured:
                service.create_catchup_plan_request(
                    payload,
                    service.DAILY_ACCOUNT_IDS,
                    require_pool=True,
                )
        self.assertEqual(captured.exception.code, "x_account_not_publishable")
        storage_mock.assert_not_called()
        self.assertIsNone(self.store.created_args)

    def test_failure_record_uses_server_derived_missing_count_and_scope(self):
        payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "expected_missing_count": 3,
            "error_code": "x_post_catchup_candidate_shortage",
            "error_message": "not enough clean candidates",
        }
        result = service.record_catchup_failure_request(
            payload,
            service.DAILY_ACCOUNT_IDS,
        )
        self.assertEqual(result["status"], "failed_preflight")
        self.assertEqual(result["missing_account_ids"], [104, 105, 106])
        self.assertEqual(
            self.store.failure_args,
            (
                "2026-07-27",
                "2026-07-26",
                4,
                "scope_expansion_v1",
                3,
                service.DAILY_ACCOUNT_IDS,
                "x_post_catchup_candidate_shortage",
                "not enough clean candidates",
            ),
        )

    def test_failed_preflight_query_is_safe_and_retriable(self):
        failed = catchup_run(status="failed_preflight", with_queues=False)
        self.store.catchup = {"found": True, "run": failed, "queues": []}
        result = service.query_catchup_plan_request(
            self.query_payload(),
            service.DAILY_ACCOUNT_IDS,
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["run"]["status"], "failed_preflight")
        self.assertEqual(result["queues"], [])
        self.assertEqual(result["missing_account_ids"], [104, 105, 106])

    def test_daily_publish_accepts_exactly_one_daily_or_catchup_parent(self):
        for run_id, catchup_run_id, allowed in (
            (4, None, True),
            (None, 9, True),
            (None, None, False),
            (4, 9, False),
        ):
            with self.subTest(run_id=run_id, catchup_run_id=catchup_run_id):
                self.store.reserve_calls.clear()
                self.store.queue = {
                    "account_id": 104,
                    "run_id": run_id,
                    "catchup_run_id": catchup_run_id,
                }
                if allowed:
                    result = service.publish_queue_request(
                        88,
                        service.DAILY_ACCOUNT_IDS,
                    )
                    self.assertEqual(result["status"], "published")
                    self.assertEqual(self.store.reserve_calls, [88])
                else:
                    with self.assertRaises(service.ServiceError) as captured:
                        service.publish_queue_request(
                            88,
                            service.DAILY_ACCOUNT_IDS,
                        )
                    self.assertEqual(
                        captured.exception.code,
                        "x_daily_account_scope_denied",
                    )
                    self.assertEqual(self.store.reserve_calls, [])

    def test_catchup_http_routes_are_daily_bearer_only(self):
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % server.server_address[1]

        def request(path, token, body):
            return urllib.request.Request(
                base_url + path,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )

        expected = {
            "found": False,
            "parent_run_id": 4,
            "reason": "scope_expansion_v1",
            "missing_account_ids": [104, 105, 106],
            "run": None,
            "queues": [],
        }
        create_payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "candidates": [
                self.candidate(account_id)
                for account_id in (104, 105, 106)
            ],
        }
        failure_payload = {
            **self.query_payload(),
            "source_date": "2026-07-26",
            "expected_missing_count": 3,
            "error_code": "x_post_catchup_candidate_shortage",
            "error_message": "not enough clean candidates",
        }
        expected_create = catchup_run()
        expected_failure = catchup_run(
            status="failed_preflight",
            with_queues=False,
        )
        try:
            with mock.patch.object(
                service,
                "query_catchup_plan_request",
                return_value=expected,
            ) as query_mock, mock.patch.object(
                service,
                "create_catchup_plan_request",
                return_value=expected_create,
            ) as create_mock, mock.patch.object(
                service,
                "record_catchup_failure_request",
                return_value=expected_failure,
            ) as failure_mock:
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/catchup-plan/query",
                        service.DAILY_INTERNAL_TOKEN,
                        self.query_payload(),
                    )
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": expected},
                    )
                query_mock.assert_called_once_with(
                    self.query_payload(),
                    service.DAILY_ACCOUNT_IDS,
                )
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/catchup-plan",
                        service.DAILY_INTERNAL_TOKEN,
                        create_payload,
                    )
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": expected_create},
                    )
                create_mock.assert_called_once_with(
                    create_payload,
                    service.DAILY_ACCOUNT_IDS,
                    require_pool=True,
                )
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/catchup-runs/record-failure",
                        service.DAILY_INTERNAL_TOKEN,
                        failure_payload,
                    )
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": expected_failure},
                    )
                failure_mock.assert_called_once_with(
                    failure_payload,
                    service.DAILY_ACCOUNT_IDS,
                )
                for path, body in (
                    ("/internal/posts/catchup-plan/query", self.query_payload()),
                    ("/internal/posts/catchup-plan", create_payload),
                    (
                        "/internal/posts/catchup-runs/record-failure",
                        failure_payload,
                    ),
                ):
                    with self.subTest(path=path), self.assertRaises(
                        urllib.error.HTTPError
                    ) as denied:
                        urllib.request.urlopen(
                            request(path, service.INTERNAL_TOKEN, body)
                        )
                    self.assertEqual(denied.exception.code, 403)
                    denied_body = json.loads(
                        denied.exception.read().decode("utf-8")
                    )
                    denied.exception.close()
                    self.assertEqual(
                        denied_body["error"],
                        "x_daily_internal_required",
                    )
                self.assertEqual(query_mock.call_count, 1)
                self.assertEqual(create_mock.call_count, 1)
                self.assertEqual(failure_mock.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Offline admin and scheduling tests for the X auto-template service."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.core import XAutoPostStore  # noqa: E402
from features.x_auto_posts.publisher import AutoLiveGates  # noqa: E402
from features.x_auto_posts.service import (  # noqa: E402
    AutoPostServiceError,
    XAutoPostHTTPServer,
    XAutoPostService,
    build_service_from_env,
)
from scripts.test_x_auto_post_validation import valid_payload  # noqa: E402


UTC = timezone.utc


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.value


class FakeBridge:
    def __init__(self):
        self.accounts_by_id = {
            "101": {
                "id": 101,
                "x_user_id": "x-101",
                "username": "account101",
                "display_name": "Account 101",
                "status": "active",
                "publish_eligible": True,
                "subscription_type": "premium",
                "long_video_eligible": True,
            },
            "102": {
                "id": 102,
                "x_user_id": "x-102",
                "username": "account102",
                "display_name": "Account 102",
                "status": "active",
                "publish_eligible": True,
                "subscription_type": "unknown",
                "long_video_eligible": False,
            },
        }

    def accounts(self):
        return [dict(value) for value in self.accounts_by_id.values()]

    def verify_account(self, account_id):
        value = self.accounts_by_id.get(str(account_id))
        if value is None:
            raise AutoPostServiceError("x_auto_account_missing", "missing", 404)
        return dict(value)


class FakeBlacklist:
    loaded_at_utc = "2026-08-11T00:00:00+00:00"
    source_row_count = 0
    sha256 = "b" * 64


class FakeSource:
    def blacklist_snapshot(self):
        return FakeBlacklist()


class FakeSelector:
    source = FakeSource()
    metrics = object()
    history = object()
    material_validator = object()
    product = "Dramawave"
    app_id = 1479
    material_data_source = 6


class FakeExecutor:
    def __init__(self, gates):
        self.gates = gates

    def execute_next(self, _worker_id):
        raise AssertionError("admin tests must not publish")


class XAutoPostServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.store = XAutoPostStore(
            Path(self.temp.name) / "x-auto.sqlite3", now_fn=self.clock
        )
        self.bridge = FakeBridge()

    def service(self, gates=AutoLiveGates()):
        return XAutoPostService(
            self.store,
            self.bridge,
            FakeSelector(),
            FakeExecutor(gates),
            now_fn=self.clock,
            runner_kick_path=Path(self.temp.name) / "run" / "manual-kick",
        )

    @staticmethod
    def actor(payload):
        return {**payload, "_actor": {"user_id": "803", "name": "operator"}}

    def test_accounts_and_created_template_are_safe_and_disabled(self):
        service = self.service()
        accounts = service.accounts()
        self.assertEqual(accounts["total"], 2)
        created = service.create_template(self.actor(valid_payload()))["template"]
        self.assertFalse(created["enabled"])
        self.assertEqual(created["config"]["language"], "en")
        self.assertNotIn("access_token", repr(accounts).lower())

    def test_copy_starts_disabled_and_update_creates_immutable_version(self):
        service = self.service()
        created = service.create_template(self.actor(valid_payload()))["template"]
        copied = service.copy_template(
            created["id"],
            self.actor({"expected_version": 1, "name": "Copy"}),
        )["template"]
        self.assertFalse(copied["enabled"])
        updated = service.update_template(
            created["id"],
            self.actor(
                {
                    **valid_payload(name="Updated"),
                    "body_template": "{{drama_name}}\n{{desc}}\nWatch: {{url}}",
                    "expected_version": 1,
                }
            ),
        )["template"]
        self.assertEqual(updated["version"], 2)
        original = self.store.get_template(created["id"], version=1)
        self.assertEqual(original.config["body_template"], valid_payload()["body_template"])
        self.assertNotEqual(original.config_sha256, updated["config_sha256"])

    def test_template_list_exposes_real_next_and_last_run_facts(self):
        service = self.service(AutoLiveGates(True, True, True))
        template = service.create_template(self.actor(valid_payload()))["template"]
        service.set_enabled(
            template["id"],
            True,
            self.actor({"expected_version": template["version"]}),
        )
        run = self.store.create_run(
            run_key="ui-list-last-run",
            template_id=template["id"],
            template_version=template["version"],
            trigger_type="manual",
            scheduled_at_utc="2026-08-11T09:00:00+00:00",
            shanghai_date="2026-08-11",
            publish_time="17:00",
            blacklist_snapshot={},
        )
        item = service.templates({})["templates"][0]
        self.assertEqual(item["next_run_at"], "2026-08-11T12:35:00+00:00")
        self.assertEqual(item["last_run"]["id"], run.id)
        self.assertEqual(item["last_run_status"], "queued")
        self.assertEqual(item["last_run_at"], run.created_at)

    def test_disabled_template_does_not_advertise_next_run(self):
        service = self.service()
        created = service.create_template(self.actor(valid_payload()))["template"]
        item = service.template(created["id"])["template"]
        self.assertNotIn("next_run_at", item)

    def test_random_template_read_does_not_create_a_plan(self):
        service = self.service(AutoLiveGates(True, True, True))
        template = service.create_template(
            self.actor(valid_payload(schedule={"mode": "random", "daily_count": 2}))
        )["template"]
        service.set_enabled(
            template["id"],
            True,
            self.actor({"expected_version": template["version"]}),
        )
        item = service.template(template["id"])["template"]
        self.assertNotIn("next_run_at", item)
        self.assertIsNone(
            self.store.get_random_plan(template["id"], template["version"], "2026-08-11")
        )

    def test_run_now_is_blocked_before_creating_rows_when_gates_closed(self):
        service = self.service()
        template = service.create_template(self.actor(valid_payload()))["template"]
        with self.assertRaises(AutoPostServiceError) as caught:
            service.run_now(
                template["id"],
                self.actor(
                    {
                        "expected_version": 1,
                        "confirmed": True,
                        "idempotency_key": "request-0001",
                    }
                ),
            )
        self.assertEqual(caught.exception.code, "x_auto_live_gates_closed")
        self.assertEqual(self.store.list_runs(), [])

    def test_open_gate_run_now_is_idempotent_and_freezes_account_tasks(self):
        service = self.service(AutoLiveGates(True, True, True))
        template = service.create_template(self.actor(valid_payload()))["template"]
        payload = self.actor(
            {
                "expected_version": 1,
                "confirmed": True,
                "idempotency_key": "request-0002",
            }
        )
        first = service.run_now(template["id"], payload)
        second = service.run_now(template["id"], payload)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        tasks = self.store.list_tasks(run_id=first["run_id"])
        self.assertEqual([task.account_id for task in tasks], ["101", "102"])
        self.assertEqual({task.language for task in tasks}, {"en"})

    def test_closed_tick_creates_nothing(self):
        service = self.service()
        result = service.tick()
        self.assertEqual(result["held"], "live_gates_closed")
        self.assertEqual(result["created_runs"], [])

    def test_placeholder_token_fails_before_database_or_network_setup(self):
        with self.assertRaises(AutoPostServiceError) as caught:
            build_service_from_env(
                {
                    "X_AUTO_POST_INTERNAL_TOKEN": (
                        "replace-with-unique-random-token-at-least-32-characters"
                    ),
                    "X_POST_AUTO_INTERNAL_TOKEN": "z" * 48,
                }
            )
        self.assertEqual(caught.exception.code, "x_auto_internal_bearer_invalid")

    def test_http_server_waits_for_inflight_requests(self):
        self.assertFalse(XAutoPostHTTPServer.daemon_threads)
        self.assertTrue(XAutoPostHTTPServer.block_on_close)


if __name__ == "__main__":
    unittest.main()

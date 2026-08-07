#!/usr/bin/env python3
"""Offline integration tests for the TT auto-post admin service.

These tests use a temporary SQLite ledger and fakes only.  They never start
the HTTP server, call TikTok, or invoke the publishing executor.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.core import TTPostAutoStore  # noqa: E402
from features.tt_auto_posts.legacy_reader import LegacyAccountSetting  # noqa: E402
from features.tt_auto_posts.publisher import AutoLiveGates  # noqa: E402
from features.tt_auto_posts.service import (  # noqa: E402
    AutoPostServiceError,
    TTAutoPostHTTPServer,
    TTAutoPostService,
    build_service_from_env,
)
from features.tt_auto_posts.validation import (  # noqa: E402
    RESOURCE_TYPE_V2_LABELS,
    ValidationError,
    normalize_template_payload,
)


UTC = timezone.utc


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeAccountRepository:
    def __init__(self):
        self.accounts = {
            "640": {
                "source_account_id": "640",
                "account_id": "640",
                "username": "account640",
                "display_name": "Account 640",
                "publish_eligible": True,
            },
            "641": {
                "source_account_id": "641",
                "account_id": "641",
                "username": "account641",
                "display_name": "Account 641",
                "publish_eligible": True,
            },
        }

    def list_public_accounts(self):
        return [dict(value) for value in self.accounts.values()]

    def get_public_account(self, account_id):
        value = self.accounts.get(str(account_id))
        if value is None:
            raise LookupError("account unavailable")
        return dict(value)

    def as_account_source(self):
        return object()


class FakeLegacyReader:
    def get_account_setting(self, account_id):
        account_id = str(account_id)
        if account_id not in {"640", "641"}:
            raise LookupError("account setting unavailable")
        return LegacyAccountSetting(
            account_id=account_id,
            drama_language="en" if account_id == "640" else "es",
            privacy_level="PUBLIC_TO_EVERYONE",
            allow_comment=True,
            allow_duet=False,
            allow_stitch=False,
            brand_content_toggle=False,
            brand_organic_toggle=False,
            is_aigc=True,
            version=3,
            updated_at="2026-08-05T00:00:00+00:00",
        )


class FakeBlacklist:
    drama_series_codes = frozenset({"blocked-drama"})
    material_data_source_ids = frozenset({"blocked-material"})
    loaded_at_utc = "2026-08-05T00:00:00+00:00"
    source_row_count = 2
    sha256 = "b" * 64


class FakeSource:
    def __init__(self):
        self.blacklist_calls = 0

    def blacklist_snapshot(self):
        self.blacklist_calls += 1
        return FakeBlacklist()


class FakeSelector:
    def __init__(self):
        self.source = FakeSource()
        self.metrics = object()
        self.legacy_reader = object()
        self.material_validator = None
        self.product = "Dramawave"
        self.app_id = 1479
        self.material_data_source = 6


class FakeExecutor:
    def __init__(self, gates):
        self.gates = gates
        self.media_profile_version = "tt-post-source-direct-v1"
        self.source_trim_tail_seconds = 0.0
        self.execute_calls = []

    def execute_next(self, worker_id):
        self.execute_calls.append(worker_id)
        raise AssertionError("admin/scheduler tests must not execute a publish task")


def template_payload(*, name="Template A", mode="fixed", account_ids=None):
    schedule = (
        {"mode": "fixed", "times": ["18:00", "18:30"]}
        if mode == "fixed"
        else {"mode": "random", "daily_count": 2}
    )
    return {
        "name": name,
        "account_ids": list(account_ids or ["640", "641"]),
        "caption_template": "Drama {{content_id}}\n{desc}\n{url}",
        "metric_window_days": 7,
        "drama_launch_window_days": 0,
        "cooldown_days": 3,
        "platform": 0,
        "drama_rule": {
            "spend_min": "0",
            "spend_max": None,
            "roas_min": None,
            "roas_max": "200",
            "sort_by": "roas",
            "sort_direction": "desc",
            "resource_type_v2": ["1", "2"],
        },
        "material_rule": {
            "spend_min": None,
            "spend_max": "9999.50",
            "roas_min": "0",
            "roas_max": None,
            "sort_by": "spend",
            "sort_direction": "asc",
            "duration_min_seconds": 5,
            "duration_max_seconds": 120,
        },
        "schedule": schedule,
    }


class TTAutoPostServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = MutableClock(datetime(2026, 8, 5, 10, 0, tzinfo=UTC))
        self.store = TTPostAutoStore(
            Path(self.temp.name) / "tt-auto.sqlite3",
            now_fn=self.clock,
        )
        self.accounts = FakeAccountRepository()
        self.legacy = FakeLegacyReader()
        self.selector = FakeSelector()

    def test_http_server_waits_for_inflight_threads_on_shutdown(self):
        self.assertFalse(TTAutoPostHTTPServer.daemon_threads)
        self.assertTrue(TTAutoPostHTTPServer.block_on_close)

    def test_health_exposes_preparation_profile_and_trim(self):
        service, _ = self.service(gates=AutoLiveGates(True, True, True))
        result = service.health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "tt-post-source-direct-v1")
        self.assertEqual(result["source_trim_tail_seconds"], 0.0)
        self.assertTrue(result["gates"]["is_open"])

    def test_startup_rejects_documented_placeholder_bearer_before_storage(self):
        with self.assertRaises(AutoPostServiceError) as caught:
            build_service_from_env(
                {
                    "TT_AUTO_POST_INTERNAL_TOKEN": (
                        "replace-with-unique-random-token-at-least-32-characters"
                    )
                }
            )
        self.assertEqual(caught.exception.code, "tt_auto_internal_bearer_invalid")

    def service(self, *, gates=None):
        executor = FakeExecutor(gates or AutoLiveGates())
        service = TTAutoPostService(
            self.store,
            self.legacy,
            self.accounts,
            self.selector,
            executor,
            now_fn=self.clock,
            runner_kick_path=Path(self.temp.name) / "run" / "manual-kick",
            schedule_grace_seconds=600,
        )
        return service, executor

    @staticmethod
    def actor(payload):
        return {
            **payload,
            "_actor": {"user_id": "803", "name": "operator"},
        }

    def create(self, service, **kwargs):
        return service.create_template(
            self.actor(template_payload(**kwargs))
        )["template"]

    def test_validation_is_strict_and_platform_is_exactly_zero(self):
        normalized = normalize_template_payload(template_payload())
        self.assertEqual(normalized["metric_window_days"], 7)
        self.assertEqual(normalized["platform"], 0)
        self.assertEqual(normalized["drama_rule"]["sort_by"], "roas")
        self.assertEqual(normalized["material_rule"]["sort_direction"], "asc")

        invalid = template_payload()
        invalid["platform"] = 9
        with self.assertRaises(ValidationError):
            normalize_template_payload(invalid)
        code_template = template_payload()
        code_template["caption_template"] = "Find the ending with {code}"
        normalized_code = normalize_template_payload(code_template)
        self.assertEqual(
            normalized_code["caption_template"],
            "Find the ending with {code}",
        )
        invalid = template_payload()
        invalid["unexpected"] = True
        with self.assertRaises(ValidationError):
            normalize_template_payload(invalid)

    def test_caption_template_allows_omitting_drama_id_macro(self):
        payload = template_payload()
        payload["caption_template"] = "Watch the full story\n{desc}\n{url}"

        normalized = normalize_template_payload(payload)

        self.assertEqual(
            normalized["caption_template"],
            "Watch the full story\n{desc}\n{url}",
        )

        service, _executor = self.service()
        created = service.create_template(self.actor(payload))["template"]
        self.assertEqual(
            created["config"]["caption_template"],
            "Watch the full story\n{desc}\n{url}",
        )

    def test_resource_type_v2_is_optional_and_enum_limited(self):
        empty = template_payload()
        empty["drama_rule"]["resource_type_v2"] = []
        normalized = normalize_template_payload(empty)
        self.assertEqual(normalized["drama_rule"]["resource_type_v2"], [])

        omitted = template_payload()
        omitted["drama_rule"].pop("resource_type_v2")
        normalized = normalize_template_payload(omitted)
        self.assertEqual(normalized["drama_rule"]["resource_type_v2"], [])

        self.assertEqual(
            set(RESOURCE_TYPE_V2_LABELS),
            {"0", *(str(value) for value in range(1, 23)), "100"},
        )
        self.assertEqual(RESOURCE_TYPE_V2_LABELS["21"], "AI翻译解说剧首发")
        self.assertEqual(RESOURCE_TYPE_V2_LABELS["22"], "AI翻译解说剧首发")

        for invalid_value in (-1, 23, "01", True, None):
            with self.subTest(invalid_value=invalid_value):
                invalid = template_payload()
                invalid["drama_rule"]["resource_type_v2"] = [invalid_value]
                with self.assertRaises(ValidationError) as caught:
                    normalize_template_payload(invalid)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_crud_copy_enable_disable_and_version_checks(self):
        service, executor = self.service()
        created = self.create(service)
        template_id = created["id"]
        self.assertEqual(created["version"], 1)
        self.assertFalse(created["enabled"])
        self.assertTrue(created["confirmed"])

        listing = service.templates({})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(service.template(template_id)["template"]["name"], "Template A")

        updated_payload = template_payload(name="Template B", account_ids=["640"])
        updated_payload["expected_version"] = 1
        updated = service.update_template(
            template_id, self.actor(updated_payload)
        )["template"]
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["name"], "Template B")
        self.assertFalse(updated["enabled"])

        copied = service.copy_template(
            template_id,
            self.actor({"expected_version": 2, "name": "Template B copy"}),
        )["template"]
        self.assertNotEqual(copied["id"], template_id)
        self.assertFalse(copied["enabled"])
        self.assertTrue(copied["confirmed"])

        enabled = service.set_enabled(
            template_id,
            True,
            self.actor({"expected_version": 2}),
        )["template"]
        self.assertTrue(enabled["enabled"])
        enabled_at = enabled["enabled_at_utc"]
        self.clock.value = datetime(2026, 8, 5, 10, 1, tzinfo=UTC)
        replayed_enable = service.set_enabled(
            template_id,
            True,
            self.actor({"expected_version": 2}),
        )["template"]
        self.assertEqual(replayed_enable["enabled_at_utc"], enabled_at)
        disabled = service.set_enabled(
            template_id,
            False,
            self.actor({"expected_version": 2}),
        )["template"]
        self.assertFalse(disabled["enabled"])
        with self.assertRaises(Exception) as caught:
            service.set_enabled(
                template_id,
                True,
                self.actor({"expected_version": 1}),
            )
        self.assertEqual(
            getattr(caught.exception, "code", ""),
            "tt_auto_template_version_conflict",
        )
        self.assertEqual(executor.execute_calls, [])

    def test_manual_run_requires_explicit_confirmation_and_only_queues(self):
        service, executor = self.service()
        template = self.create(service, account_ids=["640"])
        with self.assertRaises(AutoPostServiceError) as caught:
            service.run_now(
                template["id"],
                self.actor(
                    {
                        "expected_version": 1,
                        "confirmed": False,
                        "idempotency_key": "manual-test-0001",
                    }
                ),
            )
        self.assertEqual(
            caught.exception.code, "tt_auto_run_confirmation_required"
        )
        self.assertEqual(self.store.list_runs(), [])
        with self.assertRaises(AutoPostServiceError) as missing_key:
            service.run_now(
                template["id"],
                self.actor({"expected_version": 1, "confirmed": True}),
            )
        self.assertEqual(missing_key.exception.code, "invalid_request")
        self.assertEqual(self.store.list_runs(), [])

        result = service.run_now(
            template["id"],
            self.actor(
                {
                    "expected_version": 1,
                    "confirmed": True,
                    "idempotency_key": "manual-test-0001",
                }
            ),
        )
        self.assertEqual(result["run"]["trigger_type"], "manual")
        tasks = self.store.list_tasks(run_id=result["run_id"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "pending")
        self.assertEqual(tasks[0].drama_language, "en")
        self.assertEqual(tasks[0].account_username, "account640")
        self.assertEqual(tasks[0].account_display_name, "Account 640")
        self.assertEqual(executor.execute_calls, [])
        self.assertEqual(self.selector.source.blacklist_calls, 1)

        replay = service.run_now(
            template["id"],
            self.actor(
                {
                    "expected_version": 1,
                    "confirmed": True,
                    "idempotency_key": "manual-test-0001",
                }
            ),
        )
        self.assertEqual(replay["run_id"], result["run_id"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(len(self.store.list_tasks(run_id=result["run_id"])), 1)
        self.assertEqual(self.selector.source.blacklist_calls, 1)

        other = self.create(service, account_ids=["641"])
        with self.assertRaises(AutoPostServiceError) as reused_for_other_facts:
            service.run_now(
                other["id"],
                self.actor(
                    {
                        "expected_version": 1,
                        "confirmed": True,
                        "idempotency_key": "manual-test-0001",
                    }
                ),
            )
        self.assertEqual(
            reused_for_other_facts.exception.code,
            "tt_auto_run_idempotency_conflict",
        )
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_fixed_schedule_creates_idempotent_run_without_execution(self):
        gates = AutoLiveGates(True, True, True)
        service, executor = self.service(gates=gates)
        payload = template_payload(account_ids=["640"])
        payload["schedule"] = {"mode": "fixed", "times": ["18:00"]}
        template = service.create_template(self.actor(payload))["template"]
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": 1})
        )

        first = service.tick()
        second = service.tick()
        self.assertEqual(len(first["created_runs"]), 1)
        self.assertEqual(second["created_runs"], [])
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(executor.execute_calls, [])

    def test_random_schedule_is_stable_and_does_not_execute(self):
        gates = AutoLiveGates(True, True, True)
        service, executor = self.service(gates=gates)
        template = self.create(
            service,
            mode="random",
            account_ids=["640"],
        )
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": 1})
        )
        snapshot = self.store.get_template(template["id"])
        first = service._schedule_times(snapshot, "2026-08-05")
        second = service._schedule_times(snapshot, "2026-08-05")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)

        hour, minute = (int(value) for value in first[0].split(":"))
        # Convert the chosen Beijing time to UTC without relying on local TZ.
        if hour >= 8:
            self.clock.value = datetime(2026, 8, 5, hour - 8, minute, tzinfo=UTC)
        else:
            self.clock.value = datetime(2026, 8, 4, 16 + hour, minute, tzinfo=UTC)
        result = service.tick()
        self.assertEqual(len(result["created_runs"]), 1)
        self.assertEqual(executor.execute_calls, [])

    def test_closed_gates_hold_schedule_and_create_nothing(self):
        service, executor = self.service(gates=AutoLiveGates())
        template = self.create(service, account_ids=["640"])
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": 1})
        )
        result = service.tick()
        self.assertEqual(result["held"], "live_gates_closed")
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(executor.execute_calls, [])

    def test_edit_and_reenable_does_not_duplicate_an_existing_schedule_slot(self):
        service, _ = self.service(gates=AutoLiveGates(True, True, True))
        payload = template_payload(account_ids=["640"])
        payload["schedule"] = {"mode": "fixed", "times": ["18:00"]}
        template = service.create_template(self.actor(payload))["template"]
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": 1})
        )
        self.assertEqual(len(service.tick()["created_runs"]), 1)

        self.clock.value = datetime(2026, 8, 5, 10, 3, tzinfo=UTC)
        edited = template_payload(name="Template v2", account_ids=["640"])
        edited["schedule"] = {"mode": "fixed", "times": ["18:00"]}
        edited["expected_version"] = 1
        updated = service.update_template(
            template["id"], self.actor(edited)
        )["template"]
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": updated["version"]})
        )
        self.assertEqual(service.tick()["created_runs"], [])
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].template_version, 1)
        self.assertEqual(
            runs[0].run_key,
            f"tt-auto:auto:v1:{template['id']}:2026-08-05:1800",
        )

    def test_enable_after_slot_does_not_backfill_within_grace(self):
        service, _ = self.service(gates=AutoLiveGates(True, True, True))
        payload = template_payload(account_ids=["640"])
        payload["schedule"] = {"mode": "fixed", "times": ["18:00"]}
        template = service.create_template(self.actor(payload))["template"]
        self.clock.value = datetime(2026, 8, 5, 10, 5, tzinfo=UTC)
        service.set_enabled(
            template["id"], True, self.actor({"expected_version": 1})
        )
        self.assertEqual(service.tick()["created_runs"], [])
        self.assertEqual(self.store.list_runs(), [])

    def test_run_date_filter_is_inclusive_by_beijing_calendar_day(self):
        service, _ = self.service()
        template = self.create(service, account_ids=["640"])
        self.clock.value = datetime(2026, 8, 5, 15, 59, tzinfo=UTC)
        first = service.run_now(
            template["id"],
            self.actor(
                {
                    "expected_version": 1,
                    "confirmed": True,
                    "idempotency_key": "manual-beijing-day-1",
                }
            ),
        )
        self.clock.value = datetime(2026, 8, 5, 16, 1, tzinfo=UTC)
        second = service.run_now(
            template["id"],
            self.actor(
                {
                    "expected_version": 1,
                    "confirmed": True,
                    "idempotency_key": "manual-beijing-day-2",
                }
            ),
        )
        result = service.runs({"from": ["2026-08-05"], "to": ["2026-08-05"]})
        self.assertEqual([item["id"] for item in result["runs"]], [first["run_id"]])
        self.assertNotEqual(first["run_id"], second["run_id"])
        with self.assertRaises(AutoPostServiceError):
            service.runs({"to": ["2026-02-30"]})

    def test_public_run_detail_omits_source_and_prepared_media_urls(self):
        service, _ = self.service()
        template = self.create(service, account_ids=["640"])
        result = service.run_now(
            template["id"],
            self.actor(
                {
                    "expected_version": 1,
                    "confirmed": True,
                    "idempotency_key": "manual-public-dto-1",
                }
            ),
        )
        task = self.store.list_tasks(run_id=result["run_id"])[0]
        self.store.reserve_material(
            task_id=task.id,
            material_id="901",
            content_id="D1",
            selection_snapshot={
                "drama": {"content_id": "D1"},
                "material": {
                    "material_id": "901",
                    "media_url": "https://signed.example/source.mp4?token=secret",
                    "source_media_url": "https://signed.example/source.mp4?token=secret",
                },
            },
        )
        self.store.transition_task(
            task.id,
            "preparing",
            expected_statuses={"reserved"},
            updates={"source_media_url": "https://signed.example/source.mp4?token=secret"},
        )
        self.store.transition_task(
            task.id,
            "ready",
            expected_statuses={"preparing"},
            updates={"prepared_media_url": "https://signed.example/final.mp4?token=secret"},
        )
        public_task = service.run(result["run_id"])["tasks"][0]
        serialized = str(public_task)
        self.assertTrue(public_task["prepared"])
        self.assertNotIn("media_url", serialized)
        self.assertNotIn("token=secret", serialized)

    def test_startup_rejects_auto_database_aliasing_legacy_database(self):
        shared_root = Path(self.temp.name) / "shared-state"
        shared_root.mkdir()
        legacy_db = shared_root / "tt-post.sqlite3"

        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute("CREATE TABLE legacy_sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO legacy_sentinel(value) VALUES('untouched')")
            conn.commit()

        with self.assertRaises(AutoPostServiceError) as caught:
            build_service_from_env(
                {
                    "TT_AUTO_POST_INTERNAL_TOKEN": "i" * 48,
                    "TT_AUTO_POST_STATE_ROOT": str(shared_root),
                    "TT_AUTO_POST_LEGACY_STATE_ROOT": str(shared_root),
                    "TT_AUTO_POST_DB_PATH": str(legacy_db),
                    "TT_AUTO_POST_LEGACY_DB_PATH": str(legacy_db),
                }
            )
        self.assertEqual(caught.exception.code, "tt_auto_db_path_collision")
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            value = conn.execute("SELECT value FROM legacy_sentinel").fetchone()[0]
        self.assertEqual(tables, {"legacy_sentinel"})
        self.assertEqual(value, "untouched")


if __name__ == "__main__":
    unittest.main()

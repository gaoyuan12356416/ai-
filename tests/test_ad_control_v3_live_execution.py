import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path

from features.ad_control_v3.errors import AdControlV3Error
from features.ad_control_v3.live_execution import FacebookLiveExecutor
from features.ad_control_v3.scheduler import candidate_schedule_due, runner_event_key
from features.ad_control_v3.schemas import behavior_hash
from tests.test_ad_control_v3_core import NORMAL, base_payload, make_service


class FakeLiveExecutor:
    def __init__(self, status="succeeded", writes=1):
        self.calls = []
        self.status = status
        self.writes = writes

    def execute(self, group, target):
        self.calls.append((copy.deepcopy(group), copy.deepcopy(target)))
        return dict(target, status=self.status, reason="", meta_write_count=self.writes)


class ServiceLiveExecutionTests(unittest.TestCase):
    @staticmethod
    def live_group(service, repository):
        group = service.create_rule_group(NORMAL, base_payload())
        current = repository.groups[group["group_id"]]
        current["run_mode"] = "live"
        current["behavior_hash"] = behavior_hash(current)
        service.preview(NORMAL, group["group_id"], {})
        return repository.groups[group["group_id"]]

    def test_manual_live_execute_requires_exact_confirmation_and_persists_result(self):
        service, repository, _ = make_service()
        fake = FakeLiveExecutor()
        service.live_executor = fake
        service.live_pause_enabled = True
        group = self.live_group(service, repository)

        with self.assertRaises(AdControlV3Error) as raised:
            service.execute(NORMAL, group["group_id"], {"confirm": "yes"})
        self.assertEqual("live_execute_confirm_required", raised.exception.code)
        self.assertEqual([], fake.calls)

        result = service.execute(
            NORMAL,
            group["group_id"],
            {"confirm": "EXECUTE_LIVE_RULE_GROUP"},
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["summary"]["succeeded_count"])
        self.assertEqual(1, result["summary"]["meta_write_count"])
        self.assertEqual(1, len(fake.calls))
        stored = repository.executions[result["execution_id"]]
        self.assertEqual("manual_execute", stored["trigger_source"])

    def test_emergency_stop_blocks_before_live_dependency(self):
        service, repository, _ = make_service()
        fake = FakeLiveExecutor()
        service.live_executor = fake
        service.live_pause_enabled = True
        group = self.live_group(service, repository)
        repository.groups[group["group_id"]]["emergency_stopped"] = True
        with self.assertRaises(AdControlV3Error) as raised:
            service.execute(
                NORMAL,
                group["group_id"],
                {"confirm": "EXECUTE_LIVE_RULE_GROUP"},
            )
        self.assertEqual("rule_group_emergency_stopped", raised.exception.code)
        self.assertEqual([], fake.calls)


class SchedulerTests(unittest.TestCase):
    def test_fixed_time_and_cross_midnight_window_use_account_timezone(self):
        # 02:00 UTC is 19:00 on the previous day in Los Angeles.
        now = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
        due, reason, context = candidate_schedule_due(
            {"account_timezone": "America/Los_Angeles"},
            {
                "type": "fixed_time",
                "fixed_time": "19:00",
                "allowed_start_time": "18:00",
                "allowed_end_time": "02:00",
            },
            now,
        )
        self.assertTrue(due)
        self.assertEqual("", reason)
        self.assertEqual("19:00", context["account_local_time"])

    def test_interval_and_event_key_are_minute_idempotent(self):
        now = datetime(2026, 7, 16, 2, 15, tzinfo=timezone.utc)
        due, _, _ = candidate_schedule_due(
            {"account_timezone": "UTC"},
            {"type": "interval", "interval_minutes": 15},
            now,
        )
        self.assertTrue(due)
        self.assertEqual(runner_event_key("g-1", now), runner_event_key("g-1", now.replace(second=59)))
        self.assertNotEqual(runner_event_key("g-1", now), runner_event_key("g-2", now))


class PauseExecutor(FacebookLiveExecutor):
    def __init__(self, client):
        super().__init__(lambda: None, lambda: None, lambda: None, pause_enabled=True)
        self.client = client
        self.store_updates = []

    def _source_rows(self, target):
        return [{"id": 1, "app_id": "app-1", "ad_id": target["object_id"]}]

    def _token(self, product, source_rows):
        return "secret"

    def _client(self, token):
        return self.client

    def _update_store_copy_status(self, level, object_id, status):
        self.store_updates.append((level, object_id, status))


class FakePauseClient:
    def __init__(self, already=False):
        self.already = already
        self.write_count = 0
        self.posts = []

    def get(self, object_id, fields):
        return {
            "id": object_id,
            "account_id": "123",
            "campaign_id": "c-1",
            "adset_id": "set-1",
            "status": "PAUSED" if self.already or self.posts else "ACTIVE",
            "configured_status": "PAUSED" if self.already or self.posts else "ACTIVE",
        }

    def post(self, object_id, values):
        self.posts.append((object_id, dict(values)))
        self.write_count += 1
        return {"success": True}


class LiveExecutorPrimitiveTests(unittest.TestCase):
    def test_pause_verifies_account_and_readback_then_updates_only_ads_ai_copy_rows(self):
        client = FakePauseClient()
        executor = PauseExecutor(client)
        result = executor.execute(
            {"group_id": "g"},
            {
                "channel": "facebook",
                "object_level": "ad",
                "object_id": "ad-1",
                "campaign_id": "c-1",
                "adset_id": "set-1",
                "ad_account_id": "123",
                "product": "Dramawave",
                "action": "pause",
            },
        )
        self.assertEqual("succeeded", result["status"])
        self.assertEqual([("ad-1", {"status": "PAUSED"})], client.posts)
        self.assertEqual([("ad", "ad-1", "PAUSED")], executor.store_updates)

    def test_already_paused_is_idempotent_and_has_zero_meta_writes(self):
        client = FakePauseClient(already=True)
        executor = PauseExecutor(client)
        result = executor.execute(
            {},
            {
                "object_level": "campaign", "object_id": "c-1", "campaign_id": "c-1",
                "ad_account_id": "123", "product": "Dramawave", "action": "pause",
            },
        )
        self.assertEqual("skipped", result["status"])
        self.assertEqual("already_paused", result["reason"])
        self.assertEqual(0, result["meta_write_count"])

    def test_source_code_has_no_kunlunads_update_or_delete(self):
        source = (Path(__file__).resolve().parents[1] / "features" / "ad_control_v3" / "live_execution.py").read_text(encoding="utf-8")
        upper = source.upper()
        self.assertNotIn("UPDATE `KUNLUNADS_DEV`", upper)
        self.assertNotIn("DELETE FROM `KUNLUNADS_DEV`", upper)

    def test_copied_id_is_recorded_before_readback_validation(self):
        class Client:
            write_count = 1

            def post(self, object_id, values):
                return {"copied_campaign_id": "new-campaign"}

            def get(self, object_id, fields):
                return {"id": object_id, "source_campaign_id": "source", "status": "ACTIVE"}

        executor = FacebookLiveExecutor(lambda: None, lambda: None, lambda: None)
        state = {"campaign": None, "adsets": {}, "ads": {}}
        with self.assertRaises(AdControlV3Error) as raised:
            executor._copy_campaign(Client(), "source", state)
        self.assertEqual("copy_mapping_incomplete", raised.exception.code)
        self.assertEqual("new-campaign", state["campaign"]["id"])


if __name__ == "__main__":
    unittest.main()

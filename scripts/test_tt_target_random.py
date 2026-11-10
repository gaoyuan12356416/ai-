"""Offline midnight preparation and target schedule contract tests."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.social_random_schedule import POLICY
from features.tt_auto_posts.core import TTPostAutoStore
from features.tt_auto_posts.publisher import AutoLiveGates
from features.tt_auto_posts.service import TTAutoPostService
from scripts.test_tt_auto_post_service import (
    MutableClock, FakeAccountRepository, FakeLegacyReader, FakeSelector,
    FakeExecutor, template_payload,
)
from scripts import test_tt_posts_service as pool_fixtures

UTC = timezone.utc


class TargetTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = MutableClock(datetime(2026, 11, 10, 8, tzinfo=UTC))
        self.store = TTPostAutoStore(Path(self.tmp.name) / "auto.db", now_fn=self.clock)
        self.executor = FakeExecutor(AutoLiveGates(True, True, True))
        self.service = TTAutoPostService(self.store, FakeLegacyReader(), FakeAccountRepository(),
                                       FakeSelector(), self.executor, now_fn=self.clock)
        payload = template_payload(mode="random")
        self.template = self.service.create_template(payload)["template"]
        self.service.set_enabled(self.template["id"], True, {"expected_version": 1})

    def midnight(self):
        self.clock.value = datetime(2026, 11, 10, 16, tzinfo=UTC)
        return self.service.tick()

    def test_midnight_creates_every_target_slot_once(self):
        self.assertEqual([], self.service.tick()["created_runs"])
        result = self.midnight()
        self.assertEqual(4, len(result["created_runs"]))
        tasks = self.store.list_tasks()
        by_target = {target: [x.scheduled_at_utc for x in tasks if x.account_id == target] for target in ("640", "641")}
        self.assertEqual([2, 2], [len(x) for x in by_target.values()])
        self.assertNotEqual(by_target["640"], by_target["641"])
        self.assertTrue(all(t.schedule_policy == POLICY for t in tasks))
        self.assertTrue(all(t.scheduled_at_utc >= "2026-11-10T16:00:00" for t in tasks))
        self.assertEqual([], self.service.tick()["created_runs"])
        self.assertEqual(4, len(self.store.list_tasks()))
        self.assertEqual([], self.executor.execute_calls)
        detail = self.service.template(self.template["id"])["template"]
        self.assertEqual(2, len(detail["random_day_plan"]["targets"]))

    def test_native_prepares_future_today_but_never_publishes_early(self):
        self.midnight()
        tasks = sorted(self.store.list_tasks(), key=lambda t: t.scheduled_at_utc)
        task = tasks[-1]
        claim = self.store.claim_next_executable_task(worker_id="test", lease_seconds=60,
                    now=self.clock.value, prepare_ahead_seconds=0, allowed_phases={"selection"}, task_id=task.id)
        self.assertIsNotNone(claim)
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            conn.execute("UPDATE tt_auto_task SET status='ready',claim_token='',lease_expires_at_utc='' WHERE id=?", (task.id,))
            conn.commit()
        self.assertIsNone(self.store.claim_next_executable_task(worker_id="test", lease_seconds=60,
                         now=self.clock.value, allowed_phases={"publish"}, task_id=task.id))
        self.clock.value = datetime.fromisoformat(task.scheduled_at_utc) + timedelta(days=2)
        late = self.store.claim_next_executable_task(worker_id="test", lease_seconds=60,
                         now=self.clock.value, allowed_phases={"publish"}, task_id=task.id)
        self.assertIsNotNone(late)

    def test_edit_retains_today_and_uses_new_count_tomorrow(self):
        self.midnight()
        before = [(t.id, t.account_id, t.scheduled_at_utc) for t in self.store.list_tasks()]
        self.clock.value += timedelta(hours=3)
        payload = template_payload(mode="random")
        payload["schedule"]["daily_count"] = 3
        payload["expected_version"] = 1
        self.service.update_template(self.template["id"], payload)
        self.service.set_enabled(self.template["id"], True, {"expected_version": 2})
        self.assertEqual([], self.service.tick()["created_runs"])
        self.assertEqual(before, [(t.id, t.account_id, t.scheduled_at_utc) for t in self.store.list_tasks()])
        self.clock.value = datetime(2026, 11, 11, 16, tzinfo=UTC)
        self.assertEqual(6, len(self.service.tick()["created_runs"]))

    def test_future_random_day_not_created_by_rolling_lookahead(self):
        self.service.prepare_ahead_seconds = 43200
        self.clock.value = datetime(2026, 11, 10, 15, 59, tzinfo=UTC)
        self.assertEqual([], self.service.tick()["created_runs"])
        self.assertEqual([], self.store.list_tasks())

    def test_native_lookahead_cannot_prepare_next_day_target_task(self):
        self.service._random_times = lambda *args: ["09:01", "17:31"]
        self.midnight()
        task = min(self.store.list_tasks(), key=lambda item: item.scheduled_at_utc)
        # Existing future tasks must remain gated even if a native worker has
        # the legacy twelve-hour preparation window configured.
        self.clock.value = datetime(2026, 11, 10, 15, 59, tzinfo=UTC)
        self.assertIsNone(self.store.claim_next_executable_task(
            worker_id="test", lease_seconds=60, now=self.clock.value,
            prepare_ahead_seconds=43200, allowed_phases={"selection"}, task_id=task.id,
        ))
        self.clock.value = datetime(2026, 11, 10, 16, tzinfo=UTC)
        self.assertIsNotNone(self.store.claim_next_executable_task(
            worker_id="test", lease_seconds=60, now=self.clock.value,
            prepare_ahead_seconds=43200, allowed_phases={"selection"}, task_id=task.id,
        ))

    def test_equal_minutes_on_other_accounts_do_not_merge_runs(self):
        self.service._random_times = lambda *args: ["09:01", "17:31"]
        self.assertEqual(4, len(self.midnight()["created_runs"]))
        self.assertEqual(4, len({run.run_key for run in self.store.list_runs()}))
        self.assertEqual(4, len(self.store.list_tasks()))

    def test_unavailable_account_does_not_stop_other_targets(self):
        self.service.account_repository.accounts.pop("640")
        self.assertEqual(4, len(self.midnight()["created_runs"]))
        tasks = self.store.list_tasks()
        self.assertEqual(2, sum(t.account_id == "640" and t.status == "failed" for t in tasks))
        self.assertEqual(2, sum(t.account_id == "641" and t.status == "pending" for t in tasks))

    def test_restart_repairs_run_before_task_creation_without_extra_accounts(self):
        self.midnight()
        run = self.store.list_runs()[0]
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            conn.execute("DELETE FROM tt_auto_event WHERE task_id IN (SELECT id FROM tt_auto_task WHERE run_id=?)", (run.id,))
            conn.execute("DELETE FROM tt_auto_task WHERE run_id=?", (run.id,))
            conn.commit()
        self.service.tick()
        self.assertEqual(1, len(self.store.list_tasks(run_id=run.id)))
        self.assertEqual(run.target_account_id, self.store.list_tasks(run_id=run.id)[0].account_id)


class TargetPoolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pool_fixtures.ServiceLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service(pool_fixtures.OPEN_GATES)
        self.fixture.add_ready(self.service)
        self.saved = self.service.auto_config_save(self.fixture.auto_config_payload(
            publish_times=[], schedule_mode="random", random_daily_count=1,
        ))["item"]
        self.fixture.clock.value = datetime(2026, 7, 29, 16, tzinfo=UTC)

    def test_pool_freezes_at_midnight_reuses_output_waits_and_can_be_late(self):
        output = self.service.schedules_due({})["items"]
        self.assertEqual(1, len(output))
        queue = self.service.store.get_queue(output[0]["queue_id"])
        self.assertEqual(POLICY, queue["schedule_policy"])
        self.assertEqual("scheduled", queue["status"])
        self.assertTrue(queue["prepared_output_sha256"])
        self.assertEqual([], self.service.store.claim_due("test", now=self.fixture.clock.value))
        self.assertEqual([], self.service.schedules_due({})["items"])
        self.fixture.clock.value += timedelta(days=2)
        claimed = self.service.store.claim_due("test", now=self.fixture.clock.value, grace_seconds=90)
        self.assertEqual(1, len(claimed))
        self.assertEqual("claimed", self.service.store.get_queue(queue["id"])["status"])

    def test_claimed_unbound_random_survives_cross_day_recovery(self):
        plan = self.saved["random_daily_plans"][0]
        publish_time = plan["publish_times"][0]
        scheduled = datetime.fromisoformat(plan["shanghai_date"] + "T" + publish_time).replace(
            tzinfo=timezone(timedelta(hours=8))).astimezone(UTC).isoformat().replace("+00:00", "Z")
        key = "tt-post:auto:v1:101:" + plan["shanghai_date"] + ":" + publish_time.replace(":", "")
        run = self.service.store.claim_recurring_run(key, "auto", "101", plan["shanghai_date"], publish_time,
                    scheduled, config_version=plan["config_version"], required_preparation_profile=self.service.media_profile_version)
        self.assertEqual(POLICY, run["schedule_policy"])
        self.fixture.clock.value += timedelta(days=2)
        output = self.service.schedules_due({})["items"]
        recovered = [item for item in output if item.get("id") == run["id"]]
        self.assertEqual(1, len(recovered))
        self.assertTrue(recovered[0]["queue_id"])
        self.assertNotEqual("failed_preflight", recovered[0]["status"])

    def _assert_random_to_fixed_takes_effect_tomorrow(self, *, legacy=False):
        today = self.service.schedules_due({})["items"][0]
        current_plan = self.service.store.list_random_daily_plans(shanghai_dates=["2026-07-30"])
        self.assertEqual(1, len(self.service.store.list_random_daily_plans(shanghai_dates=["2026-07-31"])))
        if legacy:
            schedule = self.service.store.get_daily_schedule("101")
            self.service.store.save_daily_schedule(
                "101", ["11:00"], enabled=True, expected_version=schedule["version"],
                consent_version="test-fixed-switch", consented_at=self.fixture.clock.value.isoformat(),
            )
        else:
            self.service.auto_config_save(self.fixture.auto_config_payload(
                version=self.saved["version"], publish_times=["11:00"], schedule_mode="fixed",
            ))
        self.assertEqual(current_plan, self.service.store.list_random_daily_plans(shanghai_dates=["2026-07-30"]))
        self.assertEqual([], self.service.store.list_random_daily_plans(shanghai_dates=["2026-07-31"]))
        self.assertEqual([], self.service.schedules_due({})["items"])
        self.assertEqual(POLICY, self.service.store.get_queue(today["queue_id"])["schedule_policy"])
        payload = self.fixture.recurring_material_payload(key="next-day-fixed")
        payload["material_id"] = "9002"
        self.fixture.add_ready(self.service, payload)
        self.fixture.clock.value = datetime(2026, 7, 31, 3, tzinfo=UTC)
        output = self.service.schedules_due({})["items"]
        self.assertEqual(1, len(output))
        self.assertEqual("11:00", output[0]["publish_time"])
        queue = self.service.store.get_queue(output[0]["queue_id"])
        self.assertEqual("", queue["schedule_policy"])
        self.assertEqual("2026-07-31T03:00:00Z", queue["scheduled_at_utc"])

    def test_random_to_fixed_discards_only_future_unstarted_preview(self):
        self._assert_random_to_fixed_takes_effect_tomorrow()

    def test_legacy_fixed_schedule_discards_future_random_preview(self):
        self._assert_random_to_fixed_takes_effect_tomorrow(legacy=True)

    def test_mode_edit_preserves_future_plan_with_existing_reservation(self):
        plan = self.service.store.ensure_random_daily_plans(["2026-07-31"])[0]
        publish_time = plan["publish_times"][0]
        scheduled = datetime.fromisoformat("2026-07-31T" + publish_time).replace(
            tzinfo=timezone(timedelta(hours=8))).astimezone(UTC).isoformat().replace("+00:00", "Z")
        key = "tt-post:auto:v1:101:2026-07-31:" + publish_time.replace(":", "")
        run = self.service.store.claim_recurring_run(
            key, "auto", "101", "2026-07-31", publish_time, scheduled,
            config_version=plan["config_version"], required_preparation_profile=self.service.media_profile_version,
        )
        self.service.auto_config_save(self.fixture.auto_config_payload(
            version=self.saved["version"], publish_times=["11:00"], schedule_mode="fixed",
        ))
        self.assertEqual([plan], self.service.store.list_random_daily_plans(shanghai_dates=["2026-07-31"]))
        self.assertEqual(run, self.service.store.get_recurring_run_by_key(key))


if __name__ == "__main__":
    unittest.main()

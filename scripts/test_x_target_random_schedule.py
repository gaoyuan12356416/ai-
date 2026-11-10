"""Offline acceptance tests for target-specific X midnight preparation."""
import contextlib
import sqlite3
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features import social_random_schedule as schedules
from features.x_posts import service as posts
from features.x_auto_posts.publisher import AutoLiveGates
from scripts.test_x_auto_post_service import XAutoPostServiceTests
from scripts.test_x_auto_post_validation import valid_payload
from scripts.test_x_post_multi_schedule_store import XPostMultiScheduleStoreTests
from scripts.test_x_post_priority_manual_store import material_candidate
from scripts.test_x_post_schedule_runner import FakeSidecar, make_config
from scripts import x_post_schedule_runner as runner


class AutoTargetScheduleTests(XAutoPostServiceTests):
    # Keep the existing fixture without rerunning its unrelated inherited tests.
    def setUp(self):
        super().setUp()
        with self.store._transaction() as conn:
            conn.execute("UPDATE social_random_policy SET effective_date='2026-08-12'")

    def test_target_midnight_independent_stable_and_early_prepare(self):
        service = self.service(AutoLiveGates(True, True, True))
        template = service.create_template(self.actor(valid_payload(schedule={"mode": "random", "daily_count": 3})))["template"]
        service.set_enabled(template["id"], True, self.actor({"expected_version": 1}))
        self.clock.value = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)
        result = service.tick()
        self.assertEqual(len(result["created_runs"]), 6)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 6)
        self.assertEqual({t.schedule_policy for t in tasks}, {schedules.POLICY})
        by_account = {a: sorted(t.scheduled_at_utc for t in tasks if t.account_id == a) for a in ("101", "102")}
        self.assertNotEqual(by_account["101"], by_account["102"])
        self.assertTrue(all(len(times) == 3 for times in by_account.values()))
        self.assertEqual(service.tick()["created_runs"], [])
        claim = self.store.claim_next_executable_task(worker_id="test", lease_seconds=60, now=self.clock.value)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.claim_phase, "selection")
        # A prepared future task must not be claimed as a publication.
        with self.store._transaction() as conn:
            conn.execute("UPDATE x_auto_task SET status='ready',claim_token='',claim_worker='',lease_expires_at_utc='' WHERE id=?", (claim.task.id,))
            conn.execute("UPDATE x_auto_task SET status='canceled' WHERE id<>?", (claim.task.id,))
        self.assertIsNone(self.store.claim_next_executable_task(worker_id="test", lease_seconds=60, now=self.clock.value))
        later = datetime.fromisoformat(claim.task.scheduled_at_utc) + timedelta(days=1)
        self.assertEqual(self.store.claim_next_executable_task(worker_id="test", lease_seconds=60, now=later).claim_phase, "publish")
        self.assertEqual(len(service.template(template["id"])["template"]["target_daily_plans"][0]["targets"]), 2)

    def test_target_same_day_edit_keeps_frozen_plan(self):
        service = self.service(AutoLiveGates(True, True, True))
        payload = valid_payload(schedule={"mode": "random", "daily_count": 2})
        template = service.create_template(self.actor(payload))["template"]
        service.set_enabled(template["id"], True, self.actor({"expected_version": 1}))
        self.clock.value = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)
        service.tick()
        original = [(t.id, t.scheduled_at_utc) for t in self.store.list_tasks()]
        self.clock.value += timedelta(hours=8)
        updated = service.update_template(template["id"], self.actor({**payload, "expected_version": 1, "schedule": {"mode": "random", "daily_count": 4}}))["template"]
        service.set_enabled(template["id"], True, self.actor({"expected_version": updated["version"]}))
        self.assertEqual(service.tick()["created_runs"], [])
        self.assertEqual([(t.id, t.scheduled_at_utc) for t in self.store.list_tasks()], original)

    def test_target_disable_during_tick_does_not_block_other_template(self):
        service = self.service(AutoLiveGates(True, True, True))
        templates = []
        for name in ("paused concurrently", "healthy"):
            payload = valid_payload(schedule={"mode": "random", "daily_count": 2})
            payload["name"] = name
            template = service.create_template(self.actor(payload))["template"]
            service.set_enabled(template["id"], True, self.actor({"expected_version": 1}))
            templates.append(template)
        self.clock.value = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)
        original = service._create_run
        disabled = []
        def create(template, **kwargs):
            if template.id == templates[0]["id"] and not disabled:
                service.set_enabled(template.id, False, self.actor({"expected_version": 1}))
                disabled.append(template.id)
            return original(template, **kwargs)
        with patch.object(service, "_create_run", side_effect=create):
            result = service.tick()
        self.assertEqual(len(result["created_runs"]), 4)
        self.assertEqual({t.template_id for t in self.store.list_tasks()}, {templates[1]["id"]})


class PoolTargetScheduleTests(XPostMultiScheduleStoreTests):
    def setUp(self):
        super().setUp()
        with contextlib.closing(posts._connect(self.db_path)) as conn:
            conn.execute("UPDATE social_random_policy SET effective_date='2026-07-28'")
            conn.commit()

    def test_pool_midnight_all_targets_immutable_and_no_future_freeze(self):
        self.save_random_schedule("material", [2, 3], 3)
        now = datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ)
        due = self.store.due_schedule_slots(now)
        self.assertEqual(sum(len(item["account_ids"]) for item in due["items"]), 6)
        self.assertTrue(all(item["schedule_policy"] == schedules.POLICY for item in due["items"]))
        self.assertTrue(all(item["planned_publish_at_utc"] > schedules.utc_at("2026-07-28") for item in due["items"]))
        snapshot = self.store.get_schedule_config("material", now=now)["random_daily_plans"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(len(snapshot[0]["target_plans"]), 2)
        target_times = {item["account_id"]: item["publish_times"] for item in snapshot[0]["target_plans"]}
        self.assertNotEqual(target_times[2], target_times[3])
        self.store.due_schedule_slots(now + timedelta(hours=18))
        self.assertEqual(self.store.get_schedule_config("material", now=now)["random_daily_plans"], snapshot)
        with contextlib.closing(posts._connect(self.db_path)) as conn:
            self.assertIsNone(schedules.get_day_plan(conn, "x:pool:material", "2026-07-29"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_schedule_run").fetchone()[0], len(due["items"]))

    def test_cross_pool_only_shared_account_times_excluded(self):
        self.save_random_schedule("material", [2, 3], 4)
        self.save_random_schedule("drama", [2, 4], 4)
        now = datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ)
        self.store.due_schedule_slots(now)
        def times(source):
            plan = self.store.get_schedule_config(source, now=now)["random_daily_plans"][0]
            return next(set(t["publish_times"]) for t in plan["target_plans"] if t["account_id"] == 2)
        self.assertFalse(times("material") & times("drama"))

    def test_same_minute_targets_share_run_without_losing_slots(self):
        self.save_random_schedule("material", [2, 3], 1)
        with patch.object(posts, "_generate_random_publish_times", return_value=["19:17"]):
            due = self.store.due_schedule_slots(datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ))
        self.assertEqual(len(due["items"]), 1)
        self.assertEqual(due["items"][0]["account_ids"], [2, 3])
        with contextlib.closing(posts._connect(self.db_path)) as conn:
            slots = conn.execute("SELECT native_id FROM social_random_slot").fetchall()
            self.assertEqual(len(slots), 2)
            self.assertEqual(slots[0][0], slots[1][0])

    def test_drama_prepares_successive_episodes_without_advancing_publication(self):
        self.save_random_schedule("drama", [2], 2)
        pool = self.add_drama(free_episode_count=3)
        midnight = datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ)
        with patch.object(posts, "_generate_random_publish_times", return_value=["12:17", "18:27"]):
            due = self.store.due_schedule_slots(midnight)["items"]
        first = self.store.create_schedule_plan("drama", "2026-07-28", "12:17", due[0]["version"], [self.drama_candidate(pool, 2, 1)], account_languages={2: "en"})
        candidates = self.store.available_drama_pool_items(account_ids=[2], account_languages={2: "en"}, schedule_identity=due[1])
        self.assertEqual(candidates[0]["next_sub_number"], 2)
        second = self.store.create_schedule_plan("drama", "2026-07-28", "18:27", due[1]["version"], [self.drama_candidate(pool, 2, 2)], account_languages={2: "en"})
        with contextlib.closing(posts._connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT next_sub_number,published_episode_count FROM x_post_drama_pool WHERE id=?", (pool["id"],)).fetchone()[0], 1)
        q1, q2 = first["queues"][0], second["queues"][0]
        with self.assertRaises(posts.XPostError) as early:
            self.store.assert_scheduled_publishable(q1["id"], now=midnight)
        self.assertEqual(early.exception.code, "x_post_schedule_not_due")
        later = midnight + timedelta(days=1)
        self.assertTrue(self.store.assert_scheduled_publishable(q1["id"], now=later))
        with self.assertRaises(posts.XPostError) as previous:
            self.store.assert_scheduled_publishable(q2["id"], now=later)
        self.assertEqual(previous.exception.code, "x_post_drama_previous_episode_pending")
        self.publish_queue(q1, 1)
        self.assertTrue(self.store.assert_scheduled_publishable(q2["id"], now=later))
        self.publish_queue(q2, 2)
        with contextlib.closing(posts._connect(self.db_path)) as conn:
            self.assertEqual(tuple(conn.execute("SELECT next_sub_number,published_episode_count FROM x_post_drama_pool WHERE id=?", (pool["id"],)).fetchone()), (3, 2))

    def _parallel_pool_tick(self, source, account_times):
        """Exercise the real threaded tick and SQLite freeze, with no posting."""
        self.save_random_schedule(source, list(account_times), len(next(iter(account_times.values()))))
        midnight = datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ)
        with patch.object(posts, "_generate_random_publish_times", side_effect=list(account_times.values())):
            due = self.store.due_schedule_slots(midnight)["items"]
        fixture = self
        local = threading.local()
        frozen = []
        all_frozen = threading.Event()
        handoff_before_all_frozen = []
        class NativeSidecar(FakeSidecar):
            def query_schedule_plan(self, path, identity):
                local.identity = identity
                return fixture.store.query_schedule_plan(identity["source_type"], identity["run_date"], identity["publish_time"])
            def create_schedule_plan(self, path, payload):
                result = fixture.store.create_schedule_plan(payload["source_type"], payload["run_date"], payload["publish_time"], payload["version"], payload["candidates"], account_languages={int(a): "en" for a in payload["account_ids"]})
                frozen.append((payload["publish_time"], result["queues"][0]))
                if len(frozen) == len(due):
                    all_frozen.set()
                return result["queues"]
        sidecar = NativeSidecar(list(reversed(due)))
        def select(_config, _sidecar, accounts, **kwargs):
            identity = local.identity
            if source == "drama":
                candidates = self.store.available_drama_pool_items(account_ids=identity["account_ids"], account_languages={a: "en" for a in identity["account_ids"]}, schedule_identity=identity)
                candidate = self.drama_candidate(candidates[0], accounts[0]["id"], candidates[0]["next_sub_number"])
            else:
                candidates = self.store.available_pool_items(100, schedule_identity=identity)
                candidate = self.material_candidate(candidates[0], accounts[0]["id"])
            # Widen the former read/freeze race across two actual worker threads.
            time.sleep(0.05)
            candidate["source_date"] = kwargs["source_date"]
            return [candidate]
        def handoff(_config, _sidecar, identity, queues, **kwargs):
            if len(frozen) == 1:
                handoff_before_all_frozen.append(identity["publish_time"])
                self.assertTrue(all_frozen.wait(5), "later planning waited for earlier publication")
            return {"status": "dispatch_queued", "attempted_count": 0, **identity}
        with patch.object(runner, "DEFAULT_WORK_DIR", self.temp.name), patch.object(runner, "pipeline_dispatch_enabled", return_value=True), patch.object(runner, "_publish_frozen_queues", side_effect=handoff):
            result = runner.execute_schedule_tick(replace(make_config(self.temp.name), batch_workers=2), sidecar=sidecar, material_candidate_loader=select, drama_candidate_loader=select, now=midnight)
        self.assertEqual(result["status"], "dispatch_queued", result)
        self.assertEqual(len(frozen), len(due))
        self.assertEqual([item[0] for item in frozen], sorted(item["publish_time"] for item in due))
        self.assertEqual(len(handoff_before_all_frozen), 1)
        return frozen

    def test_parallel_drama_tick_freezes_episodes_in_schedule_order(self):
        self.add_drama(free_episode_count=3)
        frozen = self._parallel_pool_tick("drama", {2: ["12:17", "18:27"]})
        self.assertEqual([q["episode_number"] for _, q in frozen], [1, 2])

    def test_parallel_material_targets_reserve_distinct_fifo_items(self):
        self.add_snapshot_materials(["201", "202"])
        frozen = self._parallel_pool_tick("material", {2: ["12:17"], 3: ["18:27"]})
        self.assertEqual([q["material_id"] for _, q in frozen], ["202", "201"])

    def test_auto_bridge_freezes_not_before_without_delaying_preparation(self):
        planned = "2099-07-28T10:27:00Z"
        args = ("701", 2, "target-random-701", "template-17", 3, "{{drama_name}}\n{{desc}}\n{{url}}", {"user_id": "admin-1", "name": "Admin"})
        run = self.store.create_auto_template_run(*args, schedule_policy=schedules.POLICY, planned_publish_at_utc=planned)
        self.assertEqual(run["planned_publish_at_utc"], planned)
        self.assertFalse(self.store.create_auto_template_run(*args, schedule_policy=schedules.POLICY, planned_publish_at_utc=planned)["created"])
        with self.assertRaises(posts.XPostError):
            self.store.create_auto_template_run(*args, schedule_policy=schedules.POLICY, planned_publish_at_utc="2099-07-28T11:27:00Z")
        self.assertEqual(self.store.claim_manual_run("auto_template")["run"]["id"], run["id"])
        plan = self.store.create_manual_plan(run["id"], [material_candidate(run, 2, "701")], trigger_source="auto_template")
        queue = plan["queues"][0]
        with self.assertRaises(posts.XPostError) as before:
            self.store.assert_scheduled_publishable(queue["id"])
        self.assertEqual(before.exception.code, "x_post_schedule_not_due")
        self.assertTrue(self.store.assert_scheduled_publishable(queue["id"], now=datetime(2099, 7, 29, tzinfo=timezone.utc)))

    def test_fixed_edit_cannot_collide_with_another_pools_frozen_target(self):
        self.save_random_schedule("drama", [2, 3], 1)
        now = datetime(2026, 7, 28, 0, 0, tzinfo=posts.BEIJING_TZ)
        with patch.object(posts, "_generate_random_publish_times", side_effect=[["12:17"], ["15:27"]]):
            self.store.due_schedule_slots(now)
        with self.assertRaises(posts.XPostError) as conflict:
            self.store.save_schedule_config("material", {"enabled": True, "account_ids": [2], "publish_times": ["12:17"], "version": 1}, eligible_account_ids=[2], now=now + timedelta(hours=1))
        self.assertEqual(conflict.exception.code, "x_post_schedule_collision")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (AutoTargetScheduleTests, PoolTargetScheduleTests):
        suite.addTests(cls(name) for name in cls.__dict__ if name.startswith("test_"))
    return suite


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(load_tests(None, None, None))
    sys.exit(not result.wasSuccessful())

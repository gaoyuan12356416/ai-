"""X dispatch planners freeze exact items without media work or publication."""
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch import x_planning
from features.x_auto_posts.publisher import AutoPostExecutor, AutoLiveGates
from scripts import test_x_auto_post_publisher as auto_fixture
from scripts import test_x_post_manual_runner as manual_fixture
from scripts import test_x_post_schedule_runner as schedule_fixture
from scripts import x_post_manual_runner as manual
from scripts import x_post_schedule_runner as schedule
from scripts import test_x_post_priority_manual_store as manual_store_fixture
from scripts import test_x_post_multi_schedule_store as schedule_store_fixture
from features.x_posts import service as post_store


def candidate(material_id="123456"):
    return {"pool_item_id": 7, "pool_created_at": "2026-07-27T00:00:00Z",
            "material_id": material_id, "material_language": "en", "media_kind": "video",
            "source_duration": 30, "content_id": "CONTENT-1", "material_url": "https://media.example.test/source.mp4",
            "material_name": "Episode", "drama_name": "Drama", "tag": "Drama",
            "description": "A complete episode description."}


def account(account_id=640):
    return {"id": account_id, "username": "example", "display_name": "Example", "x_user_id": "123",
            "drama_language": "en", "long_video_eligible": False, "publish_eligible": True}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1"})
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_metadata_candidate_has_no_invented_media_evidence(self):
        item = x_planning.deferred_candidate(account(), candidate(), 1, 1)
        self.assertEqual(item["media_validation_mode"], "deferred")
        self.assertEqual(item["preflight_sha256"], "")
        self.assertEqual(item["preflight_size"], 0)
        self.assertEqual(item["preflight_duration"], 0)
        self.assertEqual(item["material_url"], candidate()["material_url"])
        with self.assertRaises(Exception):
            x_planning.deferred_candidate(account(), {**candidate(), "material_language": "ja"}, 1, 1)

    def test_material_fifo_planning_never_downloads_or_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = schedule_fixture.make_config(str(Path(directory) / "not-a-media-directory"))
            forbidden = mock.Mock(side_effect=AssertionError("media work in planner"))
            items, failures = schedule._preflight_material_candidates(
                config, mock.Mock(), [candidate()], [account()], source_date="2026-07-27",
                timestamp=1, downloader=forbidden, prober=forbidden, repair_client=forbidden,
                assignment_identity={"source_type": "material", "run_date": "2026-07-27", "publish_time": "10:00", "version": 1})
        self.assertEqual(failures, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["media_validation_mode"], "deferred")
        forbidden.assert_not_called()

    def test_manual_new_plan_hands_off_without_old_publisher_or_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            sidecar = manual_fixture.FakeManualSidecar(manual_fixture.manual_run())
            with mock.patch.object(manual, "_manual_candidates", return_value=manual_fixture.planned_candidates()):
                result = manual.execute_manual_tick(schedule_fixture.make_config(directory), sidecar=sidecar)
        self.assertEqual(result["status"], "dispatch_queued")
        self.assertEqual(result["attempted_count"], 0)
        self.assertTrue(any(call[0] == "plan" for call in sidecar.calls))
        self.assertFalse(any(call[0] in {"storage", "publish"} for call in sidecar.calls))

    def test_manual_metadata_pairs_survive_one_bad_item(self):
        with tempfile.TemporaryDirectory() as directory:
            config = schedule_fixture.make_config(directory)
            run = manual_fixture.manual_run()
            connection = mock.Mock()
            with mock.patch.object(manual, "select_manual_candidates", return_value=([candidate("501"), candidate("502")], [])), \
                 mock.patch.object(manual, "_preflight_candidates", side_effect=AssertionError("legacy preparation called")):
                items = manual._manual_candidates(config, run, [account(11), account(12)],
                    connection_factory=lambda _: connection, downloader=mock.Mock(), prober=mock.Mock(), repair_client=mock.Mock())
        self.assertEqual([item["material_id"] for item in items], ["501", "502"])
        self.assertTrue(all(item["media_validation_mode"] == "deferred" for item in items))

    def test_frozen_queues_never_reenter_old_publish(self):
        sidecar = mock.Mock()
        identity = {"source_type": "material", "run_date": "2026-11-02", "publish_time": "10:00", "version": 1}
        result = schedule._publish_frozen_queues(None, sidecar, identity,
                    [{"id": 1, "account_id": 2, "status": "publishing"}], resumed=True)
        self.assertEqual(result["attempted_count"], 0)
        self.assertEqual(result["results"][0]["status"], "publishing")
        sidecar.publish_queue.assert_not_called()


class AutoPlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = auto_fixture.XAutoPostPublisherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_auto_metadata_planning_never_invokes_media_preflight(self):
        fixture = self.fixture
        _token, task = fixture._reserve()
        bridge = auto_fixture.FakeBridge(task)
        bridge.has_queue = False
        captured = []
        bridge.storage_preflight = mock.Mock(side_effect=AssertionError("storage check in planner"))
        def freeze(run_id, body):
            captured.append(body)
            return bridge._run(with_queue=True)
        bridge.create_plan = freeze
        forbidden = mock.Mock(side_effect=AssertionError("media work in planner"))
        executor = AutoPostExecutor(fixture.store, auto_fixture.DummySelector(), bridge,
            lambda *_: candidate(), forbidden, dispatch_enabled=True, now_fn=fixture.clock,
            gates=AutoLiveGates(True, True, True), lease_seconds=120)
        executor._query_or_plan(task, bridge._run(with_queue=False), account())
        self.assertEqual(captured[0]["media_validation_mode"], "deferred")
        self.assertEqual(captured[0]["preflight_duration"], 0)
        forbidden.assert_not_called()
        bridge.storage_preflight.assert_not_called()

    def test_auto_ready_reads_receipt_without_republish_or_recover(self):
        fixture = self.fixture
        task = fixture._ready()
        fixture.clock.value += dt.timedelta(seconds=121)
        bridge = auto_fixture.FakeBridge(task)
        bridge.recover_run = mock.Mock(side_effect=AssertionError("legacy recovery called"))
        executor = fixture._executor(bridge)
        executor.dispatch_enabled = True
        first = executor.execute_next("dispatch-observer")
        self.assertEqual(first["task"]["status"], "retry_wait")
        self.assertFalse(first["task"]["unknown_outcome"])
        import sqlite3
        with sqlite3.connect(fixture.store.db_path) as connection:
            self.assertEqual(connection.execute("SELECT publish_attempt_count FROM x_auto_task WHERE id=?", (task.id,)).fetchone()[0], 0)
        self.assertEqual(bridge.publish_calls, 0)
        self.assertFalse(executor.execute_next("dispatch-observer")["claimed"])
        fixture.clock.value += dt.timedelta(seconds=31)
        bridge.queue_status = "published"
        final = executor.execute_next("dispatch-observer")
        self.assertEqual(final["task"]["status"], "published")
        self.assertEqual(bridge.publish_calls, 0)
        bridge.recover_run.assert_not_called()

    def test_cutover_preserves_old_unqueued_backlog(self):
        fixture = self.fixture
        claim = fixture.store.claim_next_executable_task(worker_id="planner", lease_seconds=120,
            now=fixture.clock(), publish_delegated=True, planning_cutoff=fixture.clock())
        self.assertIsNone(claim)
        self.assertEqual(fixture.store.get_task(fixture.task.id).status, "pending")


class CanonicalClaimTests(unittest.TestCase):
    def test_manual_planner_skips_old_backlog_and_leaves_inflight_untouched(self):
        fixture = manual_store_fixture.XPostPriorityManualStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "0"}), \
             mock.patch.object(post_store, "utc_now", return_value="2026-11-02T01:00:00Z"):
            inflight = fixture.store.create_manual_run(["501"], [2], "inflight", manual_store_fixture.ACTOR)
            fixture.store.claim_manual_run()
            plan = fixture.store.create_manual_plan(inflight["id"], [manual_store_fixture.material_candidate(inflight, 2, "501")])
            log = fixture.store.reserve_log(plan["queues"][0]["id"])
            fixture.store.prepare_log(log["id"], "https://example.com/long", "https://example.com/short", "safe copy")
            fixture.store.mark_publishing(log["id"])
            old = fixture.store.create_manual_run(["502"], [3], "old", manual_store_fixture.ACTOR)
        with mock.patch.object(post_store, "utc_now", return_value="2026-11-02T03:00:00Z"):
            fresh = fixture.store.create_manual_run(["503"], [4], "fresh", manual_store_fixture.ACTOR)
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1", "PIPELINE_X_DISPATCH_CUTOVER_AT": "2026-11-02T02:00:00Z"}):
            claimed = fixture.store.claim_manual_run()
        self.assertEqual(claimed["run"]["id"], fresh["id"])
        self.assertEqual(fixture.store.get_manual_run(old["id"])["status"], "queued")
        current = fixture.store.get_manual_run(inflight["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["queues"][0]["status"], "publishing")

    def test_schedule_planner_skips_old_claims_and_frozen_queues(self):
        fixture = schedule_store_fixture.XPostMultiScheduleStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.save_schedule("material", [2], ["10:00"])
        old_time = dt.datetime(2026, 7, 26, 10, 0, 10, tzinfo=post_store.BEIJING_TZ)
        new_time = dt.datetime(2026, 7, 27, 10, 0, 10, tzinfo=post_store.BEIJING_TZ)
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "0"}):
            fixture.store.due_schedule_slots(old_time)
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1", "PIPELINE_X_DISPATCH_CUTOVER_AT": "2026-07-27T01:00:00Z"}):
            due = fixture.store.due_schedule_slots(new_time)
            self.assertEqual([row["run_date"] for row in due["items"]], ["2026-07-27"])
            pool = fixture.store.add_pool_materials(["105"], actor=manual_store_fixture.ACTOR,
                    validation_checks=[{"material_id": "105", "error_code": ""}])["items"][0]
            fixture.store.create_schedule_plan("material", "2026-07-27", "10:00", due["items"][0]["version"], [fixture.material_candidate(pool, 2)])
            self.assertEqual(fixture.store.due_schedule_slots(new_time)["items"], [])
            fixture.store.due_schedule_slots(new_time + dt.timedelta(days=2, hours=3))
        self.assertEqual(fixture.store.query_schedule_plan("material", "2026-07-26", "10:00")["run"]["status"], "claimed")
        self.assertEqual(fixture.store.query_schedule_plan("material", "2026-07-27", "10:00")["run"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()

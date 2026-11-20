from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock

from features.fb_auto_posts.gpu import PrepareExecutor
from features.fb_auto_posts import hit_material, manual_batch
from features.fb_auto_posts.publisher import AutoPostExecutor, GraphResult
from features.fb_auto_posts.repositories import PageCredential
from features.fb_auto_posts.service import Runtime
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.fb_native import FBNativeBridge
from scripts import test_fb_hit_drama
from scripts.test_fb_auto_validation import payload
from tests import test_fb_hit_material_batch


class OriginalTemplateTests(TestCase):
    def setUp(self):
        fixture = test_fb_hit_drama.HitDramaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store, self.actor = fixture.store, fixture.actor
        self.id = fixture.template["id"]
        self.store.set_template_status(self.id, False, self.actor, 1)
        self.config = dict(payload(), video_template="original")
        self.item = self.store.update_template(self.id, self.config, self.actor, 1)
        self.store.set_template_status(self.id, True, self.actor, self.item["version"])

    def plan(self, slot="manual-original", trigger="manual", **kwargs):
        result = self.store.create_run(self.id, slot, trigger, self.actor, self.fixture.pages,
                                       self.fixture.materials, **kwargs)
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id",
                                                      (result["run_id"],))]

    def test_config_round_trip_copy_and_capacity(self):
        self.assertEqual(self.store.get_template(self.id, self.actor)["config"]["video_template"], "original")
        self.assertEqual(self.store.copy_template(self.id, self.actor, self.item["version"])["config"]["video_template"], "original")
        runtime = Runtime(self.store, self.fixture.pages, self.fixture.materials, Mock(), Mock(), "fixture",
                          metric_store=Mock())
        capacity = runtime.validate_activation(self.item)
        self.assertEqual(capacity["estimated_daily_gpu_jobs"], 0)
        self.assertEqual(capacity["estimated_daily_graph_posts"], 2)
        self.store.set_template_status(self.id, False, self.actor, self.item["version"])
        updated = self.store.update_template(self.id, payload(), self.actor, self.item["version"])
        self.store.set_template_status(self.id, True, self.actor, updated["version"])
        rows = self.plan()
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "planned" and row["gpu_job_id"] for row in rows))

    def test_scheduled_source_reaches_publish_only_at_due_time(self):
        self.store.enqueue_due_slots(live_enabled=True)
        runtime = Runtime(self.store, self.fixture.pages, self.fixture.materials, self.fixture.runtime.executor,
                          Mock(), "fixture", prebuild_enabled=True)
        run = runtime.plan_next("test-planner")
        with self.store.connect() as conn:
            task = dict(conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id LIMIT 1",
                                     (run["run_id"],)).fetchone())
        due = datetime.fromisoformat(task["planned_publish_at_utc"])
        self.assertGreater(due, self.store.now_fn())
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["media_selection_kind"], "template_original")
        self.assertEqual((task["gpu_job_id"], task["prepared_sha256"]), ("", ""))
        gpu = Mock()
        self.assertFalse(PrepareExecutor(self.store, gpu, live_enabled=True).prepare_next("test")["claimed"])
        gpu.prepare.assert_not_called()
        self.assertIsNone(self.store.claim_next("before-due", task_id=task["id"]))
        self.store.now_fn = lambda: due
        adapter = LedgerAdapter(LedgerSpec("fb", "auto", "", "fb_auto_task"))
        job = adapter.project(task, now=due)
        self.assertEqual(job["stage"], "publish")
        bridge = FBNativeBridge(self.fixture.runtime)
        claim = {"claim_token": "fixture-claim-token-01234", "node_id": "test"}
        descriptor = bridge.reserve(dict(job, generation=1), claim)
        self.assertEqual(descriptor["processing_mode"], "original")
        self.assertEqual(descriptor["artifact"]["identity_mode"], "remote_url")
        self.assertEqual(descriptor["request"]["media_url"], task["source_media_url"])
        self.assertEqual(descriptor["request"]["message"], task["message_text"])

    def test_native_publisher_sends_source_and_does_not_resend(self):
        task = self.plan()[0]
        self.fixture.pages.eligible_credentials = lambda pid: [PageCredential(pid, "1", "2", "fixture")]
        graph = Mock()
        graph.publish_video.return_value = GraphResult("success", "12345")
        executor = AutoPostExecutor(self.store, self.fixture.pages, graph, live_enabled=True)
        executor.execute_next("test")
        self.assertEqual(graph.publish_video.call_args.args[2:], (task["source_media_url"], task["message_text"]))
        self.assertIsNone(self.store.claim_next("same-task", task_id=task["id"]))

    def test_original_rejects_a_substituted_url_and_other_pages_continue(self):
        first, other = self.plan()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET media_url=?,prepared_media_url=? WHERE id=?",
                         ("https://media.test/other.mp4", "https://media.test/other.mp4", first["id"]))
        self.assertIsNone(self.store.claim_next("wrong-media", task_id=first["id"]))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,error_code FROM fb_auto_task WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(tuple(row), ("skipped", "fb_auto_prepared_contract_invalid"))
        self.assertIsNotNone(self.store.claim_next("other", task_id=other["id"]))

    def test_hit_drama_uses_the_frozen_mode(self):
        result = test_fb_hit_drama.publish_drama(self.fixture.runtime, self.id, self.actor,
                                                 dict(self.fixture.request, expected_version=self.item["version"]))
        with self.store.connect() as conn:
            run = conn.execute("SELECT video_template FROM fb_auto_run WHERE id=?", (result["run_id"],)).fetchone()
            row = conn.execute("SELECT status,media_url,source_media_url FROM fb_auto_task WHERE run_id=?",
                               (result["run_id"],)).fetchone()
        self.assertEqual(run[0], "original")
        self.assertEqual((row["status"], row["media_url"]), ("ready", row["source_media_url"]))

    def test_single_hit_material_uses_original(self):
        runtime = self.fixture.runtime
        runtime.resolve_source = Mock()
        material = self.fixture.materials.candidate_snapshot({"language": "en"}).candidates[0]
        runtime.materials.exact_material = Mock(return_value=material)
        result = hit_material.publish_material(runtime, self.id, self.actor, dict(
            expected_version=self.item["version"], material_id=material.material_id, operation_id="single-original"))
        with self.store.connect() as conn:
            task = conn.execute("SELECT * FROM fb_auto_task WHERE status='ready' AND run_id=?",
                                (result["run_id"],)).fetchone()
        self.assertEqual((task["video_template"], task["media_url"]), ("original", material.media_url))

    def test_operator_batch_freezes_the_tls_source(self):
        pages = self.fixture.pages.list_pages()
        material = dict(material_id="41", content_id="42", language="en", series_code="series",
                        material_name="Material", drama_name="Drama", drama_description="Description",
                        duration_seconds=60, media_url="http://" + manual_batch.COS_SOURCE_HOST + "/video.mp4")
        with self.store.connect() as conn:
            template = dict(self.store._template_row(conn, self.id, self.actor))
            manifest = manual_batch.build_manifest(conn, template, pages, {"41": material}, ["41"], "manual-original",
                                                    waived_series="series", waive_drama_cooldown=False, now=self.store.now_fn())
        result = manual_batch.reserve_batch(self.store, manifest, max_jobs=10, max_daily_jobs=20, automatic_daily_jobs=0)
        with self.store.connect() as conn:
            task = dict(conn.execute("SELECT * FROM fb_auto_task WHERE run_id=?", (result["run_id"],)).fetchone())
            self.assertIsNotNone(manual_batch.task_policy(conn, task))
        self.assertEqual(task["media_url"], manual_batch.prepare_source_url(material["media_url"]))
        self.assertEqual((task["status"], task["gpu_job_id"]), ("ready", ""))


class HitMaterialOriginalTests(TestCase):
    def test_batch_uses_original_and_keeps_the_per_page_schedule(self):
        fixture = test_fb_hit_material_batch.BatchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store, tid, actor = fixture.store, fixture.template["id"], fixture.actor
        store.set_template_status(tid, False, actor, 1)
        template = store.update_template(tid, dict(payload(), video_template="original"), actor, 1)
        request = dict(fixture.request, expected_version=template["version"])
        first = fixture.publish(request)
        self.assertEqual(first["queued"], 2)
        self.assertTrue(fixture.publish(request)["idempotent"])
        with store.connect() as conn:
            rows = conn.execute("SELECT * FROM fb_auto_task WHERE run_id=?", (first["run_id"],)).fetchall()
        for task in rows:
            self.assertEqual(task["video_template"], "original")
            self.assertEqual(task["status"], "ready")
            self.assertEqual(task["media_url"], task["source_media_url"])
            self.assertEqual(task["planned_publish_at_utc"], (fixture.future-timedelta(minutes=30)).isoformat())

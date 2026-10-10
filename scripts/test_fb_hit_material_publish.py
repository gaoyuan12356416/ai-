import copy
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError, utc_iso
from features.fb_auto_posts.gpu import PrepareExecutor
from features.fb_auto_posts.hit_material import MARKER, normalize_request, publish_material
from features.fb_auto_posts.manual_batch import COS_SOURCE_HOST, task_policy
from features.fb_auto_posts.publisher import AutoPostExecutor, GraphResult
from features.fb_auto_posts.repositories import MaterialCandidate, MaterialRepository, PageCredential, PageGroup, PageTarget, RepositoryError, _probe_exact_material_duration
from features.fb_auto_posts.service import Handler, Runtime
from features.fb_auto_posts.validation import normalize_template_payload
from scripts.test_fb_auto_validation import payload


class HitMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(self.tmp.name) / "fb.sqlite3", now_fn=lambda: self.now)
        self.actor = ActorScope("operator", "Operator", True, "248")
        self.raw = payload()
        self.raw.update(cooldown_days=14, drama_cooldown_hours=24, message_template="{{url}}\n{{desc}}")
        self.template = self.store.create_template(self.raw, self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(self.template["id"], True, self.actor, self.template["version"])
        self.page_rows = [PageTarget("6", ("6",), str(10001 + i), "248", "UTC", language, 1, "Page " + language)
                          for i, language in enumerate(("en", "es", "", "zh-tw"))]
        self.pages = Mock()
        self.pages.list_groups.return_value = [PageGroup("6", "248", 0, "Pages", "1479", "Dramawave", 4, 4)]
        self.pages.list_pages.side_effect = lambda *_args, **_kwargs: list(self.page_rows)
        self.pages.legacy_conflicts.return_value = []
        self.pages.eligible_credentials.side_effect = lambda page_id: [PageCredential(page_id, "fb-user", "c1", "secret")]
        self.material = MaterialCandidate("1234", "drama-en", "https://" + COS_SOURCE_HOST + "/clip.mp4", "Material",
                                          "Drama", "en", Decimal(715), Decimal(0), None, Decimal(0), None, "1", "Description", "FBmanual")
        self.materials = Mock()
        self.materials.exact_material.return_value = self.material
        self.executor = Mock(live_enabled=True)
        self.runtime = Runtime(self.store, self.pages, self.materials, self.executor, Mock(), "x" * 32,
                               max_daily_jobs=100, max_jobs_per_slot=10, max_publishable_pages=10)
        self.request = {"expected_version": 1, "material_id": "1234", "operation_id": "ui-operation"}

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, **values):
        return publish_material(self.runtime, self.template["id"], self.actor, {**self.request, **values})

    def seed(self, page_id, status, *, material_id="1234", unknown=0, created=None, completed="", content_id="drama-en"):
        with self.store.connect() as conn:
            return conn.execute("""INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,
                material_id,content_id,created_at_utc,completed_at_utc,unknown_outcome) VALUES(0,1,1,?,'6',?,?,?,?,?,?)""",
                (page_id, status, material_id, content_id, utc_iso(created or self.now), completed, unknown)).lastrowid

    def rows(self, run_id):
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id", (run_id,))]

    def ready(self, run_id):
        with self.store.connect() as conn:
            conn.execute("""UPDATE fb_auto_task SET status='ready',prepared_at_utc=created_at_utc,
                media_url='https://cdn.example/prepared.mp4',prepared_media_url='https://cdn.example/prepared.mp4',
                prepared_sha256=?,prepared_size_bytes=100,prepared_duration_seconds='715',
                prepared_profile='tt-post-random-overlay-h264-720x1280-v3' WHERE run_id=? AND status='planned'""", ("a" * 64, run_id))

    def test_one_material_per_page_including_other_or_missing_language(self):
        result = self.publish()
        self.assertEqual((result["total_pages"], result["queued"], result["skipped"]), (4, 4, 0))
        tasks = self.rows(result["run_id"])
        self.assertEqual({t["material_id"] for t in tasks}, {"1234"})
        self.assertEqual(len({t["page_id"] for t in tasks}), 4)
        self.assertEqual(len({t["gpu_job_id"] for t in tasks}), 4)
        self.assertTrue(all(t["short_url"] in t["message_text"] and "Description" in t["message_text"] for t in tasks))
        self.assertTrue(all("af_channel=AIpost" in t["long_url"] for t in tasks))
        self.materials.candidate_snapshot.assert_not_called()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_due_slot").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_schedule_plan").fetchone()[0], 0)
            self.assertEqual(task_policy(conn, tasks[0])["waive_drama_cooldown"], True)

    def test_paused_missing_authorization_unknown_and_cooldown_are_independent_skips(self):
        changed = copy.deepcopy(self.raw)
        changed["page_daily_limits"] = [{"page_id": "10001", "daily_count": 0}]
        self.store.set_template_status(1, False, self.actor, 1)
        updated = self.store.update_template(1, changed, self.actor, 1, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(1, True, self.actor, updated["version"])
        self.request["expected_version"] = updated["version"]
        self.page_rows[1] = replace(self.page_rows[1], eligible_token_count=0)
        self.seed("10003", "failed", unknown=1)
        self.seed("10004", "published")
        result = self.publish()
        self.assertEqual((result["queued"], result["skipped"]), (0, 4))
        reasons = {row["page_id"]: row["reason"] for row in result["skipped_pages"]}
        self.assertEqual(reasons, {"10001": "fb_auto_page_frequency_limit", "10002": "fb_page_missing_eligible_token",
                                   "10003": "fb_auto_page_unknown_block", "10004": "fb_auto_material_cooldown"})
        run = self.store.get_run(result["run_id"], self.actor)["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["skipped_pages"], result["skipped_pages"])
        self.assertIsNone(self.store.claim_prepare_next("gpu"))

    def test_retry_returns_frozen_receipt_after_template_pool_source_or_gates_change(self):
        first = self.publish()
        self.store.set_template_status(1, False, self.actor, 1)
        self.store.update_template(1, {**self.raw, "name": "Changed"}, self.actor, 1)
        self.store.set_template_status(1, False, self.actor, 2)
        self.runtime.executor.live_enabled = False
        self.runtime.prebuild_enabled = False
        self.pages.list_pages.side_effect = AssertionError("Retry must not re-read Pages")
        self.materials.exact_material.side_effect = AssertionError("Retry must not re-read material")
        again = self.publish()
        self.assertEqual({**first, "idempotent": True}, again)
        self.assertEqual(len(self.rows(first["run_id"])), 4)
        for values in ({"material_id": "9999"}, {"expected_version": 2}):
            with self.assertRaises(StoreError) as caught:
                self.publish(**values)
            self.assertEqual(caught.exception.code, "fb_auto_operation_conflict")

    def test_actor_scope_applies_even_to_idempotent_retry(self):
        self.publish()
        unauthorized = ActorScope("other", "Other", False, "999")
        with self.assertRaises(StoreError) as caught:
            publish_material(self.runtime, 1, unauthorized, self.request)
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_payload_and_closed_gate_create_nothing(self):
        invalid = [{**self.request, "source_url": self.material.media_url}, {**self.request, "manifest": {}},
                   {**self.request, "material_id": [1234]}, {**self.request, "material_id": True},
                   {**self.request, "expected_version": 1.5}, {**self.request, "operation_id": "../evil"}]
        for data in invalid:
            with self.assertRaises(StoreError):
                normalize_request(1, data)
        self.runtime.executor.live_enabled = False
        with self.assertRaises(StoreError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "fb_auto_live_gate_closed")
        self.runtime.executor.live_enabled = True
        self.runtime.prebuild_enabled = False
        with self.assertRaises(StoreError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "fb_auto_prebuild_gate_closed")
        self.materials.exact_material.assert_not_called()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_both_capacity_limits_and_shared_failures_roll_back_without_partial_run(self):
        self.runtime.max_jobs_per_slot = 3
        with self.assertRaises(StoreError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "fb_auto_capacity_exceeded")
        self.runtime.max_jobs_per_slot = 10
        self.runtime.max_daily_jobs = 7  # 4 planned automatic + 4 requested manual.
        with self.assertRaises(StoreError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "fb_auto_capacity_exceeded")
        self.runtime.max_daily_jobs = 100
        self.materials.exact_material.side_effect = RepositoryError("fb_auto_source_query_failed", "Unavailable")
        with self.assertRaises(RepositoryError):
            self.publish()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_version_changed_during_source_read_is_rejected(self):
        def changed(*_args):
            self.store.set_template_status(1, False, self.actor, 1)
            self.store.update_template(1, {**self.raw, "name": "Changed"}, self.actor, 1)
            return self.material
        self.materials.exact_material.side_effect = changed
        with self.assertRaises(StoreError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "fb_auto_version_conflict")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def concurrent(self, operation_ids):
        barrier = threading.Barrier(2)
        self.materials.exact_material.side_effect = lambda *_args: (barrier.wait(3), self.material)[1]
        # Separate store instances exercise SQLite's cross-process transaction gate.
        other_store = FBAutoPostStore(self.store.path, now_fn=lambda: self.now)
        other_runtime = Runtime(other_store, self.pages, self.materials, Mock(live_enabled=True), Mock(), "x" * 32,
                                max_daily_jobs=100, max_jobs_per_slot=10, max_publishable_pages=10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(publish_material, runtime, 1, self.actor, {**self.request, "operation_id": operation})
                    for runtime, operation in zip((self.runtime, other_runtime), operation_ids)]
            return [job.result(5) for job in jobs]

    def test_concurrent_retries_create_one_run_and_one_task_per_page(self):
        results = self.concurrent(("same", "same"))
        self.assertEqual({item["run_id"] for item in results}, {1})
        self.assertEqual(sorted(item["idempotent"] for item in results), [False, True])
        self.assertEqual(len(self.rows(1)), 4)

    def test_different_operations_still_reserve_same_page_material_once(self):
        results = self.concurrent(("one", "two"))
        self.assertEqual(sorted(item["queued"] for item in results), [0, 4])
        skipped = next(item for item in results if item["queued"] == 0)
        self.assertEqual({item["reason"] for item in skipped["skipped_pages"]}, {"fb_auto_material_active"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='planned'").fetchone()[0], 4)

    def test_preparation_and_publish_use_existing_chain_and_waive_only_drama_cooldown(self):
        for page in self.page_rows:
            self.seed(page.page_id, "published", material_id="different")
        result = self.publish()
        gpu = Mock()
        gpu.prepare.return_value = {"media_url": "https://cdn.example/prepared.mp4", "sha256": "a" * 64,
                                    "size_bytes": 100, "duration_seconds": "715", "profile": "tt-post-random-overlay-h264-720x1280-v3"}
        preparer = PrepareExecutor(self.store, gpu, live_enabled=True)
        for _ in self.page_rows:
            self.assertEqual(preparer.prepare_next("gpu")["status"], "ready")
        self.assertEqual(gpu.prepare.call_count, 4)
        self.assertEqual({call.kwargs["source_url"] for call in gpu.prepare.call_args_list}, {self.material.media_url})
        self.assertEqual(self.store.claim_next("publisher")["material_id"], "1234")

    def test_material_and_unknown_holds_are_rechecked_immediately_before_publication(self):
        result = self.publish()
        self.ready(result["run_id"])
        self.seed("10001", "published")
        self.seed("10002", "failed", unknown=1)
        self.assertEqual(self.store.claim_next("publisher")["page_id"], "10003")
        reasons = {task["page_id"]: task["skip_reason"] for task in self.rows(result["run_id"]) if task["status"] == "skipped"}
        self.assertEqual(reasons, {"10001": "fb_auto_material_cooldown_at_publish", "10002": "fb_auto_page_unknown_at_publish"})

    def test_unknown_ledger_receipt_also_blocks_if_task_status_has_drifted(self):
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,unknown_outcome,created_at_utc,updated_at_utc)
                VALUES(999,'10001','other','unknown',1,?,?)""", (utc_iso(self.now), utc_iso(self.now)))
        result = self.publish()
        self.assertEqual(result["skipped_pages"], [{"page_id": "10001", "reason": "fb_auto_page_unknown_block"}])
        self.ready(result["run_id"])
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,unknown_outcome,created_at_utc,updated_at_utc)
                VALUES(998,'10002','other','unknown',1,?,?)""", (utc_iso(self.now), utc_iso(self.now)))
        self.assertEqual(self.store.claim_next("publisher")["page_id"], "10003")

    def test_fourteen_day_rule_keeps_creation_time_basis_at_reserve_and_publish(self):
        self.page_rows = self.page_rows[:1]
        self.seed("10001", "published", created=self.now - timedelta(days=15), completed=utc_iso(self.now))
        result = self.publish()
        self.assertEqual(result["queued"], 1)
        self.ready(result["run_id"])
        self.assertEqual(self.store.claim_next("publisher")["page_id"], "10001")

    def test_unknown_graph_outcome_is_never_replayed(self):
        self.page_rows = self.page_rows[:1]
        result = self.publish()
        self.ready(result["run_id"])
        graph = Mock()
        graph.publish_video.return_value = GraphResult("unknown", error_code="graph_timeout", message="unknown")
        executor = AutoPostExecutor(self.store, self.pages, graph, live_enabled=True,
                                    min_request_interval_seconds=0, short_link_writer=lambda *_args: None)
        self.assertEqual(executor.execute_next("publisher")["status"], "unknown")
        executor.execute_next("publisher")
        retry = self.publish()
        self.assertTrue(retry["idempotent"])
        blocked = self.publish(operation_id="another-operation")
        self.assertEqual(blocked["skipped_pages"], [{"page_id": "10001", "reason": "fb_auto_page_unknown_block"}])
        self.assertEqual(graph.publish_video.call_count, 1)

    def test_tampered_receipt_does_not_get_manual_policy(self):
        result = self.publish()
        self.ready(result["run_id"])
        with self.store.connect() as conn:
            config = json.loads(conn.execute("SELECT config_json FROM fb_auto_run WHERE id=?", (result["run_id"],)).fetchone()[0])
            config[MARKER]["manifest"]["entries"][0]["material_id"] = "other"
            conn.execute("UPDATE fb_auto_run SET config_json=? WHERE id=?", (json.dumps(config), result["run_id"]))
        self.assertIsNone(self.store.claim_next("publisher"))
        self.assertEqual({task["error_code"] for task in self.rows(result["run_id"])}, {"fb_manual_batch_invalid"})

    def test_malformed_assignment_fails_preparation_before_gpu_call(self):
        result = self.publish()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET selection_json='invalid json' WHERE run_id=?", (result["run_id"],))
        gpu = Mock()
        outcome = PrepareExecutor(self.store, gpu, live_enabled=True).prepare_next("gpu")
        self.assertEqual(outcome["status"], "failed")
        gpu.prepare.assert_not_called()

    def test_http_existing_route_preserves_old_run_now_and_returns_safe_receipt(self):
        runtime = self.runtime
        class TestHandler(Handler):
            pass
        TestHandler.runtime = runtime
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/admin/fb-auto-publish/templates/1/run-now", json.dumps({"_actor": asdict(self.actor), **body}),
                                   {"Authorization": "Bearer " + "x" * 32, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        status, result = post(self.request)
        self.assertEqual(status, 202)
        self.assertEqual(result["queued"], 4)
        status, again = post(self.request)
        self.assertTrue(again["idempotent"])
        status, old = post({"expected_version": 1, "operation_id": "ordinary-run-operation"})
        self.assertEqual(status, 202)
        self.assertIn("due_slot_id", old)
        status, rejected = post({**self.request, "source_url": "https://bad.example/source.mp4"})
        self.assertEqual((status, rejected["code"]), (400, "invalid_request"))


class ExactMaterialRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.mysql = Mock(schema="source", blacklist_schema="settings")
        self.source = {"material_id": "1234", "data_source": 6, "product": "Dramawave", "type": 2, "is_delete": 0,
                       "content_id": "drama-en", "language": "en", "media_url": "http://" + COS_SOURCE_HOST + "/clip.mp4",
                       "material_name": "Material", "material_tag": "Tag", "video_duration": "715"}
        self.drama = {"id": 1, "drama_name": "Drama", "language": "en", "resource_type_v2": "1", "series_code": "123"}
        self.description = {"drama_description": "A description", "description_count": 1}
        self.blocked = []
        self.mysql.select.side_effect = self.select
        self.probe = Mock(return_value=Decimal("715"))
        self.repo = MaterialRepository(self.mysql, now_fn=lambda: datetime(2026, 10, 10, tzinfo=timezone.utc), duration_probe=self.probe)
        self.config = normalize_template_payload({**payload(), "message_template": "{{desc}} {{url}}"})
        self.config.update(app_id="1479", product="Dramawave")

    def select(self, sql, _params):
        if "ads_custom_source" in sql: return [self.source]
        if "ads_facebook_post_blacklist" in sql: return self.blocked
        if "ads_drama_info" in sql: return [self.drama]
        if "ads_drama_resource" in sql: return [self.description]
        raise AssertionError(sql)

    def test_exact_sql_source_uses_bound_id_and_ignores_automatic_metric_duration_and_language_filters(self):
        self.config["material_rule"].update(duration_max_seconds=20, spend_min="9999")
        self.config["drama_rule"]["resource_type_v2"] = ["5"]
        self.config["language"] = "es"
        material = self.repo.exact_material(self.config, "1234")
        self.assertEqual((material.material_id, material.language, material.duration_seconds), ("1234", "en", Decimal(715)))
        self.assertEqual(material.media_url, self.source["media_url"].replace("http:", "https:", 1))
        self.assertEqual(self.mysql.select.call_args_list[0].args[1], ("1234",))
        self.assertNotIn("spend", self.mysql.select.call_args_list[0].args[0])

    def test_invalid_source_blacklist_or_ambiguous_description_rejected(self):
        for key, value in (("type", 1), ("is_delete", 1), ("product", "Other"), ("video_duration", 3601),
                           ("media_url", "https://elsewhere.example/video.mp4")):
            original = self.source[key]
            self.source[key] = value
            with self.assertRaises(RepositoryError):
                self.repo.exact_material(self.config, "1234")
            self.source[key] = original
        self.blocked = [{"id": 8}]
        with self.assertRaises(RepositoryError) as caught:
            self.repo.exact_material(self.config, "1234")
        self.assertEqual(caught.exception.code, "fb_auto_material_blacklisted")
        self.blocked = []
        self.description["description_count"] = 2
        with self.assertRaises(RepositoryError) as caught:
            self.repo.exact_material(self.config, "1234")
        self.assertEqual(caught.exception.code, "fb_auto_material_description_invalid")

    def test_missing_source_duration_probes_only_validated_https_media(self):
        for missing in (0, None, "", -1):
            self.source["video_duration"] = missing
            material = self.repo.exact_material(self.config, "1234")
            self.assertEqual(material.duration_seconds, Decimal("715"))
            self.probe.assert_called_with(self.source["media_url"].replace("http:", "https:", 1))
        self.probe.reset_mock()
        self.source["media_url"] = "https://outside.example/clip.mp4"
        with self.assertRaises(RepositoryError):
            self.repo.exact_material(self.config, "1234")
        self.probe.assert_not_called()

    def test_probe_timeout_and_invalid_actual_duration_fail_closed(self):
        self.source["video_duration"] = 0
        self.probe.side_effect = subprocess.TimeoutExpired("ffprobe", 30)
        with self.assertRaises(RepositoryError) as caught:
            self.repo.exact_material(self.config, "1234")
        self.assertEqual(caught.exception.code, "fb_auto_material_probe_failed")
        self.probe.side_effect = None
        for invalid in (0, -1, 3601, "NaN", "Infinity", "invalid"):
            self.probe.return_value = invalid
            with self.assertRaises(RepositoryError):
                self.repo.exact_material(self.config, "1234")

    def test_ffprobe_is_read_only_bounded_and_has_no_shell_or_error_echo(self):
        source = "https://" + COS_SOURCE_HOST + "/clip.mp4"
        with patch("features.fb_auto_posts.repositories.subprocess.run", return_value=Mock(returncode=0, stdout=b'{"format":{"duration":"715"}}')) as run:
            self.assertEqual(_probe_exact_material_duration(source), Decimal("715"))
            self.assertIsInstance(run.call_args.args[0], list)
            self.assertEqual(run.call_args.args[0][-1], source)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        for output in (b"bad JSON", b"x" * 8193):
            with patch("features.fb_auto_posts.repositories.subprocess.run", return_value=Mock(returncode=0, stdout=output)):
                with self.assertRaises(RepositoryError):
                    _probe_exact_material_duration(source)
        with patch("features.fb_auto_posts.repositories.subprocess.run", side_effect=subprocess.TimeoutExpired(["ffprobe", source], 30)):
            with self.assertRaises(RepositoryError) as caught:
                _probe_exact_material_duration(source)
            self.assertNotIn(source, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

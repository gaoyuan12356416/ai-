import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.manual_batch import BatchError, COS_SOURCE_HOST, build_manifest, canonical, digest, prepare_source_url, reserve_batch, task_policy
from features.fb_auto_posts.repositories import PageTarget
from scripts.test_fb_auto_validation import payload


class ManualBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(self.tmp.name) / "fb.sqlite3", now_fn=lambda: self.now)
        raw = payload()
        raw.update(group_ids=["62"], message_template="{{url}}\n{{desc}}", drama_cooldown_hours=24)
        self.actor = ActorScope("operator", "Operator", True, "248")
        t = self.store.create_template(raw, self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(t["id"], True, self.actor, t["version"])
        with self.store.connect() as c:
            self.template = dict(c.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?", (t["id"],)).fetchone())
        self.pages = []
        self.materials, self.ids = {}, []
        base = 1000
        for language, count, mids in [("en", 67, ["11", "12", "13"]), ("es", 18, ["21", "22", "23"]), ("zh-tw", 8, ["31", "32", "33", "34"]), ("id", 13, ["41"]), ("th", 1, ["51"])]:
            self.pages += [PageTarget("62", ("62",), str(base + i), "248", "UTC", language, 1, "Page " + str(base + i)) for i in range(count)]
            base += 1000
            for mid in mids:
                self.ids.append(mid)
                self.materials[mid] = {"material_id": mid, "content_id": "drama-" + language, "language": language, "series_code": "25639", "duration_seconds": "715", "media_url": "https://cdn.example/" + mid + ".mp4", "material_name": "Material " + mid, "drama_name": "Drama", "drama_description": "Description", "material_tag": "FBmanual"}

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self):
        with self.store.connect() as conn:
            return build_manifest(conn, self.template, self.pages, self.materials, self.ids, "test-batch", waived_series="25639", waive_drama_cooldown=True, now=self.now)

    def reserve(self, manifest=None, **kwargs):
        return reserve_batch(self.store, manifest or self.manifest(), max_jobs=kwargs.get("max_jobs", 200), max_daily_jobs=kwargs.get("max_daily_jobs", 1000), automatic_daily_jobs=kwargs.get("automatic_daily_jobs", 526))

    def ready(self, task_ids=None):
        with self.store.connect() as conn:
            params = []
            clause = ""
            if task_ids is not None:
                clause = " AND id IN (" + ",".join("?" for _ in task_ids) + ")"
                params = task_ids
            conn.execute("UPDATE fb_auto_task SET status='ready',prepared_at_utc=created_at_utc,media_url='https://cdn.example/prepared.mp4',prepared_media_url='https://cdn.example/prepared.mp4',prepared_sha256='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',prepared_size_bytes=100,prepared_duration_seconds='715',prepared_profile='tt-post-random-overlay-h264-720x1280-v3' WHERE status='planned'" + clause, params)

    def test_exact_distribution_one_task_per_page_and_interleaved_languages(self):
        m = self.manifest()
        self.assertEqual(len(m["entries"]), 107)
        self.assertEqual([e["language"] for e in m["entries"][:5]], ["en", "es", "zh-tw", "id", "th"])
        self.assertEqual(Counter(e["material_id"] for e in m["entries"]), Counter({"11": 23, "12": 22, "13": 22, "21": 6, "22": 6, "23": 6, "31": 2, "32": 2, "33": 2, "34": 2, "41": 13, "51": 1}))
        result = self.reserve(m)
        again = self.reserve(m)
        self.assertTrue(again["idempotent"])
        self.assertEqual(result["run_id"], again["run_id"])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*),COUNT(DISTINCT page_id) FROM fb_auto_task").fetchone()[:], (107, 107))
            row = dict(c.execute("SELECT * FROM fb_auto_task ORDER BY id LIMIT 1").fetchone())
            self.assertIn(row["short_url"], row["message_text"])
            self.assertIn("af_channel=AIpost", row["long_url"])
            self.assertEqual(task_policy(c, row)["waive_drama_cooldown"], True)

    def test_out_of_order_preparation_and_concurrent_claims_preserve_request_order(self):
        self.reserve()
        with self.store.connect() as c:
            first, second = [r[0] for r in c.execute("SELECT id FROM fb_auto_task ORDER BY id LIMIT 2")]
        self.ready([second])
        self.assertIsNone(self.store.claim_next("worker-two"))
        self.ready([first])
        self.assertEqual(self.store.claim_next("worker-one")["id"], first)
        self.assertIsNone(self.store.claim_next("worker-two"))
        self.store.complete_submitted_with_attempt(first, 1, credential_id="c", fb_user_id="u", graph_post_id="123456", trace_id="", definite_attempts=0)
        self.assertEqual(self.store.claim_next("worker-two")["id"], second)

    def test_unknown_and_missing_authorization_do_not_consume_rotation(self):
        blocked = self.pages[0]
        self.pages.insert(1, PageTarget("62", ("62",), "999", "248", "UTC", "en", 0, "No token"))
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,created_at_utc,unknown_outcome) VALUES(0,1,1,?,'62','unknown',?,1)", (blocked.page_id, self.now.isoformat()))
        m = self.manifest()
        first_en = next(e for e in m["entries"] if e["language"] == "en")
        self.assertEqual((first_en["page_id"], first_en["material_id"]), (self.pages[2].page_id, "11"))
        self.assertEqual({s["reason"] for s in m["skipped_pages"]}, {"page_unknown", "missing_eligible_token"})

    def test_cooldown_chooses_next_material_and_does_not_duplicate_page(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,created_at_utc) VALUES(0,1,1,?,'62','published','11',?)", (self.pages[0].page_id, self.now.isoformat()))
        m = self.manifest()
        en = [e for e in m["entries"] if e["language"] == "en"]
        self.assertEqual([e["material_id"] for e in en[:3]], ["12", "13", "11"])
        self.assertEqual(len({e["page_id"] for e in m["entries"]}), 107)

    def test_changed_holds_and_capacity_fail_without_partial_writes(self):
        m = self.manifest()
        with self.assertRaises(BatchError):
            self.reserve(m, max_jobs=100)
        with self.assertRaises(BatchError):
            self.reserve(m, max_daily_jobs=600)
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,created_at_utc) VALUES(0,1,1,?,'62','unknown',?)", (self.pages[0].page_id, self.now.isoformat()))
        with self.assertRaises(BatchError):
            self.reserve(m)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_receipt_tampering_fails_closed(self):
        result = self.reserve()
        self.ready()
        with self.store.connect() as c:
            config = json.loads(c.execute("SELECT config_json FROM fb_auto_run WHERE id=?", (result["run_id"],)).fetchone()[0])
            config["operator_material_batch"]["manifest"]["entries"][0]["material_id"] = "wrong"
            c.execute("UPDATE fb_auto_run SET config_json=? WHERE id=?", (canonical(config), result["run_id"]))
        self.assertIsNone(self.store.claim_next("worker"))
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='skipped' AND error_code='fb_manual_batch_invalid'").fetchone()[0], 107)

    def test_manual_batch_rechecks_same_material_and_unknown_before_submission(self):
        self.reserve()
        self.ready()
        with self.store.connect() as c:
            first, second, third = [dict(r) for r in c.execute("SELECT * FROM fb_auto_task ORDER BY id LIMIT 3")]
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,created_at_utc,completed_at_utc) VALUES(0,1,1,?,'62','published',?,?,?)", (first['page_id'],first['material_id'],self.now.isoformat(),self.now.isoformat()))
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,unknown_outcome,created_at_utc) VALUES(0,1,1,?,'62','failed',1,?)", (second['page_id'],self.now.isoformat()))
        self.assertEqual(self.store.claim_next("worker")['id'],third['id'])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT error_code FROM fb_auto_task WHERE id=?", (first['id'],)).fetchone()[0], 'fb_auto_material_cooldown_at_publish')
            self.assertEqual(c.execute("SELECT error_code FROM fb_auto_task WHERE id=?", (second['id'],)).fetchone()[0], 'fb_auto_page_unknown_at_publish')

    def test_ordinary_tasks_keep_cooldown_and_do_not_get_manual_policy(self):
        self.reserve()
        self.ready()
        with self.store.connect() as c:
            config = self.template["config_json"]
            rid = c.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,'ordinary','auto','queued',?,?)", (config, self.now.isoformat())).lastrowid
            tid = c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,content_id,created_at_utc,planned_publish_at_utc,prepared_at_utc,media_url,prepared_media_url,source_media_url) VALUES(?,1,1,'99999','62','ready','drama-en',?,?,?,'https://cdn.example/p.mp4','https://cdn.example/p.mp4','https://cdn.example/s.mp4')", (rid, self.now.isoformat(), self.now.isoformat(), self.now.isoformat())).lastrowid
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,content_id,created_at_utc,completed_at_utc) VALUES(0,1,1,'99999','62','published','drama-en',?,?)", (self.now.isoformat(), self.now.isoformat()))
            self.assertIsNone(task_policy(c, dict(c.execute("SELECT * FROM fb_auto_task WHERE id=?", (tid,)).fetchone())))
        self.store.claim_next("worker")
        with self.store.connect() as c:
            row = c.execute("SELECT status,skip_reason FROM fb_auto_task WHERE id=?", (tid,)).fetchone()
            self.assertEqual(row[:], ("skipped", "fb_auto_drama_cooldown_at_publish"))

    def test_https_normalization_preserves_object_and_rejects_other_sources(self):
        source = "http://" + COS_SOURCE_HOST + "/custom/source/clip.mp4?version=1"
        self.assertEqual(prepare_source_url(source), source.replace("http:", "https:", 1))
        self.assertEqual(prepare_source_url(prepare_source_url(source)), prepare_source_url(source))
        for bad in ["https://example.com/clip.mp4", "https://user@" + COS_SOURCE_HOST + "/clip.mp4", "http://" + COS_SOURCE_HOST + ":80/clip.mp4", source + "#fragment"]:
            with self.assertRaises(BatchError):
                prepare_source_url(bad)

    def test_manual_preparation_uses_https_without_changing_frozen_source_or_job(self):
        from features.fb_auto_posts.gpu import PrepareExecutor
        for material in self.materials.values():
            material["media_url"] = "http://" + COS_SOURCE_HOST + "/" + material["material_id"] + ".mp4"
        self.reserve()
        with self.store.connect() as c:
            original = dict(c.execute("SELECT * FROM fb_auto_task ORDER BY id LIMIT 1").fetchone())
        gpu = Mock()
        gpu.prepare.return_value = {"profile": "tt-post-random-overlay-h264-720x1280-v3", "media_url": "https://cdn.example/p.mp4", "sha256": "a" * 64, "size_bytes": 100, "duration_seconds": "715"}
        PrepareExecutor(self.store, gpu, live_enabled=True).prepare_next("prepare-worker")
        self.assertEqual(gpu.prepare.call_args.kwargs["source_url"], prepare_source_url(original["source_media_url"]))
        self.assertEqual(gpu.prepare.call_args.kwargs["job_id"], original["gpu_job_id"])
        with self.store.connect() as c:
            current = dict(c.execute("SELECT * FROM fb_auto_task WHERE id=?", (original["id"],)).fetchone())
            self.assertEqual(current["source_media_url"], original["source_media_url"])
            self.assertTrue(task_policy(c, current))

    def recovery_fixture(self):
        from scripts.fb_auto_post_manual_material_batch import recover_unattempted_preparation
        for material in self.materials.values():
            material["media_url"] = "http://" + COS_SOURCE_HOST + "/" + material["material_id"] + ".mp4"
            material["media_info"] = {"size_bytes": 100, "etag": '"same-object"'}
        self.reserve()
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_task SET status='failed',attempt_count=1,error_code='fb_auto_prepared_response_invalid',completed_at_utc=?", (self.now.isoformat(),))
        response = Mock(status_code=200, headers={"Content-Type": "video/mp4", "Content-Length": "100", "ETag": '"same-object"'})
        return recover_unattempted_preparation, response, {"FB_AUTO_POST_DB_PATH": self.store.path}

    def test_recovery_is_in_place_and_idempotent(self):
        recover, response, env = self.recovery_fixture()
        with self.store.connect() as c:
            before = [tuple(r) for r in c.execute("SELECT id,page_id,material_id,gpu_job_id FROM fb_auto_task ORDER BY id")]
        with patch("requests.head", return_value=response):
            self.assertEqual(recover(env, "test-batch", str(Path(self.tmp.name) / "audit.json"))["recovered"], 107)
            self.assertEqual(recover(env, "test-batch", str(Path(self.tmp.name) / "repeat.json"))["recovered"], 0)
        with self.store.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute("SELECT id,page_id,material_id,gpu_job_id FROM fb_auto_task ORDER BY id")], before)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='planned'").fetchone()[0], 107)

    def test_recovery_refuses_any_publication_or_unknown_without_partial_reset(self):
        recover, response, env = self.recovery_fixture()
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_task SET unknown_outcome=1 WHERE id=(SELECT MAX(id) FROM fb_auto_task)")
        with patch("requests.head", return_value=response), self.assertRaises(BatchError):
            recover(env, "test-batch", str(Path(self.tmp.name) / "blocked.json"))
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='failed'").fetchone()[0], 107)
            c.execute("UPDATE fb_auto_task SET unknown_outcome=0,attempt_count=2 WHERE id=(SELECT MAX(id) FROM fb_auto_task)")
        with patch("requests.head", return_value=response), self.assertRaises(BatchError):
            recover(env, "test-batch", str(Path(self.tmp.name) / "blocked-attempt.json"))


if __name__ == "__main__":
    unittest.main()

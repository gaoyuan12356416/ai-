import random
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.publisher import AutoPostExecutor, GraphResult
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageCredential, PageTarget
from scripts.test_fb_auto_validation import payload


class Pages:
    def legacy_conflicts(self, _ids): return []
    def list_pages(self, *_args, **_kwargs):
        return [PageTarget("6", ("6", "18"), "10001", "248", "UTC", "english", 2), PageTarget("6", ("6",), "10002", "248", "UTC", "english", 0)]


class Materials:
    def __init__(self): self.calls = 0
    def candidate_snapshot(self, _config):
        self.calls += 1
        return CandidateSnapshot((MaterialCandidate("501", "drama1", "https://cdn.example/a.mp4", "M", "D", "english", Decimal("30"), Decimal("5"), Decimal("50"), Decimal("10"), Decimal("60"), "1"),), (11,), ("2026-08-16",))
    def choose_from(self, candidates, excluded): return next((item for item in candidates if item.material_id not in set(excluded)), None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.now = datetime(2026, 8, 17, 2, 30, tzinfo=timezone.utc); self.store = FBAutoPostStore(Path(self.tmp.name) / "fb.sqlite3", now_fn=lambda: self.now, rng=random.Random(1)); self.actor = ActorScope("u", "测试", False, "248")
        self.template = self.store.create_template(payload(), self.actor, {"app_id": "1479", "product": "Dramawave"})
    def tearDown(self): self.tmp.cleanup()
    def enable(self, template=None):
        template = template or self.store.get_template(self.template["id"], self.actor)
        if template["status"] != "enabled":
            template = self.store.set_template_status(template["id"], True, self.actor, template["version"])
        return template
    def ready_next(self):
        self.enable()
        task = self.store.claim_prepare_next("p", 3600)
        self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        return self.store.claim_next("w", 900)
    def stage_due(self, slot_key, *, trigger_type="auto", planned_publish_at_utc=None, template=None, lease_seconds=3600):
        template = template or self.store.get_template(self.template["id"], self.actor)
        planned = planned_publish_at_utc or self.now.isoformat(timespec="seconds")
        now = self.now.isoformat(timespec="seconds")
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,trigger_type,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'pending',?,?,?)",
                (template["id"], template["version"], slot_key, planned, trigger_type, now, now),
            )
        claimed = self.store.claim_due_slot("planner", lease_seconds)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["slot_key"], slot_key)
        return claimed

    def test_run_loads_material_snapshot_once_and_preserves_skip_reason(self):
        materials = Materials(); result = self.store.create_run(self.template["id"], "manual:1", "manual", self.actor, Pages(), materials)
        self.assertEqual(materials.calls, 1); self.assertEqual(result["summary"]["total_pages"], 2); self.assertEqual(result["summary"]["missing_token_pages"], 1); self.assertEqual(result["summary"]["overlap_pages"], 1)
        detail = self.store.get_run(result["run_id"], self.actor)["run"]
        self.assertEqual({task["skip_reason"] for task in detail["tasks"]}, {"", "fb_page_missing_eligible_token"})

    def test_description_and_short_link_are_frozen_with_task_identity(self):
        raw=payload(); raw["name"]="Macros"; raw["message_template"]="{{drama_name}}\n{{desc}}\n{{url}}"
        template=self.store.create_template(raw,self.actor,{"app_id":"1479","product":"Dramawave"})
        class OnePage(Pages):
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","en",1,"Free Reels")]
        material=MaterialCandidate("501","AcWE9aQz8q","https://cdn.example/a.mp4","Opening","My Drama","en",Decimal("30"),Decimal("5"),Decimal("50"),Decimal("10"),Decimal("60"),"1","A short drama description","hook")
        class RichMaterials(Materials):
            def candidate_snapshot(self,_config): return CandidateSnapshot((material,),(11,),("2026-08-16",))
        result=self.store.create_run(template["id"],"manual:macros","manual",self.actor,OnePage(),RichMaterials())
        with self.store.connect() as conn:
            task=dict(conn.execute("SELECT id,message_text,short_url,long_url FROM fb_auto_task WHERE run_id=?",(result["run_id"],)).fetchone())
        self.assertEqual(task["short_url"],f"https://gy.g2flow.com/s2l/fb/{task['id']}.html")
        self.assertEqual(task["message_text"],f"My Drama\nA short drama description\n{task['short_url']}")
        parsed=urllib.parse.urlsplit(task["long_url"]); values=dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(parsed.path,"/ads/0/2049/view"); self.assertEqual(values["af_channel"],"AIpost")
        self.assertEqual(values["af_adset"],"Free Reels"); self.assertEqual(values["af_ad_id"],"501")
        self.assertEqual(values["af_c_id"],str(task["id"])); self.assertTrue(values["c"].endswith("*hook*"+str(task["id"])))
        detail=self.store.get_run(result["run_id"],self.actor)["run"]["tasks"][0]
        self.assertEqual((detail["short_url"],detail["long_url"]),(task["short_url"],task["long_url"]))

    def test_macro_values_are_not_recursively_expanded(self):
        material=MaterialCandidate("501","d1","https://cdn.example/a.mp4","M {{desc}}","D","en",Decimal("30"),Decimal("1"),Decimal("1"),Decimal("1"),Decimal("1"),"1","Description {{url}}","hook")
        rendered=self.store._message({"message_template":"{{material_name}} | {{desc}} | {{url}}"},material,"https://gy.g2flow.com/s2l/fb/7.html")
        self.assertEqual(rendered,"M {{desc}} | Description {{url}} | https://gy.g2flow.com/s2l/fb/7.html")

    def test_previous_backlog_blocks_next_slot(self):
        self.store.set_template_status(self.template["id"], True, self.actor, self.template["version"])
        self.stage_due("auto:1")
        self.store.create_run(self.template["id"], "auto:1", "auto", self.actor, Pages(), Materials(), expected_template_version=self.template["version"])
        self.stage_due("auto:2")
        with self.assertRaisesRegex(StoreError, "上一个时隙"): self.store.create_run(self.template["id"], "auto:2", "auto", self.actor, Pages(), Materials(), expected_template_version=self.template["version"])

    def test_random_plan_is_persisted_non_hour_and_spaced(self):
        config = payload(); config["schedule"] = {"mode": "random", "daily_count": 3, "start": "08:00", "end": "12:00"}
        config = __import__("features.fb_auto_posts.validation", fromlist=["normalize_template_payload"]).normalize_template_payload(config)
        one = self.store.schedule_times(99, 1, config, "2026-08-18"); two = self.store.schedule_times(99, 1, config, "2026-08-18")
        self.assertEqual(one, two); minutes = [int(x[:2])*60+int(x[3:]) for x in one]; self.assertTrue(all(x % 60 for x in minutes)); self.assertTrue(all(b-a >= 60 for a,b in zip(minutes, minutes[1:])))

    def test_full_day_random_plan_guarantees_24_safe_slots(self):
        config = payload(); config["schedule"] = {"mode": "random", "daily_count": 24, "start": "00:00", "end": "23:59"}
        config = __import__("features.fb_auto_posts.validation", fromlist=["normalize_template_payload"]).normalize_template_payload(config)
        times = self.store.schedule_times(100, 1, config, "2026-08-18")
        minutes = [int(value[:2])*60+int(value[3:]) for value in times]
        self.assertEqual(len(minutes), 24); self.assertTrue(all(value % 60 for value in minutes)); self.assertTrue(all(b-a >= 60 for a,b in zip(minutes, minutes[1:])))

    def test_run_rechecks_page_overlap_after_enabled_group_membership_drift(self):
        other_payload = payload(); other_payload["name"] = "Other"; other_payload["group_ids"] = ["18"]
        other = self.store.create_template(other_payload, self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(self.template["id"], True, self.actor, self.template["version"])
        self.store.set_template_status(other["id"], True, self.actor, other["version"])
        self.stage_due("auto:drift")
        with self.assertRaisesRegex(StoreError, "当前Page组成员") as caught:
            self.store.create_run(self.template["id"], "auto:drift", "auto", self.actor, Pages(), Materials(), expected_template_version=self.template["version"])
        self.assertEqual(caught.exception.code, "fb_auto_page_template_conflict")

    def test_unknown_page_cannot_be_queued_again(self):
        first = self.store.create_run(self.template["id"], "manual:1", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        self.store.complete_task(task["id"], {"status": "unknown", "error_code": "fb_graph_network_outcome_unknown", "error_message": "待确认"})
        second = self.store.create_run(self.template["id"], "manual:2", "manual", self.actor, Pages(), Materials())
        self.assertEqual(second["summary"]["queued_tasks"], 0)
        reasons = {item["skip_reason"] for item in self.store.get_run(second["run_id"], self.actor)["run"]["tasks"]}
        self.assertIn("fb_auto_page_unknown_block", reasons)

    def test_enabled_template_must_be_disabled_before_edit(self):
        self.store.set_template_status(
            self.template["id"], True, self.actor, self.template["version"]
        )
        with self.assertRaisesRegex(StoreError, "先停用"):
            self.store.update_template(
                self.template["id"],
                payload(),
                self.actor,
                self.template["version"],
                {"app_id": "1479", "product": "Dramawave"},
            )

    def test_running_publish_blocks_disable_and_update_until_terminal(self):
        self.enable()
        self.store.create_run(self.template["id"], "manual:running-boundary", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        with self.assertRaises(StoreError) as disabled:
            self.store.set_template_status(self.template["id"], False, self.actor, 1)
        self.assertEqual((disabled.exception.code, disabled.exception.status), ("fb_auto_template_running_change_denied", 409))
        changed = payload(); changed["name"] = "version two"
        with self.assertRaises(StoreError) as updated:
            self.store.update_template(self.template["id"], changed, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        self.assertEqual((updated.exception.code, updated.exception.status), ("fb_auto_template_running_change_denied", 409))
        self.store.complete_task(task["id"], {"status":"failed", "error_code":"fb_graph_definite_failure", "error_message":"Meta明确拒绝"})
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        result = self.store.update_template(self.template["id"], changed, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        self.assertEqual(result["version"], 2)

    def test_disabled_template_rejects_manual_due_without_creating_queue(self):
        with self.assertRaises(StoreError) as caught:
            self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-disabled-run-0001")
        self.assertEqual((caught.exception.code, caught.exception.status), ("fb_auto_manual_template_disabled", 409))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_due_slot").fetchone()[0], 0)

    def test_disable_cancels_pending_manual_due_and_planned_ready_tasks_permanently(self):
        class TwoEligiblePages(Pages):
            def list_pages(self, *_args, **_kwargs):
                return [
                    PageTarget("6", ("6",), "10001", "248", "UTC", "english", 1),
                    PageTarget("6", ("6",), "10002", "248", "UTC", "english", 1),
                ]
        self.enable()
        due = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-cancel-queued-0001")
        created = self.store.create_run(self.template["id"], "manual:cancel-planned-ready", "manual", self.actor, TwoEligiblePages(), Materials(), planned_publish_at_utc=(self.now + timedelta(hours=1)).isoformat())
        preparing = self.store.claim_prepare_next("prepare")
        self.store.complete_prepare(preparing["id"], {"media_url":"https://cdn.example/manual-ready.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        with self.store.connect() as conn:
            due_row = conn.execute("SELECT status,error_code FROM fb_auto_due_slot WHERE id=?", (due["due_slot_id"],)).fetchone()
            tasks = conn.execute("SELECT status,skip_reason FROM fb_auto_task WHERE run_id=? ORDER BY id", (created["run_id"],)).fetchall()
            run_status = conn.execute("SELECT status FROM fb_auto_run WHERE id=?", (created["run_id"],)).fetchone()[0]
        self.assertEqual(tuple(due_row), ("failed", "fb_auto_manual_template_disabled"))
        self.assertTrue(all(tuple(row) == ("skipped", "fb_auto_manual_template_disabled") for row in tasks))
        self.assertEqual(run_status, "completed")
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        self.assertIsNone(self.store.claim_due_slot("restart-planner"))
        self.assertIsNone(self.store.claim_prepare_next("restart-prepare"))
        self.assertIsNone(self.store.claim_next("restart-publish"))

    def test_manual_preparing_disable_reenable_late_callback_stays_skipped(self):
        self.enable()
        created = self.store.create_run(self.template["id"], "manual:cancel-preparing", "manual", self.actor, Pages(), Materials())
        task = self.store.claim_prepare_next("prepare", 60)
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        result = self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/late-manual.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.assertEqual((result["status"], result["skip_reason"]), ("skipped", "fb_auto_manual_template_disabled"))
        self.now += timedelta(minutes=2)
        self.assertIsNone(self.store.claim_prepare_next("expired-retry"))
        self.assertIsNone(self.store.claim_next("must-not-publish"))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,skip_reason FROM fb_auto_task WHERE run_id=? AND id=?", (created["run_id"], task["id"])).fetchone()
        self.assertEqual(tuple(row), ("skipped", "fb_auto_manual_template_disabled"))

    def test_manual_due_disable_reenable_cannot_create_or_restore_run(self):
        self.enable()
        due = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-cancel-planning-0001")
        claimed = self.store.claim_due_slot("planner", 60)
        self.assertEqual(claimed["id"], due["due_slot_id"])
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(self.template["id"], claimed["slot_key"], "manual", self.actor, Pages(), Materials(), expected_template_version=1)
        self.assertEqual(caught.exception.code, "fb_auto_due_slot_template_changed")
        self.store.defer_due_slot(due["due_slot_id"], "fb_auto_transient")
        self.store.complete_due_slot(due["due_slot_id"], run_id=999)
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,error_code,run_id FROM fb_auto_due_slot WHERE id=?", (due["due_slot_id"],)).fetchone()
            run_count = conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0]
        self.assertEqual(tuple(row), ("failed", "fb_auto_manual_template_disabled", None))
        self.assertEqual(run_count, 0)

    def test_legacy_ready_without_prepared_at_is_never_claimed(self):
        self.enable()
        created = self.store.create_run(self.template["id"], "manual:legacy-ready", "manual", self.actor, Pages(), Materials())
        with self.store.connect() as conn:
            task_id = int(conn.execute("SELECT id FROM fb_auto_task WHERE run_id=? AND status='planned'", (created["run_id"],)).fetchone()[0])
            conn.execute("UPDATE fb_auto_task SET status='ready',media_url='https://cdn.example/legacy-prepared.mp4',prepared_media_url='https://cdn.example/legacy-prepared.mp4',prepared_at_utc='' WHERE id=?", (task_id,))
        self.assertIsNone(self.store.claim_next("legacy-must-not-publish"))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,skip_reason FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(tuple(row), ("skipped", "fb_auto_prepared_contract_invalid"))

    def test_runtime_invalid_prepared_media_contract_is_terminal(self):
        self.enable()
        created = self.store.create_run(self.template["id"], "manual:invalid-ready-contract", "manual", self.actor, Pages(), Materials())
        with self.store.connect() as conn:
            task_id = int(conn.execute("SELECT id FROM fb_auto_task WHERE run_id=? AND status='planned'", (created["run_id"],)).fetchone()[0])
            conn.execute("UPDATE fb_auto_task SET status='ready',prepared_at_utc=?,media_url='https://cdn.example/wrong.mp4',prepared_media_url='https://cdn.example/prepared.mp4' WHERE id=?", (self.now.isoformat(timespec="seconds"), task_id))
        self.assertIsNone(self.store.claim_next("invalid-contract"))
        with self.store.connect() as conn:
            task = conn.execute("SELECT status,error_code FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()
            run_status = conn.execute("SELECT status FROM fb_auto_run WHERE id=?", (created["run_id"],)).fetchone()[0]
        self.assertEqual(tuple(task), ("skipped", "fb_auto_prepared_contract_invalid"))
        self.assertEqual(run_status, "completed")

    def test_storage_upgrade_skips_legacy_ready_without_prepared_timestamp(self):
        created = self.store.create_run(self.template["id"], "manual:old-schema-ready", "manual", self.actor, Pages(), Materials())
        with self.store.connect() as conn:
            task_id = int(conn.execute("SELECT id FROM fb_auto_task WHERE run_id=? AND status='planned'", (created["run_id"],)).fetchone()[0])
            conn.execute("UPDATE fb_auto_task SET status='ready',media_url='https://cdn.example/prepared.mp4',prepared_media_url='https://cdn.example/prepared.mp4' WHERE id=?", (task_id,))
            current_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fb_auto_task'").fetchone()[0]
            legacy_sql = current_sql.replace("CREATE TABLE fb_auto_task", "CREATE TABLE fb_auto_task_legacy", 1).replace(" prepared_at_utc TEXT NOT NULL DEFAULT '',", "")
            self.assertNotIn("prepared_at_utc", legacy_sql)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(fb_auto_task)") if row[1] != "prepared_at_utc"]
            projection = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
            conn.execute(legacy_sql)
            conn.execute(f"INSERT INTO fb_auto_task_legacy({projection}) SELECT {projection} FROM fb_auto_task")
            conn.execute("DROP TABLE fb_auto_task")
            conn.execute("ALTER TABLE fb_auto_task_legacy RENAME TO fb_auto_task")
        upgraded = FBAutoPostStore(self.store.path, now_fn=lambda: self.now)
        with upgraded.connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(fb_auto_task)")}
            task = conn.execute("SELECT status,error_code,prepared_at_utc FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()
            run_status = conn.execute("SELECT status FROM fb_auto_run WHERE id=?", (created["run_id"],)).fetchone()[0]
        self.assertIn("prepared_at_utc", columns)
        self.assertEqual(tuple(task), ("skipped", "fb_auto_legacy_ready_unverified", ""))
        self.assertEqual(run_status, "completed")

    def test_disabled_same_version_pauses_prepare_and_publish_then_resumes(self):
        self.enable()
        self.stage_due("auto:pause-resume")
        created = self.store.create_run(self.template["id"], "auto:pause-resume", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        self.assertIsNone(self.store.claim_prepare_next("disabled-prepare"))
        self.enable()
        task = self.store.claim_prepare_next("prepare")
        completed = self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.assertEqual(completed["status"], "ready")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT prepared_at_utc FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()[0], self.now.isoformat(timespec="seconds"))
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        self.assertIsNone(self.store.claim_next("disabled-publish"))
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        self.assertEqual(self.store.claim_next("publish")["run_id"], created["run_id"])

    def test_update_skips_old_planned_and_ready_tasks(self):
        class OnePage(Pages):
            def list_pages(self, *_args, **_kwargs):
                return [PageTarget("6", ("6",), "10001", "248", "UTC", "english", 1)]
        self.enable()
        self.stage_due("auto:old-planned", planned_publish_at_utc=(self.now + timedelta(hours=1)).isoformat())
        first = self.store.create_run(self.template["id"], "auto:old-planned", "auto", self.actor, OnePage(), Materials(), planned_publish_at_utc=(self.now + timedelta(hours=1)).isoformat(), expected_template_version=1)
        class OtherPage(Pages):
            def list_pages(self, *_args, **_kwargs):
                return [PageTarget("6", ("6",), "20001", "248", "UTC", "english", 1)]
        self.stage_due("auto:old-ready", planned_publish_at_utc=(self.now + timedelta(hours=2)).isoformat())
        second = self.store.create_run(self.template["id"], "auto:old-ready", "auto", self.actor, OtherPage(), Materials(), planned_publish_at_utc=(self.now + timedelta(hours=2)).isoformat(), expected_template_version=1)
        task = self.store.claim_prepare_next("prepare-ready")
        self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/old-ready.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        updated_payload = payload(); updated_payload["name"] = "version two"
        updated = self.store.update_template(self.template["id"], updated_payload, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        self.assertEqual(updated["version"], 2)
        with self.store.connect() as conn:
            rows = conn.execute("SELECT run_id,status,skip_reason FROM fb_auto_task WHERE run_id IN (?,?) ORDER BY run_id", (first["run_id"], second["run_id"])).fetchall()
        self.assertTrue(all(row["status"] == "skipped" for row in rows))
        self.assertTrue(all(row["skip_reason"] == "fb_auto_template_version_changed" for row in rows))
        self.store.set_template_status(self.template["id"], True, self.actor, 2)
        self.assertIsNone(self.store.claim_next("must-not-publish-old-version"))

    def test_prepare_completion_after_version_drift_is_safely_skipped(self):
        self.enable()
        self.stage_due("auto:prepare-version-drift")
        self.store.create_run(self.template["id"], "auto:prepare-version-drift", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        task = self.store.claim_prepare_next("prepare")
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        updated_payload = payload(); updated_payload["name"] = "version two"
        self.store.update_template(self.template["id"], updated_payload, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        result = self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/stale.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.assertEqual((result["status"], result["skip_reason"]), ("skipped", "fb_auto_template_version_changed"))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,prepared_at_utc FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(tuple(row), ("skipped", self.now.isoformat(timespec="seconds")))

    def test_prepare_retry_after_version_drift_is_safely_skipped(self):
        self.enable()
        self.stage_due("auto:prepare-retry-version-drift")
        self.store.create_run(self.template["id"], "auto:prepare-retry-version-drift", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        task = self.store.claim_prepare_next("prepare")
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        updated_payload = payload(); updated_payload["name"] = "version two"
        self.store.update_template(self.template["id"], updated_payload, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        result = self.store.defer_prepare(task["id"], "fb_gpu_transient", "稍后重试")
        self.assertEqual((result["status"], result["skip_reason"]), ("skipped", "fb_auto_template_version_changed"))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,next_prepare_at_utc FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(tuple(row), ("skipped", ""))

    def test_publish_claim_keeps_publish_at_gate(self):
        publish_at = self.now + timedelta(hours=1)
        self.store.create_run(self.template["id"], "manual:future-publish-gate", "manual", self.actor, Pages(), Materials(), planned_publish_at_utc=publish_at.isoformat())
        self.enable()
        task = self.store.claim_prepare_next("prepare")
        self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/future.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.assertIsNone(self.store.claim_next("too-early"))
        self.now = publish_at
        self.assertEqual(self.store.claim_next("on-time")["id"], task["id"])

    def test_auto_prepare_completion_and_ready_claim_expire_after_grace(self):
        self.enable()
        self.stage_due("auto:late-complete")
        first = self.store.create_run(self.template["id"], "auto:late-complete", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        preparing = self.store.claim_prepare_next("prepare")
        self.now += timedelta(seconds=601)
        completed = self.store.complete_prepare(preparing["id"], {"media_url":"https://cdn.example/late.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.assertEqual((completed["status"], completed["skip_reason"]), ("skipped", "fb_auto_task_too_late"))
        self.stage_due("auto:late-ready")
        second = self.store.create_run(self.template["id"], "auto:late-ready", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        ready = self.store.claim_prepare_next("prepare-ready")
        self.store.complete_prepare(ready["id"], {"media_url":"https://cdn.example/ready.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.now += timedelta(seconds=601)
        self.assertIsNone(self.store.claim_next("late-publish"))
        with self.store.connect() as conn:
            status = conn.execute("SELECT status FROM fb_auto_task WHERE run_id=?", (second["run_id"],)).fetchone()[0]
        self.assertEqual(status, "skipped")

    def test_expired_preparing_auto_work_is_terminal_instead_of_stuck(self):
        self.enable()
        due = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-expired-lease-0001")
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_due_slot SET trigger_type='auto',planned_publish_at_utc=? WHERE id=?", (self.now.isoformat(), due["due_slot_id"]))
        claimed_due = self.store.claim_due_slot("planner", lease_seconds=60)
        self.assertEqual(claimed_due["id"], due["due_slot_id"])
        self.now += timedelta(seconds=601)
        self.assertIsNone(self.store.claim_due_slot("planner-2", lease_seconds=60))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM fb_auto_due_slot WHERE id=?", (due["due_slot_id"],)).fetchone()[0], "missed")

        self.stage_due("auto:expired-prepare")
        run = self.store.create_run(self.template["id"], "auto:expired-prepare", "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        preparing = self.store.claim_prepare_next("prepare", lease_seconds=60)
        self.now += timedelta(seconds=601)
        self.assertIsNone(self.store.claim_prepare_next("prepare-2", lease_seconds=60))
        with self.store.connect() as conn:
            task = conn.execute("SELECT status,skip_reason FROM fb_auto_task WHERE id=?", (preparing["id"],)).fetchone()
            run_status = conn.execute("SELECT status FROM fb_auto_run WHERE id=?", (run["run_id"],)).fetchone()[0]
        self.assertEqual(tuple(task), ("skipped", "fb_auto_task_too_late"))
        self.assertEqual(run_status, "completed")

    def test_reconcile_terminal_unknown_preserves_graph_id(self):
        created = self.store.create_run(self.template["id"], "manual:reconcile", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        self.store.complete_task(task["id"], {"status": "submitted", "graph_post_id": "vid_9"})
        self.assertIsNone(self.store.claim_submitted("too-soon", 300))
        self.now += timedelta(minutes=5)
        claimed = self.store.claim_submitted("r", 300)
        result = self.store.reconcile_task(claimed["id"], "unknown", error_code="fb_graph_reconcile_all_credentials_rejected", error_message="需人工确认")
        self.assertEqual(result["graph_object_id"], "vid_9"); self.assertTrue(result["unknown_outcome"])
        detail = self.store.get_run(created["run_id"], self.actor)["run"]
        self.assertEqual(detail["tasks"][0]["status"], "unknown"); self.assertEqual(detail["tasks"][0]["graph_object_id"], "vid_9")

    def test_reconcile_preserves_publish_definite_attempt_count(self):
        self.store.create_run(self.template["id"], "manual:attempts", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        self.store.complete_task(task["id"], {"status":"submitted","graph_post_id":"vid_9","definite_attempts":2})
        self.now += timedelta(minutes=5)
        claimed = self.store.claim_submitted("r", 300); self.store.reconcile_task(claimed["id"], "published")
        with self.store.connect() as conn:
            attempts = conn.execute("SELECT definite_attempts FROM fb_auto_publish_ledger WHERE task_id=?",(task["id"],)).fetchone()[0]
        self.assertEqual(attempts,2)

    def test_transient_reconcile_has_persistent_backoff(self):
        self.store.create_run(self.template["id"], "manual:reconcile-backoff", "manual", self.actor, Pages(), Materials())
        task = self.ready_next(); self.store.complete_task(task["id"], {"status":"submitted","graph_post_id":"vid_9"})
        self.now += timedelta(minutes=5)
        claimed = self.store.claim_submitted("r1",300); self.store.reconcile_task(claimed["id"],"submitted",error_code="fb_graph_reconcile_transient")
        self.assertIsNone(self.store.claim_submitted("r2",300))
        self.now += timedelta(minutes=5)
        self.assertEqual(self.store.claim_submitted("r2",300)["id"],task["id"])

    def test_manual_due_slot_is_fast_idempotent_and_versioned(self):
        self.enable()
        first = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-request-0001")
        second = self.store.enqueue_manual_due_slot(self.template["id"], self.actor, expected_template_version=1, operation_id="operator-request-0001")
        self.assertEqual(first["due_slot_id"], second["due_slot_id"])
        self.assertFalse(first["idempotent"]); self.assertTrue(second["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_due_slot").fetchone()[0], 1)

    def test_future_slots_for_same_page_can_prepare_but_submitted_blocks_claim(self):
        class OnePage(Pages):
            def list_pages(self, *_args, **_kwargs):
                return [PageTarget("6", ("6",), "10001", "248", "UTC", "en", 1)]
        first_at = self.now + timedelta(hours=1)
        second_at = self.now + timedelta(hours=2)
        first_material=Materials().candidate_snapshot({}).candidates[0]
        second_material=MaterialCandidate("502","drama1","https://cdn.example/b.mp4","M2","D","en",Decimal("30"),Decimal("4"),Decimal("40"),Decimal("9"),Decimal("50"),"1")
        class TwoMaterials(Materials):
            def candidate_snapshot(self,_config): return CandidateSnapshot((first_material,second_material),(11,),("2026-08-16",))
        first = self.store.create_run(self.template["id"], "manual:future:1", "manual", self.actor, OnePage(), TwoMaterials(), planned_publish_at_utc=first_at.isoformat())
        second = self.store.create_run(self.template["id"], "manual:future:2", "manual", self.actor, OnePage(), TwoMaterials(), planned_publish_at_utc=second_at.isoformat())
        self.enable()
        with self.store.connect() as conn:
            chosen=[row[0] for row in conn.execute("SELECT material_id FROM fb_auto_task WHERE run_id IN (?,?) ORDER BY run_id",(first["run_id"],second["run_id"]))]
        self.assertEqual(chosen,["501","502"])
        for worker in ("prepare-1", "prepare-2"):
            task = self.store.claim_prepare_next(worker, 3600)
            self.store.complete_prepare(task["id"], {"media_url":f"https://cdn.example/{worker}.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.now = first_at
        task1 = self.store.claim_next("publish-1", 900)
        self.assertEqual(task1["run_id"], first["run_id"])
        self.store.complete_task(task1["id"], {"status":"submitted", "graph_post_id":"video_1"})
        self.now = second_at
        self.assertIsNone(self.store.claim_next("publish-2", 900))
        self.now += timedelta(minutes=5)
        submitted = self.store.claim_submitted("reconcile", 300)
        self.store.reconcile_task(submitted["id"], "published")
        task2 = self.store.claim_next("publish-2", 900)
        self.assertEqual(task2["run_id"], second["run_id"])

    def test_five_future_slots_freeze_full_page_day_with_unique_material_per_page(self):
        class ThirteenPages(Pages):
            def list_pages(self, *_args, **_kwargs):
                return [
                    PageTarget("6", ("6",), str(10000 + index), "248", "UTC", "en", 1 if index < 8 else 0)
                    for index in range(13)
                ]
        candidates = tuple(
            MaterialCandidate(str(600 + index), "drama1", f"https://cdn.example/{index}.mp4", f"M{index}", "D", "en", Decimal("30"), Decimal("4"), Decimal("40"), Decimal("9"), Decimal("50"), "1")
            for index in range(6)
        )
        class DayMaterials(Materials):
            def candidate_snapshot(self, _config): return CandidateSnapshot(candidates, (11,), ("2026-08-16",))
        self.enable()
        run_ids = []
        for offset in range(1, 6):
            slot_key = f"auto:v1:2026-08-18:0{offset}:30"
            publish_at = (self.now + timedelta(hours=offset)).isoformat()
            self.stage_due(slot_key, planned_publish_at_utc=publish_at)
            created = self.store.create_run(
                self.template["id"], slot_key, "auto", self.actor,
                ThirteenPages(), DayMaterials(), planned_publish_at_utc=publish_at,
                expected_template_version=1,
            )
            run_ids.append(created["run_id"])
        placeholders = ",".join("?" for _ in run_ids)
        with self.store.connect() as conn:
            counts = dict(conn.execute(f"SELECT status,COUNT(*) FROM fb_auto_task WHERE run_id IN ({placeholders}) GROUP BY status", run_ids))
            per_page = conn.execute(
                f"SELECT page_id,COUNT(*) AS tasks,COUNT(DISTINCT material_id) AS materials FROM fb_auto_task WHERE run_id IN ({placeholders}) AND status='planned' GROUP BY page_id ORDER BY page_id",
                run_ids,
            ).fetchall()
        self.assertEqual(counts, {"planned": 40, "skipped": 25})
        self.assertEqual(len(per_page), 8)
        self.assertTrue(all((row["tasks"], row["materials"]) == (5, 5) for row in per_page))

    def test_concurrent_future_runs_atomically_reserve_different_materials(self):
        import threading
        raw=payload(); raw["name"]="zero-cooldown"; raw["cooldown_days"]=0
        template=self.store.create_template(raw,self.actor,{"app_id":"1479","product":"Dramawave"})
        class OnePage(Pages):
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","en",1)]
        first=Materials().candidate_snapshot({}).candidates[0]
        second=MaterialCandidate("502","drama1","https://cdn.example/b.mp4","M2","D","en",Decimal("30"),Decimal("4"),Decimal("40"),Decimal("9"),Decimal("50"),"1")
        barrier=threading.Barrier(2)
        class ConcurrentMaterials(Materials):
            def candidate_snapshot(self,_config):
                barrier.wait(2)
                return CandidateSnapshot((first,second),(11,),("2026-08-16",))
        other_store=FBAutoPostStore(self.store.path,now_fn=lambda:self.now,rng=random.Random(2))
        results=[]; errors=[]
        def create(store,key,hours):
            try: results.append(store.create_run(template["id"],key,"manual",self.actor,OnePage(),ConcurrentMaterials(),planned_publish_at_utc=(self.now+timedelta(hours=hours)).isoformat()))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=create,args=(self.store,"manual:concurrent:1",1)),threading.Thread(target=create,args=(other_store,"manual:concurrent:2",2))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads)); self.assertFalse(errors); self.assertEqual(len(results),2)
        with self.store.connect() as conn: chosen=[row[0] for row in conn.execute("SELECT material_id FROM fb_auto_task WHERE run_id IN (?,?) ORDER BY material_id",tuple(item["run_id"] for item in results))]
        self.assertEqual(chosen,["501","502"])

    def test_stale_running_preserves_definite_attempts(self):
        self.store.create_run(self.template["id"], "manual:stale", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        self.store.record_attempt(task["id"], 1, credential_id="1", fb_user_id="7", result_kind="definite_failure", error_code="expired")
        self.now += timedelta(minutes=16)
        self.assertEqual(self.store.mark_stale_running_unknown(), 1)
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,definite_attempts FROM fb_auto_publish_ledger WHERE task_id=?", (task["id"],)).fetchone()
        self.assertEqual(tuple(row), ("unknown", 1))

    def test_graph_acceptance_task_and_ledger_are_atomic(self):
        self.store.create_run(self.template["id"], "manual:atomic", "manual", self.actor, Pages(), Materials())
        task = self.ready_next()
        with patch.object(FBAutoPostStore, "_upsert_ledger", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.store.complete_submitted_with_attempt(task["id"], 1, credential_id="1", fb_user_id="7", graph_post_id="video_1")
        with self.store.connect() as conn:
            self.assertEqual(tuple(conn.execute("SELECT status,graph_post_id FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()), ("running", ""))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_publish_attempt WHERE task_id=?", (task["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_publish_ledger WHERE task_id=?", (task["id"],)).fetchone()[0], 0)

    def test_auto_run_final_transaction_rechecks_enabled_version(self):
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        self.stage_due("auto:disable-race")
        store = self.store
        class DisablingMaterials(Materials):
            def candidate_snapshot(self, config):
                store.set_template_status(self_template["id"], False, self_actor, 1)
                return super().candidate_snapshot(config)
        self_template, self_actor = self.template, self.actor
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(self.template["id"], "auto:disable-race", "auto", self.actor, Pages(), DisablingMaterials(), expected_template_version=1)
        self.assertEqual(caught.exception.code, "fb_auto_due_slot_template_disabled")
        with self.store.connect() as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_auto_run_final_transaction_rejects_due_missed_during_catalog_read(self):
        self.enable()
        due = self.stage_due("auto:late-during-plan", lease_seconds=60)
        store = self.store
        class ExpiringMaterials(Materials):
            def candidate_snapshot(self, config):
                self_outer.now += timedelta(seconds=601)
                self_outer.assertIsNone(store.claim_due_slot("late-sweeper", 60, max_late_seconds=600))
                return super().candidate_snapshot(config)
        self_outer = self
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(
                self.template["id"], due["slot_key"], "auto", self.actor, Pages(), ExpiringMaterials(),
                expected_template_version=1, expected_due_id=due["id"], expected_due_lease_owner="planner",
            )
        self.assertEqual(caught.exception.code, "fb_auto_due_slot_template_changed")
        with self.store.connect() as conn:
            self.assertEqual(tuple(conn.execute("SELECT status,error_code FROM fb_auto_due_slot WHERE id=?", (due["id"],)).fetchone()), ("missed", "fb_auto_due_slot_too_late"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_auto_run_final_transaction_rejects_reclaimed_due_owner(self):
        self.enable()
        due = self.stage_due("auto:lease-owner-race")
        store = self.store
        class ReclaimedMaterials(Materials):
            def candidate_snapshot(self, config):
                with store.connect() as conn:
                    conn.execute("UPDATE fb_auto_due_slot SET lease_owner='planner-new' WHERE id=?", (due["id"],))
                return super().candidate_snapshot(config)
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(
                self.template["id"], due["slot_key"], "auto", self.actor, Pages(), ReclaimedMaterials(),
                expected_template_version=1, expected_due_id=due["id"], expected_due_lease_owner="planner",
            )
        self.assertEqual(caught.exception.code, "fb_auto_due_slot_lease_superseded")
        with self.store.connect() as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_due_lease_expiry_token_blocks_old_worker_finalize_with_same_owner(self):
        self.enable()
        publish_at = (self.now + timedelta(hours=1)).isoformat()
        first = self.stage_due("auto:same-owner-lease-aba", planned_publish_at_utc=publish_at, lease_seconds=60)
        self.now += timedelta(seconds=61)
        second = self.store.claim_due_slot("planner", 60)
        self.assertEqual((second["id"], second["lease_owner"]), (first["id"], first["lease_owner"]))
        self.assertNotEqual(second["lease_expires_at_utc"], first["lease_expires_at_utc"])
        with self.assertRaises(StoreError) as stale_create:
            self.store.create_run(
                self.template["id"], first["slot_key"], "auto", self.actor, Pages(), Materials(),
                planned_publish_at_utc=publish_at, expected_template_version=1,
                expected_due_id=first["id"], expected_due_lease_owner=first["lease_owner"],
                expected_due_lease_expires_at_utc=first["lease_expires_at_utc"],
            )
        self.assertEqual(stale_create.exception.code, "fb_auto_due_slot_lease_superseded")
        self.assertFalse(self.store.complete_due_slot(first["id"], error_code="stale-complete", expected_lease_owner=first["lease_owner"], expected_lease_expires_at_utc=first["lease_expires_at_utc"]))
        self.assertFalse(self.store.defer_due_slot(first["id"], "stale-defer", expected_lease_owner=first["lease_owner"], expected_lease_expires_at_utc=first["lease_expires_at_utc"]))
        with self.store.connect() as conn:
            still_second = conn.execute("SELECT status,lease_owner,lease_expires_at_utc FROM fb_auto_due_slot WHERE id=?", (second["id"],)).fetchone()
        self.assertEqual(tuple(still_second), ("preparing", second["lease_owner"], second["lease_expires_at_utc"]))
        run = self.store.create_run(
            self.template["id"], second["slot_key"], "auto", self.actor, Pages(), Materials(),
            planned_publish_at_utc=publish_at, expected_template_version=1,
            expected_due_id=second["id"], expected_due_lease_owner=second["lease_owner"],
            expected_due_lease_expires_at_utc=second["lease_expires_at_utc"],
        )
        self.assertTrue(self.store.complete_due_slot(second["id"], run_id=run["run_id"], expected_lease_owner=second["lease_owner"], expected_lease_expires_at_utc=second["lease_expires_at_utc"]))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM fb_auto_due_slot WHERE id=?", (second["id"],)).fetchone()[0], "prepared")

    def test_complete_due_slot_marks_version_drift_missed(self):
        self.enable()
        due = self.stage_due("auto:complete-version-drift")
        run = self.store.create_run(self.template["id"], due["slot_key"], "auto", self.actor, Pages(), Materials(), expected_template_version=1)
        self.store.set_template_status(self.template["id"], False, self.actor, 1)
        changed = payload(); changed["name"] = "version two"
        self.store.update_template(self.template["id"], changed, self.actor, 1, {"app_id":"1479","product":"Dramawave"})
        self.store.complete_due_slot(due["id"], run_id=run["run_id"])
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,error_code,run_id FROM fb_auto_due_slot WHERE id=?", (due["id"],)).fetchone()
        self.assertEqual(tuple(row), ("missed", "fb_auto_due_slot_template_changed", None))

    def test_auto_run_rechecks_page_growth_and_capacity_after_catalog_freeze(self):
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        self.stage_due("auto:growth")
        class GrowingPages:
            def __init__(self): self.calls=0
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,*_args,**_kwargs):
                self.calls+=1; count=1 if self.calls==1 else 21
                return [PageTarget("6",("6",),str(10000+i),"248","UTC","en",1) for i in range(count)]
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(self.template["id"],"auto:growth","auto",self.actor,GrowingPages(),Materials(),expected_template_version=1,max_publishable_pages=100,max_jobs_per_slot=20,max_daily_jobs=1000)
        self.assertEqual(caught.exception.code,"fb_auto_capacity_exceeded")
        with self.store.connect() as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0],0)

    def test_eighth_token_at_968_seconds_completes_within_publish_and_reconcile_leases(self):
        self.store.create_run(self.template["id"],"manual:eight-token-budget","manual",self.actor,Pages(),Materials())
        self.enable()
        prepared=self.store.claim_prepare_next("prepare",3600)
        self.store.complete_prepare(prepared["id"],{"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        credentials=[PageCredential("10001",str(index),str(index),f"token-{index}") for index in range(1,9)]
        store=self.store
        class EightPages:
            def eligible_credentials(self,_page): return credentials
        class PublishGraph:
            def __init__(self): self.calls=0
            def publish_video(self,*_args):
                self.calls+=1; store_now[0]+=timedelta(seconds=121); self_outer.now=store_now[0]
                self_outer.assertEqual(store.mark_stale_running_unknown(),0)
                return GraphResult("success",post_id="vid_8") if self.calls==8 else GraphResult("definite_failure",error_code="fb_graph_190")
        self_outer=self; store_now=[self.now]; publish_started=self.now
        publish_graph=PublishGraph(); published=AutoPostExecutor(self.store,EightPages(),publish_graph,live_enabled=True,min_request_interval_seconds=0,rng=random.Random(1)).execute_next("publish",1200)
        self.assertEqual(published["status"],"submitted"); self.assertEqual(publish_graph.calls,8); self.assertEqual((self.now-publish_started).total_seconds(),968); self.assertLess((self.now-publish_started).total_seconds(),1200)
        self.now+=timedelta(minutes=5)
        blocked=[]
        class ReconcileGraph:
            def __init__(self): self.calls=0
            def reconcile_video(self,_object_id,_credential):
                self.calls+=1; self_outer.now+=timedelta(seconds=121)
                blocked.append(store.claim_submitted("duplicate",1200) is None)
                return GraphResult("success",post_id="vid_8") if self.calls==8 else GraphResult("credential_failure",error_code="fb_graph_190")
        reconcile_graph=ReconcileGraph(); reconciled=AutoPostExecutor(self.store,EightPages(),reconcile_graph,live_enabled=True,min_request_interval_seconds=0,rng=random.Random(2)).reconcile_next("reconcile",1200)
        self.assertEqual(reconciled["status"],"published"); self.assertEqual(reconcile_graph.calls,8); self.assertTrue(all(blocked))
        with self.store.connect() as conn:
            task=conn.execute("SELECT status,graph_post_id FROM fb_auto_task WHERE id=?",(prepared["id"],)).fetchone(); attempts=conn.execute("SELECT COUNT(*) FROM fb_auto_publish_attempt WHERE task_id=?",(prepared["id"],)).fetchone()[0]
        self.assertEqual(tuple(task),("published","vid_8")); self.assertEqual(attempts,8)


if __name__ == "__main__": unittest.main()

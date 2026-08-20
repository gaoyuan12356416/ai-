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
    def ready_next(self):
        task = self.store.claim_prepare_next("p", 3600)
        self.store.complete_prepare(task["id"], {"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        return self.store.claim_next("w", 900)

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
        self.store.create_run(self.template["id"], "auto:1", "auto", self.actor, Pages(), Materials(), expected_template_version=self.template["version"])
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
        store = self.store
        class DisablingMaterials(Materials):
            def candidate_snapshot(self, config):
                store.set_template_status(self_template["id"], False, self_actor, 1)
                return super().candidate_snapshot(config)
        self_template, self_actor = self.template, self.actor
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(self.template["id"], "auto:disable-race", "auto", self.actor, Pages(), DisablingMaterials(), expected_template_version=1)
        self.assertEqual(caught.exception.code, "fb_auto_due_slot_template_changed")
        with self.store.connect() as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)

    def test_auto_run_rechecks_page_growth_and_capacity_after_catalog_freeze(self):
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
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

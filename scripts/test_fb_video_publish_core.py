"""Offline transactional/worker tests. Every external call is a fake."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_video_publish.core import Store, DomainError, normalize_batch
from features.fb_video_publish.service import Service, PREFIX
from features.fb_video_publish.worker import Worker
from features.fb_video_publish.guards import install_backstops, prepare_available
from features.fb_auto_posts.publisher import GraphResult


class Sources:
    def __init__(self):
        self.pages = {str(i): {"page_id": str(i), "page_name": "Page " + str(i), "eligible_token_count": 2,
                              "language": "zh", "blocked_reason": "", "legacy_conflict": False} for i in (11, 12)}
        self.materials = {str(i): {"material_id": str(i), "material_name": "素材 " + str(i), "content_id": str(i + 100),
             "language": "zh", "drama_name": "测试剧", "drama_description": "简介", "material_tag": "",
             "duration_seconds": 30, "media_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test/" + str(i) + ".mp4"} for i in (21, 22)}
    def list_pages(self, actor, q="", *, page_ids=None):
        return [p for pid, p in self.pages.items() if page_ids is None or pid in page_ids]
    def get_pages(self, actor, ids):
        if any(self.pages[i]["blocked_reason"] for i in ids):
            raise DomainError("page_blocked", "授权异常", 409)
        return copy.deepcopy({i: self.pages[i] for i in ids})
    def get_materials(self, ids, need_desc=False):
        assert not need_desc, "Only the pair using desc may require it"
        return copy.deepcopy({i: self.materials[i] for i in ids})
    def search_materials(self, query):
        return {"items": list(self.materials.values()), "next_cursor": None, "has_more": False}


class NoShuffle:
    def shuffle(self, values): pass


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        self.store = Store(Path(self.temp.name) / "db.sqlite", now_fn=lambda: self.now)
        self.actor = dict(tenant_key="t1", user_id="u1", user_name="Tester", is_admin=False, business_owner_id="803", sub_user_id="100")
        self.sources = Sources()
        self.service = Service(self.store, self.sources, "x" * 48, live_enabled=True)
        self.configs = self.store.save_configs(self.actor, ["11", "12"], "zh", "{name}\n{desc}\n{url}")
    def req(self, pairs=None, **changes):
        pairs = pairs or [("21", "11")]
        configs = self.store.configs(self.actor)
        result = dict(operation_id=uuid.uuid4().hex, material_ids=list(dict.fromkeys(m for m,p in pairs)),
                      page_ids=list(dict.fromkeys(p for m,p in pairs)), processing_mode="original",
                      schedule_mode="now", scheduled_at_utc="", interval_minutes=0,
                      pairs=[dict(material_id=m,page_id=p, config_instance_id=configs[p]["config_instance_id"],
                                  config_version=configs[p]["version"], caption_template=configs[p]["caption_template"]) for m,p in pairs])
        result.update(changes)
        return result
    def preflight(self, req):
        return self.service.handle("POST", PREFIX + "/preflight", self.actor, req)
    def submit(self, req=None, repeat=False):
        req = req or self.req()
        return self.service.handle("POST", PREFIX + "/batches", self.actor,
                                  {**req, "preview_token": self.preflight(req)["preview_token"], "allow_repeat": repeat})
    def tasks(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM fb_video_task ORDER BY sequence")]
    def ready(self):
        with self.store.connect() as c: c.execute("UPDATE fb_video_task SET link_state='ready'")
    def worker(self, outcomes=None, reconcile=None, before=None, gpu=None):
        results = list(outcomes or [GraphResult("success", post_id="11_123")])
        calls = []
        def publish(*args):
            calls.append(args)
            if before: before()
            result = results.pop(0)
            if isinstance(result, BaseException): raise result
            return result
        pages = SimpleNamespace(eligible_credentials=lambda p: [SimpleNamespace(credential_id=str(i),fb_user_id=str(i),token="test") for i in range(2)])
        graph = SimpleNamespace(publish_video=publish, reconcile_video=lambda *args: reconcile or GraphResult("success", post_id="11_123"))
        worker = Worker(self.store, pages, gpu, graph, sources=self.sources, live_enabled=True, prepare_enabled=True,
                        rng=NoShuffle(), link_writer=lambda root,lid,long,**kw: {"short_url":"https://gy.g2flow.com/s2l/facebook/"+str(lid)+".html","long_url":long,"sha256":"a"*64},
                        readback=lambda *args: None)
        worker.calls = calls
        return worker


class CoreTests(Fixture):
    def test_configured_pages_only_resolve_saved_ids_and_keep_config_language(self):
        self.sources.pages["99"] = dict(self.sources.pages["11"], page_id="99")
        self.sources.pages["11"]["language"] = "en"
        with patch.object(self.sources, "list_pages", wraps=self.sources.list_pages) as lookup:
            result = self.service.configured_pages(self.actor)
        lookup.assert_called_once_with(self.actor, page_ids=["11", "12"])
        self.assertEqual([p["page_id"] for p in result["items"]], ["11", "12"])
        self.assertEqual(result["items"][0]["language"], "zh")
        self.sources.pages.pop("12")
        result = self.service.configured_pages(self.actor)
        self.assertEqual([p["page_id"] for p in result["items"]], ["11"])

    def test_empty_configured_pages_do_not_query_source(self):
        with patch.object(self.store, "configs", return_value={}), patch.object(self.sources, "list_pages") as lookup:
            self.assertEqual(self.service.configured_pages(self.actor), {"ok": True, "items": []})
        lookup.assert_not_called()

    def test_invalid_config_selections_do_not_start_source_queries(self):
        with patch.object(self.sources, "list_pages") as lookup:
            for ids in ([], ["11", "11"], [str(i) for i in range(1, 202)]):
                with self.subTest(ids=len(ids)), self.assertRaises(DomainError) as rejected:
                    self.service._config_access(self.actor, ids)
                self.assertEqual(rejected.exception.status, 400)
        lookup.assert_not_called()

    def test_preflight_readonly_real_link_budget_and_parameters(self):
        result = self.preflight(self.req())
        self.assertIn("[提交", result["items"][0]["caption"])
        self.assertEqual(8, len(result["items"][0]["long_url_parameters"]))
        with self.store.connect() as c:
            for name in ("batch","task","link","attempt"):
                self.assertEqual(0, c.execute("SELECT count(*) FROM fb_video_"+name).fetchone()[0])
    def test_expanded_short_link_budget(self):
        r=self.req(); r["pairs"][0]["caption_template"]="x"*4960+"{url}"
        with self.assertRaises(Exception): self.preflight(r)
    def test_missing_desc_only_affects_pair_using_it(self):
        self.sources.materials["22"]["drama_description"]=""
        r=self.req([("21","11"),("22","12")]); r["pairs"][1]["caption_template"]="{name}"
        self.assertEqual(2,len(self.preflight(r)["items"]))
        r["pairs"][1]["caption_template"]="{desc}"
        with self.assertRaises(Exception): self.preflight(r)
    def test_atomic_cas_and_recreate_instance(self):
        r=self.req()
        c=self.configs[0]
        self.store.delete_config(self.actor,"11",c["config_instance_id"],c["version"])
        new=self.store.save_configs(self.actor,["11"],"zh","{name}")[0]
        self.assertNotEqual(c["config_instance_id"],new["config_instance_id"])
        with self.assertRaises(DomainError): self.preflight(r)
        with self.assertRaises(DomainError): self.store.save_configs(self.actor,["13","12"],"zh","x")
        self.assertNotIn("13",self.store.configs(self.actor))
    def test_config_shared_draft_and_batch_private(self):
        other={**self.actor,"user_id":"u2"}
        self.assertEqual(2,len(self.store.configs(other)))
        self.store.draft(self.actor,{"x":1})
        self.assertIsNone(self.store.draft(other)["draft"])
        result=self.submit()
        with self.assertRaises(DomainError): self.store.get_batch(other,result["batch_id"])
        self.assertEqual(result["batch_id"],self.store.get_batch({**other,"is_admin":True},result["batch_id"])["batch"]["batch_id"])
        with self.assertRaises(DomainError): self.store.get_batch({**other,"is_admin":True,"tenant_key":"t2"},result["batch_id"])
    def test_signed_full_facts_and_atomic_failure(self):
        r=self.req([("21","11"),("22","12")]); token=self.preflight(r)["preview_token"]
        self.sources.materials["22"]["drama_name"]="changed"
        with self.assertRaises(DomainError) as raised:
            self.service.handle("POST",PREFIX+"/batches",self.actor,{**r,"preview_token":token,"allow_repeat":False})
        self.assertEqual("preview_changed",raised.exception.code)
        self.assertTrue(raised.exception.accepted is False)
        self.assertEqual([],self.tasks())
    def test_expiry_and_replay_bypass_changed_sources(self):
        r=self.req(); result=self.submit(r)
        self.now+=timedelta(hours=1)
        self.sources.pages["11"]["blocked_reason"]="gone"
        replay=self.service.handle("POST",PREFIX+"/batches",self.actor,{**r,"preview_token":"expired","allow_repeat":False})
        self.assertEqual(result["batch_id"],replay["batch_id"])
        r["pairs"][0]["caption_template"]="changed"
        with self.assertRaises(DomainError) as e:
            self.service.handle("POST",PREFIX+"/batches",self.actor,{**r,"preview_token":"","allow_repeat":False})
        self.assertEqual("operation_conflict",e.exception.code)
    def test_links_per_pair_and_2310_eight_fields(self):
        self.submit(self.req([("21","11"),("21","12"),("22","11")]))
        rows=self.tasks()
        self.assertEqual(3,len({r["short_url"] for r in rows}))
        self.assertEqual(3,len({r["gpu_job_id"] for r in rows}))
        for r in rows:
            parsed=urlsplit(r["long_url"]); q=parse_qs(parsed.query)
            self.assertEqual("/ads/101/2310/view",parsed.path); self.assertEqual(8,len(q))
            self.assertEqual(r["task_id"],q["af_c_id"][0]); self.assertEqual(r["page_id"],q["af_adset_id"][0])
            self.assertEqual("100",q["af_channel"][0]); self.assertTrue(q["c"][0].endswith("*"+str(r["ledger_id"])))
            self.assertNotIn("{url}",r["caption"])
    def test_repeat_explicit_and_active_global_cross_tenant(self):
        first=self.submit(); tid=self.tasks()[0]["task_id"]
        with self.assertRaises(DomainError): self.preflight(self.req())
        with self.store.connect() as c: c.execute("UPDATE fb_video_task SET status='published',graph_post_id='receipt' WHERE task_id=?",(tid,))
        r=self.req(); self.assertTrue(self.preflight(r)["repeat_required"])
        with self.assertRaises(DomainError): self.submit(r)
        self.submit(r,repeat=True)
        other={**self.actor,"tenant_key":"t2"}
        cfg=self.store.save_configs(other,["11"],"zh","{name}")[0]
        r2=self.req();r2["pairs"][0].update(config_instance_id=cfg["config_instance_id"],config_version=cfg["version"])
        with self.assertRaises(DomainError): self.service.handle("POST",PREFIX+"/preflight",other,r2)
    def test_language_authorization_unassigned_and_cap(self):
        r=self.req(); self.sources.materials["21"]["language"]="en"
        with self.assertRaises(DomainError): self.preflight(r)
        self.sources.materials["21"]["language"]="zh";self.sources.pages["11"]["blocked_reason"]="expired"
        with self.assertRaises(DomainError): self.preflight(r)
        r["material_ids"].append("22")
        with self.assertRaises(DomainError): normalize_batch(r)
        r=self.req();r["pairs"]*=201
        with self.assertRaises(DomainError): normalize_batch(r)
    def test_daily_quota_and_compact_list(self):
        self.store.daily_limit=1
        with self.assertRaises(DomainError): self.submit(self.req([("21","11"),("22","12")]))
        self.assertEqual([],self.tasks())
        self.submit()
        item=self.store.list_batches(self.actor)["items"][0]
        self.assertNotIn("items",item);self.assertEqual(1,item["task_count"])
    def test_task_pagination_and_owner_isolation(self):
        self.submit(self.req([("21","11"),("22","12")]))
        first=self.service.handle("GET",PREFIX+"/tasks",self.actor,query={"limit":"1","status":"queued"})
        self.assertTrue(first["has_more"]); self.assertEqual(1,len(first["items"]))
        second=self.service.handle("GET",PREFIX+"/tasks",self.actor,query={"limit":"1","cursor":first["next_cursor"]})
        self.assertNotEqual(first["items"][0]["task_id"],second["items"][0]["task_id"])
        other={**self.actor,"user_id":"u2"}
        self.assertEqual([],self.store.list_tasks(other)["items"])
        with self.assertRaises(DomainError):self.store.get_task(other,first["items"][0]["task_id"])
    def test_expired_preview_is_rejected_without_ids(self):
        r=self.req(); token=self.preflight(r)["preview_token"];self.now+=timedelta(minutes=16)
        with self.assertRaises(DomainError) as error:
            self.service.handle("POST",PREFIX+"/batches",self.actor,{**r,"preview_token":token,"allow_repeat":False})
        self.assertEqual("preview_expired",error.exception.code);self.assertEqual([],self.tasks())
    def test_same_operation_concurrent_submit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        r=self.req(); payload={**r,"preview_token":self.preflight(r)["preview_token"],"allow_repeat":False}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.service.handle("POST",PREFIX+"/batches",self.actor,payload),range(2)))
        self.assertEqual(results[0]["batch_id"],results[1]["batch_id"]);self.assertEqual(1,len(self.tasks()))
    def test_future_quota_uses_beijing_day(self):
        self.store.daily_limit=1
        r=self.req([("21","11"),("22","11")],schedule_mode="scheduled",
                   scheduled_at_utc="2026-10-20T15:59:00+00:00",interval_minutes=2)
        self.submit(r)
        self.assertEqual(["2026-10-20","2026-10-21"],[r["planned_bj_date"] for r in self.tasks()])
    def test_frozen_template_and_cancel_lease(self):
        self.submit()
        claim=self.store.claim("link","test")
        cfg=self.configs[0]
        self.store.save_configs(self.actor,["11"],"zh","NEW",expected=(cfg["config_instance_id"],cfg["version"]))
        self.assertNotIn("NEW",self.tasks()[0]["caption"])
        self.store.cancel(self.actor,claim["task_id"])
        self.assertEqual("stale_claim",self.store.finish_stage(claim,"link",{"sha256":"a"*64})["status"])
    def test_prepare_shared_capacity(self):
        self.submit(self.req([("21","11"),("22","12")],processing_mode="random_overlay"))
        with patch.dict(os.environ,{"FB_VIDEO_SHARED_GPU_PREPARES":"2"}):
            first=self.store.claim("prepare","one");self.assertIsNotNone(first)
            self.assertIsNone(self.store.claim("prepare","two"))
            self.now+=timedelta(seconds=10201)
            retry=self.store.claim("prepare","restart")
            self.assertEqual(first["gpu_job_id"],retry["gpu_job_id"])
            self.assertEqual("stale_claim",self.store.finish_stage(first,"prepare",{})["status"])
    def test_cancel_inflight_render_keeps_capacity_until_callback(self):
        self.submit(self.req([("21","11"),("22","12")],processing_mode="random_overlay"))
        first=self.store.claim("prepare","gpu")
        self.store.cancel(self.actor,first["task_id"])
        self.assertIsNone(self.store.claim("prepare","next"))
        self.assertEqual("stale_claim",self.store.finish_stage(first,"prepare",{})["status"])
        self.assertIsNotNone(self.store.claim("prepare","next"))
    def test_expired_prepare_callback_cannot_revive_even_before_reclaim(self):
        self.submit(self.req(processing_mode="random_overlay"))
        first=self.store.claim("prepare","old")
        self.now+=timedelta(seconds=10200)
        self.assertEqual("stale_claim",self.store.finish_stage(first,"prepare",{"media_url":"stale"})["status"])
        self.assertEqual("preparing",self.tasks()[0]["prepare_state"])
        current=self.store.claim("prepare","new")
        self.assertEqual(first["gpu_job_id"],current["gpu_job_id"])
        self.assertNotEqual(first["claim_token"],current["claim_token"])
        self.assertEqual("stage_complete",self.store.finish_stage(current,"prepare",{"media_url":"fresh"})["status"])
        self.assertEqual("fresh",json.loads(self.tasks()[0]["prepared_json"])["media_url"])
    def test_expired_link_error_or_success_cannot_rewrite_before_reclaim(self):
        self.submit()
        first=self.store.claim("link","old")
        self.now+=timedelta(seconds=300)
        self.assertEqual("stale_claim",self.store.finish_stage(first,"link",{"sha256":"bad"})["status"])
        self.assertEqual("stale_claim",self.store.finish_stage(first,"link",error=DomainError("bad","bad"),retry=True)["status"])
        self.assertEqual("writing",self.tasks()[0]["link_state"])
        current=self.store.claim("link","new")
        self.assertEqual("stage_complete",self.store.finish_stage(current,"link",{"sha256":"good"})["status"])
        self.assertEqual("good",self.tasks()[0]["link_sha256"])
    def test_sql_backstops_both_directions(self):
        with self.store.connect() as c:
            c.execute("CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,page_id TEXT,material_id TEXT,status TEXT,graph_post_id TEXT DEFAULT '',unknown_outcome INTEGER DEFAULT 0)")
            install_backstops(c)
            c.execute("INSERT INTO fb_auto_task(page_id,material_id,status) VALUES('11','21','planned')")
        with self.assertRaises(DomainError): self.submit()
        with self.store.connect() as c: c.execute("DELETE FROM fb_auto_task")
        self.submit()
        with self.store.connect() as c:
            with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO fb_auto_task(page_id,material_id,status) VALUES('11','21','planned')")


class WorkerTests(Fixture):
    def test_persist_intent_before_graph_and_reconcile(self):
        self.submit();self.ready()
        def before():
            with self.store.connect() as c:
                self.assertEqual("started",c.execute("SELECT result_kind FROM fb_video_attempt ORDER BY id DESC LIMIT 1").fetchone()[0])
        worker=self.worker(before=before)
        self.assertEqual("submitted",worker.execute_next()["status"])
        self.now+=timedelta(seconds=61)
        self.assertEqual("published",worker.reconcile_next()["status"])
        self.assertEqual(1,len(worker.calls))
    def test_unknown_stops_tokens_and_page_successor(self):
        self.submit(self.req([("21","11"),("22","11")]));self.ready()
        worker=self.worker([GraphResult("unknown")])
        self.assertEqual("unknown",worker.execute_next()["status"])
        self.assertEqual("no_pending",worker.execute_next()["status"]);self.assertEqual(1,len(worker.calls))
    def test_definite_failure_rotates_until_all_credentials_rejected(self):
        self.submit();self.ready();worker=self.worker([GraphResult("definite_failure",error_code="fb_graph_100"),
                                                     GraphResult("definite_failure",error_code="fb_graph_368")])
        self.assertEqual("failed",worker.execute_next()["status"]);self.assertEqual(2,len(worker.calls))
        self.assertEqual("fb_graph_368",self.tasks()[0]["error_code"])
        self.assertIn("全部2个",self.tasks()[0]["error_message"])
        with self.store.connect() as c:
            self.assertEqual([("0","fb_graph_100"),("1","fb_graph_368")],
                             [tuple(r) for r in c.execute("SELECT credential_id,error_code FROM fb_video_attempt ORDER BY id")])
    def test_368_rotates_then_unknown_stops_remaining_credentials(self):
        self.submit();self.ready();worker=self.worker([GraphResult("definite_failure",error_code="fb_graph_368"),GraphResult("unknown")])
        worker.pages.eligible_credentials=lambda p: [SimpleNamespace(credential_id=str(i),fb_user_id=str(i),token="test-"+str(i)) for i in range(3)]
        self.assertEqual("unknown",worker.execute_next()["status"]);self.assertEqual(2,len(worker.calls))
    def test_all_credentials_including_ninth_and_later_keep_current_lease(self):
        self.submit();self.ready()
        def slow_response():
            self.now+=timedelta(seconds=119)
            self.assertEqual(0,self.store.tick()["unknown_marked"])
            self.assertIsNone(self.store.claim("execute","competing"))
        worker=self.worker([GraphResult("definite_failure",error_code="fb_graph_368")]*11+
                           [GraphResult("success",post_id="final-receipt")],before=slow_response)
        worker.pages.eligible_credentials=lambda p: [SimpleNamespace(credential_id=str(i),fb_user_id=str(i),token="test-"+str(i)) for i in range(12)]
        self.assertEqual("submitted",worker.execute_next()["status"])
        self.assertEqual(12,len(worker.calls))
        self.assertEqual(12,len({call[1].credential_id for call in worker.calls}))
        self.assertEqual("final-receipt",self.tasks()[0]["graph_post_id"])
        with self.store.connect() as c:
            self.assertEqual(12,c.execute("SELECT COUNT(*) FROM fb_video_attempt").fetchone()[0])
    def test_receipt_with_failure_never_rotates(self):
        self.submit();self.ready();worker=self.worker([GraphResult("definite_failure",post_id="receipt",error_code="fb_graph_368")])
        self.assertEqual("submitted",worker.execute_next()["status"]);self.assertEqual(1,len(worker.calls))
    def test_expired_claim_cannot_renew_or_write(self):
        self.submit();self.ready();claim=self.store.claim("execute","old")
        self.now+=timedelta(seconds=1201)
        self.assertIsNone(self.store.begin_attempt(claim,"1","1"))
        self.assertEqual(1,self.store.tick()["unknown_marked"])
    def test_inflight_intent_cannot_start_another_token(self):
        self.submit();self.ready();claim=self.store.claim("execute","old")
        self.assertIsNotNone(self.store.begin_attempt(claim,"1","1"))
        self.assertIsNone(self.store.begin_attempt(claim,"2","2"))
    def test_credential_failure_can_rotate(self):
        self.submit();self.ready();worker=self.worker([GraphResult("definite_failure",error_code="fb_graph_190"),GraphResult("success",post_id="p")])
        self.assertEqual("submitted",worker.execute_next()["status"]);self.assertEqual(2,len(worker.calls))
    def test_empty_receipt_and_transport_exception_unknown(self):
        for outcome in (GraphResult("success"),RuntimeError("network")):
            self.setUp()
            self.submit();self.ready();worker=self.worker([outcome])
            self.assertEqual("unknown",worker.execute_next()["status"]);self.assertEqual(1,len(worker.calls))
    def test_started_crash_remains_unknown(self):
        self.submit();self.ready()
        claim=self.store.claim("execute","crash")
        self.store.begin_attempt(claim,"1","1")
        self.now+=timedelta(seconds=1201)
        self.assertEqual(1,self.store.tick()["unknown_marked"])
        self.assertIsNone(self.store.claim("execute","restart"))
        with self.store.connect() as c:self.assertEqual("started",c.execute("SELECT result_kind FROM fb_video_attempt").fetchone()[0])
    def test_same_page_waits_for_reconciled_predecessor(self):
        self.submit(self.req([("21","11"),("22","11")]));self.ready()
        worker=self.worker([GraphResult("success",post_id="p"),GraphResult("success",post_id="p2")])
        worker.execute_next();self.assertEqual("no_pending",worker.execute_next()["status"])
        self.now+=timedelta(seconds=61);worker.reconcile_next()
        self.assertEqual("submitted",worker.execute_next()["status"])
    def test_late_predecessor_enforces_actual_interval_without_moving_quota(self):
        self.submit(self.req([("21","11"),("22","11")],interval_minutes=10));self.ready()
        frozen=[(r["planned_at_utc"],r["planned_bj_date"]) for r in self.tasks()]
        self.now+=timedelta(minutes=20)
        worker=self.worker([GraphResult("success",post_id="p"),GraphResult("success",post_id="p2")])
        worker.execute_next();self.now+=timedelta(seconds=61);worker.reconcile_next()
        self.assertEqual("no_pending",worker.execute_next()["status"])
        self.now+=timedelta(minutes=9,seconds=59)
        self.assertEqual("no_pending",worker.execute_next()["status"])
        self.now+=timedelta(seconds=1)
        self.assertEqual("submitted",worker.execute_next()["status"])
        self.assertEqual(frozen,[(r["planned_at_utc"],r["planned_bj_date"]) for r in self.tasks()])
    def test_failed_predecessor_does_not_add_success_interval(self):
        self.submit(self.req([("21","11"),("22","11")],interval_minutes=10));self.ready()
        self.now+=timedelta(minutes=20)
        worker=self.worker([GraphResult("definite_failure",error_code="fb_graph_100"),
                            GraphResult("definite_failure",error_code="fb_graph_368"),GraphResult("success",post_id="p2")])
        self.assertEqual("failed",worker.execute_next()["status"])
        self.assertEqual("submitted",worker.execute_next()["status"])
    def test_no_call_until_link_verified(self):
        self.submit();worker=self.worker()
        self.assertEqual("no_pending",worker.execute_next()["status"])
        worker.link_next()
        self.assertEqual("submitted",worker.execute_next()["status"])
    def test_recipe_is_frozen_and_each_pair_distinct_job(self):
        self.submit(self.req([("21","11"),("21","12")],processing_mode="random_overlay"))
        seen=[]
        def gpu(**kw):
            seen.append(kw["job_id"])
            return {"media_url":"https://cdn.test/"+kw["job_id"]+".mp4","sha256":"b"*64,"profile":"p","random_overlay_recipe":{"seed":kw["job_id"]}}
        worker=self.worker(gpu=SimpleNamespace(prepare=gpu))
        worker.prepare_next();worker.prepare_next()
        self.assertEqual(2,len(set(seen)))
        for row in self.tasks():self.assertEqual(row["gpu_job_id"],json.loads(row["prepared_json"])["random_overlay_recipe"]["seed"])
    def test_gpu_retry_reuses_job_and_permanent_mismatch_fails(self):
        from features.fb_auto_posts.gpu import GPUPrepareError
        self.submit(self.req(processing_mode="random_overlay"))
        jobs=[]
        def gpu(**kw):
            jobs.append(kw["job_id"])
            if len(jobs)==1:raise GPUPrepareError("fb_auto_gpu_unavailable","unavailable",503)
            raise GPUPrepareError("fb_auto_prepared_identity_mismatch","mismatch",502)
        worker=self.worker(gpu=SimpleNamespace(prepare=gpu))
        worker.prepare_next()
        self.assertEqual("queued",self.tasks()[0]["status"])
        self.now+=timedelta(minutes=6);worker.prepare_next()
        self.assertEqual("failed",self.tasks()[0]["status"]);self.assertEqual(jobs[0],jobs[1])
    def test_gpu_recipe_is_required_for_new_manual_contract(self):
        self.submit(self.req(processing_mode="random_overlay"))
        worker=self.worker(gpu=SimpleNamespace(prepare=lambda **kw:{"media_url":"https://cdn.test/out.mp4","sha256":"b"*64,"profile":"p"}))
        worker.prepare_next()
        self.assertEqual("failed",self.tasks()[0]["status"])
        self.assertEqual("prepared_recipe_missing",self.tasks()[0]["error_code"])
    def test_fresh_material_drift_blocks_graph(self):
        self.submit();self.ready();self.sources.materials["21"]["content_id"]="999"
        worker=self.worker();self.assertEqual("failed",worker.execute_next()["status"]);self.assertEqual([],worker.calls)


class CrossPipelineTests(Fixture):
    def auto(self):
        from decimal import Decimal
        from features.fb_auto_posts.core import FBAutoPostStore, ActorScope
        from features.fb_auto_posts.repositories import PageTarget, MaterialCandidate, CandidateSnapshot
        from scripts.test_fb_auto_validation import payload
        old=FBAutoPostStore(self.store.path,now_fn=lambda:self.now)
        with self.store.connect() as c:install_backstops(c)
        actor=ActorScope("u","old",True,"803")
        template=old.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave"})
        old.set_template_status(template["id"],True,actor,template["version"])
        class Pages:
            def legacy_conflicts(self,*args):return []
            def list_pages(self,*args,**kw):
                return [PageTarget("6",("6",),p,"803","UTC","zh",1) for p in ("11","12")]
        class Materials:
            def candidate_snapshot(self,*args):
                return CandidateSnapshot((MaterialCandidate("501","601","https://cdn.test/501.mp4","m","d","zh",Decimal("30"),Decimal("5"),Decimal("50"),Decimal("10"),Decimal("60"),"1"),),(1,),("2026-10-19",))
            def choose_from(self,candidates,excluded):return next((m for m in candidates if m.material_id not in excluded),None)
        old.create_run(template["id"],"manual:one","manual",actor,Pages(),Materials())
        return old
    def test_full_schema_two_prepares_combined_and_due_priority(self):
        old=self.auto()
        self.submit(self.req(processing_mode="random_overlay"))
        with patch.dict(os.environ,{"FB_VIDEO_SHARED_GPU_PREPARES":"2"}):
            self.assertIsNotNone(old.claim_prepare_next("auto",3600))
            self.assertIsNotNone(self.store.claim("prepare","manual"))
            self.assertIsNone(old.claim_prepare_next("auto2",3600))
    def test_manual_execution_holds_old_claim_until_reconciled(self):
        old=self.auto()
        task=old.claim_prepare_next("auto",3600)
        old.complete_prepare(task["id"],{"media_url":"https://cdn.test/prepared.mp4","sha256":"a"*64,"size_bytes":1,
             "duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        self.submit();self.ready()
        manual=self.store.claim("execute","manual")
        self.assertIsNotNone(manual)
        self.assertIsNone(old.claim_next("auto"))
        self.store.record_outcome(manual,GraphResult("unknown"))
        self.assertIsNone(old.claim_next("auto"))
    def test_old_execution_holds_new_claim_on_same_page(self):
        old=self.auto()
        task=old.claim_prepare_next("auto",3600)
        old.complete_prepare(task["id"],{"media_url":"https://cdn.test/prepared.mp4","sha256":"a"*64,"size_bytes":1,
             "duration_seconds":30,"profile":"tt-post-random-overlay-h264-720x1280-v3"})
        oldclaim=old.claim_next("auto")
        self.assertIsNotNone(oldclaim)
        self.submit();self.ready()
        self.assertIsNone(self.store.claim("execute","manual"))


class SidecarTests(Fixture):
    def test_manual_body_accepts_exact_4mib_and_rejects_one_byte_over(self):
        import io
        from features.fb_auto_posts.service import Handler, ServiceError
        for limit,path in ((4*1024*1024,PREFIX+"/preflight"),(256*1024,"/api/fb-auto-post/templates")):
            with self.subTest(path=path):
                body=b'{"value":"' + b"x"*(limit-12) + b'"}'
                self.assertEqual(limit,len(body))
                handler=SimpleNamespace(path=path,headers={"Content-Length":str(len(body))},rfile=io.BytesIO(body))
                self.assertEqual(limit-12,len(Handler.read_json(handler)["value"]))
                handler.headers["Content-Length"]=str(limit+1)
                with self.assertRaises(ServiceError):
                    Handler.read_json(handler)
    def test_canary_opaque_content_identity(self):
        from scripts.validate_fb_video_prepare_only import content_id
        for text in ("dz7pZsXfV0","123","drama_A-9"):
            self.assertEqual(text,content_id(text))
        for text in ("","x"*129,"id\nother","\ud800"):
            with self.assertRaises(ValueError):content_id(text)
    def test_internal_actor_header_domain_errors_and_bounded_routes(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from features.fb_auto_posts.service import Handler
        runtime=SimpleNamespace(internal_token="z"*48,manual=self.service,manual_worker=self.worker())
        class TestHandler(Handler):
            pass
        TestHandler.runtime=runtime
        server=ThreadingHTTPServer(("127.0.0.1",0),TestHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method,path,payload=None,authorized=True):
            connection=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            headers={"Content-Type":"application/json","X-FB-Video-Actor":json.dumps(self.actor)}
            if authorized:headers["Authorization"]="Bearer "+"z"*48
            connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
            response=connection.getresponse();data=json.loads(response.read());connection.close()
            return response.status,data
        self.assertEqual(403,request("GET",PREFIX+"/pages",authorized=False)[0])
        self.assertEqual(200,request("GET",PREFIX+"/pages")[0])
        status,data=request("POST",PREFIX+"/preflight",{"bad":True})
        self.assertEqual(400,status);self.assertIs(False,data["accepted"])
        status,data=request("POST",PREFIX+"/preflight",self.req())
        self.assertEqual(200,status);self.assertIn("preview_token",data)
        self.assertEqual(404,request("GET",PREFIX+"/tasks/"+"0"*32)[0])


if __name__ == "__main__":
    unittest.main()

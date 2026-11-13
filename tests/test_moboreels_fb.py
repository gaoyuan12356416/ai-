"""Offline FB receipts/creator isolation; run canonical and FB runtime overlay."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import features
if os.environ.get("FB_TEST_OVERLAY") == "1":
    features.__path__.insert(0, str(ROOT / "ops/moboreels-attribution-20261113/runtime/fb/features"))

import features.fb_auto_posts
# Canonical checkout omits production-only helpers; load those from the fetched baseline.
features.fb_auto_posts.__path__.append(str(ROOT / "ops/moboreels-attribution-20261113/runtime/fb/features/fb_auto_posts"))
from features.fb_video_publish import moboreels, templates
if os.environ.get("FB_ELIGIBILITY_OVERLAY") == "1":
    import importlib.util
    spec = importlib.util.spec_from_file_location('features.fb_video_publish.moboreels', ROOT / 'ops/moboreels-publish-eligibility-20261113/runtime/fb/features/fb_video_publish/moboreels.py')
    spec.loader.exec_module(moboreels)
from features.fb_video_publish.core import Store, canonical
from features.fb_video_publish.worker import Worker
from features.fb_video_publish.sources import SourceRepository
from features.fb_auto_posts import moboreels as auto
from features.fb_auto_posts.repositories import MaterialCandidate, MaterialRepository

NOW = datetime(2026, 11, 13, 1, tzinfo=timezone.utc)
MATERIAL = dict(material_id="555", content_id="story-999", material_name="clip", language="en",
                drama_name="Title", drama_description="Description", duration_seconds=40,
                media_url="https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test.mp4")
STORY = dict(story_id="story-999", story_name="Provider title", introduce="Provider description", lang_id=3, is_show=1)
LINK = dict(status="succeeded", story_id="story-999", short_url="https://ai.yingliangads.com/m/abcdefghijklmno",
            target_url="https://moboreels.example/story/999", short_link_id="abc", promotion_key="pk",
            promotion_request_id="pr", koc_code="code")
ACTOR = dict(tenant_key="tenant", user_id="creator", user_name="Creator", sub_user_id="12", is_admin=False)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/"video.sqlite", now_fn=lambda: NOW)

    def freeze(self, mode="original", caption="{name} {{m_url}}", pool=None, group_pool=None):
        pair = dict(material_id="555", page_id="12345", caption_template=caption)
        request = dict(operation_id="op1", processing_mode=mode, pairs=[pair], interval_minutes=0)
        config = {"12345": dict(config_instance_id="a"*32, version=1, language="en", caption_template=caption)}
        resolved = dict(materials={"555": MATERIAL}, pages={"12345": dict(page_name="Page", page_id="12345")})
        with self.store.connect() as conn, patch.object(moboreels.macro_client, "resolve_m_url", side_effect=AssertionError("preview must not create")):
            conn.execute("BEGIN IMMEDIATE")
            result = self.store.freeze(conn, ACTOR, request, resolved, config, {}, [(1,pair,1,NOW,"2026-11-13")], pool=pool, group_pool=group_pool)
            conn.commit()
        return result["batch"]["items"][0]

    def worker(self):
        return Worker(self.store, Mock(), Mock(), Mock(), live_enabled=True, prepare_enabled=True,
                      link_writer=Mock(side_effect=AssertionError("legacy wrapper called")),
                      readback=Mock(side_effect=AssertionError("legacy readback called")))

    def remote(self, actor, **kwargs):
        # Acquiring a write lock proves no caller SQLite write txn spans networking.
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        self.assertEqual(actor["user_id"], "creator")
        self.assertEqual(kwargs["story_id"], "story-999")
        self.assertEqual(kwargs["material_id"], "555")
        self.assertTrue(kwargs["task_id"].startswith("fb:video:"))
        return LINK

    def test_preview_and_freeze_are_read_only_and_link_receipt_is_reused(self):
        task = self.freeze()
        self.assertEqual(task["short_url"], "")
        worker = self.worker()
        with patch.object(moboreels.macro_client,"lookup_story", return_value=STORY), patch.object(moboreels.macro_client,"resolve_actor", return_value={"tenant_key":"tenant","user_id":"creator","name":"Renamed"}), patch.object(moboreels.macro_client,"resolve_m_url",side_effect=self.remote) as resolve:
            worker.link_next()
            with self.store.connect() as conn:
                frozen=dict(conn.execute("SELECT * FROM fb_video_task").fetchone())
            self.assertEqual(frozen["short_url"], LINK["short_url"])
            self.assertEqual(frozen["caption"], "Provider title "+LINK["short_url"])
            worker._moboreels_link(frozen, kind="execute")
            self.assertEqual(resolve.call_count, 1)
        worker.link_writer.assert_not_called()

    def test_failure_does_not_reach_gpu_or_graph(self):
        self.freeze("random_overlay")
        worker=self.worker()
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY), patch.object(moboreels.macro_client,"resolve_actor", return_value={"user_id":"creator"}), patch.object(moboreels.macro_client,"resolve_m_url",side_effect=moboreels.macro_client.MoboreelsLinkError("结果未知，待核对")) as resolve:
            worker.prepare_next()
            worker.prepare_next()
        self.assertEqual(resolve.call_count,1)
        worker.gpu.prepare.assert_not_called()
        worker.graph.publish_video.assert_not_called()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM fb_video_task").fetchone()[0],"failed")

    def test_creator_snapshot_tampering_fails_before_remote(self):
        self.freeze()
        with self.store.connect() as conn:
            row=conn.execute("SELECT * FROM fb_video_task").fetchone()
            snap=json.loads(row["snapshot_json"])
            snap["moboreels"]["creator"]["user_id"]="page-owner"
            conn.execute("UPDATE fb_video_task SET snapshot_json=?",(canonical(snap),))
        with patch.object(moboreels.macro_client,"resolve_m_url") as resolve:
            self.worker().link_next()
        resolve.assert_not_called()

    def test_prepare_freezes_receipt_before_gpu(self):
        self.freeze("random_overlay")
        worker=self.worker()
        def prepare(**kwargs):
            with self.store.connect() as conn:
                frozen=json.loads(conn.execute("SELECT snapshot_json FROM fb_video_task").fetchone()[0])
            self.assertEqual(frozen["moboreels"]["link"], LINK)
            return dict(media_url="https://cdn.example/prepared.mp4",profile="test",sha256="a"*64,random_overlay_recipe={"a":1})
        worker.gpu.prepare.side_effect=prepare
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",return_value={"user_id":"creator"}),patch.object(moboreels.macro_client,"resolve_m_url",side_effect=self.remote):
            worker.prepare_next()
        worker.gpu.prepare.assert_called_once()

    def test_legacy_freeze_keeps_original_wrapper(self):
        task=self.freeze(caption="{name} {url}")
        self.assertIn("/s2l/facebook/",task["short_url"])


class MacroTests(unittest.TestCase):
    def test_braces_preview_and_safe_links(self):
        for macro in ("{{m_url}}",):
            self.assertEqual(templates.render_template("{name} "+macro,MATERIAL,LINK["short_url"]),"Title "+LINK["short_url"])
            self.assertNotIn("https://",templates.render_preview(macro,MATERIAL))
        self.assertFalse(moboreels.contains("{m_url}"))
        with self.assertRaises(templates.TemplateError):
            templates.render_template("{m_url}",MATERIAL,LINK["short_url"],manual=True)
        with self.assertRaises(Exception):
            templates.render_template("{{m_url}}",MATERIAL,"https://evil.example/m/abcdefghijk")
        self.assertEqual(templates.render_template("{{name}} {desc}",MATERIAL,LINK["short_url"]),"Title Description")

    def test_no_recursive_macro_or_mixed_url(self):
        self.assertEqual(auto.moboreels.render_auto("{{drama_name}} {{m_url}}",dict(MATERIAL,drama_name="{{m_url}}"),LINK["short_url"]),"{{m_url}} "+LINK["short_url"])
        with self.assertRaises(moboreels.MoboreelsFBError):
            auto.moboreels.render_auto("{{url}} {{m_url}}",MATERIAL,LINK["short_url"])

    def test_source_branch_never_uses_dramawave_metadata(self):
        mysql=SimpleNamespace(schema="kunlunads_dev",blacklist_schema="ads_setting")
        repo=SourceRepository.__new__(SourceRepository)
        repo.mysql=mysql
        repo.now_fn=lambda:NOW
        sql,params=repo._conditions(manual=True,moboreels=True)
        self.assertNotIn("ads_drama_info",sql)
        self.assertNotIn("Dramawave",params)
        self.assertIn("s.type=2",sql)

    def test_read_broker_permits_only_the_exact_catalog_table(self):
        from features.fb_video_publish.query_broker import validate_query
        payload=dict(sql="SELECT d.story_id FROM `ads_ai`.cdreader_koc_drama d WHERE d.institution_user_id=%s LIMIT 10",params=["institution"],schema="kunlunads_dev",blacklist_schema="ads_setting")
        self.assertEqual(validate_query(payload)[0],payload["sql"])
        for sql in (payload["sql"].replace("ads_ai","kunlunads_dev"),payload["sql"].replace("cdreader_koc_drama","cdreader_koc_promotion"),"DELETE FROM `ads_ai`.cdreader_koc_drama"):
            with self.assertRaises(ValueError):validate_query(dict(payload,sql=sql))


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"auto.sqlite"
        from features.fb_auto_posts.core import _ClosingConnection
        def connect():
            conn=sqlite3.connect(self.path,factory=_ClosingConnection);conn.row_factory=sqlite3.Row;return conn
        self.store=SimpleNamespace(connect=connect,now_fn=lambda:NOW)
        self.intent=auto.intent({"message_template":"{{drama_name}} {{m_url}}"},MATERIAL,{"created_by":"creator","created_name":"Original","owner_user_id":"page-owner"})
        with connect() as c:
            c.execute("CREATE TABLE fb_auto_task(id INTEGER,material_id TEXT,content_id TEXT,page_id TEXT,status TEXT,moboreels_json TEXT,message_text TEXT,short_url TEXT,long_url TEXT,lease_owner TEXT,lease_expires_at_utc TEXT)")
            c.execute("INSERT INTO fb_auto_task VALUES(1,'555','story-999','12345','preparing',?,'{{m_url}}','','','worker','2026-11-13T03:00:00+00:00')",(json.dumps(self.intent),))
        self.task=self.read()

    def read(self):
        with self.store.connect() as c:return dict(c.execute("SELECT * FROM fb_auto_task").fetchone())

    def test_auto_uses_template_creator_and_reuses_frozen_receipt(self):
        def remote(actor,**kwargs):
            with self.store.connect() as c:c.execute("BEGIN IMMEDIATE");c.rollback()
            self.assertEqual(actor["user_id"],"creator")
            self.assertEqual(kwargs["task_id"],"fb:auto:1")
            self.assertEqual(kwargs["story_id"],"story-999")
            return LINK
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",side_effect=lambda uid,*args:{"user_id":uid}) as actor,patch.object(moboreels.macro_client,"resolve_m_url",side_effect=remote) as resolve:
            result=auto.ensure_link(self.store,self.task)
            self.assertEqual(result["short_url"],LINK["short_url"])
            auto.ensure_link(self.store,result)
            self.assertEqual(resolve.call_count,1)
            self.assertEqual(actor.call_args.args[0],"creator")

    def test_legacy_needs_no_new_database_access(self):
        self.assertEqual(auto.ensure_link(None,{"message_text":"legacy"}),{"message_text":"legacy"})

    def test_intent_serializes_decimal_and_no_owner_fallback(self):
        material=MaterialCandidate("555","story-999",MATERIAL["media_url"],"clip","Title","en",Decimal(40),None,None,None,None,"moboreels")
        intent=auto.intent({"message_template":"{{m_url}}"},material,{"created_by":"creator","owner_user_id":"page-owner"})
        json.dumps(intent)
        self.assertEqual(intent["creator"]["user_id"],"creator")
        with self.assertRaises(moboreels.MoboreelsFBError):moboreels.resolve({},MATERIAL,page_id="12345",task_id="1")

    def test_default_selection_does_not_read_legacy_metrics(self):
        config=dict(message_template="{{m_url}}",product="Dramawave",material_data_source=6,language="en",drama_launch_window_days=0,
                    drama_rule={},material_rule=dict(duration_min_seconds=1,duration_max_seconds=600))
        mysql=Mock(schema="kunlunads_dev")
        mysql.select.return_value=[dict(MATERIAL,video_duration=40)]
        repo=MaterialRepository(mysql,Mock())
        with patch.dict(os.environ,{"MOBOREELS_INSTITUTION_USER_ID":"institution"}):
            result=repo.candidate_snapshot(config)
        self.assertEqual(len(result.candidates),1)
        self.assertIsNone(result.candidates[0].material_spend)
        repo.metric_store.load_metric_window.assert_not_called()
        sql=mysql.select.call_args.args[0]
        self.assertIn("`ads_ai`.cdreader_koc_drama",sql)
        self.assertNotIn("ads_drama_info",sql)
        self.assertIn("d.institution_user_id=%s",sql)
        self.assertEqual(mysql.select.call_args.args[1][0],"institution")
        self.assertIn("s.product=%s",sql)
        for rule in ({"spend_min":"1"},{"roas_max":"0"}):
            with self.assertRaises(Exception):auto.validate_rules(dict(config,drama_rule=rule))

    def test_actual_create_run_freezes_template_creator_without_network(self):
        from features.fb_auto_posts.core import FBAutoPostStore, ActorScope
        from features.fb_auto_posts.repositories import PageTarget, CandidateSnapshot
        store=FBAutoPostStore(Path(self.tmp.name)/"full.sqlite",now_fn=lambda:NOW)
        rule=dict(spend_min=None,spend_max=None,roas_min=None,roas_max=None,sort_by="spend",sort_direction="desc")
        config=dict(name="Moboreels test",group_ids=["1"],language="en",message_template="{{drama_name}} {{m_url}}",
            drama_rule=dict(rule,resource_type_v2=[]),material_rule=dict(rule,duration_min_seconds=1,duration_max_seconds=600),
            schedule=dict(mode="fixed",times=["09:00"]),video_template="random_overlay",cooldown_days=0)
        creator=ActorScope("creator","Creator",False,"12")
        operator=ActorScope("administrator","Admin",True,"")
        template=store.create_template(config,creator,dict(app_id="1479",product="Dramawave",metric_product="Dramawave",metric_platform=0,material_data_source=6))
        with store.connect() as conn:
            conn.execute("UPDATE fb_auto_template SET status='enabled'")
        material=MaterialCandidate("555","story-999",MATERIAL["media_url"],"clip","Title","en",Decimal(40),None,None,None,None,"moboreels")
        pages=Mock()
        pages.legacy_conflicts.return_value=[]
        pages.list_pages.return_value=[PageTarget("1",("1",),"12345","12","UTC","en",1)]
        materials=Mock()
        materials.candidate_snapshot.return_value=CandidateSnapshot((material,),(),())
        materials.choose_from.return_value=material
        with patch.object(moboreels.macro_client,"resolve_m_url",side_effect=AssertionError("freeze must not make request")):
            store.create_run(template["id"],"test-slot","manual",operator,pages,materials)
        with store.connect() as conn:
            row=dict(conn.execute("SELECT * FROM fb_auto_task").fetchone())
        self.assertEqual(json.loads(row["moboreels_json"])["creator"]["user_id"],"creator")
        self.assertIn("{{m_url}}",row["message_text"])
        self.assertFalse(row["short_url"])
        if os.environ.get("FB_TEST_OVERLAY") == "1":
            from features.pipeline_dispatch.fb_native import FBNativeBridge
            runtime=SimpleNamespace(store=store,executor=SimpleNamespace(live_enabled=True,short_link_writer=Mock(),short_link_root=""))
            bridge=FBNativeBridge(runtime)
            job=dict(channel="fb",family="auto",source_id=str(row["id"]),source_key="fb:auto:"+str(row["id"]),stage="prepare",generation=1,executor_backend="cpu")
            claim=dict(claim_token="a"*32,node_id="test")
            with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",return_value={"user_id":"creator"}),patch.object(moboreels.macro_client,"resolve_m_url",return_value=LINK) as resolve:
                descriptor=bridge.reserve(job,claim)
                self.assertEqual(descriptor["kind"],"fb_prepare")
                self.assertEqual(bridge.reserve(job,claim),descriptor)
                self.assertEqual(resolve.call_count,1)
            with store.connect() as conn:
                task=dict(conn.execute("SELECT * FROM fb_auto_task").fetchone())
                receipt=json.loads(conn.execute("SELECT task_json FROM pipeline_fb_claim").fetchone()[0])
            self.assertEqual(json.loads(receipt["moboreels_json"])["link"],LINK)
            task.update(prepared_media_url="https://cdn.example/prepared.mp4",media_url="https://cdn.example/prepared.mp4")
            with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY):
                request=bridge._auto_preflight(task)
            self.assertIn(LINK["short_url"],request["message"])
            runtime.executor.short_link_writer.assert_not_called()

    @unittest.skipUnless(os.environ.get("FB_TEST_OVERLAY") == "1", "manual_batch is a live FB feature")
    def test_manual_batch_freezes_intent_then_preparer_reuses_creator_receipt(self):
        from features.fb_auto_posts.core import FBAutoPostStore, ActorScope
        from features.fb_auto_posts.repositories import PageTarget
        from features.fb_auto_posts.manual_batch import build_manifest, reserve_batch
        store=FBAutoPostStore(Path(self.tmp.name)/"batch.sqlite",now_fn=lambda:NOW)
        rule=dict(spend_min=None,spend_max=None,roas_min=None,roas_max=None,sort_by="spend",sort_direction="desc")
        config=dict(name="Batch test",group_ids=["1"],language="en",message_template="{{drama_name}} {{m_url}}",
            drama_rule=dict(rule,resource_type_v2=[]),material_rule=dict(rule,duration_min_seconds=1,duration_max_seconds=600),
            schedule=dict(mode="fixed",times=["09:00"]),video_template="random_overlay",cooldown_days=0)
        template=store.create_template(config,ActorScope("creator","Original",False,"12"),dict(app_id="1479",product="Dramawave",metric_product="Dramawave",metric_platform=0,material_data_source=6))
        with store.connect() as conn:
            conn.execute("UPDATE fb_auto_template SET status='enabled'")
            raw=dict(conn.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version").fetchone())
            manifest=build_manifest(conn,raw,[PageTarget("1",("1",),"12345","12","UTC","en",1)],
                {"555":dict(MATERIAL,series_code="unrelated-internal-series")},["555"],"test-batch",
                waived_series="old-DW-series",waive_drama_cooldown=False,now=NOW)
        with patch.object(moboreels.macro_client,"resolve_m_url",side_effect=AssertionError("reservation must not network")):
            reserve_batch(store,manifest,max_jobs=10,max_daily_jobs=10,automatic_daily_jobs=0)
        with store.connect() as conn:task=dict(conn.execute("SELECT * FROM fb_auto_task").fetchone())
        self.assertEqual(json.loads(task["moboreels_json"])["creator"]["user_id"],"creator")
        self.assertEqual(task["short_url"],"")
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",return_value={"user_id":"creator"}),patch.object(moboreels.macro_client,"resolve_m_url",return_value=LINK) as resolve:
            frozen=auto.ensure_link(store,task)
            auto.ensure_link(store,frozen)
        self.assertEqual(resolve.call_count,1)


@unittest.skipUnless(os.environ.get("FB_TEST_OVERLAY") == "1", "native dispatch belongs to the fetched live runtime")
class NativeBridgeTests(unittest.TestCase):
    setUp=VideoTests.setUp
    freeze=VideoTests.freeze
    worker=VideoTests.worker
    remote=VideoTests.remote

    def bridge(self):
        from features.pipeline_dispatch.fb_native import FBNativeBridge
        worker=self.worker()
        runtime=SimpleNamespace(store=self.store,manual_worker=worker,executor=SimpleNamespace(live_enabled=True))
        return FBNativeBridge(runtime)

    def test_native_video_preparation_and_publish_descriptor_use_frozen_m_url(self):
        task=self.freeze("random_overlay")
        bridge=self.bridge()
        job=dict(channel="fb",family="video",source_id=task["task_id"],source_key="fb:video:"+task["task_id"],stage="prepare",generation=1,executor_backend="cpu")
        claim=dict(claim_token="a"*32,node_id="test")
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",return_value={"user_id":"creator"}),patch.object(moboreels.macro_client,"resolve_m_url",side_effect=self.remote) as resolve:
            prepared=bridge.reserve(job,claim)
            self.assertEqual(prepared["kind"],"fb_prepare")
            self.assertEqual(bridge.reserve(job,claim),prepared)
            self.assertEqual(resolve.call_count,1)
        with self.store.connect() as conn:
            frozen=json.loads(conn.execute("SELECT task_json FROM pipeline_fb_claim").fetchone()[0])
        self.assertEqual(json.loads(frozen["snapshot_json"])["moboreels"]["link"],LINK)
        self.store.finish_stage(frozen,"prepare",dict(media_url="https://cdn.example/prepared.mp4",sha256="a"*64,profile="cpu",random_overlay_recipe={"a":1}))
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY), patch.object(moboreels.macro_client,"resolve_m_url",side_effect=AssertionError("must reuse")):
            publication=bridge.reserve(dict(job,stage="publish",generation=2),dict(claim,claim_token="b"*32))
        self.assertIn(LINK["short_url"],publication["request"]["message"])
        bridge.runtime.manual_worker.link_writer.assert_not_called()
        bridge.runtime.manual_worker.readback.assert_not_called()

    def test_native_unknown_never_emits_upload_descriptor(self):
        task=self.freeze("random_overlay")
        bridge=self.bridge()
        job=dict(channel="fb",family="video",source_id=task["task_id"],source_key="fb:video:"+task["task_id"],stage="prepare",generation=1,executor_backend="cpu")
        with patch.object(moboreels.macro_client,"lookup_story",return_value=STORY),patch.object(moboreels.macro_client,"resolve_actor",return_value={"user_id":"creator"}),patch.object(moboreels.macro_client,"resolve_m_url",side_effect=moboreels.macro_client.MoboreelsLinkError("结果未知")):
            with self.assertRaises(moboreels.MoboreelsFBError):bridge.reserve(job,dict(claim_token="a"*32,node_id="test"))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_fb_claim").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT status FROM fb_video_task").fetchone()[0],"failed")


if __name__=="__main__":unittest.main()

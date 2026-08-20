import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.gpu import GPUPrepareClient, GPUPrepareError, PROFILE, PrepareExecutor
from features.fb_auto_posts.metrics import MetricRefresher
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageTarget
from features.fb_auto_posts.service import Runtime, ServiceError, build_runtime
from features.fb_auto_posts.validation import normalize_template_payload
from scripts.test_fb_auto_validation import payload


UTC = timezone.utc


class Clock:
    def __init__(self, value): self.value = value
    def __call__(self): return self.value


class MetricAndScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.clock = Clock(datetime(2026,8,18,2,0,tzinfo=UTC))
        self.store = FBAutoPostStore(Path(self.tmp.name)/"state.sqlite3", now_fn=self.clock)
    def tearDown(self): self.tmp.cleanup()

    def test_metric_window_ratio_of_sums_and_generation_freeze(self):
        one = self.store.record_metric_generation(platform=0,metric_date="2026-08-16",product="Dramawave",rows=[{"content_id":"d1","material_id":"1","spend":"1","af_revenue0":"1"}],refreshed_at_utc="2026-08-17T00:00:00+00:00")
        two = self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[{"content_id":"d1","material_id":"1","spend":"9","af_revenue0":"0"}],refreshed_at_utc="2026-08-18T00:00:00+00:00")
        window = self.store.load_metric_window(product="Dramawave",platform=0,dates=["2026-08-16","2026-08-17"])
        self.assertEqual(window.generation_ids,(one["id"],two["id"])); self.assertEqual(window.by_material[("d1","1")].roas,Decimal("10"))

    def test_missing_ready_day_fails_closed(self):
        with self.assertRaises(StoreError) as caught: self.store.load_metric_window(product="Dramawave",platform=0,dates=["2026-08-17"])
        self.assertEqual(caught.exception.code,"fb_auto_metric_window_not_ready")

    def test_bad_generation_does_not_move_active_pointer(self):
        first=self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[],refreshed_at_utc="2026-08-18T00:00:00+00:00")
        with self.assertRaises(StoreError): self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[{"content_id":"","material_id":"1","spend":0,"af_revenue0":0}],refreshed_at_utc="2026-08-18T01:00:00+00:00")
        self.assertEqual(self.store.load_metric_window(product="Dramawave",platform=0,dates=["2026-08-17"]).generation_ids,(first["id"],))

    def test_older_ready_generation_retry_cannot_roll_back_pointer(self):
        old = self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[],refreshed_at_utc="2026-08-18T00:00:00+00:00")
        new = self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[],refreshed_at_utc="2026-08-18T01:00:00+00:00")
        retried = self.store.record_metric_generation(platform=0,metric_date="2026-08-17",product="Dramawave",rows=[],refreshed_at_utc="2026-08-18T00:00:00+00:00")
        self.assertEqual(retried["id"], old["id"])
        self.assertEqual(self.store.load_metric_window(product="Dramawave",platform=0,dates=["2026-08-17"]).generation_ids,(new["id"],))

    def test_single_day_refresh_sql_is_product_platform_date_scoped(self):
        class MySQL:
            schema="kunlunads_dev"
            def __init__(self): self.calls=[]
            def iter_select(self,sql,params): self.calls.append((sql,params)); return iter([{"content_id":"d1","material_id":"1","spend":1,"af_revenue0":2}])
        mysql=MySQL(); MetricRefresher(mysql,self.store).refresh_day("2026-08-17",refreshed_at=self.clock())
        sql,params=mysql.calls[0]; normalized=" ".join(sql.split())
        self.assertIn("s.dt=%s",sql); self.assertNotIn("s.dt>=",sql)
        self.assertIn("CHAR_LENGTH(TRIM(s.resource_id)) AS material_id_digits",normalized)
        binary_key="HEX(CONVERT(TRIM(s.data_source_id) USING utf8mb4))"
        self.assertIn(f"GROUP BY TRIM(s.data_source_id),{binary_key}, TRIM(s.resource_id),CHAR_LENGTH(TRIM(s.resource_id))",normalized)
        self.assertIn(f"ORDER BY {binary_key}, CHAR_LENGTH(TRIM(s.resource_id)),TRIM(s.resource_id)",normalized)
        self.assertEqual(params,("Dramawave",0,"2026-08-17"))

    def test_metric_refresh_passes_lazy_iterator_to_streaming_writer(self):
        events=[]
        class MySQL:
            schema="kunlunads_dev"
            def iter_select(self,_sql,_params):
                for material in ("1","2","10"): events.append("yield:"+material); yield {"content_id":"d","material_id":material,"spend":1,"af_revenue0":1}
        class Store:
            def record_metric_generation_streaming(self,**kwargs):
                self.rows=kwargs["rows"]; events.append("writer"); self.items=list(self.rows); return {"status":"ready","row_count":len(self.items)}
        store=Store(); result=MetricRefresher(MySQL(),store).refresh_day("2026-08-17",refreshed_at=self.clock())
        self.assertEqual(events[0],"writer"); self.assertEqual([item["material_id"] for item in store.items],["1","2","10"]); self.assertEqual(result["row_count"],3)

    def test_incomplete_same_generation_key_fails_explicitly(self):
        import hashlib, json
        refreshed="2026-08-18T00:00:00+00:00"; identity=json.dumps([0,"2026-08-17","Dramawave",refreshed],separators=(",",":")); key="fb-auto-metric-v1-"+hashlib.sha256(identity.encode()).hexdigest()
        with self.store.connect() as conn: conn.execute("INSERT INTO fb_auto_metric_generation(generation_key,platform,metric_date,product,status,row_count,checksum,refreshed_at_utc,created_at_utc) VALUES(?,0,'2026-08-17','Dramawave','building',0,'',?,?)",(key,refreshed,refreshed))
        with self.assertRaises(StoreError) as caught: self.store.record_metric_generation_streaming(platform=0,metric_date="2026-08-17",product="Dramawave",rows=iter(()),refreshed_at_utc=refreshed)
        self.assertEqual(caught.exception.code,"fb_auto_metric_generation_incomplete")

    def test_streaming_metric_writer_rejects_lexical_1_10_2_order(self):
        rows = (
            {"content_id":"d","material_id":material,"spend":1,"af_revenue0":1}
            for material in ("1","10","2")
        )
        with self.assertRaises(StoreError) as caught:
            self.store.record_metric_generation_streaming(
                platform=0,
                metric_date="2026-08-17",
                product="Dramawave",
                rows=rows,
                refreshed_at_utc="2026-08-18T03:00:00+00:00",
            )
        self.assertEqual(caught.exception.code,"fb_auto_metric_row_invalid")

    def test_streaming_metric_writer_uses_binary_content_order(self):
        rows = iter((
            {"content_id":"Z","material_id":"1","spend":1,"af_revenue0":1},
            {"content_id":"Z","material_id":"2","spend":1,"af_revenue0":1},
            {"content_id":"Z","material_id":"10","spend":1,"af_revenue0":1},
            {"content_id":"Z","material_id":"999999999999999999999999","spend":1,"af_revenue0":1},
            {"content_id":"a","material_id":"1","spend":1,"af_revenue0":1},
        ))
        result = self.store.record_metric_generation_streaming(
            platform=0, metric_date="2026-08-17", product="Dramawave", rows=rows,
            refreshed_at_utc="2026-08-18T04:00:00+00:00",
        )
        self.assertEqual(result["row_count"],5)

    def test_streaming_metric_writer_rejects_noncanonical_material_ids(self):
        for offset, material_id in enumerate(("01","١"),5):
            with self.subTest(material_id=material_id), self.assertRaises(StoreError) as caught:
                self.store.record_metric_generation_streaming(
                    platform=0, metric_date="2026-08-17", product="Dramawave",
                    rows=iter(({"content_id":"d","material_id":material_id,"spend":1,"af_revenue0":1},)),
                    refreshed_at_utc=f"2026-08-18T0{offset}:00:00+00:00",
                )
            self.assertEqual(caught.exception.code,"fb_auto_metric_row_invalid")

    def test_streaming_metric_failure_rolls_back_and_preserves_active_pointer(self):
        ready = self.store.record_metric_generation(
            platform=0, metric_date="2026-08-17", product="Dramawave", rows=[],
            refreshed_at_utc="2026-08-18T02:00:00+00:00",
        )
        rows = iter((
            {"content_id":"d","material_id":"1","spend":1,"af_revenue0":1},
            {"content_id":"d","material_id":"1","spend":1,"af_revenue0":1},
        ))
        with self.assertRaises(StoreError) as caught:
            self.store.record_metric_generation_streaming(
                platform=0, metric_date="2026-08-17", product="Dramawave", rows=rows,
                refreshed_at_utc="2026-08-18T08:00:00+00:00",
            )
        self.assertEqual(caught.exception.code,"fb_auto_metric_row_invalid")
        window = self.store.load_metric_window(product="Dramawave",platform=0,dates=["2026-08-17"])
        self.assertEqual(window.generation_ids,(ready["id"],))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_metric_generation WHERE status='building'").fetchone()[0],0)

    def test_scheduler_persists_future_prepare_window_and_is_idempotent(self):
        actor=ActorScope("u","n",False,"248"); raw=payload(); raw["schedule"]={"mode":"fixed","times":["10:30"]}
        template=self.store.create_template(raw,actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        self.store.set_template_status(template["id"],True,actor,template["version"])
        first=self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=14400)
        second=self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=14400)
        self.assertEqual(first["enqueued"],1); self.assertEqual(second["enqueued"],0)
        with self.store.connect() as conn:
            row=conn.execute("SELECT planned_publish_at_utc,status FROM fb_auto_due_slot").fetchone()
        self.assertEqual(row["planned_publish_at_utc"],"2026-08-18T02:30:00+00:00"); self.assertEqual(row["status"],"pending")

    def test_scheduler_restart_catches_up_and_records_old_gap(self):
        actor=ActorScope("u","n",False,"248"); raw=payload(); raw["schedule"]={"mode":"fixed","times":["10:30"]}
        template=self.store.create_template(raw,actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0}); self.store.set_template_status(template["id"],True,actor,1)
        self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=3600)
        later=self.clock.value+timedelta(hours=8); result=self.store.enqueue_due_slots(live_enabled=True,at=later,prepare_ahead_seconds=3600,max_catchup_minutes=180)
        self.assertGreaterEqual(result["missed"],1)

    def test_disabled_template_invalidates_already_planned_due_slot(self):
        actor=ActorScope("u","n",False,"248"); raw=payload(); raw["schedule"]={"mode":"fixed","times":["10:30"]}
        template=self.store.create_template(raw,actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0}); self.store.set_template_status(template["id"],True,actor,1)
        self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=14400)
        self.store.set_template_status(template["id"],False,actor,1)
        class Executor: live_enabled=True
        runtime=Runtime(self.store,object(),object(),Executor(),object(),"x"*32)
        result=runtime.plan_next("worker")
        self.assertEqual(result["error"],"fb_auto_due_slot_template_changed")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM fb_auto_due_slot").fetchone()[0],"failed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0],0)

    def test_future_due_slot_for_new_template_version_coexists_with_old_version(self):
        actor=ActorScope("u","n",False,"248"); raw=payload(); raw["schedule"]={"mode":"fixed","times":["10:30"]}
        template=self.store.create_template(raw,actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        self.store.set_template_status(template["id"],True,actor,1)
        self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=14400)
        self.store.set_template_status(template["id"],False,actor,1)
        raw["name"]="v2"
        updated=self.store.update_template(template["id"],raw,actor,1,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        self.store.set_template_status(template["id"],True,actor,updated["version"])
        self.store.enqueue_due_slots(live_enabled=True,at=self.clock(),prepare_ahead_seconds=14400)
        with self.store.connect() as conn:
            rows=conn.execute("SELECT template_version,slot_key FROM fb_auto_due_slot WHERE template_id=? ORDER BY template_version",(template["id"],)).fetchall()
        self.assertEqual([row["template_version"] for row in rows],[1,2])
        self.assertTrue(rows[0]["slot_key"].startswith("auto:v1:")); self.assertTrue(rows[1]["slot_key"].startswith("auto:v2:"))

    def test_gate_zero_creates_no_slot_or_watermark(self):
        result=self.store.enqueue_due_slots(live_enabled=False,at=self.clock())
        self.assertEqual(result["status"],"live_gate_closed")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_due_slot").fetchone()[0],0); self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_scheduler_state").fetchone()[0],0)

    def test_stable_gpu_job_is_different_per_page(self):
        actor=ActorScope("u","n",False,"248"); template=self.store.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        class Pages:
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","english",1),PageTarget("6",("6",),"10002","248","UTC","english",1)]
        material=MaterialCandidate("1","d1","https://cdn.example/source.mp4","m","d","english",Decimal("30"),Decimal("1"),Decimal("1"),Decimal("1"),Decimal("1"),"1")
        class Materials:
            def candidate_snapshot(self,_config): return CandidateSnapshot((material,),(11,),("2026-08-17",))
            def choose_from(self,items,_excluded): return items[0]
        run=self.store.create_run(template["id"],"manual:stable","manual",actor,Pages(),Materials())
        with self.store.connect() as conn: jobs=[row[0] for row in conn.execute("SELECT gpu_job_id FROM fb_auto_task WHERE run_id=? ORDER BY page_id",(run["run_id"],))]
        self.assertEqual(len(set(jobs)),2); self.assertTrue(all(job.startswith("fb-page-") for job in jobs))

    def test_capacity_gate_reports_real_counts(self):
        actor=ActorScope("u","n",False,"248"); template=self.store.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        for offset in range(1,8): self.store.record_metric_generation(platform=0,metric_date=(self.clock().astimezone(timezone(timedelta(hours=8))).date()-timedelta(days=offset)).isoformat(),product="Dramawave",rows=[],refreshed_at_utc=f"2026-08-{18-offset:02d}T00:00:00+00:00")
        class Pages:
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","english",1),PageTarget("6",("6",),"10002","248","UTC","english",1)]
        class Executor: live_enabled=False
        runtime=Runtime(self.store,Pages(),object(),Executor(),object(),"x"*32,max_daily_jobs=100,max_publishable_pages=10,max_jobs_per_slot=1)
        with self.assertRaises(ServiceError) as caught: runtime.validate_activation(template)
        self.assertEqual(caught.exception.code,"fb_auto_capacity_exceeded"); self.assertIn("全局最坏同槽GPU任务 2/1",str(caught.exception))

    def test_capacity_gate_sums_all_enabled_templates_in_same_slot(self):
        actor=ActorScope("u","n",False,"248")
        candidate=self.store.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        other_raw=payload(); other_raw["name"]="other"; other_raw["group_ids"]=["18"]
        other=self.store.create_template(other_raw,actor,{"app_id":"1479","product":"Dramawave","material_data_source":6,"metric_product":"Dramawave","metric_platform":0})
        self.store.set_template_status(other["id"],True,actor,other["version"])
        for offset in range(1,8):
            day=(self.clock().astimezone(timezone(timedelta(hours=8))).date()-timedelta(days=offset)).isoformat()
            self.store.record_metric_generation(platform=0,metric_date=day,product="Dramawave",rows=[],refreshed_at_utc=f"2026-08-{18-offset:02d}T00:00:00+00:00")
        class Pages:
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,group_ids,**_kwargs):
                group=str(group_ids[0]); base=10000 if group=="6" else 20000
                return [PageTarget(group,(group,),str(base+i),"248","UTC","en",1) for i in range(15)]
        class Executor: live_enabled=False
        runtime=Runtime(self.store,Pages(),object(),Executor(),object(),"x"*32,max_daily_jobs=1000,max_publishable_pages=100,max_jobs_per_slot=20)
        with self.assertRaises(ServiceError) as caught: runtime.validate_activation(candidate)
        self.assertEqual(caught.exception.code,"fb_auto_capacity_exceeded"); self.assertIn("30/20",str(caught.exception))

    def test_operational_and_metric_paths_must_be_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            same=str(Path(tmp)/"same.sqlite3")
            env={"FB_AUTO_POST_INTERNAL_TOKEN":"x"*32,"FB_AUTO_POST_DB_PATH":same,"FB_AUTO_METRIC_DB_PATH":same}
            with self.assertRaisesRegex(ValueError,"independent"):
                build_runtime(env)

    def test_short_link_root_must_be_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            env={"FB_AUTO_POST_INTERNAL_TOKEN":"x"*32,"FB_AUTO_GPU_PREPARE_INTERNAL_TOKEN":"y"*32,"FB_AUTO_POST_DB_PATH":str(Path(tmp)/"post.sqlite3"),"FB_AUTO_METRIC_DB_PATH":str(Path(tmp)/"metric.sqlite3"),"FB_AUTO_POST_SHORT_LINK_ROOT":"relative/fb"}
            with self.assertRaisesRegex(ValueError,"SHORT_LINK_ROOT"):
                build_runtime(env)

    def test_metric_streaming_write_does_not_lock_operational_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            operational=FBAutoPostStore(Path(tmp)/"operational.sqlite3",now_fn=self.clock)
            metrics=FBAutoPostStore(Path(tmp)/"metrics.sqlite3",now_fn=self.clock)
            entered=threading.Event(); release=threading.Event(); errors=[]
            def rows():
                entered.set(); release.wait(2)
                yield {"content_id":"d","material_id":"1","spend":1,"af_revenue0":1}
            def writer():
                try: metrics.record_metric_generation_streaming(platform=0,metric_date="2026-08-17",product="Dramawave",rows=rows(),refreshed_at_utc="2026-08-18T00:00:00+00:00")
                except Exception as exc: errors.append(exc)
            thread=threading.Thread(target=writer); thread.start(); self.assertTrue(entered.wait(1))
            actor=ActorScope("u","n",False,"248")
            created=operational.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave"})
            release.set(); thread.join(2)
            self.assertFalse(thread.is_alive()); self.assertFalse(errors); self.assertEqual(created["id"],1)

    def test_gpu_transient_failure_is_deferred_with_backoff_then_retried(self):
        actor=ActorScope("u","n",False,"248"); template=self.store.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave"})
        class Pages:
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","en",1)]
        material=MaterialCandidate("1","d1","https://cdn.example/source.mp4","m","d","en",Decimal("30"),Decimal("1"),Decimal("1"),Decimal("1"),Decimal("1"),"1")
        class Materials:
            def candidate_snapshot(self,_config): return CandidateSnapshot((material,),(11,),("2026-08-17",))
            def choose_from(self,items,_excluded): return items[0]
        self.store.create_run(template["id"],"manual:gpu-retry","manual",actor,Pages(),Materials())
        class GPU:
            def __init__(self): self.calls=0
            def prepare(self,**_kwargs):
                self.calls+=1
                if self.calls==1: raise GPUPrepareError("fb_gpu_cos_upload_failed","COS结果未知",502)
                return {"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":PROFILE}
        gpu=GPU(); executor=PrepareExecutor(self.store,gpu,live_enabled=True)
        first=executor.prepare_next("prepare")
        self.assertEqual(first["status"],"planned"); self.assertIsNone(self.store.claim_prepare_next("too-soon"))
        self.clock.value += timedelta(minutes=5)
        second=executor.prepare_next("prepare")
        self.assertEqual(second["status"],"ready"); self.assertEqual(gpu.calls,2)

    def test_gpu_identity_error_is_terminal(self):
        actor=ActorScope("u","n",False,"248"); template=self.store.create_template(payload(),actor,{"app_id":"1479","product":"Dramawave"})
        class Pages:
            def legacy_conflicts(self,_ids): return []
            def list_pages(self,*_args,**_kwargs): return [PageTarget("6",("6",),"10001","248","UTC","en",1)]
        material=MaterialCandidate("1","d1","https://cdn.example/source.mp4","m","d","en",Decimal("30"),Decimal("1"),Decimal("1"),Decimal("1"),Decimal("1"),"1")
        class Materials:
            def candidate_snapshot(self,_config): return CandidateSnapshot((material,),(11,),("2026-08-17",))
            def choose_from(self,items,_excluded): return items[0]
        self.store.create_run(template["id"],"manual:gpu-terminal","manual",actor,Pages(),Materials())
        class GPU:
            def prepare(self,**_kwargs): raise GPUPrepareError("fb_auto_prepared_identity_mismatch","不一致",502)
        result=PrepareExecutor(self.store,GPU(),live_enabled=True).prepare_next("prepare")
        self.assertEqual(result["status"],"failed")


class GPUClientTests(unittest.TestCase):
    class Response:
        status=200
        def __init__(self,payload): self.payload=payload
        def read(self,_limit): return __import__("json").dumps(self.payload).encode()
    class Connection:
        def __init__(self,payload): self.payload=payload; self.request_data=None
        def request(self,*args,**kwargs): self.request_data=(args,kwargs)
        def getresponse(self): return GPUClientTests.Response(self.payload)
        def close(self): pass
    def item(self,**changes):
        value={"job_id":"fb-page-"+"a"*48,"content_id":"d1","profile":PROFILE,"output_url":"https://cdn.example/prepared.mp4","output_sha256":"b"*64,"output_size":123,"probe":{"duration":30}}
        value.update(changes); return value
    def test_strict_prepared_contract(self):
        connection=self.Connection({"item":self.item()}); client=GPUPrepareClient("x"*32,connection_factory=lambda *_:connection)
        result=client.prepare(job_id="fb-page-"+"a"*48,content_id="d1",source_url="https://cdn.example/source.mp4",video_template="random_overlay")
        self.assertEqual(result["profile"],PROFILE); self.assertIn("/internal/fb-page-media/prepare",connection.request_data[0])
    def test_source_url_output_is_rejected(self):
        connection=self.Connection({"item":self.item(output_url="https://cdn.example/source.mp4")}); client=GPUPrepareClient("x"*32,connection_factory=lambda *_:connection)
        with self.assertRaises(GPUPrepareError): client.prepare(job_id="fb-page-"+"a"*48,content_id="d1",source_url="https://cdn.example/source.mp4",video_template="random_overlay")
    def test_wrong_profile_is_rejected(self):
        connection=self.Connection({"item":self.item(profile="tt-profile")}); client=GPUPrepareClient("x"*32,connection_factory=lambda *_:connection)
        with self.assertRaises(GPUPrepareError): client.prepare(job_id="fb-page-"+"a"*48,content_id="d1",source_url="https://cdn.example/source.mp4",video_template="random_overlay")


if __name__ == "__main__": unittest.main()

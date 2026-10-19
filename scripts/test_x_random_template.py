"""Offline configuration, frozen media and GPU-receipt regressions."""
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from features.x_posts import service, random_template as rt, random_template_gpu as gpu, media_repair
from features.x_posts.random_template_store import PreparationStore
from scripts.test_x_post_priority_manual_store import material_candidate, ACTOR
from scripts.test_x_post_media_repair import make_config, FakeCosClient, FakeRunner, source_probe


class RandomTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "posts.sqlite3"
        self.store = service.XPostStore(self.db)
        self.source, self.output = b"source video", b"random video"
        self.sha = lambda b: hashlib.sha256(b).hexdigest()

    def manual_queue(self, enabled=True, duration=30):
        run = self.store.create_manual_run(["501"], [2], "random-test-501", ACTOR, random_template_enabled=enabled)
        self.store.claim_manual_run()
        candidate = material_candidate(run, 2, "501")
        candidate.update(preflight_sha256=self.sha(self.source), preflight_size=len(self.source), preflight_duration=duration)
        self.store.create_manual_plan(run["id"], [candidate])
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            qid = c.execute("SELECT id FROM x_post_queue").fetchone()[0]
        return self.store.get_queue(qid)

    def media(self, body, path, kind="video"):
        Path(path).write_bytes(body)
        return {"path": Path(path), "sha256": self.sha(body), "size": len(body),
                "media_kind": kind, "media_type": "video/mp4" if kind == "video" else "image/jpeg"}

    def prepare(self, queue, client=None, **kw):
        def download(url, path, hosts, **kwargs):
            return self.media(self.output if url.endswith("output.mp4") else self.source, path)
        def probe(path, **kwargs):
            return {"duration": float(queue["preflight_duration"]), "width": 720, "height": 1280}
        values = dict(queue=queue, log=self.store.reserve_log(queue["id"]), account={"id": 2}, db_path=self.db,
                      public_root=self.root / "public", allowed_media_hosts=("media.example.test",),
                      environ={"X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256": "a" * 64},
                      downloader=download, prober=probe, client=client or self.client())
        values.update(kw)
        return rt.prepare_random_template(**values)

    def client(self, mutate=None):
        def prepare(request):
            result = {k: request[k] for k in ("queue_id", "job_key", "profile", "source_sha256", "source_size", "asset_manifest_sha256")}
            result.update(output_url="https://media.example.test/output.mp4", output_sha256=self.sha(self.output),
                          output_size=len(self.output), probe={"duration": request["source_duration"]},
                          recipe={"asset_set_sha256": "a" * 64})
            if mutate:mutate(result)
            return result
        return SimpleNamespace(prepare=mock.Mock(side_effect=prepare))

    def test_manual_flag_defaults_and_idempotency_and_queue_freeze(self):
        queue = self.manual_queue()
        self.assertIs(queue["random_template_enabled"], True)
        run = self.store.get_manual_run(queue["manual_run_id"])
        self.assertIs(run["random_template_enabled"], True)
        with self.assertRaises(service.XPostError):
            self.store.create_manual_run(["501"], [2], "random-test-501", ACTOR, random_template_enabled=False)
        with contextlib.closing(sqlite3.connect(self.db)) as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE x_post_queue SET random_template_enabled=0 WHERE id=?", (queue["id"],))
        service.ensure_storage(self.db)
        service.ensure_storage(self.db)
        self.assertIs(self.store.get_queue(queue["id"])["random_template_enabled"], True)
        self.assertIs(self.store.get_schedule_config("material")["random_template_enabled"], False)
        self.assertIs(self.store.get_schedule_config("drama")["random_template_enabled"], False)

    def test_auto_bridge_flag_and_mismatched_replay(self):
        args = ("601", 3, "task-601", "template-1", 1, "{{drama_name}}\n{{desc}}", ACTOR)
        run = self.store.create_auto_template_run(*args, random_template_enabled=True)
        self.assertTrue(run["random_template_enabled"])
        self.store.claim_manual_run("auto_template")
        self.store.create_manual_plan(run["id"], [material_candidate(run, 3, "601")], trigger_source="auto_template")
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT random_template_enabled FROM x_post_queue").fetchone()[0], 1)
        with self.assertRaises(service.XPostError):
            self.store.create_auto_template_run(*args)

    def test_strict_boolean_rejects_strings_and_numbers(self):
        for invalid in ("false", "true", 0, 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(service.XPostError):
                self.store.create_manual_run(["501"], [2], "bad-flag", ACTOR, random_template_enabled=invalid)

    def test_render_reuse_and_final_byte_integrity(self):
        queue = self.manual_queue()
        client = self.client()
        with self.prepare(queue, client) as prepared:
            self.assertEqual(prepared.for_queue(queue, service.DEFAULT_MAX_MEDIA_BYTES)["sha256"], self.sha(self.output))
            Path(prepared.media["path"]).write_bytes(b"tampered")
            with self.assertRaises(service.XPostError):prepared.for_queue(queue, service.DEFAULT_MAX_MEDIA_BYTES)
        with self.prepare(queue, client, environ={"X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256": "b"*64}) as prepared:
            self.assertIsInstance(prepared, rt.PreparedRandomTemplateMedia)
        self.assertEqual(client.prepare.call_count, 1)
        self.assertEqual(PreparationStore(self.db).get(queue["id"])["status"], "ready")

    def test_bad_receipt_stops_and_never_returns_original(self):
        queue = self.manual_queue()
        client = self.client(lambda r:r.update(output_sha256="f"*64))
        with self.assertRaises(service.XPostError):
            with self.prepare(queue, client):self.fail("invalid output was accepted")
        self.assertEqual(PreparationStore(self.db).get(queue["id"])["status"], "failed")
        self.assertEqual(self.store.get_log(self.store.reserve_log(queue["id"])["id"])["attempt_count"], 0)

    def test_disabled_and_image_skip_gpu(self):
        queue = self.manual_queue(False)
        client = self.client()
        with self.prepare(queue, client) as result:self.assertIsNone(result)
        client.prepare.assert_not_called()
        queue = dict(queue, random_template_enabled=True, preflight_duration=0)
        def image_download(url, path, hosts, **kwargs):return self.media(self.source,path,"image")
        with self.prepare(queue, client, downloader=image_download) as result:self.assertIsNone(result)
        client.prepare.assert_not_called()

    def test_prior_attempt_blocks_preparation(self):
        queue = self.manual_queue()
        client = self.client()
        log = {**self.store.reserve_log(queue["id"]), "attempt_count": 1}
        with self.assertRaises(service.XPostError):
            with self.prepare(queue, client, log=log):pass
        client.prepare.assert_not_called()

    def test_fixed_and_random_schedule_freeze_flag(self):
        for mode in ("fixed", "random"):
            with self.subTest(mode=mode):
                db=self.root/(mode+".sqlite3");store=service.XPostStore(db)
                now=datetime(2026,10,19,8,tzinfo=service.BEIJING_TZ)
                cfg=store.save_schedule_config("material", {"enabled":True,"account_ids":[2],"publish_times":["09:00"] if mode=="fixed" else [],
                    "schedule_mode":mode,"random_daily_count":1 if mode=="random" else 0,"version":1,"random_template_enabled":True}, eligible_account_ids=[2],now=now)
                date="2026-10-19" if mode=="fixed" else "2026-10-20"
                if mode=="random":
                    store.ensure_random_schedule_plans([date])
                    with contextlib.closing(sqlite3.connect(db)) as c:
                        row=c.execute("SELECT random_template_enabled,publish_times_json FROM x_post_schedule_random_plan WHERE source_type='material' AND run_date=?",(date,)).fetchone()
                    self.assertEqual(row[0],1);time=json.loads(row[1])[0]
                else:time="09:00"
                store.due_schedule_slots(datetime.fromisoformat(date+"T"+time+":00+08:00"))
                run=store.query_schedule_plan("material",date,time)["run"]
                self.assertIs(run["random_template_enabled"],True)
                # Later config edits cannot alter the claimed snapshot.
                cfg=store.save_schedule_config("material", {"enabled":True,"account_ids":[2],"publish_times":["09:00"] if mode=="fixed" else [],
                    "schedule_mode":mode,"random_daily_count":1 if mode=="random" else 0,"version":cfg["version"],"random_template_enabled":False}, eligible_account_ids=[2],now=now)
                self.assertFalse(cfg["random_template_enabled"])
                self.assertTrue(store.query_schedule_plan("material",date,time)["run"]["random_template_enabled"])


    def test_random_video_upload_and_post_are_simulated_once(self):
        from scripts.test_x_posts import ScriptedHttpClient, response
        queue = self.manual_queue()
        http = ScriptedHttpClient([response(200, {"data":{"id":"media1"}}),
            response(200,{"data":{"expires_at":1}}),response(200,{"data":{"id":"media1"}}),
            response(201,{"data":{"id":"190002","text":"ok"}})])
        with self.prepare(queue) as prepared, mock.patch.object(service,"probe_media",return_value={"duration":30.,"width":720,"height":1280}):
            kwargs=dict(db_path=self.db,queue_id=queue["id"],account={"id":2,"username":queue["account_username"]},
                access_token="fixture",public_root=self.root/"public",short_base_url="https://gy.g2flow.com/s2l",
                allowed_media_hosts=["media.example.test"],http_client=http,prepared_media=prepared,sleeper=lambda s:None)
            result=service.publish_canary(**kwargs)
            self.assertEqual(result["status"],"published")
            count=len(http.requests)
            self.assertEqual(service.publish_canary(**kwargs)["post_id"],"190002")
            self.assertEqual(len(http.requests),count)
        self.assertEqual(len([r for r in http.requests if r["url"].endswith("/tweets")]),1)
        self.assertTrue(any(self.output in (r["body"] or b"") for r in http.requests if isinstance(r["body"],bytes)))

    def test_gpu_timeout_never_reaches_upload(self):
        queue=self.manual_queue()
        client=SimpleNamespace(prepare=mock.Mock(side_effect=TimeoutError("GPU timeout")))
        with self.assertRaises(service.XPostError):
            with self.prepare(queue,client):self.fail("timeout produced media")
        self.assertEqual(PreparationStore(self.db).get(queue["id"])["status"],"failed")
        self.assertEqual(self.store.reserve_log(queue["id"])["attempt_count"],0)

    def test_auto_600_second_limit_and_relay_source_identity(self):
        queue=self.manual_queue(duration=601)
        premium={"id":2,"subscription_type":"premium","long_video_publish_eligible":True,"protected":False}
        with self.assertRaises(service.XPostError) as error:
            with self.prepare(queue,account=premium,auto_template=True):pass
        self.assertEqual(error.exception.code,"x_auto_duration_out_of_range")
        with self.prepare(queue,account=premium) as prepared:
            self.assertIsInstance(prepared,rt.PreparedRandomTemplateMedia)
        relay={**queue,"delivery_mode":service.PREMIUM_RELAY_REPOST_MODE,"relay_account_id":12}
        client=self.client()
        with self.assertRaises(service.XPostError):
            with self.prepare(relay,client,account={"id":2}):pass
        client.prepare.assert_not_called()


class GPUContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=make_config(self.temp.name)
        self.request={"queue_id":1,"job_key":"x-random-v1-"+"b"*64,"content_id":"drama-1",
            "source_url":"https://media.example.com/source.mp4","source_sha256":"c"*64,"source_size":100,
            "source_duration":30.0,"profile":gpu.PROFILE,"asset_manifest_sha256":"a"*64,"max_duration_seconds":140}

    def test_long_video_contract_and_no_silent_trimming(self):
        for duration,limit in ((139.9,140),(600,600),(7200,14400),(14400,14400)):
            request={**self.request,"source_duration":duration,"max_duration_seconds":limit}
            self.assertEqual(gpu.validate_request(request,self.config)["source_duration"],duration)
        with self.assertRaises(media_repair.MediaRepairError):
            gpu.validate_request({**self.request,"source_duration":141},self.config)

    def test_bad_host_profile_and_boolean_identity_rejected(self):
        for fields in ({"source_url":"https://attacker.test/a.mp4"},{"profile":"fb-random"},{"queue_id":True},{"source_size":True}):
            with self.subTest(fields=fields),self.assertRaises(media_repair.MediaRepairError):
                gpu.validate_request({**self.request,**fields},self.config)


    def test_frozen_recipe_reuse_timeout_and_source_change(self):
        import threading
        source=b"source";self.request.update(source_sha256=hashlib.sha256(source).hexdigest(),source_size=len(source))
        cos=FakeCosClient()
        def download(url,path,*args,**kwargs):
            Path(path).write_bytes(source)
            return {"sha256":hashlib.sha256(source).hexdigest(),"size":len(source)}
        base=media_repair.MediaRepairProcessor(self.config,cos_client=cos,runner=FakeRunner(source_probe(duration="30")),downloader=download)
        assets={"manifest_sha256":"a"*64,"categories":{c:({"name":c+".png","sha256":"d"*64,"size":1,"media_type":"image/png","path":Path(self.temp.name)/(c+".png")},) for c in ("border","opacity_video","corners","tint")}}
        worker=gpu.RandomTemplateProcessor(base,asset_root=self.temp.name,manifest_sha256="a"*64,assets=assets)
        recipes=[]
        def render(source,output,info,recipe,paths):
            self.assertTrue(base._gpu_slot.locked())
            recipes.append(recipe)
            if len(recipes)==1:raise TimeoutError("GPU timeout")
            Path(output).write_bytes(b"rendered")
        worker._render=render
        with mock.patch.object(media_repair,"_job_flock",side_effect=lambda p:contextlib.nullcontext()),mock.patch.object(gpu.shutil,"disk_usage",return_value=SimpleNamespace(free=100*1024**3)),mock.patch.object(media_repair,"validate_output",return_value={"duration":30.,"width":720,"height":1280}):
            with self.assertRaises(TimeoutError):worker.prepare(self.request)
            # Use a stable source probe on retry, independent of fake runner's second-probe output.
            worker._info=lambda p:{"duration":30.,"video":{},"has_audio":True}
            first=worker.prepare(self.request)
            self.assertEqual(worker.prepare(self.request),first)
            self.assertEqual(recipes[0],recipes[1])
            self.assertEqual(len(recipes),2)
            self.assertEqual(len(cos.upload_calls),1)
            with self.assertRaises(media_repair.MediaRepairError):worker.prepare({**self.request,"source_sha256":"f"*64})
        self.assertFalse(base._gpu_slot.locked())


if __name__=='__main__':unittest.main()

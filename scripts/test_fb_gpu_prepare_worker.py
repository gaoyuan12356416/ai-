import ast, http.client, json, os, tempfile, threading, time, unittest
from unittest.mock import patch
from pathlib import Path
from features.fb_gpu.prepare_worker import PROFILE, CosObjectStore, Server, WorkerConfig, PrepareWorkerError, build_command, cleanup_stale_failed_jobs, validate_request
from features.fb_gpu.random_overlay import derive_recipe

def config(root):
    return WorkerConfig("127.0.0.1",8836,"x"*32,root,root,"a"*64,("cdn.example.com",),"id","key","bucket-1","ap-test","https://media.example.com","fb-page-random-overlay-h264-v3","/usr/bin/ffmpeg","/usr/bin/ffprobe")
def request(**changes):
    value={"job_id":"fb-page-"+"a"*48,"content_id":"d1","source_url":"https://cdn.example.com/a.mp4","source_trim_tail_seconds":0,"video_template":"random_overlay","expected_profile":PROFILE}; value.update(changes); return value

class WorkerTests(unittest.TestCase):
    def test_prepare_worker_has_no_tiktok_api_dependency(self):
        source = (Path(__file__).parents[1] / "features" / "fb_gpu" / "prepare_worker.py").read_text(encoding="utf-8").lower()
        tree = ast.parse(source)
        imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any("tiktok" in name for name in imported))
        self.assertNotIn("seal_key", source)

    def test_config_needs_no_tt_or_seal_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env={"FB_PAGE_GPU_HOST":"127.0.0.1","FB_PAGE_GPU_PORT":"8836","FB_PAGE_GPU_INTERNAL_TOKEN":"x"*32,"FB_PAGE_GPU_WORK_ROOT":tmp,"FB_PAGE_GPU_RANDOM_OVERLAY_ROOT":tmp,"FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256":"a"*64,"FB_PAGE_GPU_ALLOWED_SOURCE_HOSTS":"cdn.example.com","FB_PAGE_GPU_COS_SECRET_ID":"id","FB_PAGE_GPU_COS_SECRET_KEY":"key","FB_PAGE_GPU_COS_BUCKET":"bucket-1","FB_PAGE_GPU_COS_REGION":"ap-test","FB_PAGE_GPU_COS_DOMAIN":"https://media.example.com","FB_PAGE_GPU_COS_PREFIX":"fb-page-random-overlay-h264-v3","FB_PAGE_GPU_PROFILE":PROFILE,"FB_PAGE_GPU_VIDEO_ENCODER":"h264_nvenc","FB_PAGE_GPU_PREPARE_ONLY":"1"}
            loaded=WorkerConfig.from_env(env); self.assertEqual(loaded.port,8836); self.assertEqual(loaded.cos_timeout_seconds,120); self.assertEqual(loaded.cos_prefix,"fb-page-random-overlay-h264-v3"); self.assertFalse(any(key.startswith("TT_") for key in env))
    def test_strict_fields_profile_trim_and_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config(Path(tmp)); self.assertEqual(validate_request(request(),cfg)["content_id"],"d1")
            for bad in (request(extra=True),request(expected_profile="wrong"),request(source_trim_tail_seconds=1),request(source_url="https://evil.example/a.mp4")):
                with self.assertRaises(PrepareWorkerError): validate_request(bad,cfg)
    def test_same_job_recipe_is_idempotent_and_other_job_differs(self):
        rows=lambda prefix:({"media_type":"image/png","name":prefix+"1","sha256":"1"*64,"size":1,"path":Path("/tmp/"+prefix+"1")},{"media_type":"image/png","name":prefix+"2","sha256":"2"*64,"size":1,"path":Path("/tmp/"+prefix+"2")})
        assets={"manifest_sha256":"f"*64,"categories":{"border":rows("b"),"opacity_video":rows("o"),"corners":rows("c"),"tint":rows("t")}}
        args={"content_id":"d1","profile":PROFILE,"source_url_sha256":"e"*64,"asset_set":assets}
        one=derive_recipe(job_id="job1",**args); self.assertEqual(one,derive_recipe(job_id="job1",**args)); self.assertNotEqual(one,derive_recipe(job_id="job2",**args))
    def test_command_is_h264_v3_without_publish_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config(Path(tmp)); recipe={"rotation_millidegrees":0,"scale_bp":10000,"tint_opacity_bp":100}; assets={key:Path(tmp)/(key+".bin") for key in ("border","opacity_video","corners","tint")}
            command=build_command(cfg,Path(tmp)/"s.mp4",Path(tmp)/"o.mp4",{"has_audio":True,"duration":30},recipe,assets)
            self.assertEqual(command[command.index("-c:v")+1],"libvpx-vp9"); self.assertIn("h264_nvenc",command); self.assertNotIn("publish", " ".join(command))
    def test_publish_class_routes_are_404_and_no_processor_call(self):
        class Processor:
            def __init__(self): self.calls=0
            def prepare(self,_raw): self.calls+=1; return {}
        processor=Processor(); server=Server(("127.0.0.1",0),processor,"x"*32); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            for path in ("/internal/tt-post/publish","/internal/fb-page-media/publish","/internal/tt-post/creator-info"):
                connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1]); connection.request("POST",path,body=b"{}",headers={"Authorization":"Bearer "+"x"*32,"Content-Length":"2","Content-Type":"application/json"}); response=connection.getresponse(); response.read(); connection.close(); self.assertEqual(response.status,404)
            self.assertEqual(processor.calls,0)
        finally: server.shutdown(); server.server_close(); thread.join(2)
    def test_cos_key_is_idempotent_and_https(self):
        class Missing(Exception):
            def get_status_code(self): return 404
        class Client:
            def __init__(self): self.uploads=0; self.exists=False
            def head_object(self,**_kwargs):
                if not self.exists: raise Missing("missing")
                return {"Content-Length":"3","x-cos-meta-sha256":"b"*64}
            def put_object(self,**kwargs): self.uploads+=1; self.exists=True; self.kwargs=kwargs
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"o.mp4"; path.write_bytes(b"abc"); client=Client(); store=CosObjectStore(config(Path(tmp)),client)
            first=store.upload(path,"fb-page-"+"a"*48,"b"*64,3); second=store.upload(path,"fb-page-"+"a"*48,"b"*64,3)
            self.assertEqual(client.uploads,1); self.assertEqual(first["key"],second["key"]); self.assertTrue(first["key"].startswith("fb-page-random-overlay-h264-v3/bb/")); self.assertTrue(first["url"].startswith("https://")); self.assertTrue(second["reused"]); self.assertEqual(client.kwargs["ACL"],"public-read"); self.assertEqual(client.kwargs["ContentType"],"video/mp4")
            self.assertEqual(client.kwargs["Metadata"],{"sha256":"b"*64,"profile":PROFILE})

    def test_cos_unknown_upload_result_recovers_by_head_on_same_key(self):
        class Missing(Exception):
            def get_status_code(self): return 404
        class Client:
            def __init__(self): self.exists=False; self.uploads=0
            def head_object(self,**_kwargs):
                if not self.exists: raise Missing("missing")
                return {"Content-Length":"3","x-cos-meta-sha256":"b"*64}
            def put_object(self,**_kwargs):
                self.uploads+=1; self.exists=True; raise TimeoutError("unknown completion")
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"o.mp4"; path.write_bytes(b"abc"); client=Client(); store=CosObjectStore(config(Path(tmp)),client)
            with self.assertRaises(PrepareWorkerError) as caught: store.upload(path,"fb-page-"+"a"*48,"b"*64,3)
            self.assertEqual(caught.exception.code,"fb_gpu_cos_upload_failed")
            recovered=store.upload(path,"fb-page-"+"a"*48,"b"*64,3)
            self.assertTrue(recovered["reused"]); self.assertEqual(client.uploads,1)
    def test_cos_403_is_failure_not_missing(self):
        class Forbidden(Exception):
            def get_status_code(self): return 403
        class Client:
            def head_object(self,**_kwargs): raise Forbidden()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"o"; path.write_bytes(b"abc")
            with self.assertRaises(PrepareWorkerError) as caught: CosObjectStore(config(Path(tmp)),Client()).upload(path,"fb-page-"+"a"*48,"b"*64,3)
            self.assertEqual(caught.exception.code,"fb_gpu_cos_head_failed")
    def test_prepare_route_rejects_wrong_content_type_before_processor(self):
        class Processor:
            def __init__(self): self.calls=0
            def prepare(self,_raw): self.calls+=1; return {}
        processor=Processor(); server=Server(("127.0.0.1",0),processor,"x"*32); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1]); connection.request("POST","/internal/fb-page-media/prepare",body=b"{}",headers={"Authorization":"Bearer "+"x"*32,"Content-Type":"text/plain"}); response=connection.getresponse(); response.read(); connection.close(); self.assertEqual(response.status,400); self.assertEqual(processor.calls,0)
        finally: server.shutdown(); server.server_close(); thread.join(2)

    def test_prepare_route_rejects_chunked_body_before_processor(self):
        class Processor:
            def __init__(self): self.calls=0
            def prepare(self,_raw): self.calls+=1; return {}
        processor=Processor(); server=Server(("127.0.0.1",0),processor,"x"*32); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1]); connection.putrequest("POST","/internal/fb-page-media/prepare"); connection.putheader("Authorization","Bearer "+"x"*32); connection.putheader("Content-Type","application/json"); connection.putheader("Transfer-Encoding","chunked"); connection.endheaders(); connection.send(b"2\r\n{}\r\n0\r\n\r\n"); response=connection.getresponse(); response.read(); connection.close(); self.assertEqual(response.status,400); self.assertEqual(processor.calls,0)
        finally: server.shutdown(); server.server_close(); thread.join(2)
    def test_transcode_failure_same_job_retries_without_redownload(self):
        class Response:
            status_code=200
            def iter_content(self,_size): yield b"source"
        class Session:
            trust_env=False
            def __init__(self,owner): self.owner=owner
            def get(self,*_args,**_kwargs): self.owner.downloads+=1; return Response()
            def close(self): pass
        class Storage:
            def upload(self,_path,job,sha,size): return {"url":"https://media.example.com/x.mp4","key":job+sha,"reused":False}
        class Runner:
            def __init__(self): self.calls=0
            def __call__(self,command,**_kwargs):
                self.calls+=1
                if self.calls==1: raise RuntimeError("ffmpeg")
                Path(command[-1]).write_bytes(b"output")
        with tempfile.TemporaryDirectory() as tmp:
            owner=type("Owner",(),{"downloads":0})(); processor=__import__("features.fb_gpu.prepare_worker",fromlist=["PrepareProcessor"]).PrepareProcessor.__new__(__import__("features.fb_gpu.prepare_worker",fromlist=["PrepareProcessor"]).PrepareProcessor)
            processor.config=config(Path(tmp)); processor.assets={"manifest_sha256":"f"*64,"categories":{category:({"media_type":"image/png","name":category,"sha256":"1"*64,"size":1,"path":Path(tmp)/category},) for category in ("border","opacity_video","corners","tint")}}; processor.session_factory=lambda:Session(owner); processor.runner=Runner(); processor.object_store=Storage(); processor.lock=threading.Lock(); (Path(tmp)/"jobs").mkdir()
            probe={"duration":30,"has_audio":True,"video":{"codec_name":"h264","profile":"High","width":720,"height":1280},"audio":{}}
            with patch("features.fb_gpu.prepare_worker._probe",return_value=probe):
                with self.assertRaises(PrepareWorkerError): processor.prepare(request())
                result=processor.prepare(request())
            job_root=Path(tmp)/"jobs"/request()["job_id"]
            self.assertEqual(owner.downloads,1); self.assertEqual(result["status"],"ready"); self.assertTrue((job_root/"manifest.json").is_file()); self.assertFalse((job_root/"source.mp4").exists()); self.assertFalse((job_root/"output.tmp.mp4").exists())

    def test_download_interruption_same_job_retries_cleanly(self):
        class Response:
            status_code=200
            def __init__(self,owner): self.owner=owner
            def iter_content(self,_size):
                yield b"partial"
                if self.owner.calls==1: raise ConnectionError("interrupted")
                yield b"source"
        class Session:
            trust_env=False
            def __init__(self,owner): self.owner=owner
            def get(self,*_args,**_kwargs): self.owner.calls+=1; return Response(self.owner)
            def close(self): pass
        class Storage:
            def upload(self,_path,job,sha,size): return {"url":"https://media.example.com/x.mp4","key":job+sha,"reused":False}
        class Runner:
            def __call__(self,command,**_kwargs): Path(command[-1]).write_bytes(b"output")
        with tempfile.TemporaryDirectory() as tmp:
            owner=type("Owner",(),{"calls":0})(); module=__import__("features.fb_gpu.prepare_worker",fromlist=["PrepareProcessor"]); processor=module.PrepareProcessor.__new__(module.PrepareProcessor)
            processor.config=config(Path(tmp)); processor.assets={"manifest_sha256":"f"*64,"categories":{category:({"media_type":"image/png","name":category,"sha256":"1"*64,"size":1,"path":Path(tmp)/category},) for category in ("border","opacity_video","corners","tint")}}; processor.session_factory=lambda:Session(owner); processor.runner=Runner(); processor.object_store=Storage(); processor.lock=threading.Lock(); (Path(tmp)/"jobs").mkdir()
            probe={"duration":30,"has_audio":True,"video":{"codec_name":"h264","profile":"High","width":720,"height":1280},"audio":{}}
            with patch("features.fb_gpu.prepare_worker._probe",return_value=probe):
                with self.assertRaises(ConnectionError): processor.prepare(request())
                result=processor.prepare(request())
            job_root=Path(tmp)/"jobs"/request()["job_id"]
            self.assertEqual(owner.calls,2); self.assertEqual(result["status"],"ready"); self.assertFalse((job_root/"source.download.tmp").exists())

    def test_cleanup_removes_only_stale_failed_job_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cfg=config(root); jobs=root/"jobs"; jobs.mkdir()
            old=jobs/("fb-page-"+"a"*48); fresh=jobs/("fb-page-"+"b"*48); success=jobs/("fb-page-"+"c"*48); invalid=jobs/"not-a-job"
            for item in (old,fresh,success,invalid): item.mkdir(); (item/"source.mp4").write_bytes(b"x")
            (success/"manifest.json").write_text("{}",encoding="utf-8")
            stale=time.time()-cfg.failed_job_retention_seconds-10
            for path in (old,success,invalid):
                for child in path.iterdir(): os.utime(child,(stale,stale))
                os.utime(path,(stale,stale))
            self.assertEqual(cleanup_stale_failed_jobs(cfg,now_fn=time.time),1)
            self.assertFalse(old.exists()); self.assertTrue(fresh.exists()); self.assertTrue(success.exists()); self.assertTrue(invalid.exists())

    def test_cleanup_never_follows_symlink_outside_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cfg=config(root); jobs=root/"jobs"; jobs.mkdir(); link=jobs/("fb-page-"+"d"*48); link.mkdir(); marker=link/"keep"; marker.write_text("safe",encoding="utf-8")
            original=Path.is_symlink
            with patch.object(Path,"is_symlink",autospec=True,side_effect=lambda value: True if value==link else original(value)):
                cleanup_stale_failed_jobs(cfg,now_fn=lambda:time.time()+cfg.failed_job_retention_seconds+10)
            self.assertTrue(marker.exists())

if __name__=="__main__": unittest.main()

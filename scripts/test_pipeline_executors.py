"""Node-side operation order and duplicate-prevention transport regressions."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from features.pipeline_dispatch.executors import NodeExecutors, ExecutionError, COSArtifactStore, cpu_thread_command
from features.pipeline_dispatch.youtube_cover import inline_image, generate_cover
from features.fb_auto_posts.publisher import GraphResult
from features.x_posts.service import HttpResponse, XPostError


class Context:
    def __init__(self):
        self.operations, self.checkpoints = [], []
        self.last_operation_id = ""
    def check(self): pass
    def phase(self, name): pass
    def checkpoint(self, phase, receipt): self.checkpoints.append((phase, receipt))
    @contextmanager
    def operation(self, name, details, **kwargs):
        self.operations.append((name, details, kwargs))
        self.last_operation_id = "operation-%03d" % len(self.operations)
        yield {"access_token": "short-token", "credential_id": "credential-%d" % len(self.operations), "fb_user_id": "user1"}


class MissingObject(Exception):
    def get_status_code(self): return 404


class MemoryCOS:
    """Separate nodes share only objects, never local manifests or memory."""
    def __init__(self):
        self.objects, self.heads, self.puts = {}, {}, []
    def get_object(self, Bucket, Key):
        if Key not in self.objects: raise MissingObject()
        return {"Body": SimpleNamespace(get_raw_stream=lambda: io.BytesIO(self.objects[Key]))}
    def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.puts.append(kwargs)
        if "ForbidOverwrite" in kwargs: raise ValueError("deployed COS SDK rejects this named parameter")
        if key in self.objects and kwargs.get("Metadata", {}).get("x-cos-forbid-overwrite") == "true": raise RuntimeError("exists")
        self.objects[key] = kwargs["Body"]
    def head_object(self, Bucket, Key):
        if Key not in self.heads: raise MissingObject()
        return self.heads[Key]


class MemoryStore:
    def __init__(self, client):
        self.client = client
        self.config = SimpleNamespace(cos_bucket="media", cos_prefix="prepared", cos_domain="https://media.example", profile="frozen-v1")
    def url(self, key): return self.config.cos_domain + "/" + key
    def upload(self, key, path, sha, size, **kwargs):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha and len(data) == size
        self.client.objects[key] = data
        self.client.heads[key] = {"Content-Length": str(size), "x-cos-meta-sha256": sha}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"state_root": self.temp.name, "backend": "cpu"}
        self.context = Context()
    def tearDown(self): self.temp.cleanup()

    def test_uploaded_qualified_video_survives_hard_crash_and_moves_to_another_node(self):
        for kind in ("fb_prepare", "tt_prepare", "x_random", "x_repair"):
            with self.subTest(kind=kind):
                cloud = MemoryCOS()
                store = MemoryStore(cloud)
                body = b"qualified-video"
                sha = hashlib.sha256(body).hexdigest()
                key = "prepared/" + sha + ".mp4"
                request = {"job_id": "same-item", "job_key": "same-item", "source_url": "https://source.example/video", "expected_profile": "frozen-v1"}
                result = {"output_url": store.url(key), "output_sha256": sha, "output_size": len(body), "profile": "frozen-v1", "probe": {"duration": 10}, "recipe": {"asset_set_sha256": "a" * 64, "choice": 7}}
                renderer = SimpleNamespace(config=store.config, object_store=store)
                calls = []
                def render(frozen):
                    calls.append("render")
                    renderer.qualified_artifact(result, key)
                    cloud.objects[key] = body
                    cloud.heads[key] = {"Content-Length": len(body), "x-cos-meta-sha256": sha}
                    raise SystemExit("node power lost before local manifest and central completion")
                renderer.prepare = renderer.repair = render
                first = NodeExecutors(self.config, factories={kind: lambda: renderer})
                with self.assertRaises(SystemExit): first.prepare(kind, {"request": request}, self.context)
                # Another process has no renderer state or local manifest.
                other = SimpleNamespace(config=store.config, object_store=MemoryStore(cloud),
                                        prepare=lambda _: self.fail("must not rerender"), repair=lambda _: self.fail("must not rerepair"))
                second = NodeExecutors({**self.config, "state_root": str(Path(self.temp.name) / kind)}, factories={kind: lambda: other})
                recovered = second.prepare(kind, {"request": request}, Context())
                self.assertEqual(recovered["preparation"], result)
                self.assertEqual(recovered["artifact"]["sha256"], sha)
                self.assertEqual(calls, ["render"])
                self.assertEqual(cloud.puts[0]["ACL"], "private")
                self.assertNotIn(request["source_url"].encode(), cloud.puts[0]["Body"])

    def test_receipt_requires_uploaded_identity_and_rejects_changed_request_or_recipe(self):
        cloud, request = MemoryCOS(), {"job_id": "item-1", "expected_profile": "frozen-v1"}
        store = MemoryStore(cloud)
        sha, key = "a" * 64, "prepared/aaa.mp4"
        result = {"output_url": store.url(key), "output_sha256": sha, "output_size": 5, "recipe": {"choice": 1}}
        journal = COSArtifactStore(store, "fb_prepare", request, source_key="fb:auto:1")
        journal.qualified(result, key)
        with self.assertRaisesRegex(ExecutionError, "artifact_upload_incomplete"): journal.recover()
        cloud.heads[key] = {"Content-Length": 5, "x-cos-meta-sha256": "b" * 64}
        with self.assertRaisesRegex(ExecutionError, "artifact_fingerprint_changed"): journal.recover()
        cloud.heads[key]["x-cos-meta-sha256"] = sha
        self.assertEqual(journal.recover(), result)
        changed = COSArtifactStore(store, "fb_prepare", {**request, "expected_profile": "other"}, source_key="fb:auto:1")
        with self.assertRaisesRegex(ExecutionError, "artifact_receipt_conflict"): changed.recover()
        wrong_owner = COSArtifactStore(store, "fb_prepare", request, source_key="fb:auto:2")
        with self.assertRaisesRegex(ExecutionError, "artifact_receipt_conflict"): wrong_owner.recover()
        tampered = json.loads(cloud.objects[journal.key]); tampered["result"]["recipe"]["choice"] = 2
        cloud.objects[journal.key] = json.dumps(tampered).encode()
        with self.assertRaisesRegex(ExecutionError, "artifact_receipt_conflict"): journal.recover()

    def test_tt_native_callback_precedes_upload_and_preserves_full_random_contract(self):
        from scripts import test_tt_gpu_worker as fixture
        config = fixture.make_random_overlay_config(Path(self.temp.name))
        store = fixture.FakeObjectStore()
        renderer = fixture.worker.TTPostGPUProcessor(config,
            runner=fixture.FakeRunner([fixture.input_probe(28.5), fixture.prepared_probe(28.5)]),
            downloader=fixture.make_downloader([]), object_store=store)
        captured, sequence = [], []
        renderer.qualified_artifact = lambda result, key: (captured.append((result, key)), sequence.append("qualified"))
        upload = store.upload
        def checked_upload(*args, **kwargs):
            self.assertEqual(sequence, ["qualified"])
            sequence.append("upload")
            return upload(*args, **kwargs)
        store.upload = checked_upload
        result = renderer.prepare(fixture.make_prepare(expected_profile=fixture.worker.RANDOM_OVERLAY_PROFILE, source_trim_tail_seconds=0))
        self.assertEqual(sequence, ["qualified", "upload"])
        self.assertEqual({k: result[k] for k in captured[0][0]}, captured[0][0])
        self.assertEqual(captured[0][0]["random_overlay_recipe"], result["random_overlay_recipe"])
        self.assertEqual(captured[0][0]["assets"], result["assets"])

    def test_private_receipt_lost_put_ack_is_read_back_before_media_upload(self):
        cloud, store = MemoryCOS(), None
        store = MemoryStore(cloud)
        put = cloud.put_object
        def lost_ack(**kwargs): put(**kwargs); raise TimeoutError("lost ack")
        cloud.put_object = lost_ack
        journal = COSArtifactStore(store, "x_repair", {"job_key": "fixed"})
        result = {"output_url": store.url("prepared/a.mp4"), "output_sha256": "a" * 64, "output_size": 1}
        journal.qualified(result, "prepared/a.mp4")
        self.assertEqual(len(cloud.puts), 1)
        with self.assertRaisesRegex(ExecutionError, "artifact_upload_incomplete"): journal.recover()

    def test_cover_uploaded_but_completion_lost_is_reused_without_paid_generation(self):
        cloud, calls = MemoryCOS(), []
        store = MemoryStore(cloud)
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        raw = image.getvalue(); reference = inline_image(raw)
        task = {"id": "a" * 32, "reference_cover": {"asset_id": "b" * 32, "sha256": reference["sha256"]}, "versions": [{"number": 1}], "current_version": 1}
        def generate(*args): calls.append("paid-generation"); return raw
        request = {"task": task, "reference_image": reference}
        first = NodeExecutors(self.config, factories={"youtube_prepare": generate, "artifact_store": lambda: store})
        result = first.prepare("youtube_prepare", {"request": request}, self.context)
        # No coordinator completion was sent. A replacement host has only COS.
        second = NodeExecutors({**self.config, "state_root": str(Path(self.temp.name) / "other")}, factories={"youtube_prepare": lambda *a: self.fail("duplicate paid generation"), "artifact_store": lambda: MemoryStore(cloud)})
        recovered = second.prepare("youtube_prepare", {"request": request}, Context())
        self.assertEqual(result, recovered)
        self.assertEqual(calls, ["paid-generation"])
        self.assertEqual(result["artifact"]["sha256"], result["cover"]["sha256"])
        self.assertEqual(result["artifact"]["size"], result["cover"]["size"])
        self.assertEqual(result["artifact"]["profile"], "youtube-cover-v1")

    def test_cover_generation_intent_blocks_other_node_but_original_can_finish(self):
        cloud, calls = MemoryCOS(), []
        store = MemoryStore(cloud)
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        raw = image.getvalue(); reference = inline_image(raw)
        task = {"id": "a" * 32, "reference_cover": {"asset_id": "b" * 32, "sha256": reference["sha256"]}, "versions": [{"number": 1}], "current_version": 1}
        request = {"task": task, "reference_image": reference}
        second = NodeExecutors({**self.config, "state_root": str(Path(self.temp.name) / "replacement")}, factories={"youtube_prepare": lambda *a: self.fail("second paid generation"), "artifact_store": lambda: MemoryStore(cloud)})
        def generate(*args):
            calls.append("paid")
            with self.assertRaisesRegex(ExecutionError, "youtube_generation_interrupted"):
                second.prepare("youtube_prepare", {"request": request}, Context())
            return raw
        original = NodeExecutors(self.config, factories={"youtube_prepare": generate, "artifact_store": lambda: store})
        result = original.prepare("youtube_prepare", {"request": request}, self.context)
        self.assertEqual(second.prepare("youtube_prepare", {"request": request, "recovery_only": True}, Context()), result)
        self.assertEqual(calls, ["paid"])
        intent_puts = [p for p in cloud.puts if p["Key"].endswith(".generation-intent.json")]
        self.assertEqual(len(intent_puts), 1)
        self.assertEqual(intent_puts[0]["ACL"], "private")
        self.assertEqual(intent_puts[0]["Metadata"]["x-cos-forbid-overwrite"], "true")

    def test_cover_hard_crash_requires_explicit_new_version_or_retry_identity(self):
        cloud, calls = MemoryCOS(), []
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        raw = image.getvalue(); reference = inline_image(raw)
        task = {"id": "a" * 32, "reference_cover": {"asset_id": "b" * 32, "sha256": reference["sha256"]}, "versions": [{"number": 1}], "current_version": 1}
        request = {"task": task, "reference_image": reference}
        def interrupted(*args): calls.append("first-paid"); raise SystemExit("node disappeared")
        original = NodeExecutors(self.config, factories={"youtube_prepare": interrupted, "artifact_store": lambda: MemoryStore(cloud)})
        with self.assertRaises(SystemExit): original.prepare("youtube_prepare", {"request": request}, Context())
        def generate(*args): calls.append("explicit-paid"); return raw
        replacement = NodeExecutors({**self.config, "state_root": str(Path(self.temp.name) / "next")}, factories={"youtube_prepare": generate, "artifact_store": lambda: MemoryStore(cloud)})
        with self.assertRaisesRegex(ExecutionError, "youtube_generation_interrupted"):
            replacement.prepare("youtube_prepare", {"request": request}, Context())
        self.assertEqual(calls, ["first-paid"])
        # Existing explicit retry preserves version but freezes a new intent ID.
        retry = {**request, "task": {**task, "generation_retry_id": "c" * 32}}
        self.assertEqual(replacement.prepare("youtube_prepare", {"request": retry}, Context())["status"], "ready")
        version_two = {**request, "task": {**task, "current_version": 2, "versions": [{"number": 1}, {"number": 2}]}}
        self.assertEqual(replacement.prepare("youtube_prepare", {"request": version_two}, Context())["status"], "ready")
        self.assertEqual(calls, ["first-paid", "explicit-paid", "explicit-paid"])

    def test_cover_recovery_only_without_pointer_never_starts_generation(self):
        cloud = MemoryCOS()
        executor = NodeExecutors(self.config, factories={"youtube_prepare": lambda *a: self.fail("cannot pay during recovery"), "artifact_store": lambda: MemoryStore(cloud)})
        request = {"task": {"id": "a" * 32, "current_version": 1}}
        with self.assertRaisesRegex(ExecutionError, "youtube_generation_interrupted"):
            executor.prepare("youtube_prepare", {"request": request, "recovery_only": True}, self.context)
        self.assertEqual(cloud.puts, [])

    def test_catalog_capabilities_are_channel_scoped_and_frozen_bytes_are_checked(self):
        expected, changed = "a" * 64, "b" * 64
        with patch.dict(os.environ, {"PIPELINE_EXECUTORS": "fb_prepare,tt_prepare,x_random",
                "FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256": expected,
                "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256": expected,
                "X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256": expected}):
            for kind, channel in (("fb_prepare", "fb"), ("tt_prepare", "tt"), ("x_random", "x")):
                with self.subTest(kind=kind):
                    processor = SimpleNamespace(config=SimpleNamespace(random_overlay_assets={"manifest_sha256": expected}),
                                                assets={"manifest_sha256": expected})
                    executor = NodeExecutors(self.config, factories={kind: lambda: processor})
                    self.assertIn("catalog:" + channel + ":" + expected, executor.capabilities())
                    payload = {"required_capabilities": ["catalog:" + channel + ":" + expected]}
                    executor._validate_frozen_catalog(kind, payload)
                    # Matching configuration strings cannot conceal a different
                    # actual validated catalog supplied to the renderer.
                    processor.assets["manifest_sha256"] = changed
                    with self.assertRaisesRegex(ExecutionError, "frozen_catalog_mismatch"):
                        executor.prepare(kind, payload, self.context)
                    processor.assets["manifest_sha256"] = expected
                    payload["required_capabilities"] = ["catalog:" + channel + ":" + changed]
                    with self.assertRaisesRegex(ExecutionError, "frozen_catalog_mismatch"):
                        executor.prepare(kind, payload, self.context)

    def test_fb_rotates_past_eight_definite_rejections(self):
        calls = []
        def publish(*args, **kwargs):
            calls.append(args)
            return GraphResult("credential_failure", error_code="190") if len(calls) < 10 else GraphResult("success", post_id="page_post")
        executor = NodeExecutors(self.config, platform_clients={"fb_publish": SimpleNamespace(publish_video=publish)})
        artifact = {"url": "https://media.example/video.mp4", "identity_mode": "remote_url"}
        result = executor.fb_publish({"processing_mode": "original", "request": {"page_id": "123", "media_url": artifact["url"], "message": "text"}}, artifact, self.context)
        self.assertEqual(result["status"], "published")
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(self.context.checkpoints), 10)

    def test_tt_cpu_random_profile_is_distinct_and_cpu_runner_bounds_encoder(self):
        from features.tt_gpu.worker import _selected_media_profile, _encoder_arguments
        self.assertEqual(_selected_media_profile("hevc_nvenc", "random_overlay"), "tt-post-random-overlay-hevc-720x1280-v3")
        self.assertEqual(_selected_media_profile("libx264", "random_overlay"), "tt-post-random-overlay-libx264-720x1280-v1")
        config = SimpleNamespace(video_encoder="libx264")
        command = cpu_thread_command(["ffmpeg", "-i", "in.mp4", *_encoder_arguments(config), "out.mp4"])
        self.assertEqual(command[-3:], ["-threads", "4", "out.mp4"])
        self.assertNotIn("h264_nvenc", command)
        with self.assertRaises(ExecutionError): cpu_thread_command(["ffmpeg", "-c:v", "hevc_nvenc", "out"])

    def test_fb_unknown_is_never_rotated(self):
        calls = []
        def publish(*args, **kwargs): calls.append(args); return GraphResult("unknown")
        executor = NodeExecutors(self.config, platform_clients={"fb_publish": SimpleNamespace(publish_video=publish)})
        artifact = {"url": "https://media.example/video.mp4", "identity_mode": "remote_url"}
        result = executor.fb_publish({"processing_mode": "original", "request": {"page_id": "123", "media_url": artifact["url"], "message": "text"}}, artifact, self.context)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(calls), 1)

    def test_x_share_uses_text_only_exact_body_once(self):
        calls = []
        def send(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return HttpResponse(201, body=b'{"data":{"id":"123456"}}')
        executor = NodeExecutors(self.config, platform_clients={"x_share_publish": SimpleNamespace(request=send)})
        result = executor.x_share_publish({"request": {"text": "example URL"}}, None, self.context)
        self.assertEqual(result["post_id"], "123456")
        self.assertEqual(json.loads(calls[0][2]["body"]), {"text": "example URL"})
        self.assertEqual(self.context.checkpoints[0][0], "x_share_published")
        self.assertEqual(len(calls), 1)

    def test_x_share_408_is_unknown_but_403_is_definite(self):
        for status in (408, 403):
            with self.subTest(status=status):
                executor = NodeExecutors(self.config, platform_clients={"x_share_publish": SimpleNamespace(request=lambda *a, **k: HttpResponse(status))})
                if status == 408:
                    with self.assertRaises(ExecutionError) as raised:
                        executor.x_share_publish({"request": {"text": "text"}}, None, self.context)
                    self.assertTrue(raised.exception.unknown_outcome)
                else:
                    result = executor.x_share_publish({"request": {"text": "text"}}, None, self.context)
                    self.assertEqual(result["status"], "failed")

    def test_tt_initialize_receipt_precedes_processing(self):
        calls = []
        def initialize(*args): calls.append("init"); return {"publish_id": "v_pub.1", "log_id": "log1"}
        def status(*args):
            self.assertEqual(self.context.checkpoints[0][0], "tt_initialized")
            calls.append("status"); return {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["123"]}
        executor = NodeExecutors(self.config, platform_clients={"tt_publish": SimpleNamespace(initialize_video=initialize, fetch_status=status)})
        artifact = {"url": "https://media.example/video.mp4", "sha256": "a" * 64, "size": 10}
        executor.tt_publish({"request": {"verified_origin": "https://media.example", "post_info": {"title": "title"}}}, artifact, self.context)
        self.assertEqual(calls, ["init", "status"])

    def test_youtube_media_local_upload_then_confirm_before_comment(self):
        calls = []
        video = b"video-bytes"
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        def download(url, path, **kwargs): path.write_bytes(video)
        def begin(*args, **kwargs): calls.append("begin"); return "https://www.googleapis.com/upload?upload_id=1"
        def upload(*args): calls.append("upload"); return {"state": "submitted", "video_id": "video_123"}
        def thumbnail(*args, **kwargs): calls.append("thumbnail")
        def state(*args, **kwargs):
            calls.append("state")
            return {"state": "succeeded", "visibility": "public" if "visibility" in calls else "private", "preserved_status": {"privacyStatus": "private"}}
        def public(*args, **kwargs): calls.append("visibility")
        def comment(*args, **kwargs):
            self.assertEqual(calls[-1], "state")
            calls.append("comment"); return "comment1"
        api = SimpleNamespace(download=download, begin_resumable=begin, upload=upload, set_thumbnail=thumbnail, read_reviewed_video_state=state, make_video_public=public, publish_comment=comment)
        executor = NodeExecutors(self.config, platform_clients={"youtube_publish": api})
        payload = {"request": {"title": "title", "description": "desc", "channel_id": "UC" + "A" * 22, "comment_text": "hello", "approved_cover": inline_image(image.getvalue())}}
        with patch.dict(os.environ, {"PIPELINE_MEDIA_ALLOWED_HOSTS": "media.example"}), patch("features.pipeline_dispatch.executors.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "1.0"}}))):
            result = executor.youtube_publish(payload, {"url": "https://media.example/video.mp4", "identity_mode": "source_to_fingerprint"}, self.context)
        self.assertEqual(result["video_id"], "video_123")
        self.assertEqual(calls, ["begin", "upload", "thumbnail", "state", "visibility", "state", "comment"])
        self.assertEqual(self.context.checkpoints[0][0], "youtube_source")
        self.assertEqual(self.context.checkpoints[0][1]["sha256"], hashlib.sha256(video).hexdigest())

    def test_cover_generation_uses_frozen_reference_and_returns_valid_jpeg(self):
        image = io.BytesIO(); Image.new("RGB", (640, 360)).save(image, format="JPEG")
        raw = image.getvalue(); image_fact = inline_image(raw)
        task = {"id": "a" * 32, "reference_cover": {"asset_id": "b" * 32, "sha256": image_fact["sha256"]}, "versions": [{"number": 1}], "current_version": 1}
        def generator(frozen, version):
            saved = Path(self.temp.name) / ("youtube-" + task["id"]) / "assets" / ("b" * 32 + ".jpg")
            self.assertEqual(saved.read_bytes(), raw)
            return raw
        result = generate_cover({"task": task, "reference_image": image_fact}, self.temp.name, self.context, generator)
        self.assertEqual(result["reference_sha256"], image_fact["sha256"])
        self.assertEqual(result["cover"]["mime"], "image/jpeg")

    def test_youtube_continuation_reads_and_comments_without_upload_methods(self):
        calls = []
        def state(*args, **kwargs):
            calls.append("state")
            return {"state": "succeeded", "visibility": "public"}
        def comment(*args, **kwargs):
            self.assertEqual(self.context.checkpoints[-1][0], "youtube_schedule_readback")
            calls.append("comment")
            return "comment1"
        api = SimpleNamespace(read_reviewed_video_state=state, publish_comment=comment)
        executor = NodeExecutors(self.config, platform_clients={"youtube_publish": api})
        request = {"video_id": "video_123", "channel_id": "channel1", "comment_text": "hello", "publish_at": "2026-11-02T00:00:00Z"}
        result = executor.youtube_publish({"mode": "continuation", "request": request}, None, self.context)
        self.assertEqual((result["status"], calls), ("published", ["state", "comment"]))
        calls.clear()
        executor.youtube_publish({"mode": "continuation", "request": {**request, "comment_id": "comment1"}}, None, self.context)
        self.assertEqual(calls, ["state"])

    def test_youtube_legacy_uploads_public_without_cover_or_visibility_put(self):
        calls = []
        def begin(*args, **kwargs):
            self.assertEqual(kwargs["privacy_status"], "public")
            calls.append("begin")
            return "https://www.googleapis.com/upload?upload_id=1"
        def download(artifact, path, context): path.write_bytes(b"video"); return path
        def upload(*args): calls.append("upload"); return {"state": "submitted", "video_id": "video_123"}
        def state(*args): calls.append("state"); return {"state": "published", "visibility": "public"}
        def comment(*args, **kwargs): calls.append("comment"); return "comment1"
        api = SimpleNamespace(begin_resumable=begin, upload=upload, video_status=state, publish_comment=comment)
        executor = NodeExecutors(self.config, platform_clients={"youtube_publish": api})
        payload = {"mode": "legacy", "request": {"title": "title", "description": "desc", "channel_id": "channel", "comment_text": "hello"}}
        artifact = {"url": "https://media.example/video.mp4", "sha256": "a" * 64, "size": 5}
        with patch.object(executor, "_download", side_effect=download):
            result = executor.youtube_publish(payload, artifact, self.context)
        self.assertEqual(result["status"], "published")
        self.assertEqual(calls, ["begin", "upload", "state", "comment"])

    def test_x_preflight_repairs_only_once_and_keeps_original_identity(self):
        calls = []
        original = b"unsupported-video"
        repaired = b"compatible-video"
        output = {"output_url": "https://media.example/repaired.mp4", "output_sha256": hashlib.sha256(repaired).hexdigest(), "output_size": len(repaired), "profile": "cpu-profile", "probe": {"duration": 10}}
        def repair(request): calls.append(request); return output
        cloud = MemoryCOS(); store = MemoryStore(cloud)
        store.config.profile = "cpu-profile"
        output["output_url"] = store.url("prepared/repaired.mp4")
        cloud.heads["prepared/repaired.mp4"] = {"Content-Length": len(repaired), "x-cos-meta-sha256": hashlib.sha256(repaired).hexdigest()}
        processor = SimpleNamespace(config=store.config, object_store=store, repair=repair)
        executor = NodeExecutors(self.config, factories={"x_repair": lambda: processor})
        def download(url, path, **kwargs): path.write_bytes(original)
        def download_repaired(artifact, path, context, **kwargs): path.write_bytes(repaired); return path
        raw_probe = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}
        valid_probe = {"duration": 10, "width": 720, "height": 720, "codec": "h264", "size": len(repaired)}
        request = {"identity_mode": "source_to_fingerprint", "source_url": "https://media.example/original.mp4", "max_duration_seconds": 140, "repair_request": {"job_key": "a" * 64, "material_id": "1", "pool_item_id": "2", "profile": "cpu-profile", "duration_policy": "standard"}}
        with patch.dict(os.environ, {"PIPELINE_MEDIA_ALLOWED_HOSTS": "media.example"}), patch("features.drama_synthesis.youtube.YouTubeHTTPClient.download", side_effect=download), patch("features.pipeline_dispatch.executors.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(raw_probe))), patch("features.x_posts.service.probe_media", side_effect=[XPostError("invalid_media_codec", "unsupported", 422), valid_probe]), patch.object(executor, "_download", side_effect=download_repaired):
            result = executor.x_preflight(request, self.context)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(result["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(result["artifact"]["sha256"], hashlib.sha256(repaired).hexdigest())
        self.assertEqual(self.context.checkpoints[0][0], "x_preflight")
        self.assertEqual(result["repair_request"]["trigger_code"], "invalid_media_codec")

    def test_x_preflight_reuses_canonical_repaired_artifact_without_redownload(self):
        executor = NodeExecutors(self.config)
        artifact = {"url": "https://media.example/repaired.mp4", "sha256": "b" * 64, "size": 10, "mime": "video/mp4", "profile": "cpu-profile", "duration_seconds": 10}
        with patch("features.drama_synthesis.youtube.YouTubeHTTPClient.download", side_effect=AssertionError("must not redownload original")):
            result = executor.x_preflight({"prepared_artifact": artifact}, self.context)
        self.assertEqual(result["artifact"], artifact)
        self.assertEqual(self.context.checkpoints, [])


if __name__ == "__main__": unittest.main()

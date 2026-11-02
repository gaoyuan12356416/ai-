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
from features.pipeline_dispatch.executors import NodeExecutors, ExecutionError, cpu_thread_command
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


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"state_root": self.temp.name, "backend": "cpu"}
        self.context = Context()
    def tearDown(self): self.temp.cleanup()

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
        processor = SimpleNamespace(config=SimpleNamespace(profile="cpu-profile"), repair=repair)
        executor = NodeExecutors(self.config, factories={"x_repair": lambda: processor})
        def download(url, path, **kwargs): path.write_bytes(original)
        def download_repaired(artifact, path, context): path.write_bytes(repaired); return path
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

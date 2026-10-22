import copy
import shutil
import subprocess
import tempfile
import unittest
import hashlib
import json
from unittest import mock
from pathlib import Path

from features.video_editing.recipe import validate_recipe, PROFILE, output_frames
from features.video_editing.render import build_normalize_command, build_assemble_command, assembly_layers, probe_media, run_ffmpeg, validate_output
from features.video_editing.gpu_integration import EditingService, Config
from scripts.test_drama_synthesis_gpu_cache import FakeMultipartCos


def recipe_fixture():
    return {"version": 1, "profile": PROFILE, "sources": [{"source_id": "s", "kind": "episode", "url": "https://source.test/a.mp4",
        "identity": {"size_bytes": 100, "etag": "v1"}, "duration_ms": 5000, "width": 320, "height": 180, "has_audio": True}],
        "clips": [{"id": "a", "source_id": "s", "start_ms": 500, "end_ms": 1500, "transition": "fade"},
                  {"id": "b", "source_id": "s", "start_ms": 2000, "end_ms": 3000, "transition": "cut", "mute": True},
                  {"id": "c", "source_id": "s", "start_ms": 3500, "end_ms": 4500, "transition": "fade", "fit": "cover"},
                  {"id": "d", "source_id": "s", "start_ms": 1000, "end_ms": 2000, "transition": "cut"}]}


class RenderTests(unittest.TestCase):
    def test_large_timeline_bounds_decoders_and_preserves_total_frames(self):
        recipe = recipe_fixture()
        recipe["clips"] = [{"id": str(i), "source_id": "s", "start_ms": 0, "end_ms": 1000,
                             "transition": "fade" if i < 99 and i % 2 == 0 else "cut"} for i in range(100)]
        recipe = validate_recipe(recipe)
        layers, final = assembly_layers(recipe)
        self.assertEqual(len(layers), 3)
        self.assertTrue(all(len(group["inputs"]) <= 8 for layer in layers for group in layer))
        self.assertEqual(layers[-1][0]["frames"], output_frames(recipe))

    def test_commands_include_matching_audio_and_geometry(self):
        recipe = validate_recipe(recipe_fixture())
        command = build_normalize_command("source.mp4", recipe["clips"][0], "out.mp4", has_audio=True)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("pad=720:1280", graph); self.assertIn("aresample=48000", graph)
        muted = build_normalize_command("source.mp4", recipe["clips"][1], "out.mp4", has_audio=True)
        self.assertIn("anullsrc", muted[muted.index("-filter_complex") + 1])
        joined = build_assemble_command(["a.mp4", "b.mp4", "c.mp4", "d.mp4"], recipe, "joined.mp4")
        graph = joined[joined.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade:duration=0.3:offset=0.700000000", graph)
        self.assertIn("acrossfade=d=0.3", graph); self.assertIn("concat=n=2", graph)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_mixed_fps_silent_cut_fade_and_trim(self):
        with tempfile.TemporaryDirectory(prefix="video-editing-media-") as root:
            root = Path(root)
            source = root / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=5",
                "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=44100:duration=5", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
            recipe = validate_recipe(recipe_fixture()); paths = []
            for index, clip in enumerate(recipe["clips"]):
                path = root / (str(index) + ".mp4")
                run_ffmpeg(build_normalize_command(source, clip, path, has_audio=(index != 2), encoder="libx264"), 1)
                validate_output(probe_media(path), 30); paths.append(path)
            final = root / "result.mp4"
            run_ffmpeg(build_assemble_command(paths, recipe, final, encoder="libx264"), 3.4)
            info = validate_output(probe_media(final), output_frames(recipe))
            self.assertEqual((info["width"], info["height"]), (720, 1280))
            self.assertLessEqual(abs(info["duration_ms"] - 3400), 34)
            # Decode the final video and audio, catching malformed joins rather
            # than treating a successful mux/ffprobe alone as media acceptance.
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"],
                           check=True, capture_output=True, timeout=30)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_failed_ffmpeg_retains_safe_diagnostic_outside_media(self):
        with tempfile.TemporaryDirectory(prefix="video-editing-diagnostic-") as root:
            root = Path(root); state = root / "state/clip.local.json"; state.parent.mkdir()
            output = root / "media.mp4"
            service = EditingService(Config(root / "media", root / "state", "b", "r"), lambda: None)
            context = {"recipe_sha256": "a" * 64, "source_sha256": "b" * 64,
                       "source_size_bytes": 100, "asset_set_sha256": "c" * 64}
            command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=duration=0.1",
                       "-vf", "private_diagnostic_test_filter", "-y", str(output)]
            with self.assertRaises(Exception):
                service._local_render(output, state, {"fingerprint": "d" * 64}, command, .1,
                                      diagnostic_context=context)
            diagnostic = json.loads(state.with_name(state.name + ".failure.json").read_text())
            self.assertNotEqual(diagnostic["process"]["returncode"], 0)
            self.assertGreater(diagnostic["stderr"]["bytes"], 0)
            self.assertFalse(diagnostic["stderr"]["raw_stored"])
            self.assertEqual(diagnostic["context"], context)
            self.assertNotIn("private_diagnostic_test_filter", json.dumps(diagnostic))
            self.assertFalse(state.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_service_probe_render_private_receipts_cleanup_and_replay(self):
        class Cos:
            def __init__(self): self.objects = {}
            def __getattr__(self, name):
                if name == "get_bucket_versioning": return lambda **kw: {}
                def call(**kwargs):
                    instance = self.objects.setdefault(kwargs["Key"], FakeMultipartCos())
                    return getattr(instance, name)(**kwargs)
                return call
        with tempfile.TemporaryDirectory(prefix="video-editing-service-") as root:
            root = Path(root); source = root / "source.mp4"; cos = Cos()
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True, capture_output=True)
            data = source.read_bytes(); sha = hashlib.sha256(data).hexdigest()
            identity = {"bucket": "b", "key": "video-editing/v1/source.mp4", "size_bytes": len(data), "etag": "original"}
            original = FakeMultipartCos(); original.object_headers = {"Content-Length": str(len(data)), "ETag": '"original"'}
            cos.objects[identity["key"]] = original
            item = {"source_id": "s", "kind": "asset", "url": "https://b.cos.ap-hongkong.myqcloud.com/" + identity["key"], "identity": identity}
            config = Config(root / "work", root / "state", "b", "ap-hongkong", encoder="libx264", reserve_bytes=0)
            service = EditingService(config, lambda: cos)
            def download(row, path, **_kwargs):
                Path(path).write_bytes(data)
                return {"path": str(path), "sha256": sha, "size_bytes": len(data), "etag": '"original"'}
            with mock.patch("features.video_editing.gpu_integration.verified_source_download", side_effect=download) as transfer:
                probe = {"job_id": "ve_" + "a" * 32, "task_type": "video_editing_v1", "operation": "probe",
                         "recipe": {"version": 1, "profile": PROFILE, "sources": [item], "clips": []}}
                probed = service.execute(probe)
                self.assertEqual(probed["source_identity"]["sha256"], sha)
                self.assertNotIn("preview", probed)
                self.assertFalse((config.root / probe["job_id"]).exists())
                self.assertEqual(service.execute(probe), probed)
                self.assertEqual(transfer.call_count, 1)
                item = {**item, "identity": probed["source_identity"], **probed["metadata"]}
                payload = {"job_id": "ve_" + "b" * 32, "task_type": "video_editing_v1", "operation": "render",
                    "recipe": {"version": 1, "profile": PROFILE, "sources": [item], "clips": [
                        {"id": "1", "source_id": "s", "start_ms": 0, "end_ms": 1000, "transition": "fade"},
                        {"id": "2", "source_id": "s", "start_ms": 1000, "end_ms": 2000, "transition": "cut"}]}}
                rendered = service.execute(payload)
                self.assertEqual(rendered["artifact"]["duration_ms"], 1700)
                self.assertEqual(rendered["artifact"]["has_audio"], True)
                self.assertFalse((config.root / payload["job_id"]).exists())
                self.assertEqual(service.execute(payload), rendered)
                self.assertEqual(transfer.call_count, 2)
                self.assertTrue((config.state_root / payload["job_id"] / "result.json").exists())
                upload = cos.objects[rendered["artifact"]["key"]]
                self.assertEqual(upload.calls["create"][0]["ACL"], "private")


if __name__ == "__main__": unittest.main()

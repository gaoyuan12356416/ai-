import copy
import shutil
import subprocess
import tempfile
import unittest
import hashlib
import json
import re
from unittest import mock
from pathlib import Path

from features.video_editing.recipe import validate_recipe, LEGACY_PROFILE, PROFILE, output_frames, preview_geometry
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
    def test_compact_export_keeps_legacy_and_intermediates_for_both_recipe_versions(self):
        for version in (1, 2):
            recipe = recipe_fixture(); recipe["version"] = version
            if version == 2: recipe["clips"][0]["mirror"] = True
            clip = validate_recipe(recipe)["clips"][0]
            for encoder in ("h264_nvenc", "libx264"):
                with self.subTest(version=version, encoder=encoder):
                    legacy = build_normalize_command("in.mp4", clip, "out.mp4", has_audio=True, encoder=encoder)
                    for profile, final in ((LEGACY_PROFILE, True), (PROFILE, False)):
                        self.assertEqual(legacy, build_normalize_command("in.mp4", clip, "out.mp4", has_audio=True,
                            encoder=encoder, profile=profile, final=final))
                    compact = build_normalize_command("in.mp4", clip, "out.mp4", has_audio=True,
                        encoder=encoder, profile=PROFILE, final=True)
                    self.assertEqual(compact[compact.index("-maxrate") + 1], "3M")
                    self.assertEqual(compact[compact.index("-bufsize") + 1], "6M")
                    self.assertEqual(compact[compact.index("-preset") + 1], "p6" if encoder == "h264_nvenc" else "medium")
                    quality_option = "-cq" if encoder == "h264_nvenc" else "-crf"
                    self.assertEqual(compact[compact.index(quality_option) + 1], "26" if encoder == "h264_nvenc" else "24")
                    self.assertEqual(compact[compact.index("-filter_complex") + 1], legacy[legacy.index("-filter_complex") + 1])
                    self.assertEqual(compact[compact.index("-t") + 1], legacy[legacy.index("-t") + 1])

    def test_mirror_graph_runs_after_fit_and_leaves_audio_unchanged(self):
        value = recipe_fixture(); value["version"] = 2
        value["clips"][0].update(mirror=True, subtitle_band={"top": 200, "bottom": 400})
        clip = validate_recipe(value)["clips"][0]
        command = build_normalize_command("source.mp4", clip, "out.mp4", has_audio=True)
        graph = command[command.index("-filter_complex") + 1]
        self.assertLess(graph.index("pad=720:1280"), graph.index("split=2"))
        self.assertLess(graph.index("trim=end_frame="), graph.index("split=2"))
        self.assertIn("crop=720:200:0:200", graph)
        self.assertIn("overlay=0:200:shortest=1[v]", graph)
        plain = build_normalize_command("source.mp4", {**clip, "mirror": False}, "out.mp4", has_audio=True)
        self.assertEqual(graph.split(";[0:a:0]", 1)[1], plain[plain.index("-filter_complex") + 1].split(";[0:a:0]", 1)[1])
        unprotected = build_normalize_command("source.mp4", {**clip, "subtitle_band": None}, "out.mp4", has_audio=True)
        self.assertIn("[normalized]hflip[v]", unprotected[unprotected.index("-filter_complex") + 1])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_proxy_geometry_matches_actual_sar_and_rotated_normalization(self):
        with tempfile.TemporaryDirectory(prefix="video-editing-geometry-") as directory:
            root = Path(directory)
            for width, height, rotation, sar in ((1920, 1080, 0, "1"), (638, 480, 0, "1"),
                                                (640, 480, 0, "4/3"), (640, 480, 90, "4/3")):
                with self.subTest(width=width, height=height, rotation=rotation, sar=sar):
                    raw, source, output = root / "raw.mp4", root / "source.mp4", root / "proxy.mp4"
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=white:size={width}x{height}:rate=30:duration=0.2", "-vf", "setsar=" + sar,
                        "-c:v", "libx264", str(raw)], check=True, capture_output=True, timeout=30)
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotation),
                        "-i", str(raw), "-c", "copy", str(source)], check=True, capture_output=True, timeout=30)
                    metadata = probe_media(source)
                    self.assertEqual(metadata["rotation"], rotation)
                    geometry = preview_geometry(metadata, proxy=True)
                    clip = {"start_frame": 0, "end_frame": 6, "fit": "contain", "mute": False}
                    run_ffmpeg(build_normalize_command(source, clip, output, has_audio=False, encoder="libx264"), .2)
                    gray = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                        "-pix_fmt", "gray", "-f", "rawvideo", "-"], check=True, capture_output=True, timeout=30).stdout
                    rows = [y for y in range(1280) if max(gray[y * 720:(y + 1) * 720]) > 128]
                    middle = gray[rows[len(rows) // 2] * 720:(rows[len(rows) // 2] + 1) * 720]
                    columns = [x for x, value in enumerate(middle) if value > 128]
                    self.assertEqual(geometry["content_rect"],
                        {"x": columns[0], "y": rows[0], "width": len(columns), "height": len(rows)})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_mirror_preserves_only_selected_subtitle_band(self):
        with tempfile.TemporaryDirectory(prefix="video-editing-mirror-") as directory:
            root = Path(directory); still = root / "readable.ppm"; source = root / "source.mp4"
            # An intentionally asymmetric frame with actual readable glyphs.
            # This fixture needs no font installation, OCR or imaging package.
            pixels = bytearray()
            for y in range(1280):
                pixels.extend(bytes((220, 35, 30) if y < 960 else (35, 70, 160)) * 360)
                pixels.extend(bytes((25, 130, 210) if y < 960 else (100, 160, 25)) * 360)
            glyphs = {"R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
                      "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
                      "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
                      "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
                      "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
                      "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
                      "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"]}
            for offset, letter in enumerate("READ123"):
                for row, line in enumerate(glyphs[letter]):
                    for col, bit in enumerate(line):
                        if bit != "1": continue
                        for dy in range(10):
                            start = ((1040 + row * 10 + dy) * 720 + 100 + offset * 70 + col * 10) * 3
                            pixels[start:start + 30] = bytes((255, 255, 255)) * 10
            still.write_bytes(b"P6\n720 1280\n255\n" + pixels)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(still),
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.6", "-t", "0.6",
                "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                check=True, capture_output=True, timeout=30)
            base = {"start_frame": 0, "end_frame": 18, "fit": "contain", "mute": False}
            rendered = {}
            for name, effect in (("plain", {}), ("protected", {"mirror": True}),
                                 ("full", {"mirror": True, "subtitle_band": None}),
                                 ("top", {"mirror": True, "subtitle_band": {"top": 0, "bottom": 320}})):
                output = root / (name + ".mp4")
                run_ffmpeg(build_normalize_command(source, {**base, **effect}, output, has_audio=True, encoder="libx264"), .6)
                validate_output(probe_media(output), 18)
                rendered[name] = output
            def decode(path, flip=False):
                command = ["ffmpeg", "-v", "error", "-i", str(path)]
                if flip: command += ["-vf", "hflip"]
                return subprocess.run([*command, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                      check=True, capture_output=True, timeout=30).stdout
            def region(data, top, bottom): return data[top * 720 * 3:bottom * 720 * 3]
            def difference(a, b): return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
            plain, flipped = decode(rendered["plain"]), decode(rendered["plain"], True)
            protected, full, top = (decode(rendered[key]) for key in ("protected", "full", "top"))
            self.assertLess(difference(region(protected, 4, 956), region(flipped, 4, 956)), 3)
            self.assertLess(difference(region(protected, 964, 1276), region(plain, 964, 1276)), 3)
            self.assertGreater(difference(region(protected, 964, 1276), region(flipped, 964, 1276)), 25)
            self.assertLess(difference(full, flipped), 3)
            self.assertLess(difference(region(top, 4, 316), region(plain, 4, 316)), 3)
            self.assertLess(difference(region(top, 324, 1276), region(flipped, 324, 1276)), 3)
            joined = root / "joined.mp4"
            recipe = {"clips": [{**base, "transition": "fade"}, {**base, "transition": "cut"}]}
            run_ffmpeg(build_assemble_command([rendered["protected"], rendered["top"]], recipe, joined, encoder="libx264"), .9)
            validate_output(probe_media(joined), 27)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(joined), "-f", "null", "-"],
                           check=True, capture_output=True, timeout=30)

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
            # FFmpeg 7.1 clears link frame_rate in setpts; FFmpeg 8 preserves
            # it by default. Emulate the production behavior when the newer
            # binary exposes strip_fps so this regression is caught locally.
            help_text = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=setpts"],
                capture_output=True, text=True, check=True).stdout
            if "strip_fps" in help_text:
                legacy = root / "result-legacy-setpts.mp4"
                command = build_assemble_command(paths, recipe, legacy, encoder="libx264")
                graph_index = command.index("-filter_complex") + 1
                command[graph_index], count = re.subn(r"(?<!a)setpts=([^;,\[]+)",
                    r"setpts=\1:strip_fps=1", command[graph_index])
                self.assertGreaterEqual(count, 7)
                run_ffmpeg(command, 3.4)
                validate_output(probe_media(legacy), output_frames(recipe))
                subprocess.run(["ffmpeg", "-v", "error", "-i", str(legacy), "-f", "null", "-"],
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
    def test_real_service_compacts_final_layer_only_for_both_profiles_and_mirror_versions(self):
        class Cos:
            def __init__(self): self.objects = {}
            def __getattr__(self, name):
                if name == "get_bucket_versioning": return lambda **kw: {}
                def call(**kwargs):
                    instance = self.objects.setdefault(kwargs["Key"], FakeMultipartCos())
                    return getattr(instance, name)(**kwargs)
                return call
        with tempfile.TemporaryDirectory(prefix="video-editing-compact-") as root:
            root = Path(root); source = root / "source.mp4"; cos = Cos()
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=160x120:rate=25:duration=0.6", "-c:v", "libx264", "-preset", "ultrafast", str(source)],
                check=True, capture_output=True)
            data = source.read_bytes(); sha = hashlib.sha256(data).hexdigest()
            item = {"source_id": "s", "kind": "asset", "url": "https://b.cos.ap-hongkong.myqcloud.com/video-editing/v1/source.mp4",
                "identity": {"bucket": "b", "key": "video-editing/v1/source.mp4", "size_bytes": len(data), "sha256": sha},
                "duration_ms": 600, "width": 160, "height": 120, "has_audio": False}
            def download(row, path, **_kwargs):
                Path(path).write_bytes(data)
                return {"path": str(path), "sha256": sha, "size_bytes": len(data)}
            config = Config(root / "work", root / "state", "b", "ap-hongkong", encoder="libx264", reserve_bytes=0)
            service = EditingService(config, lambda: cos)
            cases = ((LEGACY_PROFILE, 1, 1), (PROFILE, 1, 1), (PROFILE, 13, 1),
                     (LEGACY_PROFILE, 1, 2), (PROFILE, 1, 2), (PROFILE, 13, 2))
            for index, (profile, count, version) in enumerate(cases):
                with self.subTest(profile=profile, clips=count, version=version):
                    payload = {"job_id": "ve_" + str(index + 1) * 32, "task_type": "video_editing_v1", "operation": "render",
                        "recipe": {"version": version, "profile": profile, "sources": [item], "clips": [
                            {"id": str(i), "source_id": "s", "start_ms": 0, "end_ms": 600,
                             "transition": "fade" if count > 1 and i == 0 else "cut",
                             **({"mirror": True} if version == 2 and i == 0 else {})} for i in range(count)]}}
                    with mock.patch("features.video_editing.gpu_integration.verified_source_download", side_effect=download), \
                         mock.patch.object(service, "_local_render", wraps=service._local_render) as renders:
                        result = service.execute(payload)
                        commands = [call.args[3] for call in renders.call_args_list]
                        compact = [command for command in commands if command[command.index("-crf") + 1] == "24"]
                        self.assertEqual(len(commands), 16 if count == 13 else 1)
                        self.assertEqual(len(compact), 1 if profile == PROFILE else 0)
                        if compact: self.assertEqual(compact[0], commands[-1])
                        self.assertEqual(result["artifact"]["duration_ms"], 7500 if count == 13 else 600)
                        self.assertEqual(service.execute(payload), result)
                        self.assertEqual(renders.call_count, len(commands))

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
                mirrored_payload = copy.deepcopy(payload)
                mirrored_payload["job_id"] = "ve_" + "c" * 32
                mirrored_payload["recipe"]["version"] = 2
                mirrored_payload["recipe"]["clips"][0]["mirror"] = True
                mirrored = service.execute(mirrored_payload)
                self.assertNotEqual(mirrored["recipe_sha256"], rendered["recipe_sha256"])
                self.assertEqual(mirrored["artifact"]["duration_ms"], 1700)
                self.assertEqual(service.execute(mirrored_payload), mirrored)
                self.assertEqual(transfer.call_count, 3)


if __name__ == "__main__": unittest.main()

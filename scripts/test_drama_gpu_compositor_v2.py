"""CPU-only contract tests for the chunked OpenCL random-overlay renderer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis import gpu, gpu_compositor
from features.drama_synthesis.composition import (
    RENDERER_PROFILE,
    compile_random_overlay_spec,
    composition_sha256,
    plan_chunks,
    validate_composition_spec,
)
from features.drama_synthesis.core import DramaSynthesisError, RECIPE_PROFILE
from scripts import benchmark_drama_gpu_compositor_v2 as benchmark


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CompositorFixture:
    def __init__(self, root: Path, duration: float = 250.0):
        self.root = root
        self.duration = duration
        self.source = root / "source.mp4"
        self.output = root / "result.mp4"
        self.source.write_bytes(b"source-video")
        self.paths = {}
        self.rows = {}
        for category, media_type in (
            ("border", "image/png"),
            ("opacity_video", "video/webm"),
            ("corners", "video/webm"),
            ("tint", "image/png"),
        ):
            suffix = ".png" if media_type == "image/png" else ".webm"
            path = root / (category + suffix)
            content = (category + "-asset").encode()
            path.write_bytes(content)
            self.paths[category] = path
            self.rows[category] = {
                "media_type": media_type,
                "name": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        unsigned = {
            "profile": RECIPE_PROFILE,
            "version": 1,
            "source": "concat_video",
            "assets": self.rows,
            "asset_set_sha256": "b" * 64,
            "rotation_millidegrees": 1000,
            "scale_bp": 10050,
            "tint_opacity_bp": 600,
        }
        self.recipe = {**unsigned, "recipe_sha256": hashlib.sha256(canonical(unsigned).encode()).hexdigest()}
        self.asset_set = {
            "manifest_sha256": "b" * 64,
            "categories": {
                category: ({**self.rows[category], "path": path},)
                for category, path in self.paths.items()
            },
        }

    def probe(self, _ffprobe: str, path: Path):
        path = Path(path)
        if path == self.source:
            return {
                "duration": self.duration,
                "has_audio": True,
                "audio": {"codec_name": "aac"},
                "video": {"codec_name": "h264", "profile": "High", "width": 1920, "height": 1080},
            }
        if path == self.paths["opacity_video"]:
            return {"duration": 10.0, "has_audio": False, "video": {"codec_name": "vp9"}}
        if path == self.paths["corners"]:
            return {"duration": 60.0, "has_audio": False, "video": {"codec_name": "vp9"}}
        name = path.name
        if "chunk-00000" in name:
            duration = min(120.0, self.duration)
        elif "chunk-00001" in name:
            duration = min(120.0, max(0.0, self.duration - 120.0))
        elif "chunk-00002" in name:
            duration = max(0.0, self.duration - 240.0)
        else:
            duration = self.duration
        return {
            "duration": duration,
            "has_audio": "final" in name or path == self.output,
            "first_packet_keyframe": True,
            "audio": {
                "codec_name": "aac", "profile": "LC", "sample_rate": "48000", "channels": 2,
                "channel_layout": "stereo", "codec_tag_string": "mp4a",
            } if "final" in name or path == self.output else None,
            "video": {
                "codec_name": "h264", "profile": "High", "width": 720, "height": 1280,
                "pix_fmt": "yuv420p", "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                "nb_frames": str(round(duration * 30)), "level": 31, "time_base": "1/15360",
                "codec_tag_string": "avc1", "extradata_size": 45,
                "extradata_hash": "SHA256:" + "d" * 64, "is_avc": "true",
                "nal_length_size": "4", "color_range": "tv",
                "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
                "chroma_location": "left", "field_order": "progressive", "has_b_frames": 0,
            },
        }

    @staticmethod
    def runner(command, **_kwargs):
        Path(command[-1]).write_bytes(hashlib.sha256("\0".join(command).encode()).digest())

    @staticmethod
    def runtime_probe(_ffmpeg):
        return {
            "version": 1, "release_sha": "c" * 40,
            "declared_identity": "ffmpeg-opencl-nvenc-runtime-v1",
            "ffmpeg": {"sha256": "e" * 64, "size_bytes": 123456},
            "opencl_device": "0.0", "gpu_driver_identity_sha256": "f" * 64,
        }

    def patches(self):
        return (
            mock.patch.object(gpu_compositor, "load_asset_set", return_value=self.asset_set),
            mock.patch.object(gpu_compositor, "validate_recipe"),
            mock.patch.object(gpu_compositor, "selected_asset_paths", return_value=self.paths),
            mock.patch.object(gpu_compositor, "cache_root", return_value=self.root / "cache"),
        )

    def render(self, runner=None, probe=None, confirmed_stopped_recovery=False):
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3]:
            return gpu_compositor.render_chunked_random_output(
                source=self.source,
                output=self.output,
                recipe=self.recipe,
                asset_root=self.root,
                manifest_sha256="b" * 64,
                ffmpeg="ffmpeg",
                ffprobe="ffprobe",
                confirmed_stopped_recovery=confirmed_stopped_recovery,
                runtime_probe=self.runtime_probe,
                runner=runner or self.runner,
                probe=probe or self.probe,
            )


class CompositionSpecTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = CompositorFixture(Path(self.directory.name))
        self.source_info = self.fixture.probe("ffprobe", self.fixture.source)

    def test_scene_hash_and_frame_chunks_are_deterministic(self):
        first = compile_random_overlay_spec(self.fixture.recipe, self.source_info)
        second = compile_random_overlay_spec(dict(self.fixture.recipe), dict(self.source_info))
        self.assertEqual(composition_sha256(first), composition_sha256(second))
        self.assertEqual(first["renderer_profile"], RENDERER_PROFILE)
        chunks = plan_chunks(first["timeline"]["total_frames"], seconds=120)
        self.assertEqual([row["frame_count"] for row in chunks], [3600, 3600, 300])
        self.assertEqual(sum(row["frame_count"] for row in chunks), 7500)

    def test_scene_validation_is_strict(self):
        scene = compile_random_overlay_spec(self.fixture.recipe, self.source_info)
        scene["layers"][0]["unexpected"] = True
        with self.assertRaises(DramaSynthesisError) as caught:
            validate_composition_spec(scene)
        self.assertEqual(caught.exception.code, "drama_composition_invalid")

    def test_benchmark_output_must_be_a_new_child_of_fixed_root(self):
        allowed = self.fixture.root / "benchmarks"
        allowed.mkdir()
        created = benchmark.safe_new_output_root(str(allowed / "run-1"), allowed)
        self.assertTrue(created.is_dir())
        with self.assertRaisesRegex(ValueError, "must_be_new"):
            benchmark.safe_new_output_root(str(created), allowed)
        with self.assertRaisesRegex(ValueError, "outside"):
            benchmark.safe_new_output_root(str(self.fixture.root / "outside"), allowed)
        self.assertEqual(
            benchmark.safe_output_root(str(created), allowed, resume=True), created
        )
        with self.assertRaisesRegex(ValueError, "resume_root_invalid"):
            benchmark.safe_output_root(str(self.fixture.root / "outside"), allowed, resume=True)

    def test_runtime_fingerprint_binds_release_binary_device_and_driver(self):
        ffmpeg = self.fixture.root / "ffmpeg"
        nvidia_smi = self.fixture.root / "nvidia-smi"
        ffmpeg.write_bytes(b"verified-ffmpeg")
        nvidia_smi.write_bytes(b"verified-nvidia-smi")
        ffmpeg.chmod(0o700)
        nvidia_smi.chmod(0o700)
        env = {
            "DRAMA_GPU_RELEASE_SHA": "c" * 40,
            "DRAMA_GPU_RUNTIME_IDENTITY": "ffmpeg-opencl-nvenc-runtime-v1",
            "DRAMA_GPU_OPENCL_DEVICE": "0.0",
            "DRAMA_NVIDIA_SMI": str(nvidia_smi),
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    gpu_compositor.subprocess, "run",
                    return_value=SimpleNamespace(stdout="GPU-test, 565.57.01\n"),
                ):
            result = gpu_compositor._runtime_fingerprint(
                str(ffmpeg), nvidia_validator=lambda _path: True
            )
        self.assertEqual(result["release_sha"], "c" * 40)
        self.assertEqual(result["ffmpeg"]["sha256"], hashlib.sha256(b"verified-ffmpeg").hexdigest())
        self.assertRegex(result["gpu_driver_identity_sha256"], r"^[0-9a-f]{64}$")


class MediaProbeTests(unittest.TestCase):
    def _responses(self):
        media = {
            "streams": [{"codec_type": "video", "duration": "30.000000"}],
            "format": {"duration": "30.043984"},
        }
        return [
            SimpleNamespace(stdout=json.dumps(media)),
            SimpleNamespace(stdout=json.dumps({"packets": [{"flags": "K_"}]})),
        ]

    def test_compositor_probe_prefers_video_timeline_over_container_tick(self):
        with mock.patch.object(gpu_compositor.subprocess, "run", side_effect=self._responses()):
            info = gpu_compositor._probe("ffprobe", Path("source.mp4"))
        self.assertEqual(info["duration"], 30.0)

    def test_benchmark_probe_uses_the_same_video_timeline(self):
        with mock.patch.object(benchmark.subprocess, "run", side_effect=self._responses()):
            info = benchmark.probe("ffprobe", Path("source.mp4"))
        self.assertEqual(info["duration"], 30.0)

    def test_compositor_accepts_four_lanes_and_bounds_threads(self):
        self.assertEqual(gpu_compositor.compositor_lanes("4"), 4)
        self.assertEqual(gpu_compositor.compositor_filter_threads("2"), 2)
        for invalid in ("0", "5", True):
            with self.subTest(invalid=invalid), self.assertRaises(DramaSynthesisError):
                gpu_compositor.compositor_lanes(invalid)

    def test_visual_comparison_frames_use_sequential_trim_not_fast_seek(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.mp4"
            candidate = root / "candidate.mp4"
            legacy.write_bytes(b"legacy")
            candidate.write_bytes(b"candidate")
            commands = []

            def runner(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"comparison")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(benchmark.subprocess, "run", side_effect=runner):
                records = __import__(
                    "scripts.compare_drama_gpu_compositor_v2", fromlist=["extract_comparisons"]
                ).extract_comparisons("ffmpeg", legacy, candidate, root, 30.0)

        self.assertEqual(len(records), 3)
        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertNotIn("-ss", command)
            graph = command[command.index("-filter_complex") + 1]
            self.assertEqual(graph.count("trim=start="), 2)

    def test_visual_clip_uses_accurate_transcode_not_keyframe_stream_copy(self):
        compare = __import__(
            "scripts.compare_drama_gpu_compositor_v2", fromlist=["create_clip"]
        )
        with mock.patch.object(compare.subprocess, "run") as run:
            compare.create_clip(
                "ffmpeg", Path("source.mp4"), Path("clip.mp4"), 60, 30
            )
        command = run.call_args.args[0]
        self.assertNotIn("copy", command)
        self.assertIn("libx264", command)
        self.assertEqual(command[command.index("-ss") + 1], "60")
        self.assertEqual(command[command.index("-t") + 1], "30")

    def test_clean_reference_uses_rotation_angle_for_canvas_extents(self):
        compare = __import__(
            "scripts.compare_drama_gpu_compositor_v2", fromlist=["render_clean_reference"]
        )
        with tempfile.TemporaryDirectory() as directory:
            fixture = CompositorFixture(Path(directory), duration=30.0)
            output = fixture.root / "clean-reference.mp4"
            captured = []

            def runner(command, **_kwargs):
                captured.append(command)
                return SimpleNamespace(returncode=0)

            graph = (
                "[0:v]rotate=1.000000*PI/180:ow=rotw(iw):oh=roth(ih):c=black@0[v]"
            )
            with mock.patch.object(compare, "load_asset_set", return_value=fixture.asset_set), \
                    mock.patch.object(compare, "validate_recipe"), \
                    mock.patch.object(compare, "selected_asset_paths", return_value=fixture.paths), \
                    mock.patch.object(
                        compare, "build_drama_random_command",
                        return_value=["ffmpeg", "-filter_complex", graph, str(output)],
                    ), \
                    mock.patch.object(compare.subprocess, "run", side_effect=runner):
                compare.render_clean_reference(
                    "ffmpeg", fixture.source, output,
                    fixture.probe("ffprobe", fixture.source), fixture.recipe,
                    str(fixture.root), "b" * 64,
                )

        self.assertEqual(len(captured), 1)
        corrected = captured[0][captured[0].index("-filter_complex") + 1]
        self.assertNotIn("rotw(iw)", corrected)
        self.assertIn("ow=rotw(1.000000*PI/180)", corrected)
        self.assertIn("oh=roth(1.000000*PI/180)", corrected)


class OpenCLCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = CompositorFixture(Path(self.directory.name))

    def test_command_uses_one_fused_gpu_filter_and_nvenc(self):
        scene = compile_random_overlay_spec(
            self.fixture.recipe, self.fixture.probe("ffprobe", self.fixture.source)
        )
        kernel = gpu_compositor.compile_opencl_kernel(scene)
        kernel_path = self.fixture.root / "kernel.cl"
        kernel_path.write_text(kernel["source"], encoding="utf-8")
        command = gpu_compositor.build_opencl_chunk_command(
            ffmpeg="ffmpeg", source=self.fixture.source, output=self.fixture.output,
            spec=scene, assets=self.fixture.paths,
            asset_media_types={key: row["media_type"] for key, row in self.fixture.rows.items()},
            asset_durations={"opacity_video": 10.0, "corners": 60.0},
            chunk=plan_chunks(7500, seconds=120)[1], kernel_path=kernel_path,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph.count("program_opencl="), 1)
        self.assertIn("inputs=5", graph)
        self.assertIn("h264_nvenc", command)
        self.assertIn("opencl=ocl:0.0", command)
        self.assertEqual(command[command.index("-filter_complex_threads") + 1], "2")
        self.assertGreaterEqual(command.count("-threads"), 5)
        self.assertNotIn("overlay=", graph)
        self.assertNotIn("rotate=", graph)
        self.assertNotIn("scale=720:1280", graph)
        self.assertIn(
            "[0:v]setpts=PTS,fps=30,tpad=stop_mode=clone:stop_duration=1,"
            "format=rgba,hwupload[source]",
            graph,
        )
        self.assertNotIn("[0:v]fps=30,tpad=stop_mode=clone:stop_duration=1", graph)
        self.assertEqual(graph.count("setpts=PTS-STARTPTS"), 4)
        self.assertIn("fps=30,setpts=N/(30*TB)", graph)
        self.assertEqual(command[command.index("-r") + 1], "30")
        self.assertIn("transfer_characteristics=1", command[command.index("-bsf:v") + 1])
        self.assertEqual(kernel["source"].count("get_image_width(source)"), 2)
        self.assertNotIn("SCENE_SOURCE_WIDTH", kernel["source"])
        geometry = gpu_compositor._clean_main_geometry(1.005)
        self.assertEqual(geometry, {
            "main_width": 722,
            "main_height": 1286,
        })
        for name, value in (
            ("SCENE_MAIN_WIDTH", geometry["main_width"]),
            ("SCENE_MAIN_HEIGHT", geometry["main_height"]),
        ):
            self.assertIn("#define %s %d" % (name, value), kernel["source"])
        self.assertNotIn("SCENE_ROTATED_WIDTH", kernel["source"])
        self.assertNotIn("SCENE_OVERLAY_Y", kernel["source"])
        self.assertIn("centered.x * cosine + centered.y * sine", kernel["source"])

    def test_clean_geometry_keeps_only_centered_scaled_plane(self):
        geometry = gpu_compositor._clean_main_geometry(0.9963)
        self.assertEqual(geometry, {
            "main_width": 716,
            "main_height": 1274,
        })
        self.assertEqual(set(geometry), {"main_width", "main_height"})
        self.assertEqual(geometry["main_width"] % 2, 0)
        self.assertEqual(geometry["main_height"] % 2, 0)


class ChunkedRenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = CompositorFixture(Path(self.directory.name))

    def test_render_reuses_verified_output_and_preserves_public_contract(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return self.fixture.runner(command, **kwargs)

        result = self.fixture.render(runner)
        self.assertEqual(set(result), {
            "output_sha256", "output_size", "duration_seconds", "profile", "recipe_sha256"
        })
        self.assertTrue(self.fixture.output.is_file())
        self.assertEqual(len(calls), 5)  # three chunks, stream-copy join, audio mux
        calls.clear()
        self.assertEqual(self.fixture.render(runner), result)
        self.assertEqual(calls, [])
        self.assertFalse(any((self.fixture.root / "cache").rglob("chunk-*.mp4")))

    def test_legacy_renderer_reuses_completed_v2_output_after_code_rollback(self):
        result = self.fixture.render()
        with mock.patch.object(gpu, "load_asset_set", return_value={}), \
                mock.patch.object(gpu, "validate_recipe"), \
                mock.patch.object(gpu, "selected_asset_paths", return_value={}), \
                mock.patch.object(gpu, "_probe", side_effect=self.fixture.probe):
            reused = gpu._render_random_output_legacy(
                source=self.fixture.source,
                output=self.fixture.output,
                recipe=self.fixture.recipe,
                asset_root=self.fixture.root,
                manifest_sha256="b" * 64,
                runner=mock.Mock(side_effect=AssertionError("rollback must not render")),
            )
        self.assertEqual(reused, result)

    def test_two_compositor_lanes_render_chunks_in_parallel_with_one_job_slot(self):
        mutex = threading.Lock()
        both_running = threading.Event()
        release = threading.Event()
        active = 0
        maximum = 0

        def runner(command, **kwargs):
            nonlocal active, maximum
            is_chunk = any(".chunk-" in part for part in command)
            if is_chunk:
                with mutex:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        both_running.set()
                if not both_running.wait(2):
                    raise RuntimeError("second compositor lane did not start")
                release.set()
            try:
                return self.fixture.runner(command, **kwargs)
            finally:
                if is_chunk:
                    with mutex:
                        active -= 1

        with mock.patch.dict(os.environ, {"DRAMA_GPU_COMPOSITOR_LANES": "2"}):
            self.fixture.render(runner)
        self.assertTrue(release.is_set())
        self.assertEqual(maximum, 2)

    def test_timeout_keeps_verified_chunks_and_retry_resumes_at_next_chunk(self):
        calls = []

        def interrupted(command, **kwargs):
            calls.append(command)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired(command, 1)
            return self.fixture.runner(command, **kwargs)

        with self.assertRaises(DramaSynthesisError) as caught:
            self.fixture.render(interrupted)
        self.assertEqual(caught.exception.code, "drama_render_chunk_timeout")
        self.assertEqual(len(list((self.fixture.root / "cache").rglob("chunk-00000.json"))), 1)

        resumed = []

        def runner(command, **kwargs):
            resumed.append(command)
            return self.fixture.runner(command, **kwargs)

        self.fixture.render(runner)
        self.assertEqual(len(resumed), 4)  # remaining two chunks, join, mux
        self.assertFalse(any("chunk-00000" in part for command in resumed for part in command))

    def test_process_uncertain_failure_preserves_partial_for_reconciliation(self):
        def uncertain(command, **_kwargs):
            output = Path(command[-1])
            output.write_bytes(b"partial-evidence")
            raise DramaSynthesisError(
                "drama_media_checkpoint_unverified", "checkpoint uncertain", 503
            )

        with self.assertRaises(DramaSynthesisError) as caught:
            self.fixture.render(uncertain)
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_unverified")
        partials = list((self.fixture.root / "cache").rglob(".chunk-00000.tmp.mp4"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), b"partial-evidence")

        def invalid_partial_probe(ffprobe, path):
            if (
                Path(path).name == ".chunk-00000.tmp.mp4"
                and Path(path).read_bytes() == b"partial-evidence"
            ):
                raise DramaSynthesisError("drama_render_chunk_failed", "partial", 502)
            return self.fixture.probe(ffprobe, path)

        renderer = mock.Mock(side_effect=AssertionError("uncertain partial must not be overwritten"))
        with self.assertRaises(DramaSynthesisError) as replay:
            self.fixture.render(renderer, probe=invalid_partial_probe)
        self.assertEqual(replay.exception.code, "drama_media_checkpoint_unverified")
        renderer.assert_not_called()
        self.assertEqual(partials[0].read_bytes(), b"partial-evidence")

        calls = []

        def recovered_runner(command, **kwargs):
            calls.append(command)
            return self.fixture.runner(command, **kwargs)

        self.fixture.render(
            recovered_runner, probe=invalid_partial_probe, confirmed_stopped_recovery=True
        )
        self.assertEqual(len(calls), 5)
        self.assertFalse(partials[0].exists())

    def test_existing_unverified_output_fails_closed(self):
        self.fixture.output.write_bytes(b"foreign-output")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.fixture.render()
        self.assertEqual(caught.exception.code, "drama_media_checkpoint_conflict")

    def test_prepared_artifact_recovers_after_final_marker_write_interruption(self):
        temporary = self.fixture.root / ".stage.mp4"
        artifact = self.fixture.root / "artifact.mp4"
        marker = self.fixture.root / "artifact.json"
        prepare = self.fixture.root / "artifact.prepare.json"
        temporary.write_bytes(b"durable-stage")
        identity = {"kind": "test", "sha256": "a" * 64}
        result = {"sha256": hashlib.sha256(b"durable-stage").hexdigest(), "size_bytes": 13}
        original = gpu_compositor.save_completed
        with mock.patch.object(gpu_compositor, "save_completed", side_effect=OSError("power loss")):
            with self.assertRaises(OSError):
                gpu_compositor._commit_prepared_artifact(
                    temporary, artifact, marker, prepare, identity, result
                )
        self.assertTrue(artifact.is_file())
        self.assertTrue(prepare.is_file())
        with mock.patch.object(gpu_compositor, "save_completed", side_effect=original) as save:
            recovered = gpu_compositor._recover_prepared_artifact(
                artifact, marker, prepare, temporary, identity
            )
        self.assertEqual(recovered, result)
        self.assertTrue(marker.is_file())
        self.assertFalse(prepare.exists())
        save.assert_called_once()

    def test_opencl_failure_never_falls_back_to_legacy_renderer(self):
        failure = DramaSynthesisError("drama_render_chunk_failed", "failed", 502)
        with mock.patch.dict(os.environ, {"DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2"}), \
                mock.patch.object(gpu_compositor, "render_chunked_random_output", side_effect=failure), \
                mock.patch.object(gpu, "_render_random_output_legacy") as legacy:
            with self.assertRaises(DramaSynthesisError) as caught:
                gpu.render_random_output(
                    source=self.fixture.source, output=self.fixture.output,
                    recipe=self.fixture.recipe, asset_root=self.fixture.root,
                    manifest_sha256="b" * 64,
                )
        self.assertEqual(caught.exception.code, "drama_render_chunk_failed")
        legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

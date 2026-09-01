#!/usr/bin/env python3
"""CPU-only package/HTTP tests: no installed ML stack, models, or external API."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_drama_synthesis_gpu_runtime as runtime
from scripts import demucs_extract_vocals as demucs_adapter
from features.drama_synthesis import async_runtime
from features.drama_synthesis.core import DramaSynthesisError


def load_fake_worker(fake_app):
    spec = importlib.util.spec_from_file_location(
        "_drama_gpu_runtime_test", ROOT / "scripts/drama_synthesis_gpu_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"app": fake_app}), mock.patch.dict(
        os.environ, {
            "DRAMA_GPU_MAX_CONCURRENCY": "1",
            "DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2",
            "DRAMA_GPU_COMPOSITOR_LANES": "4",
            "DRAMA_GPU_FILTER_THREADS": "2",
            "DRAMA_GPU_CHUNK_SECONDS": "120",
        }
    ):
        spec.loader.exec_module(module)
    return module


class DemucsAdapterTests(unittest.TestCase):
    def test_silence_and_near_silence_use_finite_unit_scale(self):
        for reference_std, signal_std in ((0, 0), (1e-12, 1e-12)):
            self.assertEqual(demucs_adapter.normalization_parameters(0, reference_std, signal_std), (0, 1))

    def test_antiphase_stereo_keeps_its_nonzero_channel_scale(self):
        self.assertEqual(demucs_adapter.normalization_parameters(0, 0, 0.5), (0, 0.5))

    def test_normal_audio_preserves_reference_normalization(self):
        self.assertEqual(demucs_adapter.normalization_parameters(0.1, 0.3, 0.5), (0.1, 0.3))

    def test_nonfinite_or_invalid_audio_statistics_are_rejected(self):
        for values in ((float("nan"), 0, 0), (0, float("inf"), 0),
                       (0, 0, float("nan")), (0, -1, 1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                demucs_adapter.normalization_parameters(*values)

    def test_original_positional_and_option_protocol(self):
        args = demucs_adapter.parse_args([
            "input.wav", "vocals.wav", "-n", "mdx_extra_q", "-d", "cuda",
            "--segment", "8", "--shifts", "1", "-j", "0", "--overlap", "0.25",
        ])
        self.assertEqual(args.input, Path("input.wav"))
        self.assertEqual(args.output, Path("vocals.wav"))
        self.assertEqual((args.name, args.device, args.segment, args.shifts, args.jobs),
                         ("mdx_extra_q", "cuda", 8, 1, 0))
        self.assertEqual(args.overlap, 0.25)

    def test_legacy_default_model_and_dynamic_device_are_preserved(self):
        args = demucs_adapter.parse_args(["input.wav", "vocals.wav"])
        self.assertEqual(args.name, "htdemucs")
        self.assertIsNone(args.device)
        self.assertIsNone(args.segment)

    def test_help_does_not_need_site_packages(self):
        proc = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/demucs_extract_vocals.py"), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--repo", proc.stdout)

    def test_required_local_repository_fails_before_ml_import(self):
        with mock.patch.dict(os.environ, {"DEMUCS_REQUIRE_LOCAL_MODELS": "1", "DEMUCS_MODEL_REPO": ""}):
            with self.assertRaisesRegex(ValueError, "required for offline"):
                demucs_adapter.main(["input.wav", "vocals.wav"])

    def test_local_repository_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            env = {"DEMUCS_REQUIRE_LOCAL_MODELS": "1", "DEMUCS_MODEL_REPO": first}
            self.assertEqual(demucs_adapter.resolve_model_repo(environ=env), Path(first).resolve())
            self.assertEqual(demucs_adapter.resolve_model_repo(second, environ=env), Path(second).resolve())
            self.assertIsNone(demucs_adapter.resolve_model_repo(environ={}))

    def test_missing_and_relative_repository_never_fall_back_to_download(self):
        for path in ("missing-relative-models", str(ROOT / "does-not-exist-models")):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "existing absolute"):
                demucs_adapter.resolve_model_repo(path, environ={})

    def test_local_model_is_explicitly_passed_to_demucs(self):
        with tempfile.TemporaryDirectory() as repo:
            get_model = mock.Mock(side_effect=RuntimeError("stop before inference"))
            modules = {
                "soundfile": SimpleNamespace(),
                "torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)),
                "demucs.apply": SimpleNamespace(BagOfModels=type("FakeBag", (), {}), apply_model=mock.Mock()),
                "demucs.pretrained": SimpleNamespace(get_model=get_model),
            }
            with mock.patch.dict(sys.modules, modules), self.assertRaisesRegex(RuntimeError, "stop before inference"):
                demucs_adapter.main(["in.wav", "out.wav", "-n", "mdx_extra_q", "--repo", repo, "-d", "cuda"])
            get_model.assert_called_once_with("mdx_extra_q", repo=Path(repo).resolve())


class RuntimePackageTests(unittest.TestCase):
    def fake_gpu_module(self, original):
        fake_builder = mock.Mock(return_value=original)
        spec = importlib.util.spec_from_file_location(
            "features.drama_synthesis._thread_budget_test", ROOT / "features/drama_synthesis/gpu.py"
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "features.fb_gpu.prepare_worker": SimpleNamespace(build_command=fake_builder),
        }):
            spec.loader.exec_module(module)
        return module, fake_builder

    def test_drama_random_graph_bounds_threads_without_changing_fb_command(self):
        graph = "[0:v]setpts=PTS-STARTPTS,fps=30[v];[2:v]setpts=PTS-STARTPTS[a]"
        original = ["ffmpeg", "-y", "-i", "source.mp4", "-filter_complex", graph, "output.mp4"]
        module, fake_builder = self.fake_gpu_module(original)
        args = (object(), "source", "output", {}, {}, {})
        actual = module.build_drama_random_command(*args)
        fake_builder.assert_called_once_with(*args)
        expected = list(original)
        expected[5] = graph.replace("[0:v]setpts=PTS-STARTPTS,", "[0:v]setpts=PTS,", 1)
        self.assertEqual(actual, ["ffmpeg", "-filter_complex_threads", "2", *expected[1:]])
        self.assertEqual(original[5], graph)
        self.assertIn("[2:v]setpts=PTS-STARTPTS", actual[7])
        self.assertNotIn("-copyts", actual)
        self.assertNotIn("-start_at_zero", actual)

    def test_drama_random_graph_drift_or_duplicate_source_reset_fails_closed(self):
        for graph in ("[0:v]fps=30[v]", "[0:v]setpts=PTS-STARTPTS,[0:v]setpts=PTS-STARTPTS,"):
            module, _ = self.fake_gpu_module(["ffmpeg", "-filter_complex", graph])
            with self.subTest(graph=graph), self.assertRaisesRegex(Exception, "配置不兼容"):
                module.build_drama_random_command(None, None, None, None, None, None)

    def test_random_duration_allows_rounding_but_rejects_lost_intro_and_audio_padding(self):
        module, _ = self.fake_gpu_module([])
        self.assertTrue(module.random_output_duration_matches(5.021016, 5.0, "5.000000"))
        self.assertFalse(module.random_output_duration_matches(5.021016, 3.966667, "3.966667"))
        self.assertFalse(module.random_output_duration_matches(5.021016, 5.021016, "3.966667"))
        self.assertFalse(module.random_output_duration_matches(7200, 7199, "7199"))
        for value in (None, 0, float("nan"), float("inf")):
            self.assertFalse(module.random_output_duration_matches(5, 5, value))

    def test_direct_dependencies_are_exact_not_a_transitive_lock_claim(self):
        deps = runtime.direct_requirements()
        self.assertEqual(deps["torch"], "2.5.1+cu124")
        self.assertEqual(deps["torchaudio"], "2.5.1+cu124")
        self.assertEqual(deps["demucs"], "4.0.1")
        self.assertEqual(deps["diffq"], "0.2.4")
        self.assertEqual(deps["numpy"], "1.26.4")
        self.assertIn("cos-python-sdk-v5", deps)
        text = (runtime.PACKAGE / "requirements-direct-cu124.txt").read_text(encoding="utf-8")
        self.assertIn("NOT a complete transitive lock", text)

    def test_package_versions_are_metadata_only_and_exact(self):
        self.assertEqual(runtime.package_issues({"torch": "2.5.1+cu124"}, lambda _: "2.5.1+cu124"), [])
        self.assertEqual(runtime.package_issues({"torch": "2.5.1+cu124"}, lambda _: "2.5.1"),
                         ["package_version_mismatch:torch"])
        def missing(_):
            raise runtime.importlib.metadata.PackageNotFoundError()
        self.assertEqual(runtime.package_issues({"demucs": "4.0.1"}, missing), ["missing_package:demucs"])

    def test_path_check_rejects_escape_and_prefix_lookalike(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            self.assertTrue(runtime.path_in_root(root / "work/jobs", root))
            self.assertFalse(runtime.path_in_root(root / "../outside", root))
            self.assertFalse(runtime.path_in_root(Path(str(root) + "-other") / "work", root))
            self.assertFalse(runtime.path_in_root("relative/path", root))

    def test_release_identity_binds_code_root_to_exact_release_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sha = "d" * 40
            release = base / "releases" / sha
            release.mkdir(parents=True)
            self.assertEqual(
                runtime.release_identity_issues(
                    {"DRAMA_GPU_RELEASE_SHA": sha}, code_root=release, base=base
                ), []
            )
            self.assertEqual(
                runtime.release_identity_issues(
                    {"DRAMA_GPU_RELEASE_SHA": sha}, code_root=base, base=base
                ), ["release_identity_mismatch"]
            )

    def test_storage_gate_binds_writable_paths_to_available_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {}
            for key in runtime.WRITABLE_DIRECTORY_KEYS:
                path = root / key.lower()
                path.mkdir()
                env[key] = str(path)
            self.assertEqual(
                runtime.storage_issues(env, base=root, minimum_free_bytes=1), []
            )
            self.assertEqual(
                runtime.storage_issues(env, base=root, minimum_free_bytes=2 ** 63),
                ["isolated_storage_low_space"],
            )

    def test_empty_environment_fails_closed_without_disclosing_values(self):
        issues = runtime.validate_environment({"GPU_VIDEO_WORKER_TOKEN": "fake-private-value"})
        self.assertIn("invalid:DEMUCS_REQUIRE_LOCAL_MODELS", issues)
        self.assertIn("outside_isolated_root:DEMUCS_PYTHON", issues)
        self.assertNotIn("fake-private-value", json.dumps(issues))

    def test_complete_template_passes_without_creating_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lines = (runtime.PACKAGE / "worker.env.example").read_text(encoding="utf-8").splitlines()
            env = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
            env = {key: value.replace(runtime.BASE.as_posix(), str(root)) for key, value in env.items()}
            for key in runtime.DIRECTORY_KEYS + runtime.FILE_KEYS + ("DRAMA_JOB_DB_PATH",):
                self.assertTrue(runtime.path_in_root(env[key], root), key)
            for key in runtime.REQUIRED_VALUES:
                if not env[key]:
                    env[key] = "fake-test-value"
            env["DRAMA_GPU_RELEASE_SHA"] = "c" * 40
            env["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"] = "a" * 64
            for key in runtime.DIRECTORY_KEYS:
                Path(env[key]).mkdir(parents=True, exist_ok=True)
            for key in runtime.FILE_KEYS:
                path = Path(env[key])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake binary for path validation only")
                path.chmod(0o700)
            with mock.patch.object(runtime, "valid_nvidia_smi", return_value=True):
                self.assertEqual(runtime.validate_environment(env, root=root), [])
            self.assertFalse(Path(env["DRAMA_JOB_DB_PATH"]).exists())

    def test_compositor_capability_check_uses_opencl_and_nvenc_without_media_output(self):
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        env = {
            "DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2",
            "DRAMA_FFMPEG": "/isolated/ffmpeg",
            "DRAMA_GPU_OPENCL_DEVICE": "0.0",
        }
        self.assertEqual(runtime.compositor_capability_issues(env, runner), [])
        command = runner.call_args.args[0]
        self.assertIn("opencl=ocl:0.0", command)
        self.assertIn("h264_nvenc", command)
        self.assertEqual(command[-2:], ["null", "-"])
        runner.return_value.returncode = 1
        self.assertEqual(
            runtime.compositor_capability_issues(env, runner),
            ["gpu_compositor_capability_check_failed"],
        )

    def test_compositor_pipeline_check_exercises_five_inputs_and_strict_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            categories = {}
            for category, media_type in (
                ("border", "image/png"), ("opacity_video", "video/webm"),
                ("corners", "video/webm"), ("tint", "image/png"),
            ):
                path = root / (category + (".png" if media_type == "image/png" else ".webm"))
                path.write_bytes(category.encode())
                categories[category] = ({
                    "media_type": media_type, "name": path.name, "sha256": "a" * 64,
                    "size": path.stat().st_size, "path": path,
                },)
            asset_set = {"manifest_sha256": "b" * 64, "categories": categories}
            commands = []

            def runner(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"media")
                return SimpleNamespace(returncode=0)

            def probe(_ffprobe, path):
                path = Path(path)
                if path.suffix == ".webm":
                    return {"duration": 5.0, "has_audio": False, "video": {"codec_name": "vp9"}}
                if path.name == "source.mp4":
                    return {
                        "duration": 1.0, "has_audio": False,
                        "video": {"codec_name": "mpeg4", "width": 720, "height": 1280},
                    }
                return {
                    "duration": 1.0, "has_audio": False, "audio": None,
                    "first_packet_keyframe": True,
                    "video": {
                        "codec_name": "h264", "profile": "High", "width": 720, "height": 1280,
                        "pix_fmt": "yuv420p", "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                        "nb_frames": "30", "level": 31, "time_base": "1/15360",
                        "codec_tag_string": "avc1", "extradata_size": 45,
                        "extradata_hash": "SHA256:" + "d" * 64, "is_avc": "true",
                        "nal_length_size": "4", "color_range": "tv",
                        "color_space": "bt709", "color_transfer": "bt709",
                        "color_primaries": "bt709", "chroma_location": "left",
                        "field_order": "progressive", "has_b_frames": 0,
                    },
                }

            env = {
                "DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2",
                "DRAMA_FFMPEG": "/isolated/ffmpeg", "DRAMA_FFPROBE": "/isolated/ffprobe",
                "DRAMA_GPU_OPENCL_DEVICE": "0.0", "TMPDIR": str(root),
            }
            self.assertEqual(
                runtime.compositor_pipeline_issues(env, asset_set, runner=runner, probe=probe), []
            )
            self.assertEqual(len(commands), 2)
            graph = commands[1][commands[1].index("-filter_complex") + 1]
            self.assertIn("program_opencl=inputs=5", graph)
            self.assertIn("h264_nvenc", commands[1])

    def test_legacy_backend_skips_gpu_capability_probe(self):
        runner = mock.Mock()
        self.assertEqual(runtime.compositor_capability_issues({
            "DRAMA_GPU_COMPOSITOR_BACKEND": "legacy_cpu",
        }, runner), [])
        runner.assert_not_called()

    def test_publication_and_unified_sync_require_explicit_zero(self):
        for key in ("YOUTUBE_LIVE_ENABLED", "DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED"):
            self.assertIn(f"forbidden:{key}", runtime.validate_environment({}))
            self.assertIn(f"forbidden:{key}", runtime.validate_environment({key: "1"}))
            self.assertNotIn(f"forbidden:{key}", runtime.validate_environment({key: "0"}))

    def test_outbound_worker_route_and_cpu_short_links_are_forbidden(self):
        issues = runtime.validate_environment({
            "GPU_VIDEO_WORKER_URL": "http://127.0.0.1:18787",
            "DRAMA_SHORT_LINK_ROOT": "/cpu/public", "DRAMA_SHORT_LINK_OWNER": "other",
            "TT_DRAMA_RESOURCE_SOURCE": "w2a_cache",
        })
        self.assertIn("forbidden:GPU_VIDEO_WORKER_URL", issues)
        self.assertIn("forbidden:cpu_short_link_configuration", issues)
        self.assertIn("forbidden:resource_cache_initialization", issues)

    def test_model_repository_checks_bag_bytes_and_checkpoint_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag = root / "bag.yaml"
            template = root / "template.yaml"
            text = "models: ['12345678']\nsegment: 44\n"
            bag.write_text(text, encoding="utf-8")
            template.write_text(text, encoding="utf-8")
            data = b"fake checkpoint, no torch involved"
            digest = hashlib.sha256(data).hexdigest()
            name = f"12345678-{digest[:8]}.th"
            checkpoint = root / name
            checkpoint.write_bytes(data)
            sources = {"bag_file": "bag.yaml", "files": [{"name": name, "sha256_prefix": digest[:8], "sha256": digest}]}
            hashes = runtime.check_model_repository(root, sources, template)
            self.assertEqual(hashes[name], digest)
            checkpoint.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum_mismatch"):
                runtime.check_model_repository(root, sources, template)
            checkpoint.write_bytes(data)
            (root / "12345678.th").write_bytes(data)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                runtime.check_model_repository(root, sources, template)

    def test_missing_model_bag_fails_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "bag_missing"):
                runtime.check_model_repository(directory, {"bag_file": "absent.yaml"}, "unused")

    def test_official_model_file_inventory(self):
        sources = json.loads((runtime.PACKAGE / "model-sources.json").read_text(encoding="utf-8"))
        self.assertEqual(sources["model"], "mdx_extra_q")
        self.assertEqual(len(sources["files"]), 4)
        for entry in sources["files"]:
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(entry["sha256"].startswith(entry["sha256_prefix"]))
        self.assertEqual({row["name"].split("-")[0] for row in sources["files"]},
                         {"83fc094f", "464b36d7", "14fc6a69", "7fd6ef75"})

    def test_service_and_environment_use_the_same_isolated_runtime(self):
        unit = (ROOT / "deploy/drama-synthesis-gpu-worker.service").read_text(encoding="utf-8")
        env = (runtime.PACKAGE / "worker.env.example").read_text(encoding="utf-8")
        python = "/data/drama-synthesis-gpu/runtime/current/bin/python"
        self.assertIn(f"ExecStart={python} ", unit)
        self.assertIn(f"ExecStartPre={python} ", unit)
        self.assertIn(f"DEMUCS_PYTHON={python}", env)
        self.assertIn("ProtectHome=yes", unit)
        self.assertIn("DRAMA_GPU_MAX_CONCURRENCY=1", env)
        self.assertIn("DRAMA_GPU_COMPOSITOR_BACKEND=opencl_fused_v2", env)
        self.assertIn("DRAMA_GPU_COMPOSITOR_LANES=4", env)
        self.assertIn("DRAMA_GPU_FILTER_THREADS=2", env)
        self.assertIn("DRAMA_GPU_COMPOSITOR_CACHE_ROOT=/data/drama-synthesis-gpu/work/compositor-cache", env)
        self.assertIn("CPUQuota=800%", unit)
        self.assertIn("TasksMax=512", unit)
        self.assertIn("DEMUCS_REQUIRE_LOCAL_MODELS=1", env)
        self.assertNotIn("/usr/bin/python3.9", unit)
        self.assertNotIn("/root/", unit + env)
        for key in ("GPU_VIDEO_WORKER_TOKEN", "COS_SECRET_ID", "COS_SECRET_KEY"):
            self.assertRegex(env, rf"(?m)^{key}=$")


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.strict_cache = mock.Mock(return_value=None)
        self.legacy_cache = mock.Mock(return_value=None)
        self.fake_app = SimpleNamespace(
            WORK_ROOT=self.directory.name,
            cached_gpu_video_result=self.legacy_cache,
            strict_cached_gpu_video_result=self.strict_cache,
            gpu_video_resume_ready=mock.Mock(return_value=True),
            drama_random_template_catalog=mock.Mock(return_value={"version": 1}),
            handle_gpu_video_render=mock.Mock(return_value={"ok": True}),
            handle_gpu_video_cover=mock.Mock(return_value={"ok": True}),
        )
        self.worker = load_fake_worker(self.fake_app)
        self.env = mock.patch.dict(os.environ, {"GPU_VIDEO_WORKER_TOKEN": "test-token"})
        self.env.start()
        self.server = self.worker.ThreadingHTTPServer(("127.0.0.1", 0), self.worker.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.worker.RUNTIME is not None:
            self.assertTrue(self.worker.RUNTIME.close(timeout=3))
        self.directory.cleanup()
        self.env.stop()

    def request(self, method, path, payload=None, authorization="Bearer test-token"):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {"Authorization": authorization, "Content-Type": "application/json"}
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_valid_render_and_auth_scheme(self):
        self.assertEqual(self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32})[0], 200)
        self.assertEqual(self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32}, "test-token")[0], 401)
        self.fake_app.handle_gpu_video_render.assert_called_once()

    def test_busy_render_is_rejected_but_cover_catalog_health_bypass(self):
        self.assertTrue(self.worker.RENDER_SLOTS.acquire(blocking=False))
        try:
            status, payload = self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32})
            self.assertEqual((status, payload["code"]), (503, "gpu_render_busy"))
            self.fake_app.handle_gpu_video_render.assert_not_called()
            self.assertEqual(self.request("POST", "/api/gpu-video/cover", {"job_id": "a" * 32})[0], 200)
            status, health = self.request("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(health["compositor_backend"], "opencl_fused_v2")
            self.assertEqual(health["compositor_chunk_seconds"], 120)
            self.assertEqual(health["compositor_lanes"], 4)
            self.assertEqual(health["render_concurrency"], 1)
            self.assertEqual(health["renderer_profile"], "drama-opencl-fused-h264-720x1280-v2")
            self.assertRegex(health["kernel_template_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(health["release_sha"], "")
            self.assertEqual(health["runtime_identity"], "ffmpeg-opencl-nvenc-runtime-v1")
            self.assertEqual(self.request("GET", "/api/gpu-video/random-overlay/catalog")[0], 200)
        finally:
            self.worker.RENDER_SLOTS.release()

    def test_render_slot_released_after_error_without_internal_details(self):
        self.fake_app.handle_gpu_video_render.side_effect = RuntimeError("private stack and credential")
        status, payload = self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32})
        self.assertEqual(status, 500)
        self.assertNotIn("private", json.dumps(payload))
        self.fake_app.handle_gpu_video_render.side_effect = None
        self.assertEqual(self.request("POST", "/api/gpu-video/render", {"job_id": "b" * 32})[0], 200)

    def test_cover_callback_unblocks_a_running_render(self):
        entered, cover = threading.Event(), threading.Event()
        responses = []

        def wait_for_cover(_payload):
            entered.set()
            if not cover.wait(timeout=2):
                raise RuntimeError("cover callback was blocked by the render slot")
            return {"ok": True}

        def complete_cover(_payload):
            cover.set()
            return {"ok": True}

        self.fake_app.handle_gpu_video_render.side_effect = wait_for_cover
        self.fake_app.handle_gpu_video_cover.side_effect = complete_cover
        caller = threading.Thread(target=lambda: responses.append(
            self.request("POST", "/api/gpu-video/render", {"job_id": "c" * 32})
        ))
        caller.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(self.request("POST", "/api/gpu-video/cover", {"job_id": "c" * 32})[0], 200)
        finally:
            cover.set()
            caller.join(timeout=3)
        self.assertEqual(responses, [(200, {"ok": True})])

    def test_job_id_validation_rejects_traversal_and_non_strings_on_both_routes(self):
        for job_id in ("../other", "..", "/absolute", "a/b", "a\\b", "a b", "", None, 123, True, "a" * 129):
            for route in ("render", "cover", "jobs"):
                with self.subTest(job_id=job_id, route=route):
                    status, result = self.request("POST", f"/api/gpu-video/{route}", {"job_id": job_id})
                    self.assertEqual((status, result["code"]), (400, "invalid_job_id"))
        self.fake_app.handle_gpu_video_render.assert_not_called()
        self.fake_app.handle_gpu_video_cover.assert_not_called()

    def test_media_only_surface_and_object_payload(self):
        self.assertEqual(self.request("POST", "/api/cpu/operation", {"job_id": "a" * 32})[0], 404)
        self.assertEqual(self.request("POST", "/api/gpu-video/render", ["a" * 32])[0], 400)

    def test_content_id_rejects_path_control_and_oversize_but_preserves_unicode(self):
        for content_id in ("../escape", "a/b", "a\\b", "bad\x00id", "bad\nid", "bad\x7fid", "a" * 201, "剧" * 67):
            with self.subTest(content_id=content_id):
                status, payload = self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32, "content_id": content_id})
                self.assertEqual((status, payload["code"]), (400, "invalid_content_id"))
        self.fake_app.handle_gpu_video_render.assert_not_called()
        for index, content_id in enumerate((None, "", "剧集-咖啡_第1部", "c3d8e5ed-5f0e-4a04-b08b-3b27f4e90abc", 123456)):
            with self.subTest(content_id=content_id):
                self.assertEqual(self.request("POST", "/api/gpu-video/render", {"job_id": "content-" + str(index), "content_id": content_id})[0], 200)

    def test_concurrency_bounds_fail_closed(self):
        self.assertEqual(self.worker.render_concurrency({}), 1)
        self.assertEqual(self.worker.render_concurrency({"DRAMA_GPU_MAX_CONCURRENCY": "2"}), 2)
        self.assertEqual(self.worker.render_concurrency({
            "DRAMA_GPU_MAX_CONCURRENCY": "1", "DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2",
        }), 1)
        with self.assertRaises(ValueError):
            self.worker.render_concurrency({
                "DRAMA_GPU_MAX_CONCURRENCY": "2", "DRAMA_GPU_COMPOSITOR_BACKEND": "opencl_fused_v2",
            })
        for value in ("0", "3", "8", "9", "NaN", "", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.worker.render_concurrency({"DRAMA_GPU_MAX_CONCURRENCY": value})

    def test_async_submit_response_loss_reattaches_without_duplicate_and_cover_still_works(self):
        entered, finish = threading.Event(), threading.Event()
        self.fake_app.handle_gpu_video_render.side_effect = lambda payload: (
            entered.set(), finish.wait(3), result_for(payload)
        )[-1]
        payload = render_payload()
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            client.request("POST", "/api/gpu-video/jobs", json.dumps(payload), {
                "Content-Type": "application/json", "Authorization": "Bearer test-token",
            })
            client.close()  # no getresponse: production response-loss scenario
            self.assertTrue(entered.wait(2))
            status, current = self.request("GET", "/api/gpu-video/jobs/" + payload["job_id"])
            self.assertEqual((status, current["status"], current["generation"]), (200, "running", 1))
            self.assertEqual(self.request("POST", "/api/gpu-video/jobs", payload)[0], 202)
            status, body = self.request("POST", "/api/gpu-video/render", payload)
            self.assertEqual((status, body["code"]), (503, "gpu_job_running"))
            self.assertEqual(self.request("POST", "/api/gpu-video/cover", {"job_id": payload["job_id"]})[0], 200)
            self.fake_app.handle_gpu_video_render.assert_called_once()
        finally:
            client.close()
            finish.set()
        wait_for(lambda: self.worker.RUNTIME.get(payload["job_id"])["status"] == "completed")
        status, completed = self.request("POST", "/api/gpu-video/jobs", payload)
        self.assertEqual((status, completed["status"]), (202, "completed"))

    def test_worker_binds_strict_async_and_legacy_sync_cache_callbacks(self):
        runtime = self.worker.get_runtime()
        self.assertIs(runtime.cached_result, self.strict_cache)
        self.assertIs(runtime.sync_cached_result, self.legacy_cache)

    def test_async_auth_validation_query_and_resume_routes(self):
        self.assertEqual(self.request("POST", "/api/gpu-video/jobs", render_payload(), "bad")[0], 401)
        self.assertEqual(self.request("GET", "/api/gpu-video/jobs/unknown", authorization="bad")[0], 401)
        status, body = self.request("GET", "/api/gpu-video/jobs/unknown")
        self.assertEqual((status, body["code"]), (404, "gpu_job_not_found"))
        self.assertEqual(self.request("POST", "/api/gpu-video/jobs", {"job_id": "missing-input"})[0], 400)
        self.assertEqual(self.request("GET", "/api/gpu-video/jobs/../escape")[0], 400)
        payload = render_payload()
        self.fake_app.handle_gpu_video_render.side_effect = RuntimeError("private token https://source.test/secret")
        self.assertEqual(self.request("POST", "/api/gpu-video/jobs", payload)[0], 202)
        wait_for(lambda: self.worker.RUNTIME.get(payload["job_id"])["status"] == "failed")
        status, state = self.request("GET", "/api/gpu-video/jobs/" + payload["job_id"])
        self.assertNotIn("secret", json.dumps(state))
        resume = {**payload, "expected_generation": 1}
        self.assertEqual(self.request("POST", "/api/gpu-video/jobs/wrong/resume", resume)[0], 400)
        self.assertEqual(self.request("POST", "/api/gpu-video/jobs/" + payload["job_id"] + "/resume", payload)[0], 400)
        status, state = self.request("POST", "/api/gpu-video/jobs/" + payload["job_id"] + "/resume", resume)
        self.assertEqual((status, state["generation"]), (202, 2))
        status, repeated = self.request("POST", "/api/gpu-video/jobs/" + payload["job_id"] + "/resume", resume)
        self.assertEqual((status, repeated["generation"]), (202, 2))


def render_payload(job_id="async-fixture"):
    return {
        "job_id": job_id, "content_id": "drama-fixture", "episode_start": 1, "episode_end": 2,
        "outputs": {"concat_video": True, "no_bgm_video": False, "random_template_video": False},
        "episodes": [
            {"episode_number": 1, "episode_url": "https://source.example.test/1.mp4?token=private-source"},
            {"episode_number": 2, "episode_url": "https://source.example.test/2.mp4"},
        ],
        "await_cover_16x9": True,
    }


def result_for(payload):
    return {"job_id": payload["job_id"], "output_video_url": "https://output.example.test/finished.mp4"}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("local fixture did not reach expected state")
        threading.Event().wait(0.01)


class AsyncRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self, *, execute=None, cached=None, **kwargs):
        value = async_runtime.AsyncRuntime(
            self.directory.name, execute or mock.Mock(side_effect=result_for),
            cached or mock.Mock(return_value=None), **kwargs,
        )
        self.addCleanup(value.close, 3)
        return value

    def crashed(self, value, *, children=None, launches=None):
        """Fixture of a durable running record from a process that has exited."""
        payload = render_payload()
        value.submit(payload)
        with value._mutex:
            record = value._records[payload["job_id"]]
            value._begin(record)
            record.update(_owner="exited-runtime-owner", _children=children or {}, _launches=launches or {})
            value._save(record)
        self.assertTrue(value.close())
        return payload

    def test_fingerprint_freezes_order_and_source_but_accepts_late_cover_delivery(self):
        payload = render_payload()
        late = {**payload, "cover_16x9_url": "https://cover.test/new.jpg",
                "cover_wait_timeout": 9999, "expected_generation": 1}
        self.assertEqual(async_runtime.render_fingerprint(payload), async_runtime.render_fingerprint(late))
        value = self.runtime(autostart=False)
        self.assertEqual(value.submit(payload), value.submit(late))
        for changed in ({**payload, "episodes": list(reversed(payload["episodes"]))},
                        {**payload, "await_cover_16x9": False},
                        {**late, "await_cover_16x9": False},
                        {**payload, "episode_end": 3}):
            self.assertNotEqual(async_runtime.render_fingerprint(payload), async_runtime.render_fingerprint(changed))

    def test_initial_fixed_cover_url_is_frozen_and_cannot_be_rebound(self):
        value = self.runtime(autostart=False)
        for field in ("cover_16x9_url", "cover_url"):
            payload = {**render_payload("fixed-" + field), "await_cover_16x9": False,
                       field: "https://cover.test/first.jpg"}
            first = value.submit(payload)
            for changes in ({field: "https://cover.test/other.jpg"}, {"await_cover_16x9": True}):
                with self.subTest(field=field, changes=changes), self.assertRaises(DramaSynthesisError) as caught:
                    value.submit({**payload, **changes})
                self.assertEqual((caught.exception.code, caught.exception.status), ("gpu_job_input_conflict", 409))
            self.assertEqual(value.get(payload["job_id"]), first)
        value.execute.assert_not_called()

    def test_explicit_download_route_is_identity_but_legacy_payload_is_unchanged(self):
        payload = render_payload()
        # Frozen pre-route identity: old queued jobs must not gain a route key.
        self.assertEqual(async_runtime.render_fingerprint(payload),
                         "a53b662a5d30a329d915b57d72cbe6f7bb0acb433c6b628310ba7f2eb418aba2")
        self.assertNotIn("download_route", async_runtime.canonical_render_payload(payload)["episodes"][0])
        source = "https://img.tianmai.cn/resource/Abc/001.mp4"
        payload["episodes"][0]["episode_url"] = source
        legacy_identity = async_runtime.render_fingerprint(payload)
        route = {"version": 1, "source_url": source, "primary_url": source, "fallback_url": ""}
        payload["episodes"][0]["download_route"] = route
        self.assertNotEqual(async_runtime.render_fingerprint(payload), legacy_identity)
        self.assertEqual(async_runtime.canonical_render_payload(payload)["episodes"][0]["download_route"], route)
        value = self.runtime(autostart=False)
        first = value.submit(payload)
        for updates in ({"primary_url": source.replace("img.tianmai.cn", "accelerate.tianmai.cn")},
                        {"fallback_url": source}):
            changed = deepcopy(payload)
            changed["episodes"][0]["download_route"].update(updates)
            with self.subTest(updates=updates), self.assertRaises(DramaSynthesisError) as caught:
                value.submit(changed)
            self.assertEqual((caught.exception.code, caught.exception.status), ("gpu_job_input_conflict", 409))
        self.assertEqual(value.get(payload["job_id"]), first)
        value.execute.assert_not_called()

    def test_malformed_download_route_rejects_before_cache_or_execution(self):
        value = self.runtime(autostart=False)
        source = render_payload()["episodes"][0]["episode_url"]
        route = {"version": 1, "source_url": source, "primary_url": source, "fallback_url": ""}
        invalid = [None, [], "original", {}, {**route, "version": True}, {**route, "version": 2},
                   {**route, "extra": "ignored"}, {**route, "source_url": source + "changed"},
                   {**route, "primary_url": []}, {**route, "primary_url": "file:///private"},
                   {**route, "primary_url": "https://user:secret@source.test/1.mp4"},
                   {**route, "primary_url": "https://source.test:99999/1.mp4"},
                   {**route, "primary_url": source + "\n"}, {**route, "fallback_url": None},
                   {**route, "fallback_url": " "}, {**route, "fallback_url": "https://source.test/1.mp4#part"}]
        for index, malformed in enumerate(invalid):
            payload = render_payload()
            payload["episodes"][0]["download_route"] = malformed
            with self.subTest(index=index), self.assertRaises(DramaSynthesisError) as caught:
                value.submit(payload)
            self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_request", 400))
        value.cached_result.assert_not_called()
        value.execute.assert_not_called()

    def test_persist_before_accept_and_duplicate_and_conflict_do_not_mutate(self):
        value = self.runtime(autostart=False)
        payload = render_payload()
        first = value.submit(payload)
        path = value.root / "jobs" / (payload["job_id"] + ".json")
        before = path.read_bytes()
        self.assertEqual(json.loads(before)["status"], "queued")
        self.assertEqual(value.submit(payload), first)
        self.assertEqual(path.read_bytes(), before)
        changed = deepcopy(payload)
        changed["episodes"][0]["episode_url"] += "changed"
        with self.assertRaises(DramaSynthesisError) as caught:
            value.submit(changed)
        self.assertEqual(caught.exception.code, "gpu_job_input_conflict")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("private-source", json.dumps(value.get(payload["job_id"])))
        self.assertNotIn("_payload", first)
        value.execute.assert_not_called()

    def test_two_dispatchers_execute_distinct_queued_jobs_in_parallel(self):
        mutex = threading.Lock()
        release = threading.Event()
        simultaneous = threading.Event()
        active = 0
        maximum = 0

        def execute(payload):
            nonlocal active, maximum
            with mutex:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    simultaneous.set()
            if not release.wait(3):
                raise RuntimeError("parallel fixture timed out")
            with mutex:
                active -= 1
            return result_for(payload)

        value = self.runtime(
            execute=execute,
            render_slots=threading.BoundedSemaphore(2),
            dispatcher_workers=2,
        )
        first = render_payload("parallel-one")
        second = render_payload("parallel-two")
        value.submit(first)
        value.submit(second)
        try:
            self.assertTrue(simultaneous.wait(2))
        finally:
            release.set()
        wait_for(lambda: value.get(first["job_id"])["status"] == "completed")
        wait_for(lambda: value.get(second["job_id"])["status"] == "completed")
        self.assertEqual(maximum, 2)

    def test_concurrent_duplicate_submits_execute_exactly_once(self):
        entered, finish = threading.Event(), threading.Event()
        execute = mock.Mock(side_effect=lambda payload: (entered.set(), finish.wait(3), result_for(payload))[-1])
        value = self.runtime(execute=execute)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                replies = list(pool.map(lambda _: value.submit(render_payload()), range(16)))
            self.assertTrue(entered.wait(2))
            self.assertEqual({row["generation"] for row in replies}, {1})
            execute.assert_called_once()
        finally:
            finish.set()
        wait_for(lambda: value.get("async-fixture")["status"] == "completed")
        original = value.get("async-fixture")
        self.assertEqual(value.submit(render_payload()), original)

    def test_completed_manifest_and_get_bypass_busy_slot_and_full_queue(self):
        slots = threading.BoundedSemaphore(1)
        self.assertTrue(slots.acquire(False))
        self.addCleanup(slots.release)
        cache = mock.Mock(side_effect=lambda payload: result_for(payload) if payload["job_id"] == "cached" else None)
        value = self.runtime(render_slots=slots, queue_limit=1, cached=cache, autostart=False)
        pending = value.submit(render_payload("pending"))
        self.assertEqual(value.get("pending"), pending)
        with self.assertRaises(DramaSynthesisError) as caught:
            value.submit(render_payload("overflow"))
        self.assertEqual(caught.exception.code, "gpu_queue_full")
        self.assertEqual(value.submit(render_payload("cached"))["status"], "completed")
        self.assertEqual(value.run_sync(render_payload("cached")), result_for(render_payload("cached")))
        value.execute.assert_not_called()

    def test_async_and_legacy_sync_use_separate_cache_contracts(self):
        strict = mock.Mock(return_value=None)
        legacy = mock.Mock(side_effect=result_for)
        value = self.runtime(
            cached=strict, sync_cached_result=legacy, autostart=False,
        )
        async_payload = render_payload("async-strict")
        sync_payload = render_payload("legacy-compatible")
        self.assertEqual(value.submit(async_payload)["status"], "queued")
        self.assertEqual(value.run_sync(sync_payload), result_for(sync_payload))
        strict.assert_called_once_with(async_payload)
        legacy.assert_called_once_with(sync_payload)
        value.execute.assert_not_called()

    def test_invalid_completed_cache_is_never_a_render_miss(self):
        cache = mock.Mock(side_effect=async_runtime.runtime_error("gpu_result_cache_unverified"))
        value = self.runtime(cached=cache, autostart=False)
        with self.assertRaises(DramaSynthesisError) as caught:
            value.submit(render_payload())
        self.assertEqual(caught.exception.code, "gpu_result_cache_unverified")
        self.assertEqual(list((value.root / "jobs").glob("*.json")), [])
        value.execute.assert_not_called()

    def test_four_hours_is_not_an_execution_deadline_and_heartbeat_is_not_progress(self):
        clock, contexts = [1000.0], []
        entered, finish = threading.Event(), threading.Event()

        def execute(payload):
            contexts.append(async_runtime.capture_context())
            async_runtime.emit_progress("downloading", downloaded_bytes=100, total_bytes=1000,
                                        source_url="private-source", command="private-command")
            entered.set()
            finish.wait(3)
            return result_for(payload)

        value = self.runtime(execute=execute, clock=lambda: clock[0])
        value.submit(render_payload())
        try:
            self.assertTrue(entered.wait(2))
            initial = value.get("async-fixture")
            clock[0] += 14401
            with async_runtime.use_context(contexts[0]):
                async_runtime.emit_progress("downloading", downloaded_bytes=100, bytes_per_second=2)
            current = value.get("async-fixture")
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["started_at"], initial["started_at"])
            self.assertNotEqual(current["heartbeat_at"], initial["heartbeat_at"])
            self.assertEqual(current["last_progress_at"], initial["last_progress_at"])
            self.assertNotIn("private", json.dumps(current))
            with async_runtime.use_context(contexts[0]):
                async_runtime.emit_progress("downloading", downloaded_bytes=101)
            self.assertNotEqual(value.get("async-fixture")["last_progress_at"], initial["last_progress_at"])
        finally:
            finish.set()
        wait_for(lambda: value.get("async-fixture")["status"] == "completed")
        before = (value.root / "jobs/async-fixture.json").read_bytes()
        clock[0] += 100
        with async_runtime.use_context(contexts[0]):
            async_runtime.emit_progress("failed", percent=0)
        self.assertEqual((value.root / "jobs/async-fixture.json").read_bytes(), before)

    def test_same_stage_advancement_is_high_water_but_rates_refresh_and_new_stage_resets(self):
        clock, contexts = [1000.0], []
        entered, finish = threading.Event(), threading.Event()

        def execute(payload):
            contexts.append(async_runtime.capture_context())
            async_runtime.emit_progress(
                "rendering_random", out_time_seconds=120, frame=3600,
                bytes_done=5000, percent=50, fps=24.0, speed=0.5,
                duration_seconds=600,
            )
            entered.set()
            finish.wait(3)
            return result_for(payload)

        value = self.runtime(execute=execute, clock=lambda: clock[0])
        value.submit(render_payload())
        try:
            self.assertTrue(entered.wait(2))
            initial = value.get("async-fixture")
            clock[0] += 1
            with async_runtime.use_context(contexts[0]):
                async_runtime.emit_progress(
                    "rendering_random", out_time_seconds=119, frame=3599,
                    bytes_done=4999, percent=49, fps=30.0, speed=0.75,
                    duration_seconds=650,
                )
            current = value.get("async-fixture")
            self.assertEqual(
                {key: current["progress"][key]
                 for key in ("out_time_seconds", "frame", "bytes_done", "percent")},
                {"out_time_seconds": 120, "frame": 3600, "bytes_done": 5000, "percent": 50},
            )
            self.assertEqual(current["progress"]["fps"], 30.0)
            self.assertEqual(current["progress"]["speed"], 0.75)
            self.assertEqual(current["progress"]["duration_seconds"], 650)
            self.assertEqual(current["last_progress_at"], initial["last_progress_at"])

            clock[0] += 1
            with async_runtime.use_context(contexts[0]):
                async_runtime.emit_progress("uploading", uploaded_bytes=10, percent=5,
                                            fps=12.0, speed=0.2)
            changed = value.get("async-fixture")
            self.assertEqual(changed["stage"], "uploading")
            self.assertEqual(changed["progress"], {
                "uploaded_bytes": 10, "percent": 5, "fps": 12.0, "speed": 0.2,
            })
            self.assertNotEqual(changed["last_progress_at"], current["last_progress_at"])
        finally:
            finish.set()
        wait_for(lambda: value.get("async-fixture")["status"] == "completed")

    def test_queued_restart_preserves_generation_and_first_start(self):
        first = self.runtime(autostart=False)
        submitted = first.submit(render_payload())
        self.assertTrue(first.close())
        second = self.runtime(autostart=False)
        restored = second.get("async-fixture")
        self.assertEqual(restored, submitted)
        second.start()
        wait_for(lambda: second.get("async-fixture")["status"] == "completed")
        second.execute.assert_called_once()

    def test_stopped_running_process_recovers_once_with_new_generation(self):
        first = self.runtime(autostart=False)
        payload = self.crashed(first, children={"123": {"pid": 123, "start_ticks": "1", "boot_id": "old"}})
        second = self.runtime(process_probe=lambda _: "stopped", autostart=False)
        restored = second.get(payload["job_id"])
        self.assertEqual((restored["status"], restored["generation"]), ("queued", 2))
        self.assertIsNotNone(restored["started_at"])
        second.start()
        wait_for(lambda: second.get(payload["job_id"])["status"] == "completed")
        second.execute.assert_called_once()

    def test_restart_adopts_verified_manifest_without_new_execution(self):
        first = self.runtime(autostart=False)
        payload = self.crashed(first)
        started = first.get(payload["job_id"])["started_at"]
        second = self.runtime(cached=mock.Mock(side_effect=result_for), autostart=False)
        restored = second.get(payload["job_id"])
        self.assertEqual((restored["status"], restored["generation"], restored["started_at"]), ("completed", 1, started))
        second.execute.assert_not_called()

    def test_surviving_or_unknown_child_blocks_all_heavy_work_but_not_query(self):
        first = self.runtime(autostart=False)
        self.crashed(first, children={"123": {"pid": 123, "start_ticks": "1", "boot_id": "old"}})
        second = self.runtime(process_probe=lambda _: "alive", autostart=False)
        current = second.get("async-fixture")
        self.assertEqual(current["status"], "recovery_required")
        self.assertEqual(current["error"]["code"], "gpu_previous_process_running")
        second.submit(render_payload("other-job"))
        second.start()
        self.assertTrue(second._resource_blocked())
        self.assertEqual(second.get("other-job")["status"], "queued")
        with self.assertRaises(DramaSynthesisError):
            second.run_sync(render_payload("legacy-other"))
        second.execute.assert_not_called()

    def test_crash_between_popen_and_pid_capture_fails_closed(self):
        first = self.runtime(autostart=False)
        self.crashed(first, launches={"unresolved-launch": {"boot_id": async_runtime._boot_id()}})
        second = self.runtime(process_probe=lambda _: "stopped", autostart=False)
        current = second.get("async-fixture")
        self.assertEqual((current["status"], current["error"]["code"]), ("recovery_required", "gpu_process_state_unknown"))
        with self.assertRaises(DramaSynthesisError):
            second.resume(render_payload(), 1)
        second.execute.assert_not_called()

    def test_corrupt_ledger_blocks_start_without_overwrite(self):
        first = self.runtime(autostart=False)
        first.submit(render_payload())
        first.close()
        path = first.root / "jobs/async-fixture.json"
        path.write_text("corrupt existing record", encoding="utf-8")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.runtime(autostart=False)
        self.assertEqual(caught.exception.code, "gpu_runtime_unverified")
        self.assertEqual(path.read_text(), "corrupt existing record")

    def test_second_runtime_process_cannot_take_owner_lock(self):
        first = self.runtime(autostart=False)
        with self.assertRaises(DramaSynthesisError) as caught:
            self.runtime(autostart=False)
        self.assertEqual(caught.exception.code, "gpu_runtime_unavailable")
        first.close()
        second = self.runtime(autostart=False)
        self.assertEqual(second.submit(render_payload())["generation"], 1)

    def test_explicit_failed_resume_is_checkpoint_gated_and_replay_idempotent(self):
        execute = mock.Mock(side_effect=RuntimeError("private stack https://credentials.test/token"))
        value = self.runtime(execute=execute, can_resume=lambda _: True)
        value.submit(render_payload())
        wait_for(lambda: value.get("async-fixture")["status"] == "failed")
        first = value.get("async-fixture")
        self.assertNotIn("private", json.dumps(first))
        self.assertEqual(value.submit(render_payload()), first)
        self.assertEqual(value.resume(render_payload(), 1)["generation"], 2)
        wait_for(lambda: value.get("async-fixture")["status"] == "failed")
        self.assertEqual(value.resume(render_payload(), 1)["generation"], 2)
        self.assertEqual(execute.call_count, 2)
        value.can_resume = lambda _: False
        with self.assertRaises(DramaSynthesisError) as caught:
            value.resume(render_payload(), 2)
        self.assertEqual(caught.exception.code, "gpu_job_resume_unavailable")
        self.assertEqual(execute.call_count, 2)

    def test_live_child_cannot_be_cleared_and_exception_does_not_release_resource_guard(self):
        def execute(payload):
            with async_runtime.process_launch():
                async_runtime.record_process(123)
            async_runtime.clear_process(123)
            raise RuntimeError("private failure")

        identity = {"pid": 123, "start_ticks": "1", "boot_id": "boot"}
        with mock.patch.object(async_runtime, "process_identity", return_value=identity):
            value = self.runtime(execute=execute, process_probe=lambda _: "alive")
            value.submit(render_payload())
            wait_for(lambda: value.get("async-fixture")["status"] == "recovery_required")
        record = json.loads((value.root / "jobs/async-fixture.json").read_text(encoding="utf-8"))
        self.assertIn("123", record["_children"])
        self.assertEqual(record["_launches"], {})
        self.assertTrue(value._resource_blocked())
        self.assertNotIn("private", json.dumps(value.get("async-fixture")))

    def test_clear_process_only_accepts_confirmed_stopped_child(self):
        probe_state = ["stopped"]
        probe = mock.Mock(side_effect=lambda _: probe_state[0])
        value = self.runtime(autostart=False, process_probe=probe)
        identity = {"pid": 123, "start_ticks": "1", "boot_id": "boot"}

        for name, state, recorded, expected_code in (
                ("stopped", "stopped", True, None),
                ("alive", "alive", True, "gpu_previous_process_running"),
                ("unknown", "unknown", True, "gpu_process_state_unknown"),
                ("missing", "stopped", False, "gpu_process_state_unknown")):
            with self.subTest(name=name):
                payload = render_payload("clear-" + name)
                value.submit(payload)
                with value._mutex:
                    record = value._records[payload["job_id"]]
                    value._begin(record)
                    if recorded:
                        record["_children"]["123"] = dict(identity)
                        value._save(record)
                    context = async_runtime._ExecutionContext(
                        value, record["job_id"], record["generation"], value.instance,
                    )

                probe_state[0] = state
                calls_before = probe.call_count
                if expected_code is None:
                    value._process_event(context, 123, begin=False)
                else:
                    with self.assertRaises(DramaSynthesisError) as caught:
                        value._process_event(context, 123, begin=False)
                    self.assertEqual(caught.exception.code, expected_code)

                durable = json.loads(
                    (value.root / "jobs" / (payload["job_id"] + ".json")).read_text(encoding="utf-8")
                )
                if name == "stopped":
                    self.assertNotIn("123", durable["_children"])
                elif name == "missing":
                    self.assertEqual(durable["_children"]["123"], {"pid": 123})
                    self.assertEqual(probe.call_count, calls_before)
                else:
                    self.assertEqual(durable["_children"]["123"], identity)

                # Leave this direct private-method fixture in a terminal state
                # so runtime shutdown does not resemble an interrupted render.
                with value._mutex:
                    record.update(status="failed", stage="failed", _owner=None,
                                  _children={}, _launches={})
                    value._save(record)

    def test_context_propagation_and_stale_generation_fence(self):
        first_context, finish = [], threading.Event()
        entered = threading.Event()

        def execute(payload):
            context = async_runtime.capture_context()
            first_context.append(context)
            with ThreadPoolExecutor(max_workers=1) as pool:
                def progress():
                    with async_runtime.use_context(context):
                        async_runtime.emit_progress("normalizing", normalized_episodes=1, total_segments=2)
                pool.submit(progress).result()
            entered.set()
            finish.wait(3)
            return result_for(payload)

        value = self.runtime(execute=execute)
        value.submit(render_payload())
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(value.get("async-fixture")["progress"]["normalized_episodes"], 1)
            old = first_context[0]
            stale = async_runtime._ExecutionContext(value, old.job_id, old.generation - 1, old.owner)
            with async_runtime.use_context(stale):
                async_runtime.emit_progress("uploading", uploaded_bytes=999)
            self.assertEqual(value.get("async-fixture")["stage"], "normalizing")
        finally:
            finish.set()

    def test_stop_intake_preserves_accepted_queue_and_existing_queries(self):
        value = self.runtime(autostart=False)
        state = value.submit(render_payload())
        value.stop_intake()
        self.assertEqual(value.submit(render_payload()), state)
        self.assertEqual(value.get("async-fixture"), state)
        with self.assertRaises(DramaSynthesisError) as caught:
            value.submit(render_payload("new-after-stop"))
        self.assertEqual(caught.exception.code, "gpu_runtime_unavailable")

    def test_durable_write_failure_never_accepts_or_starts_an_in_memory_job(self):
        value = self.runtime(autostart=False)
        with mock.patch.object(async_runtime.os, "replace", side_effect=OSError("private disk path")):
            with self.assertRaises(DramaSynthesisError) as caught:
                value.submit(render_payload())
        self.assertEqual(caught.exception.code, "gpu_runtime_unverified")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(list((value.root / "jobs").glob("*.json")), [])
        with self.assertRaises(DramaSynthesisError) as caught:
            value.get("async-fixture")
        self.assertEqual((caught.exception.code, caught.exception.status), ("gpu_runtime_unverified", 503))
        value.execute.assert_not_called()

    def test_runtime_directories_use_durable_creation_and_fail_before_intake(self):
        real = async_runtime.durable_ensure_directory
        with mock.patch.object(async_runtime, "durable_ensure_directory", wraps=real) as durable:
            value = self.runtime(autostart=False)
        self.assertEqual(
            [Path(call.args[0]) for call in durable.call_args_list],
            [value.root.parent, value.root, value.root / "jobs", value.root / "locks"],
        )

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            async_runtime, "durable_ensure_directory", side_effect=OSError("directory fsync fault"),
        ):
            with self.assertRaises(DramaSynthesisError) as caught:
                async_runtime.AsyncRuntime(
                    directory, mock.Mock(), mock.Mock(), autostart=False,
                )
        self.assertEqual(caught.exception.code, "gpu_runtime_unverified")

    def test_shutdown_keeps_owner_lock_while_active_job_drains(self):
        entered, finish = threading.Event(), threading.Event()
        execute = lambda payload: (entered.set(), finish.wait(3), result_for(payload))[-1]
        value = self.runtime(execute=execute)
        value.submit(render_payload())
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(value.close(timeout=0))
            with self.assertRaises(DramaSynthesisError) as caught:
                self.runtime(autostart=False)
            self.assertEqual(caught.exception.code, "gpu_runtime_unavailable")
        finally:
            finish.set()
        wait_for(lambda: value.get("async-fixture")["status"] == "completed")
        self.assertTrue(value.close(timeout=2))

    def test_per_job_file_lock_also_fences_execution(self):
        value = self.runtime(autostart=False)
        lock = async_runtime._FileLock(value.root / "locks/async-fixture.lock")
        self.assertTrue(lock.acquire())
        try:
            value.submit(render_payload())
            value.start()
            self.assertEqual(value.get("async-fixture")["status"], "queued")
            value.execute.assert_not_called()
        finally:
            lock.release()
        wait_for(lambda: value.get("async-fixture")["status"] == "completed")
        value.execute.assert_called_once()

    def test_pid_reuse_and_reboot_are_distinct_from_a_surviving_process(self):
        original = {"pid": 123, "start_ticks": "1", "boot_id": "boot-1"}
        with mock.patch.object(async_runtime, "process_identity", return_value={**original, "state": "R"}):
            self.assertEqual(async_runtime.process_state(original), "alive")
        with mock.patch.object(async_runtime, "process_identity", return_value={**original, "start_ticks": "2"}):
            self.assertEqual(async_runtime.process_state(original), "stopped")
        with mock.patch.object(async_runtime, "process_identity", return_value={**original, "boot_id": "boot-2"}):
            self.assertEqual(async_runtime.process_state(original), "stopped")
        self.assertEqual(async_runtime.process_state({"pid": 123}), "unknown")

    def test_real_short_lived_child_is_recorded_and_reaped_without_media_work(self):
        def execute(payload):
            with async_runtime.process_launch():
                child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=os.name == "posix")
                async_runtime.record_process(child.pid)
            try:
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                async_runtime.clear_process(child.pid)
            return result_for(payload)

        value = self.runtime(execute=execute)
        value.submit(render_payload())
        wait_for(lambda: value.get("async-fixture")["status"] != "queued"
                 and value.get("async-fixture")["status"] != "running")
        self.assertEqual(value.get("async-fixture")["status"], "completed")
        record = json.loads((value.root / "jobs/async-fixture.json").read_text(encoding="utf-8"))
        self.assertEqual((record["_children"], record["_launches"]), ({}, {}))

    def test_safe_media_error_code_survives_but_its_untrusted_message_does_not(self):
        value = self.runtime(execute=mock.Mock())
        for code, message in (("drama_episode_source_changed", "视频源版本发生变化，已停止续传"),
                              ("drama_episode_download_route_invalid", "视频下载线路配置与冻结任务不一致"),
                              ("drama_concat_normalization_invalid", "转码后的剧集片段仍不兼容，已停止拼接")):
            error = DramaSynthesisError(code, "private https://source.test/token", 409)
            value.execute.side_effect = error
            payload = render_payload(code)
            value.submit(payload)
            wait_for(lambda: value.get(code)["status"] == "failed")
            self.assertEqual(value.get(code)["error"], {"code": code, "message": message})

    def test_checkpoint_and_upload_uncertainty_require_recovery_without_rerender_label(self):
        value = self.runtime(execute=mock.Mock())
        for code in sorted(async_runtime.RECOVERY_BLOCKING_CODES):
            with self.subTest(code=code):
                value.execute.side_effect = async_runtime.runtime_error(code)
                value.submit(render_payload(code))
                wait_for(lambda: value.get(code)["status"] == "recovery_required")
                current = value.get(code)
                self.assertEqual(current["stage"], "recovery_required")
                self.assertEqual(current["error"]["code"], code)
                record = json.loads((value.root / "jobs" / (code + ".json")).read_text(encoding="utf-8"))
                self.assertTrue(record["_cache_blocked"])

    def test_malformed_async_request_rejects_before_cache_or_execute(self):
        value = self.runtime(autostart=False)
        for updates in ({"episodes": []}, {"episodes": [True]}, {"outputs": []}, {"outputs": {"concat_video": "yes"}},
                        {"await_cover_16x9": "false"}, {"transport_extra": float("nan")},
                        {"episodes": [{"episode_number": 1, "episode_url": "file:///secret"}]},
                        {"episodes": [{"episode_number": True, "episode_url": "https://source.test/1"}]}):
            with self.subTest(updates=updates), self.assertRaises(DramaSynthesisError):
                value.submit({**render_payload(), **updates})
        value.cached_result.assert_not_called()
        value.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

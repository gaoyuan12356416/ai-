#!/usr/bin/env python3
"""CPU-only package/HTTP tests: no installed ML stack, models, or external API."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_drama_synthesis_gpu_runtime as runtime
from scripts import demucs_extract_vocals as demucs_adapter


def load_fake_worker(fake_app):
    spec = importlib.util.spec_from_file_location(
        "_drama_gpu_runtime_test", ROOT / "scripts/drama_synthesis_gpu_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"app": fake_app}), mock.patch.dict(
        os.environ, {"DRAMA_GPU_MAX_CONCURRENCY": "1"}
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
            env["DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256"] = "a" * 64
            for key in runtime.DIRECTORY_KEYS:
                Path(env[key]).mkdir(parents=True, exist_ok=True)
            for key in runtime.FILE_KEYS:
                path = Path(env[key])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake binary for path validation only")
                path.chmod(0o700)
            self.assertEqual(runtime.validate_environment(env, root=root), [])
            self.assertFalse(Path(env["DRAMA_JOB_DB_PATH"]).exists())

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
        self.assertIn("DEMUCS_REQUIRE_LOCAL_MODELS=1", env)
        self.assertNotIn("/usr/bin/python3.9", unit)
        self.assertNotIn("/root/", unit + env)
        for key in ("GPU_VIDEO_WORKER_TOKEN", "COS_SECRET_ID", "COS_SECRET_KEY"):
            self.assertRegex(env, rf"(?m)^{key}=$")


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fake_app = SimpleNamespace(
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
            self.assertEqual(self.request("GET", "/healthz")[0], 200)
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
            for route in ("render", "cover"):
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
        for content_id in (None, "", "剧集-咖啡_第1部", "c3d8e5ed-5f0e-4a04-b08b-3b27f4e90abc", 123456):
            with self.subTest(content_id=content_id):
                self.assertEqual(self.request("POST", "/api/gpu-video/render", {"job_id": "a" * 32, "content_id": content_id})[0], 200)

    def test_concurrency_bounds_fail_closed(self):
        self.assertEqual(self.worker.render_concurrency({}), 1)
        for value in ("0", "9", "NaN", "", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.worker.render_concurrency({"DRAMA_GPU_MAX_CONCURRENCY": value})


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Offline X native routing, budget and failure-contract regressions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from features.x_posts import media_repair as repair
from features.x_posts import random_template_gpu as gpu


class XNativeTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(profile=gpu.PROFILE, ffmpeg_bin="/qualified/ffmpeg",
                                      max_output_bytes=512 * 1024**2)
        self.info = {"duration": 30., "video": {"width": 720, "height": 1280,
                     "time_base": "1/15360"}, "format": {"start_time": "0.125"},
                     "has_audio": True}
        self.recipe = {"frozen": "unchanged"}
        self.assets = {"border": Path("border.png")}
        self.env = mock.patch.dict(os.environ, {"RANDOM_GPU_NATIVE": "1",
                "RANDOM_GPU_PYTHON": "/isolated/venv/bin/python",
                "RANDOM_GPU_ASSET_CACHE_ROOT": "/verified/assets"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def command(self, info=None):
        return gpu.random_command(self.config, Path("source.mp4"), Path("output.mp4"),
                                  info or self.info, self.recipe, self.assets)

    def test_native_preserves_short_and_long_size_budgets(self):
        for duration in (30., 1200., 14400.):
            with self.subTest(duration=duration):
                info = dict(self.info, duration=duration)
                command = self.command(info)
                self.assertEqual(command[:3], ["/isolated/venv/bin/python", "-m",
                                               "features.random_gpu.native_pipeline"])
                spec = json.loads(command[command.index("--spec")+1])
                rate = repair.output_rate_control(self.config.max_output_bytes, duration)
                options = spec["encoder_options"]
                self.assertEqual(options, dict(codec="h264", profile="high", preset="P5",
                    rc="vbr", cq=21, bitrate=rate["video_bitrate_kbps"]*1000,
                    maxbitrate=rate["video_maxrate_kbps"]*1000,
                    vbvbufsize=rate["video_bufsize_kbps"]*1000, fps=30, gop=60,
                    idrperiod=60, global_header=1))
                self.assertEqual(spec["audio_bitrate"], 128000)
                self.assertEqual(spec["ffmpeg"], self.config.ffmpeg_bin)
                self.assertEqual(spec["info"], info)
                self.assertEqual(spec["recipe"], self.recipe)
                self.assertEqual(spec["assets"], {"border": "border.png"})

    def test_insufficient_size_budget_rejected_before_render(self):
        self.config.max_output_bytes = 1024
        with mock.patch.object(gpu.native_pipeline, "command") as native:
            with self.assertRaises(repair.MediaRepairError):
                self.command()
            native.assert_not_called()

    def test_cpu_profile_never_routes_to_native_and_keeps_legacy_budget(self):
        self.config.profile = gpu.CPU_PROFILE
        with mock.patch.object(gpu, "build_command", return_value=["ffmpeg", "-b:v", "0",
                "-b:a", "192k", "output.mp4"]) as build, \
                mock.patch.object(gpu.native_pipeline, "command") as native:
            command = self.command()
        native.assert_not_called()
        self.assertEqual(build.call_args.args[0].video_encoder, "libx264")
        self.assertEqual(build.call_args.args[0].compositor_backend, "cpu_legacy")
        self.assertEqual(command, ["ffmpeg", "-b:v", "5000k", "-b:a", "128k",
                                  "-maxrate", "6000k", "-bufsize", "10000k", "output.mp4"])

    def test_disabled_native_preserves_gpu_legacy_command(self):
        with mock.patch.dict(os.environ, {"RANDOM_GPU_NATIVE": "0"}), \
                mock.patch.object(gpu, "build_command", return_value=["ffmpeg", "-b:v", "0",
                "-b:a", "192k", "output.mp4"]) as build:
            command = self.command()
        self.assertEqual(build.call_args.args[0].video_encoder, "h264_nvenc")
        self.assertEqual(command[command.index("-b:a")+1], "128k")

    def test_probe_retains_container_origin_and_exact_video_timebase(self):
        worker = gpu.RandomTemplateProcessor.__new__(gpu.RandomTemplateProcessor)
        worker.config = self.config
        worker.repair = SimpleNamespace(runner=mock.Mock())
        video = dict(self.info["video"], codec_type="video")
        container = dict(self.info["format"], duration="30.000000")
        raw = {"format": container, "streams": [video, {"codec_type": "audio"}]}
        with mock.patch.object(repair, "_probe_payload", return_value=raw):
            info = worker._info(Path("source.mp4"))
        self.assertEqual(info, dict(self.info, video=video, format=container))

    def test_native_stall_does_not_normalize_or_retry_frozen_recipe(self):
        worker = gpu.RandomTemplateProcessor.__new__(gpu.RandomTemplateProcessor)
        worker.config, worker.timeout = self.config, 120
        worker.runner = mock.Mock(side_effect=gpu.RenderStalled("stalled", 120))
        with self.assertRaises(gpu.RenderStalled):
            worker._render(Path("source.mp4"), Path("output.mp4"), self.info,
                           self.recipe, self.assets)
        self.assertEqual(worker.runner.call_count, 1)
        self.assertIn("features.random_gpu.native_pipeline", worker.runner.call_args.args[0])

    def test_legacy_stall_keeps_existing_normalize_retry(self):
        worker = gpu.RandomTemplateProcessor.__new__(gpu.RandomTemplateProcessor)
        worker.config, worker.timeout = self.config, 120
        worker.runner = mock.Mock(side_effect=[gpu.RenderStalled("stalled", 120), None, None])
        worker._info = mock.Mock(return_value=self.info)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"RANDOM_GPU_NATIVE": "0"}), \
                mock.patch.object(gpu, "random_command", return_value=["legacy"]):
            worker._render(Path(directory)/"source.mp4", Path(directory)/"output.mp4",
                           self.info, self.recipe, self.assets)
        self.assertEqual(worker.runner.call_count, 3)
        self.assertIn("h264_nvenc", worker.runner.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()

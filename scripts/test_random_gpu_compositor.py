"""Contracts for switching only the TT/FB composition stage to OpenCL."""
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from features.random_gpu.compositor import BACKEND, LEGACY, backend, fuse_command, kernel_source


RECIPE = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}


class CompositorTests(unittest.TestCase):
    def test_invalid_configuration_cannot_silently_select_cpu(self):
        self.assertEqual(backend(), LEGACY)
        self.assertEqual(backend(BACKEND), BACKEND)
        for value in ("cuda", "", None):
            with self.assertRaises(ValueError): backend(value)

    def test_kernel_parameters_are_bounded_and_not_source_text(self):
        for key in RECIPE:
            for value in (True, "1; __kernel", 10000000, float("nan")):
                with self.assertRaises(ValueError): kernel_source({**RECIPE, key: value})
        self.assertEqual(kernel_source(RECIPE), kernel_source(dict(RECIPE)))
        self.assertNotEqual(kernel_source(RECIPE), kernel_source({**RECIPE, "scale_bp": 10000}))

    def test_platform_adapters_preserve_encoder_audio_duration_and_inputs(self):
        modules = ["features.tt_gpu.worker", "features.fb_gpu.prepare_worker"]
        for name in modules:
            try: module = importlib.import_module(name)
            except ModuleNotFoundError: continue
            if "compositor_backend" not in module.WorkerConfig.__dataclass_fields__:
                continue  # A historical TT copy in the FB baseline is untouched.
            for has_audio in (True, False):
                with self.subTest(worker=name, audio=has_audio), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); output = root / "output.mp4"
                    assets = {key: root / (key + (".webm" if key in ("opacity_video", "corners") else ".png"))
                              for key in ("border", "opacity_video", "corners", "tint")}
                    info = {"duration": 10.25, "has_audio": has_audio}
                    cfg = SimpleNamespace(ffmpeg="ffmpeg", ffmpeg_bin="ffmpeg", video_encoder="hevc_nvenc",
                                          compositor_backend=LEGACY)
                    def build():
                        if name.endswith("prepare_worker"):
                            return module.build_command(cfg, root / "source.mp4", output, info, RECIPE, assets)
                        return module.build_random_overlay_command(cfg, root / "source.mp4", output, info, 10.25, RECIPE, assets)
                    old = build(); cfg.compositor_backend = BACKEND; new = build()
                    # The complete delivery suffix, including the no-audio map,
                    # is byte-for-byte the established platform command.
                    self.assertEqual(old[old.index("-map"):], new[new.index("-map"):])
                    paths = lambda cmd: [cmd[i + 1] for i, value in enumerate(cmd) if value == "-i"]
                    self.assertEqual(paths(old), paths(new))
                    graph = new[new.index("-filter_complex") + 1]
                    self.assertIn("program_opencl=inputs=5", graph)
                    self.assertEqual(graph.count("fps=30:start_time=0"), 5)
                    self.assertNotIn("fps=", graph.split("program_opencl", 1)[1])
                    self.assertNotIn("PTS-STARTPTS", graph)
                    self.assertNotIn("rotate=", graph)
                    self.assertTrue(list(root.glob("compositor-*.cl")))
                    self.assertFalse(list(root.glob(".compositor-*")))

    def test_reject_non_overlay_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                fuse_command(["ffmpeg", "-i", "source", "-filter_complex", "null[v]"], RECIPE, Path(tmp) / "out.mp4")


if __name__ == "__main__": unittest.main()

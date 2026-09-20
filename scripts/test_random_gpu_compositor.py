"""Contracts for switching only the TT/FB composition stage to OpenCL."""
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from features.random_gpu.compositor import (
    BACKEND, LEGACY, backend, fuse_command, kernel_source, preflight, validate_source_overlay,
)


RECIPE = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}
SOURCE_OVERLAY = {"version": 1, "opacity_bp": 350, "scale_bp": 12500}


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

    def test_source_overlay_is_optional_strict_and_bounded(self):
        self.assertIsNone(validate_source_overlay(RECIPE))
        for opacity, scale in ((200, 11000), (500, 15000), (200, 15000), (500, 11000)):
            overlay = {"version": 1, "opacity_bp": opacity, "scale_bp": scale}
            self.assertEqual(validate_source_overlay({**RECIPE, "source_overlay": overlay}), overlay)
        malformed = [None, {}, False, [], {**SOURCE_OVERLAY, "extra": 1}]
        for key in SOURCE_OVERLAY:
            malformed.append({name: value for name, value in SOURCE_OVERLAY.items() if name != key})
            for value in (True, False, None, "1", 1.0):
                malformed.append({**SOURCE_OVERLAY, key: value})
        for key, values in {"version": (0, 2), "opacity_bp": (199, 501),
                            "scale_bp": (10999, 15001)}.items():
            malformed.extend({**SOURCE_OVERLAY, key: value} for value in values)
        for invalid in malformed:
            with self.subTest(overlay=invalid), self.assertRaises(ValueError):
                kernel_source({**RECIPE, "source_overlay": invalid})

    def test_source_overlay_kernel_reuses_unrotated_source_and_is_topmost(self):
        old = kernel_source(RECIPE)
        self.assertNotIn("#define SCENE_SOURCE_OVERLAY_", old)
        new = kernel_source({**RECIPE, "source_overlay": SOURCE_OVERLAY})
        self.assertIn("#define SCENE_SOURCE_OVERLAY_OPACITY 0.03500000f", new)
        self.assertIn("#define SCENE_SOURCE_OVERLAY_SCALE 1.25000000f", new)
        self.assertIn("#ifdef SCENE_SOURCE_OVERLAY_OPACITY", new)
        ghost = new.split("#ifdef SCENE_SOURCE_OVERLAY_OPACITY", 1)[1].split("#endif", 1)[0]
        self.assertIn("read_imagef(source, linear_edge, source_uv)", ghost)
        self.assertIn("(uv - (float2)(0.5f, 0.5f)) /", ghost)
        self.assertNotIn("sample_main", ghost)
        self.assertNotIn("SCENE_ROTATION_RADIANS", ghost)
        self.assertLess(new.index("read_imagef(corners"), new.index("value = over(value, source_overlay)"))

    def test_overlay_constants_change_shader_cache_identity_without_extra_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = ["ffmpeg", "-i", "source.mp4", "-i", "border.png", "-i", "opacity.webm",
                       "-i", "corners.webm", "-i", "tint.png", "-filter_complex", "null[v]",
                       "-map", "[v]", "-map", "0:a:0", "-t", "1", str(root / "out.mp4")]
            recipes = [RECIPE] + [{**RECIPE, "source_overlay": overlay} for overlay in (
                SOURCE_OVERLAY, {**SOURCE_OVERLAY, "opacity_bp": 351},
                {**SOURCE_OVERLAY, "scale_bp": 12501})]
            commands = [fuse_command(command, recipe, root / "out.mp4") for recipe in recipes]
            self.assertEqual(len(list(root.glob("compositor-*.cl"))), 4)
            for generated in commands:
                self.assertEqual(generated.count("-i"), 5)
                graph = generated[generated.index("-filter_complex") + 1]
                self.assertIn("program_opencl=inputs=6", graph)
                self.assertIn("split=2[backraw][mainraw]", graph)
                self.assertNotIn("[sourceraw]", graph)
                self.assertEqual(command[command.index("-map"):], generated[generated.index("-map"):])

    def test_preflight_compiles_enabled_source_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            observed = []
            def run(command, **kwargs):
                observed.append(next(Path(tmp).glob("compositor-preflight-*/preflight.cl")).read_text())
            with mock.patch("features.random_gpu.compositor.subprocess.run", side_effect=run) as runner:
                preflight("ffmpeg", "hevc_nvenc", tmp)
            self.assertEqual(runner.call_count, 1)
            self.assertIn("#define SCENE_SOURCE_OVERLAY_OPACITY 0.05000000f", observed[0])
            self.assertIn("#define SCENE_SOURCE_OVERLAY_SCALE 1.50000000f", observed[0])

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
                    self.assertIn("program_opencl=inputs=6", graph)
                    self.assertEqual(graph.count("fps=30:start_time=0"), 5)
                    self.assertNotIn("fps=", graph.split("program_opencl", 1)[1])
                    self.assertNotIn("PTS-STARTPTS", graph)
                    self.assertNotIn("rotate=", graph)
                    self.assertEqual(new[new.index("-reinit_filter") + 1], "0")
                    self.assertIn("eval=frame", graph)
                    self.assertTrue(list(root.glob("compositor-*.cl")))
                    self.assertFalse(list(root.glob(".compositor-*")))

    def test_reject_non_overlay_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                fuse_command(["ffmpeg", "-i", "source", "-filter_complex", "null[v]"], RECIPE, Path(tmp) / "out.mp4")


if __name__ == "__main__": unittest.main()

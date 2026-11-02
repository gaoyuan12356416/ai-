#!/usr/bin/env python3
"""Focused contract tests for TT four-layer random production."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_gpu.random_overlay import (  # noqa: E402
    ASSET_CATEGORIES,
    CATEGORIES,
    RandomOverlayError,
    derive_recipe,
    load_asset_set,
    validate_recipe,
)
from features.tt_gpu.worker import build_random_overlay_command  # noqa: E402


class RandomOverlayTests(unittest.TestCase):
    def build_assets(self, root: Path):
        categories = {}
        for category in ASSET_CATEGORIES:
            rows = []
            for index in range(2):
                suffix = ".png" if category in {"border", "tint"} else ".webm"
                name = "%s-%02d%s" % (category.replace("_", "-"), index, suffix)
                payload = (category + str(index)).encode("ascii")
                path = root / name
                path.write_bytes(payload)
                rows.append(
                    {
                        "media_type": "image/png" if suffix == ".png" else "video/webm",
                        "name": name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                )
            categories[category] = rows
        manifest = {"categories": categories, "version": 1}
        raw = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
        (root / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_asset_set_and_recipe_are_stable_bounded_and_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = self.build_assets(root)
            assets = load_asset_set(root, manifest_sha)
            kwargs = {
                "job_id": "job-12345678",
                "content_id": "DRAMA123",
                "profile": "tt-post-random-overlay-hevc-720x1280-v3",
                "source_url_sha256": "a" * 64,
                "asset_set": assets,
            }
            first = derive_recipe(**kwargs)
            second = derive_recipe(**kwargs)
            self.assertEqual(first, second)
            self.assertEqual(set(first["assets"]), set(CATEGORIES))
            self.assertTrue(-2000 <= first["rotation_millidegrees"] <= 2000)
            self.assertTrue(9800 <= first["scale_bp"] <= 10200)
            self.assertTrue(100 <= first["tint_opacity_bp"] <= 1000)
            self.assertEqual(set(first["source_overlay"]), {"version", "opacity_bp", "scale_bp"})
            self.assertEqual(first["source_overlay"]["version"], 1)
            self.assertTrue(200 <= first["source_overlay"]["opacity_bp"] <= 500)
            self.assertTrue(11000 <= first["source_overlay"]["scale_bp"] <= 15000)
            self.assertNotIn("light", first["assets"])
            validate_recipe(first, assets)

    def test_new_jobs_vary_both_source_parameters_and_include_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = load_asset_set(root, self.build_assets(root))
            kwargs = dict(content_id="DRAMA123", profile="random-v3",
                          source_url_sha256="a" * 64, asset_set=assets)
            overlays = [derive_recipe(job_id="job-%d" % index, **kwargs)["source_overlay"]
                        for index in range(40)]
            self.assertGreater(len({value["opacity_bp"] for value in overlays}), 1)
            self.assertGreater(len({value["scale_bp"] for value in overlays}), 1)
            # Dedicated labels keep these independent of the main transform.
            for opacity_seed, scale_seed, opacity, scale in (
                (0, 0, 200, 11000), (300, 4000, 500, 15000),
                (300, 0, 500, 11000), (0, 4000, 200, 15000),
            ):
                labels = {"source-overlay-opacity": opacity_seed,
                          "source-overlay-scale": scale_seed}
                with mock.patch("features.tt_gpu.random_overlay._seed",
                                side_effect=lambda _identity, label: labels.get(label, 0)):
                    recipe = derive_recipe(job_id="bounds", **kwargs)
                self.assertEqual(recipe["source_overlay"],
                                 {"version": 1, "opacity_bp": opacity, "scale_bp": scale})

    def test_legacy_recipe_is_accepted_without_mutation_and_overlay_is_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = load_asset_set(root, self.build_assets(root))
            recipe = derive_recipe(job_id="legacy", content_id="DRAMA123", profile="random-v3",
                                   source_url_sha256="a" * 64, asset_set=assets)
            overlay = recipe.pop("source_overlay")
            before = json.dumps(recipe, sort_keys=True)
            validate_recipe(recipe, assets)
            self.assertEqual(json.dumps(recipe, sort_keys=True), before)
            malformed = [None, {}, False, [], {**overlay, "extra": 1}]
            for key in overlay:
                malformed.append({name: value for name, value in overlay.items() if name != key})
                for value in (True, False, None, "1", 1.0):
                    malformed.append({**overlay, key: value})
            for key, values in {"version": (0, 2), "opacity_bp": (199, 501),
                                "scale_bp": (10999, 15001)}.items():
                malformed.extend({**overlay, key: value} for value in values)
            for invalid in malformed:
                with self.subTest(overlay=invalid), self.assertRaises(RandomOverlayError):
                    validate_recipe({**recipe, "source_overlay": invalid}, assets)

    def test_asset_tamper_and_recipe_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = self.build_assets(root)
            assets = load_asset_set(root, manifest_sha)
            recipe = derive_recipe(
                job_id="job-12345678",
                content_id="DRAMA123",
                profile="tt-post-random-overlay-hevc-720x1280-v3",
                source_url_sha256="b" * 64,
                asset_set=assets,
            )
            recipe["scale_bp"] = 12000
            with self.assertRaises(RandomOverlayError):
                validate_recipe(recipe, assets)
            first_asset = next(root.glob("border-*.png"))
            first_asset.write_bytes(b"tampered")
            with self.assertRaises(RandomOverlayError):
                load_asset_set(root, manifest_sha)

    def test_ffmpeg_command_stacks_four_layers_and_keeps_full_audio(self):
        config = SimpleNamespace(ffmpeg_bin="ffmpeg", video_encoder="hevc_nvenc", compositor_backend="cpu_legacy")
        paths = {
            "border": Path("border.png"),
            "opacity_video": Path("opacity.webm"),
            "corners": Path("corners.webm"),
            "tint": Path("tint.png"),
        }
        recipe = {
            "rotation_millidegrees": -1250,
            "scale_bp": 10125,
            "tint_opacity_bp": 750,
        }
        command = build_random_overlay_command(
            config,
            Path("source.mp4"),
            Path("output.mp4"),
            {"has_audio": True},
            12.5,
            recipe,
            paths,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("rotate=-1.250000*PI/180", graph)
        self.assertIn("iw*1.0125", graph)
        self.assertIn("colorchannelmixer=aa=0.0750", graph)
        self.assertLess(graph.index("[base][tint]"), graph.index("[o1][opacity]"))
        self.assertLess(graph.index("[o1][opacity]"), graph.index("[o2][border]"))
        self.assertLess(graph.index("[o2][border]"), graph.index("[o3][corners]"))
        self.assertNotIn("[light]", graph)
        self.assertNotIn("light.webm", command)
        self.assertEqual(command[command.index("-map", command.index("[v]")) + 1], "0:a:0")
        self.assertIn("12.500000", command)

    def test_ffmpeg_source_ghost_uses_same_source_after_corners_without_audio_changes(self):
        config = SimpleNamespace(ffmpeg_bin="ffmpeg", video_encoder="hevc_nvenc",
                                 compositor_backend="cpu_legacy")
        paths = {category: Path(category + ".png") for category in CATEGORIES}
        legacy = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}
        for has_audio in (True, False):
            old = build_random_overlay_command(config, Path("source.mp4"), Path("output.mp4"),
                                               {"has_audio": has_audio}, 12.5, legacy, paths)
            for opacity, scale in ((200, 11000), (500, 15000)):
                with self.subTest(audio=has_audio, opacity=opacity, scale=scale):
                    recipe = {**legacy, "source_overlay": {
                        "version": 1, "opacity_bp": opacity, "scale_bp": scale}}
                    command = build_random_overlay_command(config, Path("source.mp4"),
                        Path("output.mp4"), {"has_audio": has_audio}, 12.5, recipe, paths)
                    graph = command[command.index("-filter_complex") + 1]
                    self.assertEqual(old[:old.index("-filter_complex")],
                                     command[:command.index("-filter_complex")])
                    self.assertEqual(old[old.index("-map"):], command[command.index("-map"):])
                    self.assertIn("split=3[backraw][mainraw][sourceraw]", graph)
                    ghost = graph.split("[sourceraw]scale", 1)[1].split("[sourceghost]", 1)[0]
                    self.assertIn("force_original_aspect_ratio=increase", ghost)
                    self.assertEqual(ghost.count("crop=720:1280"), 2)
                    self.assertIn("iw*%.4f" % (scale / 10000), ghost)
                    self.assertIn("colorchannelmixer=aa=%.4f" % (opacity / 10000), ghost)
                    self.assertNotIn("rotate=", ghost)
                    self.assertLess(graph.index("[o3][corners]"),
                                    graph.index("[decorated][sourceghost]"))
            for invalid in (None, {"version": 1, "opacity_bp": True, "scale_bp": 12000}):
                with self.assertRaisesRegex(RuntimeError, "random overlay recipe is invalid"):
                    build_random_overlay_command(config, Path("source.mp4"), Path("output.mp4"),
                        {"has_audio": has_audio}, 12.5, {**legacy, "source_overlay": invalid}, paths)

    def test_random_output_bounds_padded_audio_without_output_shortest(self):
        legacy = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}
        recipes = (legacy, {**legacy, "source_overlay": {
            "version": 1, "opacity_bp": 500, "scale_bp": 15000}})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {category: root / (category + ".png") for category in CATEGORIES}
            for backend in ("cpu_legacy", "opencl_fused_contain_v1"):
                for encoder in ("hevc_nvenc", "h264_nvenc"):
                    config = SimpleNamespace(ffmpeg_bin="ffmpeg", video_encoder=encoder,
                                             compositor_backend=backend)
                    for has_audio in (True, False):
                        for recipe in recipes:
                            with self.subTest(backend=backend, encoder=encoder, audio=has_audio,
                                              overlay="source_overlay" in recipe):
                                command = build_random_overlay_command(config, root / "source.mp4",
                                    root / "output.mp4", {"has_audio": has_audio}, 108.3, recipe, paths)
                                self.assertNotIn("-shortest", command)
                                self.assertEqual(command.count("-t"), 1)
                                self.assertEqual(command[command.index("-t") + 1], "108.300000")
                                self.assertEqual(command[command.index("-af") + 1],
                                                 "aresample=48000:async=1:first_pts=0,apad")
                                audio_map = command.index("-map", command.index("[v]")) + 1
                                self.assertEqual(command[audio_map], "0:a:0" if has_audio else "5:a:0")
                                for option, value in (("-c:a", "aac"), ("-profile:a", "aac_low"),
                                                      ("-ar", "48000"), ("-ac", "2")):
                                    self.assertEqual(command[command.index(option) + 1], value)
                                graph = command[command.index("-filter_complex") + 1]
                                self.assertIn("shortest=1:eof_action=", graph)


if __name__ == "__main__":
    unittest.main(verbosity=2)

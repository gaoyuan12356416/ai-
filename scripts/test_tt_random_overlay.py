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
            self.assertNotIn("light", first["assets"])
            validate_recipe(first, assets)

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
        config = SimpleNamespace(ffmpeg_bin="ffmpeg", video_encoder="hevc_nvenc")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

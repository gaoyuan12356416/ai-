"""Frozen jobs must render against their original, verified asset catalog."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.drama_synthesis.core import DramaSynthesisError
from features.drama_synthesis.gpu import render_random_output


class CatalogVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.old_root, self.new_root = root / "old", root / "new"
        self.old_root.mkdir(); self.new_root.mkdir()
        self.old_sha, self.new_sha = "a" * 64, "b" * 64
        self.env = {"RANDOM_OVERLAY_ASSET_CATALOGS": json.dumps({self.old_sha: str(self.old_root)})}
        self.args = dict(source=root / "source.mp4", output=root / "out.mp4",
                         recipe={"asset_set_sha256": self.old_sha},
                         asset_root=self.new_root, manifest_sha256=self.new_sha)

    def test_frozen_recipe_selects_old_catalog_before_each_render_backend(self):
        for backend, target in [
            ("legacy_cpu", "features.drama_synthesis.gpu._render_random_output_legacy"),
            ("opencl_fused_v2", "features.drama_synthesis.gpu_compositor.render_chunked_random_output"),
        ]:
            with self.subTest(backend=backend), patch.dict(os.environ, {**self.env, "DRAMA_GPU_COMPOSITOR_BACKEND": backend}), patch(target, return_value={"ok": True}) as render:
                self.assertEqual(render_random_output(**self.args), {"ok": True})
                self.assertEqual(render.call_args.kwargs["asset_root"], self.old_root)
                self.assertEqual(render.call_args.kwargs["manifest_sha256"], self.old_sha)
                self.assertIs(render.call_args.kwargs["recipe"], self.args["recipe"])

    def test_current_recipe_uses_current_default(self):
        self.args["recipe"] = {"asset_set_sha256": self.new_sha}
        with patch.dict(os.environ, {**self.env, "DRAMA_GPU_COMPOSITOR_BACKEND": "legacy_cpu"}), patch("features.drama_synthesis.gpu._render_random_output_legacy", return_value={}) as render:
            render_random_output(**self.args)
            self.assertEqual(render.call_args.kwargs["asset_root"], self.new_root)
            self.assertEqual(render.call_args.kwargs["manifest_sha256"], self.new_sha)

    def test_unknown_frozen_version_fails_before_renderer(self):
        self.args["recipe"] = {"asset_set_sha256": "c" * 64}
        with patch.dict(os.environ, self.env), patch("features.drama_synthesis.gpu._render_random_output_legacy") as render:
            with self.assertRaises(DramaSynthesisError) as raised:
                render_random_output(**self.args)
            self.assertEqual(raised.exception.code, "drama_template_catalog_unavailable")
            render.assert_not_called()


if __name__ == "__main__":
    unittest.main()

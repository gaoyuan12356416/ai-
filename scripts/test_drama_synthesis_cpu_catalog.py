#!/usr/bin/env python3
"""CPU query boundary regression; temp metadata only, no external requests."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.catalog import MAX_MANIFEST_BYTES, catalog_from_manifest
from features.drama_synthesis.core import DramaSynthesisError, RECIPE_CATEGORIES, freeze_random_recipe
from features.drama_synthesis.gpu import catalog_from_assets


def fixture_manifest():
    categories = {}
    for category, count in {"border": 3, "corners": 3, "opacity_video": 5, "tint": 7, "light": 2}.items():
        suffix = ".webm" if category in {"corners", "opacity_video"} else ".png"
        categories[category] = [
            {
                "name": f"{category}-{index:02d}{suffix}",
                "sha256": hashlib.sha256(f"{category}-{index}".encode()).hexdigest(),
                "size": len(f"{category}-{index}".encode()),
                "media_type": "video/webm" if suffix == ".webm" else "image/png",
            }
            for index in range(count)
        ]
    return {"version": 1, "categories": categories}


class CPUCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "drama_random_template_catalog")
        cls.app_function_code = compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / "app.py"), "exec")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "manifest.json"
        self.document = fixture_manifest()
        self.digest = self.write_manifest(self.document)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def assert_unavailable(self, path=None, digest=None):
        with self.assertRaises(DramaSynthesisError) as caught:
            catalog_from_manifest(self.path if path is None else path, self.digest if digest is None else digest)
        self.assertEqual(caught.exception.code, "drama_template_catalog_unavailable")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(str(self.path), str(caught.exception))

    def app_catalog(self, **overrides):
        network = mock.Mock(side_effect=AssertionError("CPU catalog attempted network access"))
        asset_read = mock.Mock(side_effect=AssertionError("CPU catalog attempted to read media assets"))
        namespace = {
            "DRAMA_RANDOM_OVERLAY_MANIFEST_FILE": str(self.path),
            "DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256": self.digest,
            "DRAMA_RANDOM_OVERLAY_ROOT": "/not-a-cpu-asset-bundle",
            "GPU_VIDEO_WORKER_URL": "http://127.0.0.1:18788",
            "GPU_VIDEO_WORKER_TOKEN": "fake-not-a-real-token",
            "GPU_VIDEO_WORKER_TIMEOUT": 30,
            "catalog_from_manifest": catalog_from_manifest,
            "catalog_from_assets": asset_read,
            "DramaSynthesisError": DramaSynthesisError,
            "requests": SimpleNamespace(get=network, post=network),
        }
        namespace.update(overrides)
        exec(self.app_function_code, namespace)
        return namespace["drama_random_template_catalog"], network, asset_read

    def test_metadata_only_four_layers_315_combinations_no_light(self):
        self.assertEqual(list(self.root.iterdir()), [self.path])
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")), mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database")):
            result = catalog_from_manifest(self.path, self.digest)
        self.assertEqual(set(result["categories"]), set(RECIPE_CATEGORIES))
        self.assertNotIn("light", result["categories"])
        product = 1
        for rows in result["categories"].values():
            product *= len(rows)
            self.assertTrue(all(set(row) == {"name", "sha256", "size", "media_type"} for row in rows))
        self.assertEqual(product, 315)
        self.assertEqual(result["manifest_sha256"], self.digest)

    def test_same_manifest_matches_verified_gpu_catalog_and_recipe(self):
        gpu_root = self.root / "gpu-assets"
        gpu_root.mkdir()
        (gpu_root / "manifest.json").write_bytes(self.path.read_bytes())
        for category, rows in self.document["categories"].items():
            for index, row in enumerate(rows):
                (gpu_root / row["name"]).write_bytes(f"{category}-{index}".encode())
        cpu_catalog = catalog_from_manifest(self.path, self.digest)
        gpu_catalog = catalog_from_assets(gpu_root, self.digest)
        self.assertEqual(cpu_catalog, gpu_catalog)
        for mode in ("auto", "manual"):
            options = {"mode": mode, "source": "no_bgm_video", "layers": {name: rows[0]["name"] for name, rows in cpu_catalog["categories"].items()}}
            kwargs = {"job_id": "d" * 32, "content_id": "CPU-boundary-test", "request": options}
            self.assertEqual(freeze_random_recipe(catalog=cpu_catalog, **kwargs), freeze_random_recipe(catalog=gpu_catalog, **kwargs))

    def test_cpu_app_catalog_does_not_contact_gpu_or_database(self):
        function, network, assets = self.app_catalog()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")), mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database")):
            result = function()
        self.assertEqual(result, catalog_from_manifest(self.path, self.digest))
        network.assert_not_called()
        assets.assert_not_called()

    def test_cpu_missing_configuration_does_not_fall_back_to_gpu_or_bundle(self):
        function, network, assets = self.app_catalog(DRAMA_RANDOM_OVERLAY_MANIFEST_FILE="")
        with self.assertRaises(DramaSynthesisError):
            function()
        network.assert_not_called()
        assets.assert_not_called()

    def test_cpu_bad_pin_does_not_fall_back_to_gpu_or_bundle(self):
        function, network, assets = self.app_catalog(DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256="0" * 64)
        with self.assertRaises(DramaSynthesisError):
            function()
        network.assert_not_called()
        assets.assert_not_called()

    def test_gpu_local_asset_diagnostic_remains_local(self):
        local_catalog = {"local": True}
        check = mock.Mock(return_value=local_catalog)
        function, network, _ = self.app_catalog(DRAMA_RANDOM_OVERLAY_MANIFEST_FILE="", GPU_VIDEO_WORKER_URL="", catalog_from_assets=check)
        self.assertEqual(function(), local_catalog)
        check.assert_called_once_with("/not-a-cpu-asset-bundle", self.digest)
        network.assert_not_called()

    def test_pin_required_exact_and_case_normalized(self):
        self.assertEqual(catalog_from_manifest(self.path, self.digest.upper())["manifest_sha256"], self.digest)
        for digest in ("", "invalid", "0" * 64, self.digest + " "):
            with self.subTest(digest=digest):
                self.assert_unavailable(digest=digest)

    def test_absolute_regular_file_required(self):
        for path in ("manifest.json", "", self.root, self.root / "missing.json"):
            with self.subTest(path=str(path)):
                self.assert_unavailable(path=path)

    def test_symlink_rejected_before_open(self):
        info = list(self.path.stat())
        info[0] = stat.S_IFLNK | 0o777
        with mock.patch.object(Path, "lstat", return_value=os.stat_result(info)), mock.patch("features.drama_synthesis.catalog.os.open") as opened:
            self.assert_unavailable()
        opened.assert_not_called()

    def test_changed_opened_file_type_is_rejected(self):
        info = list(self.path.stat())
        info[0] = stat.S_IFIFO | 0o600
        with mock.patch("features.drama_synthesis.catalog.os.fstat", return_value=os.stat_result(info)):
            self.assert_unavailable()

    def test_empty_and_oversized_metadata_are_rejected(self):
        for raw in (b"", b"x" * (MAX_MANIFEST_BYTES + 1)):
            digest = self.write_manifest(raw)
            with self.subTest(size=len(raw)):
                self.assert_unavailable(digest=digest)

    def test_metadata_change_is_detected_on_next_read(self):
        catalog_from_manifest(self.path, self.digest)
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        self.assert_unavailable()

    def test_malformed_duplicate_keys_and_non_utf8_are_rejected(self):
        for raw in (b"not-json", b"[]", b"null", b"\xff", b'{"version":1,"version":1,"categories":{}}'):
            digest = self.write_manifest(raw)
            with self.subTest(raw=repr(raw)):
                self.assert_unavailable(digest=digest)

    def test_version_and_category_contract(self):
        changed = []
        for version in (True, "1", 2, None):
            value = copy.deepcopy(self.document)
            value["version"] = version
            changed.append(value)
        for categories in ([], {}, {key: val for key, val in self.document["categories"].items() if key != "light"}):
            value = copy.deepcopy(self.document)
            value["categories"] = categories
            changed.append(value)
        value = copy.deepcopy(self.document)
        value["categories"]["unknown"] = []
        changed.append(value)
        for index, value in enumerate(changed):
            with self.subTest(index=index):
                self.assert_unavailable(digest=self.write_manifest(value))

    def test_asset_contract_including_excluded_light_is_validated(self):
        invalid_rows = [None, {}, {**self.document["categories"]["border"][0], "path": "/private"}]
        first = self.document["categories"]["border"][0]
        for key, value in (("name", "../outside.png"), ("name", 1), ("sha256", "x"), ("sha256", 42), ("size", True), ("size", "1"), ("size", 0), ("size", 2 * 1024**3 + 1), ("media_type", "text/html"), ("media_type", [])):
            invalid_rows.append({**first, key: value})
        for category in ("border", "light"):
            for index, row in enumerate(invalid_rows):
                value = copy.deepcopy(self.document)
                value["categories"][category][0] = row
                with self.subTest(category=category, index=index):
                    self.assert_unavailable(digest=self.write_manifest(value))

    def test_empty_duplicate_and_oversized_categories_are_rejected(self):
        first = self.document["categories"]["border"][0]
        for rows in ([], {}, "invalid", [first, first], [first] * 1001):
            value = copy.deepcopy(self.document)
            value["categories"]["border"] = rows
            with self.subTest(kind=type(rows).__name__, length=len(rows)):
                self.assert_unavailable(digest=self.write_manifest(value))


if __name__ == "__main__":
    unittest.main()

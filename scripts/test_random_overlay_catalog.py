#!/usr/bin/env python3
"""Offline tests for trusted overlay catalog selection."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.random_overlay_catalog import (
    AssetCatalogError, CATALOGS_ENV, configured_asset_catalogs, resolve_asset_catalog,
)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = self.root / "current-assets"
        self.previous = self.root / "previous-assets"
        self.default = {"asset_root": self.current, "manifest_sha256": "a" * 64}

    def test_default_works_without_a_registry_or_filesystem_access(self):
        self.assertEqual(
            resolve_asset_catalog(**self.default, catalogs_json=""),
            (self.current, "a" * 64),
        )
        self.assertFalse(self.current.exists())

    def test_environment_selects_only_the_explicit_old_catalog(self):
        value = json.dumps({"b" * 64: str(self.previous)})
        with mock.patch.dict(os.environ, {CATALOGS_ENV: value}):
            self.assertEqual(
                resolve_asset_catalog(**self.default, requested_sha256="B" * 64),
                (self.previous, "b" * 64),
            )
            self.assertEqual(resolve_asset_catalog(**self.default), (self.current, "a" * 64))

    def test_current_directory_cannot_be_redefined(self):
        raw = json.dumps({"a" * 64: str(self.previous)})
        with self.assertRaises(AssetCatalogError):
            resolve_asset_catalog(**self.default, catalogs_json=raw)

    def test_identical_default_entry_is_allowed(self):
        raw = json.dumps({"a" * 64: str(self.current)})
        self.assertEqual(configured_asset_catalogs(**self.default, catalogs_json=raw),
                         {"a" * 64: self.current})

    def test_unknown_or_malformed_requested_sha_fails_closed(self):
        for value in ("", "c" * 64, "b" * 63, "../assets", 123, {"sha": "a" * 64}):
            with self.subTest(value=value), self.assertRaises(AssetCatalogError):
                resolve_asset_catalog(**self.default, requested_sha256=value, catalogs_json="{}")

    def test_invalid_json_shape_or_unused_entry_rejects_the_whole_registry(self):
        for raw in ("not json", "[]", "null", "42", "true",
                    json.dumps({"invalid": str(self.previous)}),
                    json.dumps({"b" * 64: None}),
                    json.dumps({"b" * 64: [str(self.previous)]})):
            with self.subTest(raw=raw), self.assertRaises(AssetCatalogError):
                resolve_asset_catalog(**self.default, catalogs_json=raw)

    def test_absolute_safe_paths_are_required_for_default_and_registered_roots(self):
        for invalid in ("relative/assets", "", str(self.root / ".." / "escape"),
                        str(self.root) + "\x00suffix", str(self.root) + "\n",
                        str(Path(self.root.anchor))):
            with self.subTest(root=invalid):
                with self.assertRaises(AssetCatalogError):
                    resolve_asset_catalog(asset_root=invalid, manifest_sha256="a" * 64,
                                          catalogs_json="{}")
                with self.assertRaises(AssetCatalogError):
                    resolve_asset_catalog(**self.default,
                        catalogs_json=json.dumps({"b" * 64: invalid}))

    def test_duplicate_keys_cannot_silently_override_a_catalog(self):
        directory = json.dumps(str(self.previous))
        for first, second in (("b" * 64, "b" * 64), ("b" * 64, "B" * 64)):
            raw = '{"%s":%s,"%s":%s}' % (first, directory, second, directory)
            with self.subTest(first=first, second=second), self.assertRaises(AssetCatalogError):
                resolve_asset_catalog(**self.default, catalogs_json=raw)

    def test_registry_size_is_bounded(self):
        entries = {"%064x" % index: str(self.previous) for index in range(65)}
        for raw in (json.dumps(entries), " " * 65537):
            with self.subTest(length=len(raw)), self.assertRaises(AssetCatalogError):
                resolve_asset_catalog(**self.default, catalogs_json=raw)


if __name__ == "__main__":
    unittest.main()

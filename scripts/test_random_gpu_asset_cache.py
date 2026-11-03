"""Admission regression: all selectable immutable layers, no silent fallback."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from features.random_gpu.asset_cache import validate_configured_cache, VERSION


class AssetCacheAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'layer.webm'
        self.source.write_bytes(b'immutable source')
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.target = self.root / (sha + '.nut')
        self.target.write_bytes(b'verified RGBA frames')
        self.receipt = self.root / (sha + '.json')
        self.receipt.write_text(json.dumps({'version': VERSION, 'source_sha256': sha,
            'rgba_frames_verified': True, 'size': self.target.stat().st_size,
            'mtime_ns': self.target.stat().st_mtime_ns}))
        self.env = {'RANDOM_GPU_ASSET_CACHE_ROOT': str(self.root),
            'X_POST_RANDOM_TEMPLATE_ASSET_ROOT': str(self.root),
            'X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256': 'a' * 64}
        self.assets = {'categories': {c: [{'path': self.source}] for c in
            ('border', 'opacity_video', 'corners', 'tint')}}
        # Light assets are retained in manifests but are never selected by v3.
        self.assets['categories']['light'] = [{'path': self.root / 'unused.webm'}]
        self.loader = self.enterContext(patch('features.fb_gpu.random_overlay.load_asset_set', return_value=self.assets))

    def test_complete_cache_checked_without_unused_legacy_layer(self):
        self.assertEqual(validate_configured_cache(self.env),
            {'enabled': True, 'catalogs_verified': 1, 'assets_verified': 1})

    def test_missing_selected_layer_blocks_admission(self):
        self.receipt.unlink()
        with self.assertRaises(FileNotFoundError): validate_configured_cache(self.env)

    def test_modified_cache_blocks_admission(self):
        self.target.write_bytes(b'changed content and size')
        with self.assertRaisesRegex(ValueError, 'changed or unverified'):
            validate_configured_cache(self.env)

    def test_incomplete_configuration_is_not_silently_disabled(self):
        with self.assertRaisesRegex(ValueError, 'asset_cache_catalog_missing'):
            validate_configured_cache({'RANDOM_GPU_ASSET_CACHE_ROOT': str(self.root)})
        self.assertEqual(validate_configured_cache({})['enabled'], False)

    def test_all_advertised_catalogs_checked(self):
        env = {**self.env, 'RANDOM_OVERLAY_ASSET_CATALOGS': json.dumps({'b' * 64: str(self.root / 'older')})}
        self.assertEqual(validate_configured_cache(env)['catalogs_verified'], 2)
        self.assertEqual(self.loader.call_count, 2)


if __name__ == '__main__': unittest.main()

#!/usr/bin/env python3
"""Catalog rollover regression; all rendering, storage and TikTok APIs are fake."""

import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.random_overlay_catalog import configured_asset_catalogs
from features.tt_gpu import worker
from scripts.test_tt_gpu_worker import (
    FakeObjectStore, FakeRunner, FakeTikTokAPI, JOB_ID, input_probe,
    make_downloader, make_prepare, make_publish, make_random_overlay_config, prepared_probe,
)


class CatalogRolloverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old_config = make_random_overlay_config(self.root, gates=True)
        self.old_sha = self.old_config.random_overlay_manifest_sha256
        self.new_root = self.root / "expanded-assets"
        shutil.copytree(self.old_config.random_overlay_root, self.new_root)
        manifest = json.loads((self.new_root / "manifest.json").read_text())
        name, data = "border-additional.png", b"additional-catalog-asset"
        (self.new_root / name).write_bytes(data)
        manifest["categories"]["border"].append({
            "name": name, "media_type": "image/png", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
        raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        (self.new_root / "manifest.json").write_bytes(raw)
        self.new_sha = hashlib.sha256(raw).hexdigest()
        self.catalogs_json = json.dumps({self.old_sha: str(self.old_config.random_overlay_root)})
        roots = configured_asset_catalogs(asset_root=self.new_root,
            manifest_sha256=self.new_sha, catalogs_json=self.catalogs_json)
        catalog_assets = {sha: worker.load_asset_set(root, sha) for sha, root in roots.items()}
        self.new_config = replace(self.old_config,
            random_overlay_root=self.new_root, random_overlay_manifest_sha256=self.new_sha,
            random_overlay_assets=catalog_assets[self.new_sha],
            random_overlay_catalog_assets=catalog_assets)
        self.api = FakeTikTokAPI()
        self.downloads = []
        self.request = make_prepare(expected_profile=worker.RANDOM_OVERLAY_PROFILE,
                                    source_trim_tail_seconds=0)

    def processor(self, config, runner=None):
        return worker.TTPostGPUProcessor(config, runner=runner or FakeRunner(),
            downloader=make_downloader(self.downloads), object_store=FakeObjectStore(),
            tiktok_api=self.api)

    def prepared_old(self):
        processor = self.processor(self.old_config,
            FakeRunner([input_probe(28.5), prepared_probe(28.5)]))
        result = processor.prepare(self.request)
        return result, processor._prepare_manifest_path(JOB_ID)

    def config_env(self):
        config = self.new_config
        return {
            "TT_POST_GPU_ENABLED": "1", "TT_POST_GPU_INTERNAL_TOKEN": "i" * 40,
            "TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64": base64.urlsafe_b64encode(b"k" * 32).decode(),
            "TT_POST_GPU_ALLOWED_SOURCE_HOSTS": "media.example.com",
            "TT_POST_GPU_WORK_ROOT": str(config.work_root),
            "TT_POST_GPU_FFMPEG_BIN": config.ffmpeg_bin,
            "TT_POST_GPU_FFPROBE_BIN": config.ffprobe_bin,
            "TT_POST_GPU_FONT_FILE": str(config.font_file),
            "TT_POST_GPU_FIXED_OUTRO_PATH": str(config.fixed_outro_path),
            "TT_POST_GPU_LOGO_PATH": str(config.logo_path),
            "TT_POST_GPU_MEDIA_MODE": worker.RANDOM_OVERLAY_MEDIA_MODE,
            "TT_POST_GPU_RANDOM_OVERLAY_ROOT": str(self.new_root),
            "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256": self.new_sha,
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS": "0",
            "TT_POST_GPU_COS_SECRET_ID": "fixture-id", "TT_POST_GPU_COS_SECRET_KEY": "fixture-key",
            "TT_POST_GPU_COS_BUCKET": "fixture-bucket", "TT_POST_GPU_COS_REGION": "ap-fixture",
            "TT_POST_GPU_COS_DOMAIN": "https://pull.example.com",
            "RANDOM_OVERLAY_ASSET_CATALOGS": self.catalogs_json,
        }

    def test_startup_verifies_both_default_and_approved_historical_bundles(self):
        with mock.patch.dict(os.environ, self.config_env(), clear=True):
            loaded = worker.WorkerConfig.from_env()
        self.assertEqual(set(loaded.random_overlay_catalog_assets), {self.old_sha, self.new_sha})
        self.assertEqual(loaded.random_overlay_assets["manifest_sha256"], self.new_sha)

    def test_startup_rejects_tampered_historical_asset_even_when_default_is_valid(self):
        asset = next(self.old_config.random_overlay_root.glob("border-*.png"))
        asset.write_bytes(b"changed")
        with mock.patch.dict(os.environ, self.config_env(), clear=True):
            with self.assertRaises(worker.TTGPUError) as caught:
                worker.WorkerConfig.from_env()
        self.assertEqual(caught.exception.code, "invalid_configuration")
        self.assertIn("fingerprint", str(caught.exception))

    def test_ready_recipe_and_manifest_survive_a_default_catalog_change(self):
        original, manifest = self.prepared_old()
        before = manifest.read_bytes()
        runner = FakeRunner()
        restarted = self.processor(self.new_config, runner)
        reused = restarted.prepare(self.request)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["random_overlay_recipe"], original["random_overlay_recipe"])
        self.assertEqual(reused["assets"]["asset_set_sha256"], self.old_sha)
        self.assertEqual(reused["output_sha256"], original["output_sha256"])
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(runner.commands, [])

    def test_frozen_old_recipe_reaches_fake_publish_without_redrawing_or_repeat_init(self):
        original, manifest = self.prepared_old()
        before = manifest.read_bytes()
        restarted = self.processor(self.new_config)
        result = restarted.publish(make_publish(self.new_config))
        self.assertEqual(result["state"], "initialized")
        self.assertEqual(self.api.init_calls[0][2], original["output_url"])
        self.assertEqual(manifest.read_bytes(), before)
        with self.assertRaises(worker.TTGPUError) as caught:
            restarted.publish(make_publish(self.new_config))
        self.assertEqual(caught.exception.code, "tt_publish_reconcile_required")
        self.assertEqual(len(self.api.init_calls), 1)

    def test_fresh_job_uses_expanded_default_even_with_legacy_catalog_approved(self):
        processor = self.processor(self.new_config,
            FakeRunner([input_probe(28.5), prepared_probe(28.5)]))
        result = processor.prepare(self.request)
        self.assertEqual(result["random_overlay_recipe"]["asset_set_sha256"], self.new_sha)
        self.assertEqual(result["assets"]["asset_set_sha256"], self.new_sha)

    def test_unapproved_old_sha_blocks_reuse_and_fake_publish_before_any_api_write(self):
        _, manifest = self.prepared_old()
        before = manifest.read_bytes()
        config = replace(self.new_config, random_overlay_catalog_assets={})
        restarted = self.processor(config)
        with self.assertRaises(worker.TTGPUError):
            restarted.prepare(self.request)
        with self.assertRaises(worker.TTGPUError):
            restarted.publish(make_publish(config))
        self.assertEqual(self.api.init_calls, [])
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(len(self.downloads), 1)

    def test_old_catalog_does_not_allow_changed_source_identity(self):
        _, manifest = self.prepared_old()
        before = manifest.read_bytes()
        restarted = self.processor(self.new_config)
        changed = dict(self.request, source_url="https://media.example.com/other.mp4")
        with self.assertRaises(worker.TTGPUError) as caught:
            restarted.prepare(changed)
        self.assertEqual(caught.exception.code, "prepare_idempotency_conflict")
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(len(self.downloads), 1)

    def test_old_catalog_does_not_allow_tampered_selected_asset_identity(self):
        _, manifest = self.prepared_old()
        stored = json.loads(manifest.read_text())
        for field in ("request", "result"):
            stored[field]["random_overlay_recipe"]["assets"]["border"]["sha256"] = "f" * 64
        worker._atomic_write_json(manifest, stored)
        before = manifest.read_bytes()
        restarted = self.processor(self.new_config)
        with self.assertRaises(worker.TTGPUError):
            restarted.prepare(self.request)
        with self.assertRaises(worker.TTGPUError):
            restarted.publish(make_publish(self.new_config))
        self.assertEqual(self.api.init_calls, [])
        self.assertEqual(manifest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

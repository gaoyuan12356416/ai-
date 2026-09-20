#!/usr/bin/env python3
"""Catalog rollover regression; all rendering, storage and TikTok APIs are fake."""

import base64
from dataclasses import replace
import hashlib
import json
import http.client
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
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
        # Emulate an actual pre-source-overlay completed manifest: no receipt,
        # no optional field, and no migration/backfill at reuse time.
        manifest = processor._prepare_manifest_path(JOB_ID)
        stored = json.loads(manifest.read_text())
        for section in ("request", "result"):
            stored[section]["random_overlay_recipe"].pop("source_overlay")
        result["random_overlay_recipe"].pop("source_overlay")
        worker._atomic_write_json(manifest, stored)
        processor._random_recipe_path(JOB_ID).unlink()
        return result, manifest

    def failed_old(self):
        runner = FakeRunner([input_probe(28.5)])
        def fail_render(command, **kwargs):
            if "ffmpeg" in Path(command[0]).name.lower():
                # The immutable choices and actual source hash must be durable
                # before the first FFmpeg invocation, including failed renders.
                frozen = json.loads(processor._random_recipe_path(JOB_ID).read_text())
                self.assertIn("source_overlay", frozen["recipe"])
                self.assertIn("source_sha256", frozen)
                self.assertIn("source_size", frozen)
                return SimpleNamespace(returncode=1, stdout="", stderr="fixture failure")
            return runner(command, **kwargs)
        processor = self.processor(self.old_config, fail_render)
        with mock.patch.object(worker.shutil, "rmtree", wraps=shutil.rmtree) as cleanup:
            with self.assertRaises(worker.TTGPUError) as caught:
                processor.prepare(self.request)
        self.assertEqual(caught.exception.code, "random_overlay_transcode_failed")
        self.assertFalse(processor._prepare_manifest_path(JOB_ID).exists())
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(Path(cleanup.call_args.args[0]).parent, processor.jobs_root)
        path = processor._random_recipe_path(JOB_ID)
        return processor, path, json.loads(path.read_text())

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
        self.assertNotIn("source_overlay", reused["random_overlay_recipe"])
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
        self.assertIn("source_overlay", result["random_overlay_recipe"])

    def test_failed_recipe_survives_restart_catalog_change_and_cleanup(self):
        processor, path, frozen = self.failed_old()
        before = path.read_bytes()
        processor.cleanup_due_media()
        self.assertEqual(path.read_bytes(), before)
        # The receipt itself records the trusted historical root, even if it
        # is no longer the default or listed in the startup catalog mapping.
        config = replace(self.new_config, random_overlay_catalog_assets={})
        runner = FakeRunner([input_probe(28.5), prepared_probe(28.5)])
        restarted = self.processor(config, runner)
        with mock.patch.object(worker, "derive_recipe", side_effect=AssertionError("redrawn")):
            result = restarted.prepare(self.request)
        self.assertEqual(result["random_overlay_recipe"], frozen["recipe"])
        self.assertEqual(result["assets"]["asset_set_sha256"], self.old_sha)
        self.assertEqual(path.read_bytes(), before)
        manifest = restarted._prepare_manifest_path(JOB_ID)
        manifest_before = manifest.read_bytes()
        again = self.processor(config, FakeRunner())
        with mock.patch.object(worker, "derive_recipe", side_effect=AssertionError("redrawn")):
            reused = again.prepare(self.request)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["random_overlay_recipe"], frozen["recipe"])
        self.assertEqual(manifest.read_bytes(), manifest_before)
        self.assertEqual(path.read_bytes(), before)

    def test_download_failure_already_freezes_choices_for_retry(self):
        processor = self.processor(self.old_config)
        processor.downloader = mock.Mock(side_effect=worker.TTGPUError("source_download_failed", "fixture"))
        with self.assertRaises(worker.TTGPUError):
            processor.prepare(self.request)
        path = processor._random_recipe_path(JOB_ID)
        frozen = json.loads(path.read_text())
        self.assertNotIn("source_sha256", frozen)
        restarted = self.processor(self.new_config,
            FakeRunner([input_probe(28.5), prepared_probe(28.5)]))
        with mock.patch.object(worker, "derive_recipe", side_effect=AssertionError("redrawn")):
            result = restarted.prepare(self.request)
        self.assertEqual(result["random_overlay_recipe"], frozen["recipe"])
        self.assertIn("source_sha256", json.loads(path.read_text()))

    def test_frozen_legacy_receipt_keeps_original_graph_and_missing_field(self):
        _, path, frozen = self.failed_old()
        frozen["recipe"].pop("source_overlay")
        worker._atomic_write_json(path, frozen)
        before = path.read_bytes()
        runner = FakeRunner([input_probe(28.5), prepared_probe(28.5)])
        restarted = self.processor(self.new_config, runner)
        with mock.patch.object(worker, "derive_recipe", side_effect=AssertionError("redrawn")):
            result = restarted.prepare(self.request)
        self.assertEqual(result["random_overlay_recipe"], frozen["recipe"])
        self.assertNotIn("source_overlay", result["random_overlay_recipe"])
        command = next(command for command in runner.commands if "-filter_complex" in command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("split=2[backraw][mainraw]", graph)
        self.assertNotIn("sourceghost", graph)
        self.assertEqual(path.read_bytes(), before)

    def test_unreadable_completed_manifest_is_never_overwritten(self):
        _, manifest = self.prepared_old()
        manifest.write_bytes(b"unreadable-completed-manifest")
        before = manifest.read_bytes()
        restarted = self.processor(self.new_config)
        with mock.patch.object(worker, "derive_recipe", side_effect=AssertionError("redrawn")):
            with self.assertRaises(worker.TTGPUError) as caught:
                restarted.prepare(self.request)
        self.assertEqual(caught.exception.code, "random_overlay_recipe_invalid")
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(len(self.downloads), 1)

    def test_failed_recipe_rejects_changed_source_before_probe_or_render(self):
        _, path, _ = self.failed_old()
        before = path.read_bytes()
        runner = FakeRunner()
        restarted = self.processor(self.new_config, runner)
        def changed_source(url, destination, *args):
            data = b"different-source-at-same-url"
            Path(destination).write_bytes(data)
            return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        restarted.downloader = changed_source
        with self.assertRaises(worker.TTGPUError) as caught:
            restarted.prepare(self.request)
        self.assertEqual(caught.exception.code, "source_integrity_mismatch")
        self.assertEqual(runner.commands, [])
        self.assertEqual(path.read_bytes(), before)

    def test_failed_recipe_rejects_changed_request_and_tampered_catalog(self):
        _, path, _ = self.failed_old()
        before = path.read_bytes()
        restarted = self.processor(self.new_config)
        with self.assertRaises(worker.TTGPUError) as caught:
            restarted.prepare(dict(self.request, source_url="https://media.example.com/different.mp4"))
        self.assertEqual(caught.exception.code, "prepare_idempotency_conflict")
        next(self.old_config.random_overlay_root.glob("border-*.png")).write_bytes(b"tampered")
        with self.assertRaises(worker.TTGPUError) as caught:
            restarted.prepare(self.request)
        self.assertEqual(caught.exception.code, "random_overlay_recipe_invalid")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.downloads), 1)

    def test_malformed_frozen_recipe_is_not_replaced_or_rendered(self):
        _, path, frozen = self.failed_old()
        cases = [b"not-json", json.dumps({**frozen, "version": True}).encode(),
                 json.dumps({**frozen, "source_size": True}).encode(),
                 json.dumps({**frozen, "recipe": {**frozen["recipe"], "source_overlay": None}}).encode()]
        for content in cases:
            with self.subTest(content=content[:30]):
                path.write_bytes(content)
                restarted = self.processor(self.new_config)
                with self.assertRaises(worker.TTGPUError) as caught:
                    restarted.prepare(self.request)
                self.assertEqual(caught.exception.code, "random_overlay_recipe_invalid")
                self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(self.downloads), 1)

    def test_random_health_advertises_source_overlay_capability(self):
        processor = self.processor(self.new_config)
        server = worker.TTPostGPUHTTPServer(("127.0.0.1", 0), processor, self.new_config.internal_token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", worker.HEALTH_PATH)
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["source_overlay_version"], 1)
            self.assertEqual(payload["profile"], worker.RANDOM_OVERLAY_PROFILE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

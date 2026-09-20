"""FB source-overlay contract and durable retry regression coverage."""
import copy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from features.fb_gpu import prepare_worker as worker
from features.fb_gpu.random_overlay import (
    ASSET_CATEGORIES, RandomOverlayError, derive_recipe, load_asset_set,
    validate_recipe,
)
from features.random_gpu.compositor import BACKEND
from scripts.test_fb_gpu_prepare_worker import config, request


def catalog(root, marker=b"asset"):
    root.mkdir(parents=True)
    rows = {}
    for category in ASSET_CATEGORIES:
        path = root / (category + ".png")
        data = marker + category.encode()
        path.write_bytes(data)
        rows[category] = [{"media_type": "image/png", "name": path.name,
                           "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "categories": rows}), encoding="utf-8")
    return load_asset_set(root, hashlib.sha256(manifest.read_bytes()).hexdigest())


def make_processor(root, assets, *, fail_upload=False):
    processor = worker.PrepareProcessor.__new__(worker.PrepareProcessor)
    processor.config = replace(config(root), asset_root=assets["root"],
                               asset_manifest_sha256=assets["manifest_sha256"])
    processor.assets = assets
    processor.lock = threading.Lock()
    processor.slots = threading.BoundedSemaphore(1)
    processor.job_locks = [threading.Lock() for _ in range(64)]
    processor.active_jobs = set()
    processor.last_cleanup_at = time.monotonic()
    (root / "jobs").mkdir()
    processor.source_bytes = b"original-source"
    processor.downloads = 0
    processor.commands = []
    processor.uploads = 0

    def download(url, path):
        processor.downloads += 1
        path.write_bytes(processor.source_bytes)
        return hashlib.sha256(processor.source_bytes).hexdigest()

    def render(command, **kwargs):
        # Receipt must already be durable when an FFmpeg call becomes possible.
        receipt = Path(command[-1]).parent / "recipe.json"
        if not receipt.is_file():
            raise AssertionError("render started before recipe freeze")
        processor.commands.append(command)
        Path(command[-1]).write_bytes(b"encoded-video")

    def upload(path, job, sha, size):
        processor.uploads += 1
        if fail_upload and processor.uploads == 1:
            raise worker.PrepareWorkerError("fb_gpu_cos_upload_failed", "test upload failure", 502)
        return {"url": "https://media.example.com/output.mp4", "key": sha, "reused": False}

    processor._download = download
    processor.runner = render
    processor.object_store = type("Store", (), {"upload": staticmethod(upload)})()
    return processor


PROBE = {"duration": 10.25, "has_audio": True,
         "video": {"codec_name": "h264", "profile": "High", "width": 720, "height": 1280},
         "audio": {}}


class AssetFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = catalog(self.root / "old-assets")
        self.recipe = derive_recipe(job_id="job1", content_id="d1", profile=worker.PROFILE,
                                    source_url_sha256="e" * 64, asset_set=self.assets)


class SourceOverlayTests(AssetFixture, unittest.TestCase):
    def test_health_advertises_source_overlay_capability(self):
        processor = type("Processor", (), {"config": config(self.root)})()
        server = worker.Server(("127.0.0.1", 0), processor, "x" * 32)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            connection.request("GET", "/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["source_overlay_version"], 1)
            self.assertEqual(payload["profile"], worker.PROFILE)
            self.assertTrue(payload["prepare_only"])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_new_recipes_deterministic_strict_and_reach_both_bounds(self):
        again = derive_recipe(job_id="job1", content_id="d1", profile=worker.PROFILE,
                              source_url_sha256="e" * 64, asset_set=self.assets)
        self.assertEqual(self.recipe, again)
        for index in range(300):
            recipe = derive_recipe(job_id=str(index), content_id="d1", profile=worker.PROFILE,
                                   source_url_sha256="e" * 64, asset_set=self.assets)
            overlay = recipe["source_overlay"]
            self.assertEqual(set(overlay), {"version", "opacity_bp", "scale_bp"})
            self.assertTrue(all(type(value) is int for value in overlay.values()))
            self.assertTrue(200 <= overlay["opacity_bp"] <= 500)
            self.assertTrue(11000 <= overlay["scale_bp"] <= 15000)
        for high in (False, True):
            def seed(_identity, label):
                return {"source-overlay-opacity": 300, "source-overlay-scale": 4000}.get(label, 0) if high else 0
            with patch("features.fb_gpu.random_overlay._seed", side_effect=seed):
                recipe = derive_recipe(job_id="j", content_id="d", profile=worker.PROFILE,
                                       source_url_sha256="e" * 64, asset_set=self.assets)
            self.assertEqual(recipe["source_overlay"], {"version": 1, "opacity_bp": 500 if high else 200,
                                                       "scale_bp": 15000 if high else 11000})

    def test_identity_labels_are_stable_without_changing_existing_draws(self):
        # Existing stable identity plus the two new labels have a fixed result.
        assets = {**self.assets, "manifest_sha256": "f" * 64}
        recipe = derive_recipe(job_id="job1", content_id="d1", profile=worker.PROFILE,
                               source_url_sha256="e" * 64, asset_set=assets)
        self.assertEqual(recipe["source_overlay"], {"version": 1, "opacity_bp": 230, "scale_bp": 11236})
        self.assertEqual(recipe["version"], 1)
        self.assertEqual(worker.PROFILE, "tt-post-random-overlay-h264-720x1280-v3")

    def test_legacy_validates_without_mutation_and_present_object_is_strict(self):
        legacy = copy.deepcopy(self.recipe)
        del legacy["source_overlay"]
        before = copy.deepcopy(legacy)
        validate_recipe(legacy, self.assets)
        self.assertEqual(legacy, before)
        invalid = [None, False, [], {}, {"version": 1, "opacity_bp": 200},
                   {**self.recipe["source_overlay"], "extra": 1}]
        for field, values in {
            "version": (None, True, 1.0, "1", 0, 2),
            "opacity_bp": (None, False, 200.0, "200", 199, 501),
            "scale_bp": (None, False, 11000.0, "11000", 10999, 15001),
        }.items():
            invalid += [{**self.recipe["source_overlay"], field: value} for value in values]
        for overlay in invalid:
            with self.subTest(overlay=overlay), self.assertRaises(RandomOverlayError):
                validate_recipe({**self.recipe, "source_overlay": overlay}, self.assets)

    def test_legacy_graph_unchanged_and_new_layer_topmost_without_audio_changes(self):
        paths = {name: self.root / (name + ".bin") for name in ("border", "opacity_video", "corners", "tint")}
        legacy = {"rotation_millidegrees": 1250, "scale_bp": 9900, "tint_opacity_bp": 500}
        new_recipe = {**legacy, "source_overlay": {"version": 1, "opacity_bp": 500, "scale_bp": 15000}}
        for has_audio in (True, False):
            cfg = config(self.root)
            info = {"duration": 10.25, "has_audio": has_audio}
            build = lambda recipe: worker.build_command(cfg, self.root / "source.mp4", self.root / "output.mp4", info, recipe, paths)
            old, new = build(legacy), build(new_recipe)
            old_graph = old[old.index("-filter_complex") + 1]
            # Byte-for-byte legacy graph from FB baseline e3bef98.
            self.assertEqual(hashlib.sha256(old_graph.encode()).hexdigest(),
                             "13f195cc37d7d78bac9e8eb8c4761fcb3a5bfdacdc27218840dc82674aa5ddd2")
            self.assertEqual(old[old.index("-map"):], new[new.index("-map"):])
            self.assertEqual(old[:old.index("-filter_complex")], new[:new.index("-filter_complex")])
            graph = new[new.index("-filter_complex") + 1]
            self.assertIn("[0:v]setpts=PTS-STARTPTS,fps=30,split=3[backraw][mainraw][sourceraw]", graph)
            source_layer = next(part for part in graph.split(";") if part.startswith("[sourceraw]"))
            self.assertIn("force_original_aspect_ratio=increase", source_layer)
            self.assertIn("crop=720:1280", source_layer)
            self.assertIn("iw*1.5000", source_layer)
            self.assertIn("aa=0.0500", source_layer)
            self.assertNotIn("rotate", source_layer)
            self.assertTrue(graph.endswith("[composite][sourceghost]overlay=(W-w)/2:(H-h)/2:shortest=1:eof_action=repeat,format=yuv420p[v]"))
            self.assertLess(graph.index("[o3][corners]"), graph.index("[composite][sourceghost]"))
            self.assertNotIn("amix", graph)
            self.assertNotIn("asplit", graph)
            self.assertIn("0:a:0" if has_audio else "5:a:0", new)
            with self.assertRaises(ValueError): build({**legacy, "source_overlay": None})

    def test_gpu_cache_identity_includes_source_overlay_parameters(self):
        paths = {name: self.root / (name + ".bin") for name in ("border", "opacity_video", "corners", "tint")}
        cfg = replace(config(self.root), compositor_backend=BACKEND)
        graphs = []
        for overlay in (None, {"version": 1, "opacity_bp": 200, "scale_bp": 11000},
                        {"version": 1, "opacity_bp": 201, "scale_bp": 11000},
                        {"version": 1, "opacity_bp": 200, "scale_bp": 11001}):
            recipe = {key: value for key, value in self.recipe.items() if key != "source_overlay"}
            if overlay is not None: recipe["source_overlay"] = overlay
            command = worker.build_command(cfg, self.root / "source.mp4", self.root / "output.mp4",
                                           PROBE, recipe, paths)
            graphs.append(command[command.index("-filter_complex") + 1])
        self.assertEqual(len(set(graphs)), 4)
        self.assertEqual(len(list(self.root.glob("compositor-*.cl"))), 4)


class FrozenRecipeTests(AssetFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.probe = patch("features.fb_gpu.prepare_worker._probe", return_value=PROBE)
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.disk = patch("features.fb_gpu.prepare_worker.shutil.disk_usage", return_value=type("Disk", (), {"free": 64 * 1024 ** 3})())
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def processor(self, **kwargs):
        return make_processor(self.root, self.assets, **kwargs)

    def test_completed_legacy_manifest_reused_verbatim(self):
        processor = self.processor()
        root = self.root / "jobs" / request()["job_id"]
        root.mkdir()
        legacy = {key: value for key, value in self.recipe.items() if key != "source_overlay"}
        stored = {"request": worker.validate_request(request(), processor.config),
                  "result": {"status": "ready", "random_overlay_recipe": legacy, "output_url": "https://media.example.com/old.mp4"}}
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        original = manifest.read_bytes()
        with patch("features.fb_gpu.prepare_worker.derive_recipe", side_effect=AssertionError("must reuse")):
            result = processor.prepare(request())
        self.assertTrue(result["reused"])
        self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((root / "recipe.json").exists())
        self.assertEqual(processor.downloads, 0)
        self.assertEqual(processor.commands, [])

    def test_upload_retry_after_cleanup_and_catalog_change_uses_frozen_recipe(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        root = self.root / "jobs" / request()["job_id"]
        receipt = root / "recipe.json"
        frozen_bytes = receipt.read_bytes()
        frozen = json.loads(frozen_bytes)
        self.assertIn("source_overlay", frozen["recipe"])
        self.assertEqual(frozen["source_sha256"], hashlib.sha256(b"original-source").hexdigest())
        self.assertFalse((root / "manifest.json").exists())
        processor.assets = catalog(self.root / "new-assets", b"next-catalog")
        (root / "keep-note.txt").write_text("preserve", encoding="utf-8")
        (root / ("compositor-" + "a" * 20 + ".cl")).write_text("kernel", encoding="utf-8")
        self.assertEqual(worker.cleanup_stale_failed_jobs(processor.config, now_fn=lambda: time.time() + 604801), 1)
        self.assertEqual(receipt.read_bytes(), frozen_bytes)
        self.assertTrue((root / "keep-note.txt").is_file())
        self.assertFalse((root / "source.mp4").exists())
        with patch("features.fb_gpu.prepare_worker.derive_recipe", side_effect=AssertionError("must not redraw")):
            result = processor.prepare(request())
        self.assertEqual(result["random_overlay_recipe"], frozen["recipe"])
        self.assertEqual(receipt.read_bytes(), frozen_bytes)
        self.assertEqual(processor.downloads, 2)
        self.assertEqual(processor.commands[0], processor.commands[1])
        self.assertEqual(processor.uploads, 2)
        self.assertTrue((root / "manifest.json").is_file())

    def test_changed_source_after_cleanup_fails_closed(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        root = self.root / "jobs" / request()["job_id"]
        frozen = (root / "recipe.json").read_bytes()
        worker.cleanup_stale_failed_jobs(processor.config, now_fn=lambda: time.time() + 604801)
        processor.source_bytes = b"changed-source-at-same-url"
        with self.assertRaises(worker.PrepareWorkerError) as caught: processor.prepare(request())
        self.assertEqual(caught.exception.code, "fb_gpu_source_conflict")
        self.assertEqual((root / "recipe.json").read_bytes(), frozen)
        self.assertEqual(len(processor.commands), 1)
        self.assertFalse((root / "manifest.json").exists())

    def test_unavailable_frozen_catalog_never_uses_new_default(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        receipt = self.root / "jobs" / request()["job_id"] / "recipe.json"
        original = receipt.read_bytes()
        processor.assets = catalog(self.root / "new-assets", b"next-catalog")
        # Loss of the historical catalog must fail instead of silently drawing
        # from the next default or accepting assets with different fingerprints.
        (self.assets["root"] / "manifest.json").write_text("tampered", encoding="utf-8")
        with patch("features.fb_gpu.prepare_worker.derive_recipe", side_effect=AssertionError("must not redraw")):
            with self.assertRaises(worker.PrepareWorkerError) as caught: processor.prepare(request())
        self.assertEqual(caught.exception.code, "fb_gpu_recipe_invalid")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(len(processor.commands), 1)

    def test_failed_durable_write_stops_before_first_render(self):
        processor = self.processor()
        with patch("features.fb_gpu.prepare_worker.os.fsync", side_effect=OSError("storage failed")):
            with self.assertRaises(OSError): processor.prepare(request())
        root = self.root / "jobs" / request()["job_id"]
        self.assertFalse((root / "recipe.json").exists())
        self.assertFalse((root / "recipe.tmp").exists())
        self.assertEqual(processor.commands, [])
        self.assertEqual(processor.uploads, 0)

    def test_frozen_legacy_receipt_does_not_get_upgraded(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        receipt = self.root / "jobs" / request()["job_id"] / "recipe.json"
        frozen = json.loads(receipt.read_text(encoding="utf-8"))
        del frozen["recipe"]["source_overlay"]
        receipt.write_text(json.dumps(frozen), encoding="utf-8")
        original = receipt.read_bytes()
        with patch("features.fb_gpu.prepare_worker.derive_recipe", side_effect=AssertionError("legacy frozen")):
            result = processor.prepare(request())
        self.assertNotIn("source_overlay", result["random_overlay_recipe"])
        self.assertEqual(receipt.read_bytes(), original)
        command = processor.commands[-1]
        self.assertNotIn("sourceghost", command[command.index("-filter_complex") + 1])

    def test_request_conflict_and_corrupt_receipt_never_redraw(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        receipt = self.root / "jobs" / request()["job_id"] / "recipe.json"
        original = receipt.read_bytes()
        with self.assertRaises(worker.PrepareWorkerError) as caught:
            processor.prepare(request(content_id="changed"))
        self.assertEqual(caught.exception.code, "fb_gpu_job_conflict")
        self.assertEqual(receipt.read_bytes(), original)
        frozen = json.loads(original)
        frozen["recipe"]["source_overlay"] = None
        receipt.write_text(json.dumps(frozen), encoding="utf-8")
        with patch("features.fb_gpu.prepare_worker.derive_recipe", side_effect=AssertionError("must fail closed")):
            with self.assertRaises(worker.PrepareWorkerError) as caught: processor.prepare(request())
        self.assertEqual(caught.exception.code, "fb_gpu_recipe_invalid")
        self.assertEqual(len(processor.commands), 1)

    def test_frozen_cleanup_does_not_follow_scratch_symlinks(self):
        processor = self.processor(fail_upload=True)
        with self.assertRaises(worker.PrepareWorkerError): processor.prepare(request())
        root = self.root / "jobs" / request()["job_id"]
        source = root / "source.mp4"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda value: value == source or original(value)):
            worker.cleanup_stale_failed_jobs(processor.config, now_fn=lambda: time.time() + 604801)
        self.assertEqual(source.read_bytes(), b"original-source")
        self.assertTrue((root / "recipe.json").is_file())


if __name__ == "__main__":
    unittest.main()

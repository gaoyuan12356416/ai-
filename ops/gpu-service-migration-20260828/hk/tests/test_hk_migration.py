import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import check_storage
import deploy
import merge_x_manifests as manifests


class HkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, directory, status="ready", profile=deploy.X_PROFILE):
        key = "a" * 64
        value = {
            "version": 4, "status": status, "cos_key": "x-post-media-repair/example.mp4",
            "request": {
                "job_key": key, "material_id": "123", "pool_item_id": "456",
                "source_url": "https://example.test/source.mp4", "source_sha256": "b" * 64,
                "source_size": 1024, "trigger_code": "invalid_media_codec",
                "profile": profile, "duration_policy": "premium",
            },
            "result": {
                "job_key": key, "profile": profile, "output_url": "https://example.test/out.mp4",
                "output_sha256": "c" * 64, "output_size": 1000, "probe": {},
            },
        }
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (key + ".json")
        path.write_text(json.dumps(value))
        return path

    def test_volume_uuid_and_free_space_fail_closed(self):
        usage = type("Usage", (), {"free": 30 * 1024 ** 3})()
        with mock.patch.object(check_storage, "require_boundary", return_value=self.root), \
             mock.patch.object(check_storage.shutil, "disk_usage", return_value=usage), \
             mock.patch.object(check_storage.subprocess, "check_output", return_value="wrong\n"):
            with self.assertRaises(ValueError):
                check_storage.inspect_storage(str(self.root))
        with mock.patch.object(check_storage, "require_boundary", return_value=self.root), \
             mock.patch.object(check_storage.shutil, "disk_usage", return_value=usage), \
             mock.patch.object(check_storage.subprocess, "check_output",
                               return_value=check_storage.EXPECTED_UUID + "\n"):
            self.assertFalse(check_storage.inspect_storage(str(self.root))["independent_mount_required"])
            with self.assertRaises(ValueError):
                check_storage.inspect_storage(str(self.root), min_free_gib=31)

    def test_boundary_rejects_sibling_and_parent(self):
        allowed = self.root / "service"
        allowed.mkdir()
        self.assertEqual(check_storage.require_boundary(allowed, [str(allowed)]), allowed)
        for bad in (self.root, self.root / "service-other"):
            with self.assertRaises(ValueError):
                check_storage.require_boundary(bad, [str(allowed)])

    def test_cutover_requires_both_explicit_gates(self):
        for approval, paused in [(None, True), (deploy.RUN_ID, False), ("different", True)]:
            with self.assertRaises(ValueError):
                deploy.require_cutover(approval, paused)
        deploy.require_cutover(deploy.RUN_ID, True)

    def test_archive_rejects_path_and_symlink_escape(self):
        for name, link in [("../outside", None), ("link", "../../outside")]:
            archive = self.root / ("test-" + str(bool(link)) + ".tgz")
            with tarfile.open(archive, "w:gz") as out:
                info = tarfile.TarInfo(name)
                if link:
                    info.type = tarfile.SYMTYPE
                    info.linkname = link
                    out.addfile(info)
                else:
                    info.size = 1
                    out.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ValueError):
                deploy.safe_extract(archive, self.root / "extract")
        self.assertFalse((self.root / "outside").exists())

    def test_archive_requires_completed_matching_transfer_proof(self):
        archive = self.root / "ad-data.tgz"
        archive.write_bytes(b"archive-test-data")
        with mock.patch.object(deploy, "INPUTS", self.root):
            with self.assertRaises(ValueError):
                deploy.verified_archive(archive.name)
            proof = self.root / (archive.name + ".verified.json")
            proof.write_text(json.dumps({"files": [{"name": archive.name,
                "bytes": archive.stat().st_size, "sha256": deploy.digest(archive)}]}))
            self.assertEqual(deploy.verified_archive(archive.name), archive)
            archive.write_bytes(b"altered-data")
            with self.assertRaises(ValueError):
                deploy.verified_archive(archive.name)

    def test_hk_collision_never_queries_head_or_overwrites(self):
        source = self.manifest(self.root / "us")
        target = self.manifest(self.root / "hk")
        checker = mock.Mock(return_value=True)
        self.assertEqual(manifests.classify(source, target, checker), "already_present")
        saved = target.read_bytes()
        source.write_text(source.read_text().replace('"output_size": 1000', '"output_size": 2000'))
        self.assertEqual(manifests.classify(source, target, checker), "hk_kept")
        self.assertEqual(target.read_bytes(), saved)
        checker.assert_not_called()

    def test_old_profile_and_missing_status_are_archive_only(self):
        missing = self.root / "missing.json"
        source = self.manifest(self.root / "us", status=None)
        self.assertEqual(manifests.classify(source, missing), "not_ready")
        source = self.manifest(self.root / "us", profile="old-v3")
        self.assertEqual(manifests.classify(source, missing), "historical_profile")
        source = self.manifest(self.root / "us")
        self.assertEqual(manifests.classify(source, missing), "eligible_requires_head")
        self.assertEqual(manifests.classify(source, missing, lambda _: False), "head_failed")

    def test_apply_requires_stopped_worker_and_is_idempotent(self):
        xroot = self.root / "x"
        target = xroot / "state/manifests"
        target.mkdir(parents=True)
        self.manifest(self.root / "us")
        with mock.patch.object(manifests, "X_ROOT", xroot), \
             mock.patch.object(manifests, "active", return_value=True):
            with self.assertRaises(ValueError):
                manifests.merge(self.root / "us", target, apply=True, with_head=True)
        with mock.patch.object(manifests, "X_ROOT", xroot), \
             mock.patch.object(manifests, "active", return_value=False), \
             mock.patch.object(manifests, "load_head_checker", return_value=lambda _: True), \
             mock.patch.object(manifests, "write_json"):
            first = manifests.merge(self.root / "us", target, apply=True, with_head=True)
            second = manifests.merge(self.root / "us", target, apply=True, with_head=True)
        self.assertEqual(first["counts"], {"imported": 1})
        self.assertEqual(second["counts"], {"already_present": 1})

    def test_source_capture_and_original_url_contract(self):
        provenance = json.loads((ROOT / "source/provenance.json").read_text())
        for item in provenance["files"]:
            self.assertEqual(hashlib.sha256((ROOT / "source" / item["name"]).read_bytes()).hexdigest(),
                             item["sha256"])
        spec = importlib.util.spec_from_file_location(
            "frozen_generation", ROOT / "source/ad_material_generation_service.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.PUBLIC_ROOT = self.root / "public"
        self.assertEqual(module.public_url(module.PUBLIC_ROOT / "job" / "asset.png"),
                         "http://127.0.0.1:18797/files/job/asset.png")
        with self.assertRaises(ValueError):
            module.public_url(self.root / "outside.png")
        for name in ("generation.env", "vision.env"):
            values = deploy.parse_env(ROOT / "env" / name)
            self.assertTrue(all("\n" not in value for value in values.values()))


if __name__ == "__main__":
    unittest.main()

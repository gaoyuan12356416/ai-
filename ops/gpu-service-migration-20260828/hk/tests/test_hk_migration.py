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
import ad_models_probe as models_probe
import x_offline_pipeline as offline_pipeline


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

    def test_dependency_tunnel_only_resumes_original_active_state(self):
        for was_active in (True, False):
            with self.subTest(was_active=was_active):
                snapshot = {"fragment": "/etc/systemd/system/x-post-media-repair-tunnel.service",
                            "sha256": "known-fragment", "enabled": "enabled", "active": was_active}
                with mock.patch.object(deploy, "BACKUP", self.root / str(was_active)), \
                     mock.patch.object(deploy, "tunnel_snapshot", side_effect=lambda: dict(snapshot)), \
                     mock.patch.object(deploy, "active", return_value=True), \
                     mock.patch.object(deploy, "run") as run:
                    deploy.capture_x_tunnel_baseline()
                    snapshot["active"] = False
                    result = deploy.restore_x_tunnel_if_previously_active()
                    self.assertEqual(result["restored"], was_active)
                    if was_active:
                        run.assert_called_once_with(["systemctl", "start", deploy.X_TUNNEL])
                    else:
                        run.assert_not_called()

    def test_dependency_tunnel_configuration_drift_is_not_overwritten(self):
        snapshot = {"fragment": "original-unit", "sha256": "original", "enabled": "enabled",
                    "active": True}
        with mock.patch.object(deploy, "BACKUP", self.root), \
             mock.patch.object(deploy, "tunnel_snapshot", side_effect=lambda: dict(snapshot)), \
             mock.patch.object(deploy, "run") as run:
            deploy.capture_x_tunnel_baseline()
            snapshot["sha256"] = "changed-by-another-operator"
            with self.assertRaises(ValueError):
                deploy.restore_x_tunnel_if_previously_active()
            run.assert_not_called()

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

    def test_catalog_probe_extracts_only_access_and_account_fields(self):
        fragment = models_probe.extract_fragment({"auth_mode": "chatgpt", "tokens": {
            "access_token": "test-access", "account_id": "test-account",
            "refresh_token": "must-not-transfer", "id_token": "must-not-transfer-either"}})
        self.assertEqual(set(fragment), {"access_token", "account_id"})
        with self.assertRaises(ValueError):
            models_probe.validate_fragment(dict(fragment, refresh_token="extra"))
        with self.assertRaises(ValueError):
            models_probe.validate_fragment(dict(fragment, access_token="bad\r\nheader"))
        self.assertTrue(str(models_probe.PROBE_BASE).startswith("/data/migrations/"))

    def test_catalog_probe_never_emits_response_body_or_identity(self):
        body = json.dumps({"error": {"code": "unsupported_country_region_territory",
                                    "message": "sensitive-account test-access"}}).encode()
        result = models_probe.safe_result(403, body, {
            "Content-Type": "application/problem+json; charset=utf-8", "Server": "cloudflare",
            "Set-Cookie": "sensitive-account test-access", "X-Request-Id": "private-identity"})
        self.assertEqual(result["safe_error_code"], "unsupported_country_region_territory")
        self.assertEqual(result["content_type_category"], "json")
        self.assertNotIn("sensitive-account", json.dumps(result))
        self.assertNotIn("test-access", json.dumps(result))
        self.assertNotIn("private-identity", json.dumps(result))
        unknown = models_probe.safe_result(403, b'{"error":{"code":"private-value"}}')
        self.assertEqual(unknown["safe_error_code"], "unclassified_http_error")

    def test_catalog_probe_is_fixed_get_without_redirect_or_model_call(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"models":[{"slug":"gpt-5.5"}]}'
        response.headers = {"Content-Type": "application/json"}
        opener = mock.Mock()
        opener.open.return_value = response
        result = models_probe.catalog_request({"access_token": "test-access",
                                               "account_id": "test-account"}, opener)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, models_probe.URL)
        self.assertIsNone(request.data)
        self.assertTrue(result["target_model_visible"])
        self.assertIsNone(models_probe.RefuseRedirect().redirect_request(
            request, None, 302, "redirect", {}, "https://example.test/collect"))

    def test_catalog_metadata_only_uses_whitelisted_page_categories(self):
        cases = [
            ("Just a moment...", "cloudflare", "cloudflare_challenge"),
            ("Attention Required! | Cloudflare", "cloudflare", "cloudflare_block"),
            ("Access denied", "nginx/1.0", "unclassified_html"),
            ("Unsupported country, region, or territory", "cloudflare", "region_restriction_notice"),
            ("Account deactivated", "cloudflare", "account_restriction_notice"),
            ("private-account sensitive-token", "private-server-identity", "unclassified_html"),
        ]
        for title, server, category in cases:
            with self.subTest(category=category, server=server):
                body = ("<html><title>" + title + "</title><body>private-account sensitive-token"
                        " unsupported_country_region_territory</body></html>").encode()
                result = models_probe.safe_result(403, body, {
                    "Content-Type": "text/html; charset=UTF-8", "Server": server})
                self.assertEqual(result["page_category"], category)
                serialized = json.dumps(result)
                for private in ("private-account", "sensitive-token", "private-server-identity"):
                    self.assertNotIn(private, serialized)

    def test_catalog_http_error_preserves_only_safe_challenge_metadata(self):
        body = io.BytesIO(b'<html><title>private-account</title>secret-token</html>')
        error = models_probe.urllib.error.HTTPError(models_probe.URL, 403, "Forbidden", {
            "Content-Type": "text/html", "Server": "cloudflare", "CF-Mitigated": "challenge",
            "Set-Cookie": "secret-token"}, body)
        opener = mock.Mock()
        opener.open.side_effect = error
        result = models_probe.catalog_request({"access_token": "test-access",
                                               "account_id": "test-account"}, opener)
        self.assertTrue(result["cf_mitigated_challenge"])
        self.assertEqual(result["page_category"], "cloudflare_challenge")
        self.assertEqual(result["safe_error_code"], "non_json_response")
        self.assertTrue(body.closed)
        self.assertNotIn("private-account", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))
        opener.open.assert_called_once()

    def test_catalog_probe_cannot_overwrite_an_existing_result(self):
        report = self.root / "result-HK.json"
        report.write_text('{"http_status":403}')
        original = report.read_bytes()
        checksum = hashlib.sha256((ROOT / "ad_models_probe.py").read_bytes()).hexdigest()
        with mock.patch.object(models_probe, "probe_directory", return_value=self.root), \
             mock.patch.object(models_probe, "catalog_request") as request:
            with self.assertRaises(ValueError):
                models_probe.remote_probe("HK", "1" * 40, checksum)
            request.assert_not_called()
        self.assertEqual(report.read_bytes(), original)

    def test_offline_adapters_only_copy_private_synthetic_files(self):
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"isolated-fixture")
        downloader = offline_pipeline.FakeDownloader(source, self.root)
        download = downloader(offline_pipeline.SOURCE_URL, self.root / "download.mp4",
                              ("offline.invalid",), max_bytes=1024,
                              http_client=offline_pipeline.RejectHTTP())
        self.assertEqual(download["sha256"], offline_pipeline.digest(source))
        with self.assertRaises(ValueError):
            downloader("https://production.invalid/file", self.root / "bad.mp4",
                       ("offline.invalid",), max_bytes=1024,
                       http_client=offline_pipeline.RejectHTTP())
        store = offline_pipeline.FakeCOS(self.root)
        with self.assertRaises(offline_pipeline.FakeNotFound):
            store.head_object(Bucket=offline_pipeline.FAKE_BUCKET, Key="output/result.mp4")
        arguments = dict(Bucket=offline_pipeline.FAKE_BUCKET, Key="output/result.mp4",
                         LocalFilePath=str(source), Metadata={"x-cos-meta-sha256": download["sha256"]})
        store.upload_file(**arguments)
        head = store.head_object(Bucket=offline_pipeline.FAKE_BUCKET, Key=arguments["Key"])
        self.assertEqual(head["x-cos-meta-sha256"], download["sha256"])
        self.assertEqual(int(head["Content-Length"]), source.stat().st_size)
        for key in ("../outside.mp4", "/absolute.mp4"):
            with self.assertRaises(ValueError):
                store.upload_file(**dict(arguments, Key=key))
        with self.assertRaises(ValueError):
            store.upload_file(**dict(arguments, Bucket="production"))
        self.assertEqual(store.uploads, 1)

    def test_offline_runner_rejects_other_binaries_network_and_outside_paths(self):
        binary = self.root / "ffmpeg"
        runner = offline_pipeline.RecordingRunner(self.root, binaries=[binary])
        with mock.patch.object(offline_pipeline.subprocess, "run") as run:
            run.return_value.returncode = 0
            runner([str(binary), "-i", str(self.root / "source.mp4")])
            for command in ([str(self.root / "other")],
                            [str(binary), "-i", "https://production.invalid/file"],
                            [str(binary), "-i", str(self.root.parent / "outside.mp4")]):
                with self.assertRaises(ValueError):
                    runner(command)
            run.assert_called_once()

    def test_offline_execution_requires_private_network_and_readonly_production(self):
        with mock.patch.object(offline_pipeline.os, "readlink", return_value="net:[1]"):
            with self.assertRaises(ValueError):
                offline_pipeline.verify_namespace(self.root)
        with mock.patch.object(offline_pipeline.os, "readlink", side_effect=["net:[2]", "net:[1]"]), \
             mock.patch.object(offline_pipeline.os, "statvfs", create=True,
                               return_value=type("Filesystem", (), {"f_flag": 0})()):
            with self.assertRaises(ValueError):
                offline_pipeline.verify_namespace(self.root)

    def test_offline_acceptance_evidence_is_never_overwritten(self):
        path = self.root / "attempt.json"
        offline_pipeline.write_once(path, {"attempt": 1})
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            offline_pipeline.write_once(path, {"attempt": 2})
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

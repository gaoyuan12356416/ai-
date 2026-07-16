import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy import apply_ad_control_v3 as deploy_v3


SOURCE_APP = b"""class Handler:\n    def do_GET(self):\n        parsed = urlparse(self.path)\n        legacy()\n"""

TARGET_APP = b'''class Handler:\n    def _dispatch_ad_control_v3(self, parsed):\n        """Lazily dispatch the isolated V3 surface after its prefix matched."""\n        try:\n            from features.ad_control_v3 import routes as ad_control_v3_routes\n            ad_control_v3_routes.dispatch(self, self.command, parsed)\n        except Exception:\n            logging.exception("ad-control V3 route dispatcher failed")\n            json_response(\n                self,\n                500,\n                {\n                    "code": "internal_error",\n                    "error": "internal server error",\n                    "message": "internal server error",\n                },\n                no_store=True,\n            )\n        return True\n\n    def do_GET(self):\n        parsed = urlparse(self.path)\n        if parsed.path == "/api/ad-control/v3" or parsed.path.startswith("/api/ad-control/v3/"):\n            self._dispatch_ad_control_v3(parsed)\n            return\n        legacy()\n'''


class AdControlV3ExactOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-control-v3-deploy-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.live = self.root / "live"
        self.backup = self.root / "data-disk-backups"
        self.repo.mkdir()
        self.live.mkdir()
        self._git("init")
        self._git("config", "user.name", "Codex Test")
        self._git("config", "user.email", "codex-test@example.invalid")
        (self.repo / "features" / "ad_control_v3").mkdir(parents=True)
        (self.repo / "features" / "ad_control_v3" / "__init__.py").write_bytes(b"VERSION = 2\n")
        (self.repo / "features" / "ad_control_v3" / "common.py").write_bytes(b"UNCHANGED = True\n")
        self._git("add", "features/ad_control_v3/__init__.py", "features/ad_control_v3/common.py")
        self.source_commit = self._commit_app(SOURCE_APP, "source")
        (self.repo / "features" / "ad_control_v3" / "assets").mkdir(parents=True)
        (self.repo / "features" / "ad_control_v3" / "templates").mkdir(parents=True)
        (self.repo / "features" / "ad_control_v3" / "channels").mkdir(parents=True)
        required_python = {
            "__init__.py": b"VERSION = 3\n",
            "catalog.py": b"CATALOG = True\n",
            "errors.py": b"ERRORS = True\n",
            "repository.py": b"REPOSITORY = True\n",
            "rule_engine.py": b"RULE_ENGINE = True\n",
            "routes.py": b"ROUTES = True\n",
            "schemas.py": b"SCHEMAS = True\n",
            "service.py": b"V3 = True\n",
            "storage.py": b"STORAGE = True\n",
            "page_renderer.py": b"RENDERER = True\n",
        }
        for relative, value in required_python.items():
            (self.repo / "features" / "ad_control_v3" / relative).write_bytes(value)
        for relative in ("__init__.py", "base.py", "facebook.py", "tiktok.py"):
            (self.repo / "features" / "ad_control_v3" / "channels" / relative).write_bytes(
                ("CHANNEL_%s = True\n" % relative.replace(".", "_").upper()).encode("ascii")
            )
        (self.repo / "features" / "ad_control_v3" / "assets" / "app.js").write_bytes(b"window.V3 = true;\n")
        (self.repo / "features" / "ad_control_v3" / "assets" / "app.css").write_bytes(b".v3 {}\n")
        (self.repo / "features" / "ad_control_v3" / "templates" / "rule-groups.html").write_bytes(b"<main>rules</main>\n")
        (self.repo / "features" / "ad_control_v3" / "templates" / "execution-logs.html").write_bytes(b"<main>logs</main>\n")
        (self.repo / "features" / "ad_control_v3" / "ignored.txt").write_bytes(b"not-runtime\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "ad_control_v3_runner.py").write_bytes(b"#!/usr/bin/env python3\n")
        (self.repo / "doc.txt").write_bytes(b"never-install\n")
        self._git("add", "features/ad_control_v3", "scripts/ad_control_v3_runner.py", "doc.txt")
        self.target_commit = self._commit_app(TARGET_APP, "target")
        (self.live / "app.py").write_bytes(SOURCE_APP)
        (self.live / "features" / "ad_control_v3").mkdir(parents=True)
        (self.live / "features" / "ad_control_v3" / "__init__.py").write_bytes(b"VERSION = 2\n")
        (self.live / "features" / "ad_control_v3" / "common.py").write_bytes(b"UNCHANGED = True\n")

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.repo)] + list(arguments),
            text=True,
            stderr=subprocess.STDOUT,
        )

    def _commit_app(self, value, message):
        (self.repo / "app.py").write_bytes(value)
        self._git("add", "app.py")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    def apply(self, check=False, backup=True):
        return deploy_v3.apply_release(
            root=self.live,
            repo=self.repo,
            source_revision=self.source_commit,
            target_revision=self.target_commit,
            backup_dir=self.backup if backup else None,
            check=check,
        )

    def test_check_apply_and_repeat_are_exact_and_idempotent(self):
        checked = self.apply(check=True)
        self.assertEqual("would_change", checked["status"])
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())
        self.assertFalse(self.backup.exists())

        applied = self.apply()
        self.assertEqual("changed", applied["status"])
        self.assertEqual(TARGET_APP, (self.live / "app.py").read_bytes())
        release_backups = list(self.backup.glob("ad-control-v3-*-to-*"))
        self.assertEqual(1, len(release_backups))
        self.assertEqual(SOURCE_APP, (release_backups[0] / "app.py").read_bytes())
        self.assertEqual(
            b"VERSION = 2\n",
            (release_backups[0] / "runtime" / "features" / "ad_control_v3" / "__init__.py").read_bytes(),
        )
        self.assertEqual(
            b"UNCHANGED = True\n",
            (release_backups[0] / "runtime" / "features" / "ad_control_v3" / "common.py").read_bytes(),
        )
        self.assertEqual(b"VERSION = 3\n", (self.live / "features" / "ad_control_v3" / "__init__.py").read_bytes())
        self.assertEqual(b"V3 = True\n", (self.live / "features" / "ad_control_v3" / "service.py").read_bytes())
        self.assertEqual(b"window.V3 = true;\n", (self.live / "features" / "ad_control_v3" / "assets" / "app.js").read_bytes())
        self.assertEqual(b"#!/usr/bin/env python3\n", (self.live / "scripts" / "ad_control_v3_runner.py").read_bytes())
        self.assertFalse((self.live / "features" / "ad_control_v3" / "ignored.txt").exists())
        self.assertFalse((self.live / "doc.txt").exists())

        repeated = self.apply()
        self.assertEqual("unchanged", repeated["status"])
        self.assertEqual(1, len(list(self.backup.glob("ad-control-v3-*-to-*"))))

    def test_successful_release_can_be_checked_and_rolled_back_exactly(self):
        self.assertEqual("changed", self.apply()["status"])
        checked = deploy_v3.rollback_release(
            self.live, self.repo, self.source_commit, self.target_commit,
            self.backup, check=True,
        )
        self.assertEqual("would_rollback", checked["status"])
        self.assertEqual(TARGET_APP, (self.live / "app.py").read_bytes())

        rolled_back = deploy_v3.rollback_release(
            self.live, self.repo, self.source_commit, self.target_commit,
            self.backup,
        )
        self.assertEqual("rolled_back", rolled_back["status"])
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())
        self.assertEqual(
            b"VERSION = 2\n",
            (self.live / "features" / "ad_control_v3" / "__init__.py").read_bytes(),
        )
        self.assertFalse((self.live / "features" / "ad_control_v3" / "service.py").exists())
        self.assertFalse((self.live / "scripts" / "ad_control_v3_runner.py").exists())
        repeated = deploy_v3.rollback_release(
            self.live, self.repo, self.source_commit, self.target_commit,
            self.backup,
        )
        self.assertEqual("unchanged", repeated["status"])

    def test_successful_release_rollback_refuses_target_drift(self):
        self.apply()
        (self.live / "features" / "ad_control_v3" / "service.py").write_bytes(b"drift\n")
        with self.assertRaisesRegex(RuntimeError, "live runtime source drift"):
            deploy_v3.rollback_release(
                self.live, self.repo, self.source_commit, self.target_commit,
                self.backup,
            )
        self.assertEqual(TARGET_APP, (self.live / "app.py").read_bytes())

    def test_drift_is_rejected_before_backup_or_write(self):
        unknown = b"concurrent = True\n"
        (self.live / "app.py").write_bytes(unknown)
        with self.assertRaisesRegex(RuntimeError, "live app source drift"):
            self.apply()
        self.assertEqual(unknown, (self.live / "app.py").read_bytes())
        self.assertFalse(self.backup.exists())

    def test_apply_requires_explicit_data_disk_backup_directory(self):
        with self.assertRaisesRegex(RuntimeError, "backup_dir on the data disk is required"):
            self.apply(backup=False)
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())

    def test_patch_with_unrelated_app_addition_is_rejected(self):
        unrelated_commit = self._commit_app(
            TARGET_APP + b"UNRELATED_SETTING = True\n", "unrelated target"
        )
        with self.assertRaisesRegex(RuntimeError, "unrelated added line"):
            deploy_v3.verified_overlay(self.repo, self.source_commit, unrelated_commit)

    def test_patch_that_removes_old_app_source_is_rejected(self):
        destructive = TARGET_APP.replace(b"        legacy()\n", b"")
        destructive_commit = self._commit_app(destructive, "destructive target")
        with self.assertRaisesRegex(RuntimeError, "must be additive"):
            deploy_v3.verified_overlay(self.repo, self.source_commit, destructive_commit)

    def test_incomplete_required_runtime_manifest_is_rejected(self):
        self._git("rm", "features/ad_control_v3/routes.py")
        self._git("commit", "-m", "incomplete runtime")
        incomplete_commit = self._git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "reviewed target runtime is incomplete"):
            deploy_v3.verified_runtime_manifest(
                self.repo, self.source_commit, incomplete_commit
            )

    def test_missing_transitive_runtime_dependency_is_rejected(self):
        self._git("rm", "features/ad_control_v3/repository.py")
        self._git("commit", "-m", "missing repository dependency")
        incomplete_commit = self._git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "reviewed target runtime is incomplete"):
            deploy_v3.verified_runtime_manifest(
                self.repo, self.source_commit, incomplete_commit
            )

    def test_invalid_target_python_is_rejected_before_live_write(self):
        (self.repo / "features" / "ad_control_v3" / "repository.py").write_bytes(
            b"def broken(:\n"
        )
        self._git("add", "features/ad_control_v3/repository.py")
        self._git("commit", "-m", "invalid runtime python")
        invalid_commit = self._git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "does not compile"):
            deploy_v3.verified_runtime_manifest(
                self.repo, self.source_commit, invalid_commit
            )

    def test_shared_lock_rejects_competing_deployer(self):
        lock_path = self.live / ".deployment.lock"
        with deploy_v3.exclusive_deploy_lock(lock_path):
            with self.assertRaisesRegex(RuntimeError, "deployment lock busy"):
                self.apply()
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())

    def test_existing_unknown_runtime_file_is_drift_and_blocks_all_writes(self):
        target = self.live / "features" / "ad_control_v3" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"hotfix = 'unknown'\n")
        with self.assertRaisesRegex(RuntimeError, "live runtime source drift"):
            self.apply()
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())
        self.assertEqual(b"hotfix = 'unknown'\n", target.read_bytes())
        self.assertFalse(self.backup.exists())

    def test_install_failure_rolls_back_app_and_deletes_new_runtime_files(self):
        real_atomic_write = deploy_v3.atomic_write

        def fail_after_app_replace(path, value, mode):
            real_atomic_write(path, value, mode)
            if Path(path).name == "app.py":
                raise RuntimeError("injected app install failure")

        with mock.patch.object(
            deploy_v3, "atomic_write", side_effect=fail_after_app_replace
        ):
            with self.assertRaisesRegex(RuntimeError, "injected app install failure"):
                self.apply()
        self.assertEqual(SOURCE_APP, (self.live / "app.py").read_bytes())
        self.assertEqual(b"VERSION = 2\n", (self.live / "features" / "ad_control_v3" / "__init__.py").read_bytes())
        self.assertFalse((self.live / "features" / "ad_control_v3" / "service.py").exists())
        self.assertFalse((self.live / "features" / "ad_control_v3" / "assets" / "app.js").exists())
        self.assertFalse((self.live / "scripts" / "ad_control_v3_runner.py").exists())
        release_backups = list(self.backup.glob("ad-control-v3-*-to-*"))
        self.assertEqual(1, len(release_backups))
        self.assertEqual(SOURCE_APP, (release_backups[0] / "app.py").read_bytes())


if __name__ == "__main__":
    unittest.main()

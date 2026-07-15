import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy import apply_ad_control_account_copy_v2 as deploy_v2


class ExactGitAppMergeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-control-v2-deploy-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.live = self.root / "live"
        self.backup = self.root / "backup"
        self.repo.mkdir()
        self.live.mkdir()
        self._git("init")
        self._git("config", "user.name", "Codex Test")
        self._git("config", "user.email", "codex-test@example.invalid")
        (self.repo / "app.py").write_bytes(b"value = 'source'\n")
        (self.repo / "unrelated.txt").write_bytes(b"source-only\n")
        self._git("add", "app.py", "unrelated.txt")
        self._git("commit", "-m", "source")
        self.source_commit = self._git("rev-parse", "HEAD").strip()
        (self.repo / "app.py").write_bytes(
            b"value = 'target'\n\ndef ad_control_v2():\n    return True\n"
        )
        (self.repo / "unrelated.txt").write_bytes(b"target-only\n")
        self._git("add", "app.py", "unrelated.txt")
        self._git("commit", "-m", "target")
        self.target_commit = self._git("rev-parse", "HEAD").strip()
        self.source_bytes = subprocess.check_output(
            ["git", "-C", str(self.repo), "show", "%s:app.py" % self.source_commit]
        )
        self.target_bytes = subprocess.check_output(
            ["git", "-C", str(self.repo), "show", "%s:app.py" % self.target_commit]
        )
        (self.live / "app.py").write_bytes(self.source_bytes)

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.repo)] + list(arguments),
            text=True,
            stderr=subprocess.STDOUT,
        )

    def apply(self, check=False):
        return deploy_v2.apply_release(
            root=self.live,
            repo=self.repo,
            source_revision=self.source_commit,
            target_revision=self.target_commit,
            backup_dir=self.backup,
            check=check,
        )

    def test_check_is_read_only_then_apply_is_exact_and_idempotent(self):
        checked = self.apply(check=True)
        self.assertEqual("would_change", checked["status"])
        self.assertEqual(self.source_bytes, (self.live / "app.py").read_bytes())
        self.assertFalse(self.backup.exists())

        applied = self.apply()
        self.assertEqual("changed", applied["status"])
        self.assertEqual(self.target_bytes, (self.live / "app.py").read_bytes())
        backups = list(self.backup.glob("app.py.before-ad-control-v2-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(self.source_bytes, backups[0].read_bytes())

        repeated = self.apply()
        self.assertEqual("unchanged", repeated["status"])
        self.assertEqual(1, len(list(self.backup.glob("app.py.before-ad-control-v2-*"))))
        self.assertEqual(self.target_bytes, (self.live / "app.py").read_bytes())

    def test_existing_identical_backup_is_reused_after_interrupted_attempt(self):
        first = self.apply()
        backup_path = Path(first["backup"])
        (self.live / "app.py").write_bytes(self.source_bytes)
        second = self.apply()
        self.assertEqual("changed", second["status"])
        self.assertEqual(str(backup_path), second["backup"])
        self.assertEqual(1, len(list(self.backup.glob("app.py.before-ad-control-v2-*"))))

    def test_unknown_live_source_fails_before_backup_or_write(self):
        unknown = b"value = 'concurrent-change'\n"
        (self.live / "app.py").write_bytes(unknown)
        with self.assertRaisesRegex(RuntimeError, "live app source mismatch"):
            self.apply()
        self.assertEqual(unknown, (self.live / "app.py").read_bytes())
        self.assertFalse(self.backup.exists())

    def test_source_change_after_backup_is_not_overwritten(self):
        unknown = b"value = 'late-concurrent-change'\n"
        real_backup = deploy_v2.atomic_create_backup

        def backup_then_drift(path, value, mode=0o600):
            result = real_backup(path, value, mode=mode)
            (self.live / "app.py").write_bytes(unknown)
            return result

        with mock.patch.object(
            deploy_v2, "atomic_create_backup", side_effect=backup_then_drift
        ):
            with self.assertRaisesRegex(
                RuntimeError, "live app source changed after backup"
            ):
                self.apply()
        self.assertEqual(unknown, (self.live / "app.py").read_bytes())
        backups = list(self.backup.glob("app.py.before-ad-control-v2-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(self.source_bytes, backups[0].read_bytes())

    def test_backup_fsync_failure_leaves_no_checkpoint_or_app_change(self):
        target = self.backup / "durable-backup"
        self.backup.mkdir()
        with mock.patch.object(deploy_v2.os, "fsync", side_effect=OSError("disk")):
            with self.assertRaisesRegex(OSError, "disk"):
                deploy_v2.atomic_create_backup(target, self.source_bytes)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.backup.glob(".*.pending.*")))
        self.assertEqual(self.source_bytes, (self.live / "app.py").read_bytes())

    def test_corrupt_existing_backup_blocks_install(self):
        merge = deploy_v2.verified_merge(
            self.repo, self.source_commit, self.target_commit
        )
        self.backup.mkdir()
        backup_path = self.backup / (
            "app.py.before-ad-control-v2-%s-to-%s"
            % (merge["source_sha256"][:12], merge["target_sha256"][:12])
        )
        backup_path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "existing backup checksum mismatch"):
            self.apply()
        self.assertEqual(self.source_bytes, (self.live / "app.py").read_bytes())
        self.assertEqual(b"corrupt", backup_path.read_bytes())

    def test_existing_backup_is_fsynced_before_reuse(self):
        first = self.apply()
        backup_path = Path(first["backup"])
        (self.live / "app.py").write_bytes(self.source_bytes)
        real_fsync = deploy_v2.os.fsync
        calls = []

        def record_fsync(descriptor):
            calls.append(descriptor)
            return real_fsync(descriptor)

        with mock.patch.object(deploy_v2.os, "fsync", side_effect=record_fsync):
            second = self.apply()
        self.assertEqual("changed", second["status"])
        self.assertEqual(str(backup_path), second["backup"])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.target_bytes, (self.live / "app.py").read_bytes())

    def test_post_install_failure_restores_source_under_same_attempt(self):
        real_atomic_write = deploy_v2.atomic_write
        installed_target = []

        def install_then_fail(path, value, mode):
            real_atomic_write(path, value, mode)
            if value == self.target_bytes and not installed_target:
                installed_target.append(True)
                raise RuntimeError("injected post-install failure")

        with mock.patch.object(
            deploy_v2, "atomic_write", side_effect=install_then_fail
        ):
            with self.assertRaisesRegex(RuntimeError, "post-install failure"):
                self.apply()
        self.assertEqual(self.source_bytes, (self.live / "app.py").read_bytes())
        backups = list(self.backup.glob("app.py.before-ad-control-v2-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(self.source_bytes, backups[0].read_bytes())

    def test_post_install_unknown_concurrent_bytes_are_not_overwritten(self):
        real_atomic_write = deploy_v2.atomic_write
        unknown = b"value = 'uncoordinated-writer'\n"
        installed_target = []

        def install_then_external_change(path, value, mode):
            real_atomic_write(path, value, mode)
            if value == self.target_bytes and not installed_target:
                installed_target.append(True)
                Path(path).write_bytes(unknown)
                raise RuntimeError("injected post-install failure")

        with mock.patch.object(
            deploy_v2, "atomic_write", side_effect=install_then_external_change
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite unknown"):
                self.apply()
        self.assertEqual(unknown, (self.live / "app.py").read_bytes())
        backups = list(self.backup.glob("app.py.before-ad-control-v2-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(self.source_bytes, backups[0].read_bytes())

    def test_temporary_inode_mode_is_set_before_its_fsync(self):
        target = self.live / "mode-order.py"
        events = []

        def record_fchmod(descriptor, mode):
            events.append(("fchmod", descriptor, mode))

        def record_fsync(descriptor):
            events.append(("fsync", descriptor, None))

        with mock.patch.object(
            deploy_v2.os, "fchmod", side_effect=record_fchmod, create=True
        ), mock.patch.object(deploy_v2.os, "fsync", side_effect=record_fsync):
            deploy_v2.atomic_write(target, b"mode-safe\n", 0o640)

        self.assertEqual("fchmod", events[0][0])
        self.assertEqual(0o640, events[0][2])
        self.assertEqual("fsync", events[1][0])
        self.assertEqual(events[0][1], events[1][1])
        self.assertEqual(b"mode-safe\n", target.read_bytes())

    def test_competing_reviewed_deployer_fails_closed_on_shared_lock(self):
        lock_path = self.live / ".deployment.lock"
        with deploy_v2.exclusive_deploy_lock(lock_path):
            with self.assertRaisesRegex(RuntimeError, "deployment lock busy"):
                self.apply()
        self.assertEqual(self.source_bytes, (self.live / "app.py").read_bytes())
        self.assertFalse(self.backup.exists())

    def test_isolated_patch_targets_only_app_blob(self):
        merge = deploy_v2.verified_merge(
            self.repo, self.source_commit, self.target_commit
        )
        self.assertEqual(self.target_bytes, merge["target_bytes"])
        self.assertIn(b"diff --git a/app.py b/app.py", merge["patch_bytes"])
        self.assertNotIn(b"unrelated.txt", merge["patch_bytes"])


if __name__ == "__main__":
    unittest.main()

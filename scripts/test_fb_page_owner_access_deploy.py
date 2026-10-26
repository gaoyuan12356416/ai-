"""Isolated failure tests for the two-process owner-scope deployment."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import deploy_fb_page_owner_access as owner_release

release = owner_release.release


class OwnerAccessDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source, self.target = self.root / "new.py", self.root / "live.py"
        self.source.write_bytes(b"new")
        self.target.write_bytes(b"old")
        self.targets = [(self.source, self.target, 0o644)]
        self.saved = {u: {"active": "active", "enabled": "enabled"}
                      for u in release.RESTORE_UNITS + [release.base.API_UNIT]}
        self.stack.enter_context(patch.object(release.base, "BASE", self.root))
        self.stack.enter_context(patch.object(release.pool_release, "saved_states", return_value=self.saved))
        self.stop = self.stack.enter_context(patch.object(release.pool_release, "quiesce"))
        self.stack.enter_context(patch.object(release, "assert_pool_window"))
        self.stack.enter_context(patch.object(release, "allowed_targets", return_value={str(self.target)}))
        self.snapshot = self.stack.enter_context(patch.object(release.base, "snapshot_db"))
        self.resume = self.stack.enter_context(patch.object(release, "resume"))

    def manifest(self):
        return json.loads(next((self.root / "backups").glob("*/manifest.json")).read_text())

    def test_success_restarts_main_broker_and_preserves_snapshot(self):
        owner_release.apply_release(self.targets, "a" * 40)
        self.assertEqual(self.target.read_bytes(), b"new")
        self.snapshot.assert_called_once()
        self.resume.assert_called_once_with(self.saved, restart_main=True)
        self.assertEqual(self.manifest()["phase"], "deployed")
        self.assertTrue(self.manifest()["main_api_restarted"])
        self.assertFalse(self.manifest()["database_migrated"])

    def test_drain_failure_resumes_without_changing_files_or_restarting_main(self):
        self.stop.side_effect = RuntimeError("busy")
        with self.assertRaisesRegex(RuntimeError, "busy"):
            owner_release.apply_release(self.targets, "b" * 40)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.snapshot.assert_not_called()
        self.resume.assert_called_once_with(self.saved, restart_main=False)

    def test_pre_reopen_copy_failure_restores_files(self):
        original = release.base.atomic_bytes
        failed = []
        def atomic(data, path, mode=0o644):
            if path == self.target and data == b"new" and not failed:
                failed.append(True)
                raise OSError("copy failed")
            original(data, path, mode)
        with patch.object(release.base, "atomic_bytes", side_effect=atomic):
            with self.assertRaisesRegex(OSError, "copy failed"):
                owner_release.apply_release(self.targets, "c" * 40)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(self.manifest()["phase"], "failed_restored")

    def test_possible_new_acceptance_never_triggers_automatic_narrowing(self):
        self.resume.side_effect = RuntimeError("resume failed after reopening")
        with patch.object(owner_release, "_restore_files") as restore:
            with self.assertRaisesRegex(RuntimeError, "resume failed"):
                owner_release.apply_release(self.targets, "d" * 40)
        restore.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertEqual(self.manifest()["phase"], "reopen_failed_new_code_retained")

    def test_rollback_rejects_unfinished_work_and_rechecks_before_restore(self):
        db = self.root / "operational.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript("CREATE TABLE fb_video_task(status TEXT,unknown_outcome INTEGER);"
                               "CREATE TABLE fb_video_pool(status TEXT);")
        with patch.object(release.base, "DB", db):
            owner_release.assert_rollback_clear()
            for statement, cleanup in (
                ("INSERT INTO fb_video_task VALUES('queued',0)", "DELETE FROM fb_video_task"),
                ("INSERT INTO fb_video_task VALUES('failed',1)", "DELETE FROM fb_video_task"),
                ("INSERT INTO fb_video_pool VALUES('enabled')", "DELETE FROM fb_video_pool"),
            ):
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute(statement)
                    conn.commit()
                with patch.object(owner_release, "_rollback") as rollback, \
                        patch.object(owner_release, "_restore_files") as restore:
                    with self.assertRaisesRegex(RuntimeError, "Reconcile"):
                        owner_release.rollback(self.root / "manifest.json")
                    with self.assertRaisesRegex(RuntimeError, "Reconcile"):
                        owner_release.guarded_restore({})
                rollback.assert_not_called()
                restore.assert_not_called()
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute(cleanup)
                    conn.commit()


if __name__ == "__main__":
    unittest.main()

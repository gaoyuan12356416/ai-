"""Offline scoped patch safety checks; no server calls or publish requests."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import deploy_fb_page_list_performance as release


class PerformanceReleaseTests(unittest.TestCase):
    def saved(self):
        return {u: {"active": "active", "enabled": "enabled"}
                for u in release.RESTORE_UNITS + [release.base.API_UNIT]}

    def test_pool_window_covers_midnight_and_recent_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE fb_video_pool(status TEXT,times_json TEXT)")
                conn.execute("INSERT INTO fb_video_pool VALUES('enabled','[\"00:00\"]')")
                conn.commit()
            with patch.object(release.base, "DB", db):
                # Beijing Oct 20 23:59 and Oct 21 00:00:30 both block a restart.
                for utc in ("2026-10-20T15:59:00+00:00", "2026-10-20T16:00:30+00:00"):
                    with self.assertRaisesRegex(RuntimeError, "nearby"):
                        release.assert_pool_window(datetime.fromisoformat(utc))
                self.assertEqual(release.assert_pool_window(datetime(2026, 10, 20, 15, 50, tzinfo=timezone.utc)), 1)

    def test_paused_pool_does_not_block_window(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE fb_video_pool(status TEXT,times_json TEXT)")
                conn.execute("INSERT INTO fb_video_pool VALUES('paused','[\"00:00\"]')")
                conn.commit()
            with patch.object(release.base, "DB", db):
                self.assertEqual(release.assert_pool_window(datetime(2026, 10, 20, 16, tzinfo=timezone.utc)), 0)

    def test_stage_rejects_migration_or_app_change(self):
        for relative in ("app.py", "features/fb_video_publish/core.py", "deploy/gate.conf",
                         "features/fb_auto_posts/service.py", "static/quick-nav.js"):
            with patch.object(release.base, "run", return_value=relative):
                with self.assertRaisesRegex(RuntimeError, "outside performance patch"):
                    release.stage_targets()

    def test_exact_pushed_ref_required(self):
        values = ["a" * 40, "", "git@github.com:gaoyuan12356416/ai-.git",
                  "b" * 40 + "\t" + release.REMOTE_REF]
        with patch.object(release.base, "run", side_effect=values):
            with self.assertRaisesRegex(RuntimeError, "exact pushed"):
                release.verified_commit(release.REMOTE_REF)

    def test_main_restart_only_when_gateway_requires_it(self):
        saved = self.saved()
        with patch.object(release.pool_release, "restore_states") as restore, \
                patch.object(release.base, "health"), \
                patch.object(release.base, "state", return_value=saved[release.base.API_UNIT]), \
                patch.object(release.subprocess, "run") as commands:
            release.resume(saved)
            commands.assert_not_called()
            release.resume(saved, restart_main=True)
            commands.assert_called_once_with(["systemctl", "restart", release.base.API_UNIT], check=True)
        self.assertEqual(restore.call_args.args[1],
                         release.pool_release.TIMERS + [release.pool_release.WORKER, release.pool_release.POOL_UNIT])

    def test_corrupt_backup_rejected_before_stopping_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, backup = root / "service.py", root / "old"
            target.write_bytes(b"new")
            backup.write_bytes(b"corrupt")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"target": str(target), "backup": str(backup),
                                                      "before": hashlib.sha256(b"old").hexdigest(),
                                                      "after": release.base.sha(target)}]}))
            with patch.object(release, "allowed_targets", return_value={str(target)}), \
                    patch.object(release.pool_release, "quiesce") as stop:
                with self.assertRaisesRegex(RuntimeError, "backup drift"):
                    release.rollback(manifest)
            stop.assert_not_called()

    def test_apply_backs_up_database_and_restores_after_copy_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.py", root / "service.py"
            source.write_bytes(b"new")
            target.write_bytes(b"old")
            real_atomic = release.base.atomic_bytes
            failed = []
            def write(data, path, mode=0o644):
                if Path(path) == target and data == b"new" and not failed:
                    failed.append(True)
                    raise OSError("copy interrupted")
                real_atomic(data, path, mode)
            with patch.object(release.base, "BASE", root), \
                    patch.object(release.pool_release, "saved_states", return_value=self.saved()), \
                    patch.object(release.pool_release, "quiesce"), \
                    patch.object(release, "assert_pool_window"), \
                    patch.object(release.base, "snapshot_db") as snapshot, \
                    patch.object(release, "resume") as resumed, \
                    patch.object(release, "allowed_targets", return_value={str(target)}), \
                    patch.object(release.base, "atomic_bytes", side_effect=write):
                with self.assertRaisesRegex(OSError, "copy interrupted"):
                    release.apply_release([(source, target, 0o644)], "a" * 40)
            self.assertEqual(target.read_bytes(), b"old")
            snapshot.assert_called_once()
            resumed.assert_called_once()
            manifest = json.loads(next((root / "backups").glob("*/manifest.json")).read_text())
            self.assertEqual(manifest["phase"], "failed_restored")
            self.assertFalse(manifest["main_api_restarted"])

    def test_drain_failure_resumes_without_writing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "new.py", root / "live.py"
            source.write_bytes(b"new")
            target.write_bytes(b"old")
            with patch.object(release.base, "BASE", root), \
                    patch.object(release.pool_release, "saved_states", return_value=self.saved()), \
                    patch.object(release.pool_release, "quiesce", side_effect=RuntimeError("busy")), \
                    patch.object(release, "assert_pool_window"), patch.object(release, "resume") as resumed, \
                    patch.object(release.base, "snapshot_db") as snapshot:
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    release.apply_release([(source, target, 0o644)], "b" * 40)
            self.assertEqual(target.read_bytes(), b"old")
            snapshot.assert_not_called()
            resumed.assert_called_once()

    def test_partial_rollback_does_not_resume_mixed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text('{"files":[]}')
            with patch.object(release, "validate_restore"), patch.object(release, "assert_pool_window"), \
                    patch.object(release.pool_release, "saved_states", return_value=self.saved()), \
                    patch.object(release.pool_release, "quiesce"), \
                    patch.object(release, "restore_files", side_effect=OSError("disk error")), \
                    patch.object(release, "resume") as resumed:
                with self.assertRaisesRegex(OSError, "disk error"):
                    release.rollback(manifest)
            resumed.assert_not_called()


if __name__ == "__main__":
    unittest.main()

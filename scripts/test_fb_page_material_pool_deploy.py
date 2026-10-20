"""Offline migration, drain and recovery tests; no real service calls."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import deploy_fb_page_material_pool as release


class PoolReleaseTests(unittest.TestCase):
    def test_old_columns_and_manual_facts_preserved_by_additive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "operational.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("""
                    CREATE TABLE historical(id INTEGER PRIMARY KEY, caption TEXT, short_url TEXT);
                    INSERT INTO historical VALUES(17,'冻结文案','https://fixture.invalid/17');
                    CREATE TABLE without_rowid(id TEXT PRIMARY KEY,v TEXT) WITHOUT ROWID;
                    INSERT INTO without_rowid VALUES('x','原值');
                """)
            columns, before = release.fact_snapshot(db)
            report = release.migrate_preserving(db)
            self.assertTrue(report["preserved"])
            self.assertEqual(release.fact_snapshot(db, columns)[1], before)
            with closing(sqlite3.connect(db)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"fb_video_pool", "fb_video_pool_item", "fb_video_pool_slot"} <= tables)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_video_task").fetchone()[0], 0)
                conn.execute("ALTER TABLE historical ADD COLUMN creator TEXT DEFAULT ''")
                conn.commit()
            self.assertEqual(release.fact_snapshot(db, columns)[1], before)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE historical SET caption='changed'")
                conn.commit()
            self.assertNotEqual(release.fact_snapshot(db, columns)[1], before)

    def test_quiesce_does_not_stop_sidecar_until_claims_finish(self):
        saved = {u: {"active": "active", "enabled": "enabled"} for u in release.TIMERS + [release.WORKER, release.POOL_UNIT]}
        with patch.object(release, "in_flight", side_effect=[(["old.service"], 1), ([], 0), ([], 0)]), \
                patch.object(release.time, "sleep"), patch.object(release.subprocess, "run") as command:
            release.quiesce(saved)
        self.assertEqual(command.call_args_list[0].args[0],
                         ["systemctl", "stop", *release.TIMERS, release.WORKER, release.POOL_UNIT])
        self.assertEqual(command.call_args_list[1].args[0], ["systemctl", "stop", release.base.FB_UNIT])

    def test_drain_timeout_leaves_sidecar_running(self):
        saved = {u: {"active": "active"} for u in release.TIMERS + [release.WORKER, release.POOL_UNIT]}
        with patch.object(release, "in_flight", return_value=([], 1)), \
                patch.object(release.time, "monotonic", side_effect=[0, 121]), \
                patch.object(release.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "still processing"):
                release.quiesce(saved)
        self.assertEqual(len(command.call_args_list), 1)

    def test_failure_before_switch_restores_original_producer_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(release.base, "BASE", root), \
                    patch.object(release, "saved_states", return_value={"fixture": "state"}), \
                    patch.object(release, "quiesce", side_effect=RuntimeError("busy")), \
                    patch.object(release, "restore_states") as restore:
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    release.apply_release(root / "stage", [], "fixture")
            self.assertEqual(restore.call_args.args[1],
                             [release.base.FB_UNIT] + release.TIMERS + [release.WORKER, release.POOL_UNIT])

    def test_failure_after_reopen_keeps_compatible_backend_and_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "new.py", root / "live.py"
            source.write_bytes(b"new")
            target.write_bytes(b"old")
            def commands(args, **kwargs):
                if args[:3] == ["systemctl", "enable", "--now"]:
                    raise RuntimeError("scheduler enable failure")
            with patch.object(release.base, "BASE", root), \
                    patch.object(release, "saved_states", return_value={}), \
                    patch.object(release, "quiesce"), patch.object(release.base, "snapshot_db"), \
                    patch.object(release, "migrate_preserving", return_value={"preserved": True}), \
                    patch.object(release.base, "health"), \
                    patch.object(release, "restore_states") as restore, \
                    patch.object(release.subprocess, "run", side_effect=commands):
                with self.assertRaisesRegex(RuntimeError, "scheduler enable"):
                    release.apply_release(root / "stage", [(source, target, 0o644)], "fixture")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertTrue(json.loads((root / "backups/stage/manifest.json").read_text())["failure_after_reopen"])
            self.assertEqual(restore.call_args.args[1], release.TIMERS + [release.WORKER])

    def test_partial_schema_migration_never_restarts_incompatible_old_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(release.base, "BASE", root), \
                    patch.object(release, "saved_states", return_value={}), \
                    patch.object(release, "quiesce"), patch.object(release.base, "snapshot_db"), \
                    patch.object(release, "migrate_preserving", side_effect=RuntimeError("partial migration")), \
                    patch.object(release, "restore_states") as restore:
                with self.assertRaisesRegex(RuntimeError, "partial migration"):
                    release.apply_release(root / "stage", [], "fixture")
            restore.assert_not_called()

    def test_functional_rollback_does_not_restore_backend_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend, old, gate = root / "core.py", root / "backup", root / "pool.conf"
            backend.write_bytes(b"compatible backend")
            old.write_bytes(b"old incompatible backend")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"target": str(backend), "backup": str(old),
                                                      "after": release.base.sha(backend)}]}))
            with patch.object(release, "DROPIN", gate), patch.object(release, "saved_states", return_value={}), \
                    patch.object(release, "quiesce"), patch.object(release.base, "health"), \
                    patch.object(release, "restore_states"), patch.object(release.subprocess, "run") as commands:
                release.rollback(manifest)
            self.assertEqual(backend.read_bytes(), b"compatible backend")
            self.assertIn(b"FB_VIDEO_V2_ENABLED=0", gate.read_bytes())
            self.assertIn(b"FB_VIDEO_POOL_ENABLED=0", gate.read_bytes())
            self.assertTrue(any(c.args[0] == ["systemctl", "disable", release.POOL_UNIT] for c in commands.call_args_list))

    def test_corrupted_rollback_backup_is_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, backup = root / "view.js", root / "backup"
            target.write_bytes(b"new UI")
            backup.write_bytes(b"old UI")
            before = release.base.sha(backup)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"target": str(target), "backup": str(backup),
                                                      "before": before, "after": release.base.sha(target)}]}))
            backup.write_bytes(b"corrupt")
            with patch.object(release.base, "atomic_bytes") as writes, patch.object(release, "quiesce") as stop:
                with self.assertRaisesRegex(RuntimeError, "backup drift"):
                    release.rollback(manifest)
            writes.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(target.read_bytes(), b"new UI")

    def test_partial_backend_copy_prevents_unsafe_functional_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "core.py"
            target.write_bytes(b"old positional inserts")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"target": str(target), "backup": None, "after": "new-hash"}]}))
            with patch.object(release.base, "atomic_bytes") as writes, patch.object(release, "quiesce") as stop:
                with self.assertRaisesRegex(RuntimeError, "Compatible backend incomplete"):
                    release.rollback(manifest)
            writes.assert_not_called()
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()

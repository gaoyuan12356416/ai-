"""Offline main-API-only patch safety; no real service calls."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import deploy_fb_page_list_performance as historical_release
import deploy_fb_pool_load_performance as delivery
import test_fb_page_list_performance_deploy as inherited
from deploy_fb_pool_load_performance import _release as release


class PoolPerformanceReleaseTests(inherited.PerformanceReleaseTests):
    def setUp(self):
        self.scope = patch.object(inherited, "release", release)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_historical_release_configuration_is_unchanged(self):
        self.assertEqual(historical_release.BASELINE, "661be45")
        self.assertEqual(historical_release.REMOTE_REF, "refs/heads/codex/fb-page-list-performance-20261020")
        self.assertIn("features/fb_video_publish/sources.py", historical_release.ALLOWED)
        self.assertIsNot(release, historical_release)
        self.assertEqual(release.BASELINE, "0770d15")
        self.assertEqual(release.REMOTE_REF, "refs/heads/codex/fb-pool-load-performance-20261020")

    def test_only_reviewed_gateway_and_four_statics_are_deployable(self):
        self.assertEqual(release.ALLOWED, {
            "features/fb_video_publish/gateway.py",
            "static/fb-pages.html", "static/fb-video-publish.html",
            "static/fb-video-publish-records.html", "static/fb-video-publish.js",
        })
        for relative in ("features/fb_video_publish/pool.py", "features/fb_video_publish/sources.py",
                         "features/fb_video_publish/service.py"):
            with patch.object(release.base, "run", return_value=relative):
                with self.assertRaisesRegex(RuntimeError, "outside performance patch"):
                    release.stage_targets()


class MainOnlyDeploymentTests(unittest.TestCase):
    @staticmethod
    def states():
        return {unit: {"active": "active", "enabled": "enabled", "main_pid": str(index + 10)}
                for index, unit in enumerate(delivery.LONG_RUNNING + delivery.TIMERS + delivery.ONESHOTS)}

    def fixture(self, root, *, idle=None, write=None):
        stack = ExitStack()
        source, target = root / "source.py", root / "gateway.py"
        source.write_bytes(b"new gateway")
        target.write_bytes(b"old gateway")
        (root / "app.py").write_bytes(b"unchanged main integration")
        stack.enter_context(patch.object(delivery.base, "BASE", root))
        stack.enter_context(patch.object(delivery.base, "MAIN", root))
        stack.enter_context(patch.object(release, "allowed_targets", return_value={str(target)}))
        stack.enter_context(patch.object(delivery, "protected_snapshot", side_effect=lambda: self.states()))
        stack.enter_context(patch.object(delivery, "unit_snapshot", return_value={
            "active": "active", "enabled": "enabled", "main_pid": "100"}))
        stack.enter_context(patch.object(delivery, "manual_idle", side_effect=idle or (lambda: {
            "unfinished_or_leased_manual_tasks": 0, "enabled_pools": 0})))
        stack.enter_context(patch.object(delivery.base, "health"))
        db_backup = stack.enter_context(patch.object(delivery.base, "snapshot_db"))
        # The old runner may have eight active jobs; it is outside this patch's
        # restart scope and must never enter a general publisher drain.
        stack.enter_context(patch.object(delivery.base, "active_work",
                                        side_effect=AssertionError("eight unrelated old jobs must not block")))
        stack.enter_context(patch.object(delivery.pool_release, "quiesce",
                                        side_effect=AssertionError("FB executors must stay running")))
        commands = stack.enter_context(patch.object(delivery.subprocess, "run"))
        if write:
            stack.enter_context(patch.object(delivery.base, "atomic_bytes", side_effect=write))
        return stack, source, target, commands, db_backup

    def test_only_main_restarts_and_old_executors_are_never_drained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack, source, target, commands, backup = self.fixture(root)
            with stack:
                delivery.apply_release([(source, target, 0o644)], "a" * 40)
            commands.assert_called_once_with(["systemctl", "restart", delivery.base.API_UNIT], check=True)
            backup.assert_called_once()
            self.assertEqual(target.read_bytes(), b"new gateway")
            self.assertEqual((root / "app.py").read_bytes(), b"unchanged main integration")
            path = next((root / "backups").glob("pool-load-*/manifest.json"))
            manifest = json.loads(path.read_text())
            self.assertEqual(manifest["phase"], "deployed")
            self.assertEqual(manifest["scope"], delivery.SCOPE)
            self.assertEqual(manifest["protected_before"], manifest["protected_verification"]["after"])

    def test_manual_idle_ignores_old_work_but_blocks_each_broker_dependent_case(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "operational.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("""
                    CREATE TABLE fb_video_task(status TEXT,prepare_lease TEXT,link_lease TEXT,execution_lease TEXT);
                    CREATE TABLE fb_video_pool(status TEXT);
                    CREATE TABLE fb_auto_task(status TEXT);
                    INSERT INTO fb_auto_task VALUES('running'),('running'),('running'),('running'),
                        ('running'),('running'),('running'),('running');
                    INSERT INTO fb_video_task VALUES('published','','','');
                """)
            with patch.object(delivery.base, "DB", db):
                self.assertEqual(delivery.manual_idle()["unfinished_or_leased_manual_tasks"], 0)
                for status, prepare, link, execution in (
                    ("queued", "", "", ""), ("unknown", "", "", ""),
                    ("published", "lease", "", ""), ("failed", "", "lease", ""),
                    ("cancelled", "", "", "lease"),
                ):
                    with closing(sqlite3.connect(db)) as conn:
                        conn.execute("UPDATE fb_video_task SET status=?,prepare_lease=?,link_lease=?,execution_lease=?",
                                     (status, prepare, link, execution))
                        conn.commit()
                    with self.assertRaisesRegex(RuntimeError, "Manual publishing"):
                        delivery.manual_idle()
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute("UPDATE fb_video_task SET status='published',prepare_lease='',link_lease='',execution_lease=''")
                    conn.execute("INSERT INTO fb_video_pool VALUES('enabled')")
                    conn.commit()
                with self.assertRaisesRegex(RuntimeError, "enabled pool"):
                    delivery.manual_idle()

    def test_oneshot_natural_completion_is_recorded_but_long_running_pid_change_blocks(self):
        before = self.states()
        after = self.states()
        unit = delivery.ONESHOTS[0]
        after[unit] = {"active": "inactive", "enabled": "enabled", "main_pid": "0"}
        with patch.object(delivery, "protected_snapshot", return_value=after):
            result = delivery.verify_protected(before)
        self.assertIn(unit, result["natural_oneshot_transitions"])
        after[delivery.base.FB_UNIT]["main_pid"] = "unexpected"
        with patch.object(delivery, "protected_snapshot", return_value=after):
            with self.assertRaisesRegex(RuntimeError, "Publisher process changed"):
                delivery.verify_protected(before)

    def test_timer_state_change_is_not_hidden(self):
        before, after = self.states(), self.states()
        after[delivery.TIMERS[0]]["active"] = "inactive"
        with patch.object(delivery, "protected_snapshot", return_value=after):
            with self.assertRaisesRegex(RuntimeError, "Timer changed"):
                delivery.verify_protected(before)

    def test_new_manual_work_before_restart_restores_files_without_any_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack, source, target, commands, _ = self.fixture(
                root, idle=[{}, {}, RuntimeError("Manual publishing appeared")])
            with stack:
                with self.assertRaisesRegex(RuntimeError, "appeared"):
                    delivery.apply_release([(source, target, 0o644)], "b" * 40)
            commands.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old gateway")
            self.assertEqual(json.loads(next((root / "backups").glob("*/manifest.json")).read_text())["phase"],
                             "failed_restored")

    def test_partial_file_copy_failure_restores_without_stopping_or_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "gateway.py"
            atomic = delivery.base.atomic_bytes
            def write(data, path, mode=0o644):
                if path == target and data == b"new gateway":
                    raise OSError("copy failure")
                atomic(data, path, mode)
            stack, source, target, commands, _ = self.fixture(root, write=write)
            with stack:
                with self.assertRaisesRegex(OSError, "copy failure"):
                    delivery.apply_release([(source, target, 0o644)], "c" * 40)
            commands.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old gateway")

    def test_main_app_drift_prevents_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_bytes(b"current")
            with patch.object(delivery.base, "MAIN", root), patch.object(delivery.subprocess, "run") as commands:
                with self.assertRaisesRegex(RuntimeError, "app.py changed"):
                    delivery.restart_main({"active": "active"}, "wrong hash")
            commands.assert_not_called()

    def test_old_manifest_cannot_use_main_only_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"phase":"failed_restored","files":[]}')
            with patch.object(delivery, "manual_idle") as idle, \
                    patch.object(delivery.subprocess, "run") as commands:
                with self.assertRaisesRegex(RuntimeError, "original deployer"):
                    delivery.rollback(path)
            idle.assert_not_called()
            commands.assert_not_called()

    def test_main_only_rollback_restores_files_without_database_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack, source, target, commands, backup = self.fixture(root)
            with stack:
                delivery.apply_release([(source, target, 0o644)], "d" * 40)
                path = next((root / "backups").glob("*/manifest.json"))
                commands.reset_mock()
                backup.reset_mock()
                delivery.rollback(path)
            commands.assert_called_once_with(["systemctl", "restart", delivery.base.API_UNIT], check=True)
            backup.assert_not_called()
            self.assertEqual(target.read_bytes(), b"old gateway")


if __name__ == "__main__":
    unittest.main()

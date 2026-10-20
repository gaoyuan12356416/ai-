from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_social_facebook as deploy


class ReportOnlyDeployTests(unittest.TestCase):
    def daily_manifest(self, root):
        manifest = {"files": {}, "daily_files": {}, "status": "prepared"}
        for name in ("previous_daily", "release"):
            directory = root / name
            directory.mkdir()
            (directory / "report.py").write_bytes(b"original\n")
            manifest[name] = str(directory)
        manifest["daily_trees"] = {
            name: deploy.controlled_tree(manifest[name])
            for name in ("previous_daily", "release")}
        return manifest

    def test_backup_is_readonly_and_consistent(self):
        with tempfile.TemporaryDirectory() as root:
            source, backup = Path(root) / "source.sqlite3", Path(root) / "backup.sqlite3"
            with closing(sqlite3.connect(source)) as c, c:
                c.executescript("CREATE TABLE publisher(id INTEGER PRIMARY KEY,status TEXT);"
                                "INSERT INTO publisher VALUES(1,'unknown');")
            old = source.read_bytes()
            deploy.snapshot(source, backup)
            self.assertEqual(source.read_bytes(), old)
            with closing(sqlite3.connect(backup)) as c:
                self.assertEqual(c.execute("SELECT status FROM publisher").fetchone()[0], "unknown")

    def test_manifest_rejects_drift_and_corrupt_rollback_backup(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, target, backup = root / "source", root / "live.py", root / "backup"
            source.mkdir()
            (source / "new.py").write_text("new")
            target.write_text("old")
            old_hash = deploy.sha(target)
            record = {"relative": "new.py", "before": old_hash, "after": deploy.sha(source / "new.py")}
            saved = deploy.backup_file(backup, target)
            saved.parent.mkdir(parents=True)
            saved.write_text("old")
            manifest = self.daily_manifest(root)
            manifest.update({"source": str(source), "backup": str(backup),
                             "files": {str(target): record}})
            deploy.verify_files(manifest)
            target.write_text("new")
            deploy.verify_files(manifest, rollback=True)
            target.write_text("unrelated concurrent change")
            with self.assertRaisesRegex(RuntimeError, "destination_changed"):
                deploy.verify_files(manifest, rollback=True)
            target.write_text("new")
            saved.write_text("corrupt")
            with self.assertRaisesRegex(RuntimeError, "backup_changed"):
                deploy.verify_files(manifest, rollback=True)

    def check_tree_drift_under_lock(self, name):
        for action in ("apply", "rollback"):
            for change in ("modify", "add", "delete"):
                with self.subTest(action=action, directory=name, change=change), \
                        tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    manifest = self.daily_manifest(root)
                    directory = Path(manifest[name])

                    @contextmanager
                    def drift_on_lock(_):
                        if change == "modify":
                            (directory / "report.py").write_bytes(b"concurrent edit\n")
                        elif change == "add":
                            (directory / "new.py").write_bytes(b"new module\n")
                        else:
                            (directory / "report.py").unlink()
                        yield

                    current = manifest["previous_daily" if action == "apply" else "release"]
                    with patch.object(deploy, "load", return_value=manifest), \
                            patch.object(deploy, "DAILY_LINK", Path(current)), \
                            patch.object(deploy, "active", return_value="inactive"), \
                            patch.object(deploy, "lock", side_effect=drift_on_lock), \
                            patch.object(deploy, "command") as command, \
                            patch.object(deploy, "switch") as switch, \
                            patch.object(deploy, "atomic_copy") as copy:
                        with self.assertRaisesRegex(RuntimeError, "release_tree_changed:" + name):
                            getattr(deploy, action)("/backup")
                    command.assert_not_called()
                    switch.assert_not_called()
                    copy.assert_not_called()

    def test_previous_release_drift_rejected_inside_apply_and_rollback_lock(self):
        self.check_tree_drift_under_lock("previous_daily")

    def test_new_release_drift_rejected_inside_apply_and_rollback_lock(self):
        self.check_tree_drift_under_lock("release")

    def test_tree_hash_excludes_only_git_and_python_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.daily_manifest(root)
            release = Path(manifest["release"])
            for name in (".git", "__pycache__"):
                directory = release / name
                directory.mkdir()
                (directory / "runtime").write_text("ignored")
            (release / "report.pyc").write_bytes(b"ignored")
            deploy.verify_files(manifest)
            deploy.verify_files(manifest, rollback=True)
            # Raw bytes matter: unlike the legacy per-file hash, CRLF is drift.
            (release / "report.py").write_bytes(b"original\r\n")
            with self.assertRaisesRegex(RuntimeError, "release_tree_changed:release"):
                deploy.verify_files(manifest, rollback=True)

    def test_release_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.daily_manifest(root)
            linked = Path(manifest["release"]) / "linked.py"
            try:
                linked.symlink_to(Path(manifest["previous_daily"]) / "report.py")
            except OSError as error:
                self.skipTest("Host does not permit symlinks: " + str(error))
            with self.assertRaisesRegex(RuntimeError, "release_tree_symlink"):
                deploy.verify_files(manifest)
            with self.assertRaisesRegex(RuntimeError, "release_tree_symlink"):
                deploy.verify_files(manifest, rollback=True)

    def test_manifest_without_both_release_trees_requires_restaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.daily_manifest(Path(temporary))
            del manifest["daily_trees"]["previous_daily"]
            with self.assertRaisesRegex(RuntimeError, "release_tree_manifest_required"):
                deploy.verify_files(manifest, rollback=True)

    def test_in_flight_daily_rejects_before_mutation(self):
        manifest = {"status": "prepared"}
        with patch.object(deploy, "load", return_value=manifest), \
                patch.object(deploy, "verify_files"), patch.object(deploy, "active", return_value="active"), \
                patch.object(deploy, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "daily_in_flight"):
                deploy.apply("/backup")
        command.assert_not_called()

    def test_rollback_never_replaces_any_database_or_publisher_code(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target, saved = root / "report.py", root / "backup/files/report.py"
            target.write_text("new report")
            saved.parent.mkdir(parents=True)
            saved.write_text("old report")
            # Build a POSIX-style target independent of the test host path syntax.
            manifest = {"backup": str(root / "backup"), "files": {"/report.py": {
                "before": "old", "after": "new", "relative": "report.py"}},
                "previous_daily": str(root / "old"), "release": str(root / "new"),
                "daily_timer_before": "active", "status": "deployed"}
            (root / "old").mkdir()
            calls, copies = [], []
            from contextlib import nullcontext
            with patch.object(deploy, "load", return_value=manifest), patch.object(deploy, "verify_files"), \
                    patch.object(deploy, "DAILY_LINK", root / "old"), \
                    patch.object(deploy, "active", return_value="inactive"), \
                    patch.object(deploy, "lock", return_value=nullcontext()), \
                    patch.object(deploy, "command", side_effect=lambda *a, **k: calls.append(a) or ""), \
                    patch.object(deploy, "atomic_copy", side_effect=lambda a, b: copies.append((str(a), str(b)))), \
                    patch.object(deploy, "switch"), patch.object(deploy, "health"), patch.object(deploy, "save"):
                deploy.rollback("/backup")
            self.assertEqual(copies, [(str(saved), str(Path("/report.py")))])
            self.assertEqual(manifest["status"], "rolled_back")
            restarted = [call for call in calls if call[:2] == ("systemctl", "restart")]
            self.assertEqual(restarted, [("systemctl", "restart", "drama-material-api.service")])
            self.assertFalse(any("fb-auto-post-service" in " ".join(call) for call in calls))
            self.assertFalse(any("x-post-automation" in " ".join(call) for call in calls))

    def test_systemd_collects_each_minute_without_delivery(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "deploy/social-facebook-attribution.service").read_text()
        timer = (root / "deploy/social-facebook-attribution.timer").read_text()
        self.assertIn("OnUnitActiveSec=60s", timer)
        self.assertIn("scripts/social_facebook_attribution.py", unit)
        self.assertNotIn("--send", unit)
        self.assertIn("ReadOnlyPaths=/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3", unit)
        self.assertIn("ProtectSystem=strict", unit)


if __name__ == "__main__":
    unittest.main()

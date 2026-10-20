import copy
import io
import json
import sqlite3
import tempfile
import types
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import deploy_social_analytics as deploy


class DeployHelpersTests(unittest.TestCase):
    def test_publisher_facts_ignore_only_authorized_url_and_added_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            backup, live = Path(folder) / "before.sqlite", Path(folder) / "live.sqlite"
            with closing(sqlite3.connect(backup)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,long_url TEXT,status TEXT);
                    INSERT INTO x_post_publish_log VALUES(1,'old','published');
                    CREATE TABLE x_post_dedupe(material_id TEXT PRIMARY KEY,post_id TEXT) WITHOUT ROWID;
                    INSERT INTO x_post_dedupe VALUES('material','confirmed');
                    CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,status TEXT);
                    INSERT INTO x_post_manual_run VALUES(1,'completed');
                """)
            live.write_bytes(backup.read_bytes())
            with closing(sqlite3.connect(live)) as connection, connection:
                connection.executescript("""
                    UPDATE x_post_publish_log SET long_url='new';
                    ALTER TABLE x_post_manual_run ADD COLUMN actor_tenant_key TEXT DEFAULT '';
                    CREATE TABLE x_post_link_attribution(log_id INTEGER,context_json TEXT);
                    INSERT INTO x_post_link_attribution VALUES(1,'frozen');
                """)
            result = deploy.verify_publication_facts(backup, live, "x_post_")
            self.assertEqual(len(result), 3)
            self.assertNotIn("x_post_link_attribution", result)
            self.assertEqual(result["x_post_dedupe"]["rows"], 1)
            with closing(sqlite3.connect(live)) as connection, connection:
                connection.execute("UPDATE x_post_dedupe SET post_id='changed'")
            with self.assertRaisesRegex(RuntimeError, "x_post_dedupe"):
                deploy.verify_publication_facts(backup, live, "x_post_")

    def test_source_copy_makes_code_and_public_assets_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.py"
            source.write_text("print('safe')\n")
            release = root / "release"
            release.mkdir()
            with patch.object(Path, "chmod") as chmod:
                deploy.copy_code(source, release / "features/social/report.py", release)
                self.assertIn(((0o644,), {}), [(call.args, call.kwargs) for call in chmod.call_args_list])
                self.assertIn(((0o755,), {}), [(call.args, call.kwargs) for call in chmod.call_args_list])
            self.assertEqual((release / "features/social/report.py").read_text(), source.read_text())
            with self.assertRaisesRegex(RuntimeError, "outside release"):
                deploy.copy_code(source, root / "outside.py", release)

    def test_read_access_check_uses_systemd_user_and_group(self):
        calls = []
        def command(*args):
            calls.append(args)
            if "-p" in args:
                return "x-post-automation" if "User" in args else "x-group"
            return ""
        with patch.object(deploy, "command", side_effect=command):
            result = deploy.check_readable("x.service", [Path("/release/service.py")])
        self.assertEqual(result, {"user": "x-post-automation", "group": "x-group"})
        self.assertEqual(calls[-1][:6], ("runuser", "--user", "x-post-automation", "--group", "x-group", "--"))

    def test_navigation_preserves_old_enabled_and_admin_guards_and_other_groups(self):
        original = [
            {"key": "youtube", "label": "YouTube", "enabled": False, "adminOnly": True, "items": [
                {"key": "youtubeAnalytics", "label": "Old", "href": "/old", "enabled": False},
                {"key": "keep", "label": "Keep", "href": "/keep", "custom": 123}]},
            {"key": "other", "label": "Other", "custom": [1, 2], "items": []},
        ]
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, "WEB", Path(folder)):
            path = Path(folder) / "navigation.json"
            path.write_text(json.dumps(original))
            deploy.install_navigation()
            current = json.loads(path.read_text())
        self.assertEqual(current[1], original[1])
        self.assertEqual(current[0]["items"], [original[0]["items"][1]])
        social = current[-1]
        self.assertFalse(social["enabled"])
        self.assertTrue(social["adminOnly"])
        self.assertFalse(social["items"][0]["enabled"])
        self.assertEqual(social["module"], "social_analytics")

    def test_unverified_failed_stage_cannot_resume_triggers(self):
        with patch.object(deploy, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "recovery_verification_required"):
                deploy.restore_triggers({"status": "held_for_recovery", "triggers": {}})
        command.assert_not_called()

    def test_rollback_health_accepts_removed_social_route_only_in_legacy_mode(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        def urlopen(url, **kwargs):
            if "/api/social-" in url:
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
            if "/api/youtube-" in url:
                raise urllib.error.HTTPError(url, 401, "auth required", {}, io.BytesIO(b""))
            return Response()
        with patch.object(deploy.urllib.request, "urlopen", side_effect=urlopen), \
             patch.object(deploy, "active", return_value="active"):
            deploy.health(allow_legacy=True)
            with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 100]), \
                 patch.object(deploy.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "health verification failed"):
                    deploy.health()

    def test_staged_release_tampering_stops_before_deployment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "x.py").write_text("original")
            release = root / "release/x"
            release.mkdir(parents=True)
            (release / "x.py").write_text("changed")
            manifest = {"files": {"x.py": deploy.digest(source / "x.py")},
                        "commit": "a" * 40, "report_files": [], "releases": str(root / "release")}
            with patch.object(deploy, "SOURCE", source), patch.object(deploy, "X_FILES", ["x.py"]), \
                 patch.object(deploy, "command", side_effect=["a" * 40, ""]):
                with self.assertRaisesRegex(RuntimeError, "staged release changed"):
                    deploy.assert_staged(manifest)

    def test_sandbox_report_check_never_uses_send_and_requires_complete_quality(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            calls = []
            def command(*args):
                calls.append(args)
                actual_state = Path(args[args.index("--state-dir") + 1])
                expected = actual_state / "previews" / "2026-10-19" / "report.json"
                expected.parent.mkdir(parents=True)
                expected.write_text(json.dumps({"quality": {"complete": False}, "totals": {}}))
                return json.dumps({"status": "ready", "date": "2026-10-19", "path": str(expected.parent)})
            fixed = deploy.datetime.fromisoformat("2026-10-20T03:00:00+00:00")
            clock = type("Clock", (), {"now": staticmethod(lambda zone: fixed)})
            with patch.object(deploy, "DAILY_STATE", state / "live"), patch.object(deploy, "datetime", clock), \
                 patch.object(deploy, "command", side_effect=command):
                with self.assertRaisesRegex(RuntimeError, "report_incomplete"):
                    deploy.verify_report_runtime({"commit": "a" * 40, "backup": str(state)})
            self.assertIn("--preview", calls[0])
            self.assertNotIn("--send", calls[0])
            self.assertTrue(any(arg.startswith("--property=ReadOnlyPaths=") for arg in calls[0]))
            self.assertEqual(Path(calls[0][calls[0].index("--state-dir") + 1]), state / "report-preview")
            self.assertNotEqual(Path(calls[0][calls[0].index("--state-dir") + 1]), state / "live")
            self.assertTrue(any(arg.startswith("--property=ReadWritePaths=" + str(state / "report-preview"))
                                for arg in calls[0]))

    def test_sandbox_already_running_cannot_pass_using_an_old_preview(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(deploy, "command", return_value='{"status":"already_running"}'):
            with self.assertRaisesRegex(RuntimeError, "preview_not_generated"):
                deploy.verify_report_runtime({"commit": "a" * 40, "backup": folder})


class QuiesceTests(unittest.TestCase):
    def exercise(self, failure):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "x.sqlite3"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE x_post_queue(status TEXT)")
            state = {"timer": "active", "worker": "inactive",
                     "drama-material-api.service": "active", "x-post-automation.service": "active",
                     "x-auto-post-service.service": "active"}
            commands, saved, locks = [], [], []
            def command(*args):
                commands.append(args)
                if args[0:2] == ("systemctl", "stop"):
                    for unit in args[2:]:
                        state[unit] = "inactive"
                return ""
            manifest = {"status": "prepared"}
            def handle(path, mode):
                locks.append(path)
                return io.BytesIO()
            fake_lock = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None)
            with patch.object(deploy, "POST_DB", db), patch.object(deploy, "TRIGGERS", ["timer"]), \
                 patch.object(deploy, "WORKERS", ["worker"]), patch.object(deploy, "fcntl", fake_lock), \
                 patch.object(deploy, "active", side_effect=lambda unit: state[unit]), \
                 patch.object(deploy, "command", side_effect=command), patch("builtins.open", side_effect=handle), \
                 patch.object(deploy, "save", side_effect=lambda value: saved.append(copy.deepcopy(value))), \
                 patch.object(deploy, "restore_triggers") as restore:
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "test failure"):
                        with deploy.quiesced(manifest):
                            raise RuntimeError("test failure")
                    restore.assert_not_called()
                else:
                    with deploy.quiesced(manifest):
                        manifest["status"] = "deployed"
                    restore.assert_called_once()
            return manifest, state, locks

    def test_failure_holds_services_and_timers_without_automatic_resume(self):
        manifest, state, locks = self.exercise(True)
        self.assertEqual(manifest["status"], "held_for_recovery")
        self.assertEqual(state["timer"], "inactive")
        self.assertEqual(state["x-post-automation.service"], "inactive")
        self.assertTrue(any(Path(path).name == "run.lock" and
                            Path(path).parent.name == "youtube-publisher-daily-report" for path in locks))
        self.assertIn("x-post-automation.service", manifest["paused_services"])

    def test_success_only_restores_after_caller_marks_verified(self):
        manifest, state, locks = self.exercise(False)
        self.assertEqual(manifest["status"], "deployed")


if __name__ == "__main__":
    unittest.main()

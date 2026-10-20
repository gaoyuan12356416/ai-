"""Exercise rollback decisions on temporary files/SQLite; no service operations."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deploy_fb_video_publish as deployment
from patch_fb_video_publish_integration import patch_app, patch_quick_nav


class DeploymentTests(unittest.TestCase):
    def test_historical_links_keep_public_route_after_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            backup = base / "backups" / "reviewed"
            backup.mkdir(parents=True)
            db = base / "facts.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(
                    "CREATE TABLE fb_video_task(status); INSERT INTO fb_video_task VALUES('published');"
                    "CREATE TABLE fb_video_link(id); INSERT INTO fb_video_link VALUES(1);")
            app = base / "app.py"
            server = base / "short-server.conf"
            snippet = base / "short-snippet.conf"
            old = backup / "old-app"
            old.write_bytes(b"old app")
            app.write_bytes(b"new app")
            server.write_bytes(b"new route")
            snippet.write_bytes(b"new snippet")
            manifest = backup / "manifest.json"
            manifest.write_text(json.dumps({"files": [
                {"target": str(app), "backup": str(old), "after": deployment.sha(app), "mode_before": 0o644},
                {"target": str(server), "backup": None, "after": deployment.sha(server), "mode_before": 0o644},
                {"target": str(snippet), "backup": None, "after": deployment.sha(snippet), "mode_before": 0o644},
            ]}))
            with patch.multiple(deployment, BASE=base, DB=db, SHORT_SERVER=server, SHORT_SNIPPET=snippet), \
                    patch.object(deployment, "state", return_value={"active": "inactive"}), \
                    patch.object(deployment, "unit_present", return_value=True), \
                    patch.object(deployment, "active_work", return_value=([], 0)), \
                    patch.object(deployment, "health", return_value=True), \
                    patch.object(deployment.subprocess, "run") as commands:
                deployment.rollback(manifest)
            self.assertEqual(app.read_bytes(), b"old app")
            self.assertEqual(server.read_bytes(), b"new route")
            self.assertEqual(snippet.read_bytes(), b"new snippet")
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT id FROM fb_video_link").fetchall(), [(1,)])
            self.assertFalse(any(call.args[0] == ["systemctl", "stop"] for call in commands.call_args_list))

    def test_partial_copy_before_migration_restores_only_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            backup = base / "backups" / "partial"
            backup.mkdir(parents=True)
            db = base / "facts.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("CREATE TABLE fb_auto_fixture(id); INSERT INTO fb_auto_fixture VALUES(7);")
            changed, untouched, absent = (base / name for name in ("changed.py", "untouched.py", "new.py"))
            changed.write_bytes(b"new changed")
            untouched.write_bytes(b"old untouched")
            old_changed, old_untouched = backup / "old-changed", backup / "old-untouched"
            old_changed.write_bytes(b"old changed")
            old_untouched.write_bytes(b"old untouched")
            manifest = backup / "manifest.json"
            manifest.write_text(json.dumps({"files": [
                {"target": str(changed), "backup": str(old_changed), "before": deployment.sha(old_changed),
                 "after": deployment.sha(changed), "mode_before": 0o644},
                {"target": str(untouched), "backup": str(old_untouched), "before": deployment.sha(old_untouched),
                 "after": "not-yet-copied", "mode_before": 0o644},
                {"target": str(absent), "backup": None, "before": None,
                 "after": "not-yet-created", "mode_before": 0o644},
            ]}))
            with patch.multiple(deployment, BASE=base, DB=db), \
                    patch.object(deployment, "state", return_value={"active": "inactive"}), \
                    patch.object(deployment, "unit_present", return_value=False), \
                    patch.object(deployment, "active_work", return_value=([], 0)), \
                    patch.object(deployment, "health", return_value=True), \
                    patch.object(deployment.subprocess, "run") as commands, \
                    patch.object(deployment, "atomic_bytes", wraps=deployment.atomic_bytes) as writes:
                deployment.rollback(manifest)
            self.assertEqual(changed.read_bytes(), b"old changed")
            self.assertEqual(untouched.read_bytes(), b"old untouched")
            self.assertFalse(absent.exists())
            self.assertEqual([call.args[1] for call in writes.call_args_list], [changed])
            self.assertFalse(any(call.args[0][1:3] == ["disable", "--now"] for call in commands.call_args_list))
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT id FROM fb_auto_fixture").fetchall(), [(7,)])
                self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name='fb_video_task'").fetchall())

    def test_only_third_hash_state_blocks_restore_and_backup_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, backup = root / "target", root / "backup"
            backup.write_bytes(b"before")
            target.write_bytes(b"after")
            item = {"target": str(target), "backup": str(backup), "before": deployment.sha(backup),
                    "after": deployment.sha(target), "mode_before": 0o644}
            self.assertEqual(deployment.rollback_targets({"files": [item]}), [item])
            target.write_bytes(b"before")
            self.assertEqual(deployment.rollback_targets({"files": [item]}), [])
            target.write_bytes(b"third-party change")
            with self.assertRaisesRegex(RuntimeError, "target drift"):
                deployment.rollback_targets({"files": [item]})
            target.write_bytes(b"after")
            backup.write_bytes(b"corrupted backup")
            with self.assertRaisesRegex(RuntimeError, "backup drift"):
                deployment.rollback_targets({"files": [item]})

    def test_missing_unit_is_tolerated_but_systemctl_errors_are_not_hidden(self):
        for code in (0, 1, 4):
            with patch.object(deployment.subprocess, "run",
                              return_value=SimpleNamespace(returncode=code, stdout="not-found\n")):
                self.assertFalse(deployment.unit_present("fixture.service"))
        with patch.object(deployment.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout="loaded\n")):
            self.assertTrue(deployment.unit_present("fixture.service"))
        with patch.object(deployment.subprocess, "run",
                          return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                deployment.unit_present("fixture.service")

    def test_manual_facts_accepts_half_migrated_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "facts.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("CREATE TABLE fb_video_link(id); INSERT INTO fb_video_link VALUES(1);")
            with patch.object(deployment, "DB", db):
                self.assertEqual(deployment.manual_facts(), (0, True))

    def test_late_batch_keeps_guards_and_gate_is_reversible(self):
        for prior_gate in (None, b"[Service]\nEnvironment=PRIOR_SETTING=1\n"):
            with self.subTest(prior_gate=bool(prior_gate)), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                backup = base / "backups" / "race"
                backup.mkdir(parents=True)
                db = base / "facts.sqlite3"
                with closing(sqlite3.connect(db)) as conn:
                    conn.executescript("CREATE TABLE fb_video_task(status); CREATE TABLE fb_video_link(id);")
                app = base / "main/app.py"
                main, static = base / "main", base / "public"
                gate = base / "systemd/zz-fb-video-closed.conf"
                gate.parent.mkdir()
                if prior_gate is not None:
                    gate.write_bytes(prior_gate)
                manifest = backup / "manifest.json"
                files = []
                for index, (target, old, new) in enumerate((
                    (app, b"old app", b"new guarded app"),
                    (main / "static/navigation.json", b'[]', b'[{"items":[{"module":"fb_video_publish","enabled":true}]}]'),
                    (static / "navigation.json", b'[]', b'[{"items":[{"module":"fb_video_publish","enabled":true}]}]'),
                )):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(new)
                    saved = backup / ("file-" + str(index))
                    saved.write_bytes(old)
                    files.append({"target": str(target), "backup": str(saved), "before": deployment.sha(saved),
                                  "after": deployment.sha(target), "mode_before": 0o644})
                manifest.write_text(json.dumps({"files": files}))
                raced = False

                def command(args, **kwargs):
                    nonlocal raced
                    if args == ["systemctl", "stop", deployment.FB_UNIT] and not raced:
                        raced = True
                        with closing(sqlite3.connect(db)) as conn:
                            conn.execute("INSERT INTO fb_video_task VALUES('queued')")
                            conn.commit()
                    return SimpleNamespace(returncode=0, stdout="")

                with patch.multiple(deployment, BASE=base, DB=db, MAIN=main, STATIC=static, ADMISSION_GATE=gate), \
                        patch.object(deployment, "state", return_value={"active": "inactive"}), \
                        patch.object(deployment, "unit_present", return_value=False), \
                        patch.object(deployment, "active_work", return_value=([], 0)), \
                        patch.object(deployment, "health", return_value=True), \
                        patch.object(deployment.subprocess, "run", side_effect=command) as commands:
                    deployment.rollback(manifest)
                    self.assertEqual(app.read_bytes(), b"new guarded app")
                    self.assertIn(b"FB_VIDEO_PUBLISH_ENABLED=0", gate.read_bytes())
                    saved_manifest = json.loads(manifest.read_text())
                    self.assertTrue(saved_manifest["admission_closed_with_active_tasks"])
                    gate_item = next(item for item in saved_manifest["files"] if item["target"] == str(gate))
                    self.assertEqual(gate_item["after"], deployment.sha(gate))
                    self.assertFalse(json.loads((static / "navigation.json").read_text())[0]["items"][0]["enabled"])
                    self.assertTrue(any(call.args[0] == ["systemctl", "restart", deployment.FB_UNIT]
                                        for call in commands.call_args_list))
                    self.assertFalse(any(call.args[0] == ["systemctl", "restart", deployment.FB_UNIT, deployment.API_UNIT]
                                         for call in commands.call_args_list))
                    with closing(sqlite3.connect(db)) as conn:
                        conn.execute("UPDATE fb_video_task SET status='cancelled'")
                        conn.commit()
                    deployment.rollback(manifest)
                self.assertEqual(app.read_bytes(), b"old app")
                self.assertEqual(json.loads((static / "navigation.json").read_text()), [])
                if prior_gate is None:
                    self.assertFalse(gate.exists())
                else:
                    self.assertEqual(gate.read_bytes(), prior_gate)

    def test_main_patch_does_not_replace_other_features(self):
        sample = '''MODULE_PERMISSIONS = {
    "old_module": "old",
}
DEFAULT_USER_PERMISSIONS = {
    "old_module": False,
}
def load_navigation_config():
    with open(NAVIGATION_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        return filter_navigation(json.load(handle))
class DramaMaterialHandler:
    def do_GET(self):
        parsed = urlparse(self.path)
        allowed = {
            "/fb-post-ad-delete.js",
        }
        unrelated_get()
    def do_POST(self):
        parsed = urlparse(self.path)
        unrelated_post()
'''
        changed = patch_app(sample)
        self.assertIn("unrelated_get()", changed)
        self.assertIn("unrelated_post()", changed)
        self.assertIn('"old_module": False', changed)
        self.assertEqual(changed.count("from features.fb_video_publish.gateway import dispatch"), 2)
        self.assertEqual(patch_app(changed), changed)
        with self.assertRaises(ValueError):
            patch_app(sample.replace("MODULE_PERMISSIONS = {", "MODULE_PERMISSIONS=dict("))


if __name__ == "__main__":
    unittest.main()

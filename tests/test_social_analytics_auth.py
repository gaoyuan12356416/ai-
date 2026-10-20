"""Exercise native app authorization rules without importing app startup code."""
from __future__ import annotations

import ast
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from scripts.social_analytics_permissions import (
    ROOT, apply_manifest, build_manifest, load_policy, write_manifest,
)

APP = ROOT / "app.py"


def original_navigation(**changes):
    item = {
        "key": "youtubeAnalytics", "label": "YouTube 数据报表", "href": "/youtube-analytics.html",
        "module": "youtube_auto_publish", "enabled": True,
    }
    item.update(changes)
    return [{"key": "youtube", "label": "YouTube", "module": "youtube_auto_publish", "items": [item]}]


def social_navigation():
    return [{"key": "social", "label": "社媒数据报表", "module": "social_analytics",
             "items": [{"key": "socialAnalytics", "label": "发布效果分析",
                        "href": "/social-analytics.html", "module": "social_analytics", "enabled": True}]}]


def native_definitions(names, namespace):
    tree = ast.parse(APP.read_text(encoding="utf-8-sig"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.ClassDef):
            selected.extend(child for child in node.body
                            if isinstance(child, ast.FunctionDef) and child.name in names)
    if {node.name for node in selected} != set(names):
        raise AssertionError("native authorization definitions missing")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(APP), "exec"), namespace)


class PermissionsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.db = self.directory / "main.sqlite"
        self.nav = self.directory / "navigation-before.json"
        self.nav.write_text(json.dumps(original_navigation()), encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE drama_admin_user(user_id TEXT PRIMARY KEY,tenant_key TEXT,"
                               "name TEXT,role TEXT,permissions_json TEXT,updated_at TEXT)")
            records = [
                ("eligible", "t", "Eligible", "user", {"youtube_auto_publish": True, "x_accounts": False}),
                ("x-only", "t", "X Only", "user", {"x_accounts": True}),
                ("none", "t", "None", "user", {}),
                ("disabled", "t", "Disabled", "user", {"youtube_auto_publish": False}),
                ("explicit-false", "t", "False", "user", {"youtube_auto_publish": True, "social_analytics": False}),
                ("explicit-true", "t", "True", "user", {"social_analytics": True}),
                ("admin", "t", "Admin", "admin", {}),
                ("no-tenant", "", "Missing", "user", {"youtube_auto_publish": True}),
            ]
            connection.executemany(
                "INSERT INTO drama_admin_user VALUES(?,?,?,?,?,?)",
                [(user, tenant, name, role, json.dumps(perms), "2026-01-01 00:00:00")
                 for user, tenant, name, role, perms in records],
            )

    def tearDown(self):
        self.temp.cleanup()

    def read_all(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT * FROM drama_admin_user ORDER BY user_id").fetchall()

    def test_only_former_report_users_receive_new_independent_permission(self):
        before = self.read_all()
        manifest = build_manifest(self.db, self.nav)
        self.assertEqual([change["user_id"] for change in manifest["changes"]], ["eligible"])
        self.assertEqual(self.read_all(), before)
        result = apply_manifest(self.db, manifest)
        self.assertEqual(result["changed_users"], ["eligible"])
        users = {row[0]: json.loads(row[4]) for row in self.read_all()}
        self.assertEqual(users["eligible"], {
            "youtube_auto_publish": True, "x_accounts": False, "social_analytics": True})
        self.assertNotIn("social_analytics", users["x-only"])
        self.assertFalse(users["explicit-false"]["social_analytics"])
        self.assertNotIn("social_analytics", users["admin"])
        self.assertNotIn("social_analytics", users["no-tenant"])

    def test_apply_and_rollback_are_idempotent_and_restore_exact_original_values(self):
        before = self.read_all()
        manifest = build_manifest(self.db, self.nav)
        apply_manifest(self.db, manifest)
        repeat = apply_manifest(self.db, manifest)
        self.assertEqual(repeat["changed_users"], [])
        self.assertEqual(repeat["already_at_target"], ["eligible"])
        apply_manifest(self.db, manifest, rollback=True)
        self.assertEqual(self.read_all(), before)
        self.assertEqual(apply_manifest(self.db, manifest, rollback=True)["changed_users"], [])

    def test_disabled_old_navigation_cannot_grant_permission(self):
        self.nav.write_text(json.dumps(original_navigation(enabled=False)), encoding="utf-8")
        self.assertEqual(build_manifest(self.db, self.nav)["changes"], [])

    def test_admin_only_old_navigation_cannot_grant_regular_users(self):
        self.nav.write_text(json.dumps(original_navigation(adminOnly=True)), encoding="utf-8")
        self.assertEqual(build_manifest(self.db, self.nav)["changes"], [])

    def test_group_permission_is_enforced_using_native_app_policy(self):
        navigation = original_navigation()
        navigation[0]["module"] = "cover_synthesis"
        self.nav.write_text(json.dumps(navigation), encoding="utf-8")
        self.assertEqual(build_manifest(self.db, self.nav)["changes"], [])

    def test_no_old_report_entry_stops_instead_of_guessing(self):
        self.nav.write_text(json.dumps(social_navigation()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "navigation_missing"):
            build_manifest(self.db, self.nav)

    def test_permission_edit_after_freeze_blocks_apply_without_overwrite(self):
        manifest = build_manifest(self.db, self.nav)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE drama_admin_user SET permissions_json=? WHERE user_id='eligible'",
                               (json.dumps({"youtube_auto_publish": False}),))
        current = self.read_all()
        with self.assertRaisesRegex(ValueError, "row_drift"):
            apply_manifest(self.db, manifest)
        self.assertEqual(self.read_all(), current)

    def test_rollback_refuses_to_erase_new_admin_permission_changes(self):
        manifest = build_manifest(self.db, self.nav)
        apply_manifest(self.db, manifest)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE drama_admin_user SET permissions_json=? WHERE user_id='eligible'",
                               (json.dumps({"youtube_auto_publish": True, "social_analytics": True,
                                            "cover_synthesis": True}),))
        current = self.read_all()
        with self.assertRaisesRegex(ValueError, "row_drift"):
            apply_manifest(self.db, manifest, rollback=True)
        self.assertEqual(self.read_all(), current)

    def test_manifest_and_original_navigation_must_remain_frozen(self):
        manifest = build_manifest(self.db, self.nav)
        changed = copy.deepcopy(manifest)
        changed["changes"][0]["after_permissions_json"] = '{"social_analytics":true,"settings":true}'
        with self.assertRaisesRegex(ValueError, "manifest_invalid"):
            apply_manifest(self.db, changed)
        self.nav.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "navigation_changed"):
            apply_manifest(self.db, manifest)

    def test_backup_file_cannot_be_overwritten(self):
        manifest = build_manifest(self.db, self.nav)
        path = self.directory / "permission-manifest.json"
        write_manifest(path, manifest)
        saved = path.read_bytes()
        with self.assertRaises(FileExistsError):
            write_manifest(path, manifest)
        self.assertEqual(path.read_bytes(), saved)


class NativeActorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, _ = load_policy(APP)
        native_definitions({"_social_analytics_actor", "load_session"}, cls.base)

    def setUp(self):
        self.namespace = dict(self.base)
        # Recompile so each native function uses this test's isolated globals.
        native_definitions({"_social_analytics_actor", "load_session"}, self.namespace)
        self.responses = []
        self.session = {
            "user_id": "alice", "tenant_key": "tenant", "name": "Alice", "role": "user",
            "permissions": {"social_analytics": True},
        }
        self.navigation = social_navigation()
        self.namespace.update(
            SESSION_COOKIE_NAME="session",
            load_session=lambda token: self.session if token == "cookie-token" else None,
            load_navigation_config=lambda: self.navigation,
            json_response=lambda handler, status, body, **kwargs: self.responses.append((status, body)),
        )
        self.handler = type("Handler", (), {"_cookies": lambda self: {"session": "cookie-token"}})()

    def actor(self):
        return self.namespace["_social_analytics_actor"](self.handler)

    def test_regular_user_keeps_tenant_owner_scope_and_no_system_capability(self):
        actor = self.actor()
        self.assertEqual(actor["tenant_key"], "tenant")
        self.assertEqual(actor["user_id"], "alice")
        self.assertEqual(actor["role"], "user")
        self.assertNotIn("_system_report", actor)

    def test_youtube_or_x_permission_alone_cannot_enter_social_report(self):
        for permissions in ({"youtube_auto_publish": True}, {"x_accounts": True}):
            self.session["permissions"] = permissions
            self.assertIsNone(self.actor())
            self.assertEqual(self.responses[-1], (403, {
                "error": "permission_denied", "module": "social_analytics"}))

    def test_cookie_token_and_complete_identity_are_required(self):
        for changes in ({"auth_type": "api_token"}, {"tenant_key": ""}, {"user_id": ""}):
            original = dict(self.session)
            self.session.update(changes)
            self.assertIsNone(self.actor())
            self.assertEqual(self.responses[-1][0], 401)
            self.session = original
        self.handler = type("Handler", (), {"_cookies": lambda self: {}})()
        self.assertIsNone(self.actor())
        self.assertEqual(self.responses[-1][0], 401)

    def test_admin_retains_tenant_and_respects_disabled_navigation(self):
        self.session.update(role="admin", permissions={})
        actor = self.actor()
        self.assertEqual(actor["role"], "admin")
        self.assertEqual(actor["tenant_key"], "tenant")
        self.navigation[0]["enabled"] = False
        self.assertIsNone(self.actor())
        self.assertEqual(self.responses[-1][0], 403)

    def test_navigation_failure_is_not_treated_as_authorization(self):
        def unavailable():
            raise OSError()
        self.namespace["load_navigation_config"] = unavailable
        self.assertIsNone(self.actor())
        self.assertEqual(self.responses[-1], (503, {"error": "navigation_config_unavailable"}))

    def test_permission_and_role_changes_are_read_from_current_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.sqlite"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE drama_admin_user(user_id TEXT,role TEXT,permissions_json TEXT,email TEXT);
                    CREATE TABLE drama_admin_session(session_token TEXT,user_id TEXT,union_id TEXT,
                        open_id TEXT,name TEXT,en_name TEXT,avatar_url TEXT,tenant_key TEXT,source TEXT,expires_at INTEGER);
                    INSERT INTO drama_admin_user VALUES('alice','user','{"social_analytics":true}','test@example.invalid');
                    INSERT INTO drama_admin_session VALUES('cookie-token','alice','','','Alice','','','tenant','feishu',9999999999);
                """)
            native_definitions({"load_session"}, self.namespace)
            self.namespace.update(
                JOB_DB_LOCK=threading.Lock(), get_job_db_connection=lambda: sqlite3.connect(path),
                cleanup_expired_sessions=lambda: None, default_role_for_user=lambda user: "user",
            )
            self.assertEqual(self.actor()["role"], "user")
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE drama_admin_user SET permissions_json='{}'")
            self.assertIsNone(self.actor())
            self.assertEqual(self.responses[-1][0], 403)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE drama_admin_user SET role='admin'")
            self.assertEqual(self.actor()["role"], "admin")
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DELETE FROM drama_admin_user")
            self.assertIsNone(self.actor())


if __name__ == "__main__":
    unittest.main()

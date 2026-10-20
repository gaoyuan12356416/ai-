"""Authentication/framing tests; no production adapters or network."""
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish import gateway
from features.fb_video_publish.client import GatewayError, route
from features.fb_video_publish.navigation import ensure_navigation


class Handler:
    def __init__(self, body=None, method="POST"):
        raw = json.dumps(body or {}).encode()
        self.command = method
        self.headers = Message()
        self.headers["Content-Length"] = str(len(raw))
        self.headers["Content-Type"] = "application/json"
        self.headers["Origin"] = "https://ai.example.test"
        self.rfile = io.BytesIO(raw)
        self.close_connection = False
        self.responses = []

    def _cookies(self):
        return {"session": "dummy"}

    def _require_same_origin_json(self):
        return (self.headers["Origin"] == "https://ai.example.test"
                and self.headers["Content-Type"] == "application/json")


class GatewayTests(unittest.TestCase):
    def globals(self, session):
        return {
            "SESSION_COOKIE_NAME": "session", "load_session": lambda _: session,
            "has_module_permission": lambda s, key: s.get("allowed") is True,
            "load_navigation_config": lambda: [],
            "navigation_item_access": lambda *args: {"allowed": True},
            "json_response": lambda h, status, body, **kwargs: h.responses.append((status, body)),
        }

    def dispatch(self, handler, session, path="/draft"):
        parsed = gateway.urlsplit("/api/fb-video-publish" + path)
        with patch.object(gateway, "request", return_value={"ok": True}) as remote:
            gateway.dispatch(handler, parsed, self.globals(session))
        return remote

    def test_cookie_and_tenant_required(self):
        for session in (None, {}, {"auth_type": "api_token", "tenant_key": "t", "user_id": "u"},
                        {"tenant_key": "t", "user_id": ""}):
            handler = Handler()
            remote = self.dispatch(handler, session)
            self.assertEqual(handler.responses[0][0], 401)
            remote.assert_not_called()

    def test_permission_is_default_deny(self):
        handler = Handler()
        self.dispatch(handler, {"tenant_key": "t", "user_id": "u"})
        self.assertEqual(handler.responses[0][0], 403)

    def test_payload_cannot_override_identity(self):
        handler = Handler({"tenant_key": "evil", "_actor": {"is_admin": True}, "sub_user_id": "999", "draft": {}})
        remote = self.dispatch(handler, {"tenant_key": "tenant", "user_id": "u", "allowed": True})
        kwargs = remote.call_args.kwargs
        self.assertEqual(kwargs["actor"]["tenant_key"], "tenant")
        self.assertFalse(kwargs["actor"]["is_admin"])
        self.assertNotIn("sub_user_id", kwargs["payload"])
        self.assertNotIn("_actor", kwargs["payload"])

    def test_records_cancel_survive_loss_of_business_mapping(self):
        self.assertFalse(route("GET", "/api/fb-video-publish/batches")[1])
        self.assertFalse(route("POST", "/api/fb-video-publish/tasks/" + "a" * 32 + "/cancel")[1])
        self.assertTrue(route("POST", "/api/fb-video-publish/batches")[1])

    def test_ambiguous_or_excessive_body_rejected_before_read(self):
        for edit, status in (
            (lambda h: h.headers.add_header("Content-Length", "2"), 411),
            (lambda h: h.headers.add_header("Transfer-Encoding", "chunked"), 411),
            (lambda h: h.headers.replace_header("Content-Length", str(4 * 1024 * 1024 + 1)), 413),
        ):
            handler = Handler()
            edit(handler)
            with self.assertRaises(GatewayError) as ctx:
                gateway.read_json(handler)
            self.assertEqual(ctx.exception.status, status)
            self.assertEqual(handler.rfile.tell(), 0)

    def test_origin_required(self):
        handler = Handler()
        del handler.headers["Origin"]
        with self.assertRaises(GatewayError) as ctx:
            gateway.read_json(handler)
        self.assertEqual(ctx.exception.code, "same_origin_required")
        self.assertEqual(handler.rfile.tell(), 0)

    def test_cross_origin_and_navigation_disabled(self):
        handler = Handler()
        handler.headers["Sec-Fetch-Site"] = "cross-site"
        with self.assertRaises(GatewayError):
            gateway.read_json(handler)
        handler = Handler()
        app = self.globals({"tenant_key": "t", "user_id": "u", "allowed": True})
        app["navigation_item_access"] = lambda *args: {"allowed": False}
        with patch.object(gateway, "request") as remote:
            gateway.dispatch(handler, gateway.urlsplit("/api/fb-video-publish/draft"), app)
        self.assertEqual(handler.responses[0][0], 403)
        remote.assert_not_called()

    def test_mapping_is_active_unique_and_requires_admin_too(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "users.sqlite"
            with sqlite3.connect(database) as conn:
                conn.execute("CREATE TABLE drama_admin_user(tenant_key,user_id,email)")
                conn.execute("INSERT INTO drama_admin_user VALUES('t','u',' Mail@Example.test ')")
            conn.close()
            app = {
                "JOB_DB_PATH": database, "ADMIN_MAPPING_MYSQL_DATABASE": "ads_ai",
                "ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53", "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "test", "ADMIN_MAPPING_MYSQL_USER": "test",
            }
            actor = {"tenant_key": "t", "user_id": "u", "is_admin": True}
            result = type("Result", (), {"stdout": "1\n42\n"})()
            with patch.object(gateway.subprocess, "run", return_value=result) as run:
                self.assertEqual(gateway.attribution_identity(app, actor), "42")
                sql = run.call_args.args[0][-1]
                self.assertIn("status=0", sql)
                self.assertIn("START TRANSACTION READ ONLY", sql)
                self.assertIn("BINARY LOWER(TRIM(email))", sql)
            result.stdout = "1\n42\n43\n"
            with patch.object(gateway.subprocess, "run", return_value=result):
                with self.assertRaises(GatewayError) as ctx:
                    gateway.attribution_identity(app, actor)
                self.assertEqual(ctx.exception.code, "attribution_mapping_invalid")

    def test_admin_page_reads_do_not_require_unused_attribution(self):
        session = {"tenant_key": "t", "user_id": "u", "role": "admin", "allowed": True}
        for path in ("/pages", "/pages/available"):
            handler = Handler(method="GET")
            with patch.object(gateway, "attribution_identity", side_effect=AssertionError("unused mapping")), \
                 patch("features.fb_video_publish.identity_broker.enrich_creator_names",
                       side_effect=lambda app, tenant, result: result):
                remote = self.dispatch(handler, session, path)
            self.assertEqual(handler.responses[0][0], 200)
            actor = remote.call_args.kwargs["actor"]
            self.assertEqual(actor["tenant_key"], "t")
            self.assertTrue(actor["is_admin"])
            self.assertEqual(actor["business_owner_id"], "")
            self.assertEqual(actor["sub_user_id"], "")

    def test_owner_reads_and_admin_writes_still_require_current_mapping(self):
        for role, method, path in (
            ("user", "GET", "/pages"), ("user", "GET", "/pages/available"),
            ("admin", "GET", "/pages/12345/pool"),
            ("admin", "POST", "/pages"), ("admin", "POST", "/preflight"),
            ("admin", "POST", "/batches"),
        ):
            session = {"tenant_key": "t", "user_id": "u", "role": role, "allowed": True}
            handler = Handler(method=method)
            with patch.object(gateway, "attribution_identity",
                              side_effect=GatewayError("attribution_mapping_invalid", "missing mapping", 409)) as mapping:
                remote = self.dispatch(handler, session, path)
            mapping.assert_called_once()
            remote.assert_not_called()
            self.assertEqual(handler.responses[0][0], 409)

    def test_navigation_preserves_disables_and_old_child_permission(self):
        original = [{"key": "facebook_platform", "module": "fb_page_posts", "enabled": False, "items": [
            {"key": "old", "module": "fb_page_posts"},
            {"key": "fbVideoPublish", "module": "fb_video_publish", "enabled": False}]}]
        result = ensure_navigation(original)
        self.assertNotIn("module", result[0])
        self.assertFalse(result[0]["enabled"])
        self.assertEqual(result[0]["items"][0]["module"], "fb_page_posts")
        self.assertFalse(result[0]["items"][1]["enabled"])
        self.assertEqual(len(result[0]["items"]), 4)
        self.assertEqual(ensure_navigation(result), result)
        self.assertIn("module", original[0])


if __name__ == "__main__":
    unittest.main()

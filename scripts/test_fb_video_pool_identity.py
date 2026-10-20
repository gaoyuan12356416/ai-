"""Private identity boundary, public pool scope and scheduler integration tests."""
from __future__ import annotations

import ast
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish import gateway, identity_broker, identity_client
from features.fb_video_publish.client import GatewayError, route
from features.fb_auto_posts.service import Handler as SidecarHandler, ServiceError, build_runtime
from patch_fb_page_material_pool_integration import patch_app, MARKER
import fb_video_pool_scheduler as scheduler_client

TOKEN = "a" * 48


class Handler:
    def __init__(self, payload=None):
        raw = json.dumps(payload if payload is not None else {"tenant_key": "t", "user_id": "u"}).encode()
        self.command = "POST"
        self.client_address = ("127.0.0.1", 1234)
        self.headers = Message()
        self.headers["Authorization"] = "Bearer " + TOKEN
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(raw))
        self.rfile = io.BytesIO(raw)
        self.responses = []
        self.close_connection = False


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "users.sqlite"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE drama_admin_user(tenant_key,user_id,name,role,status,permissions_json,email)")
            conn.execute("INSERT INTO drama_admin_user VALUES(?,?,?,?,?,?,?)",
                         ("t", "u", "Original", "user", "active", '{"fb_video_publish":true}', "u@example.test"))
            conn.execute("INSERT INTO drama_admin_user VALUES(?,?,?,?,?,?,?)",
                         ("other", "otheruser", "Other tenant", "admin", "active", "{}", "other@example.test"))
            conn.commit()
        self.app = {
            "JOB_DB_PATH": self.db,
            "normalize_user_permissions": lambda value, role: {"fb_video_publish": True} if role == "admin" else json.loads(value),
            "has_module_permission": lambda actor, key: actor.get("permissions", {}).get(key) is True,
            "load_navigation_config": lambda: [],
            "navigation_item_access": lambda actor, key, config: {"allowed": key == "fbPageManagement"},
            "json_response": lambda handler, status, payload, **kwargs: handler.responses.append((status, payload)),
        }
        self.mapping = patch.object(gateway, "attribution_identity", return_value="42")
        self.mapping_mock = self.mapping.start()

    def tearDown(self):
        self.mapping.stop()
        self.temp.cleanup()

    def update(self, **fields):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE drama_admin_user SET " + ",".join(key + "=?" for key in fields)
                         + " WHERE tenant_key='t' AND user_id='u'", list(fields.values()))
            conn.commit()

    def resolve(self):
        return identity_broker.resolve_identity(self.app, "t", "u")

    def test_current_active_identity_is_small_and_trusted(self):
        actor = self.resolve()
        self.assertEqual(set(actor), identity_broker.ACTOR_KEYS)
        self.assertEqual(actor["user_name"], "Original")
        self.assertEqual(actor["sub_user_id"], "42")
        self.assertFalse(actor["is_admin"])
        self.update(name="Current", role="admin")
        self.assertEqual(self.resolve()["user_name"], "Current")
        self.assertTrue(self.resolve()["is_admin"])

    def test_account_disable_delete_or_tenant_move_rechecked(self):
        for fields in ({"status": "disabled"}, {"tenant_key": "other"}, {"role": "system"}):
            with self.subTest(fields=fields):
                self.update(**fields)
                with self.assertRaises(GatewayError) as ctx:
                    self.resolve()
                self.assertEqual(ctx.exception.code, "identity_inactive")
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("UPDATE drama_admin_user SET tenant_key='t',status='active',role='user' WHERE user_id='u'")
                    conn.commit()
        self.mapping_mock.assert_not_called()

    def test_permission_and_navigation_loss(self):
        self.update(permissions_json='{"fb_video_publish":false}')
        with self.assertRaises(GatewayError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "identity_permission_denied")
        self.update(permissions_json='{"fb_video_publish":true}')
        self.app["navigation_item_access"] = lambda *args: {"allowed": False}
        with self.assertRaises(GatewayError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.status, 403)
        self.mapping_mock.assert_not_called()

    def test_mapping_invalid_is_permanent_and_unavailable_transient(self):
        for status, expected in ((409, "identity_attribution_invalid"), (503, "mapping_unavailable")):
            self.mapping_mock.side_effect = GatewayError("mapping_unavailable", "safe", status)
            with self.assertRaises(GatewayError) as ctx:
                self.resolve()
            self.assertEqual(ctx.exception.code, expected)
            self.assertEqual(ctx.exception.status, 403 if status == 409 else 503)

    def test_identifier_injection_cannot_escape_tenant_query(self):
        for tenant, user in (("t' OR 1=1 --", "u"), ("t", "u' OR 1=1 --"), ("other", "u")):
            with self.assertRaises(GatewayError):
                identity_broker.resolve_identity(self.app, tenant, user)
        self.mapping_mock.assert_not_called()

    def dispatch(self, handler):
        with patch.dict(identity_broker.os.environ, {"FB_AUTO_POST_INTERNAL_TOKEN": TOKEN}):
            identity_broker.dispatch(handler, self.app)
        return handler.responses[0]

    def test_private_broker_accepts_only_direct_loopback_and_unique_bearer(self):
        self.assertEqual(self.dispatch(Handler())[0], 200)
        for key, value in (("Forwarded", ""), ("X-Forwarded-For", "127.0.0.1"),
                           ("X-Forwarded-Host", ""), ("X-Real-IP", "")):
            handler = Handler()
            handler.headers[key] = value
            self.assertEqual(self.dispatch(handler)[0], 403)
            self.assertEqual(handler.rfile.tell(), 0)
        for remote in ("192.0.2.1", "::ffff:127.0.0.1"):
            handler = Handler()
            handler.client_address = (remote, 1)
            self.assertEqual(self.dispatch(handler)[0], 403)
        handler = Handler()
        handler.headers["Authorization"] = "Bearer " + TOKEN
        self.assertEqual(self.dispatch(handler)[0], 403)
        handler = Handler()
        handler.headers.replace_header("Authorization", "Bearer bad")
        self.assertEqual(self.dispatch(handler)[0], 403)

    def test_broker_rejects_spoofed_actor_fields_and_ambiguous_framing(self):
        for payload in ({"tenant_key": "t", "user_id": "u", "is_admin": True},
                        {"tenant_key": "t", "user_id": "u", "actor": {}},
                        {"tenant_key": [], "user_id": "u"}, [], {}):
            self.assertEqual(self.dispatch(Handler(payload))[0], 400)
        for key, value in (("Content-Length", "2"), ("Transfer-Encoding", "chunked")):
            handler = Handler()
            handler.headers[key] = value
            self.assertEqual(self.dispatch(handler)[0], 400)
            self.assertEqual(handler.rfile.tell(), 0)
        self.mapping_mock.assert_not_called()

    def test_historical_names_batch_same_tenant_snapshot_and_id_fallback(self):
        items = [
            {"created_by": "u", "created_by_name": "Frozen", "updated_by": "otheruser"},
            {"created_by": "u", "created_by_name": ""},
            {"created_by": "otheruser", "created_by_name": ""},
            {"created_by": "deleted", "created_by_name": "", "updated_by": "u"},
        ]
        result = identity_broker.enrich_creator_names(self.app, "t", {"ok": True, "items": items})
        self.assertEqual([item["created_by_name"] for item in result["items"]],
                         ["Frozen", "Original", "otheruser", "deleted"])
        self.assertEqual(items[1]["created_by_name"], "")


class ClientTests(unittest.TestCase):
    def remote(self, data, status=200):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status_code = status
        response.iter_content.return_value = [json.dumps(data).encode()]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.return_value = response
        return session

    def actor(self):
        return {"tenant_key": "t", "user_id": "u", "user_name": "Name",
                "is_admin": False, "sub_user_id": "42", "business_owner_id": "42"}

    def test_resolver_is_fixed_loopback_no_redirect_proxy_and_current_every_time(self):
        session = self.remote({"ok": True, "actor": self.actor()})
        with patch.object(identity_client.requests, "Session", return_value=session):
            client = identity_client.IdentityClient({"FB_AUTO_POST_INTERNAL_TOKEN": TOKEN})
            self.assertEqual(client.resolve("t", "u"), self.actor())
            client.resolve("t", "u")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args.args[0], "http://127.0.0.1:8787/internal/fb-video-publish/identity")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_resolver_rejects_wrong_tenant_identity_extra_fields_or_mapping(self):
        for delta in ({"tenant_key": "other"}, {"user_id": "other"}, {"is_admin": "true"},
                      {"role": "admin"}, {"sub_user_id": "0"}, {"business_owner_id": "43"}):
            actor = {**self.actor(), **delta}
            with patch.object(identity_client.requests, "Session", return_value=self.remote({"ok": True, "actor": actor})):
                with self.assertRaises(GatewayError) as ctx:
                    identity_client.IdentityClient({"FB_AUTO_POST_INTERNAL_TOKEN": TOKEN}).resolve("t", "u")
                self.assertEqual(ctx.exception.code, "identity_unavailable")

    def test_resolver_preserves_only_explicit_permanent_identity_errors(self):
        for code, status in (("identity_inactive", 403), ("identity_permission_denied", 403),
                             ("identity_attribution_invalid", 403), ("forbidden", 403), ("secret-server-error", 500)):
            with patch.object(identity_client.requests, "Session", return_value=self.remote({"code": code}, status)):
                with self.assertRaises(GatewayError) as ctx:
                    identity_client.IdentityClient({"FB_AUTO_POST_INTERNAL_TOKEN": TOKEN}).resolve("t", "u")
                self.assertEqual(ctx.exception.code, code if code.startswith("identity_") else "identity_unavailable")


class IntegrationTests(unittest.TestCase):
    def test_pool_routes_are_management_only_materials_no_records_scope(self):
        self.assertEqual(set(route("GET", "/api/fb-video-publish/materials")[0]), {"fbVideoPublish", "fbPageManagement"})
        for method, suffix in [("GET", ""), ("GET", "/operations/" + "a" * 32)] + [
                ("POST", "/" + action) for action in ("settings", "enable", "pause", "items", "items/preview", "remove", "move")]:
            self.assertEqual(route(method, "/api/fb-video-publish/pages/123/pool" + suffix), (("fbPageManagement",), True))
        for method, suffix in (("GET", "/enable"), ("POST", ""), ("POST", "/slots"), ("GET", "/../pool")):
            with self.assertRaises(GatewayError):
                route(method, "/api/fb-video-publish/pages/123/pool" + suffix)

    def test_gateway_enable_replaces_session_snapshot_with_current_identity(self):
        handler = Handler({"is_admin": True, "sub_user_id": "999"})
        handler.headers["Origin"] = "https://example.test"
        handler._require_same_origin_json = lambda: True
        handler._cookies = lambda: {"session": "cookie"}
        session = {"tenant_key": "t", "user_id": "u", "name": "Old", "role": "admin"}
        actor = ClientTests().actor()
        app = {"SESSION_COOKIE_NAME": "session", "load_session": lambda _: session,
               "has_module_permission": lambda *a: True, "load_navigation_config": lambda: [],
               "navigation_item_access": lambda *a: {"allowed": True},
               "json_response": lambda h, s, p, **k: h.responses.append((s, p))}
        with patch.object(identity_broker, "resolve_identity", return_value=actor) as resolve, \
                patch.object(gateway, "request", return_value={"ok": True}) as request:
            gateway.dispatch(handler, gateway.urlsplit("/api/fb-video-publish/pages/123/pool/enable"), app)
        self.assertEqual(request.call_args.kwargs["actor"], actor)
        self.assertEqual(request.call_args.kwargs["payload"], {})
        resolve.assert_called_once_with(app, "t", "u")

    def test_targeted_patch_is_idempotent_preserves_source_and_rejects_drift(self):
        source = (
            "# FB_VIDEO_PUBLISH_INTEGRATION_V1\nimport urllib.parse\n"
            "class DramaMaterialHandler:\n"
            "    def do_POST(self):\n"
            "        parsed = urllib.parse.urlsplit(self.path)\n"
            "        return old(parsed)\n")
        for text in (source, source.replace("\n", "\r\n")):
            patched = patch_app(text)
            self.assertEqual(patch_app(patched), patched)
            self.assertEqual(patched.count(MARKER), 1)
            self.assertIn("return old(parsed)", patched)
            self.assertEqual(len(ast.parse(patched).body), len(ast.parse(text).body))
            with self.assertRaises(ValueError):
                patch_app(patched.replace("return dispatch(self, globals())", "return changed()"))

    def test_live_snapshot_patch_changes_only_one_insert_when_available(self):
        path = ROOT / "output/pool-live-app-baseline.py"
        if not path.exists():
            self.skipTest("Local read-only live snapshot not present")
        source = path.read_bytes().decode()
        patched = patch_app(source)
        self.assertEqual(patch_app(patched), patched)
        lines = patched.splitlines(keepends=True)
        index = next(i for i, line in enumerate(lines) if MARKER in line)
        self.assertEqual("".join(lines[:index] + lines[index + 4:]), source)

    def test_sidecar_pool_internal_routes_do_not_dispatch_worker(self):
        for action in ("start", "tick"):
            scheduler = SimpleNamespace(start=Mock(return_value={"ok": True}), tick=Mock(return_value={"ok": True}))
            handler = SimpleNamespace(path="/internal/fb-video-publish/pool-" + action, command="POST",
                internal=lambda: True, read_json=lambda: {"worker_id": "pool-test"},
                runtime=SimpleNamespace(video_pool_scheduler=scheduler, manual_worker=Mock()), send_json=Mock())
            SidecarHandler.dispatch(handler)
            getattr(scheduler, action).assert_called_once_with("pool-test")
            handler.runtime.manual_worker.dispatch.assert_not_called()
        handler.read_json = lambda: {"worker_id": "pool-test", "actor": {"is_admin": True}}
        with self.assertRaises(ServiceError):
            SidecarHandler.dispatch(handler)
        handler.internal = lambda: False
        with self.assertRaises(ServiceError) as ctx:
            SidecarHandler.dispatch(handler)
        self.assertEqual(ctx.exception.status, 403)

    def test_sidecar_identity_errors_preserve_denial_and_acceptance(self):
        handler = SimpleNamespace(send_json=Mock())
        SidecarHandler.handle_error(handler, GatewayError("identity_inactive", "账号已失效", 403))
        status, body = handler.send_json.call_args.args
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "identity_inactive")
        self.assertFalse(body["accepted"])

    def test_scheduler_reuses_worker_id_and_restarts_registration_after_failure(self):
        class Stop:
            cycles = 0
            waits = []
            def is_set(self): return self.cycles >= 3
            def wait(self, timeout):
                self.waits.append(timeout)
                self.cycles += 1
        stop, calls = Stop(), []
        def caller(action, worker):
            calls.append((action, worker))
            if len(calls) == 2: raise RuntimeError("lost response")
            return {"ok": True}
        with patch("builtins.print"):
            scheduler_client.run(stop, caller=caller, clock=lambda: 0)
        self.assertEqual([action for action, _ in calls], ["pool-start", "pool-tick", "pool-start", "pool-tick", "pool-tick"])
        self.assertEqual(len({worker for _, worker in calls}), 1)
        self.assertEqual(stop.waits, [10, 10, 10])

    def test_runtime_gates_new_features_without_stopping_frozen_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "FB_AUTO_POST_INTERNAL_TOKEN": TOKEN, "FB_AUTO_GPU_PREPARE_INTERNAL_TOKEN": TOKEN,
                "FB_AUTO_POST_DB_PATH": str(Path(directory) / "operations.sqlite"),
                "FB_AUTO_METRIC_DB_PATH": str(Path(directory) / "metrics.sqlite"),
                "FB_AUTO_POST_LIVE_ENABLED": "1", "FB_VIDEO_PUBLISH_ENABLED": "1",
                "FB_AUTO_POST_SHORT_LINK_ROOT": str(Path(directory).resolve() / "old-links"),
                "FB_VIDEO_SHORT_LINK_ROOT": str(Path(directory).resolve() / "links"),
            }
            with patch.object(identity_client.requests, "Session", side_effect=AssertionError("No network during construction")):
                runtime = build_runtime(env)
                self.assertFalse(runtime.manual.v2_enabled)
                self.assertFalse(runtime.manual.pool_enabled)
                self.assertFalse(runtime.video_pool_scheduler.live_enabled)
                self.assertTrue(runtime.manual_worker.live_enabled)
                self.assertTrue(callable(runtime.manual.pool.identity_resolver))
                runtime = build_runtime({**env, "FB_VIDEO_V2_ENABLED": "1", "FB_VIDEO_POOL_ENABLED": "1"})
                self.assertTrue(runtime.manual.v2_enabled)
                self.assertTrue(runtime.manual.pool_enabled)
                self.assertTrue(runtime.video_pool_scheduler.live_enabled)


if __name__ == "__main__":
    unittest.main()

"""No-network execution of the exact app handler and shared permission methods."""
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
import threading
import types
import unittest
from email.message import Message
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.youtube_auto_publish.templates import WorkflowError
from features.drama_synthesis.core import DramaSynthesisError

PREFIX = "/api/youtube-auto-publish"
TASK_ID = "a" * 32
COVER_ID = "b" * 32
GET_PATHS = ["/bootstrap", "/bootstrap?include_channels=0", "/channels", "/materials", "/tasks", "/settings", "/tasks/" + TASK_ID, "/covers/" + COVER_ID]
POST_PATHS = ["/tasks", "/covers", "/covers/upload", "/settings", "/tasks/" + TASK_ID + "/review", "/tasks/" + TASK_ID + "/retry"]


def load_contract():
    """Avoid legacy app import-time services and execute unmodified AST bodies."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    assignments = {"MODULE_PERMISSIONS", "DEFAULT_USER_PERMISSIONS", "ADMIN_PERMISSIONS", "_YOUTUBE_AUTO_SERVICE", "_YOUTUBE_AUTO_SERVICE_LOCK"}
    functions = {"normalize_user_permissions", "has_module_permission", "navigation_item_access", "get_youtube_auto_service"}
    methods = {"_cookies", "_youtube_auto_actor", "_youtube_auto_json", "_require_same_origin_json", "_dispatch_youtube_auto_publish", "do_GET", "do_POST"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id in assignments for x in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "DramaMaterialHandler":
            node.bases = []
            node.body = [x for x in node.body if isinstance(x, ast.FunctionDef) and x.name in methods]
            nodes.append(node)
    namespace = {"__name__": "youtube_http_contract", "json": json, "threading": threading,
                 "hashlib": hashlib, "re": re, "urlparse": urlparse, "parse_qs": parse_qs,
                 "DramaSynthesisError": DramaSynthesisError,
                 "SESSION_COOKIE_NAME": "session", "parse_json_text": lambda value, default: json.loads(value)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return namespace


class ServiceSpy:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.asset_record = None

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.failure:
                raise self.failure
            if name == "asset":
                if args[0]["user_id"] != "owner":
                    raise WorkflowError("not_found", "封面不存在", 404)
                return self.asset_record
            return {"ok": True, "method": name}
        return call


class YouTubeHttpTests(unittest.TestCase):
    def test_light_bootstrap_and_channels_contract(self):
        self.assertEqual(self.request('GET','/bootstrap?include_channels=0').status,200)
        self.assertEqual(self.service.calls[-1][2],{'include_channels':False})
        self.assertEqual(self.request('GET','/bootstrap').status,200)
        self.assertEqual(self.service.calls[-1][2],{'include_channels':True})
        self.assertEqual(self.request('GET','/channels').status,200)
        self.assertEqual(self.service.calls[-1][0],'channel_options')
    @classmethod
    def setUpClass(cls):
        cls.contract = load_contract()

    def setUp(self):
        self.network = mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.service = ServiceSpy()
        self.session = {"tenant_key": "tenant", "user_id": "owner", "open_id": "ou_owner", "name": "Operator",
                        "role": "user", "permissions": {"youtube_auto_publish": True}, "secret_token": "never_forward"}
        self.nav = json.loads((ROOT / "static/navigation.json").read_text(encoding="utf-8"))
        self.contract.update(load_session=lambda cookie: self.session if cookie == "valid" else None,
                             load_navigation_config=lambda: self.nav,
                             get_youtube_auto_service=mock.Mock(return_value=self.service),
                             feishu_auth_enabled=lambda: False)
        self.contract["parse_cookie_header"] = lambda value: {k: v.value for k, v in SimpleCookie(value).items()}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        image_path = Path(self.temp.name) / "cover.jpg"
        image_path.write_bytes(b"test-immutable-cover")
        self.service.asset_record = {"path": str(image_path), "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()}

    def request(self, method, suffix, body=b"{}", headers=None, cookie=True):
        handler = self.contract["DramaMaterialHandler"]()
        handler.command = method
        handler.path = PREFIX + suffix
        handler.headers = Message()
        default = {"Host": "ai.example", "X-Forwarded-Proto": "https"}
        if cookie:
            default["Cookie"] = "session=valid"
        if method == "POST":
            default.update({"Content-Type": "application/json", "Content-Length": str(len(body)), "Origin": "https://ai.example"})
        default.update(headers or {})
        for key, value in default.items():
            if value is not None:
                for item in value if isinstance(value, list) else [value]:
                    handler.headers[key] = item
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.close_connection = False
        handler.response_headers = {}
        handler.send_response = lambda status: setattr(handler, "status", status)
        handler.send_header = lambda key, value: handler.response_headers.update({key: value})
        handler.end_headers = lambda: None
        def json_response(target, status, payload, no_store=False):
            target.status, target.payload = status, payload
            if no_store:
                target.response_headers["Cache-Control"] = "no-store"
        self.contract["json_response"] = json_response
        getattr(handler, "do_" + method)()
        return handler

    def test_every_route_requires_actual_cookie_even_with_auth_disabled(self):
        for method, paths in [("GET", GET_PATHS), ("POST", POST_PATHS)]:
            for path in paths:
                for headers in ({}, {"Authorization": "Bearer accepted-legacy-token"}, {"X-API-Token": "accepted-legacy-token"}):
                    with self.subTest(method=method, path=path, headers=headers):
                        response = self.request(method, path, cookie=False, headers=headers)
                        self.assertEqual(response.status, 401)
                        self.assertEqual(response.payload["error"], "cookie_auth_required")
        self.contract["get_youtube_auto_service"].assert_not_called()

    def test_module_denial_applies_to_every_route_even_if_nav_omits_module(self):
        self.session["permissions"] = {"drama_synthesis": True}
        for group in self.nav:
            if group["key"] == "youtube_platform":
                group.pop("module")
                group["items"][0].pop("module")
        for method, paths in [("GET", GET_PATHS), ("POST", POST_PATHS)]:
            for path in paths:
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method, path).status, 403)
        self.assertFalse(self.service.calls)

    def test_missing_tenant_user_and_token_session_are_rejected(self):
        for key in ("tenant_key", "user_id"):
            previous = self.session[key]
            self.session[key] = ""
            self.assertEqual(self.request("GET", "/bootstrap").status, 401)
            self.session[key] = previous
        self.session["auth_type"] = "api_token"
        self.assertEqual(self.request("GET", "/bootstrap").status, 401)

    def test_shared_navigation_disabled_admin_only_and_unavailable_gates(self):
        group = next(g for g in self.nav if g["key"] == "youtube_platform")
        for node, key in [(group, "enabled"), (group["items"][0], "enabled")]:
            node[key] = False
            for method, paths in [("GET", GET_PATHS), ("POST", POST_PATHS)]:
                for path in paths:
                    self.assertEqual(self.request(method, path).status, 403)
            node[key] = True
        group["adminOnly"] = True
        self.assertEqual(self.request("GET", "/bootstrap").status, 403)
        self.session["role"] = "admin"
        self.assertEqual(self.request("GET", "/bootstrap").status, 200)
        self.contract["load_navigation_config"] = mock.Mock(side_effect=OSError("secret path"))
        response = self.request("GET", "/bootstrap")
        self.assertEqual(response.status, 503)
        self.assertNotIn("secret", json.dumps(response.payload))

    def test_content_length_and_transfer_encoding_fail_before_service(self):
        cases = [(None, 411), ("", 411), ("-1", 411), ("2.0", 411), ("+2", 411), ("1", 400), ("32769", 413), ("9" * 100, 413), (["2", "2"], 411)]
        for value, status in cases:
            with self.subTest(value=value):
                response = self.request("POST", "/tasks", headers={"Content-Length": value})
                self.assertEqual(response.status, status)
                self.assertTrue(response.close_connection)
        self.assertEqual(self.request("POST", "/tasks", headers={"Transfer-Encoding": "chunked"}).status, 411)
        self.contract["get_youtube_auto_service"].assert_not_called()

    def test_cover_upload_aliases_share_three_megabyte_limit(self):
        for path in ("/covers", "/covers/upload"):
            self.assertEqual(self.request("POST", path, headers={"Content-Length": str(3 * 1024 * 1024 + 1)}).status, 413)
            body = json.dumps({"data": "a" * 40000}).encode()
            self.assertEqual(self.request("POST", path, body=body).status, 200)
            self.assertEqual(self.service.calls[-1][0], "upload_cover")
        self.assertEqual(self.request("POST", "/tasks", body=body).status, 413)

    def test_same_origin_and_json_required_for_every_post(self):
        bad = [({"Origin": None}, 403), ({"Origin": "null"}, 403), ({"Origin": "https://evil.example"}, 403),
               ({"Origin": "http://ai.example"}, 403), ({"Origin": "file://ai.example"}, 403),
               ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Type": "text/plain"}, 415),
               ({"Content-Type": "multipart/form-data"}, 415)]
        for path in POST_PATHS:
            for headers, status in bad:
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.request("POST", path, headers=headers).status, status)
        self.assertFalse(self.service.calls)
        self.assertEqual(self.request("POST", "/tasks", headers={"Origin": None, "Referer": "https://ai.example/youtube-publish.html"}).status, 200)

    def test_invalid_json_and_truncated_bodies_are_rejected(self):
        for body in (b"null", b"[]", b"{", b"\xff\xff", b"{}trailing"):
            self.assertEqual(self.request("POST", "/tasks", body=body).status, 400)
        self.assertEqual(self.request("POST", "/tasks", headers={"Content-Length": "10"}).status, 400)
        self.assertFalse(self.service.calls)

    def test_route_dispatch_and_server_actor_projection(self):
        expected_get = ["bootstrap", "bootstrap", "channel_options", "list_materials", "list_tasks", "settings", "get_task", "asset"]
        expected_post = ["create_task", "upload_cover", "upload_cover", "save_settings", "review", "retry"]
        body = json.dumps({"actor": {"role": "admin", "user_id": "attacker"}, "creator": "fake", "role": "admin", "user_id": "fake", "is_admin": True, "title": "Allowed"}).encode()
        for method, paths, names in [("GET", GET_PATHS, expected_get), ("POST", POST_PATHS, expected_post)]:
            self.assertEqual(len(paths), len(names))
            for path, name in zip(paths, names):
                self.assertEqual(self.request(method, path, body=body).status, 200)
                call_name, args, _ = self.service.calls[-1]
                self.assertEqual(call_name, name)
                self.assertEqual(args[0], {"tenant_key": "tenant", "user_id": "owner", "open_id": "ou_owner", "name": "Operator", "role": "user", "is_admin": False})
                for value in args[1:]:
                    if isinstance(value, dict):
                        self.assertEqual(value, {"title": "Allowed"})

    def test_queries_are_forwarded_with_bounds(self):
        self.request("GET", "/materials?search=hello%20world")
        self.assertEqual(self.service.calls[-1][2], {"search": "hello world"})
        self.request("GET", "/tasks?search=" + "x" * 240 + "&status=review")
        self.assertEqual(self.service.calls[-1][2], {"search": "x" * 200, "status": "review"})
        self.assertEqual(self.request("GET", "/tasks?" + "&".join("q%d=x" % x for x in range(10))).status, 400)

    def test_cover_ownership_integrity_and_private_headers(self):
        response = self.request("GET", "/covers/" + COVER_ID)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.response_headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(response.response_headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.response_headers["Content-Type"], "image/jpeg")
        self.session["user_id"] = "other-owner"
        self.assertEqual(self.request("GET", "/covers/" + COVER_ID).status, 404)
        self.session["user_id"] = "owner"
        Path(self.service.asset_record["path"]).write_bytes(b"changed")
        response = self.request("GET", "/covers/" + COVER_ID)
        self.assertEqual(response.status, 409)
        self.assertFalse(response.wfile.getvalue())

    def test_service_errors_are_safe_and_unknown_routes_do_not_initialize(self):
        for path in ("/", "/tasks/../secret", "/covers/not-an-id", "/tasks/" + TASK_ID + "/approve"):
            self.assertEqual(self.request("GET", path).status, 404)
        self.contract["get_youtube_auto_service"].assert_not_called()
        self.service.failure = WorkflowError("approval_stale", "版本已更新", 409)
        self.assertEqual(self.request("POST", "/tasks/" + TASK_ID + "/review").payload, {"error": "approval_stale", "message": "版本已更新"})
        self.service.failure = RuntimeError("private token/password/path")
        response = self.request("GET", "/bootstrap")
        self.assertEqual(response.status, 503)
        self.assertNotIn("password", json.dumps(response.payload))
        self.service.failure = DramaSynthesisError("youtube_retry_unsafe", "发布结果尚未确认，请核对后重试", 409, private_data="never_forward")
        response = self.request("POST", "/tasks/" + TASK_ID + "/retry")
        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["error"], "youtube_retry_unsafe")
        self.assertNotIn("never_forward", json.dumps(response.payload))

    def test_module_is_independent_assignable_and_nav_matches(self):
        self.assertEqual(self.contract["MODULE_PERMISSIONS"]["youtube_auto_publish"], "YouTube 自动发布")
        self.assertFalse(self.contract["DEFAULT_USER_PERMISSIONS"]["youtube_auto_publish"])
        self.assertTrue(self.contract["ADMIN_PERMISSIONS"]["youtube_auto_publish"])
        normalize = self.contract["normalize_user_permissions"]
        self.assertFalse(normalize({"drama_synthesis": True})["youtube_auto_publish"])
        self.assertTrue(normalize({"youtube_auto_publish": True})["youtube_auto_publish"])
        item = next(item for group in self.nav for item in group["items"] if item["key"] == "youtubeAutoPublish")
        self.assertEqual(item["href"], "/youtube-publish.html")
        self.assertEqual(item["module"], "youtube_auto_publish")

    def real_service(self):
        from features.youtube_auto_publish.service import YouTubeWorkflow
        from features.youtube_auto_publish.source import MaterialSource
        query = mock.Mock(side_effect=AssertionError("SQL forbidden"))
        channels = mock.Mock(side_effect=AssertionError("channel lookup forbidden"))
        service = YouTubeWorkflow(Path(self.temp.name) / "workflow.sqlite", Path(self.temp.name) / "assets",
                                  MaterialSource("", query), channels, mock.Mock(), mock.Mock())
        self.contract["get_youtube_auto_service"] = lambda: service
        return service, query, channels

    def test_real_service_unconfigured_source_dto_and_business_error(self):
        _, query, channels = self.real_service()
        bootstrap = self.request("GET", "/bootstrap")
        self.assertEqual(bootstrap.status, 200)
        self.assertFalse(bootstrap.payload["source"]["configured"])
        self.assertEqual(bootstrap.payload["channels"], [])
        materials = self.request("GET", "/materials")
        self.assertEqual(materials.payload["items"], [])
        self.assertFalse(materials.payload["configured"])
        body = json.dumps({"operation_id": "idempotent-request-0001", "material_id": "123"}).encode()
        response = self.request("POST", "/tasks", body=body)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["error"], "material_source_unconfigured")
        self.assertTrue(response.payload["message"])
        self.assertEqual(self.request("GET", "/tasks").payload["items"], [])
        query.assert_not_called()
        channels.assert_not_called()

    def test_real_service_cover_access_and_settings_admin_gate(self):
        from PIL import Image
        import base64
        self.real_service()
        data = io.BytesIO()
        Image.new("RGB", (320, 180), "blue").save(data, format="JPEG")
        payload = json.dumps({"data": base64.b64encode(data.getvalue()).decode(), "actor": {"role": "admin"}}).encode()
        upload = self.request("POST", "/covers", body=payload)
        self.assertEqual(upload.status, 200)
        cover_path = upload.payload["asset"]["url"][len(PREFIX):]
        self.assertEqual(self.request("GET", cover_path).status, 200)
        self.session["user_id"] = "other-owner"
        self.assertEqual(self.request("GET", cover_path).status, 404)
        self.session["role"] = "admin"
        self.assertEqual(self.request("GET", cover_path).status, 200)
        self.session["tenant_key"] = "other-tenant"
        self.assertEqual(self.request("GET", cover_path).status, 404)
        self.session["tenant_key"] = "tenant"
        self.session["role"] = "user"
        settings = json.dumps({"default_description": "Reviewed default", "role": "admin"}).encode()
        self.assertEqual(self.request("POST", "/settings", body=settings).status, 403)
        self.session["role"] = "admin"
        self.assertEqual(self.request("POST", "/settings", body=settings).status, 200)
        self.assertEqual(self.request("GET", "/settings").payload["default_description"], "Reviewed default")

    def test_service_initialization_is_lazy_and_thread_safe(self):
        runtime = types.ModuleType("features.youtube_auto_publish.runtime")
        runtime.build_service = mock.Mock(return_value=object())
        namespace = load_contract()
        self.assertIsNone(namespace["_YOUTUBE_AUTO_SERVICE"])
        runtime.build_service.assert_not_called()
        app_module = types.ModuleType(namespace["__name__"])
        results = []
        with mock.patch.dict(sys.modules, {runtime.__name__: runtime, app_module.__name__: app_module}):
            threads = [threading.Thread(target=lambda: results.append(namespace["get_youtube_auto_service"]())) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        runtime.build_service.assert_called_once_with(app_module)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result is results[0] for result in results))


if __name__ == "__main__":
    unittest.main(verbosity=2)

import ast
import io
import json
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from features.ad_control_v3 import routes
from features.ad_control_v3.errors import AdControlV3Error


class FakeHandler:
    def __init__(self, *, session=None, payload=None):
        self.headers = {
            "Content-Type": "application/json",
            "Host": "ai.example.test",
        }
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.cookie_allowed = True
        self.module_allowed = True
        self.origin_allowed = True
        self.cookie_checks = []
        self.module_checks = []
        self.same_origin_checks = 0
        self._session_value = session or {
            "user_id": "user-1",
            "role": "optimizer",
            "email": "user@example.test",
        }
        self.payload = {} if payload is None else payload

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        return None

    def _require_cookie_module(self, module):
        self.cookie_checks.append(module)
        return self.cookie_allowed

    def _require_module(self, module):
        self.module_checks.append(module)
        return self.module_allowed

    def _require_same_origin_json(self):
        self.same_origin_checks += 1
        return self.origin_allowed

    def _session(self):
        return dict(self._session_value)

    def _read_json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class FakeService:
    def __init__(self):
        self.calls = []

    def _result(self, name, *args):
        self.calls.append((name, args))
        return {"operation": name}

    def meta(self, actor):
        return self._result("meta", actor)

    def list_rule_groups(self, actor, filters, page, page_size):
        return self._result("list_rule_groups", actor, filters, page, page_size)

    def create_rule_group(self, actor, payload):
        return self._result("create_rule_group", actor, payload)

    def get_rule_group(self, actor, group_id):
        return self._result("get_rule_group", actor, group_id)

    def update_rule_group(self, actor, group_id, payload, expected_version):
        return self._result("update_rule_group", actor, group_id, payload, expected_version)

    def delete_rule_group(self, actor, group_id):
        return self._result("delete_rule_group", actor, group_id)

    def duplicate_rule_group(self, actor, group_id, payload):
        return self._result("duplicate_rule_group", actor, group_id, payload)

    def preview(self, actor, group_id, payload):
        return self._result("preview", actor, group_id, payload)

    def execute(self, actor, group_id, payload):
        return self._result("execute", actor, group_id, payload)

    def set_enabled(self, actor, group_id, enabled, confirm):
        return self._result("set_enabled", actor, group_id, enabled, confirm)

    def emergency_stop(self, actor, group_id):
        return self._result("emergency_stop", actor, group_id)

    def scope_estimate(self, actor, payload):
        return self._result("scope_estimate", actor, payload)

    def list_executions(self, actor, filters, page, page_size):
        return self._result("list_executions", actor, filters, page, page_size)

    def get_execution(self, actor, execution_id):
        return self._result("get_execution", actor, execution_id)


class AdControlV3RouteTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.factory = mock.patch.object(routes, "get_service", return_value=self.service)
        self.service_factory = self.factory.start()

    def tearDown(self):
        self.factory.stop()

    @staticmethod
    def dispatch(path, method="GET", handler=None):
        handler = handler or FakeHandler()
        routes.dispatch(handler, method, urlparse(path))
        return handler

    def test_dynamic_pages_and_assets_require_cookie_module_and_are_no_store(self):
        page = self.dispatch("/api/ad-control/v3/ui/rule-groups")
        self.assertEqual(200, page.status)
        self.assertEqual(["ad_control_v3"], page.cookie_checks)
        self.assertEqual([], page.module_checks)
        self.assertEqual("no-store", page.response_headers["Cache-Control"])
        self.assertIn("text/html", page.response_headers["Content-Type"])
        self.assertIn(
            "'sha256-hwxbDTADufampcgI9oc75ltbbfB38tCWOve6LIq/j68='",
            page.response_headers["Content-Security-Policy"],
        )
        self.assertIn(b"ruleGroupsApp", page.wfile.getvalue())

        asset = self.dispatch("/api/ad-control/v3/assets/app.css")
        self.assertEqual(200, asset.status)
        self.assertEqual(["ad_control_v3"], asset.cookie_checks)
        self.assertEqual("nosniff", asset.response_headers["X-Content-Type-Options"])
        self.assertIn("text/css", asset.response_headers["Content-Type"])

    def test_cookie_denial_stops_page_before_render_or_service(self):
        handler = FakeHandler()
        handler.cookie_allowed = False
        result = self.dispatch("/api/ad-control/v3/ui/execution-logs", handler=handler)
        self.assertIsNone(result.status)
        self.assertEqual([], self.service.calls)

    def test_meta_requires_module_and_receives_server_actor(self):
        handler = FakeHandler(session={"user_id": "admin-1", "role": "admin"})
        result = self.dispatch("/api/ad-control/v3/meta", handler=handler)
        self.assertEqual(200, result.status)
        self.assertEqual(["ad_control_v3"], result.module_checks)
        call = self.service.calls[0]
        self.assertEqual("meta", call[0])
        self.assertTrue(call[1][0]["is_admin"])

    def test_all_writes_require_same_origin_json_before_service_resolution(self):
        for method, path in (
            ("POST", "/api/ad-control/v3/rule-groups"),
            ("PUT", "/api/ad-control/v3/rule-groups/g-1"),
            ("DELETE", "/api/ad-control/v3/rule-groups/g-1"),
        ):
            with self.subTest(method=method):
                handler = FakeHandler(payload={})
                handler.origin_allowed = False
                self.dispatch(path, method, handler)
                self.assertEqual(1, handler.same_origin_checks)
        self.assertEqual([], self.service.calls)
        self.service_factory.assert_not_called()

    def test_rule_group_crud_and_actions_use_strict_methods(self):
        created = self.dispatch(
            "/api/ad-control/v3/rule-groups",
            "POST",
            FakeHandler(payload={"name": "draft"}),
        )
        self.assertEqual(201, created.status)
        self.assertEqual("create_rule_group", self.service.calls[-1][0])

        listed = self.dispatch(
            "/api/ad-control/v3/rule-groups?page=2&page_size=40&product=Dramawave&product=FreeReels"
        )
        self.assertEqual(200, listed.status)
        _, args = self.service.calls[-1]
        self.assertEqual({"product": ["Dramawave", "FreeReels"]}, args[1])
        self.assertEqual((2, 40), args[2:])

        updated = self.dispatch(
            "/api/ad-control/v3/rule-groups/group%3A1",
            "PUT",
            FakeHandler(payload={"name": "updated", "version": 7}),
        )
        self.assertEqual(200, updated.status)
        self.assertEqual(("group:1", {"name": "updated", "version": 7}, 7), self.service.calls[-1][1][1:])

        enabled = self.dispatch(
            "/api/ad-control/v3/rule-groups/group%3A1/enabled",
            "POST",
            FakeHandler(payload={"enabled": True, "confirm": "ENABLE_LIVE_MODE"}),
        )
        self.assertEqual(200, enabled.status)
        self.assertEqual(("group:1", True, "ENABLE_LIVE_MODE"), self.service.calls[-1][1][1:])

        executed = self.dispatch(
            "/api/ad-control/v3/rule-groups/group%3A1/execute",
            "POST",
            FakeHandler(payload={"confirm": "EXECUTE_LIVE_RULE_GROUP"}),
        )
        self.assertEqual(200, executed.status)
        self.assertEqual("execute", self.service.calls[-1][0])
        self.assertEqual(
            ("group:1", {"confirm": "EXECUTE_LIVE_RULE_GROUP"}),
            self.service.calls[-1][1][1:],
        )

        deleted = self.dispatch(
            "/api/ad-control/v3/rule-groups/group%3A1",
            "DELETE",
            FakeHandler(payload={}),
        )
        self.assertEqual(200, deleted.status)
        self.assertEqual("delete_rule_group", self.service.calls[-1][0])

    def test_non_boolean_enabled_and_invalid_pagination_are_safe_400(self):
        enabled = self.dispatch(
            "/api/ad-control/v3/rule-groups/g/enabled",
            "POST",
            FakeHandler(payload={"enabled": "false"}),
        )
        self.assertEqual(400, enabled.status)
        self.assertEqual("validation_error", enabled.json()["error"])

        page = self.dispatch("/api/ad-control/v3/executions?page_size=1000")
        self.assertEqual(400, page.status)
        self.assertEqual("validation_error", page.json()["error"])

        deep_page = self.dispatch("/api/ad-control/v3/executions?page=1001")
        self.assertEqual(400, deep_page.status)
        self.assertEqual("validation_error", deep_page.json()["error"])

    def test_oversized_json_is_rejected_before_service_construction(self):
        handler = FakeHandler(payload={})
        handler.headers["Content-Length"] = str(routes.MAX_JSON_BODY_BYTES + 1)
        result = self.dispatch(
            "/api/ad-control/v3/rule-groups",
            "POST",
            handler,
        )
        self.assertEqual(413, result.status)
        self.assertEqual("request_too_large", result.json()["error"])
        self.service_factory.assert_not_called()

    def test_service_error_status_and_details_are_preserved_as_json(self):
        self.factory.stop()
        error = AdControlV3Error(
            "optimizer_forbidden",
            "optimizer is not allowed",
            status=403,
            details={"optimizer_id": 99},
        )
        with mock.patch.object(routes, "get_service", side_effect=error):
            result = self.dispatch("/api/ad-control/v3/meta")
        self.factory.start()
        self.assertEqual(403, result.status)
        self.assertEqual("optimizer_forbidden", result.json()["error"])
        self.assertEqual({"optimizer_id": 99}, result.json()["details"])
        self.assertIn("application/json", result.response_headers["Content-Type"])

    def test_unknown_routes_and_methods_are_json_and_do_not_leak_paths(self):
        missing = self.dispatch("/api/ad-control/v3/assets/../../app.py")
        self.assertEqual(404, missing.status)
        self.assertEqual("not_found", missing.json()["error"])

        wrong_method = self.dispatch("/api/ad-control/v3/executions", "POST", FakeHandler(payload={}))
        self.assertEqual(405, wrong_method.status)
        self.assertEqual("GET", wrong_method.response_headers["Allow"])
        self.assertEqual("method_not_allowed", wrong_method.json()["error"])
        self.service_factory.assert_not_called()


class AdControlV3LazyAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.handler = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DramaMaterialHandler"
        )

    def method(self, name):
        return next(
            node
            for node in self.handler.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    def test_routes_module_is_imported_only_inside_lazy_dispatcher(self):
        module_imports = [
            node
            for node in self.tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and "ad_control_v3" in ast.unparse(node)
        ]
        self.assertEqual([], module_imports)
        dispatcher = self.method("_dispatch_ad_control_v3")
        imports = [
            node
            for node in ast.walk(dispatcher)
            if isinstance(node, ast.ImportFrom)
            and node.module == "features.ad_control_v3"
        ]
        self.assertEqual(1, len(imports))

    def test_v3_guard_precedes_legacy_routes_for_get_post_and_delete(self):
        for method_name in ("do_GET", "do_POST", "do_DELETE"):
            with self.subTest(method=method_name):
                method = self.method(method_name)
                guard_indexes = [
                    index
                    for index, node in enumerate(method.body)
                    if isinstance(node, ast.If)
                    and "/api/ad-control/v3" in ast.unparse(node.test)
                ]
                self.assertEqual(1, len(guard_indexes))
                dispatch_call = next(
                    node
                    for node in ast.walk(method.body[guard_indexes[0]])
                    if isinstance(node, ast.Call)
                    and "_dispatch_ad_control_v3" in ast.unparse(node.func)
                )
                self.assertIsNotNone(dispatch_call)
                legacy_route_indexes = [
                    index
                    for index, node in enumerate(method.body)
                    if isinstance(node, ast.If)
                    and "/api/ad-control/v3" not in ast.unparse(node.test)
                ]
                self.assertTrue(legacy_route_indexes)
                self.assertLess(guard_indexes[0], min(legacy_route_indexes))

    def test_put_reuses_post_dispatch_with_original_http_command(self):
        put_method = self.method("do_PUT")
        self.assertIn("self.do_POST()", ast.unparse(put_method))
        dispatcher = self.method("_dispatch_ad_control_v3")
        self.assertIn("self.command", ast.unparse(dispatcher))

    def test_http_and_deployer_sources_parse_as_python_39(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "app.py",
            "features/ad_control_v3/routes.py",
            "deploy/apply_ad_control_v3.py",
        ):
            with self.subTest(path=relative):
                source = (root / relative).read_text(encoding="utf-8")
                ast.parse(source, filename=relative, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()

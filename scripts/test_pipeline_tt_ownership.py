"""Trusted cookie ownership reaches new TT direct-test rows without rewriting history."""
import ast
import contextlib
import logging
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import test_tt_posts_service as fixtures


class DirectTestOwnership(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service(fixtures.OPEN_GATES)
        self.config = self.fixture.configure_auto(self.service, ["101"])

    def payload(self):
        return self.fixture.direct_test_payload(self.config["version"])

    def owner(self, identity):
        with contextlib.closing(sqlite3.connect(self.service.store.db_path)) as conn:
            return conn.execute("SELECT created_by_user_id,created_by_name FROM tt_post_direct_test WHERE id=?", (identity,)).fetchone()

    @staticmethod
    def gateway(request, session, send, *, path="/api/admin/tt-posts/test-publish"):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8-sig"))
        route = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Compare) and isinstance(node.test.comparators[0], ast.Set)
                     and any(isinstance(item, ast.Constant) and item.value == "/api/admin/tt-posts/test-publish" for item in node.test.comparators[0].elts)
                     and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_tt_post_service_request" for call in ast.walk(node)))
        wrapper = ast.parse("def dispatch(self, parsed):\n    pass\n").body[0]
        wrapper.body = [route]
        module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
        reply = Mock()
        namespace = {"_tt_post_service_request": send, "append_audit_log": Mock(), "json_response": reply,
                     "logging": logging, "TTPostAdminClientError": RuntimeError}
        exec(compile(module, "app.py", "exec"), namespace)
        caller = SimpleNamespace(_require_cookie_navigation_item=Mock(return_value=True),
            _require_same_origin_json=Mock(return_value=True), _session=lambda: session, _read_json=lambda: request)
        namespace["dispatch"](caller, SimpleNamespace(path=path))
        return reply

    def test_real_gateway_overrides_forged_browser_owner_before_sqlite_creation(self):
        request = {**self.payload(), "_internal_actor": {"user_id": "attacker", "name": "Fake"},
                   "actor": {"user_id": "attacker"}, "actor_user_id": "attacker", "actor_name": "Fake",
                   "created_by_user_id": "attacker", "owner_user_id": "attacker", "owner_tenant_key": "fake"}
        observed = []
        def send(method, path, payload):
            observed.append(payload)
            return self.service.direct_test_create(payload)
        reply = self.gateway(request, {"user_id": "cookie-owner", "name": "Cookie Name", "tenant_key": "real"}, send)
        self.assertEqual(reply.call_args.args[1], 200)
        item = reply.call_args.args[2]["item"]
        self.assertEqual(self.owner(item["id"]), ("cookie-owner", "Cookie Name"))
        self.assertEqual(observed[0]["_internal_actor"], {"user_id": "cookie-owner", "name": "Cookie Name"})
        self.assertFalse(set(observed[0]) & {"actor", "actor_user_id", "actor_name", "created_by_user_id", "owner_user_id", "owner_tenant_key"})
        self.assertEqual(request["_internal_actor"]["user_id"], "attacker")

    def test_replay_never_reassigns_existing_or_historical_owner(self):
        for with_owner in (False, True):
            payload = {**self.payload(), "idempotency_key": "owner-replay-" + str(with_owner)}
            if with_owner:
                payload["_internal_actor"] = {"user_id": "original", "name": "Original"}
            created = self.service.direct_test_create(payload)["item"]
            replay = self.service.direct_test_create({**payload, "_internal_actor": {"user_id": "other", "name": "Other"}})["item"]
            self.assertEqual(replay["id"], created["id"])
            self.assertEqual(self.owner(created["id"]), ("original", "Original") if with_owner else ("", ""))
            with contextlib.closing(sqlite3.connect(self.service.store.db_path)) as conn:
                conn.execute("UPDATE tt_post_direct_test SET status='failed' WHERE id=?", (created["id"],))
                conn.commit()

    def test_gateway_does_not_add_actor_to_other_tt_routes(self):
        request = {"material_id": "9001"}
        send = Mock(return_value={"item": {}})
        reply = self.gateway(request, {"user_id": "cookie-owner", "name": "Name"}, send, path="/api/admin/tt-posts/materials/preview")
        self.assertEqual(reply.call_args.args[1], 200)
        self.assertEqual(send.call_args.kwargs["payload"], request)


if __name__ == "__main__":
    unittest.main()

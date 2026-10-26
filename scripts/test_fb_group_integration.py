"""Group entry permission and identity separation; isolated, never calls Meta."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_video_publish import identity_broker, identity_client
from features.fb_video_publish.client import GatewayError, route
import test_fb_video_pool_identity as legacy
import fb_group_pool_scheduler as scheduler


class GroupIdentityTests(unittest.TestCase):
    def setUp(self):
        legacy.IdentityTests.setUp(self)

    def tearDown(self):
        legacy.IdentityTests.tearDown(self)

    def test_group_only_navigation_does_not_require_page_management(self):
        self.app["navigation_item_access"] = lambda actor, key, nav: {"allowed": key == "fbPostMaterialPool"}
        actor = identity_broker.resolve_identity(self.app, "t", "u", navigation_key="fbPostMaterialPool")
        self.assertEqual(actor["sub_user_id"], "42")
        with self.assertRaises(GatewayError):
            identity_broker.resolve_identity(self.app, "t", "u")

    def test_old_page_permission_does_not_authorize_group_schedule(self):
        with self.assertRaises(GatewayError) as exc:
            identity_broker.resolve_identity(self.app, "t", "u", navigation_key="fbPostMaterialPool")
        self.assertEqual(exc.exception.code, "identity_permission_denied")
        self.mapping_mock.assert_not_called()

    def test_private_broker_checks_selected_entry(self):
        self.app["navigation_item_access"] = lambda actor, key, nav: {"allowed": key == "fbPostMaterialPool"}
        handler = legacy.Handler({"tenant_key": "t", "user_id": "u", "navigation_key": "fbPostMaterialPool"})
        with patch.dict(identity_broker.os.environ, {"FB_AUTO_POST_INTERNAL_TOKEN": legacy.TOKEN}):
            identity_broker.dispatch(handler, self.app)
        self.assertEqual(handler.responses[0][0], 200)
        bad = legacy.Handler({"tenant_key": "t", "user_id": "u", "navigation_key": "users"})
        with patch.dict(identity_broker.os.environ, {"FB_AUTO_POST_INTERNAL_TOKEN": legacy.TOKEN}):
            identity_broker.dispatch(bad, self.app)
        self.assertEqual(bad.responses[0][0], 400)

    def test_client_forwards_only_allowlisted_entry(self):
        fixtures = legacy.ClientTests()
        session = fixtures.remote({"ok": True, "actor": fixtures.actor()})
        with patch.object(identity_client.requests, "Session", return_value=session):
            identity_client.IdentityClient({"FB_AUTO_POST_INTERNAL_TOKEN": legacy.TOKEN}).resolve("t", "u", navigation_key="fbPostMaterialPool")
        self.assertEqual(session.post.call_args.kwargs["json"], {"tenant_key": "t", "user_id": "u", "navigation_key": "fbPostMaterialPool"})
        with self.assertRaises(GatewayError):
            identity_client.IdentityClient({}).resolve("t", "u", navigation_key="users")


class GroupRouteTests(unittest.TestCase):
    def test_group_routes_do_not_inherit_manual_page_permissions(self):
        prefix = "/api/fb-video-publish"
        for method, suffix in [("GET", "/page-groups"), ("GET", "/material-pool"), ("GET", "/material-pool/schedule"),
                               ("GET", "/material-pool/runs"), ("GET", "/material-pool/operations/" + "a" * 32),
                               *[("POST", "/material-pool/" + part) for part in ("items", "items/resolve", "items/remove", "schedule", "preview", "pages/preview", "enable", "pause")]]:
            self.assertEqual(route(method, prefix + suffix)[0], ("fbPostMaterialPool",))
        self.assertTrue(route("POST", prefix + "/material-pool/enable")[1])
        self.assertTrue(route("POST", prefix + "/material-pool/pages/preview")[1])
        self.assertFalse(route("GET", prefix + "/material-pool/runs")[1])
        for method, suffix in [("DELETE", "/material-pool"), ("POST", "/material-pool/run-now"), ("GET", "/material-pool/unsafe"), ("GET", "/page-groups/")]:
            with self.assertRaises(GatewayError):
                route(method, prefix + suffix)

    def test_scheduler_reconnect_registers_before_tick(self):
        class Stop:
            iterations = 0
            def is_set(self):
                return self.iterations >= 2
            def wait(self, seconds):
                self.iterations += 1
        actions = []
        def caller(action, worker):
            actions.append(action)
            return {"ok": True, "status": "not_started" if len(actions) == 2 else "tick_complete"}
        scheduler.run(Stop(), caller=caller, clock=lambda: 0)
        self.assertEqual(actions, ["group-pool-start", "group-pool-tick", "group-pool-start", "group-pool-tick"])


if __name__ == "__main__":
    unittest.main()

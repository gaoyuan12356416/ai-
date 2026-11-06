"""Exercise real authenticated legacy HTTP dispatch branches without network writes."""
import io
import json
import os
import unittest
from email.message import Message
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch

from features.fb_auto_posts import service as fb
from features.tt_auto_posts import service as tt_auto
from features.tt_posts import service as tt
from features.x_accounts import oauth_service as x


TOKEN = "fixture-internal-token-only"


def handler(cls, path, body=None, authorized=True):
    instance = cls.__new__(cls)
    encoded = json.dumps(body or {"worker_id": "fixture-worker"}).encode()
    instance.path, instance.command = path, "POST"
    instance.client_address = ("127.0.0.1", 1)
    instance.headers = Message()
    instance.headers["Authorization"] = "Bearer " + (TOKEN if authorized else "wrong")
    instance.headers["Content-Length"] = str(len(encoded))
    instance.rfile = io.BytesIO(encoded)
    instance.send_json, instance.send_text, instance._json = Mock(), Mock(), Mock()
    return instance


class LegacyAdmissionTests(unittest.TestCase):
    def fb_handler(self, path, enabled=True, authorized=True):
        runtime = SimpleNamespace(internal_token=TOKEN, prepare_next=Mock(return_value={"legacy": True}),
            executor=SimpleNamespace(execute_next=Mock(return_value={"legacy": True}), reconcile_next=Mock(return_value={"readonly": True})),
            manual_worker=SimpleNamespace(dispatch=Mock(return_value={"legacy": True})),
            plan_next=Mock(return_value={"planned": True}), tick=Mock(return_value={"planned": True}),
            video_pool_scheduler=SimpleNamespace(start=Mock(return_value={"status": "started"}),
                tick=Mock(return_value={"status": "tick_complete"})))
        request = handler(fb.Handler, path, authorized=authorized)
        request.runtime = runtime
        with patch.dict(os.environ, {"PIPELINE_FB_DISPATCH_ENABLED": "1" if enabled else "0"}):
            request.dispatch()
        return request, runtime

    def test_fb_prepare_and_publish_handoff_before_original_executor(self):
        for path in ("/internal/fb-auto-post/prepare-next", "/internal/fb-auto-post/execute-next", "/internal/fb-video-publish/prepare-next", "/internal/fb-video-publish/execute-next"):
            with self.subTest(path=path):
                request, runtime = self.fb_handler(path)
                result = request.send_json.call_args.args[1]
                self.assertEqual((result["status"], result["claimed"], result["item"], result["items"]), ("dispatch_queued", False, None, []))
                runtime.prepare_next.assert_not_called()
                runtime.executor.execute_next.assert_not_called()
                runtime.manual_worker.dispatch.assert_not_called()
                old, _ = self.fb_handler(path, enabled=False)
                self.assertEqual(old.send_json.call_args.args, (200, {"legacy": True}))

    def test_fb_guard_keeps_auth_planning_and_readonly_reconciliation(self):
        with self.assertRaises(fb.ServiceError):
            self.fb_handler("/internal/fb-auto-post/execute-next", authorized=False)
        for path in ("/internal/fb-auto-post/tick", "/internal/fb-auto-post/plan-next", "/internal/fb-auto-post/reconcile-next", "/internal/fb-video-publish/link-next", "/internal/fb-video-publish/reconcile-next"):
            with self.subTest(path=path):
                request, _ = self.fb_handler(path)
                self.assertNotIn("dispatch_queued", request.send_json.call_args.args[1])

    def test_fb_new_bridge_still_calls_canonical_dispatch(self):
        with patch.dict(os.environ, {"PIPELINE_FB_CANONICAL_BRIDGE_ENABLED": "1"}), patch("features.pipeline_dispatch.fb_native.handle_fb_dispatch", return_value={"accepted": True}) as bridge:
            request, _ = self.fb_handler("/internal/pipeline-dispatch/fb/reserve")
            bridge.assert_called_once()
            self.assertEqual(request.send_json.call_args.args, (200, {"accepted": True}))

    def test_fb_page_pool_planning_survives_execution_handoff(self):
        for action, status in (("start", "started"), ("tick", "tick_complete")):
            with self.subTest(action=action):
                request, runtime = self.fb_handler("/internal/fb-video-publish/pool-" + action)
                self.assertEqual(request.send_json.call_args.args, (200, {"status": status}))
                getattr(runtime.video_pool_scheduler, action).assert_called_once_with("fixture-worker")
                runtime.manual_worker.dispatch.assert_not_called()
                runtime.executor.execute_next.assert_not_called()

    def test_fb_authenticated_bridge_serializer_only_allows_internal_credentials(self):
        payload = {"credentials": {"access_token": "fixture-private-value"}}
        for path, authorized, bridge_enabled in (("/internal/pipeline-dispatch/fb/begin-operation", True, True),
                                                  ("/api/admin/fb-auto-publish/templates", True, True),
                                                  ("/internal/pipeline-dispatch/fb/begin-operation", False, True),
                                                  ("/internal/pipeline-dispatch/fb/begin-operation", True, False)):
            with self.subTest(path=path, authorized=authorized, bridge_enabled=bridge_enabled):
                request = handler(fb.Handler, path, authorized=authorized)
                request.runtime = SimpleNamespace(internal_token=TOKEN)
                request.send_json = MethodType(fb.Handler.send_json, request)
                request.send_response, request.send_header, request.end_headers = Mock(), Mock(), Mock()
                request.wfile = io.BytesIO()
                with patch.dict(os.environ, {"PIPELINE_FB_DISPATCH_ENABLED": "1", "PIPELINE_FB_CANONICAL_BRIDGE_ENABLED": "1" if bridge_enabled else "0"}):
                    if authorized and bridge_enabled and path.startswith("/internal/"):
                        with patch("features.pipeline_dispatch.fb_native.handle_fb_dispatch", return_value=payload):
                            request.dispatch()
                    else:
                        request.send_json(200, payload)
                returned = json.loads(request.wfile.getvalue())
                if authorized and bridge_enabled and path.startswith("/internal/"):
                    self.assertEqual(request.send_response.call_args.args, (200,))
                    self.assertEqual(returned, payload)
                else:
                    self.assertEqual(request.send_response.call_args.args, (500,))
                    self.assertNotIn("credentials", returned)

    def test_tt_auto_legacy_guard_new_bridge_and_old_flag(self):
        for enabled in (True, False):
            service = SimpleNamespace(execute_next=Mock(return_value={"legacy": True}))
            request = handler(tt_auto.TTAutoPostRequestHandler, "/internal/tt-auto-post/execute-next")
            request.server = SimpleNamespace(internal_token=TOKEN, tt_auto_service=service)
            with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1" if enabled else "0"}):
                status, result = request._dispatch()
            self.assertEqual(status, 200)
            if enabled:
                self.assertFalse(result["claimed"])
                service.execute_next.assert_not_called()
            else:
                self.assertTrue(result["legacy"])
                service.execute_next.assert_called_once()
        request.path = "/internal/pipeline-dispatch/tt/reserve"
        request.rfile = io.BytesIO(b"{}")
        request.headers.replace_header("Content-Length", "2")
        with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1", "PIPELINE_TT_CANONICAL_BRIDGE_ENABLED": "1"}), patch("features.pipeline_dispatch.tt_native.handle_tt_dispatch", return_value={"canonical": True}) as bridge:
            self.assertEqual(request._dispatch(), (200, {"canonical": True}))
            bridge.assert_called_once()

    def test_tt_material_claim_and_execution_routes_handoff(self):
        paths = ("/internal/tt-posts/claim", "/internal/tt-posts/direct-tests/claim",
                 "/internal/tt-posts/preparations/claim", "/internal/tt-posts/direct-tests/preparations/claim",
                 "/internal/tt-posts/preparations/1/process", "/internal/tt-posts/direct-tests/preparations/1/process",
                 "/internal/tt-posts/queue/1/publish", "/internal/tt-posts/direct-tests/1/publish")
        for path in paths:
            with self.subTest(path=path):
                service = Mock()
                request = handler(tt.TTPostRequestHandler, path)
                request.server = SimpleNamespace(internal_token=TOKEN, tt_service=service)
                with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1"}):
                    result = request._dispatch()
                self.assertEqual((result["status"], result["items"], result["item"]), ("dispatch_queued", [], None))
                self.assertEqual(service.mock_calls, [])

    def test_tt_material_auth_bridge_and_reconcile_unchanged(self):
        service = SimpleNamespace(publish_claimed=Mock(return_value={"legacy": True}), reconcile=Mock(return_value={"readonly": True}))
        request = handler(tt.TTPostRequestHandler, "/internal/tt-posts/queue/1/publish", {"claim_token": "fixture"})
        request.server = SimpleNamespace(internal_token=TOKEN, tt_service=service)
        with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "0"}):
            self.assertEqual(request._dispatch(), {"legacy": True})
        request.path = "/internal/tt-posts/queue/1/reconcile"
        request.rfile = io.BytesIO(b"{}")
        request.headers.replace_header("Content-Length", "2")
        with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1"}):
            self.assertEqual(request._dispatch(), {"readonly": True})
        request.path = "/internal/pipeline-dispatch/tt/reserve"
        request.rfile = io.BytesIO(b"{}")
        with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1", "PIPELINE_TT_CANONICAL_BRIDGE_ENABLED": "1"}), patch("features.pipeline_dispatch.tt_native.handle_tt_dispatch", return_value={"canonical": True}) as bridge:
            self.assertEqual(request._dispatch(), {"canonical": True})
            bridge.assert_called_once()
        request.path = "/internal/tt-posts/queue/1/publish"
        request.headers.replace_header("Authorization", "Bearer wrong")
        with patch.dict(os.environ, {"PIPELINE_TT_DISPATCH_ENABLED": "1"}), self.assertRaises(PermissionError):
            request._dispatch()

    def test_x_queue_handoff_after_existing_token_scope_checks(self):
        for path, role in (("/internal/posts/queue/1/publish", "daily"), ("/internal/posts/auto-template/queue/1/publish", "auto")):
            for enabled in (True, False):
                with self.subTest(path=path, enabled=enabled):
                    request = handler(x.Handler, path)
                    with patch.object(x, "INTERNAL_TOKEN", "backend-other"), patch.object(x, "DAILY_INTERNAL_TOKEN", TOKEN if role == "daily" else "daily-other"), patch.object(x, "AUTO_INTERNAL_TOKEN", TOKEN if role == "auto" else "auto-other"), patch.object(x, "publish_queue_request", return_value={"legacy": True}) as publish, patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1" if enabled else "0"}):
                        request.do_POST()
                    status, result = request.send_json.call_args.args
                    self.assertEqual(status, 200)
                    if enabled:
                        self.assertFalse(result["claimed"])
                        publish.assert_not_called()
                    else:
                        self.assertEqual(result, {"item": {"legacy": True}})
                        publish.assert_called_once()
        request = handler(x.Handler, "/internal/posts/auto-template/queue/1/publish")
        with patch.object(x, "INTERNAL_TOKEN", TOKEN), patch.object(x, "publish_queue_request") as publish, patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1"}):
            request.do_POST()
        self.assertEqual(request.send_json.call_args.args[0], 403)
        publish.assert_not_called()

    def test_x_new_bridge_is_not_intercepted(self):
        request = handler(x.Handler, "/internal/pipeline-dispatch/x/reserve")
        with patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1"}), patch("features.pipeline_dispatch.x_native.handle", return_value="canonical") as bridge:
            self.assertEqual(request.do_POST(), "canonical")
            bridge.assert_called_once()


if __name__ == "__main__":
    unittest.main()

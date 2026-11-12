import copy
from email.message import Message
import io
import json
import unittest
from unittest import mock
from urllib.parse import urlsplit

from features.moboreels_library import bridge


SESSION = {"tenant_key": "tenant-trusted", "user_id": "user-trusted", "name": "真实登录用户", "role": "user"}


class FakeHandler:
    def __init__(self, method="POST", cookie="session-ok", body=b"{}"):
        self.command = method
        self.cookie = cookie
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.same_origin = True
        self.same_origin_checks = 0

    def _cookies(self):
        return {"SESSION": self.cookie} if self.cookie else {}

    def _require_same_origin_json(self):
        self.same_origin_checks += 1
        return self.same_origin

    def _session(self):
        raise AssertionError("must not use generic API-token session fallback")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.raw = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.raw[:limit]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.responses = []
        self.session = copy.deepcopy(SESSION)
        self.module_allowed = True
        self.nav_allowed = True
        self.app = {
            "SESSION_COOKIE_NAME": "SESSION",
            "load_session": lambda token: self.session if token == "session-ok" else None,
            "has_module_permission": lambda session, module: self.module_allowed and module == "moboreels_library",
            "load_navigation_config": lambda: {},
            "navigation_item_access": lambda session, key, config: {"allowed": self.nav_allowed and key == "moboreelsLibrary"},
            "json_response": lambda handler, status, payload, **kwargs: self.responses.append((status, payload, kwargs)),
        }
        self.opener = mock.Mock()
        self.opener.open.return_value = FakeResponse({"promotion": {"status": "succeeded"}})
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(bridge, "OPENER", self.opener).start()
        mock.patch.object(bridge, "read_env_file", return_value={}).start()
        mock.patch.dict("os.environ", {"MOBOREELS_INTERNAL_TOKEN": "test-only-internal-token"}).start()

    def dispatch(self, handler, path="/api/moboreels/dramas/story-a/promotion"):
        bridge.dispatch(handler, urlsplit(path), self.app)

    def test_missing_cookie_cannot_use_api_token_or_generic_session(self):
        handler = FakeHandler(cookie=None)
        handler.headers["Authorization"] = "Bearer screenshot-token"
        self.dispatch(handler)
        self.assertEqual(self.responses[-1][0], 401)
        self.opener.open.assert_not_called()
        self.session["auth_type"] = "api_token"
        self.dispatch(FakeHandler())
        self.assertEqual(self.responses[-1][0], 401)
        self.opener.open.assert_not_called()

    def test_incomplete_cookie_identity_is_rejected(self):
        for key in ["tenant_key", "user_id"]:
            self.session = dict(SESSION, **{key: ""})
            self.dispatch(FakeHandler())
            self.assertEqual(self.responses[-1][0], 401)
        self.opener.open.assert_not_called()

    def test_module_and_navigation_permissions_are_enforced(self):
        self.module_allowed = False
        self.dispatch(FakeHandler())
        self.assertEqual(self.responses[-1][0], 403)
        self.module_allowed = True
        self.nav_allowed = False
        self.dispatch(FakeHandler())
        self.assertEqual(self.responses[-1][0], 403)
        self.opener.open.assert_not_called()

    def test_post_requires_same_origin_before_proxy_call(self):
        handler = FakeHandler()
        handler.same_origin = False
        self.dispatch(handler)
        self.assertEqual(handler.same_origin_checks, 1)
        self.opener.open.assert_not_called()

    def test_actor_and_any_nonempty_payload_are_rejected(self):
        for body in [{"actor": {"user_id": "someone-else"}}, {"star_user_id": "spoofed"}, {"trigger": "scheduled"}, [], None, "{}"]:
            with self.subTest(body=body):
                self.dispatch(FakeHandler(body=json.dumps(body).encode()))
                self.assertEqual(self.responses[-1][0], 400)
        self.opener.open.assert_not_called()

    def test_only_cookie_identity_is_forwarded_with_fixed_destination(self):
        handler = FakeHandler()
        self.dispatch(handler, "/api/moboreels/dramas/story-a/promotion?user_id=spoofed&star_user_id=bad")
        request = self.opener.open.call_args.args[0]
        envelope = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:8864/internal/dispatch")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-internal-token")
        self.assertEqual(envelope["actor"], SESSION)
        self.assertEqual(envelope["body"], {})
        self.assertEqual(envelope["path"], "/dramas/story-a/promotion")
        self.assertEqual(envelope["method"], "POST")
        self.assertEqual(self.responses[-1][0], 200)
        self.assertTrue(self.responses[-1][2]["no_store"])
        self.assertNotIn("test-only-internal-token", json.dumps(self.responses))

    def test_get_filter_query_preserves_explicit_all_status(self):
        handler = FakeHandler(method="GET", body=b"")
        self.dispatch(handler, "/api/moboreels/dramas?is_show=&page_index=2&search=%E6%B5%8B%E8%AF%95")
        envelope = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(envelope["query"], {"is_show": "", "page_index": "2", "search": "测试"})
        self.assertEqual(handler.same_origin_checks, 0)

    def test_invalid_body_framing_is_not_forwarded(self):
        for setup in ["absent", "duplicate", "negative", "too-large", "transfer", "invalid-json", "invalid-utf8"]:
            with self.subTest(setup=setup):
                handler = FakeHandler()
                if setup == "absent":
                    del handler.headers["Content-Length"]
                elif setup == "duplicate":
                    handler.headers["Content-Length"] = "2"
                elif setup == "negative":
                    handler.headers.replace_header("Content-Length", "-1")
                elif setup == "too-large":
                    handler.headers.replace_header("Content-Length", "16385")
                elif setup == "transfer":
                    handler.headers["Transfer-Encoding"] = "chunked"
                elif setup == "invalid-json":
                    handler.rfile = io.BytesIO(b"{{")
                else:
                    handler.rfile = io.BytesIO(b"\xff\xff")
                self.dispatch(handler)
                self.assertEqual(self.responses[-1][0], 400)
        self.opener.open.assert_not_called()

    def test_transport_failure_does_not_retry_creation(self):
        self.opener.open.side_effect = TimeoutError("timeout")
        self.dispatch(FakeHandler())
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.responses[-1][0], 503)
        self.assertIn("核对", self.responses[-1][1]["message"])

    def test_internal_redirect_is_disabled_and_oversized_payload_is_rejected(self):
        self.assertIsNone(bridge.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com"))
        response = FakeResponse({})
        response.raw = b"x" * (4 * 1024 * 1024 + 1)
        self.opener.open.return_value = response
        self.dispatch(FakeHandler())
        self.assertEqual(self.responses[-1][0], 503)


if __name__ == "__main__":
    unittest.main()

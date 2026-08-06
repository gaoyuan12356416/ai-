#!/usr/bin/env python3
"""Static and loopback-client contracts for the TT auto-publish proxy.

The large main app is parsed as source so this suite has no production DB,
session, or service dependencies.
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.client import (  # noqa: E402
    TT_AUTO_ADMIN_PREFIX,
    TTAutoPostAdminClientError,
    error_payload,
    parse_admin_query,
    request_admin,
)


APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(
    (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TTAutoPublishAppContractTests(unittest.TestCase):
    def test_app_parses_and_navigation_reuses_tt_posts_permission(self):
        ast.parse(APP_SOURCE)
        platform = next(item for item in NAVIGATION if item["key"] == "tiktok_platform")
        entries = {item["key"]: item for item in platform["items"]}
        self.assertEqual(entries["ttAutoPublishTemplates"]["module"], "tt_posts")
        self.assertEqual(entries["ttAutoPublishRuns"]["module"], "tt_posts")
        self.assertEqual(
            entries["ttAutoPublishTemplates"]["href"],
            "/tt-auto-publish-templates.html",
        )
        self.assertEqual(
            entries["ttAutoPublishRuns"]["href"],
            "/tt-publish-logs.html",
        )

    def test_get_proxy_routes_are_allowlisted_navigation_gated_and_no_store(self):
        start = APP_SOURCE.index(
            "        if (\n"
            "            parsed.path in {\n"
            "                TT_AUTO_ADMIN_PREFIX + \"/accounts\","
        )
        end = APP_SOURCE.index(
            '        if parsed.path in {\n            "/api/admin/tt-posts/accounts",',
            start,
        )
        route = APP_SOURCE[start:end]
        self.assertIn('TT_AUTO_ADMIN_PREFIX + "/accounts"', route)
        self.assertIn('TT_AUTO_ADMIN_PREFIX + "/templates"', route)
        self.assertIn('TT_AUTO_ADMIN_PREFIX + "/runs"', route)
        self.assertIn('r"/(?:templates|runs)/[1-9][0-9]*"', route)
        self.assertIn('"ttAutoPublishTemplates"', route)
        self.assertIn('"ttAutoPublishRuns"', route)
        self.assertIn('TT_AUTO_ADMIN_PREFIX + "/publish-logs"', APP_SOURCE)
        self.assertIn("self._require_cookie_navigation_item(navigation_key)", route)
        self.assertIn("tt_auto_posts_query_params(parsed.path, parsed.query)", route)
        self.assertIn("tt_auto_post_service_request(", route)
        self.assertIn("no_store=True", route)

    def test_post_proxy_requires_permission_same_origin_and_injects_actor(self):
        start = APP_SOURCE.index("        tt_auto_template_match = re.fullmatch(")
        end = APP_SOURCE.index(
            "        tt_post_queue_action_match = re.fullmatch(", start
        )
        route = APP_SOURCE[start:end]
        for suffix in ("copy", "enable", "disable", "preview", "run-now"):
            self.assertIn(suffix, route)
        self.assertIn(
            'self._require_cookie_navigation_item(\n                "ttAutoPublishTemplates"',
            route,
        )
        self.assertIn("self._require_same_origin_json()", route)
        self.assertIn('outbound_payload["_actor"]', route)
        self.assertIn('"user_id": str(session.get("user_id")', route)
        self.assertIn('"name": str(session.get("name")', route)
        self.assertIn("payload=outbound_payload", route)
        self.assertIn("append_audit_log(", route)
        self.assertIn("no_store=True", route)
        self.assertNotIn("access_token", route.lower())
        self.assertNotIn("internal_token", route.lower())

    def test_client_query_and_route_allowlists_fail_closed(self):
        self.assertEqual(
            parse_admin_query(
                TT_AUTO_ADMIN_PREFIX + "/templates",
                "status=enabled&q=alpha&limit=20&offset=0",
            ),
            {"status": "enabled", "q": "alpha", "limit": "20", "offset": "0"},
        )
        self.assertEqual(
            parse_admin_query(
                TT_AUTO_ADMIN_PREFIX + "/publish-logs",
                "publish_source=material_pool&trigger_type=scheduled&limit=20&offset=0",
            ),
            {
                "publish_source": "material_pool",
                "trigger_type": "scheduled",
                "limit": "20",
                "offset": "0",
            },
        )
        with self.assertRaises(TTAutoPostAdminClientError):
            parse_admin_query(
                TT_AUTO_ADMIN_PREFIX + "/templates", "access_token=secret"
            )
        with self.assertRaises(TTAutoPostAdminClientError):
            request_admin(
                "DELETE",
                TT_AUTO_ADMIN_PREFIX + "/templates/1",
                environ=self._environ(),
            )

    @mock.patch("features.tt_auto_posts.client.requests.Session")
    def test_client_rejects_documented_placeholder_bearers(self, session_factory):
        for token in (
            "replace-with-unique-random-token-at-least-32-characters",
            "must-match-etc-tt-auto-post-secrets",
        ):
            environ = self._environ()
            environ["TT_AUTO_POST_INTERNAL_TOKEN"] = token
            with self.assertRaises(TTAutoPostAdminClientError) as caught:
                request_admin(
                    "GET",
                    TT_AUTO_ADMIN_PREFIX + "/accounts",
                    environ=environ,
                )
            self.assertEqual(
                caught.exception.code, "tt_auto_post_service_not_configured"
            )
        session_factory.assert_not_called()

    @staticmethod
    def _environ():
        return {
            "TT_AUTO_POST_ADMIN_SERVICE_URL": "http://127.0.0.1:18831",
            "TT_AUTO_POST_INTERNAL_TOKEN": "i" * 48,
            "TT_AUTO_POST_ADMIN_TIMEOUT": "30",
        }

    @mock.patch("features.tt_auto_posts.client.requests.Session")
    def test_client_forwards_actor_only_to_loopback_and_keeps_token_in_header(
        self, session_factory
    ):
        session = session_factory.return_value
        session.request.return_value = FakeResponse(
            200,
            {"ok": True, "template": {"id": 1, "name": "Template A"}},
        )
        payload = {
            "name": "Template A",
            "_actor": {"user_id": "803", "name": "operator"},
        }
        result = request_admin(
            "POST",
            TT_AUTO_ADMIN_PREFIX + "/templates",
            payload=payload,
            environ=self._environ(),
        )
        self.assertTrue(result["ok"])
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(
            args[1], "http://127.0.0.1:18831" + TT_AUTO_ADMIN_PREFIX + "/templates"
        )
        self.assertEqual(kwargs["json"]["_actor"], payload["_actor"])
        self.assertNotIn("Authorization", kwargs["json"])
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer " + "i" * 48
        )
        self.assertFalse(session.trust_env)
        self.assertFalse(kwargs["allow_redirects"])
        session.close.assert_called_once_with()

    @mock.patch("features.tt_auto_posts.client.requests.Session")
    def test_sensitive_sidecar_response_is_rejected_and_secret_error_redacted(
        self, session_factory
    ):
        session_factory.return_value.request.return_value = FakeResponse(
            200,
            {"ok": True, "account": {"access_token": "very-secret"}},
        )
        with self.assertRaises(TTAutoPostAdminClientError) as caught:
            request_admin(
                "GET",
                TT_AUTO_ADMIN_PREFIX + "/accounts",
                environ=self._environ(),
            )
        self.assertEqual(caught.exception.code, "tt_auto_post_unsafe_response")

        unsafe = TTAutoPostAdminClientError(
            "upstream_error",
            "Authorization: Bearer top-secret-access-token",
            502,
        )
        status, payload = error_payload(unsafe)
        self.assertEqual(status, 502)
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("bearer ", serialized)

    def test_client_rejects_sensitive_request_fields_before_network(self):
        with mock.patch(
            "features.tt_auto_posts.client.requests.request"
        ) as request:
            with self.assertRaises(TTAutoPostAdminClientError):
                request_admin(
                    "POST",
                    TT_AUTO_ADMIN_PREFIX + "/templates",
                    payload={"_actor": {"user_id": "803"}, "accessToken": "bad"},
                    environ=self._environ(),
                )
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Static and loopback-client contracts for X automatic publishing UI proxy."""

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

from features.x_auto_posts.client import (  # noqa: E402
    X_AUTO_ADMIN_PREFIX,
    XAutoPostAdminClientError,
    error_payload,
    parse_admin_query,
    request_admin,
)


APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "features" / "x_auto_posts" / "client.py").read_text(
    encoding="utf-8"
)
NAVIGATION = json.loads(
    (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")


class XAutoPublishAppContractTests(unittest.TestCase):
    @staticmethod
    def _environ():
        return {
            "X_AUTO_POST_ADMIN_SERVICE_URL": "http://127.0.0.1:18833",
            "X_AUTO_POST_INTERNAL_TOKEN": "x" * 48,
            "X_AUTO_POST_ADMIN_TIMEOUT": "30",
        }

    def test_app_parses_and_navigation_reuses_x_accounts_permission(self):
        ast.parse(APP_SOURCE)
        platform = next(item for item in NAVIGATION if item["key"] == "x_platform")
        entries = {item["key"]: item for item in platform["items"]}
        self.assertEqual(entries["xAutoPublishTemplates"]["module"], "x_accounts")
        self.assertEqual(entries["xAutoPublishRuns"]["module"], "x_accounts")
        self.assertEqual(
            entries["xAutoPublishTemplates"]["href"],
            "/x-auto-publish-templates.html",
        )
        self.assertEqual(
            entries["xAutoPublishRuns"]["href"],
            "/x-auto-publish-runs.html",
        )
        ordered = [item["key"] for item in sorted(platform["items"], key=lambda item: item["order"])]
        self.assertLess(ordered.index("xAccountList"), ordered.index("xAutoPublishTemplates"))
        self.assertLess(ordered.index("xAutoPublishRuns"), ordered.index("xPostMaterialPool"))

    def test_get_proxy_routes_are_allowlisted_navigation_gated_and_no_store(self):
        start = APP_SOURCE.index(
            "        if (\n"
            "            parsed.path in {\n"
            "                X_AUTO_ADMIN_PREFIX + \"/accounts\","
        )
        end = APP_SOURCE.index(
            "        if (\n"
            "            parsed.path in {\n"
            "                TT_AUTO_ADMIN_PREFIX + \"/accounts\"," ,
            start,
        )
        route = APP_SOURCE[start:end]
        for suffix in ("/accounts", "/templates", "/runs"):
            self.assertIn(f'X_AUTO_ADMIN_PREFIX + "{suffix}"', route)
        self.assertIn('r"/(?:templates|runs)/[1-9][0-9]*"', route)
        self.assertIn('"xAutoPublishTemplates"', route)
        self.assertIn('"xAutoPublishRuns"', route)
        self.assertIn("self._require_cookie_navigation_item(navigation_key)", route)
        self.assertIn("x_auto_posts_query_params(parsed.path, parsed.query)", route)
        self.assertIn("x_auto_post_service_request(", route)
        self.assertIn("no_store=True", route)
        self.assertNotIn("publish-logs", route)

    def test_post_proxy_requires_permission_same_origin_actor_and_audit(self):
        start = APP_SOURCE.index("        x_auto_template_match = re.fullmatch(")
        end = APP_SOURCE.index("        tt_auto_template_match = re.fullmatch(", start)
        route = APP_SOURCE[start:end]
        for suffix in ("copy", "enable", "disable", "preview", "run-now"):
            self.assertIn(suffix, route)
        self.assertIn('"xAutoPublishTemplates"', route)
        self.assertIn("self._require_same_origin_json()", route)
        self.assertIn('outbound_payload["_actor"]', route)
        self.assertIn('"user_id": str(session.get("user_id")', route)
        self.assertIn('"name": str(session.get("name")', route)
        self.assertIn("payload=outbound_payload", route)
        self.assertIn("append_audit_log(", route)
        self.assertIn('"x_auto_publish_template"', route)
        self.assertIn("202 if action_suffix == \"run-now\" else 200", route)
        self.assertIn("no_store=True", route)
        self.assertNotIn("access_token", route.lower())
        self.assertNotIn("internal_token", route.lower())

    def test_account_refresh_proxy_is_explicit_same_origin_audited_and_no_store(self):
        start = APP_SOURCE.index("        x_auto_account_verify_match = re.fullmatch(")
        end = APP_SOURCE.index("        x_auto_template_match = re.fullmatch(", start)
        route = APP_SOURCE[start:end]
        self.assertIn(r'accounts/([1-9][0-9]*)/verify', route)
        self.assertIn('"xAutoPublishTemplates"', route)
        self.assertIn("self._require_cookie_navigation_item(", route)
        self.assertIn("self._require_same_origin_json()", route)
        self.assertIn("if request_payload != {}:", route)
        self.assertIn('x_auto_post_service_request(\n                    "POST"', route)
        self.assertIn('payload={}', route)
        self.assertIn('"refresh_x_auto_publish_account"', route)
        self.assertIn('"refresh_x_auto_publish_account_failed"', route)
        self.assertGreaterEqual(route.count("append_audit_log("), 2)
        self.assertGreaterEqual(route.count("no_store=True"), 3)
        self.assertNotIn("access_token", route.lower())
        self.assertNotIn("refresh_token", route.lower())

    def test_client_namespace_query_and_route_allowlists_fail_closed(self):
        self.assertEqual(X_AUTO_ADMIN_PREFIX, "/api/admin/x-auto-publish")
        self.assertIn('DEFAULT_SERVICE_URL = "http://127.0.0.1:18833"', CLIENT_SOURCE)
        for name in (
            "X_AUTO_POST_ADMIN_SERVICE_URL",
            "X_AUTO_POST_ADMIN_TIMEOUT",
            "X_AUTO_POST_INTERNAL_TOKEN",
        ):
            self.assertIn(name, CLIENT_SOURCE)
        self.assertEqual(
            parse_admin_query(
                X_AUTO_ADMIN_PREFIX + "/templates",
                "status=enabled&q=alpha&limit=20&offset=0",
            ),
            {"status": "enabled", "q": "alpha", "limit": "20", "offset": "0"},
        )
        self.assertEqual(
            parse_admin_query(
                X_AUTO_ADMIN_PREFIX + "/runs",
                "template_id=3&trigger_type=manual&status=completed&limit=20&offset=0",
            ),
            {
                "template_id": "3",
                "trigger_type": "manual",
                "status": "completed",
                "limit": "20",
                "offset": "0",
            },
        )
        with self.assertRaises(XAutoPostAdminClientError):
            parse_admin_query(X_AUTO_ADMIN_PREFIX + "/templates", "access_token=secret")
        with self.assertRaises(XAutoPostAdminClientError):
            parse_admin_query(X_AUTO_ADMIN_PREFIX + "/publish-logs", "limit=20")
        with self.assertRaises(XAutoPostAdminClientError):
            request_admin(
                "DELETE",
                X_AUTO_ADMIN_PREFIX + "/templates/1",
                environ=self._environ(),
            )
        with self.assertRaises(XAutoPostAdminClientError):
            request_admin(
                "GET",
                X_AUTO_ADMIN_PREFIX + "/accounts/12/verify",
                environ=self._environ(),
            )
        with self.assertRaises(XAutoPostAdminClientError):
            request_admin(
                "GET",
                X_AUTO_ADMIN_PREFIX + "/accounts",
                query={"unexpected": "1"},
                environ=self._environ(),
            )

    @mock.patch("features.x_auto_posts.client.requests.Session")
    def test_client_forwards_explicit_account_refresh_to_exact_loopback(self, session_factory):
        session = session_factory.return_value
        session.request.return_value = FakeResponse(
            200,
            {
                "ok": True,
                "account": {
                    "id": 12,
                    "status": "active",
                    "publish_approved": True,
                    "publish_eligible": True,
                },
            },
        )
        result = request_admin(
            "POST",
            X_AUTO_ADMIN_PREFIX + "/accounts/12/verify",
            payload={},
            environ=self._environ(),
        )
        self.assertTrue(result["account"]["publish_eligible"])
        args, kwargs = session.request.call_args
        self.assertEqual(
            args,
            (
                "POST",
                "http://127.0.0.1:18833"
                + X_AUTO_ADMIN_PREFIX
                + "/accounts/12/verify",
            ),
        )
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "x" * 48)
        self.assertFalse(session.trust_env)
        self.assertFalse(kwargs["allow_redirects"])
        session.close.assert_called_once_with()

    @mock.patch("features.x_auto_posts.client.requests.Session")
    def test_client_rejects_non_exact_loopback_and_placeholder_bearers(self, session_factory):
        variants = (
            {
                **self._environ(),
                "X_AUTO_POST_ADMIN_SERVICE_URL": "http://127.0.0.1:18831",
            },
            {
                **self._environ(),
                "X_AUTO_POST_INTERNAL_TOKEN": "replace-with-unique-random-token-at-least-32-characters",
            },
        )
        for environ in variants:
            with self.subTest(environ=environ):
                with self.assertRaises(XAutoPostAdminClientError) as caught:
                    request_admin(
                        "GET",
                        X_AUTO_ADMIN_PREFIX + "/accounts",
                        environ=environ,
                    )
                self.assertEqual(caught.exception.code, "x_auto_post_service_not_configured")
        session_factory.assert_not_called()

    @mock.patch("features.x_auto_posts.client.requests.Session")
    def test_client_forwards_actor_only_to_exact_loopback(self, session_factory):
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
            X_AUTO_ADMIN_PREFIX + "/templates",
            payload=payload,
            environ=self._environ(),
        )
        self.assertTrue(result["ok"])
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("POST", "http://127.0.0.1:18833" + X_AUTO_ADMIN_PREFIX + "/templates"))
        self.assertEqual(kwargs["json"]["_actor"], payload["_actor"])
        self.assertNotIn("Authorization", kwargs["json"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "x" * 48)
        self.assertFalse(session.trust_env)
        self.assertFalse(kwargs["allow_redirects"])
        session.close.assert_called_once_with()

    @mock.patch("features.x_auto_posts.client.requests.Session")
    def test_sensitive_sidecar_response_is_rejected_and_error_redacted(self, session_factory):
        session_factory.return_value.request.return_value = FakeResponse(
            200,
            {"ok": True, "account": {"access_token": "very-secret"}},
        )
        with self.assertRaises(XAutoPostAdminClientError) as caught:
            request_admin(
                "GET",
                X_AUTO_ADMIN_PREFIX + "/accounts",
                environ=self._environ(),
            )
        self.assertEqual(caught.exception.code, "x_auto_post_unsafe_response")

        unsafe = XAutoPostAdminClientError(
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
        with mock.patch("features.x_auto_posts.client.requests.Session") as session_factory:
            with self.assertRaises(XAutoPostAdminClientError):
                request_admin(
                    "POST",
                    X_AUTO_ADMIN_PREFIX + "/templates",
                    payload={"_actor": {"user_id": "803"}, "accessToken": "bad"},
                    environ=self._environ(),
                )
            session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

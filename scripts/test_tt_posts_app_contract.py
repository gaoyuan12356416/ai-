#!/usr/bin/env python3
"""TT Post AI-backend proxy contract checks without production DB or network."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
NAVIGATION = json.loads(
    (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
)


def _literal_assignment(name):
    tree = ast.parse(APP_SOURCE)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in item.targets
        )
    )
    return ast.literal_eval(node.value)


def _client_namespace():
    names = {
        "TTPostAdminClientError",
        "_tt_post_contains_sensitive_key",
        "_tt_post_safe_error_message",
        "_tt_post_public_payload",
        "_tt_post_query_params",
        "_tt_post_service_request",
        "tt_posts_error_payload",
    }
    tree = ast.parse(APP_SOURCE)
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in names
        )
    ]

    class FakeRequests:
        RequestException = RuntimeError
        request = mock.Mock()

    namespace = {
        "json": json,
        "re": re,
        "parse_qs": parse_qs,
        "urlparse": urlparse,
        "requests": FakeRequests,
        "TT_POST_ADMIN_SERVICE_URL": "http://127.0.0.1:18829",
        "TT_POST_ADMIN_INTERNAL_TOKEN": "i" * 48,
        "TT_POST_ADMIN_TIMEOUT": 360,
        "TT_POST_ADMIN_PREVIEW_TIMEOUT": 9060,
        "TT_POST_ADMIN_ROUTE_METHODS": {
            "/api/admin/tt-posts/accounts": {"GET"},
            "/api/admin/tt-posts/account-settings": {"GET", "POST"},
            "/api/admin/tt-posts/account-settings/creator-info": {"POST"},
            "/api/admin/tt-posts/account-settings/batch": {"POST"},
            "/api/admin/tt-posts/account-settings/batch/creator-info": {"POST"},
            "/api/admin/tt-posts/creator-info": {"POST"},
            "/api/admin/tt-posts/materials/preview": {"POST"},
            "/api/admin/tt-posts/material-pool": {"GET", "POST"},
            "/api/admin/tt-posts/schedule": {"GET", "POST"},
            "/api/admin/tt-posts/run-now": {"POST"},
            "/api/admin/tt-posts/queue": {"GET"},
            "/api/admin/tt-posts/events": {"GET"},
        },
        "TT_POST_SENSITIVE_KEYS": {
            "accesstoken",
            "refreshtoken",
            "authorization",
            "credential",
            "credentials",
            "claimtoken",
            "internaltoken",
            "clientsecret",
            "password",
        },
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(
        compile(ast.fix_missing_locations(module), str(APP_PATH), "exec"),
        namespace,
    )
    return namespace, FakeRequests


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TTPostsAppContractTest(unittest.TestCase):
    def test_permission_exists_and_is_default_off(self):
        modules = _literal_assignment("MODULE_PERMISSIONS")
        defaults = _literal_assignment("DEFAULT_USER_PERMISSIONS")
        self.assertEqual(modules["tt_posts"], "TikTok 社媒发布")
        self.assertIs(defaults["tt_posts"], False)
        self.assertIn(
            "ADMIN_PERMISSIONS = {key: True for key in MODULE_PERMISSIONS}",
            APP_SOURCE,
        )

    def test_tt_navigation_permission_fails_closed_for_ordinary_user(self):
        tree = ast.parse(APP_SOURCE)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "navigation_item_access"
        )
        namespace = {
            "has_module_permission": lambda session, key: (
                session.get("role") == "admin"
                or bool(session.get("permissions", {}).get(key))
            )
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[function], type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        access = namespace["navigation_item_access"]
        denied = access(
            {"role": "user", "permissions": {"tt_posts": False}},
            "ttPostPool",
            NAVIGATION,
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["error"], "permission_denied")
        self.assertEqual(denied["module"], "tt_posts")
        self.assertTrue(
            access(
                {"role": "user", "permissions": {"tt_posts": True}},
                "ttPostPool",
                NAVIGATION,
            )["allowed"]
        )
        self.assertTrue(
            access(
                {"role": "user", "permissions": {"tt_posts": True}},
                "ttAccountSettings",
                NAVIGATION,
            )["allowed"]
        )
        denied_settings = access(
            {"role": "user", "permissions": {"tt_posts": False}},
            "ttAccountSettings",
            NAVIGATION,
        )
        self.assertFalse(denied_settings["allowed"])
        self.assertEqual(denied_settings["module"], "tt_posts")
        self.assertTrue(
            access(
                {"role": "admin", "permissions": {}},
                "ttPostPool",
                NAVIGATION,
            )["allowed"]
        )

    def test_get_routes_use_navigation_gate_and_no_store(self):
        start = APP_SOURCE.index(
            '        if parsed.path in {\n'
            '            "/api/admin/tt-posts/accounts",'
        )
        end = APP_SOURCE.index(
            '        if parsed.path == "/api/x-accounts/config":',
            start,
        )
        route = APP_SOURCE[start:end]
        self.assertIn('"ttAccountSettings"', route)
        self.assertIn('"ttPostPool"', route)
        self.assertIn(
            "self._require_cookie_navigation_item(navigation_key)",
            route,
        )
        self.assertIn('"/api/admin/tt-posts/account-settings"', route)
        self.assertIn('"/api/admin/tt-posts/queue"', route)
        self.assertIn('"/api/admin/tt-posts/events"', route)
        self.assertIn('"/api/admin/tt-posts/material-pool"', route)
        self.assertIn('"/api/admin/tt-posts/schedule"', route)
        self.assertIn("_tt_post_query_params(", route)
        self.assertIn("_tt_post_service_request(", route)
        self.assertGreaterEqual(route.count("no_store=True"), 2)

    def test_post_routes_use_same_origin_audit_and_no_store(self):
        start = APP_SOURCE.index(
            '        if parsed.path in {\n'
            '            "/api/admin/tt-posts/account-settings",'
        )
        end = APP_SOURCE.index(
            "        x_post_account_verify_match = re.fullmatch(",
            start,
        )
        route = APP_SOURCE[start:end]
        self.assertIn('"ttAccountSettings"', route)
        self.assertIn('"ttPostPool"', route)
        self.assertIn(
            "self._require_cookie_navigation_item(navigation_key)",
            route,
        )
        self.assertIn("_require_same_origin_json()", route)
        self.assertIn(
            '"/api/admin/tt-posts/account-settings/creator-info"',
            route,
        )
        self.assertIn(
            '"/api/admin/tt-posts/account-settings/batch"',
            route,
        )
        self.assertIn(
            '"/api/admin/tt-posts/account-settings/batch/creator-info"',
            route,
        )
        self.assertIn('"/api/admin/tt-posts/materials/preview"', route)
        self.assertIn('"/api/admin/tt-posts/material-pool"', route)
        self.assertIn('"/api/admin/tt-posts/schedule"', route)
        self.assertIn('"/api/admin/tt-posts/run-now"', route)
        self.assertNotIn('"/api/admin/tt-posts/queue"', route)
        self.assertIn("_tt_post_service_request(", route)
        self.assertIn("append_audit_log(", route)
        self.assertIn('"save_tt_post_account_settings"', route)
        self.assertIn('"batch_save_tt_post_account_settings"', route)
        self.assertIn(
            '"batch_check_tt_post_account_settings_creator_info"',
            route,
        )
        self.assertIn(
            '"check_tt_post_account_settings_creator_info"',
            route,
        )
        self.assertIn('"source_account_ids"', route)
        self.assertIn('"saved_count"', route)
        self.assertIn('"privacy_level"', route)
        self.assertIn('"version"', route)
        self.assertNotIn('"caption_text":', route)
        self.assertGreaterEqual(route.count("no_store=True"), 2)

    def test_legacy_exact_queue_creation_is_not_publicly_writable(self):
        methods = _literal_assignment("TT_POST_ADMIN_ROUTE_METHODS")
        self.assertEqual(
            {"GET"},
            methods["/api/admin/tt-posts/queue"],
        )
        start = APP_SOURCE.index(
            '        if parsed.path in {\n'
            '            "/api/admin/tt-posts/account-settings",'
        )
        end = APP_SOURCE.index(
            "        x_post_account_verify_match = re.fullmatch(",
            start,
        )
        route = APP_SOURCE[start:end]
        self.assertNotIn(
            '"/api/admin/tt-posts/queue": "create_tt_post_queue"',
            route,
        )

    def test_queue_actions_use_dynamic_sidecar_routes_and_safe_audit(self):
        start = APP_SOURCE.index(
            "        tt_post_queue_action_match = re.fullmatch("
        )
        end = APP_SOURCE.index(
            '        if parsed.path in {\n'
            '            "/api/admin/tt-posts/account-settings",',
            start,
        )
        route = APP_SOURCE[start:end]
        self.assertIn(
            r"/api/admin/tt-posts/queue/([1-9][0-9]*)/(cancel|reconcile)",
            route,
        )
        self.assertIn(
            '_require_cookie_navigation_item("ttPostPool")',
            route,
        )
        self.assertIn("_require_same_origin_json()", route)
        self.assertIn("_tt_post_service_request(", route)
        self.assertIn('"cancel_tt_post_queue"', route)
        self.assertIn('"manual_reconcile_tt_post_queue"', route)
        self.assertIn("append_audit_log(", route)
        self.assertNotIn("claim_token", route)
        self.assertNotIn("caption", route)
        self.assertGreaterEqual(route.count("no_store=True"), 2)

    def test_mocked_service_success_preserves_account_ids_as_strings(self):
        namespace, fake_requests = _client_namespace()
        fake_requests.request.reset_mock()
        fake_requests.request.return_value = FakeResponse(
            200,
            {
                "items": [
                    {
                        "source_account_id": 700,
                        "main_account_id": 123456789012345678,
                        "external_account_id": "external-7",
                        "token_status": 2,
                    }
                ],
                "gates": {"live_enabled": False},
            },
        )
        result = namespace["_tt_post_service_request"](
            "GET",
            "/api/admin/tt-posts/accounts",
            query={},
        )
        self.assertEqual(result["items"][0]["source_account_id"], "700")
        self.assertEqual(
            result["items"][0]["main_account_id"],
            "123456789012345678",
        )
        self.assertEqual(result["items"][0]["token_status"], 2)
        call = fake_requests.request.call_args
        self.assertEqual(call.args[:2], ("GET", "http://127.0.0.1:18829/api/admin/tt-posts/accounts"))
        self.assertEqual(call.kwargs["params"], None)
        self.assertIsNone(call.kwargs["json"])
        self.assertEqual(
            call.kwargs["headers"]["Authorization"],
            "Bearer " + "i" * 48,
        )
        self.assertNotIn("Authorization", call.args[1])

    def test_mocked_service_rejects_secret_fields_in_response(self):
        namespace, fake_requests = _client_namespace()
        fake_requests.request.reset_mock()
        fake_requests.request.return_value = FakeResponse(
            200,
            {
                "items": [
                    {
                        "source_account_id": "700",
                        "access_token": "must-not-cross-boundary",
                    }
                ]
            },
        )
        with self.assertRaises(namespace["TTPostAdminClientError"]) as caught:
            namespace["_tt_post_service_request"](
                "GET",
                "/api/admin/tt-posts/accounts",
            )
        self.assertEqual(caught.exception.code, "tt_post_unsafe_response")
        self.assertNotIn("must-not-cross-boundary", str(caught.exception))

    def test_mocked_cancel_and_manual_reconcile_use_exact_dynamic_paths(self):
        namespace, fake_requests = _client_namespace()
        fake_requests.request.reset_mock()
        fake_requests.request.side_effect = [
            FakeResponse(
                200,
                {"item": {"queue_id": 81, "status": "cancelled"}},
            ),
            FakeResponse(
                200,
                {
                    "item": {
                        "queue_id": 82,
                        "status": "reconciling",
                    },
                    "remote_status": "processing_download",
                },
            ),
        ]
        canceled = namespace["_tt_post_service_request"](
            "POST",
            "/api/admin/tt-posts/queue/81/cancel",
            payload={"reason": "operator canceled"},
        )
        reconciled = namespace["_tt_post_service_request"](
            "POST",
            "/api/admin/tt-posts/queue/82/reconcile",
            payload={},
        )
        self.assertEqual(canceled["item"]["queue_id"], 81)
        self.assertEqual(reconciled["item"]["queue_id"], 82)
        calls = fake_requests.request.call_args_list
        self.assertTrue(calls[0].args[1].endswith("/queue/81/cancel"))
        self.assertTrue(calls[1].args[1].endswith("/queue/82/reconcile"))
        self.assertEqual(calls[0].kwargs["json"], {"reason": "operator canceled"})
        self.assertEqual(calls[1].kwargs["json"], {})
        self.assertNotIn("claim_token", json.dumps(calls[1].kwargs))

    def test_mocked_service_redacts_credential_like_error_message(self):
        namespace, fake_requests = _client_namespace()
        fake_requests.request.reset_mock()
        fake_requests.request.return_value = FakeResponse(
            502,
            {
                "code": "tt_upstream_error",
                "message": "Authorization: Bearer should-never-appear",
            },
        )
        with self.assertRaises(namespace["TTPostAdminClientError"]) as caught:
            namespace["_tt_post_service_request"](
                "POST",
                "/api/admin/tt-posts/creator-info",
                payload={"source_account_id": "700"},
            )
        self.assertEqual(caught.exception.code, "tt_upstream_error")
        self.assertEqual(str(caught.exception), "TT Post服务请求失败")
        self.assertNotIn("should-never-appear", str(caught.exception))

    def test_material_preview_uses_long_timeout_without_widening_other_routes(self):
        namespace, fake_requests = _client_namespace()
        fake_requests.request.reset_mock()
        fake_requests.request.side_effect = [
            FakeResponse(200, {"item": {"material_id": "4665764"}}),
            FakeResponse(200, {"items": []}),
        ]
        namespace["_tt_post_service_request"](
            "POST",
            "/api/admin/tt-posts/materials/preview",
            payload={"material_id": "4665764"},
        )
        namespace["_tt_post_service_request"](
            "GET",
            "/api/admin/tt-posts/accounts",
        )
        calls = fake_requests.request.call_args_list
        self.assertEqual(calls[0].kwargs["timeout"], 9060)
        self.assertEqual(calls[1].kwargs["timeout"], 360)

    def test_query_parser_rejects_duplicates_and_secret_names(self):
        namespace, _fake_requests = _client_namespace()
        parser = namespace["_tt_post_query_params"]
        error_type = namespace["TTPostAdminClientError"]
        self.assertEqual(
            parser(
                "page=2&source_account_id=700",
                {"page", "source_account_id"},
            ),
            {"page": "2", "source_account_id": "700"},
        )
        with self.assertRaises(error_type):
            parser("queue_id=1&queue_id=2", {"queue_id"})
        with self.assertRaises(error_type):
            parser("access_token=secret", {"queue_id"})


if __name__ == "__main__":
    unittest.main()

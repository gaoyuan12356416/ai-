#!/usr/bin/env python3
"""Static regression checks for the AI backend X-account route boundaries.

The production backend is a large composite module with many external runtime
dependencies, so these checks intentionally parse its source without importing
or starting unrelated services.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
NGINX_SOURCE = (ROOT / "deploy" / "nginx-x-oauth.conf").read_text(encoding="utf-8")
NAVIGATION_SOURCE = (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
QUICK_NAV_SOURCE = (ROOT / "static" / "quick-nav.js").read_text(encoding="utf-8")
X_POST_LOGS_SOURCE = (ROOT / "static" / "x-post-logs.html").read_text(encoding="utf-8")


def source_between(start, end):
    start_at = APP_SOURCE.index(start)
    end_at = APP_SOURCE.index(end, start_at + len(start))
    return APP_SOURCE[start_at:end_at]


class XAccountsAppContractTest(unittest.TestCase):
    def test_actor_includes_tenant_and_user_identity(self):
        tree = ast.parse(APP_SOURCE)
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "x_accounts_actor"
        )
        module = ast.Module(body=[function], type_ignores=[])
        namespace = {}
        exec(compile(ast.fix_missing_locations(module), str(APP_PATH), "exec"), namespace)
        actor = namespace["x_accounts_actor"](
            {
                "tenant_key": "tenant-a",
                "user_id": "user-a",
                "name": "Owner",
                "email": "owner@example.com",
                "role": "admin",
            }
        )
        self.assertEqual(actor["tenant_key"], "tenant-a")
        self.assertEqual(actor["user_id"], "user-a")
        self.assertEqual(actor["role"], "admin")

    def test_owner_and_admin_list_routes_use_distinct_scopes(self):
        owner = source_between(
            'if parsed.path == "/api/x-accounts":',
            'if parsed.path == "/api/admin/x-accounts":',
        )
        admin = source_between(
            'if parsed.path == "/api/admin/x-accounts":',
            'if parsed.path == "/api/drama-material/products":',
        )
        self.assertIn('_require_cookie_module("x_accounts")', owner)
        self.assertIn('query_x_authorized_accounts(x_accounts_actor(session), scope="mine")', owner)
        self.assertIn("_require_cookie_admin()", admin)
        self.assertIn('query_x_authorized_accounts(x_accounts_actor(session), scope="all")', admin)

    def test_verify_and_logout_pass_server_session_actor(self):
        post_routes = source_between(
            'if parsed.path == "/api/x-accounts/authorize":',
            'if parsed.path == "/api/admin/users/role":',
        )
        self.assertIn('verify_x_account(account_id, x_accounts_actor(session), scope="mine")', post_routes)
        self.assertIn('verify_x_account(account_id, x_accounts_actor(session), scope="all")', post_routes)
        self.assertIn("logout_x_account(account_id, x_accounts_actor(session))", post_routes)
        self.assertIn("_require_cookie_admin()", post_routes)
        self.assertIn('_require_cookie_module("x_accounts")', post_routes)

    def test_admin_gate_explicitly_rejects_api_tokens(self):
        admin_gate = source_between(
            "def _require_cookie_admin(self):",
            "def _require_same_origin_json(self):",
        )
        self.assertIn('session.get("auth_type") == "api_token"', admin_gate)
        self.assertIn('session.get("role") == "admin"', admin_gate)
        self.assertIn('"cookie_auth_required"', admin_gate)

    def test_x_routes_and_nginx_force_no_store(self):
        get_routes = source_between(
            'if parsed.path == "/api/x-accounts/config":',
            'if parsed.path == "/api/drama-material/products":',
        )
        post_routes = source_between(
            'if parsed.path == "/api/x-accounts/authorize":',
            'if parsed.path == "/api/admin/users/role":',
        )
        self.assertGreaterEqual(get_routes.count("no_store=True"), 6)
        self.assertGreaterEqual(post_routes.count("no_store=True"), 8)
        self.assertIn("location = /api/admin/x-accounts", NGINX_SOURCE)
        self.assertIn("location ^~ /api/admin/x-accounts/", NGINX_SOURCE)
        self.assertIn("location ^~ /api/admin/x-posts/", NGINX_SOURCE)
        self.assertGreaterEqual(NGINX_SOURCE.count('add_header Cache-Control "no-store" always;'), 5)

    def test_x_post_admin_routes_are_cookie_admin_only_and_no_store(self):
        routes = source_between(
            'if parsed.path == "/api/admin/x-posts/logs":',
            'if parsed.path == "/api/drama-material/products":',
        )
        self.assertEqual(routes.count("_require_cookie_admin()"), 2)
        self.assertEqual(routes.count('"actor": x_accounts_actor(self._session())'), 2)
        self.assertEqual(routes.count('"scope": "all"'), 2)
        self.assertIn("query_x_post_logs(params)", routes)
        self.assertIn("query_x_post_runs(params)", routes)
        self.assertGreaterEqual(routes.count("no_store=True"), 6)

    def test_x_post_query_parameter_validation(self):
        tree = ast.parse(APP_SOURCE)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "x_post_admin_query_params"
        )
        namespace = {"parse_qs": parse_qs, "re": re}
        exec(
            compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(APP_PATH), "exec"),
            namespace,
        )
        parse = namespace["x_post_admin_query_params"]
        self.assertEqual(
            parse("page=2&page_size=500&run_date=2026-07-23&material_id=5221348"),
            {"page": 2, "page_size": 100, "run_date": "2026-07-23", "material_id": "5221348"},
        )
        with self.assertRaises(ValueError):
            parse("access_token=secret")
        with self.assertRaises(ValueError):
            parse("run_date=23-07-2026")
        with self.assertRaises(ValueError):
            parse("account_id=1%20OR%201=1")

    def test_x_post_log_navigation_and_dom_link_allowlists(self):
        self.assertIn('"key": "xPostLogs"', NAVIGATION_SOURCE)
        self.assertIn('xPostLogs: "/x-post-logs.html"', QUICK_NAV_SOURCE)
        self.assertIn('activeKey:"xPostLogs"', X_POST_LOGS_SOURCE)
        self.assertIn('url.hostname === "x.com"', X_POST_LOGS_SOURCE)
        self.assertIn('url.hostname === "ai.yingliangads.com"', X_POST_LOGS_SOURCE)
        self.assertIn("replaceChildren()", X_POST_LOGS_SOURCE)
        self.assertNotIn("access_token", X_POST_LOGS_SOURCE.lower())
        self.assertNotIn("refresh_token", X_POST_LOGS_SOURCE.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

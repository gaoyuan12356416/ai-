#!/usr/bin/env python3
"""Static regression checks for the AI backend X-account route boundaries.

The production backend is a large composite module with many external runtime
dependencies, so these checks intentionally parse its source without importing
or starting unrelated services.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
NGINX_SOURCE = (ROOT / "deploy" / "nginx-x-oauth.conf").read_text(encoding="utf-8")


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
        self.assertGreaterEqual(NGINX_SOURCE.count('add_header Cache-Control "no-store" always;'), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)

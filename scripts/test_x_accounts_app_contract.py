#!/usr/bin/env python3
"""Static regression checks for the AI backend X-account route boundaries.

The production backend is a large composite module with many external runtime
dependencies, so these checks intentionally parse its source without importing
or starting unrelated services.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.selector import material_key, normalize_material_url  # noqa: E402


APP_PATH = ROOT / "app.py"
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")
NGINX_SOURCE = (ROOT / "deploy" / "nginx-x-oauth.conf").read_text(encoding="utf-8")
NAVIGATION_SOURCE = (ROOT / "static" / "navigation.json").read_text(encoding="utf-8")
QUICK_NAV_SOURCE = (ROOT / "static" / "quick-nav.js").read_text(encoding="utf-8")
X_ACCOUNT_LIST_SOURCE = (ROOT / "static" / "x-account-list.html").read_text(
    encoding="utf-8"
)
X_POST_LOGS_SOURCE = (ROOT / "static" / "x-post-logs.html").read_text(encoding="utf-8")
X_POST_POOL_SOURCE = (ROOT / "static" / "x-post-material-pool.html").read_text(
    encoding="utf-8"
)
X_ACCOUNTS_CLIENT_SOURCE = (
    ROOT / "features" / "x_accounts" / "client.py"
).read_text(encoding="utf-8")
X_ACCOUNTS_SIDECAR_SOURCE = (
    ROOT / "features" / "x_accounts" / "oauth_service.py"
).read_text(encoding="utf-8")
X_POST_DAILY_SERVICE_SOURCE = (
    ROOT / "deploy" / "x-post-daily.service"
).read_text(encoding="utf-8")
X_POST_DAILY_TIMER_SOURCE = (
    ROOT / "deploy" / "x-post-daily.timer"
).read_text(encoding="utf-8")


def source_between(start, end):
    start_at = APP_SOURCE.index(start)
    end_at = APP_SOURCE.index(end, start_at + len(start))
    return APP_SOURCE[start_at:end_at]


class XAccountsAppContractTest(unittest.TestCase):
    def test_dynamic_daily_batch_has_extended_timeout_and_generic_timer_label(self):
        self.assertIn("TimeoutStartSec=360min", X_POST_DAILY_SERVICE_SOURCE)
        self.assertIn(
            "Description=Publish configured daily Dramawave X posts",
            X_POST_DAILY_TIMER_SOURCE,
        )
        self.assertNotIn("Publish three daily", X_POST_DAILY_TIMER_SOURCE)

    def test_drama_pool_sidecar_forwards_account_affinity_scope(self):
        self.assertIn(
            'account_ids=payload.get("account_ids")',
            X_ACCOUNTS_SIDECAR_SOURCE,
        )

    def test_x_account_list_displays_daily_auto_publish_status_in_twelve_columns(self):
        self.assertIn(
            '<th>X账号</th><th class="auto-publish-col">自动发布 Post</th>',
            X_ACCOUNT_LIST_SOURCE,
        )
        self.assertRegex(
            X_ACCOUNT_LIST_SOURCE,
            r'\$\{booleanChips\(item\)\}</div></div></td>\s*'
            r'<td class="auto-publish-col">',
        )
        self.assertIn(
            "item.daily_auto_publish_configured === true",
            X_ACCOUNT_LIST_SOURCE,
        )
        self.assertIn("已配置", X_ACCOUNT_LIST_SOURCE)
        self.assertIn("未配置", X_ACCOUNT_LIST_SOURCE)
        self.assertGreaterEqual(
            X_ACCOUNT_LIST_SOURCE.count('colspan="12"'),
            4,
        )
        self.assertNotIn('colspan="11"', X_ACCOUNT_LIST_SOURCE)
        self.assertNotIn("expected_count || 3", X_POST_LOGS_SOURCE)
        self.assertNotIn('id="latestPublished">0 / 3', X_POST_LOGS_SOURCE)
        self.assertIn('item.batch_kind === "catchup"', X_POST_LOGS_SOURCE)
        self.assertIn("`补发批次 ${item.catchup_run_id}`", X_POST_LOGS_SOURCE)
        self.assertIn(
            '"href": "/x-account-list.html?v=20260727catchup1"',
            NAVIGATION_SOURCE,
        )

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

    def test_post_page_auto_verification_preserves_actor_identity(self):
        tree = ast.parse(APP_SOURCE)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "x_accounts_actor",
                "x_post_drama_verification_actor",
            }
        ]
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=functions, type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        actor = namespace["x_post_drama_verification_actor"](
            {
                "tenant_key": "tenant-a",
                "user_id": "user-a",
                "name": "Operator",
                "email": "operator@example.com",
                "role": "user",
            }
        )
        self.assertEqual(actor["tenant_key"], "tenant-a")
        self.assertEqual(actor["user_id"], "user-a")
        self.assertEqual(actor["name"], "Operator")
        self.assertEqual(actor["email"], "operator@example.com")
        self.assertEqual(actor["role"], "admin")

    def test_rate_limit_error_survives_main_backend_mapping(self):
        tree = ast.parse(APP_SOURCE)
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "X_ACCOUNTS_ERROR_META"
                for target in node.targets
            ):
                nodes.append(node)
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "x_accounts_error_payload"
            ):
                nodes.append(node)
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=nodes, type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        error = type("RateLimitError", (), {"code": "x_post_rate_limited"})()
        status, payload = namespace["x_accounts_error_payload"](error)
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"], "x_post_rate_limited")

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
            "def _require_cookie_navigation_item(self, item_key):",
        )
        self.assertIn('session.get("auth_type") == "api_token"', admin_gate)
        self.assertIn('session.get("role") == "admin"', admin_gate)
        self.assertIn('"cookie_auth_required"', admin_gate)

    def test_navigation_item_access_matches_quick_nav_rules(self):
        tree = ast.parse(APP_SOURCE)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "navigation_item_access"
        )
        namespace = {
            "has_module_permission": lambda session, module: (
                session.get("role") == "admin"
                or bool((session.get("permissions") or {}).get(module))
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
        user = {
            "role": "user",
            "permissions": {"x_accounts": True, "group_access": True},
        }
        config = [
            {
                "key": "x",
                "module": "group_access",
                "items": [
                    {
                        "key": "xPostMaterialPool",
                        "module": "x_accounts",
                        "enabled": True,
                    }
                ],
            }
        ]
        self.assertTrue(access(user, "xPostMaterialPool", config)["allowed"])

        missing_item_permission = {
            **user,
            "permissions": {"x_accounts": False, "group_access": True},
        }
        denied = access(
            missing_item_permission,
            "xPostMaterialPool",
            config,
        )
        self.assertEqual(denied["error"], "permission_denied")
        self.assertEqual(denied["module"], "x_accounts")

        admin_only_config = [
            {
                **config[0],
                "items": [{**config[0]["items"][0], "adminOnly": True}],
            }
        ]
        self.assertEqual(
            access(user, "xPostMaterialPool", admin_only_config)["error"],
            "admin_required",
        )
        self.assertTrue(
            access(
                {"role": "admin", "permissions": {}},
                "xPostMaterialPool",
                admin_only_config,
            )["allowed"]
        )
        disabled_config = [
            {
                **config[0],
                "items": [{**config[0]["items"][0], "enabled": False}],
            }
        ]
        self.assertEqual(
            access(user, "xPostMaterialPool", disabled_config)["error"],
            "navigation_item_unavailable",
        )
        self.assertEqual(
            access(user, "missing", config)["error"],
            "navigation_item_unavailable",
        )

    def test_navigation_item_gate_is_cookie_only_and_fail_closed(self):
        gate = source_between(
            "def _require_cookie_navigation_item(self, item_key):",
            "def _require_same_origin_json(self):",
        )
        self.assertIn('session.get("auth_type") == "api_token"', gate)
        self.assertIn("load_navigation_config()", gate)
        self.assertIn("navigation_item_access(", gate)
        self.assertIn('"navigation_config_unavailable"', gate)
        self.assertIn("no_store=True", gate)

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
        account_page = re.search(
            r"location = /x-account-list\.html \{(?P<body>.*?)\n\}",
            NGINX_SOURCE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(account_page)
        self.assertIn(
            'add_header Cache-Control "no-cache, no-store, must-revalidate" always;',
            account_page.group("body"),
        )
        for page_name in (
            "x-post-material-pool",
            "x-post-drama-pool",
        ):
            page = re.search(
                r"location = /%s\.html \{(?P<body>.*?)\n\}"
                % re.escape(page_name),
                NGINX_SOURCE,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(page)
            self.assertIn(
                'add_header Cache-Control "no-cache, no-store, must-revalidate" always;',
                page.group("body"),
            )

    def test_x_post_routes_use_scoped_cookie_gates_and_no_store(self):
        routes = source_between(
            'if parsed.path == "/api/admin/x-posts/logs":',
            'if parsed.path == "/api/drama-material/products":',
        )
        self.assertEqual(routes.count("_require_cookie_admin()"), 2)
        self.assertEqual(
            routes.count(
                '_require_cookie_navigation_item("xPostMaterialPool")'
            ),
            4,
        )
        self.assertEqual(routes.count('"actor": x_accounts_actor(self._session())'), 5)
        self.assertEqual(routes.count('"scope": "all"'), 5)
        self.assertIn("query_x_post_logs(params)", routes)
        self.assertIn("query_x_post_runs(params)", routes)
        self.assertIn("query_x_post_material_pool(", routes)
        self.assertIn("x_post_enrich_material_pool_preview_urls(result)", routes)
        self.assertGreaterEqual(routes.count("no_store=True"), 6)

    def test_x_post_material_preview_uses_navigation_gate_pool_scope_and_no_store(self):
        route = source_between(
            'if parsed.path == "/api/admin/x-posts/material-pool/preview":',
            'if parsed.path == "/api/admin/x-posts/material-pool":',
        )
        self.assertIn(
            '_require_cookie_navigation_item("xPostMaterialPool")',
            route,
        )
        self.assertIn("query_x_post_material_pool(", route)
        self.assertIn('navigation_item="xPostMaterialPool"', route)
        self.assertIn('"material_id": material_id', route)
        self.assertIn('"actor": x_accounts_actor(self._session())', route)
        self.assertIn('"scope": "all"', route)
        self.assertIn("x_post_material_preview_location(material_id)", route)
        self.assertIn('self.send_header("Cache-Control", "no-store")', route)
        self.assertIn('self.send_header("Referrer-Policy", "no-referrer")', route)
        self.assertIn("no_store=True", route)
        self.assertNotIn("append_audit_log(", route)

    def test_x_post_material_preview_location_is_https_and_injection_safe(self):
        tree = ast.parse(APP_SOURCE)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "x_post_normalize_material_ids",
                "x_post_material_preview_urls",
                "x_post_material_preview_location",
            }
        ]
        namespace = {
            "DB_NAME": "kunlunads_dev",
            "re": re,
            "urlparse": urlparse,
            "x_post_material_key": material_key,
            "x_post_normalize_material_url": normalize_material_url,
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=functions, type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        preview_location = namespace["x_post_material_preview_location"]
        queries = []

        def valid_loader(query):
            queries.append(query)
            return [["5503209", "https://media.example.test/material.mp4"]]

        self.assertEqual(
            preview_location("5503209", valid_loader),
            "https://media.example.test/material.mp4",
        )
        self.assertEqual(
            queries,
            [
                "SELECT CAST(id AS CHAR),url FROM "
                "`kunlunads_dev`.ads_custom_source "
                "WHERE id IN (5503209) ORDER BY id"
            ],
        )
        for invalid in ("", "0", "-1", "1 OR 1=1", "1%20OR%201=1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                preview_location(invalid, valid_loader)
        with self.assertRaises(LookupError):
            preview_location("5503209", lambda _query: [])
        self.assertEqual(
            preview_location(
                "5503209",
                lambda _query: [["5503209", "http://media.example.test/a.mp4"]],
            ),
            "https://media.example.test/a.mp4",
        )
        with self.assertRaises(LookupError):
            preview_location(
                "5503209",
                lambda _query: [["5503209", "ftp://media.example.test/a.mp4"]],
            )
        with self.assertRaises(LookupError):
            preview_location(
                "5503209",
                lambda _query: [["5503209", "https://user:pass@example.test/a.mp4"]],
            )
        with self.assertRaises(LookupError):
            preview_location(
                "5503209",
                lambda _query: [["5503209", "https://example.test/a.mp4\r\nX-Test: 1"]],
            )
        preview_urls = namespace["x_post_material_preview_urls"]
        normalize_material_ids = namespace["x_post_normalize_material_ids"]
        self.assertEqual(
            normalize_material_ids(["005503209", "5503209", "11761405635"]),
            ["5503209", "11761405635"],
        )
        self.assertEqual(
            preview_urls(
                ["5503209", "11761405635"],
                lambda _query: [
                    ["5503209", "https://media.example.test/source.mp4"],
                ],
            ),
            {
                "5503209": "https://media.example.test/source.mp4",
                "11761405635": "",
            },
        )

    def test_x_post_initial_material_checks_reuse_selector_and_fail_closed(self):
        tree = ast.parse(APP_SOURCE)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "x_post_normalize_material_ids",
                "x_post_initial_material_checks",
            }
        ]
        namespace = {
            "DB_NAME": "kunlunads_dev",
            "MYSQL_HOST": "readonly.example.test",
            "MYSQL_PORT": "63350",
            "MYSQL_USER": "reader",
            "MYSQL_PASSWORD": "secret",
            "X_POST_POOL_VALIDATION_MESSAGES": {
                "material_not_found_or_ineligible": "素材不可用",
            },
            "datetime": datetime,
            "timezone": timezone,
            "re": re,
            "x_post_material_key": material_key,
            "x_post_previous_source_date": lambda _now=None: "2026-07-22",
            "connect_x_post_read_only": lambda **_kwargs: None,
            "select_x_post_pool_candidates": None,
        }
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=functions, type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        validate = namespace["x_post_initial_material_checks"]
        captured = {}

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        connection = Connection()

        def loader(_connection, pool_items, source_date, limit, schema):
            captured.update(
                {
                    "pool_items": pool_items,
                    "source_date": source_date,
                    "limit": limit,
                    "schema": schema,
                }
            )
            return (
                [{"pool_item_id": 1, "material_id": "5503209"}],
                [
                    {
                        "pool_item_id": 2,
                        "material_id": "11761405635",
                        "error_code": "material_not_found_or_ineligible",
                        "error_message": "missing",
                    }
                ],
            )

        checks = validate(
            ["5503209", "11761405635"],
            connection_factory=lambda: connection,
            candidate_loader=loader,
            now=datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            checks,
            [
                {
                    "material_id": "5503209",
                    "error_code": "",
                    "error_message": "",
                },
                {
                    "material_id": "11761405635",
                    "error_code": "material_not_found_or_ineligible",
                    "error_message": "素材不可用",
                },
            ],
        )
        self.assertEqual(captured["source_date"], "2026-07-22")
        self.assertEqual(captured["limit"], 2)
        self.assertEqual(captured["schema"], "kunlunads_dev")
        self.assertTrue(connection.closed)
        failed = validate(
            ["5503209"],
            connection_factory=lambda: (_ for _ in ()).throw(RuntimeError("down")),
            candidate_loader=loader,
        )
        self.assertEqual(
            failed[0]["error_code"],
            "material_validation_unavailable",
        )

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

        pool_function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "x_post_pool_query_params"
        )
        pool_namespace = {"parse_qs": parse_qs, "re": re}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[pool_function], type_ignores=[])
                ),
                str(APP_PATH),
                "exec",
            ),
            pool_namespace,
        )
        parse_pool = pool_namespace["x_post_pool_query_params"]
        self.assertEqual(
            parse_pool(
                "page=2&page_size=500&status=unpublished"
                "&availability=validation_failed&material_id=5221348"
            ),
            {
                "page": 2,
                "page_size": 100,
                "status": "unpublished",
                "availability": "validation_failed",
                "material_id": "5221348",
            },
        )
        with self.assertRaises(ValueError):
            parse_pool("access_token=secret")
        with self.assertRaises(ValueError):
            parse_pool("availability=queued%20OR%201=1")
        with self.assertRaises(ValueError):
            parse_pool("material_id=0")

    def test_x_post_pool_mutations_use_navigation_gate_same_origin_and_no_store(self):
        post_route = source_between(
            'if parsed.path == "/api/admin/x-posts/material-pool":',
            'if parsed.path == "/api/x-accounts/authorize":',
        )
        self.assertIn(
            '_require_cookie_navigation_item("xPostMaterialPool")',
            post_route,
        )
        self.assertIn("_require_same_origin_json()", post_route)
        self.assertIn("x_post_initial_material_checks(material_ids)", post_route)
        self.assertIn("add_x_post_material_pool(", post_route)
        self.assertIn('navigation_item="xPostMaterialPool"', post_route)
        self.assertIn("validation_checks=validation_checks", post_route)
        self.assertIn("append_audit_log(", post_route)
        self.assertIn("no_store=True", post_route)

        delete_route = source_between(
            "x_pool_delete_match = re.fullmatch(",
            'if parsed.path == "/api/ad-control/v3"',
        )
        self.assertIn(
            '_require_cookie_navigation_item("xPostMaterialPool")',
            delete_route,
        )
        self.assertIn("_require_same_origin_json()", delete_route)
        self.assertIn("delete_x_post_material_pool(", delete_route)
        self.assertIn('navigation_item="xPostMaterialPool"', delete_route)
        self.assertIn("append_audit_log(", delete_route)
        self.assertIn("no_store=True", delete_route)

    def test_x_post_drama_batch_delete_keeps_navigation_audit_and_sidecar_boundary(self):
        route = source_between(
            'if parsed.path == "/api/admin/x-posts/drama-pool/batch-delete":',
            'if parsed.path == "/api/admin/x-posts/drama-pool":',
        )
        self.assertIn(
            '_require_cookie_navigation_item("xPostDramaPool")',
            route,
        )
        self.assertIn("_require_same_origin_json()", route)
        self.assertIn("batch_delete_x_post_drama_pool(", route)
        self.assertIn('navigation_item="xPostDramaPool"', route)
        self.assertIn("batch_delete_x_post_drama_pool_failed", route)
        self.assertIn("audit_recorded", route)
        self.assertIn("no_store=True", route)

        self.assertIn(
            "def batch_delete_x_post_drama_pool(",
            X_ACCOUNTS_CLIENT_SOURCE,
        )
        self.assertIn(
            '"/internal/posts/drama-pool/batch-delete"',
            X_ACCOUNTS_CLIENT_SOURCE,
        )
        self.assertIn(
            'parsed.path == "/internal/posts/drama-pool/batch-delete"',
            X_ACCOUNTS_SIDECAR_SOURCE,
        )
        self.assertIn(
            "batch_delete_post_drama_pool_request(payload)",
            X_ACCOUNTS_SIDECAR_SOURCE,
        )

    def test_x_post_account_auto_verify_uses_navigation_gate_and_safe_scope(self):
        route = source_between(
            "x_post_account_verify_match = re.fullmatch(",
            "x_post_schedule_routes = {",
        )
        self.assertIn("x-posts/drama-pool", route)
        self.assertIn("_require_cookie_navigation_item(navigation_item)", route)
        self.assertIn("_require_same_origin_json()", route)
        self.assertIn(
            "x_post_drama_verification_actor(session)",
            route,
        )
        self.assertIn('scope="all"', route)
        self.assertIn("verify_x_account(", route)
        self.assertIn("only_refresh_required=True", route)
        self.assertIn("preserve_transient_status=True", route)
        self.assertIn("auto_verify_x_post_account", route)
        self.assertIn("auto_verify_x_post_account_failed", route)
        self.assertIn("no_store=True", route)

    def test_x_post_navigation_and_dom_link_allowlists(self):
        self.assertIn('"key": "xPostLogs"', NAVIGATION_SOURCE)
        self.assertIn('"key": "xPostMaterialPool"', NAVIGATION_SOURCE)
        self.assertIn('xPostLogs: "/x-post-logs.html"', QUICK_NAV_SOURCE)
        self.assertIn('xPostMaterialPool: "/x-post-material-pool.html"', QUICK_NAV_SOURCE)
        self.assertIn('activeKey:"xPostLogs"', X_POST_LOGS_SOURCE)
        self.assertIn('activeKey: "xPostMaterialPool"', X_POST_POOL_SOURCE)
        self.assertIn('url.hostname === "x.com"', X_POST_LOGS_SOURCE)
        self.assertIn('url.hostname === "ai.yingliangads.com"', X_POST_LOGS_SOURCE)
        self.assertIn('url.hostname === "x.com"', X_POST_POOL_SOURCE)
        self.assertIn("<th>素材预览</th>", X_POST_POOL_SOURCE)
        self.assertIn("<th>Post 预览</th>", X_POST_POOL_SOURCE)
        self.assertIn("safeMaterialUrl(item.material_preview_url)", X_POST_POOL_SOURCE)
        self.assertIn("result.skipped_count", X_POST_POOL_SOURCE)
        self.assertIn("result.already_in_pool_count", X_POST_POOL_SOURCE)
        self.assertIn("result.already_used_count", X_POST_POOL_SOURCE)
        self.assertNotIn(
            "/api/admin/x-posts/material-pool/preview?material_id=",
            X_POST_POOL_SOURCE,
        )
        self.assertIn('validation_failed: "不可用"', X_POST_POOL_SOURCE)
        self.assertIn('setText(cell, "无法预览")', X_POST_POOL_SOURCE)
        self.assertIn('fetch("/navigation.json"', X_POST_POOL_SOURCE)
        self.assertIn('cache: "no-store"', X_POST_POOL_SOURCE)
        self.assertIn(
            'navigationAllows(state.auth, navigationConfig, "xPostMaterialPool")',
            X_POST_POOL_SOURCE,
        )
        self.assertIn('id="permissionGate"', X_POST_POOL_SOURCE)
        self.assertNotIn("if (!user.is_admin)", X_POST_POOL_SOURCE)
        self.assertNotIn('id="adminGate"', X_POST_POOL_SOURCE)
        self.assertIn('link.rel = "noopener noreferrer"', X_POST_POOL_SOURCE)
        self.assertIn("cell.colSpan = 10", X_POST_POOL_SOURCE)
        self.assertIn("replaceChildren()", X_POST_LOGS_SOURCE)
        self.assertIn("replaceChildren()", X_POST_POOL_SOURCE)
        self.assertNotIn("access_token", X_POST_LOGS_SOURCE.lower())
        self.assertNotIn("refresh_token", X_POST_LOGS_SOURCE.lower())
        self.assertNotIn("access_token", X_POST_POOL_SOURCE.lower())
        self.assertNotIn("refresh_token", X_POST_POOL_SOURCE.lower())
        self.assertNotIn("innerhtml", X_POST_POOL_SOURCE.lower())
        self.assertNotIn("\ufffd", X_POST_POOL_SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

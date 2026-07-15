import json
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app


class AdControlLargeAccountPoolTest(unittest.TestCase):
    OWNER_USER_ID = "large-pool-owner"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_job_db_path = app.JOB_DB_PATH
        app.JOB_DB_PATH = os.path.join(self.temp_dir.name, "jobs.sqlite3")
        app.AD_CONTROL_ACCOUNT_LIST_CACHE.clear()
        app.AD_CONTROL_DEFAULT_USER_CACHE.clear()
        app.AD_CONTROL_TOKEN_CACHE.clear()
        app.ensure_ad_control_tables()

    def tearDown(self):
        app.JOB_DB_PATH = self.original_job_db_path
        app.AD_CONTROL_ACCOUNT_LIST_CACHE.clear()
        app.AD_CONTROL_DEFAULT_USER_CACHE.clear()
        app.AD_CONTROL_TOKEN_CACHE.clear()
        self.temp_dir.cleanup()

    @property
    def session(self):
        return {"user_id": self.OWNER_USER_ID}

    def create_binding(self, account_count=245):
        account_ids = [str(1000000000000000 + index) for index in range(account_count)]
        app.save_ad_control_account_group(
            {
                "group_id": "pool",
                "product": "dramawave",
                "name": "large pool",
                "account_ids": account_ids,
            },
            self.session,
        )
        rules = [{
            "name": "pause",
            "action": "pause",
            "enabled": True,
            "conditions": [{"field": "spend", "op": "gte", "value": 10}],
        }]
        app.save_ad_control_rule_set(
            {
                "rule_set_id": "rules",
                "product": "dramawave",
                "name": "rules",
                "rules": rules,
                "default_window": {"type": "since_start", "hours": 24},
            },
            self.session,
        )
        app.save_ad_control_rule_group(
            {
                "group_id": "binding",
                "product": "dramawave",
                "name": "binding",
                "account_group_id": "pool",
                "rule_set_id": "rules",
                "enabled": False,
                "strategy": {"frontend_rule_group_id": "frontend", "close_time": "12:00"},
            },
            self.session,
        )
        return account_ids

    def seed_current_preview(self, expires_at=None):
        scope = app.ad_control_resolve_live_scope({"rule_group_id": "binding"})
        preview_hash = app.ad_control_live_scope_hash(scope)
        preview_id = "preview"
        expires_at = expires_at or (
            datetime.utcnow() + timedelta(minutes=20)
        ).strftime("%Y-%m-%d %H:%M:%S")
        criteria = {
            "mode": "live",
            "product": scope["product"],
            "accounts": scope["account_ids"],
            "rules": scope["rules"],
            "window": scope["window"],
            "strategy": scope["strategy"],
            "rule_group_id": "binding",
            "binding_id": "binding",
            "owner_user_id": self.OWNER_USER_ID,
            "object_level": scope["object_level"],
            "run_mode": scope["run_mode"],
            "preview_hash": preview_hash,
        }
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ad_control_preview (
                      preview_id, actor_user_id, action, level, product, criteria_json,
                      sample_json, total_count, created_at, expires_at
                    ) VALUES (?, ?, 'pause', 'campaign', ?, ?, '[]', 0, CURRENT_TIMESTAMP, ?)
                    """,
                    (
                        preview_id,
                        self.OWNER_USER_ID,
                        scope["product"],
                        json.dumps(criteria),
                        expires_at,
                    ),
                )
                conn.execute(
                    "UPDATE ad_control_rule_group SET last_preview_id=?, last_preview_hash=? "
                    "WHERE group_id='binding'",
                    (preview_id, preview_hash),
                )
                conn.commit()
            finally:
                conn.close()
        return preview_hash

    def test_large_pool_can_be_enabled_with_current_preview(self):
        account_ids = self.create_binding()
        self.assertGreaterEqual(app.AD_CONTROL_MAX_LIVE_ACCOUNTS, len(account_ids))
        binding = app.fetch_ad_control_rule_group(
            "binding", owner_user_id=self.OWNER_USER_ID
        )
        self.assertEqual(len(binding["account_ids"]), 245)
        self.assertEqual(binding["preview_status"], "missing")

        self.seed_current_preview()
        binding = app.fetch_ad_control_rule_group(
            "binding", owner_user_id=self.OWNER_USER_ID
        )
        self.assertEqual(binding["preview_status"], "ready")
        with mock.patch.object(
            app,
            "ad_control_validate_scope_token_access",
            return_value={"ok": True, "checked_count": 245},
        ) as validate:
            enabled = app.set_ad_control_rule_group_enabled(
                "binding", True, owner_user_id=self.OWNER_USER_ID
            )
        self.assertTrue(enabled["enabled"])
        self.assertEqual(validate.call_args.args[0]["account_ids"], account_ids)

    def test_token_validation_checks_all_245_accounts_with_one_token_lookup(self):
        account_ids = self.create_binding()
        with mock.patch.object(app, "ad_control_product_app_default_user", return_value={"user_id": "42"}), \
             mock.patch.object(app, "ad_control_token_for_user_id", return_value="token") as token_lookup, \
             mock.patch.object(app, "ad_control_graph_get", side_effect=lambda token, object_id, fields: {"name": object_id}):
            result = app.validate_ad_control_token_config({
                "product": "dramawave", "accounts": account_ids,
            })
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked_count"], 245)
        self.assertEqual(result["ok_count"], 245)
        self.assertEqual(len(result["results"]), 245)
        token_lookup.assert_called_once_with("42")

    def test_critical_db_query_retries_transient_failure(self):
        with mock.patch.object(
            app,
            "run_mysql",
            side_effect=[RuntimeError("busy"), RuntimeError("busy"), [["ok"]]],
        ) as run, mock.patch.object(app.time, "sleep"):
            rows = app.ad_control_run_critical_mysql("SELECT 1", "test")
        self.assertEqual(rows, [["ok"]])
        self.assertEqual(run.call_count, 3)

    def test_insight_schema_retries_transient_read_failure(self):
        rows = [["campaign_id"], ["dt"], ["ad_account_id"]]
        with mock.patch.object(
            app,
            "run_mysql",
            side_effect=[subprocess.CalledProcessError(1, ["mysql"]), rows],
        ) as run, mock.patch.object(app.time, "sleep"):
            columns = app.ad_control_validate_insight_start_schema()
        self.assertEqual(columns, {"campaign_id", "dt", "ad_account_id"})
        self.assertEqual(run.call_count, 2)

    def test_insight_schema_read_failure_is_not_reported_as_missing_columns(self):
        with mock.patch.object(app, "AD_CONTROL_CRITICAL_DB_RETRIES", 3), \
             mock.patch.object(
                 app,
                 "run_mysql",
                 side_effect=subprocess.CalledProcessError(1, ["mysql"]),
             ) as run, \
             mock.patch.object(app.time, "sleep"):
            with self.assertRaises(app.StructuredApiError) as raised:
                app.ad_control_validate_insight_start_schema()
        self.assertEqual(raised.exception.code, "insight_start_schema_unavailable")
        self.assertEqual(raised.exception.details.get("cause"), "CalledProcessError")
        self.assertNotIn("missing", raised.exception.details)
        self.assertEqual(run.call_count, 3)

    def test_insight_schema_still_fails_closed_for_real_missing_column(self):
        with mock.patch.object(app, "run_mysql", return_value=[["campaign_id"]]) as run:
            with self.assertRaises(app.StructuredApiError) as raised:
                app.ad_control_validate_insight_start_schema()
        self.assertEqual(raised.exception.code, "invalid_insight_start_schema")
        self.assertEqual(raised.exception.details.get("missing"), "dt")
        run.assert_called_once()

    def test_default_user_falls_back_to_durable_local_cache(self):
        app.ad_control_store_local_default_user_cache({
            "product": "dramawave",
            "user_id": "803",
            "app_id": "1479",
            "app_name": "Dramawave",
            "app_key": "1031273318485141",
        })
        app.AD_CONTROL_DEFAULT_USER_CACHE.clear()
        with mock.patch.object(
            app, "ad_control_run_critical_mysql", side_effect=RuntimeError("max connections")
        ):
            config = app.ad_control_product_app_default_user("dramawave")
        self.assertEqual(config["user_id"], "803")
        self.assertEqual(config["source"], "local_default_user_cache")

    def test_account_change_disables_binding_and_invalidates_preview(self):
        account_ids = self.create_binding()
        self.seed_current_preview()
        with app.JOB_DB_LOCK:
            conn = app.get_job_db_connection()
            try:
                conn.execute(
                    "UPDATE ad_control_rule_group SET enabled=1 WHERE group_id='binding'"
                )
                conn.commit()
            finally:
                conn.close()

        app.save_ad_control_account_group(
            {
                "group_id": "pool",
                "product": "dramawave",
                "name": "large pool",
                "account_ids": account_ids + ["9999999999999999"],
            },
            self.session,
        )
        binding = app.fetch_ad_control_rule_group(
            "binding", owner_user_id=self.OWNER_USER_ID
        )
        self.assertFalse(binding["enabled"])
        self.assertEqual(binding["preview_status"], "missing")
        self.assertFalse(binding["last_preview_id"])

    def test_expired_preview_fails_closed_on_enable(self):
        self.create_binding()
        self.seed_current_preview(
            (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        )
        with mock.patch.object(
            app,
            "ad_control_validate_scope_token_access",
            return_value={"ok": True, "checked_count": 245},
        ) as validate:
            with self.assertRaises(app.StructuredApiError) as error:
                app.set_ad_control_rule_group_enabled(
                    "binding", True, owner_user_id=self.OWNER_USER_ID
                )
        self.assertEqual(error.exception.code, "preview_required")
        validate.assert_not_called()
        binding = app.fetch_ad_control_rule_group(
            "binding", owner_user_id=self.OWNER_USER_ID
        )
        self.assertFalse(binding["enabled"])
        self.assertEqual(binding["preview_status"], "expired")

    def test_account_list_falls_back_to_saved_pool_without_business_db(self):
        account_ids = self.create_binding(account_count=3)
        with mock.patch.object(
            app, "ad_control_run_mysql", side_effect=RuntimeError("db unavailable")
        ):
            result = app.list_ad_control_accounts(
                "dramawave", owner_user_id=self.OWNER_USER_ID
            )
        self.assertEqual(result["source"], "saved_pools")
        self.assertEqual([item["account_id"] for item in result["items"]], account_ids)
        self.assertIn("已显示账户池", result["warning"])

    def test_product_account_cache_is_isolated_by_owner(self):
        route_source = inspect.getsource(app.DramaMaterialHandler.do_GET)
        self.assertIn(
            "owner_user_id=ad_control_actor(self._session())", route_source
        )
        owner_one_account = "1111111111111111"
        owner_two = "other-large-pool-owner"
        owner_two_account = "2222222222222222"
        app.save_ad_control_account_group(
            {
                "group_id": "owner-one-pool",
                "product": "dramawave",
                "name": "owner one pool",
                "account_ids": [owner_one_account],
            },
            self.session,
        )
        app.save_ad_control_account_group(
            {
                "group_id": "owner-two-pool",
                "product": "dramawave",
                "name": "owner two pool",
                "account_ids": [owner_two_account],
            },
            {"user_id": owner_two},
        )

        with mock.patch.object(app, "ad_control_run_mysql", return_value=[]):
            first = app.list_ad_control_accounts(
                "dramawave", owner_user_id=self.OWNER_USER_ID
            )
        self.assertEqual(first["source"], "business_db")
        self.assertEqual(
            [item["account_id"] for item in first["items"]], [owner_one_account]
        )

        with mock.patch.object(
            app, "ad_control_run_mysql", side_effect=RuntimeError("db unavailable")
        ) as query:
            second = app.list_ad_control_accounts(
                "dramawave", owner_user_id=owner_two
            )
        query.assert_called_once()
        self.assertEqual(second["source"], "saved_pools")
        self.assertEqual(
            [item["account_id"] for item in second["items"]], [owner_two_account]
        )

        first_again = app.list_ad_control_accounts(
            "dramawave", owner_user_id=self.OWNER_USER_ID
        )
        self.assertEqual(first_again["source"], "cache")
        self.assertEqual(
            [item["account_id"] for item in first_again["items"]],
            [owner_one_account],
        )

    def test_concurrent_account_requests_share_one_business_db_refresh(self):
        self.create_binding(account_count=3)

        def query(sql, timeout_seconds=None):
            time.sleep(0.02)
            if "MAX(d.updated_at)" in sql:
                return [["1000000000000000", "2026-07-10 00:00:00"]]
            return [["1000000000000000", "Account A", "+8", "1", "0"]]

        with mock.patch.object(app, "ad_control_run_mysql", side_effect=query) as run:
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(
                    lambda _: app.list_ad_control_accounts(
                        "dramawave", owner_user_id=self.OWNER_USER_ID
                    ),
                    range(6),
                ))
        self.assertEqual(run.call_count, 2)
        self.assertEqual({result["source"] for result in results}, {"business_db", "cache"})
        self.assertTrue(all(len(result["items"]) == 3 for result in results))

    def test_account_pool_in_use_cannot_be_deleted(self):
        self.create_binding(account_count=3)
        with self.assertRaises(app.StructuredApiError) as error:
            app.delete_ad_control_account_group(
                "pool", owner_user_id=self.OWNER_USER_ID
            )
        self.assertEqual(error.exception.code, "account_group_in_use")


if __name__ == "__main__":
    unittest.main()

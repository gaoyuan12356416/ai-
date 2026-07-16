import ast
import copy
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.ad_control_v3.catalog import StaticOptimizerIdentityResolver, facebook_field_catalog
from features.ad_control_v3.channels.facebook import (
    FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID,
    FacebookAdapter,
    SOURCE_QUERY_MAX_EXECUTION_TIME_MS,
)
from features.ad_control_v3.errors import AdControlV3Error
from features.ad_control_v3.repository import MemoryRepository
from features.ad_control_v3.rule_engine import evaluate_candidates
from features.ad_control_v3.schemas import Actor
from features.ad_control_v3.service import (
    COMPUTED_INSIGHT_FIELDS,
    SOURCE_MYSQL_READ_TIMEOUT_MAX_SECONDS,
    SOURCE_MYSQL_READ_TIMEOUT_MIN_SECONDS,
    SOURCE_INSIGHT_REQUIRED_COLUMNS,
    Service,
    _bounded_environment_int,
    _execute_bounded_source_query,
    _validate_source_schema_rows,
)
from features.ad_control_v3.storage import MemorySnapshotStore, SafeDataRoot


PRODUCTS = [
    {"channel": "facebook", "product_value": "Dramawave", "canonical_product": "Dramawave", "product_type": "short_drama", "enabled": True},
    {"channel": "facebook", "product_value": "FreeReels", "canonical_product": "FreeReels", "product_type": "short_drama", "enabled": True},
]


def base_payload(level="campaign", action="pause"):
    rule = {
        "rule_id": "rule-1",
        "name": "spend gate",
        "priority": 1,
        "logic": "and",
        "action": action,
        "conditions": [{"field": "spend", "operator": "gte", "value": 10}],
    }
    if action == "copy":
        carriers = {
            "campaign": "deep_copy_campaign",
            "adset": "same_campaign",
            "ad": "isolated_adset",
        }
        rule["copy_parameters"] = {
            "carrier_strategy": carriers[level],
            "budget_mode": "actual_cpi_multiplier",
            "budget_multiplier": 10,
            "roas_adjustment_direction": "increase",
            "roas_adjustment_percent": 5,
        }
    return {
        "name": "爆款观察",
        "description": "",
        "channel": "facebook",
        "object_level": level,
        "run_mode": "observe",
        "optimizer_id": 248,
        "products": ["Dramawave"],
        "account_timezones": [],
        "rules": [rule],
        "schedule": {},
        "quotas": {},
        "selection": {"mode": "all", "metric_window_days": 3},
    }


class QueryStub:
    def __init__(self):
        self.calls = []
        self.rows_by_product = {}

    def __call__(self, sql, params):
        self.calls.append((sql, tuple(params)))
        product = params[0]
        rows = self.rows_by_product.get(product)
        if rows is not None:
            return copy.deepcopy(rows)
        object_id = "c-1"
        if "s.adset_id" in sql and "AS object_id" in sql:
            object_id = "set-1"
        if "s.ad_id" in sql and "AS object_id" in sql:
            object_id = "ad-1"
        return [candidate_row(product, object_id)]


def candidate_row(product="Dramawave", object_id="c-1", spend=20, account="act_123"):
    return {
        "ad_account_id": account,
        "object_id": object_id,
        "object_name": "",
        "campaign_id": "c-1",
        "adset_id": "set-1" if object_id != "c-1" else "",
        "ad_id": "ad-1" if object_id == "ad-1" else "",
        "campaign_parent_count": 1,
        "adset_parent_count": 1 if object_id != "c-1" else 0,
        "product": product,
        "optimizer_id": 248,
        "series_code": "S-1",
        "series_code_count": 1,
        "content_id": "",
        "app": "Dramawave",
        "app_count": 1,
        "app_id": "1479",
        "app_id_count": 1,
        "os_type": "ios",
        "os_type_count": 1,
        "country": "US",
        "country_count": 1,
        "language": "en",
        "language_count": 1,
        "country_group": "Tier1",
        "country_group_count": 1,
        "drama_language": "en",
        "drama_language_count": 1,
        "bid_type": "LOWEST_COST",
        "bid_type_count": 1,
        "page_id": "page-1",
        "page_id_count": 1,
        "task_type": "auto_publish",
        "task_type_count": 1,
        "latest_auto_publish_dt": "2026-07-15",
        "latest_resource_created_at": "2026-07-14 01:00:00",
        "latest_spend_at": "2026-07-16 01:00:00",
        "resource_id": "r-1",
        "resource_id_count": 1,
        "resource_name": "asset",
        "resource_name_count": 1,
        "source_id": "source-1",
        "source_id_count": 1,
        "w2a_page_id": "",
        "w2a_page_id_count": 0,
        "ad_type": "video",
        "ad_type_count": 1,
        "category": "drama",
        "category_count": 1,
        "resource_tag": "winner",
        "resource_tag_count": 1,
        "source_type": "video",
        "source_type_count": 1,
        "resource_type": "creative",
        "resource_type_count": 1,
        "created_data_id": "created-1",
        "created_data_id_count": 1,
        "task_id": "task-1",
        "task_id_count": 1,
        "spend": spend,
        "impressions": 1000,
        "clicks": 100,
        "installs": 10,
        "purchase": 2,
        "revenue": 40,
        "day1_retain": 4,
        "retain_install": 8,
        "events": 12,
        "atc": 3,
        "delivery_cnt": 7,
        "af_installs": 5,
        "af_revenue": 30,
        "ad_impression": 200,
        "ad_impression_revenue": 10,
        "account_timezone": "America/Los_Angeles",
    }


def make_service(query=None, scheduler_enabled=False, scan_concurrency=1):
    query = query or QueryStub()
    repository = MemoryRepository(PRODUCTS)
    resolver = StaticOptimizerIdentityResolver(
        {"user-1": [248], "user-2": [248], "ambiguous": [248, 249]},
        [
            {"optimizer_id": 248, "name": "Opt A", "email": "a@example.com"},
            {"optimizer_id": 249, "name": "Opt B", "email": "b@example.com"},
        ],
    )
    fixed_now = lambda: datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
    return (
        Service(
            repository,
            {"facebook": FacebookAdapter(query), "tiktok": __import__("features.ad_control_v3.channels.tiktok", fromlist=["TikTokAdapter"]).TikTokAdapter()},
            resolver,
            MemorySnapshotStore(),
            timezone_loader=lambda: ["UTC", "America/Los_Angeles"],
            clock=fixed_now,
            scheduler_enabled=scheduler_enabled,
            scan_concurrency=scan_concurrency,
        ),
        repository,
        query,
    )


NORMAL = {"user_id": "user-1", "email": "a@example.com", "name": "Opt A", "role": "optimizer"}
SAME_OPTIMIZER_OTHER_USER = {"user_id": "user-2", "email": "other@example.com", "name": "Other"}
ADMIN = {"user_id": "admin-1", "email": "admin@example.com", "name": "Admin", "role": "admin"}


class SchemaAndPermissionTests(unittest.TestCase):
    def test_source_schema_fixture_requires_raw_components_not_computed_ratios(self):
        production_component_fixture = {
            "day1_retain", "retain_install", "af_revenue", "spend",
            "ad_impression_revenue",
        }
        self.assertTrue(production_component_fixture.issubset(SOURCE_INSIGHT_REQUIRED_COLUMNS))
        self.assertTrue(COMPUTED_INSIGHT_FIELDS.isdisjoint(SOURCE_INSIGHT_REQUIRED_COLUMNS))
        self.assertIn("data_source", SOURCE_INSIGHT_REQUIRED_COLUMNS)

    def test_source_schema_requires_reviewed_dpdo_prefix(self):
        insight_rows = [{"Field": field} for field in SOURCE_INSIGHT_REQUIRED_COLUMNS]
        account_rows = [{"Field": field} for field in ("account_id", "platform_id", "time_zone")]
        dpdo_rows = [
            {"Key_name": "dpdo", "Seq_in_index": index, "Column_name": field}
            for index, field in enumerate(("data_source", "product", "dt", "optimizer"), 1)
        ]
        _validate_source_schema_rows(insight_rows, account_rows, dpdo_rows)
        pss_only = [
            {"Key_name": "pss", "Seq_in_index": index, "Column_name": field}
            for index, field in enumerate(("product", "dt", "series_code"), 1)
        ]
        with self.assertRaises(AdControlV3Error) as raised:
            _validate_source_schema_rows(insight_rows, account_rows, pss_only)
        self.assertEqual("source_schema_mismatch", raised.exception.code)

    def test_actor_role_admin_is_recognized(self):
        self.assertTrue(Actor.from_value(ADMIN).is_admin)

    def test_create_is_always_disabled_and_observe(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["enabled"] = True
        payload["run_mode"] = "live"
        group = service.create_rule_group(NORMAL, payload)
        self.assertFalse(group["enabled"])
        self.assertEqual("observe", group["run_mode"])
        self.assertEqual(248, group["optimizer_id"])

    def test_account_scope_is_rejected_recursively(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["selection"]["account_ids"] = ["123"]
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("account_scope_forbidden", raised.exception.code)

    def test_normal_user_cannot_forge_optimizer(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["optimizer_id"] = 249
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("optimizer_forbidden", raised.exception.code)

    def test_admin_optimizer_must_be_active(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["optimizer_id"] = 999
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(ADMIN, payload)
        self.assertEqual("invalid_optimizer", raised.exception.code)

    def test_same_optimizer_can_read_but_not_mutate_another_owner(self):
        service, _, _ = make_service()
        group = service.create_rule_group(NORMAL, base_payload())
        visible = service.get_rule_group(SAME_OPTIMIZER_OTHER_USER, group["group_id"])
        self.assertEqual(group["group_id"], visible["group_id"])
        self.assertFalse(visible["can_mutate"])
        own_list = service.list_rule_groups(NORMAL)
        shared_list = service.list_rule_groups(SAME_OPTIMIZER_OTHER_USER)
        admin_list = service.list_rule_groups(ADMIN)
        self.assertTrue(own_list["items"][0]["can_mutate"])
        self.assertFalse(shared_list["items"][0]["can_mutate"])
        self.assertTrue(admin_list["items"][0]["can_mutate"])
        with self.assertRaises(AdControlV3Error) as raised:
            service.delete_rule_group(SAME_OPTIMIZER_OTHER_USER, group["group_id"])
        self.assertEqual(404, raised.exception.status)

    def test_update_accepts_route_json_version_and_removes_it_from_config(self):
        service, _, _ = make_service()
        group = service.create_rule_group(NORMAL, base_payload())
        payload = base_payload()
        payload["name"] = "updated through route contract"
        payload["version"] = group["config_version"]
        updated = service.update_rule_group(NORMAL, group["group_id"], payload)
        self.assertEqual(2, updated["config_version"])
        self.assertNotIn("version", updated)

    def test_tiktok_and_unsupported_field_fail_closed(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["channel"] = "tiktok"
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("channel_not_enabled", raised.exception.code)
        payload = base_payload("campaign")
        payload["rules"][0]["conditions"][0] = {"field": "creative_id", "operator": "eq", "value": "x"}
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("field_not_supported", raised.exception.code)

    def test_copy_carrier_is_level_specific_and_unknown_fields_rejected(self):
        service, _, _ = make_service()
        payload = base_payload("adset", "copy")
        payload["rules"][0]["copy_parameters"]["carrier_strategy"] = "deep_copy_campaign"
        with self.assertRaises(AdControlV3Error):
            service.create_rule_group(NORMAL, payload)
        payload = base_payload("campaign", "copy")
        payload["rules"][0]["copy_parameters"]["account_id"] = "123"
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("account_scope_forbidden", raised.exception.code)

    def test_meta_contract_has_safe_actor_flat_fields_and_three_levels(self):
        service, _, _ = make_service()
        meta = service.meta(ADMIN)
        self.assertTrue(meta["actor"]["is_admin"])
        self.assertEqual("admin", meta["actor"]["role"])
        self.assertNotIn("token", meta["actor"])
        self.assertTrue(meta["fields"])
        self.assertEqual({"campaign", "adset", "ad"}, set(meta["field_catalog"]))
        self.assertEqual("observe", meta["defaults"]["run_mode"])
        self.assertTrue(meta["capabilities"]["rule_group_search"])
        self.assertEqual(["name", "group_id"], meta["capabilities"]["rule_group_search_fields"])


class AdapterAndPreviewTests(unittest.TestCase):
    def test_bounded_query_has_all_mandatory_predicates_and_safe_prefix(self):
        query = QueryStub()
        adapter = FacebookAdapter(query)
        for level in ("campaign", "adset", "ad"):
            adapter.discover(
                {
                    "object_level": level,
                    "products": ["Dramawave"],
                    "optimizer_id": 248,
                    "date_from": "2026-07-14",
                    "date_to": "2026-07-16",
                    "account_timezones": [],
                }
            )
        self.assertEqual(3, len(query.calls))
        for sql, params in query.calls:
            for fragment in (
                "s.platform = %s", "s.product = %s", "BINARY s.product = BINARY %s",
                "s.dt BETWEEN %s AND %s", "s.optimizer = %s", "FORCE INDEX (dpdo)",
                "s.data_source IN (0, 6)",
            ):
                self.assertIn(fragment, sql)
            self.assertEqual(1, sql.count("MAX_EXECUTION_TIME(%s)" % SOURCE_QUERY_MAX_EXECUTION_TIME_MS))
            self.assertLessEqual(SOURCE_QUERY_MAX_EXECUTION_TIME_MS, 8000)
            self.assertLess(
                SOURCE_QUERY_MAX_EXECUTION_TIME_MS,
                SOURCE_MYSQL_READ_TIMEOUT_MIN_SECONDS * 1000,
            )
            self.assertEqual(10, SOURCE_MYSQL_READ_TIMEOUT_MAX_SECONDS)
            self.assertIn("CASE WHEN LEFT", sql)
            self.assertNotIn("ads_accounts_setting", sql)
            self.assertIn("0 AS settings_row_count", sql)
            self.assertNotIn("REPLACE(LOWER(CAST(s.ad_account_id", sql)
            self.assertEqual(
                ("Dramawave", 248, 0, "Dramawave", "Dramawave", "2026-07-14", "2026-07-16", 248),
                params[:-1],
            )

    def test_timezone_join_is_only_present_for_an_explicit_timezone_filter(self):
        self.assertEqual(0, FACEBOOK_ACCOUNT_SETTINGS_PLATFORM_ID)
        query = QueryStub()
        adapter = FacebookAdapter(query)
        base_scope = {
            "object_level": "campaign", "products": ["Dramawave"], "optimizer_id": 248,
            "date_from": "2026-07-16", "date_to": "2026-07-16", "required_fields": [],
        }
        adapter.discover(dict(base_scope, account_timezones=[]))
        without_timezone = query.calls[-1][0]
        adapter.discover(dict(base_scope, account_timezones=["America/Los_Angeles"]))
        with_timezone = query.calls[-1][0]
        self.assertNotIn("ads_accounts_setting", without_timezone)
        self.assertNotIn("COUNT(*) AS settings_row_count", without_timezone)
        self.assertIn("ads_accounts_setting", with_timezone)
        self.assertIn("COUNT(*) AS settings_row_count", with_timezone)
        self.assertIn("x.platform_id = 0", with_timezone)
        self.assertNotIn("x.platform_id = 1", with_timezone)

    def test_scope_estimate_uses_identity_only_projection(self):
        service, _, query = make_service()
        estimate = service.scope_estimate(NORMAL, {
            "channel": "facebook", "object_level": "campaign", "products": ["Dramawave"],
            "optimizer_id": 248, "account_timezones": [], "metric_window_days": 1,
        })
        sql = query.calls[-1][0]
        self.assertNotIn("GROUP_CONCAT", sql)
        self.assertNotIn("SUM(s.", sql)
        self.assertNotIn("ads_accounts_setting", sql)
        self.assertIn("campaign_parent_count", sql)
        self.assertEqual("identity_only", estimate["projection_mode"])
        self.assertEqual([], estimate["required_fields"])

    def test_preview_projection_uses_conditions_and_top_n_computed_dependencies(self):
        service, _, query = make_service()
        payload = base_payload()
        payload["rules"][0]["conditions"] = [{"field": "cpi", "operator": "gte", "value": 0}]
        payload["selection"] = {
            "mode": "global_top_n", "metric_window_days": 1, "top_n": 1,
            "sort_field": "roas", "sort_direction": "desc",
        }
        group = service.create_rule_group(NORMAL, payload)
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual(1, preview["summary"]["planned_count"])
        sql = query.calls[-1][0]
        for field in ("spend", "installs", "revenue"):
            self.assertIn("SUM(s.%s)" % field, sql)
        for field in ("impressions", "clicks", "purchase", "af_revenue"):
            self.assertNotIn("SUM(s.%s)" % field, sql)
        self.assertNotIn("GROUP_CONCAT", sql)
        self.assertEqual("rule_fields", preview["summary"]["projection_mode"])
        self.assertEqual(["cpi", "roas"], preview["summary"]["required_fields"])

    def test_actual_cpi_copy_budget_adds_cpi_projection_dependencies(self):
        service, _, query = make_service()
        group = service.create_rule_group(NORMAL, base_payload(action="copy"))
        preview = service.preview(NORMAL, group["group_id"], {})
        sql = query.calls[-1][0]
        self.assertIn("SUM(s.spend)", sql)
        self.assertIn("SUM(s.installs)", sql)
        self.assertIn("cpi", preview["summary"]["required_fields"])
        target = preview["targets"][0]
        self.assertFalse(target["copy_live_ready"])
        self.assertEqual(
            ["copy_persistence_not_configured", "roas_bid_unavailable"],
            target["copy_readiness_reasons"],
        )

        # Zero CPI cannot produce a valid positive Meta budget in a future live
        # copy path, even though zero is otherwise a present numeric metric.
        zero_cpi_candidate = candidate_row()
        zero_cpi_candidate["installs"] = 1
        zero_cpi_candidate["cpi"] = 0
        zero_result = evaluate_candidates(
            [zero_cpi_candidate],
            group["rules"],
            facebook_field_catalog("campaign"),
            {"mode": "all"},
        )
        self.assertIn(
            "actual_cpi_unavailable",
            zero_result["targets"][0]["copy_readiness_reasons"],
        )

    def test_context_projection_selects_only_the_requested_context(self):
        sql, columns = FacebookAdapter(QueryStub())._query_for_level(
            "campaign", ["series_code"]
        )
        self.assertIn("s.series_code", sql)
        self.assertNotIn("s.app_id", sql)
        self.assertNotIn("SUM(s.", sql)
        self.assertIn("series_code", columns)
        self.assertNotIn("app_id", columns)

    def test_unknown_required_projection_field_fails_closed(self):
        with self.assertRaises(AdControlV3Error) as raised:
            FacebookAdapter(QueryStub()).discover({
                "object_level": "campaign", "products": ["Dramawave"], "optimizer_id": 248,
                "date_from": "2026-07-16", "date_to": "2026-07-16",
                "account_timezones": [], "required_fields": ["drop_table"],
            })
        self.assertEqual("required_field_not_supported", raised.exception.code)

    def test_source_query_sets_session_timeout_before_every_statement(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.closed = False

            def execute(self, sql, params):
                self.calls.append((sql, tuple(params)))

            def fetchall(self):
                return [{"ok": 1}]

            def close(self):
                self.closed = True

        class Connection:
            def __init__(self):
                self.cursor_value = Cursor()
                self.closed = False

            def cursor(self):
                return self.cursor_value

            def close(self):
                self.closed = True

        connection = Connection()
        rows = _execute_bounded_source_query(lambda: connection, "SHOW COLUMNS FROM t", ())
        self.assertEqual([{"ok": 1}], rows)
        self.assertEqual(
            ("SET SESSION max_execution_time = %s", (SOURCE_QUERY_MAX_EXECUTION_TIME_MS,)),
            connection.cursor_value.calls[0],
        )
        self.assertEqual(("SHOW COLUMNS FROM t", ()), connection.cursor_value.calls[1])
        self.assertTrue(connection.cursor_value.closed)
        self.assertTrue(connection.closed)

    def test_source_query_failure_is_retryable_503_and_closes_connection(self):
        class Cursor:
            def execute(self, sql, params):
                raise RuntimeError("driver detail must not escape")

            def close(self):
                self.closed = True

        class Connection:
            def __init__(self):
                self.cursor_value = Cursor()
                self.closed = False

            def cursor(self):
                return self.cursor_value

            def close(self):
                self.closed = True

        connection = Connection()
        with self.assertLogs(level="WARNING") as captured:
            with self.assertRaises(AdControlV3Error) as raised:
                _execute_bounded_source_query(lambda: connection, "SELECT 1", ())
        self.assertEqual("source_query_unavailable", raised.exception.code)
        self.assertEqual(503, raised.exception.status)
        self.assertNotIn("driver detail", raised.exception.message)
        self.assertIn("ad-control V3 source query failed", "\n".join(captured.output))
        self.assertTrue(connection.cursor_value.closed)
        self.assertTrue(connection.closed)

    def test_invalid_source_timeout_environment_is_service_503(self):
        with mock.patch.dict(
            os.environ,
            {"AD_CONTROL_V3_SOURCE_MYSQL_READ_TIMEOUT_SECONDS": "not-an-int"},
        ):
            with self.assertRaises(AdControlV3Error) as raised:
                _bounded_environment_int(
                    "AD_CONTROL_V3_SOURCE_MYSQL_READ_TIMEOUT_SECONDS",
                    10,
                    SOURCE_MYSQL_READ_TIMEOUT_MIN_SECONDS,
                    SOURCE_MYSQL_READ_TIMEOUT_MAX_SECONDS,
                )
        self.assertEqual("service_not_configured", raised.exception.code)
        self.assertEqual(503, raised.exception.status)

    def test_multi_product_scan_exceeding_total_deadline_fails_closed(self):
        adapter = FacebookAdapter(QueryStub(), query_deadline_seconds=15)
        with mock.patch(
            "features.ad_control_v3.channels.facebook.time.monotonic",
            side_effect=[0, 0, 1, 16],
        ):
            with self.assertRaises(AdControlV3Error) as raised:
                adapter.discover({
                    "object_level": "campaign", "products": ["Dramawave", "FreeReels"],
                    "optimizer_id": 248, "date_from": "2026-07-16", "date_to": "2026-07-16",
                    "account_timezones": [], "required_fields": [],
                })
        self.assertEqual("scope_query_deadline_exceeded", raised.exception.code)

    def test_high_value_source_fields_are_level_scoped_aggregated_and_computed(self):
        campaign_catalog = {item["key"] for item in facebook_field_catalog("campaign") if item["filterable"]}
        ad_catalog = {item["key"] for item in facebook_field_catalog("ad") if item["filterable"]}
        for field in (
            "app_id", "os_type", "day1_retain", "retain_install", "retention_rate",
            "events", "atc", "delivery_cnt", "af_installs", "af_revenue", "af_roas",
            "ad_impression", "ad_impression_revenue", "ad_impression_roas",
            "bid_type", "page_id", "task_type",
        ):
            self.assertIn(field, campaign_catalog)
        for field in ("resource_tag", "source_type", "resource_type", "created_data_id", "task_id"):
            self.assertNotIn(field, campaign_catalog)
            self.assertIn(field, ad_catalog)

        query = QueryStub()
        adapter = FacebookAdapter(query)
        campaign_sql, _ = adapter._query_for_level("campaign")
        ad_sql, _ = adapter._query_for_level("ad")
        for fragment in ("s.app_id", "s.os_type", "SUM(s.day1_retain)", "SUM(s.af_revenue)", "SUM(s.ad_impression_revenue)"):
            self.assertIn(fragment, campaign_sql)
        self.assertIn("@@session.group_concat_max_len", campaign_sql)
        self.assertIn("OCTET_LENGTH(GROUP_CONCAT", campaign_sql)
        for fragment in ("s.resource_tag", "s.source_type", "s.resource_type", "s.created_data_id", "s.task_id"):
            self.assertIn(fragment, ad_sql)

        rows = adapter.discover({
            "object_level": "campaign", "products": ["Dramawave"], "optimizer_id": 248,
            "date_from": "2026-07-16", "date_to": "2026-07-16", "account_timezones": [],
        })
        self.assertEqual("1479", rows[0]["app_id"])
        self.assertEqual(50.0, rows[0]["retention_rate"])
        self.assertEqual(1.5, rows[0]["af_roas"])
        self.assertEqual(0.5, rows[0]["ad_impression_roas"])

    def test_ad_singular_context_multi_value_and_truncation_fail_closed(self):
        for value, count, expected_reason in (
            ("winner\ncontrol", 2, "ambiguous_object_context"),
            ("winner", 2, "context_aggregation_truncated"),
        ):
            query = QueryStub()
            row = candidate_row(object_id="ad-1")
            row["resource_tag"] = value
            row["resource_tag_count"] = count
            query.rows_by_product["Dramawave"] = [row]
            rows = FacebookAdapter(query).discover({
                "object_level": "ad", "products": ["Dramawave"], "optimizer_id": 248,
                "date_from": "2026-07-16", "date_to": "2026-07-16", "account_timezones": [],
            })
            self.assertEqual(expected_reason, rows[0]["blocked_reason"])

    def test_group_concat_byte_limit_detects_truncated_last_value(self):
        query = QueryStub()
        row = candidate_row()
        row["series_code"] = "abcdefghij"
        row["series_code_count"] = 1
        row["series_code_concat_bytes"] = 10
        row["context_concat_limit"] = 10
        query.rows_by_product["Dramawave"] = [row]
        rows = FacebookAdapter(query).discover({
            "object_level": "campaign", "products": ["Dramawave"], "optimizer_id": 248,
            "date_from": "2026-07-16", "date_to": "2026-07-16", "account_timezones": [],
        })
        self.assertEqual("context_aggregation_truncated", rows[0]["blocked_reason"])

    def test_scope_and_preview_share_nonblocking_scan_gate(self):
        class BlockingQuery(QueryStub):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def __call__(self, sql, params):
                self.entered.set()
                if not self.release.wait(5):
                    raise RuntimeError("blocking query test timeout")
                return super().__call__(sql, params)

        query = BlockingQuery()
        service, _, _ = make_service(query)
        group = service.create_rule_group(NORMAL, base_payload())
        first_errors = []

        def run_estimate():
            try:
                service.scope_estimate(NORMAL, {
                    "channel": "facebook", "object_level": "campaign", "products": ["Dramawave"],
                    "optimizer_id": 248, "account_timezones": [], "metric_window_days": 1,
                })
            except Exception as exc:  # pragma: no cover - asserted below
                first_errors.append(exc)

        thread = threading.Thread(target=run_estimate)
        thread.start()
        self.assertTrue(query.entered.wait(2))
        try:
            with self.assertRaises(AdControlV3Error) as raised:
                service.preview(NORMAL, group["group_id"], {})
            self.assertEqual("scan_busy", raised.exception.code)
            self.assertEqual(429, raised.exception.status)
        finally:
            query.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], first_errors)

    def test_scan_concurrency_has_strict_hard_limit(self):
        with self.assertRaises(AdControlV3Error) as raised:
            make_service(scan_concurrency=5)
        self.assertEqual("service_not_configured", raised.exception.code)

    def test_adapter_does_not_strip_a_second_act_prefix(self):
        query = QueryStub()
        query.rows_by_product["Dramawave"] = [candidate_row(account="act_123")]
        rows = FacebookAdapter(query).discover(
            {
                "object_level": "campaign",
                "products": ["Dramawave"],
                "optimizer_id": 248,
                "date_from": "2026-07-16",
                "date_to": "2026-07-16",
                "account_timezones": [],
            }
        )
        self.assertEqual("act_123", rows[0]["ad_account_id"])

    def test_adapter_has_product_total_candidate_and_deadline_limits(self):
        adapter = FacebookAdapter(QueryStub(), max_products=2)
        with self.assertRaises(AdControlV3Error) as raised:
            adapter.discover(
                {
                    "object_level": "campaign",
                    "products": ["p1", "p2", "p3"],
                    "optimizer_id": 248,
                    "date_from": "2026-07-16",
                    "date_to": "2026-07-16",
                    "account_timezones": [],
                }
            )
        self.assertEqual("product_scope_too_large", raised.exception.code)
        query = QueryStub()
        query.rows_by_product["Dramawave"] = [candidate_row(object_id="c-1"), candidate_row(object_id="c-2")]
        adapter = FacebookAdapter(query, max_total_candidates=1)
        with self.assertRaises(AdControlV3Error) as raised:
            adapter.discover(
                {
                    "object_level": "campaign",
                    "products": ["Dramawave"],
                    "optimizer_id": 248,
                    "date_from": "2026-07-16",
                    "date_to": "2026-07-16",
                    "account_timezones": [],
                }
            )
        self.assertEqual("scope_candidate_limit_exceeded", raised.exception.code)

    def test_three_levels_observe_and_no_external_mutator_exists(self):
        for level in ("campaign", "adset", "ad"):
            service, repository, _ = make_service()
            group = service.create_rule_group(NORMAL, base_payload(level))
            result = service.preview(NORMAL, group["group_id"], {})
            self.assertEqual("ready", result["status"])
            self.assertEqual(1, result["summary"]["planned_count"])
            self.assertEqual(1, len(repository.executions))
            self.assertEqual("observed", next(iter(repository.executions.values()))["status"])

    def test_cross_product_same_object_is_blocked(self):
        query = QueryStub()
        query.rows_by_product = {
            "Dramawave": [candidate_row("Dramawave", "c-1")],
            "FreeReels": [candidate_row("FreeReels", "c-1")],
        }
        service, _, _ = make_service(query)
        payload = base_payload()
        payload["products"] = ["Dramawave", "FreeReels"]
        group = service.create_rule_group(NORMAL, payload)
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual(1, preview["summary"]["blocked_count"])
        self.assertEqual("ambiguous_object_scope", preview["targets"][0]["reason"])

    def test_missing_required_parent_identity_is_blocked_for_adset_and_ad(self):
        for level, object_id, missing_field in (
            ("adset", "set-1", "campaign_parent_count"),
            ("ad", "ad-1", "campaign_parent_count"),
            ("ad", "ad-1", "adset_parent_count"),
        ):
            query = QueryStub()
            row = candidate_row(object_id=object_id)
            row[missing_field] = 0
            query.rows_by_product["Dramawave"] = [row]
            service, _, _ = make_service(query)
            group = service.create_rule_group(NORMAL, base_payload(level))
            preview = service.preview(NORMAL, group["group_id"], {})
            self.assertEqual("ambiguous_object_scope", preview["targets"][0]["reason"])

    def test_timezone_missing_is_audited_not_silently_included(self):
        query = QueryStub()
        row = candidate_row()
        row["account_timezone"] = ""
        query.rows_by_product["Dramawave"] = [row]
        service, _, _ = make_service(query)
        payload = base_payload()
        payload["account_timezones"] = ["UTC"]
        group = service.create_rule_group(NORMAL, payload)
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual("missing_account_timezone", preview["targets"][0]["reason"])

    def test_duplicate_identical_account_timezone_settings_do_not_restrict_empty_filter(self):
        query = QueryStub()
        row = candidate_row(account="123")
        row["settings_row_count"] = 2
        row["settings_timezone_count"] = 1
        query.rows_by_product["Dramawave"] = [row]
        service, _, _ = make_service(query)
        group = service.create_rule_group(NORMAL, base_payload())
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual(1, preview["summary"]["planned_count"])
        self.assertEqual("matched", preview["targets"][0]["reason"])

    def test_conflicting_timezone_rows_are_ignored_when_filter_is_empty(self):
        query = QueryStub()
        row = candidate_row(account="123")
        row["settings_row_count"] = 2
        row["settings_timezone_count"] = 2
        query.rows_by_product["Dramawave"] = [row]
        service, _, _ = make_service(query)
        group = service.create_rule_group(NORMAL, base_payload())
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual("matched", preview["targets"][0]["reason"])

    def test_conflicting_timezone_rows_are_blocked_when_filter_is_configured(self):
        query = QueryStub()
        row = candidate_row(account="123")
        row["settings_row_count"] = 2
        row["settings_timezone_count"] = 2
        query.rows_by_product["Dramawave"] = [row]
        service, _, _ = make_service(query)
        payload = base_payload()
        payload["account_timezones"] = ["America/Los_Angeles"]
        group = service.create_rule_group(NORMAL, payload)
        preview = service.preview(NORMAL, group["group_id"], {})
        self.assertEqual("ambiguous_account_timezone", preview["targets"][0]["reason"])

    def test_update_invalidates_preview_and_enable_needs_current_preview(self):
        service, _, _ = make_service(scheduler_enabled=True)
        group = service.create_rule_group(NORMAL, base_payload())
        service.preview(NORMAL, group["group_id"], {})
        enabled = service.set_enabled(NORMAL, group["group_id"], True)
        self.assertTrue(enabled["enabled"])
        update = base_payload()
        update["name"] = "changed"
        updated = service.update_rule_group(NORMAL, group["group_id"], update, group["config_version"])
        self.assertFalse(updated["enabled"])
        with self.assertRaises(AdControlV3Error) as raised:
            service.set_enabled(NORMAL, group["group_id"], True)
        self.assertEqual("stale_preview", raised.exception.code)

    def test_emergency_stop_clears_preview_and_requires_a_new_one(self):
        service, repository, _ = make_service(scheduler_enabled=True)
        group = service.create_rule_group(NORMAL, base_payload())
        service.preview(NORMAL, group["group_id"], {})
        service.emergency_stop(NORMAL, group["group_id"])
        stopped = repository.groups[group["group_id"]]
        self.assertTrue(stopped["emergency_stopped"])
        self.assertEqual("", stopped["last_preview_id"])
        self.assertEqual("", stopped["last_preview_hash"])
        with self.assertRaises(AdControlV3Error) as raised:
            service.set_enabled(NORMAL, group["group_id"], True)
        self.assertEqual("stale_preview", raised.exception.code)
        service.preview(NORMAL, group["group_id"], {})
        enabled = service.set_enabled(NORMAL, group["group_id"], True)
        self.assertTrue(enabled["enabled"])
        self.assertFalse(enabled["emergency_stopped"])

    def test_live_copy_fails_before_adapter_copy(self):
        service, repository, _ = make_service()
        group = service.create_rule_group(NORMAL, base_payload("campaign", "copy"))
        service.preview(NORMAL, group["group_id"], {})
        current = repository.groups[group["group_id"]]
        current["run_mode"] = "live"
        current["behavior_hash"] = __import__("features.ad_control_v3.schemas", fromlist=["behavior_hash"]).behavior_hash(current)
        current["last_preview_hash"] = current["behavior_hash"]
        repository.previews[current["last_preview_id"]]["behavior_hash"] = current["behavior_hash"]
        with self.assertRaises(AdControlV3Error) as raised:
            service.set_enabled(NORMAL, group["group_id"], True)
        self.assertEqual("copy_persistence_not_configured", raised.exception.code)

    def test_unreleased_scheduler_blocks_enable_without_state_write(self):
        service, repository, _ = make_service()
        group = service.create_rule_group(NORMAL, base_payload())
        service.preview(NORMAL, group["group_id"], {})
        with self.assertRaises(AdControlV3Error) as raised:
            service.set_enabled(NORMAL, group["group_id"], True)
        self.assertEqual("runner_scheduler_not_configured", raised.exception.code)
        self.assertFalse(repository.groups[group["group_id"]]["enabled"])

    def test_execution_list_shape_filters_and_verified_snapshot(self):
        service, _, _ = make_service()
        group = service.create_rule_group(NORMAL, base_payload())
        preview = service.preview(NORMAL, group["group_id"], {})
        page = service.list_executions(
            NORMAL,
            {"product": "Dramawave", "action": "pause", "object_id": "c-1", "date_from": "2026-07-16", "date_to": "2026-07-16"},
        )
        self.assertEqual(1, page["total"])
        item = page["items"][0]
        self.assertEqual("爆款观察", item["rule_group_name"])
        self.assertEqual(["Dramawave"], item["products"])
        self.assertEqual(["pause"], item["actions"])
        self.assertEqual(0, item["meta_write_count"])
        detail = service.get_execution(NORMAL, preview["execution_id"])
        self.assertTrue(detail["snapshot_valid"])
        self.assertEqual(0, detail["meta_write_count"])
        self.assertEqual("爆款观察", detail["rule_group_name"])
        self.assertEqual(["Dramawave"], detail["products"])
        self.assertEqual(["pause"], detail["actions"])
        self.assertEqual("Opt A", detail["optimizer_name"])
        self.assertEqual(1, detail["target_count"])

    def test_schedule_quota_selection_and_condition_values_are_strict(self):
        service, _, _ = make_service()
        payload = base_payload()
        payload["schedule"] = {"type": "interval", "interval_minutes": 0}
        with self.assertRaises(AdControlV3Error):
            service.create_rule_group(NORMAL, payload)
        payload = base_payload()
        payload["quotas"] = {"unknown": 1}
        with self.assertRaises(AdControlV3Error):
            service.create_rule_group(NORMAL, payload)
        payload = base_payload()
        payload["rules"][0]["conditions"][0]["value"] = "not-a-number"
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("condition_value_invalid", raised.exception.code)
        payload = base_payload()
        payload["selection"] = {"mode": "global_top_n", "metric_window_days": 2, "top_n": 1, "sort_field": "object_id", "sort_direction": "desc"}
        with self.assertRaises(AdControlV3Error) as raised:
            service.create_rule_group(NORMAL, payload)
        self.assertEqual("field_not_supported", raised.exception.code)


class RuleEngineTests(unittest.TestCase):
    def test_relative_drama_age_uses_explicit_evaluation_time_and_boundary(self):
        base = candidate_row(account="123")
        base.update({"channel": "facebook", "object_level": "campaign", "object_id": "c-1", "blocked_reason": ""})
        rules = [{
            "rule_id": "recent", "priority": 1, "logic": "and", "action": "pause",
            "conditions": [{"field": "latest_auto_publish_dt", "operator": "within_last_days", "value": 2}],
        }]
        evaluation_time = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
        base["latest_auto_publish_dt"] = "2026-07-14T00:00:00+00:00"
        result = evaluate_candidates(
            [base], rules, facebook_field_catalog("campaign"), {"mode": "all"}, evaluation_time=evaluation_time
        )
        self.assertEqual(1, result["summary"]["planned_count"])
        base["latest_auto_publish_dt"] = "2026-07-13T23:59:59+00:00"
        result = evaluate_candidates(
            [base], rules, facebook_field_catalog("campaign"), {"mode": "all"}, evaluation_time=evaluation_time
        )
        self.assertEqual(0, result["summary"]["planned_count"])
        older_rules = copy.deepcopy(rules)
        older_rules[0]["conditions"][0]["operator"] = "older_than_days"
        result = evaluate_candidates(
            [base], older_rules, facebook_field_catalog("campaign"), {"mode": "all"}, evaluation_time=evaluation_time
        )
        self.assertEqual(1, result["summary"]["planned_count"])

    def test_pause_wins_and_copy_parameters_are_immutable_in_copy_target(self):
        candidate = candidate_row(account="123")
        candidate.update({"channel": "facebook", "object_level": "campaign", "object_id": "c-1", "blocked_reason": ""})
        rules = [
            {"rule_id": "copy", "priority": 1, "logic": "and", "action": "copy", "conditions": [{"field": "spend", "operator": "gte", "value": 10}], "copy_parameters": {"budget_mode": "source_budget_ratio", "source_budget_ratio": 50}},
            {"rule_id": "pause", "priority": 9, "logic": "and", "action": "pause", "conditions": [{"field": "country", "operator": "eq", "value": "US"}]},
        ]
        result = evaluate_candidates([candidate], rules, facebook_field_catalog("campaign"), {"mode": "all"})
        target = result["targets"][0]
        self.assertEqual("pause", target["action"])
        self.assertEqual(["copy"], target["shadowed_by_rule"])
        self.assertEqual({}, target["copy_parameters"])
        self.assertEqual({"country": "US", "spend": 20}, target["condition_evidence"])
        self.assertFalse(target["condition_evidence_truncated"])
        copy_only = evaluate_candidates([candidate], rules[:1], facebook_field_catalog("campaign"), {"mode": "all"})
        copy_target = copy_only["targets"][0]
        self.assertEqual(50, copy_target["copy_parameters"]["source_budget_ratio"])
        self.assertFalse(copy_target["copy_live_ready"])
        self.assertEqual(
            ["copy_persistence_not_configured", "source_budget_unavailable"],
            copy_target["copy_readiness_reasons"],
        )

    def test_top_n_is_stable_and_blocked_does_not_consume_quota(self):
        candidates = []
        for object_id, spend in (("c-2", 10), ("c-1", 10), ("c-3", 30)):
            item = candidate_row(object_id=object_id, spend=spend)
            item.update({"channel": "facebook", "object_level": "campaign", "object_id": object_id, "blocked_reason": ""})
            candidates.append(item)
        blocked = candidate_row(object_id="c-0", spend=100)
        blocked.update({"channel": "facebook", "object_level": "campaign", "object_id": "c-0", "blocked_reason": "ambiguous_object_scope"})
        candidates.append(blocked)
        rules = [{"rule_id": "pause", "priority": 1, "logic": "and", "action": "pause", "conditions": [{"field": "spend", "operator": "gte", "value": 1}]}]
        result = evaluate_candidates(
            candidates,
            rules,
            facebook_field_catalog("campaign"),
            {"mode": "global_top_n", "top_n": 2, "sort_field": "spend", "sort_direction": "desc"},
        )
        planned = [item["object_id"] for item in result["targets"] if item["status"] == "would_pause"]
        self.assertEqual(["c-1", "c-3"], sorted(planned))
        self.assertEqual(3, result["summary"]["matched_before_selection"])
        self.assertEqual(2, result["summary"]["planned_count"])
        self.assertEqual(1, result["summary"]["deferred_count"])
        self.assertEqual(1, result["summary"]["blocked_count"])

    def test_all_account_and_product_selection_modes_and_ascending_direction(self):
        candidates = []
        for object_id, account, product, spend in (
            ("a1", "100", "Dramawave", 5),
            ("a2", "100", "Dramawave", 10),
            ("b1", "200", "FreeReels", 7),
            ("b2", "200", "FreeReels", 20),
        ):
            item = candidate_row(product=product, object_id=object_id, spend=spend, account=account)
            item.update({"channel": "facebook", "object_level": "campaign", "object_id": object_id, "blocked_reason": ""})
            candidates.append(item)
        rules = [{"rule_id": "pause", "priority": 1, "logic": "and", "action": "pause", "conditions": [{"field": "spend", "operator": "gte", "value": 1}]}]
        all_result = evaluate_candidates(candidates, rules, facebook_field_catalog("campaign"), {"mode": "all"})
        self.assertEqual(4, all_result["summary"]["planned_count"])
        account_result = evaluate_candidates(
            candidates, rules, facebook_field_catalog("campaign"),
            {"mode": "account_top_n", "top_n": 1, "sort_field": "spend", "sort_direction": "asc"},
        )
        self.assertEqual(
            {"a1", "b1"},
            {item["object_id"] for item in account_result["targets"] if item["status"] == "would_pause"},
        )
        product_result = evaluate_candidates(
            candidates, rules, facebook_field_catalog("campaign"),
            {"mode": "product_top_n", "top_n": 1, "sort_field": "spend", "sort_direction": "desc"},
        )
        self.assertEqual(
            {"a2", "b2"},
            {item["object_id"] for item in product_result["targets"] if item["status"] == "would_pause"},
        )

    def test_undefined_ratios_do_not_match_or_win_ascending_top_n(self):
        query = QueryStub()
        zero = candidate_row()
        for field in ("spend", "impressions", "clicks", "installs", "purchase", "retain_install"):
            zero[field] = 0
        query.rows_by_product["Dramawave"] = [zero]
        row = FacebookAdapter(query).discover({
            "object_level": "campaign", "products": ["Dramawave"], "optimizer_id": 248,
            "date_from": "2026-07-16", "date_to": "2026-07-16", "account_timezones": [],
        })[0]
        for field in (
            "ctr", "cpm", "cpc", "cpi", "purchase_cpa", "roas",
            "retention_rate", "af_roas", "ad_impression_roas",
        ):
            self.assertIsNone(row[field], field)

        low_cpi_rule = [{
            "rule_id": "low-cpi", "priority": 1, "logic": "and", "action": "pause",
            "conditions": [{"field": "cpi", "operator": "lte", "value": 10}],
        }]
        row.update({"channel": "facebook", "object_level": "campaign", "blocked_reason": ""})
        self.assertEqual(
            0,
            evaluate_candidates([row], low_cpi_rule, facebook_field_catalog("campaign"), {"mode": "all"})["summary"]["planned_count"],
        )

        missing = candidate_row(object_id="missing", spend=10)
        missing.update({"channel": "facebook", "object_level": "campaign", "blocked_reason": "", "cpi": None})
        defined = candidate_row(object_id="defined", spend=10)
        defined.update({"channel": "facebook", "object_level": "campaign", "blocked_reason": "", "cpi": 5})
        spend_rule = [{
            "rule_id": "spend", "priority": 1, "logic": "and", "action": "pause",
            "conditions": [{"field": "spend", "operator": "gte", "value": 1}],
        }]
        selected = evaluate_candidates(
            [missing, defined], spend_rule, facebook_field_catalog("campaign"),
            {"mode": "global_top_n", "top_n": 1, "sort_field": "cpi", "sort_direction": "asc"},
        )
        self.assertEqual(
            ["defined"],
            [item["object_id"] for item in selected["targets"] if item["status"] == "would_pause"],
        )


class StorageAndCompatibilityTests(unittest.TestCase):
    def test_safe_data_root_writes_atomically_and_blocks_escape(self):
        with tempfile.TemporaryDirectory() as parent:
            root = pathlib.Path(parent) / "v3-data"
            store = SafeDataRoot(root, require_distinct_device=False, min_free_bytes=0)
            metadata = store.write_snapshot("preview", "abc123", {"中文": [1, 2]})
            self.assertEqual({"中文": [1, 2]}, store.read_snapshot(metadata))
            self.assertTrue((root / "tmp").is_dir())
            self.assertTrue((root / "exports").is_dir())
            self.assertTrue((root / "cache").is_dir())
            with self.assertRaises(AdControlV3Error):
                store.read_snapshot({"relative_path": "../escape", "sha256": "x"})

    def test_snapshot_size_limit_fails_before_replace(self):
        with tempfile.TemporaryDirectory() as parent:
            root = pathlib.Path(parent) / "v3-data"
            store = SafeDataRoot(root, require_distinct_device=False, min_free_bytes=0, max_uncompressed_bytes=8)
            with self.assertRaises(AdControlV3Error) as raised:
                store.write_snapshot("preview", "too-big", {"value": "123456789"})
            self.assertEqual("snapshot_too_large", raised.exception.code)
            self.assertFalse((root / "snapshots" / "preview" / "too-big.json.gz").exists())

    def test_all_v3_python_parses_as_python_39(self):
        paths = list((ROOT / "features" / "ad_control_v3").rglob("*.py")) + [ROOT / "scripts" / "ad_control_v3_runner.py"]
        for path in paths:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))

    def test_runner_is_disabled_by_default_without_building_service(self):
        env = dict(os.environ)
        env.pop("AD_CONTROL_V3_RUNNER_ENABLED", None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ad_control_v3_runner.py")],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn('"status": "disabled"', result.stdout)


if __name__ == "__main__":
    unittest.main()

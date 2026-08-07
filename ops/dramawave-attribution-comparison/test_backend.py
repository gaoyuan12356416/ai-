from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import common
import refresh_cache
import service
import warm_cache


def custom_row(**overrides):
    row = {
        "dt": "2026-08-01",
        "channel": "Meta",
        "channel_id": "0",
        "product": "Dramawave",
        "app_id": "Dramawave",
        "optimizer_id": "123",
        "optimizer_name": "Alice",
        "country_group": "US",
        "ad_account_id": "9000000000000000001",
        "campaign_id": "1200000000000000001",
        "campaign_name": "Campaign A",
        "adset_id": "1200000000000000011",
        "adset_name": "Ad Set A",
        "ad_id": "1200000000000000111",
        "ad_name": "Ad A",
        "spend": 100.0,
        "impressions": 1000,
        "clicks": 100,
        "installs": 20,
        "af_installs": 18,
    }
    row.update(overrides)
    return row


def revenue_row(**overrides):
    row = {
        "campaign_id": "1200000000000000001",
        "campaign_name": "Campaign A",
        "adset_id": "1200000000000000011",
        "adset_name": "Ad Set A",
        "ad_id": "1200000000000000111",
        "ad_name": "Ad A",
        "users": 18,
        "purchase_d0": 2,
        "purchase_d7": 3,
        "revenue_iaa_d0": 10.0,
        "revenue_iap_d0": 20.0,
        "revenue_iaa_d7": 12.0,
        "revenue_iap_d7": 24.0,
        "ad_impression_count": 500,
    }
    row.update(overrides)
    return row


def mark_published_d10_cache(conn, version: str, *, rollup_version: str | None = None):
    common.set_meta(conn, "comparison_window", common.COMPARISON_WINDOW)
    common.set_meta(conn, "new_attribution_source", common.NEW_ATTRIBUTION_SOURCE)
    common.set_meta(conn, "data_version", version)
    common.set_meta(conn, "rollup_version", rollup_version or version)


def api_qs(query: str = "") -> dict[str, list[str]]:
    separator = "&" if query else ""
    return parse_qs(
        f"{query}{separator}api_schema_version={common.API_SCHEMA_VERSION}"
    )


class MappingTests(unittest.TestCase):
    def test_lookup_stores_unique_identity_directly_and_upgrades_only_on_collision(self):
        target = {}
        first = ("first",)
        second = ("second",)
        refresh_cache.add_lookup_candidate(target, "key", first)
        self.assertIs(target["key"], first)
        refresh_cache.add_lookup_candidate(target, "key", first)
        self.assertIs(target["key"], first)
        refresh_cache.add_lookup_candidate(target, "key", second)
        self.assertEqual(target["key"], {first, second})

    def test_mapping_consumes_source_streams_sequentially(self):
        events = []

        def stream(label, rows):
            events.append(f"{label}:start")
            for row in rows:
                yield row
            events.append(f"{label}:end")

        mapped = refresh_cache.map_day_state(
            dt.date(2026, 8, 1),
            stream("custom", [custom_row()]),
            stream("d7", [revenue_row()]),
            stream("d10", [revenue_row()]),
        )
        self.assertEqual(
            events,
            [
                "custom:start",
                "custom:end",
                "d7:start",
                "d7:end",
                "d10:start",
                "d10:end",
            ],
        )
        self.assertEqual(mapped.stats["fact_rows"], 1)
        self.assertEqual(len(list(mapped.iter_facts())), 1)

    def test_cache_warmer_uses_exact_frontend_default_keys(self):
        plan = warm_cache.request_plan(
            {
                "data_version": "v1",
                "cache": {"start_date": "2026-08-01", "end_date": "2026-08-06"},
                "defaults": {
                    "start_date": "2026-08-03",
                    "end_date": "2026-08-06",
                    "basis": "d0",
                    "dimensions": ["dt", "campaign"],
                },
            }
        )
        queries = [params for path, params in plan if path == "/api/query"]
        rankings = [params for path, params in plan if path == "/api/rankings"]
        options = [params for path, params in plan if path == "/api/options"]
        self.assertTrue(
            any(
                params["start_date"] == "2026-08-03"
                and params["dimensions"] == "dt,campaign"
                and params["metric_basis"] == "d0"
                for params in queries
            )
        )
        self.assertTrue(
            any(
                params["start_date"] == "2026-08-01"
                and params["dimensions"] == "adset"
                and params["metric_basis"] == "d7"
                for params in queries
            )
        )
        self.assertTrue(all(params["include_rankings"] == "0" for params in queries))
        self.assertTrue(
            all(
                params["api_schema_version"] == "2"
                for path, params in plan
                if path in {"/api/options", "/api/query", "/api/rankings"}
            )
        )
        self.assertEqual({params["metric_basis"] for params in rankings}, {"d0", "d7"})
        self.assertTrue(all(params["data_version"] == "v1" for params in rankings))
        self.assertTrue(all("limit" not in params for params in rankings))
        self.assertEqual({params["start_date"] for params in options}, {"2026-08-01", "2026-08-03"})
        self.assertLess(len(plan), service.RESPONSE_CACHE_SIZE)

    def test_custom_sql_uses_live_schema_and_pss_index(self):
        self.assertIn("FORCE INDEX (pss)", refresh_cache.CUSTOM_SQL)
        self.assertNotIn("c.campaign_name", refresh_cache.CUSTOM_SQL)
        self.assertNotIn("c.adset_name", refresh_cache.CUSTOM_SQL)
        self.assertNotIn("c.ad_name", refresh_cache.CUSTOM_SQL)

    def test_os_like_duplicate_rows_are_merged_once(self):
        day = dt.date(2026, 8, 1)
        old = [revenue_row(), revenue_row(users=2, revenue_iaa_d0=1, revenue_iap_d0=4)]
        new = [revenue_row(revenue_iaa_d0=25, revenue_iap_d0=25)]
        facts, stats = refresh_cache.map_day(day, [custom_row()], old, new)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["d7_users"], 20)
        self.assertAlmostEqual(facts[0]["d7_revenue_iaa_d0"] + facts[0]["d7_revenue_iap_d0"], 35.0)
        self.assertAlmostEqual(facts[0]["d10_revenue_iaa_d0"] + facts[0]["d10_revenue_iap_d0"], 50.0)
        self.assertEqual(facts[0]["mapping_status"], "mapped")
        self.assertEqual(facts[0]["matched_grain"], "ad")
        self.assertEqual(stats["d7_candidate_keys"], 1)

    def test_adset_fallback_and_ambiguity_are_not_allocated(self):
        day = dt.date(2026, 8, 1)
        unique = custom_row(ad_id="", adset_id="set-unique", spend=40)
        ambiguous_a = custom_row(ad_id="a", adset_id="set-shared", country_group="US", spend=50)
        ambiguous_b = custom_row(ad_id="b", adset_id="set-shared", country_group="CA", spend=60)
        old = [
            revenue_row(ad_id="", adset_id="set-unique", revenue_iaa_d0=4, revenue_iap_d0=6),
            revenue_row(ad_id="", adset_id="set-shared", revenue_iaa_d0=8, revenue_iap_d0=12),
        ]
        facts, stats = refresh_cache.map_day(day, [unique, ambiguous_a, ambiguous_b], old, [])
        mapped = [row for row in facts if row["mapping_status"] == "mapped"]
        ambiguous = [row for row in facts if row["mapping_status"] == "ambiguous"]
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0]["matched_grain"], "adset")
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["country_group"], "")
        self.assertEqual(ambiguous[0]["spend"], 0)
        self.assertAlmostEqual(sum(row["d7_revenue_iaa_d0"] + row["d7_revenue_iap_d0"] for row in facts), 30.0)
        self.assertEqual(stats["d7_mapped_keys"], 1)
        self.assertEqual(stats["d7_ambiguous_keys"], 1)

    def test_unmatched_global_revenue_is_excluded_from_dramawave_scope(self):
        day = dt.date(2026, 8, 1)
        facts, stats = refresh_cache.map_day(
            day,
            [custom_row()],
            [
                revenue_row(revenue_iaa_d0=4, revenue_iap_d0=6),
                revenue_row(campaign_id="other", adset_id="other", ad_id="other", revenue_iaa_d0=8, revenue_iap_d0=12),
            ],
            [],
        )
        self.assertEqual(sum(row["d7_candidate_keys"] for row in facts), 1)
        self.assertEqual(stats["excluded_unmatched_d7_rows"], 1)
        self.assertEqual(stats["d7_source_revenue_iaa_d0"] + stats["d7_source_revenue_iap_d0"], 30)
        self.assertEqual(stats["d7_merged_revenue_iaa_d0"] + stats["d7_merged_revenue_iap_d0"], 30)
        self.assertEqual(stats["d7_candidate_revenue_iaa_d0"] + stats["d7_candidate_revenue_iap_d0"], 10)
        self.assertEqual(
            stats["d7_excluded_unscoped_revenue_iaa_d0"] + stats["d7_excluded_unscoped_revenue_iap_d0"],
            20,
        )
        self.assertEqual(stats["d7_fact_revenue_iaa_d0"] + stats["d7_fact_revenue_iap_d0"], 10)

    def test_campaign_fallback(self):
        day = dt.date(2026, 8, 1)
        custom = custom_row(ad_id="", adset_id="")
        old = [revenue_row(ad_id="", adset_id="")]
        facts, _ = refresh_cache.map_day(day, [custom], old, [])
        self.assertEqual(facts[0]["matched_grain"], "campaign")

    def test_present_but_missing_ad_does_not_fall_back_to_adset(self):
        day = dt.date(2026, 8, 1)
        custom = custom_row(ad_id="known-ad", adset_id="shared-set")
        old = [revenue_row(ad_id="unknown-ad", adset_id="shared-set")]
        facts, stats = refresh_cache.map_day(day, [custom], old, [])
        self.assertEqual(facts[0]["mapping_status"], "spend_only")
        self.assertEqual(stats["d7_candidate_keys"], 0)
        self.assertEqual(stats["excluded_unmatched_d7_rows"], 1)
        self.assertGreater(stats["d7_excluded_unscoped_revenue_iaa_d0"], 0)

    def test_adset_fallback_is_unique_when_only_lower_ad_ids_differ(self):
        day = dt.date(2026, 8, 1)
        custom_a = custom_row(ad_id="ad-a", ad_name="A", adset_id="shared", spend=30)
        custom_b = custom_row(ad_id="ad-b", ad_name="B", adset_id="shared", spend=70)
        old = [revenue_row(ad_id="", adset_id="shared", revenue_iaa_d0=10, revenue_iap_d0=20)]
        facts, stats = refresh_cache.map_day(day, [custom_a, custom_b], old, [])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["spend"], 100)
        self.assertEqual(facts[0]["impressions"], 2000)
        self.assertEqual(stats["custom_source_spend"], 100)
        self.assertEqual(stats["custom_merged_spend"], 100)
        self.assertEqual(stats["custom_fact_spend"], 100)
        self.assertEqual(facts[0]["mapping_status"], "mapped")
        self.assertEqual(facts[0]["matched_grain"], "adset")
        self.assertEqual(stats["d7_ambiguous_keys"], 0)

    def test_revenue_fills_campaign_and_adset_names_without_ad_name_pollution(self):
        day = dt.date(2026, 8, 1)
        custom_a = custom_row(campaign_name="", adset_name="", ad_id="ad-a", ad_name="")
        custom_b = custom_row(campaign_name="", adset_name="", ad_id="ad-b", ad_name="", spend=25)
        old = [
            revenue_row(ad_id="ad-a", ad_name="Revenue Ad A"),
            revenue_row(ad_id="ad-b", ad_name="Revenue Ad B"),
        ]
        facts, _ = refresh_cache.map_day(day, [custom_a, custom_b], old, [])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["campaign_name"], "Campaign A")
        self.assertEqual(facts[0]["adset_name"], "Ad Set A")
        self.assertEqual(facts[0]["ad_name"], "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        retention_patcher = mock.patch.object(
            service, "retention_start", return_value=dt.date(2026, 8, 1)
        )
        retention_patcher.start()
        self.addCleanup(retention_patcher.stop)
        service.clear_response_cache()
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "dashboard.sqlite3"
        with common.connect_sqlite(self.path) as conn:
            first = refresh_cache.blank_fact(dt.date(2026, 8, 1))
            first.update(
                custom_row(),
            )
            first.update(
                {
                    "mapping_status": "mapped",
                    "matched_grain": "ad",
                    "d7_revenue_iaa_d0": 10,
                    "d7_revenue_iap_d0": 40,
                    "d10_revenue_iaa_d0": 25,
                    "d10_revenue_iap_d0": 50,
                    "d7_revenue_iaa_d7": 20,
                    "d7_revenue_iap_d7": 50,
                    "d10_revenue_iaa_d7": 35,
                    "d10_revenue_iap_d7": 65,
                    "d7_candidate_keys": 1,
                    "d7_mapped_keys": 1,
                    "d10_candidate_keys": 1,
                    "d10_mapped_keys": 1,
                }
            )
            second = refresh_cache.blank_fact(dt.date(2026, 8, 1))
            second.update(
                custom_row(
                    campaign_id="1200000000000000002",
                    campaign_name="Campaign B",
                    adset_id="1200000000000000022",
                    ad_id="1200000000000000222",
                    spend=100,
                    country_group="CA",
                )
            )
            second.update(
                {
                    "mapping_status": "mapped",
                    "matched_grain": "ad",
                    "d7_revenue_iaa_d0": 5,
                    "d7_revenue_iap_d0": 5,
                    "d10_revenue_iaa_d0": 10,
                    "d10_revenue_iap_d0": 10,
                    "d7_candidate_keys": 1,
                    "d7_mapped_keys": 1,
                    "d10_candidate_keys": 1,
                    "d10_mapped_keys": 1,
                }
            )
            common.insert_facts(conn, [first, second])
            refresh_cache.rebuild_rollups_for_day(conn, "2026-08-01")
            mark_published_d10_cache(conn, "v-test")
            common.set_meta(conn, "generated_at", "2026-08-01T12:00:00+08:00")
            conn.execute(
                "INSERT INTO refresh_log(dt,started_at,finished_at,status,fact_rows,detail,data_version) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "2026-08-01",
                    "2026-08-01T11:59:00+08:00",
                    "2026-08-01T12:00:00+08:00",
                    "success",
                    2,
                    json.dumps(
                        {
                            "excluded_unmatched_d7_rows": 2,
                            "excluded_unmatched_d10_rows": 3,
                            "d7_excluded_unscoped_revenue_iaa_d0": 4,
                            "d7_excluded_unscoped_revenue_iap_d0": 6,
                            "d10_excluded_unscoped_revenue_iaa_d0": 8,
                            "d10_excluded_unscoped_revenue_iap_d0": 12,
                            "d7_excluded_unscoped_revenue_iaa_d7": 5,
                            "d7_excluded_unscoped_revenue_iap_d7": 7,
                            "d10_excluded_unscoped_revenue_iaa_d7": 9,
                            "d10_excluded_unscoped_revenue_iap_d7": 13,
                        }
                    ),
                    "v-test",
                ),
            )
            conn.commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_aggregate_recomputes_roas_after_sum_and_keeps_ids_as_text(self):
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign&metric_basis=d0&sort_by=spend&sort_dir=desc&data_version=v-test"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            conn.execute("BEGIN")
            payload = service.query_payload(conn, params)
        self.assertEqual(payload["pagination"]["total"], 2)
        self.assertEqual(payload["totals"]["spend"], 200)
        self.assertEqual(payload["totals"]["d7_revenue"], 60)
        self.assertEqual(payload["totals"]["d10_revenue"], 95)
        self.assertAlmostEqual(payload["totals"]["d7_roas"], 0.3)
        self.assertIsInstance(payload["rows"][0]["campaign"], str)
        self.assertEqual(payload["mapping_quality"]["d7_coverage_ratio"], 1)
        self.assertEqual(payload["mapping_quality"]["d10_coverage_ratio"], 1)

    def test_api_and_metadata_expose_only_the_d10_attribution_contract(self):
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign&"
            "metric_basis=d0&sort_by=d10_revenue&include_rankings=0"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            payload = service.query_payload(conn, params)
            metadata = service.meta_payload(conn)
            _filename, csv_body = service.export_csv(conn, params)

        legacy_prefix = "d" + str(30)
        self.assertIn("d10_revenue", payload["totals"])
        self.assertIn("d10_roas", payload["totals"])
        self.assertFalse(any(key.startswith(legacy_prefix) for key in payload["totals"]))
        self.assertIn("d10_coverage_ratio", payload["mapping_quality"])
        self.assertFalse(
            any(key.startswith(legacy_prefix) for key in payload["mapping_quality"])
        )
        self.assertEqual(
            metadata["source_tables"]["new_attribution"],
            "kunlunads_dev.ads_app_revenues_10d",
        )
        self.assertEqual(metadata["api_schema_version"], 2)
        self.assertEqual(common.MIN_DATE, dt.date(2026, 8, 1))
        self.assertEqual(metadata["minimum_date"], "2026-08-01")
        self.assertEqual(metadata["cache"]["expected_start_date"], "2026-08-01")
        self.assertTrue(metadata["cache"]["range_complete"])
        self.assertEqual(metadata["cache"]["missing_dates"], [])
        self.assertEqual(metadata["defaults"]["start_date"], "2026-08-01")
        self.assertIn("app_revenues_10d", metadata["source_max_updated_at"])
        csv_header = csv_body.decode("utf-8-sig").splitlines()[0]
        self.assertIn("d10_revenue", csv_header)
        self.assertNotIn(legacy_prefix, csv_header)

    def test_data_endpoints_require_api_schema_version_two(self):
        query = (
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign&"
            "include_rankings=0"
        )
        invalid_params = (
            parse_qs(query),
            parse_qs(query + "&api_schema_version=1"),
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            self.assertEqual(service.meta_payload(conn)["api_schema_version"], 2)
            for params in invalid_params:
                for reader in (
                    lambda: service.options_payload(conn, params),
                    lambda: service.query_payload(conn, params),
                    lambda: service.rankings_payload(conn, params),
                    lambda: service.export_csv(conn, params),
                ):
                    with self.assertRaisesRegex(
                        service.RequestError,
                        "api_schema_version=2 is required",
                    ) as error:
                        reader()
                    self.assertEqual(error.exception.status, 409)

    def test_legacy_attribution_semantics_fail_closed_for_every_web_read(self):
        legacy_window = "D" + str(30)
        legacy_source = "kunlunads_dev.ads_app_revenues_" + str(30) + "d"
        with common.connect_sqlite(self.path) as conn:
            common.set_meta(conn, "comparison_window", legacy_window)
            common.set_meta(conn, "new_attribution_source", legacy_source)
            conn.commit()
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign&"
            "include_rankings=0"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            for reader in (
                lambda: service.meta_payload(conn),
                lambda: service.options_payload(conn, params),
                lambda: service.query_payload(conn, params),
                lambda: service.rankings_payload(conn, params),
                lambda: service.export_csv(conn, params),
            ):
                with self.assertRaisesRegex(service.RequestError, "D10 cache contract rejected") as error:
                    reader()
                self.assertEqual(error.exception.status, 503)

    def test_same_version_and_parameters_use_bounded_service_cache(self):
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign&"
            "data_version=v-test&include_rankings=0"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            first_payload = service.query_payload(conn, params)
            second_payload = service.query_payload(conn, params)
        self.assertIs(first_payload, second_payload)
        self.assertEqual(len(service._RESPONSE_CACHE), 1)

    def test_rankings_can_be_deferred_and_loaded_from_independent_cache_key(self):
        query_params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&include_rankings=0&data_version=v-test"
        )
        ranking_params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&metric_basis=d0&data_version=v-test"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn, mock.patch.object(
            service, "build_rankings", wraps=service.build_rankings
        ) as builder:
            main_payload = service.query_payload(conn, query_params)
            self.assertEqual(main_payload["rankings"], {})
            builder.assert_not_called()
            ranking_payload = service.rankings_payload(conn, ranking_params)
            self.assertEqual(ranking_payload["data_version"], "v-test")
            self.assertEqual(set(ranking_payload["rankings"]), set(service.RANKING_DIMENSIONS))
            self.assertEqual(builder.call_count, 1)
            self.assertIs(ranking_payload, service.rankings_payload(conn, ranking_params))
            alternate = api_qs(
                "start_date=2026-08-01&end_date=2026-08-01&metric_basis=d0&data_version=v-test&"
                "dimensions=adset&sort_by=d10_revenue&offset=50&limit=100"
            )
            self.assertIs(ranking_payload, service.rankings_payload(conn, alternate))
            self.assertEqual(builder.call_count, 1)
        namespaces = {key[0] for key in service._RESPONSE_CACHE}
        self.assertEqual(namespaces, {"query", "rankings"})

    def test_same_ranking_key_is_singleflight_across_threads(self):
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&metric_basis=d0&data_version=v-test"
        )
        original = service.build_rankings
        started = threading.Event()
        calls = []

        def delayed(*args, **kwargs):
            calls.append(1)
            started.set()
            time.sleep(0.1)
            return original(*args, **kwargs)

        results = []
        errors = []

        def worker():
            try:
                with common.connect_sqlite(self.path, readonly=True) as conn:
                    results.append(service.rankings_payload(conn, params))
            except Exception as exc:  # pragma: no cover - asserted below.
                errors.append(exc)

        with mock.patch.object(service, "build_rankings", side_effect=delayed):
            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(started.wait(timeout=2))
            second.start()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(calls), 1)
        self.assertIs(results[0], results[1])

    def test_source_scope_exclusions_are_global_to_date_range_and_flag_filters(self):
        unfiltered = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&metric_basis=d0&include_rankings=0"
        )
        filtered = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&metric_basis=d0&"
            "country_group=US&include_rankings=0"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            global_item = service.query_payload(conn, unfiltered)["mapping_quality"]["source_scope_exclusions"]
            filtered_item = service.query_payload(conn, filtered)["mapping_quality"]["source_scope_exclusions"]
        self.assertEqual(
            set(global_item),
            {"scope", "business_filters_applied", "d7_rows", "d10_rows", "d7_revenue", "d10_revenue"},
        )
        self.assertEqual(global_item["scope"], "date_range_global_not_filter_attributable")
        self.assertFalse(global_item["business_filters_applied"])
        self.assertEqual(global_item["d7_rows"], 2)
        self.assertEqual(global_item["d10_rows"], 3)
        self.assertEqual(global_item["d7_revenue"], 10)
        self.assertEqual(global_item["d10_revenue"], 20)
        self.assertTrue(filtered_item["business_filters_applied"])
        self.assertEqual(filtered_item["d7_rows"], global_item["d7_rows"])
        self.assertEqual(filtered_item["d10_rows"], global_item["d10_rows"])
        self.assertEqual(filtered_item["d7_revenue"], global_item["d7_revenue"])
        self.assertEqual(filtered_item["d10_revenue"], global_item["d10_revenue"])

    def test_source_scope_exclusions_use_latest_success_per_day_and_selected_basis(self):
        latest = {
            "excluded_unmatched_d7_rows": 7,
            "excluded_unmatched_d10_rows": 9,
            "d7_excluded_unscoped_revenue_iaa_d7": 11,
            "d7_excluded_unscoped_revenue_iap_d7": 13,
            "d10_excluded_unscoped_revenue_iaa_d7": 17,
            "d10_excluded_unscoped_revenue_iap_d7": 19,
        }
        with common.connect_sqlite(self.path) as conn:
            conn.execute(
                "INSERT INTO refresh_log(dt,started_at,finished_at,status,detail,data_version) VALUES(?,?,?,?,?,?)",
                (
                    "2026-08-01",
                    "2026-08-01T12:29:00+08:00",
                    "2026-08-01T12:30:00+08:00",
                    "success",
                    json.dumps(latest),
                    "v-test",
                ),
            )
            conn.commit()
        service.clear_response_cache()
        params = api_qs(
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&metric_basis=d7&include_rankings=0"
        )
        with common.connect_sqlite(self.path, readonly=True) as conn:
            item = service.query_payload(conn, params)["mapping_quality"]["source_scope_exclusions"]
        self.assertEqual(item["d7_rows"], 7)
        self.assertEqual(item["d10_rows"], 9)
        self.assertEqual(item["d7_revenue"], 24)
        self.assertEqual(item["d10_revenue"], 36)

    def test_common_filters_have_equality_first_date_indexes(self):
        with common.connect_sqlite(self.path, readonly=True) as conn:
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list('attribution_fact')")}
            filter_indexes = {
                row["name"] for row in conn.execute("PRAGMA index_list('attribution_filter_daily')")
            }
            campaign_indexes = {
                row["name"] for row in conn.execute("PRAGMA index_list('attribution_campaign_daily')")
            }
        self.assertTrue(
            {
                "idx_fact_dt",
                "idx_fact_channel_dt",
                "idx_fact_app_dt",
                "idx_fact_optimizer_dt",
                "idx_fact_country_dt",
                "idx_fact_account_dt",
            }.issubset(indexes)
        )
        self.assertFalse(
            {
                "idx_fact_dt_channel",
                "idx_fact_dt_app",
                "idx_fact_dt_optimizer",
                "idx_fact_dt_country",
                "idx_fact_dt_account",
                "idx_fact_dt_campaign",
                "idx_fact_dt_adset",
            }
            & indexes
        )
        self.assertEqual(len(filter_indexes), 6)
        self.assertEqual(len(campaign_indexes), 6)

    def test_rollup_routing_and_stale_version_fallback(self):
        empty = parse_qs("start_date=2026-08-01&end_date=2026-08-01")
        campaign_search = parse_qs("campaign_q=Campaign")
        adset_search = parse_qs("adset_q=Ad+Set")
        with common.connect_sqlite(self.path, readonly=True) as conn:
            self.assertEqual(service.detail_table_for(conn, empty, ["dt"]), service.FILTER_ROLLUP_TABLE)
            self.assertEqual(
                service.detail_table_for(conn, empty, ["campaign"]), service.CAMPAIGN_ROLLUP_TABLE
            )
            self.assertEqual(service.detail_table_for(conn, empty, ["adset"]), service.FACT_TABLE)
            self.assertEqual(service.context_table_for(conn, empty), service.FILTER_ROLLUP_TABLE)
            self.assertEqual(service.context_table_for(conn, campaign_search), service.CAMPAIGN_ROLLUP_TABLE)
            self.assertEqual(service.context_table_for(conn, adset_search), service.FACT_TABLE)
            self.assertEqual(
                service.ranking_table_for(conn, empty, "campaign"), service.CAMPAIGN_ROLLUP_TABLE
            )
            self.assertEqual(service.ranking_table_for(conn, empty, "adset"), service.FACT_TABLE)
            self.assertEqual(service.ranking_table_for(conn, adset_search, "campaign"), service.FACT_TABLE)
        with common.connect_sqlite(self.path) as conn:
            common.set_meta(conn, "rollup_version", "stale")
            conn.commit()
        with common.connect_sqlite(self.path, readonly=True) as conn:
            self.assertEqual(service.detail_table_for(conn, empty, ["dt"]), service.FACT_TABLE)
            self.assertEqual(service.context_table_for(conn, empty), service.FACT_TABLE)
            self.assertEqual(service.ranking_table_for(conn, empty, "campaign"), service.FACT_TABLE)

    def test_meta_marks_rollup_version_mismatch_unhealthy(self):
        with common.connect_sqlite(self.path, readonly=True) as conn:
            self.assertTrue(service.meta_payload(conn)["cache"]["rollups_current"])
        with common.connect_sqlite(self.path) as conn:
            common.set_meta(conn, "rollup_version", "stale")
            conn.commit()
        with common.connect_sqlite(self.path, readonly=True) as conn:
            self.assertFalse(service.meta_payload(conn)["cache"]["rollups_current"])

    def test_rollup_results_exactly_match_fact_fallback(self):
        queries = [
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&include_rankings=0",
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign",
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=adset",
            "start_date=2026-08-01&end_date=2026-08-01&dimensions=optimizer&country_group=US",
        ]
        with common.connect_sqlite(self.path, readonly=True) as conn:
            rollup_payloads = [service.query_payload(conn, api_qs(query)) for query in queries]
        with common.connect_sqlite(self.path) as conn:
            common.set_meta(conn, "rollup_version", "stale")
            conn.commit()
        service.clear_response_cache()
        with common.connect_sqlite(self.path, readonly=True) as conn:
            fact_payloads = [service.query_payload(conn, api_qs(query)) for query in queries]
        self.assertEqual(rollup_payloads, fact_payloads)

    def test_query_table_whitelist_blocks_identifier_injection(self):
        with common.connect_sqlite(self.path, readonly=True) as conn:
            with self.assertRaises(service.RequestError):
                service.aggregate_rows(
                    conn,
                    table="attribution_fact; DROP TABLE cache_meta",
                    where_sql="dt=?",
                    where_values=["2026-08-01"],
                    dimensions=["dt"],
                    basis="d0",
                )
            with self.assertRaises(service.RequestError):
                service.grouped_count(
                    conn,
                    "dt=?",
                    ["2026-08-01"],
                    ["dt"],
                    table="not_a_table",
                )
            with self.assertRaises(service.RequestError):
                service.export_csv(
                    conn,
                    api_qs("start_date=2026-08-01&end_date=2026-08-01&dimensions=dt"),
                    table="attribution_fact UNION SELECT 1",
                )

    def test_d7_cumulative_basis_is_independent_from_attribution_window(self):
        params = api_qs("start_date=2026-08-01&end_date=2026-08-01&dimensions=dt&metric_basis=d7")
        with common.connect_sqlite(self.path, readonly=True) as conn:
            payload = service.query_payload(conn, params)
        self.assertEqual(payload["totals"]["d7_revenue"], 70)
        self.assertEqual(payload["totals"]["d10_revenue"], 100)

    def test_version_conflict_and_identifier_whitelist(self):
        with common.connect_sqlite(self.path, readonly=True) as conn:
            with self.assertRaises(service.RequestError) as conflict:
                service.query_payload(conn, api_qs("data_version=old&dimensions=dt"))
            self.assertEqual(conflict.exception.status, 409)
            with self.assertRaises(service.RequestError):
                service.query_payload(conn, api_qs("dimensions=dt,(SELECT+1)&start_date=2026-08-01&end_date=2026-08-01"))

    def test_cutoff_and_options(self):
        with common.connect_sqlite(self.path, readonly=True) as conn:
            with self.assertRaises(service.RequestError):
                service.parse_range(conn, parse_qs("start_date=2026-07-31&end_date=2026-08-01"))
            result = service.options_payload(conn, api_qs("start_date=2026-08-01&end_date=2026-08-01"))
        self.assertEqual(result["options"]["channel"][0], {"value": "0", "label": "Meta"})

    def test_missing_cache_day_is_not_silently_treated_as_zero(self):
        with common.connect_sqlite(self.path) as conn:
            row = refresh_cache.blank_fact(dt.date(2026, 8, 3))
            row.update(custom_row(dt="2026-08-03", spend=1))
            common.insert_facts(conn, [row])
            conn.commit()
        with common.connect_sqlite(self.path, readonly=True) as conn:
            coverage = service.cache_coverage(conn)
            self.assertFalse(coverage["complete"])
            self.assertIn("2026-08-02", coverage["missing_dates"])
            with self.assertRaisesRegex(service.RequestError, "missing requested dates"):
                service.parse_range(conn, parse_qs("start_date=2026-08-01&end_date=2026-08-03"))


class DeploymentContractTests(unittest.TestCase):
    def test_default_and_example_use_an_independent_d10_cache_file(self):
        expected = (
            "/mnt/data-disk/dramawave-attribution-comparison/cache/"
            "dashboard-d10.sqlite3"
        )
        self.assertEqual(common.DEFAULT_DB_PATH, expected)
        env_example = (
            Path(__file__).with_name("deploy") / "dashboard.env.example"
        ).read_text(encoding="utf-8")
        self.assertIn(f"DRAMAWAVE_ATTRIBUTION_DB_PATH={expected}", env_example)
        self.assertNotIn(
            "DRAMAWAVE_ATTRIBUTION_DB_PATH=/mnt/data-disk/"
            "dramawave-attribution-comparison/cache/dashboard.sqlite3",
            env_example,
        )

    def test_refresh_unit_shares_tt_lock_and_enforces_memory_limit(self):
        deploy_dir = Path(__file__).with_name("deploy")
        service_unit = (
            deploy_dir / "dramawave-attribution-comparison-refresh.service"
        ).read_text(encoding="utf-8")
        timer_unit = (
            deploy_dir / "dramawave-attribution-comparison-refresh.timer"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "/usr/bin/flock -E 75 -xn /tmp/tt_minis_multi_dim_dashboard.lock",
            service_unit,
        )
        self.assertNotIn("PrivateTmp=true", service_unit)
        self.assertIn("SuccessExitStatus=75", service_unit)
        self.assertIn("ReadWritePaths=/tmp", service_unit)
        self.assertIn("MemoryHigh=800M", service_unit)
        self.assertIn("MemoryMax=1G", service_unit)
        self.assertIn("OnCalendar=*-*-* *:04,34:00", timer_unit)


class RefreshAtomicityTests(unittest.TestCase):
    def _checkpointed_legacy_wal_cache(self, path: Path) -> tuple[bytes, int]:
        legacy_prefix = "d" + str(30)
        legacy_source = "kunlunads_dev.ads_app_revenues_" + str(30) + "d"
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            conn.executescript(
                "CREATE TABLE cache_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE attribution_fact("
                "id INTEGER PRIMARY KEY,dt TEXT NOT NULL,"
                f"{legacy_prefix}_revenue_iaa_d0 REAL NOT NULL DEFAULT 0);"
            )
            conn.executemany(
                "INSERT INTO cache_meta(key,value) VALUES(?,?)",
                (
                    ("data_version", json.dumps("legacy-version")),
                    ("comparison_window", json.dumps("D" + str(30))),
                    ("new_attribution_source", json.dumps(legacy_source)),
                ),
            )
            conn.commit()
            self.assertEqual(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())
        return path.read_bytes(), path.stat().st_mtime_ns

    def _assert_legacy_cache_untouched(
        self,
        path: Path,
        snapshot: tuple[bytes, int],
    ) -> None:
        bytes_before, mtime_before = snapshot
        self.assertEqual(path.read_bytes(), bytes_before)
        self.assertEqual(path.stat().st_mtime_ns, mtime_before)
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())

    @contextlib.contextmanager
    def _d10_cache_with_tampered_uncheckpointed_wal(self, path: Path):
        with common.connect_sqlite(path) as conn:
            mark_published_d10_cache(conn, "valid-main-version")
            conn.commit()
            self.assertEqual(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        writer = sqlite3.connect(path)
        try:
            self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "UPDATE cache_meta SET value=? WHERE key='comparison_window'",
                (json.dumps("D" + str(30)),),
            )
            writer.commit()
            self.assertTrue(Path(str(path) + "-wal").exists())
            immutable_metadata = common.preflight_existing_cache(path)
            self.assertEqual(
                immutable_metadata["comparison_window"],
                common.COMPARISON_WINDOW,
            )
            yield
        finally:
            writer.close()

    def test_refresh_rejects_a_published_legacy_cache_before_opening_mysql(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            snapshot = self._checkpointed_legacy_wal_cache(path)
            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=["2026-08-01"],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )
            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache, "mysql_connection"
            ) as mysql, mock.patch.object(
                refresh_cache,
                "connect_sqlite",
                side_effect=AssertionError("ordinary SQLite open reached before contract rejection"),
            ) as ordinary_open:
                with self.assertRaisesRegex(common.CacheContractError, "D10 cache contract rejected"):
                    refresh_cache.refresh(args)
                mysql.assert_not_called()
                ordinary_open.assert_not_called()
            self._assert_legacy_cache_untouched(path, snapshot)

    def test_service_startup_rejects_legacy_wal_cache_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            snapshot = self._checkpointed_legacy_wal_cache(path)
            with mock.patch.dict(
                os.environ,
                {"DRAMAWAVE_ATTRIBUTION_DB_PATH": str(path)},
                clear=False,
            ), mock.patch(
                "sys.argv",
                ["service.py", "--skip-mount-check"],
            ), mock.patch.object(
                service,
                "connect_sqlite",
                side_effect=AssertionError("ordinary SQLite open reached before contract rejection"),
            ) as ordinary_open, mock.patch.object(
                service,
                "ThreadingHTTPServer",
            ) as server, mock.patch("builtins.print"):
                self.assertEqual(service.main(), 2)
                ordinary_open.assert_not_called()
                server.assert_not_called()
            self._assert_legacy_cache_untouched(path, snapshot)

    def test_refresh_wal_aware_gate_rejects_committed_uncheckpointed_tamper(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard-d10.sqlite3"
            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=["2026-08-01"],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )
            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            ordinary_connect = refresh_cache.connect_sqlite
            readonly_calls = []

            @contextlib.contextmanager
            def readonly_only(target=None, *, readonly=False):
                if not readonly:
                    raise AssertionError("writable SQLite opened after WAL contract failure")
                readonly_calls.append(target)
                with ordinary_connect(target, readonly=True) as conn:
                    yield conn

            with self._d10_cache_with_tampered_uncheckpointed_wal(path), mock.patch.dict(
                os.environ,
                env,
                clear=False,
            ), mock.patch.object(
                refresh_cache,
                "connect_sqlite",
                readonly_only,
            ), mock.patch.object(
                refresh_cache,
                "mysql_connection",
            ) as mysql:
                with self.assertRaisesRegex(
                    common.CacheContractError,
                    "comparison_window",
                ):
                    refresh_cache.refresh(args)
                self.assertEqual(readonly_calls, [path])
                mysql.assert_not_called()

    def test_service_wal_aware_gate_rejects_committed_uncheckpointed_tamper(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard-d10.sqlite3"
            ordinary_connect = service.connect_sqlite
            readonly_calls = []

            @contextlib.contextmanager
            def readonly_only(target=None, *, readonly=False):
                if not readonly:
                    raise AssertionError("writable SQLite opened after WAL contract failure")
                readonly_calls.append(target)
                with ordinary_connect(target, readonly=True) as conn:
                    yield conn

            with self._d10_cache_with_tampered_uncheckpointed_wal(path), mock.patch.dict(
                os.environ,
                {"DRAMAWAVE_ATTRIBUTION_DB_PATH": str(path)},
                clear=False,
            ), mock.patch(
                "sys.argv",
                ["service.py", "--skip-mount-check"],
            ), mock.patch.object(
                service,
                "connect_sqlite",
                readonly_only,
            ), mock.patch.object(
                service,
                "ThreadingHTTPServer",
            ) as server, mock.patch("builtins.print"):
                self.assertEqual(service.main(), 2)
                self.assertEqual(readonly_calls, [path])
                server.assert_not_called()

    def test_empty_d10_cache_is_refreshable_but_not_web_readable_until_published(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                metadata = common.validate_cache_contract(
                    conn,
                    allow_unpublished_empty=True,
                )
                self.assertNotIn("data_version", metadata)
            with common.connect_sqlite(path, readonly=True) as conn:
                with self.assertRaisesRegex(service.RequestError, "data_version is missing") as error:
                    service.meta_payload(conn)
                self.assertEqual(error.exception.status, 503)

    def test_fresh_cache_bootstrap_creates_only_d10_schema_columns(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                columns = {
                    table: {
                        row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    for table in (
                        "attribution_fact",
                        "refresh_fact_stage",
                        "refresh_revenue_stage",
                        "refresh_log",
                        "attribution_filter_daily",
                        "attribution_campaign_daily",
                    )
                }

        legacy_prefix = "d" + str(30)
        for table, names in columns.items():
            self.assertFalse(
                any(legacy_prefix in name for name in names),
                f"legacy attribution column remained in {table}",
            )
        self.assertIn("d10_revenue_iaa_d0", columns["attribution_fact"])
        self.assertIn("d10_candidate_keys", columns["refresh_fact_stage"])
        self.assertIn("d10_present", columns["refresh_revenue_stage"])
        self.assertIn("source_d10_updated_at", columns["refresh_log"])
        self.assertEqual(
            refresh_cache.D10_TABLE,
            "kunlunads_dev.ads_app_revenues_10d",
        )

    def test_cache_contract_rejects_partial_and_mixed_schemas(self):
        legacy_column = ("d" + str(30)) + "_revenue_iaa_d0"
        for case in ("partial", "mixed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tempdir:
                path = Path(tempdir) / "dashboard-d10.sqlite3"
                with common.connect_sqlite(path) as conn:
                    mark_published_d10_cache(conn, "v-test")
                    if case == "partial":
                        columns = [
                            row["name"]
                            for row in conn.execute("PRAGMA table_info(attribution_fact)")
                            if row["name"] != "d10_revenue_iap_d0"
                        ]
                        selected_columns = ",".join(columns)
                        conn.execute(
                            "CREATE TABLE attribution_fact_partial AS "
                            f"SELECT {selected_columns} FROM attribution_fact"
                        )
                        conn.execute("DROP TABLE attribution_fact")
                        conn.execute(
                            "ALTER TABLE attribution_fact_partial "
                            "RENAME TO attribution_fact"
                        )
                    else:
                        conn.execute(
                            "ALTER TABLE attribution_fact ADD COLUMN "
                            f"{legacy_column} REAL NOT NULL DEFAULT 0"
                        )
                    with self.assertRaisesRegex(
                        common.CacheContractError,
                        "D10 cache contract rejected",
                    ):
                        common.validate_cache_contract(conn)

    def test_43_non_date_fact_columns_round_trip_through_row_stage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            columns = common.FACT_DIMENSIONS + common.BASE_METRICS
            self.assertEqual(len(columns) - 1, 43)
            row = {}
            for index, column in enumerate(columns):
                if column == "dt":
                    row[column] = "2026-08-01"
                elif column in common.BASE_METRICS:
                    row[column] = index + 0.25 if column in common.FLOAT_BASE_METRICS else index + 1
                else:
                    row[column] = f"sentinel-{index}"
            with common.connect_sqlite(path) as conn:
                common.insert_staged_facts(conn, "run", "created", [row])
                conn.execute(
                    f"INSERT INTO attribution_fact({','.join(columns)}) "
                    f"SELECT {','.join(columns)} FROM refresh_fact_stage WHERE run_id='run'"
                )
                actual = conn.execute(
                    f"SELECT {','.join(columns)} FROM attribution_fact"
                ).fetchone()
            for column in columns:
                self.assertEqual(actual[column], row[column], column)

    def test_revenue_union_is_disk_backed_and_conserves_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            day = dt.date(2026, 8, 1)
            with common.connect_sqlite(path) as conn:
                d7 = refresh_cache.stage_revenue_source(
                    conn,
                    "run",
                    day,
                    "d7",
                    iter(
                        [
                            revenue_row(revenue_iaa_d0=2, revenue_iap_d0=3),
                            revenue_row(revenue_iaa_d0=5, revenue_iap_d0=7),
                        ]
                    ),
                    batch_size=1,
                )
                d10 = refresh_cache.stage_revenue_source(
                    conn, "run", day, "d10", iter([revenue_row(revenue_iaa_d0=11, revenue_iap_d0=13)])
                )
                merged = refresh_cache.revenue_stage_stats(conn, "run", day)
                custom = refresh_cache.build_custom_day(day, iter([custom_row()]))
                mapped = refresh_cache.map_staged_revenue(
                    day,
                    custom,
                    refresh_cache.iter_staged_revenue(conn, "run", day),
                    {"d7": d7, "d10": d10},
                    merged,
                )
            fact = next(iter(mapped.iter_facts()))
            self.assertEqual(mapped.stats["revenue_union_rows"], 1)
            self.assertEqual(mapped.stats["d7_source_rows"], 2)
            self.assertEqual(mapped.stats["d7_merged_keys"], 1)
            self.assertEqual(fact["d7_revenue_iaa_d0"], 7)
            self.assertEqual(fact["d7_revenue_iap_d0"], 10)
            self.assertEqual(fact["d10_revenue_iaa_d0"], 11)
            self.assertEqual(fact["d10_revenue_iap_d0"], 13)

    def test_publish_validation_checks_each_staged_base_metric_against_stats(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            day = dt.date(2026, 8, 1)
            facts, stats = refresh_cache.map_day(
                day, [custom_row()], [revenue_row()], [revenue_row()]
            )
            with common.connect_sqlite(path) as conn:
                common.insert_staged_facts(conn, "run", "created", facts)
                self.assertEqual(refresh_cache.validate_staged_facts(conn, "run", day, stats), 1)
                conn.execute(
                    "UPDATE refresh_fact_stage SET d10_ad_impression_count=d10_ad_impression_count+1 "
                    "WHERE run_id='run'"
                )
                with self.assertRaisesRegex(RuntimeError, "d10_ad_impression_count conservation"):
                    refresh_cache.validate_staged_facts(conn, "run", day, stats)

    def test_row_stage_schema_tracks_every_publishable_fact_column(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                fact_columns = [
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(attribution_fact)")
                    if row["name"] != "id"
                ]
                stage_columns = [
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(refresh_fact_stage)")
                    if row["name"] not in {"run_id", "created_at"}
                ]
            self.assertEqual(stage_columns, fact_columns)

    def test_data_disk_gate_rejects_paths_outside_mount(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaisesRegex(RuntimeError, "cache path must stay under"):
                common.verify_data_disk(Path(tempdir) / "dashboard.sqlite3")

    def test_default_plan_always_has_today_yesterday_and_rotating_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                dates, historical = refresh_cache.choose_default_dates(conn, dt.date(2026, 8, 6))
                self.assertEqual(dates, [dt.date(2026, 8, 6), dt.date(2026, 8, 5), dt.date(2026, 8, 1)])
                self.assertEqual(historical, dt.date(2026, 8, 1))
                common.set_meta(conn, "history_cursor", "2026-08-01")
                conn.commit()
            with common.connect_sqlite(path) as conn:
                dates, historical = refresh_cache.choose_default_dates(conn, dt.date(2026, 8, 6))
                self.assertEqual(dates[-1], dt.date(2026, 8, 2))
                self.assertEqual(historical, dt.date(2026, 8, 2))

    def test_source_port_gate_rejects_anything_except_63350(self):
        env = {
            "ADMIN_MAPPING_MYSQL_HOST": "db",
            "ADMIN_MAPPING_MYSQL_PORT": "63353",
            "ADMIN_MAPPING_MYSQL_USER": "reader",
            "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
            "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "63350"):
                refresh_cache.require_source_config()

    def test_refresh_entry_rejects_bootstrap_before_august_first(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard-d10.sqlite3"
            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=None,
                bootstrap_start="2026-07-31",
                bootstrap_end="2026-08-01",
                skip_mount_check=True,
            )
            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache, "mysql_connection"
            ) as mysql:
                with self.assertRaisesRegex(ValueError, "cannot be earlier than 2026-08-01"):
                    refresh_cache.refresh(args)
                mysql.assert_not_called()

    def test_successful_refresh_builds_conserving_rollups_and_versions_them(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=["2026-08-01"],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )
            with common.connect_sqlite(path) as conn:
                conn.execute(
                    "INSERT INTO refresh_stage(run_id,dt,created_at,payload) VALUES(?,?,?,?)",
                    ("abandoned", "2026-08-01", "2026-08-06", "{}"),
                )
                common.insert_staged_facts(
                    conn,
                    "abandoned",
                    "2026-08-06",
                    [refresh_cache.blank_fact(dt.date(2026, 8, 1))],
                )
                refresh_cache.stage_revenue_source(
                    conn,
                    "abandoned",
                    dt.date(2026, 8, 1),
                    "d7",
                    iter([revenue_row()]),
                )
                conn.commit()

            @contextlib.contextmanager
            def fake_connection(_config):
                yield object()

            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache, "mysql_connection", fake_connection
            ), mock.patch.object(
                refresh_cache, "source_day_snapshot", lambda _source: contextlib.nullcontext()
            ), mock.patch.object(
                refresh_cache,
                "fetch_custom",
                return_value=([custom_row(campaign_name="", adset_name="", ad_name="")], "2026-08-06 12:00:00"),
            ), mock.patch.object(
                refresh_cache,
                "fetch_revenue",
                return_value=([revenue_row()], "2026-08-06 12:00:00"),
            ):
                result = refresh_cache.refresh(args)

            with common.connect_sqlite(path, readonly=True) as conn:
                metadata = common.get_meta(conn)
                self.assertEqual(metadata["data_version"], result["data_version"])
                self.assertEqual(metadata["rollup_version"], result["data_version"])
                self.assertEqual(metadata["comparison_window"], common.COMPARISON_WINDOW)
                self.assertEqual(
                    metadata["new_attribution_source"],
                    common.NEW_ATTRIBUTION_SOURCE,
                )
                fact_totals = refresh_cache.additive_totals(conn, service.FACT_TABLE, "2026-08-01")
                for table in (service.FILTER_ROLLUP_TABLE, service.CAMPAIGN_ROLLUP_TABLE):
                    self.assertEqual(
                        refresh_cache.additive_totals(conn, table, "2026-08-01"),
                        fact_totals,
                    )
                detail = json.loads(
                    conn.execute(
                        "SELECT detail FROM refresh_log WHERE dt=? AND status='success' ORDER BY id DESC LIMIT 1",
                        ("2026-08-01",),
                    ).fetchone()["detail"]
                )
                self.assertEqual(detail["attribution_filter_daily_rows"], 1)
                self.assertEqual(detail["attribution_campaign_daily_rows"], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) n FROM refresh_stage").fetchone()["n"], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) n FROM refresh_fact_stage").fetchone()["n"], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) n FROM refresh_revenue_stage").fetchone()["n"], 0
                )

    def test_successful_refresh_prunes_expired_facts_rollups_and_logs(self):
        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 10, 1, 4, 0, tzinfo=dt.timezone.utc)
                return value if tz is None else value.astimezone(tz)

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            expired_day = "2026-08-02"
            retained_day = "2026-10-01"
            with common.connect_sqlite(path) as conn:
                expired = refresh_cache.blank_fact(dt.date.fromisoformat(expired_day))
                expired.update(custom_row(dt=expired_day, spend=9))
                common.insert_facts(conn, [expired])
                refresh_cache.rebuild_rollups_for_day(conn, expired_day)
                conn.execute(
                    "INSERT INTO refresh_log(dt,started_at,finished_at,status,fact_rows,detail,data_version) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        expired_day,
                        "2026-08-02T11:59:00+08:00",
                        "2026-08-02T12:00:00+08:00",
                        "success",
                        1,
                        "{}",
                        "old-version",
                    ),
                )
                mark_published_d10_cache(conn, "old-version")
                conn.commit()

            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=[retained_day],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )

            @contextlib.contextmanager
            def fake_connection(_config):
                yield object()

            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache.dt, "datetime", FixedDateTime
            ), mock.patch.object(
                refresh_cache, "mysql_connection", fake_connection
            ), mock.patch.object(
                refresh_cache, "source_day_snapshot", lambda _source: contextlib.nullcontext()
            ), mock.patch.object(
                refresh_cache,
                "fetch_custom",
                return_value=(
                    [custom_row(dt=retained_day)],
                    "2026-10-01 12:00:00",
                ),
            ), mock.patch.object(
                refresh_cache,
                "fetch_revenue",
                return_value=([revenue_row()], "2026-10-01 12:00:00"),
            ):
                refresh_cache.refresh(args)

            with common.connect_sqlite(path, readonly=True) as conn:
                for table in (
                    service.FACT_TABLE,
                    service.FILTER_ROLLUP_TABLE,
                    service.CAMPAIGN_ROLLUP_TABLE,
                    "refresh_log",
                ):
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) n FROM {table} WHERE dt=?", (expired_day,)
                        ).fetchone()["n"],
                        0,
                    )
                self.assertGreater(
                    conn.execute(
                        "SELECT COUNT(*) n FROM attribution_fact WHERE dt=?", (retained_day,)
                    ).fetchone()["n"],
                    0,
                )

    def test_failed_multi_day_refresh_keeps_previous_facts_and_version(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                old_a = refresh_cache.blank_fact(dt.date(2026, 8, 1))
                old_a.update(custom_row(spend=11))
                old_b = refresh_cache.blank_fact(dt.date(2026, 8, 2))
                old_b.update(custom_row(dt="2026-08-02", spend=22))
                common.insert_facts(conn, [old_a, old_b])
                mark_published_d10_cache(conn, "old-version")
                conn.commit()

            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=["2026-08-01", "2026-08-02"],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )

            @contextlib.contextmanager
            def fake_connection(_config):
                yield object()

            def fake_custom(_source, day):
                if day == dt.date(2026, 8, 2):
                    raise RuntimeError("injected source failure")
                return [custom_row(spend=999)], "2026-08-06 12:00:00"

            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache, "mysql_connection", fake_connection
            ), mock.patch.object(
                refresh_cache, "source_day_snapshot", lambda _source: contextlib.nullcontext()
            ), mock.patch.object(refresh_cache, "fetch_custom", fake_custom), mock.patch.object(
                refresh_cache,
                "fetch_revenue",
                return_value=([revenue_row()], "2026-08-06 12:00:00"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    refresh_cache.refresh(args)

            with common.connect_sqlite(path, readonly=True) as conn:
                self.assertEqual(common.get_meta(conn)["data_version"], "old-version")
                rows = conn.execute("SELECT dt,spend FROM attribution_fact ORDER BY dt").fetchall()
                self.assertEqual([(row["dt"], row["spend"]) for row in rows], [("2026-08-01", 11), ("2026-08-02", 22)])
                self.assertEqual(conn.execute("SELECT COUNT(*) n FROM refresh_stage").fetchone()["n"], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) n FROM refresh_fact_stage").fetchone()["n"], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) n FROM refresh_revenue_stage").fetchone()["n"], 0
                )

    def test_mid_publish_failure_rolls_back_every_day_and_cleans_all_stages(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "dashboard.sqlite3"
            with common.connect_sqlite(path) as conn:
                for day, spend in (("2026-08-01", 11), ("2026-08-02", 22)):
                    old = refresh_cache.blank_fact(dt.date.fromisoformat(day))
                    old.update(custom_row(dt=day, spend=spend))
                    common.insert_facts(conn, [old])
                mark_published_d10_cache(conn, "old-version")
                conn.commit()

            args = argparse.Namespace(
                db_path=str(path),
                env_file=None,
                date=["2026-08-01", "2026-08-02"],
                bootstrap_start=None,
                bootstrap_end=None,
                skip_mount_check=True,
            )

            @contextlib.contextmanager
            def fake_connection(_config):
                yield object()

            def fake_custom(_source, day):
                return [custom_row(dt=day.isoformat(), spend=999)], "2026-08-06 12:00:00"

            original_rebuild = refresh_cache.rebuild_rollups_for_day

            def fail_second_publish(conn, day):
                if str(day) == "2026-08-02":
                    raise RuntimeError("injected publish failure")
                return original_rebuild(conn, day)

            env = {
                "ADMIN_MAPPING_MYSQL_HOST": "readonly",
                "ADMIN_MAPPING_MYSQL_PORT": "63350",
                "ADMIN_MAPPING_MYSQL_USER": "reader",
                "ADMIN_MAPPING_MYSQL_PASSWORD": "secret",
                "ADMIN_MAPPING_MYSQL_DATABASE": "kunlunads_dev",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                refresh_cache, "mysql_connection", fake_connection
            ), mock.patch.object(
                refresh_cache, "source_day_snapshot", lambda _source: contextlib.nullcontext()
            ), mock.patch.object(refresh_cache, "fetch_custom", fake_custom), mock.patch.object(
                refresh_cache,
                "fetch_revenue",
                return_value=([revenue_row()], "2026-08-06 12:00:00"),
            ), mock.patch.object(
                refresh_cache, "rebuild_rollups_for_day", fail_second_publish
            ):
                with self.assertRaisesRegex(RuntimeError, "injected publish failure"):
                    refresh_cache.refresh(args)

            with common.connect_sqlite(path, readonly=True) as conn:
                self.assertEqual(common.get_meta(conn)["data_version"], "old-version")
                rows = conn.execute("SELECT dt,spend FROM attribution_fact ORDER BY dt").fetchall()
                self.assertEqual(
                    [(row["dt"], row["spend"]) for row in rows],
                    [("2026-08-01", 11), ("2026-08-02", 22)],
                )
                for table in ("refresh_stage", "refresh_fact_stage", "refresh_revenue_stage"):
                    self.assertEqual(
                        conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"],
                        0,
                        table,
                    )


if __name__ == "__main__":
    unittest.main()

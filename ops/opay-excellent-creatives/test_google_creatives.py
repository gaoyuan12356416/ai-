import copy
import contextlib
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import opay_excellent_creatives as report
import google_creatives as google


RESOURCE = "customers/1234567890/assets/123"
CONFIG = {"ng": "NG OPay", "pk": "PK OPay"}
DIM = {9: {"product": "Opay", "material_type": 2}}


def source(**changes):
    row = {"id": "123", "resource_id": RESOURCE, "asset_type": "2", "impressions": "1000000",
           "clicks": "20000", "conversions": "100", "cost": "6000000000", "dt": "2026-07-01",
           "type": "3", "app_id": "ng", "account": "123-456-7890", "app_name": "OPay", "updated_at": "now"}
    row.update(changes)
    return row


def mapping(**changes):
    row = {"asset_name": RESOURCE, "source_id": "1001", "resource_id": "9", "source_type": "3", "source_custom_id": "9"}
    row.update(changes)
    return row


def rate_row(**changes):
    row = {"dt": "2026-07-01", "account": "123-456-7890", "currency": "NGN",
           "exchange_rate": "1363.01", "last_exchange_rate": "1376.62", "spend": "1376620", "spend_usd": "1000"}
    row.update(changes)
    return row


def normalized(rows=None, dimensions=None, currency="USD", rates=None, mapping_rows=None):
    rows = rows if rows is not None else [source()]
    mappings = google.collapse_mappings([RESOURCE], mapping_rows or [mapping()])
    values, duplicates = google.normalize_rows(report.google_context(), rows, CONFIG, mappings,
                                                DIM if dimensions is None else dimensions,
                                                {"1234567890": currency}, rates or {})
    return values, mappings, duplicates


def insert_dimension(connection):
    connection.execute("""INSERT INTO material_dim VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (9, "Opay", 2, "MGM_N6000.mp4", "https://example.myqcloud.com/a.mp4", "", "1",
                        "测试制作者", "admin_id", "MGM_N6000", "2026-01-01", "2026-01-01", 0, "now"))
    connection.commit()


class MetricTests(unittest.TestCase):
    def test_all_six_formulas(self):
        actual = report.derived_metrics(8000, 4000000, 20000, 1490, 234)
        self.assertEqual(actual["d0_cpa"], 34.188034)
        self.assertEqual(actual["cpm"], 2)
        self.assertEqual(actual["apm"], 0.0585)
        self.assertEqual(actual["ctr"], 0.005)
        self.assertEqual(actual["cvr"], 0.0745)
        self.assertAlmostEqual(actual["install_to_d0_rate"], 234 / 1490, places=7)

    def test_missing_af_and_installs_are_not_zero(self):
        actual = report.derived_metrics(6000, 1000000, 20000, None, None)
        self.assertEqual(actual, {"d0_cpa": None, "cpm": 6, "apm": None, "ctr": 0.02, "cvr": None, "install_to_d0_rate": None})
        self.assertNotIn("Infinity", report.json_bytes(actual).decode())

    def test_zero_is_distinct_from_missing(self):
        actual = report.derived_metrics(6000, 1000000, 20000, 100, 0)
        self.assertIsNone(actual["d0_cpa"])
        self.assertEqual(actual["apm"], 0)
        self.assertEqual(actual["install_to_d0_rate"], 0)
        zeros = report.derived_metrics(0, 0, 0, 0, 0)
        self.assertEqual(zeros["ctr"], 0)
        self.assertTrue(all(zeros[key] is None for key in zeros if key != "ctr"))

    def test_missing_spend_does_not_make_zero_cpm_or_cpa(self):
        actual = report.derived_metrics(None, 1000, 20, None, 5)
        self.assertIsNone(actual["cpm"])
        self.assertIsNone(actual["d0_cpa"])
        self.assertEqual(actual["apm"], 5)


class GoogleMappingTests(unittest.TestCase):
    def test_repeated_ads_map_to_one_asset(self):
        result = google.collapse_mappings([RESOURCE], [mapping(), mapping(source_id="1002")])[RESOURCE]
        self.assertEqual(result["custom_source_id"], 9)
        self.assertEqual(result["mapping_status"], "exact")
        self.assertEqual(result["mapping_rows"], 2)

    def test_conflicting_or_nonlocal_mapping_is_rejected(self):
        result = google.collapse_mappings([RESOURCE], [mapping(), mapping(resource_id="10", source_custom_id="10")])
        self.assertEqual(result[RESOURCE]["mapping_status"], "ambiguous")
        result = google.collapse_mappings([RESOURCE], [mapping(source_type="6")])
        self.assertEqual(result[RESOURCE]["mapping_status"], "invalid_source")

    def test_mapping_requires_byte_exact_resource(self):
        result = google.collapse_mappings([RESOURCE], [mapping(asset_name=RESOURCE.upper())])
        self.assertEqual(result[RESOURCE]["mapping_status"], "unmapped")

    def test_one_valid_chain_cannot_hide_an_invalid_candidate(self):
        for invalid in (mapping(source_custom_id="10"), mapping(source_type="6"), mapping(source_type="NULL")):
            result = google.collapse_mappings([RESOURCE], [mapping(), invalid])[RESOURCE]
            self.assertEqual(result["mapping_status"], "invalid_source")
            self.assertIsNone(result["custom_source_id"])

    def test_fractional_platform_conversions_do_not_become_installs(self):
        rows, _, _ = normalized([source(conversions="123.4567891234")])
        self.assertEqual(rows[0]["conversions"], 123.4567891234)
        with self.assertRaisesRegex(ValueError, "negative Google conversion"):
            normalized([source(conversions="-0.1")])
        for invalid in ("NaN", "Infinity", "1e1000"):
            with self.assertRaises(ValueError):
                normalized([source(conversions=invalid)])

    def test_material_scope_type_and_missing_metadata(self):
        for dims, status in (({}, "missing_material"), ({9: {"product": "OperaNews", "material_type": 2}}, "out_of_scope"),
                             ({9: {"product": "Opay", "material_type": 1}}, "type_mismatch")):
            rows, _, _ = normalized(dimensions=dims)
            self.assertEqual(rows[0]["mapping_status"], status)

    def test_asset_micro_units_and_campaign_grain(self):
        rows, _, _ = normalized()
        self.assertEqual(Decimal(rows[0]["usd_amount"]), Decimal("6000"))
        self.assertEqual(rows[0]["row_type"], 3)
        self.assertEqual(rows[0]["conversions"], 100)

    def test_duplicate_asset_day_is_not_multiplied(self):
        rows, _, duplicates = normalized([source(), source()])
        self.assertEqual((len(rows), duplicates), (1, 1))
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            normalized([source(), source(clicks="20001")])

    def test_resource_customer_and_app_identity_are_guarded(self):
        with self.assertRaisesRegex(ValueError, "resource/account"):
            normalized([source(account="222-222-2222")])
        with self.assertRaisesRegex(ValueError, "conflicting Google app"):
            normalized([source(app_name="OPayPakistan")])
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            normalized([source(app_id="opera")])
        rows, _, _ = normalized([source(app_id="pk", app_name="OPayPakistan")])
        self.assertEqual(rows[0]["app"], "PK OPay")


class HistoricalFxTests(unittest.TestCase):
    def test_july_uses_last_rate_when_current_has_advanced(self):
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row()])
        rate, status, currency = rates[("2026-07-01", "1234567890")]
        self.assertEqual((rate, status, currency), (Decimal("1376.62"), "historical_reconciled", "NGN"))
        rows, _, _ = normalized([source(cost="1376620000000")], currency="NGN", rates=rates)
        self.assertEqual(Decimal(rows[0]["usd_amount"]), Decimal("1000"))

    def test_current_historical_column_is_also_a_candidate(self):
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(exchange_rate="1376.62", last_exchange_rate="1371.87")])
        self.assertEqual(rates[("2026-07-01", "1234567890")][0], Decimal("1376.62"))

    def test_missing_candidate_does_not_hide_a_verified_rate(self):
        for missing in (None, "NULL", "", "NaN"):
            rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(exchange_rate="1376.62", last_exchange_rate=missing)])
            self.assertEqual(rates[("2026-07-01", "1234567890")][0], Decimal("1376.62"))
            rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(exchange_rate=missing, last_exchange_rate=missing)])
            self.assertIsNone(rates[("2026-07-01", "1234567890")][0])

    def test_unusable_historical_spend_is_an_fx_gap(self):
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(spend_usd=None)])
        self.assertEqual(rates[("2026-07-01", "1234567890")][1], "fx_unreconciled")
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(), rate_row(spend="0", spend_usd=None)])
        self.assertEqual(rates[("2026-07-01", "1234567890")][0], Decimal("1376.62"))

    def test_absent_or_unreconciled_fx_stays_unknown(self):
        rows, _, _ = normalized(currency="NGN")
        self.assertIsNone(rows[0]["usd_amount"])
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(spend_usd="900")])
        self.assertEqual(rates[("2026-07-01", "1234567890")][1], "fx_unreconciled")

    def test_ambiguous_rates_are_not_guessed(self):
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(spend="1", spend_usd="0")])
        self.assertEqual(rates[("2026-07-01", "1234567890")][1], "fx_ambiguous")

    def test_daily_rate_must_reconcile_all_campaign_rows(self):
        rates = google.resolve_fx({"1234567890": "NGN"}, [rate_row(), rate_row(spend="1363010")])
        self.assertIsNone(rates[("2026-07-01", "1234567890")][0])

    def test_currency_conflicts_fail_closed(self):
        rates = google.resolve_fx({"1234567890": "USD"}, [rate_row()])
        self.assertEqual(rates[("2026-07-01", "1234567890")][1], "currency_conflict")

    def test_measured_zero_cost_is_known_without_fx(self):
        rows, _, _ = normalized([source(cost="0")], currency="NGN")
        self.assertEqual(rows[0]["usd_amount"], "0")
        self.assertEqual(rows[0]["fx_status"], "zero_cost")


class GoogleMonthTests(unittest.TestCase):
    def build(self, connection):
        return report.build_month_payload(connection, "2026-07", "final", report.load_keyword_config(), {"overrides": {}})

    def populate(self, connection, sources, currencies=None, rates=None):
        mappings = google.collapse_mappings([RESOURCE], [mapping(), mapping(source_id="1002")])
        rows, _ = google.normalize_rows(report.google_context(), sources, CONFIG, mappings, DIM,
                                       currencies or {"1234567890": "USD"}, rates or {})
        google.store_month(report.google_context(), connection, "2026-07", rows, mappings, {"app_config": CONFIG})

    def test_google_rule_b_nullable_fields_and_real_id(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                self.populate(connection, [source(), source(resource_id="customers/1234567890/assets/999", clicks="0"), source(id="456", resource_id="customers/1234567890/campaigns/456", type="0", asset_type="0", cost="100000000000", impressions="10000000", clicks="100000")])
                payload = self.build(connection)
                rows = [row for row in payload["rows"] if row["channel"] == "Google"]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual((row["custom_source_id"], row["spend"], row["selection_rule"]), (9, 6000, "B"))
                self.assertIsNone(row["installs"])
                self.assertIsNone(row["af_d0_first_transactions"])
                self.assertIsNone(row["metrics"]["cvr"])
                self.assertEqual(row["metrics"]["cpm"], 6)
                self.assertEqual(row["platform_conversions"], 100)
                self.assertFalse(row["evidence"]["rule_a_available"])
                self.assertEqual(row["evidence"]["platform_ctr"], 0.01)
                report.json_bytes(payload)

    def test_missing_fx_on_one_day_excludes_whole_material_month(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                rates = {("2026-07-01", "1234567890"): (Decimal("1"), "historical_reconciled", "NGN")}
                self.populate(connection, [source(), source(dt="2026-07-02"), source(id="456", resource_id="campaign", type="0", asset_type="0", dt="2026-07-02")], currencies={"1234567890": "NGN"}, rates=rates)
                payload = self.build(connection)
                self.assertEqual(payload["rows"], [])
                audit = next(a for a in payload["audits"] if a["channel"] == "Google" and a["app"] == "NG OPay")
                self.assertEqual(audit["incomplete_material_count"], 1)
                self.assertIsNone(audit["platform_spend"])
                self.assertIsNone(audit["mapping_coverage"])

    def test_month_totals_preserve_subcent_precision(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                self.populate(connection, [source(cost="1004000"), source(dt="2026-07-02", cost="1004000")])
                _, _, materials = google.month_aggregates(report.google_context(), connection, "2026-07")
                self.assertEqual(materials[0]["spend_cents"], 201)

    def test_fractional_conversion_cache_and_sum(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                self.populate(connection, [source(conversions="123.4567891234"), source(dt="2026-07-02", conversions="0.1")])
                _, _, materials = google.month_aggregates(report.google_context(), connection, "2026-07")
                self.assertEqual(materials[0]["platform_conversions"], 123.5567891234)
                self.assertIsNone(materials[0]["installs"])
                self.assertIsNone(materials[0]["af_d0_count"])
                report.json_bytes({"platform_conversions": materials[0]["platform_conversions"]})

    def test_missing_campaign_disables_a_b_uses_asset_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                self.populate(connection, [source(), source(resource_id="customers/1234567890/assets/999", clicks="0")])
                payload = self.build(connection)
                self.assertEqual(len(payload["rows"]), 1)
                self.assertEqual(payload["rows"][0]["selection_rule"], "B")
                audit = next(a for a in payload["audits"] if a["channel"] == "Google" and a["app"] == "NG OPay")
                self.assertEqual(audit["baseline_missing_account_days"], 1)
                benchmark = next(b for b in payload["benchmarks"] if b["channel"] == "Google" and b["app"] == "NG OPay")
                self.assertIsNone(benchmark["ctr"])
                self.assertIsNone(benchmark["spend"])

    def test_platform_usd_missing_does_not_disable_complete_ctr(self):
        with tempfile.TemporaryDirectory() as temp:
            with contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
                insert_dimension(connection)
                self.populate(connection, [source(), source(resource_id="customers/1234567890/assets/999", clicks="0"), source(id="456", resource_id="campaign", type="0", asset_type="0", cost="100000000000", impressions="10000000", clicks="100000")])
                connection.execute("UPDATE google_insight SET usd_amount=NULL,fx_status='fx_missing' WHERE row_type=0")
                connection.commit()
                payload = self.build(connection)
                self.assertEqual(len(payload["rows"]), 1)
                self.assertEqual(payload["rows"][0]["selection_rule"], "B")
                self.assertEqual(payload["rows"][0]["evidence"]["platform_ctr"], 0.01)
                self.assertFalse(payload["rows"][0]["evidence"]["platform_cpa_available"])
                self.assertIsNone(payload["rows"][0]["evidence"]["platform_cpa"])
                self.assertIsNone(payload["rows"][0]["evidence"]["cumulative_spend_ratio"])
                audit = next(a for a in payload["audits"] if a["channel"] == "Google" and a["app"] == "NG OPay")
                self.assertEqual(audit["platform_fx_missing_rows"], 1)
                self.assertEqual(audit["platform_fx_missing_native_spend"], {"USD": 100000})
                self.assertIn("Campaign日记录", audit["message"])

    def test_google_only_does_not_fetch_meta_tiktok_or_af(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(report, "assert_read_only"), \
                mock.patch.object(report, "load_product_config") as product, \
                mock.patch.object(report, "fetch_insight_day") as insights, \
                mock.patch.object(report, "fetch_af_day") as af, \
                mock.patch.object(google, "refresh_month", return_value={"google_rows": 2}):
            report.refresh_month("2026-07", Path(temp) / "cache.sqlite3", google_only=True)
            product.assert_not_called()
            insights.assert_not_called()
            af.assert_not_called()

    def test_google_metadata_refresh_is_normal_run_only_and_once(self):
        with tempfile.TemporaryDirectory() as temp, \
                contextlib.closing(report.cache_conn(Path(temp) / "cache.sqlite3")) as connection:
            insert_dimension(connection)
            changed = dict(connection.execute("SELECT * FROM material_dim WHERE custom_source_id=9").fetchone())
            changed["maker"] = "更新制作者"
            mappings = google.collapse_mappings([RESOURCE], [mapping()])
            with mock.patch.object(report, "assert_read_only"), \
                    mock.patch.object(report, "fetch_ads_source_dims", return_value=[]), \
                    mock.patch.object(report, "fetch_material_dims", side_effect=lambda ids: [changed] if 9 in ids else []), \
                    mock.patch.object(google, "load_app_config", return_value=CONFIG), \
                    mock.patch.object(google, "fetch_month", return_value=[source()]), \
                    mock.patch.object(google, "fetch_mappings", return_value=mappings), \
                    mock.patch.object(google, "fetch_currencies", return_value={"1234567890": "USD"}), \
                    mock.patch.object(google, "fetch_fx_rows", return_value=[]):
                google.refresh_month(report.google_context(), connection, "2026-07", refresh_dimensions=False)
                self.assertEqual(connection.execute("SELECT maker FROM material_dim WHERE custom_source_id=9").fetchone()[0], "测试制作者")
                seen = set()
                google.refresh_month(report.google_context(), connection, "2026-07", refresh_dimensions=True, refreshed_material_ids=seen)
                self.assertEqual(connection.execute("SELECT maker FROM material_dim WHERE custom_source_id=9").fetchone()[0], "更新制作者")
                self.assertIn(9, seen)
                changed["maker"] = "不应重复读取"
                google.refresh_month(report.google_context(), connection, "2026-07", refresh_dimensions=True, refreshed_material_ids=seen)
                self.assertEqual(connection.execute("SELECT maker FROM material_dim WHERE custom_source_id=9").fetchone()[0], "更新制作者")


class CacheAndUpgradeTests(unittest.TestCase):
    def test_v1_cannot_be_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v1.sqlite3"
            connection = report.cache_conn(path)
            connection.execute("UPDATE cache_meta SET value='1' WHERE key='schema_version'")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "V1 cache is frozen"):
                report.cache_conn(path)

    def test_clone_is_isolated_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "v1.sqlite3", Path(temp) / "v2.sqlite3"
            connection = report.cache_conn(old)
            insert_dimension(connection)
            connection.execute("UPDATE cache_meta SET value='1' WHERE key='schema_version'")
            connection.commit()
            connection.close()
            report.clone_cache(old, new)
            with contextlib.closing(sqlite3.connect(str(old))) as original, contextlib.closing(report.cache_conn(new)) as cloned:
                self.assertEqual(original.execute("SELECT value FROM cache_meta WHERE key='schema_version'").fetchone()[0], "1")
                self.assertEqual(cloned.execute("SELECT value FROM cache_meta WHERE key='schema_version'").fetchone()[0], "2")
                self.assertEqual(cloned.execute("SELECT COUNT(*) FROM material_dim").fetchone()[0], 1)
            with self.assertRaisesRegex(RuntimeError, "new, distinct"):
                report.clone_cache(old, new)

    def test_preserve_frozen_rows_and_reject_unrelated_changes(self):
        base_row = {"channel": "Meta", "app": "NG OPay", "custom_source_id": 12, "spend": 6000,
                    "impressions": 100000, "clicks": 3000, "installs": 100, "af_d0_first_transactions": 10,
                    "selection_rule": "B", "source_status": "available"}
        baseline = {"schema_version": 1, "rows": [base_row], "audits": [], "benchmarks": []}
        changed = copy.deepcopy(baseline)
        changed["rows"][0]["source_status"] = "unavailable"
        report.preserve_non_google_snapshot(baseline, changed)
        self.assertEqual(changed["rows"][0]["source_status"], "available")
        self.assertIn("metrics", changed["rows"][0])
        changed["rows"][0]["spend"] = 6001
        with self.assertRaisesRegex(RuntimeError, "changed frozen"):
            report.preserve_non_google_snapshot(baseline, changed)

    def test_cli_google_only_requires_refresh(self):
        with self.assertRaisesRegex(SystemExit, "requires --refresh"):
            report.main(["--month", "2026-07", "--google-only", "--publish"])


if __name__ == "__main__":
    unittest.main()

"""Exact YouTube-chain regressions; no network, refresh, or selection-rule tests."""

import contextlib
import json
import sqlite3
import unittest
from decimal import Decimal
from unittest import mock

import google_creatives as google
import opay_excellent_creatives as report


RESOURCE = "customers/3390911691/assets/153068630146"
CONFIG = {"ng-external-app": "NG OPay", "pk-external-app": "PK OPay"}
CUSTOM_ID = 1508604
DIMENSIONS = {CUSTOM_ID: {"product": "Opay", "material_type": 2}}


def bridge(**changes):
    # Production-reported link IDs. App aliases below are controlled fixtures;
    # y.app_id is an INTERNAL setting ID, never a raw fact's external app_id.
    row = {
        "asset_name": RESOURCE, "source_id": "1054668302", "resource_id": "73456",
        "source_type": "6", "source_custom_id": "73456", "source_row_id": "1054668302",
        "youtube_id": "73456", "youtube_source_id": "1043181638",
        "youtube_original_source_id": "1508604", "original_source_row_id": "1043181638",
        "original_source_type": "3", "original_custom_id": "1508604",
        "youtube_app_id": "130", "youtube_app_setting_id": "130", "youtube_app_name": "OPay",
        "youtube_video_id": "ytFixture01",
    }
    row.update(changes)
    return row


def direct(**changes):
    row = {
        "asset_name": RESOURCE, "source_id": "1043181638", "resource_id": "1508604",
        "source_type": "3", "source_custom_id": "1508604", "source_row_id": "1043181638",
    }
    row.update(changes)
    return row


def fact(**changes):
    row = {
        "id": "123", "resource_id": RESOURCE, "asset_type": "2", "impressions": "1000000",
        "clicks": "20000", "conversions": "123.5", "cost": "6000000000", "dt": "2026-07-01",
        "type": "3", "app_id": "ng-external-app", "account": "339-091-1691",
        "app_name": "OPay NGN", "updated_at": "2026-07-02 00:00:00",
    }
    row.update(changes)
    return row


def normalize(mapping_rows, facts=None, dimensions=None):
    mappings = google.collapse_mappings([RESOURCE], mapping_rows)
    rows, duplicates = google.normalize_rows(
        report.google_context(), [fact()] if facts is None else facts, CONFIG, mappings,
        DIMENSIONS if dimensions is None else dimensions, {"3390911691": "USD"}, {},
    )
    return rows, duplicates, mappings


class YouTubeChainTests(unittest.TestCase):
    def assert_mapping(self, candidates, status, custom_id=None):
        result = google.collapse_mappings([RESOURCE], candidates)[RESOURCE]
        self.assertEqual(result["mapping_status"], status)
        self.assertEqual(result["custom_source_id"], custom_id)
        return result

    def test_reported_production_chain_resolves_final_custom_not_youtube_id(self):
        result = self.assert_mapping([bridge()], "exact", CUSTOM_ID)
        self.assertNotEqual(result["custom_source_id"], 73456)
        rows, _, _ = normalize([bridge()])
        self.assertEqual((rows[0]["mapping_status"], rows[0]["custom_source_id"]), ("exact", CUSTOM_ID))
        self.assertEqual(rows[0]["asset_type"], 2)

    def test_other_reported_ng_custom_targets_use_the_same_bridge_contract(self):
        for target in (2786191, 1337250, 3368139):
            with self.subTest(target=target):
                # Only the final IDs were supplied for these examples; the
                # bridge IDs deliberately reuse the controlled fixture above.
                rows, _, _ = normalize(
                    [bridge(original_custom_id=str(target), youtube_original_source_id=str(target))],
                    dimensions={target: {"product": "Opay", "material_type": 2}},
                )
                self.assertEqual((rows[0]["mapping_status"], rows[0]["custom_source_id"]), ("exact", target))

    def test_optional_original_custom_id_may_be_empty_but_never_override_final(self):
        for empty in (None, "", "  ", "NULL", "\\N"):
            with self.subTest(empty=empty):
                self.assert_mapping([bridge(youtube_original_source_id=empty)], "exact", CUSTOM_ID)
        for invalid in ("0", "73456", "1043181638", "1508605", "not-an-id", "1508604.0"):
            with self.subTest(invalid=invalid):
                self.assert_mapping([bridge(youtube_original_source_id=invalid)], "invalid_source")

    def test_remote_video_id_must_be_nonempty_on_every_selected_candidate(self):
        for missing in (None, "", "  ", "NULL", "\\N"):
            with self.subTest(missing=missing):
                bad = bridge(youtube_video_id=missing)
                self.assert_mapping([bad], "invalid_source")
                self.assert_mapping([bridge(), bad], "invalid_source")
        bad = bridge()
        del bad["youtube_video_id"]
        self.assert_mapping([bad], "invalid_source")

    def test_every_required_link_id_is_positive_and_present(self):
        fields = (
            "source_id", "resource_id", "source_custom_id", "source_row_id", "youtube_id",
            "youtube_source_id", "original_source_row_id", "original_custom_id",
            "youtube_app_id", "youtube_app_setting_id",
        )
        for field in fields:
            for invalid in (None, "", "NULL", "0", "-1", "1.5", "１２３"):
                with self.subTest(field=field, invalid=invalid):
                    self.assert_mapping([bridge(**{field: invalid})], "invalid_source")
            row = bridge()
            del row[field]
            with self.subTest(missing_field=field):
                # Required original provenance fields are supplied by the SQL
                # reader; malformed returned records are also fail-closed.
                if field in ("source_id", "resource_id", "source_custom_id"):
                    continue
                self.assert_mapping([row], "invalid_source")

    def test_all_link_equalities_and_both_source_types_are_checked(self):
        for field in (
            "source_id", "source_row_id", "resource_id", "source_custom_id", "youtube_id",
            "youtube_source_id", "original_source_row_id", "youtube_app_id", "youtube_app_setting_id",
        ):
            with self.subTest(field=field):
                self.assert_mapping([bridge(**{field: "999999"})], "invalid_source")
        for field in ("source_type", "original_source_type"):
            for invalid in (None, "NULL", "0", "2", "4", "7", "3.0"):
                with self.subTest(field=field, invalid=invalid):
                    self.assert_mapping([bridge(**{field: invalid})], "invalid_source")
        self.assert_mapping([bridge(original_source_type="6")], "invalid_source")

    def test_one_valid_candidate_cannot_hide_a_broken_bridge(self):
        for bad in (
            bridge(youtube_id=None), bridge(resource_id="999"), bridge(youtube_source_id="999"),
            bridge(youtube_original_source_id="999"), bridge(youtube_app_setting_id=None),
            bridge(original_source_type="6"), direct(source_custom_id="999"),
        ):
            with self.subTest(bad=bad):
                self.assert_mapping([bridge(), bad], "invalid_source")
                self.assert_mapping([bad, bridge()], "invalid_source")

    def test_conflicting_final_custom_targets_are_ambiguous(self):
        other = bridge(original_custom_id="2786191", youtube_original_source_id="2786191")
        self.assert_mapping([bridge(), other], "ambiguous")
        self.assert_mapping([bridge(), direct(resource_id="2786191", source_custom_id="2786191")], "ambiguous")

    def test_different_intermediate_youtube_ids_and_direct_path_may_agree(self):
        other = bridge(source_id="1054668303", source_row_id="1054668303", resource_id="73457",
                       source_custom_id="73457", youtube_id="73457", youtube_source_id="1043181639",
                       original_source_row_id="1043181639")
        result = self.assert_mapping([bridge(), other, direct(), bridge()], "exact", CUSTOM_ID)
        self.assertEqual(result["mapping_rows"], 4)
        self.assertEqual(sum(item[0] == "youtube_bridge" for item in result["provenance"]), 2)

    def test_legacy_direct_type3_video_and_image_are_preserved(self):
        for asset_type, material_type in ((2, 2), (4, 1)):
            for include_join_id in (True, False):
                with self.subTest(asset_type=asset_type, include_join_id=include_join_id):
                    candidate = direct()
                    if not include_join_id:
                        del candidate["source_row_id"]
                    rows, _, mappings = normalize(
                        [candidate], [fact(asset_type=str(asset_type))],
                        {CUSTOM_ID: {"product": "Opay", "material_type": material_type}},
                    )
                    self.assertEqual(rows[0]["mapping_status"], "exact")
                    self.assertEqual(mappings[RESOURCE]["provenance"], [("1043181638", "1508604", "3", "1508604")])
                    self.assertEqual(mappings[RESOURCE]["youtube_app_names"], [])
        self.assert_mapping([direct(source_row_id=None)], "invalid_source")
        self.assert_mapping([direct(source_row_id="999")], "invalid_source")

    def test_asset_name_is_byte_exact_and_unmapped_assets_remain_unmapped(self):
        for asset in (RESOURCE.upper(), RESOURCE + " ", RESOURCE.replace("3390911691", "1234567890")):
            self.assert_mapping([bridge(asset_name=asset)], "unmapped")
        self.assert_mapping([], "unmapped")

    def test_provenance_preserves_legacy_tuple_and_auditable_bridge_even_if_invalid(self):
        for original_id, status in (("1508604", "exact"), ("999", "invalid_source")):
            result = self.assert_mapping([bridge(youtube_original_source_id=original_id)], status,
                                         CUSTOM_ID if status == "exact" else None)
            self.assertIn(("1054668302", "73456", "6", "73456"), result["provenance"])
            self.assertIn((
                "youtube_bridge", "1054668302", "73456", "1054668302", "6", "73456", "73456",
                "1043181638", original_id, "1043181638", "3", "1508604", "130", "130", "OPay", "ytFixture01",
            ), result["provenance"])


class YouTubeNormalizationTests(unittest.TestCase):
    def test_internal_app_id_is_resolved_by_setting_name_and_report_app_key(self):
        for name in ("OPay", "OPay NGN", " opay   ngn "):
            rows, _, _ = normalize([bridge(youtube_app_name=name)])
            self.assertEqual((rows[0]["mapping_status"], rows[0]["app"]), ("exact", "NG OPay"))
        for name in ("OPayPakistan", "OPay Pakistan"):
            rows, _, _ = normalize([bridge(youtube_app_name=name)], [fact(app_id="pk-external-app", app_name=name)])
            self.assertEqual((rows[0]["mapping_status"], rows[0]["app"]), ("exact", "PK OPay"))

    def test_unknown_missing_or_other_app_name_fails_closed(self):
        for name in (None, "", "NULL", "OperaNews", "OPayPakistan", "ng-external-app", "130"):
            with self.subTest(name=name):
                rows, _, _ = normalize([bridge(youtube_app_name=name)])
                self.assertEqual(rows[0]["mapping_status"], "app_mismatch")
                # An invalid mapping still retains the measured raw fact.
                self.assertEqual((rows[0]["impressions"], Decimal(rows[0]["usd_amount"])), (1000000, Decimal("6000")))

    def test_every_bridge_app_must_match_even_with_same_final_target(self):
        for bad_name in ("OPayPakistan", "", "OperaNews"):
            rows, _, _ = normalize([bridge(), bridge(youtube_app_name=bad_name), direct()])
            self.assertEqual(rows[0]["mapping_status"], "app_mismatch")
        rows, _, _ = normalize([bridge(), bridge(youtube_app_name="OPay NGN"), direct()])
        self.assertEqual(rows[0]["mapping_status"], "exact")

    def test_mapping_app_is_checked_per_fact_without_cross_app_poisoning(self):
        rows, _, _ = normalize([bridge()], [fact(), fact(app_id="pk-external-app", app_name="OPayPakistan")])
        self.assertEqual([(row["app"], row["mapping_status"]) for row in rows],
                         [("NG OPay", "exact"), ("PK OPay", "app_mismatch")])

    def test_existing_product_material_type_and_missing_material_guards_remain(self):
        for dimensions, status in (
            ({}, "missing_material"),
            ({CUSTOM_ID: {"product": "OperaNews", "material_type": 2}}, "out_of_scope"),
            ({CUSTOM_ID: {"product": "Opay", "material_type": 1}}, "type_mismatch"),
        ):
            rows, _, _ = normalize([bridge()], dimensions=dimensions)
            self.assertEqual(rows[0]["mapping_status"], status)

    def test_youtube_bridge_never_maps_an_image_even_if_dimension_is_image(self):
        rows, _, _ = normalize([bridge()], [fact(asset_type="4")],
                               {CUSTOM_ID: {"product": "Opay", "material_type": 1}})
        self.assertEqual(rows[0]["mapping_status"], "type_mismatch")

    def test_multiple_mappings_and_duplicate_facts_do_not_multiply_metrics(self):
        other = bridge(resource_id="73457", source_custom_id="73457", youtube_id="73457")
        rows, duplicates, _ = normalize([bridge(), bridge(), other, direct()], [fact(), fact(id="456")])
        self.assertEqual((len(rows), duplicates), (1, 1))
        self.assertEqual((rows[0]["impressions"], rows[0]["clicks"], rows[0]["conversions"]),
                         (1000000, 20000, 123.5))
        self.assertEqual(Decimal(rows[0]["usd_amount"]), Decimal("6000"))
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            normalize([bridge(), other], [fact(), fact(clicks="20001")])

    def test_bridge_provenance_survives_existing_cache_writer(self):
        rows, _, mappings = normalize([bridge()])
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            google.ensure_schema(connection)
            google.store_month(report.google_context(), connection, "2026-07", rows, mappings, {})
            persisted = connection.execute("SELECT provenance_json FROM google_asset_mapping").fetchone()[0]
            self.assertEqual(json.loads(persisted), [list(item) for item in mappings[RESOURCE]["provenance"]])
            self.assertEqual(connection.execute("SELECT COUNT(*),SUM(clicks) FROM google_insight").fetchone(), (1, 20000))


class YouTubeMappingQueryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            ATTACH DATABASE ':memory:' AS kunlunads_dev;
            CREATE TABLE kunlunads_dev.ads_google_resource_mapping
              (asset_name TEXT, source_id INTEGER, resource_id INTEGER);
            CREATE TABLE kunlunads_dev.ads_source
              (id INTEGER PRIMARY KEY, source_type INTEGER, source_id INTEGER);
            CREATE TABLE kunlunads_dev.ads_youtube_videos
              (id INTEGER PRIMARY KEY, source_id INTEGER, original_source_id TEXT, app_id INTEGER, video_id TEXT);
            CREATE TABLE kunlunads_dev.ads_apps_setting
              (id INTEGER PRIMARY KEY, name TEXT, app_id TEXT);
            INSERT INTO kunlunads_dev.ads_source VALUES
              (1054668302,6,73456),(1043181638,3,1508604),(1054668303,6,73457);
            INSERT INTO kunlunads_dev.ads_youtube_videos VALUES
              (73456,1043181638,'1508604',130,'ytFixture01'),(73457,1043181638,'1508604',130,'ytFixture02');
            INSERT INTO kunlunads_dev.ads_apps_setting VALUES
              (130,'OPay','ng-external-app'),(131,'OPay NGN','ng-external-app');
        """)
        self.context = report.google_context()
        self.queries = []
        self.query_row_counts = []

        def run_mysql(sql, timeout):
            self.queries.append((sql, timeout))
            rows = self.connection.execute(sql).fetchall()
            self.query_row_counts.append(len(rows))
            return rows

        self.context.run_mysql = run_mysql

    def insert_mapping(self, source_id=1054668302, resource_id=73456):
        self.connection.execute("INSERT INTO kunlunads_dev.ads_google_resource_mapping VALUES(?,?,?)",
                                (RESOURCE, source_id, resource_id))

    def test_real_sql_keeps_pk_joins_separate_from_facts_and_never_multiplies(self):
        self.insert_mapping()
        self.insert_mapping()
        self.insert_mapping(1054668303, 73457)
        self.insert_mapping(1043181638, 1508604)
        mappings = google.fetch_mappings(self.context, [RESOURCE, RESOURCE])
        self.assertEqual(self.query_row_counts, [4])
        self.assertEqual((mappings[RESOURCE]["mapping_status"], mappings[RESOURCE]["custom_source_id"]), ("exact", CUSTOM_ID))
        self.assertEqual(mappings[RESOURCE]["mapping_rows"], 4)
        self.assertEqual(mappings[RESOURCE]["youtube_app_names"], ["OPay"])
        sql, timeout = self.queries[0]
        self.assertEqual(timeout, 90)
        compact = " ".join(sql.lower().split())
        self.assertIn("left join kunlunads_dev.ads_youtube_videos y on s.source_type=6 and y.id=s.source_id", compact)
        self.assertIn("left join kunlunads_dev.ads_source original_source on original_source.id=y.source_id", compact)
        self.assertIn("left join kunlunads_dev.ads_apps_setting app_setting on app_setting.id=y.app_id", compact)
        self.assertIn("y.video_id", compact)
        self.assertNotIn("ads_google_insights", compact)
        self.assertNotIn("sum(", compact)
        rows, duplicates = google.normalize_rows(self.context, [fact(), fact()], CONFIG, mappings,
                                                  DIMENSIONS, {"3390911691": "USD"}, {})
        self.assertEqual((len(rows), duplicates, rows[0]["clicks"], Decimal(rows[0]["usd_amount"])),
                         (1, 1, 20000, Decimal("6000")))

    def test_direct_source_does_not_join_unrelated_youtube_row_with_colliding_id(self):
        self.insert_mapping(1043181638, 1508604)
        self.connection.execute("INSERT INTO kunlunads_dev.ads_youtube_videos VALUES(1508604,999,'999',999,NULL)")
        result = google.fetch_mappings(self.context, [RESOURCE])[RESOURCE]
        self.assertEqual((result["mapping_status"], result["custom_source_id"]), ("exact", CUSTOM_ID))
        self.assertEqual(result["youtube_app_names"], [])
        self.assertEqual(len(result["provenance"]), 1)

    def test_left_joins_retain_broken_candidates_instead_of_hiding_them(self):
        self.insert_mapping()
        self.insert_mapping(999, 999)
        result = google.fetch_mappings(self.context, [RESOURCE])[RESOURCE]
        self.assertEqual(result["mapping_rows"], 2)
        self.assertEqual(result["mapping_status"], "invalid_source")

    def test_missing_youtube_original_or_setting_fails_closed_in_actual_query(self):
        self.insert_mapping()
        for table, identifier in (("ads_youtube_videos", 73456), ("ads_source", 1043181638), ("ads_apps_setting", 130)):
            with self.subTest(table=table):
                self.connection.execute("SAVEPOINT missing_link")
                self.connection.execute(f"DELETE FROM kunlunads_dev.{table} WHERE id=?", (identifier,))
                result = google.fetch_mappings(self.context, [RESOURCE])[RESOURCE]
                self.assertEqual(result["mapping_rows"], 1)
                self.assertEqual(result["mapping_status"], "invalid_source")
                self.connection.execute("ROLLBACK TO missing_link")
                self.connection.execute("RELEASE missing_link")

    def test_source_resource_mismatch_is_not_filtered_out_by_sql(self):
        self.insert_mapping()
        self.insert_mapping(resource_id=73457)
        result = google.fetch_mappings(self.context, [RESOURCE])[RESOURCE]
        self.assertEqual(result["mapping_rows"], 2)
        self.assertEqual(result["mapping_status"], "invalid_source")

    def test_missing_remote_video_id_is_selected_and_rejected_not_silently_filtered(self):
        self.insert_mapping()
        self.insert_mapping(1054668303, 73457)
        for missing in (None, "", "   "):
            with self.subTest(missing=missing):
                self.connection.execute("UPDATE kunlunads_dev.ads_youtube_videos SET video_id=? WHERE id=73457", (missing,))
                result = google.fetch_mappings(self.context, [RESOURCE])[RESOURCE]
                self.assertEqual(result["mapping_rows"], 2)
                self.assertEqual(result["mapping_status"], "invalid_source")

    def test_query_is_bounded_chunked_and_skips_empty_resource_set(self):
        self.context.run_mysql = mock.Mock(return_value=[])
        resources = [f"customers/3390911691/assets/{index}" for index in range(601)]
        result = google.fetch_mappings(self.context, resources + resources)
        self.assertEqual(len(result), 601)
        self.assertEqual(self.context.run_mysql.call_count, 3)
        for call in self.context.run_mysql.call_args_list:
            self.assertLessEqual(call.args[0].count("customers/3390911691/assets/"), 300)
            self.assertEqual(call.kwargs["timeout"], 90)
        self.context.run_mysql.reset_mock()
        self.assertEqual(google.fetch_mappings(self.context, []), {})
        self.context.run_mysql.assert_not_called()


if __name__ == "__main__":
    unittest.main()

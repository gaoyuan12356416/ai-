"""Scope contract tests; the source callback is always an in-memory fake."""
from copy import deepcopy
import unittest
import tempfile
from unittest.mock import Mock

from features.fb_ad_asset_delete.core import AssetError, normalize_input
from features.fb_ad_asset_delete.source import SqlSource, q
from features.fb_ad_asset_delete.video_index import VideoIndex, ReferenceRows


def product(pid="301", parent="301", kind="App"):
    return dict(id=pid, name="Catalog " + pid, parent_id=parent, parent_name="Catalog " + parent, kind=kind)


def drama(cid="101", parent="301", series="SERIES-A", language="en", pid="301"):
    return dict(parent_id=parent, content_id=cid, series_code=series, language=language,
                name="Example " + cid, product_id=pid, product_name="Drama " + pid)


def ad(row_id="1", ad_id="1001", pid="301", original="", source="", name="contentid[101]", **extra):
    row = dict(row_id=row_id, product_id=pid, ad_id=ad_id, creative_id="2001", video_ids_raw='["3001"]',
               source_ids_raw=source, original_ids_raw=original, account_id="act_444", user_id="803",
               campaign_id="4001", ad_name=name, campaign_name="", local_status="ACTIVE")
    row.update(extra)
    return row


def material(mid="501", cid="101", pid="301", language="en"):
    return dict(id=mid, content_id=cid, product="Catalog " + pid, language=language)


class FixtureSource(SqlSource):
    def __init__(self, **fixtures):
        super().__init__(lambda *_: (_ for _ in ()).throw(AssertionError("No real SQL transport")))
        self.fixtures, self.reads = fixtures, []
        self.temp = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.temp.cleanup)
        rows = [[i+1, r["ad_id"], r["product_id"], r["account_id"], r["video_ids_raw"]]
                for i, r in enumerate(fixtures.get("references", []))]
        self.video_index = VideoIndex(self.temp.name, lambda: iter(rows))

    def read(self, sql, columns, timeout=30):
        self.reads.append((sql, columns, timeout))
        if columns == ("max_id",):
            return [{"max_id": "100"}]
        if ".ads_drama_info" in sql:
            bucket = "dramas"
        elif ".ads_custom_source" in sql:
            bucket = "materials" if "data_source_id IN" in sql else "extra_materials"
        elif ".ads_source" in sql:
            bucket = "links" if "source_id IN" in sql else "extra_links"
        elif ".ads_facebook_auto_created_data" in sql:
            bucket = "ads" if columns == self.AD_COLUMNS else "identities" if columns == ("ad_id", "product_id", "account_id") else "references"
        elif ".ads_apps_setting" in sql:
            bucket = "products"
        else:
            raise AssertionError("Unexpected SQL shape: " + sql)
        return deepcopy(self.fixtures.get(bucket, []))


class SourceTests(unittest.TestCase):
    def resolve(self, source, dramas=None, products=None):
        return source.resolve_ads(dramas or [drama()], products or [product()])

    def test_business_source_error_is_unavailable_not_empty(self):
        source = SqlSource(Mock(side_effect=RuntimeError("OperationalError")))
        with self.assertRaises(AssetError) as error:
            source.read("SELECT id FROM source", ("id",))
        self.assertEqual((error.exception.code, error.exception.status), ("source_unavailable", 503))
        with self.assertRaises(AssetError):
            source.read("DELETE FROM source", ("id",))
        self.assertEqual(source.query.call_count, 1)

    def test_sql_values_are_hex_encoded_and_schema_cannot_inject_sql(self):
        value = "abc' OR 1=1 -- 测试"
        literal = q(value)
        self.assertTrue(literal.startswith("_utf8mb4 0x"))
        self.assertNotIn("CONVERT", literal)
        self.assertNotIn(value, literal)
        self.assertEqual(bytes.fromhex(literal.split("0x", 1)[1].split(" ", 1)[0]).decode("utf-8"), value)
        with self.assertRaises(ValueError):
            SqlSource(Mock(), schema="db;DELETE FROM x")

    def test_product_catalog_assigns_app_w2a_and_checks_nonadmin_permission(self):
        query = Mock(return_value=[("301", "App drama", "14", "301", "App drama", "803"),
                                   ("401", "W2A drama", "10000", "301", "App drama", "804")])
        source = SqlSource(query, lookup_actor=lambda session: {"sub_user_id": "248"})
        products = source.list_products({"role": "user", "user_id": "user1"})
        self.assertEqual([(p["id"], p["kind"], p["parent_id"]) for p in products],
                         [("301", "App", "301"), ("401", "W2A", "301")])
        sql = query.call_args.args[0]
        self.assertIn("admin_role_users", sql)
        self.assertIn("EXISTS", sql)
        self.assertIn(q("248"), sql)
        self.assertIn("a.app_type=14", sql)

    def test_unmapped_user_has_no_product_permission_and_no_default_product(self):
        query = Mock()
        source = SqlSource(query, lookup_actor=lambda _: {})
        self.assertEqual(source.list_products({"role": "user", "user_id": "unmapped"}), [])
        query.assert_not_called()
        source.list_products = Mock(return_value=[product()])
        with self.assertRaises(AssetError) as error:
            source.selected_products({"role": "admin"}, ["826"])
        self.assertEqual((error.exception.code, error.exception.status), ("product_permission_denied", 403))

    def test_w2a_is_selected_independently_without_adding_parent_app(self):
        source = SqlSource(Mock())
        source.list_products = Mock(return_value=[product(), product("401", "301", "W2A")])
        selected = source.selected_products({}, ["401"])
        self.assertEqual([p["id"] for p in selected], ["401"])
        self.assertEqual(selected[0]["parent_id"], "301")

    def test_content_id_selects_only_exact_language_version(self):
        source = FixtureSource(dramas=[drama(), drama("102", language="fr")])
        rows, blockers = source.resolve_dramas("content_id", ["101"], [product()])
        self.assertEqual([(d["content_id"], d["language"]) for d in rows], [("101", "en")])
        self.assertEqual(blockers, [])

    def test_resource_expands_all_languages_for_only_selected_product(self):
        source = FixtureSource(dramas=[drama(), drama("102", language="fr"), drama("103", parent="999", language="es")])
        rows, blockers = source.resolve_dramas("series_code", ["SERIES-A"], [product("401", "301", "W2A")])
        self.assertEqual({(d["product_id"], d["content_id"], d["language"]) for d in rows},
                         {("401", "101", "en"), ("401", "102", "fr")})
        self.assertEqual(blockers, [])
        self.assertNotIn("release_status", source.reads[0][0])
        self.assertNotIn("deploy_time", source.reads[0][0])

    def test_resource_comparison_uses_same_normalization_as_input(self):
        source = FixtureSource(dramas=[drama(series="series-a")])
        input_type, ids, _ = normalize_input({"input_type": "series_code", "ids": ["series-a"], "product_ids": ["301"]})
        rows, blockers = source.resolve_dramas(input_type, ids, [product()])
        self.assertEqual([row["content_id"] for row in rows], ["101"])
        self.assertEqual(blockers, [])

    def test_conflicting_drama_identity_and_missing_parent_are_explicit_blockers(self):
        source = FixtureSource(dramas=[drama(), drama(language="fr")])
        rows, blockers = source.resolve_dramas("content_id", ["101"], [product()])
        self.assertEqual(rows, [])
        self.assertEqual(blockers[0]["code"], "ambiguous_drama")
        source = FixtureSource()
        rows, blockers = source.resolve_dramas("content_id", ["101"], [product("401", "0", "W2A")])
        self.assertEqual(rows, [])
        self.assertEqual(blockers[0]["code"], "drama_not_found")
        self.assertEqual(source.reads, [])

    def test_source_lineage_matches_without_name_markers_and_includes_history(self):
        source = FixtureSource(materials=[material()], links=[dict(id="701", material_id="501")],
                               ads=[ad(original="501", source="701", name="historic title", local_status="DELETED")])
        rows, _ = self.resolve(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["content_ids"], rows[0]["account_id"], rows[0]["reason"]), (["101"], "444", ""))
        self.assertEqual(rows[0]["local_status"], "DELETED")
        self.assertEqual(rows[0]["video_ids"], ["3001"])
        ad_queries = [sql for sql, columns, _ in source.reads if columns == source.AD_COLUMNS]
        self.assertTrue(all("a.product IN (" + q("301") + ")" in sql for sql in ad_queries))
        self.assertTrue(all("a.status=" not in sql for sql in ad_queries))

    def test_historical_complete_markers_are_accepted_but_substrings_are_not(self):
        source = FixtureSource(ads=[ad("1", "1001", name="prefix contentid[101] suffix"),
                                   ad("2", "1002", name="historic", campaign_name="$101@2026"),
                                   ad("3", "1003", name="series 101 episode"),
                                   ad("4", "1004", name="contentid[1010]"),
                                   ad("5", "1005", name="contentid[101")])
        rows, _ = self.resolve(source)
        self.assertEqual({row["ad_id"] for row in rows}, {"1001", "1002"})

    def test_other_product_same_content_id_never_expands_the_selected_scope(self):
        source = FixtureSource(materials=[material()], ads=[ad("1", "1001", pid="401", original="501"),
                                                          ad("2", "1002", pid="301", original="501")])
        rows, _ = self.resolve(source, [drama(pid="401")], [product("401", "301", "W2A")])
        self.assertEqual([row["ad_id"] for row in rows], ["1001"])
        ad_queries = [sql for sql, columns, _ in source.reads if columns == source.AD_COLUMNS]
        self.assertTrue(all("a.product IN (" + q("401") + ")" in sql for sql in ad_queries))

    def test_mismatched_material_product_or_language_is_blocked(self):
        for source_material in (material(pid="999"), material(language="fr")):
            with self.subTest(material=source_material):
                source = FixtureSource(materials=[source_material], ads=[ad(original="501")])
                rows, _ = self.resolve(source)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["reason"], "Conflicting material lineage must not authorize deletion")

    def test_material_catalog_name_and_language_comparison_normalize_case_and_space(self):
        row = material(language=" EN ")
        row["product"] = " cAtAlOg 301 "
        source = FixtureSource(materials=[row], ads=[ad(original="501", name="historical title")])
        rows, _ = self.resolve(source)
        self.assertEqual(rows[0]["reason"], "")

    def test_out_of_scope_material_and_conflicting_marker_block_the_whole_ad(self):
        source = FixtureSource(materials=[material()], extra_materials=[material("502", "202")],
                               ads=[ad(original='["501","502"]')])
        rows, _ = self.resolve(source)
        self.assertTrue(rows[0]["reason"])
        self.assertEqual(rows[0]["content_ids"], ["101"])
        source = FixtureSource(materials=[material()], ads=[ad(original="501", name="contentid[202]")])
        rows, _ = self.resolve(source)
        self.assertTrue(rows[0]["reason"])

    def test_unresolved_part_of_multi_material_ad_blocks(self):
        source = FixtureSource(materials=[material()], links=[dict(id="701", material_id="501")],
                               ads=[ad(source='["701","702"]', original="501")])
        rows, _ = self.resolve(source)
        self.assertTrue(rows[0]["reason"])

    def test_present_but_unresolved_single_source_cannot_fall_back_to_a_name_marker(self):
        for source_id, original_id in (("999", ""), ("", "999")):
            with self.subTest(source=source_id, original=original_id):
                source = FixtureSource(ads=[ad(source=source_id, original=original_id)])
                rows, _ = self.resolve(source)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["reason"])

    def test_malformed_source_or_video_field_blocks_instead_of_dropping_bad_values(self):
        for field in ("source_ids_raw", "original_ids_raw", "video_ids_raw"):
            for value in ("unparseable", '["501","bad"]', '["501",broken]'):
                with self.subTest(field=field, value=value):
                    candidate = ad(source="701", original="501")
                    candidate[field] = value
                    source = FixtureSource(materials=[material()], links=[dict(id="701", material_id="501")], ads=[candidate])
                    rows, _ = self.resolve(source)
                    self.assertEqual(len(rows), 1)
                    self.assertTrue(rows[0]["reason"])

    def test_empty_json_arrays_are_absent_relations_and_allow_image_ad_cleanup(self):
        source = FixtureSource(ads=[ad(original="[]", source="[]", video_ids_raw="[]")])
        rows, _ = self.resolve(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "")
        self.assertEqual(rows[0]["video_ids"], [])
        source = FixtureSource(materials=[material()], ads=[ad(original='["501","999"]')])
        rows, _ = self.resolve(source)
        self.assertTrue(rows[0]["reason"])

    def test_conflicting_records_for_one_ad_and_invalid_account_are_blocked(self):
        source = FixtureSource(ads=[ad("1", "1001"), ad("2", "1001", creative_id="2002"),
                                   ad("3", "1002", account_id="bad-account")])
        rows, _ = self.resolve(source)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["reason"] for row in rows))

    def test_too_many_ad_candidates_stop_instead_of_returning_partial_scope(self):
        source = FixtureSource(ads=[ad(str(n), str(10000 + n)) for n in range(10001)])
        with self.assertRaises(AssetError) as error:
            self.resolve(source)
        self.assertEqual(error.exception.code, "too_many_ads")

    def test_shared_references_include_other_products_and_match_complete_video_ids(self):
        references = [dict(ad_id="9001", product_id="999", account_id="555", creative_id="2001", video_ids_raw='["3001","30010"]'),
                      dict(ad_id="9002", product_id="999", account_id="555", creative_id="2002", video_ids_raw="30010")]
        source = FixtureSource(references=references)
        refs = source.shared_references([dict(kind="creative", object_id="2001"), dict(kind="video", object_id="3001")])
        self.assertEqual({(row["key"], row["ad_id"], row["product_id"]) for row in refs},
                         {("creative:2001", "9001", "999"), ("video:3001", "9001", "999")})
        self.assertTrue(all("product IN" not in sql for sql, _, _ in source.reads))

    def test_incomplete_shared_reference_scan_blocks_deletion(self):
        source = SqlSource(Mock())
        source.read = Mock(return_value=[{}] * 100001)
        with self.assertRaises(AssetError) as error:
            source.shared_references([dict(kind="creative", object_id="2001")])
        self.assertEqual(error.exception.code, "reference_check_incomplete")

    def test_malformed_matching_video_reference_cannot_be_silently_dropped(self):
        source = FixtureSource(references=[dict(ad_id="9001", product_id="999", account_id="555",
                                               creative_id="2001", video_ids_raw='["3001",broken]')])
        with self.assertRaises(AssetError) as error:
            source.shared_references([dict(kind="video", object_id="3001")])
        self.assertEqual(error.exception.code, "video_index_malformed")

    def test_verified_creative_uses_account_index_without_product_filter(self):
        source = FixtureSource(references=[])
        source.shared_references([dict(kind="creative", object_id="2001", account_ids=["444"], account_verified=True)])
        sql = source.reads[0][0]
        self.assertIn("FORCE INDEX (ad_account_id)", sql)
        self.assertIn(q("444"), sql)
        self.assertIn(q("act_444"), sql)
        self.assertNotIn("product IN", sql)

    def test_all_video_targets_use_one_complete_index_and_no_regex_query(self):
        source = FixtureSource(references=[])
        source.video_index = Mock()
        source.video_index.references.return_value = ReferenceRows(proof={"generation_id": "one"})
        source.shared_references([dict(kind="video", object_id=str(3000 + i), account_ids=["444"], account_verified=True) for i in range(501)])
        self.assertEqual([], source.reads)
        source.video_index.references.assert_called_once()
        self.assertEqual(501, len(source.video_index.references.call_args.args[0]))

    def test_missing_video_index_fails_closed_without_legacy_full_scan(self):
        source = SqlSource(Mock())
        with self.assertRaises(AssetError) as error:
            source.shared_references([dict(kind="video", object_id="3001")])
        self.assertEqual(error.exception.code, "video_index_unavailable")
        source.query.assert_not_called()

    def test_global_ad_identity_conflict_is_checked_outside_selected_product(self):
        for other in (dict(ad_id="1001", product_id="999", account_id="444"),
                      dict(ad_id="1001", product_id="301", account_id="555")):
            with self.subTest(other=other):
                source = FixtureSource(ads=[ad()], identities=[dict(ad_id="1001", product_id="301", account_id="act_444"), other])
                rows, _ = self.resolve(source)
                self.assertTrue(rows[0]["reason"])
                identity_queries = [sql for sql, columns, _ in source.reads if columns == ("ad_id", "product_id", "account_id")]
                self.assertEqual(len(identity_queries), 1)
                self.assertIn("WHERE ad_id IN", identity_queries[0])
                self.assertNotIn("product IN", identity_queries[0])

    def test_matching_global_ad_identity_keeps_selected_ad_eligible(self):
        source = FixtureSource(ads=[ad()], identities=[dict(ad_id="1001", product_id="301", account_id="act_444")])
        rows, _ = self.resolve(source)
        self.assertEqual(rows[0]["reason"], "")

    def test_global_ad_identity_overflow_prevents_execution(self):
        source = FixtureSource(ads=[ad()], identities=[dict(ad_id="1001", product_id="301", account_id="444")] * 50001)
        with self.assertRaises(AssetError) as error:
            self.resolve(source)
        self.assertEqual(error.exception.code, "ad_identity_check_incomplete")


if __name__ == "__main__":
    unittest.main()

"""Fresh, bounded source lookups; all SQL executes against synthetic local data."""
import re
import sqlite3
import unittest
from unittest.mock import Mock

from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.source import SqlSource, q


class SelectedProductQueryTests(unittest.TestCase):
    def test_selected_ids_are_filtered_in_sql_and_keep_requested_order(self):
        query = Mock(return_value=[("301", "App", "14", "301", "App", "803"),
                                   ("401", "W2A", "10000", "301", "App", "804")])
        source = SqlSource(query)
        source.list_products = Mock(side_effect=AssertionError("Do not load whole catalog"))
        result = source.selected_products({"role": "admin"}, ["401", "301"])
        self.assertEqual([row["id"] for row in result], ["401", "301"])
        self.assertEqual(result[0]["parent_id"], "301")
        self.assertIn("a.id IN (" + q("401") + "," + q("301") + ")", query.call_args.args[0])
        query.assert_called_once()

    def test_each_selection_rereads_acl_and_product_default(self):
        query = Mock(side_effect=[[("401", "W2A", "10000", "301", "App", "803")],
                                  [("401", "W2A", "10000", "301", "App", "999")], []])
        actor = Mock(return_value={"sub_user_id": "248"})
        source = SqlSource(query, lookup_actor=actor)
        session = {"role": "user", "user_id": "operator"}
        self.assertEqual(source.selected_products(session, ["401"])[0]["default_user"], "803")
        self.assertEqual(source.selected_products(session, ["401"])[0]["default_user"], "999")
        with self.assertRaises(AssetError) as caught:
            source.selected_products(session, ["401"])
        self.assertEqual(caught.exception.code, "product_permission_denied")
        self.assertEqual((query.call_count, actor.call_count), (3, 3))
        for call in query.call_args_list:
            self.assertIn("admin_role_users", call.args[0])
            self.assertIn("EXISTS", call.args[0])
            self.assertIn("a.id IN (" + q("401") + ")", call.args[0])

    def test_missing_selected_product_denies_whole_selection(self):
        query = Mock(return_value=[("301", "App", "14", "301", "App", "803")])
        with self.assertRaises(AssetError) as caught:
            SqlSource(query).selected_products({"role": "admin"}, ["301", "401"])
        self.assertEqual(caught.exception.code, "product_permission_denied")

    def test_unmapped_actor_and_empty_selection_do_not_query(self):
        query = Mock()
        source = SqlSource(query)
        with self.assertRaises(AssetError):
            source.selected_products({"role": "user"}, ["301"])
        self.assertEqual(source.selected_products({"role": "admin"}, []), [])
        query.assert_not_called()


class CredentialSqlTests(unittest.TestCase):
    """Execute the combined SELECT with only MySQL literal/cast adaptation.

    This exercises joins, CASE, tuple scoping, ordering and both LIMITs instead
    of mocking a result which presupposes the desired credential was selected.
    Production MySQL parsing/EXPLAIN remains a separate read-only deploy check.
    """
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE ads_facebook_auto_created_data (
                id INTEGER PRIMARY KEY, product TEXT, ad_id TEXT, ad_account_id TEXT,
                user_id TEXT, publish_queue_id INTEGER);
            CREATE TABLE ads_template_make_queue (
                id INTEGER PRIMARY KEY, product TEXT, user_id TEXT, default_token TEXT);
            CREATE TABLE ads_apps_setting (id TEXT PRIMARY KEY, default_user TEXT);
            CREATE TABLE ads_facebook_info (user_id TEXT, facebookUserID TEXT, accessToken TEXT);
            INSERT INTO ads_apps_setting VALUES ('1','803'),('2','999');
            INSERT INTO ads_template_make_queue VALUES (500,'1','804','-1'),(501,'1','803','1');
            INSERT INTO ads_facebook_auto_created_data VALUES
                (1,'1','101','act_444','804',500),(2,'1','102','444','803',501);
            INSERT INTO ads_facebook_info VALUES
                ('803','90001','default-synthetic'),('804','90002','queue-synthetic');
        """)
        self.calls = []
        self.source = SqlSource(self.query, schema="main")
        self.obj = dict(kind="video", account_ids=["444"], user_ids=["803", "804"],
                        ad_ids=["101", "102"], product_ids=["1"])

    def query(self, sql, timeout):
        self.calls.append((sql, timeout))
        params = []
        def literal(match):
            params.append(bytes.fromhex(match[1]).decode("utf-8"))
            return "?"
        sql = re.sub(r"_utf8mb4 0x([0-9a-f]+)", literal, sql)
        sql = re.sub(r"CAST\((chosen\.[a-z_]+) AS BINARY\)", r"(CAST(\1 AS TEXT) COLLATE BINARY)", sql)
        return self.db.execute(sql, params).fetchall()

    def frozen(self, rid="1", aid="101", uid="804", pid="1"):
        return dict(source_row_id=rid, ad_id=aid, account_id="444", user_id=uid, product_id=pid)

    def resolve(self):
        return self.source.video_account_credential(self.obj, "444")

    def assert_failure(self, code):
        with self.assertRaises(AssetError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("synthetic", str(getattr(caught.exception, "detail", {})))
        return caught.exception

    def test_one_query_chooses_lowest_matching_source_and_exact_queue_user(self):
        result = self.resolve()
        self.assertEqual((result["credential_source_row_id"], result["credential_user_id"], result["token"]),
                         ("1", "804", "queue-synthetic"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 5)
        self.assertIn("ORDER BY a.id LIMIT 1", self.calls[0][0])
        self.assertTrue(self.calls[0][0].endswith("LIMIT 2"))

    def test_current_rule_and_default_token_change_are_not_cached(self):
        self.obj["video_account_sources"] = [self.frozen()]
        self.assertEqual(self.resolve()["token"], "queue-synthetic")
        self.db.execute("UPDATE ads_template_make_queue SET default_token='1' WHERE id=500")
        self.db.execute("UPDATE ads_apps_setting SET default_user='999' WHERE id='1'")
        self.db.execute("INSERT INTO ads_facebook_info VALUES ('999','90009','new-synthetic')")
        result = self.resolve()
        self.assertEqual((result["credential_user_id"], result["token"]), ("999", "new-synthetic"))
        self.assertEqual(len(self.calls), 2)

    def test_frozen_tuples_cannot_cross_product_user_and_ad_associations(self):
        self.obj["video_account_sources"] = [self.frozen(), self.frozen("2", "102", "803")]
        self.db.execute("UPDATE ads_facebook_auto_created_data SET ad_id='102' WHERE id=1")
        self.db.execute("UPDATE ads_facebook_auto_created_data SET ad_id='101' WHERE id=2")
        self.assert_failure("credential_source_missing")

    def test_legacy_preview_excludes_source_users_outside_frozen_scope_before_limit(self):
        self.obj["user_ids"] = ["803"]
        result = self.resolve()
        self.assertEqual((result["credential_source_row_id"], result["credential_user_id"]), ("2", "803"))

    def test_missing_first_queue_never_falls_through_to_later_valid_source(self):
        self.db.execute("DELETE FROM ads_template_make_queue WHERE id=500")
        error = self.assert_failure("publish_queue_missing")
        self.assertEqual(error.detail["credential_source_row_id"], "1")

    def test_mismatched_first_queue_never_falls_through_to_later_valid_source(self):
        self.db.execute("UPDATE ads_template_make_queue SET user_id='803' WHERE id=500")
        error = self.assert_failure("publish_queue_mismatch")
        self.assertEqual(error.detail["credential_publish_queue_id"], "500")

    def test_missing_selected_token_never_falls_through_to_later_identity(self):
        self.db.execute("DELETE FROM ads_facebook_info WHERE user_id='804'")
        error = self.assert_failure("publishing_token_unavailable")
        self.assertEqual(error.detail["credential_user_id"], "804")

    def test_duplicate_nonempty_selected_tokens_fail_even_if_values_equal(self):
        self.db.execute("INSERT INTO ads_facebook_info VALUES ('804','90002','queue-synthetic')")
        self.assert_failure("publishing_token_unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_blank_token_rows_do_not_hide_the_single_nonempty_token(self):
        self.db.execute("INSERT INTO ads_facebook_info VALUES ('804','90002','   ')")
        self.assertEqual(self.resolve()["token"], "queue-synthetic")

    def test_invalid_first_rule_never_reads_another_frozen_users_token(self):
        self.db.execute("UPDATE ads_template_make_queue SET default_token='0' WHERE id=500")
        self.assert_failure("publish_token_rule_unknown")

    def test_missing_default_does_not_fall_back_to_the_publishing_user(self):
        self.db.execute("UPDATE ads_template_make_queue SET default_token='1' WHERE id=500")
        self.db.execute("UPDATE ads_apps_setting SET default_user='0' WHERE id='1'")
        self.assert_failure("product_default_token_missing")

    def test_large_source_history_only_returns_first_source_credentials(self):
        self.db.executemany("INSERT INTO ads_facebook_auto_created_data VALUES (?,'1','101','444','804',500)",
                            [(rid,) for rid in range(3, 10004)])
        self.assertEqual(self.resolve()["credential_source_row_id"], "1")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()

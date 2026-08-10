import importlib.util
import re
import subprocess
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "tt_minis_multi_dim_dashboard.py"


def load_generator():
    sys.modules.setdefault("opera_product_daily_dashboard", types.ModuleType("opera_product_daily_dashboard"))
    spec = importlib.util.spec_from_file_location("tt_minis_source_query_generator", str(MODULE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata_tuple(metric_id):
    values = {
        "metric_id": str(metric_id),
        "product_id": "3346",
        "ac_user_id": "248",
        "ad_account_id": "7001",
        "campaign_id": "3001",
        "adset_id": "4001",
        "minis_id": "mn1yi38ikcrqhitt",
        "publish_user_id": "248",
        "campaign_name": "90001-tt-minis-campaign",
        "adset_name": "90001-tt-minis-adgroup",
        "ad_name": "90001-tt-minis-ad",
        "source_id": "5001",
        "original_source_id": "5002",
        "material_id": "5003",
        "country": "US",
        "drama_language": "en",
        "bid_type": "auto",
        "status": "ACTIVE",
        "op_status": "ENABLE",
        "ad_created_at": "2026-08-10 10:00:00",
        "optimizer_id": "248",
        "optimizer_name": "optimizer",
    }
    module = load_generator()
    return tuple(values[column] for column in module.SCOPE_METADATA_COLUMNS)


class SourceQueryOptimizationTest(unittest.TestCase):
    def setUp(self):
        self.module = load_generator()

    def test_metric_ids_are_digit_only_and_canonicalized_before_string_lookup(self):
        self.assertEqual("123", self.module.normalize_metric_id("123"))
        self.assertEqual("123", self.module.normalize_metric_id("00123"))
        self.assertEqual("", self.module.normalize_metric_id("123x"))
        self.assertEqual("", self.module.normalize_metric_id(""))

    def test_insight_queries_are_date_bounded_and_do_not_join_publish_history(self):
        calls = []

        def run_mysql(sql, timeout=0):
            calls.append((sql, timeout))
            if "category = 2" in sql:
                return [
                    (
                        "2026-08-10",
                        "101",
                        "7001",
                        "3001",
                        "4001",
                        "ad name",
                        "US",
                        "en",
                        "10.000000",
                        "5.000000",
                        "1000",
                        "100",
                        "300",
                        "2",
                    )
                ]
            return [
                (
                    "2026-08-10",
                    "3001",
                    "7001",
                    "campaign name",
                    "US",
                    "en",
                    "10.000000",
                    "5.000000",
                    "1000",
                    "100",
                    "300",
                    "2",
                )
            ]

        self.module.run_mysql = run_mysql
        ad_rows = self.module.fetch_ad_insight_rows("2026-08-09", "2026-08-10")
        campaign_rows = self.module.fetch_campaign_insight_rows("2026-08-09", "2026-08-10")

        self.assertEqual("101", ad_rows[0]["ad_id"])
        self.assertEqual("3001", campaign_rows[0]["campaign_id"])
        self.assertEqual(2, len(calls))
        for sql, timeout in calls:
            self.assertIn("FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(pcsa)", sql)
            self.assertIn("start_date BETWEEN '2026-08-09' AND '2026-08-10'", sql)
            self.assertNotIn("ads_tiktok_auto_created_data", sql)
            self.assertNotIn("tiktok_publish_template_queue", sql)
            self.assertEqual(300, timeout)

    def test_metadata_uses_indexed_id_batches_then_queue_primary_key_scope(self):
        calls = []
        self.module.SOURCE_METADATA_CHUNK_SIZE = 2

        def run_mysql(sql, timeout=0):
            calls.append((sql, timeout))
            match = re.search(r"ac0\.ad_id IN \(([^)]+)\)", sql)
            self.assertIsNotNone(match)
            return [metadata_tuple(value.strip().strip("'")) for value in match.group(1).split(",")]

        self.module.run_mysql = run_mysql
        metadata = self.module.fetch_scope_metadata("ad", ["003", "2", "1", "bad", "2"])

        self.assertEqual(["1", "2", "3"], sorted(metadata, key=int))
        self.assertEqual(2, len(calls))
        self.assertIn("ac0.ad_id IN ('1','2')", calls[0][0])
        self.assertIn("ac0.ad_id IN ('3')", calls[1][0])
        for sql, timeout in calls:
            self.assertIn("ads_tiktok_auto_created_data ac0 FORCE INDEX (ad_id)", sql)
            self.assertIn("STRAIGHT_JOIN kunlunads_dev.tiktok_publish_template_queue q0", sql)
            self.assertIn("MAX(ac0.created_at) AS latest_created_at", sql)
            self.assertIn("GROUP BY ac0.ad_id", sql)
            self.assertIn("ads_tiktok_auto_created_data ac1 FORCE INDEX (ad_id)", sql)
            self.assertIn("ac1.created_at <=> newest.latest_created_at", sql)
            self.assertIn("MAX(ac1.id) AS latest_id", sql)
            self.assertIn("WHERE ac1.product_id IN (1479,3346)", sql)
            self.assertIn("q1.minis_id = 'mn1yi38ikcrqhitt'", sql)
            self.assertEqual(0, sql.count("GROUP_CONCAT("))
            self.assertIn("AS latest_id", sql)
            self.assertIn("ads_tiktok_auto_created_data ac FORCE INDEX (PRIMARY)", sql)
            self.assertIn("ON ac.id = latest.latest_id", sql)
            self.assertIn("ac.ad_id = latest.metric_id", sql)
            self.assertIn("ac.product_id IN (1479,3346)", sql)
            self.assertIn("STRAIGHT_JOIN kunlunads_dev.tiktok_publish_template_queue q", sql)
            self.assertIn("ON q.id = ac.publish_queue_id", sql)
            self.assertGreaterEqual(sql.count("q.minis_id = 'mn1yi38ikcrqhitt'"), 1)
            self.assertIn("q0.minis_id = 'mn1yi38ikcrqhitt'", sql)
            self.assertNotIn("FROM (\n        SELECT id, user_id, minis_id", sql)
            self.assertEqual(120, timeout)

    def test_two_max_latest_fixture_prefers_time_then_id_inside_exact_scope(self):
        rows = [
            {"id": 10, "created_at": "2026-08-09 10:00:00", "product": 3346, "minis": "target"},
            {"id": 9, "created_at": "2026-08-10 10:00:00", "product": 3346, "minis": "target"},
            {"id": 11, "created_at": "2026-08-10 10:00:00", "product": 3346, "minis": "target"},
            {"id": 99, "created_at": "2026-08-11 10:00:00", "product": 9999, "minis": "target"},
            {"id": 100, "created_at": "2026-08-12 10:00:00", "product": 3346, "minis": "other"},
        ]

        def two_max(items):
            scoped = [
                item
                for item in items
                if item["product"] in (1479, 3346) and item["minis"] == "target"
            ]
            latest_created_at = max(
                (item["created_at"] for item in scoped if item["created_at"] is not None),
                default=None,
            )
            return max(
                item["id"]
                for item in scoped
                if item["created_at"] == latest_created_at
            )

        self.assertEqual(11, two_max(rows))
        self.assertEqual(
            13,
            two_max(
                [
                    {"id": 12, "created_at": None, "product": 3346, "minis": "target"},
                    {"id": 13, "created_at": None, "product": 3346, "minis": "target"},
                ]
            ),
        )

    def test_campaign_metadata_uses_campaign_index(self):
        calls = []

        def run_mysql(sql, timeout=0):
            calls.append(sql)
            return [metadata_tuple("3001")]

        self.module.run_mysql = run_mysql
        metadata = self.module.fetch_scope_metadata("campaign", ["3001"])

        self.assertIn("3001", metadata)
        self.assertEqual(1, len(calls))
        self.assertIn("ads_tiktok_auto_created_data ac0 FORCE INDEX (campaign_id)", calls[0])
        self.assertIn("ac0.campaign_id IN ('3001')", calls[0])
        self.assertIn("MAX(ac0.created_at) AS latest_created_at", calls[0])
        self.assertIn("GROUP BY ac0.campaign_id", calls[0])
        self.assertIn("MAX(ac1.id) AS latest_id", calls[0])
        self.assertEqual(0, calls[0].count("GROUP_CONCAT("))

    def test_app_revenue_scans_each_requested_date_once_then_filters_scoped_keys(self):
        calls = []

        def run_mysql(sql, timeout=0):
            calls.append((sql, timeout))
            if "'20260809'" in sql:
                return [("3001", "4"), ("outside-scope", "999")]
            return [("3001", "5"), ("outside-scope", "999")]

        self.module.run_mysql = run_mysql
        users = self.module.fetch_app_revenue_users(
            [{"campaign_id": "3001"}],
            "2026-08-09",
            "2026-08-10",
            "campaign",
        )

        self.assertEqual(
            {
                ("2026-08-09", "3001"): Decimal("4"),
                ("2026-08-10", "3001"): Decimal("5"),
            },
            users,
        )
        self.assertEqual(2, len(calls))
        self.assertIn("WHERE dt = '20260809'", calls[0][0])
        self.assertIn("WHERE dt = '20260810'", calls[1][0])
        for sql, timeout in calls:
            self.assertIn("ads_app_revenues FORCE INDEX(dt)", sql)
            self.assertIn("GROUP BY CAST(campaign_id AS CHAR)", sql)
            self.assertNotIn("campaign_id IN", sql)
            self.assertEqual(self.module.APP_REVENUE_QUERY_TIMEOUT_SECONDS, timeout)

    def test_ad_python_merge_preserves_output_and_metric_contract(self):
        insights = [
            {
                "dt": "2026-08-10",
                "ad_id": "101",
                "advertiser_id": "7009",
                "campaign_id": "3009",
                "adgroup_id": "4009",
                "ads_name": "insight ad name",
                "country_id": "CA",
                "language": "fr",
                "spend": "10.125000",
                "revenue": "5.005000",
                "impressions": "1000",
                "clicks": "100",
                "ad_impression": "300",
                "row_count": "2",
            },
            {
                "dt": "2026-08-10",
                "ad_id": "202",
                "advertiser_id": "7010",
                "campaign_id": "3010",
                "adgroup_id": "4010",
                "ads_name": "outside minis",
                "country_id": "US",
                "language": "en",
                "spend": "99",
                "revenue": "1",
                "impressions": "1",
                "clicks": "0",
                "ad_impression": "0",
                "row_count": "1",
            },
        ]
        metadata = dict(zip(self.module.SCOPE_METADATA_COLUMNS, metadata_tuple("101")))
        seen_ids = []
        self.module.fetch_ad_insight_rows = lambda _start, _end: insights

        def fetch_scope(_level, metric_ids):
            seen_ids.extend(list(metric_ids))
            return {"101": metadata}

        self.module.fetch_scope_metadata = fetch_scope
        self.module.fetch_content_mapping = lambda _rows: {
            ("90001", "en", 0): {"content_id": "content-1", "name": "mapped resource"}
        }
        self.module.fetch_app_revenue_users = lambda _rows, _start, _end, _level: {
            ("2026-08-10", "101"): Decimal("4")
        }

        rows = self.module.fetch_ad_rows_from_source("2026-08-09", "2026-08-10")

        self.assertEqual(["101", "202"], seen_ids)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(set(self.module.SOURCE_COLUMNS), set(row))
        self.assertEqual("7009", row["ad_account_id"])
        self.assertEqual("3009", row["campaign_id"])
        self.assertEqual("4009", row["adset_id"])
        self.assertEqual("dramawaveminis", row["app_id"])
        self.assertEqual("content-1", row["data_source_id"])
        self.assertEqual("mapped resource", row["resource_name"])
        self.assertEqual("US", row["country"])
        self.assertEqual("CA", row["country_group"])
        self.assertEqual("fr", row["language"])
        self.assertEqual(10.13, row["spend"])
        self.assertEqual(5.01, row["revenue"])
        self.assertEqual(0.49, row["roas"])
        self.assertEqual(4, row["installs"])
        self.assertEqual(2.53, row["cpi"])
        self.assertEqual(0.1, row["ctr"])

    def test_empty_insight_scope_does_not_query_metadata(self):
        self.module.fetch_ad_insight_rows = lambda _start, _end: []
        self.module.fetch_scope_metadata = lambda *_args: self.fail("metadata query should not run")
        self.module.fetch_content_mapping = lambda _rows: {}
        self.module.fetch_app_revenue_users = lambda *_args: {}

        self.assertEqual([], self.module.fetch_ad_rows_from_source("2026-08-10", "2026-08-10"))

    def test_mysql_runner_removes_password_from_argv_and_redacts_failure(self):
        secret = "do-not-leak-this-password"
        captured = {}
        self.module.base.mysql_cmd = lambda: [
            "mysql",
            "-hdb.example",
            "-P63350",
            "-uviewer",
            "-p" + secret,
            "-N",
            "-B",
            "-e",
        ]

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="access denied -p%s --password=%s" % (secret, secret),
            )

        with mock.patch.object(self.module.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(self.module.MySQLQueryError) as caught:
                self.module.run_mysql("SELECT 'query-must-not-be-in-error'", timeout=7)

        command_text = " ".join(captured["command"])
        error_text = str(caught.exception)
        self.assertNotIn(secret, command_text)
        self.assertNotIn("--password", command_text)
        self.assertNotIn("-p" + secret, command_text)
        self.assertEqual(secret, captured["env"]["MYSQL_PWD"])
        self.assertNotIn(secret, error_text)
        self.assertIn("MySQLQueryError(returncode=1", error_text)
        self.assertIn("<redacted>", error_text)
        self.assertNotIn("query-must-not-be-in-error", error_text)

    def test_mysql_runner_sanitizes_timeout_exception(self):
        secret = "timeout-password"
        self.module.base.mysql_cmd = lambda: ["mysql", "-p" + secret, "-N", "-B", "-e"]

        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(
                command,
                11,
                output="stdout " + secret,
                stderr="stderr --password=" + secret,
            )

        with mock.patch.object(self.module.subprocess, "run", side_effect=timeout):
            with self.assertRaises(self.module.MySQLQueryTimeout) as caught:
                self.module.run_mysql("SELECT 1", timeout=11)

        error_text = str(caught.exception)
        self.assertEqual(11, caught.exception.timeout)
        self.assertNotIn(secret, error_text)
        self.assertIn("MySQLQueryTimeout", error_text)
        self.assertIn("<redacted>", error_text)

    def test_mysql_runner_redacts_inherited_mysql_pwd(self):
        secret = "inherited-password"
        self.module.base.mysql_cmd = lambda: ["mysql", "-N", "-B", "-e"]
        result = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="access denied for " + secret,
        )

        with mock.patch.dict(self.module.os.environ, {"MYSQL_PWD": secret}):
            with mock.patch.object(self.module.subprocess, "run", return_value=result):
                with self.assertRaises(self.module.MySQLQueryError) as caught:
                    self.module.run_mysql("SELECT 1")

        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))

    def test_mysql_runner_treats_double_quotes_as_plain_tsv_data(self):
        self.module.base.mysql_cmd = lambda: ["mysql", "-N", "-B", "-e"]
        result = types.SimpleNamespace(
            returncode=0,
            stdout='"leading quote\tunmatched"quote\tplain\n',
            stderr="",
        )

        with mock.patch.object(self.module.subprocess, "run", return_value=result):
            rows = self.module.run_mysql("SELECT safe_fixture")

        self.assertEqual([["\"leading quote", 'unmatched"quote', "plain"]], rows)


if __name__ == "__main__":
    unittest.main()

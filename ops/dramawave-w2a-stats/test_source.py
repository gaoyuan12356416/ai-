"""Exercise the emitted aggregation SQL against a representative relational fixture."""

import os
import re
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from decimal import Decimal

import source


class ExactSum:
    """SQLite otherwise sums DECIMAL fixtures as binary floats, unlike MySQL."""
    def __init__(self):
        self.total = Decimal(0)

    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))

    def finalize(self):
        return format(self.total, "f")


class SqlFixture:
    """Translate only MySQL syntax; keep WHERE/JOIN/GROUP BY semantics intact."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        # Model the case-insensitive, padded source collation; the production
        # binary cast must deliberately bypass these text-comparison rules.
        self.db.create_collation(
            "SOURCE_CI",
            lambda left, right: ((left.rstrip().casefold() > right.rstrip().casefold())
                                 - (left.rstrip().casefold() < right.rstrip().casefold())),
        )
        self.db.execute("ATTACH DATABASE ':memory:' AS kunlunads_dev")
        self.db.executescript("""
          CREATE TABLE kunlunads_dev.ads_alternative_account_group (id INTEGER PRIMARY KEY);
          CREATE TABLE kunlunads_dev.ads_alternative_accounts
            (id INTEGER PRIMARY KEY, group_id INTEGER, account_id TEXT, deleted_at INTEGER);
          CREATE TABLE kunlunads_dev.ads_custom_source_insight
            (dt TEXT, product TEXT, w2a_page_id INTEGER, ad_account_id TEXT COLLATE SOURCE_CI,
             impressions INTEGER, clicks INTEGER, installs INTEGER,
             af_revenue0 TEXT, revenue TEXT, spend TEXT, data_source INTEGER);
          INSERT INTO kunlunads_dev.ads_alternative_account_group VALUES (648), (649), (650);
          INSERT INTO kunlunads_dev.ads_alternative_accounts VALUES
            (1,648,'101',0), (2,648,'202',0), (3,648,'202',0),
            (4,648,'999',1), (5,649,'777',0);
        """)
        self.db.executemany(
            "INSERT INTO kunlunads_dev.ads_custom_source_insight VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-10-31", "Dramawave", 2334, "101", 9999, 9999, 9999, "9999.00", "9999.00", "9999.00", 0),
                ("2026-11-01", "Dramawave", 2334, "101", 10, 2, 1, "1.10", "99.00", "2.30", 0),
                ("2026-11-01", "Dramawave", 2334, "202", 20, 3, 2, "2.20", "500.00", "4.50", 6),
                ("2026-11-06", "Dramawave", 2334, "101", 100, 10, 4, "0.05", "900.00", "20.00", 9),
                ("2026-11-01", "Dramawave", 2334, "999", 999, 999, 999, 999, 999, 999, 0),
                ("2026-11-01", "Dramawave", 2335, "101", 7, 1, 1, "0.70", "999.00", "1.20", 0),
                ("2026-11-01", "Dramawave", 0, "202", 3, 1, 1, "0.30", "999.00", "0.50", 0),
                ("2026-11-01", "Dramawave", None, "101", 2, 0, 0, "0.20", "999.00", "0.25", 0),
                ("2026-11-01", "Hotdrama", 2334, "101", 13, 3, 2, "1.30", "999.00", "2.60", 0),
                ("2026-11-01", "Drama-B", 1234, "101", 50, 5, 4, "4.50", "999.00", "8.00", 0),
                ("2026-11-01", "Drama-C", 4321, "202", 6, 1, 1, "0.60", "999.00", "1.00", 0),
                ("2026-11-01", None, 0, "202", 1, 1, 0, "0.10", "999.00", "0.20", 0),
                ("2026-11-01", "", None, "101", 1, 0, 0, "0.05", "999.00", "0.10", 0),
                ("2026-11-01", "Drama-B", 1234, "999", 9999, 9999, 9999, "9999.00", "9999.00", "9999.00", 0),
                ("2026-10-31", "Drama-B", 1234, "202", 9999, 9999, 9999, "9999.00", "9999.00", "9999.00", 0),
                ("2026-11-01", "Dramawave", 2334, None, 999, 999, 999, 999, 999, 999, 0),
            ],
        )
        self.queries = []
        self.db.create_function("DATE_FORMAT", 2, lambda value, fmt: value)
        self.db.create_aggregate("SUM", 1, ExactSum)

    def query(self, sql):
        self.queries.append(sql)
        self.assert_read_only(sql)
        translated = re.sub(r"FORCE\s+INDEX\s*\([^)]*\)", "", sql, flags=re.I)
        translated = re.sub(
            r"CAST\s*\(\s*(ad_account_id)\s+AS\s+BINARY\s*\)",
            r"CAST(\1 AS BLOB)", translated, flags=re.I,
        )
        return [tuple(None if value is None else str(value) for value in row)
                for row in self.db.execute(translated).fetchall()]

    @staticmethod
    def assert_read_only(sql):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssertionError("Source attempted a non-SELECT statement")


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SqlFixture()
        self.addCleanup(self.fixture.db.close)
        self.config = {"pool_id": 648}
        self.source = source.MySQLSource(self.config, query=self.fixture.query)

    def test_actual_pool_membership_excludes_deleted_and_other_pools_and_deduplicates(self):
        self.assertEqual(self.source.load_scope(self.config), ("101", "202"))

    def test_real_aggregation_includes_all_products_pages_but_keeps_pool_date_and_af_revenue0(self):
        accounts = self.source.load_scope(self.config)
        rows = self.source.read_daily(accounts)
        self.assertEqual(rows, [
            {"day": "2026-11-01", "impression": 113, "click": 17, "install": 12,
             "revenue": Decimal("11.05"), "cost": Decimal("20.65")},
            {"day": "2026-11-06", "impression": 100, "click": 10, "install": 4,
             "revenue": Decimal("0.05"), "cost": Decimal("20.00")},
        ])
        # Products B/C/other/empty/NULL and all pages are eligible; the pool and
        # 11/1 floor still exclude the unrelated account and older high values.
        self.assertEqual(rows[-1]["impression"], 100)

    def test_account_scope_is_byte_exact_despite_case_and_padded_source_collation(self):
        self.fixture.db.executemany(
            "INSERT INTO kunlunads_dev.ads_alternative_accounts VALUES (?,650,?,0)",
            [(6, "123"), (7, "act_AbC")],
        )
        self.fixture.db.executemany(
            "INSERT INTO kunlunads_dev.ads_custom_source_insight VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [("2026-11-06", "Dramawave", 2334, account, count, 1, 1, "1.00", "99.00", "1.00", 0)
             for account, count in [("123", 1), ("123 ", 100), ("0123", 100),
                                    ("act_AbC", 2), ("act_abc", 100), ("act_AbC ", 100)]],
        )
        scope = self.source.load_scope({"pool_id": 650})
        self.assertEqual(scope, ("123", "act_AbC"))
        rows = self.source.read_daily(scope)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["impression"], 3)
        self.assertEqual(rows[0]["revenue"], Decimal("2.00"))

    def test_existing_empty_pool_is_distinct_from_missing_pool(self):
        self.assertEqual(self.source.load_scope({"pool_id": 650}), ())
        with self.assertRaises(source.SourceError):
            self.source.load_scope({"pool_id": 651})

    def test_real_member_with_null_or_empty_identity_is_not_an_empty_pool(self):
        for account in (None, "", "   "):
            with self.subTest(account=account):
                self.fixture.db.execute(
                    "INSERT OR REPLACE INTO kunlunads_dev.ads_alternative_accounts VALUES (6,650,?,0)",
                    (account,),
                )
                with self.assertRaises(source.SourceError):
                    self.source.load_scope({"pool_id": 650})

    def test_empty_account_list_never_runs_a_broad_metrics_query(self):
        self.assertEqual(self.source.read_daily(()), [])
        self.assertEqual(self.fixture.queries, [])

    def test_account_literal_cannot_escape_filter(self):
        suspicious = "101') OR 1=1 --"
        self.assertEqual(self.source.read_daily((suspicious,)), [])
        self.assertNotIn(suspicious, self.fixture.queries[-1])

    def test_missing_pool_configuration_never_queries_database(self):
        for pool_id in (None, "", 0, -1, "648 OR 1=1"):
            with self.subTest(pool_id=pool_id):
                with self.assertRaises(source.SourceError):
                    self.source.load_scope({"pool_id": pool_id})
        self.assertEqual(self.fixture.queries, [])

    def test_scheduled_query_uses_shared_gate_and_keeps_password_out_of_arguments(self):
        secret = "fixture-only-mysql-secret"
        environment = {"W2A_MYSQL_USER": "fixture_user", "W2A_MYSQL_PASSWORD": secret,
                       "SQL_GATE_BYPASS": "1", "SQL_GATE_TEST_MODE": "1"}
        completed = SimpleNamespace(returncode=0, stdout="1\n42\n", stderr="")
        with patch.dict(os.environ, environment), patch.object(source.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.source._query("SELECT 42"), [("42",)])
        args = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(args[0], "/usr/local/bin/mysql-gated")
        self.assertIn("--host=101.32.56.53", args)
        self.assertIn("--port=63350", args)
        self.assertNotIn(secret, " ".join(args))
        self.assertNotIn(secret, options["input"])
        self.assertEqual(options["env"]["MYSQL_PWD"], secret)
        self.assertNotIn("SQL_GATE_BYPASS", options["env"])
        self.assertNotIn("SQL_GATE_TEST_MODE", options["env"])
        self.assertIn("START TRANSACTION READ ONLY", options["input"])
        self.assertIn("SELECT @@read_only", options["input"])
        self.assertLessEqual(options["timeout"], 120)

    def test_driver_failure_never_exposes_stderr_or_credentials(self):
        secret = "fixture-only-mysql-secret"
        with patch.dict(os.environ, {"W2A_MYSQL_USER": "fixture_user", "W2A_MYSQL_PASSWORD": secret}):
            with patch.object(source.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="driver SQL private-identity password=" + secret,
            )):
                with self.assertRaises(source.SourceError) as raised:
                    self.source._query("SELECT 42")
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn("private-identity", str(raised.exception))
            with patch.object(source.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="0\n42\n", stderr="",
            )):
                with self.assertRaisesRegex(source.SourceError, "read-only"):
                    self.source._query("SELECT 42")

    def test_only_daily_aggregate_gets_longer_bounded_timeout(self):
        candidate = source.MySQLSource(self.config)
        membership = SimpleNamespace(returncode=0, stdout="1\n648\t1\t101\n", stderr="")
        metrics = SimpleNamespace(
            returncode=0, stdout="1\n2026-11-06\t10\t1\t1\t0.25\t0.50\n", stderr="",
        )
        with patch.dict(os.environ, {
            "W2A_MYSQL_USER": "fixture_user", "W2A_MYSQL_PASSWORD": "fixture-password",
        }), patch.object(source.subprocess, "run", side_effect=[membership, metrics, membership]) as run:
            accounts = candidate.load_scope(self.config)
            self.assertEqual(candidate.read_daily(accounts)[0]["revenue"], Decimal("0.25"))
            self.assertEqual(candidate.load_scope(self.config), accounts)
        self.assertEqual(run.call_count, 3)
        limits = [(20000, 30, 110), (240000, 250, 330), (20000, 30, 110)]
        for invocation, (statement_ms, gate_seconds, outer_seconds) in zip(run.call_args_list, limits):
            options = invocation.kwargs
            self.assertEqual(invocation.args[0][0], "/usr/local/bin/mysql-gated")
            self.assertTrue(options["input"].startswith(
                "SET SESSION max_execution_time=%d;\n" % statement_ms,
            ))
            self.assertEqual(options["env"]["SQL_GATE_EXEC_TIMEOUT_SECONDS"], str(gate_seconds))
            self.assertEqual(options["timeout"], outer_seconds)
            self.assertEqual(options["env"]["SQL_GATE_WAIT_TIMEOUT_SECONDS"], "60")
            self.assertEqual(options["env"]["SQL_GATE_RETRY_DELAYS_SECONDS"], "")

    def test_invalid_source_metrics_raise_instead_of_fabricating_zero(self):
        invalid = [
            ("2026-02-29", "1", "1", "1", "1.00", "2.00"),
            ("2026-11-06", "1.5", "1", "1", "1.00", "2.00"),
            ("2026-11-06", "1", "1", "1", "NaN", "2.00"),
            ("2026-11-06", "1", "1", "1", None, "2.00"),
        ]
        for metric in invalid:
            with self.subTest(metric=metric):
                candidate = source.MySQLSource(self.config, query=lambda sql: [metric])
                with self.assertRaises(source.SourceError):
                    candidate.read_daily(("101",))


if __name__ == "__main__":
    unittest.main()

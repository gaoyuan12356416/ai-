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
        self.db.execute("ATTACH DATABASE ':memory:' AS kunlunads_dev")
        self.db.executescript("""
          CREATE TABLE kunlunads_dev.ads_alternative_account_group (id INTEGER PRIMARY KEY);
          CREATE TABLE kunlunads_dev.ads_alternative_accounts
            (id INTEGER PRIMARY KEY, group_id INTEGER, account_id TEXT, deleted_at INTEGER);
          CREATE TABLE kunlunads_dev.ads_custom_source_insight
            (dt TEXT, product TEXT, w2a_page_id INTEGER, ad_account_id TEXT,
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
                ("2026-11-01", "Dramawave", 2334, "101", 10, 2, 1, "1.10", "99.00", "2.30", 0),
                ("2026-11-01", "Dramawave", 2334, "202", 20, 3, 2, "2.20", "500.00", "4.50", 6),
                ("2026-11-06", "Dramawave", 2334, "101", 100, 10, 4, "0.05", "900.00", "20.00", 9),
                ("2026-11-01", "Dramawave", 2334, "999", 999, 999, 999, 999, 999, 999, 0),
                ("2026-11-01", "Dramawave", 2335, "101", 999, 999, 999, 999, 999, 999, 0),
                ("2026-11-01", "Hotdrama", 2334, "101", 999, 999, 999, 999, 999, 999, 0),
                ("2026-11-01", "Dramawave", 2334, None, 999, 999, 999, 999, 999, 999, 0),
            ],
        )
        self.queries = []
        self.db.create_function("DATE_FORMAT", 2, lambda value, fmt: value)
        self.db.create_aggregate("SUM", 1, ExactSum)

    @staticmethod
    def text_literal(hex_value):
        decoded = bytes.fromhex(hex_value).decode("utf-8")
        return "'" + decoded.replace("'", "''") + "'"

    def query(self, sql):
        self.queries.append(sql)
        self.assert_read_only(sql)
        translated = re.sub(r"FORCE\s+INDEX\s*\([^)]*\)", "", sql, flags=re.I)
        translated = re.sub(
            r"CONVERT\s*\(\s*0x([0-9a-f]+)\s+USING\s+utf8mb4\s*\)",
            lambda match: self.text_literal(match.group(1)), translated, flags=re.I,
        )
        translated = re.sub(
            r"_utf8mb4\s+0x([0-9a-f]+)",
            lambda match: self.text_literal(match.group(1)), translated, flags=re.I,
        )
        translated = re.sub(
            r"\b0x([0-9a-f]+)\b",
            lambda match: self.text_literal(match.group(1)), translated, flags=re.I,
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

    def test_real_aggregation_uses_af_revenue0_and_all_scope_predicates(self):
        accounts = self.source.load_scope(self.config)
        rows = self.source.read_daily(accounts)
        self.assertEqual(rows, [
            {"day": "2026-11-01", "impression": 30, "click": 5, "install": 3,
             "revenue": Decimal("3.30"), "cost": Decimal("6.80")},
            {"day": "2026-11-06", "impression": 100, "click": 10, "install": 4,
             "revenue": Decimal("0.05"), "cost": Decimal("20.00")},
        ])
        # The data_source=9 row remains eligible: no undocumented channel gate.
        self.assertEqual(rows[-1]["impression"], 100)

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

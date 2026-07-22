#!/usr/bin/env python3
"""Unit tests for the SocialKit TikTok account snapshot sync."""

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("sync_socialkit_tiktok_accounts.py")
SPEC = importlib.util.spec_from_file_location("socialkit_tt_sync", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def source_row(account_id=101, token="fake-access-token", has_metrics=1):
    return {
        "source_account_id": account_id,
        "team_id": 7,
        "main_account_id": "main-1",
        "external_account_id": "external-1",
        "account_name": "Example Account",
        "account_link": "https://www.tiktok.com/@example",
        "post_count": 10,
        "fan_count": 20,
        "view_count": 30,
        "like_count": 40,
        "comment_count": 50,
        "collect_count": 60,
        "share_count": 70,
        "access_token": token,
        "token_status": 2,
        "account_status": 2,
        "token_expires_time": 999999999999999999,
        "last_token_checked_time": 888888888888888888,
        "disable_publish": 0,
        "has_metric_snapshot": has_metrics,
        "metric_status": 1 if has_metrics else 0,
        "source_account_updated_time": 777777777777777777,
        "source_metric_updated_at": 1234567890 if has_metrics else 0,
    }


class NormalizeSourceRowsTest(unittest.TestCase):
    def test_normalizes_expected_fields(self):
        rows = MODULE.normalize_source_rows([source_row()], max_rows=10)
        self.assertEqual(rows[0]["source_account_id"], 101)
        self.assertEqual(rows[0]["access_token"], "fake-access-token")

    def test_rejects_empty_source(self):
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "zero active"):
            MODULE.normalize_source_rows([], max_rows=10)

    def test_rejects_duplicate_account_id(self):
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "duplicate"):
            MODULE.normalize_source_rows(
                [source_row(), source_row()], max_rows=10
            )

    def test_rejects_source_over_cap(self):
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "safety cap"):
            MODULE.normalize_source_rows(
                [source_row(101), source_row(102)], max_rows=1
            )


class EndpointSafetyTest(unittest.TestCase):
    def test_rejects_empty_password(self):
        config = MODULE.MysqlConfig(
            host=MODULE.SOURCE_HOST,
            port=MODULE.SOURCE_PORT,
            user="reader",
            password="",
            database=MODULE.SOURCE_DATABASE,
        )
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "password is empty"):
            MODULE.validate_config(config, role="source")

    def test_rejects_read_port_as_target(self):
        config = MODULE.MysqlConfig(
            host=MODULE.TARGET_HOST,
            port=63350,
            user="writer",
            password="fake-password",
            database=MODULE.TARGET_DATABASE,
        )
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "target endpoint"):
            MODULE.validate_config(config, role="target")

    def test_rejects_wrong_target_database(self):
        config = MODULE.MysqlConfig(
            host=MODULE.TARGET_HOST,
            port=MODULE.TARGET_PORT,
            user="writer",
            password="fake-password",
            database="kunlunads_dev",
        )
        with self.assertRaisesRegex(MODULE.SyncSafetyError, "target endpoint"):
            MODULE.validate_config(config, role="target")


class SqlAndSummarySafetyTest(unittest.TestCase):
    def test_upsert_is_fixed_to_one_table_and_parameterized(self):
        self.assertIn(
            "`ads_ai`.`tiktok_personal_account_snapshot`", MODULE.UPSERT_SQL
        )
        self.assertNotIn("fake-access-token", MODULE.UPSERT_SQL)
        values = MODULE._upsert_values(source_row(), "a" * 32)
        self.assertEqual(len(values), len(MODULE.SOURCE_TARGET_COLUMNS) + 1)

    def test_deactivation_clears_plaintext_token(self):
        self.assertIn("access_token = NULL", MODULE.DEACTIVATE_SQL)
        self.assertIn("last_seen_sync_id <> %s", MODULE.DEACTIVATE_SQL)

    def test_summary_never_contains_token_or_account_identity(self):
        summary = MODULE.build_summary(
            [source_row()], run_id="b" * 32, dry_run=True
        )
        rendered = str(summary)
        self.assertNotIn("fake-access-token", rendered)
        self.assertNotIn("Example Account", rendered)
        self.assertEqual(summary["access_tokens_present"], 1)


class FakeCursor:
    def __init__(self, fail_upsert=False):
        self.fail_upsert = fail_upsert
        self.executions = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, parameters=None):
        self.executions.append((sql, parameters))
        if sql == MODULE.UPSERT_SQL and self.fail_upsert:
            raise RuntimeError("simulated target failure")
        if sql == MODULE.DEACTIVATE_SQL:
            self.rowcount = 2
        return 1

    def fetchone(self):
        return {"db_name": MODULE.TARGET_DATABASE, "read_only": 0}


class FakeConnection:
    def __init__(self, fail_upsert=False):
        self.cursor_instance = FakeCursor(fail_upsert=fail_upsert)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class TargetTransactionTest(unittest.TestCase):
    def target_config(self):
        return MODULE.MysqlConfig(
            host=MODULE.TARGET_HOST,
            port=MODULE.TARGET_PORT,
            user="writer",
            password="fake-password",
            database=MODULE.TARGET_DATABASE,
        )

    def test_commits_upserts_then_deactivates(self):
        connection = FakeConnection()
        with mock.patch.object(MODULE, "_connect", return_value=connection):
            result = MODULE.sync_target(
                self.target_config(), [source_row()], run_id="c" * 32
            )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual(result["upsert_operations"], 1)
        self.assertEqual(result["deactivated_rows"], 2)
        executed_sql = [item[0] for item in connection.cursor_instance.executions]
        self.assertLess(
            executed_sql.index(MODULE.UPSERT_SQL),
            executed_sql.index(MODULE.DEACTIVATE_SQL),
        )

    def test_rolls_back_when_an_upsert_fails(self):
        connection = FakeConnection(fail_upsert=True)
        with mock.patch.object(MODULE, "_connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                MODULE.sync_target(
                    self.target_config(), [source_row()], run_id="d" * 32
                )
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)


class MaxRowsTest(unittest.TestCase):
    def test_max_rows_cannot_exceed_hard_cap(self):
        previous = os.environ.get("SOCIALKIT_TT_SYNC_MAX_ROWS")
        os.environ["SOCIALKIT_TT_SYNC_MAX_ROWS"] = "1001"
        try:
            with self.assertRaisesRegex(MODULE.SyncSafetyError, "between 1"):
                MODULE._max_source_rows()
        finally:
            if previous is None:
                os.environ.pop("SOCIALKIT_TT_SYNC_MAX_ROWS", None)
            else:
                os.environ["SOCIALKIT_TT_SYNC_MAX_ROWS"] = previous


if __name__ == "__main__":
    unittest.main()

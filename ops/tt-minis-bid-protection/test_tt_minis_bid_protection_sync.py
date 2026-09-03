#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

sync_spec = importlib.util.spec_from_file_location(
    "tt_minis_bid_protection_sync", ROOT / "tt_minis_bid_protection_sync.py"
)
sync = importlib.util.module_from_spec(sync_spec)
sys.modules[sync_spec.name] = sync
sync_spec.loader.exec_module(sync)

rotate_spec = importlib.util.spec_from_file_location(
    "rotate_tt_business_api_token", ROOT / "rotate_tt_business_api_token.py"
)
rotate = importlib.util.module_from_spec(rotate_spec)
rotate_spec.loader.exec_module(rotate)


class FakeResponse(object):
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def sample_candidate(level="CAMPAIGN", query_id="100", product_id=3346):
    return {
        "record_date": "2026-09-02",
        "product_id": product_id,
        "product_name": "Dramawave",
        "minis_id": "mn1yi38ikcrqhitt",
        "advertiser_id": "900",
        "data_level": level,
        "query_id": query_id,
        "campaign_id": query_id if level == "CAMPAIGN" else "101",
        "adgroup_id": query_id if level == "ADGROUP" else None,
        "source_adgroup_id": "200",
    }


class AmountAndContractTests(unittest.TestCase):
    def test_scaled_credit_uses_decimal(self):
        scaled, amount = sync.parse_scaled_credit("3000000")
        self.assertEqual(Decimal("3000000"), scaled)
        self.assertEqual(Decimal("30.00000"), amount)

    def test_zero_credit_allows_empty_currency(self):
        candidate = sample_candidate()
        rows = sync.normalize_history_records(
            [
                {
                    "data_level": "CAMPAIGN",
                    "query_id": "100",
                    "record_date": "2026-09-02",
                    "bid_protection_daily_status": "TARGET_MET",
                    "status_detail": "",
                    "credit_amount": "0",
                    "currency": "",
                }
            ],
            {"100": candidate},
            "2026-09-02",
            "CAMPAIGN",
        )
        self.assertEqual(Decimal("0.00000"), rows[0]["credit_amount"])
        self.assertEqual("", rows[0]["currency"])

    def test_unknown_daily_status_fails_closed(self):
        with self.assertRaises(sync.SyncError):
            sync.normalize_history_records(
                [
                    {
                        "query_id": "100",
                        "record_date": "2026-09-02",
                        "bid_protection_daily_status": "ACTIVE",
                        "credit_amount": "0",
                    }
                ],
                {"100": sample_candidate()},
                "2026-09-02",
                "CAMPAIGN",
            )

    def test_201_ids_split_200_plus_1(self):
        self.assertEqual([200, 1], [len(part) for part in sync.chunks(list(range(201)), 200)])

    def test_history_tasks_never_mix_advertisers(self):
        rows = []
        for advertiser in ("1", "2"):
            for index in range(3):
                item = sample_candidate("CAMPAIGN", str(index + 1))
                item["advertiser_id"] = advertiser
                rows.append(item)
        tasks = sync.history_tasks(rows)
        self.assertEqual(2, len(tasks))
        for day, advertiser, level, items in tasks:
            self.assertEqual({advertiser}, {item["advertiser_id"] for item in items})
            self.assertEqual("CAMPAIGN", level)
            self.assertEqual({day}, {item["record_date"] for item in items})


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        if hasattr(sync._THREAD_LOCAL, "session"):
            del sync._THREAD_LOCAL.session

    def test_history_request_contract_and_path(self):
        session = FakeSession(
            [FakeResponse({"code": 0, "data": {"bid_protection_records": []}})]
        )
        client = sync.TikTokBidProtectionClient(
            "unit-test-secret", max_retries=0, session_factory=lambda: session
        )
        self.assertEqual([], client.fetch_history("900", "CAMPAIGN", ["100"], "2026-09-02"))
        url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/report/bid_protection/detail/get/"))
        self.assertEqual("[\"100\"]", kwargs["params"]["query_ids"])
        self.assertEqual("2026-09-02", kwargs["params"]["start_date"])
        self.assertEqual("unit-test-secret", kwargs["headers"]["Access-Token"])

    def test_http_200_business_error_is_failure(self):
        session = FakeSession([FakeResponse({"code": 40001, "message": "denied", "request_id": "r"})])
        client = sync.TikTokBidProtectionClient(
            "unit-test-secret", max_retries=0, session_factory=lambda: session
        )
        with self.assertRaises(sync.ApiError) as caught:
            client.fetch_status("900", "CAMPAIGN", ["100"])
        self.assertEqual(40001, caught.exception.code)

    def test_empty_api_response_creates_no_rows(self):
        client = mock.Mock()
        client.fetch_history.return_value = []
        result = sync.sync_candidates(client, [sample_candidate()], workers=1, dry_run=True)
        self.assertEqual(0, result["rows"])
        self.assertEqual(0, result["missing"])
        self.assertEqual(1, result["not_applicable"])
        self.assertEqual([], result["failures"])
        client.fetch_status.assert_not_called()

    def test_successful_sparse_history_is_not_retried(self):
        client = mock.Mock()
        client.fetch_history.return_value = []
        result = sync.sync_candidates(client, [sample_candidate()], workers=1, dry_run=True)
        self.assertEqual([], result["failures"])
        self.assertEqual([], result["retry_candidates"])
        self.assertEqual(1, result["not_applicable"])


class SourceAndWriteContractTests(unittest.TestCase):
    def test_mysql_cli_places_execute_after_overridden_port(self):
        provider = lambda: [
            "mysql",
            "-hdb.example",
            "-uuser",
            "-psecret",
            "-N",
            "-B",
            "-e",
        ]
        with mock.patch.object(sync, "_MYSQL_COMMAND_PROVIDER", provider):
            command, env, secrets = sync.mysql_cli_command(sync.READ_PORT)
        self.assertEqual(["-P", "63350", "-e"], command[-3:])
        self.assertEqual(1, command.count("-e"))
        self.assertNotIn("secret", " ".join(command))
        self.assertEqual("secret", env["MYSQL_PWD"])
        self.assertEqual(("secret",), secrets)

    def test_failed_request_state_round_trips_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "failed.json")
            sync.save_retry_candidates(path, [sample_candidate()])
            loaded = sync.load_retry_candidates(path, "2026-07-05", "2026-09-02")
        self.assertEqual(1, len(loaded))
        self.assertEqual(("2026-09-02", "900", "CAMPAIGN", "100"), sync.candidate_key(loaded[0]))

    def test_insight_query_uses_exact_account_scope_and_campaign_only(self):
        captured = []

        def fake_query(sql, timeout=0):
            captured.append(sql)
            return []

        with mock.patch.object(sync, "run_mysql_query", side_effect=fake_query):
            sync.fetch_insight_candidates(
                "2026-09-02",
                "CAMPAIGN",
                {"900": sync.MINIS_PRODUCT_MAP["mn1yi38ikcrqhitt"]},
            )
        sql = captured[0]
        self.assertIn("FORCE INDEX (dt)", sql)
        self.assertNotIn("product IN", sql)
        self.assertIn("i.category = 0", sql)
        self.assertIn("i.dt = '2026-09-02'", sql)
        self.assertIn("SELECT DISTINCT account_id", sql)
        self.assertIn("account_stats like '%minis_id%'", sql)
        self.assertIn("platform_id='3'", sql)
        self.assertIn(
            "CAST(target_accounts.account_id AS UNSIGNED) = CAST(i.advertiser_id AS UNSIGNED)",
            sql,
        )
        self.assertIn("HAVING SUM(i.stat_cost) > 0", sql)

    def test_account_scope_statement_matches_operator_sql(self):
        normalized = " ".join(sync.TARGET_ACCOUNT_SQL.split()).lower()
        self.assertEqual(
            "select distinct account_id from kunlunads_dev.ads_accounts_setting "
            "where account_stats like '%minis_id%' and platform_id='3'",
            normalized,
        )

    def test_target_account_metadata_maps_all_scoped_accounts(self):
        with mock.patch.object(
            sync,
            "run_mysql_query",
            side_effect=[
                [["900"], ["901"]],
                [
                    ["900", "mn1yi38ikcrqhitt"],
                    ["901", "mnuh3eucymp1wqwt"],
                ],
            ],
        ):
            metadata = sync.fetch_target_account_metadata()
        self.assertEqual(3346, metadata["900"]["product_id"])
        self.assertEqual(3380, metadata["901"]["product_id"])

    def test_unknown_minis_id_fails_closed(self):
        with mock.patch.object(
            sync,
            "run_mysql_query",
            side_effect=[[["900"]], [["900", "unknown-minis"]]],
        ):
            with self.assertRaises(sync.SyncError):
                sync.fetch_target_account_metadata()

    def test_unique_upsert_never_changes_business_key(self):
        update_clause = sync.UPSERT_SQL.split("ON DUPLICATE KEY UPDATE", 1)[1]
        for key in ("record_date", "advertiser_id", "data_level", "query_id"):
            self.assertNotIn("%s = VALUES" % key, update_clause)
        self.assertIn("credit_amount = VALUES(credit_amount)", update_clause)

    def test_upsert_is_eligible_for_pymysql_multi_value_batching(self):
        cursors = importlib.import_module("pymysql.cursors")
        self.assertIsNotNone(cursors.RE_INSERT_VALUES.match(sync.UPSERT_SQL))

    def test_target_write_commits_each_bounded_batch(self):
        class FakeCursor(object):
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def executemany(self, sql, values):
                self.calls.append((sql, list(values)))

        class FakeConnection(object):
            def __init__(self):
                self.cursor_value = FakeCursor()
                self.commits = 0
                self.rollbacks = 0

            def cursor(self):
                return self.cursor_value

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                pass

        connection = FakeConnection()
        module = mock.Mock()
        module.connect.return_value = connection
        row = sample_candidate()
        row.update(
            protection_status="TARGET_MET",
            status_detail="",
            credit_amount_scaled=Decimal("0"),
            credit_amount=Decimal("0.00000"),
            currency="",
        )
        with mock.patch.object(sync.importlib, "import_module", return_value=module), mock.patch.object(
            sync,
            "mysql_connection_settings",
            return_value={"host": "db", "port": 63353, "user": "u", "password": "p"},
        ), mock.patch.object(sync, "WRITE_BATCH_SIZE", 2):
            self.assertEqual(3, sync.write_history_rows([row, row, row]))
        self.assertEqual(2, connection.commits)
        self.assertEqual(0, connection.rollbacks)
        self.assertEqual(sync.WRITE_MAX_STATEMENT_BYTES, connection.cursor_value.max_stmt_length)
        self.assertTrue(all(len(values[0]) == 15 for _, values in connection.cursor_value.calls))

    def test_merge_is_idempotent_on_business_key(self):
        old = sample_candidate()
        new = dict(old)
        new["product_name"] = "dramawaveminis"
        merged = sync.merge_candidates([old], [new])
        self.assertEqual(1, len(merged))
        self.assertEqual("dramawaveminis", merged[0]["product_name"])

    def test_terminal_candidate_keys_use_full_business_key(self):
        captured = []

        def fake_query(sql, timeout=0):
            captured.append(sql)
            return [["2026-09-02", "900", "CAMPAIGN", "100"]]

        with mock.patch.object(sync, "run_mysql_query", side_effect=fake_query):
            keys = sync.fetch_terminal_candidate_keys("2026-09-02")
        self.assertEqual({("2026-09-02", "900", "CAMPAIGN", "100")}, keys)
        self.assertIn("CAST(record_date AS CHAR)", captured[0])
        self.assertNotIn("%%", captured[0])
        self.assertIn("record_date = '2026-09-02'", captured[0])
        for status in sync.TERMINAL_STATUSES:
            self.assertIn(status, captured[0])

    def test_run_sync_skips_existing_terminal_candidate(self):
        args = mock.Mock(
            daily=False,
            backfill_days=None,
            start_date="2026-09-02",
            end_date=None,
            token_db="unused",
            token_key="unused",
            api_timeout=1,
            workers=1,
            dry_run=True,
            skip_pending=False,
            retry_state="",
        )
        candidate = sample_candidate()
        events = []
        empty_result = {
            "requests": 0,
            "rows": 0,
            "failures": [],
            "missing": 0,
            "not_applicable": 0,
            "retry_candidates": [],
        }
        with mock.patch.object(
            sync, "beijing_today", return_value=sync.parse_day("2026-09-03")
        ), mock.patch.object(sync, "load_access_token", return_value="unit-test-secret"), mock.patch.object(
            sync,
            "fetch_target_account_metadata",
            return_value={"900": sync.MINIS_PRODUCT_MAP["mn1yi38ikcrqhitt"]},
        ), mock.patch.object(
            sync, "build_day_candidates", return_value=[candidate]
        ), mock.patch.object(
            sync, "fetch_terminal_candidate_keys", return_value={sync.candidate_key(candidate)}
        ), mock.patch.object(
            sync, "sync_candidates", return_value=empty_result
        ) as call, mock.patch.object(
            sync, "emit", side_effect=lambda event, **fields: events.append((event, fields))
        ):
            self.assertEqual(0, sync.run_sync(args))
        self.assertEqual([], call.call_args.args[1])
        complete = [fields for event, fields in events if event == "sync_complete"][0]
        self.assertEqual(1, complete["terminal_skipped"])

    def test_daily_mode_refreshes_latest_14_completed_days(self):
        with tempfile.TemporaryDirectory() as directory:
            args = mock.Mock(
                daily=True,
                backfill_days=None,
                start_date=None,
                end_date=None,
                token_db="unused",
                token_key="unused",
                api_timeout=1,
                workers=1,
                dry_run=True,
                retry_state=os.path.join(directory, "failed.json"),
            )
            dates = []

            def build(day, data_level, metadata):
                dates.append(day)
                return []

            with mock.patch.object(
                sync, "beijing_today", return_value=sync.parse_day("2026-09-03")
            ), mock.patch.object(
                sync, "load_access_token", return_value="unit-test-secret"
            ), mock.patch.object(
                sync,
                "fetch_target_account_metadata",
                return_value={"900": sync.MINIS_PRODUCT_MAP["mn1yi38ikcrqhitt"]},
            ), mock.patch.object(
                sync, "build_day_candidates", side_effect=build
            ), mock.patch.object(sync, "emit"):
                self.assertEqual(0, sync.run_sync(args))
        self.assertEqual(14, len(dates))
        self.assertEqual("2026-08-20", dates[0])
        self.assertEqual("2026-09-02", dates[-1])

    def test_partial_api_failure_is_logged_and_exits_nonzero(self):
        args = mock.Mock(
            daily=False,
            backfill_days=None,
            start_date="2026-09-02",
            end_date=None,
            token_db="unused",
            token_key="unused",
            api_timeout=1,
            workers=1,
            dry_run=True,
            skip_pending=False,
            retry_state="",
        )
        failure = {
            "record_date": "2026-09-02",
            "advertiser_id": "900",
            "data_level": "CAMPAIGN",
            "id_count": 1,
            "error": "denied",
        }
        events = []
        with mock.patch.object(sync, "beijing_today", return_value=sync.parse_day("2026-09-03")), mock.patch.object(
            sync, "load_access_token", return_value="unit-test-secret"
        ), mock.patch.object(
            sync,
            "fetch_target_account_metadata",
            return_value={"900": sync.MINIS_PRODUCT_MAP["mn1yi38ikcrqhitt"]},
        ), mock.patch.object(sync, "build_day_candidates", return_value=[sample_candidate()]), mock.patch.object(
            sync,
            "sync_candidates",
            return_value={
                "requests": 1,
                "rows": 0,
                "failures": [failure],
                "missing": 1,
                "not_applicable": 0,
                "retry_candidates": [sample_candidate()],
            },
        ), mock.patch.object(sync, "fetch_terminal_candidate_keys", return_value=set()), mock.patch.object(
            sync, "emit", side_effect=lambda event, **fields: events.append((event, fields))
        ):
            self.assertEqual(2, sync.run_sync(args))
        self.assertIn(("request_failed", failure), events)
        complete = [fields for event, fields in events if event == "sync_complete"][0]
        self.assertEqual(1, complete["failed_account_count"])

    def test_write_failure_persists_full_attempted_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "failed.json")
            args = mock.Mock(
                daily=False,
                backfill_days=None,
                start_date="2026-09-02",
                end_date=None,
                token_db="unused",
                token_key="unused",
                api_timeout=1,
                workers=1,
                dry_run=False,
                skip_pending=False,
                retry_state=state_path,
            )
            with mock.patch.object(
                sync, "beijing_today", return_value=sync.parse_day("2026-09-03")
            ), mock.patch.object(sync, "load_access_token", return_value="unit-test-secret"), mock.patch.object(
                sync,
                "fetch_target_account_metadata",
                return_value={"900": sync.MINIS_PRODUCT_MAP["mn1yi38ikcrqhitt"]},
            ), mock.patch.object(
                sync, "build_day_candidates", return_value=[sample_candidate()]
            ), mock.patch.object(sync, "fetch_terminal_candidate_keys", return_value=set()), mock.patch.object(
                sync, "sync_candidates", side_effect=sync.SyncError("write failed")
            ), mock.patch.object(sync, "emit"
            ):
                with self.assertRaises(sync.SyncError):
                    sync.run_sync(args)
            loaded = sync.load_retry_candidates(state_path, "2026-07-05", "2026-09-02")
            self.assertEqual(1, len(loaded))

    def test_token_update_returns_snapshot_from_committing_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            token_db = TokenRotationTests().create_token_db(directory)
            old_row = rotate.load_token_row(token_db, "native_growth_default")
            with mock.patch.object(rotate, "load_token_row", side_effect=AssertionError("post-commit reload")):
                new_row = rotate.update_token_row(
                    token_db, "native_growth_default", old_row, "new-token", "changed"
                )
            self.assertEqual(rotate.token_hash("new-token"), new_row["token_hash"])


class TokenRotationTests(unittest.TestCase):
    def create_token_db(self, directory, token="old-token"):
        path = os.path.join(directory, "tokens.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE tt_business_api_tokens (
              token_key TEXT PRIMARY KEY,
              product_id INTEGER,
              access_token TEXT NOT NULL,
              token_hash TEXT NOT NULL,
              status INTEGER NOT NULL DEFAULT 1,
              purpose TEXT,
              note TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO tt_business_api_tokens "
            "(token_key, product_id, access_token, token_hash, status, purpose, note) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            ("native_growth_default", 1479, token, rotate.token_hash(token), "native", "original"),
        )
        conn.commit()
        conn.close()
        return path

    @staticmethod
    def fake_pools():
        return {product_id: [sample_candidate(product_id=product_id)] for product_id in rotate.REQUIRED_PRODUCT_IDS}

    def test_successful_rotation_uses_backup_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            token_db = self.create_token_db(directory)
            backup_dir = os.path.join(directory, "backup")
            pools = self.fake_pools()
            with mock.patch.object(rotate.sync, "emit"), mock.patch.object(
                rotate, "candidate_pool", return_value=pools
            ), mock.patch.object(
                rotate, "capture_native_growth_baseline", return_value=pools
            ), mock.patch.object(
                rotate,
                "run_compatibility_canaries",
                return_value={key: 1 for key in pools},
            ):
                backup_path = rotate.rotate(
                    token_db,
                    "native_growth_default",
                    backup_dir,
                    "new-token",
                    "2026-09-02",
                    require_production_paths=False,
                )
            self.assertTrue(os.path.exists(backup_path))
            row = rotate.load_token_row(token_db, "native_growth_default")
            self.assertEqual(rotate.token_hash("new-token"), row["token_hash"])
            self.assertNotEqual("old-token", row["access_token"])

    def test_post_commit_canary_failure_restores_old_row(self):
        with tempfile.TemporaryDirectory() as directory:
            token_db = self.create_token_db(directory)
            backup_dir = os.path.join(directory, "backup")
            pools = self.fake_pools()
            calls = {"count": 0}

            def canary(token, day, required_candidates):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise rotate.RotationError("post canary failed")
                return {key: len(values) for key, values in required_candidates.items()}

            with mock.patch.object(rotate.sync, "emit"), mock.patch.object(
                rotate, "candidate_pool", return_value=pools
            ), mock.patch.object(
                rotate, "capture_native_growth_baseline", return_value=pools
            ), mock.patch.object(
                rotate, "run_compatibility_canaries", side_effect=canary
            ):
                with self.assertRaises(rotate.RotationError):
                    rotate.rotate(
                        token_db,
                        "native_growth_default",
                        backup_dir,
                        "new-token",
                        "2026-09-02",
                        require_production_paths=False,
                    )
            row = rotate.load_token_row(token_db, "native_growth_default")
            self.assertEqual("old-token", row["access_token"])
            self.assertEqual("original", row["note"])


if __name__ == "__main__":
    unittest.main()

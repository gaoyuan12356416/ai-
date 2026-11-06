"""Contract tests for the isolated W2A daily-stats endpoint."""

import copy
import contextlib
import datetime as dt
import hashlib
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from decimal import Decimal

import w2a_stats as api


TOKEN = "fixture-token-not-a-production-secret-0123456789"
PATH = "/api/v1/dramawave/w2a/2334/daily-stats"
QUERY = "start_date=2026-11-01&end_date=2026-11-06"
FIRST = dt.datetime(2026, 11, 6, 2, 0, tzinfo=dt.timezone.utc)
SECOND = FIRST + dt.timedelta(hours=1)


def row(day="2026-11-06", revenue="128.50", **overrides):
    result = {
        "day": day,
        "impression": 10000,
        "click": 320,
        "install": 45,
        "revenue": Decimal(revenue),
        "cost": Decimal("96.20"),
    }
    result.update(overrides)
    return result


class FakeSource:
    def __init__(self, rows=None, accounts=("101", "202")):
        self.rows = list(rows or [])
        self.accounts = accounts
        self.scope_error = None
        self.data_error = None
        self.scope_calls = []
        self.data_calls = []
        self.on_read = None

    def load_scope(self, config):
        self.scope_calls.append(config["pool_id"])
        if self.scope_error:
            raise self.scope_error
        return self.accounts

    def read_daily(self, accounts):
        self.data_calls.append(tuple(accounts))
        if self.on_read:
            self.on_read()
        if self.data_error:
            raise self.data_error
        return copy.deepcopy(self.rows)


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.token_file = self.root / "token"
        self.token_file.write_text(TOKEN, encoding="utf-8")
        self.config_file = self.root / "config.json"
        self.config = {
            "pool_id": 77,
            "token_file": str(self.token_file),
            "allowed_ips": ["91.203.98.0/24", "82.145.216.0/24"],
            "trusted_proxies": ["127.0.0.1/32"],
            "state_dir": str(self.root / "state"),
            "bind_host": "127.0.0.1",
            "port": 0,
        }
        self.write_config()

    def write_config(self):
        self.config_file.write_text(json.dumps(self.config), encoding="utf-8")

    def refresh(self, source, now=FIRST):
        return api.refresh(str(self.config_file), source=source, now=now)

    def query(self, query=QUERY, peer="91.203.98.17", headers=None):
        if headers is None:
            headers = {"Authorization": "Bearer " + TOKEN}
        return api.query_response(str(self.config_file), query, peer, headers)

    def test_exact_fields_decimal_numbers_and_inclusive_sorted_range(self):
        source = FakeSource([
            row("2026-11-07"), row("2026-11-06", revenue="99999999999999.99"),
            row("2026-11-01", revenue="0.10"), row("2026-10-31"),
        ])
        self.assertEqual(self.refresh(source)["status"], "ok")
        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(result["code"], "OK")
        self.assertFalse(result["stale"])
        self.assertEqual([x["day"] for x in result["data"]], ["2026-11-01", "2026-11-06"])
        for item in result["data"]:
            self.assertEqual(set(item), {"day", "impression", "click", "install", "revenue", "cost"})
            for name in ("impression", "click", "install"):
                self.assertIsInstance(item[name], int)
        wire = api.dumps(result)
        decoded = json.loads(wire, parse_float=Decimal)
        self.assertEqual(decoded["data"][0]["revenue"], Decimal("0.10"))
        self.assertEqual(decoded["data"][1]["revenue"], Decimal("99999999999999.99"))
        self.assertNotIn('"0.10"', wire)
        self.assertEqual(source.data_calls, [("101", "202")])

    def test_empty_success_is_distinct_from_unsynced_and_unconfigured(self):
        self.assertEqual(self.query()[0], 503)
        self.assertEqual(self.query()[1]["code"], "DATA_NOT_READY")
        self.assertEqual(self.refresh(FakeSource())["status"], "ok")
        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(result["data"], [])
        self.assertTrue(result["updated_at"])
        self.config["pool_id"] = None
        self.write_config()
        untouched = FakeSource([row()])
        self.assertEqual(self.refresh(untouched)["status"], "config_not_ready")
        self.assertEqual(untouched.scope_calls, [])
        self.assertEqual(untouched.data_calls, [])
        self.assertEqual(self.query()[1]["code"], "CONFIG_NOT_READY")

    def test_empty_valid_pool_does_not_query_metrics(self):
        source = FakeSource([row()], accounts=())
        self.assertEqual(self.refresh(source)["status"], "ok")
        self.assertEqual(source.data_calls, [])
        self.assertEqual(self.query()[1]["data"], [])

    def test_daily_values_are_replaced_not_accumulated_and_deleted_days_disappear(self):
        source = FakeSource([row("2026-11-01"), row("2026-11-06", revenue="10.00")])
        self.refresh(source)
        source.rows = [row("2026-11-06", revenue="15.25", impression=500)]
        self.refresh(source, now=SECOND)
        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["revenue"], Decimal("15.25"))
        self.assertEqual(result["data"][0]["impression"], 500)

    def test_same_scope_failure_preserves_full_snapshot_and_success_time(self):
        source = FakeSource([row()])
        self.refresh(source)
        original = self.query()[1]
        source.data_error = RuntimeError("source unavailable")
        self.assertEqual(self.refresh(source, now=SECOND)["status"], "failed")
        status, failed = self.query()
        self.assertEqual(status, 200)
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["data"], original["data"])
        self.assertEqual(failed["updated_at"], original["updated_at"])
        source.data_error = None
        self.refresh(source, now=SECOND)
        self.assertFalse(self.query()[1]["stale"])
        self.assertNotEqual(self.query()[1]["updated_at"], original["updated_at"])

    def test_first_refresh_failure_does_not_look_like_empty_success(self):
        source = FakeSource()
        source.data_error = RuntimeError("source unavailable")
        self.assertEqual(self.refresh(source)["status"], "failed")
        status, result = self.query()
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "DATA_NOT_READY")

    def test_changed_membership_invalidates_old_rows_before_failed_aggregation(self):
        source = FakeSource([row()])
        self.refresh(source)
        source.accounts = ("202",)
        source.data_error = RuntimeError("new scope query failed")
        self.refresh(source, now=SECOND)
        status, result = self.query()
        self.assertEqual(status, 503)
        self.assertNotEqual(result.get("code"), "OK")
        source.data_error = None
        source.rows = [row(revenue="3.00")]
        self.refresh(source, now=SECOND)
        self.assertEqual(self.query()[1]["data"][0]["revenue"], Decimal("3.00"))

    def test_pool_config_change_immediately_blocks_old_snapshot(self):
        self.refresh(FakeSource([row()]))
        self.config["pool_id"] = 88
        self.write_config()
        self.assertEqual(self.query()[0], 503)

    def test_obsolete_policy_snapshot_is_hidden_until_current_scope_refresh(self):
        source = FakeSource([row(revenue="5.00", impression=10)])
        old_policies = [
            [self.config["pool_id"], source.accounts],
            ["dramawave-pool-all-pages-v1", "Dramawave", self.config["pool_id"], source.accounts],
        ]
        for old_policy in old_policies:
            with self.subTest(old_policy=old_policy):
                self.refresh(source)
                old_policy_hash = hashlib.sha256(json.dumps(
                    old_policy, separators=(",", ":"), ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                # Reproduce V1.1 page-only or V1.2 unrestricted-date identity.
                with contextlib.closing(sqlite3.connect(
                    str(self.root / "state" / "snapshot.sqlite3")
                )) as connection, connection:
                    connection.execute(
                        "UPDATE state SET scope_id=?, snapshot_scope=?, scope_valid=1 WHERE id=1",
                        (old_policy_hash, old_policy_hash),
                    )
                status, payload = self.query()
                self.assertEqual(status, 503)
                self.assertEqual(payload["code"], "DATA_NOT_READY")
                self.assertFalse(api.health(str(self.config_file))["has_snapshot"])
                source.data_error = RuntimeError("first current-scope read unavailable")
                self.assertEqual(self.refresh(source, now=SECOND)["status"], "failed")
                self.assertEqual(self.query()[0], 503)
                self.assertFalse(api.health(str(self.config_file))["has_snapshot"])
                source.data_error = None
                source.rows = [row(revenue="18.25", impression=42)]
                self.assertEqual(self.refresh(source, now=SECOND)["status"], "ok")
                status, payload = self.query()
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"][0]["revenue"], Decimal("18.25"))
                self.assertEqual(payload["data"][0]["impression"], 42)
                self.assertTrue(api.health(str(self.config_file))["has_snapshot"])

    def test_date_floor_filters_snapshot_rows_without_rejecting_early_requests(self):
        self.refresh(FakeSource([
            row("2026-10-31", revenue="9999.00"),
            row("2026-11-01", revenue="1.00"),
            row("2026-11-02", revenue="2.00"),
        ]))
        # Deliberately retain an earlier row in the fixture snapshot to prove
        # the API read itself enforces the reporting floor independently of SQL.
        with contextlib.closing(sqlite3.connect(
            str(self.root / "state" / "snapshot.sqlite3")
        )) as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT day FROM daily WHERE day='2026-10-31'",
            ).fetchone())
        status, payload = self.query("start_date=2026-10-01&end_date=2026-10-31")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], [])
        status, payload = self.query("start_date=2026-10-01&end_date=2026-11-01")
        self.assertEqual(status, 200)
        self.assertEqual([value["day"] for value in payload["data"]], ["2026-11-01"])
        status, payload = self.query("start_date=2026-11-02&end_date=2026-11-06")
        self.assertEqual(status, 200)
        self.assertEqual([value["day"] for value in payload["data"]], ["2026-11-02"])

    def test_config_changed_during_read_cannot_publish_under_new_pool(self):
        source = FakeSource([row()])
        def change_pool():
            self.config["pool_id"] = 88
            self.write_config()
        source.on_read = change_pool
        self.refresh(source)
        self.assertEqual(self.query()[0], 503)

    def test_membership_lookup_failure_does_not_serve_unverified_scope(self):
        source = FakeSource([row()])
        self.refresh(source)
        source.scope_error = RuntimeError("membership unavailable")
        self.refresh(source, now=SECOND)
        self.assertEqual(self.query()[0], 503)

    def test_membership_change_during_read_cannot_publish_old_scope(self):
        source = FakeSource([row()])
        self.refresh(source)
        source.on_read = lambda: setattr(source, "accounts", ("202",))
        self.refresh(source, now=SECOND)
        self.assertEqual(self.query()[0], 503)

    def test_invalid_batch_keeps_prior_complete_snapshot(self):
        source = FakeSource([row(revenue="5.00")])
        self.refresh(source)
        original = self.query()[1]
        source.rows = [row("2026-11-01"), row("2026-11-06", revenue="NaN")]
        self.assertEqual(self.refresh(source, now=SECOND)["status"], "failed")
        status, result = self.query()
        self.assertEqual(status, 200)
        self.assertEqual(result["data"], original["data"])
        self.assertEqual(result["updated_at"], original["updated_at"])
        self.assertTrue(result["stale"])

    def test_refresh_cli_distinguishes_operational_failures_from_deliberately_unconfigured(self):
        cases = [
            ({"status": "ok"}, 0), ({"status": "busy"}, 0),
            ({"status": "config_not_ready", "reason": "pool_not_configured"}, 0),
            ({"status": "config_not_ready", "reason": "scope_unavailable"}, 1),
            ({"status": "config_not_ready", "reason": "invalid_configuration"}, 1),
            ({"status": "failed", "reason": "source_read_failed"}, 1),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                with patch.object(api, "refresh", return_value=result), patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(api.main(["refresh", "--config", str(self.config_file)]), expected)

    def test_strict_dates_unknown_and_duplicate_parameters(self):
        self.refresh(FakeSource())
        invalid = [
            "", "start_date=2026-11-01", "end_date=2026-11-06",
            "start_date=2026-11-07&end_date=2026-11-06",
            "start_date=2026-2-01&end_date=2026-11-06",
            "start_date=2026-02-29&end_date=2026-11-06",
            "start_date=2026-11-01T00:00:00&end_date=2026-11-06",
            QUERY + "&start_date=2026-11-02",
            QUERY + "&page_id=9999", QUERY + "&account_pool_id=88",
        ]
        for query in invalid:
            with self.subTest(query=query):
                status, result = self.query(query)
                self.assertEqual(status, 400)
                self.assertEqual(result["code"], "INVALID_PARAMETER")
        self.assertEqual(self.query("start_date=2024-02-29&end_date=2024-02-29")[0], 200)

    def test_bearer_required_and_invalid_token_is_not_echoed(self):
        self.refresh(FakeSource())
        for headers in ({}, {"Authorization": "Bearer wrong-secret"}, {"Authorization": "Basic " + TOKEN}):
            with self.subTest(headers=list(headers)):
                status, result = self.query(headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(result["code"], "UNAUTHORIZED")
                self.assertNotIn(TOKEN, api.dumps(result))
                self.assertNotIn("wrong-secret", api.dumps(result))
        self.assertEqual(self.query(headers={"authorization": "Bearer " + TOKEN})[0], 200)

    def test_real_user_cidr_boundaries_and_empty_allowlist(self):
        self.refresh(FakeSource())
        for peer in ("91.203.98.0", "91.203.98.255", "82.145.216.0", "82.145.216.255"):
            with self.subTest(peer=peer):
                self.assertEqual(self.query(peer=peer)[0], 200)
        for peer in ("91.203.97.255", "91.203.99.0", "82.145.215.255", "82.145.217.0"):
            with self.subTest(peer=peer):
                self.assertEqual(self.query(peer=peer)[0], 403)
        self.config["allowed_ips"] = []
        self.write_config()
        self.assertEqual(self.query()[0], 403)

    def test_untrusted_peer_cannot_spoof_forwarded_ip(self):
        self.refresh(FakeSource())
        headers = {"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "91.203.98.17"}
        self.assertEqual(self.query(peer="203.0.113.9", headers=headers)[0], 403)
        self.assertEqual(self.query(peer="127.0.0.1", headers=headers)[0], 200)

    def test_trusted_proxy_uses_rightmost_untrusted_address(self):
        self.refresh(FakeSource())
        headers = {"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "91.203.98.17, 203.0.113.9"}
        self.assertEqual(self.query(peer="127.0.0.1", headers=headers)[0], 403)
        headers["X-Forwarded-For"] = "91.203.98.17, 127.0.0.1"
        self.assertEqual(self.query(peer="127.0.0.1", headers=headers)[0], 200)
        headers["X-Forwarded-For"] = "not-an-ip"
        self.assertEqual(self.query(peer="127.0.0.1", headers=headers)[0], 403)

    def test_real_http_endpoint_serializes_contract_and_observes_config_reload(self):
        self.refresh(FakeSource([row()]))
        server = api.make_server(str(self.config_file))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "91.203.98.17"}
        client.request("GET", PATH + "?" + QUERY, headers=headers)
        response = client.getresponse()
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type"))
        self.assertEqual(json.loads(body)["data"][0]["revenue"], 128.5)
        self.assertRegex(body, r'"revenue"\s*:\s*128\.50')
        self.config["allowed_ips"] = []
        self.write_config()
        client.request("GET", PATH + "?" + QUERY, headers=headers)
        response = client.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        client.request("GET", "/api/v1/dramawave/w2a/9999/daily-stats?" + QUERY, headers=headers)
        response = client.getresponse()
        response.read()
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import contextlib
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from features.x_account_stats.service import (
    StatsRefreshError,
    assert_approved_mysql_entry,
    build_snapshot,
    campaign_id_from_long_url,
    merge_account_stats,
    read_ledger_metrics,
    revenue_query,
    run_gated_mysql,
    write_snapshot_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
UI_SOURCE = (ROOT / "static" / "x-account-list.html").read_text(encoding="utf-8")
SERVICE_UNIT = (ROOT / "deploy" / "x-account-operating-stats.service").read_text(encoding="utf-8")
TIMER_UNIT = (ROOT / "deploy" / "x-account-operating-stats.timer").read_text(encoding="utf-8")
REFRESH_SOURCE = (ROOT / "scripts" / "refresh_x_account_operating_stats.py").read_text(encoding="utf-8")


class XAccountOperatingStatsTests(unittest.TestCase):
    def make_ledger(self, path: Path) -> None:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE x_post_queue(
                    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    relay_account_id INTEGER NOT NULL
                );
                CREATE TABLE x_post_publish_log(
                    id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL, x_post_id TEXT NOT NULL,
                    published_at TEXT NOT NULL, long_url TEXT NOT NULL
                );
                CREATE TABLE x_post_repost_ledger(
                    id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL,
                    relay_account_id INTEGER NOT NULL,
                    target_account_id INTEGER NOT NULL, status TEXT NOT NULL,
                    source_post_id TEXT NOT NULL,
                    source_published_at TEXT NOT NULL,
                    reposted_at TEXT NOT NULL
                );
                """
            )
            queues = [
                (1, 10, "direct", 0),
                (2, 10, "direct", 0),
                (3, 10, "direct", 0),
                (4, 20, "premium_relay_repost", 99),
                (5, 30, "direct", 0),
                (6, 40, "premium_relay_repost", 88),
                (7, 10, "direct", 0),
                (8, 30, "direct", 0),
            ]
            conn.executemany("INSERT INTO x_post_queue VALUES(?,?,?,?)", queues)
            logs = [
                (1, 1, 10, "published", "p1", "2026-08-16T16:00:00Z", "https://w/?c=camp%2Bexact&af_c_id=1"),
                (2, 2, 10, "published", "p2", "2026-08-17T15:59:59Z", "https://w/?c=only-ten&af_c_id=2"),
                (3, 3, 10, "published", "p3", "2026-08-17T16:00:00Z", "https://w/?c=next-day&af_c_id=3"),
                (4, 4, 20, "published", "source", "2026-08-17T15:00:00Z", "https://w/?c=relay-target&af_c_id=4"),
                (5, 5, 30, "failed", "", "", "https://w/?c=camp%2Bexact&af_c_id=5"),
                (6, 7, 10, "published", "p7", "2026-08-17T16:00:00Z", "https://w/?c=shared-name&af_c_id=7"),
                (7, 8, 30, "published", "p8", "2026-08-17T16:00:00Z", "https://w/?c=shared-name&af_c_id=8"),
            ]
            conn.executemany("INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?)", logs)
            conn.execute(
                "INSERT INTO x_post_repost_ledger VALUES(1,4,99,20,'reposted','source','2026-08-16T15:59:59Z','2026-08-17T15:59:59Z')"
            )
            conn.execute(
                "INSERT INTO x_post_repost_ledger VALUES(2,6,77,40,'reposted','mismatch','2026-08-17T10:00:00Z','2026-08-17T11:00:00Z')"
            )
            conn.commit()

    def test_actual_actor_mapping_and_beijing_yesterday_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "ledger.sqlite3"
            self.make_ledger(db_path)
            metrics, campaigns, evidence = read_ledger_metrics(
                db_path, date(2026, 8, 17)
            )
        self.assertEqual(metrics[10]["published_posts_total"], 4)
        self.assertEqual(metrics[10]["published_posts_yesterday"], 2)
        self.assertEqual(metrics[99]["published_posts_total"], 1)
        self.assertEqual(metrics[99]["published_posts_yesterday"], 0)
        self.assertEqual(metrics[20]["reposts_total"], 1)
        self.assertEqual(metrics[20]["reposts_yesterday"], 1)
        self.assertNotIn(20, {key for key, item in metrics.items() if item["published_posts_total"]})
        self.assertEqual(campaigns["1"], 10)
        self.assertEqual(campaigns["4"], 20)
        self.assertEqual(campaigns["5"], 30)  # ownership is independent of publish status
        self.assertEqual(campaigns["7"], 10)
        self.assertEqual(campaigns["8"], 30)  # same campaign name, different IDs
        self.assertEqual(evidence["conflicts"], 0)
        self.assertEqual(evidence["unconfirmed"], 1)
        self.assertEqual(evidence["ledger_conflicts"], 1)
        self.assertNotIn(77, metrics)
        self.assertNotIn(88, metrics)

    def test_campaign_id_is_exact_and_duplicates_or_missing_id_are_rejected(self):
        self.assertEqual(campaign_id_from_long_url("https://w/?c=changed&af_c_id=3"), "3")
        self.assertEqual(campaign_id_from_long_url("https://w/?af_c_id=a&af_c_id=b"), "")
        self.assertEqual(campaign_id_from_long_url("https://w/?c=3"), "")
        self.assertEqual(campaign_id_from_long_url("https://w/?af_c_id="), "")
        self.assertEqual(campaign_id_from_long_url("https://w/?af_c_id=A%2BB"), "A+B")

    def test_revenue_query_uses_exact_site_and_db_beijing_date_definition(self):
        query = revenue_query(date(2026, 8, 17))
        binary_campaign = "CONVERT(COALESCE(campaign_id,'') USING binary)"
        self.assertIn("SET SESSION time_zone = '+08:00'", query)
        self.assertIn("DATE(FROM_UNIXTIME(event_time))='2026-08-17'", query)
        self.assertIn("WHERE site_id='2116'", query)
        self.assertIn("FROM ads_drama_bills FORCE INDEX(idx_site_event_time)", query)
        self.assertIn("SUM(event_revenue_usd)", query)
        self.assertIn("REPLACE(TO_BASE64", query)
        self.assertEqual(query.count(binary_campaign), 1)
        self.assertIn(f"TO_BASE64({binary_campaign})", query)
        self.assertIn("AS campaign_id_b64", query)
        self.assertIn("GROUP BY campaign_id_b64", query)
        self.assertIn("ORDER BY campaign_id_b64", query)
        # MySQL 5.7 ONLY_FULL_GROUP_BY accepts grouping by the complete projection alias.
        self.assertNotIn(f"GROUP BY {binary_campaign}", query)
        self.assertNotIn("GROUP BY campaign\n", query)
        self.assertNotIn("ORDER BY campaign;", query)
        self.assertNotRegex(query, r"\bc\b")
        self.assertNotIn("LIKE", query.upper())

    def test_campaign_id_join_recovers_names_without_claiming_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "ledger.sqlite3"
            self.make_ledger(db_path)
            before = db_path.read_bytes()
            metrics, campaigns, evidence = read_ledger_metrics(db_path, date(2026, 8, 17))
            self.assertEqual(before, db_path.read_bytes())
        snapshot = build_snapshot(
            ledger_metrics=metrics, campaign_accounts=campaigns,
            campaign_evidence=evidence,
            revenue_rows=[
                ("1", Decimal("67.01"), Decimal("0")),
                ("5", Decimal("4.97"), Decimal("0")),
                ("", Decimal("699.72"), Decimal("0")),
                ("nonexistent", Decimal("2"), Decimal("0")),
            ],
            now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["accounts"]["10"]["revenue_total_usd"], "67.010000")
        self.assertEqual(snapshot["accounts"]["30"]["revenue_total_usd"], "4.970000")
        self.assertEqual(snapshot["accounts"]["30"]["published_posts_total"], 1)
        self.assertEqual(snapshot["unallocated_revenue"]["total_usd"], "701.720000")
        self.assertEqual(snapshot["attribution"]["method"], "campaign-id-publish-ledger-v2")

    def test_inconsistent_frozen_id_or_log_owner_is_unallocated(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "ledger.sqlite3"
            self.make_ledger(db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE x_post_publish_log SET long_url='https://w/?af_c_id=2' WHERE queue_id=1")
                conn.execute("UPDATE x_post_publish_log SET account_id=90 WHERE queue_id=3")
                conn.execute("UPDATE x_post_publish_log SET long_url='https://w/?c=4' WHERE queue_id=4")
                conn.commit()
            _, campaigns, evidence = read_ledger_metrics(db_path, date(2026, 8, 17))
        self.assertNotIn("1", campaigns)
        self.assertNotIn("3", campaigns)
        self.assertNotIn("4", campaigns)
        self.assertEqual(campaigns["2"], 10)
        self.assertEqual(evidence["missing"], 1)
        self.assertEqual(evidence["ledger_conflicts"], 3)

    @mock.patch("features.x_account_stats.service.subprocess.run")
    def test_mysql_uses_host_gate_and_password_only_in_child_environment(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout="Y2FtcA==\t1.25\t0.25\n",
            stderr="",
        )
        with mock.patch.dict(os.environ, {"X_INTERNAL_TOKEN": "do-not-forward"}):
            rows = run_gated_mysql(
                host="reader",
                port=63350,
                user="readonly",
                password="secret-value",
                database="kunlunads_dev",
                query="SELECT 1",
            )
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[0], "/usr/bin/mysql")
        self.assertNotIn("mysql.real", " ".join(command))
        self.assertNotIn("secret-value", " ".join(command))
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], "secret-value")
        self.assertNotIn("X_INTERNAL_TOKEN", kwargs["env"])
        self.assertEqual(rows, [("camp", Decimal("1.25"), Decimal("0.25"))])
        with self.assertRaises(StatsRefreshError):
            run_gated_mysql(
                host="reader", port=63350, user="readonly", password="x",
                database="kunlunads_dev", query="SELECT 1",
                mysql_binary="/usr/bin/mysql.real",
            )

    @mock.patch("features.x_account_stats.service.subprocess.run")
    def test_binary_campaign_rows_keep_case_and_trailing_spaces_distinct(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout=(
                "RXhhY3Q=\t1.00\t0.10\n"
                "ZXhhY3Q=\t2.00\t0.20\n"
                "ZXhhY3Qg\t3.00\t0.30\n"
            ),
            stderr="",
        )
        rows = run_gated_mysql(
            host="reader", port=63350, user="readonly", password="secret",
            database="kunlunads_dev", query=revenue_query(date(2026, 8, 17)),
        )
        self.assertEqual([row[0] for row in rows], ["Exact", "exact", "exact "])
        snapshot = build_snapshot(
            ledger_metrics={},
            campaign_accounts={"Exact": 1, "exact": 2, "exact ": 3},
            campaign_evidence={}, revenue_rows=rows,
            now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["accounts"]["1"]["revenue_total_usd"], "1.000000")
        self.assertEqual(snapshot["accounts"]["2"]["revenue_total_usd"], "2.000000")
        self.assertEqual(snapshot["accounts"]["3"]["revenue_total_usd"], "3.000000")

    def test_mysql_gate_target_must_match_exact_approved_wrapper(self):
        approved = assert_approved_mysql_entry(
            resolver=lambda _entry: Path("/usr/local/bin/mysql-gated")
        )
        self.assertEqual(approved, Path("/usr/local/bin/mysql-gated"))
        for drift in (
            "/usr/bin/mysql.real",
            "/usr/bin/mariadb",
            "/opt/vendor/mysql",
            "/usr/local/bin/mysql-gated-copy",
        ):
            with self.subTest(drift=drift), self.assertRaises(StatsRefreshError):
                assert_approved_mysql_entry(resolver=lambda _entry, value=drift: Path(value))
        with self.assertRaises(StatsRefreshError):
            assert_approved_mysql_entry(
                "/usr/bin/mariadb",
                resolver=lambda _entry: Path("/usr/local/bin/mysql-gated"),
            )
    def test_exact_attribution_unallocated_and_money_precision(self):
        snapshot = build_snapshot(
            ledger_metrics={
                7: {
                    "published_posts_total": 2,
                    "published_posts_yesterday": 1,
                    "reposts_total": 3,
                    "reposts_yesterday": 1,
                }
            },
            campaign_accounts={"exact": 7},
            campaign_evidence={"campaigns": 1, "conflicts": 0, "missing": 0},
            revenue_rows=[
                ("exact", Decimal("1234.5"), Decimal("2.345678")),
                ("missing", Decimal("9.1"), Decimal("1.2")),
                ("", Decimal("0.2"), Decimal("0")),
            ],
            now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["accounts"]["7"]["revenue_total_usd"], "1234.500000")
        self.assertEqual(snapshot["accounts"]["7"]["revenue_yesterday_usd"], "2.345678")
        self.assertEqual(snapshot["unallocated_revenue"]["total_usd"], "9.300000")
        self.assertEqual(snapshot["unallocated_revenue"]["yesterday_usd"], "1.200000")

    def test_missing_and_stale_cache_do_not_break_account_dto(self):
        base = {"items": [{"id": 7, "username": "safe"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "current.json"
            missing = merge_account_stats(
                base, cache, now=datetime(2026, 8, 18, tzinfo=timezone.utc)
            )
            self.assertEqual(missing["operating_stats_meta"]["status"], "missing")
            self.assertIsNone(missing["items"][0]["operating_stats"]["revenue_total_usd"])
            snapshot = build_snapshot(
                ledger_metrics={}, campaign_accounts={},
                campaign_evidence={"campaigns": 0, "conflicts": 0, "missing": 0},
                revenue_rows=[], now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            )
            write_snapshot_atomic(snapshot, cache, root)
            stale = merge_account_stats(
                base, cache, now=datetime(2026, 8, 18, tzinfo=timezone.utc),
                max_age_seconds=3600,
            )
        self.assertEqual(stale["operating_stats_meta"]["status"], "stale")
        self.assertEqual(stale["items"][0]["operating_stats"]["published_posts_total"], 0)

    def test_cache_business_date_and_future_clock_boundaries(self):
        base = {"items": [{"id": 7}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "current.json"
            prior_day = build_snapshot(
                ledger_metrics={}, campaign_accounts={},
                campaign_evidence={}, revenue_rows=[],
                now=datetime(2026, 8, 17, 15, 50, tzinfo=timezone.utc),
            )
            write_snapshot_atomic(prior_day, cache, root)
            crossed = merge_account_stats(
                base, cache,
                now=datetime(2026, 8, 17, 16, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(crossed["operating_stats_meta"]["status"], "stale")
            self.assertIn("business_date", crossed["operating_stats_meta"]["stale_reasons"])
            self.assertEqual(crossed["operating_stats_meta"]["yesterday_date"], "2026-08-16")

            future = build_snapshot(
                ledger_metrics={}, campaign_accounts={},
                campaign_evidence={}, revenue_rows=[],
                now=datetime(2026, 8, 18, 0, 10, tzinfo=timezone.utc),
            )
            write_snapshot_atomic(future, cache, root)
            future_result = merge_account_stats(
                base, cache,
                now=datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(future_result["operating_stats_meta"]["status"], "stale")
            self.assertIn("future_generated_at", future_result["operating_stats_meta"]["stale_reasons"])

            small_skew = build_snapshot(
                ledger_metrics={}, campaign_accounts={},
                campaign_evidence={}, revenue_rows=[],
                now=datetime(2026, 8, 18, 0, 2, tzinfo=timezone.utc),
            )
            write_snapshot_atomic(small_skew, cache, root)
            accepted = merge_account_stats(
                base, cache,
                now=datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(accepted["operating_stats_meta"]["status"], "fresh")

    def test_cache_rejects_missing_malformed_or_mismatched_business_dates(self):
        base = {"items": [{"id": 7}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "current.json"
            valid = build_snapshot(
                ledger_metrics={}, campaign_accounts={}, campaign_evidence={},
                revenue_rows=[], now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
            )
            cases = (
                ("missing_business", {key: value for key, value in valid.items() if key != "business_date"}),
                ("missing_yesterday", {key: value for key, value in valid.items() if key != "yesterday_date"}),
                ("malformed_business", {**valid, "business_date": "2026/08/18"}),
                ("malformed_yesterday", {**valid, "yesterday_date": "2026/08/17"}),
                ("noncanonical", {**valid, "business_date": "20260818"}),
                ("mismatch", {**valid, "yesterday_date": "2026-08-16"}),
            )
            for name, snapshot in cases:
                with self.subTest(name=name):
                    write_snapshot_atomic(snapshot, cache, root)
                    result = merge_account_stats(
                        base, cache,
                        now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
                    )
                    self.assertEqual(result["operating_stats_meta"]["status"], "missing")
                    self.assertIsNone(
                        result["items"][0]["operating_stats"]["published_posts_total"]
                    )

    def test_ui_fields_public_metric_removals_and_usd_format(self):
        for label in (
            "累计 Post", "累计 Repost", "累计收入",
            "未归属 X 收入（累计）", "未归属 X 收入（昨日）",
        ):
            self.assertIn(label, UI_SOURCE)
        self.assertIn("`${yesterday} Post`", UI_SOURCE)
        self.assertIn("`${yesterday} Repost`", UI_SOURCE)
        self.assertIn("`${yesterday} 收入`", UI_SOURCE)
        self.assertIn('currency:"USD"', UI_SOURCE)
        self.assertIn("meta.yesterday_date", UI_SOURCE)
        self.assertIn("昨日口径", UI_SOURCE)
        self.assertIn('["粉丝", "followers_count"]', UI_SOURCE)
        self.assertIn('["帖子", "tweet_count"]', UI_SOURCE)
        self.assertIn('["喜欢", "like_count"]', UI_SOURCE)
        self.assertNotIn('"following_count"', UI_SOURCE)
        self.assertNotIn('"listed_count"', UI_SOURCE)
        self.assertNotIn('"media_count"', UI_SOURCE)

    def test_admin_auth_no_store_and_timer_contracts_remain(self):
        route_start = APP_SOURCE.index('if parsed.path == "/api/admin/x-accounts":')
        route = APP_SOURCE[route_start : route_start + 1300]
        self.assertIn("if not self._require_cookie_admin()", route)
        self.assertIn("merge_account_stats", route)
        self.assertIn("no_store=True", route)
        self.assertIn('cache:"no-store"', UI_SOURCE)
        self.assertIn("/usr/bin/mysql", SERVICE_UNIT)
        self.assertNotIn("mysql.real", SERVICE_UNIT)
        self.assertIn("assert_approved_mysql_entry()", REFRESH_SOURCE)
        self.assertIn("/mnt/data-disk/x-account-operating-stats", SERVICE_UNIT)
        self.assertIn("CapabilityBoundingSet=CAP_DAC_READ_SEARCH", SERVICE_UNIT)
        self.assertIn("AmbientCapabilities=CAP_DAC_READ_SEARCH", SERVICE_UNIT)
        self.assertNotIn("CAP_DAC_OVERRIDE", SERVICE_UNIT)
        self.assertIn("/var/lib/sql-connection-gate/session-locks", SERVICE_UNIT)
        self.assertIn("InaccessiblePaths=/var/lib/x-post-automation/tokens /etc/ssh", SERVICE_UNIT)
        self.assertIn("10:00:00 Asia/Shanghai", TIMER_UNIT)
        self.assertIn("11:00:00 Asia/Shanghai", TIMER_UNIT)
        self.assertIn("12:00:00 Asia/Shanghai", TIMER_UNIT)
        self.assertNotIn("09:10:00 Asia/Shanghai", TIMER_UNIT)
        self.assertNotIn("21:10:00 Asia/Shanghai", TIMER_UNIT)


if __name__ == "__main__":
    unittest.main()

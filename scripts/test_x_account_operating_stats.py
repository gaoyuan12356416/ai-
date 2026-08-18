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
    build_snapshot,
    campaign_from_long_url,
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
                    delivery_mode TEXT NOT NULL
                );
                CREATE TABLE x_post_publish_log(
                    id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL,
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
                (1, 10, "direct"),
                (2, 10, "direct"),
                (3, 10, "direct"),
                (4, 20, "premium_relay_repost"),
                (5, 30, "direct"),
            ]
            conn.executemany("INSERT INTO x_post_queue VALUES(?,?,?)", queues)
            logs = [
                (1, 1, "published", "p1", "2026-08-16T16:00:00Z", "https://w/?c=camp%2Bexact"),
                (2, 2, "published", "p2", "2026-08-17T15:59:59Z", "https://w/?c=only-ten"),
                (3, 3, "published", "p3", "2026-08-17T16:00:00Z", "https://w/?c=next-day"),
                (4, 4, "published", "source", "2026-08-17T15:00:00Z", "https://w/?c=relay-target"),
                (5, 5, "failed", "", "", "https://w/?c=camp%2Bexact"),
            ]
            conn.executemany("INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?)", logs)
            conn.execute(
                "INSERT INTO x_post_repost_ledger VALUES(1,4,99,20,'reposted','source','2026-08-16T15:59:59Z','2026-08-17T15:59:59Z')"
            )
            conn.commit()

    def test_actual_actor_mapping_and_beijing_yesterday_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "ledger.sqlite3"
            self.make_ledger(db_path)
            metrics, campaigns, evidence = read_ledger_metrics(
                db_path, date(2026, 8, 17)
            )
        self.assertEqual(metrics[10]["published_posts_total"], 3)
        self.assertEqual(metrics[10]["published_posts_yesterday"], 2)
        self.assertEqual(metrics[99]["published_posts_total"], 1)
        self.assertEqual(metrics[99]["published_posts_yesterday"], 0)
        self.assertEqual(metrics[20]["reposts_total"], 1)
        self.assertEqual(metrics[20]["reposts_yesterday"], 1)
        self.assertNotIn(20, {key for key, item in metrics.items() if item["published_posts_total"]})
        self.assertNotIn("camp+exact", campaigns)  # conflicting q.account_id is fail-closed
        self.assertEqual(campaigns["relay-target"], 20)
        self.assertEqual(evidence["conflicts"], 1)

    def test_campaign_value_is_exact_and_duplicate_c_is_rejected(self):
        self.assertEqual(campaign_from_long_url("https://w/?x=1&c=A%2BB"), "A+B")
        self.assertEqual(campaign_from_long_url("https://w/?c=a&c=b"), "")
        self.assertEqual(campaign_from_long_url("https://w/?af_c_id=3"), "")

    def test_revenue_query_uses_exact_site_and_db_beijing_date_definition(self):
        query = revenue_query(date(2026, 8, 17))
        self.assertIn("SET SESSION time_zone = '+08:00'", query)
        self.assertIn("DATE(FROM_UNIXTIME(event_time))='2026-08-17'", query)
        self.assertIn("WHERE site_id='2116'", query)
        self.assertIn("SUM(event_revenue_usd)", query)
        self.assertIn("REPLACE(TO_BASE64", query)
        self.assertNotIn("LIKE", query.upper())

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

    def test_ui_fields_public_metric_removals_and_usd_format(self):
        for label in (
            "累计 Post", "昨日 Post", "累计 Repost", "昨日 Repost",
            "累计收入", "昨日收入", "未归属 X 收入（累计）", "未归属 X 收入（昨日）",
        ):
            self.assertIn(label, UI_SOURCE)
        self.assertIn('currency:"USD"', UI_SOURCE)
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
        self.assertIn('resolved_mysql.name == "mysql.real"', REFRESH_SOURCE)
        self.assertIn("/mnt/data-disk/x-account-operating-stats", SERVICE_UNIT)
        self.assertIn("09:10:00 Asia/Shanghai", TIMER_UNIT)
        self.assertIn("21:10:00 Asia/Shanghai", TIMER_UNIT)


if __name__ == "__main__":
    unittest.main()

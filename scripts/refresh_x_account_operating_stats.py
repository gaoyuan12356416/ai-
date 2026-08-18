#!/usr/bin/env python3
"""Refresh the local X account operating-statistics cache."""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_account_stats.service import (  # noqa: E402
    DEFAULT_CACHE_PATH,
    DEFAULT_CACHE_ROOT,
    SHANGHAI,
    StatsRefreshError,
    assert_approved_mysql_entry,
    build_snapshot,
    read_ledger_metrics,
    revenue_query,
    run_gated_mysql,
    utc_now,
    write_snapshot_atomic,
)


def _required(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise StatsRefreshError("统计刷新配置不完整：%s" % name)
    return value


def main() -> int:
    stage = "startup"
    try:
        assert_approved_mysql_entry()
        stage = "ledger"
        now = utc_now()
        yesterday = now.astimezone(SHANGHAI).date() - timedelta(days=1)
        ledger_metrics, campaign_accounts, campaign_evidence = read_ledger_metrics(
            os.environ.get(
                "X_ACCOUNT_STATS_LEDGER_DB",
                "/var/lib/x-post-automation/accounts.sqlite3",
            ),
            yesterday,
        )
        stage = "revenue"
        revenue_rows = run_gated_mysql(
            host=_required("X_ACCOUNT_STATS_MYSQL_HOST"),
            port=int(os.environ.get("X_ACCOUNT_STATS_MYSQL_PORT", "63350")),
            user=_required("X_ACCOUNT_STATS_MYSQL_USER"),
            password=_required("X_ACCOUNT_STATS_MYSQL_PASSWORD"),
            database=os.environ.get(
                "X_ACCOUNT_STATS_MYSQL_DATABASE", "kunlunads_dev"
            ),
            query=revenue_query(yesterday),
            mysql_binary="/usr/bin/mysql",
            connect_timeout=int(
                os.environ.get("X_ACCOUNT_STATS_MYSQL_CONNECT_TIMEOUT", "10")
            ),
            query_timeout=int(
                os.environ.get("X_ACCOUNT_STATS_MYSQL_QUERY_TIMEOUT", "300")
            ),
        )
        stage = "snapshot"
        snapshot = build_snapshot(
            ledger_metrics=ledger_metrics,
            campaign_accounts=campaign_accounts,
            campaign_evidence=campaign_evidence,
            revenue_rows=revenue_rows,
            now=now,
        )
        stage = "cache"
        write_snapshot_atomic(
            snapshot,
            os.environ.get("X_ACCOUNT_STATS_CACHE_PATH", str(DEFAULT_CACHE_PATH)),
            os.environ.get("X_ACCOUNT_STATS_CACHE_ROOT", str(DEFAULT_CACHE_ROOT)),
        )
        print(
            "x_account_operating_stats_refresh_ok accounts=%d revenue_campaigns=%d generated_at=%s"
            % (
                len(snapshot["accounts"]),
                len(revenue_rows),
                snapshot["generated_at_utc"],
            )
        )
        return 0
    except (StatsRefreshError, OSError, ValueError):
        print(
            "x_account_operating_stats_refresh_failed stage=%s" % stage,
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

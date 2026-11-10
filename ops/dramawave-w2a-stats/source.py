"""Read-only account-pool export across all products and pages.

Scheduled reads must use the CPU host's shared SQL connection gate. Credentials
come only from the refresh service environment, never CLI arguments or logs.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation
import os
import subprocess


class SourceError(RuntimeError):
    """A deliberately credential-free failure safe for internal logs."""


def _account_literal(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceError("invalid account identity")
    # Account identity remains exact (no act_ removal or numeric coercion).
    return "X'%s'" % value.encode("utf-8").hex()


def _count(value):
    # TikTok count columns are DECIMAL(10,2). Accept integral decimals without
    # truncating a fractional count or converting through binary floating point.
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("invalid source count")
    return int(number)


class MySQLSource:
    def __init__(self, config, query=None):
        self.config = config
        self.query = query or self._query

    def _query(self, sql):
        user = os.environ.get("W2A_MYSQL_USER", "")
        password = os.environ.get("W2A_MYSQL_PASSWORD", "")
        if not user or not password:
            raise SourceError("source credentials are not configured")
        # Both metric sources are aggregated in one consistent SELECT snapshot.
        # Keep membership reads short and bound the single hourly aggregate.
        query_seconds = 240 if sql.lstrip().startswith("SELECT dt AS day") else 20
        env = os.environ.copy()
        env["MYSQL_PWD"] = password
        env["SQL_GATE_JOB_NAME"] = "dramawave-w2a-stats-2334"
        env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "60"
        env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = str(query_seconds + 10)
        env["SQL_GATE_RETRY_DELAYS_SECONDS"] = ""
        env.pop("SQL_GATE_BYPASS", None)
        env.pop("SQL_GATE_TEST_MODE", None)
        # This explicit gated path must not become mysql.real or a driver.
        args = [
            "/usr/local/bin/mysql-gated", "--protocol=TCP",
            "--host=101.32.56.53", "--port=63350", "--user=" + user,
            "--connect-timeout=5", "--default-character-set=utf8mb4",
            "--batch", "--raw", "--skip-column-names",
        ]
        script = ("SET SESSION max_execution_time=%d;\n" % (query_seconds * 1000) +
                  "START TRANSACTION READ ONLY;\nSELECT @@read_only;\n"
                  + sql + ";\nCOMMIT;\n")
        try:
            result = subprocess.run(args, input=script, text=True,
                                    encoding="utf-8", capture_output=True,
                                    env=env, timeout=query_seconds + 90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise SourceError("source query unavailable or timed out") from None
        if result.returncode != 0:
            # Never echo driver stderr (may contain identities or SQL).
            raise SourceError("source query failed")
        lines = result.stdout.splitlines()
        if not lines or lines[0].strip() != "1":
            raise SourceError("read-only source verification failed")
        return [tuple(None if item == "NULL" else item
                      for item in line.split("\t")) for line in lines[1:]]

    def load_scope(self, config):
        pool_id = config.get("pool_id")
        if isinstance(pool_id, bool) or not isinstance(pool_id, int) or pool_id <= 0:
            raise SourceError("account pool is not configured")
        rows = self.query("""
SELECT g.id, a.id, a.account_id
FROM kunlunads_dev.ads_alternative_account_group g
LEFT JOIN kunlunads_dev.ads_alternative_accounts a
  ON a.group_id = g.id AND COALESCE(a.deleted_at, 0) = 0
WHERE g.id = %d
ORDER BY a.account_id, a.id
""".strip() % pool_id)
        if not rows:
            raise SourceError("account pool does not exist")
        accounts = set()
        for row in rows:
            if len(row) != 3 or str(row[0]) != str(pool_id):
                raise SourceError("invalid account pool result")
            if row[1] is None:
                if row[2] is not None:
                    raise SourceError("invalid empty pool result")
                continue
            value = row[2]
            _account_literal(value)
            accounts.add(value)
        return tuple(sorted(accounts))

    def read_daily(self, account_ids):
        accounts = tuple(sorted(set(account_ids)))
        if not accounts:
            return []
        values = ", ".join(_account_literal(value) for value in accounts)
        # A text comparison enables the advertiser/date index. The additional
        # binary predicate preserves byte-exact membership despite its collation.
        text_values = ", ".join("CONVERT(%s USING utf8mb4) COLLATE utf8mb4_unicode_ci" % _account_literal(value)
                                for value in accounts)
        rows = self.query("""
SELECT dt AS day,
       SUM(impression) AS impression,
       SUM(click) AS click,
       SUM(install) AS install,
       SUM(revenue) AS revenue,
       SUM(cost) AS cost
FROM (
    SELECT start_date AS dt,
           SUM(show_cnt) AS impression,
           SUM(click_cnt) AS click,
           0 AS install,
           0 AS revenue,
           SUM(stat_cost) AS cost
    FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(ads)
    WHERE start_date >= '2026-11-01'
      AND category = 0
      AND advertiser_id IN (%s)
      AND CAST(advertiser_id AS BINARY) IN (%s)
    GROUP BY start_date
    UNION ALL
    SELECT dt,
           0 AS impression,
           0 AS click,
           SUM(installs) AS install,
           SUM(af_revenue0) AS revenue,
           0 AS cost
    FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(stats_date)
    WHERE dt >= '2026-11-01'
      AND CAST(ad_account_id AS BINARY) IN (%s)
    GROUP BY dt
) AS facts
GROUP BY dt
ORDER BY dt
""".strip() % (text_values, values, values))
        result = []
        try:
            for row in rows:
                if len(row) != 6:
                    raise ValueError("bad source row")
                day = str(row[0])
                if dt.date.fromisoformat(day).isoformat() != day:
                    raise ValueError("bad source day")
                amount = [Decimal(str(value)) for value in row[4:]]
                if any(not value.is_finite() for value in amount):
                    raise ValueError("non-finite source amount")
                result.append({
                    "day": day, "impression": _count(row[1]), "click": _count(row[2]),
                    "install": _count(row[3]), "revenue": amount[0], "cost": amount[1],
                })
        except (TypeError, ValueError, InvalidOperation):
            raise SourceError("invalid source metric result") from None
        return result

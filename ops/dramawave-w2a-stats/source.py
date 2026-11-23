"""Read-only account-pool export across all products and pages.

Scheduled reads must use the CPU host's shared SQL connection gate. Credentials
come only from the refresh service environment, never CLI arguments or logs.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import os
import re
import subprocess
import sys
import time


class SourceError(RuntimeError):
    """A deliberately credential-free failure safe for internal logs."""


def _query_diagnostic(kind, started, status, **fields):
    """Keep query timing/error numbers without SQL, identities or driver text."""
    payload = {"event": "w2a_source_query", "kind": kind, "status": status,
               "elapsed_seconds": round(time.monotonic() - started, 3)}
    payload.update(fields)
    try:
        print(json.dumps(payload, separators=(",", ":")), file=sys.stderr, flush=True)
    except OSError:
        # A failed diagnostic sink must not discard otherwise complete metrics.
        pass


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
    def __init__(self, config, query=None, today=None):
        self.config = config
        self.query = query or self._query
        self.today = today or (lambda: dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())

    def _query(self, sql):
        user = os.environ.get("W2A_MYSQL_USER", "")
        password = os.environ.get("W2A_MYSQL_PASSWORD", "")
        if not user or not password:
            raise SourceError("source credentials are not configured")
        statements = (sql,) if isinstance(sql, str) else tuple(sql)
        kind = "metrics" if statements[0].lstrip().startswith("SELECT dt AS day") else "scope"
        # Daily reads share one connection and consistent transaction. Each day
        # is bounded separately, with a second cap on the complete history read.
        query_seconds = 60 if kind == "metrics" else 20
        execution_seconds = 500 if kind == "metrics" else 30
        env = os.environ.copy()
        env["MYSQL_PWD"] = password
        env["SQL_GATE_JOB_NAME"] = "dramawave-w2a-stats-2334"
        env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "60"
        env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = str(execution_seconds)
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
        transaction = ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;\n"
                       "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;\n"
                       if kind == "metrics" else "START TRANSACTION READ ONLY;\n")
        script = ("SET SESSION max_execution_time=%d;\n" % (query_seconds * 1000) +
                  transaction + "SELECT @@read_only;\n" + ";\n".join(statements) + ";\nCOMMIT;\n")
        started = time.monotonic()
        try:
            result = subprocess.run(args, input=script, text=True,
                                    encoding="utf-8", capture_output=True,
                                    env=env, timeout=execution_seconds + 80, check=False)
        except subprocess.TimeoutExpired:
            _query_diagnostic(kind, started, "process_timeout", statement_limit_seconds=query_seconds)
            raise SourceError("source query unavailable or timed out") from None
        except OSError:
            _query_diagnostic(kind, started, "process_unavailable")
            raise SourceError("source query unavailable or timed out") from None
        if result.returncode != 0:
            # Only retain the numeric MySQL code, never the original message.
            match = re.search(r"\bERROR\s+([0-9]{3,5})\b", result.stderr or "")
            _query_diagnostic(kind, started, "failed", returncode=result.returncode,
                              mysql_error_code=int(match.group(1)) if match else None,
                              statement_limit_seconds=query_seconds)
            raise SourceError("source query failed")
        lines = result.stdout.splitlines()
        if not lines or lines[0].strip() != "1":
            _query_diagnostic(kind, started, "read_only_verification_failed")
            raise SourceError("read-only source verification failed")
        _query_diagnostic(kind, started, "ok", result_rows=len(lines) - 1,
                          statement_count=len(statements))
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
        template = """
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
    WHERE %s
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
    WHERE %s
      AND CAST(ad_account_id AS BINARY) IN (%s)
    GROUP BY dt
) AS facts
GROUP BY dt
ORDER BY dt
""".strip()
        first_day = dt.date(2026, 11, 1)
        last_day = max(first_day, self.today())
        statements = []
        day = first_day
        while day <= last_day:
            # The final range is open-ended, preserving any future-dated source
            # rows and avoiding a race if the refresh spans Beijing midnight.
            comparison = ">=" if day == last_day else "="
            date_value = "'%s'" % day.isoformat()
            statements.append(template % (
                "start_date %s %s" % (comparison, date_value), text_values, values,
                "dt %s %s" % (comparison, date_value), values,
            ))
            day += dt.timedelta(days=1)
        rows = self.query(tuple(statements))
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

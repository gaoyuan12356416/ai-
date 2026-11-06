"""Read-only source for the fixed DramaWave W2A 2334 export.

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
    return "CONVERT(0x%s USING utf8mb4)" % value.encode("utf-8").hex()


class MySQLSource:
    def __init__(self, config, query=None):
        self.config = config
        self.query = query or self._query

    def _query(self, sql):
        user = os.environ.get("W2A_MYSQL_USER", "")
        password = os.environ.get("W2A_MYSQL_PASSWORD", "")
        if not user or not password:
            raise SourceError("source credentials are not configured")
        env = os.environ.copy()
        env["MYSQL_PWD"] = password
        env["SQL_GATE_JOB_NAME"] = "dramawave-w2a-stats-2334"
        env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "60"
        env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = "30"
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
        script = ("SET SESSION max_execution_time=20000;\n"
                  "START TRANSACTION READ ONLY;\nSELECT @@read_only;\n"
                  + sql + ";\nCOMMIT;\n")
        try:
            result = subprocess.run(args, input=script, text=True,
                                    encoding="utf-8", capture_output=True,
                                    env=env, timeout=110, check=False)
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
        rows = self.query("""
SELECT dt AS day,
       SUM(impressions) AS impression,
       SUM(clicks) AS click,
       SUM(installs) AS install,
       SUM(af_revenue0) AS revenue,
       SUM(spend) AS cost
FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX (wcds)
WHERE product = 'Dramawave'
  AND w2a_page_id = 2334
  AND ad_account_id IN (%s)
GROUP BY dt
ORDER BY dt
""".strip() % values)
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
                    "day": day, "impression": int(row[1]), "click": int(row[2]),
                    "install": int(row[3]), "revenue": amount[0], "cost": amount[1],
                })
        except (TypeError, ValueError, InvalidOperation):
            raise SourceError("invalid source metric result") from None
        return result

"""Refresh X Tag selection metrics through the host FIFO. Only the SQL client needs root."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


RUNTIME = Path("/opt/x-tag-publisher/current")
DB = "/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3"
LOCK = "/run/x-tag-auto/metric.lock"
SOURCE = "kunlunads_dev.ads_custom_source_insight"
BUCKET = 32768


def schedule(days, pointers, hour):
    """Yesterday each hour, missing days immediately, older ready days in rotation."""
    yesterday, *older = sorted(days, reverse=True)
    rotate = [older[hour % len(older)]] if older else []
    return list(dict.fromkeys([yesterday, *(day for day in older if day not in pointers), *rotate]))


def pointers():
    with sqlite3.connect("file:" + DB + "?mode=ro", uri=True, timeout=10) as db:
        return dict(db.execute("""
            SELECT p.metric_date,p.generation_id FROM x_auto_metric_active_pointer p
            JOIN x_auto_metric_generation g ON g.id=p.generation_id
            WHERE p.product='Dramawave' AND p.platform=0 AND g.status='ready'
        """))


def mysql(statement):
    if os.environ["X_AUTO_POST_MYSQL_USER"] != "ads_aius":
        raise RuntimeError("metric reader does not match the host FIFO account")
    # Never replay a partially emitted stdout stream. The next hourly run can retry the day.
    env = dict(os.environ, MYSQL_PWD=os.environ["X_AUTO_POST_MYSQL_PASSWORD"],
               SQL_GATE_JOB_NAME="x-tag-selection-metrics", SQL_GATE_RETRY_DELAYS_SECONDS="",
               SQL_GATE_EXEC_TIMEOUT_SECONDS="240")
    env.pop("SQL_GATE_BYPASS", None)
    client = ["/usr/bin/mysql", "--no-defaults", "--protocol=TCP", "--host=101.32.56.53",
              "--port=63350", "--user=" + env["X_AUTO_POST_MYSQL_USER"], "--compress",
              "--batch", "--raw", "--quick", "--skip-column-names", "--skip-reconnect",
              "--connect-timeout=5", "--default-character-set=utf8mb4"]
    sql = ("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
           "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY; SELECT @@read_only;\n"
           + statement + "\nCOMMIT; SELECT 'x_tag_metric_complete';\n")
    proc = subprocess.run(client, input=sql.encode(), capture_output=True, env=env)
    if proc.returncode:
        errno = re.search(rb"ERROR (\d+)|SQL_GATE_[A-Z_]+", proc.stderr)
        code = errno.group().decode() if errno else str(proc.returncode)
        raise RuntimeError("metric replica query failed (%s)" % code)
    lines = proc.stdout.splitlines()
    if lines[:1] != [b"1"] or lines[-1:] != [b"x_tag_metric_complete"]:
        raise RuntimeError("metric replica response incomplete")
    return lines[1:-1]


def ranges(buckets):
    """The index-only planning read sets costs, never the extent of the snapshot."""
    low, count = 0, 0
    for bucket, size in buckets:
        count += size
        if count >= 2 * BUCKET:
            high = (bucket + 1) * BUCKET
            yield low, high
            low, count = high, 0
    yield low, None


def fetch_day(day):
    parsed = date.fromisoformat(day)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if parsed.isoformat() != day or not 1 <= (today - parsed).days <= 30:
        raise ValueError("expected one of the last 30 complete Beijing dates")
    buckets = mysql(f"""
        SELECT /*+ MAX_EXECUTION_TIME(30000) */ id DIV {BUCKET},COUNT(*)
        FROM {SOURCE} FORCE INDEX (stats_date) WHERE dt='{day}'
        GROUP BY id DIV {BUCKET} ORDER BY id DIV {BUCKET};
    """)
    statements = []
    for low, high in ranges(tuple(map(int, row.split(b"\t"))) for row in buckets):
        upper_bound = "" if high is None else f" AND id < {high}"
        statements.append(f"""
            SELECT /*+ MAX_EXECUTION_TIME(30000) */
                HEX(WEIGHT_STRING(TRIM(data_source_id))),HEX(MIN(BINARY TRIM(data_source_id))),
                TRIM(resource_id), SUM(COALESCE(spend,0)),SUM(COALESCE(af_revenue0,0))
            FROM {SOURCE} FORCE INDEX (stats_date)
            WHERE dt='{day}' AND id >= {low}{upper_bound}
                AND product='Dramawave' AND platform=0 AND data_source_id IS NOT NULL
                AND TRIM(data_source_id)<>'' AND resource_id REGEXP '^[1-9][0-9]*$'
            GROUP BY TRIM(data_source_id),TRIM(resource_id) ORDER BY NULL;
        """)
    # All partial sums belong to one replica snapshot. Preserve MySQL's content-ID
    # collation across slices and the original content/material ordering for the store.
    totals = {}
    for line in mysql("\n".join(statements)):
        weight, content, material, spend, revenue = line.split(b"\t")
        key = (weight, material)
        name = bytes.fromhex(content.decode()).decode("utf-8")
        cost, income = Decimal(spend.decode()), Decimal(revenue.decode())
        if key in totals:
            prior, prior_cost, prior_income = totals[key]
            name, cost, income = min(name, prior), cost + prior_cost, income + prior_income
        totals[key] = (name, cost, income)
    return [[totals[key][0], key[1].decode(), str(totals[key][1]), str(totals[key][2])]
            for key in sorted(totals)]


def store_operation(args):
    if os.geteuid() == 0:
        raise RuntimeError("cache must be handled by the X Tag service account")
    sys.path.insert(0, str(RUNTIME))
    from features.x_auto_posts.core import XAutoPostStore
    from scripts.x_auto_post_metric_runner import metric_refresh_lock

    if args.store == "plan":
        now = datetime.now(timezone(timedelta(hours=8)))
        lookback = int(os.environ.get("X_AUTO_POST_METRIC_LOOKBACK_DAYS", "7"))
        if not 1 <= lookback <= 30:
            raise ValueError("invalid metric lookback")
        days = [(now.date() - timedelta(days=n)).isoformat() for n in range(1, lookback + 1)]
        active = pointers()
        chosen = schedule(days, active, now.hour)
        return [[day, active.get(day, 0)] for day in chosen]

    payload = json.load(sys.stdin)
    day = payload["day"]
    with metric_refresh_lock(LOCK) as acquired:
        if not acquired:
            raise RuntimeError("metric cache writer is busy")
        if pointers().get(day, 0) != payload["previous"]:
            return {"day": day, "status": "superseded"}
        store = XAutoPostStore(DB)
        fields = ("content_id", "material_id", "spend", "af_revenue0")
        generation = store.record_metric_generation(
            platform=0, metric_date=day, product="Dramawave",
            rows=(dict(zip(fields, row)) for row in payload["rows"]),
            refreshed_at_utc=payload["started"])
        ready = store.activate_metric_generation(generation.id)
        return {"day": day, "generation": ready.id, "rows": ready.row_count, "status": ready.status}


def cache(action, payload=None):
    import pwd
    account = pwd.getpwnam("x-post-daily")
    cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--store", action]
    env = {"PATH": "/usr/bin:/bin", "X_AUTO_POST_METRIC_LOOKBACK_DAYS":
           os.environ.get("X_AUTO_POST_METRIC_LOOKBACK_DAYS", "7")}
    proc = subprocess.run(cmd, user=account.pw_uid, group=account.pw_gid, extra_groups=[], env=env,
                          input=json.dumps(payload) if payload is not None else "",
                          stdout=subprocess.PIPE, text=True, check=True)
    return json.loads(proc.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", choices=("plan", "apply"))
    args = parser.parse_args()
    if args.store:
        print(json.dumps(store_operation(args)))
        return 0
    failed = False
    for day, previous in cache("plan"):
        started = datetime.now(timezone.utc)
        tick = time.monotonic()
        try:
            rows = fetch_day(day)
            result = cache("apply", {"day": day, "previous": previous, "rows": rows,
                                     "started": started.isoformat()})
        except (OSError, ValueError, ArithmeticError, RuntimeError, subprocess.SubprocessError) as exc:
            result = {"day": day, "status": "failed", "error": str(exc)[:250]}
            failed = True
        print(json.dumps(dict(result, seconds=round(time.monotonic() - tick, 2))), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

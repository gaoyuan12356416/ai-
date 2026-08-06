#!/usr/bin/env python3
"""Refresh the Dramawave attribution comparison SQLite cache from read-only MySQL."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import json
import os
import sys
import uuid
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from common import (
    BASE_METRICS,
    FACT_DIMENSIONS,
    FLOAT_BASE_METRICS,
    MIN_DATE,
    ROLLUP_TABLE_DIMENSIONS,
    connect_sqlite,
    db_path,
    get_meta,
    insert_facts,
    iso_now,
    parse_date,
    retention_start,
    set_meta,
    verify_data_disk,
    yyyymmdd,
)


CUSTOM_TABLE = "kunlunads_dev.ads_custom_source_insight"
D7_TABLE = "kunlunads_dev.ads_app_revenues"
D30_TABLE = "kunlunads_dev.ads_app_revenues_30d"
ADMIN_TABLE = "kunlunads_dev.admin_users"
CHANNEL_NAMES = {"0": "Meta", "1": "Google", "3": "TikTok", "4": "Kwai"}

CUSTOM_SQL = f"""
SELECT
  c.dt,
  COALESCE(CAST(c.platform AS CHAR), '') AS channel_id,
  COALESCE(c.app_id, '') AS app_id,
  COALESCE(CAST(c.optimizer AS CHAR), '') AS optimizer_id,
  COALESCE(NULLIF(u.name,''), NULLIF(u.username,''), CAST(c.optimizer AS CHAR), '') AS optimizer_name,
  COALESCE(c.country_group, '') AS country_group,
  COALESCE(CAST(c.ad_account_id AS CHAR), '') AS ad_account_id,
  COALESCE(CAST(c.campaign_id AS CHAR), '') AS campaign_id,
  '' AS campaign_name,
  COALESCE(CAST(c.adset_id AS CHAR), '') AS adset_id,
  '' AS adset_name,
  COALESCE(CAST(c.ad_id AS CHAR), '') AS ad_id,
  '' AS ad_name,
  COALESCE(SUM(c.spend),0) AS spend,
  COALESCE(SUM(c.impressions),0) AS impressions,
  COALESCE(SUM(c.clicks),0) AS clicks,
  COALESCE(SUM(c.installs),0) AS installs,
  COALESCE(SUM(c.af_installs),0) AS af_installs,
  MAX(c.updated_at) AS source_updated_at
FROM {CUSTOM_TABLE} c FORCE INDEX (pss)
LEFT JOIN {ADMIN_TABLE} u ON u.id=c.optimizer
WHERE c.dt=%s AND c.product='Dramawave'
GROUP BY c.dt,c.platform,c.app_id,c.optimizer,u.name,u.username,c.country_group,
         c.ad_account_id,c.campaign_id,c.adset_id,c.ad_id
"""

REVENUE_SQL = """
SELECT
  COALESCE(CAST(campaign_id AS CHAR), '') AS campaign_id,
  MAX(COALESCE(campaign_name,'')) AS campaign_name,
  COALESCE(CAST(adset_id AS CHAR), '') AS adset_id,
  MAX(COALESCE(adset_name,'')) AS adset_name,
  COALESCE(CAST(ad_id AS CHAR), '') AS ad_id,
  MAX(COALESCE(ad_name,'')) AS ad_name,
  COALESCE(SUM(users),0) AS users,
  COALESCE(SUM(purchase_d0_count),0) AS purchase_d0,
  COALESCE(SUM(purchase_d7_count),0) AS purchase_d7,
  COALESCE(SUM(revenue_iaa_d0),0) AS revenue_iaa_d0,
  COALESCE(SUM(revenue_iap_d0),0) AS revenue_iap_d0,
  COALESCE(SUM(revenue_iaa_d7),0) AS revenue_iaa_d7,
  COALESCE(SUM(revenue_iap_d7),0) AS revenue_iap_d7,
  COALESCE(SUM(ad_impression_count),0) AS ad_impression_count,
  MAX(updated_at) AS source_updated_at
FROM {table}
WHERE dt=%s
GROUP BY campaign_id,adset_id,ad_id
"""

CUSTOM_INTEGER_METRICS = ("impressions", "clicks", "installs", "af_installs")
REVENUE_INTEGER_METRICS = ("users", "purchase_d0", "purchase_d7", "ad_impression_count")
REVENUE_FLOAT_METRICS = ("revenue_iaa_d0", "revenue_iap_d0", "revenue_iaa_d7", "revenue_iap_d7")
REVENUE_METRICS = REVENUE_INTEGER_METRICS + REVENUE_FLOAT_METRICS


def load_env_file(path: str | None) -> None:
    if not path:
        return
    source = Path(path)
    if not source.exists():
        raise RuntimeError(f"environment file not found: {source}")
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def require_source_config() -> dict[str, Any]:
    names = {
        "host": "ADMIN_MAPPING_MYSQL_HOST",
        "port": "ADMIN_MAPPING_MYSQL_PORT",
        "user": "ADMIN_MAPPING_MYSQL_USER",
        "password": "ADMIN_MAPPING_MYSQL_PASSWORD",
        "database": "ADMIN_MAPPING_MYSQL_DATABASE",
    }
    missing = [env_name for env_name in names.values() if not os.environ.get(env_name)]
    if missing:
        raise RuntimeError("missing source environment variables: " + ", ".join(missing))
    port = int(os.environ[names["port"]])
    if port != 63350:
        raise RuntimeError(f"refusing non-read-only MySQL port {port}; expected 63350")
    return {
        "host": os.environ[names["host"]],
        "port": port,
        "user": os.environ[names["user"]],
        "password": os.environ[names["password"]],
        "database": os.environ[names["database"]],
    }


@contextlib.contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - Windows local tests do not execute production refresh.
            pass
        except BlockingIOError as exc:
            raise RuntimeError("another refresh is already running") from exc
        yield
    finally:
        handle.close()


@contextlib.contextmanager
def mysql_connection(config: dict[str, Any]):
    try:
        import pymysql
        from pymysql.cursors import SSDictCursor
    except ImportError as exc:  # pragma: no cover - production host has PyMySQL.
        raise RuntimeError("PyMySQL is required for refresh_cache.py") from exc
    connection = pymysql.connect(
        **config,
        charset="utf8mb4",
        cursorclass=SSDictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=600,
        write_timeout=30,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@read_only AS read_only")
            row = cursor.fetchone()
            if not row or int(row["read_only"]) != 1:
                raise RuntimeError("source server did not report @@read_only=1")
        yield connection
    finally:
        connection.close()


@contextlib.contextmanager
def source_day_snapshot(connection: Any):
    """Keep custom, D7 and D30 reads for one day on one repeatable-read snapshot."""
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION READ ONLY, WITH CONSISTENT SNAPSHOT")
    try:
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def integer(value: Any) -> int:
    return int(round(number(value)))


def timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat(sep=" ")
    return str(value)


def fetch_custom(connection: Any, day: dt.date) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    source_updated_at = ""
    with connection.cursor() as cursor:
        cursor.execute(CUSTOM_SQL, (day.isoformat(),))
        for raw in cursor:
            row = {key: text(raw.get(key)) for key in FACT_DIMENSIONS if key not in BASE_METRICS}
            row.update(
                {
                    "dt": day.isoformat(),
                    "channel_id": text(raw.get("channel_id")),
                    "channel": CHANNEL_NAMES.get(text(raw.get("channel_id")), f"渠道 {text(raw.get('channel_id'))}"),
                    "product": "Dramawave",
                    "matched_grain": "none",
                    "mapping_status": "spend_only",
                    "spend": number(raw.get("spend")),
                    **{key: integer(raw.get(key)) for key in CUSTOM_INTEGER_METRICS},
                }
            )
            rows.append(row)
            source_updated_at = max(source_updated_at, timestamp(raw.get("source_updated_at")))
    return rows, source_updated_at


def fetch_revenue(connection: Any, table: str, day: dt.date) -> tuple[list[dict[str, Any]], str]:
    if table not in {D7_TABLE, D30_TABLE}:
        raise RuntimeError("unexpected revenue table")
    rows: list[dict[str, Any]] = []
    source_updated_at = ""
    with connection.cursor() as cursor:
        cursor.execute(REVENUE_SQL.format(table=table), (yyyymmdd(day),))
        for raw in cursor:
            row = {
                "campaign_id": text(raw.get("campaign_id")),
                "campaign_name": text(raw.get("campaign_name")),
                "adset_id": text(raw.get("adset_id")),
                "adset_name": text(raw.get("adset_name")),
                "ad_id": text(raw.get("ad_id")),
                "ad_name": text(raw.get("ad_name")),
                **{key: integer(raw.get(key)) for key in REVENUE_INTEGER_METRICS},
                **{key: number(raw.get(key)) for key in REVENUE_FLOAT_METRICS},
            }
            rows.append(row)
            source_updated_at = max(source_updated_at, timestamp(raw.get("source_updated_at")))
    return rows, source_updated_at


def custom_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        text(row.get(key))
        for key in (
            "dt",
            "channel_id",
            "app_id",
            "optimizer_id",
            "country_group",
            "ad_account_id",
            "campaign_id",
            "adset_id",
        )
    )


def blank_fact(day: dt.date) -> dict[str, Any]:
    result = {key: "" for key in FACT_DIMENSIONS}
    result.update({key: 0 for key in BASE_METRICS})
    result.update({"dt": day.isoformat(), "product": "Dramawave", "matched_grain": "none", "mapping_status": "spend_only"})
    return result


def merge_custom_rows(day: dt.date, rows: Iterable[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    facts: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        identity = custom_identity(row)
        fact = facts.get(identity)
        if fact is None:
            fact = blank_fact(day)
            for key in FACT_DIMENSIONS:
                if key in row:
                    fact[key] = row[key]
            facts[identity] = fact
        else:
            for label in ("optimizer_name", "campaign_name", "adset_name", "ad_name"):
                if not fact.get(label) and row.get(label):
                    fact[label] = row[label]
            if fact.get("ad_id") != row.get("ad_id"):
                fact["ad_id"] = ""
                fact["ad_name"] = ""
        fact["spend"] += number(row.get("spend"))
        for key in CUSTOM_INTEGER_METRICS:
            fact[key] += integer(row.get(key))
    return facts


def revenue_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (text(row.get("campaign_id")), text(row.get("adset_id")), text(row.get("ad_id")))


def merge_revenue_sources(
    old_rows: Iterable[dict[str, Any]], new_rows: Iterable[dict[str, Any]]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for prefix, rows in (("d7", old_rows), ("d30", new_rows)):
        for row in rows:
            key = revenue_key(row)
            target = merged.setdefault(
                key,
                {
                    "campaign_id": key[0],
                    "adset_id": key[1],
                    "ad_id": key[2],
                    "campaign_name": "",
                    "adset_name": "",
                    "ad_name": "",
                    "d7_present": False,
                    "d30_present": False,
                },
            )
            target[f"{prefix}_present"] = True
            for name in ("campaign_name", "adset_name", "ad_name"):
                if row.get(name):
                    target[name] = text(row[name])
            for metric in REVENUE_METRICS:
                target[f"{prefix}_{metric}"] = target.get(f"{prefix}_{metric}", 0) + row.get(metric, 0)
    return merged


def build_lookup(
    facts: dict[tuple[str, ...], dict[str, Any]], custom_rows: Iterable[dict[str, Any]]
) -> dict[str, dict[str, set[tuple[str, ...]]]]:
    lookup: dict[str, dict[str, set[tuple[str, ...]]]] = {
        "ad": defaultdict(set),
        "adset": defaultdict(set),
        "campaign": defaultdict(set),
    }
    for row in custom_rows:
        identity = custom_identity(row)
        if identity not in facts:
            continue
        if row.get("ad_id"):
            lookup["ad"][text(row["ad_id"])].add(identity)
        if row.get("adset_id"):
            lookup["adset"][text(row["adset_id"])].add(identity)
        if row.get("campaign_id"):
            lookup["campaign"][text(row["campaign_id"])].add(identity)
    return lookup


def match_candidates(
    row: dict[str, Any], lookup: dict[str, dict[str, set[tuple[str, ...]]]]
) -> tuple[str, set[tuple[str, ...]]]:
    # Fall back only when the finer identifier is absent. A present-but-missing
    # ad_id must never be silently attributed to another ad in the same ad set.
    for level, key_name in (("ad", "ad_id"), ("adset", "adset_id"), ("campaign", "campaign_id")):
        key = text(row.get(key_name))
        if key:
            return level, set(lookup[level].get(key, set()))
    return "unmatched", set()


def add_revenue(target: dict[str, Any], source: dict[str, Any], prefix: str) -> None:
    for metric in REVENUE_METRICS:
        target[f"{prefix}_{metric}"] += source.get(f"{prefix}_{metric}", 0)


def common_candidate_fact(day: dt.date, candidates: list[dict[str, Any]], revenue: dict[str, Any], level: str) -> dict[str, Any]:
    fact = blank_fact(day)
    for key in (
        "channel",
        "channel_id",
        "app_id",
        "optimizer_id",
        "optimizer_name",
        "country_group",
        "ad_account_id",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
    ):
        values = {text(candidate.get(key)) for candidate in candidates}
        if len(values) == 1:
            fact[key] = values.pop()
    for key in ("campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id"):
        if revenue.get(key):
            fact[key] = text(revenue[key])
    fact["ad_name"] = ""
    fact["matched_grain"] = level
    fact["mapping_status"] = "ambiguous"
    return fact


def map_day(
    day: dt.date,
    custom_rows: list[dict[str, Any]],
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    facts = merge_custom_rows(day, custom_rows)
    custom_metrics = ("spend",) + CUSTOM_INTEGER_METRICS
    custom_source_sums = {
        metric: sum(number(row.get(metric, 0)) for row in custom_rows) for metric in custom_metrics
    }
    custom_base_sums = {
        metric: sum(number(row.get(metric, 0)) for row in facts.values()) for metric in custom_metrics
    }
    for metric in custom_metrics:
        tolerance = 0.0 if metric in CUSTOM_INTEGER_METRICS else max(0.01, abs(custom_source_sums[metric]) * 1e-9)
        if abs(custom_base_sums[metric] - custom_source_sums[metric]) > tolerance:
            raise RuntimeError(
                f"custom {metric} source-merge conservation failed: "
                f"{custom_base_sums[metric]} != {custom_source_sums[metric]}"
            )
    lookup = build_lookup(facts, custom_rows)
    revenues = merge_revenue_sources(old_rows, new_rows)
    ambiguous_facts: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "custom_source_rows": len(custom_rows),
        "custom_fact_rows": len(facts),
        "revenue_union_rows": len(revenues),
        "excluded_unmatched_rows": 0,
        "excluded_unmatched_d7_rows": 0,
        "excluded_unmatched_d30_rows": 0,
        "d7_source_rows": len(old_rows),
        "d30_source_rows": len(new_rows),
    }
    source_sums = {
        "d7": {metric: sum(number(row.get(metric, 0)) for row in old_rows) for metric in REVENUE_METRICS},
        "d30": {metric: sum(number(row.get(metric, 0)) for row in new_rows) for metric in REVENUE_METRICS},
    }
    merged_sums = {
        prefix: {
            metric: sum(
                number(row.get(f"{prefix}_{metric}", 0))
                for row in revenues.values()
                if row.get(f"{prefix}_present")
            )
            for metric in REVENUE_METRICS
        }
        for prefix in ("d7", "d30")
    }
    for prefix in ("d7", "d30"):
        stats[f"{prefix}_merged_keys"] = sum(
            1 for row in revenues.values() if row.get(f"{prefix}_present")
        )
        for metric in REVENUE_METRICS:
            expected = source_sums[prefix][metric]
            actual = merged_sums[prefix][metric]
            tolerance = 0.0 if metric in REVENUE_INTEGER_METRICS else max(0.01, abs(expected) * 1e-9)
            if abs(actual - expected) > tolerance:
                raise RuntimeError(
                    f"{prefix} {metric} source-merge conservation failed: {actual} != {expected}"
                )
    excluded_sums = {prefix: {metric: 0.0 for metric in REVENUE_METRICS} for prefix in ("d7", "d30")}
    candidate_sums = {prefix: {metric: 0.0 for metric in REVENUE_METRICS} for prefix in ("d7", "d30")}
    for revenue in revenues.values():
        level, candidate_ids = match_candidates(revenue, lookup)
        if not candidate_ids:
            stats["excluded_unmatched_rows"] += 1
            for prefix in ("d7", "d30"):
                if revenue.get(f"{prefix}_present"):
                    stats[f"excluded_unmatched_{prefix}_rows"] += 1
                    for metric in REVENUE_METRICS:
                        excluded_sums[prefix][metric] += number(revenue.get(f"{prefix}_{metric}", 0))
            continue
        candidates = [facts[identity] for identity in sorted(candidate_ids)]
        mapped = len(candidates) == 1
        target = candidates[0] if mapped else common_candidate_fact(day, candidates, revenue, level)
        if not mapped:
            ambiguous_facts.append(target)
        else:
            for label in ("campaign_name", "adset_name"):
                if revenue.get(label):
                    target[label] = text(revenue[label])
            # The custom table has no name columns and this collapsed identity
            # may represent multiple ads. Keep ad_name empty instead of
            # attaching one revenue row's label to the whole fact.
            target["ad_name"] = ""
            if target["matched_grain"] in {"none", level}:
                target["matched_grain"] = level
                target["mapping_status"] = "mapped"
            else:
                target["matched_grain"] = "mixed"
                target["mapping_status"] = "mapped"
        for prefix in ("d7", "d30"):
            if not revenue.get(f"{prefix}_present"):
                continue
            target[f"{prefix}_candidate_keys"] += 1
            target[f"{prefix}_{'mapped' if mapped else 'ambiguous'}_keys"] += 1
            add_revenue(target, revenue, prefix)
            for metric in REVENUE_METRICS:
                candidate_sums[prefix][metric] += number(revenue.get(f"{prefix}_{metric}", 0))
    output = list(facts.values()) + ambiguous_facts
    for metric in custom_metrics:
        output_total = sum(number(row.get(metric, 0)) for row in output)
        tolerance = 0.0 if metric in CUSTOM_INTEGER_METRICS else max(0.01, abs(custom_source_sums[metric]) * 1e-9)
        if abs(output_total - custom_source_sums[metric]) > tolerance:
            raise RuntimeError(
                f"custom {metric} output conservation failed: {output_total} != {custom_source_sums[metric]}"
            )
        stats[f"custom_source_{metric}"] = custom_source_sums[metric]
        stats[f"custom_merged_{metric}"] = custom_base_sums[metric]
        stats[f"custom_fact_{metric}"] = output_total
    for prefix in ("d7", "d30"):
        for metric in REVENUE_METRICS:
            actual = sum(number(row.get(f"{prefix}_{metric}", 0)) for row in output)
            expected = candidate_sums[prefix][metric]
            tolerance = 0.0 if metric in REVENUE_INTEGER_METRICS else max(0.01, abs(expected) * 1e-9)
            if abs(actual - expected) > tolerance:
                raise RuntimeError(f"{prefix} {metric} conservation failed: {actual} != {expected}")
            excluded = excluded_sums[prefix][metric]
            if abs(merged_sums[prefix][metric] - expected - excluded) > tolerance:
                raise RuntimeError(
                    f"{prefix} {metric} merge-scope conservation failed: "
                    f"merged={merged_sums[prefix][metric]} candidate={expected} excluded={excluded}"
                )
            stats[f"{prefix}_source_{metric}"] = source_sums[prefix][metric]
            stats[f"{prefix}_merged_{metric}"] = merged_sums[prefix][metric]
            stats[f"{prefix}_candidate_{metric}"] = expected
            stats[f"{prefix}_excluded_unscoped_{metric}"] = excluded
            stats[f"{prefix}_fact_{metric}"] = actual
        stats[f"{prefix}_candidate_keys"] = sum(integer(row.get(f"{prefix}_candidate_keys", 0)) for row in output)
        stats[f"{prefix}_mapped_keys"] = sum(integer(row.get(f"{prefix}_mapped_keys", 0)) for row in output)
        stats[f"{prefix}_ambiguous_keys"] = sum(integer(row.get(f"{prefix}_ambiguous_keys", 0)) for row in output)
    stats["fact_rows"] = len(output)
    return output, stats


def encode_stage(rows: list[dict[str, Any]], stats: dict[str, Any], source_times: dict[str, str]) -> bytes:
    raw = json.dumps({"rows": rows, "stats": stats, "source_times": source_times}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=5)


def decode_stage(payload: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(payload).decode("utf-8"))


def additive_totals(conn: Any, table: str, day: str) -> dict[str, float]:
    if table not in {"attribution_fact", *ROLLUP_TABLE_DIMENSIONS}:
        raise RuntimeError(f"unexpected conservation table: {table}")
    selected = ",".join(f"COALESCE(SUM({metric}),0) AS {metric}" for metric in BASE_METRICS)
    row = conn.execute(f"SELECT {selected} FROM {table} WHERE dt=?", (day,)).fetchone()
    return {metric: number(row[metric]) for metric in BASE_METRICS}


def rebuild_rollups_for_day(conn: Any, day: dt.date | str) -> dict[str, int]:
    day_text = day.isoformat() if isinstance(day, dt.date) else str(day)
    fact_totals = additive_totals(conn, "attribution_fact", day_text)
    counts: dict[str, int] = {}
    for table, dimensions in ROLLUP_TABLE_DIMENSIONS.items():
        conn.execute(f"DELETE FROM {table} WHERE dt=?", (day_text,))
        columns = dimensions + BASE_METRICS
        selected = list(dimensions) + [f"SUM({metric})" for metric in BASE_METRICS]
        conn.execute(
            f"INSERT INTO {table}({','.join(columns)}) "
            f"SELECT {','.join(selected)} FROM attribution_fact WHERE dt=? "
            f"GROUP BY {','.join(dimensions)}",
            (day_text,),
        )
        rollup_totals = additive_totals(conn, table, day_text)
        for metric in BASE_METRICS:
            expected = fact_totals[metric]
            actual = rollup_totals[metric]
            tolerance = max(0.01, abs(expected) * 1e-9) if metric in FLOAT_BASE_METRICS else 0.0
            if abs(actual - expected) > tolerance:
                raise RuntimeError(
                    f"{table} {day_text} {metric} conservation failed: {actual} != {expected}"
                )
        counts[f"{table}_rows"] = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE dt=?", (day_text,)).fetchone()["n"]
        )
    return counts


def choose_default_dates(conn: Any, today: dt.date) -> tuple[list[dt.date], dt.date | None]:
    recent = [value for value in (today, today - dt.timedelta(days=1)) if value >= MIN_DATE]
    historical_end = today - dt.timedelta(days=2)
    historical: dt.date | None = None
    if historical_end >= retention_start(today):
        oldest = retention_start(today)
        meta = get_meta(conn)
        cursor_raw = text(meta.get("history_cursor"))
        try:
            cursor = dt.date.fromisoformat(cursor_raw)
        except ValueError:
            cursor = oldest - dt.timedelta(days=1)
        historical = cursor + dt.timedelta(days=1)
        if historical < oldest or historical > historical_end:
            historical = oldest
    dates = list(dict.fromkeys(recent + ([historical] if historical else [])))
    return dates, historical


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    if start > end:
        raise ValueError("bootstrap start cannot be later than end")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def resolve_dates(args: argparse.Namespace, conn: Any, today: dt.date) -> tuple[list[dt.date], dt.date | None]:
    if args.date:
        dates = [parse_date(value, name="date") for value in args.date]
        historical = None
    elif args.bootstrap_start or args.bootstrap_end:
        if not args.bootstrap_start or not args.bootstrap_end:
            raise ValueError("--bootstrap-start and --bootstrap-end must be supplied together")
        dates = date_range(parse_date(args.bootstrap_start, name="bootstrap_start"), parse_date(args.bootstrap_end, name="bootstrap_end"))
        historical = None
    else:
        dates, historical = choose_default_dates(conn, today)
    if any(value > today for value in dates):
        raise ValueError(f"cannot refresh future date after Beijing today {today.isoformat()}")
    earliest = retention_start(today)
    dates = [value for value in dates if value >= earliest]
    if not dates:
        raise ValueError("no dates remain inside retention window")
    return list(dict.fromkeys(dates)), historical


def refresh(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(args.env_file or os.environ.get("DRAMAWAVE_SOURCE_ENV_FILE"))
    path = Path(args.db_path or db_path())
    verify_data_disk(path, skip=args.skip_mount_check)
    source_config = require_source_config()
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    run_id = uuid.uuid4().hex
    started_at = iso_now()
    with process_lock(path.parent / "refresh.lock"):
        with connect_sqlite(path) as conn:
            dates, historical = resolve_dates(args, conn, today)
            conn.execute("DELETE FROM refresh_stage WHERE created_at < ?", ((dt.datetime.now() - dt.timedelta(days=2)).isoformat(),))
            conn.commit()
        print("refresh plan:", ",".join(value.isoformat() for value in dates), flush=True)
        log_ids: dict[str, int] = {}
        try:
            with mysql_connection(source_config) as source:
                for day in dates:
                    with connect_sqlite(path) as conn:
                        cursor = conn.execute(
                            "INSERT INTO refresh_log(dt,started_at,status,detail) VALUES(?,?,?,?)",
                            (day.isoformat(), started_at, "running", f"run_id={run_id}"),
                        )
                        log_ids[day.isoformat()] = int(cursor.lastrowid)
                        conn.commit()
                    with source_day_snapshot(source):
                        custom_rows, custom_time = fetch_custom(source, day)
                        old_rows, old_time = fetch_revenue(source, D7_TABLE, day)
                        new_rows, new_time = fetch_revenue(source, D30_TABLE, day)
                    if not custom_rows:
                        raise RuntimeError(f"refusing empty custom source for {day.isoformat()}")
                    if not old_rows or not new_rows:
                        raise RuntimeError(
                            f"refusing empty revenue source for {day.isoformat()}: d7={len(old_rows)} d30={len(new_rows)}"
                        )
                    facts, stats = map_day(day, custom_rows, old_rows, new_rows)
                    with connect_sqlite(path, readonly=True) as existing_conn:
                        existing = existing_conn.execute(
                            "SELECT COUNT(*) fact_rows,SUM(d7_candidate_keys) d7_keys,SUM(d30_candidate_keys) d30_keys "
                            "FROM attribution_fact WHERE dt=?",
                            (day.isoformat(),),
                        ).fetchone()
                    if int(existing["fact_rows"] or 0) and not facts:
                        raise RuntimeError(f"refusing to replace non-empty cache with empty facts for {day.isoformat()}")
                    if int(existing["d7_keys"] or 0) and not int(stats["d7_candidate_keys"]):
                        raise RuntimeError(f"refusing zero D7 candidates over prior non-zero cache for {day.isoformat()}")
                    if int(existing["d30_keys"] or 0) and not int(stats["d30_candidate_keys"]):
                        raise RuntimeError(f"refusing zero D30 candidates over prior non-zero cache for {day.isoformat()}")
                    payload = encode_stage(
                        facts,
                        stats,
                        {
                            "ads_custom_source_insight": custom_time,
                            "app_revenues": old_time,
                            "app_revenues_30d": new_time,
                        },
                    )
                    with connect_sqlite(path) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO refresh_stage(run_id,dt,created_at,payload) VALUES(?,?,?,?)",
                            (run_id, day.isoformat(), iso_now(), payload),
                        )
                        conn.commit()
                    print(f"staged {day.isoformat()}: {len(facts)} facts", flush=True)
            version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + run_id[:8]
            generated_at = iso_now()
            source_max: dict[str, str] = {}
            with connect_sqlite(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous_meta = get_meta(conn)
                previous_rollups_current = bool(previous_meta.get("data_version")) and (
                    previous_meta.get("rollup_version") == previous_meta.get("data_version")
                )
                previous_times = previous_meta.get("source_max_updated_at") or {}
                if isinstance(previous_times, dict):
                    source_max.update({key: text(value) for key, value in previous_times.items()})
                rebuilt_dates: set[str] = set()
                for day in dates:
                    stage_row = conn.execute(
                        "SELECT payload FROM refresh_stage WHERE run_id=? AND dt=?",
                        (run_id, day.isoformat()),
                    ).fetchone()
                    if not stage_row:
                        raise RuntimeError(f"missing staged payload for {day.isoformat()}")
                    staged = decode_stage(stage_row["payload"])
                    conn.execute("DELETE FROM attribution_fact WHERE dt=?", (day.isoformat(),))
                    insert_facts(conn, staged["rows"])
                    staged["stats"].update(rebuild_rollups_for_day(conn, day))
                    rebuilt_dates.add(day.isoformat())
                    for key, value in staged["source_times"].items():
                        source_max[key] = max(source_max.get(key, ""), text(value))
                    log_id = log_ids[day.isoformat()]
                    conn.execute(
                        "UPDATE refresh_log SET finished_at=?,status='success',fact_rows=?,detail=?,"
                        "source_custom_updated_at=?,source_d7_updated_at=?,source_d30_updated_at=?,data_version=? WHERE id=?",
                        (
                            generated_at,
                            int(staged["stats"]["fact_rows"]),
                            json.dumps(staged["stats"], ensure_ascii=False, separators=(",", ":")),
                            staged["source_times"].get("ads_custom_source_insight", ""),
                            staged["source_times"].get("app_revenues", ""),
                            staged["source_times"].get("app_revenues_30d", ""),
                            version,
                            log_id,
                        ),
                    )
                keep_from = retention_start(today).isoformat()
                # Existing caches predate the rollup tables. Rebuild every
                # retained fact day once before advertising a current rollup
                # version; later refreshes only rebuild their target dates.
                if not previous_rollups_current:
                    retained_dates = [
                        str(row["dt"])
                        for row in conn.execute(
                            "SELECT DISTINCT dt FROM attribution_fact WHERE dt>=? ORDER BY dt",
                            (keep_from,),
                        )
                    ]
                    for retained_day in retained_dates:
                        if retained_day not in rebuilt_dates:
                            rebuild_rollups_for_day(conn, retained_day)
                conn.execute("DELETE FROM attribution_fact WHERE dt < ?", (keep_from,))
                conn.execute("DELETE FROM attribution_filter_daily WHERE dt < ?", (keep_from,))
                conn.execute("DELETE FROM attribution_campaign_daily WHERE dt < ?", (keep_from,))
                conn.execute("DELETE FROM refresh_log WHERE dt < ?", (keep_from,))
                conn.execute("DELETE FROM refresh_stage WHERE run_id=?", (run_id,))
                set_meta(conn, "data_version", version)
                set_meta(conn, "rollup_version", version)
                set_meta(conn, "generated_at", generated_at)
                set_meta(conn, "source_max_updated_at", source_max)
                set_meta(conn, "last_refresh_dates", [value.isoformat() for value in dates])
                if historical:
                    set_meta(conn, "history_cursor", historical.isoformat())
                # Bootstrap changes most of the table, so collect deterministic
                # planner statistics before exposing the new version. Normal
                # three-day refreshes use SQLite's bounded maintenance hint.
                if len(dates) > 3:
                    conn.execute("ANALYZE attribution_fact")
                    conn.execute("ANALYZE attribution_filter_daily")
                    conn.execute("ANALYZE attribution_campaign_daily")
                else:
                    conn.execute("PRAGMA optimize")
                conn.commit()
            result = {
                "ok": True,
                "run_id": run_id,
                "data_version": version,
                "generated_at": generated_at,
                "dates": [value.isoformat() for value in dates],
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        except Exception as exc:
            finished_at = iso_now()
            with connect_sqlite(path) as conn:
                conn.execute("DELETE FROM refresh_stage WHERE run_id=?", (run_id,))
                for log_id in log_ids.values():
                    conn.execute(
                        "UPDATE refresh_log SET finished_at=?,status='failed',detail=? WHERE id=? AND status='running'",
                        (finished_at, f"{type(exc).__name__}: {exc}", log_id),
                    )
                conn.commit()
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path")
    parser.add_argument("--env-file")
    parser.add_argument("--date", action="append", help="refresh one YYYY-MM-DD; repeatable")
    parser.add_argument("--bootstrap-start")
    parser.add_argument("--bootstrap-end")
    parser.add_argument("--skip-mount-check", action="store_true", help="local tests only")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        refresh(args)
        return 0
    except Exception as exc:
        print(f"refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

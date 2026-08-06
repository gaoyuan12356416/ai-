#!/usr/bin/env python3
"""Refresh the Dramawave attribution comparison SQLite cache from read-only MySQL."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gc
import itertools
import json
import os
import sys
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
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
    insert_staged_facts,
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


@dataclass
class SourceRead:
    rows: int = 0
    updated_at: str = ""


def source_updated_at(value: Any) -> str:
    return value.updated_at if isinstance(value, SourceRead) else text(value)


def fetch_custom(connection: Any, day: dt.date) -> tuple[Iterable[dict[str, Any]], SourceRead]:
    state = SourceRead()

    def rows() -> Iterable[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute(CUSTOM_SQL, (day.isoformat(),))
            for raw in cursor:
                row = {key: text(raw.get(key)) for key in FACT_DIMENSIONS if key not in BASE_METRICS}
                row.update(
                    {
                        "dt": day.isoformat(),
                        "channel_id": text(raw.get("channel_id")),
                        "channel": CHANNEL_NAMES.get(
                            text(raw.get("channel_id")), f"渠道 {text(raw.get('channel_id'))}"
                        ),
                        "product": "Dramawave",
                        "matched_grain": "none",
                        "mapping_status": "spend_only",
                        "spend": number(raw.get("spend")),
                        **{key: integer(raw.get(key)) for key in CUSTOM_INTEGER_METRICS},
                    }
                )
                state.rows += 1
                state.updated_at = max(state.updated_at, timestamp(raw.get("source_updated_at")))
                yield row

    return rows(), state


def fetch_revenue(connection: Any, table: str, day: dt.date) -> tuple[Iterable[dict[str, Any]], SourceRead]:
    if table not in {D7_TABLE, D30_TABLE}:
        raise RuntimeError("unexpected revenue table")
    state = SourceRead()

    def rows() -> Iterable[dict[str, Any]]:
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
                state.rows += 1
                state.updated_at = max(state.updated_at, timestamp(raw.get("source_updated_at")))
                yield row

    return rows(), state


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
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {"ad": {}, "adset": {}, "campaign": {}}
    for row in custom_rows:
        identity = custom_identity(row)
        if identity not in facts:
            continue
        if row.get("ad_id"):
            add_lookup_candidate(lookup["ad"], text(row["ad_id"]), identity)
        if row.get("adset_id"):
            add_lookup_candidate(lookup["adset"], text(row["adset_id"]), identity)
        if row.get("campaign_id"):
            add_lookup_candidate(lookup["campaign"], text(row["campaign_id"]), identity)
    return lookup


def add_lookup_candidate(target: dict[str, Any], key: str, identity: tuple[str, ...]) -> None:
    current = target.get(key)
    if current is None:
        target[key] = identity
    elif isinstance(current, set):
        current.add(identity)
    elif current != identity:
        target[key] = {current, identity}


def match_candidates(
    row: dict[str, Any], lookup: dict[str, dict[str, Any]]
) -> tuple[str, Any]:
    # Fall back only when the finer identifier is absent. A present-but-missing
    # ad_id must never be silently attributed to another ad in the same ad set.
    for level, key_name in (("ad", "ad_id"), ("adset", "adset_id"), ("campaign", "campaign_id")):
        key = text(row.get(key_name))
        if key:
            candidate = lookup[level].get(key)
            return level, candidate
    return "unmatched", None


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


@dataclass
class MappedDay:
    facts: dict[tuple[str, ...], dict[str, Any]]
    ambiguous_facts: list[dict[str, Any]]
    stats: dict[str, Any]

    def iter_facts(self) -> Iterable[dict[str, Any]]:
        return itertools.chain(self.facts.values(), self.ambiguous_facts)


@dataclass
class CustomDayState:
    facts: dict[tuple[str, ...], dict[str, Any]]
    lookup: dict[str, dict[str, Any]]
    stats: dict[str, Any]


def build_custom_day(day: dt.date, custom_rows: Iterable[dict[str, Any]]) -> CustomDayState:
    custom_metrics = ("spend",) + CUSTOM_INTEGER_METRICS
    source_sums = {metric: 0.0 for metric in custom_metrics}
    facts: dict[tuple[str, ...], dict[str, Any]] = {}
    lookup: dict[str, dict[str, Any]] = {"ad": {}, "adset": {}, "campaign": {}}
    source_rows = 0
    for row in custom_rows:
        source_rows += 1
        for metric in custom_metrics:
            source_sums[metric] += number(row.get(metric, 0))
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
        for metric in CUSTOM_INTEGER_METRICS:
            fact[metric] += integer(row.get(metric))
        for level, key_name in (("ad", "ad_id"), ("adset", "adset_id"), ("campaign", "campaign_id")):
            key = text(row.get(key_name))
            if key:
                add_lookup_candidate(lookup[level], key, identity)
    merged_sums = {
        metric: sum(number(row.get(metric, 0)) for row in facts.values())
        for metric in custom_metrics
    }
    stats: dict[str, Any] = {"custom_source_rows": source_rows, "custom_fact_rows": len(facts)}
    for metric in custom_metrics:
        tolerance = 0.0 if metric in CUSTOM_INTEGER_METRICS else max(0.01, abs(source_sums[metric]) * 1e-9)
        if abs(merged_sums[metric] - source_sums[metric]) > tolerance:
            raise RuntimeError(
                f"custom {metric} source-merge conservation failed: "
                f"{merged_sums[metric]} != {source_sums[metric]}"
            )
        stats[f"custom_source_{metric}"] = source_sums[metric]
        stats[f"custom_merged_{metric}"] = merged_sums[metric]
    return CustomDayState(facts=facts, lookup=lookup, stats=stats)


def _revenue_upsert_sql(prefix: str) -> str:
    if prefix not in {"d7", "d30"}:
        raise RuntimeError(f"unexpected revenue prefix: {prefix}")
    fixed = (
        "run_id", "dt", "created_at", "campaign_id", "campaign_name",
        "adset_id", "adset_name", "ad_id", "ad_name", f"{prefix}_present",
    )
    metric_columns = tuple(f"{prefix}_{metric}" for metric in REVENUE_METRICS)
    columns = fixed + metric_columns
    updates = ["created_at=excluded.created_at"]
    for label in ("campaign_name", "adset_name", "ad_name"):
        updates.append(
            f"{label}=CASE WHEN excluded.{label}<>'' THEN excluded.{label} ELSE {label} END"
        )
    updates.append(f"{prefix}_present=1")
    updates.extend(f"{column}={column}+excluded.{column}" for column in metric_columns)
    return (
        f"INSERT INTO refresh_revenue_stage({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)}) "
        "ON CONFLICT(run_id,dt,campaign_id,adset_id,ad_id) DO UPDATE SET "
        + ",".join(updates)
    )


def stage_revenue_source(
    conn: Any,
    run_id: str,
    day: dt.date,
    prefix: str,
    rows: Iterable[dict[str, Any]],
    *,
    batch_size: int = 1000,
) -> dict[str, Any]:
    sql = _revenue_upsert_sql(prefix)
    staged_at = iso_now()
    count = 0
    sums = {metric: 0.0 for metric in REVENUE_METRICS}
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        count += 1
        values = []
        for metric in REVENUE_METRICS:
            value = (
                integer(row.get(metric, 0))
                if metric in REVENUE_INTEGER_METRICS
                else number(row.get(metric, 0))
            )
            sums[metric] += number(value)
            values.append(value)
        batch.append(
            (
                run_id,
                day.isoformat(),
                staged_at,
                text(row.get("campaign_id")),
                text(row.get("campaign_name")),
                text(row.get("adset_id")),
                text(row.get("adset_name")),
                text(row.get("ad_id")),
                text(row.get("ad_name")),
                1,
                *values,
            )
        )
        if len(batch) >= batch_size:
            conn.executemany(sql, batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
    return {"rows": count, "sums": sums}


def _candidate_identities(candidate: Any) -> tuple[tuple[str, ...], ...]:
    if candidate is None:
        return ()
    if isinstance(candidate, set):
        return tuple(sorted(candidate))
    return (candidate,)


def map_staged_revenue(
    day: dt.date,
    custom: CustomDayState,
    revenue_rows: Iterable[dict[str, Any]],
    source_stats: dict[str, dict[str, Any]],
    merged_stats: dict[str, Any],
) -> MappedDay:
    facts = custom.facts
    ambiguous_facts: list[dict[str, Any]] = []
    stats = dict(custom.stats)
    stats.update(
        {
            "revenue_union_rows": int(merged_stats["revenue_union_rows"]),
            "excluded_unmatched_rows": 0,
            "excluded_unmatched_d7_rows": 0,
            "excluded_unmatched_d30_rows": 0,
            "d7_source_rows": int(source_stats["d7"]["rows"]),
            "d30_source_rows": int(source_stats["d30"]["rows"]),
        }
    )
    excluded_sums = {
        prefix: {metric: 0.0 for metric in REVENUE_METRICS} for prefix in ("d7", "d30")
    }
    candidate_sums = {
        prefix: {metric: 0.0 for metric in REVENUE_METRICS} for prefix in ("d7", "d30")
    }
    for prefix in ("d7", "d30"):
        stats[f"{prefix}_merged_keys"] = int(merged_stats[f"{prefix}_merged_keys"])
        for metric in REVENUE_METRICS:
            expected = number(source_stats[prefix]["sums"][metric])
            actual = number(merged_stats[f"{prefix}_{metric}"])
            tolerance = 0.0 if metric in REVENUE_INTEGER_METRICS else max(0.01, abs(expected) * 1e-9)
            if abs(actual - expected) > tolerance:
                raise RuntimeError(
                    f"{prefix} {metric} source-merge conservation failed: {actual} != {expected}"
                )
    for revenue in revenue_rows:
        level, candidate = match_candidates(revenue, custom.lookup)
        candidate_ids = _candidate_identities(candidate)
        if not candidate_ids:
            stats["excluded_unmatched_rows"] += 1
            for prefix in ("d7", "d30"):
                if revenue.get(f"{prefix}_present"):
                    stats[f"excluded_unmatched_{prefix}_rows"] += 1
                    for metric in REVENUE_METRICS:
                        excluded_sums[prefix][metric] += number(revenue.get(f"{prefix}_{metric}", 0))
            continue
        candidates = [facts[identity] for identity in candidate_ids]
        mapped = len(candidates) == 1
        target = candidates[0] if mapped else common_candidate_fact(day, candidates, revenue, level)
        if not mapped:
            ambiguous_facts.append(target)
        else:
            for label in ("campaign_name", "adset_name"):
                if revenue.get(label):
                    target[label] = text(revenue[label])
            target["ad_name"] = ""
            if target["matched_grain"] in {"none", level}:
                target["matched_grain"] = level
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
    custom_metrics = ("spend",) + CUSTOM_INTEGER_METRICS
    for metric in custom_metrics:
        actual = sum(
            number(row.get(metric, 0))
            for row in itertools.chain(facts.values(), ambiguous_facts)
        )
        expected = number(custom.stats[f"custom_source_{metric}"])
        tolerance = 0.0 if metric in CUSTOM_INTEGER_METRICS else max(0.01, abs(expected) * 1e-9)
        if abs(actual - expected) > tolerance:
            raise RuntimeError(f"custom {metric} output conservation failed: {actual} != {expected}")
        stats[f"custom_fact_{metric}"] = actual
    for prefix in ("d7", "d30"):
        for metric in REVENUE_METRICS:
            actual = sum(
                number(row.get(f"{prefix}_{metric}", 0))
                for row in itertools.chain(facts.values(), ambiguous_facts)
            )
            expected = candidate_sums[prefix][metric]
            excluded = excluded_sums[prefix][metric]
            merged = number(merged_stats[f"{prefix}_{metric}"])
            tolerance = 0.0 if metric in REVENUE_INTEGER_METRICS else max(0.01, abs(expected) * 1e-9)
            if abs(actual - expected) > tolerance:
                raise RuntimeError(f"{prefix} {metric} conservation failed: {actual} != {expected}")
            if abs(merged - expected - excluded) > tolerance:
                raise RuntimeError(
                    f"{prefix} {metric} merge-scope conservation failed: "
                    f"merged={merged} candidate={expected} excluded={excluded}"
                )
            stats[f"{prefix}_source_{metric}"] = number(source_stats[prefix]["sums"][metric])
            stats[f"{prefix}_merged_{metric}"] = merged
            stats[f"{prefix}_candidate_{metric}"] = expected
            stats[f"{prefix}_excluded_unscoped_{metric}"] = excluded
            stats[f"{prefix}_fact_{metric}"] = actual
        for suffix in ("candidate_keys", "mapped_keys", "ambiguous_keys"):
            stats[f"{prefix}_{suffix}"] = sum(
                integer(row.get(f"{prefix}_{suffix}", 0))
                for row in itertools.chain(facts.values(), ambiguous_facts)
            )
    stats["fact_rows"] = len(facts) + len(ambiguous_facts)
    return MappedDay(facts=facts, ambiguous_facts=ambiguous_facts, stats=stats)


def revenue_stage_stats(conn: Any, run_id: str, day: dt.date) -> dict[str, Any]:
    fields = ["COUNT(*) AS revenue_union_rows"]
    for prefix in ("d7", "d30"):
        fields.append(f"SUM(CASE WHEN {prefix}_present=1 THEN 1 ELSE 0 END) AS {prefix}_merged_keys")
        fields.extend(
            f"COALESCE(SUM({prefix}_{metric}),0) AS {prefix}_{metric}"
            for metric in REVENUE_METRICS
        )
    row = conn.execute(
        f"SELECT {','.join(fields)} FROM refresh_revenue_stage WHERE run_id=? AND dt=?",
        (run_id, day.isoformat()),
    ).fetchone()
    return dict(row)


def iter_staged_revenue(conn: Any, run_id: str, day: dt.date) -> Iterable[dict[str, Any]]:
    columns = (
        "campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name",
        "d7_present", "d30_present",
    ) + tuple(f"{prefix}_{metric}" for prefix in ("d7", "d30") for metric in REVENUE_METRICS)
    cursor = conn.execute(
        f"SELECT {','.join(columns)} FROM refresh_revenue_stage WHERE run_id=? AND dt=?",
        (run_id, day.isoformat()),
    )
    for row in cursor:
        yield dict(row)


def map_day_state(
    day: dt.date,
    custom_rows: Iterable[dict[str, Any]],
    old_rows: Iterable[dict[str, Any]],
    new_rows: Iterable[dict[str, Any]],
) -> MappedDay:
    """Disk-backed compatibility entry point used by focused mapping tests."""
    custom = build_custom_day(day, custom_rows)
    with tempfile.TemporaryDirectory(prefix="dramawave-map-") as tempdir:
        path = Path(tempdir) / "mapping.sqlite3"
        with connect_sqlite(path) as conn:
            d7 = stage_revenue_source(conn, "mapping", day, "d7", old_rows)
            d30 = stage_revenue_source(conn, "mapping", day, "d30", new_rows)
            return map_staged_revenue(
                day,
                custom,
                iter_staged_revenue(conn, "mapping", day),
                {"d7": d7, "d30": d30},
                revenue_stage_stats(conn, "mapping", day),
            )


def map_day(
    day: dt.date,
    custom_rows: Iterable[dict[str, Any]],
    old_rows: Iterable[dict[str, Any]],
    new_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compatibility helper for focused mapping tests; production stages rows directly."""
    mapped = map_day_state(day, custom_rows, old_rows, new_rows)
    return list(mapped.iter_facts()), mapped.stats


def additive_totals(conn: Any, table: str, day: str) -> dict[str, float]:
    if table not in {"attribution_fact", *ROLLUP_TABLE_DIMENSIONS}:
        raise RuntimeError(f"unexpected conservation table: {table}")
    selected = ",".join(f"COALESCE(SUM({metric}),0) AS {metric}" for metric in BASE_METRICS)
    row = conn.execute(f"SELECT {selected} FROM {table} WHERE dt=?", (day,)).fetchone()
    return {metric: number(row[metric]) for metric in BASE_METRICS}


def expected_staged_metric(stats: dict[str, Any], metric: str) -> float:
    if metric in {"spend", *CUSTOM_INTEGER_METRICS}:
        return number(stats[f"custom_fact_{metric}"])
    for prefix in ("d7", "d30"):
        marker = f"{prefix}_"
        if metric.startswith(marker):
            suffix = metric[len(marker):]
            if suffix in REVENUE_METRICS:
                return number(stats[f"{prefix}_fact_{suffix}"])
            if suffix in {"candidate_keys", "mapped_keys", "ambiguous_keys"}:
                return number(stats[f"{prefix}_{suffix}"])
    raise RuntimeError(f"no staged conservation statistic for {metric}")


def validate_staged_facts(
    conn: Any, run_id: str, day: Any, stats: dict[str, Any]
) -> int:
    day_text = day.isoformat() if isinstance(day, dt.date) else str(day)
    selected = ["COUNT(*) AS fact_rows"] + [
        f"COALESCE(SUM({metric}),0) AS {metric}" for metric in BASE_METRICS
    ]
    row = conn.execute(
        f"SELECT {','.join(selected)} FROM refresh_fact_stage WHERE run_id=? AND dt=?",
        (run_id, day_text),
    ).fetchone()
    count = int(row["fact_rows"])
    if count != int(stats["fact_rows"]):
        raise RuntimeError(f"missing staged facts for {day_text}: {count} != {stats['fact_rows']}")
    for metric in BASE_METRICS:
        actual = number(row[metric])
        expected = expected_staged_metric(stats, metric)
        tolerance = max(0.01, abs(expected) * 1e-9) if metric in FLOAT_BASE_METRICS else 0.0
        if abs(actual - expected) > tolerance:
            raise RuntimeError(
                f"staged {day_text} {metric} conservation failed: {actual} != {expected}"
            )
    return count


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
            # The process lock proves no earlier run is still live. Staging is
            # intentionally non-resumable, so remove every abandoned run
            # together rather than leaving a recent crash orphan for two days.
            conn.execute("DELETE FROM refresh_stage WHERE run_id<>?", (run_id,))
            conn.execute("DELETE FROM refresh_fact_stage WHERE run_id<>?", (run_id,))
            conn.execute("DELETE FROM refresh_revenue_stage WHERE run_id<>?", (run_id,))
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
                        custom_rows, custom_read = fetch_custom(source, day)
                        custom = build_custom_day(day, custom_rows)
                        old_rows, old_read = fetch_revenue(source, D7_TABLE, day)
                        with connect_sqlite(path) as revenue_conn:
                            revenue_conn.execute(
                                "DELETE FROM refresh_revenue_stage WHERE run_id=? AND dt=?",
                                (run_id, day.isoformat()),
                            )
                            revenue_conn.commit()
                            d7_source_stats = stage_revenue_source(
                                revenue_conn, run_id, day, "d7", old_rows
                            )
                        new_rows, new_read = fetch_revenue(source, D30_TABLE, day)
                        with connect_sqlite(path) as revenue_conn:
                            d30_source_stats = stage_revenue_source(
                                revenue_conn, run_id, day, "d30", new_rows
                            )
                    if not int(custom.stats["custom_source_rows"]):
                        raise RuntimeError(f"refusing empty custom source for {day.isoformat()}")
                    if not int(d7_source_stats["rows"]) or not int(d30_source_stats["rows"]):
                        raise RuntimeError(
                            f"refusing empty revenue source for {day.isoformat()}: "
                            f"d7={d7_source_stats['rows']} d30={d30_source_stats['rows']}"
                        )
                    with connect_sqlite(path) as conn:
                        merged_stats = revenue_stage_stats(conn, run_id, day)
                        mapped = map_staged_revenue(
                            day,
                            custom,
                            iter_staged_revenue(conn, run_id, day),
                            {"d7": d7_source_stats, "d30": d30_source_stats},
                            merged_stats,
                        )
                        stats = mapped.stats
                        existing = conn.execute(
                            "SELECT COUNT(*) fact_rows,SUM(d7_candidate_keys) d7_keys,SUM(d30_candidate_keys) d30_keys "
                            "FROM attribution_fact WHERE dt=?",
                            (day.isoformat(),),
                        ).fetchone()
                        if int(existing["fact_rows"] or 0) and not int(stats["fact_rows"]):
                            raise RuntimeError(
                                f"refusing to replace non-empty cache with empty facts for {day.isoformat()}"
                            )
                        if int(existing["d7_keys"] or 0) and not int(stats["d7_candidate_keys"]):
                            raise RuntimeError(
                                f"refusing zero D7 candidates over prior non-zero cache for {day.isoformat()}"
                            )
                        if int(existing["d30_keys"] or 0) and not int(stats["d30_candidate_keys"]):
                            raise RuntimeError(
                                f"refusing zero D30 candidates over prior non-zero cache for {day.isoformat()}"
                            )
                        source_times = {
                            "ads_custom_source_insight": source_updated_at(custom_read),
                            "app_revenues": source_updated_at(old_read),
                            "app_revenues_30d": source_updated_at(new_read),
                        }
                        payload = json.dumps(
                            {"stats": stats, "source_times": source_times},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        staged_at = iso_now()
                        conn.execute(
                            "DELETE FROM refresh_fact_stage WHERE run_id=? AND dt=?",
                            (run_id, day.isoformat()),
                        )
                        staged_count = insert_staged_facts(
                            conn, run_id, staged_at, mapped.iter_facts()
                        )
                        if staged_count != int(stats["fact_rows"]):
                            raise RuntimeError(
                                f"staged fact count mismatch for {day.isoformat()}: "
                                f"{staged_count} != {stats['fact_rows']}"
                            )
                        conn.execute(
                            "INSERT OR REPLACE INTO refresh_stage(run_id,dt,created_at,payload) VALUES(?,?,?,?)",
                            (run_id, day.isoformat(), staged_at, payload),
                        )
                        conn.execute(
                            "DELETE FROM refresh_revenue_stage WHERE run_id=? AND dt=?",
                            (run_id, day.isoformat()),
                        )
                        conn.commit()
                    print(f"staged {day.isoformat()}: {stats['fact_rows']} facts", flush=True)
                    # The next day can be larger than the previous one. Drop
                    # all row graphs before starting its three source reads so
                    # their peaks never overlap on the small production host.
                    del custom_rows, old_rows, new_rows, custom, mapped, stats, payload
                    gc.collect()
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
                    staged = json.loads(stage_row["payload"])
                    validate_staged_facts(conn, run_id, day, staged["stats"])
                    conn.execute("DELETE FROM attribution_fact WHERE dt=?", (day.isoformat(),))
                    fact_columns = FACT_DIMENSIONS + BASE_METRICS
                    conn.execute(
                        f"INSERT INTO attribution_fact({','.join(fact_columns)}) "
                        f"SELECT {','.join(fact_columns)} FROM refresh_fact_stage "
                        "WHERE run_id=? AND dt=?",
                        (run_id, day.isoformat()),
                    )
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
                conn.execute("DELETE FROM refresh_fact_stage WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM refresh_revenue_stage WHERE run_id=?", (run_id,))
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
                conn.execute("DELETE FROM refresh_fact_stage WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM refresh_revenue_stage WHERE run_id=?", (run_id,))
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

#!/usr/bin/env python3
"""Read-only aggregate HTTP API for the Dramawave D7/D30 dashboard."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from common import (
    BASE_METRICS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DIMENSION_COLUMNS,
    FILTER_COLUMNS,
    MIN_DATE,
    RETENTION_DAYS,
    connect_sqlite,
    db_path,
    get_meta,
    parse_date,
    retention_start,
    verify_data_disk,
)


ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.html"
MAX_PAGE_SIZE = 500
MAX_EXPORT_ROWS = 200_000
RANKING_DIMENSIONS = ("campaign", "adset", "optimizer", "country_group")
RESPONSE_CACHE_SIZE = 128
_RESPONSE_CACHE: "OrderedDict[tuple[Any, ...], dict[str, Any]]" = OrderedDict()
_RESPONSE_CACHE_LOCK = threading.RLock()
_RANKINGS_INFLIGHT: dict[tuple[Any, ...], threading.Event] = {}
SORTABLE_METRICS = {
    "spend",
    "impressions",
    "clicks",
    "installs",
    "af_installs",
    "d7_revenue",
    "d30_revenue",
    "d7_roas",
    "d30_roas",
    "revenue_diff",
    "lift_rate",
}
FACT_TABLE = "attribution_fact"
FILTER_ROLLUP_TABLE = "attribution_filter_daily"
CAMPAIGN_ROLLUP_TABLE = "attribution_campaign_daily"
QUERY_TABLES = frozenset({FACT_TABLE, FILTER_ROLLUP_TABLE, CAMPAIGN_ROLLUP_TABLE})


class RequestError(ValueError):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


def response_cache_key(namespace: str, version: str, params: dict[str, list[str]]) -> tuple[Any, ...]:
    # Keep repeated-value order: first() deliberately uses the final occurrence
    # for scalar parameters, so reversing duplicates can change semantics.
    normalized = tuple(sorted((key, tuple(str(value) for value in values)) for key, values in params.items()))
    return (namespace, version, normalized)


def response_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _RESPONSE_CACHE_LOCK:
        value = _RESPONSE_CACHE.get(key)
        if value is not None:
            _RESPONSE_CACHE.move_to_end(key)
        return value


def response_cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    with _RESPONSE_CACHE_LOCK:
        _RESPONSE_CACHE[key] = value
        _RESPONSE_CACHE.move_to_end(key)
        while len(_RESPONSE_CACHE) > RESPONSE_CACHE_SIZE:
            _RESPONSE_CACHE.popitem(last=False)


def clear_response_cache() -> None:
    with _RESPONSE_CACHE_LOCK:
        _RESPONSE_CACHE.clear()


def ranking_semantic_params(params: dict[str, list[str]]) -> dict[str, list[str]]:
    names = {
        "start_date",
        "start",
        "end_date",
        "end",
        "data_version",
        "metric_basis",
        "basis",
        "campaign",
        "campaign_q",
        "adset",
        "adset_q",
        *FILTER_COLUMNS,
    }
    return {key: list(values) for key, values in params.items() if key in names}


def validate_query_table(table: str) -> str:
    if table not in QUERY_TABLES:
        raise RequestError(f"unsupported query table: {table}")
    return table


def rollups_current(conn: sqlite3.Connection) -> bool:
    metadata = get_meta(conn)
    data_version = str(metadata.get("data_version") or "")
    return bool(data_version) and str(metadata.get("rollup_version") or "") == data_version


def has_campaign_search(params: dict[str, list[str]]) -> bool:
    return bool(first(params, "campaign", "campaign_q").strip())


def has_adset_search(params: dict[str, list[str]]) -> bool:
    return bool(first(params, "adset", "adset_q").strip())


def business_filters_applied(params: dict[str, list[str]]) -> bool:
    if has_campaign_search(params) or has_adset_search(params):
        return True
    return any(any(value != "" for value in params.get(parameter, [])) for parameter in FILTER_COLUMNS)


def detail_table_for(
    conn: sqlite3.Connection, params: dict[str, list[str]], dimensions: list[str]
) -> str:
    if not rollups_current(conn):
        return FACT_TABLE
    if has_adset_search(params) or "adset" in dimensions:
        return FACT_TABLE
    if has_campaign_search(params) or "campaign" in dimensions:
        return CAMPAIGN_ROLLUP_TABLE
    return FILTER_ROLLUP_TABLE


def context_table_for(conn: sqlite3.Connection, params: dict[str, list[str]]) -> str:
    if not rollups_current(conn):
        return FACT_TABLE
    if has_adset_search(params):
        return FACT_TABLE
    if has_campaign_search(params):
        return CAMPAIGN_ROLLUP_TABLE
    return FILTER_ROLLUP_TABLE


def ranking_table_for(conn: sqlite3.Connection, params: dict[str, list[str]], dimension: str) -> str:
    if dimension not in RANKING_DIMENSIONS:
        raise RequestError(f"unsupported ranking dimension: {dimension}")
    if not rollups_current(conn):
        return FACT_TABLE
    if dimension == "adset" or has_adset_search(params):
        return FACT_TABLE
    if dimension == "campaign":
        return CAMPAIGN_ROLLUP_TABLE
    return context_table_for(conn, params)


def first(params: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        values = params.get(name)
        if values:
            return values[-1]
    return default


def clamp_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_dimensions(params: dict[str, list[str]]) -> list[str]:
    raw = first(params, "dimensions", "group_by", default="dt,campaign")
    result: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        aliases = {
            "date": "dt",
            "app_id": "delivery_product",
            "optimizer_id": "optimizer",
            "campaign_id": "campaign",
            "adset_id": "adset",
            "ad_account_id": "account",
            "mapping_level": "matched_grain",
        }
        key = aliases.get(key, key)
        if key not in DIMENSION_COLUMNS:
            raise RequestError(f"unsupported dimension: {key}")
        if key not in result:
            result.append(key)
    if not result:
        raise RequestError("at least one dimension is required")
    return result


def metric_basis(params: dict[str, list[str]]) -> str:
    value = first(params, "metric_basis", "basis", default="d0").lower()
    if value not in {"d0", "d7"}:
        raise RequestError("metric_basis must be d0 or d7")
    return value


def cache_range(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute("SELECT MIN(dt) AS start_date, MAX(dt) AS end_date FROM attribution_fact").fetchone()
    return (row["start_date"] or "", row["end_date"] or "")


def cache_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    actual = {str(row["dt"]) for row in conn.execute("SELECT DISTINCT dt FROM attribution_fact ORDER BY dt")}
    if not actual:
        return {
            "start_date": "",
            "end_date": "",
            "missing_dates": [],
            "complete": False,
            "current_date_present": False,
        }
    start_date, end_date = min(actual), max(actual)
    expected_start = retention_start().isoformat()
    cursor = dt.date.fromisoformat(expected_start)
    end = dt.date.fromisoformat(end_date)
    missing: list[str] = []
    while cursor <= end:
        value = cursor.isoformat()
        if value not in actual:
            missing.append(value)
        cursor += dt.timedelta(days=1)
    complete = start_date <= expected_start and not missing
    return {
        "start_date": start_date,
        "end_date": end_date,
        "expected_start_date": expected_start,
        "missing_dates": missing,
        "complete": complete,
        "current_date_present": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat() in actual,
    }


def parse_range(conn: sqlite3.Connection, params: dict[str, list[str]]) -> tuple[str, str]:
    available_start, available_end = cache_range(conn)
    if not available_start or not available_end:
        raise RequestError("cache is empty", HTTPStatus.SERVICE_UNAVAILABLE)
    start_text = first(params, "start_date", "start", default=available_start)
    end_text = first(params, "end_date", "end", default=available_end)
    try:
        start = parse_date(start_text, name="start_date")
        end = parse_date(end_text, name="end_date")
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    if start > end:
        raise RequestError("start_date cannot be later than end_date")
    if start_text < available_start or end_text > available_end:
        raise RequestError(f"requested range must be within {available_start}..{available_end}")
    available = {
        str(row["dt"])
        for row in conn.execute(
            "SELECT DISTINCT dt FROM attribution_fact WHERE dt>=? AND dt<=?",
            (start_text, end_text),
        )
    }
    cursor = start
    missing: list[str] = []
    while cursor <= end:
        value = cursor.isoformat()
        if value not in available:
            missing.append(value)
        cursor += dt.timedelta(days=1)
    if missing:
        raise RequestError("cache is missing requested dates: " + ",".join(missing), HTTPStatus.SERVICE_UNAVAILABLE)
    return start_text, end_text


def build_where(params: dict[str, list[str]], start_date: str, end_date: str) -> tuple[str, list[Any]]:
    clauses = ["dt >= ?", "dt <= ?"]
    values: list[Any] = [start_date, end_date]
    for parameter, column in FILTER_COLUMNS.items():
        requested = [item for item in params.get(parameter, []) if item != ""]
        if not requested:
            continue
        placeholders = ",".join("?" for _ in requested)
        clauses.append(f"{column} IN ({placeholders})")
        values.extend(requested)
    campaign = first(params, "campaign", "campaign_q").strip()
    if campaign:
        needle = f"%{escape_like(campaign)}%"
        clauses.append("(campaign_id LIKE ? ESCAPE '\\' OR campaign_name LIKE ? ESCAPE '\\')")
        values.extend([needle, needle])
    adset = first(params, "adset", "adset_q").strip()
    if adset:
        needle = f"%{escape_like(adset)}%"
        clauses.append("(adset_id LIKE ? ESCAPE '\\' OR adset_name LIKE ? ESCAPE '\\')")
        values.extend([needle, needle])
    return " AND ".join(clauses), values


def dimension_select(dimensions: list[str]) -> tuple[list[str], list[str]]:
    select: list[str] = []
    group: list[str] = []
    for key in dimensions:
        value_column, label_column = DIMENSION_COLUMNS[key]
        select.append(f"{value_column} AS {key}")
        group.append(value_column)
        if label_column != value_column:
            select.append(f"MAX({label_column}) AS {key}_name")
    return select, group


def metric_select(basis: str) -> list[str]:
    suffix = "d0" if basis == "d0" else "d7"
    old_revenue = f"SUM(d7_revenue_iaa_{suffix}) + SUM(d7_revenue_iap_{suffix})"
    new_revenue = f"SUM(d30_revenue_iaa_{suffix}) + SUM(d30_revenue_iap_{suffix})"
    fields = [f"SUM({column}) AS {column}" for column in ("spend", "impressions", "clicks", "installs", "af_installs")]
    fields.extend(
        [
            f"({old_revenue}) AS d7_revenue",
            f"({new_revenue}) AS d30_revenue",
            f"CASE WHEN SUM(spend) != 0 THEN ({old_revenue}) / SUM(spend) END AS d7_roas",
            f"CASE WHEN SUM(spend) != 0 THEN ({new_revenue}) / SUM(spend) END AS d30_roas",
            f"(({new_revenue}) - ({old_revenue})) AS revenue_diff",
            f"CASE WHEN ({old_revenue}) != 0 THEN (({new_revenue}) - ({old_revenue})) / ({old_revenue}) END AS lift_rate",
            "SUM(d7_users) AS d7_users",
            f"SUM(d7_purchase_{suffix}) AS d7_purchases",
            "SUM(d30_users) AS d30_users",
            f"SUM(d30_purchase_{suffix}) AS d30_purchases",
        ]
    )
    return fields


def rows_to_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def aggregate_rows(
    conn: sqlite3.Connection,
    *,
    table: str = FACT_TABLE,
    where_sql: str,
    where_values: list[Any],
    dimensions: list[str],
    basis: str,
    sort_by: str = "spend",
    sort_dir: str = "desc",
    limit: int | None = None,
    offset: int = 0,
    include_group_total: bool = False,
) -> list[dict[str, Any]]:
    table = validate_query_table(table)
    dims, groups = dimension_select(dimensions)
    selected = dims + metric_select(basis)
    if include_group_total:
        selected.append("COUNT(*) OVER() AS __group_total")
    if sort_by not in SORTABLE_METRICS and sort_by not in dimensions:
        raise RequestError(f"unsupported sort_by: {sort_by}")
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
    sql = f"SELECT {','.join(selected)} FROM {table} WHERE {where_sql}"
    if groups:
        sql += " GROUP BY " + ",".join(groups)
    sql += f" ORDER BY {sort_by} {direction}"
    values = list(where_values)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        values.extend([limit, offset])
    return rows_to_dict(conn.execute(sql, values).fetchall())


def aggregate_total(
    conn: sqlite3.Connection,
    where_sql: str,
    values: list[Any],
    basis: str,
    *,
    table: str = FACT_TABLE,
) -> dict[str, Any]:
    table = validate_query_table(table)
    sql = f"SELECT {','.join(metric_select(basis))} FROM {table} WHERE {where_sql}"
    return dict(conn.execute(sql, values).fetchone())


def grouped_count(
    conn: sqlite3.Connection,
    where_sql: str,
    values: list[Any],
    dimensions: list[str],
    *,
    table: str = FACT_TABLE,
) -> int:
    table = validate_query_table(table)
    _, groups = dimension_select(dimensions)
    sql = f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM {table} WHERE {where_sql} GROUP BY {','.join(groups)})"
    return int(conn.execute(sql, values).fetchone()["n"])


def quality_select(basis: str) -> list[str]:
    suffix = "d0" if basis == "d0" else "d7"
    return [
        "SUM(d7_candidate_keys) AS d7_total_rows",
        "SUM(d7_mapped_keys) AS d7_mapped_rows",
        "SUM(d7_ambiguous_keys) AS d7_ambiguous_rows",
        "SUM(d30_candidate_keys) AS d30_total_rows",
        "SUM(d30_mapped_keys) AS d30_mapped_rows",
        "SUM(d30_ambiguous_keys) AS d30_ambiguous_rows",
        f"SUM(d7_revenue_iaa_{suffix}+d7_revenue_iap_{suffix}) AS d7_total_revenue",
        f"SUM(CASE WHEN mapping_status='mapped' THEN d7_revenue_iaa_{suffix}+d7_revenue_iap_{suffix} ELSE 0 END) AS d7_mapped_revenue",
        f"SUM(d30_revenue_iaa_{suffix}+d30_revenue_iap_{suffix}) AS d30_total_revenue",
        f"SUM(CASE WHEN mapping_status='mapped' THEN d30_revenue_iaa_{suffix}+d30_revenue_iap_{suffix} ELSE 0 END) AS d30_mapped_revenue",
    ]


def finalize_quality(item: dict[str, Any]) -> dict[str, Any]:
    for prefix in ("d7", "d30"):
        total = int(item.get(f"{prefix}_total_rows") or 0)
        mapped = int(item.get(f"{prefix}_mapped_rows") or 0)
        ambiguous = int(item.get(f"{prefix}_ambiguous_rows") or 0)
        total_revenue = float(item.get(f"{prefix}_total_revenue") or 0)
        mapped_revenue = float(item.get(f"{prefix}_mapped_revenue") or 0)
        item[f"{prefix}_unmapped_rows"] = max(0, total - mapped - ambiguous)
        item[f"{prefix}_coverage_ratio"] = mapped / total if total else None
        item[f"{prefix}_revenue_coverage_ratio"] = mapped_revenue / total_revenue if total_revenue else None
    # Backwards-compatible combined fields used by older report clients.
    item["total_revenue_rows"] = max(int(item.get("d7_total_rows") or 0), int(item.get("d30_total_rows") or 0))
    item["mapped_revenue_rows"] = max(int(item.get("d7_mapped_rows") or 0), int(item.get("d30_mapped_rows") or 0))
    item["ambiguous_revenue_rows"] = max(int(item.get("d7_ambiguous_rows") or 0), int(item.get("d30_ambiguous_rows") or 0))
    item["unmapped_revenue_rows"] = max(int(item.get("d7_unmapped_rows") or 0), int(item.get("d30_unmapped_rows") or 0))
    item["mapped_ratio"] = min(
        (ratio for ratio in (item.get("d7_coverage_ratio"), item.get("d30_coverage_ratio")) if ratio is not None),
        default=None,
    )
    item["denominator_scope"] = "revenue keys with at least one Dramawave custom-source candidate"
    item["unscoped_source_rows_excluded"] = True
    return item


def source_scope_exclusions(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    basis: str,
    *,
    filtered: bool,
) -> dict[str, Any]:
    suffix = "d0" if basis == "d0" else "d7"
    result: dict[str, Any] = {
        "scope": "date_range_global_not_filter_attributable",
        "business_filters_applied": bool(filtered),
        "d7_rows": 0,
        "d30_rows": 0,
        "d7_revenue": 0.0,
        "d30_revenue": 0.0,
    }
    rows = conn.execute(
        "SELECT r.detail FROM refresh_log r "
        "JOIN (SELECT dt,MAX(id) AS id FROM refresh_log "
        "WHERE status='success' AND dt>=? AND dt<=? GROUP BY dt) latest ON latest.id=r.id "
        "ORDER BY r.dt",
        (start_date, end_date),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(str(row["detail"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(detail, dict):
            continue
        for prefix in ("d7", "d30"):
            try:
                result[f"{prefix}_rows"] += int(detail.get(f"excluded_unmatched_{prefix}_rows") or 0)
            except (TypeError, ValueError):
                pass
            for revenue_type in ("iaa", "iap"):
                try:
                    result[f"{prefix}_revenue"] += float(
                        detail.get(f"{prefix}_excluded_unscoped_revenue_{revenue_type}_{suffix}") or 0
                    )
                except (TypeError, ValueError):
                    pass
    return result


def aggregate_total_and_quality(
    conn: sqlite3.Connection,
    where_sql: str,
    values: list[Any],
    basis: str,
    *,
    table: str = FACT_TABLE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    table = validate_query_table(table)
    metric_fields = metric_select(basis)
    sql = f"SELECT {','.join(metric_fields + quality_select(basis))} FROM {table} WHERE {where_sql}"
    combined = dict(conn.execute(sql, values).fetchone())
    quality_keys = {
        "d7_total_rows",
        "d7_mapped_rows",
        "d7_ambiguous_rows",
        "d30_total_rows",
        "d30_mapped_rows",
        "d30_ambiguous_rows",
        "d7_total_revenue",
        "d7_mapped_revenue",
        "d30_total_revenue",
        "d30_mapped_revenue",
    }
    totals = {key: value for key, value in combined.items() if key not in quality_keys}
    quality_item = {key: combined.get(key) for key in quality_keys}
    return totals, finalize_quality(quality_item)


def quality(
    conn: sqlite3.Connection,
    where_sql: str,
    values: list[Any],
    basis: str,
    *,
    table: str = FACT_TABLE,
) -> dict[str, Any]:
    return aggregate_total_and_quality(conn, where_sql, values, basis, table=table)[1]


def meta_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    metadata = get_meta(conn)
    rollup_ready = bool(metadata.get("data_version")) and (
        str(metadata.get("rollup_version") or "") == str(metadata.get("data_version") or "")
    )
    coverage = cache_coverage(conn)
    start_date, end_date = coverage["start_date"], coverage["end_date"]
    generated_at = metadata.get("generated_at") or metadata.get("data_version_generated_at") or ""
    stale = True
    if generated_at:
        try:
            parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            stale = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc) > timedelta(minutes=45)
        except ValueError:
            stale = True
    source_times = metadata.get("source_max_updated_at") or {
        "ads_custom_source_insight": metadata.get("source_custom_updated_at", ""),
        "app_revenues": metadata.get("source_d7_updated_at", ""),
        "app_revenues_30d": metadata.get("source_d30_updated_at", ""),
    }
    return {
        "data_version": metadata.get("data_version", ""),
        "generated_at": generated_at,
        "source_max_updated_at": source_times,
        "cache": {
            "start_date": start_date,
            "end_date": end_date,
            "retention_days": RETENTION_DAYS,
            "refresh_interval_minutes": 30,
            "stale": stale,
            "complete": coverage["complete"],
            "range_complete": coverage["complete"],
            "current_date_present": coverage["current_date_present"],
            "expected_start_date": coverage.get("expected_start_date", MIN_DATE.isoformat()),
            "missing_dates": coverage["missing_dates"],
            "rollups_current": rollup_ready,
        },
        "defaults": {
            "start_date": max(start_date, end_date and (datetime.fromisoformat(end_date).date() - timedelta(days=6)).isoformat()) if start_date else "",
            "end_date": end_date,
            "basis": "d0",
            "dimensions": ["dt", "campaign"],
        },
        "dimensions": list(DIMENSION_COLUMNS),
        "metric_bases": ["d0", "d7"],
        "minimum_date": MIN_DATE.isoformat(),
        "source_tables": {
            "old_attribution": "kunlunads_dev.ads_app_revenues",
            "new_attribution": "kunlunads_dev.ads_app_revenues_30d",
            "delivery": "kunlunads_dev.ads_custom_source_insight",
        },
    }


def check_version(conn: sqlite3.Connection, params: dict[str, list[str]]) -> str:
    current = str(get_meta(conn).get("data_version") or "")
    requested = first(params, "data_version")
    if requested and requested != current:
        raise RequestError("data_version changed; reload from page 1", HTTPStatus.CONFLICT)
    return current


def options_payload(conn: sqlite3.Connection, params: dict[str, list[str]]) -> dict[str, Any]:
    version = check_version(conn, params)
    cache_key = response_cache_key("options", version, params)
    cached = response_cache_get(cache_key)
    if cached is not None:
        return cached
    start_date, end_date = parse_range(conn, params)
    table = FILTER_ROLLUP_TABLE if rollups_current(conn) else FACT_TABLE
    table = validate_query_table(table)
    definitions = {
        "channel": ("channel_id", "channel"),
        "app_id": ("app_id", "app_id"),
        "optimizer_id": ("optimizer_id", "optimizer_name"),
        "country_group": ("country_group", "country_group"),
        "ad_account_id": ("ad_account_id", "ad_account_id"),
    }
    result: dict[str, list[dict[str, str]]] = {}
    for key, (value_column, label_column) in definitions.items():
        rows = conn.execute(
            f"SELECT {value_column} value, MAX({label_column}) label FROM {table} "
            f"WHERE dt>=? AND dt<=? AND {value_column}!='' GROUP BY {value_column} ORDER BY label,value",
            (start_date, end_date),
        ).fetchall()
        result[key] = [{"value": str(row["value"]), "label": str(row["label"] or row["value"])} for row in rows]
    payload = {"data_version": version, "options": result}
    response_cache_put(cache_key, payload)
    return payload


def include_rankings(params: dict[str, list[str]]) -> bool:
    value = first(params, "include_rankings", default="1").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise RequestError("include_rankings must be 0 or 1")


def build_rankings(
    conn: sqlite3.Connection,
    params: dict[str, list[str]],
    where_sql: str,
    values: list[Any],
    basis: str,
) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for dimension in RANKING_DIMENSIONS:
        rankings[dimension] = aggregate_rows(
            conn,
            table=ranking_table_for(conn, params, dimension),
            where_sql=where_sql,
            where_values=values,
            dimensions=[dimension],
            basis=basis,
            sort_by="spend",
            sort_dir="desc",
            limit=8,
        )
    return rankings


def rankings_payload(conn: sqlite3.Connection, params: dict[str, list[str]]) -> dict[str, Any]:
    version = check_version(conn, params)
    cache_key = response_cache_key("rankings", version, ranking_semantic_params(params))
    owner_event: Any = None
    while owner_event is None:
        with _RESPONSE_CACHE_LOCK:
            cached = _RESPONSE_CACHE.get(cache_key)
            if cached is not None:
                _RESPONSE_CACHE.move_to_end(cache_key)
                return cached
            existing = _RANKINGS_INFLIGHT.get(cache_key)
            if existing is None:
                owner_event = threading.Event()
                _RANKINGS_INFLIGHT[cache_key] = owner_event
                break
        if not existing.wait(timeout=30):
            raise RequestError("ranking computation is still busy", HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        start_date, end_date = parse_range(conn, params)
        basis = metric_basis(params)
        where_sql, values = build_where(params, start_date, end_date)
        payload = {
            "data_version": version,
            "rankings": build_rankings(conn, params, where_sql, values, basis),
        }
        response_cache_put(cache_key, payload)
        return payload
    finally:
        with _RESPONSE_CACHE_LOCK:
            event = _RANKINGS_INFLIGHT.pop(cache_key, None)
            if event is not None:
                event.set()


def query_payload(conn: sqlite3.Connection, params: dict[str, list[str]]) -> dict[str, Any]:
    version = check_version(conn, params)
    cache_key = response_cache_key("query", version, params)
    cached = response_cache_get(cache_key)
    if cached is not None:
        return cached
    start_date, end_date = parse_range(conn, params)
    dimensions = normalize_dimensions(params)
    basis = metric_basis(params)
    where_sql, values = build_where(params, start_date, end_date)
    rows_table = detail_table_for(conn, params, dimensions)
    context_table = context_table_for(conn, params)
    sort_by = first(params, "sort_by", "sort", default="spend")
    sort_dir = first(params, "sort_dir", default="desc").lower()
    if sort_dir not in {"asc", "desc"}:
        raise RequestError("sort_dir must be asc or desc")
    limit = clamp_int(first(params, "limit", default="50"), default=50, minimum=1, maximum=MAX_PAGE_SIZE)
    offset = clamp_int(first(params, "offset", default="0"), default=0, minimum=0, maximum=10_000_000)
    rows = aggregate_rows(
        conn,
        table=rows_table,
        where_sql=where_sql,
        where_values=values,
        dimensions=dimensions,
        basis=basis,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
        include_group_total=True,
    )
    if rows:
        total_rows = int(rows[0].get("__group_total") or 0)
        for row in rows:
            row.pop("__group_total", None)
    else:
        total_rows = grouped_count(conn, where_sql, values, dimensions, table=rows_table) if offset else 0
    totals, mapping_quality = aggregate_total_and_quality(
        conn, where_sql, values, basis, table=context_table
    )
    mapping_quality["source_scope_exclusions"] = source_scope_exclusions(
        conn,
        start_date,
        end_date,
        basis,
        filtered=business_filters_applied(params),
    )
    rankings = rankings_payload(conn, params)["rankings"] if include_rankings(params) else {}
    payload = {
        "data_version": version,
        "generated_at": get_meta(conn).get("generated_at", ""),
        "metric_basis": basis,
        "dimensions": dimensions,
        "totals": totals,
        "trend": aggregate_rows(
            conn,
            table=context_table,
            where_sql=where_sql,
            where_values=values,
            dimensions=["dt"],
            basis=basis,
            sort_by="dt",
            sort_dir="asc",
        ),
        "rankings": rankings,
        "rows": rows,
        "pagination": {"total": total_rows, "offset": offset, "returned": len(rows), "limit": limit},
        "mapping_quality": mapping_quality,
    }
    response_cache_put(cache_key, payload)
    return payload


def export_csv(
    conn: sqlite3.Connection,
    params: dict[str, list[str]],
    *,
    table: str | None = None,
) -> tuple[str, bytes]:
    check_version(conn, params)
    start_date, end_date = parse_range(conn, params)
    dimensions = normalize_dimensions(params)
    basis = metric_basis(params)
    where_sql, values = build_where(params, start_date, end_date)
    selected_table = validate_query_table(table) if table is not None else detail_table_for(conn, params, dimensions)
    sort_by = first(params, "sort_by", "sort", default="spend")
    sort_dir = first(params, "sort_dir", default="desc")
    count = grouped_count(conn, where_sql, values, dimensions, table=selected_table)
    if count > MAX_EXPORT_ROWS:
        raise RequestError(f"export contains {count} rows; narrow filters below {MAX_EXPORT_ROWS}", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    rows = aggregate_rows(
        conn,
        table=selected_table,
        where_sql=where_sql,
        where_values=values,
        dimensions=dimensions,
        basis=basis,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    output = io.StringIO(newline="")
    fields = list(rows[0]) if rows else dimensions + ["spend", "d7_revenue", "d7_roas", "d30_revenue", "d30_roas", "revenue_diff", "lift_rate"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    filename = f"dramawave-attribution-{start_date}-{end_date}.csv"
    return filename, ("\ufeff" + output.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "DramawaveAttribution/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}\n")

    def send_bytes(
        self,
        body: bytes,
        *,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache_control: str = "private, max-age=0, must-revalidate",
        filename: str | None = None,
        allow_gzip: bool = True,
    ) -> None:
        etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        if self.headers.get("If-None-Match") == etag and status == HTTPStatus.OK:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        encoded = body
        use_gzip = allow_gzip and len(body) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        if use_gzip:
            encoded = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def send_json(self, payload: Any, *, status: int = HTTPStatus.OK, no_store: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_bytes(
            body,
            content_type="application/json; charset=utf-8",
            status=status,
            cache_control="private, no-store" if no_store else "private, max-age=0, must-revalidate",
        )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        params = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=100)
        try:
            if path in {"/", "/index.html"}:
                self.send_bytes(INDEX_PATH.read_bytes(), content_type="text/html; charset=utf-8", cache_control="private, no-store")
                return
            if path == "/healthz":
                self.handle_health()
                return
            if path == "/api/meta":
                with connect_sqlite(readonly=True) as conn:
                    self.send_json(meta_payload(conn), no_store=True)
                return
            if path == "/api/options":
                with connect_sqlite(readonly=True) as conn:
                    conn.execute("BEGIN")
                    self.send_json(options_payload(conn, params))
                return
            if path == "/api/query":
                with connect_sqlite(readonly=True) as conn:
                    conn.execute("BEGIN")
                    self.send_json(query_payload(conn, params))
                return
            if path == "/api/rankings":
                with connect_sqlite(readonly=True) as conn:
                    conn.execute("BEGIN")
                    self.send_json(rankings_payload(conn, params))
                return
            if path == "/api/export.csv":
                with connect_sqlite(readonly=True) as conn:
                    conn.execute("BEGIN")
                    filename, body = export_csv(conn, params)
                    self.send_bytes(body, content_type="text/csv; charset=utf-8", filename=filename, allow_gzip=False)
                return
            self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND, no_store=True)
        except RequestError as exc:
            self.send_json({"error": str(exc)}, status=exc.status, no_store=True)
        except (FileNotFoundError, sqlite3.Error) as exc:
            self.send_json({"error": "cache unavailable", "detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE, no_store=True)
        except Exception as exc:  # pragma: no cover - final safety net is exercised in deployment probes.
            self.log_error("unhandled request error: %r", exc)
            self.send_json({"error": "internal server error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR, no_store=True)

    def handle_health(self) -> None:
        try:
            with connect_sqlite(readonly=True) as conn:
                payload = meta_payload(conn)
                row_count = int(conn.execute("SELECT COUNT(*) n FROM attribution_fact").fetchone()["n"])
            healthy = bool(
                row_count
                and payload["data_version"]
                and payload["cache"]["range_complete"]
                and payload["cache"]["rollups_current"]
            )
            self.send_json(
                {
                    "ok": healthy,
                    "stale": payload["cache"]["stale"],
                    "data_version": payload["data_version"],
                    "generated_at": payload["generated_at"],
                    "cache_start_date": payload["cache"]["start_date"],
                    "cache_end_date": payload["cache"]["end_date"],
                    "fact_rows": row_count,
                    "cache_complete": payload["cache"]["range_complete"],
                    "current_date_present": payload["cache"]["current_date_present"],
                    "missing_dates": payload["cache"]["missing_dates"],
                    "rollups_current": payload["cache"]["rollups_current"],
                },
                status=(
                    HTTPStatus.OK
                    if healthy
                    else HTTPStatus.SERVICE_UNAVAILABLE
                ),
                no_store=True,
            )
        except (FileNotFoundError, sqlite3.Error) as exc:
            self.send_json({"ok": False, "error": "cache unavailable", "detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE, no_store=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DRAMAWAVE_ATTRIBUTION_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DRAMAWAVE_ATTRIBUTION_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--skip-mount-check", action="store_true", help="local tests only")
    args = parser.parse_args()
    if not db_path().exists():
        print(f"cache database does not exist: {db_path()}", file=sys.stderr)
        return 2
    try:
        verify_data_disk(db_path(), skip=args.skip_mount_check)
    except Exception as exc:
        print(f"cache mount verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving Dramawave attribution dashboard on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

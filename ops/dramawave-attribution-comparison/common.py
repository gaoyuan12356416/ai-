#!/usr/bin/env python3
"""Shared constants and SQLite helpers for the Dramawave attribution dashboard."""

from __future__ import annotations

import datetime as dt
import contextlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Iterable, Iterator


MIN_DATE = dt.date(2026, 8, 1)
RETENTION_DAYS = 60
DEFAULT_DB_PATH = "/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10.sqlite3"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8832
EXPECTED_DATA_DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
COMPARISON_WINDOW = "D10"
NEW_ATTRIBUTION_SOURCE = "kunlunads_dev.ads_app_revenues_10d"
API_SCHEMA_VERSION = 2
LEGACY_ATTRIBUTION_TOKEN = "d" + str(30)

DIMENSION_COLUMNS = {
    "dt": ("dt", "dt"),
    "channel": ("channel", "channel"),
    "delivery_product": ("app_id", "app_id"),
    "optimizer": ("optimizer_id", "optimizer_name"),
    "country_group": ("country_group", "country_group"),
    "account": ("ad_account_id", "ad_account_id"),
    "campaign": ("campaign_id", "campaign_name"),
    "adset": ("adset_id", "adset_name"),
    "matched_grain": ("matched_grain", "matched_grain"),
}

FILTER_COLUMNS = {
    "channel": "channel_id",
    "app_id": "app_id",
    "optimizer_id": "optimizer_id",
    "country_group": "country_group",
    "account_id": "ad_account_id",
}

BASE_METRICS = (
    "spend",
    "impressions",
    "clicks",
    "installs",
    "af_installs",
    "d7_users",
    "d7_purchase_d0",
    "d7_purchase_d7",
    "d7_revenue_iaa_d0",
    "d7_revenue_iap_d0",
    "d7_revenue_iaa_d7",
    "d7_revenue_iap_d7",
    "d7_ad_impression_count",
    "d10_users",
    "d10_purchase_d0",
    "d10_purchase_d7",
    "d10_revenue_iaa_d0",
    "d10_revenue_iap_d0",
    "d10_revenue_iaa_d7",
    "d10_revenue_iap_d7",
    "d10_ad_impression_count",
    "d7_candidate_keys",
    "d7_mapped_keys",
    "d7_ambiguous_keys",
    "d10_candidate_keys",
    "d10_mapped_keys",
    "d10_ambiguous_keys",
)

FLOAT_BASE_METRICS = frozenset(
    metric for metric in BASE_METRICS if metric == "spend" or "_revenue_" in metric
)

FACT_DIMENSIONS = (
    "dt",
    "channel",
    "channel_id",
    "product",
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
    "matched_grain",
    "mapping_status",
)

FILTER_ROLLUP_DIMENSIONS = (
    "dt",
    "channel",
    "channel_id",
    "product",
    "app_id",
    "optimizer_id",
    "optimizer_name",
    "country_group",
    "ad_account_id",
    "matched_grain",
    "mapping_status",
)

CAMPAIGN_ROLLUP_DIMENSIONS = FILTER_ROLLUP_DIMENSIONS + (
    "campaign_id",
    "campaign_name",
)

ROLLUP_TABLE_DIMENSIONS = {
    "attribution_filter_daily": FILTER_ROLLUP_DIMENSIONS,
    "attribution_campaign_daily": CAMPAIGN_ROLLUP_DIMENSIONS,
}

MAPPING_METRIC_SUFFIXES = ("_candidate_keys", "_mapped_keys", "_ambiguous_keys")
REVENUE_STAGE_METRICS = frozenset(
    metric
    for metric in BASE_METRICS
    if metric.startswith(("d7_", "d10_")) and not metric.endswith(MAPPING_METRIC_SUFFIXES)
)
CACHE_CONTRACT_COLUMNS = {
    "cache_meta": {"key", "value"},
    "attribution_fact": {"id", *FACT_DIMENSIONS, *BASE_METRICS},
    "refresh_log": {
        "id",
        "dt",
        "started_at",
        "finished_at",
        "status",
        "fact_rows",
        "detail",
        "source_custom_updated_at",
        "source_d7_updated_at",
        "source_d10_updated_at",
        "data_version",
    },
    "refresh_stage": {"run_id", "dt", "created_at", "payload"},
    "refresh_fact_stage": {"run_id", "created_at", *FACT_DIMENSIONS, *BASE_METRICS},
    "refresh_revenue_stage": {
        "run_id",
        "dt",
        "created_at",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "d7_present",
        "d10_present",
        *REVENUE_STAGE_METRICS,
    },
    **{
        table: {*dimensions, *BASE_METRICS}
        for table, dimensions in ROLLUP_TABLE_DIMENSIONS.items()
    },
}


def _rollup_schema_sql(table: str, dimensions: tuple[str, ...]) -> str:
    dimension_defaults = {
        "product": "Dramawave",
        "matched_grain": "none",
        "mapping_status": "spend_only",
    }
    columns = []
    for column in dimensions:
        default = dimension_defaults.get(column, "")
        columns.append(f"    {column} TEXT NOT NULL DEFAULT '{default}'")
    for metric in BASE_METRICS:
        sql_type = "REAL" if metric in FLOAT_BASE_METRICS else "INTEGER"
        columns.append(f"    {metric} {sql_type} NOT NULL DEFAULT 0")
    prefix = "filter" if table == "attribution_filter_daily" else "campaign"
    indexes = [f"CREATE INDEX IF NOT EXISTS idx_{prefix}_dt ON {table}(dt);"]
    for suffix, column in (
        ("channel_dt", "channel_id"),
        ("app_dt", "app_id"),
        ("optimizer_dt", "optimizer_id"),
        ("country_dt", "country_group"),
        ("account_dt", "ad_account_id"),
    ):
        indexes.append(f"CREATE INDEX IF NOT EXISTS idx_{prefix}_{suffix} ON {table}({column}, dt);")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(columns) + "\n);\n" + "\n".join(indexes)


ROLLUP_SCHEMA_SQL = "\n".join(
    _rollup_schema_sql(table, dimensions) for table, dimensions in ROLLUP_TABLE_DIMENSIONS.items()
)

CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attribution_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dt TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT 'Dramawave',
    app_id TEXT NOT NULL DEFAULT '',
    optimizer_id TEXT NOT NULL DEFAULT '',
    optimizer_name TEXT NOT NULL DEFAULT '',
    country_group TEXT NOT NULL DEFAULT '',
    ad_account_id TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    campaign_name TEXT NOT NULL DEFAULT '',
    adset_id TEXT NOT NULL DEFAULT '',
    adset_name TEXT NOT NULL DEFAULT '',
    ad_id TEXT NOT NULL DEFAULT '',
    ad_name TEXT NOT NULL DEFAULT '',
    matched_grain TEXT NOT NULL DEFAULT 'none',
    mapping_status TEXT NOT NULL DEFAULT 'spend_only',
    spend REAL NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    installs INTEGER NOT NULL DEFAULT 0,
    af_installs INTEGER NOT NULL DEFAULT 0,
    d7_users INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d7_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d7_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    d10_users INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d10_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d10_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    d7_candidate_keys INTEGER NOT NULL DEFAULT 0,
    d7_mapped_keys INTEGER NOT NULL DEFAULT 0,
    d7_ambiguous_keys INTEGER NOT NULL DEFAULT 0,
    d10_candidate_keys INTEGER NOT NULL DEFAULT 0,
    d10_mapped_keys INTEGER NOT NULL DEFAULT 0,
    d10_ambiguous_keys INTEGER NOT NULL DEFAULT 0
);
DROP INDEX IF EXISTS idx_fact_dt_channel;
DROP INDEX IF EXISTS idx_fact_dt_app;
DROP INDEX IF EXISTS idx_fact_dt_optimizer;
DROP INDEX IF EXISTS idx_fact_dt_country;
DROP INDEX IF EXISTS idx_fact_dt_account;
DROP INDEX IF EXISTS idx_fact_dt_campaign;
DROP INDEX IF EXISTS idx_fact_dt_adset;
CREATE INDEX IF NOT EXISTS idx_fact_dt ON attribution_fact(dt);
-- Equality-first companions keep common filtered ranges indexable. With only
-- (dt, value), SQLite cannot seek the second column once dt becomes a range.
CREATE INDEX IF NOT EXISTS idx_fact_channel_dt ON attribution_fact(channel_id, dt);
CREATE INDEX IF NOT EXISTS idx_fact_app_dt ON attribution_fact(app_id, dt);
CREATE INDEX IF NOT EXISTS idx_fact_optimizer_dt ON attribution_fact(optimizer_id, dt);
CREATE INDEX IF NOT EXISTS idx_fact_country_dt ON attribution_fact(country_group, dt);
CREATE INDEX IF NOT EXISTS idx_fact_account_dt ON attribution_fact(ad_account_id, dt);
CREATE TABLE IF NOT EXISTS cache_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dt TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    fact_rows INTEGER,
    detail TEXT,
    source_custom_updated_at TEXT,
    source_d7_updated_at TEXT,
    source_d10_updated_at TEXT,
    data_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_log_dt ON refresh_log(dt, id DESC);
CREATE TABLE IF NOT EXISTS refresh_stage (
    run_id TEXT NOT NULL,
    dt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY(run_id, dt)
);
CREATE INDEX IF NOT EXISTS idx_refresh_stage_created ON refresh_stage(created_at);
""" + ROLLUP_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS refresh_fact_stage (
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dt TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT 'Dramawave',
    app_id TEXT NOT NULL DEFAULT '',
    optimizer_id TEXT NOT NULL DEFAULT '',
    optimizer_name TEXT NOT NULL DEFAULT '',
    country_group TEXT NOT NULL DEFAULT '',
    ad_account_id TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    campaign_name TEXT NOT NULL DEFAULT '',
    adset_id TEXT NOT NULL DEFAULT '',
    adset_name TEXT NOT NULL DEFAULT '',
    ad_id TEXT NOT NULL DEFAULT '',
    ad_name TEXT NOT NULL DEFAULT '',
    matched_grain TEXT NOT NULL DEFAULT 'none',
    mapping_status TEXT NOT NULL DEFAULT 'spend_only',
    spend REAL NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    installs INTEGER NOT NULL DEFAULT 0,
    af_installs INTEGER NOT NULL DEFAULT 0,
    d7_users INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d7_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d7_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    d10_users INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d10_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d10_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    d7_candidate_keys INTEGER NOT NULL DEFAULT 0,
    d7_mapped_keys INTEGER NOT NULL DEFAULT 0,
    d7_ambiguous_keys INTEGER NOT NULL DEFAULT 0,
    d10_candidate_keys INTEGER NOT NULL DEFAULT 0,
    d10_mapped_keys INTEGER NOT NULL DEFAULT 0,
    d10_ambiguous_keys INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_refresh_fact_stage_run_day
    ON refresh_fact_stage(run_id, dt);
CREATE INDEX IF NOT EXISTS idx_refresh_fact_stage_created
    ON refresh_fact_stage(created_at);
CREATE TABLE IF NOT EXISTS refresh_revenue_stage (
    run_id TEXT NOT NULL,
    dt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    campaign_name TEXT NOT NULL DEFAULT '',
    adset_id TEXT NOT NULL DEFAULT '',
    adset_name TEXT NOT NULL DEFAULT '',
    ad_id TEXT NOT NULL DEFAULT '',
    ad_name TEXT NOT NULL DEFAULT '',
    d7_present INTEGER NOT NULL DEFAULT 0,
    d10_present INTEGER NOT NULL DEFAULT 0,
    d7_users INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d7_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d7_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d7_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d7_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d7_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    d10_users INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d0 INTEGER NOT NULL DEFAULT 0,
    d10_purchase_d7 INTEGER NOT NULL DEFAULT 0,
    d10_revenue_iaa_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d0 REAL NOT NULL DEFAULT 0,
    d10_revenue_iaa_d7 REAL NOT NULL DEFAULT 0,
    d10_revenue_iap_d7 REAL NOT NULL DEFAULT 0,
    d10_ad_impression_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id, dt, campaign_id, adset_id, ad_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_refresh_revenue_stage_created
    ON refresh_revenue_stage(created_at);
"""


def db_path() -> Path:
    return Path(os.environ.get("DRAMAWAVE_ATTRIBUTION_DB_PATH", DEFAULT_DB_PATH))


def verify_data_disk(path: Path, *, skip: bool = False) -> None:
    if skip:
        return
    resolved = path.resolve()
    data_root = Path("/mnt/data-disk").resolve()
    if os.path.commonpath((str(resolved), str(data_root))) != str(data_root):
        raise RuntimeError(f"cache path must stay under {data_root}: {resolved}")
    probe = resolved if resolved.exists() else resolved.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    result = subprocess.run(
        ["findmnt", "-n", "-o", "UUID", "-T", str(probe)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    actual = result.stdout.strip()
    if actual != EXPECTED_DATA_DISK_UUID:
        raise RuntimeError(f"unexpected data-disk UUID {actual!r}; expected {EXPECTED_DATA_DISK_UUID}")


def beijing_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def beijing_today() -> dt.date:
    return beijing_now().date()


def iso_now() -> str:
    return beijing_now().isoformat(timespec="seconds")


def retention_start(today: dt.date | None = None) -> dt.date:
    current = today or beijing_today()
    return max(MIN_DATE, current - dt.timedelta(days=RETENTION_DAYS - 1))


def parse_date(value: str, *, name: str = "date") -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: expected YYYY-MM-DD") from exc
    if parsed < MIN_DATE:
        raise ValueError(f"{name} cannot be earlier than {MIN_DATE.isoformat()}")
    return parsed


def yyyymmdd(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


@contextlib.contextmanager
def connect_sqlite(path: Path | str | None = None, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    target = Path(path or db_path())
    if readonly:
        uri = f"file:{target.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(CREATE_SCHEMA_SQL)
    try:
        yield conn
        if not readonly and conn.in_transaction:
            conn.commit()
    except Exception:
        if not readonly and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "INSERT INTO cache_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, encoded),
    )


def get_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in conn.execute("SELECT key,value FROM cache_meta"):
        try:
            result[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            result[row["key"]] = row["value"]
    return result


class CacheContractError(RuntimeError):
    """Raised when a cache cannot safely be interpreted as published D10 data."""


def validate_cache_contract(
    conn: sqlite3.Connection,
    *,
    allow_unpublished_empty: bool = False,
) -> dict[str, Any]:
    """Validate cache schema and attribution semantics before reading or refreshing.

    A fresh, structurally D10 database may be opened by the refresher before its
    first successful publication. Every web read requires a data version and the
    exact semantic markers written in the same transaction as that version.
    """

    metadata = get_meta(conn)
    mismatches: list[str] = []
    for table, required in CACHE_CONTRACT_COLUMNS.items():
        actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(required - actual)
        if missing:
            mismatches.append(f"{table} missing {','.join(missing)}")
        legacy = sorted(
            column for column in actual if LEGACY_ATTRIBUTION_TOKEN in column.lower()
        )
        if legacy:
            mismatches.append(f"{table} contains legacy columns {','.join(legacy)}")

    data_version = str(metadata.get("data_version") or "")
    comparison_window = str(metadata.get("comparison_window") or "")
    attribution_source = str(metadata.get("new_attribution_source") or "")
    unpublished_empty = False
    if allow_unpublished_empty and not data_version:
        row = conn.execute("SELECT COUNT(*) AS n FROM attribution_fact").fetchone()
        unpublished_empty = int(row["n"] or 0) == 0
        if not unpublished_empty:
            mismatches.append("unpublished cache contains attribution facts")

    if not unpublished_empty:
        if not data_version:
            mismatches.append("data_version is missing")
        if comparison_window != COMPARISON_WINDOW:
            mismatches.append(
                f"comparison_window={comparison_window or '<missing>'}; expected {COMPARISON_WINDOW}"
            )
        if attribution_source != NEW_ATTRIBUTION_SOURCE:
            mismatches.append(
                "new_attribution_source="
                f"{attribution_source or '<missing>'}; expected {NEW_ATTRIBUTION_SOURCE}"
            )
    elif comparison_window or attribution_source:
        # Partial metadata on an unpublished file indicates an interrupted or
        # hand-edited bootstrap. Only a complete exact pair is safe to resume.
        if comparison_window != COMPARISON_WINDOW:
            mismatches.append(
                f"comparison_window={comparison_window or '<missing>'}; expected {COMPARISON_WINDOW}"
            )
        if attribution_source != NEW_ATTRIBUTION_SOURCE:
            mismatches.append(
                "new_attribution_source="
                f"{attribution_source or '<missing>'}; expected {NEW_ATTRIBUTION_SOURCE}"
            )

    if mismatches:
        raise CacheContractError("D10 cache contract rejected: " + "; ".join(mismatches))
    return metadata


def preflight_existing_cache(
    path: Path | str | None = None,
    *,
    allow_unpublished_empty: bool = False,
) -> dict[str, Any]:
    """Validate an existing cache without creating WAL or shared-memory files.

    This immutable connection is only for the first contract gate. Normal API
    reads must continue using connect_sqlite(readonly=True) so they observe
    committed WAL updates from live refreshes.
    """

    target = Path(path or db_path())
    uri = f"file:{target.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        return validate_cache_contract(
            conn,
            allow_unpublished_empty=allow_unpublished_empty,
        )
    finally:
        conn.close()


def insert_facts(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    columns = FACT_DIMENSIONS + BASE_METRICS
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO attribution_fact({','.join(columns)}) VALUES({placeholders})"
    count = 0

    def prepared() -> Iterator[tuple[Any, ...]]:
        nonlocal count
        for row in rows:
            count += 1
            yield tuple(row.get(column, 0 if column in BASE_METRICS else "") for column in columns)

    conn.executemany(sql, prepared())
    return count


def insert_staged_facts(
    conn: sqlite3.Connection,
    run_id: str,
    created_at: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    columns = FACT_DIMENSIONS + BASE_METRICS
    insert_columns = ("run_id", "created_at") + columns
    placeholders = ",".join("?" for _ in insert_columns)
    sql = f"INSERT INTO refresh_fact_stage({','.join(insert_columns)}) VALUES({placeholders})"
    count = 0

    def prepared() -> Iterator[tuple[Any, ...]]:
        nonlocal count
        for row in rows:
            count += 1
            values = tuple(row.get(column, 0 if column in BASE_METRICS else "") for column in columns)
            yield (run_id, created_at) + values

    conn.executemany(sql, prepared())
    return count

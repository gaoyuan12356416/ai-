#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the Feishu-protected AI game performance static dashboard.

MySQL is used only by the refresh command. Normal browser requests read the
versioned static files generated from the local SQLite cache.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = Path(os.environ.get("AI_GAME_REPORT_DATA_ROOT", "/mnt/data-disk/ai-game-performance"))
DEFAULT_CACHE_DB = DEFAULT_DATA_ROOT / "cache" / "ai-game-performance.sqlite3"
DEFAULT_WEB_DIR = Path(
    os.environ.get("AI_GAME_REPORT_WEB_DIR", "/usr/share/nginx/html/reports/ai-game-performance")
)
BASE_MODULE_DIR = Path(os.environ.get("AI_GAME_REPORT_BASE_MODULE_DIR", "/root/codex_test"))
PUBLIC_URL = "https://ai.yingliangads.com/reports/ai-game-performance/"
PRODUCT = "Neonarcade"
BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_RETENTION_DAYS = int(os.environ.get("AI_GAME_REPORT_RETENTION_DAYS", "60"))
DEFAULT_REFRESH_DAYS = int(os.environ.get("AI_GAME_REPORT_REFRESH_DAYS", "3"))
PUBLISHED_FILE_STALE_GRACE_SECONDS = 24 * 60 * 60
MYSQL_ERROR_MAX_CHARS = 400

PLATFORM_CHANNEL = {0: "Facebook Ads", 1: "googleadwords_int", 3: "tiktokglobal_int"}
SOURCE_CHANNELS = frozenset(PLATFORM_CHANNEL.values())
UNITY_CHANNEL = "unityads_int"
GENERIC_GAME_ID = "1000000000000000000"
UNMAPPED_GAME_ID = "__UNMAPPED__"
AMBIGUOUS_GAME_ID = "__AMBIGUOUS__"
UNMARKED_GAME_ID = "__UNMARKED__"

MANUAL_SOURCE_COLUMNS = [
    "source_id",
    "dt",
    "conversion_country",
    "game_name",
    "game_id",
    "channel",
    "campaign_id",
    "adset_id",
    "ad_id",
    "campaign_name",
    "adset_name",
    "ad_name",
    "manual_cost",
    "manual_installs",
    "d1_retained",
    "play_duration_seconds",
    "day0_revenue",
    "day1_revenue",
    "updated_at",
]

DELIVERY_SOURCE_COLUMNS = [
    "source_id",
    "dt",
    "platform",
    "source_country",
    "campaign_id",
    "adset_id",
    "ad_id",
    "source_spend",
    "source_installs",
    "source_impressions",
    "source_clicks",
    "updated_at",
]

OVERVIEW_COLUMNS = [
    "dt",
    "game_id",
    "game_name",
    "channel",
    "mapping_status",
    "spend_source",
    "effective_spend",
    "source_spend",
    "source_installs",
    "source_impressions",
    "source_clicks",
    "manual_cost",
    "manual_installs",
    "d1_retained",
    "play_weighted_seconds",
    "play_weight_installs",
    "day0_revenue",
    "day1_revenue",
    "source_row_count",
    "manual_row_count",
    "source_ctr",
    "source_cpi",
    "d1_retention_rate",
    "avg_play_duration_seconds",
    "d0_roas",
    "cost_per_d1_retained",
]

DELIVERY_COLUMNS = [
    "dt",
    "game_id",
    "game_name",
    "channel",
    "mapping_status",
    "source_country",
    "campaign_id",
    "adset_id",
    "ad_id",
    "source_spend",
    "source_installs",
    "source_impressions",
    "source_clicks",
    "source_row_count",
]

CONVERSION_COLUMNS = [
    "dt",
    "game_id",
    "game_name",
    "channel",
    "conversion_country",
    "campaign_id",
    "adset_id",
    "ad_id",
    "campaign_name",
    "adset_name",
    "ad_name",
    "manual_cost",
    "manual_installs",
    "d1_retained",
    "play_duration_seconds",
    "play_weighted_seconds",
    "play_weight_installs",
    "day0_revenue",
    "day1_revenue",
    "manual_row_count",
]

NUMERIC_COLUMNS = {
    "effective_spend",
    "source_spend",
    "source_installs",
    "source_impressions",
    "source_clicks",
    "manual_cost",
    "manual_installs",
    "d1_retained",
    "play_duration_seconds",
    "play_weighted_seconds",
    "play_weight_installs",
    "day0_revenue",
    "day1_revenue",
    "source_row_count",
    "manual_row_count",
    "source_ctr",
    "source_cpi",
    "d1_retention_rate",
    "avg_play_duration_seconds",
    "d0_roas",
    "cost_per_d1_retained",
}

PROJECT_ID_PATTERN = re.compile(r"projectid\[(\d+)\]", re.IGNORECASE)
SAFE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_DIR_PATTERN = re.compile(r"^\d{8}T\d{12,20}[+-]\d{4}$")


class MySQLQueryError(RuntimeError):
    pass


class MySQLQueryTimeout(MySQLQueryError):
    def __init__(self, timeout: int, stderr: str = ""):
        detail = " stderr=%s" % stderr if stderr else ""
        super().__init__("MySQLQueryTimeout(timeout=%s)%s" % (timeout, detail))
        self.timeout = timeout


def bj_now() -> datetime:
    return datetime.now(BJ_TZ)


def dec(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def number(value, digits: int = 6) -> float:
    return round(float(dec(value)), digits)


def integer(value) -> int:
    return int(dec(value))


def text(value) -> str:
    return str(value or "").strip()


def normalize_id(value) -> str:
    raw = text(value)
    return str(int(raw)) if raw.isdigit() else ""


def sql_quote(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def validate_date(value: str) -> str:
    raw = text(value)
    if not SAFE_DATE_PATTERN.fullmatch(raw):
        raise ValueError("invalid date: %s" % raw)
    date.fromisoformat(raw)
    return raw


def each_date(start_date: str, end_date: str):
    current = date.fromisoformat(validate_date(start_date))
    end = date.fromisoformat(validate_date(end_date))
    if current > end:
        raise ValueError("start_date must be <= end_date")
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def redact_mysql_error(value, secrets=()) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    message = str(value or "")
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "<redacted>")
    message = re.sub(r"(?i)--password(?:=|\s+)\S+", "--password=<redacted>", message)
    message = re.sub(r"(?i)(?<!\S)-p\S+", "-p<redacted>", message)
    return " ".join(message.split())[:MYSQL_ERROR_MAX_CHARS]


def mysql_command_env():
    module_dir = str(BASE_MODULE_DIR)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    try:
        import opera_product_daily_dashboard as base
    except ImportError as exc:  # pragma: no cover - production-only dependency
        raise RuntimeError("opera_product_daily_dashboard.py is required for MySQL refresh") from exc
    raw = list(base.mysql_cmd())
    safe = []
    secrets = []
    env = os.environ.copy()
    if env.get("MYSQL_PWD"):
        secrets.append(env["MYSQL_PWD"])
    index = 0
    while index < len(raw):
        arg = str(raw[index])
        if arg in ("-p", "--password") and index + 1 < len(raw):
            secrets.append(str(raw[index + 1]))
            index += 2
            continue
        if arg.startswith("--password="):
            secrets.append(arg.split("=", 1)[1])
            index += 1
            continue
        if arg.startswith("-p") and len(arg) > 2:
            secrets.append(arg[2:])
            index += 1
            continue
        safe.append(arg)
        index += 1
    if secrets:
        env["MYSQL_PWD"] = secrets[-1]
    return safe, env, tuple(secrets)


def run_mysql(sql: str, timeout: int = 180):
    command, env, secrets = mysql_command_env()
    compact_sql = " ".join(str(sql).split())
    try:
        process = subprocess.run(
            command + [compact_sql],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MySQLQueryTimeout(timeout, redact_mysql_error(getattr(exc, "stderr", ""), secrets))
    if process.returncode:
        detail = redact_mysql_error(process.stderr, secrets)
        raise MySQLQueryError(
            "MySQLQueryError(returncode=%s, stderr=%s)" % (process.returncode, detail or "<empty>")
        )
    return list(csv.reader(process.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))


def assert_read_only() -> None:
    rows = run_mysql("SELECT @@read_only", timeout=30)
    if not rows or text(rows[0][0]) != "1":
        raise RuntimeError("refusing refresh because MySQL @@read_only is not 1")


def detect_manual_duration_column() -> str:
    rows = run_mysql(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA='ads_ai'
          AND TABLE_NAME='ads_manual_daily_performance'
          AND COLUMN_NAME IN ('play_duration_seconds','avg_play_duration_seconds')
        ORDER BY FIELD(COLUMN_NAME,'play_duration_seconds','avg_play_duration_seconds')
        """,
        timeout=30,
    )
    available = [text(row[0]) for row in rows]
    for candidate in ("play_duration_seconds", "avg_play_duration_seconds"):
        if candidate in available:
            return candidate
    raise RuntimeError("manual duration column is missing")


def source_date_bounds(retention_days: int = DEFAULT_RETENTION_DAYS):
    rows = run_mysql(
        "SELECT DATE_FORMAT(MIN(stat_date),'%Y-%m-%d'), DATE_FORMAT(MAX(stat_date),'%Y-%m-%d') "
        "FROM ads_ai.ads_manual_daily_performance",
        timeout=60,
    )
    today = bj_now().date()
    retention_start = today - timedelta(days=max(1, retention_days) - 1)
    if not rows or not rows[0][0] or not rows[0][1]:
        return retention_start.isoformat(), today.isoformat()
    start = max(date.fromisoformat(rows[0][0]), retention_start)
    end = min(date.fromisoformat(rows[0][1]), today)
    return start.isoformat(), end.isoformat()


def extract_project_id(*names: str) -> str:
    for name in names:
        matches = sorted(set(PROJECT_ID_PATTERN.findall(text(name))))
        if len(matches) == 1:
            return normalize_id(matches[0])
        if len(matches) > 1:
            return AMBIGUOUS_GAME_ID
    return ""


def display_game_name(game_id: str, game_name: str = "") -> str:
    if game_id == GENERIC_GAME_ID:
        return text(game_name) or "通用素材"
    if game_id == AMBIGUOUS_GAME_ID:
        return "多游戏待归属"
    if game_id == UNMAPPED_GAME_ID:
        return "未归属"
    if game_id == UNMARKED_GAME_ID or not game_id:
        return "未标记游戏"
    return text(game_name) or ("游戏 " + game_id)


def normalize_manual_row(row: dict) -> dict:
    raw_game_id = normalize_id(row.get("game_id"))
    if not raw_game_id:
        raw_game_id = extract_project_id(row.get("ad_name"), row.get("adset_name"), row.get("campaign_name"))
    game_id = raw_game_id or UNMARKED_GAME_ID
    game_name = display_game_name(game_id, row.get("game_name"))
    installs = integer(row.get("manual_installs"))
    duration = number(row.get("play_duration_seconds"))
    return {
        "source_id": integer(row.get("source_id")),
        "dt": validate_date(row.get("dt")),
        "conversion_country": text(row.get("conversion_country")) or "未填",
        "game_name": game_name,
        "game_id": game_id,
        "channel": text(row.get("channel")) or "未填",
        "campaign_id": normalize_id(row.get("campaign_id")),
        "adset_id": normalize_id(row.get("adset_id")),
        "ad_id": normalize_id(row.get("ad_id")),
        "campaign_name": text(row.get("campaign_name")),
        "adset_name": text(row.get("adset_name")),
        "ad_name": text(row.get("ad_name")),
        "manual_cost": number(row.get("manual_cost")),
        "manual_installs": installs,
        "d1_retained": integer(row.get("d1_retained")),
        "play_duration_seconds": duration,
        "day0_revenue": number(row.get("day0_revenue")),
        "day1_revenue": number(row.get("day1_revenue")),
        "updated_at": text(row.get("updated_at")),
    }


def fetch_manual_day(day: str, duration_column: str):
    day = validate_date(day)
    if duration_column not in ("play_duration_seconds", "avg_play_duration_seconds"):
        raise ValueError("unsupported duration column")
    sql = """
    SELECT
      id,
      DATE_FORMAT(stat_date, '%Y-%m-%d'),
      country,
      game_name,
      game_id,
      channel,
      campaign_id,
      adset_id,
      ad_id,
      campaign_name,
      adset_name,
      ad_name,
      cost,
      install,
      day1_retention_count,
      {duration_column},
      day0_revenue,
      day1_revenue,
      DATE_FORMAT(updated_at, '%Y-%m-%d %H:%i:%s')
    FROM ads_ai.ads_manual_daily_performance FORCE INDEX(uk_manual_daily_ad_grain)
    WHERE stat_date = {day}
    ORDER BY id
    """.format(duration_column=duration_column, day=sql_quote(day))
    return [normalize_manual_row(dict(zip(MANUAL_SOURCE_COLUMNS, item))) for item in run_mysql(sql, timeout=240)]


def fetch_delivery_day(day: str):
    day = validate_date(day)
    sql = """
    SELECT
      id,
      DATE_FORMAT(dt, '%Y-%m-%d'),
      platform,
      country,
      campaign_id,
      adset_id,
      ad_id,
      spend,
      installs,
      impressions,
      clicks,
      DATE_FORMAT(updated_at, '%Y-%m-%d %H:%i:%s')
    FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(pss)
    WHERE product = {product}
      AND dt = {day}
      AND platform IN (0,1,3)
    ORDER BY id
    """.format(product=sql_quote(PRODUCT), day=sql_quote(day))
    rows = []
    for item in run_mysql(sql, timeout=180):
        source = dict(zip(DELIVERY_SOURCE_COLUMNS, item))
        platform = integer(source.get("platform"))
        rows.append(
            {
                "source_id": integer(source.get("source_id")),
                "dt": validate_date(source.get("dt")),
                "platform": platform,
                "channel": PLATFORM_CHANNEL.get(platform, "platform_%s" % platform),
                "source_country": text(source.get("source_country")) or "未填",
                "campaign_id": normalize_id(source.get("campaign_id")),
                "adset_id": normalize_id(source.get("adset_id")),
                "ad_id": normalize_id(source.get("ad_id")),
                "source_spend": number(source.get("source_spend")),
                "source_installs": integer(source.get("source_installs")),
                "source_impressions": integer(source.get("source_impressions")),
                "source_clicks": integer(source.get("source_clicks")),
                "updated_at": text(source.get("updated_at")),
            }
        )
    return rows


def cache_conn(cache_db: Path = DEFAULT_CACHE_DB):
    cache_db = Path(cache_db)
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(cache_db), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_cache_schema(connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS manual_conversion_fact (
          source_id INTEGER PRIMARY KEY,
          dt TEXT NOT NULL,
          conversion_country TEXT NOT NULL,
          game_id TEXT NOT NULL,
          game_name TEXT NOT NULL,
          channel TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          adset_id TEXT NOT NULL,
          ad_id TEXT NOT NULL,
          campaign_name TEXT NOT NULL,
          adset_name TEXT NOT NULL,
          ad_name TEXT NOT NULL,
          manual_cost REAL NOT NULL,
          manual_installs INTEGER NOT NULL,
          d1_retained INTEGER NOT NULL,
          play_duration_seconds REAL NOT NULL,
          day0_revenue REAL NOT NULL,
          day1_revenue REAL NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_manual_fact_dt ON manual_conversion_fact(dt);
        CREATE INDEX IF NOT EXISTS idx_manual_fact_game_dt ON manual_conversion_fact(game_id, dt);
        CREATE INDEX IF NOT EXISTS idx_manual_fact_channel_dt ON manual_conversion_fact(channel, dt);

        CREATE TABLE IF NOT EXISTS delivery_fact (
          source_id INTEGER PRIMARY KEY,
          dt TEXT NOT NULL,
          platform INTEGER NOT NULL,
          channel TEXT NOT NULL,
          source_country TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          adset_id TEXT NOT NULL,
          ad_id TEXT NOT NULL,
          game_id TEXT NOT NULL,
          game_name TEXT NOT NULL,
          mapping_status TEXT NOT NULL,
          source_spend REAL NOT NULL,
          source_installs INTEGER NOT NULL,
          source_impressions INTEGER NOT NULL,
          source_clicks INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_fact_dt ON delivery_fact(dt);
        CREATE INDEX IF NOT EXISTS idx_delivery_fact_game_dt ON delivery_fact(game_id, dt);
        CREATE INDEX IF NOT EXISTS idx_delivery_fact_channel_dt ON delivery_fact(channel, dt);

        CREATE TABLE IF NOT EXISTS refresh_log (
          fact_type TEXT NOT NULL,
          dt TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          refreshed_at TEXT NOT NULL,
          PRIMARY KEY (fact_type, dt)
        );

        CREATE TABLE IF NOT EXISTS publish_audit (
          data_version TEXT PRIMARY KEY,
          start_date TEXT NOT NULL,
          end_date TEXT NOT NULL,
          manual_rows INTEGER NOT NULL,
          delivery_rows INTEGER NOT NULL,
          quality_json TEXT NOT NULL,
          published_at TEXT NOT NULL
        );
        """
    )
    connection.commit()


def replace_manual_day(connection, day: str, rows) -> None:
    day = validate_date(day)
    columns = [
        "source_id",
        "dt",
        "conversion_country",
        "game_id",
        "game_name",
        "channel",
        "campaign_id",
        "adset_id",
        "ad_id",
        "campaign_name",
        "adset_name",
        "ad_name",
        "manual_cost",
        "manual_installs",
        "d1_retained",
        "play_duration_seconds",
        "day0_revenue",
        "day1_revenue",
        "updated_at",
    ]
    now = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    with connection:
        connection.execute("DELETE FROM manual_conversion_fact WHERE dt=?", (day,))
        connection.executemany(
            "INSERT INTO manual_conversion_fact (%s) VALUES (%s)"
            % (",".join(columns), ",".join("?" for _ in columns)),
            ([row[column] for column in columns] for row in rows),
        )
        connection.execute(
            "INSERT OR REPLACE INTO refresh_log(fact_type,dt,row_count,refreshed_at) VALUES ('manual',?,?,?)",
            (day, len(rows), now),
        )


def mapping_key(channel: str, campaign_id: str, adset_id: str, ad_id: str):
    if channel == "googleadwords_int":
        if campaign_id and adset_id:
            return channel, campaign_id, adset_id
        return None
    if channel in ("Facebook Ads", "tiktokglobal_int"):
        if ad_id:
            return channel, ad_id
        return None
    return None


def build_game_mapping(connection):
    game_names = {}
    key_games = collections.defaultdict(set)
    rows = connection.execute(
        """
        SELECT dt, source_id, channel, campaign_id, adset_id, ad_id, game_id, game_name
        FROM manual_conversion_fact
        ORDER BY dt DESC, source_id DESC
        """
    )
    for row in rows:
        game_id = text(row["game_id"])
        if game_id not in (UNMARKED_GAME_ID, UNMAPPED_GAME_ID, AMBIGUOUS_GAME_ID, ""):
            candidate_name = display_game_name(game_id, row["game_name"])
            current_name = game_names.get(game_id, "")
            if not current_name or (current_name == "游戏 " + game_id and candidate_name != current_name):
                game_names[game_id] = candidate_name
            key = mapping_key(row["channel"], row["campaign_id"], row["adset_id"], row["ad_id"])
            if key:
                key_games[key].add(game_id)
    return key_games, game_names


def assign_delivery_game(row: dict, key_games, game_names):
    key = mapping_key(row["channel"], row["campaign_id"], row["adset_id"], row["ad_id"])
    games = key_games.get(key, set()) if key else set()
    if len(games) == 1:
        game_id = next(iter(games))
        return game_id, game_names.get(game_id, display_game_name(game_id)), "mapped"
    if len(games) > 1:
        return AMBIGUOUS_GAME_ID, display_game_name(AMBIGUOUS_GAME_ID), "ambiguous"
    return UNMAPPED_GAME_ID, display_game_name(UNMAPPED_GAME_ID), "unmapped"


def replace_delivery_day(connection, day: str, rows, key_games, game_names) -> None:
    day = validate_date(day)
    columns = [
        "source_id",
        "dt",
        "platform",
        "channel",
        "source_country",
        "campaign_id",
        "adset_id",
        "ad_id",
        "game_id",
        "game_name",
        "mapping_status",
        "source_spend",
        "source_installs",
        "source_impressions",
        "source_clicks",
        "updated_at",
    ]
    normalized = []
    for row in rows:
        game_id, game_name, status = assign_delivery_game(row, key_games, game_names)
        item = dict(row)
        item.update({"game_id": game_id, "game_name": game_name, "mapping_status": status})
        normalized.append(item)
    now = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    with connection:
        connection.execute("DELETE FROM delivery_fact WHERE dt=?", (day,))
        connection.executemany(
            "INSERT INTO delivery_fact (%s) VALUES (%s)"
            % (",".join(columns), ",".join("?" for _ in columns)),
            ([row[column] for column in columns] for row in normalized),
        )
        connection.execute(
            "INSERT OR REPLACE INTO refresh_log(fact_type,dt,row_count,refreshed_at) VALUES ('delivery',?,?,?)",
            (day, len(normalized), now),
        )


def remap_delivery_cache(connection, key_games, game_names) -> int:
    updates = []
    for row in connection.execute(
        "SELECT source_id,channel,campaign_id,adset_id,ad_id FROM delivery_fact ORDER BY source_id"
    ):
        game_id, game_name, status = assign_delivery_game(dict(row), key_games, game_names)
        updates.append((game_id, game_name, status, row["source_id"]))
    with connection:
        connection.executemany(
            "UPDATE delivery_fact SET game_id=?,game_name=?,mapping_status=? WHERE source_id=?", updates
        )
    return len(updates)


def prune_cache(connection, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    cutoff = (bj_now().date() - timedelta(days=max(1, retention_days) - 1)).isoformat()
    with connection:
        manual = connection.execute("DELETE FROM manual_conversion_fact WHERE dt<?", (cutoff,)).rowcount
        delivery = connection.execute("DELETE FROM delivery_fact WHERE dt<?", (cutoff,)).rowcount
        logs = connection.execute("DELETE FROM refresh_log WHERE dt<?", (cutoff,)).rowcount
    return {"cutoff": cutoff, "manual_deleted": manual, "delivery_deleted": delivery, "logs_deleted": logs}


def refresh_cache(start_date: str, end_date: str, cache_db: Path = DEFAULT_CACHE_DB, retention_days: int = DEFAULT_RETENTION_DAYS):
    start_date = validate_date(start_date)
    end_date = validate_date(end_date)
    assert_read_only()
    duration_column = detect_manual_duration_column()
    connection = cache_conn(cache_db)
    timings = []
    try:
        ensure_cache_schema(connection)
        for day in each_date(start_date, end_date):
            started = time.monotonic()
            rows = fetch_manual_day(day, duration_column)
            replace_manual_day(connection, day, rows)
            timings.append({"fact": "manual", "date": day, "rows": len(rows), "seconds": round(time.monotonic() - started, 3)})
        key_games, game_names = build_game_mapping(connection)
        for day in each_date(start_date, end_date):
            started = time.monotonic()
            rows = fetch_delivery_day(day)
            replace_delivery_day(connection, day, rows, key_games, game_names)
            timings.append({"fact": "delivery", "date": day, "rows": len(rows), "seconds": round(time.monotonic() - started, 3)})
        remapped = remap_delivery_cache(connection, key_games, game_names)
        pruned = prune_cache(connection, retention_days)
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("SQLite quick_check failed: %s" % check)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "duration_column": duration_column,
            "mapping_keys": len(key_games),
            "remapped_delivery_rows": remapped,
            "timings": timings,
            "pruned": pruned,
            "quick_check": check,
        }
    finally:
        connection.close()


def cache_date_bounds(connection):
    row = connection.execute(
        """
        SELECT MIN(dt), MAX(dt)
        FROM (
          SELECT dt FROM manual_conversion_fact
          UNION ALL
          SELECT dt FROM delivery_fact
        )
        """
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None, None
    return row[0], row[1]


def ratio(numerator, denominator) -> float:
    denominator_value = dec(denominator)
    return number(dec(numerator) / denominator_value) if denominator_value else 0.0


def overview_rows_for_day(connection, day: str):
    day = validate_date(day)
    combined = {}
    manual_sql = """
    SELECT
      game_id,
      MAX(game_name) AS game_name,
      channel,
      SUM(manual_cost) AS manual_cost,
      SUM(manual_installs) AS manual_installs,
      SUM(d1_retained) AS d1_retained,
      SUM(play_duration_seconds * manual_installs) AS play_weighted_seconds,
      SUM(CASE WHEN manual_installs > 0 THEN manual_installs ELSE 0 END) AS play_weight_installs,
      SUM(day0_revenue) AS day0_revenue,
      SUM(day1_revenue) AS day1_revenue,
      COUNT(*) AS manual_row_count
    FROM manual_conversion_fact
    WHERE dt=?
    GROUP BY game_id, channel
    """
    for row in connection.execute(manual_sql, (day,)):
        key = (row["game_id"], row["channel"])
        combined[key] = {
            "dt": day,
            "game_id": row["game_id"],
            "game_name": display_game_name(row["game_id"], row["game_name"]),
            "channel": row["channel"],
            "mapping_status": "manual_only",
            "source_spend": 0.0,
            "source_installs": 0,
            "source_impressions": 0,
            "source_clicks": 0,
            "source_row_count": 0,
            "manual_cost": number(row["manual_cost"]),
            "manual_installs": integer(row["manual_installs"]),
            "d1_retained": integer(row["d1_retained"]),
            "play_weighted_seconds": number(row["play_weighted_seconds"]),
            "play_weight_installs": integer(row["play_weight_installs"]),
            "day0_revenue": number(row["day0_revenue"]),
            "day1_revenue": number(row["day1_revenue"]),
            "manual_row_count": integer(row["manual_row_count"]),
        }
    delivery_sql = """
    SELECT
      game_id,
      MAX(game_name) AS game_name,
      channel,
      mapping_status,
      SUM(source_spend) AS source_spend,
      SUM(source_installs) AS source_installs,
      SUM(source_impressions) AS source_impressions,
      SUM(source_clicks) AS source_clicks,
      COUNT(*) AS source_row_count
    FROM delivery_fact
    WHERE dt=?
    GROUP BY game_id, channel, mapping_status
    """
    for row in connection.execute(delivery_sql, (day,)):
        key = (row["game_id"], row["channel"])
        target = combined.setdefault(
            key,
            {
                "dt": day,
                "game_id": row["game_id"],
                "game_name": display_game_name(row["game_id"], row["game_name"]),
                "channel": row["channel"],
                "mapping_status": row["mapping_status"],
                "source_spend": 0.0,
                "source_installs": 0,
                "source_impressions": 0,
                "source_clicks": 0,
                "source_row_count": 0,
                "manual_cost": 0.0,
                "manual_installs": 0,
                "d1_retained": 0,
                "play_weighted_seconds": 0.0,
                "play_weight_installs": 0,
                "day0_revenue": 0.0,
                "day1_revenue": 0.0,
                "manual_row_count": 0,
            },
        )
        target["mapping_status"] = row["mapping_status"]
        target["source_spend"] += number(row["source_spend"])
        target["source_installs"] += integer(row["source_installs"])
        target["source_impressions"] += integer(row["source_impressions"])
        target["source_clicks"] += integer(row["source_clicks"])
        target["source_row_count"] += integer(row["source_row_count"])
    result = []
    for target in combined.values():
        if target["channel"] in SOURCE_CHANNELS:
            target["effective_spend"] = number(target["source_spend"])
            target["spend_source"] = "custom_source_insight"
        elif target["channel"] == UNITY_CHANNEL:
            target["effective_spend"] = number(target["manual_cost"])
            target["spend_source"] = "manual_fallback"
        else:
            target["effective_spend"] = 0.0
            target["spend_source"] = "none"
        target["source_ctr"] = ratio(target["source_clicks"], target["source_impressions"])
        target["source_cpi"] = ratio(target["source_spend"], target["source_installs"])
        target["d1_retention_rate"] = ratio(target["d1_retained"], target["manual_installs"])
        target["avg_play_duration_seconds"] = ratio(
            target["play_weighted_seconds"], target["play_weight_installs"]
        )
        target["d0_roas"] = ratio(target["day0_revenue"], target["effective_spend"])
        target["cost_per_d1_retained"] = ratio(target["effective_spend"], target["d1_retained"])
        result.append(target)
    return sorted(result, key=lambda row: (-dec(row["effective_spend"]), row["game_name"], row["channel"]))


def delivery_rows_for_day(connection, day: str):
    day = validate_date(day)
    rows = []
    for row in connection.execute(
        """
        SELECT dt,game_id,game_name,channel,mapping_status,source_country,
               campaign_id,adset_id,ad_id,source_spend,source_installs,
               source_impressions,source_clicks
        FROM delivery_fact
        WHERE dt=?
        ORDER BY source_spend DESC, source_id
        """,
        (day,),
    ):
        item = dict(row)
        item["source_spend"] = number(item["source_spend"])
        for key in ("source_installs", "source_impressions", "source_clicks"):
            item[key] = integer(item[key])
        item["source_row_count"] = 1
        rows.append(item)
    return rows


def conversion_rows_for_day(connection, day: str):
    day = validate_date(day)
    rows = []
    for row in connection.execute(
        """
        SELECT dt,game_id,game_name,channel,conversion_country,campaign_id,adset_id,ad_id,
               campaign_name,adset_name,ad_name,manual_cost,manual_installs,d1_retained,
               play_duration_seconds,day0_revenue,day1_revenue
        FROM manual_conversion_fact
        WHERE dt=?
        ORDER BY manual_cost DESC, source_id
        """,
        (day,),
    ):
        item = dict(row)
        item["manual_cost"] = number(item["manual_cost"])
        item["manual_installs"] = integer(item["manual_installs"])
        item["d1_retained"] = integer(item["d1_retained"])
        item["play_duration_seconds"] = number(item["play_duration_seconds"])
        item["play_weighted_seconds"] = number(
            dec(item["play_duration_seconds"]) * dec(item["manual_installs"])
        )
        item["play_weight_installs"] = item["manual_installs"] if item["manual_installs"] > 0 else 0
        item["day0_revenue"] = number(item["day0_revenue"])
        item["day1_revenue"] = number(item["day1_revenue"])
        item["manual_row_count"] = 1
        rows.append(item)
    return rows


def quality_for_range(connection, start_date: str, end_date: str):
    row = connection.execute(
        """
        SELECT
          COUNT(*) AS source_rows,
          COALESCE(SUM(source_spend),0) AS source_spend,
          SUM(mapping_status='mapped') AS mapped_rows,
          COALESCE(SUM(CASE WHEN mapping_status='mapped' THEN source_spend ELSE 0 END),0) AS mapped_spend,
          SUM(mapping_status='ambiguous') AS ambiguous_rows,
          COALESCE(SUM(CASE WHEN mapping_status='ambiguous' THEN source_spend ELSE 0 END),0) AS ambiguous_spend,
          SUM(mapping_status='unmapped') AS unmapped_rows,
          COALESCE(SUM(CASE WHEN mapping_status='unmapped' THEN source_spend ELSE 0 END),0) AS unmapped_spend
        FROM delivery_fact
        WHERE dt BETWEEN ? AND ?
        """,
        (start_date, end_date),
    ).fetchone()
    source_rows = integer(row["source_rows"])
    source_spend = number(row["source_spend"])
    manual_row = connection.execute(
        "SELECT COUNT(*) rows_count,COALESCE(SUM(manual_installs),0) installs,COALESCE(SUM(manual_cost),0) cost "
        "FROM manual_conversion_fact WHERE dt BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()
    return {
        "source_rows": source_rows,
        "source_spend": source_spend,
        "mapped_rows": integer(row["mapped_rows"]),
        "mapped_spend": number(row["mapped_spend"]),
        "mapped_row_ratio": ratio(row["mapped_rows"], source_rows),
        "mapped_spend_ratio": ratio(row["mapped_spend"], source_spend),
        "ambiguous_rows": integer(row["ambiguous_rows"]),
        "ambiguous_spend": number(row["ambiguous_spend"]),
        "unmapped_rows": integer(row["unmapped_rows"]),
        "unmapped_spend": number(row["unmapped_spend"]),
        "manual_rows": integer(manual_row["rows_count"]),
        "manual_installs": integer(manual_row["installs"]),
        "manual_cost": number(manual_row["cost"]),
    }


def encode_rows(rows, columns):
    dict_columns = [column for column in columns if column not in NUMERIC_COLUMNS]
    dictionaries = {}
    indexes = {}
    for column in dict_columns:
        values = sorted({text(row.get(column)) for row in rows})
        dictionaries[column] = values
        indexes[column] = {value: index for index, value in enumerate(values)}
    compact = []
    for row in rows:
        encoded = []
        for column in columns:
            value = row.get(column, "")
            if column in indexes:
                encoded.append(indexes[column][text(value)])
            else:
                encoded.append(value)
        compact.append(encoded)
    return {"columns": columns, "dict_columns": dict_columns, "dicts": dictionaries, "rows": compact}


def aggregate_overview(rows):
    total = {
        "effective_spend": Decimal("0"),
        "source_spend": Decimal("0"),
        "source_installs": 0,
        "source_impressions": 0,
        "source_clicks": 0,
        "manual_cost": Decimal("0"),
        "manual_installs": 0,
        "d1_retained": 0,
        "play_weighted_seconds": Decimal("0"),
        "play_weight_installs": 0,
        "day0_revenue": Decimal("0"),
        "day1_revenue": Decimal("0"),
        "source_row_count": 0,
        "manual_row_count": 0,
    }
    for row in rows:
        for key in ("effective_spend", "source_spend", "manual_cost", "play_weighted_seconds", "day0_revenue", "day1_revenue"):
            total[key] += dec(row.get(key))
        for key in (
            "source_installs",
            "source_impressions",
            "source_clicks",
            "manual_installs",
            "d1_retained",
            "play_weight_installs",
            "source_row_count",
            "manual_row_count",
        ):
            total[key] += integer(row.get(key))
    result = {key: (number(value) if isinstance(value, Decimal) else value) for key, value in total.items()}
    result.update(
        {
            "source_ctr": ratio(total["source_clicks"], total["source_impressions"]),
            "source_cpi": ratio(total["source_spend"], total["source_installs"]),
            "d1_retention_rate": ratio(total["d1_retained"], total["manual_installs"]),
            "avg_play_duration_seconds": ratio(total["play_weighted_seconds"], total["play_weight_installs"]),
            "d0_roas": ratio(total["day0_revenue"], total["effective_spend"]),
            "cost_per_d1_retained": ratio(total["effective_spend"], total["d1_retained"]),
        }
    )
    return result


def atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def prune_published_versions(data_dir: Path, current_version: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    removed = 0
    if not data_dir.exists():
        return removed
    for child in data_dir.iterdir():
        if not child.is_dir() or child.name == current_version or not VERSION_DIR_PATTERN.fullmatch(child.name):
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age >= PUBLISHED_FILE_STALE_GRACE_SECONDS:
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError:
                continue
    return removed


def report_html() -> str:
    path = ROOT / "report.html"
    if not path.exists():
        raise RuntimeError("report.html is missing")
    return path.read_text(encoding="utf-8")


def publish_from_cache(cache_db: Path = DEFAULT_CACHE_DB, output_dir: Path = DEFAULT_WEB_DIR):
    output_dir = Path(output_dir)
    connection = cache_conn(cache_db)
    try:
        ensure_cache_schema(connection)
        start_date, end_date = cache_date_bounds(connection)
        if not start_date or not end_date:
            raise RuntimeError("cache has no report dates")
        generated = bj_now()
        data_version = generated.strftime("%Y%m%dT%H%M%S%f%z")
        data_files = {"overview": {}, "delivery": {}, "conversion": {}}
        row_counts = {"overview": 0, "delivery": 0, "conversion": 0}
        daily_totals = {}
        for day in each_date(start_date, end_date):
            view_rows = {
                "overview": overview_rows_for_day(connection, day),
                "delivery": delivery_rows_for_day(connection, day),
                "conversion": conversion_rows_for_day(connection, day),
            }
            daily_totals[day] = aggregate_overview(view_rows["overview"])
            for view, rows in view_rows.items():
                columns = {
                    "overview": OVERVIEW_COLUMNS,
                    "delivery": DELIVERY_COLUMNS,
                    "conversion": CONVERSION_COLUMNS,
                }[view]
                payload = encode_rows(rows, columns)
                payload.update({"view": view, "date": day, "data_version": data_version})
                relative = "data/%s/%s/%s.json" % (data_version, view, day)
                target = output_dir / relative
                atomic_write(target, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                data_files[view][day] = relative
                row_counts[view] += len(rows)
        quality = quality_for_range(connection, start_date, end_date)
        summary_rows = []
        for day in each_date(start_date, end_date):
            summary_rows.extend(overview_rows_for_day(connection, day))
        summary = aggregate_overview(summary_rows)
        manifest = {
            "meta": {
                "title": "AI游戏产品测转化报表",
                "generated_at": generated.strftime("%Y-%m-%d %H:%M:%S"),
                "data_version": data_version,
                "start_date": start_date,
                "end_date": end_date,
                "default_date": (generated.date() - timedelta(days=1)).isoformat(),
                "public_url": PUBLIC_URL,
                "timezone": "Asia/Shanghai",
                "currency_note": "源表没有币种字段；金额按上传与渠道源保持同一币种展示为 $。",
                "source_note": "渠道事实：ads_custom_source_insight product=Neonarcade；产品测转化：ads_manual_daily_performance；Unity 花费使用手工表兜底。",
                "today_partial": end_date == generated.date().isoformat(),
            },
            "views": {
                "overview": {"label": "游戏总览", "description": "渠道事实与产品测转化在日期+游戏+渠道共享维度汇总"},
                "delivery": {"label": "渠道明细", "description": "统一渠道事实；country 表示渠道国家/分组"},
                "conversion": {"label": "转化明细", "description": "手工产品测转化事实；country 表示转化国家"},
            },
            "data_files": data_files,
            "row_counts": row_counts,
            "summary": summary,
            "daily_totals": daily_totals,
            "quality": quality,
        }
        # index.html never references an unpublished version. latest.json is the commit point.
        atomic_write(output_dir / "index.html", report_html())
        atomic_write(output_dir / "latest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        audit_recorded = True
        try:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO publish_audit(data_version,start_date,end_date,manual_rows,delivery_rows,quality_json,published_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        data_version,
                        start_date,
                        end_date,
                        row_counts["conversion"],
                        row_counts["delivery"],
                        json.dumps(quality, ensure_ascii=False, sort_keys=True),
                        generated.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
        except sqlite3.Error:
            # latest.json is the filesystem commit point. An audit-only failure
            # must not turn an already committed publication into a false retry.
            audit_recorded = False
        pruned_versions = prune_published_versions(output_dir / "data", data_version)
        return {
            "data_version": data_version,
            "start_date": start_date,
            "end_date": end_date,
            "row_counts": row_counts,
            "quality": quality,
            "summary": summary,
            "output_dir": str(output_dir),
            "pruned_versions": pruned_versions,
            "audit_recorded": audit_recorded,
        }
    finally:
        connection.close()


def check_cache(cache_db: Path = DEFAULT_CACHE_DB):
    connection = cache_conn(cache_db)
    try:
        ensure_cache_schema(connection)
        bounds = cache_date_bounds(connection)
        return {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "start_date": bounds[0],
            "end_date": bounds[1],
            "manual_rows": connection.execute("SELECT COUNT(*) FROM manual_conversion_fact").fetchone()[0],
            "delivery_rows": connection.execute("SELECT COUNT(*) FROM delivery_fact").fetchone()[0],
        }
    finally:
        connection.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--check-cache", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_WEB_DIR)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not any((args.refresh_cache, args.publish, args.full_refresh, args.check_cache)):
        raise SystemExit("choose --refresh-cache, --publish, --full-refresh, or --check-cache")
    result = {}
    if args.full_refresh:
        args.refresh_cache = True
        assert_read_only()
        source_start, source_end = source_date_bounds(args.retention_days)
        args.start_date = args.start_date or source_start
        args.end_date = args.end_date or source_end
    elif args.refresh_cache:
        end = date.fromisoformat(validate_date(args.end_date)) if args.end_date else bj_now().date()
        start = (
            date.fromisoformat(validate_date(args.start_date))
            if args.start_date
            else end - timedelta(days=max(1, args.refresh_days) - 1)
        )
        args.start_date, args.end_date = start.isoformat(), end.isoformat()
    if args.refresh_cache:
        result["refresh"] = refresh_cache(
            args.start_date,
            args.end_date,
            cache_db=args.cache_db,
            retention_days=args.retention_days,
        )
    if args.publish:
        result["publish"] = publish_from_cache(cache_db=args.cache_db, output_dir=args.output_dir)
    if args.check_cache:
        result["cache"] = check_cache(args.cache_db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


if __name__ == "__main__":
    main()

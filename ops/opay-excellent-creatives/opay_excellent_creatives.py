#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the public OPay monthly excellent-creative static report.

MySQL is accessed only by explicit refresh commands and must be the verified
read-only replica. Browser traffic reads versioned JSON produced from SQLite.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BJ_TZ = timezone(timedelta(hours=8))
PUBLIC_URL = "https://ai.yingliangads.com/reports/opay-excellent-creatives/"
DEFAULT_DATA_ROOT = Path(
    os.environ.get("OPAY_REPORT_DATA_ROOT", "/mnt/data-disk/opay-excellent-creatives")
)
DEFAULT_CACHE_DB = DEFAULT_DATA_ROOT / "cache" / "opay-excellent-creatives.sqlite3"
DEFAULT_WEB_DIR = Path(
    os.environ.get(
        "OPAY_REPORT_WEB_DIR",
        "/usr/share/nginx/html/reports/opay-excellent-creatives",
    )
)
BASE_MODULE_DIR = Path(os.environ.get("OPAY_REPORT_BASE_MODULE_DIR", "/root/codex_test"))
KEYWORD_CONFIG_PATH = Path(
    os.environ.get(
        "OPAY_REPORT_KEYWORD_CONFIG",
        str(ROOT / "selling_points.v2026-08-26.json"),
    )
)
KEYWORD_OVERRIDES_PATH = Path(
    os.environ.get(
        "OPAY_REPORT_KEYWORD_OVERRIDES",
        str(ROOT / "selling_point_overrides.json"),
    )
)
MEDIA_TIMEOUT_SECONDS = int(os.environ.get("OPAY_REPORT_MEDIA_TIMEOUT_SECONDS", "12"))
MEDIA_MAX_BYTES = int(os.environ.get("OPAY_REPORT_MEDIA_MAX_BYTES", str(25 * 1024 * 1024)))
MEDIA_WORKERS = max(1, min(12, int(os.environ.get("OPAY_REPORT_MEDIA_WORKERS", "6"))))
MYSQL_ERROR_MAX_CHARS = 400
SCHEMA_VERSION = 1
REPORT_START_MONTH = "2026-01"
SAFE_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
SAFE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_VERSION_PATTERN = re.compile(r"^\d{8}T\d{12,20}[+-]\d{4}$")

PLATFORM_CHANNEL = {0: "Meta", 1: "Google", 3: "TikTok"}
AF_PID_PLATFORM = {"Facebook Ads": 0, "googleadwords_int": 1, "tiktokglobal_int": 3}
APP_ORDER = ("NG OPay", "PK OPay")
CHANNEL_ORDER = ("Google", "Meta", "TikTok")
TARGET_SETTING_PRODUCTS = {"opay": "NG OPay", "opaypakistan": "PK OPay"}
KNOWN_APP_ALIASES = {
    "opay": "NG OPay",
    "opay ngn": "NG OPay",
    "opaypakistan": "PK OPay",
}
DEFAULT_MEDIA_HOST_SUFFIXES = (
    ".myqcloud.com",
    ".yingliang.tech",
    ".yingliangads.com",
)

INSIGHT_COLUMNS = (
    "id",
    "dt",
    "platform",
    "app_id",
    "campaign_id",
    "adset_id",
    "ad_id",
    "resource_id",
    "source_id",
    "source_type",
    "resource_type",
    "resource_name",
    "resource_tag",
    "spend",
    "impressions",
    "clicks",
    "installs",
    "auto_publish_dt",
    "updated_at",
)
AF_COLUMNS = (
    "dt",
    "pid",
    "setting_product_id",
    "campaign_id",
    "adset_id",
    "ad_id",
    "d0_count",
    "source_row_count",
)


class MySQLQueryError(RuntimeError):
    pass


class MySQLQueryTimeout(MySQLQueryError):
    pass


class FrozenMonthError(RuntimeError):
    pass


def bj_now() -> datetime:
    return datetime.now(BJ_TZ)


def text(value) -> str:
    return str(value or "").strip()


def integer(value) -> int:
    try:
        return int(Decimal(str(value or "0")))
    except (InvalidOperation, ValueError):
        return 0


def decimal_value(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def cents(value) -> int:
    return int((decimal_value(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def dollars(value_cents: int) -> float:
    return round(int(value_cents or 0) / 100.0, 2)


def number(value, digits: int = 6) -> float:
    return round(float(value or 0), digits)


def normalized_id(value) -> str:
    raw = text(value)
    if not raw or not raw.isdigit():
        return ""
    return str(int(raw))


def sql_quote(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def chunked(values, size=500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def validate_date(value: str) -> str:
    raw = text(value)
    if not SAFE_DATE_PATTERN.fullmatch(raw):
        raise ValueError("invalid date: %s" % raw)
    date.fromisoformat(raw)
    return raw


def validate_month(value: str) -> str:
    raw = text(value)
    if not SAFE_MONTH_PATTERN.fullmatch(raw):
        raise ValueError("invalid month: %s" % raw)
    parsed = date.fromisoformat(raw + "-01")
    return parsed.strftime("%Y-%m")


def month_bounds(month: str):
    month = validate_month(month)
    start = date.fromisoformat(month + "-01")
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def each_date(start_date: str, end_date: str):
    current = date.fromisoformat(validate_date(start_date))
    end = date.fromisoformat(validate_date(end_date))
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def each_month(start_month: str, end_month: str):
    current = date.fromisoformat(validate_month(start_month) + "-01")
    end = date.fromisoformat(validate_month(end_month) + "-01")
    if current > end:
        raise ValueError("from-month must be <= to-month")
    while current <= end:
        yield current.strftime("%Y-%m")
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)


def latest_complete_month(now: datetime | None = None) -> str:
    now = now or bj_now()
    first = date(now.year, now.month, 1)
    previous = first - timedelta(days=1)
    return previous.strftime("%Y-%m")


def app_key(value) -> str:
    normalized = " ".join(text(value).casefold().split())
    return KNOWN_APP_ALIASES.get(normalized, "")


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
    except ImportError as exc:  # pragma: no cover - production dependency
        raise RuntimeError("opera_product_daily_dashboard.py is required for refresh") from exc
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
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        detail = redact_mysql_error(getattr(exc, "stderr", ""), secrets)
        raise MySQLQueryTimeout("MySQL query timed out after %ss: %s" % (timeout, detail)) from exc
    if process.returncode:
        raise MySQLQueryError(
            "MySQL query failed: %s"
            % (redact_mysql_error(process.stderr, secrets) or "unknown error")
        )
    return list(csv.reader(process.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))


def mysql_cli_port(command) -> int:
    command = [str(item) for item in command]
    for index, item in enumerate(command):
        if item == "-P" and index + 1 < len(command):
            return integer(command[index + 1])
        if item.startswith("-P") and len(item) > 2:
            return integer(item[2:])
        if item == "--port" and index + 1 < len(command):
            return integer(command[index + 1])
        if item.startswith("--port="):
            return integer(item.split("=", 1)[1])
    return 0


def assert_read_only() -> None:
    command, _environment, _secrets = mysql_command_env()
    if mysql_cli_port(command) != 63350:
        raise RuntimeError("refusing refresh: expected read-only MySQL entry port 63350")
    rows = run_mysql("SELECT @@read_only", timeout=30)
    if not rows or text(rows[0][0]) != "1":
        raise RuntimeError("refusing refresh: expected read-only MySQL port 63350")


def load_product_config() -> dict[int, str]:
    rows = run_mysql(
        """
        SELECT id,name,revenue_evente
        FROM kunlunads_dev.setting_product
        WHERE LOWER(name) IN ('opay','opaypakistan')
        ORDER BY id
        """,
        timeout=30,
    )
    found = {}
    for row in rows:
        product_name = text(row[1]).casefold()
        target_app = TARGET_SETTING_PRODUCTS.get(product_name)
        if not target_app:
            continue
        events = {item.strip() for item in text(row[2]).split(",") if item.strip()}
        if "First_Transaction" not in events:
            raise RuntimeError("setting_product %s does not configure First_Transaction" % row[1])
        if target_app in found.values():
            raise RuntimeError("duplicate setting_product config for %s" % target_app)
        found[integer(row[0])] = target_app
    if set(found.values()) != set(APP_ORDER):
        raise RuntimeError("missing OPay setting_product configuration")
    return found


def fetch_insight_day(day: str):
    day = validate_date(day)
    sql = """
    SELECT
      id,DATE_FORMAT(dt,'%%Y-%%m-%%d'),platform,app_id,campaign_id,adset_id,ad_id,
      resource_id,source_id,source_type,resource_type,resource_name,resource_tag,
      spend,impressions,clicks,installs,
      COALESCE(DATE_FORMAT(auto_publish_dt,'%%Y-%%m-%%d'),''),
      DATE_FORMAT(updated_at,'%%Y-%%m-%%d %%H:%%i:%%s')
    FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(pss)
    WHERE product='Opay' AND dt=%s AND platform IN (0,1,3)
    ORDER BY id
    """ % sql_quote(day)
    return [dict(zip(INSIGHT_COLUMNS, row)) for row in run_mysql(sql, timeout=180)]


def fetch_af_day(day: str, product_config: dict[int, str]):
    day = validate_date(day)
    ids = ",".join(str(item) for item in sorted(product_config))
    sql = """
    SELECT
      dt,pid,app_id,campaign_id,adset_id,ad_id,
      SUM(revenue_event_count1_0),COUNT(*)
    FROM kunlunads_dev.ads_af_revenues_zone FORCE INDEX(ddc)
    WHERE data_source=0 AND dt=%s AND app_id IN (%s)
    GROUP BY dt,pid,app_id,campaign_id,adset_id,ad_id
    """ % (sql_quote(day), ids)
    result = []
    for source in run_mysql(sql, timeout=180):
        row = dict(zip(AF_COLUMNS, source))
        platform = AF_PID_PLATFORM.get(text(row["pid"]))
        app = product_config.get(integer(row["setting_product_id"]))
        if platform is None or not app:
            continue
        result.append(
            {
                "dt": validate_date(row["dt"]),
                "platform": platform,
                "app": app,
                "campaign_id": normalized_id(row["campaign_id"]),
                "adset_id": normalized_id(row["adset_id"]),
                "ad_id": normalized_id(row["ad_id"]),
                "d0_count": integer(row["d0_count"]),
                "source_row_count": integer(row["source_row_count"]),
            }
        )
    return result


def cache_conn(cache_db: Path = DEFAULT_CACHE_DB):
    cache_db = Path(cache_db)
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(cache_db), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_cache_schema(connection)
    return connection


def ensure_cache_schema(connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cache_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ads_source_dim (
          source_id INTEGER PRIMARY KEY,
          source_type INTEGER NOT NULL,
          custom_source_id INTEGER NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS material_dim (
          custom_source_id INTEGER PRIMARY KEY,
          product TEXT NOT NULL,
          material_type INTEGER NOT NULL,
          name TEXT NOT NULL,
          source_url TEXT NOT NULL,
          cover_url TEXT NOT NULL,
          designer TEXT NOT NULL,
          maker TEXT NOT NULL,
          maker_source TEXT NOT NULL,
          tag_name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          is_delete INTEGER NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS platform_daily (
          dt TEXT NOT NULL,
          platform INTEGER NOT NULL,
          app TEXT NOT NULL,
          spend_cents INTEGER NOT NULL,
          impressions INTEGER NOT NULL,
          clicks INTEGER NOT NULL,
          installs INTEGER NOT NULL,
          source_row_count INTEGER NOT NULL,
          PRIMARY KEY (dt,platform,app)
        );
        CREATE TABLE IF NOT EXISTS af_daily (
          dt TEXT NOT NULL,
          platform INTEGER NOT NULL,
          app TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          adset_id TEXT NOT NULL,
          ad_id TEXT NOT NULL,
          d0_count INTEGER NOT NULL,
          source_row_count INTEGER NOT NULL,
          PRIMARY KEY (dt,platform,app,campaign_id,adset_id,ad_id)
        );
        CREATE TABLE IF NOT EXISTS material_daily (
          dt TEXT NOT NULL,
          platform INTEGER NOT NULL,
          app TEXT NOT NULL,
          custom_source_id INTEGER NOT NULL,
          campaign_id TEXT NOT NULL,
          adset_id TEXT NOT NULL,
          ad_id TEXT NOT NULL,
          spend_cents INTEGER NOT NULL,
          impressions INTEGER NOT NULL,
          clicks INTEGER NOT NULL,
          installs INTEGER NOT NULL,
          af_d0_count INTEGER NOT NULL,
          first_auto_publish_dt TEXT NOT NULL,
          resource_tags_json TEXT NOT NULL,
          source_row_count INTEGER NOT NULL,
          mapping_status TEXT NOT NULL,
          PRIMARY KEY (dt,platform,app,custom_source_id,ad_id)
        );
        CREATE TABLE IF NOT EXISTS daily_audit (
          dt TEXT NOT NULL,
          platform INTEGER NOT NULL,
          app TEXT NOT NULL,
          platform_spend_cents INTEGER NOT NULL,
          exact_spend_cents INTEGER NOT NULL,
          ambiguous_spend_cents INTEGER NOT NULL,
          invalid_mapping_spend_cents INTEGER NOT NULL,
          out_of_scope_spend_cents INTEGER NOT NULL,
          strict_gap_spend_cents INTEGER NOT NULL,
          source_row_count INTEGER NOT NULL,
          exact_row_count INTEGER NOT NULL,
          invalid_row_count INTEGER NOT NULL,
          ambiguous_ad_days INTEGER NOT NULL,
          af_total INTEGER NOT NULL,
          af_mapped INTEGER NOT NULL,
          PRIMARY KEY (dt,platform,app)
        );
        CREATE TABLE IF NOT EXISTS month_state (
          month TEXT NOT NULL,
          stage TEXT NOT NULL,
          generated_at TEXT NOT NULL,
          snapshot_path TEXT NOT NULL,
          snapshot_sha256 TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          status TEXT NOT NULL,
          diff_json TEXT NOT NULL,
          data_version TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (month,stage)
        );
        CREATE TABLE IF NOT EXISTS publish_audit (
          data_version TEXT PRIMARY KEY,
          generated_at TEXT NOT NULL,
          month_count INTEGER NOT NULL,
          manifest_sha256 TEXT NOT NULL,
          manifest_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS material_daily_month_scope
          ON material_daily(dt,platform,app,custom_source_id);
        CREATE INDEX IF NOT EXISTS af_daily_month_scope
          ON af_daily(dt,platform,app);
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


def cached_ids(connection, table: str, column: str, values):
    result = set()
    values = sorted({integer(value) for value in values if integer(value) > 0})
    for part in chunked(values):
        placeholders = ",".join("?" for _ in part)
        rows = connection.execute(
            "SELECT %s FROM %s WHERE %s IN (%s)" % (column, table, column, placeholders),
            part,
        ).fetchall()
        result.update(integer(row[0]) for row in rows)
    return result


def fetch_ads_source_dims(source_ids):
    result = []
    for part in chunked(sorted({integer(item) for item in source_ids if integer(item) > 0})):
        sql = """
        SELECT id,source_type,source_id
        FROM kunlunads_dev.ads_source
        WHERE id IN (%s)
        """ % ",".join(str(item) for item in part)
        for row in run_mysql(sql, timeout=60):
            result.append(
                {
                    "source_id": integer(row[0]),
                    "source_type": integer(row[1]),
                    "custom_source_id": integer(row[2]),
                }
            )
    return result


def fetch_admin_map(designers):
    numeric_ids = sorted({integer(item) for item in designers if text(item).isdigit() and integer(item) > 0})
    usernames = sorted({text(item) for item in designers if text(item) and not text(item).isdigit()})
    rows = []
    for part in chunked(numeric_ids):
        sql = """
        SELECT id,username,name,main_username
        FROM kunlunads_dev.admin_users
        WHERE id IN (%s)
        """ % ",".join(str(item) for item in part)
        rows.extend(run_mysql(sql, timeout=60))
    for part in chunked(usernames, size=200):
        sql = """
        SELECT id,username,name,main_username
        FROM kunlunads_dev.admin_users
        WHERE username IN (%s)
        """ % ",".join(sql_quote(item) for item in part)
        rows.extend(run_mysql(sql, timeout=60))
    by_id = {}
    by_username = {}
    for row in rows:
        display = text(row[3]) or text(row[2])
        if not display:
            continue
        by_id[integer(row[0])] = display
        by_username[text(row[1]).casefold()] = display
    return by_id, by_username


def resolve_maker(designer, by_id, by_username):
    designer = text(designer)
    if designer.isdigit():
        maker = by_id.get(integer(designer), "未登记")
        return maker, ("admin_id" if maker != "未登记" else "unresolved")
    maker = by_username.get(designer.casefold(), "未登记")
    return maker, ("username" if maker != "未登记" else "unresolved")


def fetch_material_dims(custom_source_ids):
    raw = []
    for part in chunked(sorted({integer(item) for item in custom_source_ids if integer(item) > 0})):
        sql = """
        SELECT id,product,type,name,url,cover,designer,tag_name,
               DATE_FORMAT(created_at,'%%Y-%%m-%%d %%H:%%i:%%s'),
               DATE_FORMAT(updated_at,'%%Y-%%m-%%d %%H:%%i:%%s'),is_delete
        FROM kunlunads_dev.ads_custom_source
        WHERE id IN (%s)
        """ % ",".join(str(item) for item in part)
        raw.extend(run_mysql(sql, timeout=90))
    designers = [row[6] for row in raw]
    by_id, by_username = fetch_admin_map(designers)
    result = []
    for row in raw:
        designer = text(row[6])
        maker, maker_source = resolve_maker(designer, by_id, by_username)
        result.append(
            {
                "custom_source_id": integer(row[0]),
                "product": text(row[1]),
                "material_type": integer(row[2]),
                "name": text(row[3]),
                "source_url": text(row[4]),
                "cover_url": text(row[5]),
                "designer": designer,
                "maker": maker,
                "maker_source": maker_source,
                "tag_name": text(row[7]),
                "created_at": text(row[8]),
                "updated_at": text(row[9]),
                "is_delete": integer(row[10]),
            }
        )
    return result


def ensure_dimensions(
    connection,
    insight_rows,
    *,
    force=False,
    refreshed_source_ids=None,
    refreshed_material_ids=None,
):
    refreshed_source_ids = refreshed_source_ids if refreshed_source_ids is not None else set()
    refreshed_material_ids = refreshed_material_ids if refreshed_material_ids is not None else set()
    source_ids = {
        integer(row.get("source_id"))
        for row in insight_rows
        if integer(row.get("platform")) in (0, 3) and integer(row.get("source_id")) > 0
    }
    material_ids = {
        integer(row.get("resource_id"))
        for row in insight_rows
        if integer(row.get("platform")) in (0, 3) and integer(row.get("resource_id")) > 0
    }
    if force:
        source_query = source_ids - refreshed_source_ids
        material_query = material_ids - refreshed_material_ids
    else:
        source_query = source_ids - cached_ids(connection, "ads_source_dim", "source_id", source_ids)
        material_query = material_ids - cached_ids(
            connection, "material_dim", "custom_source_id", material_ids
        )
    now_text = bj_now().isoformat()
    source_dims = fetch_ads_source_dims(source_query)
    material_dims = fetch_material_dims(material_query)
    with connection:
        for row in source_dims:
            connection.execute(
                """
                INSERT OR REPLACE INTO ads_source_dim(
                  source_id,source_type,custom_source_id,fetched_at
                ) VALUES(?,?,?,?)
                """,
                (
                    row["source_id"],
                    row["source_type"],
                    row["custom_source_id"],
                    now_text,
                ),
            )
        for row in material_dims:
            connection.execute(
                """
                INSERT OR REPLACE INTO material_dim(
                  custom_source_id,product,material_type,name,source_url,cover_url,
                  designer,maker,maker_source,tag_name,created_at,updated_at,is_delete,fetched_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["custom_source_id"],
                    row["product"],
                    row["material_type"],
                    row["name"],
                    row["source_url"],
                    row["cover_url"],
                    row["designer"],
                    row["maker"],
                    row["maker_source"],
                    row["tag_name"],
                    row["created_at"],
                    row["updated_at"],
                    row["is_delete"],
                    now_text,
                ),
            )
    refreshed_source_ids.update(source_query)
    refreshed_material_ids.update(material_query)


def load_dim_maps(connection, source_ids, material_ids):
    sources = {}
    materials = {}
    for part in chunked(sorted({integer(item) for item in source_ids if integer(item) > 0})):
        placeholders = ",".join("?" for _ in part)
        for row in connection.execute(
            "SELECT * FROM ads_source_dim WHERE source_id IN (%s)" % placeholders, part
        ):
            sources[integer(row["source_id"])] = dict(row)
    for part in chunked(sorted({integer(item) for item in material_ids if integer(item) > 0})):
        placeholders = ",".join("?" for _ in part)
        for row in connection.execute(
            "SELECT * FROM material_dim WHERE custom_source_id IN (%s)" % placeholders, part
        ):
            materials[integer(row["custom_source_id"])] = dict(row)
    return sources, materials


def blank_scope_audit():
    return {
        "platform_spend_cents": 0,
        "exact_spend_cents": 0,
        "ambiguous_spend_cents": 0,
        "invalid_mapping_spend_cents": 0,
        "out_of_scope_spend_cents": 0,
        "strict_gap_spend_cents": 0,
        "source_row_count": 0,
        "exact_row_count": 0,
        "invalid_row_count": 0,
        "ambiguous_ad_days": 0,
        "af_total": 0,
        "af_mapped": 0,
    }


def normalize_insight_row(source):
    platform = integer(source.get("platform"))
    app = app_key(source.get("app_id"))
    return {
        "id": integer(source.get("id")),
        "dt": validate_date(source.get("dt")),
        "platform": platform,
        "app": app,
        "campaign_id": normalized_id(source.get("campaign_id")),
        "adset_id": normalized_id(source.get("adset_id")),
        "ad_id": normalized_id(source.get("ad_id")),
        "resource_id": integer(source.get("resource_id")),
        "source_id": integer(source.get("source_id")),
        "source_type": integer(source.get("source_type")),
        "resource_type": integer(source.get("resource_type")),
        "resource_name": text(source.get("resource_name")),
        "resource_tag": text(source.get("resource_tag")),
        "spend_cents": cents(source.get("spend")),
        "impressions": integer(source.get("impressions")),
        "clicks": integer(source.get("clicks")),
        "installs": integer(source.get("installs")),
        "auto_publish_dt": text(source.get("auto_publish_dt")),
        "updated_at": text(source.get("updated_at")),
    }


def aggregate_exact_rows(rows, af_by_ad):
    grouped = {}
    for row in rows:
        key = (
            row["dt"],
            row["platform"],
            row["app"],
            row["resource_id"],
            row["ad_id"],
        )
        target = grouped.setdefault(
            key,
            {
                "dt": row["dt"],
                "platform": row["platform"],
                "app": row["app"],
                "custom_source_id": row["resource_id"],
                "campaign_id": row["campaign_id"],
                "adset_id": row["adset_id"],
                "ad_id": row["ad_id"],
                "spend_cents": 0,
                "impressions": 0,
                "clicks": 0,
                "installs": 0,
                "af_d0_count": 0,
                "first_auto_publish_dt": "",
                "resource_tags": set(),
                "source_row_count": 0,
                "mapping_status": "exact",
            },
        )
        target["spend_cents"] += row["spend_cents"]
        target["impressions"] += row["impressions"]
        target["clicks"] += row["clicks"]
        target["installs"] += row["installs"]
        target["source_row_count"] += 1
        if row["resource_tag"]:
            target["resource_tags"].add(row["resource_tag"])
        if row["auto_publish_dt"] and (
            not target["first_auto_publish_dt"]
            or row["auto_publish_dt"] < target["first_auto_publish_dt"]
        ):
            target["first_auto_publish_dt"] = row["auto_publish_dt"]
    for target in grouped.values():
        target["af_d0_count"] = af_by_ad.get(
            (target["platform"], target["app"], target["ad_id"]), 0
        )
    return list(grouped.values())


def process_day(connection, day: str, insight_source_rows, af_rows):
    rows = []
    for source in insight_source_rows:
        row = normalize_insight_row(source)
        if row["platform"] in PLATFORM_CHANNEL and row["app"]:
            rows.append(row)

    source_ids = {row["source_id"] for row in rows if row["platform"] in (0, 3)}
    material_ids = {row["resource_id"] for row in rows if row["platform"] in (0, 3)}
    source_dims, material_dims = load_dim_maps(connection, source_ids, material_ids)

    scope_audits = {
        (platform, app): blank_scope_audit()
        for platform in PLATFORM_CHANNEL
        for app in APP_ORDER
    }
    platform_totals = {
        (platform, app): {
            "spend_cents": 0,
            "impressions": 0,
            "clicks": 0,
            "installs": 0,
            "source_row_count": 0,
        }
        for platform in PLATFORM_CHANNEL
        for app in APP_ORDER
    }
    for row in rows:
        scope = (row["platform"], row["app"])
        total = platform_totals[scope]
        total["spend_cents"] += row["spend_cents"]
        total["impressions"] += row["impressions"]
        total["clicks"] += row["clicks"]
        total["installs"] += row["installs"]
        total["source_row_count"] += 1
        scope_audits[scope]["platform_spend_cents"] += row["spend_cents"]
        scope_audits[scope]["source_row_count"] += 1

    af_by_ad = collections.defaultdict(int)
    af_totals = collections.defaultdict(int)
    for row in af_rows:
        scope = (row["platform"], row["app"])
        af_totals[scope] += row["d0_count"]
        if row["ad_id"]:
            af_by_ad[(row["platform"], row["app"], row["ad_id"])] += row["d0_count"]

    candidates = []
    candidate_ad_materials = collections.defaultdict(set)
    for row in rows:
        scope = (row["platform"], row["app"])
        audit = scope_audits[scope]
        if row["platform"] == 1:
            audit["strict_gap_spend_cents"] += row["spend_cents"]
            continue
        source_dim = source_dims.get(row["source_id"])
        material_dim = material_dims.get(row["resource_id"])
        valid_source = (
            row["source_type"] == 3
            and source_dim
            and integer(source_dim["source_type"]) == 3
            and integer(source_dim["custom_source_id"]) == row["resource_id"]
        )
        if not valid_source or not material_dim or not row["ad_id"]:
            audit["invalid_mapping_spend_cents"] += row["spend_cents"]
            audit["invalid_row_count"] += 1
            continue
        if text(material_dim["product"]).casefold() != "opay":
            audit["out_of_scope_spend_cents"] += row["spend_cents"]
            audit["invalid_row_count"] += 1
            continue
        candidates.append(row)
        candidate_ad_materials[(row["platform"], row["app"], row["ad_id"])].add(
            row["resource_id"]
        )

    ambiguous_keys = {key for key, values in candidate_ad_materials.items() if len(values) > 1}
    for key in ambiguous_keys:
        scope_audits[(key[0], key[1])]["ambiguous_ad_days"] += 1
    exact_rows = []
    for row in candidates:
        scope = (row["platform"], row["app"])
        ad_key = (row["platform"], row["app"], row["ad_id"])
        if ad_key in ambiguous_keys:
            scope_audits[scope]["ambiguous_spend_cents"] += row["spend_cents"]
            continue
        exact_rows.append(row)
        scope_audits[scope]["exact_spend_cents"] += row["spend_cents"]
        scope_audits[scope]["exact_row_count"] += 1

    material_daily = aggregate_exact_rows(exact_rows, af_by_ad)
    for material_row in material_daily:
        scope_audits[(material_row["platform"], material_row["app"])][
            "af_mapped"
        ] += material_row["af_d0_count"]
    for scope, total in af_totals.items():
        if scope in scope_audits:
            scope_audits[scope]["af_total"] = total

    with connection:
        for table in ("platform_daily", "af_daily", "material_daily", "daily_audit"):
            connection.execute("DELETE FROM %s WHERE dt=?" % table, (day,))
        for (platform, app), total in platform_totals.items():
            connection.execute(
                """
                INSERT INTO platform_daily(
                  dt,platform,app,spend_cents,impressions,clicks,installs,source_row_count
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    day,
                    platform,
                    app,
                    total["spend_cents"],
                    total["impressions"],
                    total["clicks"],
                    total["installs"],
                    total["source_row_count"],
                ),
            )
        for row in af_rows:
            connection.execute(
                """
                INSERT OR REPLACE INTO af_daily(
                  dt,platform,app,campaign_id,adset_id,ad_id,d0_count,source_row_count
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    row["dt"],
                    row["platform"],
                    row["app"],
                    row["campaign_id"],
                    row["adset_id"],
                    row["ad_id"],
                    row["d0_count"],
                    row["source_row_count"],
                ),
            )
        for row in material_daily:
            connection.execute(
                """
                INSERT INTO material_daily(
                  dt,platform,app,custom_source_id,campaign_id,adset_id,ad_id,
                  spend_cents,impressions,clicks,installs,af_d0_count,
                  first_auto_publish_dt,resource_tags_json,source_row_count,mapping_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["dt"],
                    row["platform"],
                    row["app"],
                    row["custom_source_id"],
                    row["campaign_id"],
                    row["adset_id"],
                    row["ad_id"],
                    row["spend_cents"],
                    row["impressions"],
                    row["clicks"],
                    row["installs"],
                    row["af_d0_count"],
                    row["first_auto_publish_dt"],
                    json.dumps(sorted(row["resource_tags"]), ensure_ascii=False),
                    row["source_row_count"],
                    row["mapping_status"],
                ),
            )
        for (platform, app), audit in scope_audits.items():
            connection.execute(
                """
                INSERT INTO daily_audit(
                  dt,platform,app,platform_spend_cents,exact_spend_cents,
                  ambiguous_spend_cents,invalid_mapping_spend_cents,
                  out_of_scope_spend_cents,strict_gap_spend_cents,
                  source_row_count,exact_row_count,invalid_row_count,
                  ambiguous_ad_days,af_total,af_mapped
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    day,
                    platform,
                    app,
                    audit["platform_spend_cents"],
                    audit["exact_spend_cents"],
                    audit["ambiguous_spend_cents"],
                    audit["invalid_mapping_spend_cents"],
                    audit["out_of_scope_spend_cents"],
                    audit["strict_gap_spend_cents"],
                    audit["source_row_count"],
                    audit["exact_row_count"],
                    audit["invalid_row_count"],
                    audit["ambiguous_ad_days"],
                    audit["af_total"],
                    audit["af_mapped"],
                ),
            )

    return {
        "day": day,
        "insight_rows": len(rows),
        "af_rows": len(af_rows),
        "exact_material_ad_rows": len(material_daily),
        "ambiguous_ad_days": len(ambiguous_keys),
    }


def refresh_month(month: str, cache_db: Path = DEFAULT_CACHE_DB, *, rebuild=False):
    month = validate_month(month)
    start_date, end_date = month_bounds(month)
    assert_read_only()
    product_config = load_product_config()
    summaries = []
    refreshed_source_ids = set()
    refreshed_material_ids = set()
    with contextlib.closing(cache_conn(cache_db)) as connection:
        for day in each_date(start_date, end_date):
            insight_rows = fetch_insight_day(day)
            ensure_dimensions(
                connection,
                insight_rows,
                # Refresh every material/source dimension once per monthly run.
                # Cached facts are immutable, while maker names, covers and source
                # URLs can be corrected between the day-3 and day-5 snapshots.
                force=True,
                refreshed_source_ids=refreshed_source_ids,
                refreshed_material_ids=refreshed_material_ids,
            )
            af_rows = fetch_af_day(day, product_config)
            summaries.append(process_day(connection, day, insight_rows, af_rows))
        connection.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            ("last_refresh_%s" % month, bj_now().isoformat()),
        )
        connection.commit()
    return {
        "month": month,
        "days": len(summaries),
        "insight_rows": sum(item["insight_rows"] for item in summaries),
        "af_rows": sum(item["af_rows"] for item in summaries),
        "exact_material_ad_rows": sum(item["exact_material_ad_rows"] for item in summaries),
        "ambiguous_ad_days": sum(item["ambiguous_ad_days"] for item in summaries),
    }


def load_keyword_config(path: Path = KEYWORD_CONFIG_PATH):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if integer(document.get("schema_version")) != 1 or not document.get("entries"):
        raise RuntimeError("invalid keyword configuration")
    return document


def load_keyword_overrides(path: Path = KEYWORD_OVERRIDES_PATH):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "overrides": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    if integer(document.get("schema_version")) != 1:
        raise RuntimeError("invalid keyword override configuration")
    if not isinstance(document.get("overrides", {}), dict):
        raise RuntimeError("keyword overrides must be an object")
    return document


def normalize_match_text(value) -> str:
    normalized = unicodedata.normalize("NFKC", text(value)).casefold()
    return " ".join(normalized.split())


def split_exact_tags(values):
    result = set()
    for value in values:
        raw = normalize_match_text(value)
        if not raw:
            continue
        result.add(raw)
        for item in re.split(r"[,;|\n\r]+", raw):
            item = item.strip()
            if item:
                result.add(item)
    return result


def boundary_matches(haystack: str, needle: str):
    if not needle:
        return []
    pattern = re.compile(r"(?<![0-9a-z])%s(?![0-9a-z])" % re.escape(needle))
    return [(match.start(), match.end()) for match in pattern.finditer(haystack)]


def selling_point_status(points):
    if not points:
        return "pending", "待补关键词"
    statuses = {point.get("status") for point in points}
    if statuses == {"available"}:
        return "available", "可用"
    if statuses == {"unavailable"}:
        return "unavailable", "过期/不可用"
    return "mixed", "部分不可用"


def public_point(entry, match_source):
    return {
        "id": entry.get("id"),
        "level1": entry.get("level1") or "未分类",
        "level2": entry.get("level2") or "",
        "keyword": entry.get("display_keyword") or entry.get("upload_tag") or "",
        "status": entry.get("status") or "available",
        "status_label": entry.get("status_label") or "可用",
        "match_source": match_source,
    }


def match_selling_points(
    custom_source_id: int,
    app: str,
    tag_values,
    file_name: str,
    keyword_config,
    override_config,
):
    entries = [entry for entry in keyword_config["entries"] if entry.get("app") == app]
    by_id = {entry.get("id"): entry for entry in entries}
    override = override_config.get("overrides", {}).get(str(custom_source_id))
    if override:
        if isinstance(override, dict) and override.get("selling_point_ids"):
            selected = [by_id[item] for item in override["selling_point_ids"] if item in by_id]
            points = [public_point(entry, "manual") for entry in selected]
        elif isinstance(override, dict):
            points = [
                {
                    "id": "manual-%s" % custom_source_id,
                    "level1": text(override.get("level1")) or "人工覆盖",
                    "level2": text(override.get("level2")),
                    "keyword": text(override.get("keyword")) or "人工覆盖",
                    "status": text(override.get("status")) or "available",
                    "status_label": text(override.get("status_label")) or "人工覆盖",
                    "match_source": "manual",
                }
            ]
        else:
            points = []
        status, label = selling_point_status(points)
        return points, status, label

    exact_tags = split_exact_tags(tag_values)
    exact_entries = []
    for entry in entries:
        aliases = [normalize_match_text(item) for item in entry.get("match_aliases", [])]
        if any(alias and alias in exact_tags for alias in aliases):
            exact_entries.append(entry)
    if exact_entries:
        points = [public_point(entry, "exact_tag") for entry in exact_entries]
        status, label = selling_point_status(points)
        return points, status, label

    texts = [normalize_match_text(value) for value in [*tag_values, file_name] if text(value)]
    alias_entries = collections.defaultdict(list)
    for entry in entries:
        for alias in entry.get("match_aliases", []):
            normalized = normalize_match_text(alias)
            if normalized:
                alias_entries[normalized].append(entry)
    candidates = []
    for text_index, haystack in enumerate(texts):
        for alias in alias_entries:
            for start, end in boundary_matches(haystack, alias):
                candidates.append((-(end - start), text_index, start, end, alias))
    accepted_spans = collections.defaultdict(list)
    accepted_aliases = []
    for _negative_length, text_index, start, end, alias in sorted(candidates):
        if any(not (end <= old_start or start >= old_end) for old_start, old_end in accepted_spans[text_index]):
            continue
        accepted_spans[text_index].append((start, end))
        if alias not in accepted_aliases:
            accepted_aliases.append(alias)
    matched_entries = []
    seen_ids = set()
    for alias in accepted_aliases:
        for entry in alias_entries[alias]:
            if entry.get("id") not in seen_ids:
                matched_entries.append(entry)
                seen_ids.add(entry.get("id"))
    points = [public_point(entry, "boundary_longest") for entry in matched_entries]
    status, label = selling_point_status(points)
    return points, status, label


def ratio(numerator, denominator) -> float:
    return number(numerator / denominator, 8) if denominator else 0.0


def cpa_value(spend_cents: int, d0_count: int):
    if d0_count <= 0:
        return None
    return number((spend_cents / 100.0) / d0_count, 6)


def ctr_strictly_greater(clicks_a, impressions_a, clicks_b, impressions_b):
    if impressions_a <= 0:
        return False
    if impressions_b <= 0:
        return clicks_a > 0
    return clicks_a * impressions_b > clicks_b * impressions_a


def cpa_strictly_lower(spend_a_cents, d0_a, spend_b_cents, d0_b):
    if d0_a <= 0 or d0_b <= 0:
        return False
    return spend_a_cents * d0_b < spend_b_cents * d0_a


def rule_b_qualifies(material, platform_total):
    return integer(material.get("spend_cents")) > 500000 and ctr_strictly_greater(
        integer(material.get("clicks")),
        integer(material.get("impressions")),
        integer(platform_total.get("clicks")),
        integer(platform_total.get("impressions")),
    )


def top_half_members(materials, platform_spend_cents):
    if platform_spend_cents <= 0:
        return set(), {}, {}
    by_spend = collections.defaultdict(list)
    for material in materials:
        by_spend[material["spend_cents"]].append(material)
    top_ids = set()
    rank_map = {}
    cumulative_map = {}
    cumulative = 0
    rank = 0
    crossed = False
    for spend in sorted(by_spend, reverse=True):
        group = sorted(by_spend[spend], key=lambda item: item["custom_source_id"])
        rank += 1
        group_total = sum(item["spend_cents"] for item in group)
        include = not crossed
        cumulative += group_total
        if include:
            top_ids.update(item["custom_source_id"] for item in group)
        for item in group:
            rank_map[item["custom_source_id"]] = rank
            cumulative_map[item["custom_source_id"]] = ratio(cumulative, platform_spend_cents)
        if cumulative * 2 >= platform_spend_cents:
            crossed = True
    return top_ids, rank_map, cumulative_map


def month_aggregates(connection, month: str):
    start_date, end_date = month_bounds(month)
    platform = {}
    for row in connection.execute(
        """
        SELECT platform,app,SUM(spend_cents),SUM(impressions),SUM(clicks),SUM(installs),SUM(source_row_count)
        FROM platform_daily WHERE dt BETWEEN ? AND ? GROUP BY platform,app
        """,
        (start_date, end_date),
    ):
        platform[(integer(row[0]), text(row[1]))] = {
            "spend_cents": integer(row[2]),
            "impressions": integer(row[3]),
            "clicks": integer(row[4]),
            "installs": integer(row[5]),
            "source_row_count": integer(row[6]),
        }
    af = collections.defaultdict(int)
    for row in connection.execute(
        """
        SELECT platform,app,SUM(d0_count) FROM af_daily
        WHERE dt BETWEEN ? AND ? GROUP BY platform,app
        """,
        (start_date, end_date),
    ):
        af[(integer(row[0]), text(row[1]))] = integer(row[2])

    audits = {
        (platform_id, app): blank_scope_audit()
        for platform_id in PLATFORM_CHANNEL
        for app in APP_ORDER
    }
    for row in connection.execute(
        """
        SELECT platform,app,
          SUM(platform_spend_cents),SUM(exact_spend_cents),SUM(ambiguous_spend_cents),
          SUM(invalid_mapping_spend_cents),SUM(out_of_scope_spend_cents),SUM(strict_gap_spend_cents),
          SUM(source_row_count),SUM(exact_row_count),SUM(invalid_row_count),SUM(ambiguous_ad_days),
          SUM(af_total),SUM(af_mapped)
        FROM daily_audit WHERE dt BETWEEN ? AND ? GROUP BY platform,app
        """,
        (start_date, end_date),
    ):
        audits[(integer(row[0]), text(row[1]))] = {
            "platform_spend_cents": integer(row[2]),
            "exact_spend_cents": integer(row[3]),
            "ambiguous_spend_cents": integer(row[4]),
            "invalid_mapping_spend_cents": integer(row[5]),
            "out_of_scope_spend_cents": integer(row[6]),
            "strict_gap_spend_cents": integer(row[7]),
            "source_row_count": integer(row[8]),
            "exact_row_count": integer(row[9]),
            "invalid_row_count": integer(row[10]),
            "ambiguous_ad_days": integer(row[11]),
            "af_total": integer(row[12]),
            "af_mapped": integer(row[13]),
        }

    material = {}
    for row in connection.execute(
        """
        SELECT * FROM material_daily
        WHERE dt BETWEEN ? AND ? AND mapping_status='exact'
        ORDER BY dt,platform,app,custom_source_id,ad_id
        """,
        (start_date, end_date),
    ):
        key = (integer(row["platform"]), text(row["app"]), integer(row["custom_source_id"]))
        target = material.setdefault(
            key,
            {
                "platform": integer(row["platform"]),
                "app": text(row["app"]),
                "custom_source_id": integer(row["custom_source_id"]),
                "spend_cents": 0,
                "impressions": 0,
                "clicks": 0,
                "installs": 0,
                "af_d0_count": 0,
                "source_row_count": 0,
                "ad_days": 0,
                "resource_tags": set(),
                "first_auto_publish_dt": "",
                "first_delivery_dt": text(row["dt"]),
            },
        )
        target["spend_cents"] += integer(row["spend_cents"])
        target["impressions"] += integer(row["impressions"])
        target["clicks"] += integer(row["clicks"])
        target["installs"] += integer(row["installs"])
        target["af_d0_count"] += integer(row["af_d0_count"])
        target["source_row_count"] += integer(row["source_row_count"])
        target["ad_days"] += 1
        target["first_delivery_dt"] = min(target["first_delivery_dt"], text(row["dt"]))
        auto_dt = text(row["first_auto_publish_dt"])
        if auto_dt and (not target["first_auto_publish_dt"] or auto_dt < target["first_auto_publish_dt"]):
            target["first_auto_publish_dt"] = auto_dt
        try:
            target["resource_tags"].update(json.loads(text(row["resource_tags_json"]) or "[]"))
        except json.JSONDecodeError:
            pass
    return platform, af, audits, list(material.values())


def material_dim_map(connection, ids):
    result = {}
    for part in chunked(sorted({integer(item) for item in ids if integer(item) > 0})):
        placeholders = ",".join("?" for _ in part)
        for row in connection.execute(
            "SELECT * FROM material_dim WHERE custom_source_id IN (%s)" % placeholders, part
        ):
            result[integer(row["custom_source_id"])] = dict(row)
    return result


def build_month_payload(
    connection,
    month: str,
    stage: str,
    keyword_config,
    override_config,
):
    month = validate_month(month)
    if stage not in ("initial", "final"):
        raise ValueError("stage must be initial or final")
    platform_totals, af_totals, audit_totals, materials = month_aggregates(connection, month)
    dims = material_dim_map(connection, [item["custom_source_id"] for item in materials])
    rows = []
    benchmarks = []
    audits = []
    for channel_name in CHANNEL_ORDER:
        platform_id = next(key for key, value in PLATFORM_CHANNEL.items() if value == channel_name)
        for app in APP_ORDER:
            scope = (platform_id, app)
            total = platform_totals.get(
                scope,
                {
                    "spend_cents": 0,
                    "impressions": 0,
                    "clicks": 0,
                    "installs": 0,
                    "source_row_count": 0,
                },
            )
            platform_d0 = af_totals.get(scope, 0)
            audit = audit_totals.get(scope, blank_scope_audit())
            coverage = ratio(audit["exact_spend_cents"], total["spend_cents"])
            rule_a_available = total["spend_cents"] > 0 and coverage >= 0.5 and platform_d0 > 0
            scope_materials = [item for item in materials if (item["platform"], item["app"]) == scope]
            top_ids, rank_map, cumulative_map = top_half_members(
                scope_materials, total["spend_cents"]
            )
            selected_count = 0
            for material in scope_materials:
                custom_source_id = material["custom_source_id"]
                dim = dims.get(custom_source_id)
                if not dim or text(dim.get("product")).casefold() != "opay":
                    continue
                in_top_half = rule_a_available and custom_source_id in top_ids
                rule_a = in_top_half and cpa_strictly_lower(
                    material["spend_cents"],
                    material["af_d0_count"],
                    total["spend_cents"],
                    platform_d0,
                )
                rule_b = rule_b_qualifies(material, total)
                if not (rule_a or rule_b):
                    continue
                selected_count += 1
                selection_rule = "A+B" if rule_a and rule_b else ("A" if rule_a else "B")
                tag_values = sorted(material["resource_tags"] | {text(dim.get("tag_name"))})
                points, point_status, point_status_label = match_selling_points(
                    custom_source_id,
                    app,
                    tag_values,
                    text(dim.get("name")),
                    keyword_config,
                    override_config,
                )
                auto_dt = material["first_auto_publish_dt"]
                launch_date = auto_dt or material["first_delivery_dt"]
                launch_source = "platform_success" if auto_dt else "first_delivery"
                rows.append(
                    {
                        "month": month,
                        "channel": channel_name,
                        "app": app,
                        "custom_source_id": custom_source_id,
                        "material_name": text(dim.get("name")),
                        "material_type": {1: "PIC", 2: "VID"}.get(
                            integer(dim.get("material_type")), "UNKNOWN"
                        ),
                        "thumbnail_url": "",
                        "thumbnail_status": "pending",
                        "source_url": "",
                        "source_status": "pending",
                        "source_status_detail": "",
                        "spend": dollars(material["spend_cents"]),
                        "impressions": material["impressions"],
                        "clicks": material["clicks"],
                        "installs": material["installs"],
                        "af_d0_first_transactions": material["af_d0_count"],
                        "maker": text(dim.get("maker")) or "未登记",
                        "maker_source": text(dim.get("maker_source")) or "unresolved",
                        "first_launch_date": launch_date,
                        "first_launch_source": launch_source,
                        "selling_point_level1": "、".join(
                            dict.fromkeys(point["level1"] for point in points)
                        )
                        or "未分类",
                        "selling_point_keywords": "、".join(
                            dict.fromkeys(point["keyword"] for point in points)
                        )
                        or "待补关键词",
                        "selling_points": points,
                        "selling_point_status": point_status,
                        "selling_point_status_label": point_status_label,
                        "selection_rule": selection_rule,
                        "evidence": {
                            "material_ctr": ratio(material["clicks"], material["impressions"]),
                            "material_cpa": cpa_value(
                                material["spend_cents"], material["af_d0_count"]
                            ),
                            "material_cpa_finite": material["af_d0_count"] > 0,
                            "platform_ctr": ratio(total["clicks"], total["impressions"]),
                            "platform_cpa": cpa_value(total["spend_cents"], platform_d0),
                            "platform_cpa_finite": platform_d0 > 0,
                            "spend_rank": rank_map.get(custom_source_id),
                            "cumulative_spend_ratio": cumulative_map.get(custom_source_id, 0),
                            "in_top_50_percent": in_top_half,
                            "rule_a_available": rule_a_available,
                            "rule_a_pass": rule_a,
                            "rule_b_pass": rule_b,
                            "exact_mapping_spend_coverage": coverage,
                            "mapping_status": "exact",
                            "source_row_count": material["source_row_count"],
                            "ad_day_count": material["ad_days"],
                            "data_quality": "严格素材映射；AF 按广告日精确回连",
                        },
                        "_media": {
                            "source_url": text(dim.get("source_url")),
                            "cover_url": text(dim.get("cover_url")),
                            "updated_at": text(dim.get("updated_at")),
                        },
                    }
                )
            benchmark = {
                "month": month,
                "channel": channel_name,
                "app": app,
                "spend": dollars(total["spend_cents"]),
                "impressions": total["impressions"],
                "clicks": total["clicks"],
                "installs": total["installs"],
                "af_d0_first_transactions": platform_d0,
                "ctr": ratio(total["clicks"], total["impressions"]),
                "cpa": cpa_value(total["spend_cents"], platform_d0),
                "cpa_finite": platform_d0 > 0,
            }
            benchmarks.append(benchmark)
            if channel_name == "Google":
                note = (
                    "当前仓库未同时提供可验证完整的素材级 USD 指标与素材级 AF D0 归因；"
                    "V1 严格排除，不做广告组或资产分摊。"
                )
            elif selected_count == 0:
                note = "计算成功，入选0条。"
            else:
                note = "计算成功，入选%d条。" % selected_count
            audits.append(
                {
                    "month": month,
                    "channel": channel_name,
                    "app": app,
                    "status": "success",
                    "message": note,
                    "selected_count": selected_count,
                    "mapping_coverage": coverage,
                    "exact_mapped_spend": dollars(audit["exact_spend_cents"]),
                    "platform_spend": dollars(total["spend_cents"]),
                    "mapping_gap_spend": dollars(
                        max(0, total["spend_cents"] - audit["exact_spend_cents"])
                    ),
                    "rule_a_available": rule_a_available,
                    "ambiguous_spend": dollars(audit["ambiguous_spend_cents"]),
                    "invalid_mapping_spend": dollars(audit["invalid_mapping_spend_cents"]),
                    "out_of_scope_spend": dollars(audit["out_of_scope_spend_cents"]),
                    "strict_gap_spend": dollars(audit["strict_gap_spend_cents"]),
                    "ambiguous_ad_days": audit["ambiguous_ad_days"],
                    "invalid_row_count": audit["invalid_row_count"],
                    "af_mapping_coverage": ratio(audit["af_mapped"], platform_d0),
                    "af_mapped": audit["af_mapped"],
                    "af_total": platform_d0,
                }
            )
    rows.sort(key=lambda item: (-item["spend"], item["channel"], item["app"], item["custom_source_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "month": month,
        "stage": stage,
        "generated_at": bj_now().isoformat(),
        "keyword_config_version": keyword_config.get("config_version"),
        "selection_policy": {
            "rule_a": "累计消耗前50%且素材AF D0首交CPA严格低于平台CPA",
            "rule_b": "素材月消耗严格大于5000 USD且素材CTR严格高于平台CTR",
            "operator": "OR",
            "rule_a_min_mapping_coverage": 0.5,
            "spend_unit": "USD",
        },
        "rows": rows,
        "benchmarks": benchmarks,
        "audits": audits,
        "stage_diff": {},
    }


def allowed_media_suffixes():
    configured = [
        item.strip().casefold()
        for item in os.environ.get(
            "OPAY_REPORT_MEDIA_ALLOWED_SUFFIXES", ",".join(DEFAULT_MEDIA_HOST_SUFFIXES)
        ).split(",")
        if item.strip()
    ]
    return tuple(configured)


def normalize_media_url(value):
    raw = text(value)
    if not raw:
        return "", "missing"
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "", "invalid_url"
    if parsed.scheme.casefold() not in ("http", "https") or not parsed.hostname:
        return "", "unsafe_url"
    try:
        port = parsed.port
    except ValueError:
        return "", "invalid_url"
    if parsed.username or parsed.password or port not in (None, 80, 443):
        return "", "unsafe_url"
    hostname = parsed.hostname.casefold().rstrip(".")
    suffixes = allowed_media_suffixes()
    if not any(hostname.endswith(suffix) for suffix in suffixes):
        return "", "unsafe_host"
    normalized = parsed._replace(scheme="https", netloc=hostname).geturl()
    return normalized, "safe"


def probe_url(url: str):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "OPayReport/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=MEDIA_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 400:
                return "available", "HTTP %s" % status
            return "unavailable", "HTTP %s" % status
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 501):
            fallback = urllib.request.Request(
                url,
                method="GET",
                headers={"User-Agent": "OPayReport/1.0", "Range": "bytes=0-0"},
            )
            try:
                with urllib.request.urlopen(fallback, timeout=MEDIA_TIMEOUT_SECONDS) as response:
                    return "available", "HTTP %s" % getattr(response, "status", 200)
            except Exception as fallback_exc:  # noqa: BLE001 - row degradation
                return "unavailable", type(fallback_exc).__name__
        return "unavailable", "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - row degradation
        return "unavailable", type(exc).__name__


def download_limited(url: str, destination: Path):
    request = urllib.request.Request(url, headers={"User-Agent": "OPayReport/1.0"})
    total = 0
    with urllib.request.urlopen(request, timeout=MEDIA_TIMEOUT_SECONDS) as response:
        length = integer(response.headers.get("Content-Length"))
        if length and length > MEDIA_MAX_BYTES:
            raise RuntimeError("media exceeds byte limit")
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MEDIA_MAX_BYTES:
                    raise RuntimeError("media exceeds byte limit")
                handle.write(chunk)
    if total <= 0:
        raise RuntimeError("empty media response")


def image_suffix(path: Path):
    header = path.read_bytes()[:16]
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ""


def build_thumbnail(row, data_root: Path):
    media = row.get("_media", {})
    material_type = row.get("material_type")
    source_url, source_reason = normalize_media_url(media.get("source_url"))
    cover_url, _cover_reason = normalize_media_url(media.get("cover_url"))
    if not source_url:
        row["source_status"] = "unsafe" if source_reason.startswith("unsafe") else "unavailable"
        row["source_status_detail"] = source_reason
    else:
        row["source_url"] = source_url
        row["source_status"], row["source_status_detail"] = probe_url(source_url)

    thumbnail_input = source_url if material_type == "PIC" else (cover_url or source_url)
    if not thumbnail_input:
        row["thumbnail_status"] = "unavailable"
        row.pop("_media", None)
        return row
    cache_key = hashlib.sha256(
        (thumbnail_input + "\u0000" + text(media.get("updated_at"))).encode("utf-8")
    ).hexdigest()[:12]
    base_name = "%s-%s" % (row["custom_source_id"], cache_key)
    thumbnail_root = Path(data_root) / "thumbnails"
    thumbnail_root.mkdir(parents=True, exist_ok=True)
    for suffix in (".jpg", ".png", ".webp", ".gif"):
        existing = thumbnail_root / (base_name + suffix)
        if existing.exists() and existing.stat().st_size > 0:
            row["thumbnail_url"] = PUBLIC_URL + "assets/thumbnails/" + existing.name
            row["thumbnail_status"] = "available"
            row.pop("_media", None)
            return row

    ffmpeg = shutil.which("ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="opay-thumb-") as temporary:
            temporary_dir = Path(temporary)
            output_jpg = temporary_dir / "thumbnail.jpg"
            if material_type == "VID" and not cover_url:
                if not ffmpeg:
                    raise RuntimeError("ffmpeg_missing_for_video_frame")
                process = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        "1",
                        "-i",
                        thumbnail_input,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale='min(640,iw)':-2",
                        str(output_jpg),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=max(20, MEDIA_TIMEOUT_SECONDS * 3),
                )
                if process.returncode or not output_jpg.exists():
                    raise RuntimeError("video_frame_failed")
            else:
                source_file = temporary_dir / "source-media"
                download_limited(thumbnail_input, source_file)
                if ffmpeg:
                    process = subprocess.run(
                        [
                            ffmpeg,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(source_file),
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale='min(640,iw)':-2",
                            str(output_jpg),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=max(20, MEDIA_TIMEOUT_SECONDS * 2),
                    )
                    if process.returncode:
                        output_jpg.unlink(missing_ok=True)
                if not output_jpg.exists():
                    suffix = image_suffix(source_file)
                    if not suffix:
                        raise RuntimeError("unsupported_image")
                    fallback = temporary_dir / ("thumbnail" + suffix)
                    shutil.copy2(source_file, fallback)
                    output_jpg = fallback
            suffix = output_jpg.suffix.casefold() or ".jpg"
            final_path = thumbnail_root / (base_name + suffix)
            temporary_path = thumbnail_root / (".%s-%s.tmp" % (base_name, os.getpid()))
            shutil.copy2(output_jpg, temporary_path)
            os.replace(temporary_path, final_path)
            row["thumbnail_url"] = PUBLIC_URL + "assets/thumbnails/" + final_path.name
            row["thumbnail_status"] = "available"
    except Exception as exc:  # noqa: BLE001 - row degradation is intentional
        row["thumbnail_status"] = "unavailable"
        row["thumbnail_error"] = type(exc).__name__
    row.pop("_media", None)
    return row


def prepare_media(rows, data_root: Path, *, enabled=True):
    if not enabled:
        for row in rows:
            source_url, source_reason = normalize_media_url(row.get("_media", {}).get("source_url"))
            row["source_url"] = source_url
            row["source_status"] = "unchecked" if source_url else "unavailable"
            row["source_status_detail"] = source_reason
            row["thumbnail_status"] = "unchecked"
            row.pop("_media", None)
        return rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=MEDIA_WORKERS) as executor:
        return list(executor.map(lambda row: build_thumbnail(row, data_root), rows))


def atomic_write(path: Path, content, *, binary=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".%s.%s.tmp" % (path.name, os.getpid()))
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8", "newline": "\n"}
    with temporary.open(mode, **kwargs) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def json_bytes(document, *, pretty=False):
    if pretty:
        value = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    else:
        value = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    return value.encode("utf-8")


def sha256_bytes(value: bytes):
    return hashlib.sha256(value).hexdigest()


def load_snapshot(path: Path, expected_sha=""):
    value = Path(path).read_bytes()
    if expected_sha and sha256_bytes(value) != expected_sha:
        raise RuntimeError("snapshot checksum mismatch: %s" % path)
    return json.loads(value.decode("utf-8"))


def compute_stage_diff(base, current):
    if not base:
        return {
            "base_stage": None,
            "added_count": len(current.get("rows", [])),
            "removed_count": 0,
            "changed_count": 0,
            "added_ids": [row["custom_source_id"] for row in current.get("rows", [])],
            "removed_ids": [],
        }
    base_rows = {
        (row["channel"], row["app"], integer(row["custom_source_id"])): row
        for row in base.get("rows", [])
    }
    current_rows = {
        (row["channel"], row["app"], integer(row["custom_source_id"])): row
        for row in current.get("rows", [])
    }
    added_keys = sorted(set(current_rows) - set(base_rows))
    removed_keys = sorted(set(base_rows) - set(current_rows))
    changed = 0
    for key in set(base_rows) & set(current_rows):
        fields = ("spend", "impressions", "clicks", "installs", "af_d0_first_transactions", "selection_rule")
        if any(base_rows[key].get(field) != current_rows[key].get(field) for field in fields):
            changed += 1
    return {
        "base_stage": base.get("stage"),
        "added_count": len(added_keys),
        "removed_count": len(removed_keys),
        "changed_count": changed,
        "added_ids": [key[2] for key in added_keys],
        "removed_ids": [key[2] for key in removed_keys],
    }


def current_month_state(connection, month: str, stage: str):
    row = connection.execute(
        "SELECT * FROM month_state WHERE month=? AND stage=?", (month, stage)
    ).fetchone()
    return dict(row) if row else None


def base_snapshot_for_stage(connection, month: str, stage: str):
    preferred = "initial" if stage == "final" else stage
    state = current_month_state(connection, month, preferred)
    if not state and stage == "final":
        state = current_month_state(connection, month, "final")
    if not state:
        return None
    return load_snapshot(Path(state["snapshot_path"]), state["snapshot_sha256"])


def save_month_snapshot(
    month: str,
    stage: str,
    *,
    cache_db: Path = DEFAULT_CACHE_DB,
    data_root: Path = DEFAULT_DATA_ROOT,
    rebuild=False,
    media_enabled=True,
):
    month = validate_month(month)
    if stage not in ("initial", "final"):
        raise ValueError("invalid stage")
    data_root = Path(data_root)
    with contextlib.closing(cache_conn(cache_db)) as connection:
        existing = current_month_state(connection, month, stage)
        final_existing = current_month_state(connection, month, "final")
        if existing and not rebuild:
            return {"month": month, "stage": stage, **existing, "status": "skipped_frozen"}
        if stage == "initial" and final_existing and not rebuild:
            return {
                "month": month,
                "stage": stage,
                **final_existing,
                "status": "skipped_final_exists",
            }
        keyword_config = load_keyword_config()
        overrides = load_keyword_overrides()
        base = base_snapshot_for_stage(connection, month, stage)
        payload = build_month_payload(connection, month, stage, keyword_config, overrides)
        payload["rows"] = prepare_media(payload["rows"], data_root, enabled=media_enabled)
        payload["stage_diff"] = compute_stage_diff(base, payload)
        generated_at = payload["generated_at"]
        version = bj_now().strftime("%Y%m%dT%H%M%S%f%z")
        snapshot_path = data_root / "snapshots" / month / stage / (version + ".json")
        content = json_bytes(payload, pretty=True)
        atomic_write(snapshot_path, content, binary=True)
        digest = sha256_bytes(content)
        connection.execute(
            """
            INSERT OR REPLACE INTO month_state(
              month,stage,generated_at,snapshot_path,snapshot_sha256,row_count,status,diff_json,data_version
            ) VALUES(?,?,?,?,?,?,?,?,COALESCE((SELECT data_version FROM month_state WHERE month=? AND stage=?),''))
            """,
            (
                month,
                stage,
                generated_at,
                str(snapshot_path),
                digest,
                len(payload["rows"]),
                "success",
                json.dumps(payload["stage_diff"], ensure_ascii=False),
                month,
                stage,
            ),
        )
        connection.commit()
    return {
        "month": month,
        "stage": stage,
        "status": "success",
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": digest,
        "row_count": len(payload["rows"]),
    }


def visible_states(connection):
    rows = [dict(row) for row in connection.execute("SELECT * FROM month_state WHERE status='success'")]
    by_month = collections.defaultdict(dict)
    for row in rows:
        by_month[row["month"]][row["stage"]] = row
    result = []
    for month in sorted(by_month):
        state = by_month[month].get("final") or by_month[month].get("initial")
        if state and month >= REPORT_START_MONTH:
            result.append(state)
    return result


def report_html():
    path = ROOT / "report.html"
    if not path.exists():
        raise RuntimeError("report.html is missing")
    return path.read_text(encoding="utf-8")


def validate_storage_root(data_root: Path):
    data_root = Path(data_root)
    if str(data_root).startswith("/mnt/data-disk") and not Path("/mnt/data-disk").is_mount():
        raise RuntimeError("/mnt/data-disk is not a mounted filesystem")
    data_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(data_root).free
    if free < 1024 * 1024 * 1024:
        raise RuntimeError("less than 1 GiB free under data root")


def copy_thumbnail_assets(payloads, data_root: Path, output_dir: Path):
    source_root = Path(data_root) / "thumbnails"
    target_root = Path(output_dir) / "assets" / "thumbnails"
    target_root.mkdir(parents=True, exist_ok=True)
    names = set()
    prefix = PUBLIC_URL + "assets/thumbnails/"
    for payload in payloads:
        for row in payload.get("rows", []):
            url = text(row.get("thumbnail_url"))
            if url.startswith(prefix):
                name = url[len(prefix) :]
                if name and "/" not in name and "\\" not in name:
                    names.add(name)
    for name in sorted(names):
        source = source_root / name
        if not source.exists() or source.stat().st_size <= 0:
            raise RuntimeError("referenced thumbnail is missing: %s" % name)
        target = target_root / name
        if target.exists() and target.stat().st_size == source.stat().st_size:
            continue
        temporary = target_root / (".%s.%s.tmp" % (name, os.getpid()))
        shutil.copy2(source, temporary)
        os.replace(temporary, target)


def publish_visible_state(
    *,
    cache_db: Path = DEFAULT_CACHE_DB,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_dir: Path = DEFAULT_WEB_DIR,
):
    validate_storage_root(data_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(cache_conn(cache_db)) as connection:
        states = visible_states(connection)
        if not states:
            raise RuntimeError("no successful month snapshots to publish")
        payloads = [
            load_snapshot(Path(state["snapshot_path"]), state["snapshot_sha256"])
            for state in states
        ]
        data_version = bj_now().strftime("%Y%m%dT%H%M%S%f%z")
        if not SAFE_VERSION_PATTERN.fullmatch(data_version):
            raise RuntimeError("invalid generated data version")
        data_root_dir = output_dir / "data"
        data_root_dir.mkdir(parents=True, exist_ok=True)
        temporary_dir = data_root_dir / (".%s.tmp" % data_version)
        final_dir = data_root_dir / data_version
        if temporary_dir.exists() or final_dir.exists():
            raise RuntimeError("data version collision")
        temporary_dir.mkdir(parents=True)
        try:
            month_entries = []
            for state, payload in zip(states, payloads):
                public_payload = dict(payload)
                public_payload["data_version"] = data_version
                content = json_bytes(public_payload, pretty=False)
                atomic_write(temporary_dir / (state["month"] + ".json"), content, binary=True)
                month_entries.append(
                    {
                        "month": state["month"],
                        "stage": state["stage"],
                        "generated_at": state["generated_at"],
                        "row_count": state["row_count"],
                        "status": state["status"],
                        "snapshot_sha256": state["snapshot_sha256"],
                    }
                )
            os.replace(temporary_dir, final_dir)
            copy_thumbnail_assets(payloads, data_root, output_dir)
            atomic_write(output_dir / "index.html", report_html())
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "data_version": data_version,
                "generated_at": bj_now().isoformat(),
                "latest_month": month_entries[-1]["month"],
                "months": month_entries,
                "report_url": PUBLIC_URL,
                "access": "public_no_auth",
                "robots": "noindex,nofollow",
            }
            manifest_content = json_bytes(manifest, pretty=False)
            atomic_write(output_dir / "latest.json", manifest_content, binary=True)
            manifest_digest = sha256_bytes(manifest_content)
            try:
                connection.execute(
                    """
                    INSERT INTO publish_audit(
                      data_version,generated_at,month_count,manifest_sha256,manifest_json
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        data_version,
                        manifest["generated_at"],
                        len(month_entries),
                        manifest_digest,
                        manifest_content.decode("utf-8"),
                    ),
                )
                for state in states:
                    connection.execute(
                        "UPDATE month_state SET data_version=? WHERE month=? AND stage=?",
                        (data_version, state["month"], state["stage"]),
                    )
                connection.commit()
            except sqlite3.Error:
                pass
            return {
                "data_version": data_version,
                "generated_at": manifest["generated_at"],
                "month_count": len(month_entries),
                "latest_month": manifest["latest_month"],
                "manifest_sha256": manifest_digest,
            }
        except Exception:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir, ignore_errors=True)
            raise


def check_cache(cache_db: Path = DEFAULT_CACHE_DB):
    with contextlib.closing(cache_conn(cache_db)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        counts = {}
        for table in (
            "ads_source_dim",
            "material_dim",
            "platform_daily",
            "af_daily",
            "material_daily",
            "daily_audit",
            "month_state",
            "publish_audit",
        ):
            counts[table] = integer(connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
        date_bounds = connection.execute("SELECT MIN(dt),MAX(dt) FROM platform_daily").fetchone()
        states = [dict(row) for row in connection.execute("SELECT * FROM month_state ORDER BY month,stage")]
    return {
        "quick_check": quick,
        "counts": counts,
        "date_start": text(date_bounds[0]),
        "date_end": text(date_bounds[1]),
        "month_states": states,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--month")
    scope.add_argument("--latest-complete-month", action="store_true")
    scope.add_argument("--backfill", action="store_true")
    parser.add_argument("--from-month", default=REPORT_START_MONTH)
    parser.add_argument("--to-month")
    parser.add_argument("--stage", choices=("initial", "final"), default="final")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--check-cache", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--skip-media-checks", action="store_true")
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_WEB_DIR)
    return parser.parse_args(argv)


def requested_months(args):
    if args.month:
        return [validate_month(args.month)]
    if args.latest_complete_month:
        return [latest_complete_month()]
    if args.backfill:
        end_month = validate_month(args.to_month or latest_complete_month())
        return list(each_month(args.from_month, end_month))
    if args.refresh or args.publish:
        raise SystemExit("choose --month, --latest-complete-month, or --backfill")
    return []


def main(argv=None):
    args = parse_args(argv)
    if not any((args.refresh, args.publish, args.check_cache)):
        raise SystemExit("choose --refresh, --publish, or --check-cache")
    validate_storage_root(args.data_root)
    result = {"schema_version": SCHEMA_VERSION}
    if args.check_cache:
        result["cache"] = check_cache(args.cache_db)
    months = requested_months(args)
    generated = []
    for month in months:
        with contextlib.closing(cache_conn(args.cache_db)) as connection:
            existing = current_month_state(connection, month, args.stage)
            final_existing = current_month_state(connection, month, "final")
        frozen = existing or (args.stage == "initial" and final_existing)
        if frozen and not args.rebuild:
            generated.append(
                {
                    "month": month,
                    "stage": args.stage,
                    "status": "skipped_frozen",
                }
            )
            continue
        month_result = {"month": month, "stage": args.stage}
        if args.refresh:
            month_result["refresh"] = refresh_month(month, args.cache_db, rebuild=args.rebuild)
        month_result["snapshot"] = save_month_snapshot(
            month,
            args.stage,
            cache_db=args.cache_db,
            data_root=args.data_root,
            rebuild=args.rebuild,
            media_enabled=not args.skip_media_checks,
        )
        generated.append(month_result)
    if generated:
        result["months"] = generated
    if args.publish:
        result["publish"] = publish_visible_state(
            cache_db=args.cache_db,
            data_root=args.data_root,
            output_dir=args.output_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

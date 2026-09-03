#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synchronize TikTok mini-program bid-protection history into ads_ai."""

from __future__ import print_function

import argparse
import collections
import concurrent.futures
import csv
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests


HISTORY_URL = "https://business-api.tiktok.com/open_api/v1.3/report/bid_protection/detail/get/"
STATUS_URL = "https://business-api.tiktok.com/open_api/v1.3/report/bid_protection/status/get/"
INTEGRATED_REPORT_URL = "https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/"

TOKEN_DB = "/root/codex_test/tt_business_api_tokens.sqlite3"
TOKEN_KEY = "native_growth_default"
TARGET_TABLE = "ads_ai.ads_tiktok_minis_bid_protection_daily"
DEFAULT_RETRY_STATE = "/mnt/data-disk/tt-minis-bid-protection/state/failed_requests.json"
READ_PORT = 63350
WRITE_PORT = 63353
API_BATCH_SIZE = 200
SOURCE_METADATA_CHUNK_SIZE = 2000
WRITE_BATCH_SIZE = 10000
WRITE_MAX_STATEMENT_BYTES = 4 * 1024 * 1024
DEFAULT_WORKERS = 6
DEFAULT_TIMEOUT = 45
DEFAULT_BACKFILL_DAYS = 60

NON_TERMINAL_STATUSES = ("UNDER_PROTECTION", "CONFIRMING")
TERMINAL_STATUSES = ("INELIGIBLE", "PAYMENT_COMPLETE", "TARGET_MET")
VALID_DAILY_STATUSES = set(NON_TERMINAL_STATUSES + TERMINAL_STATUSES)
VALID_DATA_LEVELS = ("CAMPAIGN", "ADGROUP")
RETRYABLE_API_CODES = {40016, 40100, 40133, 50000, 50002, 60001}
LEVEL_CONFIG = {
    "CAMPAIGN": {"category": 0, "object_column": "campaign_id", "index": "campaign_id"},
    "ADGROUP": {"category": 1, "object_column": "adset_id", "index": "adset_id"},
}

_MYSQL_COMMAND_PROVIDER = None
_THREAD_LOCAL = threading.local()


class SyncError(RuntimeError):
    pass


class ApiError(SyncError):
    def __init__(self, code, message, request_id="", retryable=False):
        self.code = code
        self.request_id = str(request_id or "")
        self.retryable = bool(retryable)
        super(ApiError, self).__init__(
            "TikTok API failed code=%s request_id=%s message=%s"
            % (code, self.request_id or "<empty>", str(message or "")[:300])
        )


def emit(event, **fields):
    payload = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def parse_day(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise SyncError("invalid date: %s" % value)


def format_day(value):
    return value.strftime("%Y-%m-%d")


def beijing_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date()


def each_day(start_date, end_date):
    start = parse_day(start_date)
    end = parse_day(end_date)
    if start > end:
        raise SyncError("start date is after end date")
    current = start
    while current <= end:
        yield format_day(current)
        current += timedelta(days=1)


def chunks(values, size=API_BATCH_SIZE):
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def sql_quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_in(values):
    cleaned = [str(value) for value in values if str(value).strip()]
    if not cleaned:
        return "(NULL)"
    return "(" + ",".join(sql_quote(value) for value in cleaned) + ")"


def normalize_id(value, field_name="id"):
    text = str(value or "").strip()
    if not text or not re.match(r"^[0-9]{1,32}$", text):
        raise SyncError("invalid %s" % field_name)
    return text


def safe_text(value, max_bytes=60000):
    text = str(value or "")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def parse_scaled_credit(value):
    text = str(value if value is not None else "0").strip() or "0"
    try:
        scaled = Decimal(text)
    except InvalidOperation:
        raise SyncError("invalid credit_amount")
    if scaled != scaled.to_integral_value():
        raise SyncError("credit_amount is not an integer-scaled value")
    normalized = scaled / Decimal("100000")
    return scaled.quantize(Decimal("1")), normalized.quantize(Decimal("0.00001"))


def load_access_token(path=TOKEN_DB, token_key=TOKEN_KEY):
    uri = "file:%s?mode=ro" % path
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        row = conn.execute(
            "SELECT access_token FROM tt_business_api_tokens "
            "WHERE token_key = ? AND status = 1 LIMIT 1",
            (token_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not str(row[0] or "").strip():
        raise SyncError("active TikTok token was not found for key=%s" % token_key)
    return str(row[0]).strip()


def default_mysql_command():
    for path in ("/root/codex_test", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        base = importlib.import_module("opera_product_daily_dashboard")
    except ImportError as exc:
        raise SyncError("cannot import the server MySQL command provider") from exc
    return list(base.mysql_cmd())


def mysql_command_provider():
    provider = _MYSQL_COMMAND_PROVIDER or default_mysql_command
    return list(provider())


def mysql_cli_command(port=READ_PORT):
    raw = mysql_command_provider()
    safe = []
    secrets = []
    env = os.environ.copy()
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
        if arg in ("-P", "--port") and index + 1 < len(raw):
            index += 2
            continue
        if arg.startswith("--port=") or (arg.startswith("-P") and len(arg) > 2):
            index += 1
            continue
        if arg in ("-e", "--execute") or arg.startswith("--execute="):
            index += 1
            continue
        safe.append(arg)
        index += 1
    inherited = env.get("MYSQL_PWD")
    if inherited:
        secrets.append(inherited)
    if secrets:
        env["MYSQL_PWD"] = secrets[-1]
    safe.extend(["-P", str(int(port)), "-e"])
    return safe, env, tuple(secret for secret in secrets if secret)


def redact_error(value, secrets=()):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    text = re.sub(r"(?i)--password(?:=|\s+)\S+", "--password=<redacted>", text)
    text = re.sub(r"(?i)(?<!\S)-p\S+", "-p<redacted>", text)
    return " ".join(text.split())[:500]


def run_mysql_query(sql, timeout=180):
    command, env, secrets = mysql_cli_command(READ_PORT)
    compact_sql = " ".join(str(sql).split())
    try:
        proc = subprocess.run(
            command + [compact_sql],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise SyncError("read-only MySQL query timed out after %ss" % timeout)
    if proc.returncode:
        raise SyncError(
            "read-only MySQL query failed returncode=%s stderr=%s"
            % (proc.returncode, redact_error(proc.stderr, secrets) or "<empty>")
        )
    return list(csv.reader(proc.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))


def mysql_connection_settings(port=WRITE_PORT):
    raw = mysql_command_provider()
    settings = {"host": None, "user": None, "password": os.environ.get("MYSQL_PWD"), "port": int(port)}
    index = 0
    while index < len(raw):
        arg = str(raw[index])
        if arg in ("-h", "--host") and index + 1 < len(raw):
            settings["host"] = str(raw[index + 1])
            index += 2
            continue
        if arg.startswith("--host="):
            settings["host"] = arg.split("=", 1)[1]
        elif arg.startswith("-h") and len(arg) > 2:
            settings["host"] = arg[2:]
        elif arg in ("-u", "--user") and index + 1 < len(raw):
            settings["user"] = str(raw[index + 1])
            index += 2
            continue
        elif arg.startswith("--user="):
            settings["user"] = arg.split("=", 1)[1]
        elif arg.startswith("-u") and len(arg) > 2:
            settings["user"] = arg[2:]
        elif arg in ("-p", "--password") and index + 1 < len(raw):
            settings["password"] = str(raw[index + 1])
            index += 2
            continue
        elif arg.startswith("--password="):
            settings["password"] = arg.split("=", 1)[1]
        elif arg.startswith("-p") and len(arg) > 2:
            settings["password"] = arg[2:]
        index += 1
    if not settings["host"] or not settings["user"] or settings["password"] is None:
        raise SyncError("could not derive MySQL connection settings from server provider")
    return settings


def fetch_insight_candidates(day, data_level):
    level = str(data_level).upper()
    if level not in VALID_DATA_LEVELS:
        raise SyncError("unsupported data level: %s" % data_level)
    parse_day(day)
    category = LEVEL_CONFIG[level]["category"]
    adgroup_select = "CAST(adgroup_id AS CHAR)" if level == "ADGROUP" else "''"
    adgroup_group = ", adgroup_id" if level == "ADGROUP" else ""
    adgroup_filter = "AND adgroup_id <> 0" if level == "ADGROUP" else ""
    sql = """
    SELECT
      CAST(advertiser_id AS CHAR) AS advertiser_id,
      CAST(campaign_id AS CHAR) AS campaign_id,
      {adgroup_select} AS adgroup_id,
      ROUND(SUM(stat_cost), 6) AS spend
    FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX (dt)
    WHERE dt = {day}
      AND category = {category}
      AND advertiser_id <> ''
      AND campaign_id <> 0
      {adgroup_filter}
    GROUP BY advertiser_id, campaign_id{adgroup_group}
    HAVING SUM(stat_cost) > 0
    """.format(
        adgroup_select=adgroup_select,
        category=category,
        day=sql_quote(day),
        adgroup_filter=adgroup_filter,
        adgroup_group=adgroup_group,
    )
    candidates = []
    for row in run_mysql_query(sql, timeout=300):
        if len(row) != 4:
            continue
        advertiser_id, campaign_id, adgroup_id, spend = row
        try:
            advertiser_id = normalize_id(advertiser_id, "advertiser_id")
            campaign_id = normalize_id(campaign_id, "campaign_id")
            if level == "ADGROUP":
                adgroup_id = normalize_id(adgroup_id, "adgroup_id")
        except SyncError:
            continue
        candidates.append(
            {
                "record_date": day,
                "advertiser_id": advertiser_id,
                "data_level": level,
                "query_id": campaign_id if level == "CAMPAIGN" else adgroup_id,
                "campaign_id": campaign_id,
                "adgroup_id": adgroup_id if level == "ADGROUP" else None,
                "source_adgroup_id": adgroup_id or "",
                "spend": str(spend or "0"),
            }
        )
    return candidates


def scope_metadata_sql(object_ids, day, data_level):
    level = str(data_level).upper()
    config = LEVEL_CONFIG[level]
    object_column = config["object_column"]
    index_hint = config["index"]
    cutoff = format_day(parse_day(day) + timedelta(days=1))
    return """
    SELECT
      CAST(ac.ad_account_id AS CHAR) AS advertiser_id,
      CAST(ac.campaign_id AS CHAR) AS campaign_id,
      CAST(ac.adset_id AS CHAR) AS adgroup_id,
      CAST(ac.product_id AS CHAR) AS product_id,
      COALESCE(NULLIF(TRIM(q.show_name), ''), CONCAT('product_', CAST(ac.product_id AS CHAR))) AS product_name,
      TRIM(q.minis_id) AS minis_id
    FROM kunlunads_dev.ads_tiktok_auto_created_data ac FORCE INDEX ({index_hint})
    STRAIGHT_JOIN kunlunads_dev.tiktok_publish_template_queue q
     ON q.id = ac.publish_queue_id
     AND q.minis_id IS NOT NULL
     AND TRIM(q.minis_id) <> ''
     AND TRIM(q.product) REGEXP '^[0-9]+$'
     AND CAST(TRIM(q.product) AS UNSIGNED) = ac.product_id
    WHERE ac.{object_column} IN {object_ids}
      AND ac.{object_column} IS NOT NULL
      AND ac.created_at < {cutoff}
    ORDER BY ac.ad_account_id, ac.{object_column}, ac.created_at DESC, ac.id DESC
    """.format(
        object_column=object_column,
        index_hint=index_hint,
        object_ids=sql_in(object_ids),
        cutoff=sql_quote(cutoff),
    )


def attach_minis_metadata(candidates, day, data_level, metadata_cache=None):
    if not candidates:
        return []
    cache = metadata_cache if metadata_cache is not None else {}
    unresolved = [
        row
        for row in candidates
        if (data_level, row["advertiser_id"], row["query_id"]) not in cache
    ]
    object_ids = sorted({row["query_id"] for row in unresolved})
    for part in chunks(object_ids, SOURCE_METADATA_CHUNK_SIZE):
        for raw in run_mysql_query(scope_metadata_sql(part, day, data_level), timeout=180):
            if len(raw) != 6:
                continue
            advertiser_id, campaign_id, adgroup_id, product_id, product_name, minis_id = raw
            try:
                advertiser_id = normalize_id(advertiser_id, "advertiser_id")
                campaign_id = normalize_id(campaign_id, "campaign_id")
                adgroup_id = normalize_id(adgroup_id, "adgroup_id")
                if not str(product_id).isdigit() or not str(minis_id).strip():
                    continue
            except SyncError:
                continue
            query_id = campaign_id if data_level == "CAMPAIGN" else adgroup_id
            cache.setdefault((data_level, advertiser_id, query_id), {
                "product_id": int(product_id),
                "product_name": safe_text(product_name, 128),
                "minis_id": safe_text(minis_id, 64),
                "campaign_id": campaign_id,
                "source_adgroup_id": adgroup_id,
            })
    # Object ownership is immutable in TikTok.  Cache negative lookups for this
    # run as well so a 60-day backfill does not repeatedly re-scan unrelated
    # non-Minis campaigns and ad groups.
    for candidate in unresolved:
        cache.setdefault((data_level, candidate["advertiser_id"], candidate["query_id"]), None)
    scoped = []
    for candidate in candidates:
        meta = cache.get((data_level, candidate["advertiser_id"], candidate["query_id"]))
        if not meta:
            continue
        item = dict(candidate)
        item.update(meta)
        item["adgroup_id"] = item["query_id"] if data_level == "ADGROUP" else None
        scoped.append(item)
    return scoped


def build_day_candidates(day, data_level, metadata_cache=None, metadata_as_of=None):
    raw = fetch_insight_candidates(day, data_level)
    return attach_minis_metadata(raw, metadata_as_of or day, data_level, metadata_cache)


def fetch_pending_candidates(start_date, end_date):
    sql = """
    SELECT
      DATE_FORMAT(record_date, '%Y-%m-%d'),
      CAST(product_id AS CHAR), product_name, minis_id,
      advertiser_id, data_level, query_id,
      COALESCE(campaign_id, ''), COALESCE(adgroup_id, '')
    FROM {table}
    WHERE record_date BETWEEN {start_date} AND {end_date}
      AND protection_status IN ('UNDER_PROTECTION', 'CONFIRMING')
    ORDER BY record_date, advertiser_id, data_level, query_id
    """.format(
        table=TARGET_TABLE,
        start_date=sql_quote(start_date),
        end_date=sql_quote(end_date),
    )
    out = []
    for row in run_mysql_query(sql, timeout=180):
        if len(row) != 9:
            continue
        record_date, product_id, product_name, minis_id, advertiser_id, data_level, query_id, campaign_id, adgroup_id = row
        if data_level not in VALID_DATA_LEVELS:
            continue
        try:
            item = {
                "record_date": format_day(parse_day(record_date)),
                "product_id": int(product_id),
                "product_name": safe_text(product_name, 128),
                "minis_id": safe_text(minis_id, 64),
                "advertiser_id": normalize_id(advertiser_id, "advertiser_id"),
                "data_level": data_level,
                "query_id": normalize_id(query_id, "query_id"),
                "campaign_id": normalize_id(campaign_id, "campaign_id") if campaign_id else None,
                "adgroup_id": normalize_id(adgroup_id, "adgroup_id") if adgroup_id else None,
                "source_adgroup_id": adgroup_id or "",
            }
        except (SyncError, ValueError):
            continue
        out.append(item)
    return out


def merge_candidates(*candidate_sets):
    merged = {}
    for candidate_set in candidate_sets:
        for item in candidate_set:
            key = (item["record_date"], item["advertiser_id"], item["data_level"], item["query_id"])
            merged[key] = dict(item)
    return list(merged.values())


def candidate_key(item):
    return (item["record_date"], item["advertiser_id"], item["data_level"], item["query_id"])


def load_retry_candidates(path, earliest_day, latest_day):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SyncError("failed to read retry state: %s" % type(exc).__name__)
    if not isinstance(payload, list):
        raise SyncError("retry state must be a JSON list")
    earliest = parse_day(earliest_day)
    latest = parse_day(latest_day)
    valid = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise SyncError("retry state contains a non-object entry")
        try:
            item = {
                "record_date": format_day(parse_day(raw.get("record_date"))),
                "product_id": int(raw.get("product_id")),
                "product_name": safe_text(raw.get("product_name"), 128),
                "minis_id": safe_text(raw.get("minis_id"), 64),
                "advertiser_id": normalize_id(raw.get("advertiser_id"), "advertiser_id"),
                "data_level": str(raw.get("data_level") or "").upper(),
                "query_id": normalize_id(raw.get("query_id"), "query_id"),
                "campaign_id": normalize_id(raw.get("campaign_id"), "campaign_id")
                if raw.get("campaign_id")
                else None,
                "adgroup_id": normalize_id(raw.get("adgroup_id"), "adgroup_id")
                if raw.get("adgroup_id")
                else None,
                "source_adgroup_id": str(raw.get("source_adgroup_id") or ""),
            }
        except (SyncError, TypeError, ValueError):
            raise SyncError("retry state contains an invalid candidate")
        day = parse_day(item["record_date"])
        if item["data_level"] not in VALID_DATA_LEVELS or item["product_id"] <= 0:
            raise SyncError("retry state contains an invalid scope")
        if earliest <= day <= latest:
            valid.append(item)
    return merge_candidates(valid)


def save_retry_candidates(path, candidates):
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    temp_path = "%s.%s.tmp" % (path, os.getpid())
    ordered = sorted((dict(item) for item in merge_candidates(candidates)), key=candidate_key)
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(ordered, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class TikTokBidProtectionClient(object):
    def __init__(self, access_token, timeout=DEFAULT_TIMEOUT, max_retries=4, session_factory=requests.Session):
        self._access_token = str(access_token)
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)
        self.session_factory = session_factory

    def _session(self):
        session = getattr(_THREAD_LOCAL, "session", None)
        if session is None:
            session = self.session_factory()
            _THREAD_LOCAL.session = session
        return session

    @staticmethod
    def _retryable_code(code):
        try:
            number = int(code)
        except (TypeError, ValueError):
            return False
        return number in RETRYABLE_API_CODES or 50000 <= number < 60000

    def get_json(self, url, params):
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session().get(
                    url,
                    headers={"Access-Token": self._access_token},
                    params=params,
                    timeout=(5, self.timeout),
                )
                http_status = int(response.status_code)
                if http_status == 429 or http_status >= 500:
                    raise ApiError(http_status, "HTTP retryable failure", retryable=True)
                if http_status != 200:
                    raise ApiError(http_status, "HTTP request failed", retryable=False)
                try:
                    payload = response.json()
                except ValueError:
                    raise ApiError(http_status, "response was not JSON", retryable=http_status >= 500)
                code = payload.get("code")
                if code not in (0, "0"):
                    raise ApiError(
                        code,
                        payload.get("message") or "business error",
                        payload.get("request_id"),
                        self._retryable_code(code),
                    )
                return payload
            except (requests.RequestException, ApiError) as exc:
                last_error = exc
                retryable = isinstance(exc, requests.RequestException) or getattr(exc, "retryable", False)
                if not retryable or attempt >= self.max_retries:
                    if isinstance(exc, ApiError):
                        raise
                    raise SyncError("TikTok API network request failed: %s" % type(exc).__name__)
                time.sleep(min(16, 2 ** attempt))
        raise last_error

    def fetch_history(self, advertiser_id, data_level, query_ids, day):
        if len(query_ids) > API_BATCH_SIZE:
            raise SyncError("history query contains more than 200 IDs")
        params = {
            "advertiser_id": normalize_id(advertiser_id, "advertiser_id"),
            "data_level": str(data_level).upper(),
            "query_ids": json.dumps([normalize_id(value, "query_id") for value in query_ids], separators=(",", ":")),
            "start_date": format_day(parse_day(day)),
            "end_date": format_day(parse_day(day)),
        }
        payload = self.get_json(HISTORY_URL, params)
        return (payload.get("data") or {}).get("bid_protection_records") or []

    def fetch_status(self, advertiser_id, data_level, query_ids):
        if len(query_ids) > API_BATCH_SIZE:
            raise SyncError("status query contains more than 200 IDs")
        params = {
            "advertiser_id": normalize_id(advertiser_id, "advertiser_id"),
            "data_level": str(data_level).upper(),
            "query_ids": json.dumps([normalize_id(value, "query_id") for value in query_ids], separators=(",", ":")),
        }
        payload = self.get_json(STATUS_URL, params)
        return (payload.get("data") or {}).get("list") or []


def normalize_history_records(records, candidate_map, requested_day, data_level):
    normalized = {}
    for record in records:
        query_id = normalize_id(record.get("query_id"), "response query_id")
        candidate = candidate_map.get(query_id)
        if not candidate:
            raise SyncError("history response contained an unrequested query_id")
        record_date = format_day(parse_day(record.get("record_date")))
        if record_date != requested_day:
            raise SyncError("history response record_date differs from request")
        response_level = str(record.get("data_level") or data_level).upper()
        if response_level != data_level:
            raise SyncError("history response data_level differs from request")
        status = str(record.get("bid_protection_daily_status") or "").upper()
        if status not in VALID_DAILY_STATUSES:
            raise SyncError("unknown bid protection daily status")
        scaled, amount = parse_scaled_credit(record.get("credit_amount"))
        item = dict(candidate)
        item.update(
            {
                "record_date": record_date,
                "data_level": data_level,
                "query_id": query_id,
                "protection_status": status,
                "status_detail": safe_text(record.get("status_detail"), 60000),
                "credit_amount_scaled": scaled,
                "credit_amount": amount,
                "currency": safe_text(record.get("currency"), 8),
            }
        )
        normalized[query_id] = item
    return list(normalized.values())


def history_tasks(candidates):
    grouped = collections.defaultdict(list)
    for item in candidates:
        grouped[(item["record_date"], item["advertiser_id"], item["data_level"])].append(item)
    tasks = []
    for (day, advertiser_id, data_level), items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda row: row["query_id"])
        for part in chunks(ordered, API_BATCH_SIZE):
            tasks.append((day, advertiser_id, data_level, part))
    return tasks


def fetch_history_task(client, task):
    day, advertiser_id, data_level, items = task
    candidate_map = {item["query_id"]: item for item in items}
    records = client.fetch_history(advertiser_id, data_level, list(candidate_map), day)
    rows = normalize_history_records(records, candidate_map, day, data_level)
    returned_ids = {row["query_id"] for row in rows}
    # The history endpoint is intentionally sparse: a successful response does
    # not promise one record per requested object.  Current protection status
    # cannot classify a past day's omission, so code=0 omissions are counted as
    # not applicable for that day.  Only request exceptions enter retry state.
    return rows, [], len(set(candidate_map) - returned_ids)


UPSERT_SQL = """
INSERT INTO ads_ai.ads_tiktok_minis_bid_protection_daily (
  record_date, product_id, product_name, minis_id,
  advertiser_id, data_level, query_id, campaign_id, adgroup_id,
  protection_status, status_detail, credit_amount_scaled,
  credit_amount, currency, sync_at
) VALUES (
  %s, %s, %s, %s,
  %s, %s, %s, %s, %s,
  %s, %s, %s,
  %s, %s, NOW()
)
ON DUPLICATE KEY UPDATE
  product_id = VALUES(product_id),
  product_name = VALUES(product_name),
  minis_id = VALUES(minis_id),
  campaign_id = VALUES(campaign_id),
  adgroup_id = VALUES(adgroup_id),
  protection_status = VALUES(protection_status),
  status_detail = VALUES(status_detail),
  credit_amount_scaled = VALUES(credit_amount_scaled),
  credit_amount = VALUES(credit_amount),
  currency = VALUES(currency),
  sync_at = VALUES(sync_at)
"""


def write_history_rows(rows):
    if not rows:
        return 0
    try:
        pymysql = importlib.import_module("pymysql")
    except ImportError as exc:
        raise SyncError("pymysql is required for fixed target-table writes") from exc
    settings = mysql_connection_settings(WRITE_PORT)
    conn = pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database="ads_ai",
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
        read_timeout=300,
        write_timeout=300,
    )
    written = 0
    try:
        with conn.cursor() as cursor:
            cursor.max_stmt_length = WRITE_MAX_STATEMENT_BYTES
            for part in chunks(rows, WRITE_BATCH_SIZE):
                values = []
                for row in part:
                    values.append(
                        (
                            row["record_date"],
                            int(row["product_id"]),
                            safe_text(row["product_name"], 128),
                            safe_text(row["minis_id"], 64),
                            row["advertiser_id"],
                            row["data_level"],
                            row["query_id"],
                            row.get("campaign_id"),
                            row.get("adgroup_id"),
                            row["protection_status"],
                            row["status_detail"],
                            row["credit_amount_scaled"],
                            row["credit_amount"],
                            row["currency"],
                        )
                    )
                cursor.executemany(UPSERT_SQL, values)
                # Keep one connection but commit each bounded multi-value
                # statement.  This avoids a day-sized undo transaction and
                # preserves completed batches if a later batch fails.
                conn.commit()
                written += len(part)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return written


def sync_candidates(client, candidates, workers=DEFAULT_WORKERS, dry_run=False):
    tasks = history_tasks(candidates)
    rows = []
    failures = []
    if not tasks:
        return {
            "requests": 0,
            "rows": 0,
            "failures": [],
            "missing": 0,
            "not_applicable": 0,
            "retry_candidates": [],
        }
    retry_candidates = []
    not_applicable = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {executor.submit(fetch_history_task, client, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            task = future_map[future]
            try:
                task_rows, task_retries, task_not_applicable = future.result()
                rows.extend(task_rows)
                not_applicable += task_not_applicable
                retry_candidates.extend(task_retries)
            except Exception as exc:
                retry_candidates.extend(task[3])
                failures.append(
                    {
                        "record_date": task[0],
                        "advertiser_id": task[1],
                        "data_level": task[2],
                        "id_count": len(task[3]),
                        "error": redact_error(exc),
                    }
                )
    if not dry_run:
        write_history_rows(rows)
    return {
        "requests": len(tasks),
        "rows": len(rows),
        "failures": failures,
        "missing": len(retry_candidates),
        "not_applicable": not_applicable,
        "retry_candidates": retry_candidates,
    }


def run_sync(args):
    today = beijing_today()
    yesterday = today - timedelta(days=1)
    if args.daily:
        start_date = end_date = format_day(yesterday)
    elif args.backfill_days:
        count = int(args.backfill_days)
        if count < 1 or count > 60:
            raise SyncError("backfill days must be between 1 and 60")
        end_date = format_day(yesterday)
        start_date = format_day(yesterday - timedelta(days=count - 1))
    else:
        start_date = format_day(parse_day(args.start_date))
        end_date = format_day(parse_day(args.end_date or args.start_date))
    if parse_day(end_date) >= today:
        raise SyncError("current-day bid-protection amounts are not accepted as final data")
    if (parse_day(end_date) - parse_day(start_date)).days >= 60:
        raise SyncError("date range exceeds the 60-day API window")

    access_token = load_access_token(args.token_db, args.token_key)
    client = TikTokBidProtectionClient(access_token, timeout=args.api_timeout)
    emit(
        "sync_start",
        mode="daily" if args.daily else "backfill",
        start_date=start_date,
        end_date=end_date,
        dry_run=bool(args.dry_run),
    )

    retry_start = format_day(yesterday - timedelta(days=DEFAULT_BACKFILL_DAYS - 1))
    retry_rows = load_retry_candidates(args.retry_state, retry_start, format_day(yesterday))
    retry_by_day = collections.defaultdict(list)
    for item in retry_rows:
        retry_by_day[item["record_date"]].append(item)
    retry_state = {candidate_key(item): item for item in retry_rows}

    pending_by_day = collections.defaultdict(list)
    if args.daily and not args.skip_pending:
        for item in fetch_pending_candidates(retry_start, format_day(yesterday)):
            pending_by_day[item["record_date"]].append(item)

    totals = {
        "days": 0,
        "candidates": 0,
        "requests": 0,
        "rows": 0,
        "missing": 0,
        "not_applicable": 0,
        "failures": [],
    }
    metadata_cache = {}
    days = list(each_day(start_date, end_date))
    if args.daily:
        days = sorted(set(days) | set(pending_by_day) | set(retry_by_day))
    for day in days:
        fresh = []
        if start_date <= day <= end_date:
            for level in VALID_DATA_LEVELS:
                level_rows = build_day_candidates(day, level, metadata_cache, end_date)
                fresh.extend(level_rows)
                emit("source_scope", record_date=day, data_level=level, candidates=len(level_rows))
        candidates = merge_candidates(retry_by_day.get(day, []), pending_by_day.get(day, []), fresh)
        try:
            result = sync_candidates(client, candidates, workers=args.workers, dry_run=args.dry_run)
        except Exception:
            # A target-table write failure happens after the API calls have
            # completed.  Persist the entire attempted scope before failing so
            # a later daily run can safely replay it.
            if not args.dry_run:
                for candidate in candidates:
                    retry_state[candidate_key(candidate)] = candidate
                save_retry_candidates(args.retry_state, list(retry_state.values()))
            raise
        if not args.dry_run:
            for candidate in candidates:
                retry_state.pop(candidate_key(candidate), None)
            for candidate in result["retry_candidates"]:
                retry_state[candidate_key(candidate)] = candidate
            save_retry_candidates(args.retry_state, list(retry_state.values()))
        totals["days"] += 1
        totals["candidates"] += len(candidates)
        totals["requests"] += result["requests"]
        totals["rows"] += result["rows"]
        totals["missing"] += result["missing"]
        totals["not_applicable"] += result["not_applicable"]
        totals["failures"].extend(result["failures"])
        for failure in result["failures"]:
            emit("request_failed", **failure)
        emit(
            "day_complete",
            record_date=day,
            candidates=len(candidates),
            requests=result["requests"],
            rows=result["rows"],
            missing=result["missing"],
            not_applicable=result["not_applicable"],
            failures=len(result["failures"]),
            dry_run=bool(args.dry_run),
        )

    summary = dict(totals)
    failure_rows = summary.pop("failures")
    summary["failure_count"] = len(failure_rows)
    summary["failed_account_count"] = len(
        {(row["record_date"], row["advertiser_id"]) for row in failure_rows}
    )
    summary["status"] = "partial" if summary["failure_count"] else "ok"
    summary["start_date"] = start_date
    summary["end_date"] = end_date
    summary["dry_run"] = bool(args.dry_run)
    summary["retry_backlog"] = len(retry_state)
    emit("sync_complete", **summary)
    if totals["failures"]:
        return 2
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Sync TT mini-program bid-protection daily history")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daily", action="store_true", help="sync yesterday and refresh non-terminal rows")
    mode.add_argument("--backfill-days", type=int, help="backfill N completed days, maximum 60")
    mode.add_argument("--start-date", help="manual start date, YYYY-MM-DD")
    parser.add_argument("--end-date", help="manual end date, defaults to start date")
    parser.add_argument("--skip-pending", action="store_true", help="do not refresh non-terminal rows in daily mode")
    parser.add_argument("--dry-run", action="store_true", help="call read APIs but do not write ads_ai")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--api-timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--token-db", default=TOKEN_DB)
    parser.add_argument("--token-key", default=TOKEN_KEY)
    parser.add_argument("--retry-state", default=DEFAULT_RETRY_STATE)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run_sync(args)
    except Exception as exc:
        emit("sync_failed", error=redact_error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

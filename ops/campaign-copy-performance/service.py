#!/usr/bin/env python3
from __future__ import annotations

import collections
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import threading
import time
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pymysql


ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.html"
HOST = os.environ.get("CAMPAIGN_COPY_REPORT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAMPAIGN_COPY_REPORT_PORT", "8831"))
CACHE_TTL_SECONDS = max(60, int(os.environ.get("CAMPAIGN_COPY_REPORT_CACHE_TTL_SECONDS", "900")))
CACHE_REFRESH_RETRY_SECONDS = max(
    30,
    int(os.environ.get("CAMPAIGN_COPY_REPORT_REFRESH_RETRY_SECONDS", "120")),
)
STALE_IF_ERROR_SECONDS = max(
    CACHE_TTL_SECONDS,
    int(os.environ.get("CAMPAIGN_COPY_REPORT_STALE_IF_ERROR_SECONDS", "86400")),
)
CACHE_PATH_TEXT = os.environ.get("CAMPAIGN_COPY_REPORT_CACHE_PATH", "").strip()
CACHE_PATH = Path(CACHE_PATH_TEXT) if CACHE_PATH_TEXT else None
METRIC_FIELDS = (
    "impressions",
    "clicks",
    "installs",
    "spend",
    "purchase",
    "revenue",
    "af_installs",
    "af_revenue0",
    "af_revenue",
    "iaa_revenue",
    "source_rows",
)
CAMPAIGN_FIELDS = (
    "campaign_id",
    "campaign_name",
    "origin_campaign_id",
    "account_id",
    "content_id",
    "queue_id",
    "rule_name",
    "rule_label",
    "product",
    "user_id",
    "user_name",
    "user_label",
    "copy_at",
    "copy_date",
    "mapped",
    "ad_count",
    "age_hours",
    "countries",
    "languages",
)
DAILY_FIELDS = (
    "campaign_id",
    "dt",
    "spend",
    "af_revenue0",
    "af_revenue",
    "af_installs",
    "installs",
    "purchase",
    "revenue",
    "iaa_revenue",
    "impressions",
    "clicks",
    "source_rows",
)
EXTRA_ENTITY_FIELDS = (
    "level",
    "entity_key",
    "entity_id",
    "entity_name",
    "origin_entity_id",
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "account_id",
    "content_id",
    "queue_id",
    "rule_name",
    "rule_label",
    "product",
    "user_id",
    "user_name",
    "user_label",
    "copy_at",
    "copy_date",
    "mapped",
    "ad_count",
    "age_hours",
    "countries",
    "languages",
)
EXTRA_DAILY_FIELDS = (
    "level",
    "entity_key",
    "entity_id",
    "dt",
    *METRIC_FIELDS,
)


def chunks(values: list[str], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def number(value: object) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def parse_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def dominant(values: list[object], fallback: str = "") -> str:
    counter = collections.Counter(str(value).strip() for value in values if str(value or "").strip())
    if not counter:
        return fallback
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0][0]


def joined_distinct(values: list[object], limit: int = 4) -> str:
    items = sorted({str(value).strip() for value in values if str(value or "").strip()})
    if len(items) <= limit:
        return "、".join(items)
    return "、".join(items[:limit]) + f" 等{len(items)}项"


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def mysql_connection():
    port = int(os.environ.get("ADMIN_MAPPING_MYSQL_PORT", "63350"))
    if port != 63350:
        raise RuntimeError(f"refusing non-read-only MySQL port: {port}")
    required = (
        "ADMIN_MAPPING_MYSQL_HOST",
        "ADMIN_MAPPING_MYSQL_USER",
        "ADMIN_MAPPING_MYSQL_PASSWORD",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("missing MySQL settings: " + ", ".join(missing))
    return pymysql.connect(
        host=os.environ["ADMIN_MAPPING_MYSQL_HOST"],
        port=port,
        user=os.environ["ADMIN_MAPPING_MYSQL_USER"],
        password=os.environ["ADMIN_MAPPING_MYSQL_PASSWORD"],
        database="ads_business",
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=60,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


def query_raw() -> dict:
    conn = mysql_connection()
    payload: dict[str, object] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT @@read_only AS is_read_only, NOW() AS server_time, "
                "@@session.time_zone AS session_time_zone"
            )
            safety = cur.fetchone()
            if int((safety or {}).get("is_read_only") or 0) != 1:
                raise RuntimeError("refusing report build: MySQL endpoint is not read-only")
            payload["safety"] = safety

            cur.execute(
                """
                SELECT level, status, COUNT(*) AS n
                FROM ads_business.ads_campaign_rule_logs
                WHERE platform=0 AND level IN (0,1,2) AND data_source=1
                GROUP BY level, status
                ORDER BY level, status
                """
            )
            payload["copy_status_counts"] = cur.fetchall()

            cur.execute(
                """
                SELECT l.id, l.level, l.account_id, l.origin_id, l.new_id, l.queue_id,
                       l.user_id, l.app_id, l.content_id, l.created_at,
                       r.name AS rule_name, a.name AS app_name,
                       aug.name AS user_name
                FROM ads_business.ads_campaign_rule_logs l
                LEFT JOIN ads_business.ads_campaign_auto_rules r ON r.id=l.queue_id
                LEFT JOIN kunlunads_dev.ads_apps_setting a ON a.id=l.app_id
                LEFT JOIN (
                    SELECT sub_user_id,
                           COALESCE(
                               MAX(CASE WHEN status=0 THEN NULLIF(TRIM(name), '') END),
                               MAX(NULLIF(TRIM(name), ''))
                           ) AS name
                    FROM kunlunads_dev.admin_user_group
                    GROUP BY sub_user_id
                ) aug ON aug.sub_user_id=l.user_id
                WHERE l.platform=0 AND l.level IN (0,1,2) AND l.data_source=1 AND l.status=1
                ORDER BY l.created_at, l.id
                """
            )
            successful_logs = [
                row
                for row in cur.fetchall()
                if str(row.get("new_id") or "").isdigit() and int(row["new_id"]) > 0
            ]
            logs = [row for row in successful_logs if int(row.get("level") or 0) == 0]
            extra_logs = [row for row in successful_logs if int(row.get("level") or 0) in (1, 2)]
            payload["logs"] = logs
            payload["extra_logs"] = extra_logs

            campaign_ids = sorted({str(row["new_id"]) for row in logs})
            mapping: list[dict] = []
            for part in chunks(campaign_ids, 300):
                placeholders = ",".join(["%s"] * len(part))
                cur.execute(
                    f"""
                    SELECT campaign_id, ad_id, campaign_name, product,
                           ad_account_id, status, country, language, budget
                    FROM kunlunads_dev.ads_facebook_auto_created_data FORCE INDEX (campaign_id)
                    WHERE campaign_id IN ({placeholders})
                    """,
                    part,
                )
                mapping.extend(cur.fetchall())

            mapped_campaigns = {str(row["campaign_id"]) for row in mapping}
            missing = sorted(set(campaign_ids) - mapped_campaigns)
            for part in chunks(missing, 300):
                placeholders = ",".join(["%s"] * len(part))
                cur.execute(
                    f"""
                    SELECT campaign_id, ad_id, campaign_name, product,
                           ad_account_id, status, country, language, budget
                    FROM ads_ai.ads_facebook_auto_created_data FORCE INDEX (campaign_id)
                    WHERE campaign_id IN ({placeholders})
                    """,
                    part,
                )
                mapping.extend(cur.fetchall())
            payload["mapping"] = mapping

            extra_mapping: list[dict] = []
            for level, id_field in ((1, "adset_id"), (2, "ad_id")):
                entity_ids = sorted(
                    {str(row["new_id"]) for row in extra_logs if int(row.get("level") or 0) == level}
                )
                found: set[str] = set()
                for source_table in (
                    "kunlunads_dev.ads_facebook_auto_created_data",
                    "ads_ai.ads_facebook_auto_created_data",
                ):
                    remaining = sorted(set(entity_ids) - found)
                    for part in chunks(remaining, 300):
                        placeholders = ",".join(["%s"] * len(part))
                        cur.execute(
                            f"""
                            SELECT campaign_id, adset_id, ad_id, campaign_name,
                                   adset_name, ad_name, product, ad_account_id,
                                   status, country, language, budget
                            FROM {source_table}
                            WHERE {id_field} IN ({placeholders})
                            """,
                            part,
                        )
                        for source_row in cur.fetchall():
                            entity_id = str(source_row.get(id_field) or "")
                            if not entity_id:
                                continue
                            source_row["level"] = level
                            source_row["entity_id"] = entity_id
                            extra_mapping.append(source_row)
                            found.add(entity_id)
            payload["extra_mapping"] = extra_mapping

            cur.execute(
                "SELECT dt FROM kunlunads_dev.ads_custom_source_insight "
                "FORCE INDEX (index_dad) ORDER BY dt DESC LIMIT 1"
            )
            latest = cur.fetchone()
            max_dt = latest["dt"] if latest else None
            payload["insight_max_dt"] = max_dt

            ad_ids = sorted(
                {
                    str(row["ad_id"])
                    for row in mapping
                    if str(row.get("ad_id") or "").isdigit() and int(row["ad_id"]) > 0
                }
            )
            log_dates = [parse_datetime(row.get("created_at")) for row in logs]
            min_dt = min((value.date() for value in log_dates if value), default=max_dt)
            daily: list[dict] = []
            current = min_dt
            while current and max_dt and current <= max_dt:
                for part in chunks(ad_ids, 500):
                    placeholders = ",".join(["%s"] * len(part))
                    cur.execute(
                        f"""
                        SELECT campaign_id,
                               SUM(impressions) AS impressions,
                               SUM(clicks) AS clicks,
                               SUM(installs) AS installs,
                               SUM(spend) AS spend,
                               SUM(purchase) AS purchase,
                               SUM(revenue) AS revenue,
                               SUM(af_installs) AS af_installs,
                               SUM(af_revenue0) AS af_revenue0,
                               SUM(af_revenue) AS af_revenue,
                               SUM(ad_impression_revenue) AS iaa_revenue,
                               COUNT(*) AS source_rows
                        FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX (index_dad)
                        WHERE dt=%s AND platform=0 AND ad_id IN ({placeholders})
                        GROUP BY campaign_id
                        """,
                        [current] + part,
                    )
                    for row in cur.fetchall():
                        row["dt"] = current
                        daily.append(row)
                current += dt.timedelta(days=1)
            payload["daily"] = daily

            extra_ad_ids = sorted(
                {
                    str(row["ad_id"])
                    for row in extra_mapping
                    if str(row.get("ad_id") or "").isdigit() and int(row["ad_id"]) > 0
                }
            )
            extra_log_dates = [parse_datetime(row.get("created_at")) for row in extra_logs]
            extra_min_dt = min((value.date() for value in extra_log_dates if value), default=max_dt)
            extra_daily: list[dict] = []
            current = extra_min_dt
            while current and max_dt and current <= max_dt:
                for part in chunks(extra_ad_ids, 500):
                    placeholders = ",".join(["%s"] * len(part))
                    cur.execute(
                        f"""
                        SELECT ad_id,
                               SUM(impressions) AS impressions,
                               SUM(clicks) AS clicks,
                               SUM(installs) AS installs,
                               SUM(spend) AS spend,
                               SUM(purchase) AS purchase,
                               SUM(revenue) AS revenue,
                               SUM(af_installs) AS af_installs,
                               SUM(af_revenue0) AS af_revenue0,
                               SUM(af_revenue) AS af_revenue,
                               SUM(ad_impression_revenue) AS iaa_revenue,
                               COUNT(*) AS source_rows
                        FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX (index_dad)
                        WHERE dt=%s AND platform=0 AND ad_id IN ({placeholders})
                        GROUP BY ad_id
                        """,
                        [current] + part,
                    )
                    for row in cur.fetchall():
                        row["dt"] = current
                        extra_daily.append(row)
                current += dt.timedelta(days=1)
            payload["extra_daily"] = extra_daily
            return payload
    finally:
        conn.close()


def build_payload(raw: dict) -> dict:
    safety = raw.get("safety") or {}
    if int(safety.get("is_read_only") or 0) != 1:
        raise RuntimeError("refusing report build: MySQL endpoint is not read-only")
    report_time = parse_datetime(safety.get("server_time")) or dt.datetime.now()

    logs_by_campaign: dict[str, list[dict]] = collections.defaultdict(list)
    for row in raw.get("logs", []):
        logs_by_campaign[str(row["new_id"])].append(row)
    mapping_by_campaign: dict[str, list[dict]] = collections.defaultdict(list)
    for row in raw.get("mapping", []):
        mapping_by_campaign[str(row["campaign_id"])].append(row)

    daily_by_key: dict[tuple[str, str], dict] = {}
    for row in raw.get("daily", []):
        campaign_id = str(row.get("campaign_id") or "")
        stat_date = str(row.get("dt") or "")
        if not campaign_id or not stat_date:
            continue
        key = (campaign_id, stat_date)
        aggregate = daily_by_key.setdefault(
            key,
            {"campaign_id": campaign_id, "dt": stat_date, **{field: 0.0 for field in METRIC_FIELDS}},
        )
        for field in METRIC_FIELDS:
            aggregate[field] += number(row.get(field))

    campaigns: list[dict] = []
    copy_date_by_campaign: dict[str, str] = {}
    for campaign_id, logs in logs_by_campaign.items():
        log = sorted(logs, key=lambda row: (str(row.get("created_at")), int(row.get("id") or 0)))[0]
        mapping = mapping_by_campaign.get(campaign_id, [])
        copy_at = parse_datetime(log.get("created_at"))
        copy_date = copy_at.date().isoformat() if copy_at else ""
        copy_date_by_campaign[campaign_id] = copy_date
        queue_id = int(log.get("queue_id") or 0)
        rule_name = str(log.get("rule_name") or "").strip()
        rule_label = (
            f"规则 #{queue_id} · {rule_name or '名称缺失'}"
            if queue_id
            else "复制触发来源未归因（queue_id=0）"
        )
        user_id = int(log.get("user_id") or 0)
        user_name = str(log.get("user_name") or "").strip()
        user_label = f"{user_name}（ID {user_id}）" if user_name else f"未配置姓名（ID {user_id}）"
        product = str(log.get("app_name") or "").strip() or dominant(
            [row.get("product") for row in mapping],
            f"App #{log.get('app_id')}",
        )
        campaigns.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": dominant([row.get("campaign_name") for row in mapping]),
                "origin_campaign_id": str(log.get("origin_id") or ""),
                "account_id": str(log.get("account_id") or ""),
                "content_id": str(log.get("content_id") or ""),
                "queue_id": queue_id,
                "rule_name": rule_name,
                "rule_label": rule_label,
                "product": product,
                "user_id": user_id,
                "user_name": user_name,
                "user_label": user_label,
                "copy_at": str(log.get("created_at") or ""),
                "copy_date": copy_date,
                "mapped": bool(mapping),
                "ad_count": len({str(row.get("ad_id")) for row in mapping if row.get("ad_id")}),
                "age_hours": round(max(0.0, (report_time - copy_at).total_seconds() / 3600), 2)
                if copy_at
                else None,
                "countries": joined_distinct([row.get("country") for row in mapping]),
                "languages": joined_distinct([row.get("language") for row in mapping]),
            }
        )

    campaigns.sort(key=lambda row: (row["copy_at"], row["campaign_id"]), reverse=True)
    campaign_daily = []
    for (campaign_id, stat_date), row in sorted(daily_by_key.items()):
        copy_date = copy_date_by_campaign.get(campaign_id, "")
        if copy_date and stat_date < copy_date:
            continue
        campaign_daily.append(
            {
                "campaign_id": campaign_id,
                "dt": stat_date,
                **{field: round(number(row.get(field)), 4) for field in METRIC_FIELDS},
            }
        )

    extra_logs_by_entity: dict[tuple[int, str], list[dict]] = collections.defaultdict(list)
    for row in raw.get("extra_logs", []):
        level = int(row.get("level") or 0)
        if level in (1, 2):
            extra_logs_by_entity[(level, str(row["new_id"]))].append(row)

    extra_mapping_by_entity: dict[tuple[int, str], list[dict]] = collections.defaultdict(list)
    ad_to_entities: dict[str, set[tuple[int, str]]] = collections.defaultdict(set)
    for row in raw.get("extra_mapping", []):
        key = (int(row.get("level") or 0), str(row.get("entity_id") or ""))
        if key[0] not in (1, 2) or not key[1]:
            continue
        extra_mapping_by_entity[key].append(row)
        ad_id = str(row.get("ad_id") or "")
        if ad_id:
            ad_to_entities[ad_id].add(key)

    extra_daily_by_key: dict[tuple[int, str, str], dict] = {}
    for row in raw.get("extra_daily", []):
        stat_date = str(row.get("dt") or "")
        for level, entity_id in ad_to_entities.get(str(row.get("ad_id") or ""), set()):
            key = (level, entity_id, stat_date)
            aggregate = extra_daily_by_key.setdefault(
                key,
                {"level": level, "entity_id": entity_id, "dt": stat_date, **{field: 0.0 for field in METRIC_FIELDS}},
            )
            for field in METRIC_FIELDS:
                aggregate[field] += number(row.get(field))

    extra_entities: list[dict] = []
    copy_date_by_entity: dict[tuple[int, str], str] = {}
    for (level, entity_id), logs in extra_logs_by_entity.items():
        log = sorted(logs, key=lambda row: (str(row.get("created_at")), int(row.get("id") or 0)))[0]
        mapping = extra_mapping_by_entity.get((level, entity_id), [])
        copy_at = parse_datetime(log.get("created_at"))
        copy_date = copy_at.date().isoformat() if copy_at else ""
        copy_date_by_entity[(level, entity_id)] = copy_date
        queue_id = int(log.get("queue_id") or 0)
        rule_name = str(log.get("rule_name") or "").strip()
        rule_label = (
            f"规则 #{queue_id} · {rule_name or '名称缺失'}"
            if queue_id
            else "复制触发来源未归因（queue_id=0）"
        )
        user_id = int(log.get("user_id") or 0)
        user_name = str(log.get("user_name") or "").strip()
        user_label = f"{user_name}（ID {user_id}）" if user_name else f"未配置姓名（ID {user_id}）"
        product = str(log.get("app_name") or "").strip() or dominant(
            [row.get("product") for row in mapping],
            f"App #{log.get('app_id')}",
        )
        campaign_id = dominant([row.get("campaign_id") for row in mapping])
        adset_id = entity_id if level == 1 else dominant([row.get("adset_id") for row in mapping])
        ad_id = entity_id if level == 2 else ""
        entity_name = dominant(
            [row.get("adset_name" if level == 1 else "ad_name") for row in mapping]
        )
        extra_entities.append(
            {
                "level": level,
                "entity_key": f"{level}:{entity_id}",
                "entity_id": entity_id,
                "entity_name": entity_name,
                "origin_entity_id": str(log.get("origin_id") or ""),
                "campaign_id": campaign_id,
                "campaign_name": dominant([row.get("campaign_name") for row in mapping]),
                "adset_id": adset_id,
                "adset_name": dominant([row.get("adset_name") for row in mapping]),
                "ad_id": ad_id,
                "ad_name": dominant([row.get("ad_name") for row in mapping]) if level == 2 else "",
                "account_id": str(log.get("account_id") or ""),
                "content_id": str(log.get("content_id") or ""),
                "queue_id": queue_id,
                "rule_name": rule_name,
                "rule_label": rule_label,
                "product": product,
                "user_id": user_id,
                "user_name": user_name,
                "user_label": user_label,
                "copy_at": str(log.get("created_at") or ""),
                "copy_date": copy_date,
                "mapped": bool(mapping),
                "ad_count": len({str(row.get("ad_id")) for row in mapping if row.get("ad_id")}),
                "age_hours": round(max(0.0, (report_time - copy_at).total_seconds() / 3600), 2)
                if copy_at
                else None,
                "countries": joined_distinct([row.get("country") for row in mapping]),
                "languages": joined_distinct([row.get("language") for row in mapping]),
            }
        )
    extra_entities.sort(key=lambda row: (row["copy_at"], row["level"], row["entity_id"]), reverse=True)

    extra_entity_daily: list[dict] = []
    for (level, entity_id, stat_date), row in sorted(extra_daily_by_key.items()):
        copy_date = copy_date_by_entity.get((level, entity_id), "")
        if copy_date and stat_date < copy_date:
            continue
        extra_entity_daily.append(
            {
                "level": level,
                "entity_key": f"{level}:{entity_id}",
                "entity_id": entity_id,
                "dt": stat_date,
                **{field: round(number(row.get(field)), 4) for field in METRIC_FIELDS},
            }
        )

    status_by_level: dict[int, dict[int, int]] = collections.defaultdict(dict)
    for row in raw.get("copy_status_counts", []):
        status_by_level[int(row.get("level") or 0)][int(row["status"])] = int(row["n"])

    def level_pipeline(level: int) -> dict:
        counts = status_by_level.get(level, {})
        terminal = counts.get(1, 0) + counts.get(2, 0)
        return {
            "running": counts.get(0, 0),
            "success": counts.get(1, 0),
            "failed": counts.get(2, 0),
            "terminal_success_rate": ratio(counts.get(1, 0), terminal),
        }

    copy_pipelines = {str(level): level_pipeline(level) for level in (0, 1, 2)}
    copy_times = [row["copy_at"] for row in campaigns + extra_entities if row["copy_at"]]
    stat_dates = [row["dt"] for row in campaign_daily + extra_entity_daily if row["dt"]]
    return {
        "meta": {
            "generated_at": str(safety.get("server_time") or ""),
            "today": report_time.date().isoformat(),
            "session_time_zone": str(safety.get("session_time_zone") or ""),
            "insight_max_dt": str(raw.get("insight_max_dt") or ""),
            "source": "ads_business.ads_campaign_rule_logs + kunlunads_dev.admin_user_group + kunlunads_dev.ads_custom_source_insight",
            "read_only_verified": True,
            "copy_start": min(copy_times, default=""),
            "copy_end": max(copy_times, default=""),
            "stat_start": min(stat_dates, default=""),
            "stat_end": max(stat_dates, default=""),
        },
        "copy_pipeline": copy_pipelines["0"],
        "copy_pipelines": copy_pipelines,
        "campaigns": campaigns,
        "daily": campaign_daily,
        "extra_entities": extra_entities,
        "extra_daily": extra_entity_daily,
    }


def json_default(value: object):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def encode_wire_payload(payload: dict) -> dict:
    """Remove repeated JSON object keys while keeping the frontend contract explicit."""
    return {
        "v": 2,
        "m": payload["meta"],
        "p": payload["copy_pipeline"],
        "lp": payload.get("copy_pipelines", {"0": payload["copy_pipeline"]}),
        "cf": list(CAMPAIGN_FIELDS),
        "c": [[row.get(field) for field in CAMPAIGN_FIELDS] for row in payload["campaigns"]],
        "df": list(DAILY_FIELDS),
        "d": [[row.get(field) for field in DAILY_FIELDS] for row in payload["daily"]],
        "xf": list(EXTRA_ENTITY_FIELDS),
        "x": [
            [row.get(field) for field in EXTRA_ENTITY_FIELDS]
            for row in payload.get("extra_entities", [])
        ],
        "xdf": list(EXTRA_DAILY_FIELDS),
        "xd": [
            [row.get(field) for field in EXTRA_DAILY_FIELDS]
            for row in payload.get("extra_daily", [])
        ],
    }


def serialize_wire_payload(payload: dict) -> bytes:
    return json.dumps(
        encode_wire_payload(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        default=json_default,
    ).encode("utf-8")


def validate_or_convert_cache_body(body: bytes) -> bytes:
    data = json.loads(body)
    if data.get("v") == 2:
        if not data.get("m", {}).get("read_only_verified"):
            raise ValueError("disk cache is not marked read-only verified")
        if not isinstance(data.get("c"), list) or not isinstance(data.get("d"), list):
            raise ValueError("disk cache wire rows are invalid")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not data.get("meta", {}).get("read_only_verified"):
        raise ValueError("legacy disk cache is not marked read-only verified")
    if not isinstance(data.get("campaigns"), list) or not isinstance(data.get("daily"), list):
        raise ValueError("legacy disk cache rows are invalid")
    return serialize_wire_payload(data)


def etag_matches(header_value: str, etag: str) -> bool:
    def normalize(value: str) -> str:
        value = value.strip()
        return value[2:] if value.startswith("W/") else value

    wanted = normalize(etag)
    return any(token.strip() == "*" or normalize(token) == wanted for token in header_value.split(","))


class ReportCache:
    def __init__(self, cache_path: Path | None = CACHE_PATH):
        self.cache_path = cache_path
        self.state_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()
        self.body: bytes | None = None
        self.gzip_body: bytes | None = None
        self.loaded_at = 0.0
        self.etag = ""
        self.refresh_in_progress = False
        self.last_refresh_duration: float | None = None
        self.last_error = ""
        self.scheduler_thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.state_lock:
            age = int(time.monotonic() - self.loaded_at) if self.body is not None else None
            return {
                "body": self.body,
                "gzip_body": self.gzip_body,
                "etag": self.etag,
                "age": age,
                "refresh_in_progress": self.refresh_in_progress,
                "last_refresh_duration": self.last_refresh_duration,
                "last_error": self.last_error,
            }

    def _install_body(self, body: bytes, age_seconds: float = 0) -> None:
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        with self.state_lock:
            self.body = body
            self.gzip_body = compressed
            self.loaded_at = time.monotonic() - max(0, age_seconds)
            self.etag = 'W/"' + hashlib.sha256(body).hexdigest() + '"'
        self.ready_event.set()

    def _persist(self, body: bytes) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.cache_path)

    def load_disk(self) -> bool:
        if self.cache_path is None or not self.cache_path.is_file():
            return False
        age = max(0, time.time() - self.cache_path.stat().st_mtime)
        if age > STALE_IF_ERROR_SECONDS:
            print(
                json.dumps({"event": "campaign_copy_report_disk_cache_ignored", "age_seconds": int(age)}),
                flush=True,
            )
            return False
        try:
            original = self.cache_path.read_bytes()
            body = validate_or_convert_cache_body(original)
            self._install_body(body, age)
            if body != original:
                self._persist(body)
            print(
                json.dumps(
                    {
                        "event": "campaign_copy_report_disk_cache_loaded",
                        "age_seconds": int(age),
                        "bytes": len(body),
                        "gzip_bytes": len(self.gzip_body or b""),
                    }
                ),
                flush=True,
            )
            return True
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "campaign_copy_report_disk_cache_failed", "error_type": type(exc).__name__}
                ),
                file=sys.stderr,
                flush=True,
            )
            return False

    def refresh(self, wait_for_lock: bool = True) -> bool:
        acquired = self.refresh_lock.acquire(blocking=wait_for_lock)
        if not acquired:
            return False
        started = time.monotonic()
        with self.state_lock:
            self.refresh_in_progress = True
            self.last_error = ""
        try:
            payload = build_payload(query_raw())
            body = serialize_wire_payload(payload)
            self._persist(body)
            self._install_body(body)
            duration = time.monotonic() - started
            with self.state_lock:
                self.last_refresh_duration = duration
                print(
                    json.dumps(
                        {
                            "event": "campaign_copy_report_refresh",
                            "seconds": round(duration, 3),
                            "bytes": len(body),
                            "gzip_bytes": len(self.gzip_body or b""),
                            "campaigns": len(payload["campaigns"]),
                            "extra_entities": len(payload.get("extra_entities", [])),
                            "daily_rows": len(payload["daily"]) + len(payload.get("extra_daily", [])),
                            "copy_pipelines": payload.get("copy_pipelines", {}),
                            "generated_at": payload["meta"]["generated_at"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            return True
        except Exception as exc:
            snapshot = self.snapshot()
            with self.state_lock:
                self.last_error = type(exc).__name__
            print(
                json.dumps(
                    {
                        "event": "campaign_copy_report_refresh_failed",
                        "error_type": type(exc).__name__,
                        "cache_age_seconds": snapshot["age"] if snapshot["body"] is not None else -1,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            return False
        finally:
            with self.state_lock:
                self.refresh_in_progress = False
            self.ready_event.set()
            self.refresh_lock.release()

    def _scheduler_loop(self) -> None:
        delay = 0
        while not self.stop_event.wait(delay):
            ok = self.refresh(wait_for_lock=True)
            delay = CACHE_TTL_SECONDS if ok else CACHE_REFRESH_RETRY_SECONDS

    def start(self) -> None:
        self.load_disk()
        self.scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="campaign-copy-report-refresh",
            daemon=True,
        )
        self.scheduler_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.scheduler_thread is not None:
            self.scheduler_thread.join(timeout=1)

    def trigger_refresh(self) -> None:
        threading.Thread(
            target=lambda: self.refresh(wait_for_lock=False),
            name="campaign-copy-report-refresh-trigger",
            daemon=True,
        ).start()

    def get(self) -> tuple[bytes, bytes, str, str, int]:
        snapshot = self.snapshot()
        if snapshot["body"] is not None:
            if snapshot["age"] >= CACHE_TTL_SECONDS:
                self.trigger_refresh()
            state = "stale" if snapshot["age"] >= CACHE_TTL_SECONDS else "hit"
            return snapshot["body"], snapshot["gzip_body"], snapshot["etag"], state, snapshot["age"]
        self.ready_event.clear()
        snapshot = self.snapshot()
        if snapshot["body"] is not None:
            return snapshot["body"], snapshot["gzip_body"], snapshot["etag"], "hit", snapshot["age"]
        self.trigger_refresh()
        self.ready_event.wait(timeout=180)
        snapshot = self.snapshot()
        if snapshot["body"] is None:
            raise RuntimeError("report cache unavailable")
        return snapshot["body"], snapshot["gzip_body"], snapshot["etag"], "miss", snapshot["age"]


CACHE = ReportCache()


class Handler(BaseHTTPRequestHandler):
    server_version = "CampaignCopyReport/2.0"

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")

    def _send(
        self,
        status: int,
        content_type: str,
        body: bytes = b"",
        send_body: bool = True,
        headers=None,
        cache_control: str = "private, no-store",
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _route(self, send_body: bool):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                INDEX_PATH.read_bytes(),
                send_body,
                cache_control="private, max-age=300",
            )
            return
        if path == "/healthz":
            snapshot = CACHE.snapshot()
            body = json.dumps(
                {
                    "ok": True,
                    "cache_ready": snapshot["body"] is not None,
                    "cache_age_seconds": snapshot["age"],
                    "refresh_in_progress": snapshot["refresh_in_progress"],
                    "last_refresh_duration_seconds": snapshot["last_refresh_duration"],
                    "last_error": snapshot["last_error"] or None,
                    "refresh_interval_seconds": CACHE_TTL_SECONDS,
                    "persistent_cache": str(CACHE.cache_path) if CACHE.cache_path else None,
                },
                separators=(",", ":"),
            ).encode()
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body, send_body)
            return
        if path == "/api/data":
            try:
                body, compressed, etag, cache_state, cache_age = CACHE.get()
                common_headers = [
                    ("ETag", etag),
                    ("Vary", "Accept-Encoding"),
                    ("X-Report-Cache", cache_state),
                    ("X-Report-Cache-Age", str(cache_age)),
                ]
                cache_control = "private, max-age=60, stale-while-revalidate=840"
                if etag_matches(self.headers.get("If-None-Match", ""), etag):
                    self._send(
                        HTTPStatus.NOT_MODIFIED,
                        "application/json; charset=utf-8",
                        send_body=False,
                        headers=common_headers,
                        cache_control=cache_control,
                    )
                    return
                if "gzip" in self.headers.get("Accept-Encoding", "").lower():
                    body = compressed
                    common_headers.append(("Content-Encoding", "gzip"))
                self._send(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    body,
                    send_body,
                    common_headers,
                    cache_control,
                )
            except Exception:
                body = json.dumps({"error": "report_data_unavailable"}).encode()
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json; charset=utf-8", body, send_body)
            return
        body = json.dumps({"error": "not_found"}).encode()
        self._send(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", body, send_body)

    def do_GET(self):
        self._route(True)

    def do_HEAD(self):
        self._route(False)

    def log_message(self, fmt: str, *args):
        message = fmt % args
        print(json.dumps({"event": "http", "client": self.client_address[0], "message": message}), flush=True)


def self_test() -> int:
    raw = {
        "safety": {"is_read_only": 1, "server_time": "2026-07-20 12:00:00", "session_time_zone": "+08:00"},
        "copy_status_counts": [
            {"level": 0, "status": 1, "n": 1},
            {"level": 1, "status": 1, "n": 1},
            {"level": 2, "status": 2, "n": 2},
        ],
        "insight_max_dt": "2026-07-20",
        "logs": [
            {
                "id": 1,
                "new_id": "1001",
                "origin_id": "9001",
                "account_id": "act_1",
                "queue_id": 7,
                "user_id": 42,
                "user_name": "测试用户",
                "app_id": 9,
                "app_name": "测试产品",
                "content_id": "88",
                "created_at": "2026-07-20 08:00:00",
                "rule_name": "测试规则",
            }
        ],
        "mapping": [
            {
                "campaign_id": "1001",
                "ad_id": "2001",
                "campaign_name": "测试 Campaign",
                "product": "测试产品",
                "country": "US",
                "language": "en",
            }
        ],
        "daily": [
            {
                "campaign_id": "1001",
                "dt": "2026-07-20",
                "spend": 10,
                "af_revenue0": 5,
                "af_installs": 2,
                "impressions": 100,
                "clicks": 10,
                "source_rows": 1,
            }
        ],
        "extra_logs": [
            {
                "id": 2,
                "level": 1,
                "new_id": "3001",
                "origin_id": "2901",
                "account_id": "act_2",
                "queue_id": 8,
                "user_id": 43,
                "user_name": "测试操作人",
                "app_id": 10,
                "app_name": "测试产品二",
                "content_id": "99",
                "created_at": "2026-07-20 09:00:00",
                "rule_name": "Ad Set 规则",
            }
        ],
        "extra_mapping": [
            {
                "level": 1,
                "entity_id": "3001",
                "campaign_id": "1002",
                "campaign_name": "父 Campaign",
                "adset_id": "3001",
                "adset_name": "测试 Ad Set",
                "ad_id": "4001",
                "ad_name": "测试 Ad",
                "product": "测试产品二",
                "country": "JP",
                "language": "ja",
            }
        ],
        "extra_daily": [
            {
                "ad_id": "4001",
                "dt": "2026-07-20",
                "spend": 4,
                "af_revenue0": 1,
                "af_installs": 3,
                "impressions": 50,
                "clicks": 5,
                "source_rows": 1,
            }
        ],
    }
    payload = build_payload(raw)
    assert payload["meta"]["today"] == "2026-07-20"
    assert payload["campaigns"][0]["user_label"] == "测试用户（ID 42）"
    assert payload["daily"][0]["spend"] == 10
    assert payload["copy_pipeline"]["terminal_success_rate"] == 1.0
    assert payload["copy_pipelines"]["1"]["success"] == 1
    assert payload["copy_pipelines"]["2"]["failed"] == 2
    assert payload["extra_entities"][0]["entity_name"] == "测试 Ad Set"
    assert payload["extra_daily"][0]["spend"] == 4
    wire = encode_wire_payload(payload)
    assert wire["v"] == 2
    assert wire["c"][0][CAMPAIGN_FIELDS.index("user_label")] == "测试用户（ID 42）"
    assert wire["d"][0][DAILY_FIELDS.index("spend")] == 10
    assert wire["x"][0][EXTRA_ENTITY_FIELDS.index("entity_id")] == "3001"
    assert wire["xd"][0][EXTRA_DAILY_FIELDS.index("spend")] == 4
    print(json.dumps({"ok": True, "wire_version": 2, "campaigns": 1, "extra_entities": 1, "daily_rows": 2}))
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if not INDEX_PATH.is_file():
        raise RuntimeError(f"missing frontend: {INDEX_PATH}")
    CACHE.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        json.dumps(
            {
                "event": "server_start",
                "host": HOST,
                "port": PORT,
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
                "persistent_cache": str(CACHE.cache_path) if CACHE.cache_path else None,
                "background_refresh": True,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        CACHE.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

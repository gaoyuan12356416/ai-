#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the TT mini-program multi-dimensional static dashboard."""

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import opera_product_daily_dashboard as base


ROOT = Path(__file__).resolve().parent
WEB_DIR = Path("/usr/share/nginx/html/reports/tt-minis-native-growth")
CACHE_DB = ROOT / "data" / "tt_minis_multi_dim_dashboard_tti_app_revenue_cache.sqlite3"
PUBLIC_URL = "https://ai.yingliangads.com/reports/tt-minis-native-growth/"
BJ_OFFSET_HOURS = 8
DEFAULT_DAYS = 60
CACHE_REFRESH_DAYS = 2
CACHE_RETENTION_DAYS = 60
SAMPLE_VALIDATION_DAYS = 3
SAMPLE_VALIDATION_TOP_ADS = 200
DRAMAWAVE_TT_MINIS_PRODUCT_IDS = ("1479", "3346")
DRAMAWAVE_TT_MINIS_ID = "mn1yi38ikcrqhitt"
DEFAULT_METRIC_LEVEL = "campaign"
METRIC_LEVELS = {
    "campaign": {"label": "按campaign层级数据计算", "category": 0, "join_key": "campaign_id"},
    "ad": {"label": "按ad层级数据计算", "category": 2, "join_key": "ad_id"},
}
TIKTOK_INSIGHT_CATEGORY = METRIC_LEVELS["ad"]["category"]
TIKTOK_INSIGHT_PRODUCTS = ("dramawaveminis", "Dramawave")
APP_REVENUE_CHUNK_SIZE = 500


DIMENSIONS = [
    {"key": "dt", "label": "日期"},
    {"key": "optimizer_name", "label": "优化师"},
    {"key": "ad_account_id", "label": "账户"},
    {"key": "app_id", "label": "小程序"},
    {"key": "minis_id", "label": "minis_id"},
    {"key": "series_code", "label": "短剧"},
    {"key": "data_source_id", "label": "content_id"},
    {"key": "resource_name", "label": "资源"},
    {"key": "country", "label": "国家"},
    {"key": "language", "label": "语言"},
    {"key": "drama_language", "label": "剧语言"},
    {"key": "campaign_id", "label": "Campaign"},
    {"key": "adset_id", "label": "Adgroup"},
    {"key": "ad_id", "label": "Ad"},
    {"key": "resource_type", "label": "资源类型"},
    {"key": "source_type", "label": "素材类型"},
    {"key": "bid_type", "label": "出价类型"},
    {"key": "status", "label": "状态"},
]

METRICS = [
    {"key": "spend", "label": "花费", "format": "money"},
    {"key": "revenue", "label": "IAA回收", "format": "money"},
    {"key": "roas", "label": "ROAS", "format": "ratio"},
    {"key": "installs", "label": "Install", "format": "int"},
    {"key": "cpi", "label": "CPI", "format": "money"},
    {"key": "impressions", "label": "曝光", "format": "int"},
    {"key": "clicks", "label": "点击", "format": "int"},
    {"key": "ctr", "label": "CTR", "format": "pct"},
    {"key": "ad_impression", "label": "广告展示", "format": "int"},
]

ROW_COLUMNS = [
    "dt",
    "optimizer_name",
    "ad_account_id",
    "app_id",
    "minis_id",
    "series_code",
    "data_source_id",
    "resource_id",
    "resource_name",
    "country",
    "language",
    "drama_language",
    "campaign_id",
    "adset_id",
    "ad_id",
    "resource_type",
    "source_type",
    "bid_type",
    "status",
    "spend",
    "revenue",
    "installs",
    "impressions",
    "clicks",
    "ad_impression",
    "row_count",
    "roas",
    "cpi",
    "ctr",
]

NUMERIC_COLUMNS = {
    "spend",
    "revenue",
    "installs",
    "impressions",
    "clicks",
    "ad_impression",
    "row_count",
    "roas",
    "cpi",
    "ctr",
}

DICT_COLUMNS = [col for col in ROW_COLUMNS if col not in NUMERIC_COLUMNS]

SOURCE_COLUMNS = [
    "metric_level",
    "dt",
    "optimizer_id",
    "optimizer_name",
    "ad_account_id",
    "app_id",
    "product",
    "minis_id",
    "series_code",
    "data_source_id",
    "resource_id",
    "resource_name",
    "country",
    "country_group",
    "language",
    "drama_language",
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "resource_type",
    "source_type",
    "bid_type",
    "status",
    "op_status",
    "ad_created_at",
    "spend",
    "revenue",
    "installs",
    "impressions",
    "clicks",
    "ad_impression",
    "row_count",
    "roas",
    "cpi",
    "ctr",
]


def bj_now():
    return datetime.utcnow() + timedelta(hours=BJ_OFFSET_HOURS)


def dec(value):
    if value in (None, "", "NULL"):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def q2(value):
    return dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def num(value):
    return float(q2(value))


def int_num(value):
    return int(dec(value))


def sql_quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_in(values, numeric=False):
    vals = [str(v) for v in values if str(v).strip()]
    if not vals:
        return "(NULL)"
    if numeric:
        clean = [v for v in vals if v.isdigit()]
        return "(" + ",".join(clean or ["0"]) + ")"
    return "(" + ",".join(sql_quote(v) for v in vals) + ")"


def normalize_metric_level(metric_level):
    level = str(metric_level or DEFAULT_METRIC_LEVEL).strip().lower()
    if level not in METRIC_LEVELS:
        raise ValueError("unsupported metric level: %s" % metric_level)
    return level


def compact_date(value):
    return str(value or "").replace("-", "")


def chunked(values, size):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def detect_audio_type(name):
    text = str(name or "").lower()
    dubbed_words = [
        "doblado",
        "dublado",
        "dubbed",
        "doublé",
        "sulih suara",
        "alih suara",
        "synchron",
        "doppiato",
        "dublaj",
        "дубляж",
        "พากย์",
        "吹き替え",
        "더빙",
    ]
    return 1 if any(word in text for word in dubbed_words) else 0


def parse_series_code(*names):
    for name in names:
        text = str(name or "")
        before = re.split(r"-tt-minis", text, maxsplit=1, flags=re.I)[0]
        nums = re.findall(r"(\d{4,})", before)
        if nums:
            return nums[-1]
    for name in names:
        nums = re.findall(r"(?:^|[-_])(\d{4,})(?:[-_]|$)", str(name or ""))
        if nums:
            return nums[0]
    return "未填"


def date_range(days, start_date=None, end_date=None):
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        end = bj_now().date()
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    else:
        start = end - timedelta(days=max(1, days) - 1)
    if start > end:
        raise ValueError("start_date must be <= end_date")
    return start.isoformat(), end.isoformat()


def minis_ad_scope_sql():
    # Keep the minis scope anchored on the small publish queue set first. This
    # avoids scanning the full auto-created table before the minis filter.
    return """
      SELECT
        CAST(ac.ad_id AS UNSIGNED) AS ad_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.product_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS product_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.user_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ac_user_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.ad_account_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ad_account_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.campaign_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS campaign_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.adset_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS adset_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(q.minis_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS minis_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(COALESCE(q.user_id, ac.user_id, 0) AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS publish_user_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.campaign_name, ''), CONCAT('campaign_', ac.campaign_id)) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS campaign_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.adset_name, ''), CONCAT('adgroup_', ac.adset_id)) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS adset_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.ad_name, ''), CONCAT('ad_', ac.ad_id)) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ad_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.source_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS source_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.original_source_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS original_source_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.material_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS material_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.country, ''), '未填') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS country,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.language, ''), '未填') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS drama_language,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.bid_type, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS bid_type,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.status, ''), 'UNKNOWN') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS status,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.op_status, ''), 'UNKNOWN') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS op_status,
        SUBSTRING_INDEX(GROUP_CONCAT(DATE_FORMAT(ac.created_at, '%Y-%m-%d %H:%i:%s') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ad_created_at
      FROM (
        SELECT id, user_id, minis_id
        FROM kunlunads_dev.tiktok_publish_template_queue
        WHERE minis_id IS NOT NULL
          AND TRIM(minis_id) <> ''
      ) q
      STRAIGHT_JOIN kunlunads_dev.ads_tiktok_auto_created_data ac
        ON ac.publish_queue_id = q.id
      WHERE ac.ad_id IS NOT NULL
        AND TRIM(ac.ad_id) <> ''
        AND ac.ad_id REGEXP '^[0-9]+$'
        AND ac.product_id IN {product_ids}
        AND q.minis_id = {minis_id}
      GROUP BY CAST(ac.ad_id AS UNSIGNED)
    """.format(
        product_ids=sql_in(DRAMAWAVE_TT_MINIS_PRODUCT_IDS, numeric=True),
        minis_id=sql_quote(DRAMAWAVE_TT_MINIS_ID),
    )


def minis_campaign_scope_sql():
    return """
      SELECT
        CAST(ac.campaign_id AS UNSIGNED) AS campaign_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.product_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS product_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.user_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ac_user_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(ac.ad_account_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ad_account_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(q.minis_id AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS minis_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(COALESCE(q.user_id, ac.user_id, 0) AS CHAR) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS publish_user_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.campaign_name, ''), CONCAT('campaign_', ac.campaign_id)) ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS campaign_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.source_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS source_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.original_source_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS original_source_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.material_id, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS material_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.country, ''), '未填') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS country,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.language, ''), '未填') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS drama_language,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.bid_type, ''), '') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS bid_type,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.status, ''), 'UNKNOWN') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS status,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ac.op_status, ''), 'UNKNOWN') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS op_status,
        SUBSTRING_INDEX(GROUP_CONCAT(DATE_FORMAT(ac.created_at, '%Y-%m-%d %H:%i:%s') ORDER BY ac.created_at DESC, ac.id DESC SEPARATOR '||'), '||', 1) AS ad_created_at,
        COUNT(DISTINCT CAST(ac.adset_id AS CHAR)) AS scoped_adgroups,
        COUNT(DISTINCT CAST(ac.ad_id AS CHAR)) AS scoped_ads
      FROM (
        SELECT id, user_id, minis_id
        FROM kunlunads_dev.tiktok_publish_template_queue
        WHERE minis_id IS NOT NULL
          AND TRIM(minis_id) <> ''
      ) q
      STRAIGHT_JOIN kunlunads_dev.ads_tiktok_auto_created_data ac
        ON ac.publish_queue_id = q.id
      WHERE ac.campaign_id IS NOT NULL
        AND TRIM(ac.campaign_id) <> ''
        AND ac.campaign_id REGEXP '^[0-9]+$'
        AND ac.product_id IN {product_ids}
        AND q.minis_id = {minis_id}
      GROUP BY CAST(ac.campaign_id AS UNSIGNED)
    """.format(
        product_ids=sql_in(DRAMAWAVE_TT_MINIS_PRODUCT_IDS, numeric=True),
        minis_id=sql_quote(DRAMAWAVE_TT_MINIS_ID),
    )


def fetch_content_mapping(rows):
    keys = sorted(
        {
            (str(row.get("series_code") or ""), str(row.get("drama_language") or ""), int(row.get("_audio_type") or 0))
            for row in rows
            if str(row.get("series_code") or "") not in ("", "未填")
        }
    )
    if not keys:
        return {}
    series_codes = sorted({key[0] for key in keys})
    mapping = {}
    for start in range(0, len(series_codes), 300):
        part = series_codes[start : start + 300]
        sql = f"""
        SELECT
          series_code,
          language,
          audio_type,
          SUBSTRING_INDEX(GROUP_CONCAT(content_id ORDER BY updated_at DESC, id DESC SEPARATOR '||'), '||', 1) AS content_id,
          SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(name, ''), content_id) ORDER BY updated_at DESC, id DESC SEPARATOR '||'), '||', 1) AS name,
          COUNT(DISTINCT content_id) AS content_count
        FROM kunlunads_dev.ads_drama_info
        WHERE app = 'com.dramawave.app'
          AND series_code IN {sql_in(part)}
          AND content_id <> ''
        GROUP BY series_code, language, audio_type
        """
        for series_code, language, audio_type, content_id, name, content_count in base.run_mysql(sql, timeout=120):
            if int_num(content_count) == 1:
                mapping[(str(series_code), str(language), int_num(audio_type))] = {
                    "content_id": str(content_id or ""),
                    "name": str(name or ""),
                }
    return mapping


def fetch_app_revenue_users(rows, start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    key_col = "campaign_id" if metric_level == "campaign" else "ad_id"
    keys = sorted({str(row.get(key_col) or "") for row in rows if str(row.get(key_col) or "").isdigit()})
    if not keys:
        return {}
    users = {}
    start_dt = compact_date(start_date)
    end_dt = compact_date(end_date)
    index_hint = "campaign_id" if metric_level == "campaign" else "ad_id"
    for part in chunked(keys, APP_REVENUE_CHUNK_SIZE):
        sql = f"""
        SELECT
          CONCAT(SUBSTRING(dt, 1, 4), '-', SUBSTRING(dt, 5, 2), '-', SUBSTRING(dt, 7, 2)) AS stat_day,
          CAST({key_col} AS CHAR) AS metric_key,
          SUM(users) AS users
        FROM kunlunads_dev.ads_app_revenues FORCE INDEX({index_hint})
        WHERE dt BETWEEN {sql_quote(start_dt)} AND {sql_quote(end_dt)}
          AND {key_col} IN {sql_in(part)}
        GROUP BY stat_day, CAST({key_col} AS CHAR)
        """
        for stat_day, metric_key, app_users in base.run_mysql(sql, timeout=180):
            users[(str(stat_day), str(metric_key))] = dec(app_users)
    return users


def fetch_rows_from_source(start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    if metric_level == "campaign":
        return fetch_campaign_rows_from_source(start_date, end_date)
    return fetch_ad_rows_from_source(start_date, end_date)


def fetch_ad_rows_from_source(start_date, end_date):
    cols = SOURCE_COLUMNS[:-3]
    sql = f"""
    SELECT
      'ad' AS metric_level,
      DATE_FORMAT(i.start_date, '%Y-%m-%d') AS dt,
      CAST(COALESCE(m.publish_user_id, m.ac_user_id, 0) AS CHAR) AS optimizer_id,
      COALESCE(NULLIF(au.name, ''), NULLIF(au.username, ''), CONCAT('user_', CAST(COALESCE(m.publish_user_id, m.ac_user_id, 0) AS CHAR))) AS optimizer_name,
      CAST(COALESCE(NULLIF(i.advertiser_id, ''), m.ad_account_id) AS CHAR) AS ad_account_id,
      CASE WHEN m.product_id = '3346' THEN 'dramawaveminis' ELSE 'Dramawave' END AS app_id,
      'Dramawave' AS product,
      COALESCE(NULLIF(m.minis_id, ''), '未填') AS minis_id,
      '未填' AS series_code,
      '未填' AS data_source_id,
      COALESCE(NULLIF(m.original_source_id, ''), NULLIF(m.material_id, ''), NULLIF(m.source_id, ''), '未填') AS resource_id,
      COALESCE(NULLIF(m.ad_name, ''), CONCAT('ad_', CAST(i.ad_id AS CHAR))) AS resource_name,
      COALESCE(NULLIF(m.country, ''), NULLIF(i.country_id, ''), '未填') AS country,
      COALESCE(NULLIF(i.country_id, ''), '未填') AS country_group,
      COALESCE(NULLIF(i.language, ''), 'none') AS language,
      COALESCE(NULLIF(m.drama_language, ''), '未填') AS drama_language,
      CAST(COALESCE(NULLIF(i.campaign_id, ''), m.campaign_id) AS CHAR) AS campaign_id,
      COALESCE(NULLIF(m.campaign_name, ''), NULLIF(i.ads_name, ''), CONCAT('campaign_', CAST(i.campaign_id AS CHAR))) AS campaign_name,
      CAST(COALESCE(NULLIF(i.adgroup_id, ''), m.adset_id) AS CHAR) AS adset_id,
      COALESCE(NULLIF(m.adset_name, ''), CONCAT('adgroup_', CAST(COALESCE(NULLIF(i.adgroup_id, ''), m.adset_id) AS CHAR))) AS adset_name,
      CAST(i.ad_id AS CHAR) AS ad_id,
      COALESCE(NULLIF(m.ad_name, ''), CONCAT('ad_', CAST(i.ad_id AS CHAR))) AS ad_name,
      COALESCE(NULLIF(m.country, ''), '未填') AS resource_type,
      'tt_minis' AS source_type,
      COALESCE(NULLIF(m.bid_type, ''), '未填') AS bid_type,
      COALESCE(NULLIF(m.status, ''), 'UNKNOWN') AS status,
      COALESCE(NULLIF(m.op_status, ''), 'UNKNOWN') AS op_status,
      COALESCE(NULLIF(m.ad_created_at, ''), '') AS ad_created_at,
      ROUND(i.stat_cost, 6) AS spend,
      ROUND(i.ad_impression_value, 6) AS revenue,
      0 AS installs,
      i.show_cnt AS impressions,
      i.click_cnt AS clicks,
      i.ad_impression AS ad_impression,
      i.insight_rows AS row_count
    FROM (
      SELECT
        start_date,
        CAST(ad_id AS UNSIGNED) AS ad_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(advertiser_id AS CHAR) ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS advertiser_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(campaign_id AS CHAR) ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS campaign_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(adgroup_id AS CHAR) ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS adgroup_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ads_name, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS ads_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(country_id, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS country_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(language, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS language,
        ROUND(SUM(stat_cost), 6) AS stat_cost,
        ROUND(SUM(ad_impression_value), 6) AS ad_impression_value,
        SUM(show_cnt) AS show_cnt,
        SUM(click_cnt) AS click_cnt,
        SUM(ad_impression) AS ad_impression,
        COUNT(*) AS insight_rows
      FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(pcsa)
      WHERE product IN {sql_in(TIKTOK_INSIGHT_PRODUCTS)}
        AND category = {TIKTOK_INSIGHT_CATEGORY}
        AND start_date BETWEEN {sql_quote(start_date)} AND {sql_quote(end_date)}
        AND ad_id <> 0
      GROUP BY start_date, CAST(ad_id AS UNSIGNED)
    ) i
    JOIN ({minis_ad_scope_sql()}) m
      ON m.ad_id = i.ad_id
    LEFT JOIN kunlunads_dev.admin_users au
      ON au.id = COALESCE(m.publish_user_id, m.ac_user_id, 0)
    ORDER BY i.start_date DESC, spend DESC
    """
    raw = base.run_mysql(sql, timeout=300)
    rows = []
    for item in raw:
        row = dict(zip(cols, item))
        row["series_code"] = parse_series_code(row.get("campaign_name"), row.get("adset_name"), row.get("ad_name"))
        row["_audio_type"] = detect_audio_type(row.get("campaign_name")) or detect_audio_type(row.get("adset_name"))
        spend = dec(row["spend"])
        revenue = dec(row["revenue"])
        installs = dec(row["installs"])
        impressions = dec(row["impressions"])
        clicks = dec(row["clicks"])
        row.update(
            {
                "spend": num(spend),
                "revenue": num(revenue),
                "installs": int_num(installs),
                "impressions": int_num(impressions),
                "clicks": int_num(clicks),
                "ad_impression": int_num(row["ad_impression"]),
                "row_count": int_num(row["row_count"]),
                "roas": num(revenue / spend) if spend else 0.0,
                "cpi": num(spend / installs) if installs else 0.0,
                "ctr": num(clicks / impressions) if impressions else 0.0,
            }
        )
        rows.append(row)
    content_mapping = fetch_content_mapping(rows)
    for row in rows:
        meta = content_mapping.get(
            (str(row.get("series_code") or ""), str(row.get("drama_language") or ""), int(row.get("_audio_type") or 0))
        )
        if meta:
            row["data_source_id"] = meta["content_id"] or "未填"
            row["resource_name"] = meta["name"] or row.get("resource_name") or "未填"
        row.pop("_audio_type", None)
    app_users_by_key = fetch_app_revenue_users(rows, start_date, end_date, "ad")
    for row in rows:
        installs = app_users_by_key.get((str(row.get("dt")), str(row.get("ad_id"))), Decimal("0"))
        spend = dec(row["spend"])
        row["installs"] = int_num(installs)
        row["cpi"] = num(spend / installs) if installs else 0.0
    return rows


def fetch_campaign_rows_from_source(start_date, end_date):
    cols = SOURCE_COLUMNS[:-3]
    sql = f"""
    SELECT
      'campaign' AS metric_level,
      DATE_FORMAT(i.start_date, '%Y-%m-%d') AS dt,
      CAST(COALESCE(m.publish_user_id, m.ac_user_id, 0) AS CHAR) AS optimizer_id,
      COALESCE(NULLIF(au.name, ''), NULLIF(au.username, ''), CONCAT('user_', CAST(COALESCE(m.publish_user_id, m.ac_user_id, 0) AS CHAR))) AS optimizer_name,
      CAST(COALESCE(NULLIF(i.advertiser_id, ''), m.ad_account_id) AS CHAR) AS ad_account_id,
      CASE WHEN m.product_id = '3346' THEN 'dramawaveminis' ELSE 'Dramawave' END AS app_id,
      'Dramawave' AS product,
      COALESCE(NULLIF(m.minis_id, ''), '未填') AS minis_id,
      '未填' AS series_code,
      '未填' AS data_source_id,
      COALESCE(NULLIF(m.original_source_id, ''), NULLIF(m.material_id, ''), NULLIF(m.source_id, ''), '未填') AS resource_id,
      COALESCE(NULLIF(m.campaign_name, ''), NULLIF(i.ads_name, ''), CONCAT('campaign_', CAST(i.campaign_id AS CHAR))) AS resource_name,
      COALESCE(NULLIF(m.country, ''), NULLIF(i.country_id, ''), '未填') AS country,
      COALESCE(NULLIF(i.country_id, ''), '未填') AS country_group,
      COALESCE(NULLIF(i.language, ''), 'none') AS language,
      COALESCE(NULLIF(m.drama_language, ''), '未填') AS drama_language,
      CAST(i.campaign_id AS CHAR) AS campaign_id,
      COALESCE(NULLIF(m.campaign_name, ''), NULLIF(i.ads_name, ''), CONCAT('campaign_', CAST(i.campaign_id AS CHAR))) AS campaign_name,
      'campaign层级不可用' AS adset_id,
      'campaign层级不可用' AS adset_name,
      'campaign层级不可用' AS ad_id,
      'campaign层级不可用' AS ad_name,
      COALESCE(NULLIF(m.country, ''), '未填') AS resource_type,
      'tt_minis' AS source_type,
      COALESCE(NULLIF(m.bid_type, ''), '未填') AS bid_type,
      COALESCE(NULLIF(m.status, ''), 'UNKNOWN') AS status,
      COALESCE(NULLIF(m.op_status, ''), 'UNKNOWN') AS op_status,
      COALESCE(NULLIF(m.ad_created_at, ''), '') AS ad_created_at,
      ROUND(i.stat_cost, 6) AS spend,
      ROUND(i.ad_impression_value, 6) AS revenue,
      0 AS installs,
      i.show_cnt AS impressions,
      i.click_cnt AS clicks,
      i.ad_impression AS ad_impression,
      i.insight_rows AS row_count
    FROM (
      SELECT
        start_date,
        CAST(campaign_id AS UNSIGNED) AS campaign_id,
        SUBSTRING_INDEX(GROUP_CONCAT(CAST(advertiser_id AS CHAR) ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS advertiser_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(ads_name, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS ads_name,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(country_id, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS country_id,
        SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(language, ''), '') ORDER BY stat_cost DESC SEPARATOR '||'), '||', 1) AS language,
        ROUND(SUM(stat_cost), 6) AS stat_cost,
        ROUND(SUM(ad_impression_value), 6) AS ad_impression_value,
        SUM(show_cnt) AS show_cnt,
        SUM(click_cnt) AS click_cnt,
        SUM(ad_impression) AS ad_impression,
        COUNT(*) AS insight_rows
      FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(pcsa)
      WHERE product IN {sql_in(TIKTOK_INSIGHT_PRODUCTS)}
        AND category = {METRIC_LEVELS["campaign"]["category"]}
        AND start_date BETWEEN {sql_quote(start_date)} AND {sql_quote(end_date)}
        AND campaign_id <> 0
      GROUP BY start_date, CAST(campaign_id AS UNSIGNED), COALESCE(NULLIF(country_id, ''), ''), COALESCE(NULLIF(language, ''), '')
    ) i
    JOIN ({minis_campaign_scope_sql()}) m
      ON CAST(m.campaign_id AS UNSIGNED) = i.campaign_id
    LEFT JOIN kunlunads_dev.admin_users au
      ON au.id = COALESCE(m.publish_user_id, m.ac_user_id, 0)
    ORDER BY i.start_date DESC, spend DESC
    """
    raw = base.run_mysql(sql, timeout=300)
    rows = []
    for item in raw:
        row = dict(zip(cols, item))
        row["series_code"] = parse_series_code(row.get("campaign_name"))
        row["_audio_type"] = detect_audio_type(row.get("campaign_name"))
        spend = dec(row["spend"])
        revenue = dec(row["revenue"])
        installs = dec(row["installs"])
        impressions = dec(row["impressions"])
        clicks = dec(row["clicks"])
        row.update(
            {
                "spend": num(spend),
                "revenue": num(revenue),
                "installs": int_num(installs),
                "impressions": int_num(impressions),
                "clicks": int_num(clicks),
                "ad_impression": int_num(row["ad_impression"]),
                "row_count": int_num(row["row_count"]),
                "roas": num(revenue / spend) if spend else 0.0,
                "cpi": num(spend / installs) if installs else 0.0,
                "ctr": num(clicks / impressions) if impressions else 0.0,
            }
        )
        rows.append(row)
    content_mapping = fetch_content_mapping(rows)
    for row in rows:
        meta = content_mapping.get(
            (str(row.get("series_code") or ""), str(row.get("drama_language") or ""), int(row.get("_audio_type") or 0))
        )
        if meta:
            row["data_source_id"] = meta["content_id"] or "未填"
            row["resource_name"] = meta["name"] or row.get("resource_name") or "未填"
        row.pop("_audio_type", None)
    app_users_by_key = fetch_app_revenue_users(rows, start_date, end_date, "campaign")
    for row in rows:
        installs = app_users_by_key.get((str(row.get("dt")), str(row.get("campaign_id"))), Decimal("0"))
        spend = dec(row["spend"])
        row["installs"] = int_num(installs)
        row["cpi"] = num(spend / installs) if installs else 0.0
    return rows


def cache_conn():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_cache_schema(conn):
    text_cols = [c for c in SOURCE_COLUMNS if c not in NUMERIC_COLUMNS]
    numeric_cols = [c for c in SOURCE_COLUMNS if c in NUMERIC_COLUMNS]
    col_defs = ["%s TEXT" % c for c in text_cols] + ["%s REAL" % c for c in numeric_cols]
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tt_minis_multi_dim_rows (
          %s,
          refreshed_at TEXT NOT NULL
        )
        """
        % ", ".join(col_defs)
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tt_minis_multi_dim_rows)").fetchall()}
    if "metric_level" not in existing_cols:
        conn.execute("ALTER TABLE tt_minis_multi_dim_rows ADD COLUMN metric_level TEXT NOT NULL DEFAULT 'ad'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tt_minis_multi_dim_rows_dt ON tt_minis_multi_dim_rows(dt)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tt_minis_multi_dim_rows_level_dt ON tt_minis_multi_dim_rows(metric_level, dt)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tt_minis_multi_dim_refresh_log (
          metric_level TEXT NOT NULL DEFAULT 'ad',
          dt TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          refreshed_at TEXT NOT NULL,
          PRIMARY KEY(metric_level, dt)
        )
        """
    )
    log_info = conn.execute("PRAGMA table_info(tt_minis_multi_dim_refresh_log)").fetchall()
    pk_cols = [row["name"] for row in sorted([row for row in log_info if row["pk"]], key=lambda row: row["pk"])]
    if pk_cols != ["metric_level", "dt"]:
        old_name = "tt_minis_multi_dim_refresh_log_legacy"
        conn.execute("ALTER TABLE tt_minis_multi_dim_refresh_log RENAME TO %s" % old_name)
        conn.execute(
            """
            CREATE TABLE tt_minis_multi_dim_refresh_log (
              metric_level TEXT NOT NULL DEFAULT 'ad',
              dt TEXT NOT NULL,
              row_count INTEGER NOT NULL,
              refreshed_at TEXT NOT NULL,
              PRIMARY KEY(metric_level, dt)
            )
            """
        )
        old_cols = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % old_name).fetchall()}
        metric_expr = "COALESCE(metric_level, 'ad')" if "metric_level" in old_cols else "'ad'"
        conn.execute(
            """
            INSERT OR IGNORE INTO tt_minis_multi_dim_refresh_log(metric_level, dt, row_count, refreshed_at)
            SELECT %s, dt, row_count, refreshed_at FROM %s
            """
            % (metric_expr, old_name)
        )
        conn.execute("DROP TABLE %s" % old_name)
    conn.commit()


def each_date(start_date, end_date):
    cur = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def cached_dates(conn, start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    rows = conn.execute(
        """
        SELECT dt FROM tt_minis_multi_dim_refresh_log
        WHERE metric_level = ? AND dt BETWEEN ? AND ?
        """,
        (metric_level, start_date, end_date),
    ).fetchall()
    return {row["dt"] for row in rows}


def missing_cache_dates(start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    with cache_conn() as conn:
        ensure_cache_schema(conn)
        have = cached_dates(conn, start_date, end_date, metric_level)
    return [day for day in each_date(start_date, end_date) if day not in have]


def refresh_cache(start_date, end_date, metric_levels=None):
    levels = [normalize_metric_level(level) for level in (metric_levels or METRIC_LEVELS.keys())]
    refreshed_at = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    results = []
    with cache_conn() as conn:
        ensure_cache_schema(conn)
        placeholders = ",".join(["?"] * (len(SOURCE_COLUMNS) + 1))
        insert_sql = "INSERT INTO tt_minis_multi_dim_rows (%s, refreshed_at) VALUES (%s)" % (
            ",".join(SOURCE_COLUMNS),
            placeholders,
        )
        for level in levels:
            rows = fetch_rows_from_source(start_date, end_date, level)
            by_date = collections.Counter(row["dt"] for row in rows)
            conn.execute(
                "DELETE FROM tt_minis_multi_dim_rows WHERE metric_level = ? AND dt BETWEEN ? AND ?",
                (level, start_date, end_date),
            )
            conn.executemany(
                insert_sql,
                [[row.get(col, "") for col in SOURCE_COLUMNS] + [refreshed_at] for row in rows],
            )
            for day in each_date(start_date, end_date):
                conn.execute(
                    """
                    INSERT INTO tt_minis_multi_dim_refresh_log(metric_level, dt, row_count, refreshed_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(metric_level, dt) DO UPDATE SET
                      row_count = excluded.row_count,
                      refreshed_at = excluded.refreshed_at
                    """,
                    (level, day, int(by_date.get(day, 0)), refreshed_at),
                )
            results.append({"metric_level": level, "start_date": start_date, "end_date": end_date, "rows": len(rows), "refreshed_at": refreshed_at})
        conn.commit()
    return results


def prune_cache(retention_days):
    cutoff = (bj_now().date() - timedelta(days=max(1, retention_days) - 1)).isoformat()
    with cache_conn() as conn:
        ensure_cache_schema(conn)
        row_deleted = conn.execute("DELETE FROM tt_minis_multi_dim_rows WHERE dt < ?", (cutoff,)).rowcount
        log_deleted = conn.execute("DELETE FROM tt_minis_multi_dim_refresh_log WHERE dt < ?", (cutoff,)).rowcount
        conn.commit()
    return {"cutoff": cutoff, "rows_deleted": row_deleted, "logs_deleted": log_deleted}


def ensure_cache_for_range(start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    with cache_conn() as conn:
        ensure_cache_schema(conn)
        have = cached_dates(conn, start_date, end_date, metric_level)
    missing = [day for day in each_date(start_date, end_date) if day not in have]
    if not missing:
        return []
    spans = []
    span_start = span_end = missing[0]
    prev = datetime.strptime(missing[0], "%Y-%m-%d").date()
    for day in missing[1:]:
        cur = datetime.strptime(day, "%Y-%m-%d").date()
        if cur == prev + timedelta(days=1):
            span_end = day
        else:
            spans.append((span_start, span_end))
            span_start = span_end = day
        prev = cur
    spans.append((span_start, span_end))
    results = []
    for s, e in spans:
        results.extend(refresh_cache(s, e, [metric_level]))
    return results


def fetch_rows_from_cache(start_date, end_date, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    with cache_conn() as conn:
        ensure_cache_schema(conn)
        raw = conn.execute(
            """
            SELECT %s
            FROM tt_minis_multi_dim_rows
            WHERE metric_level = ? AND dt BETWEEN ? AND ?
            ORDER BY dt DESC, spend DESC
            """
            % ",".join(SOURCE_COLUMNS),
            (metric_level, start_date, end_date),
        ).fetchall()
    rows = []
    for item in raw:
        row = dict(item)
        for col in NUMERIC_COLUMNS:
            if col in row:
                row[col] = int_num(row[col]) if col in {"installs", "impressions", "clicks", "ad_impression", "row_count"} else num(row[col])
        rows.append(row)
    return rows


def aggregate_totals(rows):
    spend = sum(dec(r["spend"]) for r in rows)
    revenue = sum(dec(r["revenue"]) for r in rows)
    installs = sum(dec(r["installs"]) for r in rows)
    impressions = sum(dec(r["impressions"]) for r in rows)
    clicks = sum(dec(r["clicks"]) for r in rows)
    ad_impression = sum(dec(r["ad_impression"]) for r in rows)
    return {
        "spend": num(spend),
        "revenue": num(revenue),
        "roas": num(revenue / spend) if spend else 0.0,
        "installs": int_num(installs),
        "cpi": num(spend / installs) if installs else 0.0,
        "impressions": int_num(impressions),
        "clicks": int_num(clicks),
        "ctr": num(clicks / impressions) if impressions else 0.0,
        "ad_impression": int_num(ad_impression),
        "accounts": len({r["ad_account_id"] for r in rows if r["ad_account_id"]}),
        "campaigns": len({r["campaign_id"] for r in rows if r["campaign_id"]}),
        "adgroups": len({r["adset_id"] for r in rows if r["adset_id"] and r["adset_id"] != "campaign层级不可用"}),
        "ads": len({r["ad_id"] for r in rows if r["ad_id"] and r["ad_id"] != "campaign层级不可用"}),
    }


def rows_by_date(rows):
    out = collections.defaultdict(list)
    for row in rows:
        out[row["dt"]].append(row)
    return out


def daily_totals(rows):
    grouped = rows_by_date(rows)
    out = {}
    for day, day_rows in grouped.items():
        total = aggregate_totals(day_rows)
        total["rows"] = len(day_rows)
        out[day] = total
    return out


def sample_validation(rows):
    grouped = rows_by_date(rows)
    dates = sorted(grouped.keys(), reverse=True)[:SAMPLE_VALIDATION_DAYS]
    checks = []
    warnings = []
    for day in dates:
        day_rows = grouped[day]
        by_ad = {}
        for row in day_rows:
            ad_id = str(row["ad_id"])
            bucket = by_ad.setdefault(ad_id, {"spend": Decimal("0"), "revenue": Decimal("0"), "installs": Decimal("0")})
            bucket["spend"] += dec(row["spend"])
            bucket["revenue"] += dec(row["revenue"])
            bucket["installs"] += dec(row["installs"])
        top_ads = [
            ad_id for ad_id, _ in sorted(by_ad.items(), key=lambda kv: kv[1]["spend"], reverse=True)[:SAMPLE_VALIDATION_TOP_ADS]
        ]
        if not top_ads:
            continue
        source_spend = sum(by_ad[a]["spend"] for a in top_ads)
        source_revenue = sum(by_ad[a]["revenue"] for a in top_ads)
        source_installs = sum(by_ad[a]["installs"] for a in top_ads)
        check = {
            "date": day,
            "sample_ads": len(top_ads),
            "source_spend": num(source_spend),
            "source_revenue": num(source_revenue),
            "source_installs": int_num(source_installs),
        }
        try:
            ids_num = sql_in(top_ads, numeric=True)
            tti_sql = f"""
            SELECT
              ROUND(SUM(stat_cost), 6) AS spend,
              ROUND(SUM(ad_impression_value), 6) AS revenue
            FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(pcsa)
            WHERE product IN {sql_in(TIKTOK_INSIGHT_PRODUCTS)}
              AND start_date = {sql_quote(day)}
              AND category = {TIKTOK_INSIGHT_CATEGORY}
              AND ad_id IN {ids_num}
            """
            tti = base.run_mysql(tti_sql, timeout=120)
            tti_spend = dec(tti[0][0] if tti else 0)
            tti_revenue = dec(tti[0][1] if tti else 0)
            app_sql = f"""
            SELECT SUM(users) AS users
            FROM kunlunads_dev.ads_app_revenues FORCE INDEX(ad_id)
            WHERE dt = {sql_quote(compact_date(day))}
              AND ad_id IN {sql_in(top_ads)}
            """
            app = base.run_mysql(app_sql, timeout=120)
            app_users = dec(app[0][0] if app else 0)
            spend_diff = source_spend - tti_spend
            revenue_diff = source_revenue - tti_revenue
            install_diff = source_installs - app_users
            spend_base = max(abs(tti_spend), Decimal("1"))
            revenue_base = max(abs(tti_revenue), Decimal("1"))
            install_base = max(abs(app_users), Decimal("1"))
            spend_diff_pct = abs(spend_diff) / spend_base
            revenue_diff_pct = abs(revenue_diff) / revenue_base
            install_diff_pct = abs(install_diff) / install_base
            status = "ok"
            messages = []
            if abs(spend_diff) > Decimal("100") or spend_diff_pct > Decimal("0.01"):
                status = "warn"
                messages.append("花费样本差异 %.2f" % float(spend_diff))
            elif abs(spend_diff) > Decimal("0.02"):
                messages.append("花费样本小额差异 %.2f" % float(spend_diff))
            if abs(install_diff) > Decimal("1000") or install_diff_pct > Decimal("0.01"):
                status = "warn"
                messages.append("install样本差异 %s" % install_diff)
            elif abs(install_diff) > Decimal("0"):
                messages.append("install样本小额差异 %s" % install_diff)
            if abs(revenue_diff) > Decimal("100") or revenue_diff_pct > Decimal("0.01"):
                status = "warn"
                messages.append("IAA回收样本差异 %.2f" % float(revenue_diff))
            elif abs(revenue_diff) > Decimal("0.02"):
                messages.append("IAA回收样本小额差异 %.2f" % float(revenue_diff))
            check.update(
                {
                    "baseline_spend": num(tti_spend),
                    "baseline_revenue": num(tti_revenue),
                    "baseline_installs": int_num(app_users),
                    "spend_diff": num(spend_diff),
                    "revenue_diff": num(revenue_diff),
                    "install_diff": int_num(install_diff),
                    "spend_diff_pct": num(spend_diff_pct),
                    "revenue_diff_pct": num(revenue_diff_pct),
                    "install_diff_pct": num(install_diff_pct),
                    "status": status,
                    "message": "；".join(messages) if messages else "样本一致",
                }
            )
            if status != "ok":
                warnings.append("%s %s" % (day, check["message"]))
        except Exception as exc:
            check.update({"status": "error", "message": str(exc)[:300]})
            warnings.append("%s 校验失败：%s" % (day, str(exc)[:120]))
        checks.append(check)
    return {"type": "top_ad_sample", "checks": checks, "warnings": warnings}


def level_source_text(metric_level):
    metric_level = normalize_metric_level(metric_level)
    category = METRIC_LEVELS[metric_level]["category"]
    label = "campaign层级" if metric_level == "campaign" else "ad层级"
    install_key = "campaign_id" if metric_level == "campaign" else "ad_id"
    return "Dramawave TT小程序广告范围 + 本地缓存；花费/回收来自 ads_tiktok_insights category=%s %s，Install 来自 ads_app_revenues.%s 聚合（保留近60天，定时增量刷新近2天）" % (
        category,
        label,
        install_key,
    )


def build_payload(rows, start_date, end_date, validation, include_rows=True, metric_level=DEFAULT_METRIC_LEVEL):
    metric_level = normalize_metric_level(metric_level)
    generated_at = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    totals = aggregate_totals(rows)
    dictionaries = {}
    dictionary_index = {}
    for col in DICT_COLUMNS:
        values = sorted({str(row.get(col, "") or "") for row in rows})
        dictionaries[col] = values
        dictionary_index[col] = {value: idx for idx, value in enumerate(values)}
    compact_rows = []
    for row in rows:
        compact = []
        for col in ROW_COLUMNS:
            value = row.get(col, "")
            if col in dictionary_index:
                compact.append(dictionary_index[col].get(str(value or ""), 0))
            else:
                compact.append(value)
        compact_rows.append(compact)
    return {
        "meta": {
            "title": "TT小程序多维投放报表",
            "generated_at": generated_at,
            "start_date": start_date,
            "end_date": end_date,
            "public_url": PUBLIC_URL,
            "row_count": len(rows),
            "metric_level": metric_level,
            "metric_level_label": METRIC_LEVELS[metric_level]["label"],
            "source": level_source_text(metric_level),
            "metric_notes": {
                "spend": "SUM(ads_tiktok_insights.stat_cost)",
                "revenue": "SUM(ads_tiktok_insights.ad_impression_value)",
                "installs": "SUM(ads_app_revenues.users)",
                "roas": "SUM(ad_impression_value) / SUM(stat_cost)",
                "cpi": "SUM(stat_cost) / SUM(ads_app_revenues.users)",
            },
        },
        "default_level": DEFAULT_METRIC_LEVEL,
        "levels": {
            key: {
                "label": cfg["label"],
                "category": cfg["category"],
                "source": level_source_text(key),
                "disabled_dimensions": ["adset_id", "ad_id"] if key == "campaign" else [],
            }
            for key, cfg in METRIC_LEVELS.items()
        },
        "dimensions": DIMENSIONS,
        "metrics": METRICS,
        "columns": ROW_COLUMNS,
        "dict_columns": DICT_COLUMNS,
        "dicts": dictionaries,
        "totals": totals,
        "daily_totals": daily_totals(rows),
        "validation": validation,
        "rows": compact_rows if include_rows else [],
    }


def atomic_write(path, content, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if binary:
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def html_template(payload=None):
    boot = {}
    if payload:
        boot = {
            "meta": payload.get("meta", {}),
            "totals": payload.get("totals", {}),
            "daily_totals": payload.get("daily_totals", {}),
            "validation": payload.get("validation", {}),
            "default_level": payload.get("default_level", DEFAULT_METRIC_LEVEL),
            "levels": payload.get("levels", {}),
        }
    boot_json = json.dumps(boot, ensure_ascii=False, separators=(",", ":"))
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TT小程序多维投放报表</title>
<style>
:root{--bg:#f5f7fb;--panel:#fff;--line:#dce3ec;--text:#1d2430;--muted:#667085;--blue:#2357a5;--teal:#0f766e;--green:#07823f;--orange:#b75d00;--red:#c62828;--purple:#6d45a8;--shadow:0 1px 2px rgba(16,24,40,.06)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,"Microsoft YaHei",sans-serif}header{position:sticky;top:0;z-index:20;background:#172033;color:#fff;border-bottom:1px solid #0f1728;box-shadow:0 2px 8px rgba(0,0,0,.12)}.head{max-width:1680px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:16px}.title h1{font-size:19px;margin:0 0 4px}.title div{font-size:12px;color:#cbd5e1}.head-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.btn{height:34px;border:1px solid #cbd5e1;background:#fff;color:#172033;border-radius:7px;padding:0 11px;cursor:pointer;font-weight:600}.btn.primary{background:#2f6bc0;color:#fff;border-color:#2f6bc0}.wrap{max-width:1680px;margin:0 auto;padding:16px 18px 28px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow)}.controls{padding:13px;display:grid;grid-template-columns:repeat(6,minmax(150px,1fr));gap:10px;align-items:end}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}.field input,.field select{width:100%;height:34px;border:1px solid #c8d1de;border-radius:7px;background:#fff;padding:0 9px;color:var(--text)}.field input[type=number]{text-align:right}.preset{display:flex;gap:6px;flex-wrap:wrap}.preset button{height:28px;border:1px solid #cbd5e1;background:#f8fafc;border-radius:999px;padding:0 9px;cursor:pointer;color:#253044}.preset button.active{background:#dbeafe;border-color:#7db0f2;color:#174a8b}.kpis{display:grid;grid-template-columns:repeat(8,minmax(150px,1fr));gap:10px;margin:12px 0}.kpi{padding:13px}.kpi .label{font-size:12px;color:var(--muted);display:flex;align-items:center;justify-content:space-between}.kpi .value{font-size:23px;font-weight:750;margin-top:8px;white-space:nowrap}.kpi .sub{font-size:12px;color:var(--muted);margin-top:5px}.good{color:var(--green)!important}.mid{color:var(--orange)!important}.bad{color:var(--red)!important}.section{margin-top:14px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 8px}.section-title h2{font-size:15px;margin:0}.muted{color:var(--muted);font-size:12px}.chart{height:260px;padding:14px;overflow:hidden}.dim-box{padding:12px}.chips{display:flex;flex-wrap:wrap;gap:8px}.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid #cfd8e5;border-radius:999px;background:#fff;height:30px;padding:0 10px;font-size:12px;cursor:pointer}.chip input{margin:0}.chip.active{border-color:#2f6bc0;background:#edf5ff;color:#174a8b}.metric-toggles{display:flex;flex-wrap:wrap;gap:8px;margin-top:9px}.split{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.rank{padding:12px;min-height:260px}.rank h3{font-size:13px;margin:0 0 10px}.rank-row{display:grid;grid-template-columns:minmax(76px,1fr) 88px;gap:8px;align-items:center;margin:7px 0;font-size:12px}.bar{height:8px;background:#e9eef5;border-radius:99px;overflow:hidden;margin-top:4px}.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--teal))}.table-tools{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:11px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}.table-tools input{height:32px;min-width:260px;border:1px solid #cbd5e1;border-radius:7px;padding:0 9px}.table-wrap{overflow:auto;max-height:680px}table{width:100%;border-collapse:separate;border-spacing:0;background:#fff}th,td{border-bottom:1px solid #edf1f5;padding:8px 9px;font-size:12px;text-align:right;white-space:nowrap;vertical-align:middle}th{position:sticky;top:0;background:#eef3f9;color:#2c3646;z-index:5;cursor:pointer;font-weight:700}th:first-child,td:first-child{text-align:left}td.dim{text-align:left;color:#263446}.status{padding:10px 12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}.warn-box{border-left:4px solid var(--orange);padding:10px 12px;background:#fff8ed;color:#5f3500}.empty{padding:20px;color:var(--muted);text-align:center}.nowrap{white-space:nowrap}@media(max-width:1200px){.controls{grid-template-columns:repeat(3,1fr)}.kpis{grid-template-columns:repeat(4,1fr)}.split{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.head{align-items:flex-start;flex-direction:column}.controls{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}.split{grid-template-columns:1fr}.table-tools input{min-width:100%;width:100%}.kpi .value{font-size:19px}}
.level-bar{margin-bottom:12px;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.seg{display:flex;gap:6px;flex-wrap:wrap}.seg button{height:32px;border:1px solid #cbd5e1;background:#fff;border-radius:7px;padding:0 12px;cursor:pointer;font-weight:700;color:#253044}.seg button.active{background:#2f6bc0;border-color:#2f6bc0;color:#fff}.chip.disabled{opacity:.45;background:#f3f5f8;cursor:not-allowed}.chip.disabled input{cursor:not-allowed}
</style>
</head>
<body>
<header><div class="head"><div class="title"><h1>TT小程序多维投放报表</h1><div id="metaLine">加载中...</div></div><div class="head-actions"><button class="btn" id="refreshBtn">刷新数据</button><button class="btn primary" id="exportBtn">导出CSV</button></div></div></header>
<main class="wrap">
  <section class="panel level-bar">
    <div class="seg" id="levelButtons"></div>
    <div class="muted" id="levelHint">默认使用 campaign 层级数据</div>
  </section>
  <section class="panel controls">
    <div class="field"><label>起始日期</label><input type="date" id="startDate"></div>
    <div class="field"><label>结束日期</label><input type="date" id="endDate"></div>
    <div class="field"><label>优化师</label><select id="optimizerFilter"></select></div>
    <div class="field"><label>账户</label><select id="accountFilter"></select></div>
    <div class="field"><label>小程序</label><select id="appFilter"></select></div>
    <div class="field"><label>国家</label><select id="countryFilter"></select></div>
    <div class="field"><label>语言</label><select id="languageFilter"></select></div>
    <div class="field"><label>短剧</label><select id="seriesFilter"></select></div>
    <div class="field"><label>最低花费</label><input type="number" id="minSpend" step="1" placeholder="0"></div>
    <div class="field"><label>ROAS下限</label><input type="number" id="minRoas" step="0.01" placeholder="不限"></div>
    <div class="field"><label>CPI上限</label><input type="number" id="maxCpi" step="0.01" placeholder="不限"></div>
    <div class="field"><label>快捷日期</label><div class="preset" id="presets"></div></div>
  </section>

  <section class="kpis" id="kpis"></section>

  <section class="section panel">
    <div class="dim-box">
      <div class="section-title"><h2>自定义维度</h2><span class="muted">勾选维度后，下方透视表会按所选维度实时聚合</span></div>
      <div class="chips" id="dimensionChips"></div>
      <div class="metric-toggles" id="metricToggles"></div>
    </div>
  </section>

  <section class="section panel">
    <div class="section-title" style="padding:12px 12px 0"><h2>按日趋势</h2><span class="muted">花费、回收、ROAS、install、CPI</span></div>
    <div class="chart" id="trendChart"></div>
  </section>

  <section class="section split" id="rankings"></section>

  <section class="section panel">
    <div class="table-tools">
      <div><strong>自定义透视表</strong> <span class="muted" id="tableHint"></span></div>
      <input id="keyword" placeholder="搜索短剧、资源、campaign、ad_id、账户...">
    </div>
    <div class="table-wrap" id="pivotTable"></div>
    <div class="status" id="validationLine"></div>
  </section>
</main>
<script>
const BOOT_DATA=__BOOT_DATA__;
let DATA=null, controlsBound=false, renderTimer=null, loadingDates=false, loadedDates=new Set(), currentLevel=(BOOT_DATA.default_level||'campaign'), state={dims:['dt','optimizer_name'], metrics:['spend','revenue','roas','installs','cpi'], sortKey:'spend', sortDir:-1};
const dimLabels={}, metricLabels={}, metricFormats={};
function money(v){return '$'+Number(v||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function int(v){return Number(v||0).toLocaleString('en-US',{maximumFractionDigits:0})}
function ratio(v){return Number(v||0).toFixed(2)}
function pct(v){return (Number(v||0)*100).toFixed(2)+'%'}
function fmt(k,v){let f=metricFormats[k]; if(f==='money')return money(v); if(f==='int')return int(v); if(f==='ratio')return ratio(v); if(f==='pct')return pct(v); return String(v==null?'':v)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function roasClass(v){v=Number(v||0); return v>=1?'good':(v>=.8?'mid':'bad')}
function get(id){return document.getElementById(id)}
function parseDate(s){let p=String(s||'').split('-').map(Number); return new Date(p[0]||1970,(p[1]||1)-1,p[2]||1)}
function iso(d){let y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),day=String(d.getDate()).padStart(2,'0'); return `${y}-${m}-${day}`}
function addDays(s,n){let d=parseDate(s); d.setDate(d.getDate()+n); return iso(d)}
function bjToday(fallback){try{if(window.Intl&&Intl.DateTimeFormat){let fmt=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}); if(fmt.formatToParts){let parts=fmt.formatToParts(new Date()).reduce((a,p)=>(a[p.type]=p.value,a),{}); if(parts.year&&parts.month&&parts.day)return `${parts.year}-${parts.month}-${parts.day}`;}}}catch(e){} let d=new Date(Date.now()+8*3600*1000); let s=d.toISOString().slice(0,10); return fallback||s}
function unique(rows,key){return [...new Set(rows.map(r=>r[key]).filter(v=>v!==undefined&&v!==null&&String(v)!==''))].sort((a,b)=>String(a).localeCompare(String(b),'zh-Hans-CN'))}
function levelMeta(level=currentLevel){return ((DATA&&DATA.levels)||(BOOT_DATA&&BOOT_DATA.levels)||{})[level]||{}}
function disabledDims(){return new Set((levelMeta().disabled_dimensions)||[])}
function switchLevel(level){if(level===currentLevel||!((DATA&&DATA.levels)||{})[level])return; currentLevel=level; loadedDates=new Set(); DATA.rows=[]; state.dims=state.dims.filter(d=>!disabledDims().has(d)); if(!state.dims.length)state.dims=['dt']; buildLevelButtons(); initControls(true); loadData().catch(showLoadError)}
function buildLevelButtons(){let levels=(DATA&&DATA.levels)||(BOOT_DATA&&BOOT_DATA.levels)||{}, box=get('levelButtons'); if(!box)return; box.innerHTML=Object.keys(levels).map(k=>`<button type="button" data-level="${esc(k)}" class="${k===currentLevel?'active':''}">${esc(levels[k].label||k)}</button>`).join(''); box.querySelectorAll('button').forEach(btn=>btn.onclick=()=>switchLevel(btn.dataset.level)); let hint=get('levelHint'), info=levelMeta(); if(hint)hint.textContent=info.source||''}
function setDateRange(s,e){get('startDate').value=s; get('endDate').value=e}
function showQueryStatus(message='查询中，正在按当前筛选条件聚合...'){let hint=get('tableHint'), trend=get('trendChart'), table=get('pivotTable'), line=get('validationLine'); if(hint)hint.textContent=message; if(trend)trend.innerHTML=`<div class="empty">${esc(message)}</div>`; if(table)table.innerHTML=`<div class="empty">${esc(message)}</div>`; if(line)line.textContent=message;}
function scheduleRender(message){if(!DATA)return; showQueryStatus(message); if(renderTimer)clearTimeout(renderTimer); renderTimer=setTimeout(()=>{renderTimer=null; render();},20)}
function nextFrame(){return new Promise(resolve=>setTimeout(resolve,0))}
async function expandCompactRows(data){if(!(data.columns&&Array.isArray(data.rows)&&Array.isArray(data.rows[0])))return; let raw=data.rows, cols=data.columns, dicts=data.dicts||{}, dictCols=new Set(data.dict_columns||[]), out=new Array(raw.length), chunk=1200; for(let start=0;start<raw.length;start+=chunk){let end=Math.min(start+chunk,raw.length); for(let i=start;i<end;i++){let arr=raw[i], o={}; for(let j=0;j<cols.length;j++){let c=cols[j], v=arr[j]; if(dictCols.has(c)){let dv=(dicts[c]||[])[v]; o[c]=dv==null?'':dv;}else{o[c]=v;}} out[i]=o;} showQueryStatus(`正在展开明细 ${end.toLocaleString()} / ${raw.length.toLocaleString()} 行...`); await nextFrame();} data.rows=out;}
async function loadManifest(){showQueryStatus('正在加载索引...'); let res=await fetch('latest.json?v='+Date.now(),{cache:'no-store',credentials:'same-origin'}); if(!res.ok)throw new Error('HTTP '+res.status); let ct=res.headers.get('content-type')||''; if(!ct.includes('json')){let text=await res.text(); throw new Error(text.includes('login')||text.includes('登录')?'登录态失效，请重新打开页面完成飞书登录':'latest.json 返回的不是JSON：'+ct);} DATA=await res.json(); DATA.rows=[]; DATA.data_files=DATA.data_files||{}; currentLevel=DATA.default_level||currentLevel; DATA.dimensions.forEach(d=>dimLabels[d.key]=d.label); DATA.metrics.forEach(m=>{metricLabels[m.key]=m.label; metricFormats[m.key]=m.format}); buildLevelButtons(); initControls();}
function selectedDates(){let s=get('startDate').value,e=get('endDate').value,out=[]; if(!s||!e)return out; for(let d=s;d<=e;d=addDays(d,1))out.push(d); return out}
async function loadData(){if(loadingDates)return; loadingDates=true; try{if(!DATA)await loadManifest(); let dates=selectedDates(), missing=dates.filter(d=>!loadedDates.has(d)); if(!missing.length){scheduleRender('查询中，正在按当前筛选条件聚合...'); return;} showQueryStatus(`正在并行加载 ${missing.length} 天${levelMeta().label||currentLevel}明细...`); let version=encodeURIComponent((DATA.meta&&DATA.meta.generated_at)||''); let filesForLevel=(DATA.data_files||{})[currentLevel]||{}; let parts=await Promise.all(missing.map(async(day,i)=>{let file=(filesForLevel[day]||{}).path; if(!file)return {day,part:null}; let res=await fetch(file+'?v='+version,{cache:'default',credentials:'same-origin'}); if(!res.ok)throw new Error(day+' HTTP '+res.status); return {day,part:await res.json(),idx:i};})); for(let i=0;i<parts.length;i++){let item=parts[i]; if(!item.part){loadedDates.add(item.day); continue;} showQueryStatus(`正在展开 ${item.day} 明细 ${i+1} / ${parts.length}...`); await expandCompactRows(item.part); DATA.rows=DATA.rows.concat(item.part.rows||[]); loadedDates.add(item.day); await nextFrame();} initControls(true); scheduleRender('明细加载完成，正在按当前筛选条件聚合...');}finally{loadingDates=false;}}
function initControls(keepValues=false){let m=DATA.meta, today=bjToday(m.end_date), info=levelMeta(); buildLevelButtons(); get('metaLine').textContent=`${m.start_date} 至 ${m.end_date}｜更新 ${m.generated_at}｜当前口径 ${info.label||currentLevel}｜索引 ${Number(m.row_count||0).toLocaleString()} 行｜已加载 ${DATA.rows.length.toLocaleString()} 行｜${info.source||m.source}`; if(!get('startDate').value)get('startDate').value=today; if(!get('endDate').value)get('endDate').value=today; let vals={optimizerFilter:get('optimizerFilter').value,accountFilter:get('accountFilter').value,appFilter:get('appFilter').value,countryFilter:get('countryFilter').value,languageFilter:get('languageFilter').value,seriesFilter:get('seriesFilter').value}; fillSelect('optimizerFilter','全部优化师',unique(DATA.rows,'optimizer_name')); fillSelect('accountFilter','全部账户',unique(DATA.rows,'ad_account_id')); fillSelect('appFilter','全部小程序',unique(DATA.rows,'app_id')); fillSelect('countryFilter','全部国家',unique(DATA.rows,'country')); fillSelect('languageFilter','全部语言',unique(DATA.rows,'language')); fillSelect('seriesFilter','全部短剧',unique(DATA.rows,'series_code')); if(keepValues)Object.keys(vals).forEach(id=>{if([...get(id).options].some(o=>o.value===vals[id]))get(id).value=vals[id];}); buildPresets(m); buildChips(); if(!controlsBound){['startDate','endDate'].forEach(id=>get(id).addEventListener('change',()=>loadData().catch(showLoadError))); ['optimizerFilter','accountFilter','appFilter','countryFilter','languageFilter','seriesFilter','minSpend','minRoas','maxCpi','keyword'].forEach(id=>get(id).addEventListener('input',()=>scheduleRender('查询中，正在按当前筛选条件聚合...'))); controlsBound=true;}}
function bootTotalForRange(s,e){let daily=(BOOT_DATA&&BOOT_DATA.daily_totals)||{}, out={spend:0,revenue:0,installs:0,impressions:0,clicks:0,ad_impression:0,rows:0,accounts:0,campaigns:0,ads:0}; Object.keys(daily).forEach(day=>{if(day<s||day>e)return; let d=daily[day]||{}; ['spend','revenue','installs','impressions','clicks','ad_impression','rows','accounts','campaigns','ads'].forEach(k=>out[k]+=Number(d[k]||0));}); out.roas=out.spend?out.revenue/out.spend:0; out.cpi=out.installs?out.spend/out.installs:0; out.ctr=out.impressions?out.clicks/out.impressions:0; return out}
function renderBootKpis(t,label){let items=[['spend','花费',money(t.spend)],['revenue','IAA回收',money(t.revenue)],['roas','ROAS',ratio(t.roas),roasClass(t.roas)],['installs','Install',int(t.installs)],['cpi','CPI',money(t.cpi)],['impressions','曝光',int(t.impressions)],['clicks','点击',int(t.clicks)],['ad_impression','广告展示',int(t.ad_impression)]]; get('kpis').innerHTML=items.map(x=>`<div class="panel kpi"><div class="label">${x[1]}<span>${label}</span></div><div class="value ${x[3]||''}">${x[2]}</div><div class="sub">${Number(t.rows||0).toLocaleString()}行｜账户 ${t.accounts||0}｜Campaign ${t.campaigns||0}｜Ad ${t.ads||0}</div></div>`).join('')}
function renderBootRange(s,e,label){let t=bootTotalForRange(s,e); renderBootKpis(t,label); get('trendChart').innerHTML='<div class="empty">首屏已展示汇总，明细正在后台加载...</div>'; get('pivotTable').innerHTML='<div class="empty">明细正在后台加载，加载完成后可筛选和导出。</div>'; get('validationLine').textContent='首屏数据来自HTML内嵌轻量汇总，明细加载完成后自动切换为完整交互数据。'}
function renderBoot(){let b=BOOT_DATA||{},m=b.meta||{}; if(!m.generated_at)return; currentLevel=b.default_level||currentLevel; buildLevelButtons(); let today=bjToday(m.end_date), info=levelMeta(); get('metaLine').textContent=`${m.start_date} 至 ${m.end_date}｜更新 ${m.generated_at}｜默认口径 ${info.label||currentLevel}｜${Number(m.row_count||0).toLocaleString()} 行｜首屏已展示今天汇总，明细后台加载中...`; setDateRange(today,today); buildPresets(m); renderBootRange(today,today,'今天');}
function fillSelect(id,label,values){get(id).innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('')}
function buildPresets(meta){let box=get('presets'), m=meta||(DATA&&DATA.meta)||BOOT_DATA.meta||{}, today=bjToday(m.end_date); let items=[['today','今天'],['yesterday','昨天'],['3','近3天'],['7','近7天'],['14','近14天'],['60','近60天']]; box.innerHTML=items.map(x=>`<button data-kind="${x[0]}" type="button">${x[1]}</button>`).join('')+'<button id="resetBtn" type="button">重置</button>'; box.querySelectorAll('button[data-kind]').forEach(btn=>btn.onclick=()=>{let k=btn.dataset.kind, s=today, e=today, label=btn.textContent; if(k==='yesterday'){s=addDays(today,-1); e=s}else if(k!=='today'){let days=Number(k); s=addDays(today,-days+1)} setDateRange(s,e); if(DATA)loadData().catch(showLoadError); else renderBootRange(s,e,label);}); get('resetBtn').onclick=()=>{setDateRange(today,today); ['optimizerFilter','accountFilter','appFilter','countryFilter','languageFilter','seriesFilter','minSpend','minRoas','maxCpi','keyword'].forEach(id=>get(id).value=''); if(DATA)loadData().catch(showLoadError); else renderBootRange(today,today,'今天');};}
function buildChips(){let disabled=disabledDims(); state.dims=state.dims.filter(d=>!disabled.has(d)); if(!state.dims.length)state.dims=['dt']; get('dimensionChips').innerHTML=DATA.dimensions.map(d=>{let off=disabled.has(d.key); return `<label class="chip ${state.dims.includes(d.key)?'active':''} ${off?'disabled':''}" title="${off?'campaign口径下不可按该维度分组':''}"><input type="checkbox" value="${d.key}" ${state.dims.includes(d.key)?'checked':''} ${off?'disabled':''}>${d.label}</label>`}).join(''); get('dimensionChips').querySelectorAll('input:not(:disabled)').forEach(i=>i.onchange=()=>{state.dims=[...get('dimensionChips').querySelectorAll('input:checked:not(:disabled)')].map(x=>x.value); if(!state.dims.length)state.dims=['dt']; buildChips(); scheduleRender('查询中，正在更新透视维度...');}); get('metricToggles').innerHTML=DATA.metrics.map(m=>`<label class="chip ${state.metrics.includes(m.key)?'active':''}"><input type="checkbox" value="${m.key}" ${state.metrics.includes(m.key)?'checked':''}>${m.label}</label>`).join(''); get('metricToggles').querySelectorAll('input').forEach(i=>i.onchange=()=>{state.metrics=[...get('metricToggles').querySelectorAll('input:checked')].map(x=>x.value); if(!state.metrics.length)state.metrics=['spend','revenue','roas']; buildChips(); scheduleRender('查询中，正在更新展示指标...');});}
function filteredRows(){let s=get('startDate').value,e=get('endDate').value,opt=get('optimizerFilter').value,acc=get('accountFilter').value,app=get('appFilter').value,country=get('countryFilter').value,lang=get('languageFilter').value,series=get('seriesFilter').value,kw=get('keyword').value.trim().toLowerCase(),minSpend=Number(get('minSpend').value||0),minRoas=get('minRoas').value===''?null:Number(get('minRoas').value),maxCpi=get('maxCpi').value===''?null:Number(get('maxCpi').value); return DATA.rows.filter(r=>{if(r.dt<s||r.dt>e)return false; if(opt&&r.optimizer_name!==opt)return false; if(acc&&r.ad_account_id!==acc)return false; if(app&&r.app_id!==app)return false; if(country&&r.country!==country)return false; if(lang&&r.language!==lang)return false; if(series&&r.series_code!==series)return false; if(Number(r.spend)<minSpend)return false; if(minRoas!==null&&Number(r.roas)<minRoas)return false; if(maxCpi!==null&&Number(r.cpi)>maxCpi)return false; if(kw){let hay=[r.series_code,r.data_source_id,r.resource_name,r.campaign_name,r.campaign_id,r.adset_name,r.adset_id,r.ad_name,r.ad_id,r.ad_account_id,r.optimizer_name].join(' ').toLowerCase(); if(!hay.includes(kw))return false;} return true;});}
function emptyAgg(withSets=true){let a={spend:0,revenue:0,installs:0,impressions:0,clicks:0,ad_impression:0,rows:0}; if(withSets){a.ads=new Set(); a.campaigns=new Set(); a.adgroups=new Set(); a.accounts=new Set();} return a}
function addAgg(a,r){a.spend+=Number(r.spend||0); a.revenue+=Number(r.revenue||0); a.installs+=Number(r.installs||0); a.impressions+=Number(r.impressions||0); a.clicks+=Number(r.clicks||0); a.ad_impression+=Number(r.ad_impression||0); a.rows+=1; if(a.ads){if(r.ad_id&&r.ad_id!=='campaign层级不可用')a.ads.add(r.ad_id); if(r.campaign_id)a.campaigns.add(r.campaign_id); if(r.adset_id&&r.adset_id!=='campaign层级不可用')a.adgroups.add(r.adset_id); if(r.ad_account_id)a.accounts.add(r.ad_account_id);}}
function finalize(a){a.roas=a.spend?a.revenue/a.spend:0; a.cpi=a.installs?a.spend/a.installs:0; a.ctr=a.impressions?a.clicks/a.impressions:0; a.ads_count=a.ads?a.ads.size:0; a.campaigns_count=a.campaigns?a.campaigns.size:0; a.adgroups_count=a.adgroups?a.adgroups.size:0; a.accounts_count=a.accounts?a.accounts.size:0; return a}
function aggregate(rows,dims,withSets=true){let map=new Map(); rows.forEach(r=>{let key=dims.map(d=>r[d]||'未填').join('\\u001f'); if(!map.has(key)){let o=emptyAgg(withSets); dims.forEach(d=>o[d]=r[d]||'未填'); map.set(key,o)} addAgg(map.get(key),r)}); return [...map.values()].map(finalize)}
function render(){if(!DATA)return; let rows=filteredRows(); let total=finalize(rows.reduce((a,r)=>(addAgg(a,r),a),emptyAgg())); renderKpis(total,rows); renderTrend(rows); renderRankings(rows); renderTable(rows); renderValidation();}
function renderKpis(t,rows){let items=[['spend','花费',money(t.spend)],['revenue','IAA回收',money(t.revenue)],['roas','ROAS',ratio(t.roas),roasClass(t.roas)],['installs','Install',int(t.installs)],['cpi','CPI',money(t.cpi)],['impressions','曝光',int(t.impressions)],['clicks','点击',int(t.clicks)],['ad_impression','广告展示',int(t.ad_impression)]]; get('kpis').innerHTML=items.map(x=>`<div class="panel kpi"><div class="label">${x[1]}<span>${rows.length.toLocaleString()}行</span></div><div class="value ${x[3]||''}">${x[2]}</div><div class="sub">账户 ${t.accounts_count||0}｜Campaign ${t.campaigns_count||0}｜Ad ${t.ads_count||0}</div></div>`).join('')}
function renderTrend(rows){let daily=aggregate(rows,['dt'],false).sort((a,b)=>a.dt.localeCompare(b.dt)); if(!daily.length){get('trendChart').innerHTML='<div class="empty">无数据</div>';return} let maxSpend=Math.max(...daily.map(d=>d.spend),1), maxRev=Math.max(...daily.map(d=>d.revenue),1), w=980,h=220,p=34,step=(w-p*2)/Math.max(daily.length,1); let bars=daily.map((d,i)=>{let x=p+i*step+step*.18, bw=Math.max(12,step*.26), sh=(h-p*2)*d.spend/maxSpend, rh=(h-p*2)*d.revenue/maxRev; return `<rect x="${x}" y="${h-p-sh}" width="${bw}" height="${sh}" fill="#2f6bc0"><title>${d.dt} 花费 ${money(d.spend)}</title></rect><rect x="${x+bw+3}" y="${h-p-rh}" width="${bw}" height="${rh}" fill="#0f766e"><title>${d.dt} 回收 ${money(d.revenue)}</title></rect><text x="${x+bw}" y="${h-10}" text-anchor="middle" font-size="11" fill="#667085">${d.dt.slice(5)}</text><text x="${x+bw}" y="${h-p-sh-6}" text-anchor="middle" font-size="11" fill="${d.roas>=1?'#07823f':d.roas>=.8?'#b75d00':'#c62828'}">${ratio(d.roas)}</text>`}).join(''); get('trendChart').innerHTML=`<svg viewBox="0 0 ${w} ${h}" width="100%" height="100%" preserveAspectRatio="none"><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="#cfd8e5"/>${bars}</svg><div class="muted">蓝色=花费，绿色=IAA回收，柱顶数字=ROAS</div>`}
function rankBlock(rows,dim,title){let data=aggregate(rows,[dim],false).sort((a,b)=>b.spend-a.spend).slice(0,8); let max=Math.max(...data.map(d=>d.spend),1); return `<div class="panel rank"><h3>${title}</h3>${data.map(d=>`<div class="rank-row"><div><div title="${esc(d[dim])}">${esc(d[dim])}</div><div class="bar"><div class="fill" style="width:${Math.max(3,d.spend/max*100)}%"></div></div></div><div class="nowrap ${roasClass(d.roas)}">${money(d.spend)}<br>ROAS ${ratio(d.roas)}</div></div>`).join('')||'<div class="empty">无数据</div>'}</div>`}
function renderRankings(rows){get('rankings').innerHTML=rankBlock(rows,'optimizer_name','优化师排行')+rankBlock(rows,'series_code','短剧排行')+rankBlock(rows,'resource_name','资源排行')+rankBlock(rows,'ad_account_id','账户排行')+rankBlock(rows,'campaign_id','Campaign排行')}
function renderTable(rows){let data=aggregate(rows,state.dims); data.sort((a,b)=>{let av=a[state.sortKey],bv=b[state.sortKey]; if(typeof av==='string'||typeof bv==='string')return String(av).localeCompare(String(bv),'zh-Hans-CN')*state.sortDir; return (Number(av||0)-Number(bv||0))*state.sortDir}); let dims=state.dims, mets=state.metrics, shown=data.slice(0,1200); get('tableHint').textContent=`筛选后 ${rows.length.toLocaleString()} 行，聚合 ${data.length.toLocaleString()} 行，当前展示 ${shown.length.toLocaleString()} 行`; let th=dims.map(d=>`<th data-k="${d}">${dimLabels[d]||d}</th>`).join('')+mets.map(m=>`<th data-k="${m}">${metricLabels[m]||m}</th>`).join('')+'<th data-k="ads_count">Ad数</th><th data-k="campaigns_count">Campaign数</th>'; let body=shown.map(r=>`<tr>${dims.map(d=>`<td class="dim">${esc(r[d])}</td>`).join('')}${mets.map(m=>`<td class="${m==='roas'?roasClass(r[m]):''}">${fmt(m,r[m])}</td>`).join('')}<td>${int(r.ads_count)}</td><td>${int(r.campaigns_count)}</td></tr>`).join(''); get('pivotTable').innerHTML=`<table><thead><tr>${th}</tr></thead><tbody>${body||'<tr><td class="empty" colspan="20">无数据</td></tr>'}</tbody></table>`; get('pivotTable').querySelectorAll('th').forEach(th=>th.onclick=()=>{let k=th.dataset.k; if(state.sortKey===k)state.sortDir*=-1; else{state.sortKey=k; state.sortDir=(state.metrics.includes(k)||['ads_count','campaigns_count'].includes(k))?-1:1} renderTable(filteredRows());}); window.currentExportRows=data;}
function renderValidation(){let v=DATA.validation||{}, warns=v.warnings||[], info=levelMeta(); let checks=(v.checks||[]).map(c=>`${c.date}: ${c.status==='ok'?'OK':c.status} ${c.message||''}`).join(' ｜ '); get('validationLine').innerHTML=warns.length?`<div class="warn-box">底表样本校验提示：${esc(warns.join('；'))}</div>`:`数据状态：${esc(checks||'未执行校验')}。当前口径：${esc(info.source||'')}。`}
function exportCSV(){let rows=window.currentExportRows||[]; let dims=state.dims,mets=state.metrics,headers=[...dims.map(d=>dimLabels[d]||d),...mets.map(m=>metricLabels[m]||m),'Ad数','Campaign数']; let lines=[headers.join(',')]; rows.forEach(r=>{let vals=[...dims.map(d=>r[d]),...mets.map(m=>r[m]),r.ads_count,r.campaigns_count].map(v=>`"${String(v==null?'':v).replace(/"/g,'""')}"`); lines.push(vals.join(','));}); let blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}); let a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='tt_minis_dashboard_'+(DATA?DATA.meta.generated_at:BOOT_DATA.meta.generated_at).replace(/[-: ]/g,'')+'.csv'; a.click(); URL.revokeObjectURL(a.href)}
function showLoadError(err){get('metaLine').textContent='加载失败：'+err; document.body.insertAdjacentHTML('beforeend',`<div class="wrap"><div class="panel empty">数据加载失败：${esc(err)}</div></div>`)}
get('refreshBtn').onclick=()=>loadData().catch(showLoadError); get('exportBtn').onclick=exportCSV; renderBoot(); loadData().catch(showLoadError);
</script>
</body>
</html>
"""
    return template.replace("__BOOT_DATA__", boot_json)


def publish(payload, output_dir, rows_by_level=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for old in data_dir.glob("**/*.json"):
        try:
            old.unlink()
        except OSError:
            pass
    manifest = dict(payload)
    manifest["rows"] = []
    manifest["dicts"] = {}
    manifest["data_files"] = {}
    rows_by_level = rows_by_level or {DEFAULT_METRIC_LEVEL: []}
    for level, level_rows in rows_by_level.items():
        level = normalize_metric_level(level)
        manifest["data_files"][level] = {}
        grouped = rows_by_date(level_rows)
        for day in sorted(grouped):
            day_payload = build_payload(
                grouped[day],
                day,
                day,
                {"type": "skipped", "checks": [], "warnings": []},
                include_rows=True,
                metric_level=level,
            )
            rel = "data/%s/%s.json" % (level, day)
            atomic_write(output_dir / rel, json.dumps(day_payload, ensure_ascii=False, separators=(",", ":")))
            manifest["data_files"][level][day] = {"path": rel, "row_count": len(grouped[day])}
    data = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    atomic_write(output_dir / "latest.json", data)
    atomic_write(output_dir / "index.html", html_template(payload))
    return output_dir / "index.html"


def publish_from_cache(payload, output_dir, start_date, end_date):
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for old in data_dir.glob("**/*.json"):
        try:
            old.unlink()
        except OSError:
            pass
    manifest = dict(payload)
    manifest["rows"] = []
    manifest["dicts"] = {}
    manifest["data_files"] = {}
    for level in METRIC_LEVELS:
        manifest["data_files"][level] = {}
        for day in each_date(start_date, end_date):
            day_rows = fetch_rows_from_cache(day, day, level)
            if not day_rows:
                continue
            day_payload = build_payload(
                day_rows,
                day,
                day,
                {"type": "skipped", "checks": [], "warnings": []},
                include_rows=True,
                metric_level=level,
            )
            rel = "data/%s/%s.json" % (level, day)
            (output_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            atomic_write(output_dir / rel, json.dumps(day_payload, ensure_ascii=False, separators=(",", ":")))
            manifest["data_files"][level][day] = {"path": rel, "row_count": len(day_rows)}
            del day_rows
    data = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    atomic_write(output_dir / "latest.json", data)
    atomic_write(output_dir / "index.html", html_template(payload))
    return output_dir / "index.html"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--refresh-cache-days", type=int, default=0)
    parser.add_argument("--refresh-cache-start-date")
    parser.add_argument("--refresh-cache-end-date")
    parser.add_argument("--cache-retention-days", type=int, default=CACHE_RETENTION_DAYS)
    parser.add_argument("--refresh-cache-only", action="store_true")
    parser.add_argument("--allow-cache-backfill", action="store_true", help="Manually backfill missing cache dates from source tables before reading cache.")
    parser.add_argument("--output-dir", default=str(WEB_DIR))
    return parser.parse_args()


def main():
    args = parse_args()
    start_date, end_date = date_range(args.days, args.start_date, args.end_date)
    started = time.time()
    refresh_results = []
    if args.refresh_cache_start_date or args.refresh_cache_end_date:
        refresh_start, refresh_end = date_range(
            args.days,
            args.refresh_cache_start_date or args.refresh_cache_end_date,
            args.refresh_cache_end_date or args.refresh_cache_start_date,
        )
        refresh_results.extend(refresh_cache(refresh_start, refresh_end))
    if args.refresh_cache_days:
        refresh_start, refresh_end = date_range(args.refresh_cache_days)
        refresh_results.extend(refresh_cache(refresh_start, refresh_end))
    prune_result = prune_cache(args.cache_retention_days)
    if args.refresh_cache_only:
        print(json.dumps({"refresh": refresh_results, "prune": prune_result, "elapsed_sec": round(time.time() - started, 2)}, ensure_ascii=False, indent=2))
        return 0
    if args.allow_cache_backfill:
        for level in METRIC_LEVELS:
            refresh_results.extend(ensure_cache_for_range(start_date, end_date, level))
    missing_dates = {level: missing_cache_dates(start_date, end_date, level) for level in METRIC_LEVELS}
    rows = fetch_rows_from_cache(start_date, end_date, DEFAULT_METRIC_LEVEL)
    validation = {"type": "skipped", "checks": [], "warnings": []}
    if not args.skip_validation and rows:
        validation = sample_validation(rows)
    payload = build_payload(rows, start_date, end_date, validation, metric_level=DEFAULT_METRIC_LEVEL)
    elapsed = time.time() - started
    if args.publish:
        out = publish_from_cache(payload, Path(args.output_dir), start_date, end_date)
        print("published=%s" % out)
        print("url=%s" % PUBLIC_URL)
    if args.dry_run or not args.publish:
        rows_by_level = {DEFAULT_METRIC_LEVEL: rows}
        for level in METRIC_LEVELS:
            if level != DEFAULT_METRIC_LEVEL:
                rows_by_level[level] = fetch_rows_from_cache(start_date, end_date, level)
        print(
            json.dumps(
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "rows": {level: len(level_rows) for level, level_rows in rows_by_level.items()},
                    "totals": {level: build_payload(level_rows, start_date, end_date, {"type": "skipped", "checks": [], "warnings": []}, include_rows=False, metric_level=level)["totals"] for level, level_rows in rows_by_level.items()},
                    "validation": validation,
                    "cache_refresh": refresh_results,
                    "cache_prune": prune_result,
                    "cache_missing_dates": missing_dates,
                    "cache_db": str(CACHE_DB),
                    "elapsed_sec": round(elapsed, 2),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Voiceover drama designer task service.

This module owns the voiceover-drama feature logic so deployments for other
AI backend modules do not have to edit the shared app.py monolith.
"""

import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html import unescape

import requests

VOICEOVER_KOL_TASK_API_URL = os.environ.get(
    "VOICEOVER_KOL_TASK_API_URL", "https://ads-admin.static.kunlun.com/api/ai/kol-task"
).strip()
VOICEOVER_KOL_TASK_API_TOKEN = os.environ.get("VOICEOVER_KOL_TASK_API_TOKEN", "").strip()
VOICEOVER_KOL_TASK_API_TIMEOUT = int(os.environ.get("VOICEOVER_KOL_TASK_API_TIMEOUT", "30"))
VOICEOVER_DESIGNER_ROLE_APP_ID = os.environ.get("VOICEOVER_DESIGNER_ROLE_APP_ID", "78").strip() or "78"
VOICEOVER_DEFAULT_ROAS_THRESHOLD = float(os.environ.get("VOICEOVER_DEFAULT_ROAS_THRESHOLD", "45"))
VOICEOVER_DEFAULT_MIN_CANDIDATES = int(os.environ.get("VOICEOVER_DEFAULT_MIN_CANDIDATES", "15"))
VOICEOVER_DEFAULT_APP_ID = os.environ.get("VOICEOVER_DEFAULT_APP_ID", "").strip()
VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE = int(os.environ.get("VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE", "6") or "6")
VOICEOVER_FILTER_SOURCE_SCAN_LIMIT = int(os.environ.get("VOICEOVER_FILTER_SOURCE_SCAN_LIMIT", "1500") or "1500")
VOICEOVER_FILTER_SOURCE_SCAN_MAX = int(os.environ.get("VOICEOVER_FILTER_SOURCE_SCAN_MAX", "3000") or "3000")
VOICEOVER_FILTER_LEGACY_FALLBACK = os.environ.get("VOICEOVER_FILTER_LEGACY_FALLBACK", "0").strip() == "1"
VOICEOVER_CREATE_MAX_WORKERS = int(os.environ.get("VOICEOVER_CREATE_MAX_WORKERS", "5") or "5")
VOICEOVER_PRODUCT_OPTIONS = (
    {"key": "dramawave1479", "app_id": "1479", "label": "Dramawave"},
    {"key": "freereels979", "app_id": "979", "label": "FreeReels"},
)
VOICEOVER_PRODUCT_BY_KEY = {item["key"]: item for item in VOICEOVER_PRODUCT_OPTIONS}
VOICEOVER_PRODUCT_BY_APP_ID = {item["app_id"]: item for item in VOICEOVER_PRODUCT_OPTIONS}
VOICEOVER_SOURCE_PRODUCTS_BY_APP_ID = {
    "1479": ("Dramawave",),
    "979": ("FreeReels", "freereels-AI素材"),
}

ADMIN_MAPPING_MYSQL_DATABASE = ""
DB_NAME = ""


def _missing_dependency(name):
    raise RuntimeError("voiceover dependency is not configured: %s" % name)


class _StructuredApiError(ValueError):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


StructuredApiError = _StructuredApiError


def run_mysql(query):
    return _missing_dependency("run_mysql")


def mysql_escape_literal(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def app_package_for_app_id(app_id):
    return ""


def ad_material_actor(session):
    return {}


def api_error_payload(exc, default_code="bad_request"):
    return {"code": getattr(exc, "code", default_code), "message": str(exc), "error": str(exc)}


def configure_voiceover_drama_tasks(**deps):
    allowed = {
        "ADMIN_MAPPING_MYSQL_DATABASE",
        "DB_NAME",
        "StructuredApiError",
        "run_mysql",
        "mysql_escape_literal",
        "app_package_for_app_id",
        "ad_material_actor",
        "api_error_payload",
    }
    for key, value in deps.items():
        if key in allowed:
            globals()[key] = value


def voiceover_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def voiceover_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def voiceover_parse_content_ids(payload):
    payload = payload or {}
    raw = payload.get("content_ids")
    if raw is None:
        raw = payload.get("content_ids_text", payload.get("content_id", ""))
    if isinstance(raw, list):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [item.strip() for item in re.split(r"[\s,，;；]+", str(raw or "")) if item.strip()]
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    if not result:
        raise StructuredApiError("content_ids_required", "请填写至少一个剧 ID")
    if len(result) > 100:
        raise StructuredApiError("content_ids_too_many", "一次最多处理 100 个剧 ID")
    return result


def voiceover_parse_series_codes(payload, required=True):
    payload = payload or {}
    raw = payload.get("series_codes")
    if raw is None:
        raw = payload.get("resource_ids")
    if raw is None:
        raw = payload.get("series_code", payload.get("resource_id", ""))
    if isinstance(raw, list):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [item.strip() for item in re.split(r"[\s,，;；]+", str(raw or "")) if item.strip()]
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    if required and not result:
        raise StructuredApiError("series_codes_required", "请填写至少一个资源 ID")
    if len(result) > 100:
        raise StructuredApiError("series_codes_too_many", "一次最多处理 100 个资源 ID")
    return result


def voiceover_parse_target_language(payload):
    payload = payload or {}
    value = str(payload.get("target_language") or payload.get("target_lang") or "").strip()
    return value.lower()


def voiceover_parse_target_audio_type(payload):
    payload = payload or {}
    raw = payload.get("target_audio_type")
    if raw is None:
        raw = payload.get("audio_type")
    value = str(raw if raw is not None else "").strip()
    if value == "":
        return ""
    if value not in ("0", "1", "2"):
        raise StructuredApiError("invalid_audio_type", "配音类型只能选择 0、1、2")
    return value


def voiceover_normalize_product_app_id(value):
    value = str(value or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in VOICEOVER_PRODUCT_BY_KEY:
        return VOICEOVER_PRODUCT_BY_KEY[lowered]["app_id"]
    if value in VOICEOVER_PRODUCT_BY_APP_ID:
        return value
    if "1479" in lowered or "dramawave" in lowered:
        return "1479"
    if "979" in lowered or "freereels" in lowered or "free_reels" in lowered:
        return "979"
    return value if re.match(r"^\d+$", value) else ""


def voiceover_product_meta(app_id):
    app_id = str(app_id or "").strip()
    meta = VOICEOVER_PRODUCT_BY_APP_ID.get(app_id)
    if meta:
        return dict(meta)
    return {"key": app_id or "", "app_id": app_id, "label": app_id or ""}


def voiceover_source_product_label(product):
    text = str(product or "").strip()
    lowered = re.sub(r"[^a-z0-9]+", "", text.lower())
    if "freereels" in lowered:
        return "FreeReels"
    if "dramawave" in lowered:
        return "Dramawave"
    return text or "-"


def voiceover_source_product_names(app_id):
    return VOICEOVER_SOURCE_PRODUCTS_BY_APP_ID.get(str(app_id or "").strip(), ())


def voiceover_source_product_sql_condition(app_id, alias="s"):
    names = voiceover_source_product_names(app_id)
    if not names:
        return ""
    field = "%s.product" % alias
    return "COALESCE(%s, '') IN (%s)" % (field, voiceover_sql_in(names))


def voiceover_parse_product_app_ids(payload):
    payload = payload or {}
    raw = payload.get("product_app_ids")
    if raw is None:
        raw = payload.get("app_ids")
    if raw is None:
        raw = payload.get("products")
    if raw is None:
        raw = payload.get("product")
    if raw is None:
        raw = payload.get("app_id")
    if isinstance(raw, list):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [item.strip() for item in re.split(r"[\s,，;；]+", str(raw or "")) if item.strip()]
    result = []
    seen = set()
    for value in values:
        app_id = voiceover_normalize_product_app_id(value)
        if app_id and app_id not in seen:
            seen.add(app_id)
            result.append(app_id)
    if not result:
        default_app_id = voiceover_normalize_product_app_id(VOICEOVER_DEFAULT_APP_ID)
        if default_app_id:
            result.append(default_app_id)
        else:
            result = [item["app_id"] for item in VOICEOVER_PRODUCT_OPTIONS]
    return result


def voiceover_sql_db():
    return (ADMIN_MAPPING_MYSQL_DATABASE or DB_NAME).replace("`", "``")


def voiceover_sql_in(values):
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return "''"
    return ",".join("'%s'" % mysql_escape_literal(value) for value in cleaned)


def voiceover_series_content_ids(series_code, app_id=""):
    series_code = str(series_code or "").strip()
    if not series_code:
        return []
    database = voiceover_sql_db()
    where = "CAST(d.series_code AS CHAR)='%s' AND d.content_id<>''" % mysql_escape_literal(series_code)
    app_id = str(app_id or "").strip()
    if app_id:
        where += " AND CAST(d.app_id AS CHAR)='%s'" % mysql_escape_literal(app_id)
    rows = run_mysql(
        (
            "SELECT DISTINCT d.content_id "
            "FROM `%s`.ads_drama_info d "
            "WHERE %s "
            "ORDER BY d.content_id"
        )
        % (database, where)
    )
    return [str(row[0] if row else "").strip() for row in rows if row and str(row[0]).strip()]


def voiceover_series_content_ids_map(series_codes, app_ids=None):
    cleaned = []
    seen = set()
    for series_code in series_codes:
        value = str(series_code or "").strip()
        if value and value not in seen:
            cleaned.append(value)
            seen.add(value)
    if not cleaned:
        return {}
    app_ids = [str(app_id or "").strip() for app_id in (app_ids or []) if str(app_id or "").strip()]
    database = voiceover_sql_db()
    where = "d.series_code IN (%s) AND d.content_id<>''" % voiceover_sql_in(cleaned)
    if app_ids:
        where += " AND CAST(d.app_id AS CHAR) IN (%s)" % voiceover_sql_in(app_ids)
    rows = run_mysql(
        (
            "SELECT d.series_code, CAST(d.app_id AS CHAR), d.content_id "
            "FROM `%s`.ads_drama_info d FORCE INDEX (scoo) "
            "WHERE %s "
            "ORDER BY d.series_code, d.app_id, d.content_id"
        )
        % (database, where)
    )
    result = {(series_code, app_id): [] for series_code in cleaned for app_id in app_ids} if app_ids else {series_code: [] for series_code in cleaned}
    seen_pairs = set()
    for row in rows:
        series_code = str(row[0] if len(row) > 0 else "").strip()
        app_id = str(row[1] if len(row) > 1 else "").strip()
        content_id = str(row[2] if len(row) > 2 else "").strip()
        if not series_code or not content_id:
            continue
        result_key = (series_code, app_id) if app_ids else series_code
        pair_key = (result_key, content_id)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        result.setdefault(result_key, []).append(content_id)
    return result


def voiceover_drama_info_from_row(row, content_id=""):
    app_id_value = str(row[1] if len(row) > 1 else "").strip()
    source_content_id = content_id or str(row[2] if len(row) > 2 else "").strip()
    app_package = (
        str(row[11] if len(row) > 11 else "").strip()
        or str(row[10] if len(row) > 10 else "").strip()
        or app_package_for_app_id(app_id_value)
    )
    drama_app = str(row[7] if len(row) > 7 else "").strip() or app_package
    full_content_id = "%s#-#%s#-#%s" % (app_package, drama_app, source_content_id)
    kol_content_id = "%s#-#%s" % (app_package, source_content_id)
    product_name = str(row[9] if len(row) > 9 else "").strip() or app_package or app_id_value
    return {
        "id": str(row[0] if len(row) > 0 else "").strip(),
        "app_id": app_id_value,
        "content_id": source_content_id,
        "full_content_id": full_content_id,
        "kol_content_id": kol_content_id,
        "name": str(row[3] if len(row) > 3 else "").strip(),
        "country": str(row[4] if len(row) > 4 else "").strip(),
        "language": str(row[5] if len(row) > 5 else "").strip(),
        "series_code": str(row[6] if len(row) > 6 else "").strip(),
        "app": drama_app,
        "app_package": app_package,
        "product_name": product_name,
        "updated_at": str(row[8] if len(row) > 8 else "").strip(),
        "audio_type": str(row[13] if len(row) > 13 else "").strip(),
    }


def lookup_voiceover_drama_info_map(content_ids, app_id=""):
    cleaned = []
    seen = set()
    for content_id in content_ids:
        value = str(content_id or "").strip()
        if value and value not in seen:
            cleaned.append(value)
            seen.add(value)
    if not cleaned:
        return {}
    app_id = str(app_id or "").strip()
    database = voiceover_sql_db()
    where = "d.content_id IN (%s)" % voiceover_sql_in(cleaned)
    if app_id:
        where += " AND CAST(d.app_id AS CHAR)='%s'" % mysql_escape_literal(app_id)
    prefer_app_id = app_id or VOICEOVER_DEFAULT_APP_ID
    prefer_sql = ""
    if prefer_app_id:
        prefer_sql = "CASE WHEN CAST(d.app_id AS CHAR)='%s' THEN 0 ELSE 1 END, " % mysql_escape_literal(prefer_app_id)
    rows = run_mysql(
        (
            "SELECT CAST(d.id AS CHAR), CAST(d.app_id AS CHAR), d.content_id, d.name, d.country, "
            "d.language, d.series_code, d.app, CAST(d.updated_at AS CHAR), "
            "COALESCE(a.name, ''), COALESCE(a.package, ''), COALESCE(a.google_app_android, ''), "
            "COALESCE(a.app_id, ''), CAST(COALESCE(d.audio_type, '') AS CHAR) "
            "FROM `%s`.ads_drama_info d "
            "LEFT JOIN `%s`.ads_apps_setting a ON a.id=d.app_id "
            "WHERE %s "
            "ORDER BY d.content_id ASC, %sd.updated_at DESC, d.id ASC"
        )
        % (database, database, where, prefer_sql)
    )
    result = {}
    for row in rows:
        content_id = str(row[2] if len(row) > 2 else "").strip()
        if content_id and content_id not in result:
            result[content_id] = voiceover_drama_info_from_row(row, content_id)
    return result


def lookup_voiceover_drama_info_map_by_app(content_ids, app_ids):
    cleaned_content_ids = []
    seen_content_ids = set()
    for content_id in content_ids or []:
        value = str(content_id or "").strip()
        if value and value not in seen_content_ids:
            cleaned_content_ids.append(value)
            seen_content_ids.add(value)
    cleaned_app_ids = []
    seen_app_ids = set()
    for app_id in app_ids or []:
        value = str(app_id or "").strip()
        if value and value not in seen_app_ids:
            cleaned_app_ids.append(value)
            seen_app_ids.add(value)
    if not cleaned_content_ids or not cleaned_app_ids:
        return {}
    database = voiceover_sql_db()
    rows = run_mysql(
        (
            "SELECT CAST(d.id AS CHAR), CAST(d.app_id AS CHAR), d.content_id, d.name, d.country, "
            "d.language, d.series_code, d.app, CAST(d.updated_at AS CHAR), "
            "COALESCE(a.name, ''), COALESCE(a.package, ''), COALESCE(a.google_app_android, ''), "
            "COALESCE(a.app_id, ''), CAST(COALESCE(d.audio_type, '') AS CHAR) "
            "FROM `%s`.ads_drama_info d "
            "LEFT JOIN `%s`.ads_apps_setting a ON a.id=d.app_id "
            "WHERE d.content_id IN (%s) AND CAST(d.app_id AS CHAR) IN (%s) "
            "ORDER BY d.content_id ASC, d.app_id ASC, d.updated_at DESC, d.id ASC"
        )
        % (database, database, voiceover_sql_in(cleaned_content_ids), voiceover_sql_in(cleaned_app_ids))
    )
    result = {}
    for row in rows:
        app_id = str(row[1] if len(row) > 1 else "").strip()
        content_id = str(row[2] if len(row) > 2 else "").strip()
        key = (app_id, content_id)
        if app_id and content_id and key not in result:
            result[key] = voiceover_drama_info_from_row(row, content_id)
    return result


def lookup_voiceover_drama_info_map_by_series(series_codes, app_ids, target_language="", target_audio_type=""):
    cleaned_series_codes = []
    seen_series_codes = set()
    for series_code in series_codes or []:
        value = str(series_code or "").strip()
        if value and value not in seen_series_codes:
            cleaned_series_codes.append(value)
            seen_series_codes.add(value)
    cleaned_app_ids = []
    seen_app_ids = set()
    for app_id in app_ids or []:
        value = str(app_id or "").strip()
        if value and value not in seen_app_ids:
            cleaned_app_ids.append(value)
            seen_app_ids.add(value)
    if not cleaned_series_codes or not cleaned_app_ids:
        return {}
    database = voiceover_sql_db()
    where = (
        "d.series_code IN (%s) AND CAST(d.app_id AS CHAR) IN (%s) AND d.content_id<>''"
        % (voiceover_sql_in(cleaned_series_codes), voiceover_sql_in(cleaned_app_ids))
    )
    target_language = str(target_language or "").strip().lower()
    if target_language:
        where += " AND LOWER(TRIM(COALESCE(d.language, '')))='%s'" % mysql_escape_literal(target_language)
    target_audio_type = str(target_audio_type or "").strip()
    if target_audio_type:
        where += " AND CAST(COALESCE(d.audio_type, '') AS CHAR)='%s'" % mysql_escape_literal(target_audio_type)
    rows = run_mysql(
        (
            "SELECT CAST(d.id AS CHAR), CAST(d.app_id AS CHAR), d.content_id, d.name, d.country, "
            "d.language, d.series_code, d.app, CAST(d.updated_at AS CHAR), "
            "COALESCE(a.name, ''), COALESCE(a.package, ''), COALESCE(a.google_app_android, ''), "
            "COALESCE(a.app_id, ''), CAST(COALESCE(d.audio_type, '') AS CHAR) "
            "FROM `%s`.ads_drama_info d FORCE INDEX (scoo) "
            "LEFT JOIN `%s`.ads_apps_setting a ON a.id=d.app_id "
            "WHERE %s "
            "ORDER BY d.series_code ASC, d.app_id ASC, d.updated_at DESC, d.id ASC"
        )
        % (database, database, where)
    )
    result = {}
    for row in rows:
        app_id = str(row[1] if len(row) > 1 else "").strip()
        series_code = str(row[6] if len(row) > 6 else "").strip()
        key = (series_code, app_id)
        if app_id and series_code and key not in result:
            result[key] = voiceover_drama_info_from_row(row, str(row[2] if len(row) > 2 else "").strip())
    return result


def voiceover_pick_drama_by_series(drama_map, series_code, app_id):
    series_code = str(series_code or "").strip()
    app_id = str(app_id or "").strip()
    drama = drama_map.get((series_code, app_id))
    if drama:
        return drama
    if series_code.isdigit():
        normalized = str(int(series_code))
        drama = drama_map.get((normalized, app_id))
        if drama:
            return drama
    for (stored_series_code, stored_app_id), stored_drama in drama_map.items():
        if str(stored_app_id or "").strip() == app_id and str(stored_series_code or "").strip() == series_code:
            return stored_drama
    return None


def lookup_voiceover_drama_info(content_id, app_id=""):
    content_id = str(content_id or "").strip()
    app_id = str(app_id or "").strip()
    if not content_id:
        raise StructuredApiError("invalid_content_id", "剧 ID 不能为空")
    database = voiceover_sql_db()
    where = "d.content_id='%s'" % mysql_escape_literal(content_id)
    if app_id:
        where += " AND CAST(d.app_id AS CHAR)='%s'" % mysql_escape_literal(app_id)
    prefer_app_id = app_id or VOICEOVER_DEFAULT_APP_ID
    prefer_sql = ""
    if prefer_app_id:
        prefer_sql = "CASE WHEN CAST(d.app_id AS CHAR)='%s' THEN 0 ELSE 1 END, " % mysql_escape_literal(prefer_app_id)
    rows = run_mysql(
        (
            "SELECT CAST(d.id AS CHAR), CAST(d.app_id AS CHAR), d.content_id, d.name, d.country, "
            "d.language, d.series_code, d.app, CAST(d.updated_at AS CHAR), "
            "COALESCE(a.name, ''), COALESCE(a.package, ''), COALESCE(a.google_app_android, ''), "
            "COALESCE(a.app_id, ''), CAST(COALESCE(d.audio_type, '') AS CHAR) "
            "FROM `%s`.ads_drama_info d "
            "LEFT JOIN `%s`.ads_apps_setting a ON a.id=d.app_id "
            "WHERE %s "
            "ORDER BY %sd.updated_at DESC, d.id ASC LIMIT 1"
        )
        % (database, database, where, prefer_sql)
    )
    if not rows:
        raise StructuredApiError("drama_not_found", "未在剧库中找到剧 ID：%s" % content_id)
    return voiceover_drama_info_from_row(rows[0], content_id)


def voiceover_material_count_for_series(series_code, content_ids=None):
    series_code = str(series_code or "").strip()
    if not series_code:
        return 0
    database = voiceover_sql_db()
    content_ids = content_ids if content_ids is not None else voiceover_series_content_ids(series_code)
    if not content_ids:
        return 0
    content_id_sql = voiceover_sql_in(content_ids)
    rows = run_mysql(
        (
            "SELECT COUNT(DISTINCT s.id) "
            "FROM `%s`.ads_custom_source s FORCE INDEX (idx_source_type_source_id) "
            "WHERE s.data_source=%d AND s.data_source_id IN (%s) "
            "AND s.is_delete=0 AND COALESCE(s.url, '')<>''"
        )
        % (database, VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE, content_id_sql)
    )
    return voiceover_int(rows[0][0] if rows and rows[0] else 0, 0)


def voiceover_material_count_map_for_series(series_codes):
    cleaned = []
    seen = set()
    for series_code in series_codes:
        value = str(series_code or "").strip()
        if value and value not in seen:
            cleaned.append(value)
            seen.add(value)
    if not cleaned:
        return {}
    database = voiceover_sql_db()
    series_sql = voiceover_sql_in(cleaned)
    rows = run_mysql(
        (
            "SELECT d.series_code, COUNT(DISTINCT s.id), COUNT(DISTINCT d.content_id) "
            "FROM `%s`.ads_drama_info d FORCE INDEX (scoo) "
            "LEFT JOIN `%s`.ads_custom_source s FORCE INDEX (idx_source_type_source_id) "
            "ON s.data_source=%d AND s.data_source_id=d.content_id "
            "AND s.is_delete=0 AND COALESCE(s.url, '')<>'' "
            "WHERE d.series_code IN (%s) AND d.content_id<>'' "
            "GROUP BY d.series_code"
        )
        % (database, database, VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE, series_sql)
    )
    result = {}
    for row in rows:
        series_code = str(row[0] if row else "").strip()
        if not series_code:
            continue
        result[series_code] = {
            "material_count": voiceover_int(row[1] if len(row) > 1 else 0, 0),
            "series_content_count": voiceover_int(row[2] if len(row) > 2 else 0, 0),
        }
    return result


def voiceover_material_count_map_for_series_targets(series_targets):
    cleaned_targets = []
    seen_targets = set()
    series_codes = []
    app_ids = []
    for series_code, app_id in series_targets or []:
        series_code = str(series_code or "").strip()
        app_id = str(app_id or "").strip()
        if not series_code or not app_id:
            continue
        key = (series_code, app_id)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        cleaned_targets.append(key)
        if series_code not in series_codes:
            series_codes.append(series_code)
        if app_id not in app_ids:
            app_ids.append(app_id)
    if not cleaned_targets:
        return {}
    database = voiceover_sql_db()
    source_product_filters = []
    for app_id in app_ids:
        source_condition = voiceover_source_product_sql_condition(app_id, "s")
        if source_condition:
            source_product_filters.append(
                "(CAST(d.app_id AS CHAR)='%s' AND %s)" % (mysql_escape_literal(app_id), source_condition)
            )
    source_product_sql = " AND (%s)" % " OR ".join(source_product_filters) if source_product_filters else ""
    rows = run_mysql(
        (
            "SELECT d.series_code, CAST(d.app_id AS CHAR), COUNT(DISTINCT s.id), COUNT(DISTINCT d.content_id) "
            "FROM `%s`.ads_drama_info d FORCE INDEX (scoo) "
            "LEFT JOIN `%s`.ads_custom_source s FORCE INDEX (idx_source_type_source_id) "
            "ON s.data_source=%d AND s.data_source_id=d.content_id "
            "AND s.is_delete=0 AND COALESCE(s.url, '')<>''%s "
            "WHERE d.series_code IN (%s) AND CAST(d.app_id AS CHAR) IN (%s) AND d.content_id<>'' "
            "GROUP BY d.series_code, d.app_id"
        )
        % (
            database,
            database,
            VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE,
            source_product_sql,
            voiceover_sql_in(series_codes),
            voiceover_sql_in(app_ids),
        )
    )
    result = {}
    for row in rows:
        series_code = str(row[0] if len(row) > 0 else "").strip()
        app_id = str(row[1] if len(row) > 1 else "").strip()
        if not series_code or not app_id:
            continue
        result[(series_code, app_id)] = {
            "material_count": voiceover_int(row[2] if len(row) > 2 else 0, 0),
            "series_content_count": voiceover_int(row[3] if len(row) > 3 else 0, 0),
        }
    return result


def voiceover_material_count_map_for_content_targets(content_targets):
    cleaned_targets = []
    seen_targets = set()
    app_ids = []
    for content_id, app_id in content_targets or []:
        content_id = str(content_id or "").strip()
        app_id = str(app_id or "").strip()
        if not content_id or not app_id:
            continue
        key = (content_id, app_id)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        cleaned_targets.append(key)
        if app_id not in app_ids:
            app_ids.append(app_id)
    if not cleaned_targets:
        return {}
    database = voiceover_sql_db()
    target_sql = " UNION ALL ".join(
        "SELECT '%s' AS content_id, '%s' AS app_id"
        % (mysql_escape_literal(content_id), mysql_escape_literal(app_id))
        for content_id, app_id in cleaned_targets
    )
    source_product_filters = []
    for app_id in app_ids:
        source_condition = voiceover_source_product_sql_condition(app_id, "s")
        if source_condition:
            source_product_filters.append(
                "(target.app_id='%s' AND %s)" % (mysql_escape_literal(app_id), source_condition)
            )
    source_product_sql = " AND (%s)" % " OR ".join(source_product_filters) if source_product_filters else ""
    rows = run_mysql(
        (
            "SELECT target.content_id, target.app_id, COUNT(DISTINCT s.id) "
            "FROM (%s) target "
            "LEFT JOIN `%s`.ads_custom_source s FORCE INDEX (idx_source_type_source_id) "
            "ON s.data_source=%d AND s.data_source_id=target.content_id "
            "AND s.is_delete=0 AND COALESCE(s.url, '')<>''%s "
            "GROUP BY target.content_id, target.app_id"
        )
        % (
            target_sql,
            database,
            VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE,
            source_product_sql,
        )
    )
    result = {}
    for row in rows:
        content_id = str(row[0] if len(row) > 0 else "").strip()
        app_id = str(row[1] if len(row) > 1 else "").strip()
        if not content_id or not app_id:
            continue
        result[(content_id, app_id)] = {
            "material_count": voiceover_int(row[2] if len(row) > 2 else 0, 0),
        }
    return result


def voiceover_material_counts(payload):
    records = []
    content_targets = []
    content_ids = voiceover_parse_content_ids(payload)
    product_app_ids = voiceover_parse_product_app_ids(payload)
    drama_map = lookup_voiceover_drama_info_map_by_app(content_ids, product_app_ids)
    for app_id in product_app_ids:
        product = voiceover_product_meta(app_id)
        for content_id in content_ids:
            try:
                drama = drama_map.get((app_id, content_id))
                if not drama:
                    raise StructuredApiError("drama_not_found", "未在%s中找到剧 ID：%s" % (product.get("label") or app_id, content_id))
                content_targets.append((content_id, app_id))
                records.append({
                    "content_id": content_id,
                    "product": product,
                    "drama": drama,
                    "status": "ok",
                })
            except Exception as exc:
                records.append({
                    "content_id": content_id,
                    "product_app_id": app_id,
                    "product_key": product.get("key", ""),
                    "product_label": product.get("label", ""),
                    "material_count": 0,
                    "series_content_count": 0,
                    "status": "failed",
                    "error": api_error_payload(exc).get("message") or str(exc),
                })

    count_map = voiceover_material_count_map_for_content_targets(content_targets)
    items = []
    for record in records:
        if record.get("status") != "ok":
            items.append(record)
            continue
        drama = record.get("drama") or {}
        product = record.get("product") or voiceover_product_meta(drama.get("app_id", ""))
        count_info = count_map.get((record.get("content_id", ""), product.get("app_id", "")), {})
        items.append({
            "content_id": record.get("content_id", ""),
            "drama_name": drama.get("name", ""),
            "series_code": drama.get("series_code", ""),
            "series_content_count": 1,
            "count_scope": "content_id",
            "app_id": drama.get("app_id", ""),
            "app": drama.get("app", ""),
            "product_app_id": product.get("app_id", ""),
            "product_key": product.get("key", ""),
            "product_label": product.get("label", ""),
            "product_owner": product.get("label", ""),
            "country": drama.get("country", ""),
            "language": drama.get("language", ""),
            "material_count": count_info.get("material_count", 0),
            "status": "ok",
        })
    return {"items": items, "total": len(items)}


def voiceover_material_dedupe_key(item):
    name = str(item.get("name") or item.get("resource_name") or "").lower()
    name = re.sub(r"\.(mp4|mov|webm|m4v)$", "", name)
    name = re.sub(r"\b(en|es|tr|ru|pt|br|hi|id|de|fr|it|ar|th|vi|ms|tl|ja|ko)\b", "", name)
    name = re.sub(r"\d{4}[-_.]?\d{1,2}[-_.]?\d{1,2}", "", name)
    name = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name)
    duration = voiceover_int(item.get("duration"), 0)
    return "%s:%s" % (duration, name[:80])


def voiceover_material_source_rows_for_series(content_ids, scan_limit, product_app_id=""):
    content_ids = [str(content_id or "").strip() for content_id in (content_ids or []) if str(content_id or "").strip()]
    if not content_ids:
        return []
    scan_limit = max(1, min(10000, voiceover_int(scan_limit, VOICEOVER_FILTER_SOURCE_SCAN_LIMIT)))
    database = voiceover_sql_db()
    content_id_sql = voiceover_sql_in(content_ids)
    source_product_condition = voiceover_source_product_sql_condition(product_app_id, "s")
    source_product_sql = "AND %s " % source_product_condition if source_product_condition else ""
    sql = (
        "SELECT CAST(s.id AS CHAR), COALESCE(s.name, ''), COALESCE(s.url, ''), "
        "COALESCE(s.category, ''), COALESCE(s.product, ''), COALESCE(s.country, ''), "
        "COALESCE(s.language, ''), COALESCE(s.data_source_id, ''), COALESCE(s.content_sign, ''), "
        "COALESCE(s.video_duration, 0), COALESCE(s.designer, ''), "
        "CAST(s.user_id AS CHAR), CAST(s.initiator AS CHAR) "
        "FROM `%s`.ads_custom_source s FORCE INDEX (idx_source_type_source_id) "
        "WHERE s.data_source=%d AND s.data_source_id IN (%s) "
        "AND s.is_delete=0 AND COALESCE(s.url, '')<>'' %s"
        "ORDER BY s.updated_at DESC, s.id DESC LIMIT %d"
    ) % (database, VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE, content_id_sql, source_product_sql, scan_limit)
    return run_mysql(sql)


def voiceover_insight_summary_for_resources(resource_ids, roas_threshold, limit):
    resource_ids = [str(resource_id or "").strip() for resource_id in (resource_ids or []) if str(resource_id or "").strip()]
    if not resource_ids:
        return []
    database = voiceover_sql_db()
    resource_id_sql = voiceover_sql_in(resource_ids)
    summary_limit = max(20, min(300, voiceover_int(limit, VOICEOVER_DEFAULT_MIN_CANDIDATES) * 5))
    sql = (
        "SELECT CAST(resource_id AS CHAR), MAX(resource_name), MAX(resource_tag), "
        "SUM(spend), SUM(revenue), SUM(af_revenue0), MIN(dt), MAX(dt), COUNT(*) "
        "FROM `%s`.ads_custom_source_insight FORCE INDEX (idx_ridstype) "
        "WHERE resource_id IN (%s) "
        "GROUP BY resource_id "
        "HAVING SUM(spend)>0 AND SUM(revenue)/SUM(spend)*100 >= %.6f "
        "ORDER BY SUM(revenue)/SUM(spend) DESC, SUM(spend) DESC LIMIT %d"
    ) % (database, resource_id_sql, voiceover_float(roas_threshold, VOICEOVER_DEFAULT_ROAS_THRESHOLD), summary_limit)
    return run_mysql(sql)


def voiceover_material_item_from_source_and_summary(source_row, summary_row):
    spend = voiceover_float(summary_row[3] if len(summary_row) > 3 else 0)
    revenue = voiceover_float(summary_row[4] if len(summary_row) > 4 else 0)
    revenue0 = voiceover_float(summary_row[5] if len(summary_row) > 5 else 0)
    roas = round(revenue / spend * 100, 2) if spend > 0 else 0.0
    roas0 = round(revenue0 / spend * 100, 2) if spend > 0 else 0.0
    designer_id = str(source_row[10] if len(source_row) > 10 else "").strip()
    source_user_id = str(source_row[11] if len(source_row) > 11 else "").strip()
    source_initiator = str(source_row[12] if len(source_row) > 12 else "").strip()
    source_product = str(source_row[4] if len(source_row) > 4 else "").strip()
    return {
        "material_id": str(source_row[0] if len(source_row) > 0 else "").strip(),
        "name": str(source_row[1] if len(source_row) > 1 else "").strip(),
        "url": str(source_row[2] if len(source_row) > 2 else "").strip(),
        "category": str(source_row[3] if len(source_row) > 3 else "").strip(),
        "product": source_product,
        "source_product_label": voiceover_source_product_label(source_product),
        "country": str(source_row[5] if len(source_row) > 5 else "").strip(),
        "language": str(source_row[6] if len(source_row) > 6 else "").strip(),
        "source_content_id": str(source_row[7] if len(source_row) > 7 else "").strip(),
        "content_sign": str(source_row[8] if len(source_row) > 8 else "").strip(),
        "duration": voiceover_int(source_row[9] if len(source_row) > 9 else 0),
        "designer": designer_id,
        "designer_id": designer_id,
        "source_user_id": source_user_id,
        "source_initiator": source_initiator,
        "resource_name": str(summary_row[1] if len(summary_row) > 1 else "").strip(),
        "resource_tag": str(summary_row[2] if len(summary_row) > 2 else "").strip(),
        "spend": round(spend, 2),
        "revenue": round(revenue, 2),
        "revenue0": round(revenue0, 2),
        "roas": roas,
        "roas0": roas0,
        "stats_date_from": str(summary_row[6] if len(summary_row) > 6 else "").strip(),
        "stats_date_to": str(summary_row[7] if len(summary_row) > 7 else "").strip(),
        "insight_rows": voiceover_int(summary_row[8] if len(summary_row) > 8 else 0),
    }


def voiceover_material_rows_for_series(series_code, roas_threshold=None, limit=15, content_ids=None, product_app_id=""):
    series_code = str(series_code or "").strip()
    if not series_code:
        return []
    limit = max(0, min(100, voiceover_int(limit, VOICEOVER_DEFAULT_MIN_CANDIDATES)))
    if limit <= 0:
        return []
    roas_threshold = voiceover_float(roas_threshold, VOICEOVER_DEFAULT_ROAS_THRESHOLD)
    database = voiceover_sql_db()
    content_ids = content_ids if content_ids is not None else voiceover_series_content_ids(series_code)
    if not content_ids:
        return []
    scan_limit = max(VOICEOVER_FILTER_SOURCE_SCAN_LIMIT, limit * 80)
    scan_limit = min(max(1, VOICEOVER_FILTER_SOURCE_SCAN_MAX), scan_limit)
    scan_limits = [scan_limit]
    max_scan = max(scan_limit, VOICEOVER_FILTER_SOURCE_SCAN_MAX)
    if max_scan > scan_limit:
        scan_limits.append(max_scan)
    best_items = []
    seen_scan_limits = set()
    for current_scan_limit in scan_limits:
        if current_scan_limit in seen_scan_limits:
            continue
        seen_scan_limits.add(current_scan_limit)
        source_rows = voiceover_material_source_rows_for_series(content_ids, current_scan_limit, product_app_id=product_app_id)
        source_by_id = {str(row[0] if row else "").strip(): row for row in source_rows if row and str(row[0]).strip()}
        summary_rows = voiceover_insight_summary_for_resources(source_by_id.keys(), roas_threshold, limit)
        items = []
        for summary_row in summary_rows:
            material_id = str(summary_row[0] if summary_row else "").strip()
            source_row = source_by_id.get(material_id)
            if not source_row:
                continue
            items.append(voiceover_material_item_from_source_and_summary(source_row, summary_row))
        items = sorted(items, key=lambda item: (-voiceover_float(item.get("roas")), -voiceover_float(item.get("spend")), -voiceover_int(item.get("material_id"))))
        best_items = items[:limit]
        if len(best_items) >= limit or len(source_rows) < current_scan_limit:
            return best_items
    if best_items or not VOICEOVER_FILTER_LEGACY_FALLBACK:
        return best_items
    content_id_sql = voiceover_sql_in(content_ids)
    source_product_condition = voiceover_source_product_sql_condition(product_app_id, "s")
    source_product_sql = "AND %s " % source_product_condition if source_product_condition else ""
    sql = (
        "SELECT CAST(s.id AS CHAR), COALESCE(s.name, ''), COALESCE(s.url, ''), "
        "COALESCE(s.category, ''), COALESCE(s.product, ''), COALESCE(s.country, ''), "
        "COALESCE(s.language, ''), COALESCE(s.data_source_id, ''), COALESCE(s.content_sign, ''), "
        "COALESCE(s.video_duration, 0), COALESCE(s.designer, ''), "
        "CAST(s.user_id AS CHAR), CAST(s.initiator AS CHAR), "
        "COALESCE(i.resource_name, ''), COALESCE(i.resource_tag, ''), "
        "COALESCE(i.spend, 0), COALESCE(i.revenue, 0), COALESCE(i.revenue0, 0), "
        "COALESCE(i.stats_date_from, ''), COALESCE(i.stats_date_to, ''), COALESCE(i.insight_rows, 0) "
        "FROM ("
        "SELECT resource_id, MAX(resource_name) AS resource_name, MAX(resource_tag) AS resource_tag, "
        "SUM(spend) AS spend, SUM(revenue) AS revenue, SUM(af_revenue0) AS revenue0, "
        "MIN(dt) AS stats_date_from, MAX(dt) AS stats_date_to, COUNT(*) AS insight_rows "
        "FROM `%s`.ads_custom_source_insight FORCE INDEX (ddds) "
        "WHERE data_source=%d AND data_source_id IN (%s) "
        "GROUP BY resource_id HAVING spend>0 AND revenue/spend*100 >= %.6f"
        ") i "
        "JOIN `%s`.ads_custom_source s ON CAST(s.id AS CHAR)=CAST(i.resource_id AS CHAR) "
        "WHERE s.data_source=%d AND s.data_source_id IN (%s) AND s.is_delete=0 AND COALESCE(s.url, '')<>'' %s"
        "ORDER BY i.revenue/i.spend DESC, i.spend DESC, s.id DESC LIMIT %d"
    ) % (
        database,
        VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE,
        content_id_sql,
        roas_threshold,
        database,
        VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE,
        content_id_sql,
        source_product_sql,
        limit,
    )
    rows = run_mysql(sql)
    items = []
    for row in rows:
        spend = voiceover_float(row[15] if len(row) > 15 else 0)
        revenue = voiceover_float(row[16] if len(row) > 16 else 0)
        revenue0 = voiceover_float(row[17] if len(row) > 17 else 0)
        roas = round(revenue / spend * 100, 2) if spend > 0 else 0.0
        roas0 = round(revenue0 / spend * 100, 2) if spend > 0 else 0.0
        designer_id = str(row[10] if len(row) > 10 else "").strip()
        source_user_id = str(row[11] if len(row) > 11 else "").strip()
        source_initiator = str(row[12] if len(row) > 12 else "").strip()
        source_product = str(row[4] if len(row) > 4 else "").strip()
        items.append({
            "material_id": str(row[0] if len(row) > 0 else "").strip(),
            "name": str(row[1] if len(row) > 1 else "").strip(),
            "url": str(row[2] if len(row) > 2 else "").strip(),
            "category": str(row[3] if len(row) > 3 else "").strip(),
            "product": source_product,
            "source_product_label": voiceover_source_product_label(source_product),
            "country": str(row[5] if len(row) > 5 else "").strip(),
            "language": str(row[6] if len(row) > 6 else "").strip(),
            "source_content_id": str(row[7] if len(row) > 7 else "").strip(),
            "content_sign": str(row[8] if len(row) > 8 else "").strip(),
            "duration": voiceover_int(row[9] if len(row) > 9 else 0),
            "designer": designer_id,
            "designer_id": designer_id,
            "source_user_id": source_user_id,
            "source_initiator": source_initiator,
            "resource_name": str(row[13] if len(row) > 13 else "").strip(),
            "resource_tag": str(row[14] if len(row) > 14 else "").strip(),
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "revenue0": round(revenue0, 2),
            "roas": roas,
            "roas0": roas0,
            "stats_date_from": str(row[18] if len(row) > 18 else "").strip(),
            "stats_date_to": str(row[19] if len(row) > 19 else "").strip(),
            "insight_rows": voiceover_int(row[20] if len(row) > 20 else 0),
        })
    return items


def voiceover_apply_candidate_rules_legacy(items, roas_threshold, min_candidates):
    roas_threshold = voiceover_float(roas_threshold, VOICEOVER_DEFAULT_ROAS_THRESHOLD)
    min_candidates = max(0, voiceover_int(min_candidates, VOICEOVER_DEFAULT_MIN_CANDIDATES))
    for item in items:
        item["roas_threshold"] = roas_threshold
        item["roas_pass"] = item.get("roas", 0) >= roas_threshold
        item["risk_label"] = ""
        item["candidate_status"] = "not_selected"
        item["candidate_reason"] = "未进入默认候选"
        item["selected_by_default"] = False
        item["duplicate_of"] = ""

    selected = []
    seen_keys = {}
    pass_items = sorted([item for item in items if item["roas_pass"]], key=lambda item: (-item["roas"], -item["spend"]))
    for item in pass_items:
        key = voiceover_material_dedupe_key(item)
        if key and key in seen_keys:
            item["candidate_status"] = "duplicate"
            item["candidate_reason"] = "疑似同素材不同语种/版本，默认去重"
            item["duplicate_of"] = seen_keys[key]
            continue
        seen_keys[key] = item["material_id"]
        item["candidate_status"] = "roas_pass"
        item["candidate_reason"] = "ROAS 达标"
        item["selected_by_default"] = True
        selected.append(item)

    if len(selected) < min_candidates:
        fallback_items = sorted(
            [item for item in items if item["candidate_status"] == "not_selected"],
            key=lambda item: (-item["spend"], -item["roas"]),
        )
        for item in fallback_items:
            if len(selected) >= min_candidates:
                break
            key = voiceover_material_dedupe_key(item)
            if key and key in seen_keys:
                item["candidate_status"] = "duplicate"
                item["candidate_reason"] = "疑似同素材不同语种/版本，默认去重"
                item["duplicate_of"] = seen_keys[key]
                continue
            seen_keys[key] = item["material_id"]
            item["candidate_status"] = "substitute"
            item["candidate_reason"] = "ROAS 未达标，按消耗补足候选"
            item["risk_label"] = "替补素材"
            item["selected_by_default"] = True
            selected.append(item)
    return items


def voiceover_apply_candidate_rules(items, roas_threshold, min_candidates):
    roas_threshold = voiceover_float(roas_threshold, VOICEOVER_DEFAULT_ROAS_THRESHOLD)
    for item in items:
        item["roas_threshold"] = roas_threshold
        item["roas_pass"] = item.get("roas", 0) >= roas_threshold
        item["risk_label"] = ""
        item["candidate_status"] = "roas_pass" if item["roas_pass"] else "not_selected"
        item["candidate_reason"] = "ROAS 达标" if item["roas_pass"] else "ROAS 未达标"
        item["selected_by_default"] = bool(item["roas_pass"])
        item["duplicate_of"] = ""
    return items


def voiceover_filter_materials(payload):
    payload = payload or {}
    roas_threshold = voiceover_float(payload.get("roas_threshold"), VOICEOVER_DEFAULT_ROAS_THRESHOLD)
    min_candidates = voiceover_int(payload.get("min_candidates"), VOICEOVER_DEFAULT_MIN_CANDIDATES)
    candidate_limit = max(0, min(100, min_candidates))
    groups = []
    all_items = []
    product_app_ids = voiceover_parse_product_app_ids(payload)
    series_inputs = voiceover_parse_series_codes(payload, required=False)
    target_language = voiceover_parse_target_language(payload)
    target_audio_type = voiceover_parse_target_audio_type(payload)
    content_ids = [] if series_inputs else voiceover_parse_content_ids(payload)
    if series_inputs:
        if not target_language:
            raise StructuredApiError("target_language_required", "资源 ID 模式请选择目标语种")
        if target_audio_type == "":
            raise StructuredApiError("target_audio_type_required", "资源 ID 模式请选择配音类型")
        filter_mode = "series_code"
        target_ids = series_inputs
        drama_map = lookup_voiceover_drama_info_map_by_series(
            series_inputs,
            product_app_ids,
            target_language=target_language,
            target_audio_type=target_audio_type,
        )
        series_codes = list(series_inputs)
        for drama in drama_map.values():
            series_code = (drama or {}).get("series_code", "")
            if series_code and series_code not in series_codes:
                series_codes.append(series_code)
    else:
        filter_mode = "content_id"
        target_ids = content_ids
        drama_map = lookup_voiceover_drama_info_map_by_app(content_ids, product_app_ids)
        series_codes = []
        for drama in drama_map.values():
            series_code = (drama or {}).get("series_code", "")
            if series_code and series_code not in series_codes:
                series_codes.append(series_code)
    series_content_map = voiceover_series_content_ids_map(series_codes, product_app_ids)
    materials_by_series = {}
    for app_id in product_app_ids:
        product = voiceover_product_meta(app_id)
        for target_id in target_ids:
            if filter_mode == "series_code":
                drama = voiceover_pick_drama_by_series(drama_map, target_id, app_id)
                missing_error = "未在%s中找到资源 ID %s 下目标语种 %s / 配音类型 %s 的剧" % (
                    product.get("label") or app_id,
                    target_id,
                    target_language,
                    target_audio_type,
                )
            else:
                drama = drama_map.get((app_id, target_id))
                missing_error = "未在%s中找到剧 ID：%s" % (product.get("label") or app_id, target_id)
            if not drama:
                groups.append({
                    "filter_mode": filter_mode,
                    "target_id": target_id,
                    "target_language": target_language,
                    "target_audio_type": target_audio_type,
                    "content_id": target_id if filter_mode == "content_id" else "",
                    "series_code": target_id if filter_mode == "series_code" else "",
                    "product_app_id": product.get("app_id", ""),
                    "product_key": product.get("key", ""),
                    "product_label": product.get("label", ""),
                    "material_count": 0,
                    "default_candidate_count": 0,
                    "substitute_count": 0,
                    "status": "failed",
                    "error": missing_error,
                })
                continue
            content_id = drama.get("content_id", "") if filter_mode == "series_code" else target_id
            series_code = drama.get("series_code", "")
            series_key = (series_code, app_id)
            if series_key not in materials_by_series:
                series_materials = voiceover_material_rows_for_series(
                    series_code,
                    roas_threshold=roas_threshold,
                    limit=candidate_limit,
                    content_ids=series_content_map.get(series_key) or [],
                    product_app_id=app_id,
                )
                voiceover_apply_candidate_rules(series_materials, roas_threshold, min_candidates)
                materials_by_series[series_key] = series_materials
            materials = [dict(item) for item in materials_by_series.get(series_key, [])]
            for item in materials:
                item["target_content_id"] = content_id
                item["target_drama_name"] = drama.get("name", "")
                item["target_series_code"] = drama.get("series_code", "")
                item["target_app_id"] = product.get("app_id", "") or drama.get("app_id", "")
                item["target_product_key"] = product.get("key", "")
                item["target_product_label"] = product.get("label", "")
                item["target_app"] = drama.get("app", "")
                item["target_country"] = drama.get("country", "")
                item["target_language"] = drama.get("language", "")
                item["target_audio_type"] = drama.get("audio_type", "")
            default_count = len([item for item in materials if item.get("selected_by_default")])
            groups.append({
                "filter_mode": filter_mode,
                "target_id": target_id,
                "target_language": drama.get("language", ""),
                "target_audio_type": drama.get("audio_type", ""),
                "content_id": content_id,
                "series_code": series_code,
                "product_app_id": product.get("app_id", ""),
                "product_key": product.get("key", ""),
                "product_label": product.get("label", ""),
                "drama": drama,
                "material_count": len(materials),
                "default_candidate_count": default_count,
                "substitute_count": 0,
            })
            all_items.extend(materials)
    if groups and all(group.get("status") == "failed" for group in groups):
        raise StructuredApiError(
            "drama_not_found",
            "；".join(group.get("error", "") for group in groups[:3] if group.get("error")) or "未找到匹配产品下的剧 ID / 资源 ID",
        )
    return {
        "items": all_items,
        "groups": groups,
        "total": len(all_items),
        "roas_threshold": roas_threshold,
        "min_candidates": min_candidates,
    }


def voiceover_lookup_material(material_id, target_content_id=""):
    material_id = str(material_id or "").strip()
    if not material_id:
        raise StructuredApiError("invalid_material_id", "素材 ID 不能为空")
    material = voiceover_lookup_material_map([material_id]).get(material_id)
    if not material:
        raise StructuredApiError("material_not_found", "未找到素材：%s" % material_id)
    return material


def voiceover_lookup_material_map(material_ids):
    cleaned = []
    seen = set()
    for material_id in material_ids or []:
        value = str(material_id or "").strip()
        if value and value not in seen:
            cleaned.append(value)
            seen.add(value)
    if not cleaned:
        return {}
    database = voiceover_sql_db()
    numeric_ids = [value for value in cleaned if re.match(r"^\d+$", value)]
    string_ids = [value for value in cleaned if not re.match(r"^\d+$", value)]
    id_filters = []
    if numeric_ids:
        id_filters.append("s.id IN (%s)" % ",".join(str(voiceover_int(value)) for value in numeric_ids))
    if string_ids:
        id_filters.append("CAST(s.id AS CHAR) IN (%s)" % voiceover_sql_in(string_ids))
    if not id_filters:
        return {}
    sql = (
        "SELECT CAST(s.id AS CHAR), COALESCE(s.name, ''), COALESCE(s.url, ''), "
        "COALESCE(s.category, ''), COALESCE(s.product, ''), COALESCE(s.country, ''), "
        "COALESCE(s.language, ''), COALESCE(s.data_source_id, ''), COALESCE(s.video_duration, 0) "
        "FROM `%s`.ads_custom_source s "
        "WHERE (%s) AND s.data_source=%d AND s.is_delete=0 AND COALESCE(s.url, '')<>''"
    ) % (database, " OR ".join(id_filters), VOICEOVER_CUSTOM_SOURCE_DATA_SOURCE)
    rows = run_mysql(sql)
    result = {}
    for row in rows:
        material_id = str(row[0] if len(row) > 0 else "").strip()
        if not material_id:
            continue
        source_product = str(row[4] if len(row) > 4 else "").strip()
        result[material_id] = {
            "material_id": material_id,
            "name": str(row[1] if len(row) > 1 else "").strip(),
            "url": str(row[2] if len(row) > 2 else "").strip(),
            "category": str(row[3] if len(row) > 3 else "").strip(),
            "product": source_product,
            "source_product_label": voiceover_source_product_label(source_product),
            "country": str(row[5] if len(row) > 5 else "").strip(),
            "language": str(row[6] if len(row) > 6 else "").strip(),
            "source_content_id": str(row[7] if len(row) > 7 else "").strip(),
            "duration": voiceover_int(row[8] if len(row) > 8 else 0),
            "spend": 0,
            "revenue": 0,
            "roas": 0,
        }
    return result


def list_voiceover_designers():
    database = voiceover_sql_db()
    rows = run_mysql(
        (
            "SELECT DISTINCT CAST(aug.user_id AS CHAR), CAST(aug.sub_user_id AS CHAR), "
            "COALESCE(NULLIF(aug.name, ''), NULLIF(au.name, ''), NULLIF(au.username, ''), CAST(aug.user_id AS CHAR)), "
            "COALESCE(aug.email, ''), COALESCE(au.username, '') "
            "FROM `%s`.admin_role_users aru "
            "JOIN `%s`.admin_role_apps ara ON ara.id=aru.role_app_id "
            "LEFT JOIN `%s`.admin_user_group aug ON aug.sub_user_id=aru.user_id AND aug.status=0 "
            "LEFT JOIN `%s`.admin_users au ON au.id=aru.user_id "
            "WHERE CAST(ara.id AS CHAR)='%s' AND aug.user_id IS NOT NULL "
            "ORDER BY 3 ASC"
        )
        % (database, database, database, database, mysql_escape_literal(VOICEOVER_DESIGNER_ROLE_APP_ID))
    )
    items = []
    seen = set()
    for row in rows:
        user_id = str(row[0] if len(row) > 0 else "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        name = str(row[2] if len(row) > 2 else "").strip() or user_id
        username = str(row[4] if len(row) > 4 else "").strip()
        items.append({
            "user_id": user_id,
            "sub_user_id": str(row[1] if len(row) > 1 else "").strip(),
            "name": name,
            "email": str(row[3] if len(row) > 3 else "").strip(),
            "username": username,
            "label": "%s / %s" % (name, username or user_id),
        })
    return {"items": items, "group_role_app_id": VOICEOVER_DESIGNER_ROLE_APP_ID}


def lookup_voiceover_admin_group_for_actor(actor):
    actor = actor or {}
    email = str(actor.get("email") or "").strip()
    name = str(actor.get("name") or "").strip()
    database = voiceover_sql_db()
    filters = []
    if email:
        filters.append("LOWER(TRIM(aug.email))=LOWER(TRIM('%s'))" % mysql_escape_literal(email))
    if name:
        filters.append("aug.name='%s'" % mysql_escape_literal(name))
    if not filters:
        return {}
    rows = run_mysql(
        (
            "SELECT CAST(aug.user_id AS CHAR), CAST(aug.sub_user_id AS CHAR), aug.name, aug.email "
            "FROM `%s`.admin_user_group aug WHERE aug.status=0 AND (%s) "
            "ORDER BY CASE WHEN LOWER(TRIM(aug.email))=LOWER(TRIM('%s')) THEN 0 ELSE 1 END LIMIT 1"
        )
        % (database, " OR ".join(filters), mysql_escape_literal(email))
    )
    if not rows:
        return {}
    row = rows[0]
    return {
        "user_id": str(row[0] if len(row) > 0 else "").strip(),
        "sub_user_id": str(row[1] if len(row) > 1 else "").strip(),
        "name": str(row[2] if len(row) > 2 else "").strip(),
        "email": str(row[3] if len(row) > 3 else "").strip(),
    }


def voiceover_safe_name_part(value, fallback="NA"):
    text = str(value or "").strip() or fallback
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[\\/:*?\"<>|#]+", "-", text)
    return text[:64] or fallback


def build_voiceover_task_name(drama, requester):
    date_text = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d")
    random_text = secrets.token_hex(3)
    requester_text = requester.get("name") or requester.get("email") or requester.get("user_id") or "unknown"
    return "AI_%s_%s_%s_%s_%s_%s" % (
        voiceover_safe_name_part(drama.get("product_name")),
        voiceover_safe_name_part(drama.get("language")),
        voiceover_safe_name_part(drama.get("content_id")),
        date_text,
        voiceover_safe_name_part(requester_text),
        random_text,
    )


def post_voiceover_kol_task(body):
    if not VOICEOVER_KOL_TASK_API_TOKEN:
        raise StructuredApiError("kol_task_token_missing", "生成需求接口 token 未配置")
    response = requests.post(
        VOICEOVER_KOL_TASK_API_URL,
        headers={
            "Authorization": "Bearer %s" % VOICEOVER_KOL_TASK_API_TOKEN,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=VOICEOVER_KOL_TASK_API_TIMEOUT,
    )
    try:
        data = response.json()
    except Exception:
        preview = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", response.text or "", flags=re.IGNORECASE | re.DOTALL)
        preview = unescape(re.sub(r"<[^>]+>", " ", preview))
        preview = re.sub(r"\s+", " ", preview).strip()[:500]
        data = {"message": preview or response.text[:500]}
    if response.status_code >= 400:
        response_message = data.get("message") if isinstance(data, dict) else str(data)
        message = "生成需求接口请求失败 HTTP %s" % response.status_code
        if response_message:
            message = "%s：%s" % (message, str(response_message)[:220])
        raise StructuredApiError("kol_task_http_failed", message, status=response.status_code, response=data)
    code = data.get("code") if isinstance(data, dict) else None
    if code not in (0, "0", None) and not data.get("success"):
        raise StructuredApiError("kol_task_failed", str(data.get("message") or data.get("error") or data), response=data)
    return data


def create_voiceover_design_tasks(payload, session):
    payload = payload or {}
    raw_items = payload.get("items") or payload.get("materials") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise StructuredApiError("materials_required", "请至少选择一个素材")
    actor = ad_material_actor(session)
    requester_group = lookup_voiceover_admin_group_for_actor(actor)
    requester_user_id = requester_group.get("user_id") or requester_group.get("sub_user_id")
    if not requester_user_id:
        raise StructuredApiError("requester_missing", "无法通过当前登录人定位 admin_user_group.user_id")
    valid_designer_ids = {str(item.get("user_id") or "").strip() for item in list_voiceover_designers().get("items", [])}
    target_content_ids = []
    target_app_ids = []
    material_ids = []
    for raw_item in raw_items:
        raw_item = raw_item or {}
        target_content_id = str(raw_item.get("target_content_id") or raw_item.get("content_id") or "").strip()
        target_app_id = voiceover_normalize_product_app_id(
            raw_item.get("target_app_id")
            or raw_item.get("target_product_app_id")
            or raw_item.get("product_app_id")
            or raw_item.get("app_id")
            or raw_item.get("target_product_key")
        )
        material_id = str(raw_item.get("material_id") or raw_item.get("id") or "").strip()
        if target_content_id and target_content_id not in target_content_ids:
            target_content_ids.append(target_content_id)
        if target_app_id and target_app_id not in target_app_ids:
            target_app_ids.append(target_app_id)
        if material_id and material_id not in material_ids:
            material_ids.append(material_id)
    drama_map_by_app = lookup_voiceover_drama_info_map_by_app(target_content_ids, target_app_ids) if target_app_ids else {}
    drama_map = lookup_voiceover_drama_info_map(target_content_ids)
    material_map = voiceover_lookup_material_map(material_ids)
    results = []
    errors = []

    def create_one(index, raw_item):
        raw_item = raw_item or {}
        target_content_id = str(raw_item.get("target_content_id") or raw_item.get("content_id") or "").strip()
        target_app_id = voiceover_normalize_product_app_id(
            raw_item.get("target_app_id")
            or raw_item.get("target_product_app_id")
            or raw_item.get("product_app_id")
            or raw_item.get("app_id")
            or raw_item.get("target_product_key")
        )
        material_id = str(raw_item.get("material_id") or raw_item.get("id") or "").strip()
        if target_app_id:
            drama = drama_map_by_app.get((target_app_id, target_content_id))
        else:
            drama = drama_map.get(target_content_id)
        if not drama:
            product = voiceover_product_meta(target_app_id) if target_app_id else {}
            product_text = "%s " % (product.get("label") or target_app_id) if target_app_id else ""
            raise StructuredApiError("drama_not_found", "未在%s剧库中找到剧 ID：%s" % (product_text, target_content_id))
        material = material_map.get(material_id)
        if not material:
            raise StructuredApiError("material_not_found", "未找到素材：%s" % material_id)
        number = max(1, min(100, voiceover_int(raw_item.get("number", raw_item.get("quantity", 1)), 1)))
        designer = str(raw_item.get("designer") or raw_item.get("designer_id") or "").strip()
        if not designer:
            raise StructuredApiError("designer_required", "请选择设计师")
        if designer not in valid_designer_ids:
            raise StructuredApiError("designer_not_allowed", "设计师不在授权接单权限用户组中")
        description = str(raw_item.get("description", raw_item.get("introducation", "")) or "").strip()
        origin_name = voiceover_int(raw_item.get("origin_name", 1), 1)
        end_date = str(raw_item.get("end_date") or "").strip()
        product_app_id = str(drama.get("app_id", "") or "").strip()
        body = {
            "name": build_voiceover_task_name(drama, actor),
            "app": voiceover_int(product_app_id) if re.match(r"^\d+$", product_app_id) else product_app_id,
            "type": 11,
            "content_id": drama.get("kol_content_id", ""),
            "name_keyword": drama.get("name", ""),
            "number": number,
            "country": drama.get("country", ""),
            "language": drama.get("language", ""),
            "tag": [drama.get("name", "")],
            "category": material.get("category", ""),
            "origin_name": origin_name,
            "designer": voiceover_int(designer),
            "is_ad_activity": 0,
            "examples": [material.get("url", "")],
            "introducation": description,
            "user_id": voiceover_int(requester_user_id),
        }
        if end_date:
            body["end_date"] = end_date
        response = post_voiceover_kol_task(body)
        return {
            "index": index,
            "material_id": material_id,
            "target_content_id": target_content_id,
            "target_app_id": drama.get("app_id", ""),
            "name": body["name"],
            "request": body,
            "response": response,
            "status": "created",
        }

    max_workers = max(1, min(VOICEOVER_CREATE_MAX_WORKERS, len(raw_items)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(create_one, index, raw_item): (index, raw_item) for index, raw_item in enumerate(raw_items, 1)}
        for future in as_completed(future_map):
            index, raw_item = future_map[future]
            raw_item = raw_item or {}
            try:
                results.append(future.result())
            except Exception as exc:
                error_payload = api_error_payload(exc)
                errors.append({
                    "index": index,
                    "material_id": str(raw_item.get("material_id") or raw_item.get("id") or "").strip(),
                    "target_content_id": str(raw_item.get("target_content_id") or raw_item.get("content_id") or "").strip(),
                    "target_app_id": str(raw_item.get("target_app_id") or raw_item.get("target_product_app_id") or raw_item.get("product_app_id") or raw_item.get("app_id") or "").strip(),
                    "error": error_payload.get("message") or str(exc),
                    "code": error_payload.get("code", "bad_request"),
                    "status": error_payload.get("status"),
                })
    results = sorted(results, key=lambda item: item.get("index", 0))
    errors = sorted(errors, key=lambda item: item.get("index", 0))
    if errors and not results:
        raise StructuredApiError("voiceover_task_create_failed", "全部设计师任务创建失败", errors=errors)
    return {
        "created_count": len(results),
        "failed_count": len(errors),
        "items": results,
        "errors": errors,
    }

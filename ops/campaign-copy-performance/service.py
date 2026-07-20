#!/usr/bin/env python3
from __future__ import annotations

import collections
import datetime as dt
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
STALE_IF_ERROR_SECONDS = max(
    CACHE_TTL_SECONDS,
    int(os.environ.get("CAMPAIGN_COPY_REPORT_STALE_IF_ERROR_SECONDS", "86400")),
)
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
                SELECT status, COUNT(*) AS n
                FROM ads_business.ads_campaign_rule_logs
                WHERE platform=0 AND level=0 AND data_source=1
                GROUP BY status
                ORDER BY status
                """
            )
            payload["copy_status_counts"] = cur.fetchall()

            cur.execute(
                """
                SELECT l.id, l.account_id, l.origin_id, l.new_id, l.queue_id,
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
                WHERE l.platform=0 AND l.level=0 AND l.data_source=1 AND l.status=1
                ORDER BY l.created_at, l.id
                """
            )
            logs = [
                row
                for row in cur.fetchall()
                if str(row.get("new_id") or "").isdigit() and int(row["new_id"]) > 0
            ]
            payload["logs"] = logs

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

    status_counts = {int(row["status"]): int(row["n"]) for row in raw.get("copy_status_counts", [])}
    terminal = status_counts.get(1, 0) + status_counts.get(2, 0)
    copy_times = [row["copy_at"] for row in campaigns if row["copy_at"]]
    stat_dates = [row["dt"] for row in campaign_daily if row["dt"]]
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
        "copy_pipeline": {
            "running": status_counts.get(0, 0),
            "success": status_counts.get(1, 0),
            "failed": status_counts.get(2, 0),
            "terminal_success_rate": ratio(status_counts.get(1, 0), terminal),
        },
        "campaigns": campaigns,
        "daily": campaign_daily,
    }


def json_default(value: object):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class ReportCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.body: bytes | None = None
        self.loaded_at = 0.0
        self.etag = ""

    def get(self) -> tuple[bytes, str, str, int]:
        now = time.monotonic()
        if self.body is not None and now - self.loaded_at < CACHE_TTL_SECONDS:
            return self.body, self.etag, "hit", int(now - self.loaded_at)
        with self.lock:
            now = time.monotonic()
            if self.body is not None and now - self.loaded_at < CACHE_TTL_SECONDS:
                return self.body, self.etag, "hit", int(now - self.loaded_at)
            try:
                started = time.monotonic()
                payload = build_payload(query_raw())
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=json_default,
                ).encode("utf-8")
                self.body = body
                self.loaded_at = time.monotonic()
                self.etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                print(
                    json.dumps(
                        {
                            "event": "campaign_copy_report_refresh",
                            "seconds": round(self.loaded_at - started, 3),
                            "bytes": len(body),
                            "campaigns": len(payload["campaigns"]),
                            "daily_rows": len(payload["daily"]),
                            "generated_at": payload["meta"]["generated_at"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return body, self.etag, "miss", 0
            except Exception as exc:
                age = int(now - self.loaded_at) if self.body is not None else -1
                print(
                    json.dumps(
                        {
                            "event": "campaign_copy_report_refresh_failed",
                            "error_type": type(exc).__name__,
                            "cache_age_seconds": age,
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                if self.body is not None and age <= STALE_IF_ERROR_SECONDS:
                    return self.body, self.etag, "stale", age
                raise


CACHE = ReportCache()


class Handler(BaseHTTPRequestHandler):
    server_version = "CampaignCopyReport/1.0"

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")

    def _send(self, status: int, content_type: str, body: bytes, send_body: bool = True, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self._security_headers()
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _route(self, send_body: bool):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_PATH.read_bytes(), send_body)
            return
        if path == "/healthz":
            age = int(time.monotonic() - CACHE.loaded_at) if CACHE.body is not None else None
            body = json.dumps({"ok": True, "cache_ready": CACHE.body is not None, "cache_age_seconds": age}).encode()
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body, send_body)
            return
        if path == "/api/data":
            try:
                body, etag, cache_state, cache_age = CACHE.get()
                self._send(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    body,
                    send_body,
                    [("ETag", etag), ("X-Report-Cache", cache_state), ("X-Report-Cache-Age", str(cache_age))],
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
        "copy_status_counts": [{"status": 1, "n": 1}],
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
    }
    payload = build_payload(raw)
    assert payload["meta"]["today"] == "2026-07-20"
    assert payload["campaigns"][0]["user_label"] == "测试用户（ID 42）"
    assert payload["daily"][0]["spend"] == 10
    assert payload["copy_pipeline"]["terminal_success_rate"] == 1.0
    print(json.dumps({"ok": True, "campaigns": 1, "daily_rows": 1}))
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if not INDEX_PATH.is_file():
        raise RuntimeError(f"missing frontend: {INDEX_PATH}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(json.dumps({"event": "server_start", "host": HOST, "port": PORT, "cache_ttl_seconds": CACHE_TTL_SECONDS}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

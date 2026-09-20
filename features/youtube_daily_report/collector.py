from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BEIJING = timezone(timedelta(hours=8))
SITE_ID = "2284"
METRICS = ("revenue_cents", "refund_cents", "installs", "views", "clicks", "recharge")
DB_PATH = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
ENV_PATH = "/root/drama_material_service/.env"


def instant(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("publication_timestamp_without_timezone")
    return result.astimezone(timezone.utc)


def load_ledger(path):
    """Never import/initialize a publisher store or open OAuth credentials."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    deadline = time.monotonic() + 10
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        connection.execute("BEGIN")
        publications = [dict(r) for r in connection.execute("""SELECT
            id,job_id,source_kind,preparation_id,operator_user_id,operator_name,
            video_id,video_state,privacy_status,public_status,workflow,
            video_published_at_utc,status,created_at_utc
            FROM drama_youtube_publish ORDER BY id""")]
        links = [dict(r) for r in connection.execute("""SELECT
            job_id,material_kind,long_url FROM drama_material_short_link""")]
        connection.rollback()
    return publications, links


def campaign_keys(publications, links):
    by_link = {}
    for link in links:
        parsed = urlsplit(link["long_url"] or "")
        if parsed.hostname != "www.dramawavew2a.com" or parsed.path != "/ads/101/2284/view":
            continue
        query = parse_qs(parsed.query)
        # A duplicate attribution parameter is ambiguous, even if one value matches.
        if len(query.get("af_c_id", [])) != 1 or len(query.get("c", [])) != 1:
            continue
        by_link.setdefault((link["job_id"], link["material_kind"]), set()).add(
            (query["af_c_id"][0], query["c"][0]))
    owners, canary = {}, set()
    for row in publications:
        keys = by_link.get((row["job_id"], row["source_kind"]), set())
        if row["operator_name"] == "internal-deployment-canary":
            canary.update(keys)
            continue
        for key in keys:
            owners.setdefault(key, set()).add(row["operator_user_id"])
    return owners, canary


def read_settings(path):
    settings = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_MAPPING_MYSQL_") and "=" in line:
            key, value = line.split("=", 1)
            settings[key] = value.strip().strip('"').strip("'")
    if (settings.get("ADMIN_MAPPING_MYSQL_HOST") != "101.32.56.53"
            or settings.get("ADMIN_MAPPING_MYSQL_PORT") != "63350"):
        raise ValueError("readonly_replica_required")
    if not settings.get("ADMIN_MAPPING_MYSQL_USER") or not settings.get("ADMIN_MAPPING_MYSQL_PASSWORD"):
        raise ValueError("readonly_credentials_unavailable")
    return settings


def collect_metrics(report_date, env_path=ENV_PATH):
    day = date.fromisoformat(report_date).isoformat()
    settings = read_settings(env_path)
    # One date + one site uses the existing (site_id,dt) index. All dimensions
    # are summed once; channel can be empty on revenue/activation rows.
    sql = """SET SESSION time_zone='+00:00';
        SET SESSION MAX_EXECUTION_TIME=8000;
        SET TRANSACTION READ ONLY;
        START TRANSACTION WITH CONSISTENT SNAPSHOT;
        SELECT JSON_OBJECT('kind','connection','read_only',@@read_only);
        SELECT JSON_OBJECT('kind','metric','campaign_id',campaign_id,'campaign',campaign,
          'revenue_cents',CAST(ROUND(SUM(COALESCE(revenue,0))*100) AS SIGNED),
          'refund_cents',CAST(ROUND(SUM(COALESCE(refund_revenue,0))*100) AS SIGNED),
          'installs',SUM(COALESCE(installs,0)),'views',SUM(COALESCE(views,0)),
          'clicks',SUM(COALESCE(clicks,0)),'recharge',SUM(COALESCE(recharge,0)),
          'raw_impressions',SUM(COALESCE(impressions,0)),
          'source_rows',COUNT(*),'updated_at_utc',CAST(MAX(updated_at) AS CHAR))
        FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
        WHERE site_id='2284' AND dt='%s'
        GROUP BY campaign_id,campaign;
        ROLLBACK;
    """ % day
    env = dict(os.environ)
    env.pop("SQL_GATE_BYPASS", None)
    env["MYSQL_PWD"] = settings["ADMIN_MAPPING_MYSQL_PASSWORD"]
    env["SQL_GATE_JOB_NAME"] = "youtube-publisher-daily-report"
    env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "20"
    env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = "20"
    result = subprocess.run([
        "/usr/bin/mysql", "--host=101.32.56.53", "--port=63350",
        "--user=" + settings["ADMIN_MAPPING_MYSQL_USER"], "--connect-timeout=3",
        "--batch", "--raw", "--skip-column-names", "--default-character-set=utf8mb4",
        "--skip-reconnect"], input=sql, text=True, encoding="utf-8",
        capture_output=True, env=env, timeout=60)
    if result.returncode:
        codes = re.findall(r"ERROR\s+(\d+)", result.stderr)
        raise RuntimeError("metric_query_failed_" + "_".join(codes))
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if not records or records[0] != {"kind": "connection", "read_only": 1}:
        raise RuntimeError("readonly_replica_verification_failed")
    if any(r.get("kind") != "metric" for r in records[1:]):
        raise RuntimeError("metric_result_invalid")
    return records[1:]


def empty_metrics():
    return dict.fromkeys(METRICS, 0)


def add_metrics(target, row):
    for field in METRICS:
        value = Decimal(str(row[field]))
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError("non_integral_metric_" + field)
        if field not in ("revenue_cents", "refund_cents") and value < 0:
            raise ValueError("negative_metric_" + field)
        target[field] += int(value)


def summarize(publications, links, metrics, report_date, publication_timezone="Asia/Shanghai"):
    if publication_timezone not in ("Asia/Shanghai", "UTC"):
        raise ValueError("unsupported_publication_timezone")
    day = date.fromisoformat(report_date)
    zone = BEIJING if publication_timezone == "Asia/Shanghai" else timezone.utc
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    owners, canary = campaign_keys(publications, links)
    people, video_owners = {}, {}
    warnings = []
    for row in publications:
        if row["operator_name"] == "internal-deployment-canary":
            continue
        owner = row["operator_user_id"] or "unidentified"
        person = people.setdefault(owner, dict(owner_id=owner, name=row["operator_name"] or owner,
            published=0, publication_ids=[], **empty_metrics()))
        person["name"] = row["operator_name"] or person["name"]
        when = instant(row["video_published_at_utc"])
        confirmed = (row["video_state"] == "published" and row["video_id"]
            and row["privacy_status"] == "public" and when
            and (row["workflow"] != "reviewed_thumbnail" or row["public_status"] == "succeeded"))
        if confirmed and start <= when < end:
            video_owners.setdefault(row["video_id"], []).append((owner, row["id"]))
    for video_id, entries in video_owners.items():
        if len({owner for owner, _ in entries}) != 1:
            raise ValueError("publication_owner_ambiguous")
        owner, publish_id = entries[0]
        people[owner]["published"] += 1
        people[owner]["publication_ids"].append(publish_id)
    matched = []
    unmapped = dict(campaigns=0, source_rows=0, **empty_metrics())
    excluded = dict(campaigns=0, source_rows=0, **empty_metrics())
    metric_keys = set()
    for metric in metrics:
        key = (metric.get("campaign_id") or "", metric.get("campaign") or "")
        if key in metric_keys:
            raise ValueError("duplicate_metric_key")
        metric_keys.add(key)
        if key in canary and key not in owners:
            bucket = excluded
        elif len(owners.get(key, set())) == 1:
            owner = next(iter(owners[key])) or "unidentified"
            add_metrics(people[owner], metric)
            matched.append(dict(owner_id=owner, **metric))
            continue
        else:
            bucket = unmapped
        bucket["campaigns"] += 1
        bucket["source_rows"] += int(metric["source_rows"])
        add_metrics(bucket, metric)
    available = bool(metrics)
    if not available:
        warnings.append("源表尚无本统计日记录，效果数据待同步，不能判为零。")
    if unmapped["campaigns"]:
        warnings.append("有 %s 个归因标识无法唯一关联 AI 发布人，单列待归属，不分摊给个人。" % unmapped["campaigns"])
    # Preserve publishers with zero new posts but historical-content results.
    rows = sorted(people.values(), key=lambda r: (-r["revenue_cents"], -r["published"], r["name"]))
    totals = dict(published=sum(r["published"] for r in rows), **empty_metrics())
    for person in rows:
        add_metrics(totals, person)
    source_totals = empty_metrics()
    for metric in metrics:
        add_metrics(source_totals, metric)
    for field in METRICS:
        if totals[field] + unmapped[field] + excluded[field] != source_totals[field]:
            raise ValueError("source_total_reconciliation_failed")
    if not available:
        for row in rows + [totals]:
            row.update(dict.fromkeys(METRICS, None))
    return dict(schema_version=1, report_date=report_date,
        generated_at=datetime.now(timezone.utc).isoformat(),
        publication_timezone=publication_timezone, metric_timezone="UTC",
        publication_window_utc=[start.isoformat(), end.isoformat()],
        metric_window_utc=[report_date+"T00:00:00+00:00", (day+timedelta(days=1)).isoformat()+"T00:00:00+00:00"],
        source="kunlunads_dev.ads_facebook_page_insight", site_id=SITE_ID,
        scope="all_content_yesterday_metrics_and_yesterday_publications",
        metrics_available=available, publications_available=True,
        impressions_available=False, currency="USD", rows=rows, totals=totals,
        unmatched=unmapped, excluded_canary=excluded, source_totals=source_totals,
        source_campaigns=len(metrics), matched_campaigns=len(matched),
        source_updated_at_utc=max((m["updated_at_utc"] or "" for m in metrics), default=""),
        warnings=warnings, matched_metrics=matched,
        validation=dict(readonly=True, source_totals_reconciled=True,
                        attribution="frozen_campaign_id_and_campaign_to_ledger_operator"))


def collect(report_date, db_path=DB_PATH, env_path=ENV_PATH, publication_timezone="Asia/Shanghai"):
    publications, links = load_ledger(db_path)
    try:
        metrics = collect_metrics(report_date, env_path)
    except Exception as exc:
        report = summarize(publications, links, [], report_date, publication_timezone)
        report["warnings"] = ["效果数据查询失败（%s），本次显示待核实，不按零播报。" % type(exc).__name__]
        report["metric_error"] = type(exc).__name__
        return report
    return summarize(publications, links, metrics, report_date, publication_timezone)

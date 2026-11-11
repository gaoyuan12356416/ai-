"""Read-only inventory estimate for the independent daily material reminder.

No account refresh, media download, reservation, pool update or X API call.
The live selector validates prefetched metadata in memory; the publisher still
owns final media/account validation. SQL goes through the host FIFO gate.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BEIJING = timezone(timedelta(hours=8))
DAILY_EVENT = "x_material_inventory_daily_v2"


def read_env(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    return values


def mysql_rows(env, sql):
    prefix = "X_POST_DAILY_MYSQL_"
    if env[prefix + "HOST"] != "101.32.56.53" or int(env[prefix + "PORT"]) != 63350:
        raise ValueError("inventory_requires_read_replica")
    child_env = dict(os.environ, MYSQL_PWD=env[prefix + "PASSWORD"])
    child_env.pop("SQL_GATE_BYPASS", None)
    result = subprocess.run([
        "/usr/bin/mysql", "--host=" + env[prefix + "HOST"], "--port=63350",
        "--user=" + env[prefix + "USER"], "--connect-timeout=5",
        "--default-character-set=utf8mb4", "--batch", "--raw", "--skip-column-names",
    ], input="SET SESSION TRANSACTION READ ONLY; SET SESSION MAX_EXECUTION_TIME=15000;\n"
             "SELECT @@read_only;\n" + sql, text=True, capture_output=True, env=child_env, timeout=45)
    if result.returncode:
        raise RuntimeError("inventory_source_query_failed")
    lines = result.stdout.splitlines()
    if not lines or lines.pop(0).strip() != "1":
        raise RuntimeError("inventory_source_not_read_only")
    return [json.loads(line) for line in lines if line.strip()]


def prefetch(env, pool):
    schema = env.get("X_POST_DAILY_MYSQL_DATABASE", "kunlunads_dev")
    if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
        raise ValueError("invalid_source_schema")
    materials, dramas, deploy = {}, defaultdict(list), defaultdict(list)
    ids = list(dict.fromkeys(str(int(p["material_id"])) for p in pool))
    for start in range(0, len(ids), 200):
        rows = mysql_rows(env, """SELECT JSON_OBJECT(
          'material_id',CAST(id AS CHAR),'product',product,'material_type',type,
          'material_is_deleted',is_delete,'material_url',url,'material_name',name,
          'material_language',language,'content_id',data_source_id,
          'source_tag_name',tag_name,'video_duration',video_duration)
          FROM `%s`.ads_custom_source WHERE id IN (%s);""" % (schema, ",".join(ids[start:start + 200])))
        materials.update((str(r["material_id"]), r) for r in rows)
    content_ids = sorted({str(r["content_id"]) for r in materials.values() if r.get("content_id")})
    for start in range(0, len(content_ids), 200):
        # Hex literals keep opaque content IDs out of SQL syntax.
        keys = ",".join("CONVERT(0x%s USING utf8mb4)" % value.encode().hex()
                        for value in content_ids[start:start + 200])
        rows = mysql_rows(env, """SELECT JSON_OBJECT(
          'content_id',content_id,'series_code',series_code,'language',language,
          'drama_name',name,'drama_labels',labels,'drama_description',`desc`)
          FROM `%s`.ads_drama_resource FORCE INDEX(content_id)
          WHERE app_id=1479 AND content_id IN (%s) ORDER BY id;""" % (schema, keys))
        for row in rows:
            dramas[str(row["content_id"])].append(row)
        rows = mysql_rows(env, """SELECT JSON_OBJECT(
          'content_id',content_id,'app_id',app_id,'app',app,'language',language,'deploy_time',deploy_time)
          FROM `%s`.ads_drama_info FORCE INDEX(ac)
          WHERE app_id=1479 AND content_id IN (%s);""" % (schema, keys))
        for row in rows:
            deploy[str(row["content_id"])].append(row)
    return materials, dramas, deploy


def metadata_candidates(pool, data, now, allowed_hosts):
    from features.x_posts.selector import DramawaveCandidateSelector, PoolCandidateRejection
    from features.x_accounts.language import canonical_drama_language
    materials, dramas, deploy = data

    class CachedSelector(DramawaveCandidateSelector):
        def _pool_material_rows(self, material_id, **kwargs):
            row = materials.get(material_id)
            if row and row.get("material_type") == 2:
                try:
                    known = float(row.get("video_duration") or 0) > 0
                except (ValueError, TypeError):
                    known = False
                if not known:
                    raise PoolCandidateRejection("material_duration_missing", "duration unavailable")
            return [row] if row else []

        def _pool_drama_rows(self, content_id, language=None):
            return dramas.get(str(content_id), [])

        def _drama_deploy_rows(self, content_id, language, **kwargs):
            return [r for r in deploy.get(str(content_id), [])
                    if canonical_drama_language(r.get("language"), default="") == language]

    selector = CachedSelector(None, now=now)
    candidates, rejected = [], []
    for start in range(0, len(pool), 100):
        batch = pool[start:start + 100]
        selected, errors = selector.select_pool(batch, now.date().isoformat(), limit=len(batch), allow_images=True)
        candidates.extend(selected)
        rejected.extend(errors)
    usable = []
    for row in candidates:
        url = urlsplit(row["material_url"])
        if url.hostname not in allowed_hosts or url.username or url.password or url.port not in (None, 443):
            rejected.append({"error_code": "media_host_not_allowed"})
        else:
            usable.append(row)
    return usable, Counter(r["error_code"] for r in rejected)


def summarize(schedule, accounts, candidates, *, now, threshold_days=2):
    from features.x_accounts.language import canonical_drama_language
    count = (int(schedule["random_daily_count"]) if schedule["schedule_mode"] == "random"
             else len(json.loads(schedule["publish_times_json"]))) if schedule["enabled"] else 0
    daily, premium = Counter(), Counter()
    for account in accounts:
        language = canonical_drama_language(account.get("drama_language"))
        daily[language] += count
        if account.get("subscription_type") in {"basic", "premium", "premium_plus"}:
            premium[language] += count
    short, long = Counter(), Counter()
    for candidate in candidates:
        language = canonical_drama_language(candidate["material_language"])
        (long if candidate["source_duration"] > 1200 else short)[language] += 1
    languages = []
    for language, demand in sorted(daily.items()):
        if not demand:
            continue
        remaining = short[language] + (long[language] if premium[language] else 0)
        threshold = demand * threshold_days
        standard_threshold = (demand - premium[language]) * threshold_days
        # Abundant long videos cannot hide a shortage for ordinary accounts.
        short_low = bool(long[language] and standard_threshold and short[language] <= standard_threshold)
        languages.append({"language": language, "daily_demand": demand, "threshold": threshold,
                          "remaining": remaining, "short_remaining": short[language],
                          "long_remaining": long[language], "premium_daily_demand": premium[language],
                          "standard_threshold": standard_threshold, "short_low": short_low,
                          "alert": remaining <= threshold or short_low})
    return {"event": DAILY_EVENT, "report_date": now.astimezone(BEIJING).date().isoformat(),
            "observed_at": now.isoformat(), "threshold_days": threshold_days, "languages": languages}


def collect(config, now=None):
    from features.x_posts.account_blockers import read_account_publish_blockers
    from features.x_posts.service import _NONBLOCKING_MATERIAL_VALIDATION_SQL, _REVALIDATABLE_MATERIAL_VALIDATION_SQL
    now = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    env = read_env(config.get("publisher_env", "/etc/x-post-daily.env"))
    env.update(read_env(config.get("schedule_env", "/etc/x-post-schedule.env")))
    scan_limit = int(env.get("X_POST_SCHEDULE_SCAN_LIMIT", env.get("X_POST_DAILY_SCAN_LIMIT", 1000)))
    if not 1 <= scan_limit <= 1000:
        raise ValueError("inventory_scan_limit_invalid")
    db = Path(config.get("publisher_db", "/var/lib/x-post-automation/accounts.sqlite3")).resolve()
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        schedule = dict(conn.execute("SELECT * FROM x_post_schedule_config WHERE source_type='material'").fetchone())
        ids = json.loads(schedule["account_ids_json"])
        blockers = read_account_publish_blockers(conn, ids)
        accounts = [dict(row) for row in conn.execute(
            "SELECT id,drama_language,subscription_type,status,publish_approved,deleted_at FROM x_authorized_account")
                    if row["id"] in ids and row["status"] == "active" and row["publish_approved"]
                    and not row["deleted_at"] and (row["id"] not in blockers or
                        blockers[row["id"]]["code"] == "x_post_account_needs_review")]
        # A currently uploading account still contributes its normal daily demand.
        predicate = """FROM x_post_material_pool p WHERE p.status='unpublished'
          AND (p.last_error_code='' OR p.last_error_code IN %s OR p.last_error_code IN %s)
          AND NOT EXISTS(SELECT 1 FROM x_post_queue q WHERE q.pool_item_id=p.id OR q.material_key=p.material_key)
          AND NOT EXISTS(SELECT 1 FROM x_post_manual_material_reservation r
              WHERE r.material_key=p.material_key AND r.state='active')""" % (
                  _NONBLOCKING_MATERIAL_VALIDATION_SQL, _REVALIDATABLE_MATERIAL_VALIDATION_SQL)
        total = conn.execute("SELECT count(*) " + predicate).fetchone()[0]
        pool = [dict(r) for r in conn.execute("SELECT p.id,p.material_id,p.created_at " + predicate +
                " ORDER BY p.created_at DESC,p.id DESC LIMIT ?", (scan_limit,))]
    finally:
        conn.close()
    candidates, rejected = [], Counter()
    if schedule["enabled"] and accounts and pool:
        hosts = env.get("X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS", env.get("X_POST_DAILY_MEDIA_ALLOWED_HOSTS", ""))
        candidates, rejected = metadata_candidates(pool, prefetch(env, pool), now,
                                                   {host.strip().lower() for host in hosts.split(",") if host.strip()})
    report = summarize(schedule, accounts, candidates, now=now, threshold_days=int(config.get("threshold_days", 2)))
    report.update(scanned=len(pool), pool_total=total, scan_limit=scan_limit,
                  excluded_accounts=len(ids) - len(accounts), metadata_rejections=dict(rejected))
    return report

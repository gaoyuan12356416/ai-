"""Indexed web effect reads; full catalogs still own attribution and permissions."""
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone

from .adapters import source_site
from .backend_scope import eligible
from .report import ReportError, campaign_index, selected, validate_metrics

BATCH_SIZE = 100
SINGLE_DAY_BATCH_SIZE = 1000
WINDOW_DAYS = 14
YOUTUBE_RANGE_BATCH_SIZE = 3000
YOUTUBE_RANGE_WINDOW_DAYS = 7
NARROW_CAMPAIGNS = 10
QUERY_BUDGET_SECONDS = 40


def campaign_scope(catalog, actor, params, platform, site):
    # Build the index globally, including collisions and legacy 32-char aliases.
    # Selecting an ID never resolves its ownership; restrict/summarize retain
    # the full catalog and must still reject conflicting identities.
    return tuple(sorted({key[2] for key, candidates in campaign_index(catalog).items()
                         if key[:2] == (platform, site) and any(
                             selected(row, actor, params) and eligible(row, params["end"])
                             and not row.get("excluded") for row in candidates)}))


def windows(start, end, days=WINDOW_DAYS):
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    while current <= last:
        stop = min(last, current + timedelta(days=days - 1))
        yield current.isoformat(), stop.isoformat()
        current = stop + timedelta(days=1)


def metric_sql(start, end, platform, site, campaigns, *, site_scan=False):
    from .routes import metric_sql as unscoped_sql
    batch_size = SINGLE_DAY_BATCH_SIZE if start == end else BATCH_SIZE
    if site_scan:
        if platform != "youtube" or not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < YOUTUBE_RANGE_WINDOW_DAYS:
            raise ReportError("站点效果查询范围无效", 503)
        batch_size = YOUTUBE_RANGE_BATCH_SIZE
    if not campaigns or len(campaigns) > batch_size or any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024
            for value in campaigns):
        raise ReportError("归因 ID 查询范围无效", 503)
    literals = ["0x" + value.encode("utf-8").hex() for value in campaigns]
    # Narrow selections use (campaign_id, dt); broad YouTube history uses
    # contiguous (site_id, dt) ranges, bounded to seven days per statement.
    # The second retains exact-case identity even under a case-insensitive DB
    # collation, and stops adjacent batches from counting the same ID twice.
    indexed = ",".join("CONVERT(%s USING utf8mb4)" % value for value in literals)
    predicate = "AND campaign_id IN (%s) AND BINARY campaign_id IN (%s) " % (
        indexed, ",".join(literals))
    return unscoped_sql(start, end, platform, site).replace(
        "FORCE INDEX(sd)", "FORCE INDEX(%s)" % ("sd" if site_scan else "c_id_dt")).replace(
        "GROUP BY dt,BINARY campaign_id", predicate + "GROUP BY dt,BINARY campaign_id")


def coverage_sql(start, end, site):
    # A covering index-only date probe, independent of selected traffic. No
    # matching campaign rows on an available source date legitimately means 0.
    return """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR)))
      FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
      WHERE site_id='%s' AND dt BETWEEN '%s' AND '%s' GROUP BY dt LIMIT 94""" % (
        site, date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat())


def source(app, start, end, platform, site, campaigns):
    from .routes import MAX_ROWS, TTL, _cache, _lock, query_source_rows
    site = source_site(platform, site)
    if platform not in {"youtube", "x"}:
        raise ReportError("该渠道不支持此效果查询", 503)
    campaigns = tuple(sorted(set(campaigns)))
    digest = hashlib.sha256(json.dumps(campaigns, ensure_ascii=False).encode()).hexdigest()
    key = (start, end, "selected_campaigns_v1", platform, site, digest)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < TTL:
            _cache.move_to_end(key)
            return cached[1], cached[2], cached[3]
    settings = dict(app)
    settings["SOCIAL_ANALYTICS_QUERY_DEADLINE"] = min(
        settings.get("SOCIAL_ANALYTICS_QUERY_DEADLINE", float("inf")),
        time.monotonic() + QUERY_BUDGET_SECONDS)
    # Read coverage first. Failed coverage or any failed batch aborts this source
    # without publishing a partial result or caching a false empty success.
    coverage = query_source_rows(settings, coverage_sql(start, end, site), 93)
    populated = {row.get("date") for row in coverage}
    if len(populated) != len(coverage) or any(
            not isinstance(day, str) or not start <= day <= end for day in populated):
        raise ReportError("源数据日期覆盖不完整，待核实", 503)
    result = []
    # A few exact IDs use a tiny range scan even over 93 days. Avoid repeated
    # SQL-gate/connection overhead for a single-post drill-down; broad candidate
    # sets still use bounded date windows.
    site_scan = platform == "youtube" and start != end and len(campaigns) > BATCH_SIZE
    periods = (list(windows(start, end, YOUTUBE_RANGE_WINDOW_DAYS)) if site_scan else
               [(start, end)] if len(campaigns) <= NARROW_CAMPAIGNS else list(windows(start, end)))
    # A single-day tenant report can contain thousands of historical posts.
    # Reconnecting per 100 IDs exhausts the HTTP deadline before reading them.
    # Broad YouTube selections avoid thousands of scattered campaign ranges
    # and repeated connections. Other sources keep their existing query plan.
    batch_size = SINGLE_DAY_BATCH_SIZE if start == end else BATCH_SIZE
    if site_scan:
        batch_size = YOUTUBE_RANGE_BATCH_SIZE
    for batch_start in range(0, len(campaigns), batch_size):
        batch = campaigns[batch_start:batch_start + batch_size]
        for first, last in periods:
            if not any(first <= day <= last for day in populated):
                continue
            if time.monotonic() >= settings["SOCIAL_ANALYTICS_QUERY_DEADLINE"]:
                raise ReportError("效果查询时间预算已用完；本次不按零统计", 503)
            rows = query_source_rows(settings, metric_sql(
                first, last, platform, site, batch, site_scan=site_scan), MAX_ROWS)
            if any(str(row.get("campaign_id") or "") not in batch for row in rows):
                raise ReportError("效果数据超出所选归因 ID 范围", 503)
            validate_metrics(rows, platform, {day for day in populated if first <= day <= last}, site_id=site)
            result.extend(dict(row, platform=platform, site_id=site) for row in rows)
            if len(result) > MAX_ROWS:
                raise ReportError("数据量超过在线查询上限，请缩小日期范围", 400)
    validate_metrics(result, platform, populated, site_id=site)
    fetched = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cache[key] = (time.monotonic(), result, fetched, populated)
        _cache.move_to_end(key)
        while len(_cache) > 16:
            _cache.popitem(last=False)
    return result, fetched, populated

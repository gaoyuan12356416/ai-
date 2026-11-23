"""AI-backend source boundary; ownership is still resolved by campaign ID."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import time

from .adapters import source_key
from .report import FIELDS, ReportError, add, campaign_index, empty, resolve

SCOPE = "ai_backend_confirmed_publications_and_manual_shortlinks"


def eligible(row, day):
    # 'manual' on Facebook/X means a post, not a standalone short link.
    if row["platform"] == "youtube" and row["link_type"] == "manual":
        return bool(row.get("date")) and row["date"] <= day
    return any(fact["date"] <= day for fact in row.get("publications", []))


def facebook_source(app, start, end, site, allowed_sources=None):
    from .routes import MAX_ROWS, metric_sql, query_source_rows
    # Broad web reads must not put a month of legacy Page/material matches in
    # one statement. Keep the exact triple verification within each disjoint
    # four-day window; fail the whole source if any window fails or overflows.
    if allowed_sources is not None and (date.fromisoformat(end) - date.fromisoformat(start)).days >= 4:
        settings = dict(app)
        settings["SOCIAL_ANALYTICS_QUERY_DEADLINE"] = min(
            settings.get("SOCIAL_ANALYTICS_QUERY_DEADLINE", float("inf")), time.monotonic() + 40)
        workers = min(2, max(1, int(settings.get("SOCIAL_ANALYTICS_RANGE_WORKERS", 1))))
        if workers > 1:
            settings["SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS"] = max(
                12000, int(settings.get("SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS", 8000)))
        periods = []
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        while first <= last:
            stop = min(last, first + timedelta(days=3))
            periods.append((first.isoformat(), stop.isoformat()))
            first = stop + timedelta(days=1)

        def read_window(period):
            if time.monotonic() >= settings["SOCIAL_ANALYTICS_QUERY_DEADLINE"]:
                raise ReportError("效果查询时间预算已用完；本次不按零统计", 503)
            return facebook_source(settings, *period, site, allowed_sources=allowed_sources)

        result, fetched = [], []
        def collect(chunks):
            for rows, timestamp in chunks:
                result.extend(rows)
                fetched.append(timestamp)
                if sum(not row.get("_coverage_only") for row in result) > MAX_ROWS:
                    raise ReportError("数据量超过在线查询上限，请缩小日期范围", 400)
        if workers == 1:
            collect(map(read_window, periods))
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="social-fb-effects") as executor:
                collect(executor.map(read_window, periods))
        return result, min(fetched)
    # Filtering after a campaign-only SUM is too late: legacy FB and AI tasks
    # share short numeric campaign IDs. Retain the frozen URL's source IDs.
    sql = metric_sql(start, end, "facebook", site).replace(
        "'campaign_id',MIN(campaign_id)",
        "'adset_id',MIN(adset_id),'ad_id',MIN(ad_id),'campaign_id',MIN(campaign_id)")
    sql = sql.replace("GROUP BY dt,BINARY campaign_id",
                      "GROUP BY dt,BINARY campaign_id,BINARY adset_id,BINARY ad_id")
    if allowed_sources is not None:
        # Web ranges can contain many unrelated legacy Page/material rows.
        # Reduce the read using globally frozen candidates, never viewer filters;
        # restrict() still resolves ownership and confirms the publication date.
        literal = lambda value: "0x" + str(value).encode("utf-8").hex()
        # A large row-tuple IN causes expensive repeated comparisons on the
        # replica. Read bounded Page candidates, grouped by all three exact IDs,
        # then verify membership with an exact Python tuple set before any row
        # can leave this source boundary. Never SUM by campaign alone.
        allowed_sources = {tuple(str(value) for value in triple) for triple in allowed_sources}
        pages = sorted({triple[1] for triple in allowed_sources})
        predicate = ("BINARY adset_id IN (" + ",".join(literal(page) for page in pages) + ")"
                     if pages else "1=0")
        sql = sql.replace("GROUP BY dt,BINARY campaign_id", "AND " + predicate + " GROUP BY dt,BINARY campaign_id")
        # Source-day existence must survive even if no backend content had any
        # traffic. A zero-valued sentinel is removed by restrict, retaining only
        # coverage. It carries no other system's financial data.
        coverage = """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR),'site_id','%s',
            'campaign_id','','campaign','','adset_id','','ad_id','',
            'clicks',0,'views',0,'installs',0,'conversions',0,
            'revenue_cents',0,'refund_cents',0,'updated_at','','_coverage_only',1))
            FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
            WHERE site_id='%s' AND dt BETWEEN '%s' AND '%s' GROUP BY dt LIMIT 93""" % (site, site, start, end)
        sql = "(" + sql + ") UNION ALL (" + coverage + ")"
    rows = query_source_rows(app, sql, MAX_ROWS + (93 if allowed_sources is not None else 0))
    if sum(not row.get("_coverage_only") for row in rows) > MAX_ROWS:
        raise ReportError("数据量超过在线查询上限，请缩小日期范围", 400)
    seen = set()
    for row in rows:
        key = (row["date"], str(row.get("campaign_id") or ""),
               str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        if not start <= row["date"] <= end or key in seen or any(row.get(k) is None for k in FIELDS):
            raise ReportError("AI 后台来源数据不完整", 503)
        if str(row.get("site_id")) != site:
            raise ReportError("AI 后台来源站点不一致", 503)
        add(empty(), row)
        seen.add(key)
        row.update(platform="facebook", site_id=site)
    if allowed_sources is not None:
        rows = [row for row in rows if row.get("_coverage_only") or
                (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""),
                 str(row.get("ad_id") or "")) in allowed_sources]
    return rows, datetime.now(timezone.utc).isoformat()


def restrict(catalog, metrics, end):
    """Resolve globally first; then apply source/date scope without reassignment."""
    index = campaign_index(catalog)
    resolved = {key: resolve(rows) for key, rows in index.items()}
    selected_catalog = []
    for row in catalog:
        if not eligible(row, end) or row.get("excluded"):
            continue
        key = (row["platform"], row["site_id"], row["key"][0])
        if not resolved.get(key):
            raise ReportError("AI 后台记录的 campaign ID 用户归属冲突", 503)
        if row["platform"] == "facebook" and not row.get("effect_source"):
            raise ReportError("AI 后台发布记录缺少冻结来源 ID", 503)
        selected_catalog.append(row)
    coverage = defaultdict(set)
    accepted, outside = {}, defaultdict(lambda: dict(empty(), rows=0))
    raw_totals = empty()
    for metric in metrics:
        source = source_key(metric["platform"], metric["site_id"])
        coverage[source].add(metric["date"])
        add(raw_totals, metric)
        key = (metric["platform"], metric["site_id"], str(metric.get("campaign_id") or ""))
        row = resolved.get(key)
        reason = None
        if not row:
            reason = "no_unique_backend_campaign"
        elif not any(eligible(candidate, metric["date"]) for candidate in index[key]):
            reason = "not_published_or_link_not_yet_created"
        elif metric["platform"] == "facebook":
            provenance = (str(metric.get("adset_id") or ""), str(metric.get("ad_id") or ""))
            permitted = {(candidate["effect_source"]["adset_id"], candidate["effect_source"]["ad_id"])
                         for candidate in index[key]
                         if candidate.get("effect_source") and eligible(candidate, metric["date"])}
            if provenance not in permitted:
                reason = "outside_backend_frozen_source_ids"
        if reason:
            bucket = outside[(source, reason)]
            add(bucket, metric)
            bucket["rows"] += 1
            continue
        group = (metric["date"],) + key
        if group not in accepted:
            accepted[group] = {**metric, **empty()}
        add(accepted[group], metric)
        accepted[group]["updated_at"] = max(str(accepted[group].get("updated_at") or ""),
                                           str(metric.get("updated_at") or ""))
    kept = empty()
    for row in accepted.values():
        add(kept, row)
    if any(raw_totals[field] != kept[field] + sum(v[field] for v in outside.values()) for field in FIELDS):
        raise ReportError("AI 后台统计范围金额不守恒", 503)
    audit = dict(scope=SCOPE, raw_totals=raw_totals, included_totals=kept,
                 outside=[dict(source=source, reason=reason, **value)
                          for (source, reason), value in sorted(outside.items())])
    return selected_catalog, list(accepted.values()), dict(coverage), audit

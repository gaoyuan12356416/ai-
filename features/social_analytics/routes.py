"""Cookie-authenticated API and reusable collector, with independent channel quality."""
import json
import hashlib
import os
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

from features.youtube_analytics.titles import enrich
from .adapters import PLATFORMS, read_catalogs, source_key, source_site
from .report import (ReportError, csv_export, options, parameters, summarize, validate_metrics,
                     public_collector_metadata)

_cache = OrderedDict()
_lock = threading.Lock()
TTL = 300
MAX_ROWS = 50000


def metric_sql(start, end, platform, site_id=None):
    start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    site = source_site(platform, site_id)
    return """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR),
      'site_id','%s',
      'campaign_id',MIN(campaign_id),'campaign',MIN(campaign),
      'clicks',SUM(COALESCE(clicks,0)),'views',SUM(COALESCE(views,0)),
      'installs',SUM(COALESCE(installs,0)),'conversions',SUM(COALESCE(recharge,0)),
      'revenue_cents',CAST(ROUND(SUM(COALESCE(revenue,0))*100) AS SIGNED),
      'refund_cents',CAST(ROUND(SUM(COALESCE(refund_revenue,0))*100) AS SIGNED),
      'updated_at',CAST(MAX(updated_at) AS CHAR)))
      FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
      WHERE site_id='%s' AND dt BETWEEN '%s' AND '%s'
      GROUP BY dt,BINARY campaign_id LIMIT %d""" % (site, site, start, end, MAX_ROWS + 1)


def query_rows(app, statement, max_rows=1001):
    if (str(app["ADMIN_MAPPING_MYSQL_HOST"]) != "101.32.56.53"
            or str(app["ADMIN_MAPPING_MYSQL_PORT"]) != "63350"):
        raise ReportError("报表只读数据源配置异常", 503)
    env = dict(os.environ)
    env.pop("SQL_GATE_BYPASS", None)
    # The scheduled report opts into a bounded longer query budget; HTTP stays
    # at eight seconds. Keep the FIFO gate and the overall collection deadline.
    statement_ms = min(20000, max(1000, int(app.get("SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS", 8000))))
    statement_seconds = (statement_ms + 999) // 1000
    env.update(MYSQL_PWD=app["ADMIN_MAPPING_MYSQL_PASSWORD"], SQL_GATE_JOB_NAME="social-analytics",
               SQL_GATE_WAIT_TIMEOUT_SECONDS="5", SQL_GATE_EXEC_TIMEOUT_SECONDS=str(statement_seconds + 4))
    statement = ("SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=%d; " % statement_ms +
                 "START TRANSACTION READ ONLY; SELECT @@read_only; " + statement + "; ROLLBACK;")
    timeout = min(statement_seconds + 17, app.get("SOCIAL_ANALYTICS_QUERY_DEADLINE", float("inf")) - time.monotonic())
    if timeout < 1:
        raise ReportError("日报采集时间预算已用完", 503)
    try:
        result = subprocess.run([
            "/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350",
            "-u", str(app["ADMIN_MAPPING_MYSQL_USER"]), "--connect-timeout=3",
            "--default-character-set=utf8mb4", "--skip-reconnect", "-N", "-B", "-r"],
            input=statement, env=env, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        lines = result.stdout.splitlines()
        if result.returncode or not lines or lines.pop(0) != "1":
            if "ERROR 3024 (HY000)" in result.stderr:
                raise ReportError("效果查询超时（MySQL 3024）；本次不按零统计", 503)
            raise ValueError("readonly_query_failed")
        result_rows = [json.loads(bytes.fromhex(line).decode("utf-8")) for line in lines if line.strip()]
        if len(result_rows) > max_rows:
            raise ReportError("数据量超过在线查询上限，请缩小日期范围", 400)
        return result_rows
    except ReportError:
        raise
    except Exception:
        raise ReportError("效果数据读取失败或繁忙，请稍后重试；本次不按零统计", 503) from None


def query_source_rows(app, statement, max_rows):
    # Only the scheduled collector opts into one bounded read-only retry.
    # Keep the FIFO gate and retry only the failing source, never publication.
    attempts = min(2, max(1, int(app.get("SOCIAL_ANALYTICS_QUERY_ATTEMPTS", 1))))
    for attempt in range(attempts):
        try:
            return query_rows(app, statement, max_rows)
        except ReportError as exc:
            if (exc.status != 503 or attempt + 1 == attempts
                    or time.monotonic() + 2 >= app.get("SOCIAL_ANALYTICS_QUERY_DEADLINE", float("inf"))):
                raise
            time.sleep(1)


def source(app, start, end, platform, site_id=None):
    site = source_site(platform, site_id)
    key = (start, end, platform, site)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < TTL:
            _cache.move_to_end(key)
            return cached[1], cached[2]
    result = query_source_rows(app, metric_sql(start, end, platform, site), MAX_ROWS)
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    days = {(start_day + timedelta(days=i)).isoformat() for i in range((end_day - start_day).days + 1)}
    validate_metrics(result, platform, days, site_id=site)
    result = [dict(row, platform=platform, site_id=site) for row in result]
    fetched = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cache[key] = (time.monotonic(), result, fetched)
        _cache.move_to_end(key)
        while len(_cache) > 16:
            _cache.popitem(last=False)
    return result, fetched


def scoped_facebook_source(app, start, end, site, catalog=None):
    """Cache provenance-preserving reads separately from campaign-only reads."""
    from .backend_scope import eligible, facebook_source
    allowed = None if catalog is None else sorted({
        (str(row["key"][0]), row["effect_source"]["adset_id"], row["effect_source"]["ad_id"])
        for row in catalog if row["platform"] == "facebook" and row["site_id"] == site
        and row.get("effect_source") and eligible(row, end) and not row.get("excluded")})
    fingerprint = hashlib.sha256(json.dumps(allowed, separators=(",", ":")).encode()).hexdigest()
    key = (start, end, "facebook_source_ids_v2", source_site("facebook", site), fingerprint)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < TTL:
            _cache.move_to_end(key)
            return cached[1], cached[2]
    result, fetched = facebook_source(app, start, end, site, allowed_sources=allowed)
    with _lock:
        _cache[key] = (time.monotonic(), result, fetched)
        _cache.move_to_end(key)
        while len(_cache) > 16:
            _cache.popitem(last=False)
    return result, fetched


def build_report(app, actor, query, *, include_titles=False, export=False, ai_backend_only=False,
                 catalog_snapshot=None):
    """The sole collector used by API/CSV/daily; app is a settings dictionary."""
    params = parameters(query)
    # Content provenance is independent of permission scope. HTTP keeps the
    # authenticated tenant/owner actor; only scheduled collection is global.
    if catalog_snapshot is not None and actor.get('_system_report') is not True:
        raise ReportError('冻结归属快照仅供内部采集', 400)
    catalog, catalog_errors = (catalog_snapshot if catalog_snapshot is not None
                               else read_catalogs(app, params["platforms"]))
    metrics, errors, fetched = [], {}, []
    query_coverage, scoped_sources = {}, []
    web_scoped = ai_backend_only and actor.get("_system_report") is not True
    if web_scoped:
        app = dict(app)
        app["SOCIAL_ANALYTICS_QUERY_DEADLINE"] = min(
            app.get("SOCIAL_ANALYTICS_QUERY_DEADLINE", float("inf")), time.monotonic() + 40)
    for platform in params["platforms"]:
        for spec in PLATFORMS[platform]["sources"]:
            key = source_key(platform, spec["site_id"])
            try:
                # Retain the single-site call contract used by existing collectors.
                args = () if len(PLATFORMS[platform]["sources"]) == 1 else (spec["site_id"],)
                if ai_backend_only and platform == "facebook":
                    candidates = None if actor.get("_system_report") is True else catalog
                    channel_rows, channel_fetched = scoped_facebook_source(
                        app, params["start"], params["end"], spec["site_id"], candidates)
                elif web_scoped and platform in {"youtube", "x"}:
                    from .scoped_metrics import campaign_scope, source as scoped_source
                    candidates = campaign_scope(catalog, actor, params, platform, spec["site_id"])
                    channel_rows, channel_fetched, populated = scoped_source(
                        app, params["start"], params["end"], platform, spec["site_id"], candidates)
                    query_coverage[key] = populated
                    scoped_sources.append(key)
                else:
                    channel_rows, channel_fetched = source(app, params["start"], params["end"], platform, *args)
                metrics.extend(channel_rows)
                fetched.append(channel_fetched)
            except ReportError as exc:
                # A capacity error needs a narrower request, not a partial export.
                if exc.status == 400:
                    raise
                errors[key] = str(exc)
            except Exception:
                errors[key] = "效果数据读取失败或繁忙，待核实；本次不按零统计"
    coverage, scope_audit = None, None
    if ai_backend_only:
        from .backend_scope import restrict
        catalog, metrics, coverage, scope_audit = restrict(catalog, metrics, params["end"])
        for key, days in query_coverage.items():
            coverage.setdefault(key, set()).update(days)
    report = summarize(catalog, metrics, actor, params,
                       min(fetched) if fetched else datetime.now(timezone.utc).isoformat(),
                       errors=errors, catalog_errors=catalog_errors,
                       source_metadata=getattr(catalog_errors, "source_metadata", {}),
                       metric_coverage=coverage)
    if scope_audit is not None:
        report["meta"]["content_scope"] = scope_audit["scope"]
        report["warnings"][:2] = [
            "仅统计 AI 后台已确认成功发布的内容及 YouTube 手动短链；包含历史内容在所选 UTC 日期产生的效果。发布量为所选日期确认成功的数量。",
            "按冻结 campaign ID 归属人员；Facebook 同时核对冻结 Page 和素材 ID，排除其他系统同号来源及未成功发布记录。",
        ]
        if actor.get("_system_report") is True:
            report["scope_audit"] = scope_audit
    if include_titles:
        enrich(report, lambda statement: query_rows(app, statement), export=export)
    if scoped_sources:
        report["meta"]["indexed_effect_sources"] = scoped_sources
        for key in scoped_sources:
            platform, site = key.split(":", 1)
            quality = report["quality"]["platforms"][platform]
            quality["reconciliation"] = "selected_visible_candidates_only"
            quality["sources"][site]["reconciliation"] = "selected_visible_candidates_only"
    return report


def dispatch(handler, parsed, app, *, legacy=False):
    reply = lambda status, value: app["json_response"](handler, status, value, no_store=True)
    actor = handler._social_analytics_actor()
    if actor is None:
        return
    # Defense in depth: the global collector capability cannot enter via HTTP,
    # even if a future authentication adapter copies session fields.
    actor = {key: actor.get(key) for key in ("tenant_key", "user_id", "role")}
    prefix = "/api/youtube-analytics/" if legacy else "/api/social-analytics/"
    if handler.command != "GET" or parsed.path not in tuple(prefix + suffix for suffix in ("options", "report", "export.csv")):
        handler.close_connection = True
        return reply(404, dict(error="not_found"))
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=720)
        if parsed.path.endswith("/options"):
            if query:
                raise ReportError("筛选选项接口不接受参数")
            catalogs, errors = read_catalogs(app, ["youtube"] if legacy else list(PLATFORMS))
            result = options(catalogs, actor)
            result["quality"] = {"platforms": {}}
            metadata = getattr(errors, "source_metadata", {})
            for platform in (["youtube"] if legacy else PLATFORMS):
                sources = {}
                for spec in PLATFORMS[platform]["sources"]:
                    key = source_key(platform, spec["site_id"])
                    failure = errors.get(key) or errors.get(platform, "")
                    collector = metadata.get(key, {})
                    state = "unavailable" if failure else collector.get("state", "available")
                    if collector.get("freshness") == "stale" and state in {"complete", "available", "ok"}:
                        state = "partial"
                    sources[spec["site_id"]] = dict(spec, source_key=key, state=state, error=failure,
                                                    collector=public_collector_metadata(collector))
                states = [item["state"] for item in sources.values()]
                state = ("available" if all(value in {"complete", "available", "ok"} for value in states)
                         else "unavailable" if all(value == "unavailable" for value in states) else "partial")
                result["quality"]["platforms"][platform] = dict(
                    label=PLATFORMS[platform]["label"], state=state, sources=sources,
                    error="；".join(dict.fromkeys(item["error"] for item in sources.values() if item["error"])))
            if legacy:
                result["options"]["platform"] = [{"value": "youtube", "label": "YouTube"}]
                for row in result["options"]["channel"]:
                    row["value"] = row["value"].removeprefix("youtube:")
            return reply(200, result)
        if legacy:
            if query.get("platform") and set(query["platform"]) != {"youtube"}:
                raise ReportError("旧接口仅支持 YouTube")
            query["platform"] = ["youtube"]
            query.setdefault("group_by", ["owner"])
            query["channel"] = [value if value.startswith("youtube:") else "youtube:" + value
                                for value in query.get("channel", [])]
        export = parsed.path.endswith("/export.csv")
        report = build_report(app, actor, query, include_titles=True, export=export, ai_backend_only=True)
        if legacy:
            for row in report["all_rows"]:
                if "channel" in row:
                    row["channel"] = row["channel"].removeprefix("youtube:")
            for row in report["ranking"]:
                if "channel" in row:
                    row["channel"] = row["channel"].removeprefix("youtube:")
        if export:
            data = csv_export(report)
            handler.send_response(200)
            headers = {
                "Content-Type": "text/csv; charset=utf-8", "Content-Length": str(len(data)),
                "Content-Disposition": 'attachment; filename="social-analytics-%s-%s.csv"' % (
                    report["meta"]["start"], report["meta"]["end"]),
                "Cache-Control": "no-store", "Vary": "Cookie", "X-Content-Type-Options": "nosniff"}
            for key, value in headers.items():
                handler.send_header(key, value)
            handler.end_headers()
            handler.wfile.write(data)
            return
        report.pop("all_rows")
        return reply(200, report)
    except ReportError as exc:
        return reply(exc.status, dict(error="report_unavailable" if exc.status == 503 else "invalid_request",
                                      message=str(exc)))
    except ValueError:
        return reply(400, dict(error="invalid_request", message="请求参数无效"))
    except Exception:
        return reply(503, dict(error="report_unavailable", message="报表暂不可用，请稍后重试；本次不按零统计"))

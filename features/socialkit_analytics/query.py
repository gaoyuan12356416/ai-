"""Read-only, version-pinned SocialKit dashboard queries over immutable snapshots."""
import argparse
import collections
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

DEFAULT_ROOT = "/mnt/data-disk/socialkit-analytics"
COMMON = ("date", "team_id", "product", "platform", "account", "drama")
EFFECT_DIMS = ("country", "device_os", "material_type", "creation_source")
PUBLICATION_DIMS = ("language", "account_country", "reason", "task_status")
DIMENSIONS = {"date": "日期", "team_id": "团队", "product": "产品", "platform": "平台", "account": "账号", "drama": "剧目", "country": "收入国家", "device_os": "设备系统", "material_type": "素材类型", "creation_source": "短链来源", "language": "账号语言", "account_country": "账号国家", "reason": "发布方式", "task_status": "任务状态"}
PLATFORMS = {"facebook": "Facebook", "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "x": "X", "generic": "通用短链", "unattributed": "未归属"}
LABELS = {"material_type": {"ordinary": "普通／无素材", "new": "新剧", "hit": "爆款", "historical_hit": "历史爆款", "old": "老剧", "none": "无素材", "unknown": "未知"}, "creation_source": {"automatic_material": "自动发布素材维度", "ranking_drama": "排行榜剧维度", "browser_api": "社媒浏览器外部 API", "unknown": "历史／未知"}, "task_status": {"1": "已排期", "2": "处理中", "3": "已完成", "4": "已取消", "5": "无内容", "6": "状态 6（待确认）", "7": "状态 7（待确认）"}, "reason": {"ADS_MATERIAL": "广告素材", "DRAMA_CONTINUE": "剧集续发", "PRE_PUBLISH_ADS_MATERIAL": "预发广告素材", "X_SHORT_LINK_EPISODE": "X 短链剧集", "MANUAL": "手动", "X_SHORT_LINK_AD": "X 短链广告", "PRE_PUBLISH": "预发"}}
EFFECT_FIELDS = ("clicks", "views", "installs", "conversions", "revenue_cents", "refund_cents", "dnu_revenue_cents", "dnu_conversions")
PUBLICATION_FIELDS = ("tasks", "planned", "success", "failed")
METRICS = {"revenue": "收入 (USD)", "refunds": "退款 (USD)", "net_revenue": "收入减退款 (USD)", "dnu_revenue": "新用户收入 (USD)", "clicks": "落地页点击", "views": "落地页访问", "installs": "安装", "conversions": "充值人数汇总", "dnu_conversions": "新用户充值人数汇总", "tasks": "任务数", "planned": "计划内容", "success": "成功发布", "failed": "失败字段合计", "unfulfilled": "未计入成功", "success_rate": "计划完成率 (%)", "install_rate": "安装／点击 (%)", "pay_rate": "充值／安装 (%)", "link_count": "有指标短链数"}
_CACHE = collections.OrderedDict()
_OPTIONS_CACHE = collections.OrderedDict()
_LOCK = threading.Lock()


class QueryError(Exception):
    def __init__(self, message, status=400, code="invalid_query"):
        super().__init__(message)
        self.status, self.code = status, code


def _json_file(path, optional=False):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError):
        if optional:
            return {}
        raise QueryError("报表快照尚未准备好，请稍后刷新。", 503, "snapshot_unavailable")


def snapshot(root=DEFAULT_ROOT, version=None):
    root = Path(root).resolve()
    manifest = _json_file(root / "current.json")
    latest = str(manifest.get("version") or "")
    chosen = str(version or latest)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", chosen):
        raise QueryError("报表版本无效。")
    if chosen != latest:
        previous = _json_file(root / "previous.json", optional=True)
        if str(previous.get("version") or "") != chosen:
            raise QueryError("该报表版本已不可用，请刷新读取最新数据。", 409, "snapshot_changed")
        manifest = previous
    raw = manifest.get("database") or manifest.get("db_path") or manifest.get("sqlite_path")
    if not raw:
        raise QueryError("报表快照路径缺失。", 503, "snapshot_unavailable")
    path = (root / str(raw)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise QueryError("报表快照路径异常。", 503, "snapshot_unavailable")
    if not path.is_file():
        raise QueryError("该报表版本已不可用，请刷新读取最新数据。", 409, "snapshot_changed")
    conn = sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro&immutable=1", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        # These connection-local settings never modify the immutable database.
        # Keep grouping temporary data in memory and cap mapped file pages at 128 MiB.
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=134217728")
        stored = conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        if stored:
            recorded = json.loads(stored[0])
            if recorded.get("version") != chosen:
                raise QueryError("报表版本校验失败。", 503, "snapshot_unavailable")
            if chosen != latest:
                manifest = recorded
        elif chosen != latest:
            raise QueryError("该报表版本已不可用，请刷新读取最新数据。", 409, "snapshot_changed")
        manifest = dict(manifest, version=chosen, latest_version=latest)
        status = _json_file(root / "status.json", optional=True)
        return conn, manifest, status
    except Exception:
        conn.close()
        raise


def _values(params, key):
    value = params.get(key, [])
    return [str(v) for v in (value if isinstance(value, (list, tuple)) else [value]) if v is not None]


def _one(params, key, default=None):
    values = _values(params, key)
    if len(values) > 1:
        raise QueryError("参数 %s 只能设置一次。" % key)
    return values[0] if values else default


def _dates(start, end):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def parameters(params, manifest):
    view = _one(params, "view", "overview")
    if view not in ("overview", "effects", "publications"):
        raise QueryError("报表视图无效。")
    range_info = manifest.get("range") or {}
    today = dt.datetime.now(dt.timezone.utc).date()
    end = _one(params, "end", range_info.get("end") or manifest.get("range_end") or today.isoformat())
    start = _one(params, "start")
    try:
        end = dt.date.fromisoformat(end)
        start = dt.date.fromisoformat(start) if start else end - dt.timedelta(days=13)
    except (TypeError, ValueError):
        raise QueryError("日期格式应为 YYYY-MM-DD。")
    if end < start or (end - start).days > 365:
        raise QueryError("请选择不超过 366 天的有效日期范围。")
    allowed = COMMON + (EFFECT_DIMS if view == "effects" else PUBLICATION_DIMS if view == "publications" else ())
    groups = _values(params, "group_by") or ["platform"]
    if len(groups) == 1 and "," in groups[0]:
        groups = groups[0].split(",")
    if len(groups) > 3 or len(set(groups)) != len(groups) or any(g not in allowed for g in groups):
        raise QueryError("请选择当前视图支持的 1 至 3 个不同分组维度。")
    filters = {}
    for key in DIMENSIONS:
        if key == "date":
            continue
        values = _values(params, key)
        if values and key not in allowed:
            raise QueryError("%s 筛选仅适用于对应明细视图。" % DIMENSIONS[key])
        if len(values) > 500 or any(len(v) > 1024 for v in values):
            raise QueryError("筛选条件过多或过长。")
        if values:
            filters[key] = sorted(set(values))
    try:
        page, page_size = int(_one(params, "page", "1")), int(_one(params, "page_size", "50"))
    except ValueError:
        raise QueryError("分页参数无效。")
    if page < 1 or page_size not in (20, 50, 100):
        raise QueryError("分页参数无效。")
    search = _one(params, "search", "").strip()
    if len(search) > 200:
        raise QueryError("搜索词过长。")
    default_metric = "success" if view == "publications" else "revenue"
    sort = _one(params, "sort", default_metric)
    ranking = _one(params, "ranking_metric", default_metric)
    valid_metrics = set(METRICS) - ({"tasks", "planned", "success", "failed", "unfulfilled", "success_rate"} if view == "effects" else set(METRICS) - {"tasks", "planned", "success", "failed", "unfulfilled", "success_rate"} if view == "publications" else set())
    if sort not in valid_metrics | set(groups) or ranking not in valid_metrics:
        raise QueryError("排序或图表指标不适用于当前视图。")
    direction = _one(params, "direction", "desc")
    if direction not in ("asc", "desc"):
        raise QueryError("排序方向无效。")
    known = set(DIMENSIONS) | {"start", "end", "view", "group_by", "version", "page", "page_size", "sort", "direction", "ranking_metric", "search"}
    if set(params) - known:
        raise QueryError("包含不支持的查询参数。")
    return dict(view=view, start=start.isoformat(), end=end.isoformat(), dates=_dates(start, end), groups=groups, filters=filters, page=page, page_size=page_size, search=search, sort=sort, direction=direction, ranking_metric=ranking, allowed=allowed)


def _label(dim, value, record=None):
    if dim == "account" and record:
        return record.get("account_name") or record.get("external_account_id") or "未归属账号"
    if dim == "drama" and record:
        return record.get("drama_name") or record.get("drama_content_id") or "未归属剧目"
    if dim == "platform":
        return PLATFORMS.get(str(value), str(value))
    if dim == "product":
        return {"dramawave": "Dramawave", "hotdrama": "HotDrama"}.get(str(value).lower(), str(value) or "未归属产品")
    return LABELS.get(dim, {}).get(str(value), str(value) if value != "" else "未归属／未提供")


def _query(conn, table, params, groups, context=None):
    context = {} if context is None else context
    fields = EFFECT_FIELDS if table == "effects" else PUBLICATION_FIELDS
    column_cache = context.setdefault("columns", {})
    if table not in column_cache:
        column_cache[table] = {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}
    columns = column_cache[table]
    where, args = ["date >= ?", "date <= ?"], [params["start"], params["end"]]
    for key, values in params["filters"].items():
        where.append(key + " IN (" + ",".join("?" for _ in values) + ")")
        args.extend(values)
    if params["search"]:
        where.append("(instr(lower(drama_name),lower(?))>0 OR instr(lower(drama_content_id),lower(?))>0 OR instr(lower(account_name),lower(?))>0)")
        args.extend([params["search"]] * 3)
    selections = list(groups) + ["COALESCE(SUM(%s),0) AS %s" % (f, f) if f in columns else "0 AS " + f for f in fields]
    label_fields = (("account_name", "external_account_id") if "account" in groups else ()) + (("drama_name", "drama_content_id") if "drama" in groups else ())
    for name in label_fields:
        selections.append("MIN(%s) AS %s" % (name, name))
    if table == "effects":
        selections.append("COUNT(DISTINCT CASE WHEN short_code<>'' THEN short_code END) AS link_count")
        if "unmatched" not in context:
            # Apply exactly the same SQL filters/search once. Usually only a few
            # unmatched rows remain; do not evaluate eight CASE aggregates for
            # every matched fact again in every chart/group query.
            unknown_fields = [f for f in COMMON + EFFECT_DIMS + ("site_id", "short_code") + EFFECT_FIELDS if f in columns]
            unknown_sql = "SELECT " + ",".join(unknown_fields) + " FROM effects WHERE matched=0 AND " + " AND ".join(where)
            context["unmatched"] = [dict(row) for row in conn.execute(unknown_sql, args)]
        unmatched = {}
        for row in context["unmatched"]:
            key = tuple(row[group] for group in groups)
            entry = unmatched.setdefault(key, dict(rows=0, links=set(), metrics={field: 0 for field in EFFECT_FIELDS}))
            entry["rows"] += 1
            entry["links"].add((row["site_id"], row["short_code"]))
            for field in EFFECT_FIELDS:
                entry["metrics"][field] += row.get(field, 0)
    sql = "SELECT " + ",".join(selections) + " FROM " + table + " WHERE " + " AND ".join(where)
    if groups:
        sql += " GROUP BY " + ",".join(groups)
    result = [dict(row) for row in conn.execute(sql, args)]
    if table == "effects":
        for row in result:
            entry = unmatched.get(tuple(row[group] for group in groups))
            row["unmatched_rows"] = entry["rows"] if entry else 0
            row["unmatched_links"] = len(entry["links"]) if entry else 0
            for field in EFFECT_FIELDS:
                row["unmatched_" + field] = entry["metrics"][field] if entry else 0
    return result


def _finish(row, view):
    row = dict(row)
    for f in EFFECT_FIELDS + PUBLICATION_FIELDS + ("link_count", "unmatched_rows", "unmatched_links"):
        row.setdefault(f, 0)
    for target, source in (("revenue", "revenue_cents"), ("refunds", "refund_cents"), ("dnu_revenue", "dnu_revenue_cents")):
        row[target] = round(row[source] / 100, 2)
    row["net_revenue"] = round((row["revenue_cents"] - row["refund_cents"]) / 100, 2)
    row["unfulfilled"] = max(0, row["planned"] - row["success"])
    for target, numerator, denominator in (("install_rate", "installs", "clicks"), ("pay_rate", "conversions", "installs"), ("success_rate", "success", "planned")):
        row[target] = round(row[numerator] / row[denominator] * 100, 2) if row[denominator] else None
    if view == "effects":
        for f in PUBLICATION_FIELDS + ("unfulfilled", "success_rate"):
            row[f] = None
    if view == "publications":
        for f in EFFECT_FIELDS + ("revenue", "refunds", "dnu_revenue", "net_revenue", "install_rate", "pay_rate", "link_count", "unmatched_rows", "unmatched_links"):
            row[f] = None
    return row


def _combined(conn, params, groups, context=None):
    combined = {}
    tables = ("effects", "publications") if params["view"] == "overview" else (params["view"],)
    for table in tables:
        for row in _query(conn, table, params, groups, context):
            key = tuple(str(row.get(g, "")) for g in groups)
            dest = combined.setdefault(key, {g: row.get(g, "") for g in groups})
            for f, value in row.items():
                if f in groups:
                    continue
                if isinstance(value, (int, float)):
                    dest[f] = dest.get(f, 0) + value
                elif value and not dest.get(f):
                    dest[f] = value
    result = []
    for row in combined.values():
        row = _finish(row, params["view"])
        row["labels"] = {g: _label(g, row[g], row) for g in groups}
        result.append(row)
    return result


def _day_status(manifest, table, day):
    coverage = manifest.get("coverage", {}).get(table, {})
    if day in coverage:
        return coverage[day]
    entry = (manifest.get("date_manifest") or {}).get(day)
    if entry is not None:
        return entry.get("status", "complete") if isinstance(entry, dict) else "complete"
    return "missing"


def _stale_status(status):
    state = str(status.get("status") or status.get("refresh_status") or "").lower()
    return state in ("failed", "error", "partial") or bool(status.get("error"))


def _precomputed(root, version, kind):
    value = _json_file(Path(root) / "precomputed" / (version + "-" + kind + ".json"), optional=True)
    if value.get("version") != version or not isinstance(value.get("payload"), dict):
        return None
    return value["payload"]


def _refresh_report_state(response, manifest, status):
    response["meta"]["refresh_status"] = status
    response["meta"]["stale"] = _stale_status(status)
    response["meta"]["latest_version"] = manifest["latest_version"]
    return response


def report(root=DEFAULT_ROOT, params=None, _allow_precomputed=True):
    params = params or {}
    conn, manifest, status = snapshot(root, _one(params, "version"))
    try:
        parsed = parameters(params, manifest)
        if _allow_precomputed and parsed == parameters({}, manifest):
            warmed = _precomputed(root, manifest["version"], "overview")
            if (warmed and warmed.get("meta", {}).get("version") == manifest["version"]
                    and warmed.get("parameters") == json.loads(json.dumps(parsed))):
                return _refresh_report_state(warmed, manifest, status)
        cachekey = (str(Path(root).resolve()), manifest["version"], json.dumps(parsed, sort_keys=True))
        with _LOCK:
            saved = _CACHE.get(cachekey)
            if saved and time.monotonic() - saved[0] < 45:
                response = json.loads(saved[1])
                return _refresh_report_state(response, manifest, status)
        aggregates, query_context = {}, {}
        def aggregate(groups):
            key = tuple(groups)
            if key not in aggregates:
                aggregates[key] = _combined(conn, parsed, groups, query_context)
            # Trend source-status decoration and sorting must not mutate another
            # consumer when its group dimensions are the same.
            return [dict(row) for row in aggregates[key]]
        totals = (aggregate([]) or [_finish({}, parsed["view"])])[0]
        all_rows = aggregate(parsed["groups"])
        def sort_value(row):
            value = row.get("labels", {}).get(parsed["sort"], row.get(parsed["sort"]))
            return (value is not None, value if value is not None else 0, str(row.get("labels", {})))
        all_rows.sort(key=sort_value, reverse=parsed["direction"] == "desc")
        ranking = sorted(all_rows, key=lambda r: r.get(parsed["ranking_metric"]) or 0, reverse=True)[:10]
        trendmap = {r["date"]: r for r in aggregate(["date"])}
        trend = []
        for day in parsed["dates"]:
            row = trendmap.get(day, dict(_finish({}, parsed["view"]), date=day, labels={"date": day}))
            row["source_status"] = {t: _day_status(manifest, t, day) for t in ("effects", "publications")}
            if row["source_status"]["effects"] == "missing":
                for f in EFFECT_FIELDS + ("revenue", "refunds", "dnu_revenue", "net_revenue", "install_rate", "pay_rate", "link_count"):
                    row[f] = None
            if row["source_status"]["publications"] == "missing":
                for f in PUBLICATION_FIELDS + ("unfulfilled", "success_rate"):
                    row[f] = None
            trend.append(row)
        tables = ("effects", "publications") if parsed["view"] == "overview" else (parsed["view"],)
        missing = {t: [d for d in parsed["dates"] if _day_status(manifest, t, d) == "missing"] for t in tables}
        for t in tables:
            if len(missing[t]) == len(parsed["dates"]):
                for f in (EFFECT_FIELDS + ("revenue", "refunds", "dnu_revenue", "net_revenue", "install_rate", "pay_rate", "link_count") if t == "effects" else PUBLICATION_FIELDS + ("unfulfilled", "success_rate")):
                    totals[f] = None
        pages = max(1, math.ceil(len(all_rows) / parsed["page_size"]))
        page = min(parsed["page"], pages)
        response = dict(meta=dict(version=manifest["version"], latest_version=manifest["latest_version"], generated_at=manifest.get("generated_at") or manifest.get("refreshed_at"), source_updated_at=manifest.get("source_updated_at"), view=parsed["view"], start=parsed["start"], end=parsed["end"], effect_timezone="UTC", publication_timezone="account_local", currency="USD", refresh_status=status, stale=_stale_status(status), missing_days=missing, partial=any(missing.values()), range=manifest.get("range"), quality=manifest.get("quality", {})), group_by=parsed["groups"], totals=totals, trend=trend, ranking=ranking, breakdowns={dim: sorted(aggregate([dim]), key=lambda r:r.get("success" if parsed["view"] == "publications" else "revenue") or 0, reverse=True) for dim in ("platform", "product")}, rows=all_rows[(page-1)*parsed["page_size"]:page*parsed["page_size"]], all_rows=all_rows, pagination=dict(page=page, page_size=parsed["page_size"], pages=pages, total=len(all_rows)), parameters=parsed, metric_labels=METRICS, dimension_labels=DIMENSIONS)
        serialized = json.dumps(response, ensure_ascii=False)
        if len(serialized) <= 2_000_000:
            with _LOCK:
                _CACHE[cachekey] = (time.monotonic(), serialized)
                _CACHE.move_to_end(cachekey)
                while len(_CACHE) > 8:
                    _CACHE.popitem(last=False)
        return response
    finally:
        conn.close()


def options(root=DEFAULT_ROOT, version=None, _allow_precomputed=True):
    conn, manifest, status = snapshot(root, version)
    try:
        if _allow_precomputed:
            warmed = _precomputed(root, manifest["version"], "options")
            if warmed and warmed.get("version") == manifest["version"] and isinstance(warmed.get("options"), dict):
                warmed["refresh_status"] = status
                return warmed
        cachekey = (str(Path(root).resolve()), manifest["version"])
        with _LOCK:
            saved = _OPTIONS_CACHE.get(cachekey)
        if saved:
            result = json.loads(saved)
            result["refresh_status"] = status
            return result
        result = {}
        for dim in COMMON[1:] + EFFECT_DIMS + PUBLICATION_DIMS:
            tables = ("effects", "publications") if dim in COMMON else ("effects",) if dim in EFFECT_DIMS else ("publications",)
            values = {}
            for table in tables:
                extras = ",MIN(account_name) account_name,MIN(external_account_id) external_account_id" if dim == "account" else ",MIN(drama_name) drama_name,MIN(drama_content_id) drama_content_id" if dim == "drama" else ""
                for row in conn.execute("SELECT " + dim + extras + " FROM " + table + " GROUP BY " + dim):
                    row = dict(row)
                    key = str(row[dim])
                    detail = row.get("external_account_id") if dim == "account" else row.get("drama_content_id") if dim == "drama" else ""
                    values.setdefault(key, dict(value=key, label=_label(dim, row[dim], row), detail=detail or ""))
            result[dim] = sorted(values.values(), key=lambda v:v["label"])
        response = dict(version=manifest["version"], generated_at=manifest.get("generated_at") or manifest.get("refreshed_at"), range=manifest.get("range") or dict(start=manifest.get("range_start"), end=manifest.get("range_end")), options=result, dimensions=DIMENSIONS, metrics=METRICS, refresh_status=status, allowed_dimensions={"overview": list(COMMON), "effects": list(COMMON+EFFECT_DIMS), "publications": list(COMMON+PUBLICATION_DIMS)})
        serialized = json.dumps(response, ensure_ascii=False)
        if len(serialized) <= 4_000_000:
            with _LOCK:
                _OPTIONS_CACHE[cachekey] = serialized
                _OPTIONS_CACHE.move_to_end(cachekey)
                while len(_OPTIONS_CACHE) > 2:
                    _OPTIONS_CACHE.popitem(last=False)
        return response
    finally:
        conn.close()


def warm_snapshot(root=DEFAULT_ROOT, version=None):
    """Warm only a published current/previous version; errors never change manifests."""
    params = {"version": version} if version else {}
    overview = report(root, params, _allow_precomputed=False)
    chosen = overview["meta"]["version"]
    selectors = options(root, chosen, _allow_precomputed=False)
    folder = Path(root) / "precomputed"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for kind, payload in (("overview", overview), ("options", selectors)):
        path = folder / (chosen + "-" + kind + ".json")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(folder), prefix=chosen+"-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": chosen, "payload": payload}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(path))
            temporary = None
            files.append(str(path))
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    return {"version": chosen, "files": files}


def export_csv(result):
    groups = result["group_by"]
    view = result["meta"]["view"]
    metric_fields = [k for k in METRICS if not (view == "effects" and k in ("tasks", "planned", "success", "failed", "unfulfilled", "success_rate")) and not (view == "publications" and k not in ("tasks", "planned", "success", "failed", "unfulfilled", "success_rate"))]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow([DIMENSIONS[k] for k in groups] + [METRICS[k] for k in metric_fields] + ["快照版本", "收入日期口径", "发布日期口径"])
    def safe(value):
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value
    for row in result["all_rows"]:
        writer.writerow([safe(row["labels"].get(k, row.get(k, ""))) for k in groups] + ["" if row.get(k) is None else row[k] for k in metric_fields] + [result["meta"]["version"], "UTC", "账号当地日期"])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=DEFAULT_ROOT)
    for key in ("start", "end", "version"):
        parser.add_argument("--" + key)
    parser.add_argument("--view", choices=("overview", "effects", "publications"), default="overview")
    parser.add_argument("--group-by", default="platform")
    parser.add_argument("--filter", action="append", default=[])
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--page", default="1")
    parser.add_argument("--page-size", default="50")
    args = parser.parse_args()
    params = {k: v for k, v in vars(args).items() if k in ("start", "end", "version", "view", "group_by", "page", "page_size") and v is not None}
    for item in args.filter:
        if "=" not in item:
            parser.error("--filter requires key=value")
        key, value = item.split("=", 1)
        params.setdefault(key, [])
        if not isinstance(params[key], list):
            parser.error("--filter only accepts dimension keys")
        params[key].append(value)
    try:
        result = report(args.data_root, params)
        if args.format == "csv":
            import sys
            sys.stdout.buffer.write(export_csv(result))
        else:
            print(json.dumps(result, ensure_ascii=False))
    except QueryError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

"""Campaign-ID-only attribution and aggregation shared by every report surface."""
import csv
import io
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from features.youtube_analytics.report import ReportError, add as legacy_add
from features.youtube_analytics.titles import display_title
from .adapters import PLATFORMS

DIMENSIONS = ("date", "owner", "platform", "drama", "channel", "link_type", "language")
FIELDS = ("clicks", "views", "installs", "conversions", "revenue_cents", "refund_cents")
COUNTS = ("links", "published_count", "post_count", "repost_count")
METRICS = ("clicks", "views", "installs", "conversions", "revenue", "refunds",
           "install_rate", "pay_rate") + COUNTS
LABELS = dict(date="统计日期（UTC）", owner="人员", platform="渠道", drama="剧名", channel="账号/频道",
              link_type="链接类型", language="语言", clicks="落地页点击", views="落地页访问",
              installs="激活（安装）数", conversions="付费转化数（充值人数）", revenue="收入（USD）",
              refunds="退款（USD）", install_rate="点击安装率（%）", pay_rate="安装付费率（%）",
              links="生成链接数", published_count="成功发布数", post_count="X Post 数", repost_count="X Repost 数")
IDENTITY = ("platform", "site_id", "tenant", "owner", "drama", "channel", "language",
            "link_type", "excluded", "sub_user_id")


def parameters(query):
    allowed = set(DIMENSIONS[1:]) | {
        "start", "end", "group_by", "search", "sort", "direction", "page", "page_size", "ranking_metric"}
    if set(query) - allowed:
        raise ReportError("存在不支持的筛选参数")

    def one(key, default):
        values = query.get(key, [default])
        if len(values) != 1:
            raise ReportError("重复的参数：" + key)
        return values[0]

    try:
        today = datetime.now(timezone.utc).date()
        end = date.fromisoformat(one("end", (today - timedelta(days=1)).isoformat()))
        start = date.fromisoformat(one("start", (end - timedelta(days=6)).isoformat()))
        if start > end or (end - start).days >= 93 or end > today or start.year < 2020:
            raise ValueError()
        page, size = int(one("page", "1")), int(one("page_size", "50"))
        if not 1 <= page <= 10000 or size not in (20, 50, 100):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReportError("日期范围须为 1–93 天且不晚于今天，分页参数须有效") from None
    groups = one("group_by", "owner,platform").split(",")
    if not 1 <= len(groups) <= 3 or len(set(groups)) != len(groups) or set(groups) - set(DIMENSIONS):
        raise ReportError("请选择 1–3 个不同的分组维度")
    filters = {}
    for key in DIMENSIONS[1:]:
        values = query.get(key, [])
        if len(values) > 100 or any(not value or len(value) > 200 or any(ord(c) < 32 for c in value)
                                    for value in values):
            raise ReportError("筛选内容过长或无效")
        filters[key] = set(values)
    if filters["platform"] - set(PLATFORMS):
        raise ReportError("渠道尚未接入")
    if filters["link_type"] - {"auto", "manual", "auto_template", "system"}:
        raise ReportError("链接类型无效")
    search = one("search", "").strip()
    if len(search) > 200 or any(ord(c) < 32 for c in search):
        raise ReportError("搜索内容无效")
    sort, direction, ranking = one("sort", "revenue"), one("direction", "desc"), one("ranking_metric", "revenue")
    if sort not in METRICS + tuple(groups) or direction not in ("asc", "desc") or ranking not in METRICS:
        raise ReportError("排序参数无效")
    return dict(start=start.isoformat(), end=end.isoformat(), groups=groups, filters=filters,
                platforms=[p for p in PLATFORMS if not filters["platform"] or p in filters["platform"]],
                search=search.casefold(), sort=sort, direction=direction,
                page=page, page_size=size, ranking_metric=ranking)


def visible(row, actor):
    if actor.get("_system_report") is True:
        return True
    return (bool(actor.get("tenant_key")) and bool(actor.get("user_id"))
            and row.get("tenant") == actor["tenant_key"]
            and (actor.get("role") == "admin" or row.get("owner") == actor["user_id"]))


def campaign_index(catalog):
    result = defaultdict(list)
    for row in catalog:
        campaign_id = row["key"][0]
        if not campaign_id:
            continue
        key = (row["platform"], row["site_id"], campaign_id)
        result[key].append(row)
        alias_length = PLATFORMS[row["platform"]]["storage_alias_length"]
        if alias_length and len(campaign_id) > alias_length:
            result[(key[0], key[1], campaign_id[:alias_length])].append(row)
    return result


def identity(row):
    return tuple(row.get(key) for key in IDENTITY)


def resolve(candidates):
    if not candidates or len({row["key"][0] for row in candidates}) != 1:
        return None
    if len({identity(row) for row in candidates}) != 1:
        return None
    row = candidates[0]
    return row if row.get("tenant") and row.get("owner") and not row.get("excluded") else None


def options(catalog, actor):
    by_id = campaign_index(catalog)
    values = {dimension: {} for dimension in DIMENSIONS[1:]}
    for candidates in by_id.values():
        row = resolve(candidates)
        if row and visible(row, actor):
            for dimension in values:
                values[dimension][row[dimension]] = row.get(dimension + "_label", row[dimension])
    # A selected channel may have no campaigns yet. It still needs a visible
    # unavailable state and must never disappear into a misleading total.
    values["platform"] = {key: value["label"] for key, value in PLATFORMS.items()}
    return dict(options={
        dimension: [dict(value=key, label=value) for key, value in sorted(items.items(), key=lambda x: (x[1], x[0]))]
        for dimension, items in values.items()}, scope=scope(actor))


def scope(actor):
    return "system" if actor.get("_system_report") is True else "tenant" if actor.get("role") == "admin" else "own"


def empty():
    return dict.fromkeys(FIELDS + COUNTS, 0)


def add(target, source):
    legacy_add(target, source)
    # Use the same strict integer validation for publication counts.
    for key in COUNTS[1:]:
        check = dict.fromkeys(FIELDS + ("links",), 0)
        legacy_add(check, {"links": source.get(key, 0)})
        target[key] += check["links"]


def dto(value, available=True, publications_available=True):
    result = dict(value)
    result["revenue"] = round(result["revenue_cents"] / 100, 2)
    result["refunds"] = round(result["refund_cents"] / 100, 2)
    result["install_rate"] = round(100 * result["installs"] / result["clicks"], 2) if result["clicks"] else None
    result["pay_rate"] = round(100 * result["conversions"] / result["installs"], 2) if result["installs"] else None
    if not available:
        result.update({key: None for key in FIELDS + METRICS if key not in COUNTS})
    if not publications_available:
        result.update({key: None for key in COUNTS})
    return result


def validate_metrics(metrics, platform, days):
    seen = set()
    for metric in metrics:
        key = (metric["date"], str(metric.get("campaign_id") or ""))
        if metric["date"] not in days or key in seen or any(metric.get(field) is None for field in FIELDS):
            raise ReportError("源数据日期、唯一性或指标不完整，本次不按零统计", 503)
        if metric.get("platform", platform) != platform:
            raise ReportError("源数据渠道不一致，待核实", 503)
        add(empty(), metric)
        seen.add(key)


def summarize(catalog, metrics, actor, params, fetched_at, errors=None, catalog_errors=None):
    """Resolve globally before filtering; never join names or af_channel.

    _system_report is an internal collector capability. HTTP handlers must
    construct actors themselves and never deserialize that property.
    """
    errors, catalog_errors = errors or {}, catalog_errors or {}
    start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    platforms = params["platforms"]
    by_id = campaign_index(catalog)
    resolved = {key: resolve(candidates) for key, candidates in by_id.items()}
    populated = {platform: set() for platform in platforms}
    platform_values = {platform: empty() for platform in platforms}
    channel_quality = {platform: dict(source=empty(), attributed=empty(), unmatched=empty(), excluded=empty(),
                                     unmatched_keys=set(), excluded_keys=set(), corrected_keys=set())
                       for platform in platforms}
    totals, groups, trend = empty(), {}, {day: empty() for day in days}
    source_updated = ""

    def selected(row):
        return visible(row, actor) and row["platform"] in platforms and all(
            not values or row.get(key) in values for key, values in params["filters"].items()) and (
            not params["search"] or params["search"] in
            (str(row.get("drama_label") or "") + " " + row["drama"]).casefold())

    def emit(row, value):
        key = tuple(row[dimension] for dimension in params["groups"])
        if actor.get("_system_report") is True and "owner" in params["groups"]:
            key = (row["tenant"],) + key
        if key not in groups:
            groups[key] = dict(empty(), **{
                name: row[name] for dimension in params["groups"]
                for name in (dimension, dimension + "_label") if name in row})
            if "owner" in params["groups"]:
                groups[key]["owner_tenant"] = row["tenant"]
        add(groups[key], value)
        add(totals, value)
        add(trend[row["date"]], value)
        add(platform_values[row["platform"]], value)

    for metric in metrics:
        platform, day = metric["platform"], metric["date"]
        if platform not in platforms or platform in errors:
            continue
        add(empty(), metric)
        populated[platform].add(day)
        source_updated = max(source_updated, str(metric.get("updated_at") or ""))
        key = (platform, PLATFORMS[platform]["site_id"], str(metric.get("campaign_id") or ""))
        candidates, row = by_id.get(key, []), resolved.get(key)
        quality = channel_quality[platform]
        # Unknown campaigns cannot be safely attributed to a tenant. Only the
        # trusted scheduled collector may see the full-source reconciliation.
        quality_visible = actor.get("_system_report") is True or (
            actor.get("role") == "admin" and bool(candidates)
            and all(candidate.get("tenant") == actor.get("tenant_key") for candidate in candidates))
        if quality_visible:
            add(quality["source"], metric)
        if row:
            if quality_visible:
                add(quality["attributed"], metric)
            if selected(row):
                emit(dict(row, date=day), metric)
                if key[2] != row["key"][0]:
                    quality["corrected_keys"].add(key[2])
        elif quality_visible:
            excluded = bool(candidates) and all(candidate.get("excluded") for candidate in candidates)
            bucket = "excluded" if excluded else "unmatched"
            add(quality[bucket], metric)
            quality[bucket + "_keys"].add(key[2])

    counted_links, publications = set(), defaultdict(list)
    for row in catalog:
        key = (row["platform"], row["site_id"], row["key"][0])
        if row["platform"] not in platforms:
            continue
        for fact in row.get("publications", []):
            publications[fact["id"]].append((row, fact))
        if not resolved.get(key):
            continue
        if selected(row) and row["link_id"] not in counted_links and row["date"] in trend:
            counted_links.add(row["link_id"])
            emit(row, {"links": 1})
        if selected(row) and "date" not in params["groups"]:
            emit(dict(row, date=days[0]), {})
    publication_conflicts = 0
    publication_conflicts_by_platform = defaultdict(int)
    for candidates in publications.values():
        if (len({(identity(row), fact["date"], fact["kind"]) for row, fact in candidates}) != 1
                or any(not resolved.get((row["platform"], row["site_id"], row["key"][0]))
                       for row, _ in candidates)):
            can_report = actor.get("_system_report") is True or (
                actor.get("role") == "admin" and all(row["tenant"] == actor.get("tenant_key") for row, _ in candidates))
            if can_report and not all(row.get("excluded") for row, _ in candidates):
                publication_conflicts += 1
                for platform in {row["platform"] for row, _ in candidates}:
                    publication_conflicts_by_platform[platform] += 1
            continue
        row, fact = candidates[0]
        if selected(row) and fact["date"] in trend:
            emit(dict(row, date=fact["date"]), {
                "published_count": 1, "post_count": int(fact["kind"] == "post"),
                "repost_count": int(fact["kind"] == "repost")})

    def availability(included, day=None):
        needed = {day} if day else set(days)
        available = any(populated[platform] & needed and platform not in catalog_errors for platform in included)
        complete = all(needed <= populated[platform] and platform not in errors
                       and platform not in catalog_errors and not publication_conflicts_by_platform[platform]
                       for platform in included)
        publications_available = all(platform not in catalog_errors for platform in included)
        return available, complete, publications_available

    def formatted(value, included, day=None):
        available, complete, publication_available = availability(included, day)
        return dict(dto(value, available, publication_available),
                    data_status="complete" if complete else "partial" if available else "unavailable")

    output_rows = [formatted(row, [row["platform"]] if "platform" in row else platforms, row.get("date"))
                   for row in groups.values()]

    def sort_key(row, field):
        value = row.get(field + "_label", row.get(field))
        return value if value is not None else -math.inf

    output_rows.sort(key=lambda row: tuple(str(row.get(key, "")) for key in params["groups"]))
    output_rows.sort(key=lambda row: sort_key(row, params["sort"]), reverse=params["direction"] == "desc")
    ranking = sorted(output_rows, key=lambda row: sort_key(row, params["ranking_metric"]), reverse=True)[:10]
    ranking = [dict(row, label=" / ".join(str(row.get(key + "_label", row[key])) for key in params["groups"]))
               for row in ranking]
    platform_quality, unmatched_total, excluded_total = {}, empty(), empty()
    for platform, quality in channel_quality.items():
        available, complete, publication_available = availability([platform])
        add(unmatched_total, quality["unmatched"])
        add(excluded_total, quality["excluded"])
        # This equality is integer-exact and cannot be broken by report filters.
        if any(quality["source"][field] != quality["attributed"][field] + quality["unmatched"][field]
               + quality["excluded"][field] for field in FIELDS):
            raise ReportError("归因金额或数量不守恒，待核实", 503)
        platform_quality[platform] = dict(
            label=PLATFORMS[platform]["label"], state="complete" if complete else "partial" if available else "unavailable",
            missing_dates=[day for day in days if day not in populated[platform]],
            error=errors.get(platform) or catalog_errors.get(platform) or "",
            unmatched_campaigns=len(quality["unmatched_keys"]), excluded_campaigns=len(quality["excluded_keys"]),
            corrected_campaigns=len(quality["corrected_keys"]),
            source_totals=dto(quality["source"], available),
            attributed_totals=dto(quality["attributed"], available),
            unmatched_totals=dto(quality["unmatched"], available),
            excluded_totals=dto(quality["excluded"], available),
            reconciliation="complete" if actor.get("_system_report") is True else "visible_candidates_only",
            publications_available=publication_available,
            publication_conflicts=publication_conflicts_by_platform[platform],
            publication_status="pending" if publication_conflicts_by_platform[platform] else
                               "complete" if publication_available else "unavailable")
    available, complete, _ = availability(platforms)
    missing_dates = [day for day in days if any(day not in populated[p] for p in platforms)]
    warnings = [
        "效果按 UTC 自然日统计，覆盖全部历史链接；发布量按确认完成的 UTC 日期去重。",
        "按渠道站点内 campaign ID 唯一关联发布表或链接账本；名称与 af_channel 不参与效果归因。",
        "转化数采用源表充值人数；跨日期累加每日人数，未做跨日用户去重。",
        "手动短链仅计效果及生成链接数；X 中转源 Post 不重复计入目标账号发布量。",
    ]
    if not complete:
        warnings.append("存在未同步日期或不可用渠道；合计仅含可用部分，不代表完整数据，不能将缺失值判为零。")
    if any(q["unmatched_campaigns"] for q in platform_quality.values()):
        warnings.append("无法唯一归属的数据已单列待归属，不计入个人业绩。")
    if publication_conflicts:
        warnings.append("%s 个已确认发布对象的归属存在冲突或缺失，未计入个人发布量，发布数待核实。" % publication_conflicts)
    if params["end"] == datetime.now(timezone.utc).date().isoformat():
        warnings.append("今天的数据尚未完整，后续回传会更新。")
    total_rows = len(output_rows)
    pages = max(1, math.ceil(total_rows / params["page_size"]))
    page = min(params["page"], pages)
    offset = (page - 1) * params["page_size"]
    return dict(
        totals=formatted(totals, platforms), rows=output_rows[offset:offset + params["page_size"]],
        all_rows=output_rows, group_by=params["groups"], ranking=ranking,
        platform_totals={p: dict(formatted(v, [p]), platform=p, platform_label=PLATFORMS[p]["label"])
                         for p, v in platform_values.items()},
        trend=[dict(formatted(trend[day], platforms, day), date=day) for day in days],
        pagination=dict(page=page, page_size=params["page_size"], total=total_rows, pages=pages),
        quality=dict(platforms=platform_quality, complete=complete, missing_dates=missing_dates,
                     unmatched_campaigns=sum(q["unmatched_campaigns"] for q in platform_quality.values()),
                     unmatched_totals=dto(unmatched_total, available),
                     excluded_campaigns=sum(q["excluded_campaigns"] for q in platform_quality.values()),
                     excluded_totals=dto(excluded_total, available),
                     corrected_campaigns=sum(q["corrected_campaigns"] for q in platform_quality.values()),
                     publication_conflicts=publication_conflicts),
        meta=dict(start=params["start"], end=params["end"], timezone="UTC", currency="USD",
                  platforms=platforms, fetched_at=fetched_at, source_updated_at=source_updated,
                  scope=scope(actor), conversion_label=LABELS["conversions"],
                  attribution="site_campaign_id_frozen_ledger_v3"), warnings=warnings)


def csv_export(report):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    fields = []
    for dimension in report["group_by"]:
        if dimension != "date":
            fields.append((dimension, dimension + " ID"))
        fields.append((dimension if dimension == "date" else dimension + "_label", LABELS[dimension]))
        if dimension == "drama":
            fields.append(("drama_english_display", "英文剧名"))
    fields += [(metric, LABELS[metric]) for metric in METRICS]
    fields += [("period", "统计范围（UTC）"), ("data_status_label", "数据完整性"), ("conversion_note", "转化口径")]

    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return "'" + value
        return "未同步" if value is None else value

    writer.writerow([label for _, label in fields])
    for row in report["all_rows"]:
        value = dict(row, drama_english_display=display_title(row),
                     period=report["meta"]["start"] + " 至 " + report["meta"]["end"],
                     data_status_label={"complete": "已有源数据", "partial": "不完整，仅含可用部分",
                                        "unavailable": "未同步／待核实"}[row["data_status"]],
                     conversion_note="源表充值人数，每日人数累加，未跨日去重")
        writer.writerow([safe(value.get(key)) for key, _ in fields])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")

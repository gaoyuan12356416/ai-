"""Bounded, read-only publication catalogs. Never import a publisher or create a DB."""
import json
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from features.youtube_analytics.report import ReportError


# Future channels must provide a separate catalog adapter and a verified site.
# Their presence here never means that their publishers are enabled.
PLATFORMS = {
    "youtube": {"label": "YouTube", "site_id": "2284", "storage_alias_length": 32,
                "sources": [{"site_id": "2284", "label": "YouTube"}]},
    "x": {"label": "X", "site_id": "2116", "storage_alias_length": None,
          "sources": [{"site_id": "2116", "label": "X"}]},
    "facebook": {"label": "Facebook", "storage_alias_length": None,
                 "sources": [{"site_id": "2049", "label": "旧自动发布"},
                             {"site_id": "2310", "label": "手动发布"}]},
}
MAX_CATALOG_ROWS = 100000


def source_site(platform, site_id=None):
    """Never infer a site for a platform with multiple publication ledgers."""
    if platform not in PLATFORMS:
        raise ReportError("渠道尚未接入")
    site = str(site_id or PLATFORMS[platform].get("site_id") or "")
    if site not in {item["site_id"] for item in PLATFORMS[platform]["sources"]}:
        raise ReportError("渠道站点不匹配，待核实", 503)
    return site


def source_key(platform, site_id=None):
    return platform + ":" + source_site(platform, site_id)


def platform_metadata():
    return {platform: {"label": value["label"],
                       "sources": [dict(item) for item in value["sources"]]}
            for platform, value in PLATFORMS.items()}


class CatalogErrors(dict):
    """Preserve the two-value catalog API while carrying collector health."""
    def __init__(self):
        super().__init__()
        self.source_metadata = {}


@contextmanager
def snapshot(path):
    deadline = time.monotonic() + 15
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def rows(connection, statement):
    result = [dict(row) for row in connection.execute(statement + " LIMIT 100001")]
    if len(result) > MAX_CATALOG_ROWS:
        raise ReportError("归因记录超过在线报表容量，请联系管理员", 503)
    return result


def utc_day(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError):
        return ""


def link_key(url, site):
    parsed = urlsplit(str(url or ""))
    if (parsed.scheme != "https" or parsed.hostname != "www.dramawavew2a.com"
            or parsed.path != "/ads/101/%s/view" % site):
        return None
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
    except ValueError:
        return None
    # Campaign names remain evidence only. They never determine ownership.
    if len(query.get("af_c_id", [])) != 1 or not query["af_c_id"][0]:
        return None
    return query["af_c_id"][0], query.get("c", [""])[0]


def normalized(platform, row):
    result = dict(row)
    site = source_site(platform, row.get("site_id"))
    result.update(platform=platform, platform_label=PLATFORMS[platform]["label"],
                  site_id=site)
    result["channel"] = platform + ":" + str(row.get("channel") or "")
    prefix = platform + ":" + (site + ":" if len(PLATFORMS[platform]["sources"]) > 1 else "")
    result["link_id"] = prefix + str(row["link_id"])
    result.setdefault("publications", [])
    return result


def read_youtube(path):
    """Use the existing frozen YT identity rules, in one publication/link snapshot."""
    with snapshot(path) as connection:
        users = rows(connection, "SELECT user_id,tenant_key,name FROM drama_admin_user")
        links = rows(connection, "SELECT id,job_id,material_kind,content_id,long_url,"
                     "created_at_utc FROM drama_material_short_link WHERE publish_state='published'")
        automatic = rows(connection, "SELECT task_id,link_id,context FROM youtube_link_attribution")
        manual = rows(connection, "SELECT link_id,tenant,owner,operation_id,context_json FROM youtube_manual_short_link")
        publications = rows(connection, "SELECT id,job_id,source_kind,operator_user_id,operator_name,"
                            "channel_id,preparation_id,content_id,video_id,privacy_status,video_state,"
                            "video_published_at_utc,workflow,public_status FROM drama_youtube_publish")
        preparations = rows(connection, "SELECT id,tenant,owner,"
                            "json_extract(body,'$.material.macro_name') AS drama_name,"
                            "json_extract(body,'$.material.language') AS language,"
                            "json_extract(body,'$.channel.name') AS channel_name FROM youtube_auto_preparation")
        jobs = rows(connection, "SELECT job_id,drama_name,language FROM drama_material_job")
    names = {(row["tenant_key"], row["user_id"]): row["name"] for row in users}
    tenants = defaultdict(set)
    for row in users:
        if row["tenant_key"]:
            tenants[row["user_id"]].add(row["tenant_key"])
    frozen = defaultdict(list)
    for row in automatic:
        context = json.loads(row["context"])
        if (context.get("version") != "youtube-attribution-v1" or not context.get("task_id")
                or context["task_id"] != row["task_id"]):
            raise ReportError("YouTube 自动链接冻结任务或版本不一致，待核实", 503)
        frozen[row["link_id"]].append(dict(context, link_type="auto", expected_campaign_id=context["task_id"]))
    for row in manual:
        context = json.loads(row["context_json"])
        version = context.get("version", "youtube-manual-link-v1")
        if (version not in {"youtube-manual-link-v1", "youtube-manual-link-v2"}
                or not context.get("operation_id") or context["operation_id"] != row["operation_id"]):
            raise ReportError("YouTube 手动短链冻结操作或版本不一致，待核实", 503)
        expected = ("yt_manual_" if version == "youtube-manual-link-v1" else "") + context["operation_id"]
        frozen[row["link_id"]].append(dict(context, tenant=row["tenant"], owner=row["owner"], link_type="manual",
                                         expected_campaign_id=expected))
    prep = {row["id"]: row for row in preparations}
    job = {row["job_id"]: row for row in jobs}
    published = defaultdict(list)
    for row in publications:
        published[(row["job_id"], row["source_kind"])].append(row)
    catalog = []
    for link in links:
        key = link_key(link["long_url"], "2284")
        if not key:
            continue
        ledger = published.get((link["job_id"], link["material_kind"]), [])
        contexts = list(frozen.get(link["id"], []))
        if not contexts:
            for publication in ledger:
                owner = publication["operator_user_id"]
                preparation = prep.get(publication["preparation_id"])
                tenant = (preparation["tenant"] if preparation and preparation["owner"] == owner
                          else next(iter(tenants[owner])) if len(tenants[owner]) == 1 else "")
                saved, original = preparation or {}, job.get(publication["job_id"], {})
                contexts.append(dict(
                    tenant=tenant, owner=owner, owner_name=publication["operator_name"],
                    excluded=publication["operator_name"] == "internal-deployment-canary",
                    channel_id=publication["channel_id"],
                    channel_name=saved.get("channel_name") or publication["channel_id"],
                    content_id=link["content_id"],
                    drama_name=saved.get("drama_name") or original.get("drama_name") or link["content_id"],
                    language=saved.get("language") or original.get("language") or "未记录", link_type="auto"))
        for context in contexts:
            if context.get("expected_campaign_id", str(link["job_id"])) != key[0]:
                raise ReportError("YouTube 冻结归因 ID 与链接不一致，待核实", 503)
            tenant, owner = str(context.get("tenant") or ""), str(context.get("owner") or "")
            excluded = bool(context.get("excluded")) or bool(ledger) and all(
                row["operator_name"] == "internal-deployment-canary" for row in ledger)
            channel = str(context.get("channel_id") or "")
            facts = []
            if context["link_type"] != "manual":
                for publication in ledger:
                    day = utc_day(publication["video_published_at_utc"])
                    # Comment failures do not undo a confirmed public video.
                    if (publication["video_state"] == "published"
                            and publication["privacy_status"] == "public"
                            and (publication["workflow"] != "reviewed_thumbnail"
                                 or publication["public_status"] == "succeeded")
                            and re.fullmatch(r"[A-Za-z0-9_-]{11}", publication["video_id"] or "")
                            and str(publication["channel_id"]) == channel and day):
                        facts.append({"id": "youtube:" + publication["video_id"], "date": day,
                                      "kind": "video"})
            catalog.append(normalized("youtube", dict(
                key=key, link_id=link["id"], tenant=tenant, owner=owner,
                owner_label=names.get((tenant, owner)) or context.get("owner_name") or owner or "待归属",
                drama=str(context.get("content_id") or link["content_id"]),
                drama_label=context.get("drama_name") or link["content_id"],
                channel=channel, channel_label=context.get("channel_name") or channel or "未记录",
                language=context.get("language") or "未记录", language_label=context.get("language") or "未记录",
                link_type=context["link_type"],
                link_type_label="手动短链" if context["link_type"] == "manual" else "自动发布",
                date=utc_day(link["created_at_utc"]), excluded=excluded, publications=facts)))
    return catalog


def read_x(path):
    with snapshot(path) as connection:
        records = rows(connection, """
            SELECT a.log_id,a.queue_id,a.version,a.context_json,l.long_url,l.created_at,
                   l.status AS log_status,l.x_post_id,l.published_at,l.unknown_outcome,
                   q.status AS queue_status,q.account_id,
                   r.status AS repost_status,r.target_account_id,r.source_post_id,
                   r.reposted_at,r.unknown_outcome AS repost_unknown
            FROM x_post_link_attribution a
            JOIN x_post_publish_log l ON l.id=a.log_id AND l.queue_id=a.queue_id
            JOIN x_post_queue q ON q.id=a.queue_id
            LEFT JOIN x_post_repost_ledger r ON r.queue_id=q.id
            """)
        missing = connection.execute("""
            SELECT COUNT(*) FROM x_post_publish_log l
            LEFT JOIN x_post_link_attribution a ON a.log_id=l.id AND a.queue_id=l.queue_id
            WHERE l.long_url<>'' AND a.log_id IS NULL
            """).fetchone()[0]
        if missing:
            raise ReportError("X 历史链接归属尚未迁移完整，本次不按零统计", 503)
    catalog = []
    for record in records:
        context = json.loads(record["context_json"])
        if record["version"] != "x-attribution-v2" or context.get("version") != "x-attribution-v2":
            raise ReportError("X 冻结归因版本不支持，待核实", 503)
        if not record["long_url"]:
            # Freezing identity precedes first URL generation. No effect can
            # reach this unpublished URL yet.
            continue
        key = link_key(record["long_url"], "2116")
        if not key or key[0] != str(context.get("af_c_id") or "") or key[0] != str(record["queue_id"]):
            raise ReportError("X 冻结归因与发布链接不一致，待核实", 503)
        facts = []
        if (record["log_status"] == record["queue_status"] == "published"
                and not record["unknown_outcome"]):
            if record["repost_status"] is not None:
                day = utc_day(record["reposted_at"])
                if (record["repost_status"] == "reposted" and not record["repost_unknown"]
                        and record["target_account_id"] == record["account_id"]
                        and record["source_post_id"] and day):
                    facts.append({"id": "x:repost:%s:%s" % (record["target_account_id"], record["source_post_id"]),
                                  "date": day, "kind": "repost"})
            else:
                day = utc_day(record["published_at"])
                if record["x_post_id"] and day:
                    facts.append({"id": "x:post:" + record["x_post_id"], "date": day, "kind": "post"})
        link_type = str(context.get("link_type") or "system")
        catalog.append(normalized("x", dict(
            key=key, link_id=record["log_id"], tenant=str(context.get("tenant") or ""),
            owner=str(context.get("owner") or ""), owner_label=context.get("owner_name") or "待归属",
            sub_user_id=context.get("sub_user_id"), drama=str(context.get("content_id") or ""),
            drama_label=str(context.get("drama_name") or context.get("content_id") or "未记录"),
            channel=str(context.get("channel_id") or ""),
            channel_label=context.get("channel_name") or context.get("channel_id") or "未记录",
            language=context.get("language") or "未记录", language_label=context.get("language") or "未记录",
            link_type=link_type, link_type_label={"manual": "手动发布", "auto_template": "自动模板",
                                                "system": "系统自动发布"}.get(link_type, link_type),
            date=utc_day(record["created_at"]), excluded=bool(context.get("excluded")), publications=facts)))
    return catalog


def read_catalogs(app, platforms):
    catalog, errors = [], CatalogErrors()
    readers = {
        "youtube": lambda: read_youtube(app["JOB_DB_PATH"]),
        "x": lambda: read_x(app.get("SOCIAL_ANALYTICS_X_DB_PATH",
                                   "/var/lib/x-post-automation/accounts.sqlite3")),
    }
    for platform in platforms:
        try:
            if platform == "facebook":
                from .facebook import read_facebook
                values, failures, metadata = read_facebook(app)
                catalog.extend(values)
                errors.update(failures)
                errors.source_metadata.update(metadata)
                continue
            if platform not in readers:
                raise ReportError("渠道尚未接入", 400)
            catalog.extend(readers[platform]())
        except Exception:
            # Never send SQLite paths, connection metadata or frozen contexts to HTTP.
            for spec in PLATFORMS[platform]["sources"]:
                errors[source_key(platform, spec["site_id"])] = "发布归因账本读取失败或迁移尚未完成，待核实"
    return catalog, errors

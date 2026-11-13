"""Read-only, owner-scoped monitor snapshots over coordinator projections.

No platform API, publishing database, credential, payload or artifact is read.
Snapshots use one SQLite read transaction and a short, scope-keyed bounded cache.
"""
import base64
import contextlib
import copy
import datetime as dt
import json
import math
import re
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from .core import DispatchError, stamp
from .monitor_contract import CHANNELS, CHANNEL_MODULES, STAGES, TOTAL_KEYS

_CACHE = OrderedDict()
_LOCK = threading.Lock()
CACHE_SECONDS = 5
HEARTBEAT_SECONDS = 45
SOURCE_STALE_SECONDS = 120
MAX_TIMING_SAMPLES = 10000
ACTIVE = ("queued", "ready", "running", "legacy_running", "unknown", "backlog_paused")


def invalidate_cache(store):
    with _LOCK:
        for key in list(_CACHE):
            if key[0] == str(store.path):
                del _CACHE[key]


def _datetime(value):
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _seconds(start, finish):
    start, finish = _datetime(start), _datetime(finish)
    return max(0, (finish - start).total_seconds()) if start and finish else None


def _window(filters, now):
    name = filters.get("window", "today")
    if name == "today":
        begin = now.astimezone(dt.timezone(dt.timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0)
    elif name in ("hour", "day", "week"):
        begin = now - dt.timedelta(seconds={"hour": 3600, "day": 86400, "week": 604800}[name])
    else:
        raise DispatchError("monitor_window_invalid", status=400)
    return stamp(begin), stamp(now)


def _scope(actor, filters):
    channels = [channel for channel in CHANNELS if channel in actor.get("allowed_channels", [])]
    if filters.get("channel"):
        if filters["channel"] not in channels:
            raise DispatchError("permission_denied", status=403)
        channels = [filters["channel"]]
    if not channels:
        return "0", [], channels
    clauses = ["j.channel IN (%s)" % ",".join("?" * len(channels))]
    args = list(channels)
    if not actor.get("is_admin"):
        user, tenant = str(actor.get("user_id") or ""), str(actor.get("tenant_key") or "")
        modules = sorted(set(actor.get("allowed_modules", [])) & {m for values in CHANNEL_MODULES.values() for m in values})
        if not user or not tenant or not modules:
            return "0", [], channels
        owner_clause = "(j.owner_user_id=? AND j.owner_tenant_key=?)"
        owner_args = [user, tenant]
        if actor.get("fb_owner_user_id") and "fb_page_posts" in modules:
            owner_clause = "(" + owner_clause + " OR (j.channel='fb' AND j.required_module='fb_page_posts' AND j.owner_user_id=?))"
            owner_args.append(str(actor["fb_owner_user_id"]))
        clauses += [owner_clause, "j.required_module IN (%s)" % ",".join("?" * len(modules))]
        args += owner_args + modules
    return " AND ".join(clauses), args, channels


def _health(conn, actor, channels, now):
    if not channels:
        return []
    rows = conn.execute("SELECT channel,family,state,collected_at,error_code FROM pipeline_source_health WHERE channel IN (%s) ORDER BY channel,family LIMIT 1000" % ",".join("?" * len(channels)), channels).fetchall()
    modules = set(actor.get("allowed_modules") or [])
    result = []
    for row in rows:
        item = dict(row)
        if not actor.get("is_admin") and row["channel"] == "fb":
            # Source health has no owner data, but retain the separate FB gates.
            family = row["family"].lower()
            required = "fb_page_posts" if "auto" in family else "fb_video_publish"
            if required not in modules:
                continue
        observed = _datetime(row["collected_at"])
        stale = not observed or (now - observed).total_seconds() > SOURCE_STALE_SECONDS
        item["state"] = "unavailable" if row["state"] in ("error", "failed", "unavailable") else "stale" if stale else row["state"] if row["state"] in ("collecting", "backfilling") else "healthy"
        result.append(item)
    return result


def _common(conn, actor, channels, now):
    sources = _health(conn, actor, channels, now)
    collected = min((r["collected_at"] for r in sources if r["collected_at"]), default=None)
    missing = set(channels) - {r["channel"] for r in sources}
    warnings = []
    if missing:
        warnings.append("部分渠道尚未完成采集，缺失计数与耗时不按零统计")
    if any(row["state"] == "unavailable" for row in sources):
        warnings.append("部分渠道采集失败，保留最近成功同步的任务记录")
    if any(row["state"] == "collecting" for row in sources):
        warnings.append("正在加载近期账本，当前计数尚未完整")
    elif any(row["state"] == "backfilling" for row in sources):
        warnings.append("近期账本已采集，较早历史记录仍在后台补齐")
    return {"generated_at": stamp(now), "collected_at": collected,
            "stale": bool(missing or any(r["state"] != "healthy" for r in sources)),
            "scope": "all" if actor.get("is_admin") else "mine",
            "allowed_channels": [c for c in CHANNELS if c in actor.get("allowed_channels", [])],
            "warnings": warnings}, sources


def capacity_dto(config):
    return {"version": config["version"],
            "hosts": [{"host_id": host, "stage": stage, "max_workers": pool.get("target"),
                       "ceiling": pool.get("ceiling")} for host, pools in sorted(config["hosts"].items())
                      for stage, pool in pools.items() if stage in STAGES],
            "global_publish_limit": config.get("global_publish_limit"),
            "channel_limits": {c: config.get("channel_limits", {}).get(c) for c in CHANNELS}}


def _summary(conn, scope, args, common, sources, channels, start, end, now):
    active = "('queued','ready','running','legacy_running','unknown','backlog_paused')"
    grouped = "SELECT j.channel,j.stage,j.status,SUM(j.n) n FROM pipeline_monitor_counts j WHERE " + scope
    rows = conn.execute(grouped + " AND j.status IN " + active + " GROUP BY j.channel,j.stage,j.status UNION ALL " +
                        grouped + " AND j.status IN ('completed','published','failed') AND j.finished_at>=? AND j.finished_at<=? GROUP BY j.channel,j.stage,j.status", args + args + [start, end]).fetchall()
    published = conn.execute("SELECT j.channel,COUNT(*) n FROM pipeline_publication j WHERE " + scope + " AND j.published_at>=? AND j.published_at<=? GROUP BY j.channel", args + [start, end]).fetchall()
    known = {r["channel"] for r in sources} | {r["channel"] for r in rows} | {r["channel"] for r in published}
    by = {(channel, stage): {"channel": channel, "stage": stage,
                           **{k: (0 if channel in known else None) for k in ("queued", "running", "ready", "completed", "failed", "unknown", "backlog_paused", "throughput_per_hour")},
                           "avg_duration_seconds": None, "p95_duration_seconds": None,
                           "avg_wait_seconds": None, "oldest_wait_seconds": None,
                           "duration_samples": 0}
          for channel in channels for stage in STAGES}
    totals = {k: 0 if known or not channels else None for k in TOTAL_KEYS}
    for row in rows:
        item = by[(row["channel"], row["stage"])]
        status = "running" if row["status"] == "legacy_running" else row["status"]
        if row["stage"] == "publish" and status in ("completed", "published"):
            # Publication comes only from its unique canonical target fact.
            # The workflow may still be unknown because a later comment is pending.
            continue
        target = "completed" if status in ("completed", "published") else status
        item[target] = (item.get(target) or 0) + row["n"]
        key = ("preparing" if row["stage"] == "prepare" else "publishing") if status == "running" else ("prepared" if row["stage"] == "prepare" else "published") if status in ("completed", "published") else status
        if key in totals:
            totals[key] = (totals[key] or 0) + row["n"]
    hour = stamp(now - dt.timedelta(hours=1))
    for row in published:
        by[(row["channel"], "publish")]["completed"] = row["n"]
        totals["published"] = (totals["published"] or 0) + row["n"]
    for row in conn.execute("SELECT j.channel,j.stage,SUM(j.n) n FROM pipeline_monitor_counts j WHERE " + scope + " AND j.stage='prepare' AND j.status='completed' AND j.finished_at>=? AND j.finished_at<=? GROUP BY j.channel,j.stage", args + [hour, end]):
        by[(row["channel"], row["stage"])]["throughput_per_hour"] = row["n"]
    for row in conn.execute("SELECT j.channel,COUNT(*) n FROM pipeline_publication j WHERE " + scope + " AND j.published_at>=? AND j.published_at<=? GROUP BY j.channel", args + [hour, end]):
        by[(row["channel"], "publish")]["throughput_per_hour"] = row["n"]
    timing_rows = conn.execute("SELECT j.channel,j.stage,j.started_at,j.finished_at,j.scheduled_at FROM pipeline_job j WHERE " + scope + " AND j.hold_reason<>'metadata_only' AND j.stage='prepare' AND j.status='completed' AND j.finished_at>=? AND j.finished_at<=? AND j.started_at<>'' UNION ALL SELECT j.channel,'publish' stage,t.started_at,j.published_at finished_at,t.scheduled_at FROM pipeline_publication j JOIN pipeline_job t ON t.id=j.job_id WHERE " + scope + " AND t.hold_reason<>'metadata_only' AND j.published_at>=? AND j.published_at<=? AND t.started_at<>'' ORDER BY finished_at DESC LIMIT ?", args + [start, end] + args + [start, end, MAX_TIMING_SAMPLES + 1]).fetchall()
    samples = {}
    for row in timing_rows[:MAX_TIMING_SAMPLES]:
        seconds = _seconds(row["started_at"], row["finished_at"])
        wait = _seconds(row["scheduled_at"], row["started_at"])
        if seconds is not None:
            samples.setdefault((row["channel"], row["stage"]), []).append((seconds, wait))
    for key, values in samples.items():
        durations = sorted(v[0] for v in values)
        waits = [v[1] for v in values if v[1] is not None]
        by[key].update(avg_duration_seconds=sum(durations) / len(durations),
                       p50_duration_seconds=durations[max(0, math.ceil(len(durations) * .5) - 1)],
                       p95_duration_seconds=durations[max(0, math.ceil(len(durations) * .95) - 1)],
                       avg_wait_seconds=sum(waits) / len(waits) if waits else None,
                       duration_samples=len(durations))
    if len(timing_rows) > MAX_TIMING_SAMPLES:
        common["warnings"].append("耗时分位数使用所选范围内最近10000条已完成任务；计数保持完整")
    for row in conn.execute("SELECT j.channel,j.stage,MIN(j.scheduled_at) oldest FROM pipeline_job j WHERE " + scope + " AND j.hold_reason<>'metadata_only' AND j.status IN ('queued','ready') AND j.scheduled_at<=? GROUP BY j.channel,j.stage", args + [end]):
        by[(row["channel"], row["stage"])]["oldest_wait_seconds"] = _seconds(row["oldest"], end)
    missing_times = conn.execute("SELECT SUM(j.n) FROM pipeline_monitor_counts j WHERE " + scope + " AND j.status IN ('completed','published','failed') AND j.finished_at=''", args).fetchone()[0]
    missing_publication_times = conn.execute("SELECT COUNT(*) FROM pipeline_publication j WHERE " + scope + " AND j.published_at=''", args).fetchone()[0]
    if missing_times or missing_publication_times:
        common["warnings"].append("部分历史回执缺少完成时间，未计入分时完成量和耗时")
    failed_publications = {row["channel"]: row["n"] for row in conn.execute("SELECT j.channel,COUNT(*) n FROM pipeline_job j WHERE " + scope + " AND j.hold_reason<>'metadata_only' AND j.stage='publish' AND j.status='failed' AND j.finished_at>=? AND j.finished_at<=? AND NOT EXISTS (SELECT 1 FROM pipeline_publication p WHERE p.source_key=j.source_key) GROUP BY j.channel", args + [start, end])}
    for item in by.values():
        failures = failed_publications.get(item["channel"], 0) if item["stage"] == "publish" else (item["failed"] or 0)
        item["failed_without_publication"] = failures if item["stage"] == "publish" else None
        outcomes = (item["completed"] or 0) + failures
        item["success_rate"] = item["completed"] / outcomes if outcomes else None
    phase_names = {"download": "download", "render": "render", "prepare": "prepare", "artifact_upload": "artifact_transfer", "upload_artifact": "artifact_transfer", "upload": "platform_upload", "platform_upload": "platform_upload", "media_uploading": "platform_upload", "processing": "platform_processing", "platform_processing": "platform_processing"}
    efficiency_scope = (scope + " AND j.hold_reason<>'metadata_only' AND ((j.stage='prepare' AND j.status IN ('completed','failed') AND j.finished_at>=? AND j.finished_at<=?) OR (j.stage='publish' AND (f.published_at>=? AND f.published_at<=? OR (f.source_key IS NULL AND j.status='failed' AND j.finished_at>=? AND j.finished_at<=?))))")
    efficiency_args = args + [hour, end, hour, end, hour, end]
    phase_rows = conn.execute("SELECT j.id,j.channel,j.stage,p.phase,p.seconds FROM pipeline_job j JOIN pipeline_phase_metric p ON p.job_id=j.id AND p.generation=j.generation LEFT JOIN pipeline_publication f ON f.source_key=j.source_key WHERE " + efficiency_scope + " ORDER BY COALESCE(NULLIF(f.published_at,''),j.finished_at) DESC LIMIT ?", efficiency_args + [MAX_TIMING_SAMPLES * 8]).fetchall()
    phase_samples = {}
    for row in phase_rows:
        phase = "platform_submit" if (row["channel"], row["stage"], row["phase"]) in {
            ("fb", "publish", "publish"), ("tt", "publish", "publish_initialize")
        } else phase_names.get(row["phase"])
        if phase:
            phase_samples.setdefault((row["channel"], row["stage"], phase), []).append(row["seconds"])
    # FB pulls a URL after accepting the POST. This interval includes its fetch,
    # processing and our reconciliation cadence, not pure platform processing.
    # Old checkpoints have only a hash; never guess their post identity.
    # Bound the publication window before touching the growing event history.
    # LIMIT also prevents SQLite from flattening this into an event-table scan.
    confirmation_rows = conn.execute(
        "WITH recent AS (SELECT j.id,j.generation,f.publish_id,f.published_at "
        "FROM pipeline_publication f INDEXED BY pipeline_publication_time "
        "JOIN pipeline_job j ON j.id=f.job_id WHERE f.channel='fb' "
        "AND f.published_at>=? AND f.published_at<=? AND " + scope +
        " AND j.channel='fb' AND j.stage='publish' AND j.hold_reason<>'metadata_only' "
        "ORDER BY f.published_at DESC LIMIT ?) "
        "SELECT j.id,j.generation,j.publish_id,j.published_at,e.details_json,p.checkpoint_id,"
        "p.generation checkpoint_generation,p.received_at FROM recent j "
        "JOIN pipeline_event e INDEXED BY pipeline_event_job_type ON e.job_id=j.id AND e.event_type='checkpoint' "
        "JOIN pipeline_checkpoint p ON p.job_id=j.id AND p.checkpoint_id=json_extract(e.details_json,'$.checkpoint_id') "
        "WHERE p.phase='fb_publish_attempt' ORDER BY j.published_at DESC,p.received_at ASC LIMIT ?",
        [hour, end] + args + [MAX_TIMING_SAMPLES, MAX_TIMING_SAMPLES * 8]).fetchall()
    confirmations = {}
    for row in confirmation_rows:
        evidence = json.loads(row["details_json"])
        submitted, published = _datetime(row["received_at"]), _datetime(row["published_at"])
        if (evidence.get("kind") != "success" or evidence.get("phase") != "fb_publish_attempt"
                or evidence.get("post_id") != row["publish_id"]
                or evidence.get("generation") != row["generation"]
                or row["checkpoint_generation"] != row["generation"]
                or not submitted or not published or published < submitted):
            continue
        confirmations.setdefault(row["id"], (published - submitted).total_seconds())
    if confirmations:
        phase_samples[("fb", "publish", "platform_confirmation_wait")] = list(confirmations.values())
    for row in conn.execute("SELECT j.channel,j.stage,j.scheduled_at,j.started_at FROM pipeline_job j LEFT JOIN pipeline_publication f ON f.source_key=j.source_key WHERE " + efficiency_scope + " AND j.started_at<>'' ORDER BY COALESCE(NULLIF(f.published_at,''),j.finished_at) DESC LIMIT ?", efficiency_args + [MAX_TIMING_SAMPLES]):
        wait = _seconds(row["scheduled_at"], row["started_at"])
        if wait is not None:
            phase_samples.setdefault((row["channel"], row["stage"], "queue"), []).append(wait)
    efficiency = []
    for channel in channels:
        for stage in STAGES:
            for phase in ("download", "render", "artifact_transfer", "platform_submit", "platform_upload", "platform_processing", "platform_confirmation_wait", "queue"):
                values = sorted(phase_samples.get((channel, stage, phase), []))
                applicable = not (phase == "platform_upload" and channel in ("fb", "tt") and stage == "publish" and not values)
                efficiency.append({"channel": channel, "stage": stage, "phase": phase, "applicable": applicable, "samples": len(values), "p50_seconds": values[max(0, math.ceil(len(values) * .5) - 1)] if values else None, "p95_seconds": values[max(0, math.ceil(len(values) * .95) - 1)] if values else None})
    return dict(common, window={"start": start, "end": end, "timezone": "Asia/Shanghai"},
                totals=totals, channels=list(by.values()), sources=sources, efficiency=efficiency)


def _safe_message(value):
    text = str(value or "")[:500]
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [已隐藏]", text)
    return re.sub(r"(?i)(access_token|refresh_token|client_secret|token|password)([=:\s]+)[^\s&,]+", r"\1\2[已隐藏]", text)


def _detail_url(value):
    value = str(value or "")
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc and re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_./-]*\.html", parsed.path) else ""


def _tasks(conn, scope, args, common, filters, start, end):
    clause = scope + " AND (j.status IN ('queued','ready','running','legacy_running','unknown','backlog_paused') OR (j.finished_at>=? AND j.finished_at<=?))"
    values = list(args) + [start, end]
    for key, column in (("stage", "stage"), ("host_id", "node_id")):
        if filters.get(key):
            clause += " AND j." + column + "=?"
            values.append(filters[key])
    if filters.get("status"):
        if filters["status"] == "running":
            clause += " AND j.status IN ('running','legacy_running')"
        else:
            clause += " AND j.status=?"
            values.append(filters["status"])
    total = conn.execute("SELECT COUNT(*) FROM pipeline_job j WHERE " + clause, values).fetchone()[0]
    if filters.get("cursor"):
        try:
            cursor = int(base64.urlsafe_b64decode(str(filters["cursor"]).encode("ascii")).decode("ascii"))
            if cursor <= 0:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise DispatchError("monitor_cursor_invalid", status=400) from None
        clause += " AND j.id<?"
        values.append(cursor)
    limit = filters.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DispatchError("monitor_limit_invalid", status=400)
    fields = ("id,source_key,source_id,channel,family,stage,status,node_id,worker_id,account_label,owner_name,scheduled_at,source_created_at,started_at,finished_at,updated_at,error_code,error_message,detail_url")
    rows = conn.execute("SELECT " + fields + " FROM pipeline_job j WHERE " + clause + " ORDER BY j.id DESC LIMIT ?", values + [limit + 1]).fetchall()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item["source_family"] = item.pop("family")
        item["host_id"] = item.pop("node_id")
        item["created_at"] = item.pop("source_created_at")
        item["wait_seconds"] = _seconds(item["scheduled_at"], item["started_at"] or end)
        item["duration_seconds"] = _seconds(item["started_at"], item["finished_at"] or (end if item["status"] in ("running", "legacy_running") else ""))
        item["detail_url"] = _detail_url(item["detail_url"])
        item["error_message"] = _safe_message(item["error_message"])
        items.append(item)
    next_cursor = base64.urlsafe_b64encode(str(rows[limit - 1]["id"]).encode()).decode() if len(rows) > limit else ""
    return dict(common, items=items, total=total, next_cursor=next_cursor)


def _json(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _workers(conn, store, actor, common, now):
    if not actor.get("is_admin"):
        return dict(common, items=[], processes=[], capacity=None,
                    visibility_message="服务器与进程全局明细仅管理员可见；个人任务进度见下方任务列表。")
    config = store.config(conn)
    nodes = {row["node_id"]: dict(row) for row in conn.execute("SELECT * FROM pipeline_node ORDER BY node_id LIMIT 1000")}
    labels = {row["node_id"]: row["label"] for row in conn.execute("SELECT node_id,label FROM pipeline_node_auth")}
    workers = [dict(row) for row in conn.execute("SELECT * FROM pipeline_worker ORDER BY node_id,stage,worker_id LIMIT 10001")]
    if len(workers) > 10000:
        raise DispatchError("monitor_worker_limit", status=503)
    active = [dict(row) for row in conn.execute("SELECT id,source_key,node_id,worker_id,stage,channel,started_at,generation FROM pipeline_job WHERE status IN ('running','legacy_running') LIMIT 10001")]
    if len(active) > 10000:
        raise DispatchError("monitor_active_limit", status=503)
    jobs = {(j["node_id"], j["worker_id"]): j for j in active}
    current = {j["id"]: j for j in active}
    phases = {}
    if current:
        # Bounded worker count, no content payload or full event history.
        for offset in range(0, len(current), 400):
            ids = list(current)[offset:offset + 400]
            for row in conn.execute("SELECT job_id,phase FROM (SELECT p.job_id,p.phase,ROW_NUMBER() OVER (PARTITION BY p.job_id ORDER BY p.received_at DESC,p.checkpoint_id DESC) latest FROM pipeline_checkpoint p JOIN pipeline_job j ON j.id=p.job_id AND j.generation=p.generation WHERE p.job_id IN (%s)) WHERE latest=1" % ",".join("?" * len(ids)), ids):
                phases[row["job_id"]] = row["phase"]
    fresh = stamp(now - dt.timedelta(seconds=HEARTBEAT_SECONDS))
    processes = []
    for worker in workers:
        job = jobs.get((worker["node_id"], worker["worker_id"]))
        observed = _json(worker.get("observation_json", "{}"))
        state = "offline" if worker["last_seen_at"] < fresh else "busy" if job else "draining" if worker["draining"] else "idle"
        processes.append({"worker_id": worker["worker_id"], "host_id": worker["node_id"], "stage": worker["stage"],
                          "pid": worker["pid"], "status": state, "current_task_id": job["id"] if job else None,
                          "current_source_key": job["source_key"] if job else "", "channel": job["channel"] if job else "",
                          "phase": (observed.get("phase") if observed.get("phase") not in (None, "", "idle") else phases.get(job["id"], "")) if job else "",
                          "elapsed_seconds": _seconds(job["started_at"], stamp(now)) if job else None,
                          "last_seen_at": worker["last_seen_at"],
                          "idle_reason": _safe_message(observed.get("idle_reason") or ("停止新领取，等待当前任务完成" if worker["draining"] else "")),
                          "capabilities": observed.get("capabilities", []) if isinstance(observed.get("capabilities", []), list) else []})
    today = _window({"window": "today"}, now)[0]
    hour = stamp(now - dt.timedelta(hours=1))
    stats = {}
    for row in conn.execute("SELECT node_id,stage,COUNT(DISTINCT CASE WHEN finished_at>=? THEN source_key END) today,COUNT(DISTINCT CASE WHEN finished_at>=? THEN source_key END) hour,AVG(CASE WHEN started_at<>'' AND finished_at>=? THEN MAX(0,(julianday(finished_at)-julianday(started_at))*86400) END) avg_seconds FROM pipeline_job WHERE stage='prepare' AND status='completed' AND hold_reason<>'metadata_only' AND finished_at>=? AND finished_at<=? GROUP BY node_id,stage UNION ALL SELECT p.node_id,'publish' stage,COUNT(CASE WHEN p.published_at>=? THEN p.source_key END) today,COUNT(CASE WHEN p.published_at>=? THEN p.source_key END) hour,AVG(CASE WHEN j.started_at<>'' AND p.published_at>=? THEN MAX(0,(julianday(p.published_at)-julianday(j.started_at))*86400) END) avg_seconds FROM pipeline_publication p LEFT JOIN pipeline_job j ON j.id=p.job_id WHERE p.published_at>=? AND p.published_at<=? GROUP BY p.node_id", (today, hour, today, min(today, hour), stamp(now)) * 2):
        stats[(row["node_id"], row["stage"])] = dict(row)
    items = []
    metric_names = ("cpu_percent", "cpu_count", "memory_used_bytes", "memory_total_bytes", "gpu_percent", "gpu_memory_used_bytes", "gpu_memory_total_bytes", "encoder_percent", "gpu_count")
    for host in sorted(set(nodes) | set(config["hosts"])):
        node = nodes.get(host, {})
        metrics = _json(node.get("metrics_json", "{}"))
        item = {"host_id": host, "label": labels.get(host) or {"cpu-main": "CPU 服务器", "hk-gpu": "香港 GPU"}.get(host, host),
                "last_seen_at": node.get("last_seen_at"), "stale": not node or node["last_seen_at"] < fresh}
        item.update({name: metrics.get(name) if isinstance(metrics.get(name), (float, int)) and not isinstance(metrics.get(name), bool) and math.isfinite(metrics[name]) else None for name in metric_names})
        item["stages"] = []
        for stage in STAGES:
            pool = config["hosts"].get(host, {}).get(stage, {})
            group = [p for p in processes if p["host_id"] == host and p["stage"] == stage]
            stat = stats.get((host, stage), {})
            item["stages"].append({"stage": stage, "max_workers": pool.get("target"), "ceiling": pool.get("ceiling"),
                                   "alive_workers": sum(p["status"] != "offline" for p in group) if node else None,
                                   "busy_workers": sum(p["status"] == "busy" for p in group) if node else None,
                                   "idle_workers": sum(p["status"] == "idle" for p in group) if node else None,
                                   "completed_today": stat.get("today", 0) if node else None,
                                   "throughput_per_hour": stat.get("hour", 0) if node else None,
                                   "avg_duration_seconds": stat.get("avg_seconds")})
        items.append(item)
    return dict(common, items=items, processes=processes, capacity=capacity_dto(config))


def query(store, view, actor, filters):
    if view not in ("summary", "workers", "tasks"):
        raise DispatchError("monitor_view_invalid", status=400)
    safe_actor = {k: actor.get(k) for k in ("tenant_key", "user_id", "is_admin", "allowed_channels", "allowed_modules", "fb_owner_user_id")}
    key = (str(store.path), view, json.dumps(safe_actor, sort_keys=True), json.dumps(filters, sort_keys=True))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            _CACHE.move_to_end(key)
            return copy.deepcopy(cached[1])
    now = store.clock().astimezone(dt.timezone.utc)
    start, end = _window(filters, now)
    scope, args, channels = _scope(safe_actor, filters)
    with contextlib.closing(store.connect()) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        common, sources = _common(conn, safe_actor, channels, now)
        if view == "summary":
            result = _summary(conn, scope, args, common, sources, channels, start, end, now)
        elif view == "tasks":
            result = _tasks(conn, scope, args, common, filters, start, end)
        else:
            result = _workers(conn, store, safe_actor, common, now)
    with _LOCK:
        _CACHE[key] = (time.monotonic(), copy.deepcopy(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return result

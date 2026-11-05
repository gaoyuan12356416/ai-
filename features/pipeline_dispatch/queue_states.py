"""Read-only monitor classification; never changes dispatch or publication state.

Historical outcomes use existing incremental counters. Only indexed active jobs
are inspected for appointments, explicit holds and uncertain account blockers.
"""
import datetime as dt
import json

KEYS = ("waiting", "executing", "scheduled", "abnormal")
LIMIT = 10000


def _object(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _time(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.astimezone(dt.timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def running_issue(conn, row, now):
    if row["status"] == "legacy_running" or not row["worker_id"]:
        return ""
    worker = conn.execute("SELECT last_seen_at,observation_json FROM pipeline_worker WHERE node_id=? AND worker_id=?",
                          (row["node_id"], row["worker_id"])).fetchone()
    seen = _time(worker["last_seen_at"]) if worker else None
    lease = _time(row["lease_expires_at"])
    if (not seen or (now - seen).total_seconds() > 120) and lease and lease < now:
        return "worker_offline"
    observed = _object(worker["observation_json"]) if worker else {}
    # Do not call a real render/upload abnormal just because an old attempt
    # expired. Detect repeated non-execution only with a fresh idle/selection
    # heartbeat and at least three expirations in the last thirty minutes.
    if (not seen or (now - seen).total_seconds() > 45 or row["generation"] < 4
            or observed.get("phase") not in ("idle", "selection")):
        return ""
    expired = conn.execute("SELECT created_at FROM pipeline_event INDEXED BY pipeline_event_job_type "
                           "WHERE job_id=? AND event_type='lease_expired' ORDER BY id DESC LIMIT 3",
                           (row["id"],)).fetchall()
    if len(expired) == 3 and all((_time(r["created_at"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
                               >= now - dt.timedelta(minutes=30) for r in expired):
        return "repeated_claim"
    return ""


def classify(conn, row, now):
    if row["status"] in ("running", "legacy_running"):
        issue = running_issue(conn, row, now)
        return ("abnormal", issue) if issue else ("executing", "")
    if row["hold_reason"]:
        return "abnormal", "held"
    scheduled = _time(row["scheduled_at"])
    if scheduled is None:
        return "abnormal", "schedule_missing"
    if scheduled > now:
        return "scheduled", ""
    try:
        resources = json.loads(row["resource_keys_json"])
    except (TypeError, ValueError):
        resources = []
    if not isinstance(resources, list) or not resources or not all(isinstance(k, str) for k in resources):
        return "abnormal", "identity_missing"
    for resource in set(resources):
        # Inspect blockers internally without disclosing another owner's task
        # identity or account in the owner-scoped public DTO.
        blocked = conn.execute("SELECT 1 FROM pipeline_resource r JOIN pipeline_job b ON b.id=r.job_id "
                               "WHERE r.resource_key=? AND (b.status='unknown' OR r.quarantined=1) "
                               "UNION ALL SELECT 1 FROM pipeline_legacy_resource r JOIN pipeline_job b ON b.id=r.job_id "
                               "WHERE r.resource_key=? AND b.status='unknown' LIMIT 1", (resource, resource)).fetchone()
        if blocked:
            return "abnormal", "blocked_by_unknown"
    return "waiting", ""


def add_summary(conn, scope, args, by, totals, common, now, start, end):
    # Collector updated_at is observation time, not an anomaly occurrence time.
    # Use the outcome timestamp, then the original task's execution/plan dates.
    anomaly_time = "COALESCE(NULLIF(j.finished_at,''),NULLIF(j.started_at,''),NULLIF(j.scheduled_at,''),NULLIF(j.source_created_at,''))"
    rows = conn.execute("SELECT j.id,j.channel,j.stage,j.status,j.hold_reason,j.scheduled_at,"
                        "j.resource_keys_json,j.node_id,j.worker_id,j.generation,j.lease_expires_at,"
                        + anomaly_time + " anomaly_at "
                        "FROM pipeline_job j WHERE " + scope +
                        " AND j.status IN ('queued','ready','running','legacy_running') "
                        "AND j.hold_reason<>'metadata_only' LIMIT ?", args + [LIMIT + 1]).fetchall()
    for item in by.values():
        known = item["queued"] is not None
        item.update({k: 0 if known else None for k in KEYS})
        item["abnormal_reasons"] = {}
        item["oldest_pending_seconds"] = None
    # Keep receipt/closure semantics, including unresolved post-publication
    # comments, while scoping both counts and their reason details to the window.
    outcomes = conn.execute("SELECT j.channel,j.stage,j.status,j.error_code,COUNT(*) n FROM pipeline_job j WHERE " + scope +
        " AND j.status IN ('failed','unknown') AND j.hold_reason<>'metadata_only'"
        " AND (j.status='unknown' OR (j.source_status NOT IN ('skipped','cancelled','canceled','abandoned','duplicate','no_candidate')"
        " AND NOT EXISTS (SELECT 1 FROM pipeline_publication p WHERE p.source_key=j.source_key)))"
        " AND " + anomaly_time + ">=? AND " + anomaly_time + "<=?"
        " GROUP BY j.channel,j.stage,j.status,j.error_code", args + [start, end]).fetchall()
    from features.post_daily_report.accounting import reason_label
    for outcome in outcomes:
        item = by[(outcome['channel'], outcome['stage'])]
        item['abnormal'] = (item['abnormal'] or 0) + outcome['n']
        code = 'unknown' if outcome['status'] == 'unknown' else outcome['error_code'] or 'failed'
        item['abnormal_reasons'][code] = item['abnormal_reasons'].get(code, 0) + outcome['n']
    if len(rows) > LIMIT:
        for item in by.values():
            item.update({key: None for key in KEYS})
            item["abnormal_reasons"] = {}
        totals.update({key: None for key in KEYS})
        common["warnings"].append("任务状态分类超过读取上限，四类计数暂不可用；未以部分数据代替总数")
        return
    begin, finish = _time(start), _time(end)
    for row in rows:
        item = by[(row["channel"], row["stage"])]
        state, reason = classify(conn, row, now)
        if state == "abnormal":
            occurred = _time(row["anomaly_at"])
            if occurred is None or not begin <= occurred <= finish:
                continue
        item[state] = (item[state] or 0) + 1
        if reason:
            item["abnormal_reasons"][reason] = item["abnormal_reasons"].get(reason, 0) + 1
        if state == "waiting":
            age = max(0, (now - _time(row["scheduled_at"])).total_seconds())
            item["oldest_pending_seconds"] = max(item["oldest_pending_seconds"] or 0, age)
    for key in KEYS:
        values = [item[key] for item in by.values() if item[key] is not None]
        totals[key] = sum(values) if values else (None if by else 0)
    for item in by.values():
        item['reason_labels'] = {code: reason_label(code) for code in item['abnormal_reasons']}

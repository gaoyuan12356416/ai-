"""Read-only admin log projection, including explicit queue controls."""
import contextlib
import re
import sqlite3
from pathlib import Path
from .service import (XPostStore, XPostError, _date_value, _positive_int,
    normalize_material_key, _drama_content_id, _schedule_source_type,
    _row_dict, redact_text, _public_drama_delivery_route)

def _readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn

def query_logs(db_path, payload=None, *, connection=None):
    payload = payload if isinstance(payload, dict) else {}
    page, page_size = XPostStore._pagination(payload)
    clauses = []
    visible_status = "CASE WHEN h.reason='operator_stopped' THEN 'stopped' ELSE COALESCE(l.status,q.status) END"
    values = []
    run_date = str(payload.get("run_date", "") or "").strip()
    if run_date:
        clauses.append("q.run_date=?")
        values.append(_date_value(run_date, "run_date"))
    source_date = str(payload.get("source_date", "") or "").strip()
    if source_date:
        clauses.append("q.source_date=?")
        values.append(_date_value(source_date, "source_date"))
    raw_account_id = payload.get("account_id")
    if raw_account_id not in (None, ""):
        clauses.append("q.account_id=?")
        values.append(_positive_int(raw_account_id, "account_id"))
    status = str(payload.get("status", "") or "").strip()
    if status:
        allowed_statuses = {
            "queued", "waiting_relay", "reserved", "publishing", "media_uploading",
            "post_creating", "source_published", "repost_creating",
            "published", "failed", "stopped", "cancelled",
        }
        if status not in allowed_statuses:
            raise XPostError("invalid_request", "status筛选值无效", 400)
        clauses.append("(" + visible_status + ")=?")
        values.append(status)
    material_id = str(payload.get("material_id", "") or "").strip()
    if material_id:
        if re.fullmatch(r"[0-9]+", material_id):
            clauses.append("q.material_key=?")
            values.append(normalize_material_key(material_id))
        else:
            clauses.append("q.content_id=?")
            values.append(_drama_content_id(material_id))
    source_type = str(payload.get("source_type", "") or "").strip()
    if source_type:
        clauses.append("q.source_type=?")
        values.append(_schedule_source_type(source_type))
    task_source_sql = (
        "CASE WHEN q.manual_run_id IS NOT NULL "
        "AND COALESCE(mr.trigger_source,'manual')='auto_template' "
        "THEN 'auto_publish' "
        "WHEN q.manual_run_id IS NOT NULL THEN 'manual_publish' "
        "WHEN q.source_type='drama' THEN 'drama_pool' "
        "ELSE 'material_pool' END"
    )
    task_source = str(payload.get("task_source", "") or "").strip().lower()
    if task_source:
        if task_source not in {
            "drama_pool",
            "material_pool",
            "auto_publish",
            "manual_publish",
        }:
            raise XPostError("invalid_request", "task_source筛选值无效", 400)
        clauses.append("(%s)=?" % task_source_sql)
        values.append(task_source)
    if "unknown_outcome" in payload and payload.get("unknown_outcome") not in (None, ""):
        raw_unknown = payload.get("unknown_outcome")
        if isinstance(raw_unknown, bool):
            unknown_outcome = 1 if raw_unknown else 0
        elif str(raw_unknown).strip() in {"0", "1"}:
            unknown_outcome = int(str(raw_unknown).strip())
        else:
            raise XPostError("invalid_request", "unknown_outcome必须为0或1", 400)
        clauses.append(
            "CASE WHEN l.status IN ('post_creating','repost_creating') OR COALESCE(l.unknown_outcome,0)=1 "
            "THEN 1 ELSE 0 END=?"
        )
        values.append(unknown_outcome)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    from_sql = (
        " FROM x_post_queue q "
        "LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
        "LEFT JOIN x_post_repost_ledger r ON r.queue_id=q.id "
        "LEFT JOIN x_post_drama_delivery_route d ON d.queue_id=q.id "
        "LEFT JOIN x_post_manual_run mr ON mr.id=q.manual_run_id "
        "LEFT JOIN pipeline_canonical_hold h ON h.source_key='x:queue:'||q.id"
    )
    select = (
        "SELECT q.id AS queue_id,q.run_id,q.catchup_run_id,"
        "q.schedule_run_id,q.manual_run_id,"
        "CASE WHEN q.run_id IS NOT NULL THEN 'daily' "
        "WHEN q.catchup_run_id IS NOT NULL THEN 'catchup' "
        "WHEN q.schedule_run_id IS NOT NULL THEN 'schedule' "
        "WHEN q.manual_run_id IS NOT NULL "
        "AND COALESCE(mr.trigger_source,'manual')='auto_template' "
        "THEN 'auto_template' "
        "WHEN q.manual_run_id IS NOT NULL THEN 'manual' "
        "ELSE 'canary' END AS batch_kind,"
        + task_source_sql
        + " AS task_source,"
        "q.source_type,q.run_date,q.source_date,q.account_id,"
        "q.delivery_mode,q.relay_account_id,q.relay_account_username,"
        "q.preflight_duration,"
        "COALESCE(d.route_version,0) AS route_version,"
        "COALESCE(d.route_state,'') AS route_state,"
        "COALESCE(d.resolved_delivery_mode,'') AS "
        "resolved_delivery_mode,"
        "COALESCE(d.preflight_width,0) AS preflight_width,"
        "COALESCE(d.preflight_height,0) AS preflight_height,"
        "COALESCE(d.resolved_at,'') AS resolved_at,"
        "COALESCE(r.status,'') AS repost_status,"
        "q.pool_item_id,q.pool_created_at,q.drama_pool_item_id,"
        "q.drama_pool_created_at,q.episode_number,q.episode_key,q.name_tag,"
        "q.account_username,q.page_name,q.page_id,q.material_id,q.material_name,q.content_id,"
        "q.material_language,q.drama_name,q.tag,q.candidate_rank,q.spend,"
        "q.media_repair_trigger_code,q.media_repair_job_key,q.media_repair_profile,"
        "q.facebook_violation_count,q.tiktok_violation_count,q.twitter_violation_count,"
        "q.resource_audit_count,q.dangerous_tag_count,q.status AS queue_status,"
        "l.id AS log_id," + visible_status + " AS status,COALESCE(l.attempt_count,0) AS attempt_count,"
        "CASE WHEN l.status IN ('post_creating','repost_creating') OR COALESCE(l.unknown_outcome,0)=1 "
        "THEN 1 ELSE 0 END AS unknown_outcome,COALESCE(l.short_url,'') AS short_url,"
        "COALESCE(l.x_post_id,'') AS post_id,COALESCE(l.x_post_url,'') AS preview_url,"
        "COALESCE(l.error_code,'') AS error_code,COALESCE(l.error_message,'') AS error_message,"
        "COALESCE(l.started_at,'') AS started_at,COALESCE(l.published_at,'') AS published_at,"
        "q.created_at,q.updated_at"
        + from_sql
    )
    offset = (page - 1) * page_size
    with (contextlib.nullcontext(connection) if connection is not None else contextlib.closing(_readonly(db_path))) as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*)" + from_sql + where,
                tuple(values),
            ).fetchone()[0]
        )
        rows = conn.execute(
            select + where + " ORDER BY q.id DESC LIMIT ? OFFSET ?",
            tuple(values) + (page_size, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = _row_dict(row)
        item["unknown_outcome"] = bool(item["unknown_outcome"])
        item["error_message"] = redact_text(item["error_message"], 500)
        items.append(_public_drama_delivery_route(item))
    return {
        "items": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
        },
    }


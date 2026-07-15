#!/usr/bin/env python3
"""Apply the execution-efficiency and ``ads_ai`` log patch to live composite code.

Production is a shared monolith, so this script changes only ad-control function
blocks and refuses ambiguous source. The feature module, runner, and static files
are copied from the same verified Git commit by the deployment procedure.
"""

import argparse
import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path


IMPORT_LINE = "from features.ad_control_execution_log import service as ad_control_execution_log_service"


def replace_once(text, old, new, label):
    if new in text:
        print("%s: already applied" % label)
        return text, False
    count = text.count(old)
    if count != 1:
        raise RuntimeError("%s: expected one source block, found %s" % (label, count))
    return text.replace(old, new, 1), True


def replace_optional_once(text, old, new, label):
    """Apply an older-baseline source block when that surface still exists."""

    if new in text:
        print("%s: already applied" % label)
        return text, False
    count = text.count(old)
    if count == 0:
        print("%s: optional target absent" % label)
        return text, False
    if count != 1:
        raise RuntimeError("%s: expected at most one source block, found %s" % (label, count))
    return text.replace(old, new, 1), True


def function_matches(text, name):
    header = r"^def %s\((?:[^\n]*\):\n|.*?^\s*\):\n)" % re.escape(name)
    pattern = re.compile(header + r".*?(?=^def |\Z)", re.M | re.S)
    return list(pattern.finditer(text))


def replace_function(text, name, new_source):
    matches = function_matches(text, name)
    if len(matches) != 1:
        raise RuntimeError("function %s: expected one match, found %s" % (name, len(matches)))
    current = matches[0].group(0).rstrip() + "\n\n\n"
    desired = new_source.rstrip() + "\n\n\n"
    if current == desired:
        print("function %s: already applied" % name)
        return text, False
    return text[: matches[0].start()] + desired + text[matches[0].end() :], True


def replace_optional_function(text, name, new_source):
    """Replace a legacy compatibility hook when the target baseline has it.

    The merged application delegates action status formatting to the feature
    service and therefore no longer defines ``ad_control_action_status``.
    Older supported baselines still define it and must receive the verified
    replacement.  Multiple definitions remain a hard failure in both cases.
    """

    matches = function_matches(text, name)
    if not matches:
        print("function %s: optional target absent" % name)
        return text, False
    if len(matches) != 1:
        raise RuntimeError("function %s: expected at most one match, found %s" % (name, len(matches)))
    return replace_function(text, name, new_source)


INTEGRATION_BLOCK = r'''
def ad_control_action_log_config():
    if not AD_CONTROL_ACTION_LOG_MYSQL_HOST or not AD_CONTROL_ACTION_LOG_MYSQL_USER:
        raise RuntimeError("ad-control ads_ai writer database is not configured")
    return {
        "host": AD_CONTROL_ACTION_LOG_MYSQL_HOST,
        "port": int(AD_CONTROL_ACTION_LOG_MYSQL_PORT or 3306),
        "user": AD_CONTROL_ACTION_LOG_MYSQL_USER,
        "password": AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD,
        "database": AD_CONTROL_ACTION_LOG_DB_NAME,
        "connect_timeout": AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT,
        "read_timeout": AD_CONTROL_ACTION_LOG_IO_TIMEOUT,
        "write_timeout": AD_CONTROL_ACTION_LOG_IO_TIMEOUT,
    }


def ad_control_local_action_log_row(action_id):
    ensure_ad_control_tables()
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute(
                """
                SELECT a.*, COALESCE(p.total_count, 0) AS preview_total_count
                  FROM ad_control_action a
                  LEFT JOIN ad_control_preview p ON p.preview_id=a.preview_id
                 WHERE a.action_id=?
                 LIMIT 1
                """,
                (str(action_id or ""),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def ad_control_persist_action_log(action_id, overrides=None):
    row = ad_control_local_action_log_row(action_id)
    if not row:
        raise StructuredApiError("action_not_found", "执行日志不存在")
    overrides = dict(overrides or {})
    criteria = ad_control_safe_json_dict(row.get("criteria_json"))
    raw_results = ad_control_safe_json_list(row.get("results_json"))
    results = []
    for item in raw_results:
        if item.get("status") == "error" and "retryable" not in item:
            item = ad_control_execution_log_service.enrich_error_result(item)
        results.append(item)
    requested_value = row.get("requested_count")
    requested_count = int(requested_value if requested_value is not None else len(results))
    if "matched_count" in overrides:
        matched_value = overrides.get("matched_count")
    elif "execution_target_count" in criteria:
        matched_value = criteria.get("execution_target_count")
    elif "matched_count" in criteria:
        matched_value = criteria.get("matched_count")
    else:
        matched_value = requested_count
    matched_count = int(matched_value or 0)
    preview_error_count = int(criteria.get("preview_error_count") or 0)
    summary = ad_control_execution_log_service.execution_summary(
        results,
        matched_count=matched_count,
        requested_count=requested_count,
        preview_error_count=preview_error_count,
    )
    summary.update({key: value for key, value in overrides.items() if value is not None})
    actor_user_id = str(row.get("actor_user_id") or "")
    binding_id = str(criteria.get("binding_id") or criteria.get("rule_group_id") or "")
    record = {
        "action_id": row.get("action_id") or "",
        "preview_id": row.get("preview_id") or "",
        "binding_id": binding_id,
        "rule_id": row.get("rule_id") or criteria.get("rule_id") or "",
        "event_key": summary.get("event_key") or criteria.get("runner_event_key") or "",
        "source_type": summary.get("source_type") or ("scheduled" if actor_user_id == "ad_control_rule_runner" else "api"),
        "actor_user_id": actor_user_id,
        "product": row.get("product") or criteria.get("product") or "",
        "action": row.get("action") or criteria.get("action") or "",
        "object_level": row.get("level") or criteria.get("level") or "campaign",
        "run_status": (
            overrides.get("run_status")
            if "run_status" in overrides
            else criteria.get("runner_status") or summary.get("run_status") or ""
        ),
        "runner_reason": (
            overrides.get("runner_reason")
            if "runner_reason" in overrides
            else criteria.get("runner_reason") or summary.get("runner_reason") or ""
        ),
        "dry_run": int(row.get("dry_run") or 0),
        "scanned_count": int(criteria.get("scan_count") or row.get("preview_total_count") or 0),
        "candidate_count": int(criteria.get("candidate_count") or 0),
        "matched_count": matched_count,
        "batch_planned_count": int(criteria.get("execution_batch_count") or requested_count),
        "deferred_count": int(summary.get("deferred_count") or 0),
        "requested_count": requested_count,
        "success_count": int(row.get("success_count") or 0),
        "skipped_count": int(row.get("skipped_count") or 0),
        "error_count": int(row.get("error_count") or 0),
        "retryable_error_count": int(summary.get("retryable_error_count") or 0),
        "blocked_count": int(summary.get("blocked_count") or 0),
        "remaining_count": int(summary.get("remaining_count") or 0),
        "criteria": criteria,
        "results": results,
        "created_at": row.get("created_at") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "log_version": 2 if "scan_count" in criteria else 1,
    }
    return ad_control_execution_log_service.upsert_action(
        ad_control_action_log_config(), record, AD_CONTROL_ACTION_LOG_TABLE
    )


def ad_control_update_action_log_runner(action_id, event_key, status, reason, remaining_count):
    action_id = str(action_id or "").strip()
    if not action_id:
        return 0
    config = ad_control_action_log_config()
    try:
        updated = ad_control_execution_log_service.update_runner_status(
            config,
            action_id,
            event_key,
            status,
            reason,
            remaining_count,
            AD_CONTROL_ACTION_LOG_TABLE,
        )
        if updated:
            return updated
    except Exception:
        logging.exception("failed to update ads_ai ad-control runner status; retrying upsert")
    ad_control_persist_action_log(action_id, {
        "event_key": event_key,
        "source_type": "scheduled",
        "run_status": status,
        "runner_reason": reason,
        "remaining_count": remaining_count,
    })
    return 1


def ad_control_action_log_utc_bound(value, end=False):
    value = str(value or "").strip()
    if not value:
        return ""
    local_dt = datetime.strptime(value[:10], "%Y-%m-%d")
    if end:
        local_dt += timedelta(days=1, seconds=-1)
    return (local_dt - timedelta(hours=AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS)).strftime("%Y-%m-%d %H:%M:%S")


def ad_control_mysql_action_items(
    limit, product="", binding_id="", action="", date_from="", date_to="",
    owned_binding_ids=None, legacy_actor_user_id="", owner_filter=False,
):

    return ad_control_execution_log_service.list_actions(
        ad_control_action_log_config(),
        {
            "product": product,
            "binding_id": binding_id,
            "action": action,
            "date_from": ad_control_action_log_utc_bound(date_from),
            "date_to": ad_control_action_log_utc_bound(date_to, end=True),
            "owned_binding_ids": list(owned_binding_ids or []),
            "legacy_actor_user_id": legacy_actor_user_id,
            "owner_filter": bool(owner_filter),
        },
        limit=limit,
        table=AD_CONTROL_ACTION_LOG_TABLE,
    )


def ad_control_mysql_action(action_id):
    return ad_control_execution_log_service.fetch_action(
        ad_control_action_log_config(), action_id, AD_CONTROL_ACTION_LOG_TABLE
    )
'''.strip()


ACTION_STATUS_FUNCTION = r'''
def ad_control_action_status(item):
    criteria = item.get("criteria") or {}
    execution_summary = criteria.get("execution_summary") or {}
    run_status = str(item.get("run_status") or criteria.get("runner_status") or execution_summary.get("run_status") or "").strip().lower()
    if run_status == "partial":
        return {"key": "partial", "label": "部分完成，待续跑", "class": "warn"}
    if run_status == "blocked":
        return {"key": "blocked", "label": "执行受阻", "class": "danger"}
    if run_status == "executed":
        return {"key": "success", "label": "执行完成", "class": "ok"}
    if run_status in ("error", "failed"):
        return {"key": "failed", "label": "执行失败", "class": "danger"}
    error_count = int(item.get("error_count") or 0)
    success_count = int(item.get("success_count") or 0)
    dry_run = bool(item.get("dry_run"))
    if error_count > 0:
        return {"key": "failed", "label": "失败", "class": "danger"}
    if dry_run and success_count > 0:
        return {"key": "dry_run_ok", "label": "Dry-run 通过", "class": "warn"}
    if success_count > 0:
        return {"key": "success", "label": "成功", "class": "ok"}
    return {"key": "noop", "label": "无执行目标", "class": "warn"}
'''.strip()


LIST_ACTIONS_FUNCTION = r'''
def list_ad_control_actions(
    limit=50, product="", binding_id="", action="", date_from="", date_to="",
    include_targets=False, owner_user_id=None, internal=False,
):
    ensure_ad_control_tables()
    limit = ad_control_int(limit, 50, 1, 200)
    product = str(product or "").strip()
    binding_id = str(binding_id or "").strip()
    action = str(action or "").strip()
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    owner_user_id = str(owner_user_id or "").strip()
    if not internal and not owner_user_id:
        raise StructuredApiError("missing_owner", "current user is required")
    owned_group_ids = set()
    if not internal:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                owned_group_ids = {
                    str(row[0] or "")
                    for row in conn.execute(
                        "SELECT group_id FROM ad_control_rule_group "
                        "WHERE COALESCE(NULLIF(owner_user_id,''),created_by)=?",
                        (owner_user_id,),
                    ).fetchall()
                }
            finally:
                conn.close()
        if binding_id and binding_id not in owned_group_ids:
            raise StructuredApiError("not_found", "rule group not found")
    date_from_utc = ad_control_action_log_utc_bound(date_from)
    date_to_utc = ad_control_action_log_utc_bound(date_to, end=True)
    mysql_items = []
    mysql_available = False
    storage_error = ""
    try:
        mysql_items = ad_control_mysql_action_items(
            limit, product, binding_id, action, date_from, date_to,
            owned_binding_ids=owned_group_ids,
            legacy_actor_user_id=owner_user_id,
            owner_filter=not internal,
        )
        mysql_available = True
    except Exception as exc:
        storage_error = str(exc)
        logging.exception("failed to list ads_ai ad-control action logs")
    where = []
    params = []
    if product:
        where.append("product=?")
        params.append(product)
    if action:
        where.append("action=?")
        params.append(action)
    if date_from_utc:
        where.append("created_at>=?")
        params.append(date_from_utc)
    if date_to_utc:
        where.append("created_at<=?")
        params.append(date_to_utc)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sqlite_items = []
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            offset = 0
            batch_size = max(200, min(1000, limit * 10))
            while len(sqlite_items) < limit:
                rows = conn.execute(
                    """SELECT action_id, preview_id, actor_user_id, action, level, product,
                              criteria_json, requested_count, success_count, skipped_count,
                              error_count, dry_run, results_json, created_at
                         FROM ad_control_action %s
                     ORDER BY created_at DESC, action_id DESC LIMIT ? OFFSET ?""" % where_sql,
                    tuple(params + [batch_size, offset]),
                ).fetchall()
                if not rows:
                    break
                offset += len(rows)
                for row in rows:
                    item = dict(row)
                    item["criteria"] = ad_control_safe_json_dict(item.pop("criteria_json", "{}"))
                    item["results"] = ad_control_safe_json_list(item.pop("results_json", "[]"))
                    item["dry_run"] = bool(item.get("dry_run"))
                    item["binding_id"] = item["criteria"].get("binding_id") or item["criteria"].get("rule_group_id") or ""
                    item["log_store"] = "sqlite_fallback"
                    if not internal:
                        if item["binding_id"]:
                            if item["binding_id"] not in owned_group_ids:
                                continue
                        elif str(item.get("actor_user_id") or "") != owner_user_id:
                            continue
                    if binding_id and item["binding_id"] != binding_id:
                        continue
                    sqlite_items.append(item)
                    if len(sqlite_items) >= limit:
                        break
                if len(rows) < batch_size:
                    break
        finally:
            conn.close()
    merged = {}
    for item in mysql_items + sqlite_items:
        action_id = str(item.get("action_id") or "")
        if action_id and action_id not in merged:
            merged[action_id] = item
    items = sorted(
        merged.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True
    )[:limit]
    group_ids = sorted({
        str(
            (item.get("criteria") or {}).get("rule_group_id")
            or (item.get("criteria") or {}).get("binding_id")
            or item.get("binding_id")
            or ""
        ).strip()
        for item in items
        if str(
            (item.get("criteria") or {}).get("rule_group_id")
            or (item.get("criteria") or {}).get("binding_id")
            or item.get("binding_id")
            or ""
        ).strip()
    })
    rule_map = {}
    if group_ids:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                placeholders = ",".join(["?"] * len(group_ids))
                rows = conn.execute(
                    "SELECT group_id,name,rule_set_id,account_group_id "
                    "FROM ad_control_rule_group WHERE group_id IN (%s)" % placeholders,
                    tuple(group_ids),
                ).fetchall()
                rule_map = {str(row["group_id"] or ""): dict(row) for row in rows}
            finally:
                conn.close()
    include_targets = bool(include_targets)
    for item in items:
        criteria = item.get("criteria") or {}
        results = item.get("results") or []
        execution_summary = criteria.get("execution_summary") or {}
        group_id = str(
            criteria.get("rule_group_id")
            or criteria.get("binding_id")
            or item.get("binding_id")
            or ""
        ).strip()
        rule_group = rule_map.get(group_id) or {}
        warning_counts = {}
        for result in results:
            for warning in result.get("warnings") or []:
                warning_text = str(warning or "").strip()
                if warning_text:
                    warning_counts[warning_text] = warning_counts.get(warning_text, 0) + 1
        warning_summary = [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                warning_counts.items(), key=lambda value: (-value[1], value[0])
            )[:8]
        ]
        reason_summary = item.get("reason_summary") or ad_control_execution_log_service.reason_summary(results)
        run_status = str(
            item.get("run_status")
            or criteria.get("runner_status")
            or execution_summary.get("run_status")
            or ""
        ).strip().lower()
        status = {
            "partial": {"key": "partial", "label": "部分完成，待续跑", "class": "warn"},
            "blocked": {"key": "blocked", "label": "执行受阻", "class": "danger"},
            "executed": {"key": "success", "label": "执行完成", "class": "ok"},
            "error": {"key": "failed", "label": "执行失败", "class": "danger"},
            "failed": {"key": "failed", "label": "执行失败", "class": "danger"},
        }.get(run_status)
        if not status:
            if int(item.get("error_count") or 0) > 0:
                status = {"key": "failed", "label": "失败", "class": "danger"}
            elif item.get("dry_run") and int(item.get("success_count") or 0) > 0:
                status = {"key": "dry_run_ok", "label": "Dry-run 通过", "class": "warn"}
            elif int(item.get("success_count") or 0) > 0:
                status = {"key": "success", "label": "成功", "class": "ok"}
            else:
                status = {"key": "noop", "label": "无执行目标", "class": "warn"}
        item["audit"] = {
            "status": status,
            "created_at_local": str(item.get("created_at") or ""),
            "mode": "dry-run" if item.get("dry_run") else "real",
            "mode_label": "Dry-run 试跑" if item.get("dry_run") else "正式执行",
            "action_label": {
                "pause": "关停", "copy": "复制", "mixed": "关闭/复制",
                "reopen": "重启", "preview": "预览",
            }.get(str(item.get("action") or ""), item.get("action") or ""),
            "rule_group_id": group_id,
            "rule_group_name": rule_group.get("name") or group_id or "--",
            "rule_set_id": rule_group.get("rule_set_id") or criteria.get("rule_set_id") or "",
            "account_group_id": rule_group.get("account_group_id") or criteria.get("account_group_id") or "",
            "counts": {
                "requested": int(item.get("requested_count") or 0),
                "success": int(item.get("success_count") or 0),
                "skipped": int(item.get("skipped_count") or 0),
                "error": int(item.get("error_count") or 0),
            },
            "flow": {
                "scanned": int(item.get("scanned_count") or criteria.get("scan_count") or 0),
                "candidate": int(item.get("candidate_count") or criteria.get("candidate_count") or 0),
                "matched": int(item.get("matched_count") or criteria.get("execution_target_count") or 0),
                "batch_planned": int(item.get("batch_planned_count") or criteria.get("execution_batch_count") or item.get("requested_count") or 0),
                "deferred": int(item.get("deferred_count") or 0),
                "remaining": int(item.get("remaining_count") or 0),
                "retryable": int(item.get("retryable_error_count") or 0),
                "blocked": int(item.get("blocked_count") or 0),
            },
            "reason_summary": reason_summary,
            "warning_summary": warning_summary,
            "samples": [],
            "raw_result_count": len(results),
            "log_store": item.get("log_store") or "sqlite_fallback",
        }
        if not include_targets:
            item["results"] = []
    return {
        "items": items,
        "storage": "ads_ai" if mysql_available else "sqlite_fallback",
        "storage_error": storage_error,
    }
'''.strip()


FETCH_ACTION_FUNCTION = r'''
def fetch_ad_control_action(action_id, owner_user_id=None, internal=False):
    action_id = str(action_id or "").strip()
    if not action_id:
        raise StructuredApiError("missing_action_id", "缺少 action_id")
    owner_user_id = str(owner_user_id or "").strip()
    if not internal and not owner_user_id:
        raise StructuredApiError("missing_owner", "current user is required")
    owned_group_ids = set()
    if not internal:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                owned_group_ids = {
                    str(row[0] or "")
                    for row in conn.execute(
                        "SELECT group_id FROM ad_control_rule_group "
                        "WHERE COALESCE(NULLIF(owner_user_id,''),created_by)=?",
                        (owner_user_id,),
                    ).fetchall()
                }
            finally:
                conn.close()

    def visible(item):
        if internal:
            return True
        group_ref = str(item.get("binding_id") or (item.get("criteria") or {}).get("rule_group_id") or "")
        if group_ref:
            return group_ref in owned_group_ids
        return str(item.get("actor_user_id") or "") == owner_user_id

    try:
        item = ad_control_mysql_action(action_id)
        if item:
            if visible(item):
                return item
            raise StructuredApiError("action_not_found", "执行日志不存在")
    except StructuredApiError:
        raise
    except Exception:
        logging.exception("failed to fetch ads_ai ad-control action log action_id=%s", action_id)
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            row = conn.execute("SELECT * FROM ad_control_action WHERE action_id=?", (action_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        raise StructuredApiError("action_not_found", "执行日志不存在")
    item = dict(row)
    item["criteria"] = ad_control_safe_json_dict(item.pop("criteria_json", "{}"))
    item["results"] = ad_control_safe_json_list(item.pop("results_json", "[]"))
    item["dry_run"] = bool(item.get("dry_run"))
    item["binding_id"] = item["criteria"].get("binding_id") or item["criteria"].get("rule_group_id") or ""
    item["log_store"] = "sqlite_fallback"
    if not visible(item):
        raise StructuredApiError("action_not_found", "执行日志不存在")
    return item
'''.strip()


EXECUTE_LIVE_FUNCTION = r'''
def execute_ad_control_live(payload, session):
    ensure_ad_control_tables()
    preview = fetch_ad_control_preview(payload.get("preview_id"))
    if str(preview.get("actor_user_id") or "") != ad_control_actor(session):
        raise StructuredApiError("not_found", "preview not found")
    criteria = ad_control_safe_json_dict(preview.get("criteria_json"))
    if criteria.get("mode") != "live":
        raise StructuredApiError("invalid_preview", "preview is not a live preview")
    expected_hash = str(criteria.get("preview_hash") or "").strip()
    confirmed_hash = str(payload.get("preview_hash") or "").strip()
    if not expected_hash or confirmed_hash != expected_hash:
        raise StructuredApiError("preview_hash_mismatch", "preview hash confirmation is required")
    requested_group_id = str(payload.get("rule_group_id") or "").strip()
    preview_group_id = str(criteria.get("rule_group_id") or criteria.get("binding_id") or "").strip()
    if requested_group_id and requested_group_id != preview_group_id:
        raise StructuredApiError("preview_group_mismatch", "preview does not belong to this rule group")
    dry_run = bool(payload.get("dry_run", True))
    run_mode = str(criteria.get("run_mode") or "live").lower()
    if str(criteria.get("object_level") or "campaign").lower() == "ad":
        raise StructuredApiError("phase_not_enabled", "Ad copy phase is not enabled")
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            ad_control_validate_live_preview_group(conn, preview, criteria, require_enabled=False)
        finally:
            conn.close()
    items = ad_control_safe_json_list(preview.get("sample_json"))[:AD_CONTROL_MAX_LIVE_EXECUTE]
    has_copy = any(str(item.get("target_action") or "").lower() == "copy" for item in items)
    accepted_confirmations = {"EXECUTE_LIVE_PAUSE"}
    if has_copy:
        accepted_confirmations.add("EXECUTE_LIVE_RULE_GROUP")
    if run_mode == "live" and not dry_run and str(payload.get("confirm") or "") not in accepted_confirmations:
        raise StructuredApiError("confirm_required", "explicit confirmation required")
    if run_mode == "live" and not dry_run:
        with JOB_DB_LOCK:
            conn = get_job_db_connection()
            try:
                ad_control_validate_live_preview_group(conn, preview, criteria, require_enabled=True)
            finally:
                conn.close()
    action_id = uuid.uuid4().hex
    pre_results = []
    pause_items = []
    for item in items:
        target_action = str(item.get("target_action") or "").lower()
        base = {
            "object_key": item.get("object_key") or "",
            "object_id": item.get("object_id") or item.get("campaign_id") or item.get("ad_id") or "",
            "account_id": ad_control_normalize_account(item.get("account_id")),
            "campaign_id": str(item.get("campaign_id") or item.get("object_id") or ""),
            "campaign_name": item.get("campaign_name") or "",
            "target_action": target_action,
            "target_rule_id": item.get("target_rule_id") or "",
        }
        if run_mode != "live":
            pre_results.append(dict(
                base,
                status="observed",
                reason="would_%s" % target_action if target_action in ("pause", "copy") else "observe_mode",
            ))
        elif target_action == "copy":
            pre_results.append(dict(
                base,
                status="skipped",
                reason="phase_not_enabled" if criteria.get("object_level") == "ad" else "copy_persistence_not_configured",
            ))
        elif target_action == "pause":
            pause_items.append(item)
        else:
            pre_results.append(dict(base, status="skipped", reason="not_write_target"))

    token_configs = (
        ad_control_token_config_for_accounts(criteria.get("product"), criteria.get("accounts") or [])
        if pause_items and criteria.get("product") else {}
    )
    token_by_user = {}
    token_by_account = {}
    selected_accounts = []
    for item in pause_items:
        account_id = ad_control_normalize_account(item.get("account_id"))
        if account_id and account_id not in selected_accounts:
            selected_accounts.append(account_id)
    for account_id in selected_accounts:
        account_item = next(
            (item for item in pause_items if ad_control_normalize_account(item.get("account_id")) == account_id),
            {},
        )
        user_id = str(
            account_item.get("token_user_id")
            or (token_configs.get(account_id) or {}).get("user_id")
            or ""
        ).strip()
        if user_id and user_id not in token_by_user:
            token_by_user[user_id] = ad_control_token_for_user_id(user_id)
        token_by_account[account_id] = token_by_user.get(user_id, "")
    enforce_product_whitelist = bool(selected_accounts and criteria.get("product"))
    if enforce_product_whitelist:
        whitelist_by_account = ad_control_product_campaign_whitelist(
            criteria.get("product"), selected_accounts
        )
    else:
        whitelist_by_account = {}
    grouped = {}
    order = {}
    for index, item in enumerate(pause_items):
        account_id = ad_control_normalize_account(item.get("account_id"))
        grouped.setdefault(account_id, []).append(item)
        order[item.get("object_key") or "%s:%s" % (account_id, item.get("campaign_id"))] = index
    application_rate_limited = threading.Event()

    def deferred_result(item, account_id, reason, error_item=None):
        error_item = error_item or {}
        return {
            "object_key": item.get("object_key") or "",
            "account_id": account_id,
            "campaign_id": str(item.get("campaign_id") or item.get("object_id") or ""),
            "campaign_name": item.get("campaign_name") or "",
            "status": "deferred",
            "reason": reason,
            "retryable": True,
            "rate_limited": bool(error_item.get("rate_limited")),
            "error_code": error_item.get("error_code"),
            "error_subcode": error_item.get("error_subcode"),
        }

    def execute_account(account_id, account_items):
        account_results = []
        token = token_by_account.get(account_id) or ""
        whitelist = whitelist_by_account.get(account_id) or {}
        for item_index, item in enumerate(account_items):
            if application_rate_limited.is_set():
                for pending in account_items[item_index:]:
                    account_results.append(deferred_result(
                        pending, account_id, "deferred_after_application_rate_limit",
                        {"rate_limited": True, "error_code": 4},
                    ))
                break
            campaign_id = str(item.get("campaign_id") or item.get("object_id") or "")
            base = {
                "object_key": item.get("object_key") or "",
                "account_id": account_id,
                "campaign_id": campaign_id,
                "campaign_name": item.get("campaign_name") or "",
            }
            if item.get("skip_reason"):
                account_results.append(dict(base, status="skipped", reason=item.get("skip_reason")))
                continue
            if enforce_product_whitelist and campaign_id not in whitelist:
                account_results.append(dict(base, status="skipped", reason="outside_product_whitelist"))
                continue
            if not token:
                account_results.append(dict(base, status="skipped", reason="missing_meta_token"))
                continue
            try:
                if dry_run:
                    meta = ad_control_graph_get(token, campaign_id, "account_id,status,effective_status,name")
                    meta_account = ad_control_normalize_account(meta.get("account_id"))
                    if not meta_account or meta_account != account_id:
                        account_results.append(dict(base, status="skipped", reason="account_owner_mismatch", meta=meta))
                        continue
                    if str(meta.get("effective_status") or "").upper() != "ACTIVE":
                        account_results.append(dict(base, status="skipped", reason="not_active", meta=meta))
                        continue
                    account_results.append(dict(base, status="dry_run", meta=meta))
                    continue
                guarded = ad_control_guarded_campaign_pause(
                    preview, criteria, token, item, account_id
                )
                meta = guarded.get("meta") or {}
                if guarded.get("skip_reason"):
                    account_results.append(dict(
                        base, status="skipped", reason=guarded.get("skip_reason"), meta=meta
                    ))
                    continue
                graph_response = guarded.get("payload_result") or {}
                warnings = []
                try:
                    ad_control_save_object_state(action_id, {
                        "object_key": base["object_key"],
                        "product": criteria.get("product"),
                        "level": "campaign",
                        "account_id": account_id,
                        "object_id": campaign_id,
                        "campaign_id": campaign_id,
                    }, "paused")
                except Exception as exc:
                    logging.warning("ad control local state save failed after graph success: %s: %s", base["object_key"], exc)
                    warnings.append("local_state_save_failed: %s" % exc)
                result_item = dict(base, status="success", meta=meta, graph_response=graph_response)
                if warnings:
                    result_item["warnings"] = warnings
                account_results.append(result_item)
            except Exception as exc:
                error_item = dict(base, status="error", reason=str(exc))
                error_item.update(ad_control_execution_log_service.graph_error_details(exc))
                account_results.append(error_item)
                logging.exception("ad control live execute failed: %s", base["object_key"])
                application_limited = (
                    error_item.get("error_code") == 4
                    or error_item.get("error_subcode") == 5044001
                )
                if application_limited:
                    application_rate_limited.set()
                stop_account = (
                    bool(error_item.get("retryable"))
                    or error_item.get("error_code") in (102, 190)
                )
                if stop_account:
                    deferred_reason = (
                        "deferred_after_application_rate_limit"
                        if application_limited
                        else "deferred_after_account_error"
                    )
                    for pending in account_items[item_index + 1:]:
                        account_results.append(deferred_result(
                            pending, account_id, deferred_reason, error_item
                        ))
                    break
        return account_results

    results = list(pre_results)
    workers = min(max(1, AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS), max(1, len(grouped)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(execute_account, account_id, account_items): account_id
            for account_id, account_items in grouped.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            account_id = future_map[future]
            try:
                results.extend(future.result())
            except Exception as exc:
                logging.exception("ad control account execution worker failed: %s", account_id)
                for item in grouped.get(account_id) or []:
                    error_item = {
                        "object_key": item.get("object_key") or "",
                        "account_id": account_id,
                        "campaign_id": item.get("campaign_id") or item.get("object_id") or "",
                        "campaign_name": item.get("campaign_name") or "",
                        "status": "error",
                        "reason": str(exc),
                    }
                    error_item.update(ad_control_execution_log_service.graph_error_details(exc))
                    results.append(error_item)
    results.sort(key=lambda item: order.get(item.get("object_key") or "%s:%s" % (item.get("account_id"), item.get("campaign_id")), 10 ** 9))
    success_count = len([item for item in results if item.get("status") in ("success", "dry_run")])
    skipped_count = len([item for item in results if item.get("status") in ("skipped", "observed")])
    error_count = len([item for item in results if item.get("status") == "error"])
    summary = ad_control_execution_log_service.execution_summary(
        results,
        matched_count=int(criteria.get("execution_target_count") or len(items)),
        requested_count=len(items),
        preview_error_count=int(criteria.get("preview_error_count") or 0),
    )
    action_criteria = dict(criteria)
    action_criteria["execution_summary"] = summary
    with JOB_DB_LOCK:
        conn = get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_action (
                  action_id, preview_id, actor_user_id, action, level, product, criteria_json,
                  requested_count, success_count, skipped_count, error_count, dry_run,
                  results_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    action_id,
                    preview["preview_id"],
                    ad_control_actor(session),
                    "mixed" if has_copy else "pause",
                    criteria.get("object_level") or "campaign",
                    criteria.get("product", ""),
                    json.dumps(action_criteria, ensure_ascii=False),
                    len(items),
                    success_count,
                    skipped_count,
                    error_count,
                    1 if dry_run else 0,
                    json.dumps(results, ensure_ascii=False),
                ),
            )
            if criteria.get("rule_group_id"):
                conn.execute(
                    """
                    UPDATE ad_control_rule_group
                       SET last_run_at=CURRENT_TIMESTAMP, last_result_json=?, updated_at=CURRENT_TIMESTAMP
                     WHERE group_id=?
                    """,
                    (
                        json.dumps({
                            "action_id": action_id,
                            "success_count": success_count,
                            "skipped_count": skipped_count,
                            "error_count": error_count,
                            "dry_run": dry_run,
                            "remaining_count": summary.get("remaining_count", 0),
                        }, ensure_ascii=False),
                        criteria.get("rule_group_id"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    log_store = "ads_ai"
    log_store_error = ""
    try:
        ad_control_persist_action_log(action_id, summary)
    except Exception as exc:
        log_store = "sqlite_fallback"
        log_store_error = str(exc)
        logging.exception("failed to persist ad-control action to ads_ai action_id=%s", action_id)
    return {
        "action_id": action_id,
        "preview_id": preview["preview_id"],
        "dry_run": dry_run,
        "requested_count": len(items),
        "success_count": success_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "results": results[:200],
        "log_store": log_store,
        "log_store_error": log_store_error,
        **summary,
    }
'''.strip()


def patch_app_text(text):
    changed = False
    if IMPORT_LINE not in text:
        text, block_changed = replace_once(
            text,
            "AD_CONTROL_DB_NAME = os.environ.get(\"AD_CONTROL_DB_NAME\", DB_NAME).strip() or \"kunlunads_dev\"",
            IMPORT_LINE + "\n\n\nAD_CONTROL_DB_NAME = os.environ.get(\"AD_CONTROL_DB_NAME\", DB_NAME).strip() or \"kunlunads_dev\"",
            "feature import",
        )
        changed = changed or block_changed
    constants_old = 'AD_CONTROL_MAX_LIVE_EXECUTE = int(os.environ.get("AD_CONTROL_MAX_LIVE_EXECUTE", "200"))\n'
    constants_new = constants_old + (
        'AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT = int(os.environ.get("AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT", "20"))\n'
        'AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS", "4"))\n'
        'AD_CONTROL_ACTION_LOG_DB_NAME = os.environ.get("AD_CONTROL_ACTION_LOG_DB_NAME", "ads_ai").strip() or "ads_ai"\n'
        'AD_CONTROL_ACTION_LOG_TABLE = os.environ.get("AD_CONTROL_ACTION_LOG_TABLE", "ad_control_action_log").strip() or "ad_control_action_log"\n'
        'AD_CONTROL_ACTION_LOG_MYSQL_HOST = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_HOST", MYSQL_HOST).strip()\n'
        'AD_CONTROL_ACTION_LOG_MYSQL_PORT = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PORT", MYSQL_PORT).strip()\n'
        'AD_CONTROL_ACTION_LOG_MYSQL_USER = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_USER", MYSQL_USER).strip()\n'
        'AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD = os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PASSWORD", MYSQL_PASSWORD)\n'
        'AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "5"))\n'
        'AD_CONTROL_ACTION_LOG_IO_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_IO_TIMEOUT", "8"))\n'
        'AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS = int(os.environ.get("AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS", "8"))\n'
    )
    text, block_changed = replace_once(text, constants_old, constants_new, "ad-control constants")
    changed = changed or block_changed
    if "def ad_control_action_log_config():" not in text:
        marker = "def ad_control_resource_snapshot():"
        if text.count(marker) != 1:
            raise RuntimeError("integration marker count=%s" % text.count(marker))
        text = text.replace(marker, INTEGRATION_BLOCK + "\n\n\n" + marker, 1)
        changed = True
    if "ad_control_execution_log_service.balanced_execution_items(" not in text:
        preview_variants = [
            (
                '''    pause_items.sort(key=lambda item: (\n        ad_control_normalize_account(item.get("account_id")),\n        str(item.get("campaign_id") or item.get("object_id") or ""),\n    ))\n    pause_count = len(pause_items)\n    execution_items = pause_items[:AD_CONTROL_MAX_LIVE_EXECUTE]\n''',
                '''    pause_items.sort(key=lambda item: (\n        ad_control_normalize_account(item.get("account_id")),\n        str(item.get("campaign_id") or item.get("object_id") or ""),\n    ))\n    pause_count = len(pause_items)\n    execution_items = ad_control_execution_log_service.balanced_execution_items(\n        pause_items,\n        max_total=AD_CONTROL_MAX_LIVE_EXECUTE,\n        max_per_account=AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT,\n    )\n''',
            ),
            (
                "    execution_items = action_items[:AD_CONTROL_MAX_LIVE_EXECUTE]\n",
                '''    execution_items = ad_control_execution_log_service.balanced_execution_items(\n        action_items,\n        max_total=AD_CONTROL_MAX_LIVE_EXECUTE,\n        max_per_account=AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT,\n    )\n''',
            ),
        ]
        matches = [(old, new) for old, new in preview_variants if text.count(old) == 1]
        if len(matches) != 1:
            raise RuntimeError("balanced preview batch source block count=%s" % len(matches))
        text = text.replace(matches[0][0], matches[0][1], 1)
        changed = True
    if '"preview_error_count": len(errors),' not in text:
        criteria_variants = [
            '''        "execution_truncated": pause_count > len(execution_items),\n''',
            '''        "execution_truncated": pause_count + copy_count > len(execution_items),\n''',
        ]
        matches = [value for value in criteria_variants if text.count(value) == 1]
        if len(matches) != 1:
            raise RuntimeError("preview flow metadata source block count=%s" % len(matches))
        criteria_old = matches[0]
        criteria_new = criteria_old + '''        "scan_count": sum(int(result.get("active_count") or 0) for result in account_results),\n        "candidate_count": sum(int(result.get("candidate_count") or 0) for result in account_results),\n        "preview_error_count": len(errors),\n        "max_per_account": AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT,\n'''
        text = text.replace(criteria_old, criteria_new, 1)
        changed = True
    preview_response_old = '''        "total": total,\n        "pause_count": pause_count,\n'''
    preview_response_new = '''        "total": total,\n        "scan_count": sum(int(result.get("active_count") or 0) for result in account_results),\n        "candidate_count": sum(int(result.get("candidate_count") or 0) for result in account_results),\n        "pause_count": pause_count,\n'''
    text, block_changed = replace_once(
        text, preview_response_old, preview_response_new, "preview response flow metadata"
    )
    changed = changed or block_changed
    text, block_changed = replace_optional_function(
        text, "ad_control_action_status", ACTION_STATUS_FUNCTION
    )
    changed = changed or block_changed
    text, block_changed = replace_function(text, "list_ad_control_actions", LIST_ACTIONS_FUNCTION)
    changed = changed or block_changed
    text, block_changed = replace_optional_function(
        text, "fetch_ad_control_action", FETCH_ACTION_FUNCTION
    )
    changed = changed or block_changed
    text, block_changed = replace_function(text, "execute_ad_control_live", EXECUTE_LIVE_FUNCTION)
    changed = changed or block_changed
    audit_old = '''        "counts": {\n            "requested": int(item.get("requested_count") or 0),\n            "success": int(item.get("success_count") or 0),\n            "skipped": int(item.get("skipped_count") or 0),\n            "error": int(item.get("error_count") or 0),\n        },\n        "reason_summary": reason_summary,\n'''
    audit_new = '''        "counts": {\n            "requested": int(item.get("requested_count") or 0),\n            "success": int(item.get("success_count") or 0),\n            "skipped": int(item.get("skipped_count") or 0),\n            "error": int(item.get("error_count") or 0),\n        },\n        "flow": {\n            "scanned": int(item.get("scanned_count") or criteria.get("scan_count") or 0),\n            "candidate": int(item.get("candidate_count") or criteria.get("candidate_count") or 0),\n            "matched": int(item.get("matched_count") or criteria.get("execution_target_count") or 0),\n            "batch_planned": int(item.get("batch_planned_count") or criteria.get("execution_batch_count") or item.get("requested_count") or 0),\n            "deferred": int(item.get("deferred_count") or 0),\n            "remaining": int(item.get("remaining_count") or 0),\n            "retryable": int(item.get("retryable_error_count") or 0),\n            "blocked": int(item.get("blocked_count") or 0),\n        },\n        "log_store": item.get("log_store") or "sqlite_fallback",\n        "reason_summary": reason_summary,\n'''
    text, block_changed = replace_optional_once(
        text, audit_old, audit_new, "audit flow fields"
    )
    changed = changed or block_changed
    return text, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/root/drama_material_service")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()
    path = Path(args.root) / "app.py"
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8").replace("\r\n", "\n")
    updated, changed = patch_app_text(original)
    if changed and not args.check:
        digest = hashlib.sha256(original_bytes).hexdigest()
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_root = Path(args.backup_dir) if args.backup_dir else path.parent / "deploy_backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        os.chmod(str(backup_root), 0o700)
        backup_path = backup_root / ("app.py.before-execution-log-%s-%s" % (stamp, digest[:12]))
        shutil.copy2(str(path), str(backup_path))
        os.chmod(str(backup_path), 0o600)
        if hashlib.sha256(backup_path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("backup checksum mismatch: %s" % backup_path)
        temp_path = path.with_name(path.name + ".execution-log.tmp")
        temp_path.write_bytes(updated.encode("utf-8"))
        os.chmod(str(temp_path), path.stat().st_mode)
        os.replace(str(temp_path), str(path))
        print("backup: %s sha256=%s" % (backup_path, digest))
    print("%s: %s" % (path, "would change" if args.check and changed else "changed" if changed else "unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

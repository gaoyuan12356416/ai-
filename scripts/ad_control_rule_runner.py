#!/usr/bin/env python3
"""Run enabled product ad-control rules.

Rules are disabled by default in the UI. This runner only executes rules that
were explicitly enabled in the ad control center.
"""

import fcntl
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The host-wide mysql wrapper injects MAX_EXECUTION_TIME, but the current
# business database does not support that MySQL session variable. The runner
# already serializes each invocation and the application opens a fresh mysql
# client per query, so bypass only that incompatible wrapper behavior here.
os.environ.setdefault("MYSQL_QUERY_GUARD_BYPASS", "1")
os.environ.setdefault("AD_CONTROL_LIVE_MAX_WORKERS", "4")

import app  # noqa: E402
from features.ad_control_execution_log import service as execution_log_service  # noqa: E402


LOG_PATH = os.environ.get("AD_CONTROL_RUNNER_LOG", "/var/log/ad_control_rule_runner.log")
LOCK_PATH = os.environ.get("AD_CONTROL_RUNNER_LOCK", "/tmp/ad_control_rule_runner.lock")
WINDOW_MINUTES = max(1, int(os.environ.get("AD_CONTROL_RUNNER_WINDOW_MINUTES", "10")))
MAX_ITEMS = max(1, int(os.environ.get("AD_CONTROL_RUNNER_MAX_ITEMS", str(app.AD_CONTROL_MAX_EXECUTE))))
MAX_CONTINUATIONS = max(1, int(os.environ.get("AD_CONTROL_RUNNER_MAX_CONTINUATIONS", "24")))


def configure_logging():
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def parse_hhmm(value):
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def fixed_offset_timezone(value):
    text = str(value or "").strip()
    if not text or text.lower() == "account":
        return None
    match = re.match(r"^(?:UTC|GMT)?\s*([+-]?\d{1,2})(?::?(\d{2}))?$", text, re.I)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if abs(hours) > 14 or minutes > 59:
        return None
    sign = 1 if hours >= 0 else -1
    return timezone(timedelta(hours=hours, minutes=sign * minutes))


def now_for_timezone(tz_name):
    if str(tz_name or "").strip().lower() == "account":
        # This timestamp only forms a five-minute runner tick key. Actual due
        # evaluation happens per account in its Meta account timezone.
        return datetime.now(timezone.utc).replace(tzinfo=None), "account"
    fixed = fixed_offset_timezone(tz_name)
    if fixed:
        return datetime.now(fixed).replace(tzinfo=None), str(tz_name)
    if tz_name and str(tz_name).lower() != "account":
        try:
            import pytz

            tz = pytz.timezone(str(tz_name))
            return datetime.now(tz).replace(tzinfo=None), str(tz_name)
        except Exception:
            logging.warning("unknown timezone %s, falling back to server local time", tz_name)
    return datetime.now(), "server"


def event_due(now, hhmm):
    parsed = parse_hhmm(hhmm)
    if not parsed:
        return False, ""
    target = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    due = target <= now < target + timedelta(minutes=WINDOW_MINUTES)
    key = "%s %02d:%02d" % (now.strftime("%Y-%m-%d"), parsed[0], parsed[1])
    return due, key


def load_last_result(rule):
    payload = rule.get("last_result") or {}
    return payload if isinstance(payload, dict) else {}


def update_rule_result(rule_id, payload):
    with app.JOB_DB_LOCK:
        conn = app.get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_control_rule SET last_run_at=CURRENT_TIMESTAMP, last_result_json=?, updated_at=CURRENT_TIMESTAMP WHERE rule_id=?",
                (json.dumps(payload, ensure_ascii=False), rule_id),
            )
            conn.commit()
        finally:
            conn.close()


def update_rule_group_result(group_id, payload):
    with app.JOB_DB_LOCK:
        conn = app.get_job_db_connection()
        try:
            conn.execute(
                "UPDATE ad_control_rule_group SET last_run_at=CURRENT_TIMESTAMP, last_result_json=?, updated_at=CURRENT_TIMESTAMP WHERE group_id=?",
                (json.dumps(payload, ensure_ascii=False), group_id),
            )
            conn.commit()
        finally:
            conn.close()


def event_payload(rule, action, event_key, status, result=None, reason=""):
    return {
        "rule_id": rule.get("rule_id"),
        "rule_group_id": rule.get("group_id"),
        "product": rule.get("product"),
        "action": action,
        "event_key": event_key,
        "status": status,
        "reason": reason,
        "result": result or {},
        "finished_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }


def execution_has_errors(payload):
    result = payload.get("result") or {}
    try:
        return int(result.get("error_count") or 0) > 0
    except Exception:
        return False


def compact_event(payload):
    result = payload.get("result") or {}
    return {
        "rule_id": payload.get("rule_id") or "",
        "rule_group_id": payload.get("rule_group_id") or "",
        "product": payload.get("product") or "",
        "action": payload.get("action") or "",
        "event_key": payload.get("event_key") or "",
        "status": payload.get("status") or "",
        "reason": payload.get("reason") or "",
        "action_id": result.get("action_id") or "",
        "preview_id": result.get("preview_id") or "",
        "dry_run": bool(result.get("dry_run")),
        "requested_count": int(result.get("requested_count") or 0),
        "success_count": int(result.get("success_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "error_count": int(result.get("error_count") or 0),
        "matched_count": int(result.get("preview_pause_count") or result.get("matched_count") or 0),
        "remaining_count": int(result.get("remaining_target_count") or result.get("remaining_count") or 0),
        "retryable_error_count": int(result.get("retryable_error_count") or 0),
        "blocked_count": int(result.get("blocked_count") or 0),
        "continuation_attempt": int(result.get("continuation_attempt") or 0),
        "log_store": result.get("log_store") or "",
        "finished_at": payload.get("finished_at") or "",
    }


def log_event_result(payload):
    event = compact_event(payload)
    logging.info(
        "ad control event result rule_group_id=%s rule_id=%s product=%s action=%s event_key=%s status=%s reason=%s action_id=%s preview_id=%s requested=%s success=%s skipped=%s error=%s remaining=%s retryable=%s blocked=%s attempt=%s log_store=%s dry_run=%s",
        event["rule_group_id"], event["rule_id"], event["product"], event["action"],
        event["event_key"], event["status"], event["reason"], event["action_id"],
        event["preview_id"], event["requested_count"], event["success_count"],
        event["skipped_count"], event["error_count"], event["remaining_count"],
        event["retryable_error_count"], event["blocked_count"],
        event["continuation_attempt"], event["log_store"], event["dry_run"],
    )


def record_rule_group_preview_failure(rule_group, preview, event_key, run_status, runner_reason):
    action_id = __import__("uuid").uuid4().hex
    errors = []
    for raw_error in preview.get("errors", [])[:100]:
        item = dict(raw_error) if isinstance(raw_error, dict) else {"reason": str(raw_error or "")}
        item["status"] = "error"
        item.update(execution_log_service.graph_error_details(item.get("reason") or item))
        errors.append(item)
    criteria = {
        "mode": "live",
        "product": rule_group.get("product") or "",
        "rule_group_id": rule_group.get("group_id") or "",
        "binding_id": rule_group.get("group_id") or "",
        "runner_event_key": event_key,
        "runner_status": run_status,
        "runner_reason": runner_reason,
        "preview_error_count": int(preview.get("error_count") or len(errors)),
    }
    with app.JOB_DB_LOCK:
        conn = app.get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_action (
                  action_id, preview_id, actor_user_id, action, level, product, criteria_json,
                  requested_count, success_count, skipped_count, error_count, dry_run,
                  results_json, created_at
                ) VALUES (?, ?, ?, 'pause', 'campaign', ?, ?, 0, 0, 0, ?, 0, ?, CURRENT_TIMESTAMP)
                """,
                (
                    action_id,
                    preview.get("preview_id") or "",
                    "ad_control_rule_runner",
                    rule_group.get("product") or "",
                    json.dumps(criteria, ensure_ascii=False),
                    int(preview.get("error_count") or len(errors)),
                    json.dumps(errors, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        app.ad_control_persist_action_log(action_id, {
            "event_key": event_key,
            "source_type": "scheduled",
            "run_status": run_status,
            "runner_reason": runner_reason,
            "remaining_count": 0,
        })
    except Exception:
        logging.exception("failed to persist preview failure to ads_ai action_id=%s", action_id)
    return action_id


def record_rule_group_verification(rule_group, preview, event_key):
    action_id = __import__("uuid").uuid4().hex
    criteria = {
        "mode": "live",
        "product": rule_group.get("product") or "",
        "rule_group_id": rule_group.get("group_id") or "",
        "binding_id": rule_group.get("group_id") or "",
        "runner_event_key": event_key,
        "runner_status": "executed",
        "runner_reason": "",
        "verification_only": True,
        "scan_count": int(preview.get("scan_count") or 0),
        "candidate_count": int(preview.get("candidate_count") or preview.get("total") or 0),
        "execution_target_count": 0,
        "execution_batch_count": 0,
        "preview_error_count": 0,
    }
    with app.JOB_DB_LOCK:
        conn = app.get_job_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO ad_control_action (
                  action_id, preview_id, actor_user_id, action, level, product, criteria_json,
                  requested_count, success_count, skipped_count, error_count, dry_run,
                  results_json, created_at
                ) VALUES (?, ?, ?, 'pause', 'campaign', ?, ?, 0, 0, 0, 0, 0, '[]', CURRENT_TIMESTAMP)
                """,
                (
                    action_id,
                    preview.get("preview_id") or "",
                    "ad_control_rule_runner",
                    rule_group.get("product") or "",
                    json.dumps(criteria, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        app.ad_control_persist_action_log(action_id, {
            "event_key": event_key,
            "source_type": "scheduled",
            "run_status": "executed",
            "runner_reason": "",
            "matched_count": 0,
            "remaining_count": 0,
        })
    except Exception:
        logging.exception("failed to persist verification to ads_ai action_id=%s", action_id)
    return action_id


def criteria_for_action(rule, action):
    criteria = dict(rule.get("criteria") or {})
    criteria["product"] = criteria.get("product") or rule.get("product")
    criteria["level"] = criteria.get("level") or rule.get("level") or "campaign"
    criteria["action"] = action
    criteria["page"] = 1
    criteria["page_size"] = min(int(criteria.get("page_size") or MAX_ITEMS), MAX_ITEMS)
    if action == "pause" and not criteria.get("statuses"):
        criteria["statuses"] = ["ACTIVE"]
    if action == "reopen":
        statuses = [str(item).upper() for item in criteria.get("statuses") or []]
        if not statuses or statuses == ["ACTIVE"]:
            criteria["statuses"] = ["PAUSED"]
    return criteria


def run_event(rule, action, event_key):
    criteria = criteria_for_action(rule, action)
    if not criteria.get("product"):
        return event_payload(rule, action, event_key, "skipped", reason="missing_product")
    session = {"user_id": "ad_control_rule_runner"}
    preview = app.create_ad_control_preview(criteria, session)
    result = app.execute_ad_control(
        {
            "preview_id": preview.get("preview_id"),
            "action": action,
            "max_items": min(MAX_ITEMS, int(criteria.get("page_size") or MAX_ITEMS)),
        },
        session,
    )
    status = "error" if int(result.get("error_count") or 0) > 0 else "executed"
    return event_payload(rule, action, event_key, status, result=result, reason="execute_errors" if status == "error" else "")


def group_schedule(rule_group):
    strategy = rule_group.get("strategy") or {}
    schedule = strategy.get("schedule") if isinstance(strategy.get("schedule"), dict) else {}
    return {
        "timezone": schedule.get("timezone") or strategy.get("timezone") or strategy.get("execute_timezone"),
        "type": schedule.get("type") or "fixed_time",
        "interval_minutes": int(schedule.get("interval_minutes") or 60),
        "close_time": schedule.get("time") or strategy.get("execute_time") or strategy.get("close_time") or strategy.get("pause_time"),
        "reopen_time": strategy.get("reopen_time") or strategy.get("restart_time"),
    }


def continuation_state(previous_event, action, event_key):
    previous_event = previous_event or {}
    same_continuation = (
        previous_event.get("status") == "partial"
        and previous_event.get("action") == action
        and previous_event.get("event_key") == event_key
    )
    previous_result = (previous_event.get("result") or {}) if same_continuation else {}
    return previous_result, int(previous_result.get("continuation_attempt") or 0) + 1


def has_ads_ai_action_log(result):
    """Only update runner state when the initial ads_ai write succeeded."""
    return str((result or {}).get("log_store") or "").strip() == "ads_ai"


def run_group_event(rule_group, action, event_key, previous_event=None):
    if action != "pause":
        return event_payload(rule_group, action, event_key, "skipped", reason="unsupported_group_action")
    session = {"user_id": "ad_control_rule_runner"}
    validate_schema = app.ad_control_validate_insight_start_schema
    schema_lock = threading.Lock()
    schema_state = {"loaded": False, "columns": None, "error": None}

    def validate_schema_once():
        # Schedule/whitelist checks happen before campaign-start lookup. Keep
        # schema I/O lazy so no-due ticks do not touch the insight database,
        # while due account workers still share one success or failure.
        with schema_lock:
            if not schema_state["loaded"]:
                try:
                    schema_state["columns"] = validate_schema()
                except Exception as exc:
                    schema_state["error"] = {
                        "structured": bool(getattr(exc, "code", "")),
                        "code": str(getattr(exc, "code", "") or ""),
                        "message": str(
                            getattr(exc, "message", "")
                            or str(exc)
                            or exc.__class__.__name__
                        ),
                        "details": dict(getattr(exc, "details", {}) or {}),
                    }
                schema_state["loaded"] = True
            if schema_state["error"] is not None:
                error = schema_state["error"]
                error_type = getattr(app, "StructuredApiError", None)
                if error["structured"] and error_type is not None:
                    raise error_type(error["code"], error["message"], **error["details"])
                raise RuntimeError(error["message"])
            return schema_state["columns"]

    app.ad_control_validate_insight_start_schema = validate_schema_once
    try:
        preview = app.create_ad_control_live_preview({
            "rule_group_id": rule_group.get("group_id"),
            "scheduled": True,
        }, session, internal=True)
    finally:
        app.ad_control_validate_insight_start_schema = validate_schema
    previous_result, continuation_attempt = continuation_state(
        previous_event, action, event_key
    )
    preview_error_count = int(preview.get("error_count") or 0)
    pause_count = int(preview.get("pause_count") or 0)
    copy_count = int(preview.get("copy_count") or 0)
    execution_count = int(preview.get("execution_count") or 0)
    scheduled_due_count = int(preview.get("scheduled_due_count") or 0)
    if (
        pause_count + copy_count == 0
        and preview_error_count == 0
        and scheduled_due_count <= 0
    ):
        return event_payload(rule_group, action, event_key, "skipped", result={
            "preview_id": preview.get("preview_id"),
            "requested_count": 0,
            "success_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "preview_pause_count": 0,
            "preview_copy_count": 0,
            "remaining_target_count": 0,
            "scheduled_due_count": 0,
            "continuation_attempt": continuation_attempt,
            "verification_only": False,
        }, reason="no_accounts_due")
    if str(rule_group.get("run_mode") or "observe").lower() != "live":
        # Persist the bounded object-level would_pause/would_copy results via
        # the normal action-audit path. execute_ad_control_live exits before
        # token lookup or any Graph write whenever the preview is observe-mode.
        observed = app.execute_ad_control_live({
            "preview_id": preview.get("preview_id"),
            "preview_hash": preview.get("preview_hash"),
            "dry_run": True,
        }, session)
        observed["preview_pause_count"] = pause_count
        observed["preview_copy_count"] = copy_count
        observed["would_pause_count"] = pause_count
        observed["would_copy_count"] = copy_count
        observed["preview_error_count"] = preview_error_count
        observed["errors"] = (preview.get("errors") or [])[:10]
        observed["remaining_target_count"] = int(
            preview.get("execution_remaining_count") or preview.get("remaining_count") or 0
        )
        observed["continuation_attempt"] = continuation_attempt
        observed["observation_only"] = True
        observe_status = "error" if preview_error_count else "executed"
        observe_reason = "observe_preview_errors" if preview_error_count else "observe_mode"
        return event_payload(
            rule_group, action, event_key, observe_status,
            result=observed, reason=observe_reason,
        )
    if pause_count + copy_count == 0 and preview_error_count == 0:
        verification_action_id = record_rule_group_verification(rule_group, preview, event_key)
        return event_payload(rule_group, action, event_key, "executed", result={
            "action_id": verification_action_id,
            "preview_id": preview.get("preview_id"),
            "requested_count": 0,
            "success_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "preview_pause_count": 0,
            "preview_copy_count": 0,
            "remaining_target_count": 0,
            "continuation_attempt": continuation_attempt,
            "verification_only": True,
        }, reason="")
    if continuation_attempt > MAX_CONTINUATIONS:
        previous_action_id = str(previous_result.get("action_id") or "")
        if previous_action_id and has_ads_ai_action_log(previous_result):
            try:
                app.ad_control_update_action_log_runner(
                    previous_action_id,
                    event_key,
                    "blocked",
                    "continuation_limit_reached",
                    pause_count + copy_count,
                )
            except Exception:
                logging.exception("failed to block ads_ai action log action_id=%s", previous_action_id)
        return event_payload(rule_group, action, event_key, "error", result={
            "action_id": previous_action_id,
            "preview_id": preview.get("preview_id"),
            "error_count": int(preview.get("error_count") or 0),
            "remaining_target_count": pause_count + copy_count,
            "continuation_attempt": continuation_attempt,
        }, reason="continuation_limit_reached")
    if preview_error_count > 0 and execution_count <= 0:
        preview_details = []
        for item in preview.get("errors", []):
            reason_value = item.get("reason") if isinstance(item, dict) else item
            preview_details.append(execution_log_service.graph_error_details(reason_value))
        retryable_preview = bool(preview_details) and all(item.get("retryable") for item in preview_details)
        status = "partial" if retryable_preview and continuation_attempt < MAX_CONTINUATIONS else "error"
        log_status = "partial" if status == "partial" else "blocked"
        reason = "live_preview_errors" if status == "partial" else "live_preview_blocked"
        action_id = record_rule_group_preview_failure(
            rule_group, preview, event_key, log_status, reason
        )
        return event_payload(rule_group, action, event_key, status, result={
            "action_id": action_id,
            "preview_id": preview.get("preview_id"),
            "error_count": preview_error_count,
            "errors": preview.get("errors", [])[:10],
            "continuation_attempt": continuation_attempt,
            "remaining_target_count": 0,
        }, reason=reason)
    result = app.execute_ad_control_live({
        "preview_id": preview.get("preview_id"),
        "preview_hash": preview.get("preview_hash"),
        "dry_run": False,
        "confirm": "EXECUTE_LIVE_RULE_GROUP" if copy_count else "EXECUTE_LIVE_PAUSE",
    }, session)
    remaining_count = int(result.get("remaining_count") or 0)
    if not remaining_count:
        remaining_count = max(0, pause_count + copy_count - int(result.get("requested_count") or 0))
        remaining_count += int(result.get("retryable_error_count") or 0)
    result["preview_pause_count"] = pause_count
    result["preview_copy_count"] = copy_count
    result["remaining_target_count"] = remaining_count
    result["continuation_attempt"] = continuation_attempt
    permanent_errors = int(result.get("permanent_error_count") or 0)
    blocked_count = int(result.get("blocked_count") or 0)
    if permanent_errors > 0 or blocked_count > 0:
        status = "error"
        reason = "live_execute_blocked"
    elif remaining_count > 0 or preview_error_count > 0:
        status = "partial"
        reason = "live_execute_partial" if remaining_count > 0 else "live_preview_partial"
    else:
        status = "partial"
        reason = "live_execute_verify_remaining"
        result["verification_required"] = True
    if has_ads_ai_action_log(result):
        try:
            app.ad_control_update_action_log_runner(
                result.get("action_id"),
                event_key,
                "blocked" if status == "error" else status,
                reason,
                remaining_count,
            )
        except Exception:
            logging.exception("failed to update ads_ai runner status action_id=%s", result.get("action_id"))
    else:
        logging.warning(
            "skip ads_ai runner status update because initial log write fell back action_id=%s",
            result.get("action_id"),
        )
    return event_payload(rule_group, action, event_key, status, result=result, reason=reason)


def group_event_continuation_key(last, action):
    event = last.get("last_event") or {}
    if (
        event.get("status") == "partial"
        and event.get("action") == action
        and event.get("event_key")
    ):
        return str(event.get("event_key"))
    return ""


def run_rule_groups():
    groups = app.list_ad_control_rule_groups(internal=True).get("items", [])
    enabled = [group for group in groups if group.get("enabled") and not group.get("emergency_stopped")]
    actions = []
    for group in enabled:
        schedule = group_schedule(group)
        now, tz_label = now_for_timezone(schedule.get("timezone"))
        last = load_last_result(group)
        last_keys = dict(last.get("last_keys") or {})
        for action, hhmm in (("pause", schedule.get("close_time")),):
            account_scheduled = str(schedule.get("timezone") or "").lower() == "account"
            interval_scheduled = str(schedule.get("type") or "").lower() == "interval"
            if account_scheduled or interval_scheduled:
                # Per-account timezone/window eligibility is evaluated inside
                # the scheduled preview. The runner only creates a stable
                # five-minute tick key and must not reinterpret account time as
                # server local time.
                bucket_minute = now.minute - (now.minute % 5)
                due = True
                event_key = now.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
            else:
                due, event_key = event_due(now, hhmm)
            action_key = "%s:%s:%s" % (action, tz_label, event_key)
            continuation_key = group_event_continuation_key(last, action)
            continuing = bool(continuation_key)
            if continuing:
                action_key = continuation_key
            if not due and not continuing:
                continue
            if last_keys.get(action) == action_key and not continuing:
                continue
            try:
                payload = run_group_event(group, action, action_key, last.get("last_event") or {})
            except Exception as exc:
                logging.exception("ad control rule group failed group_id=%s action=%s", group.get("group_id"), action)
                payload = event_payload(group, action, action_key, "error", reason=str(exc))
            if payload.get("status") in ("executed", "error", "skipped"):
                last_keys[action] = action_key
            elif payload.get("status") == "partial":
                last_keys.pop(action, None)
            updated = dict(last)
            updated["last_keys"] = last_keys
            updated["last_event"] = payload
            update_rule_group_result(group.get("group_id"), updated)
            last = updated
            log_event_result(payload)
            actions.append(compact_event(payload))
    return {"rule_groups_seen": len(groups), "rule_groups_enabled": len(enabled), "rule_group_actions": actions}


def run_once():
    app.ensure_ad_control_tables()
    group_summary = run_rule_groups()
    rules = app.list_ad_control_rules(internal=True).get("items", [])
    enabled = [rule for rule in rules if rule.get("enabled")]
    actions = []
    for rule in enabled:
        schedule = rule.get("schedule") or {}
        now, tz_label = now_for_timezone(schedule.get("timezone") or schedule.get("execute_timezone"))
        last = load_last_result(rule)
        last_keys = dict(last.get("last_keys") or {})
        events = [
            ("pause", schedule.get("close_time") or schedule.get("pause_time")),
            ("reopen", schedule.get("reopen_time") or schedule.get("restart_time")),
        ]
        for action, hhmm in events:
            due, event_key = event_due(now, hhmm)
            if not due:
                continue
            action_key = "%s:%s:%s" % (action, tz_label, event_key)
            if last_keys.get(action) == action_key:
                continue
            try:
                payload = run_event(rule, action, action_key)
            except Exception as exc:
                logging.exception("ad control rule failed rule_id=%s action=%s", rule.get("rule_id"), action)
                payload = event_payload(rule, action, action_key, "error", reason=str(exc))
            if payload.get("status") != "error" and not execution_has_errors(payload):
                last_keys[action] = action_key
            updated = dict(last)
            updated["last_keys"] = last_keys
            updated["last_event"] = payload
            update_rule_result(rule.get("rule_id"), updated)
            log_event_result(payload)
            actions.append(compact_event(payload))
    summary = {
        "rules_seen": len(rules),
        "rules_enabled": len(enabled),
        "actions": actions,
        **group_summary,
    }
    logging.info("runner summary %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main():
    configure_logging()
    with open(LOCK_PATH, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "locked"}, ensure_ascii=False))
            return 0
        summary = run_once()
        print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

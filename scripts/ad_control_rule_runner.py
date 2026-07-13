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
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

# The host-wide mysql wrapper injects MAX_EXECUTION_TIME by default, but the
# current business database does not support that MySQL session variable.
# An explicit empty init command keeps the wrapper's process timeout while
# preventing the incompatible session statement.
if not any(str(arg).startswith("--init-command") for arg in app.MYSQL_BASE_CMD):
    app.MYSQL_BASE_CMD.insert(-1, "--init-command=")


LOG_PATH = os.environ.get("AD_CONTROL_RUNNER_LOG", "/var/log/ad_control_rule_runner.log")
LOCK_PATH = os.environ.get("AD_CONTROL_RUNNER_LOCK", "/tmp/ad_control_rule_runner.lock")
WINDOW_MINUTES = max(1, int(os.environ.get("AD_CONTROL_RUNNER_WINDOW_MINUTES", "10")))
MAX_ITEMS = max(1, int(os.environ.get("AD_CONTROL_RUNNER_MAX_ITEMS", str(app.AD_CONTROL_MAX_EXECUTE))))


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
        "finished_at": payload.get("finished_at") or "",
    }


def log_event_result(payload):
    event = compact_event(payload)
    logging.info(
        "ad control event result rule_group_id=%s rule_id=%s product=%s action=%s event_key=%s status=%s reason=%s action_id=%s preview_id=%s requested=%s success=%s skipped=%s error=%s dry_run=%s",
        event["rule_group_id"], event["rule_id"], event["product"], event["action"],
        event["event_key"], event["status"], event["reason"], event["action_id"],
        event["preview_id"], event["requested_count"], event["success_count"],
        event["skipped_count"], event["error_count"], event["dry_run"],
    )


def record_rule_group_preview_failure(rule_group, preview, event_key):
    action_id = __import__("uuid").uuid4().hex
    errors = preview.get("errors", [])[:100]
    criteria = {
        "mode": "live",
        "product": rule_group.get("product") or "",
        "rule_group_id": rule_group.get("group_id") or "",
        "binding_id": rule_group.get("group_id") or "",
        "runner_event_key": event_key,
        "runner_status": "error",
        "runner_reason": "live_preview_errors",
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
    return {
        "timezone": strategy.get("timezone") or strategy.get("execute_timezone"),
        "close_time": strategy.get("close_time") or strategy.get("pause_time"),
        "reopen_time": strategy.get("reopen_time") or strategy.get("restart_time"),
    }


def run_group_event(rule_group, action, event_key):
    if action != "pause":
        return event_payload(rule_group, action, event_key, "skipped", reason="unsupported_group_action")
    if not rule_group.get("product"):
        return event_payload(rule_group, action, event_key, "skipped", reason="missing_product")
    session = {"user_id": "ad_control_rule_runner"}
    preview = app.create_ad_control_live_preview({"rule_group_id": rule_group.get("group_id")}, session)
    if int(preview.get("error_count") or 0) > 0:
        action_id = record_rule_group_preview_failure(rule_group, preview, event_key)
        return event_payload(rule_group, action, event_key, "error", result={
            "action_id": action_id,
            "preview_id": preview.get("preview_id"),
            "error_count": preview.get("error_count"),
            "errors": preview.get("errors", [])[:10],
        }, reason="live_preview_errors")
    result = app.execute_ad_control_live({
        "preview_id": preview.get("preview_id"),
        "preview_hash": preview.get("preview_hash"),
        "dry_run": False,
        "confirm": "EXECUTE_LIVE_PAUSE",
    }, session)
    status = "error" if int(result.get("error_count") or 0) > 0 else "executed"
    return event_payload(rule_group, action, event_key, status, result=result, reason="live_execute_errors" if status == "error" else "")


def run_rule_groups():
    groups = app.list_ad_control_rule_groups().get("items", [])
    enabled = [group for group in groups if group.get("enabled") and not group.get("emergency_stopped")]
    actions = []
    for group in enabled:
        schedule = group_schedule(group)
        now, tz_label = now_for_timezone(schedule.get("timezone"))
        last = load_last_result(group)
        last_keys = dict(last.get("last_keys") or {})
        for action, hhmm in (("pause", schedule.get("close_time")), ("reopen", schedule.get("reopen_time"))):
            due, event_key = event_due(now, hhmm)
            if not due:
                continue
            action_key = "%s:%s:%s" % (action, tz_label, event_key)
            if last_keys.get(action) == action_key:
                continue
            try:
                payload = run_group_event(group, action, action_key)
            except Exception as exc:
                logging.exception("ad control rule group failed group_id=%s action=%s", group.get("group_id"), action)
                payload = event_payload(group, action, action_key, "error", reason=str(exc))
            if payload.get("status") != "error" and not execution_has_errors(payload):
                last_keys[action] = action_key
            updated = dict(last)
            updated["last_keys"] = last_keys
            updated["last_event"] = payload
            update_rule_group_result(group.get("group_id"), updated)
            log_event_result(payload)
            actions.append(compact_event(payload))
    return {"rule_groups_seen": len(groups), "rule_groups_enabled": len(enabled), "rule_group_actions": actions}


def run_once():
    app.ensure_ad_control_tables()
    group_summary = run_rule_groups()
    rules = app.list_ad_control_rules().get("items", [])
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

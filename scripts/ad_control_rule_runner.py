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


def event_payload(rule, action, event_key, status, result=None, reason=""):
    return {
        "rule_id": rule.get("rule_id"),
        "product": rule.get("product"),
        "action": action,
        "event_key": event_key,
        "status": status,
        "reason": reason,
        "result": result or {},
        "finished_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }


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
    return event_payload(rule, action, event_key, "executed", result=result)


def run_once():
    app.ensure_ad_control_tables()
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
            last_keys[action] = action_key
            updated = dict(last)
            updated["last_keys"] = last_keys
            updated["last_event"] = payload
            update_rule_result(rule.get("rule_id"), updated)
            actions.append(payload)
    summary = {
        "rules_seen": len(rules),
        "rules_enabled": len(enabled),
        "actions": actions,
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

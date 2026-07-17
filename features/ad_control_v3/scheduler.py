"""Account-timezone scheduler helpers for the independent V3 runner."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def _minutes(value: Any) -> int:
    text = str(value or "").strip()
    pieces = text.split(":")
    if len(pieces) != 2:
        return -1
    try:
        hour, minute = int(pieces[0]), int(pieces[1])
    except ValueError:
        return -1
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return -1
    return hour * 60 + minute


def _inside_window(now_minute: int, schedule: Mapping[str, Any]) -> bool:
    start = _minutes(schedule.get("allowed_start_time"))
    end = _minutes(schedule.get("allowed_end_time"))
    if start < 0 and end < 0:
        return True
    if start >= 0 and end < 0:
        return now_minute >= start
    if start < 0 and end >= 0:
        return now_minute <= end
    if start == end:
        return True
    if start < end:
        return start <= now_minute <= end
    return now_minute >= start or now_minute <= end


def candidate_schedule_due(
    candidate: Mapping[str, Any],
    schedule: Mapping[str, Any],
    now_utc: datetime,
) -> Tuple[bool, str, Dict[str, Any]]:
    timezone_name = str(candidate.get("account_timezone") or "").strip()
    if not timezone_name or ZoneInfo is None:
        return False, "missing_account_timezone", {}
    try:
        current = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
        local = current.astimezone(ZoneInfo(timezone_name))
    except Exception:
        return False, "invalid_account_timezone", {}
    now_minute = local.hour * 60 + local.minute
    context = {
        "account_timezone": timezone_name,
        "account_local_date": local.strftime("%Y-%m-%d"),
        "account_local_time": local.strftime("%H:%M"),
    }
    if not _inside_window(now_minute, schedule):
        return False, "outside_execution_window", context
    schedule_type = str(schedule.get("type") or "")
    if schedule_type == "fixed_time":
        due = _minutes(schedule.get("fixed_time")) == now_minute
        return due, "" if due else "outside_fixed_time", context
    if schedule_type == "interval":
        try:
            interval = int(schedule.get("interval_minutes") or 0)
        except (TypeError, ValueError):
            interval = 0
        due = interval > 0 and now_minute % interval == 0
        return due, "" if due else "outside_interval", context
    return False, "missing_schedule", context


def runner_event_key(group_id: str, scheduled_for: datetime) -> str:
    value = scheduled_for if scheduled_for.tzinfo else scheduled_for.replace(tzinfo=timezone.utc)
    bucket = value.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    digest = hashlib.sha256((str(group_id) + ":" + bucket).encode("utf-8")).hexdigest()[:32]
    return "v3:%s:%s" % (bucket, digest)


__all__ = ["candidate_schedule_due", "runner_event_key"]

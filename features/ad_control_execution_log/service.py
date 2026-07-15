"""Pure helpers and the ``ads_ai`` persistence adapter for ad control.

The module deliberately has no dependency on the monolithic ``app`` module.
Callers pass database connection settings and keep SQLite as an outbox/fallback.
"""

import ast
import json
import os
import re
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone


WRITER_HOST = "101.32.56.53"
WRITER_PORT = 63353
READER_HOST = "101.32.56.53"
READER_PORT = 63350
WRITER_DATABASE = "ads_ai"
DEFAULT_TABLE = "ad_control_action_log"
QUALIFIED_TABLE = "`ads_ai`.`ad_control_action_log`"
MAX_CONNECT_TIMEOUT_SECONDS = 3
MAX_IO_TIMEOUT_SECONDS = 5
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_LIST_ROWS = 1000
PARTIAL_STALE_SECONDS = 3 * 60 * 60
WRITE_RATE_PER_SECOND = 1.0
WRITE_BURST = 2.0
WRITER_LOCK_FILE = (
    "/var/lock/ad_control_action_log_writer.lock"
    if os.name == "posix"
    else os.path.join(tempfile.gettempdir(), "ad_control_action_log_writer.lock")
)
RETRYABLE_GRAPH_CODES = {1, 2, 4, 17, 32, 613}
RETRYABLE_GRAPH_SUBCODES = {5044001}
TERMINAL_SKIP_REASONS = {"not_active", "already_paused", "not_pause_target"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_WRITE_LOCK = threading.Lock()


class ActionLogSafetyError(RuntimeError):
    """Raised when the dedicated action-log database boundary is violated."""


def normalize_account(value):
    return str(value or "").strip().replace("act_", "").replace("ACT_", "")


def balanced_execution_items(items, max_total=200, max_per_account=20):
    """Return a deterministic, fair batch with a per-account safety cap."""
    max_total = max(1, int(max_total or 1))
    max_per_account = max(1, int(max_per_account or 1))
    grouped = OrderedDict()
    ordered = sorted(
        list(items or []),
        key=lambda item: (
            normalize_account(item.get("account_id")),
            str(item.get("campaign_id") or item.get("object_id") or ""),
        ),
    )
    for item in ordered:
        grouped.setdefault(normalize_account(item.get("account_id")), []).append(item)
    selected = []
    offsets = {account_id: 0 for account_id in grouped}
    while len(selected) < max_total:
        progressed = False
        for account_id, account_items in grouped.items():
            offset = offsets[account_id]
            if offset >= len(account_items) or offset >= max_per_account:
                continue
            selected.append(account_items[offset])
            offsets[account_id] += 1
            progressed = True
            if len(selected) >= max_total:
                break
        if not progressed:
            break
    return selected


def _json_error_payload(reason):
    text = str(reason or "").strip()
    if not text:
        return {}
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                nested = payload.get("error")
                return nested if isinstance(nested, dict) else payload
        except Exception:
            try:
                payload = ast.literal_eval(candidate)
                if isinstance(payload, dict):
                    nested = payload.get("error")
                    return nested if isinstance(nested, dict) else payload
            except Exception:
                continue
    return {}


def graph_error_details(reason):
    payload = _json_error_payload(reason)
    text = str(reason or "")
    code = payload.get("code")
    subcode = payload.get("error_subcode")
    try:
        code = int(code) if code not in (None, "") else None
    except Exception:
        code = None
    try:
        subcode = int(subcode) if subcode not in (None, "") else None
    except Exception:
        subcode = None
    lower = text.lower()
    retryable = (
        code in RETRYABLE_GRAPH_CODES
        or subcode in RETRYABLE_GRAPH_SUBCODES
        or "rate limit" in lower
        or "request limit" in lower
        or "temporarily unavailable" in lower
        or "timed out" in lower
        or "timeout" in lower
        or "connection reset" in lower
        or "bad gateway" in lower
        or "service unavailable" in lower
        or "gateway timeout" in lower
        or "internal server error" in lower
        or bool(re.search(r"(?:http(?: status)?[ =:]*)?5\d\d\b", lower))
    )
    rate_limited = (
        code in {4, 17, 32, 613}
        or subcode in RETRYABLE_GRAPH_SUBCODES
        or "rate limit" in lower
        or "request limit" in lower
    )
    return {
        "retryable": bool(retryable),
        "rate_limited": bool(rate_limited),
        "error_code": code,
        "error_subcode": subcode,
        "error_type": str(payload.get("type") or ""),
        "error_message": str(payload.get("message") or text),
    }


def enrich_error_result(result):
    result = dict(result or {})
    result.update(graph_error_details(result.get("reason")))
    return result


def execution_summary(results, matched_count=0, requested_count=0, preview_error_count=0):
    results = list(results or [])
    matched_count = max(0, int(matched_count or 0))
    requested_count = max(0, int(requested_count or 0))
    deferred_count = max(0, matched_count - requested_count) + sum(
        1 for item in results if item.get("status") == "deferred"
    )
    retryable_error_count = sum(
        1 for item in results
        if item.get("status") == "error" and bool(item.get("retryable"))
    )
    permanent_error_count = sum(
        1 for item in results
        if item.get("status") == "error" and not bool(item.get("retryable"))
    )
    terminal_skip_count = sum(
        1 for item in results
        if item.get("status") == "skipped" and str(item.get("reason") or "") in TERMINAL_SKIP_REASONS
    )
    blocked_count = sum(
        1 for item in results
        if item.get("status") == "skipped" and str(item.get("reason") or "") not in TERMINAL_SKIP_REASONS
    )
    remaining_count = deferred_count + retryable_error_count
    if permanent_error_count or blocked_count:
        run_status = "blocked"
    elif remaining_count or int(preview_error_count or 0):
        run_status = "partial"
    else:
        run_status = "executed"
    return {
        "run_status": run_status,
        "deferred_count": deferred_count,
        "remaining_count": remaining_count,
        "retryable_error_count": retryable_error_count,
        "permanent_error_count": permanent_error_count,
        "terminal_skip_count": terminal_skip_count,
        "blocked_count": blocked_count,
        "preview_error_count": max(0, int(preview_error_count or 0)),
    }


def sanitize_json(value):
    """Remove credential-shaped fields before durable logging."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"access_token", "authorization", "password", "secret", "client_secret"}:
                clean[key] = "[REDACTED]"
            else:
                clean[key] = sanitize_json(item)
        return clean
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


def reason_summary(results):
    counts = {}
    for item in results or []:
        reason = str(item.get("reason") or "").strip()
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _criteria(item):
    value = (item or {}).get("criteria") or {}
    return value if isinstance(value, dict) else {}


def _parse_utc_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_key(item):
    criteria = _criteria(item)
    return str(
        (item or {}).get("event_key")
        or criteria.get("runner_event_key")
        or criteria.get("event_key")
        or ""
    ).strip()


def _event_business_date(event_key):
    text = str(event_key or "")
    match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text)
    if match:
        return match.group(1)
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", text)
    if match:
        value = match.group(1)
        return "%s-%s-%s" % (value[:4], value[4:6], value[6:8])
    return ""


def action_business_date(item, local_offset_hours=8):
    event_date = _event_business_date(_event_key(item))
    if event_date:
        return event_date
    created_at = _parse_utc_datetime((item or {}).get("created_at"))
    if created_at:
        return (created_at + timedelta(hours=int(local_offset_hours or 0))).date().isoformat()
    text = str((item or {}).get("created_at") or "")
    return text[:10] if re.match(r"^20\d{2}-\d{2}-\d{2}", text) else ""


def _rule_identity(item):
    criteria = _criteria(item)
    return str(
        (item or {}).get("binding_id")
        or (item or {}).get("rule_id")
        or criteria.get("rule_group_id")
        or criteria.get("binding_id")
        or criteria.get("rule_id")
        or ""
    ).strip()


def action_daily_group_key(item, local_offset_hours=8):
    """Return a conservative read-model key; unsafe legacy/manual rows stay single."""
    item = dict(item or {})
    action_id = str(item.get("action_id") or "").strip()
    rule_identity = _rule_identity(item)
    source_type = str(item.get("source_type") or "").strip().lower()
    actor = str(item.get("actor_user_id") or "").strip()
    if source_type in {"manual", "api", "admin"} and actor != "ad_control_rule_runner":
        return ("action", action_id)
    scheduled = source_type in {
        "scheduled", "runner", "runner_execute", "runner_verification",
        "runner_preview_failure",
    } or actor == "ad_control_rule_runner"
    if not scheduled or not rule_identity:
        return ("action", action_id)
    business_date = action_business_date(item, local_offset_hours)
    if not business_date:
        return ("action", action_id)
    return (
        "daily",
        business_date,
        str(item.get("product") or _criteria(item).get("product") or "").strip(),
        rule_identity,
        str(item.get("action") or "").strip(),
        str(item.get("level") or item.get("object_level") or "campaign").strip(),
        "dry" if bool(item.get("dry_run")) else "real",
    )


def _run_status(item):
    criteria = _criteria(item)
    summary = criteria.get("execution_summary") or {}
    return str(
        (item or {}).get("run_status")
        or criteria.get("runner_status")
        or (summary.get("run_status") if isinstance(summary, dict) else "")
        or ""
    ).strip().lower()


def _runner_reason(item):
    criteria = _criteria(item)
    summary = criteria.get("execution_summary") or {}
    return str(
        (item or {}).get("runner_reason")
        or criteria.get("runner_reason")
        or criteria.get("reason")
        or (summary.get("runner_reason") if isinstance(summary, dict) else "")
        or ""
    ).strip()


def _verification_only(item):
    criteria = _criteria(item)
    source_type = str((item or {}).get("source_type") or "").lower()
    return bool(
        (item or {}).get("verification_only")
        or criteria.get("verification_only")
        or "verification" in source_type
    )


def _batch_sort_key(item):
    parsed = _parse_utc_datetime((item or {}).get("created_at"))
    timestamp = parsed.timestamp() if parsed else 0.0
    updated = _parse_utc_datetime((item or {}).get("updated_at"))
    updated_timestamp = updated.timestamp() if updated else timestamp
    return (
        timestamp,
        updated_timestamp,
        1 if _verification_only(item) else 0,
        str((item or {}).get("action_id") or ""),
    )


def _latest_batch(items):
    ordered = sorted((dict(item or {}) for item in items), key=_batch_sort_key)
    latest_key = _batch_sort_key(ordered[-1])[:2]
    candidates = [item for item in ordered if _batch_sort_key(item)[:2] == latest_key]
    verified = [
        item for item in candidates
        if _verification_only(item) and _run_status(item) == "executed"
    ]
    if verified:
        return max(verified, key=_batch_sort_key)
    severity = {"executed": 0, "partial": 1, "error": 2, "failed": 2, "blocked": 3}
    return max(
        candidates,
        key=lambda item: (
            severity.get(_run_status(item), 1),
            str(item.get("action_id") or ""),
        ),
    )


def _batch_payload(item):
    item = dict(item or {})
    return {
        "action_id": str(item.get("action_id") or ""),
        "preview_id": str(item.get("preview_id") or ""),
        "event_key": _event_key(item),
        "source_type": str(item.get("source_type") or ""),
        "run_status": _run_status(item),
        "runner_reason": _runner_reason(item),
        "batch_planned_count": int(item.get("batch_planned_count") or 0),
        "requested_count": int(item.get("requested_count") or 0),
        "success_count": int(item.get("success_count") or 0),
        "skipped_count": int(item.get("skipped_count") or 0),
        "error_count": int(item.get("error_count") or 0),
        "retryable_error_count": int(item.get("retryable_error_count") or 0),
        "blocked_count": int(item.get("blocked_count") or 0),
        "deferred_count": int(item.get("deferred_count") or 0),
        "remaining_count": int(item.get("remaining_count") or 0),
        "verification_only": _verification_only(item),
        "log_version": int(item.get("log_version") or 1),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "log_store": str(item.get("log_store") or "sqlite_fallback"),
        "criteria": {
            "verification_only": _verification_only(item),
            "runner_status": _criteria(item).get("runner_status") or "",
        },
        "reason_summary": list(item.get("reason_summary") or []),
    }


def _merge_reason_summaries(items):
    counts = {}
    for item in items:
        for reason in item.get("reason_summary") or []:
            if not isinstance(reason, dict):
                continue
            text = str(reason.get("reason") or "").strip()
            if text:
                counts[text] = counts.get(text, 0) + max(0, int(reason.get("count") or 0))
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _daily_display_status(latest, history_retryable=0, now=None):
    latest = dict(latest or {})
    status = _run_status(latest)
    remaining = max(0, int(latest.get("remaining_count") or 0))
    retryable = max(0, int(latest.get("retryable_error_count") or 0))
    error_count = max(0, int(latest.get("error_count") or 0))
    runner_reason = _runner_reason(latest)
    blocked = max(0, int(latest.get("blocked_count") or 0))
    if status == "executed" and not (remaining or error_count or blocked):
        label = "当日执行完成（曾重试）" if int(history_retryable or 0) > 0 else "当日执行完成"
        return {"key": "success", "label": label, "class": "ok"}
    if status == "executed":
        return {"key": "inconsistent", "label": "状态异常：完成记录仍有未处理项", "class": "danger"}
    if status == "blocked":
        return {"key": "blocked", "label": "当日执行受阻", "class": "danger"}
    if status in {"error", "failed"}:
        return {"key": "failed", "label": "当日执行失败", "class": "danger"}
    if status == "partial":
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        created_at = _parse_utc_datetime(latest.get("created_at"))
        stale = bool(created_at and (current.astimezone(timezone.utc) - created_at).total_seconds() > PARTIAL_STALE_SECONDS)
        if error_count > retryable or blocked > 0:
            return {"key": "blocked", "label": "当日执行受阻（非重试错误）", "class": "danger"}
        if stale:
            if retryable > 0:
                return {"key": "incomplete", "label": "当日未完成（限流后未完成）", "class": "danger"}
            if remaining > 0:
                return {"key": "incomplete", "label": "当日未完成，剩余 %s" % remaining, "class": "danger"}
            return {"key": "incomplete", "label": "当日未完成（缺少完成复核）", "class": "danger"}
        if remaining == 0 and runner_reason == "live_execute_verify_remaining":
            return {"key": "verifying", "label": "本批已处理，待零目标复核", "class": "warn"}
        if retryable > 0:
            return {"key": "retrying", "label": "限流/临时错误，待续跑", "class": "warn"}
        if remaining > 0:
            return {"key": "partial", "label": "处理中，待续跑 %s" % remaining, "class": "warn"}
        return {"key": "verifying", "label": "本批已处理，待零目标复核", "class": "warn"}
    if blocked > 0 or error_count > retryable:
        return {"key": "blocked", "label": "当日执行受阻（非重试错误）", "class": "danger"}
    if remaining > 0 or retryable > 0:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        created_at = _parse_utc_datetime(latest.get("created_at"))
        stale = bool(created_at and (current.astimezone(timezone.utc) - created_at).total_seconds() > PARTIAL_STALE_SECONDS)
        if stale:
            label = "当日未完成（限流后未完成）" if retryable > 0 else "当日未完成，剩余 %s" % remaining
            return {"key": "incomplete", "label": label, "class": "danger"}
        if retryable > 0:
            return {"key": "retrying", "label": "限流/临时错误，待续跑", "class": "warn"}
        return {"key": "partial", "label": "处理中，待续跑 %s" % remaining, "class": "warn"}
    if error_count > 0:
        return {"key": "failed", "label": "当日执行失败", "class": "danger"}
    if int(latest.get("success_count") or 0) > 0:
        return {"key": "success", "label": "当日执行完成", "class": "ok"}
    return {"key": "noop", "label": "当日无执行目标", "class": "warn"}


def _combine_daily_display_status(event_states, remaining_count=0, history_retryable=0):
    states = [dict(state or {}) for state in event_states if state]
    if not states:
        return {"key": "noop", "label": "当日无执行目标", "class": "warn"}
    if len(states) == 1:
        return states[0]
    keys = {str(state.get("key") or "") for state in states}
    if "inconsistent" in keys:
        return {"key": "inconsistent", "label": "当日存在状态异常事件", "class": "danger"}
    if "blocked" in keys:
        return {"key": "blocked", "label": "当日存在执行受阻事件", "class": "danger"}
    if "failed" in keys:
        return {"key": "failed", "label": "当日存在执行失败事件", "class": "danger"}
    if "incomplete" in keys:
        return {"key": "incomplete", "label": "当日存在未完成事件", "class": "danger"}
    if "retrying" in keys:
        return {"key": "retrying", "label": "当日仍有限流/临时错误待续跑", "class": "warn"}
    if "partial" in keys:
        return {"key": "partial", "label": "当日仍有 %s 待续跑" % max(0, int(remaining_count or 0)), "class": "warn"}
    if "verifying" in keys:
        return {"key": "verifying", "label": "当日仍有事件待零目标复核", "class": "warn"}
    if keys <= {"noop"}:
        return {"key": "noop", "label": "当日无执行目标", "class": "warn"}
    label = "当日执行完成（曾重试）" if int(history_retryable or 0) > 0 else "当日执行完成"
    return {"key": "success", "label": label, "class": "ok"}


def _aggregate_daily_group(items, local_offset_hours=8, now=None):
    ordered = sorted((dict(item or {}) for item in items), key=_batch_sort_key)
    latest = _latest_batch(ordered)
    event_chains = OrderedDict()
    for item in ordered:
        event_chains.setdefault(_event_key(item) or "__legacy__", []).append(item)
    event_records = []
    for chain_key, chain_items in event_chains.items():
        chain_final = _latest_batch(chain_items)
        chain_retryable = sum(max(0, int(item.get("retryable_error_count") or 0)) for item in chain_items)
        event_records.append({
            "event_key": "" if chain_key == "__legacy__" else chain_key,
            "action_id": str(chain_final.get("action_id") or ""),
            "run_status": _run_status(chain_final),
            "remaining_count": max(0, int(chain_final.get("remaining_count") or 0)),
            "display_status": _daily_display_status(
                chain_final,
                history_retryable=chain_retryable,
                now=now,
            ),
            "final": chain_final,
        })
    event_remaining_count = sum(record["remaining_count"] for record in event_records)
    history_retryable_count = sum(
        max(0, int(item.get("retryable_error_count") or 0)) for item in ordered
    )
    display_status = _combine_daily_display_status(
        [record["display_status"] for record in event_records],
        remaining_count=event_remaining_count,
        history_retryable=history_retryable_count,
    )
    representative_priority = {
        "inconsistent": 7,
        "blocked": 6,
        "failed": 5,
        "incomplete": 4,
        "retrying": 3,
        "partial": 2,
        "verifying": 1,
        "success": 0,
        "noop": 0,
    }
    representative_event = max(
        event_records,
        key=lambda record: (
            representative_priority.get(record["display_status"].get("key"), 1),
            _batch_sort_key(record["final"]),
        ),
    )
    representative_final = representative_event["final"]
    first_execution = next(
        (
            item for item in ordered
            if not _verification_only(item)
            and (int(item.get("requested_count") or 0) > 0 or int(item.get("matched_count") or 0) > 0)
        ),
        next((item for item in ordered if not _verification_only(item)), ordered[0]),
    )
    result = dict(latest)
    rule_identity = _rule_identity(latest) or _rule_identity(first_execution)
    binding_identity = str(latest.get("binding_id") or first_execution.get("binding_id") or "")
    business_date = _event_business_date(_event_key(latest)) or action_business_date(first_execution, local_offset_hours)
    batches = [_batch_payload(item) for item in ordered]
    execution_batches = [item for item in ordered if not _verification_only(item)]
    verification_batches = [item for item in ordered if _verification_only(item)]
    sum_fields = (
        "requested_count", "success_count", "skipped_count", "error_count",
        "retryable_error_count", "blocked_count", "deferred_count",
    )
    for field in sum_fields:
        result[field] = sum(max(0, int(item.get(field) or 0)) for item in ordered)
    stable_group_parts = (
        business_date,
        str(latest.get("product") or ""),
        rule_identity,
        str(latest.get("action") or ""),
        str(latest.get("level") or latest.get("object_level") or "campaign"),
        "dry" if bool(latest.get("dry_run")) else "real",
    )
    conservative_key = action_daily_group_key(latest, local_offset_hours)
    is_scheduled_daily = bool(conservative_key and conservative_key[0] == "daily")
    group_id = (
        "daily:" + ":".join(stable_group_parts)
        if is_scheduled_daily
        else "action:" + str(latest.get("action_id") or "")
    )
    result.update({
        "is_daily_group": is_scheduled_daily,
        "group_type": "daily" if is_scheduled_daily else "action",
        "group_id": group_id,
        "business_date": business_date,
        "binding_id": binding_identity,
        "rule_identity": rule_identity,
        "action_id": str(latest.get("action_id") or ""),
        "latest_action_id": str(latest.get("action_id") or ""),
        "action_ids": [str(item.get("action_id") or "") for item in ordered],
        "batch_count": len(ordered),
        "execution_batch_count": len(execution_batches),
        "verification_batch_count": len(verification_batches),
        "attempt_count": sum(max(0, int(item.get("requested_count") or 0)) for item in ordered),
        "first_created_at": str(ordered[0].get("created_at") or ""),
        "last_created_at": str(latest.get("created_at") or ""),
        "created_at": str(latest.get("created_at") or ""),
        "scanned_count": int(first_execution.get("scanned_count") or 0),
        "candidate_count": int(first_execution.get("candidate_count") or 0),
        "matched_count": int(first_execution.get("matched_count") or 0),
        "batch_planned_count": sum(max(0, int(item.get("batch_planned_count") or 0)) for item in execution_batches),
        "remaining_count": event_remaining_count,
        "run_status": {
            "success": "executed",
            "noop": "executed",
            "blocked": "blocked",
            "failed": "failed",
            "inconsistent": "blocked",
        }.get(display_status.get("key"), "partial"),
        "runner_reason": _runner_reason(representative_final),
        "reason_summary": _merge_reason_summaries(ordered),
        "results": [],
        "batches": batches,
        "event_count": len(event_records),
        "event_states": [
            {
                "event_key": record["event_key"],
                "action_id": record["action_id"],
                "run_status": record["run_status"],
                "remaining_count": record["remaining_count"],
                "display_status": record["display_status"],
            }
            for record in event_records
        ],
        "status_inferred": bool(
            any(
                int(record["final"].get("log_version") or 1) < 2
                and not _event_key(record["final"])
                and not _runner_reason(record["final"])
                for record in event_records
            )
        ),
    })
    stores = {str(item.get("log_store") or "sqlite_fallback") for item in ordered}
    result["log_store"] = next(iter(stores)) if len(stores) == 1 else "mixed"
    criteria = dict(_criteria(latest))
    if binding_identity:
        criteria.setdefault("rule_group_id", binding_identity)
        criteria.setdefault("binding_id", binding_identity)
    criteria["daily_group"] = {
        "business_date": business_date,
        "batch_count": len(ordered),
        "attempt_count": result["attempt_count"],
    }
    result["criteria"] = criteria
    result["display_status"] = display_status
    return result


def group_actions_daily(items, limit=50, local_offset_hours=8, now=None, source_truncated=False):
    """Build a bounded daily read model without changing underlying action rows."""
    merged = {}
    for item in items or []:
        action_id = str((item or {}).get("action_id") or "").strip()
        if not action_id:
            continue
        candidate = dict(item or {})
        current = merged.get(action_id)
        if current is None:
            merged[action_id] = candidate
            continue
        preference = lambda value: (
            1 if str(value.get("log_store") or "") == "ads_ai" else 0,
            int(value.get("log_version") or 1),
            _batch_sort_key(value),
        )
        if preference(candidate) > preference(current):
            merged[action_id] = candidate
    groups = OrderedDict()
    for item in sorted(merged.values(), key=_batch_sort_key):
        key = action_daily_group_key(item, local_offset_hours)
        groups.setdefault(key, []).append(item)
    aggregated = [
        _aggregate_daily_group(group, local_offset_hours, now)
        for group in groups.values()
    ]
    aggregated.sort(key=lambda item: _batch_sort_key(item), reverse=True)
    discarded = 0
    if source_truncated and aggregated:
        oldest_raw = min(merged.values(), key=_batch_sort_key)
        boundary_date = action_business_date(oldest_raw, local_offset_hours)
        retained = []
        for item in aggregated:
            if boundary_date and str(item.get("business_date") or "") <= boundary_date:
                discarded += 1
            else:
                retained.append(item)
        aggregated = retained
    limit = max(1, min(200, int(limit or 50)))
    has_more_groups = len(aggregated) > limit
    return {
        "items": aggregated[:limit],
        "truncated": bool(source_truncated or has_more_groups),
        "source_truncated": bool(source_truncated),
        "has_more_groups": bool(has_more_groups),
        "discarded_group_count": discarded,
        "raw_action_count": len(merged),
        "group_count": len(aggregated),
    }


def _identifier(value):
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError("invalid mysql identifier")
    return "`%s`" % text


def _bounded_int(value, default, maximum):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(1, min(parsed, int(maximum)))


def validate_target(config, role, table=DEFAULT_TABLE):
    """Fail closed unless a connection matches the dedicated log boundary."""
    config = dict(config or {})
    role = str(role or "").strip().lower()
    if role not in {"reader", "writer"}:
        raise ActionLogSafetyError("invalid ad-control action-log connection role")
    host = str(config.get("host") or "").strip()
    port = int(config.get("port") or 0)
    database = str(config.get("database") or "").strip()
    user = str(config.get("user") or "").strip()
    expected_host = WRITER_HOST if role == "writer" else READER_HOST
    expected_port = WRITER_PORT if role == "writer" else READER_PORT
    if host != expected_host or port != expected_port:
        raise ActionLogSafetyError(
            "ad-control action-log %s endpoint must be %s:%s" % (
                role, expected_host, expected_port,
            )
        )
    if database != WRITER_DATABASE:
        raise ActionLogSafetyError("ad-control action-log database must be ads_ai")
    if str(table or "").strip() != DEFAULT_TABLE:
        raise ActionLogSafetyError(
            "ad-control action-log table must be ad_control_action_log"
        )
    if not user:
        raise ActionLogSafetyError("ad-control action-log database user is required")
    config.update({
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "connect_timeout": _bounded_int(
            config.get("connect_timeout"), 3, MAX_CONNECT_TIMEOUT_SECONDS
        ),
        "read_timeout": _bounded_int(
            config.get("read_timeout"), 5, MAX_IO_TIMEOUT_SECONDS
        ),
        "write_timeout": _bounded_int(
            config.get("write_timeout"), 5, MAX_IO_TIMEOUT_SECONDS
        ),
    })
    return config


def _connect(config, role, table=DEFAULT_TABLE):
    import pymysql

    config = validate_target(config, role, table)
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=str(config.get("password") or ""),
        database=config["database"],
        charset="utf8mb4",
        connect_timeout=config["connect_timeout"],
        read_timeout=config["read_timeout"],
        write_timeout=config["write_timeout"],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


LOG_COLUMNS = [
    "action_id", "preview_id", "binding_id", "rule_id", "event_key", "source_type",
    "actor_user_id", "product", "action", "object_level", "run_status", "runner_reason",
    "dry_run", "scanned_count", "candidate_count", "matched_count", "batch_planned_count",
    "deferred_count", "requested_count", "success_count", "skipped_count", "error_count",
    "retryable_error_count", "blocked_count", "remaining_count", "criteria_json", "results_json",
    "reason_summary_json", "log_version", "created_at", "updated_at",
]


def normalize_record(record):
    record = dict(record or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    criteria = sanitize_json(record.pop("criteria", {}))
    results = sanitize_json(record.pop("results", []))
    normalized = {key: record.get(key, "") for key in LOG_COLUMNS}
    for key in (
        "dry_run", "scanned_count", "candidate_count", "matched_count", "batch_planned_count",
        "deferred_count", "requested_count", "success_count", "skipped_count", "error_count",
        "retryable_error_count", "blocked_count", "remaining_count", "log_version",
    ):
        normalized[key] = max(0, int(normalized.get(key) or 0))
    normalized["criteria_json"] = json.dumps(criteria, ensure_ascii=False, separators=(",", ":"))
    normalized["results_json"] = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
    normalized["reason_summary_json"] = json.dumps(reason_summary(results), ensure_ascii=False, separators=(",", ":"))
    normalized["created_at"] = str(normalized.get("created_at") or now)
    normalized["updated_at"] = str(normalized.get("updated_at") or now)
    normalized["log_version"] = int(normalized.get("log_version") or 1)
    return normalized


def _enforce_payload_limit(record):
    payload_bytes = sum(
        len(str(record.get(key) or "").encode("utf-8"))
        for key in ("criteria_json", "results_json", "reason_summary_json")
    )
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise ActionLogSafetyError(
            "ad-control action-log JSON payload exceeds %s bytes" % MAX_PAYLOAD_BYTES
        )


def _acquire_interprocess_write_slot():
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(WRITER_LOCK_FILE, flags, 0o600)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise ActionLogSafetyError("ad-control writer lock must be a regular file")
    if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
        os.close(fd)
        raise ActionLogSafetyError("ad-control writer lock has an unexpected owner")
    handle = os.fdopen(fd, "r+", encoding="ascii")
    fcntl = None
    try:
        try:
            import fcntl as fcntl_module

            fcntl = fcntl_module
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Production is Linux; the in-process lock remains active on Windows tests.
            pass
        except (BlockingIOError, OSError):
            raise ActionLogSafetyError("ad-control action-log global writer is busy")
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
            last_refill = float(state.get("last_refill") or 0.0)
            tokens = float(state.get("tokens") or 0.0)
        except Exception:
            last_refill = 0.0
            tokens = 0.0
        now = time.time()
        if last_refill <= 0:
            tokens = WRITE_BURST
        else:
            tokens = min(
                WRITE_BURST,
                tokens + max(0.0, now - last_refill) * WRITE_RATE_PER_SECOND,
            )
        if tokens < 1.0:
            raise ActionLogSafetyError(
                "ad-control action-log writer exceeded burst 2 / average 1 qps"
            )
        tokens -= 1.0
        handle.seek(0)
        handle.truncate(0)
        handle.write(json.dumps({"last_refill": now, "tokens": tokens}))
        handle.flush()
        os.fsync(handle.fileno())
        return handle, fcntl
    except Exception:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        handle.close()
        raise


def _release_interprocess_write_slot(handle, fcntl):
    if handle is None:
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    handle.close()


def _serialized_write(config, table, callback):
    """Execute one bounded statement with Linux host-wide concurrency and rate 1."""
    validate_target(config, "writer", table)
    if not _WRITE_LOCK.acquire(blocking=False):
        raise ActionLogSafetyError("ad-control action-log writer is busy")
    conn = None
    lock_handle = None
    fcntl = None
    try:
        lock_handle, fcntl = _acquire_interprocess_write_slot()
        conn = _connect(config, "writer", table)
        with conn.cursor() as cursor:
            return callback(cursor)
    finally:
        if conn is not None:
            conn.close()
        _release_interprocess_write_slot(lock_handle, fcntl)
        _WRITE_LOCK.release()


def upsert_action(config, record, table=DEFAULT_TABLE):
    config = validate_target(config, "writer", table)
    record = normalize_record(record)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(record.get("action_id") or "")):
        raise ActionLogSafetyError("invalid ad-control action_id")
    _enforce_payload_limit(record)
    column_sql = ",".join(_identifier(key) for key in LOG_COLUMNS)
    placeholders = ",".join(["%s"] * len(LOG_COLUMNS))
    updates = ",".join(
        "%s=VALUES(%s)" % (_identifier(key), _identifier(key))
        for key in LOG_COLUMNS if key not in {"action_id", "created_at"}
    )
    sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
        QUALIFIED_TABLE, column_sql, placeholders, updates,
    )
    _serialized_write(
        config,
        table,
        lambda cursor: cursor.execute(sql, [record[key] for key in LOG_COLUMNS]),
    )
    return record


def update_runner_status(config, action_id, event_key, status, reason, remaining_count, table=DEFAULT_TABLE):
    config = validate_target(config, "writer", table)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(action_id or "")):
        raise ActionLogSafetyError("invalid ad-control action_id")
    sql = "UPDATE %s SET event_key=%%s,run_status=%%s,runner_reason=%%s,remaining_count=%%s,updated_at=UTC_TIMESTAMP() WHERE action_id=%%s LIMIT 1" % QUALIFIED_TABLE
    def execute(cursor):
        cursor.execute(
            sql,
            (
                event_key or "",
                status or "",
                reason or "",
                max(0, int(remaining_count or 0)),
                action_id,
            ),
        )
        return int(cursor.rowcount or 0)

    return _serialized_write(config, table, execute)


def _decode_row(row, include_results=False):
    item = dict(row or {})
    for key in ("created_at", "updated_at"):
        value = item.get(key)
        if hasattr(value, "strftime"):
            item[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif value is not None:
            item[key] = str(value)
    for source, target, default in (
        ("criteria_json", "criteria", {}),
        ("reason_summary_json", "reason_summary", []),
    ):
        try:
            item[target] = json.loads(item.pop(source, "") or "")
        except Exception:
            item[target] = default
    if include_results:
        try:
            item["results"] = json.loads(item.pop("results_json", "") or "[]")
        except Exception:
            item["results"] = []
    else:
        item.pop("results_json", None)
        item["results"] = []
    item["level"] = item.pop("object_level", "campaign")
    item["binding_id"] = item.get("binding_id") or ""
    item["dry_run"] = bool(item.get("dry_run"))
    item["log_store"] = "ads_ai"
    return item


def list_actions_page(config, filters=None, limit=50, table=DEFAULT_TABLE):
    config = validate_target(config, "reader", table)
    filters = dict(filters or {})
    where = []
    params = []
    for key in ("product", "binding_id", "action"):
        value = str(filters.get(key) or "").strip()
        if value:
            where.append("%s=%%s" % _identifier(key))
            params.append(value)
    if filters.get("date_from"):
        where.append("created_at>=%s")
        params.append(str(filters["date_from"]))
    if filters.get("date_to"):
        where.append("created_at<=%s")
        params.append(str(filters["date_to"]))
    selected = [key for key in LOG_COLUMNS if key != "results_json"]
    query_limit = max(1, min(MAX_LIST_ROWS, int(limit or 50)))
    sql = "SELECT %s FROM %s%s ORDER BY created_at DESC, action_id DESC LIMIT %%s" % (
        ",".join(_identifier(key) for key in selected),
        QUALIFIED_TABLE,
        (" WHERE " + " AND ".join(where)) if where else "",
    )
    params.append(query_limit + 1)
    conn = _connect(config, "reader", table)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = list(cursor.fetchall())
            return {
                "items": [
                    _decode_row(row, include_results=False)
                    for row in rows[:query_limit]
                ],
                "has_more": len(rows) > query_limit,
                "limit": query_limit,
            }
    finally:
        conn.close()


def list_actions(config, filters=None, limit=50, table=DEFAULT_TABLE):
    return list_actions_page(config, filters, min(200, int(limit or 50)), table)["items"]


def fetch_action(config, action_id, table=DEFAULT_TABLE):
    config = validate_target(config, "reader", table)
    sql = "SELECT %s FROM %s WHERE action_id=%%s LIMIT 1" % (
        ",".join(_identifier(key) for key in LOG_COLUMNS), QUALIFIED_TABLE,
    )
    conn = _connect(config, "reader", table)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (str(action_id or ""),))
            row = cursor.fetchone()
            return _decode_row(row, include_results=True) if row else None
    finally:
        conn.close()

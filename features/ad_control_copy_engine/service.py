"""Safe, dependency-injected execution primitives for ad-control copies.

The module deliberately contains no HTTP or MySQL client.  Production callers
must inject Meta and created-data adapters; tests can therefore prove that
observe mode and disabled feature switches perform no external writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 production fallback
    ZoneInfo = None


LIVE_CONFIRMATION = "ENABLE_LIVE_MODE"
WRITE_ACTIONS = ("pause", "copy")
ROAS_COMPATIBLE_BID_MODES = {
    "LOWEST_COST_WITH_MIN_ROAS",
    "MINIMUM_ROAS",
    "ROAS",
    "VALUE_MIN_ROAS",
}

COPY_INTENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_control_copy_intent (
  intent_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  owner_user_id TEXT NOT NULL DEFAULT '',
  rule_group_id TEXT NOT NULL DEFAULT '',
  rule_id TEXT NOT NULL DEFAULT '',
  account_id TEXT NOT NULL DEFAULT '',
  object_level TEXT NOT NULL DEFAULT 'campaign',
  source_object_id TEXT NOT NULL DEFAULT '',
  source_created_data_id TEXT NOT NULL DEFAULT '',
  account_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'reserved',
  result_json TEXT NOT NULL DEFAULT '{}',
  error_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT NOT NULL DEFAULT ''
)
"""

def ensure_copy_tables(conn: Any) -> None:
    conn.execute(COPY_INTENT_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_control_copy_owner_date "
        "ON ad_control_copy_intent(owner_user_id, account_date, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_control_copy_group_date "
        "ON ad_control_copy_intent(rule_group_id, account_date, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_control_copy_source "
        "ON ad_control_copy_intent(account_id, source_object_id, created_at, status)"
    )


def normalize_account(value: Any) -> str:
    return re.sub(r"^act_", "", str(value or "").strip(), flags=re.I)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _positive_int(value: Any, default: int, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    result = max(1, result)
    return min(result, maximum) if maximum is not None else result


def _rule_id(rule: Mapping[str, Any], index: int) -> str:
    return str(rule.get("rule_id") or rule.get("id") or "rule_%d" % (index + 1))


def normalize_rule_group(
    payload: Mapping[str, Any],
    owner_user_id: str,
    existing: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate the V2 account-only group contract.

    Foreign owner fields are rejected instead of silently accepting delegated
    ownership.  A new group is always disabled and observe-only regardless of
    client input.  Switching an existing group to live needs an exact phrase.
    """

    payload = dict(payload or {})
    owner_user_id = str(owner_user_id or "").strip()
    if not owner_user_id:
        raise ValueError("missing_owner")
    for field in ("owner_user_id", "created_by"):
        delegated = str(payload.get(field) or "").strip()
        if delegated and delegated != owner_user_id:
            raise ValueError("owner_forbidden")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("missing_name")
    accounts: List[str] = []
    seen = set()
    raw_accounts = payload.get("account_ids") or payload.get("accounts") or []
    if not isinstance(raw_accounts, (list, tuple, set)):
        raw_accounts = re.split(r"[,，\s]+", str(raw_accounts or ""))
    for value in raw_accounts:
        account_id = normalize_account(value)
        if account_id and account_id not in seen:
            accounts.append(account_id)
            seen.add(account_id)
    if not accounts:
        raise ValueError("missing_accounts")

    object_level = str(payload.get("object_level") or payload.get("level") or "campaign").lower()
    if object_level not in ("campaign", "ad"):
        raise ValueError("invalid_object_level")
    rules = payload.get("rules") or []
    if not isinstance(rules, list) or not rules:
        raise ValueError("missing_rules")
    normalized_rules = []
    for index, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            raise ValueError("invalid_rule")
        rule = dict(raw_rule)
        action = str(rule.get("action") or "").strip().lower()
        if action in ("close", "stop"):
            action = "pause"
        if action not in WRITE_ACTIONS:
            raise ValueError("invalid_rule_action")
        rule["rule_id"] = _rule_id(rule, index)
        rule["action"] = action
        rule["priority"] = int(rule.get("priority") or index + 1)
        normalized_rules.append(rule)

    is_new = not existing
    old_mode = str((existing or {}).get("run_mode") or "observe").lower()
    requested_mode = str(payload.get("run_mode") or old_mode or "observe").lower()
    if requested_mode not in ("observe", "live"):
        raise ValueError("invalid_run_mode")
    if is_new:
        requested_mode = "observe"
        enabled = False
    else:
        confirmation = payload.get("live_mode_confirm") or payload.get("confirm") or ""
        if old_mode != "live" and requested_mode == "live" and str(confirmation) != LIVE_CONFIRMATION:
            raise ValueError("live_mode_confirm_required")
        # Save is configuration-only. Activation and deactivation must pass the
        # dedicated endpoint's preview, token, owner and confirmation gates.
        enabled = bool(existing.get("enabled", False))
    # The Ad phase is configuration-only in this release.  Never allow an
    # update payload (or a Campaign -> Ad edit) to bypass the enable endpoint's
    # phase gate by persisting an enabled Ad group directly.
    if object_level == "ad":
        enabled = False

    strategy = dict((existing or {}).get("strategy") or {})
    payload_strategy = dict(payload.get("strategy") or {})
    strategy.update(payload_strategy)
    # Direct API clients may send these group-level fields at the top level.
    # The UI already sends them under strategy; an explicit strategy value
    # wins so round-trips never silently discard either contract form.
    for key in ("schedule", "limits", "candidate_selection"):
        if key not in payload_strategy and key in payload:
            value = payload.get(key)
            strategy[key] = dict(value) if isinstance(value, Mapping) else value
    validate_group_configuration(strategy, normalized_rules)

    return {
        "group_id": str(payload.get("group_id") or (existing or {}).get("group_id") or uuid.uuid4().hex),
        "name": name,
        "owner_user_id": owner_user_id,
        "account_ids": accounts,
        "object_level": object_level,
        "run_mode": requested_mode,
        "rules": normalized_rules,
        "strategy": strategy,
        "enabled": enabled,
    }


def evaluate_rule_actions(
    item: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any]],
    match_condition: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
) -> Dict[str, Any]:
    """Resolve one write action; pause always wins and losers are shadowed."""

    matched = []
    drama_scope_failures = []
    for index, rule in enumerate(rules or []):
        if not isinstance(rule, Mapping) or rule.get("enabled") is False:
            continue
        conditions = rule.get("conditions") if isinstance(rule.get("conditions"), list) else []
        if conditions and not all(match_condition(item, condition) for condition in conditions):
            continue
        action = str(rule.get("action") or "").lower()
        if action in ("close", "stop"):
            action = "pause"
        # Legacy observe rules remain annotations, not V2 actions.
        if action not in ("pause", "copy", "observe"):
            continue
        if action == "copy":
            drama_match, drama_reason = match_drama_scope(item, rule.get("drama_scope"))
            if not drama_match:
                drama_scope_failures.append({"rule_id": _rule_id(rule, index), "reason": drama_reason})
                continue
        matched.append({
            "rule_id": _rule_id(rule, index),
            "name": str(rule.get("name") or ""),
            "action": action,
            "priority": int(rule.get("priority") or index + 1),
            "rule": dict(rule),
        })
    write_matches = [match for match in matched if match["action"] in WRITE_ACTIONS]
    write_matches.sort(key=lambda match: (
        0 if match["action"] == "pause" else 1,
        match["priority"],
        match["rule_id"],
    ))
    winner = write_matches[0] if write_matches else None
    rendered = []
    for match in matched:
        rendered_match = {key: match[key] for key in ("rule_id", "name", "action", "priority")}
        if winner and match["rule_id"] != winner["rule_id"]:
            rendered_match["status"] = "shadowed"
            rendered_match["shadowed_by_rule"] = winner["rule_id"]
        else:
            rendered_match["status"] = "selected" if winner else "observed"
        rendered.append(rendered_match)
    return {
        "matched_rules": rendered,
        "target_action": winner["action"] if winner else ("observe" if matched else "none"),
        "target_rule_id": winner["rule_id"] if winner else "",
        "target_rule": winner["rule"] if winner else {},
        "shadowed_count": len([item for item in rendered if item.get("status") == "shadowed"]),
        "drama_scope_failures": drama_scope_failures,
    }


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19 if "H" in fmt else 10], fmt)
        except Exception:
            continue
    return None


def match_drama_scope(item: Mapping[str, Any], raw_scope: Any, now: Optional[datetime] = None) -> tuple:
    scope = raw_scope if isinstance(raw_scope, Mapping) else {"type": str(raw_scope or "all")}
    scope_type = str(scope.get("type") or scope.get("mode") or "all").lower()
    if scope_type == "all":
        return True, ""
    if item.get("drama_mapping_reason"):
        return False, str(item.get("drama_mapping_reason"))
    content_id = str(item.get("content_id") or "").strip()
    series_code = str(item.get("series_code") or "").strip()
    if not content_id and not series_code:
        return False, "missing_drama_identity"
    if scope_type in ("specified", "specific"):
        requested = {
            str(value or "").strip()
            for value in (
                scope.get("drama_ids") or scope.get("values") or scope.get("ids")
                or scope.get("content_ids") or scope.get("series_codes") or []
            )
            if str(value or "").strip()
        }
        if not requested:
            return False, "missing_specified_drama_ids"
        return (bool({content_id, series_code} & requested), "drama_not_selected")
    if scope_type in ("recent_days", "recent"):
        deployed = _parse_datetime(item.get("deploy_time") or item.get("published_at"))
        if not deployed:
            return False, "missing_drama_deploy_time"
        days = _positive_int(scope.get("days"), 1, 365)
        now_value = now or datetime.utcnow()
        if deployed.tzinfo and not now_value.tzinfo:
            now_value = now_value.replace(tzinfo=timezone.utc)
        if now_value.tzinfo and not deployed.tzinfo:
            deployed = deployed.replace(tzinfo=timezone.utc)
        return (deployed >= now_value - timedelta(days=days), "drama_outside_recent_days")
    return False, "invalid_drama_scope"


def _fixed_timezone(value: Any) -> Optional[timezone]:
    text = str(value or "").strip()
    match = re.match(r"^(?:UTC|GMT)?\s*([+-]?\d{1,2})(?::?(\d{2}))?$", text, re.I)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if abs(hours) > 14 or minutes > 59:
        return None
    sign = 1 if hours >= 0 else -1
    return timezone(timedelta(hours=hours, minutes=sign * minutes))


def account_now(now_utc: datetime, timezone_name: Any) -> datetime:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    tz = _fixed_timezone(timezone_name)
    if tz is None and ZoneInfo and timezone_name:
        try:
            tz = ZoneInfo(str(timezone_name))
        except Exception:
            tz = None
    if tz is None:
        raise ValueError("unknown_account_timezone")
    return now_utc.astimezone(tz)


def _parse_hhmm(value: Any) -> Optional[tuple]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if hour <= 23 and minute <= 59 else None


def inside_execution_window(local_now: datetime, config: Mapping[str, Any]) -> bool:
    start = _parse_hhmm(config.get("allowed_after") or config.get("start_time"))
    end = _parse_hhmm(config.get("allowed_before") or config.get("end_time"))
    value = (local_now.hour, local_now.minute)
    if start and end:
        if start == end:
            return True
        if start < end:
            return start <= value <= end
        return value >= start or value <= end
    if start:
        return value >= start
    if end:
        return value <= end
    return True


def schedule_due(
    strategy: Mapping[str, Any],
    account_time_zone: Any,
    now_utc: Optional[datetime] = None,
) -> tuple:
    """Evaluate a group schedule in the selected account's real timezone.

    The runner may tick on server time, but an ``account`` schedule must never
    fall back to that server timezone.  A five-minute eligibility bucket keeps
    scheduled scans idempotent with the runner cadence.
    """

    strategy = dict(strategy or {})
    schedule = strategy.get("schedule") if isinstance(strategy.get("schedule"), Mapping) else strategy
    timezone_name = schedule.get("timezone") or strategy.get("execute_timezone") or "account"
    effective_timezone = account_time_zone if str(timezone_name).lower() == "account" else timezone_name
    try:
        local_now = account_now(now_utc or datetime.now(timezone.utc), effective_timezone)
    except ValueError:
        return False, "unknown_account_timezone"
    window = {
        "allowed_after": schedule.get("allowed_start") or schedule.get("allowed_after"),
        "allowed_before": schedule.get("allowed_end") or schedule.get("allowed_before"),
    }
    if not inside_execution_window(local_now, window):
        return False, "outside_execution_window"
    schedule_type = str(schedule.get("type") or "fixed_time").lower()
    now_minutes = local_now.hour * 60 + local_now.minute
    if schedule_type == "interval":
        interval = _positive_int(schedule.get("interval_minutes"), 60, 1440)
        interval = max(5, interval)
        return (True, "") if now_minutes % interval < 5 else (False, "outside_interval")
    target = _parse_hhmm(
        schedule.get("time")
        or strategy.get("execute_time")
        or strategy.get("close_time")
        or strategy.get("pause_time")
    )
    if not target:
        return False, "missing_execute_time"
    target_minutes = target[0] * 60 + target[1]
    return (True, "") if (now_minutes - target_minutes) % 1440 < 5 else (False, "outside_fixed_time")


def normalize_budget_config(copy_config: Mapping[str, Any]) -> Dict[str, Any]:
    budget = dict(copy_config.get("budget") if isinstance(copy_config.get("budget"), Mapping) else copy_config)
    mode = str(
        budget.get("mode") or budget.get("type")
        or budget.get("budget_strategy") or "source_ratio"
    ).lower()
    if mode == "actual_cpi_multiplier":
        budget["mode"] = "x_cpi"
        budget["multiplier"] = budget.get("multiplier") or budget.get("x")
    elif mode == "fixed_target_cpi_multiplier":
        budget["mode"] = "x_cpi"
        budget["fixed_cpi"] = budget.get("fixed_cpi") or budget.get("target_cpi")
        budget["multiplier"] = budget.get("multiplier") or budget.get("x")
    elif mode == "source_budget_ratio":
        budget["mode"] = "source_ratio"
        budget["ratio"] = budget.get("ratio") or budget.get("source_ratio")
    return budget


def compute_budget(candidate: Mapping[str, Any], copy_config: Mapping[str, Any]) -> Decimal:
    budget = normalize_budget_config(copy_config)
    mode = str(budget.get("mode") or "source_ratio").lower()
    budget_type = str(candidate.get("budget_type") or budget.get("budget_type") or "").lower()
    if budget_type not in ("daily_budget", "lifetime_budget"):
        raise ValueError("unknown_budget_type")
    if mode == "x_cpi":
        cpi = _decimal(budget.get("fixed_cpi") or candidate.get("cpi"))
        if cpi <= 0:
            spend = _decimal(candidate.get("spend") or (candidate.get("metrics") or {}).get("spend"))
            installs = _decimal(candidate.get("install") or (candidate.get("metrics") or {}).get("install"))
            if installs <= 0:
                raise ValueError("missing_cpi")
            cpi = spend / installs
        # CPI is expressed in the account's major currency unit. Meta budget
        # fields use the account currency's minor-unit offset, which is not
        # universally 100 and therefore must come from account metadata.
        currency_offset = _decimal(candidate.get("currency_offset") or budget.get("currency_offset"))
        if currency_offset <= 0 or currency_offset != currency_offset.to_integral_value():
            raise ValueError("unknown_currency_offset")
        value = cpi * _decimal(budget.get("multiplier") or budget.get("x") or "1") * currency_offset
    elif mode == "source_ratio":
        source_budget = _decimal(candidate.get("source_budget") or candidate.get("budget"))
        if source_budget <= 0 and str(candidate.get("budget_level") or "").upper() in ("ABO", "ADSET"):
            by_adset = {}
            for row in candidate.get("source_created_rows") or []:
                adset_id = str((row or {}).get("adset_id") or "")
                row_budget = _decimal((row or {}).get("adset_budget") or (row or {}).get("budget"))
                if adset_id and row_budget > 0:
                    by_adset[adset_id] = max(by_adset.get(adset_id, Decimal("0")), row_budget)
            source_budget = sum(by_adset.values(), Decimal("0"))
        if source_budget <= 0:
            raise ValueError("missing_source_budget")
        value = source_budget * _decimal(budget.get("ratio") or "1")
    else:
        raise ValueError("invalid_budget_mode")
    if value <= 0:
        raise ValueError("invalid_copy_budget")
    maximum = _decimal(budget.get("max_budget"))
    if maximum > 0:
        value = min(value, maximum)
    minimum = _decimal(budget.get("min_budget"))
    if minimum > 0:
        value = max(value, minimum)
    # Meta accepts an integer in the currency's minimum unit.
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def compute_budget_adjustments(candidate: Mapping[str, Any], copy_config: Mapping[str, Any]) -> Dict[str, Any]:
    total_budget = int(compute_budget(candidate, copy_config))
    level = str(candidate.get("budget_level") or "").upper()
    budget_type = str(candidate.get("budget_type") or normalize_budget_config(copy_config).get("budget_type") or "")
    if level in ("CBO", "CAMPAIGN"):
        return {"budget_level": "CBO", "budget_type": budget_type, "campaign_budget": total_budget}
    if level not in ("ABO", "ADSET"):
        raise ValueError("unknown_budget_level")
    rows = candidate.get("source_created_rows") or []
    source_by_adset: Dict[str, Decimal] = {}
    for row in rows:
        adset_id = str((row or {}).get("adset_id") or "")
        source_budget = _decimal((row or {}).get("adset_budget") or (row or {}).get("budget"))
        if adset_id and source_budget > 0:
            source_by_adset[adset_id] = max(source_by_adset.get(adset_id, Decimal("0")), source_budget)
    total_source = sum(source_by_adset.values(), Decimal("0"))
    if not source_by_adset or total_source <= 0:
        raise ValueError("missing_source_adset_budgets")
    ordered = sorted(source_by_adset)
    allocated: Dict[str, int] = {}
    used = 0
    for adset_id in ordered[:-1]:
        value = int((Decimal(total_budget) * source_by_adset[adset_id] / total_source).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        allocated[adset_id] = value
        used += value
    allocated[ordered[-1]] = total_budget - used
    if any(value <= 0 for value in allocated.values()):
        raise ValueError("invalid_adset_budget_allocation")
    return {"budget_level": "ABO", "budget_type": budget_type, "adset_budgets": allocated}


def compute_roas_floor(candidate: Mapping[str, Any], copy_config: Mapping[str, Any]) -> Optional[Decimal]:
    if isinstance(copy_config.get("roas"), Mapping):
        roas_config = copy_config.get("roas")
    elif isinstance(copy_config.get("roas_bid"), Mapping):
        roas_config = copy_config.get("roas_bid")
    else:
        roas_config = copy_config
    raw_change = roas_config.get("roas_change_pct")
    if raw_change in (None, "") and roas_config.get("percent") not in (None, ""):
        raw_change = roas_config.get("percent")
        if str(roas_config.get("direction") or "increase").lower() in ("decrease", "lower", "down", "降低"):
            raw_change = -abs(_decimal(raw_change))
    if raw_change in (None, "", 0, "0"):
        return None
    bid_mode = str(candidate.get("bid_strategy") or candidate.get("bid_type") or "").upper()
    if not candidate.get("roas_bid_compatible") and bid_mode not in ROAS_COMPATIBLE_BID_MODES:
        raise ValueError("unsupported_roas_bid_strategy")
    source_floor = _decimal(candidate.get("source_roas_floor") or candidate.get("bid_amount"))
    if source_floor <= 0:
        raise ValueError("missing_source_roas_floor")
    multiplier = Decimal("1") + (_decimal(raw_change) / Decimal("100"))
    if multiplier <= 0:
        raise ValueError("invalid_roas_change")
    return (source_floor * multiplier).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def select_top_candidates(candidates: Iterable[Mapping[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        item = dict(candidate)
        grouped.setdefault(normalize_account(item.get("account_id")), []).append(item)
    selected = []
    for account_id in sorted(grouped):
        ranked = sorted(
            grouped[account_id],
            key=lambda item: (
                -_decimal(item.get("roas_pct") or (item.get("metrics") or {}).get("roas_pct")),
                -_decimal(item.get("spend") or (item.get("metrics") or {}).get("spend")),
                str(item.get("campaign_id") or item.get("object_id") or ""),
            ),
        )
        selected.extend(ranked[:top_n])
    return selected


def _merge_mapping(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = dict(value) if isinstance(value, Mapping) else value
    return merged


def target_rule(group: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[str, Any]:
    wanted = str(candidate.get("target_rule_id") or "")
    for index, rule in enumerate(group.get("rules") or []):
        if isinstance(rule, Mapping) and _rule_id(rule, index) == wanted:
            return dict(rule)
    embedded = candidate.get("target_rule")
    return dict(embedded) if isinstance(embedded, Mapping) else {}


def copy_config_for_candidate(group: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge group copy defaults with the matched rule's copy parameters."""

    strategy = dict(group.get("strategy") or {})
    defaults = strategy.get("copy") if isinstance(strategy.get("copy"), Mapping) else {}
    config = dict(defaults)
    limits = strategy.get("limits") if isinstance(strategy.get("limits"), Mapping) else {}
    for key, value in limits.items():
        config.setdefault(key, value)
    if "candidate_selection" in strategy:
        config.setdefault("candidate_selection", strategy.get("candidate_selection"))

    rule = target_rule(group, candidate)
    rule_copy = rule.get("copy") if isinstance(rule.get("copy"), Mapping) else {}
    config = _merge_mapping(config, rule_copy)
    # These fields are accepted directly on each rule by the API contract.
    for key in (
        "candidate_selection", "top_n_per_account", "daily_rule_limit",
        "daily_user_limit", "source_cooldown_days", "budget", "roas",
        "allowed_after", "allowed_before", "allowed_start", "allowed_end",
    ):
        if key in rule:
            value = rule.get(key)
            if isinstance(value, Mapping) and isinstance(config.get(key), Mapping):
                config[key] = _merge_mapping(config[key], value)
            else:
                config[key] = dict(value) if isinstance(value, Mapping) else value
    return config


def candidate_selection(copy_config: Mapping[str, Any]) -> tuple:
    raw = copy_config.get("candidate_selection")
    selection = dict(raw) if isinstance(raw, Mapping) else {}
    mode = str(selection.get("mode") or selection.get("type") or raw or "").strip().lower()
    raw_top_n = (
        selection.get("top_n_per_account")
        or selection.get("top_n")
        or copy_config.get("top_n_per_account")
    )
    if not mode:
        mode = "top_n" if raw_top_n not in (None, "") else "all"
    if mode in ("all", "every", "全部"):
        return "all", None
    if mode not in ("top_n", "top_n_per_account", "topn", "top"):
        raise ValueError("invalid_candidate_selection")
    return "top_n", _positive_int(raw_top_n, 1, 100)


def _strict_decimal(value: Any, error_code: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(error_code)


def _validate_schedule(schedule: Any) -> None:
    if schedule in (None, "", {}):
        return
    if not isinstance(schedule, Mapping):
        raise ValueError("invalid_schedule")
    schedule_type = str(schedule.get("type") or "fixed_time").lower()
    if schedule_type not in ("fixed_time", "interval"):
        raise ValueError("invalid_schedule")
    if schedule_type == "fixed_time" and not _parse_hhmm(schedule.get("time")):
        raise ValueError("invalid_schedule")
    if schedule_type == "interval":
        try:
            interval = int(schedule.get("interval_minutes"))
        except (TypeError, ValueError):
            raise ValueError("invalid_schedule")
        if interval < 5 or interval > 1440:
            raise ValueError("invalid_schedule")
    start = schedule.get("allowed_start") or schedule.get("allowed_after")
    end = schedule.get("allowed_end") or schedule.get("allowed_before")
    if bool(start) != bool(end) or (start and (not _parse_hhmm(start) or not _parse_hhmm(end))):
        raise ValueError("invalid_schedule")


def _validate_limits(limits: Any, copy_config: Optional[Mapping[str, Any]] = None) -> None:
    if limits in (None, "", {}):
        limits = {}
    if not isinstance(limits, Mapping):
        raise ValueError("invalid_limits")
    values = dict(copy_config or {})
    values.update(dict(limits))
    hard_limit = _positive_int(os.environ.get("AD_CONTROL_COPY_DAILY_HARD_LIMIT"), 50, 10000)
    for aliases in (
        ("rule_daily_limit", "per_rule_daily", "daily_rule_limit"),
        ("user_daily_limit", "per_user_daily", "daily_user_limit"),
    ):
        raw = next((values.get(key) for key in aliases if values.get(key) not in (None, "")), None)
        if raw is None:
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise ValueError("invalid_limits")
        if number < 1 or number > hard_limit:
            raise ValueError("invalid_limits")
    cooldown = values.get("source_cooldown_days")
    if cooldown not in (None, ""):
        try:
            cooldown = int(cooldown)
        except (TypeError, ValueError):
            raise ValueError("invalid_limits")
        if cooldown < 0 or cooldown > 365:
            raise ValueError("invalid_limits")


def _validate_candidate_config(selection: Any, top_n: Any = None) -> None:
    if selection in (None, "", {}):
        if top_n in (None, "", 0, "0"):
            return
        selection = {"mode": "top_n_per_account", "top_n": top_n}
    config = dict(selection) if isinstance(selection, Mapping) else {"mode": selection}
    mode = str(config.get("mode") or config.get("type") or "").lower()
    if mode not in ("all", "top_n", "top_n_per_account", "topn", "top"):
        raise ValueError("invalid_candidate_selection")
    if mode != "all":
        raw_top_n = config.get("top_n") or config.get("top_n_per_account") or top_n
        try:
            number = int(raw_top_n)
        except (TypeError, ValueError):
            raise ValueError("invalid_candidate_selection")
        if number < 1 or number > 50:
            raise ValueError("invalid_candidate_selection")


def _validate_drama_scope(scope: Any) -> None:
    if scope in (None, "", {}):
        return
    config = dict(scope) if isinstance(scope, Mapping) else {"type": scope}
    scope_type = str(config.get("type") or "all").lower()
    if scope_type not in ("all", "recent", "recent_days", "specified", "selected"):
        raise ValueError("invalid_drama_scope")
    if scope_type in ("recent", "recent_days"):
        try:
            days = int(config.get("days") or config.get("recent_days"))
        except (TypeError, ValueError):
            raise ValueError("invalid_drama_scope")
        if days < 1 or days > 365:
            raise ValueError("invalid_drama_scope")
    if scope_type in ("specified", "selected"):
        values = (
            config.get("drama_ids") or config.get("values") or config.get("ids")
            or config.get("content_ids") or config.get("series_codes") or []
        )
        if not isinstance(values, (list, tuple, set)) or not any(str(value or "").strip() for value in values):
            raise ValueError("invalid_drama_scope")


def _validate_copy_config(copy_config: Any) -> None:
    if copy_config in (None, "", {}):
        return
    if not isinstance(copy_config, Mapping):
        raise ValueError("invalid_copy_budget")
    budget = copy_config.get("budget")
    if budget not in (None, "", {}):
        if not isinstance(budget, Mapping):
            raise ValueError("invalid_copy_budget")
        raw_mode = str(budget.get("mode") or budget.get("type") or budget.get("budget_strategy") or "").lower()
        if raw_mode not in (
            "x_cpi", "actual_cpi_multiplier", "fixed_target_cpi_multiplier",
            "source_ratio", "source_budget_ratio",
        ):
            raise ValueError("invalid_copy_budget")
        multiplier = budget.get("multiplier") or budget.get("x")
        if raw_mode in ("x_cpi", "actual_cpi_multiplier", "fixed_target_cpi_multiplier"):
            if _strict_decimal(multiplier, "invalid_copy_budget") <= 0:
                raise ValueError("invalid_copy_budget")
        if raw_mode == "fixed_target_cpi_multiplier":
            if _strict_decimal(budget.get("fixed_cpi") or budget.get("target_cpi"), "invalid_copy_budget") <= 0:
                raise ValueError("invalid_copy_budget")
        if raw_mode in ("source_ratio", "source_budget_ratio"):
            ratio = _strict_decimal(budget.get("ratio") or budget.get("source_ratio"), "invalid_copy_budget")
            if ratio <= 0 or ratio > 10:
                raise ValueError("invalid_copy_budget")
    roas = copy_config.get("roas") if isinstance(copy_config.get("roas"), Mapping) else copy_config.get("roas_bid")
    if roas not in (None, "", {}):
        if not isinstance(roas, Mapping):
            raise ValueError("invalid_roas_adjustment")
        direction = str(roas.get("direction") or "increase").lower()
        if direction not in ("increase", "decrease", "raise", "lower", "up", "down", "提高", "降低"):
            raise ValueError("invalid_roas_adjustment")
        percent = _strict_decimal(roas.get("percent") if roas.get("percent") is not None else 0, "invalid_roas_adjustment")
        if percent < 0 or percent > 100:
            raise ValueError("invalid_roas_adjustment")


def validate_group_configuration(strategy: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> None:
    strategy = dict(strategy or {})
    _validate_schedule(strategy.get("schedule"))
    defaults = strategy.get("copy") if isinstance(strategy.get("copy"), Mapping) else {}
    _validate_limits(strategy.get("limits"), defaults)
    _validate_candidate_config(strategy.get("candidate_selection"), defaults.get("top_n_per_account"))
    _validate_drama_scope(strategy.get("drama_scope") or defaults.get("drama_scope"))
    _validate_copy_config(defaults)
    for rule in rules or []:
        if str(rule.get("action") or "").lower() != "copy":
            continue
        _validate_candidate_config(rule.get("candidate_selection"), rule.get("top_n_per_account"))
        _validate_drama_scope(rule.get("drama_scope"))
        _validate_copy_config(rule.get("copy") or rule.get("copy_config"))


def apply_copy_candidate_selection(
    group: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Annotate unselected copy candidates without mixing independent rules."""

    items = [dict(candidate) for candidate in candidates]
    copy_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        if str(item.get("target_action") or "").lower() == "copy":
            copy_groups.setdefault(str(item.get("target_rule_id") or ""), []).append(item)
    selected_keys = set()
    for rule_id, rule_items in copy_groups.items():
        config = copy_config_for_candidate(group, rule_items[0])
        mode, top_n = candidate_selection(config)
        selected = rule_items if mode == "all" else select_top_candidates(rule_items, int(top_n or 1))
        for item in selected:
            selected_keys.add((
                rule_id,
                normalize_account(item.get("account_id")),
                str(item.get("object_id") or item.get("campaign_id") or item.get("ad_id") or ""),
                str(item.get("product") or ""),
            ))
    for item in items:
        if str(item.get("target_action") or "").lower() != "copy":
            continue
        key = (
            str(item.get("target_rule_id") or ""),
            normalize_account(item.get("account_id")),
            str(item.get("object_id") or item.get("campaign_id") or item.get("ad_id") or ""),
            str(item.get("product") or ""),
        )
        if key not in selected_keys:
            item["target_action"] = "none"
            item["candidate_selection_reason"] = "outside_top_n"
            item["skip_reason"] = "outside_top_n"
    return items


def deduplicate_account_product_campaigns(
    whitelists: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
) -> tuple:
    """Keep each Meta Campaign unique inside an account-only group.

    Product-scoped token and business metadata cannot be guessed when the same
    Campaign is attributed to more than one product, so ambiguous rows are
    removed before any Meta scan and returned as explicit fail-closed errors.
    """

    clean: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    errors: List[Dict[str, Any]] = []
    for raw_account_id, products in (whitelists or {}).items():
        account_id = normalize_account(raw_account_id)
        campaign_products: Dict[str, List[str]] = {}
        for product, campaigns in (products or {}).items():
            for campaign_id in (campaigns or {}).keys():
                campaign_products.setdefault(str(campaign_id), []).append(str(product))
        ambiguous = {
            campaign_id: sorted(set(product_names))
            for campaign_id, product_names in campaign_products.items()
            if len(set(product_names)) != 1
        }
        for campaign_id, product_names in sorted(ambiguous.items()):
            errors.append({
                "reason": "ambiguous_source_product",
                "account_id": account_id,
                "campaign_id": campaign_id,
                "products": product_names,
            })
        for product, campaigns in (products or {}).items():
            for campaign_id, value in (campaigns or {}).items():
                if str(campaign_id) in ambiguous:
                    continue
                clean.setdefault(account_id, {}).setdefault(str(product), {})[str(campaign_id)] = dict(value or {})
    return clean, errors


def idempotency_key(group: Mapping[str, Any], candidate: Mapping[str, Any], local_date: str) -> str:
    raw = "|".join([
        str(group.get("owner_user_id") or group.get("created_by") or ""),
        str(group.get("group_id") or ""),
        normalize_account(candidate.get("account_id")),
        str(candidate.get("object_level") or group.get("object_level") or "campaign"),
        str(candidate.get("object_id") or candidate.get("campaign_id") or candidate.get("ad_id") or ""),
        str(local_date),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SQLiteCopyIntentStore:
    """Atomic idempotency/quota store; connection creation is injected."""

    ACTIVE_STATUSES = ("reserved", "meta_created", "ledger_written", "activated", "completed")

    def __init__(self, connection_factory: Callable[[], Any]):
        self.connection_factory = connection_factory

    def reserve(self, intent: Mapping[str, Any], limits: Mapping[str, int], cooldown_days: int) -> Dict[str, Any]:
        conn = self.connection_factory()
        try:
            ensure_copy_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM ad_control_copy_intent WHERE idempotency_key=?",
                (intent["idempotency_key"],),
            ).fetchone()
            if existing:
                conn.rollback()
                existing_payload = dict(existing)
                try:
                    existing_payload["result"] = json.loads(existing_payload.get("result_json") or "{}")
                except Exception:
                    existing_payload["result"] = {}
                return {
                    "ok": False,
                    "reason": "duplicate_intent",
                    "intent_id": existing["intent_id"],
                    "existing": existing_payload,
                }
            placeholders = ",".join("?" for _ in self.ACTIVE_STATUSES)
            base_params = (intent["account_date"],) + self.ACTIVE_STATUSES
            user_count = conn.execute(
                "SELECT COUNT(*) FROM ad_control_copy_intent WHERE owner_user_id=? AND account_date=? AND status IN (%s)" % placeholders,
                (intent["owner_user_id"],) + base_params,
            ).fetchone()[0]
            group_count = conn.execute(
                "SELECT COUNT(*) FROM ad_control_copy_intent WHERE owner_user_id=? AND rule_group_id=? "
                "AND rule_id=? AND account_date=? AND status IN (%s)" % placeholders,
                (intent["owner_user_id"], intent["rule_group_id"], intent["rule_id"]) + base_params,
            ).fetchone()[0]
            total_count = conn.execute(
                "SELECT COUNT(*) FROM ad_control_copy_intent WHERE account_date=? AND status IN (%s)" % placeholders,
                base_params,
            ).fetchone()[0]
            if user_count >= int(limits["user"]):
                conn.rollback()
                return {"ok": False, "reason": "user_daily_limit"}
            if group_count >= int(limits["rule"]):
                conn.rollback()
                return {"ok": False, "reason": "rule_daily_limit"}
            if total_count >= int(limits["hard"]):
                conn.rollback()
                return {"ok": False, "reason": "copy_daily_hard_limit"}
            cutoff = (intent["now_utc"] - timedelta(days=max(0, cooldown_days))).strftime("%Y-%m-%d %H:%M:%S")
            recent = conn.execute(
                "SELECT intent_id FROM ad_control_copy_intent WHERE account_id=? AND source_object_id=? "
                "AND created_at>=? AND status IN (%s) LIMIT 1" % placeholders,
                (intent["account_id"], intent["source_object_id"], cutoff) + self.ACTIVE_STATUSES,
            ).fetchone()
            if recent:
                conn.rollback()
                return {"ok": False, "reason": "source_cooldown", "intent_id": recent["intent_id"]}
            conn.execute(
                """
                INSERT INTO ad_control_copy_intent (
                  intent_id,idempotency_key,owner_user_id,rule_group_id,rule_id,account_id,
                  object_level,source_object_id,source_created_data_id,account_date,status,
                  created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?, 'reserved', ?, ?)
                """,
                (
                    intent["intent_id"], intent["idempotency_key"], intent["owner_user_id"],
                    intent["rule_group_id"], intent["rule_id"], intent["account_id"],
                    intent["object_level"], intent["source_object_id"],
                    intent.get("source_created_data_id", ""), intent["account_date"],
                    intent["now_utc"].strftime("%Y-%m-%d %H:%M:%S"),
                    intent["now_utc"].strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            return {"ok": True, "intent_id": intent["intent_id"]}
        finally:
            conn.close()

    def update(self, intent_id: str, status: str, result: Optional[Mapping[str, Any]] = None, reason: str = "") -> None:
        conn = self.connection_factory()
        try:
            ensure_copy_tables(conn)
            conn.execute(
                "UPDATE ad_control_copy_intent SET status=?,result_json=?,error_reason=?,updated_at=CURRENT_TIMESTAMP,"
                "completed_at=CASE WHEN ? IN ('completed','failed','quarantined') THEN CURRENT_TIMESTAMP ELSE completed_at END "
                "WHERE intent_id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), reason, status, intent_id),
            )
            conn.commit()
        finally:
            conn.close()

@dataclass
class CopyEngineConfig:
    copy_enabled: bool = False
    ad_copy_enabled: bool = False
    daily_hard_limit: int = 50
    user_daily_limit: int = 10
    source_cooldown_days: int = 1
    graph_version: str = "v25.0"

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "CopyEngineConfig":
        env = environ or os.environ
        return cls(
            copy_enabled=str(env.get("AD_CONTROL_COPY_ENABLED", "0")).strip() == "1",
            ad_copy_enabled=str(env.get("AD_CONTROL_AD_COPY_ENABLED", "0")).strip() == "1",
            daily_hard_limit=_positive_int(env.get("AD_CONTROL_COPY_DAILY_HARD_LIMIT"), 50),
            user_daily_limit=_positive_int(env.get("AD_CONTROL_COPY_USER_DAILY_LIMIT"), 10),
            source_cooldown_days=max(0, int(env.get("AD_CONTROL_COPY_SOURCE_COOLDOWN_DAYS", "1") or 1)),
            graph_version=str(env.get("AD_CONTROL_COPY_GRAPH_VERSION", "v25.0") or "v25.0").strip(),
        )


class FacebookCampaignCopyAdapter:
    """Meta adapter using an injected HTTP transport.

    ``transport`` receives ``(method, graph_version, path, params)``.  The copy
    response is not trusted for descendants: adsets/ads are read back from the
    copied Campaign and mapped through Meta's source object fields.
    """

    def __init__(
        self,
        transport: Callable[[str, str, str, Mapping[str, Any]], Mapping[str, Any]],
        graph_version: str = "v25.0",
        poll_interval_seconds: float = 1.0,
        poll_timeout_seconds: float = 30.0,
        monotonic: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self.transport = transport
        self.graph_version = graph_version or "v25.0"
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.poll_timeout_seconds = max(self.poll_interval_seconds, float(poll_timeout_seconds))
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep

    def _call(self, method: str, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.transport(method, self.graph_version, path, dict(params or {}))
        if not isinstance(result, Mapping):
            raise RuntimeError("invalid_meta_response")
        return result

    @staticmethod
    def _items(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        data = payload.get("data")
        return [item for item in data if isinstance(item, Mapping)] if isinstance(data, list) else []

    @staticmethod
    def _copy_object_id(item: Mapping[str, Any]) -> str:
        return str(item.get("copied_campaign_id") or item.get("campaign_id") or item.get("id") or "")

    def _poll_copy_completion(self, source_campaign_id: str, response: Mapping[str, Any]) -> str:
        expected_id = str(response.get("copied_campaign_id") or response.get("id") or "")
        copy_session_id = str(response.get("copy_session_id") or "")
        if not expected_id and not copy_session_id:
            raise RuntimeError("missing_copy_tracking_id")
        deadline = self.monotonic() + self.poll_timeout_seconds
        while True:
            payload = self._call("GET", "/%s/copies" % source_campaign_id, {
                "fields": "id,copied_campaign_id,campaign_id,copy_session_id,is_completed,status",
                "limit": 100,
            })
            for item in self._items(payload):
                item_id = self._copy_object_id(item)
                item_session = str(item.get("copy_session_id") or "")
                if expected_id and item_id != expected_id:
                    continue
                if copy_session_id and item_session != copy_session_id:
                    continue
                completed = item.get("is_completed")
                if completed is None:
                    completed = str(item.get("status") or "").upper() in ("COMPLETED", "SUCCESS")
                if completed is True or str(completed).strip().lower() in ("1", "true", "yes"):
                    if not item_id:
                        raise RuntimeError("missing_copied_campaign_id")
                    return item_id
            if self.monotonic() >= deadline:
                raise RuntimeError("copy_poll_timeout")
            self.sleeper(self.poll_interval_seconds)

    def deep_copy_campaign(self, account_id: str, campaign_id: str, deep_copy: bool, status_option: str, adjustments: Mapping[str, Any]) -> Dict[str, Any]:
        parameter_overrides: Dict[str, Any] = {}
        if adjustments.get("budget_level") == "CBO":
            parameter_overrides[str(adjustments.get("budget_type"))] = int(adjustments["campaign_budget"])
        response = self._call("POST", "/%s/copies" % campaign_id, {
            "deep_copy": bool(deep_copy),
            "status_option": status_option,
            "parameter_overrides": parameter_overrides,
        })
        copied_id = self._poll_copy_completion(str(campaign_id), response)
        campaign = self._call("GET", "/%s" % copied_id, {"fields": "id,name,status,effective_status,account_id,daily_budget,lifetime_budget"})
        if normalize_account(campaign.get("account_id")) != normalize_account(account_id):
            raise RuntimeError("copied_campaign_account_mismatch")
        if str(campaign.get("status") or campaign.get("effective_status") or "").upper() != "PAUSED":
            raise RuntimeError("copied_campaign_not_paused")
        if adjustments.get("budget_level") == "CBO":
            actual_budget = campaign.get(str(adjustments.get("budget_type")))
            if int(actual_budget or 0) != int(adjustments.get("campaign_budget") or 0):
                raise RuntimeError("copied_campaign_budget_mismatch")
        adsets = self._items(self._call("GET", "/%s/adsets" % copied_id, {
            "fields": "id,name,status,effective_status,source_adset_id,daily_budget,lifetime_budget,bid_strategy,bid_constraints",
            "limit": 500,
        }))
        ads = self._items(self._call("GET", "/%s/ads" % copied_id, {
            "fields": "id,name,status,effective_status,adset_id,source_ad_id,creative{id}",
            "limit": 1000,
        }))
        normalized_adsets = []
        source_adsets = set()
        for item in adsets:
            source_id = str(item.get("source_adset_id") or "")
            new_id = str(item.get("id") or "")
            if not source_id or not new_id or source_id in source_adsets:
                raise RuntimeError("copy_mapping_incomplete")
            source_adsets.add(source_id)
            normalized = dict(item)
            normalized["source_adset_id"] = source_id
            normalized["adset_id"] = new_id
            normalized_adsets.append(normalized)
        normalized_ads = []
        source_ads = set()
        for item in ads:
            source_id = str(item.get("source_ad_id") or "")
            new_id = str(item.get("id") or "")
            creative = item.get("creative") if isinstance(item.get("creative"), Mapping) else {}
            if not source_id or not new_id or source_id in source_ads:
                raise RuntimeError("copy_mapping_incomplete")
            source_ads.add(source_id)
            normalized = dict(item)
            normalized["source_ad_id"] = source_id
            normalized["ad_id"] = new_id
            normalized["creative_id"] = str(creative.get("id") or "")
            normalized_ads.append(normalized)
        if not normalized_adsets or not normalized_ads:
            raise RuntimeError("copy_mapping_incomplete")
        for adset in normalized_adsets:
            overrides = {"status": "PAUSED"}
            if adjustments.get("budget_level") == "ABO":
                source_adset_id = adset["source_adset_id"]
                adset_budget = (adjustments.get("adset_budgets") or {}).get(source_adset_id)
                if not adset_budget:
                    raise RuntimeError("missing_copied_adset_budget")
                overrides[str(adjustments.get("budget_type"))] = int(adset_budget)
            if adjustments.get("roas_floor") is not None:
                overrides["bid_constraints"] = {"roas_average_floor": str(adjustments["roas_floor"])}
            self._call("POST", "/%s" % adset["adset_id"], overrides)
            verified = self._call("GET", "/%s" % adset["adset_id"], {
                "fields": "id,status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_constraints"
            })
            if str(verified.get("status") or verified.get("effective_status") or "").upper() != "PAUSED":
                raise RuntimeError("copied_adset_not_paused")
            if adjustments.get("budget_level") == "ABO":
                actual_budget = verified.get(str(adjustments.get("budget_type")))
                if int(actual_budget or 0) != int(adset_budget):
                    raise RuntimeError("copied_adset_budget_mismatch")
        return {
            "campaign_id": copied_id,
            "campaign_name": str(campaign.get("name") or ""),
            "status": "PAUSED",
            "mapping_complete": True,
            "adsets": normalized_adsets,
            "ads": normalized_ads,
            "copy_response": {key: response.get(key) for key in ("id", "copied_campaign_id") if response.get(key)},
        }

    def activate_campaign(self, campaign_id: str) -> Mapping[str, Any]:
        self._call("POST", "/%s" % campaign_id, {"status": "ACTIVE"})
        result = self._call("GET", "/%s" % campaign_id, {"fields": "id,status,effective_status"})
        status = str(result.get("status") or result.get("effective_status") or "").upper()
        if status != "ACTIVE":
            raise RuntimeError("copied_campaign_activation_failed")
        return {"id": str(result.get("id") or campaign_id), "status": status}


class CopyEngine:
    """Execute campaign copies with fail-closed ordering.

    ``meta`` must expose ``deep_copy_campaign`` and ``activate_campaign``.
    ``ledger`` must expose ``write_facebook_copy``.  No dependency is touched
    in observe mode or while the kill switch is disabled.
    """

    def __init__(
        self,
        meta: Any,
        ledger: Any,
        intents: Any,
        config: Optional[CopyEngineConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.meta = meta
        self.ledger = ledger
        self.intents = intents
        self.config = config or CopyEngineConfig.from_env()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, group: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        candidates = [dict(candidate) for candidate in candidates]
        run_mode = str(group.get("run_mode") or "observe").lower()
        if run_mode != "live":
            return [
                self._result(
                    candidate,
                    "observed",
                    "would_%s" % candidate.get("target_action")
                    if candidate.get("target_action") in WRITE_ACTIONS else "observe_mode",
                )
                for candidate in candidates
            ]
        object_level = str(group.get("object_level") or "campaign").lower()
        if object_level == "ad":
            return [self._result(candidate, "skipped", "phase_not_enabled") for candidate in candidates]
        if not self.config.copy_enabled:
            return [self._result(candidate, "skipped", "copy_disabled") for candidate in candidates]

        annotated = apply_copy_candidate_selection(group, candidates)
        results = []
        for candidate in annotated:
            if candidate.get("candidate_selection_reason") == "outside_top_n":
                results.append(self._result(candidate, "skipped", "outside_top_n"))
                continue
            if str(candidate.get("target_action") or "").lower() != "copy":
                results.append(self._result(candidate, "skipped", "not_copy_target"))
                continue
            results.append(self._execute_one(
                group,
                candidate,
                copy_config_for_candidate(group, candidate),
            ))
        return results

    def _execute_one(self, group: Mapping[str, Any], candidate: Mapping[str, Any], copy_config: Mapping[str, Any]) -> Dict[str, Any]:
        now_utc = self.clock()
        try:
            local_now = account_now(now_utc, candidate.get("account_time_zone"))
        except ValueError as exc:
            return self._result(candidate, "skipped", str(exc))
        if not inside_execution_window(local_now, copy_config):
            return self._result(candidate, "skipped", "outside_execution_window")
        source_rows = candidate.get("source_created_rows") or []
        if not isinstance(source_rows, list) or not source_rows:
            return self._result(candidate, "skipped", "missing_source_created_data_rows")
        try:
            budget_adjustments = compute_budget_adjustments(candidate, copy_config)
            roas_floor = compute_roas_floor(candidate, copy_config)
        except ValueError as exc:
            return self._result(candidate, "skipped", str(exc))

        rule_limit = _positive_int(
            copy_config.get("daily_rule_limit")
            or copy_config.get("rule_daily_limit")
            or copy_config.get("per_rule_daily"),
            1,
            self.config.daily_hard_limit,
        )
        user_limit = _positive_int(
            copy_config.get("daily_user_limit")
            or copy_config.get("user_daily_limit")
            or copy_config.get("per_user_daily"),
            self.config.user_daily_limit,
            self.config.daily_hard_limit,
        )
        intent = {
            "intent_id": uuid.uuid4().hex,
            "owner_user_id": str(group.get("owner_user_id") or group.get("created_by") or ""),
            "rule_group_id": str(group.get("group_id") or ""),
            "rule_id": str(candidate.get("target_rule_id") or ""),
            "account_id": normalize_account(candidate.get("account_id")),
            "object_level": "campaign",
            "source_object_id": str(candidate.get("campaign_id") or candidate.get("object_id") or ""),
            "source_created_data_id": str(candidate.get("source_created_data_id") or ""),
            "account_date": local_now.strftime("%Y-%m-%d"),
            "now_utc": now_utc,
        }
        intent["idempotency_key"] = idempotency_key(group, candidate, intent["account_date"])
        reservation = self.intents.reserve(
            intent,
            {"rule": rule_limit, "user": user_limit, "hard": self.config.daily_hard_limit},
            int(copy_config.get("source_cooldown_days", self.config.source_cooldown_days)),
        )
        if not reservation.get("ok"):
            existing = reservation.get("existing") or {}
            if reservation.get("reason") == "duplicate_intent" and existing:
                return self._resume_existing(group, candidate, copy_config, existing)
            return self._result(candidate, "skipped", reservation.get("reason") or "intent_rejected", intent_id=reservation.get("intent_id"))

        intent_id = intent["intent_id"]
        adjustments = dict(budget_adjustments)
        adjustments["roas_floor"] = str(roas_floor) if roas_floor is not None else None
        try:
            copied = self.meta.deep_copy_campaign(
                account_id=intent["account_id"],
                campaign_id=intent["source_object_id"],
                deep_copy=True,
                status_option="PAUSED",
                adjustments=adjustments,
            )
            if not copied.get("campaign_id"):
                raise RuntimeError("missing_copied_campaign_id")
            if str(copied.get("status") or "PAUSED").upper() != "PAUSED":
                raise RuntimeError("copied_campaign_not_paused")
            if not copied.get("mapping_complete"):
                raise RuntimeError("copy_mapping_incomplete")
            self.intents.update(intent_id, "meta_created", {"copied": copied, "adjustments": adjustments})
            return self._complete_after_meta(intent_id, intent, candidate, copied, adjustments)
        except Exception as exc:
            # If Meta has already created an object it remains PAUSED.  Never
            # compensate by activation or by a second copy attempt.
            reason = str(exc) or exc.__class__.__name__
            self.intents.update(intent_id, "quarantined", reason=reason)
            return self._result(candidate, "error", reason, intent_id=intent_id)

    def _resume_existing(
        self,
        group: Mapping[str, Any],
        candidate: Mapping[str, Any],
        copy_config: Mapping[str, Any],
        existing: Mapping[str, Any],
    ) -> Dict[str, Any]:
        intent_id = str(existing.get("intent_id") or "")
        status = str(existing.get("status") or "")
        state = existing.get("result") if isinstance(existing.get("result"), Mapping) else {}
        if status in ("completed", "activated"):
            return self._result(candidate, "success", "resumed_completed_intent", intent_id=intent_id, result=state)
        if status in ("failed", "quarantined"):
            return self._result(candidate, "skipped", "intent_quarantined", intent_id=intent_id)
        if status == "reserved":
            # A reserved row may still belong to another worker.  Never issue a
            # second Meta copy without a reconciliation/lease decision.
            return self._result(candidate, "skipped", "intent_in_progress", intent_id=intent_id)
        copied = state.get("copied") if isinstance(state.get("copied"), Mapping) else {}
        adjustments = state.get("adjustments") if isinstance(state.get("adjustments"), Mapping) else {}
        if not copied or not copied.get("campaign_id"):
            self.intents.update(intent_id, "quarantined", reason="resume_state_incomplete")
            return self._result(candidate, "error", "resume_state_incomplete", intent_id=intent_id)
        intent = {
            "intent_id": intent_id,
            "owner_user_id": str(existing.get("owner_user_id") or group.get("owner_user_id") or ""),
            "rule_group_id": str(existing.get("rule_group_id") or group.get("group_id") or ""),
            "rule_id": str(existing.get("rule_id") or candidate.get("target_rule_id") or ""),
            "account_id": str(existing.get("account_id") or normalize_account(candidate.get("account_id"))),
            "source_object_id": str(existing.get("source_object_id") or candidate.get("campaign_id") or ""),
        }
        try:
            if status == "meta_created":
                return self._complete_after_meta(intent_id, intent, candidate, copied, adjustments)
            if status == "ledger_written":
                ledger_result = state.get("ledger") if isinstance(state.get("ledger"), Mapping) else {}
                return self._activate_after_ledger(intent_id, candidate, copied, adjustments, ledger_result)
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            self.intents.update(intent_id, "quarantined", reason=reason)
            return self._result(candidate, "error", reason, intent_id=intent_id)
        return self._result(candidate, "skipped", "intent_in_progress", intent_id=intent_id)

    def _complete_after_meta(
        self,
        intent_id: str,
        intent: Mapping[str, Any],
        candidate: Mapping[str, Any],
        copied: Mapping[str, Any],
        adjustments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        ledger_result = self.ledger.write_facebook_copy(
            intent=dict(intent), source_candidate=dict(candidate), copied=dict(copied), adjustments=dict(adjustments)
        )
        if not ledger_result or ledger_result.get("ok") is not True:
            raise RuntimeError("created_data_write_failed")
        state = {"copied": dict(copied), "ledger": dict(ledger_result), "adjustments": dict(adjustments)}
        self.intents.update(intent_id, "ledger_written", state)
        return self._activate_after_ledger(intent_id, candidate, copied, adjustments, ledger_result)

    def _activate_after_ledger(
        self,
        intent_id: str,
        candidate: Mapping[str, Any],
        copied: Mapping[str, Any],
        adjustments: Mapping[str, Any],
        ledger_result: Mapping[str, Any],
    ) -> Dict[str, Any]:
        activation = self.meta.activate_campaign(copied["campaign_id"])
        if str((activation or {}).get("status") or "ACTIVE").upper() != "ACTIVE":
            raise RuntimeError("copied_campaign_activation_failed")
        result = {"copied": copied, "ledger": ledger_result, "activation": activation, "adjustments": adjustments}
        self.intents.update(intent_id, "completed", result)
        return self._result(candidate, "success", "", intent_id=intent_id, result=result)

    @staticmethod
    def _result(candidate: Mapping[str, Any], status: str, reason: str, **extra: Any) -> Dict[str, Any]:
        result = {
            "object_key": candidate.get("object_key") or "",
            "account_id": normalize_account(candidate.get("account_id")),
            "campaign_id": str(candidate.get("campaign_id") or candidate.get("object_id") or ""),
            "status": status,
            "reason": reason,
        }
        result.update(extra)
        return result

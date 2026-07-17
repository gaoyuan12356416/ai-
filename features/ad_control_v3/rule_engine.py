"""Pure deterministic V3 rule evaluation with pause-over-copy conflict safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import AdControlV3Error


EVIDENCE_MAX_FIELDS = 50
EVIDENCE_MAX_LIST_ITEMS = 20
EVIDENCE_MAX_TEXT_LENGTH = 512
TARGET_METRIC_FIELDS = (
    "spend", "impressions", "clicks", "installs", "purchase", "revenue",
    "day1_retain", "retain_install", "retention_rate", "events", "atc",
    "delivery_cnt", "af_installs", "af_revenue", "af_roas",
    "ad_impression", "ad_impression_revenue", "ad_impression_roas",
    "ctr", "cpm", "cpc", "cpi", "purchase_cpa", "roas",
)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != []


def _positive_finite_number(value: Any) -> bool:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return number.is_finite() and number > 0


def _number(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AdControlV3Error(
            "condition_value_invalid",
            "numeric condition value is invalid",
            details={"field": field},
        )
    if not parsed.is_finite():
        raise AdControlV3Error(
            "condition_value_invalid",
            "numeric condition value must be finite",
            details={"field": field},
        )
    return parsed


def _as_values(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _parse_time(value: Any, field: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise AdControlV3Error(
            "condition_value_invalid",
            "time condition value is invalid",
            details={"field": field},
        )


def _compare_scalar(
    actual: Any,
    operator: str,
    expected: Any,
    value_type: str,
    field: str,
    evaluation_time: Optional[datetime] = None,
) -> bool:
    if value_type == "number":
        left = _number(actual, field)
        if operator == "between":
            if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                raise AdControlV3Error("condition_value_invalid", "between requires two values", details={"field": field})
            return _number(expected[0], field) <= left <= _number(expected[1], field)
        right = _number(expected, field)
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
            "eq": left == right,
            "ne": left != right,
        }.get(operator, False)
    if value_type == "time":
        left_time = _parse_time(actual, field)
        if operator in {"within_last_days", "older_than_days"}:
            if evaluation_time is None:
                raise AdControlV3Error("evaluation_time_required", "relative time rule requires evaluation_time")
            reference = evaluation_time
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            reference = reference.astimezone(timezone.utc)
            days = int(expected)
            boundary = reference - timedelta(days=days)
            return boundary <= left_time <= reference if operator == "within_last_days" else left_time < boundary
        if operator == "between":
            if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                raise AdControlV3Error("condition_value_invalid", "between requires two times", details={"field": field})
            return _parse_time(expected[0], field) <= left_time <= _parse_time(expected[1], field)
        right_time = _parse_time(expected, field)
        return (operator == "before" and left_time < right_time) or (operator == "after" and left_time > right_time)
    left_text = str(actual)
    if operator == "eq":
        return left_text == str(expected)
    if operator == "ne":
        return left_text != str(expected)
    if operator == "contains":
        return str(expected) in left_text
    if operator == "not_contains":
        return str(expected) not in left_text
    if operator == "starts_with":
        return left_text.startswith(str(expected))
    if operator == "in":
        if not isinstance(expected, (list, tuple, set)):
            raise AdControlV3Error("condition_value_invalid", "in requires a list", details={"field": field})
        return left_text in {str(item) for item in expected}
    if operator == "not_in":
        if not isinstance(expected, (list, tuple, set)):
            raise AdControlV3Error("condition_value_invalid", "not_in requires a list", details={"field": field})
        return left_text not in {str(item) for item in expected}
    return False


def condition_matches(
    candidate: Mapping[str, Any],
    condition: Mapping[str, Any],
    capability: Mapping[str, Any],
    evaluation_time: Optional[datetime] = None,
) -> bool:
    field = str(condition.get("field") or "")
    operator = str(condition.get("operator") or "")
    actual = candidate.get(field)
    if operator == "exists":
        return _present(actual)
    if operator == "not_exists":
        return not _present(actual)
    if not _present(actual):
        return False
    expected = condition.get("value")
    value_type = str(capability.get("value_type") or "text")
    values = _as_values(actual)
    # For a deterministically aggregated set, positive operators match any
    # member; negative operators must hold for every member.
    results = [_compare_scalar(item, operator, expected, value_type, field, evaluation_time) for item in values]
    if operator in {"ne", "not_in", "not_contains"}:
        return all(results)
    return any(results)


def rule_matches(
    candidate: Mapping[str, Any],
    rule: Mapping[str, Any],
    capabilities: Mapping[str, Mapping[str, Any]],
    evaluation_time: Optional[datetime] = None,
) -> bool:
    results = [
        condition_matches(candidate, condition, capabilities[str(condition.get("field") or "")], evaluation_time)
        for condition in (rule.get("conditions") or [])
    ]
    return all(results) if str(rule.get("logic") or "") == "and" else any(results)


def _bounded_evidence_value(value: Any) -> Tuple[Any, bool]:
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        bounded = []
        truncated = len(items) > EVIDENCE_MAX_LIST_ITEMS
        for item in items[:EVIDENCE_MAX_LIST_ITEMS]:
            safe_item, item_truncated = _bounded_evidence_value(item)
            bounded.append(safe_item)
            truncated = truncated or item_truncated
        return bounded, truncated
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    text = str(value)
    return text[:EVIDENCE_MAX_TEXT_LENGTH], len(text) > EVIDENCE_MAX_TEXT_LENGTH


def _condition_evidence(
    candidate: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    capability_keys: Sequence[str],
) -> Tuple[Dict[str, Any], bool]:
    allowed = set(capability_keys)
    requested = sorted({
        str(condition.get("field") or "")
        for rule in matches
        for condition in (rule.get("conditions") or [])
        if str(condition.get("field") or "") in allowed
    })
    truncated = len(requested) > EVIDENCE_MAX_FIELDS
    evidence: Dict[str, Any] = {}
    for field in requested[:EVIDENCE_MAX_FIELDS]:
        evidence[field], value_truncated = _bounded_evidence_value(candidate.get(field))
        truncated = truncated or value_truncated
    return evidence, truncated


def _chosen_target(
    candidate: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    capability_keys: Sequence[str],
) -> Dict[str, Any]:
    ordered = sorted(matches, key=lambda item: (int(item.get("priority") or 0), str(item.get("rule_id") or "")))
    pause_rules = [item for item in ordered if item.get("action") == "pause"]
    chosen = pause_rules[0] if pause_rules else ordered[0]
    shadowed = [str(item.get("rule_id") or "") for item in ordered if item is not chosen]
    evidence, evidence_truncated = _condition_evidence(candidate, ordered, capability_keys)
    target = {
        "channel": candidate.get("channel"),
        "ad_account_id": candidate.get("ad_account_id"),
        "object_level": candidate.get("object_level"),
        "object_id": candidate.get("object_id"),
        "campaign_id": candidate.get("campaign_id") or "",
        "adset_id": candidate.get("adset_id") or "",
        "ad_id": candidate.get("ad_id") or "",
        "product": candidate.get("product"),
        "optimizer_id": candidate.get("optimizer_id"),
        "action": chosen.get("action"),
        "control_rule_id": chosen.get("rule_id"),
        "matched_rule_ids": [str(item.get("rule_id") or "") for item in ordered],
        "shadowed_by_rule": shadowed,
        "status": "would_%s" % chosen.get("action"),
        "reason": "matched",
        "copy_parameters": dict(chosen.get("copy_parameters") or {}) if chosen.get("action") == "copy" else {},
        "condition_evidence": evidence,
        "condition_evidence_truncated": evidence_truncated,
        "metrics": {
            key: candidate.get(key)
            for key in TARGET_METRIC_FIELDS
        },
    }
    if chosen.get("action") == "copy":
        parameters = dict(chosen.get("copy_parameters") or {})
        readiness_reasons = []
        budget_mode = str(parameters.get("budget_mode") or "")
        if budget_mode == "actual_cpi_multiplier" and not _positive_finite_number(candidate.get("cpi")):
            readiness_reasons.append("actual_cpi_unavailable")
        # Source budgets, CBO/ABO structure and MIN_ROAS compatibility are
        # intentionally checked through current Graph readback immediately
        # before the first copy write; the insight snapshot does not carry a
        # reliable structural budget contract.
        target["copy_live_ready"] = not readiness_reasons
        target["copy_readiness_reasons"] = readiness_reasons
    return target


def evaluate_candidates(
    candidates: Iterable[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    capabilities: Sequence[Mapping[str, Any]],
    selection: Optional[Mapping[str, Any]] = None,
    evaluation_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    capability_map = {str(item.get("key") or ""): item for item in capabilities}
    targets: List[Dict[str, Any]] = []
    scanned = 0
    blocked = 0
    unmatched = 0
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        scanned += 1
        block_reason = str(candidate.get("blocked_reason") or "")
        if block_reason:
            blocked += 1
            targets.append(
                {
                    "channel": candidate.get("channel"),
                    "ad_account_id": candidate.get("ad_account_id"),
                    "object_level": candidate.get("object_level"),
                    "object_id": candidate.get("object_id"),
                    "campaign_id": candidate.get("campaign_id") or "",
                    "adset_id": candidate.get("adset_id") or "",
                    "ad_id": candidate.get("ad_id") or "",
                    "product": candidate.get("product"),
                    "optimizer_id": candidate.get("optimizer_id"),
                    "action": "",
                    "control_rule_id": "",
                    "matched_rule_ids": [],
                    "shadowed_by_rule": [],
                    "status": "blocked",
                    "reason": block_reason,
                }
            )
            continue
        matches = [rule for rule in rules if rule_matches(candidate, rule, capability_map, evaluation_time)]
        if not matches:
            unmatched += 1
            continue
        targets.append(_chosen_target(candidate, matches, tuple(capability_map)))
    actionable = [target for target in targets if target["status"].startswith("would_")]
    matched_before_selection = len(actionable)
    selection = dict(selection or {})
    selection_mode = str(selection.get("mode") or "")
    if selection_mode not in {"all", "account_top_n", "product_top_n", "global_top_n"}:
        raise AdControlV3Error("validation_error", "unsupported candidate selection mode")
    deferred_count = 0
    if selection_mode != "all":
        try:
            top_n = int(selection.get("top_n"))
        except (TypeError, ValueError):
            raise AdControlV3Error("validation_error", "Top N selection requires top_n")
        if top_n < 1 or top_n > 10000:
            raise AdControlV3Error("validation_error", "top_n is out of range")
        sort_field = str(selection.get("sort_field") or "")
        sort_direction = str(selection.get("sort_direction") or "").lower()
        capability = capability_map.get(sort_field)
        if not sort_field or not capability or capability.get("value_type") != "number":
            raise AdControlV3Error("validation_error", "Top N selection requires a numeric sort_field")
        if sort_direction not in {"asc", "desc"}:
            raise AdControlV3Error("validation_error", "Top N selection requires asc/desc sort_direction")

        def group_key(target: Mapping[str, Any]) -> str:
            if selection_mode == "account_top_n":
                return str(target.get("ad_account_id") or "")
            if selection_mode == "product_top_n":
                return str(target.get("product") or "")
            return "__global__"

        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for target in actionable:
            buckets.setdefault(group_key(target), []).append(target)
        selected_ids = set()
        for bucket in buckets.values():
            def sort_key(target: Mapping[str, Any]) -> Tuple[Any, ...]:
                raw = (target.get("metrics") or {}).get(sort_field)
                if not _present(raw):
                    return 1, Decimal("0"), str(target.get("object_id") or "")
                numeric = _number(raw, sort_field)
                rank = numeric if sort_direction == "asc" else -numeric
                return 0, rank, str(target.get("object_id") or "")
            for chosen in sorted(bucket, key=sort_key)[:top_n]:
                selected_ids.add(id(chosen))
        for target in actionable:
            if id(target) not in selected_ids:
                target["status"] = "deferred_by_selection"
                target["reason"] = "top_n_not_selected"
                deferred_count += 1
        actionable = [target for target in actionable if target["status"].startswith("would_")]
    return {
        "summary": {
            "scanned_count": scanned,
            "matched_count": len(actionable),
            "matched_before_selection": matched_before_selection,
            "planned_count": len(actionable),
            "deferred_count": deferred_count,
            "pause_count": len([target for target in actionable if target["action"] == "pause"]),
            "copy_count": len([target for target in actionable if target["action"] == "copy"]),
            "blocked_count": blocked,
            "unmatched_count": unmatched,
        },
        "targets": targets,
    }

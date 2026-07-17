"""Input contracts shared by the V3 service, rule engine and HTTP adapter.

This module deliberately has no dependency on the V2 application or SQLite.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .errors import AdControlV3Error


CHANNEL_FACEBOOK = "facebook"
CHANNEL_TIKTOK = "tiktok"
SUPPORTED_CHANNELS = (CHANNEL_FACEBOOK, CHANNEL_TIKTOK)
OBJECT_LEVELS = ("campaign", "adset", "ad")
RUN_MODES = ("observe", "live")
ACTIONS = ("pause", "copy")
ACCOUNT_SCOPE_KEYS = frozenset(
    {
        "account_id",
        "account_ids",
        "accounts",
        "account_group_id",
        "account_group_ids",
        "account_pool_id",
        "account_pool_ids",
        "ad_account_id",
        "ad_account_ids",
    }
)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TIME_OF_DAY_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class Actor:
    user_id: str
    is_admin: bool = False
    email: str = ""
    name: str = ""
    optimizer_id: Optional[int] = None

    @classmethod
    def from_value(cls, value: Any) -> "Actor":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise AdControlV3Error("authentication_required", "login is required", 401)
        user_id = clean_text(
            value.get("user_id")
            or value.get("id")
            or value.get("sub_user_id"),
            "actor.user_id",
            required=True,
            max_length=128,
        )
        raw_admin = value.get("is_admin", value.get("admin", False))
        role = str(value.get("role") or value.get("user_role") or "").strip().lower()
        is_admin = raw_admin is True or str(raw_admin).strip().lower() in {
            "1",
            "true",
            "yes",
            "admin",
        } or role in {"admin", "administrator", "superadmin", "super_admin"}
        optimizer_id = value.get("optimizer_id")
        if optimizer_id in (None, ""):
            optimizer_id = None
        else:
            optimizer_id = positive_int(optimizer_id, "actor.optimizer_id")
        return cls(
            user_id=user_id,
            is_admin=is_admin,
            email=clean_text(value.get("email"), "actor.email", max_length=254),
            name=clean_text(
                value.get("name") or value.get("username") or value.get("display_name"),
                "actor.name",
                max_length=128,
            ),
            optimizer_id=optimizer_id,
        )


def clean_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    max_length: int = 255,
) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise AdControlV3Error("validation_error", "%s is required" % field, details={"field": field})
    if len(text) > max_length:
        raise AdControlV3Error(
            "validation_error",
            "%s is too long" % field,
            details={"field": field, "max_length": max_length},
        )
    return text


def positive_int(
    value: Any,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 2_147_483_647,
) -> int:
    if isinstance(value, bool):
        raise AdControlV3Error("validation_error", "%s must be an integer" % field, details={"field": field})
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AdControlV3Error("validation_error", "%s must be an integer" % field, details={"field": field})
    if parsed < minimum or parsed > maximum:
        raise AdControlV3Error(
            "validation_error",
            "%s is out of range" % field,
            details={"field": field, "minimum": minimum, "maximum": maximum},
        )
    return parsed


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise AdControlV3Error("validation_error", "%s must be boolean" % field, details={"field": field})


def positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise AdControlV3Error("validation_error", "%s must be numeric" % field, details={"field": field})
    if not math.isfinite(parsed) or parsed <= 0:
        raise AdControlV3Error("validation_error", "%s must be greater than zero" % field, details={"field": field})
    return parsed


def parse_iso_date(value: Any, field: str) -> date:
    text = clean_text(value, field, required=True, max_length=10)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise AdControlV3Error("validation_error", "%s must use YYYY-MM-DD" % field, details={"field": field})


def ensure_no_account_scope(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in ACCOUNT_SCOPE_KEYS:
                raise AdControlV3Error(
                    "account_scope_forbidden",
                    "V3 scope is product plus optimizer; account fields are forbidden",
                    details={"field": "%s.%s" % (path, key)},
                )
            ensure_no_account_scope(child, "%s.%s" % (path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            ensure_no_account_scope(child, "%s[%d]" % (path, index))


def normalize_string_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    max_items: int = 100,
    max_length: int = 128,
) -> List[str]:
    if value in (None, ""):
        values: Sequence[Any] = []
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise AdControlV3Error("validation_error", "%s must be a list" % field, details={"field": field})
    if len(values) > max_items:
        raise AdControlV3Error(
            "validation_error",
            "%s has too many values" % field,
            details={"field": field, "max_items": max_items},
        )
    result: List[str] = []
    for index, raw in enumerate(values):
        item = clean_text(raw, "%s[%d]" % (field, index), required=True, max_length=max_length)
        if item not in result:
            result.append(item)
    if required and not result:
        raise AdControlV3Error("validation_error", "%s requires at least one value" % field, details={"field": field})
    return result


def normalize_condition(condition: Any, index: int) -> Dict[str, Any]:
    if not isinstance(condition, Mapping):
        raise AdControlV3Error(
            "validation_error",
            "condition must be an object",
            details={"field": "conditions[%d]" % index},
        )
    field = clean_text(condition.get("field"), "condition.field", required=True, max_length=64)
    operator = clean_text(condition.get("operator"), "condition.operator", required=True, max_length=32).lower()
    normalized: Dict[str, Any] = {"field": field, "operator": operator}
    if operator not in {"exists", "not_exists"}:
        if "value" not in condition:
            raise AdControlV3Error(
                "validation_error",
                "condition.value is required",
                details={"field": "condition.value"},
            )
        normalized["value"] = condition.get("value")
    return normalized


def normalize_rules(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AdControlV3Error("validation_error", "rules requires at least one rule", details={"field": "rules"})
    if len(value) > 50:
        raise AdControlV3Error("validation_error", "too many rules", details={"field": "rules", "max_items": 50})
    result: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise AdControlV3Error("validation_error", "rule must be an object", details={"field": "rules[%d]" % index})
        rule_id = clean_text(raw.get("rule_id") or raw.get("id") or "rule-%d" % (index + 1), "rule.id", required=True, max_length=64)
        if not ID_RE.match(rule_id) or rule_id in seen_ids:
            raise AdControlV3Error("validation_error", "rule id is invalid or duplicated", details={"field": "rules[%d].id" % index})
        seen_ids.add(rule_id)
        logic = clean_text(raw.get("logic"), "rule.logic", required=True, max_length=8).lower()
        if logic not in {"and", "or"}:
            raise AdControlV3Error("validation_error", "rule.logic must be and/or", details={"field": "rules[%d].logic" % index})
        action = clean_text(raw.get("action"), "rule.action", required=True, max_length=16).lower()
        if action not in ACTIONS:
            raise AdControlV3Error("validation_error", "unsupported rule action", details={"field": "rules[%d].action" % index})
        conditions = raw.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise AdControlV3Error("validation_error", "rule requires conditions", details={"field": "rules[%d].conditions" % index})
        if len(conditions) > 30:
            raise AdControlV3Error("validation_error", "too many rule conditions", details={"field": "rules[%d].conditions" % index})
        # When priority is omitted, the visible array order is the contract.
        # This does not invent a business default: the UI-authored order is
        # preserved as a fixed, explicit zero-based priority.
        priority = positive_int(raw.get("priority", index), "rule.priority", minimum=0, maximum=100000)
        normalized = {
            "rule_id": rule_id,
            "name": clean_text(raw.get("name"), "rule.name", max_length=128),
            "priority": priority,
            "logic": logic,
            "action": action,
            "conditions": [normalize_condition(item, item_index) for item_index, item in enumerate(conditions)],
        }
        if action == "copy":
            copy_parameters = raw.get("copy_parameters") or {}
            if not isinstance(copy_parameters, Mapping):
                raise AdControlV3Error("validation_error", "copy_parameters must be an object")
            allowed_copy_fields = {
                "budget_mode",
                "budget_multiplier",
                "target_cpi",
                "source_budget_ratio",
                "roas_adjustment_direction",
                "roas_adjustment_percent",
                "carrier_strategy",
                "cooldown_days",
                "daily_copy_limit",
            }
            unknown = sorted(set(copy_parameters) - allowed_copy_fields)
            if unknown:
                raise AdControlV3Error(
                    "validation_error",
                    "copy_parameters contains unsupported fields",
                    details={"fields": unknown},
                )
            normalized["copy_parameters"] = dict(copy_parameters)
        result.append(normalized)
    return sorted(result, key=lambda item: (int(item["priority"]), item["rule_id"]))


COPY_CARRIER_STRATEGIES = {
    "campaign": {"deep_copy_campaign"},
    "adset": {"same_campaign", "new_campaign"},
    "ad": {"same_adset", "isolated_adset", "isolated_campaign"},
}


def validate_copy_parameters_for_level(rules: Sequence[Mapping[str, Any]], object_level: str) -> None:
    for rule in rules:
        if rule.get("action") != "copy":
            continue
        params = dict(rule.get("copy_parameters") or {})
        carrier = clean_text(params.get("carrier_strategy"), "copy_parameters.carrier_strategy", required=True, max_length=32)
        if carrier not in COPY_CARRIER_STRATEGIES[object_level]:
            raise AdControlV3Error(
                "validation_error",
                "carrier_strategy does not match object_level",
                details={"object_level": object_level, "carrier_strategy": carrier},
            )
        budget_mode = clean_text(params.get("budget_mode"), "copy_parameters.budget_mode", required=True, max_length=48)
        allowed_budget_modes = {
            "actual_cpi_multiplier",
            "fixed_target_cpi_multiplier",
            "source_budget_ratio",
        }
        if budget_mode not in allowed_budget_modes:
            raise AdControlV3Error("validation_error", "unsupported copy budget_mode")
        if budget_mode in {"actual_cpi_multiplier", "fixed_target_cpi_multiplier"}:
            multiplier = params.get("budget_multiplier")
            if multiplier in (None, ""):
                raise AdControlV3Error("validation_error", "budget_multiplier must be greater than zero")
            positive_float(multiplier, "copy_parameters.budget_multiplier")
        if budget_mode == "fixed_target_cpi_multiplier":
            target_cpi = params.get("target_cpi")
            if target_cpi in (None, ""):
                raise AdControlV3Error("validation_error", "target_cpi must be greater than zero")
            positive_float(target_cpi, "copy_parameters.target_cpi")
        if budget_mode == "source_budget_ratio":
            ratio = params.get("source_budget_ratio")
            if ratio in (None, ""):
                raise AdControlV3Error("validation_error", "source_budget_ratio must be greater than zero")
            positive_float(ratio, "copy_parameters.source_budget_ratio")
        direction = params.get("roas_adjustment_direction")
        percent = params.get("roas_adjustment_percent")
        if (direction in (None, "")) != (percent in (None, "")):
            raise AdControlV3Error("validation_error", "ROAS direction and percent must be configured together")
        if direction not in (None, ""):
            if direction not in {"increase", "decrease"}:
                raise AdControlV3Error("validation_error", "unsupported ROAS adjustment direction")
            try:
                numeric_percent = float(percent)
            except (TypeError, ValueError):
                raise AdControlV3Error("validation_error", "ROAS adjustment percent must be numeric")
            if numeric_percent <= 0 or numeric_percent > 100:
                raise AdControlV3Error("validation_error", "ROAS adjustment percent must be within (0, 100]")
        for key in ("cooldown_days", "daily_copy_limit"):
            if key in params and params[key] not in (None, ""):
                positive_int(params[key], "copy_parameters.%s" % key, maximum=3650)


def normalize_selection(value: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {"mode", "top_n", "sort_field", "sort_direction", "metric_window_days"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AdControlV3Error("validation_error", "selection contains unsupported fields", details={"fields": unknown})
    mode = clean_text(value.get("mode"), "selection.mode", required=True, max_length=32)
    if mode not in {"all", "account_top_n", "product_top_n", "global_top_n"}:
        raise AdControlV3Error("validation_error", "unsupported selection.mode")
    normalized: Dict[str, Any] = {
        "mode": mode,
        "metric_window_days": positive_int(
            value.get("metric_window_days"),
            "selection.metric_window_days",
            maximum=31,
        ),
    }
    if mode != "all":
        normalized["top_n"] = positive_int(value.get("top_n"), "selection.top_n", maximum=10000)
        normalized["sort_field"] = clean_text(value.get("sort_field"), "selection.sort_field", required=True, max_length=64)
        direction = clean_text(value.get("sort_direction"), "selection.sort_direction", required=True, max_length=8).lower()
        if direction not in {"asc", "desc"}:
            raise AdControlV3Error("validation_error", "selection.sort_direction must be asc/desc")
        normalized["sort_direction"] = direction
    else:
        incompatible = [key for key in ("top_n", "sort_field", "sort_direction") if value.get(key) not in (None, "")]
        if incompatible:
            raise AdControlV3Error(
                "validation_error",
                "all selection cannot include Top N fields",
                details={"fields": incompatible},
            )
    return normalized


def normalize_schedule(value: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {"type", "fixed_time", "interval_minutes", "allowed_start_time", "allowed_end_time"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AdControlV3Error("validation_error", "schedule contains unsupported fields", details={"fields": unknown})
    if not value:
        return {}
    schedule_type = clean_text(value.get("type"), "schedule.type", required=True, max_length=32)
    if schedule_type not in {"fixed_time", "interval"}:
        raise AdControlV3Error("validation_error", "unsupported schedule.type")
    result: Dict[str, Any] = {"type": schedule_type}
    if schedule_type == "fixed_time":
        fixed_time = clean_text(value.get("fixed_time"), "schedule.fixed_time", required=True, max_length=5)
        if not TIME_OF_DAY_RE.match(fixed_time):
            raise AdControlV3Error("validation_error", "schedule.fixed_time must use HH:MM")
        result["fixed_time"] = fixed_time
    else:
        result["interval_minutes"] = positive_int(
            value.get("interval_minutes"), "schedule.interval_minutes", maximum=1440
        )
    for key in ("allowed_start_time", "allowed_end_time"):
        if value.get(key) not in (None, ""):
            time_value = clean_text(value.get(key), "schedule.%s" % key, required=True, max_length=5)
            if not TIME_OF_DAY_RE.match(time_value):
                raise AdControlV3Error("validation_error", "schedule.%s must use HH:MM" % key)
            result[key] = time_value
    return result


def normalize_quotas(value: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {"group_daily_limit", "user_daily_limit", "object_cooldown_days"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AdControlV3Error("validation_error", "quotas contains unsupported fields", details={"fields": unknown})
    result: Dict[str, Any] = {}
    for key in allowed:
        if value.get(key) not in (None, ""):
            maximum = 3650 if key == "object_cooldown_days" else 10000
            result[key] = positive_int(value[key], "quotas.%s" % key, maximum=maximum)
    return result


def normalize_rule_group_payload(
    payload: Any,
    *,
    creating: bool,
    current: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AdControlV3Error("validation_error", "payload must be an object")
    ensure_no_account_scope(payload)
    source: Dict[str, Any] = dict(current or {})
    source.update(dict(payload))
    channel = clean_text(source.get("channel"), "channel", required=True, max_length=32).lower()
    if channel not in SUPPORTED_CHANNELS:
        raise AdControlV3Error("channel_not_enabled", "unsupported channel", details={"channel": channel})
    object_level = clean_text(source.get("object_level"), "object_level", required=True, max_length=16).lower()
    if object_level not in OBJECT_LEVELS:
        raise AdControlV3Error("validation_error", "unsupported object level", details={"field": "object_level"})
    products = normalize_string_list(source.get("products"), "products", required=True, max_items=20, max_length=128)
    account_timezones = normalize_string_list(source.get("account_timezones") or [], "account_timezones", max_items=100, max_length=64)
    rules = normalize_rules(source.get("rules"))
    validate_copy_parameters_for_level(rules, object_level)
    run_mode = "observe" if creating else clean_text(source.get("run_mode") or "observe", "run_mode", required=True).lower()
    if run_mode not in RUN_MODES:
        raise AdControlV3Error("validation_error", "unsupported run mode", details={"field": "run_mode"})
    optimizer_id = source.get("optimizer_id")
    if optimizer_id in (None, ""):
        optimizer_id = None
    else:
        optimizer_id = positive_int(optimizer_id, "optimizer_id")
    schedule = source.get("schedule") or {}
    quotas = source.get("quotas") or {}
    selection = source.get("selection") or {}
    for field_name, raw_value in (("schedule", schedule), ("quotas", quotas), ("selection", selection)):
        if not isinstance(raw_value, Mapping):
            raise AdControlV3Error("validation_error", "%s must be an object" % field_name, details={"field": field_name})
    return {
        "name": clean_text(source.get("name"), "name", required=True, max_length=128),
        "description": clean_text(source.get("description"), "description", max_length=1000),
        "channel": channel,
        "object_level": object_level,
        "run_mode": run_mode,
        "optimizer_id": optimizer_id,
        "products": products,
        "account_timezones": account_timezones,
        "rules": rules,
        "schedule": normalize_schedule(schedule),
        "quotas": normalize_quotas(quotas),
        "selection": normalize_selection(selection),
        "enabled": False if creating else bool(source.get("enabled", False)),
        "emergency_stopped": bool(source.get("emergency_stopped", False)),
    }


BEHAVIOR_FIELDS = (
    "channel",
    "object_level",
    "run_mode",
    "optimizer_id",
    "optimizer_ids",
    "products",
    "account_timezones",
    "rules",
    "schedule",
    "quotas",
    "selection",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def behavior_hash(group: Mapping[str, Any]) -> str:
    behavior = {key: group.get(key) for key in BEHAVIOR_FIELDS}
    return hashlib.sha256(canonical_json(behavior).encode("utf-8")).hexdigest()


def serialize_for_store(value: Any) -> str:
    return canonical_json(value)


def deserialize_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback

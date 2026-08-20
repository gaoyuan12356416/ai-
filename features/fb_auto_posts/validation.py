"""Strict, public-input validation for FB Page automatic templates."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping


RESOURCE_TYPES = {str(i) for i in range(23)} | {"100"}
TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
LANGUAGE_RE = re.compile(r"[a-z0-9]{2,8}(?:-[a-z0-9]{1,8}){0,3}")
LANGUAGE_ALIASES = {"english":"en","spanish":"es","portuguese":"pt","indonesian":"id","french":"fr","german":"de","japanese":"ja","korean":"ko","thai":"th","vietnamese":"vi","arabic":"ar","russian":"ru","filipino":"tl"}
MACRO_RE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
ALLOWED_MACROS = {"drama_name", "material_name", "content_id", "desc", "url"}


class ValidationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


def valid_internal_bearer(value: Any) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", str(value or "")))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("invalid_request", f"{label}必须是对象")
    return value


def _keys(value: Mapping[str, Any], required: set[str], optional: set[str], label: str) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise ValidationError("invalid_request", f"{label}字段不完整或包含未知字段")


def _int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError("invalid_request", f"{label}无效")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError("invalid_request", f"{label}无效") from None
    if not minimum <= result <= maximum:
        raise ValidationError("invalid_request", f"{label}超出范围")
    return result


def _decimal(value: Any, label: str, *, optional: bool = True) -> str | None:
    if value in (None, "") and optional:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("invalid_request", f"{label}无效") from None
    if not result.is_finite() or result < 0 or result > Decimal("1000000000"):
        raise ValidationError("invalid_request", f"{label}超出范围")
    return format(result, "f")


def _rule(raw: Any, *, material: bool) -> Dict[str, Any]:
    value = _mapping(raw, "素材筛选" if material else "短剧筛选")
    required = {"spend_min", "spend_max", "roas_min", "roas_max", "sort_by", "sort_direction"}
    optional = {"resource_type_v2"} if not material else {"duration_min_seconds", "duration_max_seconds"}
    _keys(value, required, optional, "筛选规则")
    result: Dict[str, Any] = {}
    for key, label in (("spend_min", "消耗下限"), ("spend_max", "消耗上限"), ("roas_min", "ROAS下限"), ("roas_max", "ROAS上限")):
        result[key] = _decimal(value.get(key), label)
    for prefix in ("spend", "roas"):
        low, high = result[f"{prefix}_min"], result[f"{prefix}_max"]
        if low is not None and high is not None and Decimal(low) > Decimal(high):
            raise ValidationError("invalid_request", "筛选下限不能大于上限")
    result["sort_by"] = str(value.get("sort_by") or "").lower()
    result["sort_direction"] = str(value.get("sort_direction") or "").lower()
    if result["sort_by"] not in {"spend", "roas"} or result["sort_direction"] not in {"asc", "desc"}:
        raise ValidationError("invalid_request", "排序规则无效")
    if material:
        result["duration_min_seconds"] = _int(value.get("duration_min_seconds", 1), "素材最小时长", 1, 14400)
        result["duration_max_seconds"] = _int(value.get("duration_max_seconds", 600), "素材最大时长", 1, 14400)
        if result["duration_min_seconds"] > result["duration_max_seconds"]:
            raise ValidationError("invalid_request", "素材时长下限不能大于上限")
    else:
        types = value.get("resource_type_v2", [])
        if not isinstance(types, list) or any(str(item) not in RESOURCE_TYPES for item in types):
            raise ValidationError("invalid_request", "短剧类型无效")
        result["resource_type_v2"] = list(dict.fromkeys(str(item) for item in types))
    return result


def _schedule(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "发布时间")
    mode = str(value.get("mode") or "").lower()
    if mode == "fixed":
        _keys(value, {"mode", "times"}, set(), "发布时间")
        times = value.get("times")
        if not isinstance(times, list) or not 1 <= len(times) <= 24:
            raise ValidationError("invalid_request", "固定发布时间必须为1到24个")
        normalized = [str(item or "").strip() for item in times]
        if any(not TIME_RE.fullmatch(item) for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValidationError("invalid_request", "固定发布时间格式无效或重复")
        return {"mode": "fixed", "times": sorted(normalized)}
    if mode == "random":
        _keys(value, {"mode", "daily_count"}, {"start", "end"}, "发布时间")
        start, end = str(value.get("start") or "08:00"), str(value.get("end") or "23:00")
        if not TIME_RE.fullmatch(start) or not TIME_RE.fullmatch(end) or start >= end:
            raise ValidationError("invalid_request", "随机发布时间窗口无效")
        count = _int(value.get("daily_count"), "每日随机次数", 1, 24)
        start_minutes = int(start[:2]) * 60 + int(start[3:])
        end_minutes = int(end[:2]) * 60 + int(end[3:])
        allowed = [minute for minute in range(start_minutes, end_minutes + 1) if minute % 60 != 0]
        maximum, next_allowed = 0, -1
        for minute in allowed:
            if minute >= next_allowed:
                maximum += 1
                next_allowed = minute + 60
        if count > maximum:
            raise ValidationError("invalid_request", "随机发布时间窗口不足以保持60分钟间隔")
        return {"mode": "random", "daily_count": count, "start": start, "end": end}
    raise ValidationError("invalid_request", "发布时间模式无效")


def normalize_template_payload(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "模板")
    required = {"name", "group_ids", "language", "message_template", "drama_rule", "material_rule", "schedule", "video_template"}
    optional = {"metric_window_days", "drama_launch_window_days", "cooldown_days", "material_data_source", "app_id", "product", "metric_product", "metric_platform"}
    if "video_template" not in value or value.get("video_template") != "random_overlay":
        raise ValidationError(
            "fb_auto_video_template_required",
            "视频制作模板必填，当前仅支持随机排重模板",
            409,
        )
    _keys(value, required, optional, "模板")
    name = str(value.get("name") or "").strip()
    language = str(value.get("language") or "").strip().lower()
    language = LANGUAGE_ALIASES.get(language, language)
    message = str(value.get("message_template") or "").replace("\r\n", "\n").strip()
    groups = value.get("group_ids")
    if not 1 <= len(name) <= 120 or not LANGUAGE_RE.fullmatch(language) or not 1 <= len(message) <= 5000:
        raise ValidationError("invalid_request", "模板名称、语言或发布文案无效")
    if re.search(r"(?i)(access[_ -]?token|refresh[_ -]?token|authorization\s*:|bearer\s+[A-Za-z0-9])", message):
        raise ValidationError("fb_auto_message_template_invalid", "发布文案疑似包含凭证信息")
    macros = MACRO_RE.findall(message)
    remainder = MACRO_RE.sub("", message)
    if "{{" in remainder or "}}" in remainder or set(macros) - ALLOWED_MACROS:
        raise ValidationError("fb_auto_message_template_invalid", "发布文案包含未知或不完整宏")
    if macros.count("url") > 1:
        raise ValidationError("fb_auto_message_template_invalid", "发布文案中的短链宏最多只能使用一次")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
        raise ValidationError("invalid_request", "至少选择一个且最多100个Page池")
    group_ids = []
    for item in groups:
        text = str(item or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,30}", text) or text in group_ids:
            raise ValidationError("invalid_request", "Page池ID无效或重复")
        group_ids.append(text)
    return {
        "name": name,
        "group_ids": group_ids,
        "language": language,
        "message_template": message,
        "metric_window_days": _int(value.get("metric_window_days", 7), "指标窗口", 1, 30),
        "drama_launch_window_days": _int(value.get("drama_launch_window_days", 0), "上线窗口", 0, 3650),
        "cooldown_days": _int(value.get("cooldown_days", 30), "Page素材冷却窗口", 0, 3650),
        "material_data_source": _int(value.get("material_data_source", 6), "素材来源", 1, 1000),
        "app_id": str(value.get("app_id") or ""),
        "product": str(value.get("product") or "")[:128],
        "metric_product": str(value.get("metric_product") or "")[:128],
        "metric_platform": _int(value.get("metric_platform", 0), "指标平台", 0, 255),
        "video_template": "random_overlay",
        "drama_rule": _rule(value.get("drama_rule"), material=False),
        "material_rule": _rule(value.get("material_rule"), material=True),
        "schedule": _schedule(value.get("schedule")),
        "material_type": "video",
    }


def config_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expected_version(value: Any) -> int:
    return _int(value, "预期版本", 1, 1_000_000_000)


__all__ = ["ValidationError", "config_hash", "expected_version", "normalize_template_payload", "valid_internal_bearer"]

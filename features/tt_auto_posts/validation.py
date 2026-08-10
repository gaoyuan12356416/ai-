"""Strict public-input validation for TT auto publishing templates."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional

from features.tt_posts.core import TTPostError, render_caption_template


RESOURCE_TYPE_V2_LABELS = {
    "0": "其他",
    "1": "翻译剧非首发",
    "2": "本土首发",
    "3": "本土对投",
    "4": "本土二轮采买",
    "5": "本土自制",
    "6": "翻译剧首发",
    "7": "首发本土动态漫",
    "8": "二轮本土动态漫",
    "9": "首发翻译动态漫",
    "10": "二轮翻译动态漫",
    "11": "翻译剧自制",
    "12": "漫剧自制",
    "13": "AI本土真人剧自制",
    "14": "AI本土真人剧首发",
    "15": "二轮本土AI真人剧",
    "16": "翻译AI真人剧首发",
    "17": "二轮翻译AI真人剧",
    "18": "AI本土解说剧自制",
    "19": "AI本土解说剧首发",
    "20": "AI本土解说剧二轮",
    "21": "AI翻译解说剧首发",
    "22": "AI翻译解说剧首发",
    "100": "小说",
}


class ValidationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "invalid_request")
        self.status = int(status)
        super().__init__(str(message or "请求参数无效"))


def is_placeholder_secret(value: Any) -> bool:
    """Reject documented/example credentials before they can authenticate."""

    canonical = re.sub(
        r"[^a-z0-9]+", "-", str(value or "").strip().lower()
    ).strip("-")
    return (
        canonical in {"change-me", "changeme", "example", "example-token"}
        or canonical.startswith("replace-with-")
        or canonical.startswith("must-match-")
        or "unique-random-token" in canonical
    )


def valid_internal_bearer(value: Any) -> bool:
    token = str(value or "")
    return (
        32 <= len(token) <= 512
        and not any(ord(char) < 33 for char in token)
        and not is_placeholder_secret(token)
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("invalid_request", f"{label}必须是对象")
    return value


def _keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise ValidationError(
            "invalid_request",
            f"{label}字段不完整或包含未知字段",
        )


def _text(value: Any, label: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError("invalid_request", f"{label}必须是文本")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise ValidationError("invalid_request", f"{label}长度无效")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in normalized):
        raise ValidationError("invalid_request", f"{label}包含无效字符")
    return normalized


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError("invalid_request", f"{label}必须是整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError("invalid_request", f"{label}必须是整数") from None
    if str(value).strip() not in {str(normalized), f"+{normalized}"}:
        raise ValidationError("invalid_request", f"{label}必须是整数")
    if normalized < minimum or normalized > maximum:
        raise ValidationError("invalid_request", f"{label}超出允许范围")
    return normalized


def expected_version(value: Any) -> int:
    return _integer(value, "模板版本", 1, 2**31 - 1)


def _decimal_text(
    value: Any,
    label: str,
    *,
    allow_none: bool = True,
) -> Optional[str]:
    if value in (None, "") and allow_none:
        return None
    if isinstance(value, bool):
        raise ValidationError("invalid_request", f"{label}必须是非负数")
    raw = str(value).strip()
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,8})?", raw):
        raise ValidationError("invalid_request", f"{label}必须是非负数")
    try:
        normalized = Decimal(raw)
    except InvalidOperation:
        raise ValidationError("invalid_request", f"{label}必须是非负数") from None
    if not normalized.is_finite() or normalized < 0:
        raise ValidationError("invalid_request", f"{label}必须是非负数")
    return format(normalized, "f")


def _range_rule(
    raw: Any,
    *,
    label: str,
    extra_required: Iterable[str] = (),
    extra_optional: Iterable[str] = (),
) -> Dict[str, Any]:
    value = _mapping(raw, label)
    base = {
        "spend_min",
        "spend_max",
        "roas_min",
        "roas_max",
        "sort_by",
        "sort_direction",
    }
    _keys(
        value,
        required=base | set(extra_required),
        optional=extra_optional,
        label=label,
    )
    result: Dict[str, Any] = {
        key: _decimal_text(value.get(key), f"{label}{key}")
        for key in ("spend_min", "spend_max", "roas_min", "roas_max")
    }
    for prefix in ("spend", "roas"):
        minimum = result[f"{prefix}_min"]
        maximum = result[f"{prefix}_max"]
        if (
            minimum is not None
            and maximum is not None
            and Decimal(minimum) > Decimal(maximum)
        ):
            raise ValidationError(
                "invalid_request", f"{label}{prefix}下限不能大于上限"
            )
    sort_by = str(value.get("sort_by") or "").strip().lower()
    direction = str(value.get("sort_direction") or "").strip().lower()
    if sort_by not in {"spend", "roas"} or direction not in {"asc", "desc"}:
        raise ValidationError("invalid_request", f"{label}排序设置无效")
    result["sort_by"] = sort_by
    result["sort_direction"] = direction
    return result


def _drama_rule(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "剧筛选")
    result = _range_rule(
        value,
        label="剧筛选",
        extra_optional={"resource_type_v2"},
    )
    types = value.get("resource_type_v2", [])
    if not isinstance(types, list) or len(types) > len(RESOURCE_TYPE_V2_LABELS):
        raise ValidationError("invalid_request", "剧类型必须是数组")
    normalized = []
    for item in types:
        text = str(item).strip()
        if isinstance(item, bool) or text not in RESOURCE_TYPE_V2_LABELS:
            raise ValidationError("invalid_request", "剧类型不在允许枚举中")
        if text not in normalized:
            normalized.append(text)
    result["resource_type_v2"] = normalized
    return result


def _material_rule(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "素材筛选")
    result = _range_rule(
        value,
        label="素材筛选",
        extra_required={"duration_min_seconds", "duration_max_seconds"},
    )
    minimum = _integer(
        value.get("duration_min_seconds"), "素材最小时长", 0, 3600
    )
    maximum = _integer(
        value.get("duration_max_seconds"), "素材最大时长", 1, 3600
    )
    if minimum > maximum:
        raise ValidationError("invalid_request", "素材时长下限不能大于上限")
    result["duration_min_seconds"] = minimum
    result["duration_max_seconds"] = maximum
    return result


def _schedule(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "发布时间")
    mode = str(value.get("mode") or "").strip().lower()
    if mode == "fixed":
        _keys(value, required={"mode", "times"}, label="发布时间")
        times = value.get("times")
        if not isinstance(times, list) or not 1 <= len(times) <= 24:
            raise ValidationError("invalid_request", "固定发布时间必须为1到24个")
        normalized = []
        for item in times:
            text = str(item or "").strip()
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", text):
                raise ValidationError("invalid_request", "固定发布时间格式无效")
            if text not in normalized:
                normalized.append(text)
        if len(normalized) != len(times):
            raise ValidationError("invalid_request", "固定发布时间不能重复")
        return {"mode": "fixed", "times": sorted(normalized)}
    if mode == "random":
        _keys(value, required={"mode", "daily_count"}, label="发布时间")
        return {
            "mode": "random",
            "daily_count": _integer(
                value.get("daily_count"), "每日随机次数", 1, 24
            ),
        }
    raise ValidationError("invalid_request", "发布时间模式无效")


def _caption_template(value: Any) -> str:
    template = _text(value, "发布文案", minimum=1, maximum=20000)
    try:
        render_caption_template(
            template,
            "123456",
            description="Drama description",
            defer_url=True,
            defer_code=True,
            defer_drama_name=True,
        )
    except TTPostError as exc:
        raise ValidationError(
            str(getattr(exc, "code", "invalid_caption_template")),
            str(exc),
            int(getattr(exc, "status", 400) or 400),
        ) from None
    return template


def normalize_template_payload(raw: Any) -> Dict[str, Any]:
    value = _mapping(raw, "模板")
    required = {
        "name",
        "account_ids",
        "caption_template",
        "drama_rule",
        "material_rule",
        "schedule",
    }
    optional = {
        "metric_window_days",
        "drama_launch_window_days",
        "cooldown_days",
        "platform",
    }
    _keys(value, required=required, optional=optional, label="模板")
    accounts = value.get("account_ids")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 100:
        raise ValidationError("invalid_request", "至少选择一个且最多100个账号")
    account_ids = []
    for item in accounts:
        normalized = str(item or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,30}", normalized):
            raise ValidationError("invalid_request", "账号ID无效")
        if normalized in account_ids:
            raise ValidationError("invalid_request", "账号ID不能重复")
        account_ids.append(normalized)
    platform = _integer(value.get("platform", 0), "平台", 0, 9)
    if platform != 0:
        raise ValidationError(
            "invalid_request", "当前版本仅支持platform=0", 400
        )
    return {
        "name": _text(value.get("name"), "模板名称", minimum=1, maximum=120),
        "account_ids": account_ids,
        "caption_template": _caption_template(value.get("caption_template")),
        "metric_window_days": _integer(
            value.get("metric_window_days", 7), "指标统计窗口", 1, 30
        ),
        "drama_launch_window_days": _integer(
            value.get("drama_launch_window_days", 0), "剧上线窗口", 0, 3650
        ),
        "cooldown_days": _integer(
            value.get("cooldown_days", 0), "剧冷却窗口", 0, 3650
        ),
        "platform": platform,
        "drama_rule": _drama_rule(value.get("drama_rule")),
        "material_rule": _material_rule(value.get("material_rule")),
        "schedule": _schedule(value.get("schedule")),
    }


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def config_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "ValidationError",
    "canonical_json",
    "config_hash",
    "expected_version",
    "is_placeholder_secret",
    "normalize_template_payload",
    "valid_internal_bearer",
]

"""Catalog value normalization and presentation, without network access."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import re


BUSINESS_COLUMNS = (
    "story_id", "story_name", "c_name", "channel_text", "introduce", "img_src",
    "lang_id", "is_full_text", "baidu_link", "baidu_link_code",
    "source_publish_time", "is_show", "local_type", "local_type_text",
    "audio_type", "audio_type_text", "holder_type", "holder_text",
    "payepisfrom", "raw_json",
)

LANGUAGES = {
    2: "繁体", 3: "英语", 4: "西语", 5: "葡语", 6: "法语", 7: "俄语",
    8: "意语", 9: "日语", 10: "阿语", 11: "印尼语", 12: "泰语",
    13: "越南语", 14: "韩语", 15: "菲律宾语", 16: "德语",
    17: "印地语", 18: "孟加拉语", 21: "马来西亚语", 22: "土耳其语",
}

_SOURCE_NAMES = {
    "baidu_link": "baiduLink",
    "baidu_link_code": "baiduLinkCode",
    "source_publish_time": "publish_time",
}
_VARCHAR_LIMITS = {
    "c_name": 255, "channel_text": 64, "is_full_text": 64,
    "baidu_link_code": 255, "source_publish_time": 64,
    "local_type_text": 64, "audio_type_text": 64, "holder_text": 64,
}
_TEXT_BYTE_LIMITS = {
    "story_name": 65535, "introduce": 16777215,
    "img_src": 65535, "baidu_link": 16777215,
}
_SMALLINT_FIELDS = {"lang_id", "local_type", "audio_type", "holder_type"}
_PRESERVE_NUMERIC = _SMALLINT_FIELDS | {"payepisfrom"}
_PRESERVE_TEXT = set(_VARCHAR_LIMITS) | set(_TEXT_BYTE_LIMITS)
_INTEGER = re.compile(r"^[+-]?\d+$")


def _integer(value, field, minimum, maximum, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError("字段 %s 必须是整数" % field)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _INTEGER.fullmatch(value):
        number = int(value)
    else:
        raise ValueError("字段 %s 必须是整数" % field)
    if number < minimum or number > maximum:
        raise ValueError("字段 %s 超出数据库取值范围" % field)
    return number


def _text(value, field):
    if value is None:
        return "" if field == "story_name" else None
    if not isinstance(value, str):
        raise ValueError("字段 %s 必须是文本" % field)
    if field in _VARCHAR_LIMITS and len(value) > _VARCHAR_LIMITS[field]:
        raise ValueError("字段 %s 超出数据库长度限制" % field)
    if field in _TEXT_BYTE_LIMITS and len(value.encode("utf-8")) > _TEXT_BYTE_LIMITS[field]:
        raise ValueError("字段 %s 超出数据库长度限制" % field)
    return value


def normalize_drama(raw):
    """Map one upstream row to the existing catalog's 20 business columns.

    Source IDs remain strings; the source time remains text because its time
    zone is unspecified. Unknown fields are retained in ``raw_json``.
    """
    if not isinstance(raw, dict):
        raise ValueError("短剧记录必须是对象")
    value = raw.get("story_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("短剧ID必须是非空字符串或整数")
    story_id = str(value)
    if not story_id.strip() or len(story_id) > 64:
        raise ValueError("短剧ID不能为空且长度不能超过64")
    is_show = _integer(raw.get("is_show"), "is_show", 0, 1, nullable=False)
    result = {"story_id": story_id, "is_show": is_show}
    for field in _PRESERVE_TEXT:
        result[field] = _text(raw.get(_SOURCE_NAMES.get(field, field)), field)
    for field in _SMALLINT_FIELDS:
        result[field] = _integer(raw.get(field), field, -32768, 32767)
    result["payepisfrom"] = _integer(raw.get("payepisfrom"), "payepisfrom", -2147483648, 2147483647)
    # Validate JSON compatibility before any SQL write and preserve source types.
    json.dumps(raw, ensure_ascii=False, allow_nan=False)
    result["raw_json"] = deepcopy(raw)
    return {field: result[field] for field in BUSINESS_COLUMNS}


def _valid_text(value):
    return isinstance(value, str) and value.strip() not in {"", "-"}


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def merge_drama(existing, raw):
    """Preserve last valid information only for explicitly delisted rows."""
    result = normalize_drama(raw)
    if not existing or result["is_show"] == 1:
        return result
    if str(existing.get("story_id")) != result["story_id"]:
        raise ValueError("不能将不同剧目的资料合并")
    for field in _PRESERVE_TEXT:
        if not _valid_text(result[field]) and _valid_text(existing.get(field)):
            result[field] = existing[field]
    for field in _PRESERVE_NUMERIC:
        if not _positive_integer(result[field]) and _positive_integer(existing.get(field)):
            result[field] = existing[field]
    return result


def _canonical_json(value):
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def equal_business(a, b):
    """Compare business data without treating JSON formatting as a change."""
    if not a or not b:
        return False
    for field in BUSINESS_COLUMNS:
        if field == "raw_json":
            try:
                if _canonical_json(a.get(field)) != _canonical_json(b.get(field)):
                    return False
            except (TypeError, ValueError):
                return False
        elif a.get(field) != b.get(field):
            return False
    return True


def language_name(lang_id):
    if lang_id is None or lang_id == "":
        return "未知语言"
    try:
        value = int(lang_id)
    except (ValueError, TypeError):
        return "未知语言（%s）" % lang_id
    if value <= 0:
        return "未知语言"
    return LANGUAGES.get(value, "未知语言（%s）" % value)


def _utc_display_time(value):
    if value is None or value == "":
        return value
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value)
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return value


class Catalog:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db

    @staticmethod
    def _present(row):
        if row is None:
            return None
        result = dict(row)
        result["language_name"] = language_name(result.get("lang_id"))
        for field in ("first_seen_at", "last_seen_at"):
            if field in result:
                result[field] = _utc_display_time(result[field])
        return result

    def list(self, params):
        result = dict(self.db.list_dramas(params))
        result["list"] = [self._present(row) for row in result.get("list", [])]
        return result

    def detail(self, story_id):
        return self._present(self.db.get_drama(str(story_id)))

"""Catalog value normalization and presentation, without network access."""

from copy import deepcopy
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime, timezone
import json
import re
import threading
import time


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


def _list_cache_key(params):
    """Canonicalize exactly the filters used by Database.list_dramas before cache lookup."""
    if not isinstance(params, dict):
        raise ValueError("分页或筛选参数无效")

    def scalar(name, default=None):
        value = params.get(name, default)
        return (value[0] if value else default) if isinstance(value, (list, tuple)) else value

    def integer(name, default, minimum, maximum):
        value = scalar(name, default)
        try:
            value = int(default if value in (None, "") else value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("分页或筛选参数无效")
        if not minimum <= value <= maximum:
            raise ValueError("分页或筛选参数超出范围")
        return value

    page = integer("page_index", 1, 1, 1000000)
    size = integer("page_size", 50, 1, 100)
    status = params.get("is_show", params.get("status", "1"))
    if isinstance(status, (list, tuple)):
        status = status[0] if status else "1"
    status = str(status)
    if status in ("all", "-1", ""):
        status = "all"
    elif status not in ("0", "1"):
        raise ValueError("上下架筛选参数无效")
    filters = tuple(None if scalar(name) in (None, "", "all") else integer(name, 0, -1, 32767)
                    for name in ("lang_id", "local_type", "audio_type"))
    query = params.get("q", params.get("search", ""))
    if isinstance(query, (list, tuple)):
        query = query[0] if query else ""
    query = str(query or "").strip()
    if len(query) > 200:
        raise ValueError("搜索内容过长")
    refresh = str(scalar("refresh", "0") or "0")
    if refresh not in ("0", "1"):
        raise ValueError("刷新参数无效")
    return (page, size, status, query) + filters, refresh == "1"


class Catalog:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._list_lock = threading.Lock()
        self._list_cache = OrderedDict()
        self._list_inflight = {}
        self._list_generation = object()
        self._list_epoch = 0

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

    def list(self, params, cache_generation=None):
        params = deepcopy(params or {})
        query_key, refresh = _list_cache_key(params)
        with self._list_lock:
            if cache_generation != self._list_generation:
                self._list_generation = cache_generation
                self._list_epoch += 1
                self._list_cache.clear()
            key = (self._list_epoch, query_key)
            pending = self._list_inflight.get(key)
            if pending is None:
                cached = self._list_cache.get(key)
                if not refresh and cached and cached[0] > time.monotonic():
                    self._list_cache.move_to_end(key)
                    return deepcopy(cached[1])
                self._list_cache.pop(key, None)
                pending = Future()
                self._list_inflight[key] = pending
                owner = True
            else:
                owner = False
        if not owner:
            return deepcopy(pending.result())
        try:
            result = deepcopy(self.db.list_dramas(params))
            result["list"] = [self._present(row) for row in result.get("list", [])]
        except BaseException as exc:
            with self._list_lock:
                self._list_inflight.pop(key, None)
                pending.set_exception(exc)
            raise
        with self._list_lock:
            # An older in-flight read must not repopulate a newer sync generation.
            if key[0] == self._list_epoch:
                self._list_cache[key] = (time.monotonic() + 30, result)
                while len(self._list_cache) > 64:
                    self._list_cache.popitem(last=False)
            self._list_inflight.pop(key, None)
            pending.set_result(result)
        # The service adds each user's promotion_status only to this detached response.
        return deepcopy(result)

    def detail(self, story_id):
        return self._present(self.db.get_drama(str(story_id)))

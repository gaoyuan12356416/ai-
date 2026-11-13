"""On-demand episodes and complete daily recommendations from the KOC v6.3 API."""
from collections import Counter, OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .catalog import LANGUAGES
from .promotions import PromotionError


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def utc_now():
    return datetime.now(timezone.utc)


def read_response(provider, endpoint, body):
    try:
        response = provider._post(endpoint, body)
        payload = response.json()
        if response.status_code != 200 or not isinstance(payload, dict) or payload.get("code") != 200:
            raise ValueError()
        return payload["data"]
    except Exception:
        raise PromotionError("供应方读取失败，请稍后重新获取", "provider_read_failed", 502) from None


class ContentTools:
    def __init__(self, settings, catalog, provider, clock=utc_now):
        self.settings, self.catalog, self.provider, self.clock = settings, catalog, provider, clock
        self.lock = threading.Lock()
        self.episodes_cache = OrderedDict()
        self.locks = [threading.Lock() for _ in range(32)]

    def episodes(self, story_id, refresh=False):
        drama = self.catalog.detail(story_id)
        if drama is None:
            raise PromotionError("未找到该短剧", "not_found", 404)
        lang = drama.get("lang_id")
        if isinstance(lang, bool) or not isinstance(lang, int) or lang <= 0:
            raise PromotionError("该剧缺少有效语言编号，请先更新剧库", "language_missing", 409)
        key = (story_id, lang)
        lock = self.locks[int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % len(self.locks)]
        with lock:
            with self.lock:
                cached = self.episodes_cache.get(key)
            if not refresh and cached and self.clock().timestamp() < cached["usable_until"]:
                return deepcopy(cached["data"])
            started = self.clock()
            raw = read_response(self.provider, "/api/story/getepislist", {
                "story_id": story_id, "lang_id": lang, "project_type": 2,
            })
            if not isinstance(raw, list) or len(raw) > 5000:
                raise PromotionError("剧集列表格式异常", "invalid_episodes", 502)
            episodes, seen = [], set()
            for row in raw:
                try:
                    number = row["epis_no"]
                    if isinstance(number, bool) or int(number) != float(number) or int(number) <= 0:
                        raise ValueError()
                    number = int(number)
                    url = row["epis_url"]
                    parsed = urlsplit(url)
                    if (not isinstance(url, str) or len(url) > 8192 or parsed.scheme != "https"
                            or not parsed.hostname or parsed.username or parsed.password
                            or any(ord(c) < 32 for c in url) or number in seen):
                        raise ValueError()
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise PromotionError("剧集地址或集数异常，请重新获取", "invalid_episodes", 502) from None
                seen.add(number)
                episodes.append({"epis_no": number, "epis_url": url})
            # Provider documents one hour. Start before the request, and retire 60s early.
            result = {"story_id": story_id, "story_name": drama.get("story_name"), "lang_id": lang,
                      "list": sorted(episodes, key=lambda x: x["epis_no"]),
                      "fetched_at": started.isoformat(), "expires_at": (started + timedelta(hours=1)).isoformat()}
            with self.lock:
                self.episodes_cache[key] = {"usable_until": started.timestamp() + 3540, "data": result}
                self.episodes_cache.move_to_end(key)
                while len(self.episodes_cache) > 64:
                    self.episodes_cache.popitem(last=False)
            return deepcopy(result)

    def recommendations(self, query):
        today = self.clock().astimezone(timezone(timedelta(hours=8))).date()
        try:
            day = date.fromisoformat(str(query.get("query_date") or today.isoformat()))
            business = int(query.get("project_type", 2))
            page = int(query.get("page_index", 1))
            size = int(query.get("page_size", 50))
            if day > today or business not in (1, 2) or not 1 <= page <= 10000 or size not in (50, 100):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("请选择当天或历史日期，业务类型和分页参数无效") from None
        scope = hashlib.sha256(self.settings.institution_user_id.encode()).hexdigest()[:16]
        path = Path(self.settings.data_dir) / "recommendations" / scope / ("%s-%s.json" % (business, day))
        lock = self.locks[int(hashlib.sha256(str(path).encode()).hexdigest(), 16) % len(self.locks)]
        with lock:
            cached = None
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            age = time.time() - float((cached or {}).get("saved_epoch", 0))
            fresh = cached and 0 <= age < 600 and query.get("refresh") != "1"
            if not fresh:
                rows, seen, expected = [], set(), None
                for index in range(1, 201):
                    data = read_response(self.provider, "/api/story/recommend", {
                        "project_type": business, "query_date": day.isoformat(), "page_index": index, "page_size": 100,
                    })
                    if (not isinstance(data, dict) or type(data.get("total")) is not int
                            or not 0 <= data["total"] <= 20000 or not isinstance(data.get("list"), list)):
                        raise PromotionError("推荐列表格式异常，原有快照已保留", "invalid_recommendations", 502)
                    if expected is None:
                        expected = data["total"]
                    elif expected != data["total"]:
                        raise PromotionError("推荐总数发生变化，请重新获取", "incomplete_recommendations", 502)
                    chunk = data["list"]
                    if len(chunk) != min(100, expected - len(rows)):
                        raise PromotionError("推荐分页不完整，原有快照已保留", "incomplete_recommendations", 502)
                    for row in chunk:
                        if (not isinstance(row, dict) or isinstance(row.get("story_id"), bool)
                                or not isinstance(row.get("story_id"), (str, int))
                                or not str(row["story_id"]).strip() or len(str(row["story_id"])) > 64
                                or str(row["story_id"]) in seen):
                            raise PromotionError("推荐剧目 ID 无效或重复", "invalid_recommendations", 502)
                        seen.add(str(row["story_id"]))
                        rows.append(row)
                    if len(rows) == expected:
                        break
                if len(rows) != expected:
                    raise PromotionError("推荐分页超出范围，原有快照已保留", "incomplete_recommendations", 502)
                cached = {"list": rows, "saved_epoch": time.time(), "fetched_at": self.clock().isoformat()}
                atomic_json(path, cached)
        rows = []
        for source in cached["list"]:
            row = {key: source.get(key) for key in ("story_id", "story_name", "lang_id", "recommend_time",
                   "recommend_reason", "recommend_remark", "recommend_grade", "tags")}
            row["story_id"] = str(row["story_id"])
            row["language_name"] = LANGUAGES.get(row["lang_id"], "未知语言（%s）" % row["lang_id"])
            rows.append(row)
        all_rows = rows
        search = str(query.get("q", "")).strip().casefold()[:200]
        rows = [r for r in rows if (not search or search in (r["story_id"] + " " + str(r["story_name"])).casefold())
                and (not query.get("lang_id") or str(r["lang_id"]) == str(query["lang_id"]))
                and (not query.get("grade") or str(r["recommend_grade"]) == str(query["grade"]))]
        return {"list": rows[(page-1)*size:page*size], "total": len(rows), "source_total": len(all_rows),
                "page_index": page, "page_size": size, "query_date": day.isoformat(), "project_type": business,
                "fetched_at": cached["fetched_at"],
                "summary": {"grades": dict(Counter(str(r["recommend_grade"] or "未分级") for r in all_rows)),
                            "languages": dict(Counter(r["language_name"] for r in all_rows))},
                "languages": sorted({(str(r["lang_id"]), r["language_name"]) for r in all_rows})}

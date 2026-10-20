"""Bounded, read-only Page and video catalog adapters for manual publishing."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from features.fb_auto_posts.languages import page_language
from features.fb_auto_posts.validation import LANGUAGE_ALIASES
from .runtime_source import BrokerReadOnlyMySQL, GatedReadOnlyMySQL
from .templates import TemplateError

MAX_MATERIALS = 200
PAGE_SIZE = 100
SOURCE_HOST = "advertising-1306474899.cos.ap-hongkong.myqcloud.com"
SOURCE_CDN_HOST = "socialkit-cdn.yingliang.tech"


class SourceError(TemplateError):
    pass


def _ids(values, maximum: int, label: str) -> list[str]:
    if (not isinstance(values, (list, tuple)) or not values or len(values) > maximum
            or any(isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,30}", str(value)) for value in values)):
        raise SourceError("invalid_request", f"{label}必须为1至{maximum}个有效ID")
    result = [str(value) for value in values]
    if len(result) != len(set(result)):
        raise SourceError("invalid_request", f"{label}不可重复")
    return result


def _language_sql(column: str) -> str:
    normalized = f"LOWER(TRIM(REPLACE(COALESCE({column},''),'_','-')))"
    aliases = " ".join("WHEN '%s' THEN '%s'" % item for item in sorted(LANGUAGE_ALIASES.items()))
    return f"(CASE {normalized} {aliases} ELSE {normalized} END)"


def _thumbnail(value) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        if (parsed.scheme not in ("https", "http") or parsed.hostname not in (SOURCE_HOST, SOURCE_CDN_HOST)
                or (parsed.scheme == "http" and parsed.hostname != SOURCE_HOST)
                or parsed.port is not None or parsed.username or parsed.password or parsed.fragment):
            return ""
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return ""


def prepare_source_url(value) -> str:
    """Accept only the two already approved source origins; never follow redirects."""
    try:
        text = str(value or "").strip()
        parsed = urlsplit(text)
        if (parsed.scheme not in ("https", "http") or parsed.hostname not in (SOURCE_HOST, SOURCE_CDN_HOST)
                or (parsed.scheme == "http" and parsed.hostname != SOURCE_HOST)
                or parsed.port is not None or parsed.username or parsed.password or parsed.fragment
                or not parsed.path.lower().endswith(".mp4") or len(text) > 4096
                or any(ord(char) < 32 for char in text)):
            raise ValueError("invalid source URL")
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        raise SourceError("material_unavailable", "素材必须来自已批准的HTTPS视频来源", 409) from None


class SourceRepository:
    def __init__(self, page_repository, material_repository, *, now_fn=None):
        self.pages, self.materials = page_repository, material_repository
        self.mysql = material_repository.mysql
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        for schema in (self.mysql.schema, self.mysql.blacklist_schema):
            if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
                raise ValueError("invalid source schema")

    def list_pages(self, actor: Mapping[str, Any], q: str = "") -> list[dict]:
        admin = actor.get("is_admin") is True
        owner = str(actor.get("business_owner_id") or "")
        groups = self.pages.list_groups(is_admin=admin, owner_user_id=owner)
        group_ids = [str(group.group_id) for group in groups
                     if str(group.app_id) == "1479" and str(group.product).casefold() == "dramawave"]
        if not group_ids:
            return []
        # Restrict group IDs before Page lookup; the repository repeats the owner guard.
        rows = self.pages.list_pages(group_ids, is_admin=admin, owner_user_id=owner)
        legacy = {str(item["overlap_page_id"]) for item in self.pages.legacy_conflicts(group_ids)}
        seen, result = set(), []
        search = str(q or "").strip().casefold()
        if len(search) > 200:
            raise SourceError("invalid_request", "Page搜索内容过长")
        for page in rows:
            pid = str(page.page_id)
            if pid in seen:
                raise SourceError("page_identity_ambiguous", "Page来源存在重复身份", 409)
            seen.add(pid)
            name = str(page.page_name or pid)
            if search and search not in (pid + " " + name).casefold():
                continue
            conflict = pid in legacy
            authorized = int(page.eligible_token_count) > 0
            reason = "旧版发布队列仍在使用此Page" if conflict else "" if authorized else "Page授权已失效或缺少有效授权"
            result.append({
                "page_id": pid, "page_name": name, "language": page_language(page.language),
                "eligible_token_count": int(page.eligible_token_count),
                "group_ids": list(page.group_ids), "blocked_reason": reason,
                "legacy_conflict": conflict,
            })
        return result

    def get_pages(self, actor: Mapping[str, Any], page_ids) -> dict[str, dict]:
        wanted = _ids(page_ids, 200, "Page")
        available = {page["page_id"]: page for page in self.list_pages(actor)}
        if any(pid not in available for pid in wanted):
            raise SourceError("page_not_found", "所选Page不在当前账号可访问的DramaWave范围内", 404)
        for pid in wanted:
            page = available[pid]
            if page["blocked_reason"]:
                raise SourceError("page_unavailable", f"Page {pid}：" + page["blocked_reason"], 409)
        return {pid: available[pid] for pid in wanted}

    def _conditions(self):
        schema, blocked = self.mysql.schema, self.mysql.blacklist_schema
        source_language, drama_language = _language_sql("s.language"), _language_sql("d.language")
        sql = f"""
            s.data_source=%s AND s.product=%s AND s.type=2 AND s.is_delete=0
            AND TRIM(COALESCE(s.data_source_id,''))<>'' AND TRIM(COALESCE(s.language,''))<>''
            AND s.video_duration>0 AND s.video_duration<=3600
            AND EXISTS (SELECT 1 FROM `{schema}`.ads_drama_info d FORCE INDEX(ac)
                WHERE d.content_id=s.data_source_id AND d.app_id=%s AND d.release_status=1
                AND d.deploy_time>0 AND d.deploy_time<=%s AND {drama_language}={source_language})
            AND NOT EXISTS (SELECT 1 FROM `{blocked}`.ads_facebook_post_blacklist b
                WHERE b.is_delete=0 AND b.type=1 AND b.content_id=s.data_source_id)
            AND NOT EXISTS (SELECT 1 FROM `{schema}`.ads_drama_info db FORCE INDEX(ac)
                JOIN `{blocked}`.ads_facebook_post_blacklist b
                  ON b.is_delete=0 AND b.type=0 AND b.content_id=db.series_code
                WHERE db.content_id=s.data_source_id AND db.app_id=%s)
        """
        return sql, ["6", "Dramawave", "1479", int(self.now_fn().timestamp()), "1479"]

    def _select(self, extra="", params=(), limit=101):
        condition, base_params = self._conditions()
        sql = f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(s.id AS CHAR) material_id,TRIM(s.data_source_id) content_id,
                   COALESCE(s.name,'') material_name,COALESCE(s.tag_name,'') material_tag,
                   COALESCE(s.url,'') media_url,COALESCE(s.cover,'') thumbnail_url,
                   COALESCE(s.language,'') language,s.video_duration,
                   COALESCE(CAST(s.user_id AS CHAR),'0') uploader_id
              FROM `{self.mysql.schema}`.ads_custom_source s FORCE INDEX(PRIMARY)
             WHERE {condition} {extra}
             ORDER BY s.id DESC LIMIT %s
        """
        return self.mysql.select(sql, base_params + list(params) + [limit])

    @staticmethod
    def _material(row):
        material_id = str(row.get("material_id") or "")
        content_id = str(row.get("content_id") or "").strip()
        language = page_language(row.get("language"))
        try:
            duration = Decimal(str(row.get("video_duration") or 0))
            url = prepare_source_url(row.get("media_url"))
            if (not re.fullmatch(r"[1-9][0-9]{0,18}", material_id) or not 0 < len(content_id) <= 256
                    or not language or not duration.is_finite() or not 0 < duration <= 3600
                    or not str(row.get("material_name") or "").strip() or len(url) > 4096
                    or any(ord(char) < 32 for char in url)):
                raise ValueError("invalid source material")
        except (ValueError, InvalidOperation, RuntimeError):
            raise SourceError("material_unavailable", "素材缺少有效视频、时长、剧集或语言信息", 409) from None
        return {
            "material_id": material_id, "material_name": str(row["material_name"]).strip(),
            "content_id": content_id, "language": language, "media_url": url,
            "duration_seconds": float(duration), "material_tag": str(row.get("material_tag") or "").strip(),
            "thumbnail_url": _thumbnail(row.get("thumbnail_url")), "uploader_id": str(row.get("uploader_id") or "0"),
            "drama_name": "", "drama_description": "",
        }

    def _enrich(self, materials: list[dict], *, strict: bool, need_desc: bool):
        if not materials:
            return []
        ids = sorted({item["content_id"] for item in materials})
        placeholders = ",".join("%s" for _ in ids)
        languages = sorted({item["language"] for item in materials})
        language_slots = ",".join("%s" for _ in languages)
        schema = self.mysql.schema
        # One bounded query per metadata kind, never one query per Page or material.
        dramas = self.mysql.select(f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(d.content_id AS CHAR) content_id,COALESCE(d.language,'') language,
                   TRIM(COALESCE(d.name,'')) drama_name
              FROM `{schema}`.ads_drama_info d FORCE INDEX(ac)
             WHERE d.app_id=%s AND d.content_id IN ({placeholders}) AND d.release_status=1
               AND d.deploy_time>0 AND d.deploy_time<=%s
               AND {_language_sql("d.language")} IN ({language_slots}) LIMIT 1001
        """, ["1479"] + ids + [int(self.now_fn().timestamp())] + languages)
        descriptions = self.mysql.select(f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ DISTINCT CAST(r.content_id AS CHAR) content_id,COALESCE(r.language,'') language,
                   TRIM(COALESCE(r.`desc`,'')) drama_description
              FROM `{schema}`.ads_drama_resource r FORCE INDEX(content_id)
             WHERE r.app_id=%s AND r.content_id IN ({placeholders}) AND r.type=2
               AND {_language_sql("r.language")} IN ({language_slots}) LIMIT 1001
        """, ["1479"] + ids + languages)
        if len(dramas) > 1000 or len(descriptions) > 1000:
            raise SourceError("material_metadata_too_large", "素材剧集关联结果超过安全范围，请缩小选择范围", 503)
        names, descs = {}, {}
        for rows, target, field in ((dramas, names, "drama_name"), (descriptions, descs, "drama_description")):
            for row in rows:
                key = (str(row.get("content_id") or "").strip(), page_language(row.get("language")))
                text = str(row.get(field) or "").strip()
                if text:
                    target.setdefault(key, set()).add(text)
        result = []
        for item in materials:
            key = (item["content_id"], item["language"])
            named, described = names.get(key, set()), descs.get(key, set())
            if len(named) != 1 or (need_desc and len(described) != 1):
                if strict:
                    raise SourceError("material_metadata_ambiguous", "所选素材缺少唯一的同语言剧名或简介", 409)
                continue
            item["drama_name"] = next(iter(named))
            item["drama_description"] = next(iter(described)) if len(described) == 1 else ""
            item["description_status"] = "matched" if len(described) == 1 else "ambiguous" if described else "missing"
            result.append(item)
        return result

    def search_materials(self, query: Mapping[str, Any]) -> dict:
        if not isinstance(query, Mapping):
            raise SourceError("invalid_request", "素材筛选格式无效")
        search = str(query.get("search") or "").strip()
        raw_language = str(query.get("language") or "").strip()
        language = page_language(raw_language) if raw_language else ""
        uploader = str(query.get("uploader_id") or "").strip()
        try:
            raw_limit = query.get("limit", 50)
            if isinstance(raw_limit, bool) or not re.fullmatch(r"[1-9][0-9]{0,2}", str(raw_limit)):
                raise ValueError("invalid limit")
            limit = int(raw_limit)
            if not 1 <= limit <= PAGE_SIZE:
                raise ValueError("invalid limit")
        except (TypeError, ValueError):
            raise SourceError("invalid_request", "每页素材数量必须为1至100") from None
        if (len(search) > 200 or (raw_language and not language)
                or (uploader and not re.fullmatch(r"0|[1-9][0-9]{0,18}", uploader))):
            raise SourceError("invalid_request", "素材筛选条件无效")
        filters = [search, language, uploader]
        fingerprint = hashlib.sha256(json.dumps(filters, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        extra, params, boundary = [], [], ""
        if search:
            extra.append(f"""AND (
                LOCATE(BINARY LOWER(%s),BINARY LOWER(CONCAT(CAST(s.id AS CHAR),' ',COALESCE(s.name,''))))>0
                OR EXISTS (SELECT 1 FROM `{self.mysql.schema}`.ads_drama_info ds FORCE INDEX(ac)
                    WHERE ds.content_id=s.data_source_id AND ds.app_id=%s AND ds.release_status=1
                      AND ds.deploy_time>0 AND ds.deploy_time<=%s
                      AND {_language_sql("ds.language")}={_language_sql("s.language")}
                      AND LOCATE(BINARY LOWER(%s),BINARY LOWER(COALESCE(ds.name,'')))>0))""")
            params.extend([search, "1479", int(self.now_fn().timestamp()), search])
        if language:
            extra.append("AND " + _language_sql("s.language") + "=%s")
            params.append(language)
        if uploader:
            extra.append("AND CAST(COALESCE(s.user_id,0) AS CHAR)=%s")
            params.append(uploader)
        cursor = str(query.get("cursor") or "")
        if cursor:
            try:
                if len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                    raise ValueError("cursor")
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
                if (set(decoded) != {"v", "before", "bound", "filter"} or decoded["v"] != 1
                        or decoded["filter"] != fingerprint
                        or not re.fullmatch(r"[1-9][0-9]{0,18}", decoded["before"])
                        or not re.fullmatch(r"[1-9][0-9]{0,18}", decoded["bound"])
                        or int(decoded["before"]) > int(decoded["bound"])):
                    raise ValueError("cursor")
                boundary = decoded["bound"]
                extra.extend(["AND s.id<%s", "AND s.id<=%s"])
                params.extend([decoded["before"], boundary])
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise SourceError("invalid_cursor", "分页标识无效或筛选条件已改变，请从第一页重新加载") from None
        rows = self._select(" ".join(extra), params, limit + 1)
        raw_ids = [str(row.get("material_id") or "") for row in rows]
        if (any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in raw_ids)
                or len(raw_ids) != len(set(raw_ids)) or raw_ids != sorted(raw_ids, key=int, reverse=True)):
            raise SourceError("material_source_ambiguous", "素材来源返回了无效或重复ID", 503)
        scanned = rows[:limit]
        materials = []
        for row in scanned:
            try:
                materials.append(self._material(row))
            except SourceError:
                continue
        items = self._enrich(materials, strict=False, need_desc=False)
        has_more = len(rows) > limit
        next_cursor = ""
        if has_more and scanned:
            value = {"v": 1, "before": str(scanned[-1]["material_id"]), "bound": boundary or raw_ids[0], "filter": fingerprint}
            next_cursor = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "limit": limit}

    def get_materials(self, material_ids, need_desc: bool = False) -> dict[str, dict]:
        wanted = _ids(material_ids, MAX_MATERIALS, "素材")
        if any(len(value) > 19 for value in wanted):
            raise SourceError("invalid_request", "素材ID无效")
        found = {}
        for offset in range(0, len(wanted), 100):
            chunk = wanted[offset:offset + 100]
            rows = self._select("AND s.id IN (" + ",".join("%s" for _ in chunk) + ")", chunk, len(chunk) + 1)
            current = []
            for row in rows:
                item = self._material(row)
                if item["material_id"] not in chunk or item["material_id"] in found:
                    raise SourceError("material_source_ambiguous", "素材来源返回了重复或非请求ID", 503)
                found[item["material_id"]] = item
                current.append(item)
            if {item["material_id"] for item in current} != set(chunk):
                raise SourceError("material_unavailable", "部分素材已删除、未上架、被列入黑名单或不符合发布条件，请重新选择", 409)
            self._enrich(current, strict=True, need_desc=need_desc)
        return {material_id: found[material_id] for material_id in wanted}

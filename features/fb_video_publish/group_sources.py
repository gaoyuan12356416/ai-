"""Read-only, exact-ID sources for the independent Facebook group material pool.

The group pool deliberately does not inherit manual publishing exemptions or
the old single-Page pool's product/source/deletion and source-language filters.
It still uses the existing FIFO SQL adapter and current Page authorization.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .group_language import (
    LEGACY_POLICY, POLICY_VERSION as SCOPE_POLICY_VERSION,
    default_selection, normalize_language, normalize_selection,
)
from .sources import SourceError, _ids, _thumbnail, prepare_source_url

# Material admission has not changed. Keep its frozen policy independent of
# the new Page-language selection and unrestricted-Page matching policy.
POLICY_VERSION = LEGACY_POLICY
QUERY_BATCH_SIZE = 100
MAX_SCOPE_ROWS = 20000
MAX_METADATA_ROWS = 10000
ID_RE = re.compile(r"[1-9][0-9]{0,30}")


def _chunks(values):
    for offset in range(0, len(values), QUERY_BATCH_SIZE):
        yield values[offset:offset + QUERY_BATCH_SIZE]


def _clean_text(value, *, maximum, required=True, campaign=False, multiline=False):
    text = str(value or "").strip()
    if ((required and not text) or len(text) > maximum
            or any(ord(char) < 32 and not (multiline and char in "\r\n\t") for char in text)
            or (campaign and "*" in text)):
        raise SourceError("material_metadata_invalid", "素材或剧目字段缺失或无法用于Facebook归因", 409)
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise SourceError("material_metadata_invalid", "素材或剧目字段编码无效", 409) from None
    return text


def _integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("invalid integer")
    return int(value)


class GroupPoolSource:
    def __init__(self, source_repository):
        # Keep initialization inert: legacy service fixtures and disabled group
        # pools must not need an external SQL source merely to construct a service.
        self.source = source_repository

    @property
    def mysql(self):
        mysql = self.source.mysql
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(mysql.schema)):
            raise SourceError("source_not_configured", "素材只读数据源配置无效", 503)
        return mysql

    def _select(self, sql, params, *, maximum):
        try:
            rows = self.mysql.select(sql, list(params))
        except Exception:
            # Database exceptions can include SQL, connection settings or token
            # values. None of those belong in per-item API errors or batch logs.
            raise SourceError("group_pool_source_unavailable", "素材池只读数据源查询失败，请稍后重试", 503) from None
        if not isinstance(rows, list) or len(rows) > maximum or any(not isinstance(row, Mapping) for row in rows):
            raise SourceError("group_pool_source_too_large", "素材池来源结果超过安全范围或格式无效", 503)
        return rows

    @staticmethod
    def _owner(actor):
        if not isinstance(actor, Mapping):
            raise SourceError("invalid_actor", "当前发布人身份无效", 403)
        admin = actor.get("is_admin") is True
        owner = str(actor.get("business_owner_id") or "")
        if not admin and not ID_RE.fullmatch(owner):
            raise SourceError("fb_auto_owner_mapping_missing", "当前账号未唯一映射到Page组负责人", 403)
        return admin, owner

    def _groups(self, actor, group_ids=None):
        admin, owner = self._owner(actor)
        batches = [None] if group_ids is None else _chunks(group_ids)
        found = {}
        for chunk in batches:
            selected = "" if chunk is None else " AND g.id IN (" + ",".join("%s" for _ in chunk) + ")"
            owned = "" if admin else " AND g.user_id=%s"
            params = ([] if chunk is None else list(chunk)) + ([] if admin else [owner])
            rows = self._select(f"""
                SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(g.id AS CHAR) group_id,
                       CAST(g.user_id AS CHAR) owner_user_id,g.type group_type,
                       COALESCE(g.name,'') group_name,CAST(g.app_id AS CHAR) app_id,
                       COALESCE(a.name,'') product,COUNT(DISTINCT i.page_id) total_pages
                  FROM `{self.mysql.schema}`.ads_facebook_page_group g
                  JOIN `{self.mysql.schema}`.ads_apps_setting a ON a.id=g.app_id
                  LEFT JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins i
                    ON i.group_id=g.id AND i.deleted_at=0 AND TRIM(i.page_id)<>''
                 WHERE g.is_delete=0 AND g.type IN (0,1)
                   AND g.app_id=1479 AND LOWER(a.name)='dramawave'{selected}{owned}
                 GROUP BY g.id,g.user_id,g.type,g.name,g.app_id,a.name
                 ORDER BY g.id LIMIT 20001
            """, params, maximum=MAX_SCOPE_ROWS)
            for row in rows:
                gid, group_owner = str(row.get("group_id") or ""), str(row.get("owner_user_id") or "")
                try:
                    group_type, count = _integer(row.get("group_type")), _integer(row.get("total_pages") or 0)
                except ValueError:
                    raise SourceError("group_source_invalid", "Page组来源数据无效", 503) from None
                if (not ID_RE.fullmatch(gid) or gid in found or group_type not in (0, 1)
                        or str(row.get("app_id")) != "1479"
                        or str(row.get("product") or "").casefold() != "dramawave"
                        or (not admin and group_owner != owner) or (chunk is not None and gid not in chunk)):
                    raise SourceError("group_source_invalid", "Page组来源范围发生变化，请刷新重试", 503)
                found[gid] = {
                    "id": gid, "group_id": gid, "name": str(row.get("group_name") or gid).strip()[:200],
                    "owner_user_id": group_owner, "group_type": group_type,
                    "app_id": "1479", "product": "Dramawave", "total_pages": count,
                }
            if len(found) > MAX_SCOPE_ROWS:
                raise SourceError("group_pool_source_too_large", "Page组来源超过安全范围", 503)
        return found

    def list_groups(self, actor) -> list[dict]:
        return list(self._groups(actor).values())

    def resolve_scope(self, actor, group_ids, *, page_language_selection=None,
                      policy_version=LEGACY_POLICY, page_ids=None) -> dict:
        wanted = _ids(group_ids, None, "Page组")
        if not isinstance(policy_version, str) or policy_version not in {LEGACY_POLICY, SCOPE_POLICY_VERSION}:
            raise SourceError("group_pool_policy_invalid", "Page语言选择政策无效", 409)
        selection = normalize_selection(default_selection() if page_language_selection is None else page_language_selection)
        if policy_version == LEGACY_POLICY and selection != default_selection():
            raise SourceError("group_pool_policy_invalid", "旧版组素材池不支持Page语言筛选", 409)
        selected_ids = None if page_ids is None else ([] if page_ids == [] else _ids(page_ids, None, "Page"))
        admin, owner = self._owner(actor)
        groups = self._groups(actor, wanted)
        if set(groups) != set(wanted):
            # One inaccessible group invalidates the complete configured scope;
            # never silently narrow a saved schedule to whichever groups remain.
            raise SourceError("group_not_found", "部分Page组不存在或不在当前发布人的可访问范围内", 403)
        members = defaultdict(list)
        total = 0
        for chunk in _chunks(wanted):
            for page_chunk in ([None] if selected_ids is None else _chunks(selected_ids)):
                slots = ",".join("%s" for _ in chunk)
                owner_clause = "" if admin else " AND g.user_id=%s"
                page_clause = "" if page_chunk is None else " AND i.page_id IN (" + ",".join("%s" for _ in page_chunk) + ")"
                rows = self._select(f"""
                    SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(g.id AS CHAR) group_id,
                           TRIM(i.page_id) page_id,COALESCE(i.language,'') language,
                           COALESCE(i.timezone,'') timezone
                      FROM `{self.mysql.schema}`.ads_facebook_page_group g
                      JOIN `{self.mysql.schema}`.ads_apps_setting a ON a.id=g.app_id
                      JOIN `{self.mysql.schema}`.ads_facebook_page_group_ins i ON i.group_id=g.id
                     WHERE g.is_delete=0 AND g.type IN (0,1) AND g.app_id=1479
                       AND LOWER(a.name)='dramawave' AND i.deleted_at=0 AND TRIM(i.page_id)<>''
                       AND g.id IN ({slots}){owner_clause}{page_clause}
                     ORDER BY g.id,i.page_id LIMIT 20001
                """, chunk + ([] if admin else [owner]) + (page_chunk or []), maximum=MAX_SCOPE_ROWS)
                total += len(rows)
                if total > MAX_SCOPE_ROWS:
                    raise SourceError("group_pool_source_too_large", "Page组成员超过安全范围，请缩小选择范围", 503)
                for row in rows:
                    gid, pid = str(row.get("group_id") or ""), str(row.get("page_id") or "").strip()
                    if gid not in chunk or not ID_RE.fullmatch(pid) or (page_chunk is not None and pid not in page_chunk):
                        raise SourceError("group_source_invalid", "Page组成员来源范围或ID无效", 503)
                    raw_language = row.get("language")
                    members[pid].append({"group_id": gid, "language": normalize_language(raw_language),
                                         "blank": raw_language is None or (isinstance(raw_language, str) and not raw_language.strip()),
                                         "timezone": str(row.get("timezone") or "")[:64]})
        aggregated, summary_counts = {}, defaultdict(int)
        summary = {"items": [], "total_page_count": len(members), "conflict_page_count": 0,
                   "invalid_page_count": 0, "missing_page_count": 0}
        for pid in sorted(members, key=int):
            records = members[pid]
            gids = sorted({row["group_id"] for row in records}, key=int)
            languages = {row["language"] for row in records}
            specific = languages - {""}
            invalid = any(not row["blank"] and not row["language"] for row in records)
            code, message = "", ""
            language = next(iter(specific)) if len(specific) == 1 else ""
            kind = "specific" if language else "unrestricted"
            if len(specific) > 1:
                code, message, kind = "page_language_conflict", "Page在所选组中的成员语言冲突", "conflict"
                summary["conflict_page_count"] += 1
            elif policy_version == LEGACY_POLICY and "" in languages:
                code, message, kind = "page_language_missing", "Page组成员缺少有效语言", "missing"
                summary["missing_page_count"] += 1
            elif invalid:
                code, message, kind = "page_language_invalid", "Page组成员包含非法非空语言，请修正成员语言", "invalid"
                summary["invalid_page_count"] += 1
            else:
                summary_counts[(language, kind)] += 1
            aggregated[pid] = {"page_id": pid, "page_name": pid, "group_ids": gids, "group_id": gids[0],
                               "language": language, "language_kind": kind, "timezone": records[0]["timezone"],
                               "code": code, "message": message}
        summary["items"] = [{"language": language, "language_kind": kind, "page_count": count}
                            for (language, kind), count in sorted(summary_counts.items())]
        # v1 keeps its historical authorization read and exclusion semantics.
        # v2 rejects malformed/conflicting and unselected languages before any
        # token lookup, while the complete selected group scope was checked above.
        eligible_ids = []
        for pid, item in aggregated.items():
            if policy_version == LEGACY_POLICY:
                eligible_ids.append(pid)
            elif not item["code"]:
                selected = (selection["include_blank"] if item["language_kind"] == "unrestricted" else
                            selection["mode"] == "all" or item["language"] in selection["codes"])
                if selected:
                    eligible_ids.append(pid)
                else:
                    item["code"], item["message"] = "page_language_not_selected", "Page语言未在本次发布选择范围内"
        authorized = {}
        for chunk in _chunks(eligible_ids):
            try:
                rows = self.source.list_pages(actor, page_ids=chunk)
            except SourceError:
                raise
            except Exception:
                raise SourceError("group_pool_source_unavailable", "Page授权只读查询失败，请稍后重试", 503) from None
            for row in rows:
                pid = str(row.get("page_id") or "")
                if pid not in chunk or pid in authorized:
                    raise SourceError("group_source_invalid", "Page授权来源范围无效", 503)
                authorized[pid] = row
        pages, excluded = {}, []
        for pid, item in aggregated.items():
            page = authorized.get(pid, {})
            try:
                token_count = _integer(page.get("eligible_token_count") or 0)
            except ValueError:
                token_count = 0
            code, message = item["code"], item["message"]
            if not code and (not page or token_count < 1 or page.get("blocked_reason")):
                code, message = "page_unavailable", "Page不在当前授权范围内或缺少有效授权"
            base = {key: value for key, value in item.items() if key not in {"code", "message"}}
            base["page_name"] = str(page.get("page_name") or pid).strip()[:255]
            if code:
                excluded.append({**base, "code": code, "message": message, "blocked_reason": message})
                continue
            pages[pid] = {**base, "eligible_token_count": token_count, "blocked_reason": "", "legacy_conflict": False}
        return {"groups": [groups[gid] for gid in wanted], "pages": pages, "excluded": excluded,
                "language_summary": summary}

    def _material_rows(self, ids):
        slots = ",".join("%s" for _ in ids)
        return self._select(f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(s.id AS CHAR) material_id,
                   s.type material_type,COALESCE(s.data_source_id,'') content_id,
                   COALESCE(s.name,'') material_name,COALESCE(s.tag_name,'') material_tag,
                   COALESCE(s.url,'') media_url,COALESCE(s.cover,'') thumbnail_url,
                   s.video_duration,COALESCE(CAST(s.user_id AS CHAR),'0') uploader_id
              FROM `{self.mysql.schema}`.ads_custom_source s FORCE INDEX(PRIMARY)
             WHERE s.id IN ({slots}) ORDER BY s.id LIMIT %s
        """, [*ids, len(ids) + 1], maximum=len(ids))

    @staticmethod
    def _material(row):
        if str(row.get("material_type")) != "2":
            raise SourceError("material_not_video", "素材不是视频", 409)
        try:
            duration = Decimal(str(row.get("video_duration") or 0))
            if not duration.is_finite() or not 0 < duration <= 3600:
                raise ValueError("invalid duration")
        except (ValueError, InvalidOperation):
            raise SourceError("material_duration_invalid", "素材时长缺失或不在0至3600秒范围内", 409) from None
        return {
            "material_id": str(row["material_id"]),
            "content_id": _clean_text(row.get("content_id"), maximum=256),
            "material_name": _clean_text(row.get("material_name"), maximum=512),
            "material_tag": _clean_text(row.get("material_tag"), maximum=512, required=False, campaign=True),
            "media_url": prepare_source_url(row.get("media_url")), "duration_seconds": float(duration),
            "thumbnail_url": _thumbnail(row.get("thumbnail_url")),
            "uploader_id": str(row.get("uploader_id") or "0")[:31],
        }

    def _metadata(self, content_ids):
        slots = ",".join("%s" for _ in content_ids)
        resources = self._select(f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(r.content_id AS CHAR) content_id,
                   CAST(r.app_id AS CHAR) app_id,COALESCE(r.series_code,'') series_code,
                   COALESCE(r.language,'') language,COALESCE(r.name,'') drama_name,
                   COALESCE(r.`desc`,'') drama_description
              FROM `{self.mysql.schema}`.ads_drama_resource r FORCE INDEX(content_id)
             WHERE r.app_id=1479 AND r.content_id IN ({slots})
             ORDER BY r.id LIMIT 10001
        """, content_ids, maximum=MAX_METADATA_ROWS)
        releases = self._select(f"""
            SELECT /*+ MAX_EXECUTION_TIME(8000) */ CAST(d.content_id AS CHAR) content_id,
                   CAST(d.app_id AS CHAR) app_id,COALESCE(d.language,'') language,
                   d.release_status,d.deploy_time
              FROM `{self.mysql.schema}`.ads_drama_info d FORCE INDEX(ac)
             WHERE d.app_id=1479 AND d.content_id IN ({slots}) LIMIT 10001
        """, content_ids, maximum=MAX_METADATA_ROWS)
        grouped, deployed = defaultdict(list), defaultdict(list)
        for rows, target in ((resources, grouped), (releases, deployed)):
            for row in rows:
                content_id = str(row.get("content_id") or "").strip()
                if content_id not in content_ids or str(row.get("app_id")) != "1479":
                    raise SourceError("material_source_ambiguous", "剧目来源返回了非请求或非Dramawave映射", 503)
                target[content_id].append(row)
        return grouped, deployed

    def _enrich(self, material, resources, releases, *, need_desc):
        if not resources:
            raise SourceError("drama_mapping_missing", "素材未关联到Dramawave剧目", 409)
        canonical = {}
        for row in resources:
            language = normalize_language(row.get("language"))
            if not language:
                raise SourceError("drama_language_missing", "关联剧目缺少有效语言", 409)
            series = _clean_text(row.get("series_code"), maximum=128)
            name = _clean_text(row.get("drama_name"), maximum=512, campaign=True)
            description = _clean_text(row.get("drama_description"), maximum=4096, required=need_desc, multiline=True)
            canonical[(language, series, name, description)] = {
                "language": language, "series_code": series, "drama_name": name,
                "drama_description": description, "description_status": "matched" if description else "missing",
            }
        if len({key[0] for key in canonical}) != 1:
            raise SourceError("drama_language_ambiguous", "关联剧目存在多个语言，无法确定发布语言", 409)
        if len(canonical) != 1:
            raise SourceError("drama_mapping_ambiguous", "素材未关联到唯一剧目身份、剧名或简介", 409)
        drama = next(iter(canonical.values()))
        matched = [row for row in releases if normalize_language(row.get("language")) == drama["language"]]
        if not matched:
            raise SourceError("drama_not_released", "剧目缺少同语言Dramawave上架和投放时间", 409)
        now_fn = getattr(self.source, "now_fn", None) or (lambda: datetime.now(timezone.utc))
        now = int(now_fn().timestamp())
        deploy_times = []
        for row in matched:
            try:
                deploy = _integer(row.get("deploy_time"))
                released = _integer(row.get("release_status"))
            except ValueError:
                raise SourceError("drama_deploy_time_invalid", "剧目上架或投放时间数据无效", 409) from None
            if released != 1 or deploy <= 0:
                raise SourceError("drama_not_released", "剧目尚未上架或缺少有效投放时间", 409)
            deploy_times.append(deploy)
        if max(deploy_times) > now:
            raise SourceError("drama_not_yet_deliverable", "尚未到达剧目可投放时间", 409)
        return {**material, **drama, "drama_deploy_time": max(deploy_times), "policy_version": POLICY_VERSION}

    def resolve_materials(self, material_ids, need_desc=True) -> dict:
        if material_ids == []:
            return {"items": [], "errors": []}
        wanted = _ids(material_ids, None, "素材")
        if any(len(mid) > 19 for mid in wanted):
            raise SourceError("invalid_request", "素材ID无效")
        found, errors = {}, {}
        for chunk in _chunks(wanted):
            rows = self._material_rows(chunk)
            seen, materials = set(), []
            for row in rows:
                mid = str(row.get("material_id") or "")
                if mid not in chunk or mid in seen:
                    raise SourceError("material_source_ambiguous", "素材来源返回了重复或非请求ID", 503)
                seen.add(mid)
                try:
                    materials.append(self._material(row))
                except SourceError as exc:
                    errors[mid] = {"material_id": mid, "code": exc.code, "message": str(exc)}
            for mid in chunk:
                if mid not in seen:
                    errors[mid] = {"material_id": mid, "code": "material_not_found", "message": "素材ID不存在"}
            if not materials:
                continue
            content_ids = sorted({item["content_id"] for item in materials})
            resources, releases = self._metadata(content_ids)
            for material in materials:
                mid, content_id = material["material_id"], material["content_id"]
                try:
                    found[mid] = self._enrich(material, resources[content_id], releases[content_id], need_desc=need_desc)
                except SourceError as exc:
                    errors[mid] = {"material_id": mid, "code": exc.code, "message": str(exc)}
        return {"items": [found[mid] for mid in wanted if mid in found],
                "errors": [errors[mid] for mid in wanted if mid in errors]}

    def get_materials(self, material_ids, need_desc=True) -> dict[str, dict]:
        resolved = self.resolve_materials(material_ids, need_desc=need_desc)
        if resolved["errors"]:
            first = resolved["errors"][0]
            raise SourceError(first["code"], first["message"], 409)
        return {item["material_id"]: item for item in resolved["items"]}

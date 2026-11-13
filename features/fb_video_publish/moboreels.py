"""Explicit Moboreels FB branch. All remote calls happen outside store transactions."""
from __future__ import annotations
import re
import os
from urllib.parse import urlsplit
from features.moboreels_library import macro_client

class MoboreelsFBError(RuntimeError):
    code = "moboreels_link_unavailable"
    status = 409

def contains(*values):
    return macro_client.contains_macro(*values)


def institution_id():
    try:
        config = macro_client.read_env_file(os.environ.get("MOBOREELS_MACRO_ENV_FILE", "/etc/moboreels-macro.env"))
        institution = os.environ.get("MOBOREELS_INSTITUTION_USER_ID", config.get("MOBOREELS_INSTITUTION_USER_ID", ""))
    except OSError:
        institution = ""
    if not institution:
        raise MoboreelsFBError("Moboreels机构范围尚未配置")
    return institution


def enrich(material):
    result = dict(material)
    story = str(result.get("content_id") or result.get("data_source_id") or "")
    if not story:
        raise MoboreelsFBError("素材缺少 Moboreels 来源剧目 ID")
    try:
        row = macro_client.lookup_story(story)
    except macro_client.MoboreelsLinkError as exc:
        raise MoboreelsFBError(str(exc)) from None
    if str(row.get("story_id") or story) != story:
        raise MoboreelsFBError("Moboreels 剧目身份与素材不一致")
    langs = {2:"zh-tw",3:"en",4:"es",5:"pt",6:"fr",7:"ru",8:"it",9:"ja",10:"ar",11:"id",12:"th",13:"vi",14:"ko",15:"tl",16:"de",17:"hi",18:"bn",21:"ms",22:"tr"}
    result.update(drama_name=str(row.get("story_name") or row.get("name") or ""),
                  drama_description=str(row.get("introduce") or row.get("description") or ""),
                  moboreels_story_id=story, description_status="matched")
    if not result.get("language") or result.get("language") == "und":
        result["language"] = langs.get(int(row.get("lang_id") or -1), "und")
    return result

def validate(record, story):
    if not isinstance(record, dict) or record.get("status") != "succeeded" or str(record.get("story_id")) != str(story):
        raise MoboreelsFBError("Moboreels 推广结果尚未冻结，未调用平台")
    short = urlsplit(str(record.get("short_url") or ""))
    target = urlsplit(str(record.get("target_url") or ""))
    if (short.scheme != "https" or short.netloc != "ai.yingliangads.com" or not re.fullmatch(r"/m/[A-Za-z0-9_-]{8,128}",short.path)
            or short.query or short.fragment or target.scheme not in ("http","https") or not target.netloc or target.username or target.password):
        raise MoboreelsFBError("Moboreels 短链冻结数据无效，未调用平台")
    return record

def resolve(creator, material, *, page_id, task_id):
    uid = str((creator or {}).get("user_id") or "")
    if not uid:
        raise MoboreelsFBError("任务缺少真实创建者，不能生成 Moboreels 推广链接")
    try:
        actor = macro_client.resolve_actor(uid, str(creator.get("tenant_key") or ""), str(creator.get("name") or ""))
        result = macro_client.resolve_m_url(actor, story_id=str(material["content_id"]), platform="fb", account_id=str(page_id),
                                           task_id=str(task_id), material_id=str(material["material_id"]), purpose="post")
    except macro_client.MoboreelsLinkError as exc:
        raise MoboreelsFBError(str(exc)) from None
    return validate(result, material["content_id"])

def render_auto(template, material, short=""):
    if "{{url}}" in str(template):
        raise MoboreelsFBError("Moboreels文案不可混用{{url}}，请仅使用{{m_url}}")
    values = {"drama_name":material.get("drama_name",""), "material_name":material.get("material_name",""),
              "content_id":material.get("content_id",""), "desc":material.get("drama_description",""), "m_url":short or "{{m_url}}"}
    # Source metadata is substituted once; braces inside titles cannot add a link.
    return re.sub(r"\{\{(drama_name|material_name|content_id|desc|m_url)\}\}", lambda m:str(values[m.group(1)] or ""), str(template))


def source_materials(repo, material_ids, *, manual=False, page_pool=False):
    """Use the existing media policy and exact IDs, with Moboreels metadata only."""
    import inspect
    from .sources import SourceError, _ids, MAX_MATERIALS
    wanted = _ids(material_ids, None if manual else MAX_MATERIALS, "素材") if material_ids else []
    if any(len(mid) > 19 for mid in wanted):
        raise SourceError("invalid_request", "素材ID无效")
    found, errors = {}, {}
    for offset in range(0, len(wanted), 100):
        chunk = wanted[offset:offset + 100]
        rows = repo._select("AND s.id IN (" + ",".join("%s" for _ in chunk) + ")", chunk, len(chunk)+1,
                            enforce_blacklist=False, manual=manual, page_pool=page_pool, moboreels=True)
        seen=set()
        for row in rows:
            mid=str(row.get("material_id") or "")
            if mid not in chunk or mid in seen:
                raise SourceError("material_source_ambiguous", "素材来源返回了重复或非请求ID", 503)
            seen.add(mid)
            try:
                policy={"manual":manual}
                if "page_pool" in inspect.signature(repo._material).parameters:
                    policy["page_pool"]=page_pool
                found[mid]=enrich(repo._material(row, **policy))
            except (SourceError, MoboreelsFBError) as exc:
                errors[mid]={"code":exc.code,"message":str(exc)}
        for mid in set(chunk)-seen:
            errors[mid]={"code":"material_unavailable","message":"素材不存在或不符合当前视频来源条件"}
    return {"items":found,"errors":errors}

"""Freeze Moboreels promotion receipts before any auto FB media preparation/write."""
import dataclasses
import json
from features.fb_video_publish import moboreels

def intent(config, material, template):
    raw = dataclasses.asdict(material) if dataclasses.is_dataclass(material) else dict(material)
    raw = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
    return {"creator":{"user_id":str(template.get("created_by") or ""),"name":str(template.get("created_name") or ""),"tenant_key":""},
            "message_template":str(config["message_template"]),"material":raw}

def ensure_link(store, task):
    if not json.loads(task.get("moboreels_json") or "{}") and not moboreels.contains(task.get("message_text")):
        return task
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM fb_auto_task WHERE id=?",(task["id"],)).fetchone()
        if not row:
            raise moboreels.MoboreelsFBError("任务已不存在")
        current = dict(row)
    frozen = json.loads(current.get("moboreels_json") or "{}")
    if not frozen:
        if moboreels.contains(current.get("message_text")):
            raise moboreels.MoboreelsFBError("任务缺少Moboreels创建者快照，未调用平台")
        return current
    if frozen.get("link"):
        link = moboreels.validate(frozen["link"],current["content_id"])
        if current.get("short_url") != link["short_url"] or current.get("long_url") != link["target_url"]:
            raise moboreels.MoboreelsFBError("任务短链与冻结推广回执不一致")
        return current
    material = moboreels.enrich(frozen["material"])
    if str(material.get("material_id")) != str(current["material_id"]) or str(material.get("content_id")) != str(current["content_id"]):
        raise moboreels.MoboreelsFBError("任务素材与冻结Moboreels身份不一致")
    link = moboreels.resolve(frozen["creator"],material,page_id=current["page_id"],task_id="fb:auto:"+str(current["id"]))
    message = moboreels.render_auto(frozen["message_template"],material,link["short_url"])
    if len(message)>5000:
        raise moboreels.MoboreelsFBError("Moboreels宏展开后的文案超过5000字符，未调用平台")
    frozen.update(material=material,link=link)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM fb_auto_task WHERE id=?",(current["id"],)).fetchone()
        if not row or row["status"] not in ("planned","preparing","ready","running") or row["moboreels_json"] != current["moboreels_json"]:
            raise moboreels.MoboreelsFBError("任务在推广请求期间已改变，已保留回执且未调用平台")
        if current.get("lease_owner") and (row["lease_owner"] != current["lease_owner"] or row["lease_expires_at_utc"] != current["lease_expires_at_utc"]):
            raise moboreels.MoboreelsFBError("任务租约已改变，未调用平台")
        conn.execute("UPDATE fb_auto_task SET moboreels_json=?,message_text=?,short_url=?,long_url=? WHERE id=?",(json.dumps(frozen,ensure_ascii=False),message,link["short_url"],link["target_url"],current["id"]))
        conn.commit()
        return dict(conn.execute("SELECT * FROM fb_auto_task WHERE id=?",(current["id"],)).fetchone())


def validate_rules(config):
    from .repositories import RepositoryError
    from decimal import Decimal
    for rule in (config.get("drama_rule", {}), config.get("material_rule", {})):
        for metric in ("spend", "roas"):
            low, high=rule.get(metric+"_min"),rule.get(metric+"_max")
            if (low is not None and Decimal(str(low))>0) or high is not None:
                raise RepositoryError("moboreels_metrics_unsupported", "Moboreels暂不支持Dramawave消耗或ROAS筛选，请清空这些筛选", 409)
    if config.get("drama_rule", {}).get("resource_type_v2") or config.get("drama_launch_window_days"):
        raise RepositoryError("moboreels_rules_unsupported", "Moboreels分类及未注明时区的上架日期不能用于旧剧目类型或上线天数筛选，请清空这些筛选", 409)
    if config.get("hit_drama_publish") or config.get("feedback_selection", {}).get("enabled"):
        raise RepositoryError("moboreels_rules_unsupported", "Moboreels暂不支持旧Post效果或爆剧规则，请关闭该选材规则", 409)
    if "{{url}}" in str(config.get("message_template") or ""):
        raise RepositoryError("moboreels_mixed_url", "Moboreels文案不可混用{{url}}，请仅使用{{m_url}}", 409)


def candidate_snapshot(repo, config):
    """Reuse the authorized material boundary, never unrelated ad metric IDs."""
    from decimal import Decimal
    from .repositories import CandidateSnapshot, MaterialCandidate, RepositoryError, _https
    validate_rules(config)
    institution = moboreels.institution_id()
    rule=config["material_rule"]
    product=str(config.get("material_product") or config["product"])
    language=str(config["language"])
    language_ids={"zh-tw":2,"en":3,"es":4,"pt":5,"fr":6,"ru":7,"it":8,"ja":9,"ar":10,"id":11,"th":12,"vi":13,"ko":14,"tl":15,"de":16,"hi":17,"bn":18,"ms":21,"tr":22}
    source_language_sql="LOWER(TRIM(s.language))=%s"
    language_params=[language]
    if product.casefold()=="dramawavesource":
        source_language_sql="("+source_language_sql+" OR (TRIM(COALESCE(s.language,''))='' AND d.lang_id=%s))"
        language_params.append(language_ids.get(language,-1))
    page_size,cursor,page_count=1000,0,0
    limit=max(1,min(int(config.get("_candidate_limit",getattr(repo,"candidate_limit",5000))),getattr(repo,"candidate_limit",5000)))
    deadline=repo.monotonic_fn()+repo.catalog_deadline_seconds
    candidates=[]
    while True:
        if repo.monotonic_fn()>=deadline or page_count>=repo.catalog_max_pages:
            raise RepositoryError("moboreels_catalog_incomplete", "Moboreels素材目录扫描未完成，未创建发布任务", 409)
        rows=repo.mysql.select(f"""
          SELECT CAST(s.id AS CHAR) material_id,TRIM(s.data_source_id) content_id,
                 s.url media_url,COALESCE(s.name,'') material_name,COALESCE(s.tag_name,'') material_tag,
                 LOWER(TRIM(s.language)) language,s.video_duration,d.story_name drama_name,d.introduce drama_description
            FROM `{repo.mysql.schema}`.ads_custom_source s FORCE INDEX(PRIMARY)
            JOIN `ads_ai`.cdreader_koc_drama d ON d.story_id=s.data_source_id AND d.institution_user_id=%s AND d.project_type=2 AND d.is_show=1
           WHERE s.data_source=%s AND s.product=%s AND s.type=2 AND s.is_delete=0
             AND {source_language_sql} AND s.video_duration>=%s AND s.video_duration<=%s
             AND s.id>%s
           ORDER BY s.id LIMIT %s
        """,(institution,int(config["material_data_source"]),product,*language_params,rule["duration_min_seconds"],rule["duration_max_seconds"],cursor,page_size))
        page_count+=1
        for row in rows:
            mid=str(row.get("material_id") or "")
            if not mid.isdigit() or int(mid)<=cursor:
                raise RepositoryError("moboreels_catalog_ambiguous", "Moboreels素材目录存在重复或无效身份", 409)
            cursor=int(mid)
            url=_https(row.get("media_url"))
            duration=Decimal(str(row.get("video_duration") or 0))
            if not url or not duration.is_finite() or not Decimal(str(rule["duration_min_seconds"]))<=duration<=Decimal(str(rule["duration_max_seconds"])):
                continue
            values=dict(material_id=mid,content_id=str(row["content_id"]),media_url=url,
                material_name=str(row.get("material_name") or ""),drama_name=str(row.get("drama_name") or ""),
                language=language,duration_seconds=duration,material_spend=None,material_roas=None,
                drama_spend=None,drama_roas=None,resource_type_v2="moboreels",
                drama_description=str(row.get("drama_description") or ""),material_tag=str(row.get("material_tag") or ""))
            candidates.append(MaterialCandidate(**{key:value for key,value in values.items() if key in MaterialCandidate.__dataclass_fields__}))
        if len(rows)<page_size:
            break
    return CandidateSnapshot(tuple(candidates[:limit]),(),())

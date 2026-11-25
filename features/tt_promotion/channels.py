"""Channel settings and readback across the original and independent TikTok ledgers."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request

from features.tt_posts.core import TTPostAccountSettings, normalize_drama_language

BEIJING = timezone(timedelta(hours=8))
DEFAULT_DB = "/mnt/data-disk/tt-promotion/promotion.sqlite3"
AUTO_DB = "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3"
POOL_DB = "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3"


class ChannelError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def promotion_db():
    return Path(os.environ.get("TT_PROMOTION_DB", DEFAULT_DB))


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def initialize(path):
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS promotion_channel_setting(
            tenant TEXT NOT NULL, account_id TEXT NOT NULL,
            enabled INTEGER CHECK(enabled IN (0,1)), language TEXT,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant, account_id))""")
        columns={row[1] for row in db.execute("PRAGMA table_info(promotion_channel_setting)")}
        if "policy_json" not in columns:
            db.execute("ALTER TABLE promotion_channel_setting ADD COLUMN policy_json TEXT")
        if "version" not in columns:
            db.execute("ALTER TABLE promotion_channel_setting ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def _accounts_request(suffix="", body=None):
    token = os.environ.get("TT_POST_INTERNAL_TOKEN", "")
    if len(token) < 32:
        raise ChannelError(503, "频道身份服务尚未配置")
    req = urllib.request.Request("http://127.0.0.1:18829/api/admin/tt-posts/account-settings"+suffix,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer "+token, "Accept":"application/json","Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ChannelError(exc.code if exc.code in (400,403,404,409,429) else 503,
                           "无法核验频道当前能力，请检查账号授权和平台状态") from exc
    except (ValueError, OSError) as exc:
        raise ChannelError(503, "暂时无法读取 TT 频道身份") from exc
    return data


def public_accounts():
    data=_accounts_request()
    if not isinstance(data.get("items"), list):
        raise ChannelError(503, "频道身份数据不完整")
    return data


POLICY_FIELDS=("privacy_level","allow_comment","allow_duet","allow_stitch","brand_content_toggle","brand_organic_toggle","is_aigc")


def creator_info(aid, facts=None):
    accounts=(public_accounts() if facts is None else facts)["items"]
    if not any(str(a.get("source_account_id") or a.get("account_id"))==aid and a.get("publish_eligible") for a in accounts):
        raise ChannelError(404,"找不到可验证的授权频道")
    live=_accounts_request("/creator-info",{"source_account_id":aid}).get("item")
    if not isinstance(live, dict) or not isinstance(live.get("privacy_level_options"),list):
        raise ChannelError(503,"平台尚未返回频道发布能力")
    return live


def _stats():
    return {"old_templates": [], "new_templates": [], "inflight": [], "slots": set(),
            "today_published": 0, "last_published_at": None}


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _date(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _pending(rows, stats, source):
    for row in rows:
        stats[str(row["account_id"])]["inflight"].append({
            "id": row["id"], "source": source, "status": row["status"],
            "scheduled_at_utc": row["scheduled_at_utc"], "material_id": row["material_id"]})


def _published(rows, stats):
    for row in rows:
        channel = stats[str(row["account_id"])]
        channel["today_published"] += row["today_count"] or 0
        newest = _date(row["last_time"])
        old = _date(channel["last_published_at"])
        if newest and (not old or newest > old):
            channel["last_published_at"] = newest.isoformat()


def _auto_stats(db, stats, today, new, actor):
    source = "promotion" if new else "auto_template"
    key = "new_templates" if new else "old_templates"
    for row in db.execute("""SELECT t.id,t.name,v.config_json FROM tt_auto_template t
            JOIN tt_auto_template_version v ON t.id=v.template_id AND v.version=t.current_version
            WHERE t.enabled=1"""):
        cfg = json.loads(row["config_json"])
        if new:
            creator = cfg.get("creator") or {}
            if creator.get("tenant_key") != actor["tenant_key"] or (
                    actor.get("role") != "admin" and creator.get("user_id") != actor["user_id"]):
                continue
        for account in cfg.get("account_ids") or []:
            stats[str(account)][key].append({"id": row["id"], "name": row["name"], "source": source})
    if new:
        scope = "j.tenant=? AND (?='admin' OR json_extract(v.config_json,'$.creator.user_id')=?)"
        args = (actor["tenant_key"],actor.get("role"),actor["user_id"])
        joins = """ FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id
            JOIN tt_auto_template_version v ON v.template_id=j.template_id AND v.version=j.version """
        for row in db.execute("SELECT s.account_id,s.slot_key"+joins+
                              " WHERE "+scope+" AND date(s.scheduled_at,'+8 hours')=?", args+(today,)):
            stats[str(row[0])]["slots"].add((source, row[1]))
        native_join = joins + " JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id "
        _published(db.execute("SELECT t.account_id,MAX(t.published_at_utc) last_time,"
            "SUM(date(t.published_at_utc,'+8 hours')=?) today_count"+native_join+"WHERE "+scope+
            " AND t.status='published' AND t.unknown_outcome=0 AND COALESCE(t.publish_id,'')<>'' GROUP BY t.account_id",
            (today,)+args),stats)
        _pending(db.execute("SELECT t.id,t.account_id,t.status,t.material_id,s.scheduled_at AS scheduled_at_utc"+
            native_join+"WHERE "+scope+" AND (t.status IN ('queued','selecting','reserved','preparing','ready',"
            "'publishing','reconciling','unknown','retry_wait') OR t.unknown_outcome=1) ORDER BY s.scheduled_at",args),stats,source)
        return
    else:
        plans = set()
        for row in db.execute("SELECT source_key,target_id,ordinal,native_id,planned_at FROM social_random_slot WHERE local_date=?", (today,)):
            account = str(row["target_id"])
            stats[account]["slots"].add((source, row["source_key"], row["ordinal"]))
            plans.add((account, row["source_key"], _date(row["planned_at"])))
        for row in db.execute("""SELECT t.account_id,t.id,t.template_id,t.scheduled_at_utc FROM tt_auto_task t
             JOIN tt_auto_run r ON r.id=t.run_id WHERE r.shanghai_date=?""", (today,)):
            account = str(row["account_id"])
            if (account, "tt_auto:"+str(row["template_id"]), _date(row["scheduled_at_utc"])) not in plans:
                stats[account]["slots"].add((source, "task", row["id"]))
    _published(db.execute("""SELECT account_id,MAX(published_at_utc) last_time,
        SUM(date(published_at_utc,'+8 hours')=?) today_count FROM tt_auto_task
        WHERE status='published' AND unknown_outcome=0 AND COALESCE(publish_id,'')<>'' GROUP BY account_id""",(today,)),stats)
    _pending(db.execute("""SELECT id,account_id,status,material_id,scheduled_at_utc FROM tt_auto_task
        WHERE status IN ('queued','selecting','reserved','preparing','ready','publishing','reconciling','unknown','retry_wait')
        OR unknown_outcome=1 ORDER BY scheduled_at_utc,id"""),stats,source)


def _pool_stats(db, stats, today):
    rows = db.execute("SELECT account_ids_json FROM tt_post_auto_publish_config WHERE enabled=1")
    for row in rows:
        for aid in json.loads(row[0]):
            stats[str(aid)]["old_templates"].append({"id": "pool", "name": "TT Post 素材池排期","source":"material_pool"})
    for row in db.execute("""SELECT account_id FROM tt_post_daily_schedule WHERE enabled=1"""):
        stats[str(row[0])]["old_templates"].append({"id":"daily", "name":"素材池频道排期","source":"material_pool"})
    for row in db.execute("SELECT account_id,id FROM tt_post_schedule_run WHERE shanghai_date=?",(today,)):
        stats[str(row[0])]["slots"].add(("pool",row[1]))
    _published(db.execute("""SELECT q.account_id,MAX(e.created_at) last_time,
       SUM(date(e.created_at,'+8 hours')=?) today_count FROM tt_post_queue q JOIN
       (SELECT queue_id,MAX(created_at) created_at FROM tt_post_event WHERE to_status='published' GROUP BY queue_id) e
       ON e.queue_id=q.id WHERE q.status='published' AND q.unknown_outcome=0 AND COALESCE(q.publish_id,'')<>''
       GROUP BY q.account_id""",(today,)),stats)
    _pending(db.execute("""SELECT id,account_id,status,material_id,scheduled_at_utc FROM tt_post_queue
       WHERE status NOT IN ('published','failed','canceled','cancelled','skipped') OR unknown_outcome=1"""),stats,"material_pool")


def handoff(actor, db_path=None, auto_path=None, pool_path=None):
    stats = defaultdict(_stats)
    today = datetime.now(BEIJING).date().isoformat()
    with closing(readonly(auto_path or AUTO_DB)) as db:
        _auto_stats(db,stats,today,False,actor)
    with closing(readonly(pool_path or POOL_DB)) as db:
        _pool_stats(db,stats,today)
    with closing(readonly(db_path or promotion_db())) as db:
        if _table(db,"tt_promotion_report_plan"):
            _auto_stats(db,stats,today,True,actor)
    return stats


def _setting_rows(path, tenant):
    with closing(readonly(path)) as db:
        return {str(row["account_id"]): dict(row) for row in db.execute(
            "SELECT * FROM promotion_channel_setting WHERE tenant=?",(tenant,))}


def channel_data(actor, *, detail=None, path=None, facts=None, with_handoff=True):
    from .profiles import profiles_for_accounts
    tenant = actor["tenant_key"]
    path = path or promotion_db()
    facts = public_accounts() if facts is None else facts
    overrides = _setting_rows(path, tenant)
    current = {str(a.get("source_account_id") or a["account_id"]): a for a in facts["items"]}
    for aid in overrides:
        current.setdefault(aid, {"account_id": aid, "eligibility_reason": "频道不在当前可发布账号清单中"})
    ids = list(current) if detail is None else [str(detail)]
    if detail is not None and ids[0] not in current:
        raise ChannelError(404, "未找到频道")
    profile_set = profiles_for_accounts(ids, actor, detail=detail is not None or not with_handoff)
    stats = handoff(actor,path) if with_handoff else defaultdict(_stats)
    items = []
    for aid in ids:
        account = current[aid]
        base = account.get("account_settings") or {}
        override = overrides.get(aid,{})
        language = override.get("language") or base.get("drama_language") or "en"
        policy = json.loads(override["policy_json"]) if override.get("policy_json") else (
            {k:base[k] for k in POLICY_FIELDS if k in base} if base.get("configured") else {})
        configured=all(k in policy for k in POLICY_FIELDS)
        auth_eligible=bool(facts.get("account_source_available",True) and account.get("publish_eligible"))
        eligible=auth_eligible and configured
        reason=account.get("eligibility_reason") or ""
        if auth_eligible and not configured:
            reason="请在频道详情中设置观看范围、互动和推广披露"
        profile = profile_set["accounts"].get(aid) or {}
        windows = profile.get("windows", {})
        amount = windows.get("28d",{}).get("revenue_cents")
        audiences = windows.get("all",{}).get("audiences") or []
        first = next((a for a in audiences if a.get("key") not in ("unknown","__unknown__") and (a.get("revenue_cents") or 0)>0),{})
        st = stats[aid]
        enabled = override.get("enabled") is None or bool(override["enabled"])
        channel = dict(account_id=aid, nickname=account.get("account_name") or account.get("display_name") or aid,
                       username=account.get("username") or "", drama_language=language, language=language,
                       enabled=enabled, eligible=eligible, auth_eligible=auth_eligible, reason=reason,
                       policy_configured=configured, publish_policy=policy,
                       revenue_28d=None if amount is None else amount/100, has_profile=bool(first),
                       audience=first.get("name"), themes=[t.get("name") for t in first.get("children",[])[:3]],
                       today_expected=len(st["slots"]), today_published=st["today_published"],
                       last_published_at=st["last_published_at"], legacy_scheduled=bool(st["old_templates"]))
        if detail is not None:
            return {"channel":channel,"profile":profile,"meta":profile_set["meta"],
                    "handoff":{k:st[k] for k in ("old_templates","new_templates","inflight")}}
        effective=dict(policy,configured=configured,drama_language=language,version=override.get("version") or base.get("version") or 1)
        items.append(dict(channel,profile=profile,account_settings=effective) if not with_handoff else channel)
    return {"items":items,"meta":profile_set["meta"]}


def worker_channels(actor):
    return channel_data(actor,with_handoff=False)["items"]


def save_settings(actor, payload, facts=None, path=None):
    account_ids = payload.get("account_ids")
    values = payload.get("settings")
    if not isinstance(account_ids,list) or not account_ids or not isinstance(values,dict) or not values or set(values)-{"enabled","drama_language","publish_policy"}:
        raise ChannelError(400, "请选择频道和待修改的设置")
    if "enabled" in values and not isinstance(values["enabled"],bool):
        raise ChannelError(400, "参与状态须为布尔值")
    if "drama_language" in values and (not isinstance(values["drama_language"],str) or not values["drama_language"].strip()):
        raise ChannelError(400, "请选择剧语言")
    lang = normalize_drama_language(values["drama_language"]) if "drama_language" in values else None
    facts = public_accounts() if facts is None else facts
    path = path or promotion_db()
    existing = _setting_rows(path, actor["tenant_key"])
    visible = {str(a.get("source_account_id") or a.get("account_id")) for a in facts["items"]} | set(existing)
    ids = set(map(str,account_ids))
    if not ids <= visible:
        raise ChannelError(404, "所选频道不可见")
    policies={}
    if "publish_policy" in values:
        if len(ids)!=1 or not isinstance(values["publish_policy"],dict) or set(values["publish_policy"])!=set(POLICY_FIELDS):
            raise ChannelError(400,"请完成单个频道的发布设置")
        aid=next(iter(ids))
        raw=values["publish_policy"]
        setting=TTPostAccountSettings.from_mapping(raw)
        if setting.brand_content_toggle and setting.privacy_level=="SELF_ONLY":
            raise ChannelError(400,"第三方品牌推广不支持仅自己可见")
        from features.tt_posts.service import TTPostService
        TTPostService._assert_creator_settings(creator_info(aid,facts),setting)
        policies[aid]=json.dumps({k:getattr(setting,k) for k in POLICY_FIELDS})
    # Seed the first independent configuration from existing trusted defaults.
    # Later legacy edits cannot silently change a configured new channel.
    seeds={}
    for row in facts["items"]:
        old=row.get("account_settings") or {}
        if old.get("configured") and all(k in old for k in POLICY_FIELDS):
            seeds[str(row.get("source_account_id") or row.get("account_id"))]=json.dumps({k:old[k] for k in POLICY_FIELDS})
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(path,timeout=10)) as db,db:
        for aid in sorted(ids):
            policy=policies.get(aid)
            db.execute("""INSERT INTO promotion_channel_setting(tenant,account_id,enabled,language,updated_by,updated_at,policy_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,account_id) DO UPDATE SET enabled=COALESCE(excluded.enabled,enabled),
                language=COALESCE(excluded.language,language),policy_json=COALESCE(?,policy_json,excluded.policy_json),
                updated_by=excluded.updated_by,updated_at=excluded.updated_at,version=version+1""",
                (actor["tenant_key"],aid,values.get("enabled"),lang,actor["user_id"],now,policy or seeds.get(aid),policy))
    return {"updated":len(ids)}

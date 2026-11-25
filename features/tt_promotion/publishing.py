"""Operations for TT Promotion. All commands are scoped to the independent ledger."""

import argparse
import hashlib
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from copy import copy
from decimal import Decimal, InvalidOperation
from functools import cached_property
from datetime import datetime, timedelta

from features.tt_auto_posts.core import AuditActor
from . import pub_copy
from .pub_native import PublicationError, Runtime, number
from .pub_store import BEIJING, PreviewStore, connection, dump, install, iso, load, now

DEFAULT_DB = "/mnt/data-disk/tt-promotion/promotion.sqlite3"
TERMINAL = {"published", "failed", "no_candidate", "canceled", "cancelled", "skipped", "missed"}
_PREVIEW_WORKERS = threading.BoundedSemaphore(3)
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_ACTIVE = set()


def audit(actor):
    return AuditActor.from_values(
        actor["user_id"], actor.get("name") or actor.get("user_name") or actor["user_id"]
    )


def identity(actor):
    return {k: str(actor.get(k) or "") for k in ("user_id", "name", "tenant_key", "role")}


def clean_ids(value, label, pattern=r"[1-9][0-9]{0,30}", maximum=200):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise PublicationError(label + "数量无效")
    out = [str(i).strip() for i in value]
    if len(set(out)) != len(out) or any(not re.fullmatch(pattern, i) for i in out):
        raise PublicationError(label + "无效或重复")
    return out


def safe_caption(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode("utf-16-le")) // 2 > 2200:
        raise PublicationError("文案不能为空或超过平台上限")
    text = raw.strip()
    for token in ("{{drama_name}}", "{{content_id}}", "{{contect_id}}", "{desc}", "{url}", "{code}"):
        raw = raw.replace(token, "")
    if "{" in raw or "}" in raw:
        raise PublicationError("文案含不支持的推广宏")
    return text


def selection_rule(raw, defaults):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise PublicationError("筛选规则字段无效")
    rule = {**defaults, **raw}
    if rule["sort_by"] not in ("spend", "roas") or rule["sort_direction"] not in ("asc", "desc"):
        raise PublicationError("排序字段或方向无效")
    for lo, hi in [
        ("spend_min", "spend_max"),
        ("roas_min", "roas_max"),
        ("duration_min_seconds", "duration_max_seconds"),
    ]:
        if lo not in rule:
            continue
        for name in (lo, hi):
            val = rule[name]
            if val is None or val == "":
                rule[name] = None
                continue
            try:
                dec = Decimal(str(val))
                if (
                    not dec.is_finite()
                    or dec < 0
                    or dec > 1e15
                    or (name.startswith("duration") and dec > 3600)
                ):
                    raise ValueError()
                rule[name] = float(dec)
            except (ValueError, InvalidOperation):
                raise PublicationError("筛选范围必须为有效非负数") from None
        if rule[lo] is not None and rule[hi] is not None and rule[lo] > rule[hi]:
            raise PublicationError("范围下限超过上限")
    if "resource_type_v2" in rule:
        values = rule["resource_type_v2"]
        rule["resource_type_v2"] = (
            [] if values == [] else clean_ids(values, "资源类型", r"[A-Za-z0-9_-]{1,64}")
        )
    return rule


def config_for(payload, visible, actor, creator=None):
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise PublicationError("模板名称长度应为1到120个字符")
    ids = clean_ids(payload.get("account_ids"), "频道")
    if not set(ids) <= visible:
        raise PublicationError("所选频道超出可见范围", 403)
    sch = payload.get("schedule")
    if not isinstance(sch, dict):
        raise PublicationError("请选择排期")
    if sch.get("mode") == "fixed":
        times = clean_ids(sch.get("times"), "发布时间", r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", 24)
        sch = {"mode": "fixed", "times": sorted(times)}
    elif sch.get("mode") == "random":
        sch = {"mode": "random", "daily_count": number(sch.get("daily_count"), 1, 24, "随机发布次数")}
    else:
        raise PublicationError("排期模式无效")
    model = payload.get("model", "income")
    video = payload.get("video_template", "random_overlay")
    if (
        model not in ("income", "rules")
        or video not in ("random_overlay", "direct_outro")
        or type(payload.get("ai_copy", False)) != bool
    ):
        raise PublicationError("选材、制作或文案设置无效")
    base = {
        "spend_min": None,
        "spend_max": None,
        "roas_min": None,
        "roas_max": None,
        "sort_by": "roas",
        "sort_direction": "desc",
    }
    return name, dict(
        account_ids=ids,
        schedule=sch,
        model=model,
        video_template=video,
        ai_copy=payload.get("ai_copy", False),
        metric_window_days=number(payload.get("metric_window_days", 7), 1, 30, "指标窗口"),
        drama_launch_window_days=number(payload.get("drama_launch_window_days", 60), 0, 3650, "剧目窗口"),
        material_window_days=number(payload.get("material_window_days", 30), 0, 3650, "素材窗口"),
        cooldown_days=number(payload.get("cooldown_days", 0), 0, 3650, "同剧冷却"),
        caption_template=safe_caption(payload.get("caption_template", "{desc}\n{code}")),
        platform=0,
        drama_rule=selection_rule(payload.get("drama_rule"), {**base, "resource_type_v2": []}),
        material_rule=selection_rule(
            payload.get("material_rule"), {**base, "duration_min_seconds": 0, "duration_max_seconds": 3600}
        ),
        creator=creator or identity(actor),
    )


def when(payload):
    value = payload.get("scheduled_at")
    if not value:
        return iso(now())
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        at = iso(date)
    except (ValueError, TypeError):
        raise PublicationError("预约时间无效") from None
    if date < now() - timedelta(minutes=1):
        raise PublicationError("预约时间已过")
    return at


def options(payload):
    mode = payload.get("mode", "template")
    if mode not in ("template", "drama", "material", "pool", "serial"):
        raise PublicationError("推广方式无效")
    result = {"mode": mode}
    if mode == "drama":
        for key in ("content_ids", "resource_ids"):
            if payload.get(key):
                pattern = (
                    r"(?:[1-9][0-9]{0,30}|[A-Za-z][A-Za-z0-9_-]{0,63})"
                    if key == "resource_ids"
                    else r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
                )
                result[key] = clean_ids(payload[key], "指定剧目", pattern, 500)
        if "content_id" in payload and payload["content_id"]:
            old = clean_ids([payload["content_id"]], "指定剧目", r"[A-Za-z0-9_-]{1,128}")[0]
            result["content_ids"] = list(dict.fromkeys([*result.get("content_ids", []), old]))
        if not (result.get("content_ids") or result.get("resource_ids")):
            raise PublicationError("请选择至少一部剧或一个资源 ID")
    if mode == "serial":
        ident = str(payload.get("content_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            raise PublicationError("请填写有效剧目 ID")
        result["content_id"] = ident
    if mode == "material":
        result["material_id"] = clean_ids([payload.get("material_id")], "素材")[0]
    if mode == "pool" and payload.get("material_ids"):
        result["material_ids"] = clean_ids(payload["material_ids"], "素材池", maximum=1000)
    if mode == "serial" and payload.get("episodes"):
        result["episodes"] = clean_ids(payload["episodes"], "剧集素材", maximum=500)
    if payload.get("manual_caption"):
        result["manual_caption"] = safe_caption(payload["manual_caption"])
    return result


def public(template):
    return dict(
        id=template.id,
        name=template.name,
        version=template.version,
        enabled=template.enabled,
        config={k: v for k, v in template.config.items() if k != "creator"},
        updated_at=template.updated_at,
    )


def visible(template, actor, channels):
    owner = template.config.get("creator") or {}
    return (
        bool(owner.get("tenant_key"))
        and owner["tenant_key"] == actor.get("tenant_key")
        and set(template.config["account_ids"]) <= set(channels)
    )


def summarize(items):
    states = [i["status"] for i in items]
    if any(i["result_bucket"] == "review" for i in items):
        return "needs_review"
    if items and all(i["result_bucket"] == "confirmed" for i in items):
        return "published"
    if states and all(s in TERMINAL for s in states):
        return "completed_with_errors"
    return "running" if any(s not in ("planned", "enqueued") for s in states) else "planned"


def day_slots(template, account, date):
    schedule = template.config["schedule"]
    if schedule["mode"] == "fixed":
        times = schedule["times"]
    else:
        seed = f"{template.id}/{template.version}/{account}/{date}"
        rng = random.Random(seed)
        count = schedule["daily_count"]
        width = 1440 // count
        minutes = [i * width + rng.randrange(1, width) for i in range(count)]
        times = [f"{m // 60:02d}:{m % 60:02d}" for m in minutes]
    return [iso(datetime.fromisoformat(date + "T" + v).replace(tzinfo=BEIJING)) for v in times]


def selection_seed(t, account, at):
    day = datetime.fromisoformat(at).astimezone(BEIJING).date()
    return f"{t.id}:{t.version}:{account}:{day}"


class Publisher:
    def __init__(self, db_path=DEFAULT_DB, runtime=None):
        self.path = str(db_path)
        self.runtime = runtime or Runtime(self.path)
        self.store = self.runtime.store
        install(self.path)

    @cached_property
    def previews(self):
        return PreviewStore(self.path)

    @staticmethod
    def channel_map(channels):
        return {
            str(c["account_id"]): dict(c, drama_language=c.get("drama_language") or c.get("language") or "")
            for c in channels
        }

    def handle(self, method, path, payload, actor, channels):
        payload = dict(payload or {})
        scope = self.channel_map(channels)
        parts = path.strip("/").split("/")
        if not actor.get("tenant_key") or not actor.get("user_id"):
            raise PublicationError("请先登录", 401)
        if parts == ["runs"] and method == "GET":
            return self.runs(actor, scope)
        if len(parts) == 2 and parts[0] == "runs" and parts[1].isdigit() and method == "GET":
            return self.run_detail(int(parts[1]), actor, scope)
        if parts[0] != "templates":
            raise PublicationError("没有此操作", 404)
        if len(parts) == 1:
            if method == "GET":
                return {"items": [public(i) for i in self.store.list_templates() if visible(i, actor, scope)]}
            if method == "POST":
                name, cfg = config_for(payload, set(scope), actor)
                return public(
                    self.store.create_template(
                        name=name, config=cfg, actor=audit(actor), confirmation={"accepted": True}
                    )
                )
        if len(parts) < 2 or not parts[1].isdigit():
            raise PublicationError("模板无效", 404)
        t = self.store.get_template(int(parts[1]))
        if not visible(t, actor, scope):
            raise PublicationError("模板不存在或无权限", 404)
        if len(parts) == 2 and method == "GET":
            return public(t)
        if len(parts) == 4 and parts[2] == "draft" and method == "GET":
            return self.draft(t, actor, scope, parts[3])
        if method not in ("PUT", "POST"):
            raise PublicationError("操作无效", 405)
        if type(payload.get("expected_version")) != int or payload["expected_version"] != t.version:
            raise PublicationError("模板已更新，请刷新", 409)
        if len(parts) == 2:
            name, cfg = config_for(payload, set(scope), actor, t.config["creator"])
            return public(
                self.store.update_template(
                    t.id,
                    expected_version=t.version,
                    name=name,
                    config=cfg,
                    actor=audit(actor),
                    confirmation={"accepted": True},
                )
            )
        action = parts[2] if len(parts) == 3 else ""
        if action in ("enable", "disable"):
            result = self.store.set_template_enabled(
                t.id, action == "enable", expected_version=t.version, actor=audit(actor)
            )
            if action == "disable":
                with connection(self.path, write=True) as db:
                    db.execute(
                        """UPDATE ttp_slot SET state='skipped',reason='模板已暂停' WHERE state='planned' AND
                        job_id IN (SELECT id FROM ttp_job WHERE template_id=? AND trigger_type='auto')""",
                        (t.id,),
                    )
            return public(result)
        if action == "draft":
            return self.start_draft(t, actor, scope, payload)
        if action == "drafts":
            return self.start_drafts(t, actor, scope, payload)
        opts = options(payload)
        ids = clean_ids(payload.get("account_ids", t.config["account_ids"]), "频道")
        if not set(ids) <= set(t.config["account_ids"]):
            raise PublicationError("频道不属于此模板", 403)
        if action == "preview":
            return self.preview_request(t, actor, scope, ids, opts, payload)
        if action == "run":
            if opts["mode"] == "serial":
                ids = self._series_order(t, scope, ids, opts)
            op = str(payload.get("operation_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", op):
                raise PublicationError("缺少操作标识")
            # Scheduled time is omitted from the hash only for immediate requests.
            stamp = when(payload) if not self._already(op, actor) else None
            key = "manual:" + str(actor["tenant_key"]) + ":" + str(actor["user_id"]) + ":" + op
            digest_fields = [t.id, t.version, ids, opts, payload.get("scheduled_at")]
            if payload.get("preview_id") or payload.get("drafts"):
                digest_fields.extend([payload.get("preview_id"), payload.get("drafts")])
            digest = dump(digest_fields)
            if stamp and (payload.get("preview_id") or payload.get("drafts")):
                opts = self.reviewed(t, actor, scope, ids, opts, payload)
            job_id = self.plan(t, actor, [(i, stamp) for i in ids], opts, key, digest, "manual")
            return self.run_detail(job_id, actor, scope)
        raise PublicationError("没有此操作", 404)

    def draft(self, t, actor, scope, key):
        with connection(self.path) as db:
            r = db.execute(
                "SELECT * FROM ttp_draft WHERE slot_key=? AND actor_id=? AND template_id=?",
                (key, actor["user_id"], t.id),
            ).fetchone()
        if not r or r["account_id"] not in scope:
            raise PublicationError("未找到预演文案", 404)
        return dict(
            id=r["slot_key"],
            state=r["state"],
            source=r["source"],
            reason=r["reason"],
            caption=r["caption_body"],
            account_id=r["account_id"],
            material_id=r["material_id"],
            template_version=r["template_version"],
            copy_facts_key=pub_copy.facts_key(load(r["facts"])),
            origin_preview_id=load(r["facts"]).get("preview_id"),
        )

    def start_draft(self, t, actor, scope, payload):
        account = clean_ids([payload.get("account_id")], "频道")[0]
        mid = clean_ids([payload.get("material_id")], "素材")[0]
        if account not in t.config["account_ids"]:
            raise PublicationError("频道不属于模板", 403)
        op = str(payload.get("operation_id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", op):
            raise PublicationError("缺少操作标识")
        key = hashlib.sha256(
            dump([actor["tenant_key"], actor["user_id"], t.id, t.version, account, mid, op]).encode()
        ).hexdigest()
        with connection(self.path) as db:
            exists = db.execute("SELECT 1 FROM ttp_draft WHERE slot_key=?", (key,)).fetchone()
        if not exists:
            ch = scope[account]
            if not ch.get("eligible") or not ch.get("enabled"):
                raise PublicationError("频道不可用", 409)
            choice = self.runtime.preview(t, ch, {"mode": "material", "material_id": mid}, key, ())
            if str(choice["material_id"]) != mid:
                raise PublicationError("素材与请求不一致", 409)
            facts = pub_copy.draft_facts(key, ch, choice, t.config["caption_template"])
            with connection(self.path, write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO ttp_draft(slot_key,template_id,template_version,actor_id,account_id,material_id,created_at,facts) VALUES(?,?,?,?,?,?,?,?)",
                    (key, t.id, t.version, actor["user_id"], account, mid, iso(now()), dump(facts)),
                )
        return self.draft(t, actor, scope, key)

    def _already(self, op, actor):
        with connection(self.path) as db:
            return db.execute(
                "SELECT 1 FROM ttp_job WHERE op_key=?",
                ("manual:" + str(actor["tenant_key"]) + ":" + str(actor["user_id"]) + ":" + op,),
            ).fetchone()

    def snapshot(self, t, actor, scope, key):
        r = self.previews.get(str(key), t, actor, scope)
        if not r:
            raise PublicationError("预演不存在、已过期或不在可见范围", 404)
        if r["version"] != t.version:
            raise PublicationError("预演对应旧版模板，请重新预演", 409)
        return r

    def start_drafts(self, t, actor, scope, payload):
        snap = self.snapshot(t, actor, scope, payload.get("preview_id"))
        if snap["schedule_version"] != t.version:
            raise PublicationError("预演使用的是冻结的旧模板版本", 409)
        op = str(payload.get("operation_id") or "")
        rows = payload.get("items")
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", op)
            or not isinstance(rows, list)
            or not 1 <= len(rows) <= 200
        ):
            raise PublicationError("草稿请求无效")
        available = {}
        for r in self.previews.rows(snap["id"]):
            choice = load(r["result"])
            if choice.get("status") == "eligible":
                available[(r["account"], str(choice["material_id"]))] = choice
        ids = []
        snapshot_channels = load(snap["scope"])
        with connection(self.path, write=True) as db:
            for item in rows:
                if not isinstance(item, dict):
                    raise PublicationError("草稿请求无效")
                account, mid = str(item.get("account_id")), str(item.get("material_id"))
                choice = available.get((account, mid))
                if not choice:
                    raise PublicationError("所选频道和素材不属于本次可执行的预演结果", 409)
                key = hashlib.sha256(dump([snap["id"], account, mid, op]).encode()).hexdigest()
                if key in ids:
                    raise PublicationError("草稿频道及素材重复")
                ids.append(key)
                facts = pub_copy.draft_facts(
                    key,
                    snapshot_channels[account],
                    choice,
                    t.config["caption_template"],
                    episode=choice.get("episode"),
                )
                facts.update(tenant=actor["tenant_key"], preview_id=snap["id"])
                db.execute(
                    """INSERT OR IGNORE INTO ttp_draft(slot_key,template_id,template_version,actor_id,
                    account_id,material_id,created_at,facts) VALUES(?,?,?,?,?,?,?,?)""",
                    (key, t.id, t.version, actor["user_id"], account, mid, iso(now()), dump(facts)),
                )
        return {"items": [self.draft(t, actor, scope, key) for key in ids]}

    def reviewed(self, t, actor, scope, ids, opts, payload):
        snap = self.snapshot(t, actor, scope, payload.get("preview_id"))
        inputs = load(snap["inputs"])
        expected = inputs["opts"]
        selection = lambda values: {k: v for k, v in values.items() if k != "manual_caption"}
        if (
            snap["schedule_version"] != t.version
            or inputs.get("plan_date")
            or selection(expected) != selection(opts)
            or not set(ids) <= set(load(snap["scope"]))
            or inputs.get("scheduled_at") != (when(payload) if payload.get("scheduled_at") else None)
        ):
            raise PublicationError("预演与执行的模板、时间、频道或选材条件不一致", 409)
        by_account = {}
        for r in self.previews.rows(snap["id"]):
            if r["account"] in ids:
                row = load(r["result"])
                if not row:
                    raise PublicationError("频道预演尚未完成", 409)
                by_account[r["account"]] = {
                    k: row.get(k)
                    for k in (
                        "status",
                        "content_id",
                        "material_id",
                        "reason",
                        "scheduled_at_utc",
                        "seed",
                        "profile_version",
                        "drama_metrics",
                        "material_metrics",
                        "copy_facts_key",
                    )
                }
        if set(by_account) != set(ids):
            raise PublicationError("缺少本次执行所需的频道预演", 409)
        adoptions = {}
        requested = payload.get("drafts", [])
        if not isinstance(requested, list) or len(requested) > len(ids):
            raise PublicationError("逐条文案请求无效")
        with connection(self.path) as db:
            for item in requested:
                if not isinstance(item, dict):
                    raise PublicationError("逐条文案请求无效")
                acct, mid = str(item.get("account_id")), str(item.get("material_id"))
                r = db.execute(
                    """SELECT * FROM ttp_draft WHERE slot_key=? AND template_id=? AND
                    template_version=? AND actor_id=? AND account_id=? AND material_id=? AND state='final'""",
                    (str(item.get("draft_id")), t.id, t.version, actor["user_id"], acct, mid),
                ).fetchone()
                facts = load(r["facts"]) if r else {}
                if (
                    not r
                    or acct in adoptions
                    or acct not in by_account
                    or by_account[acct]["status"] != "eligible"
                    or by_account[acct]["material_id"] != mid
                    or facts.get("tenant") != actor["tenant_key"]
                    or not facts.get("preview_id")
                    or pub_copy.facts_key(facts) != by_account[acct].get("copy_facts_key")
                ):
                    raise PublicationError("草稿未定稿或与预演、频道、素材不匹配", 409)
                edited = "edited_caption" in item
                body = safe_caption(item["edited_caption"] if edited else r["caption_body"])
                adoptions[acct] = dict(
                    draft_id=r["slot_key"],
                    material_id=mid,
                    source="manual" if edited else r["source"],
                    generated_source=r["source"],
                    edited=edited,
                    caption=body,
                    actor=actor["user_id"],
                    template_version=t.version,
                    origin_preview_id=facts["preview_id"],
                    copy_facts_key=pub_copy.facts_key(facts),
                )
        return dict(opts, preview_id=snap["id"], preview_rows=by_account, adoptions=adoptions)

    def plan(self, t, actor, pairs, opts, key, facts, trigger):
        stamp = iso(now())
        digest = hashlib.sha256(facts.encode()).hexdigest()
        with connection(self.path, write=True) as db:
            row = db.execute("SELECT id,request_hash FROM ttp_job WHERE op_key=?", (key,)).fetchone()
            if row:
                if row[1] != digest:
                    raise PublicationError("操作标识已绑定其他请求", 409)
                return row[0]
            version = db.execute(
                "SELECT current_version,enabled FROM tt_auto_template WHERE id=?", (t.id,)
            ).fetchone()
            if (trigger == "manual" and version[0] != t.version) or (trigger == "auto" and not version[1]):
                raise PublicationError("模板已更新", 409)
            job = db.execute(
                "INSERT INTO ttp_job(op_key,request_hash,template_id,version,template_name,tenant,actor,trigger_type,opts,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    digest,
                    t.id,
                    t.version,
                    t.name,
                    actor["tenant_key"],
                    dump(identity(actor)),
                    trigger,
                    dump(opts),
                    stamp,
                ),
            ).lastrowid
            if opts.get("mode") == "pool":
                for m in opts.get("material_ids", []):
                    db.execute("INSERT OR IGNORE INTO ttp_pool VALUES(?,?)", (t.id, m))
            for acct, at in pairs:
                slot_opts = {k: v for k, v in opts.items() if k not in ("preview_rows", "adoptions")}
                if acct in opts.get("preview_rows", {}):
                    slot_opts["expected"] = opts["preview_rows"][acct]
                if acct in opts.get("adoptions", {}):
                    slot_opts["adopted_draft"] = opts["adoptions"][acct]
                # One new owner per account/minute. Older native run IDs remain separate.
                dup = db.execute(
                    """SELECT slot_key FROM ttp_slot WHERE account_id=? AND
                    abs((julianday(scheduled_at)-julianday(?))*86400)<60 AND state NOT IN ('skipped','missed') LIMIT 1""",
                    (acct, at),
                ).fetchone()
                state, reason = ("skipped", "与新版已有排期重叠") if dup else ("planned", "")
                db.execute(
                    "INSERT INTO ttp_slot(slot_key,job_id,account_id,scheduled_at,state,reason,opts,changed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (str(job) + ":" + acct, job, acct, at, state, reason, dump(slot_opts), stamp),
                )
            return job

    def _spec(self, db, t, acct, opts):
        opts = dict(opts)
        if opts["mode"] == "pool":
            added = set(opts.get("material_ids") or [])
            added.update(
                r[0] for r in db.execute("SELECT material_id FROM ttp_pool WHERE template_id=?", (t.id,))
            )
            if not added:
                raise PublicationError("素材池暂无素材", 409)
            opts["material_ids"] = sorted(added)
        if opts["mode"] == "template":
            for row in db.execute(
                "SELECT * FROM ttp_series WHERE template_id=? AND account_id=? ORDER BY rowid", (t.id, acct)
            ):
                episodes = load(row["episodes"])
                if not self._series_state(db, row, episodes)[1]:
                    opts.update(mode="serial", content_id=row["content_id"], episodes=episodes)
                    break
        if opts["mode"] == "serial":
            row = db.execute(
                "SELECT * FROM ttp_series WHERE tenant=? AND content_id=?",
                (t.config["creator"]["tenant_key"], opts["content_id"]),
            ).fetchone()
            episodes = list(opts.get("episodes") or (load(row["episodes"]) if row else []))
            if not episodes:
                raise PublicationError("请按集序提供素材 ID", 409)
            if row and (
                row["account_id"] != acct or row["template_id"] != t.id or episodes != load(row["episodes"])
            ):
                raise PublicationError("短剧已绑定其他频道或集序", 409)
            idx, complete = self._series_state(db, row, episodes)
            if complete:
                raise PublicationError("该连载已发完", 409)
            opts.update(episodes=episodes, episode=idx + 1, material_id=episodes[idx])
        draft = opts.get("adopted_draft")
        if draft:
            if opts.get("material_id") not in (None, draft["material_id"]):
                raise PublicationError("逐条文案与当前选定集或素材不一致", 409)
            opts["material_id"] = draft["material_id"]
        return opts

    @staticmethod
    def _series_state(db, row, episodes):
        if not row:
            return 0, False
        for idx, m in enumerate(episodes):
            state = db.execute(
                """SELECT s.opts,s.account_id,j.tenant,j.template_id,t.status,t.publish_id,t.unknown_outcome
                FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id
                AND t.account_id=s.account_id WHERE s.material_id=?""",
                (m,),
            ).fetchone()
            if not state:
                return idx, False
            opts = load(state["opts"])
            if (
                state["tenant"] != row["tenant"]
                or state["template_id"] != row["template_id"]
                or state["account_id"] != row["account_id"]
                or opts.get("mode") != "serial"
                or opts.get("content_id") != row["content_id"]
                or opts.get("episode") != idx + 1
            ):
                raise PublicationError("连载素材已被其他任务占用，请核对该集", 409)
            if state["status"] != "published" or not state["publish_id"] or state["unknown_outcome"]:
                raise PublicationError("上一集尚未确认发布或需要核对", 409)
        return len(episodes), True

    def gate(self, channel, at, *, own="", day_template=None):
        acct = str(channel["account_id"])
        if not channel.get("enabled"):
            return "频道已在新版暂停参与", []
        if not channel.get("eligible"):
            return channel.get("reason") or channel.get("eligibility_reason") or "频道当前不可发布", []
        if not channel.get("drama_language"):
            return "尚未设置剧语言", []
        self.runtime.settings(channel)
        hits = self.runtime.conflicts(acct, at)
        with connection(self.path) as db:
            other = db.execute(
                """SELECT 1 FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id
                LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id
                WHERE s.account_id=? AND s.slot_key<>? AND s.state NOT IN ('skipped','missed') AND
                ((abs(julianday(s.scheduled_at)-julianday(?))*86400<60 AND NOT
                  (j.trigger_type='auto' AND j.template_id=? AND s.scheduled_at=?)) OR
                 t.status IN ('publishing','reconciling','unknown') OR t.unknown_outcome=1 OR
                 (t.status='retry_wait' AND (t.claim_phase='reconcile' OR coalesce(t.publish_id,'')<>''))) LIMIT 1""",
                (acct, own, at, day_template or -1, at),
            ).fetchone()
        if other:
            hits = [*hits, "新版已有排期或待核对发布任务"]
        return "；".join(hits), hits

    @staticmethod
    def cooling(db, acct, at, days, omit=""):
        if not days:
            return set()
        return {
            r[0]
            for r in db.execute(
                """SELECT json_extract(choice,'$.content_id') FROM ttp_slot
              WHERE account_id=? AND slot_key<>? AND material_id IS NOT NULL AND state NOT IN ('missed','skipped')
              AND abs(julianday(scheduled_at)-julianday(?))<?""",
                (acct, omit, at, days),
            )
            if r[0]
        }

    def sample(self, t, channel, opts, at, key, blocked=(), *, inspect=False, gate=None, own=""):
        acct = str(channel["account_id"])
        row = dict(
            account_id=acct,
            scheduled_at_utc=at,
            status="skipped",
            reason="",
            conflicts=[],
            caption="",
            seed=key,
        )
        reason, hits = self.gate(channel, at, own=own) if gate is None else gate
        if reason and (not hits or not inspect):
            return dict(row, reason=reason, conflicts=hits)
        with connection(self.path) as db:
            opts = self._spec(db, t, acct, opts)
            if opts["mode"] != "serial":
                opts["cooling"] = sorted(
                    set(opts.get("cooling", [])) | self.cooling(db, acct, at, t.config["cooldown_days"])
                )
        choice = self.runtime.preview(t, channel, opts, key, blocked)
        if choice["material_id"] in blocked or (
            opts["mode"] != "serial" and choice["content_id"] in opts.get("cooling", [])
        ):
            raise PublicationError("该素材已被占用或频道仍在同剧冷却期", 409)
        fingerprint = pub_copy.facts_key(
            pub_copy.draft_facts(
                "", channel, choice, t.config["caption_template"], episode=opts.get("episode")
            )
        )
        if opts.get("adopted_draft") and opts["adopted_draft"]["copy_facts_key"] != fingerprint:
            raise PublicationError("剧目、语言、集序或素材内容已改变，请重新审稿", 409)
        opts_caption = (
            opts.get("manual_caption")
            or (opts.get("adopted_draft") or {}).get("caption")
            or t.config["caption_template"]
        )
        sample = opts_caption
        for k, v in {
            "{{drama_name}}": choice["drama_name"],
            "{{content_id}}": choice["content_id"],
            "{{contect_id}}": choice["content_id"],
            "{desc}": choice.get("description") or choice["drama_name"],
            "{code}": "〔正式四位码〕",
            "{url}": "〔正式短链〕",
        }.items():
            sample = sample.replace(k, v)
        return dict(
            row,
            **{k: v for k, v in choice.items() if k not in row},
            status="skipped" if hits else "eligible",
            conflicts=hits,
            reason="；".join(hits) if hits else choice["reason"],
            caption=sample,
            options=opts,
            episode=opts.get("episode"),
            copy_facts_key=fingerprint,
        )

    def preview_request(self, t, actor, scope, ids, opts, payload):
        limit = number(
            payload.get("limit", 8 if payload.get("plan_date") or payload.get("async_preview") else 24),
            1,
            24,
            "每页数量",
        )
        offset = number(payload.get("offset", 0), 0, 100000, "分页")
        async_mode = payload.get("async_preview") is True or bool(payload.get("preview_id"))
        inspect = payload.get("include_conflict_candidates", not async_mode) is True
        key = payload.get("preview_id")
        if not key:
            date = payload.get("plan_date")
            version = t.version
            if date:
                try:
                    date = datetime.strptime(str(date), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    raise PublicationError("日排期预演日期无效") from None
                with connection(self.path) as db:
                    day = db.execute(
                        "SELECT version,slots FROM ttp_day WHERE template_id=? AND day=?", (t.id, date)
                    ).fetchone()
                if day:
                    version = day["version"]
                    pairs = [p for p in load(day["slots"]) if p[0] in ids]
                else:
                    pairs = [(acct, at) for acct in ids for at in day_slots(t, acct, date)]
                pairs.sort(key=lambda p: (p[1], p[0]))
                at = None
            else:
                at = when(payload)
                pairs = [(acct, at) for acct in ids]
            inputs = dict(
                plan_date=date or None,
                scheduled_at=at if payload.get("scheduled_at") else None,
                opts=opts,
                inspect=inspect,
            )
            fields = (
                "account_id",
                "enabled",
                "eligible",
                "drama_language",
                "account_settings",
                "profile",
                "reason",
                "eligibility_reason",
            )
            channels = {acct: {k: scope[acct][k] for k in fields if k in scope[acct]} for acct in ids}
            key = uuid.uuid4().hex
            if date and not async_mode:
                # Preserve the original offset-only day endpoint within a short review interval.
                key = hashlib.sha256(
                    dump(
                        [
                            t.id,
                            t.version,
                            actor["tenant_key"],
                            actor["user_id"],
                            inputs,
                            channels,
                            pairs,
                            int(now().timestamp()) // 300,
                        ]
                    ).encode()
                ).hexdigest()
            self.previews.create(key, t, version, actor, inputs, channels, pairs)
        snap = self.snapshot(t, actor, scope, key)
        if inspect and not load(snap["inputs"]).get("inspect") and snap["state"] == "ready":
            inputs = load(snap["inputs"])
            inputs["inspect"] = True
            with connection(self.previews.path, write=True) as db:
                db.execute(
                    "UPDATE preview SET state='working',inputs=? WHERE id=? AND state='ready'",
                    (dump(inputs), key),
                )
            snap = self.snapshot(t, actor, scope, key)
        done = self.launch_preview(snap) if snap["state"] == "working" else None
        page = self.previews.page(key, offset, limit)
        if async_mode:
            return page
        # Existing synchronous clients expect completed choices. Leave time for the gateway
        # to deliver a real timeout with the resumable snapshot if the warehouse is slow.
        deadline = time.monotonic() + 75
        while page["state"] == "working":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exc = PublicationError(
                    "候选仍在计算，可通过 preview_id " + key + " 继续查询",
                    504,
                    "tt_promotion_preview_timeout",
                )
                exc.preview_id = key
                raise exc
            if done:
                if done.wait(min(remaining, 1)):
                    done = None
            else:
                time.sleep(min(remaining, 0.25))
                done = self.launch_preview(snap)
            page = self.previews.page(key, offset, limit)
        if page["state"] == "failed":
            raise PublicationError(page["error"] or "预演失败", 503)
        return page

    def launch_preview(self, snap):
        slot = (str(self.previews.path), snap["id"])
        done = threading.Event()
        with _PREVIEW_LOCK:
            if slot in _PREVIEW_ACTIVE or not _PREVIEW_WORKERS.acquire(blocking=False):
                return None
            _PREVIEW_ACTIVE.add(slot)

        def compute():
            try:
                self.compute_preview(snap)
            finally:
                with _PREVIEW_LOCK:
                    _PREVIEW_ACTIVE.discard(slot)
                    _PREVIEW_WORKERS.release()
                done.set()

        threading.Thread(target=compute, name="tt-promotion-preview", daemon=True).start()
        return done

    def compute_preview(self, snap):
        key = snap["id"]
        token = uuid.uuid4().hex
        if not self.previews.claim(key, token):
            return
        try:
            inputs = load(snap["inputs"])
            opts = inputs["opts"]
            scope = load(snap["scope"])
            t = self.store.get_template(snap["template"], version=snap["schedule_version"])
            worker = copy(self)
            if hasattr(self.runtime, "preview_session"):
                worker.runtime = self.runtime.preview_session()
            rows = self.previews.rows(key)
            # Record every cheap gate before fetching or validating the first candidate.
            for r in rows:
                if r["checked"]:
                    continue
                detail = None
                try:
                    reason, hits = worker.gate(
                        scope[r["account"]], r["at"], day_template=t.id if inputs.get("plan_date") else None
                    )
                    if reason:
                        detail = dict(reason=reason, conflicts=hits)
                except Exception as e:
                    detail = dict(reason=error_text(e), conflicts=[])
                if detail:
                    detail.update(
                        account_id=r["account"],
                        scheduled_at_utc=r["at"],
                        status="skipped",
                        caption="",
                        seed=selection_seed(t, r["account"], r["at"]),
                    )
                if not self.previews.write_row(key, token, r["pos"], detail):
                    return
                r["result"] = dump(detail) if detail else None
            if opts["mode"] == "serial" and not inputs.get("plan_date"):
                order = {
                    acct: i
                    for i, acct in enumerate(
                        worker._series_order(t, scope, [r["account"] for r in rows], opts)
                    )
                }
                rows.sort(key=lambda r: order[r["account"]])
            seen = set()
            cooling = {}
            bound = None
            for r in rows:
                acct, at = r["account"], r["at"]
                item = load(r["result"])
                seed = selection_seed(t, acct, at)
                inspect = bool(item.get("conflicts") and inputs["inspect"] and not item.get("inspected"))
                if not item or inspect:
                    previous = cooling.get(acct, [])
                    stamp = datetime.fromisoformat(at)
                    banned = [
                        cid
                        for other, cid in previous
                        if abs((stamp - other).total_seconds()) < t.config["cooldown_days"] * 86400
                    ]
                    spec = dict(opts, cooling=banned, _catalog_at=snap["created_at"])
                    try:
                        if bound and opts["mode"] == "serial":
                            raise PublicationError("此次连载分配到频道 " + bound, 409)
                        item = worker.sample(
                            t,
                            scope[acct],
                            spec,
                            at,
                            seed,
                            seen,
                            inspect=inspect,
                            gate=(item.get("reason", ""), item.get("conflicts", [])),
                        )
                        item.pop("options", None)
                    except Exception as e:
                        item = dict(
                            account_id=acct,
                            scheduled_at_utc=at,
                            seed=seed,
                            status="skipped",
                            conflicts=item.get("conflicts", []),
                            reason=item.get("reason") or error_text(e),
                            candidate_error=error_text(e),
                            caption="",
                        )
                    if inspect:
                        item["inspected"] = True
                    if not self.previews.write_row(key, token, r["pos"], item):
                        return
                if item["status"] == "eligible":
                    seen.add(str(item["material_id"]))
                    cooling.setdefault(acct, []).append((datetime.fromisoformat(at), item["content_id"]))
                    if opts["mode"] == "serial":
                        bound = acct
            self.previews.finish(key, token, "")
        except Exception as e:
            self.previews.finish(key, token, error_text(e))

    def _series_order(self, t, scope, ids, opts):
        from .profiles import match_drama

        source = getattr(getattr(getattr(self.runtime, "native", None), "selector", None), "source", None)

        def score(acct):
            return match_drama(
                scope[acct].get("profile") or {},
                opts["content_id"],
                scope[acct]["drama_language"],
                source=source,
            )

        return sorted(ids, key=score, reverse=True)

    def runs(self, actor, scope):
        with connection(self.path) as db:
            rows = db.execute(
                "SELECT id FROM ttp_job WHERE tenant=? ORDER BY id DESC LIMIT 200", (actor["tenant_key"],)
            ).fetchall()
        items = []
        for row in rows:
            try:
                item = self.run_detail(row["id"], actor, scope)
            except PublicationError:
                continue
            item["count"] = len(item.pop("items"))
            items.append(item)
        return {"items": items}

    def run_detail(self, job, actor, scope):
        from .operations import RESULT

        with connection(self.path) as db:
            j = db.execute(
                "SELECT * FROM ttp_job WHERE id=? AND tenant=?", (job, actor["tenant_key"])
            ).fetchone()
            if not j:
                raise PublicationError("运行记录不存在", 404)
            rows = db.execute(
                f"""SELECT s.*,t.id task_id,t.status native_status,t.publish_id,t.publish_url,t.unknown_outcome,p.video_ids,{RESULT} result_bucket,
                       t.error_code,t.error_message,t.caption,t.content_id,t.material_id native_material,
                       c.source copy_source,c.reason copy_reason
                FROM ttp_slot s LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id
                LEFT JOIN ttp_copy c ON c.slot_key=s.slot_key LEFT JOIN ttp_public_receipt p ON p.task_id=t.id WHERE s.job_id=? ORDER BY s.rowid""",
                (job,),
            ).fetchall()
        if not all(r["account_id"] in scope for r in rows):
            raise PublicationError("运行记录不存在", 404)
        items = []
        for r in rows:
            ch = load(r["choice"])
            opts = load(r["opts"])
            expected = opts.get("expected") or {}
            state = r["native_status"] or r["state"]
            actual_mid = r["native_material"] or r["material_id"]
            actual_cid = r["content_id"] or ch.get("content_id", "")
            comparison = "not_previewed"
            detail = ""
            if expected:
                if state in ("skipped", "missed", "no_candidate"):
                    comparison = "skipped"
                    detail = r["reason"] or r["error_message"] or "执行时未通过资格或占用检查"
                elif not actual_mid:
                    comparison = "pending"
                    detail = "等待执行时选材"
                elif (
                    expected.get("status") == "eligible"
                    and actual_mid == expected.get("material_id")
                    and actual_cid == expected.get("content_id")
                ):
                    comparison = "matched"
                    detail = "执行选材与预演一致"
                else:
                    comparison = "changed"
                    detail = "执行时根据最新候选、预约、冷却或画像重新选材"
            adopted = opts.get("adopted_draft")
            items.append(
                dict(
                    id=r["task_id"],
                    account_id=r["account_id"],
                    status=r["native_status"] or r["state"],
                    unknown_outcome=bool(r["unknown_outcome"]),
                    result_bucket=r["result_bucket"],
                    scheduled_at_utc=r["scheduled_at"],
                    reason=r["error_message"] or r["reason"] or ch.get("reason", ""),
                    error_code=r["error_code"],
                    content_id=r["content_id"] or ch.get("content_id", ""),
                    material_id=r["native_material"] or r["material_id"],
                    drama_name=ch.get("drama_name", ""),
                    decision=ch.get("decision"),
                    preview_comparison=dict(
                        status=comparison,
                        reason=detail,
                        preview_id=opts.get("preview_id"),
                        expected_content_id=expected.get("content_id"),
                        expected_material_id=expected.get("material_id"),
                        expected=expected,
                        actual_content_id=actual_cid,
                        actual_material_id=actual_mid,
                    ),
                    copy_provenance={k: v for k, v in (adopted or {}).items() if k != "caption"}
                    | ({"overridden_by_manual": bool(opts.get("manual_caption"))} if adopted else {}),
                    profile_version=ch.get("profile_version"),
                    caption=r["caption"] or ch.get("caption", ""),
                    copy_source=r["copy_source"],
                    copy_reason=r["copy_reason"],
                    code=ch.get("code"),
                    publish_id=r["publish_id"],
                    publish_url=r["publish_url"],
                    public_video_ids=load(r["video_ids"]) if r["video_ids"] else [],
                )
            )
        return dict(
            id=j["id"],
            template_id=j["template_id"],
            template_name=j["template_name"],
            version=j["version"],
            status=summarize(items),
            trigger_type=j["trigger_type"],
            created_at=j["created_at"],
            scheduled_at_utc=min((r["scheduled_at"] for r in rows), default=None),
            items=items,
        )

    def schedule(self):
        current = now()
        today = current.astimezone(BEIJING).date().isoformat()
        for t in self.store.list_templates(enabled=True):
            creator = t.config.get("creator") or {}
            if not creator.get("tenant_key"):
                continue
            with connection(self.path, write=True) as db:
                record = db.execute(
                    "SELECT * FROM ttp_day WHERE template_id=? AND day=?", (t.id, today)
                ).fetchone()
                if not record:
                    slots = [
                        (acct, at)
                        for acct in t.config["account_ids"]
                        for at in day_slots(t, acct, today)
                        if at >= iso(datetime.fromisoformat(t.enabled_at_utc.replace("Z", "+00:00")))
                    ]
                    db.execute("INSERT INTO ttp_day VALUES(?,?,?,?)", (t.id, today, t.version, dump(slots)))
                    version, pairs = t.version, slots
                else:
                    version, pairs = record["version"], load(record["slots"])
            frozen = self.store.get_template(t.id, version=version)
            for acct, at in pairs:
                key = f"auto:{t.id}:{acct}:{at}"
                self.plan(frozen, creator, [(acct, at)], {"mode": "template"}, key, key, "auto")

    def work(self, channels_for, *, limit=32):
        self.schedule()
        current = now()
        with connection(self.path) as db:
            slots = db.execute(
                """SELECT s.*,j.template_id,j.version,j.actor,j.trigger_type FROM ttp_slot s
                JOIN ttp_job j ON j.id=s.job_id WHERE s.state IN ('planned','reserved') AND s.next_at<=?
                AND s.scheduled_at<=? ORDER BY s.scheduled_at,s.job_id,s.rowid LIMIT ?""",
                (iso(current), iso(current + timedelta(minutes=45)), limit),
            ).fetchall()
        scopes = {}
        for row in slots:
            try:
                creator = load(row["actor"])
                if row["actor"] not in scopes:
                    scopes[row["actor"]] = self.channel_map(channels_for(creator))
                ch = scopes[row["actor"]].get(row["account_id"])
                if not ch or not ch.get("enabled") or not ch.get("eligible"):
                    raise PublicationError("频道不在可发布范围或未参与", 409)
                self._advance(row, ch, current)
            except Exception as e:
                retry = is_transient(e) and current < datetime.fromisoformat(row["scheduled_at"]) + timedelta(
                    minutes=20
                )
                with connection(self.path, write=True) as db:
                    db.execute(
                        """UPDATE ttp_slot SET state=?,reason=?,next_at=?,changed_at=?
                            WHERE slot_key=? AND state=?""",
                        (
                            row["state"] if retry else "skipped",
                            error_text(e),
                            iso(current + timedelta(seconds=45)),
                            iso(current),
                            row["slot_key"],
                            row["state"],
                        ),
                    )
        return self.runtime.step()

    def _advance(self, s, ch, current):
        t = self.store.get_template(s["template_id"], version=s["version"])
        if s["state"] == "planned":
            if s["trigger_type"] == "auto" and current > datetime.fromisoformat(
                s["scheduled_at"]
            ) + timedelta(minutes=20):
                raise PublicationError("计划时段已过", 409)
            item = self.sample(
                t,
                ch,
                load(s["opts"]),
                s["scheduled_at"],
                selection_seed(t, ch["account_id"], s["scheduled_at"]),
                own=s["slot_key"],
            )
            if item["status"] != "eligible":
                raise PublicationError(item["reason"], 409)
            opts = item.pop("options")
            settings, setting_version = self.runtime.settings(ch)
            acct = dict(
                settings=settings,
                version=setting_version,
                drama_language=ch["drama_language"],
                username=ch.get("username") or ch.get("account_username") or "",
                display_name=ch.get("nickname") or ch.get("display_name") or ch.get("name") or "",
            )
            with connection(self.path, write=True) as db:
                if (
                    db.execute("SELECT state FROM ttp_slot WHERE slot_key=?", (s["slot_key"],)).fetchone()[0]
                    != "planned"
                ):
                    return
                if opts.get("mode") == "serial":
                    db.execute(
                        "INSERT OR IGNORE INTO ttp_series VALUES(?,?,?,?,?)",
                        (
                            t.config["creator"]["tenant_key"],
                            opts["content_id"],
                            t.id,
                            s["account_id"],
                            dump(opts["episodes"]),
                        ),
                    )
                    self._spec(db, t, s["account_id"], opts)
                elif item["content_id"] in self.cooling(
                    db, s["account_id"], s["scheduled_at"], t.config["cooldown_days"], s["slot_key"]
                ):
                    raise PublicationError("该频道已预约相同剧目，仍在冷却期", 409)
                try:
                    count = db.execute(
                        """UPDATE ttp_slot SET state='reserved',material_id=?,choice=?,account_json=?,opts=?,
                                changed_at=? WHERE slot_key=? AND state='planned'""",
                        (
                            item["material_id"],
                            dump(item),
                            dump(acct),
                            dump(opts),
                            iso(current),
                            s["slot_key"],
                        ),
                    ).rowcount
                except sqlite3.IntegrityError:
                    raise PublicationError("该素材已被其他任务预约", 409) from None
                if count:
                    pub_copy.submit(
                        db,
                        s["slot_key"],
                        ch,
                        item,
                        t.config,
                        opts.get("manual_caption"),
                        episode=opts.get("episode"),
                        adopted=opts.get("adopted_draft"),
                    )
            return
        if self.runtime.conflicts(s["account_id"], s["scheduled_at"]):
            raise PublicationError("旧排期或原发布任务占用该频道", 409)
        with connection(self.path, write=True) as db:
            c = db.execute("SELECT * FROM ttp_copy WHERE slot_key=?", (s["slot_key"],)).fetchone()
            if not c:
                raise PublicationError("任务文案尚未就绪", 503)
            if c["state"] != "final":
                deadline = max(
                    datetime.fromisoformat(s["scheduled_at"]),
                    datetime.fromisoformat(s["changed_at"]) + timedelta(seconds=90),
                )
                if current < deadline:
                    return
                pub_copy.fallback(db, s["slot_key"], "发布截止时间到达")
        actor = load(s["actor"])
        frozen = load(s["account_json"])
        run_key = "promotion:" + s["slot_key"]
        run = self.store.get_run_by_key(run_key)
        if not run:
            local = datetime.fromisoformat(s["scheduled_at"]).astimezone(BEIJING)
            # The promotion planner owns its frozen day/slot and eligibility.
            # Native runs are isolated commands. Reports use the frozen trigger above.
            run = self.store.create_run(
                run_key=run_key,
                template_id=t.id,
                template_version=t.version,
                trigger_type="manual",
                scheduled_at_utc=s["scheduled_at"],
                shanghai_date=local.date().isoformat(),
                publish_time=local.strftime("%H:%M"),
                actor=audit(actor),
            )
        with connection(self.path, write=True) as db:
            db.execute(
                "UPDATE ttp_slot SET native_run_id=? WHERE slot_key=? AND state='reserved'",
                (run.id, s["slot_key"]),
            )
        self.store.create_task(
            run_id=run.id,
            account_id=s["account_id"],
            account_username=frozen["username"],
            account_display_name=frozen["display_name"],
            drama_language=frozen["drama_language"],
            account_settings=frozen["settings"],
            account_setting_version=frozen["version"],
        )
        with connection(self.path, write=True) as db:
            db.execute(
                "UPDATE ttp_slot SET state='enqueued',changed_at=? WHERE slot_key=? AND state='reserved'",
                (iso(current), s["slot_key"]),
            )


def is_transient(e):
    return int(getattr(e, "status", 503) or 503) >= 500 or getattr(e, "code", "") in (
        "tt_auto_metric_window_not_ready",
    )


def error_text(e):
    if isinstance(e, PublicationError):
        return str(e)[:300]
    return str(getattr(e, "code", "") or type(e).__name__)[:160]


def initialize(path):
    from features.tt_auto_posts.core import TTPostAutoStore
    from .pub_native import old_databases
    from pathlib import Path

    if Path(path).resolve() in {Path(p).resolve() for p in old_databases()}:
        raise PublicationError("新版数据库不能指向旧发布器", 500)
    TTPostAutoStore(path)
    install(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "tick"])
    args = parser.parse_args()
    path = os.environ.get("TT_PROMOTION_DB", DEFAULT_DB)
    if args.command == "init":
        initialize(path)
        return
    from .channels import worker_channels

    service = Publisher(path)
    report = service.work(worker_channels)
    # The native wire-format also has operational details unnecessary for journal output.
    print(dump({"claimed": bool(report.get("claimed"))}))


if __name__ == "__main__":
    main()

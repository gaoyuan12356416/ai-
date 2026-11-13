"""Additive manual publishing store. No network requests occur in transactions."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .guards import automatic_block, manual_block, published_history, prepare_available, install_backstops

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))
TERMINAL = {"published", "failed", "cancelled"}


class DomainError(RuntimeError):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.status, self.details = code, status, details
        super().__init__(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise DomainError("invalid_schedule", "预约时间必须包含时区") from None


def actor_checked(actor):
    required = {"tenant_key", "user_id", "user_name", "is_admin", "business_owner_id", "sub_user_id"}
    if not isinstance(actor, dict) or set(actor) != required or not actor.get("tenant_key") or not actor.get("user_id"):
        raise DomainError("invalid_actor", "登录身份无效", 403)
    if type(actor["is_admin"]) is not bool or any(not isinstance(actor[k], str) or len(actor[k]) > 200 for k in required - {"is_admin"}):
        raise DomainError("invalid_actor", "登录身份无效", 403)
    return dict(actor)


def id_text(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,30}", str(value)):
        raise DomainError("invalid_request", "Page或素材ID无效")
    return str(value)


def normalize_batch(raw, *, manual=False):
    v2 = isinstance(raw, dict) and raw.get("schema_version") == 2
    required = {"operation_id", "material_ids", "page_ids", "pairs", "processing_mode"}
    required |= {"schema_version", "material_schedules"} if v2 else {"schedule_mode", "scheduled_at_utc", "interval_minutes"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise DomainError("invalid_request", "批次字段不完整或含未知字段")
    op = raw["operation_id"]
    if not isinstance(op, str) or not re.fullmatch(r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", op):
        raise DomainError("invalid_request", "操作标识无效")
    result = dict(raw)
    for field in ("material_ids", "page_ids"):
        values = raw[field]
        if not isinstance(values, list) or (not values or (not manual and len(values) > 200)):
            raise DomainError("invalid_request", "请选择1至200个素材/Page")
        normalized = [id_text(v) for v in values]
        if len(set(normalized)) != len(normalized):
            raise DomainError("invalid_request", "素材/Page不得重复")
        result[field] = normalized
    if raw["processing_mode"] not in {"original", "random_overlay"}:
        raise DomainError("invalid_request", "加工或发布时间选项无效")
    if v2:
        schedules = raw["material_schedules"]
        if type(raw["schema_version"]) is not int or not isinstance(schedules, dict) or set(schedules) != set(result["material_ids"]):
            raise DomainError("invalid_schedule", "逐素材预约必须完整覆盖所选素材")
        result["material_schedules"] = {}
        for mid, value in schedules.items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise DomainError("invalid_schedule", "预约为空请使用null")
            result["material_schedules"][mid] = None if value is None else stamp(parse_time(value))
    else:
        if raw["schedule_mode"] not in {"now", "scheduled"}:
            raise DomainError("invalid_request", "发布时间选项无效")
        interval = raw["interval_minutes"]
        if type(interval) is not int or not 0 <= interval <= 1440:
            raise DomainError("invalid_request", "同Page间隔应为0至1440整数分钟")
        result["scheduled_at_utc"] = stamp(parse_time(raw["scheduled_at_utc"])) if raw["schedule_mode"] == "scheduled" else ""
    pairs = raw["pairs"]
    if not isinstance(pairs, list) or (not pairs or (not manual and len(pairs) > 200)):
        raise DomainError("invalid_request", "每批请选择1至200个素材与Page配对")
    from .templates import validate_template
    seen, clean = set(), []
    for p in pairs:
        if not isinstance(p, dict) or (set(p) - ({"title"} if manual else set())) != {"material_id", "page_id", "config_instance_id", "config_version", "caption_template"}:
            raise DomainError("invalid_request", "配对字段无效")
        mid, pid = id_text(p["material_id"]), id_text(p["page_id"])
        key = (mid, pid)
        if mid not in result["material_ids"] or pid not in result["page_ids"] or key in seen:
            raise DomainError("invalid_request", "配对重复或超出本批选择范围")
        unconfigured = manual and p["config_instance_id"] == "" and type(p["config_version"]) is int and p["config_version"] == 0
        if not unconfigured and (not re.fullmatch(r"[a-f0-9]{32}", str(p["config_instance_id"])) or type(p["config_version"]) is not int or p["config_version"] < 1):
            raise DomainError("invalid_request", "Page配置版本无效")
        if "title" in p:
            p = {**p, "title": validate_template(p["title"], manual=True)}
        seen.add(key)
        clean.append({**p, "material_id": mid, "page_id": pid, "caption_template": validate_template(p["caption_template"], manual=manual)})
    if {p[0] for p in seen} != set(result["material_ids"]):
        raise DomainError("material_unassigned", "存在未分配Page的素材，请分配或移除")
    if {p[1] for p in seen} != set(result["page_ids"]):
        raise DomainError("page_unassigned", "存在未分配素材的Page，请分配或移除")
    clean.sort(key=lambda p: (result["material_ids"].index(p["material_id"]), result["page_ids"].index(p["page_id"])))
    result["pairs"] = clean
    return result


def plan_pairs(request, now):
    """One schedule calculation for confirmation, quota and immutable freezing."""
    v2 = request.get("schema_version") == 2
    if request.get("schema_version") == 3:
        times = {p["row_number"]: now if p["scheduled_at_utc"] is None else parse_time(p["scheduled_at_utc"])
                 for p in request["pairs"]}
        if any(p["scheduled_at_utc"] is not None and times[p["row_number"]] <= now for p in request["pairs"]):
            raise DomainError("invalid_schedule", "预约时间必须晚于当前时间", 409)
        ordered = sorted(enumerate(request["pairs"], 1), key=lambda item: (times[item[1]["row_number"]], item[1]["row_number"]))
    elif v2:
        times = {mid: now if value is None else parse_time(value)
                 for mid, value in request["material_schedules"].items()}
        if any(value is not None and times[mid] <= now for mid, value in request["material_schedules"].items()):
            raise DomainError("invalid_schedule", "预约时间必须晚于当前时间", 409)
        ordered = sorted(enumerate(request["pairs"], 1), key=lambda p: (times[p[1]["material_id"]], p[0]))
    else:
        start = now if request["schedule_mode"] == "now" else parse_time(request["scheduled_at_utc"])
        if request["schedule_mode"] == "scheduled" and start <= now:
            raise DomainError("invalid_schedule", "预约时间已经过去", 409)
        ordered = list(enumerate(request["pairs"], 1))
    counts, result = {}, []
    for index, pair in ordered:
        pid = pair["page_id"]
        counts[pid] = counts.get(pid, 0) + 1
        when = (times[pair["row_number"]] if request.get("schema_version") == 3 else
                times[pair["material_id"]] if v2 else start + timedelta(minutes=(counts[pid] - 1) * request["interval_minutes"]))
        result.append((index, pair, counts[pid], when, when.astimezone(BJ).date().isoformat()))
    return result


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path, *, now_fn=utcnow, daily_limit=1000):
        self.path, self.now_fn, self.daily_limit = str(path), now_fn, int(daily_limit)
        if not 1 <= self.daily_limit <= 10000:
            raise ValueError("invalid manual daily limit")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS fb_video_page(
                  tenant_key TEXT NOT NULL,page_id TEXT NOT NULL,config_instance_id TEXT NOT NULL,
                  version INTEGER NOT NULL,language TEXT NOT NULL,caption_template TEXT NOT NULL,
                  deleted INTEGER NOT NULL DEFAULT 0,created_by TEXT NOT NULL,updated_by TEXT NOT NULL,
                  created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL,PRIMARY KEY(tenant_key,page_id));
                CREATE TABLE IF NOT EXISTS fb_video_page_version(
                  config_instance_id TEXT NOT NULL,version INTEGER NOT NULL,snapshot_json TEXT NOT NULL,
                  created_at_utc TEXT NOT NULL,PRIMARY KEY(config_instance_id,version));
                CREATE TABLE IF NOT EXISTS fb_video_draft(
                  tenant_key TEXT NOT NULL,owner_user_id TEXT NOT NULL,draft_json TEXT NOT NULL,
                  updated_at_utc TEXT NOT NULL,PRIMARY KEY(tenant_key,owner_user_id));
                CREATE TABLE IF NOT EXISTS fb_video_batch(
                  batch_id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,owner_user_id TEXT NOT NULL,
                  owner_name TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                  request_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,
                  UNIQUE(tenant_key,owner_user_id,operation_id));
                CREATE TABLE IF NOT EXISTS fb_video_task(
                  task_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,tenant_key TEXT NOT NULL,owner_user_id TEXT NOT NULL,
                  page_id TEXT NOT NULL,material_id TEXT NOT NULL,content_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                  page_sequence INTEGER NOT NULL,planned_at_utc TEXT NOT NULL,planned_bj_date TEXT NOT NULL,
                  processing_mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',snapshot_json TEXT NOT NULL,
                  ledger_id INTEGER NOT NULL DEFAULT 0,short_url TEXT NOT NULL DEFAULT '',long_url TEXT NOT NULL DEFAULT '',
                  caption TEXT NOT NULL DEFAULT '',gpu_job_id TEXT NOT NULL DEFAULT '',
                  prepare_state TEXT NOT NULL,prepared_json TEXT NOT NULL DEFAULT '{}',
                  prepare_lease TEXT NOT NULL DEFAULT '',prepare_until TEXT NOT NULL DEFAULT '',
                  prepare_retry_at TEXT NOT NULL DEFAULT '',prepare_attempts INTEGER NOT NULL DEFAULT 0,
                  link_state TEXT NOT NULL DEFAULT 'pending',link_sha256 TEXT NOT NULL DEFAULT '',
                  link_lease TEXT NOT NULL DEFAULT '',link_until TEXT NOT NULL DEFAULT '',link_retry_at TEXT NOT NULL DEFAULT '',
                  execution_lease TEXT NOT NULL DEFAULT '',execution_until TEXT NOT NULL DEFAULT '',
                  reconcile_at TEXT NOT NULL DEFAULT '',graph_post_id TEXT NOT NULL DEFAULT '',
                  unknown_outcome INTEGER NOT NULL DEFAULT 0,error_code TEXT NOT NULL DEFAULT '',error_message TEXT NOT NULL DEFAULT '',
                  created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL,
                  UNIQUE(batch_id,page_id,material_id));
                CREATE TABLE IF NOT EXISTS fb_video_link(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL UNIQUE,long_url TEXT NOT NULL DEFAULT '',
                  short_url TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fb_video_attempt(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,lease_token TEXT NOT NULL,
                  credential_id TEXT NOT NULL,fb_user_id TEXT NOT NULL,result_kind TEXT NOT NULL,
                  graph_post_id TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',trace_id TEXT NOT NULL DEFAULT '',
                  created_at_utc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_fb_video_claim ON fb_video_task(status,planned_at_utc,sequence);
                CREATE INDEX IF NOT EXISTS idx_fb_video_pair ON fb_video_task(page_id,material_id,status);
                CREATE INDEX IF NOT EXISTS idx_fb_video_quota ON fb_video_task(planned_bj_date,status);
                CREATE INDEX IF NOT EXISTS idx_fb_video_batch_owner ON fb_video_batch(tenant_key,owner_user_id,created_at_utc);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_fb_video_execution ON fb_video_task(page_id)
                  WHERE status IN ('running','submitted','unknown');
            """)
            # Additive migrations leave all prior snapshots and task facts intact.
            for table, column, definition in (
                ("fb_video_page", "created_by_name", "TEXT NOT NULL DEFAULT ''"),
                ("fb_video_page", "title", "TEXT NOT NULL DEFAULT ''"),
                ("fb_video_task", "source_kind", "TEXT NOT NULL DEFAULT 'manual'"),
                ("fb_video_task", "pool_instance_id", "TEXT NOT NULL DEFAULT ''"),
                ("fb_video_task", "pool_sequence", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}:
                    conn.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
            from .pool import install_schema
            install_schema(conn)
            from .group_pool import install_schema as install_group_schema
            install_group_schema(conn)
            install_backstops(conn)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=Connection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _scope(actor, alias=""):
        p = alias + "." if alias else ""
        sql, args = p + "tenant_key=?", [actor["tenant_key"]]
        if not actor["is_admin"]:
            sql += " AND " + p + "owner_user_id=?"
            args.append(actor["user_id"])
        return sql, args

    def configs(self, actor):
        with self.connect() as conn:
            return {r["page_id"]: dict(r) for r in conn.execute("SELECT * FROM fb_video_page WHERE tenant_key=? AND deleted=0 ORDER BY page_id", (actor["tenant_key"],))}

    def save_configs(self, actor, page_ids, language, caption, *, expected=None, title=None):
        from .templates import validate_template
        from features.fb_auto_posts.languages import page_language
        caption, language = validate_template(caption, manual=True), page_language(language)
        if title is not None:
            title = validate_template(title, manual=True)
        ids = [id_text(v) for v in page_ids]
        if not ids or len(ids) > 200 or len(set(ids)) != len(ids):
            raise DomainError("invalid_request", "请选择不重复的Page")
        now = stamp(self.now_fn())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = []
            for pid in ids:
                old = conn.execute("SELECT * FROM fb_video_page WHERE tenant_key=? AND page_id=? AND deleted=0", (actor["tenant_key"], pid)).fetchone()
                if expected is None and old:
                    raise DomainError("page_already_configured", "Page已创建，请在列表编辑", 409)
                if expected is not None and (old is None or old["config_instance_id"] != expected[0] or old["version"] != expected[1]):
                    raise DomainError("config_version_conflict", "Page配置已改变，请刷新", 409)
                instance, version = (old["config_instance_id"], old["version"] + 1) if old else (uuid.uuid4().hex, 1)
                created = old["created_at_utc"] if old else now
                conn.execute("""INSERT INTO fb_video_page(tenant_key,page_id,config_instance_id,version,language,
                    caption_template,deleted,created_by,updated_by,created_at_utc,updated_at_utc,created_by_name)
                    VALUES(?,?,?,?,?,?,0,?,?,?,?,?)
                    ON CONFLICT(tenant_key,page_id) DO UPDATE SET config_instance_id=excluded.config_instance_id,
                    version=excluded.version,language=excluded.language,caption_template=excluded.caption_template,deleted=0,
                    created_by=excluded.created_by,created_by_name=excluded.created_by_name,updated_by=excluded.updated_by,
                    created_at_utc=excluded.created_at_utc,updated_at_utc=excluded.updated_at_utc""",
                    (actor["tenant_key"], pid, instance, version, language, caption, old["created_by"] if old else actor["user_id"],
                     actor["user_id"], created, now, old["created_by_name"] if old else actor["user_name"]))
                conn.execute("UPDATE fb_video_page SET title=? WHERE tenant_key=? AND page_id=?",
                             (title if title is not None else (old["title"] if old else ""), actor["tenant_key"], pid))
                if old and old["language"] != language:
                    conn.execute("""UPDATE fb_video_pool SET status='paused',pause_reason='page_language_changed',
                        version=version+1,updated_at_utc=? WHERE config_instance_id=? AND status<>'archived'""", (now, instance))
                row = dict(conn.execute("SELECT * FROM fb_video_page WHERE tenant_key=? AND page_id=?", (actor["tenant_key"], pid)).fetchone())
                conn.execute("INSERT INTO fb_video_page_version VALUES(?,?,?,?)", (instance, version, canonical(row), now))
                result.append(row)
            conn.commit()
            return result

    def delete_config(self, actor, pid, instance, version):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE fb_video_page SET deleted=1,updated_at_utc=?,updated_by=?
                WHERE tenant_key=? AND page_id=? AND deleted=0 AND config_instance_id=? AND version=?""",
                (stamp(self.now_fn()), actor["user_id"], actor["tenant_key"], pid, instance, version)).rowcount
            if not changed:
                raise DomainError("config_version_conflict", "Page配置已改变，请刷新", 409)
            conn.execute("""UPDATE fb_video_pool SET status='archived',pause_reason='page_deleted',
                version=version+1,updated_at_utc=? WHERE config_instance_id=?""", (stamp(self.now_fn()), instance))
            conn.commit()
        return {"ok": True, "page_id": pid}

    def draft(self, actor, value=...):
        with self.connect() as conn:
            if value is not ...:
                if value is not None and not isinstance(value, dict):
                    raise DomainError("invalid_request", "草稿格式无效")
                encoded = canonical(value)
                if len(encoded.encode()) > 64 * 1024 * 1024:
                    raise DomainError("request_too_large", "草稿过大", 413)
                conn.execute("INSERT INTO fb_video_draft VALUES(?,?,?,?) ON CONFLICT(tenant_key,owner_user_id) DO UPDATE SET draft_json=excluded.draft_json,updated_at_utc=excluded.updated_at_utc",
                             (actor["tenant_key"], actor["user_id"], encoded, stamp(self.now_fn())))
            row = conn.execute("SELECT draft_json FROM fb_video_draft WHERE tenant_key=? AND owner_user_id=?", (actor["tenant_key"], actor["user_id"])).fetchone()
            return {"ok": True, "draft": json.loads(row[0]) if row else None}

    @staticmethod
    def unconfigured(pid):
        return {"page_id": pid, "config_instance_id": "", "version": 0, "language": "", "caption_template": "", "title": ""}

    def check_plan(self, conn, actor, request, resolved, *, manual=False):
        repeats, configs = {}, {}
        for pair in request["pairs"]:
            pid, mid = pair["page_id"], pair["material_id"]
            row = conn.execute("SELECT * FROM fb_video_page WHERE tenant_key=? AND page_id=? AND deleted=0", (actor["tenant_key"], pid)).fetchone()
            config = dict(row) if row else (self.unconfigured(pid) if manual else None)
            if not config or config["version"] != pair["config_version"] or config["config_instance_id"] != pair["config_instance_id"]:
                raise DomainError("config_version_conflict", "Page配置已改变，请重新预览", 409)
            if not manual and config["language"] != resolved["materials"][mid]["language"]:
                raise DomainError("language_mismatch", "素材与Page配置剧语言不一致", 409)
            for reason in (automatic_block(conn, pid, mid), manual_block(conn, pid, mid)):
                if reason:
                    raise DomainError(reason, "Page有未结束或结果不明的发布，不能提交", 409)
            configs[pid] = config
            history = published_history(conn, pid, mid)
            if history:
                repeats[pid + ":" + mid] = history
        return configs, repeats

    def existing(self, actor, operation_id, request_hash=None):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM fb_video_batch WHERE tenant_key=? AND owner_user_id=? AND operation_id=?",
                               (actor["tenant_key"], actor["user_id"], operation_id)).fetchone()
            if row is None:
                return None
            if request_hash is not None and row["request_hash"] != request_hash:
                raise DomainError("operation_conflict", "同一操作标识不能用于不同批次内容", 409)
            return {"ok": True, "batch_id": row["batch_id"], "operation_id": operation_id, "idempotent": True, "batch": self._batch_dto(conn, row)}

    def submit(self, actor, request, resolved, signed_facts, allow_repeat):
        reqhash = digest(request)
        plans = plan_pairs(request, self.now_fn())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM fb_video_batch WHERE tenant_key=? AND owner_user_id=? AND operation_id=?",
                               (actor["tenant_key"], actor["user_id"], request["operation_id"])).fetchone()
            if old:
                if old["request_hash"] != reqhash:
                    raise DomainError("operation_conflict", "同一操作标识不能用于不同批次内容", 409)
                conn.commit()
                return {"ok": True, "batch_id": old["batch_id"], "operation_id": old["operation_id"], "idempotent": True, "batch": self._batch_dto(conn, old)}
            configs, repeats = self.check_plan(conn, actor, request, resolved, manual=True)
            facts = {"actor": actor, "request": request, "resolved": resolved, "configs": configs, "repeats": repeats}
            if digest(facts) != signed_facts:
                raise DomainError("preview_changed", "配置、素材或历史发布记录已变化，请重新预览", 409)
            if repeats and allow_repeat is not True:
                raise DomainError("repeat_confirmation_required", "请明确确认再次发布已成功的素材", 409)
            result = self.freeze(conn, actor, request, resolved, configs, repeats, plans)
            conn.commit()
            return result

    def freeze(self, conn, actor, request, resolved, configs, repeats, plans, *, pool=None, group_pool=None, request_hash=None):
        """Caller owns BEGIN IMMEDIATE and all admission checks; never commits."""
        from .links import short_url, build_attribution_url
        from .templates import render_template, manual_text
        if not conn.in_transaction:
            raise RuntimeError("Task freezing requires the caller transaction")
        now = stamp(self.now_fn())
        if pool and group_pool:
            raise ValueError("Task cannot belong to two pool policies")
        if pool or group_pool:
            self.check_quota(conn, plans)
        bid = uuid.uuid4().hex
        conn.execute("INSERT INTO fb_video_batch VALUES(?,?,?,?,?,?,?,?)",
                     (bid, actor["tenant_key"], actor["user_id"], actor["user_name"], request["operation_id"], request_hash or digest(request), canonical(request), now))
        for index, pair, page_seq, when, day in plans:
            tid, pid, mid = uuid.uuid4().hex, pair["page_id"], pair["material_id"]
            material, page, config = resolved["materials"][mid], resolved["pages"][pid], configs[pid]
            lid = conn.execute("INSERT INTO fb_video_link(task_id,created_at_utc) VALUES(?,?)", (tid, now)).lastrowid
            short = short_url(lid)
            context = {"page_id": pid, "page_name": page["page_name"], "material_id": mid,
                       "material_name": material["material_name"], "content_id": material["content_id"],
                       "language": material["language"], "drama_name": material["drama_name"],
                       "tag": material.get("material_tag") or "none", "sub_user_id": actor["sub_user_id"], "task_id": tid}
            if not pool and not group_pool:
                context["drama_name"] = str(context["drama_name"] or "none").replace("*", " ")[:512]
            long = build_attribution_url(context, lid, now)
            caption_template, title = manual_text(pair, config) if not pool and not group_pool else (pair["caption_template"], "")
            caption = render_template(caption_template, material, short, manual=not pool and not group_pool)
            snapshot = {"page": page, "material": material, "config_instance_id": config["config_instance_id"],
                        "template_version": config["version"], "caption_template": caption_template,
                        "language": config["language"], "actor": actor, "attribution": context,
                        "repeat_history": repeats.get(pid + ":" + mid, [])}
            if not pool and not group_pool:
                snapshot.update(title=title, manual_policy="operator-v2")
            if pool:
                snapshot["pool"] = pool
            if group_pool:
                snapshot["group_pool"] = group_pool
                snapshot["title"] = ""
            mode = request["processing_mode"]
            if request.get("schema_version") == 3:
                mode = pair["processing_mode"]
                snapshot.update(row_number=pair["row_number"], title=title,
                                credential_mode=pair["credential_mode"],
                                credential_owner_id=actor["sub_user_id"] if pair["credential_mode"] == "operator" else "")
            job = "fb-page-" + hashlib.sha256(("fb-manual:v1|" + actor["tenant_key"] + "|" + tid + "|" + pid + "|" + mid).encode()).hexdigest()[:48]
            prep_state = "ready" if mode == "original" else "planned"
            prep = {"media_url": material["media_url"], "profile": "original"} if prep_state == "ready" else {}
            conn.execute("""INSERT INTO fb_video_task(task_id,batch_id,tenant_key,owner_user_id,page_id,material_id,
                content_id,sequence,page_sequence,planned_at_utc,planned_bj_date,processing_mode,snapshot_json,
                ledger_id,short_url,long_url,caption,gpu_job_id,prepare_state,prepared_json,created_at_utc,updated_at_utc,
                source_kind,pool_instance_id,pool_sequence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, bid, actor["tenant_key"], actor["user_id"], pid, mid, material["content_id"], index, page_seq,
                 stamp(when), day, mode, canonical(snapshot), lid, short, long, caption,
                 job, prep_state, canonical(prep), now, now, "group_pool" if group_pool else "pool" if pool else "manual",
                 pool["config_instance_id"] if pool else "", pool["slot_id"] if pool else 0))
            conn.execute("UPDATE fb_video_link SET long_url=?,short_url=? WHERE id=?", (long, short, lid))
        batch = conn.execute("SELECT * FROM fb_video_batch WHERE batch_id=?", (bid,)).fetchone()
        return {"ok": True, "batch_id": bid, "operation_id": request["operation_id"], "idempotent": False, "batch": self._batch_dto(conn, batch)}

    def check_quota(self, conn, plans):
        days = {}
        for _, _, _, _, day in plans:
            days[day] = days.get(day, 0) + 1
        for day, count in days.items():
            used = conn.execute("SELECT COUNT(*) FROM fb_video_task WHERE planned_bj_date=? AND status<>'cancelled'", (day,)).fetchone()[0]
            if used + count > self.daily_limit:
                raise DomainError("daily_capacity_exceeded", "发布超过预约日期的每日配额", 409)

    @staticmethod
    def task_dto(row):
        snap = json.loads(row["snapshot_json"])
        allowed = ("task_id", "batch_id", "page_id", "material_id", "content_id", "page_sequence", "planned_at_utc",
                   "processing_mode", "status", "ledger_id", "short_url", "long_url", "caption", "prepare_state",
                   "link_state", "graph_post_id", "unknown_outcome", "error_code", "error_message", "created_at_utc", "updated_at_utc",
                   "source_kind", "pool_instance_id", "pool_sequence")
        return {**{k: row[k] for k in allowed}, "page": snap["page"], "material": snap["material"],
                "page_name": snap["page"]["page_name"], "material_name": snap["material"]["material_name"],
                "caption_template": snap["caption_template"], "template_version": snap["template_version"],
                "row_number": snap.get("row_number"), "title": snap.get("title", ""),
                "credential_mode": snap.get("credential_mode", "page_pool"),
                "prepared": json.loads(row["prepared_json"]), "can_cancel": row["status"] == "queued"}

    def _batch_dto(self, conn, row):
        tasks = [self.task_dto(t) for t in conn.execute("SELECT * FROM fb_video_task WHERE batch_id=? ORDER BY sequence", (row["batch_id"],))]
        statuses = {t["status"] for t in tasks}
        state = "unknown" if "unknown" in statuses else ("completed" if statuses <= TERMINAL else ("running" if statuses & {"running", "submitted"} else "queued"))
        return {"batch_id": row["batch_id"], "operation_id": row["operation_id"], "owner_user_id": row["owner_user_id"],
                "owner_name": row["owner_name"], "created_at_utc": row["created_at_utc"], "status": state,
                "processing_mode": json.loads(row["request_json"])["processing_mode"], "items": tasks,
                "excel": json.loads(row["request_json"]).get("excel")}

    def get_batch(self, actor, bid):
        scope, args = self._scope(actor)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM fb_video_batch WHERE batch_id=? AND " + scope, [bid, *args]).fetchone()
            if not row:
                raise DomainError("batch_not_found", "批次不存在", 404)
            return {"ok": True, "batch": self._batch_dto(conn, row)}

    def list_batches(self, actor, limit=50, cursor=""):
        scope, args = self._scope(actor)
        with self.connect() as conn:
            if cursor:
                scope += " AND rowid<?"
                args.append(int(cursor))
            rows = conn.execute("SELECT rowid AS cursor_id,* FROM fb_video_batch WHERE " + scope + " ORDER BY rowid DESC LIMIT ?", [*args, limit + 1]).fetchall()
            items = []
            for r in rows[:limit]:
                counts = {x[0]: x[1] for x in conn.execute("SELECT status,COUNT(*) FROM fb_video_task WHERE batch_id=? GROUP BY status", (r["batch_id"],))}
                statuses = set(counts)
                state = "unknown" if "unknown" in statuses else ("completed" if statuses <= TERMINAL else ("running" if statuses & {"running", "submitted"} else "queued"))
                items.append({"batch_id": r["batch_id"], "operation_id": r["operation_id"], "owner_user_id": r["owner_user_id"],
                              "owner_name": r["owner_name"], "created_at_utc": r["created_at_utc"], "status": state,
                              "processing_mode": json.loads(r["request_json"])["processing_mode"], "counts": counts,
                              "task_count": sum(counts.values()),
                              "first_planned_at_utc": conn.execute("SELECT MIN(planned_at_utc) FROM fb_video_task WHERE batch_id=?", (r["batch_id"],)).fetchone()[0]})
            return {"ok": True, "items": items,
                    "next_cursor": str(rows[limit - 1]["cursor_id"]) if len(rows) > limit else ""}

    def get_task(self, actor, tid):
        where, args = self._scope(actor)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=? AND " + where, [tid, *args]).fetchone()
            if row is None:
                raise DomainError("task_not_found", "任务不存在", 404)
            return {"ok": True, "task": self.task_dto(row)}

    def list_tasks(self, actor, limit=50, cursor="", status=""):
        where, args = self._scope(actor)
        if cursor:
            where += " AND rowid<?"
            args.append(int(cursor))
        if status:
            where += " AND status=?"
            args.append(status)
        with self.connect() as conn:
            rows = conn.execute("SELECT rowid AS cursor_row,* FROM fb_video_task WHERE " + where
                                + " ORDER BY rowid DESC LIMIT ?", [*args, limit + 1]).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            return {"ok": True, "items": [self.task_dto(r) for r in rows], "has_more": more,
                    "next_cursor": str(rows[-1]["cursor_row"]) if more and rows else None}

    def cancel(self, actor, tid):
        scope, args = self._scope(actor)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=? AND " + scope, [tid, *args]).fetchone()
            if not row:
                raise DomainError("task_not_found", "任务不存在", 404)
            if row["status"] not in {"queued", "cancelled"}:
                raise DomainError("task_not_cancellable", "任务已开始向Meta提交，不能取消", 409)
            # An in-flight GPU call cannot be revoked. Keep its capacity lease
            # until the callback/expiry while permanently fencing publication.
            conn.execute("""UPDATE fb_video_task SET status='cancelled',
                link_lease='',link_until='',updated_at_utc=? WHERE task_id=?""", (stamp(self.now_fn()), tid))
            conn.commit()
            return {"ok": True, "task": self.task_dto(conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (tid,)).fetchone())}

    def claim(self, kind, worker_id):
        now = stamp(self.now_fn())
        lease = uuid.uuid4().hex
        seconds = 10200 if kind == "prepare" else (1200 if kind in {"execute", "reconcile"} else 300)
        until = stamp(self.now_fn() + timedelta(seconds=seconds))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "prepare":
                row = conn.execute("""SELECT * FROM fb_video_task WHERE status='queued' AND processing_mode='random_overlay'
                    AND prepare_retry_at<=? AND (prepare_state='planned' OR (prepare_state='preparing' AND prepare_until<=?))
                    ORDER BY planned_at_utc,sequence LIMIT 1""", (now, now)).fetchone()
                if row is not None and not prepare_available(conn, now, "manual", row["planned_at_utc"]):
                    conn.commit()
                    return None
            elif kind == "link":
                row = conn.execute("""SELECT * FROM fb_video_task WHERE status='queued' AND link_state<>'ready'
                    AND link_retry_at<=? AND (link_lease='' OR link_until<=?) ORDER BY planned_at_utc,sequence LIMIT 1""", (now, now)).fetchone()
            elif kind == "reconcile":
                row = conn.execute("""SELECT * FROM fb_video_task WHERE status='submitted' AND reconcile_at<=?
                    AND (execution_lease='' OR execution_until<?) ORDER BY reconcile_at,sequence LIMIT 1""", (now, now)).fetchone()
            elif kind == "execute":
                rows = conn.execute("""SELECT x.*,b.request_json AS batch_request_json FROM fb_video_task x
                    JOIN fb_video_batch b ON b.batch_id=x.batch_id
                    WHERE x.status='queued' AND x.prepare_state='ready'
                    AND x.link_state='ready' AND x.planned_at_utc<=? AND NOT EXISTS(
                      SELECT 1 FROM fb_video_task prev WHERE prev.batch_id=x.batch_id AND prev.page_id=x.page_id
                      AND prev.page_sequence<x.page_sequence AND prev.status NOT IN ('published','failed','cancelled'))
                    AND NOT EXISTS(SELECT 1 FROM fb_video_task pp WHERE x.source_kind='pool'
                      AND pp.tenant_key=x.tenant_key AND pp.page_id=x.page_id AND pp.pool_sequence<x.pool_sequence
                      AND pp.source_kind='pool' AND pp.status NOT IN ('published','failed','cancelled'))
                    AND NOT EXISTS(SELECT 1 FROM fb_video_task gp WHERE x.source_kind='group_pool'
                      AND gp.tenant_key=x.tenant_key AND gp.page_id=x.page_id AND gp.source_kind='group_pool'
                      AND gp.planned_at_utc<x.planned_at_utc AND gp.status NOT IN ('published','failed','cancelled'))
                    ORDER BY x.planned_at_utc,x.sequence LIMIT 1000""", (now,)).fetchall()
                row = None
                for candidate in rows:
                    if (automatic_block(conn, candidate["page_id"], execution=True)
                            or manual_block(conn, candidate["page_id"], execution=True, exclude_task=candidate["task_id"])):
                        continue
                    interval = json.loads(candidate["batch_request_json"]).get("interval_minutes", 0)
                    if interval:
                        # Published rows are immutable: their final updated_at
                        # is the time Graph reconciliation confirmed success.
                        last_success = conn.execute("""SELECT MAX(updated_at_utc) FROM fb_video_task
                            WHERE batch_id=? AND page_id=? AND page_sequence<? AND status='published'""",
                            (candidate["batch_id"], candidate["page_id"], candidate["page_sequence"])).fetchone()[0]
                        if last_success and parse_time(last_success) + timedelta(minutes=interval) > self.now_fn():
                            continue
                    row = candidate
                    break
            else:
                raise ValueError("invalid claim kind")
            if row is None:
                conn.commit()
                return None
            if kind in {"execute", "reconcile"}:
                conn.execute("UPDATE fb_video_task SET status=?,execution_lease=?,execution_until=?,updated_at_utc=? WHERE task_id=?",
                             ("running" if kind == "execute" else "submitted", lease, until, now, row["task_id"]))
            elif kind == "prepare":
                conn.execute("UPDATE fb_video_task SET prepare_state='preparing',prepare_lease=?,prepare_until=?,prepare_attempts=prepare_attempts+1,updated_at_utc=? WHERE task_id=?",
                             (lease, until, now, row["task_id"]))
            else:
                conn.execute("UPDATE fb_video_task SET link_state='writing',link_lease=?,link_until=?,updated_at_utc=? WHERE task_id=?", (lease, until, now, row["task_id"]))
            conn.commit()
            result = dict(conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (row["task_id"],)).fetchone())
            result["claim_token"], result["worker_id"] = lease, worker_id
            return result

    def claim_is_current(self, task, *, status="running"):
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM fb_video_task WHERE task_id=? AND status=? AND execution_lease=? AND execution_until>?",
                                     (task["task_id"], status, task["claim_token"], stamp(self.now_fn()))).fetchone())

    def finish_stage(self, task, kind, result=None, error=None, retry=False):
        now, retry_at = stamp(self.now_fn()), stamp(self.now_fn() + timedelta(minutes=5))
        prefix = "prepare" if kind == "prepare" else "link"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"""SELECT * FROM fb_video_task WHERE task_id=? AND status='queued'
                AND {prefix}_lease=? AND {prefix}_until>?""",
                               (task["task_id"], task["claim_token"], now)).fetchone()
            if not row:
                if kind == "prepare":
                    conn.execute("""UPDATE fb_video_task SET prepare_lease='',prepare_until=''
                        WHERE task_id=? AND status='cancelled' AND prepare_lease=?""",
                        (task["task_id"], task["claim_token"]))
                conn.commit()
                return {"ok": True, "status": "stale_claim"}
            if error:
                code, message = str(getattr(error, "code", "stage_failed"))[:96], str(error)[:300]
                if retry and (kind != "prepare" or row["prepare_attempts"] < 5):
                    conn.execute(f"UPDATE fb_video_task SET {prefix}_state=?,{prefix}_lease='',{prefix}_until='',{prefix}_retry_at=?,error_code=?,error_message=?,updated_at_utc=? WHERE task_id=?",
                                 ("planned" if kind == "prepare" else "pending", retry_at, code, message, now, task["task_id"]))
                else:
                    conn.execute(f"UPDATE fb_video_task SET status='failed',{prefix}_state='failed',{prefix}_lease='',{prefix}_until='',error_code=?,error_message=?,updated_at_utc=? WHERE task_id=?",
                                 (code, message, now, task["task_id"]))
            elif kind == "prepare":
                conn.execute("UPDATE fb_video_task SET prepare_state='ready',prepared_json=?,prepare_lease='',prepare_until='',error_code='',error_message='',updated_at_utc=? WHERE task_id=?",
                             (canonical(result), now, task["task_id"]))
            else:
                conn.execute("UPDATE fb_video_task SET link_state='ready',link_sha256=?,link_lease='',link_until='',error_code='',error_message='',updated_at_utc=? WHERE task_id=?",
                             (result["sha256"], now, task["task_id"]))
            conn.commit()
            return {"ok": True, "task_id": task["task_id"], "status": "stage_complete"}

    def begin_attempt(self, task, credential_id, fb_user_id):
        """Renew a current claim and persist intent before any Graph write."""
        now = stamp(self.now_fn())
        until = stamp(self.now_fn() + timedelta(seconds=1200))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""SELECT 1 FROM fb_video_task WHERE task_id=? AND status='running'
                AND execution_lease=? AND execution_until>?""", (task["task_id"], task["claim_token"], now)).fetchone()
            if not current:
                conn.commit()
                return None
            if conn.execute("SELECT 1 FROM fb_video_attempt WHERE task_id=? AND result_kind='started'",
                            (task["task_id"],)).fetchone():
                conn.commit()
                return None
            # There may be more than eight credentials. Renew only while the
            # existing lease is valid, after the prior write has a saved result.
            conn.execute("UPDATE fb_video_task SET execution_until=? WHERE task_id=?",
                         (until, task["task_id"]))
            attempt_id = conn.execute("""INSERT INTO fb_video_attempt(task_id,lease_token,credential_id,fb_user_id,
                result_kind,created_at_utc) VALUES(?,?,?,?,'started',?)""",
                (task["task_id"], task["claim_token"], str(credential_id), str(fb_user_id), now)).lastrowid
            conn.commit()
            return attempt_id

    def record_outcome(self, task, outcome, *, credential_id="", fb_user_id="", final=True, reconcile=False, attempt_id=None):
        now = stamp(self.now_fn())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            required_status = "submitted" if reconcile else "running"
            row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=? AND status=? AND execution_lease=?",
                               (task["task_id"], required_status, task["claim_token"])).fetchone()
            if row is None:
                conn.commit()
                return {"ok": True, "status": "stale_claim"}
            kind = outcome.kind
            post = str(outcome.post_id or row["graph_post_id"])
            if attempt_id is not None:
                changed = conn.execute("""UPDATE fb_video_attempt SET result_kind=?,graph_post_id=?,error_code=?,trace_id=?
                    WHERE id=? AND task_id=? AND lease_token=? AND result_kind='started'""",
                    (kind, post, str(outcome.error_code)[:96], str(outcome.trace_id)[:128],
                     attempt_id, task["task_id"], task["claim_token"])).rowcount
                if changed != 1:
                    conn.rollback()
                    return {"ok": True, "status": "stale_claim"}
            else:
                conn.execute("INSERT INTO fb_video_attempt(task_id,lease_token,credential_id,fb_user_id,result_kind,graph_post_id,error_code,trace_id,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                             (task["task_id"], task["claim_token"], str(credential_id), str(fb_user_id), kind, post, str(outcome.error_code)[:96], str(outcome.trace_id)[:128], now))
            if final:
                status = ("published" if reconcile else "submitted") if kind == "success" else ("unknown" if kind == "unknown" and not reconcile else ("submitted" if reconcile and kind in {"unknown", "credential_failure"} else "failed"))
                # Explicit reconciliation hold supplied by worker preserves unknown.
                if kind == "hold":
                    status = "unknown"
                conn.execute("""UPDATE fb_video_task SET status=?,graph_post_id=?,unknown_outcome=?,error_code=?,error_message=?,
                    execution_lease='',execution_until='',reconcile_at=?,updated_at_utc=? WHERE task_id=?""",
                    (status, post, int(status == "unknown"), str(outcome.error_code)[:96], str(outcome.message)[:300],
                     stamp(self.now_fn() + timedelta(seconds=60)), now, task["task_id"]))
            conn.commit()
            return {"ok": True, "task_id": task["task_id"], "status": status if final else required_status}

    def tick(self):
        now = stamp(self.now_fn())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("""UPDATE fb_video_task SET status='unknown',unknown_outcome=1,
                error_code='worker_interrupted',error_message='平台请求执行中断，结果待人工核对',updated_at_utc=?
                WHERE status='running' AND execution_until<>'' AND execution_until<?""", (now, now)).rowcount
            conn.commit()
            return {"ok": True, "status": "tick_complete", "unknown_marked": count}

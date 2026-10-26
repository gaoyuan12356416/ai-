"""Tenant-shared, language-routed FB pool. Reads never reserve or publish."""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from datetime import timedelta

from .core import BJ, DomainError, actor_checked, canonical, digest, id_text, parse_time, stamp
from .guards import automatic_block, has_table, manual_block
from .templates import render_preview, validate_template

POLICY_VERSION = "group_pool_v1"
SCAN_LIMIT = 5000
DEFAULT_CAPTION = "{name}\n{desc}\n{url}"
MUTATION = {"operation_id", "expected_version"}
STATUSES = {"pending", "bound", "queued", "running", "submitted", "published", "failed", "cancelled", "unknown", "removed"}


def install_schema(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS fb_group_pool(
        tenant_key TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 0,
        schedule_revision INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'paused',
        group_ids_json TEXT NOT NULL DEFAULT '[]',schedule_mode TEXT NOT NULL DEFAULT 'fixed',
        times_json TEXT NOT NULL DEFAULT '[]',daily_batches INTEGER NOT NULL DEFAULT 1,
        caption_template TEXT NOT NULL,processing_mode TEXT NOT NULL DEFAULT 'original',
        publisher_user_id TEXT NOT NULL DEFAULT '',publisher_name TEXT NOT NULL DEFAULT '',
        pause_reason TEXT NOT NULL DEFAULT '',effective_at_utc TEXT NOT NULL DEFAULT '',
        created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS fb_group_pool_item(
        id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,material_id TEXT NOT NULL,
        language TEXT NOT NULL,material_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        added_by TEXT NOT NULL,task_id TEXT NOT NULL DEFAULT '',batch_id TEXT NOT NULL DEFAULT '',
        created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS uq_fb_group_pool_material ON fb_group_pool_item(tenant_key,material_id)
        WHERE status<>'removed';
      CREATE INDEX IF NOT EXISTS idx_fb_group_pool_items ON fb_group_pool_item(tenant_key,status,id);
      CREATE TABLE IF NOT EXISTS fb_group_pool_operation(
        tenant_key TEXT NOT NULL,owner_user_id TEXT NOT NULL,operation_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,
        PRIMARY KEY(tenant_key,owner_user_id,operation_id));
      CREATE TABLE IF NOT EXISTS fb_group_pool_day(
        tenant_key TEXT NOT NULL,bj_date TEXT NOT NULL,schedule_revision INTEGER NOT NULL,
        mode TEXT NOT NULL,times_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,
        PRIMARY KEY(tenant_key,bj_date,schedule_revision));
      CREATE TABLE IF NOT EXISTS fb_group_pool_run(
        id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,slot_at_utc TEXT NOT NULL,
        schedule_revision INTEGER NOT NULL,publisher_user_id TEXT NOT NULL,max_item_id INTEGER NOT NULL,
        policy_version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
        pairs_json TEXT NOT NULL DEFAULT '[]',excluded_json TEXT NOT NULL DEFAULT '[]',
        batch_id TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,
        UNIQUE(tenant_key,slot_at_utc));
      CREATE TABLE IF NOT EXISTS fb_group_pool_reservation(
        tenant_key TEXT NOT NULL,material_id TEXT NOT NULL,item_id INTEGER NOT NULL UNIQUE,
        run_id INTEGER NOT NULL,task_id TEXT NOT NULL UNIQUE,created_at_utc TEXT NOT NULL,
        PRIMARY KEY(tenant_key,material_id));
      CREATE INDEX IF NOT EXISTS idx_fb_group_pool_runs ON fb_group_pool_run(tenant_key,id);
    """)


def pool_row(conn, tenant):
    row = conn.execute("SELECT * FROM fb_group_pool WHERE tenant_key=?", (tenant,)).fetchone()
    if row:
        return dict(row)
    return {"tenant_key": tenant, "version": 0, "schedule_revision": 0, "status": "paused",
            "group_ids_json": "[]", "schedule_mode": "fixed", "times_json": "[]", "daily_batches": 1,
            "caption_template": DEFAULT_CAPTION, "processing_mode": "original", "publisher_user_id": "",
            "publisher_name": "", "pause_reason": "", "effective_at_utc": "", "created_at_utc": "", "updated_at_utc": ""}


def pool_dto(row):
    return {k: row[k] for k in ("version", "status", "schedule_mode", "daily_batches", "caption_template",
            "processing_mode", "publisher_user_id", "publisher_name", "pause_reason", "effective_at_utc")} | {
            "group_ids": json.loads(row["group_ids_json"]), "times": json.loads(row["times_json"])}


def ids(values, *, maximum=5000):
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise DomainError("invalid_request", "请选择1至" + str(maximum) + "个ID")
    return list(dict.fromkeys(id_text(v) for v in values))


def historical_materials(conn, material_ids):
    """All previous FB task states occupy materials; X history is unrelated."""
    occupied = set()
    for start in range(0, len(material_ids), 400):
        chunk = material_ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        for table in ("fb_video_task", "fb_auto_task"):
            if chunk and has_table(conn, table):
                occupied.update(str(r[0]) for r in conn.execute(
                    "SELECT DISTINCT material_id FROM " + table + " WHERE material_id IN (" + marks + ")", chunk))
    return occupied


def exclusion(code, message, **context):
    return {"code": code, "message": message, **context}


def random_times(tenant, day, revision, count):
    """Uniformly distribute the available slack around mandatory 60-minute gaps.

    24 batches use a shared nonzero minute offset, the only possible spacing.
    A deterministic seed makes pre-persistence calculation stable; the stored
    day plan remains authoritative after it has been inserted.
    """
    rng = random.Random(digest([tenant, day, revision, POLICY_VERSION]))
    slack = 1439 - 60 * (count - 1)
    for _ in range(1000):
        offsets = sorted(rng.choices(range(slack + 1), k=count))
        minutes = [60 * i + n for i, n in enumerate(offsets)]
        if all(n % 60 for n in minutes):
            return [f"{n // 60:02d}:{n % 60:02d}" for n in minutes]
    offset = rng.randint(1, 59)
    return [f"{i:02d}:{offset:02d}" for i in range(count)]


class GroupPoolService:
    def __init__(self, service, *, sources=None):
        self.service, self.store = service, service.store
        if sources is None:
            from .group_sources import GroupPoolSource
            sources = GroupPoolSource(service.sources)
        self.sources = sources
        self.enabled = bool(getattr(service, "group_pool_enabled", False))
        self.identity_resolver = None

    @staticmethod
    def _keys(payload, keys):
        if not isinstance(payload, dict) or set(payload) != set(keys):
            raise DomainError("invalid_request", "请求字段不完整或包含未知字段")

    def _operation(self, actor, suffix, payload):
        op = payload.get("operation_id")
        if not isinstance(op, str) or not re.fullmatch(r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", op):
            raise DomainError("invalid_request", "操作标识必须为UUID")
        if type(payload.get("expected_version")) is not int or payload["expected_version"] < 0:
            raise DomainError("invalid_request", "素材池版本无效")
        hashed = digest({"route": suffix, "payload": payload})
        with self.store.connect() as conn:
            prior = self._prior(conn, actor, op, hashed)
        return op, hashed, prior

    @staticmethod
    def _prior(conn, actor, op, hashed=None):
        row = conn.execute("SELECT * FROM fb_group_pool_operation WHERE tenant_key=? AND owner_user_id=? AND operation_id=?",
                           (actor["tenant_key"], actor["user_id"], op)).fetchone()
        if not row:
            return None
        if hashed is not None and row["request_hash"] != hashed:
            raise DomainError("operation_conflict", "操作标识已用于不同请求", 409)
        return {**json.loads(row["response_json"]), "idempotent": True}

    def _locked(self, conn, actor, payload):
        row = pool_row(conn, actor["tenant_key"])
        if row["version"] != payload["expected_version"]:
            raise DomainError("pool_version_conflict", "素材池已改变，请刷新后重试", 409)
        now = stamp(self.store.now_fn())
        conn.execute("""INSERT OR IGNORE INTO fb_group_pool(tenant_key,caption_template,created_at_utc,updated_at_utc)
                        VALUES(?,?,?,?)""", (actor["tenant_key"], DEFAULT_CAPTION, now, now))
        return row

    def _finish(self, conn, actor, op, hashed, **result):
        now = stamp(self.store.now_fn())
        conn.execute("UPDATE fb_group_pool SET version=version+1,updated_at_utc=? WHERE tenant_key=?", (now, actor["tenant_key"]))
        response = {"ok": True, "operation_id": op, "idempotent": False,
                    "pool": pool_dto(pool_row(conn, actor["tenant_key"])), **result}
        conn.execute("INSERT INTO fb_group_pool_operation VALUES(?,?,?,?,?,?)",
                     (actor["tenant_key"], actor["user_id"], op, hashed, canonical(response), now))
        conn.commit()
        return response

    def _mutate(self, actor, suffix, payload):
        extras = {"/items": {"material_ids"}, "/items/remove": {"item_ids"},
                  "/schedule": {"group_ids", "schedule_mode", "times", "daily_batches", "caption_template", "processing_mode"},
                  "/enable": set(), "/pause": set()}[suffix]
        self._keys(payload, MUTATION | extras)
        op, hashed, prior = self._operation(actor, suffix, payload)
        if prior:
            return prior
        # External sources must be resolved before taking the SQLite write lock.
        resolved, clean = None, None
        if suffix == "/items":
            mids = ids(payload["material_ids"])
            resolved = self.sources.resolve_materials(mids, need_desc=False)
        elif suffix == "/items/remove":
            clean = ids(payload["item_ids"])
        elif suffix == "/schedule":
            group_ids = sorted(ids(payload["group_ids"], maximum=200), key=int)
            self.sources.resolve_scope(actor, group_ids)
            mode, times, count = payload["schedule_mode"], payload["times"], payload["daily_batches"]
            if mode not in {"fixed", "random"} or type(count) is not int or not 1 <= count <= 24:
                raise DomainError("invalid_schedule", "排期模式或每日批次数无效")
            if (not isinstance(times, list) or len(times) > 1440
                    or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in times)
                    or len(set(times)) != len(times) or (mode == "fixed" and not times)):
                raise DomainError("invalid_schedule", "固定时间使用不重复的HH:mm")
            if payload["processing_mode"] != "original":
                raise DomainError("invalid_request", "组素材池当前使用原片发布")
            clean = {"groups": canonical(group_ids), "mode": mode, "times": canonical(sorted(times)), "count": count,
                     "caption": validate_template(payload["caption_template"])}
        elif suffix == "/enable":
            if self.identity_resolver is None:
                raise DomainError("identity_unavailable", "启用前身份复核尚未配置", 503)
            fresh_actor = actor_checked(self.identity_resolver(actor["tenant_key"], actor["user_id"]))
            if (fresh_actor["tenant_key"], fresh_actor["user_id"]) != (actor["tenant_key"], actor["user_id"]):
                raise DomainError("identity_inactive", "启用人身份解析不匹配", 403)
            actor = fresh_actor
            if not self.service.live_enabled:
                raise DomainError("live_gate_closed", "发布服务开关未开启", 409)
            with self.store.connect() as conn:
                row = pool_row(conn, actor["tenant_key"])
            if not json.loads(row["group_ids_json"]) or (row["schedule_mode"] == "fixed" and not json.loads(row["times_json"])):
                raise DomainError("schedule_incomplete", "请先保存Page组和排期", 409)
            if not re.fullmatch(r"[1-9][0-9]{0,30}", actor["sub_user_id"]):
                raise DomainError("identity_attribution_invalid", "发布人缺少有效归因身份", 403)
            self.sources.resolve_scope(actor, json.loads(row["group_ids_json"]))
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = self._prior(conn, actor, op, hashed)
            if prior:
                return prior
            row = self._locked(conn, actor, payload)
            now = stamp(self.store.now_fn())
            result = {}
            if suffix == "/items":
                inserted, duplicates, errors = [], [], list(resolved["errors"])
                occupied = historical_materials(conn, mids)
                for material in resolved["items"]:
                    mid = str(material["material_id"])
                    old = conn.execute("SELECT id FROM fb_group_pool_item WHERE tenant_key=? AND material_id=? AND status<>'removed'",
                                       (actor["tenant_key"], mid)).fetchone()
                    if old:
                        duplicates.append(mid)
                    elif mid in occupied:
                        errors.append(exclusion("material_previously_bound", "素材已有FB任务历史，不能自动重复发布", material_id=mid))
                    else:
                        cur = conn.execute("""INSERT INTO fb_group_pool_item(tenant_key,material_id,language,material_json,
                            added_by,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?)""",
                            (actor["tenant_key"], mid, material["language"], canonical(material), actor["user_id"], now, now))
                        inserted.append({"id": str(cur.lastrowid), "material_id": mid})
                result = {"added": inserted, "duplicates": duplicates, "errors": errors}
            elif suffix == "/items/remove":
                for item_id in clean:
                    item = conn.execute("SELECT * FROM fb_group_pool_item WHERE tenant_key=? AND id=?", (actor["tenant_key"], item_id)).fetchone()
                    if not item or item["status"] != "pending" or item["task_id"]:
                        raise DomainError("item_not_removable", "仅可删除本租户尚未绑定任务的素材", 409)
                for item_id in clean:
                    conn.execute("UPDATE fb_group_pool_item SET status='removed',updated_at_utc=? WHERE id=? AND tenant_key=?", (now, item_id, actor["tenant_key"]))
                result = {"removed": clean}
            elif suffix == "/schedule":
                effective = self.store.now_fn()
                if clean["mode"] == "random":
                    effective = (effective.astimezone(BJ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                changed_groups = row["group_ids_json"] != clean["groups"]
                conn.execute("""UPDATE fb_group_pool SET group_ids_json=?,schedule_mode=?,times_json=?,daily_batches=?,
                    caption_template=?,processing_mode='original',schedule_revision=schedule_revision+1,effective_at_utc=?,
                    status=?,pause_reason=? WHERE tenant_key=?""",
                    (clean["groups"], clean["mode"], clean["times"], clean["count"], clean["caption"],
                     effective.isoformat(timespec="microseconds"), "paused" if changed_groups else row["status"],
                     "groups_changed" if changed_groups else row["pause_reason"], actor["tenant_key"]))
            elif suffix == "/enable":
                effective = self.store.now_fn()
                if row["schedule_mode"] == "random":
                    effective = (effective.astimezone(BJ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                conn.execute("""UPDATE fb_group_pool SET status='enabled',publisher_user_id=?,publisher_name=?,pause_reason='',
                    effective_at_utc=? WHERE tenant_key=?""", (actor["user_id"], actor["user_name"], effective.isoformat(timespec="microseconds"), actor["tenant_key"]))
            else:
                conn.execute("UPDATE fb_group_pool SET status='paused',pause_reason='operator_paused' WHERE tenant_key=?", (actor["tenant_key"],))
            return self._finish(conn, actor, op, hashed, **result)

    @staticmethod
    def _pagination(query):
        try:
            limit = int(query.get("limit", 50))
            cursor = int(query.get("cursor") or 0)
            if not 1 <= limit <= 100 or cursor < 0:
                raise ValueError()
            return limit, cursor
        except (TypeError, ValueError):
            raise DomainError("invalid_request", "分页参数无效") from None

    def listing(self, actor, query):
        state, language = query.get("state", "pending"), str(query.get("language") or "")
        if state != "all" and state not in STATUSES:
            raise DomainError("invalid_request", "筛选状态无效")
        limit, cursor = self._pagination(query)
        expr = "CASE WHEN t.task_id IS NOT NULL THEN t.status ELSE i.status END"
        base = " FROM fb_group_pool_item i LEFT JOIN fb_video_task t ON t.task_id=i.task_id WHERE i.tenant_key=?"
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            counts = {k: 0 for k in STATUSES}
            for row in conn.execute("SELECT " + expr + ",COUNT(*)" + base + " GROUP BY 1", (actor["tenant_key"],)):
                counts[row[0]] = row[1]
            clauses, args = "", [actor["tenant_key"]]
            if state != "all":
                clauses += " AND " + ("i.task_id<>''" if state == "bound" else "(" + expr + ")=?")
                if state != "bound":
                    args.append(state)
            if language:
                clauses += " AND i.language=?"
                args.append(language)
            if cursor:
                clauses += " AND i.id<?"
                args.append(cursor)
            rows = conn.execute("SELECT i.*,t.status AS task_status,t.owner_user_id AS task_owner,t.error_message AS task_error" + base + clauses +
                                " ORDER BY i.id DESC LIMIT ?", [*args, limit + 1]).fetchall()
            items = []
            for row in rows[:limit]:
                material = json.loads(row["material_json"])
                visible = bool(row["task_id"]) and (actor["is_admin"] or row["task_owner"] == actor["user_id"])
                status = row["task_status"] or row["status"]
                items.append({"id": str(row["id"]), "material_id": row["material_id"],
                    "material_name": material.get("material_name", ""), "drama_name": material.get("drama_name", ""),
                    "language": row["language"], "status": status, "created_at_utc": row["created_at_utc"],
                    "task_id": row["task_id"] if visible else "", "batch_id": row["batch_id"] if visible else "",
                    "can_view_task": visible, "can_modify": not row["task_id"] and row["status"] == "pending",
                    "error_message": (row["task_error"] or "") if visible else "发布结果待核对" if status == "unknown" else ""})
            pool = pool_dto(pool_row(conn, actor["tenant_key"]))
        counts["bound"] = sum(counts[k] for k in ("queued", "running", "submitted", "published", "failed", "cancelled", "unknown"))
        return {"ok": True, "pool": pool, "feature_enabled": self.enabled, "items": items, "counts": counts, "has_more": len(rows) > limit,
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    def candidates(self, actor, pool, *, seed, max_item_id=None):
        """Freeze a bounded snapshot, then resolve current external facts unlocked."""
        scope = self.sources.resolve_scope(actor, json.loads(pool["group_ids_json"]))
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            if max_item_id is None:
                max_item_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM fb_group_pool_item WHERE tenant_key=?", (actor["tenant_key"],)).fetchone()[0]
            rows = [dict(r) for r in conn.execute("""SELECT * FROM fb_group_pool_item WHERE tenant_key=? AND id<=?
                         AND status='pending' AND task_id='' ORDER BY id DESC LIMIT ?""",
                         (actor["tenant_key"], max_item_id, SCAN_LIMIT + 1))]
            scan_truncated = len(rows) > SCAN_LIMIT
            rows = rows[:SCAN_LIMIT]
            occupied = historical_materials(conn, [r["material_id"] for r in rows])
        available = [r for r in rows if r["material_id"] not in occupied]
        materials, excluded = {}, list(scope["excluded"])
        if scan_truncated:
            excluded.append(exclusion("candidate_scan_limit", "本批仅扫描最新5000条待发素材，更早素材继续留池", scan_limit=SCAN_LIMIT))
        excluded += [exclusion("material_previously_bound", "素材已有FB任务历史", material_id=r["material_id"]) for r in rows if r["material_id"] in occupied]
        pages = dict(scope["pages"])
        needed = {}
        for page in pages.values():
            needed[page["language"]] = needed.get(page["language"], 0) + 1
        # Resolve newest candidates in bounded chunks. Invalid newest rows must
        # not hide a valid older row; languages with no target need no query.
        deadline, offset = time.monotonic() + 25, 0
        while offset < len(available) and any(needed.values()):
            if time.monotonic() >= deadline:
                excluded.append(exclusion("source_scan_budget_exceeded", "本轮素材检查已到时间上限，其余素材继续留池"))
                break
            chunk = []
            while offset < len(available) and len(chunk) < 100:
                row = available[offset]
                offset += 1
                if needed.get(row["language"], 0):
                    chunk.append(row)
            if not chunk:
                continue
            resolved = self.sources.resolve_materials([r["material_id"] for r in chunk], need_desc="{desc}" in pool["caption_template"])
            excluded.extend(resolved["errors"])
            fresh = {str(m["material_id"]): m for m in resolved["items"]}
            for row in chunk:
                material = fresh.get(row["material_id"])
                if material is None or not needed.get(row["language"], 0):
                    continue
                frozen = json.loads(row["material_json"])
                if any(str(material.get(k, "")) != str(frozen.get(k, "")) for k in ("material_id", "content_id", "language", "media_url")):
                    excluded.append(exclusion("material_identity_changed", "入池后素材身份或语言改变，请移除后重新导入", material_id=row["material_id"]))
                    continue
                try:
                    render_preview(pool["caption_template"], material)
                except Exception as exc:
                    if not hasattr(exc, "code"):
                        raise
                    excluded.append(exclusion(exc.code, str(exc), material_id=row["material_id"]))
                    continue
                materials[row["material_id"]] = material
                needed[material["language"]] -= 1
        ordered_pages = sorted(pages, key=lambda pid: digest([seed, pid]))
        return {"scope": scope, "pages": pages, "page_order": ordered_pages, "materials": materials,
                "rows": available, "excluded": excluded, "max_item_id": max_item_id,
                "scan_limit": SCAN_LIMIT, "scan_truncated": scan_truncated}

    def match(self, conn, actor, pool, facts, *, day):
        """Recheck local reservations, quota and guards within the caller transaction."""
        excluded = list(facts["excluded"])
        used = conn.execute("SELECT COUNT(*) FROM fb_video_task WHERE planned_bj_date=? AND status<>'cancelled'", (day,)).fetchone()[0]
        remaining = max(0, self.store.daily_limit - used)
        occupied = historical_materials(conn, list(facts["materials"]))
        min_item_id = min((row["id"] for row in facts["rows"]), default=facts["max_item_id"] + 1)
        live_ids = {r[0] for r in conn.execute("""SELECT id FROM fb_group_pool_item WHERE tenant_key=? AND id<=?
                   AND id>=? AND status='pending' AND task_id='' ORDER BY id DESC LIMIT ?""",
                   (actor["tenant_key"], facts["max_item_id"], min_item_id, SCAN_LIMIT))}
        queues = {}
        for row in facts["rows"]:
            material = facts["materials"].get(row["material_id"])
            if row["id"] in live_ids and material and row["material_id"] not in occupied:
                queues.setdefault(material["language"], []).append((row, material))
        pairs = []
        for pid in facts["page_order"]:
            page = facts["pages"][pid]
            reason = automatic_block(conn, pid) or manual_block(conn, pid)
            if reason:
                excluded.append(exclusion(reason, "Page存在结果不明的任务，先完成核对", page_id=pid))
                continue
            queue = queues.get(page["language"], [])
            if not queue:
                excluded.append(exclusion("language_material_unavailable", "没有可用的同语言素材", page_id=pid, language=page["language"]))
                continue
            if len(pairs) >= remaining:
                excluded.append(exclusion("daily_capacity_exceeded", "本日发布额度不足", page_id=pid))
                continue
            row, material = queue.pop(0)
            pairs.append({"item_id": str(row["id"]), "material_id": row["material_id"], "material_name": material["material_name"],
                          "language": material["language"], "page_id": pid, "page_name": page["page_name"],
                          "caption": render_preview(pool["caption_template"], material)})
        languages = {p["language"] for p in facts["pages"].values()}
        for row in facts["rows"]:
            if row["language"] not in languages:
                excluded.append(exclusion("language_page_unavailable", "所选组没有同语言可用Page，素材继续留池", material_id=row["material_id"], language=row["language"]))
        all_languages = sorted(languages | {r["language"] for r in facts["rows"]})
        capacity = [{"language": lang, "page_count": sum(p["language"] == lang for p in facts["pages"].values()),
                     "material_count": sum(r["language"] == lang and r["material_id"] not in occupied for r in facts["rows"]),
                     "matched_count": sum(p["language"] == lang for p in pairs)} for lang in all_languages]
        return {"ok": True, "pairs": pairs, "excluded": excluded, "max_item_id": facts["max_item_id"],
                "quota_remaining": remaining, "language_capacity": capacity,
                "scan_limit": facts["scan_limit"], "scan_truncated": facts["scan_truncated"]}

    def preview(self, actor, payload):
        if not isinstance(payload, dict) or set(payload) - {"group_ids", "caption_template"}:
            raise DomainError("invalid_request", "预览字段无效")
        with self.store.connect() as conn:
            pool = pool_row(conn, actor["tenant_key"])
        if "group_ids" in payload:
            pool["group_ids_json"] = canonical(sorted(ids(payload["group_ids"], maximum=200), key=int))
        if "caption_template" in payload:
            pool["caption_template"] = validate_template(payload["caption_template"])
        if not json.loads(pool["group_ids_json"]):
            raise DomainError("schedule_incomplete", "请先选择Page组", 409)
        facts = self.candidates(actor, pool, seed=[actor["tenant_key"], pool["schedule_revision"], "preview"])
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            return self.match(conn, actor, pool, facts, day=self.store.now_fn().astimezone(BJ).date().isoformat())

    def runs(self, actor, query):
        limit, cursor = self._pagination(query)
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT * FROM fb_group_pool_run WHERE tenant_key=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
                                (actor["tenant_key"], cursor, cursor, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                visible = actor["is_admin"] or actor["user_id"] == row["publisher_user_id"]
                counts = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM fb_video_task WHERE batch_id=? GROUP BY status", (row["batch_id"],))}
                status = row["status"]
                if counts:
                    status = "unknown" if "unknown" in counts else "completed" if set(counts) <= {"published", "failed", "cancelled"} else "processing"
                # Shared pool exposes aggregate progress, not another owner's frozen task content.
                items.append({"id": str(row["id"]), "slot_at_utc": row["slot_at_utc"], "status": status,
                    "reason": row["reason"], "max_item_id": row["max_item_id"], "task_counts": counts,
                    "pairs": json.loads(row["pairs_json"]) if visible else [], "excluded": json.loads(row["excluded_json"]) if visible else [],
                    "batch_id": row["batch_id"] if visible else "", "can_view_tasks": bool(visible)})
        return {"ok": True, "items": items, "has_more": len(rows) > limit,
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    def handle(self, method, suffix, actor, payload=None, query=None):
        actor, payload, query = actor_checked(actor), payload or {}, query or {}
        if method == "GET" and suffix == "":
            return self.listing(actor, query)
        if method == "GET" and suffix == "/schedule":
            with self.store.connect() as conn:
                pool = pool_row(conn, actor["tenant_key"])
                first_day = self.store.now_fn().astimezone(BJ).date().isoformat()
                if pool["effective_at_utc"]:
                    first_day = max(first_day, parse_time(pool["effective_at_utc"]).astimezone(BJ).date().isoformat())
                rows = conn.execute("SELECT * FROM fb_group_pool_day WHERE tenant_key=? AND schedule_revision=? AND bj_date>=? ORDER BY bj_date LIMIT 2",
                                    (actor["tenant_key"], pool["schedule_revision"], first_day)).fetchall()
            return {"ok": True, "pool": pool_dto(pool), "enabled": self.enabled, "feature_enabled": self.enabled,
                    "day_plan": [{"bj_date": r["bj_date"], "times": json.loads(r["times_json"]), "mode": r["mode"]} for r in rows]}
        if method == "GET" and suffix == "/runs":
            return self.runs(actor, query)
        if method == "GET" and suffix.startswith("/operations/"):
            with self.store.connect() as conn:
                prior = self._prior(conn, actor, suffix.rsplit("/", 1)[1])
            if prior is None:
                raise DomainError("operation_not_found", "操作尚未提交或不属于当前用户", 404)
            return prior
        if method == "POST" and suffix == "/items/resolve":
            self._keys(payload, {"material_ids"})
            return {"ok": True, **self.sources.resolve_materials(ids(payload["material_ids"]), need_desc=False)}
        if method == "POST" and suffix == "/preview":
            return self.preview(actor, payload)
        if method == "POST" and suffix in {"/items", "/items/remove", "/schedule", "/enable", "/pause"}:
            if not self.enabled:
                raise DomainError("group_pool_gate_closed", "组素材池写入开关未开启", 409)
            return self._mutate(actor, suffix, payload)
        raise DomainError("not_found", "接口不存在", 404)

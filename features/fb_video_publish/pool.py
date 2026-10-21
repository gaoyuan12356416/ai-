"""Tenant-shared Page queues. External lookups never run under a write lock."""
from __future__ import annotations

import json
import re
import uuid

from .core import UTC, DomainError, actor_checked, canonical, digest, id_text, stamp
from .guards import automatic_block, manual_block, published_history
from .templates import TemplateError, render_preview

IDENTITY = {"config_instance_id", "expected_version"}
ITEM_STATES = ("pending", "processing", "published", "failed", "cancelled", "removed")


def install_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_video_pool(
          config_instance_id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,page_id TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'paused',
          times_json TEXT NOT NULL DEFAULT '[]',processing_mode TEXT NOT NULL DEFAULT 'original',
          publisher_user_id TEXT NOT NULL DEFAULT '',publisher_name TEXT NOT NULL DEFAULT '',
          pause_reason TEXT NOT NULL DEFAULT '',effective_at_utc TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_fb_video_pool_enabled ON fb_video_pool(status,tenant_key,page_id);
        CREATE TABLE IF NOT EXISTS fb_video_pool_item(
          id INTEGER PRIMARY KEY AUTOINCREMENT,config_instance_id TEXT NOT NULL,
          tenant_key TEXT NOT NULL,page_id TEXT NOT NULL,material_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',position INTEGER NOT NULL,
          material_name TEXT NOT NULL,drama_name TEXT NOT NULL,language TEXT NOT NULL,
          material_json TEXT NOT NULL,repeat_history_json TEXT NOT NULL,
          added_by TEXT NOT NULL,task_id TEXT NOT NULL DEFAULT '',batch_id TEXT NOT NULL DEFAULT '',
          error_message TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fb_video_pool_pending_material
          ON fb_video_pool_item(config_instance_id,material_id) WHERE status IN ('pending','processing');
        CREATE INDEX IF NOT EXISTS idx_fb_video_pool_order
          ON fb_video_pool_item(config_instance_id,status,position,id);
        CREATE TABLE IF NOT EXISTS fb_video_pool_slot(
          id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,page_id TEXT NOT NULL,
          config_instance_id TEXT NOT NULL,bj_date TEXT NOT NULL,slot_time TEXT NOT NULL,
          slot_at_utc TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
          item_id INTEGER,material_id TEXT NOT NULL DEFAULT '',task_id TEXT NOT NULL DEFAULT '',
          batch_id TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,
          UNIQUE(tenant_key,page_id,bj_date,slot_time));
        CREATE INDEX IF NOT EXISTS idx_fb_video_pool_slot_list
          ON fb_video_pool_slot(tenant_key,page_id,id);
        CREATE TABLE IF NOT EXISTS fb_video_pool_operation(
          tenant_key TEXT NOT NULL,owner_user_id TEXT NOT NULL,operation_id TEXT NOT NULL,
          page_id TEXT NOT NULL,config_instance_id TEXT NOT NULL,request_hash TEXT NOT NULL,
          response_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,
          PRIMARY KEY(tenant_key,owner_user_id,operation_id));
        CREATE INDEX IF NOT EXISTS idx_fb_video_pool_task_sequence
          ON fb_video_task(pool_instance_id,pool_sequence,status);
        CREATE INDEX IF NOT EXISTS idx_fb_video_pool_page_sequence
          ON fb_video_task(tenant_key,page_id,source_kind,pool_sequence,status);
    """)


def current_config(conn, tenant, pid, instance=None):
    row = conn.execute("SELECT * FROM fb_video_page WHERE tenant_key=? AND page_id=? AND deleted=0",
                       (tenant, pid)).fetchone()
    if row is None:
        raise DomainError("page_not_found", "Page配置不存在", 404)
    if instance is not None and row["config_instance_id"] != instance:
        raise DomainError("config_version_conflict", "Page已删除重建，请重新打开素材池", 409)
    return dict(row)


def pool_row(conn, config):
    row = conn.execute("SELECT * FROM fb_video_pool WHERE config_instance_id=?", (config["config_instance_id"],)).fetchone()
    if row:
        return dict(row)
    return {"config_instance_id": config["config_instance_id"], "tenant_key": config["tenant_key"],
            "page_id": config["page_id"], "version": 0, "status": "paused", "times_json": "[]",
            "processing_mode": "original", "publisher_user_id": "", "publisher_name": "", "pause_reason": "",
            "effective_at_utc": "", "created_at_utc": "", "updated_at_utc": ""}


def pool_dto(pool):
    return {key: pool[key] for key in ("config_instance_id", "version", "status", "processing_mode",
                                      "publisher_user_id", "publisher_name", "pause_reason")} | {
        "times": json.loads(pool["times_json"])}


def sync_items(conn, instance):
    """Derive finished pool entries from the durable task, never the reverse."""
    conn.execute("""UPDATE fb_video_pool_item SET
        status=(SELECT status FROM fb_video_task WHERE task_id=fb_video_pool_item.task_id),
        error_message=(SELECT error_message FROM fb_video_task WHERE task_id=fb_video_pool_item.task_id),
        updated_at_utc=(SELECT updated_at_utc FROM fb_video_task WHERE task_id=fb_video_pool_item.task_id)
        WHERE config_instance_id=? AND status='processing' AND EXISTS(
          SELECT 1 FROM fb_video_task t WHERE t.task_id=fb_video_pool_item.task_id
          AND t.status IN ('published','failed','cancelled'))""", (instance,))


def normalized_ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise DomainError("invalid_request", "每次请选择1至200项")
    ids = [id_text(v) for v in values]
    if len(set(ids)) != len(ids):
        raise DomainError("invalid_request", "选择项不得重复")
    return ids


class PoolService:
    def __init__(self, service):
        self.service, self.store, self.sources = service, service.store, service.sources
        self.identity_resolver = None

    def _effective_now(self):
        # Do not round a change at HH:mm:00.500 back into that minute's slot.
        return self.store.now_fn().astimezone(UTC).isoformat(timespec="microseconds")

    def _access(self, actor, pid):
        self.service._config_access(actor, [pid])

    def _identity(self, payload, extra=()):
        self.service._keys(payload, IDENTITY | set(extra))
        if (not re.fullmatch(r"[a-f0-9]{32}", str(payload["config_instance_id"]))
                or type(payload["expected_version"]) is not int or payload["expected_version"] < 0):
            raise DomainError("invalid_request", "素材池版本无效")

    def _locked(self, conn, actor, pid, payload):
        config = current_config(conn, actor["tenant_key"], pid, payload["config_instance_id"])
        pool = pool_row(conn, config)
        if pool["version"] != payload["expected_version"]:
            raise DomainError("pool_version_conflict", "素材池已改变，请刷新后重试", 409)
        if pool["status"] == "archived":
            raise DomainError("pool_archived", "素材池已归档", 409)
        return config, pool

    def _ensure(self, conn, config):
        now = stamp(self.store.now_fn())
        conn.execute("""INSERT OR IGNORE INTO fb_video_pool(config_instance_id,tenant_key,page_id,
            effective_at_utc,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?)""",
                     (config["config_instance_id"], config["tenant_key"], config["page_id"], self._effective_now(), now, now))

    def _bump(self, conn, instance):
        conn.execute("UPDATE fb_video_pool SET version=version+1,updated_at_utc=? WHERE config_instance_id=?",
                     (stamp(self.store.now_fn()), instance))
        return pool_dto(dict(conn.execute("SELECT * FROM fb_video_pool WHERE config_instance_id=?", (instance,)).fetchone()))

    @staticmethod
    def _effective_status(row):
        return row["task_status"] if row["task_status"] in {"published", "failed", "cancelled"} else row["status"]

    def _item(self, row, actor, config):
        status = self._effective_status(row)
        can_view = bool(row["task_id"]) and (actor["is_admin"] or row["task_owner"] == actor["user_id"])
        error = row["task_error"] or row["error_message"]
        if row["task_id"] and not can_view:
            error = ("发布失败，请由发布人查看任务详情" if status == "failed"
                     else "发布结果待核对" if row["task_status"] == "unknown" else "")
        return {"id": str(row["id"]), "material_id": row["material_id"], "material_name": row["material_name"],
                "drama_name": row["drama_name"], "language": row["language"], "status": status,
                "position": row["position"], "created_at_utc": row["created_at_utc"],
                "task_id": row["task_id"] if can_view else "", "batch_id": row["batch_id"] if can_view else "",
                "error_message": error,
                "language_mismatch": row["language"] != config["language"], "can_view_task": can_view,
                "can_modify": row["status"] == "pending" and not row["task_id"]}

    def listing(self, actor, pid, query):
        state = query.get("state", "pending")
        if state not in {"pending", "processing", "published", "failed", "slots", "cancelled", "removed"}:
            raise DomainError("invalid_request", "素材池筛选状态无效")
        try:
            limit = int(query.get("limit", 50))
            cursor = str(query.get("cursor") or "")
            if not 1 <= limit <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            raise DomainError("invalid_request", "分页参数无效") from None
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            config = current_config(conn, actor["tenant_key"], pid)
            pool = pool_row(conn, config)
            instance = config["config_instance_id"]
            status_expr = "CASE WHEN t.status IN ('published','failed','cancelled') THEN t.status ELSE i.status END"
            base = """ FROM fb_video_pool_item i LEFT JOIN fb_video_task t ON t.task_id=i.task_id
                       WHERE i.config_instance_id=?"""
            counts = {k: 0 for k in (*ITEM_STATES, "slots")}
            for r in conn.execute("SELECT " + status_expr + ",COUNT(*)" + base + " GROUP BY 1", (instance,)):
                counts[r[0]] = r[1]
            counts["slots"] = conn.execute("SELECT COUNT(*) FROM fb_video_pool_slot WHERE tenant_key=? AND page_id=?",
                                          (actor["tenant_key"], pid)).fetchone()[0]
            if state == "slots":
                if cursor and not re.fullmatch(r"[1-9][0-9]{0,18}", cursor):
                    raise DomainError("invalid_cursor", "分页标识无效")
                rows = conn.execute("""SELECT s.*,t.owner_user_id FROM fb_video_pool_slot s
                    LEFT JOIN fb_video_task t ON t.task_id=s.task_id WHERE s.tenant_key=? AND s.page_id=?
                    AND (?='' OR s.id<?) ORDER BY s.id DESC LIMIT ?""",
                    (actor["tenant_key"], pid, cursor, int(cursor or 0), limit + 1)).fetchall()
                items = []
                for row in rows[:limit]:
                    can_view = bool(row["task_id"]) and (actor["is_admin"] or row["owner_user_id"] == actor["user_id"])
                    items.append({"id": str(row["id"]), "slot_at_utc": row["slot_at_utc"], "status": row["status"],
                                  "reason": row["reason"], "material_id": row["material_id"],
                                  "task_id": row["task_id"] if can_view else "", "can_view_task": can_view})
                next_cursor = str(rows[limit - 1]["id"]) if len(rows) > limit else None
            else:
                extra, params = "", [instance]
                wanted = ("failed", "cancelled", "removed") if state == "failed" else (state,)
                extra += " AND (" + status_expr + ") IN (" + ",".join("?" for _ in wanted) + ")"
                params += list(wanted)
                if cursor:
                    match = re.fullmatch(r"(-?[0-9]+):([1-9][0-9]*):([0-9]+)", cursor)
                    if not match or int(match[3]) != pool["version"]:
                        raise DomainError("pool_version_conflict", "队列顺序已改变，请刷新", 409)
                    extra += " AND (i.position>? OR (i.position=? AND i.id>?))"
                    params += [int(match[1]), int(match[1]), int(match[2])]
                rows = conn.execute("""SELECT i.*,t.status AS task_status,t.owner_user_id AS task_owner,
                    t.error_message AS task_error""" + base + extra + " ORDER BY i.position,i.id LIMIT ?",
                    [*params, limit + 1]).fetchall()
                items = [self._item(row, actor, config) for row in rows[:limit]]
                next_cursor = (str(rows[limit - 1]["position"]) + ":" + str(rows[limit - 1]["id"]) + ":" + str(pool["version"])) if len(rows) > limit else None
        return {"ok": True, "pool": pool_dto(pool), "items": items, "counts": counts,
                "next_cursor": next_cursor, "has_more": len(rows) > limit}

    def settings(self, actor, pid, payload):
        self._identity(payload, ("times", "processing_mode"))
        times = payload["times"]
        if (not isinstance(times, list) or not 1 <= len(times) <= 1440
                or any(not isinstance(v, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", v) for v in times)
                or len(set(times)) != len(times)):
            raise DomainError("invalid_times", "每日时间点至少一个，格式HH:mm且不得重复")
        if payload["processing_mode"] not in {"original", "random_overlay"}:
            raise DomainError("invalid_request", "加工模式无效")
        if payload["processing_mode"] == "random_overlay" and not self.service.prepare_enabled:
            raise DomainError("prepare_gate_closed", "视频加工服务未启用", 409)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config, pool = self._locked(conn, actor, pid, payload)
            self._ensure(conn, config)
            conn.execute("""UPDATE fb_video_pool SET times_json=?,processing_mode=?,effective_at_utc=?
                WHERE config_instance_id=?""", (canonical(sorted(times)), payload["processing_mode"],
                                               self._effective_now(), config["config_instance_id"]))
            result = self._bump(conn, config["config_instance_id"])
            conn.commit()
        return {"ok": True, "pool": result}

    def toggle(self, actor, pid, payload, enable):
        self._identity(payload)
        if enable:
            if self.identity_resolver:
                fresh = actor_checked(self.identity_resolver(actor["tenant_key"], actor["user_id"]))
                if (fresh["tenant_key"], fresh["user_id"]) != (actor["tenant_key"], actor["user_id"]):
                    raise DomainError("invalid_actor", "启用人身份解析不匹配", 403)
                actor = fresh
            if not self.service.live_enabled:
                raise DomainError("manual_live_gate_closed", "发布开关关闭", 409)
            if not re.fullmatch(r"[1-9][0-9]{0,30}", actor["sub_user_id"]):
                raise DomainError("actor_mapping_missing", "启用人缺少唯一有效归因身份", 403)
            self.sources.get_pages(actor, [pid])
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config, pool = self._locked(conn, actor, pid, payload)
            if enable:
                if not json.loads(pool["times_json"]):
                    raise DomainError("pool_times_required", "请先设置每日时间点", 409)
                if pool["processing_mode"] == "random_overlay" and not self.service.prepare_enabled:
                    raise DomainError("prepare_gate_closed", "视频加工服务未启用", 409)
                if conn.execute("""SELECT 1 FROM fb_video_pool_item WHERE config_instance_id=?
                    AND status='pending' AND language<>? LIMIT 1""", (config["config_instance_id"], config["language"])).fetchone():
                    raise DomainError("pool_language_mismatch", "请先移除剧语言不匹配的待发布素材", 409)
                reason = automatic_block(conn, pid) or manual_block(conn, pid)
                if reason:
                    raise DomainError(reason, "Page存在结果不明的任务，请先核对", 409)
            self._ensure(conn, config)
            now = stamp(self.store.now_fn())
            conn.execute("""UPDATE fb_video_pool SET status=?,pause_reason=?,effective_at_utc=?,
                publisher_user_id=?,publisher_name=? WHERE config_instance_id=?""",
                ("enabled" if enable else "paused", "" if enable else "operator_paused", self._effective_now(),
                 actor["user_id"] if enable else pool["publisher_user_id"],
                 actor["user_name"] if enable else pool["publisher_name"], config["config_instance_id"]))
            result = self._bump(conn, config["config_instance_id"])
            conn.commit()
        return {"ok": True, "pool": result}

    def _material_history(self, conn, pid, config, material):
        mid = material["material_id"]
        if material["language"] != config["language"]:
            raise DomainError("language_mismatch", "素材与Page剧语言不一致", 409)
        row = conn.execute("""SELECT 1 FROM fb_video_pool_item i LEFT JOIN fb_video_task t ON t.task_id=i.task_id
                WHERE i.config_instance_id=? AND i.material_id=? AND (i.status='pending' OR
                  (i.status='processing' AND COALESCE(t.status,'') NOT IN ('published','failed','cancelled'))) LIMIT 1""",
            (config["config_instance_id"], mid)).fetchone()
        if row:
            raise DomainError("pool_material_exists", "素材已在待发布或处理中队列", 409)
        reason = automatic_block(conn, pid, mid) or manual_block(conn, pid, mid)
        if reason:
            raise DomainError(reason, "Page或素材存在未结束/结果不明任务", 409)
        render_preview(config["caption_template"], material)
        return published_history(conn, pid, mid)

    def _facts(self, conn, actor, pid, payload, materials):
        config, pool = self._locked(conn, actor, pid, payload)
        histories = {mid: self._material_history(conn, pid, config, materials[mid]) for mid in payload["material_ids"]}
        facts = {"purpose": "pool-admission", "actor": actor, "page_id": pid,
                 "payload": payload, "config": config, "pool": pool, "materials": materials, "histories": histories}
        return facts, config, pool, histories

    def preview(self, actor, pid, payload):
        self._identity(payload, ("material_ids",))
        payload = {**payload, "material_ids": normalized_ids(payload["material_ids"])}
        materials = self.sources.get_materials(payload["material_ids"], need_desc=False)
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            facts, config, _, histories = self._facts(conn, actor, pid, payload, materials)
        items = [{"material_id": mid, "material_name": materials[mid]["material_name"],
                  "drama_name": materials[mid]["drama_name"], "language": materials[mid]["language"],
                  "repeat_required": bool(histories[mid])} for mid in payload["material_ids"]]
        return {"ok": True, "preview_token": self.service._token(facts), "items": items,
                "repeat_required": any(histories.values())}

    def resolve(self, actor, pid, payload):
        """Return an ordered valid subset and a strict admission proof for it."""
        self._identity(payload, ("material_ids",))
        values = payload["material_ids"]
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise DomainError("invalid_request", "每次请输入1至200个素材ID")
        entries, wanted, skipped = [], [], []
        for row_number, value in enumerate(values, 1):
            mid = value.strip() if isinstance(value, str) else str(value)
            if not mid:
                continue
            entry = {"row_number": row_number, "material_id": mid[:128]}
            entries.append(entry)
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,18}", mid):
                skipped.append({**entry, "code": "invalid_material_id", "message": "素材ID必须为不超过19位的正整数"})
            elif mid not in wanted:
                wanted.append(mid)
        resolved = self.sources.resolve_materials(wanted, need_desc=False) if wanted else {"items": {}, "errors": {}}
        materials, failures = resolved["items"], resolved["errors"]
        invalid_rows = {item["row_number"] for item in skipped}
        accepted, histories, items = {}, {}, []
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            config, pool = self._locked(conn, actor, pid, payload)
            for entry in entries:
                if entry["row_number"] in invalid_rows:
                    continue
                mid = entry["material_id"]
                if mid in accepted:
                    skipped.append({**entry, "code": "duplicate_material_id", "message": "重复素材ID，已保留第一次有效输入"})
                    continue
                if mid in failures:
                    skipped.append({**entry, **failures[mid]})
                    continue
                if mid not in materials:
                    raise DomainError("material_source_incomplete", "素材来源结果不完整，请重试", 503)
                try:
                    history = self._material_history(conn, pid, config, materials[mid])
                except (DomainError, TemplateError) as exc:
                    if exc.status >= 500:
                        raise
                    skipped.append({**entry, "code": exc.code, "message": str(exc)})
                    continue
                accepted[mid], histories[mid] = materials[mid], history
                items.append({**entry, "material_name": materials[mid]["material_name"],
                              "drama_name": materials[mid]["drama_name"], "language": materials[mid]["language"],
                              "repeat_required": bool(history)})
            clean = {**{key: payload[key] for key in IDENTITY}, "material_ids": list(accepted)}
            facts = {"purpose": "pool-admission", "actor": actor, "page_id": pid, "payload": clean,
                     "config": config, "pool": pool, "materials": accepted, "histories": histories}
        skipped.sort(key=lambda item: item["row_number"])
        return {"ok": True, "material_ids": list(accepted), "items": items, "skipped": skipped,
                "summary": {"total": len(entries), "valid": len(items), "skipped": len(skipped)},
                "preview_token": self.service._token(facts) if items else "",
                "repeat_required": any(histories.values())}

    def existing(self, actor, pid, operation_id, request_hash=None):
        with self.store.connect() as conn:
            row = conn.execute("""SELECT * FROM fb_video_pool_operation
                WHERE tenant_key=? AND owner_user_id=? AND operation_id=? AND page_id=?""",
                (actor["tenant_key"], actor["user_id"], operation_id, pid)).fetchone()
        if row is None:
            return None
        if request_hash is not None and row["request_hash"] != request_hash:
            raise DomainError("operation_conflict", "同一操作标识不可用于不同入池内容", 409)
        return {**json.loads(row["response_json"]), "idempotent": True}

    def add(self, actor, pid, payload):
        self._identity(payload, ("material_ids", "operation_id", "preview_token", "allow_repeat"))
        if not re.fullmatch(r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", str(payload["operation_id"])):
            raise DomainError("invalid_request", "操作标识无效")
        if type(payload["allow_repeat"]) is not bool:
            raise DomainError("invalid_request", "重复发布确认无效")
        clean = {key: payload[key] for key in IDENTITY}
        clean["material_ids"] = normalized_ids(payload["material_ids"])
        reqhash = digest({"page_id": pid, **clean, "allow_repeat": payload["allow_repeat"]})
        existing = self.existing(actor, pid, payload["operation_id"], reqhash)
        if existing:
            return existing
        if not self.service.pool_enabled:
            raise DomainError("pool_gate_closed", "素材池暂时关闭", 409)
        signed = self.service._verify(payload["preview_token"])
        materials = self.sources.get_materials(clean["material_ids"], need_desc=False)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""SELECT * FROM fb_video_pool_operation
                WHERE tenant_key=? AND owner_user_id=? AND operation_id=?""",
                (actor["tenant_key"], actor["user_id"], payload["operation_id"])).fetchone()
            if existing:
                if existing["request_hash"] != reqhash:
                    raise DomainError("operation_conflict", "同一操作标识不可用于不同入池内容", 409)
                return {**json.loads(existing["response_json"]), "idempotent": True}
            facts, config, _, histories = self._facts(conn, actor, pid, clean, materials)
            if digest(facts) != signed:
                raise DomainError("preview_changed", "配置、素材或重复历史已改变，请重新预览", 409)
            if any(histories.values()) and not payload["allow_repeat"]:
                raise DomainError("repeat_confirmation_required", "请确认已成功素材再次发布", 409)
            self._ensure(conn, config)
            instance = config["config_instance_id"]
            sync_items(conn, instance)
            position = conn.execute("SELECT COALESCE(MAX(position),0) FROM fb_video_pool_item WHERE config_instance_id=?", (instance,)).fetchone()[0]
            now, items = stamp(self.store.now_fn()), []
            for mid in clean["material_ids"]:
                position += 1
                material = materials[mid]
                item_id = conn.execute("""INSERT INTO fb_video_pool_item(config_instance_id,tenant_key,page_id,material_id,
                    position,material_name,drama_name,language,material_json,repeat_history_json,added_by,created_at_utc,updated_at_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (instance, actor["tenant_key"], pid, mid, position, material["material_name"], material["drama_name"],
                     material["language"], canonical(material), canonical(histories[mid]), actor["user_id"], now, now)).lastrowid
                items.append({"id": str(item_id), "material_id": mid, "status": "pending", "position": position})
            result = {"ok": True, "pool": self._bump(conn, instance), "items": items, "idempotent": False}
            conn.execute("INSERT INTO fb_video_pool_operation VALUES(?,?,?,?,?,?,?,?)",
                         (actor["tenant_key"], actor["user_id"], payload["operation_id"], pid, instance, reqhash, canonical(result), now))
            conn.commit()
            return result

    def reorder_or_remove(self, actor, pid, payload, remove):
        self._identity(payload, ("item_ids",) if remove else ("item_id", "direction"))
        ids = normalized_ids(payload["item_ids"]) if remove else [id_text(payload["item_id"])]
        if not remove and payload["direction"] not in {"top", "up", "down"}:
            raise DomainError("invalid_request", "移动方向无效")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config, _ = self._locked(conn, actor, pid, payload)
            instance = config["config_instance_id"]
            rows = conn.execute("""SELECT * FROM fb_video_pool_item WHERE config_instance_id=?
                AND id IN (""" + ",".join("?" for _ in ids) + ")", [instance, *ids]).fetchall()
            if len(rows) != len(ids) or any(r["status"] != "pending" or r["task_id"] for r in rows):
                raise DomainError("pool_item_bound", "只能移除或移动尚未绑定任务的素材", 409)
            now = stamp(self.store.now_fn())
            if remove:
                conn.execute("""UPDATE fb_video_pool_item SET status='removed',updated_at_utc=?
                    WHERE config_instance_id=? AND id IN (""" + ",".join("?" for _ in ids) + ")", [now, instance, *ids])
            else:
                row = rows[0]
                direction = payload["direction"]
                if direction == "top":
                    position = conn.execute("SELECT MIN(position) FROM fb_video_pool_item WHERE config_instance_id=? AND status='pending'", (instance,)).fetchone()[0] - 1
                    conn.execute("UPDATE fb_video_pool_item SET position=?,updated_at_utc=? WHERE id=?", (position, now, row["id"]))
                else:
                    operator, ordering = ("<", "DESC") if direction == "up" else (">", "ASC")
                    other = conn.execute("SELECT id,position FROM fb_video_pool_item WHERE config_instance_id=? AND status='pending' AND position "
                                         + operator + "? ORDER BY position " + ordering + ",id " + ordering + " LIMIT 1",
                                         (instance, row["position"])).fetchone()
                    if other:
                        conn.execute("UPDATE fb_video_pool_item SET position=?,updated_at_utc=? WHERE id=?", (other["position"], now, row["id"]))
                        conn.execute("UPDATE fb_video_pool_item SET position=?,updated_at_utc=? WHERE id=?", (row["position"], now, other["id"]))
            result = self._bump(conn, instance)
            conn.commit()
        return {"ok": True, "pool": result}

    def handle(self, method, pid, suffix, actor, payload, query):
        self._access(actor, pid)
        if method == "GET":
            if suffix == "":
                return self.listing(actor, pid, query)
            match = re.fullmatch(r"/operations/([a-f0-9-]{32,36})", suffix)
            if match:
                result = self.existing(actor, pid, match[1])
                if result is None:
                    raise DomainError("operation_not_found", "尚未查询到入池操作，请保留原操作标识重试", 404)
                return result
        if method == "POST":
            # Accepted operations remain recoverable even when new admission closes.
            if suffix == "/items":
                return self.add(actor, pid, payload)
            if not self.service.pool_enabled:
                raise DomainError("pool_gate_closed", "素材池暂时关闭", 409)
            if suffix == "/settings":
                return self.settings(actor, pid, payload)
            if suffix in {"/enable", "/pause"}:
                return self.toggle(actor, pid, payload, suffix == "/enable")
            if suffix == "/items/preview":
                return self.preview(actor, pid, payload)
            if suffix == "/items/resolve":
                return self.resolve(actor, pid, payload)
            if suffix in {"/remove", "/move"}:
                return self.reorder_or_remove(actor, pid, payload, suffix == "/remove")
        raise DomainError("not_found", "素材池接口不存在", 404)

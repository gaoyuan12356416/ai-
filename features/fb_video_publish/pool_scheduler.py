"""Daily Page pool slots; no Graph calls and no recovery of missed slots."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import timedelta

from .core import BJ, DomainError, actor_checked, canonical, normalize_batch, parse_time, stamp
from .guards import automatic_block, manual_block, published_history
from .pool import current_config, sync_items
from .templates import render_preview


class PoolScheduler:
    def __init__(self, service, identity_resolver, *, now_fn=None, live_enabled=True):
        self.service, self.store, self.sources = service, service.store, service.sources
        self.identity_resolver, self.live_enabled = identity_resolver, bool(live_enabled)
        self.now_fn = now_fn or self.store.now_fn
        self.worker_id, self.started_at = "", None
        self._lock = threading.Lock()

    @staticmethod
    def _worker(worker_id):
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", worker_id):
            raise DomainError("invalid_request", "调度器标识无效")

    def start(self, worker_id):
        self._worker(worker_id)
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "status": "started" if self.worker_id == worker_id else "not_started",
                    "started_at_utc": stamp(self.started_at) if self.started_at else ""}
        try:
            if self.worker_id != worker_id:
                self.worker_id, self.started_at = worker_id, self.now_fn()
            return {"ok": True, "status": "started", "started_at_utc": stamp(self.started_at)}
        finally:
            self._lock.release()

    def _candidates(self, now):
        local = now.astimezone(BJ)
        with self.store.connect() as conn:
            pools = conn.execute("""SELECT p.* FROM fb_video_pool p JOIN fb_video_page c
                ON c.config_instance_id=p.config_instance_id AND c.tenant_key=p.tenant_key AND c.page_id=p.page_id
                WHERE p.status='enabled' AND c.deleted=0""").fetchall()
            result = []
            for row in pools:
                pool = dict(row)
                for value in json.loads(pool["times_json"]):
                    hour, minute = map(int, value.split(":"))
                    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    # Half-open window: an old minute is never replayed at +60s.
                    if (at < self.started_at or at < parse_time(pool["effective_at_utc"])
                            or not 0 <= (now - at).total_seconds() < 60):
                        continue
                    key = (pool["tenant_key"], pool["page_id"], at.date().isoformat(), value)
                    if conn.execute("""SELECT 1 FROM fb_video_pool_slot
                        WHERE tenant_key=? AND page_id=? AND bj_date=? AND slot_time=?""", key).fetchone():
                        continue
                    config = current_config(conn, pool["tenant_key"], pool["page_id"], pool["config_instance_id"])
                    item = conn.execute("""SELECT * FROM fb_video_pool_item WHERE config_instance_id=?
                        AND status='pending' AND task_id='' ORDER BY position,id LIMIT 1""",
                        (pool["config_instance_id"],)).fetchone()
                    result.append({"pool": pool, "config": config, "item": dict(item) if item else None,
                                   "at": at, "key": key})
        return sorted(result, key=lambda x: (x["at"], x["pool"]["tenant_key"], x["pool"]["page_id"]))

    def _resolve(self, candidate):
        pool, item = candidate["pool"], candidate["item"]
        actor = actor_checked(self.identity_resolver(pool["tenant_key"], pool["publisher_user_id"]))
        if (actor["tenant_key"], actor["user_id"]) != (pool["tenant_key"], pool["publisher_user_id"]):
            raise DomainError("identity_inactive", "计划发布人身份不匹配", 403)
        if not re.fullmatch(r"[1-9][0-9]{0,30}", actor["sub_user_id"]):
            raise DomainError("identity_attribution_invalid", "计划发布人缺少唯一有效归因身份", 403)
        pages = self.sources.get_pages(actor, [pool["page_id"]])
        if not item:
            return actor, {"pages": pages, "materials": {}}
        materials = self.sources.get_materials([item["material_id"]], need_desc=False)
        if materials[item["material_id"]]["language"] != candidate["config"]["language"]:
            raise DomainError("language_mismatch", "素材与当前Page剧语言不一致", 409)
        render_preview(candidate["config"]["caption_template"], materials[item["material_id"]])
        return actor, {"pages": pages, "materials": materials}

    @staticmethod
    def _failure(exc):
        code = str(getattr(exc, "code", "pool_source_unavailable"))
        status = getattr(exc, "status", 503)
        if status == 403 or code in {
            "page_not_found", "page_unavailable", "page_blocked", "page_identity_ambiguous",
            "fb_video_page_unknown", "identity_inactive", "identity_permission_denied", "identity_attribution_invalid",
        }:
            return "blocked", code, True, False
        if code in {"daily_capacity_exceeded", "fb_video_material_active", "fb_video_page_busy", "prepare_gate_closed"}:
            return "skipped", code, False, False
        if status >= 500:
            return "skipped", code, False, False
        # Only explicit permanent material/template/confirmation errors consume.
        permanent = {"material_unavailable", "material_metadata_ambiguous", "language_mismatch",
                     "repeat_confirmation_required", "pool_repeat_changed", "caption_too_long",
                     "macro_source_missing", "invalid_template", "invalid_macro", "invalid_caption",
                     "template_too_long", "text_too_long", "unknown_macro", "required_text",
                     "short_link_context_invalid", "invalid_attribution", "attribution_invalid"}
        return ("failed", code, False, True) if code in permanent else ("skipped", code, False, False)

    def _finish(self, candidate, actor, resolved, error):
        pool, config, item, at = (candidate[k] for k in ("pool", "config", "item", "at"))
        now = stamp(self.now_fn())
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute("""INSERT OR IGNORE INTO fb_video_pool_slot(tenant_key,page_id,
                config_instance_id,bj_date,slot_time,slot_at_utc,status,created_at_utc) VALUES(?,?,?,?,?,?,'claimed',?)""",
                (pool["tenant_key"], pool["page_id"], pool["config_instance_id"],
                 candidate["key"][2], candidate["key"][3], stamp(at), now))
            if not inserted.rowcount:
                return {"status": "already_recorded"}
            slot_id = inserted.lastrowid
            current = conn.execute("SELECT * FROM fb_video_pool WHERE config_instance_id=?", (pool["config_instance_id"],)).fetchone()
            fresh_config = conn.execute("""SELECT * FROM fb_video_page
                WHERE tenant_key=? AND page_id=? AND deleted=0""", (pool["tenant_key"], pool["page_id"])).fetchone()
            stale = (not current or current["status"] != "enabled" or current["version"] != pool["version"]
                     or not fresh_config or fresh_config["config_instance_id"] != config["config_instance_id"]
                     or fresh_config["version"] != config["version"])
            late = not 0 <= (self.now_fn() - at).total_seconds() < 60
            if stale or late:
                conn.execute("UPDATE fb_video_pool_slot SET status='skipped',reason=? WHERE id=?",
                             ("configuration_changed" if stale else "slot_expired", slot_id))
                conn.commit()
                return {"status": "skipped"}
            current_item = conn.execute("""SELECT * FROM fb_video_pool_item WHERE config_instance_id=?
                AND status='pending' AND task_id='' ORDER BY position,id LIMIT 1""", (pool["config_instance_id"],)).fetchone()
            if (current_item["id"] if current_item else None) != (item["id"] if item else None):
                conn.execute("UPDATE fb_video_pool_slot SET status='skipped',reason='queue_changed' WHERE id=?", (slot_id,))
                conn.commit()
                return {"status": "skipped"}
            reason = automatic_block(conn, pool["page_id"]) or manual_block(conn, pool["page_id"])
            if reason:
                error = DomainError(reason, "Page存在结果不明的任务，请先核对", 409)
            if error is None and not item:
                conn.execute("UPDATE fb_video_pool_slot SET status='empty',reason='pool_empty' WHERE id=?", (slot_id,))
                conn.commit()
                return {"status": "empty"}
            if item:
                conn.execute("UPDATE fb_video_pool_slot SET item_id=?,material_id=? WHERE id=?",
                             (item["id"], item["material_id"], slot_id))
            if error is None:
                conn.execute("SAVEPOINT freeze_pool_task")
                try:
                    mid, pid = item["material_id"], pool["page_id"]
                    history = published_history(conn, pid, mid)
                    if history != json.loads(item["repeat_history_json"]):
                        raise DomainError("pool_repeat_changed", "入池后出现新的成功记录，请重新确认入池", 409)
                    if pool["processing_mode"] == "random_overlay" and not self.service.prepare_enabled:
                        raise DomainError("prepare_gate_closed", "视频加工服务未启用", 409)
                    operation = uuid.uuid5(uuid.NAMESPACE_URL, "fb-pool-slot:" + canonical(candidate["key"])).hex
                    request = normalize_batch({
                        "schema_version": 2, "operation_id": operation, "material_ids": [mid], "page_ids": [pid],
                        "processing_mode": pool["processing_mode"], "material_schedules": {mid: None},
                        "pairs": [{"material_id": mid, "page_id": pid, "config_instance_id": config["config_instance_id"],
                                   "config_version": config["version"], "caption_template": config["caption_template"]}]})
                    configs, repeats = self.store.check_plan(conn, actor, request, resolved)
                    source = {"config_instance_id": config["config_instance_id"], "slot_id": slot_id,
                              "item_id": item["id"], "slot_at_utc": stamp(at)}
                    plans = [(1, request["pairs"][0], 1, at, at.astimezone(BJ).date().isoformat())]
                    result = self.store.freeze(conn, actor, request, resolved, configs, repeats, plans, pool=source)
                    task = result["batch"]["items"][0]
                    conn.execute("""UPDATE fb_video_pool_item SET status='processing',task_id=?,batch_id=?,updated_at_utc=?
                        WHERE id=? AND status='pending' AND task_id=''""",
                        (task["task_id"], result["batch_id"], now, item["id"]))
                    conn.execute("UPDATE fb_video_pool_slot SET status='created',task_id=?,batch_id=? WHERE id=?",
                                 (task["task_id"], result["batch_id"], slot_id))
                    self.service.pool._bump(conn, pool["config_instance_id"])
                    conn.execute("RELEASE freeze_pool_task")
                    conn.commit()
                    return {"status": "created", "task_id": task["task_id"]}
                except Exception as exc:
                    conn.execute("ROLLBACK TO freeze_pool_task")
                    conn.execute("RELEASE freeze_pool_task")
                    # Unexpected SQL/logic failures roll the complete slot back.
                    if not hasattr(exc, "code"):
                        raise
                    error = exc
            status, reason, pause, consume = self._failure(error)
            conn.execute("UPDATE fb_video_pool_slot SET status=?,reason=? WHERE id=?", (status, reason, slot_id))
            if pause:
                conn.execute("""UPDATE fb_video_pool SET status='paused',pause_reason=?,version=version+1,updated_at_utc=?
                    WHERE config_instance_id=?""", (reason, now, pool["config_instance_id"]))
            if consume and item:
                conn.execute("""UPDATE fb_video_pool_item SET status='failed',error_message=?,updated_at_utc=?
                    WHERE id=? AND status='pending'""", (str(error)[:300], now, item["id"]))
                self.service.pool._bump(conn, pool["config_instance_id"])
            sync_items(conn, pool["config_instance_id"])
            conn.commit()
            return {"status": status, "reason": reason}

    def tick(self, worker_id):
        self._worker(worker_id)
        if not self.live_enabled or not self.service.live_enabled or not self.service.pool_enabled:
            return {"ok": True, "status": "pool_gate_closed", "slots_created": 0}
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "status": "tick_busy", "slots_created": 0}
        try:
            if not self.started_at or worker_id != self.worker_id:
                return {"ok": True, "status": "not_started", "slots_created": 0}
            candidates = self._candidates(self.now_fn())
            results = []
            started = time.monotonic()
            for candidate in candidates[:20]:
                actor, resolved, error = None, None, None
                if (self.now_fn() - candidate["at"]).total_seconds() >= 60:
                    error = DomainError("slot_expired", "本时点已错过，不补发", 503)
                elif time.monotonic() - started >= 30:
                    error = DomainError("scheduler_budget_exceeded", "本轮检查超时，本时点不补发", 503)
                else:
                    try:
                        actor, resolved = self._resolve(candidate)
                    except Exception as exc:
                        error = exc
                results.append(self._finish(candidate, actor, resolved, error))
            return {"ok": True, "status": "tick_complete", "slots_created": sum(r["status"] == "created" for r in results),
                    "slots_recorded": len(results), "results": results}
        finally:
            self._lock.release()

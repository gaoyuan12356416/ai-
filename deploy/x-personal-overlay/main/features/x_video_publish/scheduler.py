"""Freeze personal X pool slots into the normal manual runner, without posting.

There is deliberately no public 'run next slot' operation. Only slots after a
process startup and an explicit enable/settings change are eligible, for less
than one minute. Restart and missed slots never produce catch-up tasks.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from features.x_posts.service import XPostError
from .pool import BJ, canonical, language, parse_time, stamp, subject


class PoolScheduler:
    def __init__(self, pool, *, now_fn=None, enabled=True):
        self.pool, self.store = pool, pool.store
        self.now_fn, self.enabled = now_fn or pool.now_fn, bool(enabled)
        self.worker_id, self.started_at = "", None
        self._lock = threading.Lock()

    @staticmethod
    def _worker(worker_id):
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", worker_id):
            raise XPostError("invalid_worker_id", "调度器标识无效")

    def start(self, worker_id):
        self._worker(worker_id)
        with self._lock:
            if self.worker_id != worker_id:
                self.worker_id, self.started_at = worker_id, self.now_fn()
            return {"status": "started", "started_at_utc": stamp(self.started_at)}

    def _candidates(self, now):
        local = now.astimezone(BJ)
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM x_video_publish_pool WHERE status='enabled'").fetchall()
        # Status synchronization must precede selecting any next head. The core
        # derives outcomes from the durable runner/log, including unknown holds.
        for row in rows:
            actor = {"tenant_key": row["tenant_key"], "user_id": row["user_id"]}
            self.pool.sync_outcomes(actor, row["account_id"])
        results = []
        with self.pool.connection() as conn:
            for row in conn.execute("SELECT * FROM x_video_publish_pool WHERE status='enabled'"):
                pool = dict(row)
                owner = (pool["tenant_key"], pool["user_id"])
                for value in json.loads(pool["times_json"]):
                    hour, minute = map(int, value.split(":"))
                    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if (at < self.started_at or at < parse_time(pool["effective_at_utc"])
                            or not 0 <= (now - at).total_seconds() < 60):
                        continue
                    key = (*owner, pool["real_account_id"], at.date().isoformat(), value)
                    if conn.execute("SELECT 1 FROM x_video_publish_pool_slot WHERE tenant_key=? AND user_id=? AND real_account_id=? AND bj_date=? AND slot_time=?", key).fetchone():
                        continue
                    actor = {"tenant_key": owner[0], "user_id": owner[1]}
                    item = conn.execute("SELECT * FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='pending' AND task_id='' ORDER BY position,id LIMIT 1", (*owner, pool["account_id"])).fetchone()
                    config = self.pool._config_facts(actor, pool["account_id"], conn)
                    results.append({"pool": pool, "item": dict(item) if item else None, "at": at,
                                    "key": key, "actor": actor, "config_facts": config})
        return sorted(results, key=lambda item: (item["at"], item["key"]))

    def _resolve(self, candidate):
        actor = self.pool._fresh_actor(candidate["actor"])
        aid = str(candidate["pool"]["account_id"])
        account = self.pool._account(actor, aid)
        self.pool._usable(account)
        if str(account["x_user_id"]) != candidate["pool"]["real_account_id"]:
            raise XPostError("account_identity_changed", "账号身份已改变，计划已暂停", status=409)
        if language(account.get("drama_language") or account.get("language")) != candidate["pool"]["language"]:
            self.pool._sync_language(actor, aid, account)
            raise XPostError("account_language_changed", "账号剧语言已改变，计划已暂停", status=409)
        item = candidate["item"]
        if item is None:
            return actor, account, {}, None
        materials, errors = self.pool._materials(actor, [item["material_id"]])
        if item["material_id"] in errors:
            error = errors[item["material_id"]]
            raise XPostError(str(error.get("code") or "material_invalid"), str(error.get("message") or "素材不可用"), status=int(error.get("status") or 400))
        preview = self.store.preview(actor, {"source": "pool", "rows": [{"account_id": aid,
            "material_id": item["material_id"], "processing": candidate["pool"]["processing_mode"]}]},
            {aid: account}, materials, now=self.now_fn())
        valid = [row for row in preview["rows"] if row.get("valid") is True]
        if not valid:
            failure = preview["rows"][0]
            code = failure.get("error_code") or "material_invalid"
            raise XPostError(code, failure.get("error_message") or "素材未通过发布校验", status=409,
                             unknown_outcome=code == "x_post_unknown_outcome")
        return actor, account, materials, preview["preview_token"]

    @staticmethod
    def _failure(error):
        code, status = str(getattr(error, "code", "pool_source_unavailable")), getattr(error, "status", 503)
        if getattr(error, "unknown_outcome", False) or code in {"x_post_unknown_outcome", "pool_unknown_outcome"}:
            return "blocked", code, True, False
        if status in {401, 403} or code in {
            "identity_inactive", "identity_permission_denied", "account_not_found", "account_unavailable",
            "account_identity_changed", "account_language_changed", "account_identity_invalid",
            "x_video_account_not_found", "x_video_account_unavailable"}:
            return "blocked", code, True, False
        if status >= 500 or code in {
            "x_video_pair_active", "x_post_manual_material_unavailable", "pool_gate_closed",
            "prepare_gate_closed", "x_video_config_conflict", "x_video_material_changed", "slot_expired"}:
            return "skipped", code, False, False
        permanent = {"material_not_found", "material_unavailable", "material_invalid", "material_metadata_ambiguous",
            "x_video_material_not_found", "x_video_repeat_required", "x_post_account_language_mismatch",
            "language_mismatch", "invalid_template", "invalid_macro", "invalid_caption", "caption_too_long",
            "x_video_caption_invalid", "x_video_caption_empty", "x_video_caption_too_long", "x_video_macro_invalid",
            "x_video_caption_macro_invalid", "x_video_caption_source_missing", "x_post_copy_too_long",
            "x_post_manual_copy_invalid", "x_post_manual_copy_too_long", "x_post_manual_copy_empty"}
        return ("failed", code, False, True) if code in permanent or status in {400, 404, 422} else ("skipped", code, False, False)

    def _finish(self, candidate, actor, account, materials, token, error):
        pool, item, at = candidate["pool"], candidate["item"], candidate["at"]
        owner, aid = (pool["tenant_key"], pool["user_id"]), str(pool["account_id"])
        stamp_now = stamp(self.now_fn())
        actor = actor or candidate["actor"]
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute("""INSERT OR IGNORE INTO x_video_publish_pool_slot(tenant_key,user_id,account_id,
                real_account_id,bj_date,slot_time,at_utc,status,created_at_utc) VALUES(?,?,?,?,?,?,?,'claimed',?)""",
                (*owner, aid, pool["real_account_id"], candidate["key"][3], candidate["key"][4], stamp(at), stamp_now))
            if not inserted.rowcount:
                return {"status": "already_recorded"}
            slot_id = inserted.lastrowid
            current = self.pool._row(conn, actor, aid)
            stale = (current["status"] != "enabled" or current["version"] != pool["version"]
                     or self.pool._config_facts(actor, aid, conn) != candidate["config_facts"])
            late = not 0 <= (self.now_fn() - at).total_seconds() < 60
            head = conn.execute("SELECT * FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='pending' AND task_id='' ORDER BY position,id LIMIT 1", (*owner, aid)).fetchone()
            if stale or late or (head["id"] if head else None) != (item["id"] if item else None):
                reason = "configuration_changed" if stale else "slot_expired" if late else "queue_changed"
                conn.execute("UPDATE x_video_publish_pool_slot SET status='skipped',reason=? WHERE id=?", (reason, slot_id))
                conn.commit()
                return {"status": "skipped", "reason": reason, "slot_id": str(slot_id)}
            if error is None and item is None:
                conn.execute("UPDATE x_video_publish_pool_slot SET status='empty',reason='pool_empty' WHERE id=?", (slot_id,))
                conn.commit()
                return {"status": "empty", "slot_id": str(slot_id)}
            if item:
                conn.execute("UPDATE x_video_publish_pool_slot SET item_id=?,material_id=? WHERE id=?", (item["id"], item["material_id"], slot_id))
            if error is None:
                conn.execute("SAVEPOINT freeze_pool_task")
                try:
                    operation = "xpool:" + hashlib.sha256(canonical(candidate["key"]).encode()).hexdigest()
                    batch = self.store.commit(actor, token, operation, accounts={aid: account}, materials=materials,
                                              now=self.now_fn(), conn=conn)
                    tasks = self.pool._batch_tasks(batch)
                    if len(tasks) != 1:
                        raise RuntimeError("pool slot must freeze exactly one task")
                    task_id, batch_id = str(tasks[0].get("id") or tasks[0]["task_id"]), str(batch.get("id") or batch["batch_id"])
                    changed = conn.execute("UPDATE x_video_publish_pool_item SET status='processing',task_id=?,batch_id=?,updated_at_utc=? WHERE id=? AND status='pending' AND task_id=''", (task_id, batch_id, stamp_now, item["id"]))
                    if changed.rowcount != 1:
                        raise RuntimeError("pool head changed during freeze")
                    conn.execute("UPDATE x_video_publish_pool_slot SET status='created',task_id=?,batch_id=? WHERE id=?", (task_id, batch_id, slot_id))
                    self.pool._bump(conn, actor, aid)
                    conn.execute("RELEASE freeze_pool_task")
                    conn.commit()
                    return {"status": "created", "slot_id": str(slot_id), "task_id": task_id, "batch_id": batch_id}
                except XPostError as exc:
                    conn.execute("ROLLBACK TO freeze_pool_task")
                    conn.execute("RELEASE freeze_pool_task")
                    error = exc
                # Any unexpected SQLite/logic failure rolls the whole transaction
                # back, including slot creation, core token consumption and item.
            state, reason, pause, consume = self._failure(error)
            conn.execute("UPDATE x_video_publish_pool_slot SET status=?,reason=? WHERE id=?", (state, reason, slot_id))
            if pause:
                conn.execute("UPDATE x_video_publish_pool SET status='paused',pause_reason=?,version=version+1,updated_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=?", (reason, stamp_now, *owner, aid))
            if consume and item:
                conn.execute("UPDATE x_video_publish_pool_item SET status='failed',reason=?,updated_at_utc=? WHERE id=? AND status='pending' AND task_id=''", (str(error)[:500], stamp_now, item["id"]))
                self.pool._bump(conn, actor, aid)
            conn.commit()
            return {"status": state, "reason": reason, "slot_id": str(slot_id)}

    def tick(self, worker_id):
        self._worker(worker_id)
        if not self.enabled or not self.pool.enabled:
            return {"status": "pool_gate_closed", "slots_created": 0}
        if not self._lock.acquire(blocking=False):
            return {"status": "tick_busy", "slots_created": 0}
        try:
            if self.started_at is None or self.worker_id != worker_id:
                return {"status": "not_started", "slots_created": 0}
            candidates, results, started = self._candidates(self.now_fn()), [], time.monotonic()
            for candidate in candidates[:20]:
                actor, account, materials, token, error = None, None, None, None, None
                if (self.now_fn() - candidate["at"]).total_seconds() >= 60 or time.monotonic() - started >= 30:
                    error = XPostError("slot_expired", "已错过本时点，不补发", status=503)
                else:
                    try:
                        actor, account, materials, token = self._resolve(candidate)
                    except XPostError as exc:
                        error = exc
                    except (OSError, TimeoutError) as exc:
                        error = XPostError("pool_source_unavailable", "本时点来源查询失败，不补发", status=503)
                results.append(self._finish(candidate, actor, account, materials, token, error))
            return {"status": "tick_complete", "slots_created": sum(row["status"] == "created" for row in results),
                    "slots_recorded": len(results), "results": results}
        finally:
            self._lock.release()

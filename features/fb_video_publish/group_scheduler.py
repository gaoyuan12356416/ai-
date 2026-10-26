"""Persist daily group plans and atomically freeze due batches; never call Graph."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import timedelta

from .core import BJ, DomainError, actor_checked, canonical, parse_time, stamp
from .group_pool import exclusion, pool_row, random_times, routing


class GroupScheduler:
    def __init__(self, group_pool, identity_resolver, *, now_fn=None, live_enabled=True):
        self.pool, self.store = group_pool, group_pool.store
        self.identity_resolver = identity_resolver
        self.now_fn = now_fn or self.store.now_fn
        self.live_enabled = bool(live_enabled)
        self.worker_id, self.started_at = "", None
        self._lock = threading.Lock()

    @staticmethod
    def _worker(worker_id):
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", worker_id):
            raise DomainError("invalid_request", "调度器标识无效")

    def start(self, worker_id):
        self._worker(worker_id)
        with self._lock:
            if worker_id != self.worker_id:
                self.worker_id, self.started_at = worker_id, self.now_fn()
            return {"ok": True, "status": "started", "started_at_utc": stamp(self.started_at)}

    def _candidates(self, now):
        local, result = now.astimezone(BJ), []
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pools = [dict(r) for r in conn.execute("SELECT * FROM fb_group_pool WHERE status='enabled' ORDER BY tenant_key")]
            for pool in pools:
                for offset in (0, 1):
                    day = (local + timedelta(days=offset)).date().isoformat()
                    existing = conn.execute("SELECT times_json FROM fb_group_pool_day WHERE tenant_key=? AND bj_date=? AND schedule_revision=?",
                                            (pool["tenant_key"], day, pool["schedule_revision"])).fetchone()
                    if existing:
                        times = json.loads(existing[0])
                    else:
                        times = (random_times(pool["tenant_key"], day, pool["schedule_revision"], pool["daily_batches"])
                                 if pool["schedule_mode"] == "random" else json.loads(pool["times_json"]))
                        conn.execute("INSERT INTO fb_group_pool_day VALUES(?,?,?,?,?,?)", (pool["tenant_key"], day,
                                     pool["schedule_revision"], pool["schedule_mode"], canonical(times), stamp(now)))
                    if offset:
                        continue
                    for value in times:
                        hour, minute = map(int, value.split(":"))
                        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if (at < self.started_at or at <= parse_time(pool["effective_at_utc"])
                                or not 0 <= (now - at).total_seconds() < 60):
                            continue
                        if conn.execute("SELECT 1 FROM fb_group_pool_run WHERE tenant_key=? AND slot_at_utc=?", (pool["tenant_key"], stamp(at))).fetchone():
                            continue
                        boundary = conn.execute("SELECT COALESCE(MAX(id),0) FROM fb_group_pool_item WHERE tenant_key=?", (pool["tenant_key"],)).fetchone()[0]
                        result.append({"pool": pool, "at": at, "max_item_id": boundary})
            conn.commit()
        return result

    def _identity(self, pool):
        actor = actor_checked(self.identity_resolver(pool["tenant_key"], pool["publisher_user_id"]))
        if (actor["tenant_key"], actor["user_id"]) != (pool["tenant_key"], pool["publisher_user_id"]):
            raise DomainError("identity_inactive", "排期发布人身份不匹配", 403)
        if not re.fullmatch(r"[1-9][0-9]{0,30}", actor["sub_user_id"]):
            raise DomainError("identity_attribution_invalid", "排期发布人缺少有效归因身份", 403)
        return actor

    def _finish(self, candidate, actor, facts, error):
        pool, at = candidate["pool"], candidate["at"]
        policy_version, selection = routing(pool)
        now = stamp(self.now_fn())
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM fb_group_pool_run WHERE tenant_key=? AND slot_at_utc=?", (pool["tenant_key"], stamp(at))).fetchone():
                return {"status": "already_recorded"}
            current = pool_row(conn, pool["tenant_key"])
            stale = (current["status"] != "enabled" or current["schedule_revision"] != pool["schedule_revision"]
                     or current["publisher_user_id"] != pool["publisher_user_id"]
                     or current["effective_at_utc"] != pool["effective_at_utc"])
            late = not 0 <= (self.now_fn() - at).total_seconds() < 60
            if stale or late:
                error = DomainError("configuration_changed" if stale else "slot_expired", "配置已变化或已错过时点，不补发", 409)
            selected = {"pairs": [], "excluded": []}
            if error is None:
                selected = self.pool.match(conn, actor, pool, facts, day=at.astimezone(BJ).date().isoformat())
            else:
                selected["excluded"] = [exclusion(str(getattr(error, "code", "group_source_unavailable")),
                    str(error) if hasattr(error, "code") else "Page组或素材查询暂不可用")]
            reason = selected["excluded"][0]["code"] if error else "" if selected["pairs"] else "no_eligible_pairs"
            status = "skipped" if error else "created" if selected["pairs"] else "empty"
            run_id = conn.execute("""INSERT INTO fb_group_pool_run(tenant_key,slot_at_utc,schedule_revision,
                    publisher_user_id,max_item_id,policy_version,status,reason,pairs_json,excluded_json,created_at_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (pool["tenant_key"], stamp(at), pool["schedule_revision"],
                    pool["publisher_user_id"], candidate["max_item_id"], policy_version, status, reason,
                    canonical(selected["pairs"]), canonical(selected["excluded"]), now)).lastrowid
            if error and getattr(error, "status", 500) == 403 and not stale:
                conn.execute("UPDATE fb_group_pool SET status='paused',pause_reason=?,version=version+1,updated_at_utc=? WHERE tenant_key=?",
                             (reason, now, pool["tenant_key"]))
            if selected["pairs"]:
                # Reservations, tasks, links and batch are committed together. Any
                # unexpected failure rolls back the run too; no partial ownership.
                pairs = [{"material_id": p["material_id"], "page_id": p["page_id"], "config_instance_id": "", "config_version": 0,
                          "caption_template": pool["caption_template"]} for p in selected["pairs"]]
                operation = uuid.uuid5(uuid.NAMESPACE_URL, "fb-group-pool:" + canonical([pool["tenant_key"], stamp(at)])).hex
                request = {"operation_id": operation, "material_ids": [p["material_id"] for p in pairs],
                           "page_ids": [p["page_id"] for p in pairs], "pairs": pairs, "processing_mode": "original",
                           "schema_version": 2, "material_schedules": {p["material_id"]: None for p in pairs}}
                resolved = {"pages": {p["page_id"]: facts["pages"][p["page_id"]] for p in pairs},
                            "materials": {p["material_id"]: facts["materials"][p["material_id"]] for p in pairs}}
                configs = {pid: {"config_instance_id": "", "version": pool["schedule_revision"], "language": page["language"]}
                           for pid, page in resolved["pages"].items()}
                plans = [(n, p, 1, at, at.astimezone(BJ).date().isoformat()) for n, p in enumerate(pairs, 1)]
                snapshot = {"policy_version": policy_version, "run_id": run_id, "group_ids": json.loads(pool["group_ids_json"]),
                            "page_language_selection": selection,
                            "schedule_revision": pool["schedule_revision"], "slot_at_utc": stamp(at), "max_item_id": candidate["max_item_id"]}
                result = self.store.freeze(conn, actor, request, resolved, configs, {}, plans, group_pool=snapshot)
                item_ids = {p["material_id"]: int(p["item_id"]) for p in selected["pairs"]}
                for task in result["batch"]["items"]:
                    item_id = item_ids[task["material_id"]]
                    conn.execute("INSERT INTO fb_group_pool_reservation VALUES(?,?,?,?,?,?)",
                        (pool["tenant_key"], task["material_id"], item_id, run_id, task["task_id"], now))
                    changed = conn.execute("""UPDATE fb_group_pool_item SET status='bound',task_id=?,batch_id=?,updated_at_utc=?
                        WHERE id=? AND tenant_key=? AND status='pending' AND task_id=''""",
                        (task["task_id"], result["batch_id"], now, item_id, pool["tenant_key"])).rowcount
                    if changed != 1:
                        raise RuntimeError("Group pool reservation lost its pending item")
                conn.execute("UPDATE fb_group_pool_run SET batch_id=? WHERE id=?", (result["batch_id"], run_id))
                conn.execute("UPDATE fb_group_pool SET version=version+1,updated_at_utc=? WHERE tenant_key=?", (now, pool["tenant_key"]))
            conn.commit()
            return {"status": status, "run_id": str(run_id), "task_count": len(selected["pairs"]), "reason": reason}

    def tick(self, worker_id):
        self._worker(worker_id)
        if not self.live_enabled or not self.pool.enabled or not self.pool.service.live_enabled:
            return {"ok": True, "status": "group_pool_gate_closed", "runs_created": 0}
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "status": "tick_busy", "runs_created": 0}
        try:
            if self.worker_id != worker_id or self.started_at is None:
                return {"ok": True, "status": "not_started", "runs_created": 0}
            results = []
            for candidate in self._candidates(self.now_fn()):
                actor, facts, error = None, None, None
                try:
                    actor = self._identity(candidate["pool"])
                    facts = self.pool.candidates(actor, candidate["pool"],
                        seed=[candidate["pool"]["tenant_key"], stamp(candidate["at"])], max_item_id=candidate["max_item_id"])
                except Exception as exc:
                    error = exc
                results.append(self._finish(candidate, actor, facts, error))
            return {"ok": True, "status": "tick_complete", "runs_created": sum(r["status"] == "created" for r in results),
                    "runs_recorded": len(results), "results": results}
        finally:
            self._lock.release()

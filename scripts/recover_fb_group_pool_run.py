"""Explicit operator recovery of a skipped, unwritten group run on the same Beijing day.

Dry-run by default. Uses current identity/source/occupancy checks and the normal
task freezer; never calls Meta. A deterministic operation receipt is committed
with the new run and reservations. The original skipped run is immutable.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish.core import BJ, DomainError, Store, canonical, digest, parse_time, stamp
from features.fb_video_publish.group_pool import GroupPoolService, pool_row, routing


def require(test, message):
    if not test:
        raise DomainError("recovery_conflict", message, 409)


def check_original(conn, pool, run_id, revision, now):
    row = conn.execute("SELECT * FROM fb_group_pool_run WHERE id=? AND tenant_key=?", (run_id, pool["tenant_key"])).fetchone()
    require(row is not None, "Original run is missing")
    row = dict(row)
    require(pool["status"] == "enabled" and pool["schedule_revision"] == revision == row["schedule_revision"], "Schedule changed")
    require(row["status"] == "skipped" and row["reason"] == "group_pool_source_unavailable"
            and not row["batch_id"] and json.loads(row["pairs_json"]) == [], "Original run is not an unwritten source failure")
    require(not conn.execute("SELECT 1 FROM fb_group_pool_reservation WHERE run_id=?", (run_id,)).fetchone(), "Original run has reservations")
    slot = parse_time(row["slot_at_utc"])
    require(slot < now and slot.astimezone(BJ).date() == now.astimezone(BJ).date(), "Recovery must be on the original Beijing day")
    require(row["publisher_user_id"] == pool["publisher_user_id"], "Publisher changed")
    return row


def recover(group, identity, run_id, revision, *, apply=False):
    store = group.store
    with store.connect() as conn:
        original = conn.execute("SELECT tenant_key FROM fb_group_pool_run WHERE id=?", (run_id,)).fetchone()
        require(original is not None, "Original run is missing")
        pool = pool_row(conn, original[0])
        old = check_original(conn, pool, run_id, revision, store.now_fn())
        operation = uuid.uuid5(uuid.NAMESPACE_URL, "fb-group-recovery:" + canonical([pool["tenant_key"], run_id])).hex
        previous = conn.execute("SELECT response_json FROM fb_group_pool_operation WHERE tenant_key=? AND operation_id=?",
                                (pool["tenant_key"], operation)).fetchone()
        if previous:
            return {**json.loads(previous[0]), "idempotent": True}
    actor = identity(pool["tenant_key"], pool["publisher_user_id"])
    require((actor["tenant_key"], actor["user_id"]) == (pool["tenant_key"], pool["publisher_user_id"]), "Identity mismatch")
    facts = group.candidates(actor, pool, seed=[pool["tenant_key"], old["slot_at_utc"]], max_item_id=old["max_item_id"])
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        current = pool_row(conn, pool["tenant_key"])
        now = store.now_fn()
        check_original(conn, current, run_id, revision, now)
        require(current == pool, "Pool changed during source verification")
        previous = conn.execute("SELECT response_json FROM fb_group_pool_operation WHERE tenant_key=? AND operation_id=?",
                                (pool["tenant_key"], operation)).fetchone()
        if previous:
            return {**json.loads(previous[0]), "idempotent": True}
        day, timestamp = now.astimezone(BJ).date().isoformat(), stamp(now)
        selected = group.match(conn, actor, pool, facts, day=day)
        summary = {"ok": True, "original_run_id": run_id, "task_count": len(selected["pairs"]),
                   "excluded_codes": dict(Counter(x["code"] for x in selected["excluded"])),
                   "max_item_id": old["max_item_id"], "pairs_digest": digest(selected["pairs"]), "applied": apply}
        if not apply or not selected["pairs"]:
            return summary
        policy, selection = routing(pool)
        recovery_id = conn.execute("""INSERT INTO fb_group_pool_run(tenant_key,slot_at_utc,schedule_revision,
            publisher_user_id,max_item_id,policy_version,status,reason,pairs_json,excluded_json,created_at_utc)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (pool["tenant_key"], timestamp, revision, actor["user_id"], old["max_item_id"],
            policy, "created", "operator_recovery_of_run_" + str(run_id), canonical(selected["pairs"]),
            canonical(selected["excluded"]), timestamp)).lastrowid
        pairs = [{"material_id": p["material_id"], "page_id": p["page_id"], "config_instance_id": "", "config_version": 0,
                  "caption_template": pool["caption_template"]} for p in selected["pairs"]]
        request = {"operation_id": operation, "material_ids": [p["material_id"] for p in pairs],
                   "page_ids": [p["page_id"] for p in pairs], "pairs": pairs, "processing_mode": "original",
                   "schema_version": 2, "material_schedules": {p["material_id"]: None for p in pairs}}
        resolved = {"pages": {p["page_id"]: facts["pages"][p["page_id"]] for p in pairs},
                    "materials": {p["material_id"]: facts["materials"][p["material_id"]] for p in pairs}}
        configs = {pid: {"config_instance_id": "", "version": revision, "language": p["language"]} for pid, p in resolved["pages"].items()}
        snapshot = {"policy_version": policy, "run_id": recovery_id, "group_ids": json.loads(pool["group_ids_json"]),
                    "page_language_selection": selection, "schedule_revision": revision, "slot_at_utc": timestamp,
                    "max_item_id": old["max_item_id"], "recovery_of_run_id": run_id, "original_slot_at_utc": old["slot_at_utc"]}
        result = store.freeze(conn, actor, request, resolved, configs, {},
                              [(n, p, 1, now, day) for n, p in enumerate(pairs, 1)], group_pool=snapshot)
        item_ids = {p["material_id"]: int(p["item_id"]) for p in selected["pairs"]}
        for task in result["batch"]["items"]:
            item_id = item_ids[task["material_id"]]
            conn.execute("INSERT INTO fb_group_pool_reservation VALUES(?,?,?,?,?,?)",
                         (pool["tenant_key"], task["material_id"], item_id, recovery_id, task["task_id"], timestamp))
            changed = conn.execute("""UPDATE fb_group_pool_item SET status='bound',task_id=?,batch_id=?,updated_at_utc=?
                WHERE id=? AND tenant_key=? AND status='pending' AND task_id=''""",
                (task["task_id"], result["batch_id"], timestamp, item_id, pool["tenant_key"])).rowcount
            require(changed == 1, "Material reservation changed")
        conn.execute("UPDATE fb_group_pool_run SET batch_id=? WHERE id=?", (result["batch_id"], recovery_id))
        conn.execute("UPDATE fb_group_pool SET version=version+1,updated_at_utc=? WHERE tenant_key=?", (timestamp, pool["tenant_key"]))
        summary.update(recovery_run_id=recovery_id, batch_id=result["batch_id"], operation_id=operation, idempotent=False)
        conn.execute("INSERT INTO fb_group_pool_operation VALUES(?,?,?,?,?,?)", (pool["tenant_key"], actor["user_id"], operation,
                     digest(["operator_recovery", run_id, revision]), canonical(summary), timestamp))
        conn.commit()
        return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    from features.fb_video_publish.sources import SourceRepository, BrokerReadOnlyMySQL
    from features.fb_video_publish.group_sources import GroupPoolSource
    from features.fb_video_publish.identity_client import IdentityClient
    from features.fb_auto_posts.repositories import PagePoolRepository, MaterialRepository
    pid = subprocess.check_output(["systemctl", "show", "fb-auto-post-service.service", "-p", "MainPID", "--value"], text=True).strip()
    require(pid.isdigit() and int(pid) > 0, "Publishing service is inactive")
    env = {k.decode(): v.decode() for x in Path("/proc/" + pid + "/environ").read_bytes().split(b"\0")
           if b"=" in x for k, v in [x.split(b"=", 1)]}
    path = env["FB_AUTO_POST_DB_PATH"]
    require(env.get("FB_VIDEO_GROUP_POOL_ENABLED") == "1" and env.get("FB_VIDEO_PUBLISH_ENABLED") == "1"
            and env.get("FB_AUTO_POST_LIVE_ENABLED") == "1", "Live gates are not enabled")
    # Open the existing store without constructor migrations.
    store = Store.__new__(Store)
    store.path, store.now_fn, store.daily_limit = path, lambda: datetime.now(timezone.utc), int(env.get("FB_VIDEO_DAILY_LIMIT", "1000"))
    mysql = BrokerReadOnlyMySQL(env)
    source = GroupPoolSource(SourceRepository(PagePoolRepository(mysql), MaterialRepository(mysql)))
    group = GroupPoolService(SimpleNamespace(store=store, group_pool_enabled=True), sources=source)
    if args.apply:
        require(args.backup is not None and not args.backup.exists(), "Supply a new backup path")
        require(str(args.backup.resolve()).startswith("/mnt/data-disk/"), "Backup must be on data disk")
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect("file:" + path + "?mode=ro", uri=True) as src, sqlite3.connect(args.backup) as dst:
            src.backup(dst)
        args.backup.chmod(0o600)
    client = IdentityClient(env)
    result = recover(group, lambda t, u: client.resolve(t, u, navigation_key="fbPostMaterialPool"), args.run_id,
                     args.expected_revision, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "recovery_failed"), "type": type(exc).__name__}), flush=True)
        raise SystemExit(1)

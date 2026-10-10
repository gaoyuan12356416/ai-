#!/usr/bin/env python3
"""Audited, idempotent operator round using the current pool configurations.

Run only after explicit publication authorization, under the normal runner
environment. This adds claims, never edits daily plans or historical queues.
Selection, account verification and publication use the deployed scheduler.
"""
import argparse
import contextlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


BEIJING = timezone(timedelta(hours=8))
ACTION = "operator_current_pool_once_v1"


def claim(db, request_id, expected_versions, *, actor, now=None):
    if not re.fullmatch(r"[a-z0-9-]{8,80}", request_id) or not actor.strip():
        raise ValueError("An explicit request identity and actor are required")
    if set(expected_versions) != {"material", "drama"}:
        raise ValueError("Exactly one material and one drama round are required")
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    date, minute = current.date().isoformat(), current.strftime("%H:%M")
    stamp = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    with contextlib.closing(sqlite3.connect(str(db), timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for source in ("material", "drama"):
            key = "xpost:operator-pool-once:v1:%s:%s" % (request_id, source)
            prior = conn.execute("SELECT * FROM x_post_schedule_run WHERE slot_key=?", (key,)).fetchone()
            if prior:
                evidence = conn.execute(
                    "SELECT evidence_json FROM x_post_operator_gap_recovery_audit WHERE action=? AND identity=?",
                    (ACTION, prior["id"]),
                ).fetchone()
                if (not evidence or json.loads(evidence[0]).get("request_id") != request_id
                        or prior["config_version"] != expected_versions[source]):
                    raise ValueError("Existing request does not match its audited scope")
                rows.append(dict(prior))
                continue
            config = conn.execute("SELECT * FROM x_post_schedule_config WHERE source_type=?", (source,)).fetchone()
            if not config or not config["enabled"] or config["version"] != expected_versions[source]:
                raise ValueError("Current configuration changed; inspect before publishing")
            accounts = json.loads(config["account_ids_json"])
            if not accounts or len(accounts) != len(set(accounts)):
                raise ValueError("Invalid current account scope")
            if conn.execute("SELECT 1 FROM x_post_schedule_run WHERE source_type=? AND run_date=? AND publish_time=?", (source, date, minute)).fetchone():
                raise ValueError("This minute already belongs to another run")
            plan = conn.execute("SELECT publish_times_json FROM x_post_schedule_random_plan WHERE source_type=? AND run_date=?", (source, date)).fetchone()
            if plan and minute in json.loads(plan[0]):
                raise ValueError("This minute belongs to an immutable daily plan")
            if config["schedule_mode"] == "fixed" and minute in json.loads(config["publish_times_json"]):
                raise ValueError("This minute belongs to a configured schedule")
            cur = conn.execute(
                "INSERT INTO x_post_schedule_run(slot_key,source_type,run_date,publish_time,timezone,"
                "config_version,account_ids_json,schedule_mode,body_template,status,expected_count,"
                "created_at,updated_at,lease_heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,'claimed',?,?,?,?)",
                (key, source, date, minute, config["timezone"], config["version"], config["account_ids_json"],
                 config["schedule_mode"], config["body_template"], len(accounts), stamp, stamp, stamp),
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS x_post_operator_gap_recovery_audit("
                "id INTEGER PRIMARY KEY,action TEXT NOT NULL,identity INTEGER NOT NULL,actor TEXT NOT NULL,"
                "previous_state_json TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(action,identity))"
            )
            conn.execute(
                "INSERT INTO x_post_operator_gap_recovery_audit(action,identity,actor,previous_state_json,evidence_json,created_at) VALUES(?,?,?,?,?,?)",
                (ACTION, cur.lastrowid, actor, json.dumps(dict(config), ensure_ascii=False),
                 json.dumps({"request_id": request_id, "authorization": "Publish current material and drama pools once",
                             "daily_plans_unchanged": True, "historical_retries": False}), stamp),
            )
            rows.append(dict(conn.execute("SELECT * FROM x_post_schedule_run WHERE id=?", (cur.lastrowid,)).fetchone()))
        conn.commit()
    return rows


def identity(row):
    return {"source_type": row["source_type"], "run_date": row["run_date"],
            "publish_time": row["publish_time"], "timezone": row["timezone"],
            "version": row["config_version"], "account_ids": json.loads(row["account_ids_json"]),
            "schedule_mode": row["schedule_mode"], "body_template": row["body_template"],
            "slot_key": row["slot_key"], "frozen": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime-root", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--material-version", type=int, required=True)
    p.add_argument("--drama-version", type=int, required=True)
    args = p.parse_args()
    sys.path.insert(0, str(Path(args.runtime_root).resolve()))
    from scripts.x_post_schedule_runner import ScheduleConfig, ScheduleSidecarClient, execute_schedule_tick, process_lock
    config = ScheduleConfig.from_env()
    config.validate()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with process_lock(config.lock_path) as acquired:
        if acquired is None:
            raise RuntimeError("Another publisher holds the runner lock; no claim created")
        client = ScheduleSidecarClient(config.internal_url, config.internal_token, timeout=config.internal_timeout)
        client.preflight_storage(config.storage_preflight_path)
        backup = output / "before.sqlite3"
        if not backup.exists():
            with contextlib.closing(sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with contextlib.closing(sqlite3.connect(str(backup))) as dst:
                    src.backup(dst)
            backup.chmod(0o600)
        rows = claim(args.db, args.request_id, {"material": args.material_version, "drama": args.drama_version}, actor=args.actor)
        (output / "claims.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"claimed_runs": [{"id": r["id"], "source_type": r["source_type"], "configured_accounts": r["expected_count"]} for r in rows]}), flush=True)
        for row in rows:
            if row["status"] not in {"claimed", "running"}:
                print(json.dumps({"run_id": row["id"], "status": "already_terminal", "ledger_status": row["status"]}), flush=True)
                continue
            if row["run_date"] != datetime.now(BEIJING).date().isoformat():
                raise RuntimeError("Cross-day replay is not authorized by this command")
            class ScopedClient(ScheduleSidecarClient):
                def due_schedules(self, *_a, **_kw):
                    return [identity(row)]
            scoped = ScopedClient(config.internal_url, config.internal_token, timeout=config.internal_timeout)
            result = execute_schedule_tick(config, sidecar=scoped)
            (output / (row["source_type"] + "-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"run_id": row["id"], "result": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

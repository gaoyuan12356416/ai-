#!/usr/bin/env python3
"""Plan/apply an exact manual backlog admission; never call X or create queues."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_manual_recovery import SCHEMA, fingerprint, identity


class RecoveryRejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise RecoveryRejected(code)


def connect(path, readonly=True):
    c = sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True, timeout=5, isolation_level=None)
    c.row_factory = sqlite3.Row
    if readonly:
        c.execute("PRAGMA query_only=ON")
    return c


def rows(c, sql, params=()):
    return [dict(row) for row in c.execute(sql, params)]


def existing(c, run_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_post_manual_resume_audit'").fetchone():
        return None
    return c.execute("SELECT * FROM x_post_manual_resume_audit WHERE run_id=?", (run_id,)).fetchone()


def snapshot(c, run_ids, allowed_dates):
    require(run_ids and len(run_ids) == len(set(run_ids)) and
        all(type(i) is int and i > 0 for i in run_ids), "exact_unique_run_ids_required")
    require(allowed_dates and len(allowed_dates) <= 2 and
        all(dt.date.fromisoformat(day).isoformat() == day for day in allowed_dates), "exact_dates_required")
    result = []
    for run_id in run_ids:
        found = rows(c, "SELECT * FROM x_post_manual_run WHERE id=?", (run_id,))
        require(len(found) == 1, "manual_run_missing")
        run = found[0]
        require(run["trigger_source"] == "manual" and run["run_date"] in allowed_dates,
            "manual_run_outside_authorized_scope")
        require(run["status"] == "queued" and not any(run[k] for k in
            ("started_at", "finished_at", "queued_count", "published_count", "failed_count", "unknown_count")),
            "manual_run_already_started")
        require(not c.execute("SELECT 1 FROM x_post_queue WHERE manual_run_id=?", (run_id,)).fetchone(),
            "manual_queue_exists")
        require(not c.execute("SELECT 1 FROM x_post_manual_preflight_failure WHERE manual_run_id=?", (run_id,)).fetchone(),
            "manual_preflight_history_exists")
        accounts, materials = json.loads(run["account_ids_json"]), json.loads(run["material_ids_json"])
        require(len(accounts) == len(materials) == run["expected_count"] > 0,
            "manual_pairing_inconsistent")
        reservations = rows(c, "SELECT * FROM x_post_manual_material_reservation WHERE manual_run_id=? ORDER BY id", (run_id,))
        require(len(reservations) == len(materials) and
            sorted(str(r["material_key"]) for r in reservations) == sorted(str(m) for m in materials) and
            all(r["state"] == "active" for r in reservations), "manual_reservation_not_active")
        for reservation in reservations:
            require(not c.execute("SELECT 1 FROM x_post_manual_material_reservation WHERE material_key=? "
                "AND state='active' AND manual_run_id<>?", (reservation["material_key"], run_id)).fetchone(),
                "manual_reservation_conflict")
        require(existing(c, run_id) is None, "manual_run_already_authorized")
        result.append({"run_id": run_id, "identity_sha256": fingerprint(identity(run)),
            "run_sha256": fingerprint(run), "reservations_sha256": fingerprint(reservations)})
    return result


def plan(path, run_ids, allowed_dates):
    with contextlib.closing(connect(path)) as c:
        c.execute("BEGIN")
        result = {"kind": "x_unstarted_manual_resume", "version": 1,
            "db_path": str(Path(path).resolve()), "run_ids": sorted(run_ids),
            "allowed_dates": sorted(allowed_dates), "items": snapshot(c, sorted(run_ids), allowed_dates)}
    result["plan_sha256"] = fingerprint(result)
    return result


def apply(path, saved, expected_sha, backup_dir, actor, reason):
    unsigned = {k: v for k, v in saved.items() if k != "plan_sha256"}
    require(saved.get("kind") == "x_unstarted_manual_resume" and saved.get("version") == 1 and
        saved.get("plan_sha256") == expected_sha == fingerprint(unsigned), "plan_hash_mismatch")
    require(saved["db_path"] == str(Path(path).resolve()) and str(actor).strip() and str(reason).strip(),
        "path_actor_or_reason_invalid")
    with contextlib.closing(connect(path)) as c:
        receipts = [existing(c, i) for i in saved["run_ids"]]
        if receipts and all(r is not None and r["plan_sha256"] == expected_sha for r in receipts):
            return {"applied": False, "already_applied": True, "plan_sha256": expected_sha,
                "run_ids": saved["run_ids"], "platform_called": False}
        require(snapshot(c, saved["run_ids"], saved["allowed_dates"]) == saved["items"], "plan_state_drift")
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / ("manual-resume-" + uuid.uuid4().hex + ".sqlite3")
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"):
        pass
    with contextlib.closing(connect(path)) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
        require(destination.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    digest = hashlib.sha256()
    with backup.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(data)
    with contextlib.closing(connect(path, False)) as c:
        try:
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN IMMEDIATE")
            require(snapshot(c, saved["run_ids"], saved["allowed_dates"]) == saved["items"], "plan_state_drift")
            c.execute(SCHEMA)
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            for item in saved["items"]:
                c.execute("INSERT INTO x_post_manual_resume_audit VALUES(?,?,?,?,?,?,?,?)",
                    (item["run_id"], item["identity_sha256"], expected_sha, str(actor), str(reason), now,
                     digest.hexdigest(), json.dumps(item, sort_keys=True)))
            c.commit()
        except Exception:
            c.rollback()
            raise
    return {"applied": True, "plan_sha256": expected_sha, "run_ids": saved["run_ids"],
        "backup_path": str(backup), "backup_sha256": digest.hexdigest(), "platform_called": False,
        "queues_created": 0, "frozen_rows_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-ids")
    parser.add_argument("--dates")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--backup-dir")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    # Reserve a private output file before any authorized state mutation.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        if args.action == "plan":
            require(args.run_ids and args.dates, "run_ids_and_dates_required")
            result = plan(args.db, [int(i) for i in args.run_ids.split(",")], args.dates.split(","))
        else:
            require(all((args.plan, args.plan_sha256, args.backup_dir, args.actor, args.reason)), "apply_arguments_required")
            result = apply(args.db, json.loads(Path(args.plan).read_text()), args.plan_sha256,
                args.backup_dir, args.actor, args.reason)
        json.dump(result, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({"ok": True, "action": args.action, "plan_sha256": result["plan_sha256"]}))


if __name__ == "__main__":
    try:
        main()
    except RecoveryRejected as exc:
        print(json.dumps({"ok": False, "code": str(exc)}))
        raise SystemExit(2)

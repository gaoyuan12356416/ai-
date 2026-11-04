#!/usr/bin/env python3
"""Plan/apply the authorized retry of task 2712; never call X or select content."""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.operator_retry import (
    EXTERNAL_KEY, GENERATION, SCHEMA, TABLE, TARGET, fingerprint, frozen_identity,
)


class RecoveryRejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise RecoveryRejected(reason)


def connect(path, readonly=True):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def one(conn, sql, params):
    row = conn.execute(sql, params).fetchone()
    require(row is not None, "required_record_missing")
    return dict(row)


def audit(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    row = conn.execute("SELECT * FROM x_auto_operator_retry WHERE task_id=?", (TARGET["task_id"],)).fetchone()
    return dict(row) if row else None


def snapshot(auto, native):
    task = one(auto, "SELECT * FROM x_auto_task WHERE id=?", (TARGET["task_id"],))
    require(task["id"] == TARGET["task_id"] and all(task[key] == value for key, value in TARGET.items()
            if key not in {"task_id", "prior_run_id"}), "task_outside_authorized_scope")
    require(task["status"] == "failed" and task["error_code"] == "x_post_account_needs_review",
            "task_not_original_account_hold")
    require(task["execution_run_id"] == TARGET["prior_run_id"] and not any(task[k] for k in (
        "execution_queue_id", "execution_log_id", "unknown_outcome", "publish_id", "publish_url",
        "claim_token", "claim_worker", "claim_phase", "lease_expires_at_utc",
        "preparation_attempt_count", "publish_attempt_count")), "task_has_execution_or_lease")
    require(not auto.execute("SELECT 1 FROM x_auto_material_ledger WHERE task_id=?",
                            (task["id"],)).fetchone(), "task_material_reservation_exists")
    require(audit(auto) is None, "retry_already_authorized")
    run = one(auto, "SELECT * FROM x_auto_run WHERE id=?", (task["run_id"],))
    require(run["template_id"] == TARGET["template_id"]
            and run["template_version"] == TARGET["template_version"]
            and run["status"] in {"running", "partial_failed", "failed"}, "auto_run_identity_or_status_changed")
    prior = one(native, "SELECT * FROM x_post_manual_run WHERE id=?", (TARGET["prior_run_id"],))
    require(prior["status"] == "failed_preflight" and prior["error_code"] == "x_post_account_needs_review"
            and prior["trigger_source"] == "auto_template"
            and prior["external_task_key"] == "x-auto-task-2712"
            and prior["template_ref"] == "x-auto-template-4" and prior["template_version"] == 6
            and json.loads(prior["account_ids_json"]) == [46]
            and prior["expected_count"] == 1
            and not any(prior[k] for k in ("queued_count", "published_count", "failed_count", "unknown_count")),
            "prior_execution_not_zero_queue_failure")
    require(not native.execute("SELECT 1 FROM x_post_queue WHERE manual_run_id=?", (prior["id"],)).fetchone(),
            "prior_execution_queue_exists")
    require(not native.execute("SELECT 1 FROM x_post_manual_run WHERE external_task_key=?", (EXTERNAL_KEY,)).fetchone(),
            "retry_execution_already_exists")
    require(not native.execute("SELECT 1 FROM x_post_manual_material_reservation WHERE manual_run_id=? AND state='active'",
                               (prior["id"],)).fetchone(), "prior_material_still_reserved")
    account = one(native, "SELECT id,username,status,publish_approved,drama_language,deleted_at FROM x_authorized_account WHERE id=?", (46,))
    require(account["status"] == "active" and account["publish_approved"] == 1
            and not account["deleted_at"] and account["username"] == "nedialko123"
            and account["drama_language"] == task["language"] == "de", "account_not_publishable")
    require(not native.execute("SELECT 1 FROM x_post_publish_log WHERE account_id=46 AND "
                               "(unknown_outcome=1 OR status IN ('post_creating','repost_creating'))").fetchone(),
            "account_unknown_result_not_reconciled")
    require(not native.execute("SELECT 1 FROM x_post_repost_ledger WHERE (target_account_id=46 OR relay_account_id=46) "
                               "AND (unknown_outcome=1 OR status IN ('post_creating','repost_creating'))").fetchone(),
            "account_repost_result_not_reconciled")
    busy = []
    for row in native.execute("SELECT * FROM x_post_queue WHERE account_id=46 AND status IN ('queued','publishing') ORDER BY id"):
        queued = dict(row)
        active_log = one(native, "SELECT * FROM x_post_publish_log WHERE queue_id=?", (queued["id"],))
        require(queued["id"] != task["execution_queue_id"]
                and active_log["status"] in {"reserved", "media_uploading"}
                and not any(active_log[k] for k in ("unknown_outcome", "x_post_id", "published_at")),
                "account_busy_queue_not_safe_to_wait_behind")
        # Admission only: preserve the active publisher and let the existing
        # account-resource lock serialize this pending task behind that queue.
        busy.append({"queue_id": queued["id"], "log_id": active_log["id"],
                     "queue_status": queued["status"], "log_status": active_log["status"],
                     "attempt_count": active_log["attempt_count"],
                     "queue_sha256": fingerprint(queued), "log_sha256": fingerprint(active_log)})
    task_obj = SimpleNamespace(**{**task, "account_snapshot": json.loads(task["account_snapshot_json"])})
    identity = frozen_identity(task_obj)
    return {"identity": identity, "identity_sha256": fingerprint(identity),
            "config_sha256": run["config_sha256"], "task_sha256": fingerprint(task),
            "prior_run_sha256": fingerprint(prior), "account_sha256": fingerprint(account),
            "existing_busy_queues": busy}


def build_plan(auto_db, native_db, task_id):
    require(type(task_id) is int and task_id == TARGET["task_id"], "only_task_2712_authorized")
    with contextlib.closing(connect(auto_db)) as auto, contextlib.closing(connect(native_db)) as native:
        auto.execute("BEGIN")
        native.execute("BEGIN")
        state = snapshot(auto, native)
    result = {"kind": "x_auto_zero_queue_operator_retry", "version": 1, "target": TARGET,
              "generation": GENERATION, "external_task_key": EXTERNAL_KEY,
              "auto_db": str(Path(auto_db).resolve()), "native_db": str(Path(native_db).resolve()),
              "state": state}
    result["plan_sha256"] = fingerprint(result)
    return result


def backup(source, destination):
    with contextlib.closing(connect(source)) as src, contextlib.closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def apply_plan(plan, auto_db, native_db, backup_dir, actor, reason):
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    require(plan.get("kind") == "x_auto_zero_queue_operator_retry" and plan.get("version") == 1
            and plan.get("target") == TARGET and plan.get("generation") == GENERATION
            and plan.get("external_task_key") == EXTERNAL_KEY
            and plan.get("plan_sha256") == fingerprint(unsigned), "plan_identity_or_hash_invalid")
    require(plan["auto_db"] == str(Path(auto_db).resolve()) and plan["native_db"] == str(Path(native_db).resolve()),
            "plan_database_path_changed")
    require(isinstance(actor, str) and 0 < len(actor.strip()) <= 128
            and isinstance(reason, str) and 0 < len(reason.strip()) <= 1000, "actor_and_reason_required")
    with contextlib.closing(connect(auto_db)) as auto:
        existing = audit(auto)
        if existing:
            require(existing["plan_sha256"] == plan["plan_sha256"], "different_retry_already_authorized")
            return {"ok": True, "already_applied": True, "task_id": 2712,
                    "external_task_key": EXTERNAL_KEY, "publish_requests": 0}
    folder = Path(backup_dir).resolve()
    require(not folder.exists(), "backup_directory_must_be_new")
    folder.mkdir(parents=True, mode=0o700)
    backup(auto_db, folder / "auto.before.sqlite3")
    backup(native_db, folder / "native.before.sqlite3")
    (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # Freeze both ledgers while rechecking.  No native row is ever changed.
    with contextlib.closing(connect(auto_db, False)) as auto, contextlib.closing(connect(native_db, False)) as native:
        auto.execute("BEGIN IMMEDIATE")
        native.execute("BEGIN IMMEDIATE")
        try:
            require(snapshot(auto, native) == plan["state"], "live_state_changed_replan_required")
            auto.execute(SCHEMA)
            state = plan["state"]
            auto.execute("INSERT INTO x_auto_operator_retry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                2712, GENERATION, 446, "46", 4, 6, 1657, EXTERNAL_KEY,
                state["identity_sha256"], state["config_sha256"], state["task_sha256"],
                state["prior_run_sha256"], plan["plan_sha256"], actor.strip(), reason.strip(), stamp,
            ))
            changed = auto.execute("""UPDATE x_auto_task SET status='pending', execution_run_id=NULL,
                execution_queue_id=NULL, execution_log_id=NULL, content_id='',series_code='',material_id='',
                selection_json='{}',body_sha256='',body_utf16_units=0,selected_duration_sec=0,body_template='',
                publish_id='',publish_url='',unknown_outcome=0,next_attempt_at_utc='',
                claim_phase='',claim_worker='',claim_token='',lease_expires_at_utc='',error_code='',error_message='',
                selected_at_utc='',reserved_at_utc='',bridged_at_utc='',published_at_utc='',finished_at_utc='',updated_at=?
                WHERE id=2712 AND status='failed' AND execution_run_id=1657
                  AND execution_queue_id IS NULL AND unknown_outcome=0""", (stamp,)).rowcount
            require(changed == 1, "task_compare_and_swap_failed")
            auto.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=446", (stamp,))
            details = {"actor": actor.strip(), "reason": reason.strip(), "prior_execution_run_id": 1657,
                       "external_task_key": EXTERNAL_KEY, "plan_sha256": plan["plan_sha256"],
                       "prior_task_sha256": state["task_sha256"], "new_selection_required": True,
                       "existing_busy_queues": state["existing_busy_queues"],
                       "busy_queue_action": "preserved; ordinary account-resource serialization"}
            auto.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) "
                         "VALUES(446,2712,'operator_zero_queue_retry','failed','pending',?,?,?)", (
                             "已核对账号历史阻塞，按原模板重新选材并恢复此任务", json.dumps(details, ensure_ascii=False), stamp))
            auto.commit()
            native.rollback()
        except Exception:
            auto.rollback()
            native.rollback()
            raise
    result = {"ok": True, "task_id": 2712, "run_id": 446, "prior_execution_run_id": 1657,
              "external_task_key": EXTERNAL_KEY, "backup_dir": str(folder),
              "plan_sha256": plan["plan_sha256"], "publish_requests": 0,
              "note": "Original task rearmed; normal scheduler performs frozen-rule selection and publication."}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", type=Path)
    modes.add_argument("--apply", type=Path)
    parser.add_argument("--auto-db", required=True)
    parser.add_argument("--native-db", required=True)
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--backup-dir")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.plan:
        require(not args.plan.exists(), "plan_destination_exists")
        value = build_plan(args.auto_db, args.native_db, args.task_id)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"plan": str(args.plan), "plan_sha256": value["plan_sha256"], "publish_requests": 0}))
    else:
        require(args.task_id is None and args.backup_dir, "apply_uses_frozen_task_and_requires_backup")
        value = json.loads(args.apply.read_text(encoding="utf-8"))
        print(json.dumps(apply_plan(value, args.auto_db, args.native_db, args.backup_dir, args.actor, args.reason), ensure_ascii=False))


if __name__ == "__main__":
    main()

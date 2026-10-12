#!/usr/bin/env python3
"""Create an idempotent two-account recovery after a media-host preflight failure.

This operator-only script never publishes, resets a task, or edits a template.
Execute with the publisher's service identity and environment while its runner
and scheduler are drained. The normal X Auto runner executes the child tasks.
"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


AUTO_DB = "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3"
POST_DB = "/var/lib/x-post-automation/accounts.sqlite3"


def reader(path):
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def inspect(auto_db, post_db, source_run, source_ids, operation):
    with contextlib.closing(reader(auto_db)) as auto, contextlib.closing(reader(post_db)) as post:
        placeholders = ",".join("?" for _ in source_ids)
        tasks = [dict(r) for r in auto.execute("SELECT * FROM x_auto_task WHERE id IN (" + placeholders + ") ORDER BY id", source_ids)]
        assert len(tasks) == len(source_ids), "source tasks missing"
        parent = dict(auto.execute("SELECT * FROM x_auto_run WHERE id=?", (source_run,)).fetchone())
        assert parent["status"] in ("partial_failed", "failed")
        for task in tasks:
            ident = task["id"]
            assert task["run_id"] == source_run
            assert task["template_id"] == parent["template_id"] and task["template_version"] == parent["template_version"]
            assert task["status"] == "failed" and task["error_code"] == "media_host_not_allowed"
            assert task["language"] and task["publish_attempt_count"] == 0
            assert not any(task[k] for k in ("publish_id", "publish_url", "unknown_outcome", "execution_queue_id", "execution_log_id", "claim_token", "lease_expires_at_utc"))
            assert task["execution_run_id"]
            run = dict(post.execute("SELECT * FROM x_post_manual_run WHERE id=?", (task["execution_run_id"],)).fetchone())
            assert run["status"] == "failed_preflight" and run["error_code"] == "media_host_not_allowed"
            assert run["external_task_key"] == "x-auto-task-%s" % ident
            assert run["trigger_source"] == "auto_template"
            assert json.loads(run["account_ids_json"]) == [int(task["account_id"])]
            assert not any(run[k] for k in ("queued_count", "published_count", "unknown_count"))
            assert post.execute("SELECT count(*) FROM x_post_queue WHERE manual_run_id=?", (run["id"],)).fetchone()[0] == 0
            assert auto.execute("SELECT count(*) FROM x_auto_material_ledger WHERE task_id=?", (ident,)).fetchone()[0] == 0
        original_rows = [list(r) for r in auto.execute("SELECT * FROM x_auto_task WHERE run_id=? ORDER BY id", (source_run,))]
        original_hash = hashlib.sha256(json.dumps(original_rows, ensure_ascii=True).encode()).hexdigest()
        existing = auto.execute("SELECT id FROM x_auto_run WHERE run_key=?", (operation,)).fetchone()
    return tasks, parent, original_hash, None if existing is None else existing[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--source-run", type=int, required=True)
    parser.add_argument("--task-ids", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--auto-db", default=AUTO_DB)
    parser.add_argument("--post-db", default=POST_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rehearsal", action="store_true")
    args = parser.parse_args()
    source_ids = tuple(sorted({int(value) for value in args.task_ids.split(",")}))
    assert 1 <= len(source_ids) <= 20 and min(source_ids) > 0 and args.source_run > 0
    assert re.fullmatch(r"[A-Za-z0-9._:@-]{8,128}", args.operation_id)
    scope = "%s:%s" % (args.source_run, ",".join(map(str, source_ids)))
    operation = "x-auto:recovery:media-host:" + args.operation_id + ":" + hashlib.sha256(scope.encode()).hexdigest()[:24]
    if args.rehearsal:
        assert Path(args.auto_db).resolve() != Path(AUTO_DB).resolve()
        assert Path(args.post_db).resolve() != Path(POST_DB).resolve()
    elif args.apply:
        import pwd
        assert os.geteuid() == pwd.getpwnam("x-post-daily").pw_uid, "use publisher service identity"
    tasks, parent, original_hash, existing = inspect(args.auto_db, args.post_db, args.source_run, source_ids, operation)
    account_ids = {task["account_id"] for task in tasks}
    if existing is not None:
        with contextlib.closing(reader(args.auto_db)) as conn:
            children = [dict(r) for r in conn.execute("SELECT id,account_id,status FROM x_auto_task WHERE run_id=? ORDER BY id", (existing,))]
        assert {r["account_id"] for r in children} == account_ids
        assert len(children) == len(source_ids)
        print(json.dumps({"ok": True, "idempotent": True, "run_id": existing, "tasks": children}))
        return
    if not args.apply:
        print(json.dumps({"ok": True, "dry_run": True, "source_run_id": args.source_run, "source_task_ids": source_ids, "accounts": sorted(account_ids), "source_task_sha256": original_hash}))
        return
    sys.path.insert(0, str(Path(args.runtime).resolve()))
    from features.x_auto_posts.core import XAutoPostStore, AuditActor
    from features.x_accounts.language import canonical_drama_language
    accounts = {}
    if args.rehearsal:
        for task in tasks:
            accounts[task["account_id"]] = json.loads(task["account_snapshot_json"])
    else:
        from features.x_auto_posts.x_sidecar import XPostAutoBridgeClient
        bridge = XPostAutoBridgeClient("http://127.0.0.1:8810", os.environ["X_POST_AUTO_INTERNAL_TOKEN"])
        for task in tasks:
            account = bridge.verify_account(int(task["account_id"]), only_refresh_required=False, preserve_transient_status=True, require_publish_approved=True)
            assert str(account["id"]) == task["account_id"]
            assert account["status"] == "active" and account["publish_eligible"]
            assert canonical_drama_language(account["drama_language"]) == task["language"]
            accounts[task["account_id"]] = account
    store = XAutoPostStore(args.auto_db)
    now = datetime.now(timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=8)))
    run = store.create_run(run_key=operation, template_id=parent["template_id"], template_version=parent["template_version"], trigger_type="manual", scheduled_at_utc=now, shanghai_date=local.date().isoformat(), publish_time=local.strftime("%H:%M"), blacklist_snapshot=json.loads(parent["blacklist_snapshot_json"]), actor=AuditActor.from_values("operator-codex", "User-authorized CDN recovery"))
    result = []
    for task in tasks:
        account = dict(accounts[task["account_id"]])
        account.update(drama_language=task["language"], language_source="account")
        child = store.create_task(run_id=run.id, account_id=task["account_id"], account_username=account.get("username", task["account_username"]), account_display_name=account.get("display_name", ""), language=task["language"], body_template=task["body_template"], account_snapshot=account, account_snapshot_version=2)
        result.append({"source_task_id": task["id"], "task_id": child.id, "account_id": child.account_id, "status": child.status})
    with store._transaction() as conn:
        store._event(conn, run_id=run.id, event_type="operator_failed_preflight_recovery", created_at=store._now_iso(), details={"operation": operation, "source_run_id": args.source_run, "source_task_ids": list(source_ids), "source_task_sha256": original_hash, "reason": "User authorized socialkit-cdn.yingliang.tech allowlist and republish", "children": result})
    assert inspect(args.auto_db, args.post_db, args.source_run, source_ids, operation)[2] == original_hash, "source publication facts changed"
    print(json.dumps({"ok": True, "rehearsal": args.rehearsal, "run_id": run.id, "tasks": result, "source_tasks_preserved": True}))


if __name__ == "__main__":
    main()

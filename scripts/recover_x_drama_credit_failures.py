#!/usr/bin/env python3
"""One explicitly authorized retry of exact upload-finalize credit failures.

No timer, new queue, account reassignment, media change, or automatic replay.
The existing sidecar performs all current identity/media checks and X writes.
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
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REASON = "operator_exact_drama_upload_credit_retry_v1"
ERROR = "X媒体完成上传失败(HTTP 402): credits depleted"
AUDIT = "x_post_drama_upload_credit_recovery_audit"


class RecoveryConflict(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def one(conn, table, column, value):
    row = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
    return dict(row) if row else None


def snapshot(conn, queue_id):
    q = one(conn, "x_post_queue", "id", queue_id)
    if not q:
        raise RecoveryConflict("Queue is missing")
    return {
        "queue": q,
        "log": one(conn, "x_post_publish_log", "queue_id", queue_id),
        "pool": one(conn, "x_post_drama_pool", "id", q["drama_pool_item_id"]),
        "relay": one(conn, "x_post_repost_ledger", "queue_id", queue_id),
        "route": one(conn, "x_post_drama_delivery_route", "queue_id", queue_id),
    }


def validate(item, run_id):
    q, l, p, r, d = (item[k] for k in ("queue", "log", "pool", "relay", "route"))
    if not all((q, l, p, d)):
        raise RecoveryConflict("Incomplete frozen evidence")
    conditions = [
        q["source_type"] == "drama", q["status"] == "failed",
        q["schedule_run_id"] == run_id, q["account_id"] == l["account_id"],
        q["id"] == l["queue_id"], l["status"] == "failed",
        l["attempt_count"] == 1, l["unknown_outcome"] == 0,
        l["error_code"] == "x_upstream_error", l["error_message"] == ERROR,
        not any(l[k] for k in ("x_media_id", "x_post_id", "x_post_url", "published_at")),
        all(l[k] for k in ("long_url", "short_url", "post_text", "started_at")),
        p["status"] == "active", p["id"] == q["drama_pool_item_id"],
        p["content_id"] == q["content_id"], p["assigned_account_id"] == q["account_id"],
        p["replay_generation"] == q["drama_replay_generation"],
        p["next_sub_number"] == q["episode_number"],
        p["published_episode_count"] == q["episode_number"] - 1,
        p["free_episode_count"] >= q["episode_number"],
        p["last_error_code"] == l["error_code"], p["last_error_message"] == ERROR,
        q["media_validation_mode"] == "preflight", q["preflight_size"] > 0,
        bool(re.fullmatch(r"[a-f0-9]{64}", q["preflight_sha256"])),
        q["preflight_duration"] > 0, q["account_drama_language_frozen"] == 1,
        d["route_state"] == "resolved", d["resolved_delivery_mode"] == q["delivery_mode"],
    ]
    if q["delivery_mode"] == "direct":
        conditions += [r is None, q["relay_account_id"] == 0]
    elif q["delivery_mode"] == "premium_relay_repost" and r:
        conditions += [
            r["queue_id"] == q["id"], r["target_account_id"] == q["account_id"],
            r["relay_account_id"] == q["relay_account_id"], r["status"] == "failed",
            r["source_attempt_count"] == 1, r["repost_attempt_count"] == 0,
            r["unknown_outcome"] == 0, r["error_code"] == l["error_code"],
            r["error_message"] == ERROR,
            not any(r[k] for k in ("source_post_id", "source_post_url", "repost_id",
                                   "source_published_at", "reposted_at")),
        ]
    else:
        conditions.append(False)
    if not all(conditions):
        raise RecoveryConflict("Not an exact known upload-finalize credit failure")


def create_audit(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {AUDIT} (
        queue_id INTEGER PRIMARY KEY, schedule_run_id INTEGER NOT NULL,
        reason TEXT NOT NULL, actor TEXT NOT NULL, deployed_commit TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, before_json TEXT NOT NULL,
        before_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{AUDIT}_{operation.lower()}
            BEFORE {operation} ON {AUDIT} BEGIN
            SELECT RAISE(ABORT, 'credit recovery audit immutable'); END""")


def arm(conn, expected, run_id, *, actor, commit, manifest_sha, sync_run):
    """CAS the original queue, retain lifetime attempt counts and full evidence."""
    queue_id = expected["queue"]["id"]
    timestamp = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = snapshot(conn, queue_id)
        validate(current, run_id)
        if current != expected:
            raise RecoveryConflict("Frozen state changed since the approved manifest")
        create_audit(conn)
        if conn.execute(f"SELECT 1 FROM {AUDIT} WHERE queue_id=?", (queue_id,)).fetchone():
            raise RecoveryConflict("This authorized retry has already been consumed")
        before = canonical(current)
        conn.execute(f"INSERT INTO {AUDIT} VALUES (?,?,?,?,?,?,?,?,?)", (
            queue_id, run_id, REASON, actor, commit, manifest_sha, before,
            hashlib.sha256(before.encode()).hexdigest(), timestamp,
        ))
        # Do not clear the pool error or increment episode progress here.
        # Only the sidecar's confirmed final Post/Repost may do that.
        conn.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?",
                     (timestamp, queue_id))
        conn.execute("UPDATE x_post_publish_log SET status='reserved',updated_at=? WHERE queue_id=?",
                     (timestamp, queue_id))
        if current["relay"]:
            conn.execute("UPDATE x_post_repost_ledger SET status='reserved',updated_at=? WHERE queue_id=?",
                         (timestamp, queue_id))
        sync_run(conn, queue_id, timestamp)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def verify_route(sidecar, item):
    q = item["queue"]
    account_ids = [q["account_id"]]
    if q["relay_account_id"]:
        account_ids.append(q["relay_account_id"])
    accounts = {}
    for aid in account_ids:
        a = sidecar.verify_account(aid, schedule_preflight=True)
        if (a.get("drama_language") != q["account_drama_language"] or
                a.get("username") != (q["account_username"] if aid == q["account_id"]
                                      else q["relay_account_username"])):
            raise RecoveryConflict("Frozen account identity or language changed")
        accounts[aid] = a
    source = accounts[q["relay_account_id"] or q["account_id"]]
    if (q["preflight_duration"] > 140 or q["relay_account_id"]) and (
        not source.get("long_video_publish_eligible") or source.get("protected") is not False
    ):
        raise RecoveryConflict("Frozen long-video source no longer has qualifying membership")
    return [{k: a.get(k) for k in ("id", "username", "subscription_type", "drama_language")}
            for a in accounts.values()]


def emit(path, event):
    event = {"at": utcnow(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(canonical(event) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(canonical(event), flush=True)


@contextlib.contextmanager
def publishing_lock(path, progress):
    import fcntl
    with open(path, "a") as handle:
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started > 1800:
                    raise RecoveryConflict("Publishing lock busy for 30 minutes; no recovery was applied")
                emit(progress, {"event": "waiting_for_existing_publisher"})
                time.sleep(30)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--lock", default="/run/x-post-daily/runner.lock")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = Path(args.manifest).read_bytes()
    if hashlib.sha256(data).hexdigest() != args.expected_manifest_sha256:
        raise RecoveryConflict("Manifest digest mismatch")
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit) or not args.actor.strip():
        raise RecoveryConflict("Commit and operator identity are required")
    manifest = json.loads(data)
    run_id, items = manifest["run"]["id"], manifest["items"]
    if not items or len({item["queue"]["id"] for item in items}) != len(items):
        raise RecoveryConflict("Manifest queue scope is empty or duplicated")
    for item in items:
        validate(item, run_id)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    progress = out / "progress.jsonl"
    if not args.apply:
        with contextlib.closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            for item in items:
                if snapshot(conn, item["queue"]["id"]) != item:
                    raise RecoveryConflict("Live state differs from manifest")
        emit(progress, {"event": "validated", "queue_count": len(items), "x_writes": 0})
        return

    from features.x_posts.service import XPostStore
    from scripts.x_post_daily_runner import SidecarClient, SidecarError
    sidecar = SidecarClient(os.environ["X_POST_DAILY_INTERNAL_URL"],
                            os.environ["X_POST_DAILY_INTERNAL_TOKEN"], timeout=900)
    verifier = SidecarClient(sidecar.base_url, sidecar.token, timeout=90)
    with publishing_lock(args.lock, progress):
        with contextlib.closing(sqlite3.connect(args.db, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            backup = out / "before.sqlite3"
            if backup.exists():
                raise RecoveryConflict("Use a new output directory; never overwrite recovery evidence")
            with contextlib.closing(sqlite3.connect(backup)) as dst:
                conn.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RecoveryConflict("Backup integrity check failed")
            os.chmod(backup, 0o600)
            emit(progress, {"event": "backup_complete", "queue_count": len(items)})
            for item in items:
                q = item["queue"]
                entry = {"queue_id": q["id"], "account": q["account_username"],
                         "pool_id": q["drama_pool_item_id"], "episode": q["episode_number"]}
                try:
                    account_evidence = verify_route(verifier, item)
                except (SidecarError, RecoveryConflict) as exc:
                    emit(progress, {**entry, "event": "blocked_before_retry",
                                    "error_code": getattr(exc, "code", "frozen_route_ineligible"),
                                    "message": str(exc)})
                    if isinstance(exc, SidecarError) and (exc.unknown_outcome or exc.status == 429):
                        break
                    continue
                # The store's existing fence includes sibling accounts and all
                # unresolved external writes. It runs again inside publish.
                XPostStore._assert_account_publish_fence(conn, q)
                arm(conn, item, run_id, actor=args.actor, commit=args.commit,
                    manifest_sha=args.expected_manifest_sha256, sync_run=XPostStore._sync_run)
                emit(progress, {**entry, "event": "armed", "accounts": account_evidence})
                failure = None
                try:
                    sidecar.publish_queue("/internal/posts/queue/{queue_id}/publish", q["id"])
                except SidecarError as exc:
                    failure = exc
                # Never repeat the request after a transport exception. Read
                # the durable record first; ambiguous writes stop this batch.
                latest = snapshot(conn, q["id"])
                log = latest["log"]
                emit(progress, {**entry, "event": "publish_result", "status": log["status"],
                                "attempt_count": log["attempt_count"], "unknown": log["unknown_outcome"],
                                "error_code": log["error_code"], "message": log["error_message"],
                                "post_url": log["x_post_url"], "next_episode": latest["pool"]["next_sub_number"]})
                unknown = log["unknown_outcome"] or log["status"] in (
                    "media_uploading", "post_creating", "repost_creating")
                shared = log["error_code"] == "x_post_rate_limited" or "credits depleted" in log["error_message"].lower()
                if unknown or shared or (failure and (failure.unknown_outcome or failure.status == 429)):
                    emit(progress, {"event": "stopped_for_reconciliation_or_shared_failure"})
                    break
                if log["status"] not in ("published", "failed"):
                    emit(progress, {"event": "stopped_nonterminal_result"})
                    break
            final = {"run": one(conn, "x_post_schedule_run", "id", run_id),
                     "items": [snapshot(conn, item["queue"]["id"]) for item in items]}
            (out / "final.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            emit(progress, {"event": "finished", "published": sum(
                item["log"]["status"] == "published" for item in final["items"]),
                "scope": len(items), "run_status": final["run"]["status"]})


if __name__ == "__main__":
    main()

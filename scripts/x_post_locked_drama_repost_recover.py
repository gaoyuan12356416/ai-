#!/usr/bin/env python3
"""One audited rearm of a confirmed source Post after operator-confirmed unlock.

Never calls X, creates queues, resets counters, or recreates source Posts.
Dispatch the existing queue through the running Sidecar only after review.
"""
import argparse
import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_accounts.language import same_drama_language
from features.x_posts.service import XPostStore, _drama_episode_key, utc_now

AUDIT_TABLE = "x_post_locked_drama_repost_recovery_audit"


def recover(db_path, queue_id, x_user_id, source_post_id, *, actor, confirm_unlocked=False, apply=False):
    if not confirm_unlocked or not actor.strip() or len(actor) > 100:
        raise ValueError("Explicit operator unlock confirmation and actor are required")
    if not re.fullmatch(r"[0-9]{1,32}", str(x_user_id)) or not re.fullmatch(r"[0-9]{1,32}", str(source_post_id)):
        raise ValueError("Exact numeric X user and source Post IDs are required")
    uri = Path(db_path).resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (AUDIT_TABLE,)).fetchone()
        if exists and conn.execute(f"SELECT 1 FROM {AUDIT_TABLE} WHERE queue_id=?", (queue_id,)).fetchone():
            raise ValueError("This queue already has an unlock recovery; inspect it instead of retrying")
        queue = conn.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
        if not queue or queue["source_type"] != "drama" or queue["status"] != "failed" or queue["delivery_mode"] != "premium_relay_repost":
            raise ValueError("Only a failed frozen drama Repost can be recovered")
        account = conn.execute("SELECT id,x_user_id,status,publish_approved,drama_language FROM x_authorized_account WHERE id=?", (queue["account_id"],)).fetchone()
        if not account or account["x_user_id"] != str(x_user_id) or account["status"] != "active" or account["publish_approved"] != 1:
            raise ValueError("Target identity or publish approval changed")
        log = conn.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue_id,)).fetchone()
        relay = XPostStore._assert_relay_queue_binding(conn, queue)
        pool = conn.execute("SELECT * FROM x_post_drama_pool WHERE id=?", (queue["drama_pool_item_id"],)).fetchone()
        run = conn.execute("SELECT * FROM x_post_schedule_run WHERE id=?", (queue["schedule_run_id"],)).fetchone()
        for row in (log, relay):
            error = str(row["error_message"] if row else "").lower()
            if not row or row["status"] != "failed" or row["unknown_outcome"] or row["error_code"] != "x_upstream_error" or not all(x in error for x in ("http 403", "temporarily locked")):
                raise ValueError("Outcome must be an explicit locked-account 403, never an ambiguous write")
        if (relay["source_post_id"] != str(source_post_id) or log["x_post_id"] != str(source_post_id)
                or not relay["source_published_at"] or not relay["source_post_url"]
                or relay["repost_id"] or relay["reposted_at"] or relay["source_attempt_count"] != 1
                or relay["repost_attempt_count"] != 1 or log["attempt_count"] != 1):
            raise ValueError("Expected one confirmed source and one failed target Repost")
        if (not pool or not run or run["status"] != "completed_with_errors" or run["unknown_count"]
                or pool["status"] != "active" or pool["assigned_account_id"] != queue["account_id"]
                or pool["content_id"] != queue["content_id"] or pool["created_at"] != queue["drama_pool_created_at"]
                or pool["replay_generation"] != queue["drama_replay_generation"]
                or pool["next_sub_number"] != queue["episode_number"]
                or queue["episode_key"] != _drama_episode_key(queue["content_id"], queue["episode_number"], queue["drama_replay_generation"])
                or pool["last_error_code"] != "x_upstream_error"
                or "temporarily locked" not in pool["last_error_message"].lower()
                or not same_drama_language(account["drama_language"], pool["language"])):
            raise ValueError("Frozen drama identity, owner, progress, or parent state changed")
        XPostStore._assert_account_publish_fence(conn, queue)
        snapshot = {
            "queue": {k: queue[k] for k in ("id", "status", "account_id", "schedule_run_id", "drama_pool_item_id", "content_id", "episode_number", "episode_key", "drama_replay_generation", "relay_account_id")},
            "log": {k: log[k] for k in ("id", "status", "attempt_count", "x_post_id", "error_code", "error_message", "unknown_outcome", "updated_at")},
            "relay": dict(relay),
            "pool": {k: pool[k] for k in ("id", "status", "assigned_account_id", "next_sub_number", "published_episode_count", "last_error_code", "last_error_message")},
        }
        if apply:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL UNIQUE REFERENCES x_post_queue(id), x_user_id TEXT NOT NULL, source_post_id TEXT NOT NULL, actor TEXT NOT NULL, previous_state_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            now = utc_now()
            conn.execute(f"INSERT INTO {AUDIT_TABLE}(queue_id,x_user_id,source_post_id,actor,previous_state_json,created_at) VALUES(?,?,?,?,?,?)", (queue_id, str(x_user_id), str(source_post_id), actor, json.dumps(snapshot, ensure_ascii=False), now))
            # Preserve source IDs, all attempt counts, and the pool failure until
            # the real target Repost succeeds. source_published skips uploading.
            conn.execute("UPDATE x_post_queue SET status='publishing',updated_at=? WHERE id=?", (now, queue_id))
            conn.execute("UPDATE x_post_publish_log SET status='source_published',updated_at=? WHERE id=?", (now, log["id"]))
            conn.execute("UPDATE x_post_repost_ledger SET status='source_published',updated_at=? WHERE queue_id=?", (now, queue_id))
            XPostStore._sync_run(conn, queue_id, now)
            conn.commit()
        else:
            conn.rollback()
        return {"queue_id": queue_id, "account_id": queue["account_id"], "x_user_id": str(x_user_id), "source_post_id": str(source_post_id), "applied": apply, "source_attempt_count": relay["source_attempt_count"], "x_write_attempted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--queue-id", type=int, required=True)
    parser.add_argument("--x-user-id", required=True)
    parser.add_argument("--source-post-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--confirm-unlocked", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = recover(args.db, args.queue_id, args.x_user_id, args.source_post_id, actor=args.actor, confirm_unlocked=args.confirm_unlocked, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

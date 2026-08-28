"""Read-only, account-scoped scheduling holds derived from the publish ledger.

These holds never reconcile an unknown write or change account authorization.
A profile refresh is not evidence that X has unlocked an account.
"""

import re


def read_account_publish_blockers(conn, account_ids):
    account_ids = tuple(dict.fromkeys(int(value) for value in account_ids))
    if not account_ids:
        return {}
    requested = set(account_ids)
    placeholders = ",".join("?" for _ in account_ids)
    rows = conn.execute(
        "SELECT q.id,q.account_id,q.account_username,q.relay_account_id,"
        "q.relay_account_username,q.schedule_run_id,q.status AS queue_status,"
        "l.id AS log_id,l.status AS log_status,l.unknown_outcome,l.error_code,"
        "l.error_message,l.updated_at,l.published_at,"
        "r.status AS relay_status,r.unknown_outcome AS relay_unknown,"
        "r.source_post_id,r.source_published_at,r.reposted_at,"
        "r.error_code AS relay_error_code,r.error_message AS relay_error_message,"
        "r.updated_at AS relay_updated_at "
        "FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id "
        "LEFT JOIN x_post_repost_ledger r ON r.queue_id=q.id "
        "WHERE q.account_id IN (%s) OR q.relay_account_id IN (%s) "
        "ORDER BY q.id DESC" % (placeholders, placeholders),
        account_ids + account_ids,
    ).fetchall()
    blockers, locks, successes = {}, {}, {}

    def item(row, account_id, code):
        username = str(
            row["account_username"]
            if account_id == int(row["account_id"])
            else row["relay_account_username"]
        )
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
            username = str(account_id)
        run_id, log_id = int(row["schedule_run_id"] or 0), int(row["log_id"] or 0)
        if code == "x_post_account_locked":
            reason = "X账号临时锁定，请先登录X解锁；不自动重试"
        else:
            reason = "批次%s/日志%s有未完成或待核对的发布结果" % (run_id, log_id)
        return {
            "account_id": account_id,
            "account_username": username,
            "queue_id": int(row["id"]),
            "log_id": log_id,
            "schedule_run_id": run_id,
            "code": code,
            "message": "账号@%s：%s" % (username, reason),
        }

    for row in rows:
        target_id, relay_id = int(row["account_id"]), int(row["relay_account_id"] or 0)
        if (
            row["queue_status"] == "publishing"
            or row["unknown_outcome"]
            or row["log_status"] in {"media_uploading", "post_creating", "repost_creating"}
            or row["relay_unknown"]
            or row["relay_status"] in {"source_publishing", "reposting", "needs_review"}
        ):
            for account_id in {target_id, relay_id} & requested:
                blockers.setdefault(account_id, item(row, account_id, "x_post_account_needs_review"))
        if row["log_status"] == "published" and row["published_at"]:
            successes[target_id] = max(successes.get(target_id, ""), row["published_at"])
        if relay_id and row["source_post_id"] and row["source_published_at"]:
            successes[relay_id] = max(successes.get(relay_id, ""), row["source_published_at"])
        if row["relay_status"] == "reposted" and row["reposted_at"]:
            successes[target_id] = max(successes.get(target_id, ""), row["reposted_at"])
        error = str(row["error_message"] or row["relay_error_message"] or "").lower()
        code = row["error_code"] or row["relay_error_code"]
        if code != "x_upstream_error" or "http 403" not in error or "temporarily locked" not in error:
            continue
        # A relay source's failure belongs to the source, not the target.
        locked_id = relay_id if relay_id and not row["source_post_id"] else target_id
        if locked_id not in requested:
            continue
        timestamp = str(row["updated_at"] or row["relay_updated_at"] or "")
        if locked_id not in locks or timestamp > locks[locked_id][0]:
            locks[locked_id] = (timestamp, item(row, locked_id, "x_post_account_locked"))
    for account_id, (timestamp, blocker) in locks.items():
        if successes.get(account_id, "") <= timestamp:
            blockers.setdefault(account_id, blocker)
    for row in conn.execute(
        "SELECT id,assigned_account_id,content_id FROM x_post_drama_pool "
        "WHERE status='needs_review' AND assigned_account_id IN (%s) "
        "ORDER BY id" % placeholders, account_ids,
    ).fetchall():
        account_id = int(row["assigned_account_id"])
        blockers.setdefault(account_id, {
            "account_id": account_id, "account_username": str(account_id),
            "queue_id": 0, "log_id": 0, "schedule_run_id": 0,
            "code": "x_post_account_needs_review",
            "message": "账号%s绑定的短剧%s有待核对发布结果" % (account_id, row["content_id"]),
        })
    return {account_id: blockers[account_id] for account_id in account_ids if account_id in blockers}

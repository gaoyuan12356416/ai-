"""Check the exact AI receipt in the transaction which freezes the X post text."""
import re
import sqlite3


def caption_ready(conn, queue_id, body, eligible):
    parent = conn.execute(
        "SELECT r.trigger_source,r.external_task_key FROM x_post_queue q "
        "LEFT JOIN x_post_manual_run r ON r.id=q.manual_run_id WHERE q.id=?", (queue_id,)
    ).fetchone()
    if parent is None:
        return False
    if parent[0] != "auto_template":
        return True
    match = re.fullmatch(r"x-auto-task-([1-9][0-9]*)", str(parent[1]))
    if not match:
        return False
    ordinal = int(match[1])
    row = conn.execute("SELECT source,final_text FROM story_copy_receipt WHERE task_key=?",
                       ("tag:auto:" + match[1],)).fetchone()
    if row and row[0] == "ai" and body and row[1] == body:
        return True
    try:
        return not eligible(ordinal)
    except (OSError, sqlite3.Error):
        return False


def enabled_for(ordinal):
    from features.story_copy import store
    with store.connection(readonly=True, timeout=0.2) as c:
        return store.eligible(c, "ta", ordinal)

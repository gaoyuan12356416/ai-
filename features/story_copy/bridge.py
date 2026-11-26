"""Bounded, optional publisher integration. Never invoke a model or notification here."""

import logging
import os
import sqlite3
import time
from pathlib import Path

from . import fb_facts, store
from .model import group_key, render
from .sources import fb_task, obj, x_task

LOG = logging.getLogger(__name__)


def fb_redirect(task_id, long_url):
    from features.fb_auto_posts.links import write_short_redirect

    root = os.environ.get(
        "FB_AUTO_POST_SHORT_LINK_ROOT", "/mnt/data-disk/fb-auto-post-public/s2l/fb"
    )
    return write_short_redirect(root, task_id, long_url)


def fb_plan(selection, owner, config, page, material, task_id, timestamp):
    # The native plan already owns these facts. Generation and final selection
    # independently check the switch for this ordinal in the caption ledger.
    todo = {
        "language": material.language,
        "title": material.drama_name,
        "description": material.drama_description,
        "clip": material.material_name,
        "product": config.get("product") or config.get("material_product"),
    }
    selection["story_copy"] = todo
    try:
        if "{{m_url}}" not in str(config.get("message_template", "")):
            todo["link"] = list(owner._link_values(page, material, task_id, timestamp))
    except Exception as exc:
        LOG.warning("story_copy.optional_plan_link task=%s error=%s", task_id, type(exc).__name__)


def select(task, current, link):
    started = time.monotonic()
    deadline = started + 0.30
    attempts = 0
    while True:
        attempts += 1
        try:
            result = _select_once(task, current, link)
        except Exception as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            name = getattr(exc, "sqlite_errorname", None)
            # Python 3.9 exposes SQLite's message but not its extended error code.
            detail = str(exc) if isinstance(exc, sqlite3.Error) else ""
            if code is None and isinstance(exc, sqlite3.OperationalError):
                code = {"database is locked": 5, "database is busy": 5, "database table is locked": 6,
                        "database schema is locked": 6}.get(detail.lower().split(":")[0])
            busy = code is not None and (code & 255) in (5, 6)
            remaining = deadline - time.monotonic()
            if busy and remaining > 0 and attempts < 6:
                time.sleep(min(0.02 * 2 ** (attempts - 1), remaining))
                continue
            LOG.warning("story_copy.optional_read key=%s attempts=%d elapsed_ms=%.0f error=%s code=%s detail=%s",
                        task["key"], attempts, (time.monotonic() - started) * 1000,
                        name or type(exc).__name__, code, detail[:160])
            return "legacy", "caption_store_unavailable", current, None
        if attempts > 1:
            LOG.info("story_copy.read_recovered key=%s attempts=%d elapsed_ms=%.0f",
                     task["key"], attempts, (time.monotonic() - started) * 1000)
        return result


def _select_once(task, current, link):
    with store.connection(readonly=True, timeout=0) as c:
        if not store.eligible(c, task["family"], task["ordinal"]):
            return "off", "", current, None
        if task.get("manual"):
            return "manual", "", current, None
        row = c.execute("SELECT * FROM jobs WHERE key=?", (task["key"],)).fetchone()
        if row is None or row["state"] != "ready":
            return (
                "legacy",
                (row["reason"] if row else "") or "not_ready_at_publish",
                current,
                row["batch"] if row else None,
            )
        frozen = obj(row["payload"])
        if group_key(task) != group_key(frozen) or (
            task.get("material_id")
            and str(task["material_id"]) != str(frozen.get("material_id"))
        ):
            return "legacy", "frozen_identity_mismatch", current, row["batch"]
        if task.get("episode") != frozen.get("episode"):
            return "legacy", "episode_mismatch", current, row["batch"]
        try:
            return "ai", "", render(frozen, obj(row["draft"]), link), row["batch"]
        except Exception as exc:
            return "legacy", str(exc)[:120], current, row["batch"]


def receipt(conn, task, source, reason, text, url, batch):
    if source == "off":
        return
    conn.execute(
        "INSERT OR IGNORE INTO story_copy_receipt VALUES(?,?,?,?,?,?,?,?)",
        (
            task["key"],
            task["endpoint"],
            source,
            reason,
            text,
            url or "",
            batch,
            time.time(),
        ),
    )


def x_final(conn, log, legacy, short_url, db_path, product_url=None):
    if log["long_url"] and log["short_url"]:
        return log["post_text"]
    try:
        q = conn.execute(
            "SELECT * FROM x_post_queue WHERE id=?", (log["queue_id"],)
        ).fetchone()
        task = x_task(
            conn,
            dict(q),
            str(Path(db_path).resolve()) == str(Path(store.TAG_POSTS).resolve()),
        )
        if task is None:
            return legacy
        prev = conn.execute(
            "SELECT final_text FROM story_copy_receipt WHERE task_key=?", (task["key"],)
        ).fetchone()
        if prev:
            return prev[0]
        link = (
            product_url if "{{m_url}}" in str(q["body_template"] or "") else short_url
        )
        source, reason, body, batch = select(task, legacy, link)
        receipt(conn, task, source, reason, body, link, batch)
        return body
    except Exception as exc:
        LOG.warning("story_copy.x_fallback %s", type(exc).__name__)
        return legacy


def fb_final(conn, family, row, *, extra_link_writer=None):
    """Called inside the native claim transaction before the original task is read back."""
    conn.execute("SAVEPOINT story_copy_optional")
    try:
        data = dict(row)
        if family == "fa":
            data = fb_facts.with_plan_facts(conn, data)
        if family == "fv":
            data["_ord"] = conn.execute(
                "SELECT rowid FROM fb_video_task WHERE task_id=?", (data["task_id"],)
            ).fetchone()[0]
        task = fb_task(family, data)
        if conn.execute(
            "SELECT 1 FROM story_copy_receipt WHERE task_key=?", (task["key"],)
        ).fetchone():
            conn.execute("RELEASE story_copy_optional")
            return
        legacy = data["message_text" if family == "fa" else "caption"]
        short, long = data["short_url"], data["long_url"]
        optional_link = (
            family == "fa" and not short and len(task.get("extra_link", [])) == 2
        )
        if optional_link:
            short, long = task["extra_link"]
        source, reason, body, batch = select(task, legacy, short)
        if source == "ai" and optional_link:
            try:
                if not extra_link_writer:
                    raise ValueError("short_link_unavailable")
                extra_link_writer(data["id"], long)
            except Exception:
                source, reason, body = "legacy", "short_link_unavailable", legacy
        if source == "ai":
            if family == "fa":
                if optional_link:
                    selection = obj(data["selection_json"])
                    selection["story_copy"]["legacy_before_extra_link"] = legacy
                    conn.execute(
                        "UPDATE fb_auto_task SET selection_json=? WHERE id=?",
                        (store.dump(selection), data["id"]),
                    )
                conn.execute(
                    "UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?",
                    (body, short, long, data["id"]),
                )
            else:
                conn.execute(
                    "UPDATE fb_video_task SET caption=? WHERE task_id=?",
                    (body, data["task_id"]),
                )
        receipt(
            conn,
            task,
            source,
            reason,
            body,
            short if source == "ai" else data["short_url"],
            batch,
        )
        conn.execute("RELEASE story_copy_optional")
    except Exception as exc:
        conn.execute("ROLLBACK TO story_copy_optional")
        conn.execute("RELEASE story_copy_optional")
        LOG.warning("story_copy.fb_fallback %s", type(exc).__name__)


def restore_fb_optional_link(owner, task):
    """An AI-only redirect can fail after the initial claim. Revert before any Graph write."""
    extra = obj(task.get("selection_json")).get("story_copy", {})
    if "legacy_before_extra_link" not in extra:
        return False
    fallback = extra["legacy_before_extra_link"]
    with owner.connect() as conn:
        changed = conn.execute(
            """UPDATE fb_auto_task SET message_text=?,short_url='',long_url=''
            WHERE id=? AND status='running' AND lease_owner=? AND message_text=? AND short_url=?
            AND unknown_outcome=0 AND coalesce(graph_post_id,'')=''""",
            (
                fallback,
                task["id"],
                task["lease_owner"],
                task["message_text"],
                task["short_url"],
            ),
        ).rowcount
        if not changed:
            return False
        conn.execute(
            """UPDATE story_copy_receipt SET rowid=(SELECT max(rowid)+1 FROM story_copy_receipt),
                        source='legacy',reason='short_link_unavailable',final_text=?,short_url=''
                        WHERE task_key=? AND source='ai'""",
            (fallback, "fb:auto:" + str(task["id"])),
        )
        conn.commit()
    task.update(message_text=fallback, short_url="", long_url="")
    return True

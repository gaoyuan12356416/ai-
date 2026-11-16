"""Bounded, optional publisher integration. Never invoke a model or notification here."""

import logging
import os
import time
from pathlib import Path

from . import store
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
    try:
        with store.connection(readonly=True, timeout=0) as c:
            if not store.eligible(c, "fa", task_id):
                return
        todo = {
            "language": material.language,
            "title": material.drama_name,
            "description": material.drama_description,
            "clip": material.material_name,
            "product": config.get("product") or config.get("material_product"),
        }
        selection["story_copy"] = todo
        if "{{m_url}}" not in str(config.get("message_template", "")):
            todo["link"] = list(owner._link_values(page, material, task_id, timestamp))
    except Exception as exc:
        LOG.warning("story_copy.optional_plan %s", type(exc).__name__)


def select(task, current, link):
    # The native store always owns the original text. Losing the entire AI store returns it.
    try:
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
    except Exception as exc:
        LOG.warning("story_copy.optional_read %s", type(exc).__name__)
        return "legacy", "caption_store_unavailable", current, None


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

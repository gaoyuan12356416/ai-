"""Recover missing plan metadata only from the same frozen FB material selection."""

import logging
import sqlite3

from .sources import obj


REQUIRED = ("product", "language", "title", "description")
FIELDS = (*REQUIRED, "clip")


def with_plan_facts(db, row):
    selection = obj(row.get("selection_json"))
    info = selection.get("story_copy") or {}
    if all(info.get(k) for k in REQUIRED):
        return dict(row, selection_json=selection)
    if not all(
        row.get(k) for k in ("run_id", "template_version", "content_id", "material_id")
    ):
        return row
    # Links and final copy belong to each individual publication. Only the
    # material fields are shared, and two conflicting snapshots are ambiguous.
    columns = ", ".join("json_extract(selection_json,'$.story_copy." + k + "')" for k in FIELDS)
    try:
        peers = db.execute(
            "SELECT DISTINCT " + columns + """ FROM fb_auto_task
            WHERE run_id=? AND template_version=? AND content_id=? AND material_id=? AND id<>?
              AND json_valid(selection_json) AND json_type(selection_json,'$.story_copy')='object'
              AND coalesce(json_extract(selection_json,'$.story_copy.description'),'')<>'' LIMIT 2""",
            (row["run_id"], row["template_version"], row["content_id"], row["material_id"], row["id"]),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logging.getLogger(__name__).warning("FB plan facts read task=%s: %s", row["id"], exc)
        return row
    if len(peers) != 1:
        return row
    facts = dict(zip(FIELDS, peers[0]))
    if not all(facts[k] for k in REQUIRED) or any(
        info.get(k) and facts[k] and info[k] != facts[k] for k in FIELDS
    ):
        return row
    merged = dict(info)
    merged.update({k: v for k, v in facts.items() if v and not info.get(k)})
    return dict(row, selection_json=dict(selection, story_copy=merged))

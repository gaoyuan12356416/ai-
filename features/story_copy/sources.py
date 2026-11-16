"""Source identities and frozen metadata. Reading a source never reserves a publication."""

import json
import logging
import re

from . import store
from .model import canonical_lang, product


def obj(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def base(
    family, ordinal, key, content, lang, brand, title, description, version="", **extra
):
    facts = (
        {"series": str(description).strip()[:5400]}
        if str(description or "").strip()
        else {}
    )
    return dict(
        key=key,
        family=family,
        ordinal=int(ordinal),
        endpoint=store.SOURCES[family][0],
        content=str(content or ""),
        language=canonical_lang(lang),
        product=product(brand),
        title=str(title or ""),
        facts=facts,
        template_version=str(version or ""),
        **extra,
    )


def fb_task(family, row):
    if family == "fa":
        sel = obj(row.get("selection_json"))
        info = sel.get("story_copy") or {}
        return base(
            "fa",
            row["id"],
            "fb:auto:" + str(row["id"]),
            row["content_id"],
            info.get("language"),
            info.get("product"),
            info.get("title"),
            info.get("description"),
            row.get("template_version"),
            material_id=row.get("material_id"),
            clip=info.get("clip", ""),
            extra_link=info.get("link", []),
            manual=False,
            urgent=False,
        )
    snap = obj(row.get("snapshot_json"))
    mat = snap.get("material") or {}
    brand = (
        "moboreels"
        if snap.get("moboreels")
        else mat.get("source_app_id") or mat.get("app_id") or "dramawave"
    )
    return base(
        "fv",
        row["_ord"],
        "fb:video:" + str(row["task_id"]),
        row["content_id"],
        mat.get("language") or snap.get("language"),
        brand,
        mat.get("drama_name"),
        mat.get("drama_description"),
        snap.get("template_version"),
        material_id=row.get("material_id"),
        clip=mat.get("material_name", ""),
        manual=bool(snap.get("story_copy_manual", False)),
        urgent=row.get("source_kind") == "manual",
    )


def x_task(conn, row, tag=False):
    parent = {}
    if row.get("manual_run_id"):
        rec = conn.execute(
            "SELECT trigger_source,external_task_key,template_version,copy_contract_version FROM x_post_manual_run WHERE id=?",
            (row["manual_run_id"],),
        ).fetchone()
        if rec:
            parent = dict(rec)
    if tag and parent.get("trigger_source") == "auto_template":
        key = re.fullmatch(
            r"x-auto-task-(\d+)", str(parent.get("external_task_key", ""))
        )
        if parent.get("trigger_source") != "auto_template" or not key:
            return None
        n = int(key.group(1))
        return base(
            "ta",
            n,
            "tag:auto:" + str(n),
            row["content_id"],
            row.get("account_drama_language") or row["material_language"],
            "dramawave",
            row["drama_name"],
            row["description"],
            parent.get("template_version"),
            material_id=row["material_id"],
        )
    if (
        parent.get("trigger_source") == "auto_template"
        or int(parent.get("copy_contract_version") or 1) != 1
    ):
        return None
    drama = row.get("source_type") == "drama"
    if tag and drama:
        return None
    family = "tm" if tag else "xd" if drama else "xm"
    brand = (
        "moboreels"
        if "{{m_url}}" in str(row.get("body_template") or "")
        else "dramawave"
    )
    episode = row.get("episode_number") if drama else None
    if drama:
        pool = conn.execute(
            "SELECT app_id,content_id FROM x_post_drama_pool WHERE id=?",
            (row.get("drama_pool_item_id"),),
        ).fetchone()
        if pool:
            if str(pool["content_id"]) != str(row["content_id"]):
                episode = None
            if brand != "moboreels":
                brand = pool["app_id"] or brand
    task = base(
        family,
        row["id"],
        ("tag:queue:" if tag else "x:queue:") + str(row["id"]),
        row["content_id"],
        row.get("account_drama_language") or row["material_language"],
        brand,
        row["drama_name"],
        row["description"],
        parent.get("template_version") or row.get("body_template"),
        episode=episode,
        clip=row.get("material_name", ""),
        material_id=row["material_id"],
        urgent=bool(parent),
    )
    # Accept episode-specific evidence only when it carries both identities.
    evidence = obj(row.get("episode_evidence"))
    if (
        drama
        and episode
        and evidence.get("episode_number") == episode
        and str(evidence.get("content_id")) == task["content"]
        and evidence.get("synopsis")
    ):
        task["facts"]["episode"] = str(evidence["synopsis"])[:3500]
    return task


def tag_task(row):
    sel = obj(row.get("selection_json"))
    mat = sel.get("material") or {}
    drama = sel.get("drama") or {}
    if (
        not mat
        or not row.get("material_id")
        or str(mat.get("material_id")) != str(row["material_id"])
    ):
        return None
    if str(mat.get("content_id")) != str(row["content_id"]):
        return None
    return base(
        "ta",
        row["id"],
        "tag:auto:" + str(row["id"]),
        row["content_id"],
        row["language"],
        "dramawave",
        mat.get("drama_name") or drama.get("name"),
        mat.get("description"),
        row["template_version"],
        material_id=row["material_id"],
        clip=mat.get("material_name", ""),
        audience=(sel.get("tag_profile") or {}).get("selected_path_names", []),
    )


def initial_mark(c, family):
    row = c.execute(
        "SELECT json_extract(marks,?) FROM switches WHERE endpoint=? AND enabled=1 ORDER BY id LIMIT 1",
        ("$." + family, store.SOURCES[family][0]),
    ).fetchone()
    return int(row[0]) if row else None


def discover(c):
    for family in store.SOURCES:
        try:
            discover_family(c, family)
        except Exception as exc:
            c.rollback()
            logging.getLogger(__name__).warning(
                "story_copy.source_read %s %s", family, type(exc).__name__
            )


def discover_family(c, family):
    _, path, table = store.SOURCES[family]
    mark = initial_mark(c, family)
    if mark is None:
        return
    # Pending selection may receive facts after a later row, so retain unfinished rows in the scan.
    filters = {
        "fa": "status IN ('planned','preparing','ready','running')",
        "fv": "status IN ('queued','running')",
        "xm": "source_type='material' AND status IN ('queued','publishing')",
        "xd": "source_type='drama' AND status IN ('queued','publishing')",
        "tm": "source_type='material' AND status IN ('queued','publishing')",
        "ta": "material_id IS NOT NULL AND material_id<>'' AND status NOT IN ('published','failed','skipped','cancelled')",
    }
    with store.connection(path, readonly=True) as src:
        for row in src.execute(
            "SELECT rowid AS _ord,* FROM "
            + table
            + " WHERE rowid>? AND "
            + filters[family],
            (mark,),
        ):
            row = dict(row)
            if not store.eligible(c, family, row["_ord"]):
                continue
            task = (
                fb_task(family, row)
                if family in ("fa", "fv")
                else tag_task(row)
                if family == "ta"
                else x_task(src, row, tag=family == "tm")
            )
            if task and task["family"] == family:
                store.enqueue(c, task)
    c.commit()

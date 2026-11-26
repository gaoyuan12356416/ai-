"""Source identities and frozen metadata. Reading a source never reserves a publication."""

import json
import logging
import re
from datetime import datetime, timezone

from . import store
from .model import canonical_lang, product


DISPATCH_DB = "/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3"


def fb_publish_order(now):
    """Read the dispatcher's Facebook publish queue without taking a claim."""
    def stamp(seconds):
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")

    with store.connection(DISPATCH_DB, readonly=True, timeout=0.2) as db:
        db.execute("BEGIN")
        config = db.execute("SELECT config_json FROM pipeline_config WHERE id=1").fetchone()
        config = json.loads(config[0]) if config else {}
        # A claim removes the video from the runnable queue and acquires its own
        # resource locks. Neither event means that the publisher has taken copy.
        claimed = [r[0] for r in db.execute("""
            SELECT source_key FROM pipeline_job WHERE channel='fb' AND stage='publish'
              AND status IN ('running','legacy_running')
            ORDER BY coalesce(nullif(started_at,''),scheduled_at),id
        """)]
        if "fb" not in config.get("enabled_channels", ()) or "fb" in config.get("preparation_only_channels", ()):
            if not claimed:
                return None
            queued = []
        else:
            queued = db.execute("""
            SELECT j.source_key, row_number() OVER (
                PARTITION BY j.batch_key,(j.scheduled_at>:now) ORDER BY j.scheduled_at,j.id
            ) AS batch_position FROM pipeline_job j
            LEFT JOIN pipeline_fairness f ON f.scope='publish:fb' AND f.item=j.batch_key
            WHERE j.channel='fb' AND j.stage='publish' AND j.status IN ('queued','ready')
              AND j.hold_reason='' AND j.scheduled_at<=:near
              AND NOT EXISTS (SELECT 1 FROM pipeline_cooldown WHERE resource_key='job:'||j.id AND until_at>:now)
              AND NOT EXISTS (SELECT 1 FROM pipeline_cooldown WHERE resource_key='channel:fb' AND until_at>:now)
              AND NOT EXISTS (SELECT 1 FROM pipeline_media_gate WHERE source_key=j.source_key AND phase='pending')
              AND json_array_length(j.resource_keys_json)>0
              AND NOT EXISTS (SELECT 1 FROM json_each(j.resource_keys_json) r JOIN pipeline_cooldown cool
                              ON cool.resource_key=r.value WHERE cool.until_at>:now)
              AND NOT EXISTS (
                SELECT 1 FROM json_each(CASE WHEN EXISTS (
                    SELECT 1 FROM json_each(j.resource_keys_json) WHERE value LIKE 'fb:material:%')
                    THEN j.resource_keys_json ELSE json_array('task:'||j.source_key) END) k
                WHERE EXISTS (SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=k.value)
                   OR EXISTS (SELECT 1 FROM pipeline_resource WHERE resource_key=k.value)
                   OR EXISTS (SELECT 1 FROM pipeline_resource WHERE resource_key='prepare:'||k.value AND quarantined=1))
            ORDER BY j.scheduled_at>:now,
              CASE WHEN j.scheduled_at>:now THEN j.scheduled_at END,
              batch_position,coalesce(f.last_seq,0),j.scheduled_at,j.id
        """, {"now": stamp(now), "near": stamp(now + store.NEAR_PUBLISH_SECONDS)}).fetchall()
    ordered = claimed + [r[0] for r in queued]
    taken = fb_taken(ordered)
    return [key for key in ordered if key not in taken]


def fb_taken(keys):
    if not keys:
        return set()
    with store.connection(store.SOURCES["fa"][1], readonly=True, timeout=0.2) as native:
        return {r[0] for r in native.execute(
            "SELECT task_key FROM story_copy_receipt WHERE task_key IN (SELECT value FROM json_each(?))",
            (store.dump(keys),),
        )}


def obj(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def publish_time(*values):
    """Native schedules are UTC ISO timestamps; missing values retain FIFO priority."""
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
    return None


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
            publish_priority={"running": 4, "ready": 3, "preparing": 1}.get(row.get("status"), 0),
            publish_at=publish_time(row.get("planned_publish_at_utc")),
            publish_batch="fb:auto:" + str(row["run_id"]) if row.get("run_id") else "",
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
        # Page-pool tasks are created when their slot arrives. Start their copy
        # during video preparation, then promote again when upload can begin.
        publish_priority=(3 if row.get("prepare_state") == row.get("link_state") == "ready"
                          else 2 if row.get("source_kind") == "pool" else 0),
        publish_at=publish_time(row.get("planned_at_utc")),
        publish_batch="fb:video:" + str(row["batch_id"]) if row.get("batch_id") else "",
    )


def x_task(conn, row, tag=False):
    parent = {}
    if row.get("manual_run_id"):
        rec = conn.execute(
            # Tag and ordinary publishers keep different additive schedule schemas.
            "SELECT * FROM x_post_manual_run WHERE id=?",
            (row["manual_run_id"],),
        ).fetchone()
        if rec:
            parent = dict(rec)
    timing = parent
    if row.get("schedule_run_id"):
        rec = conn.execute(
            "SELECT * FROM x_post_schedule_run WHERE id=?",
            (row["schedule_run_id"],),
        ).fetchone()
        timing = dict(rec) if rec else {}
    publish_at = publish_time(timing.get("planned_publish_at_utc"), timing.get("scheduled_at"))
    if publish_at is None and timing.get("run_date") and timing.get("publish_time"):
        # Native fixed pool schedules store Beijing wall time separately.
        publish_at = publish_time(timing["run_date"] + "T" + timing["publish_time"] + "+08:00")
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
            publish_at=publish_at,
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
        publish_at=publish_at,
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
        publish_at=publish_time(row.get("scheduled_at_utc")),
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
    from .fb_facts import with_plan_facts

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
        if family == "fa":
            filters["fa"] += " AND NOT EXISTS (SELECT 1 FROM story_copy_receipt WHERE task_key='fb:auto:'||fb_auto_task.id)"
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
            if family == "fa":
                row = with_plan_facts(src, row)
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

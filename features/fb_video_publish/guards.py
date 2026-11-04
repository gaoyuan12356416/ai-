"""Page write mutexes and material outcome fences shared by FB publishers.

Call inside the caller's SQLite BEGIN IMMEDIATE transaction when reserving or
claiming. Tenant ownership never partitions a real Meta Page execution lock.
The optional table check permits old databases and metric-only stores.
"""
import os

AUTO_ACTIVE = ("planned", "preparing", "ready", "queued", "running", "submitted", "unknown")
MANUAL_ACTIVE = ("queued", "running", "submitted", "unknown")


def has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _unknown_reason(conn, table, page_id, material_id, *, identity="", exclude_task=""):
    where, args = "page_id=? AND (status='unknown' OR unknown_outcome=1)", [str(page_id)]
    if identity:
        where += " AND " + identity + "<>?"
        args.append(str(exclude_task))
    # An absent/corrupt historical identity cannot be made into a fictitious
    # material lock. Keep its conservative Page hold distinct from a known pair.
    unidentified = "material_id='' OR material_id GLOB '*[^0-9]*' OR substr(material_id,1,1)='0' OR length(material_id)>31"
    if conn.execute("SELECT 1 FROM " + table + " WHERE " + where + " AND (" + unidentified + ") LIMIT 1", args).fetchone():
        return "fb_video_page_unknown"
    if material_id and conn.execute("SELECT 1 FROM " + table + " WHERE " + where + " AND material_id=? LIMIT 1", [*args, str(material_id)]).fetchone():
        return "fb_video_material_unknown"
    return ""


def manual_block(conn, page_id, material_id="", *, execution=False, exclude_task=""):
    if not has_table(conn, "fb_video_task"):
        return ""
    args = [str(page_id), str(exclude_task)]
    unknown = _unknown_reason(conn, "fb_video_task", page_id, material_id, identity="task_id", exclude_task=exclude_task)
    if unknown:
        return unknown
    if execution and conn.execute("SELECT 1 FROM fb_video_task WHERE page_id=? AND task_id<>? AND status='running' LIMIT 1", args).fetchone():
        return "fb_video_page_busy"
    if material_id and conn.execute("SELECT 1 FROM fb_video_task WHERE page_id=? AND task_id<>? AND material_id=? AND status IN ('queued','running','submitted','unknown') LIMIT 1", [*args, str(material_id)]).fetchone():
        return "fb_video_material_active"
    return ""


def automatic_block(conn, page_id, material_id="", *, execution=False, exclude_task=""):
    if not has_table(conn, "fb_auto_task"):
        return ""
    args = [str(page_id), str(exclude_task)]
    unknown = _unknown_reason(conn, "fb_auto_task", page_id, material_id, identity="id", exclude_task=exclude_task)
    if not unknown and has_table(conn, "fb_auto_publish_ledger"):
        # Even a candidate's own unresolved ledger must prevent another write.
        unknown = _unknown_reason(conn, "fb_auto_publish_ledger", page_id, material_id)
    if unknown:
        return unknown
    hold_filter = " AND NOT EXISTS(SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:auto:'||fb_auto_task.id)" if has_table(conn, "pipeline_canonical_hold") else ""
    if execution and conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND id<>? AND status='running'" + hold_filter + " LIMIT 1", args).fetchone():
        return "fb_video_page_busy"
    if material_id and conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND id<>? AND material_id=? AND status IN ('planned','preparing','ready','queued','running','submitted','unknown') LIMIT 1", [*args, str(material_id)]).fetchone():
        return "fb_video_material_active"
    return ""


def manual_excluded_materials(conn, page_id, cutoff=""):
    if not has_table(conn, "fb_video_task"):
        return set()
    rows = conn.execute("""SELECT DISTINCT material_id FROM fb_video_task
        WHERE page_id=? AND (status IN ('queued','running','submitted','unknown') OR unknown_outcome=1
        OR (?<>'' AND status='published' AND created_at_utc>=?))""", (str(page_id), cutoff, cutoff))
    return {str(row[0]) for row in rows}


def published_history(conn, page_id, material_id):
    """Only stable successful receipts authorize an explicit repeat."""
    result = []
    if has_table(conn, "fb_auto_task"):
        result += [["auto", str(r[0]), str(r[1])] for r in conn.execute(
            "SELECT id,graph_post_id FROM fb_auto_task WHERE page_id=? AND material_id=? AND status='published' ORDER BY id",
            (str(page_id), str(material_id)))]
    if has_table(conn, "fb_video_task"):
        result += [["manual", str(r[0]), str(r[1])] for r in conn.execute(
            "SELECT task_id,graph_post_id FROM fb_video_task WHERE page_id=? AND material_id=? AND status='published' ORDER BY task_id",
            (str(page_id), str(material_id)))]
    return result


def prepare_available(conn, now, family, planned_at):
    """Shared capacity and earlier-due preference, under BEGIN IMMEDIATE."""
    if not has_table(conn, "fb_video_task"):
        return True
    try:
        limit = int(os.environ.get("FB_VIDEO_SHARED_GPU_PREPARES", "1"))
        if limit not in {1, 2}:
            return False
    except ValueError:
        return False
    manual = conn.execute("SELECT COUNT(*) FROM fb_video_task WHERE status IN ('queued','cancelled') AND prepare_state='preparing' AND prepare_until>?", (now,)).fetchone()[0]
    auto = 0
    if has_table(conn, "fb_auto_task"):
        auto = conn.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='preparing' AND lease_expires_at_utc>?", (now,)).fetchone()[0]
    if auto + manual >= limit or (family == "manual" and manual):
        return False
    if family == "auto":
        hold_filter = " AND NOT EXISTS(SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:video:'||fb_video_task.task_id)" if has_table(conn, "pipeline_canonical_hold") else ""
        other = conn.execute("""SELECT MIN(planned_at_utc) FROM fb_video_task
            WHERE status='queued' AND processing_mode='random_overlay' AND prepare_state='planned'
            AND prepare_retry_at<=?""" + hold_filter, (now,)).fetchone()[0]
    elif has_table(conn, "fb_auto_task"):
        hold_filter = " AND NOT EXISTS(SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:auto:'||x.id)" if has_table(conn, "pipeline_canonical_hold") else ""
        other = conn.execute("""SELECT MIN(x.planned_publish_at_utc) FROM fb_auto_task x
            JOIN fb_auto_template t ON t.id=x.template_id AND t.status='enabled' AND t.current_version=x.template_version
            WHERE x.status='planned' AND x.next_prepare_at_utc<=?""" + hold_filter, (now,)).fetchone()[0]
    else:
        other = None
    return not other or str(other) >= str(planned_at)


def install_backstops(conn):
    """Unupgraded operator CLIs still cannot bypass cross-family reservations."""
    if not has_table(conn, "fb_auto_task") or not has_table(conn, "fb_video_task"):
        return
    # Both directions are checked by SQL as a backstop for older direct writers.
    for family, table, other, active, other_active in (
        ("auto", "fb_auto_task", "fb_video_task", AUTO_ACTIVE, MANUAL_ACTIVE),
        ("manual", "fb_video_task", "fb_auto_task", MANUAL_ACTIVE, AUTO_ACTIVE),
    ):
        statuses = ",".join("'" + v + "'" for v in active)
        other_statuses = ",".join("'" + v + "'" for v in other_active)
        for event in ("INSERT", "UPDATE OF status,page_id,material_id"):
            suffix = "insert" if event == "INSERT" else "update"
            receipt_update = (" AND NOT (NEW.page_id=OLD.page_id AND NEW.material_id=OLD.material_id"
                              " AND OLD.status IN ('running','submitted','unknown')"
                              " AND (NEW.status='unknown' OR (NEW.status='submitted' AND TRIM(NEW.graph_post_id)<>'')))"
                              if suffix == "update" else "")
            # Replace the old Page-wide definition, within the caller's schema
            # transaction. Saving a receipt must remain possible while another
            # material has a live write; only running is a Page write mutex.
            conn.execute(f"DROP TRIGGER IF EXISTS fb_video_cross_{family}_{suffix}")
            conn.execute(f"""CREATE TRIGGER fb_video_cross_{family}_{suffix}
                BEFORE {event} ON {table} WHEN NEW.status IN ({statuses}) {receipt_update}
                AND EXISTS(SELECT 1 FROM {other} o WHERE o.page_id=NEW.page_id AND
                  (((o.material_id=NEW.material_id OR o.material_id='' OR o.material_id GLOB '*[^0-9]*' OR substr(o.material_id,1,1)='0' OR length(o.material_id)>31) AND (o.status='unknown' OR o.unknown_outcome=1))
                   OR (o.material_id=NEW.material_id AND o.status IN ({other_statuses}))
                   OR (NEW.status='running' AND o.status='running')))
                BEGIN SELECT RAISE(ABORT,'fb_video_cross_family_hold'); END""")

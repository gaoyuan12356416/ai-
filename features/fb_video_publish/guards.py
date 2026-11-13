"""Global Page fences shared with the automatic publisher.

Call inside the caller's SQLite BEGIN IMMEDIATE transaction when reserving or
claiming. Tenant ownership never partitions a real Meta Page execution lock.
The optional table check permits old databases and metric-only stores.
"""
import os

AUTO_ACTIVE = ("planned", "preparing", "ready", "queued", "running", "submitted", "unknown")
MANUAL_ACTIVE = ("queued", "running", "submitted", "unknown")


def has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def manual_block(conn, page_id, material_id="", *, execution=False, exclude_task=""):
    if not has_table(conn, "fb_video_task"):
        return ""
    args = [str(page_id), str(exclude_task)]
    if conn.execute("SELECT 1 FROM fb_video_task WHERE page_id=? AND task_id<>? AND (status='unknown' OR unknown_outcome=1) LIMIT 1", args).fetchone():
        return "fb_video_page_unknown"
    if execution and conn.execute("SELECT 1 FROM fb_video_task WHERE page_id=? AND task_id<>? AND status IN ('running','submitted') LIMIT 1", args).fetchone():
        return "fb_video_page_busy"
    if material_id and conn.execute("SELECT 1 FROM fb_video_task WHERE page_id=? AND task_id<>? AND material_id=? AND status IN ('queued','running','submitted','unknown') LIMIT 1", [*args, str(material_id)]).fetchone():
        return "fb_video_material_active"
    return ""


def automatic_block(conn, page_id, material_id="", *, execution=False):
    if not has_table(conn, "fb_auto_task"):
        return ""
    if conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND (status='unknown' OR unknown_outcome=1) LIMIT 1", (str(page_id),)).fetchone():
        return "fb_video_page_unknown"
    if has_table(conn, "fb_auto_publish_ledger") and conn.execute("SELECT 1 FROM fb_auto_publish_ledger WHERE page_id=? AND (status='unknown' OR unknown_outcome=1) LIMIT 1", (str(page_id),)).fetchone():
        return "fb_video_page_unknown"
    if execution and conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND status IN ('queued','running','submitted') LIMIT 1", (str(page_id),)).fetchone():
        return "fb_video_page_busy"
    if material_id and conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND material_id=? AND status IN ('planned','preparing','ready','queued','running','submitted','unknown') LIMIT 1", (str(page_id), str(material_id))).fetchone():
        return "fb_video_material_active"
    return ""


def manual_excluded_materials(conn, page_id, cutoff=""):
    if not has_table(conn, "fb_video_task"):
        return set()
    rows = conn.execute("""SELECT DISTINCT material_id FROM fb_video_task
        WHERE page_id=? AND (status IN ('queued','running','submitted','unknown')
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
        other = conn.execute("""SELECT MIN(planned_at_utc) FROM fb_video_task
            WHERE status='queued' AND processing_mode='random_overlay' AND prepare_state='planned'
            AND prepare_retry_at<=?""", (now,)).fetchone()[0]
    elif has_table(conn, "fb_auto_task"):
        other = conn.execute("""SELECT MIN(x.planned_publish_at_utc) FROM fb_auto_task x
            JOIN fb_auto_template t ON t.id=x.template_id AND t.status='enabled' AND t.current_version=x.template_version
            WHERE x.status='planned' AND x.next_prepare_at_utc<=?""", (now,)).fetchone()[0]
    else:
        other = None
    return not other or str(other) >= str(planned_at)


def install_backstops(conn):
    """Unupgraded operator CLIs still cannot bypass cross-family reservations."""
    if not has_table(conn, "fb_auto_task"):
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
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS fb_video_cross_{family}_{suffix}
                BEFORE {event} ON {table} WHEN NEW.status IN ({statuses})
                AND EXISTS(SELECT 1 FROM {other} o WHERE o.page_id=NEW.page_id AND
                  ((o.status='unknown' OR o.unknown_outcome=1)
                   OR (o.material_id=NEW.material_id AND o.status IN ({other_statuses}))
                   OR (NEW.status IN ('running','submitted') AND o.status IN ('running','submitted'))))
                BEGIN SELECT RAISE(ABORT,'fb_video_cross_family_hold'); END""")

"""FB write ownership and exact-material quarantine; no publication mutations."""
import re
from datetime import datetime, timezone


def identity_resources(page_id, material_id, stage):
    page, material = str(page_id or ""), str(material_id or "")
    if not re.fullmatch(r"[1-9][0-9]{0,30}", page):
        return [], "", ""
    page_key = "fb:page:" + page
    if not re.fullmatch(r"[1-9][0-9]{0,30}", material):
        # An unidentified legacy write cannot safely be narrowed to one asset.
        return [page_key], page_key, ""
    material_key = "fb:material:" + page + ":" + material
    return ([page_key, material_key] if stage == "publish" else [material_key]), page_key, material_key


def page_write_active(row, family, now):
    state = str(row.get("status") or "")
    if state not in {"running", "unknown"}:
        return False
    lease = row.get("execution_until" if family == "video" else "lease_expires_at_utc")
    if not lease:
        return state == "running"
    try:
        until = datetime.fromisoformat(str(lease).replace("Z", "+00:00"))
        now = (datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str)
               else now or datetime.now(timezone.utc))
        return until.tzinfo is None or until > now.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return True


def platform_attempt_count(conn, task_id):
    """Empty-credential preflight failures were historically logged as attempts."""
    count = conn.execute("""SELECT COUNT(*) FROM fb_video_attempt WHERE task_id=? AND NOT (
        result_kind='definite_failure' AND credential_id='' AND fb_user_id='' AND graph_post_id='')""",
        (task_id,)).fetchone()[0]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_fb_operation'").fetchone():
        count = max(count, conn.execute("SELECT COUNT(*) FROM pipeline_fb_operation WHERE source_key=?", ("fb:video:" + str(task_id),)).fetchone()[0])
    return count

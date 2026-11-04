"""Explicit recovery of never-selected automatic tasks; never replays a write."""
import json
import re
from datetime import datetime, timedelta

from .core import BEIJING, StoreError, utc_iso
from .effects import choose_material, load_scores
from .languages import page_language
from .strategy import cooldown_content_ids, daily_limit
from features.fb_video_publish.guards import automatic_block, manual_block, has_table


REASON = "fb_auto_page_unknown_block"


def untouched(conn, task):
    """An old skipped label alone is insufficient proof of no execution."""
    if task["status"] != "skipped" or task["skip_reason"] != REASON:
        return False
    if any(task[k] for k in ("material_id", "content_id", "source_media_url", "media_url",
                            "message_text", "short_url", "long_url", "graph_post_id",
                            "unknown_outcome", "attempt_count", "started_at_utc", "lease_owner",
                            "lease_expires_at_utc", "prepared_media_url", "prepared_at_utc")):
        return False
    key = "fb:auto:" + str(task["id"])
    for table, column, value in (
        ("fb_auto_publish_attempt", "task_id", task["id"]),
        ("fb_auto_publish_ledger", "task_id", task["id"]),
        ("pipeline_fb_claim", "source_key", key),
        ("pipeline_fb_operation", "source_key", key),
        ("pipeline_canonical_hold", "source_key", key),
    ):
        if has_table(conn, table) and conn.execute(
                "SELECT 1 FROM " + table + " WHERE " + column + "=? LIMIT 1", (value,)).fetchone():
            return False
    return True


def published_materials(conn, page_id):
    """Recovery never selects an already published Page/material, even if old."""
    result = set()
    for table in ("fb_auto_task", "fb_auto_publish_ledger", "fb_video_task"):
        if has_table(conn, table):
            result.update(str(r[0]) for r in conn.execute(
                "SELECT material_id FROM " + table + " WHERE page_id=? AND material_id<>'' "
                "AND (status IN ('published','failed_without_retry') OR graph_post_id<>'')", (page_id,)))
    return result


def recover_skipped(store, run_ids, pages, materials, snapshots, *, operation_id,
                    expected_config, apply=False, recovery_hours=24):
    """Caller supplies freshly authorized Pages and repository candidate snapshots.

    The complete final check and both reservations share one SQLite transaction.
    Preview runs identical mutations and rolls them back, including reservations
    between the two runs. No network operation is performed under the write lock.
    """
    if (not re.fullmatch(r"[0-9a-f]{32}", operation_id) or not run_ids
            or len(set(run_ids)) != len(run_ids) or len(run_ids) > 20
            or not 1 <= recovery_hours <= 24):
        raise ValueError("invalid_recovery_scope")
    by_page = {p.page_id: p for p in pages}
    now_dt = store.now_fn()
    now = utc_iso(now_dt)
    until = utc_iso(now_dt + timedelta(hours=recovery_hours))
    result = {"operation_id": operation_id, "applied": apply, "checked_at_utc": now,
              "recovery_until_utc": until, "runs": []}
    with store._lock, store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for run_index, rid in enumerate(sorted(run_ids)):
                run = conn.execute("SELECT * FROM fb_auto_run WHERE id=?", (rid,)).fetchone()
                if not run:
                    raise ValueError("run_missing")
                template = conn.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN "
                    "fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version "
                    "WHERE t.id=?", (run["template_id"],)).fetchone()
                if (not template or template["status"] != "enabled"
                        or template["current_version"] != run["template_version"]
                        or run["config_json"] != expected_config or template["config_json"] != expected_config):
                    raise StoreError("fb_auto_recovery_template_changed", "恢复期间模板已变化", 409)
                if (run["trigger_type"] != "auto" or
                        datetime.fromisoformat(run["planned_publish_at_utc"]).astimezone(BEIJING).date()
                        != now_dt.astimezone(BEIJING).date()):
                    raise ValueError("recovery_requires_same_day_auto_run")
                config = json.loads(expected_config)
                scores = load_scores(conn, config, now_dt)
                report = {"run_id": rid, "restored": [], "retained": [], "already_restored": []}
                tasks = conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id", (rid,)).fetchall()
                for task in tasks:
                    record = {"task_id": task["id"], "page_id": task["page_id"]}
                    selection = json.loads(task["selection_json"] or "{}")
                    if selection.get("recovery", {}).get("operation_id") == operation_id:
                        report["already_restored"].append(record)
                        continue
                    if task["status"] != "skipped":
                        continue
                    if task["skip_reason"] != REASON:
                        report["retained"].append(dict(record, reason=task["skip_reason"]))
                        continue
                    reason = "" if untouched(conn, task) else "existing_execution_evidence"
                    page = by_page.get(task["page_id"])
                    frozen = conn.execute("SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id=?",
                                          (rid, task["page_id"])).fetchone()
                    if not reason and (not page or not frozen):
                        reason = "page_not_in_current_authorized_scope"
                    if not reason and (page_language(page.language) != frozen["language"]
                            or page.owner_user_id != frozen["owner_user_id"]
                            or task["group_id"] not in page.group_ids):
                        reason = "page_scope_or_language_changed"
                    if not reason and daily_limit(config, task["page_id"]) == 0:
                        reason = "fb_auto_page_frequency_limit"
                    if not reason and page.eligible_token_count <= 0:
                        reason = "fb_page_missing_eligible_token"
                    if not reason:
                        reason = automatic_block(conn, page.page_id) or manual_block(conn, page.page_id)
                    material, chosen = None, {}
                    publish_at = max(task["planned_publish_at_utc"], utc_iso(now_dt + timedelta(seconds=run_index)))
                    if not reason and conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND planned_publish_at_utc=? "
                            "AND id<>? AND status IN ('planned','preparing','ready','running','submitted') LIMIT 1",
                            (task['page_id'], publish_at, task['id'])).fetchone():
                        reason = 'fb_auto_page_task_conflict'
                    if not reason:
                        snapshot = snapshots.get(page_language(page.language))
                        excluded = store._cooldown_material_ids(conn, page.page_id, int(config["cooldown_days"]))
                        excluded |= published_materials(conn, page.page_id)
                        contents = cooldown_content_ids(conn, page.page_id, publish_at,
                                                        int(config.get("drama_cooldown_hours", 0)), now)
                        candidates = [m for m in snapshot.candidates if m.content_id not in contents] if snapshot else []
                        material, chosen = choose_material(candidates, excluded, materials=materials,
                            config=config, page_id=page.page_id, slot_key=run["slot_key"], scores=scores)
                        if material is None:
                            reason = "no_current_eligible_unpublished_material"
                        else:
                            reason = automatic_block(conn, page.page_id, material.material_id) or manual_block(conn, page.page_id, material.material_id)
                    if reason:
                        report["retained"].append(dict(record, reason=reason))
                        continue
                    recovery = {"operation_id": operation_id, "original_skip_reason": task["skip_reason"],
                                "original_planned_publish_at_utc": task["planned_publish_at_utc"],
                                "original_completed_at_utc": task["completed_at_utc"],
                                "rechecked_at_utc": now, "recovery_until_utc": until,
                                "metric_generation_ids": list(snapshot.metric_generation_ids)}
                    chosen["recovery"] = recovery
                    short, long = (store._link_values(page, material, task["id"], int(now_dt.timestamp()))
                                   if "{{url}}" in config["message_template"] else ("", ""))
                    conn.execute("""UPDATE fb_auto_task SET status='planned',skip_reason='',error_code='',error_message='',
                        material_id=?,content_id=?,source_media_url=?,message_text=?,short_url=?,long_url=?,
                        completed_at_utc='',planned_publish_at_utc=?,selection_json=?,recovery_until_utc=?
                        WHERE id=? AND status='skipped' AND skip_reason=?""",
                        (material.material_id, material.content_id, material.media_url, store._message(config, material, short),
                         short, long, publish_at, json.dumps(chosen, sort_keys=True), until, task["id"], REASON))
                    conn.execute("UPDATE fb_auto_run_page SET snapshot_status='eligible',skip_reason='',eligible_token_count=? "
                                 "WHERE run_id=? AND page_id=?", (page.eligible_token_count, rid, page.page_id))
                    report["restored"].append(dict(record, material_id=material.material_id,
                                                    content_id=material.content_id, planned_publish_at_utc=publish_at))
                count = len(report["restored"])
                if count:
                    conn.execute("UPDATE fb_auto_run SET queued_tasks=queued_tasks+?,skipped_tasks=skipped_tasks-? WHERE id=?",
                                 (count, count, rid))
                    store._refresh_run(conn, rid, now)
                result["runs"].append(report)
            if apply:
                conn.commit()
            else:
                conn.rollback()
        except BaseException:
            conn.rollback()
            raise
    return result

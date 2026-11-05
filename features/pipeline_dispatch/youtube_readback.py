"""Owner-authenticated recovery of an existing video, with no platform writes."""
import json
from features.drama_synthesis.core import utc_now, _video_sync_payload


def reconcile_video(bridge, task):
    if (not task.get("video_id") or task.get("lease_owner")
            or task.get("workflow") != "reviewed_thumbnail"
            or task.get("thumbnail_status") != "succeeded"
            or task.get("processing_status") != "succeeded"
            or task.get("reviewed_phase") not in {"public", "schedule", "comment"}
            or task.get("video_state") == "published"):
        return task
    recoverable = {"youtube_visibility_unconfirmed", "youtube_schedule_unconfirmed", "youtube_public_readback_unknown"}
    if task.get("error_code") not in recoverable:
        # A UI retry can clear the current error. Recover its original typed
        # failure, rather than resuming unrelated historical unknown writes.
        with bridge.workflow.db() as conn:
            failed = conn.execute("SELECT safe_detail FROM drama_youtube_publish_event WHERE task_id=? AND phase='video' AND outcome='unknown' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
        if not failed or failed[0] not in recoverable:
            return task
    credential = bridge.workflow.channel_directory.repository.credential(
        app_id=task["app_id"], channel_local_id=task["channel_local_id"],
        account_id=task["youtube_account_id"], expected_channel_id=task["channel_id"])
    token = bridge.client.refresh_access_token(credential)
    bridge.client.verify_channel_identity(token, task["channel_id"])
    proof = bridge.client.read_reviewed_video_state(token, task["video_id"], expected_channel_id=task["channel_id"])
    public = proof.get("state") == "succeeded" and proof.get("visibility") == "public"
    scheduled = (proof.get("state") == "succeeded" and proof.get("visibility") == "private"
                 and task.get("publish_at") and not task.get("schedule_command")
                 and proof.get("preserved_status", {}).get("publishAt") == task["publish_at"])
    if not public and not scheduled:
        return task
    checked = utc_now()
    if public and task.get("publish_at") and task["publish_at"] > checked:
        return task  # Preserve the existing early-publication review rule.
    with bridge.workflow.db(True) as conn:
        current = dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (task["id"],)).fetchone())
        if current != task:  # The read can never steal a newly acquired lease.
            return current
        if public:
            published_at = proof.get("published_at") or task.get("video_published_at_utc") or checked
            comment_unknown = bool(task["comment_attempt_count"] and not task["comment_id"])
            pending = bool(task["comment_text"] and not task["comment_id"] and task["comment_status"] != "skipped")
            conn.execute("""UPDATE drama_youtube_publish SET video_state='published',
                status=?,unknown_outcome=?,public_status='succeeded',video_published_at_utc=?,
                comment_status=?,reviewed_phase=?,error_code=?,error_message=?,updated_at_utc=?,
                schedule_status=CASE WHEN publish_at<>'' THEN 'published' ELSE schedule_status END,
                schedule_command_status=CASE WHEN publish_at<>'' THEN 'confirmed' ELSE schedule_command_status END
                WHERE id=?""", (
                "unknown" if comment_unknown else "published", int(comment_unknown), published_at,
                "unknown" if comment_unknown else "queued" if pending else task["comment_status"],
                "comment" if pending else "complete", task["error_code"] if comment_unknown else "",
                task["error_message"] if comment_unknown else "", checked, task["id"]))
            payload = json.dumps(_video_sync_payload(task, task["video_id"], published_at), ensure_ascii=False, sort_keys=True)
            for kind, external in (("video", task["video_id"]), ("publish_log", str(task["id"]))):
                conn.execute("INSERT OR IGNORE INTO drama_youtube_sync_outbox(publish_id,entity_kind,external_id,payload_json,status,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?)",
                             (task["id"], kind, external, payload, "pending", checked, checked))
        else:
            conn.execute("""UPDATE drama_youtube_publish SET status='scheduled',video_state='processing',
                unknown_outcome=0,schedule_status='armed',schedule_command_status='confirmed',
                schedule_confirmed_publish_at=publish_at,error_code='',error_message='',updated_at_utc=? WHERE id=?""", (checked, task["id"]))
        detail = {"video_id": task["video_id"], "visibility": proof["visibility"],
                  "publish_at": proof.get("preserved_status", {}).get("publishAt", ""),
                  "published_at": proof.get("published_at", "")}
        conn.execute("INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                     (task["id"], "platform_readback", "public" if public else "scheduled", json.dumps(detail), checked))
    return bridge.store.youtube_task(task["id"])


def comment_continuation(task):
    if (task.get("workflow") == "reviewed_thumbnail" and task.get("video_state") == "published"
            and task.get("status") == "published" and not task.get("unknown_outcome")
            and not task.get("lease_owner") and task.get("video_id")
            and task.get("comment_text") and not task.get("comment_id")
            and not task.get("comment_attempt_count")
            and task.get("comment_status") in {"queued", "publishing"}
            and task.get("video_published_at_utc")):
        return {"kind": "comment", "video_id": task["video_id"], "scheduled_at": task["video_published_at_utc"]}
    return None

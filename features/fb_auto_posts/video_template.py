"""Media selection for frozen FB automatic-template tasks."""

SUPPORTED = ("random_overlay", "original")


def ready_original(conn, task_id, video_template, now):
    """Select the frozen source in the same transaction that reserves the task."""
    if video_template == "original":
        conn.execute("""UPDATE fb_auto_task
            SET status='ready', media_url=source_media_url, prepared_media_url=source_media_url,
                prepared_at_utc=?, prepared_profile='source', media_selection_kind='template_original',
                gpu_job_id=''
            WHERE id=? AND status='planned' AND video_template='original'""", (now, task_id))


def valid_publish_media(task):
    url = task.get("media_url")
    if not url or url != task.get("prepared_media_url"):
        return False
    same_source = url == task.get("source_media_url")
    if task.get("video_template") == "original":
        return same_source
    return not same_source or task.get("media_selection_kind") == "deadline_original"


def ready_media_sql(prefix=""):
    """SQL counterpart of valid_publish_media for atomic publication claims."""
    return f"""(TRIM({prefix}prepared_at_utc)<>'' AND TRIM({prefix}prepared_media_url)<>''
        AND {prefix}media_url={prefix}prepared_media_url
        AND CASE WHEN {prefix}video_template='original' THEN {prefix}media_url={prefix}source_media_url
                 ELSE {prefix}media_url<>{prefix}source_media_url OR {prefix}media_selection_kind='deadline_original' END)"""

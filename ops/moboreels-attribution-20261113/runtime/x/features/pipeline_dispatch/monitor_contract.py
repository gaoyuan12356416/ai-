"""Public monitor vocabulary shared by coordinator, gateway and tests.

All counters come from scoped canonical task projections. Missing measurements
are null, not zero. Completion throughput is always the trailing 60 minutes;
the selected window applies to completed/failed totals and duration samples.
"""

CHANNELS = ("x", "fb", "tt", "youtube")
STAGES = ("prepare", "publish")
# Host IDs are enrolled dynamically; the initial two are not an allowlist.
WINDOWS = ("today", "hour", "day", "week")
STATUSES = (
    "queued", "running", "ready", "completed", "published", "failed",
    "unknown", "backlog_paused",
)
CHANNEL_MODULES = {
    "x": ("x_accounts",),
    "fb": ("fb_page_posts", "fb_video_publish"),
    "tt": ("tt_posts",),
    "youtube": ("youtube_auto_publish",),
}
TOTAL_KEYS = (
    "queued", "preparing", "ready", "publishing", "prepared", "published",
    "failed", "unknown", "backlog_paused",
)

# Common: generated_at, collected_at, stale, scope (all|mine), allowed_channels,
# warnings. summary: window {start,end,timezone}, totals (TOTAL_KEYS), channels
# [{channel,stage,queued,running,ready,completed,failed,unknown,backlog_paused,
# throughput_per_hour,avg_duration_seconds,p95_duration_seconds,avg_wait_seconds,
# oldest_wait_seconds}], sources [{channel,family,state,collected_at,error_code}].
# workers: items [{host_id,label,last_seen_at,stale,cpu_percent,memory_used_bytes,
# memory_total_bytes,gpu_percent,gpu_memory_used_bytes,gpu_memory_total_bytes,
# stages:[{stage,max_workers,alive_workers,busy_workers,completed_today,
# throughput_per_hour,avg_duration_seconds}]}], capacity {version,hosts:
# [{host_id,stage,max_workers,ceiling}],global_publish_limit,channel_limits}.
# tasks: items [{id,source_key,channel,source_family,source_id,stage,status,host_id,
# worker_id,account_label,owner_name,scheduled_at,created_at,started_at,finished_at,
# updated_at,wait_seconds,duration_seconds,error_code,error_message,detail_url}],
# total,next_cursor. detail_url must point to a same-origin task page.
# workers additionally returns processes [{worker_id,host_id,stage,pid,status,
# current_task_id,current_source_key,channel,phase,elapsed_seconds,last_seen_at,
# idle_reason,capabilities}]. Job identity is omitted when outside actor scope.

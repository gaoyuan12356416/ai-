"""Scoped admission of audited zero-write schedule preflight recoveries."""
from features.pipeline_dispatch.x_manual_recovery import fingerprint


TABLE = "x_post_schedule_zero_write_resume_audit"
SCHEMA = """CREATE TABLE IF NOT EXISTS x_post_schedule_zero_write_resume_audit (
    run_id INTEGER PRIMARY KEY, identity_sha256 TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    authorized_at TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL)"""
IDENTITY_FIELDS = ("id", "slot_key", "source_type", "run_date", "publish_time", "timezone",
    "config_version", "account_ids_json", "expected_count", "body_template", "schedule_mode",
    "created_at", "random_template_enabled", "duration_policy_version")


def identity(row):
    row = dict(row)
    return {key: row.get(key) for key in IDENTITY_FIELDS}


def authorized_run_ids(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    result = []
    for row in conn.execute("SELECT r.*,a.identity_sha256 AS resume_identity_sha256 "
            "FROM x_post_schedule_run r JOIN x_post_schedule_zero_write_resume_audit a ON a.run_id=r.id "
            "WHERE r.status='claimed' AND r.queued_count=0 AND r.published_count=0 "
            "AND r.failed_count=0 AND r.unknown_count=0 AND r.error_code='' AND r.plan_attempted_at='' "
            "AND NOT EXISTS(SELECT 1 FROM x_post_queue q WHERE q.schedule_run_id=r.id)"):
        if row["resume_identity_sha256"] == fingerprint(identity(row)):
            result.append(int(row["id"]))
    return result

"""Exact, audited admission of unstarted manual runs across the X cutover.

This permission only bypasses the planning date cutoff. Normal account,
reservation, scheduling and publication checks remain authoritative.
"""
import hashlib
import json


TABLE = "x_post_manual_resume_audit"
SCHEMA = """CREATE TABLE IF NOT EXISTS x_post_manual_resume_audit (
    run_id INTEGER PRIMARY KEY, identity_sha256 TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    authorized_at TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL)"""

# Runtime status and observation timestamps may naturally advance after claim.
IDENTITY_FIELDS = ("id", "idempotency_key", "trigger_source", "run_date", "source_date",
    "account_ids_json", "material_ids_json", "body_template", "actor_user_id", "actor_name",
    "created_at", "external_task_key", "template_ref", "template_version",
    "body_template_sha256", "publish_mode", "scheduled_at", "scheduled_timezone",
    "random_template_enabled", "duration_policy_version", "actor_tenant_key", "copy_contract_version")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def identity(row):
    row = dict(row)
    return {key: row.get(key) for key in IDENTITY_FIELDS}


def authorized_run_ids(conn):
    """Read only; an older database without recovery records stays unchanged."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    result = []
    for row in conn.execute("SELECT r.*,a.identity_sha256 AS resume_identity_sha256 "
            "FROM x_post_manual_run r JOIN x_post_manual_resume_audit a ON a.run_id=r.id "
            "WHERE r.trigger_source='manual' AND r.status IN ('queued','running') "
            "AND r.queued_count=0 AND r.published_count=0 AND r.failed_count=0 AND r.unknown_count=0"):
        row = dict(row)
        if row["resume_identity_sha256"] == fingerprint(identity(row)):
            result.append(int(row["id"]))
    return result

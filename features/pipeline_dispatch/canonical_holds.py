"""Additive admission holds; canonical task history and dedupe stay intact."""
from .adapters import AdapterError, stamp

SCHEMA = """CREATE TABLE IF NOT EXISTS pipeline_canonical_hold(
    source_key TEXT PRIMARY KEY,cutoff TEXT NOT NULL,reason TEXT NOT NULL)"""


def pause_row(conn, job, cutoff, *, table, identity, state, allowed, attempt_table=None,
              attempt_identity="task_id", additional_active=None):
    """Caller owns BEGIN IMMEDIATE; all checks and insertion are one transaction."""
    if not cutoff:
        raise AdapterError("canonical_hold_cutoff_required")
    source_key, cutoff = str(job["source_key"]), stamp(cutoff)
    expected = "%s:%s:%s" % (job["channel"], job["family"], job["source_id"])
    if source_key != expected:
        raise AdapterError("canonical_hold_identity_invalid")
    row = conn.execute(f"SELECT * FROM {table} WHERE {identity}=?", (job["source_id"],)).fetchone()
    if row is None:
        raise AdapterError("canonical_task_missing")
    item = dict(row)
    scheduled = item.get("scheduled_at_utc") or item.get("planned_publish_at_utc") or item.get("planned_at_utc") or job.get("scheduled_at")
    created = item.get("created_at_utc") or item.get("created_at") or job.get("source_created_at")
    if not scheduled or not created or stamp(scheduled) > cutoff or stamp(created) > cutoff:
        raise AdapterError("canonical_task_outside_cutoff")
    if item.get(state) not in allowed or (additional_active and additional_active(item)):
        raise AdapterError("canonical_task_active_or_terminal")
    if any(item.get(k) for k in ("unknown_outcome", "publish_id", "graph_post_id", "post_id", "publish_attempt_count")):
        raise AdapterError("canonical_task_has_publish_evidence")
    if attempt_table and conn.execute(f"SELECT 1 FROM {attempt_table} WHERE {attempt_identity}=? LIMIT 1", (job["source_id"],)).fetchone():
        raise AdapterError("canonical_task_has_publish_evidence")
    existing = conn.execute("SELECT cutoff FROM pipeline_canonical_hold WHERE source_key=?", (source_key,)).fetchone()
    if existing and existing["cutoff"] != cutoff:
        raise AdapterError("canonical_hold_cutoff_conflict")
    conn.execute("INSERT OR IGNORE INTO pipeline_canonical_hold(source_key,cutoff,reason) VALUES(?,?,'cutover_overdue')", (source_key, cutoff))
    return {"held": True, "source_key": source_key, "cutoff": cutoff, "canonical_mutations": 0}

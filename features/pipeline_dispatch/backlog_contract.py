"""Read-only, source-bound permission for explicitly approved old work.

The operator owns the transaction that writes SCHEMA and committed records.
This module never removes holds, claims work, changes schedules or contacts a
platform. A permission only bypasses an old-work date boundary.
"""
import datetime as dt
import hashlib
import json
import re


SCHEMA = """CREATE TABLE IF NOT EXISTS pipeline_backlog_disposition(
    source_key TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('resume','archive')),
    state TEXT NOT NULL,source_fingerprint TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,created_at TEXT NOT NULL,details_json TEXT NOT NULL)"""

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REQUIRED = ("id", "account_id", "channel", "family")
_FROZEN = ("template_id", "template_version", "run_id", "manual_run_id", "schedule_run_id",
           "material_id", "content_id", "gpu_job_id", "source_media_url")


def _stamp(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("backlog_time_requires_timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def authorization_identity(channel, family, row):
    """Build a native identity, retaining fields already frozen at approval.

Empty material fields may be populated by normal selection later. Nonempty
material fields present at approval remain exact constraints for every phase.
"""
    if channel not in {"x", "tt", "fb", "youtube"} or not re.fullmatch(r"[a-z_]+", family):
        raise ValueError("backlog_identity_family_unsupported")
    row = dict(row)
    identity = {"id": str(row.get("id") or row.get("task_id") or ""),
                "account_id": str(row.get("account_id") or row.get("page_id") or row.get("channel_id") or ""),
                "channel": channel, "family": family}
    if any(not value for value in identity.values()):
        raise ValueError("backlog_identity_incomplete")
    identity.update(source_key="%s:%s:%s" % (channel, family, identity["id"]),
                    scheduled_at=_stamp(row.get("scheduled_at_utc") or row.get("planned_publish_at_utc") or row.get("planned_at_utc") or row.get("scheduled_at") or row.get("publish_at")),
                    created_at=_stamp(row.get("created_at") or row.get("created_at_utc")))
    for key in _FROZEN:
        if row.get(key):
            identity[key] = str(row[key])
    return identity


def source_fingerprint(identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def disposition_record(conn, source_key, row, *, schema="main"):
    """Return None, resume, archive or blocked; reads only and fails closed.

The absent table is the old-runtime compatibility case. A malformed, pending
or changed authorization is blocked. Archive never admits work even if someone
incorrectly removes its independent canonical hold.
"""
    if schema not in {"main", "pipeline_x_auto"}:
        raise ValueError("backlog_schema_invalid")
    prefix = schema + "."
    if not conn.execute("SELECT 1 FROM " + prefix + "sqlite_master WHERE type='table' AND name='pipeline_backlog_disposition'").fetchone():
        return None
    saved = conn.execute("SELECT plan_sha256,action,state,source_fingerprint,scheduled_at,created_at,details_json FROM " + prefix + "pipeline_backlog_disposition WHERE source_key=?", (source_key,)).fetchone()
    if saved is None:
        return None
    plan, action, state, fingerprint, scheduled, created, details = tuple(saved)
    if action == "archive":
        return "archive"
    try:
        if action != "resume" or state != "committed" or not _SHA.fullmatch(plan) or not _SHA.fullmatch(fingerprint):
            return "blocked"
        channel, family, _ = source_key.split(":", 2)
        current = authorization_identity(channel, family, row)
        expected = json.loads(details)["identity"]
        mandatory = {*_REQUIRED, "source_key", "scheduled_at", "created_at"}
        if (not isinstance(expected, dict) or not mandatory.issubset(expected)
                or set(expected) - mandatory - set(_FROZEN)
                or any(not isinstance(value, str) or not value for value in expected.values())
                or expected["source_key"] != source_key
                or source_fingerprint(expected) != fingerprint
                or _stamp(scheduled) != expected["scheduled_at"]
                or _stamp(created) != expected["created_at"]
                or any(current.get(key) != value for key, value in expected.items())):
            return "blocked"
        if not conn.execute("SELECT 1 FROM " + prefix + "sqlite_master WHERE type='table' AND name='pipeline_canonical_hold'").fetchone():
            return "blocked"
        if conn.execute("SELECT 1 FROM " + prefix + "pipeline_canonical_hold WHERE source_key=?", (source_key,)).fetchone():
            return "blocked"
        return "resume"
    except (KeyError, TypeError, ValueError):
        return "blocked"


def is_resume_authorized(conn, source_key, row, *, schema="main"):
    return disposition_record(conn, source_key, row, schema=schema) == "resume"

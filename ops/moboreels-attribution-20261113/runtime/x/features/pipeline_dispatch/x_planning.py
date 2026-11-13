"""Lightweight X planning; media evidence is produced by assigned nodes."""
import datetime as dt
import math
import os


def enabled(environ=None):
    source = os.environ if environ is None else environ
    return str(source.get("PIPELINE_X_DISPATCH_ENABLED", "")).lower() in {"1", "true", "yes", "on"}


def cutoff(environ=None):
    source = os.environ if environ is None else environ
    raw = str(source.get("PIPELINE_X_DISPATCH_CUTOVER_AT", "") or "")
    if not raw:
        return None
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("pipeline X cutover requires a timezone")
    return value.astimezone(dt.timezone.utc)


def deferred_candidate(account, candidate, rank, timestamp, *, duration_hint=0):
    from scripts.x_post_daily_runner import _plan_candidate
    # Identity, language, compliance, copy and link macros are still checked
    # before freezing. No downloader, prober or remote renderer runs here.
    item = _plan_candidate(account, candidate, rank, timestamp)
    hint = float(duration_hint or 0)
    if not math.isfinite(hint) or hint < 0:
        raise ValueError("invalid source duration")
    item.update(media_validation_mode="deferred", preflight_sha256="", preflight_size=0,
                preflight_duration=hint, preflight_width=0, preflight_height=0)
    return item


def queue_handoff(identity, queues, *, resumed):
    """Return actual canonical state without issuing a publication request."""
    items = [{"queue_id": q["id"], "account_id": q["account_id"], "status": q["status"]} for q in queues]
    return {**{k: identity[k] for k in ("source_type", "run_date", "publish_time", "version")},
            "resumed_existing_plan": bool(resumed), "planned_count": len(items),
            "attempted_count": 0, "published_count": sum(q["status"] == "published" for q in queues),
            "waiting_relay_count": sum(q["status"] == "waiting_relay" for q in queues),
            "status": "dispatch_queued", "results": items}

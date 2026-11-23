"""Bridge the coordinator and native ledgers without dispatching a renderer."""

import datetime as dt
import json
import uuid
from pathlib import Path

from . import media_decision

PREPARERS = {("fb", "auto"), ("fb", "video"), ("x", "queue"), ("tt", "auto")}
HOLD = "media_decision_pending"
ROLLBACK_DRAIN = Path("/run/pipeline-social-media-rollback")


def schema(c):
    media_decision.ensure_schema(c)
    c.execute("""CREATE TABLE IF NOT EXISTS pipeline_media_gate(
        source_key TEXT PRIMARY KEY, publish_at TEXT NOT NULL DEFAULT '',
        checked INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'waiting',
        ticket TEXT NOT NULL DEFAULT '', decision TEXT NOT NULL DEFAULT '{}',
        excluded TEXT NOT NULL DEFAULT '[]', retry_at TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '')""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS pipeline_media_due ON pipeline_media_gate(phase,publish_at)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS pipeline_media_delivery ON pipeline_media_gate(phase,retry_at)"
    )


def after_seconds(now, seconds):
    return (
        dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        + dt.timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds")


def observe(c, offer, now):
    if "media_context" not in offer:
        return
    key, channel = offer["source_key"], offer["channel"]
    media_decision.register_context(c, key, channel, offer["media_context"])
    if offer.get("source_status") in {
        "cancelled",
        "canceled",
        "skipped",
        "no_candidate",
    } and not any(
        offer.get(k)
        for k in (
            "source_attempt_count",
            "legacy_inflight",
            "reconcile_only",
            "unknown_outcome",
            "publish_id",
        )
    ):
        reserved = c.execute(
            "SELECT 1 FROM pipeline_media_use WHERE source_key=? AND kind<>'produced'",
            (key,),
        ).fetchone()
        if (
            reserved
            and not c.execute(
                """SELECT 1 FROM pipeline_job j WHERE j.source_key=? AND j.stage='publish'
                AND (j.external_intent OR j.status IN ('published','unknown','running','legacy_running')
                     OR EXISTS (SELECT 1 FROM pipeline_operation o WHERE o.job_id=j.id)) LIMIT 1""",
                (key,),
            ).fetchone()
        ):
            media_decision.release(c, key)
        return
    if (channel, offer["family"]) in PREPARERS:
        c.execute(
            """INSERT INTO pipeline_media_gate(source_key,publish_at,checked) VALUES(?,?,1)
             ON CONFLICT(source_key) DO UPDATE SET checked=1,publish_at=excluded.publish_at
             WHERE checked=0 OR publish_at<>excluded.publish_at""",
            (
                key,
                offer.get("media_publish_at", ""),
            ),
        )
        if (offer["media_context"] or {}).get("mode") == "image":
            c.execute(
                "UPDATE pipeline_media_gate SET phase='native' WHERE source_key=? AND phase='waiting'",
                (key,),
            )
    artifact = offer.get("artifact") or {}
    if artifact.get("sha256") and (
        offer["stage"] == "publish" or offer.get("status") in ("completed", "published")
    ):
        media_decision.record_artifact(c, key, channel, artifact, now)


def allowed(c, row):
    if (row["channel"], row["family"]) not in PREPARERS or row["stage"] != "prepare":
        return False
    if (
        row["status"] not in ("queued", "ready")
        or row["hold_reason"]
        or row["external_intent"]
    ):
        return False
    meta = json.loads(row["source_meta_json"])
    if any(
        meta.get(k)
        for k in (
            "legacy_inflight",
            "unknown_outcome",
            "source_attempt_count",
            "reconcile_only",
            "publish_id",
        )
    ):
        return False
    if row["generation"] and (
        json.loads(row["payload_json"]).get("kind")
        or c.execute(
            "SELECT 1 FROM pipeline_cloud_intent WHERE job_id=? LIMIT 1", (row["id"],)
        ).fetchone()
    ):
        return False
    return True


def consider(c, row, now, *, due_only=False):
    """Called in the same BEGIN IMMEDIATE as claim/retirement."""
    if not allowed(c, row):
        return False
    if ROLLBACK_DRAIN.exists():
        return False
    key = row["source_key"]
    gate = c.execute(
        "SELECT * FROM pipeline_media_gate WHERE source_key=?", (key,)
    ).fetchone()
    if not gate:
        if not json.loads(row["source_meta_json"]).get("canonical_fingerprint"):
            return False
        c.execute("INSERT INTO pipeline_media_gate(source_key) VALUES(?)", (key,))
        gate = c.execute(
            "SELECT * FROM pipeline_media_gate WHERE source_key=?", (key,)
        ).fetchone()
    if gate["phase"] in ("done", "native", "pending"):
        return False
    due = bool(gate["publish_at"] and gate["publish_at"] <= after_seconds(now, 300))
    if due_only and gate["checked"] and not due:
        return False
    if not gate["checked"]:
        decision = {"kind": "refresh"}
    elif (
        (row["channel"], row["family"]) == ("tt", "auto")
        and gate["phase"] != "selected"
        and (
            row["source_status"] in ("pending", "selecting")
            or not c.execute(
                "SELECT 1 FROM pipeline_media_context WHERE source_key=? AND json_extract(context_json,'$.mode')<>''",
                (key,),
            ).fetchone()
        )
    ):
        decision = {"kind": "select_source", "deadline": due}
    else:
        from .core import _job

        decision = media_decision.select(
            c,
            _job(row, True),
            at_cutoff=due,
            now=now,
            excluded=json.loads(gate["excluded"]),
        )
    if not decision:
        return False
    frozen = dict(decision, publish_at=gate["publish_at"])
    ticket = uuid.uuid4().hex
    c.execute(
        "UPDATE pipeline_media_gate SET phase='pending',ticket=?,decision=?,retry_at='',error='' WHERE source_key=?",
        (ticket, json.dumps(frozen, sort_keys=True), key),
    )
    c.execute(
        "UPDATE pipeline_job SET hold_reason=?,updated_at=? WHERE id=?",
        (HOLD, now, row["id"]),
    )
    return True


def sweep(store, limit=150):
    from .maintenance import active_holds, blocking_service

    maintenance = active_holds()
    with store.transaction() as c:
        now = store.now()
        cfg = store.config(c)
        channels = json.dumps(cfg["enabled_channels"])
        # Jobs queued by an older coordinator have no timing row. Re-read their
        # native source without taking a render slot, even on a saturated day.
        old = c.execute(
            """SELECT j.* FROM pipeline_job j LEFT JOIN pipeline_media_gate g USING(source_key)
                   WHERE g.source_key IS NULL AND j.stage='prepare' AND j.status IN ('queued','ready')
                   AND j.hold_reason='' AND j.external_intent=0
                   AND ((j.channel='fb' AND j.family IN ('auto','video')) OR (j.channel='x' AND j.family='queue')
                        OR (j.channel='tt' AND j.family='auto'))
                   AND json_extract(j.source_meta_json,'$.canonical_fingerprint') IS NOT NULL
                   AND j.channel IN (SELECT value FROM json_each(?))
                   ORDER BY j.scheduled_at,j.id LIMIT ?""",
            (channels, limit),
        ).fetchall()
        for row in old:
            if not blocking_service(row, maintenance):
                consider(c, row, now, due_only=True)
        rows = c.execute(
            """SELECT j.* FROM pipeline_media_gate g JOIN pipeline_job j
                ON j.source_key=g.source_key AND j.stage='prepare'
                WHERE g.phase IN ('waiting','selected') AND g.publish_at>'' AND g.publish_at<=?
                AND j.status IN ('queued','ready') AND j.hold_reason='' AND j.external_intent=0
                AND j.channel IN (SELECT value FROM json_each(?))
                ORDER BY g.publish_at,j.id LIMIT ?""",
            (after_seconds(now, 300), channels, limit),
        ).fetchall()
        for row in rows:
            if not blocking_service(row, maintenance):
                consider(c, row, now, due_only=True)


def pending(store, limit):
    from contextlib import closing
    from .core import _job

    with closing(store.connect()) as c:
        # A slow media RPC may finish natively after its HTTP reply times out.
        # Let every channel replay those same tickets alongside large backlogs.
        rows = c.execute(
            """WITH due AS (
                 SELECT j.id,j.source_key,
                   ROW_NUMBER() OVER (PARTITION BY j.channel
                     ORDER BY COALESCE(NULLIF(g.retry_at,''),j.updated_at),j.id) AS channel_rank
                 FROM pipeline_media_gate g JOIN pipeline_job j
                   ON j.source_key=g.source_key AND j.stage='prepare'
                 WHERE g.phase='pending' AND g.retry_at<=?
               )
               SELECT j.*,g.ticket,g.decision,g.phase FROM due
                 JOIN pipeline_job j ON j.id=due.id
                 JOIN pipeline_media_gate g ON g.source_key=due.source_key
               ORDER BY due.channel_rank,j.id LIMIT ?""",
            (store.now(), limit),
        ).fetchall()
        return [(_job(r, True), r["ticket"], json.loads(r["decision"])) for r in rows]


def close(store, job, ticket, decision, result):
    """An uncertain RPC outcome retains the original ticket for exact replay."""
    with store.transaction() as c:
        key, now = job["source_key"], store.now()
        gate = c.execute(
            "SELECT * FROM pipeline_media_gate WHERE source_key=? AND phase='pending' AND ticket=?",
            (key, ticket),
        ).fetchone()
        if not gate:
            return
        if result.get("applied") or result.get("skipped"):
            origin = decision.get("kind")
            if result.get("skipped") and not result.get("applied"):
                media_decision.release(c, key)
            c.execute(
                """UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',
                        finished_at=?,updated_at=?,source_status=?,canonical_receipt=? WHERE id=?
                        AND status IN ('queued','ready') AND hold_reason=? AND external_intent=0""",
                (
                    now,
                    now,
                    origin,
                    json.dumps(
                        {
                            "media_decision": ticket,
                            "origin": origin,
                            "reason": result.get("reason", ""),
                        }
                    ),
                    job["id"],
                    HOLD,
                ),
            )
            store.event(
                c,
                job["id"],
                "media_decision_applied",
                {
                    "origin": origin,
                    "ticket": ticket,
                    "producer_source_key": decision.get("producer_source_key", ""),
                    "reason": result.get("reason", ""),
                },
                now,
            )
            phase = "done"
        elif any(
            result.get(k)
            for k in (
                "refreshed",
                "selected",
                "candidate_invalid",
                "already_started",
                "schedule_changed",
            )
        ):
            phase = (
                "native"
                if result.get("already_started")
                else ("selected" if result.get("selected") else "waiting")
            )
            if result.get("candidate_invalid"):
                excluded = json.loads(gate["excluded"])
                sha = (decision.get("artifact") or {}).get("sha256")
                if sha and sha not in excluded:
                    excluded.append(sha)
                c.execute(
                    "UPDATE pipeline_media_gate SET excluded=? WHERE source_key=?",
                    (json.dumps(excluded), key),
                )
            if (
                result.get("candidate_invalid")
                or result.get("schedule_changed")
                or result.get("already_started")
            ):
                media_decision.release(c, key)
            store.event(
                c,
                job["id"],
                "media_decision_rechecked",
                {
                    "reason": next(
                        k
                        for k in (
                            "refreshed",
                            "selected",
                            "candidate_invalid",
                            "already_started",
                            "schedule_changed",
                        )
                        if result.get(k)
                    )
                },
                now,
            )
        else:
            code = str(result.get("reason") or "native_media_unavailable")[:100]
            c.execute(
                "UPDATE pipeline_media_gate SET retry_at=?,error=? WHERE source_key=?",
                (after_seconds(now, 10), code, key),
            )
            return
        c.execute(
            "UPDATE pipeline_media_gate SET phase=?,retry_at='',error='' WHERE source_key=?",
            (phase, key),
        )
        c.execute(
            "UPDATE pipeline_job SET hold_reason='' WHERE id=? AND hold_reason=?",
            (job["id"], HOLD),
        )

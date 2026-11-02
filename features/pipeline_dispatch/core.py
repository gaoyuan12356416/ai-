"""Single-authority leases and resource fencing; never owns platform truth.

Channel adapters retain their canonical ledgers. This database owns only
dispatch, capacity, observations, and receipt delivery. No remote worker opens it.
"""
import contextlib
import datetime as dt
import hashlib
import json
import math
import secrets
import sqlite3
from pathlib import Path

CHANNELS = ("x", "fb", "tt", "youtube")
STAGES = ("prepare", "publish")
LEASE_SECONDS = 120
# Serial long-form renders need hours to establish three completed samples.
# Keep that evidence bounded while current resource pressure gates each increase.
CAPACITY_SAMPLE_WINDOW = dt.timedelta(hours=6)
DEFAULT_CAPACITY = {
    "hosts": {
        "cpu-main": {"prepare": {"target": 2, "ceiling": 4}, "publish": {"target": 2, "ceiling": 4}},
        "hk-gpu": {"prepare": {"target": 1, "ceiling": 2}, "publish": {"target": 2, "ceiling": 4}},
    },
    "global_publish_limit": 8,
    "channel_limits": {"x": 4, "fb": 8, "tt": 4, "youtube": 2},
    "enabled_channels": [], "cutovers": {},
}
JSON_FIELDS = ("payload", "artifact", "resource_keys", "capabilities")


class DispatchError(RuntimeError):
    def __init__(self, code, message=None, status=409):
        self.code, self.status = code, status
        super().__init__(message or code)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def stamp(value=None):
    if value is None:
        value = utcnow()
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise DispatchError("timezone_required", status=400)
    return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def channel_key(value):
    value = {"facebook": "fb", "tiktok": "tt"}.get(value, value)
    if value not in CHANNELS:
        raise DispatchError("channel_invalid", status=400)
    return value


def _positive(value, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise DispatchError("capacity_invalid", status=400)
    return value


def _job(row, private=False):
    if row is None:
        return None
    data = dict(row)
    for key in JSON_FIELDS:
        data[key] = json.loads(data.pop(key + "_json"))
    data.pop("claim_hash", None)
    data.update(json.loads(data.pop("source_meta_json", "{}")))
    if not private:
        data.pop("payload", None)
        data.pop("artifact", None)
    return data


class DispatchStore:
    def __init__(self, path, clock=utcnow):
        self.path, self.clock = str(path), clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def now(self):
        return stamp(self.clock())

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextlib.contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        with contextlib.closing(self.connect()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_config (
              id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
              config_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_node (
              node_id TEXT PRIMARY KEY, capabilities_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL DEFAULT '{}', last_seen_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_enrollment (
              id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
              expires_at TEXT NOT NULL, consumed_at TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_node_auth (
              node_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
              label TEXT NOT NULL, key_version INTEGER NOT NULL DEFAULT 1,
              enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_worker (
              node_id TEXT NOT NULL, worker_id TEXT NOT NULL, stage TEXT NOT NULL,
              pid INTEGER, draining INTEGER NOT NULL DEFAULT 0, last_seen_at TEXT NOT NULL,
              observation_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(node_id,worker_id));
            CREATE TABLE IF NOT EXISTS pipeline_claim_history (
              job_id INTEGER NOT NULL,generation INTEGER NOT NULL,node_id TEXT NOT NULL,
              worker_id TEXT NOT NULL,claim_hash TEXT NOT NULL,PRIMARY KEY(job_id,generation));
            CREATE TABLE IF NOT EXISTS pipeline_job (
              id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, source_id TEXT NOT NULL,
              channel TEXT NOT NULL, family TEXT NOT NULL, batch_key TEXT NOT NULL,
              stage TEXT NOT NULL, scheduled_at TEXT NOT NULL, source_created_at TEXT NOT NULL,
              owner_user_id TEXT NOT NULL DEFAULT '', owner_tenant_key TEXT NOT NULL DEFAULT '',
              owner_name TEXT NOT NULL DEFAULT '', required_module TEXT NOT NULL DEFAULT '',
              account_label TEXT NOT NULL DEFAULT '', detail_url TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}', artifact_json TEXT NOT NULL DEFAULT '{}',
              resource_keys_json TEXT NOT NULL DEFAULT '[]', capabilities_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL, hold_reason TEXT NOT NULL DEFAULT '',
              grandfathered INTEGER NOT NULL DEFAULT 0, source_status TEXT NOT NULL DEFAULT '',
              source_meta_json TEXT NOT NULL DEFAULT '{}',
              node_id TEXT NOT NULL DEFAULT '', worker_id TEXT NOT NULL DEFAULT '',
              generation INTEGER NOT NULL DEFAULT 0, claim_hash TEXT NOT NULL DEFAULT '',
              lease_expires_at TEXT NOT NULL DEFAULT '', external_intent INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
              error_message TEXT NOT NULL DEFAULT '', canonical_receipt TEXT NOT NULL DEFAULT '',
              UNIQUE(source_key,stage));
            CREATE INDEX IF NOT EXISTS pipeline_job_ready ON pipeline_job(stage,status,scheduled_at,id);
            CREATE INDEX IF NOT EXISTS pipeline_job_scope ON pipeline_job(owner_tenant_key,owner_user_id,channel);
            CREATE INDEX IF NOT EXISTS pipeline_job_node ON pipeline_job(node_id,stage,status);
            CREATE INDEX IF NOT EXISTS pipeline_job_status_finished ON pipeline_job(status,finished_at,channel,stage);
            CREATE INDEX IF NOT EXISTS pipeline_job_channel_status_time ON pipeline_job(channel,status,finished_at,scheduled_at);
            CREATE INDEX IF NOT EXISTS pipeline_job_owner_time ON pipeline_job(owner_tenant_key,owner_user_id,channel,status,finished_at);
            CREATE TABLE IF NOT EXISTS pipeline_resource (
              resource_key TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES pipeline_job(id),
              generation INTEGER NOT NULL, quarantined INTEGER NOT NULL DEFAULT 0,
              legacy INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pipeline_legacy_resource (
              resource_key TEXT NOT NULL,job_id INTEGER NOT NULL REFERENCES pipeline_job(id),
              PRIMARY KEY(resource_key,job_id));
            CREATE TABLE IF NOT EXISTS pipeline_operation (
              operation_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES pipeline_job(id),
              generation INTEGER NOT NULL, node_id TEXT NOT NULL, operation TEXT NOT NULL,
              request_sha256 TEXT NOT NULL, account_resource TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'intent', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_receipt (
              receipt_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES pipeline_job(id),
              generation INTEGER NOT NULL, body_sha256 TEXT NOT NULL, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_delivery (
              delivery_id TEXT PRIMARY KEY,job_id INTEGER NOT NULL,generation INTEGER NOT NULL,
              claim_hash TEXT NOT NULL,request_sha256 TEXT NOT NULL,response_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_publication (
              source_key TEXT PRIMARY KEY,job_id INTEGER NOT NULL,channel TEXT NOT NULL,
              owner_user_id TEXT NOT NULL,owner_tenant_key TEXT NOT NULL,required_module TEXT NOT NULL,
              node_id TEXT NOT NULL,publish_id TEXT NOT NULL,published_at TEXT NOT NULL,proof_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS pipeline_publication_time ON pipeline_publication(channel,published_at);
            CREATE INDEX IF NOT EXISTS pipeline_publication_owner ON pipeline_publication(owner_tenant_key,owner_user_id,channel,published_at);
            CREATE TABLE IF NOT EXISTS pipeline_checkpoint (
              checkpoint_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL,
              generation INTEGER NOT NULL, phase TEXT NOT NULL,
              body_sha256 TEXT NOT NULL, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_fairness (
              scope TEXT NOT NULL, item TEXT NOT NULL, last_seq INTEGER NOT NULL,
              PRIMARY KEY(scope,item));
            CREATE TABLE IF NOT EXISTS pipeline_event (
              id INTEGER PRIMARY KEY, job_id INTEGER, event_type TEXT NOT NULL,
              details_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS pipeline_event_job_type ON pipeline_event(job_id,event_type,id);
            CREATE TABLE IF NOT EXISTS pipeline_phase_metric (
              job_id INTEGER NOT NULL, generation INTEGER NOT NULL, phase TEXT NOT NULL,
              seconds REAL NOT NULL, bytes INTEGER, created_at TEXT NOT NULL,
              PRIMARY KEY(job_id,generation,phase));
            CREATE TABLE IF NOT EXISTS pipeline_source_health (
              channel TEXT NOT NULL, family TEXT NOT NULL, state TEXT NOT NULL,
              collected_at TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(channel,family));
            CREATE TABLE IF NOT EXISTS pipeline_capacity_sample (
              node_id TEXT NOT NULL, stage TEXT NOT NULL, high_since TEXT NOT NULL DEFAULT '',
              last_change TEXT NOT NULL DEFAULT '', PRIMARY KEY(node_id,stage));
            CREATE TABLE IF NOT EXISTS pipeline_cooldown (
              resource_key TEXT PRIMARY KEY,until_at TEXT NOT NULL,reason TEXT NOT NULL);
            """)
            for table, column in (("pipeline_job", "source_meta_json"), ("pipeline_worker", "observation_json")):
                if column not in {r[1] for r in c.execute("PRAGMA table_info(" + table + ")")}:
                    c.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " TEXT NOT NULL DEFAULT '{}'")
            c.execute("INSERT OR IGNORE INTO pipeline_config VALUES(1,1,?,?)", (dumps(DEFAULT_CAPACITY), self.now()))
            from .snapshots import install
            install(c)

    @staticmethod
    def event(c, job_id, event_type, details, now):
        c.execute("INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                  (job_id, event_type, dumps(details), now))

    def config(self, conn=None):
        if conn is not None:
            row = conn.execute("SELECT * FROM pipeline_config WHERE id=1").fetchone()
            return dict(json.loads(row["config_json"]), version=row["version"])
        with contextlib.closing(self.connect()) as c:
            return self.config(c)

    def set_capacity(self, patch, actor):
        if not actor.get("is_admin"):
            raise DispatchError("admin_required", status=403)
        with self.transaction() as c:
            config = self.config(c)
            if patch.get("expected_version") != config["version"]:
                raise DispatchError("capacity_version_conflict")
            if "host_id" in patch:
                host, stage = patch["host_id"], patch.get("stage")
                if host not in config["hosts"] or stage not in STAGES:
                    raise DispatchError("capacity_pool_invalid", status=400)
                ceiling = _positive(patch.get("max_workers", patch.get("capacity")), True)
                pool = config["hosts"][host][stage]
                pool["ceiling"] = ceiling
                pool["target"] = min(pool["target"], ceiling)
                if ceiling and pool["target"] == 0:
                    pool["target"] = 1
            if "global_publish_limit" in patch:
                config["global_publish_limit"] = _positive(patch["global_publish_limit"], True)
            for channel, limit in patch.get("channel_limits", {}).items():
                config["channel_limits"][channel_key(channel)] = _positive(limit, True)
            self._save_config(c, config, actor, "capacity_changed")
            return self.config(c)

    def _save_config(self, c, config, actor, kind):
        config = dict(config)
        version = config.pop("version")
        c.execute("UPDATE pipeline_config SET version=?,config_json=?,updated_at=? WHERE id=1",
                  (version + 1, dumps(config), self.now()))
        self.event(c, None, kind, {"actor": actor.get("user_id", "system"), "version": version + 1}, self.now())

    def channel_enabled(self, channel, enabled):
        channel = channel_key(channel)
        with self.transaction() as c:
            config = self.config(c)
            channels = set(config["enabled_channels"])
            channels.add(channel) if enabled else channels.discard(channel)
            config["enabled_channels"] = sorted(channels)
            self._save_config(c, config, {}, "channel_enabled" if enabled else "channel_draining")

    @staticmethod
    def _release_finished_legacy_resources(c, source_key):
        # A source can advance from old preparation to publication between
        # collector passes. Retire only fences whose canonical stage ended.
        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id IN (SELECT id FROM pipeline_job WHERE source_key=? AND generation=0 AND status IN ('completed','published','failed'))", (source_key,))

    def _close_terminal_preparation_observation(self, c, offer):
        """A terminal native publication retires only an unclaimed observation."""
        if (offer.get("stage") != "publish" or offer.get("status") not in ("published", "failed")
                or offer.get("unknown_outcome") or offer.get("legacy_inflight")
                or (offer.get("status") == "published" and not offer.get("publish_id"))):
            return
        old = c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage='prepare'", (offer["source_key"],)).fetchone()
        if (not old or old["generation"] != 0 or old["status"] not in ("queued", "ready", "legacy_running")
                or old["claim_hash"] or old["external_intent"] or old["canonical_receipt"]):
            return
        history = c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_receipt WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_claim_history WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_checkpoint WHERE job_id=? LIMIT 1", (old["id"],) * 4).fetchone()
        if history:
            return
        # This is an index correction, not a newly measured rendering result.
        # Preserve existing timestamps; metadata_only excludes efficiency counts.
        c.execute("UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',updated_at=? WHERE id=?", (self.now(), old["id"]))
        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (old["id"],))
        c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND legacy=1", (old["id"],))
        self.event(c, old["id"], "terminal_preparation_observation_closed",
                   {"publication_status": offer["status"], "publish_id": offer.get("publish_id", ""), "previous_status": old["status"]}, self.now())

    def _converge_idle_legacy(self, c, existing, offer):
        """Retire a proven idle legacy observation, never a dispatch claim."""
        held = offer.get("status") == "backlog_paused" and offer.get("hold_reason") == "cutover_overdue"
        retrying = (existing["status"] == "legacy_running" and existing["stage"] == "prepare"
                    and offer.get("status") in ("queued", "ready"))
        if (not (held or retrying) or existing["generation"] != 0
                or existing["status"] not in ("legacy_running", "queued", "ready")
                or existing["external_intent"] or existing["claim_hash"] or existing["canonical_receipt"]
                or offer.get("legacy_inflight") is not False or offer.get("source_attempt_count") != 0
                or offer.get("unknown_outcome") or offer.get("reconcile_only") or offer.get("publish_id")):
            return
        history = c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_receipt WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_claim_history WHERE job_id=? LIMIT 1", (existing["id"],) * 3).fetchone()
        if history:
            return
        status = offer["status"]
        hold = str(offer.get("hold_reason") or ("adapter_unavailable" if offer.get("execution_available") is False else ""))
        grandfathered = 0 if held else existing["grandfathered"]
        cutoff = self.config(c)["cutovers"].get(existing["channel"])
        if (cutoff and not grandfathered and existing["source_created_at"] <= cutoff
                and stamp(offer.get("cutover_due_at") or existing["scheduled_at"]) <= cutoff):
            status, hold = "backlog_paused", "cutover_overdue"
        meta = json.loads(existing["source_meta_json"])
        meta.update({key: offer[key] for key in ("canonical_fingerprint", "source_attempt_count", "legacy_inflight", "reconcile_only", "execution_available") if key in offer})
        c.execute("UPDATE pipeline_job SET status=?,hold_reason=?,grandfathered=?,source_meta_json=?,updated_at=? WHERE id=?", (status, hold, grandfathered, dumps(meta), self.now(), existing["id"]))
        # Release only this verified pre-dispatch observation's legacy fences.
        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (existing["id"],))
        c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND legacy=1", (existing["id"],))
        self.event(c, existing["id"], "legacy_idle_observed", {"status": status, "source_status": offer.get("source_status", ""), "canonical_hold": held}, self.now())

    def _rearm_youtube_revision(self, c, existing, offer, meta):
        if existing["channel"] != "youtube" or existing["family"] != "preparation" or existing["stage"] != "prepare":
            return False
        revision = str(offer.get("source_revision") or "")
        previous = str(meta.get("source_revision") or "")
        if not previous:
            task = json.loads(existing["payload_json"]).get("request", {}).get("task", {})
            if task.get("current_version"):
                previous = str(task["current_version"]) + ":" + str(task.get("generation_retry_id") or "")
        def parsed(value):
            import re
            match = re.fullmatch(r"([1-9][0-9]*):([a-f0-9]{32})?", value)
            return (int(match[1]), match[2] or "") if match else None
        old, new = parsed(previous), parsed(revision)
        if not old and new and offer.get("source_status") != "queued_generation":
            meta["source_revision"] = revision
            c.execute("UPDATE pipeline_job SET source_meta_json=? WHERE id=?", (dumps(meta), existing["id"]))
            return False
        newer = old and new and (new[0] > old[0] or (new[0] == old[0] and new[1] and new[1] != old[1]))
        if not (newer and existing["status"] in ("completed", "failed") and not existing["external_intent"]
                and existing["hold_reason"] != "cutover_overdue" and offer.get("source_status") == "queued_generation"
                and offer.get("execution_available") is True and offer.get("status", "queued") in ("queued", "ready")
                and not offer.get("hold_reason") and not offer.get("publish_id") and not offer.get("source_attempt_count")):
            return False
        meta.update(source_revision=revision, source_attempt_count=0, legacy_inflight=False, reconcile_only=False,
                    canonical_fingerprint=offer.get("canonical_fingerprint", ""))
        c.execute("UPDATE pipeline_job SET status='queued',hold_reason='',payload_json=?,artifact_json='{}',capabilities_json=?,resource_keys_json=?,source_meta_json=?,source_status='queued_generation',node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',canonical_receipt='',error_code='',error_message='',scheduled_at=?,updated_at=? WHERE id=?",
                  (dumps(offer.get("payload", {})), dumps(offer.get("capabilities", [])), dumps(offer.get("resource_keys", [])),
                   dumps(meta), self.now(), self.now(), existing["id"]))
        c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (existing["id"],))
        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (existing["id"],))
        self.event(c, existing["id"], "youtube_revision_requested", {"previous_revision": previous, "source_revision": revision}, self.now())
        return True

    def offer(self, offer):
        now = self.now()
        channel = channel_key(offer["channel"])
        stage = offer["stage"]
        if stage not in STAGES or not str(offer.get("source_key", "")):
            raise DispatchError("offer_invalid", status=400)
        scheduled = stamp(offer.get("scheduled_at") or now)
        created = stamp(offer.get("source_created_at") or offer.get("created_at") or now)
        status = offer.get("status", "queued")
        if status not in ("queued", "ready", "completed", "published", "failed", "unknown", "legacy_running", "backlog_paused"):
            raise DispatchError("offer_status_invalid", status=400)
        continuation = offer.get("payload", {}).get("continuation") or {}
        adopting_schedule = (channel == "youtube" and stage == "publish" and offer.get("family") == "publish"
                             and offer.get("payload", {}).get("adopt_existing_schedule") is True
                             and status == "ready" and offer.get("execution_available") is True
                             and offer.get("source_status") in ("scheduled", "schedule_pending")
                             and not offer.get("legacy_inflight") and not offer.get("unknown_outcome")
                             and bool(continuation.get("video_id")) and continuation.get("video_id") == offer.get("publish_id")
                             and bool(continuation.get("scheduled_at")))
        with self.transaction() as c:
            self._close_terminal_preparation_observation(c, offer)
            if stage == "publish" and status not in ("published", "failed") and (not offer.get("legacy_inflight") or int(offer.get("source_attempt_count") or 0) > 0):
                c.execute("UPDATE pipeline_job SET status='completed',finished_at=?,updated_at=? WHERE source_key=? AND stage='prepare' AND status='legacy_running' AND generation=0", (now, now, offer["source_key"]))
            existing = c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage=?", (offer["source_key"], stage)).fetchone()
            if offer.get("metadata_only") and not existing:
                c.execute("UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',finished_at=?,updated_at=? WHERE source_key=? AND generation=0 AND status IN ('queued','ready','legacy_running')", (now, now, offer["source_key"]))
                self._release_finished_legacy_resources(c, offer["source_key"])
                return None  # Planning a queue is not a completed media render.
            if existing:
                meta = json.loads(existing["source_meta_json"])
                if adopting_schedule and existing["generation"] == 0 and not existing["external_intent"] and existing["status"] in ("unknown", "completed", "queued"):
                    try:
                        proof = json.loads(existing["canonical_receipt"] or "{}")
                    except (ValueError, TypeError):
                        proof = {"invalid": True}
                    proof_matches = isinstance(proof, dict) and (not proof or (proof.get("status") == "scheduled" and proof.get("source_key") == offer["source_key"] and proof.get("publish_id") == continuation["video_id"]))
                    history = c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_receipt WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_claim_history WHERE job_id=? LIMIT 1", (existing["id"],)*3).fetchone()
                    if proof_matches and not history:
                        meta.update({k: offer[k] for k in ("canonical_fingerprint", "source_attempt_count", "legacy_inflight", "reconcile_only", "execution_available") if k in offer})
                        c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',payload_json=?,source_meta_json=?,scheduled_at=?,started_at='',finished_at='',updated_at=? WHERE id=?", (dumps(offer["payload"]), dumps(meta), scheduled, now, existing["id"]))
                        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (existing["id"],))
                        c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND legacy=1", (existing["id"],))
                        self.event(c,existing["id"],"existing_youtube_schedule_offered",{"video_id":continuation["video_id"]},now)
                        return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), True)
                if self._rearm_youtube_revision(c, existing, offer, meta):
                    return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), True)
                if (stage == "prepare" and existing["generation"] == 0
                        and existing["status"] in ("queued", "ready") and status in ("queued", "ready")
                        and not existing["external_intent"] and not existing["claim_hash"]
                        and not existing["canonical_receipt"] and offer.get("execution_available") is True
                        and not offer.get("legacy_inflight") and not offer.get("unknown_outcome")
                        and not offer.get("reconcile_only") and not offer.get("source_attempt_count")
                        and not offer.get("publish_id") and existing["scheduled_at"] != scheduled):
                    c.execute("UPDATE pipeline_job SET scheduled_at=?,updated_at=? WHERE id=?", (scheduled, now, existing["id"]))
                    self.event(c, existing["id"], "preparation_eligibility_refreshed",
                               {"before": existing["scheduled_at"], "after": scheduled,
                                "publish_at": offer.get("cutover_due_at", "")}, now)
                if existing["generation"] == 0 and not json.loads(existing["resource_keys_json"]) and offer.get("resource_keys"):
                    c.execute("UPDATE pipeline_job SET resource_keys_json=? WHERE id=?", (dumps(sorted(set(offer["resource_keys"]))), existing["id"]))
                if offer.get("metadata_only") and not existing["generation"]:
                    c.execute("UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',finished_at=?,updated_at=? WHERE id=? AND status<>'backlog_paused'", (now, now, existing["id"]))
                if offer.get("continuation_due") and existing["channel"] == "youtube" and existing["status"] == "ready" and json.loads(existing["payload_json"]).get("continuation"):
                    c.execute("UPDATE pipeline_job SET scheduled_at=?,updated_at=? WHERE id=?", (now, now, existing["id"]))
                if stage == "prepare" and existing["status"] == "legacy_running" and not offer.get("legacy_inflight") and (offer.get("source_status") in ("ready", "review", "enqueue_pending", "completed") or offer.get("metadata_only")):
                    c.execute("UPDATE pipeline_job SET status='completed',finished_at=?,updated_at=? WHERE id=? AND generation=0", (stamp(offer["finished_at"]) if offer.get("finished_at") else now, now, existing["id"]))
                if offer.get("cutover_due_at"):
                    meta["cutover_due_at"] = offer["cutover_due_at"]
                    c.execute("UPDATE pipeline_job SET source_meta_json=? WHERE id=?", (dumps(meta), existing["id"]))
                if offer.get("ancestor_source_keys"):
                    meta["ancestor_source_keys"] = list(offer["ancestor_source_keys"])
                    c.execute("UPDATE pipeline_job SET source_meta_json=? WHERE id=?", (dumps(meta), existing["id"]))
                cutoff = self.config(c)["cutovers"].get(channel)
                in_time = not cutoff or (offer.get("started_at") and stamp(offer["started_at"]) <= cutoff)
                inherited = any(c.execute("SELECT 1 FROM pipeline_job WHERE source_key=? AND grandfathered=1", (str(key),)).fetchone() for key in meta.get("ancestor_source_keys", []))
                if inherited or (offer.get("legacy_inflight") and in_time and existing["status"] != "backlog_paused"):
                    c.execute("UPDATE pipeline_job SET grandfathered=1 WHERE id=?", (existing["id"],))
                # Canonical observations cannot reset a claim, failure, hold or write intent.
                c.execute("UPDATE pipeline_job SET source_status=? WHERE id=?",
                          (str(offer.get("source_status", "")), existing["id"]))
                if not existing["owner_tenant_key"] and offer.get("owner_tenant_key") and str(offer.get("owner_user_id", "")) == existing["owner_user_id"]:
                    c.execute("UPDATE pipeline_job SET owner_tenant_key=?,owner_name=? WHERE id=?", (offer["owner_tenant_key"], str(offer.get("owner_name") or ""), existing["id"]))
                if existing["hold_reason"] in ("adapter_unavailable", "canonical_link_pending", "canonical_review_or_enqueue") and offer.get("execution_available") is True:
                    c.execute("UPDATE pipeline_job SET hold_reason='' WHERE id=? AND generation=0", (existing["id"],))
                if existing["generation"] == 0 and (existing["status"] != "backlog_paused" or status in ("unknown", "legacy_running")) and status in ("completed", "published", "failed", "unknown", "legacy_running"):
                    c.execute("UPDATE pipeline_job SET status=?,started_at=?,finished_at=?,updated_at=?,canonical_receipt=? WHERE id=?", (status, stamp(offer["started_at"]) if offer.get("started_at") else existing["started_at"], stamp(offer["finished_at"]) if offer.get("finished_at") else existing["finished_at"], now, str(offer.get("canonical_receipt", "")), existing["id"]))
                    if status in ("unknown", "legacy_running"):
                        for resource in offer.get("resource_keys", []):
                            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (resource, existing["id"]))
                    if status in ("completed", "published", "failed"):
                        c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND legacy=1", (existing["id"],))
                        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (existing["id"],))
                    existing = c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone()
                self._converge_idle_legacy(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), offer)
                self._release_finished_legacy_resources(c, offer["source_key"])
                return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), True)
            cutoff = self.config(c)["cutovers"].get(channel)
            in_time = not cutoff or (offer.get("started_at") and stamp(offer["started_at"]) <= cutoff)
            grandfathered = bool(offer.get("legacy_inflight") and in_time) or bool(c.execute(
                "SELECT 1 FROM pipeline_job WHERE source_key=? AND grandfathered=1", (offer["source_key"],)).fetchone())
            for ancestor in offer.get("ancestor_source_keys", []):
                if c.execute("SELECT 1 FROM pipeline_job WHERE source_key=? AND grandfathered=1", (str(ancestor),)).fetchone():
                    grandfathered = True
            cutoff = self.config(c)["cutovers"].get(channel)
            hold = ""
            if offer.get("execution_available") is False:
                hold = str(offer.get("hold_reason") or "adapter_unavailable")
            if cutoff and stamp(offer.get("cutover_due_at") or scheduled) <= cutoff and created <= cutoff and not grandfathered and not adopting_schedule and status in ("queued", "ready"):
                status, hold = "backlog_paused", "cutover_overdue"
            if offer.get("legacy_inflight"):
                status = "legacy_running"
            values = {
                "source_key": str(offer["source_key"]), "source_id": str(offer.get("source_id", "")),
                "channel": channel, "family": str(offer.get("family", "")),
                "batch_key": str(offer.get("batch_key") or offer["source_key"]), "stage": stage,
                "scheduled_at": scheduled, "source_created_at": created,
                "owner_user_id": str(offer.get("owner_user_id", "")), "owner_tenant_key": str(offer.get("owner_tenant_key", "")),
                "owner_name": str(offer.get("owner_name", "")), "required_module": str(offer.get("required_module") or next(iter(offer.get("required_modules", [])), "")),
                "account_label": str(offer.get("account_label", "")), "detail_url": str(offer.get("detail_url", offer.get("details_url", ""))),
                "status": status, "hold_reason": hold, "grandfathered": int(grandfathered),
                "source_status": str(offer.get("source_status", "")), "created_at": now, "updated_at": now,
                "started_at": stamp(offer["started_at"]) if offer.get("started_at") else "",
                "finished_at": stamp(offer["finished_at"]) if offer.get("finished_at") else "",
                "canonical_receipt": str(offer.get("canonical_receipt", "")),
                "source_meta_json": dumps({k: offer[k] for k in ("canonical_fingerprint", "source_attempt_count", "legacy_inflight", "reconcile_only", "execution_available", "owner_namespace", "native_owner_id", "ancestor_source_keys", "cutover_due_at", "source_revision") if k in offer}),
            }
            for key in JSON_FIELDS:
                values[key + "_json"] = dumps(offer.get(key, [] if key in ("resource_keys", "capabilities") else {}))
            keys = list(values)
            cursor = c.execute("INSERT INTO pipeline_job(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(values[k] for k in keys))
            job_id = cursor.lastrowid
            if status in ("legacy_running", "unknown"):
                for resource in sorted(set(offer.get("resource_keys", []))):
                    # Several historical unknowns may share an account. Each
                    # remains a separate fence until every receipt is resolved.
                    c.execute("INSERT INTO pipeline_legacy_resource VALUES(?,?)", (resource, job_id))
            self._release_finished_legacy_resources(c, offer["source_key"])
            self.event(c, job_id, "offered", {"status": status}, now)
            return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone(), True)

    def cutover(self, channel, cutoff, grandfathered_source_keys=()):
        channel, cutoff = channel_key(channel), stamp(cutoff)
        with self.transaction() as c:
            config = self.config(c)
            previous = config["cutovers"].get(channel)
            if previous and previous != cutoff:
                raise DispatchError("cutover_already_frozen")
            config["cutovers"][channel] = cutoff
            if not previous:
                # Before a cutoff is known the collector conservatively marks
                # native work as in flight. Freeze only the actual cutoff-era
                # roots; a later native start must not exempt its descendants.
                c.execute("UPDATE pipeline_job SET grandfathered=0 WHERE channel=?", (channel,))
                c.execute("UPDATE pipeline_job SET grandfathered=1 WHERE channel=? AND status='legacy_running' AND started_at<>'' AND started_at<=?", (channel, cutoff))
            for key in grandfathered_source_keys:
                c.execute("UPDATE pipeline_job SET grandfathered=1 WHERE channel=? AND source_key=?", (channel, key))
            # Exact task lineage may be collected after its child. Close only
            # that graph; never infer exemptions from a shared batch/account.
            rows = c.execute("SELECT id,source_key,source_meta_json,grandfathered FROM pipeline_job WHERE channel=?", (channel,)).fetchall()
            approved = set(map(str, grandfathered_source_keys)) | {row["source_key"] for row in rows if row["grandfathered"]}
            while True:
                descendants = [row for row in rows if row["source_key"] not in approved and approved.intersection(json.loads(row["source_meta_json"]).get("ancestor_source_keys", []))]
                if not descendants:
                    break
                for row in descendants:
                    approved.add(row["source_key"])
                    c.execute("UPDATE pipeline_job SET grandfathered=1 WHERE channel=? AND source_key=?", (channel, row["source_key"]))
            eligible = c.execute("SELECT id,scheduled_at,source_meta_json FROM pipeline_job WHERE channel=? AND source_created_at<=? AND status IN ('queued','ready') AND grandfathered=0 AND external_intent=0", (channel,cutoff)).fetchall()
            paused = [row["id"] for row in eligible if stamp(json.loads(row["source_meta_json"]).get("cutover_due_at") or row["scheduled_at"]) <= cutoff]
            c.executemany("UPDATE pipeline_job SET status='backlog_paused',hold_reason='cutover_overdue',updated_at=? WHERE id=?", [(self.now(), identity) for identity in paused])
            self._save_config(c, config, {}, "cutover_frozen")
            return {"paused": len(paused), "cutoff": cutoff}

    def create_enrollment(self, label, actor):
        if not actor.get("is_admin"):
            raise DispatchError("admin_required", status=403)
        label = str(label or "").strip()
        if not label or len(label) > 100:
            raise DispatchError("node_label_invalid", status=400)
        token, identity = secrets.token_urlsafe(32), secrets.token_hex(12)
        expires = stamp(self.clock() + dt.timedelta(minutes=15))
        with self.transaction() as c:
            c.execute("INSERT INTO pipeline_enrollment(id,token_hash,label,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?)", (identity, digest(token), label, expires, str(actor.get("user_id", "")), self.now()))
            self.event(c, None, "node_enrollment_created", {"enrollment_id": identity, "actor": actor.get("user_id", "")}, self.now())
        return {"enrollment_id": identity, "token": token, "expires_at": expires}

    def consume_enrollment(self, enrollment_token, node_id):
        import re
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", str(node_id or "")):
            raise DispatchError("node_invalid", status=400)
        with self.transaction() as c:
            row = c.execute("SELECT * FROM pipeline_enrollment WHERE token_hash=?", (digest(enrollment_token),)).fetchone()
            if not row or row["consumed_at"] or row["expires_at"] <= self.now():
                raise DispatchError("enrollment_invalid_or_expired", status=403)
            if c.execute("SELECT 1 FROM pipeline_node_auth WHERE node_id=?", (node_id,)).fetchone():
                raise DispatchError("node_already_enrolled")
            bearer = secrets.token_urlsafe(48)
            c.execute("INSERT INTO pipeline_node_auth(node_id,token_hash,label,created_at) VALUES(?,?,?,?)", (node_id, digest(bearer), row["label"], self.now()))
            c.execute("UPDATE pipeline_enrollment SET consumed_at=? WHERE id=?", (self.now(), row["id"]))
            self.event(c, None, "node_enrolled", {"node_id": node_id}, self.now())
            return {"node_id": node_id, "token": bearer, "key_version": 1}

    def authenticate_node(self, token):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT node_id,key_version FROM pipeline_node_auth WHERE token_hash=? AND enabled=1", (digest(token),)).fetchone()
            return dict(row) if row else None

    def register_node(self, node_id, capabilities, metrics=None):
        if not isinstance(capabilities, list) or not all(isinstance(x, str) and len(x) <= 120 for x in capabilities):
            raise DispatchError("node_invalid", status=400)
        with self.transaction() as c:
            config = self.config(c)
            if node_id not in config["hosts"]:
                if not c.execute("SELECT 1 FROM pipeline_node_auth WHERE node_id=? AND enabled=1", (node_id,)).fetchone():
                    raise DispatchError("node_not_enrolled", status=403)
                metrics = metrics or {}
                cores = max(1, int(metrics.get("cpu_count", 1)))
                gpu = int(metrics.get("gpu_count", 0)) > 0
                ceiling = max(1, min(8, (cores - max(1, cores // 8)) // (2 if gpu else 4)))
                config["hosts"][node_id] = {
                    "prepare": {"target": min(1 if gpu else 2, ceiling), "ceiling": ceiling},
                    "publish": {"target": 2, "ceiling": 4},
                }
                self._save_config(c, config, {}, "node_capacity_initialized")
            c.execute("INSERT INTO pipeline_node VALUES(?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET capabilities_json=excluded.capabilities_json,metrics_json=excluded.metrics_json,last_seen_at=excluded.last_seen_at", (node_id, dumps(sorted(set(capabilities))), dumps(metrics or {}), self.now()))
            return {"accepted": True, "capacity": self.config(c)["hosts"][node_id], "lease_seconds": LEASE_SECONDS}

    def worker_heartbeat(self, node_id, worker_id, stage, pid=None, draining=False, observation=None):
        if stage not in STAGES or not worker_id:
            raise DispatchError("worker_invalid", status=400)
        with self.transaction() as c:
            if not c.execute("SELECT 1 FROM pipeline_node WHERE node_id=?", (node_id,)).fetchone():
                raise DispatchError("node_not_registered")
            c.execute("INSERT INTO pipeline_worker(node_id,worker_id,stage,pid,draining,last_seen_at,observation_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(node_id,worker_id) DO UPDATE SET stage=excluded.stage,pid=excluded.pid,draining=excluded.draining,last_seen_at=excluded.last_seen_at,observation_json=excluded.observation_json", (node_id, worker_id, stage, pid, int(draining), self.now(), dumps(observation or {})))
            return {"accepted": True, "capacity": self.config(c)["hosts"][node_id][stage]}

    def _expire(self, c):
        now = self.now()
        for row in c.execute("SELECT * FROM pipeline_job WHERE status='running' AND lease_expires_at<=?", (now,)).fetchall():
            uncertain = bool(row["external_intent"])
            c.execute("UPDATE pipeline_job SET status=?,error_code=?,updated_at=? WHERE id=?", ("unknown" if uncertain else "queued", "lease_expired_after_intent" if uncertain else "lease_expired", now, row["id"]))
            if uncertain:
                c.execute("UPDATE pipeline_resource SET quarantined=1 WHERE job_id=?", (row["id"],))
            else:
                c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (row["id"],))
            self.event(c, row["id"], "lease_expired", {"unknown": uncertain}, now)

    def expire(self):
        with self.transaction() as c:
            self._expire(c)

    def claim(self, node_id, worker_id, stage):
        if stage not in STAGES:
            raise DispatchError("stage_invalid", status=400)
        now = self.now()
        with self.transaction() as c:
            self._expire(c)
            node = c.execute("SELECT * FROM pipeline_node WHERE node_id=?", (node_id,)).fetchone()
            worker = c.execute("SELECT * FROM pipeline_worker WHERE node_id=? AND worker_id=? AND stage=?", (node_id, worker_id, stage)).fetchone()
            if not node or not worker or worker["draining"]:
                return {"job": None, "idle_reason": "draining" if worker and worker["draining"] else "node_offline"}
            fresh = stamp(self.clock() - dt.timedelta(seconds=45))
            if node["last_seen_at"] < fresh or worker["last_seen_at"] < fresh:
                return {"job": None, "idle_reason": "node_offline"}
            config = self.config(c)
            active = c.execute("SELECT count(*) FROM pipeline_job WHERE node_id=? AND stage=? AND status='running'", (node_id, stage)).fetchone()[0]
            if active >= config["hosts"][node_id][stage]["target"]:
                return {"job": None, "idle_reason": "capacity_limit"}
            if c.execute("SELECT 1 FROM pipeline_job WHERE node_id=? AND worker_id=? AND status='running'", (node_id, worker_id)).fetchone():
                return {"job": None, "idle_reason": "worker_busy"}
            if stage == "publish" and c.execute("SELECT count(*) FROM pipeline_job WHERE stage='publish' AND status IN ('running','legacy_running')").fetchone()[0] >= config["global_publish_limit"]:
                return {"job": None, "idle_reason": "global_publish_limit"}
            capabilities = set(json.loads(node["capabilities_json"]))
            rows = c.execute("""SELECT j.* FROM pipeline_job j
              LEFT JOIN pipeline_fairness fc ON fc.scope=? AND fc.item=j.channel
              LEFT JOIN pipeline_fairness fb ON fb.scope=?||j.channel AND fb.item=j.batch_key
              WHERE j.stage=? AND j.status IN ('queued','ready') AND j.hold_reason='' AND j.scheduled_at<=?
              ORDER BY COALESCE(fc.last_seq,0),COALESCE(fb.last_seq,0),j.scheduled_at,j.id""",
              (stage, stage + ":", stage, now)).fetchall()
            blocked = []
            for row in rows:
                if row["channel"] not in config["enabled_channels"]:
                    blocked.append("channel_disabled")
                    continue
                if not set(json.loads(row["capabilities_json"])).issubset(capabilities):
                    blocked.append("capability_unavailable")
                    continue
                resources = sorted(set(json.loads(row["resource_keys_json"])))
                if not resources:
                    blocked.append("account_identity_missing")
                    continue  # All current channel work belongs to an account, Page, or YouTube channel.
                if any(c.execute("SELECT 1 FROM pipeline_resource WHERE resource_key=? UNION ALL SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=? LIMIT 1", (r, r)).fetchone() for r in resources):
                    blocked.append("account_busy")
                    continue
                if stage == "publish":
                    if any(c.execute("SELECT 1 FROM pipeline_cooldown WHERE resource_key=? AND until_at>?", (key, now)).fetchone() for key in resources + ["channel:" + row["channel"]]):
                        blocked.append("cooldown")
                        continue
                    if c.execute("SELECT count(*) FROM pipeline_job WHERE channel=? AND stage='publish' AND status IN ('running','legacy_running')", (row["channel"],)).fetchone()[0] >= config["channel_limits"][row["channel"]]:
                        blocked.append("channel_limit")
                        continue
                # A new preparation must also yield to an older ready publish
                # of the same identity, otherwise a busy prep pool can keep
                # reacquiring the account before its completed media is sent.
                earlier = c.execute("SELECT resource_keys_json FROM pipeline_job WHERE stage='publish' AND status IN ('queued','ready') AND hold_reason='' AND channel=? AND scheduled_at<=? AND (scheduled_at<? OR (scheduled_at=? AND id<?))", (row["channel"], now, row["scheduled_at"], row["scheduled_at"], row["id"])).fetchall()
                if any(set(resources).intersection(json.loads(r[0])) for r in earlier):
                    blocked.append("account_ordering")
                    continue
                token, generation = secrets.token_urlsafe(32), row["generation"] + 1
                expires = stamp(self.clock() + dt.timedelta(seconds=LEASE_SECONDS))
                c.execute("UPDATE pipeline_job SET status='running',node_id=?,worker_id=?,generation=?,claim_hash=?,lease_expires_at=?,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,updated_at=?,error_code='',error_message='' WHERE id=?", (node_id, worker_id, generation, digest(token), expires, now, now, row["id"]))
                c.execute("INSERT INTO pipeline_claim_history VALUES(?,?,?,?,?)", (row["id"], generation, node_id, worker_id, digest(token)))
                for resource in resources:
                    c.execute("INSERT INTO pipeline_resource VALUES(?,?,?,0,0)", (resource, row["id"], generation))
                self.event(c, row["id"], "claimed", {"node_id": node_id, "worker_id": worker_id, "generation": generation}, now)
                seq = c.execute("SELECT max(id) FROM pipeline_event").fetchone()[0]
                for scope, item in ((stage, row["channel"]), (stage + ":" + row["channel"], row["batch_key"])):
                    c.execute("INSERT INTO pipeline_fairness VALUES(?,?,?) ON CONFLICT(scope,item) DO UPDATE SET last_seq=excluded.last_seq", (scope, item, seq))
                result = _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (row["id"],)).fetchone(), True)
                result["claim_token"] = token
                return {"job": result}
            return {"job": None, "idle_reason": blocked[0] if blocked else "no_eligible_task", "idle_reasons": sorted(set(blocked))}

    def _validate_claim(self, c, claim, allow_expired=False):
        row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (claim.get("job_id", claim.get("id")),)).fetchone()
        if not row or row["generation"] != claim.get("generation") or row["node_id"] != claim.get("node_id") or row["worker_id"] != claim.get("worker_id") or not secrets.compare_digest(row["claim_hash"], digest(claim.get("claim_token", ""))):
            raise DispatchError("claim_conflict")
        if not allow_expired and (row["status"] != "running" or row["lease_expires_at"] <= self.now()):
            raise DispatchError("claim_expired")
        return row

    def get_job(self, job_id):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise DispatchError("job_not_found", status=404)
            return _job(row, True)

    def heartbeat(self, claim, phase=None, metrics=None):
        with self.transaction() as c:
            row = self._validate_claim(c, claim)
            expires = stamp(self.clock() + dt.timedelta(seconds=LEASE_SECONDS))
            c.execute("UPDATE pipeline_job SET lease_expires_at=?,updated_at=? WHERE id=?", (expires, self.now(), row["id"]))
            if phase:
                c.execute("UPDATE pipeline_worker SET last_seen_at=?,observation_json=? WHERE node_id=? AND worker_id=?", (self.now(), dumps({"phase": str(phase)[:80]}), row["node_id"], row["worker_id"]))
            return {"accepted": True, "lease_expires_at": expires}

    def defer_unstarted(self, claim, required_capabilities, reason="capability_unavailable"):
        """Pin an unstarted preparation to its frozen executor capabilities."""
        if not isinstance(required_capabilities, list) or not required_capabilities or not all(
                isinstance(value, str) and 0 < len(value) <= 120 for value in required_capabilities):
            raise DispatchError("capabilities_invalid", status=400)
        with self.transaction() as c:
            row = self._validate_claim(c, claim)
            if row["stage"] != "prepare" or row["external_intent"] or c.execute(
                    "SELECT 1 FROM pipeline_operation WHERE job_id=? AND generation=? LIMIT 1",
                    (row["id"], row["generation"])).fetchone():
                raise DispatchError("operation_requires_reconciliation")
            c.execute("UPDATE pipeline_job SET status='ready',capabilities_json=?,node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',updated_at=?,error_code=?,error_message='' WHERE id=?",
                      (dumps(sorted(set(required_capabilities))), self.now(), str(reason)[:120], row["id"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND generation=?", (row["id"], row["generation"]))
            self.event(c, row["id"], "preparation_deferred", {"reason": str(reason)[:120], "required_capabilities": sorted(set(required_capabilities))}, self.now())
            return {"accepted": True, "deferred": True, "idle_reason": str(reason)[:120]}

    def begin_operation(self, claim, operation):
        with self.transaction() as c:
            row = self._validate_claim(c, claim)
            key = str(operation.get("operation_id", ""))
            fingerprint = str(operation.get("request_sha256", ""))
            resource = str(operation.get("account_resource", ""))
            if not key or len(key) > 256 or len(fingerprint) != 64 or any(x not in "0123456789abcdef" for x in fingerprint):
                raise DispatchError("operation_invalid", status=400)
            if resource not in json.loads(row["resource_keys_json"]):
                raise DispatchError("operation_resource_invalid", status=403)
            held = c.execute("SELECT * FROM pipeline_resource WHERE resource_key=?", (resource,)).fetchone()
            if not held or held["job_id"] != row["id"] or held["generation"] != row["generation"]:
                raise DispatchError("operation_resource_not_held")
            existing = c.execute("SELECT * FROM pipeline_operation WHERE operation_id=?", (key,)).fetchone()
            expected = (row["id"], row["generation"], row["node_id"], str(operation.get("operation", "")), fingerprint, resource)
            if existing:
                actual = tuple(existing[k] for k in ("job_id", "generation", "node_id", "operation", "request_sha256", "account_resource"))
                if actual != expected:
                    raise DispatchError("operation_identity_conflict")
                # Never issue another write permit after a possibly accepted request.
                raise DispatchError("operation_already_begun")
            now = self.now()
            c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (key, *expected, now, now))
            c.execute("UPDATE pipeline_job SET external_intent=1,updated_at=? WHERE id=?", (now, row["id"]))
            self.event(c, row["id"], "operation_intent", {"operation_id": key, "operation": expected[3]}, now)
            return {"job_id": row["id"], "generation": row["generation"], "node_id": row["node_id"], "account_resource": resource, "operation_id": key, "request_sha256": fingerprint}

    def validate_receipt_claim(self, claim):
        with contextlib.closing(self.connect()) as c:
            return _job(self._validate_claim(c, claim, allow_expired=True), True)

    def historical_receipt_claim(self, claim):
        with contextlib.closing(self.connect()) as c:
            current = c.execute("SELECT * FROM pipeline_job WHERE id=?", (claim.get("job_id", claim.get("id")),)).fetchone()
            if not current or current["generation"] <= claim.get("generation", 0):
                return None
            saved = c.execute("SELECT * FROM pipeline_claim_history WHERE job_id=? AND generation=?", (current["id"], claim.get("generation"))).fetchone()
            if not saved or saved["node_id"] != claim.get("node_id") or saved["worker_id"] != claim.get("worker_id") or not secrets.compare_digest(saved["claim_hash"], digest(claim.get("claim_token", ""))):
                raise DispatchError("claim_conflict")
            if current["stage"] == "prepare":
                # This is authentication for read-only canonical reconciliation,
                # not permission to complete or unlock the current generation.
                historical = _job(current, True)
                historical.update(generation=saved["generation"], node_id=saved["node_id"],
                                  worker_id=saved["worker_id"], lease_expires_at="")
                return historical
            # Only appointment continuations can advance a completed external
            # publication's generation without permission to repeat its write.
            if current["channel"] != "youtube" or current["stage"] != "publish" or not json.loads(current["payload_json"]).get("continuation"):
                raise DispatchError("historical_receipt_requires_reconciliation")
            return _job(current, True)

    def rearm_native_schedule(self, job, verified):
        continuation = verified.get("continuation")
        proof = verified.get("canonical_receipt", {})
        if not continuation or not verified.get("dispatch_completed") or not proof.get("publish_id") or continuation.get("video_id") != proof["publish_id"]:
            raise DispatchError("canonical_continuation_required")
        with self.transaction() as c:
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (job["id"],)).fetchone()
            if not row or proof.get("source_key") != row["source_key"] or row["generation"] != job["generation"]:
                raise DispatchError("canonical_identity_conflict")
            if not row["generation"] or row["status"] != "completed" or row["channel"] != "youtube" or row["stage"] != "publish":
                return {"accepted": True, "unchanged": True}
            descriptor = json.loads(row["payload_json"])
            previous = descriptor.get("continuation")
            if previous and previous.get("video_id") != continuation["video_id"]:
                raise DispatchError("continuation_identity_conflict")
            descriptor["continuation"] = dict(continuation)
            due = max(stamp(continuation["scheduled_at"]), stamp(self.clock()+dt.timedelta(seconds=30)))
            c.execute("UPDATE pipeline_job SET status='ready',scheduled_at=?,payload_json=?,external_intent=0,started_at='',finished_at='',updated_at=? WHERE id=?", (due,dumps(descriptor),self.now(),row["id"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (row["id"],))
            self.event(c,row["id"],"scheduled_continuation_recovered",{"video_id":continuation["video_id"]},self.now())
            return {"accepted": True, "scheduled_at": due}

    def delivered(self, claim, delivery_id, request):
        if not delivery_id:
            return None
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT * FROM pipeline_delivery WHERE delivery_id=?", (delivery_id,)).fetchone()
        if not row:
            return None
        identity = (claim.get("job_id", claim.get("id")), claim.get("generation"), digest(claim.get("claim_token", "")), digest(dumps(request)))
        if tuple(row[k] for k in ("job_id", "generation", "claim_hash", "request_sha256")) != identity:
            raise DispatchError("delivery_identity_conflict")
        return dict(json.loads(row["response_json"]), duplicate=True)

    def save_delivery(self, claim, delivery_id, request, response):
        if not delivery_id:
            return response
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (delivery_id, claim.get("job_id", claim.get("id")), claim["generation"], digest(claim["claim_token"]), digest(dumps(request)), dumps(response)))
        return response

    def checkpoint(self, claim, checkpoint_id, phase, receipt, canonical_verified=False):
        if not checkpoint_id or not canonical_verified:
            raise DispatchError("canonical_checkpoint_required")
        body_hash = digest(dumps(receipt))
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            existing = c.execute("SELECT * FROM pipeline_checkpoint WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            if existing:
                if (existing["job_id"], existing["generation"], existing["phase"], existing["body_sha256"]) != (row["id"], row["generation"], phase, body_hash):
                    raise DispatchError("checkpoint_identity_conflict")
                return {"accepted": True, "duplicate": True}
            c.execute("INSERT INTO pipeline_checkpoint VALUES(?,?,?,?,?,?)", (checkpoint_id, row["id"], row["generation"], str(phase)[:80], body_hash, self.now()))
            details = {"phase": str(phase)[:80]}
            # The native adapter has already validated this exact receipt. Keep
            # only the identity needed for read-only confirmation timing; the
            # checkpoint table deliberately stores no raw platform response.
            if row["channel"] == "fb" and row["stage"] == "publish" and phase == "fb_publish_attempt":
                post_id = str(receipt.get("post_id") or "")
                if receipt.get("kind") == "success" and post_id and len(post_id) <= 128 and all(char in "0123456789_" for char in post_id):
                    details.update(generation=row["generation"], checkpoint_id=checkpoint_id,
                                   post_id=post_id, kind="success")
            self.event(c, row["id"], "checkpoint", details, self.now())
            return {"accepted": True, "duplicate": False}

    def complete(self, claim, receipt_id, result, canonical_verified=False):
        if not receipt_id or len(receipt_id) > 256:
            raise DispatchError("receipt_invalid", status=400)
        body_hash = result.get("_delivery_sha256") or digest(dumps(result))
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            existing = c.execute("SELECT * FROM pipeline_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
            if existing:
                if (existing["job_id"], existing["generation"], existing["body_sha256"]) != (row["id"], row["generation"], body_hash):
                    raise DispatchError("receipt_identity_conflict")
                return {"accepted": True, "duplicate": True}
            if row["status"] not in ("running", "unknown", "queued", "completed", "published"):
                raise DispatchError("job_terminal")
            if not canonical_verified:
                raise DispatchError("canonical_receipt_required")
            status = "completed" if row["stage"] == "prepare" or (result.get("dispatch_completed") and not result.get("published")) else "published"
            if row["status"] in ("completed", "published"):
                # A canonical read-back may have beaten the durable node outbox.
                # Accept its late delivery, but never downgrade published truth.
                if row["status"] == "published":
                    status = "published"
            if row["stage"] == "prepare":
                artifact = result.get("artifact", {})
                if not isinstance(artifact, dict) or not artifact.get("sha256") or not artifact.get("size") or not (artifact.get("url") or artifact.get("object_key")):
                    raise DispatchError("portable_artifact_required")
                c.execute("UPDATE pipeline_job SET artifact_json=? WHERE id=?", (dumps(artifact), row["id"]))
            now = self.now()
            c.execute("INSERT INTO pipeline_receipt VALUES(?,?,?,?,?)", (receipt_id, row["id"], row["generation"], body_hash, now))
            c.execute("UPDATE pipeline_job SET status=?,finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END,updated_at=?,error_code='',error_message='',canonical_receipt=? WHERE id=?", (status, now, now, str(result.get("canonical_receipt", result.get("publish_id", ""))), row["id"]))
            c.execute("UPDATE pipeline_operation SET status='completed',updated_at=? WHERE job_id=? AND generation=?", (now, row["id"], row["generation"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND generation=?", (row["id"], row["generation"]))
            for phase, metric in result.get("phase_metrics", {}).items():
                seconds = metric.get("seconds") if isinstance(metric, dict) else metric
                if isinstance(seconds, (float, int)) and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds >= 0:
                    c.execute("INSERT OR IGNORE INTO pipeline_phase_metric VALUES(?,?,?,?,?,?)", (row["id"], row["generation"], str(phase)[:80], seconds, metric.get("bytes") if isinstance(metric, dict) else None, now))
            self.event(c, row["id"], "completed", {"status": status}, now)
            return {"accepted": True, "duplicate": False}

    def rearm_schedule(self, claim, continuation, delivery=None):
        """Resume a confirmed existing platform object; never repeat its upload."""
        if not continuation.get("video_id"):
            raise DispatchError("continuation_identity_missing")
        due = stamp(continuation["scheduled_at"])
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            if row["channel"] != "youtube" or row["stage"] != "publish" or row["status"] not in ("completed", "ready"):
                raise DispatchError("continuation_state_invalid")
            descriptor = json.loads(row["payload_json"])
            previous = descriptor.get("continuation")
            if previous and previous.get("video_id") != continuation["video_id"]:
                raise DispatchError("continuation_identity_conflict")
            descriptor["continuation"] = dict(continuation)
            # A platform still processing at its appointment may be read again,
            # using the existing video identity and a new fenced claim only.
            next_due = max(due, stamp(self.clock() + dt.timedelta(seconds=30)))
            c.execute("UPDATE pipeline_job SET status='ready',scheduled_at=?,payload_json=?,external_intent=0,started_at='',finished_at='',updated_at=? WHERE id=?", (next_due, dumps(descriptor), self.now(), row["id"]))
            if delivery:
                delivery_id, request, response = delivery
                c.execute("INSERT OR IGNORE INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (delivery_id, row["id"], row["generation"], digest(claim["claim_token"]), digest(dumps(request)), dumps(response)))
            self.event(c, row["id"], "scheduled_continuation", {"scheduled_at": next_due, "video_id": continuation["video_id"]}, self.now())
            return {"accepted": True, "scheduled_at": next_due}

    def fail(self, claim, code, message="", unknown=False, canonical_no_write=False):
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            if row["status"] in ("completed", "published"):
                return {"accepted": True, "terminal": True}
            # An RPC error cannot prove that a platform write did not happen.
            unknown = bool(unknown or (row["external_intent"] and not canonical_no_write))
            status = "unknown" if unknown else "failed"
            now = self.now()
            c.execute("UPDATE pipeline_job SET status=?,error_code=?,error_message=?,updated_at=?,finished_at=? WHERE id=?", (status, str(code)[:120], str(message)[:500], now, "" if unknown else now, row["id"]))
            if unknown:
                c.execute("UPDATE pipeline_resource SET quarantined=1 WHERE job_id=?", (row["id"],))
            else:
                c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (row["id"],))
            self.event(c, row["id"], "failed", {"code": str(code)[:120], "unknown": unknown}, now)
            return {"accepted": True, "status": status}

    def skip(self, claim, proof, canonical_verified=False):
        """A native no-candidate/cancelled item produced no media or publication."""
        if not canonical_verified or not isinstance(proof, dict):
            raise DispatchError("canonical_receipt_required")
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            if proof.get("source_key") != row["source_key"]:
                raise DispatchError("canonical_identity_conflict")
            if row["external_intent"] or row["status"] == "published" or proof.get("publish_id"):
                raise DispatchError("operation_requires_reconciliation")
            now = self.now()
            c.execute("UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',finished_at=?,updated_at=?,canonical_receipt=?,error_code='',error_message='' WHERE id=?", (now, now, dumps(proof), row["id"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND generation=?", (row["id"], row["generation"]))
            self.event(c, row["id"], "skipped", {"native_status": proof.get("status", "")}, now)
            return {"accepted": True, "status": "completed", "metadata_only": True}

    def source_health(self, channel, family, error_code="", collection_state="ready"):
        with self.transaction() as c:
            c.execute("INSERT INTO pipeline_source_health VALUES(?,?,?,?,?) ON CONFLICT(channel,family) DO UPDATE SET state=excluded.state,collected_at=excluded.collected_at,error_code=excluded.error_code", (channel_key(channel), family, "error" if error_code else collection_state, self.now(), str(error_code)[:120]))

    def record_execution_metrics(self, claim, metrics):
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            for key, seconds in metrics.items():
                if key.endswith("_seconds") and isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds) and 0 <= seconds <= 86400 * 30:
                    c.execute("INSERT INTO pipeline_phase_metric VALUES(?,?,?,?,NULL,?) ON CONFLICT(job_id,generation,phase) DO UPDATE SET seconds=MAX(seconds,excluded.seconds)", (row["id"], row["generation"], key[:-8][:80], seconds, self.now()))

    def record_publication(self, job_id, proof, canonical_verified=False):
        if not canonical_verified or not isinstance(proof, dict) or not proof.get("publish_id"):
            raise DispatchError("canonical_publication_required")
        with self.transaction() as c:
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone()
            if not row or row["stage"] != "publish" or proof.get("source_key") != row["source_key"]:
                raise DispatchError("canonical_identity_conflict")
            old = c.execute("SELECT publish_id FROM pipeline_publication WHERE source_key=?", (row["source_key"],)).fetchone()
            if old and old[0] != str(proof["publish_id"]):
                raise DispatchError("canonical_publication_identity_changed")
            raw_published = proof.get("video_published_at") or proof.get("published_at")
            published = stamp(raw_published or self.now()) if raw_published or not proof.get("historical") else ""
            c.execute("INSERT INTO pipeline_publication VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET owner_user_id=excluded.owner_user_id,owner_tenant_key=excluded.owner_tenant_key,required_module=excluded.required_module,published_at=CASE WHEN pipeline_publication.published_at='' THEN excluded.published_at ELSE pipeline_publication.published_at END,node_id=CASE WHEN pipeline_publication.node_id='' THEN excluded.node_id ELSE pipeline_publication.node_id END", (row["source_key"], row["id"], row["channel"], row["owner_user_id"], row["owner_tenant_key"], row["required_module"], row["node_id"], str(proof["publish_id"]), published, dumps(proof)))

    def record_cooldown(self, claim, advice):
        scope = advice.get("rate_limit_scope")
        if scope not in ("account", "channel"):
            return
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            resource = advice.get("account_resource")
            if scope == "account" and resource not in json.loads(row["resource_keys_json"]):
                raise DispatchError("cooldown_resource_invalid")
            resource = "channel:" + row["channel"] if scope == "channel" else resource
            seconds = advice.get("retry_after_seconds", 300)
            if not isinstance(seconds, (float, int)) or not math.isfinite(seconds):
                seconds = 300
            until = stamp(self.clock() + dt.timedelta(seconds=max(1, min(86400, seconds))))
            c.execute("INSERT INTO pipeline_cooldown VALUES(?,?,'platform_rate_limit') ON CONFLICT(resource_key) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)", (resource, until))
            self.event(c, row["id"], "platform_cooldown", {"resource_key": resource, "until_at": until}, self.now())

    def reconciliation_jobs(self, limit=100, after_id=0):
        with contextlib.closing(self.connect()) as c:
            rows = c.execute("SELECT * FROM pipeline_job WHERE (status IN ('unknown','legacy_running') OR (status='completed' AND stage='publish')) AND id>? ORDER BY id LIMIT ?", (after_id, min(200, max(1, int(limit))))).fetchall()
            return [_job(row, True) for row in rows]

    def reconcile_native(self, job_id, verified):
        """Converge dispatch from an owning adapter's canonical read-back only."""
        if verified.get("published"):
            status = "published"
        elif verified.get("dispatch_completed"):
            status = "completed"
        elif verified.get("definitive_failure"):
            status = "failed"
        else:
            return {"accepted": True, "pending": True}
        proof = verified.get("canonical_receipt")
        if not isinstance(proof, dict) or not proof.get("source_key"):
            raise DispatchError("canonical_receipt_required")
        with self.transaction() as c:
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone()
            if not row or proof["source_key"] != row["source_key"]:
                raise DispatchError("canonical_identity_conflict")
            if row["status"] not in ("unknown", "legacy_running") and not (row["status"] == "completed" and row["stage"] == "publish" and status in ("published", "failed")):
                return {"accepted": True, "unchanged": True}
            now = self.now()
            c.execute("UPDATE pipeline_job SET status=?,canonical_receipt=?,finished_at=?,updated_at=? WHERE id=?", (status, dumps(proof), now, now, job_id))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (job_id,))
            self.event(c, job_id, "canonical_reconciled", {"status": status}, now)
            return {"accepted": True, "status": status}

    def adapt_capacity(self):
        now = self.now()
        with self.transaction() as c:
            config = self.config(c)
            changes = []
            for node in c.execute("SELECT * FROM pipeline_node").fetchall():
                if node["last_seen_at"] < stamp(self.clock() - dt.timedelta(seconds=45)):
                    continue
                metrics = json.loads(node["metrics_json"])
                cpu, available = metrics.get("cpu_percent"), metrics.get("memory_available_fraction")
                if cpu is None or available is None:
                    continue
                for stage in STAGES:
                    pool = config["hosts"][node["node_id"]][stage]
                    state = c.execute("SELECT * FROM pipeline_capacity_sample WHERE node_id=? AND stage=?", (node["node_id"], stage)).fetchone()
                    last = state["last_change"] if state else ""
                    high = state["high_since"] if state else ""
                    pressure = cpu > 85 or available < .15
                    is_gpu = "backend:gpu" in json.loads(node["capabilities_json"])
                    if stage == "prepare" and is_gpu:
                        pressure = pressure or metrics.get("gpu_percent", 0) > 95 or metrics.get("encoder_percent", 0) > 95 or metrics.get("gpu_memory_available_fraction", 1) < .1
                    high = (high or now) if pressure else ""
                    old = pool["target"]
                    if high and high <= stamp(self.clock() - dt.timedelta(seconds=120)):
                        pool["target"] = max(1 if pool["ceiling"] else 0, old - 1)
                        high = now
                    elif (not last or last <= stamp(self.clock() - dt.timedelta(seconds=300))) and cpu < 70 and available > .25:
                        samples = c.execute("SELECT count(*) FROM pipeline_job WHERE node_id=? AND stage=? AND finished_at>=? AND status IN ('completed','published')", (node["node_id"], stage, stamp(self.clock() - CAPACITY_SAMPLE_WINDOW))).fetchone()[0]
                        candidates = c.execute("SELECT channel,capabilities_json FROM pipeline_job WHERE stage=? AND status IN ('queued','ready') AND hold_reason='' AND scheduled_at<=?", (stage, now)).fetchall()
                        node_capabilities = set(json.loads(node["capabilities_json"]))
                        waiting = any(item["channel"] in config["enabled_channels"] and set(json.loads(item["capabilities_json"])).issubset(node_capabilities) for item in candidates)
                        gpu_room = stage != "prepare" or not is_gpu or (metrics.get("gpu_percent", 100) < 80 and metrics.get("encoder_percent", 100) < 80 and metrics.get("gpu_memory_available_fraction", 0) > .2)
                        if samples >= 3 and waiting and gpu_room:
                            pool["target"] = min(old + 1, pool["ceiling"])
                    if pool["target"] != old:
                        last = now
                        changes.append({"node_id": node["node_id"], "stage": stage, "before": old, "after": pool["target"]})
                    c.execute("INSERT INTO pipeline_capacity_sample VALUES(?,?,?,?) ON CONFLICT(node_id,stage) DO UPDATE SET high_since=excluded.high_since,last_change=excluded.last_change", (node["node_id"], stage, high, last))
            if changes:
                self._save_config(c, config, {}, "capacity_adapted")
                self.event(c, None, "capacity_decisions", changes, now)
            return changes

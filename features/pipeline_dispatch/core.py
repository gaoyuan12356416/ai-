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
from .backlog_release import offer_disposition
from .backlog_contract import SCHEMA as BACKLOG_SCHEMA

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


def isolated_queue_resource(row):
    """Scope delivery ownership to the immutable task, never its account history."""
    if row["channel"] in {"x", "tt"}:
        return "task:" + str(row["source_key"])
    if row["channel"] == "fb":
        resources = json.loads(row["resource_keys_json"])
        if not any(key.startswith("fb:material:") for key in resources):
            return "task:" + str(row["source_key"])
    return ""


def independent_x_preparation(row):
    """Only a frozen X queue may render ahead; planners retain account fencing."""
    meta = json.loads(row["source_meta_json"])
    return (row["channel"] == "x" and row["family"] == "queue" and row["stage"] == "prepare"
            and str(row["source_id"]).isdigit() and row["source_key"] == "x:queue:" + str(row["source_id"])
            and not row["external_intent"]
            and not any(meta.get(k) for k in ("source_attempt_count", "unknown_outcome", "reconcile_only", "legacy_inflight", "publish_id")))


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
    def __init__(self, path, clock=utcnow, *, capability_exclusions=None):
        exclusions = {} if capability_exclusions is None else capability_exclusions
        if (not isinstance(exclusions, dict)
                or any(not isinstance(node, str) or not node or not isinstance(values, list)
                       or any(not isinstance(value, str) or not value or len(value) > 120 for value in values)
                       for node, values in exclusions.items())):
            raise DispatchError("node_capability_policy_invalid", status=400)
        self.capability_exclusions = {node: set(values) for node, values in exclusions.items()}
        self.path, self.clock = str(path), clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()
        # Convert exclusion scope only. Never change a task, generation, lease,
        # external operation, receipt, or publication while activating this policy.
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM pipeline_job WHERE id IN ("
                                "SELECT job_id FROM pipeline_resource UNION SELECT job_id FROM pipeline_legacy_resource)").fetchall()
            for row in rows:
                self._refresh_queue_isolation(conn, row)

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
            c.execute(BACKLOG_SCHEMA)
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
            CREATE TABLE IF NOT EXISTS pipeline_preparation_rejection (
              job_id INTEGER PRIMARY KEY,generation INTEGER NOT NULL,phase TEXT NOT NULL,
              receipt_sha256 TEXT NOT NULL,error_code TEXT NOT NULL,created_at TEXT NOT NULL);
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
            if "scaling_mode" in patch and ("host_id" not in patch or patch["scaling_mode"] not in ("auto", "manual")):
                raise DispatchError("capacity_scaling_mode_invalid", status=400)
            if "host_id" in patch:
                host, stage = patch["host_id"], patch.get("stage")
                if host not in config["hosts"] or stage not in STAGES:
                    raise DispatchError("capacity_pool_invalid", status=400)
                pool = config["hosts"][host][stage]
                previous_mode = pool.get("scaling_mode", "auto")
                mode = patch.get("scaling_mode", previous_mode)
                ceiling = _positive(patch.get("max_workers", patch.get("capacity", pool["ceiling"] if "scaling_mode" in patch else None)), True)
                pool["ceiling"] = ceiling
                pool["scaling_mode"] = mode
                if mode == "manual":
                    pool["target"] = ceiling
                else:
                    pool["target"] = min(pool["target"], ceiling)
                    if ceiling and pool["target"] == 0:
                        pool["target"] = 1
                if mode != previous_mode:
                    # A restored automatic pool starts a new pressure window;
                    # observations from its prior automatic period do not apply.
                    c.execute("DELETE FROM pipeline_capacity_sample WHERE node_id=? AND stage=?", (host, stage))
            if "channel_priority" in patch:
                order = patch["channel_priority"]
                if (not isinstance(order, list) or len(order) > len(CHANNELS)
                        or any(not isinstance(value, str) or value not in CHANNELS for value in order)
                        or len(set(order)) != len(order)):
                    raise DispatchError("channel_priority_invalid", status=400)
                config["channel_priority"] = list(order)
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

    def channel_enabled(self, channel, enabled, *, preparation_only=False):
        channel = channel_key(channel)
        if not isinstance(preparation_only, bool):
            raise DispatchError("preparation_only_invalid", status=400)
        with self.transaction() as c:
            config = self.config(c)
            channels = set(config["enabled_channels"])
            channels.add(channel) if enabled else channels.discard(channel)
            config["enabled_channels"] = sorted(channels)
            limited = set(config.get("preparation_only_channels", []))
            limited.add(channel) if enabled and preparation_only else limited.discard(channel)
            config["preparation_only_channels"] = sorted(limited)
            self._save_config(c, config, {}, "channel_preparation_only" if enabled and preparation_only else "channel_enabled" if enabled else "channel_draining")

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

    def _rearm_youtube_publish_retry(self, c, existing, offer, meta):
        revision = str(offer.get("source_revision") or "")
        previous = str(meta.get("source_revision") or "0")
        try:
            proof = json.loads(existing["canonical_receipt"] or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(proof, dict) or proof.get("publish_id") or proof.get("unknown_outcome"):
            return False
        if not (existing["channel"] == "youtube" and existing["family"] == "publish"
                and existing["stage"] == "publish" and existing["status"] == "failed"
                and revision.isdigit() and previous.isdigit() and int(revision) > int(previous)
                and offer.get("source_status") == "queued" and offer.get("execution_available") is True
                and existing["hold_reason"] in ("", "adapter_unavailable")
                and not any(offer.get(k) for k in ("hold_reason", "source_attempt_count", "unknown_outcome", "publish_id", "legacy_inflight", "reconcile_only"))):
            return False
        if c.execute("SELECT 1 FROM pipeline_resource WHERE job_id=? UNION ALL SELECT 1 FROM pipeline_legacy_resource WHERE job_id=? LIMIT 1", (existing["id"], existing["id"])).fetchone():
            return False
        # Only an explicit native retry of a proven never-uploaded publication.
        # Keep all prior claims, operations and receipts as historical evidence.
        meta.update({k: offer[k] for k in ("source_revision", "source_attempt_count", "legacy_inflight", "reconcile_only", "execution_available", "canonical_fingerprint") if k in offer})
        c.execute("UPDATE pipeline_job SET status='queued',source_status='queued',hold_reason='',external_intent=0,payload_json=?,artifact_json=?,source_meta_json=?,canonical_receipt='',node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',error_code='',error_message='',updated_at=? WHERE id=?",
                  (dumps(offer.get("payload", {})), dumps(offer.get("artifact", {})), dumps(meta), self.now(), existing["id"]))
        self.event(c, existing["id"], "youtube_publish_retry_requested", {"source_revision": revision, "previous_generation": existing["generation"]}, self.now())
        return True

    def _rearm_fb_skipped_observation(self, c, existing, offer, meta):
        """Admit only an explicit native recovery of an unexecuted skipped row."""
        operation_id = str(offer.get("fb_skipped_recovery") or "")
        if (len(operation_id) != 32 or any(ch not in "0123456789abcdef" for ch in operation_id)
                or existing["channel"] != "fb" or existing["family"] != "auto" or existing["stage"] != "prepare"
                or existing["generation"] != 0 or existing["status"] != "completed"
                or existing["hold_reason"] != "metadata_only" or existing["source_status"] != "skipped"
                or any(existing[k] for k in ("external_intent", "claim_hash", "lease_expires_at", "canonical_receipt"))
                or offer.get("source_status") != "planned" or offer.get("status") != "queued"
                or offer.get("execution_available") is not True
                or any(offer.get(k) for k in ("hold_reason", "publish_id", "source_attempt_count", "unknown_outcome",
                                             "legacy_inflight", "reconcile_only", "metadata_only"))):
            return False
        cutoff = self.config(c)["cutovers"].get("fb")
        if (cutoff and existing["source_created_at"] <= cutoff
                and stamp(offer.get("cutover_due_at") or offer.get("scheduled_at")) <= cutoff):
            return False
        # Any central execution history for either phase keeps the original fact.
        # Recovery never resets an attempted publish, a receipt, or a quarantine.
        for table in ("pipeline_claim_history", "pipeline_operation", "pipeline_receipt", "pipeline_checkpoint",
                      "pipeline_delivery", "pipeline_publication", "pipeline_resource", "pipeline_legacy_resource"):
            if c.execute("SELECT 1 FROM " + table + " WHERE job_id IN (SELECT id FROM pipeline_job WHERE source_key=?) LIMIT 1",
                         (existing["source_key"],)).fetchone():
                return False
        if c.execute("SELECT 1 FROM pipeline_job WHERE source_key=? AND id<>? AND (generation<>0 OR external_intent<>0 "
                     "OR claim_hash<>'' OR canonical_receipt<>'' OR status IN ('unknown','published','failed','running','legacy_running')) LIMIT 1",
                     (existing["source_key"], existing["id"])).fetchone():
            return False
        meta.update({k: offer[k] for k in ("canonical_fingerprint", "source_attempt_count", "legacy_inflight",
                    "reconcile_only", "execution_available", "cutover_due_at") if k in offer})
        meta["fb_skipped_recovery"] = operation_id
        c.execute("UPDATE pipeline_job SET status='queued',source_status='planned',hold_reason='',payload_json=?,artifact_json=?,"
                  "capabilities_json=?,resource_keys_json=?,source_meta_json=?,scheduled_at=?,started_at='',finished_at='',error_code='',error_message='',updated_at=? WHERE id=?",
                  (dumps(offer.get("payload", {})), dumps(offer.get("artifact", {})), dumps(offer.get("capabilities", [])),
                   dumps(offer.get("resource_keys", [])), dumps(meta), stamp(offer.get("scheduled_at")), self.now(), existing["id"]))
        self.event(c, existing["id"], "fb_skipped_recovery_offered", {"operation_id": operation_id}, self.now())
        return True

    def _refresh_fb_isolation(self, c, row, offer):
        """Narrow only a fresh canonical FB observation, never a worker guess."""
        if (row["channel"] != "fb" or row["family"] not in {"auto", "video"}
                or offer.get("source_key") != row["source_key"]
                or offer.get("stage") != row["stage"]):
            return
        from .fb_isolation import identity_resources
        pool_task_id = offer.get("fb_pool_task_id", "")
        if pool_task_id and (row["family"] != "video" or pool_task_id != row["source_id"]):
            return
        resources, page, material = identity_resources(offer.get("fb_page_id"), offer.get("fb_material_id"), row["stage"], pool_task_id=pool_task_id)
        if not material or set(resources) != set(offer.get("resource_keys", [])):
            return  # Unknown legacy material identity keeps the Page fence.
        old = json.loads(row["resource_keys_json"])
        c.execute("UPDATE pipeline_job SET resource_keys_json=? WHERE id=?", (dumps(sorted(resources)), row["id"]))
        native = str(offer.get("source_status") or "")
        uncertain = native in {"unknown", "submitted", "processing"} or bool(offer.get("unknown_outcome"))
        resolved = (not uncertain and not offer.get("fb_page_write_active")
                    and (native in {"failed", "failed_without_retry", "cancelled", "skipped"}
                         or (native == "published" and bool(offer.get("publish_id")))))
        if resolved:
            released = c.execute("DELETE FROM pipeline_resource WHERE job_id=?", (row["id"],)).rowcount
            released += c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (row["id"],)).rowcount
            if released:
                self.event(c, row["id"], "fb_material_isolation_resolved", {"source_status": native}, self.now())
            return
        detached = (not offer.get("fb_page_write_active")
                    and native in {"unknown", "submitted", "processing", "published", "failed", "cancelled", "skipped"})
        preparation = row["stage"] == "prepare" and not row["external_intent"]
        held = c.execute("SELECT * FROM pipeline_resource WHERE job_id=?", (row["id"],)).fetchall()
        legacy = c.execute("SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?", (row["id"],)).fetchone()
        if uncertain:
            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (material, row["id"]))
        if held:
            c.execute("INSERT OR IGNORE INTO pipeline_resource VALUES(?,?,?,?,?)",
                      (material, row["id"], row["generation"], int(uncertain), int(held[0]["legacy"])))
        if legacy:
            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (material, row["id"]))
        # A previously unidentified task may acquire its canonical material
        # identity later. Move that exact task fence to the now-known material.
        for table in ("pipeline_resource", "pipeline_legacy_resource"):
            c.execute("DELETE FROM " + table + " WHERE job_id=? AND resource_key=?",
                      (row["id"], "task:" + row["source_key"]))
        released = 0
        if detached or preparation:
            released += c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND resource_key=?", (row["id"], page)).rowcount
            released += c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=? AND resource_key=?", (row["id"], page)).rowcount
        if old != sorted(resources) or released:
            self.event(c, row["id"], "fb_material_isolation_refreshed",
                       {"material_resource": material, "page_released": bool(released)}, self.now())

    def _refresh_youtube_isolation(self, c, row, offer):
        if (row["channel"] != "youtube" or row["family"] != "publish"
                or row["source_key"] != offer.get("source_key") or row["stage"] != offer.get("stage")):
            return
        from .youtube_isolation import identity_resources
        resources = identity_resources(row["source_id"])
        if not resources or resources != offer.get("resource_keys"):
            return
        old = json.loads(row["resource_keys_json"])
        held = c.execute("SELECT * FROM pipeline_resource WHERE job_id=?", (row["id"],)).fetchall()
        legacy = c.execute("SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?", (row["id"],)).fetchone()
        uncertain = row["status"] == "unknown" or bool(offer.get("unknown_outcome"))
        # Change only the scope of exclusion. Claims, native leases, operation
        # permits, sessions and receipts remain valid, including active uploads.
        if held or row["status"] == "running":
            c.execute("INSERT OR IGNORE INTO pipeline_resource VALUES(?,?,?,?,?)",
                      (resources[0], row["id"], row["generation"], int(uncertain), int(held[0]["legacy"]) if held else 0))
        if legacy or uncertain:
            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (resources[0], row["id"]))
        released = 0
        for table in ("pipeline_resource", "pipeline_legacy_resource"):
            released += c.execute("DELETE FROM " + table + " WHERE job_id=? AND resource_key<>?", (row["id"], resources[0])).rowcount
        c.execute("UPDATE pipeline_job SET resource_keys_json=? WHERE id=?", (dumps(resources), row["id"]))
        if old != resources or released:
            self.event(c, row["id"], "youtube_task_isolation_refreshed",
                       {"task_resource": resources[0], "shared_resources_released": released}, self.now())

    def _refresh_queue_isolation(self, c, row):
        resource = isolated_queue_resource(row)
        if not resource:
            return
        held = c.execute("SELECT * FROM pipeline_resource WHERE job_id=?", (row["id"],)).fetchall()
        legacy = c.execute("SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?", (row["id"],)).fetchone()
        uncertain = row["status"] == "unknown"
        if held or row["status"] == "running":
            c.execute("INSERT OR IGNORE INTO pipeline_resource VALUES(?,?,?,?,?)",
                      (resource, row["id"], row["generation"], int(uncertain), int(held[0]["legacy"]) if held else 0))
        if legacy or uncertain:
            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (resource, row["id"]))
        released = 0
        for table in ("pipeline_resource", "pipeline_legacy_resource"):
            released += c.execute("DELETE FROM " + table + " WHERE job_id=? AND resource_key<>?", (row["id"], resource)).rowcount
        if released:
            self.event(c, row["id"], "queue_isolation_refreshed",
                       {"task_resource": resource, "shared_resources_released": released}, self.now())

    def offer(self, offer):
        now = self.now()
        channel = channel_key(offer["channel"])
        quarantine_resources = offer.get("resource_keys", [])
        if channel == "fb" and not offer.get("fb_page_write_active") and offer.get("source_status") in {"unknown", "submitted", "processing"}:
            from .fb_isolation import identity_resources
            expected, _, material = identity_resources(offer.get("fb_page_id"), offer.get("fb_material_id"), offer["stage"],
                pool_task_id=offer.get("fb_pool_task_id", "") if offer.get("family") == "video" and offer.get("fb_pool_task_id") == offer.get("source_id") else "")
            if material and set(expected) == set(quarantine_resources):
                quarantine_resources = [material]
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
            backlog_action = offer_disposition(c, offer)
            # Archived observations never become another executable phase.
            # Retain real terminal publication facts for existing published rows.
            if backlog_action == 'archive':
                archived = c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage=?", (offer['source_key'], stage)).fetchone()
                return _job(archived, True) if archived else None
            self._close_terminal_preparation_observation(c, offer)
            if stage == "publish" and status not in ("published", "failed") and (not offer.get("legacy_inflight") or int(offer.get("source_attempt_count") or 0) > 0):
                c.execute("UPDATE pipeline_job SET status='completed',finished_at=?,updated_at=? WHERE source_key=? AND stage='prepare' AND status='legacy_running' AND generation=0", (now, now, offer["source_key"]))
            existing = c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage=?", (offer["source_key"], stage)).fetchone()
            if (existing and backlog_action == 'resume' and existing['status'] == 'backlog_paused'
                    and existing['generation'] == 0 and not existing['external_intent']
                    and not existing['claim_hash'] and not existing['canonical_receipt']
                    and status in ('queued', 'ready') and offer.get('execution_available') is True
                    and not any(offer.get(k) for k in ('legacy_inflight', 'unknown_outcome', 'reconcile_only', 'source_attempt_count', 'publish_id'))):
                # A child may have been observed while its parent's two-ledger
                # release was still pending. Only matching committed native and
                # central authorization can admit that exact unclaimed child.
                history = any(c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (existing['id'],)).fetchone()
                              for table in ('pipeline_claim_history', 'pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery', 'pipeline_resource', 'pipeline_legacy_resource'))
                if not history:
                    c.execute("UPDATE pipeline_job SET status=?,hold_reason='',payload_json=?,artifact_json=?,capabilities_json=?,resource_keys_json=?,updated_at=? WHERE id=?",
                              (status, dumps(offer.get('payload', {})), dumps(offer.get('artifact', {})), dumps(offer.get('capabilities', [])), dumps(offer.get('resource_keys', [])), now, existing['id']))
                    self.event(c, existing['id'], 'backlog_authorized_phase_offered', {}, now)
                    existing = c.execute('SELECT * FROM pipeline_job WHERE id=?', (existing['id'],)).fetchone()
            if offer.get("metadata_only") and not existing:
                c.execute("UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',finished_at=?,updated_at=? WHERE source_key=? AND generation=0 AND status IN ('queued','ready','legacy_running')", (now, now, offer["source_key"]))
                self._release_finished_legacy_resources(c, offer["source_key"])
                return None  # Planning a queue is not a completed media render.
            if existing:
                meta = json.loads(existing["source_meta_json"])
                if offer.get("fb_skipped_recovery"):
                    self._rearm_fb_skipped_observation(c, existing, offer, meta)
                    # A rejected recovery must preserve its earlier receipt and
                    # fences instead of passing through observation convergence.
                    return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), True)
                if self._rearm_youtube_publish_retry(c, existing, offer, meta):
                    return _job(c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), True)
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
                if (channel == "youtube" and offer.get("family") == "preparation" and stage == "prepare"
                        and existing["generation"] == 0 and existing["status"] == "queued"
                        and not existing["hold_reason"] and not existing["external_intent"]
                        and not existing["claim_hash"] and not existing["canonical_receipt"]
                        and offer.get("source_status") == "queued_generation"
                        and offer.get("execution_available") is True):
                    # Refresh only never-claimed cover jobs from the old channel
                    # resource. Running claims, publication and unknown locks stay
                    # immutable; no historical task is retried by this migration.
                    resources = ["youtube:cover:" + existing["source_id"]]
                    previous = json.loads(existing["resource_keys_json"])
                    if (offer.get("resource_keys") == resources and len(previous) == 1
                            and previous[0].startswith("youtube:channel:")):
                        c.execute("UPDATE pipeline_job SET resource_keys_json=?,updated_at=? WHERE id=?",
                                  (dumps(resources), now, existing["id"]))
                        self.event(c, existing["id"], "youtube_cover_resource_refreshed", {}, now)
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
                # They may escalate a failed admission to a native unknown outcome.
                # Preserve the owning ledger's fence even if no permit reached a node.
                parked_schedule = (channel == "youtube" and existing["status"] == "ready"
                                   and not offer.get("unknown_outcome") and not offer.get("legacy_inflight")
                                   and bool(offer.get("confirmed_youtube_schedule"))
                                   and json.loads(existing["payload_json"]).get("continuation") == offer["confirmed_youtube_schedule"])
                if status == "unknown" and existing["status"] in ("queued", "ready", "failed") and existing["generation"] and not parked_schedule:
                    c.execute("UPDATE pipeline_job SET status='unknown',error_code='canonical_unknown_outcome',finished_at='',updated_at=? WHERE id=?", (now, existing["id"]))
                    for resource in quarantine_resources:
                        c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (resource, existing["id"]))
                    c.execute("UPDATE pipeline_resource SET quarantined=1 WHERE job_id=?", (existing["id"],))
                    self.event(c, existing["id"], "canonical_unknown_observed", {"source_status": offer.get("source_status", "")}, now)
                c.execute("UPDATE pipeline_job SET source_status=? WHERE id=?",
                          (str(offer.get("source_status", "")), existing["id"]))
                if not existing["owner_tenant_key"] and offer.get("owner_tenant_key") and str(offer.get("owner_user_id", "")) == existing["owner_user_id"]:
                    c.execute("UPDATE pipeline_job SET owner_tenant_key=?,owner_name=? WHERE id=?", (offer["owner_tenant_key"], str(offer.get("owner_name") or ""), existing["id"]))
                if existing["hold_reason"] in ("adapter_unavailable", "canonical_link_pending", "canonical_review_or_enqueue") and offer.get("execution_available") is True:
                    c.execute("UPDATE pipeline_job SET hold_reason='' WHERE id=? AND generation=0", (existing["id"],))
                if existing["generation"] == 0 and (existing["status"] != "backlog_paused" or status in ("unknown", "legacy_running")) and status in ("completed", "published", "failed", "unknown", "legacy_running"):
                    c.execute("UPDATE pipeline_job SET status=?,started_at=?,finished_at=?,updated_at=?,canonical_receipt=? WHERE id=?", (status, stamp(offer["started_at"]) if offer.get("started_at") else existing["started_at"], stamp(offer["finished_at"]) if offer.get("finished_at") else existing["finished_at"], now, str(offer.get("canonical_receipt", "")), existing["id"]))
                    if status in ("unknown", "legacy_running"):
                        for resource in quarantine_resources:
                            c.execute("INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)", (resource, existing["id"]))
                    if status in ("completed", "published", "failed"):
                        c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND legacy=1", (existing["id"],))
                        c.execute("DELETE FROM pipeline_legacy_resource WHERE job_id=?", (existing["id"],))
                    existing = c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone()
                self._converge_idle_legacy(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), offer)
                self._release_finished_legacy_resources(c, offer["source_key"])
                self._refresh_fb_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), offer)
                self._refresh_youtube_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone(), offer)
                self._refresh_queue_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (existing["id"],)).fetchone())
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
            if cutoff and stamp(offer.get("cutover_due_at") or scheduled) <= cutoff and created <= cutoff and not grandfathered and not adopting_schedule and backlog_action != 'resume' and status in ("queued", "ready"):
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
                for resource in sorted(set(quarantine_resources)):
                    # Several historical unknowns may share an account. Each
                    # remains a separate fence until every receipt is resolved.
                    c.execute("INSERT INTO pipeline_legacy_resource VALUES(?,?)", (resource, job_id))
            self._release_finished_legacy_resources(c, offer["source_key"])
            self._refresh_fb_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone(), offer)
            self._refresh_youtube_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone(), offer)
            self._refresh_queue_isolation(c, c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone())
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
            admitted = set(capabilities) - self.capability_exclusions.get(node_id, set())
            c.execute("INSERT INTO pipeline_node VALUES(?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET capabilities_json=excluded.capabilities_json,metrics_json=excluded.metrics_json,last_seen_at=excluded.last_seen_at", (node_id, dumps(sorted(admitted)), dumps(metrics or {}), self.now()))
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
                if row["channel"] == "fb" and row["stage"] == "prepare":
                    # Workers stop local rendering at the same lease deadline.
                    # Allow their bounded poll/kill/reap to finish before another
                    # host can start the same immutable preparation.
                    c.execute("INSERT INTO pipeline_cooldown VALUES(?,?,?) ON CONFLICT(resource_key) DO UPDATE SET until_at=excluded.until_at,reason=excluded.reason",
                              ("job:" + str(row["id"]), stamp(self.clock() + dt.timedelta(seconds=30)), "prepare_lease_cleanup"))
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
            # Apply immediately to cached registrations as well as future node
            # heartbeats. An unavailable tool must not interrupt other node work.
            capabilities = set(json.loads(node["capabilities_json"])) - self.capability_exclusions.get(node_id, set())
            rows = c.execute("""SELECT j.* FROM pipeline_job j
              LEFT JOIN pipeline_fairness fc ON fc.scope=? AND fc.item=j.channel
              LEFT JOIN pipeline_fairness fb ON fb.scope=?||j.channel AND fb.item=j.batch_key
              WHERE j.stage=? AND j.status IN ('queued','ready') AND j.hold_reason='' AND j.scheduled_at<=?
              ORDER BY COALESCE(fc.last_seq,0),COALESCE(fb.last_seq,0),j.scheduled_at,j.id""",
              (stage, stage + ":", stage, now)).fetchall()
            # Priority applies only at admission. Existing claims, account locks,
            # time gates and per-batch fairness remain authoritative. Unlisted
            # channels keep their fair-order place in the highest band.
            order = config.get("channel_priority", [])
            if order:
                rank = {channel: index for index, channel in enumerate(order)}
                rows = sorted(rows, key=lambda item: rank.get(item["channel"], 0))
            blocked = []
            for row in rows:
                if c.execute("SELECT 1 FROM pipeline_cooldown WHERE resource_key=? AND until_at>?", ("job:" + str(row["id"]), now)).fetchone():
                    blocked.append("account_busy")
                    continue
                if (row["channel"] not in config["enabled_channels"]
                        or (stage == "publish" and row["channel"] in config.get("preparation_only_channels", []))):
                    blocked.append("channel_disabled")
                    continue
                if not set(json.loads(row["capabilities_json"])).issubset(capabilities):
                    blocked.append("capability_unavailable")
                    continue
                resources = sorted(set(json.loads(row["resource_keys_json"])))
                if not resources:
                    blocked.append("account_identity_missing")
                    continue  # All current channel work belongs to an account, Page, or YouTube channel.
                scoped = isolated_queue_resource(row)
                independent = independent_x_preparation(row) and not scoped
                locks = [scoped] if scoped else (["prepare:" + r for r in resources] if independent else resources)
                busy = False
                for resource in locks:
                    if c.execute("SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=? LIMIT 1", (resource,)).fetchone():
                        busy = True
                        break
                    held = c.execute("SELECT r.quarantined,r.legacy,j.stage,j.status,j.lease_expires_at FROM pipeline_resource r JOIN pipeline_job j ON j.id=r.job_id WHERE r.resource_key=?", (resource,)).fetchone()
                    if held and not (independent and held["stage"] == "publish" and held["status"] == "running"
                                     and not held["quarantined"] and not held["legacy"] and held["lease_expires_at"] > now):
                        busy = True
                        break
                    if independent and c.execute("SELECT 1 FROM pipeline_resource WHERE resource_key=?", ("prepare:" + resource,)).fetchone():
                        busy = True
                        break
                    # Unknown preparation fences and legacy planners must also
                    # see the new lane. A normal publisher checks order below.
                    if not independent:
                        prep = c.execute("SELECT quarantined FROM pipeline_resource WHERE resource_key=?", ("prepare:" + resource,)).fetchone()
                        if prep and (stage != "publish" or prep["quarantined"]):
                            busy = True
                            break
                if busy:
                    blocked.append("account_busy")
                    continue
                if stage == "publish":
                    if any(c.execute("SELECT 1 FROM pipeline_cooldown WHERE resource_key=? AND until_at>?", (key, now)).fetchone() for key in resources + ["channel:" + row["channel"]]):
                        blocked.append("cooldown")
                        continue
                    if c.execute("SELECT count(*) FROM pipeline_job WHERE channel=? AND stage='publish' AND status IN ('running','legacy_running')", (row["channel"],)).fetchone()[0] >= config["channel_limits"][row["channel"]]:
                        blocked.append("channel_limit")
                        continue
                # Eligible queues have no dependency on an earlier sibling.
                token, generation = secrets.token_urlsafe(32), row["generation"] + 1
                expires = stamp(self.clock() + dt.timedelta(seconds=LEASE_SECONDS))
                c.execute("UPDATE pipeline_job SET status='running',node_id=?,worker_id=?,generation=?,claim_hash=?,lease_expires_at=?,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,updated_at=?,error_code='',error_message='' WHERE id=?", (node_id, worker_id, generation, digest(token), expires, now, now, row["id"]))
                c.execute("INSERT INTO pipeline_claim_history VALUES(?,?,?,?,?)", (row["id"], generation, node_id, worker_id, digest(token)))
                for resource in locks:
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

    def defer_blocked(self, claim, reason):
        """A native pre-claim account conflict waits without freezing a failure."""
        if reason not in {"canonical_account_busy", "x_post_schedule_not_due", "x_post_drama_previous_episode_pending"}:
            raise DispatchError("defer_reason_invalid")
        with self.transaction() as c:
            row = self._validate_claim(c, claim)
            if row["external_intent"] or c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? LIMIT 1", (row["id"],)).fetchone():
                raise DispatchError("operation_requires_reconciliation")
            c.execute("UPDATE pipeline_job SET status='queued',lease_expires_at='',updated_at=?,error_code=? WHERE id=?", (self.now(), reason, row["id"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND generation=?", (row["id"], row["generation"]))
            c.execute("INSERT INTO pipeline_cooldown VALUES(?,?,?) ON CONFLICT(resource_key) DO UPDATE SET until_at=excluded.until_at,reason=excluded.reason", ("job:" + str(row["id"]), stamp(self.clock() + dt.timedelta(seconds=30)), reason))
            self.event(c, row["id"], "admission_deferred", {"reason": reason}, self.now())
            return {"accepted": True, "deferred": True}

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
            resources = json.loads(row["resource_keys_json"])
            held_resource = resource
            if row["channel"] == "youtube" and row["family"] == "publish":
                from .youtube_isolation import task_resource
                scoped = task_resource(row["source_id"])
                # An already running generation retains its original descriptor.
                # Its exact channel is an identity alias, not a channel-wide lock.
                if scoped in resources and resource == "youtube:channel:" + row["account_label"]:
                    held_resource = scoped
            if held_resource not in resources:
                raise DispatchError("operation_resource_invalid", status=403)
            scoped = isolated_queue_resource(row)
            if scoped:
                self._refresh_queue_isolation(c, row)
                held_resource = scoped
            held = c.execute("SELECT * FROM pipeline_resource WHERE resource_key=?", (held_resource,)).fetchone()
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

    def correct_recovered_source_artifact(self, claim, artifact, preparation, proof, *, canonical_verified=False):
        """Correct the old X source-as-final projection, never its task outcome."""
        if not canonical_verified:
            raise DispatchError("canonical_receipt_required")
        if not isinstance(preparation, dict):
            return False
        with self.transaction() as c:
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (claim.get("job_id", claim.get("id")),)).fetchone()
            if (not row or row["channel"] != "x" or row["stage"] != "prepare"
                    or row["status"] != "completed" or row["external_intent"]
                    or row["generation"] <= claim.get("generation", 0)):
                return False
            saved = c.execute("SELECT * FROM pipeline_claim_history WHERE job_id=? AND generation=?", (row["id"], claim.get("generation"))).fetchone()
            if (not saved or saved["node_id"] != claim.get("node_id") or saved["worker_id"] != claim.get("worker_id")
                    or not secrets.compare_digest(saved["claim_hash"], digest(claim.get("claim_token", "")))):
                raise DispatchError("claim_conflict")
            old = json.loads(row["artifact_json"])
            frozen = json.loads(row["payload_json"])
            if (proof.get("source_key") != row["source_key"] or old.get("profile") != "source"
                    or not str(artifact.get("profile", "")).startswith("x-random-overlay-")
                    or frozen.get("kind") != "x_preflight"
                    or frozen.get("request", {}).get("random_request", {}).get("profile") != artifact.get("profile")
                    or not c.execute("SELECT 1 FROM pipeline_receipt WHERE receipt_id=? AND job_id=? AND generation=?",
                                     ("native-prepared-%s-%s" % (row["id"], row["generation"]), row["id"], row["generation"])).fetchone()
                    or c.execute("SELECT 1 FROM pipeline_resource WHERE job_id=?", (row["id"],)).fetchone()
                    or c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? AND status='intent'", (row["id"],)).fetchone()):
                return False
            # The already-native-validated result binds both the original source
            # and the final output; a mere node success flag cannot rewrite it.
            if (not old.get("sha256") or not old.get("size")
                    or preparation.get("source_sha256") != old["sha256"] or preparation.get("source_size") != old["size"]
                    or any(preparation.get(left) != artifact.get(right) for left, right in
                           (("output_sha256", "sha256"), ("output_size", "size"), ("output_url", "url"), ("profile", "profile"), ("recipe", "recipe")))
                    or not artifact.get("recipe")):
                return False
            c.execute("UPDATE pipeline_job SET artifact_json=? WHERE id=?", (dumps(artifact), row["id"]))
            self.event(c, row["id"], "qualified_final_artifact_corrected", {
                "generation": row["generation"], "source_generation": claim["generation"],
                "receipt_id": claim.get("receipt_id", ""), "old_sha256": old["sha256"],
                "sha256": artifact["sha256"], "size": artifact["size"], "profile": artifact["profile"]}, self.now())
            return True

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

    def validate_preparation_rejection(self, claim):
        with contextlib.closing(self.connect()) as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            if (not independent_x_preparation(row) or row["status"] not in {"running", "queued", "failed"}
                    or any(c.execute("SELECT 1 FROM " + table + " WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()
                           for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_receipt", "pipeline_publication"))):
                raise DispatchError("preparation_rejection_conflict")

    def reject_preparation_checkpoint(self, claim, request, code, *, canonical_verified=False):
        """Persist a definitive native rejection, never a successful checkpoint.

        The first HTTP call raises after commit so existing executors stop.
        Exact outbox redelivery ACKs this disposition, not media acceptance.
        """
        if not canonical_verified or code not in {"x_random_requires_video", "x_post_auto_template_duration_exceeded"}:
            raise DispatchError("preparation_rejection_unverified")
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            if (not independent_x_preparation(row) or request.get("phase") != "x_preflight"
                    or row["status"] not in {"running", "queued", "failed"}
                    or any(c.execute("SELECT 1 FROM " + table + " WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()
                           for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_receipt", "pipeline_publication"))):
                raise DispatchError("preparation_rejection_conflict")
            now = self.now()
            evidence = (row["id"], row["generation"], "x_preflight", digest(dumps(request.get("receipt", {}))), code, now)
            previous = c.execute("SELECT * FROM pipeline_preparation_rejection WHERE job_id=?", (row["id"],)).fetchone()
            if previous and tuple(previous)[:5] != evidence[:5]:
                raise DispatchError("preparation_rejection_conflict")
            c.execute("INSERT OR IGNORE INTO pipeline_preparation_rejection VALUES(?,?,?,?,?,?)", evidence)
            c.execute("UPDATE pipeline_job SET status='failed',error_code=?,error_message=?,finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END,updated_at=? WHERE id=?", (code, code, now, now, row["id"]))
            c.execute("DELETE FROM pipeline_resource WHERE job_id=? AND generation=?", (row["id"], row["generation"]))
            response = {"accepted": True, "status": "failed", "rejected_checkpoint": True, "error_code": code}
            c.execute("INSERT INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (request["checkpoint_id"], row["id"], row["generation"], digest(claim["claim_token"]), digest(dumps(request)), dumps(response)))
            self.event(c, row["id"], "preparation_checkpoint_rejected", {"code": code, "receipt_sha256": evidence[3]}, now)
            return response

    def acknowledge_preparation_rejection(self, claim, delivery_id, request, action):
        """Drain only authenticated callbacks for the proven rejected input.

        Older generations may acknowledge delivery but can never change the
        terminal job, its canonical failure, or a subsequently admitted retry.
        """
        if action not in {"jobs/checkpoint", "jobs/fail"} or not delivery_id:
            return None
        with self.transaction() as c:
            job_id = claim.get("job_id", claim.get("id"))
            rejection = c.execute("SELECT * FROM pipeline_preparation_rejection WHERE job_id=?", (job_id,)).fetchone()
            if not rejection:
                return None
            row = c.execute("SELECT * FROM pipeline_job WHERE id=?", (job_id,)).fetchone()
            if (not row or row["status"] != "failed" or row["generation"] != rejection["generation"]
                    or row["error_code"] != rejection["error_code"] or not independent_x_preparation(row)
                    or request.get("unknown_outcome")
                    or any(c.execute("SELECT 1 FROM " + table + " WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
                           for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_receipt", "pipeline_publication"))):
                return None
            history = c.execute("SELECT * FROM pipeline_claim_history WHERE job_id=? AND generation=?", (job_id, claim.get("generation"))).fetchone()
            if (not history or history["node_id"] != claim.get("node_id") or history["worker_id"] != claim.get("worker_id")
                    or not secrets.compare_digest(history["claim_hash"], digest(claim.get("claim_token", "")))):
                raise DispatchError("claim_conflict")
            if action == "jobs/checkpoint" and (request.get("phase") != rejection["phase"]
                    or digest(dumps(request.get("receipt", {}))) != rejection["receipt_sha256"]):
                return None
            response = {"accepted": True, "status": "failed", "rejected_checkpoint": True, "error_code": rejection["error_code"]}
            c.execute("INSERT INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (delivery_id, job_id, claim["generation"], history["claim_hash"], digest(dumps(request)), dumps(response)))
            return response

    def save_delivery(self, claim, delivery_id, request, response):
        if not delivery_id:
            return response
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (delivery_id, claim.get("job_id", claim.get("id")), claim["generation"], digest(claim["claim_token"]), digest(dumps(request)), dumps(response)))
        return response

    def acknowledge_failed_without_intent(self, claim, delivery_id, request):
        """ACK the worker's duplicate of a failed, unwritten reservation only."""
        if not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 256:
            return None
        with self.transaction() as c:
            row = self._validate_claim(c, claim, allow_expired=True)
            source = json.loads(row["source_meta_json"])
            if (row["status"] != "failed" or row["external_intent"] or row["canonical_receipt"]
                    or any(source.get(key) for key in ("source_attempt_count", "reconcile_only", "unknown_outcome", "publish_id", "legacy_inflight"))
                    or request.get("unknown_outcome", False)
                    or not row["error_code"] or request.get("error_code") != row["error_code"]
                    or c.execute("SELECT 1 FROM pipeline_operation WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()
                    or c.execute("SELECT 1 FROM pipeline_checkpoint WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()
                    or c.execute("SELECT 1 FROM pipeline_receipt WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()
                    or c.execute("SELECT 1 FROM pipeline_publication WHERE job_id=? LIMIT 1", (row["id"],)).fetchone()):
                return None
            identity = (row["id"], row["generation"], digest(claim["claim_token"]), digest(dumps(request)))
            old = c.execute("SELECT * FROM pipeline_delivery WHERE delivery_id=?", (delivery_id,)).fetchone()
            if old and tuple(old[k] for k in ("job_id", "generation", "claim_hash", "request_sha256")) != identity:
                raise DispatchError("delivery_identity_conflict")
            response = {"accepted": True, "status": "failed", "duplicate": True}
            # Reservation already failed before a native binding existed. Do
            # not call native fail again, replace the failure time, add samples,
            # or change a resource fence; just confirm this exact delivery.
            c.execute("INSERT OR IGNORE INTO pipeline_delivery VALUES(?,?,?,?,?,?)", (delivery_id, *identity, dumps(response)))
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
                    if pool.get("scaling_mode", "auto") == "manual":
                        continue
                    state = c.execute("SELECT * FROM pipeline_capacity_sample WHERE node_id=? AND stage=?", (node["node_id"], stage)).fetchone()
                    last = state["last_change"] if state else ""
                    high = state["high_since"] if state else ""
                    pressure = cpu > 85 or available < .15
                    is_gpu = "backend:gpu" in json.loads(node["capabilities_json"])
                    if stage == "prepare" and is_gpu:
                        pressure = pressure or metrics.get("gpu_percent", 0) > 95 or metrics.get("encoder_percent", 0) > 95 or metrics.get("gpu_memory_available_fraction", 1) < .1
                    high = (high or now) if pressure else ""
                    old = pool["target"]
                    # Low memory reduces admission at this evaluation; CPU/GPU
                    # utilization still needs sustained pressure for two minutes.
                    if available < .15 or (high and high <= stamp(self.clock() - dt.timedelta(seconds=120))):
                        pool["target"] = max(1 if pool["ceiling"] else 0, old - 1)
                        high = now
                    elif (not last or last <= stamp(self.clock() - dt.timedelta(seconds=300))) and cpu < 70 and available > .25:
                        samples = c.execute("SELECT count(*) FROM pipeline_job WHERE node_id=? AND stage=? AND finished_at>=? AND status IN ('completed','published')", (node["node_id"], stage, stamp(self.clock() - CAPACITY_SAMPLE_WINDOW))).fetchone()[0]
                        candidates = c.execute("SELECT channel,capabilities_json FROM pipeline_job WHERE stage=? AND status IN ('queued','ready') AND hold_reason='' AND scheduled_at<=?", (stage, now)).fetchall()
                        node_capabilities = set(json.loads(node["capabilities_json"])) - self.capability_exclusions.get(node["node_id"], set())
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

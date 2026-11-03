"""Canonical publication ledger projections and execution bridge contracts.

The dispatch database is an index, never the publication source of truth.  A
projection is deliberately not an authorization to publish: the owning channel
driver must atomically reserve the original item and run its native policy
checks before returning a portable execution descriptor.  No token, upload
session URI, lease secret, or raw source snapshot is copied to the index.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import http.client
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping


class AdapterError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


TERMINAL = frozenset({"published", "completed", "synced", "failed", "generation_failed", "enqueue_failed", "schedule_missed", "blocked_compliance", "abandoned", "failed_without_retry", "cancelled", "canceled", "skipped", "missed", "duplicate", "no_candidate"})
INFLIGHT = frozenset({"claimed", "selecting", "preparing", "generating", "running", "publishing", "uploading", "media_uploading", "post_creating", "repost_creating"})
UNCERTAIN = frozenset({"unknown", "unknown_outcome", "needs_review", "reconciling", "submitted", "processing"})
SECRET_KEYS = frozenset({"access_token", "refresh_token", "page_access_token", "token", "claim_token", "lease_token", "resumable_session_uri", "session_uri", "credential_envelope"})


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, (float, int)):
        value = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        if not value:
            return ""
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise AdapterError("naive_source_time")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def obj(value):
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def youtube_control_due(row):
    return (row.get("status") == "schedule_pending" and row.get("schedule_status") == "control_pending"
            and row.get("schedule_command") in {"reschedule", "immediate", "cancel"}
            and row.get("schedule_command_status") == "pending" and bool(row.get("schedule_operation_id"))
            and bool(row.get("video_id")) and not row.get("unknown_outcome") and not row.get("lease_owner"))


def youtube_cancelled(row):
    return (row.get("status") == "cancelled" and row.get("schedule_status") == "cancelled"
            and row.get("schedule_command_status") == "confirmed" and bool(row.get("video_id"))
            and not row.get("unknown_outcome"))


def youtube_existing_schedule(row, *, allow_control=False):
    """An uploaded legacy appointment may enter only the GET/comment lane."""
    control = allow_control and youtube_control_due(row)
    confirmed = row.get("schedule_confirmed_publish_at")
    if (row.get("workflow") != "reviewed_thumbnail" or row.get("privacy_status") != "public"
            or row.get("pipeline_dispatch_key") or row.get("lease_owner") or row.get("unknown_outcome")
            or row.get("_pipeline_history") or not row.get("video_id") or not confirmed
            or (row.get("comment_attempt_count") and not row.get("comment_id"))
            or (not control and (row.get("status") != "scheduled" or row.get("schedule_status") != "armed"
                                or row.get("schedule_command_status") != "confirmed"
                                or not row.get("publish_at") or stamp(confirmed) != stamp(row["publish_at"])))):
        return None
    return {"video_id": str(row["video_id"]), "scheduled_at": stamp(confirmed)}


def public_descriptor(value):
    """Reject secret-bearing adapters instead of silently storing their secrets."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in SECRET_KEYS:
                raise AdapterError("descriptor_contains_secret")
            public_descriptor(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            public_descriptor(child)
    return value


def canonical_fingerprint(row, identity):
    fields = {key: row.get(key) for key in (identity, "material_id", "content_id", "account_id", "page_id", "channel_id", "template_version", "gpu_job_id", "source_media_url", "material_url", "body_sha256")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def artifact_from(row):
    if row.get("_dispatch_artifact"):
        frozen = obj(row["_dispatch_artifact"])
        return {key: frozen[key] for key in ("url", "sha256", "size", "duration_seconds", "profile", "mime", "identity_mode", "recipe") if key in frozen}
    prepared = obj(row.get("prepared_json"))
    return {"url": str(prepared.get("media_url") or row.get("prepared_media_url") or ""),
            "sha256": str(prepared.get("sha256") or row.get("prepared_sha256") or row.get("prepared_output_sha256") or ""),
            "size": int(prepared.get("size_bytes") or row.get("prepared_size_bytes") or row.get("prepared_output_size") or 0),
            "duration_seconds": float(prepared.get("duration_seconds") or row.get("prepared_duration_seconds") or row.get("prepared_duration_sec") or 0),
            "profile": str(prepared.get("profile") or row.get("prepared_profile") or row.get("preparation_profile") or ""),
            "mime": "video/mp4"}


@dataclass(frozen=True)
class LedgerSpec:
    channel: str
    family: str
    path: str
    table: str
    identity: str = "id"
    state: str = "status"
    module: str = ""
    details_url: str = ""


def default_specs(paths):
    """Paths are provided by deployment; never copy a live SQLite database."""
    result = []
    def add(key, channel, family, table, identity="id", state="status", module="", page=""):
        if paths.get(key):
            result.append(LedgerSpec(channel, family, str(paths[key]), table, identity, state, module, page))
    add("x_auto", "x", "auto", "x_auto_task", module="x_accounts", page="/x-auto-post-records.html")
    add("x", "x", "queue", "x_post_queue", module="x_accounts", page="/x-post-logs.html")
    add("x_share", "x", "youtube_share", "youtube_share_item", module="x_accounts", page="/youtube-publish.html")
    add("fb", "fb", "auto", "fb_auto_task", module="fb_page_posts", page="/fb-auto-post-records.html")
    add("fb", "fb", "video", "fb_video_task", "task_id", module="fb_video_publish", page="/fb-video-publish-records.html")
    add("tt_auto", "tt", "auto", "tt_auto_task", module="tt_posts", page="/tt-auto-post-records.html")
    add("tt", "tt", "queue", "tt_post_queue", module="tt_posts", page="/tt-post-publish-logs.html")
    add("tt", "tt", "intake", "tt_post_material_intake", module="tt_posts", page="/tt-post-material-pool.html")
    add("tt", "tt", "direct_test", "tt_post_direct_test", module="tt_posts", page="/tt-post-publish-logs.html")
    add("youtube", "youtube", "preparation", "youtube_auto_preparation", state="state", module="youtube_auto_publish", page="/youtube-publish.html")
    add("youtube", "youtube", "publish", "drama_youtube_publish", module="youtube_auto_publish", page="/youtube-publish.html")
    return result


class LedgerAdapter:
    def __init__(self, spec: LedgerSpec, driver=None, *, cross_paths=None, preparation_policy=None):
        self.spec, self.driver = spec, driver
        self.cross_paths = cross_paths or {}
        self.preparation_policy = preparation_policy or {}
        ahead = self.preparation_policy.get("tt_auto_prepare_ahead_seconds", 0)
        if isinstance(ahead, bool) or not isinstance(ahead, int) or not 0 <= ahead <= 43200:
            raise AdapterError("tt_auto_prepare_ahead_invalid")
        self.cursor_state = {"active": None, "history": None, "recent": None, "observed_active": [], "recent_complete": False, "history_complete": False}

    def connect(self):
        conn = sqlite3.connect(Path(self.spec.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if self.spec.channel == "x" and self.spec.family == "queue" and self.cross_paths.get("x_auto"):
            conn.execute("ATTACH DATABASE ? AS pipeline_x_auto", (Path(self.cross_paths["x_auto"]).resolve().as_uri() + "?mode=ro",))
        return conn

    @staticmethod
    def _exists(conn, table):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _parent(self, conn, table, identity, row_id):
        if row_id is None or not self._exists(conn, table):
            return {}
        row = conn.execute(f"SELECT * FROM {table} WHERE {identity}=?", (row_id,)).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _x_origin_execution(row, origin):
        # X Auto owns the lease while its synchronous queue call renders on the
        # GPU.  The child queue/log remain queued/reserved until the first X API
        # operation; only this exact parent-child link proves active rendering.
        origin = dict(origin)
        row["legacy_parent_status"] = origin.get("status", "")
        row["legacy_parent_lease_until"] = origin.get("lease_expires_at_utc", "")
        row["legacy_parent_has_claim"] = bool(origin.get("claim_token"))
        row["legacy_parent_started_at"] = origin.get("updated_at") or origin.get("selected_at_utc", "")

    @staticmethod
    def _backlog_proof(conn, source_key, native_row, *, schema="main"):
        from .backlog_contract import disposition_record, source_fingerprint
        action = disposition_record(conn, source_key, native_row, schema=schema)
        if action not in {"resume", "archive"}:
            return None
        saved = conn.execute("SELECT plan_sha256,action,state,source_fingerprint,details_json FROM " + schema + ".pipeline_backlog_disposition WHERE source_key=?", (source_key,)).fetchone()
        if saved is None or saved["action"] != action:
            return None
        identity = obj(saved["details_json"]).get("identity")
        if (saved["state"] != "committed" or not isinstance(identity, dict)
                or identity.get("source_key") != source_key
                or not re.fullmatch(r"[a-f0-9]{64}", str(saved["plan_sha256"]))
                or source_fingerprint(identity) != saved["source_fingerprint"]):
            return None
        return {"source_key": source_key, "action": action, "plan_sha256": saved["plan_sha256"],
                "source_fingerprint": saved["source_fingerprint"], "identity": identity}

    def enrich(self, conn, raw):
        row = dict(raw)
        key = (self.spec.channel, self.spec.family)
        parent = {}
        if key == ("x", "queue"):
            if self._exists(conn, "pipeline_x_artifact"):
                frozen = conn.execute("SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?", (row["id"],)).fetchone()
                if frozen and not row.get("random_template_enabled"):
                    row["_dispatch_artifact"] = obj(frozen["artifact_json"])
            auto_owner = {}
            if self.cross_paths.get("x_auto"):
                origin = conn.execute("SELECT * FROM pipeline_x_auto.x_auto_task WHERE execution_queue_id=?", (row["id"],)).fetchall()
                if len(origin) == 1:
                    self._x_origin_execution(row, origin[0])
                    row["ancestor_source_keys"] = ["x:auto:" + str(origin[0]["id"])]
                    row["origin_created_at"] = origin[0]["created_at"]
                    row["scheduled_at"] = origin[0]["scheduled_at_utc"]
                    template = conn.execute("SELECT * FROM pipeline_x_auto.x_auto_template WHERE id=?", (origin[0]["template_id"],)).fetchone()
                    auto_owner = dict(template) if template else {}
                    native_parent = dict(origin[0])
                    if (str(native_parent.get("execution_queue_id")) == str(raw["id"])
                            and native_parent.get("account_id") is not None and raw["account_id"] is not None
                            and str(native_parent["account_id"]) == str(raw["account_id"])
                            and native_parent.get("material_id") and dict(raw).get("material_id")
                            and str(native_parent["material_id"]) == str(raw["material_id"])):
                        proof = self._backlog_proof(conn, "x:auto:" + str(native_parent["id"]), native_parent, schema="pipeline_x_auto")
                        if proof and proof["action"] == "resume":
                            row["_backlog_ancestor_authorization"] = proof
            log = conn.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (row["id"],)).fetchone() if self._exists(conn, "x_post_publish_log") else None
            if log:
                log = dict(log)
                row.update({"publish_attempt_count": log.get("attempt_count", 0), "unknown_outcome": log.get("unknown_outcome", 0), "publish_id": log.get("x_post_id", ""), "ledger_status": log.get("status", ""), "execution_log_id": log.get("id")})
                row["started_at"] = log.get("started_at", "")
                row["finished_at"] = log.get("published_at") or log.get("updated_at", "") if log.get("status") in TERMINAL else ""
            if self._exists(conn, "x_post_random_template_preparation"):
                prep = conn.execute("SELECT status,request_json,result_json FROM x_post_random_template_preparation WHERE queue_id=?", (row["id"],)).fetchone()
                if row.get("random_template_enabled") and prep and prep["status"] == "ready":
                    from features.x_posts.random_template import validate_receipt
                    result = obj(prep["result_json"])
                    validate_receipt(obj(prep["request_json"]), result)
                    # The portable source probe is input to this render, not
                    # its finished output. Recovery and publish offers must use
                    # the same validated final artifact as the native publisher.
                    row["_dispatch_artifact"] = {"url": result["output_url"], "sha256": result["output_sha256"],
                        "size": result["output_size"], "mime": "video/mp4", "duration_seconds": result["probe"]["duration"],
                        "profile": result["profile"], "recipe": result["recipe"]}
                    row.update(random_prepared=True, prepared_media_url=result.get("output_url", ""),
                               prepared_sha256=result.get("output_sha256", ""), prepared_size_bytes=result.get("output_size", 0),
                               prepared_profile=result.get("profile", ""))
            if row.get("manual_run_id"):
                parent = self._parent(conn, "x_post_manual_run", "id", row["manual_run_id"])
                row["source_family"] = parent.get("trigger_source", "manual")
            elif row.get("schedule_run_id"):
                parent = self._parent(conn, "x_post_schedule_run", "id", row["schedule_run_id"])
                row["source_family"] = parent.get("source_type", "schedule")
            if self.cross_paths.get("x_auto") and not row.get("ancestor_source_keys"):
                match = re.fullmatch(r"x-auto-task-([1-9][0-9]*)", str(parent.get("external_task_key") or ""))
                origin = conn.execute("SELECT * FROM pipeline_x_auto.x_auto_task WHERE id=?", (int(match[1]),)).fetchone() if match else None
                if (origin and parent.get("template_ref") == "x-auto-template-%s" % origin["template_id"]
                        and str(parent.get("template_version")) == str(origin["template_version"])
                        and str(row.get("account_id")) == str(origin["account_id"])
                        and str(row.get("material_id")) == str(origin["material_id"])):
                    self._x_origin_execution(row, origin)
                    row["ancestor_source_keys"] = ["x:auto:" + str(origin["id"])]
                    row["origin_created_at"], row["scheduled_at"] = origin["created_at"], origin["scheduled_at_utc"]
                    template = conn.execute("SELECT * FROM pipeline_x_auto.x_auto_template WHERE id=?", (origin["template_id"],)).fetchone()
                    auto_owner = dict(template) if template else {}
            row["batch_key"] = "x:" + str(row.get("manual_run_id") or row.get("schedule_run_id") or row.get("run_id") or row["id"])
            row["origin_created_at"] = row.get("origin_created_at") or parent.get("created_at") or parent.get("created_at_utc")
            if auto_owner:
                parent = auto_owner
            elif row.get("drama_pool_item_id"):
                pool_owner = self._parent(conn, "x_post_drama_pool", "id", row["drama_pool_item_id"])
                if pool_owner.get("created_by_user_id"):
                    parent = pool_owner
            elif row.get("pool_item_id"):
                pool_owner = self._parent(conn, "x_post_material_pool", "id", row["pool_item_id"])
                if pool_owner.get("created_by_user_id"):
                    parent = pool_owner
        elif key in {("x", "auto"), ("tt", "auto"), ("fb", "auto")}:
            prefix = self.spec.channel + "_auto"
            parent = self._parent(conn, prefix + "_run", "id", row.get("run_id"))
            template = self._parent(conn, prefix + "_template", "id", row.get("template_id"))
            parent = {**template, **parent}
            if template.get("owner_user_id") or template.get("created_by_user_id"):
                parent["owner_user_id"] = template.get("owner_user_id") or template["created_by_user_id"]
                parent["owner_tenant_key"] = template.get("owner_tenant_key") or template.get("created_by_tenant_key", "")
            if key == ("fb", "auto") and self._exists(conn, "fb_auto_publish_attempt"):
                row["publish_attempt_count"] = conn.execute("SELECT COUNT(*) FROM fb_auto_publish_attempt WHERE task_id=?", (row["id"],)).fetchone()[0]
        elif key == ("x", "youtube_share"):
            parent = self._parent(conn, "youtube_share_run", "id", row.get("run_id"))
            row["publish_attempt_count"] = int(bool(row.get("attempted_at")))
            row["scheduled_at"] = stamp(parent.get("created_at")) if parent.get("created_at") else ""
            row["origin_created_at"] = parent.get("created_at")
        elif key == ("fb", "video"):
            parent = obj(row.get("snapshot_json")).get("actor", {})
            if self._exists(conn, "fb_video_attempt"):
                row["publish_attempt_count"] = conn.execute("SELECT COUNT(*) FROM fb_video_attempt WHERE task_id=?", (row["task_id"],)).fetchone()[0]
        elif key == ("tt", "queue"):
            parent = self._parent(conn, "tt_post_material_pool", "id", row.get("pool_item_id"))
            if row.get("gpu_job_id") and self._exists(conn, "tt_post_material_intake"):
                origin = conn.execute("SELECT id FROM tt_post_material_intake WHERE gpu_job_id=? AND material_id=? AND account_id=?",
                                      (row["gpu_job_id"], row["material_id"], row["account_id"])).fetchall()
                if len(origin) == 1:
                    row["ancestor_source_keys"] = ["tt:intake:" + str(origin[0]["id"])]
        elif key == ("youtube", "preparation"):
            body = obj(row.get("body"))
            row["scheduled_at"] = row.get("created_at")
            row["cutover_due_at"] = body.get("publish_at") or row.get("created_at")
            row["channel_id"] = body.get("channel_id") or obj(body.get("channel")).get("channel_id", "")
            if row.get("state") == "generating" and self._exists(conn, "pipeline_youtube_claim"):
                claim = conn.execute("SELECT task_json FROM pipeline_youtube_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1",
                                     ("youtube:preparation:" + str(row["id"]),)).fetchone()
                saved = obj(claim[0]) if claim else {}
                lease = str(row.get("lease_token") or "")
                row["_youtube_dispatch_owned"] = bool(
                    lease.startswith("pipeline:") and saved.get("id") == row["id"]
                    and saved.get("lease_owner") == lease
                    and saved.get("version") == row.get("version") == body.get("current_version"))
            if row.get("state") == "uploading" and body.get("publish_id"):
                child = self._parent(conn, "drama_youtube_publish", "id", body["publish_id"])
                row["_youtube_publication_handoff"] = bool(child and str(child.get("preparation_id")) == str(row["id"]))
        elif key == ("youtube", "publish"):
            source_key = "youtube:publish:" + str(row["id"])
            if self._exists(conn, "drama_youtube_publish_event"):
                retry = conn.execute("SELECT id FROM drama_youtube_publish_event WHERE task_id=? AND phase='operator_retry' AND outcome='requested' ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
                row["_youtube_retry_revision"] = str(retry[0]) if retry else ""
            row["_pipeline_history"] = any(
                self._exists(conn, table) and conn.execute("SELECT 1 FROM " + table + " WHERE source_key=? LIMIT 1", (source_key,)).fetchone()
                for table in ("pipeline_youtube_claim", "pipeline_youtube_operation", "pipeline_youtube_checkpoint"))
        row["owner_user_id"] = str(row.get("owner_user_id") or row.get("created_by_user_id") or row.get("owner") or row.get("operator_user_id") or parent.get("owner_user_id") or parent.get("created_by_user_id") or parent.get("actor_user_id") or parent.get("actor_user") or parent.get("user_id") or "")
        row["owner_tenant_key"] = str(row.get("owner_tenant_key") or row.get("tenant_key") or row.get("tenant") or parent.get("owner_tenant_key") or parent.get("created_by_tenant_key") or parent.get("actor_tenant_key") or parent.get("actor_tenant") or parent.get("tenant_key") or "")
        row["owner_name"] = str(row.get("owner_name") or row.get("created_by_name") or parent.get("created_by_name") or parent.get("created_name") or parent.get("actor_name") or "")
        row["owner_namespace"] = "fb_page_owner" if key == ("fb", "auto") else "session_user"
        row["scheduled_at"] = row.get("scheduled_at") or row.get("scheduled_at_utc") or row.get("planned_at_utc") or row.get("planned_publish_at_utc") or parent.get("scheduled_at_utc") or parent.get("scheduled_at") or row.get("created_at_utc") or row.get("created_at") or parent.get("created_at")
        if self._exists(conn, "pipeline_canonical_hold"):
            key = "%s:%s:%s" % (self.spec.channel, self.spec.family, row[self.spec.identity])
            row["canonical_held"] = bool(conn.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (key,)).fetchone())
        # Identity is verified against the untouched native row. X queue has
        # no schedule column: use only its owning native parent's original
        # schedule, before project() derives a preparation eligibility time.
        native_identity = dict(raw)
        if (self.spec.channel, self.spec.family) == ("x", "queue"):
            native_identity["scheduled_at"] = row.get("scheduled_at")
        proof = self._backlog_proof(conn, "%s:%s:%s" % (self.spec.channel, self.spec.family, raw[self.spec.identity]), native_identity)
        if proof:
            row["_backlog_authorization"] = proof
        return row

    def project(self, row, now=None):
        spec = self.spec
        state = str(row.get(spec.state) or "")
        # Auto tasks bridged to canonical X queues must never generate a second job.
        bridged_auto = spec.channel == "x" and spec.family == "auto" and bool(row.get("execution_queue_id"))
        terminal = bridged_auto or state in TERMINAL or (spec.channel == "tt" and spec.family == "intake" and state == "ready")
        isolated_youtube = spec.channel == "youtube" and spec.family == "publish" and (
            row.get("workflow", "reviewed_thumbnail") not in {"reviewed_thumbnail", "legacy"} or row.get("privacy_status", "public") != "public")
        review_hold = spec.channel == "youtube" and spec.family == "preparation" and state not in {"queued_generation", "generating"} and not terminal
        pause_eligible = review_hold and state in {"review", "enqueue_pending"}
        source_id = str(row[spec.identity])
        source_key = f"{spec.channel}:{spec.family}:{source_id}"
        unknown = bool(row.get("unknown_outcome")) or state in UNCERTAIN or row.get("ledger_status") in UNCERTAIN
        attempts = int(row.get("publish_attempt_count") or row.get("video_attempt_count") or 0)
        if spec.channel == "tt" and spec.family == "queue":
            # claim retries precede the platform boundary and are not publishes.
            attempts = int(bool(row.get("publish_started_at_utc") or row.get("publish_id")))
        started = state in INFLIGHT or row.get("ledger_status") in INFLIGHT or (spec.channel == "fb" and spec.family == "video" and row.get("prepare_state") == "preparing")
        youtube_dispatch_owned = spec.channel == "youtube" and spec.family == "preparation" and bool(row.get("_youtube_dispatch_owned"))
        if youtube_dispatch_owned:
            # The dispatch job and generation journal already own this work.
            # A lost descriptor must not prevent enabling its own recovery by
            # being mistaken for a still-running pre-cutover legacy worker.
            started = False
        youtube_handoff_complete = False
        if (spec.channel == "youtube" and spec.family == "preparation" and state == "uploading"
                and row.get("_youtube_publication_handoff") and "lease_until" in row):
            # The approved-cover workflow keeps this parent label after enqueue.
            # Its exact child ledger owns upload/schedule/unknown state; a parent
            # without an active lease is no longer an executing cover worker.
            try:
                if stamp(float(row.get("lease_until") or 0)) <= stamp(now):
                    started = False
                    youtube_handoff_complete = True
            except (TypeError, ValueError, OverflowError):
                pass  # An invalid lease is not proof that legacy work ended.
        legacy_parent_inflight = (spec.channel == "x" and spec.family == "queue" and not terminal
                                  and row.get("legacy_parent_status") in INFLIGHT
                                  and bool(row.get("legacy_parent_has_claim"))
                                  and bool(row.get("legacy_parent_lease_until"))
                                  and stamp(row["legacy_parent_lease_until"]) > stamp(now))
        started = started or legacy_parent_inflight
        artifact = artifact_from(row)
        stage = "prepare"
        if state in {"ready", "queued", "reserved"} or terminal:
            stage = "publish" if artifact["url"] or spec.family in {"queue", "youtube_share", "publish"} else "prepare"
        if spec.channel == "x" and spec.family == "queue":
            portable = bool(row.get("_dispatch_artifact"))
            sha = row.get("preflight_sha256") or row.get("output_sha256") or row.get("prepared_sha256") or row.get("source_sha256")
            media_unfrozen = not portable and (row.get("media_validation_mode") == "deferred" or not sha or not row.get("preflight_size") or not row.get("preflight_duration"))
            stage = "prepare" if media_unfrozen or (bool(row.get("random_template_enabled")) and not row.get("random_prepared")) else "publish"
        if spec.channel == "fb" and spec.family == "video":
            stage = "publish" if row.get("prepare_state") == "ready" else "prepare"
        blocked_link = spec.channel == "fb" and spec.family == "video" and stage == "publish" and row.get("link_state") != "ready" and not terminal
        if spec.channel == "youtube" and spec.family == "preparation":
            # Review/enqueue are owner/central actions, not media jobs.
            stage = "prepare"
        if spec.channel == "tt" and spec.family == "queue" or spec.channel == "youtube" and spec.family == "publish":
            stage = "publish"
        if spec.channel == "fb" and spec.family == "auto":
            stage = "prepare" if state in {"planned", "preparing"} or (terminal and not artifact["url"]) else "publish"
        if spec.channel == "tt" and spec.family == "intake":
            stage = "prepare"
        if spec.channel == "tt" and spec.family == "direct_test":
            stage = "publish" if state in {"ready", "publishing", "reconciling", "published", "unknown"} or artifact["url"] else "prepare"
        if spec.channel in {"x", "tt"} and spec.family == "auto":
            stage = "publish" if state in {"ready", "publishing", "reconciling", "unknown", "published"} or (terminal and artifact["url"]) or (state == "retry_wait" and row.get("claim_phase") in {"publish", "reconcile"}) else "prepare"
        if unknown or attempts or started:
            stage = "publish" if unknown or attempts else stage
        if spec.family not in {"preparation", "intake"} and state in {"published", "synced", "completed"} and (row.get("publish_id") or row.get("post_id") or row.get("x_post_id") or row.get("graph_post_id") or row.get("video_id")):
            stage = "publish"
        if bridged_auto:
            stage, started, unknown, attempts = "prepare", False, False, 0
        resources = []
        if spec.channel in {"x", "tt"}:
            for key in ("account_id", "relay_account_id"):
                if row.get(key):
                    resources.append(f"{spec.channel}:account:{row[key]}")
        elif spec.channel == "fb" and row.get("page_id"):
            resources.append("fb:page:" + str(row["page_id"]))
        elif spec.channel == "youtube":
            body = obj(row.get("body")) if spec.family == "preparation" else {}
            identity = row.get("channel_id") or body.get("channel_id") or obj(body.get("channel")).get("channel_id")
            if identity:
                # Cover generation only writes this preparation's private asset.
                # Historical uncertain uploads must fence new platform writes,
                # without preventing the owner from generating/reviewing covers.
                resources.append("youtube:cover:" + source_id if spec.family == "preparation"
                                 else "youtube:channel:" + str(identity))
        payload = {"source_key": source_key, "native_id": source_id, "native_family": spec.family}
        # Rendering specifications are frozen by reserve(), after native checks.
        fingerprint = canonical_fingerprint(row, spec.identity)
        payload["canonical_fingerprint"] = fingerprint
        payload["source_attempt_count"] = attempts
        payload["reconcile_only"] = unknown or attempts > 0
        adopt_schedule = youtube_existing_schedule(row, allow_control=True) if spec.channel == "youtube" and spec.family == "publish" else None
        if adopt_schedule:
            payload.update(continuation=adopt_schedule, adopt_existing_schedule=True)
        publish_id = str(row.get("publish_id") or row.get("post_id") or row.get("x_post_id") or row.get("graph_post_id") or row.get("video_id") or "")
        video_published = spec.channel == "youtube" and spec.family == "publish" and row.get("video_state") == "published" and bool(row.get("video_id"))
        skipped = state in {"no_candidate", "skipped", "duplicate", "cancelled", "canceled", "abandoned"} and not (publish_id or unknown or attempts)
        completed_preparation = stage == "prepare" and (bridged_auto or state in {"ready", "completed", "synced"} or review_hold)
        confirmed = stage == "publish" and state in {"published", "synced", "completed"} and bool(publish_id) and not unknown
        projected_status = "queued" if pause_eligible else (("completed" if completed_preparation or skipped else ("published" if confirmed else "unknown" if state in {"published", "synced", "completed"} else "failed")) if terminal or review_hold else ("legacy_running" if started else ("unknown" if unknown or attempts else "queued")))
        if spec.channel == "youtube" and spec.family == "publish" and youtube_cancelled(row):
            projected_status = "completed"
        if adopt_schedule:
            projected_status = "ready"
        confirmed_schedule = None
        if (spec.channel == "youtube" and spec.family == "publish" and state == "scheduled"
                and row.get("schedule_status") == "armed" and not unknown and not row.get("lease_owner")
                and publish_id and row.get("publish_at")
                and stamp(row.get("schedule_confirmed_publish_at") or "") == stamp(row["publish_at"])):
            confirmed_schedule = {"video_id": publish_id, "scheduled_at": stamp(row["publish_at"])}
        if row.get("canonical_held"):
            projected_status = "backlog_paused"
        started_at = row.get("started_at_utc") or row.get("started_at") or row.get("claimed_at_utc") or row.get("claimed_at") or row.get("selected_at_utc")
        if legacy_parent_inflight and not started_at:
            started_at = row.get("legacy_parent_started_at")
        finished_at = row.get("finished_at_utc") or row.get("finished_at") or row.get("completed_at_utc") or row.get("published_at_utc") or row.get("video_published_at_utc") or ((row.get("updated_at_utc") or row.get("updated_at")) if terminal or review_hold else "")
        source_revision = str(row.get("_youtube_retry_revision") or "")
        if spec.channel == "youtube" and spec.family == "preparation":
            body = obj(row.get("body"))
            version = row.get("version") or body.get("current_version")
            if isinstance(version, int) and not isinstance(version, bool) and version > 0:
                source_revision = str(version) + ":" + str(body.get("generation_retry_id") or "")
        publish_at = stamp(row.get("scheduled_at")) if row.get("scheduled_at") else ""
        eligible_at = publish_at
        if stage == "prepare" and not terminal and not review_hold:
            created_at = row.get("origin_created_at") or row.get("created_at_utc") or row.get("created_at")
            created_at = stamp(created_at) if created_at else publish_at
            if spec.channel == "fb" and spec.family in {"auto", "video"}:
                # Canonical planning already bounded eligible material selection.
                # Both FB preparers may render a frozen future publication now.
                retry_at = row.get("next_prepare_at_utc") if spec.family == "auto" else row.get("prepare_retry_at")
                eligible_at = max(created_at, stamp(retry_at) if retry_at else "")
            elif spec.channel == "tt" and spec.family == "auto":
                if state in {"pending", "selecting"} and publish_at:
                    ahead = self.preparation_policy.get("tt_auto_prepare_ahead_seconds", 0)
                    eligible_at = max(created_at, stamp(datetime.fromisoformat(publish_at.replace("Z", "+00:00")) - timedelta(seconds=ahead)))
                else:
                    # A canonical reservation has already passed selection's
                    # lookahead boundary; retries retain the native retry time.
                    eligible_at = created_at
                if row.get("next_attempt_at_utc"):
                    eligible_at = max(eligible_at, stamp(row["next_attempt_at_utc"]))
        return {"source_key": source_key, "channel": spec.channel, "family": spec.family, "source_id": source_id,
                "backlog_authorization": row.get("_backlog_authorization"),
                "backlog_ancestor_authorization": row.get("_backlog_ancestor_authorization"),
                "source_revision": source_revision,
                "confirmed_youtube_schedule": confirmed_schedule,
                "ancestor_source_keys": list(row.get("ancestor_source_keys") or []),
                "batch_key": str(row.get("batch_key") or f"{spec.channel}:{spec.family}:{row.get('run_id') or row.get('batch_id') or source_id}"),
                "stage": stage, "scheduled_at": (stamp(now) if youtube_control_due(row) else adopt_schedule["scheduled_at"]) if adopt_schedule else eligible_at,
                "cutover_due_at": stamp(row.get("cutover_due_at") or row.get("publish_at") or row.get("scheduled_at") or row.get("created_at_utc") or row.get("created_at")) if (row.get("cutover_due_at") or row.get("publish_at") or row.get("scheduled_at") or row.get("created_at_utc") or row.get("created_at")) else "",
                "owner_user_id": row.get("owner_user_id", ""), "owner_tenant_key": row.get("owner_tenant_key", ""),
                "owner_namespace": row.get("owner_namespace", "session_user"), "native_owner_id": row.get("owner_user_id", ""), "owner_name": row.get("owner_name", ""),
                "required_modules": [spec.module], "required_module": spec.module,
                "source_created_at": stamp(row.get("origin_created_at") or row.get("created_at_utc") or row.get("created_at") or row.get("scheduled_at")) if (row.get("origin_created_at") or row.get("created_at_utc") or row.get("created_at") or row.get("scheduled_at")) else "",
                "account_label": str(row.get("account_username") or row.get("username") or row.get("page_id") or row.get("channel_id") or ""),
                "resource_keys": sorted(set(resources)),
                "capabilities": [f"{stage}:{spec.channel}"] + (["profile:" + row["preparation_profile"]] if spec.channel == "tt" and spec.family in {"intake", "direct_test"} and stage == "prepare" and row.get("preparation_profile") else []), "payload": payload, "artifact": artifact,
                "source_status": state, "details_url": spec.details_url, "legacy_inflight": bool(started),
                "legacy_parent_inflight": bool(legacy_parent_inflight),
                "source_attempt_count": attempts, "unknown_outcome": unknown, "reconcile_only": unknown or attempts > 0,
                "status": projected_status,
                "canonical_fingerprint": fingerprint,
                "started_at": stamp(started_at) if started_at else "",
                "finished_at": stamp(finished_at) if finished_at else "",
                "publish_id": publish_id,
                "video_published": video_published,
                "video_published_at": stamp(row["video_published_at_utc"]) if video_published and row.get("video_published_at_utc") else "",
                "continuation_due": spec.channel == "youtube" and spec.family == "publish" and youtube_control_due(row),
                "source_attempt_count": attempts, "observation_only": terminal or review_hold or isolated_youtube, "metadata_only": bridged_auto or skipped or isolated_youtube,
                "pause_eligible": not isolated_youtube and not youtube_handoff_complete and not youtube_dispatch_owned and (pause_eligible or (not terminal and not unknown and not started and not attempts)),
                "hold_reason": "cutover_overdue" if row.get("canonical_held") else "canonical_isolated_workflow" if isolated_youtube else "canonical_link_pending" if blocked_link else "canonical_review_or_enqueue" if review_hold else "",
                "execution_available": self.driver is not None and not getattr(self.driver, "control_only", False) and not terminal and not review_hold and not isolated_youtube and not unknown and not started and (not attempts or bool(adopt_schedule)) and not blocked_link and not row.get("canonical_held")}

    def scan(self, limit=200, now=None):
        limit = max(1, min(int(limit), 5000))
        with self.connect() as conn:
            if not self._exists(conn, self.spec.table):
                return []
            terminal_states = set(TERMINAL)
            if self.spec.channel == "tt" and self.spec.family == "intake":
                terminal_states.add("ready")
            terminal = tuple(sorted(terminal_states))
            marks = ",".join("?" for _ in terminal)
            identity, table, state = self.spec.identity, self.spec.table, self.spec.state
            # Prioritize the dashboard's recent window and newly arriving work
            # while historical IDs continue to advance in independent pages.
            columns = {r[1]: str(r[2]).upper() for r in conn.execute(f"PRAGMA table_info({table})")}
            time_fields = [name for name in ("created_at_utc", "created_at", "updated_at_utc", "updated_at", "completed_at_utc", "finished_at", "video_published_at_utc") if name in columns]
            recent = []
            if time_fields:
                since = datetime.fromisoformat(stamp(now).replace("Z", "+00:00")) - timedelta(days=7)
                recent_filter = " OR ".join(name + ">=?" for name in time_fields)
                recent_args = [since.timestamp() if columns[name] in {"REAL", "INTEGER", "NUMERIC"} else since.strftime("%Y-%m-%d") for name in time_fields]
                cursor = self.cursor_state.get("recent")
                recent = conn.execute(f"SELECT * FROM {table} WHERE ({recent_filter}) AND (? IS NULL OR {identity}>?) ORDER BY {identity} LIMIT ?", (*recent_args, cursor, cursor, limit)).fetchall()
                self.cursor_state["recent"] = recent[-1][identity] if recent else None
                if len(recent) < limit:
                    self.cursor_state["recent_complete"] = True
                    self.cursor_state["recent"] = None
            else:
                self.cursor_state["recent_complete"] = True
            cursor = self.cursor_state.get("active")
            rows = conn.execute(f"SELECT * FROM {table} WHERE {state} NOT IN ({marks}) AND (? IS NULL OR {identity}>?) ORDER BY {identity} LIMIT ?", (*terminal, cursor, cursor, limit)).fetchall()
            if not rows and cursor is not None:
                rows = conn.execute(f"SELECT * FROM {table} WHERE {state} NOT IN ({marks}) ORDER BY {identity} LIMIT ?", (*terminal, limit)).fetchall()
            self.cursor_state["active"] = rows[-1][identity] if rows else None
            # Rotate the IDs previously observed as active. Each query is bounded
            # but an old running item remains watched until it becomes terminal.
            pending = list(self.cursor_state.get("observed_active", []))
            watched, waiting = pending[:limit], pending[limit:]
            refreshed = []
            if watched:
                refreshed = conn.execute(f"SELECT * FROM {table} WHERE {identity} IN ({','.join('?' for _ in watched)})", watched).fetchall()
            still_active = [row[identity] for row in refreshed if row[state] not in terminal_states]
            self.cursor_state["observed_active"] = list(dict.fromkeys([*waiting, *still_active, *(row[identity] for row in rows)]))
            history = self.cursor_state.get("history")
            historical = conn.execute(f"SELECT * FROM {table} WHERE {state} IN ({marks}) AND (? IS NULL OR {identity}>?) ORDER BY {identity} LIMIT ?", (*terminal, history, history, limit)).fetchall()
            if len(historical) < limit:
                self.cursor_state["history_complete"] = True
            if not historical and history is not None:
                # A task can finish before its first active page was scanned.
                # A bounded cyclic history sweep eventually observes it too.
                historical = conn.execute(f"SELECT * FROM {table} WHERE {state} IN ({marks}) ORDER BY {identity} LIMIT ?", (*terminal, limit)).fetchall()
            if historical:
                self.cursor_state["history"] = historical[-1][identity]
            all_rows = {str(row[identity]): row for row in [*historical, *refreshed, *rows, *recent]}
            projected = [self.project(self.enrich(conn, row), now) for row in all_rows.values()]
            return [item for item in projected if item is not None]

    def read(self, identity):
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM {self.spec.table} WHERE {self.spec.identity}=?", (identity,)).fetchone()
            return self.enrich(conn, row) if row else None

    def owns_frozen_tt_selection(self, row, job, claim, expected_fingerprint):
        """Prove only our previously unselected TT task gained its frozen media."""
        if ((self.spec.channel, self.spec.family, job.get("stage")) != ("tt", "auto", "prepare")
                or row.get("status") != "preparing" or row.get("claim_phase") not in {"selection", "prepare"}
                or not str(row.get("claim_worker", "")).startswith("pipeline:")
                or row.get("unknown_outcome") or row.get("publish_attempt_count") or row.get("publish_id")):
            return False
        original = dict(row, gpu_job_id="", source_media_url="")
        unselected = dict(original, material_id="", content_id="")
        if expected_fingerprint not in {canonical_fingerprint(original, self.spec.identity), canonical_fingerprint(unselected, self.spec.identity)}:
            return False  # No other identity change, including account/template, is allowed.
        with self.connect() as conn:
            if not self._exists(conn, "pipeline_tt_claim") or not self._exists(conn, "pipeline_tt_operation"):
                return False
            frozen = conn.execute("SELECT * FROM pipeline_tt_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1", (job["source_key"],)).fetchone()
            if (not frozen or frozen["receipt_json"] or frozen["generation"] > int(job["generation"])
                    or not row.get("claim_token") or frozen["native_token"] != row["claim_token"]
                    or conn.execute("SELECT 1 FROM pipeline_tt_operation WHERE source_key=? LIMIT 1", (job["source_key"],)).fetchone()):
                return False
            if (frozen["generation"] == int(job["generation"])
                    and frozen["claim_hash"] != hashlib.sha256(str(claim.get("claim_token") or "").encode()).hexdigest()):
                return False
            descriptor = obj(frozen["descriptor_json"])
        request, identity = obj(descriptor.get("request")), obj(descriptor.get("native_identity"))
        return (descriptor.get("kind") == "tt_prepare" and descriptor.get("source_key") == job["source_key"]
                and descriptor.get("account_resource") == "tt:account:" + str(row.get("account_id"))
                and identity.get("account_id") == str(row.get("account_id"))
                and identity.get("material_id") == str(row.get("material_id"))
                and bool(request.get("job_id")) and request["job_id"] == row.get("gpu_job_id")
                and bool(request.get("content_id")) and request["content_id"] == row.get("content_id")
                and bool(request.get("source_url")) and request["source_url"] == row.get("source_media_url")
                and bool(request.get("expected_profile")))


class AdapterRegistry:
    def __init__(self, adapters=()):
        self.adapters = {(a.spec.channel, a.spec.family): a for a in adapters}
        self.errors = {}

    @classmethod
    def from_paths(cls, paths, drivers=None, *, preparation_policy=None):
        drivers = drivers or {}
        return cls(LedgerAdapter(spec, drivers.get((spec.channel, spec.family)), cross_paths=paths, preparation_policy=preparation_policy) for spec in default_specs(paths))

    def _adapter(self, job):
        try:
            return self.adapters[(job["channel"], job["family"])]
        except KeyError:
            raise AdapterError("canonical_adapter_missing") from None

    def scan(self, limit=200, now=None):
        result, self.errors = [], {}
        # Each family gets a bounded page: a busy channel cannot hide all others.
        per_family = max(1, min(int(limit), 1000))
        for key, adapter in self.adapters.items():
            try:
                result.extend(adapter.scan(per_family, now))
            except (OSError, sqlite3.Error, AdapterError) as exc:
                self.errors[":".join(key)] = getattr(exc, "code", "canonical_ledger_unavailable")
        return sorted(result, key=lambda x: (x["scheduled_at"], x["source_key"]))

    def reserve(self, job, claim):
        adapter = self._adapter(job)
        if adapter.driver is None or getattr(adapter.driver, "control_only", False):
            raise AdapterError("canonical_execution_not_connected")
        row = adapter.read(job["source_id"])
        current = adapter.project(row) if row else None
        if current is None or current.get("observation_only"):
            raise AdapterError("canonical_task_terminal_or_missing")
        expected_fingerprint = job.get("canonical_fingerprint", obj(job.get("payload")).get("canonical_fingerprint", current["canonical_fingerprint"]))
        if (current["canonical_fingerprint"] != expected_fingerprint
                and not adapter.owns_frozen_tt_selection(row, job, claim, expected_fingerprint)):
            raise AdapterError("canonical_identity_changed")
        continuation = self._verified_continuation(adapter, row, obj(job.get("payload")).get("continuation"), allow_control=True)
        if current["reconcile_only"] and not continuation:
            raise AdapterError("canonical_reconciliation_required")
        descriptor = adapter.driver.reserve(row, job, claim)
        public_descriptor(descriptor)
        return descriptor

    def begin_operation(self, job, claim, operation):
        adapter = self._adapter(job)
        if adapter.driver is None:
            raise AdapterError("canonical_execution_not_connected")
        # Credential issue is only possible after the native intent commits.
        return adapter.driver.begin_operation(job, claim, operation)

    def complete(self, job, receipt, claim):
        adapter = self._adapter(job)
        if adapter.driver is None:
            raise AdapterError("canonical_execution_not_connected")
        result = adapter.driver.complete(job, receipt, claim)
        return self._verified_result(adapter, job, result)

    def _verified_result(self, adapter, job, result):
        # Native read-back, never a node's success flag, decides publication.
        row = adapter.read(job["source_id"])
        if row is None:
            raise AdapterError("canonical_receipt_missing")
        state = str(row.get(adapter.spec.state) or "")
        proof = {"source_key": job["source_key"], "source_id": job["source_id"], "status": state,
                 "publish_id": str(row.get("publish_id") or row.get("post_id") or row.get("x_post_id") or row.get("graph_post_id") or row.get("video_id") or "")}
        confirmed = job["stage"] == "publish" and state in {"published", "synced", "completed"} and bool(proof["publish_id"]) and not row.get("unknown_outcome")
        published_at = row.get("published_at_utc") or row.get("published_at") or row.get("completed_at_utc") or row.get("finished_at")
        proof["published_at"] = stamp(published_at) if confirmed and published_at else ""
        youtube_publish = adapter.spec.channel == "youtube" and adapter.spec.family == "publish"
        if youtube_publish:
            proof["video_published"] = row.get("video_state") == "published" and bool(row.get("video_id"))
            proof["video_published_at"] = stamp(row["video_published_at_utc"]) if proof["video_published"] and row.get("video_published_at_utc") else ""
            confirmed = confirmed and row.get("comment_status") in {"published", "skipped"}
        completed = confirmed or (state == "scheduled" and bool(proof["publish_id"])) or (job["stage"] == "prepare" and (state in {"ready", "completed", "review", "enqueue_pending"} or (row.get("prepare_state") == "ready")))
        if youtube_publish and state == "scheduled":
            completed = bool(proof["publish_id"]) and row.get("schedule_status") == "armed" and not row.get("unknown_outcome")
        failure = state in {"failed", "failed_without_retry", "generation_failed", "cancelled", "canceled", "skipped", "missed", "no_candidate"} and not row.get("unknown_outcome")
        if youtube_publish and youtube_cancelled(row):
            proof["schedule_cancelled"] = True
            confirmed, completed, failure = False, True, False
        metadata_only = state in {"no_candidate", "skipped", "duplicate", "cancelled", "canceled", "abandoned"} and not (
            proof["publish_id"] or row.get("unknown_outcome") or row.get("publish_attempt_count") or row.get("video_attempt_count"))
        if metadata_only:
            proof["metadata_only"] = True
            confirmed, completed, failure = False, True, False
        next_offer = adapter.project(row)
        verified = {"canonical_status": state, "canonical_receipt": proof, "published": confirmed,
                "dispatch_completed": completed, "definitive_failure": failure, "metadata_only": metadata_only, "result": result, "next_offer": next_offer}
        continuation = self._verified_continuation(adapter, row, obj(result).get("continuation"))
        if continuation:
            verified["continuation"] = continuation
        return verified

    @staticmethod
    def _verified_continuation(adapter, row, requested, *, allow_control=False):
        if not requested:
            return None
        control = allow_control and youtube_control_due(row)
        expected_time = row.get("schedule_confirmed_publish_at") if control else row.get("publish_at")
        if (adapter.spec.channel != "youtube" or adapter.spec.family != "publish"
                or not isinstance(requested, Mapping)
                or (not control and (row.get(adapter.spec.state) != "scheduled" or row.get("schedule_status") != "armed"))
                or row.get("unknown_outcome") or not row.get("video_id")
                or requested.get("video_id") != row["video_id"] or not expected_time
                or stamp(requested.get("scheduled_at") or "") != stamp(expected_time)):
            raise AdapterError("canonical_continuation_unconfirmed")
        return {"video_id": row["video_id"], "scheduled_at": stamp(expected_time)}

    def reconcile(self, job):
        adapter = self._adapter(job)
        if adapter.driver is None:
            raise AdapterError("canonical_execution_not_connected")
        result = adapter.driver.reconcile(job)
        return self._verified_result(adapter, job, result)

    def checkpoint(self, job, claim, phase, receipt):
        driver = self._adapter(job).driver
        if driver is None or not hasattr(driver, "checkpoint"):
            raise AdapterError("canonical_checkpoint_not_connected")
        return driver.checkpoint(job, claim, phase, receipt)

    def fail(self, job, claim, receipt=None):
        driver = self._adapter(job).driver
        if driver is None or not hasattr(driver, "fail"):
            return {"canonical_no_write": False}
        return driver.fail(job, claim, receipt or claim)

    def pause(self, job, cutoff):
        driver = self._adapter(job).driver
        if driver is None or not hasattr(driver, "pause"):
            raise AdapterError("canonical_hold_not_connected")
        return driver.pause(job, stamp(cutoff))

    def apply_cutover(self, channel, cutoff, grandfathered_source_keys=()):
        """Return an immutable selection; root stores the dispatch admission hold.

        Canonical history is intentionally unchanged, including reservations,
        counters and media identity. An active item's exemption follows its
        source_key across stages, never its entire batch.
        """
        cutoff = stamp(cutoff)
        grandfathered = set(grandfathered_source_keys)
        paused, inflight, reconcile = [], [], []
        for adapter in self.adapters.values():
            if adapter.spec.channel != channel:
                continue
            for item in adapter.scan(5000):
                if item.get("observation_only") and not item.get("pause_eligible"):
                    continue
                key = item["source_key"]
                if item["reconcile_only"]:
                    reconcile.append(key)
                elif item["legacy_inflight"] or key in grandfathered:
                    inflight.append(key)
                elif (item.get("cutover_due_at") or item["scheduled_at"]) and (item.get("cutover_due_at") or item["scheduled_at"]) <= cutoff:
                    paused.append(key)
        return {"channel": channel, "cutoff": cutoff, "paused_source_keys": paused,
                "grandfathered_source_keys": sorted(set(inflight) | grandfathered),
                "reconcile_source_keys": reconcile, "canonical_mutations": 0}

    def export_cursors(self):
        return {":".join(key): adapter.cursor_state for key, adapter in self.adapters.items()}

    def collection_status(self):
        return {":".join(key): {"recent_complete": bool(adapter.cursor_state.get("recent_complete")),
                                "history_complete": bool(adapter.cursor_state.get("history_complete"))}
                for key, adapter in self.adapters.items()}

    def restore_cursors(self, values):
        for key, adapter in self.adapters.items():
            state = values.get(":".join(key))
            if isinstance(state, dict):
                adapter.cursor_state = {"active": state.get("active"), "history": state.get("history"),
                                        "recent": state.get("recent"), "recent_complete": bool(state.get("recent_complete")),
                                        "history_complete": bool(state.get("history_complete")),
                                        "observed_active": list(state.get("observed_active", []))}


class RemoteCanonicalDriver:
    """Small control-plane RPC to an owning local sidecar (never media I/O)."""
    def __init__(self, port, prefix, token, timeout=90, *, control_only=False):
        if not isinstance(port, int) or not 1024 <= port <= 65535 or not prefix.startswith("/internal/pipeline-dispatch/"):
            raise AdapterError("canonical_driver_configuration_invalid")
        self.port, self.prefix, self._token, self.timeout = port, prefix.rstrip("/"), token, timeout
        self.control_only = bool(control_only)

    def call(self, action, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request("POST", self.prefix + "/" + action, json.dumps(payload).encode(),
                               {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise AdapterError("canonical_response_too_large")
            result = json.loads(body)
            if response.status != 200:
                raise AdapterError(str(result.get("code") or result.get("error") or "canonical_bridge_failed"))
            if not isinstance(result, dict):
                raise AdapterError("canonical_response_invalid")
            return result
        except (OSError, ValueError, http.client.HTTPException):
            raise AdapterError("canonical_bridge_unavailable") from None
        finally:
            connection.close()

    def reserve(self, row, job, claim):
        if self.control_only:
            raise AdapterError("canonical_execution_not_connected")
        return self.call("reserve", {"job": job, "claim": claim})

    def begin_operation(self, job, claim, operation):
        return self.call("begin-operation", {"job": job, "claim": claim, "operation": operation})

    def complete(self, job, receipt, claim):
        return self.call("complete", {"job": job, "claim": claim, "receipt": receipt})

    def reconcile(self, job):
        return self.call("reconcile", {"job": job})

    def checkpoint(self, job, claim, phase, receipt):
        return self.call("checkpoint", {"job": job, "claim": claim, "phase": phase, "receipt": receipt})

    def fail(self, job, claim, receipt):
        return self.call("fail", {"job": job, "claim": claim, "receipt": receipt})

    def pause(self, job, cutoff):
        return self.call("pause", {"job": job, "cutoff": cutoff})

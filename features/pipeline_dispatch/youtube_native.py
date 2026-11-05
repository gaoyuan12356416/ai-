"""Canonical YouTube hooks; media generation and platform writes run on nodes.

The existing approved-cover ledger remains authoritative. Dispatch ownership
prevents legacy workers reclaiming a live or uncertain node operation.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import AdapterError, obj, stamp, youtube_existing_schedule
from .youtube_cover import image_bytes, inline_image
from features.drama_synthesis.core import utc_now
from features.youtube_auto_publish.service import encode, now
from features.youtube_auto_publish.scheduling import is_due
from features.youtube_auto_publish.engine import approved_cover_bytes, ReviewedYouTubeHTTPClient, reviewed_scope_eligible


def expiry():
    return (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(timespec="seconds").replace("+00:00", "Z")


def claim_hash(claim):
    token = str(claim.get("claim_token") or "")
    if len(token) < 16:
        raise AdapterError("dispatch_claim_invalid")
    return hashlib.sha256(token.encode()).hexdigest()


class YouTubeNativeBridge:
    def __init__(self, workflow, *, client=None):
        self.workflow, self.store = workflow, workflow.engine_store
        self.client = client or ReviewedYouTubeHTTPClient()
        self._source_locks = weakref.WeakValueDictionary()
        self._source_locks_guard = threading.Lock()
        # Reuse an already verified, short-lived access token only for reads in
        # this exact claim. Every write still reloads current authorization.
        self._read_tokens = {}
        self._read_tokens_lock = threading.Lock()
        self._video_readback_after = {}
        with workflow.db(True) as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_youtube_claim(
              source_key TEXT NOT NULL,stage TEXT NOT NULL,generation INTEGER NOT NULL,
              claim_hash TEXT NOT NULL,task_json TEXT NOT NULL,descriptor_json TEXT NOT NULL,
              receipt_json TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_key,stage,generation));
            CREATE TABLE IF NOT EXISTS pipeline_youtube_operation(
              operation_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,generation INTEGER NOT NULL,
              operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pipeline_youtube_checkpoint(
              source_key TEXT NOT NULL,generation INTEGER NOT NULL,phase TEXT NOT NULL,
              digest TEXT NOT NULL,PRIMARY KEY(source_key,generation,phase,digest));
            """)

    def _key(self, job):
        if job.get("channel") != "youtube" or job.get("family") not in {"preparation", "publish"}:
            raise AdapterError("youtube_dispatch_source_invalid")
        return str(job["source_key"]), str(job["stage"]), int(job["generation"])

    def _source_lock(self, job):
        # Slow reference preparation must not block another source's upload
        # receipt. Native generations for one source still share one fence;
        # channel-wide exclusion remains in the coordinator's resource ledger.
        if job.get("channel") != "youtube" or job.get("family") not in {"preparation", "publish"}:
            raise AdapterError("youtube_dispatch_source_invalid")
        source_key = str(job["source_key"])
        with self._source_locks_guard:
            lock = self._source_locks.get(source_key)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[source_key] = lock
            return lock

    @staticmethod
    def _continuation_descriptor(task, continuation, control):
        descriptor = {"kind": "youtube_publish", "mode": "continuation", "continuation": dict(continuation), "account_resource": "youtube:task:" + str(task["id"]), "request": {"video_id": task["video_id"], "channel_id": task["channel_id"], "comment_text": task["comment_text"], "comment_id": task["comment_id"], "publish_at": task["publish_at"]}}
        if continuation.get("kind") == "comment":
            descriptor["request"].update(comment_only=True, publish_at="")
        if control:
            descriptor["request"]["schedule_control"] = {"action": task["schedule_command"], "version": task["schedule_version"], "operation_id": task["schedule_operation_id"], "publish_at": task["publish_at"]}
        return descriptor

    def _stored(self, job, claim):
        with self.workflow.db() as conn:
            row = conn.execute("SELECT * FROM pipeline_youtube_claim WHERE source_key=? AND stage=? AND generation=?", self._key(job)).fetchone()
        if row is None or not secrets.compare_digest(row["claim_hash"], claim_hash(claim)):
            raise AdapterError("youtube_dispatch_claim_mismatch")
        return dict(row), obj(row["task_json"]), obj(row["descriptor_json"])

    def _owned(self, job, saved):
        task = self.store.youtube_task(int(job["source_id"]))
        if not task or task.get("pipeline_dispatch_key") != job["source_key"] or int(task["lease_generation"]) != int(saved["lease_generation"]):
            raise AdapterError("youtube_dispatch_native_claim_changed")
        owner = saved["lease_owner"]
        if task["lease_owner"] and task["lease_owner"] != owner:
            raise AdapterError("youtube_dispatch_native_claim_changed")
        # Native success helpers release their leases between stages. The
        # dedicated dispatch key keeps those rows invisible to legacy claims.
        with self.workflow.db(True) as conn:
            conn.execute("UPDATE drama_youtube_publish SET lease_owner=?,lease_expires_at_utc=? WHERE id=? AND pipeline_dispatch_key=? AND lease_generation=? AND lease_owner IN ('',?)", (owner, expiry(), task["id"], job["source_key"], saved["lease_generation"], owner))
        task = self.store.youtube_task(task["id"])
        return task, {"worker_id": owner, "lease_generation": int(saved["lease_generation"])}

    def reserve(self, job, claim):
        key = self._key(job)
        with self._source_lock(job):
            with self.workflow.db() as conn:
                existing = conn.execute("SELECT 1 FROM pipeline_youtube_claim WHERE source_key=? AND stage=? AND generation=?", key).fetchone()
            if existing:
                return self._stored(job, claim)[2]
            # Inherit the original workers' admission switches. A claimed
            # item may finish; turning a switch off prevents new claims only.
            if os.environ.get("YOUTUBE_LIVE_ENABLED", "0") != "1":
                raise AdapterError("youtube_publish_disabled")
            if job["family"] == "preparation" and os.environ.get("YOUTUBE_AUTO_ENABLED", "0") != "1":
                raise AdapterError("youtube_auto_disabled")
            with self.workflow.db() as conn:
                if conn.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone():
                    raise AdapterError("canonical_backlog_paused")
            if job["family"] == "preparation":
                with self.workflow.db(True) as conn:
                    row, task = self.workflow._row(conn, job["source_id"])
                    previous = conn.execute("SELECT * FROM pipeline_youtube_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1", (job["source_key"],)).fetchone()
                    if previous:
                        saved = obj(previous["task_json"])
                        descriptor = obj(previous["descriptor_json"])
                        retry_id = str(task.get("generation_retry_id") or "")
                        new_intent = (row["state"] == "queued_generation" and task.get("current_version") == row["version"]
                                      and (row["version"] > saved.get("version", 0)
                                           or (row["version"] == saved.get("version") and retry_id
                                               and retry_id != saved.get("retry_id", ""))))
                        if int(previous["generation"]) >= int(job["generation"]):
                            raise AdapterError("youtube_preparation_version_changed")
                        if not new_intent:
                            if (previous["receipt_json"] or row["state"] != "generating"
                                    or row["lease_token"] != saved.get("lease_owner")
                                    or row["version"] != saved.get("version") or task.get("current_version") != saved.get("version")
                                    or retry_id != saved.get("retry_id", "")
                                    or obj(task.get("reference_cover")).get("sha256") != saved.get("reference_sha256")):
                                raise AdapterError("youtube_preparation_version_changed")
                            owner = "pipeline:" + secrets.token_hex(16)
                            conn.execute("UPDATE youtube_auto_preparation SET lease_token=?,lease_until=? WHERE id=?", (owner, time.time() + 120, task["id"]))
                            saved["lease_owner"] = owner
                            # Old nodes remain recovery-only. New nodes may start
                            # a never-started invocation only after winning the
                            # shared immutable generation-intent CAS; an existing
                            # intent never authorizes another paid model call.
                            descriptor["recovery_only"] = True
                            descriptor["resume_once"] = True
                            conn.execute("INSERT INTO pipeline_youtube_claim(source_key,stage,generation,claim_hash,task_json,descriptor_json) VALUES(?,?,?,?,?,?)", (*key, claim_hash(claim), encode(saved), encode(descriptor)))
                            return descriptor
                    if row["state"] != "queued_generation" or row["lease_until"] > time.time() or row["lease_token"].startswith("pipeline:"):
                        raise AdapterError("youtube_preparation_not_claimable")
                    if is_due(task.get("publish_at")):
                        raise AdapterError("youtube_schedule_missed")
                    if self.workflow._ledger(task, conn):
                        raise AdapterError("youtube_preparation_already_enqueued")
                    owner = "pipeline:" + secrets.token_hex(16)
                    task["status"] = "generating"
                    conn.execute("UPDATE youtube_auto_preparation SET state='generating',body=?,lease_token=?,lease_until=? WHERE id=?", (encode(task), owner, time.time() + 120, task["id"]))
                try:
                    reference, material = self.workflow._prepare_reference(task)
                    task.update(reference_cover=reference, material=material)
                    with self.workflow.db(True) as conn:
                        row, current = self.workflow._row(conn, task["id"])
                        if row["lease_token"] != owner or row["version"] != task["current_version"]:
                            raise AdapterError("youtube_preparation_version_changed")
                        self.workflow._save(conn, task)
                        asset = self.workflow._asset(conn, reference["asset_id"], task["creator"])
                    descriptor = {"kind": "youtube_prepare", "request": {"action": "generate", "task": task, "reference_image": inline_image(Path(asset["path"]).read_bytes())}}
                    saved = {"id": task["id"], "lease_owner": owner, "version": task["current_version"],
                             "retry_id": str(task.get("generation_retry_id") or ""), "reference_sha256": reference["sha256"]}
                except Exception:
                    with self.workflow.db(True) as conn:
                        row, current = self.workflow._row(conn, task["id"])
                        if row["lease_token"] == owner:
                            current["error"] = {"code": "reference_preflight_failed", "message": "原图校验失败，请检查后重试"}
                            self.workflow._save(conn, current, "generation_failed")
                            conn.execute("UPDATE youtube_auto_preparation SET lease_token='',lease_until=0 WHERE id=?", (task["id"],))
                    raise
            else:
                with self.workflow.db(True) as conn:
                    row = conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (job["source_id"],)).fetchone()
                    if row and row["workflow"] == "reviewed_thumbnail" and os.environ.get("YOUTUBE_AUTO_ENABLED", "0") != "1":
                        raise AdapterError("youtube_auto_disabled")
                    if row is None:
                        raise AdapterError("canonical_task_missing")
                    task = dict(row)
                    continuation = obj(job.get("payload")).get("continuation")
                    adopting = False
                    if continuation:
                        from .youtube_readback import comment_continuation
                        comment_only = continuation.get("kind") == "comment"
                        if comment_only and (comment_continuation(task) != continuation or task["pipeline_dispatch_key"] != job["source_key"]):
                            raise AdapterError("youtube_comment_continuation_not_claimable")
                        control = task["status"] == "schedule_pending" and task["schedule_status"] == "control_pending" and task["schedule_command_status"] == "pending"
                        if obj(job.get("payload")).get("adopt_existing_schedule") is True and not task["pipeline_dispatch_key"]:
                            verified = youtube_existing_schedule(task, allow_control=True)
                            history = conn.execute("SELECT 1 FROM pipeline_youtube_claim WHERE source_key=? UNION ALL SELECT 1 FROM pipeline_youtube_operation WHERE source_key=? UNION ALL SELECT 1 FROM pipeline_youtube_checkpoint WHERE source_key=? LIMIT 1", (job["source_key"],)*3).fetchone()
                            if not verified or history or verified != continuation:
                                raise AdapterError("youtube_existing_schedule_not_claimable")
                            adopting = True
                        if not comment_only and (task["workflow"] != "reviewed_thumbnail" or task["unknown_outcome"]
                                or (task["pipeline_dispatch_key"] != job["source_key"] and not adopting)
                                or continuation.get("video_id") != task["video_id"] or not task["video_id"]
                                or stamp(continuation.get("scheduled_at")) != stamp(task["schedule_confirmed_publish_at"])
                                or (not control and task["schedule_confirmed_publish_at"] != task["publish_at"])
                                or (not control and task["status"] not in {"scheduled", "published"})
                                or (not control and task["schedule_status"] not in {"armed", "published"})
                                or (task["comment_attempt_count"] and not task["comment_id"])
                                or (task["lease_owner"] and not task["lease_owner"].startswith("pipeline:" + str(job["id"]) + ":"))):
                            raise AdapterError("youtube_continuation_not_claimable")
                    else:
                        if task["workflow"] not in {"reviewed_thumbnail", "legacy"} or task["privacy_status"] != "public" or task["status"] != "queued" or task["unknown_outcome"] or task["video_attempt_count"] or task["video_id"] or task["resumable_session_uri"] or task["lease_owner"] or task["pipeline_dispatch_key"]:
                            raise AdapterError("youtube_publish_not_claimable")
                        if task["reviewed_phase"] == "attribution_pending" or (task["publish_at"] and is_due(task["publish_at"])):
                            raise AdapterError("youtube_publish_preflight_pending")
                        if task["workflow"] == "reviewed_thumbnail":
                            content, mime = approved_cover_bytes(task, self.workflow.root)
                    owner = "pipeline:" + str(job["id"]) + ":" + str(job["generation"])
                    task.update(lease_owner=owner, lease_generation=int(task["lease_generation"]) + 1)
                    conn.execute("UPDATE drama_youtube_publish SET status=?,lease_owner=?,lease_generation=?,lease_expires_at_utc=?,pipeline_dispatch_key=? WHERE id=?", (task["status"] if continuation else "validating", owner, task["lease_generation"], expiry(), job["source_key"], task["id"]))
                    if adopting:
                        # The old video and this first dispatch claim acquire
                        # ownership in one SQLite transaction, with no upload.
                        saved = {"id": task["id"], "lease_owner": owner, "lease_generation": task["lease_generation"]}
                        descriptor = self._continuation_descriptor(task, continuation, control)
                        descriptor["adopted_existing_schedule"] = True
                        conn.execute("INSERT INTO pipeline_youtube_claim(source_key,stage,generation,claim_hash,task_json,descriptor_json) VALUES(?,?,?,?,?,?)", (*key, claim_hash(claim), encode(saved), encode(descriptor)))
                        return descriptor
                saved = {"id": task["id"], "lease_owner": owner, "lease_generation": task["lease_generation"]}
                if continuation:
                    descriptor = self._continuation_descriptor(task, continuation, control)
                else:
                    descriptor = {"kind": "youtube_publish", "mode": "legacy" if task["workflow"] == "legacy" else "reviewed", "account_resource": "youtube:task:" + str(task["id"]), "request": {"title": task["title"], "description": task["description_rendered"], "channel_id": task["channel_id"], "comment_text": task["comment_text"], "publish_at": task["publish_at"]}, "artifact": {"url": task["source_url"], "identity_mode": "source_to_fingerprint", "mime": "video/mp4"}}
                    if task["workflow"] == "reviewed_thumbnail": descriptor["request"]["approved_cover"] = inline_image(content, mime)
            with self.workflow.db(True) as conn:
                conn.execute("INSERT INTO pipeline_youtube_claim(source_key,stage,generation,claim_hash,task_json,descriptor_json) VALUES(?,?,?,?,?,?)", (*key, claim_hash(claim), encode(saved), encode(descriptor)))
            return descriptor

    def begin_operation(self, job, claim, operation):
        with self._source_lock(job):
            _, saved, descriptor = self._stored(job, claim)
            if job["family"] != "publish" or operation.get("account_resource") != descriptor["account_resource"]:
                raise AdapterError("youtube_operation_resource_invalid")
            task, native = self._owned(job, saved)
            name, details = str(operation.get("operation") or ""), operation.get("details") or {}
            allowed = {"youtube_begin_resumable", "youtube_upload", "youtube_thumbnail", "youtube_video_state", "youtube_visibility", "youtube_comment"}
            if descriptor.get("mode") == "legacy":
                allowed = {"youtube_begin_resumable", "youtube_upload", "youtube_video_state", "youtube_comment"}
            if descriptor.get("mode") == "continuation":
                allowed = {"youtube_video_state", "youtube_comment"}
                if descriptor["request"].get("schedule_control"):
                    allowed.add("youtube_schedule_control")
            if name not in allowed or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", str(operation.get("operation_id") or "")):
                raise AdapterError("youtube_operation_invalid")
            # Credential preparation is not a platform write. Do it before
            # recording upload/comment intent so a rejected credential cannot
            # strand a never-started video as an ambiguous upload.
            cache_key=(job['source_key'],job['generation'],claim_hash(claim),task['channel_id'])
            with self._read_tokens_lock:
                cutoff=time.monotonic()
                self._read_tokens={k:v for k,v in self._read_tokens.items() if v[0]>cutoff}
                cached=self._read_tokens.get(cache_key) if name=='youtube_video_state' else None
            token=cached[1] if cached else None
            for attempt in range(0 if cached else 2):
                try:
                    credential = self.workflow.channel_directory.repository.credential(app_id=task["app_id"], channel_local_id=task["channel_local_id"], account_id=task["youtube_account_id"], expected_channel_id=task["channel_id"])
                    if (task["workflow"] == "reviewed_thumbnail" and not reviewed_scope_eligible(credential.scopes)) or (task["workflow"] == "legacy" and not credential.capabilities["upload_eligible"]) or (task["comment_text"] and not credential.capabilities["comment_eligible"]):
                        raise AdapterError("youtube_scope_missing")
                    token = self.client.refresh_access_token(credential)
                    self.client.verify_channel_identity(token, task["channel_id"])
                    break
                except Exception as exc:
                    code = getattr(exc, "code", "")
                    lookup_unavailable = code in {"material_query_failed", "material_query_busy"}
                    if attempt and lookup_unavailable:
                        raise AdapterError("youtube_credential_lookup_unavailable") from None
                    if attempt or (not lookup_unavailable and code not in {"youtube_token_refresh_unavailable", "youtube_channel_identity_unavailable"}):
                        raise
                    time.sleep(1)
            if not cached:
                with self._read_tokens_lock:
                    self._read_tokens[cache_key]=(time.monotonic()+90,token)
            # Slow credential reads may outlive native ownership. Recheck it
            # before granting any platform-write intent.
            task, native = self._owned(job, saved)
            if name == "youtube_begin_resumable":
                privacy = "public" if task["workflow"] == "legacy" else "private"
                if details != {"title": task["title"], "description": task["description_rendered"], "size": task["source_size"], "privacy_status": privacy} or not task["source_sha256"]:
                    raise AdapterError("youtube_source_identity_changed")
                if task["workflow"] == "legacy":
                    with self.workflow.db(True) as conn:
                        changed = conn.execute("UPDATE drama_youtube_publish SET video_attempt_count=1,status='uploading',video_state='uploading',updated_at_utc=? WHERE id=? AND lease_owner=? AND lease_generation=? AND workflow='legacy' AND video_attempt_count=0 AND video_id='' AND resumable_session_uri=''", (utc_now(), task["id"], native["worker_id"], native["lease_generation"])).rowcount
                        if changed != 1: raise AdapterError("youtube_upload_already_attempted")
                        conn.execute("INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)", (task["id"], "video", "intent", "pipeline_legacy_public_upload", utc_now()))
                else:
                    self.store.mark_reviewed_upload_intent(task["id"], source_size=task["source_size"], **native)
            elif name == "youtube_upload":
                if details.get("session_uri_sha256") != hashlib.sha256(task["resumable_session_uri"].encode()).hexdigest() or details.get("source_sha256") != task["source_sha256"] or details.get("offset") != task["next_byte"]:
                    raise AdapterError("youtube_upload_identity_changed")
            else:
                if not task["video_id"] or details.get("video_id") != task["video_id"]:
                    raise AdapterError("youtube_video_identity_changed")
                if name == "youtube_thumbnail":
                    if details.get("sha256") != task["approved_cover_sha256"]:
                        raise AdapterError("youtube_cover_identity_changed")
                    approved_cover_bytes(task, self.workflow.root)
                    self.store.advance_reviewed_youtube(task["id"], "thumbnail", thumbnail_status="running", **native)
                elif name == "youtube_visibility":
                    if task["thumbnail_status"] != "succeeded" or task["processing_status"] != "succeeded" or details.get("publish_at") != task["publish_at"]:
                        raise AdapterError("youtube_publish_gate")
                    if task["publish_at"]:
                        from features.youtube_auto_publish.engine import ReviewedYouTubePublishEngine
                        if ReviewedYouTubePublishEngine._schedule_close(task["publish_at"]):
                            raise AdapterError("youtube_schedule_too_close")
                        self.store.advance_reviewed_schedule(task["id"], "arming", command_status="running", write_action="reschedule", **native)
                    else:
                        self.store.advance_reviewed_youtube(task["id"], "public", public_status="running", **native)
                elif name == "youtube_comment":
                    if task["comment_attempt_count"] or task["comment_id"] or task["video_state"] != "published":
                        raise AdapterError("youtube_comment_already_attempted")
                    if details.get("channel_id") != task["channel_id"] or details.get("text_sha256") != hashlib.sha256(task["comment_text"].encode()).hexdigest():
                        raise AdapterError("youtube_comment_identity_changed")
                    self.store.mark_comment_attempt(task["id"], **native)
                elif name == "youtube_schedule_control":
                    expected = descriptor["request"]["schedule_control"]
                    if (details.get("control") != expected or task["schedule_status"] != "control_pending"
                            or task["schedule_version"] != expected["version"] or task["schedule_operation_id"] != expected["operation_id"]
                            or expected["action"] not in {"reschedule", "immediate", "cancel"}):
                        raise AdapterError("youtube_schedule_control_changed")
                    from features.youtube_auto_publish.engine import ReviewedYouTubePublishEngine
                    if ((expected["action"] == "reschedule" and ReviewedYouTubePublishEngine._schedule_close(task["publish_at"]))
                            or (task["schedule_confirmed_publish_at"] and ReviewedYouTubePublishEngine._schedule_close(task["schedule_confirmed_publish_at"]))):
                        raise AdapterError("youtube_schedule_too_close")
                    self.store.advance_reviewed_schedule(task["id"], "control_running", command_status="running", write_action=expected["action"], **native)
                elif details.get("channel_id") != task["channel_id"]:
                    raise AdapterError("youtube_channel_identity_changed")
            with self.workflow.db(True) as conn:
                conn.execute("INSERT INTO pipeline_youtube_operation VALUES(?,?,?,?,?,?)", (operation["operation_id"], job["source_key"], job["generation"], name, operation["request_sha256"], now()))
            return {"credentials": {"access_token": token}}

    def checkpoint(self, job, claim, phase, receipt):
        with self._source_lock(job):
            _, saved, descriptor = self._stored(job, claim)
            digest = hashlib.sha256(encode(receipt).encode()).hexdigest()
            with self.workflow.db() as conn:
                if conn.execute("SELECT 1 FROM pipeline_youtube_checkpoint WHERE source_key=? AND generation=? AND phase=? AND digest=?", (job["source_key"], job["generation"], phase, digest)).fetchone():
                    return {"accepted": True}
            task, native = self._owned(job, saved)
            if descriptor.get("mode") == "continuation" and phase not in {"youtube_schedule_readback", "youtube_comment"}:
                raise AdapterError("youtube_continuation_phase_invalid")
            if descriptor.get("mode") == "legacy" and phase not in {"youtube_source", "youtube_session", "youtube_upload", "youtube_legacy_status", "youtube_comment"}:
                raise AdapterError("youtube_legacy_phase_invalid")
            if phase == "youtube_source":
                if receipt.get("url") != task["source_url"] or not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("sha256") or "")) or int(receipt.get("size", 0)) <= 0 or int(receipt.get("duration_ms", 0)) <= 0 or task["video_attempt_count"]:
                    raise AdapterError("youtube_source_receipt_invalid")
                if task["source_sha256"] and (task["source_sha256"] != receipt["sha256"] or task["source_size"] != receipt["size"]):
                    raise AdapterError("youtube_source_identity_changed")
                self.store.advance_youtube(task["id"], "uploading", source_sha256=receipt["sha256"], source_duration_ms=receipt["duration_ms"], **native)
                with self.workflow.db(True) as conn:
                    conn.execute("UPDATE drama_youtube_publish SET source_size=? WHERE id=?", (receipt["size"], task["id"]))
            elif phase == "youtube_session":
                from .executors import NodeExecutors
                NodeExecutors._validate_youtube_session(receipt.get("session_uri"))
                if task["resumable_session_uri"]:
                    if task["resumable_session_uri"] != receipt["session_uri"]:
                        raise AdapterError("youtube_session_identity_changed")
                else:
                    if task["workflow"] == "legacy":
                        if receipt.get("size") != task["source_size"] or task["video_attempt_count"] != 1:
                            raise AdapterError("youtube_session_identity_changed")
                        with self.workflow.db(True) as conn:
                            changed = conn.execute("UPDATE drama_youtube_publish SET resumable_session_uri=?,updated_at_utc=? WHERE id=? AND lease_owner=? AND lease_generation=? AND resumable_session_uri='' AND video_attempt_count=1", (receipt["session_uri"], utc_now(), task["id"], native["worker_id"], native["lease_generation"])).rowcount
                            if changed != 1: raise AdapterError("youtube_session_identity_changed")
                    else:
                        self.store.set_upload_session(task["id"], receipt["session_uri"], receipt["size"], **native)
            elif phase == "youtube_upload":
                if receipt.get("state") != "submitted" or not receipt.get("video_id"):
                    raise AdapterError("youtube_upload_requires_reconciliation")
                if task["video_id"] and task["video_id"] != receipt["video_id"]:
                    raise AdapterError("youtube_video_identity_changed")
                if not task["video_id"]:
                    self.store.video_submitted(task["id"], receipt["video_id"], **native)
            else:
                if receipt.get("video_id") != task["video_id"] or not task["video_id"]:
                    raise AdapterError("youtube_video_identity_changed")
                if phase == "youtube_legacy_status":
                    if descriptor.get("mode") != "legacy": raise AdapterError("youtube_checkpoint_phase_invalid")
                    if receipt.get("state") == "published" and receipt.get("visibility") == "public":
                        if task["video_state"] != "published": self.store.video_published(task["id"], task["video_id"], **native)
                    elif receipt.get("state") == "processing":
                        self.store.video_processing(task["id"], **native)
                    else:
                        raise AdapterError("youtube_publication_not_confirmed")
                elif phase == "youtube_thumbnail":
                    if receipt.get("sha256") != task["approved_cover_sha256"] or receipt.get("status") != "set":
                        raise AdapterError("youtube_thumbnail_receipt_invalid")
                    self.store.advance_reviewed_youtube(task["id"], "processing", thumbnail_status="succeeded", **native)
                elif phase == "youtube_processing":
                    if receipt.get("visibility") not in {"private", "public"} or receipt.get("state") not in {"succeeded", "processing", "failed"}:
                        raise AdapterError("youtube_processing_receipt_invalid")
                    self.store.advance_reviewed_youtube(task["id"], "processing", processing_status="succeeded" if receipt["state"] == "succeeded" else "failed" if receipt["state"] == "failed" else "running", **native)
                elif phase == "youtube_visibility_observation":
                    with self.workflow.db(True) as conn:
                        conn.execute("INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)", (task["id"], "visibility_readback", "unconfirmed", encode({"video_id": task["video_id"], "visibility": str(receipt.get("visibility") or "")[:20], "publish_at": str(receipt.get("publish_at") or "")[:40]}), utc_now()))
                elif phase == "youtube_visibility":
                    confirmed = receipt.get("confirmed") or {}
                    if receipt.get("publish_at") != task["publish_at"] or confirmed.get("state") != "succeeded":
                        raise AdapterError("youtube_visibility_receipt_invalid")
                    if task["publish_at"]:
                        if confirmed.get("visibility") != "private" or confirmed.get("preserved_status", {}).get("publishAt") != task["publish_at"]:
                            raise AdapterError("youtube_schedule_unconfirmed")
                        self.store.advance_reviewed_schedule(task["id"], "armed", confirmed_publish_at=task["publish_at"], command_status="confirmed", release=True, **native)
                    else:
                        if confirmed.get("visibility") != "public":
                            raise AdapterError("youtube_public_unconfirmed")
                        self.store.advance_reviewed_youtube(task["id"], "comment" if task["comment_text"] else "complete", public_status="succeeded", **native)
                        if task["video_state"] != "published":
                            self.store.video_published(task["id"], task["video_id"], **native)
                elif phase == "youtube_comment":
                    if task["comment_id"]:
                        if task["comment_id"] != receipt.get("comment_id"):
                            raise AdapterError("youtube_comment_identity_changed")
                    else:
                        self.store.comment_published(task["id"], receipt.get("comment_id"), **native)
                elif phase == "youtube_schedule_readback":
                    confirmed = receipt.get("confirmed") or {}
                    if descriptor.get("mode") != "continuation" or confirmed.get("state") != "succeeded":
                        raise AdapterError("youtube_schedule_unconfirmed")
                    control = descriptor["request"].get("schedule_control")
                    if descriptor["request"].get("comment_only"):
                        if confirmed.get("visibility") != "public" or task["video_state"] != "published" or task["comment_attempt_count"] or task["comment_id"]:
                            raise AdapterError("youtube_comment_readback_unconfirmed")
                        with self.workflow.db(True) as conn:
                            conn.execute("UPDATE drama_youtube_publish SET comment_status='publishing',reviewed_phase='comment' WHERE id=? AND lease_owner=? AND lease_generation=?", (task["id"], native["worker_id"], native["lease_generation"]))
                    elif confirmed.get("visibility") == "public":
                        expected_time = task["schedule_confirmed_publish_at"] if control and control["action"] != "immediate" else task["publish_at"] if not control else ""
                        if expected_time and expected_time > utc_now():
                            raise AdapterError("youtube_reviewed_early_public")
                        command_status = "failed" if control and control["action"] == "cancel" else "confirmed"
                        self.store.advance_reviewed_schedule(task["id"], "published", command_status=command_status, **native)
                        self.store.advance_reviewed_youtube(task["id"], "comment" if task["comment_text"] else "complete", public_status="succeeded", **native)
                        if task["video_state"] != "published":
                            with self.workflow.db(True) as conn:
                                conn.execute("UPDATE drama_youtube_publish SET status='processing' WHERE id=? AND lease_owner=? AND lease_generation=? AND video_id=? AND status IN ('scheduled','schedule_pending')", (task["id"], native["worker_id"], native["lease_generation"], task["video_id"]))
                            self.store.video_published(task["id"], task["video_id"], **native)
                    elif confirmed.get("visibility") == "private":
                        remote = confirmed.get("preserved_status", {}).get("publishAt") or ""
                        if control and control["action"] == "cancel" and not remote:
                            self.store.advance_reviewed_schedule(task["id"], "cancelled", command_status="confirmed", confirmed_publish_at="", release=True, **native)
                        elif remote and remote == task["publish_at"]:
                            self.store.advance_reviewed_schedule(task["id"], "armed", command_status="confirmed", confirmed_publish_at=remote, release=True, **native)
                        elif control and receipt.get("command_rejected") is True and remote == task["schedule_confirmed_publish_at"] and remote:
                            self.store.advance_reviewed_schedule(task["id"], "armed", command_status="failed", publish_at=remote, confirmed_publish_at=remote, release=True, error_code="youtube_schedule_control_rejected", error_message="平台未接受此次预约修改，保留已核对的原预约", **native)
                        else:
                            raise AdapterError("youtube_schedule_unconfirmed")
                    else:
                        raise AdapterError("youtube_schedule_unconfirmed")
                else:
                    raise AdapterError("youtube_checkpoint_phase_invalid")
            with self.workflow.db(True) as conn:
                conn.execute("INSERT OR IGNORE INTO pipeline_youtube_checkpoint VALUES(?,?,?,?)", (job["source_key"], job["generation"], phase, digest))
            return {"accepted": True}

    def complete(self, job, receipt, claim):
        with self._source_lock(job):
            row, saved, descriptor = self._stored(job, claim)
            if row["receipt_json"]:
                if obj(row["receipt_json"]) != receipt:
                    raise AdapterError("youtube_completion_changed")
                return self.reconcile(job)
            if job["family"] == "preparation":
                if receipt.get("version") != saved["version"] or receipt.get("reference_sha256") != saved["reference_sha256"]:
                    raise AdapterError("youtube_preparation_version_changed")
                data = image_bytes(receipt.get("cover"))
                artifact = obj(receipt.get("artifact"))
                if (artifact.get("sha256") != hashlib.sha256(data).hexdigest() or artifact.get("size") != len(data)
                        or artifact.get("profile") != "youtube-cover-v1" or artifact.get("mime") != "image/jpeg"
                        or not str(artifact.get("url", "")).startswith("https://")):
                    raise AdapterError("youtube_cover_artifact_invalid")
                with self.workflow.db() as conn:
                    current_row, body = self.workflow._row(conn, saved["id"])
                if current_row["lease_token"] != saved["lease_owner"] or current_row["version"] != saved["version"]:
                    raise AdapterError("youtube_preparation_version_changed")
                asset = self.workflow._store_image(body["creator"], data, generated=True)
                with self.workflow.db(True) as conn:
                    current_row, body = self.workflow._row(conn, saved["id"])
                    if current_row["lease_token"] != saved["lease_owner"] or current_row["version"] != saved["version"]:
                        raise AdapterError("youtube_preparation_version_changed")
                    body["versions"][-1].update(asset_id=asset["id"], created_at=now())
                    body["error"] = {}
                    if is_due(body.get("publish_at")):
                        self.workflow._mark_schedule_missed(conn, body, "review")
                    else:
                        self.workflow._save(conn, body, "review")
                    conn.execute("UPDATE youtube_auto_preparation SET lease_token='',lease_until=0 WHERE id=?", (body["id"],))
                    conn.execute("INSERT OR IGNORE INTO youtube_auto_notification(task_id,version,state) VALUES(?,?,'pending')", (body["id"], saved["version"]))
                    conn.execute("UPDATE pipeline_youtube_claim SET receipt_json=? WHERE source_key=? AND stage=? AND generation=?", (encode(receipt), *self._key(job)))
            else:
                task = self.store.youtube_task(int(job["source_id"]))
                if receipt.get("video_id") != task["video_id"] or task["status"] not in {"published", "scheduled", "cancelled"} or task["unknown_outcome"]:
                    raise AdapterError("youtube_publication_not_confirmed")
                if task["status"] == "published" and task["comment_status"] not in {"published", "skipped"}:
                    raise AdapterError("youtube_comment_not_confirmed")
                with self.workflow.db(True) as conn:
                    conn.execute("UPDATE pipeline_youtube_claim SET receipt_json=? WHERE source_key=? AND stage=? AND generation=?", (encode(receipt), *self._key(job)))
                    # Armed schedules stay hidden from legacy workers. Their
                    # same dispatch job is parked, then reclaimed for GET/comment.
                    conn.execute("UPDATE drama_youtube_publish SET pipeline_dispatch_key=?,lease_owner='',lease_expires_at_utc='' WHERE id=? AND pipeline_dispatch_key=?", (job["source_key"] if task["status"] == "scheduled" else "", task["id"], job["source_key"]))
            return self.reconcile(job)

    def fail(self, job, claim, receipt):
        with self._source_lock(job):
            _, saved, _ = self._stored(job, claim)
            if job["family"] == "preparation":
                with self.workflow.db(True) as conn:
                    row, body = self.workflow._row(conn, saved["id"])
                    if row["lease_token"] == saved["lease_owner"]:
                        body["error"] = {"code": str(receipt.get("error_code") or "generation_failed"), "message": "封面生成失败，请检查后重试"}
                        self.workflow._save(conn, body, "generation_failed")
                        conn.execute("UPDATE youtube_auto_preparation SET lease_token='',lease_until=0 WHERE id=?", (body["id"],))
                return {"canonical_no_write": True}
            task = self.store.youtube_task(int(job["source_id"]))
            with self.workflow.db() as conn:
                confirmed_current = conn.execute("SELECT 1 FROM pipeline_youtube_checkpoint WHERE source_key=? AND generation=? AND phase IN ('youtube_visibility','youtube_schedule_readback') LIMIT 1", (job["source_key"], job["generation"])).fetchone()
            scheduled_safe = task["status"] == "scheduled" and task["schedule_status"] == "armed" and (confirmed_current or not receipt.get("unknown_outcome"))
            cancelled_safe = task["status"] == "cancelled" and task["schedule_status"] == "cancelled" and task["schedule_command_status"] == "confirmed"
            if (task["status"] == "published" and task["comment_status"] in {"published", "skipped"}) or scheduled_safe or cancelled_safe:
                return {"canonical_terminal": True, "dispatch_completed": True, **self.reconcile(job)}
            task, native = self._owned(job, saved)
            unknown = bool(task["video_attempt_count"] or task["video_id"] or task["resumable_session_uri"] or receipt.get("unknown_outcome"))
            self.store.fail_youtube(task["id"], phase="comment" if task["comment_attempt_count"] else "video", code=str(receipt.get("error_code") or "node_execution_failed"), message="节点操作已停止，已保留原视频与回执供核对", unknown=unknown, **native)
            if not unknown:
                with self.workflow.db(True) as conn:
                    conn.execute("UPDATE drama_youtube_publish SET pipeline_dispatch_key='' WHERE id=? AND pipeline_dispatch_key=?", (task["id"], job["source_key"]))
            return {"canonical_no_write": not unknown}

    def reconcile(self, job):
        if job["family"] == "preparation":
            with self.workflow.db() as conn:
                row, body = self.workflow._row(conn, job["source_id"])
                saved = conn.execute("SELECT receipt_json FROM pipeline_youtube_claim WHERE source_key=? AND stage='prepare' AND receipt_json<>'' ORDER BY generation DESC LIMIT 1", (job["source_key"],)).fetchone()
                receipt = obj(saved["receipt_json"]) if saved else {}
            result = {"status": row["state"]}
            if receipt.get("version") == row["version"] and row["state"] in {"review", "enqueue_pending", "uploading"}:
                result["artifact"] = obj(receipt.get("artifact"))
                result["preparation_completed"] = True
            return result
        with self._source_lock(job):
            task = self.store.youtube_task(int(job["source_id"]))
            # This path only GETs an existing video. A failed/absent read never
            # creates a replacement upload or repeats a visibility write.
            if job.get("status") in {"unknown", "completed"} and not task.get("lease_owner") and time.monotonic() >= self._video_readback_after.get(task["id"], 0):
                self._video_readback_after[task["id"]] = time.monotonic() + 60
                from .youtube_readback import reconcile_video
                task = reconcile_video(self, task)
        result = {key: task.get(key) for key in ("status", "video_id", "comment_id", "unknown_outcome", "schedule_status", "schedule_command_status")}
        from .youtube_readback import comment_continuation
        comment = comment_continuation(task)
        if comment:
            result.update(dispatch_completed=True, continuation=comment)
        if task["status"] == "scheduled" and task["schedule_status"] == "armed" and task["video_id"] and not task["unknown_outcome"]:
            result.update(dispatch_completed=True, continuation={"video_id": task["video_id"], "scheduled_at": task["schedule_confirmed_publish_at"]})
        if task["status"] == "cancelled" and task["schedule_status"] == "cancelled" and task["schedule_command_status"] == "confirmed" and task["video_id"] and not task["unknown_outcome"]:
            result["dispatch_completed"] = True
        return result

    def pause(self, job, cutoff):
        from .adapters import stamp
        if job.get("channel") != "youtube" or job.get("family") not in {"preparation", "publish"} or job.get("source_key") != "youtube:" + job["family"] + ":" + str(job.get("source_id")):
            raise AdapterError("youtube_dispatch_source_invalid")
        if not cutoff:
            raise AdapterError("canonical_hold_cutoff_required")
        cutoff = stamp(cutoff)
        held = {"held": True, "accepted": True, "paused": True, "source_key": job["source_key"], "cutoff": cutoff, "canonical_mutations": 0}
        with self._source_lock(job), self.workflow.db(True) as conn:
            existing = conn.execute("SELECT cutoff FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone()
            if existing:
                if existing["cutoff"] != cutoff:
                    raise AdapterError("canonical_hold_cutoff_conflict")
                return held
            if job["family"] == "preparation":
                row, body = self.workflow._row(conn, job["source_id"])
                if row["state"] not in {"queued_generation", "review", "enqueue_pending"} or row["lease_until"] > time.time() or row["lease_token"].startswith("pipeline:") or self.workflow._ledger(body, conn):
                    raise AdapterError("canonical_task_already_started")
                created, scheduled = row["created_at"], body.get("publish_at") or row["created_at"]
            else:
                row = conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (job["source_id"],)).fetchone()
                if row is None or row["status"] != "queued" or row["lease_owner"] or row["pipeline_dispatch_key"] or row["video_attempt_count"] or row["video_id"] or row["resumable_session_uri"] or row["unknown_outcome"]:
                    raise AdapterError("canonical_task_already_started")
                created, scheduled = row["created_at_utc"], row["publish_at"] or row["created_at_utc"]
            if not created or not scheduled or stamp(created) > cutoff or stamp(scheduled) > cutoff:
                raise AdapterError("canonical_task_outside_cutoff")
            conn.execute("INSERT INTO pipeline_canonical_hold VALUES(?,?,?)", (job["source_key"], cutoff, "cutover_overdue"))
            return held

    def dispatch(self, action, payload):
        job, claim = payload.get("job") or {}, payload.get("claim") or {}
        if action == "reserve": return self.reserve(job, claim)
        if action == "begin-operation": return self.begin_operation(job, claim, payload.get("operation") or {})
        if action == "checkpoint": return self.checkpoint(job, claim, payload.get("phase"), payload.get("receipt") or {})
        if action == "complete": return self.complete(job, payload.get("receipt") or {}, claim)
        if action == "fail": return self.fail(job, claim, payload.get("receipt") or {})
        if action == "reconcile": return self.reconcile(job)
        if action == "pause": return self.pause(job, payload.get("cutoff"))
        raise AdapterError("canonical_bridge_action_invalid")


_INITIALIZE_LOCK = threading.Lock()


def handle_youtube_dispatch(workflow, action, payload):
    with _INITIALIZE_LOCK:
        if not hasattr(workflow, "pipeline_dispatch_bridge"):
            workflow.pipeline_dispatch_bridge = YouTubeNativeBridge(workflow)
    return workflow.pipeline_dispatch_bridge.dispatch(action, payload)

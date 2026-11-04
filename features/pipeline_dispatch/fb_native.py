"""FB canonical control plane for portable, node-local media/Graph execution.

Runs inside the existing FB sidecar, using its source repositories, policy
checks and stores. It issues a single fresh Page credential only after a
durable attempt intent; no Graph write or video render happens in this module.
"""
from __future__ import annotations
import re
import logging
import secrets
import threading
import time
import weakref
from queue import Queue, Full
from datetime import timedelta
from urllib.parse import urlsplit

from .adapters import AdapterError, obj, artifact_from, public_descriptor
from .native_common import claim_hash, dump, prepare_capabilities
from features.fb_auto_posts.publisher import GraphResult, RequestsGraphTransport
from features.fb_auto_posts.gpu import PROFILE
from features.fb_auto_posts.core import utc_iso
from features.fb_auto_posts.links import validate_short_url, build_short_url, validate_w2a_url
from features.fb_video_publish.links import short_url, validate_long_url

CPU_PROFILE = "fb-post-random-overlay-libx264-720x1280-v1"
_INITIALIZE_LOCK = threading.Lock()

class FBNativeBridge:
    def __init__(self, runtime):
        self.runtime = runtime
        self._source_locks = weakref.WeakValueDictionary()
        self._source_locks_guard = threading.Lock()
        self._reconcile_queue = Queue(maxsize=32)
        self._reconcile_pending = set()
        self._reconcile_guard = threading.Lock()
        self._reconcile_started = False
        self._last_reconcile_warning = 0.0
        self._next_tick = 0.0
        self._tick_guard = threading.Lock()
        with runtime.store.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_fb_claim(
              source_key TEXT NOT NULL,stage TEXT NOT NULL,generation INTEGER NOT NULL,
              claim_hash TEXT NOT NULL,task_json TEXT NOT NULL,descriptor_json TEXT NOT NULL,
              receipt_json TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_key,stage,generation));
            CREATE TABLE IF NOT EXISTS pipeline_fb_operation(
              operation_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,stage TEXT NOT NULL,
              generation INTEGER NOT NULL,request_sha256 TEXT NOT NULL,credential_id TEXT NOT NULL,
              fb_user_id TEXT NOT NULL,native_attempt_id INTEGER,sequence INTEGER NOT NULL,
              result_json TEXT NOT NULL DEFAULT '',checkpoint_json TEXT NOT NULL DEFAULT '');
            """)
            if "checkpoint_json" not in {row[1] for row in conn.execute("PRAGMA table_info(pipeline_fb_operation)")}:
                conn.execute("ALTER TABLE pipeline_fb_operation ADD COLUMN checkpoint_json TEXT NOT NULL DEFAULT ''")

    def _key(self, job):
        if (job.get("channel") != "fb" or job.get("family") not in {"auto", "video"}
                or job.get("stage") not in {"prepare", "publish"}
                or job.get("source_key") != "fb:%s:%s" % (job.get("family"), job.get("source_id"))):
            raise AdapterError("fb_dispatch_source_invalid")
        return str(job["source_key"]), str(job["stage"]), int(job["generation"])

    def _source_lock(self, job):
        # Slow policy/credential reads on one source must not hold unrelated
        # receipts. All stages and generations of a source share this fence;
        # active holders/waiters keep it alive without retaining task history.
        source_key = self._key(job)[0]
        with self._source_locks_guard:
            lock = self._source_locks.get(source_key)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[source_key] = lock
            return lock

    def _stored(self, job, claim):
        with self.runtime.store.connect() as conn:
            row = conn.execute("SELECT * FROM pipeline_fb_claim WHERE source_key=? AND stage=? AND generation=?", self._key(job)).fetchone()
        if row is None or not secrets.compare_digest(row["claim_hash"], claim_hash(claim)):
            raise AdapterError("fb_dispatch_claim_mismatch")
        return dict(row), obj(row["task_json"]), obj(row["descriptor_json"])

    def _video_preflight(self, task):
        worker = self.runtime.manual_worker
        snap, prepared = obj(task["snapshot_json"]), obj(task["prepared_json"])
        if task.get("source_kind") == "group_pool":
            worker._group_preflight(task, snap)
        elif worker.sources is not None:
            manual = task.get("source_kind") == "manual" and "pool" not in snap
            worker.sources.get_pages(snap["actor"], [task["page_id"]], manual=manual)
            fresh = worker.sources.get_materials([task["material_id"]], need_desc=False,
                    enforce_blacklist=task.get("source_kind") not in {"manual", "pool"}, manual=manual)[task["material_id"]]
            if any(str(fresh[k]) != str(snap["material"][k]) for k in ("material_id", "content_id", "language", "media_url")):
                raise AdapterError("material_identity_changed")
        media_url = str(prepared.get("media_url") or "")
        if task["processing_mode"] == "random_overlay":
            if not media_url or media_url == snap["material"]["media_url"] or not all(prepared.get(k) for k in ("sha256", "profile", "random_overlay_recipe")):
                raise AdapterError("prepared_media_invalid")
        elif media_url != snap["material"]["media_url"]:
            raise AdapterError("original_media_invalid")
        if short_url(task["ledger_id"]) != task["short_url"]:
            raise AdapterError("short_link_identity_mismatch")
        validate_long_url(task["long_url"], task["ledger_id"])
        title = snap.get("title", "")
        if not isinstance(title, str):
            raise AdapterError("title_invalid")
        return {"page_id": task["page_id"], "media_url": media_url, "message": task["caption"], "title": title}

    def _auto_preflight(self, task):
        if not task.get("prepared_media_url") or task.get("media_url") != task.get("prepared_media_url") or task.get("prepared_media_url") == task.get("source_media_url"):
            raise AdapterError("fb_auto_prepared_media_required")
        short, long = str(task.get("short_url") or ""), str(task.get("long_url") or "")
        if bool(short) != bool(long):
            raise AdapterError("fb_auto_short_link_snapshot_invalid")
        if short:
            if validate_short_url(short) != build_short_url(task["id"]) or str(task.get("message_text") or "").count(short) != 1:
                raise AdapterError("fb_auto_short_link_snapshot_invalid")
            validate_w2a_url(long)
            self.runtime.executor.short_link_writer(self.runtime.executor.short_link_root, int(task["id"]), long)
        return {"page_id": task["page_id"], "media_url": task["media_url"], "message": task["message_text"], "title": ""}

    def _resume_preparation(self, job, previous):
        """Fence only this source's previous authenticated prepare generation.

        The coordinator issues a new generation only after its lease expires.
        Native media leases are longer, so waiting for those would strand the
        task. Never release a legacy/other task's claim or a publication lease.
        """
        old_task, descriptor = obj(previous["task_json"]), obj(previous["descriptor_json"])
        store = self.runtime.store if job["family"] == "auto" else self.runtime.manual_worker.store
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if previous["receipt_json"]:
                raise AdapterError("canonical_preparation_already_completed")
            if conn.execute("SELECT 1 FROM pipeline_fb_operation WHERE source_key=? LIMIT 1", (job["source_key"],)).fetchone():
                raise AdapterError("canonical_existing_publish_attempt")
            if job["family"] == "auto":
                row = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (job["source_id"],)).fetchone()
                if (row is None or row["status"] != "preparing" or row["lease_owner"] != old_task.get("lease_owner")
                        or not str(row["lease_owner"]).startswith("pipeline:") or row["graph_post_id"]
                        or conn.execute("SELECT 1 FROM fb_auto_publish_attempt WHERE task_id=? LIMIT 1", (job["source_id"],)).fetchone()):
                    raise AdapterError("canonical_prepare_reclaim_conflict")
                source = row["source_media_url"]
                conn.execute("UPDATE fb_auto_task SET lease_expires_at_utc='' WHERE id=? AND lease_owner=?",
                             (job["source_id"], old_task["lease_owner"]))
            else:
                row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (job["source_id"],)).fetchone()
                if (row is None or row["status"] != "queued" or row["prepare_state"] != "preparing"
                        or row["prepare_lease"] != old_task.get("claim_token") or row["execution_lease"]
                        or not str(old_task.get("worker_id", "")).startswith("pipeline:")
                        or conn.execute("SELECT 1 FROM fb_video_attempt WHERE task_id=? LIMIT 1", (job["source_id"],)).fetchone()):
                    raise AdapterError("canonical_prepare_reclaim_conflict")
                source = obj(row["snapshot_json"])["material"]["media_url"]
                conn.execute("UPDATE fb_video_task SET prepare_until='' WHERE task_id=? AND prepare_lease=?",
                             (job["source_id"], old_task["claim_token"]))
            # Preserve the complete request, including template and selected source.
            if any(str(row[key]) != str(old_task.get(key)) for key in ("gpu_job_id", "content_id", "material_id", "page_id")):
                raise AdapterError("canonical_prepare_identity_changed")
            old_source = old_task.get("source_media_url") if job["family"] == "auto" else obj(old_task["snapshot_json"])["material"]["media_url"]
            if source != old_source:
                raise AdapterError("canonical_prepare_identity_changed")
            conn.commit()
        return descriptor

    def reserve(self, job, claim):
        key = self._key(job)
        with self._source_lock(job):
            with self.runtime.store.connect() as conn:
                previous = conn.execute("SELECT * FROM pipeline_fb_claim WHERE source_key=? AND stage=? AND generation=?", key).fetchone()
                frozen = conn.execute("SELECT * FROM pipeline_fb_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1", (job["source_key"],)).fetchone() if job["stage"] == "prepare" else None
            if previous:
                return self._stored(job, claim)[2]
            if not self.runtime.executor.live_enabled:
                raise AdapterError("live_gate_closed")
            worker_id = "pipeline:%s:%s" % (claim.get("node_id", job.get("node_id", "node")), job["generation"])
            stage, family = job["stage"], job["family"]
            identity = int(job["source_id"]) if family == "auto" else str(job["source_id"])
            if frozen and int(frozen["generation"]) >= int(job["generation"]):
                raise AdapterError("canonical_prepare_generation_stale")
            frozen_descriptor = self._resume_preparation(job, frozen) if frozen else None
            if family == "auto":
                task = (self.runtime.store.claim_prepare_next(worker_id, 10200, task_id=identity) if stage == "prepare"
                        else self.runtime.store.claim_next(worker_id, 1200, task_id=identity))
            else:
                task = self.runtime.manual_worker.store.claim("prepare" if stage == "prepare" else "execute", worker_id, task_id=identity)
            if task is None:
                # A central 120s lease can expire before the native 1200s
                # write fence. Leave a different, still-pending source queued.
                table, column = ("fb_auto_task", "id") if family == "auto" else ("fb_video_task", "task_id")
                with self.runtime.store.connect() as conn:
                    pending = conn.execute(f"SELECT status FROM {table} WHERE {column}=?", (identity,)).fetchone()
                if stage == "publish" and pending and pending[0] in {"queued", "ready"}:
                    raise AdapterError("canonical_account_busy")
                raise AdapterError("canonical_task_not_claimable")
            try:
                if frozen_descriptor:
                    descriptor = frozen_descriptor
                elif stage == "prepare":
                    if family == "auto":
                        from features.fb_auto_posts.manual_batch import prepare_source_url, task_policy
                        with self.runtime.store.connect() as conn:
                            policy = task_policy(conn, task)
                        source = prepare_source_url(task["source_media_url"]) if policy else task["source_media_url"]
                    else:
                        source = obj(task["snapshot_json"])["material"]["media_url"]
                    profile = CPU_PROFILE if job.get("executor_backend") == "cpu" else PROFILE
                    descriptor = {"kind": "fb_prepare", "request": {"job_id": task["gpu_job_id"], "content_id": task["content_id"],
                                  "source_url": source, "source_trim_tail_seconds": 0, "video_template": task.get("video_template") or "random_overlay", "expected_profile": profile}}
                    descriptor["required_capabilities"] = prepare_capabilities("fb", profile, "cpu" if profile == CPU_PROFILE else "gpu", job)
                else:
                    request = self._auto_preflight(task) if family == "auto" else self._video_preflight(task)
                    descriptor = {"kind": "fb_publish", "request": request, "page_id": task["page_id"],
                                  "body": request["message"], "artifact": artifact_from(task)}
                    descriptor["artifact"]["url"] = request["media_url"]
                    if family == "video" and task["processing_mode"] == "original":
                        descriptor["processing_mode"] = "original"
                        descriptor["artifact"]["identity_mode"] = "remote_url"
            except Exception as exc:
                self._no_write_failure(job, task, getattr(exc, "code", "canonical_preflight_failed"))
                raise
            descriptor.update({"source_key": job["source_key"], "account_resource": "fb:page:" + str(task["page_id"]),
                               "credential_ref": job["source_key"]})
            public_descriptor(descriptor)
            with self.runtime.store.connect() as conn:
                conn.execute("INSERT INTO pipeline_fb_claim(source_key,stage,generation,claim_hash,task_json,descriptor_json) VALUES(?,?,?,?,?,?)",
                             (*key, claim_hash(claim), dump(task), dump(descriptor)))
            return descriptor

    def _no_write_failure(self, job, task, code):
        """Finalize a native claim that provably issued no external request."""
        if job["family"] == "auto":
            if job["stage"] == "prepare":
                return self.runtime.store.fail_prepare(int(task["id"]), code, code)
            return self.runtime.store.complete_task(int(task["id"]), {"status": "failed", "error_code": code})
        if job["stage"] == "prepare":
            return self.runtime.manual_worker.store.finish_stage(task, "prepare", error=AdapterError(code))
        return self.runtime.manual_worker.store.record_outcome(task, GraphResult("definite_failure", "", code, code, ""), final=True)

    def _credentials(self, family, task):
        credentials = (self.runtime.pages.eligible_credentials(task["page_id"]) if family == "auto"
                       else self.runtime.manual_worker._task_credentials(task))
        # Stable order across restarts; never substitute another Page or owner.
        return sorted(credentials, key=lambda c: (c.credential_id, c.fb_user_id))

    @staticmethod
    def _graph(receipt):
        kind = str(receipt.get("kind") or "unknown")
        post_id = str(receipt.get("post_id") or receipt.get("graph_post_id") or "")
        if post_id:
            if not re.fullmatch(r"[0-9_]{1,128}", post_id):
                raise AdapterError("fb_receipt_id_invalid")
            kind = "success"
        elif kind == "success":
            kind = "unknown"
        if kind not in {"success", "definite_failure", "credential_failure", "unknown", "hold"}:
            kind = "unknown"
        return GraphResult(kind, post_id, str(receipt.get("error_code") or "")[:96],
                           str(receipt.get("message") or "")[:300], str(receipt.get("trace_id") or "")[:128])

    def _apply_outcome(self, job, task, operation, receipt, final):
        result = self._graph(receipt)
        if not final and result.kind not in {"definite_failure", "credential_failure"}:
            raise AdapterError("fb_failover_requires_definite_rejection")
        current = self.reconcile(job)
        if result.kind == "success" and current["status"] == "unknown":
            native = self._recover_accepted(job, task, operation, result)
        elif job["family"] == "video":
            native = self.runtime.manual_worker.store.record_outcome(task, result,
                     credential_id=operation["credential_id"], fb_user_id=operation["fb_user_id"],
                     final=final, attempt_id=operation["native_attempt_id"])
        elif result.kind == "success":
            native = self.runtime.store.complete_submitted_with_attempt(int(task["id"]), operation["sequence"],
                     credential_id=operation["credential_id"], fb_user_id=operation["fb_user_id"], graph_post_id=result.post_id,
                     trace_id=result.trace_id, definite_attempts=operation["sequence"] - 1)
        else:
            self.runtime.store.record_attempt(int(task["id"]), operation["sequence"], credential_id=operation["credential_id"],
                     fb_user_id=operation["fb_user_id"], result_kind="unknown" if result.kind == "unknown" else "definite_failure",
                     error_code=result.error_code, trace_id=result.trace_id)
            native = self.runtime.store.complete_task(int(task["id"]), {"status": "unknown" if result.kind == "unknown" else "failed",
                     "error_code": result.error_code, "error_message": result.message, "definite_attempts": operation["sequence"]}) if final else {"status": "running"}
        with self.runtime.store.connect() as conn:
            conn.execute("UPDATE pipeline_fb_operation SET result_json=? WHERE operation_id=? AND result_json=''", (dump(receipt), operation["operation_id"]))
        return native

    def _recover_accepted(self, job, task, operation, result):
        """Recover an exact late POST receipt into GET-only reconciliation."""
        store = self.runtime.store if job["family"] == "auto" else self.runtime.manual_worker.store
        now = utc_iso(store.now_fn())
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if job["family"] == "auto":
                row = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()
                if row is None or row["status"] != "unknown" or row["graph_post_id"]:
                    raise AdapterError("fb_receipt_recovery_conflict")
                prior = conn.execute("SELECT * FROM fb_auto_publish_attempt WHERE task_id=? AND sequence=?", (task["id"], operation["sequence"])).fetchone()
                if prior and (prior["credential_id"] != operation["credential_id"] or prior["result_kind"] not in {"unknown", "accepted"}):
                    raise AdapterError("fb_receipt_recovery_conflict")
                conn.execute("""INSERT INTO fb_auto_publish_attempt(task_id,sequence,credential_id,fb_user_id,result_kind,error_code,trace_id,created_at_utc)
                    VALUES(?,?,?,?,'accepted','',?,?) ON CONFLICT(task_id,sequence) DO UPDATE SET result_kind='accepted',error_code='',trace_id=excluded.trace_id""",
                    (task["id"], operation["sequence"], operation["credential_id"], operation["fb_user_id"], result.trace_id, now))
                conn.execute("""UPDATE fb_auto_task SET status='submitted',graph_post_id=?,unknown_outcome=0,error_code='',error_message='',
                    lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc=?,completed_at_utc=? WHERE id=? AND status='unknown'""",
                    (result.post_id, now, now, task["id"]))
                store._upsert_ledger(conn, task["id"], row["page_id"], row["material_id"], "submitted", result.post_id,
                    operation["sequence"] - 1, False, "", now)
                store._refresh_run(conn, row["run_id"], now)
            else:
                row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (task["task_id"],)).fetchone()
                if row is None or row["status"] != "unknown" or row["graph_post_id"]:
                    raise AdapterError("fb_receipt_recovery_conflict")
                changed = conn.execute("""UPDATE fb_video_attempt SET result_kind='success',graph_post_id=?,error_code='',trace_id=?
                    WHERE id=? AND task_id=? AND lease_token=? AND credential_id=? AND result_kind IN ('started','unknown','hold')""",
                    (result.post_id, result.trace_id, operation["native_attempt_id"], task["task_id"], task["claim_token"], operation["credential_id"])).rowcount
                if changed != 1:
                    raise AdapterError("fb_receipt_recovery_conflict")
                conn.execute("""UPDATE fb_video_task SET status='submitted',graph_post_id=?,unknown_outcome=0,error_code='',error_message='',
                    execution_lease='',execution_until='',reconcile_at=?,updated_at_utc=? WHERE task_id=? AND status='unknown'""",
                    (result.post_id, now, now, task["task_id"]))
            conn.commit()
        return {"status": "submitted", "graph_post_id": result.post_id, "recovered_late_receipt": True}

    def begin_operation(self, job, claim, operation):
        if job["stage"] != "publish" or operation.get("operation") not in {"fb_video_publish", "fb_publish"}:
            raise AdapterError("fb_operation_invalid")
        with self._source_lock(job):
            stored, task, descriptor = self._stored(job, claim)
            if stored["receipt_json"]:
                raise AdapterError("canonical_task_already_completed")
            details = operation.get("request") or operation.get("request_payload") or operation.get("details") or {}
            frozen = descriptor["request"]
            if any(details.get(k) != v for k, v in frozen.items()):
                raise AdapterError("fb_operation_body_changed")
            if operation.get("account_resource") != descriptor["account_resource"]:
                raise AdapterError("fb_operation_account_changed")
            operation_id = str(operation.get("operation_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9:._-]{8,200}", operation_id):
                raise AdapterError("operation_id_invalid")
            with self.runtime.store.connect() as conn:
                existing = conn.execute("SELECT * FROM pipeline_fb_operation WHERE operation_id=?", (operation_id,)).fetchone()
                previous = conn.execute("SELECT * FROM pipeline_fb_operation WHERE source_key=? AND stage=? AND generation=? ORDER BY sequence DESC LIMIT 1", self._key(job)).fetchone()
            if existing:
                # Credential responses are not replayable authorization to write.
                raise AdapterError("operation_already_started")
            credentials = self._credentials(job["family"], task)
            with self.runtime.store.connect() as conn:
                used = {r[0] for r in conn.execute("SELECT credential_id FROM pipeline_fb_operation WHERE source_key=? AND stage=? AND generation=?", self._key(job))}
            remaining = [c for c in credentials if c.credential_id not in used]
            if previous and not previous["result_json"]:
                receipt = details.get("previous_receipt")
                if not isinstance(receipt, dict) or receipt.get("operation_id") != previous["operation_id"]:
                    raise AdapterError("previous_operation_unresolved")
                if self._graph(receipt).kind not in {"definite_failure", "credential_failure"}:
                    raise AdapterError("fb_failover_requires_definite_rejection")
                if previous["checkpoint_json"] and obj(previous["checkpoint_json"]) != receipt:
                    raise AdapterError("receipt_conflict")
                self._apply_outcome(job, task, dict(previous), receipt, final=not bool(remaining))
            if not remaining:
                if previous is None:
                    self._no_write_failure(job, task, "fb_credentials_exhausted")
                return {"credentials_exhausted": True, "remaining_credentials": 0}
            credential = remaining[0]
            attempt_id = None
            if job["family"] == "video":
                attempt_id = self.runtime.manual_worker.store.begin_attempt(task, credential.credential_id, credential.fb_user_id)
                if attempt_id is None:
                    raise AdapterError("canonical_claim_stale")
            else:
                with self.runtime.store.connect() as conn:
                    current = conn.execute("SELECT status,lease_owner,lease_expires_at_utc FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()
                    if current is None or current["status"] != "running" or current["lease_owner"] != task["lease_owner"] or current["lease_expires_at_utc"] <= utc_iso(self.runtime.store.now_fn()):
                        raise AdapterError("canonical_claim_stale")
                    conn.execute("UPDATE fb_auto_task SET lease_expires_at_utc=? WHERE id=?", (utc_iso(self.runtime.store.now_fn() + timedelta(seconds=1200)), task["id"]))
            sequence = int(previous["sequence"] + 1) if previous else 1
            with self.runtime.store.connect() as conn:
                conn.execute("INSERT INTO pipeline_fb_operation(operation_id,source_key,stage,generation,request_sha256,credential_id,fb_user_id,native_attempt_id,sequence) VALUES(?,?,?,?,?,?,?,?,?)",
                             (operation_id, *self._key(job), str(operation.get("request_sha256") or ""), credential.credential_id, credential.fb_user_id, attempt_id, sequence))
            return {"canonical_intent": {"operation_id": operation_id, "sequence": sequence, "source_key": job["source_key"]},
                    "credentials": {"access_token": credential.token, "credential_id": credential.credential_id,
                                    "fb_user_id": credential.fb_user_id, "page_id": credential.page_id},
                    "remaining_credentials": len(remaining) - 1}

    def complete(self, job, receipt, claim):
        with self._source_lock(job):
            stored, task, descriptor = self._stored(job, claim)
            if stored["receipt_json"]:
                if obj(stored["receipt_json"]) != receipt:
                    raise AdapterError("receipt_conflict")
                return {"idempotent": True}
            if job["stage"] == "prepare":
                prepared = receipt.get("preparation") or receipt.get("result") or receipt.get("artifact") or receipt
                request = descriptor["request"]
                if (str(prepared.get("job_id") or "") != str(request["job_id"])
                        or str(prepared.get("content_id") or "") != str(request["content_id"])
                        or prepared.get("profile") != request["expected_profile"]):
                    raise AdapterError("fb_prepared_identity_mismatch")
                prepared = {"media_url": prepared.get("url") or prepared.get("media_url") or prepared.get("output_url"),
                            "sha256": prepared.get("sha256") or prepared.get("output_sha256"),
                            "size_bytes": prepared.get("size") or prepared.get("size_bytes") or prepared.get("output_size"),
                            "duration_seconds": prepared.get("duration_seconds") or obj(prepared.get("probe")).get("duration"),
                            "profile": prepared.get("profile"), "random_overlay_recipe": prepared.get("random_overlay_recipe") or prepared.get("recipe")}
                uri = urlsplit(str(prepared["media_url"] or ""))
                if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.query or prepared["media_url"] == request["source_url"] or not re.fullmatch(r"[a-f0-9]{64}", str(prepared["sha256"] or "")) or int(prepared["size_bytes"] or 0) <= 0 or not 0 < float(prepared["duration_seconds"] or 0) <= 3600 or not isinstance(prepared["random_overlay_recipe"], dict) or not prepared["random_overlay_recipe"]:
                    raise AdapterError("fb_prepared_receipt_invalid")
                if job["family"] == "auto":
                    with self.runtime.store.connect() as conn:
                        current = conn.execute("SELECT status,lease_owner,lease_expires_at_utc FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone()
                    if current is None or current["status"] != "preparing" or current["lease_owner"] != task["lease_owner"] or current["lease_expires_at_utc"] <= utc_iso(self.runtime.store.now_fn()):
                        raise AdapterError("canonical_claim_stale")
                    result = self.runtime.store.complete_prepare(int(task["id"]), prepared)
                else:
                    result = self.runtime.manual_worker.store.finish_stage(task, "prepare", prepared)
                    if result.get("status") == "stale_claim":
                        raise AdapterError("canonical_claim_stale")
            else:
                with self.runtime.store.connect() as conn:
                    operation = conn.execute("SELECT * FROM pipeline_fb_operation WHERE source_key=? AND stage=? AND generation=? ORDER BY sequence DESC LIMIT 1", self._key(job)).fetchone()
                if operation is None:
                    raise AdapterError("fb_publish_intent_missing")
                current = self.reconcile(job)
                incoming = self._graph(receipt)
                if (current["status"] in {"submitted", "published"} and incoming.post_id
                        and incoming.post_id == current["graph_post_id"]):
                    result = {"idempotent": True, **current}
                elif operation["result_json"] and current["status"] in {"failed", "unknown"}:
                    if obj(operation["result_json"]).get("kind") != receipt.get("kind"):
                        raise AdapterError("receipt_conflict")
                    result = {"idempotent": True, **current}
                else:
                    result = self._apply_outcome(job, task, dict(operation), receipt, final=True)
            with self.runtime.store.connect() as conn:
                conn.execute("UPDATE pipeline_fb_claim SET receipt_json=? WHERE source_key=? AND stage=? AND generation=?", (dump(receipt), *self._key(job)))
            return result

    def checkpoint(self, job, claim, phase, receipt):
        if phase != "fb_publish_attempt" or job["stage"] != "publish":
            raise AdapterError("fb_checkpoint_invalid")
        with self._source_lock(job):
            _, task, _ = self._stored(job, claim)
            with self.runtime.store.connect() as conn:
                operation = conn.execute("SELECT * FROM pipeline_fb_operation WHERE operation_id=? AND source_key=? AND stage=? AND generation=?",
                                         (receipt.get("operation_id"), *self._key(job))).fetchone()
                if operation is None:
                    raise AdapterError("fb_publish_intent_missing")
                if operation["checkpoint_json"] and obj(operation["checkpoint_json"]) != receipt:
                    raise AdapterError("receipt_conflict")
                conn.execute("UPDATE pipeline_fb_operation SET checkpoint_json=? WHERE operation_id=?", (dump(receipt), operation["operation_id"]))
            if not operation["result_json"] and self._graph(receipt).kind in {"success", "unknown", "hold"}:
                self._apply_outcome(job, task, dict(operation), receipt, final=True)
            return {"canonical_checkpoint": True, "operation_id": operation["operation_id"], "source_key": job["source_key"]}

    def fail(self, job, claim, receipt):
        with self._source_lock(job):
            _, task, _ = self._stored(job, claim)
            current = self.reconcile(job)
            if current["status"] == "published" and current.get("graph_post_id"):
                return {"canonical_terminal": True, "canonical_no_write": False, **current}
            with self.runtime.store.connect() as conn:
                operation = conn.execute("SELECT * FROM pipeline_fb_operation WHERE source_key=? AND stage=? AND generation=? ORDER BY sequence DESC LIMIT 1", self._key(job)).fetchone()
            if operation:
                evidence = obj(operation["checkpoint_json"] or operation["result_json"])
                if self._graph(evidence).kind not in {"definite_failure", "credential_failure"}:
                    return {"canonical_no_write": False, **self.reconcile(job)}
                if not operation["result_json"]:
                    self._apply_outcome(job, task, dict(operation), evidence, final=True)
                elif self.reconcile(job)["status"] == "running":
                    self._no_write_failure(job, task, str(receipt.get("error_code") or "worker_stopped_after_rejection"))
            else:
                current = self.reconcile(job)
                if current["status"] not in {"failed", "cancelled", "skipped"}:
                    self._no_write_failure(job, task, str(receipt.get("error_code") or "node_execution_failed"))
            return {"canonical_no_write": True, **self.reconcile(job)}

    def _queue_reconciliation(self, job):
        """GET existing IDs without making the shared collector wait for Meta."""
        worker = self.runtime.executor if job["family"] == "auto" else self.runtime.manual_worker
        if not callable(getattr(worker, "reconcile_next", None)):
            return
        key = str(job["source_key"])
        with self._reconcile_guard:
            if key in self._reconcile_pending:
                return
            try:
                self._reconcile_queue.put_nowait((key, job["family"], job["source_id"]))
            except Full:
                return  # The next canonical scan will offer this ID again.
            self._reconcile_pending.add(key)
            if not self._reconcile_started:
                self._reconcile_started = True
                for index in range(2):
                    threading.Thread(target=self._reconcile_worker,
                                     name="fb-readback-%s" % index, daemon=True).start()

    def _reconcile_worker(self):
        while True:
            key, family, identity = self._reconcile_queue.get()
            try:
                worker = self.runtime.executor if family == "auto" else self.runtime.manual_worker
                # Native task claims/due times deduplicate against existing
                # timers; native credential selection preserves frozen scope.
                graph = RequestsGraphTransport(api_version=getattr(getattr(worker, "graph", None), "api_version", "v22.0"),
                                               timeout_seconds=10)
                worker.reconcile_next("pipeline-fb-readback", task_id=int(identity) if family == "auto" else identity,
                                      max_credentials=8, graph=graph)
            except Exception as exc:
                # Retain the original publication/claim/attempt evidence. A
                # transient lookup failure cannot authorize a second POST.
                with self._reconcile_guard:
                    warn = time.monotonic() - self._last_reconcile_warning >= 60
                    if warn:
                        self._last_reconcile_warning = time.monotonic()
                if warn:
                    logging.getLogger(__name__).warning("fb_readback_failed source=%s error_type=%s", key, type(exc).__name__)
            finally:
                with self._reconcile_guard:
                    self._reconcile_pending.discard(key)
                self._reconcile_queue.task_done()

    def reconcile(self, job):
        # Native execution timers may be disabled after dispatch takeover.
        # Keep their existing local expiry rules alive without any POST/retry.
        if time.monotonic() >= self._next_tick and self._tick_guard.acquire(blocking=False):
            try:
                manual = getattr(self.runtime, "manual_worker", None)
                if manual is not None:
                    manual.store.tick()
                expire = getattr(self.runtime.store, "maintenance_tick", None)
                if expire is not None:
                    expire()
                self._next_tick = time.monotonic() + 15
            finally:
                self._tick_guard.release()
        table, key = ("fb_auto_task", "id") if job["family"] == "auto" else ("fb_video_task", "task_id")
        lease = "lease_expires_at_utc" if job["family"] == "auto" else "execution_until"
        store = self.runtime.store if job["family"] == "auto" else self.runtime.manual_worker.store
        with store.connect() as conn:
            row = conn.execute(f"SELECT status,graph_post_id,unknown_outcome,{lease} FROM {table} WHERE {key}=?", (job["source_id"],)).fetchone()
        if row is None:
            raise AdapterError("canonical_task_missing")
        from .fb_isolation import page_write_active
        if (row["status"] in {"submitted", "unknown"} and row["graph_post_id"]
                and not page_write_active(dict(row), job["family"], store.now_fn())):
            self._queue_reconciliation(job)
        return {key: row[key] for key in ("status", "graph_post_id", "unknown_outcome")}

    def pause(self, job, cutoff):
        from .canonical_holds import pause_row, SCHEMA
        lock_job = dict(job, generation=job.get("generation", 0), stage=job.get("stage", "prepare"))
        self._key(lock_job)
        store = self.runtime.store if job["family"] == "auto" else self.runtime.manual_worker.store
        with self._source_lock(lock_job), store.connect() as conn:
            conn.execute(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            if job["family"] == "auto":
                result = pause_row(conn, job, cutoff, table="fb_auto_task", identity="id", state="status",
                    allowed={"planned", "ready", "queued"}, attempt_table="fb_auto_publish_attempt")
            else:
                result = pause_row(conn, job, cutoff, table="fb_video_task", identity="task_id", state="status", allowed={"queued"},
                    attempt_table="fb_video_attempt", additional_active=lambda row: row.get("prepare_state") == "preparing" or row.get("link_state") == "writing")
            conn.commit()
            return result

    def dispatch(self, action, payload):
        if not isinstance(payload, dict) or set(payload) - {"job", "claim", "operation", "receipt", "phase", "cutoff"}:
            raise AdapterError("canonical_bridge_payload_invalid")
        job = payload.get("job") or {}
        if action == "reserve": return self.reserve(job, payload.get("claim") or {})
        if action == "begin-operation": return self.begin_operation(job, payload.get("claim") or {}, payload.get("operation") or {})
        if action == "complete": return self.complete(job, payload.get("receipt") or {}, payload.get("claim") or {})
        if action == "checkpoint": return self.checkpoint(job, payload.get("claim") or {}, payload.get("phase"), payload.get("receipt") or {})
        if action == "fail": return self.fail(job, payload.get("claim") or {}, payload.get("receipt") or {})
        if action == "reconcile": return self.reconcile(job)
        if action == "pause": return self.pause(job, payload.get("cutoff"))
        raise AdapterError("canonical_bridge_action_invalid")


def handle_fb_dispatch(runtime, action, payload):
    with _INITIALIZE_LOCK:
        if not hasattr(runtime, "pipeline_dispatch_bridge"):
            runtime.pipeline_dispatch_bridge = FBNativeBridge(runtime)
    return runtime.pipeline_dispatch_bridge.dispatch(action, payload)

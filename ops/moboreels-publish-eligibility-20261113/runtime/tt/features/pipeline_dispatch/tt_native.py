"""TT ledger bridge: native policy/claims centrally, media and POST on a node.

The existing media validators are reused through an injected result port. No
render, upload or publication is proxied back to this process.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import weakref
from dataclasses import is_dataclass, replace
from urllib.parse import urlsplit

from .adapters import AdapterError, ClosingConnection, artifact_from, obj, public_descriptor
from .native_common import claim_hash, dump, prepare_capabilities

_INIT_LOCK = threading.Lock()


class BoundaryReached(Exception):
    pass


class MediaPort:
    def __init__(self, delegate, result=None, expected_request=None):
        self.delegate, self.result, self.request = delegate, result, None
        self.expected_request, self.route_identity = expected_request, None

    def creator_info(self, **kwargs):
        return self.delegate.creator_info(**kwargs)

    def prepare(self, **kwargs):
        self.request = kwargs
        if self.expected_request is not None:
            material = kwargs.get("material") or {}
            actual = {"job_id": kwargs.get("job_id"), "content_id": material.get("content_id"),
                "source_url": material.get("source_media_url") or material.get("media_url"),
                "source_trim_tail_seconds": kwargs.get("source_trim_tail_seconds"), "expected_profile": kwargs.get("expected_profile")}
            if actual != self.expected_request:
                raise AdapterError("tt_frozen_prepare_request_changed")
        if self.result is not None:
            return self.result
        raise BoundaryReached()

    def publish(self, **kwargs):
        # Never retain the temporarily opened account credential.
        self.request = {k: v for k, v in kwargs.items() if k != "access_token"}
        raise BoundaryReached()


def media_clone(owner, *, result=None, profile=None, trim=None, expected_request=None, expected_route=None):
    """Intercept both legacy direct clients and frozen production route clients."""
    clone, port = copy.copy(owner), MediaPort(owner.gpu_client, result, expected_request)
    clone.gpu_client = port
    if profile is not None:
        clone.media_profile_version = profile
    if trim is not None:
        clone.source_trim_tail_seconds = trim
    route_for_task = getattr(owner, "_video_route_for_task", None)
    if callable(route_for_task):
        def routed(task):
            route = route_for_task(task)
            identity = {"profile": route.media_profile_version, "source_trim_tail_seconds": route.source_trim_tail_seconds}
            if expected_route is not None and identity != expected_route:
                raise AdapterError("tt_frozen_video_route_changed")
            port.delegate, port.route_identity = route.gpu_client, identity
            changes = {"gpu_client": port}
            if profile is not None:
                changes["media_profile_version"] = profile
            if trim is not None:
                changes["source_trim_tail_seconds"] = trim
            if is_dataclass(route):
                return replace(route, **changes)
            copied = copy.copy(route)
            for key, value in changes.items():
                setattr(copied, key, value)
            return copied
        clone._video_route_for_task = routed
    return clone, port


def outro_capabilities(job, profile):
    required = []
    if "direct-outro" in profile:
        for kind in ("outro", "font"):
            approved = os.environ.get("PIPELINE_TT_APPROVED_" + kind.upper() + "_SHA256", "").strip()
            if not re.fullmatch(r"[a-f0-9]{64}", approved):
                raise AdapterError("canonical_render_asset_approval_missing")
            matches = sorted({value for value in job.get("executor_capabilities", []) if isinstance(value, str)
                and re.fullmatch("asset:tt-" + kind + ":[a-f0-9]{64}", value)})
            if len(matches) != 1:
                raise AdapterError("canonical_render_asset_unavailable")
            if not secrets.compare_digest(matches[0], "asset:tt-" + kind + ":" + approved):
                raise AdapterError("canonical_render_asset_not_approved")
            required.extend(matches)
    return required


def preparation_target(job, profile, *, automatic=False):
    """Freeze the selected host, including profiles both encoders support."""
    from .tt_profiles import supported_profiles
    cpu_variants = {
        "tt-post-hevc-720x1280-v2": "tt-post-h264-720x1280-v2",
        "tt-post-direct-clean-hevc-720x1280-v1": "tt-post-direct-clean-h264-720x1280-v1",
        "tt-post-direct-outro-hevc-720x1280-v2": "tt-post-direct-outro-h264-720x1280-v2",
        "tt-post-random-overlay-hevc-720x1280-v3": "tt-post-random-overlay-libx264-720x1280-v1",
        "tt-post-random-overlay-h264-720x1280-v3": "tt-post-random-overlay-libx264-720x1280-v1",
    }
    # Older callers did not provide a host. Retain their prior inference;
    # current coordinator calls always provide the actual selected backend.
    backend = job.get("executor_backend") or ("cpu" if profile in cpu_variants.values() else "gpu")
    if automatic and backend == "cpu":
        profile = cpu_variants.get(profile, profile)
    if profile not in supported_profiles(backend):
        raise AdapterError("canonical_render_profile_backend_mismatch")
    return profile, backend


def post_info(queue):
    return {"title": queue.get("caption", ""), "privacy_level": queue.get("privacy_level"),
            "disable_comment": not bool(queue.get("allow_comment")), "disable_duet": not bool(queue.get("allow_duet")),
            "disable_stitch": not bool(queue.get("allow_stitch")), "brand_content_toggle": bool(queue.get("brand_content_toggle")),
            "brand_organic_toggle": bool(queue.get("brand_organic_toggle")), "is_aigc": bool(queue.get("is_aigc"))}


class TTNativeBridge:
    def __init__(self, service, *, automatic=False):
        self.service, self.automatic = service, automatic
        self.store = service.store
        self._source_locks = weakref.WeakValueDictionary()
        self._source_locks_guard = threading.Lock()
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_tt_claim(source_key TEXT,stage TEXT,generation INTEGER,
              claim_hash TEXT NOT NULL,native_token TEXT NOT NULL,descriptor_json TEXT NOT NULL,
              receipt_json TEXT NOT NULL DEFAULT '',PRIMARY KEY(source_key,stage,generation));
            CREATE TABLE IF NOT EXISTS pipeline_tt_operation(operation_id TEXT PRIMARY KEY,source_key TEXT,
              stage TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT,checkpoint_json TEXT NOT NULL DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS pipeline_tt_single_init ON pipeline_tt_operation(source_key)
              WHERE operation='tt_initialize_video';
            """)

    def connect(self):
        c = sqlite3.connect(self.store.db_path, timeout=30, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        return c

    def _key(self, job):
        families = {"auto"} if self.automatic else {"queue", "intake", "direct_test"}
        if (job.get("channel") != "tt" or job.get("family") not in families
                or job.get("source_key") != "tt:%s:%s" % (job.get("family"), job.get("source_id"))):
            raise AdapterError("tt_dispatch_source_invalid")
        return job["source_key"], job["stage"], int(job["generation"])

    def _source_lock(self, job):
        # Active holders and waiters retain the lock; unused source entries
        # disappear automatically instead of growing with the task history.
        # Stage/generation share one source fence, including late callbacks.
        source_key = self._key(job)[0]
        with self._source_locks_guard:
            lock = self._source_locks.get(source_key)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[source_key] = lock
            return lock

    def _stored(self, job, claim):
        with self.connect() as c:
            stored = c.execute("SELECT * FROM pipeline_tt_claim WHERE source_key=? AND stage=? AND generation=?", self._key(job)).fetchone()
        if stored is None or not secrets.compare_digest(stored["claim_hash"], claim_hash(claim)):
            raise AdapterError("tt_dispatch_claim_mismatch")
        return dict(stored), obj(stored["descriptor_json"])

    def _native(self, job):
        if self.automatic:
            return self.store.get_task(int(job["source_id"]))
        if job["family"] == "intake":
            return self.store.get_material_intake(int(job["source_id"]))
        if job["family"] == "direct_test":
            return self.store.get_direct_test(int(job["source_id"]))
        return self.store.get_queue(int(job["source_id"]))

    def _native_dict(self, job):
        item = self._native(job)
        return item.as_dict() if hasattr(item, "as_dict") else dict(item)

    def _prepare_descriptor(self, owner, call, *, profile=None):
        clone, port = media_clone(owner, profile=profile)
        try:
            call(clone)
        except BoundaryReached:
            pass
        if port.request is None:
            raise AdapterError("tt_media_boundary_missing")
        request = port.request
        material = request["material"]
        descriptor = {"kind": "tt_prepare", "request": {"job_id": request["job_id"], "content_id": material["content_id"],
                "source_url": material.get("source_media_url") or material.get("media_url"),
                "source_trim_tail_seconds": request["source_trim_tail_seconds"], "expected_profile": request["expected_profile"]}}
        if port.route_identity is not None:
            descriptor["native_route"] = port.route_identity
        return descriptor

    def _queue_preflight(self, queue, token):
        from features.tt_posts.core import TTPostPolicy
        from features.tt_posts.service import _creator_info_hash
        svc = self.service
        if queue.get("publish_mode") != "direct_post" or not svc.gates.is_open:
            raise AdapterError("tt_live_gates_closed")
        creator = svc._creator_recheck(str(queue["account_id"]), str(queue["gpu_job_id"]), int(queue["id"]), token)
        policy = TTPostPolicy(privacy_level=str(queue["privacy_level"]), allow_comment=bool(queue["allow_comment"]),
            allow_duet=bool(queue["allow_duet"]), allow_stitch=bool(queue["allow_stitch"]), brand_content_toggle=False,
            brand_organic_toggle=False, user_consent=True, consent_version=str(queue["consent_version"]),
            consented_at_utc=str(queue["consented_at_utc"]))
        svc._assert_creator_policy(creator, policy)
        if not secrets.compare_digest(_creator_info_hash(creator), str(queue.get("creator_info_hash") or "")):
            raise AdapterError("tt_creator_info_changed")
        self.store.renew_claim(queue["id"], token, lease_seconds=10800)
        return svc._prepare_queue_short_link(queue, token)

    def _direct_prepare_preflight(self, task):
        # Same creator/settings gate as the existing manual preparation route.
        # The immutable profile and trim are validated again at completion.
        from features.tt_posts.core import TTPostAccountSettings
        account = str(task["account_id"])
        self.service.account_repository.get_public_account(account)
        saved = self.store.get_account_settings(account, required=True)
        settings = TTPostAccountSettings.from_mapping({key: saved[key] for key in (
            "privacy_level", "allow_comment", "allow_duet", "allow_stitch", "brand_content_toggle",
            "brand_organic_toggle", "is_aigc", "drama_language")})
        creator = self.service.creator_info({"source_account_id": account})["item"]
        self.service._assert_creator_settings(creator, settings)

    def _resume_preparation(self, job, previous):
        """Revoke only the exact older pipeline prepare token, never publish."""
        descriptor = obj(previous["descriptor_json"])
        table = "tt_auto_task" if self.automatic else "tt_post_direct_test" if job["family"] == "direct_test" else "tt_post_material_intake"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM " + table + " WHERE id=?", (job["source_id"],)).fetchone()
            if previous["receipt_json"]:
                raise AdapterError("canonical_preparation_already_completed")
            if (row is None or row["status"] != "preparing" or row["claim_token"] != previous["native_token"]
                    or not str(row["claim_worker"]).startswith("pipeline:")
                    or ("claim_phase" in row.keys() and row["claim_phase"] not in ({"selection", "prepare"} if self.automatic else {"prepare"}))
                    or ("publish_id" in row.keys() and row["publish_id"])
                    or ("publish_attempt_count" in row.keys() and row["publish_attempt_count"])
                    or ("unknown_outcome" in row.keys() and row["unknown_outcome"])
                    or conn.execute("SELECT 1 FROM pipeline_tt_operation WHERE source_key=? LIMIT 1", (job["source_key"],)).fetchone()):
                raise AdapterError("canonical_prepare_reclaim_conflict")
            request = descriptor["request"]
            identity = descriptor.get("native_identity", {})
            if (str(row["gpu_job_id"]) != str(request["job_id"]) or str(row["content_id"]) != str(request["content_id"])
                    or row["source_media_url"] != request["source_url"]
                    or descriptor["account_resource"] != "tt:account:" + str(row["account_id"])
                    or (identity and str(row["material_id"]) != identity.get("material_id"))
                    or ("source_trim_tail_seconds" in row.keys() and float(row["source_trim_tail_seconds"]) != float(request["source_trim_tail_seconds"]))
                    or ("preparation_profile" in row.keys() and row["preparation_profile"] != request["expected_profile"])):
                raise AdapterError("canonical_prepare_identity_changed")
            phase = ",claim_phase='prepare'" if self.automatic else ""
            conn.execute("UPDATE " + table + " SET lease_expires_at_utc='1970-01-01T00:00:00+00:00'" + phase + " WHERE id=? AND claim_token=?",
                         (job["source_id"], previous["native_token"]))
            conn.commit()
        return descriptor

    def _pending_account_busy(self, job):
        """Classify a native pending-account fence without changing admission."""
        if not self.automatic or job["stage"] != "prepare":
            return False
        from features.tt_auto_posts.core import ACTIVE_ACCOUNT_TASK_STATUSES
        current, boundary = self.store._now_iso(), self.store.execution_not_before_utc
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (job["source_id"],)).fetchone()
            if (row is None or row["status"] != "pending" or row["claim_token"]
                    or row["publish_attempt_count"] or row["publish_id"] or row["unknown_outcome"]
                    or conn.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone()):
                return False
            return conn.execute("""SELECT 1 FROM tt_auto_task other
                WHERE account_id=? AND id<>? AND status IN (""" + ",".join("?" for _ in ACTIVE_ACCOUNT_TASK_STATUSES) + """)
                  AND NOT EXISTS(SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='tt:auto:'||other.id)
                  AND (scheduled_at_utc>=? OR publish_id<>'' OR unknown_outcome<>0 OR publish_attempt_count<>0
                       OR status IN ('publishing','reconciling','unknown') OR claim_phase='reconcile'
                       OR (claim_token<>'' AND lease_expires_at_utc>?)) LIMIT 1""",
                (row["account_id"], row["id"], *sorted(ACTIVE_ACCOUNT_TASK_STATUSES), boundary, current)).fetchone() is not None

    def reserve(self, job, claim):
        key = self._key(job)
        with self._source_lock(job):
            with self.connect() as c:
                existing = c.execute("SELECT 1 FROM pipeline_tt_claim WHERE source_key=? AND stage=? AND generation=?", key).fetchone()
                frozen = c.execute("SELECT * FROM pipeline_tt_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1", (job["source_key"],)).fetchone() if job["stage"] == "prepare" else None
            if existing:
                return self._stored(job, claim)[1]
            approved_assets, selected_profile, selected_backend = [], None, None
            if job["stage"] == "prepare" and not frozen:
                if self.automatic:
                    executor = self.service.executor
                    route_for_task = getattr(executor, "_video_route_for_task", None)
                    profile = route_for_task(self._native(job)).media_profile_version if callable(route_for_task) else executor.media_profile_version
                elif job["family"] in {"intake", "direct_test"}:
                    profile = self._native_dict(job)["preparation_profile"]
                else:
                    raise AdapterError("tt_queue_is_publish_only")
                # Missing node assets must not create a native claim with no
                # persisted binding. Frozen generations keep their original caps.
                selected_profile, selected_backend = preparation_target(job, profile, automatic=self.automatic)
                approved_assets = outro_capabilities(job, selected_profile)
            worker = "pipeline:%s:%s" % (claim.get("node_id", "node"), job["generation"])
            identity, native_token = int(job["source_id"]), ""
            if frozen and int(frozen["generation"]) >= int(job["generation"]):
                raise AdapterError("canonical_prepare_generation_stale")
            frozen_descriptor = self._resume_preparation(job, frozen) if frozen else None
            try:
                if self.automatic:
                    executor = self.service.executor
                    if not executor.gates.is_open:
                        raise AdapterError("tt_live_gates_closed")
                    native = self.store.claim_next_executable_task(worker_id=worker, lease_seconds=10800,
                        prepare_ahead_seconds=getattr(executor, "prepare_ahead_seconds", 0),
                        allowed_phases={"selection", "prepare"} if job["stage"] == "prepare" else {"publish"}, task_id=identity)
                    if native is None:
                        if self._pending_account_busy(job):
                            raise AdapterError("canonical_account_busy")
                        raise AdapterError("canonical_task_not_claimable")
                    native_token, task = native.reveal_claim_token(), native.task
                    if native.claim_phase == "selection":
                        task = executor._select(task, native_token)
                    if job["stage"] == "prepare":
                        # Only unfrozen automatic work selects the explicit H264
                        # profile; an already frozen intake profile stays intact.
                        descriptor = frozen_descriptor or self._prepare_descriptor(executor, lambda clone: clone._prepare(task, native_token), profile=selected_profile)
                        # A selection claim continues as the same frozen prepare
                        # item; loss of the HTTP response must not reselect it.
                        task = self.store.transition_task(identity, "preparing", expected_statuses={"preparing"},
                            claim_token=native_token, updates={"claim_phase": "prepare"}, event_type="pipeline_preparation_frozen")
                    else:
                        clone, port = media_clone(executor)
                        try:
                            clone._publish(task, native_token)
                        except BoundaryReached:
                            pass
                        if port.request is None:
                            raise AdapterError("tt_publish_boundary_missing")
                        descriptor = {"kind": "tt_publish", "request": {"post_info": post_info(port.request["queue"])},
                                      "artifact": artifact_from(task.as_dict())}
                    account_id = task.account_id
                elif job["family"] == "direct_test":
                    if job["stage"] == "prepare":
                        claims = self.store.claim_direct_test_prepare(worker, lease_seconds=10800, limit=1, direct_test_id=identity)
                        if not claims:
                            raise AdapterError("canonical_task_not_claimable")
                        native_token, task = claims[0].reveal_claim_token(), claims[0].item
                        self._direct_prepare_preflight(task)
                        clone = copy.copy(self.service)
                        clone.source_trim_tail_seconds = float(task["source_trim_tail_seconds"])
                        descriptor = frozen_descriptor or self._prepare_descriptor(clone, lambda svc: svc._prepare_resolved({
                            "material_id": task["material_id"], "content_id": task["content_id"],
                            "source_media_url": task["source_media_url"]}, gpu_job_id=task["gpu_job_id"]), profile=task["preparation_profile"])
                    else:
                        native = self.store.claim_direct_test_publish(identity, worker, lease_seconds=10800)
                        native_token, task = native.reveal_claim_token(), native.item
                        clone, port = media_clone(self.service)
                        try:
                            clone.direct_publish_claimed(identity, native_token)
                        except BoundaryReached:
                            pass
                        if port.request is None:
                            raise AdapterError("tt_publish_boundary_missing")
                        descriptor = {"kind": "tt_publish", "request": {"post_info": post_info(port.request["queue"])},
                                      "artifact": artifact_from(task)}
                    account_id = task["account_id"]
                elif job["family"] == "intake":
                    if job["stage"] != "prepare":
                        raise AdapterError("tt_intake_is_prepare_only")
                    native = self.store.claim_material_intake(worker, lease_seconds=10800, intake_id=identity)
                    if native is None:
                        raise AdapterError("canonical_task_not_claimable")
                    task, native_token = native.item, native.reveal_claim_token()
                    clone = copy.copy(self.service)
                    clone.source_trim_tail_seconds = float(task["source_trim_tail_seconds"])
                    descriptor = frozen_descriptor or self._prepare_descriptor(clone, lambda svc: svc._prepare_resolved({
                        "material_id": str(task["material_id"]), "content_id": str(task["content_id"]),
                        "source_media_url": task["source_media_url"]}, gpu_job_id=task["gpu_job_id"]), profile=task["preparation_profile"])
                    account_id = task["account_id"]
                else:
                    if job["stage"] != "publish":
                        raise AdapterError("tt_queue_is_publish_only")
                    claims = self.store.claim_due(worker, lease_seconds=10800, grace_seconds=600, limit=1, queue_id=identity)
                    if not claims:
                        raise AdapterError("canonical_task_not_claimable")
                    native_token = claims[0].reveal_claim_token()
                    task = self._queue_preflight(claims[0].queue, native_token)
                    descriptor = {"kind": "tt_publish", "request": {"post_info": post_info(task)}, "artifact": artifact_from(task)}
                    # Queue fields use output_* rather than intake prepared_*.
                    descriptor["artifact"].update(url=task.get("prepared_media_url") or task.get("media_url"),
                        sha256=task.get("output_sha256") or task.get("prepared_output_sha256"),
                        size=int(task.get("output_size") or task.get("media_size_bytes") or task.get("prepared_output_size") or 0))
                    account_id = task["account_id"]
            except Exception as exc:
                if native_token:
                    from features.tt_auto_posts.selector import NoEligibleMaterial
                    if self.automatic and isinstance(exc, NoEligibleMaterial):
                        current = self._native(job)
                        result = self.service.executor._retry_or_fail(current, "selection", exc, native_token)
                        self.service.executor._update_run(result.run_id)
                    else:
                        self._fail_native(job, native_token, getattr(exc, "code", "tt_preflight_failed"), known_no_write=True)
                raise
            descriptor.update(source_key=job["source_key"], account_resource="tt:account:" + str(account_id), credential_ref=job["source_key"])
            if job["stage"] == "prepare" and not descriptor.get("required_capabilities"):
                profile = descriptor["request"]["expected_profile"]
                descriptor["required_capabilities"] = prepare_capabilities("tt", profile, selected_backend, job)
                descriptor["required_capabilities"].extend(approved_assets)
            if job["stage"] == "prepare" and "native_identity" not in descriptor:
                current = self._native_dict(job)
                descriptor["native_identity"] = {"material_id": str(current["material_id"]), "account_id": str(account_id)}
            if job["stage"] == "publish":
                origin = "https://" + urlsplit(descriptor["artifact"]["url"]).netloc
                configured = os.environ.get("TT_POST_GPU_VERIFIED_URL_PREFIX", "") or os.environ.get("PIPELINE_TT_VERIFIED_ORIGIN", "")
                if configured and not descriptor["artifact"]["url"].startswith(configured):
                    raise AdapterError("tt_publish_url_property_mismatch")
                descriptor["request"]["verified_origin"] = origin
            public_descriptor(descriptor)
            with self.connect() as c:
                c.execute("INSERT INTO pipeline_tt_claim(source_key,stage,generation,claim_hash,native_token,descriptor_json) VALUES(?,?,?,?,?,?)",
                          (*key, claim_hash(claim), native_token, dump(descriptor)))
            return descriptor

    def begin_operation(self, job, claim, operation):
        with self._source_lock(job):
            stored, descriptor = self._stored(job, claim)
            if job["stage"] != "publish":
                raise AdapterError("tt_operation_invalid")
            name = operation.get("operation")
            details = operation.get("request") or operation.get("request_payload") or operation.get("details") or {}
            current = self._native_dict(job)
            if operation.get("account_resource") != descriptor["account_resource"]:
                raise AdapterError("tt_operation_account_changed")
            if name == "tt_initialize_video":
                if current.get("publish_id") or current.get("unknown_outcome") or stored["receipt_json"]:
                    raise AdapterError("tt_reconcile_only")
                if details != {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]}:
                    raise AdapterError("tt_operation_body_changed")
                from features.moboreels_library.macro_client import contains_macro, assert_publishable
                template_text = current.get("caption_template")
                if self.automatic:
                    template_text = self.store.get_template(current["template_id"], version=current["template_version"]).config.get("caption_template")
                if contains_macro(template_text):
                    assert_publishable(current.get("content_id"))
                from features.moboreels_library.macro_client import contains_macro, assert_publishable
                template_text = current.get("caption_template")
                if self.automatic:
                    template_text = self.store.get_template(current["template_id"], version=current["template_version"]).config.get("caption_template")
                if contains_macro(template_text):
                    assert_publishable(current.get("content_id"))
                from features.moboreels_library.macro_client import contains_macro, assert_publishable
                template_text = current.get("caption_template")
                if self.automatic:
                    template_text = self.store.get_template(current["template_id"], version=current["template_version"]).config.get("caption_template")
                if contains_macro(template_text):
                    assert_publishable(current.get("content_id"))
                if job["family"] == "direct_test":
                    self.store.renew_direct_test_publish(int(job["source_id"]), stored["native_token"], lease_seconds=10800)
                elif not self.automatic:
                    self.store.begin_publish(int(job["source_id"]), stored["native_token"], self.service.gates)
                elif current["status"] != "publishing":
                    raise AdapterError("canonical_claim_stale")
            elif name == "tt_fetch_status":
                if not current.get("publish_id") or details != {"publish_id": current["publish_id"]}:
                    raise AdapterError("tt_publish_id_mismatch")
            else:
                raise AdapterError("tt_operation_invalid")
            operation_id = str(operation.get("operation_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9:._-]{8,200}", operation_id):
                raise AdapterError("operation_id_invalid")
            try:
                with self.connect() as c:
                    c.execute("INSERT INTO pipeline_tt_operation(operation_id,source_key,stage,generation,operation,request_sha256) VALUES(?,?,?,?,?,?)",
                              (operation_id, *self._key(job), name, operation.get("request_sha256", "")))
            except sqlite3.IntegrityError:
                raise AdapterError("operation_already_started") from None
            source = self.service.executor.account_source if self.automatic else self.service.account_source
            with source.publish_credentials(str(current["account_id"])) as credentials:
                token = credentials.reveal_access_token()
            return {"canonical_intent": {"operation_id": operation_id, "source_key": job["source_key"]}, "credentials": {"access_token": token}}

    def checkpoint(self, job, claim, phase, receipt):
        with self._source_lock(job):
            stored, _ = self._stored(job, claim)
            with self.connect() as c:
                operation = c.execute("SELECT * FROM pipeline_tt_operation WHERE source_key=? AND stage=? AND generation=? ORDER BY rowid DESC LIMIT 1", self._key(job)).fetchone()
            expected = "tt_initialize_video" if phase == "tt_initialized" else "tt_fetch_status" if phase == "tt_status" else ""
            if not operation or operation["operation"] != expected:
                raise AdapterError("tt_checkpoint_without_operation")
            if operation["checkpoint_json"]:
                if obj(operation["checkpoint_json"]) != receipt:
                    raise AdapterError("receipt_conflict")
                return {"canonical_checkpoint": True}
            publish_id = str(receipt.get("publish_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9._~:+/-]{1,512}", publish_id):
                raise AdapterError("tt_publish_id_invalid")
            current = self._native_dict(job)
            if current.get("publish_id") and current["publish_id"] != publish_id:
                raise AdapterError("tt_publish_id_mismatch")
            if phase == "tt_initialized":
                if self.automatic:
                    if not current.get("publish_id"):
                        self.store.transition_task(int(job["source_id"]), "reconciling", expected_statuses={"publishing", "unknown"},
                            claim_token=stored["native_token"], updates={"publish_id": publish_id, "unknown_outcome": False, "claim_phase": "reconcile"}, event_type="task_publish_id_recorded")
                elif job["family"] == "direct_test":
                    if current["status"] == "unknown":
                        self.store.recover_direct_test_publish_id(int(job["source_id"]), publish_id)
                    else:
                        self.store.record_direct_test_publish_id(int(job["source_id"]), stored["native_token"], publish_id)
                else:
                    if current["status"] == "unknown":
                        # This ID belongs to the durable single-init intent;
                        # recovery never invokes the platform init again.
                        self.store.recover_publish_id_from_gpu_ledger(int(job["source_id"]), publish_id)
                    else:
                        self.store.record_publish_id(int(job["source_id"]), stored["native_token"], publish_id)
            elif receipt.get("status") in {"PUBLISH_COMPLETE", "FAILED"}:
                self._terminal(job, stored, receipt)
            with self.connect() as c:
                c.execute("UPDATE pipeline_tt_operation SET checkpoint_json=? WHERE operation_id=?", (dump(receipt), operation["operation_id"]))
            return {"canonical_checkpoint": True, "publish_id": publish_id}

    def _terminal(self, job, stored, receipt):
        success = receipt.get("status") == "PUBLISH_COMPLETE"
        if self.automatic:
            current = self._native_dict(job)
            if current["status"] in {"published", "failed"}:
                return current
            result = self.store.transition_task(int(job["source_id"]), "published" if success else "failed", expected_statuses={"reconciling"},
                claim_token=stored["native_token"], updates={"publish_id": receipt["publish_id"], "unknown_outcome": False,
                    "claim_phase": "", "claim_worker": "", "claim_token": "", "lease_expires_at_utc": "", "error_code": "" if success else "tt_platform_failed"},
                event_type="task_published" if success else "task_publish_failed")
            self.service.executor._update_run(result.run_id)
            return result.as_dict()
        if job["family"] == "direct_test":
            if success:
                return self.store.reconcile_direct_test_published(int(job["source_id"]), receipt["publish_id"], publish_url="")
            return self.store.reconcile_direct_test_failed(int(job["source_id"]), receipt["publish_id"], remote_status="failed")
        if success:
            result = self.service._reconcile_published_with_cache(int(job["source_id"]), receipt["publish_id"], publish_url="")
        else:
            result = self.store.reconcile_failed(int(job["source_id"]), receipt["publish_id"], remote_status="failed")
        self.service._sync_recurring_queue_if_present(result)
        return result

    def complete(self, job, receipt, claim):
        with self._source_lock(job):
            stored, descriptor = self._stored(job, claim)
            if stored["receipt_json"]:
                if obj(stored["receipt_json"]) != receipt:
                    raise AdapterError("receipt_conflict")
                return {"idempotent": True}
            if job["stage"] == "prepare":
                prepared = receipt.get("preparation") or receipt.get("result") or receipt
                request = descriptor["request"]
                if self.automatic:
                    clone, _ = media_clone(self.service.executor, result=prepared, profile=request["expected_profile"],
                        trim=request["source_trim_tail_seconds"], expected_request=request, expected_route=descriptor.get("native_route"))
                    result = clone._prepare(self._native(job), stored["native_token"])
                    clone._update_run(result.run_id)
                elif job["family"] == "direct_test":
                    clone, _ = media_clone(self.service, result=prepared, profile=request["expected_profile"],
                        trim=request["source_trim_tail_seconds"], expected_request=request)
                    clone._kick_runner = lambda: False
                    clone.direct_preparation_process(int(job["source_id"]), {"claim_token": stored["native_token"]})
                else:
                    current = self._native(job)
                    clone, _ = media_clone(self.service, result=prepared, profile=request["expected_profile"],
                        trim=request["source_trim_tail_seconds"], expected_request=request)
                    result = clone._prepare_resolved({"material_id": current["material_id"], "content_id": current["content_id"], "source_media_url": current["source_media_url"]}, gpu_job_id=current["gpu_job_id"])
                    self.store.complete_material_intake(int(job["source_id"]), stored["native_token"], gpu_job_id=result["gpu_job_id"],
                        prepared_media_url=result["prepared_media_url"], prepared_output_sha256=result["output_sha256"],
                        prepared_output_size=result["output_size"], prepared_duration_sec=result["duration_sec"],
                        source_trim_tail_seconds=result["source_trim_tail_seconds"], preparation_profile=result["profile"])
            else:
                with self.connect() as c:
                    proof = c.execute("SELECT checkpoint_json FROM pipeline_tt_operation WHERE source_key=? AND stage=? AND generation=? AND operation='tt_fetch_status' ORDER BY rowid DESC LIMIT 1", self._key(job)).fetchone()
                if proof is None or obj(proof["checkpoint_json"]) != receipt:
                    raise AdapterError("tt_terminal_checkpoint_missing")
                self._terminal(job, stored, receipt)
            with self.connect() as c:
                c.execute("UPDATE pipeline_tt_claim SET receipt_json=? WHERE source_key=? AND stage=? AND generation=?", (dump(receipt), *self._key(job)))
            return self.reconcile(job)

    def _fail_native(self, job, token, code, known_no_write):
        if self.automatic:
            current = self._native(job)
            if current.status in {"published", "failed", "no_candidate", "canceled", "skipped"}:
                return
            # A publish ID always keeps the task in reconcile-only status.
            status = "failed" if known_no_write else "reconciling" if current.publish_id else "unknown"
            self.store.transition_task(current.id, status, expected_statuses={current.status}, claim_token=token,
                updates={"error_code": code, "unknown_outcome": not known_no_write and not bool(current.publish_id),
                         "claim_phase": "" if known_no_write else "reconcile", "claim_worker": "", "claim_token": "", "lease_expires_at_utc": ""}, event_type="pipeline_node_failed")
            self.service.executor._update_run(current.run_id)
        elif job["family"] == "intake":
            self.store.fail_material_intake(int(job["source_id"]), token, error_code=code, error_message=code, retry_at=None)
        elif job["family"] == "direct_test":
            if job["stage"] == "prepare":
                self.store.fail_direct_test_prepare(int(job["source_id"]), token, error_code=code, error_message=code)
            elif not self._native(job).get("publish_id"):
                self.store.fail_direct_test_publish(int(job["source_id"]), token, error_code=code, error_message=code, publish_was_not_created=known_no_write)
        else:
            current = self._native(job)
            if current.get("publish_id"):
                return
            result = self.store.mark_failed(int(job["source_id"]), token, error_code=code, error_message=code, publish_was_not_created=known_no_write)
            self.service._sync_recurring_queue_if_present(result)

    def fail(self, job, claim, receipt):
        with self._source_lock(job):
            stored, _ = self._stored(job, claim)
            current = self.reconcile(job)
            if current["status"] == "published" and current.get("publish_id"):
                return {"canonical_terminal": True, "canonical_no_write": False, **current}
            with self.connect() as c:
                attempted = c.execute("SELECT 1 FROM pipeline_tt_operation WHERE source_key=? AND operation='tt_initialize_video'", (job["source_key"],)).fetchone()
            safe = attempted is None
            self._fail_native(job, stored["native_token"], str(receipt.get("error_code") or "node_execution_failed"), safe)
            return {"canonical_no_write": safe, **self.reconcile(job)}

    def reconcile(self, job):
        current = self._native_dict(job)
        return {"status": current["status"], "publish_id": current.get("publish_id", ""), "unknown_outcome": bool(current.get("unknown_outcome")),
                "published_at": current.get("published_at_utc", "")}

    def pause(self, job, cutoff):
        from .canonical_holds import pause_row, SCHEMA
        job = dict(job, stage=job.get("stage", "prepare"), generation=job.get("generation", 0))
        with self._source_lock(job), self.connect() as conn:
            conn.execute(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            table, allowed = ("tt_auto_task", {"pending", "reserved", "ready", "retry_wait"}) if self.automatic else (
                ("tt_post_direct_test", {"queued", "ready"}) if job["family"] == "direct_test" else
                ("tt_post_material_intake", {"queued", "retry_wait", "ready"}) if job["family"] == "intake" else ("tt_post_queue", {"scheduled"}))
            result = pause_row(conn, job, cutoff, table=table, identity="id", state="status", allowed=allowed,
                additional_active=lambda row: bool(row.get("claim_token")) or row.get("claim_phase") == "reconcile")
            conn.commit()
            return result

    def dispatch(self, action, payload):
        if not isinstance(payload, dict) or set(payload) - {"job", "claim", "operation", "receipt", "phase", "cutoff"}:
            raise AdapterError("canonical_bridge_payload_invalid")
        job, claim = payload.get("job") or {}, payload.get("claim") or {}
        if action == "reserve": return self.reserve(job, claim)
        if action == "begin-operation": return self.begin_operation(job, claim, payload.get("operation") or {})
        if action == "checkpoint": return self.checkpoint(job, claim, payload.get("phase"), payload.get("receipt") or {})
        if action == "complete": return self.complete(job, payload.get("receipt") or {}, claim)
        if action == "fail": return self.fail(job, claim, payload.get("receipt") or {})
        if action == "reconcile": return self.reconcile(job)
        if action == "pause": return self.pause(job, payload.get("cutoff"))
        raise AdapterError("canonical_bridge_action_invalid")


def handle_tt_dispatch(service, action, payload, *, automatic=False):
    if action == "reconcile-legacy-next":
        if not automatic:
            raise AdapterError("canonical_bridge_action_invalid")
        if (not isinstance(payload, dict) or "worker_id" not in payload
                or set(payload) - {"worker_id", "after_task_id"}):
            raise AdapterError("canonical_bridge_payload_invalid")
        # Reuse the existing authenticated/flagged canonical namespace without
        # constructing a native bridge or changing the deployed service routes.
        from .tt_readback import reconcile_next
        return reconcile_next(service.executor, payload["worker_id"], after_task_id=payload.get("after_task_id", 0))
    with _INIT_LOCK:
        if not hasattr(service, "pipeline_dispatch_bridge"):
            service.pipeline_dispatch_bridge = TTNativeBridge(service, automatic=automatic)
    return service.pipeline_dispatch_bridge.dispatch(action, payload)

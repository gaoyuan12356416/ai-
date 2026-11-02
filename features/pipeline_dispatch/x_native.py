"""X's canonical control plane; actual media and X requests run on nodes.

Imported only by the running OAuth sidecar, never by a root token-refresh
script. Every operation reuses that sidecar's account verification and tokens.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .adapters import AdapterError, obj
from features.x_posts import service as posts
from features.x_posts import duration_policy
from features.x_posts.media_limits import video_byte_limit
from features.x_posts.media_repair import REPAIR_PROFILE
from features.x_posts.random_template import queue_identity, validate_receipt
from features.x_posts.random_template_store import PreparationStore, canonical


class XNativeBridge:
    def __init__(self, runtime):
        self.r = runtime
        self.store = posts.XPostStore(runtime.POST_DB_PATH)
        self.lock = threading.RLock()
        with self.connect() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS pipeline_x_claim (
                source_key TEXT NOT NULL,stage TEXT NOT NULL,generation INTEGER NOT NULL,
                claim_hash TEXT NOT NULL,queue_id INTEGER NOT NULL,log_id INTEGER NOT NULL,
                descriptor_json TEXT NOT NULL,PRIMARY KEY(source_key,stage,generation));
              CREATE TABLE IF NOT EXISTS pipeline_x_operation (
                operation_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,generation INTEGER NOT NULL,
                operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS pipeline_x_checkpoint (
                source_key TEXT NOT NULL,generation INTEGER NOT NULL,phase TEXT NOT NULL,
                receipt_json TEXT NOT NULL,PRIMARY KEY(source_key,generation,phase));
              CREATE TABLE IF NOT EXISTS pipeline_x_artifact (
                queue_id INTEGER PRIMARY KEY,artifact_json TEXT NOT NULL,source_url TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS pipeline_canonical_hold (
                source_key TEXT PRIMARY KEY,cutoff TEXT NOT NULL,reason TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.r.POST_DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def identity(job):
        if (job.get("channel") != "x" or job.get("family") != "queue"
            or job.get("stage") not in {"prepare", "publish"}
            or job.get("source_key") != "x:queue:" + str(job.get("source_id"))):
            raise AdapterError("x_native_family_invalid")

    def key(self, job):
        self.identity(job)
        return job["source_key"], job["stage"], int(job["generation"])

    @staticmethod
    def claim_hash(claim):
        token = str(claim.get("claim_token", ""))
        if len(token) < 32:
            raise AdapterError("x_dispatch_claim_invalid")
        return hashlib.sha256(token.encode()).hexdigest()

    def stored(self, job, claim):
        with self.connect() as c:
            row = c.execute("SELECT * FROM pipeline_x_claim WHERE source_key=? AND stage=? AND generation=?", self.key(job)).fetchone()
            latest = c.execute("SELECT MAX(generation) FROM pipeline_x_claim WHERE source_key=? AND stage=?", self.key(job)[:2]).fetchone()[0]
        if not row or latest != job["generation"] or not secrets.compare_digest(row["claim_hash"], self.claim_hash(claim)):
            raise AdapterError("x_dispatch_claim_conflict")
        return dict(row), obj(row["descriptor_json"])

    def account(self, queue, target=False):
        relay = queue.get("delivery_mode") == posts.PREMIUM_RELAY_REPOST_MODE
        identity = int(queue["account_id"] if target or not relay else queue["relay_account_id"])
        duration = float(queue.get("preflight_duration") or 0)
        premium = not target and (relay or duration > duration_policy.standard_limit(queue))
        with self.connect() as c:
            parent = c.execute("SELECT trigger_source FROM x_post_manual_run WHERE id=?", (queue.get("manual_run_id"),)).fetchone()
        actor = self.r.AUTO_TEMPLATE_ACTOR if parent and parent[0] == "auto_template" else self.r.CANARY_ACTOR
        verified = self.r.verify_account(identity, actor, "all", only_refresh_required=not premium,
                                         preserve_transient_status=True, require_publish_approved=True)
        if verified.get("status") != "active" or verified.get("publish_approved") is not True or int(verified.get("id", 0)) != identity:
            raise AdapterError("x_account_not_publishable")
        if int(queue.get("account_drama_language_frozen") or 0) and not self.r.same_drama_language(verified.get("drama_language"), queue.get("account_drama_language")):
            raise AdapterError("x_post_account_language_mismatch")
        if premium and (not verified.get("long_video_publish_eligible") or verified.get("protected") is not False):
            raise AdapterError("x_post_premium_relay_unavailable")
        if queue.get("manual_run_id"):
            if queue.get("status") == "publishing":
                self.r._personal_owner_guard(queue["manual_run_id"])
            else:
                self.r._personal_run_guard(queue["manual_run_id"], verified)
        return identity, actor, verified

    def _parent(self, queue, log):
        parent_ids = [queue.get(k) for k in ("run_id", "catchup_run_id", "schedule_run_id", "manual_run_id") if queue.get(k)]
        if len(parent_ids) != 1:
            raise AdapterError("x_frozen_parent_invalid")
        if queue.get("manual_run_id"):
            with self.connect() as c:
                parent = c.execute("SELECT * FROM x_post_manual_run WHERE id=?", (queue["manual_run_id"],)).fetchone()
            own_inflight = (parent and parent["status"] == "needs_review" and not parent["error_code"]
                and queue["status"] == "publishing" and log["status"] in ("post_creating", "repost_creating")
                and not log["unknown_outcome"])
            if not parent or parent["trigger_source"] not in ("manual", "auto_template"):
                raise AdapterError("x_manual_parent_not_publishable")
            # Normal manual parents aggregate every sibling's state. A sibling
            # post_creating/unknown result must not fence unrelated accounts.
            # Personal-task permission and cancellation are checked by the
            # original owner/run guards; auto parents retain recovery fencing.
            if parent["trigger_source"] == "auto_template" and parent["status"] not in ("running", "completed") and not own_inflight:
                raise AdapterError("x_manual_parent_not_publishable")
            if int(queue["account_id"]) not in json.loads(parent["account_ids_json"]):
                raise AdapterError("x_parent_account_mismatch")
            if parent["trigger_source"] == "auto_template" and log["status"] == "reserved":
                self.store.assert_auto_template_publishable(queue["id"], log["id"])

    def _links(self, queue, log):
        if log.get("post_text") and log.get("long_url") and log.get("short_url"):
            return log
        duration = float(queue.get("preflight_duration") or 0)
        if duration < 0 or (duration == 0 and queue.get("media_validation_mode") != "preflight"):
            raise AdapterError("x_media_preflight_not_ready")
        short = posts._build_short_url(self.r.POST_SHORT_BASE_URL, log["id"])
        if queue.get("source_type") == "drama":
            body = posts.build_drama_episode_post_text(short, queue.get("episode_number"), queue.get("drama_name"), queue["description"], queue.get("body_template"))
        else:
            body = posts.build_post_text(short, queue.get("drama_name"), queue["description"], queue.get("body_template"), copy_contract_version=int(queue.get("copy_contract_version", 1)))
        if os.environ.get("X_POST_ATTRIBUTION_V2_ENABLED", "0") == "1":
            from features.x_posts.attribution import load_frozen, freeze, resolve_via_loopback, build_url
            attribution = load_frozen(self.r.POST_DB_PATH, log["id"])
            if attribution is None:
                attribution = freeze(self.r.POST_DB_PATH, log, resolve_via_loopback(queue["id"], log["id"]))
            long_url = build_url(attribution, log)
        else:
            macros = {key: queue[key] for key in ("material_language", "drama_name", "tag", "page_name", "page_id", "material_name", "material_id", "content_id")}
            macros.update(username=queue["account_username"], timestamp=int(time.time()), log_id=log["id"], queue_id=queue["id"], video_duration_seconds=duration)
            long_url = posts.build_w2a_url(macros, duration_policy_version=duration_policy.version(queue))
        log = self.store.prepare_log(log["id"], long_url, short, body)
        self.r.preflight_post_storage_request()
        posts.write_short_redirect(self.r.POST_PUBLIC_ROOT, log["id"], long_url, durable_storage={"mount_root": self.r.POST_STORAGE_MOUNT_ROOT, "storage_root": self.r.POST_STORAGE_ROOT})
        return log

    def artifact(self, queue, record):
        if queue.get("random_template_enabled"):
            if not record or record["status"] != "ready":
                raise AdapterError("x_random_preparation_required")
            result = record["result"]
            validate_receipt(record["request"], result)
            return {"url": result["output_url"], "sha256": result["output_sha256"], "size": result["output_size"], "mime": "video/mp4", "duration_seconds": result["probe"]["duration"], "profile": result["profile"], "recipe": result["recipe"]}
        with self.connect() as c:
            saved = c.execute("SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?", (queue["id"],)).fetchone()
        if saved:
            return obj(saved[0])
        if not queue.get("preflight_sha256") or not queue.get("preflight_size") or not queue.get("preflight_duration"):
            raise AdapterError("x_media_preflight_not_ready")
        return {"url": queue["material_url"], "sha256": queue["preflight_sha256"], "size": queue["preflight_size"], "mime": "video/mp4", "duration_seconds": queue.get("preflight_duration", 0), "profile": queue.get("media_repair_profile", "source")}

    def reserve(self, job, claim):
        with self.lock:
            key = self.key(job)
            with self.connect() as c:
                if c.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone():
                    raise AdapterError("canonical_backlog_paused")
                previous = c.execute("SELECT 1 FROM pipeline_x_claim WHERE source_key=? AND stage=? AND generation=?", key).fetchone()
            if previous:
                return self.stored(job, claim)[1]
            queue = self.store.get_queue(int(job["source_id"]))
            # Deferred/routed inputs are admitted only once the existing route
            # planner has frozen the original media and account identity.
            pending_route = queue.get("delivery_mode") == "duration_pending"
            log = {"id": 0, "status": "reserved", "attempt_count": 0, "unknown_outcome": 0} if pending_route and job["stage"] == "prepare" else self.store.reserve_log(queue["id"])
            if log["status"] != "reserved" or log["attempt_count"] or log["unknown_outcome"]:
                raise AdapterError("x_existing_attempt_requires_reconciliation")
            self._parent(queue, log)
            source_id, _, source = self.account(queue, target=pending_route)
            prep = PreparationStore(self.r.POST_DB_PATH)
            frozen = prep.get(queue["id"]) if queue.get("random_template_enabled") else None
            if job["stage"] == "prepare":
                if queue.get("media_validation_mode") == "deferred" or not queue.get("random_template_enabled"):
                    request = {"source_url": queue["material_url"], "source_sha256": queue.get("preflight_sha256", ""),
                               "source_size": queue.get("preflight_size", 0), "identity_mode": "source_to_fingerprint",
                               "max_media_bytes": min(self.r.POST_MAX_MEDIA_BYTES, video_byte_limit(pending_route or bool(source.get("long_video_publish_eligible")))),
                               "max_duration_seconds": 14400 if pending_route or source.get("long_video_publish_eligible") else duration_policy.standard_limit(queue)}
                    repair_profile = "x-h264-libx264-720-duration-policy-v1" if job.get("executor_backend") == "cpu" else REPAIR_PROFILE
                    request["repair_request"] = {"job_key": hashlib.sha256(canonical({"queue": queue_identity(queue), "profile": repair_profile}).encode()).hexdigest(),
                        "material_id": str(queue["material_id"]) if str(queue["material_id"]).isdigit() else str(queue["id"]),
                        "pool_item_id": int(queue["pool_item_id"] or queue["id"]), "profile": repair_profile,
                        "duration_policy": "premium" if request["max_duration_seconds"] == 14400 else duration_policy.repair_policy(queue)}
                    if not queue.get("random_template_enabled"):
                        with self.connect() as c:
                            saved = c.execute("SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?", (queue["id"],)).fetchone()
                        if saved:
                            # A completed source probe/repair is durable even
                            # when the coordinator lost its final receipt.
                            request["prepared_artifact"] = obj(saved[0])
                    if queue.get("random_template_enabled"):
                        profile = "x-random-overlay-libx264-720x1280-v1" if job.get("executor_backend") == "cpu" else "x-random-overlay-h264-720x1280-v1"
                        request["random_request"] = frozen["request"] if frozen else {
                            "job_key": "x-random-v1-" + hashlib.sha256(canonical(queue_identity(queue)).encode()).hexdigest(),
                            "queue_id": queue["id"], "content_id": str(queue["content_id"]), "profile": profile,
                            "asset_manifest_sha256": os.environ.get("X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256", ""),
                            "max_duration_seconds": request["max_duration_seconds"], "duration_policy_version": duration_policy.version(queue)}
                    descriptor = {"kind": "x_preflight", "request": request}
                else:
                    if frozen:
                        request = frozen["request"]
                    else:
                        profile = "x-random-overlay-libx264-720x1280-v1" if job.get("executor_backend") == "cpu" else "x-random-overlay-h264-720x1280-v1"
                        request = {"job_key": "x-random-v1-" + hashlib.sha256(canonical({"queue": queue_identity(queue), "sha256": queue["preflight_sha256"]}).encode()).hexdigest(),
                                   "queue_id": queue["id"], "content_id": str(queue["content_id"]), "source_url": queue["material_url"],
                                   "source_sha256": queue["preflight_sha256"], "source_size": queue["preflight_size"], "source_duration": float(queue["preflight_duration"]),
                                   "profile": profile, "asset_manifest_sha256": os.environ.get("X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256", ""),
                                   "max_duration_seconds": 14400 if source.get("long_video_publish_eligible") else duration_policy.standard_limit(queue),
                                   "duration_policy_version": duration_policy.version(queue)}
                        prep.freeze(queue["id"], request)
                    descriptor = {"kind": "x_random", "request": request}
            else:
                artifact = self.artifact(queue, frozen)
                media_limit = posts.DEFAULT_MAX_GIF_BYTES if artifact["mime"] == "image/gif" else posts.DEFAULT_MAX_IMAGE_BYTES if artifact["mime"].startswith("image/") else video_byte_limit(bool(source.get("long_video_publish_eligible")))
                if type(artifact.get("size")) is not int or not 0 < artifact["size"] <= min(media_limit, self.r.POST_MAX_MEDIA_BYTES):
                    raise AdapterError("x_artifact_exceeds_account_limit")
                log = self._links(queue, log)
                target_id, _, target = self.account(queue, target=True)
                descriptor = {"kind": "x_publish", "artifact": artifact,
                              "source_account_resource": "x:account:" + str(source_id),
                              "request": {"body": log["post_text"], "delivery_mode": queue.get("delivery_mode", "direct"),
                                          "target_x_user_id": target["x_user_id"], "media_category": posts.image_media_category(artifact["mime"]) if artifact["mime"].startswith("image/") else "amplify_video" if float(artifact["duration_seconds"]) > duration_policy.standard_limit(queue) else "tweet_video"}}
            descriptor["account_resource"] = "x:account:" + str(queue["account_id"])
            render_request = descriptor.get("request", {}).get("random_request", descriptor.get("request", {}))
            if str(render_request.get("profile", "")).startswith("x-random-overlay-"):
                descriptor["required_capabilities"] = ["prepare:x", "backend:" + ("cpu" if "libx264" in render_request["profile"] else "gpu")]
            elif descriptor["kind"] == "x_preflight":
                descriptor["required_capabilities"] = ["prepare:x", "backend:" + ("cpu" if "libx264" in repair_profile else "gpu")]
            descriptor["source_key"] = job["source_key"]
            with self.connect() as c:
                c.execute("INSERT INTO pipeline_x_claim VALUES(?,?,?,?,?,?,?)", (*key, self.claim_hash(claim), queue["id"], log["id"], canonical(descriptor)))
            return descriptor

    def begin_operation(self, job, claim, operation):
        with self.lock:
            record, descriptor = self.stored(job, claim)
            if job["stage"] != "publish":
                raise AdapterError("x_publish_phase_required")
            queue = self.store.get_queue(record["queue_id"])
            log = self.store.get_log(record["log_id"])
            details = operation.get("details", {})
            method, path = details.get("method"), details.get("path", "")
            if method not in ("POST", "GET") or not (path == "/2/tweets" or re.fullmatch(r"/2/media/upload(?:/initialize|/[A-Za-z0-9_.:-]+/(?:append|finalize)|\?.+)?", path) or re.fullmatch(r"/2/users/[0-9]+/retweets", path)):
                raise AdapterError("x_operation_route_denied")
            target = "/retweets" in path
            identity, actor, account = self.account(queue, target=target)
            if operation.get("account_resource") != "x:account:" + str(identity):
                raise AdapterError("x_operation_account_changed")
            self._parent(queue, log)
            if path == "/2/media/upload/initialize":
                if method != "POST" or log["status"] != "reserved" or log["attempt_count"] or log["unknown_outcome"]:
                    raise AdapterError("x_upload_already_started")
                expected = json.dumps({"media_type": descriptor["artifact"]["mime"], "total_bytes": descriptor["artifact"]["size"], "media_category": descriptor["request"]["media_category"]}, separators=(",", ":")).encode()
                if details.get("body_sha256") != hashlib.sha256(expected).hexdigest():
                    raise AdapterError("x_upload_identity_changed")
                self.store.mark_publishing(log["id"])
            elif path == "/2/tweets":
                if method != "POST" or log["status"] != "post_creating" or log["unknown_outcome"] or not log["x_media_id"]:
                    raise AdapterError("x_media_receipt_required")
                expected = json.dumps({"text": log["post_text"], "media": {"media_ids": [log["x_media_id"]]}}, ensure_ascii=False, separators=(",", ":")).encode()
                if details.get("body_sha256") != hashlib.sha256(expected).hexdigest():
                    raise AdapterError("x_post_body_changed")
            elif target:
                if method != "POST" or path != "/2/users/%s/retweets" % account["x_user_id"] or log["status"] != "source_published" or log["unknown_outcome"]:
                    raise AdapterError("x_repost_source_not_confirmed")
                expected = json.dumps({"tweet_id": log["x_post_id"]}, separators=(",", ":")).encode()
                if details.get("body_sha256") != hashlib.sha256(expected).hexdigest():
                    raise AdapterError("x_repost_body_changed")
                self.store.mark_reposting(queue["id"])
            else:
                if log["status"] != "media_uploading" or log["unknown_outcome"]:
                    raise AdapterError("x_upload_state_conflict")
                with self.connect() as c:
                    initialized = c.execute("SELECT receipt_json FROM pipeline_x_checkpoint WHERE source_key=? AND generation=? AND phase='x_media_initialized'", (job["source_key"], job["generation"])).fetchone()
                media_id = obj(initialized[0]).get("media_id") if initialized else None
                parsed_path = urlsplit(path)
                exact_media = parsed_path.path.startswith("/2/media/upload/" + str(media_id) + "/") if method == "POST" else parsed_path.path == "/2/media/upload" and parse_qs(parsed_path.query).get("media_id") == [media_id]
                if not media_id or not exact_media:
                    raise AdapterError("x_media_session_changed")
            with self.connect() as c:
                if c.execute("SELECT 1 FROM pipeline_x_operation WHERE operation_id=?", (operation["operation_id"],)).fetchone():
                    raise AdapterError("operation_already_started")
                c.execute("INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)", (operation["operation_id"], job["source_key"], job["generation"], operation["operation"], operation["request_sha256"], posts.utc_now()))
            with self.r.publish_credentials(identity, actor, "all") as (_, token):
                return {"credentials": {"access_token": token}}

    def checkpoint(self, job, claim, phase, receipt):
        with self.lock:
            record, descriptor = self.stored(job, claim)
            key = job["source_key"], job["generation"], phase
            encoded = canonical(receipt)
            with self.connect() as c:
                old = c.execute("SELECT receipt_json FROM pipeline_x_checkpoint WHERE source_key=? AND generation=? AND phase=?", key).fetchone()
            if old:
                if old[0] != encoded:
                    raise AdapterError("x_checkpoint_conflict")
                return {"accepted": True}
            queue = self.store.get_queue(record["queue_id"])
            log = self.store.get_log(record["log_id"]) if record["log_id"] else None
            if (phase == "x_preflight") != (job["stage"] == "prepare"):
                raise AdapterError("x_checkpoint_stage_invalid")
            if phase == "x_preflight":
                self.preflight(queue, descriptor, receipt)
            elif phase == "x_media_uploaded":
                with self.connect() as c:
                    initialized = c.execute("SELECT receipt_json FROM pipeline_x_checkpoint WHERE source_key=? AND generation=? AND phase='x_media_initialized'", (job["source_key"], job["generation"])).fetchone()
                if not initialized or obj(initialized[0]).get("media_id") != receipt.get("media_id"):
                    raise AdapterError("x_media_session_changed")
                if receipt.get("artifact_sha256") != descriptor["artifact"]["sha256"]:
                    raise AdapterError("x_artifact_changed")
                if log["status"] != "post_creating":
                    self.store.mark_media_uploaded(log["id"], receipt["media_id"])
                elif log["x_media_id"] != receipt["media_id"]:
                    raise AdapterError("x_media_identity_conflict")
            elif phase == "x_source_published":
                with self.connect() as c:
                    intent = c.execute("SELECT 1 FROM pipeline_x_operation WHERE source_key=? AND generation=? AND operation='x:POST:/2/tweets'", (job["source_key"], job["generation"])).fetchone()
                if not intent or not re.fullmatch(r"[0-9]{1,32}", str(receipt.get("post_id", ""))) or receipt.get("media_id") != log["x_media_id"]:
                    raise AdapterError("x_post_receipt_invalid")
                source_username = queue["relay_account_username"] if queue.get("delivery_mode") == posts.PREMIUM_RELAY_REPOST_MODE else queue["account_username"]
                post_url = "https://x.com/%s/status/%s" % (source_username, receipt["post_id"])
                fn = self.store.mark_relay_source_published if queue.get("delivery_mode") == posts.PREMIUM_RELAY_REPOST_MODE else self.store.mark_published
                fn(log["id"], receipt["media_id"], receipt["post_id"], post_url)
            elif phase == "x_target_reposted":
                with self.connect() as c:
                    intent = c.execute("SELECT 1 FROM pipeline_x_operation WHERE source_key=? AND generation=? AND operation LIKE 'x:POST:/2/users/%/retweets'", (job["source_key"], job["generation"])).fetchone()
                if not intent or receipt.get("post_id") != log["x_post_id"]:
                    raise AdapterError("x_repost_receipt_invalid")
                self.store.mark_reposted(queue["id"], receipt.get("repost_id", ""))
            elif phase == "x_media_initialized":
                with self.connect() as c:
                    intent = c.execute("SELECT 1 FROM pipeline_x_operation WHERE operation_id=? AND source_key=? AND generation=? AND operation='x:POST:/2/media/upload/initialize'", (receipt.get("operation_id"), job["source_key"], job["generation"])).fetchone()
                if not intent or log["status"] != "media_uploading" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(receipt.get("media_id", ""))):
                    raise AdapterError("x_media_initialize_receipt_invalid")
            else:
                raise AdapterError("x_checkpoint_phase_invalid")
            with self.connect() as c:
                c.execute("INSERT INTO pipeline_x_checkpoint VALUES(?,?,?,?)", (*key, encoded))
            return {"accepted": True}

    def preflight(self, queue, descriptor, receipt):
        artifact = receipt.get("artifact", {})
        if descriptor["kind"] != "x_preflight" or receipt.get("source_url") != descriptor["request"]["source_url"]:
            raise AdapterError("x_preflight_source_changed")
        request = descriptor["request"]
        duration = float(artifact.get("duration_seconds") or 0)
        mime = str(artifact.get("mime") or "video/mp4")
        is_image = mime in {"image/jpeg", "image/png", "image/gif"}
        max_bytes = min(request["max_media_bytes"], posts.DEFAULT_MAX_GIF_BYTES if mime == "image/gif" else posts.DEFAULT_MAX_IMAGE_BYTES) if is_image else request["max_media_bytes"]
        parsed = urlsplit(str(artifact.get("url", "")))
        if (not math.isfinite(duration) or (duration != 0 if is_image else duration <= 0)
            or duration > request["max_duration_seconds"] or (not is_image and mime != "video/mp4")
            or not re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("sha256", "")))
            or type(artifact.get("size")) is not int or not 0 < artifact["size"] <= max_bytes
            or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)
            or not posts._allowed_host(parsed.hostname, self.r.POST_MEDIA_ALLOWED_HOSTS)):
            raise AdapterError("x_preflight_invalid")
        source_sha = receipt.get("source_sha256", artifact["sha256"])
        source_size = receipt.get("source_size", artifact["size"])
        if not re.fullmatch(r"[a-f0-9]{64}", str(source_sha)) or type(source_size) is not int or source_size <= 0:
            raise AdapterError("x_preflight_source_invalid")
        if request.get("source_sha256") and (request["source_sha256"] != source_sha or request["source_size"] != source_size):
            raise AdapterError("x_preflight_identity_changed")
        repaired = receipt.get("repair")
        if repaired:
            repair_request = request.get("repair_request", {})
            expected = {"job_key": repair_request.get("job_key"), "profile": repair_request.get("profile"),
                        "output_url": artifact["url"], "output_sha256": artifact["sha256"], "output_size": artifact["size"]}
            repair_duration = float(repaired.get("probe", {}).get("duration", -1))
            if is_image or not math.isfinite(repair_duration) or any(repaired.get(k) != v for k, v in expected.items()) or abs(repair_duration - duration) > .05:
                raise AdapterError("x_repair_receipt_invalid")
        elif source_sha != artifact["sha256"] or source_size != artifact["size"]:
            raise AdapterError("x_unverified_artifact_change")
        with self.connect() as c:
            parent = c.execute("SELECT trigger_source FROM x_post_manual_run WHERE id=?", (queue.get("manual_run_id"),)).fetchone()
        if parent and parent[0] == "auto_template" and (is_image or duration > self.r.AUTO_TEMPLATE_MAX_DURATION_SECONDS):
            raise AdapterError("x_post_auto_template_duration_exceeded")
        if is_image and request.get("random_request"):
            raise AdapterError("x_random_requires_video")
        evidence = {"material_url": queue["material_url"], "preflight_sha256": artifact["sha256"], "preflight_size": artifact["size"],
                    "preflight_duration": duration, "preflight_width": artifact.get("width", 0), "preflight_height": artifact.get("height", 0)}
        if queue.get("delivery_mode") == "duration_pending":
            _, _, target = self.account(queue, target=True)
            eligible = bool(target.get("long_video_publish_eligible") and target.get("protected") is False)
            relays = self.r._premium_relay_accounts(queue["run_date"], refresh=True, drama_language=queue.get("account_drama_language")) if duration > duration_policy.standard_limit(queue) and not eligible else []
            self.store.resolve_drama_duration_route(queue["id"], evidence, eligible, relays)
        elif queue.get("media_validation_mode") == "deferred":
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                attempt = c.execute("SELECT attempt_count,unknown_outcome,status FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchone()
                if attempt and (attempt[0] or attempt[1] or attempt[2] != "reserved"):
                    raise AdapterError("x_preflight_publish_already_started")
                c.execute("UPDATE x_post_queue SET media_validation_mode='preflight',preflight_sha256=?,preflight_size=?,preflight_duration=?,updated_at=? WHERE id=? AND media_validation_mode='deferred'", (artifact["sha256"], artifact["size"], duration, posts.utc_now(), queue["id"]))
        with self.connect() as c:
            old = c.execute("SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?", (queue["id"],)).fetchone()
            if old and any(obj(old[0]).get(k) != artifact.get(k) for k in ("url", "sha256", "size", "mime", "duration_seconds", "profile")):
                raise AdapterError("x_artifact_identity_changed")
            c.execute("INSERT INTO pipeline_x_artifact VALUES(?,?,?) ON CONFLICT(queue_id) DO NOTHING", (queue["id"], canonical(artifact), receipt["source_url"]))
        if descriptor["request"].get("random_request"):
            random_request = dict(descriptor["request"]["random_request"])
            random_request.update(source_url=artifact["url"], source_sha256=artifact["sha256"], source_size=artifact["size"], source_duration=duration)
            PreparationStore(self.r.POST_DB_PATH).freeze(queue["id"], random_request)

    def complete(self, job, receipt, claim):
        with self.lock:
            record, descriptor = self.stored(job, claim)
            if job["stage"] == "prepare":
                result = receipt.get("preparation", {})
                if descriptor["kind"] == "x_random" or descriptor.get("request", {}).get("random_request"):
                    request = PreparationStore(self.r.POST_DB_PATH).get(record["queue_id"])["request"]
                    validate_receipt(request, result)
                    expected_artifact = {"url": result["output_url"], "sha256": result["output_sha256"], "size": result["output_size"],
                        "mime": "video/mp4", "duration_seconds": result["probe"]["duration"], "profile": result["profile"], "recipe": result["recipe"]}
                    if any(receipt["artifact"].get(k) != value for k, value in expected_artifact.items()):
                        raise AdapterError("x_artifact_identity_changed")
                    PreparationStore(self.r.POST_DB_PATH).complete(record["queue_id"], result)
                elif descriptor["kind"] == "x_preflight":
                    with self.connect() as c:
                        saved = c.execute("SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?", (record["queue_id"],)).fetchone()
                    if not saved or any(obj(saved[0]).get(k) != receipt["artifact"].get(k) for k in ("url", "sha256", "size", "mime", "duration_seconds", "profile")):
                        raise AdapterError("x_preflight_checkpoint_required")
                return {"status": "ready", "artifact": receipt["artifact"]}
            result = self.store.get_log(record["log_id"])
            if result["status"] != "published" or result["unknown_outcome"]:
                raise AdapterError("x_final_receipt_not_confirmed")
            return {"status": "published", "post_id": result["x_post_id"]}

    def fail(self, job, claim, receipt):
        record, _ = self.stored(job, claim)
        if not record["log_id"]:
            return {"canonical_no_write": True}
        log = self.store.get_log(record["log_id"])
        if log["status"] == "published":
            return {"status": "published", "canonical_terminal": True}
        code = str(receipt.get("error_code", "pipeline_execution_failed"))[:120]
        unknown = bool(receipt.get("unknown_outcome")) or log["status"] in ("post_creating", "repost_creating")
        if unknown and log["status"] in ("post_creating", "repost_creating"):
            # Preserve the state required to accept this exact attempt's late
            # receipt. It remains quarantined and cannot receive new permits.
            with self.connect() as c:
                c.execute("UPDATE x_post_publish_log SET unknown_outcome=1,error_code=?,error_message=?,updated_at=? WHERE id=?", (code, code, posts.utc_now(), log["id"]))
            return {"canonical_no_write": False}
        if log["status"] == "reserved":
            self.store.mark_failed_if_reserved(log["id"], code, code)
        else:
            self.store.mark_failed(log["id"], code, code, unknown)
        return {"canonical_no_write": not unknown}

    def reconcile(self, job):
        self.identity(job)
        queue = self.store.get_queue(int(job["source_id"]))
        with self.connect() as c:
            log = c.execute("SELECT status,x_post_id,unknown_outcome FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchone()
        return dict(log) if log else {"status": queue["status"]}

    def pause(self, job, cutoff):
        # Cutover uses an unclaimed canonical projection. It has no execution
        # generation; only execution operations may require a claim key.
        self.identity(job)
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT q.status,COALESCE(l.attempt_count,0) attempts,COALESCE(l.unknown_outcome,0) uncertain,COALESCE(l.status,'') log_status FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.id=?", (int(job["source_id"]),)).fetchone()
            if not row or row["status"] != "queued" or row["attempts"] or row["uncertain"] or row["log_status"] not in ("", "reserved") or c.execute("SELECT 1 FROM pipeline_x_claim WHERE source_key=?", (job["source_key"],)).fetchone():
                raise AdapterError("canonical_task_not_untouched")
            previous = c.execute("SELECT cutoff FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone()
            if previous and previous[0] != cutoff:
                raise AdapterError("canonical_hold_conflict")
            c.execute("INSERT OR IGNORE INTO pipeline_canonical_hold VALUES(?,?,'cutover_overdue')", (job["source_key"], cutoff))
        return {"held": True, "paused": True, "source_key": job["source_key"], "cutoff": cutoff}

    def dispatch(self, action, payload):
        job, claim = payload.get("job", {}), payload.get("claim", {})
        if action == "reserve": return self.reserve(job, claim)
        if action == "begin-operation": return self.begin_operation(job, claim, payload.get("operation", {}))
        if action == "checkpoint": return self.checkpoint(job, claim, payload.get("phase"), payload.get("receipt", {}))
        if action == "complete": return self.complete(job, payload.get("receipt", {}), claim)
        if action == "fail": return self.fail(job, claim, payload.get("receipt", {}))
        if action == "reconcile": return self.reconcile(job)
        if action == "pause": return self.pause(job, payload.get("cutoff"))
        raise AdapterError("x_dispatch_action_invalid")


_BRIDGE = None
_INIT_LOCK = threading.Lock()


def handle(handler, runtime, path):
    global _BRIDGE
    expected = os.environ.get("PIPELINE_X_CANONICAL_TOKEN", "")
    supplied = handler.headers.get("Authorization", "").removeprefix("Bearer ")
    if os.environ.get("PIPELINE_X_CANONICAL_BRIDGE_ENABLED") != "1" or len(expected) < 32 or not secrets.compare_digest(supplied, expected):
        handler.send_json(403, {"code": "pipeline_x_bridge_disabled_or_unauthorized"})
        return
    try:
        payload = handler.read_json(2 * 1024 * 1024)
        with _INIT_LOCK:
            if _BRIDGE is None:
                _BRIDGE = XNativeBridge(runtime)
        if payload.get("job", {}).get("family") == "youtube_share":
            from .x_share_native import dispatch_share
            result = dispatch_share(runtime, path.rsplit("/", 1)[-1], payload)
        else:
            result = _BRIDGE.dispatch(path.rsplit("/", 1)[-1], payload)
        handler.send_json(200, result)
    except Exception as exc:
        handler.send_json(409, {"code": getattr(exc, "code", "x_dispatch_failed")})

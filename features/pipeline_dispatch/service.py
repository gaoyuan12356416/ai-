"""Loopback authority for authenticated worker nodes and the UI gateway."""
import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import DispatchStore, DispatchError, dumps


class Coordinator:
    def __init__(self, store, registry, *, internal_token, master_key, owner_resolver=None):
        if len(internal_token) < 32 or len(master_key) < 32:
            raise ValueError("pipeline authority credentials missing")
        self.store, self.registry = store, registry
        self.owner_resolver = owner_resolver
        self.internal_token, self.master_key = internal_token, master_key.encode()
        self.stopping = threading.Event()
        self._last_capacity = 0
        self._reconcile_cursor = 0
        with contextlib.closing(self.store.connect()) as c:
            c.execute("CREATE TABLE IF NOT EXISTS pipeline_collector_state (id INTEGER PRIMARY KEY CHECK(id=1), value_json TEXT NOT NULL)")
            state = c.execute("SELECT value_json FROM pipeline_collector_state WHERE id=1").fetchone()
        if state and hasattr(registry, "restore_cursors"):
            registry.restore_cursors(json.loads(state[0]))

    def recover_prepared(self, job):
        """A native committed artifact survives loss of the coordinator ACK."""
        if job["stage"] != "prepare":
            return False
        observed = self.registry.reconcile(job)
        proof = observed.get("canonical_receipt", {})
        offer = observed.get("next_offer") or {}
        artifact = offer.get("artifact") or {}
        if not artifact.get("sha256"):
            artifact = (observed.get("result") or {}).get("artifact") or {}
        qualified = (isinstance(artifact, dict)
                     and isinstance(artifact.get("size"), int) and not isinstance(artifact.get("size"), bool)
                     and artifact["size"] > 0 and isinstance(artifact.get("sha256"), str)
                     and re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
                     and str(artifact.get("url", "")).startswith("https://"))
        if (proof.get("source_key") != job["source_key"] or offer.get("source_key") != job["source_key"]
                or observed.get("metadata_only") or observed.get("definitive_failure") or not qualified
                or not (observed.get("dispatch_completed") or offer.get("stage") == "publish"
                        or (observed.get("result") or {}).get("preparation_completed") is True)):
            return False
        frozen = job.get("payload", {})
        expected = frozen.get("expected_profile") or frozen.get("request", {}).get("expected_profile")
        if expected and artifact.get("profile") != expected:
            raise DispatchError("canonical_artifact_profile_changed")
        result = {"artifact": artifact, "canonical_receipt": dumps(proof), "dispatch_completed": True}
        receipt_id = "native-prepared-%s-%s" % (job["id"], job["generation"])
        self.store.complete(job, receipt_id, result, canonical_verified=True)
        self.store.offer(offer)
        return True

    def node_key(self, node_id, version=1):
        return base64.urlsafe_b64encode(hmac.new(self.master_key, (node_id + ":" + str(version)).encode(), hashlib.sha256).digest()).decode()

    def authenticate(self, token):
        if secrets.compare_digest(token, self.internal_token):
            return {"internal": True}
        node = self.store.authenticate_node(token)
        if not node:
            raise DispatchError("authentication_required", status=401)
        return node

    def scan(self):
        offers = self.registry.scan(limit=200)
        if self.owner_resolver:
            offers = self.owner_resolver.resolve(offers)
        for offer in offers:
            projected = self.store.offer(offer)
            if projected and offer.get("publish_id") and (offer.get("status") == "published" or offer.get("video_published")):
                self.store.record_publication(projected["id"], {"source_key": offer["source_key"], "publish_id": offer["publish_id"], "published_at": offer.get("finished_at"), "video_published_at": offer.get("video_published_at"), "historical": True}, canonical_verified=True)
        collection = self.registry.collection_status() if hasattr(self.registry, "collection_status") else {}
        for channel, family in self.registry.adapters:
            progress = collection.get(channel + ":" + family, {})
            state = "collecting" if progress.get("recent_complete") is False else "backfilling" if progress.get("history_complete") is False else "ready"
            self.store.source_health(channel, family, self.registry.errors.get(channel + ":" + family, ""), state)
        self.store.expire()
        # Read back the owning ledger for submitted platform work. A node loss
        # never causes another publish claim, even when the lease is expired.
        pending = self.store.reconciliation_jobs(limit=100, after_id=self._reconcile_cursor)
        self._reconcile_cursor = pending[-1]["id"] if pending else 0
        for job in pending:
            try:
                result = self.registry.reconcile(job)
                proof = result.get("canonical_receipt", {})
                if proof.get("publish_id") and (result.get("published") or proof.get("video_published")):
                    self.store.record_publication(job["id"], proof, canonical_verified=True)
                self.store.reconcile_native(job["id"], result)
                if result.get("continuation"):
                    self.store.rearm_native_schedule(job, result)
            except Exception:
                pass
        if hasattr(self.registry, "export_cursors"):
            with self.store.transaction() as c:
                c.execute("INSERT INTO pipeline_collector_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json", (dumps(self.registry.export_cursors()),))
        if time.monotonic() - self._last_capacity >= 60:
            self.store.adapt_capacity()
            self._last_capacity = time.monotonic()

    def run_collector(self):
        while not self.stopping.is_set():
            try:
                self.scan()
            except Exception:
                # Never expose source URLs, tokens, or raw driver exception text.
                print("pipeline_collection_failed", flush=True)
            self.stopping.wait(15)

    def route(self, path, payload, auth):
        if path.startswith("/internal/pipeline-monitor/"):
            if not auth.get("internal"):
                raise DispatchError("internal_gateway_required", status=403)
            actor = payload.get("actor", {})
            if path.endswith("/query"):
                from .views import query
                return query(self.store, payload.get("view"), actor, payload.get("filters", {}))
            if path.endswith("/capacity"):
                from .views import capacity_dto, invalidate_cache
                result = self.store.set_capacity(payload, actor)
                invalidate_cache(self.store)
                return {"ok": True, "capacity": capacity_dto(result)}
            if path.endswith("/enroll") or path.endswith("/nodes/enroll"):
                result = self.store.create_enrollment(payload.get("label"), actor)
                return dict(result, enrollment_url="/internal/pipeline/nodes/enroll")
            raise DispatchError("not_found", status=404)
        prefix = "/internal/pipeline/"
        if not path.startswith(prefix):
            raise DispatchError("not_found", status=404)
        action = path[len(prefix):]
        if action.startswith("admin/"):
            if not auth.get("internal"):
                raise DispatchError("internal_gateway_required", status=403)
            from .cutover import Cutover
            controller = Cutover(self)
            if action == "admin/stop-claims":
                self.store.channel_enabled(payload.get("channel"), False)
                return {"accepted": True, "enabled": False}
            if action == "admin/cutover-plan":
                return controller.plan(payload.get("channel"), payload.get("cutoff"), payload.get("grandfathered_source_keys", []))
            if action == "admin/cutover-apply":
                return controller.apply(payload.get("plan_id"), payload.get("sha256"))
            if action == "admin/cutover-enable":
                return controller.enable(payload.get("plan_id"), payload.get("sha256"))
            raise DispatchError("not_found", status=404)
        node_id = payload.get("node_id")
        if not auth.get("internal") and auth.get("node_id") != node_id:
            raise DispatchError("node_identity_mismatch", status=403)
        delivery_id = payload.get("checkpoint_id") if action == "jobs/checkpoint" else payload.get("receipt_id") if action in ("jobs/complete", "jobs/fail") else None
        if delivery_id:
            delivered = self.store.delivered(payload, delivery_id, payload)
            if delivered:
                return delivered
            if action in ("jobs/complete", "jobs/fail"):
                historical = self.store.historical_receipt_claim(payload)
                if historical:
                    observed = self.registry.reconcile(historical)
                    proof = observed.get("canonical_receipt", {})
                    if historical["stage"] == "prepare":
                        offer = observed.get("next_offer") or {}
                        artifact = offer.get("artifact") or {}
                        if not artifact.get("sha256"):
                            artifact = (observed.get("result") or {}).get("artifact") or {}
                        supplied = payload.get("result", {}).get("artifact") or {}
                        if (action != "jobs/complete" or proof.get("source_key") != historical["source_key"]
                                or not (observed.get("dispatch_completed") or offer.get("stage") == "publish"
                                        or (observed.get("result") or {}).get("preparation_completed") is True)
                                or not artifact.get("sha256") or not artifact.get("size") or not artifact.get("url")
                                or any(supplied.get(key) != artifact.get(key) for key in ("sha256", "size", "url"))):
                            raise DispatchError("historical_receipt_requires_reconciliation")
                        return self.store.save_delivery(payload, delivery_id, payload, {"accepted": True, "historical": True})
                    expected = historical["payload"]["continuation"]["video_id"]
                    supplied = payload.get("result", {}).get("video_id")
                    if (proof.get("source_key") != historical["source_key"] or proof.get("publish_id") != expected
                            or (supplied and supplied != expected)
                            or not (observed.get("published") or observed.get("dispatch_completed"))):
                        raise DispatchError("historical_receipt_requires_reconciliation")
                    # The original generation's terminal outcome is already in
                    # the native ledger. ACK delivery without touching its successor.
                    return self.store.save_delivery(payload, delivery_id, payload, {"accepted": True, "historical": True})
        def accept(response):
            return self.store.save_delivery(payload, delivery_id, payload, response)
        if action == "nodes/register":
            return self.store.register_node(node_id, payload.get("capabilities", []), payload.get("metrics", payload.get("inventory", {})))
        if action in ("workers/heartbeat", "workers/drain"):
            return self.store.worker_heartbeat(node_id, payload.get("worker_id"), payload.get("stage"), payload.get("pid"), action.endswith("drain") or payload.get("draining", False), payload.get("observation"))
        if action == "jobs/claim":
            response = self.store.claim(node_id, payload.get("worker_id"), payload.get("stage"))
            job = response["job"]
            if job:
                try:
                    if self.recover_prepared(job):
                        return {"job": None, "idle_reason": "canonical_preparation_recovered"}
                    with contextlib.closing(self.store.connect()) as c:
                        node = c.execute("SELECT capabilities_json FROM pipeline_node WHERE node_id=?", (node_id,)).fetchone()
                    capabilities = json.loads(node[0]) if node else []
                    job["executor_backend"] = "gpu" if "backend:gpu" in capabilities else "cpu"
                    job["executor_capabilities"] = capabilities
                    descriptor = self.registry.reserve(job, job)
                    # Freeze the actual descriptor so subsequent operations are
                    # verified against the same body and artifact by native code.
                    with self.store.transaction() as c:
                        self.store._validate_claim(c, job)
                        c.execute("UPDATE pipeline_job SET payload_json=?,artifact_json=? WHERE id=? AND generation=?", (dumps(descriptor), dumps(descriptor.get("artifact", job["artifact"])), job["id"], job["generation"]))
                        if descriptor.get("required_capabilities"):
                            c.execute("UPDATE pipeline_job SET capabilities_json=? WHERE id=? AND generation=?", (dumps(descriptor["required_capabilities"]), job["id"], job["generation"]))
                    job["payload"] = descriptor
                    job["artifact"] = descriptor.get("artifact", job["artifact"])
                    required = descriptor.get("required_capabilities") or []
                    if not set(required).issubset(capabilities):
                        self.store.defer_unstarted(job, required)
                        return {"job": None, "idle_reason": "capability_unavailable"}
                except Exception as exc:
                    if getattr(exc, "code", "") in {"claim_expired", "claim_conflict"}:
                        return {"job": None, "idle_reason": "claim_expired_during_reservation"}
                    try:
                        if self.recover_prepared(job):
                            return {"job": None, "idle_reason": "canonical_preparation_recovered"}
                        observed = self.registry.reconcile(job)
                        if observed.get("metadata_only"):
                            self.store.skip(job, observed.get("canonical_receipt", {}), canonical_verified=True)
                            return {"job": None, "idle_reason": "canonical_no_candidate"}
                    except Exception:
                        pass
                    self.store.fail(job, getattr(exc, "code", "canonical_reservation_failed"))
                    raise DispatchError(getattr(exc, "code", "canonical_reservation_failed")) from None
            return response
        if action == "jobs/heartbeat":
            return self.store.heartbeat(payload, payload.get("phase"), payload.get("metrics"))
        if action == "jobs/begin-operation":
            from .credentials import seal_credentials
            if hashlib.sha256(dumps(payload.get("details", {})).encode()).hexdigest() != payload.get("request_sha256"):
                raise DispatchError("operation_fingerprint_invalid", status=400)
            binding = self.store.begin_operation(payload, payload)
            job = self.store.get_job(payload["job_id"])
            try:
                issued = self.registry.begin_operation(job, payload, payload)
                if issued.get("credentials_exhausted"):
                    return {"accepted": False, "credentials_exhausted": True}
                credentials = issued.get("credentials", issued)
                envelope = seal_credentials(self.node_key(node_id, auth.get("key_version", 1)), credentials, binding, ttl_seconds=120)
            except Exception as exc:
                # Intent exists: only canonical reconciliation may clear it.
                self.store.fail(payload, getattr(exc, "code", "credential_issue_failed"), unknown=True)
                raise DispatchError(getattr(exc, "code", "credential_issue_failed")) from None
            return {"accepted": True, "credential_envelope": envelope, "binding": binding}
        if action == "jobs/checkpoint":
            job = self.store.validate_receipt_claim(payload)
            method = getattr(self.registry, "checkpoint", None)
            if method is None:
                raise DispatchError("canonical_checkpoint_unavailable", status=503)
            method(job, payload, payload.get("phase"), payload.get("receipt", {}))
            return accept(self.store.checkpoint(payload, payload.get("checkpoint_id"), payload.get("phase"), payload.get("receipt", {}), canonical_verified=True))
        if action == "jobs/complete":
            job = self.store.validate_receipt_claim(payload)
            result = payload.get("result", {})
            self.store.record_execution_metrics(payload, payload.get("metrics", {}))
            self.store.record_cooldown(payload, result)
            verified = self.registry.complete(job, result, payload)
            proof = verified.get("canonical_receipt", {})
            if verified.get("metadata_only"):
                return accept(self.store.skip(payload, proof, canonical_verified=True))
            if proof.get("publish_id") and (verified.get("published") or proof.get("video_published")):
                self.store.record_publication(job["id"], proof, canonical_verified=True)
            if verified.get("definitive_failure"):
                return accept(self.store.fail(payload, "canonical_execution_failed", canonical_no_write=True))
            if job["stage"] == "publish" and not (verified.get("published") or verified.get("dispatch_completed")):
                # Submitted/processing is not a confirmed target publication.
                return accept(self.store.fail(payload, "canonical_publication_pending", unknown=True))
            canonical = dict(result)
            canonical["_delivery_sha256"] = hashlib.sha256(dumps(payload).encode()).hexdigest()
            canonical["dispatch_completed"] = bool(verified.get("dispatch_completed"))
            canonical["published"] = bool(verified.get("published"))
            canonical["canonical_receipt"] = dumps(verified.get("canonical_receipt", {}))
            canonical["phase_metrics"] = {k[:-8]: v for k, v in payload.get("metrics", {}).items() if k.endswith("_seconds")}
            response = self.store.complete(payload, payload.get("receipt_id"), canonical, canonical_verified=True)
            if verified.get("continuation"):
                # Publish the next claim and the previous callback ACK together.
                # A crash cannot advance generation while losing its old ACK.
                self.store.rearm_schedule(payload, verified["continuation"], (delivery_id, payload, response))
            if verified.get("next_offer") and job["stage"] == "prepare":
                self.store.offer(verified["next_offer"])
            return accept(response)
        if action == "jobs/fail":
            job = self.store.validate_receipt_claim(payload)
            self.store.record_execution_metrics(payload, payload.get("metrics", {}))
            self.store.record_cooldown(payload, payload)
            native_fail = getattr(self.registry, "fail", None)
            verified = native_fail(job, payload) if native_fail else {}
            observed = self.registry.reconcile(job)
            proof = observed.get("canonical_receipt", {})
            if observed.get("metadata_only"):
                return accept(self.store.skip(payload, proof, canonical_verified=True))
            if proof.get("publish_id") and (observed.get("published") or proof.get("video_published")):
                self.store.record_publication(job["id"], proof, canonical_verified=True)
            if verified.get("canonical_terminal"):
                reconciled = self.registry.reconcile(job)
                self.store.fail(payload, "canonical_receipt_reconciliation", unknown=True)
                response = self.store.reconcile_native(job["id"], reconciled)
                if reconciled.get("continuation"):
                    self.store.rearm_schedule(payload, reconciled["continuation"], (delivery_id,payload,response))
                return accept(response)
            return accept(self.store.fail(payload, payload.get("error_code", "execution_failed"), unknown=payload.get("unknown_outcome", False), canonical_no_write=verified.get("canonical_no_write", False)))
        raise DispatchError("not_found", status=404)


def handler_type(coordinator):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200, {"ok": True, "service": "pipeline-dispatch"}) if self.path == "/health" else self.reply(404, {"code": "not_found"})

        def do_PUT(self):
            self.do_POST()

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8 * 1024 * 1024:
                    raise DispatchError("request_size_invalid", status=413)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise DispatchError("request_invalid", status=400)
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if self.path == "/internal/pipeline/nodes/enroll":
                    enrolled = coordinator.store.consume_enrollment(token, payload.get("node_id"))
                    enrolled["node_token"] = enrolled.pop("token")
                    enrolled["seal_key"] = coordinator.node_key(enrolled["node_id"], enrolled["key_version"])
                    enrolled["coordinator_url"] = payload.get("coordinator_url", "")
                    self.reply(200, enrolled)
                    return
                auth = coordinator.authenticate(token)
                self.reply(200, coordinator.route(self.path, payload, auth))
            except DispatchError as exc:
                self.reply(exc.status, {"ok": False, "code": exc.code})
            except (ValueError, TypeError, KeyError):
                self.reply(400, {"ok": False, "code": "request_invalid"})
            except Exception:
                self.reply(503, {"ok": False, "code": "coordinator_operation_failed"})
    return Handler


def build_registry(config):
    from .adapters import AdapterRegistry, RemoteCanonicalDriver
    drivers = {}
    for item in config.get("drivers", []):
        token = os.environ.get(item["token_env"], "")
        if len(token) < 32:
            raise ValueError("canonical driver token missing")
        driver = RemoteCanonicalDriver(item["port"], item["prefix"], token, control_only=item.get("control_only", False))
        for family in item["families"]:
            drivers[(item["channel"], family)] = driver
    return AdapterRegistry.from_paths(config.get("paths", {}), drivers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    store = DispatchStore(config["db_path"])
    from .owners import OwnerResolver
    coordinator = Coordinator(store, build_registry(config), internal_token=os.environ.get("PIPELINE_DISPATCH_INTERNAL_TOKEN", ""), master_key=os.environ.get("PIPELINE_DISPATCH_MASTER_KEY", ""), owner_resolver=OwnerResolver(config.get("paths", {}).get("youtube")))
    server = ThreadingHTTPServer(("127.0.0.1", int(config.get("port", 18846))), handler_type(coordinator))
    server.daemon_threads = True
    def stop(*_):
        coordinator.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    thread = threading.Thread(target=coordinator.run_collector, daemon=True)
    thread.start()
    try:
        server.serve_forever()
    finally:
        coordinator.stopping.set()
        thread.join(timeout=20)
        server.server_close()


if __name__ == "__main__":
    main()

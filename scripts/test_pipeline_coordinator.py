import datetime as dt
import hashlib
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.core import DispatchStore, DispatchError, dumps, stamp
from features.pipeline_dispatch.service import Coordinator
from features.pipeline_dispatch.credentials import open_credentials
from features.pipeline_dispatch import views


class NativeLedger:
    adapters = {}
    errors = {}

    def __init__(self):
        self.status = "queued"
        self.calls = 0
        self.available = True

    def scan(self, **kwargs): return []
    def reserve(self, job, claim): return {"kind": "x_publish", "account_resource": "x:account:1"}
    def begin_operation(self, job, claim, operation):
        self.calls += 1
        return {"credentials": {"access_token": "test-only-memory-token"}}
    def checkpoint(self, *args): return {"accepted": True}
    def complete(self, job, result, claim): return self.reconcile(job)
    def reconcile(self, job):
        return {"published": self.status == "published", "dispatch_completed": self.status == "scheduled", "canonical_receipt": {"source_key": job["source_key"], "publish_id": "receipt-1", "status": self.status}}
    def fail(self, job, payload): return {"canonical_no_write": False, "canonical_terminal": self.status == "published"}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.db", lambda: self.now)
        self.native = NativeLedger()
        self.service = Coordinator(self.store, self.native, internal_token="i" * 32, master_key="m" * 32)
        self.store.register_node("cpu-main", ["publish:x", "backend:cpu"])
        self.store.worker_heartbeat("cpu-main", "one", "publish")
        self.store.channel_enabled("x", True)
        self.admin = {"is_admin": True, "user_id": "admin", "allowed_channels": ["x"]}
        self.auth = {"internal": True}

    def offer(self, key="x:queue:1", **extras):
        row = {"source_key": key, "source_id": key.rsplit(":", 1)[-1], "channel": "x", "family": "queue", "stage": "publish", "resource_keys": ["x:account:1"], "capabilities": ["publish:x"], "scheduled_at": stamp(self.now), "source_created_at": stamp(self.now)}
        row.update(extras)
        return self.store.offer(row)

    def claim(self):
        job = self.service.route("/internal/pipeline/jobs/claim", {"node_id": "cpu-main", "worker_id": "one", "stage": "publish"}, self.auth)["job"]
        return {"job_id": job["id"], **{k: job[k] for k in ("generation", "node_id", "worker_id", "claim_token")}}

    def operation(self, claim):
        details = {"method": "POST", "path": "/2/tweets", "body_sha256": "b" * 64}
        return {**claim, "operation_id": "operation-1", "operation": "create", "account_resource": "x:account:1", "details": details, "request_sha256": hashlib.sha256(dumps(details).encode()).hexdigest()}

    def test_actual_credential_seal_and_replay_fence(self):
        self.offer(); claim = self.claim(); request = self.operation(claim)
        issued = self.service.route("/internal/pipeline/jobs/begin-operation", request, self.auth)
        with open_credentials(issued["credential_envelope"], self.service.node_key("cpu-main"), issued["binding"]) as credential:
            self.assertEqual(credential["access_token"], "test-only-memory-token")
        with self.assertRaises(DispatchError):
            self.service.route("/internal/pipeline/jobs/begin-operation", request, self.auth)
        self.assertEqual(self.native.calls, 1)

    def test_mismatched_request_fingerprint_never_issues_credential(self):
        self.offer(); claim = self.claim(); request = self.operation(claim)
        request["details"]["path"] = "/2/changed"
        with self.assertRaises(DispatchError):
            self.service.route("/internal/pipeline/jobs/begin-operation", request, self.auth)
        self.assertEqual(self.native.calls, 0)

    def test_canonical_reconcile_then_late_outbox_keeps_one_success(self):
        self.offer(); claim = self.claim()
        self.service.route("/internal/pipeline/jobs/begin-operation", self.operation(claim), self.auth)
        self.now += dt.timedelta(seconds=121)
        self.native.status = "published"
        self.service.scan()
        self.assertEqual(self.store.get_job(claim["job_id"])["status"], "published")
        late = {**claim, "receipt_id": "late-1", "result": {"post_id": "receipt-1"}, "metrics": {"platform_upload_seconds": 7}}
        self.assertTrue(self.service.route("/internal/pipeline/jobs/complete", late, self.auth)["accepted"])
        self.assertTrue(self.service.route("/internal/pipeline/jobs/complete", late, self.auth)["duplicate"])
        summary = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(summary["totals"]["published"], 1)

    def test_scheduled_is_not_counted_as_publication(self):
        self.offer(); claim = self.claim(); self.native.status = "scheduled"
        self.service.route("/internal/pipeline/jobs/complete", {**claim, "receipt_id": "schedule-1", "result": {}}, self.auth)
        result = views.query(self.store, "summary", self.admin, {})
        self.assertEqual(result["totals"]["published"], 0)
        self.assertEqual(sum(row["throughput_per_hour"] or 0 for row in result["channels"]), 0)

    def test_two_historical_unknowns_hold_same_account_until_both_resolved(self):
        first = self.offer("x:queue:old1", status="unknown")
        second = self.offer("x:queue:old2", status="unknown")
        self.offer()
        self.assertIsNone(self.store.claim("cpu-main", "one", "publish")["job"])
        for job in (first, second):
            self.store.reconcile_native(job["id"], {"definitive_failure": True, "canonical_receipt": {"source_key": job["source_key"]}})
            if job == first:
                self.assertIsNone(self.store.claim("cpu-main", "one", "publish")["job"])
        self.assertIsNotNone(self.store.claim("cpu-main", "one", "publish")["job"])

    def test_cooldown_skips_account_and_preserves_other_account_work(self):
        self.offer(); claim = self.claim()
        self.store.record_cooldown(claim, {"rate_limit_scope": "account", "account_resource": "x:account:1", "retry_after_seconds": 90})
        self.store.fail(claim, "rate_limited", canonical_no_write=True)
        self.offer("x:queue:2")
        third = self.offer("x:queue:3", resource_keys=["x:account:3"])
        self.assertEqual(self.claim()["job_id"], third["id"])

    def test_incremental_counter_transition_does_not_count_twice(self):
        self.offer(); claim = self.claim(); self.native.status = "published"
        self.service.route("/internal/pipeline/jobs/complete", {**claim, "receipt_id": "one", "result": {}}, self.auth)
        with self.store.transaction() as c:
            self.assertEqual(c.execute("SELECT SUM(n) FROM pipeline_monitor_counts").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT SUM(n) FROM pipeline_monitor_counts WHERE status='published'").fetchone()[0], 1)

    def test_no_candidate_is_neither_render_success_nor_publish_failure(self):
        self.offer(); claim = self.claim()
        self.native.complete = lambda job, result, request: {"metadata_only": True, "canonical_receipt": {"source_key": job["source_key"], "status": "no_candidate"}}
        receipt = {**claim, "receipt_id": "skip-1", "result": {}}
        self.assertTrue(self.service.route("/internal/pipeline/jobs/complete", receipt, self.auth)["metadata_only"])
        self.assertTrue(self.service.route("/internal/pipeline/jobs/complete", receipt, self.auth)["duplicate"])
        with self.store.transaction() as c:
            self.assertEqual(c.execute("SELECT COALESCE(SUM(n),0) FROM pipeline_monitor_counts").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_resource").fetchone()[0], 0)

    def test_no_candidate_cannot_clear_existing_write_intent(self):
        self.offer(); claim = self.claim()
        self.service.route("/internal/pipeline/jobs/begin-operation", self.operation(claim), self.auth)
        with self.assertRaisesRegex(DispatchError, "operation_requires_reconciliation"):
            self.store.skip(claim, {"source_key": "x:queue:1"}, canonical_verified=True)

    def test_scheduled_completion_ack_survives_next_generation(self):
        self.store.channel_enabled("youtube", True)
        self.store.register_node("cpu-main", ["publish:youtube", "backend:cpu"])
        self.offer("youtube:publish:1", channel="youtube", capabilities=["publish:youtube"], resource_keys=["youtube:channel:1"])
        claim = self.claim()
        self.native.complete = lambda job, result, request: {"dispatch_completed": True, "canonical_receipt": {"source_key": job["source_key"], "publish_id": "video-1", "status": "scheduled"}, "continuation": {"video_id": "video-1", "scheduled_at": stamp(self.now + dt.timedelta(minutes=5))}}
        receipt = {**claim, "receipt_id": "schedule-ack", "result": {}}
        self.service.route("/internal/pipeline/jobs/complete", receipt, self.auth)
        self.now += dt.timedelta(minutes=6)
        self.store.register_node("cpu-main", ["publish:youtube", "backend:cpu"])
        self.store.worker_heartbeat("cpu-main", "one", "publish")
        next_claim = self.claim()
        self.assertEqual(next_claim["generation"], claim["generation"] + 1)
        self.assertTrue(self.service.route("/internal/pipeline/jobs/complete", receipt, self.auth)["duplicate"])
        self.assertEqual(self.store.get_job(next_claim["job_id"])["status"], "running")


if __name__ == "__main__":
    unittest.main()

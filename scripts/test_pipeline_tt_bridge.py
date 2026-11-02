"""Offline native TT ledger/portable execution integration checks."""
import unittest
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch
from features.pipeline_dispatch.tt_native import TTNativeBridge
from features.pipeline_dispatch.adapters import AdapterError, public_descriptor
from scripts import test_tt_auto_post_publisher as auto_fixtures
from scripts import test_tt_posts_core as queue_fixtures
from scripts import test_tt_posts_service as service_fixtures


class TTBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = auto_fixtures.TTAutoPostPublisherIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.task = self.fixture.reserved_task(suffix="dispatch")
        self.gpu = auto_fixtures.FakeGPU()
        self.executor = self.fixture.executor(self.gpu)
        self.service = SimpleNamespace(store=self.fixture.store, executor=self.executor)
        self.bridge = TTNativeBridge(self.service, automatic=True)
        self.job = {"source_key": "tt:auto:%s" % self.task.id, "source_id": str(self.task.id),
                    "channel": "tt", "family": "auto", "stage": "prepare", "generation": 1,
                    "executor_backend": "cpu"}
        self.claim = {"claim_token": "a" * 32, "node_id": "node-3"}

    def prepare(self):
        descriptor = self.bridge.reserve(self.job, self.claim)
        self.assertEqual(self.gpu.prepare_calls, [])
        self.assertEqual(descriptor["request"]["expected_profile"], "tt-post-h264-720x1280-v2")
        request = descriptor["request"]
        result = self.gpu.prepare(job_id=request["job_id"], material={"content_id": request["content_id"]}, expected_profile=request["expected_profile"])
        self.bridge.complete(self.job, {"preparation": result}, self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, "ready")
        self.job = dict(self.job, stage="publish", generation=2)
        descriptor = self.bridge.reserve(self.job, self.claim)
        self.assertEqual(self.gpu.publish_calls, [])
        public_descriptor(descriptor)
        return descriptor

    def operation(self, descriptor, name, details, operation_id="operation-1234"):
        return {"operation": name, "operation_id": operation_id, "account_resource": descriptor["account_resource"],
                "request_sha256": "b" * 64, "details": details}

    def test_node_prepares_and_publishes_native_ledger_once(self):
        descriptor = self.prepare()
        operation = self.operation(descriptor, "tt_initialize_video", {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]})
        issued = self.bridge.begin_operation(self.job, self.claim, operation)
        self.assertIn("access_token", issued["credentials"])
        self.bridge.checkpoint(self.job, self.claim, "tt_initialized", {"publish_id": "pub-portable", "log_id": "safe-log"})
        operation = self.operation(descriptor, "tt_fetch_status", {"publish_id": "pub-portable"}, "operation-status")
        self.bridge.begin_operation(self.job, self.claim, operation)
        receipt = {"publish_id": "pub-portable", "status": "PUBLISH_COMPLETE", "post_ids": ["123"]}
        self.bridge.checkpoint(self.job, self.claim, "tt_status", receipt)
        self.bridge.complete(self.job, receipt, self.claim)
        self.assertEqual(self.bridge.reconcile(self.job)["status"], "published")
        self.assertTrue(self.bridge.complete(self.job, receipt, self.claim)["idempotent"])
        self.assertEqual(self.gpu.publish_calls, [])

    def test_lost_init_cannot_be_reissued_and_unknown_is_preserved(self):
        descriptor = self.prepare()
        operation = self.operation(descriptor, "tt_initialize_video", {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]})
        self.bridge.begin_operation(self.job, self.claim, operation)
        operation["operation_id"] = "operation-second"
        with self.assertRaisesRegex(AdapterError, "operation_already_started"):
            self.bridge.begin_operation(self.job, self.claim, operation)
        self.assertFalse(self.bridge.fail(self.job, self.claim, {"error_code": "lost_response"})["canonical_no_write"])
        self.assertEqual(self.bridge.reconcile(self.job)["status"], "unknown")

    def test_late_init_receipt_recovers_unknown_without_reinitializing(self):
        descriptor = self.prepare()
        operation = self.operation(descriptor, "tt_initialize_video", {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]})
        self.bridge.begin_operation(self.job, self.claim, operation)
        self.bridge.fail(self.job, self.claim, {"error_code": "lost_response"})
        self.bridge.checkpoint(self.job, self.claim, "tt_initialized", {"publish_id": "late-publish"})
        self.bridge.begin_operation(self.job, self.claim, self.operation(descriptor, "tt_fetch_status", {"publish_id": "late-publish"}, "late-status-operation"))
        receipt = {"publish_id": "late-publish", "status": "PUBLISH_COMPLETE", "post_ids": ["789"]}
        self.bridge.checkpoint(self.job, self.claim, "tt_status", receipt)
        self.assertEqual(self.bridge.reconcile(self.job)["status"], "published")
        with self.bridge.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_tt_operation WHERE operation='tt_initialize_video'").fetchone()[0], 1)

    def test_empty_selection_preserves_native_no_candidate_outcome(self):
        from features.tt_auto_posts.selector import NoEligibleMaterial
        with self.bridge.connect() as c:
            c.execute("UPDATE tt_auto_task SET status='pending' WHERE id=?", (self.task.id,))
        with patch.object(self.executor, '_select', side_effect=NoEligibleMaterial({})):
            with self.assertRaises(NoEligibleMaterial):
                self.bridge.reserve(self.job, self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, 'no_candidate')
        self.assertEqual(self.gpu.prepare_calls, [])

    def test_body_change_is_rejected_without_platform_intent(self):
        descriptor = self.prepare()
        operation = self.operation(descriptor, "tt_initialize_video", {"post_info": {}, "video_url": descriptor["artifact"]["url"]})
        with self.assertRaisesRegex(AdapterError, "tt_operation_body_changed"):
            self.bridge.begin_operation(self.job, self.claim, operation)
        self.assertTrue(self.bridge.fail(self.job, self.claim, {"error_code": "invalid_request"})["canonical_no_write"])

    def test_wrong_media_identity_is_rejected(self):
        descriptor = self.bridge.reserve(self.job, self.claim)
        request = descriptor["request"]
        result = self.gpu.prepare(job_id="wrong-job", material={"content_id": request["content_id"]}, expected_profile=request["expected_profile"])
        with self.assertRaisesRegex(Exception, "身份不一致"):
            self.bridge.complete(self.job, {"preparation": result}, self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, "preparing")

    def test_pause_preserves_old_reservation_and_allows_new_account_item(self):
        cutoff = self.fixture.clock().isoformat()
        before = self.fixture.store.get_task(self.task.id).as_dict()
        self.assertTrue(self.bridge.pause(self.job, cutoff)["held"])
        self.assertEqual(self.fixture.store.get_task(self.task.id).as_dict(), before)
        self.fixture.clock.value += timedelta(minutes=1)
        fresh = self.fixture.reserved_task(suffix="fresh")
        claimed = self.fixture.store.claim_next_executable_task(worker_id="new-worker", lease_seconds=120, task_id=fresh.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.task.id, fresh.id)
        with self.assertRaisesRegex(AdapterError, "canonical_task_active_or_terminal"):
            self.bridge.pause(dict(self.job, source_id=str(fresh.id), source_key="tt:auto:%s" % fresh.id), self.fixture.clock().isoformat())


class ExactQueueClaimTests(unittest.TestCase):
    def test_exact_claim_does_not_terminalize_other_historical_queue(self):
        fixture = queue_fixtures.CoreTestCase()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        old = fixture.add_and_freeze(material_id="1001")
        fixture.clock.current += timedelta(hours=2)
        new = fixture.add_and_freeze(material_id="1002", scheduled="2026-07-29 12:00:00")
        claimed = fixture.store.claim_due("pipeline-worker", now=fixture.clock(), grace_seconds=600, queue_id=new["id"], limit=1)
        self.assertEqual([c.queue_id for c in claimed], [new["id"]])
        self.assertEqual(fixture.store.get_queue(old["id"])["status"], "scheduled")


class PoolBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = service_fixtures.ServiceLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service(service_fixtures.OPEN_GATES)
        self.bridge = TTNativeBridge(self.service)
        self.claim = {"claim_token": "n" * 32, "node_id": "node-4"}

    def test_pool_intake_preserves_frozen_profile_and_validator(self):
        intake = self.service.material_pool_add(self.fixture.recurring_material_payload())["item"]
        job = {"source_key": "tt:intake:%s" % intake["id"], "source_id": str(intake["id"]), "channel": "tt", "family": "intake", "stage": "prepare", "generation": 1}
        descriptor = self.bridge.reserve(job, self.claim)
        self.assertEqual(self.fixture.gpu.prepare_jobs, [])
        request = descriptor["request"]
        prepared = self.fixture.gpu.prepare(job_id=request["job_id"], material={"content_id": request["content_id"], "source_media_url": request["source_url"]},
            source_trim_tail_seconds=request["source_trim_tail_seconds"], expected_profile=request["expected_profile"])
        self.bridge.complete(job, {"preparation": prepared}, self.claim)
        self.assertEqual(self.bridge.reconcile(job)["status"], "ready")
        self.assertEqual(self.service.store.count_recurring_materials(account_id="101"), 1)

    def test_queue_native_policy_then_portable_init_and_receipt(self):
        queue = self.service.queue_create(service_fixtures.queue_payload(self.fixture.clock, "direct_post"))["item"]
        self.fixture.clock.value += timedelta(minutes=30)
        job = {"source_key": "tt:queue:%s" % queue["id"], "source_id": str(queue["id"]), "channel": "tt", "family": "queue", "stage": "publish", "generation": 1}
        descriptor = self.bridge.reserve(job, self.claim)
        self.assertEqual(self.fixture.gpu.publish_jobs, [])
        operation = {"operation": "tt_initialize_video", "operation_id": "operation-queue", "account_resource": descriptor["account_resource"],
            "request_sha256": "a" * 64, "details": {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]}}
        self.bridge.begin_operation(job, self.claim, operation)
        self.bridge.checkpoint(job, self.claim, "tt_initialized", {"publish_id": "pub-queue"})
        operation.update(operation="tt_fetch_status", operation_id="operation-poll", details={"publish_id": "pub-queue"})
        self.bridge.begin_operation(job, self.claim, operation)
        receipt = {"publish_id": "pub-queue", "status": "PUBLISH_COMPLETE", "post_ids": ["456"]}
        self.bridge.checkpoint(job, self.claim, "tt_status", receipt)
        self.bridge.complete(job, receipt, self.claim)
        self.assertEqual(self.bridge.reconcile(job)["status"], "published")
        self.assertTrue(self.bridge.fail(job, self.claim, {"error_code": "late_failure"})["canonical_terminal"])


if __name__ == "__main__":
    unittest.main()

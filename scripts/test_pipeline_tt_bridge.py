"""Offline native TT ledger/portable execution integration checks."""
import copy
import os
import unittest
from dataclasses import dataclass, replace
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch
from features.pipeline_dispatch.tt_native import TTNativeBridge, MediaPort
from features.pipeline_dispatch.adapters import AdapterError, public_descriptor
from scripts import test_tt_auto_post_publisher as auto_fixtures
from scripts import test_tt_posts_core as queue_fixtures
from scripts import test_tt_posts_service as service_fixtures


@dataclass(frozen=True)
class ProductionVideoRoute:
    gpu_client: object
    media_profile_version: str
    source_trim_tail_seconds: float


class ProductionRoutedExecutor(auto_fixtures.AutoPostExecutor):
    """Production selects a frozen route before entering prepare/publish logic.

    Keep the real native validation and SQLite transitions, but select their
    client/profile/trim through the production frozen-dataclass call path.
    """
    def _video_route_for_task(self, task):
        return self.test_routes[(task.template_id, task.template_version)]

    def routed(self, task):
        route = self._video_route_for_task(task)
        clone = copy.copy(self)
        clone.gpu_client = route.gpu_client
        clone.media_profile_version = route.media_profile_version
        clone.source_trim_tail_seconds = route.source_trim_tail_seconds
        return clone

    def _prepare(self, task, claim_token=None):
        return super(ProductionRoutedExecutor, self.routed(task))._prepare(task, claim_token)

    def _publish(self, task, claim_token=None):
        return super(ProductionRoutedExecutor, self.routed(task))._publish(task, claim_token)


class RoutedTTBridgeTests(unittest.TestCase):
    def setUp(self):
        approvals = patch.dict(os.environ, {"PIPELINE_TT_APPROVED_OUTRO_SHA256": "d" * 64,
                                           "PIPELINE_TT_APPROVED_FONT_SHA256": "e" * 64})
        approvals.start(); self.addCleanup(approvals.stop)
        self.fixture = auto_fixtures.TTAutoPostPublisherIntegrationTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.task = self.fixture.reserved_task(suffix="routed")
        self.default_gpu, self.route_gpu = auto_fixtures.FakeGPU(), auto_fixtures.FakeGPU()
        self.executor = self.fixture.executor(self.default_gpu)
        self.executor.__class__ = ProductionRoutedExecutor
        self.executor.media_profile_version = "tt-post-random-overlay-hevc-720x1280-v3"
        self.route = ProductionVideoRoute(self.route_gpu, "tt-post-direct-outro-hevc-720x1280-v2", 4.333333)
        self.executor.test_routes = {(self.task.template_id, self.task.template_version): self.route}
        self.bridge = TTNativeBridge(SimpleNamespace(store=self.fixture.store, executor=self.executor), automatic=True)
        self.job = {"source_key": "tt:auto:%s" % self.task.id, "source_id": str(self.task.id),
                    "channel": "tt", "family": "auto", "stage": "prepare", "generation": 1, "executor_backend": "cpu",
                    "executor_capabilities": ["asset:tt-outro:" + "d" * 64, "asset:tt-font:" + "e" * 64, "catalog:tt:" + "f" * 64]}
        self.claim = {"claim_token": "r" * 32, "node_id": "cpu-main"}
        for client in (self.default_gpu, self.route_gpu):
            for method in ("prepare", "publish", "reconcile"):
                boundary = patch.object(client, method, side_effect=AssertionError("legacy media HTTP boundary crossed"))
                boundary.start(); self.addCleanup(boundary.stop)

    def result(self, descriptor):
        request = descriptor["request"]
        return auto_fixtures.FakeGPU().prepare(job_id=request["job_id"],
            material={"content_id": request["content_id"]}, expected_profile=request["expected_profile"])

    def test_frozen_route_intercepts_reserve_completion_publish_and_repeat(self):
        descriptor = self.bridge.reserve(self.job, self.claim)
        self.assertEqual(descriptor["request"]["expected_profile"], "tt-post-direct-outro-h264-720x1280-v2")
        self.assertEqual(descriptor["request"]["source_trim_tail_seconds"], 4.333333)
        self.assertEqual(descriptor["native_route"]["profile"], self.route.media_profile_version)
        self.assertIn("profile:tt-post-direct-outro-h264-720x1280-v2", descriptor["required_capabilities"])
        self.assertIn("asset:tt-outro:" + "d" * 64, descriptor["required_capabilities"])
        self.assertIn("asset:tt-font:" + "e" * 64, descriptor["required_capabilities"])
        self.assertNotIn("catalog:tt:" + "f" * 64, descriptor["required_capabilities"])
        self.assertEqual(self.bridge.reserve(self.job, self.claim), descriptor)
        receipt = {"preparation": self.result(descriptor)}
        self.bridge.complete(self.job, receipt, self.claim)
        self.assertTrue(self.bridge.complete(self.job, receipt, self.claim)["idempotent"])
        publish = dict(self.job, stage="publish", generation=2)
        published_descriptor = self.bridge.reserve(publish, self.claim)
        self.assertEqual(self.bridge.reserve(publish, self.claim), published_descriptor)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, "publishing")
        self.assertEqual(len(self.route_gpu.creator_calls), 1)
        self.assertEqual(self.default_gpu.creator_calls, [])
        self.assertIs(self.executor._video_route_for_task(self.task), self.route)
        self.assertIs(self.route.gpu_client, self.route_gpu)
        self.assertFalse(isinstance(self.executor.gpu_client, MediaPort))
        public_descriptor(published_descriptor)

    def test_cross_generation_keeps_route_profile_and_rejects_changed_route(self):
        descriptor = self.bridge.reserve(self.job, self.claim)
        retry = dict(self.job, generation=2, executor_backend="gpu")
        self.assertEqual(self.bridge.reserve(retry, self.claim), descriptor)
        self.executor.test_routes[(self.task.template_id, self.task.template_version)] = replace(self.route, source_trim_tail_seconds=0)
        with self.assertRaisesRegex(AdapterError, "tt_frozen_video_route_changed"):
            self.bridge.complete(retry, {"preparation": self.result(descriptor)}, self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, "preparing")

    def test_missing_outro_assets_fail_before_native_claim_or_render(self):
        before = self.fixture.store.get_task(self.task.id).as_dict()
        with self.assertRaisesRegex(AdapterError, "canonical_render_asset_unavailable"):
            self.bridge.reserve(dict(self.job, executor_capabilities=["catalog:tt:" + "f" * 64]), self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).as_dict(), before)
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_tt_claim").fetchone()[0], 0)

    def test_unapproved_or_unconfigured_assets_leave_automatic_task_unclaimed(self):
        before = self.fixture.store.get_task(self.task.id).as_dict()
        cases = [
            ({"PIPELINE_TT_APPROVED_OUTRO_SHA256": ""}, self.job, "approval_missing"),
            ({"PIPELINE_TT_APPROVED_FONT_SHA256": ""}, self.job, "approval_missing"),
            ({}, dict(self.job, executor_capabilities=["asset:tt-outro:" + "a" * 64, "asset:tt-font:" + "e" * 64]), "not_approved"),
            ({}, dict(self.job, executor_capabilities=["asset:tt-outro:" + "d" * 64, "asset:tt-font:" + "a" * 64]), "not_approved"),
        ]
        for approval, job, error in cases:
            with self.subTest(approval=approval, error=error), patch.dict(os.environ, approval):
                with self.assertRaisesRegex(AdapterError, "canonical_render_asset_" + error):
                    self.bridge.reserve(job, self.claim)
                self.assertEqual(self.fixture.store.get_task(self.task.id).as_dict(), before)
                with self.bridge.connect() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_tt_claim").fetchone()[0], 0)

    def test_existing_recipe_survives_approval_change_and_retry(self):
        descriptor = self.bridge.reserve(self.job, self.claim)
        retry = dict(self.job, generation=2, executor_capabilities=[])
        with patch.dict(os.environ, {"PIPELINE_TT_APPROVED_OUTRO_SHA256": "", "PIPELINE_TT_APPROVED_FONT_SHA256": "a" * 64}):
            self.assertEqual(self.bridge.reserve(retry, self.claim), descriptor)
            self.bridge.complete(retry, {"preparation": self.result(descriptor)}, self.claim)
        self.assertEqual(self.fixture.store.get_task(self.task.id).status, "ready")

    def test_source_direct_freezes_the_actual_cpu_backend(self):
        self.executor.test_routes[(self.task.template_id, self.task.template_version)] = replace(
            self.route, media_profile_version="tt-post-source-direct-v1", source_trim_tail_seconds=0)
        descriptor = self.bridge.reserve(self.job, self.claim)
        self.assertEqual(descriptor["request"]["expected_profile"], "tt-post-source-direct-v1")
        self.assertEqual(descriptor["required_capabilities"], ["prepare:tt", "backend:cpu", "profile:tt-post-source-direct-v1"])
        self.assertEqual(self.bridge.reserve(dict(self.job, generation=2, executor_backend="gpu"), self.claim), descriptor)

    def test_other_source_uses_its_own_frozen_route_without_mutating_default(self):
        first = self.bridge.reserve(self.job, self.claim)
        self.bridge.complete(self.job, {"preparation": self.result(first)}, self.claim)
        publish = dict(self.job, stage="publish", generation=2)
        descriptor = self.bridge.reserve(publish, self.claim)
        operation = {"operation": "tt_initialize_video", "operation_id": "route-first-init",
            "account_resource": descriptor["account_resource"], "request_sha256": "c" * 64,
            "details": {"post_info": descriptor["request"]["post_info"], "video_url": descriptor["artifact"]["url"]}}
        self.bridge.begin_operation(publish, self.claim, operation)
        self.bridge.checkpoint(publish, self.claim, "tt_initialized", {"publish_id": "first-route-post"})
        operation.update(operation="tt_fetch_status", operation_id="route-first-status", details={"publish_id": "first-route-post"})
        self.bridge.begin_operation(publish, self.claim, operation)
        receipt = {"publish_id": "first-route-post", "status": "PUBLISH_COMPLETE", "post_ids": ["123"]}
        self.bridge.checkpoint(publish, self.claim, "tt_status", receipt)
        self.bridge.complete(publish, receipt, self.claim)
        second = self.fixture.reserved_task(suffix="other-route")
        self.executor.test_routes[(second.template_id, second.template_version)] = replace(self.route,
            media_profile_version="tt-post-random-overlay-hevc-720x1280-v3", source_trim_tail_seconds=0)
        job = dict(self.job, source_id=str(second.id), source_key="tt:auto:%s" % second.id)
        descriptor = self.bridge.reserve(job, self.claim)
        self.assertEqual(descriptor["request"]["expected_profile"], "tt-post-random-overlay-libx264-720x1280-v1")
        self.assertEqual(descriptor["request"]["source_trim_tail_seconds"], 0)
        self.assertNotEqual(descriptor["request"]["job_id"], first["request"]["job_id"])
        self.assertEqual(self.executor._video_route_for_task(self.task), self.route)


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

    def test_intake_and_direct_assets_are_approved_before_native_claim(self):
        self.service.media_profile_version = "tt-post-direct-outro-hevc-720x1280-v2"
        intake = self.service.material_pool_add(self.fixture.recurring_material_payload())["item"]
        jobs = [{"source_key": "tt:intake:%s" % intake["id"], "source_id": str(intake["id"]),
                 "channel": "tt", "family": "intake", "stage": "prepare", "generation": 1}]
        jobs.append(self.direct_task()[1])
        for job in jobs:
            before = self.bridge._native_dict(job)
            cases = [
                ({"PIPELINE_TT_APPROVED_OUTRO_SHA256": "", "PIPELINE_TT_APPROVED_FONT_SHA256": "e" * 64},
                 ["asset:tt-outro:" + "d" * 64, "asset:tt-font:" + "e" * 64], "approval_missing"),
                ({"PIPELINE_TT_APPROVED_OUTRO_SHA256": "d" * 64, "PIPELINE_TT_APPROVED_FONT_SHA256": "e" * 64}, [], "unavailable"),
                ({"PIPELINE_TT_APPROVED_OUTRO_SHA256": "d" * 64, "PIPELINE_TT_APPROVED_FONT_SHA256": "e" * 64},
                 ["asset:tt-outro:" + "a" * 64, "asset:tt-font:" + "e" * 64], "not_approved"),
            ]
            for approval, capabilities, error in cases:
                with self.subTest(family=job["family"], error=error), patch.dict(os.environ, approval):
                    with self.assertRaisesRegex(AdapterError, "canonical_render_asset_" + error):
                        self.bridge.reserve(dict(job, executor_capabilities=capabilities), self.claim)
                    self.assertEqual(self.bridge._native_dict(job), before)
                    with self.bridge.connect() as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_tt_claim").fetchone()[0], 0)
        self.assertEqual(self.fixture.gpu.prepare_jobs, [])

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

    def direct_task(self, material_id='9001'):
        if not hasattr(self, '_direct_config'):
            self._direct_config = self.fixture.configure_auto(self.service)
        config = self._direct_config
        item = self.service.direct_test_create(self.fixture.direct_test_payload(
            config['version'], material_id=material_id, key='dispatch-direct:' + material_id))['item']
        return item, {'source_key': 'tt:direct_test:%s' % item['id'], 'source_id': str(item['id']),
                      'channel': 'tt', 'family': 'direct_test', 'stage': 'prepare', 'generation': 1,
                      'scheduled_at': item['created_at'], 'source_created_at': item['created_at']}

    def prepare_direct(self):
        item, job = self.direct_task()
        descriptor = self.bridge.reserve(job, self.claim)
        self.assertEqual(self.fixture.gpu.prepare_jobs, [])
        request = descriptor['request']
        self.assertEqual(request['expected_profile'], item['preparation_profile'])
        result = self.fixture.gpu.prepare(job_id=request['job_id'],
            material={'content_id': request['content_id'], 'source_media_url': request['source_url']},
            source_trim_tail_seconds=request['source_trim_tail_seconds'], expected_profile=request['expected_profile'])
        self.bridge.complete(job, {'preparation': result}, self.claim)
        self.assertEqual(self.bridge.reconcile(job)['status'], 'ready')
        job = dict(job, stage='publish', generation=2)
        descriptor = self.bridge.reserve(job, self.claim)
        self.assertEqual(self.fixture.gpu.publish_jobs, [])
        self.assertEqual(descriptor['request']['post_info']['privacy_level'], 'PUBLIC_TO_EVERYONE')
        self.assertFalse(descriptor['request']['post_info']['disable_comment'])
        return job, descriptor

    def test_direct_manual_preparation_publish_and_exact_receipts(self):
        from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
        job, descriptor = self.prepare_direct()
        operation = {'operation': 'tt_initialize_video', 'operation_id': 'direct-initialize',
                     'account_resource': descriptor['account_resource'], 'request_sha256': 'a' * 64,
                     'details': {'post_info': descriptor['request']['post_info'], 'video_url': descriptor['artifact']['url']}}
        self.bridge.begin_operation(job, self.claim, operation)
        self.bridge.checkpoint(job, self.claim, 'tt_initialized', {'publish_id': 'direct-publish'})
        operation.update(operation='tt_fetch_status', operation_id='direct-status', details={'publish_id': 'direct-publish'})
        self.bridge.begin_operation(job, self.claim, operation)
        receipt = {'publish_id': 'direct-publish', 'status': 'PUBLISH_COMPLETE', 'post_ids': ['direct-post']}
        self.bridge.checkpoint(job, self.claim, 'tt_status', receipt)
        self.bridge.complete(job, receipt, self.claim)
        row = self.service.store.get_direct_test(int(job['source_id']))
        self.assertEqual((row['status'], row['publish_attempt_count']), ('published', 1))
        self.assertEqual(self.fixture.gpu.publish_jobs, [])
        adapter = LedgerAdapter(LedgerSpec('tt', 'direct_test', self.service.store.db_path, 'tt_post_direct_test'))
        projected = adapter.project(adapter.read(job['source_id']))
        self.assertEqual((projected['stage'], projected['status']), ('publish', 'published'))
        self.assertEqual(projected['publish_id'], 'direct-publish')

    def test_direct_unknown_blocks_reinit_and_accepts_only_late_matching_receipt(self):
        job, descriptor = self.prepare_direct()
        operation = {'operation': 'tt_initialize_video', 'operation_id': 'direct-lost-init',
                     'account_resource': descriptor['account_resource'], 'request_sha256': 'b' * 64,
                     'details': {'post_info': descriptor['request']['post_info'], 'video_url': descriptor['artifact']['url']}}
        self.bridge.begin_operation(job, self.claim, operation)
        self.bridge.fail(job, self.claim, {'error_code': 'connection_lost'})
        self.assertEqual(self.bridge.reconcile(job)['status'], 'unknown')
        with self.assertRaisesRegex(AdapterError, 'tt_reconcile_only'):
            self.bridge.begin_operation(job, self.claim, dict(operation, operation_id='direct-another-init'))
        self.bridge.checkpoint(job, self.claim, 'tt_initialized', {'publish_id': 'late-direct-publish'})
        self.assertEqual(self.bridge.reconcile(job)['status'], 'reconciling')
        self.assertEqual(self.service.store.get_direct_test(int(job['source_id']))['publish_attempt_count'], 1)

    def test_direct_pause_keeps_reservation_and_exact_new_prepare_claim(self):
        old, job = self.direct_task()
        self.assertTrue(self.bridge.pause(job, self.fixture.clock().isoformat())['held'])
        before = self.service.store.get_direct_test(old['id'])
        fresh, fresh_job = self.direct_task('9002')
        descriptor = self.bridge.reserve(fresh_job, self.claim)
        self.assertEqual(descriptor['source_key'], fresh_job['source_key'])
        self.assertEqual(self.service.store.get_direct_test(old['id']), before)
        self.assertEqual(self.service.store.get_direct_test(fresh['id'])['status'], 'preparing')


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.core import DispatchStore, DispatchError, digest, stamp


class DispatchCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.time = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.sqlite3", clock=lambda: self.time)
        for node in ("cpu-main", "hk-gpu"):
            self.store.register_node(node, ["prepare", "publish"])
            for n in range(4):
                self.store.worker_heartbeat(node, str(n), "publish")
        self.store.channel_enabled("x", True)
        self.store.channel_enabled("fb", True)

    def offer(self, key="x:auto:1", channel="x", batch="1", stage="publish", resource="x:account:1", **extra):
        item = dict(source_key=key, source_id=key, channel=channel, family="auto", batch_key=batch,
                    stage=stage, scheduled_at=stamp(self.time-dt.timedelta(minutes=2)),
                    source_created_at=stamp(self.time-dt.timedelta(hours=1)), resource_keys=[resource],
                    capabilities=[stage], payload={"safe": "descriptor"})
        item.update(extra)
        return self.store.offer(item)

    def claim(self, node="cpu-main", worker="0", stage="publish"):
        self.store.register_node(node, ["prepare", "publish"])
        self.store.worker_heartbeat(node, worker, stage)
        return self.store.claim(node, worker, stage)["job"]

    def intent(self, claim):
        return self.store.begin_operation(claim, {"operation_id": "op-1", "operation": "create_post",
              "request_sha256": digest("body"), "account_resource": "x:account:1"})

    def test_two_hosts_race_one_claim(self):
        self.offer()
        with concurrent.futures.ThreadPoolExecutor(2) as p:
            claims=list(p.map(lambda n: self.store.claim(n,"0","publish")["job"], ["cpu-main","hk-gpu"]))
        self.assertEqual(sum(x is not None for x in claims),1)

    def test_same_account_serial_different_accounts_parallel(self):
        self.offer()
        self.offer("x:auto:2", resource="x:account:1")
        self.offer("x:auto:3", resource="x:account:3")
        a=self.claim(); b=self.claim("hk-gpu")
        self.assertEqual(a["source_key"],"x:auto:1")
        self.assertEqual(b["source_key"],"x:auto:3")

    def test_preparation_blocks_publish_and_other_preparation_across_hosts(self):
        self.offer("x:auto:prepare", stage="prepare")
        preparing = self.claim(stage="prepare")
        self.offer("x:auto:next-prep", stage="prepare")
        self.offer("x:auto:ready")
        self.offer("x:auto:other", resource="x:account:other")
        self.assertIsNone(self.claim("hk-gpu", "prepare", "prepare"))
        self.assertEqual(self.claim("hk-gpu", "publish")["source_key"], "x:auto:other")
        self.store.complete(preparing, "prepared-receipt", {"artifact": {
            "url": "https://media.example/frozen.mp4", "sha256": "a" * 64, "size": 1024}}, canonical_verified=True)
        self.assertEqual(self.claim("cpu-main", "publish")["source_key"], "x:auto:ready")

    def test_publication_blocks_same_page_and_channel_preparation(self):
        for channel, resource in (("fb", "fb:page:1"), ("youtube", "youtube:channel:UC1")):
            with self.subTest(channel=channel):
                self.store.channel_enabled(channel, True)
                self.offer(channel + ":publish:1", channel=channel, resource=resource)
                claim = self.claim("cpu-main", channel + "-publish")
                self.offer(channel + ":prepare:2", channel=channel, stage="prepare", resource=resource)
                self.offer(channel + ":prepare:3", channel=channel, stage="prepare", resource=resource + "-other")
                other = self.claim("hk-gpu", channel + "-prepare", "prepare")
                self.assertEqual(other["source_key"], channel + ":prepare:3")
                self.store.fail(claim, "definite_no_write", canonical_no_write=True)
                self.store.fail(other, "definite_no_write", canonical_no_write=True)

    def test_shared_relay_resource_serializes_different_primary_accounts(self):
        self.offer("x:auto:relay-prep", stage="prepare", resource_keys=["x:account:1", "x:account:relay"])
        self.claim(stage="prepare")
        self.offer("x:auto:relay-publish", resource_keys=["x:account:2", "x:account:relay"])
        self.assertIsNone(self.claim("hk-gpu"))
        self.offer("x:auto:independent", resource="x:account:3")
        self.assertEqual(self.claim("hk-gpu")["source_key"], "x:auto:independent")

    def test_two_hosts_race_same_account_preparations(self):
        self.offer("x:auto:1", stage="prepare")
        self.offer("x:auto:2", stage="prepare")
        for node in ("cpu-main", "hk-gpu"):
            self.store.worker_heartbeat(node, "prep", "prepare")
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda node: self.store.claim(node, "prep", "prepare")["job"], ("cpu-main", "hk-gpu")))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_preparation_expiry_releases_old_generation_without_unfencing_new_one(self):
        self.offer(stage="prepare")
        old = self.claim(stage="prepare")
        self.time += dt.timedelta(seconds=121)
        current = self.claim("hk-gpu", "prep", "prepare")
        self.assertEqual(current["generation"], 2)
        self.offer("x:auto:next")
        self.assertIsNone(self.claim("cpu-main", "pub"))
        with self.assertRaises(DispatchError):
            self.store.fail(old, "late_preparation_failure", canonical_no_write=True)
        self.store.fail(current, "definite_no_write", canonical_no_write=True)
        self.assertIsNotNone(self.claim("cpu-main", "pub"))

    def test_prepare_yields_to_older_due_publish_but_skips_to_another_account(self):
        self.offer("x:auto:already-ready")
        self.offer("x:auto:new-prep", stage="prepare")
        self.offer("x:auto:other-prep", stage="prepare", resource="x:account:other")
        self.assertEqual(self.claim(stage="prepare")["source_key"], "x:auto:other-prep")
        self.assertEqual(self.claim("hk-gpu")["source_key"], "x:auto:already-ready")

    def test_legacy_prepare_fences_both_stages_until_canonical_completion(self):
        old = self.offer("x:auto:legacy", stage="prepare", legacy_inflight=True)
        self.offer("x:auto:new-prep", stage="prepare")
        self.offer("x:auto:new-pub")
        self.assertIsNone(self.claim(stage="prepare"))
        self.assertIsNone(self.claim("hk-gpu"))
        self.offer("x:auto:legacy", stage="publish", status="published", source_status="published", publish_id="confirmed-post")
        self.assertEqual(self.store.get_job(old["id"])["status"], "completed")
        self.assertIsNotNone(self.claim("hk-gpu"))

    def test_missing_prepare_identity_skips_without_failure_and_can_be_enriched(self):
        missing = self.offer("x:auto:missing", stage="prepare", resource_keys=[])
        self.assertIsNone(self.claim(stage="prepare"))
        self.assertEqual(self.store.get_job(missing["id"])["status"], "queued")
        self.offer("x:auto:other", stage="prepare", resource="x:account:other")
        self.assertEqual(self.claim(stage="prepare")["source_key"], "x:auto:other")
        self.offer("x:auto:missing", stage="prepare", resource="x:account:restored")
        self.assertEqual(self.claim("hk-gpu", "prep", "prepare")["source_key"], "x:auto:missing")

    def test_unstarted_frozen_preparation_defers_to_capable_node_and_releases_fence(self):
        offered = self.offer(stage="prepare", payload={"kind": "x_random", "frozen_recipe": "original"})
        old = self.claim(stage="prepare")
        result = self.store.defer_unstarted(old, ["prepare", "frozen-gpu-profile"])
        self.assertTrue(result["deferred"])
        row = self.store.get_job(offered["id"])
        self.assertEqual((row["status"], row["generation"], row["node_id"]), ("ready", 1, ""))
        self.assertEqual(row["payload"], offered["payload"])
        self.assertIsNone(self.claim(stage="prepare"))
        self.store.register_node("hk-gpu", ["prepare", "frozen-gpu-profile"])
        self.store.worker_heartbeat("hk-gpu", "prep", "prepare")
        current = self.store.claim("hk-gpu", "prep", "prepare")["job"]
        self.assertEqual(current["generation"], 2)
        with self.assertRaises(DispatchError):
            self.store.fail(old, "stale", canonical_no_write=True)
        self.intent(current)
        with self.assertRaisesRegex(DispatchError, "operation_requires_reconciliation"):
            self.store.defer_unstarted(current, ["prepare"])

    def test_historical_prepare_authenticates_receipt_without_mutating_new_claim(self):
        self.offer(stage="prepare")
        old = self.claim(stage="prepare")
        self.time += dt.timedelta(seconds=121)
        current = self.claim("hk-gpu", "prep", "prepare")
        historical = self.store.historical_receipt_claim(old)
        self.assertEqual((historical["generation"], historical["node_id"], historical["worker_id"]),
                         (1, "cpu-main", "0"))
        self.assertEqual(self.store.get_job(current["id"])["generation"], 2)
        for changed in ({"claim_token": "other-token"}, {"node_id": "hk-gpu"}, {"worker_id": "other"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(DispatchError, "claim_conflict"):
                self.store.historical_receipt_claim(dict(old, **changed))
        with self.assertRaises(DispatchError):
            self.store.complete(old, "late", {"artifact": {"url": "https://media.example/a", "sha256": "a" * 64, "size": 10}}, canonical_verified=True)
        self.offer("x:auto:publish-next")
        self.assertIsNone(self.claim("cpu-main", "pub"))

    def test_explicit_youtube_cover_revision_and_retry_rearm_only_same_preparation(self):
        self.store.channel_enabled("youtube", True)
        def cover(revision, **extra):
            return self.offer("youtube:preparation:cover", channel="youtube", family="preparation", stage="prepare",
                resource="youtube:channel:UC1", source_revision=revision, source_status="queued_generation",
                execution_available=True, **extra)
        original = cover("1:")
        first = self.claim(stage="prepare")
        self.store.complete(first, "cover-v1", {"artifact": {
            "url": "https://media.example/cover.jpg", "sha256": "a" * 64, "size": 20}}, canonical_verified=True)
        self.assertEqual(cover("1:")["status"], "completed")
        revised = cover("2:")
        self.assertEqual((revised["id"], revised["generation"], revised["status"], revised["artifact"]),
                         (original["id"], 1, "queued", {}))
        second = self.claim("hk-gpu", "prep", "prepare")
        self.assertEqual(second["generation"], 2)
        self.store.fail(second, "generation_failed", canonical_no_write=True)
        self.assertEqual(cover("2:")["status"], "failed")
        retried = cover("2:" + "c" * 32)
        self.assertEqual((retried["id"], retried["status"], retried["generation"]), (original["id"], "queued", 2))
        third = self.claim("cpu-main", "prep3", "prepare")
        self.assertEqual(third["generation"], 3)
        # An old completion cannot consume or unlock this new owner request.
        with self.assertRaises(DispatchError):
            self.store.complete(first, "late-v1", {"artifact": {
                "url": "https://media.example/cover.jpg", "sha256": "a" * 64, "size": 20}}, canonical_verified=True)

    def test_youtube_cover_revision_never_rearms_paused_unknown_active_or_publish(self):
        for state in ("backlog_paused", "unknown", "legacy_running"):
            key = "youtube:preparation:" + state
            self.offer(key, channel="youtube", family="preparation", stage="prepare", status=state, source_revision="1:")
            observed = self.offer(key, channel="youtube", family="preparation", stage="prepare", source_revision="2:",
                                  source_status="queued_generation", execution_available=True)
            self.assertEqual(observed["status"], state)
        key = "youtube:publish:1"
        self.offer(key, channel="youtube", family="publish", status="completed", source_revision="1:")
        self.assertEqual(self.offer(key, channel="youtube", family="publish", source_revision="2:",
            source_status="queued_generation", execution_available=True)["status"], "completed")

    def test_unknown_expiry_quarantines_account_and_accepts_late_receipt(self):
        self.offer(); claim=self.claim(); self.intent(claim)
        self.offer("x:auto:2")
        self.time+=dt.timedelta(seconds=121)
        self.store.expire()
        self.assertEqual(self.store.get_job(claim["id"])["status"],"unknown")
        self.assertIsNone(self.claim("hk-gpu"))
        result={"publish_id":"123", "canonical_receipt":"123"}
        self.store.complete(claim,"r1",result,canonical_verified=True)
        self.assertTrue(self.store.complete(claim,"r1",result,canonical_verified=True)["duplicate"])
        self.assertIsNotNone(self.claim("hk-gpu"))

    def test_pre_intent_expiry_can_reclaim_but_stale_worker_cannot_write(self):
        self.offer(); old=self.claim(); self.time+=dt.timedelta(seconds=121)
        new=self.claim("hk-gpu")
        self.assertEqual(new["generation"],2)
        with self.assertRaises(DispatchError):self.intent(old)

    def test_same_operation_never_gets_second_write_permit(self):
        self.offer(); c=self.claim();self.intent(c)
        with self.assertRaises(DispatchError):self.intent(c)

    def test_network_error_cannot_clear_resource_hold(self):
        self.offer();c=self.claim();self.intent(c)
        self.store.fail(c,"network_error",unknown=False)
        self.assertEqual(self.store.get_job(c["id"])["status"],"unknown")

    def test_fair_channel_and_batch_rotation(self):
        for n in range(3):self.offer("x:auto:"+str(n),batch="a",resource="x:account:"+str(n))
        self.offer("x:auto:other",batch="b",resource="x:account:99")
        self.offer("fb:auto:1",channel="fb",resource="fb:page:1")
        a=self.claim();b=self.claim("hk-gpu")
        self.assertEqual(a["channel"],"x");self.assertEqual(b["channel"],"fb")
        self.assertEqual(self.claim("cpu-main","1")["batch_key"],"b")

    def test_cutover_pauses_overdue_ready_preserves_future_and_grandfather(self):
        a=self.offer();b=self.offer("x:auto:2",legacy_inflight=True,stage="prepare")
        future=self.offer("x:auto:3",scheduled_at=stamp(self.time+dt.timedelta(hours=1)))
        self.store.cutover("x",stamp(self.time),[b["source_key"]])
        self.assertEqual(self.store.get_job(a["id"])["status"],"backlog_paused")
        self.assertEqual(self.store.get_job(future["id"])["status"],"queued")
        next_stage=self.offer("x:auto:2",stage="publish")
        self.assertEqual(next_stage["status"],"queued")
        self.assertTrue(next_stage["grandfathered"])

    def test_lower_capacity_drains_without_cancelling(self):
        a=self.offer();c=self.claim()
        self.store.set_capacity({"host_id":"cpu-main","stage":"publish","max_workers":0,"expected_version":self.store.config()["version"]},{"is_admin":True})
        self.assertTrue(self.store.heartbeat(c)["accepted"])
        self.offer("x:auto:2",resource="x:account:2")
        self.assertIsNone(self.claim("cpu-main","1"))

    def test_exact_frozen_parent_without_projection_still_exempts_its_child(self):
        child=self.offer("x:queue:5373",stage="prepare",ancestor_source_keys=["x:auto:1861"])
        sibling=self.offer("x:queue:5374",stage="prepare",ancestor_source_keys=["x:auto:1862"])
        self.store.cutover("x",stamp(self.time),["x:auto:1861"])
        self.assertTrue(self.store.get_job(child["id"])["grandfathered"])
        self.assertEqual(self.store.get_job(sibling["id"])["status"],"backlog_paused")

    def test_platform_upload_retires_completed_legacy_preparation(self):
        prep=self.offer("x:queue:5375",stage="prepare",legacy_inflight=True)
        publish=self.offer("x:queue:5375",stage="publish",legacy_inflight=True,source_attempt_count=1)
        self.assertEqual(self.store.get_job(prep["id"])["status"],"completed")
        self.assertEqual(self.store.get_job(publish["id"])["status"],"legacy_running")

    def test_third_node_enrollment_no_code_change(self):
        e=self.store.create_enrollment("third server",{"is_admin":True,"user_id":"admin"})
        auth=self.store.consume_enrollment(e["token"],"third-node")
        self.assertEqual(self.store.authenticate_node(auth["token"])["node_id"],"third-node")
        self.store.register_node("third-node",["publish"],{"cpu_count":16,"gpu_count":0})
        self.store.worker_heartbeat("third-node","1","publish")
        self.offer()
        self.assertIsNotNone(self.store.claim("third-node","1","publish")["job"])
        with self.assertRaises(DispatchError):self.store.consume_enrollment(e["token"],"fourth-node")

    def test_capability_mismatch_never_claims(self):
        self.offer(capabilities=["only_gpu_profile"])
        self.assertIsNone(self.claim())

    def test_canonical_receipt_required(self):
        self.offer();c=self.claim()
        with self.assertRaises(DispatchError):self.store.complete(c,"r1",{"publish_id":"123"})


if __name__=="__main__":unittest.main()

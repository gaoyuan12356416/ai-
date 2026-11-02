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

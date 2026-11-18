import concurrent.futures
import contextlib
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.core import DispatchError, DispatchStore, stamp


class CloudRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(temp.name) / "dispatch.db", clock=lambda: self.now)
        for channel in ("fb", "x", "tt", "youtube"):
            self.store.channel_enabled(channel, True)
        enrollment = self.store.create_enrollment("cos-cloud", {"is_admin": True})
        self.store.consume_enrollment(enrollment["token"], "cos-cloud")
        self.caps = ["prepare:fb", "prepare:x", "prepare:tt", "fb_prepare", "x_preflight", "tt_prepare", "backend:cos"]
        self.health = {"queue_ready": True, "queue_limit": 10}
        self.store.register_node("cos-cloud", self.caps + ["publish:fb", "youtube_prepare"], self.health)
        self.pool("cos-cloud", 4, min_pending=100)
        self.store.worker_heartbeat("cos-cloud", "c1", "prepare")
        self.store.worker_heartbeat("cos-cloud", "c2", "prepare")

    def pool(self, node, count, **extra):
        self.store.set_capacity(dict(expected_version=self.store.config()["version"], host_id=node, stage="prepare",
                                     max_workers=count, scaling_mode="manual", **extra), {"is_admin": True})

    def offer(self, number, channel="fb", **extra):
        key = channel + ":" + str(number)
        return self.store.offer(dict(dict(source_key=key, source_id=str(number), channel=channel, stage="prepare",
                    family="auto" if channel == "fb" else "intake" if channel == "tt" else "queue", source_created_at=stamp(self.now),
                    scheduled_at=stamp(self.now), capabilities=["prepare:" + channel],
                    resource_keys=["account:" + key], batch_key="batch"), **extra))

    def claim(self, node="cos-cloud", worker="c1"):
        return self.store.claim(node, worker, "prepare")

    def test_threshold_filters_and_two_workers_at_equal_boundary(self):
        for index in range(99):
            self.offer(index, "x" if index % 2 else "fb")
        self.offer(201, "youtube")
        self.offer(202, scheduled_at=stamp(self.now + dt.timedelta(hours=1)))
        self.offer(203, capabilities=["prepare:fb", "backend:gpu"])
        self.offer(204, resource_keys=[])
        self.assertEqual(self.claim()["eligible_pending"], 99)
        self.offer(100, "tt")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda w: self.claim(worker=w), ["c1", "c2"]))
        self.assertEqual(sum(r["job"] is not None for r in results), 1)
        self.assertEqual(next(r for r in results if not r["job"])["idle_reason"], "cos_below_min_pending")
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(self.store.cloud_pending(c, self.store.config(c), stamp(self.now)), 99)

    def test_priority_overflow_drains_and_unavailable_queue(self):
        self.pool("cos-cloud", 2, min_pending=0)
        for host, backend in (("hk-gpu", "gpu"), ("cpu-main", "cpu")):
            self.store.register_node(host, ["backend:" + backend, "prepare:fb"])
            self.pool(host, 1)
            self.store.worker_heartbeat(host, "one", "prepare")
        for i in range(3):
            self.offer(i)
        self.assertEqual(self.claim()["job"]["source_id"], "2")
        self.assertEqual(self.claim("cpu-main", "one")["job"]["source_id"], "1")
        self.assertEqual(self.claim("hk-gpu", "one")["job"]["source_id"], "0")
        self.pool("cos-cloud", 0)
        self.offer(4)
        self.assertEqual(self.claim(worker="c2")["idle_reason"], "capacity_limit")
        self.assertEqual(self.store.get_job(3)["status"], "running")
        self.pool("cos-cloud", 2)
        self.store.register_node("cos-cloud", self.caps, {"queue_ready": False})
        self.assertEqual(self.claim(worker="c2")["idle_reason"], "cos_queue_unavailable")

    def test_incompatible_or_drained_peer_does_not_reserve(self):
        self.pool("cos-cloud", 2, min_pending=0)
        self.store.register_node("hk-gpu", ["backend:gpu", "prepare:tt"])
        self.store.worker_heartbeat("hk-gpu", "g", "prepare")
        self.store.register_node("cpu-main", ["backend:cpu", "prepare:fb"])
        self.store.worker_heartbeat("cpu-main", "p", "prepare", draining=True)
        self.offer(1)
        self.assertIsNotNone(self.claim()["job"])

    def test_gpu_only_offer_consumes_first_gpu_slot(self):
        self.pool("cos-cloud", 1, min_pending=0)
        self.store.register_node("hk-gpu", ["backend:gpu", "prepare:fb"])
        self.pool("hk-gpu", 1)
        self.store.worker_heartbeat("hk-gpu", "gpu", "prepare")
        self.offer(1, capabilities=["backend:gpu", "prepare:fb"])
        self.offer(2)
        self.assertEqual(self.claim()["job"]["source_id"], "2")
        self.assertEqual(self.claim("hk-gpu","gpu")["job"]["source_id"], "1")

    def test_cloud_enrollment_is_scoped(self):
        enrollment = self.store.create_enrollment("ordinary host", {"is_admin": True})
        with self.assertRaisesRegex(DispatchError, "enrollment_scope_mismatch"):
            self.store.consume_enrollment(enrollment["token"], "cos-cloud")

    def test_actual_cloud_quota_above_64_controls_admission(self):
        self.store.register_node("cos-cloud", self.caps, {"queue_ready": True, "queue_limit": 65})
        self.pool("cos-cloud", 500, min_pending=0)
        for index in range(66):
            self.offer(index)
            self.store.worker_heartbeat("cos-cloud", "quota-" + str(index), "prepare")
        for index in range(65):
            self.assertIsNotNone(self.claim(worker="quota-" + str(index))["job"])
        self.assertEqual(self.claim(worker="quota-65")["idle_reason"], "cos_queue_limit")
        self.store.set_capacity(dict(expected_version=self.store.config()["version"], host_id="cos-cloud",
                                     stage="prepare", scaling_mode="auto"), {"is_admin": True})
        self.store.register_node("cos-cloud", self.caps, {"queue_ready": True, "queue_limit": 100})
        self.store.adapt_capacity()
        pool = self.store.config()["hosts"]["cos-cloud"]["prepare"]
        self.assertEqual((pool["ceiling"], pool["target"]), (500, 100))

    def test_three_nodes_race_and_legacy_rollback(self):
        self.pool("cos-cloud", 1, min_pending=0)
        offers = [("cos-cloud","c1"),("cpu-main","cpu"),("hk-gpu","gpu")]
        for host, worker in offers[1:]:
            self.store.register_node(host, ["backend:"+worker,"prepare:fb"])
            self.pool(host, 1)
            self.store.worker_heartbeat(host,worker,"prepare")
        self.offer(1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            replies=list(pool.map(lambda pair:self.claim(*pair),offers))
        self.assertEqual([reply["job"]["node_id"] for reply in replies if reply["job"]], ["hk-gpu"])
        original=replies[2]["job"]
        self.store.set_capacity(dict(expected_version=self.store.config()["version"],prepare_routing="legacy"),
                                {"is_admin": True})
        self.pool("cos-cloud", 0)
        self.offer(2)
        self.assertEqual(self.claim("cpu-main","cpu")["job"]["source_id"], "2")
        self.assertTrue(self.store.heartbeat(original)["accepted"])

    def test_stale_capacity_version_does_not_change_threshold(self):
        version = self.store.config()["version"]
        self.pool("cos-cloud", 1, min_pending=100)
        with self.assertRaisesRegex(DispatchError,"capacity_version_conflict"):
            self.store.set_capacity(dict(expected_version=version,host_id="cos-cloud",stage="prepare",
                                         min_pending=0,max_workers=8),{"is_admin": True})
        self.assertEqual(self.store.config()["hosts"]["cos-cloud"]["prepare"]["min_pending"],100)

    def test_peer_slots_do_not_double_book_one_fb_material(self):
        self.pool("cos-cloud",1,min_pending=0)
        self.store.register_node("hk-gpu",["backend:gpu","prepare:fb"])
        self.pool("hk-gpu",2)
        for slot in ["one","two"]:self.store.worker_heartbeat("hk-gpu",slot,"prepare")
        for i,material in enumerate(["same","same","second","third"]):
            self.offer(i,resource_keys=["fb:material:"+material])
        self.assertEqual(self.claim()["job"]["source_id"],"3")
        self.assertEqual(self.claim("hk-gpu","one")["job"]["source_id"],"0")
        self.assertEqual(self.claim("hk-gpu","two")["job"]["source_id"],"2")

    def test_recovery_at_zero_and_fenced_unknown(self):
        self.pool("cos-cloud", 1, min_pending=0)
        self.offer(1)
        job = self.claim()["job"]
        self.store.cloud_intent(dict(job, action_key="a" * 64))
        self.pool("cos-cloud", 0)
        self.now += dt.timedelta(seconds=125)
        self.store.expire()
        self.assertEqual(self.store.get_job(job["id"])["error_code"], "cos_receipt_pending")
        self.assertTrue(self.store.resume_cloud(job)["accepted"])
        self.assertEqual(self.store.get_job(job["id"])["generation"], job["generation"])
        with self.assertRaises(DispatchError):
            self.store.resume_cloud(dict(job, claim_token="wrong"))
        with self.assertRaises(DispatchError):
            self.store.worker_heartbeat("cos-cloud", "bad", "publish")

    def test_canonical_completion_releases_only_this_cloud_projection(self):
        self.pool("cos-cloud",1,min_pending=0)
        self.offer(7,"x")
        job=self.claim()["job"]
        self.store.cloud_intent(dict(job,action_key="a"*64))
        self.store.resume_cloud(job,park=True)
        resource="task:"+job["source_key"]
        with self.store.transaction() as conn:
            row=conn.execute("SELECT * FROM pipeline_job WHERE id=?",(job["id"],)).fetchone()
            self.store._refresh_queue_isolation(conn,row)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM pipeline_resource WHERE resource_key=? AND quarantined=1",(resource,)).fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=?",(resource,)).fetchone())
            # An earlier coordinator projected the same unknown into its legacy table.
            conn.execute("INSERT INTO pipeline_legacy_resource VALUES(?,?)",(resource,job["id"]))
            self.store._refresh_queue_isolation(conn,row)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=?",(resource,)).fetchone())
        self.store.resume_cloud(job)
        self.store.complete(job,"receipt",{"artifact":{"url":"https://fixture.example/ci.mp4","sha256":"b"*64,"size":900},
                                           "canonical_receipt":json.dumps({"source_key":job["source_key"]})},canonical_verified=True)
        with self.store.transaction() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM pipeline_legacy_resource WHERE resource_key=?",(resource,)).fetchone())


if __name__ == "__main__":
    unittest.main()

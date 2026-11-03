"""Offline canonical X ledger, actual source fences and final-artifact recovery."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.service import Coordinator
from features.x_posts.random_template_store import PreparationStore
from scripts import test_pipeline_x_native as native_fixture


class XRecoveryTests(unittest.TestCase):
    setUp = native_fixture.NativeTests.setUp
    verify = native_fixture.NativeTests.verify
    queue = native_fixture.NativeTests.queue
    job = native_fixture.NativeTests.job
    preflight = native_fixture.NativeTests.preflight

    def registry(self):
        adapter = LedgerAdapter(LedgerSpec("x", "queue", str(self.runtime.POST_DB_PATH), "x_post_queue"), self.bridge)
        return adapter, AdapterRegistry([adapter])

    def rendered(self, queue):
        request = PreparationStore(self.runtime.POST_DB_PATH).get(queue["id"])["request"]
        result = {**request, "output_url": "https://media.example.test/rendered.mp4", "output_sha256": "d" * 64,
            "output_size": 3333, "probe": {"duration": 30}, "recipe": {"asset_set_sha256": request["asset_manifest_sha256"]}}
        artifact = {"url": result["output_url"], "sha256": result["output_sha256"], "size": result["output_size"], "mime": "video/mp4",
            "duration_seconds": 30, "profile": result["profile"], "recipe": result["recipe"]}
        return {"artifact": artifact, "preparation": result}

    def test_random_preflight_is_not_qualified_final_artifact(self):
        queue = self.queue(random=True)
        job, _, source = self.preflight(queue)
        adapter, registry = self.registry()
        before = registry.reconcile(job)
        self.assertEqual(before["next_offer"]["stage"], "prepare")
        self.assertFalse(before["next_offer"]["artifact"]["url"])
        self.assertFalse(before["dispatch_completed"])
        receipt = self.rendered(queue)
        # Simulate canonical completion followed by loss of the central ACK.
        self.bridge.complete(job, receipt, job)
        recovered = registry.reconcile(job)
        self.assertEqual(recovered["next_offer"]["stage"], "publish")
        final = recovered["next_offer"]["artifact"]
        for key in ("url", "sha256", "size", "profile", "duration_seconds", "mime"):
            self.assertEqual(final[key], receipt["artifact"][key])
        self.assertNotEqual(final["sha256"], source["sha256"])
        publication = self.job(queue, "publish")
        self.assertEqual(final["sha256"], self.bridge.reserve(publication, publication)["artifact"]["sha256"])

    def test_corrupt_random_receipt_cannot_be_recovered_as_source(self):
        queue = self.queue(random=True)
        job, _, _ = self.preflight(queue)
        receipt = self.rendered(queue)
        self.bridge.complete(job, receipt, job)
        with self.bridge.connect() as conn:
            conn.execute("UPDATE x_post_random_template_preparation SET result_json='{}' WHERE queue_id=?", (queue["id"],))
        adapter, _ = self.registry()
        with self.assertRaises(Exception):
            adapter.read(str(queue["id"]))

    def test_coordinator_recovers_final_render_after_lost_ack(self):
        queue = self.queue(random=True)
        job, _, source = self.preflight(queue)
        adapter, registry = self.registry()
        store = DispatchStore(self.root / "dispatch.sqlite")
        store.register_node("cpu-main", ["prepare:x", "backend:cpu"])
        store.worker_heartbeat("cpu-main", "prepare-1", "prepare")
        store.channel_enabled("x", True)
        store.offer(adapter.project(adapter.read(str(queue["id"]))))
        claimed = store.claim("cpu-main", "prepare-1", "prepare")["job"]
        self.assertIsNotNone(claimed)
        service = Coordinator(store, registry, internal_token="i" * 32, master_key="m" * 32)
        self.assertFalse(service.recover_prepared(claimed))
        receipt = self.rendered(queue)
        self.bridge.complete(job, receipt, job)
        self.assertTrue(service.recover_prepared(claimed))
        recovered = store.get_job(claimed["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["artifact"]["sha256"], receipt["artifact"]["sha256"])
        self.assertNotEqual(recovered["artifact"]["sha256"], source["sha256"])
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_operation").fetchone()[0], 0)

    def test_reclaim_before_preflight_preserves_first_profile_and_catalog(self):
        queue = self.queue(random=True)
        original = self.job(queue, "prepare", backend="cpu")
        frozen = self.bridge.reserve(original, original)
        later = self.job(queue, "prepare", generation=2, backend="gpu")
        self.assertEqual(self.bridge.reserve(later, later), frozen)
        self.assertIn("backend:cpu", frozen["required_capabilities"])

    def test_exact_late_preflight_saves_fact_but_keeps_failed_queue_failed(self):
        queue = self.queue()
        job = self.job(queue, "prepare")
        descriptor = self.bridge.reserve(job, job)
        self.bridge.fail(job, job, {"error_code": "coordinator_unavailable", "unknown_outcome": False})
        artifact = {"url": "https://media.example.test/final.mp4", "sha256": "b" * 64, "size": 2048,
                    "mime": "video/mp4", "duration_seconds": 30, "profile": "source"}
        receipt = {"source_url": descriptor["request"]["source_url"], "artifact": artifact}
        self.assertTrue(self.bridge.checkpoint(job, job, "x_preflight", receipt)["accepted"])
        self.assertTrue(self.bridge.checkpoint(job, job, "x_preflight", receipt)["accepted"])
        self.bridge.complete(job, {"artifact": artifact}, job)
        adapter, registry = self.registry()
        observed = registry.reconcile(job)
        self.assertTrue(observed["definitive_failure"])
        self.assertFalse(observed["dispatch_completed"])
        self.assertEqual(self.bridge.store.get_queue(queue["id"])["status"], "failed")
        with self.bridge.connect() as conn:
            row = conn.execute("SELECT status,attempt_count,unknown_outcome FROM x_post_publish_log WHERE queue_id=?", (queue["id"],)).fetchone()
            self.assertEqual(tuple(row), ("failed", 0, 0))

    def test_late_preflight_never_overrides_attempt_unknown_or_native_intent(self):
        for evidence in ("attempt", "unknown", "intent"):
            with self.subTest(evidence=evidence):
                queue = self.queue(identity=10 + self.serial)
                job = self.job(queue, "prepare")
                descriptor = self.bridge.reserve(job, job)
                self.bridge.fail(job, job, {"error_code": "coordinator_unavailable", "unknown_outcome": False})
                with self.bridge.connect() as conn:
                    if evidence == "intent":
                        conn.execute("INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)", ("operation-" + str(queue["id"]), job["source_key"], 1, "x.media.initialize", "a" * 64, "now"))
                    else:
                        field = "attempt_count" if evidence == "attempt" else "unknown_outcome"
                        conn.execute("UPDATE x_post_publish_log SET " + field + "=1 WHERE queue_id=?", (queue["id"],))
                receipt = {"source_url": descriptor["request"]["source_url"], "artifact": {"url": "https://media.example.test/final.mp4", "sha256": "b" * 64, "size": 2048, "mime": "video/mp4", "duration_seconds": 30, "profile": "source"}}
                with self.assertRaisesRegex(Exception, "x_preflight_publish_already_started"):
                    self.bridge.checkpoint(job, job, "x_preflight", receipt)

    def test_slow_account_verify_does_not_block_other_queue_checkpoint_or_complete(self):
        fast_queue = self.queue(identity=2)
        fast = self.job(fast_queue, "prepare")
        desc = self.bridge.reserve(fast, fast)
        artifact = {"url": "https://media.example.test/final.mp4", "sha256": "b" * 64, "size": 2048,
                    "mime": "video/mp4", "duration_seconds": 30, "width": 720, "height": 1280, "profile": "source"}
        slow_queue = self.queue(identity=3)
        slow = self.job(slow_queue, "prepare")
        entered, release = threading.Event(), threading.Event()
        def verify(identity, *args, **kwargs):
            if identity == 3:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test slow verification was not released")
            return self.verify(identity)
        self.runtime.verify_account = verify
        with ThreadPoolExecutor(max_workers=3) as pool:
            waiting = pool.submit(self.bridge.reserve, slow, slow)
            try:
                self.assertTrue(entered.wait(2))
                checkpoint = pool.submit(self.bridge.checkpoint, fast, fast, "x_preflight", {"source_url": desc["request"]["source_url"], "artifact": artifact})
                self.assertTrue(checkpoint.result(timeout=2)["accepted"])
                completed = pool.submit(self.bridge.complete, fast, {"artifact": artifact}, fast)
                self.assertEqual(completed.result(timeout=2)["status"], "ready")
            finally:
                release.set()
            self.assertEqual(waiting.result(timeout=2)["source_key"], slow["source_key"])

    def test_same_queue_reserve_remains_serial_and_idempotent(self):
        queue = self.queue()
        job = self.job(queue, "prepare")
        barrier = threading.Barrier(6)
        def reserve():
            barrier.wait(timeout=3)
            return self.bridge.reserve(job, job)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: reserve(), range(6)))
        self.assertTrue(all(value == results[0] for value in results))
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_claim").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_publish_log").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

"""Real spawned-process SQLite races and abrupt crash fences; no live I/O."""
import contextlib
import datetime as dt
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from features.pipeline_dispatch.core import DispatchStore, digest, stamp


NOW = dt.datetime(2026, 11, 3, 1, tzinfo=dt.timezone.utc)


def contend(path, node, worker, stage, barrier, send):
    try:
        store = DispatchStore(path, clock=lambda: NOW)
        barrier.wait(timeout=15)
        job = store.claim(node, worker, stage)["job"]
        send.send({"job_id": job["id"] if job else None})
    finally:
        send.close()


def crash_after_intent(path):
    store = DispatchStore(path, clock=lambda: NOW)
    job = store.claim("cpu-main", "pub", "publish")["job"]
    store.begin_operation(job, {"operation_id": "crashed-operation", "operation": "create_post",
        "request_sha256": digest("offline-body"), "account_resource": "x:account:shared"})
    # Model process termination after a durable write intent but before receipt.
    # No fake or real platform endpoint is called: uncertainty alone must fence.
    os._exit(17)


class ProcessRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dispatch.sqlite"
        self.now = NOW
        self.store = DispatchStore(self.path, clock=lambda: self.now)
        for node in ("cpu-main", "hk-gpu"):
            self.store.register_node(node, ["prepare", "publish"])
            for stage in ("prepare", "publish"):
                for index in range(4):
                    self.store.worker_heartbeat(node, stage + str(index), stage)
        self.store.channel_enabled("x", True)
        self.ctx = multiprocessing.get_context("spawn")

    def offer(self, source, stage):
        return self.store.offer({"source_key": "x:queue:" + source, "source_id": source, "channel": "x", "family": "queue",
            "stage": stage, "scheduled_at": stamp(NOW), "source_created_at": stamp(NOW), "resource_keys": ["x:account:shared"], "capabilities": [stage]})

    def race(self, stages):
        barrier = self.ctx.Barrier(len(stages))
        jobs, children = [], []
        try:
            for i, stage in enumerate(stages):
                recv, send = self.ctx.Pipe(duplex=False)
                child = self.ctx.Process(target=contend, args=(str(self.path), ("cpu-main", "hk-gpu")[i % 2], stage + str(i // 2), stage, barrier, send))
                child.start()
                send.close()
                children.append((child, recv))
            for child, recv in children:
                self.assertTrue(recv.poll(20), "spawned claimant did not finish")
                jobs.append(recv.recv()["job_id"])
                child.join(5)
                self.assertEqual(child.exitcode, 0)
            return jobs
        finally:
            for child, recv in children:
                recv.close()
                if child.is_alive():
                    child.terminate()  # Only this test's isolated child process.
                    child.join(5)

    def test_eight_processes_across_hosts_claim_exactly_once(self):
        offered = self.offer("one", "publish")
        winners = [value for value in self.race(["publish"] * 8) if value is not None]
        self.assertEqual(winners, [offered["id"]])
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_claim_history").fetchone()[0], 1)

    def test_prepare_publish_process_race_holds_shared_account_once(self):
        self.offer("source-prepare", "prepare")
        self.offer("source-publish", "publish")
        winners = [value for value in self.race(["prepare", "publish"] * 4) if value is not None]
        self.assertEqual(len(winners), 1)
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_resource").fetchone()[0], 1)

    def test_abrupt_process_exit_after_intent_never_reassigns_publication(self):
        first = self.offer("crashed", "publish")
        self.offer("same-account-next", "publish")
        self.store.worker_heartbeat("cpu-main", "pub", "publish")
        process = self.ctx.Process(target=crash_after_intent, args=(str(self.path),))
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate(); process.join(5)
            self.fail("isolated crash process did not exit")
        self.assertEqual(process.exitcode, 17)
        self.now += dt.timedelta(seconds=121)
        self.store.register_node("hk-gpu", ["prepare", "publish"])
        self.store.worker_heartbeat("hk-gpu", "retry", "publish")
        self.assertIsNone(self.store.claim("hk-gpu", "retry", "publish")["job"])
        self.assertEqual(self.store.get_job(first["id"])["status"], "unknown")
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_operation").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_resource WHERE job_id=?", (first["id"],)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

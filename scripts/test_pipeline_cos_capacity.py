"""The standby worker preserves in-flight execution when the cloud quota changes."""
import concurrent.futures
import os
import threading
import unittest

class WorkerCapacityTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "COS worker runs on Linux")
    def test_thread_capacity_growth_preserves_inflight_work(self):
        from features.pipeline_dispatch.cloud_node import CloudNode
        node = CloudNode.__new__(CloudNode)
        node.worker_limit = 1
        node.workers = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        node.previous_workers = []
        started, release = threading.Event(), threading.Event()

        def original():
            started.set()
            return release.wait(5)

        first = node.workers.submit(original)
        try:
            self.assertTrue(started.wait(2))
            node.ensure_worker_capacity(100)
            self.assertFalse(first.done())
            self.assertEqual(node.workers.submit(lambda: "new").result(2), "new")
            self.assertEqual(node.worker_limit, 100)
            node.ensure_worker_capacity(50)
            self.assertFalse(first.cancelled())
            release.set()
            self.assertTrue(first.result(2))
        finally:
            release.set()
            node.workers.shutdown(wait=True)
            for previous in node.previous_workers:
                previous.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

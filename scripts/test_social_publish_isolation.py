"""Offline concurrency and receipt-protection acceptance tests."""
import threading
import unittest
from scripts.x_post_dispatch import dispatch_queues
from scripts.fb_auto_post_runner import drain


def queue(identity, account, **extra):
    return {"id": identity, "account_id": account, "status": "queued", **extra}


class DispatchTests(unittest.TestCase):
    def test_unknown_and_confirmed_rows_never_write_but_other_accounts_drain(self):
        calls = []
        rows = [queue(1, 1, unknown_outcome=True), queue(2, 1),
                queue(3, 2, status="published"), queue(4, 3),
                queue(5, 4, relay_account_id=1)]
        result, _ = dispatch_queues(rows, lambda q: calls.append(q["id"]) or {**q, "status": "published"}, workers=4)
        self.assertEqual(set(calls), {2, 4, 5})
        self.assertEqual([r["status"] for r in result], ["needs_review", "published", "published", "published", "published"])

    def test_slow_account_does_not_prevent_other_lane_refill(self):
        third_started = threading.Event()
        active, maximum = set(), [0]
        guard = threading.Lock()
        def publish(q):
            with guard:
                self.assertNotIn(q["account_id"], active)
                active.add(q["account_id"])
                maximum[0] = max(maximum[0], len(active))
            if q["id"] == 1:
                self.assertTrue(third_started.wait(3))
            if q["id"] == 3:
                third_started.set()
            with guard:
                active.remove(q["account_id"])
            return {**q, "status": "published"}
        result, _ = dispatch_queues([queue(1, 1), queue(2, 2), queue(3, 3), queue(4, 1)], publish, workers=2)
        self.assertEqual(maximum[0], 2)
        self.assertTrue(all(r["status"] == "published" for r in result))

    def test_unreceipted_exception_isolates_one_queue_without_retry_or_batch_abort(self):
        calls = []
        def publish(q):
            calls.append(q["id"])
            if q["id"] == 1:
                raise TimeoutError("no receipt")
            return {**q, "status": "published"}
        result, _ = dispatch_queues([queue(1, 1), queue(2, 1), queue(3, 2)], publish, workers=1)
        self.assertEqual(calls, [1, 2, 3])
        self.assertTrue(result[0]["unknown_outcome"])

    def test_fb_failed_lane_does_not_abort_healthy_lane_refill(self):
        counts = {0: 0, 1: 0}
        def one(lane):
            counts[lane] += 1
            if lane == 0:
                raise TimeoutError("unconfirmed")
            return {"status": "no_pending" if counts[lane] == 3 else "published"}
        result = drain(one, workers=2, max_tasks=10, max_seconds=5, terminal="no_pending")
        self.assertEqual(counts, {0: 1, 1: 3})
        self.assertEqual(sum(r["status"] == "request_unconfirmed" for r in result), 1)


if __name__ == "__main__":
    unittest.main()

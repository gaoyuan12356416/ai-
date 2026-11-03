"""SQLite receipt delivery regressions; subprocess clients never call a network."""
import contextlib
import json
import multiprocessing
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.client import CoordinatorError
from features.pipeline_dispatch.receipt_outbox import ReceiptOutbox


PAYLOAD = {"job_id": 7, "generation": 3, "claim_token": "fixed-claim", "node_id": "cpu-main",
           "worker_id": "original-worker", "receipt_id": "exact-receipt", "result": {"post_id": "saved-platform-id"}}


class Client:
    def __init__(self, error=None, response=None):
        self.calls, self.error, self.response = [], error, response

    def post(self, route, payload):
        self.calls.append((route, payload))
        if self.error:
            raise self.error
        return {"accepted": True} if self.response is None else self.response


def _flush_process(path, ready, go, entered, release, results, lease_seconds=10):
    outbox = ReceiptOutbox(path, lease_seconds=lease_seconds)

    class BlockingClient:
        def post(self, route, payload):
            entered.put((route, payload))
            if not release.wait(12):
                raise RuntimeError("test callback gate timed out")
            return {"accepted": True}

    ready.put(True)
    go.wait(12)
    results.put(outbox.flush(BlockingClient(), only="exact-receipt"))


class ReceiptOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "outbox.sqlite3"
        self.ctx = multiprocessing.get_context("spawn")

    def row(self, identity="exact-receipt"):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM receipt_outbox WHERE id=?", (identity,)).fetchone())

    def processes(self, count, *, lease_seconds=10):
        ready, entered, results = self.ctx.Queue(), self.ctx.Queue(), self.ctx.Queue()
        go, release = self.ctx.Event(), self.ctx.Event()
        children = [self.ctx.Process(target=_flush_process, args=(str(self.path), ready, go, entered, release, results, lease_seconds)) for _ in range(count)]
        for child in children:
            child.start()
            self.addCleanup(self.stop_child, child)
        for _ in children:
            self.assertTrue(ready.get(timeout=10))
        go.set()
        return children, entered, release, results

    @staticmethod
    def stop_child(child):
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)

    def test_real_processes_deliver_one_receipt_once_and_competitors_do_not_ack(self):
        outbox = ReceiptOutbox(self.path)
        outbox.put("jobs/complete", PAYLOAD)
        children, entered, release, results = self.processes(3)
        self.assertEqual(entered.get(timeout=5), ("jobs/complete", PAYLOAD))
        self.assertFalse(results.get(timeout=5))
        self.assertFalse(results.get(timeout=5))
        self.assertIsNone(self.row()["acked_at"])
        with self.assertRaises(queue.Empty):
            entered.get(timeout=.1)
        release.set()
        self.assertTrue(results.get(timeout=5))
        for child in children:
            child.join(timeout=5)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertIsNotNone(self.row()["acked_at"])
        client = Client()
        self.assertTrue(ReceiptOutbox(self.path).flush(client))
        self.assertEqual(client.calls, [])

    def test_process_death_reclaims_after_lease_and_replays_only_original_callback(self):
        outbox = ReceiptOutbox(self.path)
        outbox.put("jobs/complete", PAYLOAD)
        children, entered, _, _ = self.processes(1, lease_seconds=1)
        self.assertEqual(entered.get(timeout=5), ("jobs/complete", PAYLOAD))
        self.stop_child(children[0])
        client = Client()
        self.assertFalse(outbox.flush(client, only="exact-receipt"))
        self.assertEqual(client.calls, [])
        time.sleep(max(0, self.row()["delivery_lease_until"] - time.time()) + .06)
        self.assertTrue(outbox.flush(client))
        self.assertEqual(client.calls, [("jobs/complete", PAYLOAD)])
        self.assertEqual(json.loads(self.row()["payload"]), PAYLOAD)
        self.assertEqual(self.row()["attempts"], 2)

    def test_slow_callback_renews_lease_in_another_process(self):
        outbox = ReceiptOutbox(self.path)
        outbox.put("jobs/complete", PAYLOAD)
        _, entered, release, results = self.processes(1, lease_seconds=.6)
        entered.get(timeout=5)
        time.sleep(1.3)
        client = Client()
        self.assertFalse(outbox.flush(client, only="exact-receipt"))
        self.assertEqual(client.calls, [])
        self.assertGreater(self.row()["delivery_lease_until"], time.time())
        release.set()
        self.assertTrue(results.get(timeout=5))

    def test_background_deferral_allows_inline_only_but_no_other_owner_or_failure_bypass(self):
        now = [100.0]
        outbox = ReceiptOutbox(self.path, clock=lambda: now[0])
        outbox.put("jobs/complete", PAYLOAD, defer_background_seconds=15)
        client = Client(error=CoordinatorError("canonical_reconciliation_pending", 409))
        self.assertFalse(outbox.flush(client))
        self.assertEqual(client.calls, [])
        with self.assertRaises(CoordinatorError):
            outbox.flush(client, only="exact-receipt")
        self.assertFalse(outbox.flush(client, only="exact-receipt"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.row()["last_error_code"], "canonical_reconciliation_pending")
        now[0] = 115
        client.error = None
        self.assertTrue(outbox.flush(client))
        self.assertEqual(len(client.calls), 2)

    def test_failure_backoff_is_bounded_and_reput_never_resets_retry_or_facts(self):
        now = [100.0]
        outbox = ReceiptOutbox(self.path, clock=lambda: now[0])
        outbox.put("jobs/complete", PAYLOAD)
        client = Client(response={"accepted": False, "code": "canonical_reconciliation_pending"})
        for attempt, delay in enumerate((15, 30, 60, 60), 1):
            self.assertFalse(outbox.flush(client))
            saved = self.row()
            self.assertEqual(saved["attempts"], attempt)
            self.assertEqual(saved["next_attempt_at"], now[0] + delay)
            outbox.put("jobs/complete", PAYLOAD, defer_background_seconds=999)
            self.assertEqual(self.row(), saved)
            self.assertFalse(outbox.flush(client, only="exact-receipt"))
            self.assertEqual(len(client.calls), attempt)
            now[0] += delay
        self.assertEqual(json.loads(self.row()["payload"]), PAYLOAD)
        with self.assertRaisesRegex(ValueError, "changed facts"):
            outbox.put("jobs/fail", PAYLOAD)
        client.response = {"accepted": True}
        self.assertTrue(outbox.flush(client))

    def test_pending_checkpoint_globally_blocks_same_claim_not_other_claim(self):
        now = [100.0]
        outbox = ReceiptOutbox(self.path, clock=lambda: now[0])
        checkpoint = {"job_id": 7, "generation": 3, "checkpoint_id": "first", "receipt": {"post_id": "saved-platform-id"}}
        outbox.put("jobs/checkpoint", checkpoint)
        outbox.put("jobs/complete", PAYLOAD)
        outbox.put("jobs/complete", {**PAYLOAD, "job_id": 8, "receipt_id": "other"})
        held = outbox._claim_next("first")
        client = Client()
        independent = ReceiptOutbox(self.path, clock=lambda: now[0])
        self.assertFalse(independent.flush(client, only="exact-receipt"))
        self.assertFalse(independent.flush(client))
        self.assertEqual([payload["job_id"] for _, payload in client.calls], [8])
        self.assertTrue(outbox._ack("first", held[-1]))
        self.assertTrue(independent.flush(client))
        self.assertEqual(client.calls[-1], ("jobs/complete", PAYLOAD))

    def test_expired_owner_cannot_ack_or_release_new_delivery(self):
        now = [100.0]
        outbox = ReceiptOutbox(self.path, lease_seconds=2, clock=lambda: now[0])
        outbox.put("jobs/complete", PAYLOAD)
        original = outbox._claim_next("exact-receipt")
        now[0] = 103
        self.assertFalse(outbox._ack("exact-receipt", original[-1]))
        current = outbox._claim_next("exact-receipt")
        outbox._fail("exact-receipt", original[-1], 1, "late_response")
        self.assertEqual(self.row()["delivery_owner"], current[-1])
        self.assertTrue(outbox._ack("exact-receipt", current[-1]))

    def test_legacy_schema_migrates_without_changing_saved_or_acked_receipts(self):
        raw = json.dumps(PAYLOAD, ensure_ascii=False, indent=2)
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE receipt_outbox (id TEXT PRIMARY KEY,route TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,acked_at REAL)")
            conn.execute("INSERT INTO receipt_outbox VALUES(?,?,?,?,NULL)", ("exact-receipt", "jobs/complete", raw, 42))
            conn.execute("INSERT INTO receipt_outbox VALUES(?,?,?,?,?)", ("done", "jobs/complete", raw, 40, 41))
        outbox = ReceiptOutbox(self.path)
        self.assertEqual(self.row()["payload"], raw)
        self.assertEqual(self.row()["created_at"], 42)
        self.assertEqual(self.row("done")["acked_at"], 41)
        client = Client()
        self.assertTrue(outbox.flush(client))
        self.assertEqual(client.calls, [("jobs/complete", PAYLOAD)])
        self.assertEqual(self.row()["payload"], raw)

    def test_one_flush_delivers_at_most_100_callbacks(self):
        outbox = ReceiptOutbox(self.path)
        for index in range(101):
            outbox.put("jobs/complete", {"receipt_id": str(index)})
        client = Client()
        self.assertFalse(outbox.flush(client))
        self.assertEqual(len(client.calls), 100)
        self.assertTrue(outbox.flush(client))
        self.assertEqual(len(client.calls), 101)

    def test_error_diagnostics_do_not_store_response_secrets(self):
        outbox = ReceiptOutbox(self.path)
        outbox.put("jobs/complete", PAYLOAD)
        self.assertFalse(outbox.flush(Client(response={"ok": False, "error": "https://secret.invalid/?token=private"})))
        self.assertEqual(self.row()["last_error_code"], "receipt_not_accepted")

    def test_only_explicit_positive_ack_releases_durable_receipt(self):
        now = [100.0]
        outbox = ReceiptOutbox(self.path, clock=lambda: now[0])
        outbox.put("jobs/complete", PAYLOAD)
        for response in ({}, {"unexpected": True}, {"ok": True}, {"accepted": 1}, {"accepted": True, "ok": False}):
            with self.subTest(response=response):
                self.assertFalse(outbox.flush(Client(response=response)))
                self.assertIsNone(self.row()["acked_at"])
                self.assertEqual(json.loads(self.row()["payload"]), PAYLOAD)
                now[0] = self.row()["next_attempt_at"]
        self.assertTrue(outbox.flush(Client(response={"accepted": True, "duplicate": True})))


if __name__ == "__main__":
    unittest.main()

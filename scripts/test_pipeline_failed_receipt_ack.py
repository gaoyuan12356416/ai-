"""Real HTTP/SQLite delivery after reserve failed before native binding."""
import contextlib
import datetime as dt
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.client import CoordinatorClient, CoordinatorError
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.service import Coordinator, handler_type


class UnboundNative:
    fail_calls = 0
    def reserve(self, job, claim):
        raise AdapterError("x_dispatch_failed")
    def reconcile(self, job):
        return {"canonical_receipt": {"source_key": job["source_key"], "status": "reserved"}}
    def fail(self, job, claim):
        self.fail_calls += 1
        raise AdapterError("x_dispatch_claim_conflict")


class FailedReceiptAckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.db", lambda: self.now)
        self.native = UnboundNative()
        self.coordinator = Coordinator(self.store, self.native, internal_token="i" * 32, master_key="m" * 32)
        self.store.register_node("cpu-main", ["publish:x", "backend:cpu"])
        self.store.worker_heartbeat("cpu-main", "one", "publish")
        self.store.channel_enabled("x", True)
        self.store.offer({"source_key": "x:queue:1", "source_id": "1", "channel": "x", "family": "queue",
                          "stage": "publish", "resource_keys": ["x:account:1"], "capabilities": ["publish:x"],
                          "scheduled_at": stamp(self.now), "source_created_at": stamp(self.now)})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(self.coordinator))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = CoordinatorClient("http://127.0.0.1:" + str(self.server.server_port), "i" * 32)
        job = self.client.post("jobs/claim", {"node_id": "cpu-main", "worker_id": "one", "stage": "publish", "reserve_separately": True})["job"]
        self.claim = {"job_id": job["id"], **{k: job[k] for k in ("generation", "node_id", "worker_id", "claim_token")}}
        with self.assertRaises(CoordinatorError) as error:
            self.client.post("jobs/reserve", self.claim)
        self.assertEqual("x_dispatch_failed", error.exception.code)
        self.receipt = {**self.claim, "receipt_id": "same-failure", "error_code": "x_dispatch_failed",
                        "unknown_outcome": False, "metrics": {"selection_seconds": 42}}

    def snapshot(self):
        with contextlib.closing(self.store.connect()) as c:
            return {name: [tuple(r) for r in c.execute("SELECT * FROM " + name)] for name in
                    ("pipeline_job", "pipeline_resource", "pipeline_event", "pipeline_phase_metric", "pipeline_receipt", "pipeline_publication")}

    def test_duplicate_failure_ack_leaves_facts_and_fences_unchanged(self):
        before = self.snapshot()
        self.now += dt.timedelta(hours=6)
        response = self.client.post("jobs/fail", self.receipt)
        self.assertTrue(response["accepted"])
        self.assertTrue(response["duplicate"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0, self.native.fail_calls)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(1, c.execute("SELECT count(*) FROM pipeline_delivery").fetchone()[0])
        self.assertTrue(self.client.post("jobs/fail", self.receipt)["duplicate"])
        self.assertEqual(before, self.snapshot())

    def assert_not_acknowledged(self, receipt, code=None):
        before = self.snapshot()
        with self.assertRaises(CoordinatorError) as error:
            self.client.post("jobs/fail", receipt)
        if code:
            self.assertEqual(code, error.exception.code)
        self.assertEqual(before, self.snapshot())
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(0, c.execute("SELECT count(*) FROM pipeline_delivery").fetchone()[0])

    def test_wrong_claim_identity_or_generation_is_rejected(self):
        for changed in ({"claim_token": "wrong" * 8}, {"node_id": "hk-gpu"}, {"worker_id": "other"}, {"generation": 2}):
            with self.subTest(changed=list(changed)):
                self.assert_not_acknowledged({**self.receipt, **changed}, "claim_conflict")

    def test_different_error_or_unknown_receipt_requires_native_path(self):
        # Exclude optional node metrics so a rejected ordinary fail path is
        # compared solely on its task/fence/receipt facts.
        for change in ({"error_code": "other_failure"}, {"unknown_outcome": True}):
            with self.subTest(change=change):
                self.assert_not_acknowledged({**self.receipt, "metrics": {}, **change})
        self.assertEqual(2, self.native.fail_calls)

    def test_unknown_job_and_external_intent_are_not_short_circuited(self):
        for status, intent in (("unknown", 0), ("failed", 1)):
            with self.subTest(status=status, intent=intent):
                with self.store.transaction() as c:
                    c.execute("UPDATE pipeline_job SET status=?,external_intent=? WHERE id=?", (status, intent, self.claim["job_id"]))
                self.assert_not_acknowledged({**self.receipt, "metrics": {}})

    def test_any_prior_operation_prevents_no_write_ack(self):
        with self.store.transaction() as c:
            c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      ("old-intent", self.claim["job_id"], 0, "cpu-main", "create", "a" * 64, "x:account:1", stamp(self.now), stamp(self.now)))
        self.assert_not_acknowledged({**self.receipt, "metrics": {}})

    def test_migrated_native_evidence_prevents_no_write_ack(self):
        for evidence in ({"source_attempt_count": 1}, {"reconcile_only": True}, {"unknown_outcome": True}, {"publish_id": "old-post"}, {"legacy_inflight": True}):
            with self.subTest(evidence=evidence):
                with self.store.transaction() as c:
                    c.execute("UPDATE pipeline_job SET source_meta_json=? WHERE id=?", (json.dumps(evidence), self.claim["job_id"]))
                self.assert_not_acknowledged({**self.receipt, "metrics": {}})
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET source_meta_json='{}',canonical_receipt=? WHERE id=?", (json.dumps({"status": "submitted"}), self.claim["job_id"]))
        self.assert_not_acknowledged({**self.receipt, "metrics": {}})

    def test_existing_checkpoint_or_receipt_prevents_no_write_ack(self):
        for table in ("pipeline_checkpoint", "pipeline_receipt"):
            with self.subTest(table=table):
                with self.store.transaction() as c:
                    if table == "pipeline_checkpoint":
                        c.execute("INSERT INTO pipeline_checkpoint VALUES(?,?,?,?,?,?)", ("old-proof", self.claim["job_id"], 0, "x_media_initialized", "a" * 64, stamp(self.now)))
                    else:
                        c.execute("INSERT INTO pipeline_receipt VALUES(?,?,?,?,?)", ("old-proof", self.claim["job_id"], 0, "a" * 64, stamp(self.now)))
                self.assert_not_acknowledged({**self.receipt, "metrics": {}})
                with self.store.transaction() as c:
                    c.execute("DELETE FROM " + table)

    def test_changed_delivery_body_cannot_reuse_ack_id(self):
        self.client.post("jobs/fail", self.receipt)
        with self.assertRaises(CoordinatorError) as error:
            self.client.post("jobs/fail", {**self.receipt, "error_code": "changed"})
        self.assertEqual("delivery_identity_conflict", error.exception.code)


if __name__ == "__main__":
    unittest.main()

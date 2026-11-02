"""Idle native observations must converge without clearing execution holds."""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from features.pipeline_dispatch.adapters import ClosingConnection, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore


class LegacyConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 2, 11, 20, tzinfo=dt.timezone.utc)
        self.cutoff = "2026-11-02T11:17:02+00:00"
        self.path = self.root / "x-auto.sqlite"
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.executescript("""
                CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,status TEXT,account_id INTEGER,
                  claim_phase TEXT,claim_worker TEXT,lease_expires_at_utc TEXT,
                  execution_queue_id INTEGER,publish_attempt_count INTEGER DEFAULT 0,
                  unknown_outcome INTEGER DEFAULT 0,publish_id TEXT DEFAULT '',created_at TEXT,
                  scheduled_at_utc TEXT,started_at TEXT,updated_at TEXT);
                CREATE TABLE pipeline_canonical_hold(source_key TEXT PRIMARY KEY,cutoff TEXT,reason TEXT);
                INSERT INTO x_auto_task VALUES(1866,'selecting',50,'selection','old-worker','2026-11-02T11:14:00+00:00',
                  NULL,0,0,'','2026-10-30T13:40:07+00:00','2026-11-02T11:12:00+00:00',
                  '2026-11-02T11:12:00+00:00','2026-11-02T11:12:00+00:00');
            """)
        self.adapter = LedgerAdapter(LedgerSpec("x", "auto", str(self.path), "x_auto_task", module="x_accounts"), SimpleNamespace(control_only=True))
        self.store = DispatchStore(self.root / "dispatch.sqlite", clock=lambda: self.now)
        self.original = self.store.offer(self.project())

    def project(self):
        return self.adapter.project(self.adapter.read(1866), self.now)

    def idle(self, held=False):
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("UPDATE x_auto_task SET status='retry_wait',claim_worker='',lease_expires_at_utc='',updated_at='2026-11-02T11:14:04+00:00'")
            if held:
                conn.execute("INSERT INTO pipeline_canonical_hold VALUES('x:auto:1866',?,'cutover_overdue')", (self.cutoff,))
        return self.project()

    def fences(self):
        with self.store.transaction() as conn:
            return [tuple(row) for row in conn.execute("SELECT resource_key,job_id FROM pipeline_legacy_resource ORDER BY job_id")]

    def test_native_retry_wait_with_exact_hold_replaces_stale_running_and_false_exemption(self):
        self.store.cutover("x", self.cutoff)
        self.assertTrue(self.store.get_job(self.original["id"])["grandfathered"])
        other = self.store.offer({**self.project(), "source_key": "x:auto:other", "source_id": "other", "status": "unknown", "legacy_inflight": False, "unknown_outcome": True})
        before = self.adapter.read(1866)
        offer = self.idle(held=True)
        native_after_hold = self.adapter.read(1866)
        self.assertEqual(offer["status"], "backlog_paused")
        result = self.store.offer(offer)
        self.assertEqual((result["status"], result["hold_reason"], result["grandfathered"]), ("backlog_paused", "cutover_overdue", 0))
        self.assertEqual(result["generation"], 0)
        self.assertEqual(self.fences(), [("x:account:50", other["id"])])
        self.assertEqual(self.adapter.read(1866), native_after_hold)
        self.assertEqual(before["execution_queue_id"], native_after_hold["execution_queue_id"])
        self.store.offer(offer)
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_event WHERE job_id=? AND event_type='legacy_idle_observed'", (result["id"],)).fetchone()[0], 1)

    def test_idle_retry_is_waiting_not_completed_and_keeps_control_only_gate(self):
        result = self.store.offer(self.idle())
        self.assertEqual((result["status"], result["hold_reason"]), ("queued", "adapter_unavailable"))
        self.assertEqual(self.fences(), [])
        self.assertEqual(result["finished_at"], "")
        with self.store.transaction() as conn:
            meta = json.loads(conn.execute("SELECT source_meta_json FROM pipeline_job WHERE id=?", (result["id"],)).fetchone()[0])
        self.assertFalse(meta["legacy_inflight"])

    def test_native_ambiguity_or_attempt_does_not_release_legacy_fence(self):
        offer = self.idle(held=True)
        for extra in ({"unknown_outcome": True}, {"reconcile_only": True}, {"source_attempt_count": 1}, {"legacy_inflight": True}, {"publish_id": "known-post"}):
            with self.subTest(extra=extra):
                result = self.store.offer({**offer, **extra})
                self.assertEqual(result["status"], "legacy_running")
                self.assertEqual(len(self.fences()), 1)

    def test_dispatch_generation_intent_claim_and_unknown_are_never_reopened(self):
        offer = self.idle(held=True)
        for field, value in (("generation", 1), ("external_intent", 1), ("claim_hash", "claimed"), ("canonical_receipt", "verified"), ("status", "unknown")):
            with self.subTest(field=field):
                with self.store.transaction() as conn:
                    conn.execute("UPDATE pipeline_job SET generation=0,external_intent=0,claim_hash='',canonical_receipt='',status='legacy_running' WHERE id=?", (self.original["id"],))
                    conn.execute("UPDATE pipeline_job SET " + field + "=? WHERE id=?", (value, self.original["id"]))
                result = self.store.offer(offer)
                self.assertEqual(result["status"], "unknown" if field == "status" else "legacy_running")
                self.assertEqual(len(self.fences()), 1)

    def test_saved_claim_history_requires_reconciliation_even_at_generation_zero(self):
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO pipeline_claim_history VALUES(?,1,'cpu-main','p1','old-hash')", (self.original["id"],))
        result = self.store.offer(self.idle(held=True))
        self.assertEqual(result["status"], "legacy_running")
        self.assertEqual(len(self.fences()), 1)


if __name__ == "__main__":
    unittest.main()

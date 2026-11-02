"""Cutover admission tests against actual SQLite canonical rows and hold writes."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.adapters import AdapterError, AdapterRegistry, ClosingConnection, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.canonical_holds import SCHEMA, pause_row
from features.pipeline_dispatch.core import DispatchError, DispatchStore, stamp
from features.pipeline_dispatch.cutover import Cutover
from features.pipeline_dispatch.service import Coordinator


class CanonicalAdapter(LedgerAdapter):
    def enrich(self, conn, raw):
        row = super().enrich(conn, raw)
        if row.get("parent_id"):
            row["ancestor_source_keys"] = ["tt:auto:" + str(row["parent_id"])]
        return row


class CanonicalPause:
    def __init__(self, path):
        self.path, self.fail = path, False

    def pause(self, job, cutoff):
        if self.fail:
            raise AdapterError("fixture_pause_unavailable")
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(SCHEMA)
            return pause_row(conn, job, cutoff, table="tt_auto_task", identity="id", state="status", allowed={"pending", "ready"})


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone.utc)
        self.cutoff = stamp(self.now)
        self.path = str(Path(self.temp.name) / "canonical.sqlite")
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("""CREATE TABLE tt_auto_task(id INTEGER PRIMARY KEY,status TEXT,
                account_id TEXT,run_id INTEGER,scheduled_at_utc TEXT,created_at TEXT,
                started_at_utc TEXT,parent_id INTEGER,publish_attempt_count INTEGER DEFAULT 0,
                unknown_outcome INTEGER DEFAULT 0,publish_id TEXT DEFAULT '')""")
        self.driver = CanonicalPause(self.path)
        self.adapter = CanonicalAdapter(LedgerSpec("tt", "auto", self.path, "tt_auto_task", module="tt_posts"), self.driver)
        self.registry = AdapterRegistry([self.adapter])
        self.store = DispatchStore(Path(self.temp.name) / "dispatch.sqlite", lambda: self.now)
        self.coordinator = Coordinator(self.store, self.registry, internal_token="i" * 32, master_key="m" * 32)
        self.control = Cutover(self.coordinator)

    def add(self, ident, status="pending", parent=None, scheduled=None, created=None, started=None):
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("INSERT INTO tt_auto_task(id,status,account_id,run_id,scheduled_at_utc,created_at,started_at_utc,parent_id) VALUES(?,?,?,?,?,?,?,?)",
                         (ident, status, "same-account", 7, scheduled or self.cutoff, created or self.cutoff, started, parent))

    def plan(self, **kwargs):
        return self.control.plan("tt", self.cutoff, **kwargs)

    def apply(self, plan):
        return self.control.apply(plan["plan_id"], plan["sha256"])

    def enable(self, plan):
        return self.control.enable(plan["plan_id"], plan["sha256"])

    def job(self, ident):
        with self.store.transaction() as conn:
            return dict(conn.execute("SELECT * FROM pipeline_job WHERE source_key=? ORDER BY id DESC LIMIT 1", ("tt:auto:" + str(ident),)).fetchone())

    def test_exact_item_and_descendants_exempt_not_shared_batch_or_account(self):
        self.add(1, "preparing", started=self.cutoff)
        self.add(2, parent=1)
        self.add(3, parent=2)
        self.add(4)
        plan = self.plan()
        self.assertEqual(plan["grandfathered_source_keys"], ["tt:auto:1", "tt:auto:2", "tt:auto:3"])
        self.assertEqual(plan["paused_source_keys"], ["tt:auto:4"])
        self.assertEqual(self.apply(plan)["status"], "held")
        self.assertEqual(self.job(4)["status"], "backlog_paused")
        self.assertEqual(self.job(2)["status"], "queued")
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            self.assertEqual(conn.execute("SELECT source_key FROM pipeline_canonical_hold").fetchall(), [("tt:auto:4",)])
            self.assertEqual(conn.execute("SELECT status FROM tt_auto_task WHERE id=4").fetchone()[0], "pending")

    def test_pause_failure_cannot_enable_and_same_manifest_can_retry(self):
        self.add(1)
        plan = self.plan()
        self.driver.fail = True
        self.assertEqual(self.apply(plan)["status"], "partial")
        with self.assertRaisesRegex(DispatchError, "canonical_pauses_incomplete"):
            self.enable(plan)
        self.assertNotIn("tt", self.store.config()["enabled_channels"])
        self.driver.fail = False
        self.assertEqual(self.apply(plan)["status"], "held")
        self.assertTrue(self.enable(plan)["enabled"])

    def test_inflight_must_drain_before_enable(self):
        self.add(1, "preparing", started=self.cutoff)
        plan = self.plan()
        self.apply(plan)
        with self.assertRaisesRegex(DispatchError, "legacy_items_still_running"):
            self.enable(plan)
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("UPDATE tt_auto_task SET status='completed' WHERE id=1")
        self.assertTrue(self.enable(plan)["enabled"])

    def test_future_schedule_or_creation_is_not_paused(self):
        future = stamp(self.now + dt.timedelta(seconds=1))
        self.add(1, scheduled=future)
        self.add(2, created=future)
        self.add(3)
        plan = self.plan()
        self.assertEqual(plan["paused_source_keys"], ["tt:auto:3"])
        self.apply(plan)
        self.assertEqual(self.job(1)["status"], "queued")
        self.assertEqual(self.job(2)["status"], "queued")
        self.assertTrue(self.enable(plan)["enabled"])

    def test_repeated_apply_preserves_one_hold_and_completed_exemption(self):
        self.add(1, "preparing", started=self.cutoff)
        self.add(2)
        plan = self.plan()
        self.apply(plan)
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("UPDATE tt_auto_task SET status='completed' WHERE id=1")
        self.coordinator.scan()
        self.assertEqual(self.apply(plan)["status"], "held")
        self.assertEqual(self.job(1)["grandfathered"], 1)
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_canonical_hold").fetchone()[0], 1)

    def test_historical_cutoff_recomputes_precollection_exemptions(self):
        self.add(1, "preparing", started=stamp(self.now + dt.timedelta(seconds=1)))
        self.add(2, parent=1)
        self.coordinator.scan()
        self.assertEqual(self.job(1)["grandfathered"], 1)
        plan = self.plan()
        self.assertEqual(plan["grandfathered_source_keys"], [])
        self.apply(plan)
        self.assertEqual(self.job(1)["grandfathered"], 0)
        self.assertEqual(self.job(2)["status"], "backlog_paused")

    def test_explicit_single_exemption_is_preserved(self):
        self.add(1)
        self.add(2)
        plan = self.plan(grandfathered=["tt:auto:1"])
        self.apply(plan)
        self.assertEqual(self.job(1)["grandfathered"], 1)
        self.assertEqual(self.job(2)["status"], "backlog_paused")

    def test_full_native_read_catches_inflight_outside_incremental_scan(self):
        plan = self.plan()
        self.apply(plan)
        self.add(999, "preparing", started=self.cutoff)
        self.coordinator.scan = lambda: None
        with self.assertRaisesRegex(DispatchError, "legacy_items_still_running"):
            self.enable(plan)
        self.assertNotIn("tt", self.store.config()["enabled_channels"])

    def test_new_overdue_native_row_requires_another_reviewed_plan(self):
        plan = self.plan()
        self.apply(plan)
        self.add(999)
        with self.assertRaisesRegex(DispatchError, "cutover_unplanned_backlog"):
            self.enable(plan)

    def test_missing_native_source_never_enables_from_stale_projection(self):
        plan = self.plan()
        self.apply(plan)
        with sqlite3.connect(self.path, factory=ClosingConnection) as conn:
            conn.execute("DROP TABLE tt_auto_task")
        with self.assertRaisesRegex(DispatchError, "cutover_source_unavailable"):
            self.enable(plan)
        self.assertNotIn("tt", self.store.config()["enabled_channels"])

    def test_plan_hash_and_disabled_admission_are_enforced(self):
        plan = self.plan()
        with self.assertRaisesRegex(DispatchError, "cutover_plan_identity_changed"):
            self.control.apply(plan["plan_id"], "changed")
        self.store.channel_enabled("tt", True)
        with self.assertRaisesRegex(DispatchError, "cutover_channel_must_stop_claims"):
            self.apply(plan)


if __name__ == "__main__":
    unittest.main()

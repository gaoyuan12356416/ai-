"""Exact old-work admission against real X/TT ledgers; no media/platform I/O."""
import concurrent.futures
import datetime as dt
import json
import sqlite3
import unittest

from features.pipeline_dispatch.backlog_contract import (
    SCHEMA, authorization_identity, source_fingerprint, disposition_record,
)
from scripts import test_x_auto_post_publisher as xfixtures
from scripts import test_tt_auto_post_publisher as ttfixtures


class BacklogNativeTests(unittest.TestCase):
    def x(self):
        fixture = xfixtures.XAutoPostPublisherTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        return fixture

    def tt(self):
        fixture = ttfixtures.TTAutoPostPublisherIntegrationTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        return fixture

    def save(self, store, channel, task_id, *, action="resume", state="committed"):
        with store._transaction() as conn:
            row = conn.execute("SELECT * FROM " + channel + "_auto_task WHERE id=?", (task_id,)).fetchone()
            identity = authorization_identity(channel, "auto", row)
            conn.execute(SCHEMA)
            conn.execute("INSERT INTO pipeline_backlog_disposition VALUES(?,?,?,?,?,?,?,?)", (
                identity["source_key"], "a" * 64, action, state, source_fingerprint(identity),
                identity["scheduled_at"], identity["created_at"], json.dumps({"identity": identity})))
        return identity

    def test_old_database_read_is_nonmutating_and_time_requires_timezone(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertIsNone(disposition_record(conn, "x:auto:1", {}))
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            authorization_identity("x", "auto", dict(id=1, account_id="a", created_at="2026-11-02", scheduled_at_utc="2026-11-02"))

    def test_x_exact_authorization_crosses_only_planning_boundary_and_once(self):
        f = self.x(); boundary = f.clock() + dt.timedelta(seconds=1)
        kwargs = dict(worker_id="planner", lease_seconds=120, publish_delegated=True, planning_cutoff=boundary)
        self.assertIsNone(f.store.claim_next_executable_task(**kwargs))
        before = self.save(f.store, "x", f.task.id)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claimed = list(pool.map(lambda _: f.store.claim_next_executable_task(**kwargs), range(2)))
        self.assertEqual(sum(value is not None for value in claimed), 1)
        claim = next(value for value in claimed if value is not None)
        self.assertEqual((claim.task.id, claim.claim_phase), (f.task.id, "selection"))
        with f.store._reader() as conn:
            row = conn.execute("SELECT * FROM x_auto_task WHERE id=?", (f.task.id,)).fetchone()
            after = authorization_identity("x", "auto", row)
            self.assertEqual((after["scheduled_at"], after["created_at"]), (before["scheduled_at"], before["created_at"]))
            self.assertEqual(row["publish_attempt_count"], 0)

    def test_resume_does_not_remove_hold(self):
        f = self.x(); f.store.pause_pipeline_task(f.task.id, f.clock())
        self.save(f.store, "x", f.task.id)
        self.assertIsNone(f.store.claim_next_executable_task(worker_id="planner", lease_seconds=120, planning_cutoff=f.clock()))
        with f.store._reader() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM pipeline_canonical_hold").fetchone()[0], 1)

    def test_resume_with_missing_hold_table_is_blocked_without_mutation(self):
        f = self.x(); self.save(f.store, "x", f.task.id)
        with f.store._transaction() as conn:
            conn.execute("DROP TABLE pipeline_canonical_hold")
            row = conn.execute("SELECT * FROM x_auto_task WHERE id=?", (f.task.id,)).fetchone()
            before = conn.total_changes
            self.assertEqual(disposition_record(conn, "x:auto:" + str(f.task.id), row), "blocked")
            self.assertEqual(conn.total_changes, before)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_canonical_hold'").fetchone())

    def test_x_pending_selection_can_fill_empty_fields_without_revoking_permission(self):
        f = self.x(); self.save(f.store, "x", f.task.id)
        token, task = f._reserve()
        f.store.release_task_claim(task.id, token)
        claim = f.store.claim_next_executable_task(worker_id="planner", lease_seconds=120, publish_delegated=True, planning_cutoff=f.clock())
        self.assertEqual(claim.task.material_id, "123456")
        self.assertEqual(claim.claim_phase, "prepare")

    def test_already_frozen_material_and_all_immutable_fields_reject_drift(self):
        f = self.x(); token, task = f._reserve(); f.store.release_task_claim(task.id, token)
        self.save(f.store, "x", task.id)
        with f.store._reader() as conn:
            original = dict(conn.execute("SELECT * FROM x_auto_task WHERE id=?", (task.id,)).fetchone())
            for field, value in {"id": 999, "account_id": "other", "template_id": 999, "template_version": 999,
                                 "run_id": 999, "material_id": "changed", "content_id": "changed",
                                 "scheduled_at_utc": "2026-08-12T11:00:00+00:00", "created_at": "2026-08-12T11:00:00+00:00"}.items():
                with self.subTest(field=field):
                    self.assertEqual(disposition_record(conn, "x:auto:" + str(task.id), dict(original, **{field: value})), "blocked")

    def test_malformed_or_uncommitted_authority_never_admits(self):
        for column, value in (("state", "pending"), ("plan_sha256", "bad"), ("source_fingerprint", "b" * 64),
                              ("details_json", "{}"), ("scheduled_at", "2026-08-12T11:00:00Z")):
            with self.subTest(column=column):
                f = self.x(); self.save(f.store, "x", f.task.id)
                with f.store._transaction() as conn:
                    conn.execute("UPDATE pipeline_backlog_disposition SET " + column + "=?", (value,))
                self.assertIsNone(f.store.claim_next_pending_task(worker_id="select", lease_seconds=120))
                self.assertIsNone(f.store.claim_next_executable_task(worker_id="planner", lease_seconds=120, planning_cutoff=f.clock()))

    def test_archive_blocks_both_x_paths_even_if_hold_missing_and_identity_changed(self):
        f = self.x(); self.save(f.store, "x", f.task.id, action="archive")
        with f.store._transaction() as conn:
            conn.execute("UPDATE x_auto_task SET account_id='changed' WHERE id=?", (f.task.id,))
        self.assertIsNone(f.store.claim_next_pending_task(worker_id="select", lease_seconds=120))
        self.assertIsNone(f.store.claim_next_executable_task(worker_id="planner", lease_seconds=120))

    def test_tt_old_reserved_task_requires_exact_authorization_and_keeps_reservation(self):
        f = self.tt(); task = f.reserved_task(); f.store.execution_not_before_utc = (f.clock() + dt.timedelta(days=1)).isoformat()
        kwargs = dict(worker_id="node", lease_seconds=120, task_id=task.id, allowed_phases={"prepare"})
        self.assertIsNone(f.store.claim_next_executable_task(**kwargs))
        self.save(f.store, "tt", task.id)
        before = f.store.get_task_reservation(task.id)
        claim = f.store.claim_next_executable_task(**kwargs)
        self.assertEqual((claim.task.id, claim.claim_phase), (task.id, "prepare"))
        self.assertEqual(f.store.get_task_reservation(task.id), before)
        self.assertEqual(claim.task.scheduled_at_utc, task.scheduled_at_utc)

    def test_tt_authorized_old_source_remains_same_account_fence(self):
        f = self.tt(); first = f.reserved_task(suffix="first")
        f.clock.value += dt.timedelta(seconds=1); second = f.reserved_task(suffix="second")
        f.store.execution_not_before_utc = (f.clock() + dt.timedelta(days=1)).isoformat()
        self.save(f.store, "tt", first.id); self.save(f.store, "tt", second.id)
        self.assertIsNone(f.store.claim_next_executable_task(worker_id="node", lease_seconds=120, task_id=second.id, allowed_phases={"prepare"}))
        claim = f.store.claim_next_executable_task(worker_id="node", lease_seconds=120, task_id=first.id, allowed_phases={"prepare"})
        self.assertEqual(claim.task.id, first.id)

    def test_tt_unknown_predecessor_is_never_bypassed(self):
        f = self.tt(); first = f.reserved_task(suffix="unknown")
        f.clock.value += dt.timedelta(seconds=1); second = f.reserved_task(suffix="next")
        with f.store._transaction() as conn:
            conn.execute("UPDATE tt_auto_task SET status='unknown',unknown_outcome=1,publish_attempt_count=1 WHERE id=?", (first.id,))
        f.store.execution_not_before_utc = (f.clock() + dt.timedelta(days=1)).isoformat()
        self.save(f.store, "tt", second.id)
        self.assertIsNone(f.store.claim_next_executable_task(worker_id="node", lease_seconds=120, task_id=second.id, allowed_phases={"prepare"}))
        self.assertEqual(f.store.get_task(first.id).status, "unknown")

    def test_tt_archive_and_corrupt_resume_cannot_cross_floor_or_claim_pending(self):
        for action in ("archive", "resume"):
            with self.subTest(action=action):
                f = self.tt(); task = f.reserved_task()
                self.save(f.store, "tt", task.id, action=action)
                with f.store._transaction() as conn:
                    conn.execute("UPDATE tt_auto_task SET status='pending' WHERE id=?", (task.id,))
                    if action == "resume": conn.execute("UPDATE pipeline_backlog_disposition SET source_fingerprint=?", ("0" * 64,))
                self.assertIsNone(f.store.claim_next_pending_task(worker_id="select", lease_seconds=120))
                self.assertIsNone(f.store.claim_next_executable_task(worker_id="node", lease_seconds=120, task_id=task.id))


if __name__ == "__main__":
    unittest.main()

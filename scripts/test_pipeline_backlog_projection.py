"""Read-only backlog proofs from actual X/TT native SQLite schemas."""
import json
import unittest

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.backlog_contract import SCHEMA, authorization_identity, source_fingerprint
from scripts import test_pipeline_x_native as xnative
from scripts import test_x_auto_post_publisher as xauto
from scripts import test_tt_auto_post_publisher as ttauto


class BacklogProjectionTests(unittest.TestCase):
    def setUp(self):
        self.native = xnative.NativeTests(); self.native.setUp(); self.addCleanup(self.native.doCleanups)
        self.auto = xauto.XAutoPostPublisherTests(); self.auto.setUp(); self.addCleanup(self.auto.doCleanups)
        self.queue = self.native.queue(identity=640)
        self.adapter = LedgerAdapter(LedgerSpec("x", "queue", str(self.native.runtime.POST_DB_PATH), "x_post_queue"),
                                     object(), cross_paths={"x_auto": str(self.auto.store.db_path)})

    def save(self, conn, channel, family, row, *, action="resume"):
        identity = authorization_identity(channel, family, row)
        conn.execute(SCHEMA)
        conn.execute("INSERT INTO pipeline_backlog_disposition VALUES(?,?,?,?,?,?,?,?)", (
            identity["source_key"], "b" * 64, action, "committed", source_fingerprint(identity),
            identity["scheduled_at"], identity["created_at"], json.dumps({"identity": identity})))
        return identity

    def link_parent(self):
        f = self.auto
        with f.store._transaction() as conn:
            row = conn.execute("SELECT * FROM x_auto_task WHERE id=?", (f.task.id,)).fetchone()
            identity = self.save(conn, "x", "auto", row)
        # Normal post-authorization selection fills previously empty identity
        # fields, followed by an exact canonical child link. No HTTP is used.
        claim = f.store.claim_next_executable_task(worker_id="planner", lease_seconds=120)
        f.store.reserve_material(task_id=f.task.id, material_id=str(self.queue["material_id"]), content_id="drama",
            selection={"material": {"material_id": str(self.queue["material_id"]), "video_duration": 30}}, claim_token=claim.reveal_claim_token())
        f.store.release_task_claim(f.task.id, claim.reveal_claim_token())
        with f.store._transaction() as conn:
            conn.execute("UPDATE x_auto_task SET execution_queue_id=? WHERE id=?", (self.queue["id"], f.task.id))
        return identity

    def offer(self, identity=None):
        return self.adapter.project(self.adapter.read(identity or self.queue["id"]))

    def test_only_exact_native_child_inherits_original_parent_authority(self):
        identity = self.link_parent()
        before = self.auto.store.get_task(self.auto.task.id)
        offer = self.offer()
        proof = offer["backlog_ancestor_authorization"]
        self.assertEqual(proof["identity"], identity)
        self.assertEqual(proof["source_key"], "x:auto:" + str(before.id))
        self.assertEqual(proof["plan_sha256"], "b" * 64)
        self.assertEqual(self.auto.store.get_task(before.id), before)
        sibling = self.native.queue(identity=640)
        self.assertIsNone(self.offer(sibling["id"])["backlog_ancestor_authorization"])

    def test_parent_or_child_identity_drift_does_not_grant_authority(self):
        self.link_parent()
        mutations = (("execution_queue_id", 999999), ("account_id", "different"), ("material_id", "different"),
                     ("scheduled_at_utc", "2026-08-12T11:00:00+00:00"), ("created_at", "2026-08-12T11:00:00+00:00"))
        for column, value in mutations:
            with self.subTest(column=column):
                with self.auto.store._transaction() as conn:
                    previous = conn.execute("SELECT " + column + " FROM x_auto_task WHERE id=?", (self.auto.task.id,)).fetchone()[0]
                    conn.execute("UPDATE x_auto_task SET " + column + "=? WHERE id=?", (value, self.auto.task.id))
                self.assertIsNone(self.offer()["backlog_ancestor_authorization"])
                with self.auto.store._transaction() as conn:
                    conn.execute("UPDATE x_auto_task SET " + column + "=? WHERE id=?", (previous, self.auto.task.id))

    def test_uncommitted_or_held_parent_does_not_grant_authority(self):
        self.link_parent()
        with self.auto.store._transaction() as conn:
            conn.execute("UPDATE pipeline_backlog_disposition SET state='pending'")
        self.assertIsNone(self.offer()["backlog_ancestor_authorization"])
        with self.auto.store._transaction() as conn:
            conn.execute("UPDATE pipeline_backlog_disposition SET state='committed'")
            conn.execute("INSERT INTO pipeline_canonical_hold VALUES(?,?,'cutover_overdue')", ("x:auto:" + str(self.auto.task.id), "2026-11-02T00:00:00Z"))
        self.assertIsNone(self.offer()["backlog_ancestor_authorization"])

    def test_direct_queue_uses_raw_created_and_original_native_schedule(self):
        observed = self.adapter.read(self.queue["id"])
        with self.native.bridge.connect() as conn:
            raw = dict(conn.execute("SELECT * FROM x_post_queue WHERE id=?", (self.queue["id"],)).fetchone())
            raw["scheduled_at"] = observed["scheduled_at"]
            identity = self.save(conn, "x", "queue", raw)
        self.assertEqual(self.offer()["backlog_authorization"]["identity"], identity)
        with self.native.bridge.connect() as conn:
            conn.execute("UPDATE x_post_queue SET account_id=999 WHERE id=?", (self.queue["id"],))
        self.assertIsNone(self.offer()["backlog_authorization"])

    def test_archive_proof_survives_native_hold_and_cancelled_identity_drift(self):
        observed = self.adapter.read(self.queue["id"])
        with self.native.bridge.connect() as conn:
            raw = dict(conn.execute("SELECT * FROM x_post_queue WHERE id=?", (self.queue["id"],)).fetchone())
            raw["scheduled_at"] = observed["scheduled_at"]
            identity = self.save(conn, "x", "queue", raw, action="archive")
            conn.execute("INSERT INTO pipeline_canonical_hold VALUES(?,?,'cutover_overdue')", (identity["source_key"], "2026-11-02T00:00:00Z"))
            conn.execute("UPDATE x_post_queue SET status='cancelled',account_id=999 WHERE id=?", (self.queue["id"],))
        offered = self.offer()
        self.assertEqual(offered["backlog_authorization"]["action"], "archive")
        self.assertEqual(offered["backlog_authorization"]["identity"], identity)
        self.assertFalse(offered["execution_available"])

    def test_tt_preparation_eligibility_does_not_rewrite_authorized_publish_time(self):
        f = ttauto.TTAutoPostPublisherIntegrationTests(); f.setUp(); self.addCleanup(f.doCleanups)
        task = f.reserved_task()
        with f.store._transaction() as conn:
            conn.execute("UPDATE tt_auto_task SET scheduled_at_utc='2026-08-05T14:00:00+00:00' WHERE id=?", (task.id,))
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (task.id,)).fetchone()
            identity = self.save(conn, "tt", "auto", row)
        adapter = LedgerAdapter(LedgerSpec("tt", "auto", str(f.store.db_path), "tt_auto_task"), object(),
                                preparation_policy={"tt_auto_prepare_ahead_seconds": 14400})
        offer = adapter.project(adapter.read(task.id))
        self.assertEqual(offer["backlog_authorization"]["identity"], identity)
        self.assertNotEqual(offer["scheduled_at"], offer["cutover_due_at"])
        self.assertEqual(identity["scheduled_at"], "2026-08-05T14:00:00.000000+00:00")


if __name__ == "__main__":
    unittest.main()

"""Terminal native posts cannot leave unclaimed preparation observations queued."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.adapters import ClosingConnection, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch import views


class TerminalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc)
        self.path = root / "x.sqlite3"
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.executescript("""
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,status TEXT,account_id INTEGER,
                    media_validation_mode TEXT,created_at TEXT,scheduled_at TEXT);
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,status TEXT,
                    attempt_count INTEGER,unknown_outcome INTEGER,x_post_id TEXT,started_at TEXT,published_at TEXT);
                INSERT INTO x_post_queue VALUES(5373,'queued',71,'deferred','2026-11-02T10:00:00Z','2026-11-02T10:00:00Z');
                INSERT INTO x_post_publish_log VALUES(99,5373,'reserved',0,0,'','','');
            """)
        self.adapter = LedgerAdapter(LedgerSpec("x", "queue", str(self.path), "x_post_queue", module="x_accounts"), object())
        self.store = DispatchStore(root / "dispatch.sqlite3", clock=lambda: self.now)
        self.prep = self.store.offer(self.project())

    def project(self):
        return self.adapter.project(self.adapter.read(5373), self.now)

    def publish(self):
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.execute("UPDATE x_post_queue SET status='published'")
            c.execute("UPDATE x_post_publish_log SET status='published',attempt_count=1,x_post_id='12345',started_at='2026-11-02T11:00:00Z',published_at='2026-11-02T11:10:00Z'")
        return self.project()

    def test_real_native_publication_closes_old_observation_without_render_metrics(self):
        publication = self.publish()
        self.assertEqual((publication["stage"], publication["status"], publication["source_attempt_count"]), ("publish", "published", 1))
        native_before = self.adapter.read(5373)
        post = self.store.offer(publication)
        old = self.store.get_job(self.prep["id"])
        self.assertEqual((old["status"], old["hold_reason"], old["generation"]), ("completed", "metadata_only", 0))
        self.assertEqual((old["started_at"], old["finished_at"]), ("", ""))
        self.assertEqual(self.adapter.read(5373), native_before)
        self.store.record_publication(post["id"], {"source_key": publication["source_key"], "publish_id": "12345", "published_at": publication["finished_at"]}, canonical_verified=True)
        admin = {"is_admin": True, "user_id": "admin", "allowed_channels": ["x"], "allowed_modules": []}
        result = views.query(self.store, "summary", admin, {})
        self.addCleanup(views.invalidate_cache, self.store)
        self.assertEqual(result["totals"]["prepared"], 0)
        self.assertEqual(result["totals"]["queued"], 0)
        self.assertEqual(result["totals"]["published"], 1)
        prep = next(row for row in result["channels"] if row["stage"] == "prepare")
        self.assertEqual(prep["throughput_per_hour"], 0)
        self.assertIsNone(prep["avg_duration_seconds"])
        self.store.offer(publication)  # Same terminal scan is idempotent.
        with self.store.transaction() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='terminal_preparation_observation_closed'").fetchone()[0], 1)
        self.store.register_node("cpu-main", ["prepare:x"])
        self.store.worker_heartbeat("cpu-main", "prep", "prepare")
        self.store.channel_enabled("x", True)
        self.assertIsNone(self.store.claim("cpu-main", "prep", "prepare")["job"])

    def test_ready_and_legacy_observations_close_without_changing_known_timestamps(self):
        publication = self.publish()
        for state in ("ready", "legacy_running"):
            with self.subTest(state=state):
                with self.store.transaction() as c:
                    c.execute("UPDATE pipeline_job SET status=?,hold_reason='',started_at='2026-11-02T10:00:00Z',finished_at='' WHERE id=?", (state, self.prep["id"]))
                self.store.offer(publication)
                old = self.store.get_job(self.prep["id"])
                self.assertEqual((old["status"], old["hold_reason"]), ("completed", "metadata_only"))
                self.assertEqual((old["started_at"], old["finished_at"]), ("2026-11-02T10:00:00Z", ""))

    def test_paused_unknown_intent_claim_receipt_and_new_generation_stay_untouched(self):
        publication = self.publish()
        for field, value in (("status", "backlog_paused"), ("status", "unknown"), ("status", "running"),
                             ("claim_hash", "owned"), ("external_intent", 1), ("canonical_receipt", "proof"), ("generation", 1)):
            with self.subTest(field=field, value=value):
                with self.store.transaction() as c:
                    c.execute("UPDATE pipeline_job SET status='queued',hold_reason='',claim_hash='',external_intent=0,canonical_receipt='',generation=0 WHERE id=?", (self.prep["id"],))
                    c.execute("UPDATE pipeline_job SET " + field + "=? WHERE id=?", (value, self.prep["id"]))
                before = self.store.get_job(self.prep["id"])
                self.store.offer(publication)
                self.assertEqual(self.store.get_job(self.prep["id"]), before)

    def test_saved_claim_history_and_unconfirmed_post_cannot_close_observation(self):
        publication = self.publish()
        for extra in ({"publish_id": ""}, {"unknown_outcome": True}, {"legacy_inflight": True}):
            with self.subTest(extra=extra):
                self.store.offer(dict(publication, **extra))
                self.assertEqual(self.store.get_job(self.prep["id"])["status"], "queued")
        with self.store.transaction() as c:
            c.execute("INSERT INTO pipeline_claim_history VALUES(?,1,'cpu-main','p1','previous')", (self.prep["id"],))
        self.store.offer(publication)
        self.assertEqual(self.store.get_job(self.prep["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()

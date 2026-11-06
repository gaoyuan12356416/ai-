import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.recover_x_auto_no_candidate import recover


@contextlib.contextmanager
def connect(path):
    with contextlib.closing(sqlite3.connect(path)) as c:
        with c:
            yield c


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auto = Path(self.tmp.name) / "auto.db"
        self.native = Path(self.tmp.name) / "native.db"
        with connect(self.native) as c:
            c.execute("CREATE TABLE x_post_manual_run(external_task_key TEXT)")
        fields = ("material_id content_id execution_run_id execution_queue_id execution_log_id "
            "publish_id publish_url unknown_outcome preparation_attempt_count publish_attempt_count "
            "claim_worker claim_token lease_expires_at_utc reserved_at_utc bridged_at_utc published_at_utc").split()
        with connect(self.auto) as c:
            c.executescript("""
                CREATE TABLE x_auto_run(id INTEGER PRIMARY KEY,status TEXT,finished_at_utc TEXT,updated_at TEXT);
                INSERT INTO x_auto_run VALUES(1,'partial_failed','old','old');
                CREATE TABLE x_auto_material_ledger(task_id INTEGER);
                CREATE TABLE pipeline_canonical_hold(source_key TEXT);
                CREATE TABLE x_auto_event(id INTEGER PRIMARY KEY,run_id INTEGER,task_id INTEGER,event_type TEXT,
                    from_status TEXT,to_status TEXT,message TEXT,details_json TEXT,created_at TEXT);
            """)
            c.execute("CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,status TEXT,error_code TEXT,"
                "error_message TEXT,selection_json TEXT,finished_at_utc TEXT,next_attempt_at_utc TEXT,updated_at TEXT,"
                "language TEXT," + ",".join(name + " TEXT DEFAULT ''" for name in fields) + ")")
            for tid, status in [(1, 'published'), (2, 'no_candidate'), (3, 'no_candidate')]:
                c.execute("INSERT INTO x_auto_task(id,run_id,status,error_code,selection_json,language) VALUES(?,1,?,'x_auto_no_eligible_material','{}','pt-br')", (tid,status))
            c.execute("UPDATE x_auto_task SET publish_id='original-post' WHERE id=1")

    def call(self, **kw):
        return recover(self.auto, self.native, 1, [2,3], operation_id="op", actor="test", reason="language fixed", **kw)

    def snapshot(self):
        with connect(self.auto) as c:
            return c.execute("SELECT * FROM x_auto_task ORDER BY id").fetchall()

    def test_dry_run_and_apply_preserve_published_and_frozen_identity(self):
        before = self.snapshot()
        self.assertFalse(self.call()["applied"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.call(apply=True)["applied"])
        after = self.snapshot()
        self.assertEqual(after[0], before[0])
        self.assertEqual([r[2] for r in after], ["published", "pending", "pending"])
        self.assertEqual([r[9] for r in after], ["pt-br"] * 3)
        self.assertTrue(self.call(apply=True)["already_applied"])
        self.assertEqual(self.snapshot(), after)

    def test_native_receipt_blocks_recovery_atomically(self):
        with connect(self.native) as c:
            c.execute("INSERT INTO x_post_manual_run VALUES('x-auto-task-3')")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "native execution"):
            self.call(apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_attempt_unknown_or_claim_blocks_recovery(self):
        for field in ("publish_attempt_count", "unknown_outcome", "claim_token", "execution_queue_id"):
            with self.subTest(field=field), connect(self.auto) as c:
                c.execute("UPDATE x_auto_task SET " + field + "='1' WHERE id=2")
                c.commit()
                with self.assertRaisesRegex(RuntimeError, "evidence"):
                    self.call(apply=True)
                c.execute("UPDATE x_auto_task SET " + field + "='' WHERE id=2")

    def test_published_task_is_never_requeued(self):
        with self.assertRaisesRegex(RuntimeError, "no-candidate"):
            recover(self.auto, self.native, 1, [1], operation_id="different", actor="test", reason="test", apply=True)


if __name__ == '__main__':
    unittest.main()

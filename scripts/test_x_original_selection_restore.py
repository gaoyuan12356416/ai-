import sqlite3
import unittest
from scripts.restore_x_auto_2796 import reactivate_reservation,TABLE,SCHEMA
from scripts.recover_x_unstarted_manual import RecoveryRejected

class ReservationRecoveryTests(unittest.TestCase):
    def connection(self):
        c=sqlite3.connect(':memory:');c.execute('CREATE TABLE x_post_manual_material_reservation(id INTEGER PRIMARY KEY, state TEXT,release_reason TEXT,manual_run_id INTEGER,material_key TEXT,updated_at TEXT)')
        c.execute('CREATE TABLE x_post_queue(manual_run_id INTEGER,material_id TEXT)')
        c.execute(SCHEMA)
        c.execute("INSERT INTO "+TABLE+" VALUES(2796,'native_armed','original-release-proof','now')")
        c.execute("INSERT INTO x_post_manual_material_reservation VALUES(1854,'released','x_post_account_needs_review',1817,'8009155','old')")
        c.execute("INSERT INTO x_post_manual_material_reservation VALUES(1953,'active','',1916,'other','old')")
        c.execute("CREATE TRIGGER trg_x_post_manual_reservation_state_update BEFORE UPDATE OF state,release_reason ON x_post_manual_material_reservation WHEN OLD.state<>'active' OR NEW.state NOT IN ('consumed','released') BEGIN SELECT RAISE(ABORT,'x_post_manual_material_reservation transition invalid'); END")
        c.commit();c.execute('BEGIN IMMEDIATE');return c

    def test_incident_only_and_original_guard_survives(self):
        c=self.connection();ddl=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchone()[0]
        reactivate_reservation(c,'now');c.commit()
        self.assertEqual(c.execute('SELECT state FROM x_post_manual_material_reservation WHERE id=1854').fetchone()[0],'active')
        self.assertEqual(c.execute('SELECT updated_at FROM x_post_manual_material_reservation WHERE id=1953').fetchone()[0],'old')
        self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchone()[0],ddl)
        c.execute("UPDATE x_post_manual_material_reservation SET state='consumed' WHERE id=1854")
        with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE x_post_manual_material_reservation SET state='active' WHERE id=1854")

    def test_new_queue_prevents_reactivation(self):
        c=self.connection();c.execute("INSERT INTO x_post_queue VALUES(999,'8009155')")
        with self.assertRaisesRegex(RecoveryRejected,'material_queued_elsewhere'):reactivate_reservation(c,'now')
        self.assertEqual(c.execute('SELECT state FROM x_post_manual_material_reservation WHERE id=1854').fetchone()[0],'released')

    def test_outer_failure_rolls_back_reactivation_and_trigger(self):
        c=self.connection();reactivate_reservation(c,'now');c.rollback()
        self.assertEqual(c.execute('SELECT state FROM x_post_manual_material_reservation WHERE id=1854').fetchone()[0],'released')
        with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE x_post_manual_material_reservation SET state='active' WHERE id=1854")

if __name__=='__main__':unittest.main()

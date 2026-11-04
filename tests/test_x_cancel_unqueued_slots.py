import importlib.util
from pathlib import Path
import sqlite3
import unittest

spec = importlib.util.spec_from_file_location('cancel_slots', Path(__file__).resolve().parents[1] / 'scripts/x_cancel_unqueued_slots.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''
          CREATE TABLE x_post_schedule_run(id INTEGER PRIMARY KEY,source_type,run_date,publish_time,account_ids_json,config_version,created_at,status,error_code,error_message,finished_at,updated_at);
          CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,account_id,schedule_run_id,status);
          CREATE TABLE x_post_schedule_codefix_compensation_audit(original_schedule_run_id,compensation_schedule_run_id);
          CREATE TABLE x_post_schedule_drama_scope_compensation_audit(original_schedule_run_id,compensation_schedule_run_id);
          CREATE TABLE x_post_operator_gap_recovery_audit(identity,evidence_json);
          INSERT INTO x_post_schedule_run VALUES(1,'material','2026-11-03','08:00','[10,11]',1,'old','failed_preflight','old_error','old_message','','old');
          INSERT INTO x_post_schedule_run VALUES(2,'material','2026-11-03','09:00','[10,11]',1,'old','running','','','','old');
          INSERT INTO x_post_queue VALUES(7,10,2,'publishing');
        ''')
        self.plan = m.preview(self.c, [1, 2])

    def tearDown(self):
        self.c.close()

    def apply(self):
        return m.apply(self.c, self.plan, m.sha(self.plan), actor='test', reason='operator cancelled unqueued only', backup_path='isolated-test')

    def test_preserves_inflight_and_future_and_cancels_exact_targets(self):
        before = dict(self.c.execute('SELECT * FROM x_post_queue').fetchone())
        result = self.apply()
        self.assertEqual(result['cancelled'], 3)
        self.assertEqual(result['closed_runs'], [1])
        self.assertEqual(before, dict(self.c.execute('SELECT * FROM x_post_queue').fetchone()))
        self.c.execute("UPDATE x_post_queue SET status='published' WHERE id=7")
        self.c.execute("INSERT INTO x_post_queue VALUES(8,11,999,'queued')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cancelled'):
            self.c.execute("INSERT INTO x_post_queue VALUES(9,11,2,'queued')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cancelled'):
            self.c.execute("UPDATE x_post_schedule_run SET status='claimed' WHERE id=1")

    def test_race_preserves_newly_created_task_and_aborts_whole_cancellation(self):
        self.c.execute("INSERT INTO x_post_queue VALUES(8,11,2,'queued')")
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'scope_changed'):
            self.apply()
        self.assertIsNone(self.c.execute("SELECT name FROM sqlite_master WHERE name=?", (m.TABLE,)).fetchone())
        self.assertEqual(self.c.execute('SELECT count(*) FROM x_post_queue').fetchone()[0], 2)

    def test_idempotent_application_after_publication_progress(self):
        self.apply()
        self.c.execute("UPDATE x_post_queue SET status='published' WHERE id=7")
        self.c.commit()
        self.assertTrue(self.apply()['already_applied'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM '+m.TABLE).fetchone()[0], 3)

    def test_compensation_before_and_after_queue_creation_is_fenced(self):
        self.apply()
        self.c.execute('INSERT INTO x_post_schedule_codefix_compensation_audit VALUES(2,20)')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cancelled'):
            self.c.execute("INSERT INTO x_post_queue VALUES(8,11,20,'queued')")
        self.c.execute("INSERT INTO x_post_queue VALUES(8,11,21,'queued')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cancelled'):
            self.c.execute('INSERT INTO x_post_schedule_drama_scope_compensation_audit VALUES(2,21)')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cancelled'):
            self.c.execute('INSERT INTO x_post_operator_gap_recovery_audit VALUES(?,?)', ('2','{"child_run_id":21}'))

    def test_active_planner_and_manifest_tampering_rejected(self):
        self.c.execute("UPDATE x_post_schedule_run SET status='claimed' WHERE id=1")
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'planner_may_be_inflight'):
            self.apply()
        with self.assertRaisesRegex(ValueError, 'invalid_manifest'):
            m.apply(self.c, self.plan, 'wrong', actor='a', reason='r', backup_path='b')


if __name__ == '__main__':
    unittest.main()

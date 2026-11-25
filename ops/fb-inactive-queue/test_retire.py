import sqlite3
import unittest

from retire import install


class InactiveTemplateQueueTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.executescript("""
            CREATE TABLE fb_auto_template(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE fb_auto_run(id INTEGER PRIMARY KEY,template_id INTEGER,trigger_type TEXT);
            CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,template_id INTEGER,status TEXT,
                lease_owner TEXT DEFAULT '',lease_expires_at_utc TEXT DEFAULT '',graph_post_id TEXT DEFAULT '',
                unknown_outcome INTEGER DEFAULT 0,skip_reason TEXT DEFAULT '',error_code TEXT DEFAULT '',
                error_message TEXT DEFAULT '',completed_at_utc TEXT DEFAULT '');
            CREATE TABLE fb_auto_due_slot(id INTEGER PRIMARY KEY,template_id INTEGER,trigger_type TEXT,
                status TEXT,lease_owner TEXT DEFAULT '',error_code TEXT,updated_at_utc TEXT);
            CREATE TABLE fb_auto_publish_attempt(task_id INTEGER);
            CREATE TABLE fb_auto_publish_ledger(task_id INTEGER);
            CREATE TABLE pipeline_fb_operation(source_key TEXT,stage TEXT);
            CREATE TABLE pipeline_canonical_hold(source_key TEXT);
            INSERT INTO fb_auto_template VALUES(1,'enabled'),(2,'deleted');
            INSERT INTO fb_auto_run VALUES(1,1,'auto'),(2,1,'manual'),(3,2,'auto');
            INSERT INTO fb_auto_task(id,run_id,template_id,status) VALUES
                (1,1,1,'ready'),(2,2,1,'ready'),(3,3,2,'planned'),(4,1,1,'preparing'),
                (5,1,1,'ready'),(6,1,1,'ready'),(7,1,1,'unknown'),(8,1,1,'ready'),
                (9,1,1,'ready'),(10,1,1,'running'),(11,1,1,'published');
            INSERT INTO fb_auto_publish_attempt VALUES(5);
            INSERT INTO pipeline_fb_operation VALUES('fb:auto:6','publish');
            INSERT INTO fb_auto_publish_ledger VALUES(8);
            INSERT INTO pipeline_canonical_hold VALUES('fb:auto:9');
            INSERT INTO fb_auto_due_slot(id,template_id,trigger_type,status) VALUES
                (1,1,'auto','pending'),(2,1,'manual','pending');
        """)

    def test_backfill_disable_delete_and_late_render(self):
        self.assertEqual(install(self.c), dict(tasks=1, slots=0))
        self.c.execute("UPDATE fb_auto_template SET status='disabled' WHERE id=1")
        self.c.execute("UPDATE fb_auto_task SET status='ready' WHERE id=4")
        self.c.execute("INSERT INTO fb_auto_task(id,run_id,template_id,status) VALUES(12,3,2,'ready')")
        self.c.execute("UPDATE fb_auto_template SET status='enabled' WHERE id=1")
        self.assertEqual(list(self.c.execute("SELECT id FROM fb_auto_task WHERE status='skipped' ORDER BY id")),
                         [(1,), (3,), (4,), (12,)])
        self.assertEqual(list(self.c.execute("SELECT status FROM fb_auto_due_slot ORDER BY id")),
                         [('missed',), ('pending',)])
        self.assertEqual(self.c.execute("SELECT count(*) FROM fb_inactive_queue_log").fetchone()[0], 4)
        self.c.commit()
        self.assertEqual(install(self.c), dict(tasks=0, slots=0))


if __name__ == '__main__':
    unittest.main()

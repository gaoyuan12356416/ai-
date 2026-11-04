"""A read-only planner may defer during cutover; publishers may never do so."""
import datetime as dt
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from features.fb_auto_posts.core import FBAutoPostStore
from scripts import deploy_fb_page_task_isolation as deploy


class PlanDrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fb.sqlite3'
        self.store = FBAutoPostStore(self.path)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.stamp = self.now.isoformat()
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'test','enabled',?,?)", (self.stamp, self.stamp))
            c.execute("INSERT INTO fb_auto_due_slot(id,template_id,template_version,slot_key,planned_publish_at_utc,status,lease_owner,lease_expires_at_utc,created_at_utc,updated_at_utc) VALUES(1,1,1,'future',?,'preparing','fb-auto-post-runner-primary-0',?,?,?)",
                      ((self.now + dt.timedelta(days=1)).isoformat(), (self.now + dt.timedelta(hours=1)).isoformat(), self.stamp, self.stamp))
            self.due = dict(c.execute('SELECT * FROM fb_auto_due_slot WHERE id=1').fetchone())
        self.db_patch = patch.object(deploy, 'FB_DB', self.path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def observe(self, extra='', active=0):
        sockets = '\n'.join([
            'ESTAB 0 0 127.0.0.1:18835 127.0.0.1:12345 users:(("python3",pid=99,fd=4))',
            'ESTAB 0 0 127.0.0.1:12345 127.0.0.1:18835 users:(("python3",pid=88,fd=3))',
            'ESTAB 0 0 10.0.0.108:23456 101.32.56.53:63350 users:(("python3",pid=99,fd=5))', extra])
        with patch.object(deploy, 'native_active', return_value=active), patch.object(deploy, 'drained', return_value={'threads': 5}), patch.object(deploy, 'show', return_value='88'), patch.object(deploy, 'run', return_value=sockets), patch.object(Path, 'read_bytes', return_value=b'python3\0scripts/fb_auto_post_runner.py\0plan\0'):
            return deploy.future_plan_read('99')

    def test_only_future_select_handler_is_allowed(self):
        self.assertEqual(self.observe()['due']['id'], 1)
        self.assertIsNone(self.observe(active=1))
        self.assertIsNone(self.observe('ESTAB 0 0 10.0.0.108:2222 1.2.3.4:443 users:(("python3",pid=99,fd=8))'))
        with self.store.connect() as c:
            c.execute('UPDATE fb_auto_due_slot SET planned_publish_at_utc=?', (self.stamp,))
        self.assertIsNone(self.observe())

    def test_defer_preserves_identity_and_fences_newer_claim(self):
        self.assertEqual(deploy.settle_interrupted_plan({'due': self.due}), {'deferred': True})
        with self.store.connect() as c:
            row = dict(c.execute('SELECT * FROM fb_auto_due_slot WHERE id=1').fetchone())
            self.assertEqual(row['status'], 'pending')
            for key in ('id','template_id','template_version','slot_key','planned_publish_at_utc'):
                self.assertEqual(row[key], self.due[key])
            c.execute("UPDATE fb_auto_due_slot SET status='preparing',lease_owner='new-owner'")
        self.assertEqual(deploy.settle_interrupted_plan({'due': self.due}), {'deferred': False})

    def test_created_run_is_completed_without_recreating_tasks(self):
        with self.store.connect() as c:
            run_id = c.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,'future','auto','queued','{}',?)", (self.stamp,)).lastrowid
        self.assertEqual(deploy.settle_interrupted_plan({'due': self.due}), {'completed': True})
        with self.store.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT status,run_id FROM fb_auto_due_slot WHERE id=1').fetchone()), ('prepared',run_id))
            self.assertEqual(c.execute('SELECT count(*) FROM fb_auto_run').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()

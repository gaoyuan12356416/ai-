"""Only verified historical success may release an account hold concurrently."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.x_post_gap_recovery import reconcile_direct_post


class ConfirmedPostReconciliationTests(unittest.TestCase):
    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            yield c

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'posts.sqlite3'
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY, account_id INTEGER,
                  source_type TEXT, delivery_mode TEXT, status TEXT, pool_item_id INTEGER,
                  material_key TEXT, account_username TEXT, updated_at TEXT);
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,
                  status TEXT,unknown_outcome INTEGER,x_media_id TEXT,started_at TEXT,
                  updated_at TEXT,post_text TEXT,x_post_id TEXT,x_post_url TEXT,
                  published_at TEXT,error_code TEXT,error_message TEXT);
                CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,x_user_id TEXT);
                CREATE TABLE x_post_material_pool(id INTEGER PRIMARY KEY,material_key TEXT,status TEXT);
                INSERT INTO x_authorized_account VALUES(1,'100');
                INSERT INTO x_post_material_pool VALUES(5,'123','unpublished');
                INSERT INTO x_post_queue VALUES(1,1,'material','direct','failed',5,'123','example','2026-09-26T10:01:00Z');
                INSERT INTO x_post_publish_log VALUES(1,1,'failed',1,'99','2026-09-26T09:59:00Z',
                  '2026-09-26T10:01:00Z','frozen body','','','','x_post_outcome_unknown','missing ID');
            ''')
        self.post = {'id':'200','author_id':'100','created_at':'2026-09-26T10:00:00Z',
                     'text':'frozen body','attachments':{'media_keys':['7_99']}}

    def reconcile(self, **kwargs):
        with patch('scripts.x_post_gap_recovery.XPostStore._mark_pool_published'), \
             patch('scripts.x_post_gap_recovery.XPostStore._sync_run'):
            return reconcile_direct_post(self.db, 1, self.post, actor='test-operator', **kwargs)

    def test_confirmed_success_retains_original_unknown_in_audit(self):
        self.reconcile(apply=True)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT status,unknown_outcome,x_post_id FROM x_post_publish_log').fetchone(),
                             ('published',0,'200'))
            previous = json.loads(c.execute('SELECT previous_state_json FROM x_post_operator_gap_recovery_audit').fetchone()[0])
            self.assertEqual(previous['log']['unknown_outcome'],1)
        with self.assertRaises(ValueError):
            self.reconcile(apply=True)

    def test_active_same_account_write_blocks_without_changes(self):
        with self.connect() as c:
            c.execute("INSERT INTO x_post_queue(id,account_id,status) VALUES(2,1,'publishing')")
        with self.assertRaisesRegex(ValueError,'active publication'):
            self.reconcile(apply=True)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT unknown_outcome FROM x_post_publish_log').fetchone()[0],1)

    def test_other_account_write_does_not_block_verified_history(self):
        with self.connect() as c:
            c.execute("INSERT INTO x_post_queue(id,account_id,status) VALUES(2,2,'publishing')")
        self.assertTrue(self.reconcile(apply=True)['applied'])

    def test_mismatched_platform_evidence_never_releases_hold(self):
        self.post['author_id']='999'
        with self.assertRaisesRegex(ValueError,'mismatch'):
            self.reconcile(apply=True)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT status,unknown_outcome FROM x_post_publish_log').fetchone(),('failed',1))


if __name__ == '__main__':
    unittest.main()

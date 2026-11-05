"""Offline acceptance: independent queues, exact receipt/deduplication protection."""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.x_posts.service import XPostStore, XPostError
from features.x_posts.account_blockers import read_account_publish_blockers
from features.fb_video_publish.guards import automatic_block, install_backstops
from scripts.test_x_post_priority_manual_store import material_candidate, ACTOR


class XQueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'x.db';self.store=XPostStore(self.path)

    def plan(self, material, key):
        run=self.store.create_manual_run([str(material)],[2],key,ACTOR)
        return self.store.create_manual_plan(run['id'],[material_candidate(run,2,material)])

    def start(self, plan):
        queue=plan['queues'][0]
        log=self.store.reserve_log(queue['id'])
        self.store.prepare_log(log['id'],'https://example.test/long','https://example.test/short','test')
        return self.store.mark_publishing(log['id'])

    def test_same_account_new_material_can_publish_after_unknown(self):
        old=self.plan(101,'old');log=self.start(old)
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE x_post_publish_log SET unknown_outcome=1,status='post_creating' WHERE id=?",(log['id'],))
        newer=self.plan(102,'new');started=self.start(newer)
        self.assertEqual(started['status'],'media_uploading')
        self.assertTrue(self.store.get_log(log['id'])['unknown_outcome'])
        with contextlib.closing(self.store.connect() if hasattr(self.store,'connect') else sqlite3.connect(self.path)) as c:
            c.row_factory=sqlite3.Row
            self.assertEqual(read_account_publish_blockers(c,[2]),{})
        with self.assertRaises(XPostError):self.store.mark_publishing(log['id'])

    def test_same_material_unknown_cannot_create_another_delivery(self):
        old=self.plan(101,'old');log=self.start(old)
        with contextlib.closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE x_post_publish_log SET unknown_outcome=1,status='post_creating' WHERE id=?",(log['id'],))
        with self.assertRaises(XPostError) as raised:self.plan(101,'duplicate')
        self.assertEqual(raised.exception.code,'x_post_unknown_outcome')


class FBQueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.executescript('''
            CREATE TABLE fb_auto_task(id TEXT,page_id TEXT,material_id TEXT,status TEXT,unknown_outcome INT,graph_post_id TEXT);
            CREATE TABLE fb_video_task(task_id TEXT,page_id TEXT,material_id TEXT,status TEXT,unknown_outcome INT,graph_post_id TEXT);
        ''')
        install_backstops(self.c)

    def test_unidentified_old_write_does_not_block_new_material(self):
        self.c.execute("INSERT INTO fb_auto_task VALUES('old','123','','unknown',1,'')")
        self.assertEqual(automatic_block(self.c,'123','456'), '')
        self.c.execute("INSERT INTO fb_video_task VALUES('new','123','456','queued',0,'')")
        self.assertEqual(self.c.execute("SELECT status FROM fb_auto_task").fetchone()[0],'unknown')

    def test_same_material_remains_fenced_and_other_material_proceeds(self):
        self.c.execute("INSERT INTO fb_auto_task VALUES('old','123','456','unknown',1,'')")
        self.assertEqual(automatic_block(self.c,'123','456'),'fb_video_material_unknown')
        self.assertEqual(automatic_block(self.c,'123','789'),'')
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO fb_video_task VALUES('duplicate','123','456','queued',0,'')")
        self.c.execute("INSERT INTO fb_video_task VALUES('new','123','789','queued',0,'')")


if __name__=='__main__':unittest.main()

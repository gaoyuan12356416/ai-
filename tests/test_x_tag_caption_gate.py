import sqlite3
import unittest
from features.x_tag_publish.caption_gate import caption_ready


class CaptionGateTest(unittest.TestCase):
    def test_wait_then_resume_exact_body(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        c.executescript("""CREATE TABLE x_post_manual_run(id,trigger_source,external_task_key);
            CREATE TABLE x_post_queue(id,manual_run_id);
            CREATE TABLE story_copy_receipt(task_key,source,final_text);
            INSERT INTO x_post_manual_run VALUES(1,'auto_template','x-auto-task-42');
            INSERT INTO x_post_queue VALUES(2,1),(3,NULL);""")
        enabled = lambda n: n == 42
        self.assertTrue(caption_ready(c,3,'old',enabled))
        self.assertTrue(caption_ready(c,2,'old',lambda n: False))
        self.assertFalse(caption_ready(c,2,'old',enabled))
        c.execute("INSERT INTO story_copy_receipt VALUES('tag:auto:42','legacy','old')")
        self.assertFalse(caption_ready(c,2,'old',enabled))
        c.rollback()
        self.assertEqual(c.execute("SELECT count(*) FROM story_copy_receipt").fetchone()[0],0)
        c.execute("INSERT INTO story_copy_receipt VALUES('tag:auto:42','ai','draft')")
        self.assertFalse(caption_ready(c,2,'other',enabled))
        self.assertTrue(caption_ready(c,2,'draft',enabled))

import contextlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from scripts import retry_x_unselected_tasks as recovery
from scripts.test_x_auto_post_store import AutoPostStoreTests


class RetryTests(unittest.TestCase):
    def setUp(self):
        h=AutoPostStoreTests();h.setUp();self.addCleanup(h.doCleanups);self.h=h
        self.task=h.create_task(h.create_run(h.create_template(),'retry')).id
        with h.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET status='no_candidate',error_code='x_auto_no_eligible_material',finished_at_utc='2026-08-05T01:00:00Z' WHERE id=?",(self.task,))

    def saved(self):
        with contextlib.closing(recovery.connect(self.h.db_path)) as c:
            task=recovery.snapshot(c,self.task,'2026-08-05')
        value=dict(kind='x_unselected_retry',day='2026-08-05',auto_path=str(self.h.db_path),native_path=str(self.h.db_path),dispatch_path=str(self.h.db_path),items=[task],skipped=[])
        value['sha256']=recovery.digest(value);return value

    def test_original_task_and_frozen_inputs_preserved_and_retry_idempotent(self):
        saved=self.saved()
        with patch.object(recovery,'external_check'):
            first=recovery.apply(saved,Path(self.h.temp.name)/'backup','operator')
            second=recovery.apply(saved,Path(self.h.temp.name)/'backup','operator')
        self.assertEqual(first['retried'],[self.task]);self.assertEqual(second['already_applied'],[self.task])
        with contextlib.closing(recovery.connect(self.h.db_path)) as c:
            current=dict(c.execute('SELECT * FROM x_auto_task WHERE id=?',(self.task,)).fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM x_auto_task').fetchone()[0],1)
            prior=json.loads(c.execute('SELECT prior_task_json FROM '+recovery.TABLE).fetchone()[0])
        self.assertEqual(prior,saved['items'][0]);self.assertEqual(current['status'],'pending')
        for key,value in prior.items():
            if key not in ('status','error_code','error_message','finished_at_utc','updated_at'):self.assertEqual(current[key],value,key)

    def test_selected_unknown_and_claimed_tasks_rejected(self):
        for field,value in [('material_id','123'),('unknown_outcome',1),('claim_token','active'),('execution_run_id',12)]:
            with self.subTest(field=field),self.h.store._transaction() as c:
                c.execute('UPDATE x_auto_task SET '+field+'=? WHERE id=?',(value,self.task))
                with self.assertRaisesRegex(ValueError,'evidence'):recovery.snapshot(c,self.task,'2026-08-05')
                c.rollback()

    def test_drift_is_local_skip_without_modification(self):
        saved=self.saved()
        with self.h.store._transaction() as c:c.execute("UPDATE x_auto_task SET status='pending' WHERE id=?",(self.task,))
        with patch.object(recovery,'external_check'):
            result=recovery.apply(saved,Path(self.h.temp.name)/'backup','operator')
        self.assertEqual(result['retried'],[]);self.assertEqual(len(result['skipped']),1)


if __name__=='__main__':unittest.main()

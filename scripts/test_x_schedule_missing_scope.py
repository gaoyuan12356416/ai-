import contextlib
import json
from pathlib import Path
import sqlite3
import unittest

from scripts import recover_x_schedule_gaps as recovery
from scripts.test_x_zero_write_schedule_recovery import RecoveryTests
from scripts.test_x_post_multi_schedule_store import base_candidate
from features.pipeline_dispatch.x_schedule_recovery import authorized_run_ids
from scripts.recover_x_unstarted_manual import RecoveryRejected

class MissingScopeTests(unittest.TestCase):
    def setUp(self):
        h=RecoveryTests();h.setUp();self.addCleanup(h.doCleanups);self.h=h
        self.rid=h.create(1051,'2026-11-04','', 'completed_with_errors')
        with contextlib.closing(sqlite3.connect(h.path)) as c:
            c.execute("INSERT INTO x_post_schedule_random_plan(source_type,run_date,config_version,account_ids_json,body_template,publish_times_json,created_at) VALUES('material','2026-11-04',9,'[2,3]',?,'[\"08:31\"]','2026-11-04T00:00:00Z')",(h.row(self.rid)['body_template'],))
            payload=base_candidate(2,'example',501,'content-1')
            payload.update(idempotency_key='existing-failed',schedule_run_id=self.rid,material_key='501',run_date='2026-11-04',created_at='2026-11-04T00:00:00Z',updated_at='2026-11-04T00:00:00Z',random_template_enabled=1,duration_policy_version=2)
            self.qid=c.execute('INSERT INTO x_post_queue('+','.join(payload)+') VALUES('+','.join('?' for _ in payload)+')',list(payload.values())).lastrowid
            c.execute("UPDATE x_post_schedule_run SET expected_count=1,queued_count=1,failed_count=1,publish_time='08:31' WHERE id=?",(self.rid,));c.commit()
        h.store.reserve_log(self.qid)
        with contextlib.closing(sqlite3.connect(h.path)) as c:
            c.execute("UPDATE x_post_queue SET status='failed' WHERE id=?",(self.qid,))
            c.execute("UPDATE x_post_publish_log SET status='failed',error_code='x_random_requires_video' WHERE queue_id=?",(self.qid,));c.commit()

    def test_child_only_contains_unqueued_accounts_and_replay_is_noop(self):
        before=self.h.row(self.rid)
        saved=recovery.plan(self.h.path,self.rid,'2026-11-04','23:58')
        result=recovery.apply(saved,Path(self.h.temp.name)/'backups','operator')
        self.assertEqual(result['account_ids'],[3]);self.assertEqual(self.h.row(self.rid),before)
        self.assertTrue(recovery.apply(saved,Path(self.h.temp.name)/'backups','operator')['already_applied'])
        child=self.h.row(result['child_run_id'])
        self.assertEqual((child['random_template_enabled'],child['duration_policy_version'],child['material_pool_snapshot_max_id']),(1,2,4444))
        with contextlib.closing(recovery.connect(self.h.path)) as c:
            self.assertEqual(authorized_run_ids(c),[result['child_run_id']])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM x_post_queue').fetchone()[0],1)

    def test_unknown_or_active_original_queue_is_not_replaced(self):
        self.h.update('UPDATE x_post_publish_log SET unknown_outcome=1 WHERE queue_id=?',(self.qid,))
        with self.assertRaisesRegex(RecoveryRejected,'unresolved_queue'):
            recovery.plan(self.h.path,self.rid,'2026-11-04','23:58')

    def test_scope_drift_between_plan_and_apply_is_rejected(self):
        saved=recovery.plan(self.h.path,self.rid,'2026-11-04','23:58')
        self.h.update("UPDATE x_post_schedule_run SET body_template='changed' WHERE id=?",(self.rid,))
        with self.assertRaisesRegex(RecoveryRejected,'scope_drift'):
            recovery.apply(saved,Path(self.h.temp.name)/'backups','operator')

if __name__=='__main__':unittest.main()

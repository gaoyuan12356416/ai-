import contextlib
import sqlite3
import unittest
from scripts import test_x_zero_write_schedule_recovery as fixtures
from scripts.resume_x_catchup_fifo_conflicts import tick,TABLE
from features.pipeline_dispatch.x_schedule_recovery import SCHEMA,identity,fingerprint,authorized_run_ids
from scripts.recover_x_unstarted_manual import connect,RecoveryRejected

class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RecoveryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        for rid in (1054,1057,1063):self.f.create(rid,'2026-11-04','x_post_pool_fifo_conflict')
        self.f.update("UPDATE x_post_schedule_run SET error_message='素材计划必须使用当前素材池最新的可用记录',plan_attempted_at='2026-11-04T00:09:00Z'")
        with contextlib.closing(connect(self.f.path,False)) as c:
            c.execute(SCHEMA)
            for rid in (1054,1057,1063):
                r=c.execute('SELECT * FROM x_post_schedule_run WHERE id=?',(rid,)).fetchone()
                c.execute('INSERT INTO x_post_schedule_zero_write_resume_audit VALUES(?,?,?,?,?,?,?,?)',(rid,fingerprint(identity(r)),'plan','actor','recovery','now','backup','{}'))
            c.commit()
    def run_tick(self):return tick(self.f.path,self.f.temp.name)
    def test_only_one_batch_rearmed_and_scope_retained(self):
        before=identity(self.f.row(1054));r=self.run_tick()
        self.assertEqual(r['rearmed_run_id'],1054);self.assertEqual(before,identity(self.f.row(1054)))
        self.assertEqual(self.run_tick(),{'waiting_for_same_pool_planners':[1054]})
        with contextlib.closing(connect(self.f.path)) as c:
            self.assertEqual(authorized_run_ids(c),[1054]);self.assertEqual(c.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],1)
    def test_inflight_batch_is_never_reset(self):
        self.f.update("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',plan_attempted_at='',finished_at='' WHERE id=1057")
        before=self.f.row(1057);self.assertEqual(self.run_tick(),{'waiting_for_same_pool_planners':[1057]});self.assertEqual(self.f.row(1057),before)
    def test_existing_work_or_unknown_rejection_not_retried(self):
        self.f.update('UPDATE x_post_schedule_run SET queued_count=1 WHERE id=1054')
        with self.assertRaisesRegex(RecoveryRejected,'run_has_work'):self.run_tick()
        self.f.update("UPDATE x_post_schedule_run SET queued_count=0,error_code='x_post_schedule_plan_unknown' WHERE id=1054")
        result=self.run_tick();self.assertEqual(result['rearmed_run_id'],1057);self.assertEqual(self.f.row(1054)['error_code'],'x_post_schedule_plan_unknown')
    def test_successful_partial_plan_does_not_block_unstarted_peer(self):
        self.f.update("UPDATE x_post_schedule_run SET status='queued',queued_count=1,expected_count=1 WHERE id=1057")
        before=self.f.row(1057);self.assertEqual(self.run_tick()['rearmed_run_id'],1054)
        self.assertEqual(self.f.row(1057),before)

if __name__=='__main__':unittest.main()

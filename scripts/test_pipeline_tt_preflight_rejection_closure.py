"""Exact historical closure tests on actual native/coordinator SQLite schemas."""
import contextlib
import copy
import datetime as dt
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.core import DispatchStore
from scripts import close_tt_987_preflight_rejection as recovery
from scripts import test_tt_auto_post_publisher as fixtures


class PreflightRejectionClosureTests(unittest.TestCase):
    setUpNative = fixtures.TTAutoPostPublisherIntegrationTests.setUp
    reserved_task = fixtures.TTAutoPostPublisherIntegrationTests.reserved_task

    def setUp(self):
        self.setUpNative(); self.task = self.reserved_task(suffix='historical')
        backup_guard = patch.object(recovery,'validate_backup_location')
        self.backup_guard = backup_guard.start();self.addCleanup(backup_guard.stop)
        self.other = self.reserved_task(suffix='future')
        self.central_path = Path(self.temp.name) / 'central.sqlite3'
        self.central = DispatchStore(self.central_path, self.clock)
        key = 'tt:auto:' + str(self.task.id)
        self.job = self.central.offer(dict(source_key=key,channel='tt',family='auto',source_id=str(self.task.id),
            stage='publish',source_status='retry_wait',status='unknown',source_attempt_count=2,reconcile_only=True,
            resource_keys=['tt:account:' + self.task.account_id],scheduled_at=self.clock().isoformat()))
        with self.store._transaction() as c:
            c.execute("UPDATE tt_auto_task SET status='retry_wait',claim_phase='publish',publish_attempt_count=2,"
                      "gpu_job_id='ttauto-historical-frozen',error_code='tt_upstream_rejected',"
                      "prepared_media_url='https://media.example.test/frozen.mp4',prepared_output_sha256=?,"
                      "prepared_output_size=1234,claim_worker='',claim_token='',lease_expires_at_utc='' WHERE id=?",
                      ('a'*64,self.task.id))
            for attempt in range(2):
                c.execute("INSERT INTO tt_auto_event(run_id,task_id,event_type,from_status,to_status,created_at) "
                          "VALUES(?,?,'task_claimed_publish','retry_wait','publishing',?)",
                          (self.task.run_id,self.task.id,self.clock().isoformat()))
                c.execute("INSERT INTO tt_auto_event(run_id,task_id,event_type,from_status,to_status,message,created_at) "
                          "VALUES(?,?,'task_transient_failure','publishing','retry_wait',?,?)",
                          (self.task.run_id,self.task.id,'TikTok API rejected [HTTP 200, code='+recovery.CODE+', log_id=test]',self.clock().isoformat()))

    def gpu(self):
        return dict(node_id='hk-gpu',gpu_job_id='ttauto-historical-frozen',read_only=True,
            publish_ledger_matches=[],captured_at=self.clock().isoformat(),
            checked_roots=['/data/tt-post-publisher/publishes','/data/tt-post-publisher/direct-outro-work/publishes'])

    def plan(self):
        return recovery.make_plan(self.central_path,self.store.db_path,self.gpu(),self.clock(),
                                  self.task.id,self.task.account_id)

    def read_all(self, path, table):
        with contextlib.closing(recovery.read_connection(path)) as c:
            return recovery.rows(c,'SELECT * FROM '+table+' ORDER BY id')

    def apply(self, plan=None, evidence=None):
        plan=plan or self.plan()
        return recovery.apply_plan(plan,plan['plan_sha256'],evidence or self.gpu(),
                                   Path(self.temp.name)/'backup',self.clock())

    def test_plan_is_read_only_and_apply_only_closes_original_task_with_audit_backup(self):
        native_before=self.read_all(self.store.db_path,'tt_auto_task')
        central_before=self.read_all(self.central_path,'pipeline_job')
        events_before=self.read_all(self.store.db_path,'tt_auto_event')
        plan=self.plan()
        self.assertEqual(self.read_all(self.store.db_path,'tt_auto_task'),native_before)
        result=self.apply(plan)
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(self.read_all(result['backup'],'tt_auto_task'),native_before)
        self.assertEqual(self.read_all(self.central_path,'pipeline_job'),central_before)
        after=self.read_all(self.store.db_path,'tt_auto_task')
        changed={'status','claim_phase','next_attempt_at_utc','error_code','error_message','updated_at','finished_at_utc'}
        self.assertEqual({k:v for k,v in after[0].items() if k not in changed},
                         {k:v for k,v in native_before[0].items() if k not in changed})
        self.assertEqual(after[0]['status'],'failed');self.assertEqual(after[1],native_before[1])
        events_after=self.read_all(self.store.db_path,'tt_auto_event')
        self.assertEqual(events_after[:len(events_before)],events_before)
        self.assertEqual(events_after[len(events_before)]['event_type'],'historical_creator_rejection_closed')
        self.assertEqual(after[0]['finished_at_utc'],self.clock().isoformat(timespec='seconds'))
        with contextlib.closing(recovery.read_connection(self.store.db_path)) as c:
            ledger=c.execute('SELECT * FROM tt_auto_material_ledger WHERE task_id=?',(self.task.id,)).fetchone()
            self.assertEqual(ledger['last_task_status'],'failed')
            self.assertEqual(ledger['material_id'],self.task.material_id)
            self.assertEqual(c.execute('SELECT status FROM tt_auto_run WHERE id=?',(self.task.run_id,)).fetchone()[0],'failed')
        self.backup_guard.assert_called_once()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)

    def test_any_publish_unknown_or_active_evidence_rejects(self):
        for column,value in (('publish_id','known-publish'),('unknown_outcome',1),('claim_token','active'),
                             ('lease_expires_at_utc',self.clock().isoformat())):
            with self.subTest(column=column):
                with self.store._transaction() as c:
                    old=c.execute('SELECT '+column+' FROM tt_auto_task WHERE id=?',(self.task.id,)).fetchone()[0]
                    c.execute('UPDATE tt_auto_task SET '+column+'=? WHERE id=?',(value,self.task.id))
                with self.assertRaises(recovery.RecoveryRejected):self.plan()
                with self.store._transaction() as c:c.execute('UPDATE tt_auto_task SET '+column+'=? WHERE id=?',(old,self.task.id))

    def test_all_attempts_require_immediate_definite_preflight_rejection(self):
        with self.store._transaction() as c:
            event_id=c.execute("SELECT MIN(id) FROM tt_auto_event WHERE task_id=? AND event_type='task_transient_failure'",(self.task.id,)).fetchone()[0]
            c.execute("UPDATE tt_auto_event SET message='network outcome unavailable' WHERE id=?",(event_id,))
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_task_change_after_plan_and_hash_tamper_fail_cas(self):
        plan=self.plan()
        altered=copy.deepcopy(plan);altered['proof']['task_sha256']='f'*64
        with self.assertRaises(recovery.RecoveryRejected):self.apply(altered)
        with self.store._transaction() as c:c.execute("UPDATE tt_auto_task SET prepared_output_size=5678 WHERE id=?",(self.task.id,))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.store.get_task(self.task.id).status,'retry_wait')

    def test_central_platform_evidence_is_never_cleared(self):
        with contextlib.closing(sqlite3.connect(str(self.central_path))) as c:
            c.execute('UPDATE pipeline_job SET external_intent=1 WHERE id=?',(self.job['id'],));c.commit()
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_gpu_ledger_or_stale_absence_evidence_rejects(self):
        plan=self.plan()
        for changes in ({'publish_ledger_matches':['original-ledger.json']},
                        {'captured_at':(self.clock()-dt.timedelta(minutes=3)).isoformat()},
                        {'gpu_job_id':'different-job'}):
            with self.subTest(changes=changes):
                evidence=self.gpu();evidence.update(changes)
                with self.assertRaises(recovery.RecoveryRejected):self.apply(plan,evidence)

    def test_disk_guard_failure_happens_before_backup_or_native_write(self):
        plan=self.plan();self.backup_guard.side_effect=ValueError('expected_data_disk_not_mounted')
        with self.assertRaisesRegex(ValueError,'expected_data_disk'):self.apply(plan)
        self.assertEqual(self.store.get_task(self.task.id).status,'retry_wait')
        self.assertFalse((Path(self.temp.name)/'backup').exists())

    def test_run_refresh_failure_rolls_back_task_material_and_closure_event(self):
        plan=self.plan();before=self.read_all(self.store.db_path,'tt_auto_task')
        with patch.object(recovery,'refresh_run',side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):self.apply(plan)
        self.assertEqual(self.read_all(self.store.db_path,'tt_auto_task'),before)
        with contextlib.closing(recovery.read_connection(self.store.db_path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tt_auto_event WHERE event_type='historical_creator_rejection_closed'").fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()

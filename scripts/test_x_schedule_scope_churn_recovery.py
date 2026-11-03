"""Keep the first recovery audit immutable and admit only the second known failure."""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from features.pipeline_dispatch.x_schedule_recovery import authorized_run_ids
from scripts import recover_x_schedule_scope_churn as recovery
from scripts import recover_x_zero_write_schedules as first
from scripts import test_x_zero_write_schedule_recovery as base


class ScopeChurnRecoveryTests(unittest.TestCase):
    create=base.RecoveryTests.create
    row=base.RecoveryTests.row
    update=base.RecoveryTests.update

    def setUp(self):
        base.RecoveryTests.setUp(self)
        anchors={}
        for rid in (1034,1036):
            self.create(rid,'2026-11-03','x_accounts_unavailable')
        previous=first.plan(self.path,[1034,1036],['2026-11-03'])
        first.apply(self.path,previous,previous['plan_sha256'],Path(self.temp.name)/'first-backup','first-reviewer','original recovery')
        for rid in (1034,1036):
            failed='2026-11-03T11:44:%02dZ'%(rid%60)
            self.update("UPDATE x_post_schedule_run SET status='failed_preflight',error_code='x_post_schedule_account_state_changed',error_message='scope changed',finished_at=?,updated_at=? WHERE id=?",(failed,failed,rid))
            row=self.row(rid)
            with contextlib.closing(recovery.read_connection(self.path)) as c:
                audit=dict(c.execute('SELECT * FROM '+recovery.FIRST_TABLE+' WHERE run_id=?',(rid,)).fetchone())
            anchors[rid]=dict(publish_time=row['publish_time'],failed_at=failed,identity_sha256=recovery.fingerprint(recovery.identity(row)),run_sha256=recovery.fingerprint(row),first_audit_sha256=recovery.fingerprint(audit))
        patch=mock.patch.object(recovery,'EXACT',anchors);patch.start();self.addCleanup(patch.stop)
        self.blob=(Path(__file__).with_name('x_post_schedule_runner.py')).read_bytes()
        self.runner=Path(self.temp.name)/'runner.py';self.runner.write_bytes(self.blob)
        git=mock.patch.object(recovery.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=self.blob));git.start();self.addCleanup(git.stop)
        self.create(1039,'2026-11-03','',status='claimed')

    def plan(self,ids=None):
        return recovery.plan(self.path,ids or [1034,1036],'a'*40,self.runner,Path(self.temp.name))

    def apply(self,plan):
        return recovery.apply(self.path,plan,Path(self.temp.name)/'second-backup','second-reviewer','same original zero-write tasks')

    def audit_rows(self,table):
        with contextlib.closing(recovery.read_connection(self.path)) as c:return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY run_id')]

    def test_exact_two_preserve_first_audits_snapshot_and_other_active_scan(self):
        original=self.audit_rows(recovery.FIRST_TABLE);other=self.row(1039)
        before={rid:self.row(rid) for rid in (1034,1036)}
        plan=self.plan();result=self.apply(plan)
        self.assertFalse(result['platform_called']);self.assertFalse(result['first_audit_modified'])
        self.assertEqual(self.audit_rows(recovery.FIRST_TABLE),original)
        self.assertEqual(self.row(1039),other)
        for rid in (1034,1036):
            current=self.row(rid)
            self.assertEqual(current['status'],'claimed')
            self.assertEqual(current['material_pool_snapshot_max_id'],4444)
            self.assertEqual(recovery.identity(current),recovery.identity(before[rid]))
            self.assertEqual(current['plan_attempted_at'],'')
        audits=self.audit_rows(recovery.TABLE);self.assertEqual(len(audits),2)
        self.assertEqual(json.loads(audits[0]['evidence_json'])['previous_run'],before[1034])
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            self.assertEqual(set(authorized_run_ids(c)),{1034,1036})
            self.assertEqual(c.execute('SELECT count(*) FROM x_post_queue').fetchone()[0],0)

    def test_repeat_apply_after_progress_never_resets_or_appends_new_authorization(self):
        plan=self.plan();self.apply(plan)
        self.update("UPDATE x_post_schedule_run SET status='running',queued_count=1,plan_attempted_at='started' WHERE id=1034")
        current=self.row(1034)
        self.assertTrue(self.apply(plan)['already_applied'])
        self.assertEqual(self.row(1034),current)
        self.assertEqual(len(self.audit_rows(recovery.TABLE)),2)
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_future_failure_and_scope_widening_are_not_accepted(self):
        with self.assertRaises(recovery.RecoveryRejected):self.plan([1039])
        with self.assertRaises(recovery.RecoveryRejected):self.plan([1034,1034])
        self.update("UPDATE x_post_schedule_run SET finished_at='2026-11-03T13:00:00Z',updated_at='2026-11-03T13:00:00Z' WHERE id=1034")
        with self.assertRaises(recovery.RecoveryRejected):self.plan([1034])

    def test_first_audit_and_current_identity_cannot_change(self):
        plan=self.plan()
        self.update("UPDATE "+recovery.FIRST_TABLE+" SET reason='changed' WHERE run_id=1034")
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.row(1034)['status'],'failed_preflight')

    def test_any_plan_attempt_counter_or_snapshot_drift_refuses(self):
        for field,value in [('plan_attempted_at','attempt'),('unknown_count',1),('queued_count',1),('material_pool_snapshot_max_id',4445),('body_template','changed')]:
            with self.subTest(field=field):
                original=self.row(1034)[field];plan=self.plan()
                self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1034',(value,))
                with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
                self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1034',(original,))

    def test_deployed_runner_must_match_committed_scope_wait_fix(self):
        plan=self.plan();self.runner.write_bytes(self.blob+b'\n# changed\n')
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.row(1034)['status'],'failed_preflight')

    def test_snapshot_cas_after_backup_and_readonly_plan(self):
        def dump():
            with contextlib.closing(recovery.read_connection(self.path)) as c:return '\n'.join(c.iterdump())
        before=dump();plan=self.plan();self.assertEqual(before,dump())
        real_backup=recovery.backup
        def drift(*args):
            result=real_backup(*args);self.update('UPDATE x_post_schedule_run SET unknown_count=1 WHERE id=1034');return result
        with mock.patch.object(recovery,'backup',side_effect=drift):
            with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(recovery.TABLE,)).fetchone())
        self.assertEqual(self.row(1036)['status'],'failed_preflight')


if __name__=='__main__':unittest.main()

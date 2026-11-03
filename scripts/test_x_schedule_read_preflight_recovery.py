import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import recover_x_schedule_read_preflight as recovery
from scripts import recover_x_schedule_scope_churn as second
from scripts import recover_x_zero_write_schedules as first
from scripts import test_x_zero_write_schedule_recovery as base


class ReadPreflightRecoveryTests(unittest.TestCase):
    create=base.RecoveryTests.create
    row=base.RecoveryTests.row
    update=base.RecoveryTests.update

    def setUp(self):
        base.RecoveryTests.setUp(self)
        self.create(1029,'2026-11-02','x_upstream_error')
        self.create(1034,'2026-11-03','x_accounts_unavailable')
        initial=first.plan(self.path,[1029,1034],['2026-11-02','2026-11-03'])
        first.apply(self.path,initial,initial['plan_sha256'],Path(self.temp.name)/'first','operator','original recovery')
        self.update(second.SCHEMA)
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            previous=dict(c.execute('SELECT * FROM '+recovery.FIRST+' WHERE run_id=1034').fetchone())
        self.update('INSERT INTO '+recovery.SECOND+' VALUES(?,?,?,?,?,?,?,?,?,?)',(1034,'previous-failure',previous['identity_sha256'],recovery.fingerprint(previous),'previous-plan','operator','second recovery','previous-time','previous-backup',json.dumps({'previous_failure':'scope churn'})))
        anchors={}
        for rid in (1029,1034):
            code='x_post_drama_query_failed' if rid==1029 else 'x_upstream_error'
            failed='2026-11-03T11:59:%02dZ'%(rid%60)
            self.update("UPDATE x_post_schedule_run SET status='failed_preflight',error_code=?,error_message='unclassified old read error',finished_at=?,updated_at=? WHERE id=?",(code,failed,failed,rid))
            run=self.row(rid)
            with contextlib.closing(recovery.read_connection(self.path)) as c:
                audits={t:recovery.fingerprint(recovery.audit(c,t,rid)[0]) for t in (recovery.FIRST,recovery.SECOND) if recovery.audit(c,t,rid)}
            anchors[rid]=dict(failed_at=failed,error_code=code,snapshot=run['material_pool_snapshot_max_id'],identity_sha256=recovery.fingerprint(recovery.identity(run)),run_sha256=recovery.fingerprint(run),audits=audits)
        patch=mock.patch.object(recovery,'EXACT',anchors);patch.start();self.addCleanup(patch.stop)
        root=Path(__file__).resolve().parents[1];self.deployed=Path(self.temp.name)/'runtime';blobs={}
        for name in recovery.FILES:
            blobs[name]=(root/name).read_bytes();target=self.deployed/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(blobs[name])
        self.runner=self.deployed/recovery.FILES[0]
        git=mock.patch.object(recovery.subprocess,'run',side_effect=lambda command,**kwargs:SimpleNamespace(returncode=0,stdout=blobs[command[-1].split(':',1)[1]]));git.start();self.addCleanup(git.stop)
        self.create(1039,'2026-11-03','',status='queued');self.update('UPDATE x_post_schedule_run SET queued_count=38,expected_count=38 WHERE id=1039')

    def plan(self,ids=None):return recovery.plan(self.path,ids or [1029,1034],'b'*40,self.runner,Path(self.temp.name))
    def apply(self,plan):return recovery.apply(self.path,plan,Path(self.temp.name)/'third','reviewer','one original zero-write recovery')
    def audits(self):
        with contextlib.closing(recovery.read_connection(self.path)) as c:return {t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in (recovery.FIRST,recovery.SECOND)}

    def test_exact_once_preserves_both_predecessor_audits_and_queued_1039(self):
        previous=self.audits();other=self.row(1039);before={rid:self.row(rid) for rid in (1029,1034)}
        plan=self.plan();result=self.apply(plan)
        self.assertFalse(result['platform_called']);self.assertEqual(self.audits(),previous);self.assertEqual(self.row(1039),other)
        for rid in (1029,1034):
            self.assertEqual(self.row(rid)['status'],'claimed');self.assertEqual(recovery.identity(self.row(rid)),recovery.identity(before[rid]))
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            saved=[dict(r) for r in c.execute('SELECT * FROM '+recovery.TABLE)]
            self.assertEqual(len(saved),2)
            for row in saved:self.assertEqual(json.loads(row['evidence_json'])['previous_run'],before[row['run_id']])
            self.assertEqual(c.execute('SELECT count(*) FROM x_post_queue').fetchone()[0],0)
        self.update("UPDATE x_post_schedule_run SET status='running',plan_attempted_at='new-attempt' WHERE id=1029")
        current=self.row(1029);self.assertTrue(self.apply(plan)['already_applied']);self.assertEqual(self.row(1029),current)

    def test_any_previous_audit_change_blocks_without_overwriting_it(self):
        plan=self.plan();self.update('UPDATE '+recovery.SECOND+" SET reason='new reason' WHERE run_id=1034")
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.row(1029)['status'],'failed_preflight')

    def test_future_same_code_failure_queue_fence_unknown_and_identity_are_not_admitted(self):
        with self.assertRaises(recovery.RecoveryRejected):self.plan([1039])
        for field,value in [('plan_attempted_at','written'),('unknown_count',1),('queued_count',1),('finished_at','later-failure'),('material_pool_snapshot_max_id',5555),('body_template','changed')]:
            old=self.row(1029)[field];plan=self.plan();self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1029',(value,))
            with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
            self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1029',(old,))

    def test_all_four_deployed_files_must_match_committed_fix(self):
        plan=self.plan();path=self.deployed/'features/x_posts/drama_selector.py';path.write_bytes(path.read_bytes()+b'\n# changed\n')
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.row(1034)['status'],'failed_preflight')

    def test_readonly_plan_and_compare_after_backup(self):
        def dump():
            with contextlib.closing(recovery.read_connection(self.path)) as c:return '\n'.join(c.iterdump())
        before=dump();plan=self.plan();self.assertEqual(before,dump());original=recovery.backup
        def changed(*args):
            result=original(*args);self.update('UPDATE x_post_schedule_run SET unknown_count=1 WHERE id=1034');return result
        with mock.patch.object(recovery,'backup',side_effect=changed),self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.row(1029)['status'],'failed_preflight')


if __name__=='__main__':unittest.main()

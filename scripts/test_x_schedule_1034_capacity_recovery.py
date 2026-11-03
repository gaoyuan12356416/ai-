"""Exact fourth recovery keeps the rejected plan fence and all prior audits."""
import ast
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from features.pipeline_dispatch.x_schedule_recovery import authorized_run_ids
from scripts import recover_x_schedule_1034_capacity as recovery
from scripts import recover_x_zero_write_schedules as first
from scripts import recover_x_schedule_scope_churn as second
from scripts import recover_x_schedule_read_preflight as third
from scripts import test_x_zero_write_schedule_recovery as base
from scripts.test_x_post_multi_schedule_store import base_candidate


class CapacityRecoveryTests(unittest.TestCase):
    create = base.RecoveryTests.create
    row = base.RecoveryTests.row
    update = base.RecoveryTests.update

    def setUp(self):
        base.RecoveryTests.setUp(self)
        self.create(1034, '2026-11-03', recovery.ERROR)
        self.update("UPDATE x_post_schedule_run SET publish_time='08:45',config_version=33,"
                    "account_ids_json=?,expected_count=89,error_message=?,finished_at=?,updated_at=?,"
                    "plan_attempted_at=?,lease_heartbeat_at=? WHERE id=1034",
                    (json.dumps(list(range(1,90))), recovery.MESSAGE, *([recovery.FAILED_AT]*4)))
        identity_sha = recovery.fingerprint(recovery.identity(self.row(1034)))
        self.update(first.SCHEMA); self.update(second.SCHEMA); self.update(third.SCHEMA)
        self.update('INSERT INTO '+recovery.FIRST+' VALUES(?,?,?,?,?,?,?,?)',
                    (1034, identity_sha, 'first-plan', 'operator', 'first', 'time', 'backup1', '{"old":"failure1"}'))
        self.update('INSERT INTO '+recovery.SECOND+' VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (1034, 'previous2', identity_sha, 'first-sha', 'second-plan', 'operator', 'second', 'time', 'backup2', '{"old":"failure2"}'))
        self.update('INSERT INTO '+recovery.THIRD+' VALUES(?,?,?,?,?,?,?,?,?)',
                    (1034, 'previous3', identity_sha, 'third-plan', 'operator', 'third', 'time', 'backup3', '{"old":"failure3"}'))
        self.create(1036, '2026-11-03', '', 'queued')
        self.update("UPDATE x_post_schedule_run SET publish_time='12:41',expected_count=42,queued_count=42,"
                    "account_ids_json=?,started_at='2026-11-03T13:31:33Z',plan_attempted_at='2026-11-03T13:31:33Z' WHERE id=1036",
                    (json.dumps(list(range(1,43))),))
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            for i, qid in enumerate(range(5667,5709), 1):
                payload = base_candidate(i, 'fixture'+str(i), 5000+i, 'content'+str(i))
                payload.update(id=qid, idempotency_key='predecessor-'+str(qid), schedule_run_id=1036,
                    material_key=str(5000+i), run_date='2026-11-03', created_at='2026-11-03T13:31:33Z',
                    updated_at='2026-11-03T13:31:33Z', random_template_enabled=1, duration_policy_version=2)
                c.execute('INSERT INTO x_post_queue('+','.join(payload)+') VALUES('+','.join('?' for _ in payload)+')', list(payload.values()))
            c.commit()
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            audits = {t:recovery.fingerprint(recovery.audit(c,t,1034)[0]) for t in (recovery.FIRST,recovery.SECOND,recovery.THIRD)}
            queues = recovery.rows(c, 'SELECT '+','.join(recovery.QUEUE_FIELDS)+' FROM x_post_queue WHERE schedule_run_id=1036 ORDER BY id')
        anchor = dict(run_sha256=recovery.fingerprint(self.row(1034)), identity_sha256=identity_sha,
                      failure_sha256=recovery.fingerprint(recovery.failure_identity(self.row(1034))), audits=audits,
                      predecessor_identity_sha256=recovery.fingerprint(recovery.identity(self.row(1036))),
                      predecessor_queues_sha256=recovery.fingerprint(queues))
        self.patch('ANCHOR', anchor)
        self.runtime = Path(self.temp.name)/'runtime'; root = Path(__file__).resolve().parents[1]
        self.blobs = {}
        for name in third.FILES:
            self.blobs[name] = (root/name).read_bytes()
            path = self.runtime/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(self.blobs[name])
        self.runner = self.runtime/third.FILES[0]
        self.historical = ("class Store:\n    def create_schedule_plan(self):\n"
            "        if True:\n            raise XPostError(%r, %r, 409)\n"
            "        with self.connect() as conn:\n            conn.execute('BEGIN IMMEDIATE')\n" %
            (recovery.ERROR, recovery.MESSAGE)).encode('utf-8')
        function = next(n for n in ast.walk(ast.parse(self.historical)) if isinstance(n,ast.FunctionDef))
        self.patch('FUNCTION_SHA256', hashlib.sha256(b''.join(self.historical.splitlines(keepends=True)[function.lineno-1:function.end_lineno])).hexdigest())
        git = mock.patch.object(recovery.subprocess, 'run', side_effect=self.git)
        git.start(); self.addCleanup(git.stop)

    def patch(self, name, value):
        patch = mock.patch.object(recovery,name,value); patch.start(); self.addCleanup(patch.stop)

    def git(self, command, **kwargs):
        revision, path = command[-1].split(':',1)
        return SimpleNamespace(returncode=0, stdout=self.historical if revision==recovery.HISTORICAL_COMMIT else self.blobs[path])

    def plan(self):
        return recovery.plan(self.path, 'b'*40, self.runner, Path(self.temp.name))

    def apply(self, saved):
        return recovery.apply(self.path, saved, Path(self.temp.name)/'fourth', 'operator', 'confirmed pretransaction rejection')

    def old_audits(self):
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            return {t:recovery.audit(c,t,1034) for t in (recovery.FIRST,recovery.SECOND,recovery.THIRD)}

    def test_one_admission_keeps_original_plan_attempt_and_three_audits(self):
        before, preceding, old = self.row(1034), self.row(1036), self.old_audits()
        saved = self.plan()
        with mock.patch('requests.sessions.Session.request',side_effect=AssertionError('no network')):
            result = self.apply(saved)
        self.assertFalse(result['platform_called']); self.assertEqual(result['queues_created'],0)
        current = self.row(1034)
        self.assertEqual((current['status'],current['plan_attempted_at']),('claimed',''))
        self.assertEqual(recovery.identity(current),recovery.identity(before))
        self.assertEqual(current['material_pool_snapshot_max_id'],4444)
        self.assertEqual(self.row(1036),preceding); self.assertEqual(self.old_audits(),old)
        with contextlib.closing(recovery.read_connection(self.path)) as c:
            audit = recovery.audit(c,recovery.TABLE,1034)[0]
            proof = json.loads(audit['evidence_json'])
            self.assertEqual(audit['previous_plan_attempted_at'],recovery.FAILED_AT)
            self.assertEqual(proof['previous_run'],before); self.assertEqual(proof['previous_audits'],{k:v[0] for k,v in old.items()})
            self.assertEqual(c.execute('SELECT count(*) FROM x_post_queue WHERE schedule_run_id=1034').fetchone()[0],0)
            self.assertIn(1034,authorized_run_ids(c))
        with contextlib.closing(sqlite3.connect(result['backup_path'])) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT plan_attempted_at FROM x_post_schedule_run WHERE id=1034').fetchone()[0],recovery.FAILED_AT)

    def test_repeated_apply_after_progress_is_noop(self):
        saved = self.plan(); self.apply(saved)
        self.update("UPDATE x_post_schedule_run SET status='running',queued_count=1,plan_attempted_at='new-attempt' WHERE id=1034")
        before = self.row(1034)
        with mock.patch.object(recovery,'backup',side_effect=AssertionError('duplicate backup')):
            self.assertTrue(self.apply(saved)['already_applied'])
        self.assertEqual(self.row(1034),before)

    def test_unknown_or_future_failure_changed_fence_scope_and_owner_rejected(self):
        original = self.row(1034)
        for field, value in [('error_code','x_post_schedule_plan_unknown'),('error_message','other FIFO error'),
                ('finished_at','future'),('plan_attempted_at','other attempt'),('plan_attempted_at',''),
                ('status','claimed'),('unknown_count',1),('queued_count',1),('started_at','active'),
                ('material_pool_snapshot_max_id',4445),('body_template','changed')]:
            saved=self.plan(); self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1034',(value,))
            with self.subTest(field=field,value=value),self.assertRaises(recovery.RecoveryRejected):self.apply(saved)
            self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1034',(original[field],))

    def test_real_queue_blocks_even_if_summary_counts_are_zero(self):
        saved=self.plan(); self.update('UPDATE x_post_queue SET schedule_run_id=1034 WHERE id=5667')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'original_run_has_queue'):self.apply(saved)

    def test_each_previous_audit_is_required_unchanged(self):
        for table in (recovery.FIRST,recovery.SECOND,recovery.THIRD):
            saved=self.plan(); old=self.old_audits()[table][0]['reason']
            self.update('UPDATE '+table+" SET reason='drift' WHERE run_id=1034")
            with self.subTest(table=table),self.assertRaisesRegex(recovery.RecoveryRejected,'previous_audit'):self.apply(saved)
            self.update('UPDATE '+table+' SET reason=? WHERE run_id=1034',(old,))
        self.update('DELETE FROM '+recovery.THIRD+' WHERE run_id=1034')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'previous_audit'):self.plan()

    def test_1036_must_retain_its_real_committed_queue_identity(self):
        original=self.row(1036)
        for field,value in [('status','claimed'),('plan_attempted_at',''),('queued_count',0),('expected_count',41)]:
            saved=self.plan(); self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1036',(value,))
            with self.assertRaisesRegex(recovery.RecoveryRejected,'preceding_plan'):self.apply(saved)
            self.update('UPDATE x_post_schedule_run SET '+field+'=? WHERE id=1036',(original[field],))
        self.update("UPDATE x_post_queue SET material_key='other' WHERE id=5667")
        with self.assertRaisesRegex(recovery.RecoveryRejected,'preceding_queue_identity'):self.plan()

    def test_1036_publication_progress_does_not_change_plan_or_block(self):
        saved=self.plan()
        self.update("UPDATE x_post_queue SET status='published' WHERE id=5667")
        self.update("UPDATE x_post_schedule_run SET status='running',published_count=1,updated_at='later' WHERE id=1036")
        self.assertTrue(self.apply(saved)['applied'])

    def test_readonly_plan_and_backup_to_cas_drift(self):
        def dump():
            with contextlib.closing(recovery.read_connection(self.path)) as c:return '\n'.join(c.iterdump())
        before=dump(); saved=self.plan(); self.assertEqual(before,dump()); original=recovery.backup
        def changed(*args):
            result=original(*args); self.update("UPDATE x_post_schedule_run SET lease_heartbeat_at='other-owner' WHERE id=1034"); return result
        with mock.patch.object(recovery,'backup',side_effect=changed),self.assertRaises(recovery.RecoveryRejected):self.apply(saved)
        with contextlib.closing(recovery.read_connection(self.path)) as c:self.assertEqual(recovery.audit(c,recovery.TABLE,1034),[])

    def test_deployed_file_and_historical_proof_drift_rejected(self):
        saved=self.plan(); selector=self.runtime/'features/x_posts/selector.py'
        selector.write_bytes(selector.read_bytes()+b'\n# changed\n')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'deployed'):self.apply(saved)
        selector.write_bytes(self.blobs['features/x_posts/selector.py'])
        self.historical=self.historical.replace(b'409',b'500')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'historical'):self.plan()

    def test_fourth_audit_is_immutable_and_different_plan_cannot_rearm(self):
        saved=self.plan(); self.apply(saved)
        for statement in ('UPDATE '+recovery.TABLE+" SET reason='changed'",'DELETE FROM '+recovery.TABLE,
                          'INSERT OR REPLACE INTO '+recovery.TABLE+' SELECT * FROM '+recovery.TABLE):
            with contextlib.closing(sqlite3.connect(self.path)) as c,self.assertRaises(sqlite3.IntegrityError):c.execute(statement)
        other=copy.deepcopy(saved); other['item']['queue_count']=1
        other['plan_sha256']=recovery.fingerprint({k:v for k,v in other.items() if k!='plan_sha256'})
        with self.assertRaisesRegex(recovery.RecoveryRejected,'different_fourth'):self.apply(other)


if __name__ == '__main__':
    unittest.main()

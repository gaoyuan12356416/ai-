"""Exact first-snapshot completion with real SQLite and offline verification."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from features.pipeline_dispatch.core import DispatchStore
from features.x_auto_posts.core import XAutoPostStore
from scripts import recover_x_auto_missing_snapshots as recovery
from scripts import test_pipeline_x_native as native_fixture


class MissingSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.base = native_fixture.NativeTests(); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root = self.base.root; self.native = self.base.runtime.POST_DB_PATH
        self.auto = self.root / 'auto.sqlite3'; self.central = self.root / 'central.sqlite3'
        self.now = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)
        self.store = XAutoPostStore(self.auto, now_fn=lambda: self.now)
        self.dispatch = DispatchStore(self.central, clock=lambda: self.now)
        config = {'account_ids': ['7', '9'], 'language_source': 'account', 'body_template': '{{desc}}'}
        self.accounts = {}
        with sqlite3.connect(self.native) as n:
            n.execute('CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,x_user_id TEXT,username TEXT,status TEXT,publish_approved INTEGER,drama_language TEXT)')
            for aid in (7, 9):
                n.execute('INSERT INTO x_authorized_account VALUES(?,?,?, ?,1,?)', (aid, str(200 + aid), 'account' + str(aid), 'active', 'en'))
                self.accounts[aid] = {'id': aid, 'x_user_id': str(200 + aid), 'username': 'account' + str(aid),
                    'display_name': 'Account ' + str(aid), 'status': 'active', 'publish_approved': True, 'publish_eligible': True,
                    'access_token_expired': False, 'refresh_token_available': True, 'authorization_refreshable': True,
                    'access_token_status': 'active', 'drama_language': 'en', 'subscription_type': 'none',
                    'premium_subscriber': False, 'long_video_eligible': False, 'long_video_publish_eligible': False, 'protected': False}
        with self.store._transaction() as a:
            a.execute("INSERT INTO x_auto_template(id,name,enabled,current_version,created_at,updated_at) VALUES(2,'Original',1,10,?,?)", (self.now.isoformat(),) * 2)
            for tid, (run, template, version, aid) in recovery.SCOPE.items():
                a.execute('INSERT INTO x_auto_template_version(template_id,version,config_json,config_sha256,created_at) VALUES(?,?,?,?,?)',
                    (template, version, recovery.encoded(config), recovery.digest(config), self.now.isoformat()))
                a.execute("INSERT INTO x_auto_run(id,run_key,template_id,template_version,config_sha256,trigger_type,scheduled_at_utc,shanghai_date,publish_time,status,created_at,updated_at) VALUES(?,?,?,?,?,'auto',?,'2026-11-03','08:32','running',?,?)",
                    (run, 'original-' + str(run), template, version, recovery.digest(config), self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
                a.execute("INSERT INTO x_auto_task(id,run_id,template_id,template_version,account_id,trigger_priority,scheduled_at_utc,status,language,account_snapshot_version,account_snapshot_json,body_template,error_code,created_at,updated_at) VALUES(?,?,?,?,?,1,?,'failed','',2,?,'{{desc}}','x_accounts_unavailable',?,?)",
                    (tid, run, template, version, aid, self.now.isoformat(), recovery.encoded(recovery.EMPTY_SNAPSHOT), self.now.isoformat(), self.now.isoformat()))
                for event in ('task_created', 'task_account_snapshot_failed'):
                    a.execute('INSERT INTO x_auto_event(run_id,task_id,event_type,created_at) VALUES(?,?,?,?)', (run, tid, event, self.now.isoformat()))
        with sqlite3.connect(self.central) as c:
            for tid in recovery.SCOPE:
                c.execute("INSERT INTO pipeline_job(id,source_key,source_id,channel,family,batch_key,stage,scheduled_at,source_created_at,status,hold_reason,created_at,updated_at) VALUES(?,?,?,'x','auto','original','prepare',?,?,'failed','adapter_unavailable',?,?)",
                    (tid, 'x:auto:' + str(tid), str(tid), self.now.isoformat(), self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        self.runtime = {'pid': 123, 'dispatch_enabled': True, 'bridge_enabled': True}
        with self.store._reader() as a:
            original = {tid: recovery.digest(dict(a.execute('SELECT * FROM x_auto_task WHERE id=?', (tid,)).fetchone())) for tid in recovery.SCOPE}
        for name, value in (('runtime_proof', lambda _: copy.deepcopy(self.runtime)), ('ORIGINAL_SHA', original)):
            patcher = mock.patch.object(recovery, name, value); patcher.start(); self.addCleanup(patcher.stop)
        self.bridge = mock.Mock()
        self.bridge.verify_account.side_effect = lambda aid, **_: dict(self.accounts[aid])
        self.evidence = recovery.capture_accounts(self.bridge, now_fn=lambda: self.now)

    def plan(self):
        return recovery.make_plan(self.auto, self.native, self.central, self.evidence, now=self.now)

    def apply(self, plan):
        return recovery.apply(plan, self.root / 'backups', 'operator', now=self.now)

    def test_complete_only_missing_snapshot_keep_original_identity_attempts_and_history(self):
        plan = self.plan(); native, central = self.native.read_bytes(), self.central.read_bytes()
        with mock.patch('requests.sessions.Session.request', side_effect=AssertionError('no network')):
            result = self.apply(plan)
        self.assertEqual(result['publish_requests'], 0)
        self.assertEqual((native, central), (self.native.read_bytes(), self.central.read_bytes()))
        allowed = {'status', 'language', 'account_username', 'account_display_name', 'account_snapshot_json',
            'error_code', 'error_message', 'finished_at_utc', 'updated_at'}
        with self.store._reader() as a:
            for old in plan['items']:
                task = dict(a.execute('SELECT * FROM x_auto_task WHERE id=?', (old['task']['id'],)).fetchone())
                self.assertEqual(task['status'], 'pending'); self.assertEqual(task['language'], 'en')
                self.assertEqual({k: v for k, v in task.items() if k not in allowed}, {k: v for k, v in old['task'].items() if k not in allowed})
                proof = json.loads(a.execute('SELECT evidence_json FROM ' + recovery.AUDIT + ' WHERE task_id=?', (task['id'],)).fetchone()[0])
                self.assertEqual(proof['previous']['task'], old['task'])
                self.assertEqual(json.loads(proof['previous']['task']['account_snapshot_json']), recovery.EMPTY_SNAPSHOT)
                self.assertEqual(dict(a.execute('SELECT * FROM x_auto_run WHERE id=?', (task['run_id'],)).fetchone()), old['run'])
            self.assertEqual(a.execute('SELECT count(*) FROM x_auto_material_ledger').fetchone()[0], 0)
        with sqlite3.connect(result['backup']) as b:
            self.assertEqual(b.execute('PRAGMA quick_check').fetchall(), [('ok',)])
            self.assertEqual(b.execute('SELECT DISTINCT language,status FROM x_auto_task').fetchall(), [('', 'failed')])
        self.assertTrue(self.apply(plan)['already_applied'])
        self.assertEqual(len(list((self.root / 'backups').glob('*.sqlite3'))), 1)
        if os.name != 'nt':
            self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)
        claim = self.store.claim_next_executable_task(worker_id='normal-selector', lease_seconds=120, now=self.now)
        self.assertEqual(claim.task.language, 'en'); self.assertEqual(claim.claim_phase, 'selection')

    def test_capture_only_uses_standard_verify_and_strips_extra_fields(self):
        self.accounts[7]['access_token'] = 'must-not-be-saved'
        evidence = recovery.capture_accounts(self.bridge, now_fn=lambda: self.now)
        self.assertNotIn('must-not-be-saved', json.dumps(evidence))
        self.assertEqual(set(evidence['accounts']['7']['item']), recovery.ACCOUNT_FIELDS)
        for call in self.bridge.verify_account.call_args_list:
            self.assertEqual(call.kwargs, {'only_refresh_required': False, 'preserve_transient_status': True, 'require_publish_approved': True})
        self.bridge.publish_queue.assert_not_called()

    def test_stale_unknown_ineligible_or_language_missing_account_is_not_admitted(self):
        original = copy.deepcopy(self.evidence)
        mutations = [lambda x: x['accounts']['7'].update(verified_at=(self.now - timedelta(seconds=121)).isoformat()),
            lambda x: x['accounts']['7']['item'].update(publish_eligible=False),
            lambda x: x['accounts']['7']['item'].update(publish_approved=False),
            lambda x: x['accounts']['7']['item'].update(status='error'),
            lambda x: x['accounts']['7']['item'].update(drama_language=''),
            lambda x: x['accounts']['7']['item'].update(id=9)]
        for change in mutations:
            with self.subTest(change=change):
                self.evidence = copy.deepcopy(original); change(self.evidence)
                with self.assertRaises(Exception): self.plan()
        self.evidence = original

    def test_original_task_changes_never_overwrite_language_attempts_or_claims(self):
        for field, value in (('language', 'ja'), ('unknown_outcome', 1), ('preparation_attempt_count', 1),
            ('publish_attempt_count', 1), ('claim_token', 'claimed'), ('execution_run_id', 99), ('status', 'canceled')):
            with self.subTest(field=field):
                with self.store._transaction() as a:
                    old = a.execute('SELECT ' + field + ' FROM x_auto_task WHERE id=2658').fetchone()[0]
                    a.execute('UPDATE x_auto_task SET ' + field + '=? WHERE id=2658', (value,))
                with self.assertRaisesRegex(recovery.RecoveryRejected, 'original_task_changed'): self.plan()
                with self.store._transaction() as a: a.execute('UPDATE x_auto_task SET ' + field + '=? WHERE id=2658', (old,))

    def test_account_approval_and_central_execution_drift_abort_without_partial_update(self):
        plan = self.plan()
        with sqlite3.connect(self.native) as n: n.execute('UPDATE x_authorized_account SET publish_approved=0 WHERE id=9')
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_account_blocked'): self.apply(plan)
        self.assertEqual(self.store.get_task(2658).status, 'failed')
        with sqlite3.connect(self.native) as n: n.execute('UPDATE x_authorized_account SET publish_approved=1 WHERE id=9')
        with sqlite3.connect(self.central) as c: c.execute('UPDATE pipeline_job SET generation=1 WHERE id=2674')
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'central_control_projection_has_execution'): self.apply(plan)
        self.assertEqual(self.store.get_task(2658).status, 'failed')

    def test_new_native_execution_or_ledger_hold_or_parent_cancel_blocks(self):
        with self.store._transaction() as a:
            a.execute("INSERT INTO pipeline_canonical_hold VALUES('x:auto:2658','now','operator')")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'task_held'): self.plan()
        with self.store._transaction() as a:
            a.execute('DELETE FROM pipeline_canonical_hold')
            a.execute("UPDATE x_auto_run SET status='canceled' WHERE id=418")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'parent_not_running'): self.plan()

    def test_native_execution_and_material_ledger_are_independent_hard_stops(self):
        self.base.bridge.store.create_auto_template_run('500', 7, 'x-auto-task-2658', 'x-auto-template-2',
            9, '{{drama_name}}\n{{desc}}\n{{url}}', native_fixture.ACTOR)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'native_execution_exists'): self.plan()
        with self.store._transaction() as a:
            a.execute("INSERT INTO x_auto_material_ledger(material_id,task_id,run_id,template_id,content_id,reserved_at_utc,last_task_status,updated_at) VALUES('500',2658,418,2,'drama',?,'failed',?)", (self.now.isoformat(),) * 2)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'material_ledger_exists'): self.plan()

    def test_full_plan_scope_and_snapshot_cas(self):
        plan = self.plan()
        changed = copy.deepcopy(plan); changed['items'].pop()
        changed['plan_sha256'] = recovery.document_hash(changed)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'exact_two_tasks_required'): self.apply(changed)
        changed = copy.deepcopy(plan); changed['items'][0]['snapshot']['display_name'] = 'other'
        changed['plan_sha256'] = recovery.document_hash(changed)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_state_drift'): self.apply(changed)
        self.assertEqual(self.store.get_task(2658).status, 'failed')

    def test_expired_verification_and_runtime_change_cannot_apply_a_saved_plan(self):
        plan = self.plan()
        self.now += timedelta(seconds=121)
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'account_verification_not_fresh'): self.apply(plan)
        self.now -= timedelta(seconds=121); self.runtime['pid'] = 999
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'runtime_changed'): self.apply(plan)

    def test_audit_cannot_be_updated_deleted_or_replaced(self):
        self.apply(self.plan())
        with self.store._transaction() as a:
            for sql in ('DELETE FROM ' + recovery.AUDIT, "UPDATE " + recovery.AUDIT + " SET created_at='changed'",
                'INSERT OR REPLACE INTO ' + recovery.AUDIT + ' SELECT * FROM ' + recovery.AUDIT):
                with self.assertRaises(sqlite3.IntegrityError): a.execute(sql)


if __name__ == '__main__': unittest.main()

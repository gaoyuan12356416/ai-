"""Offline exact-scope handoff against real dispatch and native SQLite ledgers."""
import copy
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.tt_native import TTNativeBridge
from scripts import test_tt_auto_post_publisher as fixtures
from scripts import recover_pipeline_tt_selection as recovery
from scripts.inspect_pipeline_tt_handoff_nodes import inspect_nodes, probe_node


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TTAutoPostPublisherIntegrationTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.now = self.f.clock()
        self.central = Path(self.f.temp.name) / 'dispatch.db'
        self.store = DispatchStore(self.central, lambda: self.now)
        self.bridge = TTNativeBridge(SimpleNamespace(store=self.f.store, executor=self.f.executor(fixtures.FakeGPU())), automatic=True)
        self.adapter = LedgerAdapter(LedgerSpec('tt', 'auto', str(self.f.store.db_path), 'tt_auto_task'))
        self.store.cutover('tt', (self.now - dt.timedelta(days=1)).isoformat())
        self.store.channel_enabled('tt', True)
        items = []
        for index in range(8):
            with patch.object(self.f.store, 'reserve_material') as reservation:
                task = self.f.reserved_task(suffix=str(index))
                selected = dict(reservation.call_args.kwargs)
            with self.bridge.connect() as c:
                c.execute('UPDATE tt_auto_task SET account_id=? WHERE id=?', (str(1000 + index), task.id))
            offer = self.adapter.project(self.adapter.read(task.id)); offer['execution_available'] = True; offer['observation_only'] = False
            self.store.offer(offer)
            self.store.register_node('cpu-main', ['prepare:tt', 'backend:cpu'])
            self.store.worker_heartbeat('cpu-main', 'worker', 'prepare')
            job = self.store.claim('cpu-main', 'worker', 'prepare')['job']
            self.assertIsNotNone(job)
            job['executor_backend'] = 'cpu'
            def selector(token):
                return SimpleNamespace(select_and_reserve=lambda request: self.f.store.reserve_material(**selected, claim_token=token))
            with patch.object(self.bridge.service.executor, '_phase_selector', side_effect=selector):
                descriptor = self.bridge.reserve(job, job)
            self.now += dt.timedelta(seconds=121)
            self.store.expire()
            self.store.register_node('cpu-main', ['prepare:tt', 'backend:cpu'])
            self.store.worker_heartbeat('cpu-main', 'worker', 'prepare')
            second = self.store.claim('cpu-main', 'worker', 'prepare')['job']
            self.assertEqual(second['generation'], 2)
            self.store.fail(second, 'canonical_identity_changed')
            row = self.adapter.read(task.id)
            with self.bridge.connect() as c:
                binding = dict(c.execute('SELECT * FROM pipeline_tt_claim WHERE source_key=?', (job['source_key'],)).fetchone())
            request = descriptor['request']
            items.append({'source_key': job['source_key'], 'job_id': job['id'], 'central_generation': 2,
                'central_error': 'canonical_identity_changed', 'central_fingerprint': offer['canonical_fingerprint'],
                'source_created_at': job['source_created_at'], 'scheduled_at_utc': row['scheduled_at_utc'],
                'account_id': row['account_id'], 'template_id': row['template_id'], 'template_version': row['template_version'],
                'material_id': row['material_id'], 'content_id': row['content_id'], 'gpu_job_id': row['gpu_job_id'],
                'profile': request['expected_profile'], 'trim_tail_seconds': request['source_trim_tail_seconds'],
                'required_capabilities': descriptor['required_capabilities'], 'descriptor_sha256': recovery.sha(descriptor),
                'request_sha256': recovery.sha(request), 'source_url_sha256': recovery.sha(request['source_url']),
                'native_token_sha256': recovery.sha(row['claim_token']), 'binding_claim_hash': binding['claim_hash']})
        self.now += dt.timedelta(hours=4)
        self.store.channel_enabled('tt', False)
        self.scope = {'kind': 'review_only_tt_frozen_prepare_handoff', 'version': 1, 'apply': False,
                      'items': items, 'source_keys': [x['source_key'] for x in items]}
        self.scope['plan_sha256'] = recovery.document_hash(self.scope)
        self.backups = Path(self.f.temp.name) / 'backups'

    def plan(self):
        return recovery.make_plan(self.central, self.f.store.db_path, self.scope, self.scope['plan_sha256'], self.now)

    def evidence(self, plan):
        return {'plan_sha256': plan['plan_sha256'], 'nodes': [dict(node_id=node, unit='pipeline-node-' + node + '.service',
            active_state='inactive', main_pid=0, captured_at=self.now.isoformat(), outbox_matching_count=0,
            task_file_matches=[], work_root_exists=True, outbox_exists=True) for node in plan['node_ids']]}

    def apply(self, plan, evidence=None):
        return recovery.apply_plan(self.central, self.f.store.db_path, plan, plan['plan_sha256'],
            evidence or self.evidence(plan), self.backups, actor='offline-reviewer', clock=lambda: self.now)

    def dump(self, path):
        with contextlib.closing(recovery.read_connection(path)) as c:
            return '\n'.join(c.iterdump())

    def test_exact_apply_preserves_native_failures_claims_appointments_and_requires_new_claim(self):
        native = self.dump(self.f.store.db_path)
        before = self.dump(self.central)
        plan = self.plan()
        self.assertEqual(self.dump(self.central), before)
        original = {x['job_id']: self.store.get_job(x['job_id']) for x in plan['items']}
        result = self.apply(plan)
        self.assertEqual(self.dump(self.f.store.db_path), native)
        self.assertEqual(self.dump(result['backup_path']), before)
        with contextlib.closing(recovery.read_connection(self.central)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='audited_preparation_handoff'").fetchone()[0], 8)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='failed'").fetchone()[0], 8)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_claim_history').fetchone()[0], 16)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_receipt').fetchone()[0], 0)
        for item in plan['items']:
            row = self.store.get_job(item['job_id'])
            self.assertEqual((row['status'], row['generation']), ('queued', 2))
            self.assertEqual(row['capabilities'], item['required_capabilities'])
            for key in ('started_at', 'finished_at', 'scheduled_at', 'error_code', 'payload', 'artifact'):
                self.assertEqual(row[key], original[item['job_id']][key])
        with self.assertRaises(recovery.HandoffRejected):
            self.apply(plan)
        self.store.channel_enabled('tt', True)
        self.store.register_node('cpu-main', plan['items'][0]['required_capabilities'])
        self.store.worker_heartbeat('cpu-main', 'next', 'prepare')
        claim = self.store.claim('cpu-main', 'next', 'prepare')['job']
        self.assertEqual(claim['generation'], 3)
        with patch.object(self.bridge.service.executor, '_prepare', side_effect=AssertionError('must reuse frozen request')):
            descriptor = self.bridge.reserve(claim, claim)
        self.assertEqual(recovery.sha(descriptor['request']), plan['items'][0]['request_sha256'])

    def test_scope_and_plan_hashes_are_not_optional(self):
        scope = copy.deepcopy(self.scope); scope['items'][0]['source_key'] = 'tt:auto:99999'
        with self.assertRaisesRegex(recovery.HandoffRejected, 'scope_hash_mismatch'):
            recovery.make_plan(self.central, self.f.store.db_path, scope, self.scope['plan_sha256'], self.now)
        plan = self.plan(); plan['items'][0]['profile'] = 'changed'
        with self.assertRaisesRegex(recovery.HandoffRejected, 'plan_hash_mismatch'):
            self.apply(plan)
        self.assertFalse(self.backups.exists())

    def test_node_inactive_fresh_scope_and_no_receipts_are_mandatory(self):
        plan = self.plan(); before = self.dump(self.central)
        for changed in ({'active_state': 'active'}, {'main_pid': 42}, {'outbox_matching_count': 1},
                        {'task_file_matches': [{'job_id': plan['items'][0]['job_id'], 'kind': 'manifest'}]},
                        {'captured_at': (self.now - dt.timedelta(seconds=61)).isoformat()}, {'work_root_exists': False}):
            with self.subTest(changed=changed):
                evidence = self.evidence(plan); evidence['nodes'][1].update(changed)
                with self.assertRaises(recovery.HandoffRejected):
                    self.apply(plan, evidence)
                self.assertEqual(self.dump(self.central), before)
        evidence = self.evidence(plan); evidence['plan_sha256'] = 'wrong'
        with self.assertRaises(recovery.HandoffRejected): self.apply(plan, evidence)

    def test_disabled_channels_and_zero_active_jobs_are_required(self):
        self.store.channel_enabled('x', True)
        with self.assertRaisesRegex(recovery.HandoffRejected, 'all_channels_must_be_disabled'): self.plan()
        self.store.channel_enabled('x', False)
        for status in ('running', 'claimed', 'leased'):
            with self.subTest(status=status):
                with self.store.transaction() as c:
                    c.execute('UPDATE pipeline_job SET status=? WHERE id=?', (status, self.scope['items'][-1]['job_id']))
                with self.assertRaisesRegex(recovery.HandoffRejected, 'active_dispatch_work'): self.plan()

    def test_native_identity_attempt_unknown_artifact_and_hold_refuse_all_changes(self):
        task = int(self.scope['items'][-1]['source_key'].split(':')[-1])
        plan = self.plan(); before = self.dump(self.central)
        fields = [('material_id', 'changed'), ('claim_token', 'changed'), ('unknown_outcome', 1),
                  ('preparation_attempt_count', 1), ('publish_attempt_count', 1), ('prepared_media_url', 'https://example.test/ready.mp4'), ('status', 'pending')]
        for field, value in fields:
            with self.subTest(field=field):
                with self.bridge.connect() as c:
                    old = c.execute('SELECT ' + field + ' FROM tt_auto_task WHERE id=?', (task,)).fetchone()[0]
                    c.execute('UPDATE tt_auto_task SET ' + field + '=? WHERE id=?', (value, task))
                with self.assertRaises(recovery.HandoffRejected): self.apply(plan)
                self.assertEqual(self.dump(self.central), before)
                with self.bridge.connect() as c:
                    c.execute('UPDATE tt_auto_task SET ' + field + '=? WHERE id=?', (old, task))
        with self.bridge.connect() as c:
            c.execute('INSERT INTO pipeline_canonical_hold VALUES(?,?,?)', (self.scope['items'][-1]['source_key'], self.now.isoformat(), 'historical'))
        with self.assertRaisesRegex(recovery.HandoffRejected, 'native_held'): self.apply(plan)
        self.assertEqual(self.dump(self.central), before)

    def test_execution_evidence_and_pre_cutover_never_enter_scope(self):
        plan = self.plan(); identity = plan['items'][-1]['job_id']
        with self.store.transaction() as c:
            c.execute("INSERT INTO pipeline_checkpoint VALUES('checkpoint',?,1,'tt_initialized','hash',?)", (identity, self.now.isoformat()))
        with self.assertRaisesRegex(recovery.HandoffRejected, 'dispatch_execution_evidence'): self.apply(plan)
        with self.store.transaction() as c:
            c.execute('DELETE FROM pipeline_checkpoint')
            c.execute("UPDATE pipeline_job SET source_created_at='2000-01-01T00:00:00Z' WHERE id=?", (identity,))
        with self.assertRaisesRegex(recovery.HandoffRejected, 'precutover_source_forbidden'): self.plan()

    def test_second_transaction_recheck_rolls_back_whole_scope_after_backup(self):
        plan = self.plan(); original = recovery.snapshot
        calls = []
        def guarded(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise recovery.HandoffRejected('simulated_last_item_drift')
            return original(*args, **kwargs)
        before = self.dump(self.central)
        with patch.object(recovery, 'snapshot', side_effect=guarded):
            with self.assertRaisesRegex(recovery.HandoffRejected, 'simulated_last_item_drift'): self.apply(plan)
        self.assertEqual(self.dump(self.central), before)
        self.assertEqual(len(list(self.backups.glob('*.sqlite3'))), 1)

    def test_whole_row_drift_after_plan_is_not_silently_overwritten(self):
        plan = self.plan()
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET payload_json='{}' WHERE id=?", (plan['items'][0]['job_id'],))
        before = self.dump(self.central)
        with self.assertRaisesRegex(recovery.HandoffRejected, 'plan_state_drift'):
            self.apply(plan)
        self.assertEqual(self.dump(self.central), before)
        self.assertFalse(self.backups.exists())

    def test_controller_uses_explicit_safe_ssh_and_never_transfers_credentials(self):
        plan = self.plan(); calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            node = plan['node_ids'][len(calls)-1]
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.evidence(plan)['nodes'][len(calls)-1]))
        nodes = [{'node_id': node, 'host': 'host.example.test', 'python': '/trusted/python3.10', 'identity_file': 'existing-key'} for node in plan['node_ids']]
        evidence = inspect_nodes(plan, plan['plan_sha256'], nodes, run)
        self.assertEqual(evidence['plan_sha256'], plan['plan_sha256'])
        for command, kwargs in calls:
            self.assertIn('BatchMode=yes', command); self.assertIn('IdentitiesOnly=yes', command); self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertNotIn('native_token_sha256', kwargs['input'])
            self.assertNotIn('UPDATE ', kwargs['input']); self.assertIn("PRAGMA query_only=ON", kwargs['input'])

    def test_node_probe_uses_actual_private_env_and_reads_acked_receipts_too(self):
        root = Path(self.f.temp.name) / 'node'; root.mkdir()
        work = root / 'tt-prepare'; work.mkdir()
        env = root / 'node.env'
        env.write_text('\n'.join(key + '=' + json.dumps(str(value)) for key, value in {
            'PIPELINE_NODE_ID': 'cpu-main', 'PIPELINE_NODE_STATE_ROOT': root,
            'TT_POST_GPU_WORK_ROOT': work, 'SOME_SECRET': 'never-return-this',
        }.items()))
        with contextlib.closing(sqlite3.connect(root / 'receipts.sqlite3')) as c:
            c.execute('CREATE TABLE receipt_outbox(payload TEXT,acked_at REAL)')
            c.execute('INSERT INTO receipt_outbox VALUES(?,1)', (json.dumps({'job_id': self.scope['items'][0]['job_id']}),))
            c.commit()
        request = {'node_id': 'cpu-main', 'items': self.scope['items']}
        # Linux unit paths cannot contain the temporary Windows parent spaces;
        # patch only the returned path to an explicit POSIX-style file key.
        state = 'ActiveState=inactive\nMainPID=0\nEnvironmentFiles=/node.env (ignore_errors=no)\n'
        original = Path.read_text
        def read(path, *args, **kwargs):
            return original(env, *args, **kwargs) if path.as_posix() == '/node.env' else original(path, *args, **kwargs)
        with patch('scripts.inspect_pipeline_tt_handoff_nodes.subprocess.check_output', return_value=state), \
                patch.object(Path, 'read_text', read):
            result = probe_node(request)
        self.assertEqual(result['outbox_matching_count'], 1)
        self.assertNotIn('never-return-this', json.dumps(result))
        with contextlib.closing(sqlite3.connect(root / 'receipts.sqlite3')) as c:
            c.execute('DELETE FROM receipt_outbox'); c.commit()
        frozen = self.scope['items'][0]
        scratch = work / 'jobs'; scratch.mkdir()
        (scratch / (frozen['gpu_job_id'] + '-other.random')).mkdir()
        with patch('scripts.inspect_pipeline_tt_handoff_nodes.subprocess.check_output', return_value=state), \
                patch.object(Path, 'read_text', read):
            self.assertEqual(probe_node(request)['task_file_matches'], [])
            (scratch / (frozen['gpu_job_id'] + '.randomsuffix')).mkdir()
            evidence = probe_node(request)
        self.assertEqual(evidence['task_file_matches'], [{'job_id': frozen['job_id'], 'kind': 'scratch'}])
        self.assertEqual(evidence['outbox_matching_count'], 0)
        plan = self.plan(); evidence['captured_at'] = self.now.isoformat()
        nodes = self.evidence(plan); nodes['nodes'][0] = evidence
        with self.assertRaisesRegex(recovery.HandoffRejected, 'node_execution_evidence'):
            self.apply(plan, nodes)
        env.write_text('PIPELINE_NODE_ID="cpu-main"\n')
        with patch('scripts.inspect_pipeline_tt_handoff_nodes.subprocess.check_output', return_value=state), \
                patch.object(Path, 'read_text', read), self.assertRaises(KeyError):
            probe_node(request)


if __name__ == '__main__':
    unittest.main()

"""Real SQLite selection/prepare response-loss recovery, without remote I/O."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.adapters import AdapterError, AdapterRegistry, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.tt_native import TTNativeBridge
from scripts import test_tt_auto_post_publisher as fixtures


class TTSelectionRecoveryTests(unittest.TestCase):
    def fixture(self, *, pending=True):
        f = fixtures.TTAutoPostPublisherIntegrationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if pending:
            # Keep the real template/run/task creation; delay the actual atomic
            # reservation until the real publisher's _select phase.
            with patch.object(f.store, 'reserve_material') as reservation:
                task = f.reserved_task(suffix='selection-recovery')
                selected = dict(reservation.call_args.kwargs)
        else:
            task = f.reserved_task(suffix='reserved-recovery')
        gpu = fixtures.FakeGPU()
        executor = f.executor(gpu)
        selected_calls = []
        def selector(token):
            def select(request):
                selected_calls.append(request.task_id)
                return f.store.reserve_material(**selected, claim_token=token)
            return SimpleNamespace(select_and_reserve=select)
        selector_patch = patch.object(executor, '_phase_selector', side_effect=selector)
        selector_patch.start(); self.addCleanup(selector_patch.stop)
        bridge = TTNativeBridge(SimpleNamespace(store=f.store, executor=executor), automatic=True)
        driver = SimpleNamespace(reserve=lambda row, job, claim: bridge.reserve(job, claim))
        adapter = LedgerAdapter(LedgerSpec('tt', 'auto', str(f.store.db_path), 'tt_auto_task', module='tt_posts'), driver)
        registry = AdapterRegistry([adapter])
        offer = adapter.project(adapter.read(task.id))
        job = dict(offer, generation=1, executor_backend='cpu')
        claim = {'claim_token': 'a' * 32, 'node_id': 'node-a'}
        return SimpleNamespace(fixture=f, store=f.store, task=task, gpu=gpu, executor=executor,
            bridge=bridge, registry=registry, adapter=adapter, offer=offer, job=job,
            claim=claim, selected_calls=selected_calls)

    def test_pending_selection_then_lost_response_reuses_exact_request_same_and_new_generation(self):
        f = self.fixture()
        descriptor = f.registry.reserve(f.job, f.claim)
        self.assertEqual(f.selected_calls, [f.task.id])
        self.assertEqual(f.adapter.read(f.task.id)['claim_phase'], 'prepare')
        self.assertNotEqual(f.offer['canonical_fingerprint'], f.adapter.project(f.adapter.read(f.task.id))['canonical_fingerprint'])
        self.assertEqual(f.registry.reserve(f.job, f.claim), descriptor)
        with patch.object(f.executor, '_select', side_effect=AssertionError('must not select again')), \
                patch.object(f.executor, '_prepare', side_effect=AssertionError('must not rebuild frozen descriptor')):
            newer = dict(f.job, generation=2, executor_backend='gpu')
            claim = {'claim_token': 'b' * 32, 'node_id': 'node-b'}
            self.assertEqual(f.registry.reserve(newer, claim), descriptor)
        self.assertEqual(f.gpu.prepare_calls, [])
        self.assertEqual(f.gpu.publish_calls, [])
        with f.bridge.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tt_auto_material_ledger').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_operation').fetchone()[0], 0)
            rows = conn.execute('SELECT native_token FROM pipeline_tt_claim ORDER BY generation').fetchall()
            self.assertNotEqual(rows[0][0], rows[1][0])
        request = descriptor['request']
        receipt = {'preparation': fixtures.FakeGPU().prepare(job_id=request['job_id'],
            material={'content_id': request['content_id']}, expected_profile=request['expected_profile'])}
        with self.assertRaises(Exception):
            f.bridge.complete(f.job, receipt, f.claim)
        f.bridge.complete(newer, receipt, claim)
        self.assertEqual(f.store.get_task(f.task.id).status, 'ready')

    def test_existing_selection_phase_and_already_reserved_origin_are_supported(self):
        for pending in (True, False):
            with self.subTest(pending=pending):
                f = self.fixture(pending=pending)
                descriptor = f.registry.reserve(f.job, f.claim)
                # Exact shape persisted by the old bridge after selection.
                with f.bridge.connect() as conn:
                    conn.execute("UPDATE tt_auto_task SET claim_phase='selection' WHERE id=?", (f.task.id,))
                with patch.object(f.executor, '_select', side_effect=AssertionError('no reselection')):
                    self.assertEqual(f.registry.reserve(dict(f.job, generation=2),
                        {'claim_token': 'c' * 32, 'node_id': 'node-c'}), descriptor)
                self.assertEqual(f.adapter.read(f.task.id)['claim_phase'], 'prepare')

    def test_identity_or_publish_evidence_never_gets_fingerprint_exception(self):
        mutations = {
            'account': "UPDATE tt_auto_task SET account_id='other'",
            'template': 'UPDATE tt_auto_task SET template_version=template_version+1',
            'material': "UPDATE tt_auto_task SET material_id='different'",
            'source': "UPDATE tt_auto_task SET source_media_url='https://media.example.test/other.mp4'",
            'token': "UPDATE tt_auto_task SET claim_token='other'",
            'worker': "UPDATE tt_auto_task SET claim_worker='legacy'",
            'unknown': 'UPDATE tt_auto_task SET unknown_outcome=1',
            'attempt': 'UPDATE tt_auto_task SET publish_attempt_count=1',
            'publish_id': "UPDATE tt_auto_task SET publish_id='published-id'",
            'operation': "INSERT INTO pipeline_tt_operation(operation_id,source_key,stage,generation,operation,request_sha256) SELECT 'op',source_key,'publish',1,'tt_initialize_video','hash' FROM pipeline_tt_claim LIMIT 1",
            'receipt': "UPDATE pipeline_tt_claim SET receipt_json='{}'",
            'binding': 'DELETE FROM pipeline_tt_claim',
        }
        for name, sql in mutations.items():
            with self.subTest(name=name):
                f = self.fixture()
                f.registry.reserve(f.job, f.claim)
                with f.bridge.connect() as conn:
                    conn.execute(sql)
                    before = dict(conn.execute('SELECT * FROM tt_auto_task').fetchone())
                with patch.object(f.executor, '_select', side_effect=AssertionError('no reselection')):
                    with self.assertRaisesRegex(AdapterError, 'canonical_identity_changed'):
                        f.registry.reserve(dict(f.job, generation=2), {'claim_token': 'c' * 32, 'node_id': 'new'})
                with f.bridge.connect() as conn:
                    self.assertEqual(dict(conn.execute('SELECT * FROM tt_auto_task').fetchone()), before)
                self.assertEqual(f.gpu.prepare_calls + f.gpu.publish_calls, [])

    def test_same_generation_requires_same_claim_and_future_binding_is_rejected(self):
        f = self.fixture()
        f.registry.reserve(f.job, f.claim)
        for generation, token in ((1, 'different'), (0, f.claim['claim_token'])):
            with self.subTest(generation=generation):
                with self.assertRaisesRegex(AdapterError, 'canonical_identity_changed'):
                    f.registry.reserve(dict(f.job, generation=generation), dict(f.claim, claim_token=token))

    def test_pending_same_account_fence_is_deferred_without_consuming_native(self):
        for status in ('preparing', 'publishing', 'reconciling', 'unknown'):
            with self.subTest(status=status):
                f = self.fixture()
                f.registry.reserve(f.job, f.claim)
                with patch.object(f.store, 'reserve_material'):
                    pending = f.fixture.reserved_task(suffix='waiting')
                job = dict(f.job, source_id=str(pending.id), source_key='tt:auto:' + str(pending.id))
                with f.bridge.connect() as conn:
                    conn.execute('UPDATE tt_auto_task SET status=? WHERE id=?', (status, f.task.id))
                    before = dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (pending.id,)).fetchone())
                with self.assertRaisesRegex(AdapterError, '^canonical_account_busy$'):
                    f.bridge.reserve(job, f.claim)
                with f.bridge.connect() as conn:
                    self.assertEqual(dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (pending.id,)).fetchone()), before)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_claim WHERE source_key=?', (job['source_key'],)).fetchone()[0], 0)
                self.assertEqual(f.gpu.prepare_calls + f.gpu.publish_calls, [])

    def test_no_busy_classification_without_current_unheld_account_fence(self):
        f = self.fixture()
        with patch.object(f.store, 'claim_next_executable_task', return_value=None):
            with self.assertRaisesRegex(AdapterError, '^canonical_task_not_claimable$'):
                f.bridge.reserve(f.job, f.claim)
        f.registry.reserve(f.job, f.claim)
        with patch.object(f.store, 'reserve_material'):
            pending = f.fixture.reserved_task(suffix='waiting')
        job = dict(f.job, source_id=str(pending.id), source_key='tt:auto:' + str(pending.id))
        with f.bridge.connect() as conn:
            conn.execute("INSERT INTO pipeline_canonical_hold(source_key,cutoff,reason) VALUES(?,?,?)", (f.job['source_key'], '2026-08-05T10:00:00Z', 'historical'))
        with patch.object(f.store, 'claim_next_executable_task', return_value=None):
            with self.assertRaisesRegex(AdapterError, '^canonical_task_not_claimable$'):
                f.bridge.reserve(job, f.claim)
        with f.bridge.connect() as conn:
            conn.execute('DELETE FROM pipeline_canonical_hold')
            conn.execute("UPDATE tt_auto_task SET scheduled_at_utc='2000-01-01T00:00:00+00:00',claim_token='',lease_expires_at_utc='' WHERE id=?", (f.task.id,))
        f.store.execution_not_before_utc='2026-08-01T00:00:00+00:00'
        with patch.object(f.store, 'claim_next_executable_task', return_value=None):
            with self.assertRaisesRegex(AdapterError, '^canonical_task_not_claimable$'):
                f.bridge.reserve(job, f.claim)


if __name__ == '__main__':
    unittest.main()

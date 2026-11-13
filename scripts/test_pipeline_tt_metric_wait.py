"""TT transient metric absence waits across midnight without platform writes."""
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp
from features.pipeline_dispatch.service import Coordinator
from features.tt_auto_posts.repositories import MetricWindowNotReady
from scripts import test_pipeline_tt_selection_recovery as selection_fixtures


class MetricWaitTests(unittest.TestCase):
    def native_fixture(self):
        # Rendering assets are supplied by a fake GPU; this test exercises the
        # real native claims/retries and SQLite state before media execution.
        assets = patch('features.pipeline_dispatch.tt_native.outro_capabilities', return_value=[])
        assets.start()
        self.addCleanup(assets.stop)
        return selection_fixtures.TTSelectionRecoveryTests.fixture(self)

    def test_missing_metrics_twice_then_ready_preserves_identity_and_appointment(self):
        f = self.native_fixture()
        original = f.adapter.read(f.task.id)
        for generation in (1, 2):
            job = dict(f.job, generation=generation)
            with patch.object(f.executor, '_select', side_effect=MetricWindowNotReady(['2026-08-04'])):
                with self.assertRaises(MetricWindowNotReady):
                    f.bridge.reserve(job, f.claim)
            task = f.store.get_task(f.task.id)
            row = f.adapter.read(task.id)
            self.assertEqual(task.status, 'retry_wait')
            self.assertEqual(row['claim_phase'], 'selection')
            self.assertEqual(row['publish_attempt_count'], 0)
            self.assertEqual(row['preparation_attempt_count'], 0)
            self.assertEqual(row['scheduled_at_utc'], original['scheduled_at_utc'])
            self.assertFalse(task.publish_id or task.unknown_outcome)
            f.fixture.clock.value += dt.timedelta(minutes=5, seconds=1)
        f.bridge.reserve(dict(f.job, generation=3), f.claim)
        self.assertEqual(f.selected_calls, [f.task.id])
        with f.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tt_auto_run').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM tt_auto_task').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_tt_operation').fetchone()[0], 0)
        self.assertEqual(f.gpu.publish_calls, [])

    def coordinator_fixture(self, mutation=None):
        f = self.native_fixture()
        store = DispatchStore(Path(f.fixture.temp.name) / 'central.db', f.fixture.clock)
        store.channel_enabled('tt', True)
        store.register_node('cpu-main', ['prepare:tt', 'backend:cpu'])
        store.worker_heartbeat('cpu-main', 'one', 'prepare')
        store.offer(dict(f.offer, capabilities=['prepare:tt'], execution_available=True, observation_only=False))
        job = store.claim('cpu-main', 'one', 'prepare')['job']
        observation = {'canonical_status': 'retry_wait',
                       'canonical_receipt': {'source_key': job['source_key'], 'publish_id': ''},
                       'next_offer': {'source_attempt_count': 0, 'unknown_outcome': False}}
        if mutation:
            mutation(observation, store, job)
        registry = SimpleNamespace(
            reserve=lambda *a: (_ for _ in ()).throw(AdapterError('tt_auto_metric_window_not_ready')),
            reconcile=lambda *a: observation)
        coordinator = Coordinator(store, registry, internal_token='i' * 32, master_key='m' * 32)
        return f, store, job, coordinator

    def test_coordinator_cooldown_preserves_time_and_reclaims_after_five_minutes(self):
        f, store, job, coordinator = self.coordinator_fixture()
        due = job['scheduled_at']
        self.assertIsNone(coordinator.reserve_job(job)['job'])
        self.assertEqual(store.get_job(job['id'])['status'], 'queued')
        self.assertEqual(store.get_job(job['id'])['scheduled_at'], due)
        self.assertIsNone(store.claim('cpu-main', 'one', 'prepare')['job'])
        f.fixture.clock.value += dt.timedelta(minutes=5, seconds=1)
        store.register_node('cpu-main', ['prepare:tt', 'backend:cpu'])
        store.worker_heartbeat('cpu-main', 'one', 'prepare')
        self.assertEqual(store.claim('cpu-main', 'one', 'prepare')['job']['generation'], 2)

    def test_unknown_ids_attempts_and_operation_never_enter_wait_branch(self):
        def operation(o, store, job):
            with store.transaction() as c:
                c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          ('op', job['id'], 1, 'cpu-main', 'tt_initialize_video', 'a'*64, 'tt:account:640', store.now(), store.now()))
        changes = [lambda o,s,j: o.update(canonical_status='unknown'),
                   lambda o,s,j: o['canonical_receipt'].update(publish_id='pub-id'),
                   lambda o,s,j: o['next_offer'].update(source_attempt_count=1),
                   lambda o,s,j: o['next_offer'].update(unknown_outcome=True), operation]
        for mutation in changes:
            with self.subTest(mutation=mutation):
                f, store, job, coordinator = self.coordinator_fixture(mutation)
                with self.assertRaises(DispatchError):
                    coordinator.reserve_job(job)
                self.assertNotEqual(store.get_job(job['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()

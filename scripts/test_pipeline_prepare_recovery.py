"""Real native ledgers: generation takeover preserves frozen media identity."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_video_publish.core import Store as FBVideoStore
from features.pipeline_dispatch.fb_native import FBNativeBridge, CPU_PROFILE
from features.pipeline_dispatch.adapters import AdapterError
from scripts import test_pipeline_tt_bridge as tt_fixtures


class FBPrepareRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.path = Path(self.temp.name) / 'native.sqlite'
        self.auto = FBAutoPostStore(str(self.path), now_fn=lambda: self.now)
        self.video = FBVideoStore(str(self.path), now_fn=lambda: self.now)
        instant = self.now.isoformat()
        with self.auto.connect() as conn:
            conn.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'t','enabled',?,?)", (instant, instant))
            conn.execute("INSERT INTO fb_auto_template_version(template_id,version,config_json,config_sha256,created_at_utc) VALUES(1,1,'{}',?,?)", ('a' * 64, instant))
            conn.execute("INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,1,'manual-slot','manual','queued','{}',?)", (instant,))
            conn.execute("""INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,created_at_utc,
                source_media_url,planned_publish_at_utc,gpu_job_id) VALUES(1,1,1,1,'page','group','planned','material','content',?,
                'https://cdn.example/source.mp4',?,?)""", (instant, instant, 'g' * 32))
            conn.execute("INSERT INTO fb_video_batch VALUES('batch','tenant','owner','Owner','op','hash','{}',?)", (instant,))
            snapshot = json.dumps({'material': {'media_url': 'https://cdn.example/manual.mp4'}})
            conn.execute("""INSERT INTO fb_video_task(task_id,batch_id,tenant_key,owner_user_id,page_id,material_id,content_id,
                sequence,page_sequence,planned_at_utc,planned_bj_date,processing_mode,status,snapshot_json,prepare_state,link_state,
                gpu_job_id,created_at_utc,updated_at_utc) VALUES('manual','batch','tenant','owner','page','material2','content2',
                1,1,?,'2026-11-02','random_overlay','queued',?,'planned','ready',?,?,?)""", (instant, snapshot, 'm' * 32, instant, instant))
        self.runtime = SimpleNamespace(store=self.auto, executor=SimpleNamespace(live_enabled=True),
            manual_worker=SimpleNamespace(store=self.video))
        self.bridge = FBNativeBridge(self.runtime)

    def reserve(self, family, generation=1, backend='cpu'):
        identity = '1' if family == 'auto' else 'manual'
        job = {'source_key': 'fb:%s:%s' % (family, identity), 'channel': 'fb', 'family': family,
               'source_id': identity, 'stage': 'prepare', 'generation': generation, 'executor_backend': backend,
               'executor_capabilities': ['catalog:fb:' + ('a' if generation == 1 else 'b') * 64]}
        claim = {'claim_token': str(generation) * 32, 'node_id': backend + '-node'}
        return job, claim, self.bridge.reserve(job, claim)

    def test_auto_and_manual_reclaim_same_descriptor_without_waiting_native_lease(self):
        for family in ('auto', 'video'):
            with self.subTest(family=family):
                old, claim, descriptor = self.reserve(family)
                recovered, newer, retried = self.reserve(family, 2, 'gpu')
                self.assertEqual(retried, descriptor)
                self.assertIn('backend:cpu', retried['required_capabilities'])
                self.assertIn('catalog:fb:' + 'a' * 64, retried['required_capabilities'])
                self.assertEqual(retried['request']['expected_profile'], CPU_PROFILE)
                old_row, old_task, _ = self.bridge._stored(old, claim)
                new_row, new_task, _ = self.bridge._stored(recovered, newer)
                self.assertNotEqual(old_task.get('claim_token') or old_task['lease_owner'], new_task.get('claim_token') or new_task['lease_owner'])
                result = {'preparation': {'job_id': descriptor['request']['job_id'], 'content_id': descriptor['request']['content_id'],
                    'profile': CPU_PROFILE, 'output_url': 'https://cdn.example/final.mp4', 'output_sha256': 'c' * 64,
                    'output_size': 1234, 'probe': {'duration': 30}, 'random_overlay_recipe': {'seed': 1}}}
                with self.assertRaisesRegex(AdapterError, 'canonical_claim_stale'):
                    self.bridge.complete(old, result, claim)
                self.bridge.complete(recovered, result, newer)
                self.assertEqual(self.bridge.reconcile(recovered)['status'], 'ready' if family == 'auto' else 'queued')

    def test_reclaim_does_not_clear_a_changed_lease_or_mutated_material(self):
        job, _, _ = self.reserve('auto')
        with self.auto.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET lease_owner='other-worker' WHERE id=1")
            before = dict(conn.execute('SELECT * FROM fb_auto_task WHERE id=1').fetchone())
        with self.assertRaisesRegex(AdapterError, 'canonical_prepare_reclaim_conflict'):
            self.reserve('auto', 2)
        with self.auto.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM fb_auto_task WHERE id=1').fetchone()), before)
        self.reserve('video')
        with self.auto.connect() as conn:
            conn.execute("UPDATE fb_video_task SET material_id='different' WHERE task_id='manual'")
            before = dict(conn.execute("SELECT * FROM fb_video_task WHERE task_id='manual'").fetchone())
        with self.assertRaisesRegex(AdapterError, 'canonical_prepare_identity_changed'):
            self.reserve('video', 2)
        with self.auto.connect() as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM fb_video_task WHERE task_id='manual'").fetchone()), before)

    def test_orphan_native_reclaim_without_new_binding_is_isolated(self):
        self.reserve('auto')
        self.reserve('auto', 2)
        # Reproduce a process exit after the native lease commit and before
        # the generation-2 bridge binding was durably inserted.
        with self.auto.connect() as conn:
            conn.execute("DELETE FROM pipeline_fb_claim WHERE source_key='fb:auto:1' AND generation=2")
            before = dict(conn.execute('SELECT * FROM fb_auto_task WHERE id=1').fetchone())
        with self.assertRaisesRegex(AdapterError, 'canonical_prepare_reclaim_conflict'):
            self.reserve('auto', 3, 'gpu')
        with self.auto.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM fb_auto_task WHERE id=1').fetchone()), before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_fb_operation').fetchone()[0], 0)
        self.assertEqual(before['status'], 'preparing')
        self.assertEqual(before['gpu_job_id'], 'g' * 32)


class TTPrepareRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tt_fixtures.TTBridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_automatic_reclaims_exact_native_lease_with_frozen_profile_and_trim(self):
        fixture = self.fixture
        descriptor = fixture.bridge.reserve(fixture.job, fixture.claim)
        with fixture.bridge.connect() as conn:
            original_token = conn.execute('SELECT claim_token FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone()[0]
        next_job = dict(fixture.job, generation=2, executor_backend='gpu')
        next_claim = {'claim_token': 'b' * 32, 'node_id': 'gpu-node'}
        fixture.executor.source_trim_tail_seconds = 9
        with patch.object(fixture.executor, '_prepare', side_effect=AssertionError('do not recompute request')):
            repeated = fixture.bridge.reserve(next_job, next_claim)
        self.assertEqual(repeated, descriptor)
        self.assertIn('backend:cpu', repeated['required_capabilities'])
        with fixture.bridge.connect() as conn:
            current_token = conn.execute('SELECT claim_token FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone()[0]
        self.assertNotEqual(original_token, current_token)
        request = descriptor['request']
        result = fixture.gpu.prepare(job_id=request['job_id'], material={'content_id': request['content_id']},
            source_trim_tail_seconds=request['source_trim_tail_seconds'], expected_profile=request['expected_profile'])
        with self.assertRaises(Exception):
            fixture.bridge.complete(fixture.job, {'preparation': result}, fixture.claim)
        fixture.bridge.complete(next_job, {'preparation': result}, next_claim)
        self.assertEqual(fixture.fixture.store.get_task(fixture.task.id).status, 'ready')

    def test_changed_native_token_is_never_cleared(self):
        fixture = self.fixture
        fixture.bridge.reserve(fixture.job, fixture.claim)
        with fixture.bridge.connect() as conn:
            conn.execute("UPDATE tt_auto_task SET claim_token='another-token',claim_worker='legacy' WHERE id=?", (fixture.task.id,))
            before = dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone())
        with self.assertRaisesRegex(AdapterError, 'canonical_prepare_reclaim_conflict'):
            fixture.bridge.reserve(dict(fixture.job, generation=2), {'claim_token': 'd' * 32, 'node_id': 'other'})
        with fixture.bridge.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone()), before)

    def test_orphan_first_native_claim_is_not_reexecuted_or_republished(self):
        fixture = self.fixture
        fixture.bridge.reserve(fixture.job, fixture.claim)
        with fixture.bridge.connect() as conn:
            conn.execute('DELETE FROM pipeline_tt_claim WHERE source_key=?', (fixture.job['source_key'],))
            before = dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone())
        with self.assertRaisesRegex(AdapterError, 'canonical_task_not_claimable'):
            fixture.bridge.reserve(dict(fixture.job, generation=2, executor_backend='gpu'),
                                   {'claim_token': 'q' * 32, 'node_id': 'new-node'})
        with fixture.bridge.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM tt_auto_task WHERE id=?', (fixture.task.id,)).fetchone()), before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_operation').fetchone()[0], 0)
        self.assertEqual(fixture.gpu.prepare_calls, [])
        self.assertEqual(fixture.gpu.publish_calls, [])


class TTManualPrepareRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tt_fixtures.PoolBridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_intake_and_direct_test_reclaim_only_their_own_token(self):
        fixture = self.fixture
        intake = fixture.service.material_pool_add(fixture.fixture.recurring_material_payload())['item']
        jobs = [{'source_key': 'tt:intake:%s' % intake['id'], 'source_id': str(intake['id']),
                 'channel': 'tt', 'family': 'intake', 'stage': 'prepare', 'generation': 1}]
        jobs.append(fixture.direct_task()[1])
        for job in jobs:
            with self.subTest(family=job['family']):
                descriptor = fixture.bridge.reserve(job, fixture.claim)
                next_job = dict(job, generation=2, executor_backend='cpu')
                new_claim = {'claim_token': 'r' * 32, 'node_id': 'new-node'}
                with patch.object(fixture.service, '_prepare_resolved', side_effect=AssertionError('frozen request only')):
                    recovered = fixture.bridge.reserve(next_job, new_claim)
                self.assertEqual(descriptor, recovered)
                request = descriptor['request']
                result = fixture.fixture.gpu.prepare(job_id=request['job_id'],
                    material={'content_id': request['content_id'], 'source_media_url': request['source_url']},
                    source_trim_tail_seconds=request['source_trim_tail_seconds'], expected_profile=request['expected_profile'])
                with self.assertRaises(Exception):
                    fixture.bridge.complete(job, {'preparation': result}, fixture.claim)
                fixture.bridge.complete(next_job, {'preparation': result}, new_claim)
                self.assertEqual(fixture.bridge.reconcile(next_job)['status'], 'ready')


if __name__ == '__main__':
    unittest.main()

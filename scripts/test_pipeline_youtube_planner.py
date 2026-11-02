"""Planner mode retains canonical enqueue rules without local heavy execution."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from features.drama_synthesis.core import DramaSynthesisStore
from features.youtube_auto_publish.service import YouTubeWorkflow


class YouTubePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = DramaSynthesisStore(root / 'jobs.sqlite3')
        self.store.ensure_storage()
        self.generator = Mock(side_effect=AssertionError('planner cannot generate'))
        self.workflow = YouTubeWorkflow(self.store.db_path, root / 'covers', None, None, None,
                                        self.store, generate=self.generator)
        self.actor = {'tenant_key': 'tenant', 'user_id': 'user', 'name': 'Tester'}
        image = io.BytesIO()
        Image.new('RGB', (640, 360)).save(image, format='JPEG')
        self.asset = self.workflow._store_image(self.actor, image.getvalue())
        self.env = patch.dict(os.environ, {'PIPELINE_YOUTUBE_DISPATCH_ENABLED': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def insert(self, identity, state='enqueue_pending', *, held=False, lease_token='', publish_at=''):
        body = {'id': identity, 'status': state, 'current_version': 1,
                'creator': self.actor, 'versions': [{'number': 1, 'approved': True, 'asset_id': self.asset['id']}],
                'material': {'id': 'material-' + identity, 'content_id': 'content-' + identity,
                             'url': 'https://media.example/video.mp4'},
                'channel': {'id': '1', 'channel_id': 'UC' + 'A' * 22, 'youtube_account_id': '1',
                            'scopes': ['https://www.googleapis.com/auth/youtube.force-ssl']},
                'title': 'Title', 'description_template': 'description', 'description': 'description',
                'comment': 'First comment', 'publish_at': publish_at}
        with self.workflow.db(True) as conn:
            conn.execute('INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at,lease_token) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (identity, 'tenant', 'user', identity, 'sha', state, 1, json.dumps(body),
                          '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', lease_token))
            if held:
                conn.execute('INSERT INTO pipeline_canonical_hold VALUES(?,?,?)',
                             ('youtube:preparation:' + identity, '2026-11-02T00:00:00Z', 'backlog'))
        return body

    def row(self, identity):
        with self.workflow.db() as conn:
            return dict(conn.execute('SELECT * FROM youtube_auto_preparation WHERE id=?', (identity,)).fetchone())

    def test_approved_cover_enqueues_one_native_publication_without_generation(self):
        identity = 'a' * 32
        self.insert(identity)
        result = self.workflow.run_once('planner')
        self.assertEqual(result, {'claimed': True, 'task_id': identity})
        body = json.loads(self.row(identity)['body'])
        self.assertEqual(body['status'], 'uploading')
        task = self.store.youtube_task(body['publish_id'])
        self.assertEqual((task['workflow'], task['status'], task['video_attempt_count']),
                         ('reviewed_thumbnail', 'queued', 0))
        self.assertEqual(task['comment_text'], 'First comment')
        # Recover the original pre-ledger acknowledgement gap; reuse its exact
        # canonical task instead of creating another publication.
        with self.workflow.db(True) as conn:
            body.pop('publish_id')
            body['status'] = 'enqueue_pending'
            conn.execute('UPDATE youtube_auto_preparation SET state=?,body=? WHERE id=?',
                         ('enqueue_pending', json.dumps(body), identity))
        self.workflow.run_once('planner')
        self.assertEqual(json.loads(self.row(identity)['body'])['publish_id'], task['id'])
        with self.workflow.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0], 1)
        self.generator.assert_not_called()

    def test_generation_review_and_expired_generation_are_untouched(self):
        identities = []
        for index, state in enumerate(('queued_generation', 'review', 'generating')):
            identity = str(index) * 32
            self.insert(identity, state, publish_at='2020-01-01T00:00:00Z')
            identities.append(identity)
        before = [self.row(identity) for identity in identities]
        self.assertFalse(self.workflow.run_once('planner')['claimed'])
        self.assertEqual([self.row(identity) for identity in identities], before)
        self.generator.assert_not_called()

    def test_held_and_pipeline_owned_enqueue_are_not_claimed(self):
        self.insert('b' * 32, held=True)
        self.insert('c' * 32, lease_token='pipeline:existing-claim')
        before = [self.row(identity * 32) for identity in ('b', 'c')]
        self.assertFalse(self.workflow.run_once('planner')['claimed'])
        self.assertEqual([self.row(identity * 32) for identity in ('b', 'c')], before)
        self.generator.assert_not_called()

    def test_non_dispatch_legacy_claim_is_preserved(self):
        self.insert('d' * 32, 'queued_generation')
        with patch.dict(os.environ, {'PIPELINE_YOUTUBE_DISPATCH_ENABLED': '0'}):
            body, _, generation = self.workflow._claim()
        self.assertTrue(generation)
        self.assertEqual(body['status'], 'generating')

    def test_cutover_holds_old_enqueue_before_planner_and_preserves_future_work(self):
        from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
        from features.pipeline_dispatch.core import DispatchStore
        from features.pipeline_dispatch.cutover import Cutover
        from features.pipeline_dispatch.service import Coordinator
        from features.pipeline_dispatch.youtube_native import YouTubeNativeBridge
        current = datetime.now(timezone.utc)
        cutoff = current.isoformat(timespec='seconds').replace('+00:00', 'Z')
        future = (current + timedelta(days=1)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        old = [('1' * 32, 'queued_generation', '2020-01-01T00:00:00Z'),
               ('2' * 32, 'review', '2020-01-01T00:00:00Z'),
               ('3' * 32, 'enqueue_pending', '2020-01-01T00:00:00Z'),
               ('4' * 32, 'enqueue_pending', '')]
        future_rows = [('a' * 32, 'enqueue_pending'), ('b' * 32, 'review'),
                       ('c' * 32, 'queued_generation')]
        for identity, state, scheduled in old:
            self.insert(identity, state, publish_at=scheduled)
        for identity, state in future_rows:
            self.insert(identity, state, publish_at=future)
        with self.workflow.db(True) as conn:
            conn.execute("UPDATE youtube_auto_preparation SET created_at='2020-01-01T00:00:00Z'")
        preserved = {identity: self.row(identity) for identity, _, _ in old}
        for identity, _ in future_rows[1:]:
            preserved[identity] = self.row(identity)
        bridge = YouTubeNativeBridge(self.workflow)
        adapter = LedgerAdapter(LedgerSpec('youtube', 'preparation', str(self.store.db_path),
            'youtube_auto_preparation', state='state', module='youtube_auto_publish'), bridge)
        dispatch = DispatchStore(Path(self.temp.name) / 'dispatch.sqlite')
        control = Cutover(Coordinator(dispatch, AdapterRegistry([adapter]),
            internal_token='i' * 32, master_key='m' * 32))
        offers = {offer['source_id']: offer for offer in control.snapshot('youtube')}
        self.assertTrue(offers['3' * 32]['observation_only'])
        self.assertTrue(offers['3' * 32]['pause_eligible'])
        self.assertFalse(offers['a' * 32]['execution_available'])
        plan = control.plan('youtube', cutoff)
        expected = {'youtube:preparation:' + identity for identity, _, _ in old}
        self.assertEqual(set(plan['paused_source_keys']), expected)
        result = control.apply(plan['plan_id'], plan['sha256'])
        self.assertEqual((result['status'], result['paused_count'], result['errors']), ('held', 4, []))
        self.assertTrue(control.enable(plan['plan_id'], plan['sha256'])['enabled'])
        self.assertEqual(self.workflow.run_once('planner')['task_id'], 'a' * 32)
        self.assertFalse(self.workflow.run_once('planner')['claimed'])
        for identity, before in preserved.items():
            self.assertEqual(self.row(identity), before)
        with self.workflow.db() as conn:
            self.assertEqual({row[0] for row in conn.execute('SELECT source_key FROM pipeline_canonical_hold')}, expected)
            publications = conn.execute('SELECT preparation_id,status,video_attempt_count,publish_at FROM drama_youtube_publish').fetchall()
        self.assertEqual([tuple(row) for row in publications], [('a' * 32, 'queued', 0, future)])
        self.generator.assert_not_called()


class YouTubePlannerEntrypointTests(unittest.TestCase):
    def test_dispatch_mode_never_constructs_or_runs_publisher(self):
        for dispatch in ('0', '1'):
            with self.subTest(dispatch=dispatch):
                app = SimpleNamespace(DRAMA_SYNTHESIS_STORE=SimpleNamespace(ensure_storage=Mock()))
                spec = importlib.util.spec_from_file_location('pipeline_youtube_planner_entry',
                    Path(__file__).with_name('youtube_auto_publish_worker.py'))
                module = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, {'app': app}):
                    spec.loader.exec_module(module)
                workflow = SimpleNamespace(run_once=Mock(return_value={'claimed': False}))
                engine = SimpleNamespace(run_once=Mock(return_value={'claimed': False}))
                lane = SimpleNamespace(workers=1, start=Mock(), close=Mock())
                failures = SimpleNamespace(run_once=Mock(return_value={'claimed': False}))
                with patch.dict(os.environ, {'PIPELINE_YOUTUBE_DISPATCH_ENABLED': dispatch,
                        'YOUTUBE_AUTO_ENABLED': '1', 'YOUTUBE_LIVE_ENABLED': '1'}), \
                     patch.object(module, 'build_service', return_value=workflow), \
                     patch.object(module, 'build_engine', return_value=engine) as factory, \
                     patch.object(module, 'build_failure_notifications', return_value=failures), \
                     patch.object(module, 'PreparationLane', return_value=lane), \
                     patch.object(module.signal, 'signal'), \
                     patch.object(module.time, 'sleep', side_effect=lambda _: module.stop(None, None)):
                    self.assertEqual(module.main(), 0)
                lane.start.assert_called_once()
                lane.close.assert_called_once()
                if dispatch == '1':
                    factory.assert_not_called()
                    engine.run_once.assert_not_called()
                else:
                    factory.assert_called_once()
                    engine.run_once.assert_called_once()


if __name__ == '__main__':
    unittest.main()

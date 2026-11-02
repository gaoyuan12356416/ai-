"""Native cover receipt, retry identity and coordinator lost-ACK integration."""
import datetime as dt
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from features.drama_synthesis.core import DramaSynthesisStore
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.pipeline_dispatch.adapters import AdapterError, AdapterRegistry, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.service import Coordinator
from features.pipeline_dispatch.youtube_cover import inline_image
from features.pipeline_dispatch.youtube_native import YouTubeNativeBridge


class YouTubePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DramaSynthesisStore(self.root / 'native.sqlite3'); self.store.ensure_storage()
        self.workflow = YouTubeWorkflow(self.store.db_path, self.root / 'covers', None, None, None, self.store)
        self.actor = {'tenant_key': 'tenant', 'user_id': 'user', 'name': 'Tester'}
        stream = io.BytesIO(); Image.new('RGB', (640, 360), 'blue').save(stream, format='JPEG')
        self.data = stream.getvalue(); self.asset = self.workflow._store_image(self.actor, self.data)
        self.identity = 'a' * 32
        body = {'id': self.identity, 'status': 'queued_generation', 'current_version': 1,
                'creator': self.actor, 'versions': [{'number': 1, 'approved': False, 'asset_id': ''}],
                'material': {'id': 'material', 'url': 'https://media.example/video.mp4'},
                'channel': {'channel_id': 'UC' + 'A' * 22}, 'publish_at': '', 'reference_cover': None}
        with self.workflow.db(True) as conn:
            conn.execute('INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (self.identity, 'tenant', 'user', 'op', 'sha', 'queued_generation', 1, json.dumps(body), '2026-11-02T00:00:00Z', '2026-11-02T00:00:00Z'))
        reference = {'asset_id': self.asset['id'], 'sha256': hashlib.sha256(self.data).hexdigest()}
        self.workflow._prepare_reference = Mock(return_value=(reference, body['material']))
        self.env = patch.dict('os.environ', {'YOUTUBE_AUTO_ENABLED': '1', 'YOUTUBE_LIVE_ENABLED': '1'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.bridge = YouTubeNativeBridge(self.workflow)
        self.job = {'id': 1, 'generation': 1, 'source_key': 'youtube:preparation:' + self.identity,
                    'source_id': self.identity, 'channel': 'youtube', 'family': 'preparation', 'stage': 'prepare'}
        self.claim = {'claim_token': 'first-claim-token-that-is-long'}
        self.artifact = {'url': 'https://cos.example/cover.jpg', 'sha256': hashlib.sha256(self.data).hexdigest(),
                         'size': len(self.data), 'profile': 'youtube-cover-v1', 'mime': 'image/jpeg'}
        self.receipt = {'cover': inline_image(self.data), 'artifact': self.artifact, 'version': 1,
                        'reference_sha256': reference['sha256']}

    def test_new_generation_preserves_reference_and_fences_old_cover_completion(self):
        frozen = self.bridge.reserve(self.job, self.claim)
        successor, token = dict(self.job, generation=2), {'claim_token': 'second-claim-token-that-is-long'}
        recovered = self.bridge.reserve(successor, token)
        self.assertEqual(recovered['request'], frozen['request'])
        self.assertTrue(recovered['recovery_only'])
        self.assertNotIn('recovery_only', recovered['request'])
        self.workflow._prepare_reference.assert_called_once()
        with self.assertRaisesRegex(AdapterError, 'youtube_preparation_version_changed'):
            self.bridge.complete(self.job, self.receipt, self.claim)
        self.bridge.complete(successor, self.receipt, token)
        result = self.bridge.reconcile(successor)
        self.assertEqual((result['status'], result['artifact']), ('review', self.artifact))
        self.assertTrue(result['preparation_completed'])

    def test_explicit_rejected_version_gets_new_generation_and_still_requires_review(self):
        self.bridge.reserve(self.job, self.claim)
        self.bridge.complete(self.job, self.receipt, self.claim)
        self.workflow.review(self.actor, self.identity, {'action': 'reject', 'version': 1, 'feedback': 'Change the title layout'})
        successor = dict(self.job, generation=2)
        token = {'claim_token': 'version-two-token-that-is-long'}
        descriptor = self.bridge.reserve(successor, token)
        self.assertEqual(descriptor['request']['task']['current_version'], 2)
        self.assertFalse(descriptor.get('recovery_only'))
        self.bridge.complete(successor, {**self.receipt, 'version': 2}, token)
        with self.workflow.db() as conn:
            row, body = self.workflow._row(conn, self.identity)
            self.assertEqual((row['state'], row['version']), ('review', 2))
            self.assertFalse(body['versions'][-1]['approved'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0], 0)

    def test_explicit_failed_generation_retry_records_new_intent_without_changing_version(self):
        original = self.bridge.reserve(self.job, self.claim)
        self.bridge.fail(self.job, self.claim, {'error_code': 'youtube_generation_interrupted'})
        self.workflow.retry(self.actor, self.identity)
        successor = dict(self.job, generation=2)
        token = {'claim_token': 'explicit-retry-token-that-is-long'}
        descriptor = self.bridge.reserve(successor, token)
        task = descriptor['request']['task']
        self.assertEqual(task['current_version'], 1)
        self.assertRegex(task['generation_retry_id'], r'^[a-f0-9]{32}$')
        self.assertNotEqual(task.get('generation_retry_id'), original['request']['task'].get('generation_retry_id'))
        self.assertFalse(descriptor.get('recovery_only'))
        self.bridge.complete(successor, self.receipt, token)
        self.assertEqual(self.bridge.reconcile(successor)['status'], 'review')

    def test_same_version_without_explicit_retry_cannot_reopen_completed_cover(self):
        self.bridge.reserve(self.job, self.claim)
        self.bridge.complete(self.job, self.receipt, self.claim)
        with self.workflow.db(True) as conn:
            _, body = self.workflow._row(conn, self.identity)
            body['status'] = 'queued_generation'
            conn.execute("UPDATE youtube_auto_preparation SET state='queued_generation',body=? WHERE id=?", (json.dumps(body), self.identity))
        with self.assertRaisesRegex(AdapterError, 'youtube_preparation_version_changed'):
            self.bridge.reserve(dict(self.job, generation=2), {'claim_token': 'unapproved-retry-token-that-is-long'})

    def test_cover_and_shared_artifact_must_have_identical_bytes(self):
        self.bridge.reserve(self.job, self.claim)
        with self.assertRaisesRegex(AdapterError, 'youtube_cover_artifact_invalid'):
            self.bridge.complete(self.job, {**self.receipt, 'artifact': {**self.artifact, 'sha256': 'b' * 64}}, self.claim)
        self.assertEqual(self.bridge.reconcile(self.job)['status'], 'generating')

    def test_native_committed_cover_is_recovered_after_central_ack_loss(self):
        clock = [dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone.utc)]
        dispatch = DispatchStore(self.root / 'dispatch.sqlite3', lambda: clock[0])
        bridge = self.bridge
        class Driver:
            def reserve(self, row, job, claim): return bridge.reserve(job, claim)
            def reconcile(self, job): return bridge.reconcile(job)
        adapter = LedgerAdapter(LedgerSpec('youtube', 'preparation', str(self.store.db_path), 'youtube_auto_preparation', state='state'), Driver())
        coordinator = Coordinator(dispatch, AdapterRegistry([adapter]), internal_token='i' * 32, master_key='m' * 32)
        offer = adapter.project(adapter.read(self.identity)); dispatch.offer(offer)
        dispatch.channel_enabled('youtube', True)
        def heartbeat():
            dispatch.register_node('cpu-main', ['prepare:youtube', 'backend:cpu'])
            dispatch.worker_heartbeat('cpu-main', 'prepare', 'prepare')
        heartbeat()
        request = {'node_id': 'cpu-main', 'worker_id': 'prepare', 'stage': 'prepare'}
        job = coordinator.route('/internal/pipeline/jobs/claim', request, {'internal': True})['job']
        self.bridge.complete(job, self.receipt, job)  # Native commit succeeds; coordinator ACK is lost.
        clock[0] += dt.timedelta(seconds=121); heartbeat()
        recovered = coordinator.route('/internal/pipeline/jobs/claim', request, {'internal': True})
        self.assertEqual(recovered['idle_reason'], 'canonical_preparation_recovered')
        saved = dispatch.get_job(job['id'])
        self.assertEqual((saved['generation'], saved['status'], saved['artifact']), (2, 'completed', self.artifact))
        self.workflow._prepare_reference.assert_called_once()

        # An explicit review rejection creates another version of the same
        # source ID. It must rearm preparation, while retaining claim history.
        self.workflow.review(self.actor, self.identity, {'action': 'reject', 'version': 1, 'feedback': 'Use a clearer title'})
        coordinator.scan()
        self.assertEqual(dispatch.get_job(job['id'])['status'], 'queued')
        heartbeat()
        revised = coordinator.route('/internal/pipeline/jobs/claim', request, {'internal': True})['job']
        self.assertEqual(revised['generation'], 3)
        self.assertEqual(revised['payload']['request']['task']['current_version'], 2)
        self.assertFalse(revised['payload'].get('recovery_only'))
        self.bridge.complete(revised, {**self.receipt, 'version': 2}, revised)
        clock[0] += dt.timedelta(seconds=121); heartbeat()
        self.assertEqual(coordinator.route('/internal/pipeline/jobs/claim', request, {'internal': True})['idle_reason'],
                         'canonical_preparation_recovered')
        saved = dispatch.get_job(job['id'])
        self.assertEqual((saved['generation'], saved['status']), (4, 'completed'))
        with self.workflow.db() as conn:
            row, body = self.workflow._row(conn, self.identity)
            self.assertEqual(row['state'], 'review')
            self.assertFalse(body['versions'][-1]['approved'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

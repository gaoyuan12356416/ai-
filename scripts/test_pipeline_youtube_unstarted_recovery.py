"""Frozen cover claims may start once, including a lost descriptor response."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.executors import COSArtifactStore, ExecutionError, NodeExecutors
from scripts import test_pipeline_youtube_preparation as native_fixture
from scripts import test_pipeline_executors as executor_fixture


class AtomicCOS(executor_fixture.MemoryCOS):
    """Atomic conditional object create; optionally force both initial GETs 404."""
    def __init__(self, race=False):
        super().__init__()
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2) if race else None

    def get_object(self, Bucket, Key):
        with self.lock:
            missing_intent = Key.endswith('.generation-intent.json') and Key not in self.objects
        if self.barrier is not None and missing_intent:
            self.barrier.wait(5)
            raise executor_fixture.MissingObject()
        return super().get_object(Bucket, Key)

    def put_object(self, **kwargs):
        with self.lock:
            return super().put_object(**kwargs)


class UnstartedCoverTests(unittest.TestCase):
    def setUp(self):
        native_fixture.YouTubePreparationTests.setUp(self)
        # Use the real stored normalized reference bytes, as production does.
        # The older native-only fixture did not invoke the node image decoder.
        with self.workflow.db() as conn:
            stored = self.workflow._asset(conn, self.asset['id'], self.actor)
        self.data = Path(stored['path']).read_bytes()
        reference, material = self.workflow._prepare_reference.return_value
        reference['sha256'] = hashlib.sha256(self.data).hexdigest()
        self.receipt['reference_sha256'] = reference['sha256']

    def executor(self, cloud, name, generator):
        return NodeExecutors({'state_root': str(self.root / name), 'backend': 'cpu'}, factories={
            'youtube_prepare': generator, 'artifact_store': lambda: executor_fixture.MemoryStore(cloud)})

    def test_lost_descriptor_can_start_once_then_recover_same_paid_output(self):
        frozen = self.bridge.reserve(self.job, self.claim)
        # The first worker never received its descriptor; no model was called.
        successor = dict(self.job, generation=2)
        token = {'claim_token': 'new-generation-claim-token'}
        descriptor = self.bridge.reserve(successor, token)
        self.assertEqual(descriptor['request'], frozen['request'])
        self.assertTrue(descriptor['recovery_only'])
        self.assertTrue(descriptor['resume_once'])
        self.workflow._prepare_reference.assert_called_once()
        cloud, calls = AtomicCOS(), []
        def generate(*args): calls.append('paid'); return self.data
        result = self.executor(cloud, 'replacement', generate).prepare('youtube_prepare', descriptor, executor_fixture.Context())
        self.bridge.complete(successor, result, token)
        recovered = self.executor(cloud, 'third', lambda *_: self.fail('paid twice')).prepare('youtube_prepare', descriptor, executor_fixture.Context())
        self.assertEqual(recovered, result)
        self.assertEqual(calls, ['paid'])
        with self.workflow.db() as conn:
            row, body = self.workflow._row(conn, self.identity)
            self.assertEqual(row['state'], 'review')
            self.assertFalse(body['versions'][-1]['approved'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0], 0)

    def test_preexisting_generation_intent_never_restarts_on_rebound_claim(self):
        frozen = self.bridge.reserve(self.job, self.claim)
        cloud = AtomicCOS()
        journal = COSArtifactStore(executor_fixture.MemoryStore(cloud), 'youtube_prepare', frozen['request'])
        journal.begin_generation()
        descriptor = self.bridge.reserve(dict(self.job, generation=2), {'claim_token': 'second-claim-token-that-is-long'})
        executor = self.executor(cloud, 'new-node', lambda *_: self.fail('existing uncertain invocation restarted'))
        with self.assertRaisesRegex(ExecutionError, 'youtube_generation_interrupted'):
            executor.prepare('youtube_prepare', descriptor, executor_fixture.Context())
        self.assertEqual(len(cloud.puts), 1)

    def test_concurrent_new_nodes_only_one_wins_generation_intent(self):
        frozen = self.bridge.reserve(self.job, self.claim)
        descriptor = dict(frozen, recovery_only=True, resume_once=True)
        cloud, calls = AtomicCOS(race=True), []
        def generate(*args): calls.append('paid'); return self.data
        executors = [self.executor(cloud, name, generate) for name in ('cpu', 'gpu')]
        def run(executor):
            try:
                return executor.prepare('youtube_prepare', descriptor, executor_fixture.Context())['status']
            except ExecutionError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, executors))
        self.assertCountEqual(outcomes, ['ready', 'youtube_generation_interrupted'])
        self.assertEqual(calls, ['paid'])
        self.assertEqual(sum(k.endswith('.generation-intent.json') for k in cloud.objects), 1)
        intent_puts = [p for p in cloud.puts if p['Key'].endswith('.generation-intent.json')]
        self.assertEqual(len(intent_puts), 2)  # Both raced; conditional create rejected one.
        self.assertTrue(all(p['Metadata'] == {'x-cos-forbid-overwrite': 'true'} for p in intent_puts))

    def test_slow_reference_for_other_source_does_not_block_cover_receipt(self):
        self.bridge.reserve(self.job, self.claim)
        with self.workflow.db(True) as conn:
            row, body = self.workflow._row(conn, self.identity)
            other = 'b' * 32
            body.update(id=other, status='queued_generation', reference_cover=None)
            body['channel'] = {'channel_id': 'UC' + 'B' * 22}
            conn.execute('INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (other, row['tenant'], row['owner'], 'op-other', 'sha-other', 'queued_generation', 1, json.dumps(body), row['created_at'], row['updated_at']))
        original = self.workflow._prepare_reference
        entered, release = threading.Event(), threading.Event()
        def prepare(task):
            entered.set()
            if not release.wait(5): raise AssertionError('reference fixture not released')
            return original(task)
        self.workflow._prepare_reference = prepare
        slow_job = dict(self.job, source_id=other, source_key='youtube:preparation:' + other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(self.bridge.reserve, slow_job, self.claim)
            try:
                self.assertTrue(entered.wait(2))
                completion = pool.submit(self.bridge.complete, self.job, self.receipt, self.claim)
                self.assertEqual(completion.result(timeout=2)['status'], 'review')
            finally:
                release.set()
            self.assertEqual(slow.result(timeout=2)['kind'], 'youtube_prepare')

    def test_same_channel_remains_exclusive_in_authority_sql_resource_ledger(self):
        now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        dispatch = DispatchStore(self.root / 'dispatch.sqlite', lambda: now)
        for node in ('cpu-main', 'hk-gpu'):
            dispatch.register_node(node, ['prepare:youtube', 'publish:youtube'])
            dispatch.worker_heartbeat(node, 'prep', 'prepare')
            dispatch.worker_heartbeat(node, 'pub', 'publish')
        dispatch.channel_enabled('youtube', True)
        common = {'channel': 'youtube', 'scheduled_at': stamp(now), 'source_created_at': stamp(now), 'resource_keys': ['youtube:channel:UC-A']}
        dispatch.offer(dict(common, source_key='youtube:preparation:first', source_id='first', family='preparation', stage='prepare', capabilities=['prepare:youtube']))
        active = dispatch.claim('cpu-main', 'prep', 'prepare')['job']
        self.assertIsNotNone(active)
        dispatch.offer(dict(common, source_key='youtube:publish:second', source_id='second', family='publish', stage='publish', capabilities=['publish:youtube']))
        self.assertIsNone(dispatch.claim('hk-gpu', 'pub', 'publish')['job'])
        dispatch.offer(dict(common, source_key='youtube:publish:other', source_id='other', family='publish', stage='publish', capabilities=['publish:youtube'], resource_keys=['youtube:channel:UC-B']))
        self.assertEqual(dispatch.claim('hk-gpu', 'pub', 'publish')['job']['source_id'], 'other')

    def test_slow_reference_does_not_block_existing_upload_receipt(self):
        task = self.store.enqueue_youtube(operation_id='offline-upload', job_id='c' * 32,
            content_id='content', app_id='1479', channel_local_id='1', channel_id='UC' + 'B' * 22,
            youtube_account_id='1', source_kind='concat_video', source_url='https://media.example/video.mp4',
            title='title', description_template='description', description_rendered='description', comment_text='',
            duplicate_confirmed=False, scopes=('https://www.googleapis.com/auth/youtube.force-ssl',), operator_user_id='user')
        job = dict(self.job, family='publish', stage='publish', source_id=str(task['id']), source_key='youtube:publish:' + str(task['id']))
        self.bridge.reserve(job, self.claim)
        self.bridge.checkpoint(job, self.claim, 'youtube_source', {'url': task['source_url'], 'sha256': 'b' * 64, 'size': 1024, 'duration_ms': 1000})
        credential = SimpleNamespace(scopes={'https://www.googleapis.com/auth/youtube.force-ssl'}, capabilities={'upload_eligible': True})
        self.workflow.channel_directory = SimpleNamespace(repository=SimpleNamespace(credential=lambda **_: credential))
        self.bridge.client = SimpleNamespace(refresh_access_token=lambda _: 'offline-token', verify_channel_identity=lambda *_: None)
        self.bridge.begin_operation(job, self.claim, {'operation_id': 'offline-upload-init', 'operation': 'youtube_begin_resumable',
            'account_resource': 'youtube:channel:' + task['channel_id'], 'request_sha256': 'a' * 64,
            'details': {'title': 'title', 'description': 'description', 'size': 1024, 'privacy_status': 'public'}})
        entered, release = threading.Event(), threading.Event()
        original = self.workflow._prepare_reference
        def slow_reference(task):
            entered.set()
            if not release.wait(5): raise AssertionError('reference fixture not released')
            return original(task)
        self.workflow._prepare_reference = slow_reference
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.bridge.reserve, self.job, self.claim)
            try:
                self.assertTrue(entered.wait(2))
                receipt = pool.submit(self.bridge.checkpoint, job, self.claim, 'youtube_upload', {'state': 'submitted', 'video_id': 'offline-video'})
                self.assertTrue(receipt.result(timeout=2)['accepted'])
                saved = self.store.youtube_task(task['id'])
                self.assertEqual(saved['video_id'], 'offline-video')
                self.assertEqual(saved['video_attempt_count'], 1)
            finally:
                release.set()
            pending.result(timeout=2)


if __name__ == '__main__':
    unittest.main()

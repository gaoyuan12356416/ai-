"""Slow native reservations over real HTTP, with independent lease heartbeats."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from features.pipeline_dispatch.client import CoordinatorClient, CoordinatorError
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.service import Coordinator, handler_type
from features.pipeline_dispatch.worker import ExecutionContext, ReceiptOutbox, execute_claim
from scripts.test_pipeline_coordinator import NativeLedger
from scripts.test_pipeline_worker import JOB, KEY, Client


class ClaimHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / 'dispatch.db', lambda: self.now)
        self.native = NativeLedger()
        self.coordinator = Coordinator(self.store, self.native, internal_token='i' * 32, master_key='m' * 32)
        self.store.register_node('cpu-main', ['publish:x', 'backend:cpu'])
        self.store.worker_heartbeat('cpu-main', 'one', 'publish')
        self.store.channel_enabled('x', True)
        self.store.offer({'source_key': 'x:queue:1', 'source_id': '1', 'channel': 'x', 'family': 'queue',
                          'stage': 'publish', 'resource_keys': ['x:account:1'], 'capabilities': ['publish:x'],
                          'scheduled_at': stamp(self.now), 'source_created_at': stamp(self.now)})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_type(self.coordinator))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = CoordinatorClient('http://127.0.0.1:' + str(self.server.server_port), 'i' * 32)

    def claim(self, split=True):
        return self.client.post('jobs/claim', {'node_id': 'cpu-main', 'worker_id': 'one', 'stage': 'publish',
                                              'reserve_separately': split})['job']

    def test_slow_reservation_keeps_original_lease_and_executes_only_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def reserve(job, claim):
            calls.append(claim['generation'])
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture_timeout')
            return {'kind': 'x_publish', 'account_resource': 'x:account:1'}
        self.native.reserve = reserve
        job = self.claim()
        self.assertTrue(job['reservation_pending'])
        self.assertFalse(entered.is_set())
        coordinator = self.coordinator
        class FastHeartbeat(ExecutionContext):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs, heartbeat_seconds=.01)
        native = self.native
        class Executor:
            calls = 0
            def execute(self, received, context):
                self.calls += 1
                assert received['payload']['kind'] == 'x_publish'
                native.status = 'published'
                return {'post_id': 'receipt-1'}
        executor = Executor()
        result = []
        outbox = ReceiptOutbox(Path(self.temp.name) / 'outbox.db')
        with patch('features.pipeline_dispatch.worker.ExecutionContext', FastHeartbeat):
            thread = threading.Thread(target=lambda: result.append(execute_claim(job, self.client, outbox, coordinator.node_key('cpu-main'), executor)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(3):
                    self.now += dt.timedelta(seconds=60)
                    deadline = time.monotonic() + 2
                    while self.store.get_job(job['id'])['lease_expires_at'] <= stamp(self.now + dt.timedelta(seconds=100)):
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    self.store.expire()
                    self.assertEqual(self.store.get_job(job['id'])['status'], 'running')
                    self.assertEqual(self.store.get_job(job['id'])['generation'], 1)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertEqual(executor.calls, 1)
        self.assertEqual(self.store.get_job(job['id'])['status'], 'published')
        self.assertNotIn('error_code', result[0])

    def test_legacy_worker_reservation_remains_inline(self):
        job = self.claim(False)
        self.assertNotIn('reservation_pending', job)
        self.assertEqual(job['payload']['kind'], 'x_publish')

    def test_reservation_rejects_other_generation_without_native_selection(self):
        job = self.claim()
        calls = []
        self.native.reserve = lambda *args: calls.append(args)
        payload = {k: job[k] for k in ('generation', 'node_id', 'worker_id', 'claim_token')}
        payload.update(job_id=job['id'], generation=2)
        with self.assertRaises(CoordinatorError) as error:
            self.client.post('jobs/reserve', payload)
        self.assertEqual(error.exception.code, 'claim_conflict')
        self.assertEqual(calls, [])

    def test_control_requests_have_separate_timeout_from_heartbeats(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"accepted":true}'
        client = CoordinatorClient('http://127.0.0.1:18846', 'i' * 32, opener=opener)
        for route, timeout in [('jobs/reserve', 120), ('jobs/complete', 120), ('jobs/checkpoint', 120),
                               ('jobs/begin-operation', 120), ('jobs/heartbeat', 10), ('jobs/claim', 10)]:
            client.post(route, {})
            self.assertEqual(opener.open.call_args.kwargs['timeout'], timeout)

    def test_late_credentials_after_lost_heartbeat_never_enter_platform_write(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                clock = [0.0]
                client = Client()
                ctx = ExecutionContext(JOB, client, ReceiptOutbox(Path(self.temp.name) / ('late-' + str(expired))), KEY, monotonic=lambda: clock[0])
                original = client.post
                def post(route, payload):
                    response = original(route, payload)
                    if route == 'jobs/begin-operation':
                        if expired:
                            clock[0] = 121
                        else:
                            ctx.lost.set()
                    return response
                client.post = post
                with self.assertRaises(Exception):
                    with ctx.operation('create', {'body': 'frozen'}):
                        self.fail('late credentials must not reach the platform')
                self.assertTrue(ctx.write_started)
                self.assertEqual(len(client.calls), 1)

    def test_native_account_conflict_waits_without_changing_appointment(self):
        from features.pipeline_dispatch.adapters import AdapterError
        self.native.reserve = lambda *args: (_ for _ in ()).throw(AdapterError('canonical_account_busy'))
        original_due = stamp(self.now)
        job = self.claim()
        identity = {k: job[k] for k in ('generation', 'node_id', 'worker_id', 'claim_token')}
        result = self.client.post('jobs/reserve', {**identity, 'job_id': job['id']})
        self.assertIsNone(result['job'])
        saved = self.store.get_job(job['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['scheduled_at'], original_due)
        self.assertIsNone(self.claim())
        self.now += dt.timedelta(seconds=31)
        self.store.worker_heartbeat('cpu-main', 'one', 'publish')
        self.store.register_node('cpu-main', ['publish:x', 'backend:cpu'])
        self.assertEqual(self.claim()['generation'], 2)

    def test_native_unknown_escalates_failed_admission_and_fences_account(self):
        job = self.claim()
        self.store.fail(job, 'canonical_task_not_claimable')
        self.store.offer({'source_key': 'x:queue:1', 'source_id': '1', 'channel': 'x', 'family': 'queue',
                          'stage': 'publish', 'status': 'unknown', 'source_status': 'unknown',
                          'resource_keys': ['x:account:1'], 'capabilities': ['publish:x'],
                          'scheduled_at': stamp(self.now), 'source_created_at': stamp(self.now)})
        self.assertEqual(self.store.get_job(job['id'])['status'], 'unknown')
        self.store.offer({'source_key': 'x:queue:2', 'source_id': '2', 'channel': 'x', 'family': 'queue',
                          'stage': 'publish', 'resource_keys': ['x:account:1'], 'capabilities': ['publish:x'],
                          'scheduled_at': stamp(self.now), 'source_created_at': stamp(self.now)})
        self.assertIsNone(self.claim())


if __name__ == '__main__':
    unittest.main()

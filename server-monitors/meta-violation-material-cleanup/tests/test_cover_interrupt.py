"""Exercise cooperative read cancellation with real transport and SQLite guards."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import cover
from features.fb_ad_asset_delete.store import Store
from meta_violation_cleanup.cover_graph import CoverGraph
from meta_violation_cleanup.cover_pipeline import run_lanes
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.cover_worker import CoverWorker
from meta_violation_cleanup.throttle import Governor, RequestNotSent
from test_cover_graph import Session
from test_cover_worker import Graph, Source, ad, creative


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoverStore(Path(self.temp.name) / 'cover.sqlite3')
        self.shared = Store(Path(self.temp.name) / 'shared.sqlite3')
        self.config = {'cover_live_enabled': True, 'execution_workers': 1}
        self.store.begin_batch('batch', {})
        self.store.enqueue('batch', {'account_id': '100', 'ad_id': '10',
            'old_creative_id': '20', 'user_id': '1', 'root_id': '1479', 'material_ids': ['501']})
        self.store.seal_batch('batch')

    def transport(self, stop):
        session = Session([])
        graph = CoverGraph({'1': 'synthetic-token'}, governor=Governor(), session=session, stop=stop)
        self.addCleanup(graph.close)
        return graph, session

    def worker(self, graph, stop=lambda: False):
        worker = CoverWorker(self.config, self.store, Source(), lambda: graph, self.shared, stop=stop)
        self.addCleanup(worker.close)
        return worker

    def test_all_read_paths_cancel_without_network_or_delete_intent(self):
        for method in ('ad', 'creative', 'references', 'inventory', 'delete'):
            with self.subTest(method=method):
                graph, session = self.transport(lambda: True)
                guard = MagicMock()
                calls = {'ad': lambda: graph.get_cover_ad('10', '1', '100'),
                         'creative': lambda: graph.get_creative('20', '1', '100'),
                         'references': lambda: graph.get_reference_ads(['10'], '1', '100'),
                         'inventory': lambda: graph.list_reference_ads('100', '1'),
                         'delete': lambda: graph.delete_creative('20', '1', '100', guard)}
                with self.assertRaises(RequestNotSent):
                    calls[method]()
                self.assertEqual(session.calls, [])
                guard.assert_not_called()

    def test_unrelated_interruption_is_not_normalized(self):
        graph, _ = self.transport(lambda: True)
        with patch('meta_violation_cleanup.graph.GraphClient._get', side_effect=InterruptedError('disk_io')):
            with self.assertRaisesRegex(InterruptedError, 'disk_io'):
                graph.get_cover_ad('10', '1', '100')

    def test_hourly_rotation_defers_unsent_task_then_next_cycle_completes(self):
        clock = [0.]
        transport, session = self.transport(lambda: clock[0] >= 3600)
        class InterruptedGraph(Graph):
            interrupt = True
            def get_cover_ad(inner, aid, user, account):
                if inner.interrupt:
                    clock[0] = 3600.
                    return transport.get_cover_ad(aid, user, account)
                return super().get_cover_ad(aid, user, account)
        graph = InterruptedGraph([ad()], [creative()])
        reports = []
        def cycle(lane, stopped):
            if lane == 'ads':
                # Source fixtures are thread-owned, like the real lane.
                reports.append(self.worker(graph, stopped).run_once(max_ads=1, process_deletes=False))
            return 0
        run_lanes(cycle, lambda: False, clock=lambda: clock[0])
        self.assertEqual(reports[0]['tasks'], {'pending': 1})
        self.assertFalse(reports[0]['halted'])
        self.assertEqual(self.store.get_task(1)['request_sent'], 0)
        self.assertEqual(session.calls, [])
        graph.interrupt = False
        result = self.worker(graph).run_once(max_ads=1, process_deletes=False)
        self.assertEqual(result['tasks'], {'succeeded': 1})
        self.assertEqual(sum(x[0] == 'bind' for x in graph.calls), 1)

    def test_interrupted_accepted_bind_is_reconciled_without_replay(self):
        stopping = threading.Event()
        transport, _ = self.transport(stopping.is_set)
        class InterruptedGraph(Graph):
            interrupt = True
            def get_cover_ad(inner, aid, user, account):
                if inner.interrupt and aid in inner.bound:
                    stopping.set()
                    return transport.get_cover_ad(aid, user, account)
                return super().get_cover_ad(aid, user, account)
        graph = InterruptedGraph([ad()], [creative()])
        worker = self.worker(graph, stopping.is_set)
        result = worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(result['tasks'], {'unknown': 1})
        self.assertFalse(result['halted'])
        self.assertEqual(self.store.stats()['deletions'], {})
        self.assertTrue(any(r['status'] == 'unknown' for r in self.shared.cover_reservations()))
        graph.interrupt = False
        stopping.clear()
        self.assertEqual(worker.reconcile_unknown()['tasks_confirmed'], 1)
        self.assertEqual(sum(x[0] == 'bind' for x in graph.calls), 1)
        self.assertEqual(self.store.get_task(1)['status'], 'succeeded')

    def test_interrupted_creation_readback_preserves_resource_without_replay(self):
        stopping = threading.Event()
        transport, _ = self.transport(stopping.is_set)
        class InterruptedGraph(Graph):
            interrupt = True
            def get_creative(inner, cid, user, account):
                if inner.interrupt and int(cid) >= 1000:
                    stopping.set()
                    return transport.get_creative(cid, user, account)
                return super().get_creative(cid, user, account)
        graph = InterruptedGraph([ad()], [creative()])
        worker = self.worker(graph, stopping.is_set)
        result = worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(result['tasks'], {'unknown': 1})
        self.assertFalse(result['halted'])
        graph.interrupt = False
        stopping.clear()
        self.assertEqual(worker.reconcile_unknown()['resources_confirmed'], 1)
        worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(self.store.get_task(1)['status'], 'succeeded')
        self.assertEqual(sum(x[0] == 'create' for x in graph.calls), 1)
        self.assertEqual(sum(x[0] == 'bind' for x in graph.calls), 1)

    def test_cleanup_interruption_does_not_consume_delete_or_halt(self):
        for phase in ('inventory', 'identity'):
            with self.subTest(phase=phase):
                graph = Graph([ad()], [creative()])
                worker = self.worker(graph)
                if phase == 'inventory':
                    worker.run_once(max_ads=1, process_deletes=False)
                row = self.store.get_delete('100', '20')
                transport, session = self.transport(lambda: True)
                if phase == 'inventory':
                    graph.list_reference_ads = transport.list_reference_ads
                    outputs = worker.process_deletions()
                else:
                    graph.delete_creative = transport.delete_creative
                    outputs = [worker._delete_one(row, worker.clock())]
                self.assertFalse(worker.halted.is_set())
                self.assertEqual(outputs[0]['status'], 'reference_scan_incomplete')
                self.assertEqual(self.store.get_delete('100', '20')['request_sent'], 0)
                self.assertIsNone(self.shared.get_cover_delete_once('100', '20'))
                self.assertEqual(session.calls, [])

    def test_shutdown_during_reconciliation_does_not_create_pause(self):
        stopping = threading.Event()
        worker, store, audit = MagicMock(), MagicMock(), MagicMock()
        worker.recover_reservations.return_value = {}
        def cancel(**kwargs):
            stopping.set()
            raise RequestNotSent('cancelled_before_request')
        worker.reconcile_unknown.side_effect = cancel
        @contextmanager
        def lease(*args):
            yield MagicMock()
        config = dict(self.config, runtime_dir=self.temp.name, shared_ledger='unused', legacy_lock='unused')
        with patch.object(cover, 'Store'), patch.object(cover, 'CoverSource'), \
             patch.object(cover, 'legacy_delete_lock', lease), \
             patch('meta_violation_cleanup.cover_worker.CoverWorker', return_value=worker):
            cover.work(config, store, audit, stopping.is_set, False, 256)
        self.assertFalse((Path(self.temp.name) / 'cover-write-paused.json').exists())
        store.fail_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()

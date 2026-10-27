import copy
import threading
import time
import types
import unittest
from unittest.mock import patch

from meta_violation_cleanup.graph import GraphClient, GraphError
from meta_violation_cleanup.reference_scan import (
    _new_reader, _ReadClient, read_known_ads, scan_accounts,
)
from meta_violation_cleanup.throttle import Governor, GovernedSession


def ad(identifier='10', account='100', creative='20'):
    return {'id': identifier, 'account_id': account, 'creative': {'id': creative},
            'status': 'PAUSED', 'effective_status': 'PAUSED'}


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code = payload, status
        self.headers = headers or {}

    def json(self):
        return copy.deepcopy(self.payload)


class Session:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        if method != 'GET':
            raise AssertionError('reference reader attempted a mutation')
        self.calls.append((method, url, kwargs))
        return self.responder(method, url, kwargs)

    def close(self):
        self.closed = True


def runner(workers=4):
    return types.SimpleNamespace(config={'shared_reference_scan_workers': workers},
                                 graph=object(), stop=lambda: False)


class ScannerTests(unittest.TestCase):
    def test_four_accounts_overlap_deduplicate_and_serialize_same_account(self):
        state = {'active': 0, 'peak': 0, 'accounts': set(), 'calls': [], 'closed': 0}
        lock = threading.Lock()
        threads = set()
        class Reader:
            reference_pages = 2

            def list_ads(self, account, user):
                with lock:
                    if account in state['accounts']:
                        raise AssertionError('same account overlapped')
                    state['accounts'].add(account)
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    state['calls'].append((account, user))
                    threads.add(threading.get_ident())
                try:
                    time.sleep(.04)
                    return [ad(account=account)]
                finally:
                    with lock:
                        state['accounts'].remove(account)
                        state['active'] -= 1

            def close(self):
                with lock:
                    state['closed'] += 1
        owner = threading.get_ident()
        metrics = {}
        keys = [('100', '1'), ('100', '2'), ('100', '1')]
        keys += [(str(101+i), '1') for i in range(7)]
        with patch('meta_violation_cleanup.reference_scan._new_reader',
                   side_effect=lambda *args: (Reader(), True)):
            results = list(scan_accounts(runner(), keys, object(), metrics=metrics))
        self.assertEqual(len(results), 9)
        self.assertEqual(len(set(state['calls'])), 9)
        self.assertEqual(state['peak'], 4)
        self.assertNotIn(owner, threads)
        self.assertTrue(all(result['complete'] for _, result in results))
        self.assertEqual(metrics['account_scans'], 9)
        self.assertEqual(metrics['accounts_completed'], 9)
        self.assertEqual(metrics['pages'], 18)
        self.assertEqual(metrics['peak_accounts'], 4)
        self.assertEqual(state['closed'], 9)

    def test_single_account_rollback_and_invalid_worker_limit(self):
        class Reader:
            reference_pages = 1
            def list_ads(self, account, user):
                return [ad(account=account)]
        with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(Reader(), False)):
            metrics = {}
            list(scan_accounts(runner(1), [('100', '1'), ('101', '1')], object(), metrics=metrics))
            self.assertEqual(metrics['peak_accounts'], 1)
            for invalid in (0, 5, True, '4'):
                with self.assertRaises(ValueError):
                    list(scan_accounts(runner(invalid), [('100', '1')], object()))

    def test_partial_inventory_is_discarded_and_other_accounts_continue(self):
        def factory(*args):
            class Reader:
                reference_pages = 0
                def list_ads(self, account, user):
                    self.reference_pages = 1
                    if account == '100':
                        raise GraphError('invalid_page', 'invalid continuation')
                    return [ad(account=account)]
                def close(self):
                    pass
            return Reader(), True
        with patch('meta_violation_cleanup.reference_scan._new_reader', side_effect=factory):
            metrics = {}
            results = dict(scan_accounts(runner(), [('100', '1'), ('101', '1')], object(), metrics=metrics))
        self.assertFalse(results[('100', '1')]['complete'])
        self.assertEqual(results[('100', '1')]['by_creative'], {})
        self.assertTrue(results[('101', '1')]['complete'])
        self.assertEqual(metrics['account_scan_failures'], 1)
        self.assertEqual(metrics['pages'], 2)

    def test_unknown_creative_binding_invalidates_account_inventory(self):
        reader = types.SimpleNamespace(list_ads=lambda *args: [ad(), dict(ad('11'), creative={})])
        with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(reader, False)):
            result = list(scan_accounts(runner(), [('100', '1')], object()))[0][1]
        self.assertFalse(result['complete'])
        self.assertEqual(result['by_creative'], {})

    def test_unexpected_exception_does_not_expose_transport_secrets(self):
        def fail(*args):
            raise RuntimeError('https://example.invalid/?access_token=SECRET')
        reader = types.SimpleNamespace(list_ads=fail)
        with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(reader, False)):
            result = list(scan_accounts(runner(), [('100', '1')], object()))[0][1]
        self.assertEqual(result['error'], {'code': 'reference_read_unavailable', 'type': 'RuntimeError'})
        self.assertNotIn('SECRET', str(result))

    def test_cancel_stops_admission_and_drains_sessions(self):
        stop = threading.Event()
        state = {'calls': 0, 'closed': 0}
        lock = threading.Lock()
        def factory(*args):
            class Reader:
                reference_pages = 0
                def list_ads(self, account, user):
                    with lock:
                        state['calls'] += 1
                    stop.set()
                    time.sleep(.02)
                    return []
                def close(self):
                    with lock:
                        state['closed'] += 1
            return Reader(), True
        with patch('meta_violation_cleanup.reference_scan._new_reader', side_effect=factory):
            with self.assertRaises(InterruptedError):
                list(scan_accounts(runner(), [(str(100+i), '1') for i in range(12)],
                                   object(), stop=stop.is_set))
        self.assertLessEqual(state['calls'], 4)
        self.assertEqual(state['calls'], state['closed'])

    def test_production_reader_has_isolated_session_and_shared_governor(self):
        original = GraphClient({'1': 'secret'}, session=Session(lambda *a: Response({})))
        self.addCleanup(original.close)
        r = runner()
        r.graph = original
        governor = Governor(maximum=32, rps=24)
        a, owned_a = _new_reader(r, governor, lambda: False)
        b, owned_b = _new_reader(r, governor, lambda: False)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertTrue(owned_a and owned_b)
        self.assertIsNot(a.http.session, b.http.session)
        self.assertIsNot(a.http.session, original.http)
        self.assertIs(a.http.governor, governor)
        self.assertIs(b.http.governor, governor)
        self.assertEqual(a.get_retries, 0)
        self.assertTrue(a.delete_stop())
        self.assertIs(a._metrics, original._metrics)

    def test_real_pagination_counter_and_invalid_second_page(self):
        for fail in (False, True):
            def responder(method, url, kwargs):
                if kwargs['params'].get('after'):
                    return Response({'data': [ad('11')]}) if not fail else Response({'broken': True})
                return Response({'data': [ad()], 'paging': {'next':
                    'https://graph.facebook.com/v25.0/act_100/ads?after=next'}})
            session = Session(responder)
            reader = _ReadClient({'1': 'secret'}, session=session, get_retries=0)
            with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(reader, True)):
                result = list(scan_accounts(runner(1), [('100', '1')], object()))[0][1]
            self.assertEqual(result['complete'], not fail)
            self.assertEqual(result['metrics']['pages'], 1 if fail else 2)
            self.assertEqual(result['by_creative'], {} if fail else {'20': [ad(), ad('11')]})
            self.assertTrue(session.closed)


class KnownBlockerReadTests(unittest.TestCase):
    def read(self, ids, responder):
        session = Session(responder)
        reader = _ReadClient({'1': 'secret'}, session=session, get_retries=0)
        with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(reader, True)):
            result = read_known_ads(runner(), '100', '1', ids, object())
        self.assertTrue(session.closed)
        return result, session

    def test_requests_at_most_50_unique_ids_and_missing_is_error(self):
        def responder(method, url, kwargs):
            ids = kwargs['params']['ids'].split(',')
            self.assertLessEqual(len(ids), 50)
            return Response({key: ad(key) for key in ids if key != '120'})
        identifiers = [str(i) for i in range(1, 121)] + ['1', '2']
        result, session = self.read(identifiers, responder)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(result['records']), 119)
        self.assertEqual(set(result['errors']), {'120'})
        self.assertEqual(result['metrics']['target_ids'], 120)

    def test_bisects_only_definite_object_errors(self):
        def responder(method, url, kwargs):
            ids = kwargs['params']['ids'].split(',')
            if '2' in ids:
                return Response({'error': {'code': 100, 'message': 'invalid object'}}, 400)
            return Response({key: ad(key) for key in ids})
        result, session = self.read(['1', '2', '3'], responder)
        self.assertEqual(set(result['records']), {'1', '3'})
        self.assertEqual(set(result['errors']), {'2'})
        self.assertGreater(len(session.calls), 1)
        result, session = self.read(['1', '2', '3'], lambda *a:
                                    Response({'error': {'code': 190, 'message': 'permission'}}, 400))
        self.assertEqual(result['records'], {})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(set(result['errors']), {'1', '2', '3'})

    def test_exact_account_and_object_proof_required(self):
        result, _ = self.read(['1', '2'], lambda *a:
                              Response({'1': ad('1', account='101'), '2': ad('3')}))
        self.assertEqual(result['records'], {})
        self.assertEqual(set(result['errors']), {'1', '2'})

    def test_malformed_per_object_error_stays_unreadable(self):
        result, _ = self.read(['1'], lambda *a: Response({'1': {'error': 'broken'}}))
        self.assertEqual(result['records'], {})
        self.assertEqual(result['errors']['1']['code'], 'invalid_object')

    def test_rate_limit_uses_shared_governor_and_does_not_retry(self):
        session = Session(lambda *a: Response({'error': {'code': 4}}, 429))
        reader = _ReadClient({'1': 'secret'}, session=session, get_retries=0)
        governor = Governor(maximum=32, rps=24)
        reader.http = GovernedSession(session, governor, lambda: False)
        with patch('meta_violation_cleanup.reference_scan._new_reader', return_value=(reader, True)):
            result = read_known_ads(runner(), '100', '1', ['1', '2'], governor)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(set(result['errors']), {'1', '2'})
        self.assertGreater(governor.snapshot()['global_cooldown_seconds'], 0)
        self.assertEqual(governor.snapshot()['rate_limit_responses'], 1)

    def test_stop_before_read_opens_no_session(self):
        with patch('meta_violation_cleanup.reference_scan._new_reader') as make:
            with self.assertRaises(InterruptedError):
                read_known_ads(runner(), '100', '1', ['1'], object(), stop=lambda: True)
        make.assert_not_called()


if __name__ == '__main__':
    unittest.main()

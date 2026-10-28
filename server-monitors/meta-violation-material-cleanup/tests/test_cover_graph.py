import copy
from contextlib import contextmanager
import json
import pathlib
import sys
import unittest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.cover_graph import CoverGraph, REFERENCE_STATUSES
from meta_violation_cleanup.cover_payload import PayloadError
from meta_violation_cleanup.graph import GraphError
from meta_violation_cleanup.throttle import Governor, RequestNotSent


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return copy.deepcopy(self.data)


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def mount(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Budget:
    def __init__(self):
        self.inside = False
        self.accounts, self.responses = [], []

    @contextmanager
    def slot(self, account, stop):
        if stop():
            raise RequestNotSent('cancelled_before_request')
        self.inside = True
        self.accounts.append(account)
        try:
            yield
        finally:
            self.inside = False

    def observe(self, response, account):
        self.responses.append((response.status_code, account))


def ad(identifier='10', creative='20', status='ACTIVE'):
    return {'id': identifier, 'account_id': '100', 'name': 'Ad',
            'status': status, 'configured_status': status, 'effective_status': status,
            'creative': {'id': creative}, 'tracking_specs': [{'x': 1}],
            'adset_id': '30', 'campaign_id': '40'}


class CoverGraphTests(unittest.TestCase):
    def client(self, responses, **kwargs):
        session, budget = Session(responses), Budget()
        graph = CoverGraph({'1': 'synthetic-secret'}, governor=budget, session=session, get_retries=0, **kwargs)
        return graph, session, budget

    def test_post_only_after_durable_guard_inside_slot_and_never_uploads_image(self):
        graph, session, budget = self.client([Response({'id': '50'})])
        guards = []
        payload = {'name': 'original', 'object_story_spec': {'page_id': '40', 'link_data': {
            'picture': 'https://cdn.example/cover.jpg', 'link': 'https://example.com?tracking=1'}}}
        def guard():
            self.assertTrue(budget.inside)
            self.assertEqual(session.calls, [])
            guards.append(True)
        result = graph.create_creative('act_100', '1', payload, guard)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['new_creative_id'], '50')
        self.assertTrue(result['request_sent'])
        self.assertEqual(guards, [True])
        self.assertEqual(budget.accounts, ['100'])
        method, url, request = session.calls[0]
        self.assertEqual(method, 'POST'); self.assertTrue(url.endswith('/act_100/adcreatives'))
        self.assertEqual(json.loads(request['data']['object_story_spec']), payload['object_story_spec'])
        self.assertNotIn('access_token', request['data'])
        self.assertFalse(request['allow_redirects'])

    def test_ad_write_contains_only_creative(self):
        graph, session, _ = self.client([Response({'success': True})])
        result = graph.bind_creative('10', '50', '1', '100', lambda: None)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(session.calls[0][2]['data'], {'creative': '{"creative_id":"50"}'})

    def test_create_projects_transport_but_preserves_expected_payload(self):
        graph, session, _ = self.client([Response({'id': '50'})])
        payload = {'object_story_spec': {}, 'enable_direct_install': False,
                   'enable_launch_instant_app': False,
                   'creative_sourcing_spec': {'destination_screenshot_spec': {'enroll_status': 'OPT_OUT'}},
                   'degrees_of_freedom_spec': {'creative_features_spec': {
                       'standard_enhancements': {'enroll_status': 'OPT_IN'},
                       'enhance_cta': {'enroll_status': 'OPT_IN'}}}}
        frozen = copy.deepcopy(payload)
        graph.create_creative('100', '1', payload, lambda: None)
        wire = session.calls[0][2]['data']
        self.assertEqual(payload, frozen)
        self.assertNotIn('enable_direct_install', wire)
        self.assertEqual(json.loads(wire['degrees_of_freedom_spec']), {
            'creative_features_spec': {'enhance_cta': {'enroll_status': 'OPT_IN'}}})
        self.assertEqual(json.loads(wire['creative_sourcing_spec']), payload['creative_sourcing_spec'])

    def test_unsupported_projection_never_claims_or_sends_write(self):
        for extra in ({'enable_direct_install': True},
                      {'degrees_of_freedom_spec': {'creative_features_spec': {
                          'standard_enhancements': {'enroll_status': 'OPT_IN'}}}}):
            graph, session, _ = self.client([]); guards = []
            with self.assertRaises(PayloadError):
                graph.create_creative('100', '1', dict(object_story_spec={}, **extra), lambda: guards.append(True))
            self.assertEqual(session.calls, [])
            self.assertEqual(guards, [])

    def test_unknown_transport_and_5xx_never_retry(self):
        for response in (requests.Timeout(), Response({'error': {'code': 2}}, 503),
                         Response(ValueError('bad json')), Response({'unexpected': True}),
                         Response({'id': True})):
            graph, session, _ = self.client([response])
            result = graph.create_creative('100', '1', {'object_story_spec': {}}, lambda: None)
            self.assertEqual(result['status'], 'unknown')
            self.assertTrue(result['request_sent'])
            self.assertEqual(len(session.calls), 1)

    def test_definite_error_is_failed_and_secrets_redacted(self):
        graph, session, _ = self.client([Response({'error': {'code': 100, 'error_subcode': 1885031,
            'message': 'bad synthetic-secret access_token=foo'}}, 400)])
        result = graph.bind_creative('10', '50', '1', '100', lambda: None)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertNotIn('=foo', json.dumps(result))
        self.assertEqual(len(session.calls), 1)

    def test_guard_rejection_sends_nothing(self):
        graph, session, _ = self.client([])
        result = graph.bind_creative('10', '50', '1', '100', lambda: False)
        self.assertFalse(result['request_sent'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(session.calls, [])

    def test_ledger_exception_propagates_without_network(self):
        graph, session, _ = self.client([])
        def ledger_error():
            raise RuntimeError('ledger failure')
        with self.assertRaisesRegex(RuntimeError, 'ledger failure'):
            graph.bind_creative('10', '50', '1', '100', ledger_error)
        self.assertEqual(session.calls, [])

    def test_full_get_preserves_configuration_and_validates_identity(self):
        graph, session, budget = self.client([Response(ad())])
        self.assertEqual(graph.get_cover_ad('10', '1', '100')['tracking_specs'], [{'x': 1}])
        self.assertEqual(budget.accounts, ['100'])
        graph, _, _ = self.client([Response(dict(ad(), account_id='101'))])
        with self.assertRaisesRegex(GraphError, 'different account'):
            graph.get_cover_ad('10', '1', '100')

    def test_reference_inventory_includes_archived_and_checks_all_pages(self):
        page1 = {'data': [ad()], 'paging': {'next': 'https://graph.facebook.com/v25.0/act_100/ads?after=next'}}
        page2 = {'data': [ad('11', '20', 'ARCHIVED')]}
        graph, session, _ = self.client([Response(page1), Response(page2)])
        rows = graph.list_reference_ads('100', '1')
        self.assertEqual([r['id'] for r in rows], ['10', '11'])
        self.assertIn('ARCHIVED', json.loads(session.calls[0][2]['params']['effective_status']))
        self.assertEqual(session.calls[1][2]['params']['after'], 'next')
        self.assertNotIn('DELETED', REFERENCE_STATUSES)

    def test_reference_incomplete_never_returns_partial_success(self):
        page = {'data': [ad()], 'paging': {'next': 'https://graph.facebook.com/v25.0/act_100/ads?after=next'}}
        graph, _, _ = self.client([Response(page), Response({'error': {'code': 190}}, 400)])
        with self.assertRaises(GraphError): graph.list_reference_ads('100', '1')

    def test_known_reference_batch_uses_same_budget_and_keeps_per_id_failures(self):
        graph, session, budget = self.client([Response({'10': ad(), '11': {'error': {'code': 100, 'message': 'missing'}}})])
        records, errors = graph.get_reference_ads(['10', '11'], '1', '100')
        self.assertEqual(set(records), {'10'}); self.assertEqual(set(errors), {'11'})
        self.assertEqual(budget.accounts, ['100'])
        self.assertEqual(session.calls[0][2]['params']['ids'], '10,11')
        with self.assertRaises(ValueError): graph.get_reference_ads([], '1', '100')

    def test_reconcile_uses_exact_account_without_full_creative_payload_read(self):
        graph, session, budget = self.client([Response({'id': '20', 'account_id': '100', 'status': 'DELETED'})])
        result = graph.reconcile('creative', '20', '1', '100')
        self.assertEqual(result['status'], 'deleted_verified')
        self.assertEqual(budget.accounts, ['100'])
        graph, _, _ = self.client([Response({'data': [dict(ad(), creative={})]})])
        with self.assertRaises(GraphError): graph.list_reference_ads('100', '1')

    def test_delete_gets_exact_account_then_one_request_and_already_deleted_no_write(self):
        graph, session, budget = self.client([Response({'id': '20', 'account_id': '100', 'status': 'ACTIVE'}),
                                            Response({'success': True})])
        guarded = []
        result = graph.delete_creative('20', '1', '100', lambda: guarded.append(True))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual([c[0] for c in session.calls], ['GET', 'DELETE'])
        self.assertEqual(guarded, [True]); self.assertEqual(budget.accounts, ['100', '100'])
        graph, session, _ = self.client([Response({'id': '20', 'account_id': '100', 'status': 'DELETED'})])
        result = graph.delete_creative('20', '1', '100', lambda: self.fail('no write expected'))
        self.assertEqual(result['status'], 'already_deleted')
        self.assertFalse(result['request_sent'])
        self.assertEqual([c[0] for c in session.calls], ['GET'])

    def test_initial_32_and_no_ramp_preserves_legacy_defaults(self):
        self.assertEqual(Governor(maximum=32).limit, 4)
        now = [0.0]
        governor = Governor(maximum=32, initial_limit=32, ramp_up=False, clock=lambda: now[0])
        self.assertEqual(governor.limit, 32)
        governor.observe(Response({'error': {'code': 613}}, 400), '100')
        self.assertEqual(governor.limit, 16)
        now[0] = 180.0
        governor.observe(Response({}, headers={'x-app-usage': '{"call_count":1}'}), '100')
        self.assertEqual(governor.limit, 16)
        for invalid in (0, 33, True, 1.5):
            with self.assertRaises(ValueError): Governor(maximum=32, initial_limit=invalid)


if __name__ == '__main__':
    unittest.main()

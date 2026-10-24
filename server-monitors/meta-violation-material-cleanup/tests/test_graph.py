import copy
import json
import pathlib
import sys
import unittest
import threading
import time
from unittest.mock import patch

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.graph import AD_FIELDS, CURRENT_STATUSES, GraphClient, GraphError


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return copy.deepcopy(self.data)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.adapters = {}

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ad(identifier="10", account="100", status="PAUSED", creative="20"):
    return {
        "id": identifier, "account_id": account, "status": status,
        "effective_status": status, "issues_info": [], "ad_review_feedback": {},
        "creative": {"id": creative}, "adset": {"id": "30", "status": "ACTIVE"},
        "campaign": {"id": "40", "status": "ACTIVE"},
    }


def creative(identifier="20", account="100", status="ACTIVE"):
    return {"id": identifier, "account_id": account, "status": status}


def page(rows, cursor=None, path="act_100/ads"):
    result = {"data": rows}
    if cursor is not None:
        result["paging"] = {
            "next": "https://graph.facebook.com/v25.0/" + path + "?after=" + cursor + "&access_token=secret-token",
            "cursors": {"after": cursor},
        }
    return Response(result)


class GraphTests(unittest.TestCase):
    def test_final_binding_check_prevents_delete_after_creative_changes(self):
        session=Session([Response(ad(creative='21'))])
        client=GraphClient({'1':'synthetic-token'},session=session)
        result=client.delete('ad','10','1','100',expected_creative_id='20')
        self.assertEqual(result['code'],'creative_changed_before_delete')
        self.assertFalse(result['request_sent'])
        self.assertEqual([c[0] for c in session.calls],['GET'])

    def test_read_worker_configuration_bounds_and_injected_session_serial(self):
        for value in (0, 17, True, '16', 2.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GraphClient({'1': 'synthetic-token'}, read_workers=value)
        for value in (1, 4, 16):
            client = GraphClient({'1': 'synthetic-token'}, read_workers=value)
            self.assertEqual(client.read_workers, value)
            client.close()
        client = GraphClient({'1': 'synthetic-token'}, read_workers=16, session=Session([]))
        self.assertEqual(client.read_workers, 1)

    def test_sixteen_read_workers_are_bounded_and_stop_before_more_work(self):
        client = GraphClient({'1': 'synthetic-token'}, read_workers=16)
        barrier = threading.Barrier(16)
        lock = threading.Lock(); stop = threading.Event()
        state = {'active': 0, 'peak': 0, 'started': 0}
        def work(item):
            with lock:
                state['active'] += 1; state['started'] += 1
                state['peak'] = max(state['peak'], state['active'])
            try:
                if item < 16:
                    barrier.wait(3)
                return item
            finally:
                with lock: state['active'] -= 1
        iterator = client._bounded_reads(work, range(100), stop.is_set)
        try:
            next(iterator); stop.set()
            with self.assertRaises(InterruptedError): next(iterator)
            self.assertEqual(state['peak'], 16)
            self.assertLessEqual(state['started'], 17)
        finally:
            iterator.close(); client.close()

    def test_get_metrics_keep_only_numeric_usage_and_rate_limit_counts(self):
        client, _, sleeps = self.client([
            Response({'error': {'code': 4, 'message': 'limited'}}, 429, {
                'Retry-After': '3',
                'X-App-Usage': json.dumps({'call_count': 42, 'total_time': 12, 'secret': 'secret-token'}),
                'x-ad-account-usage': json.dumps({'acc_id_util_pct': 3.5}),
                'x-business-use-case-usage': json.dumps({'private-business-id': [
                    {'call_count': 22, 'total_time': 30, 'estimated_time_to_regain_access': 9999}]}),
            }),
            Response(ad(), headers={
                'X-App-Usage': json.dumps({'call_count': 11, 'total_cputime': 49}),
                'x-ad-account-usage': '{not-json',
                'x-business-use-case-usage': json.dumps({'unsafe': {'call_count': True,
                    'total_time': float('inf'), 'total_cputime': 10 ** 400}}),
            }),
        ])
        client.get_ad('10', '1')
        metrics = client.safe_metrics
        self.assertEqual((metrics['get_requests'], metrics['get_retries'],
                          metrics['rate_limit_responses'], metrics['rate_limit_retries']), (2, 1, 1, 1))
        self.assertEqual((metrics['usage_app_max_percent'], metrics['usage_ad_account_max_percent'],
                          metrics['usage_business_max_percent']), (49, 3.5, 30))
        self.assertEqual((metrics['usage_app_observations'], metrics['usage_ad_account_observations'],
                          metrics['usage_business_observations']), (2, 1, 1))
        self.assertEqual(sleeps, [3])
        self.assertTrue(all(isinstance(value, (int, float)) and not isinstance(value, bool)
                            for value in metrics.values()))
        self.assertNotIn('secret-token', json.dumps(metrics))
        self.assertNotIn('private-business-id', json.dumps(metrics))

    def test_timeout_retries_are_counted_without_claiming_rate_limit(self):
        client, _, _ = self.client([requests.Timeout(), Response(ad())])
        client.get_ad('10', '1')
        self.assertEqual(client.safe_metrics['get_requests'], 2)
        self.assertEqual(client.safe_metrics['get_retries'], 1)
        self.assertEqual(client.safe_metrics['rate_limit_responses'], 0)
        self.assertEqual(client.safe_metrics['rate_limit_retries'], 0)
        self.assertEqual(client.safe_metrics['usage_app_observations'], 0)

    def test_next_read_is_refilled_before_consumer_receives_result(self):
        client=GraphClient({'1':'synthetic-token'})
        others_release=threading.Event();replacement_started=threading.Event()
        def work(item):
            if item in (1,2,3):others_release.wait(2)
            if item==4:replacement_started.set()
            return item
        iterator=client._bounded_reads(work,range(8))
        try:
            self.assertEqual(next(iterator),0)
            # The consumer is paused here, simulating serial SQL work. The
            # replacement must already run without resuming the generator.
            self.assertTrue(replacement_started.wait(1))
        finally:
            others_release.set();iterator.close();client.close()

    def pooled_sessions(self,fail_account=None):
        sessions=[]
        class RoutingSession:
            def __init__(self):
                self.closed=False;self.thread_ids=set();self.calls=[]
                sessions.append(self)
            def mount(self,*args):pass
            def close(self):self.closed=True
            def request(self,method,url,**kwargs):
                self.thread_ids.add(threading.get_ident());self.calls.append((method,url,kwargs))
                self_outer.assertFalse(self.closed)
                self_outer.assertEqual(method,'GET')
                self_outer.assertNotIn('access_token',url)
                time.sleep(.005)
                account=url.split('/act_',1)[1].split('/',1)[0]
                if account==fail_account:raise RuntimeError('synthetic read failure')
                return page([ad(identifier=str(int(account)+100000),account=account)])
        self_outer=self
        return sessions,RoutingSession

    def test_read_workers_reuse_sessions_without_cross_thread_sharing(self):
        sessions,factory=self.pooled_sessions()
        with patch('meta_violation_cleanup.graph.requests.Session',factory):
            client=GraphClient({'1':'synthetic-token'})
            try:
                accounts={str(100+i):{'user_ids':['1']} for i in range(20)}
                results=list(client.iter_account_ads(accounts))
                self.assertEqual({r[0] for r in results},set(accounts))
                self.assertTrue(all(r[1] is not None and r[2]=='1' for r in results))
                readers=[s for s in sessions if s.calls]
                self.assertLessEqual(len(readers),4)
                self.assertGreater(len(readers),1)
                self.assertTrue(any(len(s.calls)>1 for s in readers))
                self.assertTrue(all(len(s.thread_ids)==1 and s.closed for s in readers))
                self.assertFalse(sessions[0].closed)
                self.assertEqual(client.safe_metrics['get_requests'], 20)
            finally:client.close()
        self.assertTrue(all(s.closed for s in sessions))

    def test_worker_sessions_close_when_consumer_stops_early(self):
        sessions,factory=self.pooled_sessions();stop=threading.Event()
        with patch('meta_violation_cleanup.graph.requests.Session',factory):
            client=GraphClient({'1':'synthetic-token'})
            iterator=client.iter_account_ads({str(100+i):{'user_ids':['1']} for i in range(100)},stop.is_set)
            try:
                next(iterator);stop.set()
                with self.assertRaises(InterruptedError):next(iterator)
                self.assertLessEqual(sum(len(s.calls) for s in sessions),5)
                self.assertTrue(all(s.closed for s in sessions[1:]))
            finally:iterator.close();client.close()

    def test_worker_sessions_close_on_unexpected_page_failure(self):
        sessions,factory=self.pooled_sessions(fail_account='100')
        with patch('meta_violation_cleanup.graph.requests.Session',factory):
            client=GraphClient({'1':'synthetic-token'})
            try:
                with self.assertRaises(RuntimeError):
                    list(client.iter_account_ads({str(100+i):{'user_ids':['1']} for i in range(20)}))
                self.assertTrue(all(s.closed for s in sessions[1:]))
                self.assertLessEqual(sum(len(s.calls) for s in sessions),7)
            finally:client.close()

    def test_final_identity_read_blocks_new_policy_rejection(self):
        current=ad()
        current['effective_status']='DISAPPROVED'
        current['ad_review_feedback']={'global':{'Spam':'rejected'}}
        client,session,_=self.client([Response(current)])
        result=client.delete('ad','10','1','100')
        self.assertEqual(result['code'],'current_review_changed')
        self.assertFalse(result['request_sent'])
        self.assertEqual([call[0] for call in session.calls],['GET'])

    def test_stop_cancels_next_page_without_returning_partial_inventory(self):
        client,session,_=self.client([page([ad()], 'cursor')])
        original=session.request
        def request(*args,**kwargs):
            result=original(*args,**kwargs)
            client.stop=lambda:True
            return result
        session.request=request
        with self.assertRaises(InterruptedError):
            client.list_ads('100','1')
        self.assertEqual(len(session.calls),1)

    def test_stop_after_identity_get_does_not_send_delete(self):
        client,session,_=self.client([Response(creative())])
        original=session.request
        def request(*args,**kwargs):
            result=original(*args,**kwargs)
            client.delete_stop=lambda:True
            return result
        session.request=request
        result=client.delete('creative','20','1','100')
        self.assertFalse(result['request_sent'])
        self.assertEqual(result['code'],'cancelled_before_delete')
        self.assertEqual([call[0] for call in session.calls],['GET'])

    def test_already_deleted_identity_is_verified_without_delete_request(self):
        client, session, _ = self.client([Response(creative(status='DELETED'))])
        result = client.delete('creative', '20', '1', '100')
        self.assertEqual(result['status'], 'deleted_verified')
        self.assertTrue(result['confirmed_deleted'])
        self.assertFalse(result['request_sent'])
        self.assertEqual([call[0] for call in session.calls], ['GET'])

    def test_bounded_read_workers_do_not_submit_unbounded_inventory(self):
        client = GraphClient({'1': 'synthetic-token'})
        lock = threading.Lock()
        state = {'active': 0, 'peak': 0}
        def work(item):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(0.02)
            with lock:
                state['active'] -= 1
            return item
        try:
            result = list(client._bounded_reads(work, range(12)))
            self.assertEqual(sorted(result), list(range(12)))
            self.assertLessEqual(state['peak'], 4)
            self.assertGreater(state['peak'], 1)
        finally:
            client.close()

    def test_account_read_fallback_is_readonly_and_keeps_failure_evidence(self):
        session = Session([Response({'error': {'code': 190, 'message': 'expired'}}, 400),
                           page([ad()])])
        client = GraphClient({'1': 'first-token', '2': 'second-token'}, session=session)
        account, ads, user, errors = next(client.iter_account_ads({'100': {'user_ids': ['1', '2']}}))
        self.assertEqual((account, user, len(ads), len(errors)), ('100', '2', 1, 1))
        self.assertTrue(all(call[0] == 'GET' for call in session.calls))

    def client(self, responses, **kwargs):
        session = Session(responses)
        sleeps = []
        client = GraphClient({"1": "secret-token"}, session=session, sleeper=sleeps.append, **kwargs)
        return client, session, sleeps

    def test_accounts_complete_deduplicated_with_all_readable_identities(self):
        session = Session([
            page([{"id": "act_100", "account_id": "100"}], "cursor1", "987654/adaccounts"),
            page([{"id": "act_200", "account_id": "200"}]),
            page([{"id": "act_100", "account_id": "100"}]),
        ])
        client = GraphClient({"1": "secret-token", "2": "second-token"}, session=session)
        self.assertEqual(client.list_accounts(), {
            "accounts": {"100": {"user_ids": ["1", "2"]}, "200": {"user_ids": ["1"]}},
            "errors": [],
        })
        self.assertEqual(session.calls[1][1], "https://graph.facebook.com/v25.0/me/adaccounts")
        self.assertEqual(session.calls[1][2]["params"]["after"], "cursor1")
        for method, url, kwargs in session.calls:
            self.assertEqual(method, "GET")
            self.assertNotIn("access_token", url)
            self.assertNotIn("access_token", kwargs["params"])
            self.assertFalse(kwargs["allow_redirects"])

    def test_account_page_failure_discards_identity_partial_inventory(self):
        session = Session([
            page([{"id": "act_100"}], "cursor1", "me/adaccounts"),
            Response({"error": {"code": 190, "message": "secret-token expired"}}, 400),
            page([{"id": "act_200"}]),
        ])
        client = GraphClient({"1": "secret-token", "2": "second-token"}, session=session)
        result = client.list_accounts()
        self.assertEqual(result["accounts"], {"200": {"user_ids": ["2"]}})
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["user_id"], "1")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_discovery_progress_includes_just_completed_identity(self):
        session = Session([
            page([{"id": "act_100"}]),
            Response({"error": {"code": 190, "message": "expired"}}, 400),
            page([{"id": "act_100"}, {"id": "act_200"}]),
        ])
        client = GraphClient({"1": "first", "2": "second", "3": "third"}, session=session)
        progress = []
        client.on_discovery_progress = progress.append
        result = client.list_accounts()
        self.assertEqual([(p['identities_finished'], p['identities_failed'], p['accounts_discovered'])
                          for p in progress], [(1, 0, 1), (2, 1, 1), (3, 1, 2)])
        self.assertEqual(progress[-1]['accounts_discovered'], len(result['accounts']))
        self.assertEqual(progress[-1]['identities_failed'], len(result['errors']))

    def test_ads_complete_shared_creatives_and_current_status_filter(self):
        client, session, _ = self.client([
            page([ad("10", status="ACTIVE")], "cursor1"),
            page([ad("11", status="PAUSED"), ad("12", status="PENDING_REVIEW"),
                  ad("13", status="DELETED"), ad("14", status="ARCHIVED")]),
        ])
        result = client.list_ads("act_100", "1")
        self.assertEqual([row["id"] for row in result], ["10", "11", "12"])
        self.assertEqual({row["creative"]["id"] for row in result}, {"20"})
        for _, _, kwargs in session.calls:
            self.assertEqual(json.loads(kwargs["params"]["effective_status"]), list(CURRENT_STATUSES))
            self.assertEqual(kwargs["params"]["fields"], AD_FIELDS)
            self.assertNotIn("DELETED", json.loads(kwargs["params"]["effective_status"]))

    def test_filter_rejection_does_not_fallback_to_history(self):
        client, session, _ = self.client([Response({"error": {"code": 100}}, 400)])
        with self.assertRaises(GraphError):
            client.list_ads("100", "1")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("effective_status", session.calls[0][2]["params"])

    def test_page_failure_raises_without_partial_ads(self):
        client, _, _ = self.client([
            page([ad()], "cursor1"), Response({"error": {"code": 200}}, 403),
        ])
        with self.assertRaises(GraphError):
            client.list_ads("100", "1")

    def test_cursor_loop_fails(self):
        client, session, _ = self.client([page([ad()], "cursor1"), page([ad("11")], "cursor1")])
        with self.assertRaises(GraphError) as caught:
            client.list_ads("100", "1")
        self.assertEqual(caught.exception.code, "pagination_loop")
        self.assertEqual(len(session.calls), 2)

    def test_limits_never_return_truncated_success(self):
        for kwargs, responses in [
            ({"max_pages": 1}, [page([ad()], "cursor1")]),
            ({"max_objects": 1}, [page([ad(), ad("11")])]),
        ]:
            with self.subTest(kwargs=kwargs):
                client, _, _ = self.client(responses, **kwargs)
                with self.assertRaises(GraphError) as caught:
                    client.list_ads("100", "1")
                self.assertEqual(caught.exception.code, "inventory_limit")

    def test_invalid_continuation_fails_without_sending_to_new_host(self):
        for next_url in [
            "https://evil.example/v25.0/act_100/ads?after=c",
            "https://graph.facebook.com/v25.0/act_200/ads?after=c",
            "https://graph.facebook.com/v25.0/act_100/ads?before=c",
            "https://graph.facebook.com/v25.0/act_100/ads?after=c&after=d",
            "http://graph.facebook.com/v25.0/act_100/ads?after=c", "", False,
        ]:
            with self.subTest(next_url=next_url):
                client, session, _ = self.client([Response({"data": [ad()], "paging": {"next": next_url}})])
                with self.assertRaises(GraphError):
                    client.list_ads("100", "1")
                self.assertEqual(len(session.calls), 1)

    def test_page_and_cursor_shape_fail_closed(self):
        for payload in [
            {}, {"data": {}}, {"data": [False]}, {"data": [], "paging": []},
            {"data": [], "paging": {"next": "https://graph.facebook.com/v25.0/act_100/ads?after=c"}},
            {"data": [ad()], "paging": {"next": "https://graph.facebook.com/v25.0/act_100/ads?after=c", "cursors": {"after": "different"}}},
        ]:
            with self.subTest(payload=payload):
                client, _, _ = self.client([Response(payload)])
                with self.assertRaises(GraphError):
                    client.list_ads("100", "1")

    def test_inventory_object_account_mismatch_fails(self):
        client, _, _ = self.client([page([ad(account="200")])])
        with self.assertRaises(GraphError) as caught:
            client.list_ads("100", "1")
        self.assertEqual(caught.exception.code, "account_mismatch")

    def test_conflicting_duplicate_inventory_fails(self):
        client, _, _ = self.client([page([ad()], "cursor1"), page([ad(status="ACTIVE")])])
        with self.assertRaises(GraphError) as caught:
            client.list_ads("100", "1")
        self.assertEqual(caught.exception.code, "inventory_changed")

    def test_get_retries_respect_retry_after(self):
        client, session, sleeps = self.client([
            Response({"error": {"code": 4}}, 429, {"Retry-After": "7"}), Response(ad()),
        ])
        self.assertEqual(client.get_ad("10", "1")["id"], "10")
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(session.calls), 2)

    def test_get_large_retry_after_does_not_retry_early(self):
        client, session, sleeps = self.client([
            Response({"error": {"code": 4}}, 429, {"Retry-After": "120"}),
        ])
        with self.assertRaises(GraphError) as caught:
            client.get_ad("10", "1")
        self.assertEqual(caught.exception.detail["retry_after_seconds"], 120)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])

    def test_get_network_retry_is_bounded(self):
        client, session, sleeps = self.client([requests.Timeout(), requests.Timeout(), requests.Timeout()])
        with self.assertRaises(GraphError):
            client.get_ad("10", "1")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_transport_retry_adapter_disabled(self):
        _, session, _ = self.client([])
        self.assertEqual(session.adapters["https://"].max_retries.total, 0)
        self.assertEqual(session.adapters["http://"].max_retries.total, 0)

    def test_delete_single_request_accepts_only_explicit_true(self):
        for payload in [True, {"success": True}]:
            with self.subTest(payload=payload):
                client, session, sleeps = self.client([Response(creative()), Response(payload)])
                result = client.delete("creative", "20", "1", "100")
                self.assertEqual(result["status"], "api_accepted")
                self.assertTrue(result["request_sent"])
                self.assertEqual([call[0] for call in session.calls], ["GET", "DELETE"])
                self.assertEqual(sleeps, [])
                self.assertEqual(session.calls[1][2]["params"], {})

    def test_delete_unknown_outcomes_never_retry(self):
        for response in [
            requests.Timeout("secret-token"), requests.ConnectionError("secret-token"),
            Response({"error": {"code": 2}}, 500), Response({"success": True}, 503),
            Response(ValueError("secret-token")), Response({}), Response({"success": 1}),
            Response(True, 302), Response({"error": {"code": 1}}, 408),
        ]:
            with self.subTest(response=response):
                client, session, sleeps = self.client([Response(ad()), response])
                result = client.delete("ad", "10", "1", "100")
                self.assertEqual(result["status"], "unknown")
                self.assertTrue(result["request_sent"])
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(sleeps, [])
                self.assertNotIn("secret-token", json.dumps(result))

    def test_delete_429_fails_with_cooldown_and_no_retry(self):
        client, session, sleeps = self.client([
            Response(ad()), Response({"error": {"code": 4}}, 429, {"Retry-After": "120"}),
        ])
        result = client.delete("ad", "10", "1", "100")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["response"]["retry_after_seconds"], 120)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [])

    def test_delete_explicit_failure_preserves_safe_diagnostics_only(self):
        client, session, _ = self.client([
            Response(ad()), Response({"error": {
                "code": 100, "error_subcode": 33, "fbtrace_id": "trace",
                "message": "secret-token Bearer other-token access_token=third-token",
                "error_user_title": "Denied secret-token", "error_user_msg": "No permission",
                "error_data": {"access_token": "fourth-token"}, "unknown": "fifth-token",
            }}, 400),
        ])
        result = client.delete("ad", "10", "1", "100")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["response"]["error_subcode"], 33)
        self.assertEqual(result["response"]["fbtrace_id"], "trace")
        for value in ["secret-token", "other-token", "third-token", "fourth-token", "fifth-token"]:
            self.assertNotIn(value, json.dumps(result))
        self.assertEqual(len(session.calls), 2)

    def test_delete_preflight_wrong_id_or_account_prevents_write(self):
        for value in [ad("11"), ad(account="200"), {"id": "10", "status": "PAUSED"}]:
            with self.subTest(value=value):
                client, session, _ = self.client([Response(value)])
                result = client.delete("ad", "10", "1", "100")
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["request_sent"])
                self.assertEqual([call[0] for call in session.calls], ["GET"])

    def test_delete_preflight_permission_failure_does_not_send_write(self):
        client, session, _ = self.client([Response({"error": {"code": 200}}, 403)])
        result = client.delete("creative", "20", "1", "100")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["request_sent"])
        self.assertEqual(len(session.calls), 1)

    def test_delete_false_is_explicit_failure(self):
        for payload in [False, {"success": False}]:
            client, _, _ = self.client([Response(ad()), Response(payload)])
            self.assertEqual(client.delete("ad", "10", "1", "100")["status"], "failed")

    def test_reconcile_requires_exact_id_account_and_deleted_status(self):
        for kind, identifier, value, expected in [
            ("ad", "10", ad(status="DELETED"), "deleted_verified"),
            ("creative", "20", creative(status="DELETED"), "deleted_verified"),
            ("ad", "10", ad(status="PAUSED"), "present"),
            ("ad", "10", ad("11", status="DELETED"), "unknown"),
            ("ad", "10", ad(account="200", status="DELETED"), "unknown"),
            ("creative", "20", {"id": "20", "account_id": "100"}, "unknown"),
        ]:
            with self.subTest(kind=kind, value=value):
                client, session, _ = self.client([Response(value)])
                self.assertEqual(client.reconcile(kind, identifier, "1", "100")["status"], expected)
                self.assertEqual([call[0] for call in session.calls], ["GET"])

    def test_reconcile_permission_or_object_not_found_is_unknown(self):
        for code, subcode, http_status in [(100, 33, 400), (200, 0, 403), (190, 463, 400)]:
            with self.subTest(code=code):
                client, session, _ = self.client([
                    Response({"error": {"code": code, "error_subcode": subcode}}, http_status),
                ])
                self.assertEqual(client.reconcile("ad", "10", "1", "100")["status"], "unknown")
                self.assertEqual(len(session.calls), 1)

    def test_unknown_kind_invalid_id_and_missing_token_do_not_touch_network(self):
        for kind, identifier, uid, account in [
            ("video", "10", "1", "100"), ("ad", "../10", "1", "100"),
            ("ad", "10", "2", "100"), ("ad", "10", "1", "0"),
        ]:
            with self.subTest(kind=kind, identifier=identifier, uid=uid, account=account):
                client, session, _ = self.client([])
                self.assertEqual(client.delete(kind, identifier, uid, account)["status"], "failed")
                self.assertEqual(session.calls, [])

    def test_raw_get_payload_drops_unrequested_keys_without_truncating_policy(self):
        value = ad()
        policy = "long policy " * 1000
        value.update(access_token="hidden", unknown="hidden")
        value["issues_info"] = [{"message": policy, "access_token": "hidden"}]
        client, _, _ = self.client([Response(value)])
        result = client.get_ad("10", "1")
        self.assertEqual(result["issues_info"][0]["message"], policy)
        self.assertNotIn("hidden", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

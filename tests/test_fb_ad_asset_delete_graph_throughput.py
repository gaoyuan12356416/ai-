"""Merged post-DELETE evidence and quota feedback; all HTTP is fake."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from features.fb_ad_asset_delete.graph import GraphClient, GraphError
from test_fb_ad_asset_delete_core_graph import FakeTransport, Response


def video(vid="301", aid="444"):
    return {"kind": "video", "object_id": vid, "account_ids": [aid], "user_ids": ["804"]}


def prepared(aid="444", token="private-token", source="1"):
    return token, {"delete_mode": "ad_account_video", "delete_account_id": aid,
                   "delete_endpoint": "act_" + aid + "/advideos", "credential_kind": "user",
                   "credential_user_id": "804", "credential_source_row_id": source}


def invalid(status=400, message="(#100) Param video_id is not a valid video ID"):
    return Response({"error": {"code": 100, "message": message, "is_transient": False}}, status)


class MergedVerificationTests(unittest.TestCase):
    def client(self, responses, **kwargs):
        transport = FakeTransport(responses)
        return GraphClient(Mock(side_effect=AssertionError("Do not rotate credentials")),
                           transport=transport, **kwargs), transport

    def deferred(self, client, obj=None, selected=None):
        obj, selected = obj or video(), selected or prepared()
        state, result = client.delete_video_account(obj, selected[1]["delete_account_id"],
            prepared=selected, defer_verification=True)
        self.assertEqual((state, result["code"], result["needs_account_verification"]), ("failed", "100", True))
        return obj, selected, result

    def test_two_rejected_deletes_share_one_fresh_paginated_read_and_contexts(self):
        page = Response({"data": [{"id": "900"}], "paging": {"next": "https://private-token.invalid/next", "cursors": {"after": "a"}}})
        client, transport = self.client([invalid(), invalid(), page, Response({"data": [{"id": "901"}]})])
        first = self.deferred(client)
        second = self.deferred(client, video("302"), prepared(source="2"))
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "DELETE"])
        # A pre-delete/cached library must never contribute to this proof.
        client.video_library_cache["444"] = [{"id": "301"}, {"id": "302"}]
        results = client.verify_video_accounts("444", [first, second])
        self.assertEqual([state for state, _ in results], ["already_deleted", "already_deleted"])
        self.assertEqual([result["credential_source_row_id"] for _, result in results], ["1", "2"])
        self.assertEqual([result["proof"]["video_id"] for _, result in results], ["301", "302"])
        self.assertTrue(all(result["proof"]["complete"] and result["proof"]["pages"] == 2 for _, result in results))
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "DELETE", "GET", "GET"])
        self.assertTrue(all(call["headers"]["Authorization"] == "Bearer private-token" for call in transport.calls))
        self.assertEqual(transport.calls[-1]["params"]["after"], "a")
        self.assertNotIn("private-token", json.dumps(results))
        self.assertNotIn("needs_account_verification", json.dumps(results))
        self.assertTrue(first[2]["needs_account_verification"], "Input audit evidence is immutable")

    def test_one_present_video_does_not_abort_read_or_hide_absent_video(self):
        page = Response({"data": [{"id": "301"}], "paging": {"next": "next", "cursors": {"after": "a"}}})
        client, transport = self.client([invalid(), invalid(), page, Response({"data": []})])
        entries = [self.deferred(client), self.deferred(client, video("302"))]
        results = client.verify_video_accounts("444", entries)
        self.assertEqual([state for state, _ in results], ["failed", "already_deleted"])
        self.assertEqual(results[0][1]["code"], "100")
        self.assertEqual(results[0][1]["verification"]["code"], "account_video_present")
        self.assertEqual(results[1][1]["proof"]["pages"], 2)
        self.assertEqual(sum(call["method"] == "GET" for call in transport.calls), 2)

    def test_incomplete_page_or_timeout_preserves_all_explicit_failures(self):
        failures = [requests.Timeout(), Response({}), Response({"data": [{}]}),
                    Response({"data": [], "paging": {"next": "next"}}),
                    Response({"data": [], "paging": []}),
                    Response({"error": {"code": 200, "message": "Denied private-token"}}, 400)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                client, transport = self.client([invalid(), invalid(), failure])
                entries = [self.deferred(client), self.deferred(client, video("302"))]
                results = client.verify_video_accounts("444", entries)
                self.assertTrue(all(state == "failed" and result["code"] == "100" for state, result in results))
                self.assertTrue(all(result["verification"]["status"] == "unknown" for _, result in results))
                self.assertNotIn("confirmed_absent", json.dumps(results))
                self.assertNotIn("private-token", json.dumps(results))
                self.assertEqual(sum(call["method"] == "DELETE" for call in transport.calls), 2)

    def test_partial_read_with_target_present_then_timeout_proves_no_absence(self):
        page = Response({"data": [{"id": "301"}], "paging": {"next": "next", "cursors": {"after": "a"}}})
        client, _ = self.client([invalid(), invalid(), page, requests.Timeout()])
        results = client.verify_video_accounts("444", [self.deferred(client), self.deferred(client, video("302"))])
        self.assertTrue(all(state == "failed" and result["verification"]["code"] == "read_unavailable" for state, result in results))

    def test_mixed_tokens_or_accounts_fail_before_any_get(self):
        for second_obj, second_token in ((video("302"), prepared(token="different-token")),
                                          (video("302", "555"), prepared("555"))):
            with self.subTest(account=second_obj["account_ids"]):
                client, transport = self.client([invalid(), invalid()])
                entries = [self.deferred(client), self.deferred(client, second_obj, second_token)]
                with self.assertRaises(GraphError):
                    client.verify_video_accounts("444", entries)
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "DELETE"])

    def test_changed_original_target_or_context_is_ineligible_without_get(self):
        for key, value in (("video_id", "999"), ("account_id", "555"), ("credential_user_id", "999"),
                            ("needs_account_verification", False), ("message", "Unsupported post request")):
            with self.subTest(key=key):
                client, transport = self.client([invalid()])
                entry = self.deferred(client)
                entry[2][key] = value
                with self.assertRaises(GraphError):
                    client.verify_video_accounts("444", [entry])
                self.assertEqual(len(transport.calls), 1)

    def test_uncertain_and_unrelated_failures_never_become_deferred(self):
        for response in (requests.Timeout(), invalid(503), invalid(message="Unsupported post request"),
                         Response({"success": False}), Response(True)):
            with self.subTest(response=type(response).__name__):
                client, transport = self.client([response])
                _, result = client.delete_video_account(video(), "444", prepared=prepared(), defer_verification=True)
                self.assertNotIn("needs_account_verification", result)
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_forged_server_failure_marker_cannot_pass_batch_guard(self):
        client, transport = self.client([invalid()])
        entry = self.deferred(client)
        entry[2]["detail"]["http_status"] = 503
        with self.assertRaises(GraphError):
            client.verify_video_accounts("444", [entry])
        self.assertEqual(len(transport.calls), 1)

    def test_bounded_reads_do_not_confirm_absence(self):
        client, transport = self.client([invalid(), Response({"data": [{"id": "801"}, {"id": "802"}]})], max_inventory=1)
        entry = self.deferred(client)
        results = client.verify_video_accounts("444", [entry])
        self.assertEqual((results[0][0], results[0][1]["verification"]["code"]), ("failed", "account_video_read_limit"))
        self.assertLessEqual(transport.calls[-1]["timeout"], 5)
        client, transport = self.client([invalid()])
        entry = self.deferred(client)
        with patch("features.fb_ad_asset_delete.graph.time.monotonic", side_effect=[0, 11]):
            results = client.verify_video_accounts("444", [entry])
        self.assertEqual(results[0][1]["verification"]["code"], "account_video_read_limit")
        self.assertEqual(len(transport.calls), 1)

    def test_duplicate_ids_or_cursor_never_confirm_absence(self):
        page = Response({"data": [{"id": "800"}], "paging": {"next": "next", "cursors": {"after": "a"}}})
        for second in (Response({"data": [{"id": "800"}]}),
                       Response({"data": [], "paging": {"next": "next", "cursors": {"after": "a"}}})):
            client, _ = self.client([invalid(), page, second])
            result = client.verify_video_accounts("444", [self.deferred(client)])[0]
            self.assertEqual((result[0], result[1]["verification"]["code"]), ("failed", "account_video_read_incomplete"))

    def test_empty_batch_has_no_http(self):
        client, transport = self.client([])
        self.assertEqual(client.verify_video_accounts("444", []), [])
        self.assertEqual(transport.calls, [])


class QuotaFeedbackTests(unittest.TestCase):
    def test_observer_gets_only_whitelisted_numeric_metrics_and_errors(self):
        response = Response({"error": {"code": 4, "error_subcode": 99, "is_transient": True,
            "message": "Denied private-token", "fbtrace_id": "private-token"}}, 429)
        response.headers = {"x-app-usage": json.dumps({"call_count": 96, "total_time": 98, "token": "private-token"}),
            "x-ad-account-usage": json.dumps({"acc_id_util_pct": 97, "reset_time_duration": 20, "total_time": "private-token"}),
            "x-business-use-case-usage": json.dumps({"private-token": [{"call_count": 99,
                "estimated_time_to_regain_access": 2, "type": "private-token"}]}),
            "retry-after": "12", "authorization": "Bearer private-token", "location": "https://private-token.invalid"}
        events = []
        client = GraphClient(lambda users: "private-token", transport=FakeTransport([response]), response_observer=events.append)
        with self.assertRaises(GraphError):
            client.request("DELETE", "act_444/advideos", "private-token", {"video_id": "301"})
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["account_id"], event["http_status"], event["retry_after_seconds"]), ("444", 429, 12))
        self.assertEqual(event["error"], {"code": 4, "error_subcode": 99, "is_transient": True})
        self.assertEqual(event["usage"]["x-business-use-case-usage"], [{"call_count": 99, "estimated_time_to_regain_access": 2}])
        self.assertNotIn("private-token", json.dumps(events))
        self.assertNotIn("https://", json.dumps(events))

    def test_malformed_json_still_reports_status_and_retry_after_before_raise(self):
        response = Response(ValueError("private-token"), 503)
        response.headers = {"retry-after": "3", "x-app-usage": "not json"}
        events = []
        client = GraphClient(lambda users: "private-token", transport=FakeTransport([response]), response_observer=events.append)
        with self.assertRaises(GraphError):
            client.request("DELETE", "301", "private-token")
        self.assertEqual(events, [{"method": "DELETE", "account_id": "", "http_status": 503,
                                  "usage": {}, "error": {}, "retry_after_seconds": 3}])

    def test_observer_failure_cannot_change_delete_outcome(self):
        client = GraphClient(lambda users: "private-token", transport=FakeTransport([Response(True)]),
            response_observer=Mock(side_effect=RuntimeError("observer failure")))
        self.assertEqual(client.delete_video_account(video(), "444", prepared=prepared())[0], "deleted")

    def test_bad_metric_types_and_unbounded_values_are_dropped(self):
        response = Response(True)
        response.headers = {"x-app-usage": json.dumps({"call_count": True, "total_time": float("nan"),
            "total_cputime": 10**20}), "retry-after": "private-token"}
        events = []
        client = GraphClient(lambda users: "private-token", transport=FakeTransport([response]), response_observer=events.append)
        client.request("GET", "301", "private-token")
        self.assertEqual(events[0]["usage"], {"x-app-usage": {}})
        self.assertNotIn("retry_after_seconds", events[0])

    def test_request_guard_prevents_network_and_keeps_known_not_sent_failure(self):
        transport = FakeTransport([])
        guard = Mock(side_effect=GraphError("task_stopped", "Task stopped"))
        client = GraphClient(lambda users: "private-token", transport=transport, request_guard=guard)
        state, result = client.delete_video_account(video(), "444", prepared=prepared())
        self.assertEqual((state, result["code"]), ("failed", "task_stopped"))
        self.assertEqual(transport.calls, [])
        guard.assert_called_once_with()

    def test_guard_runs_before_every_pagination_request(self):
        transport = FakeTransport([Response({"data": [], "paging": {"next": "next", "cursors": {"after": "a"}}}), Response({"data": []})])
        guard = Mock()
        client = GraphClient(lambda users: "private-token", transport=transport, request_guard=guard)
        state, result = client._read_video_account_absence("301", "444", prepared())
        self.assertEqual(state, "already_deleted")
        self.assertEqual(guard.call_count, 2)

    def test_cooldown_cannot_dispatch_a_get_after_the_scan_budget(self):
        transport = FakeTransport([])
        client = GraphClient(lambda users: "private-token", transport=transport, request_guard=Mock())
        with patch("features.fb_ad_asset_delete.graph.time.monotonic", side_effect=[0, 1, 11]):
            state, result = client._read_video_account_absence("301", "444", prepared(), seconds=10)
        self.assertEqual((state, result["code"]), ("unknown", "account_video_read_limit"))
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()

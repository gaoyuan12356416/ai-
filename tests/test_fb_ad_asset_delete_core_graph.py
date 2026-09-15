"""Contract tests with no real Graph traffic or deletion calls."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

import requests

from features.fb_ad_asset_delete.core import (
    AssetError, actor_key, content_markers, creative_video_ids, normalize_input,
    normalize_phases, parse_ids, redact, stored_ids, stored_ids_complete,
)
from features.fb_ad_asset_delete.graph import GraphClient, GraphError


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if isinstance(self.body, BaseException):
            raise self.body
        return deepcopy(self.body)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(dict(method=method, url=url, **deepcopy(kwargs)))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request; no real transport is available")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def obj(kind="creative", oid="123", accounts=None):
    return dict(kind=kind, object_id=oid, account_ids=accounts or ["444"], user_ids=["803", "804"])


def account_probe():
    return Response({"id": "act_444", "account_id": "444"})


def error_response(code=100, status=400, **extra):
    return Response({"error": dict(code=code, error_subcode=33,
                                  message="Unsupported get request or missing permissions", **extra)}, status)


class CoreTests(unittest.TestCase):
    def test_complete_markers_do_not_match_name_substrings_or_partial_ids(self):
        self.assertEqual(content_markers("launch_contentid[101]_en", "cost_$202@2026-09"), {"101", "202"})
        self.assertEqual(content_markers("CONTENTID[Abc-1.2:en]"), {"Abc-1.2:en"})
        self.assertEqual(content_markers("launch_101_en", "mycontentid[101]", "contentid[101", "contentid101]", "$101"), set())
        self.assertEqual(content_markers("contentid[1010] $10100@"), {"1010", "10100"})
        self.assertNotIn("101", content_markers("contentid[1010] $10100@"))

    def test_input_defaults_and_resource_normalization_have_no_default_product(self):
        self.assertEqual(normalize_input({"ids": "101\n101\n102", "product_ids": ["301"]}),
                         ("content_id", ["101", "102"], ["301"]))
        self.assertEqual(normalize_input({"input_type": "series_code", "ids": ["series-a", "SERIES-A"], "product_ids": ["401"]}),
                         ("series_code", ["SERIES-A"], ["401"]))
        for invalid in ({"ids": ["101"]}, {"input_type": "resource", "ids": ["101"], "product_ids": ["301"]},
                        {"ids": ["101"], "product_ids": ["301 OR 1=1"]}):
            with self.assertRaises(AssetError):
                normalize_input(invalid)

    def test_limits_reject_overflow_without_truncation(self):
        self.assertEqual(len(parse_ids([str(n) for n in range(1, 51)])), 50)
        with self.assertRaises(AssetError) as error:
            normalize_input({"ids": [str(n) for n in range(1, 52)], "product_ids": ["301"]})
        self.assertEqual(error.exception.code, "too_many_ids")
        with self.assertRaises(AssetError):
            normalize_input({"ids": ["101"], "product_ids": [str(n) for n in range(1, 22)]})
        for invalid in ([True], [{}], [["101"]], ["x" * 65], ["101' OR '1'='1"]):
            with self.assertRaises(AssetError):
                parse_ids(invalid)

    def test_phases_have_server_order_and_actor_has_tenant_isolation(self):
        self.assertEqual(normalize_phases(["video", "ad", "creative", "ad"]), ["creative", "ad", "video"])
        self.assertEqual(normalize_phases(["video"]), ["video"])
        for invalid in ([], "ad", ["post"], ["creative", "POST"]):
            with self.assertRaises(AssetError):
                normalize_phases(invalid)
        self.assertNotEqual(actor_key({"tenant_key": "a:b", "user_id": "c"}),
                            actor_key({"tenant_key": "a", "user_id": "b:c"}))
        with self.assertRaises(AssetError) as error:
            actor_key({})
        self.assertEqual(error.exception.status, 401)

    def test_video_ids_read_nested_creative_fields_without_guessing(self):
        creative = {"video_id": "301", "object_story_spec": {"video_data": {"video_id": "302"}},
                    "asset_feed_spec": {"videos": [{"video_id": "303"}, {"video_id": "301"}, {"video_id": "bad"}]},
                    "description": "video_id=999", "id": "888"}
        self.assertEqual(creative_video_ids(creative), ["301", "302", "303"])
        self.assertEqual(stored_ids('["301", "302", "301"]'), ["301", "302"])
        self.assertEqual(stored_ids("301, 302;303"), ["301", "302", "303"])
        self.assertEqual(stored_ids('["301",broken]'), [])
        self.assertEqual(stored_ids("NULL"), [])

    def test_error_redaction_removes_tokens_and_request_urls(self):
        safe = redact("access_token=unit-test-secret https://graph.facebook.com/123?access_token=unit-test-secret")
        self.assertNotIn("unit-test-secret", safe)
        self.assertNotIn("https://", safe)

    def test_stored_id_completeness_distinguishes_empty_from_malformed(self):
        for value in (None, "", "0", [], "[]", '["301","302"]', "301,302", 301):
            with self.subTest(value=value):
                self.assertTrue(stored_ids_complete(value))
        for value in ('["301","bad"]', '["301",broken]', "301,bad", [True], {"id": "301"}):
            with self.subTest(value=value):
                self.assertFalse(stored_ids_complete(value))


class GraphTests(unittest.TestCase):
    def client(self, responses, **kwargs):
        transport = FakeTransport(responses)
        provider = Mock(side_effect=lambda users: "unit-test-token-" + users[0])
        return GraphClient(provider, transport=transport, **kwargs), transport, provider

    def test_each_kind_deletes_the_object_id_and_video_never_unlinks_account_edge(self):
        for kind in ("creative", "ad", "video"):
            with self.subTest(kind=kind):
                client, transport, _ = self.client([account_probe(), Response({"success": True})])
                status, proof = client.delete(obj(kind))
                self.assertEqual(status, "deleted")
                self.assertTrue(proof["success"])
                delete = transport.calls[-1]
                self.assertEqual(delete["method"], "DELETE")
                self.assertEqual(delete["url"], "https://graph.facebook.com/v25.0/123")
                self.assertNotIn("advideos", delete["url"])
                self.assertEqual(delete["params"], {})
                self.assertFalse(delete["allow_redirects"])
                self.assertNotIn("access_token", delete["params"])
                self.assertIn("Authorization", delete["headers"])

    def test_only_explicit_success_counts_as_deleted(self):
        for body, expected in ((True, "deleted"), ({"success": True}, "deleted"),
                               ({"success": "true"}, "unknown"), ({"success": False}, "unknown"),
                               ({"id": "123"}, "unknown"), ([], "unknown"), (None, "unknown")):
            with self.subTest(body=body):
                client, _, _ = self.client([account_probe(), Response(body)])
                self.assertEqual(client.delete(obj())[0], expected)

    def test_permission_and_code100_errors_cannot_be_already_deleted(self):
        for code in (100, 10, 200, 190):
            with self.subTest(code=code):
                client, _, _ = self.client([account_probe(), error_response(code)])
                self.assertEqual(client.delete(obj())[0], "failed")
                client, _, _ = self.client([account_probe(), error_response(code)])
                status, result = client.reconcile(obj())
                self.assertEqual(status, "unknown")
                self.assertNotIn("confirmed_deleted", result)

    def test_delete_timeout_connection_loss_server_error_and_bad_json_are_unknown(self):
        for response in (requests.Timeout(), requests.ConnectionError(), error_response(2, 503),
                         Response(ValueError("not JSON")), Response({}, 302)):
            with self.subTest(response=type(response).__name__):
                client, transport, _ = self.client([account_probe(), response])
                self.assertEqual(client.delete(obj())[0], "unknown")
                self.assertEqual([c["method"] for c in transport.calls], ["GET", "DELETE"])

    def test_no_automatic_token_rotation_after_a_delete_error(self):
        client, transport, provider = self.client([error_response(10), account_probe(), error_response(100)])
        self.assertEqual(client.delete(obj())[0], "failed")
        self.assertEqual([call.args[0] for call in provider.call_args_list], [["803"], ["804"]])
        self.assertEqual([c["method"] for c in transport.calls], ["GET", "GET", "DELETE"])
        self.assertEqual(sum(c["method"] == "DELETE" for c in transport.calls), 1)

    def test_read_timeout_is_unavailable_and_never_deletion_proof(self):
        client, transport, _ = self.client([account_probe(), requests.Timeout()])
        with self.assertRaises(GraphError) as error:
            client.inspect(obj(), check_references=False)
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(error.exception.code, "read_unavailable")
        self.assertTrue(all(call["method"] == "GET" for call in transport.calls))

    def test_confirmed_deleted_requires_the_expected_account(self):
        for account, expected in (("444", "already_deleted"), ("555", "unknown")):
            with self.subTest(account=account):
                client, _, _ = self.client([account_probe(), Response({"id": "123", "account_id": account, "status": "DELETED"})])
                status, result = client.reconcile(obj())
                self.assertEqual(status, expected)
                if expected == "already_deleted":
                    self.assertTrue(result["confirmed_deleted"])
                    self.assertEqual(result["proof"]["id"], "123")

    def test_inventory_follows_complete_cursor_pages_and_never_next_url(self):
        pages = [account_probe(), Response({"data": [{"id": "1"}], "paging": {"next": "https://untrusted.example/steal", "cursors": {"after": "cursor1"}}}),
                 Response({"data": [{"id": "2"}]})]
        client, transport, _ = self.client(pages)
        self.assertEqual(client.inventory(obj(), "444"), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(transport.calls[-1]["params"]["after"], "cursor1")
        self.assertTrue(all(c["url"].startswith("https://graph.facebook.com/") for c in transport.calls))
        client.inventory(obj(), "444")
        self.assertEqual(len(transport.calls), 3)

    def test_missing_or_repeated_cursor_and_partial_read_fail_closed_without_cache(self):
        cases = (
            [Response({"data": [], "paging": {"next": "next"}})],
            [Response({"data": [], "paging": {"next": "next", "cursors": {"after": "again"}}}),
             Response({"data": [], "paging": {"next": "next", "cursors": {"after": "again"}}})],
            [Response({"data": [], "paging": {"next": "next", "cursors": {"after": "cursor"}}}), requests.Timeout()],
            [Response({"unexpected": []})],
        )
        for pages in cases:
            with self.subTest(pages=len(pages)):
                client, _, _ = self.client([account_probe(), *pages])
                with self.assertRaises(GraphError):
                    client.check_references(obj(), set())
                self.assertEqual(client.inventory_cache, {})

    def test_reference_inventory_limit_is_not_silently_truncated(self):
        client, _, _ = self.client([account_probe(), Response({"data": [{"id": "1"}, {"id": "2"}]})], max_inventory=1)
        with self.assertRaises(GraphError) as error:
            client.check_references(obj(), set())
        self.assertEqual(error.exception.code, "reference_check_incomplete")

    def test_outside_scope_creative_or_nested_video_reference_blocks(self):
        for asset in (obj("creative", "123"), obj("video", "777")):
            with self.subTest(kind=asset["kind"]):
                outside = {"id": "999", "status": "ACTIVE", "creative": {"id": "123", "asset_feed_spec": {"videos": [{"video_id": "777"}]}}}
                client, _, _ = self.client([account_probe(), Response({"data": [outside]})])
                with self.assertRaises(GraphError) as error:
                    client.check_references(asset, {"111"})
                self.assertEqual(error.exception.code, "shared_outside_scope")
                client, _, _ = self.client([account_probe(), Response({"data": [outside]})])
                client.check_references(asset, {"999"})

    def test_unreadable_creative_in_outside_ad_prevents_false_no_shared_references(self):
        client, _, _ = self.client([account_probe(), Response({"data": [{"id": "999", "status": "ACTIVE"}]})])
        with self.assertRaises(GraphError) as error:
            client.check_references(obj(), set())
        self.assertEqual(error.exception.code, "reference_check_incomplete")

    def test_video_must_belong_to_a_selected_account_library(self):
        client, transport, _ = self.client([account_probe(), Response({"id": "777", "from": {"id": "888"}}), Response({"data": [{"id": "778"}]})])
        with self.assertRaises(GraphError) as error:
            client.inspect(obj("video", "777"), check_references=False)
        self.assertEqual(error.exception.code, "video_owner_unverified")
        self.assertEqual(transport.calls[-1]["url"], "https://graph.facebook.com/v25.0/act_444/advideos")
        self.assertTrue(all(call["method"] == "GET" for call in transport.calls))

    def test_ad_relationship_change_is_blocked_but_deleted_selected_creative_is_allowed(self):
        asset = dict(obj("ad"), creative_ids=["222"])
        client, _, _ = self.client([account_probe(), Response({"id": "123", "account_id": "444", "creative": {"id": "333"}})])
        with self.assertRaises(GraphError) as error:
            client.inspect(asset, deleted_creatives=["222"])
        self.assertEqual(error.exception.code, "creative_changed")
        client, _, _ = self.client([account_probe(), Response({"id": "123", "account_id": "444", "creative": {}})])
        self.assertEqual(client.inspect(asset, deleted_creatives=["222"])[0], "pending")

    def test_verified_ad_returns_discovered_creative_id_for_preview_freezing(self):
        client, _, _ = self.client([account_probe(), Response({"id": "123", "account_id": "444", "creative": {"id": "222"}})])
        status, proof = client.inspect(dict(obj("ad"), creative_ids=[]))
        self.assertEqual((status, proof["creative_id"]), ("pending", "222"))

    def test_changed_creative_video_relationship_cannot_mutate_a_frozen_preview(self):
        client, transport, _ = self.client([account_probe(), Response({"id": "123", "account_id": "444",
                                                                      "object_story_spec": {"video_data": {"video_id": "302"}}})])
        asset = dict(obj("creative"), verified_video_ids=["301"])
        with self.assertRaises(GraphError) as error:
            client.inspect(asset, check_references=False)
        self.assertEqual(error.exception.code, "creative_video_changed")
        self.assertTrue(all(call["method"] == "GET" for call in transport.calls))

    def test_path_validation_cannot_be_used_to_delete_a_different_edge_or_url(self):
        client, transport, _ = self.client([])
        for path in ("https://attacker.example/123", "123?access_token=x", "123/../444", "act_444/adcreatives", "0"):
            with self.subTest(path=path), self.assertRaises(GraphError):
                client.request("DELETE", path, "unit-test-token")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()

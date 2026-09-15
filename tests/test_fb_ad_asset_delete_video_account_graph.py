"""Account-scoped Video tests. Every SQL/Graph transport is an in-memory fake."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from features.fb_ad_asset_delete import bridge
from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.graph import GraphClient
from features.fb_ad_asset_delete.source import SqlSource, q
from test_fb_ad_asset_delete_core_graph import FakeTransport, Response
from test_fb_ad_asset_delete_video_direct import video


def credential(uid="804", token="source-user-secret", relation="ad_source_user"):
    return dict(token=token, credential_kind="user", credential_user_id=uid,
                credential_fb_user_id="90002", credential_relation=relation)


def source_row(account="444", user="804", row="1", ad="101", product="1"):
    return dict(account_id=account, user_id=user, source_row_id=row, ad_id=ad, product_id=product)


class AccountVideoSourceTests(unittest.TestCase):
    def test_frozen_source_user_precedes_product_default_without_source_reread(self):
        query = Mock(return_value=[("803", "90001", "default-secret"), ("804", "90002", "source-secret")])
        obj = dict(video("pending"), video_account_sources=[source_row()])
        result = SqlSource(query).video_account_credential(obj, "444")
        self.assertEqual(result, credential(token="source-secret"))
        query.assert_called_once()
        sql, timeout = query.call_args.args
        self.assertIn("ads_facebook_info", sql)
        self.assertNotIn("ads_facebook_auto_created_data", sql)
        self.assertNotIn("page", sql)
        self.assertIn("FIELD(CAST(user_id AS CHAR)," + q("804") + "," + q("803") + ")", sql)
        self.assertEqual(timeout, 5)

    def test_each_account_uses_its_own_frozen_source_user(self):
        obj = dict(video("pending"), account_ids=["444", "555"],
                   video_account_sources=[source_row("444", "804"), source_row("555", "803", row="2")])
        query = Mock(return_value=[("803", "90001", "user-A"), ("804", "90002", "user-B")])
        source = SqlSource(query)
        self.assertEqual(source.video_account_credential(obj, "444")["credential_user_id"], "804")
        self.assertEqual(source.video_account_credential(obj, "555")["credential_user_id"], "803")
        self.assertEqual(query.call_count, 2)

    def test_old_preview_recovers_pair_only_from_frozen_ads_products_account_and_users(self):
        query = Mock(side_effect=[[("1", "1", "101", "act_444", "804")],
                                  [("803", "90001", "default-secret"), ("804", "90002", "source-secret")]])
        result = SqlSource(query).video_account_credential(video("failed"), "444")
        self.assertEqual(result["credential_user_id"], "804")
        sql = query.call_args_list[0].args[0]
        self.assertIn("a.ad_id IN (" + q("101") + ")", sql)
        self.assertIn("a.product IN (" + q("1") + ")", sql)
        self.assertIn("a.ad_account_id IN (" + q("444") + "," + q("act_444") + ")", sql)
        self.assertNotIn("status=", sql)
        self.assertNotIn("LIKE", sql)

    def test_recovered_outside_users_and_cross_account_rows_cannot_set_preference(self):
        rows = [("1", "1", "101", "act_555", "804"), ("2", "1", "101", "act_444", "999"),
                ("3", "2", "101", "act_444", "804"), ("4", "1", "102", "act_444", "804")]
        query = Mock(side_effect=[rows, [("803", "90001", "fallback-secret"), ("804", "90002", "other-secret")]])
        result = SqlSource(query).video_account_credential(video("pending"), "444")
        self.assertEqual((result["credential_user_id"], result["credential_relation"]), ("803", "fallback"))

    def test_history_failure_keeps_frozen_user_fallback(self):
        query = Mock(side_effect=[RuntimeError("source unavailable"), [("803", "90001", "fallback-secret")]])
        result = SqlSource(query).video_account_credential(video("pending"), "444")
        self.assertEqual((result["credential_user_id"], result["credential_relation"]), ("803", "fallback"))

    def test_missing_source_token_falls_back_before_single_write_selection(self):
        query = Mock(return_value=[("803", "90001", "fallback-secret")])
        obj = dict(video("pending"), video_account_sources=[source_row()])
        result = SqlSource(query).video_account_credential(obj, "444")
        self.assertEqual((result["credential_user_id"], result["credential_relation"]), ("803", "fallback"))

    def test_user_token_is_reloaded_for_every_account_attempt(self):
        query = Mock(side_effect=[[("804", "90002", "first-secret")], [("804", "90002", "refreshed-secret")]])
        source = SqlSource(query)
        obj = dict(video("pending"), video_account_sources=[source_row()])
        self.assertEqual(source.video_account_credential(obj, "444")["token"], "first-secret")
        self.assertEqual(source.video_account_credential(obj, "444")["token"], "refreshed-secret")
        self.assertEqual(query.call_count, 2)

    def test_outside_account_and_empty_candidates_never_query(self):
        query = Mock()
        source = SqlSource(query)
        with self.assertRaises(AssetError):
            source.video_account_credential(video("pending"), "555")
        self.assertIsNone(source.video_account_credential(dict(video("pending"), user_ids=[]), "444"))
        query.assert_not_called()


class AccountVideoGraphTests(unittest.TestCase):
    def client(self, responses, provider=None, **kwargs):
        transport = FakeTransport(responses)
        account_provider = provider if provider is not None else Mock(return_value=credential())
        client = GraphClient(lambda users: "fallback-user-secret-" + users[0], transport=transport,
            video_account_credential_provider=account_provider,
            video_credential_provider=Mock(side_effect=AssertionError("No Page credential reads")), **kwargs)
        client.inspect = Mock(side_effect=AssertionError("No inspection gate"))
        client.credential = Mock(side_effect=AssertionError("No account GET gate"))
        return client, transport, account_provider

    def test_single_account_endpoint_uses_original_video_param_without_any_get(self):
        client, transport, _ = self.client([Response(True)])
        obj = dict(video("pending"), video_ids=["999"], result={"credential_kind": "page", "credential_page_id": "777"})
        status, result = client.delete_video_account(obj, "444")
        self.assertEqual(status, "deleted")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"], call["params"]),
                         ("DELETE", "https://graph.facebook.com/v25.0/act_444/advideos", {"video_id": "301"}))
        self.assertEqual(call["headers"]["Authorization"], "Bearer source-user-secret")
        self.assertEqual(call["timeout"], 20)
        self.assertFalse(call["allow_redirects"])
        self.assertEqual((result["account_id"], result["video_id"], result["delete_scope"]), ("444", "301", "ad_account_video"))
        self.assertNotIn("source-user-secret", json.dumps(result))

    def test_prepared_credentials_are_reused_without_second_provider_read(self):
        client, transport, provider = self.client([Response({"success": True})])
        obj = video("pending")
        prepared = client.prepare_video_account_delete(obj, "444")
        self.assertEqual(transport.calls, [])
        self.assertEqual(prepared[1]["delete_endpoint"], "act_444/advideos")
        self.assertEqual(prepared[1]["delete_account_id"], "444")
        self.assertEqual(client.delete_video_account(obj, "444", prepared=prepared)[0], "deleted")
        self.assertEqual(provider.call_count, 1)

    def test_different_accounts_receive_different_source_tokens_without_shared_cache(self):
        provider = Mock(side_effect=lambda obj, aid: credential("804" if aid == "444" else "803", "secret-" + aid))
        client, transport, _ = self.client([Response(True), Response(True)], provider)
        obj = dict(video("pending"), account_ids=["444", "555"])
        client.delete_video_account(obj, "444")
        client.delete_video_account(obj, "555")
        self.assertEqual([c["headers"]["Authorization"] for c in transport.calls], ["Bearer secret-444", "Bearer secret-555"])
        self.assertEqual([c["url"] for c in transport.calls],
                         ["https://graph.facebook.com/v25.0/act_444/advideos", "https://graph.facebook.com/v25.0/act_555/advideos"])

    def test_source_failure_falls_back_without_reading_meta_nodes(self):
        client, transport, _ = self.client([Response(True)], Mock(side_effect=RuntimeError("private SQL error")))
        status, result = client.delete_video_account(video("pending"), "444")
        self.assertEqual((status, result["credential_relation"], result["credential_user_id"]), ("deleted", "fallback", "803"))
        self.assertEqual([c["method"] for c in transport.calls], ["DELETE"])
        self.assertNotIn("private SQL", json.dumps(result))

    def test_page_or_outside_user_provider_response_is_ignored_before_write(self):
        for choice in (dict(credential(), credential_kind="page"), credential(uid="999")):
            with self.subTest(choice=choice["credential_kind"]):
                client, transport, _ = self.client([Response(True)], Mock(return_value=choice))
                status, result = client.delete_video_account(video("pending"), "444")
                self.assertEqual((status, result["credential_user_id"]), ("deleted", "803"))
                self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer fallback-user-secret-803")

    def test_permission_failure_never_rotates_credentials_or_rewrites(self):
        error = Response({"error": {"code": 200, "message": "Denied source-user-secret"}}, 400)
        client, transport, provider = self.client([error])
        state, result = client.delete_video_account(video("pending"), "444")
        self.assertEqual((state, result["credential_user_id"]), ("failed", "804"))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(provider.call_count, 1)
        self.assertNotIn("source-user-secret", json.dumps(result))

    def test_timeouts_ambiguous_success_and_server_failure_are_account_unknown(self):
        for response in (requests.Timeout(), requests.ConnectionError(), Response({"success": False}),
                         Response({"error": {"code": 2}}, 503)):
            with self.subTest(response=type(response).__name__):
                client, transport, _ = self.client([response])
                state, result = client.delete_video_account(video("pending"), "444")
                self.assertEqual((state, result["account_id"], result["delete_scope"]), ("unknown", "444", "ad_account_video"))
                self.assertEqual(len(transport.calls), 1)

    def test_outside_account_and_prepared_account_mismatch_issue_no_http(self):
        client, transport, provider = self.client([])
        self.assertEqual(client.delete_video_account(video("pending"), "555")[0], "failed")
        provider.assert_not_called()
        obj = dict(video("pending"), account_ids=["444", "555"])
        prepared = client.prepare_video_account_delete(obj, "444")
        self.assertEqual(client.delete_video_account(obj, "555", prepared=prepared)[0], "failed")
        self.assertEqual(transport.calls, [])

    def test_missing_or_unavailable_token_is_failed_before_any_request(self):
        for lookup in (Mock(return_value=""), Mock(side_effect=RuntimeError("secret SQL details"))):
            with self.subTest(lookup=lookup):
                client, transport, _ = self.client([], Mock(return_value=None))
                client.token_provider = lookup
                state, result = client.delete_video_account(video("pending"), "444")
                self.assertEqual(state, "failed")
                self.assertEqual(transport.calls, [])
                self.assertNotIn("secret SQL", json.dumps(result))


class AccountVideoReconcileTests(unittest.TestCase):
    client = AccountVideoGraphTests.client
    def test_complete_pagination_without_target_proves_only_this_account_absent(self):
        first = Response({"data": [{"id": "800"}], "paging": {"next": "https://untrusted.invalid/next", "cursors": {"after": "cursor-one"}}})
        client, transport, _ = self.client([first, Response({"data": [{"id": "801"}]})])
        state, result = client.reconcile_video_account(video("unknown"), "444")
        self.assertEqual(state, "already_deleted")
        self.assertTrue(result["confirmed_absent"])
        self.assertEqual(result["proof"], {"account_id": "444", "video_id": "301", "complete": True, "pages": 2, "items": 2})
        self.assertEqual(result["delete_scope"], "ad_account_video")
        self.assertTrue(all(call["method"] == "GET" and call["url"] == "https://graph.facebook.com/v25.0/act_444/advideos" for call in transport.calls))
        self.assertEqual(transport.calls[1]["params"]["after"], "cursor-one")
        self.assertNotIn("untrusted.invalid", json.dumps(result))

    def test_target_present_on_second_page_remains_unknown(self):
        first = Response({"data": [], "paging": {"next": "next", "cursors": {"after": "a"}}})
        client, _, _ = self.client([first, Response({"data": [{"id": "301"}]})])
        state, result = client.reconcile_video_account(video("unknown"), "444")
        self.assertEqual((state, result["code"]), ("unknown", "account_video_present"))
        self.assertFalse(result.get("confirmed_absent", False))

    def test_empty_complete_library_is_absent_without_global_video_get(self):
        client, transport, _ = self.client([Response({"data": []})])
        self.assertEqual(client.reconcile_video_account(video("unknown"), "444")[0], "already_deleted")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(transport.calls[0]["url"].endswith("/act_444/advideos"))

    def test_read_errors_timeout_and_malformed_data_cannot_prove_absence(self):
        for response in (Response({"error": {"code": 100, "error_subcode": 33}}, 400), requests.Timeout(),
                         Response({}), Response({"data": [{}]}), Response({"data": [], "paging": []})):
            with self.subTest(response=type(response).__name__):
                client, transport, _ = self.client([response])
                state, result = client.reconcile_video_account(video("unknown"), "444")
                self.assertEqual(state, "unknown")
                self.assertFalse(result.get("confirmed_absent", False))
                self.assertTrue(all(call["method"] == "GET" for call in transport.calls))

    def test_broken_or_repeated_cursor_and_duplicate_ids_keep_unknown(self):
        pages = [
            [Response({"data": [], "paging": {"next": "next"}})],
            [Response({"data": [], "paging": {"next": "next", "cursors": {"after": "a"}}}),
             Response({"data": [], "paging": {"next": "next", "cursors": {"after": "a"}}})],
            [Response({"data": [{"id": "800"}], "paging": {"next": "next", "cursors": {"after": "a"}}}),
             Response({"data": [{"id": "800"}]})],
        ]
        for responses in pages:
            with self.subTest(pages=len(responses)):
                client, _, _ = self.client(responses)
                state, result = client.reconcile_video_account(video("unknown"), "444")
                self.assertEqual(state, "unknown")
                self.assertFalse(result.get("confirmed_absent", False))

    def test_inventory_limit_keeps_unknown(self):
        client, _, _ = self.client([Response({"data": [{"id": "800"}, {"id": "801"}]})], max_inventory=1)
        state, result = client.reconcile_video_account(video("unknown"), "444")
        self.assertEqual((state, result["code"]), ("unknown", "account_video_read_limit"))

    def test_reconcile_time_budget_keeps_unknown_without_late_http(self):
        client, transport, _ = self.client([])
        with patch("features.fb_ad_asset_delete.graph.time.monotonic", side_effect=[0, 31]):
            state, result = client.reconcile_video_account(video("unknown"), "444")
        self.assertEqual((state, result["code"]), ("unknown", "account_video_read_limit"))
        self.assertEqual(transport.calls, [])


class AccountVideoInvalidIdTests(unittest.TestCase):
    client = AccountVideoGraphTests.client

    @staticmethod
    def invalid_id(message="(#100) Param video_id is not a valid video ID", code=100, status=400):
        return Response({"error": dict(code=code, message=message, error_subcode=0,
            type="OAuthException", fbtrace_id="trace-invalid-video",
            error_user_msg="Rejected source-user-secret", is_transient=False)}, status)

    def test_explicit_invalid_id_is_already_deleted_only_after_complete_absence(self):
        client, transport, provider = self.client([self.invalid_id(), Response({"data": []})])
        obj = video("failed")
        prepared = client.prepare_video_account_delete(obj, "444")
        state, result = client.delete_video_account(obj, "444", prepared=prepared)
        self.assertEqual(state, "already_deleted")
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "GET"])
        self.assertTrue(all(call["url"].endswith("/act_444/advideos") for call in transport.calls))
        self.assertEqual(transport.calls[0]["params"], {"video_id": "301"})
        self.assertEqual(transport.calls[1]["params"], {"fields": "id", "limit": "100"})
        self.assertTrue(all(call["headers"]["Authorization"] == "Bearer source-user-secret" for call in transport.calls))
        provider.assert_called_once_with(obj, "444")
        self.assertTrue(result["confirmed_absent"])
        self.assertEqual(result["proof"], dict(account_id="444", video_id="301", complete=True, pages=1, items=0))
        self.assertEqual(result["verification"]["status"], "already_deleted")
        self.assertEqual(result["delete_error"]["code"], "100")
        self.assertEqual(result["delete_error"]["detail"]["fbtrace_id"], "trace-invalid-video")
        self.assertEqual(result["delete_error"]["detail"]["http_status"], 400)
        self.assertNotIn("source-user-secret", json.dumps(result))

    def test_all_pages_keep_the_prepared_token_even_when_provider_changes(self):
        provider = Mock(side_effect=[credential(), credential(token="different-user-secret")])
        first = Response({"data": [{"id": "800"}], "paging": {"next": "https://untrusted.invalid/next", "cursors": {"after": "next-page"}}})
        client, transport, _ = self.client([self.invalid_id(), first, Response({"data": [{"id": "801"}]})], provider)
        state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual(state, "already_deleted")
        self.assertEqual(result["proof"]["pages"], 2)
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "GET", "GET"])
        self.assertEqual(provider.call_count, 1)
        self.assertTrue(all(call["headers"]["Authorization"] == "Bearer source-user-secret" for call in transport.calls))
        self.assertTrue(all(call["url"].endswith("/act_444/advideos") for call in transport.calls))
        self.assertEqual(transport.calls[2]["params"]["after"], "next-page")

    def test_target_on_later_page_keeps_the_original_explicit_delete_failure(self):
        first = Response({"data": [{"id": "800"}], "paging": {"next": "next", "cursors": {"after": "a"}}})
        client, transport, _ = self.client([self.invalid_id(), first, Response({"data": [{"id": "301"}]})])
        state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual((state, result["code"]), ("failed", "100"))
        self.assertEqual(result["verification"]["code"], "account_video_present")
        self.assertEqual(result["delete_error"]["message"], "(#100) Param video_id is not a valid video ID")
        self.assertIn(result["verification"]["message"], result["message"])
        self.assertIn("删除后核实", result["message"])
        self.assertFalse(result.get("confirmed_absent", False))
        self.assertEqual(sum(call["method"] == "DELETE" for call in transport.calls), 1)

    def test_unreadable_or_incomplete_library_never_confirms_the_id_was_deleted(self):
        followups = [Response({"error": {"code": 200, "message": "Missing permission source-user-secret"}}, 400),
            Response({"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request"}}, 400),
            requests.Timeout(), Response({}), Response({"data": [{}]}),
            Response({"data": [], "paging": {"next": "next"}}), Response({"data": [], "paging": []})]
        for followup in followups:
            with self.subTest(followup=type(followup).__name__):
                client, transport, provider = self.client([self.invalid_id(), followup])
                state, result = client.delete_video_account(video("failed"), "444")
                self.assertEqual((state, result["code"]), ("failed", "100"))
                self.assertEqual(result["verification"]["status"], "unknown")
                self.assertFalse(result.get("confirmed_absent", False))
                self.assertFalse(result["verification"].get("confirmed_absent", False))
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "GET"])
                self.assertEqual(provider.call_count, 1)
                self.assertNotIn("source-user-secret", json.dumps(result))

    def test_partial_inventory_followed_by_failure_cannot_prove_absence(self):
        first = Response({"data": [{"id": "800"}], "paging": {"next": "next", "cursors": {"after": "a"}}})
        client, transport, _ = self.client([self.invalid_id(), first, requests.Timeout()])
        state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual(state, "failed")
        self.assertEqual(result["verification"]["code"], "read_unavailable")
        self.assertFalse(result.get("confirmed_absent", False))
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "GET", "GET"])

    def test_inventory_limit_remains_failed_and_bounds_each_read_timeout(self):
        client, transport, _ = self.client([self.invalid_id(), Response({"data": [{"id": "800"}, {"id": "801"}]})], max_inventory=1)
        state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual((state, result["verification"]["code"]), ("failed", "account_video_read_limit"))
        self.assertEqual(transport.calls[0]["timeout"], 20)
        self.assertLessEqual(transport.calls[1]["timeout"], 5)

    def test_post_failure_read_budget_exhaustion_stays_failed_without_late_get(self):
        client, transport, _ = self.client([self.invalid_id()])
        with patch("features.fb_ad_asset_delete.graph.time.monotonic", side_effect=[0, 11]):
            state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual((state, result["verification"]["code"]), ("failed", "account_video_read_limit"))
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_generic_code_100_permissions_and_different_parameter_errors_do_not_get(self):
        errors = [self.invalid_id("Unsupported post request or missing permissions"),
            self.invalid_id("Param ad_id is not a valid video ID"),
            self.invalid_id("Param video_id is not a valid video ID or missing permissions"),
            self.invalid_id(code=200)]
        for error in errors:
            with self.subTest(message=error.body["error"]["message"]):
                client, transport, _ = self.client([error])
                state, result = client.delete_video_account(video("failed"), "444")
                self.assertEqual(state, "failed")
                self.assertNotIn("verification", result)
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_uncertain_delete_with_invalid_id_message_stays_unknown_without_get(self):
        client, transport, _ = self.client([self.invalid_id(status=503)])
        state, result = client.delete_video_account(video("failed"), "444")
        self.assertEqual(state, "unknown")
        self.assertNotIn("verification", result)
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_known_failure_read_does_not_prevent_next_video_request(self):
        client, transport, _ = self.client([self.invalid_id(), requests.Timeout(), Response({"success": True})])
        first = client.delete_video_account(video("failed"), "444")
        second = client.delete_video_account(dict(video("pending"), object_id="302"), "444")
        self.assertEqual((first[0], second[0]), ("failed", "deleted"))
        writes = [call for call in transport.calls if call["method"] == "DELETE"]
        self.assertEqual([call["params"] for call in writes], [{"video_id": "301"}, {"video_id": "302"}])


class AccountVideoBridgeTests(unittest.TestCase):
    def test_production_bridge_wires_new_account_provider(self):
        app = {"MYSQL_HOST": "101.32.56.53", "MYSQL_PORT": "63350", "MYSQL_BASE_CMD": ["fake-mysql"],
               "MYSQL_PASSWORD": "fake-password", "AD_CONTROL_DB_NAME": "kunlunads_dev",
               "ad_control_run_mysql": Mock(return_value=[["1"]])}
        with patch.object(bridge, "_service", None), patch.object(bridge.os.path, "ismount", return_value=True), \
             patch.object(bridge.Path, "mkdir"), patch.object(bridge, "VideoIndex"), patch.object(bridge, "MysqlVideoStream"), \
             patch.object(bridge, "SqlSource") as source, patch.object(bridge, "Store"), \
             patch.object(bridge, "Service") as service, patch.object(bridge, "GraphClient") as graph:
            bridge.get_service(app)
            service.call_args.args[2]()
            self.assertIs(graph.call_args.kwargs["video_account_credential_provider"], source.return_value.video_account_credential)


if __name__ == "__main__":
    unittest.main()

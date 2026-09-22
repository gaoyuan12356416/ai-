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


def credential(uid="804", token="source-user-secret", relation="publish_queue_user"):
    return dict(token=token, credential_kind="user", credential_user_id=uid,
                credential_fb_user_id="90002", credential_relation=relation,
                credential_source_row_id="1", credential_ad_id="101", credential_product_id="1",
                credential_source_user_id=uid if relation == "publish_queue_user" else "804",
                credential_publish_queue_id="500", credential_default_token="-1" if relation == "publish_queue_user" else "1")


def source_row(account="444", user="804", row="1", ad="101", product="1"):
    return dict(account_id=account, user_id=user, source_row_id=row, ad_id=ad, product_id=product)


class AccountVideoSourceTests(unittest.TestCase):
    columns = ("source_row_id", "product_id", "ad_id", "account_id", "user_id", "publish_queue_id",
               "queue_id", "queue_product_id", "queue_user_id", "default_token", "setting_product_id", "default_user",
               "token_user_id", "fb_user_id", "token")

    def row(self, **updates):
        row = dict(source_row(), publish_queue_id="500", queue_id="500", queue_product_id="1",
                   queue_user_id="804", default_token="-1", setting_product_id="1", default_user="803")
        row.update(updates)
        row.setdefault("token_user_id", row["default_user"] if row["default_token"] == "1" else row["queue_user_id"])
        row.setdefault("fb_user_id", "90002")
        row.setdefault("token", "selected-secret")
        return tuple(row[k] for k in self.columns)

    def resolve(self, flag="-1", token_rows=None, obj=None, **row_updates):
        rows = [self.row(default_token=flag, **row_updates)] if token_rows is None else [
            self.row(**dict(row_updates, default_token=flag, token_user_id=uid, fb_user_id=fbid, token=token))
            for uid, fbid, token in token_rows or [(None, None, None)]]
        query = Mock(return_value=rows)
        result = SqlSource(query).video_account_credential(obj or video("pending"), "444")
        return result, query

    def test_default_queue_uses_product_default_instead_of_publishing_user(self):
        result, query = self.resolve("1")
        self.assertEqual((result["credential_user_id"], result["credential_relation"]), ("803", "product_default_user"))
        self.assertEqual(result["credential_source_user_id"], "804")
        self.assertEqual(result["credential_default_token"], "1")
        self.assertIn("WHEN chosen.default_token='1' THEN chosen.default_user", query.call_args.args[0])
        query.assert_called_once()

    def test_nondefault_queue_uses_own_user_even_with_product_default_present(self):
        result, query = self.resolve("-1")
        self.assertEqual((result["credential_user_id"], result["credential_relation"]), ("804", "publish_queue_user"))
        self.assertEqual(result["credential_publish_queue_id"], "500")
        self.assertIn("WHEN chosen.default_token='-1' THEN chosen.queue_user_id", query.call_args.args[0])
        query.assert_called_once()

    def test_frozen_source_still_rereads_queue_flag_and_current_product_default(self):
        obj = dict(video("failed"), video_account_sources=[source_row()])
        result, query = self.resolve("1", [("999", "90009", "current-default")], obj, default_user="999")
        self.assertEqual(result["credential_user_id"], "999")
        sql, timeout = query.call_args_list[0].args
        self.assertIn("ads_template_make_queue pq ON pq.id=a.publish_queue_id", sql)
        self.assertIn("ads_apps_setting p ON p.id=a.product", sql)
        self.assertIn("a.id IN (" + q("1") + ")", sql)
        self.assertEqual(timeout, 5)

    def test_old_preview_recovers_only_exact_account_ad_product_scope(self):
        _, query = self.resolve()
        sql = query.call_args_list[0].args[0]
        for clause in ("a.ad_id IN (" + q("101") + ")", "a.product IN (" + q("1") + ")",
                       "a.ad_account_id IN (" + q("444") + "," + q("act_444") + ")"):
            self.assertIn(clause, sql)
        self.assertNotIn("status=", sql)
        self.assertNotIn("LIKE", sql)
        self.assertNotIn("page", sql)

    def test_outside_source_rows_cannot_authorize_a_token(self):
        for updates in ({"account_id": "555"}, {"user_id": "999"}, {"product_id": "2"}, {"ad_id": "102"}):
            with self.subTest(updates=updates), self.assertRaises(AssetError):
                self.resolve(**updates)

    def test_frozen_row_cannot_change_user_or_row_id(self):
        obj = dict(video("pending"), video_account_sources=[source_row()])
        for updates in ({"user_id": "803", "queue_user_id": "803"}, {"source_row_id": "2"}):
            with self.subTest(updates=updates), self.assertRaises(AssetError):
                self.resolve(obj=obj, **updates)

    def test_missing_or_mismatched_queue_never_authorizes_any_token(self):
        for updates in ({"queue_id": "NULL"}, {"publish_queue_id": "501"},
                        {"queue_product_id": "2"}, {"queue_user_id": "803"}):
            query = Mock(return_value=[self.row(**updates)])
            with self.subTest(updates=updates), self.assertRaises(AssetError):
                SqlSource(query).video_account_credential(video("pending"), "444")
            query.assert_called_once()

    def test_unknown_flags_are_not_coerced_into_a_credential_choice(self):
        for flag in ("0", "NULL", None, "2", "true"):
            with self.subTest(flag=flag), self.assertRaises(AssetError) as caught:
                self.resolve(flag)
            self.assertEqual(caught.exception.code, "publish_token_rule_unknown")

    def test_missing_product_default_never_substitutes_publishing_user(self):
        for updates in ({"default_user": "0"}, {"default_user": "NULL"}, {"setting_product_id": "2"}):
            with self.subTest(updates=updates), self.assertRaises(AssetError) as caught:
                self.resolve("1", **updates)
            self.assertEqual(caught.exception.code, "product_default_token_missing")

    def test_missing_selected_token_never_substitutes_other_frozen_user(self):
        for flag, other in (("1", "804"), ("-1", "803")):
            with self.subTest(flag=flag), self.assertRaises(AssetError) as caught:
                self.resolve(flag, [(other, "90002", "wrong-secret")])
            self.assertEqual(caught.exception.code, "publishing_token_unavailable")
            self.assertEqual(caught.exception.detail["credential_default_token"], flag)
            self.assertNotIn("secret", json.dumps(caught.exception.detail))

    def test_sql_failure_propagates_instead_of_returning_an_arbitrary_candidate(self):
        query = Mock(side_effect=RuntimeError("private database message"))
        with self.assertRaises(AssetError) as caught:
            SqlSource(query).video_account_credential(video("pending"), "444")
        self.assertEqual(caught.exception.code, "source_unavailable")
        query.assert_called_once()

    def test_every_attempt_rereads_queue_and_token_without_cache(self):
        query = Mock(side_effect=[[self.row(token="first")],
                                  [self.row(default_token="1", fb_user_id="90001", token="fresh")]])
        source = SqlSource(query)
        obj = dict(video("pending"), video_account_sources=[source_row()])
        self.assertEqual(source.video_account_credential(obj, "444")["token"], "first")
        self.assertEqual(source.video_account_credential(obj, "444")["token"], "fresh")
        self.assertEqual(query.call_count, 2)

    def test_each_account_resolves_its_own_queue_rule(self):
        obj = dict(video("pending"), account_ids=["444", "555"], video_account_sources=[
            source_row(), source_row("555", "803", row="2")])
        query = Mock(side_effect=[[self.row(token="own")],
            [self.row(source_row_id="2", account_id="555", user_id="803", queue_user_id="803",
                default_token="1", default_user="999", fb_user_id="90009", token="default")]])
        source = SqlSource(query)
        self.assertEqual(source.video_account_credential(obj, "444")["credential_user_id"], "804")
        self.assertEqual(source.video_account_credential(obj, "555")["credential_user_id"], "999")

    def test_outside_account_or_incomplete_scope_never_queries(self):
        query = Mock()
        source = SqlSource(query)
        for obj, aid in ((video("pending"), "555"), (dict(video("pending"), user_ids=[]), "444")):
            with self.assertRaises(AssetError):
                source.video_account_credential(obj, aid)
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

    def test_source_failure_does_not_use_a_different_frozen_user(self):
        client, transport, _ = self.client([Response(True)], Mock(side_effect=RuntimeError("private SQL error")))
        client.token_provider = Mock(return_value="wrong-token")
        status, result = client.delete_video_account(video("pending"), "444")
        self.assertEqual((status, result["code"]), ("failed", "credential_read_unavailable"))
        self.assertEqual(transport.calls, [])
        client.token_provider.assert_not_called()
        self.assertNotIn("private SQL", json.dumps(result))

    def test_page_or_outside_user_provider_response_fails_without_write(self):
        for choice in (dict(credential(), credential_kind="page"), credential(uid="999")):
            with self.subTest(choice=choice["credential_kind"]):
                client, transport, _ = self.client([Response(True)], Mock(return_value=choice))
                status, result = client.delete_video_account(video("pending"), "444")
                self.assertEqual((status, result["code"]), ("failed", "publishing_token_unavailable"))
                self.assertEqual(transport.calls, [])

    def test_current_product_default_can_differ_from_frozen_default_user(self):
        client, transport, _ = self.client([Response(True)], Mock(return_value=credential("999", relation="product_default_user")))
        status, result = client.delete_video_account(video("pending"), "444")
        self.assertEqual((status, result["credential_user_id"], result["credential_default_token"]), ("deleted", "999", "1"))
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer source-user-secret")

    def test_default_route_requires_matching_queue_product_ad_and_source_user(self):
        for key, value in (("credential_product_id", "2"), ("credential_ad_id", "102"),
                           ("credential_source_user_id", "999"), ("credential_publish_queue_id", ""),
                           ("credential_default_token", "-1"), ("credential_source_row_id", "")):
            with self.subTest(key=key):
                choice = dict(credential("999", relation="product_default_user"), **{key: value})
                client, transport, _ = self.client([], Mock(return_value=choice))
                self.assertEqual(client.delete_video_account(video("pending"), "444")[0], "failed")
                self.assertEqual(transport.calls, [])

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

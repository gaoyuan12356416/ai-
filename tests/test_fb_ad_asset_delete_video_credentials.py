"""Credential routing tests: fake Graph transports and temporary SQLite only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from features.fb_ad_asset_delete import bridge
from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.graph import GraphClient, VIDEO_CREDENTIAL_MAX_CREATIVES
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.source import SqlSource, q
from features.fb_ad_asset_delete.store import Store
from test_fb_ad_asset_delete_core_graph import FakeTransport, Response, account_probe
from test_fb_ad_asset_delete_service import SESSION, FakeSource, QueuedWorkers
from test_fb_ad_asset_delete_video_direct import video, job_data


def page_credential(page="91001", uid="803", fbid="90001", row="11", token=None):
    return dict(token=token or "test-page-secret-" + page, credential_kind="page",
                credential_page_id=page, credential_row_id=row,
                credential_user_id=uid, credential_fb_user_id=fbid)


def creative(cid="201", vid="301", page="91001", **extra):
    return dict(id=cid, object_story_spec={"page_id": page, "video_data": {"video_id": vid}}, **extra)


def from_response(vid="301", identity="91001"):
    return Response({"id": vid, "from": {"id": identity}})


class VideoCredentialSourceTests(unittest.TestCase):
    def test_page_token_joins_meta_identity_and_limits_internal_candidates(self):
        query = Mock(return_value=[("804", "90002", "12", "91001", "90002", " page-secret ")])
        source = SqlSource(query)
        result = source.video_credential(["803", "804"], "91001", "creative_page")
        self.assertEqual(result, page_credential(uid="804", fbid="90002", row="12", token="page-secret"))
        sql, timeout = query.call_args.args
        self.assertIn("CAST(p.fb_user_id AS BINARY)=CAST(f.facebookUserID AS BINARY)", sql)
        self.assertIn("f.user_id IN (" + q("803") + "," + q("804") + ")", sql)
        self.assertIn("p.page_id=" + q("91001"), sql)
        self.assertIn("p.status<>1", sql)
        self.assertIn("TRIM(p.page_access_token)<>''", sql)
        self.assertLessEqual(timeout, 5)
        self.assertTrue(sql.startswith("SELECT "))
        self.assertNotIn("ads_facebook_page_group", sql)

    def test_page_selection_is_deterministic_by_candidate_then_row(self):
        query = Mock(return_value=[("803", "90001", "11", "91001", "90001", "later-user"),
                                   ("804", "90002", "30", "91001", "90002", "later-row"),
                                   ("804", "90002", "12", "91001", "90002", "selected")])
        result = SqlSource(query).video_credential(["804", "803"], "91001", "creative_page")
        self.assertEqual((result["credential_user_id"], result["credential_row_id"], result["token"]),
                         ("804", "12", "selected"))

    def test_page_token_and_status_are_read_again_without_cached_candidates(self):
        query = Mock(side_effect=[[("803", "90001", "11", "91001", "90001", "first-secret")],
                                  [], [("803", "90001", "11", "91001", "90001", "fresh-secret")]])
        source = SqlSource(query)
        self.assertEqual(source.video_credential(["803"], "91001", "creative_page")["token"], "first-secret")
        self.assertIsNone(source.video_credential(["803"], "91001", "creative_page"))
        self.assertEqual(source.video_credential(["803"], "91001", "creative_page")["token"], "fresh-secret")
        self.assertEqual(query.call_count, 3)

    def test_outside_user_wrong_page_or_inconsistent_meta_join_cannot_select_token(self):
        for row in (("999", "90001", "11", "91001", "90001", "outside-secret"),
                    ("803", "90001", "11", "91002", "90001", "wrong-page-secret"),
                    ("803", "90001", "11", "91001", "90002", "bad-join-secret"),
                    ("803", "90001", "11", "91001", "90001", "   ")):
            with self.subTest(row=row[:5]):
                query = Mock(return_value=[row])
                self.assertIsNone(SqlSource(query).video_credential(["803"], "91001", "creative_page"))
                self.assertEqual(query.call_count, 1)

    def test_video_from_prefers_matching_page_token_before_person_token(self):
        query = Mock(return_value=[("803", "90001", "11", "91001", "90001", "page-secret")])
        self.assertEqual(SqlSource(query).video_credential(["803"], "91001", "video_from")["credential_kind"], "page")
        self.assertEqual(query.call_count, 1)

    def test_video_from_can_select_exact_upload_user_in_frozen_candidates(self):
        query = Mock(side_effect=[[], [("804", "90002", "uploader-secret")]])
        result = SqlSource(query).video_credential(["803", "804"], "90002", "video_from")
        self.assertEqual(result, dict(token="uploader-secret", credential_kind="user",
                                     credential_user_id="804", credential_fb_user_id="90002"))
        sql = query.call_args.args[0]
        self.assertIn("facebookUserID=" + q("90002"), sql)
        self.assertIn("user_id IN (" + q("803") + "," + q("804") + ")", sql)
        self.assertEqual(query.call_count, 2)

    def test_from_user_lookup_rejects_internal_id_coincidence_and_outside_user(self):
        for row in (("804", "90003", "wrong-person"), ("90002", "90002", "outside-user")):
            with self.subTest(row=row[:2]):
                source = SqlSource(Mock(side_effect=[[], [row]]))
                self.assertIsNone(source.video_credential(["803", "804"], "90002", "video_from"))

    def test_invalid_identity_relation_or_candidates_never_queries(self):
        query = Mock()
        source = SqlSource(query)
        for users, identity, relation in (([], "91001", "video_from"), (["bad"], "91001", "video_from"),
                                          (["803"], "91001 OR 1=1", "video_from"),
                                          (["803"], "91001", "permalink")):
            self.assertIsNone(source.video_credential(users, identity, relation))
        query.assert_not_called()


class VideoCredentialGraphTests(unittest.TestCase):
    def client(self, responses, credential=None, **kwargs):
        transport = FakeTransport(responses)
        token_provider = Mock(side_effect=lambda users: "test-user-secret-" + users[0])
        page_provider = credential if callable(credential) else Mock(return_value=credential)
        client = GraphClient(token_provider, transport=transport, video_credential_provider=page_provider, **kwargs)
        return client, transport, page_provider

    @staticmethod
    def deletes(transport):
        return [call for call in transport.calls if call["method"] == "DELETE"]

    def assert_direct_target(self, transport, vid="301", token="test-user-secret-803"):
        calls = self.deletes(transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://graph.facebook.com/v25.0/" + vid)
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer " + token)
        self.assertFalse(any("act_" in call["url"] or "/advideos" in call["url"] for call in transport.calls))
        self.assertEqual(calls[0]["timeout"], 20)

    def test_video_from_selects_page_token_and_never_reads_creative(self):
        client, transport, provider = self.client([from_response(), Response(True)], page_credential())
        status, result = client.delete(video("pending"))
        self.assertEqual(status, "deleted")
        self.assertEqual((result["credential_kind"], result["credential_relation"], result["credential_user_id"]),
                         ("page", "video_from", "803"))
        provider.assert_called_once_with(["803", "804"], "91001", "video_from")
        self.assertEqual(len(transport.calls), 2)
        self.assert_direct_target(transport, token="test-page-secret-91001")

    def test_from_selects_exact_upload_user_without_any_account_probe(self):
        user = dict(token="uploader-secret", credential_kind="user", credential_user_id="804", credential_fb_user_id="90002")
        client, transport, _ = self.client([from_response(identity="90002"), Response(True)], user)
        status, result = client.delete(video("pending"))
        self.assertEqual((status, result["credential_user_id"], result["credential_relation"]), ("deleted", "804", "video_from"))
        self.assert_direct_target(transport, token="uploader-secret")

    def test_creative_video_relation_selects_associated_page_without_replacing_video(self):
        relation = creative(effective_object_story_id="91001_77777")
        client, transport, provider = self.client([Response({"id": "301", "permalink_url": "/99999/videos/301/"}),
                                                  Response(relation), Response(True)], page_credential())
        status, result = client.delete(video("pending"))
        self.assertEqual((status, result["credential_relation"]), ("deleted", "creative_page"))
        self.assertIn("不代表已确认上传者", result["credential_lookup_message"])
        provider.assert_called_once_with(["803", "804"], "91001", "creative_page")
        self.assert_direct_target(transport, token="test-page-secret-91001")
        self.assertFalse(any("77777" in call["url"] or "99999" in call["url"] for call in transport.calls))

    def test_asset_feed_exact_video_is_accepted_but_substring_video_is_not(self):
        for relation, matches in ((dict(id="201", object_story_spec={"page_id": "91001"},
                                        asset_feed_spec={"videos": [{"video_id": "301"}]}), True),
                                  (creative(vid="3010"), False)):
            with self.subTest(matches=matches):
                client, transport, provider = self.client([Response({"id": "301"}), Response(relation), Response(True)], page_credential())
                self.assertEqual(client.delete(video("pending"))[0], "deleted")
                self.assertEqual(provider.call_count, 1 if matches else 0)
                self.assert_direct_target(transport, token="test-page-secret-91001" if matches else "test-user-secret-803")

    def test_different_pages_with_same_user_ids_never_share_page_token(self):
        provider = Mock(side_effect=lambda users, identity, relation: page_credential(page=identity))
        client, transport, _ = self.client([from_response(), Response(True), from_response("302", "91002"), Response(True)], provider)
        client.delete(video("pending"))
        other = dict(video("pending"), key="video:302", object_id="302")
        client.delete(other)
        self.assertEqual([call["headers"]["Authorization"] for call in self.deletes(transport)],
                         ["Bearer test-page-secret-91001", "Bearer test-page-secret-91002"])
        self.assertEqual(provider.call_count, 2)

    def test_same_page_token_is_reloaded_even_with_cached_creative_relation(self):
        provider = Mock(side_effect=[page_credential(token="first-page-secret"), page_credential(token="new-page-secret")])
        relation = creative(asset_feed_spec={"videos": [{"video_id": "302"}]})
        client, transport, _ = self.client([Response({"id": "301"}), Response(relation), Response(True),
                                           Response({"id": "302"}), Response(True)], provider)
        client.delete(video("pending"))
        client.delete(dict(video("pending"), key="video:302", object_id="302"))
        self.assertEqual([call["headers"]["Authorization"] for call in self.deletes(transport)],
                         ["Bearer first-page-secret", "Bearer new-page-secret"])
        self.assertEqual(sum(call["method"] == "GET" and call["url"].endswith("/201") for call in transport.calls), 1)
        self.assertEqual(provider.call_count, 2)

    def test_creative_read_in_earlier_phase_can_supply_relation_after_creative_deleted(self):
        client, transport, _ = self.client([account_probe(), Response(creative(account_id="444")),
                                           Response({"id": "301"}), Response(True)], page_credential())
        client.read_node(dict(video(), kind="creative", object_id="201"), "id,account_id,object_story_spec,video_id")
        self.assertEqual(client.delete(video("pending"))[0], "deleted")
        self.assertEqual(sum(call["url"].endswith("/201") for call in transport.calls), 1)
        self.assertEqual(self.deletes(transport)[0]["headers"]["Authorization"], "Bearer test-page-secret-91001")

    def test_unreadable_video_and_creative_still_issue_one_user_delete(self):
        for failure in (Response({"error": {"code": 200, "message": "No permission"}}, 400),
                        requests.Timeout(), requests.ConnectionError()):
            with self.subTest(failure=type(failure).__name__):
                client, transport, provider = self.client([failure, failure, Response(True)], page_credential())
                status, result = client.delete(video("pending"))
                self.assertEqual((status, result["credential_lookup"]), ("deleted", "fallback"))
                provider.assert_not_called()
                self.assert_direct_target(transport)
                self.assertTrue(all(call["timeout"] <= 3 for call in transport.calls if call["method"] == "GET"))

    def test_absent_page_token_or_sql_failure_falls_back_without_exposing_exception(self):
        for provider in (Mock(return_value=None), Mock(side_effect=RuntimeError("secret=DO_NOT_LEAK"))):
            with self.subTest(provider=provider):
                client, transport, _ = self.client([Response({"id": "301"}), Response(creative()), Response(True)], provider)
                status, result = client.delete(video("pending"))
                self.assertEqual((status, result["credential_kind"]), ("deleted", "user"))
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
                self.assert_direct_target(transport)

    def test_nonblocking_provider_rejects_outside_candidate_wrong_identity_and_secret_metadata(self):
        bad = [page_credential(uid="999"), page_credential(page="91002"),
               dict(token="outside-secret", credential_kind="user", credential_user_id="804", credential_fb_user_id="91002")]
        for credential in bad:
            with self.subTest(kind=credential["credential_kind"]):
                client, transport, _ = self.client([from_response(), Response(True)], credential)
                obj = dict(video("pending"), creative_ids=[])
                self.assertEqual(client.delete(obj)[0], "deleted")
                self.assert_direct_target(transport)
        credential = dict(page_credential(), password="DO_NOT_LEAK", access_token="DO_NOT_LEAK")
        client, _, _ = self.client([from_response(), Response(True)], credential)
        self.assertNotIn("DO_NOT_LEAK", json.dumps(client.delete(video("pending"))[1]))

    def test_mismatched_get_object_or_malformed_relation_never_selects_page(self):
        for data in (creative(cid="202"), dict(id="201", object_story_spec="not-a-map", asset_feed_spec="malformed")):
            with self.subTest(data=data["id"]):
                client, transport, provider = self.client([Response({"id": "301"}), Response(data), Response(True)], page_credential())
                self.assertEqual(client.delete(video("pending"))[0], "deleted")
                provider.assert_not_called()
                self.assert_direct_target(transport)

    def test_identity_work_is_bounded_and_delete_timeout_is_unchanged(self):
        count = VIDEO_CREDENTIAL_MAX_CREATIVES
        rows = [Response({"id": "301"})] + [Response(creative(cid=str(201+n), vid="999")) for n in range(count)] + [Response(True)]
        client, transport, provider = self.client(rows, page_credential())
        obj = dict(video("pending"), creative_ids=[str(201+n) for n in range(count+3)])
        status, result = client.delete(obj)
        self.assertEqual(status, "deleted")
        self.assertEqual(len(transport.calls), count+2)
        self.assertIn("数量上限", result["credential_lookup_message"])
        self.assert_direct_target(transport)
        provider.assert_not_called()

    def test_expired_optional_budget_skips_identity_reads_but_not_delete(self):
        client, transport, _ = self.client([Response(True)], page_credential())
        with patch("features.fb_ad_asset_delete.graph.time.monotonic", side_effect=[0, 11, 11]):
            self.assertEqual(client.delete(video("pending"))[0], "deleted")
        self.assertEqual(len(transport.calls), 1)
        self.assert_direct_target(transport)

    def test_prepared_credential_is_reused_once_without_requery_or_extra_get(self):
        client, transport, provider = self.client([from_response(), Response(True)], page_credential())
        obj = video("pending")
        prepared = client.prepare_video_delete(obj)
        before = deepcopy(prepared[1])
        self.assertNotIn("test-page-secret", json.dumps(prepared[1]))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(client.delete(obj, prepared=prepared)[0], "deleted")
        self.assertEqual(prepared[1], before)
        self.assertEqual(provider.call_count, 1)
        self.assert_direct_target(transport, token="test-page-secret-91001")

    def test_previous_page_attempt_can_retry_with_fresh_page_token_without_user_token(self):
        client, transport, provider = self.client([Response(True)], page_credential(token="fresh-page-secret"))
        client.token_provider = Mock(return_value="")
        obj = dict(video("failed"), result={"credential_kind": "page", "credential_page_id": "91001",
                                           "credential_relation": "creative_page"})
        status, result = client.delete(obj)
        self.assertEqual((status, result["credential_relation"]), ("deleted", "creative_page"))
        client.token_provider.assert_not_called()
        provider.assert_called_once_with(["803", "804"], "91001", "creative_page")
        self.assertEqual(len(transport.calls), 1)
        self.assert_direct_target(transport, token="fresh-page-secret")

    def test_invalid_previous_page_relation_is_not_used_and_new_page_field_is_ignored(self):
        client, transport, provider = self.client([Response({"id": "301"}), Response(True)], page_credential())
        obj = dict(video("failed"), creative_ids=[], credential_page_id="91001",
                   result={"credential_kind": "page", "credential_page_id": "91001", "credential_relation": "permalink"})
        self.assertEqual(client.delete(obj)[0], "deleted")
        provider.assert_not_called()
        self.assert_direct_target(transport)

    def test_previous_page_missing_current_token_falls_back_to_user(self):
        client, transport, provider = self.client([Response({"id": "301"}), Response(True)], None)
        obj = dict(video("failed"), creative_ids=[], result={"credential_kind": "page", "credential_page_id": "91001",
                                                           "credential_relation": "creative_page"})
        status, result = client.delete(obj)
        self.assertEqual((status, result["credential_kind"]), ("deleted", "user"))
        provider.assert_called_once_with(["803", "804"], "91001", "creative_page")
        self.assert_direct_target(transport)

    def test_delete_permission_failure_never_rotates_or_falls_back_to_user(self):
        failure = Response({"error": {"code": 200, "message": "Rejected test-page-secret-91001"}}, 400)
        client, transport, provider = self.client([from_response(), failure], page_credential())
        status, result = client.delete(video("pending"))
        self.assertEqual((status, result["credential_kind"], result["credential_relation"]), ("failed", "page", "video_from"))
        self.assertEqual(provider.call_count, 1)
        self.assertNotIn("test-page-secret", json.dumps(result))
        self.assert_direct_target(transport, token="test-page-secret-91001")

    def test_delete_timeout_ambiguous_response_and_server_failure_remain_unknown(self):
        for response in (requests.Timeout(), Response({"success": False}), Response({"error": {"code": 2}}, 503)):
            with self.subTest(response=type(response).__name__):
                client, transport, provider = self.client([from_response(), response], page_credential())
                status, result = client.delete(video("pending"))
                self.assertEqual((status, result["credential_kind"]), ("unknown", "page"))
                self.assertEqual(provider.call_count, 1)
                self.assert_direct_target(transport, token="test-page-secret-91001")

    def test_reconcile_with_page_token_remains_read_only_and_does_not_prove_deletion(self):
        for response in (Response({"id": "301", "status": "DELETED"}),
                         Response({"error": {"code": 100, "error_subcode": 33}}, 400), requests.Timeout()):
            with self.subTest(response=type(response).__name__):
                client, transport, _ = self.client([from_response(), response], page_credential())
                status, result = client.reconcile(video("unknown"))
                self.assertEqual(status, "unknown")
                self.assertFalse(result.get("confirmed_deleted", False))
                self.assertTrue(all(call["method"] == "GET" for call in transport.calls))
                self.assertEqual(transport.calls[-1]["headers"]["Authorization"], "Bearer test-page-secret-91001")


class VideoCredentialIntegrationTests(unittest.TestCase):
    def test_current_service_uses_account_user_and_never_replays_unknown_or_success(self):
        for response, expected in ((requests.Timeout(), "unknown"), (Response(True), "deleted")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / "ledger.sqlite3")
                job = job_data(status="failed")
                store.create_job(job)
                transport = FakeTransport([response])
                provider = Mock(return_value=page_credential())
                graph = GraphClient(lambda users: "test-user-secret", transport=transport, video_credential_provider=provider)
                workers = QueuedWorkers()
                service = Service(store, FakeSource(), lambda: graph, spawn=workers, authorize=lambda s: s)
                payload = {"preview_id": job["preview_id"], "request_id": "credential_run_one", "phases": ["video"]}
                service.execute(SESSION, job["job_id"], payload)
                workers.run_next()
                stored = store.get_job(job["job_id"])
                self.assertEqual(stored["objects"][0]["status"], expected)
                child = stored["objects"][0]["video_account_results"][0]
                self.assertEqual(child["result"]["credential_kind"], "user")
                self.assertEqual(child["result"]["delete_mode"], "ad_account_video")
                self.assertNotIn("test-page-secret", json.dumps(stored))
                service.execute(SESSION, job["job_id"], dict(payload, request_id="credential_run_two"))
                workers.run_next()
                self.assertEqual(sum(call["method"] == "DELETE" for call in transport.calls), 1)
                self.assertEqual(provider.call_count, 0)

    def test_sql_query_failure_does_not_stop_direct_deletion(self):
        source = SqlSource(Mock(side_effect=RuntimeError("SQL service unavailable")))
        transport = FakeTransport([Response({"id": "301"}), Response(creative()), Response(True)])
        graph = GraphClient(lambda users: "test-user-secret", transport=transport, video_credential_provider=source.video_credential)
        state, result = graph.delete(video("pending"))
        self.assertEqual((state, result["credential_kind"], result["credential_lookup"]), ("deleted", "user", "fallback"))
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET", "DELETE"])

    def test_production_bridge_passes_source_video_provider_to_execution_graph(self):
        app = {"MYSQL_HOST": "101.32.56.53", "MYSQL_PORT": "63350", "MYSQL_BASE_CMD": ["fake-mysql"],
               "MYSQL_PASSWORD": "fake-password", "AD_CONTROL_DB_NAME": "kunlunads_dev",
               "ad_control_run_mysql": Mock(return_value=[["1"]])}
        with patch.object(bridge, "_service", None), patch.object(bridge.os.path, "ismount", return_value=True), \
             patch.object(bridge.Path, "mkdir"), patch.object(bridge, "VideoIndex"), patch.object(bridge, "MysqlVideoStream"), \
             patch.object(bridge, "SqlSource") as source, patch.object(bridge, "Store"), \
             patch.object(bridge, "Service") as service, patch.object(bridge, "GraphClient") as graph:
            bridge.get_service(app)
            service.call_args.args[2]()
            self.assertIs(graph.call_args.kwargs["video_credential_provider"], source.return_value.video_credential)


if __name__ == "__main__":
    unittest.main()

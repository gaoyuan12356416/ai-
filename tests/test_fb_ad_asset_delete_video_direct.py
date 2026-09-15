"""Direct deletion of frozen Video IDs, using fake Graph and temporary ledgers only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import uuid

import requests

from features.fb_ad_asset_delete.core import AssetError, actor_key, stored_video_ids
from features.fb_ad_asset_delete.graph import GraphClient, GraphError
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.store import Store, StoreError
from test_fb_ad_asset_delete_core_graph import FakeTransport, Response
from test_fb_ad_asset_delete_service import SESSION, PRODUCT, DRAMA, FakeSource, FakeGraph, GraphState, QueuedWorkers
from test_fb_ad_asset_delete_source import FixtureSource, ad as source_ad, drama as source_drama, product as source_product


def video(status="blocked", code="video_reference_unverified"):
    return dict(key="video:301", kind="video", object_id="301", status=status,
                result=dict(code=code, message="Historical preflight blocked this Video"),
                product_ids=["1"], account_ids=["444"], user_ids=["803", "804"], ad_ids=["101"],
                creative_ids=["201"], video_ids=["301"], content_ids=["100"], series_codes=["SERIES-A"], languages=["en"])


def job_data(name="job-one", status="blocked", code="video_reference_unverified"):
    return dict(job_id=name, preview_id="preview-" + name, actor=actor_key(SESSION), status="ready",
                input_type="content_id", ids=["100"], products=[deepcopy(PRODUCT)], dramas=[deepcopy(DRAMA)],
                blockers=[], objects=[video(status, code)])


class DirectVideoGraphTests(unittest.TestCase):
    def client(self, responses, provider=None):
        transport = FakeTransport(responses)
        provider = provider or Mock(side_effect=lambda users: "unit-test-token-" + users[0])
        return GraphClient(provider, transport=transport), transport, provider

    def test_delete_targets_video_node_without_account_or_video_get(self):
        client, transport, _ = self.client([Response({"success": True})])
        status, result = client.delete(video("pending"))
        self.assertEqual(status, "deleted")
        self.assertTrue(result["success"])
        self.assertEqual(result["credential_user_id"], "803")
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])
        self.assertEqual(transport.calls[0]["url"], "https://graph.facebook.com/v25.0/301")
        self.assertEqual(transport.calls[0]["params"], {})
        self.assertNotIn("advideos", transport.calls[0]["url"])
        self.assertNotIn("access_token", transport.calls[0]["params"])

    def test_missing_account_metadata_is_not_a_read_gate_for_a_valid_frozen_video(self):
        client, transport, _ = self.client([Response(True)])
        target = video("pending")
        target["account_ids"] = []
        self.assertEqual(client.delete(target)[0], "deleted")
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_delete_permission_failure_is_failed_and_never_rotates_token(self):
        response = Response({"error": dict(code=200, error_subcode=1487235, type="OAuthException",
                                            message="Permissions error", is_transient=False)}, 400)
        client, transport, provider = self.client([response])
        status, result = client.delete(video())
        self.assertEqual(status, "failed")
        self.assertEqual(result["code"], "200")
        self.assertEqual(result["detail"]["error_subcode"], 1487235)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_timeout_or_ambiguous_response_remains_unknown_without_read_or_retry(self):
        for response in (requests.Timeout(), requests.ConnectionError(), Response({"success": False}),
                         Response({"error": {"code": 2, "message": "Unavailable"}}, 503)):
            with self.subTest(response=type(response).__name__):
                client, transport, provider = self.client([response])
                self.assertEqual(client.delete(video())[0], "unknown")
                self.assertEqual(provider.call_count, 1)
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_absent_candidate_token_can_be_skipped_before_first_and_only_delete(self):
        provider = Mock(side_effect=lambda users: "unit-test-token-804" if users == ["804"] else "")
        client, transport, _ = self.client([Response({"success": True})], provider)
        self.assertEqual(client.delete(video())[0], "deleted")
        self.assertEqual([call.args[0] for call in provider.call_args_list], [["803"], ["804"]])
        self.assertEqual([call["method"] for call in transport.calls], ["DELETE"])

    def test_without_any_token_no_meta_request_is_sent(self):
        client, transport, _ = self.client([], Mock(return_value=""))
        try:
            status, result = client.delete(video())
        except AssetError as error:
            self.assertEqual(error.code, "missing_token")
        else:
            self.assertEqual(status, "failed")
            self.assertEqual(result["code"], "missing_token")
        self.assertEqual(transport.calls, [])

    def test_reconcile_video_is_one_direct_get_and_missing_or_deleted_fields_are_not_deletion_proof(self):
        for response in (Response({"id": "301"}), Response({"id": "301", "status": "DELETED"}),
                         Response({"error": {"code": 100, "error_subcode": 33, "message": "Object missing or unreadable"}}, 400),
                         Response({"error": {"code": 200, "message": "Permissions error"}}, 400), requests.Timeout()):
            with self.subTest(response=type(response).__name__):
                client, transport, _ = self.client([response])
                status, result = client.reconcile(video("unknown"))
                self.assertEqual(status, "unknown")
                self.assertFalse(result.get("confirmed_deleted", False))
                self.assertEqual([call["method"] for call in transport.calls], ["GET"])
                self.assertEqual(transport.calls[0]["url"], "https://graph.facebook.com/v25.0/301")


class DirectVideoCandidateTests(unittest.TestCase):
    def test_full_length_source_csv_does_not_turn_a_truncated_tail_into_a_video_id(self):
        raw = ("301," + "123456789012345," * 40)[:512]
        tail = raw.rsplit(",", 1)[-1]
        source = FixtureSource(ads=[source_ad(video_ids_raw=raw)])
        rows, _ = source.resolve_ads([source_drama()], [source_product()])
        self.assertIn("301", rows[0]["video_ids"])
        self.assertIn("123456789012345", rows[0]["video_ids"])
        self.assertNotIn(tail, rows[0]["video_ids"])

    def test_complete_capacity_length_csv_json_and_short_scalar_ids_are_retained(self):
        csv = "1," * 255 + "2,"
        encoded = json.dumps(["1"] * 126 + ["1234"], separators=(",", ":"))
        self.assertEqual((len(csv), len(encoded)), (512, 512))
        self.assertEqual(stored_video_ids(csv), ["1", "2"])
        self.assertEqual(stored_video_ids(encoded), ["1", "1234"])
        self.assertEqual(stored_video_ids("1,23"), ["1", "23"])


class DirectVideoStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = Store(self.path)

    def claim(self, name="job-one", request_id="request-one"):
        return self.store.claim_run(name, "preview-" + name, actor_key(SESSION), ["video"], request_id)

    def test_all_historical_video_blocker_types_can_be_claimed_without_an_exception_flag(self):
        for n, code in enumerate(("video_reference_unverified", "shared_outside_scope", "video_owner_unverified",
                                  "video_index_expired", "read_unavailable", "source_ownership_conflict")):
            with self.subTest(code=code):
                name = "job-" + str(n)
                self.store.create_job(job_data(name, code=code))
                run = self.claim(name, "request-" + str(n))
                outcome = self.store.claim_object(name, "video:301", run["run_id"])
                self.assertTrue(outcome["claimed"])
                self.store.finish_object(name, "video:301", run["run_id"], "failed", dict(code="200"))
                self.store.finish_run(run["run_id"], "completed")

    def test_failed_video_remains_manually_retryable(self):
        self.store.create_job(job_data(status="failed"))
        run = self.claim()
        self.assertTrue(self.store.claim_object("job-one", "video:301", run["run_id"])["claimed"])

    def test_direct_video_cannot_override_unknown_fence_from_another_task(self):
        self.store.create_job(job_data())
        first = self.claim()
        self.assertTrue(self.store.claim_object("job-one", "video:301", first["run_id"])["claimed"])
        self.store.finish_object("job-one", "video:301", first["run_id"], "unknown", dict(code="network_uncertain"))
        self.store.finish_run(first["run_id"], "completed")
        self.store.create_job(job_data("job-two"))
        second = self.claim("job-two", "request-two")
        outcome = self.store.claim_object("job-two", "video:301", second["run_id"])
        self.assertFalse(outcome["claimed"])
        self.assertEqual(outcome["status"], "unknown")

    def test_historical_blocked_video_cannot_bypass_another_active_object_claim(self):
        self.store.create_job(job_data())
        self.store.create_job(job_data("job-two"))
        first = self.claim()
        second = self.claim("job-two", "request-two")
        self.assertTrue(self.store.claim_object("job-one", "video:301", first["run_id"])["claimed"])
        outcome = self.store.claim_object("job-two", "video:301", second["run_id"])
        self.assertFalse(outcome["claimed"])
        self.assertEqual(outcome["reason"], "object_locked")

    def test_success_receipt_skips_the_same_video_in_another_historical_task(self):
        self.store.create_job(job_data())
        first = self.claim()
        self.assertTrue(self.store.claim_object("job-one", "video:301", first["run_id"])["claimed"])
        self.store.finish_object("job-one", "video:301", first["run_id"], "deleted", dict(success=True))
        self.store.finish_run(first["run_id"], "completed")
        self.store.create_job(job_data("job-two"))
        second = self.claim("job-two", "request-two")
        outcome = self.store.claim_object("job-two", "video:301", second["run_id"])
        self.assertFalse(outcome["claimed"])
        self.assertEqual(outcome["status"], "already_deleted")

    def test_same_task_success_and_unknown_are_skipped(self):
        for state in ("deleted", "already_deleted", "unknown"):
            with self.subTest(state=state):
                name = "job-" + state
                self.store.create_job(job_data(name, state))
                run = self.claim(name, "request-" + state)
                self.assertFalse(self.store.claim_object(name, "video:301", run["run_id"])["claimed"])

    def test_blocked_creative_or_ad_still_cannot_be_claimed(self):
        for kind in ("creative", "ad"):
            with self.subTest(kind=kind):
                name = "job-" + kind
                payload = job_data(name)
                payload["objects"][0].update(kind=kind, key=kind + ":301")
                self.store.create_job(payload)
                run = self.store.claim_run(name, "preview-" + name, actor_key(SESSION), [kind], "request-" + kind)
                self.assertFalse(self.store.claim_object(name, kind + ":301", run["run_id"])["claimed"])


class DirectVideoServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = Store(self.path)
        self.source = FakeSource()
        self.source.shared_references = Mock(wraps=self.source.shared_references)
        self.source.validate_reference_snapshot = Mock(wraps=self.source.validate_reference_snapshot)
        self.state = GraphState()
        self.workers = QueuedWorkers()
        self.graph = FakeGraph(self.state)
        self.graph.inspect = Mock(wraps=self.graph.inspect)
        self.module_allowed = True
        def authorize(session):
            if not self.module_allowed:
                raise AssetError("permission_denied", "Module access revoked", 403)
            return deepcopy(session)
        self.service = Service(self.store, self.source, lambda: self.graph, spawn=self.workers, authorize=authorize)

    def preview(self):
        response = self.service.preview(SESSION, dict(ids=["100"], product_ids=["1"]))
        self.workers.run_next()
        self.state.events.clear()
        return self.store.get_job(response["job_id"])

    def execute(self, job, phases=None, extra=None, request_id=None, run=True):
        body = dict(preview_id=job["preview_id"], request_id=request_id or uuid.uuid4().hex, phases=phases or ["video"])
        body.update(extra or {})
        result = self.service.execute(SESSION, job["job_id"], body)
        if run and not result["duplicate"]:
            self.workers.run_next()
        return result

    def test_historical_blocked_video_executes_without_source_references_or_any_graph_preflight(self):
        job = self.store.create_job(job_data())
        self.source.shared_references.side_effect = AssetError("source_unavailable", "Reference query unavailable")
        self.source.validate_reference_snapshot.side_effect = AssetError("video_index_expired", "Index expired")
        self.graph.inspect.side_effect = GraphError("200", "Account or Video GET is forbidden")
        self.execute(job)
        self.assertEqual(self.state.deleted_keys(), ["video:301"])
        self.source.shared_references.assert_not_called()
        self.source.validate_reference_snapshot.assert_not_called()
        self.graph.inspect.assert_not_called()

    def test_source_video_ids_survive_creative_or_ad_get_failure_in_preview(self):
        self.state.inspect_errors["ad:101"] = GraphError("200", "Account is disabled")
        self.state.inspect_errors["creative:201"] = GraphError("200", "Creative cannot be read")
        self.source.reference_error = AssetError("source_unavailable", "History cannot be checked")
        job = self.preview()
        target = next(obj for obj in job["objects"] if obj["kind"] == "video")
        self.assertEqual(target["object_id"], "301")
        self.assertEqual(target["status"], "pending")
        self.assertEqual(target["product_ids"], ["1"])
        self.execute(job)
        self.assertEqual(self.state.deleted_keys(), ["video:301"])

    def test_preview_keeps_both_source_and_discovered_creative_video_ids(self):
        self.state.creative_videos["201"] = ["302"]
        job = self.preview()
        self.assertEqual({o["object_id"] for o in job["objects"] if o["kind"] == "video"}, {"301", "302"})
        self.assertTrue(all(o["status"] == "pending" for o in job["objects"] if o["kind"] == "video"))
        self.execute(job)
        self.assertEqual(set(self.state.deleted_keys()), {"video:301", "video:302"})

    def test_source_identity_warning_keeps_ad_blocked_without_expanding_video_product_scope(self):
        self.source.ads[0]["reason"] = "Source identity needs review"
        job = self.preview()
        by_kind = {o["kind"]: o for o in job["objects"]}
        self.assertEqual(by_kind["ad"]["status"], "blocked")
        self.assertEqual(by_kind["video"]["status"], "pending")
        self.assertEqual(by_kind["video"]["product_ids"], ["1"])
        self.execute(job, ["creative", "ad", "video"])
        self.assertEqual(self.state.deleted_keys(), ["video:301"])

    def test_original_phase_order_and_creative_ad_preflight_are_preserved(self):
        job = self.preview()
        self.graph.inspect.reset_mock()
        self.source.shared_references.reset_mock()
        self.execute(job, ["video", "ad", "creative"])
        self.assertEqual(self.state.deleted_keys(), ["creative:201", "ad:101", "video:301"])
        self.assertEqual([call.args[0]["kind"] for call in self.graph.inspect.call_args_list], ["creative", "ad"])
        self.assertTrue(all(all(o["kind"] != "video" for o in call.args[0]) for call in self.source.shared_references.call_args_list))

    def test_delete_failure_does_not_stop_other_videos_and_timeout_is_not_retried(self):
        payload = job_data()
        other = video("pending")
        other.update(key="video:302", object_id="302", video_ids=["302"])
        payload["objects"].append(other)
        job = self.store.create_job(payload)
        self.state.delete_outcomes["video:301"] = ("unknown", dict(code="network_uncertain"))
        self.state.delete_outcomes["video:302"] = ("failed", dict(code="200", message="Permissions error"))
        self.execute(job)
        self.assertEqual(self.state.deleted_keys(), ["video:301", "video:302"])
        self.state.events.clear()
        self.execute(job)
        self.assertEqual(self.state.deleted_keys(), ["video:302"])

    def test_login_module_product_permissions_and_frozen_object_scope_are_preserved(self):
        job = self.store.create_job(job_data())
        self.source.allowed = False
        with self.assertRaises(AssetError):
            self.execute(job, run=False)
        self.source.allowed = True
        for extra in ({"video_ids": ["999"]}, {"object_ids": ["999"]}, {"objects": [dict(object_id="999")]},
                      {"preview_id": "different-preview"}):
            with self.subTest(extra=extra), self.assertRaises((AssetError, StoreError)):
                self.execute(job, extra=extra, run=False)
        with self.assertRaises(AssetError):
            self.service.execute({}, job["job_id"], dict(preview_id=job["preview_id"], request_id=uuid.uuid4().hex, phases=["video"]))
        # A claim made while authorized must still obey worker-side permission refresh.
        result = self.execute(job, run=False)
        self.module_allowed = False
        self.workers.run_next()
        self.assertEqual(self.state.deleted_keys(), [])
        self.assertEqual(self.store.get_job(job["job_id"])["runs"][0]["run_id"], result["run_id"])

    def test_duplicate_request_id_does_not_enqueue_or_delete_again(self):
        job = self.store.create_job(job_data())
        request_id = uuid.uuid4().hex
        first = self.execute(job, request_id=request_id)
        second = self.execute(job, request_id=request_id)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.state.deleted_keys(), ["video:301"])
        self.assertEqual(self.workers.pending, [])

    def test_detail_exposes_historical_video_eligibility_without_reclassifying_other_states(self):
        payload = job_data()
        for number, state in ((302, "unknown"), (303, "deleted"), (304, "pending"), (305, "failed")):
            other = video(state)
            other.update(key="video:" + str(number), object_id=str(number))
            payload["objects"].append(other)
        job = self.store.create_job(payload)
        detail = self.service.detail(SESSION, job["job_id"])
        self.assertEqual(detail["video_direct_eligible_count"], 1)
        flags = {o["key"]: o.get("video_direct_eligible", False) for o in detail["objects"]}
        self.assertTrue(flags["video:301"])
        self.assertFalse(any(flags[key] for key in flags if key != "video:301"))


if __name__ == "__main__":
    unittest.main()

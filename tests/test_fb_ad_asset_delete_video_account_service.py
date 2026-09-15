"""Account-scoped Video orchestration uses SQLite and mock Graph only."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from features.fb_ad_asset_delete.core import PHASES
from features.fb_ad_asset_delete.graph import GraphClient
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.store import Store, StoreError
from test_fb_ad_asset_delete_service import FakeSource, FakeGraph, GraphState, QueuedWorkers, SESSION, ad


class AccountGraph(FakeGraph):
    def __init__(self, state, store):
        super().__init__(state)
        self.store = store

    def prepare_video_account_delete(self, obj, aid):
        return "secret-for-" + aid, dict(delete_mode="ad_account_video", delete_account_id=aid,
            delete_endpoint="act_" + aid + "/advideos", credential_kind="user",
            credential_user_id="803", credential_relation="ad_source_user")

    def delete_video_account(self, obj, aid, prepared=None):
        rows = self.store.video_account_results(self.state.job_id, obj["key"])
        row = next(item for item in rows if item["account_id"] == aid)
        assert row["status"] == "in_progress"
        assert prepared[0] == "secret-for-" + aid
        assert row["result"]["credential_user_id"] == prepared[1]["credential_user_id"]
        self.state.events.append(("DELETE_ACCOUNT", aid, obj["object_id"]))
        self.state.after_delete(dict(obj, delete_account_id=aid))
        outcome = self.state.account_outcomes.get((aid, obj["object_id"]), ("deleted", {"success": True}))
        if isinstance(outcome, Exception):
            raise outcome
        return deepcopy(outcome)

    def reconcile_video_account(self, obj, aid):
        self.state.events.append(("READ_ACCOUNT", aid, obj["object_id"]))
        return "already_deleted", dict(delete_mode="ad_account_video", delete_scope="ad_account_video", account_id=aid,
            video_id=obj["object_id"], confirmed_absent=True,
            proof={"account_id": aid, "video_id": obj["object_id"], "complete": True}, checked_at="2026-09-15T22:00:00Z")


class VideoAccountServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "ledger.sqlite3")
        self.source, self.state, self.workers = FakeSource(), GraphState(), QueuedWorkers()
        self.state.account_outcomes = {}
        self.service = Service(self.store, self.source, lambda: AccountGraph(self.state, self.store), spawn=self.workers)

    def preview(self, accounts=("444", "555")):
        self.source.ads = [dict(ad(index=i + 1, videos=["301"]), account_id=aid, row_id=str(i + 7)) for i, aid in enumerate(accounts)]
        self.state.creative_videos = {str(201 + i): ["301"] for i in range(len(accounts))}
        result = self.service.preview(SESSION, {"input_type": "content_id", "ids": ["100"], "product_ids": ["1"]})
        self.workers.run_next()
        self.state.job_id = result["job_id"]
        self.state.events.clear()
        return self.store.get_job(result["job_id"])

    def execute(self, job, phases=("video",), request_id=None):
        result = self.service.execute(SESSION, job["job_id"], dict(preview_id=job["preview_id"],
            phases=list(phases), request_id=request_id or uuid.uuid4().hex))
        if not result["duplicate"]:
            self.workers.run_next()
        return self.store.get_job(job["job_id"])

    def video(self, job):
        return next(obj for obj in job["objects"] if obj["kind"] == "video")

    def writes(self):
        return [event for event in self.state.events if event[0] == "DELETE_ACCOUNT"]

    def test_preview_keeps_exact_source_account_pairs_on_shared_video(self):
        obj = self.video(self.preview())
        self.assertEqual(obj["account_ids"], ["444", "555"])
        self.assertEqual([(item["account_id"], item["source_row_id"], item["user_id"]) for item in obj["video_account_sources"]],
                         [("444", "7", "803"), ("555", "8", "803")])

    def test_each_account_has_prewrite_audit_and_failures_continue(self):
        job = self.preview()
        self.state.account_outcomes[("444", "301")] = ("failed", {"code": "200", "message": "Denied"})
        final = self.execute(job)
        self.assertEqual(self.writes(), [("DELETE_ACCOUNT", "444", "301"), ("DELETE_ACCOUNT", "555", "301")])
        self.assertEqual(self.video(final)["status"], "failed")
        self.assertEqual([x["status"] for x in self.video(final)["video_account_results"]], ["failed", "deleted"])
        self.assertNotIn(b"secret-for", Path(self.store.path).read_bytes())

    def test_retry_skips_successful_account_and_request_id_is_idempotent(self):
        job = self.preview()
        self.state.account_outcomes[("444", "301")] = ("failed", {"code": "200"})
        self.execute(job)
        self.state.account_outcomes.clear()
        request_id = uuid.uuid4().hex
        final = self.execute(job, request_id=request_id)
        self.execute(job, request_id=request_id)
        self.assertEqual(self.writes(), [("DELETE_ACCOUNT", "444", "301"), ("DELETE_ACCOUNT", "555", "301"), ("DELETE_ACCOUNT", "444", "301")])
        self.assertEqual(self.video(final)["status"], "deleted")

    def test_account_a_receipt_never_skips_account_b_in_new_task(self):
        self.execute(self.preview(("444",)))
        later = self.preview(("555",))
        final = self.execute(later)
        self.assertEqual(self.writes(), [("DELETE_ACCOUNT", "555", "301")])
        self.assertEqual(self.video(final)["status"], "deleted")

    def test_unknown_account_continues_others_and_get_reconcile_is_scoped(self):
        job = self.preview()
        self.state.account_outcomes[("444", "301")] = ("unknown", {"code": "timeout"})
        final = self.execute(job)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.video(final)["status"], "unknown")
        self.execute(job)
        self.assertEqual(len(self.writes()), 2)
        self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
        self.assertEqual(len(self.writes()), 2)
        self.assertIn(("READ_ACCOUNT", "444", "301"), self.state.events)
        self.assertEqual(self.video(self.store.get_job(job["job_id"]))["status"], "deleted")

    def test_reconciled_unknown_leaves_other_failure_for_manual_retry(self):
        job = self.preview()
        self.state.account_outcomes[("444", "301")] = ("unknown", {"code": "timeout"})
        self.state.account_outcomes[("555", "301")] = ("failed", {"code": "200"})
        self.execute(job)
        self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
        self.assertEqual(self.video(self.store.get_job(job["job_id"]))["status"], "failed")
        self.state.account_outcomes.clear()
        self.execute(job)
        self.assertEqual(self.writes()[-1], ("DELETE_ACCOUNT", "555", "301"))
        self.assertEqual(len(self.writes()), 3)

    def test_credential_audit_failure_stops_all_new_deletes(self):
        job = self.preview()
        with patch.object(self.store, "claim_video_account", side_effect=StoreError("disk full", "ledger_error")):
            final = self.execute(job)
        self.assertEqual(self.writes(), [])
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(self.video(final)["status"], "failed")

    def test_permission_revoked_between_accounts_preserves_original_error(self):
        job = self.preview()
        self.state.after_delete = lambda obj: setattr(self.source, "allowed", False)
        final = self.execute(job)
        self.assertEqual(self.writes(), [("DELETE_ACCOUNT", "444", "301")])
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(final["runs"][-1]["summary"]["code"], "product_permission_denied")
        self.assertEqual([item["status"] for item in self.video(final)["video_account_results"]], ["deleted", "pending"])

    def test_phase_order_remains_creative_ad_video(self):
        job = self.preview(("444",))
        self.execute(job, tuple(reversed(PHASES)))
        writes = [event for event in self.state.events if event[0] in ("DELETE", "DELETE_ACCOUNT")]
        self.assertEqual(writes, [("DELETE", "creative:201"), ("DELETE", "ad:101"), ("DELETE_ACCOUNT", "444", "301")])

    def test_public_detail_includes_safe_account_progress(self):
        job = self.preview(("444",))
        self.execute(job)
        public = self.service.detail(SESSION, job["job_id"], kind="video")
        obj = public["objects"][0]
        self.assertEqual(obj["video_account_results"][0]["account_id"], "444")
        self.assertNotIn("video_account_sources", obj)
        self.assertNotIn("secret-for", str(public))

    def test_real_graph_adapter_and_ledger_share_the_audited_target(self):
        job = self.preview(("444",))
        events = []
        store = self.store

        class Transport:
            def request(inner, method, url, **kwargs):
                row = store.video_account_results(job["job_id"], "video:301")[0]
                self.assertEqual(row["status"], "in_progress")
                self.assertEqual(row["result"]["delete_endpoint"], "act_444/advideos")
                self.assertEqual(kwargs["params"], {"video_id": "301"})
                events.append((method, url))
                return type("Response", (), {"status_code": 200, "json": lambda inner: {"success": True}})()

        self.service.graph_factory = lambda: GraphClient(lambda users: "published-user-token", transport=Transport(),
            video_account_credential_provider=lambda obj, aid: dict(token="published-user-token", credential_kind="user",
                credential_user_id="803", credential_relation="ad_source_user"))
        final = self.execute(job)
        self.assertEqual(events, [("DELETE", "https://graph.facebook.com/v25.0/act_444/advideos")])
        self.assertEqual(self.video(final)["status"], "deleted")
        self.assertNotIn(b"published-user-token", Path(self.store.path).read_bytes())


if __name__ == "__main__":
    unittest.main()

"""Durable rejected DELETEs remain distinct from unknown in-flight requests."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features.fb_ad_asset_delete.store import Store, StoreError


MODE = "ad_account_video"
KEY = "video:900"


class DeferredVideoStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.store = Store(self.path)
        self.store.create_job(dict(job_id="job", preview_id="preview", actor="tenant:user",
            input_type="content_id", ids=["123"], products=[{"id": "456"}], status="ready",
            objects=[dict(key=KEY, kind="video", object_id="900", status="pending",
                          account_ids=["101", "202", "303", "404"])]))
        self.run = self.store.claim_run("job", "preview", "tenant:user", ["video"], "first")["run_id"]
        self.store.claim_object("job", KEY, self.run)
        self.store.begin_video_accounts("job", KEY, self.run)

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(query, parameters).fetchall()

    @staticmethod
    def context(account="101"):
        return dict(delete_mode=MODE, delete_account_id=account, delete_endpoint="act_" + account + "/advideos",
            credential_kind="user", credential_user_id="12", credential_relation="publish_queue_user",
            credential_publish_queue_id="789", credential_default_token="-1")

    def claim(self, account="101"):
        return self.store.claim_video_account("job", KEY, self.run, account, self.context(account))["account_attempt_id"]

    def rejection(self, account="101"):
        error = dict(code="100", message="(#100) Param video_id is not a valid video ID",
            detail=dict(code=100, http_status=400, fbtrace_id="trace-rejected"), checked_at="2026-09-22T03:00:00Z")
        return dict(self.context(account), account_id=account, video_id="900", delete_scope=MODE,
                    **error, delete_error=deepcopy(error), needs_account_verification=True)

    def defer(self, account="101", result=None):
        attempt = self.claim(account)
        result = result or self.rejection(account)
        self.store.defer_video_account_verification("job", KEY, self.run, account, attempt, result)
        return attempt, result

    def states(self):
        return {row["account_id"]: row["status"] for row in self.store.video_account_results("job", KEY)}

    def proof(self, original):
        return dict(self.context(), account_id="101", video_id="900", delete_scope=MODE, confirmed_absent=True,
            proof=dict(account_id="101", video_id="900", complete=True), delete_error=original["delete_error"])

    def test_defer_is_durable_and_does_not_release_parent_or_create_receipt(self):
        attempt, original = self.defer()
        fresh = Store(self.path)
        child = fresh.video_account_results("job", KEY)[0]
        self.assertEqual((child["status"], child["result"]), ("in_progress", original))
        saved = self.sql("SELECT status,result FROM fb_asset_delete_v2_video_account_attempts WHERE account_attempt_id=?", (attempt,))[0]
        self.assertEqual((saved[0], json.loads(saved[1])), ("in_progress", original))
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("in_progress",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])
        self.assertEqual(fresh.defer_video_account_verification("job", KEY, self.run, "101", attempt, original), child)
        changed = deepcopy(original)
        changed["detail"]["fbtrace_id"] = "other"
        changed["delete_error"]["detail"]["fbtrace_id"] = "other"
        with self.assertRaisesRegex(StoreError, "immutable"):
            fresh.defer_video_account_verification("job", KEY, self.run, "101", attempt, changed)

    def test_crash_distinguishes_rejected_inflight_unsent_and_success(self):
        self.defer("101")
        self.claim("202")
        attempt = self.claim("303")
        self.store.finish_video_account("job", KEY, self.run, "303", attempt, "deleted",
            dict(self.context("303"), account_id="303", video_id="900", delete_scope=MODE, success=True))
        counts = Store(self.path).recover_interrupted(lambda pid, start: False)
        self.assertEqual(counts["unknown_objects"], 1)
        self.assertEqual(self.states(), {"101": "failed", "202": "unknown", "303": "deleted", "404": "pending"})
        rejected = self.store.video_account_results("job", KEY)[0]["result"]
        self.assertTrue(rejected["verification_interrupted"])
        self.assertFalse(rejected["requires_reconciliation"])
        self.assertNotIn("needs_account_verification", rejected)
        self.assertEqual(rejected["delete_error"], self.rejection()["delete_error"])
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        self.assertEqual(self.sql("SELECT object_key FROM fb_asset_delete_v2_receipts"), [("video_account:303:900",)])

    def test_abort_rejected_only_releases_lock_for_manual_resume(self):
        self.defer()
        self.store.finish_run(self.run, "interrupted")
        self.assertEqual(self.states(), {"101": "failed", "202": "pending", "303": "pending", "404": "pending"})
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_objects"), [("failed",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(1,)])
        self.run = self.store.claim_run("job", "preview", "tenant:user", ["video"], "manual-resume")["run_id"]
        self.assertTrue(self.store.claim_object("job", KEY, self.run)["claimed"])
        self.store.begin_video_accounts("job", KEY, self.run)
        self.assertTrue(self.claim())

    def test_only_exact_definite_rejection_is_eligible(self):
        attempt = self.claim()
        variants = [dict(needs_account_verification=False), dict(code="200"), dict(success=True),
                    dict(confirmed_absent=True), dict(message="Unsupported get request"), dict(delete_error={}),
                    dict(checked_at="")]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt,
                                                           dict(self.rejection(), **changes))
        for http_status in (301, 302, 307, 308, 500, 503, None, True, "400"):
            result = self.rejection()
            result["detail"]["http_status"] = http_status
            result["delete_error"]["detail"]["http_status"] = http_status
            with self.subTest(http_status=http_status), self.assertRaises(StoreError):
                self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt, result)
        self.assertNotIn("needs_account_verification", self.store.video_account_results("job", KEY)[0]["result"])

    def test_claim_account_and_credential_identity_cannot_change(self):
        attempt = self.claim()
        for changes in (dict(account_id="202"), dict(video_id="901"), dict(delete_scope="video"),
                        dict(credential_user_id="99"), dict(delete_endpoint="900"), dict(credential_fb_user_id="unknown")):
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt,
                                                           dict(self.rejection(), **changes))
        other = self.claim("202")
        with self.assertRaises(StoreError):
            self.store.defer_video_account_verification("job", KEY, self.run, "101", other, self.rejection())
        missing_identity = self.rejection()
        missing_identity.pop("credential_user_id")
        with self.assertRaises(StoreError):
            self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt, missing_identity)

    def test_secret_rejected_without_partial_ledger_write(self):
        attempt = self.claim()
        original = self.rejection()
        original["detail"]["access_token"] = "never-persist"
        original["delete_error"]["detail"]["access_token"] = "never-persist"
        with self.assertRaises(StoreError) as caught:
            self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt, original)
        self.assertEqual(caught.exception.code, "unsafe_metadata")
        self.assertNotIn(b"never-persist", self.path.read_bytes())
        self.assertEqual(self.states()["101"], "in_progress")

    def test_deferred_reject_cannot_become_success_without_exact_absence(self):
        attempt, original = self.defer()
        for changes in (dict(success=True, confirmed_absent=False), dict(proof={}),
                        dict(proof=dict(account_id="202", video_id="900", complete=True)),
                        dict(proof=dict(account_id="101", video_id="901", complete=True)),
                        dict(proof=dict(account_id="101", video_id="900", complete=False)),
                        dict(delete_error={})):
            with self.subTest(changes=changes), self.assertRaises(StoreError):
                self.store.finish_video_account("job", KEY, self.run, "101", attempt, "already_deleted",
                                                dict(self.proof(original), **changes))
        self.assertEqual(self.states()["101"], "in_progress")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])
        proof = self.proof(original)
        completed = self.store.finish_video_account("job", KEY, self.run, "101", attempt, "already_deleted", proof)
        self.assertEqual(completed["status"], "already_deleted")
        self.assertEqual(self.store.finish_video_account("job", KEY, self.run, "101", attempt, "already_deleted", proof), completed)
        self.assertEqual(self.sql("SELECT object_key FROM fb_asset_delete_v2_receipts"), [("video_account:101:900",)])

    def test_incomplete_verification_retains_failure_and_original_error(self):
        attempt, original = self.defer()
        result = deepcopy(original)
        result.pop("needs_account_verification")
        result["verification"] = dict(status="unknown", code="account_video_read_incomplete")
        final = self.store.finish_video_account("job", KEY, self.run, "101", attempt, "failed", result)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["result"]["delete_error"], original["delete_error"])
        self.assertNotIn("needs_account_verification", final["result"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_deferred_audit_failure_rolls_back_then_recovery_stays_unknown(self):
        attempt = self.claim()
        self.sql("""CREATE TRIGGER fail_deferred BEFORE INSERT ON fb_asset_delete_v2_audit
            WHEN NEW.action='video_account_verification_deferred'
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
        with self.assertRaises(StoreError):
            self.store.defer_video_account_verification("job", KEY, self.run, "101", attempt, self.rejection())
        self.assertNotIn("needs_account_verification", self.store.video_account_results("job", KEY)[0]["result"])
        Store(self.path).recover_interrupted(lambda pid, start: False)
        self.assertEqual(self.states()["101"], "unknown")


if __name__ == "__main__":
    unittest.main()

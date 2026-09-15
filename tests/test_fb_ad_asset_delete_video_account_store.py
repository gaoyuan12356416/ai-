"""Account-scoped video writes must never create a global video success proof."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features.fb_ad_asset_delete.store import Store, StoreError


MODE = "ad_account_video"
KEY = "video:900"


class VideoAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.store = Store(self.path)

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(query, parameters).fetchall()

    def create(self, name="job1", accounts=("101", "202")):
        return self.store.create_job(dict(job_id=name, preview_id="preview-" + name, actor="tenant:user",
            input_type="content_id", ids=["123"], products=[{"id": "456"}], status="ready",
            objects=[dict(key=KEY, kind="video", object_id="900", status="pending", account_ids=list(accounts))]))

    def start_run(self, name="job1", request="request1"):
        return self.store.claim_run(name, "preview-" + name, "tenant:user", ["video"], request)["run_id"]

    def start(self, name="job1", accounts=("101", "202"), request="request1"):
        self.create(name, accounts)
        run_id = self.start_run(name, request)
        self.assertTrue(self.store.claim_object(name, KEY, run_id)["claimed"])
        self.store.begin_video_accounts(name, KEY, run_id)
        return run_id

    @staticmethod
    def context(account="101", user="12"):
        return dict(delete_mode=MODE, delete_account_id=account, delete_endpoint="act_" + account + "/advideos",
            credential_kind="user", credential_user_id=user, credential_fb_user_id="345",
            credential_relation="source_ad_user", credential_lookup="matched", source_row_ids=["456"], ad_ids=["789"])

    @staticmethod
    def result(account="101", status="deleted", **extra):
        return dict(delete_mode=MODE, delete_scope=MODE, account_id=account, video_id="900",
                    success=status == "deleted", **extra)

    @staticmethod
    def proof(account="101", **extra):
        return dict(delete_mode=MODE, delete_scope=MODE, account_id=account, video_id="900",
            confirmed_absent=True, proof=dict(account_id=account, video_id="900", complete=True), **extra)

    def claim(self, run_id, account="101", name="job1", context=None):
        return self.store.claim_video_account(name, KEY, run_id, account, context or self.context(account))

    def finish(self, run_id, account="101", status="deleted", name="job1", result=None):
        claim = self.claim(run_id, account, name)
        self.assertTrue(claim["claimed"])
        return self.store.finish_video_account(name, KEY, run_id, account, claim["account_attempt_id"], status,
                                               result or self.result(account, status))

    def finish_parent(self, run_id, status="deleted", name="job1"):
        result = self.store.finish_object(name, KEY, run_id, status,
            dict(delete_mode=MODE, delete_scope=MODE, video_id="900", success=status == "deleted"))
        self.store.finish_run(run_id, "completed" if status == "deleted" else "partial")
        return result

    def object(self, name="job1"):
        return self.store.get_job(name)["objects"][0]

    def states(self, name="job1"):
        return {item["account_id"]: item["status"] for item in self.store.video_account_results(name, KEY)}

    def test_begin_persists_full_scope_before_any_credential_or_attempt(self):
        run_id = self.start(accounts=("202", "101"))
        other = Store(self.path)
        obj = other.get_job("job1")["objects"][0]
        self.assertEqual(obj["result"]["delete_mode"], MODE)
        self.assertEqual([item["account_id"] for item in obj["video_account_results"]], ["202", "101"])
        self.assertEqual(obj["result"]["account_results"], obj["video_account_results"])
        self.assertEqual(self.states(), {"202": "pending", "101": "pending"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(0,)])
        self.store.begin_video_accounts("job1", KEY, run_id)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_accounts"), [(2,)])

    def test_child_requires_initialized_active_parent_and_frozen_account(self):
        self.create()
        run_id = self.start_run()
        with self.assertRaises(StoreError):
            self.store.begin_video_accounts("job1", KEY, run_id)
        self.store.claim_object("job1", KEY, run_id)
        with self.assertRaises(StoreError):
            self.claim(run_id)
        self.store.begin_video_accounts("job1", KEY, run_id)
        for account in ("999", "act_101"):
            with self.subTest(account=account), self.assertRaises(StoreError):
                self.claim(run_id, account)
        self.create("job2")
        other_run = self.start_run("job2", "other")
        with self.assertRaises(StoreError):
            self.claim(other_run)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(0,)])

    def test_credential_identity_is_safe_precommitted_and_immutable(self):
        run_id = self.start()
        context = self.context()
        claim = self.claim(run_id, context=context)
        self.assertTrue(claim["claimed"])
        attempt_id = claim["account_attempt_id"]
        saved = self.sql("SELECT context,status FROM fb_asset_delete_v2_video_account_attempts WHERE account_attempt_id=?", (attempt_id,))[0]
        self.assertEqual((json.loads(saved[0]), saved[1]), (context, "in_progress"))
        self.assertFalse(self.claim(run_id, context=context)["claimed"])
        with self.assertRaises(StoreError) as error:
            self.claim(run_id, context=self.context(user="99"))
        self.assertEqual(error.exception.code, "credential_conflict")
        with self.assertRaises(StoreError):
            self.store.finish_video_account("job1", KEY, run_id, "101", attempt_id, "deleted",
                self.result(credential_user_id="99"))
        self.assertEqual(json.loads(self.sql("SELECT context FROM fb_asset_delete_v2_video_account_attempts")[0][0]), context)

    def test_secrets_bad_endpoint_page_token_and_outside_audit_fields_rejected(self):
        run_id = self.start()
        bad = ({"access_token": "secret"}, {"token": "secret"}, {"credential_lookup": {"token": "secret"}},
               {"delete_account_id": "999"}, {"delete_endpoint": "900"}, {"credential_kind": "page"}, {"unreviewed": "value"})
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(StoreError):
                self.claim(run_id, context=dict(self.context(), **extra))
        self.assertEqual(self.states(), {"101": "pending", "202": "pending"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(0,)])

    def test_partial_failure_keeps_each_result_and_only_failed_account_retries(self):
        run_id = self.start(accounts=("101", "202", "303"))
        self.finish(run_id, "101")
        self.finish(run_id, "202", "failed", result=self.result("202", "failed", code=200))
        self.finish(run_id, "303")
        parent = self.finish_parent(run_id, "failed")
        self.assertEqual({i["account_id"]: i["status"] for i in parent["result"]["account_results"]},
                         {"101": "deleted", "202": "failed", "303": "deleted"})
        retry = self.start_run(request="retry")
        self.store.claim_object("job1", KEY, retry)
        self.store.begin_video_accounts("job1", KEY, retry)
        self.assertFalse(self.claim(retry, "101")["claimed"])
        self.finish(retry, "202")
        self.assertFalse(self.claim(retry, "303")["claimed"])
        self.finish_parent(retry)
        self.assertEqual(dict(self.sql("SELECT account_id,COUNT(*) FROM fb_asset_delete_v2_video_account_attempts GROUP BY account_id")),
                         {"101": 1, "202": 2, "303": 1})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts WHERE object_key=?", (KEY,)), [(0,)])

    def test_account_a_success_never_skips_account_b_in_a_later_task(self):
        first = self.start(accounts=("101",))
        self.finish(first)
        self.finish_parent(first)
        second = self.start("job2", ("101", "202"), "second")
        skipped = self.claim(second, "101", "job2")
        self.assertEqual((skipped["claimed"], skipped["status"]), (False, "already_deleted"))
        self.assertEqual(skipped["result"]["source_job_id"], "job1")
        self.finish(second, "202", name="job2")
        self.finish_parent(second, name="job2")
        self.assertEqual(self.sql("SELECT object_key FROM fb_asset_delete_v2_receipts ORDER BY object_key"),
                         [("video_account:101:900",), ("video_account:202:900",)])

    def test_legacy_global_video_success_still_skips_parent(self):
        self.create(accounts=("101",))
        run_id = self.start_run()
        self.store.claim_object("job1", KEY, run_id)
        self.store.finish_object("job1", KEY, run_id, "deleted", {"delete_mode": "video_id_direct", "success": True})
        self.store.finish_run(run_id, "completed")
        self.create("job2", ("202",))
        other = self.start_run("job2", "other")
        claim = self.store.claim_object("job2", KEY, other)
        self.assertEqual((claim["claimed"], claim["status"]), (False, "already_deleted"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(0,)])

    def test_global_parent_lock_serializes_even_disjoint_accounts(self):
        first = self.start(accounts=("101",))
        self.create("job2", ("202",))
        second = self.start_run("job2", "other")
        blocked = self.store.claim_object("job2", KEY, second)
        self.assertEqual((blocked["claimed"], blocked["reason"]), (False, "object_locked"))
        with self.assertRaises(StoreError):
            self.store.begin_video_accounts("job2", KEY, second)
        self.finish(first)
        self.finish_parent(first)
        self.assertTrue(self.store.claim_object("job2", KEY, second)["claimed"])
        self.store.begin_video_accounts("job2", KEY, second)
        self.assertTrue(self.claim(second, "202", "job2")["claimed"])

    def test_unknown_account_is_not_retried_but_other_accounts_continue(self):
        run_id = self.start()
        self.finish(run_id, status="unknown", result=self.result(status="unknown", code="timeout"))
        self.assertFalse(self.claim(run_id)["claimed"])
        self.finish(run_id, "202")
        self.finish_parent(run_id, "unknown")
        self.assertEqual(self.states(), {"101": "unknown", "202": "deleted"})
        retry = self.start_run(request="retry")
        self.assertFalse(self.store.claim_object("job1", KEY, retry)["claimed"])
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])

    def test_parent_cannot_claim_success_or_failure_over_unknown_or_inflight_child(self):
        run_id = self.start()
        self.claim(run_id)
        for status in ("deleted", "failed", "unknown"):
            with self.subTest(status=status), self.assertRaises(StoreError):
                self.store.finish_object("job1", KEY, run_id, status, {"delete_mode": MODE})
        with self.assertRaises(StoreError):
            self.store.finish_object("job1", KEY, run_id, "deleted", {"success": True})
        self.assertEqual(self.object()["status"], "in_progress")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_dead_worker_fences_only_sent_account_and_preserves_other_progress(self):
        run_id = self.start(accounts=("101", "202", "303", "404"))
        self.finish(run_id, "101")
        self.finish(run_id, "202", "failed")
        self.claim(run_id, "303")
        other = Store(self.path)
        self.assertEqual(other.recover_interrupted(lambda pid, start: True)["live_runs"], 1)
        self.assertEqual(self.states()["303"], "in_progress")
        self.assertEqual(other.recover_interrupted(lambda pid, start: False)["unknown_objects"], 1)
        self.assertEqual(self.states(), {"101": "deleted", "202": "failed", "303": "unknown", "404": "pending"})
        obj = self.object()
        self.assertEqual(obj["status"], "unknown")
        self.assertEqual(obj["result"]["account_results"], obj["video_account_results"])
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        self.assertEqual(self.sql("SELECT object_key FROM fb_asset_delete_v2_receipts"), [("video_account:101:900",)])
        context = json.loads(self.sql("SELECT context FROM fb_asset_delete_v2_video_account_attempts WHERE account_id='303'")[0][0])
        self.assertEqual(context, self.context("303"))

    def test_restart_during_credential_lookup_leaves_unsent_accounts_retryable(self):
        run_id = self.start()
        counts = self.store.recover_interrupted(lambda pid, start: False)
        self.assertEqual(counts["unknown_objects"], 0)
        self.assertEqual(self.object()["status"], "failed")
        self.assertEqual(self.states(), {"101": "pending", "202": "pending"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        retry = self.start_run(request="manual-resume")
        self.assertTrue(self.store.claim_object("job1", KEY, retry)["claimed"])
        self.store.begin_video_accounts("job1", KEY, retry)
        self.assertTrue(self.claim(retry)["claimed"])

    def test_restart_after_all_account_success_preserves_success_without_global_receipt(self):
        run_id = self.start()
        self.finish(run_id)
        self.finish(run_id, "202")
        self.assertEqual(self.store.recover_interrupted(lambda pid, start: False)["unknown_objects"], 0)
        self.assertEqual(self.object()["status"], "deleted")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts WHERE object_key=?", (KEY,)), [(0,)])

    def test_reconcile_proof_requires_exact_account_video_and_complete_enumeration(self):
        run_id = self.start(accounts=("101",))
        self.finish(run_id, status="unknown")
        self.finish_parent(run_id, "unknown")
        bad = [dict(self.proof(), confirmed_absent=False), dict(self.proof(), proof={"complete": True}),
               dict(self.proof(), proof=dict(account_id="202", video_id="900", complete=True)),
               dict(self.proof(), proof=dict(account_id="101", video_id="901", complete=True)),
               dict(self.proof(), proof=dict(account_id="101", video_id="900", complete=False)),
               dict(self.proof(), proof=dict(account_id="101", video_id="900", complete=1)),
               self.result(), dict(self.proof(), account_id="202"), dict(self.proof(), delete_scope="video_object")]
        for proof in bad:
            with self.subTest(proof=proof), self.assertRaises(StoreError):
                self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", proof)
        self.assertEqual(self.states(), {"101": "unknown"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_read_failure_stays_unknown_without_any_receipt(self):
        run_id = self.start(accounts=("101",))
        self.finish(run_id, status="unknown")
        self.finish_parent(run_id, "unknown")
        for code in (100, 200, "timeout"):
            obj = self.store.reconcile_video_account("job1", KEY, "101", "unknown",
                self.result(status="unknown", code=code, checked_at="2026-09-15T00:00:00Z"))
            self.assertEqual(obj["status"], "unknown")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])

    def test_proven_unknown_with_failed_and_unsent_accounts_releases_for_manual_resume(self):
        run_id = self.start(accounts=("101", "202", "303"))
        self.finish(run_id, "101", "unknown")
        self.finish(run_id, "202", "failed")
        self.finish_parent(run_id, "unknown")
        obj = self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", self.proof())
        self.assertEqual(obj["status"], "failed")
        self.assertEqual(self.states(), {"101": "already_deleted", "202": "failed", "303": "pending"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        retry = self.start_run(request="retry")
        self.store.claim_object("job1", KEY, retry)
        self.store.begin_video_accounts("job1", KEY, retry)
        self.assertFalse(self.claim(retry)["claimed"])
        self.finish(retry, "202")
        self.finish(retry, "303")
        self.finish_parent(retry)

    def test_each_unknown_needs_its_own_proof_before_global_fence_releases(self):
        run_id = self.start()
        self.finish(run_id, "101", "unknown")
        self.finish(run_id, "202", "unknown")
        self.finish_parent(run_id, "unknown")
        self.assertEqual(self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", self.proof())["status"], "unknown")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        self.assertEqual(self.store.reconcile_video_account("job1", KEY, "202", "already_deleted", self.proof("202"))["status"], "deleted")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts WHERE object_key=?", (KEY,)), [(0,)])

    def test_account_a_proof_cannot_mark_other_fenced_task_account_b_success(self):
        run_id = self.start(accounts=("101",))
        self.finish(run_id, status="unknown")
        self.finish_parent(run_id, "unknown")
        self.create("job2", ("202",))
        second = self.start_run("job2", "second")
        fenced = self.store.claim_object("job2", KEY, second)
        self.assertEqual((fenced["claimed"], fenced["status"]), (False, "unknown"))
        self.store.finish_run(second, "partial")
        self.assertEqual(self.states("job2"), {"202": "pending"})
        with self.assertRaises(StoreError):
            self.store.reconcile_video_account("job2", KEY, "202", "already_deleted", self.proof())
        self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", self.proof())
        self.assertEqual(self.object("job1")["status"], "deleted")
        self.assertEqual(self.object("job2")["status"], "failed")
        self.assertEqual(self.states("job2"), {"202": "pending"})
        retry = self.start_run("job2", "retry")
        self.assertTrue(self.store.claim_object("job2", KEY, retry)["claimed"])
        self.store.begin_video_accounts("job2", KEY, retry)
        self.assertTrue(self.claim(retry, "202", "job2")["claimed"])

    def test_legacy_reconciliation_cannot_promote_account_proof_to_global_success(self):
        run_id = self.start(accounts=("101",))
        self.finish(run_id, status="unknown")
        self.finish_parent(run_id, "unknown")
        for result in (self.proof(), {"success": True}, {"confirmed_deleted": True}):
            with self.subTest(result=result), self.assertRaises(StoreError) as error:
                self.store.reconcile_object("job1", KEY, "already_deleted", result)
            self.assertEqual(error.exception.code, "account_reconciliation_required")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_reconcile_cannot_change_an_active_account_or_make_unknown_retryable(self):
        run_id = self.start(accounts=("101",))
        self.claim(run_id)
        with self.assertRaises(StoreError):
            self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", self.proof())
        self.store.finish_run(run_id, "interrupted")
        for status in ("pending", "failed", "blocked"):
            with self.subTest(status=status), self.assertRaises(StoreError):
                self.store.reconcile_video_account("job1", KEY, "101", status, self.proof())

    def test_completed_account_attempt_is_idempotent_and_cannot_be_replaced(self):
        run_id = self.start(accounts=("101",))
        claim = self.claim(run_id)
        result = self.result()
        first = self.store.finish_video_account("job1", KEY, run_id, "101", claim["account_attempt_id"], "deleted", result)
        self.assertEqual(self.store.finish_video_account("job1", KEY, run_id, "101", claim["account_attempt_id"], "deleted", result), first)
        with self.assertRaises(StoreError):
            self.store.finish_video_account("job1", KEY, run_id, "101", claim["account_attempt_id"], "deleted", dict(result, changed="no"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_audit WHERE action='video_account_finished'"), [(1,)])

    def test_other_account_attempt_and_success_without_proof_are_rejected(self):
        run_id = self.start()
        first, second = self.claim(run_id, "101"), self.claim(run_id, "202")
        with self.assertRaises(StoreError):
            self.store.finish_video_account("job1", KEY, run_id, "101", second["account_attempt_id"], "deleted", self.result())
        with self.assertRaises(StoreError):
            self.store.finish_video_account("job1", KEY, run_id, "101", first["account_attempt_id"], "deleted", self.result(status="unknown"))
        self.assertEqual(self.states(), {"101": "in_progress", "202": "in_progress"})

    def test_audit_write_failure_rolls_back_begin_without_partial_scope(self):
        self.create()
        run_id = self.start_run()
        self.store.claim_object("job1", KEY, run_id)
        self.sql("""CREATE TRIGGER fail_audit BEFORE INSERT ON fb_asset_delete_v2_audit
            BEGIN SELECT RAISE(ABORT, 'simulated full disk'); END""")
        with self.assertRaises(StoreError):
            self.store.begin_video_accounts("job1", KEY, run_id)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_accounts"), [(0,)])
        self.assertEqual(self.object()["result"], {})

    def test_unwritable_audit_grants_zero_new_account_claims(self):
        run_id = self.start()
        self.sql("""CREATE TRIGGER fail_audit BEFORE INSERT ON fb_asset_delete_v2_audit
            BEGIN SELECT RAISE(ABORT, 'simulated full disk'); END""")
        for account in ("101", "202"):
            with self.subTest(account=account), self.assertRaises(StoreError) as error:
                self.claim(run_id, account)
            self.assertEqual(error.exception.code, "ledger_error")
        self.assertEqual(self.states(), {"101": "pending", "202": "pending"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_video_account_attempts"), [(0,)])

    def test_receipt_failure_rolls_back_success_then_recovery_fences_attempt(self):
        run_id = self.start()
        claim = self.claim(run_id)
        self.sql("""CREATE TRIGGER fail_receipt BEFORE INSERT ON fb_asset_delete_v2_receipts
            BEGIN SELECT RAISE(ABORT, 'simulated receipt failure'); END""")
        with self.assertRaises(StoreError):
            self.store.finish_video_account("job1", KEY, run_id, "101", claim["account_attempt_id"], "deleted", self.result())
        self.assertEqual(self.states(), {"101": "in_progress", "202": "pending"})
        self.store.finish_run(run_id, "interrupted")
        self.assertEqual(self.states(), {"101": "unknown", "202": "pending"})
        self.assertEqual(self.object()["status"], "unknown")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_reconciliation_audit_failure_retains_unknown_and_global_fence(self):
        run_id = self.start(accounts=("101",))
        self.finish(run_id, status="unknown")
        self.finish_parent(run_id, "unknown")
        self.sql("""CREATE TRIGGER fail_audit BEFORE INSERT ON fb_asset_delete_v2_audit
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
        with self.assertRaises(StoreError):
            self.store.reconcile_video_account("job1", KEY, "101", "already_deleted", self.proof())
        self.assertEqual(self.states(), {"101": "unknown"})
        self.assertEqual(self.object()["status"], "unknown")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])


if __name__ == "__main__":
    unittest.main()

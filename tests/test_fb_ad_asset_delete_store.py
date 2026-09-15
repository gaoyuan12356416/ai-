import concurrent.futures
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from features.fb_ad_asset_delete.store import Store, StoreError, _owner_alive, _process_start


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.store = Store(self.path)

    def job(self, name="job1", kinds=("creative", "ad", "video"), actor="tenant:user", **extra):
        data = dict(job_id=name, preview_id="preview-" + name, actor=actor,
                    input_type="content_id", ids=["101"],
                    products=[dict(id="301", name="Drama", parent_id="301")],
                    dramas=[dict(content_id="101", series_code="series1", language="en")],
                    blockers=[], status="ready",
                    objects=[dict(key=kind + ":123", kind=kind, object_id="123", status="pending",
                                  product_ids=["301"], account_id="444", evidence={"source": "exact_relation"})
                             for kind in kinds])
        data.update(extra)
        return self.store.create_job(data)

    def claim_run(self, name="job1", phases=("creative", "ad", "video"), request_id="request1", actor="tenant:user"):
        return self.store.claim_run(name, "preview-" + name, actor, list(phases), request_id)

    def finish(self, key, run_id, status="deleted", job_id="job1"):
        claim = self.store.claim_object(job_id, key, run_id)
        self.assertTrue(claim["claimed"])
        self.assertIsInstance(claim["attempt_id"], str)
        return self.store.finish_object(job_id, key, run_id, status,
                                        {"success": status == "deleted", "evidence": "controlled test"})

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                return conn.execute(query, parameters).fetchall()

    def test_additive_schema_preserves_legacy_and_second_open(self):
        self.sql("CREATE TABLE legacy_jobs (job_id TEXT)")
        self.sql("INSERT INTO legacy_jobs VALUES ('historical-post')")
        self.job()
        other = Store(self.path)
        self.assertEqual(self.sql("SELECT * FROM legacy_jobs"), [("historical-post",)])
        self.assertEqual(other.get_job("job1")["schema_version"], 2)

    def test_credential_selection_is_durable_immutable_and_survives_interruption(self):
        import json
        self.job(kinds=("video",))
        run = self.claim_run(phases=("video",))
        self.store.claim_object("job1", "video:123", run["run_id"])
        context = dict(delete_mode="video_id_direct", credential_kind="page", credential_page_id="444",
                       credential_row_id="88", credential_fb_user_id="999", credential_user_id="803", credential_relation="creative_page")
        self.store.record_object_credential("job1", "video:123", run["run_id"], context)
        self.store.record_object_credential("job1", "video:123", run["run_id"], context)
        self.assertEqual(json.loads(self.sql("SELECT result FROM fb_asset_delete_v2_attempts")[0][0]), context)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_audit WHERE action='object_credential_selected'")[0][0], 1)
        with self.assertRaises(StoreError):
            self.store.record_object_credential("job1", "video:123", run["run_id"], dict(context, credential_page_id="555"))
        for extra in ({"token": "never-persist"}, {"page_access_token": "never-persist"}, {"credential_lookup": {"token": "never-persist"}}):
            with self.subTest(extra=extra), self.assertRaises(StoreError):
                self.store.record_object_credential("job1", "video:123", run["run_id"], dict(context, **extra))
        self.store.finish_run(run["run_id"], "interrupted")
        obj = self.store.get_job("job1")["objects"][0]
        self.assertEqual(obj["status"], "unknown")
        self.assertEqual(obj["result"]["credential_page_id"], "444")
        with self.assertRaises(StoreError):
            self.store.record_object_credential("job1", "video:123", run["run_id"], context)

    def test_safe_metadata_ids_and_owner_filtered_listing(self):
        job = self.job()
        self.job("job2", actor="other:actor")
        self.assertEqual(job["objects"][0]["evidence"], {"source": "exact_relation"})
        self.assertTrue(all(isinstance(obj["object_id"], str) for obj in job["objects"]))
        self.assertEqual(len(self.store.list_jobs()), 2)
        listed = self.store.list_jobs("tenant:user")
        self.assertEqual([item["job_id"] for item in listed], ["job1"])
        self.assertNotIn("objects", listed[0])
        self.assertNotIn("objects", self.store.get_job("job1", include_objects=False))
        with self.assertRaises(StoreError) as error:
            self.store.update_job("job1", unsafe={"access_token": "not-a-real-token"})
        self.assertEqual(error.exception.code, "unsafe_metadata")
        self.assertNotIn("unsafe", self.store.get_job("job1"))

    def test_snapshot_updates_only_before_execution(self):
        self.job(status="previewing")
        obj = dict(key="ad:222", kind="ad", object_id="222", status="pending")
        updated = self.store.update_job("job1", objects=[obj], status="ready")
        self.assertEqual([item["key"] for item in updated["objects"]], ["ad:222"])
        self.claim_run(phases=("ad",))
        for fields in ({"objects": []}, {"preview_id": "other"}, {"ids": ["202"]}, {"status": "ready"}):
            with self.assertRaises(StoreError):
                self.store.update_job("job1", **fields)
        self.assertEqual(self.store.update_job("job1", note="still running")["status"], "running")

    def test_bad_snapshot_update_rolls_back_deleted_rows(self):
        self.job(kinds=("ad",))
        invalid = dict(key="ad:999", kind="creative", object_id="999", status="pending")
        with self.assertRaises(StoreError):
            self.store.update_job("job1", objects=[invalid])
        self.assertEqual(self.store.get_job("job1")["objects"][0]["key"], "ad:123")

    def test_request_idempotency_and_conflicting_parameters(self):
        self.job()
        first = self.claim_run(phases=("video", "ad", "creative"))
        self.assertEqual(first["phases"], ["creative", "ad", "video"])
        repeated = self.claim_run(phases=("ad", "creative", "video"))
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(first["run_id"], repeated["run_id"])
        self.assertFalse(first["duplicate"])
        self.job("job2")
        for name, phases, actor in (("job2", ("creative", "ad", "video"), "tenant:user"),
                                    ("job1", ("ad",), "tenant:user"),
                                    ("job1", ("creative", "ad", "video"), "admin:actor")):
            with self.assertRaises(StoreError) as error:
                self.claim_run(name, phases, actor=actor)
            self.assertEqual(error.exception.code, "request_conflict")
        self.assertEqual(len(self.store.get_job("job1")["runs"]), 1)

    def test_run_guard_and_admin_actor_authorized_by_service(self):
        self.job(status="previewing")
        with self.assertRaises(StoreError):
            self.claim_run()
        self.store.update_job("job1", status="failed")
        with self.assertRaises(StoreError):
            self.claim_run()
        self.store.update_job("job1", status="ready")
        with self.assertRaises(StoreError) as error:
            self.store.claim_run("job1", "stale-preview", "tenant:user", ["ad"], "stale")
        self.assertEqual(error.exception.code, "preview_mismatch")
        self.assertEqual(self.claim_run(actor="admin:actor")["actor"], "admin:actor")
        with self.assertRaises(StoreError):
            self.claim_run(request_id="second")

    def test_phase_results_survive_later_objects_and_runs(self):
        self.job()
        first = self.claim_run(phases=("creative",))
        creative = self.finish("creative:123", first["run_id"])
        self.store.finish_run(first["run_id"], "completed", {"creative_deleted": 1})
        second = self.claim_run(phases=("ad",), request_id="second")
        self.finish("ad:123", second["run_id"])
        self.store.finish_run(second["run_id"], "completed")
        third = self.claim_run(phases=("video",), request_id="third")
        self.finish("video:123", third["run_id"])
        self.store.finish_run(third["run_id"], "completed")
        job = self.store.get_job("job1")
        self.assertEqual([obj["status"] for obj in job["objects"]], ["deleted"] * 3)
        self.assertEqual(job["objects"][0]["result"], creative["result"])
        self.assertEqual([run["phases"] for run in job["runs"]], [["creative"], ["ad"], ["video"]])
        self.assertEqual(job["runs"][0]["summary"], {"creative_deleted": 1})

    def test_failure_continues_and_manual_retry_preserves_success(self):
        self.job()
        first = self.claim_run()
        self.finish("creative:123", first["run_id"], "failed")
        self.finish("ad:123", first["run_id"])
        self.finish("video:123", first["run_id"])
        self.assertEqual(self.store.finish_run(first["run_id"], "completed")["status"], "partial")
        second = self.claim_run(request_id="retry")
        self.finish("creative:123", second["run_id"])
        skipped = self.store.claim_object("job1", "ad:123", second["run_id"])
        self.assertFalse(skipped["claimed"])
        self.assertEqual(skipped["status"], "deleted")
        self.store.finish_run(second["run_id"], "completed")
        attempts = self.sql("SELECT object_key,COUNT(*) FROM fb_asset_delete_v2_attempts GROUP BY object_key")
        self.assertEqual(dict(attempts), {"creative:123": 2, "ad:123": 1, "video:123": 1})

    def test_object_must_belong_to_requested_phase_and_job(self):
        self.job()
        first = self.claim_run(phases=("creative",))
        with self.assertRaises(StoreError):
            self.store.claim_object("job1", "ad:123", first["run_id"])
        with self.assertRaises(StoreError):
            self.store.claim_object("job1", "creative:999", first["run_id"])
        self.job("job2")
        with self.assertRaises(StoreError):
            self.store.claim_object("job2", "creative:123", first["run_id"])

    def test_cross_job_concurrency_and_durable_success_skip(self):
        self.job(kinds=("ad",))
        self.job("job2", kinds=("ad",))
        run1 = self.claim_run(phases=("ad",))
        run2 = self.claim_run("job2", phases=("ad",), request_id="request2")
        second_store = Store(self.path)
        barrier = threading.Barrier(2)

        def claim(store, name, run_id):
            barrier.wait(timeout=5)
            return name, run_id, store.claim_object(name, "ad:123", run_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda args: claim(*args),
                                       ((self.store, "job1", run1["run_id"]),
                                        (second_store, "job2", run2["run_id"]))))
        self.assertEqual(sum(item[2]["claimed"] for item in results), 1)
        winner = next(item for item in results if item[2]["claimed"])
        loser = next(item for item in results if not item[2]["claimed"])
        self.assertEqual(loser[2]["reason"], "object_locked")
        self.store.finish_object(winner[0], "ad:123", winner[1], "deleted", {"success": True})
        skipped = second_store.claim_object(loser[0], "ad:123", loser[1])
        self.assertFalse(skipped["claimed"])
        self.assertEqual(skipped["status"], "already_deleted")
        self.assertEqual(skipped["result"]["source_job_id"], winner[0])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_attempts"), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])

    def test_unknown_fences_same_job_and_other_tasks_until_proven(self):
        self.job(kinds=("video",))
        first = self.claim_run(phases=("video",))
        self.finish("video:123", first["run_id"], "unknown")
        self.store.finish_run(first["run_id"], "partial")
        second = self.claim_run(phases=("video",), request_id="retry")
        self.assertFalse(self.store.claim_object("job1", "video:123", second["run_id"])["claimed"])
        self.job("job2", kinds=("video",))
        other = self.claim_run("job2", phases=("video",), request_id="other")
        fenced = self.store.claim_object("job2", "video:123", other["run_id"])
        self.assertEqual((fenced["claimed"], fenced["status"]), (False, "unknown"))
        for invalid_status, proof in (("pending", {"success": True}), ("failed", {"success": True}),
                                       ("already_deleted", {"error": "cannot read due to permission"})):
            with self.assertRaises(StoreError):
                self.store.reconcile_object("job1", "video:123", invalid_status, proof)
        self.store.reconcile_object("job1", "video:123", "unknown", {"error": "permission denied"})
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        self.store.reconcile_object("job2", "video:123", "already_deleted", {"proof": {"confirmed_status": "DELETED"}})
        self.assertEqual(self.store.get_job("job1")["objects"][0]["status"], "already_deleted")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])
        self.job("job3", kinds=("video",))
        run3 = self.claim_run("job3", phases=("video",), request_id="third")
        self.assertEqual(self.store.claim_object("job3", "video:123", run3["run_id"])["status"], "already_deleted")

    def test_blocked_objects_are_never_claimed(self):
        self.job(kinds=("ad",), objects=[dict(key="ad:123", kind="ad", object_id="123", status="blocked", reason="shared outside scope")])
        run = self.claim_run(phases=("ad",))
        result = self.store.claim_object("job1", "ad:123", run["run_id"])
        self.assertEqual((result["claimed"], result["status"]), (False, "blocked"))
        self.assertEqual(self.store.finish_run(run["run_id"], "completed")["status"], "partial")

    def test_live_owner_survives_new_store_and_dead_owner_is_fenced(self):
        self.job()
        run = self.claim_run()
        self.store.claim_object("job1", "creative:123", run["run_id"])
        restarted_connection = Store(self.path)
        live = restarted_connection.recover_interrupted()
        self.assertEqual(live, dict(interrupted_runs=0, unknown_objects=0, live_runs=1))
        seen = []

        def dead(pid, started):
            seen.append((pid, started))
            return False

        recovered = restarted_connection.recover_interrupted(dead)
        self.assertEqual(recovered, dict(interrupted_runs=1, unknown_objects=1, live_runs=0))
        self.assertEqual(seen[0], (run["owner_pid"], run["owner_start"]))
        job = self.store.get_job("job1")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual([obj["status"] for obj in job["objects"]], ["unknown", "pending", "pending"])
        self.assertEqual(job["runs"][0]["status"], "interrupted")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])
        resumed = self.claim_run(phases=("ad", "video"), request_id="manual-resume")
        self.finish("ad:123", resumed["run_id"])
        self.finish("video:123", resumed["run_id"])
        self.store.finish_run(resumed["run_id"], "completed")
        self.assertEqual(self.store.get_job("job1")["objects"][0]["status"], "unknown")
        with self.assertRaises(StoreError):
            self.store.finish_object("job1", "creative:123", run["run_id"], "deleted", {"success": True})

    def test_unknown_liveness_does_not_recover_and_pid_reuse_is_detected(self):
        self.job(kinds=("ad",))
        run = self.claim_run(phases=("ad",))
        self.assertEqual(self.store.recover_interrupted(lambda pid, start: None)["interrupted_runs"], 0)
        exists, started = _process_start(os.getpid())
        self.assertTrue(exists)
        self.assertEqual(started, run["owner_start"])
        with patch("features.fb_ad_asset_delete.store._process_start", return_value=(True, "new-process-start")):
            self.assertFalse(_owner_alive(run["owner_pid"], run["owner_start"]))
        with patch("features.fb_ad_asset_delete.store._process_start", return_value=(None, None)):
            self.assertTrue(_owner_alive(run["owner_pid"], run["owner_start"]))

    def test_linux_identity_parses_parentheses_and_fails_closed_without_boot_id(self):
        stat_path, boot_path = Mock(), Mock()
        tail = ["S"] + ["0"] * 18 + ["98765"]
        stat_path.read_text.return_value = "123 (worker ) unusual name) " + " ".join(tail)
        boot_path.read_text.return_value = "boot-identity\n"

        def fake_path(value):
            return stat_path if value == "/proc/123/stat" else boot_path

        with patch("features.fb_ad_asset_delete.store.os.name", "posix"), \
                patch("features.fb_ad_asset_delete.store.Path", side_effect=fake_path):
            self.assertEqual(_process_start(123), (True, "linux:boot-identity:98765"))
            boot_path.read_text.side_effect = FileNotFoundError("missing boot identity")
            self.assertEqual(_process_start(123), (None, None))
            stat_path.read_text.return_value = "123 (worker) Z " + " ".join(tail[1:])
            self.assertEqual(_process_start(123), (False, None))

    def test_finishing_with_inflight_request_keeps_unknown_fence(self):
        self.job(kinds=("ad",))
        run = self.claim_run(phases=("ad",))
        self.store.claim_object("job1", "ad:123", run["run_id"])
        result = self.store.finish_run(run["run_id"], "completed")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.get_job("job1")["objects"][0]["status"], "unknown")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("unknown",)])

    def test_claim_rollback_prevents_graph_authorization_on_failed_write(self):
        self.job(kinds=("ad",))
        run = self.claim_run(phases=("ad",))
        self.sql("""CREATE TRIGGER fail_attempt BEFORE INSERT ON fb_asset_delete_v2_attempts
                    BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END""")
        with self.assertRaises(StoreError) as error:
            self.store.claim_object("job1", "ad:123", run["run_id"])
        self.assertEqual(error.exception.code, "ledger_error")
        self.assertEqual(self.store.get_job("job1")["objects"][0]["status"], "pending")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_attempts"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_object_locks"), [(0,)])

    def test_finish_rollback_retains_claim_when_receipt_cannot_persist(self):
        self.job(kinds=("ad",))
        run = self.claim_run(phases=("ad",))
        self.store.claim_object("job1", "ad:123", run["run_id"])
        self.sql("""CREATE TRIGGER fail_receipt BEFORE INSERT ON fb_asset_delete_v2_receipts
                    BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END""")
        with self.assertRaises(StoreError):
            self.store.finish_object("job1", "ad:123", run["run_id"], "deleted", {"success": True})
        self.assertEqual(self.store.get_job("job1")["objects"][0]["status"], "in_progress")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_attempts"), [("in_progress",)])
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("in_progress",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM fb_asset_delete_v2_receipts"), [(0,)])

    def test_recovery_transaction_rolls_back_when_audit_cannot_persist(self):
        self.job(kinds=("ad",))
        run = self.claim_run(phases=("ad",))
        self.store.claim_object("job1", "ad:123", run["run_id"])
        self.sql("""CREATE TRIGGER fail_audit BEFORE INSERT ON fb_asset_delete_v2_audit
                    BEGIN SELECT RAISE(ABORT, 'simulated audit write failure'); END""")
        with self.assertRaises(StoreError):
            self.store.recover_interrupted(lambda pid, started: False)
        job = self.store.get_job("job1")
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["objects"][0]["status"], "in_progress")
        self.assertEqual(self.sql("SELECT status FROM fb_asset_delete_v2_object_locks"), [("in_progress",)])


if __name__ == "__main__":
    unittest.main()

"""Offline pool invariants with real temporary SQLite ledgers and mocked sources."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.x_posts.service import XPostError
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.pool import PoolService
from features.x_video_publish.scheduler import PoolScheduler
from features.x_video_publish.sources import SourceRepository
from scripts.test_x_video_publish_gateway import FakeMySQL


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PersonalStore(Path(self.tmp.name) / "pool.sqlite")
        self.now = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.actor = {"tenant_key": "test-tenant", "user_id": "owner-a", "name": "Offline fixture"}
        self.account = {"id": 1, "x_user_id": "9000000000000000001", "owner_tenant_key": "test-tenant",
            "owner_user_id": "owner-a", "drama_language": "en", "status": "active",
            "publish_approved": True, "publish_eligible": True, "display_name": "Fixture"}
        self.materials = {str(mid): {"material_id": str(mid), "material_name": "Video " + str(mid),
            "drama_name": "Fictional drama", "drama_description": "A short story.", "language": "en",
            "duration_seconds": 45} for mid in range(101, 106)}
        self.identity_calls, self.identity_active = [], True
        self.pool = PoolService(self.store, self.account_resolver, self.material_resolver, self.identity,
                                now_fn=lambda: self.now, enabled=True)

    def tearDown(self):
        self.tmp.cleanup()

    def account_resolver(self, actor, aid):
        return copy.deepcopy(self.account)

    def material_resolver(self, actor, mids):
        return {"items": {mid: copy.deepcopy(self.materials[mid]) for mid in mids if mid in self.materials},
                "errors": {mid: {"code": "material_not_found", "message": "Fixture material unavailable"} for mid in mids if mid not in self.materials}}

    def use_real_source(self, count=5):
        # Exercise the production repository and selector, mocking only MySQL.
        source = SourceRepository(FakeMySQL(count))
        self.pool.material_resolver = lambda actor, mids: source.resolve(mids)

    def identity(self, tenant, user):
        self.identity_calls.append((tenant, user))
        if not self.identity_active:
            raise XPostError("identity_permission_denied", "Fixture permission revoked", 403)
        return copy.deepcopy(self.actor)

    def version(self):
        return self.pool.listing(self.actor, "1")["pool"]["version"]

    def settings(self, times=None):
        return self.pool.settings(self.actor, "1", {"expected_version": self.version(), "times": times or ["08:01", "08:02", "08:03"], "processing_mode": "original"})

    def add(self, mids):
        version = self.version()
        proof = self.pool.preview(self.actor, "1", {"expected_version": version, "material_ids": mids})
        if not proof["items"]:
            return proof
        body = {"expected_version": version, "preview_id": proof["preview_id"], "idempotency_key": "admission-" + proof["preview_id"]}
        result = self.pool.add(self.actor, "1", body)
        return result, proof, body

    def enable(self):
        return self.pool.toggle(self.actor, "1", {"expected_version": self.version()}, True)

    def scheduler(self):
        scheduler = PoolScheduler(self.pool)
        scheduler.start("offline-worker")
        return scheduler

    def scalar(self, sql, args=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def test_settings_first_save_is_paused_and_version_fenced(self):
        result = self.settings()
        self.assertFalse(result["pool"]["enabled"])
        self.assertEqual(result["pool"]["version"], 1)
        with self.assertRaises(XPostError) as caught:
            self.pool.settings(self.actor, "1", {"expected_version": 0, "times": ["09:00"], "processing_mode": "original"})
        self.assertEqual(caught.exception.code, "pool_version_conflict")
        for invalid in (["25:00"], ["09:00", "09:00"], [["09:00"]]):
            with self.assertRaises(XPostError):
                self.pool.settings(self.actor, "1", {"expected_version": 1, "times": invalid, "processing_mode": "original"})

    def test_owner_boundary_including_admin_cannot_see_or_change_another_pool(self):
        self.settings()
        other = {"tenant_key": "test-tenant", "user_id": "owner-b", "is_admin": True}
        for method in (lambda: self.pool.listing(other, "1"), lambda: self.pool.toggle(other, "1", {"expected_version": 1}, False)):
            with self.assertRaises(XPostError) as caught:
                method()
            self.assertEqual(caught.exception.status, 404)

    def test_ordered_partial_admission_and_exact_retry(self):
        self.settings()
        result, preview, body = self.add(["102", "bad", "101", "102", "999", "", "1e2"])
        self.assertEqual([r["material_id"] for r in result["items"]], ["102", "101"])
        self.assertEqual([r["row_number"] for r in preview["skipped"]], [2, 4, 5, 7])
        self.assertTrue(all(r["message"] for r in preview["skipped"]))
        self.now += timedelta(minutes=11)
        self.assertTrue(self.pool.add(self.actor, "1", body)["idempotent"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 2)
        with self.assertRaises(XPostError):
            self.pool.add(self.actor, "1", {**body, "preview_id": "different"})

    def test_expired_admission_does_not_add(self):
        self.settings()
        version = self.version()
        proof = self.pool.preview(self.actor, "1", {"expected_version": version, "material_ids": ["101"]})
        self.now += timedelta(minutes=10)
        with self.assertRaises(XPostError):
            self.pool.add(self.actor, "1", {"expected_version": version, "preview_id": proof["preview_id"], "idempotency_key": "expired-admission"})
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 0)

    def test_skipped_reserved_material_does_not_invalidate_remaining_positions(self):
        self.settings()
        self.use_real_source()
        other = {**self.account, "id": 2, "x_user_id": "9000000000000000002"}
        proof = self.store.preview(self.actor, {"source": "manual", "rows": [
            {"account_id": "2", "material_id": "102"}]}, {"2": other}, self.materials, now=self.now)
        self.store.commit(self.actor, proof["preview_token"], "existing-reservation",
                          accounts={"2": other}, materials=self.materials, now=self.now)
        result, preview, body = self.add(["101", "102", "103", "101", "104"])
        self.assertEqual([r["material_id"] for r in result["items"]], ["101", "103", "104"])
        self.assertEqual([r["code"] for r in preview["skipped"]],
                         ["x_post_manual_material_unavailable", "duplicate_material_id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_post_manual_material_reservation WHERE state='active'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_post_queue"), 0)
        self.assertTrue(self.pool.add(self.actor, "1", body)["idempotent"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 3)

    def test_occupied_and_missing_rows_across_source_pages_preserve_admission_order(self):
        self.settings()
        self.use_real_source(60)
        self.add(["102"])
        submitted = ["999", *map(str, range(101, 161))]
        result, preview, _ = self.add(submitted)
        self.assertEqual([r["material_id"] for r in result["items"]], [mid for mid in submitted if mid not in {"999", "102"}])
        self.assertEqual([r["code"] for r in preview["skipped"]], ["material_not_found", "pool_material_exists"])

    def test_legacy_proof_requires_fresh_preview_without_rewriting_history(self):
        self.settings()
        self.use_real_source()
        version = self.version()
        proof = self.pool.preview(self.actor, "1", {"expected_version": version, "material_ids": ["999", "101", "103"]})
        with self.pool.connection() as conn:
            raw = json.loads(conn.execute("SELECT material_facts FROM x_video_publish_pool_preview WHERE preview_id=?", (proof["preview_id"],)).fetchone()[0])
            raw["101"]["manual_item_id"], raw["103"]["manual_item_id"] = 2, 3
            conn.execute("UPDATE x_video_publish_pool_preview SET material_facts=? WHERE preview_id=?", (json.dumps(raw), proof["preview_id"]))
        with self.assertRaises(XPostError) as caught:
            self.pool.add(self.actor, "1", {"expected_version": version, "preview_id": proof["preview_id"], "idempotency_key": "legacy-proof-admission"})
        self.assertEqual(caught.exception.code, "pool_preview_changed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 0)
        result, _, _ = self.add(["999", "101", "103"])
        self.assertEqual([r["material_id"] for r in result["items"]], ["101", "103"])

    def test_new_reservation_after_preview_still_blocks_entire_admission(self):
        self.settings()
        version = self.version()
        proof = self.pool.preview(self.actor, "1", {"expected_version": version, "material_ids": ["101", "103"]})
        other = self.store.preview(self.actor, {"source": "manual", "rows": [
            {"account_id": "1", "material_id": "103"}]}, {"1": self.account}, self.materials, now=self.now)
        self.store.commit(self.actor, other["preview_token"], "concurrent-reservation",
                          accounts={"1": self.account}, materials=self.materials, now=self.now)
        with self.assertRaises(XPostError) as caught:
            self.pool.add(self.actor, "1", {"expected_version": version, "preview_id": proof["preview_id"], "idempotency_key": "blocked-admission"})
        self.assertEqual(caught.exception.code, "pool_preview_changed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_operation"), 0)

    def test_preview_material_change_requires_recheck(self):
        self.settings()
        version = self.version()
        proof = self.pool.preview(self.actor, "1", {"expected_version": version, "material_ids": ["101"]})
        original = copy.deepcopy(self.materials["101"])
        for field, value in {"drama_description": "Changed", "material_name": "Different video",
                             "material_url": "https://example.invalid/changed.mp4", "content_id": "999",
                             "duration_seconds": 46, "language": "ja", "material_id": "999"}.items():
            with self.subTest(field=field):
                self.materials["101"] = {**original, field: value}
                with self.assertRaises(XPostError) as caught:
                    self.pool.add(self.actor, "1", {"expected_version": version, "preview_id": proof["preview_id"], "idempotency_key": "changed-admission"})
                self.assertEqual(caught.exception.code, "pool_preview_changed")
                self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item"), 0)
        self.materials["101"] = original

    def test_move_remove_keep_order_and_preserve_history(self):
        self.settings()
        result, _, _ = self.add(["101", "102", "103"])
        ids = [r["item_id"] for r in result["items"]]
        self.pool.reorder_or_remove(self.actor, "1", {"expected_version": self.version(), "item_id": ids[2], "direction": "top"}, False)
        self.assertEqual([r["material_id"] for r in self.pool.listing(self.actor, "1")["items"]], ["103", "101", "102"])
        self.pool.reorder_or_remove(self.actor, "1", {"expected_version": self.version(), "item_ids": ids[:2]}, True)
        self.assertEqual([r["material_id"] for r in self.pool.listing(self.actor, "1")["items"]], ["103"])
        self.assertEqual(self.pool.listing(self.actor, "1", {"state": "failed"})["counts"]["failed"], 2)

    def test_one_task_per_slot_exactly_once_and_next_slot_independent(self):
        self.settings()
        self.add(["101", "102", "103"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=5)
        first = scheduler.tick("offline-worker")
        self.assertEqual(first["slots_created"], 1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_post_manual_material_reservation WHERE state='active'"), 1)
        self.now += timedelta(minutes=1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_batch"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item WHERE status='pending'"), 1)

    def test_two_scheduler_instances_cannot_freeze_same_slot_twice(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        first, second = self.scheduler(), self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        a, b = first._candidates(self.now)[0], second._candidates(self.now)[0]
        a_args, b_args = first._resolve(a), second._resolve(b)
        self.assertEqual(first._finish(a, *a_args, None)["status"], "created")
        self.assertEqual(second._finish(b, *b_args, None)["status"], "already_recorded")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 1)

    def test_pause_keeps_frozen_task_and_blocks_next_slot(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        scheduler.tick("offline-worker")
        self.pool.toggle(self.actor, "1", {"expected_version": self.version()}, False)
        self.now += timedelta(minutes=1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        self.assertEqual(self.scalar("SELECT status FROM x_post_manual_run"), "queued")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item WHERE status='pending'"), 1)

    def test_unknown_task_pauses_plan_and_never_requeues_item(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        scheduler.tick("offline-worker")
        with self.pool.connection() as conn:
            conn.execute("UPDATE x_post_manual_run SET status='needs_review'")
        self.now += timedelta(minutes=1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        listing = self.pool.listing(self.actor, "1", {"state": "failed"})
        self.assertFalse(listing["pool"]["enabled"])
        self.assertEqual(listing["pool"]["pause_reason"], "unknown_outcome")
        self.assertEqual(listing["items"][0]["status"], "unknown")
        with self.assertRaises(XPostError):
            self.enable()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 1)

    def test_definitive_head_failure_enters_exception_and_next_slot_continues(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        scheduler = self.scheduler()
        del self.materials["101"]
        self.now += timedelta(minutes=1, seconds=1)
        result = scheduler.tick("offline-worker")
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 0)
        self.assertEqual(self.pool.listing(self.actor, "1", {"state": "failed"})["items"][0]["material_id"], "101")
        self.now += timedelta(minutes=1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 1)

    def test_restart_cutoff_and_elapsed_slot_are_not_backfilled(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        self.now += timedelta(minutes=1, seconds=1)
        scheduler = self.scheduler()
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        self.now += timedelta(minutes=2)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 1)
        self.assertEqual(self.scalar("SELECT slot_time FROM x_video_publish_pool_slot"), "08:03")

    def test_empty_pool_records_one_skip_per_real_account_slot(self):
        self.settings()
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        result = scheduler.tick("offline-worker")
        self.assertEqual(result["results"][0]["status"], "empty")
        scheduler.tick("offline-worker")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_slot"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 0)

    def test_identity_revocation_and_language_drift_pause_without_consuming(self):
        self.settings()
        self.add(["101"])
        self.enable()
        scheduler = self.scheduler()
        self.identity_active = False
        self.now += timedelta(minutes=1, seconds=1)
        result = scheduler.tick("offline-worker")
        self.assertEqual(result["results"][0]["status"], "blocked")
        self.assertFalse(self.pool.listing(self.actor, "1")["pool"]["enabled"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item WHERE status='pending'"), 1)
        self.assertGreaterEqual(len(self.identity_calls), 2)
        self.account["drama_language"] = "ja"
        listing = self.pool.listing(self.actor, "1")
        self.assertEqual(listing["pool"]["pause_reason"], "account_language_changed")
        self.assertTrue(listing["items"][0]["language_mismatch"])

    def test_atomic_freeze_failure_rolls_back_slot_task_and_item(self):
        self.settings()
        self.add(["101"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        original = self.store.commit
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("offline injected disk failure")
        with patch.object(self.store, "commit", interrupted):
            with self.assertRaises(sqlite3.OperationalError):
                scheduler.tick("offline-worker")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_post_manual_run"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_slot"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_item WHERE status='pending'"), 1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 1)

    def test_slot_rechecks_current_account_and_never_consumes_on_disable(self):
        self.settings()
        self.add(["101"])
        self.enable()
        scheduler = self.scheduler()
        self.account["publish_approved"] = False
        self.now += timedelta(minutes=1, seconds=1)
        result = scheduler.tick("offline-worker")
        self.assertEqual(result["results"][0]["status"], "blocked")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 0)
        self.assertEqual(self.scalar("SELECT status FROM x_video_publish_pool_item"), "pending")
        self.assertEqual(self.scalar("SELECT status FROM x_video_publish_pool"), "paused")

    def test_changed_pool_or_default_during_preflight_cannot_freeze(self):
        self.settings()
        self.add(["101"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        candidate = scheduler._candidates(self.now)[0]
        resolved = scheduler._resolve(candidate)
        self.store.save_settings(self.actor, "1", {"version": 0, "default_copy": "New caption {name}"}, self.account)
        result = scheduler._finish(candidate, *resolved, None)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "configuration_changed")
        self.assertEqual(self.scalar("SELECT status FROM x_video_publish_pool_item"), "pending")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_task"), 0)

    def test_enable_after_minute_boundary_does_not_backfill_that_minute(self):
        self.settings(["08:01"])
        self.add(["101"])
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, microseconds=1)
        self.enable()
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        self.now += timedelta(seconds=60)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM x_video_publish_pool_slot"), 0)

    def test_finished_failure_moves_to_exception_without_stopping_next_item(self):
        self.settings()
        self.add(["101", "102"])
        self.enable()
        scheduler = self.scheduler()
        self.now += timedelta(minutes=1, seconds=1)
        scheduler.tick("offline-worker")
        with self.pool.connection() as conn:
            conn.execute("UPDATE x_post_manual_run SET status='failed_preflight',error_message='Fixture definite failure'")
        self.now += timedelta(minutes=1)
        self.assertEqual(scheduler.tick("offline-worker")["slots_created"], 1)
        failed = self.pool.listing(self.actor, "1", {"state": "failed"})
        self.assertEqual(failed["items"][0]["status"], "failed")
        self.assertIn("definite", failed["items"][0]["reason"])
        self.assertTrue(failed["pool"]["enabled"])

    def test_disabled_gate_blocks_new_work_but_pause_and_read_remain_available(self):
        self.settings()
        self.enable()
        self.pool.enabled = False
        with self.assertRaises(XPostError):
            self.settings()
        self.pool.toggle(self.actor, "1", {"expected_version": self.version()}, False)
        self.assertFalse(self.pool.listing(self.actor, "1")["pool"]["enabled"])
        self.assertEqual(self.scheduler().tick("offline-worker")["status"], "pool_gate_closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

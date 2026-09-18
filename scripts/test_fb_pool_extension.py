"""Add-only future-run extension tests with no network or production writes."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError, utc_iso
from features.fb_auto_posts.pool_extension import extend_future_runs, scope_fingerprint
from scripts.test_fb_auto_validation import payload
from scripts.test_fb_page_languages import Clock, Materials, Pages, material, page


class PoolExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc))
        self.store = FBAutoPostStore(Path(self.tmp.name) / "state.sqlite3", now_fn=self.clock)
        self.actor = ActorScope("u", "QA", False, "248")
        raw = payload()
        raw["message_template"] = "{{drama_name}} | {{desc}} | {{url}}"
        self.template = self.store.create_template(raw, self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(self.template["id"], True, self.actor, self.template["version"])
        self.materials = Materials({
            "en": tuple(material(value, "en") for value in (101, 102, 103)),
            "es": tuple(material(value, "es") for value in (201, 202, 203)),
        })
        self.scope = [page(10001, "en"), page(10002, "es")]

    def tearDown(self):
        self.tmp.cleanup()

    def seed_run(self, suffix="one", minutes_ahead=60, rows=None, trigger="auto"):
        slot = f"{trigger}:extension-{suffix}"
        planned = utc_iso(self.clock() + timedelta(minutes=minutes_ahead))
        rows = rows if rows is not None else [self.scope[0]]
        if trigger == "manual":
            return self.store.create_run(self.template["id"], slot, "manual", self.actor, Pages(rows), self.materials, planned_publish_at_utc=planned)["run_id"]
        now = utc_iso(self.clock())
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,trigger_type,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'pending','auto',?,?)",
                (self.template["id"], self.template["version"], slot, planned, now, now),
            )
        due = self.store.claim_due_slot("qa-planner", 3600)
        self.assertIsNotNone(due)
        self.assertEqual(due["slot_key"], slot)
        result = self.store.create_run(
            self.template["id"], slot, "auto", self.actor, Pages(rows), self.materials,
            planned_publish_at_utc=planned, expected_template_version=self.template["version"],
            expected_due_id=due["id"], expected_due_lease_owner=due["lease_owner"],
            expected_due_lease_expires_at_utc=due["lease_expires_at_utc"],
        )
        self.assertTrue(self.store.complete_due_slot(
            due["id"], run_id=result["run_id"], expected_lease_owner=due["lease_owner"],
            expected_lease_expires_at_utc=due["lease_expires_at_utc"],
        ))
        return result["run_id"]

    def extend(self, run_ids, rows=None, materials=None, **kwargs):
        rows = self.scope if rows is None else rows
        options = {"operation_id": "qa-extension", "expected_scope": scope_fingerprint(rows), "expected_version": self.template["version"]}
        options.update(kwargs)
        return extend_future_runs(self.store, run_ids, rows, self.materials if materials is None else materials, **options)

    def task_rows(self, run_id):
        with self.store.connect() as conn:
            return {row["page_id"]: dict(row) for row in conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id", (run_id,))}

    def ledger_state(self):
        tables = ("fb_auto_run", "fb_auto_run_page", "fb_auto_task", "fb_auto_due_slot", "fb_auto_publish_attempt", "fb_auto_publish_ledger")
        with self.store.connect() as conn:
            return {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in tables}

    def receipts(self):
        with self.store.connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_auto_pool_extension'").fetchone() is None:
                return []
            return [dict(row) for row in conn.execute("SELECT * FROM fb_auto_pool_extension ORDER BY operation_id,run_id")]

    def assert_rejected_without_changes(self, code, run_ids, **kwargs):
        before, receipts = self.ledger_state(), self.receipts()
        with self.assertRaises(StoreError) as caught:
            self.extend(run_ids, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.ledger_state(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_fingerprint_is_order_independent_and_tracks_language_and_authorization(self):
        original = scope_fingerprint(self.scope)
        self.assertEqual(scope_fingerprint(list(reversed(self.scope))), original)
        self.assertEqual(scope_fingerprint([replace(self.scope[0], language="English"), self.scope[1]]), original)
        for changed in (replace(self.scope[1], language="en"), replace(self.scope[1], eligible_token_count=0), replace(self.scope[1], group_id="18")):
            with self.subTest(changed=changed):
                self.assertNotEqual(scope_fingerprint([self.scope[0], changed]), original)

    def test_adds_only_missing_pages_preserving_existing_task_media_snapshot_and_attempt(self):
        run_id = self.seed_run()
        task_id = self.task_rows(run_id)["10001"]["id"]
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='ready',media_url='https://cdn.example/prepared.mp4',prepared_at_utc=? WHERE id=?", (utc_iso(self.clock()), task_id))
            frozen_page = dict(conn.execute("SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id='10001'", (run_id,)).fetchone())
            original_run = dict(conn.execute("SELECT * FROM fb_auto_run WHERE id=?", (run_id,)).fetchone())
        self.store.record_attempt(task_id, 1, credential_id="qa-credential", fb_user_id="qa-user", result_kind="definite_failure", error_code="qa-fixture")
        original_task = self.task_rows(run_id)["10001"]
        original_attempts = self.ledger_state()["fb_auto_publish_attempt"]
        result = self.extend([run_id])
        self.assertEqual(result[0]["added"], 1)
        tasks = self.task_rows(run_id)
        self.assertEqual(tasks["10001"], original_task)
        self.assertEqual(self.ledger_state()["fb_auto_publish_attempt"], original_attempts)
        self.assertEqual((tasks["10002"]["content_id"], tasks["10002"]["material_id"], tasks["10002"]["status"]), ("drama-es", "201", "planned"))
        self.assertIn("/es/201.mp4", tasks["10002"]["source_media_url"])
        self.assertEqual(tasks["10002"]["message_text"], "Drama es | Description es | " + tasks["10002"]["short_url"])
        with self.store.connect() as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id='10001'", (run_id,)).fetchone()), frozen_page)
            new_page = dict(conn.execute("SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id='10002'", (run_id,)).fetchone())
            updated_run = dict(conn.execute("SELECT * FROM fb_auto_run WHERE id=?", (run_id,)).fetchone())
        self.assertEqual(new_page["language"], "es")
        for field in ("id", "template_id", "template_version", "slot_key", "config_json", "planned_publish_at_utc", "created_at_utc", "metric_generation_ids_json"):
            self.assertEqual(updated_run[field], original_run[field])
        self.assertEqual((updated_run["total_pages"], updated_run["queued_tasks"]), (2, 2))
        self.assertEqual(json.loads(self.receipts()[0]["added_page_ids_json"]), ["10002"])

    def test_same_operation_retry_is_idempotent_and_changes_no_rows(self):
        run_id = self.seed_run()
        self.extend([run_id])
        before, receipts = self.ledger_state(), self.receipts()
        result = self.extend([run_id])
        self.assertTrue(result[0]["idempotent"])
        self.assertEqual(result[0]["added"], 0)
        self.assertEqual(self.ledger_state(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_receipt_retry_does_not_depend_on_catalog_remaining_available(self):
        run_id = self.seed_run()
        self.extend([run_id])
        before, receipts = self.ledger_state(), self.receipts()

        class UnavailableMaterials:
            def candidate_snapshot(self, _config):
                raise AssertionError("completed extension must use its durable receipt")

        result = self.extend([run_id], materials=UnavailableMaterials())
        self.assertTrue(result[0]["idempotent"])
        self.assertEqual(self.ledger_state(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_past_run_cannot_be_extended(self):
        run_id = self.seed_run()
        self.clock.value += timedelta(hours=2)
        self.assert_rejected_without_changes("fb_auto_extension_not_future", [run_id])

    def test_run_less_than_ten_minutes_away_cannot_be_extended(self):
        run_id = self.seed_run(minutes_ahead=9)
        self.assert_rejected_without_changes("fb_auto_extension_not_future", [run_id])

    def test_manual_run_cannot_be_extended(self):
        run_id = self.seed_run(trigger="manual")
        self.assert_rejected_without_changes("fb_auto_extension_not_future", [run_id])

    def test_material_shortage_on_second_run_rolls_back_all_additions_and_receipts(self):
        first, second = self.seed_run("first", 60), self.seed_run("second", 120)
        shortage = Materials({"en": (material(101, "en"),), "es": (material(201, "es"),)})
        self.assert_rejected_without_changes("fb_auto_extension_material_shortage", [first, second], materials=shortage)
        self.assertEqual(set(self.task_rows(first)), {"10001"})
        self.assertEqual(set(self.task_rows(second)), {"10001"})

    def test_changed_scope_hash_blocks_before_any_ledger_change(self):
        run_id = self.seed_run()
        changed = [self.scope[0], replace(self.scope[1], language="en")]
        self.assert_rejected_without_changes("fb_auto_extension_scope_changed", [run_id], rows=changed, expected_scope=scope_fingerprint(self.scope))

    def test_duplicate_page_scope_is_rejected(self):
        run_id = self.seed_run()
        self.assert_rejected_without_changes("fb_auto_extension_scope_changed", [run_id], rows=self.scope + [self.scope[1]])

    def test_expected_version_mismatch_is_rejected(self):
        run_id = self.seed_run()
        self.assert_rejected_without_changes("fb_auto_extension_version_changed", [run_id], expected_version=2)

    def test_disabled_template_is_rejected(self):
        run_id = self.seed_run()
        self.store.set_template_status(self.template["id"], False, self.actor, self.template["version"])
        self.assert_rejected_without_changes("fb_auto_extension_version_changed", [run_id])

    def test_missing_page_unknown_running_or_submitted_is_blocked(self):
        target = self.seed_run("target", 60)
        blocker = self.seed_run("blocker", 120, rows=[self.scope[1]], trigger="manual")
        for status in ("unknown", "running", "submitted"):
            with self.subTest(status=status):
                with self.store.connect() as conn:
                    conn.execute("UPDATE fb_auto_task SET status=? WHERE run_id=?", (status, blocker))
                self.assert_rejected_without_changes("fb_auto_page_unknown_block", [target])

    def test_same_page_and_time_in_another_run_is_blocked(self):
        target = self.seed_run("target", 60)
        self.seed_run("blocker", 60, rows=[self.scope[1]], trigger="manual")
        self.assert_rejected_without_changes("fb_auto_extension_duplicate_slot", [target])

    def test_existing_page_removed_from_live_pool_is_rejected(self):
        run_id = self.seed_run()
        self.assert_rejected_without_changes("fb_auto_extension_membership_changed", [run_id], rows=[self.scope[1]])

    def test_missing_language_or_credential_blocks_extension_without_fallback(self):
        run_id = self.seed_run()
        for replacement in (replace(self.scope[1], language=""), replace(self.scope[1], language="bad language"), replace(self.scope[1], eligible_token_count=0)):
            with self.subTest(replacement=replacement):
                self.assert_rejected_without_changes("fb_auto_extension_page_invalid", [run_id], rows=[self.scope[0], replacement])

    def test_missing_page_language_material_never_uses_original_page_language(self):
        run_id = self.seed_run()
        english_only = Materials({"en": (material(101, "en"),)})
        self.assert_rejected_without_changes("fb_auto_extension_material_shortage", [run_id], materials=english_only)

    def test_page_capacity_limit_is_enforced_before_additions(self):
        run_id = self.seed_run()
        self.assert_rejected_without_changes("fb_auto_capacity_exceeded", [run_id], max_pages=1)

    def test_publish_floor_changes_only_new_tasks(self):
        run_id = self.seed_run()
        before = self.task_rows(run_id)["10001"]
        floor = utc_iso(self.clock() + timedelta(hours=2))
        self.extend([run_id], publish_floor=floor)
        after = self.task_rows(run_id)
        self.assertEqual(after["10001"], before)
        self.assertEqual(after["10002"]["planned_publish_at_utc"], floor)


if __name__ == "__main__":
    unittest.main()

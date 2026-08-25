import json
import random
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageTarget
from scripts.fb_auto_post_targeted_backfill import (
    RecoveryError,
    _atomic_write_report,
    _validate_report_path,
    execute_backfill,
)
from scripts.test_fb_auto_validation import payload


TARGETS = (
    "1009871948881047",
    "1014456238423538",
    "1069327366257056",
    "761697440365789",
    "957642277435629",
)


class Pages:
    def __init__(self):
        self.counts = {
            TARGETS[0]: 2,
            TARGETS[1]: 2,
            TARGETS[2]: 1,
            TARGETS[3]: 0,
            TARGETS[4]: 3,
        }

    def legacy_conflicts(self, _ids):
        return []

    def list_pages(self, *_args, **_kwargs):
        rows = [
            PageTarget("6", ("6",), page_id, "248", "UTC", "english", count)
            for page_id, count in self.counts.items()
        ]
        rows.append(PageTarget("6", ("6",), "999999999999999", "248", "UTC", "english", 1))
        return rows


class Materials:
    def candidate_snapshot(self, _config):
        candidate = MaterialCandidate(
            "501",
            "drama1",
            "https://cdn.example/a.mp4",
            "M",
            "D",
            "english",
            Decimal("30"),
            Decimal("5"),
            Decimal("50"),
            Decimal("10"),
            Decimal("60"),
            "1",
        )
        return CandidateSnapshot((candidate,), (11,), ("2026-08-24",))

    def choose_from(self, candidates, excluded):
        return next((item for item in candidates if item.material_id not in set(excluded)), None)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 8, 25, 3, 30, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(
            Path(self.tmp.name) / "fb.sqlite3",
            now_fn=lambda: self.now,
            rng=random.Random(1),
        )
        self.actor = ActorScope("u", "测试", False, "248")
        self.template = self.store.create_template(payload(), self.actor, {"app_id": "1479", "product": "Dramawave"})
        self.store.set_template_status(self.template["id"], True, self.actor, 1)
        self.pages = Pages()
        self.materials = Materials()
        self._seed_source()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_source(self):
        with self.store.connect() as conn:
            config_json = conn.execute(
                "SELECT config_json FROM fb_auto_template_version WHERE template_id=1 AND version=1"
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO fb_auto_run(
                    template_id,template_version,slot_key,trigger_type,status,config_json,
                    total_pages,publishable_pages,missing_token_pages,queued_tasks,skipped_tasks,
                    created_at_utc,completed_at_utc,planned_publish_at_utc,metric_generation_ids_json,video_template
                ) VALUES(1,1,'auto:v1:2026-08-25:08:44','auto','completed',?,6,1,5,1,5,?,?,?,'[]','random_overlay')
                """,
                (
                    config_json,
                    "2026-08-23T16:51:56+00:00",
                    "2026-08-25T00:55:45+00:00",
                    "2026-08-25T00:44:00+00:00",
                ),
            )
            self.source_run_id = int(cursor.lastrowid)
            for page_id in TARGETS:
                conn.execute(
                    """
                    INSERT INTO fb_auto_task(
                        run_id,template_id,template_version,page_id,group_id,status,skip_reason,
                        created_at_utc,completed_at_utc,planned_publish_at_utc
                    ) VALUES(?,1,1,?,'6','skipped','fb_page_missing_eligible_token',?,?,?)
                    """,
                    (
                        self.source_run_id,
                        page_id,
                        "2026-08-23T16:51:56+00:00",
                        "2026-08-23T16:51:56+00:00",
                        "2026-08-25T00:44:00+00:00",
                    ),
                )
            conn.execute(
                """
                INSERT INTO fb_auto_task(
                    run_id,template_id,template_version,page_id,group_id,status,material_id,
                    created_at_utc,completed_at_utc,planned_publish_at_utc,graph_post_id
                ) VALUES(?,1,1,'888888888888888','6','published','400',?,?,?,?)
                """,
                (
                    self.source_run_id,
                    "2026-08-23T16:51:56+00:00",
                    "2026-08-25T00:55:45+00:00",
                    "2026-08-25T00:44:00+00:00",
                    "post_1",
                ),
            )

    def kwargs(self, **overrides):
        values = {
            "source_run_id": self.source_run_id,
            "expected_source_planned_at_utc": "2026-08-25T00:44:00+00:00",
            "expected_beijing_date": "2026-08-25",
            "page_ids": TARGETS,
            "operation_id": "20260825-run20-missing5",
            "now": self.now,
        }
        values.update(overrides)
        return values

    def execute(self, **overrides):
        return execute_backfill(self.store, self.pages, self.materials, **self.kwargs(**overrides))

    def test_validate_only_is_stable_and_does_not_create_run(self):
        with self.store.connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0]
        first = self.execute()
        second = self.execute()
        with self.store.connect() as conn:
            after = conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0]
        self.assertEqual(first["status"], "validated")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual((first["eligible_page_count"], first["blocked_page_count"]), (4, 1))
        self.assertEqual(first["target_page_ids"], list(TARGETS))
        self.assertEqual(before, after)

    def test_apply_creates_only_five_targets_and_is_idempotent(self):
        validated = self.execute()
        created = self.execute(apply=True, expected_fingerprint=validated["fingerprint"])
        repeated = self.execute(apply=True, expected_fingerprint=validated["fingerprint"])
        self.assertEqual(created["status"], "created")
        self.assertEqual(repeated["status"], "already_created")
        self.assertEqual(created["run_id"], repeated["run_id"])
        self.assertEqual([item["page_id"] for item in created["tasks"]], list(TARGETS))
        self.assertEqual(sum(item["status"] == "planned" for item in created["tasks"]), 4)
        self.assertEqual(
            [item["page_id"] for item in created["tasks"] if item["skip_reason"] == "fb_page_missing_eligible_token"],
            [TARGETS[3]],
        )
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_task WHERE run_id=?", (created["run_id"],)).fetchone()[0], 5)

    def test_old_fingerprint_is_rejected_after_eligibility_drift(self):
        validated = self.execute()
        self.pages.counts[TARGETS[0]] = 0
        with self.assertRaises(RecoveryError) as caught:
            self.execute(apply=True, expected_fingerprint=validated["fingerprint"])
        self.assertEqual(caught.exception.code, "fb_auto_backfill_fingerprint_changed")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 1)

    def test_target_must_equal_all_source_missing_pages(self):
        with self.assertRaises(RecoveryError) as caught:
            self.execute(page_ids=TARGETS[:-1])
        self.assertEqual(caught.exception.code, "fb_auto_backfill_source_scope_mismatch")

    def test_source_attempt_or_ledger_is_rejected(self):
        with self.store.connect() as conn:
            task_id = conn.execute(
                "SELECT id FROM fb_auto_task WHERE run_id=? AND page_id=?",
                (self.source_run_id, TARGETS[0]),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO fb_auto_publish_attempt(task_id,sequence,result_kind,created_at_utc) VALUES(?,1,'definite_failure',?)",
                (task_id, "2026-08-25T00:45:00+00:00"),
            )
        with self.assertRaises(RecoveryError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "fb_auto_backfill_source_not_pristine")

    def test_other_operation_for_same_source_is_rejected(self):
        validated = self.execute()
        self.execute(apply=True, expected_fingerprint=validated["fingerprint"])
        with self.assertRaises(RecoveryError) as caught:
            self.execute(operation_id="20260825-run20-second-op")
        self.assertEqual(caught.exception.code, "fb_auto_backfill_already_exists")

    def test_report_path_is_new_json_under_safe_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reports"
            root.mkdir()
            target = root / "run20.json"
            self.assertEqual(_validate_report_path(target, root), target.resolve())
            outside = Path(directory) / "outside.json"
            with self.assertRaises(RecoveryError):
                _validate_report_path(outside, root)
            target.write_text("old", encoding="utf-8")
            with self.assertRaises(RecoveryError):
                _validate_report_path(target, root)

    def test_atomic_report_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reports"
            root.mkdir()
            target = root / "run20.json"
            with patch("scripts.fb_auto_post_targeted_backfill.REPORT_ROOT", root):
                _atomic_write_report(target, {"ok": True, "status": "validated"})
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "validated")
                with self.assertRaises(RecoveryError):
                    _atomic_write_report(target, {"ok": False})


if __name__ == "__main__":
    unittest.main()

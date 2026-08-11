#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_posts.core import TTPostStore
from scripts.requeue_tt_post_failed_intakes import (
    FailedIntakeRecoveryError,
    apply_recovery,
    plan_recovery,
)


SOURCE_PROFILE = "tt-post-source-direct-v1"
TARGET_PROFILE = "tt-post-random-overlay-hevc-720x1280-v3"


class FailedIntakeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = str(root / "tt-post.sqlite3")
        self.auto_db_path = str(root / "tt-auto-post.sqlite3")
        self.lock_path = str(root / "recovery.lock")
        self.store = TTPostStore(self.db_path)
        with contextlib.closing(sqlite3.connect(self.auto_db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE tt_auto_material_ledger (
                    material_id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def _failed(self, material_id: str, *, language: str = "en", error_code: str = "prepared_media_invalid"):
        content_id = "C%09d" % int(material_id)
        item = self.store.add_material_intake(
            material_id,
            "642",
            content_id,
            "https://cdn.example.com/%s.mp4" % material_id,
            idempotency_key="test-intake-%s" % material_id,
            gpu_job_id="ttpreview-old-%s" % material_id,
            source_trim_tail_seconds=0,
            preparation_profile=SOURCE_PROFILE,
            caption_template="{{content_id}}",
            caption=content_id,
            consent_version="tt-post-consent-v1",
            consented_at="2026-08-11T00:00:00Z",
            is_aigc=False,
            material_name="material %s" % material_id,
            drama_name="drama %s" % material_id,
            material_language=language,
            material_tag="safe",
            description="description",
        )
        claim = self.store.claim_material_intake("test-worker")
        self.assertEqual(int(item["id"]), int(claim.item["id"]))
        return self.store.fail_material_intake(
            item["id"],
            claim.reveal_claim_token(),
            error_code=error_code,
            error_message="old profile mismatch",
        )

    def _plan(self):
        return plan_recovery(
            self.db_path,
            self.auto_db_path,
            source_profile=SOURCE_PROFILE,
            target_profile=TARGET_PROFILE,
            source_trim_tail_seconds=0,
            allow_any_db_path=True,
        )

    def test_dry_run_then_apply_requeues_only_exact_english_failures(self):
        first = self._failed("1001")
        second = self._failed("1002")
        filipino = self._failed("1003", language="tl")
        other = self._failed("1004", error_code="network_failed")

        plan = self._plan()
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["candidate_intake_ids"], [first["id"], second["id"]])
        self.assertEqual(len(plan["candidate_set_sha256"]), 64)

        result = apply_recovery(
            self.db_path,
            self.auto_db_path,
            expected_candidate_sha256=plan["candidate_set_sha256"],
            source_profile=SOURCE_PROFILE,
            target_profile=TARGET_PROFILE,
            source_trim_tail_seconds=0,
            actor="codex-test",
            operation_id="test-recovery-20260811",
            lock_path=self.lock_path,
            allow_any_db_path=True,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["requeued_count"], 2)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            recovered = conn.execute(
                "SELECT * FROM tt_post_material_intake WHERE id=?", (first["id"],)
            ).fetchone()
            self.assertEqual(recovered["status"], "queued")
            self.assertEqual(recovered["attempt_count"], 0)
            self.assertEqual(recovered["preparation_profile"], TARGET_PROFILE)
            self.assertTrue(str(recovered["gpu_job_id"]).startswith("ttpreview-"))
            self.assertEqual(recovered["error_code"], "")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_material_intake_recovery_audit"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM tt_post_material_intake WHERE id=?",
                    (filipino["id"],),
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM tt_post_material_intake WHERE id=?",
                    (other["id"],),
                ).fetchone()[0],
                "failed",
            )

        replay = self.store.add_material_intake(
            "1001",
            "642",
            "C000001001",
            "https://cdn.example.com/1001.mp4",
            idempotency_key="test-intake-1001",
            gpu_job_id=recovered["gpu_job_id"],
            source_trim_tail_seconds=0,
            preparation_profile=TARGET_PROFILE,
            caption_template="{{content_id}}",
            caption="C000001001",
            consent_version="tt-post-consent-v1",
            consented_at="2026-08-11T00:00:00Z",
            is_aigc=False,
            material_name="material 1001",
            drama_name="drama 1001",
            material_language="en",
            material_tag="safe",
            description="description",
        )
        self.assertEqual(replay["status"], "queued")
        self.assertEqual(self._plan()["candidate_count"], 0)

    def test_candidate_hash_mismatch_is_atomic(self):
        row = self._failed("2001")
        with self.assertRaises(FailedIntakeRecoveryError) as caught:
            apply_recovery(
                self.db_path,
                self.auto_db_path,
                expected_candidate_sha256="0" * 64,
                source_profile=SOURCE_PROFILE,
                target_profile=TARGET_PROFILE,
                source_trim_tail_seconds=0,
                actor="codex-test",
                operation_id="test-recovery-hash-mismatch",
                lock_path=self.lock_path,
                allow_any_db_path=True,
            )
        self.assertEqual(caught.exception.code, "tt_post_failed_intake_candidate_changed")
        self.assertEqual(self.store.get_material_intake(row["id"])["status"], "failed")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tt_post_material_intake_recovery_audit'"
                ).fetchone()
            )

    def test_cross_ledger_overlap_fails_closed(self):
        self._failed("3001")
        with contextlib.closing(sqlite3.connect(self.auto_db_path)) as conn:
            conn.execute(
                "INSERT INTO tt_auto_material_ledger(material_id,task_id) VALUES('3001',1)"
            )
            conn.commit()
        with self.assertRaises(FailedIntakeRecoveryError) as caught:
            self._plan()
        self.assertEqual(caught.exception.code, "tt_post_failed_intake_lineage_conflict")


if __name__ == "__main__":
    unittest.main()

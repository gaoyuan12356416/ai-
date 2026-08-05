import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_posts.core import TTPostStore, render_caption_template
from scripts.upgrade_tt_post_recurring_profile import (
    ProfileUpgradeError,
    ProfileUpgradeRunner,
)


OLD_PROFILE = "tt-post-direct-outro-hevc-720x1280-v1"
NEW_PROFILE = "tt-post-direct-outro-hevc-720x1280-v2"
UNIT_PATH = REPO_ROOT / "deploy" / "tt-post-profile-upgrade.service"


class ProfileUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "tt-post.sqlite3"
        self.store = TTPostStore(self.db_path)
        self.intake = self.store.add_material_intake(
            "99001",
            "acct-1",
            "CONTENT_99001",
            "https://cdn.example.com/source-99001.mp4",
            idempotency_key="tt-post-intake:99001",
            gpu_job_id="gpu-intake-job-99001",
            source_trim_tail_seconds=0,
            preparation_profile=OLD_PROFILE,
            caption_template="Drama ID: {{contect_id}}",
            caption=render_caption_template(
                "Drama ID: {{contect_id}}",
                "CONTENT_99001",
            ),
            consent_version="tt-post-recurring-v1",
            consented_at="2026-08-05 10:00:00",
            is_aigc=False,
            material_name="material-99001",
            drama_name="Drama 99001",
            material_language="English",
            description="",
            actor_user_id="operator-1",
            actor_name="Operator",
        )
        claimed = self.store.claim_material_intake(
            "prepare-worker",
            lease_seconds=60,
        )
        self.ready = self.store.complete_material_intake(
            self.intake["id"],
            claimed.reveal_claim_token(),
            gpu_job_id=self.intake["gpu_job_id"],
            prepared_media_url="https://gpu.example.com/old-99001.mp4",
            prepared_output_sha256="a" * 64,
            prepared_output_size=123456,
            prepared_duration_sec=120.25,
            preparation_profile=OLD_PROFILE,
            source_trim_tail_seconds=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def prepared_response(**request):
        job_id = request["job_id"]
        material = request["material"]
        return {
            "content_id": material["content_id"],
            "job_id": job_id,
            "output_sha256": hashlib.sha256(job_id.encode("utf-8")).hexdigest(),
            "output_size": 654321,
            "output_url": "https://gpu.example.com/%s.mp4" % job_id,
            "probe": {"duration": 127.75},
            "profile": request["expected_profile"],
            "status": "ready",
        }

    def test_dry_run_lists_only_and_apply_updates_both_ledgers(self):
        calls = []

        def prepare(**request):
            calls.append(request)
            return self.prepared_response(**request)

        runner = ProfileUpgradeRunner(
            self.store,
            prepare,
            target_profile=NEW_PROFILE,
            source_trim_tail_seconds=0,
        )
        dry_run = runner.run(OLD_PROFILE, limit=10, apply=False)
        self.assertEqual(1, dry_run["candidate_count"])
        self.assertEqual([], calls)

        applied = runner.run(OLD_PROFILE, limit=10, apply=True)
        self.assertEqual(1, applied["upgraded_count"])
        self.assertEqual(1, len(calls))
        pool = self.store.list_recurring_materials(account_id="acct-1")[0]
        intake = self.store.get_material_intake(self.intake["id"])
        self.assertEqual(NEW_PROFILE, pool["preparation_profile"])
        self.assertEqual(NEW_PROFILE, intake["preparation_profile"])
        self.assertEqual(pool["gpu_job_id"], intake["gpu_job_id"])
        self.assertEqual(
            pool["prepared_output_sha256"],
            intake["prepared_output_sha256"],
        )
        self.assertEqual("available", pool["status"])
        self.assertEqual("ready", intake["status"])
        self.assertRegex(intake["request_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            [],
            self.store.list_available_recurring_profile_upgrades(
                OLD_PROFILE,
                NEW_PROFILE,
            ),
        )

    def test_invalid_gpu_identity_leaves_old_artifact_untouched(self):
        def prepare(**request):
            response = self.prepared_response(**request)
            response["profile"] = OLD_PROFILE
            return response

        runner = ProfileUpgradeRunner(
            self.store,
            prepare,
            target_profile=NEW_PROFILE,
            source_trim_tail_seconds=0,
        )
        with self.assertRaises(ProfileUpgradeError):
            runner.run(OLD_PROFILE, limit=10, apply=True)

        pool = self.store.list_recurring_materials(account_id="acct-1")[0]
        intake = self.store.get_material_intake(self.intake["id"])
        self.assertEqual(OLD_PROFILE, pool["preparation_profile"])
        self.assertEqual(OLD_PROFILE, intake["preparation_profile"])
        self.assertEqual("a" * 64, pool["prepared_output_sha256"])
        self.assertEqual("a" * 64, intake["prepared_output_sha256"])

    def test_atomic_fence_rejects_a_reserved_pool_row(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool SET status='reserved' WHERE id=?",
                (self.ready["recurring_pool_id"],),
            )
            conn.commit()
        runner = ProfileUpgradeRunner(
            self.store,
            self.prepared_response,
            target_profile=NEW_PROFILE,
            source_trim_tail_seconds=0,
        )
        result = runner.run(OLD_PROFILE, limit=10, apply=True)
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["upgraded_count"])

    def test_deploy_unit_is_exact_profile_scoped_and_sandboxed(self):
        unit = UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("ConditionPathIsMountPoint=/mnt/data-disk", unit)
        self.assertIn("--from-profile " + OLD_PROFILE, unit)
        self.assertIn("--to-profile " + NEW_PROFILE, unit)
        self.assertIn("--apply", unit)
        self.assertIn("User=tt-post", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn(
            "ReadWritePaths=/mnt/data-disk/tt-post-publisher /run/tt-post",
            unit,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

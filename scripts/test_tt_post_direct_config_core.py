#!/usr/bin/env python3
"""Core-only tests for atomic TT config and repeatable direct tests."""

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_posts import (  # noqa: E402
    FIXED_CAPTION_TEMPLATE,
    LiveGates,
    SafeAccount,
    TTPostError,
    TTPostStore,
    render_caption_template,
)
from features.tt_posts.links import (  # noqa: E402
    build_short_url,
    build_w2a_url,
)


UTC = timezone.utc
DIRECT_TEMPLATE = "Drama {{content_id}}\n{url}\n{desc}"


class MutableClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current


class DirectConfigCoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "tt.sqlite3"
        self.clock = MutableClock(datetime(2026, 8, 3, 2, 0, tzinfo=UTC))
        self.store = TTPostStore(self.db_path, now_fn=self.clock)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def settings():
        return {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True,
            "allow_duet": False,
            "allow_stitch": False,
            "brand_content_toggle": False,
            "brand_organic_toggle": True,
            "is_aigc": True,
        }

    def save_config(self, **overrides):
        values = {
            "expected_version": 0,
            "enabled": True,
            "publish_times": ["11:00"],
            "account_ids": ["acct-a", "acct-b"],
            "caption_template": DIRECT_TEMPLATE,
            "user_consent": True,
            "consent_version": "tt-auto-v3",
            "consented_at": "2026-08-03T02:00:00Z",
            "actor_user_id": "operator-1",
            "actor_name": "Operator",
        }
        values.update(overrides)
        return self.store.save_auto_publish_config(**values)

    def create_direct(
        self,
        *,
        material_id="1001",
        idempotency_key="direct-request-0001",
        gpu_job_id="direct-gpu-0001",
        short_link_id=8_500_000_000_000_001_001,
    ):
        short_url = build_short_url(short_link_id)
        description = "A hidden heir returns to protect his family."
        caption = render_caption_template(
            DIRECT_TEMPLATE,
            "Drama_1001",
            url=short_url,
            description=description,
        )
        return self.store.create_direct_test(
            material_id,
            "acct-a",
            "Drama_1001",
            "https://source.example.com/video.mp4",
            idempotency_key=idempotency_key,
            gpu_job_id=gpu_job_id,
            source_trim_tail_seconds=2,
            preparation_profile="tt-direct-v1",
            caption_template=DIRECT_TEMPLATE,
            caption=caption,
            short_link_id=short_link_id,
            short_url=short_url,
            settings=self.settings(),
            consent_version="tt-direct-consent-v1",
            consented_at="2026-08-03T02:00:00Z",
            config_version=7,
            material_name="episode clip",
            drama_name="Hidden Heir",
            material_language="en",
            material_tag="romance",
            description=description,
            account_username="dramawave",
            account_display_name="DramaWave",
            creator_nickname_snapshot="DramaWave",
            creator_username_snapshot="dramawave",
            creator_info_hash="a" * 64,
            creator_info_synced_at="2026-08-03T01:59:00Z",
            actor_user_id="operator-1",
            actor_name="Operator",
        )

    def prepare_direct(self, item):
        claims = self.store.claim_direct_test_prepare(
            "gpu-worker-1",
            lease_seconds=120,
            limit=20,
        )
        claim = next(
            value for value in claims if value.direct_test_id == item["id"]
        )
        token = claim.reveal_claim_token()
        self.store.renew_direct_test_prepare(
            item["id"],
            token,
            lease_seconds=120,
        )
        return self.store.complete_direct_test_prepare(
            item["id"],
            token,
            gpu_job_id=item["gpu_job_id"],
            prepared_media_url=(
                "https://socialkit-cdn.yingliang.tech/tt/%s.mp4" % item["id"]
            ),
            prepared_output_sha256="b" * 64,
            prepared_output_size=123456,
            prepared_duration_sec=61.5,
            source_trim_tail_seconds=item["source_trim_tail_seconds"],
            preparation_profile=item["preparation_profile"],
        )

    def publish_direct(self, item, remote_suffix="1"):
        claim = self.store.claim_direct_test_publish(
            item["id"],
            "publish-worker-1",
            lease_seconds=120,
        )
        token = claim.reveal_claim_token()
        self.store.renew_direct_test_publish(
            item["id"],
            token,
            lease_seconds=120,
        )
        long_url = build_w2a_url(
            {
                "username": "dramawave",
                "timestamp": 1785722400,
                "material_language": "en",
                "drama_name": "Hidden Heir",
                "tag": "romance",
                "link_id": item["short_link_id"],
                "page_name": "DramaWave",
                "page_id": item["account_id"],
                "material_name": "episode clip",
                "material_id": item["material_id"],
                "queue_id": item["id"],
                "content_id": item["content_id"],
            }
        )
        self.store.prepare_direct_test_short_link(item["id"], token, long_url)
        publish_id = "v_pub_url~v2-1.900%s" % remote_suffix
        self.store.record_direct_test_publish_id(
            item["id"],
            token,
            publish_id,
        )
        return self.store.reconcile_direct_test_published(
            item["id"],
            publish_id,
            publish_url="https://www.tiktok.com/@dramawave/video/900%s"
            % remote_suffix,
        )

    def test_storage_migration_is_idempotent_and_preserves_old_rows(self):
        old = self.store.add_material("9001")
        before = self.store.get_material(old["id"])
        TTPostStore(self.db_path, now_fn=self.clock)
        TTPostStore(self.db_path, now_fn=self.clock)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            direct_indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='tt_post_direct_test' "
                    "AND sql IS NOT NULL"
                )
            }
        self.assertIn("tt_post_auto_publish_config", names)
        self.assertIn("tt_post_direct_test", names)
        self.assertEqual(
            {
                "idx_tt_post_direct_test_prepare",
                "idx_tt_post_direct_test_publish",
                "idx_tt_post_direct_test_material",
                "ux_tt_post_direct_test_active_material",
                "ux_tt_post_direct_test_publish_id",
                "ux_tt_post_direct_test_short_link",
            },
            direct_indexes,
        )
        self.assertEqual("ok", integrity)
        self.assertEqual(before, self.store.get_material(old["id"]))

    def test_atomic_config_same_minute_remove_disable_and_rollback(self):
        initial = self.store.get_auto_publish_config()
        self.assertEqual(0, initial["version"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_auto_publish_config"
                ).fetchone()[0],
            )

        saved = self.save_config()
        self.assertEqual(1, saved["version"])
        self.assertEqual(["acct-a", "acct-b"], saved["account_ids"])
        self.assertEqual(
            ["11:00"],
            self.store.get_daily_schedule("acct-a")["publish_times"],
        )
        self.assertEqual(
            ["11:00"],
            self.store.get_daily_schedule("acct-b")["publish_times"],
        )

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_second_schedule
                BEFORE UPDATE ON tt_post_daily_schedule
                WHEN NEW.account_id='acct-b'
                BEGIN SELECT RAISE(ABORT, 'forced atomic rollback'); END
                """
            )
            conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.save_config(
                expected_version=1,
                publish_times=["12:00"],
            )
        self.assertEqual(1, self.store.get_auto_publish_config()["version"])
        self.assertEqual(
            ["11:00"],
            self.store.get_daily_schedule("acct-a")["publish_times"],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DROP TRIGGER fail_second_schedule")
            conn.commit()

        reduced = self.save_config(
            expected_version=1,
            account_ids=["acct-a"],
        )
        self.assertEqual(2, reduced["version"])
        self.assertFalse(self.store.get_daily_schedule("acct-b")["enabled"])
        disabled = self.store.save_auto_publish_config(
            expected_version=2,
            enabled=False,
        )
        self.assertEqual(["acct-a"], disabled["account_ids"])
        self.assertEqual(DIRECT_TEMPLATE, disabled["caption_template"])
        self.assertFalse(self.store.get_daily_schedule("acct-a")["enabled"])

        with self.assertRaises(TTPostError) as stale:
            self.save_config(expected_version=1)
        self.assertEqual(
            "tt_post_auto_config_version_conflict",
            stale.exception.code,
        )

    def test_first_disabled_config_needs_no_consent_and_fabricates_no_schedule(self):
        saved = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=False,
            publish_times=["11:00"],
            account_ids=["acct-a"],
            caption_template=DIRECT_TEMPLATE,
        )
        self.assertEqual(1, saved["version"])
        self.assertFalse(saved["enabled"])
        self.assertFalse(saved["user_consent"])
        self.assertEqual(["acct-a"], saved["account_ids"])
        self.assertEqual(0, self.store.get_daily_schedule("acct-a")["version"])
        self.assertEqual([], self.store.list_daily_schedules())

        with self.assertRaises(TTPostError) as missing_consent:
            self.store.save_auto_publish_config(
                expected_version=1,
                enabled=True,
                publish_times=["11:00"],
                account_ids=["acct-a"],
                caption_template=DIRECT_TEMPLATE,
            )
        self.assertEqual("tt_post_consent_required", missing_consent.exception.code)

        legacy = self.store.save_daily_schedule(
            "acct-a",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="legacy-v1",
            consented_at="2026-08-03T02:00:00Z",
        )
        stopped = self.store.save_auto_publish_config(
            expected_version=1,
            enabled=False,
            publish_times=["12:00"],
            account_ids=["acct-a"],
            caption_template=DIRECT_TEMPLATE,
        )
        self.assertEqual(2, stopped["version"])
        projected = self.store.get_daily_schedule("acct-a")
        self.assertFalse(projected["enabled"])
        self.assertTrue(projected["user_consent"])
        self.assertEqual(legacy["consent_version"], projected["consent_version"])
        self.assertEqual(legacy["consented_at_utc"], projected["consented_at_utc"])

    def test_mixed_legacy_schedules_require_disabled_explicit_review(self):
        common = {
            "enabled": True,
            "expected_version": 0,
            "consent_version": "legacy-v1",
            "consented_at": "2026-08-03T02:00:00Z",
        }
        self.store.save_daily_schedule("acct-a", ["10:00"], **common)
        self.store.save_daily_schedule("acct-b", ["11:00"], **common)
        projected = self.store.get_auto_publish_config()
        self.assertEqual(0, projected["version"])
        self.assertTrue(projected["legacy_review_required"])
        self.assertEqual("mixed", projected["legacy_schedule_mode"])
        self.assertEqual([], projected["publish_times"])

        with self.assertRaises(TTPostError):
            self.store.save_auto_publish_config(
                expected_version=0,
                enabled=False,
            )
        with self.assertRaises(TTPostError):
            self.store.save_auto_publish_config(
                expected_version=0,
                enabled=True,
                publish_times=["12:00"],
            )
        reviewed = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=False,
            publish_times=["12:00"],
            account_ids=["acct-a", "acct-b"],
            caption_template=DIRECT_TEMPLATE,
        )
        self.assertEqual(1, reviewed["version"])
        self.assertFalse(reviewed["legacy_review_required"])
        self.assertEqual(
            ["12:00"],
            self.store.get_daily_schedule("acct-a")["publish_times"],
        )
        self.assertFalse(self.store.get_daily_schedule("acct-b")["enabled"])

    def test_paused_legacy_schedule_remains_selected_without_writing_config(self):
        saved = self.store.save_daily_schedule(
            "acct-a",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="legacy-v1",
            consented_at="2026-08-03T02:00:00Z",
        )
        self.store.disable_daily_schedule(
            "acct-a",
            expected_version=saved["version"],
        )
        projected = self.store.get_auto_publish_config()
        self.assertEqual(0, projected["version"])
        self.assertFalse(projected["enabled"])
        self.assertEqual(["acct-a"], projected["account_ids"])
        self.assertEqual(["10:00"], projected["publish_times"])
        self.assertEqual("paused", projected["legacy_membership_mode"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_auto_publish_config"
                ).fetchone()[0],
            )

    def test_direct_test_repeats_material_but_fences_identities(self):
        first = self.create_direct()
        replay = self.create_direct()
        self.assertEqual(first["id"], replay["id"])
        first = self.prepare_direct(first)
        self.publish_direct(first)
        second = self.create_direct(
            idempotency_key="direct-request-0002",
            gpu_job_id="direct-gpu-0002",
            short_link_id=8_500_000_000_000_001_002,
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["material_id"], second["material_id"])

        with self.assertRaises(TTPostError) as idem:
            self.create_direct(
                material_id="1002",
                idempotency_key="direct-request-0001",
                gpu_job_id="direct-gpu-0003",
                short_link_id=8_500_000_000_000_001_003,
            )
        self.assertEqual(
            "tt_post_direct_test_idempotency_conflict",
            idem.exception.code,
        )
        with self.assertRaises(TTPostError) as gpu:
            self.create_direct(
                material_id="1002",
                idempotency_key="direct-request-0003",
                gpu_job_id="direct-gpu-0002",
                short_link_id=8_500_000_000_000_001_003,
            )
        self.assertEqual(
            "tt_post_direct_test_gpu_job_conflict",
            gpu.exception.code,
        )
        with self.assertRaises(TTPostError) as link:
            self.create_direct(
                material_id="1002",
                idempotency_key="direct-request-0004",
                gpu_job_id="direct-gpu-0004",
                short_link_id=8_500_000_000_000_001_002,
            )
        self.assertEqual(
            "tt_post_direct_test_short_link_conflict",
            link.exception.code,
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_recurring_pool"
                ).fetchone()[0],
            )

    def test_direct_prepare_publish_and_unknown_lease_lifecycle(self):
        first = self.create_direct()
        ready = self.prepare_direct(first)
        self.assertEqual("ready", ready["status"])
        published = self.publish_direct(ready)
        self.assertEqual("published", published["status"])
        self.assertTrue(published["allow_comment"])
        state = self.store.get_material_publication_state("1001")
        self.assertEqual("published", state["publication_state"])
        self.assertEqual(1, state["publish_count"])

        second = self.create_direct(
            material_id="2001",
            idempotency_key="direct-request-2001",
            gpu_job_id="direct-gpu-2001",
            short_link_id=8_500_000_000_000_002_001,
        )
        second = self.prepare_direct(second)
        self.store.claim_direct_test_publish(
            second["id"],
            "publish-worker-2",
            lease_seconds=1,
        )
        self.clock.current += timedelta(seconds=2)
        with self.assertRaises(TTPostError) as stale:
            self.store.claim_direct_test_publish(
                second["id"],
                "publish-worker-3",
            )
        self.assertEqual(
            "tt_post_direct_test_outcome_unknown",
            stale.exception.code,
        )
        unknown = self.store.get_direct_test(second["id"])
        self.assertEqual("unknown", unknown["status"])
        self.assertTrue(unknown["unknown_outcome"])
        self.assertEqual(
            "unknown",
            self.store.get_material_publication_state("2001")[
                "publication_state"
            ],
        )
        recovered = self.store.recover_direct_test_publish_id(
            second["id"],
            "v_pub_url~v2-1.2001",
        )
        self.assertEqual("reconciling", recovered["status"])
        replay = self.store.recover_direct_test_publish_id(
            second["id"],
            "v_pub_url~v2-1.2001",
        )
        self.assertEqual(recovered, replay)
        with self.assertRaises(TTPostError):
            self.store.recover_direct_test_publish_id(
                second["id"],
                "v_pub_url~v2-1.2002",
            )
        recovered = self.store.reconcile_direct_test_published(
            second["id"],
            "v_pub_url~v2-1.2001",
        )
        self.assertEqual("published", recovered["status"])

    def test_publication_aggregate_combines_legacy_and_direct(self):
        pool = self.store.add_material("3001")
        account = SafeAccount(
            account_id="acct-a",
            username="dramawave",
            display_name="DramaWave",
            avatar_url="https://example.com/avatar.jpg",
            status="active",
            publish_eligible=True,
        )
        queue = self.store.freeze_queue(
            pool["id"],
            account,
            "2026-08-03 10:00:00",
            FIXED_CAPTION_TEMPLATE,
            {
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "allow_duet": False,
                "allow_stitch": False,
                "brand_content_toggle": False,
                "brand_organic_toggle": True,
                "user_consent": True,
                "consent_version": "legacy-v1",
                "consented_at": "2026-08-03 10:00:00",
            },
            lambda material_id: {
                "material_id": material_id,
                "content_id": "Drama_1001",
                "media_url": "https://cdn.example.com/legacy.mp4",
            },
        )
        claims = self.store.claim_due(
            "legacy-worker",
            now=self.clock.current + timedelta(seconds=10),
        )
        legacy_claim = claims[0]
        legacy_token = legacy_claim.reveal_claim_token()
        self.store.begin_publish(
            queue["id"],
            legacy_token,
            LiveGates(True, True, True),
            now=self.clock.current + timedelta(seconds=10),
        )
        self.store.record_publish_id(
            queue["id"],
            legacy_token,
            "v_pub_url~v2-1.3001",
        )
        self.store.reconcile_published(
            queue["id"],
            "v_pub_url~v2-1.3001",
            publish_url="https://www.tiktok.com/@dramawave/video/3001",
        )

        direct = self.create_direct(
            material_id="3001",
            idempotency_key="direct-request-3001",
            gpu_job_id="direct-gpu-3001",
            short_link_id=8_500_000_000_000_003_001,
        )
        direct = self.prepare_direct(direct)
        self.publish_direct(direct, remote_suffix="3002")
        state = self.store.get_material_publication_state("3001")
        self.assertEqual("published", state["publication_state"])
        self.assertEqual(2, state["publish_count"])
        self.assertEqual(
            "https://www.tiktok.com/@dramawave/video/9003002",
            state["latest_publish_url"],
        )


if __name__ == "__main__":
    unittest.main()

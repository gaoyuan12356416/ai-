#!/usr/bin/env python3
"""Standalone tests for the TikTok Post pool core."""

import os
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
    AccountSourceError,
    FIXED_CAPTION_TEMPLATE,
    LiveGates,
    PublishCredentials,
    SafeAccount,
    SnapshotAccountSource,
    TTPostAccountSettings,
    TTPostError,
    TTPostPolicy,
    TTPostStore,
    beijing_to_utc,
    render_fixed_caption,
    render_caption_template,
)


UTC = timezone.utc
OPEN_GATES = LiveGates(True, True, True)
CAPTION = FIXED_CAPTION_TEMPLATE


def policy(consented_at="2026-07-29 10:00:00"):
    return {
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comment": True,
        "allow_duet": False,
        "allow_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": True,
        "user_consent": True,
        "consent_version": "tt-post-v1",
        "consented_at": consented_at,
    }


def account(account_id="acct-1"):
    return SafeAccount(
        account_id=account_id,
        username="dramawave",
        display_name="DramaWave",
        avatar_url="https://example.com/avatar.jpg",
        status="active",
        publish_eligible=True,
    )


def resolver(material_id):
    return {
        "material_id": material_id,
        "content_id": "Y9v1yQcFqM",
        "media_url": "https://cdn.example.com/material.mp4",
    }


def account_settings(**overrides):
    values = {
        "privacy_level": "SELF_ONLY",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": True,
        "is_aigc": True,
    }
    values.update(overrides)
    return TTPostAccountSettings.from_mapping(values)


class MutableClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "tt-posts.sqlite3"
        self.clock = MutableClock(datetime(2026, 7, 29, 2, 0, tzinfo=UTC))
        self.store = TTPostStore(self.db_path, now_fn=self.clock)
        self.recurring_execution_tokens = {}

    def tearDown(self):
        self.tempdir.cleanup()

    def add_and_freeze(
        self,
        material_id="1001",
        account_value=None,
        scheduled="2026-07-29 10:00:00",
        template=CAPTION,
    ):
        pool = self.store.add_material(material_id)
        return self.store.freeze_queue(
            pool["id"],
            account_value or account(),
            scheduled,
            template,
            policy(),
            resolver,
        )

    def claim_one(self, queue, now=None):
        current = now or datetime(2026, 7, 29, 2, 0, 10, tzinfo=UTC)
        claims = self.store.claim_due("worker-1", now=current)
        self.assertEqual(1, len(claims))
        self.assertEqual(queue["id"], claims[0].queue_id)
        return claims[0]


class StorageTests(CoreTestCase):
    def test_storage_has_legacy_four_plus_exactly_three_recurring_tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'tt_post_%'"
                )
            }
        finally:
            conn.close()
        self.assertEqual(
            {
                "tt_post_material_pool",
                "tt_post_queue",
                "tt_post_event",
                "tt_post_account_setting",
                "tt_post_daily_schedule",
                "tt_post_recurring_pool",
                "tt_post_schedule_run",
            },
            names,
        )

    def test_additive_migration_is_idempotent_and_preserves_legacy_data(self):
        legacy = self.store.add_material("9001")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TABLE tt_post_schedule_run")
            conn.execute("DROP TABLE tt_post_recurring_pool")
            conn.execute("DROP TABLE tt_post_daily_schedule")
            conn.commit()
        finally:
            conn.close()

        migrated = TTPostStore(self.db_path, now_fn=self.clock)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DROP INDEX idx_tt_post_schedule_run_recovery"
            )
            conn.execute(
                "ALTER TABLE tt_post_schedule_run "
                "DROP COLUMN execution_token"
            )
            conn.execute(
                "ALTER TABLE tt_post_schedule_run "
                "DROP COLUMN execution_lease_expires_at_utc"
            )
            conn.commit()
        finally:
            conn.close()
        replay = TTPostStore(self.db_path, now_fn=self.clock)
        conn = sqlite3.connect(self.db_path)
        try:
            migrated_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(tt_post_schedule_run)"
                )
            }
            migrated_indexes = {
                row[1]
                for row in conn.execute(
                    "PRAGMA index_list(tt_post_schedule_run)"
                )
            }
        finally:
            conn.close()
        self.assertIn("execution_token", migrated_columns)
        self.assertIn(
            "execution_lease_expires_at_utc",
            migrated_columns,
        )
        self.assertIn(
            "idx_tt_post_schedule_run_recovery",
            migrated_indexes,
        )
        self.assertEqual(legacy, migrated.get_material(legacy["id"]))
        self.assertEqual(
            migrated.get_daily_schedule("acct-1"),
            replay.get_daily_schedule("acct-1"),
        )

    def test_account_settings_are_required_versioned_and_boolean_safe(self):
        self.assertIsNone(self.store.get_account_settings("acct-1"))
        with self.assertRaises(TTPostError) as caught:
            self.store.get_account_settings("acct-1", required=True)
        self.assertEqual("tt_account_settings_required", caught.exception.code)

        created = self.store.save_account_settings(
            "acct-1",
            account_settings(),
            expected_version=0,
        )
        self.assertEqual(created["account_id"], "acct-1")
        self.assertEqual(created["version"], 1)
        self.assertTrue(created["configured"])
        self.assertTrue(created["commercial_disclosure"])
        self.assertFalse(created["allow_comment"])
        self.assertTrue(created["is_aigc"])

        self.clock.current += timedelta(minutes=1)
        updated = self.store.save_account_settings(
            "acct-1",
            account_settings(
                privacy_level="PUBLIC_TO_EVERYONE",
                allow_comment=True,
                brand_organic_toggle=False,
                is_aigc=False,
            ),
            expected_version=1,
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["privacy_level"], "PUBLIC_TO_EVERYONE")
        self.assertTrue(updated["allow_comment"])
        self.assertFalse(updated["commercial_disclosure"])
        self.assertFalse(updated["is_aigc"])
        self.assertEqual(self.store.list_account_settings(), [updated])

        with self.assertRaises(TTPostError) as conflict:
            self.store.save_account_settings(
                "acct-1",
                account_settings(),
                expected_version=1,
            )
        self.assertEqual(
            "tt_account_settings_version_conflict",
            conflict.exception.code,
        )

    def test_account_settings_batch_is_atomic_and_versioned(self):
        first = self.store.save_account_settings(
            "acct-1",
            account_settings(),
            expected_version=0,
        )
        self.assertEqual(first["version"], 1)

        saved = self.store.save_account_settings_batch(
            [
                {
                    "account_id": "acct-1",
                    "settings": account_settings(
                        privacy_level="PUBLIC_TO_EVERYONE",
                        allow_comment=True,
                    ),
                    "expected_version": 1,
                },
                {
                    "account_id": "acct-2",
                    "settings": account_settings(
                        privacy_level="PUBLIC_TO_EVERYONE",
                        allow_comment=True,
                    ),
                    "expected_version": 0,
                },
            ]
        )
        self.assertEqual([item["account_id"] for item in saved], ["acct-1", "acct-2"])
        self.assertEqual([item["version"] for item in saved], [2, 1])
        self.assertTrue(all(item["allow_comment"] for item in saved))

        before_first = self.store.get_account_settings("acct-1")
        before_second = self.store.get_account_settings("acct-2")
        with self.assertRaises(TTPostError) as conflict:
            self.store.save_account_settings_batch(
                [
                    {
                        "account_id": "acct-1",
                        "settings": account_settings(
                            privacy_level="SELF_ONLY",
                            allow_comment=False,
                        ),
                        "expected_version": 2,
                    },
                    {
                        "account_id": "acct-2",
                        "settings": account_settings(
                            privacy_level="SELF_ONLY",
                            allow_comment=False,
                        ),
                        "expected_version": 0,
                    },
                ]
            )
        self.assertEqual(
            "tt_account_settings_version_conflict",
            conflict.exception.code,
        )
        self.assertEqual(
            self.store.get_account_settings("acct-1"),
            before_first,
        )
        self.assertEqual(
            self.store.get_account_settings("acct-2"),
            before_second,
        )

    def test_account_settings_batch_rejects_empty_duplicate_and_oversized_targets(self):
        with self.assertRaises(TTPostError) as empty:
            self.store.save_account_settings_batch([])
        self.assertEqual("invalid_batch_targets", empty.exception.code)

        duplicate = {
            "account_id": "acct-1",
            "settings": account_settings(),
            "expected_version": 0,
        }
        with self.assertRaises(TTPostError) as repeated:
            self.store.save_account_settings_batch([duplicate, duplicate])
        self.assertEqual("invalid_batch_targets", repeated.exception.code)

        updates = [
            {
                "account_id": "acct-%d" % index,
                "settings": account_settings(),
                "expected_version": 0,
            }
            for index in range(1, 52)
        ]
        with self.assertRaises(TTPostError) as oversized:
            self.store.save_account_settings_batch(updates)
        self.assertEqual("invalid_batch_targets", oversized.exception.code)

    def test_material_id_is_globally_unique(self):
        self.store.add_material("1001")
        with self.assertRaises(TTPostError) as caught:
            self.store.add_material("1001")
        self.assertEqual("tt_post_material_already_exists", caught.exception.code)

    def test_same_account_same_utc_time_is_unique(self):
        self.add_and_freeze("1001")
        pool = self.store.add_material("1002")
        with self.assertRaises(TTPostError) as caught:
            self.store.freeze_queue(
                pool["id"],
                account(),
                "2026-07-29 10:00:00",
                CAPTION,
                policy(),
                resolver,
            )
        self.assertEqual("tt_post_account_time_conflict", caught.exception.code)

    def test_same_account_different_times_is_allowed(self):
        self.add_and_freeze("1001", scheduled="2026-07-29 10:00:00")
        second = self.add_and_freeze(
            "1002",
            scheduled="2026-07-29 10:05:00",
        )
        self.assertEqual("scheduled", second["status"])

    def test_material_cannot_be_frozen_twice(self):
        first = self.add_and_freeze("1001")
        replay = self.store.freeze_queue(
            first["pool_item_id"],
            account(),
            "2026-07-29 10:00:00",
            CAPTION,
            policy(),
            resolver,
        )
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(TTPostError):
            self.store.freeze_queue(
                first["pool_item_id"],
                account("acct-2"),
                "2026-07-29 10:05:00",
                CAPTION,
                policy(),
                resolver,
            )

    def test_queue_and_event_views_have_no_claim_token(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.assertNotIn("claim_token", self.store.get_queue(queue["id"]))
        self.assertNotIn(claim.reveal_claim_token(), repr(claim))
        serialized_events = repr(self.store.list_events(queue_id=queue["id"]))
        self.assertNotIn(claim.reveal_claim_token(), serialized_events)

    def test_queue_accepts_and_freezes_editable_caption_template(self):
        template = "Custom copy\n\nDrama ID: {{contect_id}}"
        queue = self.add_and_freeze(template=template)
        self.assertEqual(template, queue["caption_template"])
        self.assertEqual(
            "Custom copy\n\nDrama ID: Y9v1yQcFqM",
            queue["caption"],
        )

    def test_same_idempotency_key_with_changed_template_conflicts(self):
        pool = self.store.add_material("1001")
        first = self.store.freeze_queue(
            pool["id"],
            account(),
            "2026-07-29 10:00:00",
            CAPTION,
            policy(),
            resolver,
            idempotency_key="tt-post:editable-template",
        )
        self.assertEqual(CAPTION, first["caption_template"])
        with self.assertRaises(TTPostError) as caught:
            self.store.freeze_queue(
                pool["id"],
                account(),
                "2026-07-29 10:00:00",
                "Custom\n\nDrama ID: {{contect_id}}",
                policy(),
                resolver,
                idempotency_key="tt-post:editable-template",
            )
        self.assertEqual("tt_post_idempotency_conflict", caught.exception.code)


class RecurringStorageTests(CoreTestCase):
    def add_recurring(self, material_id, account_id="acct-1"):
        content_id = "CONTENT_%s" % material_id
        template = "Watch now\nDrama ID: {{contect_id}}"
        return self.store.add_recurring_material(
            material_id,
            account_id,
            content_id,
            "https://cdn.example.com/source-%s.mp4" % material_id,
            "https://gpu.example.com/prepared-%s.mp4" % material_id,
            gpu_job_id="gpu-job-recurring-%s" % material_id,
            prepared_output_sha256=("a" if account_id == "acct-1" else "b") * 64,
            prepared_output_size=123456,
            prepared_duration_sec=120.25,
            source_trim_tail_seconds=4.333333,
            preparation_profile="tt-post-outro-v1",
            caption_template=template,
            caption=render_caption_template(template, content_id),
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 10:00:00",
            is_aigc=False,
            actor_user_id="operator-1",
            actor_name="Operator",
        )

    def claim_manual(
        self,
        suffix,
        account_id="acct-1",
        publish_time="10:00",
    ):
        return self.store.claim_recurring_run(
            "tt-post:manual:%s" % suffix,
            "manual",
            account_id,
            "2026-07-29",
            publish_time,
            beijing_to_utc("2026-07-29 %s:00" % publish_time),
            config_version=0,
            manual_request_key="manual-request-%s" % suffix,
        )

    def acquire_execution(self, run):
        execution = self.store.acquire_recurring_execution(run["id"])
        token = execution.reveal_execution_token()
        self.recurring_execution_tokens[int(run["id"])] = token
        return token

    def freeze_legacy_queue_for_run(self, run, execution_token=None):
        pool_item = run["pool_item"]
        legacy_pool = self.store.add_material(pool_item["material_id"])
        if execution_token is None:
            execution_token = self.acquire_execution(run)
        else:
            self.recurring_execution_tokens[int(run["id"])] = execution_token

        def recurring_resolver(material_id):
            return {
                "material_id": material_id,
                "content_id": pool_item["content_id"],
                "media_url": pool_item["prepared_media_url"],
            }

        return self.store.freeze_queue(
            legacy_pool["id"],
            account(run["account_id"]),
            "%s %s:00" % (run["shanghai_date"], run["publish_time"]),
            pool_item["caption_template"],
            policy(),
            recurring_resolver,
            idempotency_key=run["run_key"],
            gpu_job_id=pool_item["gpu_job_id"],
            source_media_url=pool_item["source_media_url"],
            prepared_output_sha256=pool_item["prepared_output_sha256"],
            prepared_output_size=pool_item["prepared_output_size"],
            prepared_duration_sec=pool_item["prepared_duration_sec"],
            source_trim_tail_seconds=pool_item["source_trim_tail_seconds"],
            recurring_run_id=run["id"],
            recurring_execution_token=execution_token,
        )

    def test_schedule_defaults_disabled_and_saves_with_optimistic_version(self):
        default = self.store.get_daily_schedule("acct-1")
        self.assertFalse(default["enabled"])
        self.assertEqual([], default["publish_times"])
        self.assertEqual(0, default["version"])
        self.assertEqual("Asia/Shanghai", default["timezone"])

        saved = self.store.save_daily_schedule(
            "acct-1",
            ["18:30", "09:05"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 10:00:00",
            actor_user_id="operator-1",
            actor_name="Operator",
        )
        self.assertTrue(saved["enabled"])
        self.assertEqual(["09:05", "18:30"], saved["publish_times"])
        self.assertEqual(1, saved["version"])
        self.assertTrue(saved["user_consent"])
        self.assertEqual([saved], self.store.list_daily_schedules())

        with self.assertRaises(TTPostError) as conflict:
            self.store.save_daily_schedule(
                "acct-1",
                ["09:05"],
                enabled=True,
                expected_version=0,
                consent_version="tt-post-recurring-v1",
                consented_at="2026-07-29 10:01:00",
            )
        self.assertEqual(
            "tt_post_schedule_version_conflict",
            conflict.exception.code,
        )
        with self.assertRaises(TTPostError):
            self.store.save_daily_schedule(
                "acct-2",
                ["9:05"],
                enabled=True,
                expected_version=0,
                consent_version="tt-post-recurring-v1",
                consented_at="2026-07-29 10:01:00",
            )

    def test_pool_fifo_is_isolated_per_account(self):
        first = self.add_recurring("1101", "acct-1")
        second = self.add_recurring("1102", "acct-1")
        other = self.add_recurring("2201", "acct-2")
        self.assertEqual(
            2,
            self.store.count_recurring_materials(
                account_id="acct-1",
                status="available",
            ),
        )

        first_run = self.claim_manual("fifo-a", "acct-1")
        other_run = self.claim_manual("fifo-b", "acct-2")
        self.assertEqual(first["id"], first_run["pool_item_id"])
        self.assertEqual(other["id"], other_run["pool_item_id"])
        self.assertEqual("reserved", first_run["pool_item"]["status"])
        self.assertEqual("reserved", other_run["pool_item"]["status"])
        self.assertEqual(
            [second["material_id"]],
            [
                item["material_id"]
                for item in self.store.list_recurring_materials(
                    account_id="acct-1",
                    status="available",
                )
            ],
        )

    def test_double_claim_is_idempotent_and_account_active_run_is_exclusive(self):
        self.add_recurring("3101")
        self.add_recurring("3102")
        first = self.claim_manual("same")
        replay = self.claim_manual("same")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["pool_item_id"], replay["pool_item_id"])
        with self.assertRaises(TTPostError) as busy:
            self.claim_manual("other", publish_time="10:01")
        self.assertEqual("tt_post_account_publish_busy", busy.exception.code)
        self.assertEqual(
            1,
            self.store.count_recurring_materials(
                account_id="acct-1",
                status="reserved",
            ),
        )

    def test_execution_lease_is_per_run_exclusive_and_crash_recoverable(self):
        self.add_recurring("3103", "acct-1")
        self.add_recurring("3104", "acct-2")
        first = self.claim_manual("lease-a", "acct-1")
        second = self.claim_manual("lease-b", "acct-2")

        first_execution = self.store.acquire_recurring_execution(first["id"])
        second_execution = self.store.acquire_recurring_execution(second["id"])
        first_token = first_execution.reveal_execution_token()
        second_token = second_execution.reveal_execution_token()
        self.assertNotEqual(first_token, second_token)
        self.assertNotIn("execution_token", first_execution.run)
        self.assertNotIn("execution_lease_expires_at_utc", first_execution.run)
        self.assertNotIn(first_token, repr(first_execution))

        with self.assertRaises(TTPostError) as busy:
            self.store.acquire_recurring_execution(first["id"])
        self.assertEqual(
            "tt_post_recurring_execution_busy",
            busy.exception.code,
        )
        self.assertEqual(
            [],
            self.store.list_claimed_unbound_recurring_runs(),
        )

        self.clock.current += timedelta(seconds=120)
        pending = self.store.list_claimed_unbound_recurring_runs()
        self.assertEqual(
            [first["id"], second["id"]],
            [item["id"] for item in pending],
        )
        replacement = self.store.acquire_recurring_execution(first["id"])
        replacement_token = replacement.reveal_execution_token()
        self.assertNotEqual(first_token, replacement_token)
        with self.assertRaises(TTPostError) as stale:
            self.store.renew_recurring_execution(
                first["id"],
                first_token,
            )
        self.assertEqual(
            "tt_post_recurring_execution_invalid",
            stale.exception.code,
        )

    def test_release_first_fences_stale_queue_freeze(self):
        self.add_recurring("3105")
        run = self.claim_manual("release-first")
        execution_token = self.acquire_execution(run)
        released = self.store.release_recurring_preflight(
            run["id"],
            error_code="synthetic_preflight_failure",
            error_message="synthetic",
            execution_token=execution_token,
        )
        self.assertEqual("preflight_failed", released["status"])
        with self.assertRaises(TTPostError) as stale:
            self.freeze_legacy_queue_for_run(
                run,
                execution_token=execution_token,
            )
        self.assertEqual(
            "tt_post_recurring_execution_invalid",
            stale.exception.code,
        )
        self.assertEqual([], self.store.list_queues())
        self.assertEqual(
            "available",
            self.store.list_recurring_materials(
                account_id="acct-1"
            )[0]["status"],
        )

    def test_queue_freeze_first_blocks_release_until_owner_binds(self):
        self.add_recurring("3106")
        run = self.claim_manual("freeze-first")
        execution_token = self.acquire_execution(run)
        queue = self.freeze_legacy_queue_for_run(
            run,
            execution_token=execution_token,
        )
        with self.assertRaises(TTPostError) as release:
            self.store.release_recurring_preflight(
                run["id"],
                error_code="must-not-release",
                error_message="must-not-release",
                execution_token=execution_token,
            )
        self.assertEqual(
            "tt_post_preflight_release_invalid",
            release.exception.code,
        )
        still_claimed = self.store.get_recurring_run(run["id"])
        self.assertEqual("claimed", still_claimed["status"])
        self.assertEqual("reserved", still_claimed["pool_item"]["status"])
        bound = self.store.bind_recurring_queue(
            run["id"],
            queue["id"],
            execution_token=execution_token,
        )
        self.assertEqual("scheduled", bound["status"])
        self.assertEqual(queue["id"], bound["queue_id"])

    def test_claimed_unbound_runs_can_be_found_by_key_and_recovered_in_order(self):
        self.add_recurring("3111", "acct-1")
        self.add_recurring("3112", "acct-2")
        later = self.claim_manual(
            "recover-later",
            "acct-1",
            publish_time="10:01",
        )
        earlier = self.claim_manual(
            "recover-earlier",
            "acct-2",
            publish_time="10:00",
        )
        fetched = self.store.get_recurring_run_by_key(earlier["run_key"])
        self.assertEqual(earlier["id"], fetched["id"])
        pending = self.store.list_claimed_unbound_recurring_runs()
        self.assertEqual([earlier["id"], later["id"]], [
            item["id"] for item in pending
        ])

        queue = self.freeze_legacy_queue_for_run(earlier)
        self.store.bind_recurring_queue(
            earlier["id"],
            queue["id"],
            execution_token=self.recurring_execution_tokens[earlier["id"]],
        )
        self.assertEqual(
            [later["id"]],
            [
                item["id"]
                for item in self.store.list_claimed_unbound_recurring_runs()
            ],
        )

    def test_unknown_queue_or_run_blocks_all_later_account_claims(self):
        self.add_recurring("3201")
        run = self.claim_manual("unknown-queue")
        queue = self.freeze_legacy_queue_for_run(run)
        self.store.bind_recurring_queue(
            run["id"],
            queue["id"],
            execution_token=self.recurring_execution_tokens[run["id"]],
        )
        claims = self.store.claim_due(
            "worker-unknown",
            now=datetime(2026, 7, 29, 2, 0, 10, tzinfo=UTC),
        )
        self.store.begin_publish(
            queue["id"],
            claims[0].reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.store.mark_unknown(
            queue["id"],
            claims[0].reveal_claim_token(),
            reason="remote outcome cannot be proven",
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        synced = self.store.sync_recurring_from_queue(queue["id"])
        self.assertEqual("unknown", synced["status"])
        self.add_recurring("3202")
        with self.assertRaises(TTPostError) as queue_busy:
            self.claim_manual("after-unknown-queue", publish_time="10:01")
        self.assertEqual(
            "tt_post_account_publish_busy",
            queue_busy.exception.code,
        )

        self.add_recurring("3301", "acct-2")
        self.add_recurring("3302", "acct-2")
        unknown_run = self.claim_manual("unknown-run", "acct-2")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE tt_post_schedule_run
                SET status='unknown',finished_at_utc=updated_at
                WHERE id=?
                """,
                (unknown_run["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(TTPostError) as run_busy:
            self.claim_manual(
                "after-unknown-run",
                "acct-2",
                publish_time="10:01",
            )
        self.assertEqual(
            "tt_post_account_publish_busy",
            run_busy.exception.code,
        )

    def test_release_preflight_returns_material_to_available_fifo(self):
        material = self.add_recurring("4101")
        run = self.claim_manual("preflight")
        execution_token = self.acquire_execution(run)
        released = self.store.release_recurring_preflight(
            run["id"],
            error_code="creator_info_unavailable",
            error_message="Authorization: Bearer secret-value",
            execution_token=execution_token,
            actor_user_id="runner",
        )
        self.assertEqual("preflight_failed", released["status"])
        self.assertEqual("available", released["pool_item"]["status"])
        self.assertIsNone(released["pool_item"]["run_id"])
        self.assertNotIn("secret-value", released["error_message"])
        replay = self.store.release_recurring_preflight(
            run["id"],
            error_code="ignored-on-idempotent-replay",
            error_message="ignored",
        )
        self.assertEqual(released["id"], replay["id"])

        next_run = self.claim_manual("preflight-next", publish_time="10:01")
        self.assertEqual(material["id"], next_run["pool_item_id"])

    def test_bind_and_sync_follow_legacy_queue_without_changing_its_machine(self):
        self.add_recurring("5101")
        run = self.claim_manual("bind")
        queue = self.freeze_legacy_queue_for_run(run)
        bound = self.store.bind_recurring_queue(
            run["id"],
            queue["id"],
            execution_token=self.recurring_execution_tokens[run["id"]],
        )
        self.assertEqual("scheduled", bound["status"])
        self.assertEqual(queue["id"], bound["queue_id"])
        self.assertEqual("reserved", bound["pool_item"]["status"])

        claims = self.store.claim_due(
            "worker-1",
            now=datetime(2026, 7, 29, 2, 0, 10, tzinfo=UTC),
        )
        self.assertEqual(1, len(claims))
        synced = self.store.sync_recurring_from_queue(queue["id"])
        self.assertEqual("claimed", synced["status"])
        self.assertEqual("claimed", self.store.get_queue(queue["id"])["status"])

        failed_queue = self.store.mark_failed(
            queue["id"],
            claims[0].reveal_claim_token(),
            error_code="known_precommit_failure",
            error_message="rejected before publish",
            publish_was_not_created=True,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.assertEqual("failed", failed_queue["status"])
        terminal = self.store.sync_recurring_from_queue(queue["id"])
        self.assertEqual("failed", terminal["status"])
        self.assertEqual("consumed", terminal["pool_item"]["status"])
        self.assertEqual(
            "failed",
            self.store.get_queue(queue["id"])["status"],
        )


class AccountSourceTests(unittest.TestCase):
    def metadata(self, account_id="acct-1"):
        return {
            "account_id": account_id,
            "username": "dramawave",
            "display_name": "DramaWave",
            "avatar_url": "https://example.com/avatar.jpg",
            "status": "active",
            "publish_eligible": True,
        }

    def test_list_never_calls_token_loader_or_returns_token(self):
        calls = {"token": 0}

        def token_loader(_account_id):
            calls["token"] += 1
            raise AssertionError("token loader must not run")

        source = SnapshotAccountSource(
            lambda: [self.metadata()],
            lambda value: self.metadata(value),
            token_loader,
        )
        items = source.list_safe_accounts()
        self.assertEqual(0, calls["token"])
        self.assertNotIn("token", repr(items).lower())
        self.assertNotIn("access_token", items[0].as_dict())

    def test_exact_token_is_only_available_in_context_and_repr_is_redacted(self):
        secret = "tt-secret-access-token"
        requested = []
        source = SnapshotAccountSource(
            lambda: [self.metadata()],
            lambda value: self.metadata(value),
            lambda value: requested.append(value)
            or {"account_id": value, "access_token": secret},
        )
        with source.publish_credentials("acct-1") as credentials:
            self.assertIsInstance(credentials, PublishCredentials)
            self.assertEqual(secret, credentials.reveal_access_token())
            self.assertNotIn(secret, repr(credentials))
            held = credentials
        self.assertEqual(["acct-1"], requested)
        with self.assertRaises(AccountSourceError):
            held.reveal_access_token()

    def test_token_account_mismatch_fails_without_secret_in_error(self):
        secret = "mismatched-super-secret"
        source = SnapshotAccountSource(
            lambda: [self.metadata()],
            lambda value: self.metadata(value),
            lambda _value: {
                "account_id": "acct-other",
                "access_token": secret,
            },
        )
        with self.assertRaises(AccountSourceError) as caught:
            with source.publish_credentials("acct-1"):
                pass
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))

    def test_loader_exception_is_wrapped_without_leaking_token(self):
        secret = "leaked-by-loader"

        def broken(_value):
            raise RuntimeError(secret)

        source = SnapshotAccountSource(
            lambda: [self.metadata()],
            lambda value: self.metadata(value),
            broken,
        )
        with self.assertRaises(AccountSourceError) as caught:
            with source.publish_credentials("acct-1"):
                pass
        self.assertNotIn(secret, str(caught.exception))

    def test_ineligible_account_does_not_read_token(self):
        calls = []
        metadata = self.metadata()
        metadata["publish_eligible"] = False
        source = SnapshotAccountSource(
            lambda: [metadata],
            lambda _value: metadata,
            lambda value: calls.append(value),
        )
        with self.assertRaises(AccountSourceError):
            with source.publish_credentials("acct-1"):
                pass
        self.assertEqual([], calls)


class CaptionPolicyAndTimeTests(unittest.TestCase):
    def test_beijing_time_converts_to_utc(self):
        self.assertEqual(
            "2026-07-29T02:30:00Z",
            beijing_to_utc("2026-07-29 10:30:00"),
        )

    def test_caption_renders_product_owner_placeholder(self):
        rendered = render_fixed_caption("Y9v1yQcFqM")
        self.assertIn("Drama ID: Y9v1yQcFqM", rendered)
        self.assertNotIn("{{contect_id}}", rendered)

    def test_caption_accepts_correctly_spelled_alias(self):
        self.assertEqual(
            "Drama ID: ABC_123",
            render_caption_template("Drama ID: {{content_id}}", "ABC_123"),
        )

    def test_caption_requires_content_id_placeholder(self):
        with self.assertRaises(TTPostError) as caught:
            render_caption_template("Watch now", "ABC")
        self.assertEqual("caption_content_id_required", caught.exception.code)

    def test_caption_rejects_unknown_placeholder(self):
        with self.assertRaises(TTPostError):
            render_caption_template(
                "{{contect_id}} {{access_token}}",
                "ABC",
            )
        for malformed in (
            "{{contect_id}} {{bad-name}}",
            "{{contect_id}} {{unfinished",
            "{{contect_id}} stray }}",
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(TTPostError) as caught:
                    render_caption_template(malformed, "ABC")
                self.assertEqual(
                    "caption_placeholder_invalid",
                    caught.exception.code,
                )

    def test_caption_length_uses_utf16_units(self):
        allowed = ("a" * 2197) + "😀" + "{{contect_id}}"
        self.assertEqual(
            ("a" * 2197) + "😀A",
            render_caption_template(allowed, "A"),
        )
        with self.assertRaises(TTPostError) as caught:
            render_caption_template(("a" * 2198) + "😀{{contect_id}}", "A")
        self.assertEqual("caption_length_invalid", caught.exception.code)

    def test_policy_requires_every_explicit_field(self):
        data = policy()
        data.pop("allow_duet")
        with self.assertRaises(TTPostError):
            TTPostPolicy.from_mapping(data)

    def test_policy_requires_true_consent(self):
        data = policy()
        data["user_consent"] = False
        with self.assertRaises(TTPostError) as caught:
            TTPostPolicy.from_mapping(data)
        self.assertEqual("tt_post_consent_required", caught.exception.code)

    def test_policy_persists_explicit_interaction_and_disclosure(self):
        item = TTPostPolicy.from_mapping(policy())
        self.assertTrue(item.allow_comment)
        self.assertFalse(item.allow_duet)
        self.assertFalse(item.allow_stitch)
        self.assertFalse(item.brand_content_toggle)
        self.assertTrue(item.brand_organic_toggle)
        self.assertEqual("2026-07-29T02:00:00Z", item.consented_at_utc)


class LifecycleTests(CoreTestCase):
    def test_resolver_and_caption_are_frozen(self):
        calls = []

        def injected(material_id):
            calls.append(material_id)
            return {
                "material_id": material_id,
                "content_id": "CONTENT-9",
                "media_url": "https://cdn.example.com/a.mp4",
            }

        pool = self.store.add_material("1001")
        queue = self.store.freeze_queue(
            pool["id"],
            account(),
            "2026-07-29 10:00:00",
            CAPTION,
            policy(),
            injected,
        )
        self.assertEqual(["1001"], calls)
        self.assertEqual("CONTENT-9", queue["content_id"])
        self.assertIn("Drama ID: CONTENT-9", queue["caption"])
        self.assertEqual("2026-07-29T02:00:00Z", queue["scheduled_at_utc"])

    def test_live_gates_default_closed_and_require_all_three(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        for gates in (
            LiveGates(),
            LiveGates(True, False, True),
            LiveGates(True, True, False),
        ):
            with self.assertRaises(TTPostError) as caught:
                self.store.begin_publish(
                    queue["id"],
                    claim.reveal_claim_token(),
                    gates,
                    now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
                )
            self.assertEqual("tt_post_live_gate_closed", caught.exception.code)
        started = self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.assertEqual("publishing", started["status"])

    def test_claim_lease_can_be_reclaimed_before_publish(self):
        queue = self.add_and_freeze()
        first = self.claim_one(
            queue,
            now=datetime(2026, 7, 29, 2, 0, 1, tzinfo=UTC),
        )
        second = self.store.claim_due(
            "worker-2",
            now=datetime(2026, 7, 29, 2, 6, tzinfo=UTC),
            grace_seconds=600,
        )
        self.assertEqual(1, len(second))
        self.assertNotEqual(
            first.reveal_claim_token(),
            second[0].reveal_claim_token(),
        )
        self.assertEqual("worker-2", second[0].queue["claim_worker"])

    def test_overdue_schedule_is_marked_missed_and_never_claimed(self):
        queue = self.add_and_freeze()
        claims = self.store.claim_due(
            "worker-1",
            now=datetime(2026, 7, 29, 2, 5, tzinfo=UTC),
            grace_seconds=90,
        )
        self.assertEqual([], claims)
        self.assertEqual("missed", self.store.get_queue(queue["id"])["status"])

    def test_cancel_is_idempotent_before_publish(self):
        queue = self.add_and_freeze()
        canceled = self.store.cancel_queue(queue["id"], reason="operator canceled")
        replay = self.store.cancel_queue(queue["id"], reason="operator canceled")
        self.assertEqual("canceled", canceled["status"])
        self.assertEqual("canceled", replay["status"])

    def test_cancel_is_rejected_after_publish_started(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        with self.assertRaises(TTPostError):
            self.store.cancel_queue(queue["id"], reason="too late")

    def test_unknown_outcome_is_terminal_and_never_reclaimed(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        unknown = self.store.mark_unknown(
            queue["id"],
            claim.reveal_claim_token(),
            reason="network response was lost",
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        self.assertEqual("unknown", unknown["status"])
        self.assertTrue(unknown["unknown_outcome"])
        self.assertEqual(
            [],
            self.store.claim_due(
                "worker-2",
                now=datetime(2026, 7, 29, 2, 10, tzinfo=UTC),
                grace_seconds=3600,
            ),
        )

    def test_expired_publishing_lease_becomes_unknown(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(
            queue,
            now=datetime(2026, 7, 29, 2, 0, 1, tzinfo=UTC),
        )
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.store.claim_due(
            "worker-2",
            now=datetime(2026, 7, 29, 2, 6, tzinfo=UTC),
            grace_seconds=3600,
        )
        self.assertEqual("unknown", self.store.get_queue(queue["id"])["status"])

    def test_publish_id_switches_queue_to_reconcile_only(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        pending = self.store.record_publish_id(
            queue["id"],
            claim.reveal_claim_token(),
            "publish-123",
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        self.assertEqual("reconciling", pending["status"])
        self.assertEqual(
            [],
            self.store.claim_due(
                "worker-2",
                now=datetime(2026, 7, 29, 2, 10, tzinfo=UTC),
                grace_seconds=3600,
            ),
        )
        with self.assertRaises(TTPostError):
            self.store.mark_unknown(
                queue["id"],
                claim.reveal_claim_token(),
                reason="must reconcile",
            )
        published = self.store.reconcile_published(
            queue["id"],
            "publish-123",
            publish_url="https://www.tiktok.com/@dramawave/video/123",
        )
        replay = self.store.reconcile_published(
            queue["id"],
            "publish-123",
            publish_url="https://www.tiktok.com/@dramawave/video/123",
        )
        self.assertEqual("published", published["status"])
        self.assertEqual("published", replay["status"])

    def test_explicit_remote_failure_is_terminal_and_retains_publish_id(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.store.record_publish_id(
            queue["id"],
            claim.reveal_claim_token(),
            "publish-failed-123",
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        with self.assertRaises(TTPostError):
            self.store.reconcile_failed(
                queue["id"],
                "different-publish-id",
                remote_status="failed",
            )
        failed = self.store.reconcile_failed(
            queue["id"],
            "publish-failed-123",
            remote_status="publish_failed",
        )
        replay = self.store.reconcile_failed(
            queue["id"],
            "publish-failed-123",
            remote_status="publish_failed",
        )
        self.assertEqual("failed", failed["status"])
        self.assertEqual("failed", replay["status"])
        self.assertEqual("publish-failed-123", failed["publish_id"])
        self.assertEqual("tt_post_remote_publish_failed", failed["error_code"])
        self.assertFalse(failed["unknown_outcome"])
        self.assertEqual(
            [],
            self.store.claim_due(
                "worker-2",
                now=datetime(2026, 7, 29, 2, 5, tzinfo=UTC),
                grace_seconds=600,
            ),
        )
        events = self.store.list_events(queue_id=queue["id"])
        self.assertIn(
            "publish_reconciled_failed",
            [item["event_type"] for item in events],
        )

    def test_uncertain_failure_is_forced_to_unknown(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        result = self.store.mark_failed(
            queue["id"],
            claim.reveal_claim_token(),
            error_code="network_timeout",
            error_message="request timed out",
            publish_was_not_created=False,
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        self.assertEqual("unknown", result["status"])
        self.assertTrue(result["unknown_outcome"])

    def test_known_precommit_failure_is_terminal_failed(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        result = self.store.mark_failed(
            queue["id"],
            claim.reveal_claim_token(),
            error_code="media_invalid",
            error_message="media rejected before upload",
            publish_was_not_created=True,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["unknown_outcome"])

    def test_error_storage_redacts_bearer_values(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        result = self.store.mark_failed(
            queue["id"],
            claim.reveal_claim_token(),
            error_code="preflight_failed",
            error_message="Authorization: Bearer super-secret-token",
            publish_was_not_created=True,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.assertNotIn("super-secret-token", result["error_message"])
        self.assertNotIn(
            "super-secret-token",
            repr(self.store.list_events(queue_id=queue["id"])),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

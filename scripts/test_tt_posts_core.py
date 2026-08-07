#!/usr/bin/env python3
"""Standalone tests for the TikTok Post pool core."""

import os
import sqlite3
import sys
import tempfile
import threading
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
    normalize_drama_language,
    render_fixed_caption,
    render_caption_template,
)
from features.tt_posts.links import build_w2a_url  # noqa: E402


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
        "drama_language": "en",
    }
    values.update(overrides)
    return TTPostAccountSettings.from_mapping(values)


def rebuild_table_without_column(conn, table_name, column_name):
    """Simulate a legacy table on SQLite versions without DROP COLUMN."""

    def quoted(identifier):
        return '"%s"' % str(identifier).replace('"', '""')

    table = quoted(table_name)
    columns = conn.execute(
        "PRAGMA table_info(%s)" % table
    ).fetchall()
    kept = [row for row in columns if str(row[1]) != column_name]
    if len(kept) == len(columns):
        raise AssertionError("column %s is missing" % column_name)
    primary_keys = sorted(
        (int(row[5]), str(row[1])) for row in kept if int(row[5]) > 0
    )
    single_primary_key = (
        primary_keys[0][1] if len(primary_keys) == 1 else ""
    )
    original_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    original_sql = str(original_sql_row[0] or "")
    definitions = []
    for row in kept:
        name = str(row[1])
        data_type = str(row[2] or "")
        definition = "%s %s" % (quoted(name), data_type)
        if int(row[3]):
            definition += " NOT NULL"
        if row[4] is not None:
            definition += " DEFAULT %s" % str(row[4])
        if name == single_primary_key:
            definition += " PRIMARY KEY"
            if (
                data_type.upper() == "INTEGER"
                and "AUTOINCREMENT" in original_sql.upper()
            ):
                definition += " AUTOINCREMENT"
        definitions.append(definition)
    if len(primary_keys) > 1:
        definitions.append(
            "PRIMARY KEY(%s)"
            % ",".join(quoted(name) for _, name in primary_keys)
        )
    index_sql = [
        str(row[0])
        for row in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table_name,),
        ).fetchall()
        if column_name.casefold() not in str(row[0]).casefold()
    ]
    replacement_name = "%s_legacy_rebuild" % table_name
    replacement = quoted(replacement_name)
    conn.execute("DROP TABLE IF EXISTS %s" % replacement)
    conn.execute(
        "CREATE TABLE %s (%s)" % (replacement, ",".join(definitions))
    )
    column_sql = ",".join(quoted(str(row[1])) for row in kept)
    conn.execute(
        "INSERT INTO %s (%s) SELECT %s FROM %s"
        % (replacement, column_sql, column_sql, table)
    )
    conn.execute("DROP TABLE %s" % table)
    conn.execute(
        "ALTER TABLE %s RENAME TO %s" % (replacement, table)
    )
    for statement in index_sql:
        conn.execute(statement)


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
    def test_storage_has_expected_tables(self):
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
                "tt_post_code_route",
                "tt_post_code_recycle_audit",
                "tt_post_event",
                "tt_post_account_setting",
                "tt_post_daily_schedule",
                "tt_post_auto_publish_config",
                "tt_post_random_daily_plan",
                "tt_post_recurring_pool",
                "tt_post_material_intake",
                "tt_post_direct_test",
                "tt_post_schedule_run",
            },
            names,
        )

    def test_additive_migration_is_idempotent_and_preserves_legacy_data(self):
        legacy = self.store.add_material("9001")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TABLE tt_post_material_intake")
            conn.execute("DROP TABLE tt_post_schedule_run")
            conn.execute("DROP TABLE tt_post_recurring_pool")
            conn.execute("DROP TABLE tt_post_daily_schedule")
            conn.commit()
        finally:
            conn.close()

        migrated = TTPostStore(self.db_path, now_fn=self.clock)
        conn = sqlite3.connect(self.db_path)
        try:
            legacy_columns = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(tt_post_schedule_run)"
                )
                if row[1]
                not in {
                    "execution_token",
                    "execution_lease_expires_at_utc",
                }
            ]
            selected_columns = ",".join(
                '"%s"' % name.replace('"', '""')
                for name in legacy_columns
            )
            conn.execute(
                "CREATE TABLE tt_post_schedule_run_legacy AS "
                "SELECT %s FROM tt_post_schedule_run" % selected_columns
            )
            conn.execute("DROP TABLE tt_post_schedule_run")
            conn.execute(
                "ALTER TABLE tt_post_schedule_run_legacy "
                "RENAME TO tt_post_schedule_run"
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

    def test_schedule_mode_columns_migrate_from_the_previous_schema(self):
        original = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=["11:00"],
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-fixed-v1",
            consented_at="2026-07-29 10:00:00",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                ALTER TABLE tt_post_daily_schedule
                    RENAME TO tt_post_daily_schedule_current;
                CREATE TABLE tt_post_daily_schedule (
                    account_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(enabled IN (0,1)),
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
                    user_consent INTEGER NOT NULL CHECK(user_consent=1),
                    consent_version TEXT NOT NULL,
                    consented_at_utc TEXT NOT NULL,
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO tt_post_daily_schedule
                SELECT account_id,enabled,timezone,publish_times_json,
                    version,user_consent,consent_version,consented_at_utc,
                    created_by_user_id,created_by_name,updated_by_user_id,
                    updated_by_name,created_at,updated_at
                FROM tt_post_daily_schedule_current;
                DROP TABLE tt_post_daily_schedule_current;

                ALTER TABLE tt_post_auto_publish_config
                    RENAME TO tt_post_auto_publish_config_current;
                CREATE TABLE tt_post_auto_publish_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    version INTEGER NOT NULL CHECK(version>0),
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(enabled IN (0,1)),
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
                        CHECK(timezone='Asia/Shanghai'),
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    account_ids_json TEXT NOT NULL DEFAULT '[]',
                    caption_template TEXT NOT NULL,
                    user_consent INTEGER NOT NULL DEFAULT 0
                        CHECK(user_consent IN (0,1)),
                    consent_version TEXT NOT NULL DEFAULT '',
                    consented_at_utc TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO tt_post_auto_publish_config
                SELECT id,version,enabled,timezone,publish_times_json,
                    account_ids_json,caption_template,user_consent,
                    consent_version,consented_at_utc,created_by_user_id,
                    created_by_name,updated_by_user_id,updated_by_name,
                    created_at,updated_at
                FROM tt_post_auto_publish_config_current;
                DROP TABLE tt_post_auto_publish_config_current;
                """
            )
            conn.commit()
        finally:
            conn.close()

        migrated = TTPostStore(self.db_path, now_fn=self.clock)
        config = migrated.get_auto_publish_config()
        schedule = migrated.get_daily_schedule("acct-1")
        self.assertEqual(original["publish_times"], config["publish_times"])
        self.assertEqual("fixed", config["schedule_mode"])
        self.assertEqual(0, config["random_daily_count"])
        self.assertEqual("", config["random_effective_date"])
        self.assertEqual("fixed", schedule["schedule_mode"])
        self.assertEqual(0, schedule["random_daily_count"])
        self.assertEqual("", schedule["random_effective_date"])
        updated = migrated.save_auto_publish_config(
            expected_version=config["version"],
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=["07:15", "19:45"],
            schedule_mode="fixed",
            random_daily_count=0,
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-fixed-v2",
            consented_at="2026-07-29 10:05:00",
        )
        self.assertEqual(["07:15", "19:45"], updated["publish_times"])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()

    def test_random_daily_plans_are_persistent_non_hourly_and_spaced(self):
        saved = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=[],
            schedule_mode="random",
            random_daily_count=24,
            account_ids=["acct-1", "acct-2"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-random-v1",
            consented_at="2026-07-29 10:00:00",
        )
        self.assertEqual("random", saved["schedule_mode"])
        self.assertEqual(24, saved["random_daily_count"])
        self.assertEqual("2026-07-30", saved["random_effective_date"])
        self.assertEqual(2, len(saved["random_daily_plans"]))
        frozen = {
            (item["account_id"], item["shanghai_date"]): item
            for item in saved["random_daily_plans"]
        }
        for item in frozen.values():
            self.assertEqual(24, len(item["publish_times"]))
            minute_values = []
            for value in item["publish_times"]:
                hour, minute = (int(part) for part in value.split(":"))
                self.assertNotEqual(0, minute)
                minute_values.append(hour * 60 + minute)
            self.assertTrue(
                all(
                    right - left >= 60
                    for left, right in zip(
                        minute_values,
                        minute_values[1:],
                    )
                )
            )

        replay = TTPostStore(self.db_path, now_fn=self.clock)
        self.assertEqual(
            saved["random_daily_plans"],
            replay.get_auto_publish_config()["random_daily_plans"],
        )
        self.clock.current += timedelta(days=1)
        replay.ensure_random_daily_plans(["2026-07-31"])
        all_plans = replay.list_random_daily_plans()
        for account_id in ("acct-1", "acct-2"):
            first = next(
                item for item in all_plans
                if item["account_id"] == account_id
                and item["shanghai_date"] == "2026-07-30"
            )
            second = next(
                item for item in all_plans
                if item["account_id"] == account_id
                and item["shanghai_date"] == "2026-07-31"
            )
            self.assertNotEqual(first["publish_times"], second["publish_times"])

    def test_random_config_change_keeps_today_and_replaces_tomorrow(self):
        first = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=[],
            schedule_mode="random",
            random_daily_count=2,
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-random-v1",
            consented_at="2026-07-29 10:00:00",
        )
        self.clock.current += timedelta(days=1)
        self.store.ensure_random_daily_plans(["2026-07-31"])
        before = {
            item["shanghai_date"]: item["publish_times"]
            for item in self.store.list_random_daily_plans(
                account_ids=["acct-1"]
            )
        }
        changed = self.store.save_auto_publish_config(
            expected_version=first["version"],
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=[],
            schedule_mode="random",
            random_daily_count=3,
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-random-v1",
            consented_at="2026-07-30 10:00:00",
        )
        after = {
            item["shanghai_date"]: item["publish_times"]
            for item in self.store.list_random_daily_plans(
                account_ids=["acct-1"]
            )
        }
        self.assertEqual("2026-07-31", changed["random_effective_date"])
        self.assertEqual(before["2026-07-30"], after["2026-07-30"])
        self.assertEqual(3, len(after["2026-07-31"]))

    def test_concurrent_random_plan_ensure_keeps_one_persisted_result(self):
        self.store.save_auto_publish_config(
            expected_version=0,
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=[],
            schedule_mode="random",
            random_daily_count=4,
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-random-v1",
            consented_at="2026-07-29 10:00:00",
        )
        stores = [
            TTPostStore(self.db_path, now_fn=self.clock),
            TTPostStore(self.db_path, now_fn=self.clock),
        ]
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def ensure(store):
            try:
                barrier.wait()
                item = store.ensure_random_daily_plans(["2026-07-31"])[0]
                with lock:
                    results.append(item)
            except Exception as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        workers = [threading.Thread(target=ensure, args=(store,)) for store in stores]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        persisted = self.store.list_random_daily_plans(
            account_ids=["acct-1"],
            shanghai_dates=["2026-07-31"],
        )
        self.assertEqual([results[0]], persisted)

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

    def test_drama_language_defaults_normalizes_and_migrates_additively(self):
        self.assertEqual("en", normalize_drama_language(None))
        self.assertEqual("pt-br", normalize_drama_language(" PT_BR "))
        self.assertEqual("english", normalize_drama_language("English"))
        with self.assertRaises(TTPostError) as invalid:
            normalize_drama_language("en us")
        self.assertEqual("invalid_drama_language", invalid.exception.code)
        with self.assertRaises(TTPostError) as expanded:
            normalize_drama_language("ß" * 32)
        self.assertEqual("invalid_drama_language", expanded.exception.code)

        created = self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="ES"),
            expected_version=0,
        )
        self.assertEqual("es", created["drama_language"])

        conn = sqlite3.connect(self.db_path)
        try:
            rebuild_table_without_column(
                conn,
                "tt_post_account_setting",
                "drama_language",
            )
            conn.commit()
        finally:
            conn.close()
        migrated = TTPostStore(self.db_path, now_fn=self.clock)
        self.assertEqual(
            "en",
            migrated.get_account_settings("acct-1")["drama_language"],
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
        self.assertEqual(0, queue["short_link_id"])
        self.assertEqual("", queue["short_url"])
        self.assertEqual("", queue["long_url"])

    def test_queue_allocates_code_and_freezes_exact_code_macro(self):
        queue = self.add_and_freeze(
            template="Drama ID: {{content_id}}\nCode: {code}"
        )
        self.assertRegex(queue["code"], r"^[A-Z0-9]{4}$")
        self.assertEqual(
            "Drama ID: Y9v1yQcFqM\nCode: %s" % queue["code"],
            queue["caption"],
        )
        route = self.store.get_code_route_for_queue(queue["id"])
        self.assertEqual(queue["code"], route["code"])
        self.assertEqual(queue["content_id"], route["content_id"])
        self.assertEqual("TT", route["af_channel"])
        self.assertEqual("scheduled", route["state"])

    def test_queue_sanitizes_campaign_delimiters_without_rejecting_metadata(self):
        pool = self.store.add_material("1001")
        queue = self.store.freeze_queue(
            pool["id"],
            account(),
            "2026-07-29 10:00:00",
            CAPTION,
            policy(),
            resolver,
            drama_name="The * Contract [Bride]",
            material_language="en[*]",
            material_tag="romance*[vip]",
        )
        route = self.store.get_code_route_for_queue(queue["id"])
        self.assertIn(
            "noneen*The Contract Bride*romance vip*%s" % queue["id"],
            route["c"],
        )
        self.assertNotIn("[Bride]", route["c"])
        self.assertNotIn("[vip]", route["c"])

    def test_queue_freezes_url_macro_and_exact_internal_line_breaks(self):
        template = (
            "Watch the full story\n\n"
            "Drama ID: {{content_id}}\n\n"
            "{url}"
        )
        pool = self.store.add_material("1001")
        queue = self.store.freeze_queue(
            pool["id"],
            account(),
            "2026-07-29 10:00:00",
            template,
            policy(),
            resolver,
            material_name="Material 1001",
            drama_name="The Contract Bride",
            material_language="en",
            material_tag="romance",
        )
        self.assertEqual(
            (
                "Watch the full story\n\n"
                "Drama ID: Y9v1yQcFqM\n\n"
                + queue["short_url"]
            ),
            queue["caption"],
        )
        self.assertEqual(
            queue["id"],
            queue["short_link_id"],
        )
        self.assertEqual(
            "https://gy.g2flow.com/s2l/tt/%s.html" % queue["id"],
            queue["short_url"],
        )
        self.assertFalse(queue["brand_content_toggle"])
        self.assertFalse(queue["brand_organic_toggle"])
        self.assertEqual("", queue["long_url"])

        claim = self.claim_one(queue)
        tracking = {
            "username": queue["account_username"],
            "timestamp": 1784736000,
            "material_language": queue["material_language"],
            "drama_name": queue["drama_name"],
            "tag": queue["material_tag"],
            "link_id": queue["short_link_id"],
            "page_name": queue["account_display_name"],
            "page_id": queue["account_id"],
            "material_name": queue["material_name"],
            "material_id": queue["material_id"],
            "queue_id": queue["id"],
            "content_id": queue["content_id"],
        }
        target = build_w2a_url(tracking)
        prepared = self.store.prepare_short_link(
            queue["id"],
            claim.reveal_claim_token(),
            target,
        )
        self.assertEqual(target, prepared["long_url"])
        replay = self.store.prepare_short_link(
            queue["id"],
            claim.reveal_claim_token(),
            target,
        )
        self.assertEqual(target, replay["long_url"])
        conflict = build_w2a_url(
            {**tracking, "timestamp": 1784736001}
        )
        with self.assertRaises(TTPostError) as caught:
            self.store.prepare_short_link(
                queue["id"],
                claim.reveal_claim_token(),
                conflict,
            )
        self.assertEqual(
            "tt_short_link_target_conflict",
            caught.exception.code,
        )

    def test_concurrent_queue_links_use_each_own_auto_increment_id(self):
        template = "Drama ID: {{content_id}}\n{url}"
        pools = [
            self.store.add_material("81001"),
            self.store.add_material("81002"),
        ]
        stores = [
            TTPostStore(self.db_path, now_fn=self.clock),
            TTPostStore(self.db_path, now_fn=self.clock),
        ]
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def freeze(index):
            try:
                barrier.wait()
                item = stores[index].freeze_queue(
                    pools[index]["id"],
                    account("acct-%s" % (index + 1)),
                    "2026-07-29 10:00:00",
                    template,
                    policy(),
                    resolver,
                    material_name="Concurrent material %s" % index,
                    drama_name="Concurrent drama",
                    material_language="en",
                    material_tag="drama",
                )
                with lock:
                    results.append(item)
            except Exception as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=freeze, args=(index,))
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(2, len({item["id"] for item in results}))
        for item in results:
            self.assertEqual(item["id"], item["short_link_id"])
            self.assertEqual(
                "https://gy.g2flow.com/s2l/tt/%s.html" % item["id"],
                item["short_url"],
            )

    def test_queue_url_macro_requires_frozen_attribution_metadata(self):
        pool = self.store.add_material("1001")
        with self.assertRaises(TTPostError) as caught:
            self.store.freeze_queue(
                pool["id"],
                account(),
                "2026-07-29 10:00:00",
                "Drama ID: {{content_id}}\n\n{url}",
                policy(),
                resolver,
            )
        self.assertEqual(
            "tt_post_link_metadata_incomplete",
            caught.exception.code,
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


class MaterialIntakeStorageTests(CoreTestCase):
    def add_intake(
        self,
        material_id="7101",
        account_id="acct-1",
        *,
        idempotency_key=None,
        description=None,
        preparation_profile="tt-post-hevc-720x1280-v2",
        source_trim_tail_seconds=4.333333,
        template=None,
    ):
        content_id = "CONTENT_%s" % material_id
        frozen_description = (
            "Description %s" % material_id
            if description is None
            else description
        )
        frozen_template = template or "Watch now\nDrama ID: {{contect_id}}"
        return self.store.add_material_intake(
            material_id,
            account_id,
            content_id,
            "https://cdn.example.com/source-%s.mp4" % material_id,
            idempotency_key=(
                idempotency_key or "tt-post-intake:%s" % material_id
            ),
            gpu_job_id="gpu-intake-job-%s" % material_id,
            source_trim_tail_seconds=source_trim_tail_seconds,
            preparation_profile=preparation_profile,
            caption_template=frozen_template,
            caption=render_caption_template(
                frozen_template,
                content_id,
                description=frozen_description,
            ),
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 10:00:00",
            is_aigc=False,
            material_name="material-%s" % material_id,
            drama_name="Drama %s" % material_id,
            material_language="English",
            description=frozen_description,
            actor_user_id="operator-1",
            actor_name="Operator",
        )

    def complete_intake(self, intake, token):
        return self.store.complete_material_intake(
            intake["id"],
            token,
            gpu_job_id=intake["gpu_job_id"],
            prepared_media_url=(
                "https://gpu.example.com/prepared-%s.mp4"
                % intake["material_id"]
            ),
            prepared_output_sha256="a" * 64,
            prepared_output_size=123456,
            prepared_duration_sec=120.25,
            preparation_profile=intake["preparation_profile"],
            source_trim_tail_seconds=intake[
                "source_trim_tail_seconds"
            ],
        )

    def test_enqueue_is_fast_state_idempotent_and_globally_exclusive(self):
        first = self.add_intake()
        replay = self.add_intake()
        replay_with_new_key = self.add_intake(
            idempotency_key="tt-post-intake:new-key"
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["id"], replay_with_new_key["id"])
        self.assertEqual("queued", first["status"])
        self.assertEqual(0, first["attempt_count"])
        self.assertRegex(first["request_sha256"], r"^[a-f0-9]{64}$")
        self.assertNotIn("claim_token", first)
        self.assertNotIn("lease_expires_at_utc", first)
        self.assertEqual(
            [first],
            self.store.list_material_intakes(
                account_id="acct-1",
                status="queued",
            ),
        )
        self.assertEqual(first, self.store.get_material_intake(first["id"]))

        with self.assertRaises(TTPostError) as key_conflict:
            self.add_intake(
                material_id="7102",
                idempotency_key="tt-post-intake:7101",
            )
        self.assertEqual(
            "tt_post_material_intake_idempotency_conflict",
            key_conflict.exception.code,
        )
        with self.assertRaises(TTPostError) as frozen_conflict:
            self.add_intake(
                "7101",
                account_id="acct-2",
                idempotency_key="tt-post-intake:different-account",
            )
        self.assertEqual(
            "tt_post_material_intake_conflict",
            frozen_conflict.exception.code,
        )
        with self.assertRaises(TTPostError) as reverse_legacy:
            self.store.add_material("7101")
        self.assertEqual(
            "tt_post_material_already_exists",
            reverse_legacy.exception.code,
        )

        self.store.add_material("7201")
        with self.assertRaises(TTPostError) as used:
            self.add_intake("7201")
        self.assertEqual("tt_post_material_already_used", used.exception.code)

    def test_description_is_normalized_and_frozen_into_recurring_pool(self):
        intake = self.add_intake(
            "7111",
            description="  First\n\tchapter   summary  ",
            template="Drama ID: {{content_id}}\n{desc}",
        )
        self.assertEqual("First chapter summary", intake["description"])
        self.assertEqual(
            "Drama ID: CONTENT_7111\nFirst chapter summary",
            intake["caption"],
        )
        claim = self.store.claim_material_intake(
            "prepare-worker-desc",
            lease_seconds=60,
        )
        self.complete_intake(intake, claim.reveal_claim_token())
        recurring = self.store.list_recurring_materials(account_id="acct-1")
        self.assertEqual(1, len(recurring))
        self.assertEqual("First chapter summary", recurring[0]["description"])

    def test_completion_rejects_source_url_as_prepared_artifact(self):
        intake = self.add_intake("7112")
        claim = self.store.claim_material_intake(
            "prepare-worker-source",
            lease_seconds=60,
        )
        with self.assertRaises(TTPostError) as caught:
            self.store.complete_material_intake(
                intake["id"],
                claim.reveal_claim_token(),
                gpu_job_id=intake["gpu_job_id"],
                prepared_media_url=intake["source_media_url"],
                prepared_output_sha256="b" * 64,
                prepared_output_size=123456,
                prepared_duration_sec=120.25,
                preparation_profile=intake["preparation_profile"],
                source_trim_tail_seconds=intake["source_trim_tail_seconds"],
            )
        self.assertEqual("tt_prepared_media_matches_source", caught.exception.code)

    def test_claim_lease_renew_and_expired_owner_fencing(self):
        intake = self.add_intake()
        first = self.store.claim_material_intake(
            "prepare-worker-1",
            lease_seconds=60,
        )
        self.assertIsNotNone(first)
        first_token = first.reveal_claim_token()
        self.assertEqual(intake["id"], first.intake_id)
        self.assertEqual("preparing", first.item["status"])
        self.assertEqual(1, first.item["attempt_count"])
        self.assertNotIn("claim_token", first.item)
        self.assertNotIn("lease_expires_at_utc", first.item)
        self.assertNotIn(first_token, repr(first))
        self.assertIsNone(
            self.store.claim_material_intake(
                "prepare-worker-2",
                lease_seconds=60,
            )
        )

        self.clock.current += timedelta(seconds=30)
        renewed = self.store.renew_material_intake(
            intake["id"],
            first_token,
            lease_seconds=60,
        )
        self.assertEqual("preparing", renewed["status"])
        self.clock.current += timedelta(seconds=61)
        second = self.store.claim_material_intake(
            "prepare-worker-2",
            lease_seconds=60,
        )
        self.assertIsNotNone(second)
        second_token = second.reveal_claim_token()
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(2, second.item["attempt_count"])
        with self.assertRaises(TTPostError) as stale:
            self.store.renew_material_intake(
                intake["id"],
                first_token,
                lease_seconds=60,
            )
        self.assertEqual(
            "tt_post_material_intake_claim_invalid",
            stale.exception.code,
        )

    def test_retry_wait_is_due_only_after_backoff_and_failure_is_terminal(self):
        intake = self.add_intake()
        claim = self.store.claim_material_intake(
            "prepare-worker-1",
            lease_seconds=60,
        )
        retry_at = self.clock.current + timedelta(minutes=5)
        waiting = self.store.fail_material_intake(
            intake["id"],
            claim.reveal_claim_token(),
            error_code="tt_gpu_temporarily_unavailable",
            error_message="temporary",
            retry_at=retry_at,
        )
        self.assertEqual("retry_wait", waiting["status"])
        self.assertEqual(
            retry_at.isoformat().replace("+00:00", "Z"),
            waiting["next_attempt_at_utc"],
        )
        self.assertIsNone(
            self.store.claim_material_intake(
                "prepare-worker-2",
                lease_seconds=60,
            )
        )

        self.clock.current = retry_at
        retried = self.store.claim_material_intake(
            "prepare-worker-2",
            lease_seconds=60,
        )
        self.assertIsNotNone(retried)
        failed = self.store.fail_material_intake(
            intake["id"],
            retried.reveal_claim_token(),
            error_code="prepared_media_invalid",
            error_message="permanent",
        )
        self.assertEqual("failed", failed["status"])
        self.assertTrue(failed["failed_at_utc"])
        self.assertIsNone(
            self.store.claim_material_intake(
                "prepare-worker-3",
                lease_seconds=60,
            )
        )
        replay = self.store.fail_material_intake(
            intake["id"],
            "stale-token",
            error_code="prepared_media_invalid",
            error_message="permanent",
        )
        self.assertEqual(failed, replay)

    def test_claim_preserves_fifo_per_account_without_blocking_other_accounts(self):
        first = self.add_intake("7301", "acct-1")
        second = self.add_intake("7302", "acct-1")
        other = self.add_intake("8301", "acct-2")
        first_claim = self.store.claim_material_intake(
            "prepare-worker-1",
            lease_seconds=60,
        )
        self.assertEqual(first["id"], first_claim.intake_id)
        retry_at = self.clock.current + timedelta(minutes=5)
        self.store.fail_material_intake(
            first["id"],
            first_claim.reveal_claim_token(),
            error_code="temporary",
            error_message="retry later",
            retry_at=retry_at,
        )

        other_claim = self.store.claim_material_intake(
            "prepare-worker-2",
            lease_seconds=60,
        )
        self.assertEqual(other["id"], other_claim.intake_id)
        self.store.fail_material_intake(
            other["id"],
            other_claim.reveal_claim_token(),
            error_code="permanent",
            error_message="stop other account",
        )
        self.assertIsNone(
            self.store.claim_material_intake(
                "prepare-worker-3",
                lease_seconds=60,
            )
        )

        self.clock.current = retry_at
        retried = self.store.claim_material_intake(
            "prepare-worker-3",
            lease_seconds=60,
        )
        self.assertEqual(first["id"], retried.intake_id)
        completed = self.complete_intake(
            first,
            retried.reveal_claim_token(),
        )
        self.assertEqual("ready", completed["status"])
        next_claim = self.store.claim_material_intake(
            "prepare-worker-4",
            lease_seconds=60,
        )
        self.assertEqual(second["id"], next_claim.intake_id)

    def test_completion_is_atomic_ready_idempotent_and_enables_fifo(self):
        intake = self.add_intake()
        with self.assertRaises(TTPostError) as empty:
            self.store.claim_recurring_run(
                "tt-post:manual:before-ready",
                "manual",
                "acct-1",
                "2026-07-29",
                "10:00",
                beijing_to_utc("2026-07-29 10:00:00"),
                config_version=0,
                manual_request_key="manual-before-ready",
            )
        self.assertEqual("tt_post_recurring_pool_empty", empty.exception.code)

        claim = self.store.claim_material_intake(
            "prepare-worker-1",
            lease_seconds=60,
        )
        token = claim.reveal_claim_token()
        with self.assertRaises(TTPostError) as mismatch:
            self.store.complete_material_intake(
                intake["id"],
                token,
                gpu_job_id="gpu-intake-job-different",
                prepared_media_url=(
                    "https://gpu.example.com/prepared-7101.mp4"
                ),
                prepared_output_sha256="a" * 64,
                prepared_output_size=123456,
                prepared_duration_sec=120.25,
                preparation_profile=intake["preparation_profile"],
                source_trim_tail_seconds=4.333333,
            )
        self.assertEqual(
            "tt_post_material_intake_artifact_mismatch",
            mismatch.exception.code,
        )
        self.assertEqual(
            0,
            self.store.count_recurring_materials(account_id="acct-1"),
        )
        self.assertEqual(
            "preparing",
            self.store.get_material_intake(intake["id"])["status"],
        )

        completed = self.complete_intake(intake, token)
        self.assertEqual("ready", completed["status"])
        self.assertGreater(completed["recurring_pool_id"], 0)
        self.assertEqual(
            1,
            self.store.count_recurring_materials(
                account_id="acct-1",
                status="available",
            ),
        )
        recurring = self.store.list_recurring_materials(
            account_id="acct-1",
            status="available",
        )[0]
        self.assertEqual(intake["material_id"], recurring["material_id"])
        self.assertEqual(intake["created_at"], recurring["created_at"])
        self.assertEqual(
            completed,
            self.complete_intake(intake, "stale-after-success"),
        )
        claimed = self.store.claim_recurring_run(
            "tt-post:manual:after-ready",
            "manual",
            "acct-1",
            "2026-07-29",
            "10:01",
            beijing_to_utc("2026-07-29 10:01:00"),
            config_version=0,
            manual_request_key="manual-after-ready",
        )
        self.assertEqual(
            completed["recurring_pool_id"],
            claimed["pool_item_id"],
        )
        bridge = self.store.ensure_material_for_recurring(
            intake["material_id"],
            completed["recurring_pool_id"],
        )
        self.assertEqual("available", bridge["status"])
        self.assertEqual(
            bridge,
            self.store.ensure_material_for_recurring(
                intake["material_id"],
                completed["recurring_pool_id"],
            ),
        )

    def test_recurring_bridge_requires_ready_link_and_reserved_pool(self):
        pending = self.add_intake("7401")
        with self.assertRaises(TTPostError) as pending_error:
            self.store.ensure_material_for_recurring(
                pending["material_id"],
                999,
            )
        self.assertEqual(
            "tt_post_recurring_material_bridge_invalid",
            pending_error.exception.code,
        )

        claim = self.store.claim_material_intake(
            "prepare-worker-1",
            lease_seconds=60,
        )
        ready = self.complete_intake(
            pending,
            claim.reveal_claim_token(),
        )
        with self.assertRaises(TTPostError) as not_reserved:
            self.store.ensure_material_for_recurring(
                pending["material_id"],
                ready["recurring_pool_id"],
            )
        self.assertEqual(
            "tt_post_recurring_material_bridge_invalid",
            not_reserved.exception.code,
        )


class RecurringStorageTests(CoreTestCase):
    def add_recurring(
        self,
        material_id,
        account_id="acct-1",
        *,
        description="",
        material_language="en",
        preparation_profile="tt-post-outro-v1",
        template=None,
    ):
        content_id = "CONTENT_%s" % material_id
        frozen_template = template or "Watch now\nDrama ID: {{contect_id}}"
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
            preparation_profile=preparation_profile,
            caption_template=frozen_template,
            caption=render_caption_template(
                frozen_template,
                content_id,
                description=description,
            ),
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 10:00:00",
            is_aigc=False,
            material_language=material_language,
            description=description,
            actor_user_id="operator-1",
            actor_name="Operator",
        )

    def test_random_auto_claim_requires_exact_persisted_slot(self):
        saved = self.store.save_auto_publish_config(
            expected_version=0,
            enabled=True,
            timezone="Asia/Shanghai",
            publish_times=[],
            schedule_mode="random",
            random_daily_count=1,
            account_ids=["acct-1"],
            caption_template=CAPTION,
            user_consent=True,
            consent_version="tt-post-random-v1",
            consented_at="2026-07-29 10:00:00",
        )
        plan = saved["random_daily_plans"][0]
        self.add_recurring("91001", "acct-1")
        with self.assertRaises(TTPostError) as invalid:
            self.store.claim_recurring_run(
                "tt-post:auto:v1:acct-1:2026-07-30:0000",
                "auto",
                "acct-1",
                "2026-07-30",
                "00:00",
                beijing_to_utc("2026-07-30 00:00:00"),
                config_version=plan["config_version"],
            )
        self.assertEqual("tt_post_schedule_not_current", invalid.exception.code)
        publish_time = plan["publish_times"][0]
        claimed = self.store.claim_recurring_run(
            "tt-post:auto:v1:acct-1:2026-07-30:%s"
            % publish_time.replace(":", ""),
            "auto",
            "acct-1",
            "2026-07-30",
            publish_time,
            beijing_to_utc("2026-07-30 %s:00" % publish_time),
            config_version=plan["config_version"],
        )
        self.assertEqual(publish_time, claimed["publish_time"])

    def claim_manual(
        self,
        suffix,
        account_id="acct-1",
        publish_time="10:00",
        required_preparation_profile="",
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
            required_preparation_profile=required_preparation_profile,
        )

    def test_manual_claim_skips_material_from_an_old_preparation_profile(self):
        old = self.add_recurring(
            "98001",
            preparation_profile="tt-post-direct-outro-hevc-720x1280-v1",
        )
        current = self.add_recurring(
            "98002",
            preparation_profile="tt-post-direct-outro-hevc-720x1280-v2",
        )

        claimed = self.claim_manual(
            "profile-filter",
            required_preparation_profile=(
                "tt-post-direct-outro-hevc-720x1280-v2"
            ),
        )

        self.assertEqual(current["id"], claimed["pool_item_id"])
        remaining = {
            item["id"]: item
            for item in self.store.list_recurring_materials(
                account_id="acct-1"
            )
        }
        self.assertEqual(
            "available",
            remaining[old["id"]]["status"],
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
            description=pool_item.get("description") or "",
            recurring_run_id=run["id"],
            recurring_execution_token=execution_token,
        )

    def test_desc_is_frozen_from_recurring_pool_into_queue(self):
        recurring = self.add_recurring(
            "7350",
            description="  Frozen\n drama   description ",
            template="Drama ID: {{content_id}}\n{desc}",
        )
        run = self.claim_manual("desc")
        queue = self.freeze_legacy_queue_for_run(run)
        self.assertEqual("Frozen drama description", recurring["description"])
        self.assertEqual("Frozen drama description", queue["description"])
        self.assertEqual(
            "Drama ID: CONTENT_7350\nFrozen drama description",
            queue["caption"],
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

    def test_disable_schedule_is_atomic_idempotent_and_preserves_history(self):
        saved = self.store.save_daily_schedule(
            "acct-1",
            ["18:30", "09:05"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 10:00:00",
            actor_user_id="operator-1",
            actor_name="Operator One",
        )
        self.clock.current += timedelta(minutes=1)

        disabled = self.store.disable_daily_schedule(
            "acct-1",
            expected_version=saved["version"],
            actor_user_id="operator-2",
            actor_name="Operator Two",
        )

        self.assertFalse(disabled["enabled"])
        self.assertEqual(saved["version"] + 1, disabled["version"])
        self.assertEqual(saved["publish_times"], disabled["publish_times"])
        self.assertEqual(saved["user_consent"], disabled["user_consent"])
        self.assertEqual(
            saved["consent_version"],
            disabled["consent_version"],
        )
        self.assertEqual(
            saved["consented_at_utc"],
            disabled["consented_at_utc"],
        )
        self.assertEqual("operator-2", disabled["updated_by_user_id"])
        self.assertEqual("Operator Two", disabled["updated_by_name"])

        replay = self.store.disable_daily_schedule(
            "acct-1",
            expected_version=disabled["version"],
            actor_user_id="operator-3",
            actor_name="Operator Three",
        )
        self.assertEqual(disabled, replay)
        with self.assertRaises(TTPostError) as stale:
            self.store.disable_daily_schedule(
                "acct-1",
                expected_version=saved["version"],
            )
        self.assertEqual(
            "tt_post_schedule_version_conflict",
            stale.exception.code,
        )

    def test_disable_missing_schedule_is_noop_without_fabricated_consent(self):
        disabled = self.store.disable_daily_schedule(
            "acct-1",
            expected_version=0,
        )
        self.assertEqual(
            self.store.get_daily_schedule("acct-1"),
            disabled,
        )
        self.assertEqual([], self.store.list_daily_schedules())

        with self.assertRaises(TTPostError) as stale:
            self.store.disable_daily_schedule(
                "acct-1",
                expected_version=1,
            )
        self.assertEqual(
            "tt_post_schedule_version_conflict",
            stale.exception.code,
        )

    def test_disable_blocks_a_new_auto_claim_without_consuming_material(self):
        material = self.add_recurring("1099", "acct-1")
        saved = self.store.save_daily_schedule(
            "acct-1",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-recurring-v1",
            consented_at="2026-07-29 09:00:00",
        )
        disabled = self.store.disable_daily_schedule(
            "acct-1",
            expected_version=saved["version"],
        )

        with self.assertRaises(TTPostError) as stopped:
            self.store.claim_recurring_run(
                "tt-post:auto:v1:acct-1:2026-07-29:1000",
                "auto",
                "acct-1",
                "2026-07-29",
                "10:00",
                beijing_to_utc("2026-07-29 10:00:00"),
                config_version=disabled["version"],
            )
        self.assertEqual("tt_post_schedule_not_current", stopped.exception.code)
        self.assertEqual(
            "available",
            self.store.list_recurring_materials(
                account_id="acct-1",
                status="available",
            )[0]["status"],
        )
        self.assertEqual(
            material["id"],
            self.store.list_recurring_materials(
                account_id="acct-1",
                status="available",
            )[0]["id"],
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

    def test_auto_claim_uses_language_fifo_across_preparation_accounts(self):
        spanish = self.add_recurring(
            "1110", "acct-1", material_language="es"
        )
        english = self.add_recurring(
            "1111", "acct-2", material_language="EN"
        )
        self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="en", is_aigc=True),
            expected_version=0,
        )
        schedule = self.store.save_daily_schedule(
            "acct-1",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-language-v1",
            consented_at="2026-07-29 09:00:00",
        )
        self.assertEqual(
            1,
            self.store.count_recurring_materials(
                status="available", drama_language="en"
            ),
        )
        self.assertEqual(
            [english["id"]],
            [
                item["id"]
                for item in self.store.list_recurring_materials(
                    status="available", drama_language="EN"
                )
            ],
        )

        claimed = self.store.claim_recurring_run(
            "tt-post:auto:v1:acct-1:2026-07-29:1000",
            "auto",
            "acct-1",
            "2026-07-29",
            "10:00",
            beijing_to_utc("2026-07-29 10:00:00"),
            config_version=schedule["version"],
        )

        self.assertEqual(english["id"], claimed["pool_item_id"])
        self.assertEqual("acct-1", claimed["pool_item"]["account_id"])
        self.assertEqual("en", claimed["pool_item"]["material_language"])
        self.assertTrue(claimed["pool_item"]["is_aigc"])
        self.assertEqual(
            [spanish["id"]],
            [
                item["id"]
                for item in self.store.list_recurring_materials(
                    status="available",
                    drama_language="es",
                )
            ],
        )

    def test_auto_claim_waits_without_match_and_uses_current_language(self):
        historic_english = self.add_recurring(
            "1120", "acct-2", material_language=""
        )
        saved_settings = self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="es"),
            expected_version=0,
        )
        schedule = self.store.save_daily_schedule(
            "acct-1",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-language-v1",
            consented_at="2026-07-29 09:00:00",
        )
        claim_args = (
            "tt-post:auto:v1:acct-1:2026-07-29:1000",
            "auto",
            "acct-1",
            "2026-07-29",
            "10:00",
            beijing_to_utc("2026-07-29 10:00:00"),
        )

        with self.assertRaises(TTPostError) as no_match:
            self.store.claim_recurring_run(
                *claim_args,
                config_version=schedule["version"],
            )
        self.assertEqual(
            "tt_post_recurring_pool_language_empty",
            no_match.exception.code,
        )
        self.assertEqual(
            "available",
            self.store.list_recurring_materials(status="available")[0][
                "status"
            ],
        )

        self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="EN"),
            expected_version=saved_settings["version"],
        )
        claimed = self.store.claim_recurring_run(
            *claim_args,
            config_version=schedule["version"],
        )
        self.assertEqual(historic_english["id"], claimed["pool_item_id"])
        self.assertEqual("en", claimed["pool_item"]["material_language"])

    def test_language_index_migrates_and_invalid_legacy_row_cannot_block_fifo(self):
        invalid = self.add_recurring(
            "1121", "acct-9", material_language="en"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET material_language='en us' WHERE id=?",
                (invalid["id"],),
            )
            conn.execute(
                "DROP INDEX IF EXISTS "
                "idx_tt_post_recurring_pool_language_fifo"
            )
            rebuild_table_without_column(
                conn,
                "tt_post_recurring_pool",
                "routing_language",
            )
            conn.execute(
                "CREATE INDEX idx_tt_post_recurring_pool_language_fifo "
                "ON tt_post_recurring_pool(status,created_at,id)"
            )
            conn.commit()
        finally:
            conn.close()

        self.store = TTPostStore(self.db_path, now_fn=self.clock)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            self.assertIn(
                "routing_language",
                {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(tt_post_recurring_pool)"
                    ).fetchall()
                },
            )
            self.assertEqual(
                "__invalid__",
                conn.execute(
                    "SELECT routing_language FROM tt_post_recurring_pool "
                    "WHERE id=?",
                    (invalid["id"],),
                ).fetchone()["routing_language"],
            )
            self.assertEqual(
                ["status", "routing_language", "created_at", "id"],
                [
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA index_info("
                        "idx_tt_post_recurring_pool_language_fifo)"
                    ).fetchall()
                ],
            )
        finally:
            conn.close()

        valid = self.add_recurring(
            "1122", "acct-8", material_language="EN"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            query_plan = " ".join(
                str(row[3])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT * FROM tt_post_recurring_pool "
                    "WHERE status='available' AND routing_language=? "
                    "AND id<>? ORDER BY created_at,id LIMIT 1",
                    ("en", invalid["id"]),
                ).fetchall()
            )
        finally:
            conn.close()
        self.assertIn(
            "idx_tt_post_recurring_pool_language_fifo",
            query_plan,
        )
        self.assertEqual(
            "en us",
            self.store.list_recurring_materials()[0]["material_language"],
        )
        self.assertEqual(
            1,
            self.store.count_recurring_materials(
                status="available", drama_language="en"
            ),
        )
        self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="en"),
            expected_version=0,
        )
        schedule = self.store.save_daily_schedule(
            "acct-1",
            ["10:00"],
            enabled=True,
            expected_version=0,
            consent_version="tt-post-language-v1",
            consented_at="2026-07-29 09:00:00",
        )
        claimed = self.store.claim_recurring_run(
            "tt-post:auto:v1:acct-1:2026-07-29:1000",
            "auto",
            "acct-1",
            "2026-07-29",
            "10:00",
            beijing_to_utc("2026-07-29 10:00:00"),
            config_version=schedule["version"],
        )
        self.assertEqual(valid["id"], claimed["pool_item_id"])

    def test_manual_claim_keeps_the_exact_account_pool(self):
        spanish = self.add_recurring(
            "1130", "acct-1", material_language="es"
        )
        self.add_recurring("1131", "acct-2", material_language="en")
        self.store.save_account_settings(
            "acct-1",
            account_settings(drama_language="en"),
            expected_version=0,
        )

        claimed = self.claim_manual("language-exact", "acct-1")

        self.assertEqual(spanish["id"], claimed["pool_item_id"])
        self.assertEqual("es", claimed["pool_item"]["material_language"])

    def test_concurrent_auto_language_claims_never_share_one_material(self):
        first = self.add_recurring("1140", "acct-9", material_language="en")
        second = self.add_recurring("1141", "acct-8", material_language="en")
        for account_id, publish_time in (("acct-1", "10:00"), ("acct-2", "10:01")):
            self.store.save_account_settings(
                account_id,
                account_settings(drama_language="en"),
                expected_version=0,
            )
            self.store.save_daily_schedule(
                account_id,
                [publish_time],
                enabled=True,
                expected_version=0,
                consent_version="tt-post-language-v1",
                consented_at="2026-07-29 09:00:00",
            )

        stores = [
            TTPostStore(self.db_path, now_fn=self.clock),
            TTPostStore(self.db_path, now_fn=self.clock),
        ]
        results = []
        errors = []
        lock = threading.Lock()

        def claim(store, account_id, publish_time):
            try:
                result = store.claim_recurring_run(
                    "tt-post:auto:v1:%s:2026-07-29:%s"
                    % (account_id, publish_time.replace(":", "")),
                    "auto",
                    account_id,
                    "2026-07-29",
                    publish_time,
                    beijing_to_utc(
                        "2026-07-29 %s:00" % publish_time
                    ),
                    config_version=1,
                )
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=claim, args=(stores[0], "acct-1", "10:00")),
            threading.Thread(target=claim, args=(stores[1], "acct-2", "10:01")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(
            {first["id"], second["id"]},
            {item["pool_item_id"] for item in results},
        )

    def test_concurrent_auto_claims_leave_one_waiting_when_only_one_matches(self):
        material = self.add_recurring(
            "1142", "acct-9", material_language="en"
        )
        for account_id, publish_time in (("acct-1", "10:00"), ("acct-2", "10:01")):
            self.store.save_account_settings(
                account_id,
                account_settings(drama_language="en"),
                expected_version=0,
            )
            self.store.save_daily_schedule(
                account_id,
                [publish_time],
                enabled=True,
                expected_version=0,
                consent_version="tt-post-language-v1",
                consented_at="2026-07-29 09:00:00",
            )
        stores = [
            TTPostStore(self.db_path, now_fn=self.clock),
            TTPostStore(self.db_path, now_fn=self.clock),
        ]
        results = []
        errors = []
        lock = threading.Lock()

        def claim(store, account_id, publish_time):
            try:
                claimed = store.claim_recurring_run(
                    "tt-post:auto:v1:%s:2026-07-29:%s"
                    % (account_id, publish_time.replace(":", "")),
                    "auto",
                    account_id,
                    "2026-07-29",
                    publish_time,
                    beijing_to_utc(
                        "2026-07-29 %s:00" % publish_time
                    ),
                    config_version=1,
                )
                with lock:
                    results.append(claimed)
            except TTPostError as exc:
                with lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=claim, args=(stores[0], "acct-1", "10:00")),
            threading.Thread(target=claim, args=(stores[1], "acct-2", "10:01")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())

        self.assertEqual(1, len(results))
        self.assertEqual(material["id"], results[0]["pool_item_id"])
        self.assertEqual(1, len(errors))
        self.assertEqual(
            "tt_post_recurring_pool_language_empty",
            errors[0].code,
        )

    def test_auto_language_fifo_is_global_across_preparation_accounts(self):
        materials = [
            self.add_recurring("1143", "acct-9", material_language="en"),
            self.add_recurring("1144", "acct-8", material_language="EN"),
            self.add_recurring("1145", "acct-7", material_language="en"),
        ]
        claims = []
        for account_id, publish_time in (
            ("acct-1", "10:00"),
            ("acct-2", "10:01"),
            ("acct-3", "10:02"),
        ):
            self.store.save_account_settings(
                account_id,
                account_settings(drama_language="en"),
                expected_version=0,
            )
            schedule = self.store.save_daily_schedule(
                account_id,
                [publish_time],
                enabled=True,
                expected_version=0,
                consent_version="tt-post-language-v1",
                consented_at="2026-07-29 09:00:00",
            )
            claims.append(
                self.store.claim_recurring_run(
                    "tt-post:auto:v1:%s:2026-07-29:%s"
                    % (account_id, publish_time.replace(":", "")),
                    "auto",
                    account_id,
                    "2026-07-29",
                    publish_time,
                    beijing_to_utc(
                        "2026-07-29 %s:00" % publish_time
                    ),
                    config_version=schedule["version"],
                )
            )

        self.assertEqual(
            [item["id"] for item in materials],
            [item["pool_item_id"] for item in claims],
        )

    def test_legacy_recurring_without_intake_can_create_queue_bridge(self):
        material = self.add_recurring("1150", "acct-1")
        run = self.claim_manual("legacy-intake-bridge", "acct-1")
        bridge = self.store.ensure_material_for_recurring(
            material["material_id"],
            run["pool_item_id"],
        )
        self.assertEqual(material["material_id"], bridge["material_id"])
        self.assertEqual("available", bridge["status"])
        self.assertEqual(
            bridge,
            self.store.ensure_material_for_recurring(
                material["material_id"],
                run["pool_item_id"],
            ),
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

    def test_typed_account_loader_error_is_preserved(self):
        expected = AccountSourceError(
            "tt_account_snapshot_refresh_pending",
            "snapshot refresh pending",
            503,
        )

        def pending(_value):
            raise expected

        source = SnapshotAccountSource(
            lambda: [self.metadata()],
            pending,
            lambda _value: None,
        )
        with self.assertRaises(AccountSourceError) as caught:
            source.get_safe_account("acct-1")
        self.assertIs(caught.exception, expected)

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

    def test_caption_url_macro_preserves_exact_line_breaks(self):
        short_url = (
            "https://gy.g2flow.com/s2l/"
            "8000000000000000009.html"
        )
        template = (
            "Watch the full story\n\n"
            "Drama ID: {{content_id}}\n\n"
            "{url}"
        )
        self.assertEqual(
            (
                "Watch the full story\n\n"
                "Drama ID: ABC_123\n\n"
                + short_url
            ),
            render_caption_template(
                template,
                "ABC_123",
                url=short_url,
            ),
        )
        self.assertEqual(
            (
                "Watch the full story\n\n"
                "Drama ID: ABC_123\n\n"
                "{url}"
            ),
            render_caption_template(
                template,
                "ABC_123",
                defer_url=True,
            ),
        )

    def test_caption_url_macro_fails_closed(self):
        with self.assertRaises(TTPostError) as missing:
            render_caption_template(
                "Drama ID: {{content_id}}\n\n{url}",
                "ABC_123",
            )
        self.assertEqual("caption_url_required", missing.exception.code)
        with self.assertRaises(TTPostError) as unknown:
            render_caption_template(
                "Drama ID: {{content_id}}\n\n{landing_url}",
                "ABC_123",
                defer_url=True,
            )
        self.assertEqual(
            "caption_placeholder_invalid",
            unknown.exception.code,
        )
        with self.assertRaises(TTPostError) as spaced:
            render_caption_template(
                "Drama ID: {{content_id}}\n\n{ url }",
                "ABC_123",
                defer_url=True,
            )
        self.assertEqual(
            "caption_placeholder_invalid",
            spaced.exception.code,
        )
        for malformed in ("{url", "url}", "{desc", "desc}"):
            with self.subTest(malformed=malformed):
                with self.assertRaises(TTPostError) as incomplete:
                    render_caption_template(
                        "Drama ID: {{content_id}}\n" + malformed,
                        "ABC_123",
                        defer_url=True,
                        defer_description=True,
                    )
                self.assertEqual(
                    "caption_placeholder_invalid",
                    incomplete.exception.code,
                )

    def test_caption_desc_macro_is_normalized_repeated_and_single_pass(self):
        short_url = "https://gy.g2flow.com/s2l/8000000000000000009.html"
        template = (
            "Drama ID: {{content_id}}\n"
            "{desc}\n{url}\n{desc}"
        )
        rendered = render_caption_template(
            template,
            "ABC_123",
            url=short_url,
            description="  A\n story with {url} and {{content_id}}  ",
        )
        self.assertEqual(
            "Drama ID: ABC_123\n"
            "A story with {url} and {{content_id}}\n"
            + short_url
            + "\nA story with {url} and {{content_id}}",
            rendered,
        )

    def test_caption_desc_macro_fails_closed_without_valid_description(self):
        for description in (None, "", " \n\t ", "bad\x00value"):
            with self.subTest(description=description):
                with self.assertRaises(TTPostError) as caught:
                    render_caption_template(
                        "Drama ID: {{content_id}}\n{desc}",
                        "ABC_123",
                        description=description,
                    )
                self.assertEqual("caption_desc_required", caught.exception.code)
        with self.assertRaises(TTPostError) as uppercase:
            render_caption_template(
                "Drama ID: {{content_id}}\n{DESC}",
                "ABC_123",
                description="Story",
            )
        self.assertEqual("caption_placeholder_invalid", uppercase.exception.code)

    def test_caption_allows_template_without_content_id_placeholder(self):
        self.assertEqual(
            "Custom copy without a drama placeholder",
            render_caption_template(
                "Custom copy without a drama placeholder",
                "ABC",
            ),
        )

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

    def test_begin_publish_clears_legacy_disclosure_flags(self):
        queue = self.add_and_freeze()
        claim = self.claim_one(queue)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE tt_post_queue SET brand_content_toggle=1, "
                "brand_organic_toggle=1 WHERE id=?",
                (queue["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        started = self.store.begin_publish(
            queue["id"],
            claim.reveal_claim_token(),
            OPEN_GATES,
            now=datetime(2026, 7, 29, 2, 0, 20, tzinfo=UTC),
        )
        self.assertEqual("publishing", started["status"])
        self.assertFalse(started["brand_content_toggle"])
        self.assertFalse(started["brand_organic_toggle"])

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
            "v_pub_url~v2-1.7668584571734657042",
            now=datetime(2026, 7, 29, 2, 0, 30, tzinfo=UTC),
        )
        self.assertEqual("reconciling", pending["status"])
        self.assertEqual(
            "reconciling",
            self.store.get_code_route_for_queue(queue["id"])["state"],
        )
        self.assertEqual(
            "v_pub_url~v2-1.7668584571734657042",
            pending["publish_id"],
        )
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
            "v_pub_url~v2-1.7668584571734657042",
            publish_url="https://www.tiktok.com/@dramawave/video/123",
        )
        replay = self.store.reconcile_published(
            queue["id"],
            "v_pub_url~v2-1.7668584571734657042",
            publish_url="https://www.tiktok.com/@dramawave/video/123",
        )
        self.assertEqual("published", published["status"])
        self.assertEqual("published", replay["status"])
        published_route = self.store.get_code_route_for_queue(queue["id"])
        self.assertEqual("published", published_route["state"])
        self.assertTrue(published_route["published_at"])

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

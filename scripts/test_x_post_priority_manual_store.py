#!/usr/bin/env python3
"""Offline store contracts for drama priority and durable manual publishing."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service  # noqa: E402


ACTOR = {"user_id": "admin-1", "name": "Admin"}


def compliance():
    return {
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
        "dangerous_tag_count": 0,
    }


def material_candidate(run, account_id, material_id):
    material_id = str(material_id)
    return {
        "account_id": int(account_id),
        "account_username": "account%s" % account_id,
        "source_date": run["source_date"],
        "source_type": "material",
        "pool_item_id": None,
        "pool_created_at": "",
        "material_key": material_id,
        "material_id": material_id,
        "content_id": "content-%s" % material_id,
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "drama_name": "Drama %s" % material_id,
        "tag": "Romance",
        "description": "A complete and safe drama description.",
        "page_name": "Account %s" % account_id,
        "page_id": "900%s" % account_id,
        "spend": 0,
        "preflight_sha256": ("%064x" % int(material_id))[-64:],
        "preflight_size": 2048,
        "preflight_duration": 30.0,
        **compliance(),
    }


def drama_check(content_id):
    return {
        "content_id": content_id,
        "drama_name": "Drama %s" % content_id,
        "description": "A complete and safe drama description.",
        "language": "en",
        "labels": "Romance",
        "name_tag": "#Drama_%s" % content_id,
        "free_episode_count": 3,
    }


def drama_candidate(pool, account_id):
    content_id = str(pool["content_id"])
    return {
        "account_id": int(account_id),
        "account_username": "account%s" % account_id,
        "source_date": "2026-08-10",
        "source_type": "drama",
        "material_id": "%s-E1" % content_id,
        "content_id": content_id,
        "material_url": "https://media.example.test/%s-E1.mp4" % content_id,
        "material_name": "%s Episode 1" % content_id,
        "material_language": "en",
        "drama_name": "Drama %s" % content_id,
        "tag": "Romance",
        "description": "A complete and safe drama description.",
        "page_name": "Account %s" % account_id,
        "page_id": "900%s" % account_id,
        "drama_pool_item_id": int(pool["id"]),
        "drama_pool_created_at": str(pool["created_at"]),
        "episode_number": 1,
        "episode_key": "%s:1" % content_id,
        "drama_replay_generation": 1,
        "name_tag": "#Drama_%s" % content_id,
        "preflight_sha256": ("%064x" % (1000 + int(pool["id"]))) [-64:],
        "preflight_size": 2048,
        "preflight_duration": 30.0,
        **compliance(),
    }


class XPostPriorityManualStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "x-post.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temporary.cleanup()

    def add_dramas(self, *content_ids):
        result = self.store.add_drama_pool_items(
            list(content_ids),
            [drama_check(content_id) for content_id in content_ids],
            ACTOR,
        )
        return result["items"]

    def test_high_priority_is_newest_first_and_does_not_displace_owned_drama(self):
        first, second, newest = self.add_dramas("D1", "D2", "D3")
        self.assertEqual(
            [item["content_id"] for item in self.store.available_drama_pool_items(3)],
            ["D3", "D2", "D1"],
        )

        first_priority = self.store.set_drama_pool_priority(first["id"], True, ACTOR)
        second_priority = self.store.set_drama_pool_priority(second["id"], True, ACTOR)
        self.assertTrue(first_priority["priority_at"])
        self.assertGreater(second_priority["priority_at"], first_priority["priority_at"])
        self.assertEqual(
            [item["content_id"] for item in self.store.available_drama_pool_items(3)],
            ["D2", "D1", "D3"],
        )

        schedule = self.store.save_schedule_config(
            "drama",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2, 3],
                "publish_times": ["09:00"],
                "version": 1,
            },
            ACTOR,
            eligible_account_ids=[2, 3, 4],
            now=datetime(2026, 8, 11, 8, 0, tzinfo=service.BEIJING_TZ),
        )
        pools = {
            item["content_id"]: item
            for item in self.store.query_drama_pool()["items"]
        }
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-08-11",
            "09:00",
            schedule["version"],
            [
                drama_candidate(pools["D2"], 2),
                drama_candidate(pools["D1"], 3),
            ],
        )
        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in plan["queues"]],
            [(2, "D2"), (3, "D1")],
        )
        assigned = {
            item["content_id"]: item
            for item in self.store.query_drama_pool()["items"]
        }
        self.assertEqual(assigned["D2"]["assigned_account_id"], 2)
        self.assertEqual(assigned["D1"]["assigned_account_id"], 3)
        self.assertEqual(assigned["D2"]["priority_at"], "")
        self.assertEqual(assigned["D1"]["priority_at"], "")
        with self.assertRaises(service.XPostError) as conflict:
            self.store.set_drama_pool_priority(first["id"], True, ACTOR)
        self.assertEqual(conflict.exception.code, "x_post_drama_priority_conflict")

        fourth = self.add_dramas("D4")[0]
        self.store.set_drama_pool_priority(newest["id"], True, ACTOR)
        assignments = self.store.available_drama_pool_items(
            3,
            account_ids=[2, 3, 4],
        )
        self.assertEqual(
            [(item["candidate_account_id"], item["content_id"]) for item in assignments],
            [(2, "D2"), (3, "D1"), (4, "D3")],
        )
        self.store.set_drama_pool_priority(newest["id"], False, ACTOR)
        assignments = self.store.available_drama_pool_items(
            3,
            account_ids=[2, 3, 4],
        )
        self.assertEqual(assignments[2]["content_id"], "D4")
        self.assertEqual(assignments[2]["id"], fourth["id"])

    def test_validation_failure_clears_high_priority(self):
        drama = self.add_dramas("D5")[0]
        prioritized = self.store.set_drama_pool_priority(drama["id"], True, ACTOR)
        self.store.record_drama_pool_checks(
            [
                {
                    "pool_item_id": drama["id"],
                    "content_id": drama["content_id"],
                    "error_code": "drama_episode_gap",
                    "error_message": "episode gap",
                }
            ]
        )
        rejected = self.store.query_drama_pool(
            {"drama_id": drama["content_id"]}
        )["items"][0]
        self.assertTrue(prioritized["priority_at"])
        self.assertEqual(rejected["status"], "validation_failed")
        self.assertEqual(rejected["priority_at"], "")
        self.assertEqual(rejected["priority_by_user_id"], "")
        self.assertEqual(rejected["priority_by_name"], "")

    def test_manual_run_is_idempotent_pool_free_atomic_and_allows_history_reuse(self):
        run = self.store.create_manual_run(
            ["101", "102"],
            [2, 3],
            "manual-key-1",
            ACTOR,
        )
        self.assertTrue(run["created"])
        self.assertEqual(run["publish_mode"], "immediate")
        self.assertEqual(run["scheduled_at"], "")
        self.assertEqual(run["scheduled_timezone"], "Asia/Shanghai")
        replay = self.store.create_manual_run(
            ["101", "102"],
            [2, 3],
            "manual-key-1",
            ACTOR,
        )
        self.assertFalse(replay["created"])
        self.assertEqual(replay["id"], run["id"])
        with self.assertRaises(service.XPostError) as conflict:
            self.store.create_manual_run(
                ["103", "104"],
                [2, 3],
                "manual-key-1",
                ACTOR,
            )
        self.assertEqual(conflict.exception.code, "x_post_idempotency_conflict")
        self.assertEqual(self.store.query_pool({})["pagination"]["total"], 0)

        claimed = self.store.claim_manual_run()
        self.assertTrue(claimed["found"])
        self.assertEqual(claimed["run"]["id"], run["id"])
        plan = self.store.create_manual_plan(
            run["id"],
            [
                material_candidate(run, 2, "101"),
                material_candidate(run, 3, "102"),
            ],
        )
        self.assertTrue(plan["created"])
        self.assertEqual(
            [(item["account_id"], item["material_id"]) for item in plan["queues"]],
            [(2, "101"), (3, "102")],
        )
        self.assertTrue(all(item["manual_run_id"] == run["id"] for item in plan["queues"]))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue "
                    "WHERE manual_run_id=? AND pool_item_id IS NOT NULL",
                    (run["id"],),
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.store.query_pool({})["pagination"]["total"], 0)

        reused = self.store.create_manual_run(
            ["102"],
            [4],
            "manual-key-reused",
            ACTOR,
        )
        reused_plan = self.store.create_manual_plan(
            reused["id"],
            [material_candidate(reused, 4, "102")],
        )
        self.assertTrue(reused_plan["created"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT material_key,pool_item_id FROM x_post_queue "
                    "WHERE manual_run_id=?",
                    (reused["id"],),
                ).fetchone(),
                ("102", None),
            )
        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue WHERE material_key='102'"
                ).fetchone()[0],
                2,
            )

        second_run = self.store.create_manual_run(
            ["105", "106"],
            [2, 3],
            "manual-key-2",
            ACTOR,
        )
        second_plan = self.store.create_manual_plan(
            second_run["id"],
            [
                material_candidate(second_run, 2, "105"),
                material_candidate(second_run, 3, "106"),
            ],
        )
        self.assertEqual(len(second_plan["queues"]), 2)

    def test_manual_plan_allows_pool_material_without_binding_pool_row(self):
        self.store.add_pool_materials(
            ["202"],
            ACTOR,
            validation_checks=[
                {"material_id": "202", "error_code": "", "error_message": ""}
            ],
        )
        run = self.store.create_manual_run(
            ["201", "202"],
            [2, 3],
            "manual-pool-reuse",
            ACTOR,
        )
        plan = self.store.create_manual_plan(
            run["id"],
            [
                material_candidate(run, 2, "201"),
                material_candidate(run, 3, "202"),
            ],
        )
        current = self.store.get_manual_run(run["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(len(current["queues"]), 2)
        self.assertTrue(plan["created"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue "
                    "WHERE manual_run_id=? AND pool_item_id IS NOT NULL",
                    (run["id"],),
                ).fetchone()[0],
                0,
            )

    def test_manual_reservation_prevents_pool_race_and_failure_releases_material(self):
        run = self.store.create_manual_run(
            ["201", "202"],
            [2, 3],
            "manual-race",
            ACTOR,
        )
        blocked = self.store.add_pool_materials(
            ["202"],
            ACTOR,
            validation_checks=[
                {"material_id": "202", "error_code": "", "error_message": ""}
            ],
        )
        self.assertEqual(blocked["added_count"], 0)
        self.assertEqual(blocked["already_used_count"], 1)
        self.assertEqual(
            blocked["skipped_items"][0]["code"],
            "x_post_pool_material_manual_reserved",
        )
        plan = self.store.create_manual_plan(
            run["id"],
            [
                material_candidate(run, 2, "201"),
                material_candidate(run, 3, "202"),
            ],
        )
        self.assertEqual(len(plan["queues"]), 2)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_manual_material_reservation "
                    "WHERE manual_run_id=? AND state='consumed'",
                    (run["id"],),
                ).fetchone()[0],
                2,
            )

        failed_run = self.store.create_manual_run(
            ["203"],
            [4],
            "manual-release",
            ACTOR,
        )
        failed = self.store.record_manual_failure(
            failed_run["id"],
            "x_post_manual_source_preflight_failed",
            "one selected material became unavailable",
        )
        self.assertEqual(failed["status"], "failed_preflight")
        self.assertEqual(failed["queued_count"], 0)
        released = self.store.add_pool_materials(
            ["203"],
            ACTOR,
            validation_checks=[
                {"material_id": "203", "error_code": "", "error_message": ""}
            ],
        )
        self.assertEqual(released["added_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            reservation = conn.execute(
                "SELECT state,release_reason "
                "FROM x_post_manual_material_reservation WHERE manual_run_id=?",
                (failed_run["id"],),
            ).fetchone()
        self.assertEqual(reservation, ("released", "x_post_manual_source_preflight_failed"))

    def test_scheduled_manual_run_is_durable_and_not_claimed_before_due_time(self):
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T06:00:00Z",
        ):
            run = self.store.create_manual_run(
                ["211"],
                [2],
                "manual-scheduled-1",
                ACTOR,
                publish_mode="scheduled",
                scheduled_at="2026-08-13T00:05:00+08:00",
            )
        self.assertTrue(run["created"])
        self.assertEqual(run["publish_mode"], "scheduled")
        self.assertEqual(run["scheduled_at"], "2026-08-12T16:05:00Z")
        self.assertEqual(run["scheduled_timezone"], "Asia/Shanghai")
        self.assertEqual(run["run_date"], "2026-08-13")
        self.assertEqual(run["source_date"], "2026-08-12")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            reservation = conn.execute(
                "SELECT material_key,state FROM x_post_manual_material_reservation "
                "WHERE manual_run_id=?",
                (run["id"],),
            ).fetchone()
        self.assertEqual(reservation, ("211", "active"))

        direct = material_candidate(run, 2, "211")
        direct["body_template"] = run["body_template"]
        values = self.store._queue_payload(
            direct,
            run_date=run["run_date"],
            candidate_rank=1,
            require_compliance=True,
        )
        values["idempotency_key"] = "direct-queue-reservation-bypass"
        columns = ("idempotency_key",) + service.QUEUE_LEDGER_FIELDS + service.QUEUE_FIELDS
        timestamp = "2026-08-12T06:01:00Z"
        with contextlib.closing(service._connect(self.db_path)) as conn:
            placeholders = ",".join("?" for _field in columns)
            with self.assertRaises(sqlite3.IntegrityError) as guarded:
                conn.execute(
                    "INSERT INTO x_post_queue("
                    + ",".join(columns)
                    + ",status,created_at,updated_at) VALUES("
                    + placeholders
                    + ",'queued',?,?)",
                    tuple(values[field] for field in columns)
                    + (timestamp, timestamp),
                )
        self.assertIn("reserved by manual run", str(guarded.exception))

        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T16:04:59Z",
        ):
            self.assertFalse(self.store.claim_manual_run()["found"])
        self.assertEqual(self.store.get_manual_run(run["id"])["status"], "queued")

        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T16:05:00Z",
        ):
            claimed = self.store.claim_manual_run()
        self.assertTrue(claimed["found"])
        self.assertEqual(claimed["run"]["id"], run["id"])
        self.assertEqual(claimed["run"]["status"], "running")
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T16:05:01Z",
        ):
            resumed = self.store.claim_manual_run()
        self.assertTrue(resumed["found"])
        self.assertEqual(resumed["run"]["id"], run["id"])
        self.assertEqual(resumed["run"]["status"], "running")

    def test_scheduled_manual_preserves_pool_and_history_reuse_contract(self):
        added = self.store.add_pool_materials(
            ["212"],
            ACTOR,
            validation_checks=[
                {"material_id": "212", "error_code": "", "error_message": ""}
            ],
        )
        self.assertEqual(added["added_count"], 1)
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T06:00:00Z",
        ):
            run = self.store.create_manual_run(
                ["212"],
                [2],
                "manual-scheduled-pool-reuse",
                ACTOR,
                publish_mode="scheduled",
                scheduled_at="2026-08-13T00:05:00+08:00",
            )
        self.assertEqual(self.store.available_pool_items(10), [])
        pool = self.store.query_pool({"material_id": "212"})["items"]
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["availability"], "occupied")

        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T16:05:00Z",
        ):
            claimed = self.store.claim_manual_run()
            plan = self.store.create_manual_plan(
                claimed["run"]["id"],
                [material_candidate(run, 2, "212")],
            )
        self.assertTrue(plan["created"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM x_post_manual_material_reservation "
                    "WHERE manual_run_id=?",
                    (run["id"],),
                ).fetchone(),
                ("consumed",),
            )

        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T16:06:00Z",
        ):
            replay = self.store.create_manual_run(
                ["212"],
                [3],
                "manual-scheduled-history-reuse",
                ACTOR,
                publish_mode="scheduled",
                scheduled_at="2026-08-13T00:10:00+08:00",
            )
        self.assertTrue(replay["created"])
        self.assertEqual(replay["publish_mode"], "scheduled")
        self.assertEqual(replay["scheduled_at"], "2026-08-12T16:10:00Z")

    def test_scheduled_manual_time_validation_fails_closed_without_rows(self):
        invalid_timing = (
            {"publish_mode": "later", "scheduled_at": ""},
            {"publish_mode": "scheduled", "scheduled_at": ""},
            {
                "publish_mode": "immediate",
                "scheduled_at": "2026-08-12T15:00:00+08:00",
            },
            {
                "publish_mode": "scheduled",
                "scheduled_at": "2026-08-12T15:00:00",
            },
            {
                "publish_mode": "scheduled",
                "scheduled_at": "2026-08-12T15:00:01+08:00",
            },
            {
                "publish_mode": "scheduled",
                "scheduled_at": "2026-08-12T13:59:00+08:00",
            },
        )
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T06:00:00Z",
        ):
            for index, timing in enumerate(invalid_timing, 1):
                with self.subTest(timing=timing), self.assertRaises(
                    service.XPostError
                ) as rejected:
                    self.store.create_manual_run(
                        [str(230 + index)],
                        [index],
                        "manual-invalid-time-%s" % index,
                        ACTOR,
                        **timing,
                    )
                self.assertEqual(rejected.exception.code, "invalid_request")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM x_post_manual_run").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_manual_material_reservation"
                ).fetchone()[0],
                0,
            )

    def test_scheduled_manual_idempotency_replays_after_due_and_rejects_changes(self):
        create_args = (
            ["221"],
            [2],
            "manual-scheduled-idempotent",
            ACTOR,
        )
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T06:00:00Z",
        ):
            created = self.store.create_manual_run(
                *create_args,
                publish_mode="scheduled",
                scheduled_at="2026-08-12T15:00:00+08:00",
            )
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T08:00:00Z",
        ):
            replay = self.store.create_manual_run(
                *create_args,
                publish_mode="scheduled",
                scheduled_at="2026-08-12T15:00:00+08:00",
            )
            with self.assertRaises(service.XPostError) as past:
                self.store.create_manual_run(
                    ["222"],
                    [3],
                    "manual-scheduled-past",
                    ACTOR,
                    publish_mode="scheduled",
                    scheduled_at="2026-08-12T15:00:00+08:00",
                )
        self.assertFalse(replay["created"])
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(past.exception.code, "invalid_request")
        with self.assertRaises(service.XPostError) as conflict:
            self.store.create_manual_run(
                *create_args,
                publish_mode="scheduled",
                scheduled_at="2026-08-12T15:05:00+08:00",
            )
        self.assertEqual(conflict.exception.code, "x_post_idempotency_conflict")

    def test_manual_run_aggregates_known_success_and_failure_without_x_calls(self):
        run = self.store.create_manual_run(
            ["301", "302"],
            [2, 3],
            "manual-outcomes",
            ACTOR,
        )
        plan = self.store.create_manual_plan(
            run["id"],
            [
                material_candidate(run, 2, "301"),
                material_candidate(run, 3, "302"),
            ],
        )
        first_log = self.store.reserve_log(plan["queues"][0]["id"])
        self.store.prepare_log(
            first_log["id"],
            "https://www.dramawavew2a.com/view?queue=1",
            "https://gy.g2flow.com/s2l/1.html",
            "safe post text",
        )
        self.store.mark_publishing(first_log["id"])
        self.store.mark_media_uploaded(first_log["id"], "media301")
        self.store.mark_published(
            first_log["id"],
            "media301",
            "900301",
            "https://x.com/account2/status/900301",
        )
        second_log = self.store.reserve_log(plan["queues"][1]["id"])
        self.store.mark_failed_if_reserved(
            second_log["id"],
            "invalid_short_base_url",
            "known failure before any X write",
        )
        final = self.store.get_manual_run(run["id"])
        self.assertEqual(final["status"], "completed_with_errors")
        self.assertEqual(final["published_count"], 1)
        self.assertEqual(final["failed_count"], 1)
        self.assertEqual(final["unknown_count"], 0)

    def test_manual_claim_terminalizes_interrupted_publish_without_retry(self):
        run = self.store.create_manual_run(
            ["501"],
            [2],
            "manual-interrupted-1",
            ACTOR,
        )
        self.store.claim_manual_run()
        plan = self.store.create_manual_plan(
            run["id"],
            [material_candidate(run, 2, "501")],
        )
        queue_id = plan["queues"][0]["id"]
        log = self.store.reserve_log(queue_id)
        self.store.prepare_log(
            log["id"],
            "https://example.com/long",
            "https://example.com/short",
            "safe post text",
        )
        self.store.mark_publishing(log["id"])

        reclaimed = self.store.claim_manual_run()
        self.assertTrue(reclaimed["found"])
        self.assertEqual(reclaimed["run"]["status"], "stopped")
        self.assertEqual(
            reclaimed["run"]["error_code"],
            "x_post_manual_interrupted",
        )
        self.assertEqual(reclaimed["run"]["queues"][0]["status"], "publishing")
        self.assertFalse(self.store.claim_manual_run()["found"])

    def test_schema_migration_is_repeatable_and_has_manual_integrity_guards(self):
        service.XPostStore(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("x_post_manual_run", tables)
            self.assertIn("x_post_manual_material_reservation", tables)
            queue_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(x_post_queue)")
            }
            drama_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(x_post_drama_pool)")
            }
            self.assertIn("manual_run_id", queue_columns)
            manual_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(x_post_manual_run)")
            }
            self.assertTrue(
                {"publish_mode", "scheduled_at", "scheduled_timezone"}.issubset(
                    manual_columns
                )
            )
            self.assertTrue(
                {"priority_at", "priority_by_user_id", "priority_by_name"}.issubset(
                    drama_columns
                )
            )
            objects = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
                )
            }
            self.assertTrue(
                {
                    "ux_x_post_queue_manual_account",
                    "idx_x_post_queue_manual",
                    "idx_x_post_manual_run_status",
                    "idx_x_post_manual_run_due",
                    "ux_x_post_manual_reservation_active_material",
                    "idx_x_post_drama_pool_priority",
                    "trg_x_post_queue_manual_insert",
                    "trg_x_post_queue_manual_update",
                    "trg_x_post_queue_batch_parent_insert",
                    "trg_x_post_queue_batch_parent_update",
                    "trg_x_post_manual_run_timing_insert",
                    "trg_x_post_manual_reservation_insert_guard",
                    "trg_x_post_queue_manual_reservation_insert",
                }.issubset(objects)
            )
            with self.assertRaises(sqlite3.IntegrityError) as invalid_timing:
                conn.execute(
                    "INSERT INTO x_post_manual_run("
                    "idempotency_key,trigger_source,publish_mode,scheduled_at,"
                    "scheduled_timezone,run_date,source_date,account_ids_json,"
                    "material_ids_json,body_template,actor_user_id,actor_name,"
                    "status,expected_count,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "direct-noncanonical-timing",
                        "manual",
                        "scheduled",
                        "2026-08-12T15:00:00+08:00",
                        "Asia/Shanghai",
                        "2026-08-12",
                        "2026-08-11",
                        "[2]",
                        '["991"]',
                        "{{drama_name}} {{desc}}",
                        "admin-1",
                        "Admin",
                        "queued",
                        1,
                        "2026-08-12T06:00:00Z",
                        "2026-08-12T06:00:00Z",
                    ),
                )
            self.assertIn("timing invalid", str(invalid_timing.exception))
            conn.rollback()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()

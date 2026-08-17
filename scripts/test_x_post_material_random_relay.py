#!/usr/bin/env python3
"""Offline acceptance tests for ordinary material random Premium relay."""

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service
from features.x_accounts import oauth_service
from scripts import x_post_schedule_runner as runner
from scripts.x_post_daily_runner import CandidatePreflightError


def compliance():
    return {
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
        "dangerous_tag_count": 0,
    }


def account(account_id, language="en", premium=False, username=None):
    username = username or "account%s" % account_id
    return {
        "id": account_id,
        "username": username,
        "x_user_id": "900%s" % account_id,
        "display_name": "Account %s" % account_id,
        "drama_language": language,
        "long_video_eligible": premium,
        "long_video_publish_eligible": premium,
    }


def pool_candidate(material_id, duration, language="en"):
    return {
        "pool_item_id": int(material_id),
        "pool_created_at": "2026-08-17T00:00:%02dZ" % int(material_id),
        "material_id": str(material_id),
        "material_language": language,
        "duration_for_test": float(duration),
    }


class FakeRelaySidecar:
    def __init__(self, relays):
        self.relays = list(relays)
        self.calls = []

    def premium_relay_accounts(self, run_date, drama_language="en"):
        self.calls.append((run_date, drama_language))
        return [dict(item) for item in self.relays]


def fake_preflight(
    _config,
    candidate,
    selected_account,
    rank,
    _timestamp,
    _destination,
    _downloader,
    _prober,
    **_kwargs,
):
    duration = float(candidate["duration_for_test"])
    if duration > 140.0 and not selected_account.get("long_video_eligible"):
        raise CandidatePreflightError(
            "long video requires Premium",
            code="x_long_video_requires_premium",
        )
    return {
        **candidate,
        "account_id": int(selected_account["id"]),
        "account_username": str(selected_account["username"]),
        "page_name": str(selected_account["display_name"]),
        "page_id": str(selected_account["x_user_id"]),
        "preflight_duration": duration,
        "candidate_rank": rank,
        "delivery_mode": "direct",
        "relay_account_id": 0,
        "relay_account_username": "",
    }


class MaterialAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(work_dir=self.temp.name)
        self.identity = {
            "source_type": "material",
            "run_date": "2026-08-17",
            "publish_time": "09:17",
            "version": 7,
        }

    def tearDown(self):
        self.temp.cleanup()

    def preflight(self, candidates, accounts, relays=(), shuffler=None):
        options = {
            "source_date": "2026-08-16",
            "timestamp": 1786900000,
            "downloader": object(),
            "prober": object(),
            "repair_client": None,
            "assignment_identity": self.identity,
        }
        if shuffler is not None:
            options["stable_shuffler"] = shuffler
        with mock.patch.object(
            runner, "_preflight_candidate", side_effect=fake_preflight
        ):
            return runner._preflight_material_candidates(
                self.config,
                FakeRelaySidecar(relays),
                candidates,
                accounts,
                **options,
            )

    def test_stable_seed_and_injected_shuffle_have_no_probability_assertion(self):
        parts = ("target", "material", "2026-08-17", "09:17", 7, "en")
        first = runner._stable_shuffled([1, 2, 3, 4], parts)
        second = runner._stable_shuffled([1, 2, 3, 4], parts)
        self.assertEqual(first, second)
        injected = runner._stable_shuffled(
            [1, 2, 3], parts, shuffle_fn=lambda items, _seed: items[::-1]
        )
        self.assertEqual(injected, [3, 2, 1])

    def test_boundary_140_is_direct_and_140_001_uses_random_relay(self):
        target = account(1)
        relays = [
            account(10, premium=True, username="premium10"),
            account(11, premium=True, username="premium11"),
        ]
        direct, failures = self.preflight(
            [pool_candidate(1, 140.0)], [target], relays
        )
        self.assertFalse(failures)
        self.assertEqual(direct[0]["delivery_mode"], "direct")

        def choose_last(items, _seed):
            return list(items)[::-1]

        relayed, failures = self.preflight(
            [pool_candidate(2, 140.001)],
            [target],
            relays,
            shuffler=choose_last,
        )
        self.assertFalse(failures)
        self.assertEqual(relayed[0]["account_id"], 1)
        self.assertEqual(relayed[0]["delivery_mode"], "premium_relay_repost")
        self.assertEqual(relayed[0]["relay_account_id"], 11)

    def test_no_relay_fails_whole_fifo_batch_without_scanning_short(self):
        publisher = mock.Mock()
        with self.assertRaises(runner.ScheduleRunError) as rejected:
            self.preflight(
                [pool_candidate(1, 180.0), pool_candidate(2, 90.0)],
                [account(1)],
            )
        self.assertEqual(
            rejected.exception.code, "x_post_premium_relay_unavailable"
        )
        failure = runner._preflight_failure_result(
            self.identity,
            rejected.exception,
            {"status": "failed_preflight"},
        )
        self.assertEqual(failure["status"], "failed_preflight")
        self.assertEqual(failure["planned_count"], 0)
        publisher.assert_not_called()

    def test_single_premium_target_keeps_newest_fifo_long_material(self):
        accepted, failures = self.preflight(
            [pool_candidate(1, 180.0), pool_candidate(2, 90.0)],
            [account(1, premium=True)],
        )
        self.assertFalse(failures)
        self.assertEqual([item["material_id"] for item in accepted], ["1"])
        self.assertEqual(accepted[0]["delivery_mode"], "direct")

    def test_language_buckets_never_cross_assign_targets_or_relays(self):
        targets = [account(1, "en"), account(2, "ja")]
        relays = [
            account(10, "ja", premium=True, username="premium10"),
            account(11, "en", premium=True, username="premium11"),
        ]
        accepted, failures = self.preflight(
            [pool_candidate(1, 180.0, "en"), pool_candidate(2, 90.0, "ja")],
            targets,
            relays,
        )
        self.assertFalse(failures)
        by_material = {item["material_id"]: item for item in accepted}
        self.assertEqual(by_material["1"]["account_id"], 1)
        self.assertEqual(by_material["1"]["relay_account_id"], 11)
        self.assertEqual(by_material["2"]["account_id"], 2)


class MaterialRelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "x-post.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def save_schedule(self):
        return self.store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2],
                "publish_times": ["09:00"],
                "version": 1,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2],
            now=datetime(2026, 8, 17, 8, 0, tzinfo=service.BEIJING_TZ),
        )

    def add_pool(self, material_id="101"):
        return self.store.add_pool_materials(
            [material_id],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[{"material_id": material_id, "error_code": ""}],
        )["items"][0]

    def candidate(self, pool, *, relay_id=10, relay_language="en"):
        del relay_language
        return {
            "account_id": 2,
            "account_username": "target2",
            "account_drama_language": "en",
            "source_date": "2026-08-16",
            "source_type": "material",
            "material_id": str(pool["material_id"]),
            "content_id": "C%s" % pool["material_id"],
            "material_url": "https://media.example.test/%s.mp4"
            % pool["material_id"],
            "material_name": "Episode %s" % pool["material_id"],
            "material_language": "en",
            "drama_name": "Drama One",
            "tag": "Romance",
            "description": "A complete short-drama description.",
            "page_name": "Target 2",
            "page_id": "9002",
            "pool_item_id": int(pool["id"]),
            "pool_created_at": str(pool["created_at"]),
            "preflight_sha256": "a" * 64,
            "preflight_size": 1024,
            "preflight_duration": 180.0,
            "delivery_mode": "premium_relay_repost",
            "relay_account_id": relay_id,
            "relay_account_username": "premium%s" % relay_id,
            **compliance(),
        }

    def create_plan(self, relay_language="en"):
        saved = self.save_schedule()
        pool = self.add_pool()
        candidate = self.candidate(pool)
        plan = self.store.create_schedule_plan(
            "material",
            "2026-08-17",
            "09:00",
            saved["version"],
            [candidate],
            premium_account_ids=[],
            premium_relay_accounts=[
                {
                    "id": 10,
                    "username": "premium10",
                    "drama_language": relay_language,
                }
            ],
        )
        return plan, pool, candidate

    def test_schedule_freezes_target_material_and_relay_on_restart(self):
        plan, _pool, candidate = self.create_plan()
        queue = plan["queues"][0]
        replayed = self.store.create_schedule_plan(
            "material",
            "2026-08-17",
            "09:00",
            plan["config_version"],
            [candidate],
            premium_account_ids=[],
            premium_relay_accounts=[
                {"id": 10, "username": "premium10", "drama_language": "en"}
            ],
        )
        self.assertFalse(replayed["created"])
        self.assertEqual(replayed["queues"][0]["id"], queue["id"])
        self.assertEqual(replayed["queues"][0]["account_id"], 2)
        self.assertEqual(replayed["queues"][0]["relay_account_id"], 10)

    def test_pool_becomes_published_only_after_target_repost(self):
        plan, pool, _candidate = self.create_plan()
        queue = plan["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?x=1",
            "https://gy.g2flow.com/s2l/%s.html" % log["id"],
            "Drama One",
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media1")
        self.store.mark_relay_source_published(
            log["id"],
            "media1",
            "9001",
            "https://x.com/premium10/status/9001",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM x_post_material_pool WHERE id=?",
                    (pool["id"],),
                ).fetchone()[0],
                "unpublished",
            )
        self.store.mark_reposting(queue["id"])
        self.store.mark_reposted(queue["id"], "99001")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM x_post_material_pool WHERE id=?",
                    (pool["id"],),
                ).fetchone()[0],
                "published",
            )

    def test_no_relay_or_cross_language_relay_rolls_back_whole_plan(self):
        for relay_accounts in (
            [],
            [{"id": 10, "username": "premium10", "drama_language": "ja"}],
        ):
            with self.subTest(relay_accounts=relay_accounts):
                saved = self.save_schedule()
                pool = self.add_pool("10%s" % (len(relay_accounts) + 2))
                with self.assertRaises(service.XPostError) as rejected:
                    self.store.create_schedule_plan(
                        "material",
                        "2026-08-17",
                        "09:00",
                        saved["version"],
                        [self.candidate(pool)],
                        premium_account_ids=[],
                        premium_relay_accounts=relay_accounts,
                    )
                self.assertEqual(
                    rejected.exception.code,
                    "x_post_premium_relay_unavailable",
                )
                with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM x_post_repost_ledger"
                        ).fetchone()[0],
                        0,
                    )
                    status = conn.execute(
                        "SELECT status FROM x_post_material_pool WHERE id=?",
                        (pool["id"],),
                    ).fetchone()[0]
                    self.assertEqual(status, "unpublished")
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM x_post_queue "
                            "WHERE pool_item_id=? OR material_key=?",
                            (pool["id"], pool["material_key"]),
                        ).fetchone()[0],
                        0,
                    )
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.db_path = Path(self.temp.name) / "x-post.sqlite3"
                self.store = service.XPostStore(self.db_path)

    def test_manual_enqueue_and_short_material_relay_remain_denied(self):
        pool = self.add_pool()
        candidate = self.candidate(pool)
        candidate["pool_item_id"] = None
        candidate["pool_created_at"] = ""
        with self.assertRaises(service.XPostError):
            self.store.enqueue(candidate)

        saved = self.save_schedule()
        short = self.candidate(pool)
        short["preflight_duration"] = 140.0
        with self.assertRaises(service.XPostError):
            self.store.create_schedule_plan(
                "material",
                "2026-08-17",
                "09:00",
                saved["version"],
                [short],
                premium_relay_accounts=[
                    {"id": 10, "username": "premium10", "drama_language": "en"}
                ],
            )

    def test_migration_is_idempotent_and_preserves_relay_history(self):
        plan, _pool, _candidate = self.create_plan()
        queue_id = plan["queues"][0]["id"]
        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        queue = self.store.get_queue(queue_id)
        self.assertEqual(queue["delivery_mode"], "premium_relay_repost")
        self.assertEqual(queue["relay_account_id"], 10)

    def test_material_reassign_requires_language_is_stable_and_attempt_fenced(self):
        plan, _pool, _candidate = self.create_plan()
        queue = plan["queues"][0]
        with self.assertRaises(service.XPostError) as missing_language:
            self.store.reassign_premium_relay(
                queue["id"], [{"id": 11, "username": "premium11"}]
            )
        self.assertEqual(
            missing_language.exception.code,
            "x_account_drama_language_invalid",
        )
        options = [
            {"id": 11, "username": "premium11", "drama_language": "en"},
            {"id": 12, "username": "premium12", "drama_language": "en"},
        ]
        first = self.store.reassign_premium_relay(queue["id"], options)
        second = self.store.reassign_premium_relay(queue["id"], options)
        self.assertEqual(first["relay_account_id"], second["relay_account_id"])
        self.assertIn(first["relay_account_id"], {11, 12})

        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?x=1",
            "https://gy.g2flow.com/s2l/%s.html" % log["id"],
            "Drama One",
        )
        self.store.mark_publishing(log["id"])
        with self.assertRaises(service.XPostError) as fenced:
            self.store.reassign_premium_relay(queue["id"], options)
        self.assertEqual(
            fenced.exception.code, "x_post_relay_reassignment_fenced"
        )

    def test_material_plan_relay_options_require_canonical_language(self):
        saved = self.save_schedule()
        pool = self.add_pool()
        with self.assertRaises(service.XPostError) as missing:
            self.store.create_schedule_plan(
                "material",
                "2026-08-17",
                "09:00",
                saved["version"],
                [self.candidate(pool)],
                premium_relay_accounts=[
                    {"id": 10, "username": "premium10"}
                ],
            )
        self.assertEqual(
            missing.exception.code, "x_account_drama_language_invalid"
        )

    def test_sql_relay_triggers_allow_only_valid_schedule_material_relay(self):
        plan, _pool, _candidate = self.create_plan()
        valid_queue = plan["queues"][0]
        extra_pool = self.add_pool("202")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            original_run = dict(
                conn.execute(
                    "SELECT * FROM x_post_schedule_run WHERE id=?",
                    (plan["id"],),
                ).fetchone()
            )
            run_values = dict(original_run)
            run_values.pop("id")
            run_values.update(
                {
                    "slot_key": "xpost:schedule:v1:material:2026-08-17:1000",
                    "publish_time": "10:00",
                    "account_ids_json": "[99]",
                    "expected_count": 1,
                    "queued_count": 1,
                }
            )
            run_columns = list(run_values)
            cursor = conn.execute(
                "INSERT INTO x_post_schedule_run(%s) VALUES(%s)"
                % (
                    ",".join(run_columns),
                    ",".join("?" for _ in run_columns),
                ),
                tuple(run_values[column] for column in run_columns),
            )
            raw_schedule_run_id = int(cursor.lastrowid)

            template = dict(
                conn.execute(
                    "SELECT * FROM x_post_queue WHERE id=?",
                    (valid_queue["id"],),
                ).fetchone()
            )
            template.pop("id")
            template.update(
                {
                    "idempotency_key": "raw-material-relay-99",
                    "schedule_run_id": raw_schedule_run_id,
                    "account_id": 99,
                    "account_username": "target99",
                    "page_name": "Target 99",
                    "page_id": "9099",
                    "pool_item_id": extra_pool["id"],
                    "pool_created_at": extra_pool["created_at"],
                    "material_id": extra_pool["material_id"],
                    "material_key": extra_pool["material_key"],
                    "content_id": "C%s" % extra_pool["material_id"],
                    "candidate_rank": 1,
                    "delivery_mode": "premium_relay_repost",
                    "relay_account_id": 10,
                    "relay_account_username": "premium10",
                    "preflight_duration": 180.0,
                    "status": "queued",
                }
            )
            queue_columns = list(template)
            insert_sql = "INSERT INTO x_post_queue(%s) VALUES(%s)" % (
                ",".join(queue_columns),
                ",".join("?" for _ in queue_columns),
            )

            def raw_insert(overrides):
                values = dict(template)
                values.update(overrides)
                conn.execute(
                    insert_sql,
                    tuple(values[column] for column in queue_columns),
                )

            conn.execute("SAVEPOINT valid_relay")
            raw_insert({})
            conn.execute("ROLLBACK TO valid_relay")
            conn.execute("RELEASE valid_relay")

            invalid_inserts = (
                {"schedule_run_id": None},
                {"preflight_duration": 140.0},
                {"relay_account_id": 99},
                {"delivery_mode": "direct"},
            )
            for index, overrides in enumerate(invalid_inserts, 1):
                with self.subTest(insert=overrides):
                    conn.execute("SAVEPOINT invalid_insert_%s" % index)
                    with self.assertRaises(sqlite3.IntegrityError) as rejected:
                        raw_insert(overrides)
                    self.assertIn("relay binding invalid", str(rejected.exception))
                    conn.execute("ROLLBACK TO invalid_insert_%s" % index)
                    conn.execute("RELEASE invalid_insert_%s" % index)

            invalid_updates = (
                "schedule_run_id=NULL",
                "preflight_duration=140.0",
                "relay_account_id=account_id",
                "delivery_mode='direct'",
            )
            for index, assignment in enumerate(invalid_updates, 1):
                with self.subTest(update=assignment):
                    conn.execute("SAVEPOINT invalid_update_%s" % index)
                    with self.assertRaises(sqlite3.IntegrityError) as rejected:
                        conn.execute(
                            "UPDATE x_post_queue SET %s WHERE id=?" % assignment,
                            (valid_queue["id"],),
                        )
                    self.assertIn("relay binding invalid", str(rejected.exception))
                    conn.execute("ROLLBACK TO invalid_update_%s" % index)
                    conn.execute("RELEASE invalid_update_%s" % index)
            conn.commit()
            self.assertEqual(
                conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class MaterialRelayOAuthBoundaryTests(unittest.TestCase):
    def test_material_plan_preserves_requested_current_same_language_relay(self):
        captured = {}

        class FakeStore:
            def __init__(self, _path):
                pass

            def create_schedule_plan(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                candidate = args[4][0]
                return {
                    "id": 7,
                    "source_type": "material",
                    "run_date": "2026-08-17",
                    "publish_time": "09:00",
                    "config_version": 2,
                    "account_ids": [2],
                    "status": "queued",
                    "created": True,
                    "queues": [
                        {
                            "id": 9,
                            "account_id": 2,
                            "candidate_rank": 1,
                            "status": "queued",
                            "unknown_outcome": False,
                            "delivery_mode": candidate["delivery_mode"],
                            "relay_account_id": candidate["relay_account_id"],
                            "repost_status": "reserved",
                        }
                    ],
                }

        target = {
            **account(2),
            "publish_eligible": True,
            "long_video_publish_eligible": False,
        }
        relays = [
            {
                **account(10, premium=True, username="premium10"),
                "publish_eligible": True,
                "protected": False,
            },
            {
                **account(11, premium=True, username="premium11"),
                "publish_eligible": True,
                "protected": False,
            },
        ]
        payload = {
            "source_type": "material",
            "run_date": "2026-08-17",
            "publish_time": "09:00",
            "version": 2,
            "account_ids": [2],
            "candidates": [
                {
                    "material_language": "en",
                    "preflight_duration": 180.0,
                    "relay_account_id": 11,
                }
            ],
        }
        with mock.patch.object(
            oauth_service, "find_account", return_value=target
        ), mock.patch.object(
            oauth_service,
            "_premium_relay_accounts",
            return_value=relays,
        ), mock.patch.object(
            oauth_service, "preflight_post_storage_request"
        ), mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=(service.XPostError, FakeStore, None),
        ):
            result = oauth_service.create_post_schedule_plan_request(payload)
        self.assertEqual(result["queues"][0]["relay_account_id"], 11)
        trusted = captured["args"][4][0]
        self.assertEqual(trusted["account_id"], 2)
        self.assertEqual(trusted["relay_account_id"], 11)
        self.assertEqual(trusted["delivery_mode"], "premium_relay_repost")

    def test_material_plan_rejects_unrequested_fallback_relay(self):
        target = {
            **account(2),
            "publish_eligible": True,
            "long_video_publish_eligible": False,
        }
        payload = {
            "source_type": "material",
            "run_date": "2026-08-17",
            "publish_time": "09:00",
            "version": 2,
            "account_ids": [2],
            "candidates": [
                {
                    "material_language": "en",
                    "preflight_duration": 180.0,
                    "relay_account_id": 99,
                }
            ],
        }
        with mock.patch.object(
            oauth_service, "find_account", return_value=target
        ), mock.patch.object(
            oauth_service,
            "_premium_relay_accounts",
            return_value=[
                {
                    **account(10, premium=True, username="premium10"),
                    "publish_eligible": True,
                    "protected": False,
                }
            ],
        ):
            with self.assertRaises(oauth_service.ServiceError) as rejected:
                oauth_service.create_post_schedule_plan_request(payload)
        self.assertEqual(
            rejected.exception.code, "x_post_premium_relay_unavailable"
        )


if __name__ == "__main__":
    unittest.main()

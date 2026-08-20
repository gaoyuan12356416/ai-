#!/usr/bin/env python3
"""Offline integration tests for X multi-time schedules and drama progress."""

import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service


def compliance():
    return {
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
        "dangerous_tag_count": 0,
    }


def base_candidate(account_id, username, material_id, content_id):
    return {
        "account_id": account_id,
        "account_username": username,
        "source_date": "2026-07-26",
        "material_id": str(material_id),
        "content_id": str(content_id),
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "Episode %s" % material_id,
        "material_language": "en",
        "drama_name": "Drama One",
        "tag": "Romance",
        "description": "A complete short-drama episode description.",
        "page_name": "Drama Account",
        "page_id": "900%s" % account_id,
        "preflight_sha256": ("%064x" % int(account_id + int(str(material_id).lstrip("R"))))[-64:],
        "preflight_size": 1024,
        **compliance(),
    }


class XPostMultiScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "x-post.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def save_schedule(
        self,
        source_type,
        accounts,
        times,
        version=1,
        body_template=None,
    ):
        settings = {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "account_ids": accounts,
            "publish_times": times,
            "version": version,
        }
        if body_template is not None:
            settings["body_template"] = body_template
        return self.store.save_schedule_config(
            source_type,
            settings,
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )

    def save_random_schedule(
        self,
        source_type,
        accounts,
        daily_count,
        version=1,
        body_template=None,
        now=None,
    ):
        settings = {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "account_ids": accounts,
            "publish_times": [],
            "schedule_mode": "random",
            "random_daily_count": daily_count,
            "version": version,
        }
        if body_template is not None:
            settings["body_template"] = body_template
        return self.store.save_schedule_config(
            source_type,
            settings,
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=now
            or datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )

    def add_drama(self, content_id="D1", free_episode_count=2, labels=""):
        name_tag = "#Drama_One"
        if labels:
            name_tag += " #Romance"
        result = self.store.add_drama_pool_items(
            [content_id],
            [
                {
                    "content_id": content_id,
                    "drama_name": "Drama One",
                    "description": "A complete short-drama episode description.",
                    "language": "en",
                    "labels": labels,
                    "name_tag": name_tag,
                    "free_episode_count": free_episode_count,
                }
            ],
            actor={"user_id": "admin-1", "name": "Admin"},
        )
        return result["items"][0]

    def drama_candidate(self, pool, account_id, episode_number):
        replay_generation = int(pool.get("replay_generation", 1))
        episode_key = "%s:%s" % (pool["content_id"], episode_number)
        if replay_generation > 1:
            episode_key = "%s:replay%s:%s" % (
                pool["content_id"],
                replay_generation,
                episode_number,
            )
        item = base_candidate(
            account_id,
            "DramaAccount%s" % account_id,
            "R%s" % episode_number,
            pool["content_id"],
        )
        item.update(
            {
                "source_type": "drama",
                "drama_pool_item_id": pool["id"],
                "drama_pool_created_at": pool["created_at"],
                "episode_number": episode_number,
                "episode_key": episode_key,
                "drama_replay_generation": replay_generation,
                "name_tag": "#Drama_One",
            }
        )
        return item

    def material_candidate(self, pool, account_id):
        item = base_candidate(
            account_id,
            "MaterialAccount%s" % account_id,
            pool["material_id"],
            "C%s" % pool["material_id"],
        )
        item.update(
            {
                "source_type": "material",
                "pool_item_id": pool["id"],
                "pool_created_at": pool["created_at"],
            }
        )
        return item

    def publish_queue(self, queue, episode_number, post_id=None):
        log = self.store.reserve_log(queue["id"])
        text = service.build_drama_episode_post_text(
            "https://gy.g2flow.com/s2l/%s.html" % log["id"],
            episode_number,
            "Drama One",
            "A complete short-drama episode description.",
        )
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?x=1",
            "https://gy.g2flow.com/s2l/%s.html" % log["id"],
            text,
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media%s" % episode_number)
        post_id = post_id or "post%s" % episode_number
        return self.store.mark_published(
            log["id"],
            "media%s" % episode_number,
            post_id,
            "https://x.com/DramaAccount/status/%s" % post_id,
        )

    def test_schedule_config_is_versioned_due_and_cross_source_collision_safe(self):
        material_template = "{{drama_name}}\n{{desc}}\n{{url}}"
        material = self.save_schedule(
            "material",
            [2, 3],
            ["09:00", "12:30"],
            body_template=material_template,
        )
        self.assertEqual(material["version"], 2)
        self.assertEqual(material["posts_per_day"], 4)
        self.assertEqual(material["body_template"], material_template)
        self.assertIn("url", material["supported_macros"])

        due = self.store.due_schedule_slots(
            datetime(2026, 7, 27, 9, 0, tzinfo=service.BEIJING_TZ)
        )
        self.assertEqual(len(due["items"]), 1)
        self.assertEqual(due["items"][0]["account_ids"], [2, 3])
        self.assertEqual(due["items"][0]["version"], 2)
        self.assertEqual(due["items"][0]["body_template"], material_template)

        with self.assertRaises(service.XPostError) as collision:
            self.save_schedule("drama", [2], ["09:00"])
        self.assertEqual(collision.exception.code, "x_post_schedule_collision")

        drama = self.save_schedule("drama", [4], ["09:00"])
        self.assertEqual(drama["version"], 2)
        with self.assertRaises(service.XPostError) as stale:
            self.save_schedule("drama", [4], ["10:00"], version=1)
        self.assertEqual(
            stale.exception.code,
            "x_post_schedule_version_conflict",
        )

    def test_random_schedule_is_persisted_stable_spaced_and_next_day_only(self):
        template = "{{url}}\n🎬 {{drama_name}}\n{{desc}}"
        saved = self.save_random_schedule(
            "material",
            [2, 3],
            6,
            body_template=template,
        )
        self.assertEqual(saved["schedule_mode"], "random")
        self.assertEqual(saved["random_daily_count"], 6)
        self.assertEqual(saved["random_effective_date"], "2026-07-28")
        self.assertEqual(saved["publish_times"], [])
        self.assertEqual(saved["posts_per_day"], 12)
        self.assertTrue(saved["body_template"].startswith("{{url}}"))
        self.assertEqual(
            self.store.due_schedule_slots(
                datetime(2026, 7, 27, 12, 0, tzinfo=service.BEIJING_TZ)
            )["items"],
            [],
        )
        self.assertEqual(len(saved["random_daily_plans"]), 1)
        plan = saved["random_daily_plans"][0]
        self.assertEqual(plan["run_date"], "2026-07-28")
        self.assertEqual(plan["config_version"], 2)
        self.assertEqual(plan["account_ids"], [2, 3])
        self.assertEqual(plan["body_template"], template)
        self.assertEqual(len(plan["publish_times"]), 6)
        minute_values = [
            int(value[:2]) * 60 + int(value[3:])
            for value in plan["publish_times"]
        ]
        self.assertTrue(all(value % 60 for value in minute_values))
        self.assertTrue(
            all(
                right - left >= 60
                for left, right in zip(minute_values, minute_values[1:])
            )
        )
        reopened = service.XPostStore(self.db_path).get_schedule_config(
            "material",
            now=datetime(
                2026,
                7,
                27,
                9,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        self.assertEqual(
            reopened["random_daily_plans"][0]["publish_times"],
            plan["publish_times"],
        )

    def test_random_generator_supports_full_daily_limit_without_whole_hours(self):
        previous = []
        for _attempt in range(50):
            publish_times = service._generate_random_publish_times(
                service.MAX_RANDOM_DAILY_COUNT,
                previous_times=previous,
            )
            minute_values = [
                int(value[:2]) * 60 + int(value[3:])
                for value in publish_times
            ]
            self.assertEqual(
                len(publish_times),
                service.MAX_RANDOM_DAILY_COUNT,
            )
            self.assertEqual(len(set(publish_times)), len(publish_times))
            self.assertNotEqual(publish_times, previous)
            self.assertTrue(all(value % 60 for value in minute_values))
            self.assertTrue(
                all(
                    right - left
                    >= service.RANDOM_PUBLISH_MIN_GAP_MINUTES
                    for left, right in zip(
                        minute_values,
                        minute_values[1:],
                    )
                )
            )
            previous = publish_times

    def test_random_due_freezes_url_template_and_config_snapshot(self):
        template = "{{url}}\n🎬 {{drama_name}}\n{{desc}}"
        saved = self.save_random_schedule(
            "material",
            [2],
            2,
            body_template=template,
        )
        plan = saved["random_daily_plans"][0]
        first_time = plan["publish_times"][0]
        hour, minute = (int(part) for part in first_time.split(":"))
        due = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                28,
                hour,
                minute,
                10,
                tzinfo=service.BEIJING_TZ,
            )
        )
        self.assertEqual(len(due["items"]), 1)
        self.assertEqual(due["items"][0]["schedule_mode"], "random")
        self.assertEqual(due["items"][0]["version"], 2)
        self.assertEqual(due["items"][0]["body_template"], template)

        updated = self.save_random_schedule(
            "material",
            [3],
            3,
            version=2,
            body_template="🎬 {{drama_name}}\n{{desc}}\n{{url}}",
            now=datetime(
                2026,
                7,
                28,
                12,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        self.assertEqual(updated["random_effective_date"], "2026-07-29")
        frozen = self.store.query_schedule_plan(
            "material",
            "2026-07-28",
            first_time,
        )["run"]
        self.assertEqual(frozen["account_ids"], [2])
        self.assertEqual(frozen["body_template"], template)
        short_url = service._build_short_url(
            service.DEFAULT_SHORT_BASE_URL,
            9876,
        )
        rendered = service.build_post_text(
            short_url,
            "Demo Drama",
            "A frozen random-schedule description.",
            body_template=frozen["body_template"],
        )
        self.assertEqual(
            short_url,
            "https://gy.g2flow.com/s2l/9876.html",
        )
        self.assertEqual(rendered.count(short_url), 1)
        self.assertNotIn("{{url}}", rendered)

    def test_random_plans_avoid_shared_account_cross_pool_collision(self):
        material = self.save_random_schedule("material", [2], 8)
        drama = self.save_random_schedule("drama", [2], 8)
        material_times = set(
            material["random_daily_plans"][0]["publish_times"]
        )
        drama_times = set(
            drama["random_daily_plans"][0]["publish_times"]
        )
        self.assertFalse(material_times.intersection(drama_times))

    def test_random_schedule_rejects_mixed_times_and_invalid_count(self):
        for count in (0, 25):
            with self.subTest(count=count):
                with self.assertRaises(service.XPostError) as caught:
                    self.save_random_schedule("material", [2], count)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_random_daily_count",
                )
        with self.assertRaises(service.XPostError) as mixed:
            self.store.save_schedule_config(
                "material",
                {
                    "enabled": True,
                    "timezone": "Asia/Shanghai",
                    "account_ids": [2],
                    "publish_times": ["09:00"],
                    "schedule_mode": "random",
                    "random_daily_count": 2,
                    "version": 1,
                },
                actor={"user_id": "admin-1", "name": "Admin"},
                eligible_account_ids=[2],
                now=datetime(
                    2026,
                    7,
                    27,
                    8,
                    0,
                    tzinfo=service.BEIJING_TZ,
                ),
            )
        self.assertEqual(
            mixed.exception.code,
            "x_post_random_times_must_be_empty",
        )

    def test_schedule_change_is_rejected_during_the_current_slot_window(self):
        self.save_schedule("material", [2], ["10:00"])

        with self.assertRaises(service.XPostError) as rejected:
            self.store.save_schedule_config(
                "material",
                {
                    "enabled": True,
                    "timezone": "Asia/Shanghai",
                    "account_ids": [3],
                    "publish_times": ["10:00"],
                    "version": 2,
                },
                actor={"user_id": "admin-1", "name": "Admin"},
                eligible_account_ids=[2, 3],
                now=datetime(
                    2026,
                    7,
                    27,
                    10,
                    1,
                    0,
                    tzinfo=service.BEIJING_TZ,
                ),
            )

        self.assertEqual(
            rejected.exception.code,
            "x_post_schedule_slot_in_progress",
        )

    def test_schedule_template_validation_is_fail_closed(self):
        cases = (
            ("material", "{{drama_name}} {{desc}} {{episode_number}}"),
            ("material", "{{drama_name}} {{desc}} {{URL}}"),
            ("drama", "{{drama_name}} {{desc}}"),
        )
        for source_type, body_template in cases:
            with self.subTest(source_type=source_type, body_template=body_template):
                with self.assertRaises(service.XPostError) as caught:
                    self.save_schedule(
                        source_type,
                        [2],
                        ["09:00"],
                        body_template=body_template,
                    )
                self.assertEqual(caught.exception.code, "invalid_post_template")
                self.assertEqual(
                    self.store.get_schedule_config(source_type)["version"],
                    1,
                )

    def test_due_schedule_honors_ninety_second_grace_without_replaying_older_slots(self):
        self.save_schedule("material", [2], ["10:00"])
        within_grace = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                10,
                1,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(within_grace["items"]), 1)
        self.assertEqual(
            within_grace["items"][0]["publish_time"],
            "10:00",
        )
        self.assertIs(within_grace["items"][0]["frozen"], True)
        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "10:00",
            2,
            [2],
            "test_terminal",
            "finish the claimed test slot",
        )
        updated = self.store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2],
                "publish_times": ["11:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2],
            now=datetime(
                2026,
                7,
                27,
                12,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        self.assertEqual(updated["version"], 3)
        outside_grace = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                11,
                1,
                31,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(outside_grace["items"], [])

    def test_claimed_slot_keeps_its_frozen_accounts_after_config_change(self):
        self.save_schedule("material", [2], ["10:00"])
        claimed = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                10,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(claimed["items"][0]["account_ids"], [2])
        self.store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [3],
                "publish_times": ["11:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=datetime(
                2026,
                7,
                27,
                12,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        pool = self.store.add_pool_materials(
            ["105"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "105", "error_code": ""},
            ],
        )["items"][0]
        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "10:00",
            2,
            [self.material_candidate(pool, 2)],
        )
        self.assertEqual(plan["account_ids"], [2])
        self.assertEqual(
            [item["account_id"] for item in plan["queues"]],
            [2],
        )

    def test_deferred_media_is_schedule_only_and_preflight_mode_stays_strict(self):
        self.save_schedule("material", [2], ["10:00", "11:00"])
        oldest, newest = self.store.add_pool_materials(
            ["105", "106"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "105", "error_code": ""},
                {"material_id": "106", "error_code": ""},
            ],
        )["items"]
        deferred = self.material_candidate(newest, 2)
        deferred.update(
            {
                "media_validation_mode": "deferred",
                "preflight_sha256": "",
                "preflight_size": 0,
            }
        )
        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "10:00",
            2,
            [deferred],
        )
        self.assertEqual(plan["queues"][0]["media_validation_mode"], "deferred")
        self.assertEqual(plan["queues"][0]["preflight_sha256"], "")
        self.assertEqual(plan["queues"][0]["preflight_size"], 0)

        with self.assertRaises(service.XPostError) as one_off:
            self.store.enqueue(deferred)
        self.assertEqual(one_off.exception.code, "invalid_request")

        malformed_deferred = dict(deferred)
        malformed_deferred["preflight_sha256"] = "a" * 64
        malformed_deferred["preflight_size"] = 100
        with self.assertRaises(service.XPostError) as fingerprinted:
            self.store._queue_payload(
                malformed_deferred,
                require_compliance=True,
                allow_deferred_media=True,
            )
        self.assertEqual(fingerprinted.exception.code, "invalid_request")

        strict = self.material_candidate(oldest, 2)
        strict["preflight_sha256"] = ""
        strict["preflight_size"] = 0
        with self.assertRaises(service.XPostError) as missing_preflight:
            self.store.create_schedule_plan(
                "material",
                "2026-07-27",
                "11:00",
                2,
                [strict],
            )
        self.assertEqual(missing_preflight.exception.code, "invalid_request")

    def test_nonterminal_frozen_accounts_remain_in_internal_scope(self):
        self.save_schedule("material", [2], ["10:00"])
        self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                10,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [3],
                "publish_times": ["11:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=datetime(
                2026,
                7,
                27,
                12,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        self.assertEqual(
            self.store.scheduled_account_ids(
                enabled_only=True,
                include_nonterminal_runs=True,
            ),
            [3, 2],
        )
        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "10:00",
            2,
            [2],
            "test_terminal",
            "finish the frozen slot",
        )
        self.assertEqual(
            self.store.scheduled_account_ids(
                enabled_only=True,
                include_nonterminal_runs=True,
            ),
            [3],
        )

    def test_previous_day_claim_is_stopped_instead_of_auto_published(self):
        self.save_schedule("material", [2], ["10:00"])
        claimed = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                26,
                10,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(claimed["items"]), 1)

        next_day = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                9,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )

        self.assertEqual(next_day["items"], [])
        frozen = self.store.query_schedule_plan(
            "material",
            "2026-07-26",
            "10:00",
        )
        self.assertEqual(frozen["run"]["status"], "stopped")
        self.assertEqual(
            frozen["run"]["error_code"],
            "x_post_schedule_stale_claim",
        )

    def test_previous_day_stale_claim_recovery_is_exact_and_audited(self):
        self.save_schedule("material", [2], ["10:00"])
        self._add_recovery_account(2)
        claimed = self.store.due_schedule_slots(
            datetime(2026, 7, 26, 10, 0, 10, tzinfo=service.BEIJING_TZ),
            grace_seconds=90,
        )["items"][0]
        run_id = self.store.query_schedule_plan(
            "material", "2026-07-26", "10:00"
        )["run"]["id"]
        self.store.due_schedule_slots(
            datetime(2026, 7, 27, 9, 0, 10, tzinfo=service.BEIJING_TZ),
            grace_seconds=90,
        )
        now = datetime(2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ)
        validated = self.store.recover_previous_day_stale_claim_schedule_run(
            run_id,
            actor="codex_operator",
            deployed_commit="a" * 40,
            validate_only=True,
            now=now,
        )
        self.assertEqual(validated["updated_count"], 0)
        self.assertEqual(validated["validated_queue_count"], 0)
        self.assertEqual(
            self.store.get_schedule_run(run_id)["status"],
            "stopped",
        )

        recovered = self.store.recover_previous_day_stale_claim_schedule_run(
            run_id,
            actor="codex_operator",
            deployed_commit="a" * 40,
            now=now,
        )
        self.assertEqual(recovered["next_status"], "claimed")
        restored = self.store.get_schedule_run(run_id)
        self.assertEqual(restored["status"], "claimed")
        self.assertEqual(restored["error_code"], "")
        recovered_due = self.store.previous_day_recovered_schedule_slots(
            "2026-07-26",
            "a" * 40,
            now=now,
        )
        self.assertEqual(len(recovered_due["items"]), 1)
        self.assertEqual(recovered_due["items"][0]["run_date"], "2026-07-26")
        self.assertEqual(
            self.store.previous_day_recovered_schedule_slots(
                "2026-07-26",
                "b" * 40,
                now=now,
            )["items"],
            [],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT recovery_reason,actor,previous_status,"
                "previous_error_code,validated_queue_count,validated_log_count "
                "FROM x_post_schedule_previous_day_recovery_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.PREVIOUS_DAY_STALE_CLAIM_RECOVERY_REASON,
                "codex_operator",
                "stopped",
                "x_post_schedule_stale_claim",
                0,
                0,
            ),
        )

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE x_post_schedule_run SET status='stopped',"
                "error_code='x_post_schedule_stale_claim',"
                "error_message='claim timer raced guarded recovery' WHERE id=?",
                (run_id,),
            )
            conn.commit()
        resumed = self.store.recover_previous_day_stale_claim_schedule_run(
            run_id,
            actor="codex_operator",
            deployed_commit="a" * 40,
            now=now,
        )
        self.assertEqual(resumed["recovery_mode"], "claim_race_resume")
        self.assertEqual(resumed["next_status"], "claimed")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            resume_audit = conn.execute(
                "SELECT recovery_audit_id,recovery_reason,actor,"
                "validated_queue_count FROM "
                "x_post_schedule_previous_day_resume_audit "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(
            resume_audit[1:],
            (
                service.PREVIOUS_DAY_STALE_CLAIM_RECOVERY_REASON,
                "codex_operator",
                0,
            ),
        )

        with self.assertRaises(service.XPostError) as duplicate:
            self.store.recover_previous_day_stale_claim_schedule_run(
                run_id,
                actor="codex_operator",
                deployed_commit="a" * 40,
                now=now,
            )
        self.assertEqual(
            duplicate.exception.code,
            "x_post_previous_day_recovery_conflict",
        )

    def test_previous_day_partial_plan_can_be_recovered_without_scope_loss(self):
        self.save_schedule("material", [2, 3], ["10:00"])
        self._add_recovery_account(2)
        self._add_recovery_account(3)
        self.store.due_schedule_slots(
            datetime(2026, 7, 26, 10, 0, 10, tzinfo=service.BEIJING_TZ),
            grace_seconds=90,
        )
        pool = self.store.add_pool_materials(
            ["261"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "261", "error_code": ""}
            ],
        )["items"][0]
        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-26",
            "10:00",
            2,
            [self.material_candidate(pool, 2)],
        )
        run_id = plan["id"]
        self.store.due_schedule_slots(
            datetime(2026, 7, 27, 9, 0, 10, tzinfo=service.BEIJING_TZ),
            grace_seconds=90,
        )
        now = datetime(2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ)

        recovered = self.store.recover_previous_day_stale_claim_schedule_run(
            run_id,
            actor="codex_operator",
            deployed_commit="c" * 40,
            now=now,
        )
        recovered_due = self.store.previous_day_recovered_schedule_slots(
            "2026-07-26",
            "c" * 40,
            now=now,
        )

        self.assertEqual(recovered["expected_count"], 1)
        self.assertEqual(recovered["next_status"], "running")
        self.assertEqual(recovered_due["items"][0]["account_ids"], [2, 3])

    def test_previous_day_slot_remains_due_during_midnight_grace(self):
        self.save_schedule("material", [2], ["23:59"])
        claimed = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                26,
                23,
                59,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(claimed["items"]), 1)

        midnight = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                0,
                0,
                30,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(midnight["items"]), 1)
        self.assertEqual(midnight["items"][0]["run_date"], "2026-07-26")
        self.assertEqual(midnight["items"][0]["publish_time"], "23:59")
        within_grace = self.store.query_schedule_plan(
            "material",
            "2026-07-26",
            "23:59",
        )
        self.assertEqual(within_grace["run"]["status"], "claimed")

        expired = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                0,
                0,
                31,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(expired["items"], [])
        stopped = self.store.query_schedule_plan(
            "material",
            "2026-07-26",
            "23:59",
        )
        self.assertEqual(stopped["run"]["status"], "stopped")

    def test_stale_drama_schedule_marks_pool_needs_review(self):
        self.save_schedule("drama", [2], ["10:00"])
        pool = self.add_drama()
        claimed = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                26,
                10,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(claimed["items"]), 1)
        self.store.create_schedule_plan(
            "drama",
            "2026-07-26",
            "10:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )

        next_day = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                9,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )

        self.assertEqual(next_day["items"], [])
        frozen = self.store.query_schedule_plan(
            "drama",
            "2026-07-26",
            "10:00",
        )
        self.assertEqual(frozen["run"]["status"], "stopped")
        blocked = self.store.query_drama_pool()["items"][0]
        self.assertEqual(blocked["status"], "needs_review")
        self.assertEqual(
            blocked["last_error_code"],
            "x_post_schedule_stale_claim",
        )
        with self.assertRaises(service.XPostError) as unavailable:
            self.store.available_drama_pool_items()
        self.assertEqual(
            unavailable.exception.code,
            "x_post_drama_pool_needs_review",
        )

    def test_unassigned_schedule_failure_does_not_classify_the_drama(self):
        self.save_schedule("drama", [2], ["10:00"])
        pool = self.add_drama()
        claimed = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                10,
                0,
                10,
                tzinfo=service.BEIJING_TZ,
            ),
            grace_seconds=90,
        )
        self.assertEqual(len(claimed["items"]), 1)

        with self.assertRaises(service.XPostError) as mismatched:
            self.store.record_schedule_failure(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [2],
                "media_preflight_failed",
                "episode media is invalid",
                drama_pool_item_id=pool["id"],
                content_id="DIFFERENT",
            )
        self.assertEqual(
            mismatched.exception.code,
            "x_post_drama_pool_item_unavailable",
        )
        unchanged = self.store.query_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
        )
        self.assertEqual(unchanged["run"]["status"], "claimed")

        failure = self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [2],
            "media_preflight_failed",
            "episode media is invalid",
            drama_pool_item_id=pool["id"],
            content_id=pool["content_id"],
        )
        self.assertEqual(failure["status"], "failed_preflight")
        self.assertTrue(failure["recorded"])
        unchanged_pool = self.store.query_drama_pool()["items"][0]
        self.assertEqual(unchanged_pool["status"], "pending")
        self.assertEqual(unchanged_pool["last_error_code"], "")
        self.assertEqual(unchanged_pool["last_error_message"], "")
        with self.assertRaises(service.XPostError) as replay:
            self.store.create_schedule_plan(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [self.drama_candidate(pool, 2, 1)],
            )
        self.assertEqual(
            replay.exception.code,
            "x_post_schedule_run_exists",
        )
        frozen_failure = self.store.query_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
        )
        self.assertEqual(frozen_failure["run"]["status"], "failed_preflight")
        self.assertEqual(
            frozen_failure["run"]["error_code"],
            "media_preflight_failed",
        )
        self.assertEqual(frozen_failure["queues"], [])

    def test_drama_pool_check_skips_unbound_failure_but_refuses_bound_drama(self):
        rejected_pool = self.add_drama(content_id="REJECTED")
        next_pool = self.add_drama(content_id="NEXT")
        with self.assertRaises(service.XPostError) as transient:
            self.store.record_drama_pool_checks(
                [
                    {
                        "pool_item_id": rejected_pool["id"],
                        "content_id": rejected_pool["content_id"],
                        "error_code": "media_download_failed",
                        "error_message": "temporary COS timeout",
                    }
                ]
            )
        self.assertEqual(transient.exception.code, "invalid_request")
        transient_pool = self.store.query_drama_pool(
            {"content_id": rejected_pool["content_id"]}
        )["items"][0]
        self.assertEqual(transient_pool["status"], "pending")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE x_post_drama_pool SET status='needs_review',"
                "last_error_code='legacy_preflight_failure' WHERE id=?",
                (rejected_pool["id"],),
            )
            conn.commit()

        checked = self.store.record_drama_pool_checks(
            [
                {
                    "pool_item_id": rejected_pool["id"],
                    "content_id": rejected_pool["content_id"],
                    "error_code": "source_not_repairable",
                    "error_message": "source duration is outside the X contract",
                }
            ]
        )

        self.assertEqual(checked["updated_count"], 1)
        available = self.store.available_drama_pool_items(
            limit=10,
            account_ids=[2],
        )
        self.assertEqual(
            [item["id"] for item in available],
            [next_pool["id"]],
        )
        rejected = self.store.query_drama_pool(
            {"content_id": rejected_pool["content_id"]}
        )["items"][0]
        self.assertEqual(rejected["status"], "validation_failed")

        self.save_schedule("drama", [2], ["10:00"])
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [self.drama_candidate(next_pool, 2, 1)],
        )
        self.assertEqual(plan["queues"][0]["account_id"], 2)
        unrelated_pool = self.add_drama(content_id="UNRELATED")
        with self.assertRaises(service.XPostError) as unrelated:
            self.store.record_schedule_failure(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [2],
                "media_preflight_failed",
                "stale candidate failure",
                drama_pool_item_id=unrelated_pool["id"],
                content_id=unrelated_pool["content_id"],
            )
        self.assertEqual(
            unrelated.exception.code,
            "x_post_schedule_failure_scope_mismatch",
        )
        unrelated_row = self.store.query_drama_pool(
            {"content_id": unrelated_pool["content_id"]}
        )["items"][0]
        self.assertEqual(unrelated_row["status"], "pending")
        with self.assertRaises(service.XPostError) as bound:
            self.store.record_drama_pool_checks(
                [
                    {
                        "pool_item_id": next_pool["id"],
                        "content_id": next_pool["content_id"],
                        "error_code": "source_not_repairable",
                        "error_message": "bound episode failed",
                    }
                ]
            )
        self.assertEqual(
            bound.exception.code,
            "x_post_drama_pool_item_bound",
        )
        failure = self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [2],
            "media_preflight_failed",
            "bound episode failed",
            drama_pool_item_id=next_pool["id"],
            content_id=next_pool["content_id"],
        )
        self.assertFalse(failure["recorded"])
        bound_pool = self.store.query_drama_pool(
            {"content_id": next_pool["content_id"]}
        )["items"][0]
        self.assertEqual(bound_pool["status"], "needs_review")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_assignment_immutable"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_x_post_drama_pool_assignment_evidence"
            )
            conn.execute(
                "UPDATE x_post_drama_pool SET assigned_account_id=0,"
                "assigned_at='',assigned_source_queue_id=NULL,status='active' "
                "WHERE id=?",
                (next_pool["id"],),
            )
            conn.commit()
        with self.assertRaises(service.XPostError) as historical:
            self.store.record_drama_pool_checks(
                [
                    {
                        "pool_item_id": next_pool["id"],
                        "content_id": next_pool["content_id"],
                        "error_code": "source_not_repairable",
                        "error_message": "legacy unbound row still has history",
                    }
                ]
            )
        self.assertEqual(
            historical.exception.code,
            "x_post_drama_pool_item_bound",
        )
        historical_pool = self.store.query_drama_pool(
            {"content_id": next_pool["content_id"]}
        )["items"][0]
        self.assertEqual(historical_pool["status"], "active")

        success_check = {
            "pool_item_id": rejected_pool["id"],
            "content_id": rejected_pool["content_id"],
            "error_code": "",
            "error_message": "",
            "expected_error_code": "source_not_repairable",
            "expected_episode_number": 1,
        }
        guarded = self.store.record_drama_pool_checks(
            [success_check],
            validate_only=True,
        )
        self.assertEqual(guarded["updated_count"], 0)
        self.assertEqual(guarded["validated_count"], 1)
        self.assertTrue(guarded["validate_only"])
        still_failed = self.store.query_drama_pool(
            {"content_id": rejected_pool["content_id"]}
        )["items"][0]
        self.assertEqual(still_failed["status"], "validation_failed")

        restored = self.store.record_drama_pool_checks([success_check])
        self.assertEqual(restored["updated_count"], 1)
        self.assertEqual(restored["validated_count"], 1)
        recovered = self.store.query_drama_pool(
            {"content_id": rejected_pool["content_id"]}
        )["items"][0]
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["last_error_code"], "")
        self.assertEqual(recovered["last_error_message"], "")

        with self.assertRaises(service.XPostError) as repeated:
            self.store.record_drama_pool_checks([success_check])
        self.assertEqual(
            repeated.exception.code,
            "x_post_drama_pool_revalidation_conflict",
        )

    def test_drama_pool_accepts_large_batch_and_returns_compact_items(self):
        drama_ids = ["D%03d" % index for index in range(100)]
        validation_checks = [
            {
                "content_id": content_id,
                "drama_name": "Drama %s" % content_id,
                "description": "剧" * 10000,
                "language": "en",
                "labels": "",
                "name_tag": "#Drama_%s" % content_id,
                "free_episode_count": 1,
            }
            for content_id in drama_ids
        ]
        request_size = len(
            json.dumps(
                {
                    "drama_ids": drama_ids,
                    "validation_checks": validation_checks,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
        self.assertGreater(request_size, 16 * 1024)
        self.assertLess(request_size, 5 * 1024 * 1024)

        result = self.store.add_drama_pool_items(
            drama_ids,
            validation_checks,
            actor={"user_id": "admin-1", "name": "Admin"},
        )
        self.assertEqual(result["created_count"], 100)
        self.assertEqual(len(result["items"]), 100)
        self.assertNotIn("description", result["items"][0])
        response_size = len(
            json.dumps(result, ensure_ascii=False).encode("utf-8")
        )
        self.assertLess(response_size, 2 * 1024 * 1024)

    def test_drama_post_template_matches_the_requested_copy(self):
        rendered = service.build_drama_episode_post_text(
            "https://gy.g2flow.com/s2l/1.html",
            2,
            "Drama One",
            "A complete drama description.",
        )
        self.assertEqual(
            rendered,
            "🎬 Drama One\n"
            "Episode 2\n"
            "A complete drama description.\n\n"
            "#shortdrama #shortfilms #tvdrama #aidrama #dramawave",
        )

    def test_one_account_can_run_multiple_material_points_without_reuse(self):
        template = "{{drama_name}}\n{{desc}}\n{{url}}"
        self.save_schedule(
            "material",
            [2],
            ["09:00", "10:00"],
            body_template=template,
        )
        added = self.store.add_pool_materials(
            ["101", "102"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "101", "error_code": ""},
                {"material_id": "102", "error_code": ""},
            ],
        )
        oldest_pool, newest_pool = added["items"]

        first = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [self.material_candidate(newest_pool, 2)],
        )
        second = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "10:00",
            2,
            [self.material_candidate(oldest_pool, 2)],
        )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["queues"][0]["account_id"], 2)
        self.assertEqual(second["queues"][0]["account_id"], 2)
        self.assertEqual(first["queues"][0]["body_template"], template)
        self.assertEqual(second["queues"][0]["body_template"], template)
        self.assertNotEqual(
            first["queues"][0]["material_key"],
            second["queues"][0]["material_key"],
        )

    def test_material_schedule_cannot_skip_the_newest_available_pool_item(self):
        self.save_schedule("material", [2], ["09:00"])
        added = self.store.add_pool_materials(
            ["201", "202"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "201", "error_code": ""},
                {"material_id": "202", "error_code": ""},
            ],
        )

        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "material",
                "2026-07-27",
                "09:00",
                2,
                [self.material_candidate(added["items"][0], 2)],
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_pool_fifo_conflict",
        )

    def test_material_schedule_atomically_clears_selected_revalidatable_error(self):
        self.save_schedule("material", [2], ["09:00"])
        pool = self.store.add_pool_materials(
            ["211"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {
                    "material_id": "211",
                    "error_code": "material_not_found_or_ineligible",
                    "error_message": "historical selector could not hydrate it",
                }
            ],
        )["items"][0]

        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [self.material_candidate(pool, 2)],
        )

        self.assertTrue(plan["created"])
        self.assertEqual(len(plan["queues"]), 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            revalidated = conn.execute(
                "SELECT last_checked_at,last_error_code,last_error_message "
                "FROM x_post_material_pool WHERE id=?",
                (pool["id"],),
            ).fetchone()
        self.assertTrue(revalidated["last_checked_at"])
        self.assertEqual(revalidated["last_error_code"], "")
        self.assertEqual(revalidated["last_error_message"], "")

    def test_media_only_historical_errors_are_rechecked_but_unsafe_is_not(self):
        self.save_schedule("material", [2], ["09:00", "10:00", "11:00"])
        for index, (publish_time, error_code) in enumerate(
            zip(
                ("09:00", "10:00", "11:00"),
                (
                    "repaired_media_invalid",
                    "x_post_media_repair_unreachable",
                    "cos_upload_failed",
                ),
            ),
            1,
        ):
            with self.subTest(error_code=error_code):
                material_id = str(300 + index)
                pool = self.store.add_pool_materials(
                    [material_id],
                    actor={"user_id": "admin-1", "name": "Admin"},
                    validation_checks=[
                        {
                            "material_id": material_id,
                            "error_code": error_code,
                            "error_message": "historical media-only failure",
                        }
                    ],
                )["items"][0]
                self.store.create_schedule_plan(
                    "material",
                    "2026-07-27",
                    publish_time,
                    2,
                    [self.material_candidate(pool, 2)],
                )
                with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                    cleared = conn.execute(
                        "SELECT last_error_code FROM x_post_material_pool WHERE id=?",
                        (pool["id"],),
                    ).fetchone()[0]
                self.assertEqual(cleared, "")
        self.assertNotIn(
            "material_source_tag_unsafe",
            service.REVALIDATABLE_MATERIAL_VALIDATION_CODES,
        )

    def test_material_schedule_cannot_skip_unrevalidated_historical_error(self):
        self.save_schedule("material", [2], ["09:00"])
        oldest, newest = self.store.add_pool_materials(
            ["212", "213"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "212", "error_code": ""},
                {
                    "material_id": "213",
                    "error_code": "material_not_video",
                    "error_message": "historical selector classified it as an image",
                },
            ],
        )["items"]

        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "material",
                "2026-07-27",
                "09:00",
                2,
                [self.material_candidate(oldest, 2)],
            )

        self.assertEqual(rejected.exception.code, "x_post_pool_fifo_conflict")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT last_error_code FROM x_post_material_pool WHERE id=?",
                (newest["id"],),
            ).fetchone()
            queue_count = conn.execute(
                "SELECT COUNT(*) FROM x_post_queue"
            ).fetchone()[0]
        self.assertEqual(row[0], "material_not_video")
        self.assertEqual(queue_count, 0)

    def test_material_schedule_allows_latest_set_to_follow_account_capability_order(self):
        self.save_schedule("material", [2, 3], ["09:00"])
        added = self.store.add_pool_materials(
            ["221", "222"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "221", "error_code": ""},
                {"material_id": "222", "error_code": ""},
            ],
        )
        oldest_pool, newest_pool = added["items"]

        created = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [
                self.material_candidate(oldest_pool, 2),
                self.material_candidate(newest_pool, 3),
            ],
        )

        self.assertTrue(created["created"])
        self.assertEqual(
            [queue["account_id"] for queue in created["queues"]],
            [2, 3],
        )
        self.assertEqual(
            [queue["pool_item_id"] for queue in created["queues"]],
            [oldest_pool["id"], newest_pool["id"]],
        )
        self.assertEqual(
            {queue["pool_item_id"] for queue in created["queues"]},
            {oldest_pool["id"], newest_pool["id"]},
        )

    def test_material_schedule_skips_current_long_row_after_premium_target_used(self):
        self.save_schedule("material", [2, 3, 4], ["09:00"])
        added = self.store.add_pool_materials(
            ["231", "232", "233", "234", "235"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "231", "error_code": ""},
                {"material_id": "232", "error_code": ""},
                {"material_id": "233", "error_code": ""},
                {
                    "material_id": "234",
                    "error_code": "x_long_video_requires_premium",
                    "error_message": "Premium capacity was already consumed",
                },
                {"material_id": "235", "error_code": ""},
            ],
        )["items"]
        oldest, second_oldest, middle, _skipped_long, newest = added
        premium = self.material_candidate(newest, 2)
        premium["preflight_duration"] = 200.0
        standard_one = self.material_candidate(middle, 3)
        standard_one["preflight_duration"] = 100.0
        standard_two = self.material_candidate(second_oldest, 4)
        standard_two["preflight_duration"] = 100.0

        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [premium, standard_one, standard_two],
            premium_account_ids=[2],
        )
        self.assertEqual(len(plan["queues"]), 3)
        self.assertNotEqual(oldest["id"], second_oldest["id"])

    def test_material_schedule_skips_currently_unpublishable_long_fifo_row(self):
        self.save_schedule("material", [2], ["09:00"])
        oldest, newest = self.store.add_pool_materials(
            ["241", "242"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "241", "error_code": ""},
                {
                    "material_id": "242",
                    "error_code": "x_long_video_requires_premium",
                    "error_message": "A Premium account is still available",
                },
            ],
        )["items"]
        candidate = self.material_candidate(oldest, 2)
        candidate["preflight_duration"] = 200.0

        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [candidate],
            premium_account_ids=[2],
        )
        self.assertEqual(plan["queues"][0]["pool_item_id"], oldest["id"])
        self.assertNotEqual(oldest["id"], newest["id"])

    def test_material_available_subset_creates_partial_audited_run(self):
        self.save_schedule("material", [2, 3], ["09:00"])
        pool = self.store.add_pool_materials(
            ["251"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[{"material_id": "251", "error_code": ""}],
        )["items"][0]

        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [self.material_candidate(pool, 2)],
        )
        frozen = self.store.query_schedule_plan(
            "material", "2026-07-27", "09:00"
        )
        due = self.store.due_schedule_slots(
            datetime(
                2026,
                7,
                27,
                9,
                0,
                30,
                tzinfo=service.BEIJING_TZ,
            )
        )

        self.assertEqual(len(plan["queues"]), 1)
        self.assertEqual(frozen["run"]["account_ids"], [2, 3])
        self.assertEqual(frozen["run"]["expected_count"], 1)
        self.assertEqual(frozen["run"]["error_code"], "")
        self.assertIn("本次已为1个", frozen["run"]["error_message"])
        self.assertEqual(due["items"][0]["account_ids"], [2, 3])

    def test_material_fifo_replay_rejects_stale_skip_evidence(self):
        self.assertFalse(
            service._material_fifo_selection_matches(
                [
                    {
                        "id": 2,
                        "last_error_code": "material_source_tag_unsafe",
                        "last_checked_at": "2026-07-27T00:00:00Z",
                    },
                    {
                        "id": 1,
                        "last_error_code": "",
                        "last_checked_at": "",
                    },
                ],
                [
                    {
                        "pool_item_id": 1,
                        "account_id": 2,
                        "preflight_duration": 100.0,
                    }
                ],
                [2],
                [],
                validation_cutoff="2026-07-27T01:00:00Z",
            )
        )

    def test_material_schedule_accepts_newest_violation_audit_record(self):
        self.save_schedule("material", [2], ["09:00"])
        pool = self.store.add_pool_materials(
            ["211"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {
                    "material_id": "211",
                    "error_code": "material_has_violation",
                    "error_message": "historical violation evidence",
                },
            ],
        )["items"][0]
        candidate = self.material_candidate(pool, 2)
        candidate["facebook_violation_count"] = 2

        created = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [candidate],
        )

        self.assertTrue(created["created"])
        self.assertEqual(created["queues"][0]["material_id"], "211")
        self.assertEqual(
            created["queues"][0]["facebook_violation_count"],
            2,
        )

    def test_drama_plan_keeps_each_unfinished_drama_on_one_account(self):
        self.save_schedule("drama", [2, 3], ["09:00", "10:00"])
        second_pool = self.add_drama(
            content_id="D2",
            free_episode_count=2,
            labels="",
        )
        first_pool = self.add_drama(free_episode_count=2, labels="")
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        self.assertEqual(
            [
                (item["account_id"], item["episode_key"])
                for item in first_plan["queues"]
            ],
            [(2, "D1:1"), (3, "D2:1")],
        )

        for queue_item in first_plan["queues"]:
            self.publish_queue(queue_item, 1)
        active = self.store.query_drama_pool()["items"]
        self.assertEqual(
            [
                (
                    item["content_id"],
                    item["assigned_account_id"],
                    item["next_sub_num"],
                )
                for item in active
            ],
            [("D1", 2, 2), ("D2", 3, 2)],
        )

        second_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 2),
                self.drama_candidate(second_pool, 3, 2),
            ],
        )
        self.assertEqual(
            [
                (item["account_id"], item["episode_key"])
                for item in second_plan["queues"]
            ],
            [(2, "D1:2"), (3, "D2:2")],
        )
        for queue_item in second_plan["queues"]:
            self.publish_queue(queue_item, 2)
        completed = self.store.query_drama_pool()["items"]
        self.assertTrue(
            all(item["status"] == "completed" for item in completed)
        )
        self.assertTrue(
            all(item["next_sub_num"] == 3 for item in completed)
        )
        self.assertTrue(
            all(item["published_episode_count"] == 2 for item in completed)
        )
        self.assertEqual(
            self.store.query_schedule_plan(
                "drama", "2026-07-27", "10:00"
            )["run"]["status"],
            "completed",
        )

    def test_reordering_accounts_keeps_existing_drama_bindings(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=2)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        for queue_item in first_plan["queues"]:
            self.publish_queue(queue_item, 1)
        updated = self.store.save_schedule_config(
            "drama",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [3, 2],
                "publish_times": ["10:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )

        second_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            updated["version"],
            [
                self.drama_candidate(second_pool, 3, 2),
                self.drama_candidate(first_pool, 2, 2),
            ],
        )
        self.assertEqual(
            [
                (item["account_id"], item["episode_key"])
                for item in second_plan["queues"]
            ],
            [(3, "D2:2"), (2, "D1:2")],
        )

    def test_full_replay_preserves_history_and_starts_a_new_generation(self):
        self.save_schedule("drama", [2], ["09:00", "10:00"])
        pool = self.add_drama(content_id="D1", free_episode_count=2)
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        first_queue = first_plan["queues"][0]
        self.publish_queue(first_queue, 1, post_id="original-post")
        current = self.store.query_drama_pool()["items"][0]
        snapshot = {
            "pool_item_id": current["id"],
            "content_id": current["content_id"],
            "status": current["status"],
            "replay_generation": current["replay_generation"],
            "free_episode_count": current["free_episode_count"],
            "published_episode_count": current[
                "published_episode_count"
            ],
            "next_sub_number": current["next_sub_number"],
            "assigned_account_id": current["assigned_account_id"],
        }
        actor = {"user_id": "admin-1", "name": "Admin"}

        dry_run = self.store.reset_drama_pool_for_replay(
            [pool["id"]],
            actor=actor,
            reason=service.DRAMA_REPLAY_REASON,
            expected_snapshots=[snapshot],
        )
        self.assertTrue(dry_run["validate_only"])
        self.assertEqual(dry_run["reset_count"], 0)
        unchanged = self.store.query_drama_pool()["items"][0]
        self.assertEqual(unchanged["replay_generation"], 1)
        self.assertEqual(unchanged["published_episode_count"], 1)

        applied = self.store.reset_drama_pool_for_replay(
            [pool["id"]],
            actor=actor,
            reason=service.DRAMA_REPLAY_REASON,
            expected_snapshots=[snapshot],
            validate_only=False,
        )
        self.assertEqual(applied["reset_count"], 1)
        replay_pool = self.store.query_drama_pool()["items"][0]
        self.assertEqual(replay_pool["replay_generation"], 2)
        self.assertEqual(replay_pool["status"], "pending")
        self.assertEqual(replay_pool["published_episode_count"], 0)
        self.assertEqual(replay_pool["next_sub_number"], 1)
        self.assertEqual(replay_pool["assigned_account_id"], 0)
        self.assertEqual(replay_pool["queue_count"], 1)

        replay_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [self.drama_candidate(replay_pool, 2, 1)],
        )
        replay_queue = replay_plan["queues"][0]
        self.assertEqual(replay_queue["drama_replay_generation"], 2)
        self.assertEqual(replay_queue["episode_key"], "D1:replay2:1")
        self.publish_queue(replay_queue, 1, post_id="replay-post")

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            queues = conn.execute(
                "SELECT id,episode_key,drama_replay_generation,status "
                "FROM x_post_queue ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [
                    (
                        row["episode_key"],
                        row["drama_replay_generation"],
                        row["status"],
                    )
                    for row in queues
                ],
                [
                    ("D1:1", 1, "published"),
                    ("D1:replay2:1", 2, "published"),
                ],
            )
            audit = conn.execute(
                "SELECT * FROM x_post_drama_replay_audit"
            ).fetchone()
            self.assertEqual(audit["from_generation"], 1)
            self.assertEqual(audit["to_generation"], 2)
            self.assertEqual(audit["from_published_episode_count"], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_drama_replay_audit SET actor_name='Other' "
                    "WHERE id=?",
                    (audit["id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_drama_pool SET replay_generation=3 "
                    "WHERE id=?",
                    (pool["id"],),
                )

    def test_new_account_receives_newest_unassigned_drama(self):
        self.save_schedule("drama", [2], ["09:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=2)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(first_pool, 2, 1)],
        )
        self.publish_queue(first_plan["queues"][0], 1)
        newest_pool = self.add_drama(content_id="D3", free_episode_count=2)
        updated = self.store.save_schedule_config(
            "drama",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2, 3],
                "publish_times": ["10:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[2, 3, 4],
            now=datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )

        second_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            updated["version"],
            [
                self.drama_candidate(first_pool, 2, 2),
                self.drama_candidate(newest_pool, 3, 1),
            ],
        )
        self.assertEqual(
            [
                (item["account_id"], item["content_id"])
                for item in second_plan["queues"]
            ],
            [(2, "D1"), (3, "D3")],
        )

    def test_duplicate_drama_pool_row_cannot_fill_two_accounts(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        pool = self.add_drama(content_id="ONLY", free_episode_count=2)
        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "drama",
                "2026-07-27",
                "09:00",
                2,
                [
                    self.drama_candidate(pool, 2, 1),
                    self.drama_candidate(pool, 3, 2),
                ],
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_assignment_conflict",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue"
                ).fetchone()[0],
                0,
            )

    def test_drama_available_subset_creates_and_completes_partial_run(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        pool = self.add_drama(content_id="ONLY", free_episode_count=2)

        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        frozen = self.store.query_schedule_plan(
            "drama", "2026-07-27", "09:00"
        )

        self.assertEqual(len(plan["queues"]), 1)
        self.assertEqual(frozen["run"]["account_ids"], [2, 3])
        self.assertEqual(frozen["run"]["expected_count"], 1)
        self.assertEqual(frozen["run"]["error_code"], "")
        self.assertIn("本次已为1个", frozen["run"]["error_message"])

        self.publish_queue(plan["queues"][0], 1)
        completed = self.store.query_schedule_plan(
            "drama", "2026-07-27", "09:00"
        )
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertEqual(completed["run"]["published_count"], 1)
        self.assertEqual(completed["run"]["error_code"], "")
        self.assertIn("本次已为1个", completed["run"]["error_message"])

    def test_drama_assignment_does_not_hide_later_bound_account_after_gap(self):
        self.save_schedule("drama", [2, 3], ["09:00", "10:00"])
        long_pool = self.add_drama(content_id="LONG", free_episode_count=2)
        short_pool = self.add_drama(content_id="SHORT", free_episode_count=1)
        first = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(short_pool, 2, 1),
                self.drama_candidate(long_pool, 3, 1),
            ],
        )
        for item in first["queues"]:
            self.publish_queue(item, 1, "first-%s" % item["account_id"])

        available = self.store.available_drama_pool_items(
            limit=1000,
            account_ids=[2, 3],
        )

        self.assertEqual(
            [(item["candidate_account_id"], item["content_id"]) for item in available],
            [(3, "LONG")],
        )

    def test_drama_assignment_rejects_cross_account_continuation(self):
        self.save_schedule("drama", [2, 3], ["09:00", "10:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=2)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        for queue_item in first_plan["queues"]:
            self.publish_queue(queue_item, 1)

        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [
                    self.drama_candidate(second_pool, 2, 2),
                    self.drama_candidate(first_pool, 3, 2),
                ],
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_assignment_conflict",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_queue SET account_id=3 WHERE id=?",
                    (first_plan["queues"][0]["id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_queue SET source_type='material' "
                    "WHERE id=?",
                    (first_plan["queues"][0]["id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_drama_pool SET assigned_at=? "
                    "WHERE id=?",
                    (
                        "2026-07-28T00:00:00Z",
                        first_pool["id"],
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM x_post_queue WHERE id=?",
                    (first_plan["queues"][0]["id"],),
                )

    def test_unassigned_drama_latest_set_allows_account_permutation(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        older_pool = self.add_drama(content_id="LONG", free_episode_count=2)
        newer_pool = self.add_drama(content_id="SHORT", free_episode_count=2)

        available = self.store.available_drama_pool_items(
            limit=2,
            account_ids=[2, 3],
        )
        self.assertEqual(
            [(item["candidate_account_id"], item["id"]) for item in available],
            [(2, newer_pool["id"]), (3, older_pool["id"])],
        )

        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(older_pool, 2, 1),
                self.drama_candidate(newer_pool, 3, 1),
            ],
        )

        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in plan["queues"]],
            [(2, "LONG"), (3, "SHORT")],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            assigned = conn.execute(
                "SELECT content_id,assigned_account_id "
                "FROM x_post_drama_pool WHERE id IN (?,?) "
                "ORDER BY content_id",
                (older_pool["id"], newer_pool["id"]),
            ).fetchall()
        self.assertEqual(assigned, [("LONG", 2), ("SHORT", 3)])

    def test_retryable_long_drama_keeps_target_and_uses_relay_source(self):
        saved = self.save_schedule("drama", [2, 3], ["09:00"])
        older_short = self.add_drama(content_id="OLDER-SHORT")
        newer_short = self.add_drama(content_id="NEWER-SHORT")
        newest_long = self.add_drama(content_id="NEWEST-LONG")
        recorded = self.store.record_drama_pool_checks(
            [
                {
                    "pool_item_id": newest_long["id"],
                    "content_id": newest_long["content_id"],
                    "error_code": "x_long_video_requires_premium",
                    "error_message": (
                        "Videos longer than 140 seconds require a "
                        "token-confirmed X Premium subscription"
                    ),
                }
            ]
        )
        self.assertEqual(recorded["updated_count"], 1)

        available = self.store.available_drama_pool_items(
            limit=1000,
            account_ids=[2, 3],
            premium_account_ids=[],
        )
        self.assertEqual(
            [(item["candidate_account_id"], item["id"]) for item in available],
            [(2, newest_long["id"]), (3, newer_short["id"])],
        )
        long_candidate = self.drama_candidate(newest_long, 2, 1)
        long_candidate.update(
            {
                "preflight_duration": 180.0,
                "delivery_mode": "premium_relay_repost",
                "relay_account_id": 9,
                "relay_account_username": "premium9",
            }
        )
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            saved["version"],
            [
                long_candidate,
                self.drama_candidate(newer_short, 3, 1),
            ],
            premium_account_ids=[],
            premium_relay_accounts=[
                {"id": 9, "username": "premium9"}
            ],
        )
        self.assertEqual(
            [(item["account_id"], item["content_id"]) for item in plan["queues"]],
            [(2, "NEWEST-LONG"), (3, "NEWER-SHORT")],
        )
        self.assertEqual(plan["queues"][0]["relay_account_id"], 9)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            long_row = conn.execute(
                "SELECT status,assigned_account_id,last_error_code "
                "FROM x_post_drama_pool WHERE id=?",
                (newest_long["id"],),
            ).fetchone()
        self.assertEqual(long_row, ("active", 2, ""))

    def test_legacy_frozen_cross_account_queue_is_blocked_before_publish(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=2)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        foreign_queue_id = plan["queues"][1]["id"]
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DROP TRIGGER trg_x_post_queue_drama_update"
            )
            conn.execute(
                "UPDATE x_post_queue SET drama_pool_item_id=?,"
                "content_id=?,episode_number=2,episode_key=? WHERE id=?",
                (
                    first_pool["id"],
                    first_pool["content_id"],
                    "D1:2",
                    foreign_queue_id,
                ),
            )
            conn.commit()

        with self.assertRaises(service.XPostError) as rejected:
            self.store.reserve_log(foreign_queue_id)
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_account_binding_conflict",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_publish_log "
                    "WHERE queue_id=?",
                    (foreign_queue_id,),
                ).fetchone()[0],
                0,
            )

    def test_storage_migration_uses_earliest_confirmed_account_as_owner(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=1)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        self.publish_queue(plan["queues"][0], 1)
        self.publish_queue(plan["queues"][1], 1)
        first_queue_id = plan["queues"][0]["id"]
        second_queue_id = plan["queues"][1]["id"]

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DROP TRIGGER trg_x_post_queue_drama_update"
            )
            conn.execute(
                "DROP TRIGGER "
                "trg_x_post_drama_pool_assignment_immutable"
            )
            conn.execute(
                "DROP TRIGGER "
                "trg_x_post_drama_pool_assignment_evidence"
            )
            conn.execute(
                "UPDATE x_post_queue SET drama_pool_item_id=?,"
                "content_id=?,episode_number=2,episode_key=? WHERE id=?",
                (
                    first_pool["id"],
                    first_pool["content_id"],
                    "D1:2",
                    second_queue_id,
                ),
            )
            conn.execute(
                "UPDATE x_post_drama_pool SET assigned_account_id=0,"
                "assigned_at='',assigned_source_queue_id=NULL"
            )
            conn.commit()

        migrated = service.XPostStore(self.db_path)
        pools = {
            item["content_id"]: item
            for item in migrated.query_drama_pool()["items"]
        }
        self.assertEqual(pools["D1"]["assigned_account_id"], 2)
        self.assertEqual(
            pools["D1"]["assigned_source_queue_id"],
            first_queue_id,
        )
        self.assertEqual(pools["D1"]["assigned_account_username"], "DramaAccount2")
        self.assertEqual(pools["D2"]["assigned_account_id"], 0)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_publish_log"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT account_id FROM x_post_queue WHERE id=?",
                    (second_queue_id,),
                ).fetchone()[0],
                3,
            )

        reopened = service.XPostStore(self.db_path)
        reopened_pools = {
            item["content_id"]: item
            for item in reopened.query_drama_pool()["items"]
        }
        self.assertEqual(reopened_pools["D1"]["assigned_account_id"], 2)
        self.assertEqual(
            reopened_pools["D1"]["assigned_source_queue_id"],
            first_queue_id,
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_publish_log"
                ).fetchone()[0],
                2,
            )

    def test_storage_migration_still_rejects_invalid_drama_episode_identity(self):
        self.save_schedule("drama", [2], ["09:00"])
        pool = self.add_drama(content_id="D1", free_episode_count=2)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DROP TRIGGER trg_x_post_queue_drama_update"
            )
            conn.execute(
                "UPDATE x_post_queue SET episode_key='D1:999' WHERE id=?",
                (plan["queues"][0]["id"],),
            )
            conn.commit()

        with self.assertRaises(service.XPostError) as rejected:
            service.XPostStore(self.db_path)
        self.assertEqual(
            rejected.exception.code,
            "x_post_storage_conflict",
        )

    def test_completed_drama_releases_account_to_newest_unassigned_drama(self):
        self.save_schedule("drama", [2], ["09:00", "10:00"])
        completed_pool = self.add_drama(
            content_id="DONE",
            free_episode_count=1,
        )
        first_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(completed_pool, 2, 1)],
        )
        self.publish_queue(first_plan["queues"][0], 1)
        self.add_drama(content_id="OLDER", free_episode_count=1)
        next_pool = self.add_drama(content_id="NEXT", free_episode_count=1)

        second_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [self.drama_candidate(next_pool, 2, 1)],
        )
        self.assertEqual(
            second_plan["queues"][0]["content_id"],
            "NEXT",
        )
        pools = {
            item["content_id"]: item
            for item in self.store.query_drama_pool()["items"]
        }
        self.assertEqual(pools["DONE"]["status"], "completed")
        self.assertEqual(pools["DONE"]["assigned_account_id"], 2)
        self.assertEqual(pools["NEXT"]["assigned_account_id"], 2)

    def test_enabled_schedule_cannot_remove_unfinished_drama_owner(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        second_pool = self.add_drama(content_id="D2", free_episode_count=2)
        first_pool = self.add_drama(content_id="D1", free_episode_count=2)
        self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        with self.assertRaises(service.XPostError) as rejected:
            self.store.save_schedule_config(
                "drama",
                {
                    "enabled": True,
                    "timezone": "Asia/Shanghai",
                    "account_ids": [3],
                    "publish_times": ["10:00"],
                    "version": 2,
                },
                actor={"user_id": "admin-1", "name": "Admin"},
                eligible_account_ids=[2, 3, 4],
                now=datetime(
                    2026,
                    7,
                    27,
                    8,
                    0,
                    tzinfo=service.BEIJING_TZ,
                ),
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_owner_not_configured",
        )

    def test_disabling_schedule_preserves_unfinished_drama_owner(self):
        self.save_schedule("drama", [2], ["09:00"])
        pool = self.add_drama(content_id="D1", free_episode_count=2)
        self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )

        disabled = self.store.save_schedule_config(
            "drama",
            {
                "enabled": False,
                "timezone": "Asia/Shanghai",
                "account_ids": [2],
                "publish_times": ["09:00"],
                "version": 2,
            },
            actor={"user_id": "admin-1", "name": "Admin"},
            eligible_account_ids=[],
            now=datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )

        self.assertFalse(disabled["enabled"])
        row = self.store.query_drama_pool()["items"][0]
        self.assertEqual(row["assigned_account_id"], 2)
        self.assertTrue(row["assigned_at"])

    def test_known_drama_failure_is_local_and_other_account_can_continue(self):
        self.save_schedule("drama", [2, 3], ["09:00", "10:00"])
        later_pool = self.add_drama(
            content_id="D2",
            free_episode_count=1,
        )
        pool = self.add_drama(free_episode_count=2)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        log = self.store.reserve_log(plan["queues"][0]["id"])
        self.store.mark_failed_if_reserved(
            log["id"],
            "media_preflight_failed",
            "known media failure",
        )
        pools = {
            item["content_id"]: item
            for item in self.store.query_drama_pool()["items"]
        }
        blocked = pools[pool["content_id"]]
        self.assertEqual(blocked["status"], "active")
        self.assertEqual(blocked["last_error_code"], "media_preflight_failed")
        self.assertEqual(blocked["assigned_account_id"], 2)
        available = self.store.available_drama_pool_items(
            account_ids=[2, 3],
            account_languages={2: "en", 3: "en"},
        )
        self.assertEqual(
            [(item["content_id"], item["candidate_account_id"]) for item in available],
            [(later_pool["content_id"], 3)],
        )
        continued = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "10:00",
            2,
            [self.drama_candidate(later_pool, 3, 1)],
        )
        self.assertEqual(continued["queues"][0]["account_id"], 3)

    def test_unknown_drama_failure_still_blocks_every_later_plan(self):
        self.save_schedule("drama", [2, 3], ["09:00", "10:00"])
        later_pool = self.add_drama(content_id="D2", free_episode_count=1)
        pool = self.add_drama(content_id="D1", free_episode_count=2)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        log = self.store.reserve_log(plan["queues"][0]["id"])
        self.store.mark_failed(
            log["id"],
            "x_post_outcome_unknown",
            "result could not be confirmed",
            unknown_outcome=True,
        )
        blocked = {
            item["content_id"]: item
            for item in self.store.query_drama_pool()["items"]
        }[pool["content_id"]]
        self.assertEqual(blocked["status"], "needs_review")
        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [self.drama_candidate(later_pool, 3, 1)],
            )
        self.assertEqual(rejected.exception.code, "x_post_unknown_outcome")

    def test_drama_pool_batch_delete_is_atomic_and_returns_compact_items(self):
        first = self.add_drama(content_id="DELETE1")
        second = self.add_drama(content_id="DELETE2")

        result = self.store.delete_drama_pool_items(
            [second["id"], first["id"]]
        )

        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(
            result["items"],
            [
                {
                    "id": second["id"],
                    "content_id": "DELETE2",
                    "deleted": True,
                },
                {
                    "id": first["id"],
                    "content_id": "DELETE1",
                    "deleted": True,
                },
            ],
        )
        self.assertEqual(
            self.store.query_drama_pool()["pagination"]["total"],
            0,
        )

    def test_drama_pool_batch_delete_rolls_back_when_any_item_has_history(self):
        self.save_schedule("drama", [2], ["09:00"])
        deletable = self.add_drama(content_id="DELETABLE")
        occupied = self.add_drama(content_id="OCCUPIED")
        self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(occupied, 2, 1)],
        )

        with self.assertRaises(service.XPostError) as rejected:
            self.store.delete_drama_pool_items(
                [deletable["id"], occupied["id"]]
            )

        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_pool_item_occupied",
        )
        rows = self.store.query_drama_pool()["items"]
        self.assertEqual(
            {item["content_id"] for item in rows},
            {"OCCUPIED", "DELETABLE"},
        )
        occupied_row = next(
            item for item in rows if item["content_id"] == "OCCUPIED"
        )
        deletable_row = next(
            item for item in rows if item["content_id"] == "DELETABLE"
        )
        self.assertTrue(occupied_row["has_history"])
        self.assertFalse(occupied_row["deletable"])
        self.assertEqual(occupied_row["queue_count"], 1)
        self.assertFalse(deletable_row["has_history"])
        self.assertTrue(deletable_row["deletable"])

    def test_drama_pool_batch_delete_rejects_legacy_content_history(self):
        self.save_schedule("drama", [2], ["09:00"])
        occupied = self.add_drama(content_id="LEGACY")
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(occupied, 2, 1)],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS trg_x_post_queue_drama_update"
            )
            conn.execute(
                "UPDATE x_post_queue SET drama_pool_item_id=NULL WHERE id=?",
                (plan["queues"][0]["id"],),
            )
            conn.commit()

        with self.assertRaises(service.XPostError) as rejected:
            self.store.delete_drama_pool_items([occupied["id"]])

        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_pool_item_occupied",
        )
        row = self.store.query_drama_pool()["items"][0]
        self.assertEqual(row["queue_count"], 1)
        self.assertTrue(row["has_history"])
        self.assertFalse(row["deletable"])

    def test_drama_pool_batch_delete_validates_entire_request_before_delete(self):
        pool = self.add_drama(content_id="KEEP")
        invalid_requests = (
            [],
            [pool["id"], pool["id"]],
            [0],
            list(range(1, 102)),
        )
        for pool_item_ids in invalid_requests:
            with self.subTest(pool_item_ids=pool_item_ids[:3]):
                with self.assertRaises(service.XPostError) as rejected:
                    self.store.delete_drama_pool_items(pool_item_ids)
                self.assertEqual(rejected.exception.code, "invalid_request")
        with self.assertRaises(service.XPostError) as missing:
            self.store.delete_drama_pool_items([pool["id"], 999999])
        self.assertEqual(
            missing.exception.code,
            "x_post_drama_pool_item_not_found",
        )
        self.assertEqual(
            self.store.query_drama_pool()["pagination"]["total"],
            1,
        )

    def test_drama_pool_delete_allows_unoccupied_validation_failure_only(self):
        invalid = self.store.add_drama_pool_items(
            ["INVALID"],
            [
                {
                    "content_id": "INVALID",
                    "error_code": "drama_resource_invalid",
                    "error_message": "短剧资源数据不完整",
                }
            ],
            actor={"user_id": "admin-1", "name": "Admin"},
        )["items"][0]
        row = self.store.query_drama_pool()["items"][0]
        self.assertEqual(row["status"], "validation_failed")
        self.assertTrue(row["deletable"])
        deleted = self.store.delete_drama_pool_item(invalid["id"])
        self.assertEqual(deleted["id"], invalid["id"])
        self.assertEqual(deleted["content_id"], "INVALID")
        self.assertEqual(deleted["status"], "validation_failed")
        self.assertTrue(deleted["deleted"])

    def test_drama_pool_status_guard_rejects_unbound_active_record(self):
        pool = self.add_drama(content_id="ACTIVE")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE x_post_drama_pool SET status='active' WHERE id=?",
                (pool["id"],),
            )
            conn.commit()

        row = self.store.query_drama_pool()["items"][0]
        self.assertFalse(row["deletable"])
        self.assertFalse(row["has_history"])
        with self.assertRaises(service.XPostError) as rejected:
            self.store.delete_drama_pool_items([pool["id"]])
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_pool_item_occupied",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM x_post_drama_pool WHERE id=?",
                    (pool["id"],),
                )

    def test_known_drama_failure_allows_mixed_batch_to_complete_with_errors(self):
        self.save_schedule("drama", [2, 3], ["09:00"])
        second_pool = self.add_drama(
            content_id="SECOND",
            free_episode_count=2,
        )
        first_pool = self.add_drama(
            content_id="FIRST",
            free_episode_count=2,
        )
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        log = self.store.reserve_log(plan["queues"][0]["id"])
        self.store.mark_failed_if_reserved(
            log["id"],
            "x_upstream_error",
            "known X rejection",
        )

        frozen = self.store.query_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
        )
        self.assertEqual(frozen["run"]["status"], "queued")
        self.assertFalse(frozen["run"]["finished_at"])
        self.assertEqual(
            [item["status"] for item in frozen["queues"]],
            ["failed", "queued"],
        )
        self.assertEqual(
            [item["error_code"] for item in frozen["queues"]],
            ["x_upstream_error", ""],
        )
        self.publish_queue(plan["queues"][1], 1)
        completed = self.store.query_schedule_plan(
            "drama", "2026-07-27", "09:00"
        )
        self.assertEqual(completed["run"]["status"], "completed_with_errors")
        self.assertEqual(completed["run"]["failed_count"], 1)
        self.assertEqual(completed["run"]["published_count"], 1)
        self.assertTrue(completed["run"]["finished_at"])

    def test_resumed_failed_rate_limit_is_exposed_by_schedule_query(self):
        self.save_schedule("material", [2], ["09:00"])
        pool = self.store.add_pool_materials(
            ["401"],
            actor={"user_id": "admin-1", "name": "Admin"},
            validation_checks=[{"material_id": "401", "error_code": ""}],
        )["items"][0]
        plan = self.store.create_schedule_plan(
            "material",
            "2026-07-27",
            "09:00",
            2,
            [self.material_candidate(pool, 2)],
        )
        log = self.store.reserve_log(plan["queues"][0]["id"])
        self.store.mark_failed_if_reserved(
            log["id"], "x_post_rate_limited", "retry later"
        )
        frozen = self.store.query_schedule_plan(
            "material", "2026-07-27", "09:00"
        )
        self.assertEqual(frozen["run"]["status"], "stopped")
        self.assertEqual(frozen["queues"][0]["status"], "failed")
        self.assertEqual(
            frozen["queues"][0]["error_code"], "x_post_rate_limited"
        )

    def test_legacy_whole_batch_recovery_is_fenced_for_local_known_failure(self):
        self.save_schedule("drama", [2, 3], ["07:00", "09:00"])
        second_pool = self.add_drama(
            content_id="SECOND",
            free_episode_count=2,
        )
        first_pool = self.add_drama(
            content_id="FIRST",
            free_episode_count=2,
        )
        published_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "07:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 1),
                self.drama_candidate(second_pool, 3, 1),
            ],
        )
        self.publish_queue(published_plan["queues"][0], 1)
        self.publish_queue(published_plan["queues"][1], 1)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [
                self.drama_candidate(first_pool, 2, 2),
                self.drama_candidate(second_pool, 3, 2),
            ],
        )
        first_queue = plan["queues"][0]
        log = self.store.reserve_log(first_queue["id"])
        self.store.mark_failed_if_reserved(
            log["id"],
            "invalid_short_base_url",
            "short base URL is invalid",
        )

        with self.assertRaises(service.XPostError) as fenced:
            self.store.recover_pre_x_schedule_failure(
                first_queue["id"],
                "invalid_short_base_url",
                validate_only=True,
            )
        self.assertEqual(fenced.exception.code, "x_post_pre_x_recovery_conflict")
        frozen = self.store.query_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
        )
        self.assertEqual(frozen["run"]["status"], "queued")
        self.assertEqual(frozen["run"]["failed_count"], 1)
        self.assertEqual(
            [item["status"] for item in frozen["queues"]],
            ["failed", "queued"],
        )
        failed_log = self.store.get_log(log["id"])
        self.assertEqual(failed_log["status"], "failed")
        self.assertEqual(failed_log["error_code"], "invalid_short_base_url")
        failed_pool = self.store.query_drama_pool(
            {"drama_id": "FIRST"}
        )["items"][0]
        self.assertEqual(failed_pool["status"], "active")
        self.assertEqual(failed_pool["assigned_account_id"], 2)
        self.assertEqual(failed_pool["next_sub_number"], 2)
        self.assertEqual(failed_pool["last_error_code"], "invalid_short_base_url")

        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_pre_x_schedule_failure(
                first_queue["id"],
                "invalid_short_base_url",
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_pre_x_recovery_conflict",
        )

    def test_pre_x_recovery_rejects_started_or_unknown_failures(self):
        self.save_schedule("drama", [2], ["07:00", "09:00"])
        pool = self.add_drama(content_id="FIRST")
        published_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "07:00",
            2,
            [self.drama_candidate(pool, 2, 1)],
        )
        self.publish_queue(published_plan["queues"][0], 1)
        plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
            2,
            [self.drama_candidate(pool, 2, 2)],
        )
        queue = plan["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.mark_failed_if_reserved(
            log["id"],
            "invalid_short_base_url",
            "short base URL is invalid",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE x_post_publish_log SET attempt_count=1 "
                "WHERE id=?",
                (log["id"],),
            )
            conn.commit()
        with self.assertRaises(service.XPostError) as rejected:
            self.store.recover_pre_x_schedule_failure(
                queue["id"],
                "invalid_short_base_url",
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_pre_x_recovery_conflict",
        )
        with self.assertRaises(service.XPostError) as disallowed:
            self.store.recover_pre_x_schedule_failure(
                queue["id"],
                "x_upstream_error",
            )
        self.assertEqual(
            disallowed.exception.code,
            "x_post_pre_x_recovery_not_allowed",
        )

    def _add_recovery_account(self, account_id, status="active"):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS x_authorized_account("
                "id INTEGER PRIMARY KEY,status TEXT NOT NULL,"
                "publish_approved INTEGER NOT NULL,"
                "token_store_key TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO x_authorized_account("
                "id,status,publish_approved,token_store_key) "
                "VALUES(?,?,1,?)",
                (account_id, status, "account-%s.json" % account_id),
            )
            conn.commit()

    def _failed_schedule_run(
        self,
        error_code="x_token_missing",
        *,
        source_type="material",
        error_message="Account preflight failed before any queue or X write",
    ):
        saved = self.save_schedule(source_type, [2], ["09:00"])
        self._add_recovery_account(2)
        due = self.store.due_schedule_slots(
            datetime(2026, 7, 27, 9, 0, tzinfo=service.BEIJING_TZ)
        )["items"][0]
        failed = self.store.record_schedule_failure(
            source_type,
            "2026-07-27",
            "09:00",
            saved["version"],
            [2],
            error_code,
            error_message,
        )
        self.assertEqual(failed["status"], "failed_preflight")
        return failed

    def test_failed_preflight_recovery_is_same_day_zero_write_and_audited(self):
        failed = self._failed_schedule_run()
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(validated["validated_count"], 1)
        self.assertEqual(validated["updated_count"], 0)
        self.assertEqual(
            self.store.get_schedule_run(failed["id"])["status"],
            "failed_preflight",
        )

        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        restored = self.store.get_schedule_run(failed["id"])
        self.assertEqual(restored["status"], "claimed")
        self.assertEqual(restored["error_code"], "")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT recovery_reason,actor,previous_status,"
                "previous_error_code,validated_queue_count,"
                "validated_log_count FROM x_post_schedule_recovery_audit "
                "WHERE schedule_run_id=?",
                (failed["id"],),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_RECOVERY_REASON,
                "codex_operator",
                "failed_preflight",
                "x_token_missing",
                0,
                0,
            ),
        )
        due = self.store.due_schedule_slots(
            datetime(2026, 7, 27, 10, 0, tzinfo=service.BEIJING_TZ)
        )
        self.assertEqual(
            [item["slot_key"] for item in due["items"]],
            [restored["slot_key"]],
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_token_missing",
                reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_failed_preflight_recovery_accepts_proven_material_capacity_fix(self):
        failed = self._failed_schedule_run(
            "x_post_schedule_material_preflight_shortage",
            error_message=(
                "not enough FIFO material candidates passed media preflight"
            ),
        )
        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_schedule_material_preflight_shortage",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=datetime(
                2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
            ),
        )
        self.assertEqual(recovered["updated_count"], 1)
        self.assertEqual(
            self.store.get_schedule_run(failed["id"])["status"],
            "claimed",
        )

    def test_failed_preflight_recovery_accepts_proven_dimension_fix(self):
        failed = self._failed_schedule_run(
            "invalid_media_dimensions",
            source_type="drama",
            error_message=(
                "episode BOoD0GOWhX:1 media preflight failed: "
                "素材分辨率或宽高比不符合X"
            ),
        )
        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "invalid_media_dimensions",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=datetime(
                2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
            ),
        )
        self.assertEqual(recovered["updated_count"], 1)
        self.assertEqual(
            self.store.get_schedule_run(failed["id"])["status"],
            "claimed",
        )

    def test_failed_preflight_config_recovery_rejects_unproven_message(self):
        failed = self._failed_schedule_run(
            "x_post_schedule_material_preflight_shortage",
            error_message="unrelated candidate selection failure",
        )
        with self.assertRaises(service.XPostError) as rejected:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_schedule_material_preflight_shortage",
                reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                now=datetime(
                    2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
                ),
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_failed_preflight_recovery_accepts_exact_frozen_random_plan(self):
        saved = self.save_random_schedule("material", [2], 2)
        self._add_recovery_account(2)
        plan = saved["random_daily_plans"][0]
        first_time = plan["publish_times"][0]
        hour, minute = (int(part) for part in first_time.split(":"))
        run_now = datetime(
            2026,
            7,
            28,
            hour,
            minute,
            10,
            tzinfo=service.BEIJING_TZ,
        )
        due = self.store.due_schedule_slots(run_now)["items"][0]
        failed = self.store.record_schedule_failure(
            "material",
            "2026-07-28",
            first_time,
            due["version"],
            [2],
            "x_token_missing",
            "Token file was unavailable before queue creation",
        )

        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=run_now,
        )

        self.assertEqual(recovered["updated_count"], 1)
        restored = self.store.get_schedule_run(failed["id"])
        self.assertEqual(restored["schedule_mode"], "random")
        self.assertEqual(restored["status"], "claimed")
        self.assertEqual(restored["account_ids"], plan["account_ids"])
        self.assertEqual(restored["body_template"], plan["body_template"])

    def test_failed_preflight_corrective_retry_is_once_and_audited(self):
        failed = self._failed_schedule_run()
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_pool_invalid_response",
            "Material pool FIFO order is invalid",
        )

        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_pool_invalid_response",
            reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
            actor="codex_operator",
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(validated["recovery_mode"], "corrective")
        self.assertEqual(validated["updated_count"], 0)
        corrected = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_pool_invalid_response",
            reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.assertEqual(corrected["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT c.recovery_reason,c.previous_error_code,"
                "c.validated_queue_count,c.validated_log_count "
                "FROM x_post_schedule_corrective_retry_audit c "
                "JOIN x_post_schedule_recovery_audit i "
                "ON i.id=c.initial_recovery_audit_id "
                "WHERE c.schedule_run_id=? AND i.schedule_run_id=?",
                (failed["id"], failed["id"]),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
                "x_post_pool_invalid_response",
                0,
                0,
            ),
        )

        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_pool_invalid_response",
            "Material pool FIFO order is invalid",
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_pool_invalid_response",
                reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
                actor="codex_operator",
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_zero_write_material_preflight_can_defer_for_drama_slot(self):
        failed = self._failed_schedule_run()
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_schedule_operator_deferred_for_due_slot",
            "operator deferred zero-write material preflight to protect "
            "scheduled drama slot",
        )

        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_schedule_operator_deferred_for_due_slot",
            reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        self.assertEqual(
            self.store.get_schedule_run(failed["id"])["status"],
            "claimed",
        )

        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_schedule_operator_deferred_for_due_slot",
            "operator deferred zero-write material preflight to protect "
            "scheduled drama slot",
        )
        capacity = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_schedule_operator_deferred_for_due_slot",
            reason=service.FAILED_PREFLIGHT_CAPACITY_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.assertEqual(capacity["recovery_mode"], "capacity")
        self.assertEqual(capacity["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT recovery_reason,previous_error_code,"
                "validated_queue_count,validated_log_count "
                "FROM x_post_schedule_capacity_retry_audit "
                "WHERE schedule_run_id=?",
                (failed["id"],),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_CAPACITY_RECOVERY_REASON,
                "x_post_schedule_operator_deferred_for_due_slot",
                0,
                0,
            ),
        )

        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_schedule_material_preflight_shortage",
            "not enough FIFO material candidates passed media preflight",
        )
        post_capacity = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_schedule_material_preflight_shortage",
            reason=service.FAILED_PREFLIGHT_POST_CAPACITY_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit="a" * 40,
            now=recovery_now,
        )
        self.assertEqual(
            post_capacity["recovery_mode"],
            "post_capacity_transient",
        )
        self.assertEqual(post_capacity["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            post_audit = conn.execute(
                "SELECT recovery_reason,deployed_commit,"
                "previous_error_code,validated_queue_count,"
                "validated_log_count "
                "FROM x_post_schedule_post_capacity_retry_audit "
                "WHERE schedule_run_id=?",
                (failed["id"],),
            ).fetchone()
        self.assertEqual(
            post_audit,
            (
                service.FAILED_PREFLIGHT_POST_CAPACITY_RECOVERY_REASON,
                "a" * 40,
                "x_post_schedule_material_preflight_shortage",
                0,
                0,
            ),
        )

    def test_failed_preflight_corrective_retry_rejects_unproven_message(self):
        failed = self._failed_schedule_run()
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "material",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_schedule_preflight_failed",
            "unrelated validation failure",
        )
        with self.assertRaises(service.XPostError) as rejected:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_schedule_preflight_failed",
                reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
                actor="codex_operator",
                now=recovery_now,
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_verified_repair_retry_requires_two_audits_and_job_proof(self):
        failed = self._failed_schedule_run(source_type="drama")
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_token_missing",
            reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_pool_invalid_response",
            "Material pool FIFO order is invalid",
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_pool_invalid_response",
            reason=service.FAILED_PREFLIGHT_CORRECTIVE_RECOVERY_REASON,
            actor="codex_operator",
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_media_repair_invalid_response",
            "unassigned Premium drama routing failed: "
            "X media repair probe does not meet the X video contract",
        )
        job_key = "a" * 64

        with self.assertRaises(service.XPostError) as missing_job:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_media_repair_invalid_response",
                reason=(
                    service.FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON
                ),
                actor="codex_operator",
                now=recovery_now,
            )
        self.assertEqual(
            missing_job.exception.code,
            "x_post_failed_preflight_recovery_not_allowed",
        )

        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_media_repair_invalid_response",
            reason=service.FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON,
            actor="codex_operator",
            verified_repair_job_key=job_key,
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(validated["recovery_mode"], "verified_repair")
        self.assertEqual(validated["verified_repair_job_key"], job_key)
        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_media_repair_invalid_response",
            reason=service.FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON,
            actor="codex_operator",
            verified_repair_job_key=job_key,
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT v.recovery_reason,v.verified_repair_job_key,"
                "v.previous_error_code,v.validated_queue_count,"
                "v.validated_log_count "
                "FROM x_post_schedule_verified_repair_retry_audit v "
                "JOIN x_post_schedule_recovery_audit i "
                "ON i.id=v.initial_recovery_audit_id "
                "JOIN x_post_schedule_corrective_retry_audit c "
                "ON c.id=v.corrective_retry_audit_id "
                "WHERE v.schedule_run_id=? AND i.schedule_run_id=? "
                "AND c.schedule_run_id=?",
                (failed["id"], failed["id"], failed["id"]),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON,
                job_key,
                "x_post_media_repair_invalid_response",
                0,
                0,
            ),
        )

        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_post_media_repair_invalid_response",
            "unassigned Premium drama routing failed: "
            "X media repair probe does not meet the X video contract",
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_media_repair_invalid_response",
                reason=(
                    service.FAILED_PREFLIGHT_VERIFIED_REPAIR_RECOVERY_REASON
                ),
                actor="codex_operator",
                verified_repair_job_key=job_key,
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

        deployed_commit = "b" * 40
        validated_compensation = (
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_media_repair_invalid_response",
                reason=service.FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON,
                actor="codex_operator",
                verified_repair_job_key=job_key,
                deployed_commit=deployed_commit,
                compensation_publish_time="09:01",
                validate_only=True,
                now=recovery_now,
            )
        )
        self.assertEqual(
            validated_compensation["recovery_mode"],
            "codefix_compensation",
        )
        self.assertEqual(validated_compensation["updated_count"], 0)
        compensation = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_post_media_repair_invalid_response",
            reason=service.FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON,
            actor="codex_operator",
            verified_repair_job_key=job_key,
            deployed_commit=deployed_commit,
            compensation_publish_time="09:01",
            now=recovery_now,
        )
        self.assertEqual(compensation["updated_count"], 1)
        compensation_run_id = compensation["compensation_run_id"]
        original = self.store.get_schedule_run(failed["id"])
        compensation_run = self.store.get_schedule_run(compensation_run_id)
        self.assertEqual(original["status"], "failed_preflight")
        self.assertEqual(compensation_run["status"], "claimed")
        self.assertEqual(compensation_run["publish_time"], "09:01")
        self.assertEqual(
            compensation_run["slot_key"],
            "xpost:schedule:v1:drama:2026-07-27:0901",
        )
        self.assertEqual(
            compensation_run["account_ids"],
            original["account_ids"],
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            codefix_audit = conn.execute(
                "SELECT recovery_reason,deployed_commit,"
                "verified_repair_job_key,validated_queue_count,"
                "validated_log_count "
                "FROM x_post_schedule_codefix_compensation_audit "
                "WHERE original_schedule_run_id=? "
                "AND compensation_schedule_run_id=?",
                (failed["id"], compensation_run_id),
            ).fetchone()
        self.assertEqual(
            codefix_audit,
            (
                service.FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON,
                deployed_commit,
                job_key,
                0,
                0,
            ),
        )
        due = self.store.due_schedule_slots(recovery_now)
        self.assertEqual(
            [item["slot_key"] for item in due["items"]],
            [compensation_run["slot_key"]],
        )
        pool = self.add_drama(content_id="CODEFIX-COMPENSATION")
        created_plan = self.store.create_schedule_plan(
            "drama",
            "2026-07-27",
            "09:01",
            original["config_version"],
            [self.drama_candidate(pool, 2, 1)],
        )
        self.assertEqual(created_plan["id"], compensation_run_id)
        self.assertEqual(len(created_plan["queues"]), 1)
        with self.assertRaises(service.XPostError) as duplicate_compensation:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_post_media_repair_invalid_response",
                reason=service.FAILED_PREFLIGHT_CODEFIX_COMPENSATION_REASON,
                actor="codex_operator",
                verified_repair_job_key=job_key,
                deployed_commit=deployed_commit,
                compensation_publish_time="09:02",
                now=recovery_now,
            )
        self.assertEqual(
            duplicate_compensation.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_failed_preflight_recovery_rejects_stale_or_unready_scope(self):
        failed = self._failed_schedule_run("x_upstream_error")
        with self.assertRaises(service.XPostError) as stale:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_upstream_error",
                reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                now=datetime(
                    2026, 7, 28, 9, 5, tzinfo=service.BEIJING_TZ
                ),
            )
        self.assertEqual(
            stale.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )
        self._add_recovery_account(2, status="disabled")
        with self.assertRaises(service.XPostError) as unready:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_upstream_error",
                reason=service.FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                now=datetime(
                    2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
                ),
            )
        self.assertEqual(
            unready.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_drama_capability_fallback_recovery_is_commit_bound_and_once(self):
        failed = self._failed_schedule_run(source_type="drama")
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        message = (
            "episode DRAMA-LONG:1 media preflight failed: "
            "Videos longer than 140 seconds require a token-confirmed "
            "X Premium subscription"
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_long_video_requires_premium",
            message,
        )

        with self.assertRaises(service.XPostError) as missing_commit:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_long_video_requires_premium",
                reason=(
                    service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON
                ),
                actor="codex_operator",
                now=recovery_now,
            )
        self.assertEqual(
            missing_commit.exception.code,
            "x_post_failed_preflight_recovery_not_allowed",
        )

        deployed_commit = "c" * 40
        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_long_video_requires_premium",
            reason=service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=deployed_commit,
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(
            validated["recovery_mode"],
            "drama_capability_fallback",
        )
        self.assertEqual(validated["deployed_commit"], deployed_commit)
        self.assertEqual(validated["updated_count"], 0)

        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_long_video_requires_premium",
            reason=service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=deployed_commit,
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        self.assertEqual(
            self.store.get_schedule_run(failed["id"])["status"],
            "claimed",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT recovery_reason,actor,deployed_commit,"
                "previous_status,previous_error_code,"
                "validated_queue_count,validated_log_count "
                "FROM x_post_schedule_drama_capability_recovery_audit "
                "WHERE schedule_run_id=?",
                (failed["id"],),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON,
                "codex_operator",
                deployed_commit,
                "failed_preflight",
                "x_long_video_requires_premium",
                0,
                0,
            ),
        )

        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_long_video_requires_premium",
            message,
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_long_video_requires_premium",
                reason=(
                    service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON
                ),
                actor="codex_operator",
                deployed_commit=deployed_commit,
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_preflight_token_refresh_recovery_requires_capability_audit(self):
        failed = self._failed_schedule_run(source_type="drama")
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        token_commit = "d" * 40
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_account_not_publishable",
            "X账号当前不可用于手动发布",
        )
        with self.assertRaises(service.XPostError) as missing_capability_audit:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_account_not_publishable",
                reason=service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
                actor="codex_operator",
                deployed_commit=token_commit,
                now=recovery_now,
            )
        self.assertEqual(
            missing_capability_audit.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_long_video_requires_premium",
            "episode DRAMA-LONG:1 media preflight failed: Videos longer "
            "than 140 seconds require a token-confirmed X Premium "
            "subscription",
        )
        capability_commit = "c" * 40
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_long_video_requires_premium",
            reason=service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=capability_commit,
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_account_not_publishable",
            "X账号当前不可用于手动发布",
        )

        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_account_not_publishable",
            reason=service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=token_commit,
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(
            validated["recovery_mode"],
            "preflight_token_refresh",
        )
        self.assertEqual(validated["deployed_commit"], token_commit)
        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_account_not_publishable",
            reason=service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=token_commit,
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT t.recovery_reason,t.actor,t.deployed_commit,"
                "t.previous_error_code,t.validated_queue_count,"
                "t.validated_log_count "
                "FROM x_post_schedule_token_refresh_recovery_audit t "
                "JOIN x_post_schedule_drama_capability_recovery_audit d "
                "ON d.id=t.drama_capability_recovery_audit_id "
                "WHERE t.schedule_run_id=? AND d.schedule_run_id=?",
                (failed["id"], failed["id"]),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
                "codex_operator",
                token_commit,
                "x_account_not_publishable",
                0,
                0,
            ),
        )

        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_account_not_publishable",
            "X账号当前不可用于手动发布",
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "x_account_not_publishable",
                reason=service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
                actor="codex_operator",
                deployed_commit=token_commit,
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_transient_media_recovery_requires_token_refresh_audit(self):
        failed = self._failed_schedule_run(source_type="drama")
        recovery_now = datetime(
            2026, 7, 27, 9, 5, tzinfo=service.BEIJING_TZ
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_long_video_requires_premium",
            "episode DRAMA-LONG:1 media preflight failed: Videos longer "
            "than 140 seconds require a token-confirmed X Premium "
            "subscription",
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_long_video_requires_premium",
            reason=service.FAILED_PREFLIGHT_DRAMA_CAPABILITY_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit="c" * 40,
            now=recovery_now,
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "x_account_not_publishable",
            "X账号当前不可用于手动发布",
        )
        self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "x_account_not_publishable",
            reason=service.FAILED_PREFLIGHT_TOKEN_REFRESH_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit="d" * 40,
            now=recovery_now,
        )
        media_message = (
            "episode DRAMA-SLOW:1 media preflight failed: "
            "素材下载响应中断: The read operation timed out"
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "media_download_failed",
            media_message,
        )
        media_commit = "e" * 40
        validated = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "media_download_failed",
            reason=service.FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=media_commit,
            validate_only=True,
            now=recovery_now,
        )
        self.assertEqual(validated["recovery_mode"], "transient_media_retry")
        recovered = self.store.recover_failed_preflight_schedule_run(
            failed["id"],
            "media_download_failed",
            reason=service.FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON,
            actor="codex_operator",
            deployed_commit=media_commit,
            now=recovery_now,
        )
        self.assertEqual(recovered["updated_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            audit = conn.execute(
                "SELECT m.recovery_reason,m.deployed_commit,"
                "m.previous_error_code,m.validated_queue_count,"
                "m.validated_log_count "
                "FROM x_post_schedule_transient_media_recovery_audit m "
                "JOIN x_post_schedule_token_refresh_recovery_audit t "
                "ON t.id=m.token_refresh_recovery_audit_id "
                "WHERE m.schedule_run_id=? AND t.schedule_run_id=?",
                (failed["id"], failed["id"]),
            ).fetchone()
        self.assertEqual(
            audit,
            (
                service.FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON,
                media_commit,
                "media_download_failed",
                0,
                0,
            ),
        )
        self.store.record_schedule_failure(
            "drama",
            "2026-07-27",
            "09:00",
            failed["config_version"],
            [2],
            "media_download_failed",
            media_message,
        )
        with self.assertRaises(service.XPostError) as repeated:
            self.store.recover_failed_preflight_schedule_run(
                failed["id"],
                "media_download_failed",
                reason=service.FAILED_PREFLIGHT_TRANSIENT_MEDIA_RECOVERY_REASON,
                actor="codex_operator",
                deployed_commit=media_commit,
                now=recovery_now,
            )
        self.assertEqual(
            repeated.exception.code,
            "x_post_failed_preflight_recovery_conflict",
        )

    def test_schema_is_additive_and_integrity_check_passes(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("x_post_schedule_config", tables)
            self.assertIn("x_post_schedule_run", tables)
            self.assertIn("x_post_schedule_recovery_audit", tables)
            self.assertIn("x_post_schedule_corrective_retry_audit", tables)
            self.assertIn(
                "x_post_schedule_verified_repair_retry_audit",
                tables,
            )
            self.assertIn(
                "x_post_schedule_codefix_compensation_audit",
                tables,
            )
            self.assertIn(
                "x_post_schedule_drama_capability_recovery_audit",
                tables,
            )
            self.assertIn(
                "x_post_schedule_token_refresh_recovery_audit",
                tables,
            )
            self.assertIn(
                "x_post_schedule_transient_media_recovery_audit",
                tables,
            )
            self.assertIn("x_post_schedule_random_plan", tables)
            self.assertIn("x_post_drama_pool", tables)
            drama_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_drama_pool)"
                )
            }
            self.assertTrue(
                {
                    "assigned_account_id",
                    "assigned_at",
                    "assigned_source_queue_id",
                }.issubset(drama_columns)
            )
            config_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_schedule_config)"
                )
            }
            self.assertTrue(
                {
                    "schedule_mode",
                    "random_daily_count",
                    "random_effective_date",
                }.issubset(config_columns)
            )
            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger'"
                )
            }
            self.assertTrue(
                {
                    "trg_x_post_queue_drama_insert",
                    "trg_x_post_queue_drama_update",
                    "trg_x_post_queue_drama_assignment_source_delete",
                    "trg_x_post_drama_pool_assignment_immutable",
                    "trg_x_post_drama_pool_assignment_evidence",
                    "trg_x_post_drama_pool_assignment_insert_evidence",
                }.issubset(triggers)
            )
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )

    def test_random_schema_migration_preserves_legacy_fixed_config(self):
        legacy_path = Path(self.temp.name) / "legacy-schedule.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE x_post_schedule_config (
                    source_type TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    account_ids_json TEXT NOT NULL DEFAULT '[]',
                    publish_times_json TEXT NOT NULL DEFAULT '[]',
                    body_template TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE x_post_schedule_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_key TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    publish_time TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    config_version INTEGER NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    body_template TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_count INTEGER NOT NULL,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type,run_date,publish_time)
                );
                """
            )
            conn.execute(
                "INSERT INTO x_post_schedule_config("
                "source_type,enabled,account_ids_json,publish_times_json,"
                "body_template,version,created_at,updated_at"
                ") VALUES(?,1,?,?,?,?,?,?)",
                (
                    "material",
                    "[16,14]",
                    '["10:32"]',
                    "{{url}}\n🎬 {{drama_name}}\n{{desc}}",
                    15,
                    "2026-08-10T06:11:24Z",
                    "2026-08-10T06:11:24Z",
                ),
            )
            conn.commit()

        migrated = service.XPostStore(legacy_path).get_schedule_config(
            "material",
            now=datetime(
                2026,
                8,
                10,
                15,
                0,
                tzinfo=service.BEIJING_TZ,
            ),
        )
        self.assertEqual(migrated["schedule_mode"], "fixed")
        self.assertEqual(migrated["random_daily_count"], 0)
        self.assertEqual(migrated["random_effective_date"], "")
        self.assertEqual(migrated["account_ids"], [16, 14])
        self.assertEqual(migrated["publish_times"], ["10:32"])
        self.assertEqual(migrated["version"], 15)
        self.assertTrue(migrated["body_template"].startswith("{{url}}"))
        with contextlib.closing(sqlite3.connect(legacy_path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_schedule_random_plan"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()

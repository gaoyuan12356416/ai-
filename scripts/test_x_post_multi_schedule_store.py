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

    def test_drama_shortage_creates_no_partial_queue(self):
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
            "x_post_schedule_drama_shortage",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue"
                ).fetchone()[0],
                0,
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

    def test_known_drama_failure_blocks_later_episodes(self):
        self.save_schedule("drama", [2], ["09:00", "10:00"])
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
        blocked = self.store.query_drama_pool()["items"][0]
        self.assertEqual(blocked["status"], "needs_review")
        with self.assertRaises(service.XPostError) as unavailable:
            self.store.available_drama_pool_items()
        self.assertEqual(
            unavailable.exception.code,
            "x_post_drama_pool_needs_review",
        )

        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_schedule_plan(
                "drama",
                "2026-07-27",
                "10:00",
                2,
                [self.drama_candidate(later_pool, 2, 1)],
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_drama_pool_needs_review",
        )

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

    def test_first_drama_failure_stops_batch_with_later_queues_unexecuted(self):
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
        self.assertEqual(frozen["run"]["status"], "stopped")
        self.assertTrue(frozen["run"]["finished_at"])
        self.assertEqual(
            [item["status"] for item in frozen["queues"]],
            ["failed", "queued"],
        )

    def test_exact_pre_x_config_failure_can_restore_the_frozen_batch(self):
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

        validated = self.store.recover_pre_x_schedule_failure(
            first_queue["id"],
            "invalid_short_base_url",
            validate_only=True,
        )
        self.assertEqual(validated["validated_count"], 1)
        self.assertEqual(validated["updated_count"], 0)
        self.assertTrue(validated["validate_only"])
        self.assertEqual(
            self.store.query_schedule_plan(
                "drama",
                "2026-07-27",
                "09:00",
            )["run"]["status"],
            "stopped",
        )

        recovered = self.store.recover_pre_x_schedule_failure(
            first_queue["id"],
            "invalid_short_base_url",
        )
        self.assertEqual(recovered["updated_count"], 1)
        self.assertFalse(recovered["validate_only"])
        frozen = self.store.query_schedule_plan(
            "drama",
            "2026-07-27",
            "09:00",
        )
        self.assertEqual(frozen["run"]["status"], "queued")
        self.assertEqual(frozen["run"]["failed_count"], 0)
        self.assertEqual(
            [item["status"] for item in frozen["queues"]],
            ["queued", "queued"],
        )
        restored_log = self.store.get_log(log["id"])
        self.assertEqual(restored_log["status"], "reserved")
        self.assertEqual(restored_log["attempt_count"], 0)
        self.assertEqual(restored_log["error_code"], "")
        restored_pool = self.store.query_drama_pool(
            {"drama_id": "FIRST"}
        )["items"][0]
        self.assertEqual(restored_pool["status"], "active")
        self.assertEqual(restored_pool["assigned_account_id"], 2)
        self.assertEqual(restored_pool["next_sub_number"], 2)
        self.assertEqual(restored_pool["last_error_code"], "")

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

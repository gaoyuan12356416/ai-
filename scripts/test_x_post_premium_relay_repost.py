#!/usr/bin/env python3
"""Offline regression tests for Premium relay source plus target Repost."""

from __future__ import annotations

import contextlib
import json
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

from features.x_accounts import oauth_service  # noqa: E402
from features.x_posts import service  # noqa: E402


def compliance():
    return {
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
        "dangerous_tag_count": 0,
    }


class ScriptedHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected X request")
        return self.responses.pop(0)


class PremiumRelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "x-post.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def save_schedule(self, account_ids, times=("09:00",)):
        return self.store.save_schedule_config(
            "drama",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": list(account_ids),
                "publish_times": list(times),
                "version": 1,
            },
            actor={"user_id": "admin", "name": "Admin"},
            eligible_account_ids=list(account_ids),
            now=datetime(2026, 8, 12, 8, 0, tzinfo=service.BEIJING_TZ),
        )

    def add_drama(self, content_id, free_episode_count=2):
        return self.store.add_drama_pool_items(
            [content_id],
            [
                {
                    "content_id": content_id,
                    "drama_name": "Drama %s" % content_id,
                    "description": "A complete episode description.",
                    "language": "en",
                    "labels": "Romance",
                    "name_tag": "#Drama_%s" % content_id,
                    "free_episode_count": free_episode_count,
                }
            ],
            actor={"user_id": "admin", "name": "Admin"},
        )["items"][0]

    @staticmethod
    def candidate(pool, account_id, rank, *, long=True):
        episode = int(pool["next_sub_number"])
        return {
            "account_id": account_id,
            "account_username": "target%s" % account_id,
            "source_date": "2026-08-11",
            "source_type": "drama",
            "material_id": "M%s" % pool["id"],
            "content_id": pool["content_id"],
            "material_url": "https://media.example.test/%s.mp4" % pool["id"],
            "material_name": "Episode %s" % episode,
            "material_language": "en",
            "drama_name": "Drama %s" % pool["content_id"],
            "tag": "Romance",
            "description": "A complete episode description.",
            "page_name": "Target %s" % account_id,
            "page_id": "200%s" % account_id,
            "candidate_rank": rank,
            "drama_pool_item_id": pool["id"],
            "drama_pool_created_at": pool["created_at"],
            "episode_number": episode,
            "episode_key": "%s:%s" % (pool["content_id"], episode),
            "drama_replay_generation": int(pool.get("replay_generation", 1)),
            "name_tag": "#Drama_%s" % pool["content_id"],
            "preflight_sha256": ("%064x" % (1000 + rank))[-64:],
            "preflight_size": 1024 + rank,
            "preflight_duration": 180.0 if long else 90.0,
            "delivery_mode": (
                "premium_relay_repost" if long else "direct"
            ),
            "relay_account_id": 10 if long else 0,
            "relay_account_username": "premium10" if long else "",
            **compliance(),
        }

    def create_relay_plan(self, account_ids=(2,), relay_accounts=None):
        saved = self.save_schedule(account_ids)
        for index in range(len(account_ids)):
            self.add_drama("D%s" % (index + 1))
        assignments = self.store.available_drama_pool_items(
            1000,
            account_ids=list(account_ids),
            premium_account_ids=[],
        )
        candidates = [
            self.candidate(pool, int(pool["candidate_account_id"]), rank)
            for rank, pool in enumerate(assignments, 1)
        ]
        return self.store.create_schedule_plan(
            "drama",
            "2026-08-12",
            "09:00",
            saved["version"],
            candidates,
            premium_account_ids=[],
            premium_relay_accounts=relay_accounts
            or [{"id": 10, "username": "premium10"}],
        )

    def test_single_premium_account_receives_every_relay_assignment(self):
        plan = self.create_relay_plan(account_ids=(2, 3, 4, 5))
        self.assertEqual(
            [queue["relay_account_id"] for queue in plan["queues"]],
            [10, 10, 10, 10],
        )
        loads = self.store.premium_relay_account_loads(
            "2026-08-12", [10]
        )
        self.assertEqual(
            loads, [{"account_id": 10, "relay_assignment_count": 4}]
        )

    def test_multiple_premium_accounts_are_balanced_deterministically(self):
        plan = self.create_relay_plan(
            account_ids=(2, 3, 4, 5, 6),
            relay_accounts=[
                {"id": 11, "username": "premium11"},
                {"id": 10, "username": "premium10"},
            ],
        )
        self.assertEqual(
            [queue["relay_account_id"] for queue in plan["queues"]],
            [10, 11, 10, 11, 10],
        )
        loads = self.store.premium_relay_account_loads(
            "2026-08-12", [10, 11]
        )
        self.assertEqual(
            loads,
            [
                {"account_id": 11, "relay_assignment_count": 2},
                {"account_id": 10, "relay_assignment_count": 3},
            ],
        )
        self.assertLessEqual(
            max(item["relay_assignment_count"] for item in loads)
            - min(item["relay_assignment_count"] for item in loads),
            1,
        )

    def test_balancing_load_does_not_reset_at_midnight(self):
        saved = self.save_schedule((2,))
        self.add_drama("CROSS-DAY", free_episode_count=2)
        first_pool = self.store.available_drama_pool_items(
            1000, account_ids=[2], premium_account_ids=[]
        )[0]
        first = self.store.create_schedule_plan(
            "drama",
            "2026-08-12",
            "09:00",
            saved["version"],
            [self.candidate(first_pool, 2, 1)],
            premium_account_ids=[],
            premium_relay_accounts=[
                {"id": 11, "username": "premium11"},
                {"id": 10, "username": "premium10"},
            ],
        )["queues"][0]
        self.assertEqual(first["relay_account_id"], 10)
        log = self.store.reserve_log(first["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/view?x=1",
            "https://gy.g2flow.com/s2l/1.html",
            "Episode 1",
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media1")
        self.store.mark_relay_source_published(
            log["id"],
            "media1",
            "9001",
            "https://x.com/premium10/status/9001",
        )
        self.store.mark_reposting(first["id"])
        self.store.mark_reposted(first["id"], "99001")

        second_pool = self.store.available_drama_pool_items(
            1000, account_ids=[2], premium_account_ids=[]
        )[0]
        second_candidate = self.candidate(second_pool, 2, 1)
        second_candidate["source_date"] = "2026-08-12"
        second = self.store.create_schedule_plan(
            "drama",
            "2026-08-13",
            "09:00",
            saved["version"],
            [second_candidate],
            premium_account_ids=[],
            premium_relay_accounts=[
                {"id": 11, "username": "premium11"},
                {"id": 10, "username": "premium10"},
            ],
        )["queues"][0]
        self.assertEqual(second["relay_account_id"], 11)
        loads = self.store.premium_relay_account_loads(
            "2026-08-13", [10, 11]
        )
        self.assertEqual(
            loads,
            [
                {"account_id": 10, "relay_assignment_count": 1},
                {"account_id": 11, "relay_assignment_count": 1},
            ],
        )

    def test_operator_log_distinguishes_target_and_relay_accounts(self):
        queue = self.create_relay_plan()["queues"][0]
        item = self.store.query_logs({"page": 1, "page_size": 20})["items"][0]
        self.assertEqual(item["queue_id"], queue["id"])
        self.assertEqual(item["account_id"], 2)
        self.assertEqual(item["account_username"], "target2")
        self.assertEqual(item["delivery_mode"], "premium_relay_repost")
        self.assertEqual(item["relay_account_id"], 10)
        self.assertEqual(item["relay_account_username"], "premium10")
        self.assertEqual(item["repost_status"], "reserved")

    def test_storage_migration_is_idempotent_with_relay_rows(self):
        self.create_relay_plan(account_ids=(2, 3))
        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM x_post_repost_ledger").fetchone()[0],
                2,
            )
            indexes = {
                row[1]
                for row in conn.execute(
                    "PRAGMA index_list(x_post_repost_ledger)"
                ).fetchall()
            }
        self.assertIn("ix_x_post_repost_relay_load", indexes)
        self.assertIn("ux_x_post_repost_source_target", indexes)

    def test_entitlement_loss_reassigns_only_before_source_attempt(self):
        queue = self.create_relay_plan(
            relay_accounts=[
                {"id": 10, "username": "premium10"},
                {"id": 11, "username": "premium11"},
            ]
        )["queues"][0]
        self.assertEqual(queue["relay_account_id"], 10)
        reassigned = self.store.reassign_premium_relay(
            queue["id"], [{"id": 11, "username": "premium11"}]
        )
        self.assertEqual(reassigned["relay_account_id"], 11)
        self.assertEqual(
            self.store.get_repost_ledger(queue["id"])["relay_account_id"],
            11,
        )
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/view?x=1",
            "https://gy.g2flow.com/s2l/1.html",
            "Episode 1",
        )
        self.store.mark_publishing(log["id"])
        with self.assertRaises(service.XPostError) as caught:
            self.store.reassign_premium_relay(
                queue["id"], [{"id": 10, "username": "premium10"}]
            )
        self.assertEqual(
            caught.exception.code, "x_post_relay_reassignment_fenced"
        )

    def test_episode_advances_only_after_confirmed_target_repost(self):
        queue = self.create_relay_plan()["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/view?x=1",
            "https://gy.g2flow.com/s2l/1.html",
            "Episode 1",
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
            before = conn.execute(
                "SELECT next_sub_number,published_episode_count,status "
                "FROM x_post_drama_pool"
            ).fetchone()
        self.assertEqual(before, (1, 0, "active"))
        self.assertEqual(self.store.get_queue(queue["id"])["status"], "publishing")
        self.store.mark_reposting(queue["id"])
        source_time = self.store.get_log(log["id"])["published_at"]
        final_time = "2099-01-01T00:00:00Z"
        with mock.patch.object(service, "utc_now", return_value=final_time):
            self.store.mark_reposted(queue["id"], "99001")
        self.store.mark_reposted(queue["id"], "99001")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            after = conn.execute(
                "SELECT next_sub_number,published_episode_count,status "
                "FROM x_post_drama_pool"
            ).fetchone()
        self.assertEqual(after, (2, 1, "active"))
        self.assertEqual(self.store.get_queue(queue["id"])["status"], "published")
        self.assertEqual(
            self.store.get_repost_ledger(queue["id"])["status"], "reposted"
        )
        self.assertNotEqual(source_time, final_time)
        self.assertEqual(self.store.get_log(log["id"])["published_at"], final_time)

    def test_unknown_repost_never_reopens_source_publish(self):
        queue = self.create_relay_plan()["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/view?x=1",
            "https://gy.g2flow.com/s2l/1.html",
            "Episode 1",
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media1")
        self.store.mark_relay_source_published(
            log["id"], "media1", "9001", "https://x.com/premium10/status/9001"
        )
        self.store.mark_reposting(queue["id"])
        self.store.mark_repost_failed(
            queue["id"],
            "x_repost_outcome_unknown",
            "transport response was lost",
            True,
        )
        ledger = self.store.get_repost_ledger(queue["id"])
        self.assertEqual(ledger["status"], "needs_review")
        self.assertEqual(ledger["source_post_id"], "9001")
        self.assertEqual(ledger["source_attempt_count"], 1)
        self.assertEqual(ledger["repost_attempt_count"], 1)
        with self.assertRaises(service.XPostError) as caught:
            self.store.mark_publishing(log["id"])
        self.assertEqual(caught.exception.code, "x_post_unknown_outcome")
        self.assertTrue(caught.exception.unknown_outcome)

    def test_confirmed_source_with_lost_ledger_commit_is_fenced(self):
        queue = self.create_relay_plan()["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/view?x=1",
            "https://gy.g2flow.com/s2l/1.html",
            "Episode 1",
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media1")
        failed = self.store.mark_post_commit_unknown(
            log["id"],
            "media1",
            "9001",
            "https://x.com/premium10/status/9001",
            "simulated final commit loss",
        )
        ledger = self.store.get_repost_ledger(queue["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["unknown_outcome"])
        self.assertEqual(ledger["status"], "needs_review")
        self.assertTrue(ledger["unknown_outcome"])
        self.assertEqual(ledger["source_post_id"], "9001")
        self.assertEqual(ledger["source_attempt_count"], 1)

    def test_relay_pre_x_failure_updates_both_ledgers(self):
        queue = self.create_relay_plan()["queues"][0]
        log = self.store.reserve_log(queue["id"])
        failed = self.store.mark_failed_if_reserved(
            log["id"], "x_token_missing", "relay token is unavailable"
        )
        ledger = self.store.get_repost_ledger(queue["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["unknown_outcome"])
        self.assertEqual(ledger["status"], "failed")
        self.assertEqual(ledger["source_attempt_count"], 0)
        self.assertEqual(ledger["source_post_id"], "")

    def test_capability_miss_does_not_poison_bound_drama_pool(self):
        saved = self.save_schedule((2,), times=("09:00", "10:00"))
        pool = self.add_drama("BOUND")
        pool = self.store.available_drama_pool_items(
            1000,
            account_ids=[2],
            premium_account_ids=[],
        )[0]
        direct = self.candidate(pool, 2, 1, long=False)
        self.store.create_schedule_plan(
            "drama", "2026-08-12", "09:00", saved["version"], [direct]
        )
        failed = self.store.record_schedule_failure(
            "drama",
            "2026-08-12",
            "10:00",
            saved["version"],
            [2],
            "x_post_premium_relay_unavailable",
            "No Premium relay is currently available",
            drama_pool_item_id=pool["id"],
            content_id=pool["content_id"],
        )
        self.assertEqual(failed["status"], "failed_preflight")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,last_error_code FROM x_post_drama_pool WHERE id=?",
                (pool["id"],),
            ).fetchone()
        self.assertEqual(row, ("active", ""))

    def test_migration_reopens_only_proven_zero_write_capability_block(self):
        pool = self.add_drama("LEGACY")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE x_post_drama_pool SET status='needs_review',"
                "last_error_code='x_long_video_requires_premium',"
                "last_error_message='legacy deterministic preflight' "
                "WHERE id=?",
                (pool["id"],),
            )
            conn.commit()
        service.ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,last_error_code FROM x_post_drama_pool WHERE id=?",
                (pool["id"],),
            ).fetchone()
            audit = conn.execute(
                "SELECT previous_status,previous_error_code,recovery_reason "
                "FROM x_post_drama_capability_block_recovery WHERE pool_item_id=?",
                (pool["id"],),
            ).fetchone()
        self.assertEqual(row, ("pending", ""))
        self.assertEqual(
            audit,
            (
                "needs_review",
                "x_long_video_requires_premium",
                "premium_relay_repost_zero_write_migration_v1",
            ),
        )


class PremiumRelayApiTests(unittest.TestCase):
    def test_repost_uses_official_user_context_endpoint(self):
        http = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    headers={"content-type": "application/json"},
                    body=json.dumps(
                        {"data": {"retweeted": True, "rest_id": "99001"}}
                    ).encode("utf-8"),
                )
            ]
        )
        result = service.XApiClient(http_client=http).repost(
            "secret-token", "123456789", "9001"
        )
        self.assertEqual(result["repost_id"], "99001")
        method, url, request = http.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.x.com/2/users/123456789/retweets")
        self.assertEqual(
            json.loads(request["body"].decode("utf-8")),
            {"tweet_id": "9001"},
        )
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")

    def test_missing_reposted_confirmation_is_unknown(self):
        http = ScriptedHttpClient(
            [
                service.HttpResponse(
                    200,
                    headers={"content-type": "application/json"},
                    body=b'{"data":{"retweeted":false}}',
                )
            ]
        )
        with self.assertRaises(service.XPostError) as caught:
            service.XApiClient(http_client=http).repost(
                "secret-token", "123456789", "9001"
            )
        self.assertTrue(caught.exception.unknown_outcome)


class PremiumRelaySidecarOrchestrationTests(PremiumRelayStoreTests):
    def account(self, account_id):
        premium = account_id == 10
        return {
            "id": account_id,
            "username": "premium10" if premium else "target2",
            "x_user_id": "1010" if premium else "2002",
            "display_name": "Premium" if premium else "Target",
            "status": "active",
            "publish_approved": True,
            "publish_eligible": True,
            "subscription_type": "premium" if premium else "none",
            "premium_subscriber": premium,
            "long_video_eligible": premium,
            "long_video_publish_eligible": premium,
            "protected": False,
        }

    def test_sidecar_source_then_repost_is_idempotent(self):
        queue = self.create_relay_plan()["queues"][0]
        source_calls = []
        repost_calls = []
        verify_calls = []

        def fake_publish_canary(**kwargs):
            source_calls.append(int(kwargs["account"]["id"]))
            store = service.XPostStore(kwargs["db_path"])
            log = store.reserve_log(kwargs["queue_id"])
            store.prepare_log(
                log["id"],
                "https://www.dramawavew2a.com/view?x=1",
                "https://gy.g2flow.com/s2l/1.html",
                "Episode 1",
            )
            store.mark_publishing(log["id"])
            store.mark_media_uploaded(log["id"], "media1")
            published = store.mark_relay_source_published(
                log["id"],
                "media1",
                "9001",
                "https://x.com/premium10/status/9001",
            )
            return service._result_from_log(published)

        @contextlib.contextmanager
        def credentials(account_id, _actor, _scope):
            yield self.account(account_id), "token-%s" % account_id

        class FakeXApiClient:
            def __init__(self, **_kwargs):
                pass

            def repost(self, token, user_id, post_id):
                repost_calls.append((token, user_id, post_id))
                return {"repost_id": "99001", "data": {"retweeted": True}}

        def verify(account_id, *_args, **kwargs):
            verify_calls.append((int(account_id), dict(kwargs)))
            return self.account(account_id)

        with mock.patch.object(oauth_service, "POST_DB_PATH", self.db_path), mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=(service.XPostError, service.XPostStore, fake_publish_canary),
        ), mock.patch.object(
            oauth_service,
            "verify_account",
            side_effect=verify,
        ), mock.patch.object(
            oauth_service,
            "publish_credentials",
            side_effect=credentials,
        ), mock.patch(
            "features.x_posts.XApiClient", FakeXApiClient
        ):
            first = oauth_service.publish_queue_request(
                queue["id"], [2], allow_schedule=True
            )
            second = oauth_service.publish_queue_request(
                queue["id"], [2], allow_schedule=True
            )

        self.assertEqual(first["status"], "published")
        self.assertEqual(second, first)
        self.assertEqual(source_calls, [10])
        self.assertEqual(repost_calls, [("token-2", "2002", "9001")])
        self.assertEqual([call[0] for call in verify_calls], [10, 2])
        self.assertNotIn("only_refresh_required", verify_calls[0][1])
        self.assertTrue(verify_calls[1][1]["only_refresh_required"])

    def test_sidecar_unknown_repost_never_republishes_source(self):
        queue = self.create_relay_plan()["queues"][0]
        source_calls = []
        repost_calls = []

        def fake_publish_canary(**kwargs):
            source_calls.append(int(kwargs["account"]["id"]))
            store = service.XPostStore(kwargs["db_path"])
            log = store.reserve_log(kwargs["queue_id"])
            store.prepare_log(
                log["id"],
                "https://www.dramawavew2a.com/view?x=1",
                "https://gy.g2flow.com/s2l/1.html",
                "Episode 1",
            )
            store.mark_publishing(log["id"])
            store.mark_media_uploaded(log["id"], "media1")
            published = store.mark_relay_source_published(
                log["id"],
                "media1",
                "9001",
                "https://x.com/premium10/status/9001",
            )
            return service._result_from_log(published)

        @contextlib.contextmanager
        def credentials(account_id, _actor, _scope):
            yield self.account(account_id), "token-%s" % account_id

        class UnknownXApiClient:
            def __init__(self, **_kwargs):
                pass

            def repost(self, token, user_id, post_id):
                repost_calls.append((token, user_id, post_id))
                raise service.XPostError(
                    "x_repost_outcome_unknown",
                    "simulated lost response",
                    502,
                    True,
                )

        with mock.patch.object(oauth_service, "POST_DB_PATH", self.db_path), mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=(service.XPostError, service.XPostStore, fake_publish_canary),
        ), mock.patch.object(
            oauth_service,
            "verify_account",
            side_effect=lambda account_id, *_args, **_kwargs: self.account(account_id),
        ), mock.patch.object(
            oauth_service,
            "publish_credentials",
            side_effect=credentials,
        ), mock.patch(
            "features.x_posts.XApiClient", UnknownXApiClient
        ):
            with self.assertRaises(oauth_service.ServiceError) as first:
                oauth_service.publish_queue_request(
                    queue["id"], [2], allow_schedule=True
                )
            with self.assertRaises(oauth_service.ServiceError) as second:
                oauth_service.publish_queue_request(
                    queue["id"], [2], allow_schedule=True
                )

        self.assertEqual(first.exception.code, "x_publish_unknown")
        self.assertEqual(second.exception.code, "x_publish_unknown")
        self.assertEqual(source_calls, [10])
        self.assertEqual(repost_calls, [("token-2", "2002", "9001")])
        ledger = self.store.get_repost_ledger(queue["id"])
        self.assertEqual(ledger["status"], "needs_review")
        self.assertEqual(ledger["source_post_id"], "9001")


if __name__ == "__main__":
    unittest.main()

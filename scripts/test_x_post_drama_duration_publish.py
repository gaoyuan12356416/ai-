#!/usr/bin/env python3
"""Pure-mock publish-order tests for duration-pending drama queues."""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import features.x_posts as x_posts_package  # noqa: E402
from features.x_accounts import oauth_service  # noqa: E402
from features.x_posts import publish_media_repair  # noqa: E402
from features.x_posts import service as post_service  # noqa: E402


TARGET_ID = 7
RELAY_ID = 19
QUEUE_ID = 71
LOG_ID = 501
MAX_BYTES = 512 * 1024 * 1024


def pending_queue():
    return {
        "id": QUEUE_ID,
        "run_id": 0,
        "catchup_run_id": 0,
        "schedule_run_id": 44,
        "manual_run_id": 0,
        "run_date": "2026-09-01",
        "source_type": "drama",
        "status": "queued",
        "account_id": TARGET_ID,
        "account_username": "reeldrama",
        "account_drama_language": "en",
        "account_drama_language_frozen": 1,
        "delivery_mode": "duration_pending",
        "relay_account_id": 0,
        "relay_account_username": "",
        "route_version": post_service.DRAMA_DURATION_ROUTE_VERSION,
        "route_state": "duration_pending",
        "resolved_delivery_mode": "",
        "material_url": "https://media.example.test/source.mp4",
        "media_validation_mode": "deferred",
        "preflight_sha256": "",
        "preflight_size": 0,
        "preflight_duration": 0.0,
        "preflight_width": 0,
        "preflight_height": 0,
        "original_material_url": "",
        "media_repair_trigger_code": "",
        "media_repair_job_key": "",
        "media_repair_profile": "",
        "media_repair_source_sha256": "",
    }


def final_evidence(duration):
    return {
        "material_url": "https://media.example.test/final.mp4",
        "original_material_url": (
            "https://media.example.test/source.mp4"
        ),
        "media_repair_trigger_code": "invalid_media_codec",
        "media_repair_job_key": "a" * 64,
        "media_repair_profile": "x-h264-nvenc-720-duration-policy-v5",
        "media_repair_source_sha256": "b" * 64,
        "media_validation_mode": "preflight",
        "preflight_sha256": "c" * 64,
        "preflight_size": 123456,
        "preflight_duration": float(duration),
        "preflight_width": 720,
        "preflight_height": 1280,
    }


class BoundPreparedMedia:
    def __init__(self, events, resolved_queue):
        self.events = events
        self.resolved_queue = dict(resolved_queue)
        self.local_media = object()
        self.for_queue_calls = []

    def for_queue(self, queue, max_media_bytes):
        self.events.append("prepared_for_queue")
        self.for_queue_calls.append((dict(queue), int(max_media_bytes)))
        if dict(queue) != self.resolved_queue:
            raise AssertionError("publish did not reuse the resolver-frozen queue")
        if int(max_media_bytes) != MAX_BYTES:
            raise AssertionError("publish changed the prepared-media byte limit")
        return self.local_media


class PreparedMedia:
    def __init__(self, events, duration):
        self.events = events
        self.evidence = final_evidence(duration)
        self.bind_calls = []
        self.bound = None

    def bind_resolved(self, queue):
        self.events.append("prepared_bind")
        self.bind_calls.append(dict(queue))
        self.bound = BoundPreparedMedia(self.events, queue)
        return self.bound


class FakeStore:
    def __init__(self, events, duration, relay_accounts):
        self.events = events
        self.duration = float(duration)
        self.relay_accounts = list(relay_accounts)
        self.queue = pending_queue()
        self.resolve_calls = []
        self.reserve_calls = []
        self.log = {
            "id": LOG_ID,
            "status": "reserved",
            "unknown_outcome": False,
            "short_url": "",
            "x_post_id": "",
            "x_post_url": "",
        }

    def get_queue(self, queue_id):
        self.events.append("get_queue")
        if int(queue_id) != QUEUE_ID:
            raise AssertionError("unexpected queue")
        return dict(self.queue)

    def resolve_drama_duration_route(
        self,
        queue_id,
        media_evidence,
        target_long_video_eligible,
        eligible_relay_accounts,
    ):
        self.events.append("resolve_route")
        if int(queue_id) != QUEUE_ID:
            raise AssertionError("unexpected queue")
        if dict(media_evidence) != final_evidence(self.duration):
            raise AssertionError("resolver did not receive final media evidence")
        if list(eligible_relay_accounts) != self.relay_accounts:
            raise AssertionError("resolver received a different relay snapshot")
        self.resolve_calls.append(
            {
                "target_long_video_eligible": target_long_video_eligible,
                "relay_accounts": list(eligible_relay_accounts),
            }
        )
        resolved = {**self.queue, **dict(media_evidence)}
        if self.duration <= 140.0 or target_long_video_eligible:
            resolved.update(
                {
                    "status": "queued",
                    "route_state": "resolved",
                    "resolved_delivery_mode": "direct",
                    "delivery_mode": "direct",
                    "relay_account_id": 0,
                    "relay_account_username": "",
                }
            )
        elif self.relay_accounts:
            relay = self.relay_accounts[0]
            resolved.update(
                {
                    "status": "queued",
                    "route_state": "resolved",
                    "resolved_delivery_mode": "premium_relay_repost",
                    "delivery_mode": "premium_relay_repost",
                    "relay_account_id": int(relay["id"]),
                    "relay_account_username": str(relay["username"]),
                }
            )
        else:
            resolved.update(
                {
                    "status": "waiting_relay",
                    "route_state": "waiting_relay",
                    "resolved_delivery_mode": "",
                    "delivery_mode": "duration_pending",
                    "relay_account_id": 0,
                    "relay_account_username": "",
                }
            )
        self.queue = resolved
        return dict(resolved)

    def reserve_log(self, queue_id):
        self.events.append("reserve_log")
        self.reserve_calls.append(int(queue_id))
        if self.queue["route_state"] != "resolved":
            raise AssertionError("log reserved before route resolution")
        return dict(self.log)

    def mark_failed_if_reserved(self, *_args, **_kwargs):
        raise AssertionError("successful mock flow must not record failure")

    def get_log(self, log_id):
        if int(log_id) != LOG_ID:
            raise AssertionError("unexpected log")
        return dict(self.log)

    def mark_reposting(self, queue_id):
        self.events.append("mark_reposting")
        if int(queue_id) != QUEUE_ID or self.log["status"] != "source_published":
            raise AssertionError("repost started before source confirmation")
        return {"source_post_id": "source-post-900"}

    def mark_reposted(self, queue_id, repost_id):
        self.events.append("mark_reposted")
        if int(queue_id) != QUEUE_ID or repost_id != "repost-901":
            raise AssertionError("unexpected repost confirmation")
        self.log.update(
            {
                "status": "published",
                "short_url": "https://gy.g2flow.com/s2l/501.html",
                "x_post_id": "repost-901",
                "x_post_url": "https://x.com/reeldrama/status/repost-901",
            }
        )

    def mark_repost_failed(self, *_args, **_kwargs):
        raise AssertionError("successful mock repost must not fail")


class PublishHarness:
    def __init__(self, case, duration, target_eligible, relay_accounts):
        self.case = case
        self.events = []
        self.duration = float(duration)
        self.target_eligible = bool(target_eligible)
        self.relay_accounts = list(relay_accounts)
        self.store = FakeStore(self.events, duration, relay_accounts)
        self.prepared = PreparedMedia(self.events, duration)
        self.relay_lookup = mock.Mock(side_effect=self._relay_lookup)
        self.verify = mock.Mock(side_effect=self._verify_account)
        self.credentials = mock.Mock(side_effect=self._credentials_context)
        self.publish = mock.Mock(side_effect=self._publish_canary)

    def _account(self, account_id):
        if int(account_id) == TARGET_ID:
            return {
                "id": TARGET_ID,
                "username": "reeldrama",
                "x_user_id": "target-x-user",
                "drama_language": "en",
                "long_video_publish_eligible": self.target_eligible,
                "protected": False,
            }
        if int(account_id) == RELAY_ID:
            return {
                "id": RELAY_ID,
                "username": "premiumrelay",
                "x_user_id": "relay-x-user",
                "drama_language": "en",
                "long_video_publish_eligible": True,
                "protected": False,
            }
        raise AssertionError("unexpected account")

    def _verify_account(self, account_id, _actor, scope, **kwargs):
        if scope != "all":
            raise AssertionError("duration route did not use admin scope")
        suffix = ":refresh" if kwargs.get("only_refresh_required") else ""
        self.events.append("verify:%s%s" % (int(account_id), suffix))
        return self._account(account_id)

    def _relay_lookup(self, run_date, *, refresh, drama_language):
        self.events.append("relay_lookup")
        if run_date != "2026-09-01" or refresh is not True:
            raise AssertionError("relay lookup did not use current-token mode")
        if drama_language != "en":
            raise AssertionError("relay lookup crossed the frozen language")
        return list(self.relay_accounts)

    @contextlib.contextmanager
    def _credentials_context(self, account_id, _actor, scope):
        if scope != "all":
            raise AssertionError("credentials did not use admin scope")
        account_id = int(account_id)
        self.events.append("credentials_enter:%s" % account_id)
        try:
            yield self._account(account_id), "offline-test-token"
        finally:
            self.events.append("credentials_exit:%s" % account_id)

    @contextlib.contextmanager
    def _prepare_context(self, **kwargs):
        self.events.append("prepare_enter")
        if dict(kwargs["queue"]) != pending_queue():
            raise AssertionError("preparation did not receive the frozen queue")
        try:
            yield self.prepared
        finally:
            self.events.append("prepare_exit")

    def _publish_canary(self, **kwargs):
        self.events.append("publish_canary_enter")
        if kwargs.get("prepared_media") is not self.prepared.bound:
            raise AssertionError("bound prepared capability was not reused")
        local_media = kwargs["prepared_media"].for_queue(
            self.store.queue,
            kwargs["max_media_bytes"],
        )
        if local_media is not self.prepared.bound.local_media:
            raise AssertionError("prepared local file capability changed")
        self.events.append("x_source_write")
        if self.store.queue["delivery_mode"] == "premium_relay_repost":
            self.store.log.update(
                {
                    "status": "source_published",
                    "short_url": "https://gy.g2flow.com/s2l/501.html",
                    "x_post_id": "source-post-900",
                    "x_post_url": (
                        "https://x.com/premiumrelay/status/source-post-900"
                    ),
                }
            )
            return {
                "status": "source_published",
                "queue_id": QUEUE_ID,
                "log_id": LOG_ID,
                "post_id": "source-post-900",
            }
        return {
            "status": "published",
            "queue_id": QUEUE_ID,
            "log_id": LOG_ID,
            "short_url": "https://gy.g2flow.com/s2l/501.html",
            "post_id": "direct-post-800",
            "preview_url": "https://x.com/reeldrama/status/direct-post-800",
        }

    @contextlib.contextmanager
    def patches(self, *, enabled=True, prepare=True):
        class FakeXApiClient:
            def __init__(inner_self, *, timeout):
                if timeout != oauth_service.POST_HTTP_TIMEOUT_SECONDS:
                    raise AssertionError("unexpected X client timeout")

            def repost(inner_self, access_token, x_user_id, source_post_id):
                self.events.append("x_repost_write")
                if (
                    access_token != "offline-test-token"
                    or x_user_id != "target-x-user"
                    or source_post_id != "source-post-900"
                ):
                    raise AssertionError("unexpected mock repost request")
                return {"repost_id": "repost-901"}

        prepare_target = (
            self._prepare_context
            if prepare
            else mock.Mock(
                side_effect=AssertionError("parked queue must not prepare media")
            )
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "POST_DRAMA_DURATION_ROUTING_ENABLED",
                    enabled,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "POST_MAX_MEDIA_BYTES",
                    MAX_BYTES,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "_x_posts_api",
                    return_value=(
                        post_service.XPostError,
                        lambda _db_path: self.store,
                        self.publish,
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "verify_account",
                    self.verify,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "_premium_relay_accounts",
                    self.relay_lookup,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    oauth_service,
                    "publish_credentials",
                    self.credentials,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    publish_media_repair,
                    "prepare_duration_pending_drama_media",
                    prepare_target,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    x_posts_package,
                    "XApiClient",
                    FakeXApiClient,
                )
            )
            yield prepare_target

    def publish_queue(self):
        return oauth_service.publish_queue_request(
            QUEUE_ID,
            [TARGET_ID],
            allow_schedule=True,
        )

    def assert_order(self, *names):
        positions = [self.events.index(name) for name in names]
        self.case.assertEqual(positions, sorted(positions), self.events)


class DramaDurationPublishTest(unittest.TestCase):
    def test_short_video_resolves_direct_without_relay_and_reuses_file(self):
        harness = PublishHarness(self, 140.0, False, [])
        harness.relay_lookup.side_effect = AssertionError(
            "short video must not query relay accounts"
        )
        with harness.patches():
            result = harness.publish_queue()

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["post_id"], "direct-post-800")
        self.assertEqual(harness.store.queue["delivery_mode"], "direct")
        self.assertEqual(harness.store.reserve_calls, [QUEUE_ID])
        self.assertEqual(len(harness.prepared.bind_calls), 1)
        self.assertEqual(len(harness.prepared.bound.for_queue_calls), 1)
        harness.assert_order(
            "prepare_enter",
            "verify:7",
            "resolve_route",
            "prepared_bind",
            "reserve_log",
            "credentials_enter:7",
            "prepared_for_queue",
            "x_source_write",
            "prepare_exit",
        )

    def test_long_video_uses_same_language_relay_then_target_repost(self):
        relay = {
            "id": RELAY_ID,
            "username": "premiumrelay",
            "drama_language": "en",
        }
        harness = PublishHarness(self, 140.000001, False, [relay])
        with harness.patches():
            result = harness.publish_queue()

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["post_id"], "repost-901")
        self.assertEqual(
            harness.store.queue["delivery_mode"],
            "premium_relay_repost",
        )
        self.assertEqual(harness.store.queue["relay_account_id"], RELAY_ID)
        harness.relay_lookup.assert_called_once_with(
            "2026-09-01",
            refresh=True,
            drama_language="en",
        )
        self.assertEqual(len(harness.prepared.bound.for_queue_calls), 1)
        harness.assert_order(
            "prepare_enter",
            "verify:7",
            "relay_lookup",
            "resolve_route",
            "prepared_bind",
            "reserve_log",
            "credentials_enter:19",
            "prepared_for_queue",
            "x_source_write",
            "prepare_exit",
            "credentials_enter:7",
            "mark_reposting",
            "x_repost_write",
            "mark_reposted",
        )

    def test_missing_relay_waits_with_zero_log_credentials_or_x_write(self):
        harness = PublishHarness(self, 141.0, False, [])
        with harness.patches():
            result = harness.publish_queue()

        self.assertEqual(
            result,
            {
                "status": "waiting_relay",
                "queue_id": QUEUE_ID,
                "delivery_mode": "duration_pending",
                "preflight_duration": 141.0,
                "error_code": "x_post_premium_relay_unavailable",
            },
        )
        self.assertEqual(harness.store.reserve_calls, [])
        self.assertEqual(harness.prepared.bind_calls, [])
        harness.credentials.assert_not_called()
        harness.publish.assert_not_called()
        self.assertNotIn("x_source_write", harness.events)
        self.assertNotIn("x_repost_write", harness.events)
        harness.assert_order(
            "prepare_enter",
            "verify:7",
            "relay_lookup",
            "resolve_route",
            "prepare_exit",
        )

    def test_disabled_flag_parks_pending_with_zero_media_token_or_x(self):
        harness = PublishHarness(self, 0.0, False, [])
        harness.verify.side_effect = AssertionError(
            "disabled route must not verify or refresh a Token"
        )
        harness.relay_lookup.side_effect = AssertionError(
            "disabled route must not inspect relay accounts"
        )
        with harness.patches(enabled=False, prepare=False) as prepare_mock:
            result = harness.publish_queue()

        self.assertEqual(
            result,
            {
                "status": "waiting_relay",
                "queue_id": QUEUE_ID,
                "delivery_mode": "duration_pending",
                "preflight_duration": 0.0,
                "error_code": "x_post_drama_duration_routing_disabled",
            },
        )
        prepare_mock.assert_not_called()
        self.assertEqual(harness.store.resolve_calls, [])
        self.assertEqual(harness.store.reserve_calls, [])
        harness.credentials.assert_not_called()
        harness.publish.assert_not_called()
        self.assertEqual(harness.events, ["get_queue"])


if __name__ == "__main__":
    unittest.main()

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
    def test_storage_has_exactly_four_feature_tables(self):
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
            },
            names,
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

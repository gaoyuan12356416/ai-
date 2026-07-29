#!/usr/bin/env python3
"""Focused tests for the TT CPU sidecar, GPU client, and runner."""

from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from features.tt_gpu.credentials import open_access_token
from features.tt_posts.core import (
    LiveGates,
    SnapshotAccountSource,
    TTPostAccountSettings,
    TTPostError,
    TTPostStore,
)
from features.tt_posts.service import (
    ACCOUNT_LIST_SQL,
    ACCOUNT_METADATA_SQL,
    ACCOUNT_TOKEN_SQL,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    GPUClient,
    GPUClientError,
    MySQLSnapshotAccountRepository,
    SnapshotMySQLConfig,
    TTPostHTTPServer,
    TTPostService,
    TTPostServiceError,
    DramawaveMaterialResolver,
    _normalized_creator_info,
)
from scripts.tt_post_runner import (
    RunnerConfig,
    RunnerError,
    execute_runner_tick,
)


UTC = timezone.utc
OPEN_GATES = LiveGates(True, True, True)
CLOSED_GATES = LiveGates()
INTERNAL_TOKEN = "cpu-" + ("c" * 40)
GPU_TOKEN = "gpu-" + ("g" * 40)
SEAL_KEY_BYTES = b"k" * 32
SEAL_KEY = base64.urlsafe_b64encode(SEAL_KEY_BYTES).rstrip(b"=").decode("ascii")


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeCursor:
    def __init__(self, router, statements):
        self.router = router
        self.statements = statements
        self.rows = []

    def execute(self, sql, params=()):
        self.statements.append((str(sql), tuple(params)))
        self.rows = list(self.router(str(sql), tuple(params)))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class FakeConnection:
    def __init__(self, router, statements):
        self.router = router
        self.statements = statements

    def cursor(self):
        return FakeCursor(self.router, self.statements)

    def close(self):
        return None


def account_row(account_id="101"):
    return {
        "source_account_id": int(account_id),
        "main_account_id": "main-" + account_id,
        "external_account_id": "creator_" + account_id,
        "account_name": "Creator " + account_id,
        "account_link": "https://www.tiktok.com/@creator_" + account_id,
        "fan_count": 1234,
        "token_status": 2,
        "account_status": 2,
        "token_expires_time": 1999999999999999999,
        "last_token_checked_time": 1900000000000000000,
        "disable_publish": 0,
        "has_metric_snapshot": 1,
        "is_active": 1,
        "last_seen_at": "2026-07-29 10:00:00",
        "updated_at": "2026-07-29 10:00:00",
    }


class AccountSQLTests(unittest.TestCase):
    def config(self):
        return SnapshotMySQLConfig(
            "101.32.56.53",
            63350,
            "reader",
            "mysql-password",
            "ads_ai",
        )

    def test_list_and_metadata_sql_never_name_access_token(self):
        for statement in (ACCOUNT_LIST_SQL, ACCOUNT_METADATA_SQL):
            lowered = statement.lower()
            self.assertNotIn("access_token", lowered)
            self.assertNotRegex(lowered, r"select\s+\*")
            for condition in (
                "is_active = 1",
                "account_status = 2",
                "token_status = 2",
                "disable_publish = 0",
                "token_expires_time > %s",
            ):
                self.assertIn(condition, statement)

    def test_token_sql_is_exact_single_account_execution_query(self):
        lowered = ACCOUNT_TOKEN_SQL.lower()
        self.assertIn("select source_account_id, access_token", lowered)
        self.assertEqual(lowered.count("source_account_id = %s"), 1)
        self.assertNotIn(" in (", lowered)
        self.assertIn("limit 2", lowered)

    def test_list_uses_only_safe_statement_and_returns_full_safe_dto(self):
        statements = []

        def router(sql, _params):
            if sql == ACCOUNT_LIST_SQL:
                return [account_row()]
            raise AssertionError("unexpected SQL")

        repo = MySQLSnapshotAccountRepository(
            self.config(),
            connection_factory=lambda _config: FakeConnection(router, statements),
            now_fn=lambda: datetime(2026, 7, 29, tzinfo=UTC),
            verify_identity=False,
        )
        items = repo.list_public_accounts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_account_id"], "101")
        self.assertEqual(items[0]["account_name"], "Creator 101")
        self.assertEqual(items[0]["fan_count"], 1234)
        self.assertTrue(items[0]["publish_eligible"])
        rendered = json.dumps(items)
        self.assertNotIn("access_token", rendered.lower())
        self.assertEqual([sql for sql, _params in statements], [ACCOUNT_LIST_SQL])

    def test_exact_credential_context_queries_metadata_then_one_token(self):
        statements = []
        secret = "token-must-never-leak"

        def router(sql, params):
            if sql == ACCOUNT_METADATA_SQL:
                self.assertEqual(params[0], "101")
                return [account_row()]
            if sql == ACCOUNT_TOKEN_SQL:
                self.assertEqual(params[0], "101")
                return [{"source_account_id": 101, "access_token": secret}]
            raise AssertionError("unexpected SQL")

        repo = MySQLSnapshotAccountRepository(
            self.config(),
            connection_factory=lambda _config: FakeConnection(router, statements),
            now_fn=lambda: datetime(2026, 7, 29, tzinfo=UTC),
            verify_identity=False,
        )
        with repo.as_account_source().publish_credentials("101") as credentials:
            self.assertEqual(credentials.reveal_access_token(), secret)
            self.assertNotIn(secret, repr(credentials))
        self.assertEqual(
            [sql for sql, _params in statements],
            [ACCOUNT_METADATA_SQL, ACCOUNT_TOKEN_SQL],
        )


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _limit):
        return self._body


class CaptureConnection:
    def __init__(self, response_payload, status=200):
        self.response = FakeResponse(response_payload, status)
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "headers": dict(headers or {}),
            }
        )

    def getresponse(self):
        return self.response

    def close(self):
        return None


class GPUClientTests(unittest.TestCase):
    def test_signed_creator_avatar_url_is_omitted_from_public_dto(self):
        normalized = _normalized_creator_info(
            {
                "creator_nickname": "Creator",
                "creator_username": "creator",
                "creator_avatar_url": (
                    "https://avatar.example.com/a.jpg"
                    "?refresh_token=not-an-account-token"
                ),
                "privacy_level_options": ["SELF_ONLY"],
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": 3600,
            }
        )
        self.assertEqual(normalized["creator_avatar_url"], "")

    def client(self, connection):
        return GPUClient(
            "http://127.0.0.1:18830",
            GPU_TOKEN,
            SEAL_KEY,
            timeout=10,
            connection_factory=lambda _host, _port, _timeout: connection,
        )

    def test_creator_info_sends_envelope_and_never_raw_token(self):
        connection = CaptureConnection({"item": {"status": "ok"}})
        client = self.client(connection)
        raw_token = "raw-tiktok-secret"
        job_id = "ttcreator-101-abcdef123456"
        client.creator_info(
            job_id=job_id,
            source_account_id="101",
            access_token=raw_token,
        )
        request = connection.requests[0]
        payload = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(
            set(payload),
            {"job_id", "source_account_id", "credential_envelope"},
        )
        self.assertNotIn(raw_token, request["body"].decode("utf-8"))
        self.assertEqual(
            request["headers"]["Authorization"],
            "Bearer " + GPU_TOKEN,
        )
        with open_access_token(
            payload["credential_envelope"],
            SEAL_KEY,
            job_id=job_id,
            source_account_id="101",
            operation="creator_info",
        ) as decrypted:
            self.assertEqual(decrypted, raw_token)

    def test_prepare_contract_has_gpu_download_and_trim_only(self):
        connection = CaptureConnection({"item": {"status": "ready"}})
        client = self.client(connection)
        client.prepare(
            job_id="ttpost-abcdef1234567890",
            material={
                "content_id": "ABCD1234",
                "source_media_url": "https://cdn.example.com/source.mp4",
            },
            source_trim_tail_seconds=4.333333,
        )
        payload = json.loads(connection.requests[0]["body"].decode("utf-8"))
        self.assertEqual(
            set(payload),
            {
                "job_id",
                "content_id",
                "source_url",
                "source_trim_tail_seconds",
            },
        )
        self.assertNotIn("source_sha256", payload)
        self.assertNotIn("source_size", payload)
        self.assertNotIn("material_id", payload)

    def test_publish_is_flat_and_inverts_allow_flags(self):
        connection = CaptureConnection({"item": {"publish_id": "pub-1"}})
        client = self.client(connection)
        client.publish(
            job_id="ttpost-abcdef1234567890",
            source_account_id="101",
            access_token="token",
            queue={
                "caption": "hello",
                "privacy_level": "SELF_ONLY",
                "allow_comment": True,
                "allow_duet": False,
                "allow_stitch": True,
                "brand_content_toggle": False,
                "brand_organic_toggle": True,
                "is_aigc": True,
            },
        )
        payload = json.loads(connection.requests[0]["body"].decode("utf-8"))
        self.assertNotIn("post", payload)
        self.assertFalse(payload["disable_comment"])
        self.assertTrue(payload["disable_duet"])
        self.assertFalse(payload["disable_stitch"])
        self.assertTrue(payload["is_aigc"])
        self.assertNotIn("media_url", payload)

    def test_ineligible_media_profile_is_known_not_created(self):
        connection = CaptureConnection(
            {
                "code": "tt_media_profile_not_direct_post_eligible",
                "message": "prepared media profile is not eligible",
            },
            status=409,
        )
        client = self.client(connection)
        with self.assertRaises(GPUClientError) as caught:
            client.publish(
                job_id="ttpost-abcdef1234567890",
                source_account_id="101",
                access_token="token",
                queue={
                    "caption": "hello",
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": False,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": False,
                    "is_aigc": True,
                },
            )
        self.assertTrue(caught.exception.publish_was_not_created)
        self.assertFalse(caught.exception.unknown_outcome)

    def test_reconcile_uses_job_ledger_and_does_not_send_publish_id(self):
        connection = CaptureConnection({"item": {"state": "processing"}})
        client = self.client(connection)
        client.reconcile(
            job_id="ttpost-abcdef1234567890",
            source_account_id="101",
            access_token="token",
        )
        payload = json.loads(connection.requests[0]["body"].decode("utf-8"))
        self.assertEqual(
            set(payload),
            {"job_id", "source_account_id", "credential_envelope"},
        )
        self.assertNotIn("publish_id", payload)

    def test_gpu_client_rejects_any_non_exact_loopback_endpoint(self):
        with self.assertRaises(TTPostServiceError):
            GPUClient(
                "http://127.0.0.1:18831",
                GPU_TOKEN,
                SEAL_KEY,
            )


class FakeAccountRepository:
    def __init__(self):
        self.account = {
            "source_account_id": "101",
            "account_id": "101",
            "main_account_id": "main-101",
            "external_account_id": "creator_101",
            "username": "creator_101",
            "display_name": "Creator 101",
            "account_name": "Creator 101",
            "account_link": "https://www.tiktok.com/@creator_101",
            "avatar_url": "",
            "fan_count": 1234,
            "token_status": 2,
            "account_status": 2,
            "disable_publish": 0,
            "is_active": 1,
            "status": "active",
            "publish_eligible": True,
        }

    def list_public_accounts(self):
        return [dict(self.account)]

    def get_public_account(self, account_id):
        if str(account_id) != "101":
            raise TTPostServiceError("tt_account_not_found", "not found", 404)
        return dict(self.account)

    @staticmethod
    def _safe_account_mapping(item):
        return {
            "account_id": item["account_id"],
            "username": item["username"],
            "display_name": item["display_name"],
            "avatar_url": "",
            "status": "active",
            "publish_eligible": True,
        }

    def as_account_source(self):
        return SnapshotAccountSource(
            list_loader=lambda: [self._safe_account_mapping(self.account)],
            account_loader=lambda account_id: self._safe_account_mapping(
                self.get_public_account(account_id)
            ),
            token_loader=lambda account_id: {
                "account_id": str(account_id),
                "access_token": "ephemeral-token",
            },
        )


class FakeMaterialResolver:
    def __init__(self):
        self.source_url = "https://cdn.example.com/source-a.mp4"

    def resolve(self, material_id):
        return {
            "material_id": str(material_id),
            "content_id": "ABCD1234",
            "media_url": self.source_url,
            "source_media_url": self.source_url,
            "material_name": "Material",
            "drama_name": "Drama",
        }


def creator_info():
    return {
        "creator_info": {
            "creator_nickname": "Creator 101",
            "creator_username": "creator_live_101",
            "creator_avatar_url": "",
            "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
            "comment_disabled": False,
            "duet_disabled": False,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 600,
        },
        "status": "ok",
    }


class FakeGPU:
    def __init__(self):
        self.prepare_jobs = []
        self.prepare_job_id_override = ""
        self.publish_jobs = []
        self.reconcile_jobs = []
        self.publish_error = None
        self.reconcile_result = {
            "publish_id": "pub-101",
            "state": "published",
            "status": {"status": "PUBLISH_COMPLETE"},
        }
        self.creator_info_override = None

    def creator_info(self, **_kwargs):
        return self.creator_info_override or creator_info()

    def prepare(self, *, job_id, material, source_trim_tail_seconds):
        self.prepare_jobs.append(
            (job_id, material["source_media_url"], source_trim_tail_seconds)
        )
        return {
            "job_id": self.prepare_job_id_override or job_id,
            "content_id": material["content_id"],
            "output_sha256": "a" * 64,
            "output_size": 123456,
            "output_url": "https://cdn.example.com/prepared.mp4",
            "probe": {"duration": 45.5},
            "profile": "tt-post-v1",
            "status": "ready",
        }

    def publish(self, *, job_id, **_kwargs):
        self.publish_jobs.append(job_id)
        if self.publish_error:
            raise self.publish_error
        return {
            "job_id": job_id,
            "publish_id": "pub-101",
            "state": "initialized",
            "status": "ok",
        }

    def reconcile(self, *, job_id, **_kwargs):
        self.reconcile_jobs.append(job_id)
        return dict(self.reconcile_result)


def queue_payload(clock, publish_mode="hold", key="tt-post:test-key"):
    scheduled = (clock.value + timedelta(minutes=30)).isoformat()
    return {
        "idempotency_key": key,
        "source_account_id": "101",
        "material_id": "9001",
        "content_id": "ABCD1234",
        "scheduled_at": scheduled,
        "timezone": "Asia/Shanghai",
        "caption_text": (
            "Watch the full story in the app 🎬\n\n"
            "Drama ID: ABCD1234\n\n"
            "Visit my profile → Open the link → Search the Drama ID → Watch now."
        ),
        "publish_mode": publish_mode,
        "consent": {
            "accepted": True,
            "version": "tt-direct-post-consent-20260729",
            "accepted_at": clock.value.isoformat(),
        },
    }


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 7, 29, 3, 0, tzinfo=UTC))
        self.accounts = FakeAccountRepository()
        self.materials = FakeMaterialResolver()
        self.gpu = FakeGPU()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, gates, *, configure_settings=True):
        store = TTPostStore(
            Path(self.temp.name) / "tt.sqlite3",
            now_fn=self.clock,
        )
        if configure_settings and store.get_account_settings("101") is None:
            store.save_account_settings(
                "101",
                TTPostAccountSettings.from_mapping(
                    {
                        "privacy_level": "SELF_ONLY",
                        "allow_comment": False,
                        "allow_duet": False,
                        "allow_stitch": False,
                        "brand_content_toggle": False,
                        "brand_organic_toggle": False,
                        "is_aigc": True,
                    }
                ),
                expected_version=0,
            )
        return TTPostService(
            store,
            self.accounts,
            self.materials,
            self.gpu,
            gates=gates,
            now_fn=self.clock,
            source_trim_tail_seconds=4.333333,
            media_profile_version="tt-post-outro-20260729-v1",
        )

    def test_accounts_expose_configuration_state_without_credentials(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        initial = service.accounts()["items"][0]
        self.assertEqual(initial["account_settings"], {"configured": False})
        rendered = json.dumps(initial)
        self.assertNotIn("access_token", rendered)

        saved = service.account_settings_save(
            {
                "source_account_id": "101",
                "privacy_level": "SELF_ONLY",
                "allow_comment": True,
                "allow_duet": False,
                "allow_stitch": False,
                "commercial_disclosure": True,
                "brand_organic_toggle": True,
                "brand_content_toggle": False,
                "is_aigc": True,
                "expected_version": 0,
            }
        )["item"]
        self.assertEqual(saved["account_settings"]["version"], 1)
        self.assertTrue(saved["account_settings"]["allow_comment"])
        self.assertEqual(
            saved["creator_info"]["creator_username"],
            "creator_live_101",
        )
        listed = service.account_settings()["items"][0]
        self.assertTrue(listed["account_settings"]["configured"])
        self.assertEqual(listed["account_settings"]["version"], 1)

    def test_account_settings_save_revalidates_live_tiktok_capability(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        self.gpu.creator_info_override = {
            **creator_info(),
            "creator_info": {
                **creator_info()["creator_info"],
                "comment_disabled": True,
            },
        }
        with self.assertRaises(TTPostServiceError) as caught:
            service.account_settings_save(
                {
                    "source_account_id": "101",
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": True,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "commercial_disclosure": False,
                    "brand_organic_toggle": False,
                    "brand_content_toggle": False,
                    "is_aigc": False,
                    "expected_version": 0,
                }
            )
        self.assertEqual("tt_interaction_not_allowed", caught.exception.code)
        self.assertIsNone(service.store.get_account_settings("101"))

    def test_account_settings_reject_inconsistent_commercial_disclosure(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        with self.assertRaises(TTPostServiceError) as caught:
            service.account_settings_save(
                {
                    "source_account_id": "101",
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": False,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "commercial_disclosure": False,
                    "brand_organic_toggle": True,
                    "brand_content_toggle": False,
                    "is_aigc": False,
                    "expected_version": 0,
                }
            )
        self.assertEqual(
            "tt_commercial_disclosure_invalid",
            caught.exception.code,
        )

    def test_account_settings_require_optimistic_version(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        with self.assertRaises(TTPostServiceError) as caught:
            service.account_settings_save(
                {
                    "source_account_id": "101",
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": False,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "commercial_disclosure": False,
                    "brand_organic_toggle": False,
                    "brand_content_toggle": False,
                    "is_aigc": False,
                }
            )
        self.assertEqual(
            "invalid_account_settings_version",
            caught.exception.code,
        )
        self.assertEqual(self.gpu.prepare_jobs, [])

        invalid = {
            "source_account_id": "101",
            "privacy_level": "SELF_ONLY",
            "allow_comment": False,
            "allow_duet": False,
            "allow_stitch": False,
            "commercial_disclosure": False,
            "brand_organic_toggle": False,
            "brand_content_toggle": False,
            "is_aigc": False,
            "expected_version": 0.5,
        }
        with self.assertRaises(TTPostServiceError) as malformed:
            service.account_settings_save(invalid)
        self.assertEqual(
            "invalid_account_settings_version",
            malformed.exception.code,
        )

    def test_queue_requires_saved_account_settings_before_gpu_work(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        with self.assertRaises(TTPostError) as caught:
            service.queue_create(queue_payload(self.clock))
        self.assertEqual("tt_account_settings_required", caught.exception.code)
        self.assertEqual(self.gpu.prepare_jobs, [])

    def test_queue_freezes_saved_settings_and_ignores_legacy_overrides(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.update(
            {
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "allow_duet": True,
                "allow_stitch": True,
                "commercial_disclosure": True,
                "brand_organic_toggle": True,
                "brand_content_toggle": True,
                "is_aigc": False,
            }
        )
        created = service.queue_create(payload)["item"]
        self.assertEqual(created["privacy_level"], "SELF_ONLY")
        self.assertFalse(created["allow_comment"])
        self.assertFalse(created["allow_duet"])
        self.assertFalse(created["allow_stitch"])
        self.assertFalse(created["commercial_disclosure"])
        self.assertTrue(created["is_aigc"])

    def test_idempotent_replay_keeps_original_after_account_setting_changes(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        created = service.queue_create(payload)["item"]
        service.store.save_account_settings(
            "101",
            TTPostAccountSettings.from_mapping(
                {
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "allow_comment": True,
                    "allow_duet": True,
                    "allow_stitch": True,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": True,
                    "is_aigc": False,
                }
            ),
            expected_version=1,
        )
        replay = service.queue_create(payload)["item"]
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(replay["privacy_level"], "SELF_ONLY")
        self.assertFalse(replay["allow_comment"])
        self.assertTrue(replay["is_aigc"])
        self.assertEqual(len(self.gpu.prepare_jobs), 1)

    def test_preview_job_changes_when_only_source_url_changes(self):
        service = self.service(CLOSED_GATES)
        first = service.material_preview({"material_id": "9001"})["item"]
        first_job = first["gpu_job_id"]
        self.materials.source_url = "https://cdn.example.com/source-b.mp4"
        second = service.material_preview({"material_id": "9001"})["item"]
        self.assertNotEqual(first_job, second["gpu_job_id"])
        self.assertEqual(
            [item[1] for item in self.gpu.prepare_jobs],
            [
                "https://cdn.example.com/source-a.mp4",
                "https://cdn.example.com/source-b.mp4",
            ],
        )

    def test_prepare_rejects_gpu_job_identity_mismatch(self):
        service = self.service(CLOSED_GATES)
        self.gpu.prepare_job_id_override = "ttpost-wrong-job-identity"
        with self.assertRaises(TTPostServiceError) as caught:
            service.material_preview({"material_id": "9001"})
        self.assertEqual(
            caught.exception.code,
            "tt_prepared_media_identity_mismatch",
        )

    def test_closed_gate_hold_becomes_blocked_without_publish_init(self):
        service = self.service(CLOSED_GATES)
        created = service.queue_create(queue_payload(self.clock))["item"]
        self.assertEqual(created["publish_mode"], "hold")
        self.assertEqual(created["source_trim_tail_seconds"], 4.333333)
        self.assertEqual(created["prepared_output_sha256"], "a" * 64)
        self.assertEqual(created["account_display_name"], "Creator 101")
        self.assertEqual(
            created["creator_username_snapshot"],
            "creator_live_101",
        )
        self.assertTrue(created["creator_info_hash"])
        self.clock.value += timedelta(minutes=30)
        claimed = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": 600,
                "limit": 20,
            }
        )
        self.assertEqual(len(claimed["items"]), 1)
        item = claimed["items"][0]
        self.assertNotIn("claim_token", item)
        self.assertEqual(item["queue"]["queue_status"], "blocked_compliance")
        self.assertEqual(self.gpu.publish_jobs, [])

    def test_queue_generates_fixed_caption_when_client_omits_it(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.pop("caption_text")
        created = service.queue_create(payload)["item"]
        self.assertEqual(
            created["caption_text"],
            (
                "Watch the full story in the app 🎬\n\n"
                "Drama ID: ABCD1234\n\n"
                "Visit my profile → Open the link → Search the Drama ID → Watch now."
            ),
        )

    def test_queue_rejects_modified_caption_with_correct_drama_id(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload["caption_text"] = (
            "Custom copy\n\n"
            "Drama ID: ABCD1234\n\n"
            "Visit my profile → Open the link → Search the Drama ID → Watch now."
        )
        with self.assertRaises(TTPostServiceError) as caught:
            service.queue_create(payload)
        self.assertEqual(
            "tt_caption_fixed_template_mismatch",
            caught.exception.code,
        )
        self.assertEqual(self.gpu.prepare_jobs, [])

    def test_historical_custom_caption_exact_replay_remains_idempotent(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        created = service.queue_create(payload)["item"]
        historical_caption = (
            "Historical custom copy\n\n"
            "Drama ID: ABCD1234\n\n"
            "Visit my profile → Open the link → Search the Drama ID → Watch now."
        )
        connection = sqlite3.connect(Path(self.temp.name) / "tt.sqlite3")
        try:
            connection.execute(
                """
                UPDATE tt_post_queue
                SET caption_template=?,caption=?
                WHERE id=?
                """,
                (
                    historical_caption.replace(
                        "ABCD1234",
                        "{{contect_id}}",
                    ),
                    historical_caption,
                    created["id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        payload["caption_text"] = historical_caption
        replay = service.queue_create(payload)["item"]
        self.assertEqual(created["id"], replay["id"])
        self.assertEqual(historical_caption, replay["caption_text"])
        self.assertEqual(1, len(self.gpu.prepare_jobs))

    def test_claim_lease_is_shorter_than_grace_and_reclaims_claimed_once(self):
        self.assertLessEqual(DEFAULT_LEASE_SECONDS, 300)
        self.assertLess(DEFAULT_LEASE_SECONDS, DEFAULT_GRACE_SECONDS)
        service = self.service(OPEN_GATES)
        service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )
        self.clock.value += timedelta(minutes=30)
        first = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": DEFAULT_GRACE_SECONDS,
                "limit": 20,
            }
        )["items"][0]
        lease = datetime.fromisoformat(
            first["queue"]["lease_expires_at_utc"].replace("Z", "+00:00")
        )
        self.assertEqual(
            (lease - self.clock.value).total_seconds(),
            DEFAULT_LEASE_SECONDS,
        )
        self.clock.value += timedelta(seconds=DEFAULT_LEASE_SECONDS + 1)
        second = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": DEFAULT_GRACE_SECONDS,
                "limit": 20,
            }
        )["items"][0]
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertEqual(second["queue"]["attempt_count"], 2)

    def test_open_gate_uses_one_stable_job_then_reconciles_only(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        stable_job = created["gpu_job_id"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": 600,
                "limit": 20,
            }
        )["items"][0]
        published = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]
        self.assertEqual(published["status"], "reconciling")
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        reconciled = service.reconcile(created["id"])["item"]
        self.assertEqual(reconciled["status"], "published")
        self.assertEqual(self.gpu.reconcile_jobs, [stable_job])

    def test_automatic_reconcile_terminalizes_explicit_remote_failure(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        stable_job = created["gpu_job_id"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": 600,
                "limit": 20,
            }
        )["items"][0]
        pending = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]
        self.assertEqual("reconciling", pending["status"])
        self.gpu.reconcile_result = {
            "publish_id": "pub-101",
            "state": "publish_failed",
        }
        failed = service.reconcile(created["id"])["item"]
        self.assertEqual("failed", failed["status"])
        self.assertEqual("pub-101", failed["publish_id"])
        self.assertEqual(
            "tt_post_remote_publish_failed",
            failed["error_code"],
        )
        self.assertFalse(failed["unknown_outcome"])
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        self.assertEqual(
            service.claim_due(
                {
                    "worker_id": "tt-post-runner-primary",
                    "grace_seconds": 600,
                    "limit": 20,
                }
            )["items"],
            [],
        )
        events = service.events(created["id"])["items"]
        self.assertIn(
            "publish_reconciled_failed",
            [item["event_type"] for item in events],
        )

    def test_ineligible_media_profile_failure_never_becomes_unknown(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": DEFAULT_GRACE_SECONDS,
                "limit": 20,
            }
        )["items"][0]
        self.gpu.publish_error = GPUClientError(
            "tt_media_profile_not_direct_post_eligible",
            "prepared media profile is not eligible",
            409,
            publish_was_not_created=True,
        )
        failed = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]
        self.assertEqual("failed", failed["status"])
        self.assertEqual(
            "tt_media_profile_not_direct_post_eligible",
            failed["error_code"],
        )
        self.assertFalse(failed["unknown_outcome"])
        self.assertFalse(failed["publish_id"])

    def test_unknown_never_republishes_and_manual_gpu_ledger_recovery_is_reconcile_only(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        stable_job = created["gpu_job_id"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": 600,
                "limit": 20,
            }
        )["items"][0]
        self.gpu.publish_error = GPUClientError(
            "tt_upstream_unavailable",
            "network outcome unknown",
            503,
            unknown_outcome=True,
        )
        unknown = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]
        self.assertTrue(unknown["unknown_outcome"])
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(
            service.claim_due(
                {
                    "worker_id": "tt-post-runner-primary",
                    "grace_seconds": 600,
                    "limit": 20,
                }
            )["items"],
            [],
        )
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        self.gpu.reconcile_result = {
            "publish_id": "pub-recovered",
            "state": "processing",
            "status": {"status": "PROCESSING_DOWNLOAD"},
        }
        recovered = service.manual_reconcile(created["id"])["item"]
        self.assertEqual(recovered["status"], "reconciling")
        self.assertEqual(recovered["publish_id"], "pub-recovered")
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        self.assertEqual(self.gpu.reconcile_jobs, [stable_job])
        events = service.events(created["id"])["items"]
        self.assertIn(
            "publish_id_recovered_from_gpu_ledger",
            [item["event_type"] for item in events],
        )

    def test_manual_reconcile_terminalizes_recovered_remote_failure(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        stable_job = created["gpu_job_id"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": 600,
                "limit": 20,
            }
        )["items"][0]
        self.gpu.publish_error = GPUClientError(
            "tt_upstream_unavailable",
            "network outcome unknown",
            503,
            unknown_outcome=True,
        )
        unknown = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]
        self.assertEqual("unknown", unknown["status"])
        self.gpu.reconcile_result = {
            "publish_id": "pub-recovered-failed",
            "state": "failed",
        }
        failed = service.manual_reconcile(created["id"])["item"]
        self.assertEqual("failed", failed["status"])
        self.assertEqual("pub-recovered-failed", failed["publish_id"])
        self.assertEqual(
            "tt_post_remote_publish_failed",
            failed["error_code"],
        )
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        events = service.events(created["id"])["items"]
        event_types = [item["event_type"] for item in events]
        self.assertIn("publish_id_recovered_from_gpu_ledger", event_types)
        self.assertIn("publish_reconciled_failed", event_types)

    def test_idempotency_conflicts_when_frozen_content_identity_changes(self):
        service = self.service(CLOSED_GATES)
        service.queue_create(queue_payload(self.clock))
        changed = queue_payload(self.clock)
        changed["content_id"] = "DIFFERENT123"
        changed["caption_text"] = changed["caption_text"].replace(
            "ABCD1234",
            "DIFFERENT123",
        )
        with self.assertRaises(TTPostServiceError) as caught:
            service.queue_create(changed)
        self.assertEqual(caught.exception.code, "tt_post_idempotency_conflict")


class MaterialResolverTests(unittest.TestCase):
    def test_resolver_reuses_strict_x_selector_and_returns_source_url(self):
        connection = mock.Mock()
        connection.close = mock.Mock()
        resolver = DramawaveMaterialResolver(
            lambda: connection,
            now_fn=lambda: datetime(2026, 7, 29, tzinfo=UTC),
        )
        selected = [
            {
                "pool_item_id": 1,
                "material_id": "9001",
                "content_id": "ABCD1234",
                "material_url": "https://cdn.example.com/source.mp4",
                "material_name": "Material",
                "drama_name": "Drama",
                "material_language": "English",
                "description": "Description",
            }
        ]
        with mock.patch(
            "features.tt_posts.service.select_pool_candidates",
            return_value=(selected, []),
        ) as selector:
            item = resolver.resolve("9001")
        self.assertEqual(item["content_id"], "ABCD1234")
        self.assertEqual(
            item["source_media_url"],
            "https://cdn.example.com/source.mp4",
        )
        selector.assert_called_once()
        connection.close.assert_called_once()


class FakeRunnerSidecar:
    def __init__(self):
        self.calls = []

    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return [
            {
                "id": 1,
                "source_account_id": "101",
                "status": "reconciling",
                "publish_id": "pub-1",
            }
        ]

    def reconcile(self, queue_id):
        self.calls.append(("reconcile", queue_id))
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "101",
                "status": "published",
                "publish_id": "pub-1",
            }
        }

    def claim(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return [
            {
                "queue": {
                    "id": 2,
                    "source_account_id": "101",
                    "status": "hold",
                    "queue_status": "blocked_compliance",
                }
            },
            {
                "queue": {
                    "id": 3,
                    "source_account_id": "102",
                    "status": "unknown",
                    "unknown_outcome": True,
                },
                "claim_token": "must-not-be-used",
            },
            {
                "queue": {
                    "id": 4,
                    "source_account_id": "103",
                    "status": "claimed",
                },
                "claim_token": "claim-secret",
            },
        ]

    def publish(self, queue_id, claim_token):
        self.calls.append(("publish", queue_id, claim_token))
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "103",
                "status": "reconciling",
                "publish_id": "pub-4",
            }
        }


class ReconcileIsolationSidecar(FakeRunnerSidecar):
    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return [
            {
                "id": 11,
                "source_account_id": "101",
                "status": "reconciling",
                "publish_id": "pub-11",
            },
            {
                "id": 12,
                "source_account_id": "102",
                "status": "reconciling",
                "publish_id": "pub-12",
            },
        ]

    def reconcile(self, queue_id):
        self.calls.append(("reconcile", queue_id))
        if queue_id == 11:
            raise RunnerError(
                "tt_post_publish_id_conflict",
                "Authorization: Bearer must-not-render",
                409,
            )
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "102",
                "status": "published",
                "publish_id": "pub-12",
            }
        }


class PublishIsolationSidecar(FakeRunnerSidecar):
    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return []

    def claim(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return [
            {
                "queue": {
                    "id": 21,
                    "source_account_id": "201",
                    "status": "claimed",
                },
                "claim_token": "claim-secret-21",
            },
            {
                "queue": {
                    "id": 22,
                    "source_account_id": "202",
                    "status": "claimed",
                },
                "claim_token": "claim-secret-22",
            },
        ]

    def publish(self, queue_id, claim_token):
        self.calls.append(("publish", queue_id, claim_token))
        if queue_id == 21:
            raise RunnerError(
                "tt_post_publish_preflight_failed",
                "Authorization: Bearer must-not-render-publish",
                409,
            )
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "202",
                "status": "reconciling",
                "publish_id": "pub-22",
            }
        }


class ReconcileBacklogSidecar(FakeRunnerSidecar):
    def claim(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return [
            {
                "queue": {
                    "id": 31,
                    "source_account_id": "301",
                    "status": "claimed",
                },
                "claim_token": "claim-secret-31",
            }
        ]

    def publish(self, queue_id, claim_token):
        self.calls.append(("publish", queue_id, claim_token))
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "301",
                "status": "reconciling",
                "publish_id": "pub-31",
            }
        }

    def reconciling(self, limit):
        self.calls.append(("reconciling", limit))
        return [
            {
                "id": 1000 + index,
                "source_account_id": str(400 + index),
                "status": "reconciling",
                "publish_id": "pub-backlog-%s" % index,
            }
            for index in range(limit)
        ]

    def reconcile(self, queue_id):
        self.calls.append(("reconcile", queue_id))
        return {
            "item": {
                "id": queue_id,
                "status": "published",
                "publish_id": "present",
            }
        }


class RunnerTests(unittest.TestCase):
    def config(self):
        return RunnerConfig(
            internal_url="http://127.0.0.1:18829",
            internal_token=INTERNAL_TOKEN,
            worker_id="tt-post-runner-primary",
            grace_seconds=600,
            claim_limit=20,
            reconcile_limit=5,
            timeout=30,
            lock_path=str(Path(tempfile.gettempdir()) / "tt-post-runner.lock"),
        )

    def test_runner_claims_first_and_never_republishes_unknown(self):
        sidecar = FakeRunnerSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["claim", "publish", "reconciling", "reconcile"],
        )
        publish = sidecar.calls[1]
        self.assertEqual(publish[1], 4)
        rendered = json.dumps(result)
        self.assertNotIn("claim-secret", rendered)
        self.assertNotIn("must-not-be-used", rendered)
        self.assertEqual(result["publish_request_count"], 1)

    def test_reconcile_business_error_does_not_block_other_items_or_claims(self):
        sidecar = ReconcileIsolationSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            [
                "claim",
                "publish",
                "reconciling",
                "reconcile",
                "reconcile",
            ],
        )
        self.assertEqual(result["reconcile_count"], 1)
        self.assertEqual(result["reconcile_error_count"], 1)
        self.assertEqual(result["publish_request_count"], 1)
        error = next(
            item
            for item in result["results"]
            if item["operation"] == "reconcile_error"
        )
        self.assertEqual(error["queue_id"], 11)
        self.assertEqual(error["error_code"], "tt_post_publish_id_conflict")
        rendered = json.dumps(result)
        self.assertNotIn("must-not-render", rendered)
        self.assertNotIn("claim-secret", rendered)

    def test_reconcile_backlog_starts_only_after_due_claim_publish(self):
        sidecar = ReconcileBacklogSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls[:3]],
            ["claim", "publish", "reconciling"],
        )
        self.assertEqual(result["publish_request_count"], 1)
        self.assertEqual(result["reconcile_count"], self.config().reconcile_limit)
        self.assertEqual(result["reconcile_budget"], 5)

    def test_publish_business_error_does_not_block_later_claims(self):
        sidecar = PublishIsolationSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["claim", "publish", "publish", "reconciling"],
        )
        self.assertEqual(result["publish_request_count"], 1)
        self.assertEqual(result["publish_request_error_count"], 1)
        error = next(
            item
            for item in result["results"]
            if item["operation"] == "publish_request_error"
        )
        self.assertEqual(error["queue_id"], 21)
        self.assertEqual(
            error["error_code"],
            "tt_post_publish_preflight_failed",
        )
        rendered = json.dumps(result)
        self.assertNotIn("must-not-render-publish", rendered)
        self.assertNotIn("claim-secret-21", rendered)
        self.assertNotIn("claim-secret-22", rendered)

    def test_sidecar_unreachable_before_listing_still_fails_tick(self):
        class UnreachableSidecar(FakeRunnerSidecar):
            def reconciling(self, _limit):
                raise RunnerError(
                    "tt_post_sidecar_unreachable",
                    "sidecar unavailable",
                    502,
                )

        with self.assertRaises(RunnerError) as caught:
            execute_runner_tick(self.config(), client=UnreachableSidecar())
        self.assertEqual(caught.exception.code, "tt_post_sidecar_unreachable")

    def test_runner_rejects_any_grace_other_than_ten_minutes(self):
        bad = RunnerConfig(
            **{
                **self.config().__dict__,
                "grace_seconds": 601,
            }
        )
        with self.assertRaises(RunnerError):
            bad.validate()


class HTTPContractTests(unittest.TestCase):
    class Facade:
        gates = CLOSED_GATES

        def accounts(self):
            return {"items": [], "gates": self.gates.as_dict()}

        def account_settings(self):
            return {"items": [], "marker": "account-settings"}

        def account_settings_save(self, payload):
            return {"item": {"marker": "account-settings-save", **payload}}

        def creator_info(self, payload):
            return {"item": {"marker": "creator", **payload}}

        def material_preview(self, payload):
            return {"item": {"marker": "material", **payload}}

        def queue_list(self, _query):
            return {"items": [], "marker": "queue-list"}

        def queue_create(self, payload):
            return {"item": {"marker": "queue-create", **payload}}

        def queue_cancel(self, queue_id, _payload):
            return {"item": {"marker": "cancel", "id": int(queue_id)}}

        def manual_reconcile(self, queue_id):
            return {"item": {"marker": "manual", "id": int(queue_id)}}

        def events(self, queue_id):
            return {"items": [{"marker": "events", "queue_id": str(queue_id)}]}

        def claim_due(self, _payload):
            return {"items": []}

        def reconciling(self, _limit):
            return {"items": []}

        def publish_claimed(self, queue_id, _claim_token):
            return {"item": {"marker": "publish", "id": int(queue_id)}}

        def reconcile(self, queue_id):
            return {"item": {"marker": "reconcile", "id": int(queue_id)}}

    def setUp(self):
        self.server = TTPostHTTPServer(
            ("127.0.0.1", 0),
            self.Facade(),
            INTERNAL_TOKEN,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + INTERNAL_TOKEN,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_loopback_routes_cover_current_ui_and_runner_contract(self):
        self.assertEqual(
            self.request("/api/admin/tt-posts/accounts")["items"],
            [],
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/account-settings"
            )["marker"],
            "account-settings",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/account-settings/creator-info",
                "POST",
                {"source_account_id": "101"},
            )["item"]["marker"],
            "creator",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/account-settings",
                "POST",
                {"source_account_id": "101"},
            )["item"]["marker"],
            "account-settings-save",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/creator-info",
                "POST",
                {"source_account_id": "101"},
            )["item"]["marker"],
            "creator",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/materials/preview",
                "POST",
                {"material_id": "9001"},
            )["item"]["marker"],
            "material",
        )
        self.assertEqual(
            self.request("/api/admin/tt-posts/queue")["marker"],
            "queue-list",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/queue",
                "POST",
                {"idempotency_key": "key"},
            )["item"]["marker"],
            "queue-create",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/queue/7/cancel",
                "POST",
                {},
            )["item"]["marker"],
            "cancel",
        )
        self.assertEqual(
            self.request("/api/admin/tt-posts/events?queue_id=7")["items"][0][
                "marker"
            ],
            "events",
        )
        self.assertEqual(
            self.request(
                "/internal/tt-posts/claim",
                "POST",
                {},
            )["items"],
            [],
        )
        self.assertEqual(
            self.request("/internal/tt-posts/reconciling?limit=10")["items"],
            [],
        )
        self.assertEqual(
            self.request(
                "/internal/tt-posts/queue/7/publish",
                "POST",
                {"claim_token": "claim"},
            )["item"]["marker"],
            "publish",
        )
        self.assertEqual(
            self.request(
                "/internal/tt-posts/queue/7/reconcile",
                "POST",
                {},
            )["item"]["marker"],
            "reconcile",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/queue/7/reconcile",
                "POST",
                {},
            )["item"]["marker"],
            "manual",
        )


class DeployContractTests(unittest.TestCase):
    def test_deploy_defaults_are_fail_closed_and_minutely(self):
        root = Path(__file__).resolve().parents[1]
        env = (root / "deploy" / "tt-post.env.example").read_text("utf-8")
        app_env = (
            root / "deploy" / "tt-post-app.env.example"
        ).read_text("utf-8")
        app_drop_in = (
            root / "deploy" / "drama-material-api-tt-post.conf"
        ).read_text("utf-8")
        timer = (root / "deploy" / "tt-post-runner.timer").read_text("utf-8")
        service = (root / "deploy" / "tt-post-service.service").read_text("utf-8")
        runner = (root / "deploy" / "tt-post-runner.service").read_text("utf-8")
        self.assertIn("TT_POST_LIVE_ENABLED=0", env)
        self.assertIn("TT_POST_DIRECT_AUDIT_APPROVED=0", env)
        self.assertIn("TT_POST_URL_PROPERTY_VERIFIED=0", env)
        self.assertIn("TT_POST_GRACE_SECONDS=600", env)
        self.assertIn("TT_POST_RECONCILE_LIMIT=5", env)
        self.assertIn(
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333",
            env,
        )
        self.assertIn("OnCalendar=*-*-* *:*:00 Asia/Shanghai", timer)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("EnvironmentFile=/etc/tt-post.secrets", service)
        self.assertIn("TimeoutStartSec=3600s", runner)
        self.assertNotIn("TimeoutStartSec=55s", runner)
        self.assertIn(
            "TT_POST_ADMIN_SERVICE_URL=http://127.0.0.1:18829",
            app_env,
        )
        self.assertIn("TT_POST_INTERNAL_TOKEN=", app_env)
        self.assertIn(
            "EnvironmentFile=-/etc/tt-post-app.env",
            app_drop_in,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

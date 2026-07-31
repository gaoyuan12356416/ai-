#!/usr/bin/env python3
"""Focused tests for the TT CPU sidecar, GPU client, and runner."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from features.tt_gpu.credentials import open_access_token
from features.tt_posts.core import (
    AccountSourceError,
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
    ManualPublishCanary,
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
    TTPostSidecarClient,
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
        self.assertEqual((client.timeout, client.prepare_timeout), (10, 10))
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
        connection_timeouts = []
        client = GPUClient(
            "http://127.0.0.1:18830",
            GPU_TOKEN,
            SEAL_KEY,
            timeout=10,
            prepare_timeout=9000,
            connection_factory=lambda _host, _port, timeout: (
                connection_timeouts.append(timeout) or connection
            ),
        )
        client.prepare(
            job_id="ttpost-abcdef1234567890",
            material={
                "content_id": "ABCD1234",
                "source_media_url": "https://cdn.example.com/source.mp4",
            },
            source_trim_tail_seconds=4.333333,
            expected_profile="tt-post-hevc-720x1280-v2",
        )
        payload = json.loads(connection.requests[0]["body"].decode("utf-8"))
        self.assertEqual(
            set(payload),
            {
                "job_id",
                "content_id",
                "expected_profile",
                "source_url",
                "source_trim_tail_seconds",
            },
        )
        self.assertEqual(
            payload["expected_profile"],
            "tt-post-hevc-720x1280-v2",
        )
        self.assertNotIn("source_sha256", payload)
        self.assertNotIn("source_size", payload)
        self.assertNotIn("material_id", payload)
        self.assertEqual(connection_timeouts, [9000])

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

    def test_manual_canary_uses_dedicated_gpu_route_and_envelope_operation(self):
        connection = CaptureConnection({"item": {"publish_id": "pub-1"}})
        client = self.client(connection)
        job_id = "ttpost-abcdef1234567890"
        client.publish(
            job_id=job_id,
            source_account_id="101",
            access_token="token",
            queue={
                "material_id": "9001",
                "caption": "private test",
                "privacy_level": "SELF_ONLY",
                "allow_comment": False,
                "allow_duet": False,
                "allow_stitch": False,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": True,
            },
            manual_canary=True,
            manual_canary_id="test-canary-001",
        )
        request = connection.requests[0]
        self.assertEqual(
            request["path"],
            "/internal/tt-post/canary-publish",
        )
        payload = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(payload["manual_canary_id"], "test-canary-001")
        self.assertEqual(payload["material_id"], "9001")
        with open_access_token(
            payload["credential_envelope"],
            SEAL_KEY,
            job_id=job_id,
            source_account_id="101",
            operation="canary_publish",
        ) as decrypted:
            self.assertEqual(decrypted, "token")

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


class ManualCanaryConfigTests(unittest.TestCase):
    def test_expired_well_formed_canary_boots_inactive(self):
        config = ManualPublishCanary.from_env(
            {
                "TT_POST_MANUAL_CANARY_ENABLED": "1",
                "TT_POST_MANUAL_CANARY_ACKNOWLEDGEMENT": (
                    "I_ACCEPT_ONE_SHOT_PRIVATE_TIKTOK_CANARY_20260731"
                ),
                "TT_POST_MANUAL_CANARY_ID": "expired-canary-001",
                "TT_POST_MANUAL_CANARY_EXPIRES_AT_UTC": (
                    "2020-01-01T00:00:00Z"
                ),
                "TT_POST_MANUAL_CANARY_ACCOUNT_ID": "101",
                "TT_POST_MANUAL_CANARY_POOL_ID": "1",
                "TT_POST_MANUAL_CANARY_MATERIAL_ID": "9001",
                "TT_POST_MANUAL_CANARY_CONTENT_ID": "ABCD1234",
                "TT_POST_MANUAL_CANARY_GPU_JOB_ID": (
                    "ttpost-abcdef1234567890"
                ),
                "TT_POST_MANUAL_CANARY_OUTPUT_SHA256": "a" * 64,
                "TT_POST_MANUAL_CANARY_OUTPUT_SIZE": "123456",
                "TT_POST_MANUAL_CANARY_PROFILE": (
                    "tt-post-hevc-720x1280-v2"
                ),
            }
        )
        self.assertTrue(config.ready)
        self.assertFalse(config.is_active())


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
        self.accounts = {"101": self.account}

    def add_account(self, account_id):
        account_id = str(account_id)
        item = {
            **self.account,
            "source_account_id": account_id,
            "account_id": account_id,
            "main_account_id": "main-" + account_id,
            "external_account_id": "creator_" + account_id,
            "username": "creator_" + account_id,
            "display_name": "Creator " + account_id,
            "account_name": "Creator " + account_id,
            "account_link": "https://www.tiktok.com/@creator_" + account_id,
        }
        self.accounts[account_id] = item
        return item

    def list_public_accounts(self):
        return [dict(item) for item in self.accounts.values()]

    def get_public_account(self, account_id):
        item = self.accounts.get(str(account_id))
        if item is None:
            raise TTPostServiceError("tt_account_not_found", "not found", 404)
        return dict(item)

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
            list_loader=lambda: [
                self._safe_account_mapping(item)
                for item in self.accounts.values()
            ],
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
        self.timeout = 900
        self.prepare_jobs = []
        self.prepare_error = None
        self.prepare_job_id_override = ""
        self.prepared_profile = "tt-post-hevc-720x1280-v2"
        self.publish_jobs = []
        self.publish_requests = []
        self.reconcile_jobs = []
        self.publish_error = None
        self.reconcile_result = {
            "publish_id": "pub-101",
            "state": "published",
            "status": {"status": "PUBLISH_COMPLETE"},
        }
        self.creator_info_override = None
        self.creator_info_by_account = {}
        self.creator_info_calls = []
        self.prepared_duration = 45.5

    def creator_info(self, **kwargs):
        account_id = str(kwargs.get("source_account_id") or "")
        self.creator_info_calls.append(account_id)
        if self.creator_info_override is not None:
            return self.creator_info_override
        if account_id in self.creator_info_by_account:
            return self.creator_info_by_account[account_id]
        result = creator_info()
        result["creator_info"] = {
            **result["creator_info"],
            "creator_nickname": "Creator " + (account_id or "101"),
            "creator_username": "creator_live_" + (account_id or "101"),
        }
        return result

    def prepare(
        self,
        *,
        job_id,
        material,
        source_trim_tail_seconds,
        expected_profile,
    ):
        self.prepare_jobs.append(
            (
                job_id,
                material["source_media_url"],
                source_trim_tail_seconds,
                expected_profile,
            )
        )
        if self.prepare_error is not None:
            raise self.prepare_error
        return {
            "job_id": self.prepare_job_id_override or job_id,
            "content_id": material["content_id"],
            "output_sha256": "a" * 64,
            "output_size": 123456,
            "output_url": "https://cdn.example.com/prepared.mp4",
            "probe": {"duration": self.prepared_duration},
            "profile": self.prepared_profile,
            "status": "ready",
        }

    def publish(self, *, job_id, **kwargs):
        self.publish_jobs.append(job_id)
        self.publish_requests.append(
            {"job_id": job_id, **dict(kwargs)}
        )
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

    def service(
        self,
        gates,
        *,
        configure_settings=True,
        manual_canary=None,
    ):
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
            manual_canary=manual_canary,
            now_fn=self.clock,
            source_trim_tail_seconds=4.333333,
            media_profile_version="tt-post-hevc-720x1280-v2",
        )

    def arm_manual_canary(self, service):
        pool = service.store.list_recurring_materials(
            account_id="101",
            status="available",
            limit=1,
        )[0]
        service.manual_canary = ManualPublishCanary(
            enabled=True,
            acknowledged=True,
            canary_id="test-canary-001",
            expires_at_utc=(
                self.clock.value + timedelta(hours=1)
            ).isoformat().replace("+00:00", "Z"),
            account_id="101",
            pool_id=int(pool["id"]),
            material_id=str(pool["material_id"]),
            content_id=str(pool["content_id"]),
            gpu_job_id=str(pool["gpu_job_id"]),
            output_sha256=str(pool["prepared_output_sha256"]),
            output_size=int(pool["prepared_output_size"]),
            profile=str(pool["preparation_profile"]),
        )
        return pool

    def recurring_material_payload(self, key="tt-post-pool:test:9001"):
        return {
            "idempotency_key": key,
            "source_account_id": "101",
            "material_id": "9001",
            "content_id": "ABCD1234",
            "caption_template": (
                "Watch the full story in the app 🎬\n\n"
                "Drama ID: {{contect_id}}\n\n"
                "Visit my profile → Open the link → "
                "Search the Drama ID → Watch now."
            ),
            "consent": {
                "accepted": True,
                "version": "tt-recurring-post-consent-20260730",
                "accepted_at": self.clock.value.isoformat(),
            },
        }

    def process_one_preparation(self, service):
        claimed = service.preparation_claim(
            {
                "worker_id": "tt-post-prepare-test",
                "lease_seconds": 180,
            }
        )
        self.assertIsNotNone(claimed["item"])
        return service.preparation_process(
            claimed["item"]["id"],
            {"claim_token": claimed["claim_token"]},
        )

    def add_ready(self, service, payload=None):
        service.material_pool_add(
            payload or self.recurring_material_payload()
        )
        processed = self.process_one_preparation(service)
        self.assertEqual(
            processed["item"]["preparation_status"],
            "ready",
        )
        return processed["item"]

    def schedule_payload(self, version=0, enabled=True, publish_time="11:00"):
        return {
            "source_account_id": "101",
            "enabled": enabled,
            "publish_time": publish_time,
            "timezone": "Asia/Shanghai",
            "expected_version": version,
            "consent": {
                "accepted": True,
                "version": "tt-recurring-post-consent-20260730",
                "accepted_at": self.clock.value.isoformat(),
            },
        }

    def test_recurring_material_and_daily_schedule_are_saved_separately(self):
        service = self.service(CLOSED_GATES)
        added = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        self.assertEqual(added["preparation_status"], "queued")
        self.assertFalse(added["publish_ready"])
        self.assertEqual(added["source_account_id"], "101")
        self.assertEqual(added["content_id"], "ABCD1234")
        self.assertIn("Drama ID: ABCD1234", added["caption_text"])
        self.assertEqual(self.gpu.prepare_jobs, [])
        self.assertEqual(self.gpu.creator_info_calls, [])
        self.assertEqual(
            service.material_pool_list(
                {"source_account_id": ["101"]}
            )["summary"]["queued"],
            1,
        )
        prepared = self.process_one_preparation(service)["item"]
        self.assertEqual(prepared["preparation_status"], "ready")

        saved = service.schedule_save(self.schedule_payload())["item"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["publish_time"], "11:00")
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["available_material_count"], 1)
        fetched = service.schedule_get(
            {"source_account_id": ["101"]}
        )["item"]
        self.assertEqual(fetched["version"], 1)
        self.assertEqual(fetched["publish_times"], ["11:00"])
        self.assertTrue(fetched["next_run_at"])

    def test_recurring_pool_exact_retry_is_idempotent(self):
        service = self.service(CLOSED_GATES)
        first = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        second = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            service.store.count_recurring_materials(account_id="101"),
            0,
        )
        self.assertEqual(len(service.store.list_material_intakes()), 1)
        self.process_one_preparation(service)
        self.assertEqual(
            service.store.count_recurring_materials(account_id="101"),
            1,
        )

    def test_daily_schedule_version_rejects_fractional_json_number(self):
        service = self.service(CLOSED_GATES)
        payload = self.schedule_payload()
        payload["expected_version"] = 0.9
        with self.assertRaises(TTPostServiceError) as caught:
            service.schedule_save(payload)
        self.assertEqual(
            caught.exception.code,
            "tt_post_schedule_version_required",
        )
        self.assertEqual(
            service.store.get_daily_schedule("101")["version"],
            0,
        )

    def test_disable_schedule_skips_all_publish_dependencies(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        saved = service.store.save_daily_schedule(
            "101",
            ["11:00"],
            enabled=True,
            expected_version=0,
            consent_version="tt-recurring-post-consent-20260730",
            consented_at=self.clock.value.isoformat(),
        )
        request = {
            "source_account_id": "101",
            "enabled": False,
            "expected_version": saved["version"],
            # Known activation-only fields remain wire-compatible with old
            # clients, but the stop lane must not inspect or depend on them.
            "timezone": "Not/A-Timezone",
            "publish_time": "not-a-time",
            "publish_times": {"not": "a-list"},
            "consent": {"accepted": False},
        }

        with (
            mock.patch.object(
                self.accounts,
                "get_public_account",
                side_effect=AssertionError("account repository must not run"),
            ),
            mock.patch.object(
                service,
                "creator_info",
                side_effect=AssertionError("creator_info must not run"),
            ),
        ):
            disabled = service.schedule_save(request)["item"]

        self.assertFalse(disabled["enabled"])
        self.assertEqual(saved["version"] + 1, disabled["version"])
        self.assertEqual(saved["publish_times"], disabled["publish_times"])
        self.assertEqual(
            saved["consent_version"],
            disabled["consent_version"],
        )
        self.assertEqual(0, disabled["available_material_count"])
        self.assertEqual([], self.gpu.creator_info_calls)

    def test_disable_missing_schedule_is_dependency_free_noop(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        with (
            mock.patch.object(
                self.accounts,
                "get_public_account",
                side_effect=AssertionError("account repository must not run"),
            ),
            mock.patch.object(
                service,
                "creator_info",
                side_effect=AssertionError("creator_info must not run"),
            ),
        ):
            disabled = service.schedule_save(
                {
                    "source_account_id": "101",
                    "enabled": False,
                    "expected_version": 0,
                }
            )["item"]

        self.assertFalse(disabled["enabled"])
        self.assertEqual(0, disabled["version"])
        self.assertFalse(disabled["user_consent"])
        self.assertEqual([], service.store.list_daily_schedules())

    def test_enable_schedule_still_requires_explicit_consent(self):
        service = self.service(CLOSED_GATES)
        payload = self.schedule_payload()
        payload.pop("consent")

        with self.assertRaises(TTPostServiceError) as caught:
            service.schedule_save(payload)

        self.assertEqual("tt_post_consent_required", caught.exception.code)
        self.assertEqual(0, service.store.get_daily_schedule("101")["version"])

    def test_daily_schedule_time_is_unique_and_released_on_change_or_disable(self):
        service = self.service(CLOSED_GATES)
        self.accounts.add_account("102")
        service.store.save_account_settings(
            "102",
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
        first = service.schedule_save(self.schedule_payload())["item"]
        replay = self.schedule_payload(version=first["version"])
        replayed = service.schedule_save(replay)["item"]
        self.assertEqual(replayed["publish_time"], "11:00")

        second = self.schedule_payload()
        second["source_account_id"] = "102"
        with self.assertRaises(TTPostError) as conflict:
            service.schedule_save(second)
        self.assertEqual(
            conflict.exception.code,
            "tt_post_schedule_time_conflict",
        )

        changed = self.schedule_payload(
            version=replayed["version"],
            publish_time="11:01",
        )
        service.schedule_save(changed)
        second_saved = service.schedule_save(second)["item"]
        self.assertEqual(second_saved["publish_time"], "11:00")

        second_disabled = dict(second)
        second_disabled.update(
            {
                "enabled": False,
                "expected_version": second_saved["version"],
            }
        )
        service.schedule_save(second_disabled)
        changed_back = self.schedule_payload(
            version=changed["expected_version"] + 1,
            publish_time="11:00",
        )
        self.assertEqual(
            service.schedule_save(changed_back)["item"]["publish_time"],
            "11:00",
        )

    def test_daily_schedule_time_conflict_is_atomic_across_store_instances(self):
        db_path = Path(self.temp.name) / "concurrent-schedule.sqlite3"
        stores = [
            TTPostStore(db_path, now_fn=self.clock),
            TTPostStore(db_path, now_fn=self.clock),
        ]
        barrier = threading.Barrier(2)
        outcomes = []

        def save(index, account_id):
            barrier.wait()
            try:
                stores[index].save_daily_schedule(
                    account_id,
                    ["11:00"],
                    enabled=True,
                    expected_version=0,
                    consent_version="tt-recurring-post-consent-20260730",
                    consented_at=self.clock.value.isoformat(),
                )
            except TTPostError as exc:
                outcomes.append(("error", exc.code))
            else:
                outcomes.append(("saved", account_id))

        workers = [
            threading.Thread(target=save, args=(0, "101")),
            threading.Thread(target=save, args=(1, "102")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(
            sorted(kind for kind, _value in outcomes),
            ["error", "saved"],
        )
        self.assertIn(
            ("error", "tt_post_schedule_time_conflict"),
            outcomes,
        )

    def test_closed_gates_manual_publish_does_not_consume_material(self):
        service = self.service(CLOSED_GATES)
        self.add_ready(service)
        with self.assertRaises(TTPostServiceError) as caught:
            service.run_now(
                {
                    "source_account_id": "101",
                    "idempotency_key": "tt-post-manual:test-closed",
                }
            )
        self.assertEqual(
            caught.exception.code,
            "tt_post_live_gates_closed",
        )
        item = service.store.list_recurring_materials(
            account_id="101"
        )[0]
        self.assertEqual(item["status"], "available")
        self.assertEqual(service.store.list_queues(), [])
        self.assertEqual(self.gpu.publish_jobs, [])

    def test_manual_publish_waits_for_ready_and_same_key_then_succeeds_once(self):
        service = self.service(OPEN_GATES)
        queued = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        request = {
            "source_account_id": "101",
            "idempotency_key": "tt-post-manual:wait-for-ready",
        }

        with self.assertRaises(TTPostError) as not_ready:
            service.run_now(request)
        self.assertEqual(
            "tt_post_recurring_pool_empty",
            not_ready.exception.code,
        )
        self.assertEqual("queued", service.store.get_material_intake(queued["id"])["status"])
        self.assertEqual([], service.store.list_queues())
        run_key = "tt-post:manual:v1:101:%s" % hashlib.sha256(
            request["idempotency_key"].encode("utf-8")
        ).hexdigest()[:32]
        with self.assertRaises(TTPostError) as missing_run:
            service.store.get_recurring_run_by_key(run_key)
        self.assertEqual(
            "tt_post_schedule_run_not_found",
            missing_run.exception.code,
        )

        self.process_one_preparation(service)
        submitted = service.run_now(request)["item"]

        self.assertTrue(submitted["queue_id"])
        self.assertEqual(1, len(service.store.list_queues()))
        self.assertEqual("reserved", submitted["pool_item"]["status"])

    def test_manual_canary_forces_private_one_shot_with_global_gates_closed(self):
        service = self.service(CLOSED_GATES)
        self.add_ready(service)
        current_settings = service.store.get_account_settings("101")
        service.store.save_account_settings(
            "101",
            TTPostAccountSettings.from_mapping(
                {
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "allow_comment": True,
                    "allow_duet": True,
                    "allow_stitch": True,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": False,
                    "is_aigc": True,
                }
            ),
            expected_version=current_settings["version"],
        )
        self.gpu.creator_info_override = creator_info()
        self.gpu.creator_info_override["creator_info"][
            "privacy_level_options"
        ] = ["SELF_ONLY"]
        self.arm_manual_canary(service)
        before = service.schedule_get(
            {"source_account_id": ["101"]}
        )["item"]
        self.assertTrue(before["manual_canary_ready"])
        self.assertTrue(before["can_publish_now"])
        self.assertFalse(service.gates.is_open)

        run = service.run_now(
            {
                "source_account_id": "101",
                "idempotency_key": "tt-post-manual:test-canary",
            }
        )["item"]
        queue = service.store.get_queue(run["queue_id"])
        self.assertEqual(queue["privacy_level"], "SELF_ONLY")
        self.assertFalse(queue["allow_comment"])
        self.assertFalse(queue["allow_duet"])
        self.assertFalse(queue["allow_stitch"])
        self.assertFalse(queue["brand_content_toggle"])
        self.assertFalse(queue["brand_organic_toggle"])
        self.assertIn(
            "tt-post:manual-canary:v1:test-canary-001:101:",
            queue["idempotency_key"],
        )

        claimed = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": DEFAULT_GRACE_SECONDS,
                "limit": 1,
            }
        )["items"][0]
        self.assertIn("claim_token", claimed)
        self.assertTrue(claimed["queue"]["manual_canary"])
        result = service.publish_claimed(
            queue["id"],
            claimed["claim_token"],
        )["item"]
        self.assertEqual(result["status"], "reconciling")
        self.assertEqual(len(self.gpu.publish_requests), 1)
        request = self.gpu.publish_requests[0]
        self.assertTrue(request["manual_canary"])
        self.assertEqual(
            request["manual_canary_id"],
            "test-canary-001",
        )
        self.assertEqual(request["queue"]["privacy_level"], "SELF_ONLY")
        self.assertFalse(
            service.schedule_get(
                {"source_account_id": ["101"]}
            )["item"]["manual_canary_ready"]
        )

    def test_manual_canary_never_authorizes_an_automatic_due_slot(self):
        service = self.service(CLOSED_GATES)
        self.add_ready(service)
        service.schedule_save(
            self.schedule_payload(publish_time="11:00")
        )
        self.arm_manual_canary(service)
        schedule = service.schedule_get(
            {"source_account_id": ["101"]}
        )["item"]
        self.assertFalse(schedule["manual_canary_ready"])
        self.assertFalse(schedule["can_publish_now"])
        due = service.schedules_due({})["items"]
        self.assertEqual(len(due), 1)
        self.assertEqual(
            due[0]["error_code"],
            "tt_post_live_gates_closed",
        )
        self.assertEqual(service.store.list_queues(), [])
        self.assertEqual(
            service.store.list_recurring_materials(
                account_id="101"
            )[0]["status"],
            "available",
        )

    def test_manual_canary_target_mismatch_releases_preflight_material(self):
        service = self.service(CLOSED_GATES)
        self.add_ready(service)
        self.arm_manual_canary(service)
        service.manual_canary = replace(
            service.manual_canary,
            material_id="9999",
        )
        with self.assertRaises(TTPostServiceError) as caught:
            service.run_now(
                {
                    "source_account_id": "101",
                    "idempotency_key": "tt-post-manual:test-wrong-target",
                }
            )
        self.assertEqual(
            caught.exception.code,
            "tt_post_manual_canary_target_mismatch",
        )
        self.assertEqual(service.store.list_queues(), [])
        self.assertEqual(
            service.store.list_recurring_materials(
                account_id="101"
            )[0]["status"],
            "available",
        )
        self.assertEqual(self.gpu.publish_jobs, [])

    def test_manual_publish_is_idempotent_and_does_not_change_daily_schedule(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        saved = service.schedule_save(
            self.schedule_payload(publish_time="12:00")
        )["item"]
        request = {
            "source_account_id": "101",
            "idempotency_key": "tt-post-manual:test-open",
        }
        first = service.run_now(request)["item"]
        self.clock.value += timedelta(minutes=2)
        second = service.run_now(request)["item"]
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["queue_id"], second["queue_id"])
        self.assertEqual(len(service.store.list_queues()), 1)
        self.assertEqual(
            service.store.list_recurring_materials(
                account_id="101"
            )[0]["status"],
            "reserved",
        )
        current = service.schedule_get(
            {"source_account_id": ["101"]}
        )["item"]
        self.assertEqual(current["version"], saved["version"])
        self.assertEqual(current["publish_time"], "12:00")

    def test_daily_due_is_slot_idempotent_and_fifo(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        self.clock.value = datetime(2026, 7, 29, 3, 0, 20, tzinfo=UTC)
        service.schedule_save(
            self.schedule_payload(publish_time="11:00")
        )
        first = service.schedules_due({})["items"]
        second = service.schedules_due({})["items"]
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(service.store.list_queues()), 1)

    def test_daily_due_limit_processes_only_one_new_account_per_call(self):
        service = self.service(OPEN_GATES)
        self.accounts.add_account("102")
        service.store.save_account_settings(
            "102",
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
        self.add_ready(service)
        second_material = self.recurring_material_payload(
            "tt-post-pool:test:9002"
        )
        second_material.update(
            {
                "source_account_id": "102",
                "material_id": "9002",
            }
        )
        self.add_ready(service, second_material)
        self.clock.value = datetime(2026, 7, 29, 3, 0, 20, tzinfo=UTC)
        service.schedule_save(self.schedule_payload(publish_time="11:00"))
        second_schedule = self.schedule_payload(publish_time="10:59")
        second_schedule["source_account_id"] = "102"
        service.schedule_save(second_schedule)

        first_result = service.schedules_due({"limit": 1})
        first = first_result["items"]
        second_result = service.schedules_due({"limit": 1})
        second = second_result["items"]

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            {first[0]["source_account_id"], second[0]["source_account_id"]},
            {"101", "102"},
        )
        self.assertEqual(first_result["deferred_count"], 1)
        self.assertEqual(
            first_result["oldest_deferred_at_utc"],
            "2026-07-29T03:00:00Z",
        )
        self.assertEqual(second_result["deferred_count"], 0)
        self.assertEqual(second_result["oldest_deferred_at_utc"], "")
        self.assertEqual(len(service.store.list_queues()), 2)

    def test_material_pool_rejects_media_over_current_account_limit(self):
        service = self.service(OPEN_GATES)
        self.gpu.prepared_duration = 700
        queued = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        self.assertEqual(queued["preparation_status"], "queued")
        processed = self.process_one_preparation(service)
        self.assertEqual(
            processed["processing_error"]["code"],
            "tt_prepared_media_duration_invalid",
        )
        self.assertEqual(
            processed["item"]["preparation_status"],
            "failed",
        )
        self.assertEqual(
            service.store.count_recurring_materials(account_id="101"),
            0,
        )

    def test_transient_prepare_failure_is_persisted_for_background_retry(self):
        service = self.service(OPEN_GATES)
        self.gpu.prepare_error = GPUClientError(
            "prepare_timeout",
            "temporary GPU timeout",
            502,
            publish_was_not_created=True,
        )
        queued = service.material_pool_add(
            self.recurring_material_payload()
        )["item"]
        self.assertEqual(queued["preparation_status"], "queued")
        processed = self.process_one_preparation(service)
        self.assertTrue(processed["processing_error"]["retryable"])
        self.assertEqual(
            processed["item"]["preparation_status"],
            "retry_wait",
        )
        self.assertTrue(processed["item"]["next_attempt_at_utc"])
        self.assertEqual(
            service.store.count_recurring_materials(account_id="101"),
            0,
        )

    def test_live_creator_duration_failure_releases_fifo_material(self):
        service = self.service(OPEN_GATES)
        self.gpu.prepared_duration = 500
        self.add_ready(service)
        changed = creator_info()
        changed["creator_info"]["max_video_post_duration_sec"] = 300
        self.gpu.creator_info_override = changed
        with self.assertRaises(TTPostServiceError) as caught:
            service.run_now(
                {
                    "source_account_id": "101",
                    "idempotency_key": "tt-post-manual:too-long",
                }
            )
        self.assertEqual(
            caught.exception.code,
            "tt_prepared_media_duration_invalid",
        )
        item = service.store.list_recurring_materials(
            account_id="101"
        )[0]
        self.assertEqual(item["status"], "available")
        self.assertEqual(service.store.list_queues(), [])

    def test_manual_retry_recovers_queue_committed_before_run_binding(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        request = {
            "source_account_id": "101",
            "idempotency_key": "tt-post-manual:recover-bind-gap",
        }
        with mock.patch.object(
            service.store,
            "bind_recurring_queue",
            side_effect=TTPostError(
                "synthetic_bind_failure",
                "模拟进程在队列提交后中断",
                500,
            ),
        ):
            with self.assertRaises(TTPostError) as caught:
                service.run_now(request)
        self.assertEqual(caught.exception.code, "synthetic_bind_failure")
        queues = service.store.list_queues()
        self.assertEqual(len(queues), 1)

        with mock.patch.object(
            service.store,
            "bind_recurring_queue",
            side_effect=TTPostError(
                "synthetic_bind_failure_again",
                "模拟恢复绑定再次中断",
                500,
            ),
        ):
            with self.assertRaises(TTPostError) as caught_again:
                service.run_now(request)
        self.assertEqual(
            caught_again.exception.code,
            "synthetic_bind_failure_again",
        )
        pending = service.store.get_recurring_run_by_key(
            service.store.list_claimed_unbound_recurring_runs()[0]["run_key"]
        )
        self.assertEqual(pending["status"], "claimed")
        self.assertEqual(pending["pool_item"]["status"], "reserved")

        recovered = service.run_now(request)["item"]
        self.assertEqual(recovered["queue_id"], queues[0]["id"])
        self.assertEqual(
            service.store.list_recurring_materials(
                account_id="101"
            )[0]["status"],
            "reserved",
        )

    def test_daily_runner_recovers_claim_before_freeze_across_minutes(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        service.schedule_save(
            self.schedule_payload(publish_time="11:00")
        )
        with mock.patch.object(
            service.store,
            "freeze_queue",
            side_effect=RuntimeError("simulated process exit"),
        ):
            with self.assertRaises(RuntimeError):
                service.schedules_due({})
        pending = service.store.get_recurring_run_by_key(
            "tt-post:auto:v1:101:2026-07-29:1100"
        )
        self.assertEqual(pending["status"], "claimed")
        self.assertIsNone(pending["queue_id"])
        self.assertEqual(
            service.store.list_claimed_unbound_recurring_runs(),
            [],
        )
        self.assertEqual(service.store.list_queues(), [])

        self.clock.value += timedelta(seconds=121)
        recovered = service.schedules_due({})["items"]
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["queue_id"])
        self.assertEqual(len(service.store.list_queues()), 1)
        self.assertEqual(
            service.store.list_claimed_unbound_recurring_runs(),
            [],
        )

    def test_claim_before_freeze_recovers_at_exact_600_second_boundary(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        service.schedule_save(
            self.schedule_payload(publish_time="11:00")
        )
        with mock.patch.object(
            service.store,
            "freeze_queue",
            side_effect=RuntimeError("simulated process exit"),
        ):
            with self.assertRaises(RuntimeError):
                service.schedules_due({})

        self.clock.value += timedelta(seconds=600)
        recovered = service.schedules_due({})["items"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual("scheduled", recovered[0]["status"])
        self.assertTrue(recovered[0]["queue_id"])
        self.assertEqual(len(service.store.list_queues()), 1)

    def test_claim_without_queue_releases_only_after_600_seconds(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        service.schedule_save(
            self.schedule_payload(publish_time="11:00")
        )
        with mock.patch.object(
            service.store,
            "freeze_queue",
            side_effect=RuntimeError("simulated process exit"),
        ):
            with self.assertRaises(RuntimeError):
                service.schedules_due({})

        self.clock.value += timedelta(seconds=601)
        released = service.schedules_due({})["items"]
        self.assertEqual(len(released), 1)
        self.assertEqual("preflight_failed", released[0]["status"])
        self.assertEqual(
            "tt_post_recurring_run_expired",
            released[0]["error_code"],
        )
        self.assertIsNone(released[0]["queue_id"])
        self.assertEqual("available", released[0]["pool_item"]["status"])
        self.assertEqual(service.store.list_queues(), [])

    def test_manual_crash_retry_uses_original_frozen_minute(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        request = {
            "source_account_id": "101",
            "idempotency_key": "tt-post-manual:frozen-minute",
        }
        with mock.patch.object(
            service.store,
            "freeze_queue",
            side_effect=RuntimeError("simulated process exit"),
        ):
            with self.assertRaises(RuntimeError):
                service.run_now(request)

        self.clock.value += timedelta(seconds=121)
        original_creator_info = service.creator_info

        def slow_creator_info(payload):
            self.clock.value += timedelta(seconds=900)
            return original_creator_info(payload)

        service.creator_info = slow_creator_info
        recovered = service.run_now(request)["item"]
        queue = service.store.get_queue(recovered["queue_id"])
        self.assertEqual(
            "2026-07-29T03:00:00Z",
            recovered["scheduled_at_utc"],
        )
        self.assertEqual(
            recovered["scheduled_at_utc"],
            queue["scheduled_at_utc"],
        )
        self.assertEqual(len(service.store.list_queues()), 1)

    def test_expired_owner_cannot_freeze_after_new_owner_preflight_release(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        request = {
            "source_account_id": "101",
            "idempotency_key": "tt-post-manual:execution-fence-race",
        }
        freeze_entered = threading.Event()
        allow_stale_freeze = threading.Event()
        results = {}
        original_freeze = service.store.freeze_queue
        original_creator_info = service.creator_info
        creator_calls = {"count": 0}
        creator_lock = threading.Lock()

        def creator_info(payload):
            with creator_lock:
                creator_calls["count"] += 1
                call_number = creator_calls["count"]
            if call_number == 2:
                raise TTPostServiceError(
                    "synthetic_second_preflight_failure",
                    "synthetic",
                    409,
                )
            return original_creator_info(payload)

        def blocked_freeze(*args, **kwargs):
            freeze_entered.set()
            if not allow_stale_freeze.wait(5):
                raise RuntimeError("test freeze wait timed out")
            return original_freeze(*args, **kwargs)

        def first_request():
            try:
                results["first"] = service.run_now(request)
            except Exception as exc:  # noqa: BLE001 - assert exact fence below
                results["first_error"] = exc

        with (
            mock.patch.object(
                service,
                "creator_info",
                side_effect=creator_info,
            ),
            mock.patch.object(
                service.store,
                "freeze_queue",
                side_effect=blocked_freeze,
            ),
        ):
            worker = threading.Thread(target=first_request)
            worker.start()
            self.assertTrue(freeze_entered.wait(5))
            self.clock.value += timedelta(seconds=121)
            try:
                with self.assertRaises(TTPostServiceError) as second:
                    service.run_now(request)
                self.assertEqual(
                    "synthetic_second_preflight_failure",
                    second.exception.code,
                )
            finally:
                allow_stale_freeze.set()
                worker.join(5)

        self.assertFalse(worker.is_alive())
        first_error = results.get("first_error")
        self.assertIsInstance(first_error, TTPostError)
        self.assertEqual(
            "tt_post_recurring_execution_invalid",
            first_error.code,
        )
        run_key = "tt-post:manual:v1:101:%s" % (
            hashlib.sha256(
                request["idempotency_key"].encode("utf-8")
            ).hexdigest()[:32]
        )
        run = service.store.get_recurring_run_by_key(run_key)
        self.assertEqual("preflight_failed", run["status"])
        self.assertIsNone(run["queue_id"])
        self.assertEqual("available", run["pool_item"]["status"])
        self.assertEqual(service.store.list_queues(), [])

    def test_daily_slot_retries_within_grace_after_manual_account_lock(self):
        service = self.service(OPEN_GATES)
        self.add_ready(service)
        self.materials.source_url = "https://cdn.example.com/source-b.mp4"
        second_payload = self.recurring_material_payload(
            "tt-post-pool:test:9002"
        )
        second_payload["material_id"] = "9002"
        self.add_ready(service, second_payload)
        service.schedule_save(
            self.schedule_payload(publish_time="11:01")
        )
        manual = service.run_now(
            {
                "source_account_id": "101",
                "idempotency_key": "tt-post-manual:before-daily-slot",
            }
        )["item"]

        self.clock.value += timedelta(minutes=1)
        blocked = service.schedules_due({})["items"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(
            blocked[0]["error_code"],
            "tt_post_account_publish_busy",
        )
        service.queue_cancel(manual["queue_id"], {})

        self.clock.value += timedelta(minutes=1)
        retried = service.schedules_due({})["items"]
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["trigger_type"], "auto")
        self.assertEqual(retried[0]["material_id"], "9002")
        self.assertTrue(retried[0]["queue_id"])

    def test_accounts_expose_configuration_state_without_credentials(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        initial_result = service.accounts()
        self.assertTrue(initial_result["account_source_available"])
        self.assertNotIn("warning", initial_result)
        initial = initial_result["items"][0]
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

    def test_accounts_add_generic_placeholders_for_unavailable_schedules(self):
        service = self.service(CLOSED_GATES)
        disabled_account = self.accounts.add_account("202")
        disabled_account["disable_publish"] = 1
        disabled_account["publish_eligible"] = False
        expired_account = self.accounts.add_account("203")
        expired_account["token_status"] = 1
        expired_account["publish_eligible"] = False
        for index, account_id in enumerate(("201", "202", "203"), start=1):
            service.store.save_daily_schedule(
                account_id,
                ["12:%02d" % index],
                enabled=True,
                expected_version=0,
                consent_version="tt-recurring-post-consent-20260730",
                consented_at=self.clock.value.isoformat(),
            )

        with mock.patch.object(
            self.accounts,
            "list_public_accounts",
            return_value=[dict(self.accounts.account)],
        ):
            items = service.accounts()["items"]

        placeholders = {
            item["source_account_id"]: item
            for item in items
            if item.get("management_only")
        }
        self.assertEqual({"201", "202", "203"}, set(placeholders))
        for account_id, item in placeholders.items():
            self.assertEqual(account_id, item["account_id"])
            self.assertFalse(item["publish_eligible"])
            self.assertEqual("unavailable", item["status"])
            self.assertIn("只能查看并停用", item["eligibility_reason"])
            self.assertEqual(
                {"configured": False},
                item["account_settings"],
            )
            for forbidden in (
                "username",
                "display_name",
                "account_name",
                "account_link",
                "external_account_id",
                "main_account_id",
                "token_status",
                "account_status",
                "access_token",
            ):
                self.assertNotIn(forbidden, item)
        rendered = json.dumps(placeholders, ensure_ascii=False)
        self.assertNotIn("creator_202", rendered)
        self.assertNotIn("creator_203", rendered)

    def test_unavailable_scheduled_account_can_read_and_disable_but_not_enable(self):
        service = self.service(CLOSED_GATES)
        saved = service.schedule_save(
            self.schedule_payload(publish_time="12:10")
        )["item"]
        self.accounts.accounts.pop("101")
        self.gpu.creator_info_calls.clear()

        placeholder = service.accounts()["items"][0]
        self.assertEqual("101", placeholder["source_account_id"])
        self.assertFalse(placeholder["publish_eligible"])
        self.assertTrue(placeholder["management_only"])
        fetched = service.schedule_get(
            {"source_account_id": ["101"]}
        )["item"]
        self.assertEqual(saved["version"], fetched["version"])
        self.assertTrue(fetched["enabled"])

        disabled = service.schedule_save(
            {
                "source_account_id": "101",
                "enabled": False,
                "expected_version": fetched["version"],
            }
        )["item"]
        self.assertFalse(disabled["enabled"])
        self.assertEqual(saved["version"] + 1, disabled["version"])

        enable_request = self.schedule_payload(
            version=disabled["version"],
            publish_time="12:10",
        )
        with self.assertRaises(TTPostServiceError) as unavailable:
            service.schedule_save(enable_request)
        self.assertEqual("tt_account_not_found", unavailable.exception.code)
        self.assertEqual([], self.gpu.creator_info_calls)
        unchanged = service.store.get_daily_schedule("101")
        self.assertFalse(unchanged["enabled"])
        self.assertEqual(disabled["version"], unchanged["version"])

    def test_schedule_get_default_does_not_require_a_publishable_account(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        with mock.patch.object(
            self.accounts,
            "get_public_account",
            side_effect=AssertionError("schedule read must remain local"),
        ):
            item = service.schedule_get(
                {"source_account_id": ["999"]}
            )["item"]

        self.assertEqual("999", item["source_account_id"])
        self.assertFalse(item["enabled"])
        self.assertEqual(0, item["version"])
        self.assertEqual([], item["publish_times"])

    def test_account_source_failure_returns_only_local_schedules_and_can_stop(self):
        service = self.service(CLOSED_GATES)
        saved = service.schedule_save(
            self.schedule_payload(publish_time="12:20")
        )["item"]
        source_error = AccountSourceError(
            "tt_account_source_unavailable",
            "sensitive upstream detail token=must-not-leak",
            503,
        )

        with (
            mock.patch.object(
                self.accounts,
                "list_public_accounts",
                side_effect=source_error,
            ),
            mock.patch.object(
                self.accounts,
                "get_public_account",
                side_effect=source_error,
            ),
        ):
            result = service.accounts()
            fetched = service.schedule_get(
                {"source_account_id": ["101"]}
            )["item"]
            disabled = service.schedule_save(
                {
                    "source_account_id": "101",
                    "enabled": False,
                    "expected_version": fetched["version"],
                }
            )["item"]

        self.assertFalse(result["account_source_available"])
        self.assertIn("仅显示本地已有排期", result["warning"])
        self.assertEqual(1, len(result["items"]))
        placeholder = result["items"][0]
        self.assertEqual("101", placeholder["source_account_id"])
        self.assertFalse(placeholder["publish_eligible"])
        self.assertTrue(placeholder["management_only"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sensitive upstream detail", rendered)
        self.assertNotIn("must-not-leak", rendered)
        self.assertTrue(fetched["enabled"])
        self.assertEqual(saved["version"], fetched["version"])
        self.assertFalse(disabled["enabled"])
        self.assertEqual(saved["version"] + 1, disabled["version"])

    def test_account_source_failure_without_schedule_is_empty_and_cannot_enable(self):
        service = self.service(CLOSED_GATES)
        source_error = AccountSourceError(
            "tt_account_source_unavailable",
            "sensitive database exception",
            503,
        )

        with (
            mock.patch.object(
                self.accounts,
                "list_public_accounts",
                side_effect=source_error,
            ),
            mock.patch.object(
                self.accounts,
                "get_public_account",
                side_effect=source_error,
            ),
        ):
            result = service.accounts()
            with self.assertRaises(AccountSourceError) as unavailable:
                service.schedule_save(
                    self.schedule_payload(publish_time="12:30")
                )

        self.assertFalse(result["account_source_available"])
        self.assertEqual([], result["items"])
        self.assertIn("所有账号均不可发布", result["warning"])
        self.assertEqual(
            "tt_account_source_unavailable",
            unavailable.exception.code,
        )
        self.assertEqual([], self.gpu.creator_info_calls)
        self.assertEqual([], service.store.list_daily_schedules())
        self.assertEqual(0, service.store.get_daily_schedule("101")["version"])

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

    def test_batch_creator_info_returns_safe_common_capability_intersection(self):
        self.accounts.add_account("102")
        service = self.service(CLOSED_GATES, configure_settings=False)
        first = creator_info()
        first["creator_info"]["privacy_level_options"] = [
            "SELF_ONLY",
            "PUBLIC_TO_EVERYONE",
        ]
        second = creator_info()
        second["creator_info"].update(
            {
                "creator_nickname": "Creator 102",
                "creator_username": "creator_live_102",
                "privacy_level_options": [
                    "MUTUAL_FOLLOW_FRIENDS",
                    "PUBLIC_TO_EVERYONE",
                ],
                "duet_disabled": True,
                "max_video_post_duration_sec": 300,
            }
        )
        self.gpu.creator_info_by_account = {"101": first, "102": second}

        result = service.account_settings_batch_creator_info(
            {"source_account_ids": ["101", "102"]}
        )
        self.assertEqual(
            [item["source_account_id"] for item in result["items"]],
            ["101", "102"],
        )
        common = result["common_capabilities"]
        self.assertEqual(
            common["privacy_level_options"],
            ["PUBLIC_TO_EVERYONE"],
        )
        self.assertFalse(common["comment_disabled"])
        self.assertTrue(common["duet_disabled"])
        self.assertFalse(common["stitch_disabled"])
        self.assertEqual(common["max_video_post_duration_sec"], 300)
        self.assertEqual(sorted(self.gpu.creator_info_calls), ["101", "102"])
        self.assertNotIn("access_token", json.dumps(result))

    def test_batch_target_validation_happens_before_creator_info_calls(self):
        service = self.service(CLOSED_GATES, configure_settings=False)
        for account_ids in (
            [],
            ["101", "101"],
            [str(index) for index in range(1, 52)],
        ):
            with self.assertRaises(TTPostServiceError) as caught:
                service.account_settings_batch_creator_info(
                    {"source_account_ids": account_ids}
                )
            self.assertEqual("invalid_batch_targets", caught.exception.code)
        self.assertEqual(self.gpu.creator_info_calls, [])

    def test_batch_save_updates_mixed_versions_atomically(self):
        self.accounts.add_account("102")
        service = self.service(CLOSED_GATES, configure_settings=False)
        service.store.save_account_settings(
            "101",
            TTPostAccountSettings.from_mapping(
                {
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": False,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": False,
                    "is_aigc": False,
                }
            ),
            expected_version=0,
        )
        result = service.account_settings_batch_save(
            {
                "targets": [
                    {"source_account_id": "101", "expected_version": 1},
                    {"source_account_id": "102", "expected_version": 0},
                ],
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "allow_duet": True,
                "allow_stitch": True,
                "commercial_disclosure": False,
                "brand_organic_toggle": False,
                "brand_content_toggle": False,
                "is_aigc": False,
            }
        )
        self.assertEqual(result["saved_count"], 2)
        self.assertEqual(
            [item["account_settings"]["version"] for item in result["items"]],
            [2, 1],
        )
        self.assertTrue(
            service.store.get_account_settings("101")["allow_comment"]
        )
        self.assertTrue(
            service.store.get_account_settings("102")["allow_duet"]
        )

    def test_batch_save_capability_failure_writes_nothing(self):
        self.accounts.add_account("102")
        service = self.service(CLOSED_GATES, configure_settings=False)
        second = creator_info()
        second["creator_info"]["duet_disabled"] = True
        self.gpu.creator_info_by_account["102"] = second
        with self.assertRaises(TTPostServiceError) as caught:
            service.account_settings_batch_save(
                {
                    "targets": [
                        {"source_account_id": "101", "expected_version": 0},
                        {"source_account_id": "102", "expected_version": 0},
                    ],
                    "privacy_level": "SELF_ONLY",
                    "allow_comment": False,
                    "allow_duet": True,
                    "allow_stitch": False,
                    "commercial_disclosure": False,
                    "brand_organic_toggle": False,
                    "brand_content_toggle": False,
                    "is_aigc": False,
                }
            )
        self.assertEqual("tt_interaction_not_allowed", caught.exception.code)
        self.assertIsNone(service.store.get_account_settings("101"))
        self.assertIsNone(service.store.get_account_settings("102"))

    def test_batch_save_version_conflict_rolls_back_all_targets(self):
        self.accounts.add_account("102")
        service = self.service(CLOSED_GATES, configure_settings=False)
        for account_id in ("101", "102"):
            service.store.save_account_settings(
                account_id,
                TTPostAccountSettings.from_mapping(
                    {
                        "privacy_level": "SELF_ONLY",
                        "allow_comment": False,
                        "allow_duet": False,
                        "allow_stitch": False,
                        "brand_content_toggle": False,
                        "brand_organic_toggle": False,
                        "is_aigc": False,
                    }
                ),
                expected_version=0,
            )
        before = {
            account_id: service.store.get_account_settings(account_id)
            for account_id in ("101", "102")
        }
        with self.assertRaises(TTPostError) as caught:
            service.account_settings_batch_save(
                {
                    "targets": [
                        {"source_account_id": "101", "expected_version": 1},
                        {"source_account_id": "102", "expected_version": 0},
                    ],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "allow_comment": True,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "commercial_disclosure": False,
                    "brand_organic_toggle": False,
                    "brand_content_toggle": False,
                    "is_aigc": False,
                }
            )
        self.assertEqual(
            "tt_account_settings_version_conflict",
            caught.exception.code,
        )
        self.assertEqual(
            {
                account_id: service.store.get_account_settings(account_id)
                for account_id in ("101", "102")
            },
            before,
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

    def test_preview_only_validates_material_without_gpu_preparation(self):
        service = self.service(CLOSED_GATES)
        first = service.material_preview({"material_id": "9001"})["item"]
        self.assertEqual(first["preparation_status"], "not_started")
        self.assertFalse(first["publish_ready"])
        self.assertEqual(
            first["source_media_url"],
            "https://cdn.example.com/source-a.mp4",
        )
        self.materials.source_url = "https://cdn.example.com/source-b.mp4"
        second = service.material_preview({"material_id": "9001"})["item"]
        self.assertEqual(
            second["source_media_url"],
            "https://cdn.example.com/source-b.mp4",
        )
        self.assertEqual(self.gpu.prepare_jobs, [])
        self.assertEqual(self.gpu.creator_info_calls, [])

    def test_prepare_rejects_gpu_job_identity_mismatch(self):
        service = self.service(CLOSED_GATES)
        service.material_pool_add(self.recurring_material_payload())
        self.gpu.prepare_job_id_override = "ttpost-wrong-job-identity"
        processed = self.process_one_preparation(service)
        self.assertEqual(
            processed["processing_error"]["code"],
            "tt_prepared_media_identity_mismatch",
        )
        self.assertEqual(processed["item"]["preparation_status"], "failed")

    def test_prepare_rejects_gpu_profile_drift(self):
        service = self.service(CLOSED_GATES)
        service.material_pool_add(self.recurring_material_payload())
        self.gpu.prepared_profile = "tt-post-h264-720x1280-v2"
        processed = self.process_one_preparation(service)
        self.assertEqual(
            processed["processing_error"]["code"],
            "tt_prepared_media_profile_mismatch",
        )
        self.assertEqual(processed["item"]["preparation_status"], "failed")

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

    def test_queue_accepts_editable_template_and_renders_real_drama_id(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.pop("caption_text")
        payload["caption_template"] = (
            "Custom copy for this drama\n\n"
            "Drama ID: {{contect_id}}\n\n"
            "Watch now."
        )
        created = service.queue_create(payload)["item"]
        self.assertEqual(
            (
                "Custom copy for this drama\n\n"
                "Drama ID: ABCD1234\n\n"
                "Watch now."
            ),
            created["caption_text"],
        )
        self.assertEqual(payload["caption_template"], created["caption_template"])

    def test_queue_rejects_template_without_drama_placeholder_before_gpu(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.pop("caption_text")
        payload["caption_template"] = "Custom copy without a drama placeholder"
        with self.assertRaises(TTPostError) as caught:
            service.queue_create(payload)
        self.assertEqual("caption_content_id_required", caught.exception.code)
        self.assertEqual(self.gpu.prepare_jobs, [])

    def test_queue_rejects_caption_that_does_not_match_template_render(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload["caption_template"] = "Drama ID: {{contect_id}}\nWatch now."
        payload["caption_text"] = "Drama ID: WRONG\nWatch now."
        with self.assertRaises(TTPostServiceError) as caught:
            service.queue_create(payload)
        self.assertEqual(
            "tt_caption_template_render_mismatch",
            caught.exception.code,
        )
        self.assertEqual(self.gpu.prepare_jobs, [])

    def test_queue_uses_same_deterministic_identity_as_validated_material(self):
        service = self.service(CLOSED_GATES)
        preview = service.material_preview({"material_id": "9001"})["item"]
        expected_job_id = service._preparation_job_id(preview)
        created = service.queue_create(queue_payload(self.clock))["item"]
        self.assertEqual(expected_job_id, created["gpu_job_id"])
        self.assertEqual(
            [expected_job_id],
            [job[0] for job in self.gpu.prepare_jobs],
        )

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

    def test_legacy_caption_replay_does_not_require_stored_template_spelling(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        created = service.queue_create(payload)["item"]
        legacy_caption = payload["caption_text"].replace("\n", "\r\n")
        legacy_template = legacy_caption.replace(
            "ABCD1234",
            "{{content_id}}",
        )
        connection = sqlite3.connect(Path(self.temp.name) / "tt.sqlite3")
        try:
            connection.execute(
                """
                UPDATE tt_post_queue
                SET caption_template=?,caption=?
                WHERE id=?
                """,
                (legacy_template, legacy_caption, created["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        payload["caption_text"] = legacy_caption
        replay = service.queue_create(payload)["item"]
        self.assertEqual(created["id"], replay["id"])
        self.assertEqual(legacy_template, replay["caption_template"])
        self.assertEqual(legacy_caption, replay["caption_text"])
        self.assertEqual(1, len(self.gpu.prepare_jobs))

    def test_explicit_template_change_with_same_key_conflicts(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.pop("caption_text")
        payload["caption_template"] = "Drama ID: {{contect_id}}\nWatch now."
        service.queue_create(payload)
        changed = dict(payload)
        changed["caption_template"] = "Drama ID: {{content_id}}\nWatch now."
        with self.assertRaises(TTPostServiceError) as caught:
            service.queue_create(changed)
        self.assertEqual("tt_post_idempotency_conflict", caught.exception.code)
        self.assertEqual(1, len(self.gpu.prepare_jobs))

    def test_explicit_template_exact_replay_returns_existing_without_gpu(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        payload.pop("caption_text")
        payload["caption_template"] = "Drama ID: {{contect_id}}\nWatch now."
        created = service.queue_create(payload)["item"]
        replay = service.queue_create(dict(payload))["item"]
        self.assertEqual(created["id"], replay["id"])
        self.assertEqual(created["caption_template"], replay["caption_template"])
        self.assertEqual(created["caption_text"], replay["caption_text"])
        self.assertEqual(1, len(self.gpu.prepare_jobs))

    def test_exact_replay_returns_existing_after_original_schedule_is_due(self):
        service = self.service(CLOSED_GATES)
        payload = queue_payload(self.clock)
        created = service.queue_create(payload)["item"]

        self.clock.value += timedelta(minutes=31)
        replay = service.queue_create(dict(payload))["item"]

        self.assertEqual(created["id"], replay["id"])
        self.assertEqual(created["scheduled_at_utc"], replay["scheduled_at_utc"])
        self.assertEqual(1, len(self.gpu.prepare_jobs))

        new_request = dict(payload)
        new_request["idempotency_key"] = "tt-post:expired-new-request"
        with self.assertRaises(TTPostServiceError) as caught:
            service.queue_create(new_request)
        self.assertEqual("tt_schedule_too_soon", caught.exception.code)
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

    def test_publish_renews_claim_across_slow_credentials_and_gpu_calls(self):
        service = self.service(OPEN_GATES)
        created = service.queue_create(
            queue_payload(self.clock, publish_mode="direct_post")
        )["item"]
        self.clock.value += timedelta(minutes=30)
        claim = service.claim_due(
            {
                "worker_id": "tt-post-runner-primary",
                "grace_seconds": DEFAULT_GRACE_SECONDS,
                "limit": 1,
            }
        )["items"][0]
        original_creator_info = self.gpu.creator_info
        original_publish = self.gpu.publish
        original_credentials = service.account_source.publish_credentials

        @contextlib.contextmanager
        def slow_credentials(account_id):
            self.clock.value += timedelta(seconds=100)
            with original_credentials(account_id) as credentials:
                yield credentials

        def slow_creator_info(**kwargs):
            self.clock.value += timedelta(seconds=900)
            return original_creator_info(**kwargs)

        def slow_publish(**kwargs):
            self.clock.value += timedelta(seconds=900)
            return original_publish(**kwargs)

        service.account_source.publish_credentials = slow_credentials
        self.gpu.creator_info = slow_creator_info
        self.gpu.publish = slow_publish
        published = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]

        self.assertEqual(published["status"], "reconciling")
        self.assertEqual(self.gpu.publish_jobs, [created["gpu_job_id"]])

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
            "publish_id": "v_pub_url~v2-1.7668584571734657042",
            "state": "processing",
            "status": {"status": "PROCESSING_DOWNLOAD"},
        }
        recovered = service.manual_reconcile(created["id"])["item"]
        self.assertEqual(recovered["status"], "reconciling")
        self.assertEqual(
            recovered["publish_id"],
            "v_pub_url~v2-1.7668584571734657042",
        )
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        self.assertEqual(self.gpu.reconcile_jobs, [stable_job])
        events = service.events(created["id"])["items"]
        self.assertIn(
            "publish_id_recovered_from_gpu_ledger",
            [item["event_type"] for item in events],
        )

    def test_publish_id_persistence_failure_becomes_unknown_not_publishing(self):
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

        def fail_record_publish_id(*_args, **_kwargs):
            raise sqlite3.OperationalError(
                "injected publish ID persistence failure"
            )

        service.store.record_publish_id = fail_record_publish_id
        unknown = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]

        self.assertEqual("unknown", unknown["status"])
        self.assertTrue(unknown["unknown_outcome"])
        self.assertFalse(unknown["publish_id"])
        self.assertEqual(self.gpu.publish_jobs, [stable_job])
        self.assertEqual(
            [],
            service.claim_due(
                {
                    "worker_id": "tt-post-runner-secondary",
                    "grace_seconds": 600,
                    "limit": 20,
                }
            )["items"],
        )
        events = service.events(created["id"])["items"]
        self.assertIn(
            "publish_outcome_unknown",
            [item["event_type"] for item in events],
        )

    def test_reconcile_required_id_persistence_failure_becomes_unknown(self):
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
            "tt_publish_reconcile_required",
            "GPU ledger already has a publish ID",
            409,
            details={
                "publish_id": "v_pub_url~v2-1.7668584571734657042",
            },
        )

        def fail_record_publish_id(*_args, **_kwargs):
            raise sqlite3.OperationalError(
                "injected recovered ID persistence failure"
            )

        service.store.record_publish_id = fail_record_publish_id
        unknown = service.publish_claimed(
            created["id"],
            claim["claim_token"],
        )["item"]

        self.assertEqual("unknown", unknown["status"])
        self.assertTrue(unknown["unknown_outcome"])
        self.assertFalse(unknown["publish_id"])
        self.assertEqual([stable_job], self.gpu.publish_jobs)
        self.assertEqual([], self.gpu.reconcile_jobs)

    def test_manual_reconcile_recovers_initialized_publishing_row_without_init(self):
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
        service.store.begin_publish(
            created["id"],
            claim["claim_token"],
            OPEN_GATES,
        )
        self.gpu.reconcile_result = {
            "publish_id": "v_pub_url~v2-1.7668584571734657042",
            "state": "processing",
            "status": {"status": "PROCESSING_DOWNLOAD"},
        }

        recovered = service.manual_reconcile(created["id"])["item"]

        self.assertEqual("reconciling", recovered["status"])
        self.assertEqual(
            "v_pub_url~v2-1.7668584571734657042",
            recovered["publish_id"],
        )
        self.assertEqual([], self.gpu.publish_jobs)
        self.assertEqual([stable_job], self.gpu.reconcile_jobs)
        replay = service.store.record_publish_id(
            created["id"],
            claim["claim_token"],
            "v_pub_url~v2-1.7668584571734657042",
        )
        self.assertEqual("reconciling", replay["status"])
        self.assertEqual(
            "v_pub_url~v2-1.7668584571734657042",
            replay["publish_id"],
        )
        self.assertEqual([], self.gpu.publish_jobs)
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
    @staticmethod
    def _material_row(**overrides):
        row = {
            "material_id": "9001",
            "product": "Dramawave",
            "material_type": 2,
            "is_delete": 0,
            "material_url": "https://cdn.example.com/source.mp4",
            "material_name": "Material",
            "material_language": "English",
            "content_id": "ABCD1234",
            "source_tag_name": None,
            "video_duration": 2087,
        }
        row.update(overrides)
        return row

    @staticmethod
    def _material_connection(rows):
        statements = []

        def router(sql, params):
            if "ads_custom_source cs" not in sql:
                raise AssertionError("unexpected material query")
            return rows

        return FakeConnection(router, statements), statements

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
            "features.tt_posts.service._select_tt_pool_candidates",
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

    def test_resolver_accepts_long_tt_video_and_keeps_shared_safety_checks(self):
        connection, statements = self._material_connection(
            [self._material_row(video_duration=2087)]
        )
        resolver = DramawaveMaterialResolver(
            lambda: connection,
            now_fn=lambda: datetime(2026, 7, 29, tzinfo=UTC),
        )
        zero_violations = {
            "facebook_count": 0,
            "tiktok_count": 0,
            "twitter_count": 0,
            "resource_audit_count": 0,
        }
        drama_rows = [
            {
                "content_id": "ABCD1234",
                "series_code": "SERIES1",
                "language": "English",
                "drama_name": "Drama",
                "drama_labels": "romance",
                "drama_description": "Description",
            }
        ]
        with (
            mock.patch(
                "features.tt_posts.service."
                "_TTDramawaveCandidateSelector._violation_counts",
                return_value=zero_violations,
            ) as violations,
            mock.patch(
                "features.tt_posts.service."
                "_TTDramawaveCandidateSelector._material_tags",
                return_value=[],
            ) as tags,
            mock.patch(
                "features.tt_posts.service."
                "_TTDramawaveCandidateSelector._pool_drama_rows",
                return_value=drama_rows,
            ) as mapping,
            mock.patch(
                "features.tt_posts.service."
                "_TTDramawaveCandidateSelector._validate_drama_deploy_time",
                return_value=0,
            ) as deploy_time,
        ):
            item = resolver.resolve("9001")

        self.assertEqual(item["content_id"], "ABCD1234")
        self.assertEqual(
            item["source_media_url"],
            "https://cdn.example.com/source.mp4",
        )
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0][1], ("9001",))
        violations.assert_called_once_with("9001")
        tags.assert_called_once_with("9001")
        mapping.assert_called_once_with("ABCD1234", "English")
        deploy_time.assert_called_once_with("ABCD1234", "English")

    def test_resolver_returns_specific_base_eligibility_errors(self):
        cases = (
            ("missing", [], "material_not_found", 404),
            (
                "not-video",
                [self._material_row(material_type=1)],
                "material_type_not_video",
                409,
            ),
            (
                "deleted",
                [self._material_row(is_delete=1)],
                "material_deleted",
                409,
            ),
            (
                "duration",
                [self._material_row(video_duration=3601)],
                "material_duration_out_of_range",
                409,
            ),
        )
        for label, rows, error_code, status in cases:
            with self.subTest(label=label):
                connection, _statements = self._material_connection(rows)
                resolver = DramawaveMaterialResolver(
                    lambda connection=connection: connection,
                    now_fn=lambda: datetime(2026, 7, 29, tzinfo=UTC),
                )
                with self.assertRaises(TTPostServiceError) as caught:
                    resolver.resolve("9001")
                self.assertEqual(caught.exception.code, error_code)
                self.assertEqual(caught.exception.status, status)


class FakeRunnerSidecar:
    def __init__(self):
        self.calls = []
        self.pending_claims = [
            {
                "queue": {
                    "id": 4,
                    "source_account_id": "103",
                    "status": "claimed",
                },
                "claim_token": "claim-secret",
            },
        ]

    def schedules_due(self, limit):
        self.calls.append(("schedules_due", limit))
        return {"items": []}

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
        if not self.pending_claims:
            return []
        return [self.pending_claims.pop(0)]

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
    def __init__(self):
        super().__init__()
        self.pending_reconcile = [
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

    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        if not self.pending_reconcile:
            return []
        return [self.pending_reconcile.pop(0)]

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


class UnknownRunnerSidecar(FakeRunnerSidecar):
    def __init__(self):
        super().__init__()
        self.pending_claims = [
            {
                "queue": {
                    "id": 3,
                    "source_account_id": "102",
                    "status": "unknown",
                    "unknown_outcome": True,
                },
                "claim_token": "must-not-be-used",
            }
        ]

    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return []


class PublishIsolationSidecar(FakeRunnerSidecar):
    def __init__(self):
        super().__init__()
        self.pending_claims = [
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

    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return []

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
    def __init__(self):
        super().__init__()
        self.pending_claims = [
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


class NewDailyQueueSidecar(FakeRunnerSidecar):
    def __init__(self):
        super().__init__()
        self.pending_claims = [
            {
                "queue": {
                    "id": 41,
                    "source_account_id": "401",
                    "material_id": "9401",
                    "status": "claimed",
                },
                "claim_token": "claim-secret-41",
            }
        ]

    def schedules_due(self, limit):
        self.calls.append(("schedules_due", limit))
        return {
            "items": [
                {
                    "run_id": 71,
                    "queue_id": 41,
                    "source_account_id": "401",
                    "material_id": "9401",
                    "status": "scheduled",
                    "caption": "must-not-enter-runner-output",
                }
            ],
            "deferred_count": 2,
            "oldest_deferred_at_utc": "2026-07-29T03:00:00Z",
        }

    def publish(self, queue_id, claim_token):
        self.calls.append(("publish", queue_id, claim_token))
        return {
            "item": {
                "id": queue_id,
                "source_account_id": "401",
                "status": "reconciling",
                "publish_id": "pub-41",
            }
        }

    def reconciling(self, _limit):
        self.calls.append(("reconciling",))
        return []


class RunnerTests(unittest.TestCase):
    def config(self):
        return RunnerConfig(
            internal_url="http://127.0.0.1:18829",
            internal_token=INTERNAL_TOKEN,
            worker_id="tt-post-runner-primary",
            grace_seconds=600,
            claim_limit=1,
            reconcile_limit=1,
            timeout=30,
            schedule_timeout=45,
            publish_timeout=60,
            reconcile_timeout=45,
            lock_path=(
                str(Path(tempfile.gettempdir()) / "tt-post-runner.lock")
                if sys.platform == "win32"
                else "/run/tt-post/tt-post-runner-test.lock"
            ),
        )

    def test_runner_schedules_first_claims_singly_and_never_republishes_unknown(self):
        sidecar = FakeRunnerSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            [
                "schedules_due",
                "claim",
                "publish",
                "reconciling",
                "reconcile",
            ],
        )
        publish = sidecar.calls[2]
        self.assertEqual(publish[1], 4)
        self.assertTrue(
            all(
                call[1]["limit"] == 1
                for call in sidecar.calls
                if call[0] == "claim"
            )
        )
        rendered = json.dumps(result)
        self.assertNotIn("claim-secret", rendered)
        self.assertNotIn("must-not-be-used", rendered)
        self.assertEqual(result["publish_request_count"], 1)

    def test_runner_never_republishes_unknown(self):
        sidecar = UnknownRunnerSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertNotIn(
            "publish",
            [call[0] for call in sidecar.calls],
        )
        self.assertEqual(result["publish_request_count"], 0)
        self.assertNotIn("must-not-be-used", json.dumps(result))

    def test_reconcile_business_error_does_not_block_other_items_or_claims(self):
        sidecar = ReconcileIsolationSidecar()
        first = execute_runner_tick(self.config(), client=sidecar)
        second = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(first["reconcile_count"], 0)
        self.assertEqual(first["reconcile_error_count"], 1)
        self.assertEqual(first["publish_request_count"], 1)
        self.assertEqual(second["reconcile_count"], 1)
        self.assertEqual(second["reconcile_error_count"], 0)
        error = next(
            item
            for item in first["results"]
            if item["operation"] == "reconcile_error"
        )
        self.assertEqual(error["queue_id"], 11)
        self.assertEqual(error["error_code"], "tt_post_publish_id_conflict")
        rendered = json.dumps({"first": first, "second": second})
        self.assertNotIn("must-not-render", rendered)
        self.assertNotIn("claim-secret", rendered)

    def test_reconcile_response_cannot_exceed_per_tick_budget(self):
        class OversizedReconcileSidecar(FakeRunnerSidecar):
            def reconciling(self, _limit):
                return [
                    {
                        "id": 51,
                        "status": "reconciling",
                        "publish_id": "pub-51",
                    },
                    {
                        "id": 52,
                        "status": "reconciling",
                        "publish_id": "pub-52",
                    },
                ]

        with self.assertRaises(RunnerError) as caught:
            execute_runner_tick(
                self.config(),
                client=OversizedReconcileSidecar(),
            )
        self.assertEqual(
            caught.exception.code,
            "tt_post_sidecar_invalid_response",
        )

    def test_reconcile_backlog_starts_only_after_due_claim_publish(self):
        sidecar = ReconcileBacklogSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls[:3]],
            ["schedules_due", "claim", "publish"],
        )
        self.assertEqual(result["publish_request_count"], 1)
        self.assertEqual(result["reconcile_count"], self.config().reconcile_limit)
        self.assertEqual(result["reconcile_budget"], 1)

    def test_new_daily_queue_uses_remaining_claim_budget_and_safe_result(self):
        sidecar = NewDailyQueueSidecar()
        result = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["schedules_due", "claim", "publish", "reconciling"],
        )
        self.assertEqual(result["schedule_due_count"], 1)
        self.assertEqual(result["schedule_deferred_count"], 2)
        self.assertEqual(
            result["oldest_deferred_at_utc"],
            "2026-07-29T03:00:00Z",
        )
        self.assertEqual(result["publish_request_count"], 1)
        rendered = json.dumps(result)
        self.assertNotIn("must-not-enter-runner-output", rendered)
        self.assertNotIn("claim-secret-41", rendered)

    def test_publish_business_error_defers_later_claim_to_next_tick(self):
        sidecar = PublishIsolationSidecar()
        first = execute_runner_tick(self.config(), client=sidecar)
        second = execute_runner_tick(self.config(), client=sidecar)
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            [
                "schedules_due",
                "claim",
                "publish",
                "reconciling",
                "schedules_due",
                "claim",
                "publish",
                "reconciling",
            ],
        )
        self.assertEqual(first["publish_request_count"], 0)
        self.assertEqual(first["publish_request_error_count"], 1)
        self.assertEqual(second["publish_request_count"], 1)
        error = next(
            item
            for item in first["results"]
            if item["operation"] == "publish_request_error"
        )
        self.assertEqual(error["queue_id"], 21)
        self.assertEqual(
            error["error_code"],
            "tt_post_publish_preflight_failed",
        )
        rendered = json.dumps({"first": first, "second": second})
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
        defaults = RunnerConfig.from_env()
        self.assertEqual(
            (
                defaults.timeout,
                defaults.schedule_timeout,
                defaults.publish_timeout,
                defaults.reconcile_timeout,
            ),
            (60, 1500, 2400, 1500),
        )
        bad = RunnerConfig(
            **{
                **self.config().__dict__,
                "grace_seconds": 601,
            }
        )
        with self.assertRaises(RunnerError):
            bad.validate()

    def test_publish_route_alone_uses_the_longer_sidecar_timeout(self):
        timeouts = []
        connections = [
            CaptureConnection({"items": []}),
            CaptureConnection({"items": []}),
            CaptureConnection({"item": {"id": 7, "status": "reconciling"}}),
            CaptureConnection({"item": {"id": 7, "status": "published"}}),
        ]

        def factory(_host, _port, timeout):
            timeouts.append(timeout)
            return connections.pop(0)

        client = TTPostSidecarClient(
            "http://127.0.0.1:18829",
            INTERNAL_TOKEN,
            timeout=60,
            schedule_timeout=1500,
            publish_timeout=2400,
            reconcile_timeout=1500,
            connection_factory=factory,
        )
        client.schedules_due(1)
        client.claim(
            worker_id="runner",
            grace_seconds=600,
            limit=1,
        )
        client.publish(7, "claim-token")
        client.reconcile(7)
        self.assertEqual(timeouts, [1500, 60, 2400, 1500])


class HTTPContractTests(unittest.TestCase):
    class Facade:
        gates = CLOSED_GATES

        def accounts(self):
            return {"items": [], "gates": self.gates.as_dict()}

        def account_settings(self):
            return {"items": [], "marker": "account-settings"}

        def account_settings_save(self, payload):
            return {"item": {"marker": "account-settings-save", **payload}}

        def account_settings_batch_creator_info(self, payload):
            return {
                "items": [
                    {"marker": "account-settings-batch-creator", **payload}
                ]
            }

        def account_settings_batch_save(self, payload):
            return {
                "items": [
                    {"marker": "account-settings-batch-save", **payload}
                ],
                "saved_count": 1,
            }

        def creator_info(self, payload):
            return {"item": {"marker": "creator", **payload}}

        def material_preview(self, payload):
            return {"item": {"marker": "material", **payload}}

        def material_pool_list(self, _query):
            return {"items": [], "marker": "material-pool-list"}

        def material_pool_add(self, payload):
            return {"item": {"marker": "material-pool-add", **payload}}

        def preparation_claim(self, payload):
            return {"item": {"id": 7, **payload}, "claim_token": "x" * 43}

        def preparation_renew(self, intake_id, payload):
            return {
                "item": {
                    "marker": "preparation-renew",
                    "id": int(intake_id),
                    **payload,
                }
            }

        def preparation_process(self, intake_id, payload):
            return {
                "item": {
                    "marker": "preparation-process",
                    "id": int(intake_id),
                    **payload,
                }
            }

        def schedule_get(self, _query):
            return {"item": {"marker": "schedule-get"}}

        def schedule_save(self, payload):
            return {"item": {"marker": "schedule-save", **payload}}

        def run_now(self, payload):
            return {"item": {"marker": "run-now", **payload}}

        def schedules_due(self, _payload):
            return {"items": [], "marker": "schedules-due"}

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
                "/api/admin/tt-posts/account-settings/batch/creator-info",
                "POST",
                {"source_account_ids": ["101"]},
            )["items"][0]["marker"],
            "account-settings-batch-creator",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/account-settings/batch",
                "POST",
                {"targets": [{"source_account_id": "101"}]},
            )["items"][0]["marker"],
            "account-settings-batch-save",
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
            self.request(
                "/api/admin/tt-posts/material-pool"
                "?source_account_id=101"
            )["marker"],
            "material-pool-list",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/material-pool",
                "POST",
                {"material_id": "9001"},
            )["item"]["marker"],
            "material-pool-add",
        )
        preparation_claim = self.request(
            "/internal/tt-posts/preparations/claim",
            "POST",
            {"worker_id": "worker-1", "lease_seconds": 180},
        )
        self.assertEqual(preparation_claim["item"]["id"], 7)
        self.assertEqual(
            self.request(
                "/internal/tt-posts/preparations/7/renew",
                "POST",
                {
                    "claim_token": "x" * 43,
                    "lease_seconds": 180,
                },
            )["item"]["marker"],
            "preparation-renew",
        )
        self.assertEqual(
            self.request(
                "/internal/tt-posts/preparations/7/process",
                "POST",
                {"claim_token": "x" * 43},
            )["item"]["marker"],
            "preparation-process",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/schedule?source_account_id=101"
            )["item"]["marker"],
            "schedule-get",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/schedule",
                "POST",
                {"source_account_id": "101"},
            )["item"]["marker"],
            "schedule-save",
        )
        self.assertEqual(
            self.request(
                "/api/admin/tt-posts/run-now",
                "POST",
                {
                    "source_account_id": "101",
                    "idempotency_key": "manual-1",
                },
            )["item"]["marker"],
            "run-now",
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
            self.request(
                "/internal/tt-posts/schedules/due",
                "POST",
                {},
            )["marker"],
            "schedules-due",
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
        path_unit = (
            root / "deploy" / "tt-post-runner.path"
        ).read_text("utf-8")
        nginx = (root / "deploy" / "nginx-x-oauth.conf").read_text("utf-8")
        service = (root / "deploy" / "tt-post-service.service").read_text("utf-8")
        runner = (root / "deploy" / "tt-post-runner.service").read_text("utf-8")
        gpu_env = (
            root / "deploy" / "tt-post-gpu.env.example"
        ).read_text("utf-8")
        self.assertIn("TT_POST_LIVE_ENABLED=0", env)
        self.assertIn("TT_POST_DIRECT_AUDIT_APPROVED=0", env)
        self.assertIn("TT_POST_URL_PROPERTY_VERIFIED=0", env)
        self.assertIn("TT_POST_GRACE_SECONDS=600", env)
        self.assertIn("TT_POST_RECONCILE_LIMIT=1", env)
        self.assertIn(
            "TT_POST_RUNNER_KICK_PATH=/run/tt-post/manual-kick",
            env,
        )
        self.assertIn(
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333",
            env,
        )
        self.assertIn(
            "TT_POST_MEDIA_PROFILE_VERSION=tt-post-hevc-720x1280-v2",
            env,
        )
        self.assertIn("TT_POST_CLAIM_LIMIT=1", env)
        self.assertIn("TT_POST_RECONCILE_LIMIT=1", env)
        self.assertIn("TT_POST_INTERNAL_TIMEOUT=60", env)
        self.assertIn("TT_POST_SCHEDULE_TIMEOUT=1500", env)
        self.assertIn("TT_POST_PUBLISH_TIMEOUT=2400", env)
        self.assertIn("TT_POST_RECONCILE_TIMEOUT=1500", env)
        self.assertIn("TT_POST_GPU_TIMEOUT=900", env)
        self.assertIn("TT_POST_GPU_PREPARE_TIMEOUT=9000", env)
        self.assertIn(
            "location = /api/admin/tt-posts/materials/preview",
            nginx,
        )
        self.assertIn("proxy_read_timeout 120s;", nginx)
        self.assertIn("proxy_send_timeout 120s;", nginx)
        self.assertIn("OnCalendar=*-*-* *:*:00 Asia/Shanghai", timer)
        self.assertIn(
            "PathChanged=/run/tt-post/manual-kick",
            path_unit,
        )
        self.assertIn("Unit=tt-post-runner.service", path_unit)
        self.assertIn("RuntimeDirectory=tt-post", service)
        self.assertNotIn("RuntimeDirectory=tt-post\n", runner)
        self.assertIn(
            "TT_POST_GPU_MAX_DURATION_SECONDS=3600",
            gpu_env,
        )
        self.assertIn(
            "TT_POST_GPU_MAX_OUTPUT_BYTES=4294967296",
            gpu_env,
        )
        self.assertIn("TT_POST_GPU_VIDEO_ENCODER=hevc_nvenc", gpu_env)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("EnvironmentFile=/etc/tt-post.secrets", service)
        self.assertIn("TimeoutStartSec=5700s", runner)
        self.assertNotIn("TimeoutStartSec=55s", runner)
        self.assertIn(
            "TT_POST_ADMIN_SERVICE_URL=http://127.0.0.1:18829",
            app_env,
        )
        self.assertIn("TT_POST_ADMIN_TIMEOUT=600", app_env)
        self.assertIn("TT_POST_ADMIN_PREVIEW_TIMEOUT=60", app_env)
        self.assertIn("TT_POST_INTERNAL_TOKEN=", app_env)
        self.assertIn(
            "EnvironmentFile=-/etc/tt-post-app.env",
            app_drop_in,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

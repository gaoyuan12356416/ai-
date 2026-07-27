#!/usr/bin/env python3
"""Focused tests for the X OAuth account-management feature."""

from __future__ import annotations

import contextlib
import concurrent.futures
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_accounts import client  # noqa: E402
from features.x_accounts import oauth_service as service  # noqa: E402


class XAccountsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        service.DATA_DIR = root
        service.DB_PATH = root / "accounts.sqlite3"
        service.TOKENS_DIR = root / "tokens"
        service.CLIENT_ID = "test-client-id"
        service.CLIENT_SECRET = "test-client-secret"
        service.INTERNAL_TOKEN = "test-internal-token"
        service.DAILY_INTERNAL_TOKEN = "test-daily-internal-token"
        service.DAILY_ACCOUNT_IDS = (101, 102, 103)
        service.PUBLIC_BASE_URL = "https://ai.yingliangads.com/x-oauth"
        service.ADMIN_RETURN_URL = "https://ai.yingliangads.com/x-accounts.html"
        service.SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access", "media.write")
        service.STATE_TTL_SECONDS = 600
        service.POST_DB_PATH = service.DB_PATH
        service.POST_PUBLIC_ROOT = root / "s2l"
        service.POST_SHORT_BASE_URL = "https://ai.yingliangads.com/s2l"
        service.POST_MEDIA_ALLOWED_HOSTS = ("media.example.com",)
        service.POST_HTTP_TIMEOUT_SECONDS = 30
        service.POST_MAX_MEDIA_BYTES = 512 * 1024 * 1024
        service._ACCOUNT_LOCKS.clear()
        service.ensure_storage()
        self.owner = {
            "tenant_key": "tenant-a", "user_id": "u-1", "name": "测试用户",
            "email": "a@example.com", "role": "user",
        }
        self.other_owner = {
            "tenant_key": "tenant-a", "user_id": "u-2", "name": "其他用户",
            "email": "b@example.com", "role": "user",
        }
        self.same_user_other_tenant = {
            "tenant_key": "tenant-b", "user_id": "u-1", "name": "跨租户同ID用户",
            "email": "cross-tenant@example.com", "role": "user",
        }
        self.admin = {
            "tenant_key": "tenant-a", "user_id": "admin-1", "name": "管理员",
            "email": "admin@example.com", "role": "admin",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daily_account_scope_accepts_one_and_fifty_but_rejects_invalid_scope(self):
        self.assertEqual(service._daily_account_scope((7,)), (7,))
        fifty = tuple(range(1, 51))
        self.assertEqual(service._daily_account_scope(fifty), fifty)
        for values in (
            (),
            (1, 1),
            (0,),
            (-1,),
            tuple(range(1, 52)),
        ):
            with self.subTest(values=values), self.assertRaises(service.ServiceError):
                service._daily_account_scope(values)

    def test_root_only_systemd_environment_file_is_not_reopened_after_privilege_drop(self):
        env_file = self.root / "root-only.env"
        env_file.write_text("X_INTERNAL_TOKEN=must-not-be-read\n", encoding="utf-8")
        with mock.patch.object(
            Path,
            "read_text",
            side_effect=PermissionError("simulated root-only 0600 file"),
        ), mock.patch.dict(
            os.environ,
            {"X_INTERNAL_TOKEN": "already-injected-by-systemd"},
            clear=False,
        ):
            service.load_env_file(env_file)
            self.assertEqual(
                os.environ["X_INTERNAL_TOKEN"],
                "already-injected-by-systemd",
            )

    def test_x_http_and_application_rate_limits_keep_stable_429_semantics(self):
        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode("utf-8")

            def read(self, _limit):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(
            service._NO_REDIRECT_OPENER,
            "open",
            return_value=Response(
                {
                    "errors": [
                        {
                            "code": 88,
                            "message": "wording is deliberately ignored",
                        }
                    ]
                }
            ),
        ):
            with self.assertRaises(service.ServiceError) as application_limit:
                service.http_json(service.USERS_ME_URL)
        self.assertEqual(application_limit.exception.code, "x_post_rate_limited")
        self.assertEqual(application_limit.exception.status, 429)

        http_error = urllib.error.HTTPError(
            service.USERS_ME_URL,
            429,
            "limited",
            {},
            io.BytesIO(
                b'{"type":"https://api.x.com/2/problems/usage-capped"}'
            ),
        )
        with mock.patch.object(
            service._NO_REDIRECT_OPENER,
            "open",
            side_effect=http_error,
        ):
            with self.assertRaises(service.ServiceError) as http_limit:
                service.http_json(service.USERS_ME_URL)
        self.assertEqual(http_limit.exception.code, "x_post_rate_limited")
        self.assertEqual(http_limit.exception.status, 429)

    def new_state(self, actor=None):
        result = service.create_authorization(actor or self.owner)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result["authorization_url"]).query)
        return result, query["state"][0]

    def complete(self, x_user_id="123456789", username="tester", scope=None, expires_in=7200, actor=None, account_fields=None):
        _result, state = self.new_state(actor)
        token = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": scope or " ".join(service.SCOPES),
        }
        account_data = {"id": x_user_id, "username": username, "name": "Test User", "profile_image_url": "https://pbs.twimg.com/a.jpg"}
        account_data.update(account_fields or {})
        account = {"data": account_data}
        with mock.patch.object(service, "token_request", return_value=token), mock.patch.object(service, "user_request", return_value=account):
            item = service.complete_authorization("one-time-code", state)
        return item

    def canary_payload(self, item):
        return {
            "account_id": item["id"],
            "source_date": "2026-07-22",
            "material_id": "123",
            "content_id": "456",
            "material_url": "https://media.example.com/video.mp4",
            "material_name": "material-name",
            "material_language": "English",
            "drama_name": "Drama name",
            "tag": "Drama",
            "description": "Drama description",
            "account_username": "untrusted-client-value",
            "page_name": "untrusted-client-value",
            "page_id": "untrusted-client-value",
            "queue_id": "untrusted-client-value",
            "source_queue_id": "untrusted-client-value",
            "idempotency_key": "untrusted-client-value",
        }

    def fake_x_posts_api(self, captured, publish_result=None, publish_error=None):
        class FakeXPostError(RuntimeError):
            def __init__(self, code, message, status=400, unknown_outcome=False):
                super().__init__(message)
                self.code = code
                self.status = status
                self.unknown_outcome = unknown_outcome

        class FakeStore:
            def __init__(_self, db_path):
                captured["db_path"] = db_path

            def enqueue(_self, payload):
                captured["enqueued"] = dict(payload)
                return {"id": 17}

        def fake_publish(**kwargs):
            captured["publish_calls"] = captured.get("publish_calls", 0) + 1
            captured["publish_kwargs"] = dict(kwargs)
            if publish_error is not None:
                if isinstance(publish_error, tuple):
                    raise FakeXPostError(*publish_error)
                raise publish_error
            return dict(
                publish_result
                or {
                    "status": "published",
                    "log_id": 18,
                    "short_url": "https://ai.yingliangads.com/s2l/18.html",
                    "post_id": "1900000000000000000",
                    "preview_url": "https://x.com/canary_user/status/1900000000000000000",
                    "access_token": "must-not-escape",
                }
            )

        return FakeXPostError, FakeStore, fake_publish

    def test_authorization_url_uses_pkce_and_hashed_one_time_state(self):
        result, raw_state = self.new_state()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result["authorization_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(set(query["scope"][0].split()), set(service.SCOPES))
        self.assertEqual(query["redirect_uri"], ["https://ai.yingliangads.com/x-oauth/callback"])
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            row = conn.execute("SELECT state_hash,code_verifier,actor_user_id,actor_tenant_key FROM x_oauth_state").fetchone()
        self.assertEqual(row[0], service.state_digest(raw_state))
        self.assertNotEqual(row[0], raw_state)
        self.assertTrue(row[1])
        self.assertEqual(row[2], "u-1")
        self.assertEqual(row[3], "tenant-a")
        service.consume_state(raw_state)
        with self.assertRaises(service.ServiceError):
            service.consume_state(raw_state)

    def test_expired_state_is_rejected_without_writing_token(self):
        _result, raw_state = self.new_state()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_oauth_state SET expires_at='2000-01-01T00:00:00Z'")
            conn.commit()
        with self.assertRaises(service.ServiceError):
            service.complete_authorization("expired-code", raw_state)
        self.assertEqual(list(service.TOKENS_DIR.glob("*.json")), [])

    def test_callback_upserts_account_and_never_returns_tokens(self):
        first = self.complete(username="first")
        second = self.complete(username="second")
        result = service.list_accounts(self.owner, "mine")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["username"], "second")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("access-secret", serialized)
        self.assertNotIn("refresh-secret", serialized)
        token_file = service.TOKENS_DIR / "123456789.json"
        self.assertTrue(token_file.exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(service.DB_PATH.stat().st_mode), 0o600)
        self.assertEqual(first["x_user_id"], second["x_user_id"])

    def test_multiple_x_accounts_use_separate_token_files(self):
        self.complete(x_user_id="111111111", username="first")
        self.complete(x_user_id="222222222", username="second")
        result = service.list_accounts(self.owner, "mine")
        self.assertEqual(result["total"], 2)
        self.assertEqual({item["x_user_id"] for item in result["items"]}, {"111111111", "222222222"})
        self.assertTrue((service.TOKENS_DIR / "111111111.json").exists())
        self.assertTrue((service.TOKENS_DIR / "222222222.json").exists())

    def test_owner_scoped_lists_admin_all_and_nonowner_idor(self):
        first = self.complete(x_user_id="111111111", username="first", actor=self.owner)
        second = self.complete(x_user_id="222222222", username="second", actor=self.other_owner)

        self.assertEqual([item["id"] for item in service.list_accounts(self.owner, "mine")["items"]], [first["id"]])
        self.assertEqual([item["id"] for item in service.list_accounts(self.other_owner, "mine")["items"]], [second["id"]])
        self.assertEqual(service.list_accounts(self.same_user_other_tenant, "mine")["items"], [])
        self.assertEqual(
            {item["id"] for item in service.list_accounts(self.admin, "all")["items"]},
            {first["id"], second["id"]},
        )
        with self.assertRaises(service.ServiceError) as denied_all:
            service.list_accounts(self.owner, "all")
        self.assertEqual(denied_all.exception.code, "x_admin_required")

        with mock.patch.object(service, "read_token_file") as read_mock:
            with self.assertRaises(service.ServiceError) as denied:
                service.verify_account(second["id"], self.owner)
        self.assertEqual(denied.exception.code, "x_account_not_found")
        read_mock.assert_not_called()
        with mock.patch.object(service, "read_token_file") as token_read_mock:
            with self.assertRaises(service.ServiceError) as logout_denied:
                service.logout_account(second["id"], self.owner)
        self.assertEqual(logout_denied.exception.code, "x_account_not_found")
        token_read_mock.assert_not_called()

        with self.assertRaises(service.ServiceError) as cross_tenant_verify:
            service.verify_account(first["id"], self.same_user_other_tenant)
        self.assertEqual(cross_tenant_verify.exception.code, "x_account_not_found")
        with self.assertRaises(service.ServiceError) as cross_tenant_logout:
            service.logout_account(first["id"], self.same_user_other_tenant)
        self.assertEqual(cross_tenant_logout.exception.code, "x_account_not_found")

        verified_payload = {"data": {"id": "222222222", "username": "admin-verified", "name": "Admin Verified"}}
        with mock.patch.object(service, "user_request", return_value=verified_payload):
            verified = service.verify_account(second["id"], self.admin, "all")
        self.assertEqual(verified["username"], "admin-verified")

    def test_account_dto_marks_only_configured_daily_auto_publish_accounts(self):
        configured = self.complete(
            "222222223",
            "daily_configured",
            actor=self.owner,
        )
        manual_only = self.complete(
            "222222224",
            "manual_only",
            actor=self.owner,
        )
        from features.x_posts.service import XPostStore
        from datetime import datetime

        XPostStore(service.POST_DB_PATH).save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [configured["id"]],
                "publish_times": ["10:00"],
                "version": 1,
            },
            actor=self.admin,
            eligible_account_ids=[configured["id"], manual_only["id"]],
            now=datetime.fromisoformat("2026-07-27T08:00:00+08:00"),
        )

        items = service.list_accounts(self.admin, "all")["items"]
        by_id = {item["id"]: item for item in items}

        self.assertIs(
            by_id[configured["id"]]["daily_auto_publish_configured"],
            True,
        )
        self.assertIs(
            by_id[manual_only["id"]]["daily_auto_publish_configured"],
            False,
        )
        self.assertTrue(by_id[configured["id"]]["publish_eligible"])
        self.assertTrue(by_id[manual_only["id"]]["publish_eligible"])

    def test_internal_schedule_scope_keeps_claimed_account_after_config_change(self):
        from datetime import datetime
        from features.x_posts.service import BEIJING_TZ, XPostStore

        store = XPostStore(service.POST_DB_PATH)
        store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [2],
                "publish_times": ["10:00"],
                "version": 1,
            },
            actor=self.admin,
            eligible_account_ids=[2, 3],
            now=datetime(2026, 7, 27, 8, 0, tzinfo=BEIJING_TZ),
        )
        store.due_schedule_slots(
            datetime(2026, 7, 27, 10, 0, 10, tzinfo=BEIJING_TZ),
            grace_seconds=90,
        )
        store.save_schedule_config(
            "material",
            {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "account_ids": [3],
                "publish_times": ["11:00"],
                "version": 2,
            },
            actor=self.admin,
            eligible_account_ids=[2, 3],
            now=datetime(2026, 7, 27, 12, 0, tzinfo=BEIJING_TZ),
        )

        self.assertEqual(
            service._active_schedule_account_scope(),
            (3, 2),
        )

    def test_different_owner_cannot_overwrite_existing_account_or_token(self):
        original = self.complete(username="original", actor=self.owner)
        token_file = service.token_path(original["x_user_id"])
        original_token = token_file.read_bytes()
        _result, state = self.new_state(self.other_owner)
        takeover_token = {
            "access_token": "takeover-access",
            "refresh_token": "takeover-refresh",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": original["x_user_id"], "username": "takeover", "name": "Takeover"}}
        with mock.patch.object(service, "token_request", return_value=takeover_token), mock.patch.object(service, "user_request", return_value=account):
            with self.assertRaises(service.ServiceError) as caught:
                service.complete_authorization("takeover-code", state)
        self.assertEqual(caught.exception.code, "x_account_owned_by_other")
        self.assertEqual(token_file.read_bytes(), original_token)
        current = service.find_account(original["id"])
        self.assertEqual(current["username"], "original")
        self.assertEqual(current["owner_user_id"], self.owner["user_id"])
        self.assertEqual(current["authorized_by_user_id"], self.owner["user_id"])

        _result, cross_tenant_state = self.new_state(self.same_user_other_tenant)
        with mock.patch.object(service, "token_request", return_value=takeover_token), mock.patch.object(service, "user_request", return_value=account):
            with self.assertRaises(service.ServiceError) as cross_tenant_caught:
                service.complete_authorization("cross-tenant-code", cross_tenant_state)
        self.assertEqual(cross_tenant_caught.exception.code, "x_account_owned_by_other")
        self.assertEqual(token_file.read_bytes(), original_token)
        self.assertEqual(service.find_account(original["id"])["owner_tenant_key"], self.owner["tenant_key"])

    def test_profile_metrics_are_nullable_and_refresh_on_verify(self):
        item = self.complete(
            username="metrics_user",
            account_fields={
                "public_metrics": {
                    "followers_count": 101,
                    "following_count": 22,
                    "tweet_count": 303,
                    "listed_count": 4,
                    "like_count": 505,
                    "media_count": 66,
                },
                "verified": True,
                "protected": False,
                "location": "Shenzhen",
                "created_at": "2020-01-02T03:04:05Z",
            },
        )
        self.assertEqual(item["followers_count"], 101)
        self.assertEqual(item["like_count"], 505)
        self.assertEqual(item["media_count"], 66)
        self.assertIs(item["verified"], True)
        self.assertIs(item["protected"], False)
        self.assertEqual(item["profile_url"], "https://x.com/metrics_user")
        self.assertEqual(item["x_created_at"], "2020-01-02T03:04:05Z")
        self.assertTrue(item["profile_synced_at"].endswith("Z"))
        self.assertEqual(item["last_profile_sync_at"], item["profile_synced_at"])

        refreshed_profile = {
            "data": {
                "id": item["x_user_id"],
                "username": "metrics_user",
                "name": "Metrics",
                "public_metrics": {
                    "followers_count": 111,
                    "following_count": 23,
                    "tweet_count": 333,
                    "listed_count": 5,
                    "like_count": 555,
                    "media_count": 77,
                },
                "verified": False,
                "protected": True,
            }
        }
        with mock.patch.object(service, "user_request", return_value=refreshed_profile):
            refreshed = service.verify_account(item["id"], self.owner)
        self.assertEqual(refreshed["followers_count"], 111)
        self.assertEqual(refreshed["like_count"], 555)
        self.assertEqual(refreshed["media_count"], 77)
        self.assertIs(refreshed["verified"], False)
        self.assertIs(refreshed["protected"], True)
        self.assertEqual(refreshed["location"], "Shenzhen")

        without_metrics = self.complete(x_user_id="999999999", username="no_metrics")
        for field in ("followers_count", "following_count", "tweet_count", "listed_count", "like_count", "media_count"):
            self.assertIsNone(without_metrics[field])
        historical = self.complete(x_user_id="888888888", username="a" * 16)
        self.assertEqual(historical["profile_url"], "https://x.com/" + ("a" * 16))
        too_long = self.complete(x_user_id="777777778", username="a" * 51)
        self.assertEqual(too_long["profile_url"], "")
        unsafe = self.complete(x_user_id="777777779", username="bad/name")
        self.assertEqual(unsafe["profile_url"], "")

    def test_missing_scope_is_visible(self):
        granted = "tweet.read tweet.write users.read offline.access"
        item = self.complete(scope=granted)
        self.assertEqual(item["status"], "scope_missing")
        self.assertEqual(item["missing_scopes"], ["media.write"])

    def test_existing_database_migrates_idempotently_and_legacy_owner_is_fail_closed(self):
        service.DB_PATH.unlink()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.executescript(
                """
                CREATE TABLE x_authorized_account (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    x_user_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    profile_image_url TEXT NOT NULL DEFAULT '',
                    token_store_key TEXT NOT NULL DEFAULT '',
                    token_type TEXT NOT NULL DEFAULT 'bearer',
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_authorized_at TEXT NOT NULL,
                    last_authorized_at TEXT NOT NULL,
                    access_expires_at TEXT NOT NULL DEFAULT '',
                    last_token_refresh_at TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT NOT NULL DEFAULT '',
                    last_error_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    authorized_by_user_id TEXT NOT NULL DEFAULT '',
                    authorized_by_name TEXT NOT NULL DEFAULT '',
                    authorized_by_email TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO x_authorized_account(
                    x_user_id,username,display_name,first_authorized_at,last_authorized_at,
                    authorized_by_user_id,authorized_by_name,authorized_by_email,created_at,updated_at
                ) VALUES(
                    '777777777','legacy','Legacy','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',
                    'u-1','Legacy Owner','legacy@example.com','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z'
                );
                """
            )
            conn.commit()

        service.ensure_storage()
        service.ensure_storage()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(x_authorized_account)")}
            row = conn.execute(
                "SELECT owner_tenant_key,owner_user_id,owner_name,owner_email FROM x_authorized_account"
            ).fetchone()
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(x_authorized_account)")}
        self.assertTrue({"followers_count", "like_count", "media_count", "profile_synced_at", "disconnected_at"} <= columns)
        self.assertEqual(dict(row), {
            "owner_tenant_key": "",
            "owner_user_id": "u-1",
            "owner_name": "Legacy Owner",
            "owner_email": "legacy@example.com",
        })
        self.assertIn("idx_x_account_owner_updated", indexes)
        self.assertEqual(service.list_accounts(self.owner, "mine")["items"], [])
        self.assertEqual(service.list_accounts(self.admin, "all")["total"], 1)
        _result, state = self.new_state(self.owner)
        token = {
            "access_token": "legacy-new-access", "refresh_token": "legacy-new-refresh",
            "expires_in": 7200, "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": "777777777", "username": "legacy", "name": "Legacy"}}
        with mock.patch.object(service, "token_request", return_value=token), mock.patch.object(service, "user_request", return_value=account):
            with self.assertRaises(service.ServiceError) as legacy_conflict:
                service.complete_authorization("legacy-code", state)
        self.assertEqual(legacy_conflict.exception.code, "x_account_owned_by_other")

    def test_logout_soft_disables_idempotently_without_touching_token_or_x(self):
        item = self.complete(
            username="logout_user",
            account_fields={"public_metrics": {"followers_count": 88, "like_count": 99, "media_count": 7}},
        )
        token_file = service.token_path(item["x_user_id"])
        before = token_file.read_bytes()
        with mock.patch.object(service, "read_token_file") as token_read_mock, \
                mock.patch.object(service, "http_json") as upstream_mock, \
                mock.patch.object(service, "delete_token_artifacts") as delete_mock:
            disabled = service.logout_account(item["id"], self.owner)
            second = service.logout_account(item["id"], self.owner)
        token_read_mock.assert_not_called()
        upstream_mock.assert_not_called()
        delete_mock.assert_not_called()
        self.assertEqual(token_file.read_bytes(), before)
        service.cleanup_disconnected_token_artifacts()
        self.assertEqual(token_file.read_bytes(), before)
        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse(disabled["publish_eligible"])
        self.assertEqual(disabled["followers_count"], 88)
        self.assertEqual(disabled["like_count"], 99)
        self.assertTrue(disabled["disconnected_at"].endswith("Z"))
        self.assertEqual(disabled["disconnected_by_user_id"], self.owner["user_id"])
        self.assertEqual(second["status"], "disabled")
        self.assertEqual(second["disconnected_at"], disabled["disconnected_at"])

        with mock.patch.object(service, "read_token_file") as verify_token, \
                mock.patch.object(service, "user_request") as verify_user:
            with self.assertRaises(service.ServiceError) as verify_denied:
                service.verify_account(item["id"], self.owner)
        self.assertEqual(verify_denied.exception.code, "x_account_disabled")
        verify_token.assert_not_called()
        verify_user.assert_not_called()

        with mock.patch.object(service, "read_token_file") as publish_token:
            with self.assertRaises(service.ServiceError) as publish_denied:
                with service.publish_credentials(item["id"], self.owner):
                    self.fail("disabled account must not yield publishing credentials")
        self.assertEqual(publish_denied.exception.code, "x_account_disabled")
        publish_token.assert_not_called()

        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            events = conn.execute(
                "SELECT outcome,error_code,actor_tenant_key FROM x_oauth_event WHERE event_type='logout' ORDER BY id"
            ).fetchall()
        self.assertEqual(events, [("completed", "", self.owner["tenant_key"])])

    def test_legacy_revoke_pending_soft_disables_even_with_unreadable_token(self):
        item = self.complete(username="legacy_pending")
        token_file = service.token_path(item["x_user_id"])
        token_file.write_text("not-json", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute(
                "UPDATE x_authorized_account SET status='revoke_pending',last_error='legacy remote failure' WHERE id=?",
                (item["id"],),
            )
            conn.commit()

        with mock.patch.object(service, "read_token_file") as token_read_mock, \
                mock.patch.object(service, "http_json") as upstream_mock, \
                mock.patch.object(service, "delete_token_artifacts") as delete_mock:
            disabled = service.logout_account(item["id"], self.owner)
        token_read_mock.assert_not_called()
        upstream_mock.assert_not_called()
        delete_mock.assert_not_called()
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["last_error"], "")
        self.assertEqual(token_file.read_text(encoding="utf-8"), "not-json")
        self.assertIn("authorization_url", service.create_authorization(self.owner))

    def test_publish_credentials_require_active_status_and_access_token(self):
        item = self.complete(username="publishable")
        with service.publish_credentials(item["id"], self.owner) as (account, access_token):
            self.assertTrue(account["publish_eligible"])
            self.assertEqual(account["status"], "active")
            self.assertEqual(access_token, "access-secret")

        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET status='error' WHERE id=?", (item["id"],))
            conn.commit()
        with self.assertRaises(service.ServiceError) as not_publishable:
            with service.publish_credentials(item["id"], self.owner):
                self.fail("non-active account must not yield publishing credentials")
        self.assertEqual(not_publishable.exception.code, "x_account_not_publishable")

        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET status='active' WHERE id=?", (item["id"],))
            conn.commit()
        token_without_access = json.loads(service.token_path(item["x_user_id"]).read_text(encoding="utf-8"))
        token_without_access.pop("access_token", None)
        service.atomic_write_json(service.token_path(item["x_user_id"]), token_without_access)
        with self.assertRaises(service.ServiceError) as token_missing:
            with service.publish_credentials(item["id"], self.owner):
                self.fail("account without access token must not publish")
        self.assertEqual(token_missing.exception.code, "x_token_missing")

    def test_canary_refreshes_expired_token_and_publishes_under_account_lock(self):
        item = self.complete(username="old_username")
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute(
                "UPDATE x_authorized_account SET access_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00Z", item["id"]),
            )
            conn.commit()
        refreshed = {
            "access_token": "new-access-secret",
            "refresh_token": "new-refresh-secret",
            "token_type": "bearer",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        profile = {
            "data": {
                "id": item["x_user_id"],
                "username": "canary_user",
                "name": "Canary User",
            }
        }
        captured = {}
        fake_api = self.fake_x_posts_api(captured)
        with mock.patch.object(service, "token_request", return_value=refreshed) as refresh_mock, \
                mock.patch.object(service, "user_request", return_value=profile), \
                mock.patch.object(service, "_x_posts_api", return_value=fake_api):
            result = service.publish_canary_request(self.canary_payload(item))

        refresh_mock.assert_called_once_with(
            {"grant_type": "refresh_token", "refresh_token": "refresh-secret"}
        )
        self.assertEqual(captured["publish_calls"], 1)
        self.assertEqual(captured["publish_kwargs"]["access_token"], "new-access-secret")
        self.assertEqual(captured["enqueued"]["account_id"], item["id"])
        self.assertEqual(captured["enqueued"]["account_username"], "canary_user")
        self.assertNotIn("queue_id", captured["enqueued"])
        self.assertNotIn("source_queue_id", captured["enqueued"])
        self.assertNotIn("idempotency_key", captured["enqueued"])
        self.assertEqual(captured["enqueued"]["page_name"], "Canary User")
        self.assertEqual(captured["enqueued"]["page_id"], item["x_user_id"])
        self.assertEqual(captured["publish_kwargs"]["account"]["username"], "canary_user")
        self.assertEqual(result["status"], "published")
        self.assertNotIn("access_token", result)
        saved = json.loads(service.token_path(item["x_user_id"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "new-refresh-secret")

    def test_canary_disabled_account_fails_before_queue_or_upstream(self):
        item = self.complete(username="disabled_canary")
        service.logout_account(item["id"], self.owner)
        with mock.patch.object(service, "token_request") as refresh_mock, \
                mock.patch.object(service, "user_request") as user_mock, \
                mock.patch.object(service, "_x_posts_api") as posts_api_mock:
            with self.assertRaises(service.ServiceError) as denied:
                service.publish_canary_request(self.canary_payload(item))
        self.assertEqual(denied.exception.code, "x_account_disabled")
        refresh_mock.assert_not_called()
        user_mock.assert_not_called()
        posts_api_mock.assert_not_called()

    def test_canary_unknown_create_post_outcome_is_not_retried(self):
        item = self.complete(username="unknown_canary")
        profile = {
            "data": {
                "id": item["x_user_id"],
                "username": "unknown_canary",
                "name": "Unknown Canary",
            }
        }
        captured = {}

        fake_api = self.fake_x_posts_api(
            captured,
            publish_error=("x_upstream_error", "Create Post结果不确定 access-secret", 502, True),
        )
        with mock.patch.object(service, "user_request", return_value=profile), \
                mock.patch.object(service, "_x_posts_api", return_value=fake_api):
            with self.assertRaises(service.ServiceError) as failed:
                service.publish_canary_request(self.canary_payload(item))
        self.assertEqual(failed.exception.code, "x_publish_unknown")
        self.assertEqual(failed.exception.status, 503)
        self.assertEqual(captured["publish_calls"], 1)
        self.assertNotIn("access-secret", str(failed.exception))

    def test_legacy_disconnected_logout_still_cleans_residual_token_artifacts(self):
        item = self.complete(username="legacy_disconnected")
        token_file = service.token_path(item["x_user_id"])
        tombstone = service.TOKENS_DIR / (".%s.legacy.disconnecting" % token_file.name)
        tombstone.write_text("legacy", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET status='disconnected' WHERE id=?", (item["id"],))
            conn.commit()

        result = service.logout_account(item["id"], self.owner)
        self.assertEqual(result["status"], "disconnected")
        self.assertFalse(token_file.exists())
        self.assertFalse(tombstone.exists())

    def test_same_owner_can_reauthorize_disabled_account(self):
        original = self.complete(username="before_logout")
        first_authorized_at = original["first_authorized_at"]
        old_payload = json.loads(service.token_path(original["x_user_id"]).read_text(encoding="utf-8"))
        old_payload["access_token"] = "retained-old-access"
        service.atomic_write_json(service.token_path(original["x_user_id"]), old_payload)
        old_token = service.token_path(original["x_user_id"]).read_bytes()
        service.logout_account(original["id"], self.owner)
        self.assertEqual(service.token_path(original["x_user_id"]).read_bytes(), old_token)

        restored = self.complete(username="after_logout", actor=dict(self.owner, name="Owner Renamed"))
        self.assertEqual(restored["id"], original["id"])
        self.assertEqual(restored["status"], "active")
        self.assertTrue(restored["publish_eligible"])
        self.assertEqual(restored["username"], "after_logout")
        self.assertEqual(restored["first_authorized_at"], first_authorized_at)
        self.assertEqual(restored["disconnected_at"], "")
        self.assertEqual(restored["owner_name"], self.owner["name"])
        self.assertEqual(restored["authorized_by_name"], "Owner Renamed")
        restored_token = service.token_path(restored["x_user_id"]).read_bytes()
        self.assertNotEqual(restored_token, old_token)
        self.assertEqual(json.loads(restored_token.decode("utf-8"))["access_token"], "access-secret")

    def test_oauth_callback_completed_after_disable_is_explicit_reauthorization(self):
        item = self.complete(username="before_delayed_callback")
        _result, delayed_state = self.new_state(self.owner)
        service.logout_account(item["id"], self.owner)
        replacement = {
            "access_token": "delayed-access",
            "refresh_token": "delayed-refresh",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": item["x_user_id"], "username": "after_delayed_callback", "name": "Restored"}}
        with mock.patch.object(service, "token_request", return_value=replacement), \
                mock.patch.object(service, "user_request", return_value=account):
            restored = service.complete_authorization("delayed-code", delayed_state)
        self.assertEqual(restored["status"], "active")
        self.assertTrue(restored["publish_eligible"])
        saved = json.loads(service.token_path(item["x_user_id"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], "delayed-access")

    def test_required_scopes_cannot_be_removed_by_environment_config(self):
        original_scopes = service.SCOPES
        service.SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
        try:
            self.assertFalse(service.config_payload()["configured"])
            with self.assertRaises(service.ServiceError) as caught:
                service.create_authorization(self.owner)
            self.assertEqual(caught.exception.code, "x_oauth_not_configured")
        finally:
            service.SCOPES = original_scopes

    def test_verify_rejects_empty_or_different_token_owner(self):
        for payload in ({"data": {}}, {"data": {"id": "987654321", "username": "wrong"}}):
            with self.subTest(payload=payload):
                item = self.complete()
                with mock.patch.object(service, "user_request", return_value=payload):
                    with self.assertRaises(service.ServiceError) as caught:
                        service.verify_account(item["id"], self.owner)
                self.assertEqual(caught.exception.code, "x_identity_mismatch")
                self.assertEqual(service.find_account(item["id"])["status"], "error")
                service._ACCOUNT_LOCKS.clear()
                for path in service.TOKENS_DIR.glob("*.json"):
                    path.unlink()
                with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
                    conn.execute("DELETE FROM x_authorized_account")
                    conn.commit()

    def test_expired_access_token_refreshes_and_rotates_refresh_token(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute(
                "UPDATE x_authorized_account SET access_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00Z", item["id"]),
            )
            conn.commit()
        self.assertEqual(service.find_account(item["id"])["status"], "refresh_required")
        refreshed = {
            "access_token": "new-access-secret",
            "refresh_token": "new-refresh-secret",
            "token_type": "bearer",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": "123456789", "username": "verified", "name": "Verified"}}
        with mock.patch.object(service, "token_request", return_value=refreshed) as refresh_mock, mock.patch.object(service, "user_request", return_value=account):
            verified = service.verify_account(item["id"], self.owner)
        self.assertEqual(verified["status"], "active")
        self.assertEqual(verified["username"], "verified")
        self.assertTrue(verified["last_token_refresh_at"].endswith("Z"))
        self.assertTrue(verified["last_verified_at"].endswith("Z"))
        refresh_mock.assert_called_once()
        saved = json.loads((service.TOKENS_DIR / "123456789.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "new-refresh-secret")

    def test_invalid_grant_marks_account_revoked(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET access_expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", item["id"]))
            conn.commit()
        with mock.patch.object(service, "token_request", side_effect=service.ServiceError("x_token_revoked", "X授权已失效", 409)):
            with self.assertRaises(service.ServiceError):
                service.verify_account(item["id"], self.owner)
        self.assertEqual(service.find_account(item["id"])["status"], "revoked")

    def test_authorization_and_verify_events_are_sanitized(self):
        item = self.complete()
        with mock.patch.object(service, "user_request", side_effect=service.ServiceError("x_token_revoked", "X授权已失效", 409)):
            with self.assertRaises(service.ServiceError):
                service.verify_account(item["id"], self.owner)
        self.assertEqual(service.find_account(item["id"])["status"], "revoked")
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            rows = conn.execute("SELECT event_type,outcome,error_code FROM x_oauth_event ORDER BY id").fetchall()
        self.assertIn(("authorization", "started", ""), rows)
        self.assertIn(("authorization", "completed", ""), rows)
        self.assertIn(("verify", "failed", "x_token_revoked"), rows)
        serialized = json.dumps(rows)
        self.assertNotIn("access-secret", serialized)
        self.assertNotIn("refresh-secret", serialized)

    def test_audit_failure_does_not_reverse_core_oauth_result(self):
        _result, state = self.new_state()
        token = {
            "access_token": "audit-access",
            "refresh_token": "audit-refresh",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": "123456789", "username": "audit-safe", "name": "Audit Safe"}}
        with mock.patch.object(service, "token_request", return_value=token), mock.patch.object(service, "user_request", return_value=account), mock.patch.object(service, "record_event", side_effect=sqlite3.OperationalError("audit unavailable")):
            item = service.complete_authorization("code", state)
            verified = service.verify_account(item["id"], self.owner)
        self.assertEqual(verified["status"], "active")

    def test_parallel_verify_refreshes_rotating_token_only_once(self):
        item = self.complete()
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET access_expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", item["id"]))
            conn.commit()
        refreshed = {
            "access_token": "parallel-access",
            "refresh_token": "parallel-refresh",
            "token_type": "bearer",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }
        account = {"data": {"id": "123456789", "username": "parallel", "name": "Parallel"}}
        start = threading.Barrier(3)

        def do_verify():
            start.wait(timeout=5)
            return service.verify_account(item["id"], self.owner)

        def slow_refresh(_fields):
            time.sleep(0.15)
            return dict(refreshed)

        with mock.patch.object(service, "token_request", side_effect=slow_refresh) as refresh_mock, mock.patch.object(service, "user_request", return_value=account):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(do_verify) for _ in range(2)]
                start.wait(timeout=5)
                results = [future.result(timeout=5) for future in futures]
        self.assertEqual(refresh_mock.call_count, 1)
        self.assertTrue(all(result["status"] == "active" for result in results))

    def test_reauthorization_and_verify_share_x_user_lock(self):
        item = self.complete(username="before")
        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            conn.execute("UPDATE x_authorized_account SET access_expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", item["id"]))
            conn.commit()
        reauth_actor = dict(self.owner, name="重新授权用户")
        _result, new_state = self.new_state(reauth_actor)
        refresh_started = threading.Event()

        def token_dispatch(fields):
            if fields.get("grant_type") == "refresh_token":
                refresh_started.set()
                time.sleep(0.2)
                return {
                    "access_token": "old-flow-access",
                    "refresh_token": "old-flow-refresh",
                    "expires_in": 7200,
                    "scope": " ".join(service.SCOPES),
                }
            return {
                "access_token": "reauthorized-access",
                "refresh_token": "reauthorized-refresh",
                "expires_in": 7200,
                "scope": " ".join(service.SCOPES),
            }

        def user_dispatch(access_token):
            username = "reauthorized" if access_token == "reauthorized-access" else "verified-old"
            return {"data": {"id": "123456789", "username": username, "name": username}}

        with mock.patch.object(service, "token_request", side_effect=token_dispatch), mock.patch.object(service, "user_request", side_effect=user_dispatch):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                verify_future = pool.submit(service.verify_account, item["id"], self.owner)
                self.assertTrue(refresh_started.wait(timeout=5))
                callback_future = pool.submit(service.complete_authorization, "new-code", new_state)
                verify_future.result(timeout=5)
                callback_future.result(timeout=5)
        final_item = service.find_account(item["id"])
        final_token = json.loads((service.TOKENS_DIR / "123456789.json").read_text(encoding="utf-8"))
        self.assertEqual(final_item["username"], "reauthorized")
        self.assertEqual(final_token["refresh_token"], "reauthorized-refresh")

    def test_callback_owner_lock_serializes_logout_after_token_exchange(self):
        item = self.complete(username="before_owner_lock")
        _result, state = self.new_state(self.owner)
        exchange_started = threading.Event()
        release_exchange = threading.Event()
        replacement = {
            "access_token": "owner-lock-access",
            "refresh_token": "owner-lock-refresh",
            "expires_in": 7200,
            "scope": " ".join(service.SCOPES),
        }

        def blocked_exchange(_fields):
            exchange_started.set()
            self.assertTrue(release_exchange.wait(timeout=5))
            return dict(replacement)

        account = {
            "data": {"id": item["x_user_id"], "username": "owner_locked", "name": "Owner Locked"}
        }
        with mock.patch.object(service, "token_request", side_effect=blocked_exchange), mock.patch.object(service, "user_request", return_value=account):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                callback_future = pool.submit(service.complete_authorization, "owner-lock-code", state)
                self.assertTrue(exchange_started.wait(timeout=5))
                logout_future = pool.submit(service.logout_account, item["id"], self.owner)
                time.sleep(0.1)
                self.assertFalse(logout_future.done())
                release_exchange.set()
                callback_result = callback_future.result(timeout=5)
                logout_result = logout_future.result(timeout=5)

        self.assertEqual(callback_result["username"], "owner_locked")
        self.assertEqual(logout_result["status"], "disabled")
        saved_token = json.loads(service.token_path(item["x_user_id"]).read_text(encoding="utf-8"))
        self.assertEqual(saved_token["refresh_token"], "owner-lock-refresh")

    def test_publish_context_holds_account_lock_until_upstream_work_finishes(self):
        item = self.complete(username="publish_lock")
        publish_started = threading.Event()
        release_publish = threading.Event()

        def publish_work():
            with service.publish_credentials(item["id"], self.owner) as (_account, access_token):
                self.assertEqual(access_token, "access-secret")
                publish_started.set()
                self.assertTrue(release_publish.wait(timeout=5))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            publish_future = pool.submit(publish_work)
            self.assertTrue(publish_started.wait(timeout=5))
            logout_future = pool.submit(service.logout_account, item["id"], self.owner)
            time.sleep(0.1)
            self.assertFalse(logout_future.done())
            release_publish.set()
            publish_future.result(timeout=5)
            disabled = logout_future.result(timeout=5)
        self.assertEqual(disabled["status"], "disabled")
        with self.assertRaises(service.ServiceError) as blocked:
            with service.publish_credentials(item["id"], self.owner):
                self.fail("disabled account must not publish")
        self.assertEqual(blocked.exception.code, "x_account_disabled")

    def test_handler_log_drops_callback_query(self):
        handler = object.__new__(service.Handler)
        handler.path = "/callback?code=secret-code&state=secret-state"
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 12345)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            handler.log_message("ignored %s", "requestline")
        logged = output.getvalue()
        self.assertIn("GET /callback", logged)
        self.assertNotIn("secret-code", logged)
        self.assertNotIn("secret-state", logged)

    def test_canary_internal_route_auth_payload_limit_and_success_contract(self):
        non_loopback = object.__new__(service.Handler)
        non_loopback.client_address = ("203.0.113.10", 12345)
        non_loopback.headers = {"Authorization": "Bearer " + service.INTERNAL_TOKEN}
        self.assertFalse(non_loopback.is_internal_authorized())

        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%s/internal/posts/canary" % server.server_address[1]

        def request(body, token=None):
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            return urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )

        expected = {
            "status": "published",
            "log_id": 18,
            "short_url": "https://ai.yingliangads.com/s2l/18.html",
            "post_id": "1900000000000000000",
            "preview_url": "https://x.com/canary_user/status/1900000000000000000",
        }
        try:
            with mock.patch.object(service, "publish_canary_request", return_value=expected) as publish_mock:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(request({"account_id": 2}), timeout=5)
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
                publish_mock.assert_not_called()

                with self.assertRaises(urllib.error.HTTPError) as too_large:
                    urllib.request.urlopen(
                        request(
                            {"account_id": 2, "description": "x" * service.MAX_BODY_BYTES},
                            service.INTERNAL_TOKEN,
                        ),
                        timeout=5,
                    )
                self.assertEqual(too_large.exception.code, 413)
                too_large.exception.close()
                publish_mock.assert_not_called()

                with urllib.request.urlopen(
                    request({"account_id": 2}, service.INTERNAL_TOKEN), timeout=5
                ) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, {"item": expected})
                self.assertNotIn("token", json.dumps(payload).lower())
                publish_mock.assert_called_once_with({"account_id": 2})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_daily_token_is_route_scoped_and_passes_fixed_account_scope(self):
        accounts = [
            self.complete(
                str(2101 + index),
                "daily_scope_%s" % (index + 1),
                actor=self.owner,
            )
            for index in range(9)
        ]
        service.DAILY_ACCOUNT_IDS = tuple(item["id"] for item in accounts)
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % server.server_address[1]

        def request(path, body=None):
            return urllib.request.Request(
                base_url + path,
                data=json.dumps(body or {}).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + service.DAILY_INTERNAL_TOKEN,
                    "Content-Type": "application/json",
                },
                method="POST",
            )

        forbidden = (
            "/internal/posts/canary",
            "/internal/authorize",
            "/internal/accounts/query",
            "/internal/accounts/%s/verify" % accounts[0]["id"],
            "/internal/accounts/%s/logout" % accounts[0]["id"],
            "/internal/posts/logs/query",
            "/internal/posts/runs/query",
            "/internal/posts/material-pool/query",
            "/internal/posts/material-pool/add",
            "/internal/posts/material-pool/1/delete",
        )
        expected_plan = {
            "id": 11,
            "run_date": "2026-07-23",
            "source_date": "2026-07-22",
            "queues": [],
        }
        expected_publish = {
            "status": "published",
            "log_id": 12,
            "short_url": "https://ai.yingliangads.com/s2l/12.html",
            "post_id": "1900000000000000012",
            "preview_url": "https://x.com/daily_scope_1/status/1900000000000000012",
        }
        try:
            for path in forbidden:
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(request(path), timeout=5)
                    self.assertEqual(denied.exception.code, 403)
                    denied.exception.close()

            with mock.patch.object(
                service, "verify_account", return_value=accounts[0]
            ) as verify_mock:
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/accounts/%s/verify" % accounts[0]["id"]
                    ),
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                verify_mock.assert_called_once()

                with self.assertRaises(urllib.error.HTTPError) as denied_account:
                    urllib.request.urlopen(
                        request("/internal/posts/accounts/999/verify"),
                        timeout=5,
                    )
                self.assertEqual(denied_account.exception.code, 403)
                denied_account.exception.close()

            plan_payload = {
                "run_date": "2026-07-23",
                "source_date": "2026-07-22",
                "candidates": [
                    {"account_id": account_id}
                    for account_id in service.DAILY_ACCOUNT_IDS
                ],
            }
            with mock.patch.object(
                service, "create_daily_plan_request", return_value=expected_plan
            ) as plan_mock:
                with urllib.request.urlopen(
                    request("/internal/posts/daily-plan", plan_payload),
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
            plan_mock.assert_called_once_with(
                plan_payload,
                service.DAILY_ACCOUNT_IDS,
                require_pool=True,
            )

            query_payload = {"run_date": "2026-07-23"}
            expected_query = {
                "found": False,
                "run": None,
                "queues": [],
            }
            with mock.patch.object(
                service,
                "query_daily_plan_request",
                return_value=expected_query,
            ) as query_mock:
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/daily-plan/query",
                        query_payload,
                    ),
                    timeout=5,
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": expected_query},
                    )
            query_mock.assert_called_once_with(
                query_payload,
                service.DAILY_ACCOUNT_IDS,
            )

            with mock.patch.object(
                service,
                "preflight_post_storage_request",
                return_value={"ready": True, "mounted": True, "atomic_write": True},
            ) as storage_mock:
                with urllib.request.urlopen(
                    request("/internal/posts/storage/preflight"),
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                storage_mock.assert_called_once_with(9)

            failure_payload = {
                "run_date": "2026-07-23",
                "source_date": "2026-07-22",
                "error_code": "x_post_daily_candidate_shortage",
                "error_message": "not enough candidates",
                "expected_count": 9,
            }
            expected_failure = {
                "id": 15,
                "run_date": "2026-07-23",
                "source_date": "2026-07-22",
                "status": "failed_preflight",
                "expected_count": 9,
                "recorded": True,
            }
            with mock.patch.object(
                service,
                "record_post_run_failure_request",
                return_value=expected_failure,
            ) as failure_mock:
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/runs/record-failure",
                        failure_payload,
                    ),
                    timeout=5,
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": expected_failure},
                    )
                failure_mock.assert_called_once_with(
                    failure_payload,
                    service.DAILY_ACCOUNT_IDS,
                )

            available_items = {
                "items": [
                    {
                        "id": 1,
                        "material_id": "7001",
                        "material_key": "7001",
                        "created_at": "2026-07-23T00:00:00Z",
                    }
                ]
            }
            with mock.patch.object(
                service,
                "available_post_material_pool_request",
                return_value=available_items,
            ) as available_mock:
                with urllib.request.urlopen(
                    request(
                        "/internal/posts/material-pool/available",
                        {"limit": 50},
                    ),
                    timeout=5,
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        available_items,
                    )
                available_mock.assert_called_once_with({"limit": 50})

            with mock.patch.object(
                service,
                "record_post_material_pool_checks_request",
                return_value={"updated_count": 1},
            ) as check_mock:
                check_payload = {
                    "checks": [
                        {
                            "pool_item_id": 1,
                            "error_code": "material_invalid",
                            "error_message": "invalid",
                        }
                    ]
                }
                with urllib.request.urlopen(
                    request("/internal/posts/material-pool/check", check_payload),
                    timeout=5,
                ) as response:
                    self.assertEqual(
                        json.loads(response.read().decode("utf-8")),
                        {"item": {"updated_count": 1}},
                    )
                check_mock.assert_called_once_with(check_payload)

            with mock.patch.object(
                service,
                "create_daily_plan_request",
                side_effect=service.ServiceError(
                    "x_post_material_already_used",
                    "transaction rolled back",
                    409,
                ),
            ):
                with self.assertRaises(urllib.error.HTTPError) as plan_failed:
                    urllib.request.urlopen(
                        request("/internal/posts/daily-plan", plan_payload),
                        timeout=5,
                    )
                plan_error_payload = json.loads(
                    plan_failed.exception.read().decode("utf-8")
                )
                plan_failed.exception.close()
                self.assertIs(plan_error_payload["outcome_known"], True)
                self.assertIs(plan_error_payload["unknown_outcome"], False)

            with mock.patch.object(
                service, "publish_queue_request", return_value=expected_publish
            ) as publish_mock:
                with urllib.request.urlopen(
                    request("/internal/posts/queue/77/publish"),
                    timeout=5,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, {"item": expected_publish})
                publish_mock.assert_called_once_with(
                    "77",
                    service.DAILY_ACCOUNT_IDS,
                )

            for code, expected_known, expected_unknown in (
                ("media_download_failed", True, False),
                ("x_publish_unknown", False, True),
            ):
                with self.subTest(code=code), mock.patch.object(
                    service,
                    "publish_queue_request",
                    side_effect=service.ServiceError(code, "publish failed", 503),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as failed:
                        urllib.request.urlopen(
                            request("/internal/posts/queue/77/publish"),
                            timeout=5,
                        )
                    error_payload = json.loads(
                        failed.exception.read().decode("utf-8")
                    )
                    failed.exception.close()
                    self.assertIs(
                        error_payload["outcome_known"],
                        expected_known,
                    )
                    self.assertIs(
                        error_payload["unknown_outcome"],
                        expected_unknown,
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_daily_plan_requires_complete_ordered_nine_account_scope(self):
        accounts = [
            self.complete(
                str(2201 + index),
                "nine_scope_%s" % (index + 1),
                actor=self.owner,
            )
            for index in range(10)
        ]
        service.DAILY_ACCOUNT_IDS = tuple(
            item["id"] for item in accounts[:9]
        )

        def build_candidates(account_ids):
            items = []
            for rank, account_id in enumerate(account_ids, 1):
                material_id = str(7200 + rank)
                items.append(
                    {
                        "account_id": account_id,
                        "source_date": "2026-07-22",
                        "material_id": material_id,
                        "content_id": "content-" + material_id,
                        "material_url": (
                            "https://media.example.com/%s.mp4" % material_id
                        ),
                        "material_name": "material-" + material_id,
                        "material_language": "English",
                        "drama_name": "Daily Drama",
                        "tag": "romance",
                        "description": "Safe daily description",
                        "candidate_rank": rank,
                        "spend": 100 - rank,
                        "preflight_sha256": (
                            "%064x" % int(material_id)
                        )[-64:],
                        "preflight_size": 5,
                        "compliance_counts": {
                            "facebook_violation_count": 0,
                            "tiktok_violation_count": 0,
                            "twitter_violation_count": 0,
                            "resource_audit_count": 0,
                            "dangerous_tag_count": 0,
                        },
                    }
                )
            return items

        configured_ids = service.DAILY_ACCOUNT_IDS
        invalid_scopes = {
            "missing_account": configured_ids[:-1],
            "same_set_wrong_order": (
                configured_ids[1],
                configured_ids[0],
                *configured_ids[2:],
            ),
            "out_of_scope": (
                *configured_ids[:-1],
                accounts[9]["id"],
            ),
        }
        for label, account_ids in invalid_scopes.items():
            with self.subTest(label=label):
                with self.assertRaises(service.ServiceError) as denied:
                    service.create_daily_plan_request(
                        {
                            "run_date": "2026-07-23",
                            "source_date": "2026-07-22",
                            "candidates": build_candidates(account_ids),
                        },
                        configured_ids,
                    )
                self.assertEqual(
                    denied.exception.code,
                    "x_daily_account_scope_denied",
                )
                self.assertEqual(denied.exception.status, 403)

        with mock.patch.object(
            service,
            "preflight_post_storage_request",
            return_value={"ready": True, "mounted": True, "atomic_write": True},
        ) as storage_preflight:
            plan = service.create_daily_plan_request(
                {
                    "run_date": "2026-07-23",
                    "source_date": "2026-07-22",
                    "candidates": build_candidates(configured_ids),
                },
                configured_ids,
            )

        storage_preflight.assert_called_once_with(9)
        self.assertEqual(plan["expected_count"], 9)
        self.assertEqual(len(plan["queues"]), 9)
        self.assertEqual(
            tuple(queue["account_id"] for queue in plan["queues"]),
            configured_ids,
        )

    def test_daily_plan_route_accepts_bounded_non_ascii_descriptions(self):
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%s/internal/posts/daily-plan" % server.server_address[1]

        def max_url(host, fill):
            prefix = "https://%s/" % host
            suffix = ".mp4"
            return prefix + (fill * (4096 - len(prefix) - len(suffix))) + suffix

        from features.x_posts import XPostStore

        candidates = []
        validator = XPostStore(self.root / "body-limit.sqlite3")
        for index in range(50):
            material_id = str(9000 + index)
            item = {
                "account_id": 1000 + index,
                "account_username": "daily_%02d" % index,
                "source_date": "2026-07-22",
                "material_id": material_id,
                "content_id": "c" * 128,
                "material_url": max_url("media.example.com", "a"),
                "material_name": "素材" * 127 + "名",
                "material_language": "语" * 32,
                "drama_name": "短剧" * 127 + "名",
                "tag": "标签" * 127 + "类",
                "description": "剧" * 4096,
                "page_name": "页面" * 127 + "名",
                "page_id": "p" * 64,
                "candidate_rank": index + 1,
                "spend": 100 - index,
                "original_material_url": max_url(
                    "origin.example.com",
                    "b",
                ),
                "media_repair_trigger_code": "invalid_media_codec",
                "media_repair_job_key": "repair-%02d" % index,
                "media_repair_profile": "x-h264-yuv420p",
                "media_repair_source_sha256": ("%064x" % (index + 1))[-64:],
                "preflight_sha256": ("%064x" % (index + 101))[-64:],
                "preflight_size": 5,
                "compliance_counts": {
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                },
            }
            validator._queue_payload(
                item,
                run_date="2026-07-23",
                candidate_rank=index + 1,
                require_compliance=True,
            )
            candidates.append(item)
        payload = {
            "run_date": "2026-07-23",
            "source_date": "2026-07-22",
            "candidates": candidates,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertEqual(service.MAX_BODY_BYTES, 16 * 1024)
        self.assertEqual(service.MAX_DAILY_PLAN_BODY_BYTES, 2 * 1024 * 1024)
        self.assertGreater(len(raw), 1024 * 1024)
        self.assertLess(len(raw), service.MAX_DAILY_PLAN_BODY_BYTES)
        expected = {
            "id": 1,
            "run_date": "2026-07-23",
            "source_date": "2026-07-22",
            "status": "queued",
            "queues": [],
        }
        try:
            with mock.patch.object(
                service, "create_daily_plan_request", return_value=expected
            ) as create_mock:
                request = urllib.request.Request(
                    url,
                    data=raw,
                    headers={
                        "Authorization": "Bearer " + service.INTERNAL_TOKEN,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result, {"item": expected})

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            url,
                            data=b"{}",
                            headers={
                                "Authorization": "Bearer " + service.INTERNAL_TOKEN,
                                "Content-Type": "application/json",
                                "Content-Length": str(
                                    service.MAX_DAILY_PLAN_BODY_BYTES + 1
                                ),
                            },
                            method="POST",
                        ),
                        timeout=5,
                    )
                self.assertEqual(rejected.exception.code, 413)
                rejected.exception.close()
                create_mock.assert_called_once_with(payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_internal_route_body_limits_keep_check_batch_separate_from_common_routes(self):
        self.assertEqual(service.MAX_BODY_BYTES, 16 * 1024)
        self.assertEqual(service.MAX_DAILY_CHECK_BODY_BYTES, 128 * 1024)
        check_payload = {
            "checks": [
                {
                    "pool_item_id": 1,
                    "error_code": "material_invalid",
                    "error_message": "错" * 7000,
                }
            ]
        }
        raw = json.dumps(check_payload, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(raw), service.MAX_BODY_BYTES)
        self.assertLess(len(raw), service.MAX_DAILY_CHECK_BODY_BYTES)

        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % server.server_address[1]

        def request(path):
            return urllib.request.Request(
                base_url + path,
                data=raw,
                headers={
                    "Authorization": "Bearer " + service.INTERNAL_TOKEN,
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )

        try:
            with mock.patch.object(
                service,
                "record_post_material_pool_checks_request",
                return_value={"updated_count": 1},
            ) as check_mock:
                with urllib.request.urlopen(
                    request("/internal/posts/material-pool/check"),
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                check_mock.assert_called_once_with(check_payload)

            with mock.patch.object(service, "publish_canary_request") as canary_mock:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(
                        request("/internal/posts/canary"),
                        timeout=5,
                    )
                self.assertEqual(rejected.exception.code, 413)
                rejected.exception.close()
                canary_mock.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_run_failure_client_forwards_dynamic_expected_count(self):
        payload = {
            "run_date": "2026-07-23",
            "source_date": "2026-07-22",
            "error_code": "x_post_daily_candidate_shortage",
            "error_message": "not enough candidates",
            "expected_count": 9,
            "ignored": "must not cross the sidecar boundary",
        }
        with mock.patch.object(
            client,
            "_request",
            return_value={"item": {"recorded": True}},
        ) as request_mock:
            client.record_x_post_run_failure(payload)
        request_mock.assert_called_once_with(
            "/internal/posts/runs/record-failure",
            method="POST",
            payload={
                "run_date": "2026-07-23",
                "source_date": "2026-07-22",
                "error_code": "x_post_daily_candidate_shortage",
                "error_message": "not enough candidates",
                "expected_count": 9,
            },
        )

    def test_daily_plan_log_queries_and_material_key_contract(self):
        accounts = [
            self.complete("2001", "daily_one", actor=self.owner),
            self.complete("2002", "daily_two", actor=self.owner),
            self.complete("2003", "daily_three", actor=self.owner),
            self.complete("2004", "daily_out_of_scope", actor=self.owner),
        ]
        service.DAILY_ACCOUNT_IDS = tuple(item["id"] for item in accounts[:3])
        candidates = []
        for rank, (account, material_id) in enumerate(zip(accounts[:3], ("7001", "7002", "7003")), 1):
            candidates.append(
                {
                    "account_id": account["id"],
                    "source_date": "2026-07-22",
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "material_url": "https://media.example.com/%s.mp4" % material_id,
                    "material_name": "material-" + material_id,
                    "material_language": "English",
                    "drama_name": "Daily Drama",
                    "tag": "romance",
                    "description": "Safe daily description",
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": ("%064x" % int(material_id))[-64:],
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                    "account_username": "untrusted",
                    "page_id": "untrusted",
                    "page_name": "untrusted",
                }
            )
        with mock.patch.object(
            service,
            "preflight_post_storage_request",
            return_value={"ready": True, "mounted": True, "atomic_write": True},
        ) as storage_preflight:
            out_of_scope = [dict(item) for item in candidates]
            out_of_scope[-1]["account_id"] = accounts[3]["id"]
            with self.assertRaises(service.ServiceError) as denied:
                service.create_daily_plan_request(
                    {
                        "run_date": "2026-07-23",
                        "source_date": "2026-07-22",
                        "candidates": out_of_scope,
                    },
                    service.DAILY_ACCOUNT_IDS,
                )
            self.assertEqual(denied.exception.code, "x_daily_account_scope_denied")
            plan = service.create_daily_plan_request(
                {
                    "run_date": "2026-07-23",
                    "source_date": "2026-07-22",
                    "candidates": candidates,
                },
                service.DAILY_ACCOUNT_IDS,
            )
        storage_preflight.assert_called_once_with(3)
        self.assertEqual(len(plan["queues"]), 3)
        self.assertEqual(plan["queues"][0]["account_username"], "daily_one")
        self.assertNotIn("material_url", plan["queues"][0])
        occupied = service.query_post_material_keys_request(
            {"material_ids": ["07001", "9999", "7003"]}
        )
        self.assertEqual(occupied, {"material_keys": ["7001", "7003"]})

        logs = service.query_post_logs_request(
            {"actor": self.admin, "scope": "all", "page": 1, "page_size": 10}
        )
        self.assertEqual(logs["pagination"]["total"], 3)
        self.assertNotIn("material_url", logs["items"][0])
        with self.assertRaises(service.ServiceError) as denied:
            service.query_post_logs_request(
                {"actor": self.owner, "scope": "all", "page": 1, "page_size": 10}
            )
        self.assertEqual(denied.exception.code, "x_admin_required")

        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client.configure_x_accounts_client(
                "http://127.0.0.1:%s" % server.server_address[1],
                service.INTERNAL_TOKEN,
                timeout=5,
            )
            self.assertEqual(
                client.query_x_post_material_keys(["7002", "9999"]),
                {"item": {"material_keys": ["7002"]}},
            )
            self.assertEqual(
                client.query_x_post_logs(
                    {"actor": self.admin, "scope": "all", "page": 1, "page_size": 10}
                )["pagination"]["total"],
                3,
            )
            self.assertEqual(
                client.query_x_post_runs(
                    {"actor": self.admin, "scope": "all", "page": 1, "page_size": 10}
                )["pagination"]["total"],
                1,
            )
            added_pool = client.add_x_post_material_pool(
                ["08001", "8002"],
                self.admin,
                validation_checks=[
                    {
                        "material_id": "8001",
                        "error_code": "",
                        "error_message": "",
                    },
                    {
                        "material_id": "8002",
                        "error_code": "",
                        "error_message": "",
                    },
                ],
            )
            self.assertEqual(added_pool["created_count"], 2)
            queried_pool = client.query_x_post_material_pool(
                {
                    "actor": self.admin,
                    "scope": "all",
                    "page": 1,
                    "page_size": 10,
                    "status": "unpublished",
                    "availability": "available",
                }
            )
            self.assertEqual(queried_pool["pagination"]["total"], 2)
            self.assertEqual(
                [item["material_id"] for item in queried_pool["items"]],
                ["8001", "8002"],
            )
            checked_pool = client.record_x_post_material_pool_checks(
                [
                    {
                        "pool_item_id": added_pool["items"][1]["id"],
                        "error_code": "material_not_found_or_ineligible",
                        "error_message": "素材不可用",
                    }
                ]
            )
            self.assertEqual(checked_pool["item"]["updated_count"], 1)
            self.assertEqual(
                client.query_x_post_material_pool(
                    {
                        "actor": self.admin,
                        "scope": "all",
                        "page": 1,
                        "page_size": 10,
                        "availability": "validation_failed",
                    }
                )["pagination"]["total"],
                1,
            )
            with self.assertRaises(client.XAccountsClientError) as pool_denied:
                client.add_x_post_material_pool(["8003"], self.owner)
            self.assertEqual(pool_denied.exception.code, "x_admin_required")
            with self.assertRaises(client.XAccountsClientError) as wrong_grant:
                client.add_x_post_material_pool(
                    ["8003"],
                    self.owner,
                    navigation_item="xPostLogs",
                )
            self.assertEqual(wrong_grant.exception.code, "x_admin_required")
            deleted_pool = client.delete_x_post_material_pool(
                added_pool["items"][0]["id"],
                self.admin,
            )
            self.assertTrue(deleted_pool["item"]["deleted"])
            self.assertEqual(
                client.query_x_post_material_pool(
                    {
                        "actor": self.admin,
                        "scope": "all",
                        "page": 1,
                        "page_size": 10,
                    }
                )["pagination"]["total"],
                1,
            )
            owner_added = client.add_x_post_material_pool(
                ["8003"],
                self.owner,
                validation_checks=[
                    {
                        "material_id": "8003",
                        "error_code": "",
                        "error_message": "",
                    }
                ],
                navigation_item="xPostMaterialPool",
            )
            self.assertEqual(owner_added["created_count"], 1)
            owner_pool = client.query_x_post_material_pool(
                {
                    "actor": self.owner,
                    "scope": "all",
                    "page": 1,
                    "page_size": 10,
                },
                navigation_item="xPostMaterialPool",
            )
            self.assertEqual(owner_pool["pagination"]["total"], 2)
            owner_deleted = client.delete_x_post_material_pool(
                owner_added["items"][0]["id"],
                self.owner,
                navigation_item="xPostMaterialPool",
            )
            self.assertTrue(owner_deleted["item"]["deleted"])
            not_overwritten = client.record_x_post_run_failure(
                {
                    "run_date": "2026-07-23",
                    "source_date": "2026-07-22",
                    "error_code": "late_failure",
                    "error_message": "must not overwrite",
                    "expected_count": 3,
                }
            )
            self.assertFalse(not_overwritten["item"]["recorded"])
            self.assertEqual(not_overwritten["item"]["status"], "queued")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_publish_by_queue_uses_frozen_row_and_returns_only_safe_fields(self):
        account = self.complete("3001", "queue_owner", actor=self.owner)
        payload = self.canary_payload(account)
        payload.update(
            {
                "account_username": "queue_owner",
                "page_name": "Queue Owner",
                "page_id": "3001",
                "run_date": "2026-07-23",
            }
        )
        from features.x_posts import XPostError, XPostStore

        queue = XPostStore(service.POST_DB_PATH).enqueue(payload)
        captured = {}

        @contextlib.contextmanager
        def credentials(account_id, actor, scope):
            captured["credentials"] = (account_id, dict(actor), scope)
            yield account, "access-secret"

        def fake_publish(**kwargs):
            captured["publish"] = dict(kwargs)
            return {
                "status": "published",
                "log_id": 8,
                "short_url": "https://ai.yingliangads.com/s2l/8.html",
                "post_id": "9001",
                "preview_url": "https://x.com/queue_owner/status/9001",
                "access_token": "must-not-escape",
            }

        with mock.patch.object(service, "verify_account", return_value=account), mock.patch.object(
            service, "publish_credentials", credentials
        ), mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, XPostStore, fake_publish),
        ):
            result = service.publish_queue_request(queue["id"])
        self.assertEqual(result["post_id"], "9001")
        self.assertNotIn("access_token", result)
        self.assertEqual(captured["publish"]["queue_id"], queue["id"])
        self.assertEqual(captured["publish"]["access_token"], "access-secret")

    def test_daily_publish_scope_rejects_non_daily_queue_before_log_reservation(self):
        account = self.complete("3000", "not_a_daily_queue", actor=self.owner)
        payload = self.canary_payload(account)
        payload.update(
            {
                "account_username": "not_a_daily_queue",
                "page_name": "Not A Daily Queue",
                "page_id": "3000",
            }
        )
        from features.x_posts import XPostStore

        queue = XPostStore(service.POST_DB_PATH).enqueue(payload)
        service.DAILY_ACCOUNT_IDS = (account["id"], account["id"] + 1, account["id"] + 2)
        with self.assertRaises(service.ServiceError) as denied:
            service.publish_queue_request(queue["id"], service.DAILY_ACCOUNT_IDS)
        self.assertEqual(denied.exception.code, "x_daily_account_scope_denied")
        with contextlib.closing(sqlite3.connect(service.POST_DB_PATH)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_publish_log"
                ).fetchone()[0],
                0,
            )

    def test_published_queue_replay_short_circuits_before_token_verification(self):
        account = self.complete("3002", "published_owner", actor=self.owner)
        payload = self.canary_payload(account)
        payload.update(
            {
                "account_username": "published_owner",
                "page_name": "Published Owner",
                "page_id": "3002",
                "run_date": "2026-07-23",
            }
        )
        from features.x_posts import XPostError, XPostStore, publish_canary

        store = XPostStore(service.POST_DB_PATH)
        queue = store.enqueue(payload)
        log = store.reserve_log(queue["id"])
        store.prepare_log(
            log["id"],
            "https://example.invalid/frozen",
            "https://ai.yingliangads.com/s2l/%s.html" % log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nFrozen" % log["id"],
        )
        store.mark_publishing(log["id"])
        store.mark_media_uploaded(log["id"], "media3002")
        store.mark_published(
            log["id"],
            "media3002",
            "9003002",
            "https://x.com/published_owner/status/9003002",
        )
        with mock.patch.object(
            service,
            "verify_account",
            side_effect=AssertionError("published replay must not verify token"),
        ), mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, XPostStore, publish_canary),
        ):
            result = service.publish_queue_request(queue["id"])
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["post_id"], "9003002")

    def test_pre_publish_account_failure_is_persisted_as_known_failure(self):
        account = self.complete("3003", "disabled_after_plan", actor=self.owner)
        payload = self.canary_payload(account)
        payload.update(
            {
                "account_username": "disabled_after_plan",
                "page_name": "Disabled After Plan",
                "page_id": "3003",
                "run_date": "2026-07-23",
            }
        )
        from features.x_posts import XPostError, XPostStore, publish_canary

        store = XPostStore(service.POST_DB_PATH)
        queue = store.enqueue(payload)
        with mock.patch.object(
            service,
            "verify_account",
            side_effect=service.ServiceError(
                "x_account_disabled", "disabled after plan", 409
            ),
        ), mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, XPostStore, publish_canary),
        ):
            with self.assertRaises(service.ServiceError) as caught:
                service.publish_queue_request(queue["id"])
        self.assertEqual(caught.exception.code, "x_account_disabled")
        log = store.query_logs(
            {"account_id": account["id"], "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["error_code"], "x_account_disabled")
        self.assertFalse(log["unknown_outcome"])

    def test_publish_verify_rate_limit_marks_daily_run_stopped(self):
        accounts = [
            self.complete("3101", "rate_one", actor=self.owner),
            self.complete("3102", "rate_two", actor=self.owner),
            self.complete("3103", "rate_three", actor=self.owner),
        ]
        from features.x_posts import XPostStore

        candidates = []
        for rank, account in enumerate(accounts, 1):
            material_id = str(93100 + rank)
            candidate = self.canary_payload(account)
            candidate.update(
                {
                    "account_username": account["username"],
                    "page_name": account["display_name"],
                    "page_id": account["x_user_id"],
                    "material_id": material_id,
                    "content_id": "content-" + material_id,
                    "material_name": "material-" + material_id,
                    "candidate_rank": rank,
                    "spend": 100 - rank,
                    "preflight_sha256": ("%064x" % int(material_id))[-64:],
                    "preflight_size": 5,
                    "compliance_counts": {
                        "facebook_violation_count": 0,
                        "tiktok_violation_count": 0,
                        "twitter_violation_count": 0,
                        "resource_audit_count": 0,
                        "dangerous_tag_count": 0,
                    },
                }
            )
            candidates.append(candidate)
        store = XPostStore(service.POST_DB_PATH)
        plan = store.create_daily_plan(
            "2026-07-23",
            "2026-07-22",
            candidates,
        )
        first_queue = plan["queues"][0]
        with mock.patch.object(
            service,
            "verify_account",
            side_effect=service.ServiceError(
                "x_post_rate_limited",
                "X API usage cap",
                429,
            ),
        ):
            with self.assertRaises(service.ServiceError) as limited:
                service.publish_queue_request(first_queue["id"])
        self.assertEqual(limited.exception.code, "x_post_rate_limited")
        self.assertEqual(limited.exception.status, 429)
        log = store.query_logs(
            {"account_id": accounts[0]["id"], "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["error_code"], "x_post_rate_limited")
        self.assertEqual(store.get_run(plan["id"])["status"], "stopped")

    def test_frozen_username_mismatch_after_reservation_is_known_failure(self):
        account = self.complete("3004", "renamed_after_plan", actor=self.owner)
        payload = self.canary_payload(account)
        payload.update(
            {
                "account_username": "frozen_original_name",
                "page_name": "Frozen Original Name",
                "page_id": "3004",
                "material_id": "93004",
                "run_date": "2026-07-23",
            }
        )
        from features.x_posts import XPostError, XPostStore, publish_canary

        store = XPostStore(service.POST_DB_PATH)
        queue = store.enqueue(payload)

        @contextlib.contextmanager
        def credentials(_account_id, _actor, _scope):
            yield account, "access-secret"

        with mock.patch.object(
            service, "verify_account", return_value=account
        ), mock.patch.object(
            service, "publish_credentials", credentials
        ), mock.patch.object(
            service,
            "_x_posts_api",
            return_value=(XPostError, XPostStore, publish_canary),
        ):
            with self.assertRaises(service.ServiceError) as caught:
                service.publish_queue_request(queue["id"])
        self.assertEqual(caught.exception.code, "x_post_account_mismatch")
        log = store.query_logs(
            {"account_id": account["id"], "page": 1, "page_size": 10}
        )["items"][0]
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["error_code"], "x_post_account_mismatch")
        self.assertFalse(log["unknown_outcome"])

    def test_internal_api_requires_token_and_client_contract_matches(self):
        proxy_handlers = [
            handler
            for handler in client._NO_REDIRECT_OPENER.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertFalse(any(handler.proxies for handler in proxy_handlers))
        with self.assertRaises(ValueError):
            client.configure_x_accounts_client("https://example.com", "must-not-leak", timeout=5)
        item = self.complete(username="internal_contract")
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base_url + "/internal/config", timeout=5)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            client.configure_x_accounts_client(base_url, service.INTERNAL_TOKEN, timeout=5)
            config = client.get_x_accounts_config()
            self.assertTrue(config["configured"])
            self.assertEqual(config["scopes"], list(service.SCOPES))
            self.assertEqual(client.query_x_accounts(self.owner, "mine")["total"], 1)
            self.assertEqual(client.query_x_accounts(self.other_owner, "mine")["items"], [])
            self.assertEqual(client.query_x_accounts(self.admin, "all")["total"], 1)
            with self.assertRaises(client.XAccountsClientError) as denied_all:
                client.query_x_accounts(self.owner, "all")
            self.assertEqual(denied_all.exception.code, "x_admin_required")

            old_list_request = urllib.request.Request(
                base_url + "/internal/accounts",
                headers={"Authorization": "Bearer " + service.INTERNAL_TOKEN},
            )
            with self.assertRaises(urllib.error.HTTPError) as old_list:
                urllib.request.urlopen(old_list_request, timeout=5)
            self.assertEqual(old_list.exception.code, 404)
            old_list.exception.close()

            started = client.start_x_authorization(self.owner)
            self.assertIn("authorization_url", started)
            account_payload = {"data": {"id": item["x_user_id"], "username": "via-client", "name": "Via Client"}}
            with mock.patch.object(service, "user_request", return_value=account_payload):
                verified = client.verify_x_account(item["id"], self.owner)
            self.assertEqual(verified["item"]["username"], "via-client")
            token_before = service.token_path(item["x_user_id"]).read_bytes()
            logged_out = client.logout_x_account(item["id"], self.owner)
            self.assertEqual(logged_out["item"]["status"], "disabled")
            self.assertEqual(service.token_path(item["x_user_id"]).read_bytes(), token_before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_authorization_headers_are_not_forwarded_across_redirects(self):
        captured = {"authorization": None, "count": 0}

        class TargetHandler(service.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                captured["count"] += 1
                captured["authorization"] = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, _format, *_args):
                return

        target = service.ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        target_url = "http://127.0.0.1:%s/capture" % target.server_address[1]

        class RedirectHandler(service.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        redirect = service.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        redirect_url = "http://127.0.0.1:%s" % redirect.server_address[1]
        try:
            client.configure_x_accounts_client(redirect_url, "internal-secret", timeout=5)
            with self.assertRaises(client.XAccountsClientError):
                client.get_x_accounts_config()
            with self.assertRaises(service.ServiceError):
                service.http_json(redirect_url + "/upstream", headers={"Authorization": "Bearer user-secret"})
            self.assertEqual(captured["count"], 0)
            self.assertIsNone(captured["authorization"])
        finally:
            redirect.shutdown()
            redirect.server_close()
            target.shutdown()
            target.server_close()
            redirect_thread.join(timeout=5)
            target_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

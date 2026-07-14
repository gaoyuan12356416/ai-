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
        service.DATA_DIR = root
        service.DB_PATH = root / "accounts.sqlite3"
        service.TOKENS_DIR = root / "tokens"
        service.CLIENT_ID = "test-client-id"
        service.CLIENT_SECRET = "test-client-secret"
        service.INTERNAL_TOKEN = "test-internal-token"
        service.PUBLIC_BASE_URL = "https://ai.yingliangads.com/x-oauth"
        service.ADMIN_RETURN_URL = "https://ai.yingliangads.com/x-accounts.html"
        service.SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access", "media.write")
        service.STATE_TTL_SECONDS = 600
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
        with mock.patch.object(service, "revoke_token") as revoke_mock:
            with self.assertRaises(service.ServiceError) as logout_denied:
                service.logout_account(second["id"], self.owner)
        self.assertEqual(logout_denied.exception.code, "x_account_not_found")
        revoke_mock.assert_not_called()

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

    def test_logout_revokes_access_then_refresh_is_idempotent_and_preserves_metadata(self):
        item = self.complete(
            username="logout_user",
            account_fields={"public_metrics": {"followers_count": 88, "like_count": 99, "media_count": 7}},
        )
        token_file = service.token_path(item["x_user_id"])
        with mock.patch.object(service, "revoke_token", return_value={"revoked": True}) as revoke_mock:
            logged_out = service.logout_account(item["id"], self.owner)
        self.assertEqual([call.args[0] for call in revoke_mock.call_args_list], ["access-secret", "refresh-secret"])
        self.assertFalse(token_file.exists())
        self.assertEqual(logged_out["status"], "disconnected")
        self.assertEqual(logged_out["followers_count"], 88)
        self.assertEqual(logged_out["like_count"], 99)
        self.assertTrue(logged_out["disconnected_at"].endswith("Z"))
        self.assertEqual(logged_out["disconnected_by_user_id"], self.owner["user_id"])

        token_file.write_text("legacy-residual", encoding="utf-8")
        legacy_tombstone = service.TOKENS_DIR / (".%s.legacy.disconnecting" % token_file.name)
        legacy_tombstone.write_text("legacy-tombstone", encoding="utf-8")
        service.cleanup_disconnected_token_artifacts()
        self.assertFalse(token_file.exists())
        self.assertFalse(legacy_tombstone.exists())
        with mock.patch.object(service, "revoke_token") as second_revoke:
            second = service.logout_account(item["id"], self.owner)
        second_revoke.assert_not_called()
        self.assertEqual(second["status"], "disconnected")
        self.assertEqual(second["disconnected_at"], logged_out["disconnected_at"])

        with self.assertRaises(service.ServiceError) as verify_denied:
            service.verify_account(item["id"], self.owner)
        self.assertEqual(verify_denied.exception.code, "x_token_missing")
        self.assertEqual(service.find_account(item["id"])["status"], "disconnected")

    def test_logout_partial_failure_persists_pending_and_can_retry(self):
        item = self.complete(username="retry_logout")
        token_file = service.token_path(item["x_user_id"])
        before = token_file.read_bytes()
        _result, stale_state = self.new_state(self.owner)
        failure = service.ServiceError("x_upstream_error", "temporary", 502)
        with mock.patch.object(service, "revoke_token", side_effect=[{"revoked": True}, failure]) as revoke_mock:
            with self.assertRaises(service.ServiceError) as caught:
                service.logout_account(item["id"], self.owner)
        self.assertEqual(caught.exception.code, "x_disconnect_failed")
        self.assertEqual(revoke_mock.call_count, 2)
        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.read_bytes(), before)
        self.assertEqual(service.find_account(item["id"])["status"], "revoke_pending")

        with mock.patch.object(service, "token_request") as refresh_mock, mock.patch.object(service, "user_request") as user_mock:
            with self.assertRaises(service.ServiceError) as pending_verify:
                service.verify_account(item["id"], self.owner)
        self.assertEqual(pending_verify.exception.code, "x_disconnect_pending")
        refresh_mock.assert_not_called()
        user_mock.assert_not_called()

        with self.assertRaises(service.ServiceError) as pending_start:
            service.create_authorization(self.owner)
        self.assertEqual(pending_start.exception.code, "x_disconnect_pending")

        with mock.patch.object(service, "token_request") as token_exchange, mock.patch.object(service, "user_request") as user_lookup:
            with self.assertRaises(service.ServiceError) as pending_reauthorize:
                service.complete_authorization("replacement-code", stale_state)
        self.assertEqual(pending_reauthorize.exception.code, "x_disconnect_pending")
        token_exchange.assert_not_called()
        user_lookup.assert_not_called()
        self.assertEqual(token_file.read_bytes(), before)
        self.assertEqual(service.find_account(item["id"])["status"], "revoke_pending")

        with mock.patch.object(service, "revoke_token", return_value={"revoked": True}) as retry_revoke:
            disconnected = service.logout_account(item["id"], self.owner)
        self.assertEqual([call.args[0] for call in retry_revoke.call_args_list], ["access-secret", "refresh-secret"])
        self.assertEqual(disconnected["status"], "disconnected")
        self.assertFalse(token_file.exists())

        with contextlib.closing(sqlite3.connect(service.DB_PATH)) as conn:
            events = conn.execute(
                "SELECT outcome,error_code,actor_tenant_key FROM x_oauth_event WHERE event_type='logout' ORDER BY id"
            ).fetchall()
        self.assertIn(("failed", "x_disconnect_failed", self.owner["tenant_key"]), events)

    def test_logout_retains_unreadable_token_as_pending_for_operator_recovery(self):
        item = self.complete(username="corrupt_logout")
        token_file = service.token_path(item["x_user_id"])
        token_file.write_text("not-json", encoding="utf-8")
        with mock.patch.object(service, "revoke_token") as revoke_mock:
            with self.assertRaises(service.ServiceError) as caught:
                service.logout_account(item["id"], self.owner)
        self.assertEqual(caught.exception.code, "x_disconnect_failed")
        revoke_mock.assert_not_called()
        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.read_text(encoding="utf-8"), "not-json")
        self.assertEqual(service.find_account(item["id"])["status"], "revoke_pending")

    def test_logout_local_delete_failure_stays_pending_with_token_for_retry(self):
        item = self.complete(username="delete_failure")
        token_file = service.token_path(item["x_user_id"])
        before = token_file.read_bytes()
        with mock.patch.object(service, "revoke_token", return_value={"revoked": True}) as revoke_mock, mock.patch.object(service, "delete_token_artifacts", side_effect=OSError("read only")):
            with self.assertRaises(service.ServiceError) as caught:
                service.logout_account(item["id"], self.owner)
        self.assertEqual(caught.exception.code, "x_disconnect_failed")
        self.assertEqual(revoke_mock.call_count, 2)
        self.assertTrue(token_file.exists())
        self.assertEqual(token_file.read_bytes(), before)
        self.assertEqual(service.find_account(item["id"])["status"], "revoke_pending")

    def test_same_owner_can_reauthorize_disconnected_account(self):
        original = self.complete(username="before_logout")
        first_authorized_at = original["first_authorized_at"]
        with mock.patch.object(service, "revoke_token", return_value={"revoked": True}):
            service.logout_account(original["id"], self.owner)

        restored = self.complete(username="after_logout", actor=dict(self.owner, name="Owner Renamed"))
        self.assertEqual(restored["id"], original["id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["username"], "after_logout")
        self.assertEqual(restored["first_authorized_at"], first_authorized_at)
        self.assertEqual(restored["disconnected_at"], "")
        self.assertEqual(restored["owner_name"], self.owner["name"])
        self.assertEqual(restored["authorized_by_name"], "Owner Renamed")
        self.assertTrue(service.token_path(restored["x_user_id"]).exists())

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
        with mock.patch.object(service, "token_request", side_effect=blocked_exchange), mock.patch.object(service, "user_request", return_value=account), mock.patch.object(service, "revoke_token", return_value={"revoked": True}) as revoke_mock:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                callback_future = pool.submit(service.complete_authorization, "owner-lock-code", state)
                self.assertTrue(exchange_started.wait(timeout=5))
                logout_future = pool.submit(service.logout_account, item["id"], self.owner)
                time.sleep(0.1)
                self.assertFalse(logout_future.done())
                revoke_mock.assert_not_called()
                release_exchange.set()
                callback_result = callback_future.result(timeout=5)
                logout_result = logout_future.result(timeout=5)

        self.assertEqual(callback_result["username"], "owner_locked")
        self.assertEqual(logout_result["status"], "disconnected")
        self.assertEqual(
            [call.args[0] for call in revoke_mock.call_args_list],
            ["owner-lock-access", "owner-lock-refresh"],
        )
        self.assertFalse(service.token_path(item["x_user_id"]).exists())

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

    def test_internal_api_requires_token_and_client_contract_matches(self):
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
            with mock.patch.object(service, "revoke_token", return_value={"revoked": True}) as revoke_mock:
                logged_out = client.logout_x_account(item["id"], self.owner)
            self.assertEqual(logged_out["item"]["status"], "disconnected")
            self.assertEqual(revoke_mock.call_count, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_revoke_request_uses_confidential_client_auth_and_form_body(self):
        with mock.patch.object(service, "http_json", return_value={"revoked": True}) as request_mock:
            result = service.revoke_token("token-value")
        self.assertEqual(result, {"revoked": True})
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], service.REVOKE_URL)
        self.assertEqual(kwargs["method"], "POST")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(
            urllib.parse.parse_qs(kwargs["body"].decode("utf-8")),
            {"token": ["token-value"]},
        )
        self.assertTrue(kwargs["allow_revoked"])
        self.assertTrue(kwargs["allow_non_json"])

    def test_revoke_http_accepts_empty_or_non_json_2xx_body(self):
        class RevokeResponseHandler(service.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path == "/empty":
                    self.send_response(204)
                    self.end_headers()
                    return
                body = b"revoked"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = service.ThreadingHTTPServer(("127.0.0.1", 0), RevokeResponseHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % server.server_address[1]
        try:
            for path in ("/empty", "/text"):
                with self.subTest(path=path):
                    result = service.http_json(
                        base_url + path,
                        method="POST",
                        body=b"token=redacted",
                        allow_non_json=True,
                    )
                    self.assertEqual(result, {})
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

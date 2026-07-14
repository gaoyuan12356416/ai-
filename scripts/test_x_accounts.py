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

    def tearDown(self):
        self.temp_dir.cleanup()

    def new_state(self, actor=None):
        result = service.create_authorization(actor or {"user_id": "u-1", "name": "测试用户", "email": "a@example.com"})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result["authorization_url"]).query)
        return result, query["state"][0]

    def complete(self, x_user_id="123456789", username="tester", scope=None, expires_in=7200):
        _result, state = self.new_state()
        token = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": scope or " ".join(service.SCOPES),
        }
        account = {"data": {"id": x_user_id, "username": username, "name": "Test User", "profile_image_url": "https://pbs.twimg.com/a.jpg"}}
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
            row = conn.execute("SELECT state_hash,code_verifier,actor_user_id FROM x_oauth_state").fetchone()
        self.assertEqual(row[0], service.state_digest(raw_state))
        self.assertNotEqual(row[0], raw_state)
        self.assertTrue(row[1])
        self.assertEqual(row[2], "u-1")
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
        result = service.list_accounts()
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
        result = service.list_accounts()
        self.assertEqual(result["total"], 2)
        self.assertEqual({item["x_user_id"] for item in result["items"]}, {"111111111", "222222222"})
        self.assertTrue((service.TOKENS_DIR / "111111111.json").exists())
        self.assertTrue((service.TOKENS_DIR / "222222222.json").exists())

    def test_missing_scope_is_visible(self):
        granted = "tweet.read tweet.write users.read offline.access"
        item = self.complete(scope=granted)
        self.assertEqual(item["status"], "scope_missing")
        self.assertEqual(item["missing_scopes"], ["media.write"])

    def test_required_scopes_cannot_be_removed_by_environment_config(self):
        original_scopes = service.SCOPES
        service.SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
        try:
            self.assertFalse(service.config_payload()["configured"])
            with self.assertRaises(service.ServiceError) as caught:
                service.create_authorization({"user_id": "u-1"})
            self.assertEqual(caught.exception.code, "x_oauth_not_configured")
        finally:
            service.SCOPES = original_scopes

    def test_verify_rejects_empty_or_different_token_owner(self):
        for payload in ({"data": {}}, {"data": {"id": "987654321", "username": "wrong"}}):
            with self.subTest(payload=payload):
                item = self.complete()
                with mock.patch.object(service, "user_request", return_value=payload):
                    with self.assertRaises(service.ServiceError) as caught:
                        service.verify_account(item["id"])
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
            verified = service.verify_account(item["id"])
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
                service.verify_account(item["id"])
        self.assertEqual(service.find_account(item["id"])["status"], "revoked")

    def test_authorization_and_verify_events_are_sanitized(self):
        item = self.complete()
        with mock.patch.object(service, "user_request", side_effect=service.ServiceError("x_token_revoked", "X授权已失效", 409)):
            with self.assertRaises(service.ServiceError):
                service.verify_account(item["id"])
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
            verified = service.verify_account(item["id"])
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
            return service.verify_account(item["id"])

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
        _result, new_state = self.new_state({"user_id": "u-new", "name": "重新授权用户"})
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
                verify_future = pool.submit(service.verify_account, item["id"])
                self.assertTrue(refresh_started.wait(timeout=5))
                callback_future = pool.submit(service.complete_authorization, "new-code", new_state)
                verify_future.result(timeout=5)
                callback_future.result(timeout=5)
        final_item = service.find_account(item["id"])
        final_token = json.loads((service.TOKENS_DIR / "123456789.json").read_text(encoding="utf-8"))
        self.assertEqual(final_item["username"], "reauthorized")
        self.assertEqual(final_token["refresh_token"], "reauthorized-refresh")

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
            self.assertEqual(client.list_x_accounts()["items"], [])
            started = client.start_x_authorization({"user_id": "u-2", "name": "后台用户"})
            self.assertIn("authorization_url", started)
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

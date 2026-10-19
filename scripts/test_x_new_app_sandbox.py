"""Isolation, browser boundary and mocked OAuth acceptance checks; no X writes."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

import x_new_app_sandbox as sandbox


class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="x-app-sandbox-test-")
        os.environ["X_INTERNAL_TOKEN"] = "inherited-production-sentinel"
        os.environ["X_CLIENT_ID"] = "inherited-production-client"
        sandbox.prepare(cls.temp.name)
        cls.server = sandbox.ThreadingHTTPServer(("127.0.0.1", 0), sandbox.make_handler())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        assert Path(cls.temp.name).resolve().name.startswith("x-app-sandbox-test-")
        cls.temp.cleanup()

    def setUp(self):
        conn = sandbox.oauth.db_connect()
        try:
            for table in ("x_authorized_account", "x_oauth_state", "x_oauth_event"):
                conn.execute("DELETE FROM " + table)
            conn.commit()
        finally:
            conn.close()
        for path in sandbox.oauth.TOKENS_DIR.glob("*.json"):
            path.unlink()
        sandbox.CONFIG.clear()
        sandbox.oauth.CLIENT_ID = sandbox.oauth.CLIENT_SECRET = ""

    def request(self, path, body=None, *, host="127.0.0.1:18880", session=True, origin=sandbox.BASE, csrf=True, guard=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": host}
        if guard:
            headers["X-Sandbox-Callback-Guard"] = sandbox.BROWSER_GUARD
        if session:
            headers["Cookie"] = "x_sandbox=" + sandbox.SESSION
        if body is not None:
            headers.update({"Origin": origin, "Content-Type": "application/json"})
            if csrf:
                headers["X-Sandbox-CSRF"] = sandbox.CSRF
        conn.request("POST" if body is not None else "GET", path,
                     body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def configure(self):
        return self.request("/api/config", {"client_id": "fake-sandbox-client", "client_secret": "fake-sandbox-secret"})

    def test_inherited_configuration_is_cleared_and_no_posting_tables_exist(self):
        self.assertNotIn("X_INTERNAL_TOKEN", os.environ)
        self.assertEqual(sandbox.oauth.CLIENT_ID, "")
        self.assertTrue(sandbox.oauth.DB_PATH.is_relative_to(Path(self.temp.name)))
        self.assertTrue(sandbox.oauth.TOKENS_DIR.is_relative_to(Path(self.temp.name)))
        conn = sandbox.oauth.db_connect()
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertFalse(any(name.startswith("x_post_") for name in tables))

    def test_host_session_origin_and_csrf_are_required(self):
        for kwargs in ({"host": "evil.example:18880"}, {"session": False},
                       {"origin": "https://evil.example"}, {"csrf": False}):
            status, _, _ = self.request("/api/config", {}, **kwargs)
            self.assertEqual(status, 403)
        self.assertEqual(self.request("/api/status", session=False)[0], 403)

    def test_keys_cannot_substitute_for_oauth2_and_secrets_are_not_returned(self):
        sandbox.CONFIG.update(consumer_key="fake-consumer", consumer_secret="fake-consumer-secret", bearer_token="fake-app-bearer")
        self.assertEqual(self.request("/api/authorize", {})[0], 503)
        self.assertEqual(self.configure()[0], 200)
        status, _, raw = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["configured"])
        for secret in (b"fake-sandbox-secret", b"fake-consumer", b"fake-app-bearer"):
            self.assertNotIn(secret, raw)

    def test_production_and_publish_routes_are_disabled(self):
        for path in ("/internal/authorize", "/internal/accounts/query", "/internal/posts/queue/1/publish", "/api/publish"):
            self.assertEqual(self.request(path, {})[0], 404)
            self.assertEqual(self.request(path)[0], 404)

    def test_mocked_real_oauth_callback_verify_and_single_account(self):
        self.assertEqual(self.configure()[0], 200)
        status, _, raw = self.request("/api/authorize", {})
        self.assertEqual(status, 200)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(json.loads(raw)["authorization_url"]).query)
        self.assertEqual(params["redirect_uri"], [sandbox.CALLBACK_BASE + "/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        callback = "/x-oauth/callback?" + urllib.parse.urlencode({"state": params["state"][0], "code": "fake-code"})
        token = {"access_token": "fake-user-access", "refresh_token": "fake-user-refresh",
                 "token_type": "bearer", "expires_in": 7200, "scope": " ".join(sandbox.oauth.SCOPES)}
        user = {"data": {"id": "123456789", "username": "sandbox_only", "name": "Sandbox Only", "subscription_type": "none"}}
        with patch.object(sandbox.oauth, "token_request", return_value=token) as token_call, patch.object(sandbox.oauth, "user_request", return_value=user):
            code, headers, _ = self.request(callback)
            self.assertEqual(code, 302)
            self.assertIn("oauth=success", headers["Location"])
            self.assertEqual(token_call.call_args.args[0]["redirect_uri"], sandbox.CALLBACK_BASE + "/callback")
            code, _, raw = self.request("/api/verify", {})
            self.assertEqual(code, 200)
            self.assertNotIn(b"fake-user-access", raw)
            self.assertNotIn(b"fake-user-refresh", raw)
        item = sandbox.accounts()[0]
        self.assertEqual(item["username"], "sandbox_only")
        self.assertFalse(item["publish_approved"])
        self.assertEqual(item["missing_scopes"], [])
        self.assertEqual(self.request("/api/authorize", {})[0], 409)
        self.assertEqual(self.request("/api/config", {"client_id": "another-app", "client_secret": "fake-new-secret"})[0], 409)

    def test_ordinary_browser_cannot_start_or_consume_production_callback(self):
        self.configure()
        self.assertEqual(self.request("/api/authorize", {}, guard=False)[0], 403)
        self.assertEqual(self.request("/x-oauth/callback?code=fake&state=invalid", guard=False)[0], 403)

    def test_invalid_callback_is_redirected_without_creating_an_account(self):
        code, headers, raw = self.request("/x-oauth/callback?code=fake&state=invalid")
        self.assertEqual(code, 302)
        self.assertIn("oauth=error", headers["Location"])
        self.assertEqual(sandbox.accounts(), [])


if __name__ == "__main__":
    unittest.main()

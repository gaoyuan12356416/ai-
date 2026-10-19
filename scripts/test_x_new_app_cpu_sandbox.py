"""Isolated CPU authorization/authentication checks. No upstream X calls."""
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

import x_new_app_cpu_sandbox as cpu

ACTOR = {"tenant_key": "fake-tenant", "user_id": "fake-operator", "name": "Test", "email": "", "role": "admin"}


class CPUChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="x-cpu-sandbox-test-")
        cpu.prepare(cls.temp.name)
        cpu.local.oauth.CLIENT_ID = "fake-client"
        cpu.local.oauth.CLIENT_SECRET = "fake-secret"
        cls.server = cpu.ThreadingHTTPServer(("127.0.0.1", 0), cpu.make_handler())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def setUp(self):
        conn = cpu.local.oauth.db_connect()
        try:
            for table in ("x_authorized_account", "x_oauth_state", "x_oauth_event"):
                conn.execute("DELETE FROM " + table)
            conn.commit()
        finally:
            conn.close()

    def request(self, path, body=None, *, cookie=True, csrf=True, origin=cpu.SITE, host="ai.yingliangads.com"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": host}
        if cookie:
            headers["Cookie"] = cpu.COOKIE_NAME + "=" + cpu.local.SESSION
        if body is not None:
            headers.update({"Content-Type": "application/json", "Origin": origin})
            if csrf:
                headers["X-Sandbox-CSRF"] = cpu.local.CSRF
        conn.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_anonymous_root_uses_existing_feishu_login_and_callback_is_gated(self):
        error = cpu.local.oauth.ServiceError("auth_required", "login", 401)
        with patch.object(cpu, "read_actor", side_effect=error):
            code, headers, _ = self.request("/", cookie=False)
            self.assertEqual(code, 302)
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["next"], ["/x-app-test/"])
            self.assertEqual(self.request("/callback?state=" + cpu.STATE_PREFIX + "A" * 43)[0], 401)

    def test_admin_page_session_csrf_and_write_route_boundaries(self):
        with patch.object(cpu, "read_actor", return_value=ACTOR):
            code, headers, body = self.request("/", cookie=False)
            self.assertEqual(code, 200)
            self.assertIn("Secure", headers["Set-Cookie"])
            self.assertNotIn(b"fake-secret", body)
            for kwargs in ({"cookie": False}, {"csrf": False}, {"origin": "https://evil.example"}, {"host": "evil.example"}):
                self.assertEqual(self.request("/api/authorize", {}, **kwargs)[0], 403)
            for path in ("/internal/authorize", "/api/publish", "/api/config"):
                self.assertEqual(self.request(path, {})[0], 404)

    def test_prefixed_state_roundtrip_keeps_official_redirect_and_separate_tokens(self):
        with patch.object(cpu, "read_actor", return_value=ACTOR):
            code, _, raw = self.request("/api/authorize", {})
            self.assertEqual(code, 200)
            auth = json.loads(raw)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(auth["authorization_url"]).query)
            state = query["state"][0]
            self.assertRegex(state, "^" + cpu.STATE_PREFIX + "[A-Za-z0-9_-]{43}$")
            self.assertEqual(query["redirect_uri"], ["https://ai.yingliangads.com/x-oauth/callback"])
            token = {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 7200, "scope": " ".join(cpu.local.oauth.SCOPES)}
            user = {"data": {"id": "123456789", "username": "cpu_test", "name": "CPU Test", "subscription_type": "none"}}
            with patch.object(cpu.local.oauth, "token_request", return_value=token) as token_call, patch.object(cpu.local.oauth, "user_request", return_value=user):
                code, headers, _ = self.request("/callback?" + urllib.parse.urlencode({"state": state, "code": "fake-code"}))
                self.assertEqual(code, 302)
                self.assertEqual(headers["Location"], cpu.PUBLIC + "/?oauth=success")
                self.assertEqual(token_call.call_args.args[0]["redirect_uri"], "https://ai.yingliangads.com/x-oauth/callback")
                self.assertEqual(self.request("/api/verify", {})[0], 200)
            items = cpu.local.accounts()
            self.assertEqual(len(items), 1)
            self.assertFalse(items[0]["publish_approved"])
            self.assertNotIn("fake-access", json.dumps(items))
            self.assertEqual(self.request("/api/authorize", {})[0], 409)
            conn = cpu.local.oauth.db_connect()
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name LIKE 'x_post_%'").fetchone()[0], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

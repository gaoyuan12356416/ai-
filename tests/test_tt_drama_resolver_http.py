import http.client
import json
import threading
import unittest

import app
from features.tt_drama_resolver.service import (
    ResolveOutcome,
    ResolverUnavailableError,
)


VALID_CONTENT_ID = "l9rP6ey2CB"
MISSING_CONTENT_ID = "ZZZZZZZZZZ_NOT_REAL_2026"


class _AllowLimiter:
    def allow(self, _key):
        return True


class _DenyLimiter:
    def allow(self, _key):
        return False


class _FakeResolver:
    def __init__(self):
        self.calls = []
        self.unavailable = False

    def resolve(self, content_id):
        self.calls.append(content_id)
        if self.unavailable:
            raise ResolverUnavailableError("unavailable")
        if content_id == VALID_CONTENT_ID:
            return ResolveOutcome(
                True,
                {
                    "content_id": content_id,
                    "title": "Example Drama",
                    "description": "Description",
                    "cover_url": "https://static-v1.mydramawave.com/cover.jpg",
                    "country": "us",
                    "language": "en",
                    "episode_count": 80,
                    "source_updated_at": "2026-07-27T00:00:00",
                },
                "MISS",
            )
        return ResolveOutcome(False, None, "MISS")


class ResolverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_resolver = app.TT_DRAMA_RESOLVER
        cls.original_limiter = app.TT_DRAMA_RESOLVER_RATE_LIMITER
        cls.original_request_gate = app.TT_DRAMA_RESOLVER_REQUEST_GATE
        cls.resolver = _FakeResolver()
        app.TT_DRAMA_RESOLVER = cls.resolver
        app.TT_DRAMA_RESOLVER_RATE_LIMITER = _AllowLimiter()
        cls.server = app.ThreadedHTTPServer(
            ("127.0.0.1", 0), app.DramaMaterialHandler
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        app.TT_DRAMA_RESOLVER = cls.original_resolver
        app.TT_DRAMA_RESOLVER_RATE_LIMITER = cls.original_limiter
        app.TT_DRAMA_RESOLVER_REQUEST_GATE = cls.original_request_gate

    def setUp(self):
        self.resolver.calls.clear()
        self.resolver.unavailable = False
        app.TT_DRAMA_RESOLVER_RATE_LIMITER = _AllowLimiter()
        app.TT_DRAMA_RESOLVER_REQUEST_GATE = threading.BoundedSemaphore(4)

    def request(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("X-TT-Drama-Cache", headers)
            timing = headers["Server-Timing"]
            self.assertTrue(timing.startswith("tt-drama-resolver;dur="))
            self.assertGreaterEqual(float(timing.rsplit("=", 1)[1]), 0.0)
            return response.status, headers, json.loads(body)
        finally:
            connection.close()

    def test_found_response_is_public_observable_and_no_store(self):
        status, headers, payload = self.request(
            "/api/public/tt-drama/resolve?content_id=" + VALID_CONTENT_ID
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["data"]["content_id"], VALID_CONTENT_ID)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-TT-Drama-Cache"], "MISS")
        self.assertIn("tt-drama-resolver;dur=", headers["Server-Timing"])
        self.assertEqual(self.resolver.calls, [VALID_CONTENT_ID])

    def test_not_found_is_404_and_does_not_return_data(self):
        status, headers, payload = self.request(
            "/api/public/tt-drama/resolve?content_id=" + MISSING_CONTENT_ID
        )
        self.assertEqual(status, 404)
        self.assertFalse(payload["found"])
        self.assertEqual(payload["error"], "not_found")
        self.assertNotIn("data", payload)
        self.assertEqual(headers["X-TT-Drama-Cache"], "MISS")

    def test_invalid_or_extra_query_is_400_without_resolver_call(self):
        for query in (
            "",
            "?content_id=short",
            "?content_id=%20" + VALID_CONTENT_ID,
            "?content_id=" + VALID_CONTENT_ID + "&content_id=" + VALID_CONTENT_ID,
            "?content_id=" + VALID_CONTENT_ID + "&af_adset_id=XXX",
        ):
            status, headers, payload = self.request(
                "/api/public/tt-drama/resolve" + query
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_request")
            self.assertEqual(headers["X-TT-Drama-Cache"], "BYPASS")
        self.assertEqual(self.resolver.calls, [])

    def test_rate_limit_and_dependency_failure_have_distinct_statuses(self):
        app.TT_DRAMA_RESOLVER_RATE_LIMITER = _DenyLimiter()
        status, headers, payload = self.request(
            "/api/public/tt-drama/resolve?content_id=" + VALID_CONTENT_ID
        )
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"], "rate_limited")
        self.assertEqual(headers["X-TT-Drama-Cache"], "RATE_LIMITED")
        self.assertEqual(self.resolver.calls, [])

        app.TT_DRAMA_RESOLVER_RATE_LIMITER = _AllowLimiter()
        self.resolver.unavailable = True
        status, headers, payload = self.request(
            "/api/public/tt-drama/resolve?content_id=" + VALID_CONTENT_ID
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "resolver_unavailable")
        self.assertEqual(headers["X-TT-Drama-Cache"], "ERROR")

    def test_global_inflight_gate_fails_fast(self):
        gate = threading.BoundedSemaphore(1)
        self.assertTrue(gate.acquire(blocking=False))
        app.TT_DRAMA_RESOLVER_REQUEST_GATE = gate
        try:
            status, headers, payload = self.request(
                "/api/public/tt-drama/resolve?content_id=" + VALID_CONTENT_ID
            )
        finally:
            gate.release()
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "resolver_overloaded")
        self.assertEqual(headers["X-TT-Drama-Cache"], "OVERLOADED")
        self.assertEqual(self.resolver.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

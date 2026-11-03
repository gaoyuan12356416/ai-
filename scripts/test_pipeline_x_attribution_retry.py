"""Only read-only attribution resolution may retry; link/X writes stay single."""
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from features.pipeline_dispatch import x_native
from features.x_posts.attribution import AttributionError
from scripts import test_pipeline_x_native as fixture


class AttributionRetryTests(unittest.TestCase):
    verify = fixture.NativeTests.verify
    queue = fixture.NativeTests.queue
    job = fixture.NativeTests.job
    preflight = fixture.NativeTests.preflight

    def setUp(self):
        fixture.NativeTests.setUp(self)
        self.q = self.queue(auto=True)
        prep, _, artifact = self.preflight(self.q)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        self.pub = self.job(self.q, "publish")
        with self.bridge.connect() as c:
            self.log = dict(c.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (self.q["id"],)).fetchone())
        self.context = {
            "version": "x-attribution-v2", "channel_id": "21", "sub_user_id": "42",
            "af_c_id": str(self.q["id"]), "tenant": "tenant", "owner": "owner",
            "channel_name": "channel", "material_id": "501", "material_name": "video",
            "content_id": "episode", "language": "en", "drama_name": "drama", "tag": "tag",
            "frozen_at": self.log["created_at"],
        }
        env = mock.patch.dict(os.environ, {"X_POST_ATTRIBUTION_V2_ENABLED": "1", "X_INTERNAL_TOKEN": "private-token-" + "x" * 40})
        env.start(); self.addCleanup(env.stop)
        sleep = mock.patch.object(x_native.time, "sleep")
        self.sleep = sleep.start(); self.addCleanup(sleep.stop)

    def response(self, context=None, raw=None):
        response = io.BytesIO(raw if raw is not None else json.dumps({"context": context or self.context}).encode())
        response.status = 200
        return response

    def error(self, status):
        return urllib.error.HTTPError("http://localhost/private-secret", status, "private-error-token", {}, io.BytesIO(b"private-response-token"))

    def assert_no_write(self):
        with self.bridge.connect() as c:
            self.assertFalse(c.execute("SELECT 1 FROM pipeline_x_claim WHERE stage='publish'").fetchone())
            self.assertFalse(c.execute("SELECT 1 FROM pipeline_x_operation").fetchone())
            log = c.execute("SELECT attempt_count,post_text,long_url,short_url FROM x_post_publish_log WHERE queue_id=?", (self.q["id"],)).fetchone()
            self.assertEqual(tuple(log), (0, "", "", ""))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='x_post_link_attribution'").fetchone():
                self.assertFalse(c.execute("SELECT 1 FROM x_post_link_attribution").fetchone())
        x_native.posts.write_short_redirect.assert_not_called()

    def test_503_then_reset_then_success_freezes_and_builds_links_once(self):
        effects = [self.error(503), urllib.error.URLError(ConnectionResetError("private-network-token")), self.response()]
        with mock.patch.object(x_native.urllib.request, "urlopen", side_effect=effects) as opener:
            descriptor = self.bridge.reserve(self.pub, self.pub)
            self.assertEqual(self.bridge.reserve(self.pub, self.pub), descriptor)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([c.args[0].data for c in opener.call_args_list], [json.dumps({"queue_id": self.q["id"], "log_id": self.log["id"]}).encode()] * 3)
        self.assertTrue(all(c.args[0].full_url == "http://127.0.0.1:8787/internal/x-posts/attribution/resolve" for c in opener.call_args_list))
        self.assertTrue(all(0 < c.kwargs["timeout"] <= 20 for c in opener.call_args_list))
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5), mock.call(1.0)])
        x_native.posts.write_short_redirect.assert_called_once()
        with self.bridge.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_link_attribution").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT attempt_count FROM x_post_publish_log WHERE queue_id=?", (self.q["id"],)).fetchone()[0], 0)
            self.assertFalse(c.execute("SELECT 1 FROM pipeline_x_operation").fetchone())

    def test_exhausted_5xx_has_three_calls_no_write_and_safe_log(self):
        with mock.patch.object(x_native.urllib.request, "urlopen", side_effect=[self.error(503) for _ in range(3)]) as opener:
            with self.assertLogs(x_native.__name__, level="WARNING") as logs:
                with self.assertRaises(AttributionError): self.bridge.reserve(self.pub, self.pub)
        self.assertEqual(opener.call_count, 3)
        self.assertIn("http=503 attempts=3", logs.output[0])
        self.assertNotIn("private-", logs.output[0])
        self.assert_no_write()

    def test_4xx_and_nontransient_transport_do_not_retry(self):
        for error in [self.error(code) for code in (400, 401, 403, 404, 409, 429)] + [urllib.error.URLError(ValueError("private-reason"))]:
            with self.subTest(error=type(error).__name__):
                self.sleep.reset_mock()
                with mock.patch.object(x_native.urllib.request, "urlopen", side_effect=error) as opener:
                    with self.assertLogs(x_native.__name__, level="WARNING"):
                        with self.assertRaises(AttributionError): self.bridge.reserve(self.pub, self.pub)
                self.assertEqual(opener.call_count, 1); self.sleep.assert_not_called(); self.assert_no_write()

    def test_invalid_context_identity_or_response_does_not_retry(self):
        responses = [self.response(dict(self.context, af_c_id="999999")), self.response(dict(self.context, frozen_at="2020-01-01T00:00:00Z")),
                     self.response(raw=b"not-json"), self.response(raw=b"{}"), self.response(raw=b"x" * 65537)]
        for response in responses:
            with self.subTest(size=len(response.getvalue())):
                with mock.patch.object(x_native.urllib.request, "urlopen", return_value=response) as opener:
                    with self.assertRaises(AttributionError): self.bridge.reserve(self.pub, self.pub)
                self.assertEqual(opener.call_count, 1); self.sleep.assert_not_called(); self.assert_no_write()

    def test_network_timeout_or_refused_is_bounded(self):
        for error in (TimeoutError("private-timeout"), ConnectionRefusedError("private-refused")):
            with mock.patch.object(x_native.urllib.request, "urlopen", side_effect=error) as opener:
                with self.assertLogs(x_native.__name__, level="WARNING"):
                    with self.assertRaises(AttributionError): self.bridge.reserve(self.pub, self.pub)
            self.assertEqual(opener.call_count, 3); self.assert_no_write()

    def test_total_budget_stops_retry(self):
        with mock.patch.object(x_native.time, "monotonic", side_effect=[0, 0, 56]):
            with mock.patch.object(x_native.urllib.request, "urlopen", side_effect=TimeoutError()) as opener:
                with self.assertLogs(x_native.__name__, level="WARNING"):
                    with self.assertRaises(AttributionError): x_native._resolve_attribution(self.q["id"], self.log)
        self.assertEqual(opener.call_count, 1); self.sleep.assert_not_called(); self.assert_no_write()


if __name__ == "__main__": unittest.main()

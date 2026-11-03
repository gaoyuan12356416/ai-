"""Canonical failure diagnostics must reach account blockers without retrying X."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.diagnostics import exception_failure_fields, failure_fields
from features.x_posts import service as posts
from features.x_posts.account_blockers import read_account_publish_blockers
from scripts import test_pipeline_x_native as native_fixture


class NativeFailureDiagnosticsTests(unittest.TestCase):
    # Reuse offline, real-SQLite fixtures without inheriting unrelated tests.
    verify = native_fixture.NativeTests.verify
    queue = native_fixture.NativeTests.queue
    job = native_fixture.NativeTests.job
    preflight = native_fixture.NativeTests.preflight
    operation = native_fixture.NativeTests.operation
    publish = native_fixture.NativeTests.publish

    def setUp(self):
        native_fixture.NativeTests.setUp(self)
        self.network = mock.patch.object(posts.UrllibHttpClient, "request",
            side_effect=AssertionError("failure reconciliation must not call X"))
        self.http = self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.http.assert_not_called)

    def start_publish(self, *, initialize=True, uploaded=False):
        queue = self.queue(auto=True)
        prep, _, artifact = self.preflight(queue)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        job = self.job(queue, "publish")
        descriptor = self.bridge.reserve(job, job)
        if initialize:
            body = json.dumps({"media_type": "video/mp4", "total_bytes": 2048,
                "media_category": "tweet_video"}, separators=(",", ":")).encode()
            operation = self.operation(job, "POST", "/2/media/upload/initialize",
                body, "initialize-" + job["source_id"])
            self.bridge.begin_operation(job, job, operation)
            if uploaded:
                self.bridge.checkpoint(job, job, "x_media_initialized",
                    {"media_id": "media123", "operation_id": operation["operation_id"]})
                self.bridge.checkpoint(job, job, "x_media_uploaded",
                    {"media_id": "media123", "artifact_sha256": descriptor["artifact"]["sha256"]})
        return queue, job

    def log(self, queue):
        with self.bridge.connect() as conn:
            return dict(conn.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?",
                (queue["id"],)).fetchone())

    def test_initialize_403_receipt_reaches_account_lock_blocker(self):
        queue, job = self.start_publish()
        error = posts.XPostError("x_upstream_error",
            "X media initialize failed (HTTP 403): Your account is temporarily locked.", 502, False)
        # Node and coordinator apply the existing sanitizer independently.
        receipt = {"error_code": error.code, "unknown_outcome": False,
            **failure_fields(exception_failure_fields(error)["error_message"])}
        result = self.bridge.fail(job, job, receipt)
        log = self.log(queue)
        self.assertTrue(result["canonical_no_write"])
        self.assertEqual((log["status"], log["attempt_count"], log["unknown_outcome"]), ("failed", 1, 0))
        self.assertEqual(log["error_code"], error.code)
        self.assertIn("HTTP 403", log["error_message"])
        with self.bridge.connect() as conn:
            blocker = read_account_publish_blockers(conn, [queue["account_id"]])
            self.assertEqual(blocker[queue["account_id"]]["code"], "x_post_account_locked")
            self.assertEqual(conn.execute("SELECT count(*) FROM pipeline_x_operation WHERE operation='x:POST:/2/tweets'").fetchone()[0], 0)

    def test_native_boundary_redacts_url_and_credentials(self):
        queue, job = self.start_publish()
        self.bridge.fail(job, job, {"error_code": "x_upstream_error", "unknown_outcome": False,
            "error_message": "HTTP 403 temporarily locked https://host.invalid/a?secret=sensitive "
                "Authorization: Bearer super-secret-value " + "x" * 1000})
        message = self.log(queue)["error_message"]
        self.assertIn("temporarily locked", message)
        for secret in ("host.invalid", "sensitive", "super-secret-value"):
            self.assertNotIn(secret, message)
        self.assertLessEqual(len(message), 500)

    def test_missing_or_non_string_diagnostic_keeps_legacy_code(self):
        for value in (None, 403, {}, [], ""):
            with self.subTest(value=value):
                queue, job = self.start_publish(initialize=False)
                self.bridge.fail(job, job, {"error_code": "pipeline_lease_lost",
                    "error_message": value, "unknown_outcome": False})
                log = self.log(queue)
                self.assertEqual((log["status"], log["attempt_count"], log["unknown_outcome"]), ("failed", 0, 0))
                self.assertEqual(log["error_message"], "pipeline_lease_lost")

    def test_reserved_diagnostic_does_not_create_attempt(self):
        queue, job = self.start_publish(initialize=False)
        self.bridge.fail(job, job, {"error_code": "x_upstream_error", "unknown_outcome": False,
            "error_message": "Read-only preflight failed (HTTP 503)"})
        log = self.log(queue)
        self.assertEqual(log["error_message"], "Read-only preflight failed (HTTP 503)")
        self.assertEqual((log["attempt_count"], log["unknown_outcome"]), (0, 0))

    def test_post_creating_keeps_unknown_and_accepts_exact_late_receipt(self):
        queue, job = self.start_publish(uploaded=True)
        self.assertEqual(self.log(queue)["status"], "post_creating")
        body = json.dumps({"text": self.log(queue)["post_text"], "media": {"media_ids": ["media123"]}},
            ensure_ascii=False, separators=(",", ":")).encode()
        self.bridge.begin_operation(job, job,
            self.operation(job, "POST", "/2/tweets", body, "post-" + job["source_id"]))
        result = self.bridge.fail(job, job, {"error_code": "x_post_outcome_unknown",
            "unknown_outcome": False, "error_message": "Post response missing after HTTP timeout"})
        log = self.log(queue)
        self.assertFalse(result["canonical_no_write"])
        self.assertEqual((log["status"], log["attempt_count"], log["unknown_outcome"]), ("post_creating", 1, 1))
        self.assertEqual(log["error_message"], "Post response missing after HTTP timeout")
        self.bridge.checkpoint(job, job, "x_source_published", {"media_id": "media123", "post_id": "123456"})
        self.assertEqual(self.log(queue)["status"], "published")

    def test_upload_unknown_is_not_downgraded_by_lock_diagnostic(self):
        queue, job = self.start_publish()
        result = self.bridge.fail(job, job, {"error_code": "x_upstream_error", "unknown_outcome": True,
            "error_message": "HTTP 403 temporarily locked"})
        log = self.log(queue)
        self.assertFalse(result["canonical_no_write"])
        self.assertEqual((log["status"], log["attempt_count"], log["unknown_outcome"]), ("failed", 1, 1))
        with self.bridge.connect() as conn:
            self.assertEqual(read_account_publish_blockers(conn, [queue["account_id"]])[queue["account_id"]]["code"],
                "x_post_account_needs_review")

    def test_published_receipt_is_unchanged_by_late_failure(self):
        queue = self.queue(auto=True)
        prep, _, artifact = self.preflight(queue)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        job, _ = self.publish(queue)
        before = self.log(queue)
        result = self.bridge.fail(job, job, {"error_code": "x_upstream_error", "unknown_outcome": True,
            "error_message": "HTTP 403 temporarily locked"})
        self.assertTrue(result["canonical_terminal"])
        self.assertEqual(self.log(queue), before)


if __name__ == "__main__":
    unittest.main()

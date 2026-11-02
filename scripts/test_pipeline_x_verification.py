"""Real native/OAuth ledgers; only X network and local media download are fake."""
import contextlib
import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.executors import NodeExecutors
from features.x_accounts import oauth_service as oauth
from features.x_posts import service as posts
from features.x_posts.random_template_store import canonical
from scripts import test_pipeline_x_native as native_fixtures
from scripts import test_x_accounts as account_fixtures
from scripts.test_x_posts import ScriptedHttpClient, response


class NativeContext:
    def __init__(self, fixture, job):
        self.fixture, self.job = fixture, job
        self.operations, self.last_operation_id = [], ""
    def check(self): pass
    def phase(self, _phase): pass
    def checkpoint(self, phase, receipt):
        self.fixture.bridge.checkpoint(self.job, self.job, phase, receipt)
    @contextlib.contextmanager
    def operation(self, name, details, *, account_resource, write):
        self.last_operation_id = "native-node-%s" % (len(self.operations) + 1)
        operation = {"operation_id": self.last_operation_id, "operation": name,
            "account_resource": account_resource, "details": details,
            "request_sha256": hashlib.sha256(canonical(details).encode()).hexdigest()}
        self.operations.append(operation)
        yield self.fixture.bridge.begin_operation(self.job, self.job, operation)["credentials"]


class XVerificationTests(unittest.TestCase):
    def setUp(self):
        self.auth = account_fixtures.XAccountsTestCase()
        self.auth.setUp(); self.addCleanup(self.auth.tearDown)
        self.item = self.auth.complete(account_fields={"subscription_type": "premium", "protected": False})
        with contextlib.closing(sqlite3.connect(oauth.DB_PATH)) as conn, conn:
            conn.execute("UPDATE x_authorized_account SET drama_language='en' WHERE id=?", (self.item["id"],))
        self.fixture = native_fixtures.NativeTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.bridge = self.fixture.bridge
        self.fixture.runtime.verify_account = oauth.verify_account
        self.fixture.runtime.publish_credentials = oauth.publish_credentials
        self.fixture.runtime.CANARY_ACTOR = oauth.CANARY_ACTOR
        self.fixture.runtime.AUTO_TEMPLATE_ACTOR = oauth.AUTO_TEMPLATE_ACTOR
        self.user = mock.patch.object(oauth, "user_request", return_value={"data": {
            "id": self.item["x_user_id"], "username": self.item["username"],
            "subscription_type": "premium", "protected": False}})
        self.user_request = self.user.start(); self.addCleanup(self.user.stop)
        self.queue = self.fixture.queue(identity=self.item["id"])
        prep, _, artifact = self.fixture.preflight(self.queue, duration=1300)
        self.bridge.complete(prep, {"artifact": artifact}, prep)
        self.user_request.reset_mock()
        self.job = self.fixture.job(self.queue, "publish")
        self.descriptor = self.bridge.reserve(self.job, self.job)
        self.context = NativeContext(self.fixture, self.job)

    def operation(self, method, path, body=b"", retry_of=None):
        details = {"method": method, "path": path, "body_sha256": hashlib.sha256(body).hexdigest()}
        if retry_of is not None:
            details["media_401_retry_of"] = retry_of
        with self.context.operation("x:" + method + ":" + path.split("?")[0], details,
                account_resource="x:account:" + str(self.item["id"]), write=method != "GET") as credentials:
            return dict(credentials)

    def initialize(self):
        descriptor = self.descriptor
        body = json.dumps({"media_type": descriptor["artifact"]["mime"],
            "total_bytes": descriptor["artifact"]["size"],
            "media_category": descriptor["request"]["media_category"]}, separators=(",", ":")).encode()
        self.operation("POST", "/2/media/upload/initialize", body)
        self.context.checkpoint("x_media_initialized", {"media_id": "media123", "operation_id": self.context.last_operation_id})

    def test_many_long_video_requests_reuse_verification_but_refresh_expiring_token(self):
        self.assertEqual(self.user_request.call_count, 1)  # New claim, real OAuth verifier.
        self.initialize()
        for i in range(12):
            self.operation("POST", "/2/media/upload/media123/append", ("part-%s" % i).encode())
        for _ in range(8):
            self.operation("GET", "/2/media/upload?media_id=media123&command=STATUS")
        self.assertEqual(self.user_request.call_count, 1)
        with contextlib.closing(sqlite3.connect(oauth.DB_PATH)) as conn, conn:
            conn.execute("UPDATE x_authorized_account SET access_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (self.item["id"],))
        with mock.patch.object(oauth, "token_request", return_value={"access_token": "rotated-token", "refresh_token": "test-refresh", "expires_in": 7200}) as refresh:
            credentials = self.operation("GET", "/2/media/upload?media_id=media123&command=STATUS")
        self.assertEqual(credentials["access_token"], "rotated-token")
        refresh.assert_called_once()
        self.assertEqual(self.user_request.call_count, 2)
        self.operation("GET", "/2/media/upload?media_id=media123&command=STATUS")
        self.assertEqual(self.user_request.call_count, 2)

    def test_changed_approval_and_app_identity_still_block_before_credentials(self):
        self.initialize()
        for mutation in ("approval", "app", "premium"):
            with self.subTest(mutation=mutation):
                if mutation == "approval":
                    with contextlib.closing(sqlite3.connect(oauth.DB_PATH)) as conn, conn:
                        conn.execute("UPDATE x_authorized_account SET publish_approved=0 WHERE id=?", (self.item["id"],))
                    change = contextlib.nullcontext()
                else:
                    with contextlib.closing(sqlite3.connect(oauth.DB_PATH)) as conn, conn:
                        conn.execute("UPDATE x_authorized_account SET publish_approved=1 WHERE id=?", (self.item["id"],))
                    if mutation == "app":
                        change = mock.patch.object(oauth, "CLIENT_ID", "different-app")
                    else:
                        with contextlib.closing(sqlite3.connect(oauth.DB_PATH)) as conn, conn:
                            conn.execute("UPDATE x_authorized_account SET subscription_type='none' WHERE id=?", (self.item["id"],))
                        change = contextlib.nullcontext()
                with change, self.assertRaises((oauth.ServiceError, AdapterError)):
                    self.operation("POST", "/2/media/upload/media123/append", b"blocked")
        self.assertEqual(self.user_request.call_count, 1)

    def test_media_retry_is_exact_one_per_claim_and_preserves_unknown(self):
        self.initialize()
        path, body = "/2/media/upload/media123/append", b"same-segment"
        self.operation("POST", path, body)
        original = self.context.last_operation_id
        with self.assertRaisesRegex(AdapterError, "x_media_retry_invalid"):
            self.operation("POST", path, b"changed", original)
        self.operation("POST", path, body, original)
        self.assertEqual(self.user_request.call_count, 2)
        with self.assertRaisesRegex(AdapterError, "x_media_retry_invalid"):
            self.operation("POST", path, body, original)
        self.operation("GET", "/2/media/upload?media_id=media123&command=STATUS")
        another = self.context.last_operation_id
        with self.assertRaisesRegex(AdapterError, "x_media_retry_invalid"):
            self.operation("GET", "/2/media/upload?media_id=media123&command=STATUS", retry_of=another)
        self.assertEqual(self.user_request.call_count, 2)
        self.bridge.fail(self.job, self.job, {"error_code": "x_post_rate_limited", "unknown_outcome": True})
        self.assertTrue(self.bridge.reconcile(self.job)["unknown_outcome"])
        with self.assertRaises(AdapterError):
            self.operation("POST", path, body)
        self.assertTrue(self.bridge.reconcile(self.job)["unknown_outcome"])

    def test_retry_binds_full_status_query_and_generation(self):
        self.initialize()
        path = "/2/media/upload?media_id=media123&command=STATUS"
        self.operation("GET", path)
        original = self.context.last_operation_id
        with self.assertRaisesRegex(AdapterError, "x_media_retry_invalid"):
            self.operation("GET", path.replace("media123", "another-media"), retry_of=original)
        changed = dict(self.job, generation=2)
        self.context.job = changed
        with self.assertRaisesRegex(AdapterError, "x_dispatch_claim_conflict"):
            self.operation("GET", path, retry_of=original)
        self.context.job = self.job
        self.operation("GET", path, retry_of=original)
        self.assertEqual(self.user_request.call_count, 2)

    def execute(self, replies):
        transport = ScriptedHttpClient(replies)
        node = NodeExecutors({"state_root": str(self.fixture.root / "node"), "backend": "cpu"})
        media = self.fixture.root / "media.bin"
        media.write_bytes(b"a" * self.descriptor["artifact"]["size"])
        with mock.patch.object(node, "_download", return_value=media), \
                mock.patch.object(posts, "UrllibHttpClient", return_value=transport), mock.patch.object(posts.time, "sleep"):
            result = node.x_publish(self.descriptor, self.descriptor["artifact"], self.context)
        return result, transport

    def test_node_media_401_refreshes_once_then_completes_original_attempt(self):
        result, transport = self.execute([response(401), response(200, {"data": {"id": "media123"}}),
            response(200), response(200), response(201, {"data": {"id": "123456"}})])
        self.assertEqual(result["post_id"], "123456")
        self.assertEqual(self.user_request.call_count, 2)
        self.assertEqual(transport.requests[0]["url"], transport.requests[1]["url"])
        self.assertEqual(transport.requests[0]["body"], transport.requests[1]["body"])
        self.assertTrue(all(r["headers"]["Authorization"] == "Bearer access-secret" for r in transport.requests))
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_media_retry").fetchone()[0], 1)
            self.assertEqual(tuple(conn.execute("SELECT attempt_count,status FROM x_post_publish_log").fetchone()), (1, "published"))

    def test_node_repeated_media_401_stops_without_post(self):
        with self.assertRaises(posts.XPostError) as caught:
            self.execute([response(401), response(401)])
        self.assertEqual(caught.exception.code, "x_token_invalid")
        self.assertEqual(self.user_request.call_count, 2)
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_operation").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_operation WHERE operation='x:POST:/2/tweets'").fetchone()[0], 0)

    def test_node_post_401_is_not_retried(self):
        with self.assertRaises(posts.XPostError):
            self.execute([response(200, {"data": {"id": "media123"}}), response(200), response(200), response(401)])
        self.assertEqual(self.user_request.call_count, 1)
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_media_retry").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_x_operation WHERE operation='x:POST:/2/tweets'").fetchone()[0], 1)

    def test_node_repost_401_is_not_retried(self):
        from scripts.test_pipeline_executors import Context
        self.context = Context()
        self.descriptor["request"].update(delivery_mode="premium_relay_repost", target_x_user_id="123456789")
        with self.assertRaises(posts.XPostError):
            self.execute([response(200, {"data": {"id": "media123"}}), response(200), response(200),
                response(201, {"data": {"id": "123456"}}), response(401)])
        repost = [details for name, details, _ in self.context.operations if name.endswith("/retweets")]
        self.assertEqual(len(repost), 1)
        self.assertNotIn("media_401_retry_of", repost[0])


if __name__ == "__main__": unittest.main()

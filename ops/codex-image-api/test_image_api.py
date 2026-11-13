import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from api import handler_class
from common import CosUploader, checked_image, public_target, sha256
from http.server import ThreadingHTTPServer
from store import ApiError, Store


PAYLOAD = {"request_id": "test-1", "items": [{"image_urls": ["https://example.com/1.png", "https://example.com/2.png"], "prompt": "中文提示词"}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_mapping_and_terminal_visibility(self):
        task, created = self.store.submit("owner", PAYLOAD)
        self.assertTrue(created)
        self.assertEqual(self.store.query("owner", task)["status"], "queued")
        first, fresh = self.store.next_image()
        self.assertEqual((first["item_index"], first["image_index"], first["prompt"]), (0, 0, "中文提示词"))
        self.assertTrue(fresh)
        self.store.update(first, "succeeded", "succeeded", cos_url="https://cos/1.png")
        ongoing = self.store.query("owner", task)
        self.assertFalse(ongoing["done"])
        self.assertIsNone(ongoing["results"])
        second, _ = self.store.next_image()
        self.store.update(second, "failed", "failed", error={"code": "invalid_image", "message": "bad"})
        final = self.store.query("owner", task)
        self.assertEqual((final["status"], final["done"], final["completed"], final["failed"]), ("partial_failed", True, 2, 1))
        self.assertEqual([r["image_index"] for r in final["results"]], [0, 1])
        self.assertIsNone(final["results"][1]["cos_url"])

    def test_idempotency_under_parallel_submission(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.submit("owner", PAYLOAD), range(12)))
        self.assertEqual(len({r[0] for r in results}), 1)
        self.assertEqual(sum(r[1] for r in results), 1)
        changed = json.loads(json.dumps(PAYLOAD))
        changed["items"][0]["prompt"] = "changed"
        with self.assertRaises(ApiError) as cm:
            self.store.submit("owner", changed)
        self.assertEqual(cm.exception.http_status, 409)

    def test_recovery_preserves_claim(self):
        self.store.submit("owner", PAYLOAD)
        row, _ = self.store.next_image()
        self.store.update(row, "generating")
        reopened = Store(self.store.path)
        same, fresh = reopened.next_image()
        self.assertFalse(fresh)
        self.assertEqual(same["attempt_id"], row["attempt_id"])
        self.assertEqual(same["stage"], "generating")

    def test_owner_isolation(self):
        task, _ = self.store.submit("owner", PAYLOAD)
        with self.assertRaises(ApiError) as cm:
            self.store.query("other", task)
        self.assertEqual(cm.exception.http_status, 404)

    def test_no_request_id_creates_new_task(self):
        payload = {"items": PAYLOAD["items"]}
        self.assertNotEqual(self.store.submit("owner", payload)[0], self.store.submit("owner", payload)[0])

    def test_group_and_image_order(self):
        payload = {"items": PAYLOAD["items"] + [{"image_urls": ["https://example.com/3.png"], "prompt": "second"}]}
        task, _ = self.store.submit("owner", payload)
        rows = []
        for _ in range(3):
            row, _ = self.store.next_image()
            rows.append((row["item_index"], row["image_index"], row["prompt"]))
            self.store.update(row, "succeeded", "succeeded", cos_url="https://cos/image.png")
        self.assertEqual(rows, [(0, 0, "中文提示词"), (0, 1, "中文提示词"), (1, 0, "second")])
        self.assertEqual(self.store.query("owner", task)["status"], "succeeded")

    def test_invalid_shape_not_enqueued(self):
        for payload in ({}, {"items": []}, {"items": [{"prompt": "x", "image_urls": []}]}, {"items": PAYLOAD["items"], "bad": True}):
            with self.assertRaises(ApiError):
                self.store.submit("owner", payload)
        self.assertEqual(self.store.next_image(), (None, False))


class HttpTests(StoreTests):
    def setUp(self):
        super().setUp()
        config = types.SimpleNamespace(tokens=lambda: ["test-token"], body_limit=1024 * 1024)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(config, self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = json.loads(r.read())
        result = (r.status, data, dict(r.getheaders()))
        c.close()
        return result

    def test_http_contract_and_no_generation_on_submit(self):
        headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
        code, submitted, _ = self.request("POST", "/api/image-generation/tasks", json.dumps(PAYLOAD), headers)
        self.assertEqual(code, 202)
        self.assertEqual(submitted["total"], 2)
        code, state, response_headers = self.request("GET", "/api/image-generation/tasks/" + submitted["task_id"], headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(state["status"], "queued")
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("POST", "/api/image-generation/tasks", json.dumps(PAYLOAD), headers)[0], 200)

    def test_auth_and_not_found(self):
        self.assertEqual(self.request("GET", "/api/image-generation/tasks/ig_" + "1" * 32)[0], 401)
        self.assertEqual(self.request("GET", "/api/image-generation/tasks/ig_" + "1" * 32, headers={"X-API-Token": "test-token"})[0], 404)


class DownloadTests(unittest.TestCase):
    @staticmethod
    def resolver(address):
        return lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def test_private_targets_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.assertRaises(ApiError):
                public_target("https://reference.example/image.png", self.resolver(address))

    def test_protocol_and_credentials_rejected(self):
        for url in ("file:///etc/passwd", "http://user:pass@example.com/a.png", "ftp://example.com/a.png"):
            with self.assertRaises(ApiError):
                public_target(url, self.resolver("8.8.8.8"))

    def test_public_dns_resolution_is_pinned(self):
        parsed, port, address = public_target("https://example.com/a.png", self.resolver("8.8.8.8"))
        self.assertEqual((parsed.hostname, port, address), ("example.com", 443, "8.8.8.8"))

    def test_invalid_image(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fake.png"
            path.write_text("not an image")
            with self.assertRaises(ApiError):
                checked_image(path)


class UploadTests(unittest.TestCase):
    def test_unknown_put_is_reconciled_without_second_upload(self):
        class Missing(Exception):
            def get_status_code(self): return 404
        class Client:
            puts = 0
            stored = None
            def head_object(self, **kw):
                if self.stored: return self.stored
                raise Missing()
            def put_object(self, **kw):
                self.puts += 1
                data = kw["Body"].read()
                self.stored = {"Content-Length": str(len(data)), "x-cos-meta-sha256": kw["Metadata"]["sha256"]}
                raise OSError("response lost")
        uploader = object.__new__(CosUploader)
        uploader.client, uploader.bucket, uploader.prefix, uploader.base = Client(), "bucket", "images", "https://cos.example"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "result.png"
            p.write_bytes(b"fixture")
            with patch("common.time.sleep"):
                result = uploader.upload({"task_id": "ig_test", "item_index": 0, "image_index": 0}, p)
        self.assertEqual(uploader.client.puts, 1)
        self.assertTrue(result.endswith("ig_test/0000-0000.png"))


@unittest.skipUnless(os.name == "posix", "systemd worker requires Linux")
class RecoveryTests(unittest.TestCase):
    def test_alive_unit_is_never_relaunched(self):
        from worker import Worker
        row = {"attempt_id": "abc", "task_id": "ig_test", "ordinal": 0}
        worker = Worker(types.SimpleNamespace(), None)
        with patch("worker.unit_state", return_value={"ActiveState": "active", "MainPID": "123"}):
            self.assertFalse(worker.collect(row, Path("/not-needed")))

    def test_dead_unknown_result_fails_without_retry(self):
        from worker import Worker
        row = {"attempt_id": "abc", "task_id": "ig_test", "ordinal": 0}
        worker = Worker(types.SimpleNamespace(), None)
        with tempfile.TemporaryDirectory() as d, patch("worker.unit_state", return_value={"ActiveState": "inactive", "MainPID": "0"}), patch.object(worker, "sync_auth"):
            with self.assertRaises(ApiError) as cm:
                worker.collect(row, Path(d))
            self.assertEqual(cm.exception.code, "execution_interrupted")


if __name__ == "__main__":
    unittest.main()

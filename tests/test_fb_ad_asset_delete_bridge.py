import json
from email.message import Message
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from features.fb_ad_asset_delete import bridge
from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.store import StoreError


class Handler:
    def __init__(self, method="GET", payload=None):
        self.command, self.payload, self.response = method, payload, None
        self.headers = Message()
        self.headers["Content-Length"] = str(len(json.dumps(payload or {}).encode()))
        self.headers["Content-Type"] = "application/json"
        self.origin_allowed = True
    def _cookies(self):
        return {"session": "test-session"}
    def _require_same_origin_json(self):
        if not self.origin_allowed:
            self.response = (403, {"error": "same_origin_required"})
        return self.origin_allowed
    def _read_json(self):
        return self.payload


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.session = {"user_id": "operator", "tenant_key": "tenant", "role": "user"}
        self.app = {"SESSION_COOKIE_NAME": "session", "load_session": lambda token: self.session,
            "has_module_permission": lambda session, key: key == "fb_ad_asset_delete",
            "json_response": self.send, "ad_control_actor": lambda session: session["user_id"]}
        self.service = Mock()
        self.service.products.return_value = {"items": [{"id": "3443", "name": "W2A", "kind": "W2A"}]}
        self.temp = tempfile.TemporaryDirectory()
        self.app["JOB_DB_PATH"] = str(Path(self.temp.name) / "legacy.sqlite3")
    def tearDown(self):
        self.temp.cleanup()
    @staticmethod
    def send(handler, status, payload, **kwargs):
        handler.response = (status, payload)
    def dispatch(self, suffix, method="GET", payload=None):
        h = Handler(method, payload)
        with patch.object(bridge, "get_service", return_value=self.service):
            bridge.dispatch(h, urlparse(bridge.PREFIX + suffix), self.app)
        return h.response
    def test_no_cookie_cannot_use_legacy_auth_bypass(self):
        self.session = None
        self.assertEqual(401, self.dispatch("/products")[0])
        self.service.products.assert_not_called()
    def test_dedicated_permission_required_before_service_access(self):
        self.app["has_module_permission"] = lambda *args: False
        self.assertEqual(403, self.dispatch("/products")[0])
        self.service.products.assert_not_called()
    def test_old_mutation_routes_are_gone(self):
        for suffix in ("/delete-posts", "/delete-ads"):
            self.assertEqual(410, self.dispatch(suffix, "POST", {"job_id": "old"})[0])
        self.service.execute.assert_not_called()
    def test_same_origin_guard_precedes_preview_or_execute(self):
        h = Handler("POST", {})
        h.origin_allowed = False
        bridge.dispatch(h, urlparse(bridge.PREFIX + "/preview"), self.app)
        self.assertEqual(403, h.response[0])
    def test_execute_uses_only_url_job_and_server_service(self):
        self.service.execute.return_value = {"job_id": "job1", "run_id": "run", "duplicate": False}
        payload = {"preview_id": "preview", "request_id": "request_123456789", "phases": ["video", "ad"]}
        self.assertEqual(202, self.dispatch("/jobs/job1/execute", "POST", payload)[0])
        self.service.execute.assert_called_once_with(self.session, "job1", payload)

    def test_recheck_uses_frozen_route_and_returns_async_without_execute(self):
        self.service.recheck.return_value = {"job_id": "job1", "operation_id": "check", "read_only": True}
        payload = {"preview_id": "preview", "request_id": "request_123456789"}
        self.assertEqual(202, self.dispatch("/jobs/job1/recheck", "POST", payload)[0])
        self.service.recheck.assert_called_once_with(self.session, "job1", payload)
        self.service.execute.assert_not_called()
    def test_invalid_large_body_and_array_rejected(self):
        self.assertEqual(400, self.dispatch("/preview", "POST", [1])[0])
        h = Handler("POST", {})
        h.headers.replace_header("Content-Length", "1000000")
        with patch.object(bridge, "get_service", return_value=self.service):
            bridge.dispatch(h, urlparse(bridge.PREFIX + "/preview"), self.app)
        self.assertEqual(413, h.response[0])
        self.service.preview.assert_not_called()
    def test_errors_are_classified_without_raw_secret_messages(self):
        self.service.products.side_effect = RuntimeError("access_token=DO_NOT_EXPOSE")
        status, body = self.dispatch("/products")
        self.assertEqual(503, status)
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(body))
        self.service.products.side_effect = AssetError("product_permission_denied", "denied", 403)
        self.assertEqual(403, self.dispatch("/products")[0])
    def test_legacy_history_is_read_only_owner_and_product_scoped(self):
        path = self.app["JOB_DB_PATH"]
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE fb_post_ad_delete_job(job_id TEXT,actor_user_id TEXT,products_json TEXT,updated_at TEXT,series_ids_json TEXT)")
            db.executemany("INSERT INTO fb_post_ad_delete_job VALUES(?,?,?,?,?)", [
                ("own", "operator", '["3443"]', "2026-01-01", '["XEY271"]'),
                ("other", "other", '["3443"]', "2026-01-01", '[]'),
                ("loan", "operator", '["826"]', "2026-01-01", '[]')])
        db.close()
        before = Path(path).read_bytes()
        rows = bridge._legacy(self.app, self.session, self.service)
        self.assertEqual(["own"], [r["job_id"] for r in rows])
        self.assertTrue(rows[0]["read_only"])
        self.assertEqual(before, Path(path).read_bytes())
        self.session["role"] = "admin"
        self.assertEqual(3, len(bridge._legacy(self.app, self.session, self.service)))
    def test_detail_pagination_and_missing_legacy(self):
        self.service.detail.return_value = {"objects": [], "total": 0}
        self.assertEqual(200, self.dispatch("/jobs/job1?page=2&page_size=25&kind=video")[0])
        self.service.detail.assert_called_once_with(self.session, "job1", page="2", page_size="25", kind="video")
        self.service.detail.side_effect = StoreError("not found", "not_found")
        self.assertEqual(404, self.dispatch("/jobs/missing")[0])


if __name__ == "__main__":
    unittest.main()

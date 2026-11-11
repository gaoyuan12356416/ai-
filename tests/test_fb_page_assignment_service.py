import contextlib
import copy
import http.client
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from features.fb_page_assignment.notify import DeliveryError
from features.fb_page_assignment.server import ROUTE, handler
from features.fb_page_assignment.service import Service
from features.fb_page_assignment.sources import SourceError
from features.fb_page_assignment.storage import Store


class FakeSources:
    def __init__(self):
        self.calls = []
        self.error = None
        self.pages = [dict(page_id="12345678901234567890", language="en", status=0,
                           forbidden_status=0, is_permission=1, active_ad_count=0, fan_ccount=10)]

    def load(self, ids):
        self.calls.append(list(ids))
        if self.error:
            raise self.error
        return dict(pages=copy.deepcopy(self.pages), warnings=[], source_read_at="2026-11-11T00:00:00+00:00",
                    dramas={cid: dict(exists=True, language="en", audience="female", themes=["romance"])
                            for cid in ids})


class FakeWriter:
    def __init__(self):
        self.calls = []
        self.error = None

    def write_task(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.error:
            raise self.error


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.error = None

    def send(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.error:
            raise self.error
        return "om_test_receipt"


class ServiceFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "outbox.sqlite3")
        self.sources, self.writer, self.notifier = FakeSources(), FakeWriter(), FakeNotifier()
        self.service = Service(self.sources, self.store, self.writer, self.notifier,
                               profile_loader=lambda ids, root: ({}, {"version": "fixture"}, []))
        self.logs = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.logs)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def request(self, task_id="task_fixture", ids=None):
        return self.service.handle(task_id, {"external_task_id": "caller_1", "drama_ids": ids or ["drama_1"]})


class ServiceTests(ServiceFixture, unittest.TestCase):
    def test_success_returns_only_task_id_and_ordered_id_pairs(self):
        status, response = self.request(ids=["drama_1", "drama_2", "drama_1"])
        self.assertEqual(status, 200)
        self.assertEqual(set(response), {"task_id", "results"})
        self.assertEqual([row["drama_id"] for row in response["results"]], ["drama_1", "drama_2", "drama_1"])
        self.assertTrue(all(set(row) == {"drama_id", "page_id"} for row in response["results"]))
        self.assertTrue(all(isinstance(row["page_id"], str) for row in response["results"]))
        saved = self.store.get(response["task_id"])
        self.assertEqual(saved["sync_status"], "synced")
        self.assertEqual(saved["rows"][0]["diagnostics"]["profile_snapshot"], {"version": "fixture"})
        self.service.tick()
        self.assertEqual(self.notifier.calls, [])

    def test_source_failure_preserves_each_id_and_alerts_once(self):
        self.sources.error = SourceError("source_query_timeout")
        status, response = self.request(ids=["drama_1", "drama_2"])
        self.assertEqual(status, 503)
        self.assertEqual(response["results"], [{"drama_id": "drama_1", "page_id": None},
                                               {"drama_id": "drama_2", "page_id": None}])
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.notifier.calls), 1)
        saved = self.store.get("task_fixture")
        self.assertEqual(saved["alert_status"], "sent")
        self.assertEqual(saved["sync_status"], "synced")
        self.assertEqual(self.writer.calls[-1]["feishu_message_id"], "om_test_receipt")

    def test_mysql_failure_retains_allocation_and_does_not_repeat_alert(self):
        self.writer.error = RuntimeError("password=SHOULD_NOT_LEAK")
        status, response = self.request()
        self.assertEqual(status, 200)
        self.assertIsNotNone(response["results"][0]["page_id"])
        self.assertEqual(self.store.get("task_fixture")["sync_status"], "pending")
        self.service.tick()
        self.service.tick()
        saved = self.store.get("task_fixture")
        self.assertIn("log_sync_pending", saved["alert_reasons"])
        self.assertEqual(saved["alert_status"], "sent")
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertNotIn("SHOULD_NOT_LEAK", self.logs.getvalue())
        self.assertEqual(len(self.sources.calls), 1)

    def test_partial_batch_failure_keeps_successful_rows(self):
        original = self.sources.load
        def load(ids):
            data = original(ids)
            del data["dramas"]["bad"]
            return data
        self.sources.load = load
        status, response = self.request(ids=["drama_1", "bad", "drama_2"])
        self.assertEqual(status, 200)
        self.assertIsNotNone(response["results"][0]["page_id"])
        self.assertIsNone(response["results"][1]["page_id"])
        self.assertIsNotNone(response["results"][2]["page_id"])
        self.assertIn("drama_not_found", self.store.get("task_fixture")["alert_reasons"])

    def test_local_persistence_failure_does_not_report_unlogged_success(self):
        with patch.object(self.store, "save", side_effect=OSError("secret=SHOULD_NOT_LEAK")):
            status, response = self.request()
        self.assertEqual((status, response["results"]), (503, []))
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.notifier.calls[0]["alert_reasons"], ["local_persistence_failed"])
        self.assertEqual(self.writer.calls, [])
        self.assertNotIn("SHOULD_NOT_LEAK", self.logs.getvalue())

    def test_unknown_delivery_is_never_replayed(self):
        self.sources.pages = []
        self.notifier.error = DeliveryError("feishu_send_unknown", uncertain=True)
        self.request()
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.store.get("task_fixture")["alert_status"], "unknown")

    def test_unexpected_notifier_error_is_sanitized_and_preserves_allocation(self):
        self.service.profile_loader = lambda ids, root: ({}, {}, ["profile_snapshot_unavailable"])
        self.notifier.error = RuntimeError("Bearer SHOULD_NOT_LEAK")
        self.request()
        self.service.tick()
        saved = self.store.get("task_fixture")
        self.assertEqual(saved["alert_status"], "unknown")
        self.assertIsNotNone(saved["rows"][0]["page_id"])
        self.assertNotIn("SHOULD_NOT_LEAK", self.logs.getvalue())

    def test_notifier_claim_is_durable_before_send(self):
        self.sources.pages = []
        original = self.notifier.send
        def send(request):
            self.assertEqual(self.store.get(request["task_id"])["alert_status"], "sending")
            return original(request)
        self.notifier.send = send
        self.request()
        self.service.tick()
        self.assertEqual(self.store.get("task_fixture")["alert_status"], "sent")

    def test_bad_request_does_not_query_sources(self):
        status, response = self.service.handle("bad_request", {"external_task_id": "caller_1", "drama_ids": []})
        self.assertEqual((status, response["results"]), (400, []))
        self.assertEqual(self.sources.calls, [])
        self.assertIsNotNone(self.store.get("bad_request"))
        self.assertEqual(self.store.get("bad_request")["rows"][0]["item_index"], 0)

    def test_sync_coordination_exception_keeps_original_success(self):
        with patch.object(self.store, "sync_task", side_effect=RuntimeError("secret=SHOULD_NOT_LEAK")):
            status, response = self.request()
        self.assertEqual(status, 200)
        self.assertIsNotNone(response["results"][0]["page_id"])
        saved = self.store.get("task_fixture")
        self.assertEqual(saved["response"], response)
        self.assertIn("log_sync_pending", saved["alert_reasons"])
        self.assertEqual(saved["alert_status"], "pending")
        self.assertEqual(self.notifier.calls, [])
        self.assertNotIn("SHOULD_NOT_LEAK", self.logs.getvalue())

    def test_start_recovers_interrupted_send_without_replaying(self):
        self.sources.pages = []
        self.request()
        self.store.claim_alert("task_fixture")
        self.service.start()
        self.service.close()
        self.assertEqual(self.store.get("task_fixture")["alert_status"], "unknown")
        self.assertEqual(self.notifier.calls, [])


class HttpTests(ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.token = "independent_fixture_token_1234567890"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.service, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def http(self, method="POST", body=None, authorized=True, route=ROUTE):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["Authorization"] = "Bearer " + self.token
            connection.request(method, route, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_requires_independent_token_without_notifying_on_auth_failure(self):
        status, response = self.http(body='{"external_task_id":"caller","drama_ids":["d"]}', authorized=False)
        self.assertEqual(status, 401)
        self.assertIsNone(self.store.get(response["task_id"]))
        self.assertEqual(self.sources.calls, [])
        self.assertEqual(self.notifier.calls, [])
        self.assertNotIn(self.token, self.logs.getvalue())

    def test_http_malformed_json_is_logged_and_notified(self):
        status, response = self.http(body="{not_json")
        self.assertEqual(status, 400)
        self.assertIn("invalid_json_or_body", self.store.get(response["task_id"])["alert_reasons"])
        self.service.tick()
        self.assertEqual(len(self.notifier.calls), 1)

    def test_http_does_not_offer_get_query_route(self):
        status, _ = self.http(method="GET")
        self.assertEqual(status, 405)
        status, _ = self.http(method="GET", route=ROUTE + "/task_id")
        self.assertEqual(status, 404)

    def test_http_unexpected_exception_does_not_leak_secret(self):
        self.service.profile_loader = Mock(side_effect=RuntimeError("password=SHOULD_NOT_LEAK"))
        status, response = self.http(body='{"external_task_id":"caller","drama_ids":["d","d2"]}')
        self.assertEqual(status, 503)
        saved = self.store.get(response["task_id"])
        self.assertIn("internal_error", saved["alert_reasons"])
        self.assertEqual(response["results"], [{"drama_id": "d", "page_id": None},
                                               {"drama_id": "d2", "page_id": None}])
        self.assertEqual([row["drama_id"] for row in saved["rows"]], ["d", "d2"])
        self.assertNotIn("SHOULD_NOT_LEAK", self.logs.getvalue())
        self.assertNotIn("SHOULD_NOT_LEAK", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()

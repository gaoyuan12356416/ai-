"""Service routing and shared-response tests; no external network or databases."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The production launcher is Linux-only; route tests also run on Windows.
try:
    import fcntl  # noqa: F401
except ImportError:
    sys.modules["fcntl"] = SimpleNamespace()
    try:
        from features.moboreels_library.service import Application, Handler, public_run
    finally:
        del sys.modules["fcntl"]
else:
    from features.moboreels_library.service import Application, Handler, public_run

from features.moboreels_library.db import DatabaseError
from features.moboreels_library.promotions import PromotionError
from features.moboreels_library.settings import Settings


ACTOR = {"tenant_key": "tenant", "user_id": "user-a", "name": "Operator A"}
RUN_ID = "a" * 32


def private_run():
    return {
        "id": RUN_ID, "run_id": RUN_ID, "status": "running", "stage": "writing",
        "created_at": "2026-11-12T09:00:00.000Z", "started_at": "2026-11-12T09:00:00.001Z",
        "updated_at": "2026-11-12T09:01:00.000Z", "finished_at": None, "error": None,
        "total": 100, "processed": 70, "total_pages": 1, "fetched_pages": 1, "compared_rows": 100, "verified": 0,
        "new_count": 10, "updated_count": 20, "listed_count": 5, "delisted_count": 3,
        "actor": ACTOR, "resume_actor": {"tenant_key": "private-tenant", "user_id": "private-user", "name": "Private Name"},
        "resume_count": 2, "resume_trigger": "manual", "resumable": True, "source_complete": True,
        "snapshot_path": "/private/snapshot/should-not-be-public", "future_private_field": "hidden",
    }


class FakeDatabase:
    def __init__(self):
        self.query = None
        self.failure = False
        self.row = {"story_id": "42", "story_name": "A drama", "is_show": 1, "lang_id": 23,
                    "first_seen_at": "2026-11-12 09:00:00.123", "last_seen_at": "2026-11-12 10:00:00.456",
                    "source_publish_time": "2026-11-12 12:34:56"}

    def list_dramas(self, query):
        if self.failure:
            raise DatabaseError("private database failure details")
        self.query = copy.deepcopy(query)
        return {"list": [copy.deepcopy(self.row)], "total": 1, "page_index": 1, "page_size": 50,
                "filters": {"languages": [23]}}

    def get_drama(self, story):
        return copy.deepcopy(self.row) if story == "42" else None


class FakeSync:
    def __init__(self):
        self.run = private_run()
        self.started = []

    def latest(self):
        return copy.deepcopy(self.run)

    def get(self, run_id):
        return copy.deepcopy(self.run) if run_id == RUN_ID else None

    def start(self, trigger, actor):
        self.started.append((trigger, copy.deepcopy(actor)))
        return copy.deepcopy(self.run)

    def last_success_at(self):
        return "2026-11-11T01:15:00.000Z"


class FakePromotions:
    def __init__(self):
        self.recovered = False
        self.calls = []
        self.creating = False
        self.get_failure = False

    def recover(self):
        self.recovered = True

    def statuses(self, actor, stories):
        self.calls.append(("statuses", copy.deepcopy(actor), list(stories)))
        return {"42": "succeeded"}

    def get(self, actor, story):
        self.calls.append(("get", copy.deepcopy(actor), story))
        if self.get_failure:
            raise PromotionError("not available")
        return {"story_id": story, "status": "succeeded", "koc_code": "ABC123"}

    def create(self, actor, story):
        self.calls.append(("create", copy.deepcopy(actor), story))
        return {"promotion": {"story_id": story, "status": "creating" if self.creating else "succeeded"}, "reused": not self.creating}


class ServiceRouteTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path("unused"), "institution", "not-used", "internal-test-only", "unused", "unused")
        self.db, self.sync, self.promotions = FakeDatabase(), FakeSync(), FakePromotions()
        self.app = Application(self.settings, db=self.db, provider=object(), sync=self.sync, promotions=self.promotions)

    def request(self, method, path, **extra):
        return self.app.dispatch(dict(method=method, path=path, actor=copy.deepcopy(ACTOR), **extra))

    def test_shared_run_is_explicit_projection_without_actor_or_future_private_fields(self):
        source = private_run()
        rendered = public_run(source)
        expected = {"id", "run_id", "status", "stage", "created_at", "started_at", "finished_at", "updated_at",
                    "total_pages", "fetched_pages", "compared_rows", "verified", "error", "total_rows", "processed_rows",
                    "inserted", "updated", "listed", "delisted", "unchanged"}
        self.assertEqual(set(rendered), expected)
        self.assertEqual(rendered["processed_rows"], 70)
        self.assertEqual(rendered["compared_rows"], 100)
        self.assertEqual(rendered["unchanged"], 40)
        self.assertNotIn("private-user", json.dumps(rendered))
        self.assertNotIn("/private/", json.dumps(rendered))
        self.assertIn("resume_actor", source)
        self.assertIsNone(public_run(None))

    def test_catalog_route_matches_current_list_contract_and_adds_personal_status(self):
        status, body = self.request("GET", "/dramas", query={"q": "A", "lang_id": "23"})
        self.assertEqual(status, 200)
        self.assertEqual(body["list"][0]["language_name"], "未知语言（23）")
        self.assertEqual(body["list"][0]["first_seen_at"], "2026-11-12T09:00:00.123Z")
        self.assertEqual(body["list"][0]["promotion_status"], "succeeded")
        self.assertEqual(self.db.query, {"q": "A", "lang_id": "23"})
        self.assertEqual(self.promotions.calls[-1], ("statuses", ACTOR, ["42"]))

    def test_detail_uses_source_story_id_and_preserves_source_time(self):
        status, body = self.request("GET", "/dramas/%34%32")
        self.assertEqual(status, 200)
        self.assertEqual(body["drama"]["story_id"], "42")
        self.assertEqual(body["drama"]["source_publish_time"], "2026-11-12 12:34:56")
        self.assertEqual(self.request("GET", "/dramas/not-found")[0], 404)

    def test_detail_remains_usable_when_promotion_read_fails(self):
        self.promotions.get_failure = True
        status, body = self.request("GET", "/dramas/42")
        self.assertEqual(status, 200)
        self.assertEqual(body["drama"]["story_id"], "42")
        self.assertIsNone(body["promotion"])
        self.assertIn("promotion_error", body)

    def test_sync_routes_reuse_active_run_and_never_reveal_private_resume_fields(self):
        status, body = self.request("POST", "/sync-runs", body={"trigger": "scheduled"})
        self.assertEqual(status, 202)
        self.assertTrue(body["reused"])
        self.assertEqual(self.sync.started, [("manual", ACTOR)])
        self.assertNotIn("resume_actor", body["run"])
        self.assertNotIn("actor", body["run"])
        status, body = self.request("GET", "/sync-runs/latest")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_success_at"], "2026-11-11T01:15:00.000Z")
        self.assertEqual(self.request("GET", "/sync-runs/" + RUN_ID)[0], 200)
        self.assertEqual(self.request("GET", "/sync-runs/" + "b" * 32)[0], 404)

    def test_scheduler_identity_is_required_for_scheduled_trigger(self):
        actor = dict(ACTOR, user_id="moboreels-scheduler", name="Daily scheduler")
        self.app.dispatch({"method": "POST", "path": "/sync-runs", "actor": actor, "body": {"trigger": "scheduled"}})
        self.assertEqual(self.sync.started[-1], ("scheduled", actor))

    def test_promotion_routes_ignore_frontend_identity_and_preserve_creating_response(self):
        status, body = self.request("POST", "/dramas/42/promotion", body={"star_user_id": "attacker", "actor": {"user_id": "attacker"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.promotions.calls[-1], ("create", ACTOR, "42"))
        self.assertTrue(body["reused"])
        self.promotions.creating = True
        self.assertEqual(self.request("POST", "/dramas/42/promotion", body={})[0], 202)
        self.assertEqual(self.request("GET", "/dramas/42/promotion")[0], 200)

    def test_invalid_actor_and_invalid_source_id_are_rejected(self):
        with self.assertRaises(PromotionError) as error:
            self.app.dispatch({"method": "GET", "path": "/dramas", "actor": {}})
        self.assertEqual(error.exception.status, 401)
        for path in ("/dramas/" + "a" * 65, "/dramas/%00"):
            with self.assertRaises(ValueError):
                self.request("GET", path)

    def test_internal_http_auth_error_mapping_and_catalog_contract(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.app
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()

        def post(token, refresh=False):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                envelope = json.dumps({"method": "GET", "path": "/dramas", "actor": ACTOR,
                                       "query": {"refresh": "1"} if refresh else {}}).encode()
                connection.request("POST", "/internal/dispatch", envelope,
                                   {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(post("wrong")[0], 403)
            status, body = post(self.settings.internal_token)
            self.assertEqual(status, 200)
            self.assertEqual(body["list"][0]["story_id"], "42")
            self.db.failure = True
            status, body = post(self.settings.internal_token, refresh=True)
            self.assertEqual(status, 503)
            self.assertEqual(body["error"], "database_unavailable")
            self.assertNotIn("private database", json.dumps(body))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()

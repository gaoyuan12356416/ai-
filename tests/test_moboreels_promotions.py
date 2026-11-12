import concurrent.futures
import copy
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import requests

from features.moboreels_library.promotions import PromotionError, PromotionManager, identity, interpret
from features.moboreels_library.provider import ProviderClient
from features.moboreels_library.settings import Settings


ACTOR = {"tenant_key": "tenant-a", "user_id": "user-a", "name": "测试用户"}


def response(story="story-a", code="ABC123"):
    return {"http_status": 200, "response": {"code": 200, "data": {
        "story_id": story, "project_type": 2, "koc_code": code,
        "deep_link": "https://eng.moboreels.com/test/" + code,
        "home_link": "https://eng.moboreels.com/video/" + code,
    }}}


class FakeDB:
    def __init__(self):
        self.records = {}
        self.drama = {"story_id": "story-a", "is_show": 1}
        self.fail_mirror = False
        self.fail_read = False
        self.mirror_calls = []

    def get_promotion(self, star, story):
        if self.fail_read:
            raise OSError("read unavailable")
        return copy.deepcopy(self.records.get((star, story)))

    def get_drama(self, story):
        return copy.deepcopy(self.drama)

    def mirror_promotion(self, record):
        self.mirror_calls.append(copy.deepcopy(record))
        if self.fail_mirror:
            raise OSError("write unavailable")
        self.records[(record["star_user_id"], record["story_id"])] = copy.deepcopy(record)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.error = None
        self.entered = None
        self.release = None
        self.lock = threading.Lock()

    def create_promotion(self, payload, request_id):
        with self.lock:
            self.calls.append((copy.deepcopy(payload), request_id))
            index = len(self.calls)
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(5):
            raise RuntimeError("test worker did not release")
        if self.error:
            raise self.error
        return response(payload["story_id"], "CODE" + str(index))


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), "institution-a", "not-real-token", "not-real-internal", "test", "test")
        self.db = FakeDB()
        self.provider = FakeProvider()
        self.manager = PromotionManager(self.settings, self.db, self.provider)

    def test_identity_is_stable_and_separates_tenant_and_user(self):
        star = identity("institution-a", ACTOR)[0]
        self.assertEqual(len(star), 32)
        self.assertEqual(star, identity("institution-a", dict(ACTOR, name="新名字"))[0])
        for actor in [dict(ACTOR, tenant_key="tenant-b"), dict(ACTOR, user_id="user-b")]:
            self.assertNotEqual(star, identity("institution-a", actor)[0])
        self.assertNotEqual(star, identity("institution-b", ACTOR)[0])
        for actor in [{}, dict(ACTOR, name=""), dict(ACTOR, user_id=" ")]:
            with self.assertRaises(PromotionError):
                identity("institution-a", actor)

    def test_same_user_reuses_saved_link_and_name_change_keeps_identity(self):
        first = self.manager.create(ACTOR, "story-a")
        again = self.manager.create(dict(ACTOR, name="更新姓名"), "story-a")
        self.assertEqual(first["promotion"]["status"], "succeeded")
        self.assertTrue(again["reused"])
        self.assertEqual(first["promotion"], again["promotion"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0][0]["star_user_id"], identity("institution-a", ACTOR)[0])
        self.assertEqual(self.provider.calls[0][0]["star_name"], ACTOR["name"])
        self.assertFalse({"tenant_key", "user_id", "request_json", "response_json", "request_id"} & set(first["promotion"]))

    def test_different_users_have_different_codes_and_private_statuses(self):
        other = dict(ACTOR, user_id="user-b", name="其他用户")
        first = self.manager.create(ACTOR, "story-a")["promotion"]
        self.assertIsNone(self.manager.get(other, "story-a"))
        self.assertEqual(self.manager.statuses(other, ["story-a"]), {})
        second = self.manager.create(other, "story-a")["promotion"]
        self.assertNotEqual(first["koc_code"], second["koc_code"])
        self.assertEqual(self.manager.get(ACTOR, "story-a")["koc_code"], first["koc_code"])
        self.assertEqual(len(self.provider.calls), 2)

    def test_concurrent_clicks_claim_only_one_creation(self):
        self.provider.entered, self.provider.release = threading.Event(), threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
            first = pool.submit(self.manager.create, ACTOR, "story-a")
            self.assertTrue(self.provider.entered.wait(3))
            others = [pool.submit(self.manager.create, ACTOR, "story-a") for _ in range(8)]
            results = [future.result(timeout=5) for future in others]
            self.assertTrue(all(item["reused"] and item["promotion"]["status"] == "creating" for item in results))
            self.provider.release.set()
            self.assertEqual(first.result(timeout=5)["promotion"]["status"], "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_timeout_is_unknown_and_never_replayed(self):
        self.provider.error = TimeoutError("upstream timeout")
        first = self.manager.create(ACTOR, "story-a")
        self.assertEqual(first["promotion"]["status"], "unknown")
        self.provider.error = None
        again = self.manager.create(ACTOR, "story-a")
        self.assertTrue(again["reused"])
        self.assertEqual(again["promotion"]["status"], "unknown")
        self.assertEqual(len(self.provider.calls), 1)

    def test_all_non_success_business_responses_stay_unknown_without_retry(self):
        for index, code in enumerate([None, 0, 400, 401, 429, 500, 503, "200", "invalid", [], {}]):
            story = "failure-%s" % index
            envelope = response(story)
            envelope["response"]["code"] = code
            with self.subTest(code=code), mock.patch.object(self.provider, "create_promotion", return_value=envelope) as create:
                self.assertEqual(interpret(envelope, story), ("unknown", {}))
                first = self.manager.create(ACTOR, story)
                self.assertEqual(first["promotion"]["status"], "unknown")
                restored = PromotionManager(self.settings, self.db, self.provider)
                restored.recover()
                again = restored.create(ACTOR, story)
                self.assertTrue(again["reused"])
                self.assertEqual(again["promotion"]["status"], "unknown")
                self.assertEqual(create.call_count, 1)

    def test_database_timestamps_are_utc_iso_in_response_and_local_cache(self):
        cases = [
            ("2026-11-12 01:02:03", "2026-11-12T01:02:03.000Z"),
            ("2026-11-12 01:02:03.123000", "2026-11-12T01:02:03.123Z"),
            ("2026-11-12T01:02:03.123", "2026-11-12T01:02:03.123Z"),
            ("2026-11-12T01:02:03.123Z", "2026-11-12T01:02:03.123Z"),
            ("2026-11-12T09:02:03.123+08:00", "2026-11-12T01:02:03.123Z"),
            (datetime.datetime(2026, 11, 12, 1, 2, 3, 123000), "2026-11-12T01:02:03.123Z"),
            (datetime.datetime(2026, 11, 12, 9, 2, 3, 123000, tzinfo=datetime.timezone(datetime.timedelta(hours=8))), "2026-11-12T01:02:03.123Z"),
        ]
        star = identity("institution-a", ACTOR)[0]
        for index, (value, expected) in enumerate(cases):
            story = "timestamp-%s" % index
            self.db.records[(star, story)] = dict(
                response(story)["response"]["data"], status="succeeded",
                created_at=value, updated_at=value)
            with self.subTest(value=value):
                promotion = self.manager.get(ACTOR, story)
                self.assertEqual(promotion["created_at"], expected)
                self.assertEqual(promotion["updated_at"], expected)
                with mock.patch.object(self.db, "get_promotion", side_effect=AssertionError("must use saved local record")):
                    self.assertEqual(self.manager.get(ACTOR, story), promotion)
                with self.manager.connect() as conn:
                    stored = self.manager._load(conn, self.manager.key(star, story))
                self.assertEqual(stored["created_at"], expected)
                self.assertEqual(stored["updated_at"], expected)
        self.assertFalse(self.provider.calls)

    def test_restart_recovers_durable_response_before_local_state_update(self):
        original = self.manager._save
        def interrupt_final_save(conn, key, record, pending=1):
            if record["status"] == "succeeded":
                raise RuntimeError("simulated process interruption after receipt")
            return original(conn, key, record, pending)
        with mock.patch.object(self.manager, "_save", side_effect=interrupt_final_save):
            with self.assertRaises(RuntimeError):
                self.manager.create(ACTOR, "story-a")
        restored = PromotionManager(self.settings, self.db, self.provider)
        restored.recover()
        result = restored.create(ACTOR, "story-a")
        self.assertTrue(result["reused"])
        self.assertEqual(result["promotion"]["status"], "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_restart_without_response_keeps_unknown(self):
        with mock.patch.object(self.manager, "_receipt", side_effect=OSError("disk write failed")):
            with self.assertRaises(OSError):
                self.manager.create(ACTOR, "story-a")
        restored = PromotionManager(self.settings, self.db, self.provider)
        restored.recover()
        self.assertEqual(restored.create(ACTOR, "story-a")["promotion"]["status"], "unknown")
        self.assertEqual(len(self.provider.calls), 1)

    def test_mirror_failure_does_not_change_external_success(self):
        result = self.manager.create(ACTOR, "story-a")
        self.db.fail_mirror = True
        with self.assertLogs(level="WARNING"):
            self.manager.flush_outbox()
        self.assertEqual(self.manager.get(ACTOR, "story-a")["status"], "succeeded")
        self.assertTrue(self.manager.create(ACTOR, "story-a")["reused"])
        self.db.fail_mirror = False
        self.manager.flush_outbox()
        mirrored = self.db.records[(identity("institution-a", ACTOR)[0], "story-a")]
        self.assertEqual(mirrored["koc_code"], result["promotion"]["koc_code"])
        self.assertEqual(len(self.provider.calls), 1)
        with self.manager.connect() as conn:
            self.assertEqual(conn.execute("SELECT mirror_pending FROM promotions").fetchone()[0], 0)

    def test_database_read_failure_does_not_start_new_creation(self):
        self.db.fail_read = True
        with self.assertRaises(PromotionError):
            self.manager.create(ACTOR, "story-a")
        self.assertFalse(self.provider.calls)

    def test_down_drama_rejects_new_but_keeps_existing_link(self):
        self.db.drama["is_show"] = 0
        with self.assertRaises(PromotionError) as caught:
            self.manager.create(ACTOR, "story-a")
        self.assertEqual(caught.exception.code, "drama_unavailable")
        self.assertFalse(self.provider.calls)
        self.db.drama["is_show"] = 1
        result = self.manager.create(ACTOR, "story-a")
        self.db.drama["is_show"] = 0
        again = self.manager.create(ACTOR, "story-a")
        self.assertTrue(again["reused"])
        self.assertEqual(result["promotion"], again["promotion"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_operator_created_restores_original_without_api_call(self):
        self.provider.error = TimeoutError()
        self.manager.create(ACTOR, "story-a")
        star = identity("institution-a", ACTOR)[0]
        with self.assertRaises(PromotionError):
            self.manager.resolve(star, "story-a", "created", "", {}, "operator")
        original = response()["response"]["data"]
        result = self.manager.resolve(star, "story-a", "created", "供应方后台已确认原口令", original, "operator")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["koc_code"], original["koc_code"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(self.manager.create(ACTOR, "story-a")["reused"])

    def test_operator_not_created_only_unlocks_explicit_retry(self):
        self.provider.error = TimeoutError()
        self.manager.create(ACTOR, "story-a")
        star = identity("institution-a", ACTOR)[0]
        result = self.manager.resolve(star, "story-a", "not-created", "供应方确认没有创建", operator="operator")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(self.provider.calls), 1, "resolution must not send a request")
        self.provider.error = None
        self.assertEqual(self.manager.create(ACTOR, "story-a")["promotion"]["status"], "succeeded")
        self.assertEqual(len(self.provider.calls), 2)

    def test_bad_success_payload_becomes_unknown_and_recovers(self):
        for data in [[], "unexpected", 1, None, {"story_id": "different", "project_type": 2}]:
            with self.subTest(data=data):
                self.assertEqual(interpret({"http_status": 200, "response": {"code": 200, "data": data}}, "story-a")[0], "unknown")
        for key, value in [("deep_link", "javascript:alert(1)"), ("home_link", "data:text/html,test"), ("deep_link", "https://user:password@eng.moboreels.com/code")]:
            envelope = response()
            envelope["response"]["data"][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(interpret(envelope, "story-a")[0], "unknown")
        for http in [301, 400, 500, 504]:
            self.assertEqual(interpret(dict(response(), http_status=http), "story-a")[0], "unknown")


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload
    def json(self):
        return self.payload


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path("unused"), "Institution", "MiXeD_FakeToken", "unused", "unused", "unused", api_interval=0)

    def test_exact_signature_headers_and_no_direct_token(self):
        http = mock.Mock()
        http.post.return_value = FakeResponse(200, response()["response"])
        client = ProviderClient(self.settings, http)
        with mock.patch("features.moboreels_library.provider.time.time", return_value=1700000000.123):
            client.create_promotion({"story_id": "story-a"}, "request-guid")
        args, kwargs = http.post.call_args
        self.assertEqual(args[0], "https://kocapi-cn.cdreader.com/api/story/getcodeinfo")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["timestamp"], "1700000000123")
        expected = hashlib.sha256("request-guid_1700000000123_institution_mixed_faketoken".encode()).hexdigest()
        self.assertEqual(kwargs["headers"]["sign"], expected)
        self.assertNotIn("token", kwargs["headers"])

    def test_creation_transport_failure_has_one_attempt(self):
        http = mock.Mock()
        http.post.side_effect = requests.Timeout()
        client = ProviderClient(self.settings, http)
        with self.assertRaises(requests.Timeout):
            client.create_promotion({"story_id": "story-a"}, "request-guid")
        self.assertEqual(http.post.call_count, 1)

    def test_read_retry_uses_fresh_request_ids(self):
        http = mock.Mock()
        http.post.side_effect = [FakeResponse(503, {}), FakeResponse(429, {}), FakeResponse(200, {"code": 200, "data": {"total": 1, "list": [{"story_id": "one"}]}})]
        client = ProviderClient(self.settings, http)
        with mock.patch("features.moboreels_library.provider.time.sleep"):
            result = client.get_page(3)
        self.assertEqual(result["total"], 1)
        calls = http.post.call_args_list
        self.assertEqual(len({call.kwargs["headers"]["reqid"] for call in calls}), 3)
        self.assertTrue(all(call.kwargs["json"] == {"project_type": 2, "status": 0, "page_index": 3, "page_size": 100} for call in calls))


if __name__ == "__main__":
    unittest.main()

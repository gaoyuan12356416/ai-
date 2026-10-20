"""Offline contract/outbox regression tests. Never contacts a network or production DB."""
import concurrent.futures
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features import cover_material_source as material

_yt_spec = importlib.util.spec_from_file_location(
    "offline_youtube_cover_sync",
    Path(__file__).resolve().parents[1] / "ops" / "youtube-cover-material-sync" / "sync.py")
ytsync = importlib.util.module_from_spec(_yt_spec)
_yt_spec.loader.exec_module(ytsync)


TOKEN = "unit-test-only-token-not-a-real-secret"


def source(**overrides):
    result = {
        "source_kind": "screenshot",
        "source_key": "screenshot:job-123:square_1x1",
        "app_id": 1479,
        "content_id": "drama-中文-123",
        "sha256": "a" * 64,
        "url": "https://ai.yingliangads.com/static/cover.jpg",
        "name": "中文剧目封面",
        "language": "en",
        "country": "",
        "category": "1:1",
        "owner_email": "owner@example.invalid",
        "system_actor": False,
        "is_backfill": False,
        "legacy_final_ids": [],
    }
    result.update(overrides)
    return result


def formal(request, **overrides):
    result = {
        "id": 901,
        "product": material.PRODUCTS[request["app_id"]],
        "type": 1,
        "content_sign": request["content_sign"],
        "data_source_id": request["content_id"],
        "name": request["name"],
        "category": request["category"],
        "country": request["country"],
        "language": request["language"],
        "user_id": request["user_id"],
        "initiator": request["initiator"],
        "is_delete": 0,
    }
    result.update(overrides)
    return result


class FakeResponse:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.body = body
        self.raw = raw

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return copy.deepcopy(self.body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _chunk_size):
        yield self.raw


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, copy.deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeCatalog:
    def __init__(self, person_id=248):
        self.person_id = person_id
        self.rows = {}
        self.find_error = None
        self.find_calls = 0

    def person(self, _source):
        return self.person_id

    def find(self, request):
        self.find_calls += 1
        if self.find_error:
            raise self.find_error
        return copy.deepcopy(self.rows.get(request["content_sign"], []))


class OfflineTest(unittest.TestCase):
    def setUp(self):
        # A missing mock fails locally before any socket or subprocess can be used.
        for target in ("socket.socket.connect", "socket.create_connection",
                       "requests.sessions.Session.request", "subprocess.run"):
            patch = mock.patch(target, side_effect=AssertionError("external I/O forbidden in offline QA"))
            patch.start()
            self.addCleanup(patch.stop)


class PayloadTests(OfflineTest):
    def test_all_four_products_keep_business_product_and_integer_people(self):
        for app_id in (1479, 979, 3360, 2373):
            with self.subTest(app_id=app_id):
                payload = material.make_payload(source(app_id=str(app_id)), 248)
                self.assertEqual(payload["app_id"], app_id)
                self.assertIs(type(payload["app_id"]), int)
                self.assertIs(type(payload["user_id"]), int)
                self.assertIs(type(payload["initiator"]), int)
                self.assertEqual(payload["user_id"], 248)
                self.assertEqual(payload["initiator"], 248)
                self.assertEqual(payload["content_id"], "drama-中文-123")
                self.assertEqual(payload["name"], "中文剧目封面")
                self.assertNotIn("Authorization", payload)

    def test_invalid_product_content_and_person_are_blocked(self):
        cases = [(source(app_id=1), 248), (source(content_id=""), 248),
                 (source(content_id=None), 248), (source(), 0),
                 (source(), -1), (source(), True), (source(), "248"),
                 (source(), 248.0)]
        for record, person in cases:
            with self.subTest(record=record, person=person):
                with self.assertRaises(ValueError):
                    material.make_payload(record, person)

    def test_all_language_country_defaults(self):
        expected = {
            "en": "US", "es": "ES", "pt": "PT", "fr": "FR", "de": "DE",
            "it": "IT", "pl": "PL", "ru": "RU", "tr": "TR", "ar": "SA",
            "ja": "JP", "ko": "KR", "id": "ID", "th": "TH", "vi": "VN",
            "cs": "CZ", "el": "GR", "ro": "RO", "zh-TW": "TW", "zh-CN": "CN",
        }
        for language, country in expected.items():
            with self.subTest(language=language):
                self.assertEqual(material.country_language("", language),
                                 (country, language.lower()))

    def test_valid_country_preserved_and_invalid_country_defaulted(self):
        self.assertEqual(material.country_language(" gb ", "EN-us"), ("GB", "en"))
        self.assertEqual(material.country_language("invalid", "pt-BR"), ("PT", "pt"))
        self.assertEqual(material.country_language("VI", "vi"), ("VI", "vi"))

    def test_language_aliases_and_unknown_language(self):
        aliases = {"zh_Hant": "zh-tw", "zh_Hans": "zh-cn", "jp": "ja",
                   "kr": "ko", "in": "id", "vn": "vi", "cz": "cs"}
        for given, expected in aliases.items():
            with self.subTest(language=given):
                self.assertEqual(material.canonical_language(given), expected)
        for language in ("", "unknown", "xx", None):
            with self.subTest(language=language):
                with self.assertRaisesRegex(ValueError, "language_unmapped"):
                    material.country_language("US", language)

    def test_identity_stable_across_metadata_backfill_and_dictionary_order(self):
        original = source()
        copied = dict(reversed(list(original.items())))
        copied.update(is_backfill=True, legacy_final_ids=[23], generated_at="2026-10-01")
        self.assertEqual(material.identity(original), material.identity(copied))
        self.assertEqual(len(material.identity(original)), 64)

    def test_identity_separates_size_version_product_and_source_kind(self):
        original = source()
        variants = [
            source(source_key="screenshot:job-123:landscape_1_91x1", category="1.91:1"),
            source(source_key="screenshot:job-123:portrait_4x5", category="4:5"),
            source(sha256="b" * 64), source(app_id=979),
            source(source_kind="youtube", source_key="youtube:video-123:v2"),
        ]
        self.assertEqual(len({material.identity(original), *(material.identity(v) for v in variants)}), 6)

    def test_formal_readback_matches_every_required_field(self):
        request = material.make_payload(source(), 248)
        self.assertEqual(material.verify_final(formal(request), request), [])
        for field in ("product", "type", "content_sign", "data_source_id", "name",
                      "category", "user_id", "initiator", "is_delete", "language"):
            with self.subTest(field=field):
                row = formal(request, **{field: "incorrect"})
                self.assertIn(field, material.verify_final(row, request))
        row = formal(request, country="us", language="EN-us", user_id="248")
        self.assertEqual(material.verify_final(row, request), [])

    def test_drama_catalog_country_may_be_empty_or_different(self):
        request = material.make_payload(source(country="US"), 248)
        for country in ("", None, "GB", "jp"):
            with self.subTest(country=country):
                self.assertEqual(material.verify_final(formal(request, country=country), request), [])

    def test_country_override_does_not_relax_language_or_person_validation(self):
        request = material.make_payload(source(country="US"), 248)
        for field, value in (("language", "ja"), ("user_id", 249), ("initiator", 249)):
            with self.subTest(field=field):
                result = material.verify_final(formal(request, country="", **{field: value}), request)
                self.assertEqual(result, [field])


class ClientTests(OfflineTest):
    def invoke(self, status=200, body=None):
        session = FakeSession(FakeResponse(status, body))
        client = material.Client({"AD_MATERIAL_SOURCE_API_TOKEN": TOKEN}, session)
        return client.post(material.make_payload(source(), 248)), session

    def test_endpoint_token_and_standard_bearer_json_request(self):
        (state, receipt), session = self.invoke(body={"code": 0, "data": {"id": 42}})
        self.assertEqual((state, receipt["api_id"]), ("accepted", 42))
        endpoint, kwargs = session.calls[0]
        self.assertEqual(endpoint, material.ENDPOINT)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (5, 60))
        self.assertNotIn(TOKEN, material.encoded(receipt))
        with self.assertRaisesRegex(ValueError, "token_missing"):
            material.Client({}, session)
        with self.assertRaisesRegex(ValueError, "unexpected_material_source_endpoint"):
            material.Client({"AD_MATERIAL_SOURCE_API_TOKEN": TOKEN,
                             "AD_MATERIAL_SOURCE_API_URL": "https://example.invalid"}, session)

    def test_authentication_json_is_rejected(self):
        for status in (401, 403):
            with self.subTest(status=status):
                (state, receipt), _ = self.invoke(status, {"error": TOKEN})
                self.assertEqual(state, "rejected")
                self.assertEqual(receipt["reason"], "authentication_rejected")
                self.assertNotIn(TOKEN, material.encoded(receipt))

    def test_authentication_html_is_rejected(self):
        for status in (401, 403):
            with self.subTest(status=status):
                (state, receipt), _ = self.invoke(status, ValueError("HTML response"))
                self.assertEqual(state, "rejected")
                self.assertEqual(receipt["reason"], "authentication_rejected")

    def test_business_validation_rejected_and_server_failure_unknown(self):
        for code, expected in ((-1, "rejected"), ("-1", "rejected"), (-2, "unknown")):
            with self.subTest(code=code):
                (state, receipt), _ = self.invoke(body={"code": code, "message": TOKEN})
                self.assertEqual(state, expected)
                self.assertNotIn(TOKEN, material.encoded(receipt))

    def test_duplicate_requires_readback(self):
        for message in ("素材已经存在", "素材已存在", "duplicate content_sign"):
            with self.subTest(message=message):
                (state, receipt), _ = self.invoke(body={"code": -1, "msg": message})
                self.assertEqual(state, "unknown")
                self.assertEqual(receipt["reason"], "duplicate_requires_readback")

    def test_invalid_json_non_object_or_missing_nested_id_unknown(self):
        bodies = [ValueError("invalid JSON"), [], "ok", None, {},
                  {"id": 12}, {"code": 0, "id": 12}, {"code": 0, "data": {}},
                  {"data": []}, {"data": {"id": True}}, {"data": {"id": 0}},
                  {"data": {"id": -3}}, {"data": {"id": "2.5"}}]
        for body in bodies:
            with self.subTest(body=body):
                (state, receipt), _ = self.invoke(body=body)
                self.assertEqual(state, "unknown")
                self.assertNotIn("api_id", receipt)

    def test_positive_success_code_with_valid_id_is_accepted(self):
        for code in (1, 201, "1", "201", 10000):
            with self.subTest(code=code):
                (state, receipt), _ = self.invoke(body={"code": code, "data": {"id": "42"}})
                self.assertEqual(state, "accepted")
                self.assertEqual(receipt["api_id"], 42)

    def test_unspecified_success_code_valid_id_and_message_redaction(self):
        for code in (None, 0, "0", "success", 200):
            with self.subTest(code=code):
                body = {"data": {"id": 42}, "msg": TOKEN}
                if code is not None:
                    body["code"] = code
                (state, receipt), _ = self.invoke(body=body)
                self.assertEqual(state, "accepted")
                self.assertNotIn(TOKEN, material.encoded(receipt))

    def test_http_failure_and_unknown_negative_code_never_accepted(self):
        for status, code in ((500, 0), (429, 0), (302, 0), (200, -3),
                             (200, "-3"), (200, "-1.0")):
            with self.subTest(status=status, code=code):
                (state, _receipt), _ = self.invoke(status, {"code": code, "data": {"id": 42}})
                self.assertNotEqual(state, "accepted")

    def test_explicit_false_success_is_not_accepted_even_with_id(self):
        (state, _receipt), _ = self.invoke(body={"success": False, "data": {"id": 42}})
        self.assertNotEqual(state, "accepted")

    def test_arbitrary_remote_code_is_not_persisted_as_secret(self):
        (_state, receipt), _ = self.invoke(body={"code": TOKEN, "data": {"id": 42}})
        self.assertNotIn(TOKEN, material.encoded(receipt))


class LedgerTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = material.Ledger(self.tmp.name)
        self.addCleanup(self.ledger.close)
        self.catalog = FakeCatalog()
        self.client = mock.Mock()
        self.client.post.return_value = ("accepted", {"api_id": 42, "http_status": 200})
        self.inspect = mock.Mock(return_value={"sha256": "a" * 64})

    def send(self, record=None):
        return material.submit(record or source(), self.ledger, self.catalog, self.client,
                               verify_image=self.inspect)

    def test_prepare_is_stable_and_freezes_request(self):
        record = source()
        request = material.make_payload(record, 248)
        first = self.ledger.prepare(record, request)
        self.assertEqual(first["state"], "prepared")
        self.assertEqual(self.ledger.prepare(record, request)["key"], first["key"])
        with self.assertRaisesRegex(ValueError, "frozen_request_changed"):
            self.ledger.prepare(record, dict(request, name="changed"))
        self.assertEqual(self.ledger.summary(), {"prepared": 1})

    def test_submit_rejects_metadata_changes_for_existing_image_identity(self):
        self.send()
        changes = {
            "content_id": "other-drama", "name": "changed name",
            "url": "https://ai.yingliangads.com/other.jpg", "category": "4:5",
            "owner_email": "other@example.invalid", "system_actor": True,
            "country": "GB", "language": "ja",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "frozen_source_changed"):
                    self.send(source(**{field: value}))
        self.assertEqual(self.client.post.call_count, 1)

    def test_confirmed_identity_does_not_silently_adopt_changed_content(self):
        first = self.send()
        request = json.loads(first["request_json"])
        self.catalog.rows[first["key"]] = [formal(request)]
        self.assertEqual(self.send()["state"], "confirmed")
        with self.assertRaisesRegex(ValueError, "frozen_source_changed"):
            self.send(source(content_id="other-drama"))
        self.assertEqual(self.client.post.call_count, 1)

    def test_backfill_metadata_and_equivalent_language_keep_same_attempt(self):
        self.send()
        row = self.send(source(is_backfill=True, backfill_kind="migration",
                               legacy_final_ids=[90], generated_at="2026-01-01",
                               country=" us ", language="EN-us"))
        self.assertEqual((row["state"], row["attempts"]), ("accepted", 1))
        self.assertEqual(self.client.post.call_count, 1)

    def test_attempt_is_committed_before_post_and_crash_cannot_repeat(self):
        def crash(_request):
            check = sqlite3.connect(self.ledger.path)
            try:
                state, attempts = check.execute("SELECT state, attempts FROM uploads").fetchone()
                self.assertEqual((state, attempts), ("unknown", 1))
                self.assertEqual(check.execute(
                    "SELECT COUNT(*) FROM events WHERE state='outbound_started'").fetchone()[0], 1)
            finally:
                check.close()
            raise KeyboardInterrupt("simulated process interruption")
        self.client.post.side_effect = crash
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        result = self.send()
        self.assertEqual((result["state"], result["attempts"]), ("unknown", 1))
        self.assertEqual(self.client.post.call_count, 1)

    def test_timeout_then_readback_preserves_formal_evidence_but_requires_receipt(self):
        self.client.post.side_effect = TimeoutError(TOKEN)
        first = self.send()
        self.assertEqual((first["state"], first["attempts"]), ("unknown", 1))
        request = json.loads(first["request_json"])
        self.catalog.rows[first["key"]] = [formal(request)]
        second = self.send()
        self.assertEqual((second["state"], second["api_id"], second["final_id"]),
                         ("unknown", None, 901))
        self.assertEqual(second["error"], "formal_found_receipt_missing")
        self.assertEqual(json.loads(second["final_json"]), formal(request))
        third = self.send()
        self.assertEqual(third["state"], "unknown")
        self.assertEqual(self.client.post.call_count, 1)
        events = [dict(row) for row in self.ledger.db.execute("SELECT * FROM events")]
        self.assertNotIn(TOKEN, material.encoded([second, events]))

    def test_accepted_receipt_waits_for_formal_readback(self):
        first = self.send()
        self.assertEqual((first["state"], first["api_id"], first["final_id"]), ("accepted", 42, None))
        second = self.send()
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(self.client.post.call_count, 1)
        self.catalog.rows[first["key"]] = [formal(json.loads(first["request_json"]))]
        third = self.send()
        self.assertEqual((third["state"], third["api_id"], third["final_id"]), ("confirmed", 42, 901))
        self.assertEqual(self.send()["state"], "confirmed")
        self.assertEqual(self.client.post.call_count, 1)

    def test_preexisting_formal_readback_requires_receipt_without_post(self):
        request = material.make_payload(source(), 248)
        self.catalog.rows[request["content_sign"]] = [formal(request)]
        result = self.send()
        self.assertEqual((result["state"], result["attempts"], result["api_id"], result["final_id"]),
                         ("unknown", 0, None, 901))
        self.assertEqual(result["error"], "formal_found_receipt_missing")
        self.assertEqual(self.send()["state"], "unknown")
        self.client.post.assert_not_called()
        self.inspect.assert_not_called()

    def test_duplicate_or_missing_id_readback_cannot_confirm_or_repeat_post(self):
        for index, response in enumerate((
                {"code": -1, "msg": "素材已经存在!"},
                {"code": 0, "data": {}},
                {"code": 0, "id": 42})):
            with self.subTest(response=response):
                record = source(source_key="missing-receipt:" + str(index))
                session = FakeSession(FakeResponse(body=response))
                actual_client = material.Client({"AD_MATERIAL_SOURCE_API_TOKEN": TOKEN}, session)
                first = material.submit(record, self.ledger, self.catalog, actual_client,
                                        verify_image=self.inspect)
                request = json.loads(first["request_json"])
                self.catalog.rows[first["key"]] = [formal(request)]
                for _ in range(2):
                    result = material.submit(record, self.ledger, self.catalog, actual_client,
                                             verify_image=self.inspect)
                    self.assertEqual((result["state"], result["api_id"], result["final_id"]),
                                     ("unknown", None, 901))
                    self.assertEqual(result["error"], "formal_found_receipt_missing")
                self.assertEqual(len(session.calls), 1)

    def test_invalid_persisted_receipt_never_satisfies_formal_confirmation(self):
        for index, identifier in enumerate((None, 0, -1, 1.5, "invalid")):
            with self.subTest(identifier=identifier):
                record = source(source_key="invalid-receipt:" + str(index))
                request = material.make_payload(record, 248)
                row = self.ledger.prepare(record, request)
                row = self.ledger.change(row["key"], "accepted", api_id=identifier)
                self.catalog.rows[row["key"]] = [formal(request)]
                result = material.reconcile(self.ledger, self.catalog, row)
                self.assertEqual((result["state"], result["final_id"]), ("unknown", 901))
                self.assertEqual(result["error"], "formal_found_receipt_missing")
        self.assertFalse(material.valid_api_receipt({"api_id": True}))
        self.client.post.assert_not_called()

    def test_prior_confirmed_missing_receipt_is_readback_only_and_never_posts(self):
        for found in (False, True):
            with self.subTest(found=found):
                record = source(source_key="prior-confirmed:" + str(found))
                request = material.make_payload(record, 248)
                row = self.ledger.prepare(record, request)
                self.ledger.change(row["key"], "confirmed", final_id=901, final_json=material.encoded(formal(request)))
                if found:
                    self.catalog.rows[row["key"]] = [formal(request)]
                for _ in range(2):
                    result = self.send(record)
                    self.assertEqual((result["state"], result["api_id"], result["final_id"]),
                                     ("unknown", None, 901))
                    self.assertEqual(result["error"], "formal_found_receipt_missing" if found
                                     else "valid_api_receipt_missing")
        self.client.post.assert_not_called()
        self.inspect.assert_not_called()

    def test_confirmed_event_records_requested_and_catalog_country_including_empty(self):
        for index, country in enumerate(("", None, "GB")):
            with self.subTest(country=country):
                record = source(source_key="country-override:" + str(index), country="US")
                first = self.send(record)
                request = json.loads(first["request_json"])
                self.catalog.rows[request["content_sign"]] = [formal(request, country=country)]
                result = self.send(record)
                self.assertEqual(result["state"], "confirmed")
                self.assertEqual(json.loads(result["final_json"])["country"], country)
                raw = self.ledger.db.execute(
                    "SELECT detail_json FROM events WHERE key=? AND state='confirmed' ORDER BY id DESC LIMIT 1",
                    (result["key"],)).fetchone()[0]
                evidence = json.loads(raw)
                self.assertEqual(evidence["country_policy"], "drama_catalog_override")
                self.assertEqual(evidence["requested_country"], "US")
                self.assertEqual(evidence["formal_country"], country)
        self.assertEqual(self.client.post.call_count, 3)

    def test_country_override_with_wrong_language_or_person_stays_blocked(self):
        for field, value in (("language", "ja"), ("user_id", 249), ("initiator", 249)):
            with self.subTest(field=field):
                record = source(source_key="country-guard:" + field)
                request = material.make_payload(record, 248)
                self.catalog.rows[request["content_sign"]] = [formal(request, country="", **{field: value})]
                result = self.send(record)
                self.assertEqual(result["state"], "blocked")
                self.assertIn(field, result["error"])
        self.client.post.assert_not_called()

    def test_sql_failure_prevents_post_and_does_not_mark_zero_or_attempted(self):
        self.catalog.find_error = RuntimeError("catalog_read_failed_2013")
        with self.assertRaisesRegex(RuntimeError, "catalog_read_failed"):
            self.send()
        row = self.ledger.get(material.identity(source()))
        self.assertEqual((row["state"], row["attempts"]), ("prepared", 0))
        self.client.post.assert_not_called()
        self.inspect.assert_not_called()

    def test_post_success_sql_failure_keeps_receipt_and_never_resends(self):
        def accepted(_request):
            self.catalog.find_error = RuntimeError("catalog_read_failed_2013")
            return "accepted", {"api_id": 42}
        self.client.post.side_effect = accepted
        with self.assertRaises(RuntimeError):
            self.send()
        row = self.ledger.get(material.identity(source()))
        self.assertEqual((row["state"], row["attempts"], row["api_id"]), ("accepted", 1, 42))
        self.catalog.find_error = None
        self.send()
        self.assertEqual(self.client.post.call_count, 1)

    def test_formal_mismatch_or_duplicate_blocks_without_post(self):
        for matches in ("mismatch", "duplicate"):
            record = source(source_key="case:" + matches)
            request = material.make_payload(record, 248)
            self.catalog.rows[request["content_sign"]] = (
                [formal(request, product="wrong")] if matches == "mismatch"
                else [formal(request), formal(request, id=902)])
            result = self.send(record)
            self.assertEqual(result["state"], "blocked")
        self.client.post.assert_not_called()

    def test_rejected_and_unknown_do_not_repeat_post(self):
        for state in ("rejected", "unknown"):
            with self.subTest(state=state):
                record = source(source_key="case:" + state)
                self.client.post.return_value = (state, {"reason": "test"})
                self.send(record)
                self.send(record)
        self.assertEqual(self.client.post.call_count, 2)

    def test_partial_success_retains_each_image_and_never_repeats_attempted_image(self):
        records = [source(source_key="size:" + size, category=category)
                   for size, category in material.SPECS]
        def post(request):
            if request["category"] == "1.91:1":
                raise TimeoutError("simulated timeout")
            self.catalog.rows[request["content_sign"]] = [formal(request)]
            return "accepted", {"api_id": 42}
        self.client.post.side_effect = post
        rows = [self.send(record) for record in records]
        self.assertEqual([row["state"] for row in rows], ["confirmed", "unknown", "confirmed"])
        retried = [self.send(record) for record in records]
        self.assertEqual([row["state"] for row in retried], ["confirmed", "unknown", "confirmed"])
        self.assertEqual(self.client.post.call_count, 3)

    def test_image_preflight_failure_holds_zero_attempts(self):
        self.inspect.side_effect = ValueError("image_sha_changed")
        with self.assertRaisesRegex(ValueError, "image_sha_changed"):
            self.send()
        row = self.ledger.get(material.identity(source()))
        self.assertEqual((row["state"], row["attempts"]), ("prepared", 0))
        self.client.post.assert_not_called()

    def test_persistent_hold_prevents_new_outbound(self):
        (Path(self.tmp.name) / "HOLD").write_text("test", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "outbound_on_hold"):
            self.send()
        self.assertEqual(self.ledger.get(material.identity(source()))["attempts"], 0)
        self.client.post.assert_not_called()

    def test_receipt_storage_failure_retains_unknown_durable_attempt(self):
        real_change = self.ledger.change
        self.ledger.change = mock.Mock(side_effect=sqlite3.OperationalError("disk full"))
        with self.assertRaises(sqlite3.OperationalError):
            self.send()
        self.ledger.change = real_change
        row = self.send()
        self.assertEqual((row["state"], row["attempts"]), ("unknown", 1))
        self.assertEqual(self.client.post.call_count, 1)

    def test_concurrent_connections_only_one_can_claim_outbound(self):
        record = source()
        self.ledger.prepare(record, material.make_payload(record, 248))
        barrier = threading.Barrier(2)
        def worker():
            ledger = material.Ledger(self.tmp.name)
            try:
                barrier.wait(timeout=5)
                try:
                    ledger.begin_attempt(material.identity(record))
                    return "claimed"
                except ValueError as exc:
                    return str(exc)
            finally:
                ledger.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(outcomes, ["claimed", "outbound_already_attempted"])
        self.assertEqual(self.ledger.get(material.identity(record))["attempts"], 1)


class PersonAndScreenshotTests(OfflineTest):
    def catalog(self, rows):
        catalog = material.Catalog({"ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53",
                                    "ADMIN_MAPPING_MYSQL_PORT": "63350"})
        catalog.query = mock.Mock(return_value=rows)
        return catalog

    def test_real_person_requires_unique_active_email_match(self):
        catalog = self.catalog([{"id": 356}])
        self.assertEqual(catalog.person(source(owner_email=" Owner@Example.Invalid ")), 356)
        sql = catalog.query.call_args.args[0]
        self.assertIn("status=0", sql)
        self.assertIn(material.literal("owner@example.invalid"), sql)
        for rows in ([], [{"id": 3}, {"id": 4}], [{"id": None}]):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "person_mapping_not_unique"):
                    self.catalog(rows).person(source())
        with self.assertRaisesRegex(ValueError, "person_email_missing"):
            catalog.person(source(owner_email=""))

    def test_system_person_is_explicit_248_and_must_be_active(self):
        catalog = self.catalog([{"id": 248}])
        self.assertEqual(catalog.person(source(system_actor=True, owner_email="")), 248)
        sql = catalog.query.call_args.args[0]
        self.assertIn("sub_user_id=248", sql)
        self.assertIn("status=0", sql)
        with self.assertRaisesRegex(ValueError, "person_mapping_not_unique"):
            self.catalog([]).person(source(system_actor=True))

    def test_replica_guard_and_query_failures_never_fall_back(self):
        for host, port in (("localhost", 63350), ("101.32.56.53", 3306)):
            with self.subTest(host=host, port=port):
                with self.assertRaisesRegex(ValueError, "read_replica"):
                    material.Catalog({"ADMIN_MAPPING_MYSQL_HOST": host,
                                      "ADMIN_MAPPING_MYSQL_PORT": port})
        catalog = self.catalog([])
        catalog.query.side_effect = RuntimeError("catalog_read_failed_2013")
        with self.assertRaises(RuntimeError):
            catalog.person(source())

    def test_catalog_query_enforces_readonly_and_keeps_password_out_of_args(self):
        env = {"ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53",
               "ADMIN_MAPPING_MYSQL_PORT": "63350", "ADMIN_MAPPING_MYSQL_USER": "qa",
               "ADMIN_MAPPING_MYSQL_PASSWORD": TOKEN}
        catalog = material.Catalog(env)
        completed = mock.Mock(returncode=0, stdout='1\n{"id":248}\n', stderr="")
        with mock.patch.object(material.subprocess, "run", return_value=completed) as run:
            self.assertEqual(catalog.query("SELECT JSON_OBJECT('id',248)"), [{"id": 248}])
        args, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertNotIn(TOKEN, material.encoded(args))
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], TOKEN)
        self.assertNotIn("SQL_GATE_BYPASS", kwargs["env"])
        self.assertIn("SET SESSION TRANSACTION READ ONLY", kwargs["input"])
        self.assertIn("START TRANSACTION", kwargs["input"])
        self.assertIn("@@read_only=1", kwargs["input"])

    def test_catalog_error_suppresses_arbitrary_stderr_and_password(self):
        env = {"ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53",
               "ADMIN_MAPPING_MYSQL_USER": "qa", "ADMIN_MAPPING_MYSQL_PASSWORD": TOKEN}
        catalog = material.Catalog(env)
        completed = mock.Mock(returncode=1, stdout="", stderr="ERROR 2013 " + TOKEN)
        with mock.patch.object(material.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "^catalog_read_failed_2013$"):
                catalog.query("SELECT 1")

    def test_catalog_unknown_or_writable_replica_is_not_empty_success(self):
        env = {"ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53",
               "ADMIN_MAPPING_MYSQL_USER": "qa", "ADMIN_MAPPING_MYSQL_PASSWORD": TOKEN}
        for output in ("", "0\n", '{"id":248}\n'):
            with self.subTest(output=output):
                completed = mock.Mock(returncode=0, stdout=output, stderr="")
                with mock.patch.object(material.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, "catalog_not_readonly"):
                        material.Catalog(env).query("SELECT 1")

    def test_owner_email_is_read_from_temporary_source_db_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owners.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE drama_admin_user(user_id TEXT, email TEXT)")
                conn.execute("INSERT INTO drama_admin_user VALUES('u-1',' OWNER@Example.Invalid ')")
                conn.commit()
            self.assertEqual(material.owner_email(path, "u-1"), "owner@example.invalid")
            with self.assertRaisesRegex(ValueError, "creator_email_not_unique"):
                material.owner_email(path, "missing")
            with material.contextlib.closing(material.readonly(path)) as conn:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM drama_admin_user")

    def test_screenshot_three_size_identity_and_system_mapping(self):
        job = {"job_id": "job-123456", "app_id": "1479", "content_id": "drama-1",
               "creator_user_id": "screenshot-api-direct", "language": "en",
               "drama_name": "中文名称", "finished_at": "2026-10-19T12:00:00+08:00"}
        for size, _category in material.SPECS:
            job[size + "_url"] = "https://ai.yingliangads.com/" + size + ".jpg"
        inspect = mock.Mock(return_value={"sha256": "a" * 64, "width": 100, "height": 100})
        rows = material.screenshot_sources(job, db_path="must-not-be-opened.sqlite", inspect=inspect)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["category"] for row in rows], ["1:1", "1.91:1", "4:5"])
        self.assertEqual(len({material.identity(row) for row in rows}), 3)
        self.assertTrue(all(row["system_actor"] for row in rows))
        self.assertTrue(all(row["content_id"] == "drama-1" for row in rows))
        self.assertTrue(all(row["generated_at"] == job["finished_at"] for row in rows))
        job["creator_user_id"] = "api:screenshot-api-direct"
        prefixed = material.screenshot_sources(job, db_path="must-not-be-opened.sqlite", inspect=inspect)
        self.assertTrue(all(row["system_actor"] for row in prefixed))

    def test_missing_image_fails_before_ingestion(self):
        job = {"job_id": "job-1", "app_id": 1479, "content_id": "drama-1",
               "creator_user_id": "screenshot-api-direct"}
        with self.assertRaisesRegex(ValueError, "three_cover_images_required"):
            material.screenshot_sources(job, inspect=mock.Mock())


class ImageTests(OfflineTest):
    def png(self, width=100, height=100):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
        return buffer.getvalue()

    def session(self, raw, status=200):
        session = mock.Mock()
        session.get.return_value = FakeResponse(status=status, raw=raw)
        return session

    def test_image_sha_and_dimensions_from_downloaded_bytes(self):
        raw = self.png()
        sha = hashlib.sha256(raw).hexdigest()
        session = self.session(raw)
        result = material.inspect_image("https://ai.yingliangads.com/a.png", sha, "1:1", session)
        self.assertEqual((result["sha256"], result["width"], result["height"]), (sha, 100, 100))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_untrusted_url_hash_change_ratio_mismatch_and_http_fail(self):
        session = self.session(self.png())
        for url in ("http://ai.yingliangads.com/a.png", "https://evil.invalid/a.png",
                    "https://user:password@ai.yingliangads.com/a.png"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "untrusted_image_url"):
                    material.inspect_image(url, session=session)
        url = "https://ai.yingliangads.com/a.png"
        with self.assertRaisesRegex(ValueError, "image_sha_changed"):
            material.inspect_image(url, "b" * 64, session=session)
        with self.assertRaisesRegex(ValueError, "image_ratio_mismatch"):
            material.inspect_image(url, expected_category="4:5", session=session)
        with self.assertRaisesRegex(ValueError, "image_http_403"):
            material.inspect_image(url, session=self.session(b"", 403))


class YoutubeIntegrationTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ytsync.Store(Path(self.tmp.name) / "sync.sqlite3")
        self.addCleanup(self.store.close)
        self.env = {"AI_COVER_SOURCE_ROOT": str(Path(self.tmp.name) / "core"),
                    "AD_MATERIAL_SOURCE_API_TOKEN": TOKEN,
                    "COS_DOMAIN": "advertising-1306474899.cos.ap-hongkong.myqcloud.com"}
        self.old_catalog = mock.Mock()
        self.old_catalog.rows.return_value = []
        self.new_catalog = FakeCatalog()
        self.client = mock.Mock()
        self.client.post.return_value = ("accepted", {"api_id": 42})
        self.publisher = mock.Mock()
        self.inspect = mock.Mock(return_value={"sha256": "a" * 64})
        self.real_submit = material.submit
        # Exercise the real shared outbox, replacing only external dependencies.
        patches = [
            mock.patch.object(material, "Catalog", return_value=self.new_catalog),
            mock.patch.object(material, "Client", return_value=self.client),
            mock.patch.object(material, "submit", side_effect=lambda *args:
                              self.real_submit(*args, verify_image=self.inspect)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def add_source(self, task_id="yt-task-123", state="pending", attempts=0):
        item = {
            "task_id": task_id, "version": 2, "asset_id": "asset-1",
            "path": str(Path(self.tmp.name) / "mock-cover.jpg"), "sha256": "a" * 64,
            "asset_created_at": "2026-10-18T00:00:00+00:00",
            "tenant": "tenant-1", "owner": "owner-1", "width": 1664, "height": 936,
            "category": "16:9", "owner_name": "Test User",
            "owner_email": "owner@example.invalid", "content_id": "drama-yt",
            "title": "中文 YouTube 封面", "language": "ja", "cover_source": "ai",
            "source_scope": "published", "video_id": "AbcDef12345",
        }
        self.store.add([item])
        key, _mid = ytsync.identity(item)
        if state != "pending" or attempts:
            self.store.change(key, state, attempts=attempts, next_check=0)
        return item, key

    def tick(self):
        ytsync.custom_tick(self.store, self.old_catalog, self.publisher, self.env)

    def row(self, key):
        return dict(self.store.connection.execute(
            "SELECT * FROM cover_sync WHERE key=?", (key,)).fetchone())

    def force_due(self, key):
        with self.store.connection:
            self.store.connection.execute("UPDATE cover_sync SET next_check=0 WHERE key=?", (key,))

    def test_old_accepted_and_unknown_only_reconcile_legacy_protocol(self):
        self.add_source("old-accepted", "accepted", 1)
        self.add_source("old-unknown", "unknown", 1)
        self.tick()
        self.assertGreater(self.old_catalog.rows.call_count, 0)
        self.client.post.assert_not_called()
        self.publisher.post.assert_not_called()
        self.publisher.upload.assert_not_called()
        self.assertEqual(self.new_catalog.find_calls, 0)
        self.assertEqual(self.store.summary()["counts"], {"accepted": 1, "unknown": 1})

    def test_old_readback_sql_failure_prevents_all_new_post(self):
        self.add_source("old-unknown", "unknown", 1)
        self.add_source("new-pending")
        self.old_catalog.rows.side_effect = RuntimeError("catalog_read_failed")
        with self.assertRaises(RuntimeError):
            self.tick()
        self.client.post.assert_not_called()
        self.publisher.upload.assert_not_called()

    def test_pending_upload_uses_shared_core_and_original_product_content(self):
        _item, key = self.add_source()
        self.tick()
        self.publisher.upload.assert_called_once()
        self.publisher.post.assert_not_called()
        self.client.post.assert_called_once()
        request = self.client.post.call_args.args[0]
        self.assertEqual((request["app_id"], request["content_id"]), (1479, "drama-yt"))
        self.assertEqual((request["category"], request["country"], request["language"]),
                         ("16:9", "JP", "ja"))
        self.assertEqual((request["user_id"], request["initiator"]), (248, 248))
        self.assertEqual(self.row(key)["state"], "custom_pending")
        with contextlib.closing(sqlite3.connect(Path(self.env["AI_COVER_SOURCE_ROOT"]) / "upload.sqlite3")) as db:
            state, attempts, raw = db.execute(
                "SELECT state, attempts, source_json FROM uploads").fetchone()
        self.assertEqual((state, attempts), ("accepted", 1))
        self.assertEqual(json.loads(raw)["source_kind"], "youtube")

    def test_unknown_on_next_tick_only_reconciles_without_second_post_or_upload(self):
        _item, key = self.add_source()
        self.client.post.side_effect = TimeoutError("simulated timeout")
        self.tick()
        self.assertEqual(self.row(key)["state"], "custom_pending")
        self.force_due(key)
        self.tick()
        self.assertEqual(self.client.post.call_count, 1)
        self.assertEqual(self.publisher.upload.call_count, 1)
        self.assertEqual(self.row(key)["state"], "custom_pending")

    def test_timeout_formal_readback_stays_pending_without_receipt_and_never_repeats(self):
        _item, key = self.add_source()
        self.client.post.side_effect = TimeoutError("simulated timeout")
        self.tick()
        request = self.client.post.call_args.args[0]
        self.new_catalog.rows[request["content_sign"]] = [formal(request)]
        self.force_due(key)
        self.tick()
        self.assertEqual((self.row(key)["state"], self.row(key)["final_id"]), ("custom_pending", 901))
        uploads = self.publisher.upload.call_count
        self.force_due(key)
        self.tick()
        self.assertEqual(self.publisher.upload.call_count, uploads)
        self.assertEqual(self.client.post.call_count, 1)
        with contextlib.closing(sqlite3.connect(Path(self.env["AI_COVER_SOURCE_ROOT"]) / "upload.sqlite3")) as db:
            state, api_id, final_id, error = db.execute(
                "SELECT state, api_id, final_id, error FROM uploads").fetchone()
        self.assertEqual((state, api_id, final_id, error),
                         ("unknown", None, 901, "formal_found_receipt_missing"))

    def test_valid_receipt_and_formal_readback_confirm_without_repeating_upload(self):
        _item, key = self.add_source()
        self.tick()
        request = self.client.post.call_args.args[0]
        self.new_catalog.rows[request["content_sign"]] = [formal(request)]
        self.force_due(key)
        self.tick()
        self.assertEqual((self.row(key)["state"], self.row(key)["final_id"]), ("confirmed", 901))
        self.tick()
        self.assertEqual(self.publisher.upload.call_count, 1)
        self.assertEqual(self.client.post.call_count, 1)

    def test_legacy_ready_with_attempt_never_crosses_protocol(self):
        self.add_source(state="ready", attempts=1)
        self.tick()
        self.publisher.upload.assert_not_called()
        self.client.post.assert_not_called()

    def test_legacy_publisher_post_permanently_retired(self):
        publisher = object.__new__(ytsync.Publisher)
        with self.assertRaisesRegex(RuntimeError, "legacy_material_post_retired"):
            publisher.post([{"test": "request"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Offline regression for MySQL batch-safe YouTube credential JSON transport."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import COMMENT_SCOPE, DramaSynthesisError, scope_capabilities
from features.drama_synthesis.youtube import YouTubeCredentialRepository


CHANNEL = "UCHJ1jFaYuW8g5EM7hM5pPpg"
UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN = {"refresh_token": "fixture-refresh", "scope": [UPLOAD, "https://www.googleapis.com/auth/youtube", READONLY, COMMENT_SCOPE]}
CLIENT = {"client_id": "fixture-client", "client_secret": "fixture-secret"}


def hex_text(text):
    return text.encode("utf-8").hex().upper()


def hex_json(value):
    return hex_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def mysql_batch_escape(text):
    return text.replace("\\", "\\\\").replace("\x00", "\\0").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


class YouTubeRepositoryEncodingTests(unittest.TestCase):
    def setUp(self):
        self.probe = mock.Mock(return_value=True)
        self.sql = []
        self.network = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden"))
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)

    def repo(self, token_hex=None, client_hex=None, *, status="1"):
        token_hex = hex_json(TOKEN) if token_hex is None else token_hex
        client_hex = hex_json({"web": CLIENT}) if client_hex is None else client_hex
        row = ("263", CHANNEL, "Shahrul Ikmal", status, "255", token_hex, client_hex)
        def query(sql):
            self.sql.append(sql)
            self.assertIn("HEX(COALESCE(a.account_token,''))", sql)
            self.assertIn("HEX(COALESCE(a.account_credentials,''))", sql)
            return [row]
        return YouTubeCredentialRepository(query, identity_probe=self.probe)

    def credential(self, repo):
        return repo.credential(app_id="1479", channel_local_id="263", account_id="255", expected_channel_id=CHANNEL)

    def assert_ineligible(self, token_value, client_value):
        # Explicit values, including None, must not be replaced by valid fixtures.
        repo = self.repo()
        row = ("263", CHANNEL, "Shahrul Ikmal", "1", "255", token_value, client_value)
        repo.query_runner = lambda _sql: [row]
        self.assertEqual(repo.list_for_app("1479"), [])
        with self.assertRaises(DramaSynthesisError) as error:
            self.credential(repo)
        self.assertEqual(error.exception.code, "youtube_channel_not_eligible")
        self.probe.assert_not_called()
        self.network_mock.assert_not_called()

    def test_mysql_batch_escaped_slashes_restore_exact_scopes(self):
        raw = json.dumps(TOKEN).replace("/", r"\/")
        # The old plain-JSON projection is double-escaped by mysql -B.
        old_scopes = json.loads(mysql_batch_escape(raw))["scope"]
        self.assertIn(r"https:\/\/", old_scopes[0])
        self.assertFalse(scope_capabilities(old_scopes)["upload_eligible"])
        # HEX is unchanged by batch escaping, so JSON is decoded exactly once.
        encoded = hex_text(raw)
        self.assertEqual(mysql_batch_escape(encoded), encoded)
        credential = self.credential(self.repo(mysql_batch_escape(encoded)))
        self.assertEqual(credential.refresh_token, TOKEN["refresh_token"])
        for key in ("eligible", "upload_eligible", "identity_eligible", "comment_eligible"):
            self.assertIs(credential.capabilities[key], True)
        self.assertIn(UPLOAD, credential.scopes)
        self.assertFalse(any("\\" in scope for scope in credential.scopes))

    def test_backslash_slash_controls_and_unicode_are_preserved_exactly(self):
        value = "fixture/\\literal\\/\tTAB\nLINE\rCR\x00NUL-中文😀-\"quote\""
        token = dict(TOKEN, refresh_token=value)
        client = {"client_id": value + "id", "client_secret": value + "secret"}
        for ensure_ascii in (False, True):
            token_hex = hex_text(json.dumps(token, ensure_ascii=ensure_ascii).replace("/", r"\/"))
            client_hex = hex_text(json.dumps({"installed": client}, ensure_ascii=ensure_ascii).replace("/", r"\/"))
            with self.subTest(ensure_ascii=ensure_ascii):
                credential = self.credential(self.repo(token_hex, client_hex))
                self.assertEqual(credential.refresh_token, token["refresh_token"])
                self.assertEqual(credential.client_id, client["client_id"])
                self.assertEqual(credential.client_secret, client["client_secret"])
                self.assertTrue(credential.capabilities["eligible"])

    def test_client_config_web_installed_and_direct_stay_supported(self):
        for client in ({"web": CLIENT}, {"installed": CLIENT}, CLIENT):
            with self.subTest(wrapper=tuple(client)):
                credential = self.credential(self.repo(client_hex=hex_json(client)))
                self.assertEqual((credential.client_id, credential.client_secret), (CLIENT["client_id"], CLIENT["client_secret"]))

    def test_uppercase_and_lowercase_hex_stay_supported(self):
        for transform in (str.upper, str.lower):
            with self.subTest(transform=transform.__name__):
                self.assertTrue(self.credential(self.repo(transform(hex_json(TOKEN)), transform(hex_json(CLIENT)))).capabilities["eligible"])

    def test_bad_hex_never_falls_back_to_raw_json_or_probes_identity(self):
        bad_values = (None, 123, True, {}, b"7B7D", "", "0", "GG", "7B 7D", "7B\t7D", "7B7D\n", "0x7B7D", json.dumps(TOKEN), json.dumps({"web": CLIENT}))
        for bad in bad_values:
            for column in ("token", "client"):
                with self.subTest(column=column, bad_type=type(bad).__name__, bad_index=bad_values.index(bad)):
                    self.assert_ineligible(bad if column == "token" else hex_json(TOKEN), bad if column == "client" else hex_json({"web": CLIENT}))

    def test_invalid_utf8_is_not_replaced_or_decoded_as_latin1(self):
        for bad in ("FF", "C328", "EDA080", "EFBBBF7B7D"):
            for column in ("token", "client"):
                with self.subTest(column=column, hex_shape=bad):
                    self.assert_ineligible(bad if column == "token" else hex_json(TOKEN), bad if column == "client" else hex_json(CLIENT))

    def test_invalid_or_non_object_json_is_ineligible_without_probe(self):
        for text in ("", "{", "{} trailing", "[]", "null", "true", "123", '"json text"', json.dumps(json.dumps(TOKEN))):
            for column in ("token", "client"):
                with self.subTest(column=column, text=text):
                    self.assert_ineligible(hex_text(text) if column == "token" else hex_json(TOKEN), hex_text(text) if column == "client" else hex_json(CLIENT))

    def test_literal_backslashes_in_stored_scope_are_not_reinterpreted(self):
        malformed = dict(TOKEN, scope=[value.replace("/", r"\/") for value in TOKEN["scope"]])
        self.assert_ineligible(hex_json(malformed), hex_json(CLIENT))

    def test_existing_scope_and_channel_status_gates_remain_unchanged(self):
        for scope, status in (([UPLOAD], "1"), ([READONLY], "1"), ([], "1"), (TOKEN["scope"], "2")):
            with self.subTest(scope=scope, status=status):
                repo = self.repo(hex_json(dict(TOKEN, scope=scope)), status=status)
                self.assertEqual(repo.list_for_app("1479"), [])
                with self.assertRaises(DramaSynthesisError):
                    self.credential(repo)
        self.probe.assert_not_called()

    def test_comment_scope_is_not_inferred_from_upload_or_identity(self):
        credential = self.credential(self.repo(hex_json(dict(TOKEN, scope=[UPLOAD, READONLY]))))
        self.assertTrue(credential.capabilities["eligible"])
        self.assertFalse(credential.capabilities["comment_eligible"])

    def test_query_is_select_only_and_safe_dto_excludes_credentials(self):
        repo = self.repo()
        items = repo.list_for_app("1479")
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]), {"channel_local_id", "channel_id", "channel_name", "youtube_account_id", "upload_eligible", "identity_eligible", "comment_eligible"})
        self.assertEqual(items[0]["youtube_account_id"], "255")
        self.probe.assert_called_once()
        self.assertIn("`kunlunads_dev`.ads_youtube_channels", self.sql[0])
        self.assertIn("`kunlunads_dev`.ads_youtube_accounts", self.sql[0])
        self.assertTrue(all(sql.strip().startswith("SELECT ") for sql in self.sql))
        for secret in (TOKEN["refresh_token"], CLIENT["client_id"], CLIENT["client_secret"]):
            self.assertNotIn(secret, json.dumps(items))
            self.assertFalse(any(secret in sql for sql in self.sql))
        self.network_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

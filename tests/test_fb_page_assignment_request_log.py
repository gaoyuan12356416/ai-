import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from features.fb_page_assignment.request_log import RequestBodyLog, redact_body


class RequestBodyLogTests(unittest.TestCase):
    def test_preserves_raw_whitespace_duplicate_keys_and_invalid_field_types(self):
        raw = b'{\n  "external_task_id": "first", "external_task_id": 123,\n "data": {"drama_ids": ["d"]}\n}'
        self.assertEqual(redact_body(raw), (raw.decode(), False, True))

    def test_redacts_nested_duplicate_and_escaped_sensitive_fields_without_losing_other_fields(self):
        raw = (b'{ "external_task_id":123, "token":"first-secret", "token":"second-secret",'
               b'"data":{"\\u0041uthorization":"Bearer third-secret","password":{"v":"fourth-secret"}},'
               b'"drama_ids":["d"],"api_key":42 }')
        body, redacted, utf8_valid = redact_body(raw)
        self.assertTrue(redacted)
        self.assertTrue(utf8_valid)
        for secret in ("first-secret", "second-secret", "third-secret", "fourth-secret"):
            self.assertNotIn(secret, body)
        self.assertIn('"external_task_id":123', body)
        self.assertIn('"drama_ids":["d"]', body)
        self.assertEqual(json.loads(body)["data"]["password"], "[REDACTED]")
        self.assertEqual(json.loads(body)["api_key"], "[REDACTED]")

    def test_redacts_known_api_token_in_arbitrary_fields_and_unicode_escapes(self):
        body, redacted, _ = redact_body(
            b'{"note":"prefix fixture-secret suffix","other":"fixture-\\u0073ecret"}',
            ("fixture-secret",))
        self.assertTrue(redacted)
        self.assertNotIn("fixture-secret", body)
        self.assertEqual(json.loads(body)["other"], "[REDACTED]")

    def test_malformed_secret_value_hides_remainder_but_plain_malformed_json_is_retained(self):
        raw = b'{ "external_task_id":null,"access_token":"unfinished secret'
        body, redacted, _ = redact_body(raw)
        self.assertTrue(redacted)
        self.assertNotIn("unfinished secret", body)
        self.assertIn('"external_task_id":null', body)
        self.assertEqual(redact_body(b'{not_json'), ('{not_json', False, True))

    def test_private_file_contains_body_and_only_selected_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            logger = RequestBodyLog(Path(temporary) / "request-bodies", secrets=("fixture-secret",))
            path = logger.save("fixture_task", b'{"drama_ids":["d"]}',
                               content_type="application/json", client_ip="192.0.2.1")
            record = json.loads(path.read_text())
            self.assertEqual(record["task_id"], "fixture_task")
            self.assertEqual(record["raw_body"], '{"drama_ids":["d"]}')
            self.assertEqual(record["body_bytes"], len(record["raw_body"].encode()))
            self.assertEqual(record["client_ip"], "192.0.2.1")
            self.assertNotIn("authorization", record)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(logger.root.stat().st_mode), 0o700)

    def test_invalid_utf8_is_explicitly_marked(self):
        body, redacted, utf8_valid = redact_body(b'{"bad":"\xff"}')
        self.assertFalse(utf8_valid)
        self.assertFalse(redacted)
        self.assertIn("\ufffd", body)


if __name__ == "__main__":
    unittest.main()

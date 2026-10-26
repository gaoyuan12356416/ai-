import sys
from pathlib import Path
import unittest

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup_graph import PageGraph


class Budget:
    def wait(self): pass
    def cool(self, seconds): pass


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def json(self):
        if isinstance(self.body, Exception): raise self.body
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception): raise value
        return value
    def close(self): pass


class GraphTests(unittest.TestCase):
    def graph(self, *responses):
        session = Session(*responses)
        return PageGraph("123", "secret-token", {"credential_id": "9"}, Budget(), session=session), session

    def test_timeout_unknown_and_no_retry_or_token_in_url(self):
        graph, session = self.graph(requests.ReadTimeout("secret-token"))
        self.assertEqual(graph.delete("123_456")["kind"], "unknown")
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("secret-token", session.calls[0][1])
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_error_5xx_is_unknown_even_with_json_error(self):
        graph, _ = self.graph(Response({"error": {"code": 2}}, 503))
        self.assertEqual(graph.delete("123_456")["kind"], "unknown")

    def test_permission_failure_never_deleted(self):
        graph, _ = self.graph(Response({"error": {"code": 200, "message": "bad secret-token"}}, 403))
        result = graph.delete("123_456")
        self.assertEqual(result["kind"], "failure")
        self.assertNotIn("secret-token", str(result))

    def test_foreign_page_cannot_receive_delete(self):
        graph, session = self.graph()
        with self.assertRaises(ValueError): graph.delete("999_456")
        self.assertFalse(session.calls)

    def test_raw_video_linkage_conflict_cannot_fallback(self):
        graph, session = self.graph(Response({"id": "88", "post_id": "999_456"}))
        result = graph.resolve("88")
        self.assertFalse(result["ok"])
        self.assertEqual(len(session.calls), 1)

    def test_video_response_identity_must_match(self):
        graph, session = self.graph(Response({"id": "99", "post_id": "123_456"}))
        self.assertEqual(graph.resolve("88")["error"], "video_identity_conflict")
        self.assertEqual(len(session.calls), 1)

    def test_known_video_cannot_use_synthetic_suffix(self):
        graph, session = self.graph(Response({"id": "88"}))
        self.assertFalse(graph.resolve("88", allow_post_suffix=False)["ok"])
        self.assertEqual(len(session.calls), 1)

    def test_untyped_suffix_requires_graph_owner_and_exact_identity(self):
        graph, session = self.graph(Response({"error": {"code": 100}}, 400),
                                    Response({"id": "123_456", "from": {"id": "123"},
                                              "created_time": "2026-10-01T00:00:00+0000"}))
        self.assertEqual(graph.resolve("456")["id"], "123_456")
        self.assertEqual(len(session.calls), 2)

    def test_unavailable_requires_live_same_page_identity(self):
        graph, _ = self.graph(Response({"error": {"code": 100, "error_subcode": 33}}, 400),
                              Response({"id": "999"}))
        self.assertFalse(graph.inspect("123_456")["absent"])

    def test_success_is_only_explicit_ack(self):
        for body in ({"success": True}, True):
            graph, _ = self.graph(Response(body))
            self.assertEqual(graph.delete("123_456")["kind"], "success")
        graph, _ = self.graph(Response({"success": False}))
        self.assertEqual(graph.delete("123_456")["kind"], "unknown")


if __name__ == "__main__": unittest.main()

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

    def test_unavailable_without_predelete_evidence_never_proves_absence(self):
        graph, _ = self.graph(Response({"error": {"code": 100, "error_subcode": 33}}, 400),
                              Response({"id": "999"}))
        self.assertFalse(graph.inspect("123_456").get("absent"))

    def test_target_still_in_list_cannot_validate_canary_receipt(self):
        graph, _ = self.graph(Response({"error": {"code": 10}}, 400), Response({"id": "123"}),
                              Response({"data": [{"id": "123_456", "created_time": "2026-08-02T23:25:30+0000"}]}))
        graph.verified_times["123_456"] = "2026-08-02T23:25:30+0000"
        graph.publication_membership["123_456"] = {"complete": True, "present": True}
        graph.acknowledged_posts.add("123_456")
        result = graph.inspect("123_456")
        self.assertFalse(result["absent"])
        self.assertFalse(result["receipt_validated"])

    def test_success_is_only_explicit_ack(self):
        for body in ({"success": True}, True):
            graph, _ = self.graph(Response(body))
            self.assertEqual(graph.delete("123_456")["kind"], "success")
        graph, _ = self.graph(Response({"success": False}))
        self.assertEqual(graph.delete("123_456")["kind"], "unknown")

    def test_mixed_permission_error_needs_complete_publication_listing(self):
        graph, session = self.graph(Response({"error": {"code": 10}}, 400),
                                    Response({"id": "123"}), Response({"data": []}))
        graph.verified_times["123_456"] = "2026-08-02T23:25:30+0000"
        graph.publication_membership["123_456"] = {"complete": True, "present": True}
        result = graph.inspect("123_456")
        self.assertTrue(result["absent"])
        self.assertTrue(result["published_window"]["complete"])
        self.assertEqual(session.calls[-1][1], "https://graph.facebook.com/v25.0/123/posts")
        self.assertIn("since", session.calls[-1][2]["params"])

    def test_unreadable_publication_list_is_not_absence(self):
        graph, _ = self.graph(Response({"error": {"code": 10}}, 400),
                              Response({"id": "123"}), Response({"error": {"code": 10}}, 400))
        graph.verified_times["123_456"] = "2026-08-02T23:25:30+0000"
        graph.publication_membership["123_456"] = {"complete": True, "present": True}
        self.assertFalse(graph.inspect("123_456")["absent"])

    def test_no_predelete_membership_keeps_readback_unconfirmed_despite_ack(self):
        graph, session = self.graph(Response({"error": {"code": 10}}, 400), Response({"id": "123"}))
        graph.verified_times["123_456"] = "2026-08-02T23:25:30+0000"
        graph.acknowledged_posts.add("123_456")
        result = graph.inspect("123_456")
        self.assertFalse(result["absent"])
        self.assertTrue(result["receipt_validated"])
        self.assertEqual(len(session.calls), 2)

    def test_permission_error_without_ack_is_never_receipt_validation(self):
        graph, _ = self.graph(Response({"error": {"code": 10}}, 400), Response({"id": "123"}))
        graph.verified_times["123_456"] = "2026-08-02T23:25:30+0000"
        result = graph.inspect("123_456")
        self.assertFalse(result["absent"])
        self.assertFalse(result["receipt_validated"])

    def test_listing_requires_complete_pagination_and_preserves_present_target(self):
        graph, session = self.graph(Response({"data": [], "paging": {"next": "SECRET_URL", "cursors": {"after": "cursor1"}}}),
                                    Response({"data": [{"id": "123_456", "created_time": "2026-08-02T23:25:30+0000"}]}))
        result = graph.window_presence("123_456", "2026-08-02T23:25:30+0000")
        self.assertTrue(result["complete"])
        self.assertTrue(result["present"])
        self.assertEqual(result["pages"], 2)
        self.assertNotIn("SECRET_URL", str(result))
        self.assertEqual(session.calls[1][2]["params"]["after"], "cursor1")

    def test_ignored_time_filter_is_not_absence(self):
        graph, _ = self.graph(Response({"data": [{"id": "123_99", "created_time": "2026-10-01T00:00:00+0000"}]}))
        self.assertFalse(graph.window_presence("123_456", "2026-08-02T23:25:30+0000")["complete"])


if __name__ == "__main__": unittest.main()

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup
from cleanup_source import PageCredential


class Source:
    def credentials(self, *args): return []
    def precheck(self, page, materials, source_row_ids=None):
        self.checked = (page, materials, source_row_ids)
        return {"ok": True}


def row(**kwargs):
    value = dict(page_id="123", material_id="7", source_row_id="9000",
                 raw_post_id="88", real_post_id="456", group_ids=[3, 24],
                 published_at_utc="2026-10-01T00:00:00+00:00", table="ads_facebook_post_log", log_id="1")
    value.update(kwargs)
    return value


class CliTests(unittest.TestCase):
    def test_cross_log_real_suffix_dedup_preserves_source_namespace(self):
        rows = [row(), row(raw_post_id="456", real_post_id="", log_id="2")]
        evidence, unresolved = cleanup.normalized_evidence({"candidates": rows}, Source(), "v25.0")
        self.assertFalse(unresolved)
        self.assertEqual({r["post_id"] for r in evidence}, {"123_456"})
        self.assertEqual(evidence[0]["source_row_id"], "9000")
        self.assertEqual(evidence[0]["material_id"], "7")

    def test_explicit_conflicting_post_ids_excluded(self):
        evidence, unresolved = cleanup.normalized_evidence(
            {"candidates": [row(raw_post_id="123_999", real_post_id="456")]}, Source(), "v25.0")
        self.assertFalse(evidence)
        self.assertEqual(unresolved[0]["reason"], "conflicting_explicit_post_identity")

    def test_full_post_id_in_raw_is_preserved_for_mandatory_inspection(self):
        evidence, unresolved = cleanup.normalized_evidence(
            {"candidates": [row(raw_post_id="123_456", real_post_id="")]}, Source(), "v25.0")
        self.assertFalse(unresolved)
        self.assertEqual(evidence[0]["post_id"], "123_456")

    def test_known_ai_video_is_not_treated_as_post_suffix(self):
        source = Source()
        source.credentials = lambda *args: [PageCredential("123", "9", "10", "secret")]
        with patch.object(cleanup, "PageGraph") as mock:
            graph = mock.return_value
            graph.identity.return_value = {"ok": True}
            graph.resolve.return_value = {"ok": False, "error": "unresolved"}
            evidence, unresolved = cleanup.normalized_evidence(
                {"candidates": [row(table="fb_auto_task", real_post_id="")]}, source, "v25.0")
            graph.resolve.assert_called_once_with("88", allow_post_suffix=False)
            self.assertFalse(evidence)
            self.assertEqual(len(unresolved), 1)

    def test_ai_source_row_arrays_revalidated(self):
        source = Source()
        adapter = cleanup.Adapter(source)
        target = {"page_id": "123", "material_ids": ["7"], "evidence": [
            {"source_row_id": "9000"}, {"source_row_ids": ["9001", "9000"]}]}
        bound = adapter.for_target(target)
        self.assertTrue(bound._source_check()["ok"])
        self.assertEqual(source.checked[2], ["9000", "9001"])

    def test_suffix_resolution_cache_cannot_bypass_ai_video_policy(self):
        source = Source()
        source.credentials = lambda *args: [PageCredential("123", "9", "10", "secret")]
        with patch.object(cleanup, "PageGraph") as mock:
            graph = mock.return_value
            graph.identity.return_value = {"ok": True}
            graph.resolve.side_effect = [{"ok": True, "id": "123_88"}, {"ok": False}]
            evidence, unresolved = cleanup.normalized_evidence({"candidates": [
                row(real_post_id=""), row(table="fb_auto_task", real_post_id="")
            ]}, source, "v25.0")
            self.assertEqual(graph.resolve.call_count, 2)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(len(unresolved), 1)

    def test_credential_context_contains_no_secret(self):
        credential = PageCredential("123", "9", "10", "secret")
        self.assertEqual(cleanup.safe_credential(credential)["credential_id"], "9")
        self.assertNotIn("secret", str(cleanup.safe_credential(credential)))


if __name__ == "__main__": unittest.main()

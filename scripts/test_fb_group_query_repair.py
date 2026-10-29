"""Failure classification, byte-exact metadata and bounded read retry regressions."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_group_sources import Catalog, resource
from test_fb_video_publish_broker import BrokerBoundaryTests, Handler
from features.fb_video_publish.templates import TemplateError
from features.fb_video_publish import runtime_source


class QueryRepairTests(unittest.TestCase):
    def test_transient_read_retries_once_but_invalid_queries_do_not(self):
        for code, expected in (("source_query_timeout", 2), ("source_connection_failed", 2),
                               ("source_query_rejected", 1), ("source_readonly_check_failed", 1)):
            db = Catalog()
            db.select = Mock(side_effect=TemplateError(code, "secret SQL", 503))
            with self.assertRaises(TemplateError) as raised:
                db.source().resolve_materials(["1"])
            self.assertEqual(expected, db.select.call_count)
            self.assertNotIn("secret", str(raised.exception))

    def test_transient_read_recovers_without_changing_parameters(self):
        db = Catalog()
        original = db.select
        db.select = Mock(side_effect=[TemplateError("source_busy", "busy", 503), []])
        self.assertEqual([], db.source()._select("SELECT fixture", ["1"], maximum=1))
        self.assertEqual(db.select.call_args_list[0], db.select.call_args_list[1])

    def test_metadata_preserves_case_accent_and_space_conflicts(self):
        db = Catalog()
        db.resources = [resource(), resource(drama_name="drama 1")]
        result = db.source().resolve_materials(["1"])
        self.assertEqual("drama_mapping_ambiguous", result["errors"][0]["code"])
        sql = next(sql for sql, _ in db.calls if "ads_drama_resource" in sql)
        self.assertEqual(6, sql.count("BINARY "))
        self.assertIn("DISTINCT BINARY", sql)
        self.assertNotIn("ORDER BY r.id", sql)

    def test_mysql_errno_classified_without_stderr_disclosure(self):
        db = runtime_source.GatedReadOnlyMySQL({"FB_AUTO_MYSQL_USER": "fixture", "FB_AUTO_MYSQL_PASSWORD": "fixture"})
        for errno, code in ((3024, "source_query_timeout"), (2013, "source_connection_failed"), (1064, "source_query_failed")):
            process = Mock(returncode=1)
            process.communicate.return_value = (b"", ("ERROR %d private SQL and secret" % errno).encode())
            with patch.object(runtime_source.subprocess, "Popen", return_value=process):
                with self.assertRaises(TemplateError) as raised:
                    db.select("SELECT id FROM `kunlunads_dev`.ads_custom_source", [])
            self.assertEqual(code, raised.exception.code)
            self.assertNotIn("private", str(raised.exception))

    def test_broker_reports_database_timeout_as_503_not_bad_request(self):
        handler = Handler()
        BrokerBoundaryTests().dispatch(handler, side_effect=TemplateError("source_query_timeout", "private SQL", 503))
        self.assertEqual(503, handler.responses[0][0])
        self.assertEqual("source_query_timeout", handler.responses[0][1]["code"])
        self.assertNotIn("private", str(handler.responses))


if __name__ == "__main__":
    unittest.main()

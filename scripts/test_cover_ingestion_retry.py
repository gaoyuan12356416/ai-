"""Offline transport, stage-classification and duplicate-submission regressions."""
import ast
import hashlib
import io
import logging
from pathlib import Path
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features import cover_material_source as material
from test_cover_material_source import FakeResponse, ImageTests, LedgerTests, source

URL = "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test.png"


class RetryTests(ImageTests):
    def setUp(self):
        super().setUp()
        self.sleep = mock.patch.object(material.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_timeout_then_success(self):
        session = mock.Mock()
        session.get.side_effect = [requests.ReadTimeout("test"), FakeResponse(raw=self.png())]
        self.assertEqual(material.inspect_image(URL, session=session)["width"], 100)
        self.assertEqual(session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(session.get.call_args.kwargs["timeout"], (10, 40))

    def test_retry_exhaustion_is_bounded_and_sanitized(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("SECRET-not-for-logs")
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaisesRegex(material.ImageFetchError, "attempts=3") as error:
                material.inspect_image(URL, session=session)
        self.assertNotIn("SECRET", str(error.exception) + str(logs.output))
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(3)])

    def test_transient_http_then_success(self):
        for status in material.IMAGE_RETRY_HTTP:
            session = mock.Mock()
            session.get.side_effect = [FakeResponse(status=status), FakeResponse(raw=self.png())]
            self.assertEqual(material.inspect_image(URL, session=session)["width"], 100)
            self.assertEqual(session.get.call_count, 2)

    def test_terminal_errors_do_not_retry(self):
        for status in (301, 400, 401, 403, 404):
            session = self.session(b"", status)
            with self.assertRaises(ValueError):
                material.inspect_image(URL, session=session)
            self.assertEqual(session.get.call_count, 1)
        session = mock.Mock()
        session.get.side_effect = requests.exceptions.SSLError("certificate failed")
        with self.assertRaises(requests.exceptions.SSLError):
            material.inspect_image(URL, session=session)
        self.assertEqual(session.get.call_count, 1)

    def test_partial_stream_is_discarded_before_retry(self):
        class Partial(FakeResponse):
            def iter_content(self, size):
                yield b"partial-corrupt-prefix"
                raise requests.exceptions.ChunkedEncodingError("stream interrupted")
        raw = self.png()
        session = mock.Mock()
        session.get.side_effect = [Partial(), FakeResponse(raw=raw)]
        result = material.inspect_image(URL, session=session)
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(session.get.call_count, 2)

    def test_integrity_failure_is_not_a_transport_retry(self):
        for sha, ratio, raw in [("b" * 64, "1:1", self.png()), ("", "4:5", self.png()),
                                ("", "", b"not-an-image")]:
            session = self.session(raw)
            with self.assertRaises((ValueError, OSError)):
                material.inspect_image(URL, sha, ratio, session)
            self.assertEqual(session.get.call_count, 1)


class NoReplayTests(LedgerTests):
    def test_image_preflight_failure_can_resume_without_reposting_confirmed(self):
        first = self.send()
        self.catalog.rows[first["key"]] = [self.formal_row(first)]
        self.assertEqual(self.send()["state"], "confirmed")
        self.inspect.side_effect = material.ImageFetchError("image_fetch_retry_exhausted")
        other = source(source_key="screenshot:job-123:landscape_1_91x1")
        with self.assertRaises(material.ImageFetchError):
            self.send(other)
        self.assertEqual(self.ledger.get(material.identity(other))["attempts"], 0)
        self.assertEqual(self.client.post.call_count, 1)
        # A confirmed row returns before GET and POST, even while COS is failing.
        self.assertEqual(self.send()["state"], "confirmed")
        self.assertEqual(self.client.post.call_count, 1)
        self.inspect.side_effect = None
        self.send(other)
        self.assertEqual(self.client.post.call_count, 2)
        self.assertEqual(self.ledger.get(material.identity(other))["attempts"], 1)

    @staticmethod
    def formal_row(row):
        import json
        from test_cover_material_source import formal
        return formal(json.loads(row["request_json"]))


class MessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        names = {"notify_screenshot_ai_source_callback", "screenshot_failed_size_labels",
                 "humanize_screenshot_failure_reason", "build_screenshot_failure_message"}
        subset = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        cls.ns = dict(logging=logging, append_audit_log=mock.Mock(),
                      is_screenshot_generation_no_output_error=lambda e: "missing raw_generated_path" in str(e),
                      is_screenshot_source_consistency_rejection=lambda e: False)
        exec(compile(subset, "app-functions", "exec"), cls.ns)

    def test_callback_tags_stage_and_keeps_original_cause(self):
        error = requests.ReadTimeout("COS timed out")
        self.ns["post_screenshot_ai_source_callback"] = mock.Mock(side_effect=error)
        with mock.patch.object(logging, "exception"):
            with self.assertRaises(material.ScreenshotMaterialIngestionError) as caught:
                self.ns["notify_screenshot_ai_source_callback"]({}, raise_on_error=True)
        self.assertIs(caught.exception.__cause__, error)

    def test_ingestion_failure_does_not_claim_generation_timeout(self):
        text = self.ns["build_screenshot_failure_message"]({},
                  str(material.ScreenshotMaterialIngestionError("image_fetch_retry_exhausted")))
        self.assertIn("素材入库失败", text)
        self.assertIn("3 次读取", text)
        self.assertNotIn("图片生成进程超时", text)

    def test_generation_error_is_still_generation_error_with_existing_images(self):
        job = {size + "_url": URL for size, _ in material.SPECS}
        for error in ("timed out", "missing raw_generated_path"):
            text = self.ns["build_screenshot_failure_message"](job, error)
            self.assertIn("封面图片生成失败", text)
            self.assertNotIn("素材入库失败", text)

    def test_historical_callback_timeout_and_unknown_receipt(self):
        job = {size + "_url": URL for size, _ in material.SPECS}
        job.update(progress=96, progress_detail="三种截图素材已生成，正在回传素材接口")
        text = self.ns["build_screenshot_failure_message"](job, URL + " Read timed out")
        self.assertIn("读取 COS 图片", text)
        self.assertIn("素材入库失败", text)
        text = self.ns["build_screenshot_failure_message"]({},
                     "material_ingestion_failed: material_source_pending:unknown:key")
        self.assertIn("先对账", text)


if __name__ == "__main__":
    unittest.main()

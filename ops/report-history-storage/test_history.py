import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import history


class ImportTests(unittest.TestCase):
    def test_round_trip_order_missing_values_numeric_metrics_and_metadata(self):
        rows = [
            {"date": "2026-11-01", "account": "001", "campaign": 3, "spend": 2.0, "revenue": "0.12"},
            {
                "date": "2026-11-02",
                "account": "002",
                "campaign": 5,
                "spend": 0,
                "revenue": None,
                "clicks": 1,
            },
        ]
        document = {"rows": rows, "start": "2026-11-01", "totals": {"spend": 2}}
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "day-2026-11-01.json"
            p.write_text(json.dumps(document))
            parsed, kind, meta, source_hash, st = history.probe_rows(p, "dw_daily")
        cols = sorted(set().union(*parsed))
        pages, digest = history.pack_pages(parsed, "dw_daily", cols)
        page = pages[0]
        self.assertEqual(
            (page[1:4], float(page[4]), float(page[5])), ((2, "2026-11-01", "2026-11-02"), 2, 0.12)
        )
        header = (
            "dw_daily",
            str(p),
            source_hash,
            st.st_size,
            kind,
            json.dumps(cols),
            json.dumps(meta),
            json.dumps([page[0]]),
            "verified",
            2,
            1,
            digest,
        )
        stored = (page[0], "dw_daily", *page[1:])

        class Cursor:
            def cursor(self, **kwargs):
                return contextlib.nullcontext(self)

        dst = io.StringIO()
        with patch.object(
            history, "connection", return_value=contextlib.nullcontext(Cursor())
        ), patch.object(history, "verified_view", return_value=header), patch.object(
            history, "fetch_pages", return_value=[stored]
        ):
            history.verify_block(None, pages, "dw_daily", cols)
            history.read_version("a" * 64, dst)
        self.assertEqual(json.loads(dst.getvalue()), document)

    def test_duplicate_receipts_keep_every_line_but_only_safe_fields(self):
        raw = b'{"ad_id":"123","final_status":"ok","response":{"access_token":"test"}}\n'
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "receipt.jsonl"
            p.write_bytes(raw + raw)
            rows, kind, _, digest, _ = history.probe_rows(p, "fb_receipt_index")
        self.assertEqual(kind, "jsonl-index")
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"ad_id", "final_status", "source_line_sha256"})
        self.assertEqual(rows[0]["source_line_sha256"], history.sha(raw.rstrip()))
        self.assertEqual(digest, history.sha(raw + raw))

    def test_unsupported_and_sensitive_values_are_retained(self):
        self.assertIsNone(history.match("../analysis/hotdrama-review-1/perf.json"))
        self.assertIsNone(history.match("analysis/hotdrama-review-1/arbitrary.json"))
        self.assertEqual(history.match("analysis/hotdrama-review-1/perf.json"), "hotdrama_report")
        for value in (
            {"access_token": "x"},
            "Authorization: bearer private",
            "https://example.test/file?t=secret",
        ):
            with self.assertRaisesRegex(ValueError, "unreviewed_business_value"):
                history.pack_pages([{"ad": value}], "hotdrama_report", ["ad"])
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "input.json"
            p.write_text('[{"extra_field":7}]')
            with self.assertRaisesRegex(ValueError, "unreviewed_row_fields"):
                history.probe_rows(p, "hotdrama_report")


if __name__ == "__main__":
    unittest.main()

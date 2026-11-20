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

    def test_checkpoint_preserves_query_times_and_source_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "read-checkpoints"
            folder.mkdir()
            path = folder / "day-2026-10-09.json"
            total = dict(date="2026-10-09", n=17, zero_rows_omitted=9, **dict.fromkeys(history.METRICS, 0))
            meta = dict(query_time="2026-10-16 18:26:17", completed_at="2026-10-16 18:26:52", isolation="REPEATABLE-READ", total=total)
            path.write_text(json.dumps(dict(meta, rows=[])))
            self.assertEqual(history.probe_rows(path, "dw_daily")[2], meta)
            total["unreviewed"] = "extra"
            path.write_text(json.dumps(dict(meta, rows=[])))
            with self.assertRaisesRegex(ValueError, "unreviewed_metadata_fields"):
                history.probe_rows(path, "dw_daily")

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

    def test_positional_rows_require_the_original_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.json"
            path.write_text('[["4","5","6","2026-01-01","7"]]')
            with self.assertRaisesRegex(ValueError, "positional_columns_unconfirmed"):
                history.probe_rows(path, "hotdrama_report")
            path.with_name("query_once.py").write_text(
                """jobs=[('logs',f"SELECT queue_id,manual_id,cross_queue_ids,created_at,app_id FROM original_log")]"""
            )
            rows, kind, meta, _, _ = history.probe_rows(path, "hotdrama_report")
            self.assertEqual(kind, "positional")
            self.assertEqual(
                [rows[0][k] for k in meta["source_column_order"]], json.loads(path.read_text())[0]
            )
        digest = "eaa" + "0" * 61
        history.pack_pages([{"source_line_sha256": digest}], "fb_receipt_index", ["source_line_sha256"])

    def test_reviewed_unlabelled_cells_roundtrip_by_position_without_inferred_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rel = "analysis/hotdrama-optimizer-20261021-22/logs.json"
            path = root / rel
            path.parent.mkdir(parents=True)
            document = [["075", "4", None, "2026-10-21", "9"], ["075", "5", "", "2026-10-22", "9"]]
            path.write_text(json.dumps(document))
            manifest = root / "review.json"
            manifest.write_text(json.dumps({rel: history.sha(path.read_bytes())}))
            with patch.object(history, "BASE", root), patch.object(history, "REVIEWED_ORDINAL_SOURCES", manifest):
                rows, kind, meta, _, _ = history.probe_rows(path, "hotdrama_report")
                self.assertEqual((kind, meta["business_column_mapping"]), ("positional", "unavailable"))
                order = meta["source_column_order"]
                self.assertEqual([[r[k] for k in order] for r in rows], document)
                pages, _ = history.pack_pages(rows, "hotdrama_report", order)
                self.assertEqual(pages[0][2:9], (None,) * 7)
                path.write_text(json.dumps(document[:1]))
                with self.assertRaisesRegex(ValueError, "positional_columns_unconfirmed"):
                    history.probe_rows(path, "hotdrama_report")


if __name__ == "__main__":
    unittest.main()

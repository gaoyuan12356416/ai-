import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from features.fb_auto_posts.core import StoreError
from features.fb_auto_posts.metric_mysql import (MetricBrokerClient, MySQLMetricStore, amount_text,
    canonical_row, digest_row, exact_add, exact_amount, metric_store_from_env, window_from_rows)
from scripts.fb_auto_metric_broker import validate_window
from scripts.migrate_fb_metrics_ads_ai import json_metric_refs, integer_ids


class FakeCursor:
    lastrowid = 7
    def __init__(self, owner): self.owner = owner
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=()): self.owner.calls.append((sql, params))
    def executemany(self, sql, rows):
        self.owner.calls.append((sql, rows))
        self.owner.rows.extend(dict(zip(("generation_id", "content_id", "material_id", "spend", "af_revenue0"), row)) for row in rows)


class FakeWriter:
    def __init__(self): self.calls, self.rows = [], []
    def cursor(self): return FakeCursor(self)
    def commit(self): self.calls.append(("COMMIT", ()))
    def close(self): pass


class FakeReader:
    def __init__(self, writer, corrupt=False): self.writer, self.corrupt = writer, corrupt
    def generation(self, **kwargs): return None
    def count(self, ident): return len(self.writer.rows)
    def rows(self, ident):
        return [{**row, **({"spend": "999"} if self.corrupt else {})} for row in self.writer.rows]


class MetricTests(unittest.TestCase):
    def test_exact_decimal_and_no_rounding(self):
        self.assertEqual(amount_text("0.000000000001"), "0.000000000001")
        self.assertEqual(amount_text("12.300000"), "12.3")
        self.assertEqual(exact_add(Decimal("9999999999999999999999999"), Decimal("0.000000000001")), Decimal("9999999999999999999999999.000000000001"))
        for value in ("0.0000000000001", "1e26", "-1", "NaN"):
            with self.assertRaises(Exception): exact_amount(value)

    def test_window_preserves_case_ids_and_money(self):
        rows = [{"kind": "generation", "day": "2026-11-12", "id": 4728, "status": "ready"},
                {"kind": "row", "content_id": "ABC", "material_id": "1", "spend": "0.1", "af_revenue0": "0.2"},
                {"kind": "row", "content_id": "abc", "material_id": "1", "spend": "0.2", "af_revenue0": "0.1"}]
        window = window_from_rows("Dramawave", 0, ["2026-11-12"], rows)
        self.assertEqual(window.generation_ids, (4728,))
        self.assertEqual(window.by_drama["ABC"].spend, Decimal("0.1"))
        self.assertEqual(len(window.by_material), 2)
        with self.assertRaises(StoreError) as raised:
            window_from_rows("Dramawave", 0, ["2026-11-12", "2026-11-11"], rows)
        self.assertEqual(raised.exception.code, "fb_auto_metric_window_not_ready")
        self.assertEqual(raised.exception.conflicts, [{"missing_dates": ["2026-11-11"]}])

    def test_fixed_broker_contract(self):
        valid = {"product": "Dramawave", "platform": 0, "dates": ["2026-11-12"]}
        self.assertEqual(validate_window(valid)[2], ("2026-11-12",))
        for value in ({**valid, "sql": "SELECT 1"}, {**valid, "platform": True}, {**valid, "dates": []}, {**valid, "dates": ["2026-11-12"] * 31}):
            with self.assertRaises(StoreError): validate_window(value)

    def test_recursive_reference_collection(self):
        value = {"task_id": 999, "frozen": {"metric_generation_ids": [2, 3]},
                 "selection": [{"metric_generations": {"en": [4, 5]}}]}
        self.assertEqual(json_metric_refs(value), {2, 3, 4, 5})
        self.assertEqual(integer_ids({"en": [1, 2]}), {1, 2})

    def test_writer_publishes_only_after_exact_readback(self):
        writer = FakeWriter(); store = MySQLMetricStore({}, reader=FakeReader(writer), writer_factory=lambda: writer)
        result = store.record_metric_generation_streaming(platform=0, product="Dramawave", metric_date="2026-11-12",
                   refreshed_at_utc="2026-11-13T00:00:00+00:00", rows=[{"content_id": "A", "material_id": "1", "spend": "1.2300", "af_revenue0": "4.5600"}])
        self.assertEqual(result["id"], 7)
        self.assertEqual(result["spend"], "1.23")
        self.assertTrue(any("active_pointer" in sql for sql, _ in writer.calls))
        writer = FakeWriter(); store = MySQLMetricStore({}, reader=FakeReader(writer, corrupt=True), writer_factory=lambda: writer)
        with self.assertRaises(StoreError):
            store.record_metric_generation_streaming(platform=0, product="Dramawave", metric_date="2026-11-12",
                 refreshed_at_utc="2026-11-13T00:00:00+00:00", rows=[{"content_id": "A", "material_id": "1", "spend": "1.23", "af_revenue0": "4.56"}])
        self.assertFalse(any("active_pointer" in sql for sql, _ in writer.calls))

    def test_mysql_runtime_never_opens_sqlite_or_secret_config(self):
        with patch("features.fb_auto_posts.metric_mysql.config_from_file", side_effect=AssertionError("must stay privileged")):
            store = metric_store_from_env({"FB_AUTO_METRIC_STORAGE": "mysql", "FB_AUTO_POST_INTERNAL_TOKEN": "x" * 40})
            self.assertIsInstance(store, MetricBrokerClient)


if __name__ == "__main__": unittest.main()

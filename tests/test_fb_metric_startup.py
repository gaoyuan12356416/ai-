"""Existing metric sources remain readable while a migration holds a read txn.

Run twice, with FB_TEST_OVERLAY=1 for the independently deployed FB runtime.
No database, broker, GPU or Graph network connection is permitted in these tests.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import features
if os.environ.get("FB_TEST_OVERLAY") == "1":
    features.__path__.insert(0, str(ROOT / "ops/moboreels-attribution-20261113/runtime/fb/features"))

from features.fb_auto_posts.core import FBAutoPostStore, StoreError
from features.fb_auto_posts import service


class FastBusyStore(FBAutoPostStore):
    def connect(self):
        conn = super().connect()
        conn.execute("PRAGMA busy_timeout=50")
        return conn


class MetricStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.metric = self.folder / "metric.sqlite3"
        self.operational = self.folder / "operational.sqlite3"
        self.env = {
            "FB_AUTO_POST_INTERNAL_TOKEN": "test-internal-" + "a" * 32,
            "FB_AUTO_GPU_PREPARE_INTERNAL_TOKEN": "test-gpu-" + "b" * 32,
            "FB_AUTO_POST_DB_PATH": str(self.operational),
            "FB_AUTO_METRIC_DB_PATH": str(self.metric),
            "FB_AUTO_POST_SHORT_LINK_ROOT": str(self.folder / "short-links"),
            "FB_VIDEO_SHORT_LINK_ROOT": str(self.folder / "manual-links"),
        }
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("Network forbidden")).start()
        patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")).start()

    def seed_metric_only(self):
        # Deliberately no task, policy, schedule, effects, or Moboreels columns.
        with closing(sqlite3.connect(self.metric)) as conn, conn:
            conn.executescript("""
            CREATE TABLE fb_auto_metric_generation (
                id INTEGER PRIMARY KEY, generation_key TEXT, platform INTEGER,
                metric_date TEXT, product TEXT, status TEXT, row_count INTEGER,
                checksum TEXT, refreshed_at_utc TEXT, created_at_utc TEXT,
                ready_at_utc TEXT, error_code TEXT, error_message TEXT);
            CREATE TABLE fb_auto_metric_daily (
                generation_id INTEGER,content_id TEXT,material_id TEXT,
                spend TEXT,af_revenue0 TEXT);
            CREATE TABLE fb_auto_metric_active_pointer (
                platform INTEGER,metric_date TEXT,product TEXT,generation_id INTEGER,
                activated_at_utc TEXT);
            INSERT INTO fb_auto_metric_generation VALUES
                (1,'test-generation',0,'2026-11-12','Dramawave','ready',1,'test',
                 '2026-11-13T00:00:00Z','2026-11-13T00:00:00Z','2026-11-13T00:00:00Z','','');
            INSERT INTO fb_auto_metric_daily VALUES (1,'42','100','12.50','31.25');
            INSERT INTO fb_auto_metric_active_pointer VALUES
                (0,'2026-11-12','Dramawave',1,'2026-11-13T00:00:00Z');
            """)

    def hold_reader(self):
        conn = sqlite3.connect(self.metric)
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM fb_auto_metric_daily").fetchall()
        self.addCleanup(conn.close)
        return conn

    def test_build_and_query_with_long_read_transaction_preserves_metric_file(self):
        self.seed_metric_only()
        before = hashlib.sha256(self.metric.read_bytes()).hexdigest()
        reader = self.hold_reader()
        runtime = service.build_runtime(self.env)
        window = runtime.metric_store.load_metric_window(
            product="Dramawave", platform=0, dates=["2026-11-12"])
        self.assertEqual(window.by_drama["42"].revenue, Decimal("31.25"))
        self.assertEqual(window.by_drama["42"].spend, Decimal("12.50"))
        self.assertTrue(reader.in_transaction)
        self.assertEqual(hashlib.sha256(self.metric.read_bytes()).hexdigest(), before)
        with runtime.store.connect() as conn:
            self.assertIn("moboreels_json", {row[1] for row in conn.execute("PRAGMA table_info(fb_auto_task)")})
        with runtime.metric_store.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='fb_auto_task'").fetchone())

    def test_original_initialization_would_lock_existing_reader(self):
        # Start from the original complete schema, matching the deployed database.
        FBAutoPostStore(self.metric)
        self.hold_reader()
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            FastBusyStore(self.metric)
        FBAutoPostStore(self.metric, initialize_storage=False)

    def test_existing_missing_table_refuses_startup(self):
        self.seed_metric_only()
        with closing(sqlite3.connect(self.metric)) as conn, conn:
            conn.execute("DROP TABLE fb_auto_metric_active_pointer")
        before = self.metric.read_bytes()
        with self.assertRaises(StoreError) as raised:
            service.build_runtime(self.env)
        self.assertEqual(raised.exception.code, "fb_auto_metric_schema_invalid")
        self.assertEqual(self.metric.read_bytes(), before)

    def test_existing_missing_column_refuses_startup(self):
        self.seed_metric_only()
        with closing(sqlite3.connect(self.metric)) as conn, conn:
            conn.execute("ALTER TABLE fb_auto_metric_daily RENAME COLUMN af_revenue0 TO wrong_revenue")
        with self.assertRaises(StoreError) as raised:
            FBAutoPostStore(self.metric, initialize_storage=False)
        self.assertEqual(raised.exception.code, "fb_auto_metric_schema_invalid")

    def test_corrupt_source_refuses_startup(self):
        self.metric.write_bytes(b"not a sqlite database")
        with self.assertRaises(StoreError) as raised:
            service.build_runtime(self.env)
        self.assertEqual(raised.exception.code, "fb_auto_metric_schema_invalid")
        self.assertEqual(self.metric.read_bytes(), b"not a sqlite database")

    def test_new_metric_database_still_initializes_and_missing_days_fail(self):
        runtime = service.build_runtime(self.env)
        self.assertTrue(self.metric.is_file())
        runtime.metric_store.validate_metric_storage()
        with self.assertRaises(StoreError) as raised:
            runtime.metric_store.load_metric_window(product="Dramawave", platform=0, dates=["2026-11-12"])
        self.assertEqual(raised.exception.code, "fb_auto_metric_window_not_ready")

    def test_read_only_constructor_does_not_create_missing_database(self):
        with self.assertRaises(StoreError):
            FBAutoPostStore(self.metric, initialize_storage=False)
        self.assertFalse(self.metric.exists())


if __name__ == "__main__":
    unittest.main()

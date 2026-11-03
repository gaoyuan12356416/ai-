"""Repeated query-side stores must not rerun migration write transactions."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_posts import service as posts


class XStorageInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "posts.sqlite3"

    def test_concurrent_queries_initialize_one_database_once(self):
        barrier = threading.Barrier(8)
        def query(_):
            barrier.wait(timeout=5)
            store = posts.XPostStore(self.db)
            for _ in range(3):
                store = posts.XPostStore(self.db)
                self.assertEqual("material", store.get_schedule_config("material")["source_type"])
        with mock.patch.object(posts, "ensure_storage", wraps=posts.ensure_storage) as migrate:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(query, range(8)))
            self.assertEqual(1, migrate.call_count)

    def test_existing_read_transaction_does_not_block_query_store(self):
        posts.XPostStore(self.db)
        original = posts._connect
        def short_wait(path):
            conn = original(path)
            conn.execute("PRAGMA busy_timeout=30")
            return conn
        with contextlib.closing(sqlite3.connect(self.db)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM x_post_queue").fetchone()
            # A constructor migration takes a write lock and its commit would
            # fail behind this legitimate reader. An ordinary query need not.
            with mock.patch.object(posts, "_connect", side_effect=short_wait):
                store = posts.XPostStore(self.db)
                self.assertEqual("material", store.get_schedule_config("material")["source_type"])
            reader.rollback()

    def test_failed_first_initialization_can_retry(self):
        original = posts.ensure_storage
        with mock.patch.object(posts, "ensure_storage", side_effect=[RuntimeError("offline migration failure"), mock.DEFAULT], wraps=original) as migrate:
            with self.assertRaisesRegex(RuntimeError, "offline migration failure"):
                posts.XPostStore(self.db)
            posts.XPostStore(self.db)
            posts.XPostStore(self.db)
            self.assertEqual(2, migrate.call_count)

    def test_two_databases_have_independent_initialization(self):
        other = self.root / "other.sqlite3"
        with mock.patch.object(posts, "ensure_storage", wraps=posts.ensure_storage) as migrate:
            for _ in range(2):
                posts.XPostStore(self.db)
                posts.XPostStore(other)
            self.assertEqual(2, migrate.call_count)

    def test_initializing_one_database_does_not_hold_another(self):
        other = self.root / "other.sqlite3"
        entered, release = threading.Event(), threading.Event()
        original = posts.ensure_storage
        def migrate(path):
            if Path(path) == self.db:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("other database was blocked")
            return original(path)
        with mock.patch.object(posts, "ensure_storage", side_effect=migrate):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(posts.XPostStore, self.db)
                try:
                    self.assertTrue(entered.wait(3))
                    second = pool.submit(posts.XPostStore, other)
                    self.assertEqual(other, second.result(timeout=3).db_path)
                finally:
                    release.set()
                first.result(timeout=3)

    def test_replaced_database_is_initialized(self):
        posts.XPostStore(self.db)
        # Preserve the old inode so a new file cannot reuse it accidentally.
        self.db.rename(self.root / "previous.sqlite3")
        with mock.patch.object(posts, "ensure_storage", wraps=posts.ensure_storage) as migrate:
            posts.XPostStore(self.db)
            posts.XPostStore(self.db)
            self.assertEqual(1, migrate.call_count)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(0, conn.execute("SELECT count(*) FROM x_post_queue").fetchone()[0])

    def test_changed_schema_invalidates_cache_but_data_updates_do_not(self):
        posts.XPostStore(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DROP TRIGGER trg_x_post_pool_delete_guard")
            conn.commit()
        with mock.patch.object(posts, "ensure_storage", wraps=posts.ensure_storage) as migrate:
            posts.XPostStore(self.db)
            with contextlib.closing(sqlite3.connect(self.db)) as conn:
                self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_x_post_pool_delete_guard'").fetchone())
                conn.execute("UPDATE x_post_schedule_config SET enabled=enabled")
                conn.commit()
            posts.XPostStore(self.db)
            self.assertEqual(1, migrate.call_count)

    def test_explicit_migration_remains_available(self):
        posts.XPostStore(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute("PRAGMA schema_version").fetchone()[0]
        posts.ensure_storage(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertGreater(conn.execute("PRAGMA schema_version").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()

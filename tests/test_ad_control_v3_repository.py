import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.ad_control_v3.errors import AdControlV3Error
from features.ad_control_v3.repository import (
    MAX_PERSISTED_TARGETS,
    TARGET_INSERT_CHUNK_SIZE,
    MemoryRepository,
    MySQLRepository,
    TABLES,
    qualified_table,
)


class MemoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository(
            [{"channel": "facebook", "product_value": "Dramawave", "product_type": "short_drama", "enabled": True}]
        )
        base = {
            "group_id": "g1",
            "name": "one",
            "channel": "facebook",
            "object_level": "campaign",
            "run_mode": "observe",
            "owner_user_id": "u1",
            "optimizer_id": 248,
            "products": ["Dramawave"],
            "config_version": 1,
            "behavior_hash": "a" * 64,
            "last_preview_id": "",
            "last_preview_hash": "",
            "enabled": False,
            "emergency_stopped": False,
            "deleted": False,
            "updated_at": "2026-07-16 00:00:00",
        }
        self.repository.create_rule_group(base)

    def test_false_string_filter_is_false_not_truthy(self):
        self.assertEqual(1, self.repository.list_rule_groups({"enabled": "false"})["total"])
        self.assertEqual(0, self.repository.list_rule_groups({"enabled": "true"})["total"])

    def test_product_string_filter_is_one_enum_not_characters(self):
        self.assertEqual(1, self.repository.list_rule_groups({"products": "Dramawave"})["total"])
        self.assertEqual(0, self.repository.list_rule_groups({"products": "D"})["total"])

    def test_optimistic_version_and_enable_guard(self):
        record = self.repository.get_rule_group("g1")
        record["config_version"] = 2
        with self.assertRaises(AdControlV3Error) as raised:
            self.repository.update_rule_group("g1", record, expected_version=99)
        self.assertEqual("version_conflict", raised.exception.code)
        with self.assertRaises(AdControlV3Error) as raised:
            self.repository.set_group_state(
                "g1",
                enabled=True,
                updated_by="u1",
                updated_at="now",
                expected_version=2,
                expected_behavior_hash="b" * 64,
                expected_preview_id="missing",
                require_fresh_preview=True,
            )
        self.assertEqual("stale_preview", raised.exception.code)

    def test_emergency_state_can_atomically_clear_preview_pointer(self):
        self.repository.groups["g1"]["last_preview_id"] = "p1"
        self.repository.groups["g1"]["last_preview_hash"] = "a" * 64
        result = self.repository.set_group_state(
            "g1", enabled=False, emergency_stopped=True, clear_preview=True,
            updated_by="u1", updated_at="now",
        )
        self.assertEqual("", result["last_preview_id"])
        self.assertEqual("", result["last_preview_hash"])

    def test_target_persistence_limit_is_enforced_before_mutation(self):
        with self.assertRaises(AdControlV3Error) as raised:
            self.repository.save_preview({}, [{}] * (MAX_PERSISTED_TARGETS + 1))
        self.assertEqual("target_persist_limit_exceeded", raised.exception.code)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = []
        self.rowcount = 1
        self._rows = []

    def execute(self, sql, params=()):
        self.connection.calls.append((sql, tuple(params)))
        if self.connection.fail_contains and self.connection.fail_contains in sql:
            raise RuntimeError("injected bundle failure")
        if sql.startswith("SELECT"):
            if "COUNT(*)" in sql:
                self._rows = [{"total": 0}]
            else:
                self._rows = []
        return self.rowcount

    def fetchall(self):
        return self._rows

    def executemany(self, sql, rows):
        materialized = list(rows)
        self.connection.calls.append((sql, tuple(materialized)))
        if self.connection.fail_contains and self.connection.fail_contains in sql:
            raise RuntimeError("injected bundle failure")
        self.rowcount = len(materialized)
        return self.rowcount


class FakeConnection:
    def __init__(self, role, fail_contains=""):
        self.role = role
        self.calls = []
        self.closed = False
        self.committed = False
        self.rolled_back = False
        self.autocommit_values = []
        self.fail_contains = fail_contains

    def cursor(self):
        return FakeCursor(self)

    def autocommit(self, value):
        self.autocommit_values.append(value)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class MySQLBoundaryTests(unittest.TestCase):
    def test_only_eight_ads_ai_tables_are_allowlisted(self):
        self.assertEqual(8, len(TABLES))
        for key in TABLES:
            self.assertTrue(qualified_table(key).startswith("`ads_ai`."))
        with self.assertRaises(AdControlV3Error):
            qualified_table("ad_control_action_log")

    def test_reader_and_writer_factories_are_separate(self):
        readers = []
        writers = []

        def reader_factory():
            conn = FakeConnection("reader")
            readers.append(conn)
            return conn

        def writer_factory():
            conn = FakeConnection("writer")
            writers.append(conn)
            return conn

        repository = MySQLRepository(reader_factory, writer_factory)
        repository.list_products("facebook")
        self.assertEqual(1, len(readers))
        self.assertEqual(0, len(writers))
        repository.soft_delete_rule_group("g1", updated_by="u1", updated_at="now")
        self.assertEqual(1, len(writers))
        self.assertEqual([False], writers[0].autocommit_values)
        self.assertTrue(writers[0].committed)

    def test_product_filter_uses_one_bound_value_and_exists_bridge(self):
        readers = []

        def reader_factory():
            conn = FakeConnection("reader")
            readers.append(conn)
            return conn

        repository = MySQLRepository(reader_factory, lambda: FakeConnection("writer"))
        repository.list_rule_groups({"products": "Dramawave"})
        all_calls = [call for conn in readers for call in conn.calls]
        count_sql, count_params = all_calls[0]
        self.assertIn("ad_control_v3_rule_group_product", count_sql)
        self.assertIn("EXISTS", count_sql)
        self.assertEqual(("Dramawave",), count_params)

    def test_execution_date_filter_binds_utc8_day_as_utc_storage_bounds(self):
        readers = []

        def reader_factory():
            conn = FakeConnection("reader")
            readers.append(conn)
            return conn

        repository = MySQLRepository(reader_factory, lambda: FakeConnection("writer"))
        repository.list_executions({"date_from": "2026-07-17", "date_to": "2026-07-17"})
        count_sql, count_params = readers[0].calls[0]
        self.assertIn("e.created_at>=%s", count_sql)
        self.assertIn("e.created_at<%s", count_sql)
        self.assertEqual(("2026-07-16 16:00:00", "2026-07-17 16:00:00"), count_params)

    def test_enable_update_contains_atomic_preview_guard(self):
        source = (ROOT / "features" / "ad_control_v3" / "repository.py").read_text(encoding="utf-8")
        self.assertIn("p.expires_at>UTC_TIMESTAMP(6)", source)
        self.assertIn("p.behavior_hash=", source)
        self.assertIn("require_fresh_preview", source)

    def test_preview_execution_bundle_rolls_back_as_one_transaction(self):
        writers = []

        def writer_factory():
            conn = FakeConnection("writer", "ad_control_v3_execution` (")
            writers.append(conn)
            return conn

        repository = MySQLRepository(lambda: FakeConnection("reader"), writer_factory)
        common = {
            "rule_group_id": "g1", "config_version": 1, "behavior_hash": "a" * 64,
            "optimizer_id": 248, "channel": "facebook", "object_level": "campaign",
            "summary": {}, "snapshot_relative_path": "snapshots/preview/p.json.gz",
            "snapshot_sha256": "b" * 64, "snapshot_byte_size": 10,
            "created_by_user_id": "u1", "created_at": "2026-07-16 00:00:00.000000",
        }
        preview = dict(common, preview_id="p1", status="ready", expires_at="2026-07-16 01:00:00.000000")
        execution = dict(
            common,
            execution_id="e1", preview_id="p1", run_mode="observe", trigger_source="manual_preview",
            status="observed", finished_at="2026-07-16 00:00:00.000000",
        )
        with self.assertRaises(RuntimeError):
            repository.save_preview_execution_bundle(preview, execution, [])
        self.assertTrue(writers[0].rolled_back)
        self.assertFalse(writers[0].committed)

    def test_preview_execution_targets_use_bounded_executemany_chunks(self):
        writers = []

        def writer_factory():
            conn = FakeConnection("writer")
            writers.append(conn)
            return conn

        repository = MySQLRepository(lambda: FakeConnection("reader"), writer_factory)
        common = {
            "rule_group_id": "g1", "config_version": 1, "behavior_hash": "a" * 64,
            "optimizer_id": 248, "channel": "facebook", "object_level": "campaign",
            "summary": {}, "snapshot_relative_path": "snapshots/preview/p.json.gz",
            "snapshot_sha256": "b" * 64, "snapshot_byte_size": 10,
            "created_by_user_id": "u1", "created_at": "2026-07-16 00:00:00.000000",
        }
        preview = dict(common, preview_id="p1", status="ready", expires_at="2026-07-16 01:00:00.000000")
        execution = dict(
            common, execution_id="e1", preview_id="p1", run_mode="observe",
            trigger_source="manual_preview", status="observed", finished_at="2026-07-16 00:00:00.000000",
        )
        target_count = TARGET_INSERT_CHUNK_SIZE * 2 + 1
        targets = [{"object_id": "c-%s" % index} for index in range(target_count)]
        repository.save_preview_execution_bundle(preview, execution, targets)
        target_batches = [
            params for sql, params in writers[0].calls
            if "preview_target" in sql or "execution_target" in sql
        ]
        self.assertEqual(
            [TARGET_INSERT_CHUNK_SIZE, TARGET_INSERT_CHUNK_SIZE, 1] * 2,
            [len(batch) for batch in target_batches],
        )
        self.assertTrue(writers[0].committed)


class MigrationContractTests(unittest.TestCase):
    def test_ddl_has_exactly_eight_v3_tables_and_ads_ai_only(self):
        ddl = (ROOT / "doc" / "008.ad-control-v3-dynamic-ui" / "sql" / "001_create_ad_control_v3_tables.sql").read_text(encoding="utf-8")
        self.assertEqual(8, ddl.count("CREATE TABLE IF NOT EXISTS"))
        self.assertNotIn("kunlunads_dev`.`ad_control_v3", ddl)
        self.assertNotIn("ad_control_action_log", ddl)
        for table in TABLES.values():
            self.assertIn("`ads_ai`.`%s`" % table, ddl)

    def test_exact_product_enum_columns_are_binary_collated(self):
        ddl = (ROOT / "doc" / "008.ad-control-v3-dynamic-ui" / "sql" / "001_create_ad_control_v3_tables.sql").read_text(encoding="utf-8")
        exact_product_column = "`product_value` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
        exact_canonical_column = "`canonical_product` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
        self.assertEqual(4, ddl.count(exact_product_column))
        self.assertEqual(1, ddl.count(exact_canonical_column))
        self.assertNotIn("`product_value` VARCHAR(128) NOT NULL", ddl)
        self.assertNotIn("`canonical_product` VARCHAR(128) NOT NULL", ddl)

    def test_seed_has_15_exact_products_including_w2a(self):
        seed = (ROOT / "doc" / "008.ad-control-v3-dynamic-ui" / "sql" / "002_seed_fb_short_drama_products.sql").read_text(encoding="utf-8")
        self.assertEqual(15, seed.count("JSON_ARRAY(),JSON_OBJECT"))
        self.assertIn("'[w2a]FreeReels-double'", seed)
        self.assertIn("'Drama Suagr'", seed)
        self.assertNotIn("LIKE", seed.upper())

    def test_rollback_refuses_real_data_before_any_drop(self):
        rollback = (ROOT / "doc" / "008.ad-control-v3-dynamic-ui" / "sql" / "900_rollback_empty_v3_tables.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE PROCEDURE", rollback)
        self.assertIn("SIGNAL SQLSTATE '45000'", rollback)
        self.assertLess(rollback.index("IF business_rows <> 0"), rollback.index("DROP TABLE `ads_ai`.`ad_control_v3_runner_event`"))
        self.assertNotIn("DROP TABLE IF EXISTS", rollback)


if __name__ == "__main__":
    unittest.main()

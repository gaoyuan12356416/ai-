"""Deployment safety checks without credentials, network, service mutation or DDL."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("moboreels_deploy", ROOT / "ops/moboreels-attribution-20261113/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentSafetyTests(unittest.TestCase):
    def test_sql_comment_semicolon_is_not_statement_boundary(self):
        sql = "-- ignored;\nCREATE TABLE IF NOT EXISTS ads_ai.example (\n id bigint NOT NULL COMMENT '说明;保留',\n PRIMARY KEY(id)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='表说明;保留';"
        self.assertEqual(len(deploy.split_sql(sql)), 1)
        self.assertEqual(deploy.expected_schema(deploy.split_sql(sql))["example"]["columns"][0]["comment"], "说明;保留")

    def test_schema_drift_rejected_with_independent_metadata(self):
        sql = "CREATE TABLE IF NOT EXISTS ads_ai.example (\n id bigint NOT NULL COMMENT '编号',\n name varchar(64) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '名称',\n PRIMARY KEY(id),\n KEY n(name(20))\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='示例';"
        rows = [
            {"check":"new_table","table_name":"example","exists":1,"engine":"InnoDB","collation":"utf8mb4_unicode_ci","comment":"示例"},
            {"check":"column","table_name":"example","position":1,"column_name":"id","column_type":"bigint(20)","nullable":"NO","default":None,"collation":None,"extra":"","comment":"编号"},
            {"check":"column","table_name":"example","position":2,"column_name":"name","column_type":"varchar(64)","nullable":"YES","default":None,"collation":"utf8mb4_bin","extra":"","comment":"名称"},
            {"check":"index","table_name":"example","index_name":"PRIMARY","non_unique":0,"position":1,"column_name":"id","prefix_length":None},
            {"check":"index","table_name":"example","index_name":"n","non_unique":1,"position":1,"column_name":"name","prefix_length":20},
        ]
        statements = deploy.split_sql(sql)
        deploy.validate_schema(statements, rows)
        rows[2]["collation"] = "utf8mb4_unicode_ci"
        with self.assertRaisesRegex(RuntimeError, "collation"):
            deploy.validate_schema(statements, rows)

    def test_all_reviewed_tables_have_columns_and_chinese_comments(self):
        statements = []
        for path in deploy.DDL_FILES:
            statements += deploy.split_sql((ROOT / path).read_text(encoding="utf-8"))
        expected = deploy.expected_schema(statements)
        self.assertEqual(set(expected), deploy.TABLES)
        self.assertEqual({table:len(value["columns"]) for table,value in expected.items()},
            {"cdreader_koc_order":22, "cdreader_koc_ad_revenue":12, "cdreader_koc_ad_day_snapshot":7,
             "cdreader_koc_short_link":21, "cdreader_koc_media_account":19})
        for definition in expected.values():
            self.assertTrue(all(c["comment"] for c in definition["columns"]))

    def test_live_sha_fence_rejects_unrelated_edit_and_unexpected_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "owner.py"
            entry = {"target":str(target),"before":None,"after":"irrelevant"}
            deploy.fence(entry)
            target.write_text("old",encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Live SHA"):
                deploy.fence(entry)
            entry["before"] = deploy.digest(target)
            deploy.fence(entry)
            target.write_text("concurrent change",encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Live SHA"):
                deploy.fence(entry)

    def test_tree_fence_ignores_bytecode_but_catches_source_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "module.py"
            source.write_text("x=1\n",encoding="utf-8")
            before = deploy.tree_digest(root)
            (root / "__pycache__").mkdir()
            (root / "__pycache__/module.pyc").write_bytes(b"cache")
            self.assertEqual(deploy.tree_digest(root), before)
            source.write_text("x=2\n",encoding="utf-8")
            self.assertNotEqual(deploy.tree_digest(root), before)

    def test_busy_check_preserves_unknown_receipt_and_blocks_only_active_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            path = root / "ledger.sqlite3"
            with closing(sqlite3.connect(str(path))) as conn, conn:
                conn.execute("CREATE TABLE task(status TEXT,lease_until INTEGER)")
                conn.execute("INSERT INTO task VALUES('unknown',0)")
            spec = {"checks":[{"database":str(path),"name":"leased","sql":"SELECT COUNT(*) FROM task WHERE lease_until>strftime('%s','now')"}],
                    "informational_checks":[{"database":str(path),"name":"unknown","sql":"SELECT COUNT(*) FROM task WHERE status='unknown'"}]}
            deploy.busy(spec, root, "x")
            with closing(sqlite3.connect(str(path))) as conn, conn:
                self.assertEqual(conn.execute("SELECT status FROM task").fetchone()[0], "unknown")
                conn.execute("INSERT INTO task VALUES('running',9999999999)")
            with self.assertRaisesRegex(RuntimeError, "In-flight"):
                deploy.busy(spec, root, "x")


if __name__ == "__main__":
    unittest.main()


"""Deployment safety checks without credentials, network, service mutation or DDL."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("moboreels_deploy", ROOT / "ops/moboreels-attribution-20261113/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
rollback_spec = importlib.util.spec_from_file_location("moboreels_rollback", ROOT / "ops/moboreels-attribution-20261113/rollback.py")
rollback = importlib.util.module_from_spec(rollback_spec)
rollback_spec.loader.exec_module(rollback)


class DeploymentSafetyTests(unittest.TestCase):
    def test_fb_metric_history_is_not_backed_up_but_nonempty_task_table_blocks(self):
        checks = json.loads((ROOT / 'ops/moboreels-attribution-20261113/activation-checks.template.json').read_text())
        fb = checks['phases']['fb']
        self.assertEqual(fb['backup_databases'], ['/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'])
        guard = next(row for row in fb['checks'] if row['name'] == 'metric_task_table_must_be_empty')
        self.assertEqual(guard['database'], '/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3')
        self.assertEqual(guard['sql'], 'SELECT COUNT(*) FROM fb_auto_task')
        self.assertNotIn(guard, fb.get('informational_checks', []))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'private').mkdir()
            metric = root / 'metric.sqlite3'
            with closing(sqlite3.connect(str(metric))) as conn, conn:
                conn.execute('CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY)')
            spec = {'checks': [dict(guard, database=str(metric))]}
            deploy.busy(spec, root, 'fb')
            with closing(sqlite3.connect(str(metric))) as conn, conn:
                conn.execute('INSERT INTO fb_auto_task VALUES(1)')
            with self.assertRaisesRegex(RuntimeError, 'In-flight'):
                deploy.busy(spec, root, 'fb')

    def test_rollback_loads_current_tool_release_and_verifies_both_versions(self):
        manifest = {"commit": "b" * 40, "release": str(ROOT / "original-business-release")}
        helper = SimpleNamespace(verify=Mock())
        module_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        with patch.object(rollback.importlib.util, "spec_from_file_location", return_value=module_spec) as locate, \
                patch.object(rollback.importlib.util, "module_from_spec", return_value=helper):
            self.assertIs(rollback.load_deployer(manifest, "a" * 40), helper)
        locate.assert_called_once_with("moboreels_deploy", ROOT / "ops/moboreels-attribution-20261113/deploy.py")
        self.assertEqual(helper.verify.call_args_list, [call(ROOT, "a" * 40),
                                                       call(Path(manifest["release"]), "b" * 40)])
        self.assertEqual(manifest, {"commit": "b" * 40, "release": str(ROOT / "original-business-release")})

    def test_rollback_deployer_sha_defaults_to_business_commit(self):
        manifest = {"commit": "b" * 40, "release": str(ROOT)}
        helper = SimpleNamespace(verify=Mock())
        module_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        with patch.object(rollback.importlib.util, "spec_from_file_location", return_value=module_spec), \
                patch.object(rollback.importlib.util, "module_from_spec", return_value=helper):
            rollback.load_deployer(manifest)
        self.assertEqual(helper.verify.call_args_list, [call(ROOT, "b" * 40), call(ROOT, "b" * 40)])

    def test_cancelled_fb_preparation_only_blocks_with_an_active_lease(self):
        checks = json.loads((ROOT / 'ops/moboreels-attribution-20261113/activation-checks.template.json').read_text())
        query = next(row['sql'] for row in checks['phases']['fb']['checks'] if row['name']=='fb_video_task_busy')
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE fb_video_task(status,prepare_state,link_state,prepare_lease,prepare_until,link_lease,link_until,execution_lease,execution_until)')
            conn.execute("INSERT INTO fb_video_task VALUES('cancelled','preparing','ready','','','','','','')")
            self.assertEqual(conn.execute(query).fetchone()[0], 0)
            conn.execute("UPDATE fb_video_task SET prepare_lease='still-active',prepare_until='2999-01-01'")
            self.assertEqual(conn.execute(query).fetchone()[0], 1)
            conn.execute("UPDATE fb_video_task SET prepare_until='2000-01-01'")
            self.assertEqual(conn.execute(query).fetchone()[0], 0)
            conn.execute("UPDATE fb_video_task SET status='queued'")
            self.assertEqual(conn.execute(query).fetchone()[0], 1)

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

    def test_backup_explicit_owner_list_excludes_coordinator_and_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            owner = root / "owner.sqlite3"
            with closing(sqlite3.connect(str(owner))) as conn, conn:
                conn.execute("CREATE TABLE receipt(id INTEGER PRIMARY KEY, status TEXT)")
                conn.executemany("INSERT INTO receipt VALUES(?,?)", [(1, "unknown"), (2, "published")])
            # If a read-only check database leaked into backup selection, this absent
            # coordinator path would fail; only the explicitly owned ledger is copied.
            deploy.backups({"backup_databases": [str(owner)], "checks": [
                {"database": str(root / "coordinator-must-not-open.sqlite3")}]}, root, "x")
            copies = list((root / "private").rglob("*.sqlite3"))
            self.assertEqual(len(copies), 1)
            with closing(sqlite3.connect(str(copies[0]))) as conn:
                self.assertEqual(conn.execute("SELECT * FROM receipt ORDER BY id").fetchall(),
                                 [(1, "unknown"), (2, "published")])
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_backup_pins_snapshot_before_concurrent_wal_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            owner = root / "owner.sqlite3"
            real_connect = sqlite3.connect
            with closing(real_connect(str(owner))) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE receipt(id INTEGER PRIMARY KEY, value TEXT)")
                writer.execute("INSERT INTO receipt VALUES(1,'before')")
                writer.commit()
                snapshots = []

                class SourceConnection(sqlite3.Connection):
                    def backup(self, destination, **kwargs):
                        snapshots.append(self.in_transaction)
                        # Commit after backups() pins its source read snapshot, but
                        # before sqlite3_backup starts. A deferred BEGIN alone fails this test.
                        writer.execute("UPDATE receipt SET value='after' WHERE id=1")
                        writer.commit()
                        return super().backup(destination, **kwargs)

                def connect(path, **kwargs):
                    if kwargs.get("uri"):
                        kwargs["factory"] = SourceConnection
                    return real_connect(path, **kwargs)

                with patch.object(deploy.sqlite3, "connect", side_effect=connect):
                    deploy.backups({"backup_databases": [str(owner)]}, root, "x")
                self.assertEqual(snapshots, [True])
                copied = next((root / "private").rglob("*.sqlite3"))
                with closing(real_connect(str(copied))) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM receipt").fetchone(), ("before",))
                self.assertEqual(writer.execute("SELECT value FROM receipt").fetchone(), ("after",))

    def test_backup_deadline_discards_partial_and_releases_source_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private").mkdir()
            owner = root / "owner.sqlite3"
            with closing(sqlite3.connect(str(owner))) as conn, conn:
                conn.execute("CREATE TABLE receipt(id INTEGER PRIMARY KEY, status TEXT)")
                conn.execute("INSERT INTO receipt VALUES(1,'unknown')")
            # Deadline is reached in the sqlite3_backup progress callback.
            with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 121]):
                with self.assertRaisesRegex(RuntimeError, "backup deadline exceeded"):
                    deploy.backups({"backup_databases": [str(owner)]}, root, "x")
            self.assertEqual(list((root / "private").rglob("*.sqlite3")), [])
            self.assertEqual(list((root / "private").rglob("*.partial")), [])
            with closing(sqlite3.connect(str(owner), timeout=0)) as conn:
                conn.execute("BEGIN EXCLUSIVE")  # No leaked read transaction or connection.
                self.assertEqual(conn.execute("SELECT status FROM receipt").fetchone(), ("unknown",))
                conn.rollback()


if __name__ == "__main__":
    unittest.main()

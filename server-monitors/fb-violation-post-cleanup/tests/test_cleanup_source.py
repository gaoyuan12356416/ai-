import importlib.util
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "cleanup_source.py"
SPEC = importlib.util.spec_from_file_location("cleanup_source", MODULE_PATH)
source = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source
SPEC.loader.exec_module(source)

START = "2026-07-28T09:14:18+00:00"
END = "2026-10-26T09:14:18+00:00"


class Reader:
    schema = "kunlunads_dev"

    def __init__(self, callback=lambda sql, params: []):
        self.callback, self.queries, self.snapshots = callback, [], 0

    @contextmanager
    def snapshot(self):
        self.snapshots += 1
        yield self

    def select(self, sql, params=()):
        self.queries.append((sql, list(params)))
        return self.callback(sql, list(params))


def ordinary(**overrides):
    row = {"log_id": "9", "page_id": "101", "source_row_id": "14508332348",
           "source_type": 3, "material_id": "7543889", "fb_violation": 1,
           "raw_post_id": "801", "real_post_id": "101_901", "resource_id": "",
           "published_log_time": datetime(2026, 8, 1, 12), "publisher_fb_user_id": ""}
    return {**row, **overrides}


def ai_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE fb_auto_task(id INTEGER,page_id TEXT,material_id TEXT,status TEXT,
            graph_post_id TEXT,unknown_outcome INTEGER,completed_at_utc TEXT);
        CREATE TABLE fb_auto_publish_ledger(task_id INTEGER,page_id TEXT,material_id TEXT,status TEXT,
            graph_post_id TEXT,unknown_outcome INTEGER);
        CREATE TABLE fb_auto_publish_attempt(id INTEGER,task_id INTEGER,fb_user_id TEXT,result_kind TEXT);
        CREATE TABLE fb_video_task(task_id TEXT,page_id TEXT,material_id TEXT,status TEXT,
            graph_post_id TEXT,unknown_outcome INTEGER);
        CREATE TABLE fb_video_attempt(id INTEGER,task_id TEXT,fb_user_id TEXT,result_kind TEXT,
            graph_post_id TEXT,created_at_utc TEXT);
    """)
    return conn


class AdapterTests(unittest.TestCase):
    def test_exact_authorized_groups_and_read_endpoint(self):
        with self.assertRaisesRegex(source.SourceError, "exact_authorized_groups"):
            source.SourceAdapter(Reader(), groups=(3, 4, 24, 25, 26))
        for config in ({"FB_AUTO_MYSQL_PORT": "63353"}, {"MYSQL_HOST": "127.0.0.1"}):
            with self.assertRaisesRegex(source.SourceError, "read_only_endpoint"):
                source.ReadOnlyMySQL(config)

    def test_page_membership_deduplicates_and_preserves_all_groups(self):
        reader = Reader(lambda sql, params: [{"page_id": "101", "group_id": 3},
            {"page_id": "101", "group_id": 4}, {"page_id": "101", "group_id": 3}])
        self.assertEqual({"101": [3, 4]}, source.SourceAdapter(reader)._pages(reader))
        query, params = reader.queries[0]
        self.assertIn("p.status=0", query)
        self.assertIn("i.deleted_at=0", query)
        self.assertIn("g.is_delete=0", query)
        self.assertEqual([3, 4, 24, 25], params)

    def test_logs_namespace_boundary_and_resource_conflict(self):
        def callback(sql, params):
            if "ads_post_auto_publish_logs" in sql:
                return [ordinary(log_id="10", resource_id="wrong-material")]
            return [ordinary(), ordinary(log_id="11", published_log_time=datetime(2026, 10, 26, 17, 14, 18)),
                    ordinary(log_id="12", fb_violation=5)]
        reader = Reader(callback)
        candidates, unresolved = source.SourceAdapter(reader)._sql_logs(reader, {"101": [3]}, source.utc(START), source.utc(END))
        self.assertEqual(["9"], [item["log_id"] for item in candidates])
        self.assertEqual("2026-08-01T04:00:00+00:00", candidates[0]["published_at_utc"])
        self.assertEqual("7543889", candidates[0]["material_id"])
        self.assertEqual("14508332348", candidates[0]["source_row_id"])
        self.assertEqual({"outside_frozen_window", "source_scope_or_flag_conflict", "auto_resource_material_mismatch"},
                         {item["reason"] for item in unresolved})
        for query, params in reader.queries:
            self.assertIn("s.id=l.source_id", query)
            self.assertIn("s.source_type=3 AND s.fb_violation=1", query)
            self.assertEqual(datetime(2026, 7, 28, 17, 14, 18), params[-2])
            if "ads_post_auto_publish_logs" in query:
                self.assertIn("FORCE INDEX (`created_at`)", query)
                self.assertIn("l.status=1", query)

    def test_log_page_chunks_never_exceed_100(self):
        reader = Reader()
        pages = {str(index): [3] for index in range(1, 206)}
        source.SourceAdapter(reader)._sql_logs(reader, pages, source.utc(START), source.utc(END))
        self.assertEqual(6, len(reader.queries))
        self.assertEqual([100, 100, 5, 100, 100, 5], [len(params)-2 for _, params in reader.queries])

    def test_nonviolating_same_post_mapping_is_excluded(self):
        row = ordinary()
        candidate = {**row, "table": "ads_facebook_post_log"}
        reader = Reader(lambda sql, params: [ordinary(log_id="100", material_id="99", fb_violation=0)]
                        if "ads_post_auto_publish_logs" in sql else [])
        eligible, unresolved = source.SourceAdapter(reader)._conflicting_logs(reader, [candidate], source.utc(START), source.utc(END))
        self.assertEqual([], eligible)
        self.assertEqual("99", unresolved[0]["conflicting_material_id"])
        self.assertEqual("conflicting_log_material_identity", unresolved[0]["reason"])

    def test_placeholder_aliases_do_not_link_unrelated_posts(self):
        candidate = {**ordinary(real_post_id="0"), "table": "ads_facebook_post_log"}
        reader = Reader(lambda sql, params: [ordinary(log_id="100", material_id="99", raw_post_id="802", real_post_id="0", fb_violation=0)])
        eligible, unresolved = source.SourceAdapter(reader)._conflicting_logs(reader, [candidate], source.utc(START), source.utc(END))
        self.assertEqual([candidate], eligible)
        self.assertEqual([], unresolved)
        self.assertEqual({"901", "101_901"}, source.post_aliases("101", ["101_901", "0", "None", ""]))

    def test_non_target_ai_material_cannot_hide_same_video_conflict(self):
        candidate = {**ordinary(), "table": "ads_facebook_post_log"}
        rows = [{"table": "fb_video_task", "origin": "ai_manual", "log_id": "a1", "page_id": "101",
                 "graph_post_id": "801", "material_id": "unflagged-other-material", "status": "published", "unknown_outcome": 0}]
        eligible, unresolved = source.SourceAdapter(Reader())._conflicting_ai([candidate], rows)
        self.assertEqual([], eligible)
        self.assertEqual("conflicting_ai_material_identity", unresolved[0]["reason"])

    def test_ai_unknown_same_video_does_not_become_deletion_candidate(self):
        candidate = {**ordinary(), "table": "ads_facebook_post_log"}
        rows = [{"table": "fb_video_task", "origin": "ai_manual", "log_id": "a1", "page_id": "101",
                 "graph_post_id": "801", "material_id": "7543889", "status": "unknown", "unknown_outcome": 1}]
        eligible, unresolved = source.SourceAdapter(Reader())._conflicting_ai([candidate], rows)
        self.assertEqual([], eligible)
        self.assertEqual("ai_publication_unresolved_for_candidate", unresolved[0]["reason"])

    def test_precheck_refreshes_flags_and_exact_source_rows(self):
        current = {"flag": 1, "id": "14508332348"}
        def callback(sql, params):
            if "ads_facebook_page_group" in sql:
                return [{"page_id": "101", "group_id": 3}]
            return [{"source_row_id": current["id"], "material_id": "7543889", "source_type": 3, "fb_violation": current["flag"]}]
        reader = Reader(callback)
        adapter = source.SourceAdapter(reader)
        self.assertTrue(adapter.precheck("101", ["7543889"], ["14508332348"])["eligible"])
        current["flag"] = 5
        self.assertEqual("material_no_longer_violating", adapter.precheck("101", ["7543889"], ["14508332348"])["reason"])
        current.update(flag=1, id="77")
        self.assertEqual("source_row_changed", adapter.precheck("101", ["7543889"], ["14508332348"])["reason"])
        self.assertEqual(3, reader.snapshots)

    def test_credentials_exact_page_preference_dedup_and_safe_context(self):
        reader = Reader(lambda sql, params: [
            {"credential_id": "2", "page_id": "101", "fb_user_id": "88", "page_access_token": "secret-A"},
            {"credential_id": "3", "page_id": "101", "fb_user_id": "99", "page_access_token": "secret-B"},
            {"credential_id": "4", "page_id": "101", "fb_user_id": "99", "page_access_token": "secret-B"}])
        credentials = source.SourceAdapter(reader).credentials("101", "99")
        self.assertEqual(["3", "2"], [item.credential_id for item in credentials])
        self.assertNotIn("secret", repr(credentials))
        self.assertNotIn("token", credentials[0].context)
        self.assertIn("status=0", reader.queries[0][0])
        self.assertEqual(["101"], reader.queries[0][1])

    def test_ai_published_requires_agreement_and_success_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "publisher.db"
            conn = ai_db(path)
            for task, ledger_post, status in ((1, "801", "published"), (2, "different", "published"), (3, "803", "submitted")):
                post = str(800 + task)
                conn.execute("INSERT INTO fb_auto_task VALUES(?,?,?,?,?,?,?)", (task, "101", "7543889", status, post, 0, "2026-08-01T04:00:00+00:00"))
                conn.execute("INSERT INTO fb_auto_publish_ledger VALUES(?,?,?,?,?,?)", (task, "101", "7543889", status, ledger_post, 0))
            conn.execute("INSERT INTO fb_video_task VALUES('manual1','101','7543889','published','804',0)")
            conn.execute("INSERT INTO fb_video_task VALUES('manual2','101','7543889','published','805',0)")
            conn.execute("INSERT INTO fb_video_attempt VALUES(1,'manual1','99','success','804','2026-08-02T04:00:00+00:00')")
            conn.commit()
            conn.close()
            original_bytes = path.read_bytes()
            reader = Reader(lambda sql, params: [{"source_row_id": "14508332348", "material_id": "7543889", "source_type": 3, "fb_violation": 1}])
            adapter = source.SourceAdapter(reader, path)
            candidates, unresolved, stats = adapter._ai_candidates(reader, {"101": [3]}, source.utc(START), source.utc(END))
            self.assertEqual({"1", "manual1"}, {item["log_id"] for item in candidates})
            self.assertEqual({"ai_task_ledger_mismatch", "ai_publication_unresolved", "invalid_evidence_timestamp"}, {item["reason"] for item in unresolved})
            self.assertEqual(5, stats["ai_rows_reviewed"])
            self.assertEqual(original_bytes, path.read_bytes())
            automatic = next(item for item in candidates if item["log_id"] == "1")
            self.assertEqual("801", automatic["raw_post_id"])
            self.assertEqual("", automatic["real_post_id"])

    def test_missing_ai_file_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.db"
            with self.assertRaisesRegex(source.SourceError, "ai_read_only_scan_failed"):
                source.SourceAdapter(Reader(), path)._ai_rows({"101": [3]})
            self.assertFalse(path.exists())


class FakeConnection:
    def __init__(self, read_only=1):
        self.read_only, self.commands, self.closed, self.rolled_back = read_only, [], False, False
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=()): self.commands.append((sql, params))
    def fetchone(self): return {"read_only": self.read_only}
    def fetchmany(self, count): return []
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


class ReaderTests(unittest.TestCase):
    def test_snapshot_enforces_readonly_timezone_isolation_and_select_only(self):
        connection = FakeConnection()
        reader = source.ReadOnlyMySQL({}, connect_fn=lambda **kwargs: connection)
        with reader.snapshot() as snapshot:
            self.assertEqual([], snapshot.select("SELECT 1"))
            with self.assertRaisesRegex(source.SourceError, "only_single_select"):
                snapshot.select("DELETE FROM anything")
        commands = [item[0] for item in connection.commands]
        self.assertIn("SET SESSION time_zone='+08:00'", commands)
        self.assertIn("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ", commands)
        self.assertIn("SET TRANSACTION READ ONLY", commands)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.rolled_back)

    def test_nonreadonly_server_aborts_and_closes(self):
        connection = FakeConnection(0)
        reader = source.ReadOnlyMySQL({}, connect_fn=lambda **kwargs: connection)
        with self.assertRaisesRegex(source.SourceError, "database_not_read_only"):
            with reader.snapshot():
                self.fail("must not yield writable connection")
        self.assertTrue(connection.closed)

    def test_driver_exception_never_exposes_secrets(self):
        def connect(**kwargs):
            raise ValueError("password=must-not-appear")
        with self.assertRaisesRegex(source.SourceError, "source_connection_or_snapshot_failed") as captured:
            with source.ReadOnlyMySQL({}, connect_fn=connect).snapshot():
                pass
        self.assertNotIn("must-not-appear", str(captured.exception))


if __name__ == "__main__":
    unittest.main()

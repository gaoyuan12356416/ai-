"""No database connection: protocol, pagination and credential routing tests."""
import json
import os
from pathlib import Path
import queue
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meta_violation_cleanup import sql_source as source


class FakeOutput:
    def __init__(self):
        self.items = queue.Queue()

    def readline(self, limit):
        return self.items.get(timeout=5)

    def close(self):
        self.items.put("")


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.pending = ""

    def write(self, value):
        self.pending += value

    def flush(self):
        sql, self.pending = self.pending, ""
        self.process.commands.append(sql)
        marker = re.search(r"SELECT JSON_ARRAY\(_utf8mb4 0x([0-9a-f]+)\);\s*$", sql)
        marker = bytes.fromhex(marker.group(1)).decode()
        if sql.startswith("SELECT JSON_OBJECT('read_only'"):
            response = [{"read_only": self.process.readonly}]
        elif sql.startswith("SET SESSION"):
            response = []
        else:
            response = self.process.responder(sql)
        if response is None:
            self.process.returncode = 1
            self.process.stdout.items.put("")
            return
        for row in response:
            self.process.stdout.items.put(json.dumps(row) + "\n")
        self.process.stdout.items.put(json.dumps([marker]) + "\n")

    def close(self):
        self.process.returncode = self.process.returncode or 0
        self.process.stdout.items.put("")


class FakeMysql:
    def __init__(self, responder, readonly=1):
        self.responder = responder
        self.readonly = readonly
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.returncode = None
        self.commands = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        return self.returncode or 0

    def terminate(self):
        self.returncode = -15
        self.stdout.items.put("")

    def kill(self):
        self.terminate()


def violation(identifier, status=0, date="2026-10-22 08:00:00.000000"):
    return {"id": str(identifier), "ad_id": str(10000 + identifier), "source_id": "300", "app_id": "1479",
            "created_at": date, "updated_at": date, "issues_info": '{"global":{"Spam":"test"}}',
            "is_dynamic": 0, "delete_status": status}


class SqlSourceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DRAMA_DB_HOST": "101.32.56.53", "DRAMA_DB_PORT": "63350",
                                          "DRAMA_DB_USER": "synthetic-user", "DRAMA_DB_PASSWORD": "synthetic-secret",
                                          "SQL_GATE_BYPASS": "1"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def make_source(self, **config):
        return source.SqlSource(dict(env_file=None, **config))

    def run_source(self, method, responder, readonly=1, **config):
        process = FakeMysql(responder, readonly=readonly)
        adapter = self.make_source(**config)
        with patch.object(source.subprocess, "Popen", return_value=process) as popen:
            result = method(adapter)
        return result, adapter, process, popen

    def test_gated_readonly_command_no_password_and_bypass_disabled(self):
        result, adapter, process, popen = self.run_source(lambda db: db.products(), lambda sql: [])
        self.assertEqual(result, [])
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/mysql")
        self.assertIn("--port=63350", command)
        self.assertIn("--no-defaults", command)
        self.assertNotIn("synthetic-secret", " ".join(command))
        self.assertNotIn("-e", command)
        self.assertEqual(popen.call_args.kwargs["env"]["MYSQL_PWD"], "synthetic-secret")
        self.assertEqual(popen.call_args.kwargs["env"]["SQL_GATE_BYPASS"], "0")
        self.assertIs(popen.call_args.kwargs["stderr"], source.subprocess.DEVNULL)
        self.assertIn("SET SESSION time_zone='+08:00'", process.commands[1])
        self.assertIn("SET SESSION max_execution_time=30000", process.commands[1])
        self.assertIn("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY", process.commands[1])
        self.assertEqual(adapter.safe_metrics["sessions"], 1)
        self.assertNotIn("synthetic", str(adapter.safe_metrics))

    def test_write_endpoint_and_gate_bypass_binary_rejected(self):
        with patch.dict(os.environ, {"DRAMA_DB_PORT": "63353"}):
            with self.assertRaisesRegex(source.SqlSourceError, "read_only_endpoint_required"):
                self.make_source()
        with self.assertRaisesRegex(source.SqlSourceError, "fifo_mysql_required"):
            self.make_source(mysql_binary="/usr/bin/mysql.real")

    def test_missing_read_only_proof_stops_before_data_query(self):
        process = FakeMysql(lambda sql: self.fail("source read must not occur"), readonly=0)
        with patch.object(source.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(source.SqlSourceError, "read_only_proof_missing"):
                self.make_source().products()
        self.assertEqual(len(process.commands), 1)

    def test_violation_states_and_pages_share_one_snapshot(self):
        pages = iter([[violation(1), violation(2)], [violation(3)], [violation(4, 1)], []])
        def responder(sql):
            if "GROUP BY delete_status" in sql:
                return [{"delete_status": 0}, {"delete_status": 1}, {"delete_status": None}]
            self.assertIn("FORCE INDEX(dc)", sql)
            self.assertIn("ORDER BY created_at,id LIMIT 2", sql)
            return next(pages)
        result, adapter, process, popen = self.run_source(
            lambda db: db.violations("2026-10-01", "2026-10-23"), responder, page_size=2)
        self.assertEqual([row["id"] for row in result], ["1", "2", "3", "4"])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(adapter.safe_metrics["violation_pages"], 4)
        self.assertTrue(any("id>2" in sql for sql in process.commands))
        self.assertTrue(any("delete_status IS NULL" in sql for sql in process.commands))

    def test_duplicate_or_out_of_window_violation_fails_whole_scan(self):
        for bad in (violation(1), violation(2, date="2026-10-23 00:00:00.000000")):
            pages = iter([[{"delete_status": 0}], [violation(1), bad]])
            with self.assertRaises(source.SqlSourceError):
                self.run_source(lambda db: db.violations("2026-10-01", "2026-10-23"),
                                lambda sql: next(pages))

    def test_partial_sql_stream_never_returns_partial_results_or_sensitive_error(self):
        process = FakeMysql(lambda sql: None)
        adapter = self.make_source()
        with patch.object(source.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(source.SqlSourceError, "sql_stream_incomplete") as error:
                adapter.credentials()
        self.assertNotIn("accessToken", str(error.exception))
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertEqual(adapter.safe_metrics["query_failures"], 1)

    def test_timeout_closes_session_without_exposing_query(self):
        adapter = self.make_source()
        session = source._Session(adapter)
        process = FakeMysql(lambda sql: [])
        session.proc = process
        with patch.object(session.lines, "get", side_effect=queue.Empty):
            with self.assertRaisesRegex(source.SqlSourceError, "sql_query_timeout") as error:
                session.query("SELECT secret_value")
        self.assertTrue(session.closed)
        self.assertNotIn("secret_value", str(error.exception))

    def test_lineage_batches_maximum_200_and_preserves_duplicate_ad_rows(self):
        calls = []
        def responder(sql):
            calls.append(sql)
            if len(calls) == 1:
                return [{"id": "1", "ad_id": "1", "product": "1479"},
                        {"id": "2", "ad_id": "1", "product": "3302"}]
            return []
        result, _, _, _ = self.run_source(lambda db: db.lineages(map(str, range(1, 402))), responder)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["1"]), 2)
        self.assertEqual(result["1"][1]["row_id"], "2")
        self.assertTrue(all("WHERE ad_id IN" in sql and "LIKE" not in sql for sql in calls))

    def test_sources_and_write_time_violation_evidence_use_primary_batches(self):
        def responder(sql):
            self.assertIn("FORCE INDEX(PRIMARY)", sql)
            return [violation(1)] if "ads_facebook_violations" in sql else [
                {"id": "1", "source_type": 3, "source_id": "900"}]
        result, _, _, _ = self.run_source(lambda db: db.sources(["1"]), responder)
        self.assertEqual(result["1"]["source_id"], "900")
        result, _, _, _ = self.run_source(lambda db: db.violations_by_ids(["1"]), responder)
        self.assertEqual(result[0]["id"], "1")

    def test_newest_nonempty_credential_is_kept_only_in_memory(self):
        def responder(sql):
            self.assertIn("ORDER BY user_id,id DESC", sql)
            return [{"id": "3", "user_id": "803", "token": "current-synthetic"},
                    {"id": "2", "user_id": "803", "token": "old-synthetic"}]
        result, adapter, _, _ = self.run_source(lambda db: db.credentials(), responder)
        self.assertEqual(result, {"803": "current-synthetic"})
        self.assertNotIn("current-synthetic", str(adapter.safe_metrics))

    def credential_route(self, flag="1", **changes):
        row = dict(id="10", ad_id="100", product="1479", user_id="803", publish_queue_id="500",
                   queue_id="500", queue_product="1479", queue_user_id="803", default_token=flag,
                   setting_id="1479", default_user="804")
        row.update(changes)
        return row

    def test_selected_credential_obeys_product_default_and_queue_user(self):
        for flag, user in (("1", "804"), ("-1", "803")):
            route = self.credential_route(flag)
            def responder(sql):
                if "ads_template_make_queue" in sql:
                    return [route]
                self.assertIn("user_id=" + source._quote(user), sql)
                self.assertIn("ORDER BY id DESC LIMIT 1", sql)
                return [{"token": "chosen-synthetic"}]
            result, _, _, _ = self.run_source(lambda db: db.selected_credential(route), responder)
            self.assertEqual(result, user)
            self.assertNotIn("chosen-synthetic", result)

    def test_missing_mismatched_or_unknown_queue_never_falls_back(self):
        for changes in (dict(queue_id=None), dict(queue_product="3302"), dict(queue_user_id="999"),
                        dict(default_token="0"), dict(default_user="0")):
            route = self.credential_route(**changes)
            def responder(sql):
                self.assertIn("ads_template_make_queue", sql, "must not fetch another credential")
                return [route]
            with self.assertRaises(source.SqlSourceError):
                self.run_source(lambda db: db.selected_credential(route), responder)

    def test_invalid_ids_limits_and_oversized_window_rejected(self):
        with self.assertRaises(source.SqlSourceError):
            self.make_source(page_size=2001)
        adapter = self.make_source()
        with self.assertRaises(source.SqlSourceError):
            adapter.lineages(["1'); DELETE FROM data;"])
        with self.assertRaises(source.SqlSourceError):
            adapter.violations("2026-09-01", "2026-10-23")

    def test_utc_window_is_converted_to_beijing_before_sql(self):
        def responder(sql):
            if "GROUP BY" in sql:
                return [{"delete_status": 0}]
            self.assertIn(source._quote("2026-10-22 08:00:00.000000"), sql)
            return []
        self.run_source(lambda db: db.violations("2026-10-22T00:00:00+00:00", "2026-10-22T01:00:00+00:00"), responder)


if __name__ == "__main__":
    unittest.main()

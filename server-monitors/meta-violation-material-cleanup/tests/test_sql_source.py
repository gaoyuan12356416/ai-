"""No database connection: protocol, pagination and credential routing tests."""
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
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
        self.assertNotIn("--no-defaults", command)  # FIFO prepends its own options.
        self.assertIn("--protocol=TCP", command)
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

    def test_batch_credentials_use_one_session_and_at_most_200_primary_ids(self):
        routes = [self.credential_route(id=str(n), ad_id=str(10000 + n)) for n in range(1, 402)]
        by_id = {row['id']: row for row in routes}
        batches = []
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                self.assertIn('a FORCE INDEX(PRIMARY)', sql)
                clause = re.search(r'WHERE a.id IN \(([^)]+)\)', sql).group(1)
                ids = [bytes.fromhex(value).decode() for value in re.findall(r'0x([0-9a-f]+)', clause)]
                batches.append(ids)
                self.assertLessEqual(len(ids), 200)
                return [by_id[value] for value in ids]
            self.assertIn('EXISTS(SELECT 1', sql)
            self.assertNotIn(source._quote('token'), sql)
            return [{'user_id': '804', 'available': 1}]
        result, adapter, process, popen = self.run_source(
            lambda db: db.selected_credentials(routes), responder)
        self.assertEqual(len(result), 401)
        self.assertTrue(all(value == {'user_id': '804', 'error': ''} for value in result.values()))
        self.assertEqual([len(values) for values in batches], [200, 200, 1])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(adapter.safe_metrics['sessions'], 1)
        self.assertEqual(len(process.commands), 6)  # read-only, transaction, 3 routes, 1 existence

    def test_batch_credentials_isolate_route_errors_without_partial_success_for_bad_rows(self):
        first = self.credential_route(id='10')
        queue_user = self.credential_route('-1', id='11')
        mismatched = self.credential_route(id='12')
        unavailable = self.credential_route(id='13')
        missing = self.credential_route(id='14')
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                return [first, queue_user, dict(mismatched, queue_product='3302'),
                        dict(unavailable, default_user='805')]
            return [{'user_id': '803', 'available': 1}, {'user_id': '804', 'available': 1},
                    {'user_id': '805', 'available': 0}]
        result, _, _, _ = self.run_source(
            lambda db: db.selected_credentials([first, queue_user, mismatched, unavailable, missing]), responder)
        self.assertEqual(result['10'], {'user_id': '804', 'error': ''})
        self.assertEqual(result['11'], {'user_id': '803', 'error': ''})
        self.assertEqual(result['12']['error'], 'credential_queue_mismatch')
        self.assertEqual(result['13'], {'user_id': '', 'error': 'publishing_credential_unavailable'})
        self.assertEqual(result['14']['error'], 'credential_lineage_changed')

    def test_batch_token_existence_queries_are_also_bounded_to_200_identities(self):
        routes = [self.credential_route(id=str(n), default_user=str(1000 + n)) for n in range(1, 202)]
        by_id = {row['id']: row for row in routes}
        sizes = []
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                clause = re.search(r'WHERE a.id IN \(([^)]+)\)', sql).group(1)
                ids = [bytes.fromhex(value).decode() for value in re.findall(r'0x([0-9a-f]+)', clause)]
                return [by_id[value] for value in ids]
            users = [bytes.fromhex(value).decode() for value in re.findall(
                r'SELECT _utf8mb4 0x([0-9a-f]+) AS user_id', sql)]
            sizes.append(len(users))
            self.assertLessEqual(len(users), 200)
            return [{'user_id': user, 'available': 1} for user in users]
        result, _, _, popen = self.run_source(lambda db: db.selected_credentials(routes), responder)
        self.assertEqual(len(result), 201)
        self.assertEqual(result['201']['user_id'], '1201')
        self.assertEqual(sizes, [200, 1])
        self.assertEqual(popen.call_count, 1)

    def test_batch_duplicate_input_conflict_and_bad_identity_fail_closed_per_row(self):
        first = self.credential_route(id='10')
        conflict = dict(first, product='3302')
        invalid = self.credential_route(id='11', user_id='0')
        changed_row = self.credential_route(id='12', row_id='99')
        result, _, _, popen = self.run_source(
            lambda db: db.selected_credentials([first, conflict, first, invalid, changed_row]),
            lambda sql: self.fail('no valid route should query'))
        self.assertEqual(result['10']['error'], 'ambiguous_credential_lineage')
        self.assertEqual(result['11']['error'], 'invalid_source_identifier')
        self.assertEqual(result['12']['error'], 'credential_lineage_changed')
        popen.assert_not_called()

    def test_batch_duplicate_identical_input_deduplicates_and_accepts_row_id_alias(self):
        row = self.credential_route()
        alias = dict(row, row_id=row['id'])
        del alias['id']
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                return [row]
            return [{'user_id': '804', 'available': 1}]
        result, _, _, _ = self.run_source(lambda db: db.selected_credentials([row, alias]), responder)
        self.assertEqual(result, {'10': {'user_id': '804', 'error': ''}})

    def test_batch_duplicate_or_unrequested_sql_route_rejects_whole_call(self):
        route = self.credential_route()
        for rows in ([route, route], [dict(route, id='99')]):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(source.SqlSourceError, 'invalid_credential_route_result'):
                    self.run_source(lambda db: db.selected_credentials([route]), lambda sql: rows)

    def test_batch_availability_missing_duplicate_or_malformed_fails_whole_call(self):
        route = self.credential_route()
        for values in ([], [{'user_id': '804', 'available': True}],
                       [{'user_id': '804', 'available': 1}, {'user_id': '804', 'available': 1}],
                       [{'user_id': '999', 'available': 1}],
                       [{'user_id': '804', 'available': 1, 'token': 'must-not-return'}]):
            with self.subTest(values=values):
                def responder(sql):
                    return [route] if 'ads_template_make_queue' in sql else values
                with self.assertRaises(source.SqlSourceError) as error:
                    self.run_source(lambda db: db.selected_credentials([route]), responder)
                self.assertNotIn('must-not-return', str(error.exception))

    def test_batch_unknown_rule_does_not_fetch_any_credential(self):
        route = self.credential_route(default_token='0')
        def responder(sql):
            self.assertIn('ads_template_make_queue', sql)
            return [route]
        result, _, _, _ = self.run_source(lambda db: db.selected_credentials([route]), responder)
        self.assertEqual(result['10'], {'user_id': '', 'error': 'credential_rule_unknown'})

    def test_batch_empty_and_unkeyable_input_do_not_launch_mysql(self):
        adapter = self.make_source()
        with patch.object(source.subprocess, 'Popen') as popen:
            self.assertEqual(adapter.selected_credentials([]), {})
            with self.assertRaisesRegex(source.SqlSourceError, 'invalid_credential_row_id'):
                adapter.selected_credentials([{}])
            with self.assertRaisesRegex(source.SqlSourceError, 'invalid_credential_lineage'):
                adapter.selected_credentials([None])
        popen.assert_not_called()

    def test_batch_result_is_not_cached_and_single_preflight_remains_fresh(self):
        routes = [self.credential_route(default_user='804'), self.credential_route(default_user='805')]
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                return [routes.pop(0)]
            if 'EXISTS(SELECT 1' in sql:
                return [{'user_id': '804', 'available': 1}]
            self.assertIn('user_id=' + source._quote('805'), sql)
            return [{'token': 'new-synthetic'}]
        adapter = self.make_source()
        with patch.object(source.subprocess, 'Popen', side_effect=[FakeMysql(responder), FakeMysql(responder)]) as popen:
            self.assertEqual(adapter.selected_credentials([self.credential_route()])['10']['user_id'], '804')
            self.assertEqual(adapter.selected_credential(self.credential_route()), '805')
        self.assertEqual(popen.call_count, 2)

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

    def test_nested_snapshot_uses_one_connection_and_original_deadline(self):
        process = FakeMysql(lambda sql: [])
        adapter = self.make_source()
        with patch.object(source.subprocess, "Popen", return_value=process) as popen:
            with adapter.snapshot() as outer:
                self.assertIs(outer, adapter)
                session, started = adapter._active, adapter._active.started
                adapter.scope_products()
                with adapter.snapshot() as inner:
                    self.assertIs(inner, adapter)
                    adapter.lineages(["123"])
                    self.assertIs(adapter._active, session)
                    self.assertEqual(adapter._active.started, started)
                self.assertFalse(session.closed)
            self.assertTrue(session.closed)
            self.assertIsNone(adapter._active)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(adapter.safe_metrics["sessions"], 1)
        self.assertEqual(sum("START TRANSACTION" in sql for sql in process.commands), 1)

    def test_scope_products_reads_only_root_ids_and_direct_children(self):
        def responder(sql):
            self.assertIn("WHERE id IN (1479,3302) OR ads_app_id IN (1479,3302)", sql)
            self.assertIn("ads_apps_setting", sql)
            self.assertNotIn("LIKE", sql)
            return [{"id": "1479", "name": "DramaWave", "ads_app_id": "0", "default_user": "803"},
                    {"id": "1723", "name": "W2A", "ads_app_id": "1479", "default_user": "804"}]
        result, _, _, _ = self.run_source(lambda db: db.scope_products(), responder)
        self.assertEqual([row["id"] for row in result], ["1479", "1723"])
        self.assertEqual(result[1]["default_user"], "804")

    def test_nested_query_error_poison_closes_snapshot_until_outer_exit(self):
        failed = FakeMysql(lambda sql: None)
        fresh = FakeMysql(lambda sql: [])
        adapter = self.make_source()
        with patch.object(source.subprocess, "Popen", side_effect=[failed, fresh]) as popen:
            with adapter.snapshot():
                session = adapter._active
                with self.assertRaisesRegex(source.SqlSourceError, "sql_stream_incomplete"):
                    adapter.sources(["123"])
                self.assertTrue(session.closed)
                with self.assertRaisesRegex(source.SqlSourceError, "sql_snapshot_unavailable"):
                    adapter.products()
                self.assertEqual(popen.call_count, 1)
            self.assertIsNone(adapter._active)
            self.assertEqual(adapter.products(), [])
            self.assertEqual(popen.call_count, 2)

    def test_snapshot_closes_on_user_exception_without_opening_another_session(self):
        process = FakeMysql(lambda sql: [])
        adapter = self.make_source()
        with patch.object(source.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaisesRegex(ValueError, "synthetic_abort"):
                with adapter.snapshot():
                    active = adapter._active
                    adapter.scope_products()
                    raise ValueError("synthetic_abort")
        self.assertTrue(active.closed)
        self.assertIsNone(adapter._active)
        self.assertEqual(popen.call_count, 1)

    def test_another_thread_waits_then_opens_its_own_session(self):
        first, second = FakeMysql(lambda sql: []), FakeMysql(lambda sql: [])
        adapter = self.make_source()
        started, done = threading.Event(), threading.Event()
        failures = []
        def worker():
            started.set()
            try:
                adapter.scope_products()
            except Exception as exc:
                failures.append(type(exc).__name__)
            finally:
                done.set()
        with patch.object(source.subprocess, "Popen", side_effect=[first, second]) as popen:
            with adapter.snapshot():
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(done.wait(0.05))
                self.assertEqual(popen.call_count, 1)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(popen.call_count, 2)
        self.assertEqual(failures, [])
        self.assertEqual(sum("START TRANSACTION" in sql for sql in second.commands), 1)

    def test_different_adapter_cannot_open_second_connection_in_same_thread(self):
        process = FakeMysql(lambda sql: [])
        first, second = self.make_source(), self.make_source()
        with patch.object(source.subprocess, "Popen", return_value=process) as popen:
            with first.snapshot():
                with self.assertRaisesRegex(source.SqlSourceError, "another_source_snapshot_active"):
                    second.scope_products()
                first.scope_products()
        self.assertEqual(popen.call_count, 1)

    def test_credentials_are_not_cached_across_or_inside_snapshots(self):
        counter = [0]
        def responder(sql):
            counter[0] += 1
            return [{"id": "10", "user_id": "803", "token": "synthetic-%d" % counter[0]}]
        adapter = self.make_source()
        processes = [FakeMysql(responder), FakeMysql(responder)]
        with patch.object(source.subprocess, "Popen", side_effect=processes) as popen:
            with adapter.snapshot():
                self.assertEqual(adapter.credentials()["803"], "synthetic-1")
                self.assertEqual(adapter.credentials()["803"], "synthetic-2")
            with adapter.snapshot():
                self.assertEqual(adapter.credentials()["803"], "synthetic-3")
        self.assertEqual(popen.call_count, 2)

    def test_expired_outer_snapshot_does_not_send_another_query(self):
        process = FakeMysql(lambda sql: [])
        adapter = self.make_source(session_timeout_seconds=60)
        with patch.object(source.subprocess, "Popen", return_value=process):
            with adapter.snapshot():
                before = len(process.commands)
                adapter._active.started -= 61
                with self.assertRaisesRegex(source.SqlSourceError, "sql_query_timeout"):
                    adapter.scope_products()
                self.assertEqual(len(process.commands), before)
                self.assertTrue(adapter._active.closed)


if __name__ == "__main__":
    unittest.main()

"""No database connection: protocol, pagination and credential routing tests."""
import ast
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
    def test_error_codes_only_expose_fixed_internal_literals(self):
        from meta_violation_cleanup.runner import safe_error
        for code in source.SqlSourceError._CODES:
            error = source.SqlSourceError(code)
            self.assertEqual(error.code, code)
            self.assertEqual(str(error), code)
            self.assertEqual(safe_error(error), {"type": "SqlSourceError", "code": code})
        for unsafe in ("access_token_synthetic_secret", "SELECT token FROM secrets",
                       "synthetic-secret", None, {"password": "synthetic-secret"}):
            error = source.SqlSourceError(unsafe)
            self.assertEqual(error.code, "sql_source_error")
            self.assertEqual(str(error), "sql_source_error")
            self.assertNotIn("synthetic", repr(error))
            self.assertEqual(safe_error(error), {"type": "SqlSourceError", "code": "sql_source_error"})

    def test_all_adapter_error_sites_use_allowlisted_literals(self):
        tree = ast.parse(Path(source.__file__).read_text(encoding="utf-8"))
        sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "SqlSourceError"]
        self.assertTrue(sites)
        for node in sites:
            self.assertEqual(len(node.args), 1)
            self.assertIsInstance(node.args[0], ast.Constant)
            self.assertIn(node.args[0].value, source.SqlSourceError._CODES)

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

    def test_protocol_compression_is_requested_without_changing_gate_or_endpoint(self):
        enabled=self.make_source()
        disabled=self.make_source(mysql_compress=False)
        self.assertIn('--compress',enabled._command)
        self.assertNotIn('--compress',disabled._command)
        self.assertEqual([part for part in enabled._command if part!='--compress'],disabled._command)
        for invalid in ('true',1,None):
            with self.subTest(invalid=invalid),self.assertRaises(source.SqlSourceError):
                self.make_source(mysql_compress=invalid)

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

    def test_lineage_batches_maximum_1900_and_preserves_duplicate_ad_rows(self):
        calls = []
        def responder(sql):
            calls.append(sql)
            if len(calls) == 1:
                return [{"id": "1", "ad_id": "1", "product": "1479"},
                        {"id": "2", "ad_id": "1", "product": "3302"}]
            return []
        result, _, _, _ = self.run_source(lambda db: db.lineages(map(str, range(1, 3802))), responder)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["1"]), 2)
        self.assertEqual(result["1"][1]["row_id"], "2")
        self.assertTrue(all("WHERE ad_id IN" in sql and "LIKE" not in sql for sql in calls))
        self.assertTrue(all("FORCE INDEX(ad_id)" in sql and "LIMIT 2000" in sql for sql in calls))
        self.assertEqual([len(re.findall(r'0x[0-9a-f]+', re.search(r'ad_id IN \(([^)]+)\)', sql).group(1)))
                          for sql in calls], [1900, 1900, 1])

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
            self.assertIn("ORDER BY id DESC LIMIT 2000", sql)
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

    def test_batch_credentials_use_one_session_and_at_most_1000_primary_ids(self):
        routes = [self.credential_route(id=str(n), ad_id=str(10000 + n)) for n in range(1, 2002)]
        by_id = {row['id']: row for row in routes}
        batches = []
        def responder(sql):
            if 'ads_template_make_queue' in sql:
                self.assertIn('a FORCE INDEX(PRIMARY)', sql)
                clause = re.search(r'WHERE a.id IN \(([^)]+)\)', sql).group(1)
                ids = [bytes.fromhex(value).decode() for value in re.findall(r'0x([0-9a-f]+)', clause)]
                batches.append(ids)
                self.assertLessEqual(len(ids), 1000)
                return [by_id[value] for value in ids]
            self.assertIn('EXISTS(SELECT 1', sql)
            self.assertNotIn(source._quote('token'), sql)
            return [{'user_id': '804', 'available': 1}]
        result, adapter, process, popen = self.run_source(
            lambda db: db.selected_credentials(routes), responder)
        self.assertEqual(len(result), 2001)
        self.assertTrue(all(value == {'user_id': '804', 'error': ''} for value in result.values()))
        self.assertEqual([len(values) for values in batches], [1000, 1000, 1])
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
        for values in ([],
                       [{'user_id': '804', 'available': 1}, {'user_id': '804', 'available': 1}],
                       [{'user_id': '999', 'available': 1}],
                       [{'user_id': '804', 'available': 1, 'token': 'must-not-return'}]):
            with self.subTest(values=values):
                def responder(sql):
                    return [route] if 'ads_template_make_queue' in sql else values
                with self.assertRaises(source.SqlSourceError) as error:
                    self.run_source(lambda db: db.selected_credentials([route]), responder)
                self.assertNotIn('must-not-return', str(error.exception))

    def test_batch_availability_accepts_live_mysql_json_boolean_and_integer_only(self):
        route = self.credential_route()
        for available in (True, False, 1, 0):
            with self.subTest(available=available):
                def responder(sql):
                    return [route] if 'ads_template_make_queue' in sql else [
                        {'user_id': '804', 'available': available}]
                result, _, _, _ = self.run_source(lambda db: db.selected_credentials([route]), responder)
                expected = ({'user_id': '804', 'error': ''} if available else
                            {'user_id': '', 'error': 'publishing_credential_unavailable'})
                self.assertEqual(result['10'], expected)
        for available in ('true', 'false', '1', '0', '', None, 1.0, 0.0, -1, 2, [], {}):
            with self.subTest(available=available):
                def responder(sql):
                    return [route] if 'ads_template_make_queue' in sql else [
                        {'user_id': '804', 'available': available}]
                with self.assertRaisesRegex(source.SqlSourceError, 'invalid_credential_availability'):
                    self.run_source(lambda db: db.selected_credentials([route]), responder)

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
            self.assertIn("WHERE id>0 AND (id IN (1479,3302) OR ads_app_id IN (1479,3302))", sql)
            self.assertIn("ads_apps_setting", sql)
            self.assertNotIn("LIKE", sql)
            return [{"id": "1479", "name": "DramaWave", "ads_app_id": "0", "default_user": "803"},
                    {"id": "1723", "name": "W2A", "ads_app_id": "1479", "default_user": "804"}]
        result, _, _, _ = self.run_source(lambda db: db.scope_products(), responder)
        self.assertEqual([row["id"] for row in result], ["1479", "1723"])
        self.assertEqual(result[1]["default_user"], "804")

    def test_product_directory_pages_3550_rows_by_primary_id(self):
        calls = []
        def responder(sql):
            self.assertIn('FORCE INDEX(PRIMARY)', sql)
            self.assertIn('ORDER BY id LIMIT 2000', sql)
            cursor = int(re.search(r'WHERE id>(\d+)', sql).group(1))
            calls.append(cursor)
            return [{'id': str(n)} for n in range(cursor + 1, min(cursor + 2001, 3551))]
        result, _, _, popen = self.run_source(lambda db: db.products(), responder)
        self.assertEqual(len(result), 3550)
        self.assertEqual(calls, [0, 2000])
        self.assertEqual(popen.call_count, 1)

    def test_product_scope_keeps_or_predicate_grouped_after_page_boundary(self):
        pages = iter([[{'id': '1479'}, {'id': '1723'}], [{'id': '3302'}]])
        calls = []
        def responder(sql):
            calls.append(sql)
            self.assertIn('AND (id IN (1479,3302) OR ads_app_id IN (1479,3302))', sql)
            return next(pages)
        result, _, _, _ = self.run_source(lambda db: db.scope_products(), responder, page_size=2)
        self.assertEqual([r['id'] for r in result], ['1479', '1723', '3302'])
        self.assertIn('WHERE id>1723 AND (', calls[1])

    def test_lineage_page_boundary_keeps_all_rows_for_one_ad(self):
        calls = []
        def responder(sql):
            self.assertIn('FORCE INDEX(ad_id)', sql)
            self.assertIn('ORDER BY ad_id,id LIMIT 2000', sql)
            match = re.search(r'AND id>(\d+)', sql)
            cursor = int(match.group(1)) if match else 0
            calls.append(cursor)
            return [{'id': str(n), 'ad_id': '10'}
                    for n in range(cursor + 1, min(cursor + 2001, 21002))]
        result, _, _, popen = self.run_source(lambda db: db.lineages(['10']), responder)
        self.assertEqual(len(result['10']), 21001)
        self.assertEqual(len(calls), 11)
        self.assertEqual(calls[1], 2000)
        self.assertEqual(result['10'][-1]['row_id'], '21001')
        self.assertEqual(popen.call_count, 1)

    def test_lineage_keyset_accepts_database_ad_order_and_rejects_repeated_pages(self):
        pages = iter([[{'id': '1', 'ad_id': '10'}, {'id': '2', 'ad_id': '10'}],
                      [{'id': '3', 'ad_id': '2'}]])
        calls = []
        def responder(sql):
            calls.append(sql)
            return next(pages)
        result, _, _, _ = self.run_source(lambda db: db.lineages(['2', '10']), responder, page_size=2)
        self.assertEqual(len(result['10']), 2)
        self.assertEqual(len(result['2']), 1)
        self.assertIn('ad_id>' + source._quote('10'), calls[1])
        self.assertIn('AND id>2', calls[1])
        for pages in (
                [[{'id': '1', 'ad_id': '10'}, {'id': '2', 'ad_id': '10'}], [{'id': '2', 'ad_id': '10'}]],
                [[{'id': '1', 'ad_id': '10'}, {'id': '2', 'ad_id': '2'}], [{'id': '3', 'ad_id': '10'}]],
                [[{'id': '2', 'ad_id': '10'}, {'id': '1', 'ad_id': '10'}]]):
            iterator = iter(pages)
            with self.assertRaisesRegex(source.SqlSourceError, 'ad_lineage_incomplete'):
                self.run_source(lambda db: db.lineages(['2', '10']), lambda sql: next(iterator), page_size=2)

    def test_directory_and_lineage_page_overflow_fail_without_partial_result(self):
        cases = ((lambda db: db.products(), [{'id': '1'}, {'id': '2'}, {'id': '3'}]),
                 (lambda db: db.credentials(), [{'id': str(n), 'user_id': '803', 'token': 'synthetic'}
                                               for n in (3, 2, 1)]),
                 (lambda db: db.lineages(['10']), [{'id': str(n), 'ad_id': '10'} for n in (1, 2, 3)]))
        for method, rows in cases:
            with self.assertRaises(source.SqlSourceError):
                self.run_source(method, lambda sql: rows, page_size=2)

    def test_credential_directory_pages_descending_and_keeps_latest_user_token(self):
        calls = []
        def responder(sql):
            self.assertIn('FORCE INDEX(PRIMARY)', sql)
            self.assertIn('ORDER BY id DESC LIMIT 2000', sql)
            match = re.search(r'AND id<(\d+)', sql)
            cursor = int(match.group(1)) if match else 2002
            calls.append(cursor)
            return [{'id': str(n), 'user_id': '803' if n in (2001, 1) else '804',
                     'token': 'synthetic-%d' % n}
                    for n in range(cursor - 1, max(cursor - 2001, 0), -1)]
        result, adapter, _, popen = self.run_source(lambda db: db.credentials(), responder)
        self.assertEqual(calls, [2002, 2])
        self.assertEqual(result, {'803': 'synthetic-2001', '804': 'synthetic-2000'})
        self.assertNotIn('synthetic', str(adapter.safe_metrics))
        self.assertEqual(popen.call_count, 1)

    def test_credential_directory_repeated_or_out_of_order_page_fails_closed(self):
        for ids in ((1, 2), (2, 2)):
            with self.assertRaisesRegex(source.SqlSourceError, 'credential_directory_incomplete'):
                self.run_source(lambda db: db.credentials(), lambda sql: [
                    {'id': str(n), 'user_id': '803', 'token': 'synthetic'} for n in ids])

    def test_directory_global_caps_still_fail_instead_of_truncating(self):
        def products(sql):
            cursor = int(re.search(r'WHERE id>(\d+)', sql).group(1))
            return [{'id': str(n)} for n in range(cursor + 1, cursor + 2001)]
        with self.assertRaisesRegex(source.SqlSourceError, 'product_directory_incomplete'):
            self.run_source(lambda db: db.products(), products)
        def credentials(sql):
            match = re.search(r'AND id<(\d+)', sql)
            cursor = int(match.group(1)) if match else 12001
            return [{'id': str(n), 'user_id': '803', 'token': 'synthetic'}
                    for n in range(cursor - 1, max(cursor - 2001, 0), -1)]
        with self.assertRaisesRegex(source.SqlSourceError, 'credential_directory_incomplete'):
            self.run_source(lambda db: db.credentials(), credentials)

    def test_sources_use_at_most_1000_primary_ids_and_preserve_all_membership(self):
        sizes = []
        def responder(sql):
            self.assertIn('FORCE INDEX(PRIMARY)', sql)
            clause = re.search(r'WHERE id IN \(([^)]+)\)', sql).group(1)
            ids = [bytes.fromhex(value).decode() for value in re.findall(r'0x([0-9a-f]+)', clause)]
            sizes.append(len(ids))
            self.assertLessEqual(int(re.search(r'LIMIT (\d+)', sql).group(1)), 2000)
            return [{'id': identifier, 'source_type': 3, 'source_id': identifier} for identifier in ids]
        result, _, _, popen = self.run_source(lambda db: db.sources(map(str, range(1, 1002))), responder)
        self.assertEqual(len(result), 1001)
        self.assertEqual(sizes, [1000, 1])
        self.assertEqual(popen.call_count, 1)

    def test_config_accepts_1900_ids_but_never_over_2000_rows_per_page(self):
        self.assertEqual(self.make_source().lineage_batch_size, 1900)
        self.assertEqual(self.make_source(lineage_batch_size=1000).lineage_batch_size, 1000)
        self.assertEqual(self.make_source(lineage_batch_size=200).lineage_batch_size, 200)
        for config in ({'lineage_batch_size': 1901}, {'page_size': 2001}):
            with self.assertRaisesRegex(source.SqlSourceError, 'unsafe_sql_limits'):
                self.make_source(**config)

    def context_row(self, **changes):
        row = self.credential_route(id='10', ad_id='100', product='1723', queue_product='1723',
                                   setting_id='1723', setting_parent='1479', parent_setting_id='1479',
                                   creative_id='5000', source_id='900', original_source_id='9010',
                                   ad_account_id='7000', available=True)
        row.update(changes)
        return row

    def context_responder(self, rows, source_rows=None):
        if source_rows is None:
            source_rows = [{'id': '900', 'source_type': 3, 'source_id': '9010'}]
        def responder(sql):
            if 'ads_source FORCE INDEX(PRIMARY)' in sql:
                return source_rows
            self.assertIn('a FORCE INDEX(ad_id)', sql)
            self.assertIn('LEFT JOIN', sql)
            self.assertNotIn('INNER JOIN', sql)
            self.assertIn('parent.id=p.ads_app_id', sql)
            self.assertIn('EXISTS(SELECT 1', sql)
            self.assertNotIn(source._quote('token'), sql)
            self.assertNotIn('ads_facebook_violations', sql)
            return rows
        return responder

    def test_current_context_uses_two_business_reads_in_one_fresh_snapshot(self):
        result, adapter, process, popen = self.run_source(
            lambda db: db.current_ad_context('100', '5000'), self.context_responder([self.context_row()]))
        self.assertEqual(result['scope'], {'1723': '1479'})
        self.assertEqual(result['credential_users'], ['804'])
        self.assertEqual(result['sources']['900']['source_id'], '9010')
        self.assertEqual(result['rows'][0]['row_id'], '10')
        self.assertEqual(len(process.commands), 4)  # proof, transaction, joined lineage, source PK
        self.assertEqual(adapter.safe_metrics['sessions'], 1)
        self.assertEqual(popen.call_count, 1)

    def test_current_context_preserves_other_creatives_without_validating_old_route(self):
        old = self.context_row(id='11', creative_id='6000', product='669', setting_id=None,
                               queue_id=None, source_id='unusable-history', available=False)
        result, _, _, _ = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                          self.context_responder([self.context_row(), old]))
        self.assertEqual(len(result['rows']), 2)
        self.assertIsNone(result['rows'][1]['setting_id'])
        self.assertEqual(result['scope'], {'1723': '1479'})
        self.assertEqual(set(result['sources']), {'900'})

    def test_current_context_no_matching_creative_returns_no_identity_evidence(self):
        result, _, process, _ = self.run_source(lambda db: db.current_ad_context('100', '6000'),
                                               self.context_responder([self.context_row()]))
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['sources'], {})
        self.assertEqual(result['scope'], {})
        self.assertEqual(result['credential_users'], [])
        self.assertEqual(len(process.commands), 3)

    def test_current_context_does_not_filter_new_product_lineage_to_frozen_product(self):
        changed = self.context_row(id='11', product='3303', queue_product='3303', setting_id='3303',
                                   setting_parent='3302', parent_setting_id='3302')
        result, _, _, _ = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                          self.context_responder([self.context_row(), changed]))
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['scope'], {'1723': '1479', '3303': '3302'})

    def test_current_context_root_product_requires_existing_exact_setting(self):
        for root in ('1479', '3302'):
            row = self.context_row(product=root, queue_product=root, setting_id=root,
                                   setting_parent='0', parent_setting_id=None)
            result, _, _, _ = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                              self.context_responder([row]))
            self.assertEqual(result['scope'], {root: root})
            with self.assertRaisesRegex(source.SqlSourceError, 'current_product_unavailable'):
                self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                self.context_responder([dict(row, setting_id=None)]))

    def test_current_context_missing_child_parent_queue_or_token_fail_closed(self):
        cases = (({'parent_setting_id': None}, 'current_product_unavailable'),
                 ({'setting_id': '3302'}, 'current_product_unavailable'),
                 ({'queue_id': None}, 'credential_queue_mismatch'),
                 ({'queue_product': '3302'}, 'credential_queue_mismatch'),
                 ({'queue_user_id': '805'}, 'credential_queue_mismatch'),
                 ({'default_token': '0'}, 'credential_rule_unknown'),
                 ({'available': False}, 'publishing_credential_unavailable'),
                 ({'available': 'true'}, 'invalid_credential_availability'))
        for changes, code in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(source.SqlSourceError, code):
                    self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                    self.context_responder([self.context_row(**changes)]))

    def test_current_context_queue_user_and_conflicting_current_users_are_preserved(self):
        row = self.context_row(default_token='-1')
        second = self.context_row(id='11')
        result, _, _, _ = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                          self.context_responder([row, second]))
        self.assertEqual(result['credential_users'], ['803', '804'])

    def test_current_context_missing_or_malformed_current_sources_fail_closed(self):
        for rows, sources in (([self.context_row()], []),
                              ([self.context_row(source_id='not-an-id')], [])):
            with self.assertRaisesRegex(source.SqlSourceError, 'invalid_source_mapping'):
                self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                self.context_responder(rows, sources))
        result, _, process, _ = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                               self.context_responder([self.context_row(source_id='0')], []))
        self.assertEqual(result['sources'], {})
        self.assertEqual(len(process.commands), 3)

    def test_current_context_reads_all_lineage_pages_and_never_truncates(self):
        pages = iter([[self.context_row(id='1'), self.context_row(id='2')], [self.context_row(id='3')]])
        queries = []
        def responder(sql):
            if 'ads_source FORCE INDEX(PRIMARY)' in sql:
                return [{'id': '900', 'source_type': 3, 'source_id': '9010'}]
            queries.append(sql)
            return next(pages)
        result, _, _, popen = self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                              responder, page_size=2)
        self.assertEqual(len(result['rows']), 3)
        self.assertIn('AND a.id>2 ORDER BY a.id LIMIT 2', queries[1])
        self.assertEqual(popen.call_count, 1)
        for rows in ([self.context_row(id='2'), self.context_row(id='1')],
                     [self.context_row(id='1'), self.context_row(id='1')],
                     [self.context_row(ad_id='101')]):
            with self.assertRaises(source.SqlSourceError):
                self.run_source(lambda db: db.current_ad_context('100', '5000'),
                                self.context_responder(rows))

    def test_current_context_reuses_outer_snapshot_but_never_caches_current_values(self):
        counter = [0]
        def responder(sql):
            if 'ads_source FORCE INDEX(PRIMARY)' in sql:
                return [{'id': '900', 'source_type': 3, 'source_id': '9010'}]
            counter[0] += 1
            return [self.context_row(default_user=str(803 + counter[0]))]
        adapter = self.make_source()
        process = FakeMysql(responder)
        with patch.object(source.subprocess, 'Popen', return_value=process) as popen:
            with adapter.snapshot():
                self.assertEqual(adapter.current_ad_context('100', '5000')['credential_users'], ['804'])
                self.assertEqual(adapter.current_ad_context('100', '5000')['credential_users'], ['805'])
        self.assertEqual(popen.call_count, 1)

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

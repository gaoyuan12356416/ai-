"""Offline regression tests; load only SQL helpers, never production credentials/Graph."""
import ast
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("fb_2446289_daily_replace_delete.py")


def load_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"mysql_failure_details", "mysql_query", "executor_mysql_query"}
    constants = {"MYSQL_RETRY_DELAYS", "MYSQL_RETRYABLE_CODES", "MYSQL_GATE_WAIT_SECONDS", "MYSQL_GATE_CLEANUP_GRACE_SECONDS"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "mod" and target.attr == "mysql_query" for target in node.targets):
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    env = {
        "csv": csv, "json": json, "os": os, "re": re,
        "subprocess": types.SimpleNamespace(run=mock.Mock(), PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired),
        "time": types.SimpleNamespace(sleep=mock.Mock()), "log": mock.Mock(),
        "mod": types.SimpleNamespace(MYSQL_HOST="read-host", MYSQL_USER="read-user", MYSQL_PASSWORD="private-password-for-test", DEV_PORT="63350"),
    }
    exec(compile(module, str(SOURCE), "exec"), env)
    return env


def result(code=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


GATE = 'SQL_GATE {"event":"granted","job":"fb2446289-daily","pool":"ads_aius-read","version":"1.1.0","wait_seconds":0.001}'
HANDSHAKE = "ERROR 2013 (HY000): Lost connection to MySQL server at 'reading authorization packet', system error: 11"


class MysqlRetryTests(unittest.TestCase):
    def setUp(self):
        self.env = load_helpers()
        self.run = self.env["subprocess"].run
        self.query = self.env["mysql_query"]

    def test_handshake_retry_recovers_and_discards_partial_output(self):
        self.run.side_effect = [result(1, "partial\tinvalid\n", GATE + "\n" + HANDSHAKE), result(0, "ok\t2\n", GATE)]
        self.assertEqual(self.query("63350", "SELECT ad_id FROM data"), [["ok", "2"]])
        self.assertEqual(self.run.call_count, 2)
        self.env["time"].sleep.assert_called_once_with(1)
        self.assertIn("code=2013", self.env["log"].call_args[0][0])

    def test_retry_exhausted_keeps_handshake_and_gate_diagnostics(self):
        self.run.return_value = result(1, "partial", GATE + "\n" + HANDSHAKE)
        with self.assertRaises(RuntimeError) as caught:
            self.query("63350", "SELECT 1")
        text = str(caught.exception)
        self.assertIn("ERROR 2013", text)
        self.assertIn("reading authorization packet", text)
        self.assertIn("SQL_GATE", text)
        self.assertIn("attempt=3/3", text)
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.env["time"].sleep.call_args_list, [mock.call(1), mock.call(3)])

    def test_all_transient_codes_retry(self):
        for code in (1040, 1203, 2002, 2003, 2006, 2013, 2055, 3024):
            with self.subTest(code=code):
                self.setUp()
                self.run.side_effect = [result(1, "", "ERROR %s (HY000): unavailable" % code), result(0, "1\n")]
                self.assertEqual(self.query("63350", "SELECT 1"), [["1"]])
                self.assertEqual(self.run.call_count, 2)

    def test_permanent_and_unknown_errors_are_not_retried(self):
        for stderr in ("ERROR 1045 (28000): access denied", "ERROR 1064 (42000): near 'private SQL token'", GATE, "wrapper failed"):
            with self.subTest(stderr=stderr):
                self.setUp()
                self.run.return_value = result(1, "", stderr)
                with self.assertRaises(RuntimeError) as caught:
                    self.query("63350", "SELECT 1")
                self.assertNotIn("private SQL token", str(caught.exception))
                self.assertEqual(self.run.call_count, 1)
                self.env["time"].sleep.assert_not_called()

    def test_timeout_is_not_retried_or_leaked(self):
        self.run.side_effect = subprocess.TimeoutExpired(["mysql", "SELECT private-token"], 7, output="private-output", stderr="private-password-for-test")
        with self.assertRaises(RuntimeError) as caught:
            self.query("63350", "SELECT private-token", timeout=7)
        self.assertIn("not retried", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.run.call_count, 1)
        self.env["time"].sleep.assert_not_called()

    def test_clean_gate_execution_timeout_retries_only_current_select(self):
        self.run.side_effect = [
            result(124, "partial\tinvalid\n", GATE + "\nSQL_GATE_EXECUTION_TIMEOUT\n"),
            result(0, "page\t42\n", GATE),
        ]
        self.assertEqual(self.query("63350", "SELECT ad_id, page_id FROM pages", timeout=60), [["page", "42"]])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args_list[0].args, self.run.call_args_list[1].args)
        self.env["time"].sleep.assert_called_once_with(1)
        self.assertIn("code=execution_timeout", self.env["log"].call_args.args[0])

    def test_clean_gate_timeout_exhausts_three_attempts(self):
        self.run.return_value = result(124, "private-partial", GATE + "\nSQL_GATE_EXECUTION_TIMEOUT\n")
        with self.assertRaises(RuntimeError) as caught:
            self.query("63350", "SELECT 1", timeout=60)
        self.assertIn("attempt=3/3", str(caught.exception))
        self.assertIn("SQL_GATE_EXECUTION_TIMEOUT", str(caught.exception))
        self.assertNotIn("private-partial", str(caught.exception))
        self.assertEqual(self.run.call_count, 3)

    def test_execution_timeout_with_cleanup_failure_is_not_retried(self):
        self.run.return_value = result(124, "", "SQL_GATE_EXECUTION_TIMEOUT\nSQL_GATE_CHILD_CLEANUP_FAILED\n")
        with self.assertRaises(RuntimeError):
            self.query("63350", "SELECT 1")
        self.assertEqual(self.run.call_count, 1)

    def test_ungated_timeout_does_not_claim_gate_cleanup(self):
        self.run.return_value = result(124, "", "SQL_GATE_EXECUTION_TIMEOUT\n")
        with self.assertRaises(RuntimeError):
            self.query("63348", "SELECT 1", timeout=240)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 240)
        self.assertNotIn("SQL_GATE_EXEC_TIMEOUT_SECONDS", self.run.call_args.kwargs["env"])

    def test_gate_safety_failures_override_transient_mysql_code(self):
        for exit_code, marker in ((75, ""), (124, ""), (125, ""), (1, "SQL_GATE_CHILD_CLEANUP_FAILED"), (1, "SQL_GATE_DAEMON_LOST")):
            with self.subTest(exit_code=exit_code, marker=marker):
                self.setUp()
                self.run.return_value = result(exit_code, "", HANDSHAKE + "\n" + marker)
                with self.assertRaises(RuntimeError) as caught:
                    self.query("63350", "SELECT 1")
                if marker:
                    self.assertIn(marker, str(caught.exception))
                self.assertEqual(self.run.call_count, 1)
                self.env["time"].sleep.assert_not_called()

    def test_credentials_are_environment_only_and_gate_retries_disabled(self):
        self.run.return_value = result(0, "1\n", GATE)
        with mock.patch.dict(os.environ, {"SQL_GATE_BYPASS": "1", "SQL_GATE_RETRY_DELAYS_SECONDS": "5,10"}):
            self.query("63350", "SELECT 1", timeout=12)
        args, kwargs = self.run.call_args
        self.assertEqual(args[0][0], "mysql")
        self.assertIn("--connect-timeout=5", args[0])
        self.assertNotIn("private-password-for-test", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], "private-password-for-test")
        self.assertEqual(kwargs["env"]["SQL_GATE_BYPASS"], "0")
        self.assertEqual(kwargs["env"]["SQL_GATE_RETRY_DELAYS_SECONDS"], "")
        self.assertEqual(kwargs["env"]["SQL_GATE_JOB_NAME"], "fb2446289-daily")
        self.assertEqual(kwargs["env"]["SQL_GATE_WAIT_TIMEOUT_SECONDS"], "300")
        self.assertEqual(kwargs["env"]["SQL_GATE_EXEC_TIMEOUT_SECONDS"], "12")
        self.assertEqual(kwargs["timeout"], 342)

    def test_sensitive_stderr_not_exposed(self):
        self.run.return_value = result(1, "private-result", HANDSHAKE + " password=private-password-for-test token=private-token SELECT secret FROM data")
        with self.assertRaises(RuntimeError) as caught:
            self.query("63350", "SELECT secret FROM data")
        text = str(caught.exception)
        self.assertNotIn("private-password-for-test", text)
        self.assertNotIn("private-token", text)
        self.assertNotIn("SELECT secret", text)
        self.assertNotIn("private-result", text)
        for call in self.env["log"].call_args_list:
            self.assertNotIn("private", call.args[0])

    def test_non_select_and_multi_statement_are_rejected_without_execution(self):
        for sql in ("UPDATE data SET status=1", "DELETE FROM data", "SELECT 1; DELETE FROM data"):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    self.query("63350", sql)
        self.run.assert_not_called()

    def test_executor_token_queries_use_same_helper(self):
        self.run.side_effect = [result(1, "", HANDSHAKE), result(0, "803\tprivate-token\n")]
        self.assertIs(self.env["mod"].mysql_query, self.env["executor_mysql_query"])
        self.assertEqual(self.env["mod"].mysql_query("SELECT user_id, accessToken FROM info", timeout=22), [["803", "private-token"]])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 352)
        self.assertIn("-P63350", self.run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

"""Exercise the exact app adapter without importing unrelated production workers."""
import ast
import os
from pathlib import Path
import unittest
from unittest.mock import Mock


class SqlAdapterTests(unittest.TestCase):
    def test_optional_stdin_preserves_existing_call_contract(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ad_control_run_mysql")
        subprocess = Mock()
        subprocess.run.return_value.stdout = "1\tvalue\n"
        scope = dict(os=os, subprocess=subprocess, MYSQL_PASSWORD="", MYSQL_BASE_CMD=["mysql", "-N", "-B", "-e"], AD_CONTROL_ACCOUNT_LIST_TIMEOUT_SECONDS=12)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "adapter", "exec"), scope)
        query = "SELECT 1"
        self.assertEqual([["1", "value"]], scope["ad_control_run_mysql"](query))
        args, kw = subprocess.run.call_args
        self.assertEqual(["mysql", "-N", "-B", "-e", query], args[0])
        self.assertIsNone(kw["input"])
        scope["ad_control_run_mysql"](query, timeout_seconds=180, via_stdin=True)
        args, kw = subprocess.run.call_args
        self.assertEqual(["mysql", "-N", "-B"], args[0])
        self.assertEqual(query + ";\n", kw["input"])
        self.assertEqual("180", kw["env"]["MYSQL_QUERY_TIMEOUT_SECONDS"])


if __name__ == "__main__":
    unittest.main()

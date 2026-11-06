"""Fixed replay window and dedupe checks without production imports or writes."""
import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("fb_2446289_daily_replace_delete.py")


def helpers():
    names = {"parse_window_end", "screening_window_sql", "screen_failed_ads", "sql_quote", "chunks", "normalize_account_id", "account_query_values", "build_execution_rows"}
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    env = {"argparse": argparse, "datetime": datetime, "json": json, "random": random,
           "log": mock.Mock(), "mysql_query": mock.Mock(), "write_csv": mock.Mock(),
           "write_json": mock.Mock(), "now_str": lambda: "2026-11-06 14:00:00",
           "mod": mock.Mock(DEV_PORT="63350"), "SEC_PORT": "63348", "MATCH_CONDITION": "test"}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), env)
    return env


class ScreeningWindowTests(unittest.TestCase):
    def test_default_window_is_unchanged(self):
        self.assertEqual(helpers()["screening_window_sql"](24), "created_at >= NOW() - INTERVAL 24 HOUR")

    def test_fixed_window_applies_to_candidates_and_replacements(self):
        env = helpers()
        env["mysql_query"].side_effect = [[["123", "456", "first", "last", "1"]], [], [], []]
        end = "2026-11-06 10:00:03"
        rows, metadata = env["screen_failed_ads"](24, Path("source.csv"), Path("source.json"), end)
        queries = [call.args[1] for call in env["mysql_query"].call_args_list if call.args[0] == "63348"]
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertIn("created_at >= '2026-11-06 10:00:03' - INTERVAL 24 HOUR", query)
            self.assertIn("created_at < '2026-11-06 10:00:03'", query)
            self.assertNotIn("NOW()", query)
        self.assertEqual(metadata["window_end"], end)
        self.assertEqual(rows[0]["ad_id"], "123")

    def test_invalid_window_rejected_before_query(self):
        env = helpers()
        for value in ("2026-11-31 10:00:00", "2026-11-06'; SELECT 1", "now"):
            with self.assertRaises(argparse.ArgumentTypeError):
                env["screen_failed_ads"](24, Path("source.csv"), Path("source.json"), value)
        env["mysql_query"].assert_not_called()

    def test_replay_keeps_original_success_ids_out_of_execution(self):
        env = helpers()
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.jsonl"
            source = [{"ad_id": "123", "account_id": "456"}]
            history = {"123", "older-success"}
            rows, skipped = env["build_execution_rows"](source, {}, history, result)
            self.assertEqual(rows, [])
            self.assertEqual(skipped, 1)
            self.assertEqual(history, {"123", "older-success"})
            self.assertEqual(json.loads(result.read_text())["final_status"], "skipped_success_history")


if __name__ == "__main__":
    unittest.main()

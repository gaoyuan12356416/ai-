#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import importlib.util
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "tt_minis_insight_level_consistency_monitor.py"


def load_module():
    base = types.ModuleType("opera_product_daily_dashboard")
    base.mysql_cmd = lambda: ["mysql", "-hreadonly", "-uads", "-psecret", "-N", "-B", "-e"]
    base.compact_sql = lambda sql: " ".join(sql.split())
    base.feishu_token = lambda: ("https://example.invalid", "token")
    base.post_json = lambda *args, **kwargs: {"code": 0, "data": {"message_id": "test"}}

    dash = types.ModuleType("tt_minis_multi_dim_dashboard")
    dash.TIKTOK_INSIGHT_PRODUCTS = ("dramawaveminis", "Dramawave")
    dash.sql_quote = lambda value: "'%s'" % str(value).replace("'", "''")
    dash.sql_in = lambda values, numeric=False: "(" + ",".join(
        str(value) if numeric else dash.sql_quote(value) for value in values
    ) + ")"

    sys.modules[base.__name__] = base
    sys.modules[dash.__name__] = dash
    spec = importlib.util.spec_from_file_location("tt_minis_consistency_monitor_test_target", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load_module()


def snapshot_payload(day, level, rows):
    return {
        "meta": {
            "metric_level": level,
            "start_date": day,
            "end_date": day,
            "row_count": len(rows),
        },
        "rows": rows,
    }


class PublishedSnapshotTest(unittest.TestCase):
    def write_snapshot(self, root, days):
        generated_at = monitor.bj_now().strftime("%Y-%m-%d %H:%M:%S")
        manifest = {"meta": {"generated_at": generated_at}, "data_files": {"campaign": {}, "ad": {}}}
        for day, levels in days.items():
            for level in ("campaign", "ad"):
                relative = "data/%s/%s.json" % (level, day)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = snapshot_payload(day, level, levels[level])
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                manifest["data_files"][level][day] = {"path": relative, "row_count": len(levels[level])}
        (root / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return generated_at

    def test_two_days_use_two_adgroup_queries_and_snapshot_for_other_levels(self):
        days = {
            "2026-08-09": {
                "campaign": [
                    {"campaign_id": "100", "adset_id": "campaign层级不可用", "ad_id": "campaign层级不可用", "row_count": 2, "spend": "10", "revenue": "5"},
                    {"campaign_id": "999", "adset_id": "campaign层级不可用", "ad_id": "campaign层级不可用", "row_count": 1, "spend": "999", "revenue": "999"},
                ],
                "ad": [
                    {"campaign_id": "100", "adset_id": "10", "ad_id": "1000", "row_count": 3, "spend": "10.1", "revenue": "5.1"},
                ],
            },
            "2026-08-10": {
                "campaign": [
                    {"campaign_id": "200", "adset_id": "campaign层级不可用", "ad_id": "campaign层级不可用", "row_count": 4, "spend": "20", "revenue": "8"},
                ],
                "ad": [
                    {"campaign_id": "200", "adset_id": "20", "ad_id": "2000", "row_count": 5, "spend": "20.2", "revenue": "8.2"},
                ],
            },
        }
        sql_calls = []

        def fake_run_mysql(sql, timeout=120):
            sql_calls.append((sql, timeout))
            if "2026-08-09" in sql:
                return [
                    ["10", "100", "0", "2", "10.05", "5.05"],
                    ["99", "999", "0", "1", "500", "500"],
                ]
            return [["20", "200", "0", "4", "20.1", "8.1"]]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            generated_at = self.write_snapshot(root, days)
            with mock.patch.object(monitor, "run_mysql_safe", side_effect=fake_run_mysql):
                result, actual_generated_at = monitor.fetch_days(
                    ["2026-08-09", "2026-08-10"],
                    report_root=root,
                    max_snapshot_age=timedelta(hours=2),
                )

        self.assertEqual(generated_at, actual_generated_at)
        self.assertEqual(2, len(sql_calls))
        self.assertTrue(all(timeout == 120 for _, timeout in sql_calls))
        self.assertTrue(all("category = 1" in sql for sql, _ in sql_calls))
        self.assertTrue(all("GROUP BY CAST(adgroup_id AS UNSIGNED)" in sql for sql, _ in sql_calls))
        self.assertLess(max(len(sql) for sql, _ in sql_calls), 2500)
        self.assertEqual(Decimal("10"), result["2026-08-09"]["campaign"]["spend"])
        self.assertEqual(1, result["2026-08-09"]["campaign"]["campaigns"])
        self.assertEqual(Decimal("10.05"), result["2026-08-09"]["adgroup"]["spend"])
        self.assertEqual(1, result["2026-08-09"]["adgroup"]["adgroups"])
        self.assertEqual(Decimal("10.1"), result["2026-08-09"]["ad"]["spend"])
        self.assertEqual(3, result["2026-08-09"]["ad"]["rows"])

    def test_manifest_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                monitor.resolve_snapshot_path(root, "../outside.json")

    def test_row_count_mismatch_is_rejected(self):
        day = "2026-08-10"
        rows = [{"campaign_id": "1", "adset_id": "1", "ad_id": "1", "row_count": 1, "spend": 1, "revenue": 1}]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_snapshot(root, {day: {"campaign": rows, "ad": rows}})
            manifest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            manifest["data_files"]["ad"][day]["row_count"] = 2
            (root / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "row count mismatch"):
                monitor.load_published_levels([day], report_root=root)


class AlertSemanticsTest(unittest.TestCase):
    def test_single_bad_level_and_recovery_semantics_are_unchanged(self):
        item = monitor.classify_metric(
            "2026-08-10",
            "revenue",
            {"campaign": Decimal("1000"), "adgroup": Decimal("1002"), "ad": Decimal("1200")},
            Decimal("100"),
            Decimal("0.05"),
        )
        self.assertEqual("ad", item["abnormal_level"])
        state = {"active": {}}
        alerts, recoveries = monitor.changed_alerts([item], state)
        self.assertEqual([item], alerts)
        self.assertEqual([], recoveries)
        alerts, recoveries = monitor.changed_alerts([], state)
        self.assertEqual([], alerts)
        self.assertEqual([item], recoveries)
        title, template = monitor.notification_presentation([], recoveries)
        self.assertIn("已恢复", title)
        self.assertEqual("green", template)

    def run_main_with(self, state_file, all_data, response):
        args = SimpleNamespace(
            date="2026-08-10",
            days=1,
            abs_threshold="100",
            pct_threshold="0.05",
            send=True,
            dry_run=False,
            dry_run_message=False,
            chat_id="oc_test",
            snapshot_root="/unused",
        )
        with mock.patch.object(monitor, "STATE_FILE", state_file), mock.patch.object(
            monitor, "parse_args", return_value=args
        ), mock.patch.object(
            monitor, "fetch_days", return_value=(all_data, "2026-08-10 16:00:00")
        ), mock.patch.object(
            monitor, "send_feishu_card", return_value=response
        ) as send, contextlib.redirect_stdout(io.StringIO()):
            result = monitor.main()
        return result, send

    def test_failed_alert_send_does_not_advance_state_and_next_run_retries(self):
        day = "2026-08-10"
        normal = {"rows": 1, "campaigns": 1, "adgroups": 1, "ads": 1, "spend": Decimal("1000"), "revenue": Decimal("1000")}
        abnormal = dict(normal, revenue=Decimal("1200"))
        all_data = {day: {"campaign": dict(normal), "adgroup": dict(normal), "ad": abnormal}}
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            original = {"active": {}, "marker": "unchanged"}
            state_file.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Feishu send failed"):
                self.run_main_with(state_file, all_data, {"code": 1})
            self.assertEqual(original, json.loads(state_file.read_text(encoding="utf-8")))

            result, send = self.run_main_with(
                state_file,
                all_data,
                {"code": 0, "data": {"message_id": "om_retry"}},
            )
            self.assertEqual(0, result)
            self.assertEqual(1, send.call_count)
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn(day + ":revenue", saved["active"])

    def test_failed_recovery_send_keeps_active_state_for_next_retry(self):
        day = "2026-08-10"
        active = monitor.classify_metric(
            day,
            "revenue",
            {"campaign": Decimal("1000"), "adgroup": Decimal("1000"), "ad": Decimal("1200")},
            Decimal("100"),
            Decimal("0.05"),
        )
        normal = {"rows": 1, "campaigns": 1, "adgroups": 1, "ads": 1, "spend": Decimal("1000"), "revenue": Decimal("1000")}
        all_data = {day: {"campaign": dict(normal), "adgroup": dict(normal), "ad": dict(normal)}}
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            original = {"active": {active["key"]: active}, "marker": "unchanged"}
            state_file.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Feishu send failed"):
                self.run_main_with(state_file, all_data, {"code": 1})
            self.assertEqual(original, json.loads(state_file.read_text(encoding="utf-8")))

            result, send = self.run_main_with(
                state_file,
                all_data,
                {"code": 0, "data": {"message_id": "om_recovery_retry"}},
            )
            self.assertEqual(0, result)
            self.assertEqual(1, send.call_count)
            self.assertEqual({}, json.loads(state_file.read_text(encoding="utf-8"))["active"])


class MysqlCredentialRedactionTest(unittest.TestCase):
    def test_password_moves_to_env_and_is_absent_from_argv(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="10\t100\t0\t1\t2\t3\n", stderr="")
        with mock.patch.object(
            monitor.base,
            "mysql_cmd",
            return_value=["mysql", "-hreadonly", "-uads", "-pSUPERSECRET", "-N", "-B", "-e"],
        ), mock.patch.object(monitor.subprocess, "run", return_value=completed) as run:
            rows = monitor.run_mysql_safe("SELECT 1", timeout=12)
        self.assertEqual([["10", "100", "0", "1", "2", "3"]], rows)
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertFalse(any("SUPERSECRET" in item or item.startswith("-p") for item in command))
        self.assertEqual("SUPERSECRET", env["MYSQL_PWD"])

    def test_timeout_exception_never_repeats_raw_command_or_password(self):
        timeout = subprocess.TimeoutExpired(cmd=["mysql", "-pSUPERSECRET", "SELECT 1"], timeout=120)
        with mock.patch.object(
            monitor.base,
            "mysql_cmd",
            return_value=["mysql", "-pSUPERSECRET", "-N", "-B", "-e"],
        ), mock.patch.object(monitor.subprocess, "run", side_effect=timeout):
            with self.assertRaises(RuntimeError) as caught:
                monitor.run_mysql_safe("SELECT 1", timeout=120)
        message = str(caught.exception)
        self.assertEqual("TT minis adgroup query timed out after 120 seconds", message)
        self.assertNotIn("SUPERSECRET", message)
        self.assertNotIn("-p", message)

    def test_long_password_flag_and_separate_value_are_removed_from_argv(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(
            monitor.base,
            "mysql_cmd",
            return_value=["mysql", "--password", "SUPERSECRET", "-N", "-B", "-e"],
        ), mock.patch.object(monitor.subprocess, "run", return_value=completed) as run:
            monitor.run_mysql_safe("SELECT 1")
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertNotIn("--password", command)
        self.assertNotIn("SUPERSECRET", command)
        self.assertEqual("SUPERSECRET", env["MYSQL_PWD"])

    def test_mysql_tsv_treats_quotes_as_data_not_csv_syntax(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='10\t100\t0\t1\t"2"\t3\n',
            stderr="",
        )
        with mock.patch.object(monitor.subprocess, "run", return_value=completed):
            rows = monitor.run_mysql_safe("SELECT 1")
        self.assertEqual([["10", "100", "0", "1", '"2"', "3"]], rows)

    def test_mysql_error_stderr_is_not_copied_to_exception(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="simulated failure mentioning SUPERSECRET",
        )
        with mock.patch.object(monitor.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as caught:
                monitor.run_mysql_safe("SELECT 1")
        self.assertEqual("TT minis adgroup query failed with mysql exit code 1", str(caught.exception))
        self.assertNotIn("SUPERSECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

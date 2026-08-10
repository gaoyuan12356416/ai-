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
    def write_snapshot(self, root, days, age=timedelta(0)):
        generated_at = (monitor.bj_now() - age).strftime("%Y-%m-%d %H:%M:%S")
        manifest = {"meta": {"generated_at": generated_at}, "data_files": {"ad": {}}}
        for day, rows in days.items():
            relative = "data/ad/%s.json" % day
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = snapshot_payload(day, "ad", rows)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            manifest["data_files"]["ad"][day] = {"path": relative, "row_count": len(rows)}
        (root / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return generated_at

    def test_two_days_use_two_same_statement_queries_and_ignore_stale_snapshot_metrics(self):
        days = {
            "2026-08-09": [
                {"campaign_id": "100", "adset_id": "10", "ad_id": "1000", "row_count": 999, "spend": "9999", "revenue": "1"},
            ],
            "2026-08-10": [
                {"campaign_id": "200", "adset_id": "20", "ad_id": "2000", "row_count": 999, "spend": "1", "revenue": "9999"},
            ],
        }
        sql_calls = []

        def fake_run_mysql(sql, timeout=120):
            sql_calls.append((sql, timeout))
            if "2026-08-09" in sql:
                return [
                    ["campaign", "100", "100", "7", "8", "2", "10", "5"],
                    ["campaign", "999", "999", "99", "999", "1", "500", "500"],
                    ["adgroup", "10", "100", "10", "7", "3", "10", "5"],
                    ["adgroup", "99", "999", "99", "999", "1", "500", "500"],
                    ["ad", "1000", "100", "10", "1000", "4", "10", "5"],
                    ["ad", "9999", "999", "99", "9999", "1", "500", "500"],
                ]
            return [
                ["campaign", "200", "200", "0", "0", "4", "20", "8"],
                ["adgroup", "20", "200", "20", "0", "5", "20", "8"],
                ["ad", "2000", "200", "20", "2000", "6", "20", "8"],
            ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            generated_at = self.write_snapshot(root, days, age=timedelta(minutes=90))
            with mock.patch.object(monitor, "run_mysql_safe", side_effect=fake_run_mysql):
                result, actual_generated_at, diagnostics = monitor.fetch_days(
                    ["2026-08-09", "2026-08-10"],
                    report_root=root,
                    max_snapshot_age=timedelta(hours=2),
                )

        self.assertEqual(generated_at, actual_generated_at)
        self.assertEqual("snapshot_campaign_closed_live_children", diagnostics["2026-08-09"]["scope_mode"])
        self.assertEqual(1, diagnostics["2026-08-09"]["snapshot_campaign_count"])
        self.assertEqual(6, diagnostics["2026-08-09"]["live_group_rows_returned"])
        self.assertEqual(3, diagnostics["2026-08-09"]["matched_live_group_rows"])
        self.assertEqual(3, diagnostics["2026-08-09"]["ignored_foreign_group_rows"])
        self.assertEqual(3, diagnostics["2026-08-09"]["ignored_foreign_insight_rows"])
        self.assertEqual(2, len(sql_calls))
        self.assertTrue(all(timeout == 120 for _, timeout in sql_calls))
        for sql, _ in sql_calls:
            self.assertEqual(2, sql.count("UNION ALL"))
            self.assertEqual(3, sql.count("FORCE INDEX(pcsa)"))
            self.assertEqual(3, sql.count("GROUP BY"))
            for category in (0, 1, 2):
                self.assertEqual(1, sql.count("category = %s" % category))
        self.assertLess(max(len(sql) for sql, _ in sql_calls), 7000)
        for level in monitor.LEVELS:
            self.assertEqual(Decimal("10"), result["2026-08-09"][level]["spend"])
            self.assertEqual(Decimal("5"), result["2026-08-09"][level]["revenue"])
        self.assertEqual(2, result["2026-08-09"]["campaign"]["rows"])
        self.assertEqual(3, result["2026-08-09"]["adgroup"]["rows"])
        self.assertEqual(4, result["2026-08-09"]["ad"]["rows"])
        self.assertEqual(1, result["2026-08-09"]["campaign"]["campaigns"])
        self.assertEqual(1, result["2026-08-09"]["adgroup"]["adgroups"])
        self.assertEqual(1, result["2026-08-09"]["ad"]["ads"])
        self.assertEqual(
            [],
            monitor.find_anomalies(
                "2026-08-09",
                result["2026-08-09"],
                Decimal("100"),
                Decimal("0.05"),
            ),
        )

    def test_new_children_under_scoped_campaign_are_included_and_foreign_campaign_is_ignored(self):
        scope_rows = [
            {"campaign_id": "100", "adset_id": "10", "ad_id": "1000"},
            {"campaign_id": "200", "adset_id": "20", "ad_id": "2000"},
        ]
        live_rows = [
            ["campaign", "100", "100", "0", "0", "4", "100", "100"],
            ["adgroup", "10", "100", "10", "0", "2", "50", "50"],
            ["adgroup", "11", "100", "11", "0", "2", "50", "50"],
            ["ad", "1000", "100", "10", "1000", "1", "50", "50"],
            ["ad", "1001", "100", "11", "1001", "1", "50", "50"],
            ["campaign", "200", "200", "0", "0", "1", "10", "8"],
            ["adgroup", "20", "200", "20", "0", "1", "10", "8"],
            ["ad", "2000", "200", "20", "2000", "1", "10", "8"],
            ["campaign", "999", "999", "0", "0", "1", "900", "900"],
            ["adgroup", "99", "999", "99", "0", "1", "900", "900"],
            ["ad", "9999", "999", "99", "9999", "1", "900", "900"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=live_rows):
            metrics, diagnostics = monitor.fetch_live_levels(
                "2026-08-10",
                monitor.scope_context_from_ad_rows(scope_rows),
            )
        for level in monitor.LEVELS:
            self.assertEqual(Decimal("110"), metrics[level]["spend"])
            self.assertEqual(Decimal("108"), metrics[level]["revenue"])
        self.assertEqual(3, diagnostics["ignored_foreign_group_rows"])
        self.assertEqual(3, diagnostics["ignored_foreign_insight_rows"])
        self.assertEqual(3, metrics["adgroup"]["adgroups"])
        self.assertEqual(3, metrics["ad"]["ads"])
        self.assertEqual(
            [],
            monitor.find_anomalies(
                "2026-08-10",
                metrics,
                Decimal("100"),
                Decimal("0.05"),
            ),
        )

    def test_child_row_without_campaign_id_fails_closed(self):
        scope_rows = [{"campaign_id": "100", "adset_id": "10", "ad_id": "1000"}]
        live_rows = [
            ["campaign", "100", "100", "0", "0", "1", "100", "100"],
            ["adgroup", "10", "0", "10", "0", "2", "100", "100"],
            ["ad", "1000", "100", "10", "1000", "1", "100", "100"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=live_rows):
            with self.assertRaisesRegex(RuntimeError, "child rows without campaign_id: groups=1 rows=2"):
                monitor.fetch_live_levels(
                    "2026-08-10",
                    monitor.scope_context_from_ad_rows(scope_rows),
                )

    def test_partial_ad_campaign_coverage_remains_an_alertable_metric_gap(self):
        scope_rows = [
            {"campaign_id": "100", "adset_id": "10", "ad_id": "1000"},
            {"campaign_id": "200", "adset_id": "20", "ad_id": "2000"},
        ]
        live_rows = [
            ["campaign", "100", "100", "0", "0", "1", "100", "100"],
            ["campaign", "200", "200", "0", "0", "1", "10", "10"],
            ["adgroup", "10", "100", "10", "0", "1", "100", "100"],
            ["adgroup", "20", "200", "20", "0", "1", "10", "10"],
            ["ad", "2000", "200", "20", "2000", "1", "10", "10"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=live_rows):
            metrics, diagnostics = monitor.fetch_live_levels(
                "2026-08-10",
                monitor.scope_context_from_ad_rows(scope_rows),
            )
        self.assertEqual(Decimal("110"), metrics["campaign"]["spend"])
        self.assertEqual(Decimal("110"), metrics["adgroup"]["spend"])
        self.assertEqual(Decimal("10"), metrics["ad"]["spend"])
        self.assertEqual(1, diagnostics["missing_scoped_campaign_count_by_level"]["ad"])
        anomalies = monitor.find_anomalies(
            "2026-08-10",
            metrics,
            Decimal("100"),
            Decimal("0.05"),
        )
        self.assertEqual({"spend", "revenue"}, {item["metric"] for item in anomalies})
        self.assertTrue(all(item["abnormal_level"] == "ad" for item in anomalies))

    def test_campaign_absent_from_all_live_levels_contributes_zero_without_failure(self):
        scope_rows = [
            {"campaign_id": "100", "adset_id": "10", "ad_id": "1000"},
            {"campaign_id": "200", "adset_id": "20", "ad_id": "2000"},
        ]
        live_rows = [
            ["campaign", "200", "200", "0", "0", "1", "10", "10"],
            ["adgroup", "20", "200", "20", "0", "1", "10", "10"],
            ["ad", "2000", "200", "20", "2000", "1", "10", "10"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=live_rows):
            metrics, diagnostics = monitor.fetch_live_levels(
                "2026-08-10",
                monitor.scope_context_from_ad_rows(scope_rows),
            )
        self.assertEqual(1, diagnostics["missing_scoped_campaign_count_all_levels"])
        self.assertEqual([], monitor.find_anomalies("2026-08-10", metrics, Decimal("100"), Decimal("0.05")))

    def test_zero_matched_groups_fails_closed(self):
        context = monitor.scope_context_from_ad_rows(
            [{"campaign_id": "100", "adset_id": "10", "ad_id": "1000"}]
        )
        foreign_rows = [
            ["campaign", "999", "999", "0", "0", "1", "10", "10"],
            ["adgroup", "99", "999", "99", "0", "1", "10", "10"],
            ["ad", "9999", "999", "99", "9999", "1", "10", "10"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=foreign_rows):
            with self.assertRaisesRegex(RuntimeError, "matched no live groups for scoped campaigns"):
                monitor.fetch_live_levels("2026-08-10", context)

    def test_numeric_ids_are_canonicalized_like_unsigned_sql_scope_ids(self):
        self.assertEqual("123", monitor.numeric_id("000123"))
        context = monitor.scope_context_from_ad_rows(
            [{"campaign_id": "000100", "adset_id": "00010", "ad_id": "001000"}]
        )
        self.assertEqual({"100"}, context["campaign_ids"])
        self.assertEqual(1, context["snapshot_adgroup_count"])
        self.assertEqual(1, context["snapshot_ad_count"])
        self.assertEqual(
            {"campaign_id": 1, "adset_id": 1, "ad_id": 1},
            context["snapshot_noncanonical_id_count_by_field"],
        )

    def test_valid_query_with_missing_level_stays_zero_and_participates_in_alerting(self):
        scope_rows = [{"campaign_id": "100", "adset_id": "10", "ad_id": "1000"}]
        live_rows = [
            ["campaign", "100", "100", "0", "0", "1", "1000", "1000"],
            ["ad", "1000", "100", "10", "1000", "1", "1000", "1000"],
        ]
        with mock.patch.object(monitor, "run_mysql_safe", return_value=live_rows):
            metrics, _ = monitor.fetch_live_levels(
                "2026-08-10",
                monitor.scope_context_from_ad_rows(scope_rows),
            )
        self.assertEqual(Decimal("0"), metrics["adgroup"]["spend"])
        anomalies = monitor.find_anomalies(
            "2026-08-10",
            metrics,
            Decimal("100"),
            Decimal("0.05"),
        )
        self.assertEqual({"spend", "revenue"}, {item["metric"] for item in anomalies})
        self.assertTrue(all(item["abnormal_level"] == "adgroup" for item in anomalies))

    def test_invalid_query_level_and_column_count_fail_closed(self):
        context = monitor.scope_context_from_ad_rows(
            [{"campaign_id": "100", "adset_id": "10", "ad_id": "1000"}]
        )
        with mock.patch.object(monitor, "run_mysql_safe", return_value=[["invalid"] * 8]):
            with self.assertRaisesRegex(RuntimeError, "invalid metric level"):
                monitor.fetch_live_levels("2026-08-10", context)
        with mock.patch.object(monitor, "run_mysql_safe", return_value=[["campaign"] * 7]):
            with self.assertRaisesRegex(RuntimeError, "invalid column count"):
                monitor.fetch_live_levels("2026-08-10", context)

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
            self.write_snapshot(root, {day: rows})
            manifest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            manifest["data_files"]["ad"][day]["row_count"] = 2
            (root / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "row count mismatch"):
                monitor.load_published_scope_contexts([day], report_root=root)


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
            monitor,
            "fetch_days",
            return_value=(
                all_data,
                "2026-08-10 16:00:00",
                {"2026-08-10": {"scope_mode": "snapshot_campaign_closed_live_children"}},
            ),
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
                self.run_main_with(state_file, all_data, {})
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

    def test_zero_hit_failure_does_not_turn_active_anomaly_into_recovery(self):
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
        original = {
            "active": {
                "2026-08-10:spend": {
                    "key": "2026-08-10:spend",
                    "day": "2026-08-10",
                    "metric": "spend",
                    "abnormal_level": "adgroup",
                }
            },
            "marker": "unchanged",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            state_file.write_text(json.dumps(original), encoding="utf-8")
            with mock.patch.object(monitor, "STATE_FILE", state_file), mock.patch.object(
                monitor, "parse_args", return_value=args
            ), mock.patch.object(
                monitor,
                "fetch_days",
                side_effect=RuntimeError("TT minis consistency query matched no live groups for scoped campaigns"),
            ), mock.patch.object(
                monitor, "send_feishu_card"
            ) as send, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "matched no live groups"):
                    monitor.main()
            self.assertEqual(0, send.call_count)
            self.assertEqual(original, json.loads(state_file.read_text(encoding="utf-8")))


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
        self.assertEqual("TT minis consistency query timed out after 120 seconds", message)
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
        self.assertEqual("TT minis consistency query failed with mysql exit code 1", str(caught.exception))
        self.assertNotIn("SUPERSECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

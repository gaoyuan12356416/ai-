"""Independent offline-validator tests; fixtures never use generator formulas."""

import ast
import copy
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import validate_v2_upgrade as validator


HERE = Path(__file__).resolve().parent
BASE_VERSION = "20260826T120000000000+0800"
V2_VERSION = "20260827T120000000000+0800"
FORMULAS = {"d0_cpa": 60, "cpm": 6, "apm": 0.1, "ctr": 0.02,
            "cvr": 0.05, "install_to_d0_rate": 0.1}
BENCH_FORMULAS = {"d0_cpa": 100, "cpm": 10, "apm": 0.1, "ctr": 0.01,
                  "cvr": 0.1, "install_to_d0_rate": 0.1}


def material(month, channel, app, source_id):
    return {
        "month": month, "channel": channel, "app": app, "custom_source_id": source_id,
        "material_type": "VID", "material_name": "素材甲", "maker": "制作者",
        "source_url": "https://example.invalid/%s.mp4" % source_id,
        "thumbnail_url": "assets/thumbnails/%s-hash.jpg" % source_id,
        "source_status": "available", "thumbnail_status": "available",
        "spend": 6000, "impressions": 1000000, "clicks": 20000,
        "installs": 1000, "af_d0_first_transactions": 100, "selection_rule": "B",
        "selling_points": [{"keyword": "关键词", "order": [1, 2]}],
        "evidence": {"material_ctr": 0.02, "material_cpa": 60, "platform_ctr": 0.01,
                     "rule_a_available": True, "rule_a_pass": False, "rule_b_pass": True,
                     "original_extra": {"legacy_flag": True, "list": ["a", "b"]}},
    }


def month_payload(month):
    rows = [material(month, "Meta", "NG OPay", 101), material(month, "TikTok", "PK OPay", 202)]
    benchmarks, audits = [], []
    for channel in validator.CHANNELS:
        for app in validator.APPS:
            benchmarks.append({"month": month, "channel": channel, "app": app,
                               "spend": 10000, "impressions": 1000000, "clicks": 10000,
                               "installs": 1000, "af_d0_first_transactions": 100,
                               "ctr": 0.01, "cpa": 100, "cpa_finite": True,
                               "legacy_extra": {"preserve": "all fields"}})
            selected = sum(row["channel"] == channel and row["app"] == app for row in rows)
            audits.append({"month": month, "channel": channel, "app": app, "status": "success",
                           "message": "旧审计", "selected_count": selected, "platform_spend": 10000,
                           "mapping_coverage": 0.6, "mapping_gap_spend": 4000, "af_total": 100,
                           "af_mapped": 100, "af_mapping_coverage": 1,
                           "rule_a_available": channel != "Google", "legacy_extra": [1, 2]})
    return {"schema_version": 1, "data_version": BASE_VERSION, "month": month, "stage": "final",
            "generated_at": "2026-08-26T12:00:00+08:00", "keyword_config_version": "fixture",
            "rows": rows, "benchmarks": benchmarks, "audits": audits}


def candidate_payload(baseline):
    payload = copy.deepcopy(baseline)
    payload.update(schema_version=2, data_version=V2_VERSION, generated_at="2026-08-27T12:00:00+08:00")
    for row in payload["rows"]:
        row["metrics"] = dict(FORMULAS)
    google = material(payload["month"], "Google", "NG OPay", 901)
    google.update(installs=None, af_d0_first_transactions=None, platform_conversions=123.25)
    google["metrics"] = {"d0_cpa": None, "cpm": 6, "apm": None, "ctr": 0.02,
                         "cvr": None, "install_to_d0_rate": None}
    google["evidence"].update(rule_a_available=False, rule_a_pass=False, rule_b_pass=True,
                              material_cpa=None, mapping_status="exact", usd_status="verified",
                              metric_source="ads_google_insights:type=3",
                              af_status="missing_asset_attribution", installs_status="missing_asset_installs")
    payload["rows"].append(google)
    for benchmark in payload["benchmarks"]:
        benchmark["metrics"] = dict(BENCH_FORMULAS)
        if benchmark["channel"] == "Google":
            benchmark["installs"] = None
            benchmark["metrics"].update(cvr=None, install_to_d0_rate=None)
    for audit in payload["audits"]:
        if audit["channel"] == "Google":
            audit.update(selected_count=1 if audit["app"] == "NG OPay" else 0,
                         metric_source="ads_google_insights:type=0", af_mapped=None, af_mapping_coverage=None,
                         fx_missing_rows=0, platform_fx_missing_rows=0, incomplete_material_count=0,
                         fx_missing_native_spend={}, platform_fx_missing_native_spend={},
                         baseline_missing_account_days=0)
    return payload


class UpgradeValidatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="opay-v2-validation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = self.root / "baseline"
        self.candidate = self.root / "candidate"
        self.old = {month: month_payload(month) for month in validator.MONTHS}
        self.new = {month: candidate_payload(self.old[month]) for month in validator.MONTHS}
        self.write_reports()

    def write_reports(self):
        for root, payloads, schema, version in ((self.baseline, self.old, 1, BASE_VERSION),
                                                (self.candidate, self.new, 2, V2_VERSION)):
            folder = root / "data" / version
            folder.mkdir(parents=True, exist_ok=True)
            for month, payload in payloads.items():
                self.write_json(folder / (month + ".json"), payload)
            self.write_json(root / "latest.json", {
                "schema_version": schema, "data_version": version, "latest_month": "2026-07",
                "months": [{"month": month, "stage": "final", "status": "success", "row_count": len(payloads[month]["rows"])}
                           for month in validator.MONTHS],
            })

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def path(self, month="2026-07", baseline=False):
        return (self.baseline if baseline else self.candidate) / "data" / (BASE_VERSION if baseline else V2_VERSION) / (month + ".json")

    def report(self):
        return validator.validate_upgrade(self.baseline, self.candidate)

    def assert_pass(self):
        report = self.report()
        self.assertEqual(report["status"], "PASS", report["errors"])
        return report

    def assert_fail(self, pattern):
        report = self.report()
        self.assertEqual(report["status"], "FAIL")
        self.assertRegex("\n".join(report["errors"]), pattern)
        return report

    def gg(self, field="rows", app="NG OPay"):
        return next(row for row in self.new["2026-07"][field] if row["channel"] == "Google" and row["app"] == app)

    def test_valid_seven_months_include_zero_google_scope(self):
        report = self.assert_pass()
        self.assertEqual(report["month_count"], 7)
        self.assertEqual(report["months"][-1]["candidate_channel_counts"], {"Google": 1, "Meta": 1, "TikTok": 1})
        self.assertEqual(report["months"][-1]["google_gaps"][1]["selected_count"], 0)

    def test_native_fx_gaps_are_preserved_and_not_converted_to_usd(self):
        self.gg("audits").update(fx_missing_native_spend={"NGN": 12345.67}, platform_fx_missing_native_spend={"NGN": 13802269.04})
        self.write_reports()
        gap = self.assert_pass()["months"][-1]["google_gaps"][0]
        self.assertEqual(gap["fx_missing_native_spend"], {"NGN": "12345.67"})
        self.assertEqual(gap["platform_fx_missing_native_spend"], {"NGN": "13802269.04"})
        self.gg("audits")["fx_missing_native_spend"]["NGN"] = -1
        self.write_reports()
        self.assert_fail("finite and nonnegative")

    def test_negative_platform_conversions_are_rejected(self):
        self.gg()["platform_conversions"] = -0.1
        self.write_reports()
        self.assert_fail("platform_conversions.*finite and nonnegative")

    def test_read_only_no_network_or_subprocess(self):
        before = {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network is forbidden")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess is forbidden")):
            self.assert_pass()
        after = {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_no_production_formula_imports(self):
        tree = ast.parse((HERE / "validate_v2_upgrade.py").read_text(encoding="utf-8"))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        self.assertTrue(imports.isdisjoint({"opay_excellent_creatives", "google_creatives", "sqlite3", "requests", "subprocess"}))

    def test_independent_normal_and_null_zero_formulas(self):
        record = self.new["2026-07"]["rows"][0]
        self.assertEqual(validator.expected_metrics(record), {key: Decimal(str(value)) for key, value in FORMULAS.items()})
        record.update(spend=0, impressions=0, clicks=0, installs=0, af_d0_first_transactions=0)
        zero = validator.expected_metrics(record)
        self.assertEqual(zero["ctr"], 0)
        self.assertTrue(all(value is None for key, value in zero.items() if key != "ctr"))
        record.update(spend=None, impressions=1000, clicks=20, installs=None, af_d0_first_transactions=5)
        missing = validator.expected_metrics(record)
        self.assertEqual(missing["apm"], 5)
        self.assertEqual(missing["ctr"], Decimal("0.02"))
        self.assertIsNone(missing["d0_cpa"])
        self.assertIsNone(missing["cpm"])

    def test_fx_missing_platform_usd_keeps_complete_ctr_and_rule_b(self):
        benchmark, audit = self.gg("benchmarks"), self.gg("audits")
        benchmark.update(spend=None, cpa=None, cpa_finite=None)
        benchmark["metrics"].update(d0_cpa=None, cpm=None)
        audit.update(platform_spend=None, mapping_coverage=None, mapping_gap_spend=None, platform_fx_missing_rows=10)
        self.write_reports()
        report = self.assert_pass()
        self.assertEqual(report["months"][-1]["google_gaps"][0]["platform_fx_missing_rows"], 10)

    def test_missing_campaign_baseline_is_valid_only_when_b_is_paused(self):
        self.new["2026-07"]["rows"] = [row for row in self.new["2026-07"]["rows"] if row["channel"] != "Google"]
        benchmark, audit = self.gg("benchmarks"), self.gg("audits")
        benchmark.update(spend=None, impressions=None, clicks=None, ctr=None, cpa=None, cpa_finite=None)
        benchmark["metrics"] = dict.fromkeys(FORMULAS)
        audit.update(selected_count=0, baseline_missing_account_days=1, platform_spend=None,
                     mapping_gap_spend=None, mapping_coverage=None)
        self.write_reports()
        self.assert_pass()
        self.new["2026-07"]["rows"].append(candidate_payload(self.old["2026-07"])["rows"][-1])
        audit["selected_count"] = 1
        self.write_reports()
        self.assert_fail("rule B must pause")

    def test_every_original_non_google_row_field_is_compared(self):
        original = copy.deepcopy(self.new["2026-07"])
        for mutation in (lambda row: row.update(maker="changed"),
                         lambda row: row.pop("source_url"),
                         lambda row: row.update(extra_new_field="not exempt"),
                         lambda row: row["selling_points"][0].update(order=[2, 1]),
                         lambda row: row["evidence"]["original_extra"].update(legacy_flag=1),
                         lambda row: row["evidence"].update(metrics={"not": "top-level"})):
            self.new["2026-07"] = copy.deepcopy(original)
            mutation(self.new["2026-07"]["rows"][0])
            self.write_reports()
            self.assert_fail("Meta/TikTok fields changed")

    def test_non_google_benchmark_and_audit_extra_fields_are_compared(self):
        for field in ("benchmarks", "audits"):
            payload = self.new["2026-07"]
            item = next(row for row in payload[field] if row["channel"] == "Meta")
            old = copy.deepcopy(item["legacy_extra"])
            item["legacy_extra"] = "changed"
            self.write_reports()
            self.assert_fail("Meta/TikTok fields changed")
            item["legacy_extra"] = old

    def test_non_google_row_addition_or_removal_fails(self):
        self.new["2026-07"]["rows"].pop(0)
        self.write_reports()
        self.assert_fail("Meta/TikTok key set changed")

    def test_metrics_are_checked_independently_for_rows_and_all_benchmarks(self):
        original = copy.deepcopy(self.new["2026-07"])
        for field in ("rows", "benchmarks"):
            for key in FORMULAS:
                self.new["2026-07"] = copy.deepcopy(original)
                row = next(row for row in self.new["2026-07"][field] if row["channel"] == "Meta")
                row["metrics"][key] += 1
                self.write_reports()
                self.assert_fail("formula mismatch")

    def test_metrics_precision_is_six_or_eight_not_unrounded(self):
        for key, number in (("d0_cpa", 60.0000001), ("cpm", 6.0000001), ("apm", 0.100000001)):
            self.new["2026-07"] = candidate_payload(self.old["2026-07"])
            self.new["2026-07"]["rows"][0]["metrics"][key] = number
            self.write_reports()
            self.assert_fail("declared .*decimal precision")

    def test_missing_metric_and_extra_metric_fail(self):
        self.gg()["metrics"].pop("cvr")
        self.write_reports()
        self.assert_fail("exactly the six")
        self.gg()["metrics"]["cvr"] = None
        self.gg()["metrics"]["cpc"] = 1
        self.write_reports()
        self.assert_fail("exactly the six")

    def test_missing_metrics_cannot_be_coerced_to_zero(self):
        self.gg()["metrics"]["apm"] = 0
        self.write_reports()
        self.assert_fail("must be null")

    def test_google_conversions_do_not_fill_install_or_af_metrics(self):
        self.gg()["metrics"]["cvr"] = 0.0061625
        self.write_reports()
        self.assert_fail("must be null")

    def test_google_af_and_installs_must_be_explicit_null(self):
        for field in ("installs", "af_d0_first_transactions"):
            self.new["2026-07"] = candidate_payload(self.old["2026-07"])
            row = self.gg()
            row[field] = 0
            row["metrics"]["cvr" if field == "installs" else "apm"] = 0
            self.write_reports()
            self.assert_fail("asset .* must be null")

    def test_google_id_is_positive_json_integer_not_bool_float_or_string(self):
        for value in (0, -1, True, 901.0, "901"):
            self.gg()["custom_source_id"] = value
            self.write_reports()
            self.assert_fail("custom_source_id must be an integer >= 1")

    def test_google_only_b_and_boolean_rule_evidence(self):
        self.gg()["selection_rule"] = "A+B"
        self.write_reports()
        self.assert_fail("only rule B")
        self.gg()["selection_rule"] = "B"
        self.gg()["evidence"]["rule_a_available"] = True
        self.write_reports()
        self.assert_fail("inconsistent A/B evidence")

    def test_strict_spend_boundary(self):
        self.gg().update(spend=5000)
        self.gg()["metrics"]["cpm"] = 5
        self.write_reports()
        self.assert_fail("strictly > 5000")
        self.gg().update(spend=5000.01)
        self.gg()["metrics"]["cpm"] = 5.00001
        self.write_reports()
        self.assert_pass()

    def test_ctr_equality_fails_and_unrounded_strict_crossover_passes(self):
        self.gg().update(clicks=10000)
        self.gg()["metrics"]["ctr"] = 0.01
        self.gg()["evidence"]["material_ctr"] = 0.01
        self.write_reports()
        self.assert_fail("CTR must be strictly above")
        self.gg().update(impressions=100000000, clicks=1000001)
        self.gg()["metrics"].update(cpm=0.06, ctr=0.01000001)
        self.gg()["evidence"]["material_ctr"] = 0.01000001
        self.write_reports()
        self.assert_pass()

    def test_type0_and_type3_provenance_are_required(self):
        self.gg("audits")["metric_source"] = "ads_google_insights:type=3"
        self.write_reports()
        self.assert_fail("type0 benchmark provenance")
        self.gg("audits")["metric_source"] = "ads_google_insights:type=0"
        self.gg()["evidence"]["metric_source"] = "ad-group allocation"
        self.write_reports()
        self.assert_fail("exact type3 material provenance")

    def test_missing_scope_is_rejected_even_when_it_has_no_selected_rows(self):
        original_old, original_new = copy.deepcopy(self.old), copy.deepcopy(self.new)
        for target in ("old", "new"):
            for field in ("benchmarks", "audits"):
                self.old, self.new = copy.deepcopy(original_old), copy.deepcopy(original_new)
                getattr(self, target)["2026-07"][field].pop()
                self.write_reports()
                self.assert_fail("missing scope")

    def test_duplicate_row_benchmark_and_audit_keys_are_rejected(self):
        original = copy.deepcopy(self.new["2026-07"])
        for field in ("rows", "benchmarks", "audits"):
            self.new["2026-07"] = copy.deepcopy(original)
            self.new["2026-07"][field].append(copy.deepcopy(self.new["2026-07"][field][0]))
            self.write_reports()
            self.assert_fail("duplicate .* key")

    def test_json_duplicate_keys_are_rejected_at_top_and_nested_levels(self):
        path = self.candidate / "latest.json"
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2', 1), encoding="utf-8")
        self.assert_fail("duplicate JSON key")
        path.write_text(raw, encoding="utf-8")
        path = self.path()
        raw = path.read_text(encoding="utf-8")
        path.write_text('{"nested":{"x":1,"x":2},' + raw[1:], encoding="utf-8")
        self.assert_fail("duplicate JSON key")

    def test_nan_infinity_and_overflow_are_rejected_anywhere(self):
        raw = self.path().read_text(encoding="utf-8")
        for token in ("NaN", "Infinity", "-Infinity", "1e999"):
            self.path().write_text('{"extra":' + token + ',' + raw[1:], encoding="utf-8")
            self.assert_fail("non-finite")

    def test_string_infinity_is_not_a_numeric_metric(self):
        self.gg()["metrics"]["ctr"] = "Infinity"
        self.write_reports()
        self.assert_fail("finite JSON number")

    def test_same_seven_frozen_months_required(self):
        path = self.candidate / "latest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        missing = copy.deepcopy(manifest)
        missing["months"].pop()
        self.write_json(path, missing)
        self.assert_fail("same seven months")
        duplicate = copy.deepcopy(manifest)
        duplicate["months"].append(duplicate["months"][0])
        self.write_json(path, duplicate)
        self.assert_fail("duplicate manifest month")
        manifest["months"][0]["stage"] = "initial"
        self.write_json(path, manifest)
        self.assert_fail("successful frozen final")

    def test_clone_then_publish_mixed_schema_is_rejected(self):
        self.new["2026-07"]["schema_version"] = 1
        self.write_reports()
        self.assert_fail("mixed-schema publish is forbidden")

    def test_manifest_payload_version_and_row_count_must_match(self):
        self.new["2026-07"]["data_version"] = BASE_VERSION
        self.write_reports()
        self.assert_fail("payload data_version mismatch")
        self.new["2026-07"]["data_version"] = V2_VERSION
        self.write_reports()
        path = self.candidate / "latest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["months"][-1]["row_count"] = 99
        self.write_json(path, manifest)
        self.assert_fail("manifest row_count mismatch")

    def test_path_traversal_and_same_directory_are_rejected(self):
        report = validator.validate_upgrade(self.baseline, self.baseline)
        self.assertIn("must be different", report["errors"][0])
        path = self.candidate / "latest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["data_version"] = "../../outside"
        self.write_json(path, manifest)
        self.assert_fail("invalid data_version")

    def test_all_seven_month_results_are_reported_after_one_failure(self):
        self.new["2026-01"]["rows"][0]["maker"] = "changed"
        self.write_reports()
        report = self.assert_fail("Meta/TikTok fields changed")
        self.assertEqual(len(report["months"]), 7)
        self.assertEqual(report["months"][-1]["status"], "PASS")

    def test_manifest_change_during_validation_is_rejected(self):
        original = validator.read_json
        reads = 0

        def changing(path):
            nonlocal reads
            value, digest = original(path)
            if path == self.candidate / "latest.json":
                reads += 1
                if reads > 1:
                    digest = "changed"
            return value, digest

        with mock.patch.object(validator, "read_json", side_effect=changing):
            self.assert_fail("latest.json changed during validation")

    def test_cli_success_and_failure_are_json_with_correct_exit_code(self):
        command = [sys.executable, "-B", str(HERE / "validate_v2_upgrade.py"),
                   "--baseline-dir", str(self.baseline), "--candidate-dir", str(self.candidate)]
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(json.loads(process.stdout)["status"], "PASS")
        self.gg()["selection_rule"] = "A"
        self.write_reports()
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()

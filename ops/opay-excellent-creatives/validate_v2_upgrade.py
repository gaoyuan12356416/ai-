#!/usr/bin/env python3
"""Read-only, independent validation of frozen V1 public data against V2.

Usage: python validate_v2_upgrade.py --baseline-dir V1_PUBLIC --candidate-dir V2_PUBLIC

Only latest.json and its seven versioned month files are read. No generator,
database, network, media fetch, or output-file writes are involved. Provenance
labels are checked, not independently certified against the source warehouse.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


MONTHS = tuple("2026-%02d" % month for month in range(1, 8))
CHANNELS = ("Google", "Meta", "TikTok")
APPS = ("NG OPay", "PK OPay")
SCOPES = frozenset((channel, app) for channel in CHANNELS for app in APPS)
METRIC_DIGITS = {
    "d0_cpa": 6,
    "cpm": 6,
    "apm": 8,
    "ctr": 8,
    "cvr": 8,
    "install_to_d0_rate": 8,
}
VERSION_PATTERN = re.compile(r"\d{8}T\d{12,20}[+-]\d{4}\Z")


class ValidationError(ValueError):
    """An invalid or incompatible public artifact, not an implementation error."""


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def reject_constant(value):
    raise ValidationError("non-finite JSON constant: %s" % value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def check_finite_tree(value):
    if isinstance(value, dict):
        for item in value.values():
            check_finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            check_finite_tree(item)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        require(finite, "non-finite JSON number (including numeric overflow)")


def read_json(path):
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=Decimal,
        )
        check_finite_tree(value)
        require(isinstance(value, dict), "top-level JSON must be an object")
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, ValueError, InvalidOperation) as exc:
        raise ValidationError("%s: %s" % (path, exc)) from exc


def integer(value, label, *, minimum=0, nullable=False):
    if value is None and nullable:
        return None
    require(type(value) is int and value >= minimum, "%s must be an integer >= %d" % (label, minimum))
    return value


def numeric(value, label, *, nullable=False):
    if value is None and nullable:
        return None
    require(not isinstance(value, bool) and isinstance(value, (int, float, Decimal)),
            "%s must be a finite JSON number%s" % (label, " or null" if nullable else ""))
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "%s must be finite and nonnegative" % label)
    return result


def required_field(record, field, label):
    require(field in record, "%s missing field %s" % (label, field))
    return record[field]


def expected_metrics(record, label="record"):
    """Independent formulas from raw totals; do not import production helpers."""
    spend = numeric(required_field(record, "spend", label), label + ".spend", nullable=True)
    counts = {}
    for field in ("impressions", "clicks", "installs", "af_d0_first_transactions"):
        value = integer(required_field(record, field, label), label + "." + field, nullable=True)
        counts[field] = Decimal(value) if value is not None else None
    impressions, clicks = counts["impressions"], counts["clicks"]
    installs, first_transactions = counts["installs"], counts["af_d0_first_transactions"]

    def divide(numerator, denominator, factor=1):
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator * Decimal(factor) / denominator

    with localcontext() as context:
        context.prec = 50
        ctr = None if clicks is None or impressions is None else (
            Decimal(0) if impressions == 0 else clicks / impressions
        )
        return {
            "d0_cpa": divide(spend, first_transactions),
            "cpm": divide(spend, impressions, 1000),
            "apm": divide(first_transactions, impressions, 1000),
            "ctr": ctr,
            "cvr": divide(installs, clicks),
            "install_to_d0_rate": divide(first_transactions, installs),
        }


def assert_metric(actual, expected, label, digits):
    if expected is None:
        require(actual is None, "%s must be null, not zero or a substituted value" % label)
        return
    value = numeric(actual, label)
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + abs(value.adjusted()) + digits + 2)
        require(value == value.quantize(Decimal(1).scaleb(-digits)),
                "%s exceeds the declared %d-decimal precision" % (label, digits))
    if expected == 0:
        require(value == 0, "%s must preserve measured zero" % label)
        return
    # A half unit at the published precision permits either tie-rounding rule.
    # The tiny extra allowance only covers existing binary-float serialization.
    tolerance = Decimal(5).scaleb(-digits - 1) + Decimal("1e-12")
    require(abs(value - expected) <= tolerance,
            "%s formula mismatch: got %s, expected %s (tolerance %s)" % (label, value, expected, tolerance))


def validate_metrics(record, label):
    metrics = required_field(record, "metrics", label)
    require(isinstance(metrics, dict) and set(metrics) == set(METRIC_DIGITS),
            "%s.metrics must contain exactly the six metric keys" % label)
    expected = expected_metrics(record, label)
    for key, digits in METRIC_DIGITS.items():
        assert_metric(metrics[key], expected[key], label + ".metrics." + key, digits)
    return expected


def exact_equal(left, right):
    """JSON semantic equality, without Python's True == 1 shortcut."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(exact_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(exact_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        return Decimal(str(left)) == Decimal(str(right))
    return type(left) is type(right) and left == right


def without_metrics(record):
    # Only the new, top-level metrics object is exempt. Nested evidence and all
    # original metadata, media fields, list ordering, and unknown fields count.
    return {key: value for key, value in record.items() if key != "metrics"}


def index_records(payload, field, month, side):
    records = required_field(payload, field, side)
    require(isinstance(records, list), "%s.%s must be a list" % (side, field))
    indexed = {}
    for position, record in enumerate(records):
        label = "%s.%s[%d]" % (side, field, position)
        require(isinstance(record, dict), label + " must be an object")
        require(record.get("month") == month, label + " month mismatch")
        scope = (record.get("channel"), record.get("app"))
        require(all(isinstance(part, str) for part in scope) and scope in SCOPES,
                label + " unknown channel/App scope")
        key = scope
        if field == "rows":
            key += (integer(record.get("custom_source_id"), label + ".custom_source_id", minimum=1),)
        require(key not in indexed, "%s duplicate %s key: %s" % (side, field, key))
        indexed[key] = record
    if field != "rows":
        require(set(indexed) == SCOPES, "%s.%s missing scope(s): %s" % (side, field, sorted(SCOPES - set(indexed))))
    return indexed


def compare_non_google(before, after, label):
    old = {key: without_metrics(value) for key, value in before.items() if key[0] != "Google"}
    new = {key: without_metrics(value) for key, value in after.items() if key[0] != "Google"}
    require(set(old) == set(new), "%s Meta/TikTok key set changed" % label)
    for key in old:
        require(exact_equal(old[key], new[key]), "%s Meta/TikTok fields changed for %s (only metrics may differ)" % (label, key))


def load_manifest(root, schema, side):
    require(root.is_dir(), "%s public directory does not exist: %s" % (side, root))
    manifest, digest = read_json(root / "latest.json")
    require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == schema,
            "%s latest schema_version must be %d" % (side, schema))
    version = manifest.get("data_version")
    require(isinstance(version, str) and VERSION_PATTERN.fullmatch(version), side + " invalid data_version")
    require(manifest.get("latest_month") == MONTHS[-1], side + " latest_month must be 2026-07")
    entries = manifest.get("months")
    require(isinstance(entries, list), side + " months must be a list")
    indexed = {}
    for entry in entries:
        require(isinstance(entry, dict), side + " month entry must be an object")
        month = entry.get("month")
        require(isinstance(month, str) and month in MONTHS, side + " unexpected month")
        require(month not in indexed, side + " duplicate manifest month: " + month)
        require(entry.get("stage") == "final" and entry.get("status") == "success",
                side + " month must be a successful frozen final: " + month)
        integer(entry.get("row_count"), side + "." + month + ".row_count")
        indexed[month] = entry
    require(set(indexed) == set(MONTHS), side + " must contain the same seven months 2026-01..2026-07")
    return manifest, indexed, digest


def load_month(root, manifest, entry, schema, side):
    month = entry["month"]
    path = root / "data" / manifest["data_version"] / (month + ".json")
    require(path.resolve().is_relative_to(root), side + " month file escapes public directory")
    payload, digest = read_json(path)
    require(type(payload.get("schema_version")) is int and payload["schema_version"] == schema,
            "%s %s month schema_version must be %d; mixed-schema publish is forbidden" % (side, month, schema))
    require(payload.get("month") == month and payload.get("stage") == "final", side + " month/stage mismatch")
    require(payload.get("data_version") == manifest["data_version"], side + " payload data_version mismatch")
    require(isinstance(payload.get("rows"), list) and len(payload["rows"]) == entry["row_count"],
            side + " manifest row_count mismatch for " + month)
    return payload, digest


def validate_google_scope(scope, benchmark, audit, rows):
    label = "Google " + scope[1]
    require(audit.get("metric_source") == "ads_google_insights:type=0", label + " missing type0 benchmark provenance")
    require(audit.get("status") == "success", label + " scope is not successfully refreshed")
    require(audit.get("rule_a_available") is False, label + " rule A must remain unavailable")
    missing = integer(required_field(audit, "baseline_missing_account_days", label), label + ".baseline_missing_account_days")
    if missing:
        require(not rows, label + " rule B must pause when Campaign account-day baseline is missing")
        for field in ("spend", "impressions", "clicks", "ctr", "cpa"):
            require(benchmark.get(field) is None, label + " incomplete Campaign baseline must keep " + field + " null")
    if benchmark.get("spend") is None:
        for field in ("platform_spend", "mapping_coverage", "mapping_gap_spend"):
            require(audit.get(field) is None, label + " unknown platform USD must keep " + field + " null")
    for field in ("fx_missing_rows", "platform_fx_missing_rows", "incomplete_material_count"):
        integer(required_field(audit, field, label), label + "." + field)
    require(audit.get("af_mapped") is None and audit.get("af_mapping_coverage") is None,
            label + " asset AF audit must remain null")

    for row in rows:
        item = "%s material %s" % (label, row["custom_source_id"])
        require(row.get("selection_rule") == "B", item + " may select only rule B")
        spend = numeric(row.get("spend"), item + ".spend")
        require(spend > Decimal(5000), item + " spend must be strictly > 5000 USD")
        for field in ("installs", "af_d0_first_transactions"):
            require(field in row and row[field] is None, item + " asset " + field + " must be null")
        require(row.get("material_type") in ("VID", "PIC"), item + " unsupported material type")
        exposure = integer(row.get("impressions"), item + ".impressions", minimum=1)
        clicks = integer(row.get("clicks"), item + ".clicks")
        platform_exposure = integer(benchmark.get("impressions"), label + ".impressions")
        platform_clicks = integer(benchmark.get("clicks"), label + ".clicks")
        # Known zero campaign exposure preserves V1's CTR=0 convention; missing
        # exposure is rejected above and must never acquire that zero meaning.
        better_ctr = clicks > 0 if platform_exposure == 0 else clicks * platform_exposure > platform_clicks * exposure
        require(better_ctr and missing == 0, item + " CTR must be strictly above complete type0 benchmark")
        evidence = row.get("evidence")
        require(isinstance(evidence, dict), item + " missing evidence")
        require(evidence.get("metric_source") == "ads_google_insights:type=3" and evidence.get("mapping_status") == "exact",
                item + " missing exact type3 material provenance")
        require(evidence.get("usd_status") == "verified", item + " USD must be verified")
        require(evidence.get("rule_a_available") is False and evidence.get("rule_a_pass") is False
                and evidence.get("rule_b_pass") is True, item + " inconsistent A/B evidence")
        assert_metric(evidence.get("material_ctr"), expected_metrics(row)["ctr"], item + ".evidence.material_ctr", 8)
        assert_metric(evidence.get("platform_ctr"), expected_metrics(benchmark)["ctr"], item + ".evidence.platform_ctr", 8)
        require(evidence.get("material_cpa") is None, item + " missing asset AF cannot produce CPA")


def monthly_summary(month, before, after, audits):
    old_counts = Counter(row["channel"] for row in before.values())
    new_counts = Counter(row["channel"] for row in after.values())
    gaps = []
    for app in APPS:
        audit = audits[("Google", app)]
        gap = {"app": app, "selected_count": audit.get("selected_count")}
        for key in ("platform_spend", "mapping_gap_spend", "mapping_coverage"):
            value = audit.get(key)
            gap[key] = None if value is None else str(value)
        for key in ("fx_missing_rows", "platform_fx_missing_rows", "incomplete_material_count", "baseline_missing_account_days"):
            gap[key] = audit.get(key)
        gaps.append(gap)
    return {
        "month": month,
        "baseline_channel_counts": {key: old_counts[key] for key in CHANNELS},
        "candidate_channel_counts": {key: new_counts[key] for key in CHANNELS},
        "preserved_non_google_rows": sum(value for key, value in old_counts.items() if key != "Google"),
        "google_gaps": gaps,
    }


def validate_upgrade(baseline_dir, candidate_dir):
    result = {"status": "FAIL", "read_only": True, "months": [], "errors": []}
    try:
        baseline, candidate = Path(baseline_dir).resolve(), Path(candidate_dir).resolve()
        require(baseline != candidate, "baseline and candidate directories must be different")
        old_manifest, old_entries, old_digest = load_manifest(baseline, 1, "baseline")
        new_manifest, new_entries, new_digest = load_manifest(candidate, 2, "candidate")
        result.update({"baseline_data_version": old_manifest["data_version"],
                       "candidate_data_version": new_manifest["data_version"],
                       "baseline_manifest_sha256": old_digest, "candidate_manifest_sha256": new_digest})
    except (ValidationError, OSError) as exc:
        result["errors"].append(str(exc))
        return result

    for month in MONTHS:
        summary = {"month": month}
        try:
            old, old_sha = load_month(baseline, old_manifest, old_entries[month], 1, "baseline")
            new, new_sha = load_month(candidate, new_manifest, new_entries[month], 2, "candidate")
            old_indexes = {key: index_records(old, key, month, "baseline") for key in ("rows", "benchmarks", "audits")}
            new_indexes = {key: index_records(new, key, month, "candidate") for key in ("rows", "benchmarks", "audits")}
            summary = monthly_summary(month, old_indexes["rows"], new_indexes["rows"], new_indexes["audits"])
            summary.update({"baseline_payload_sha256": old_sha, "candidate_payload_sha256": new_sha})
            for key in old_indexes:
                compare_non_google(old_indexes[key], new_indexes[key], month + "." + key)
            for key, row in new_indexes["rows"].items():
                validate_metrics(row, month + ".rows." + str(key))
            for scope, benchmark in new_indexes["benchmarks"].items():
                expected = validate_metrics(benchmark, month + ".benchmarks." + str(scope))
                assert_metric(required_field(benchmark, "ctr", "benchmark"), expected["ctr"], str(scope) + ".ctr", 8)
                assert_metric(required_field(benchmark, "cpa", "benchmark"), expected["d0_cpa"], str(scope) + ".cpa", 6)
                rows = [row for key, row in new_indexes["rows"].items() if key[:2] == scope]
                audit = new_indexes["audits"][scope]
                require(integer(audit.get("selected_count"), str(scope) + ".selected_count") == len(rows),
                        "%s audit selected_count mismatch" % (scope,))
                if scope[0] == "Google":
                    validate_google_scope(scope, benchmark, audit, rows)
            summary["status"] = "PASS"
        except ValidationError as exc:
            summary.update({"status": "FAIL", "error": str(exc)})
            result["errors"].append(month + ": " + str(exc))
        result["months"].append(summary)

    # Refuse a result assembled across two publications even if the individual
    # immutable month files happened to pass their checks.
    for root, expected_digest, side in ((baseline, old_digest, "baseline"), (candidate, new_digest, "candidate")):
        try:
            _manifest, digest = read_json(root / "latest.json")
            require(digest == expected_digest, side + " latest.json changed during validation")
        except ValidationError as exc:
            result["errors"].append(str(exc))
    result["status"] = "FAIL" if result["errors"] else "PASS"
    result["month_count"] = len(result["months"])
    result["limitations"] = "Offline artifact validation only; no MySQL, live FX, source-ID existence, media, or browser verification."
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True, help="frozen schema1 public directory")
    parser.add_argument("--candidate-dir", type=Path, required=True, help="staged schema2 public directory")
    args = parser.parse_args(argv)
    result = validate_upgrade(args.baseline_dir, args.candidate_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

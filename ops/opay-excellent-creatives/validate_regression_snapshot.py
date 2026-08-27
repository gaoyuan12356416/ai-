#!/usr/bin/env python3
"""Compare a generated month payload with the frozen 2026-07 signature."""

import argparse
import collections
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE / "fixtures" / "2026-07-regression.json"


def payload_signature(payload, non_google_only=False):
    rows = sorted(
        [row for row in payload.get("rows", []) if not non_google_only or row["channel"] != "Google"],
        key=lambda row: (row["channel"], row["app"], row["custom_source_id"]),
    )
    audits = sorted(
        [item for item in payload.get("audits", []) if not non_google_only or item["channel"] != "Google"], key=lambda item: (item["channel"], item["app"])
    )
    return {
        "month": payload.get("month"),
        "keyword_config_version": payload.get("keyword_config_version"),
        "row_count": len(rows),
        "channel_app_counts": dict(
            sorted(collections.Counter(row["channel"] + "|" + row["app"] for row in rows).items())
        ),
        "rule_counts": dict(
            sorted(collections.Counter(row["selection_rule"] for row in rows).items())
        ),
        "source_status_counts": dict(
            sorted(collections.Counter(row["source_status"] for row in rows).items())
        ),
        "thumbnail_status_counts": dict(
            sorted(collections.Counter(row["thumbnail_status"] for row in rows).items())
        ),
        "keyword_status_counts": dict(
            sorted(collections.Counter(row["selling_point_status"] for row in rows).items())
        ),
        "selected_rows": [
            {
                "channel": row["channel"],
                "app": row["app"],
                "custom_source_id": row["custom_source_id"],
                "spend": row["spend"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "installs": row["installs"],
                "af_d0": row["af_d0_first_transactions"],
                "selection_rule": row["selection_rule"],
            }
            for row in rows
        ],
        "audits": [
            {
                "channel": item["channel"],
                "app": item["app"],
                "selected_count": item["selected_count"],
                "platform_spend": item["platform_spend"],
                "mapping_coverage": item["mapping_coverage"],
                "mapping_gap_spend": item["mapping_gap_spend"],
                "rule_a_available": item["rule_a_available"],
                "af_total": item["af_total"],
                "af_mapped": item["af_mapped"],
                "ambiguous_ad_days": item["ambiguous_ad_days"],
            }
            for item in audits
        ],
    }


def expected_signature(fixture):
    return {key: fixture[key] for key in payload_signature({}).keys()}


def validate(payload, fixture, non_google_only=False):
    actual = payload_signature(payload, non_google_only=non_google_only)
    expected = expected_signature(fixture)
    if non_google_only:
        if any(row["channel"] == "Google" for row in expected["selected_rows"]):
            raise ValueError("non-Google comparison requires the V1 baseline fixture")
        expected["audits"] = [item for item in expected["audits"] if item["channel"] != "Google"]
    if actual != expected:
        raise AssertionError(
            "2026-07 regression mismatch\nexpected=%s\nactual=%s"
            % (
                json.dumps(expected, ensure_ascii=False, sort_keys=True),
                json.dumps(actual, ensure_ascii=False, sort_keys=True),
            )
        )
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--non-google-only", action="store_true", help="compare unchanged Meta/TikTok against the frozen V1 baseline")
    args = parser.parse_args(argv)
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    signature = validate(payload, fixture, non_google_only=args.non_google_only)
    print(
        json.dumps(
            {"status": "PASS", "month": signature["month"], "row_count": signature["row_count"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

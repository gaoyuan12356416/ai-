"""Verify API, CSV and cards against one private source snapshot, without network."""
import argparse
from contextlib import closing
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.social_analytics.facebook import collect
from features.social_analytics.report import csv_export, FIELDS
from features.social_analytics.routes import build_report
from features.social_daily_report.collector import present
from features.social_daily_report.report import build_cards, preview_html


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rehearse(snapshot, output):
    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    if output.exists() or not snapshot.is_dir():
        raise ValueError("new_output_required")
    output.mkdir(parents=True)
    (output / ".gitignore").write_text("*\n", encoding="utf-8")
    before = {p.name: digest(p) for p in snapshot.glob("*.sqlite3")}
    ledger = output / "attribution.sqlite3"
    first = collect(snapshot / "fb.sqlite3", snapshot / "main.sqlite3", ledger, mode="full")
    second = collect(snapshot / "fb.sqlite3", snapshot / "main.sqlite3", ledger, mode="incremental")
    assert before == {p.name: digest(p) for p in snapshot.glob("*.sqlite3")}, "source_changed"
    settings = dict(JOB_DB_PATH=str(snapshot / "main.sqlite3"),
                    SOCIAL_ANALYTICS_X_DB_PATH=str(snapshot / "x.sqlite3"),
                    SOCIAL_ANALYTICS_FB_DB_PATH=str(snapshot / "fb.sqlite3"),
                    SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB=str(ledger))
    metrics = json.loads((snapshot / "metrics.json").read_text(encoding="utf-8"))

    def fixed_source(app, start, end, platform, site=None):
        assert start == end == "2026-10-19"
        site = site or {"youtube": "2284", "x": "2116"}[platform]
        return [row for row in metrics if row["platform"] == platform and row["site_id"] == site], "fixed-snapshot"

    query = {"start": ["2026-10-19"], "end": ["2026-10-19"],
             "group_by": ["owner,platform"], "page_size": ["100"]}
    with patch("features.social_analytics.routes.source", side_effect=fixed_source):
        report = build_report(settings, {"role": "admin", "_system_report": True}, query)
        with closing(sqlite3.connect(snapshot / "main.sqlite3")) as c:
            creator = c.execute("SELECT user_id,tenant_key FROM drama_admin_user WHERE user_id='892fd2e8'").fetchone()
        assert creator
        actor = {"user_id": creator[0], "tenant_key": creator[1], "role": "admin"}
        web_report = build_report(settings, actor, query)
        personal_report = build_report(settings, dict(actor, role="user"), query)
    quality = report["quality"]["platforms"]
    for platform, value in quality.items():
        for site, source in value["sources"].items():
            for metric in FIELDS:
                if source["source_totals"][metric] is None:
                    assert not any(row["site_id"] == site for row in metrics)
                    assert source["state"] == "unavailable"
                else:
                    assert source["source_totals"][metric] == sum(source[bucket + "_totals"][metric]
                        for bucket in ("attributed", "unmatched", "excluded")), (site, metric)
    assert quality["facebook"]["source_totals"]["revenue_cents"] == 1181542
    assert report["platform_totals"]["facebook"]["revenue_cents"] == 593983
    assert quality["facebook"]["unmatched_totals"]["revenue_cents"] == 587559
    assert report["platform_totals"]["youtube"]["revenue_cents"] == 91037
    assert report["platform_totals"]["x"]["revenue_cents"] == 78042
    assert report["platform_totals"]["facebook"]["post_count"] is None
    assert report["platform_totals"]["facebook"]["repost_count"] is None
    assert report["totals"]["revenue_cents"] == 763062
    assert web_report["totals"]["revenue_cents"] == report["totals"]["revenue_cents"]
    fb_rows = [r for r in report["all_rows"] if r["platform"] == "facebook"]
    assert {r["owner"] for r in fb_rows if r["revenue_cents"]} == {"892fd2e8"}
    assert all(r["revenue_cents"] is None for r in fb_rows if r["owner"] != "892fd2e8")
    assert all(r["owner"] == creator[0] for r in personal_report["all_rows"])
    daily = present(report, "2026-10-19")
    assert daily["totals"] == {k: report["totals"].get(k) for k in daily["totals"]}
    assert sum(r["revenue_cents"] or 0 for r in daily["rows"]) == report["totals"]["revenue_cents"]
    csv_bytes = csv_export(report)
    lines = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    header = next(i for i, row in enumerate(lines) if "收入（USD）" in row and "人员" in row)
    revenue_column = lines[header].index("收入（USD）")
    values = []
    assert len(lines[header + 1:]) == len(report["all_rows"])
    for line, row in zip(lines[header + 1:], report["all_rows"]):
        if row["revenue_cents"] is None:
            assert line[revenue_column] == "未同步"
        else:
            amount = Decimal(line[revenue_column])
            assert int(amount * 100) == row["revenue_cents"]
            values.append(amount)
    assert int(sum(values) * 100) == report["totals"]["revenue_cents"]
    cards = build_cards(daily, "isolated-preview")
    write(output / "report.json", report)
    write(output / "web-report.json", web_report)
    write(output / "daily.json", daily)
    write(output / "cards.json", cards)
    (output / "report.csv").write_bytes(csv_bytes)
    (output / "preview.html").write_text(preview_html(cards), encoding="utf-8")
    summary = dict(first_collection=first, repeat_collection=second,
                   totals=report["totals"], platform_totals=report["platform_totals"],
                   source_revenue_cents={key: value["source_totals"]["revenue_cents"] for key, value in quality.items()},
                   unmatched_revenue_cents={key: value["unmatched_totals"]["revenue_cents"] for key, value in quality.items()},
                   fb_source_states={site: value["state"] for site, value in quality["facebook"]["sources"].items()},
                   source_unchanged=True, api_csv_daily_consistent=True, publisher_calls=0, delivered_messages=0,
                   snapshot_manifest_sha256=digest(snapshot / "manifest.json"))
    write(output / "acceptance.json", summary)
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rehearse(args.snapshot, args.output)

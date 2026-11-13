#!/usr/bin/env python3
"""Read-only full-window equivalence between the frozen SQLite source and ads_ai."""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.metric_mysql import MySQLMetricStore, amount_text, config_from_file, exact_add


def summarize(rows):
    digest, spend, revenue = hashlib.sha256(), Decimal(0), Decimal(0)
    for key, pair in sorted(rows.items()):
        a, b = amount_text(pair[0]), amount_text(pair[1])
        digest.update(json.dumps([*key, a, b], ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        spend, revenue = exact_add(spend, pair[0]), exact_add(revenue, pair[1])
    return {"material_rows": len(rows), "sha256": digest.hexdigest(), "spend": str(spend), "af_revenue0": str(revenue)}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--source", required=True); parser.add_argument("--output", required=True)
    parser.add_argument("--config", default="/etc/codex-private-storage-archive.json"); args = parser.parse_args()
    store = MySQLMetricStore(config_from_file(args.config)); proof = []
    with closing(sqlite3.connect(Path(args.source).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row; db.execute("PRAGMA query_only=ON"); db.execute("BEGIN")
        end = date.fromisoformat(db.execute("SELECT MAX(metric_date) FROM fb_auto_metric_active_pointer").fetchone()[0])
        for days in (3, 30):
            dates = [(end - timedelta(days=index)).isoformat() for index in range(days)]
            marks = ",".join("?" for _ in dates)
            pointers = {row["metric_date"]: row["generation_id"] for row in db.execute(
                "SELECT * FROM fb_auto_metric_active_pointer WHERE platform=0 AND product='Dramawave' AND metric_date IN (" + marks + ")", dates)}
            ids = tuple(pointers[day] for day in dates); expected = {}
            for row in db.execute("SELECT content_id,material_id,spend,af_revenue0 FROM fb_auto_metric_daily WHERE generation_id IN (" + marks + ")", ids):
                key = (row["content_id"], row["material_id"]); old = expected.get(key, (Decimal(0), Decimal(0)))
                expected[key] = (exact_add(old[0], Decimal(row["spend"])), exact_add(old[1], Decimal(row["af_revenue0"])))
            actual = store.load_metric_window(product="Dramawave", platform=0, dates=dates)
            actual_rows = {key: (row.spend, row.revenue) for key, row in actual.by_material.items()}
            assert actual.generation_ids == ids, "generation window mismatch"
            assert actual_rows == expected, "material metric window mismatch"
            proof.append({"days": days, "start": dates[-1], "end": dates[0], "generation_ids": list(ids),
                          **summarize(expected), "exact_match": True})
    Path(args.output).write_text(json.dumps({"ok": True, "windows": proof}, indent=2))
    print(json.dumps({"ok": True, "windows": [{key: value for key, value in row.items() if key != "generation_ids"} for row in proof]}))


if __name__ == "__main__": main()

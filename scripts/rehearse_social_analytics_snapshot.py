"""Rehearse the real migration exclusively on a downloaded private snapshot."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.social_analytics.adapters import read_x, read_youtube
from features.social_analytics.report import csv_export, parameters, summarize
from features.x_posts.attribution import Resolver
from features.x_posts.attribution_migration import create_manifest, run


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(path, *, urls=False):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        data = {}
        for table in ("x_post_queue", "x_post_publish_log", "x_post_manual_run", "x_post_repost_ledger"):
            records = [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY id")]
            if table == "x_post_publish_log" and not urls:
                for row in records:
                    row.pop("long_url")
            data[table] = records
    return hashlib.sha256(canonical(data)).hexdigest()


def announce(stage, **kwargs):
    print(json.dumps(dict(stage=stage, **kwargs), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source, output = Path(args.snapshot).resolve(), Path(args.output).resolve()
    if not source.is_dir() or output.exists() or output == source:
        raise ValueError("new_isolated_output_required")
    output.mkdir(parents=True)
    (output / ".gitignore").write_text("*\n", encoding="utf-8")
    database = output / "x.sqlite3"
    shutil.copy2(source / "x.sqlite3", database)
    source_html = source / "s2l"
    if not source_html.is_dir():
        source_html = Path(json.loads((source / "manifest.json").read_text(encoding="utf-8"))["short_links_root"])
    shutil.copytree(source_html, output / "s2l")
    # The allowlisted snapshot omits production schema constraints. Restore
    # its ID lookup performance in the writable fixture, not in the original.
    with closing(sqlite3.connect(database)) as connection, connection:
        for table in ("x_post_queue", "x_post_publish_log", "x_post_manual_run"):
            connection.execute("CREATE UNIQUE INDEX fixture_%s_id ON %s(id)" % (table, table))
    mapping = json.loads((source / "user_mapping.json").read_text(encoding="utf-8"))
    by_email, by_id = defaultdict(set), defaultdict(set)
    for row in mapping:
        by_email[row["email"]].add(row["sub_user_id"])
        by_id[row["sub_user_id"]].add(row["email"])
    resolver = Resolver(database, source / "auto.sqlite3", source / "main.sqlite3",
                        lambda email: sorted(by_email[email]), lambda sub_id: sorted(by_id[sub_id]))
    before, before_urls = fingerprint(database), fingerprint(database, urls=True)
    state = output / "migration"
    manifest = create_manifest(database, output / "s2l", resolver, state)
    announce("manifest_created", **manifest)
    manifest_data = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    failures = []
    first_id = manifest_data["records"][0]["log"]["id"]

    def fault(log_id, phase):
        if log_id == first_id and phase == "file_applied":
            raise RuntimeError("deliberate_rehearsal_interruption")

    try:
        run(state, manifest["manifest_sha256"], fault=fault)
        raise AssertionError("fault_not_triggered")
    except RuntimeError as exc:
        assert str(exc) == "deliberate_rehearsal_interruption"
    assert fingerprint(database) == before
    announce("interruption_verified")
    applied = run(state, manifest["manifest_sha256"])
    assert fingerprint(database) == before
    announce("full_apply_verified", **applied)
    repeat = run(state, manifest["manifest_sha256"])
    assert fingerprint(database) == before
    announce("idempotent_repeat_verified", **repeat)
    catalog = read_youtube(source / "main.sqlite3") + read_x(database)
    metrics = json.loads((source / "metrics.json").read_text(encoding="utf-8"))
    report = summarize(catalog, metrics, {"role": "admin", "_system_report": True},
                       parameters({"start": ["2026-10-19"], "end": ["2026-10-19"]}), "private-fixed-snapshot")
    (output / "report.json").write_bytes(canonical(report))
    (output / "report.csv").write_bytes(csv_export(report))
    contexts = [record["context"] for record in manifest_data["records"]]
    counts = {
        "link_types": dict(Counter(context["link_type"] for context in contexts)),
        "fallback_reasons": dict(Counter(context["fallback_reason"] for context in contexts)),
        "sub_user_ids": dict(Counter(context["sub_user_id"] for context in contexts)),
    }
    (output / "attribution-counts.json").write_bytes(canonical(counts))
    summary = {
        "source_revenue_cents": {key: value["source_totals"]["revenue_cents"]
                                 for key, value in report["quality"]["platforms"].items()},
        "attributed_revenue_cents": {key: value["revenue_cents"] for key, value in report["platform_totals"].items()},
        "unmatched_revenue_cents": {key: value["unmatched_totals"]["revenue_cents"]
                                    for key, value in report["quality"]["platforms"].items()},
        "excluded_revenue_cents": {key: value["excluded_totals"]["revenue_cents"]
                                   for key, value in report["quality"]["platforms"].items()},
        "published": {key: {field: value[field] for field in ("published_count", "post_count", "repost_count")}
                      for key, value in report["platform_totals"].items()},
        "quality_complete": report["quality"]["complete"],
        "publication_conflicts": report["quality"]["publication_conflicts"],
    }
    announce("fixed_snapshot_report_verified", **summary)
    rollback = run(state, manifest["manifest_sha256"], rollback=True)
    assert fingerprint(database) == before
    assert fingerprint(database, urls=True) == before_urls
    for path in (output / "s2l").glob("*.html"):
        assert path.read_bytes() == (source_html / path.name).read_bytes()
    announce("full_rollback_verified", **rollback)
    # Leave a verified migrated fixture for daily/API/browser acceptance.
    run(state, manifest["manifest_sha256"])
    assert fingerprint(database) == before
    result = dict(manifest=manifest, summary=summary, attribution_counts=counts,
                  publication_facts_sha256=before, original_facts_and_urls_sha256=before_urls,
                  output=str(output), interruption_resume=True, idempotence=True, rollback=True)
    (output / "acceptance.json").write_bytes(canonical(result))
    announce("rehearsal_complete", output=str(output), publication_facts_sha256=before)


if __name__ == "__main__":
    main()

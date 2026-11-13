#!/usr/bin/env python3
"""Copy live/referenced metric generations; retain superseded history in the source archive."""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_auto_posts.metric_mysql import MySQLMetricStore, config_from_file, exact_add


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def integer_ids(value):
    if isinstance(value, dict):
        return {item for child in value.values() for item in integer_ids(child)}
    if isinstance(value, list):
        return {item for child in value for item in integer_ids(child)}
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return {value}
    return set()


def json_metric_refs(value):
    result = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if "metric_gener" in key:
                result.update(integer_ids(child))
            else:
                result.update(json_metric_refs(child))
    elif isinstance(value, list):
        for child in value: result.update(json_metric_refs(child))
    return result


def collect_references(paths):
    result, evidence = set(), []
    for path in paths:
        with closing(readonly(path)) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                table_sql = '"' + table.replace('"', '""') + '"'
                columns = db.execute("PRAGMA table_info(" + table_sql + ")").fetchall()
                names = {column["name"] for column in columns}
                filters = []
                if "pipeline_job" in tables:
                    # Shared dispatch also has X/TT/YouTube generations. Only FB
                    # references belong to this separate metric identity space.
                    if "channel" in names: filters.append("channel='fb'")
                    elif "source_key" in names: filters.append("source_key LIKE 'fb:%'")
                    elif "job_id" in names:
                        filters.append("job_id IN (SELECT id FROM pipeline_job WHERE channel='fb')")
                for column in columns:
                    name = column["name"]
                    direct = "metric_gener" in name
                    if not direct and "json" not in name: continue
                    quoted = '"' + name.replace('"', '""') + '"'
                    query = "SELECT " + quoted + " FROM " + table_sql
                    predicates = filters + ([] if direct else ["INSTR(" + quoted + ",'metric_gener')>0"])
                    if predicates: query += " WHERE " + " AND ".join(predicates)
                    found, rows = set(), 0
                    for row in db.execute(query):
                        if not row[0]: continue
                        try: value = json.loads(row[0])
                        except (ValueError, TypeError):
                            if direct: raise RuntimeError("invalid metric reference JSON")
                            continue
                        ids = integer_ids(value) if direct else json_metric_refs(value)
                        if ids: rows += 1; found.update(ids)
                    if found:
                        evidence.append({"database": str(path), "table": table, "column": name,
                                         "rows": rows, "generation_ids": sorted(found)})
                        result.update(found)
    return result, evidence


def plan(db, reference_paths):
    headers = [dict(row) for row in db.execute("SELECT * FROM fb_auto_metric_generation ORDER BY id")]
    pointers = [dict(row) for row in db.execute("SELECT * FROM fb_auto_metric_active_pointer ORDER BY platform,metric_date,product")]
    references, evidence = collect_references(reference_paths)
    selected = references | {int(row["generation_id"]) for row in pointers}
    by_id = {int(row["id"]): row for row in headers}
    if selected - set(by_id): raise RuntimeError("missing referenced metric generations")
    if any(by_id[item]["status"] != "ready" for item in selected): raise RuntimeError("selected generation is not ready")
    data = {"selected_headers": [by_id[item] for item in sorted(selected)], "pointers": pointers,
            "references": evidence, "source_generation_count": len(headers),
            "source_rows": sum(row["row_count"] for row in headers),
            "selected_rows": sum(by_id[item]["row_count"] for item in selected),
            "archived_only_generation_ids": sorted(set(by_id) - selected),
            "id_mapping": "identity: old generation_id is preserved"}
    data["plan_sha256"] = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return data


def install(config):
    store = MySQLMetricStore(config)
    sql = (ROOT / "deploy/fb_auto_metric_ads_ai.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    with closing(store.writer_factory()) as db, db.cursor() as cursor:
        cursor.execute("SELECT DATABASE(),@@read_only")
        if tuple(cursor.fetchone()) != ("ads_ai", 0): raise RuntimeError("wrong DDL endpoint")
        for statement in sql.split(";"):
            if statement.strip(): cursor.execute(statement)
        db.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "install", "migrate", "verify"))
    parser.add_argument("--source", default="/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3")
    parser.add_argument("--reference-db", action="append", default=[])
    parser.add_argument("--config", default="/etc/codex-private-storage-archive.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.mode == "install":
        install(config_from_file(args.config)); print(json.dumps({"ok": True, "ddl_tables": 3})); return
    refs = args.reference_db or ["/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3",
                                  "/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3"]
    with closing(readonly(args.source)) as source:
        manifest = plan(source, refs)
        Path(args.output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.mode == "plan":
            print(json.dumps({key: manifest[key] for key in ("plan_sha256", "source_generation_count", "source_rows", "selected_rows")} | {"selected_generations": len(manifest["selected_headers"]), "active_days": len(manifest["pointers"])})); return
        store, proof = MySQLMetricStore(config_from_file(args.config)), []
        for index, header in enumerate(manifest["selected_headers"]):
            generation_id = int(header["id"])
            if args.mode == "migrate":
                rows = source.execute("SELECT content_id,material_id,spend,af_revenue0 FROM fb_auto_metric_daily "
                                      "WHERE generation_id=? ORDER BY content_id,LENGTH(material_id),material_id", (generation_id,))
                result = store.import_generation(header, rows)
            else:
                stored = store.reader.generation(generation_id=generation_id)
                if not stored or stored["status"] != "ready" or stored["checksum"] != header["checksum"] or stored["row_count"] != header["row_count"]:
                    raise RuntimeError("metric generation metadata mismatch")
                result = dict(store.verify(generation_id, stored["row_count"], stored["storage_checksum"]), id=generation_id)
            proof.append(result)
            if (index + 1) % 10 == 0:
                print(json.dumps({"verified_generations": index + 1, "total": len(manifest["selected_headers"])}), flush=True)
        if args.mode == "migrate":
            by_id = {row["id"]: row for row in manifest["selected_headers"]}
            with closing(store.writer_factory()) as db, db.cursor() as cursor:
                for pointer in manifest["pointers"]:
                    store.activate(cursor, by_id[pointer["generation_id"]], pointer["activated_at_utc"])
                db.commit()
        import time
        deadline = time.monotonic() + 30
        while True:
            target_pointers = store.reader.query(["SELECT HEX(JSON_OBJECT('platform',platform,'metric_date',metric_date,'product',product,"
                                                  "'generation_id',generation_id,'activated_at_utc',activated_at_utc)) FROM ads_ai.fb_auto_metric_active_pointer ORDER BY platform,metric_date,product"])
            if target_pointers == manifest["pointers"] or time.monotonic() >= deadline: break
            time.sleep(1)
        if target_pointers != manifest["pointers"]: raise RuntimeError("metric pointer verification mismatch")
        spend, revenue = Decimal(0), Decimal(0)
        for row in proof:
            spend = exact_add(spend, Decimal(row["spend"])); revenue = exact_add(revenue, Decimal(row["af_revenue0"]))
        manifest.update(verified=True, proof=proof, migrated_rows=sum(row["rows"] for row in proof),
                        migrated_spend=str(spend), migrated_af_revenue0=str(revenue), pointers_verified=len(target_pointers))
        Path(args.output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: manifest[key] for key in ("verified", "migrated_rows", "migrated_spend", "migrated_af_revenue0", "pointers_verified")}))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "code": getattr(exc, "code", "metric_migration_failed")}))
        raise SystemExit(1)

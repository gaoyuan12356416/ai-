#!/usr/bin/env python3
"""Explicit, resumable migration operations. No publisher APIs are called."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from features.cover_material_source import (Catalog, Client, Ledger, ROOT, SOURCE_DB,
                                           encoded, identity, reconcile, submit, utcnow)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded(data), encoding="utf-8")
    os.chmod(str(temp), 0o600)
    os.replace(str(temp), str(path))


def read_manifest(path):
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = hashlib.sha256(encoded(body["sources"]).encode()).hexdigest()
    if body["sha256"] != expected:
        raise ValueError("manifest_hash_mismatch")
    return body


def freeze(path, sources, issues, cutoff):
    body = dict(cutoff=cutoff, window_hours=168, created_at=utcnow(), sources=sources, issues=issues,
                sha256=hashlib.sha256(encoded(sources).encode()).hexdigest())
    if Path(path).exists():
        old = read_manifest(path)
        if old["sha256"] != body["sha256"] or old["cutoff"] != cutoff:
            raise ValueError("immutable_manifest_already_exists")
        return old
    write_json(path, body)
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("discover", "run", "status", "reconcile", "export"))
    parser.add_argument("--root", default=os.environ.get("AI_COVER_SOURCE_ROOT", ROOT))
    parser.add_argument("--source-db", default=SOURCE_DB)
    parser.add_argument("--sync-db", default="/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3")
    parser.add_argument("--manifest")
    parser.add_argument("--cutoff", help="Required fixed ISO timestamp with timezone for discover")
    parser.add_argument("--adoption-receipts", help="Optional audited key-to-receipt JSON; default reads legacy trial receipts")
    parser.add_argument("--max-new", type=int, default=50)
    parser.add_argument("--keys", nargs="*", help="Exact content_sign allowlist for representative canaries")
    args = parser.parse_args()
    env = dict(os.environ)
    catalog = Catalog(env)
    if args.command == "discover":
        from history import discover
        if not args.cutoff or not args.manifest:
            raise ValueError("explicit_cutoff_and_manifest_required")
        cutoff = dt.datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("timezone_required")
        adoption = json.loads(Path(args.adoption_receipts).read_text(encoding="utf-8")) if args.adoption_receipts else None
        sources, issues = discover(args.cutoff, args.source_db, args.sync_db, catalog, adoption)
        body = freeze(args.manifest, sources, issues, args.cutoff)
        print(encoded(dict(cutoff=body["cutoff"], sha256=body["sha256"],
                           count=len(sources), issues=issues,
                           samples=[dict(key=identity(s), app_id=s["app_id"], source_kind=s["source_kind"])
                                    for s in sources[:5]])))
        return
    ledger = Ledger(args.root)
    try:
        if args.command == "run":
            if not args.manifest:
                raise ValueError("frozen_manifest_required")
            manifest = read_manifest(args.manifest)
            sources = manifest["sources"]
            if args.keys:
                selected = set(args.keys)
                sources = [s for s in sources if identity(s) in selected]
                if len(sources) != len(selected):
                    raise ValueError("canary_key_not_unique_in_manifest")
            client = Client(env)
            count = 0
            for source in sources:
                key = identity(source)
                old = ledger.get(key)
                if old and old["state"] == "confirmed":
                    continue
                if count >= min(50, max(1, args.max_new)):
                    break
                count += 1
                row = submit(source, ledger, catalog, client)
                for delay in (2, 5, 10):
                    if row["state"] not in ("accepted", "unknown"):
                        break
                    time.sleep(delay)
                    row = reconcile(ledger, catalog, row)
                print(encoded(dict(key=key, state=row["state"], api_id=row["api_id"],
                                   final_id=row["final_id"], error=row["error"])), flush=True)
                if row["state"] in ("rejected", "blocked"):
                    raise RuntimeError("representative_or_batch_validation_failed:" + row["error"])
                # No immediate retry on any uncertain outcome.
                if row["state"] != "confirmed":
                    raise RuntimeError("outcome_requires_reconciliation")
        elif args.command == "reconcile":
            rows = [dict(r) for r in ledger.db.execute(
                "SELECT * FROM uploads WHERE state IN ('accepted','unknown') ORDER BY created_at LIMIT 50")]
            for row in rows:
                reconcile(ledger, catalog, row)
        elif args.command == "export":
            rows = [dict(r) for r in ledger.db.execute("SELECT * FROM uploads ORDER BY created_at,key")]
            result = []
            for row in rows:
                source = json.loads(row["source_json"])
                result.append(dict(key=row["key"], state=row["state"], source_key=source["source_key"],
                                   app_id=source["app_id"], content_id=source["content_id"],
                                   legacy_final_ids=source.get("legacy_final_ids", []),
                                   api_id=row["api_id"], final_id=row["final_id"], attempts=row["attempts"],
                                   error=row["error"], legacy_material_id=source.get("legacy_material_id"),
                                   old_accepted_at=source.get("legacy_accepted_at", "")))
            write_json(Path(args.root) / "id-mapping.json", dict(at=utcnow(), counts=ledger.summary(), rows=result))
        print(encoded(dict(counts=ledger.summary(), checked_at=utcnow())))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()

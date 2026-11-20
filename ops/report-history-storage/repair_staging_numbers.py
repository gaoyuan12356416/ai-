"""Repair only confirmed JSON float rendering loss in unverified staging pages.

No DDL, snapshot replacement, or updates to pages used by verified snapshots.
Run without --apply first. The supplied plan must be a subset of the frozen plan.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import time

import history as h

JOB = h.BASE / "report-storage-20261120"


def rendering_loss(expected, actual):
    if not isinstance(expected, list) and type(expected) is type(actual) and expected == actual:
        return True
    if type(expected) is float and type(actual) is float:
        return math.isfinite(actual) and abs(expected - actual) <= 32 * max(math.ulp(expected), math.ulp(actual))
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(rendering_loss(a, b) for a, b in zip(expected, actual))
    return False


def prepare(reader, item):
    path = h.BASE / item["path"]
    h.check(h.match(item["path"]) == "hotdrama_report", "repair_scope_mismatch")
    h.check(path.resolve() == path and path.is_file(), "repair_path_changed")
    rows, kind, meta, digest, st = h.probe_rows(path, "hotdrama_report")
    h.check((st.st_ino, st.st_size, st.st_mtime_ns) == (item["inode"], item["size"], item["mtime_ns"]), "frozen_source_changed")
    h.check(kind == "positional", "repair_format_mismatch")
    cols = sorted({k for row in rows for k in row})
    pages, total = h.pack_pages(rows, "hotdrama_report", cols)
    sid = h.sha(("hotdrama_report\0" + item["path"] + "\0" + digest).encode())
    header = h.verified_view(reader, sid)
    h.check(header and header[8] == "staging", "repair_requires_staging")
    h.confirm_header(header, ("hotdrama_report", item["path"], digest, st.st_size, kind, cols, meta, [p[0] for p in pages], len(rows), len(pages), total))
    repairs = []
    missing_pages = 0
    with reader.cursor(buffered=True) as q:
        for chunk in h.group(pages, 12):
            fetched = {r[0].lower(): r for r in h.fetch_pages(q, [p[0] for p in chunk], True)}
            h.check(set(fetched) <= {p[0] for p in chunk}, "repair_unexpected_pages")
            missing_pages += len({p[0] for p in chunk} - set(fetched))
            for p in chunk:
                if p[0] not in fetched:
                    continue  # migrate inserts only these reconciled missing IDs.
                row = fetched[p[0]]
                h.check(row[1] == "hotdrama_report" and row[2] == p[1] and
                        (str(row[3]) if row[3] else None) == p[2] and
                        (str(row[4]) if row[4] else None) == p[3] and
                        list(row[5:10]) == list(p[4:9]) and row[-2].lower() == p[-2], "repair_non_payload_mismatch")
                if h.sha(h.encoded(h.normalize(json.loads(row[-1]))).encode()) == p[-2]:
                    continue
                h.check(rendering_loss(json.loads(p[-1]), json.loads(row[-1])), "repair_not_float_rendering_loss")
                q.execute("SELECT HEX(snapshot_id) FROM " + h.SNAP + " WHERE status='verified' AND JSON_CONTAINS(page_ids,JSON_QUOTE(%s)) LIMIT 1", (p[0],))
                h.check(q.fetchone() is None, "repair_page_used_by_verified_snapshot")
                q.execute("SELECT rows_json,SHA2(CAST(rows_json AS CHAR),256) FROM " + h.PAGE + " WHERE page_id=UNHEX(%s)", (p[0],))
                raw, server_hash = q.fetchone()
                repairs.append(dict(page_id=p[0], records_sha256=p[-2], before_rows_json=raw,
                                    before_server_hash=server_hash, after_rows_json=h.storage_payload(p[-1])))
    return dict(path=item["path"], snapshot=sid, source_sha256=digest, pages=len(pages), missing_pages=missing_pages, repairs=repairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    frozen = {v["path"]: v for v in json.loads((JOB / "plans/history-v1.json").read_text())}
    plan = json.loads(args.plan.read_text())
    h.check(0 < len(plan) <= 2 and len({v["path"] for v in plan}) == len(plan), "repair_plan_size")
    h.check(all(v == frozen.get(v["path"]) for v in plan), "repair_plan_not_frozen")
    h.check(args.backup.resolve().parent == JOB / "handoff-cpu" and args.journal.resolve().parent == JOB / "receipts", "repair_evidence_scope")
    with (JOB / "status/numeric-repair.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with h.connection(False) as reader:
            prepared = [prepare(reader, v) for v in plan]
            print(h.encoded(dict(apply=args.apply, paths=[dict(path=v["path"], snapshot=v["snapshot"], repair_pages=len(v["repairs"]), missing_pages=v["missing_pages"]) for v in prepared])), flush=True)
            if not args.apply:
                return
            with args.backup.open("x") as backup:
                json.dump(prepared, backup, ensure_ascii=False)
                backup.flush(); os.fsync(backup.fileno())
            with h.connection(True) as writer, writer.cursor(buffered=True) as put:
                for target in prepared:
                    current = h.verified_view(reader, target["snapshot"])
                    h.check(current and current[8] == "staging", "repair_requires_staging")
                    for p in target["repairs"]:
                        put.execute("UPDATE " + h.PAGE + " SET rows_json=%s WHERE page_id=UNHEX(%s) AND records_sha256=UNHEX(%s) AND SHA2(CAST(rows_json AS CHAR),256)=%s AND NOT EXISTS (SELECT 1 FROM " + h.SNAP + " WHERE status='verified' AND JSON_CONTAINS(page_ids,JSON_QUOTE(%s))) LIMIT 1",
                                    (p["after_rows_json"], p["page_id"], p["records_sha256"], p["before_server_hash"], p["page_id"]))
                        h.check(put.rowcount == 1, "repair_concurrent_change")
                        for attempt in range(8):
                            with reader.cursor(buffered=True) as get:
                                view = h.fetch_pages(get, [p["page_id"]], True)
                            if view and h.sha(h.encoded(h.normalize(json.loads(view[0][-1]))).encode()) == p["records_sha256"]:
                                break
                            time.sleep(1 + attempt)
                        else:
                            raise ValueError("repair_replication_pending")
                # Reuse the original header/IDs and verify every page on the replica.
                for item in plan:
                    result = h.migrate(h.BASE / item["path"], "hotdrama_report", reader, writer)
                    h.journal(args.journal, dict(time=time.time(), path=item["path"], dataset="hotdrama_report", status="verified", **result))
                    print(h.encoded(dict(path=item["path"], status="verified", **result)), flush=True)


if __name__ == "__main__":
    main()

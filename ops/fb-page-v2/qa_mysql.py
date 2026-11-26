"""Replay an exported isolated audit sink through the production MySQL writer in a network=none container."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.fb_page_assignment_v2.audit import COLS, _check
from features.fb_page_assignment_v2.audit_sandbox import isolated_mysql_sink


def batches(db, requests=12):
    cursor = db.execute("SELECT " + ",".join(COLS) + " FROM audit ORDER BY task_id,item_index")
    rows, ids, last = [], 0, None
    for values in cursor:
        if values[0] != last:
            if ids >= requests:
                yield rows
                rows, ids = [], 0
            ids += 1
            last = values[0]
        row = list(values)
        for col in ("created_at","updated_at"):
            i = COLS.index(col)
            row[i] = datetime.fromisoformat(row[i])
        rows.append(tuple(row))
    if rows:
        yield rows


def verify_source(db):
    res = db.execute("PRAGMA quick_check").fetchone()
    if res != ("ok",):
        raise ValueError("source snapshot integrity check failed")
    for sql in ("SELECT 1 FROM qa_event LIMIT 0", "SELECT "+",".join(COLS)+" FROM audit LIMIT 0"):
        db.execute(sql)
    counts = Counter()
    for group, n in db.execute("SELECT CASE WHEN item_index>0 THEN 'decisions' WHEN "
                               "selection_stage='candidate_snapshot' THEN 'snapshots' WHEN "
                               "selection_stage='v2_request' THEN 'requests' ELSE 'unknown' END AS kind,"
                               "count(*) FROM audit GROUP BY kind"):
        counts[group] = n
    if counts["unknown"] or min(counts["decisions"],counts["requests"],counts["snapshots"]) < 1:
        raise ValueError("not a completed Page V2 QA export")
    return dict(counts)


def apply(archive, expected_hash, container, workers):
    h = hashlib.sha256()
    with archive.open("rb") as f:
        for part in iter(lambda:f.read(1024*1024),b""):
            h.update(part)
    if h.hexdigest() != expected_hash:
        raise ValueError("source archive SHA-256 differs")
    with closing(sqlite3.connect(archive.resolve().as_uri()+"?mode=ro&immutable=1",uri=True)) as db:
        counts = verify_source(db)
        sink = isolated_mysql_sink(container)
        def write(rows):
            expected = {(r[0],r[1]):_check(r) for r in rows}
            sink.write(rows,expected)
            return len(rows)
        start = time.monotonic()
        completed = committed_rows = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            running = []
            for rows in batches(db):
                running.append(pool.submit(write,rows))
                if len(running) >= workers:
                    committed_rows += running.pop(0).result()
                    completed += 1
            for future in running:
                committed_rows += future.result()
                completed += 1
        with sink.connection(role="write",deadline=time.monotonic()+25) as conn, conn.cursor() as c:
            c.execute("SELECT count(*),count(DISTINCT task_id),@@version FROM ads_ai.fb_drama_page_assignment_log")
            total, unique_tasks, version = c.fetchone()
        return {"ok":committed_rows==sum(counts.values())==total,
                "source_sha256":expected_hash,"container":container,"source_rows":counts,
                "database_rows":total,"database_unique_task_ids":unique_tasks, "mysql_version":version,
                "writer_workers":workers,"verified_batches":completed,"rows_checked":committed_rows,
                "wall_seconds":round(time.monotonic()-start,3),"transport":"unix_socket_network_none"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source",required=True,type=Path)
    parser.add_argument("--sha256",required=True)
    parser.add_argument("--container",required=True)
    parser.add_argument("--workers",type=int,choices=(1,2),default=2)
    parser.add_argument("--output",required=True,type=Path)
    args = parser.parse_args()
    with args.output.open("x",encoding="utf8") as out:
        try:
            result = apply(args.source,args.sha256,args.container,args.workers)
        except Exception as exc:
            json.dump({"ok":False,"reason":type(exc).__name__},out)
            raise
        json.dump(result,out)
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 2)

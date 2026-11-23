"""Verify legacy outbox rows on the read replica, then release matching local detail."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.fb_page_assignment.runtime import environment
from features.fb_page_assignment.storage import Store, Writer


def verify(path, env_file):
    import mysql.connector
    env = environment(env_file)
    checked, excluded = {}, 0
    local = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    local.row_factory = sqlite3.Row
    remote = mysql.connector.connect(host="101.32.56.53", port=63350, database="ads_ai",
        user=env["ADMIN_MAPPING_MYSQL_USER"], password=env["ADMIN_MAPPING_MYSQL_PASSWORD"],
        autocommit=True, compress=True, connection_timeout=5, read_timeout=30, write_timeout=8)
    try:
        cur = remote.cursor()
        cur.execute("SELECT @@read_only")
        if cur.fetchone() != (1,):
            raise RuntimeError("expected read replica")
        cur.execute("SET SESSION MAX_EXECUTION_TIME=20000")
        ids = [r[0] for r in local.execute("""SELECT task_id FROM fb_page_assignment_requests
            WHERE synced_revision=revision AND alert_status NOT IN ('pending','sending')
              AND substr(rows_json,1,1)='[' ORDER BY task_id""")]
        for start in range(0, len(ids), 30):
            group = ids[start:start+30]
            cur.execute("""SELECT task_id,item_index,payload_hash,SHA2(diagnostics_json,256),revision,
                alert_status,feishu_message_id FROM ads_ai.fb_drama_page_assignment_log FORCE INDEX(PRIMARY)
                WHERE task_id IN (""" + ",".join(["%s"] * len(group)) + ")", tuple(group))
            facts = {}
            for tid, index, *values in cur.fetchall():
                facts.setdefault(tid, {})[index] = tuple(values)
            for tid in group:
                row = local.execute("SELECT * FROM fb_page_assignment_requests WHERE task_id=?", (tid,)).fetchone()
                task = Store._decode(row)
                if not task or task["details_cleared"]:
                    continue
                expected = {}
                for item in task["rows"]:
                    record = Writer._record(task, item)
                    diag = hashlib.sha256(record["diagnostics_json"].encode()).hexdigest()
                    expected[item["item_index"]] = (record["payload_hash"], diag, task["revision"],
                                                     task["alert_status"], task["feishu_message_id"])
                if expected and expected == facts.get(tid):
                    checked[tid] = dict(revision=task["revision"], initial_hash=task["initial_hash"],
                                        hashes=[[i, v[0]] for i, v in sorted(expected.items())])
                else:
                    excluded += 1
    finally:
        remote.close()
        local.close()
    return {"summary": {"confirmed_tasks": len(checked), "excluded_tasks": excluded}, "tasks": checked}


def apply_verified(path, manifest):
    store = Store(path)
    compacted = 0
    for tid, proof in manifest["tasks"].items():
        compacted += store.compact_verified(tid, proof["revision"], proof["initial_hash"], dict(proof["hashes"]))
    return dict(verified=len(manifest["tasks"]), compacted=compacted)


def enable_reclamation(path):
    with sqlite3.connect(str(path), isolation_level=None, timeout=2) as db:
        if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 1:
            db.execute("PRAGMA auto_vacuum=FULL")
            db.execute("VACUUM")
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return dict(auto_vacuum=db.execute("PRAGMA auto_vacuum").fetchone()[0], checkpoint_busy=busy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outbox", type=Path, default=Path("/mnt/data-disk/fb-page-assignment/outbox.sqlite3"))
    parser.add_argument("--env", default="/etc/fb-page-assignment.env")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reclaim", action="store_true", help="convert an existing SQLite file in a quiet window")
    args = parser.parse_args()
    path = args.outbox.resolve(strict=True)
    if args.apply:
        result = apply_verified(path, json.loads(args.manifest.read_text()))
        if args.reclaim:
            result.update(enable_reclamation(path))
    else:
        if args.reclaim:
            parser.error("--reclaim requires --apply")
        os.umask(0o077)
        manifest = verify(path, args.env)
        temp = args.manifest.with_suffix(".next")
        with temp.open("x", encoding="utf-8") as output:
            json.dump(manifest, output, separators=(",", ":"))
        os.replace(temp, args.manifest)
        result = manifest["summary"]
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

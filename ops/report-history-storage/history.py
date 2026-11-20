"""Versioned imports for reviewed report inputs. Does not modify existing business tables or delete files."""

import argparse
import contextlib
import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

BASE = Path("/mnt/data-disk")
SNAP = "ads_ai.report_history_snapshot"
PAGE = "ads_ai.report_history_metric_page"
CONFIG = Path("/root/.codex/plugins/feishu/config.json")
METRICS = ("spend", "revenue", "installs", "impressions", "clicks")
AGG = ("spend_sum", "revenue_sum", "installs_sum", "impressions_sum", "clicks_sum")
RECEIPT = (
    "ad_id",
    "account_id",
    "page_id",
    "original_page_id",
    "selected_page_id",
    "old_creative_id",
    "new_creative_id",
    "current_creative_id",
    "prepare_status",
    "update_http_status",
    "delete_http_status",
    "final_status",
    "started_at",
    "finished_at",
)
MISSING = {"__report_field_absent__": True}
DW = set(
    "date uid app platform account campaign objective page resource content country language spend revenue installs impressions clicks".split()
)
META = set("account adset bids campaign controls goals n".split())
DW.update(("tz", "strategy"))
HOT = set(
    "dt date optimizer platform ad_account_id campaign_id ad_id created_data_id source_id data_source_id series_code drama_language spend revenue installs impressions clicks account campaign ad cid content language material updated_at time_zone rows_count purchases collect_revenue purchase_revenue ad_impression_revenue first_spend first_dt first_spend_date id manual_id queue_id cross_queue_ids app_id created_at publish_queue_id source_ad_id template_id user_id is_manual supper_id publish_queue ads_source_id content_id".split()
)
HOT_NAMES = {
    "perf.json",
    "perf_verified.json",
    "performance_snapshot.json",
    "daily.json",
    "hours.json",
    "detail.json",
    "pre7.json",
    "history.json",
    "logs.json",
    "new0.json",
    "new3.json",
    "created0.json",
    "created3.json",
    "mapping.json",
    "custom.json",
    "priorcampaign.json",
}
HOT.update(
    (
        "resource_created_at",
        "campaign_name",
        "original_source_id",
        "first_date",
        "performance_snapshot",
        "isolation_level",
    )
)
SENSITIVE = re.compile(
    r"token|password|secret|authorization|cookie|signature|api[_-]?key|https?://[^\s]+[?&](?:sig|token|key)=",
    re.I,
)
PRIVATE_VALUE = re.compile(
    r"https?://|(?:access[_-]?token|authorization|api[_-]?key|secret|password|cookie|signature)\s*[:=]|\bbearer\s+[\w.-]+|\bEAA[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.I,
)
SAFE_DATASETS = {"dw_daily", "dw_adset", "hotdrama_report", "fb_receipt_index"}
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def encoded(val):
    return json.dumps(val, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check(ok, code):
    if not ok:
        raise ValueError(code)


def normalize(v):
    if isinstance(v, float) and v.is_integer() and abs(v) <= 2**53:
        return int(v)
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [normalize(x) for x in v]
    return v


def business_cell(value):
    if isinstance(value, str):
        return len(value) <= 4096 and not PRIVATE_VALUE.search(value)
    return value is None or isinstance(value, (bool, int, float))


def match(rel):
    p = Path(rel)
    parts = p.parts
    n = p.name
    if p.is_absolute() or ".." in parts:
        return None
    if 3 <= len(parts) <= 5 and parts[0] == "dramawave-review" and parts[1].startswith("topic190ddc"):
        if re.fullmatch(r"day-\d{4}-\d{2}-\d{2}\.json", n):
            return "dw_daily"
        if re.fullmatch(r"new-metadata-\d+\.json", n):
            return "dw_adset"
    if (
        len(parts) == 3
        and parts[0] == "analysis"
        and parts[1].startswith(("hotdrama-review-", "hotdrama-optimizer-"))
        and n in HOT_NAMES
    ):
        return "hotdrama_report"
    if (
        len(parts) == 4
        and parts[:3] == ("projects", "codex_test", "output")
        and re.fullmatch(r"fb_2446289_daily_replace_delete_\d{8}_\d{6}\.jsonl", n)
    ):
        return "fb_receipt_index"
    return None


def probe_rows(path, dataset):
    before = path.stat()
    if dataset == "fb_receipt_index":
        digest = hashlib.sha256()
        rows = []
        with path.open("rb") as src:
            for raw in src:
                digest.update(raw)
                if not raw.strip():
                    continue
                obj = json.loads(raw)
                check(isinstance(obj, dict) and "ad_id" in obj, "unrecognized_receipt")
                row = {k: obj[k] for k in RECEIPT if k in obj}
                check(all(business_cell(val) for val in row.values()), "unreviewed_receipt_index_value")
                row["source_line_sha256"] = sha(raw.rstrip(b"\r\n"))
                rows.append(row)
        source_hash = digest.hexdigest()
        kind = "jsonl-index"
        meta = {}
    else:
        raw = path.read_bytes()
        source_hash = sha(raw)
        doc = json.loads(raw)
        del raw
        if isinstance(doc, dict):
            check(isinstance(doc.get("rows"), list), "unsupported_envelope")
            rows = doc.pop("rows")
            meta = doc
            kind = "object-rows"
            check(
                not set(meta) - {"totals", "start", "end", "date", "dt", "count", "from", "to"},
                "unreviewed_metadata_fields",
            )
            check(
                len(encoded(meta).encode()) <= 65536 and not SENSITIVE.search(encoded(meta)),
                "unreviewed_metadata_values",
            )
        else:
            check(isinstance(doc, list), "not_array")
            rows = doc
            meta = {}
            kind = "array"
        allowed = {"dw_daily": DW, "dw_adset": META, "hotdrama_report": HOT}[dataset]
        cols = set()
        for row in rows:
            check(isinstance(row, dict), "unrecognized_row")
            cols.update(row)
        check(not cols - allowed, "unreviewed_row_fields")
        if dataset == "dw_daily" and rows:
            check({"date", "account", "campaign", "spend", "revenue"} <= cols, "not_daily_performance")
        if dataset == "dw_adset" and rows:
            check({"account", "campaign", "adset"} <= cols, "not_adset_metadata")
    after = path.stat()
    check(
        (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
        "source_changed",
    )
    return rows, kind, meta, source_hash, before


def stat_date(row):
    raw = row.get("dt") or row.get("date") or row.get("started_at")
    if isinstance(raw, str) and re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return datetime.date.fromisoformat(raw[:10]).isoformat()
    return None


def pack_pages(rows, dataset, cols):
    whole = hashlib.sha256()
    prefix = (dataset + "\n" + encoded(cols) + "\n").encode()
    pages = []
    vals = []
    size = 0
    dates = []
    sums = [None] * len(METRICS)

    def close():
        payload = "[" + ",".join(vals) + "]"
        digest = sha(payload.encode())
        return (
            sha(prefix + bytes.fromhex(digest)),
            len(vals),
            min(dates) if dates else None,
            max(dates) if dates else None,
            *[x.quantize(Decimal("0.00000001")) if x is not None else None for x in sums],
            digest,
            payload,
        )

    for row in rows:
        value = [normalize(row[k]) if k in row else MISSING for k in cols]
        line = encoded(value)
        check(all(business_cell(x) for x in row.values()), "unreviewed_business_value")
        raw = line.encode()
        check(len(raw) <= 192 * 1024, "oversize_business_record")
        if vals and (size + len(raw) > 160 * 1024 or len(vals) >= 1000):
            pages.append(close())
            vals = []
            size = 0
            dates = []
            sums = [None] * len(METRICS)
        vals.append(line)
        size += len(raw) + 1
        whole.update(raw + b"\n")
        d = stat_date(row)
        if d:
            dates.append(d)
        for i, k in enumerate(METRICS):
            val = row.get(k)
            if val is not None and val != "":
                num = Decimal(str(val))
                check(num.is_finite(), "invalid_metric")
                sums[i] = (sums[i] or Decimal(0)) + num
    if vals:
        pages.append(close())
    return pages, whole.hexdigest()


@contextlib.contextmanager
def connection(writer):
    import mysql.connector

    conf = json.loads(CONFIG.read_text())["mysql"]
    check(conf["host"] == "101.32.56.53", "database_host_mismatch")
    conn = mysql.connector.connect(
        host=conf["host"],
        port=63353 if writer else 63350,
        user=conf["user"],
        password=conf["password"],
        database="ads_ai",
        connection_timeout=6,
        read_timeout=60,
        write_timeout=60,
        compress=True,
        use_pure=True,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor(buffered=True) as cur:
            cur.execute("SELECT DATABASE(),@@read_only")
            check(cur.fetchone() == ("ads_ai", 0 if writer else 1), "database_identity_mismatch")
        yield conn
    finally:
        conn.close()


def group(items, n):
    for start in range(0, len(items), n):
        yield items[start : start + n]


def fetch_pages(cur, keys, detail):
    if not keys:
        return []
    cols = (
        "HEX(page_id),dataset,record_count,min_date,max_date,"
        + ",".join(AGG)
        + ",HEX(records_sha256),rows_json"
        if detail
        else "HEX(page_id)"
    )
    sql = (
        "SELECT "
        + cols
        + " FROM "
        + PAGE
        + " WHERE page_id IN ("
        + ",".join(["UNHEX(%s)"] * len(keys))
        + ")"
    )
    cur.execute(sql, keys)
    return cur.fetchall()


def verified_view(reader, sid):
    with reader.cursor(buffered=True) as q:
        q.execute(
            "SELECT dataset,relative_path,HEX(source_sha256),source_bytes,source_format,columns_json,document_metadata,page_ids,status,record_count,page_count,HEX(records_sha256) FROM "
            + SNAP
            + " WHERE snapshot_id=UNHEX(%s)",
            (sid,),
        )
        return q.fetchone()


def confirm_header(row, expected):
    if not row:
        return False
    dataset, rel, src_hash, sz, kind, cols, meta, page_ids, _, n, np, dig = row
    actual = (
        dataset,
        rel,
        src_hash.lower(),
        sz,
        kind,
        json.loads(cols),
        json.loads(meta),
        json.loads(page_ids),
        n,
        np,
        dig.lower(),
    )
    check(actual == expected, "replica_snapshot_content_mismatch")
    return True


def verify_block(q, chunk, dataset, cols):
    wanted = {p[0]: p for p in chunk}
    for attempt in range(5):
        rows = fetch_pages(q, list(wanted), True)
        if len(rows) == len(wanted):
            break
        time.sleep(1 + attempt)
    check(len(rows) == len(wanted), "replica_pages_pending")
    for key, scope, n, d0, d1, *rest in rows:
        p = wanted[key.lower()]
        nums = rest[:5]
        digest = rest[5].lower()
        payload = rest[6]
        body = json.loads(payload)
        normalized = encoded(normalize(body)).encode()
        check(
            scope == dataset
            and n == p[1] == len(body)
            and (str(d0) if d0 else None) == p[2]
            and (str(d1) if d1 else None) == p[3]
            and list(nums) == list(p[4:9])
            and sha(normalized) == digest == p[-2],
            "replica_business_data_mismatch",
        )
        check(
            sha((dataset + "\n" + encoded(cols) + "\n").encode() + bytes.fromhex(digest)) == p[0],
            "replica_page_key_mismatch",
        )


def identical_version(reader, dataset, path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    checksum = digest.hexdigest()
    with reader.cursor(buffered=True) as q:
        q.execute(
            "SELECT HEX(snapshot_id) FROM "
            + SNAP
            + " WHERE source_sha256=UNHEX(%s) AND dataset=%s AND status='verified' LIMIT 1",
            (checksum, dataset),
        )
        hit = q.fetchone()
    return checksum, verified_view(reader, hit[0].lower()) if hit else None


def migrate(path, dataset, reader, writer):
    rel = path.relative_to(BASE).as_posix()
    st = path.stat()
    source_hash, original = identical_version(reader, dataset, path)
    pages = []
    if original:
        check(original[2].lower() == source_hash and st.st_size == original[3], "source_size_mismatch")
        kind, cols, meta, ids = original[4], *(json.loads(x) for x in original[5:8])
        n, page_count, rows_hash = original[9], original[10], original[11].lower()
    else:
        rows, kind, meta, fresh_hash, _ = probe_rows(path, dataset)
        check(fresh_hash == source_hash, "source_changed")
        n = len(rows)
        cols = sorted({k for row in rows for k in row})
        pages, rows_hash = pack_pages(rows, dataset, cols)
        del rows
        ids = [p[0] for p in pages]
        page_count = len(ids)
    sid = sha((dataset + "\0" + rel + "\0" + source_hash).encode())
    exp = (dataset, rel, source_hash, st.st_size, kind, cols, meta, ids, n, page_count, rows_hash)
    old = verified_view(reader, sid)
    if old and confirm_header(old, exp) and old[8] == "verified":
        return dict(
            snapshot=sid,
            source_sha256=source_hash,
            records=n,
            pages=page_count,
            bytes=st.st_size,
            reused=True,
        )
    with writer.cursor(buffered=True) as put, reader.cursor(buffered=True) as get:
        put.execute(
            "INSERT INTO "
            + SNAP
            + "(snapshot_id,dataset,relative_path,source_sha256,source_bytes,source_mtime,source_format,columns_json,document_metadata,page_ids,status,record_count,page_count,records_sha256) VALUES(UNHEX(%s),%s,%s,UNHEX(%s),%s,%s,%s,%s,%s,%s,'staging',%s,%s,UNHEX(%s)) ON DUPLICATE KEY UPDATE snapshot_id=VALUES(snapshot_id)",
            (
                sid,
                dataset,
                rel,
                source_hash,
                st.st_size,
                int(st.st_mtime),
                kind,
                encoded(cols),
                encoded(meta),
                encoded(ids),
                n,
                page_count,
                rows_hash,
            ),
        )
        unique = list({p[0]: p for p in pages}.values())
        for window in group(unique, 90):
            already = {r[0].lower() for r in fetch_pages(get, [p[0] for p in window], False)}
            missing = [(p[0], dataset, *p[1:]) for p in window if p[0] not in already]
            for batch in group(missing, 3):
                put.executemany(
                    "INSERT INTO "
                    + PAGE
                    + "(page_id,dataset,record_count,min_date,max_date,"
                    + ",".join(AGG)
                    + ",records_sha256,rows_json) VALUES(UNHEX(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,UNHEX(%s),%s) ON DUPLICATE KEY UPDATE page_id=VALUES(page_id)",
                    batch,
                )
            for block in group(window, 12):
                verify_block(get, block, dataset, cols)
        for i in range(6):
            current = verified_view(reader, sid)
            if current:
                break
            time.sleep(1 + i)
        check(confirm_header(current, exp), "snapshot_not_replicated")
        now = path.stat()
        check(
            (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            == (now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns),
            "source_changed_at_verification",
        )
        put.execute(
            "UPDATE "
            + SNAP
            + " SET status='verified',verified_at=UTC_TIMESTAMP(6) WHERE snapshot_id=UNHEX(%s) AND status='staging' LIMIT 1",
            (sid,),
        )
        for i in range(6):
            latest = verified_view(reader, sid)
            if latest and latest[8] == "verified":
                break
            time.sleep(1 + i)
        check(
            latest and latest[8] == "verified" and confirm_header(latest, exp),
            "verified_snapshot_pending_replication",
        )
    return dict(
        snapshot=sid,
        source_sha256=source_hash,
        records=n,
        pages=page_count,
        bytes=st.st_size,
        reused=bool(original),
    )


TABLE_COLUMNS = {
    "report_history_snapshot": (
        "snapshot_id:binary dataset:varchar relative_path:varchar source_sha256:binary source_bytes:bigint "
        "source_mtime:bigint source_format:varchar columns_json:json document_metadata:json page_ids:json "
        "status:enum record_count:bigint page_count:int records_sha256:binary verified_at:datetime"
    ).split(),
    "report_history_metric_page": (
        "page_id:binary dataset:varchar record_count:int min_date:date max_date:date spend_sum:decimal "
        "revenue_sum:decimal installs_sum:decimal impressions_sum:decimal clicks_sum:decimal records_sha256:binary rows_json:json"
    ).split(),
}


def schema_state(reader):
    with reader.cursor(buffered=True) as cur:
        cur.execute("""SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,COLUMN_KEY FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA='ads_ai' AND TABLE_NAME IN ('report_history_snapshot','report_history_metric_page')
          ORDER BY TABLE_NAME,ORDINAL_POSITION""")
        actual = {}
        for table, column, dtype, key in cur:
            actual.setdefault(table, []).append((column + ":" + dtype, key == "PRI"))
    for table, columns in actual.items():
        check(
            columns == [(name, i == 0) for i, name in enumerate(TABLE_COLUMNS[table])],
            "existing_schema_mismatch",
        )
    return sorted(actual)


def initialize():
    text = SCHEMA_PATH.read_text()
    statements = [stmt.strip() for stmt in text.split(";") if stmt.strip()]
    check(len(statements) == 2, "unexpected_schema")
    expected = {SNAP, PAGE}
    found = {re.match(r"^CREATE TABLE IF NOT EXISTS (ads_ai\.\w+) \(", s).group(1) for s in statements}
    check(found == expected, "unexpected_table")
    with connection(False) as reader:
        before = schema_state(reader)
    print(encoded({"existing_before": before}), flush=True)
    with connection(True) as db, db.cursor(buffered=True) as cur:
        for sql in statements:
            cur.execute(sql)
    with connection(False) as db:
        for i in range(8):
            visible = schema_state(db)
            if len(visible) == 2:
                print(encoded({"replica_verified": visible}), flush=True)
                return
            time.sleep(1 + i)
    raise RuntimeError("ddl_not_visible_on_replica")


def candidates():
    found = set()
    for pattern in (
        "dramawave-review/topic190ddc*/**/day-*.json",
        "dramawave-review/topic190ddc*/**/new-metadata-*.json",
        "analysis/hotdrama-*/*.json",
        "projects/codex_test/output/fb_2446289_daily_replace_delete_*.jsonl",
    ):
        for p in BASE.glob(pattern):
            rel = p.relative_to(BASE).as_posix()
            if match(rel) and p.is_file() and p.resolve() == p:
                found.add(rel)
    frozen = []
    for rel in sorted(found):
        stat = BASE.joinpath(rel).stat()
        frozen.append(dict(path=rel, size=stat.st_size, inode=stat.st_ino, mtime_ns=stat.st_mtime_ns))
    return sorted(frozen, key=lambda item: (item["size"], item["path"]))


def journal(path, entry):
    with path.open("a", encoding="utf8") as f:
        f.write(encoded(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def run(paths, ledger):
    import fcntl

    check(isinstance(paths, list) and len(paths) == len({item["path"] for item in paths}), "invalid_plan")
    with open(str(ledger) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with connection(False) as rdb, connection(True) as wdb:
            check(set(schema_state(rdb)) == set(TABLE_COLUMNS), "tables_unavailable")
            for item in paths:
                rel = item["path"]
                dataset = match(rel)
                check(dataset in SAFE_DATASETS, "path_not_approved")
                p = BASE / rel
                try:
                    check(p.resolve() == p and p.is_file(), "changed_path")
                    st = p.stat()
                    check(
                        (st.st_ino, st.st_size, st.st_mtime_ns)
                        == (item["inode"], item["size"], item["mtime_ns"]),
                        "frozen_source_changed",
                    )
                    result = migrate(p, dataset, rdb, wdb)
                    journal(
                        ledger,
                        dict(time=time.time(), path=rel, dataset=dataset, status="verified", **result),
                    )
                except (ValueError, TypeError, ArithmeticError, OSError) as e:
                    reason = (
                        str(e)
                        if type(e) is ValueError and re.fullmatch(r"[a-z_]{1,80}", str(e))
                        else type(e).__name__
                    )
                    journal(
                        ledger,
                        dict(time=time.time(), path=rel, dataset=dataset, status="retained", reason=reason),
                    )
                except Exception as e:
                    journal(
                        ledger,
                        dict(
                            time=time.time(),
                            path=rel,
                            dataset=dataset,
                            status="db_error",
                            reason=type(e).__name__,
                            errno=getattr(e, "errno", None),
                        ),
                    )
                    raise


def read_version(sid, out):
    check(re.fullmatch("[a-f0-9]{64}", sid), "invalid_version")
    with connection(False) as db, db.cursor(buffered=True) as cur:
        header = verified_view(db, sid)
        check(header and header[8] == "verified", "unknown_verified_version")
        dataset, _, _, _, kind, cols, meta, ids, _, n, _, total_digest = header
        cols = json.loads(cols)
        meta = json.loads(meta)
        ids = json.loads(ids)
        h = hashlib.sha256()
        count = 0
        if kind == "object-rows":
            base = encoded(meta)[:-1]
            out.write(base + ("," if meta else "") + '"rows":[')
        elif kind == "array":
            out.write("[")
        for keys in group(ids, 8):
            page_map = {row[0].lower(): row for row in fetch_pages(cur, keys, True)}
            for key in keys:
                row = page_map[key]
                body = normalize(json.loads(row[-1]))
                check(sha(encoded(body).encode()) == row[-2].lower(), "stored_page_digest")
                for cells in body:
                    h.update(encoded(cells).encode() + b"\n")
                    obj = {k: val for k, val in zip(cols, cells) if val != MISSING}
                    if kind != "jsonl-index" and count:
                        out.write(",")
                    out.write(encoded(obj) + ("\n" if kind == "jsonl-index" else ""))
                    count += 1
        if kind == "object-rows":
            out.write("]}")
        elif kind == "array":
            out.write("]")
        check(count == n and h.hexdigest() == total_digest.lower(), "stored_version_digest")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema")
    sub.add_parser("inventory")
    runp = sub.add_parser("import")
    runp.add_argument("--plan", type=Path, required=True)
    runp.add_argument("--journal", type=Path, required=True)
    read = sub.add_parser("read")
    read.add_argument("snapshot")
    args = ap.parse_args()
    if args.cmd == "schema":
        initialize()
    elif args.cmd == "inventory":
        print(encoded(candidates()))
    elif args.cmd == "import":
        run(json.loads(args.plan.read_text()), args.journal)
    else:
        read_version(args.snapshot, sys.stdout)


if __name__ == "__main__":
    main()

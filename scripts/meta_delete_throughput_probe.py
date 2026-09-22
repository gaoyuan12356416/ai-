"""Manual, bounded SQL-only old/new probe. No Meta calls and no ledger writes.

Run before switching the live code, or supply the backed-up baseline root.
Passwords come from the running API process; tokens exist only in process
memory. Reports contain safe credential context and digest equality, not hashes
or tokens. The mysql command is the same FIFO wrapper used by the live app.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time


SAFE_CREDENTIAL_FIELDS = (
    "credential_source_row_id", "credential_ad_id", "credential_product_id", "credential_source_user_id",
    "credential_publish_queue_id", "credential_default_token", "credential_kind", "credential_user_id",
    "credential_relation", "credential_fb_user_id",
)
PROBE_STAGE = "arguments"


class ProbeQueryError(RuntimeError):
    def __init__(self, code=None):
        super().__init__("Read-only probe query failed; raw output suppressed")
        self.errno = code


def load_baseline(path):
    name = "features.fb_ad_asset_delete._throughput_probe_baseline"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SqlSource


def main():
    global PROBE_STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--video-ids", nargs="+", required=True)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-root", type=Path, default=Path("/root/drama_material_service"))
    parser.add_argument("--sub-user-id", help="Optional explicit internal user for fresh product ACL reads")
    parser.add_argument("--max-pairs", type=int, default=4)
    args = parser.parse_args()
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.job_id), "Invalid job ID"
    assert 1 <= len(args.video_ids) <= 10 and all(re.fullmatch(r"[1-9][0-9]{0,31}", v) for v in args.video_ids)
    assert 1 <= args.max_pairs <= 20
    assert not args.sub_user_id or re.fullmatch(r"[1-9][0-9]{0,31}", args.sub_user_id)
    args.code_root, args.baseline_root = args.code_root.resolve(), args.baseline_root.resolve()
    new_path = args.code_root / "features/fb_ad_asset_delete/source.py"
    old_path = args.baseline_root / "features/fb_ad_asset_delete/source.py"
    assert new_path.is_file() and old_path.is_file()
    sys.path.insert(0, str(args.code_root))
    from features.fb_ad_asset_delete.core import AssetError, account_id
    from features.fb_ad_asset_delete.source import SqlSource
    BaselineSource = load_baseline(old_path)

    PROBE_STAGE = "frozen_scope"
    disk = Path("/mnt/data-disk")
    assert disk.is_mount() and shutil.disk_usage(disk).free > 100 * 1024**2
    assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(disk)], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    ledger = disk / "fb-ad-asset-delete/tasks.sqlite3"
    assert ledger.is_file() and os.access(ledger.parent, os.W_OK)
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
        job = conn.execute("SELECT metadata FROM fb_asset_delete_v2_jobs WHERE job_id=?", (args.job_id,)).fetchone()
        assert job, "Frozen job not found"
        product_ids = [str(p["id"]) for p in json.loads(job[0])["products"]]
        assert 1 <= len(product_ids) <= 20 and all(re.fullmatch(r"[1-9][0-9]{0,31}", p) for p in product_ids)
        pairs = []
        for vid in dict.fromkeys(args.video_ids):
            row = conn.execute("SELECT payload,result FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' AND object_id=?",
                               (args.job_id, vid)).fetchone()
            assert row, "Frozen video not found"
            obj = dict(json.loads(row[0]), kind="video", object_id=vid, result=json.loads(row[1]))
            pairs.extend((obj, aid) for aid in sorted({account_id(v) for v in obj["account_ids"]}))
    assert pairs, "No frozen account/video pairs"

    PROBE_STAGE = "live_database_config"
    pid = subprocess.check_output(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"], text=True).strip()
    assert pid.isdigit() and int(pid) > 1
    env = dict(x.split("=", 1) for x in Path("/proc/" + pid + "/environ").read_bytes().decode().split("\0") if "=" in x)
    host = env.get("DRAMA_DB_HOST") or env.get("ADMIN_MAPPING_MYSQL_HOST")
    port = env.get("DRAMA_DB_PORT") or env.get("ADMIN_MAPPING_MYSQL_PORT")
    assert host == "101.32.56.53" and port == "63350", "Read-only source endpoint required"
    user = env.get("DRAMA_DB_USER") or env.get("ADMIN_MAPPING_MYSQL_USER")
    password = env.get("DRAMA_DB_PASSWORD") or env.get("ADMIN_MAPPING_MYSQL_PASSWORD")
    assert user and password, "Live DB credentials unavailable"
    schema = env.get("AD_CONTROL_DB_NAME") or env.get("DRAMA_DB_NAME") or "kunlunads_dev"
    assert re.fullmatch(r"[A-Za-z0-9_]+", schema)
    sql_env = os.environ.copy()
    sql_env["MYSQL_PWD"] = password
    sql_env.pop("SQL_GATE_BYPASS", None)
    command = ["/usr/bin/mysql", "-h", host, "-P", port, "-u", user,
               "--connect-timeout=5", "--default-character-set=utf8mb4", "-N", "-B"]
    del env, password
    calls, plan_calls = [], []

    def execute(sql, timeout, explain=False):
        assert sql.lstrip().upper().startswith("SELECT"), "Only source SELECT statements are allowed"
        timeout = max(3, min(int(timeout), 10))
        child_env = dict(sql_env, MYSQL_QUERY_TIMEOUT_SECONDS=str(timeout), MYSQL_QUERY_TIMEOUT_KILL_AFTER="3",
                         MYSQL_MAX_EXECUTION_TIME_MS=str(timeout * 1000))
        started = time.monotonic()
        result = subprocess.run(command, input="SELECT @@read_only;\n" + ("EXPLAIN " if explain else "") + sql + ";\n",
                                capture_output=True, text=True, env=child_env, timeout=timeout + 8)
        if result.returncode:
            match = re.search(r"\bERROR\s+(\d{4})\b", result.stderr or "")
            raise ProbeQueryError(int(match[1]) if match else None)
        lines = result.stdout.splitlines()
        assert lines and lines[0] == "1", "Business source is not confirmed read-only"
        rows = [line.split("\t") for line in lines[1:] if line.strip()]
        item = dict(query_sha256=hashlib.sha256(sql.encode()).hexdigest()[:16], rows_returned=len(rows),
                    duration_ms=round((time.monotonic() - started) * 1000), timeout_seconds=timeout)
        (plan_calls if explain else calls).append(item)
        return rows

    captured = []
    def query(sql, timeout):
        captured.append(sql)
        return execute(sql, timeout)

    actor = (lambda _: {"sub_user_id": args.sub_user_id}) if args.sub_user_id else None
    old = BaselineSource(query, schema=schema, lookup_actor=actor)
    new = SqlSource(query, schema=schema, lookup_actor=actor)
    session = {"role": "user" if args.sub_user_id else "admin"}
    report = dict(read_only=True, meta_requests=0, ledger_writes=0, checked_at=datetime.now(timezone.utc).isoformat(),
        code_root=str(args.code_root), baseline_root=str(args.baseline_root), job_id=args.job_id,
        source_sha256=dict(baseline=hashlib.sha256(old_path.read_bytes()).hexdigest(),
                           new=hashlib.sha256(new_path.read_bytes()).hexdigest()),
        sql_command=command[0], sql_command_resolved=str(Path(command[0]).resolve()),
        product_acl_mode="explicit_internal_user" if args.sub_user_id else "admin_catalog_only",
        sequential_reads_not_transaction_snapshot=True, available_pairs=len(pairs), checked_pairs=min(len(pairs), args.max_pairs),
        sampled=len(pairs) > args.max_pairs, selected_product_ids=product_ids, items=[], explain=[])

    def products(source):
        start = len(calls)
        try:
            result = dict(status="selected", products=source.selected_products(session, product_ids))
        except AssetError as exc:
            result = dict(status="unresolved", code=exc.code)
        result["sql_reads"] = calls[start:]
        return result

    PROBE_STAGE = "selected_products"
    baseline_products = products(old)
    new_sql_start = len(captured)
    new_products = products(new)
    new_statements = list(captured[new_sql_start:])
    report["products"] = dict(baseline=baseline_products, new=new_products,
        equivalent={k: v for k, v in baseline_products.items() if k != "sql_reads"} ==
                   {k: v for k, v in new_products.items() if k != "sql_reads"})

    def credential(source, obj, aid):
        start = len(calls)
        digest = None
        try:
            selected = source.video_account_credential(obj, aid)
            token = str(selected.pop("token", ""))
            assert token, "Credential returned no token"
            digest = hashlib.sha256(token.encode()).digest()
            del token
            outcome = dict(status="selected", context={k: selected[k] for k in SAFE_CREDENTIAL_FIELDS if k in selected})
        except AssetError as exc:
            detail = getattr(exc, "detail", {})
            outcome = dict(status="unresolved", code=exc.code, context={k: detail[k] for k in SAFE_CREDENTIAL_FIELDS if k in detail})
        outcome["sql_reads"] = calls[start:]
        return outcome, digest

    PROBE_STAGE = "credential_comparison"
    for obj, aid in pairs[:args.max_pairs]:
        before, old_digest = credential(old, obj, aid)
        new_sql_start = len(captured)
        after, new_digest = credential(new, obj, aid)
        new_statements.extend(captured[new_sql_start:])
        same_context = ({k: v for k, v in before.items() if k != "sql_reads"} ==
                        {k: v for k, v in after.items() if k != "sql_reads"})
        same_token = hmac.compare_digest(old_digest, new_digest) if old_digest is not None and new_digest is not None else None
        report["items"].append(dict(video_id=obj["object_id"], account_id=aid, baseline=before, new=after,
            same_context=same_context, same_token_digest=same_token, equivalent=same_context and same_token is not False))
        del old_digest, new_digest

    PROBE_STAGE = "explain"
    for statement in dict.fromkeys(new_statements):
        try:
            plan = execute(statement, 5, explain=True)
            report["explain"].append(dict(plan_calls[-1], status="ok", plan_rows=plan))
        except ProbeQueryError as exc:
            report["explain"].append(dict(status="failed", error_number=exc.errno,
                query_sha256=hashlib.sha256(statement.encode()).hexdigest()[:16]))
    report["sql_reads"] = len(calls)
    report["explain_reads"] = len(plan_calls)
    report["all_equivalent"] = report["products"]["equivalent"] and all(item["equivalent"] for item in report["items"])
    report["selected_pairs"] = sum(item["new"]["status"] == "selected" for item in report["items"])
    report["passed"] = (report["all_equivalent"] and report["selected_pairs"] > 0 and
                        all(item["status"] == "ok" for item in report["explain"]))
    PROBE_STAGE = "write_report"
    path = ledger.parent / ("throughput-source-probe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps(dict(report, report_path=str(path)), ensure_ascii=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Neither subprocess output nor a SQL/credential-bearing traceback may
        # reach logs if a transport, argument or environment check fails.
        print(json.dumps(dict(passed=False, probe_error=type(error).__name__,
                              stage=PROBE_STAGE, error_number=getattr(error, "errno", None), raw_details_suppressed=True)))
        sys.exit(2)

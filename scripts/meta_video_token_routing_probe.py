"""Read-only production credential selection probe. No Meta requests or writes."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--video-ids", nargs="+", required=True)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.job_id)
    assert 1 <= len(args.video_ids) <= 10 and all(re.fullmatch(r"[1-9][0-9]{0,31}", vid) for vid in args.video_ids)
    sys.path.insert(0, str(args.code_root.resolve()))
    from features.fb_ad_asset_delete.core import AssetError, account_id
    from features.fb_ad_asset_delete.graph import GraphClient
    from features.fb_ad_asset_delete.source import SqlSource

    disk = Path("/mnt/data-disk")
    assert disk.is_mount() and subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(disk)], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    pid = subprocess.check_output(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"], text=True).strip()
    env = dict(x.split("=", 1) for x in Path("/proc/" + pid + "/environ").read_bytes().decode().split("\0") if "=" in x)
    sql_env = os.environ.copy()
    sql_env["MYSQL_PWD"] = env.get("DRAMA_DB_PASSWORD") or env.get("ADMIN_MAPPING_MYSQL_PASSWORD")
    command = ["/usr/bin/mysql.real", "-h", "101.32.56.53", "-P", "63350", "-u",
        env.get("DRAMA_DB_USER") or env.get("ADMIN_MAPPING_MYSQL_USER"), "--connect-timeout=5",
        "--init-command=SET SESSION MAX_EXECUTION_TIME=5000", "-N", "-B"]
    calls = [0]

    def query(sql, timeout):
        assert sql.lstrip().upper().startswith("SELECT")
        calls[0] += 1
        result = subprocess.run(command, input="SELECT @@read_only;" + sql + ";", capture_output=True,
            text=True, env=sql_env, timeout=timeout)
        if result.returncode:
            raise RuntimeError("Read-only source query failed")
        lines = result.stdout.splitlines()
        assert lines and lines[0] == "1"
        return [line.split("\t") for line in lines[1:]]

    class NoNetwork:
        def request(self, *args, **kwargs):
            raise AssertionError("This probe cannot send any Meta request")

    source = SqlSource(query)
    graph = GraphClient(source.token, video_account_credential_provider=source.video_account_credential, transport=NoNetwork())
    ledger = disk / "fb-ad-asset-delete/tasks.sqlite3"
    report = dict(read_only=True, code_root=str(args.code_root.resolve()), job_id=args.job_id,
                  checked_at=datetime.now(timezone.utc).isoformat(), meta_requests=0, items=[])
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
        for vid in args.video_ids:
            row = conn.execute("SELECT payload,result FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' AND object_id=?",
                               (args.job_id, vid)).fetchone()
            assert row, "Frozen video not found"
            obj = dict(json.loads(row[0]), kind="video", object_id=vid, result=json.loads(row[1]))
            for aid in sorted({account_id(value) for value in obj["account_ids"]}):
                item = dict(video_id=vid, account_id=aid)
                try:
                    token, context = graph.prepare_video_account_delete(obj, aid)
                    assert token and not any(key in context for key in ("token", "access_token", "page_access_token"))
                    item.update(status="selected", context=context)
                    del token
                except AssetError as exc:
                    item.update(status="unresolved", code=exc.code, message=exc.message, context=getattr(exc, "detail", {}))
                report["items"].append(item)
    report["sql_reads"] = calls[0]
    path = ledger.parent / ("token-routing-probe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(path), 0o600)
    print(json.dumps(dict(report, report_path=str(path)), ensure_ascii=True))


if __name__ == "__main__":
    main()

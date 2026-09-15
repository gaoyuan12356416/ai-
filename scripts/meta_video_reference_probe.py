"""Read-only production snapshot benchmark. Never imports app or calls Meta DELETE."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_ad_asset_delete.video_index import MysqlVideoStream, VideoIndex
from features.fb_ad_asset_delete.core import redact


def service_environment():
    pid = subprocess.check_output(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"], text=True).strip()
    return dict(x.split("=", 1) for x in Path("/proc/%s/environ" % pid).read_bytes().decode().split("\0") if "=" in x)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    disk = Path("/mnt/data-disk")
    def guard():
        assert disk.is_mount()
        assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(disk)], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    guard()
    env = service_environment()
    host = env.get("DRAMA_DB_HOST") or env.get("ADMIN_MAPPING_MYSQL_HOST")
    port = env.get("DRAMA_DB_PORT") or env.get("ADMIN_MAPPING_MYSQL_PORT")
    user = env.get("DRAMA_DB_USER") or env.get("ADMIN_MAPPING_MYSQL_USER")
    password = env.get("DRAMA_DB_PASSWORD") or env.get("ADMIN_MAPPING_MYSQL_PASSWORD")
    assert host == "101.32.56.53" and port == "63350" and user and password
    ledger = disk / "fb-ad-asset-delete/tasks.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri()+"?mode=ro", uri=True)) as conn:
        ids = [r[0] for r in conn.execute("SELECT object_id FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video'", (args.job_id,))]
        assert ids, "frozen Video list not found"
    stream = MysqlVideoStream(["mysql", "-h", host, "-P", port, "-u", user, "-N", "-B", "--default-character-set=utf8mb4"], password, "kunlunads_dev")
    index = VideoIndex(ledger.parent / "video-reference-index", stream, validate_storage=guard)
    started = time.monotonic()
    try:
        proof = index.build(lambda kind, done, total: print(json.dumps(dict(rows=done, total=total, elapsed_seconds=round(time.monotonic()-started, 2))), flush=True))
        before_lookup = time.monotonic()
        try:
            refs = index.references(ids)
            lookup = dict(references=len(refs), lookup_seconds=round(time.monotonic()-before_lookup, 4))
        except Exception as exc:
            lookup = dict(error=getattr(exc, "code", type(exc).__name__))
        with closing(sqlite3.connect(index.path.as_uri()+"?mode=ro", uri=True)) as conn:
            malformed = [dict(source_row_id=r[0], ad_id=r[1], product_id=r[2], account_id=r[3], raw=redact(r[4]))
                         for r in conn.execute("SELECT row_id,ad_id,product_id,account_id,raw FROM malformed LIMIT 20")]
        report = dict(read_only=True, source_connections=1, proof=proof,
                      elapsed_seconds=round(time.monotonic()-started, 2), index_bytes=index.path.stat().st_size,
                      peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, targets=len(ids), malformed_examples=malformed, **lookup)
    except Exception as exc:
        report = dict(read_only=True, error=getattr(exc, "code", type(exc).__name__), elapsed_seconds=round(time.monotonic()-started, 2))
    path = ledger.parent / "reference-probe.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(path), 0o600)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())

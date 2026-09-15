"""Manually verify selected Video credentials using GET only, without importing app."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_ad_asset_delete.graph import GraphClient
from features.fb_ad_asset_delete.source import SqlSource
from features.fb_ad_asset_delete.store import _CREDENTIAL_FIELDS


class ReadOnlyTransport:
    def __init__(self):
        self.http = requests.Session()
        self.calls = 0

    def request(self, method, url, **kwargs):
        if method != "GET":
            raise RuntimeError("This probe cannot write to Meta")
        self.calls += 1
        return self.http.request(method, url, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.job_id), "Invalid job ID"
    disk = Path("/mnt/data-disk")
    assert disk.is_mount()
    assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(disk)], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert os.access(disk / "fb-ad-asset-delete", os.W_OK) and shutil.disk_usage(disk).free > 100 * 1024**2
    pid = subprocess.check_output(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"], text=True).strip()
    env = dict(x.split("=", 1) for x in Path("/proc/" + pid + "/environ").read_bytes().decode().split("\0") if "=" in x)
    user = env.get("DRAMA_DB_USER") or env.get("ADMIN_MAPPING_MYSQL_USER")
    sql_env = os.environ.copy()
    sql_env["MYSQL_PWD"] = env.get("DRAMA_DB_PASSWORD") or env.get("ADMIN_MAPPING_MYSQL_PASSWORD")
    command = ["/usr/bin/mysql.real", "-h", "101.32.56.53", "-P", "63350", "-u", user,
               "--connect-timeout=5", "--init-command=SET SESSION MAX_EXECUTION_TIME=10000", "-N", "-B"]

    def query(sql, timeout):
        assert sql.lstrip().upper().startswith("SELECT")
        result = subprocess.run(command, input="SELECT @@read_only;" + sql + ";", capture_output=True,
                                text=True, env=sql_env, timeout=timeout)
        if result.returncode:
            raise RuntimeError("Read-only credential query failed")
        lines = result.stdout.splitlines()
        assert lines and lines[0] == "1"
        return [line.split("\t") for line in lines[1:]]

    source = SqlSource(query)
    transport = ReadOnlyTransport()
    graph = GraphClient(source.token, video_credential_provider=source.video_credential, transport=transport)
    ledger = disk / "fb-ad-asset-delete/tasks.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT object_id,payload,result FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' AND status='failed' ORDER BY ordinal", (args.job_id,)).fetchall()
    assert rows, "No explicitly failed Video objects in this frozen job"
    assert len(rows) <= 100, "This manual probe is limited to 100 Video objects"
    report = dict(read_only=True, job_id=args.job_id, checked_at=datetime.now(timezone.utc).isoformat(), items=[])
    identities = {}
    for oid, payload, result in rows:
        obj = json.loads(payload)
        obj.update(kind="video", object_id=oid, result=json.loads(result))
        token, context = graph.prepare_video_delete(obj)
        assert not set(context) - _CREDENTIAL_FIELDS
        if context.get("credential_kind") == "page":
            key = (context["credential_row_id"], context["credential_page_id"])
            if key not in identities:
                response = transport.request("GET", graph.base + "me", params={"fields": "id,name"},
                    headers={"Authorization": "Bearer " + token}, timeout=10, allow_redirects=False)
                data = response.json()
                identities[key] = dict(http=response.status_code, id=data.get("id"), name=data.get("name"),
                    matches_selected_page=response.status_code == 200 and str(data.get("id")) == key[1])
            identity = identities[key]
        else:
            identity = {"matches_selected_page": False}
        report["items"].append(dict(video_id=oid, credential=context, identity=identity, delete_permission="unverified"))
    report["graph_get_calls"] = transport.calls
    report["meta_delete_calls"] = 0
    report["all_page_identities_verified"] = all(row["identity"]["matches_selected_page"] for row in report["items"])
    path = ledger.parent / ("video-credential-probe-" + args.job_id + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(path), 0o600)
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()

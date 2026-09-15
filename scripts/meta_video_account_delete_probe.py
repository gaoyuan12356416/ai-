"""Resolve frozen account/Video targets and actual ad credentials with GET only."""
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
from features.fb_ad_asset_delete.core import account_id
from features.fb_ad_asset_delete.graph import GraphClient
from features.fb_ad_asset_delete.source import SqlSource


class ReadOnlyTransport:
    def __init__(self):
        self.http = requests.Session()
        self.calls = 0

    def request(self, method, url, **kwargs):
        if method != "GET":
            raise RuntimeError("The probe cannot send Meta writes")
        self.calls += 1
        return self.http.request(method, url, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.job_id)
    disk = Path("/mnt/data-disk")
    assert disk.is_mount() and shutil.disk_usage(disk).free > 100 * 1024**2
    assert subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", str(disk)], text=True).strip() == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert os.access(disk / "fb-ad-asset-delete", os.W_OK)
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
            raise RuntimeError("Read-only source query failed")
        lines = result.stdout.splitlines()
        assert lines and lines[0] == "1"
        return [line.split("\t") for line in lines[1:]]

    source, transport = SqlSource(query), ReadOnlyTransport()
    graph = GraphClient(source.token, video_account_credential_provider=source.video_account_credential, transport=transport)
    ledger = disk / "fb-ad-asset-delete/tasks.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT object_id,payload,result FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' AND status='failed' ORDER BY ordinal", (args.job_id,)).fetchall()
    assert rows and len(rows) <= 100
    report = dict(read_only=True, job_id=args.job_id, checked_at=datetime.now(timezone.utc).isoformat(), items=[])
    identities = {}
    for video_id, payload, result in rows:
        obj = dict(json.loads(payload), kind="video", object_id=video_id, result=json.loads(result))
        for aid in sorted({account_id(value) for value in obj["account_ids"]}):
            token, context = graph.prepare_video_account_delete(obj, aid)
            assert not any(key in context for key in ("token", "access_token", "page_access_token"))
            uid = context["credential_user_id"]
            if uid not in identities:
                response = transport.request("GET", graph.base + "me", params={"fields": "id,name"},
                    headers={"Authorization": "Bearer " + token}, timeout=10, allow_redirects=False)
                data = response.json()
                identities[uid] = dict(http=response.status_code, id=data.get("id"),
                    matches_credential=response.status_code == 200 and str(data.get("id")) == context.get("credential_fb_user_id"))
            report["items"].append(dict(account_id=aid, video_id=video_id, method="DELETE",
                endpoint="act_" + aid + "/advideos", parameters={"video_id": video_id}, credential=context,
                credential_identity=identities[uid], delete_permission="unverified"))
    report.update(graph_get_calls=transport.calls, meta_delete_calls=0,
        all_credentials_verified=all(x["credential_identity"]["matches_credential"] for x in report["items"]))
    path = ledger.parent / ("video-account-delete-probe-" + args.job_id + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(path), 0o600)
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()

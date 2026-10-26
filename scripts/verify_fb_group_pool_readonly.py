"""Read-only production source verification using current broker/identity services.

This script never constructs a Store, writes a pool, creates a short link or
calls Meta. Runtime secrets are loaded into process memory and never printed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_video_publish.sources import SourceRepository, BrokerReadOnlyMySQL
from features.fb_video_publish.group_sources import GroupPoolSource
from features.fb_video_publish.identity_client import IdentityClient
from features.fb_auto_posts.repositories import PagePoolRepository, MaterialRepository

DB = Path("/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3")


def runtime_environment():
    pid = subprocess.check_output(["systemctl", "show", "fb-auto-post-service.service", "-p", "MainPID", "--value"], text=True).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("FB runtime is not active")
    return {key.decode(): value.decode() for item in Path("/proc/" + pid + "/environ").read_bytes().split(b"\0")
            if b"=" in item for key, value in [item.split(b"=", 1)]}


def verify(after_deploy=False):
    env = runtime_environment()
    with closing(sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute("SELECT material_id,snapshot_json FROM fb_video_task ORDER BY created_at_utc DESC LIMIT 1000").fetchall()
        counts = {name: conn.execute("SELECT count(*) FROM " + name).fetchone()[0]
                  for name in ("fb_video_task", "fb_video_link", "fb_video_batch")}
    actors = {}
    for _, snapshot in rows:
        actor = json.loads(snapshot)["actor"]
        actors[(actor["tenant_key"], actor["user_id"])] = actor
    client = IdentityClient(env)
    mysql = BrokerReadOnlyMySQL(env)
    source = GroupPoolSource(SourceRepository(PagePoolRepository(mysql), MaterialRepository(mysql)))
    current = None
    groups = []
    for tenant, user in sorted(actors, key=lambda key: actors[key].get("is_admin") is not True):
        try:
            candidate = client.resolve(tenant, user, navigation_key="fbPostMaterialPool" if after_deploy else "fbPageManagement")
            candidate_groups = source.list_groups(candidate)
            current, groups = candidate, candidate_groups
            if groups:
                break
        except Exception:
            continue
    if current is None:
        raise RuntimeError("No current publishing identity available for read-only verification")
    # A bounded real sample is sufficient; do not walk every Page in large groups.
    sampled_groups = sorted((g for g in groups if 0 < g["total_pages"] <= 30), key=lambda g: g["total_pages"])[:2]
    group_ids = [str(group["id"]) for group in sampled_groups]
    scope = source.resolve_scope(current, group_ids) if group_ids else {"pages": {}, "excluded": []}
    mids = list(dict.fromkeys(str(row[0]) for row in rows))[:5]
    result = source.resolve_materials(mids, need_desc=False)
    output = {"ok": True, "readonly": True, "phase": "after_deploy" if after_deploy else "before_deploy",
              "identity_valid": True, "group_count": len(groups), "checked_groups": group_ids,
              "page_count": len(scope["pages"]), "language_counts": dict(Counter(p["language"] for p in scope["pages"].values())),
              "page_excluded_codes": dict(Counter(item["code"] for item in scope["excluded"])),
              "material_samples": [{"material_id": item["material_id"], "language": item["language"], "duration_seconds": item["duration_seconds"]} for item in result["items"]],
              "material_errors": [{"material_id": item["material_id"], "code": item["code"]} for item in result["errors"]],
              "observed_existing_counts": counts}
    if after_deploy:
        import requests
        headers = {"Authorization": "Bearer " + env["FB_AUTO_POST_INTERNAL_TOKEN"], "X-FB-Video-Actor": json.dumps(current, ensure_ascii=True)}
        responses = {}
        with requests.Session() as session:
            session.trust_env = False
            for suffix in ("/material-pool", "/material-pool/schedule", "/material-pool/runs"):
                response = session.get("http://127.0.0.1:18835/api/fb-video-publish" + suffix, headers=headers, timeout=(3, 20), allow_redirects=False)
                body = response.json()
                if response.status_code != 200 or body.get("ok") is not True:
                    raise RuntimeError("Read-only runtime API verification failed")
                responses[suffix] = {"status_code": response.status_code, "pool_status": body.get("pool", {}).get("status"),
                                     "feature_enabled": body.get("feature_enabled"), "item_count": len(body.get("items", []))}
        output["runtime_api"] = responses
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-deploy", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.after_deploy), ensure_ascii=False))
    except Exception as exc:
        # Do not stringify arbitrary database/HTTP exceptions or environment.
        code = str(getattr(exc, "code", "readonly_verification_failed"))
        print(json.dumps({"ok": False, "readonly": True, "code": code}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Read-only live language-scope evidence; never save, import, enable or publish."""
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_fb_group_pool_readonly as support
from features.fb_video_publish.group_language import POLICY_VERSION


def group_counts():
    with closing(sqlite3.connect("file:" + str(support.DB) + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        return {name: conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in
                ("fb_group_pool", "fb_group_pool_item", "fb_group_pool_operation", "fb_group_pool_day",
                 "fb_group_pool_run", "fb_group_pool_reservation")}


def identity(env):
    with closing(sqlite3.connect("file:" + str(support.DB) + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT snapshot_json FROM fb_video_task ORDER BY created_at_utc DESC LIMIT 1000").fetchall()
    actors = [json.loads(row[0]).get("actor", {}) for row in rows]
    candidate = next(a for a in actors if a.get("is_admin") is True)
    return support.IdentityClient(env).resolve(candidate["tenant_key"], candidate["user_id"], navigation_key="fbPostMaterialPool")


def verify(after_deploy=False):
    import requests
    before = group_counts()
    env = support.runtime_environment()
    actor = identity(env)
    mysql = support.BrokerReadOnlyMySQL(env)
    source = support.GroupPoolSource(support.SourceRepository(support.PagePoolRepository(mysql), support.MaterialRepository(mysql)))
    scenarios = [("two_groups_english", ["42", "43"], {"mode": "selected", "codes": ["en"], "include_blank": False}),
                 ("large_group_english_and_blank", ["3"], {"mode": "selected", "codes": ["en"], "include_blank": True}),
                 ("blank_group_only", ["45"], {"mode": "selected", "codes": [], "include_blank": True})]
    output = {"ok": True, "readonly": True, "phase": "after_deploy" if after_deploy else "before_deploy", "scenarios": []}
    headers = {"Authorization": "Bearer " + env["FB_AUTO_POST_INTERNAL_TOKEN"], "X-FB-Video-Actor": json.dumps(actor, ensure_ascii=True)}
    with requests.Session() as session:
        session.trust_env = False
        for name, groups, selection in scenarios:
            start = time.monotonic()
            if after_deploy:
                response = session.post("http://127.0.0.1:18835/api/fb-video-publish/material-pool/pages/preview",
                                        json={"group_ids": groups, "page_language_selection": selection, "limit": 100},
                                        headers=headers, timeout=(3, 90), allow_redirects=False)
                body = response.json()
                if response.status_code != 200 or body.get("ok") is not True:
                    raise RuntimeError("Readonly target preview failed")
                result = {"name": name, "http_status": response.status_code, "total": body["total"],
                          "eligible": body["eligible_count"], "excluded": body["excluded_count"],
                          "language_summary": body["language_summary"], "returned_count": len(body["items"]),
                          "excluded_codes": dict(Counter(item.get("code", "") for item in body["items"] if not item["eligible"]))}
            else:
                scope = source.resolve_scope(actor, groups, page_language_selection=selection, policy_version=POLICY_VERSION)
                selected_errors = [e for e in scope["excluded"] if e["code"] != "page_language_not_selected"]
                result = {"name": name, "eligible": len(scope["pages"]), "excluded": len(selected_errors),
                          "language_summary": scope["language_summary"],
                          "eligible_languages": dict(Counter(p["language"] or "blank" for p in scope["pages"].values())),
                          "excluded_codes": dict(Counter(e["code"] for e in selected_errors))}
            result["elapsed_seconds"] = round(time.monotonic() - start, 3)
            output["scenarios"].append(result)
        if after_deploy:
            response = session.post("http://127.0.0.1:18835/api/fb-video-publish/material-pool/preview",
                                    json={"group_ids": ["42", "43"], "page_language_selection": scenarios[0][2]},
                                    headers=headers, timeout=(3, 90), allow_redirects=False)
            body = response.json()
            if response.status_code != 200 or body.get("ok") is not True:
                raise RuntimeError("Readonly pairing failed")
            output["pairing"] = {"http_status": response.status_code, "pairs": len(body["pairs"]), "language_capacity": body["language_capacity"]}
    after = group_counts()
    if after != before:
        raise RuntimeError("Group state changed during read-only verification; reconcile")
    output["group_rows_unchanged"] = True
    output["group_row_counts"] = after
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-deploy", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.after_deploy), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "readonly": True, "code": str(getattr(exc, "code", "readonly_verification_failed"))}))
        raise SystemExit(1)

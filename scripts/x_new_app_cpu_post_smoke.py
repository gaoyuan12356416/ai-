#!/usr/bin/env python3
"""Exactly one authorized text Post from the independent CPU test account."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request

import x_new_app_cpu_sandbox as cpu

EXPECTED_USER_ID = "1531581362968272897"
EXPECTED_USERNAME = "gaoyuan30105257"
TEXT = "X API 新 App 独立环境发布测试，2026-10-19。\n这是一条测试 Post。"
RUN_ID = "20261019-new-app-text-v1"


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def request(url, access, *, text=None):
    req = urllib.request.Request(url, method="POST" if text is not None else "GET",
                                 data=json.dumps({"text": text}, ensure_ascii=False).encode() if text is not None else None,
                                 headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"})
    try:
        with cpu.local.oauth._NO_REDIRECT_OPENER.open(req, timeout=30) as response:
            return response.status, json.loads(response.read(128 * 1024))
    except urllib.error.HTTPError as exc:
        raw = exc.read(128 * 1024).decode("utf-8", "replace").replace(access, "[redacted]")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": "non_json_upstream_error"}
        return exc.code, payload


def run(data_dir):
    root = Path(data_dir).resolve()
    if root != Path("/mnt/data-disk/x-new-app-sandbox-20261019/data"):
        raise RuntimeError("Only the named independent test data directory is allowed")
    os.umask(0o077)
    cpu.prepare(root)
    tests = root / "text-post-tests"
    tests.mkdir(mode=0o700, exist_ok=True)
    ledger_path = tests / (RUN_ID + ".json")
    lock = (tests / (RUN_ID + ".lock")).open("a")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if ledger_path.exists():
            existing = json.loads(ledger_path.read_text(encoding="utf-8"))
            # Every existing run is readback-only, including a crash before receipt.
            if existing["status"] == "post_creating":
                existing.update(status="unknown_outcome", unknown_outcome=True)
                save(ledger_path, existing)
            return existing
        conn = cpu.local.oauth.db_connect()
        try:
            rows = conn.execute("SELECT * FROM x_authorized_account ORDER BY id").fetchall()
            if len(rows) != 1:
                raise RuntimeError("Exactly one independent test account is required")
            row = dict(rows[0])
            if row["x_user_id"] != EXPECTED_USER_ID or row["username"] != EXPECTED_USERNAME:
                raise RuntimeError("Test account identity changed")
            with sqlite3.connect(str(tests / (RUN_ID + ".accounts-before.sqlite3"))) as backup:
                conn.backup(backup)
        finally:
            conn.close()
        actor = {"tenant_key": row["owner_tenant_key"], "user_id": row["owner_user_id"],
                 "name": row["owner_name"], "email": row["owner_email"], "role": "admin"}
        record = {"run_id": RUN_ID, "status": "prepared", "account_id": row["id"],
                  "x_user_id": EXPECTED_USER_ID, "username": EXPECTED_USERNAME,
                  "client_id_sha256": hashlib.sha256(cpu.local.oauth.CLIENT_ID.encode()).hexdigest(),
                  "text": TEXT, "attempts": 0, "unknown_outcome": False,
                  "prepared_at": cpu.local.oauth.iso_utc()}
        save(ledger_path, record)
        original_approval = bool(row["publish_approved"])
        try:
            cpu.local.oauth.verify_account(row["id"], actor, preserve_transient_status=True, allow_token_refresh=False)
            cpu.local.oauth.set_account_publish_approval(row["id"], True, actor)
            with cpu.local.oauth.publish_credentials(row["id"], actor) as (item, access):
                if item["x_user_id"] != EXPECTED_USER_ID:
                    raise RuntimeError("Publish credential identity changed")
                record.update(status="post_creating", attempts=1, request_started_at=cpu.local.oauth.iso_utc())
                save(ledger_path, record)
                try:
                    http, response = request("https://api.x.com/2/tweets", access, text=TEXT)
                except Exception:
                    record.update(status="unknown_outcome", unknown_outcome=True, error="write_transport_error")
                    save(ledger_path, record)
                    return record
                record.update(create_http_status=http, create_response=response)
                post_id = str((response.get("data") or {}).get("id", ""))
                if http == 201 and post_id.isdigit():
                    record.update(status="published", post_id=post_id,
                                  preview_url="https://x.com/" + EXPECTED_USERNAME + "/status/" + post_id,
                                  published_at=cpu.local.oauth.iso_utc())
                    save(ledger_path, record)
                    try:
                        read_http, read_payload = request("https://api.x.com/2/tweets/" + post_id + "?tweet.fields=author_id,created_at", access)
                        record.update(read_http_status=read_http, read_response=read_payload)
                        data = read_payload.get("data") or {}
                        verified = read_http == 200 and str(data.get("id")) == post_id and str(data.get("author_id")) == EXPECTED_USER_ID and data.get("text") == TEXT
                        record["status"] = "published_verified" if verified else "published_needs_readback"
                        record["readback_verified"] = verified
                    except Exception:
                        record.update(status="published_needs_readback", readback_verified=False)
                elif 400 <= http < 500 and http != 408:
                    record.update(status="failed_known", unknown_outcome=False)
                else:
                    record.update(status="unknown_outcome", unknown_outcome=True)
                save(ledger_path, record)
        except Exception:
            record.update(status="preflight_failed" if record["attempts"] == 0 else "unknown_outcome",
                          unknown_outcome=record["attempts"] > 0)
            save(ledger_path, record)
            raise
        finally:
            cpu.local.oauth.set_account_publish_approval(row["id"], original_approval, actor)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--live", action="store_true", help="Explicitly publish the one frozen test Post")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"run_id": RUN_ID, "username": EXPECTED_USERNAME, "text": TEXT, "publishes": 0}, ensure_ascii=False))
        return
    result = run(args.data_dir)
    print(json.dumps({key: result.get(key) for key in ("run_id", "status", "post_id", "preview_url", "attempts", "unknown_outcome", "readback_verified", "create_http_status", "read_http_status")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

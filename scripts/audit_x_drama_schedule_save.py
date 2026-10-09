#!/usr/bin/env python3
"""Replay drama schedule saves on a live-ledger snapshot kept only in memory."""
import ast
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_posts import service
from features.x_accounts.client import XAccountsClientError


def snapshot(conn):
    result = {}
    for table in ["x_post_drama_pool", "x_post_queue", "x_post_publish_log", "x_post_repost_ledger", "x_post_drama_delivery_route", "x_authorized_account"]:
        rows = conn.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()}
    return result


def main():
    source = sqlite3.connect("file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    uri = "file:drama-save-audit?mode=memory&cache=shared"
    anchor = sqlite3.connect(uri, uri=True)
    source.backup(anchor)
    source.close()
    before = snapshot(anchor)

    def connect(*_args, **_kwargs):
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    service._connect = connect
    store = service.XPostStore.__new__(service.XPostStore)
    store.db_path = "isolated-memory-snapshot"
    cfg = store.get_schedule_config("drama")
    blockers = store.schedule_account_blockers(cfg["account_ids"])
    excluded = [i for i in cfg["account_ids"] if blockers.get(i, {}).get("code") == "x_account_not_publishable"]
    selected = [i for i in cfg["account_ids"] if i not in excluded]
    settings = {k: cfg[k] for k in ["enabled", "timezone", "body_template", "version"]}
    settings.update(account_ids=selected, publish_times=[], schedule_mode="random", random_daily_count=1)
    result = store.save_schedule_config("drama", settings, {"user_id": "diagnostic", "name": "diagnostic"}, eligible_account_ids=selected)
    assert result["account_ids"] == selected and result["random_daily_count"] == 1
    candidates = store.available_drama_pool_items(account_ids=selected)
    assert all(r["candidate_account_id"] in selected for r in candidates)
    assert snapshot(anchor) == before, "historical ledger or bindings changed in replay"
    tree = ast.parse((ROOT / "app.py").read_text())
    nodes = [n for n in tree.body if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "X_ACCOUNTS_ERROR_META" for t in n.targets)) or (isinstance(n, ast.FunctionDef) and n.name == "x_accounts_error_payload")]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "app-mapping", "exec"), namespace)
    code = "x_post_drama_owner_not_configured"
    message = "Unfinished drama D1 requires account 2"
    status, payload = namespace["x_accounts_error_payload"](XAccountsClientError(code, message, 409))
    assert status == 409 and payload["error"] == code and payload["message"] == message
    print(json.dumps({"mode": "read_only_live_snapshot_in_memory", "excluded_account_ids": excluded,
                      "selected_account_ids": selected, "saved_in_memory": True,
                      "random_daily_count": result["random_daily_count"], "random_effective_date": result["random_effective_date"],
                      "candidate_account_ids": [r["candidate_account_id"] for r in candidates],
                      "binding_and_ledger_unchanged": True, "error_mapping_status": status, "ledger": before}))


if __name__ == "__main__":
    main()

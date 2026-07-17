#!/usr/bin/env python3
"""Read-only, token-safe V3 identity smoke test for one authenticated user."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple


DEFAULT_DB = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
DEFAULT_BASE_URL = "http://127.0.0.1:8787/api/ad-control/v3"


def active_session(database: str, user_id: str) -> Tuple[str, str]:
    connection = sqlite3.connect("file:%s?mode=ro" % database, uri=True)
    try:
        row = connection.execute(
            """
            SELECT session_token, name
            FROM drama_admin_session
            WHERE user_id=? AND expires_at>?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, int(time.time())),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise RuntimeError("no active session for requested user")
    return str(row[0]), str(row[1] or "")


def get_json(url: str, cookie_name: str, token: str) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={"Cookie": "%s=%s" % (cookie_name, token), "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        body = error.read()
        status = int(error.code)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"code": "non_json_response"}
    return status, payload if isinstance(payload, dict) else {"value": payload}


def integer_ids(values: Any) -> List[int]:
    result: List[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return sorted(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--expected-optimizer-ids", required=True)
    parser.add_argument("--database", default=DEFAULT_DB)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cookie-name", default="drama_admin_session")
    args = parser.parse_args()

    expected = sorted({int(value) for value in args.expected_optimizer_ids.split(",") if value.strip()})
    token, session_name = active_session(args.database, args.user_id)
    meta_status, meta = get_json(args.base_url.rstrip("/") + "/meta", args.cookie_name, token)
    groups_status, groups = get_json(
        args.base_url.rstrip("/") + "/rule-groups?page=1&page_size=20",
        args.cookie_name,
        token,
    )

    actor = meta.get("actor") if isinstance(meta.get("actor"), dict) else {}
    is_admin = bool(actor.get("is_admin"))
    actor_ids = integer_ids(actor.get("optimizer_ids"))
    optimizer_items = meta.get("optimizers") if isinstance(meta.get("optimizers"), list) else []
    selectable_ids = integer_ids([
        item.get("optimizer_id")
        for item in optimizer_items
        if isinstance(item, dict)
    ])
    expected_in_actor_scope = set(expected).issubset(actor_ids)
    expected_selectable = set(expected).issubset(selectable_ids) if is_admin else True
    expected_present = expected_in_actor_scope and expected_selectable
    resolved = [
        {
            "optimizer_id": int(item.get("optimizer_id")),
            "name": str(item.get("name") or ""),
        }
        for item in optimizer_items
        if isinstance(item, dict)
        and str(item.get("optimizer_id") or "").isdigit()
        and int(item.get("optimizer_id")) in expected
    ]
    result = {
        "ok": meta_status == 200 and groups_status == 200 and expected_present,
        "user_id": args.user_id,
        "session_name": session_name,
        "meta_status": meta_status,
        "rule_groups_status": groups_status,
        "is_admin": is_admin,
        "identity_check": "admin_self_alias_scope" if is_admin else "actor_scope",
        "actor_optimizer_ids": actor_ids,
        "expected_optimizer_ids": expected,
        "expected_optimizer_ids_in_actor_scope": expected_in_actor_scope,
        "expected_optimizer_ids_selectable": expected_selectable,
        "expected_optimizer_ids_present": expected_present,
        "resolved_expected_optimizers": resolved,
        "rule_group_total": int(groups.get("total") or 0) if groups_status == 200 else None,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

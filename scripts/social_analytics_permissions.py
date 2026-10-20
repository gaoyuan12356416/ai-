#!/usr/bin/env python3
"""Freeze/apply/revert only the report module permission inherited from old access."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
MODULE = "social_analytics"
CONSTANTS = {"MODULE_PERMISSIONS", "DEFAULT_USER_PERMISSIONS", "ADMIN_PERMISSIONS"}
FUNCTIONS = {
    "parse_json_text", "normalize_user_permissions", "has_module_permission",
    "navigation_item_access", "validate_navigation_config",
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def load_policy(path):
    """Load only pure authorization rules, never import app/start publisher stores."""
    source = Path(path).read_text(encoding="utf-8-sig")
    parsed = ast.parse(source)
    nodes = []
    for node in parsed.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
    selected = ast.Module(body=nodes, type_ignores=[])
    namespace = {"json": json}
    exec(compile(selected, str(path), "exec"), namespace)
    if not (CONSTANTS | FUNCTIONS) <= namespace.keys():
        raise ValueError("authorization_policy_incomplete")
    policy_hash = sha256(ast.dump(selected, include_attributes=False).encode())
    return namespace, policy_hash


def _permissions(raw):
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("permissions_must_be_object")
    return value


def _connect(path, readonly=True):
    if readonly:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
    else:
        # mode=rw prevents an accidental new empty database at a mistyped path.
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def build_manifest(db_path, navigation_path, app_source=ROOT / "app.py"):
    policy, policy_hash = load_policy(app_source)
    nav_bytes = Path(navigation_path).read_bytes()
    navigation = policy["validate_navigation_config"](json.loads(nav_bytes.decode("utf-8-sig")))
    if not any(item.get("key") == "youtubeAnalytics" for group in navigation for item in group.get("items", [])):
        raise ValueError("old_youtube_report_navigation_missing")
    with closing(_connect(db_path)) as connection:
        users = [dict(row) for row in connection.execute(
            "SELECT user_id,tenant_key,name,role,permissions_json,updated_at FROM drama_admin_user ORDER BY user_id"
        )]
    changes, decisions = [], []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    seen = set()
    for user in users:
        if user["user_id"] in seen:
            raise ValueError("duplicate_admin_user_id")
        seen.add(user["user_id"])
        permissions = _permissions(user["permissions_json"])
        session = dict(user, permissions=permissions)
        module_allowed = policy["has_module_permission"](session, "youtube_auto_publish")
        access = policy["navigation_item_access"](session, "youtubeAnalytics", navigation)
        if MODULE in permissions:
            reason = "already_explicitly_configured"
        elif user["role"] == "admin":
            reason = "admin_implicit_module_access"
        elif not module_allowed:
            reason = "no_previous_youtube_permission"
        elif not access.get("allowed"):
            reason = "no_previous_report_navigation_access"
        elif not user["user_id"] or not user["tenant_key"]:
            reason = "incomplete_user_identity"
        else:
            after = dict(permissions, **{MODULE: True})
            changes.append({
                "user_id": user["user_id"], "tenant_key": user["tenant_key"], "role": user["role"],
                "before_permissions_json": user["permissions_json"],
                "after_permissions_json": compact(after),
                "before_updated_at": user["updated_at"], "after_updated_at": stamp,
                "reason": "inherit_existing_youtube_report_access",
            })
            reason = "grant_social_analytics"
        decisions.append({"user_id": user["user_id"], "tenant_key": user["tenant_key"], "reason": reason})
    value = {
        "schema_version": SCHEMA, "module": MODULE, "created_at": stamp,
        "db_path": str(Path(db_path).resolve()), "navigation_path": str(Path(navigation_path).resolve()),
        "navigation_sha256": sha256(nav_bytes), "policy_sha256": policy_hash,
        "changes": changes, "decisions": decisions,
    }
    value["manifest_sha256"] = sha256(compact(value).encode())
    return value


def verify_manifest(manifest):
    data = dict(manifest)
    digest = data.pop("manifest_sha256", "")
    if digest != sha256(compact(data).encode()) or data.get("schema_version") != SCHEMA or data.get("module") != MODULE:
        raise ValueError("permission_manifest_invalid")
    seen = set()
    for change in data["changes"]:
        if change["user_id"] in seen or change["role"] == "admin":
            raise ValueError("permission_manifest_invalid_user")
        seen.add(change["user_id"])
        before = _permissions(change["before_permissions_json"])
        after = _permissions(change["after_permissions_json"])
        if MODULE in before or after != dict(before, **{MODULE: True}):
            raise ValueError("permission_manifest_scope_violation")


def apply_manifest(db_path, manifest, *, rollback=False, app_source=ROOT / "app.py"):
    verify_manifest(manifest)
    if str(Path(db_path).resolve()) != manifest["db_path"]:
        raise ValueError("permission_database_path_mismatch")
    _, policy_hash = load_policy(app_source)
    if policy_hash != manifest["policy_sha256"]:
        raise ValueError("authorization_policy_changed")
    if sha256(Path(manifest["navigation_path"]).read_bytes()) != manifest["navigation_sha256"]:
        raise ValueError("frozen_navigation_changed")
    source, target = ("after", "before") if rollback else ("before", "after")
    changed, skipped = [], []
    with closing(_connect(db_path, readonly=False)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for change in manifest["changes"]:
                row = connection.execute(
                    "SELECT user_id,tenant_key,role,permissions_json,updated_at FROM drama_admin_user WHERE user_id=?",
                    (change["user_id"],),
                ).fetchone()
                if not row or row["tenant_key"] != change["tenant_key"] or row["role"] != change["role"]:
                    raise ValueError("permission_identity_drift")
                expected = (change[source + "_permissions_json"], change[source + "_updated_at"])
                desired = (change[target + "_permissions_json"], change[target + "_updated_at"])
                current = (row["permissions_json"], row["updated_at"])
                if current == desired:
                    skipped.append(change["user_id"])
                elif current == expected:
                    changed.append(change["user_id"])
                else:
                    raise ValueError("permission_row_drift")
            for change in manifest["changes"]:
                if change["user_id"] not in changed:
                    continue
                cursor = connection.execute(
                    "UPDATE drama_admin_user SET permissions_json=?,updated_at=? WHERE user_id=? "
                    "AND permissions_json IS ? AND updated_at IS ?",
                    (change[target + "_permissions_json"], change[target + "_updated_at"], change["user_id"],
                     change[source + "_permissions_json"], change[source + "_updated_at"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("permission_cas_failed")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {"status": "rolled_back" if rollback else "applied", "changed_users": changed,
            "already_at_target": skipped, "manifest_sha256": manifest["manifest_sha256"]}


def write_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A frozen backup cannot be silently overwritten by a second dry-run.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True, help="Frozen per-user JSON backup")
    parser.add_argument("--navigation", help="Immutable original navigation snapshot; required for dry-run")
    parser.add_argument("--app-source", default=str(ROOT / "app.py"))
    parser.add_argument("--mode", choices=["dry-run", "apply", "rollback"], default="dry-run")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.mode == "dry-run":
        if not args.navigation:
            parser.error("--navigation is required for dry-run")
        manifest = build_manifest(args.db, args.navigation, args.app_source)
        write_manifest(args.manifest, manifest)
        print(json.dumps({"status": "prepared", "changes": len(manifest["changes"]),
                          "users_checked": len(manifest["decisions"]), "manifest": args.manifest,
                          "manifest_sha256": manifest["manifest_sha256"]}, ensure_ascii=False))
    else:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        print(json.dumps(apply_manifest(args.db, manifest, rollback=args.mode == "rollback",
                                       app_source=args.app_source), ensure_ascii=False))


if __name__ == "__main__":
    main()

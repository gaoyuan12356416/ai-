#!/usr/bin/env python3
"""Safely assign the three reviewed legacy rule groups to their real owner."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


EXPECTED_GROUP_COUNT = 3
REVIEWED_OWNER = "892fd2e8"
REVIEWED_CREATED_BY = "codex"
REVIEWED_GROUP_STATES = {
    "frg_plus8_non_asian_lang_10am_dramawave_binding": ("dramawave", 1, 0, 0),
    "frg_plus8_non_asian_lang_10am_freereels_binding": ("freereels", 0, 0, 1),
    "frg_plus8_non_asian_lang_10am_hotdrama_binding": ("hotdrama", 0, 0, 1),
}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_expected_group_states(values):
    states = {}
    for value in values or []:
        parts = str(value or "").rsplit(":", 4)
        if len(parts) != 5 or not parts[0].strip() or not parts[1].strip():
            raise RuntimeError(
                "expected group state must be "
                "GROUP_ID:PRODUCT:ENABLED:EMERGENCY:DELETED"
            )
        group_id = parts[0].strip()
        if group_id in states:
            raise RuntimeError("duplicate expected group id: %s" % group_id)
        try:
            flags = tuple(int(item) for item in parts[2:])
        except ValueError:
            raise RuntimeError("expected group state flags must be 0 or 1: %s" % value)
        if any(item not in (0, 1) for item in flags):
            raise RuntimeError("expected group state flags must be 0 or 1: %s" % value)
        states[group_id] = (parts[1].strip(),) + flags
    if len(states) != EXPECTED_GROUP_COUNT:
        raise RuntimeError("exactly three unique expected group states are required")
    return states


def require_absolute_file(value, label):
    path = Path(value or "")
    if not path.is_absolute():
        raise RuntimeError("%s must be an absolute path" % label)
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError("%s does not exist: %s" % (label, path))
    return path


def columns(conn, table):
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)]


def rows_as_dicts(cursor):
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def normalized_account_ids(value):
    try:
        items = json.loads(value or "[]")
    except Exception:
        raise RuntimeError("account_ids_json is invalid JSON")
    if not isinstance(items, list):
        raise RuntimeError("account_ids_json must be a list")
    normalized = tuple(str(item or "").strip() for item in items)
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise RuntimeError("account_ids_json contains blank or duplicate IDs")
    return normalized


def normalized_rules(value, label):
    try:
        rules = json.loads(value or "[]")
    except Exception:
        raise RuntimeError("%s rules_json is invalid JSON" % label)
    if not isinstance(rules, list) or not rules:
        raise RuntimeError("%s rules_json must be a non-empty list" % label)
    return rules


def read_snapshot(db_path, expected_states, expected_created_by, require_ensured=False):
    uri = "%s?mode=ro" % Path(db_path).resolve().as_uri()
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("SQLite integrity_check failed: %s" % integrity)
        group_columns = columns(conn, "ad_control_rule_group")
        if not group_columns:
            raise RuntimeError("missing ad_control_rule_group")
        required = {
            "group_id", "product", "account_group_id", "account_ids_json",
            "enabled", "emergency_stopped", "created_by", "deleted",
        }
        if not required.issubset(set(group_columns)):
            raise RuntimeError("ad_control_rule_group is missing required legacy columns")
        if require_ensured and not {
            "owner_user_id", "rule_set_id", "strategy_json", "object_level", "run_mode"
        }.issubset(set(group_columns)):
            raise RuntimeError("target schema ensure did not add all V2 rule group columns")
        group_rows = rows_as_dicts(conn.execute(
            "SELECT * FROM ad_control_rule_group ORDER BY group_id"
        ))
        actual_ids = {str(row.get("group_id") or "") for row in group_rows}
        if actual_ids != set(expected_states) or len(group_rows) != EXPECTED_GROUP_COUNT:
            raise RuntimeError("rule group ID set does not match the three reviewed groups")

        owners = []
        stable_rows = {}
        full_rows = {}
        linked_pools = {}
        linked_rule_sets = {}
        pool_columns = columns(conn, "ad_control_account_group")
        for row in group_rows:
            group_id = str(row["group_id"])
            actual_state = (
                int(row["enabled"] or 0),
                int(row["emergency_stopped"] or 0),
                int(row["deleted"] or 0),
            )
            expected_product, expected_enabled, expected_emergency, expected_deleted = (
                expected_states[group_id]
            )
            if actual_state != (expected_enabled, expected_emergency, expected_deleted):
                raise RuntimeError("unexpected enabled/emergency/deleted state: %s" % group_id)
            if str(row.get("product") or "") != expected_product:
                raise RuntimeError("unexpected product: %s" % group_id)
            if str(row.get("created_by") or "") != expected_created_by:
                raise RuntimeError("unexpected created_by: %s" % group_id)
            normalized_rules(row.get("rules_json") or "[]", group_id)
            owners.append(str(row.get("owner_user_id") or ""))
            stable_rows[group_id] = {
                key: row.get(key)
                for key in (
                    "group_id", "name", "product", "account_group_id",
                    "account_ids_json", "rules_json", "enabled",
                    "emergency_stopped", "last_preview_id", "last_preview_hash",
                    "last_run_at", "last_result_json", "created_by",
                    "created_at", "updated_at", "deleted",
                )
                if key in row
            }
            full_rows[group_id] = {
                key: value for key, value in row.items() if key != "owner_user_id"
            }
            embedded_account_ids = normalized_account_ids(
                row.get("account_ids_json") or "[]"
            )
            pool_id = str(row.get("account_group_id") or "")
            if not pool_id:
                linked_pools[group_id] = None
                resolved_account_ids = embedded_account_ids
            else:
                if not pool_columns:
                    raise RuntimeError("missing linked account group table")
                pool_cursor = conn.execute(
                    "SELECT group_id,product,account_ids_json,created_by,deleted "
                    "FROM ad_control_account_group WHERE group_id=?",
                    (pool_id,),
                )
                pool_row = pool_cursor.fetchone()
                if pool_row is None:
                    raise RuntimeError("missing linked account group: %s" % pool_id)
                pool = dict(zip(
                    [item[0] for item in pool_cursor.description], pool_row
                ))
                if int(pool["deleted"] or 0) != 0:
                    raise RuntimeError("linked account group is deleted: %s" % pool_id)
                if str(pool["product"] or "") != str(row.get("product") or ""):
                    raise RuntimeError("linked account group product mismatch: %s" % pool_id)
                if str(pool["created_by"] or "") != expected_created_by:
                    raise RuntimeError("linked account group creator mismatch: %s" % pool_id)
                pool["parsed_account_ids"] = normalized_account_ids(
                    pool["account_ids_json"]
                )
                linked_pools[group_id] = pool
                resolved_account_ids = pool["parsed_account_ids"]
            if int(row["enabled"] or 0) == 1 and not resolved_account_ids:
                raise RuntimeError("active rule group has no resolved account IDs: %s" % group_id)

            if require_ensured:
                rule_set_id = str(row.get("rule_set_id") or "")
                if not rule_set_id:
                    raise RuntimeError("ensured rule group has no rule_set_id: %s" % group_id)
                rule_set_cursor = conn.execute(
                    "SELECT rule_set_id,product,rules_json,created_by,deleted "
                    "FROM ad_control_rule_set WHERE rule_set_id=?",
                    (rule_set_id,),
                )
                rule_set_row = rule_set_cursor.fetchone()
                if rule_set_row is None:
                    raise RuntimeError("missing ensured rule set: %s" % rule_set_id)
                rule_set = dict(zip(
                    [item[0] for item in rule_set_cursor.description], rule_set_row
                ))
                if str(rule_set["product"] or "") != expected_product:
                    raise RuntimeError("ensured rule set product mismatch: %s" % rule_set_id)
                if str(rule_set["created_by"] or "") != expected_created_by:
                    raise RuntimeError("ensured rule set creator mismatch: %s" % rule_set_id)
                if int(rule_set["deleted"] or 0) != 0:
                    raise RuntimeError("ensured rule set is deleted: %s" % rule_set_id)
                normalized_rules(rule_set["rules_json"], rule_set_id)
                linked_rule_sets[group_id] = rule_set

        owner_set = set(owners)
        if len(owner_set) != 1:
            raise RuntimeError("mixed rule group owner state is not allowed")

        if not columns(conn, "ad_control_rule"):
            raise RuntimeError("missing legacy standalone ad_control_rule")
        standalone_total, standalone_enabled = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),0) "
            "FROM ad_control_rule"
        ).fetchone()
        if int(standalone_total) != 0 or int(standalone_enabled) != 0:
            raise RuntimeError("legacy standalone baseline must remain 0/0")

        return {
            "owners": owner_set,
            "stable_rows": stable_rows,
            "full_rows_without_owner": full_rows,
            "linked_pools": linked_pools,
            "linked_rule_sets": linked_rule_sets,
            "standalone": (int(standalone_total), int(standalone_enabled)),
            "integrity": "ok",
        }
    finally:
        conn.close()


def ensure_release_schema(app_path, db_path):
    app_path = Path(app_path).resolve()
    os.environ["DRAMA_JOB_DB_PATH"] = str(Path(db_path).resolve())
    sys.path.insert(0, str(app_path.parent))
    try:
        spec = importlib.util.spec_from_file_location(
            "_ad_control_v2_release_app", str(app_path)
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load release app: %s" % app_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.JOB_DB_PATH = str(Path(db_path).resolve())
        module.ensure_ad_control_tables()
    finally:
        if sys.path and sys.path[0] == str(app_path.parent):
            sys.path.pop(0)


def verify_transaction_baseline(conn, ensured, expected_states):
    rows = conn.execute(
        "SELECT * FROM ad_control_rule_group ORDER BY group_id"
    ).fetchall()
    if len(rows) != EXPECTED_GROUP_COUNT or {
        str(row["group_id"] or "") for row in rows
    } != set(expected_states):
        raise RuntimeError("rule group set drifted before owner transaction")
    full_rows = {
        str(row["group_id"]): {
            key: row[key] for key in row.keys() if key != "owner_user_id"
        }
        for row in rows
    }
    if full_rows != ensured["full_rows_without_owner"]:
        raise RuntimeError("rule group behavior drifted before owner transaction")

    pools = {}
    rule_sets = {}
    for row in rows:
        group_id = str(row["group_id"])
        pool_id = str(row["account_group_id"] or "")
        if pool_id:
            cursor = conn.execute(
                "SELECT group_id,product,account_ids_json,created_by,deleted "
                "FROM ad_control_account_group WHERE group_id=?",
                (pool_id,),
            )
            value = cursor.fetchone()
            if value is None:
                raise RuntimeError("linked account group drifted before owner transaction")
            pool = dict(value)
            pool["parsed_account_ids"] = normalized_account_ids(pool["account_ids_json"])
            pools[group_id] = pool
        else:
            pools[group_id] = None
        rule_set_id = str(row["rule_set_id"] or "")
        cursor = conn.execute(
            "SELECT rule_set_id,product,rules_json,created_by,deleted "
            "FROM ad_control_rule_set WHERE rule_set_id=?",
            (rule_set_id,),
        )
        value = cursor.fetchone()
        if value is None:
            raise RuntimeError("linked rule set drifted before owner transaction")
        rule_sets[group_id] = dict(value)
    if pools != ensured["linked_pools"]:
        raise RuntimeError("linked account groups drifted before owner transaction")
    if rule_sets != ensured["linked_rule_sets"]:
        raise RuntimeError("linked rule sets drifted before owner transaction")

    standalone = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),0) "
        "FROM ad_control_rule"
    ).fetchone()
    if tuple(int(item) for item in standalone) != ensured["standalone"]:
        raise RuntimeError("standalone baseline drifted before owner transaction")
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise RuntimeError("SQLite integrity drifted before owner transaction")
    return {str(row["owner_user_id"] or "") for row in rows}


def migrate_database(
    db_path,
    app_path,
    owner,
    expected_created_by,
    expected_states,
    ensure_schema=ensure_release_schema,
    before_owner_transaction=None,
):
    before = read_snapshot(db_path, expected_states, expected_created_by)
    allowed_before = {"", expected_created_by, owner}
    if not before["owners"].issubset(allowed_before):
        raise RuntimeError("foreign owner state is not allowed")

    ensure_schema(app_path, db_path)
    ensured = read_snapshot(
        db_path, expected_states, expected_created_by, require_ensured=True
    )
    if ensured["stable_rows"] != before["stable_rows"]:
        raise RuntimeError("schema ensure changed stable rule group behavior")
    if ensured["linked_pools"] != before["linked_pools"]:
        raise RuntimeError("schema ensure changed linked account groups")
    if ensured["standalone"] != before["standalone"]:
        raise RuntimeError("schema ensure changed standalone baseline")
    if ensured["owners"] not in ({expected_created_by}, {owner}):
        raise RuntimeError("post-ensure owner state is mixed or foreign")
    if before_owner_transaction is not None:
        before_owner_transaction(db_path)

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    updated_count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        transaction_owners = verify_transaction_baseline(
            conn, ensured, expected_states
        )
        if transaction_owners == {expected_created_by}:
            for group_id in sorted(expected_states):
                cursor = conn.execute(
                    "UPDATE ad_control_rule_group SET owner_user_id=? "
                    "WHERE group_id=? AND created_by=? AND owner_user_id=?",
                    (owner, group_id, expected_created_by, expected_created_by),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("owner update rowcount mismatch: %s" % group_id)
                updated_count += 1
            if updated_count != EXPECTED_GROUP_COUNT:
                raise RuntimeError("first owner migration must update exactly three rows")
        elif transaction_owners == {owner}:
            updated_count = 0
        else:
            raise RuntimeError("mixed owner state changed before owner transaction")
        post_update_owners = verify_transaction_baseline(
            conn, ensured, expected_states
        )
        if post_update_owners != {owner}:
            raise RuntimeError("owner transaction did not converge before commit")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    after_update = read_snapshot(
        db_path, expected_states, expected_created_by, require_ensured=True
    )
    if after_update["owners"] != {owner}:
        raise RuntimeError("owner migration did not converge to the requested owner")
    if after_update["full_rows_without_owner"] != ensured["full_rows_without_owner"]:
        raise RuntimeError("owner transaction changed non-owner rule group fields")
    if after_update["linked_pools"] != ensured["linked_pools"]:
        raise RuntimeError("owner transaction changed linked account groups")
    if after_update["linked_rule_sets"] != ensured["linked_rule_sets"]:
        raise RuntimeError("owner transaction changed linked rule sets")
    if after_update["standalone"] != ensured["standalone"]:
        raise RuntimeError("owner transaction changed standalone baseline")

    ensure_schema(app_path, db_path)
    final = read_snapshot(
        db_path, expected_states, expected_created_by, require_ensured=True
    )
    if final["owners"] != {owner}:
        raise RuntimeError("second schema ensure reverted owner assignment")
    if final["full_rows_without_owner"] != after_update["full_rows_without_owner"]:
        raise RuntimeError("second schema ensure changed rule group behavior")
    if final["linked_pools"] != after_update["linked_pools"]:
        raise RuntimeError("second schema ensure changed linked account groups")
    if final["linked_rule_sets"] != after_update["linked_rule_sets"]:
        raise RuntimeError("second schema ensure changed linked rule sets")
    if final["standalone"] != after_update["standalone"]:
        raise RuntimeError("second schema ensure changed standalone baseline")
    return {
        "updated_count": updated_count,
        "group_count": EXPECTED_GROUP_COUNT,
        "linked_pool_count": sum(1 for value in final["linked_pools"].values() if value),
        "standalone_total": final["standalone"][0],
        "standalone_enabled": final["standalone"][1],
        "integrity": final["integrity"],
    }


def backup_sqlite(source_path, target_path):
    source_uri = "%s?mode=ro" % Path(source_path).resolve().as_uri()
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def run_release(
    db_path,
    app_path,
    owner,
    expected_created_by,
    expected_states,
    apply,
    ensure_schema=ensure_release_schema,
):
    if apply:
        result = migrate_database(
            db_path, app_path, owner, expected_created_by, expected_states,
            ensure_schema=ensure_schema,
        )
        result.update({"mode": "apply", "database": str(db_path)})
        return result

    before_sha256 = sha256_file(db_path)
    with tempfile.TemporaryDirectory(prefix="ad-control-v2-owner-check-") as value:
        rehearsal_db = Path(value) / "rehearsal.sqlite3"
        backup_sqlite(db_path, rehearsal_db)
        result = migrate_database(
            rehearsal_db, app_path, owner, expected_created_by, expected_states,
            ensure_schema=ensure_schema,
        )
    after_sha256 = sha256_file(db_path)
    if after_sha256 != before_sha256:
        raise RuntimeError("dry-run changed the source SQLite database")
    result.update({
        "mode": "check",
        "database": str(db_path),
        "source_sha256": before_sha256,
    })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--expected-created-by", required=True)
    parser.add_argument("--expected-group-state", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        db_path = require_absolute_file(args.db, "--db")
        app_path = require_absolute_file(args.app, "--app")
        owner = str(args.owner or "").strip()
        expected_created_by = str(args.expected_created_by or "").strip()
        if owner != REVIEWED_OWNER or expected_created_by != REVIEWED_CREATED_BY:
            raise RuntimeError(
                "this one-time migration is locked to owner=%s created_by=%s"
                % (REVIEWED_OWNER, REVIEWED_CREATED_BY)
            )
        states = parse_expected_group_states(args.expected_group_state)
        if states != REVIEWED_GROUP_STATES:
            raise RuntimeError(
                "expected group IDs/products/states do not match the reviewed baseline"
            )
        result = run_release(
            db_path, app_path, owner, expected_created_by, states, args.apply
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

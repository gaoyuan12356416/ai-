#!/usr/bin/env python3
"""Add the three external-id idempotency keys to the live unified ledger.

The migration is additive and rerunnable.  It never creates or drops a legacy
table and never reads or prints credential values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import pymysql


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.unified_youtube_rpc import (  # noqa: E402
    ACCOUNT_HOST,
    MIGRATOR_TABLE_PRIVILEGES,
    MIGRATOR_USER,
    REQUIRED_COLUMN_DEFINITIONS,
    SCHEMA,
    UnifiedYouTubeLedger,
    WRITER_USER,
    load_database_credential_file,
    validate_exact_account_grants,
    validate_required_schema_rows,
)
from features.drama_synthesis.unified_youtube import read_secure_owned_file  # noqa: E402


CLUSTER_ID = "cynosdbmysql-5kxxsre7"
HOST = "101.32.56.53"
PORT = 63353
USER = MIGRATOR_USER
MIGRATIONS = {
    "ads_youtube_videos": {
        "column": "drama_external_video_id",
        "column_sql": "VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL DEFAULT NULL",
        "index": "ux_ads_youtube_videos_drama_external_video_id",
    },
    "ads_youtube_comments": {
        "column": "drama_external_comment_id",
        "column_sql": "VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL DEFAULT NULL",
        "index": "ux_ads_youtube_comments_drama_external_comment_id",
    },
    "ads_youtube_publish_log": {
        "column": "drama_external_publish_id",
        "column_sql": "VARCHAR(19) CHARACTER SET ascii COLLATE ascii_bin NULL DEFAULT NULL",
        "index": "ux_ads_youtube_publish_log_drama_external_publish_id",
    },
}


def _migration_contract_sha256() -> str:
    contract = {
        "migrations": MIGRATIONS,
        "required_column_definitions": {
            table: {
                column: list(definition)
                for column, definition in definitions.items()
            }
            for table, definitions in REQUIRED_COLUMN_DEFINITIONS.items()
        },
    }
    encoded = json.dumps(contract, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


MIGRATION_CONTRACT_SHA256 = _migration_contract_sha256()

BACKUP_EVIDENCE_KEYS = {
    "cluster_id", "schema", "backup_id", "backup_status", "backup_completed_at_utc",
    "verified_at_utc", "verification_source", "rehearsal_status", "rehearsal_at_utc",
    "restore_instance_id", "migration_contract_sha256", "candidate_git_sha",
    "rehearsal_result_sha256",
}


def _utc_timestamp(value: Any) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise RuntimeError("backup evidence timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError:
        raise RuntimeError("backup evidence timestamp is invalid") from None
    return parsed


def load_backup_evidence_file(path_text: str, *, candidate_git_sha: str = "") -> Mapping[str, Any]:
    try:
        raw = read_secure_owned_file(path_text, max_bytes=8192)
        value = json.loads(raw.decode("utf-8"))
    except (RuntimeError, UnicodeDecodeError, ValueError):
        raise RuntimeError("verified backup evidence file is invalid") from None
    if not isinstance(value, Mapping) or set(value) != BACKUP_EVIDENCE_KEYS:
        raise RuntimeError("verified backup evidence file is invalid")
    if (
        value.get("cluster_id") != CLUSTER_ID
        or value.get("schema") != SCHEMA
        or value.get("backup_status") != "SUCCESS"
        or value.get("verification_source") != "tencent_cynosdb_api"
        or value.get("rehearsal_status") != "PASS"
        or not re.fullmatch(r"[A-Za-z0-9_.:/+-]{8,200}", str(value.get("backup_id") or ""))
        or not re.fullmatch(r"cynosdbmysql-[a-z0-9]{4,64}", str(value.get("restore_instance_id") or ""))
        or value.get("restore_instance_id") == CLUSTER_ID
        or value.get("migration_contract_sha256") != MIGRATION_CONTRACT_SHA256
        or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("candidate_git_sha") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("rehearsal_result_sha256") or ""))
        or (candidate_git_sha and value.get("candidate_git_sha") != candidate_git_sha)
    ):
        raise RuntimeError("verified backup evidence file is invalid")
    completed = _utc_timestamp(value.get("backup_completed_at_utc"))
    verified = _utc_timestamp(value.get("verified_at_utc"))
    rehearsal = _utc_timestamp(value.get("rehearsal_at_utc"))
    now = datetime.now(timezone.utc)
    if not (
        completed <= rehearsal <= verified <= now + timedelta(minutes=5)
        and now - completed <= timedelta(hours=48)
        and now - verified <= timedelta(hours=4)
    ):
        raise RuntimeError("verified backup evidence is stale or inconsistent")
    return dict(value, evidence_sha256=hashlib.sha256(raw).hexdigest())


def _connect(config: Mapping[str, Any], *, expected_user: str = USER):
    if (
        config.get("host") != HOST
        or config.get("port") != PORT
        or config.get("user") != expected_user
        or config.get("database") != SCHEMA
    ):
        raise RuntimeError("migration database target is invalid")
    return pymysql.connect(
        host=str(config["host"]),
        port=int(config["port"]),
        user=str(config["user"]),
        password=str(config["password"]),
        database=str(config["database"]),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _inspect(cursor: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for table, spec in MIGRATIONS.items():
        cursor.execute(
            "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,"
            "CHARACTER_SET_NAME,COLLATION_NAME,EXTRA "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (SCHEMA, table),
        )
        column_rows = list(cursor.fetchall())
        columns = {str(row["COLUMN_NAME"]): row for row in column_rows}
        if "id" not in columns:
            raise RuntimeError("required unified table is unavailable")
        column = columns.get(spec["column"])
        cursor.execute(
            "SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s "
            "ORDER BY SEQ_IN_INDEX",
            (SCHEMA, table, spec["index"]),
        )
        index_rows = list(cursor.fetchall())
        if column is not None:
            cursor.execute(
                "SELECT COUNT(*) AS duplicate_groups FROM ("
                "SELECT 1 FROM `%s`.`%s` WHERE `%s` IS NOT NULL "
                "GROUP BY `%s` HAVING COUNT(*)>1 LIMIT 1) duplicate_probe"
                % (SCHEMA, table, spec["column"], spec["column"])
            )
            duplicate_groups = int(cursor.fetchone()["duplicate_groups"])
        else:
            duplicate_groups = 0
        result[table] = {
            "columns": column_rows,
            "column": dict(column) if column else None,
            "index": [dict(row) for row in index_rows],
            "duplicate_groups": duplicate_groups,
        }
    return result


def _validate_existing(state: Mapping[str, Mapping[str, Any]], *, require_external: bool) -> None:
    validate_required_schema_rows(
        [row for item in state.values() for row in item.get("columns", [])],
        require_external=require_external,
    )
    for table, item in state.items():
        index_rows = item.get("index") or []
        expected_column = MIGRATIONS[table]["column"]
        if index_rows and not (
            len(index_rows) == 1
            and int(index_rows[0].get("NON_UNIQUE") or 0) == 0
            and int(index_rows[0].get("SEQ_IN_INDEX") or 0) == 1
            and str(index_rows[0].get("COLUMN_NAME") or "") == expected_column
        ):
            raise RuntimeError("existing external-id index has an incompatible definition")
        if int(item.get("duplicate_groups") or 0):
            raise RuntimeError("external-id duplicates block the unique index")


def migrate(
    credential_file: str,
    *,
    apply: bool,
    cluster_id: str,
    backup_evidence_file: str = "",
    candidate_git_sha: str = "",
) -> Mapping[str, Any]:
    if cluster_id != CLUSTER_ID:
        raise RuntimeError("migration cluster confirmation is invalid")
    if apply:
        if not backup_evidence_file or not re.fullmatch(r"[0-9a-f]{40}", candidate_git_sha):
            raise RuntimeError("apply requires exact backup evidence and candidate git sha")
    elif backup_evidence_file or candidate_git_sha:
        raise RuntimeError("backup evidence and candidate git sha are valid only for apply")
    backup_evidence = (
        load_backup_evidence_file(backup_evidence_file, candidate_git_sha=candidate_git_sha)
        if apply else None
    )
    config = load_database_credential_file(credential_file, expected_user=MIGRATOR_USER)
    connection = _connect(config, expected_user=MIGRATOR_USER)
    applied = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATABASE() AS database_name,CURRENT_USER() AS account_name,@@read_only AS server_read_only"
            )
            identity = cursor.fetchone()
            if (
                not isinstance(identity, Mapping)
                or identity.get("database_name") != SCHEMA
                or str(identity.get("account_name") or "") != "%s@%s" % (MIGRATOR_USER, ACCOUNT_HOST)
                or int(identity.get("server_read_only") or 0) != 0
            ):
                raise RuntimeError("migration database identity is invalid")
            grant_fingerprint = validate_exact_account_grants(
                cursor,
                str(identity["account_name"]),
                expected_user=MIGRATOR_USER,
                expected_table_privileges=MIGRATOR_TABLE_PRIVILEGES,
            )
            before = _inspect(cursor)
            _validate_existing(before, require_external=False)
            plan = []
            for table, spec in MIGRATIONS.items():
                if before[table]["column"] is None:
                    plan.append({"table": table, "action": "add_column_and_unique_index"})
                elif not before[table]["index"]:
                    plan.append({"table": table, "action": "add_unique_index"})
            if apply:
                for item in plan:
                    table = item["table"]
                    spec = MIGRATIONS[table]
                    if item["action"] == "add_column_and_unique_index":
                        sql = (
                            "ALTER TABLE `%s`.`%s` ADD COLUMN `%s` %s, "
                            "ADD UNIQUE KEY `%s` (`%s`), ALGORITHM=INPLACE, LOCK=NONE"
                            % (SCHEMA, table, spec["column"], spec["column_sql"], spec["index"], spec["column"])
                        )
                    else:
                        sql = (
                            "ALTER TABLE `%s`.`%s` ADD UNIQUE KEY `%s` (`%s`), "
                            "ALGORITHM=INPLACE, LOCK=NONE"
                            % (SCHEMA, table, spec["index"], spec["column"])
                        )
                    cursor.execute(sql)
                    applied.append(dict(item))
            after = _inspect(cursor)
            complete = not any(
                after[table]["column"] is None or not after[table]["index"]
                for table in MIGRATIONS
            )
            _validate_existing(after, require_external=complete)
            if apply and not complete:
                raise RuntimeError("unified schema migration verification failed")
            return {
                "ok": True,
                "mode": "apply" if apply else "dry-run",
                "cluster_id": CLUSTER_ID,
                "schema": SCHEMA,
                "plan": plan,
                "applied": applied,
                "complete": complete,
                "grant_fingerprint": grant_fingerprint,
                "backup_evidence_sha256": str((backup_evidence or {}).get("evidence_sha256") or ""),
                "candidate_git_sha": candidate_git_sha,
                "migration_contract_sha256": MIGRATION_CONTRACT_SHA256,
            }
    finally:
        connection.close()


def verify_runtime_writer(credential_file: str) -> Mapping[str, Any]:
    config = load_database_credential_file(credential_file, expected_user=WRITER_USER)

    def connect():
        return _connect(config, expected_user=WRITER_USER)

    return UnifiedYouTubeLedger(connect).health()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--cluster-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-runtime-writer", action="store_true")
    parser.add_argument("--backup-evidence-file", default="")
    parser.add_argument("--candidate-git-sha", default="")
    args = parser.parse_args()
    if args.cluster_id != CLUSTER_ID:
        raise RuntimeError("migration cluster confirmation is invalid")
    if args.verify_runtime_writer:
        if args.backup_evidence_file or args.candidate_git_sha:
            raise RuntimeError("backup evidence and candidate git sha are valid only for apply")
        result = verify_runtime_writer(args.credential_file)
    else:
        result = migrate(
            args.credential_file,
            apply=bool(args.apply),
            cluster_id=args.cluster_id,
            backup_evidence_file=args.backup_evidence_file,
            candidate_git_sha=args.candidate_git_sha,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

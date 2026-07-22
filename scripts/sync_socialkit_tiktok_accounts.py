#!/usr/bin/env python3
"""Synchronize active SocialKit TikTok account snapshots into ads_ai.

The job deliberately keeps credentials and token values out of argv, logs, and
durable artifacts. Source rows are fetched over a read-only connection and the
target write is a single transaction against one fixed table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional


SOURCE_HOST = "hk-cynosdbmysql-grp-dejftnkp.sql.tencentcdb.com"
SOURCE_PORT = 28914
SOURCE_DATABASE = "socialkit"
TARGET_HOST = "101.32.56.53"
TARGET_PORT = 63353
TARGET_DATABASE = "ads_ai"
TARGET_TABLE = "tiktok_personal_account_snapshot"
QUALIFIED_TARGET_TABLE = "`ads_ai`.`tiktok_personal_account_snapshot`"
DEFAULT_MAX_SOURCE_ROWS = 1000
DEFAULT_LOCK_FILE = "/run/lock/socialkit_tiktok_account_sync.lock"


class SyncSafetyError(RuntimeError):
    """Raised when a fixed endpoint, schema, or row-count boundary is violated."""


class SyncAlreadyRunning(RuntimeError):
    """Raised when another sync process owns the host lock."""


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


SOURCE_QUERY = """
SELECT
  c.id AS source_account_id,
  c.team_id,
  c.main_account_id,
  c.external_account_id,
  c.account_name,
  c.link AS account_link,
  COALESCE(d.post_count, 0) AS post_count,
  COALESCE(d.fan_count, 0) AS fan_count,
  COALESCE(d.view_count, 0) AS view_count,
  COALESCE(d.like_count, 0) AS like_count,
  COALESCE(d.comment_count, 0) AS comment_count,
  COALESCE(d.collect_count, 0) AS collect_count,
  COALESCE(d.share_count, 0) AS share_count,
  c.access_token,
  c.token_status,
  c.account_status,
  c.token_expires_time,
  c.last_token_checked_time,
  c.disable_publish,
  CASE WHEN d.id IS NULL THEN 0 ELSE 1 END AS has_metric_snapshot,
  COALESCE(d.status, 0) AS metric_status,
  c.updated_time AS source_account_updated_time,
  COALESCE(d.last_updated_at, 0) AS source_metric_updated_at
FROM socialkit.social_center_accounts AS c
LEFT JOIN socialkit.social_account_data AS d
  ON d.team_id = c.team_id
 AND d.account_id = c.id
WHERE c.platform = 3
  AND c.is_deleted = 0
ORDER BY c.id
"""


SOURCE_TARGET_COLUMNS = (
    "source_account_id",
    "team_id",
    "main_account_id",
    "external_account_id",
    "account_name",
    "account_link",
    "post_count",
    "fan_count",
    "view_count",
    "like_count",
    "comment_count",
    "collect_count",
    "share_count",
    "access_token",
    "token_status",
    "account_status",
    "token_expires_time",
    "last_token_checked_time",
    "disable_publish",
    "has_metric_snapshot",
    "metric_status",
    "source_account_updated_time",
    "source_metric_updated_at",
)


def _build_upsert_sql() -> str:
    insert_columns = SOURCE_TARGET_COLUMNS + (
        "is_active",
        "last_seen_sync_id",
        "last_seen_at",
    )
    placeholders = ", ".join(["%s"] * len(SOURCE_TARGET_COLUMNS))
    assignments = ",\n  ".join(
        "%s = VALUES(%s)" % (column, column)
        for column in SOURCE_TARGET_COLUMNS
        if column != "source_account_id"
    )
    return """
INSERT INTO {table} (
  {columns}
) VALUES (
  {placeholders}, 1, %s, CURRENT_TIMESTAMP(6)
)
ON DUPLICATE KEY UPDATE
  {assignments},
  is_active = 1,
  last_seen_sync_id = VALUES(last_seen_sync_id),
  last_seen_at = VALUES(last_seen_at),
  updated_at = CURRENT_TIMESTAMP(6)
""".format(
        table=QUALIFIED_TARGET_TABLE,
        columns=",\n  ".join(insert_columns),
        placeholders=placeholders,
        assignments=assignments,
    )


UPSERT_SQL = _build_upsert_sql()
DEACTIVATE_SQL = """
UPDATE {table}
SET is_active = 0,
    access_token = NULL,
    updated_at = CURRENT_TIMESTAMP(6)
WHERE is_active = 1
  AND last_seen_sync_id <> %s
""".format(table=QUALIFIED_TARGET_TABLE)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise SyncSafetyError("required environment variable is missing: %s" % name)
    return value


def _port_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise SyncSafetyError("invalid port in %s" % name) from exc
    if port <= 0 or port > 65535:
        raise SyncSafetyError("port out of range in %s" % name)
    return port


def load_source_config() -> MysqlConfig:
    config = MysqlConfig(
        host=os.environ.get("SOCIALKIT_TT_SOURCE_MYSQL_HOST", SOURCE_HOST).strip(),
        port=_port_env("SOCIALKIT_TT_SOURCE_MYSQL_PORT", SOURCE_PORT),
        user=_required_env("SOCIALKIT_TT_SOURCE_MYSQL_USER").strip(),
        password=_required_env("SOCIALKIT_TT_SOURCE_MYSQL_PASSWORD"),
        database=os.environ.get(
            "SOCIALKIT_TT_SOURCE_MYSQL_DATABASE", SOURCE_DATABASE
        ).strip(),
    )
    validate_config(config, role="source")
    return config


def load_target_config() -> MysqlConfig:
    config = MysqlConfig(
        host=os.environ.get("SOCIALKIT_TT_TARGET_MYSQL_HOST", TARGET_HOST).strip(),
        port=_port_env("SOCIALKIT_TT_TARGET_MYSQL_PORT", TARGET_PORT),
        user=_required_env("SOCIALKIT_TT_TARGET_MYSQL_USER").strip(),
        password=_required_env("SOCIALKIT_TT_TARGET_MYSQL_PASSWORD"),
        database=os.environ.get(
            "SOCIALKIT_TT_TARGET_MYSQL_DATABASE", TARGET_DATABASE
        ).strip(),
    )
    validate_config(config, role="target")
    return config


def validate_config(config: MysqlConfig, role: str) -> None:
    if not config.user:
        raise SyncSafetyError("%s MySQL user is empty" % role)
    if not config.password:
        raise SyncSafetyError("%s MySQL password is empty" % role)
    if role == "source":
        expected = (SOURCE_HOST, SOURCE_PORT, SOURCE_DATABASE)
    elif role == "target":
        expected = (TARGET_HOST, TARGET_PORT, TARGET_DATABASE)
    else:
        raise SyncSafetyError("unknown MySQL role: %s" % role)
    actual = (config.host, config.port, config.database)
    if actual != expected:
        raise SyncSafetyError(
            "%s endpoint must be %s:%s/%s" % ((role,) + expected)
        )


def _connect(config: MysqlConfig, role: str):
    import pymysql

    validate_config(config, role=role)
    if role == "source":
        connect_timeout, read_timeout, write_timeout, autocommit = 8, 15, 5, True
    else:
        connect_timeout, read_timeout, write_timeout, autocommit = 3, 5, 5, False
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise SyncSafetyError("source field is not an integer: %s" % field) from exc


def normalize_source_rows(
    raw_rows: Iterable[Mapping[str, Any]], max_rows: int
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for raw in raw_rows:
        source_account_id = _as_int(raw.get("source_account_id"), "source_account_id")
        if source_account_id <= 0:
            raise SyncSafetyError("source_account_id must be positive")
        if source_account_id in seen_ids:
            raise SyncSafetyError("duplicate source_account_id in source result")
        seen_ids.add(source_account_id)
        row = {
            "source_account_id": source_account_id,
            "team_id": _as_int(raw.get("team_id"), "team_id"),
            "main_account_id": _as_text(raw.get("main_account_id")),
            "external_account_id": _as_text(raw.get("external_account_id")),
            "account_name": _as_text(raw.get("account_name")),
            "account_link": _as_text(raw.get("account_link")),
            "post_count": _as_int(raw.get("post_count"), "post_count"),
            "fan_count": _as_int(raw.get("fan_count"), "fan_count"),
            "view_count": _as_int(raw.get("view_count"), "view_count"),
            "like_count": _as_int(raw.get("like_count"), "like_count"),
            "comment_count": _as_int(raw.get("comment_count"), "comment_count"),
            "collect_count": _as_int(raw.get("collect_count"), "collect_count"),
            "share_count": _as_int(raw.get("share_count"), "share_count"),
            "access_token": _as_text(raw.get("access_token")) or None,
            "token_status": _as_int(raw.get("token_status"), "token_status"),
            "account_status": _as_int(raw.get("account_status"), "account_status"),
            "token_expires_time": _as_int(
                raw.get("token_expires_time"), "token_expires_time"
            ),
            "last_token_checked_time": _as_int(
                raw.get("last_token_checked_time"), "last_token_checked_time"
            ),
            "disable_publish": _as_int(
                raw.get("disable_publish"), "disable_publish"
            ),
            "has_metric_snapshot": _as_int(
                raw.get("has_metric_snapshot"), "has_metric_snapshot"
            ),
            "metric_status": _as_int(raw.get("metric_status"), "metric_status"),
            "source_account_updated_time": _as_int(
                raw.get("source_account_updated_time"),
                "source_account_updated_time",
            ),
            "source_metric_updated_at": _as_int(
                raw.get("source_metric_updated_at"), "source_metric_updated_at"
            ),
        }
        rows.append(row)
        if len(rows) > max_rows:
            raise SyncSafetyError("source row count exceeds configured safety cap")
    if not rows:
        raise SyncSafetyError("source returned zero active TikTok accounts; target unchanged")
    return rows


def fetch_source_rows(config: MysqlConfig, max_rows: int) -> List[Dict[str, Any]]:
    connection = _connect(config, role="source")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION max_execution_time=10000")
            cursor.execute(SOURCE_QUERY)
            raw_rows = cursor.fetchall()
    finally:
        connection.close()
    return normalize_source_rows(raw_rows, max_rows=max_rows)


def _upsert_values(row: Mapping[str, Any], run_id: str) -> tuple:
    return tuple(row[column] for column in SOURCE_TARGET_COLUMNS) + (run_id,)


def sync_target(
    config: MysqlConfig, rows: Iterable[Mapping[str, Any]], run_id: str
) -> Dict[str, int]:
    rows = list(rows)
    connection = _connect(config, role="target")
    upsert_operations = 0
    deactivated_rows = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db_name, @@read_only AS read_only")
            identity = cursor.fetchone() or {}
            if (
                identity.get("db_name") != TARGET_DATABASE
                or "read_only" not in identity
                or int(identity["read_only"]) != 0
            ):
                raise SyncSafetyError("target writer identity/read_only check failed")
            cursor.execute("SET SESSION innodb_lock_wait_timeout=5")
            for row in rows:
                cursor.execute(UPSERT_SQL, _upsert_values(row, run_id))
                upsert_operations += 1
            cursor.execute(DEACTIVATE_SQL, (run_id,))
            deactivated_rows = max(0, int(cursor.rowcount or 0))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "upsert_operations": upsert_operations,
        "deactivated_rows": deactivated_rows,
    }


def build_summary(
    rows: Iterable[Mapping[str, Any]],
    run_id: str,
    dry_run: bool,
    target_result: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    rows = list(rows)
    token_statuses = Counter(int(row.get("token_status") or 0) for row in rows)
    summary: Dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "run_id": run_id,
        "source_rows": len(rows),
        "metric_rows": sum(int(row.get("has_metric_snapshot") or 0) for row in rows),
        "missing_metric_rows": sum(
            1 for row in rows if not int(row.get("has_metric_snapshot") or 0)
        ),
        "access_tokens_present": sum(1 for row in rows if row.get("access_token")),
        "token_status_normal": token_statuses.get(2, 0),
        "token_status_expired": token_statuses.get(3, 0),
    }
    if target_result:
        summary.update(
            {
                "target_upsert_operations": int(
                    target_result.get("upsert_operations") or 0
                ),
                "target_deactivated_rows": int(
                    target_result.get("deactivated_rows") or 0
                ),
            }
        )
    return summary


@contextmanager
def process_lock(path: str):
    if os.name != "posix":
        yield
        return
    if not path.startswith("/run/lock/") or path == "/run/lock/":
        raise SyncSafetyError("lock file must stay under /run/lock")
    import fcntl

    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncAlreadyRunning("another sync process owns the lock") from exc
        yield
    finally:
        os.close(descriptor)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync active SocialKit TikTok account snapshots into ads_ai"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and validate the source without writing ads_ai",
    )
    return parser.parse_args(argv)


def _max_source_rows() -> int:
    raw = os.environ.get("SOCIALKIT_TT_SYNC_MAX_ROWS", str(DEFAULT_MAX_SOURCE_ROWS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SyncSafetyError("SOCIALKIT_TT_SYNC_MAX_ROWS must be an integer") from exc
    if value < 1 or value > DEFAULT_MAX_SOURCE_ROWS:
        raise SyncSafetyError(
            "SOCIALKIT_TT_SYNC_MAX_ROWS must be between 1 and %s"
            % DEFAULT_MAX_SOURCE_ROWS
        )
    return value


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    run_id = uuid.uuid4().hex
    lock_file = os.environ.get("SOCIALKIT_TT_SYNC_LOCK_FILE", DEFAULT_LOCK_FILE)
    try:
        with process_lock(lock_file):
            rows = fetch_source_rows(load_source_config(), max_rows=_max_source_rows())
            if args.dry_run:
                summary = build_summary(rows, run_id=run_id, dry_run=True)
            else:
                target_result = sync_target(load_target_config(), rows, run_id=run_id)
                summary = build_summary(
                    rows,
                    run_id=run_id,
                    dry_run=False,
                    target_result=target_result,
                )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except SyncAlreadyRunning as exc:
        print(
            json.dumps(
                {"status": "skipped", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)[:1000]},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

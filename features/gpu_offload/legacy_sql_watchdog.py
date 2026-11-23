#!/usr/bin/env python3
"""Kill over-time read queries for the guarded ads_aius read endpoint.

The FIFO gate only sees mysql CLI invocations.  This watchdog uses one
persistent controller connection owned by the same database account, so it
also sees and can interrupt direct driver connections such as PyMySQL.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path


VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = "/etc/sql-query-watchdog.env"
DEFAULT_STATE_PATH = "/var/lib/sql-query-watchdog/state.json"
EXPECTED_HOST = "101.32.56.53"
EXPECTED_PORT = 63350
EXPECTED_USER = "ads_aius"
EXPECTED_ACCOUNT = "ads_aius@43.166.187.96"
TRUTHY = frozenset(("1", "true", "yes", "on"))
CANDIDATE_SQL = """
SELECT
    ID AS thread_id,
    USER AS user_name,
    HOST AS client_host,
    DB AS db_name,
    COMMAND AS command_name,
    TIME AS elapsed_seconds,
    STATE AS state_name,
    INFO AS sql_text
FROM information_schema.PROCESSLIST
WHERE USER = %s
  AND COMMAND = 'Query'
  AND TIME >= %s
  AND ID <> CONNECTION_ID()
ORDER BY TIME DESC
LIMIT %s
"""
REVALIDATE_SQL = """
SELECT
    ID AS thread_id,
    USER AS user_name,
    HOST AS client_host,
    DB AS db_name,
    COMMAND AS command_name,
    TIME AS elapsed_seconds,
    STATE AS state_name,
    INFO AS sql_text
FROM information_schema.PROCESSLIST
WHERE ID = %s
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_bool(value):
    return str(value or "").strip().lower() in TRUTHY


def load_env_file(path):
    values = {}
    config_path = Path(path)
    if not config_path.is_file():
        return values
    try:
        contents = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # systemd loads the root-only EnvironmentFile before dropping to the
        # dedicated service user.  The process can then use os.environ without
        # needing direct read access to the secret file.
        return values
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def merged_settings(config_path):
    values = load_env_file(config_path)
    values.update(os.environ)
    return values


def safe_error(exc, password=""):
    text = "{}: {}".format(type(exc).__name__, str(exc))
    if password:
        text = text.replace(password, "<redacted>")
    return re.sub(r"\s+", " ", text).strip()[:240]


def strip_leading_sql_comments(sql_text):
    remaining = str(sql_text or "")
    while True:
        remaining = remaining.lstrip()
        if remaining.startswith("#"):
            newline = remaining.find("\n")
            return "" if newline < 0 else strip_leading_sql_comments(remaining[newline + 1 :])
        if remaining.startswith("--") and (
            len(remaining) == 2 or remaining[2].isspace()
        ):
            newline = remaining.find("\n")
            return "" if newline < 0 else strip_leading_sql_comments(remaining[newline + 1 :])
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            return "" if end < 0 else strip_leading_sql_comments(remaining[end + 2 :])
        return remaining


def sql_prefix(sql_text):
    match = re.match(r"([A-Za-z]+)", strip_leading_sql_comments(sql_text))
    return match.group(1).upper() if match else ""


def sql_digest(sql_text):
    normalized = re.sub(r"\s+", " ", str(sql_text or "")).strip()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def row_value(row, key):
    if isinstance(row, dict):
        return row.get(key)
    raise TypeError("watchdog queries require dictionary rows")


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    database: str
    expected_account: str
    max_query_seconds: int
    scan_interval_seconds: float
    reconnect_delay_seconds: float
    connect_timeout_seconds: int
    io_timeout_seconds: int
    controller_query_timeout_ms: int
    max_candidates: int
    allowed_prefixes: tuple
    dry_run: bool
    state_path: str
    test_mode: bool

    @classmethod
    def from_settings(cls, values):
        prefixes = tuple(
            item.strip().upper()
            for item in values.get("SQL_WATCHDOG_ALLOWED_PREFIXES", "SELECT").split(",")
            if item.strip()
        )
        config = cls(
            host=values.get("SQL_WATCHDOG_DB_HOST", EXPECTED_HOST),
            port=int(values.get("SQL_WATCHDOG_DB_PORT", str(EXPECTED_PORT))),
            user=values.get("SQL_WATCHDOG_DB_USER", EXPECTED_USER),
            password=values.get("SQL_WATCHDOG_DB_PASSWORD", ""),
            database=values.get("SQL_WATCHDOG_DB_NAME", "kunlunads_dev"),
            expected_account=values.get(
                "SQL_WATCHDOG_EXPECTED_ACCOUNT",
                EXPECTED_ACCOUNT,
            ),
            max_query_seconds=int(
                values.get("SQL_WATCHDOG_MAX_QUERY_SECONDS", "900")
            ),
            scan_interval_seconds=float(
                values.get("SQL_WATCHDOG_SCAN_INTERVAL_SECONDS", "10")
            ),
            reconnect_delay_seconds=float(
                values.get("SQL_WATCHDOG_RECONNECT_DELAY_SECONDS", "5")
            ),
            connect_timeout_seconds=int(
                values.get("SQL_WATCHDOG_CONNECT_TIMEOUT_SECONDS", "3")
            ),
            io_timeout_seconds=int(
                values.get("SQL_WATCHDOG_IO_TIMEOUT_SECONDS", "5")
            ),
            controller_query_timeout_ms=int(
                values.get("SQL_WATCHDOG_CONTROLLER_QUERY_TIMEOUT_MS", "5000")
            ),
            max_candidates=int(
                values.get("SQL_WATCHDOG_MAX_CANDIDATES", "50")
            ),
            allowed_prefixes=prefixes,
            dry_run=parse_bool(values.get("SQL_WATCHDOG_DRY_RUN", "0")),
            state_path=values.get("SQL_WATCHDOG_STATE_PATH", DEFAULT_STATE_PATH),
            test_mode=parse_bool(values.get("SQL_WATCHDOG_TEST_MODE", "0")),
        )
        config.validate()
        return config

    def validate(self):
        if not self.password:
            raise ValueError("SQL_WATCHDOG_DB_PASSWORD is required")
        if not self.test_mode and (
            self.host,
            self.port,
            self.user,
            self.expected_account,
        ) != (
            EXPECTED_HOST,
            EXPECTED_PORT,
            EXPECTED_USER,
            EXPECTED_ACCOUNT,
        ):
            raise ValueError("watchdog target must remain the guarded ads_aius read endpoint")
        minimum_query_seconds = 1 if self.test_mode else 60
        if not minimum_query_seconds <= self.max_query_seconds <= 86400:
            raise ValueError(
                "SQL_WATCHDOG_MAX_QUERY_SECONDS must be between {} and 86400".format(
                    minimum_query_seconds
                )
            )
        if not 1 <= self.scan_interval_seconds <= 300:
            raise ValueError("SQL_WATCHDOG_SCAN_INTERVAL_SECONDS must be between 1 and 300")
        if not 1 <= self.reconnect_delay_seconds <= 300:
            raise ValueError("SQL_WATCHDOG_RECONNECT_DELAY_SECONDS must be between 1 and 300")
        if not 1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("SQL_WATCHDOG_CONNECT_TIMEOUT_SECONDS must be between 1 and 30")
        if not 1 <= self.io_timeout_seconds <= 30:
            raise ValueError("SQL_WATCHDOG_IO_TIMEOUT_SECONDS must be between 1 and 30")
        if not 1000 <= self.controller_query_timeout_ms <= 60000:
            raise ValueError("SQL_WATCHDOG_CONTROLLER_QUERY_TIMEOUT_MS is out of range")
        if not 1 <= self.max_candidates <= 100:
            raise ValueError("SQL_WATCHDOG_MAX_CANDIDATES must be between 1 and 100")
        if not self.allowed_prefixes:
            raise ValueError("SQL_WATCHDOG_ALLOWED_PREFIXES must not be empty")
        if any(prefix != "SELECT" for prefix in self.allowed_prefixes):
            raise ValueError("only SELECT may be configured as a killable prefix")
        if not os.path.isabs(self.state_path):
            raise ValueError("SQL_WATCHDOG_STATE_PATH must be absolute")

    def public_dict(self):
        return {
            "version": VERSION,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "expected_account": self.expected_account,
            "max_query_seconds": self.max_query_seconds,
            "scan_interval_seconds": self.scan_interval_seconds,
            "allowed_prefixes": list(self.allowed_prefixes),
            "dry_run": self.dry_run,
            "state_path": self.state_path,
        }


def is_killable_row(row, config, controller_id, expected_digest=None):
    try:
        thread_id = int(row_value(row, "thread_id"))
        elapsed = int(row_value(row, "elapsed_seconds"))
    except (TypeError, ValueError):
        return False
    sql_text = row_value(row, "sql_text")
    if thread_id == int(controller_id):
        return False
    if str(row_value(row, "user_name") or "") != config.user:
        return False
    if str(row_value(row, "command_name") or "") != "Query":
        return False
    if elapsed < config.max_query_seconds:
        return False
    if sql_prefix(sql_text) not in config.allowed_prefixes:
        return False
    if expected_digest is not None and sql_digest(sql_text) != expected_digest:
        return False
    return True


class QueryWatchdog:
    def __init__(self, config, connector=None):
        self.config = config
        self.connector = connector or self._pymysql_connect
        self.connection = None
        self.controller_id = None
        self.stop_requested = False
        self.started_at = utc_now()
        self.last_heartbeat = 0.0
        self.counters = {
            "scans": 0,
            "candidates": 0,
            "killed": 0,
            "would_kill": 0,
            "skipped_non_read_only": 0,
            "races": 0,
            "errors": 0,
        }
        self.state = {
            "version": VERSION,
            "healthy": False,
            "connected": False,
            "started_at": self.started_at,
            "last_scan_at": None,
            "last_scan_epoch": None,
            "last_error": None,
            "controller_id": None,
            "config": config.public_dict(),
            "counters": dict(self.counters),
        }

    def log(self, event, **fields):
        payload = {
            "event": event,
            "timestamp": utc_now(),
            "version": VERSION,
        }
        payload.update(fields)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)

    def write_state(self):
        path = Path(self.config.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.state["counters"] = dict(self.counters)
        fd, temporary = tempfile.mkstemp(
            prefix=".sql-query-watchdog-",
            suffix=".json",
            dir=str(path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(self.state, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _pymysql_connect(self):
        import pymysql

        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=self.config.connect_timeout_seconds,
            read_timeout=self.config.io_timeout_seconds,
            write_timeout=self.config.io_timeout_seconds,
            cursorclass=pymysql.cursors.DictCursor,
            program_name="sql-query-watchdog",
        )

    def connect(self):
        connection = self.connector()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET SESSION MAX_EXECUTION_TIME={}".format(
                        int(self.config.controller_query_timeout_ms)
                    )
                )
                cursor.execute(
                    "SELECT @@read_only AS server_read_only, "
                    "CURRENT_USER() AS account_identity, "
                    "CONNECTION_ID() AS connection_id"
                )
                identity = cursor.fetchone()
            if int(row_value(identity, "server_read_only")) != 1:
                raise RuntimeError("watchdog refuses a writable database endpoint")
            if str(row_value(identity, "account_identity")) != self.config.expected_account:
                raise RuntimeError("watchdog database identity does not match expected account")
            self.connection = connection
            self.controller_id = int(row_value(identity, "connection_id"))
            self.state.update(
                {
                    "healthy": False,
                    "connected": True,
                    "last_error": None,
                    "controller_id": self.controller_id,
                }
            )
            self.write_state()
            self.log(
                "watchdog_connected",
                controller_id=self.controller_id,
                target="{}:{}/{}".format(
                    self.config.host,
                    self.config.port,
                    self.config.user,
                ),
                read_only=True,
            )
        except Exception:
            connection.close()
            raise

    def close(self):
        connection, self.connection = self.connection, None
        self.controller_id = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self.state.update(
            {
                "connected": False,
                "controller_id": None,
            }
        )

    def candidate_fields(self, row):
        return {
            "thread_id": int(row_value(row, "thread_id")),
            "elapsed_seconds": int(row_value(row, "elapsed_seconds")),
            "db_name": str(row_value(row, "db_name") or ""),
            "client_host": str(row_value(row, "client_host") or ""),
            "state_name": str(row_value(row, "state_name") or ""),
            "sql_prefix": sql_prefix(row_value(row, "sql_text")),
            "sql_digest": sql_digest(row_value(row, "sql_text")),
        }

    def scan_once(self):
        if self.connection is None:
            raise RuntimeError("watchdog is not connected")
        with self.connection.cursor() as cursor:
            cursor.execute(
                CANDIDATE_SQL,
                (
                    self.config.user,
                    self.config.max_query_seconds,
                    self.config.max_candidates,
                ),
            )
            rows = list(cursor.fetchall())
            for row in rows:
                self.counters["candidates"] += 1
                fields = self.candidate_fields(row)
                if not is_killable_row(row, self.config, self.controller_id):
                    self.counters["skipped_non_read_only"] += 1
                    self.log("query_skipped_non_read_only", **fields)
                    continue
                cursor.execute(REVALIDATE_SQL, (fields["thread_id"],))
                current = cursor.fetchone()
                if current is None or not is_killable_row(
                    current,
                    self.config,
                    self.controller_id,
                    expected_digest=fields["sql_digest"],
                ):
                    self.counters["races"] += 1
                    self.log(
                        "query_changed_before_kill",
                        thread_id=fields["thread_id"],
                        sql_digest=fields["sql_digest"],
                    )
                    continue
                current_fields = self.candidate_fields(current)
                if self.config.dry_run:
                    self.counters["would_kill"] += 1
                    self.log("query_would_kill", **current_fields)
                    continue
                try:
                    cursor.execute(
                        "KILL QUERY {}".format(int(current_fields["thread_id"]))
                    )
                except Exception as exc:
                    error_code = (
                        int(exc.args[0])
                        if getattr(exc, "args", None)
                        and str(exc.args[0]).isdigit()
                        else None
                    )
                    if error_code == 1094:
                        self.counters["races"] += 1
                        self.log(
                            "query_finished_before_kill",
                            thread_id=current_fields["thread_id"],
                            sql_digest=current_fields["sql_digest"],
                        )
                        continue
                    raise
                self.counters["killed"] += 1
                self.log("query_killed", **current_fields)
        self.counters["scans"] += 1
        now = time.time()
        self.state.update(
            {
                "healthy": True,
                "connected": True,
                "last_scan_at": utc_now(),
                "last_scan_epoch": now,
                "last_error": None,
                "controller_id": self.controller_id,
            }
        )
        self.write_state()
        if now - self.last_heartbeat >= 300:
            self.last_heartbeat = now
            self.log(
                "watchdog_heartbeat",
                controller_id=self.controller_id,
                counters=dict(self.counters),
            )

    def request_stop(self, _signum=None, _frame=None):
        self.stop_requested = True

    def serve(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self.request_stop)
        self.log("watchdog_started", config=self.config.public_dict())
        self.write_state()
        while not self.stop_requested:
            try:
                if self.connection is None:
                    self.connect()
                self.scan_once()
                deadline = time.monotonic() + self.config.scan_interval_seconds
                while not self.stop_requested and time.monotonic() < deadline:
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            except Exception as exc:
                self.counters["errors"] += 1
                message = safe_error(exc, self.config.password)
                self.close()
                self.state.update(
                    {
                        "healthy": False,
                        "last_error": message,
                    }
                )
                self.write_state()
                self.log("watchdog_error", error=message)
                deadline = time.monotonic() + self.config.reconnect_delay_seconds
                while not self.stop_requested and time.monotonic() < deadline:
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        self.close()
        self.state["healthy"] = False
        self.write_state()
        self.log("watchdog_stopped")
        return 0


def read_health(state_path, max_age_seconds):
    path = Path(state_path)
    if not path.is_file():
        return False, {"healthy": False, "error": "state file is missing"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, {"healthy": False, "error": safe_error(exc)}
    last_scan = state.get("last_scan_epoch")
    fresh = (
        isinstance(last_scan, (int, float))
        and time.time() - float(last_scan) <= float(max_age_seconds)
    )
    healthy = bool(state.get("healthy") and state.get("connected") and fresh)
    if not fresh:
        state["health_error"] = "last scan is stale"
    return healthy, state


def health_command(state_path, wait_seconds, max_age_seconds):
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        healthy, state = read_health(state_path, max_age_seconds)
        if healthy or time.monotonic() >= deadline:
            print(json.dumps(state, ensure_ascii=True, sort_keys=True))
            return 0 if healthy else 1
        time.sleep(0.5)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    once = subparsers.add_parser("scan-once")
    once.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("check-config")
    health = subparsers.add_parser("health")
    health.add_argument("--wait-seconds", type=float, default=0)
    health.add_argument("--max-age-seconds", type=float, default=60)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    values = merged_settings(args.config)
    state_path = values.get("SQL_WATCHDOG_STATE_PATH", DEFAULT_STATE_PATH)
    if args.command == "health":
        return health_command(
            state_path,
            args.wait_seconds,
            args.max_age_seconds,
        )
    try:
        config = Config.from_settings(values)
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"event": "config_error", "error": safe_error(exc)},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if args.command == "check-config":
        print(json.dumps(config.public_dict(), ensure_ascii=True, sort_keys=True))
        return 0
    if args.command == "scan-once":
        if args.dry_run:
            config = replace(config, dry_run=True)
        watchdog = QueryWatchdog(config)
        try:
            watchdog.connect()
            watchdog.scan_once()
            print(json.dumps(watchdog.state, ensure_ascii=True, sort_keys=True))
            return 0
        finally:
            watchdog.close()
    return QueryWatchdog(config).serve()


if __name__ == "__main__":
    sys.exit(main())

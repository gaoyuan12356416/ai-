import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from features.gpu_offload import legacy_sql_watchdog as watchdog
from features.gpu_offload.sql_watchdog import QueryWatchdog
watchdog.QueryWatchdog = QueryWatchdog


def process_row(
    thread_id=42,
    elapsed_seconds=901,
    sql_text="SELECT 1",
    user_name="ads_aius",
    command_name="Query",
):
    return {
        "thread_id": thread_id,
        "user_name": user_name,
        "client_host": "43.166.187.96:12345",
        "db_name": "kunlunads_dev",
        "command_name": command_name,
        "elapsed_seconds": elapsed_seconds,
        "state_name": "executing",
        "sql_text": sql_text,
    }


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.connection.executed.append((compact, params))
        self.rows = []
        self.row = None
        if compact.startswith("SET SESSION MAX_EXECUTION_TIME="):
            return 0
        if compact.startswith("SELECT @@read_only AS server_read_only"):
            self.row = dict(self.connection.identity)
            return 1
        if "FROM information_schema.PROCESSLIST" in compact and "WHERE USER = %s" in compact:
            self.rows = [dict(item) for item in self.connection.candidates]
            return len(self.rows)
        if "FROM information_schema.PROCESSLIST" in compact and "WHERE ID = %s" in compact:
            thread_id = int(params[0])
            current = self.connection.revalidated.get(thread_id)
            self.row = dict(current) if current is not None else None
            return int(self.row is not None)
        if compact.startswith("KILL QUERY "):
            self.connection.killed.append(int(compact.rsplit(" ", 1)[1]))
            return 0
        raise AssertionError("unexpected SQL: {}".format(compact))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, candidates=None, revalidated=None, identity=None):
        self.candidates = list(candidates or [])
        self.revalidated = dict(revalidated or {})
        self.identity = identity or {
            "server_read_only": 1,
            "account_identity": "ads_aius@43.166.187.96",
            "connection_id": 7,
        }
        self.executed = []
        self.killed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class QueryWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.temp.name, "state.json")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides):
        values = {
            "SQL_WATCHDOG_DB_PASSWORD": "unit-test-secret",
            "SQL_WATCHDOG_STATE_PATH": self.state_path,
            "SQL_WATCHDOG_TEST_MODE": "1",
            "SQL_WATCHDOG_MAX_QUERY_SECONDS": "900",
            "SQL_WATCHDOG_SCAN_INTERVAL_SECONDS": "10",
        }
        values.update(overrides)
        return watchdog.Config.from_settings(values)

    def build_watchdog(self, connection, **overrides):
        instance = watchdog.QueryWatchdog(
            self.config(**overrides),
            connector=lambda: connection,
        )
        with redirect_stdout(io.StringIO()):
            instance.connect()
        return instance

    def test_sql_prefix_skips_leading_comments(self):
        self.assertEqual(
            watchdog.sql_prefix(" /* trace */\n-- comment\n# note\n SELECT 1"),
            "SELECT",
        )
        self.assertEqual(watchdog.sql_prefix("UPDATE table_name SET value=1"), "UPDATE")
        self.assertEqual(watchdog.sql_prefix("/* unterminated"), "")

    def test_production_config_refuses_other_target_or_prefix(self):
        with self.assertRaisesRegex(ValueError, "guarded ads_aius read endpoint"):
            watchdog.Config.from_settings(
                {
                    "SQL_WATCHDOG_DB_PASSWORD": "secret",
                    "SQL_WATCHDOG_DB_PORT": "63353",
                }
            )
        with self.assertRaisesRegex(ValueError, "only SELECT"):
            watchdog.Config.from_settings(
                {
                    "SQL_WATCHDOG_DB_PASSWORD": "secret",
                    "SQL_WATCHDOG_ALLOWED_PREFIXES": "SELECT,UPDATE",
                }
            )
        with self.assertRaisesRegex(ValueError, "between 60 and 86400"):
            watchdog.Config.from_settings(
                {
                    "SQL_WATCHDOG_DB_PASSWORD": "secret",
                    "SQL_WATCHDOG_MAX_QUERY_SECONDS": "1",
                }
            )

    def test_test_mode_allows_short_canary_threshold(self):
        config = self.config(SQL_WATCHDOG_MAX_QUERY_SECONDS="1")
        self.assertEqual(config.max_query_seconds, 1)

    def test_connect_refuses_writable_endpoint_and_closes_connection(self):
        connection = FakeConnection(
            identity={
                "server_read_only": 0,
                "account_identity": "ads_aius@43.166.187.96",
                "connection_id": 7,
            }
        )
        instance = watchdog.QueryWatchdog(
            self.config(),
            connector=lambda: connection,
        )
        with self.assertRaisesRegex(RuntimeError, "writable"):
            instance.connect()
        self.assertTrue(connection.closed)

    def test_kills_revalidated_long_select_and_records_no_sql_text(self):
        candidate = process_row(sql_text="SELECT sensitive_column FROM table_name")
        connection = FakeConnection(
            candidates=[candidate],
            revalidated={42: candidate},
        )
        instance = self.build_watchdog(connection)
        output = io.StringIO()
        with redirect_stdout(output):
            instance.scan_once()
        self.assertEqual(connection.killed, [42])
        self.assertEqual(instance.counters["killed"], 1)
        events = [
            json.loads(line)
            for line in output.getvalue().splitlines()
            if line.strip()
        ]
        event = next(item for item in events if item["event"] == "query_killed")
        self.assertEqual(event["event"], "query_killed")
        self.assertNotIn("sql_text", event)
        self.assertNotIn("sensitive_column", output.getvalue())
        with open(self.state_path, encoding="utf-8") as handle:
            state_text = handle.read()
        self.assertNotIn("sensitive_column", state_text)
        self.assertNotIn("unit-test-secret", state_text)

    def test_skips_non_read_only_statement_even_when_old(self):
        candidate = process_row(sql_text="UPDATE table_name SET value=1")
        connection = FakeConnection(candidates=[candidate])
        instance = self.build_watchdog(connection)
        with redirect_stdout(io.StringIO()):
            instance.scan_once()
        self.assertEqual(connection.killed, [])
        self.assertEqual(instance.counters["skipped_non_read_only"], 1)

    def test_revalidation_prevents_thread_id_reuse_race(self):
        candidate = process_row()
        replacement = process_row(elapsed_seconds=1)
        connection = FakeConnection(
            candidates=[candidate],
            revalidated={42: replacement},
        )
        instance = self.build_watchdog(connection)
        with redirect_stdout(io.StringIO()):
            instance.scan_once()
        self.assertEqual(connection.killed, [])
        self.assertEqual(instance.counters["races"], 1)

    def test_dry_run_never_executes_kill(self):
        candidate = process_row()
        connection = FakeConnection(
            candidates=[candidate],
            revalidated={42: candidate},
        )
        instance = self.build_watchdog(
            connection,
            SQL_WATCHDOG_DRY_RUN="1",
        )
        with redirect_stdout(io.StringIO()):
            instance.scan_once()
        self.assertEqual(connection.killed, [])
        self.assertEqual(instance.counters["would_kill"], 1)

    def test_health_requires_fresh_successful_scan(self):
        candidate = process_row()
        connection = FakeConnection(
            candidates=[candidate],
            revalidated={42: candidate},
        )
        instance = self.build_watchdog(
            connection,
            SQL_WATCHDOG_DRY_RUN="1",
        )
        with redirect_stdout(io.StringIO()):
            instance.scan_once()
        healthy, state = watchdog.read_health(self.state_path, 60)
        self.assertTrue(healthy)
        state["last_scan_epoch"] = time.time() - 120
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        healthy, state = watchdog.read_health(self.state_path, 60)
        self.assertFalse(healthy)
        self.assertEqual(state["health_error"], "last scan is stale")

    def test_safe_error_redacts_password(self):
        error = RuntimeError("failed with unit-test-secret")
        self.assertNotIn(
            "unit-test-secret",
            watchdog.safe_error(error, "unit-test-secret"),
        )


if __name__ == "__main__":
    unittest.main()

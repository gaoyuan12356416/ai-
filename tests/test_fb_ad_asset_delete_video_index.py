"""Video reference completeness tests using temporary SQLite and mocked processes."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.video_index import MysqlVideoStream, VideoIndex


MODULE = "features.fb_ad_asset_delete.video_index"


class VideoIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wall_time = 1000.0
        self.storage = Mock()
        self.disk = patch(MODULE + ".shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024**3))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def index(self, rows=(), max_age=60):
        stream = Mock(side_effect=lambda: iter(deepcopy(rows)))
        index = VideoIndex(self.root, stream, max_age=max_age,
                           clock=lambda: self.wall_time, validate_storage=self.storage)
        return index, stream

    def assert_no_stage_files(self):
        self.assertEqual(list(self.root.glob("building-*.sqlite3*")), [])

    def test_complete_snapshot_matches_exact_ids_across_all_products_and_accounts(self):
        rows = [
            [1, "101", "3442", "act_400", '["123","456","123"]'],
            [2, "102", "3360", "500", "123, 789;123"],
            [3, "103", "9999", "600", "1234"],
            [4, "104", "8888", "700", None],
            [5, "105", "7777", "800", "NULL"],
        ]
        index, stream = self.index(rows)
        result = index.references(["123"])
        self.assertEqual(list(result), [
            dict(key="video:123", ad_id="101", product_id="3442", account_id="act_400"),
            dict(key="video:123", ad_id="102", product_id="3360", account_id="500"),
        ])
        self.assertTrue(result.proof["complete"])
        self.assertEqual(result.proof["source_rows"], 5)
        self.assertEqual(result.proof["relations"], 5)
        self.assertEqual(result.proof["malformed_rows"], 0)
        self.assertEqual(index.references(["456"])[0]["ad_id"], "101")
        self.assertEqual(index.references(["12"]), [])
        self.assertEqual(index.references(["1234"])[0]["product_id"], "9999")
        self.assertEqual(stream.call_count, 1)
        self.assertEqual(stream.call_args.args, ())
        self.assert_no_stage_files()

    def test_empty_complete_source_has_a_valid_empty_generation(self):
        index, stream = self.index([])
        result = index.references(["123"])
        self.assertEqual(result, [])
        self.assertTrue(result.proof["complete"])
        self.assertEqual(result.proof["source_rows"], 0)
        self.assertTrue(index.path.exists())
        self.assertEqual(stream.call_count, 1)

    def test_target_ids_are_safely_parameterized_and_multiple_query_batches_are_complete(self):
        rows = [[n, str(1000 + n), "301", "444", str(2000 + n)] for n in range(1, 252)]
        index, _ = self.index(rows)
        result = index.references([str(2000 + n) for n in range(1, 252)])
        self.assertEqual(len(result), 251)
        self.assertEqual(len({row["ad_id"] for row in result}), 251)
        self.assertEqual(index.references(["2001' OR 1=1 --"]), [])
        self.assertEqual(index.references(["2001"])[0]["ad_id"], "1001")

    def test_partial_stream_or_duplicate_source_row_never_publishes(self):
        index, _ = self.index()
        def interrupted():
            yield [1, "101", "301", "444", "123"]
            raise RuntimeError("private-source-credentials")
        for factory in (interrupted, lambda: iter([[1, "101", "301", "444", "123"],
                                                 [1, "102", "302", "555", "456"]])):
            with self.subTest(factory=factory.__name__):
                index.stream = factory
                with self.assertRaises(AssetError) as error:
                    index.build()
                self.assertEqual(error.exception.code, "video_index_incomplete")
                self.assertNotIn("private-source-credentials", error.exception.message)
                self.assertFalse(index.path.exists())
                self.assert_no_stage_files()

    def test_fresh_build_failure_does_not_fall_back_to_a_previous_valid_generation(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]])
        previous = index.references(["123"]).proof
        def interrupted():
            yield [2, "102", "302", "555", "123"]
            raise AssetError("video_index_incomplete", "EOF missing", 503)
        index.stream = interrupted
        with self.assertRaises(AssetError) as error:
            index.references(["123"], fresh=True)
        self.assertEqual(error.exception.code, "video_index_incomplete")
        self.assertEqual(index._proof()["generation_id"], previous["generation_id"])
        self.assert_no_stage_files()

    def test_a_new_complete_generation_atomically_replaces_the_previous_one(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]])
        previous = index.references(["123"]).proof
        index.stream = lambda: iter([[2, "102", "302", "555", "456"]])
        result = index.references(["123", "456"], fresh=True)
        self.assertNotEqual(result.proof["generation_id"], previous["generation_id"])
        self.assertEqual([row["key"] for row in result], ["video:456"])
        self.assert_no_stage_files()

    def test_build_ttl_counts_time_spent_reading_the_source(self):
        index, _ = self.index(max_age=60)
        def slow_stream():
            yield [1, "101", "301", "444", "123"]
            self.wall_time += 60
        index.stream = slow_stream
        with self.assertRaises(AssetError) as error:
            index.build()
        self.assertEqual(error.exception.code, "video_index_expired")
        self.assertFalse(index.path.exists())
        self.assert_no_stage_files()

    def test_proof_expires_from_snapshot_start_and_clock_reversal_is_rejected(self):
        index, _ = self.index(max_age=60)
        def nearly_expired():
            yield [1, "101", "301", "444", "123"]
            self.wall_time += 59
        index.stream = nearly_expired
        proof = index.build()
        self.assertEqual(proof["snapshot_started_at"], 1000)
        self.assertEqual(proof["completed_at"], 1059)
        index.validate(proof)
        for current in (1060, 999):
            with self.subTest(current=current):
                self.wall_time = current
                with self.assertRaises(AssetError) as error:
                    index.validate(proof)
                self.assertEqual(error.exception.code, "video_index_expired")

    def test_expired_current_snapshot_requires_a_new_complete_build(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]], max_age=60)
        previous = index.references(["123"]).proof
        self.wall_time += 60
        index.stream = Mock(side_effect=AssetError("video_index_incomplete", "source unavailable", 503))
        with self.assertRaises(AssetError) as error:
            index.references(["123"])
        self.assertEqual(error.exception.code, "video_index_incomplete")
        self.assertEqual(index.stream.call_count, 1)
        self.assertEqual(index._proof()["generation_id"], previous["generation_id"])

    def test_any_malformed_global_video_relation_blocks_even_unrelated_targets(self):
        for raw in ('["123",broken]', '["456","bad"]', "456,bad", [True], {"video_id": "456"}):
            with self.subTest(raw=raw):
                index, _ = self.index([[1, "101", "301", "444", "123"],
                                        [2, "outside", "other-product", "other-account", raw]])
                with self.assertRaises(AssetError) as error:
                    index.references(["123"], fresh=True)
                self.assertEqual(error.exception.code, "video_index_malformed")
                self.assertEqual(index._proof()["malformed_rows"], 1)

    def test_build_singleflight_lock_excludes_other_instances_and_releases_after_completion(self):
        first, _ = self.index()
        second, stream = self.index([[2, "102", "302", "555", "456"]])
        attempts = []
        def overlapping_build():
            with self.assertRaises(AssetError) as error:
                second.build()
            attempts.append(error.exception.code)
            yield [1, "101", "301", "444", "123"]
        first.stream = overlapping_build
        first.build()
        self.assertEqual(attempts, ["video_index_building"])
        self.assertEqual(stream.call_count, 0)
        self.assertTrue(second.build()["complete"])
        self.assertEqual(stream.call_count, 1)
        self.assert_no_stage_files()

    def test_storage_validation_and_disk_full_stop_before_source_read(self):
        index, stream = self.index([[1, "101", "301", "444", "123"]])
        self.storage.side_effect = AssetError("data_disk_unavailable", "data disk missing", 503)
        with self.assertRaises(AssetError) as error:
            index.references(["123"])
        self.assertEqual(error.exception.code, "data_disk_unavailable")
        self.assertEqual(stream.call_count, 0)
        self.storage.side_effect = None
        with patch(MODULE + ".shutil.disk_usage", return_value=SimpleNamespace(free=1024)):
            with self.assertRaises(AssetError) as error:
                index.references(["123"])
        self.assertEqual(error.exception.code, "video_index_disk_full")
        self.assertEqual(stream.call_count, 0)
        self.assertFalse(index.path.exists())

    def test_mid_build_storage_failure_discards_partial_rows_and_preserves_previous_generation(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]])
        previous = index.build()
        index.stream = lambda: ([n, str(1000 + n), "302", "555", "456"] for n in range(1, 5002))
        self.storage.side_effect = [None, AssetError("data_disk_unavailable", "data disk missing", 503)]
        with self.assertRaises(AssetError) as error:
            index.build()
        self.assertEqual(error.exception.code, "data_disk_unavailable")
        self.assertEqual(index._proof()["generation_id"], previous["generation_id"])
        self.assert_no_stage_files()

    def test_sqlite_write_failure_cannot_publish_or_return_partial_references(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]])
        previous = index.build()
        real_connect = sqlite3.connect
        class FailingWrites:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def executemany(self, *args, **kwargs):
                raise sqlite3.OperationalError("database or disk is full")
        def connect(path, *args, **kwargs):
            connection = real_connect(path, *args, **kwargs)
            return FailingWrites(connection) if Path(str(path)).name.startswith("building-") else connection
        with patch(MODULE + ".sqlite3.connect", side_effect=connect):
            with self.assertRaises(AssetError) as error:
                index.references(["123"], fresh=True)
        self.assertEqual(error.exception.code, "video_index_incomplete")
        self.assertEqual(index._proof()["generation_id"], previous["generation_id"])
        self.assert_no_stage_files()

    def test_failed_fsync_or_publication_preserves_previous_generation_without_fallback(self):
        index, _ = self.index([[1, "101", "301", "444", "123"]])
        previous = index.build()
        for operation in ("os.fsync", "os.replace"):
            with self.subTest(operation=operation), patch(MODULE + "." + operation, side_effect=OSError("write failed")):
                with self.assertRaises(AssetError) as error:
                    index.references(["123"], fresh=True)
                self.assertEqual(error.exception.code, "video_index_incomplete")
            self.assertEqual(index._proof()["generation_id"], previous["generation_id"])
            self.assert_no_stage_files()


class CapturedInput(io.StringIO):
    def close(self):
        self.written = self.getvalue()
        super().close()


class FakeMysqlProcess:
    def __init__(self, rows=(), exit_code=0, text=None):
        self.stdin = CapturedInput()
        self.stdout = io.StringIO(text if text is not None else "".join(json.dumps(row) + "\n" for row in rows))
        self.returncode = None
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class MysqlVideoStreamTests(unittest.TestCase):
    def stream(self):
        return MysqlVideoStream(["/usr/bin/mysql", "-h", "readonly-db", "-P", "63350", "-N", "-B", "-e"],
                                "unit-test-password", "kunlunads_dev", timeout=60)

    def consume(self, process, factory=None):
        factory = factory or self.stream()
        with patch(MODULE + ".subprocess.Popen", return_value=process) as popen, patch(MODULE + ".threading.Timer") as timer:
            result = list(factory())
        return result, popen, timer

    def test_complete_stream_uses_gated_mysql_stdin_readonly_proof_and_clean_exit(self):
        row = [1, "101", "301", "444", '["123","456"]']
        process = FakeMysqlProcess([["readonly", 1], row, ["complete"]])
        with patch.dict("os.environ", {"SQL_GATE_BYPASS": "1"}):
            result, popen, timer = self.consume(process)
        self.assertEqual(result, [row])
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/mysql")
        self.assertNotIn("-e", command)
        for flag in ("--quick", "--raw", "--unbuffered", "--skip-reconnect"):
            self.assertIn(flag, command)
        self.assertNotIn("unit-test-password", " ".join(command))
        self.assertNotIn("unit-test-password", process.stdin.written)
        self.assertEqual(popen.call_args.kwargs["env"]["SQL_GATE_BYPASS"], "0")
        self.assertEqual(popen.call_args.kwargs["env"]["MYSQL_PWD"], "unit-test-password")
        self.assertIn("@@read_only", process.stdin.written)
        self.assertIn("JSON_ARRAY('complete')", process.stdin.written)
        self.assertIn("FORCE INDEX(PRIMARY)", process.stdin.written)
        self.assertEqual(process.stdin.written.count("FROM `kunlunads_dev`.ads_facebook_auto_created_data"), 1)
        self.assertNotRegex(process.stdin.written.upper(), r"\b(INSERT|UPDATE|ALTER|CREATE|DROP|DELETE FROM)\b")
        self.assertTrue(process.stdout.closed)
        self.assertFalse(process.terminated)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    def test_bypass_client_and_invalid_schema_are_rejected_before_process_launch(self):
        with patch(MODULE + ".subprocess.Popen") as popen:
            for command in (["/usr/bin/mysql.real"], ["mysql-cli"], ["ssh", "server", "mysql.real"]):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    MysqlVideoStream(command, "secret", "safe_schema")
            for schema in ("db;DROP", "db`", "db-name", "db.name"):
                with self.subTest(schema=schema), self.assertRaises(ValueError):
                    MysqlVideoStream(["/usr/bin/mysql"], "secret", schema)
            popen.assert_not_called()

    def test_missing_eof_readonly_or_completion_markers_and_invalid_rows_fail_closed(self):
        row = [1, "101", "301", "444", "123"]
        cases = ([], [["complete"]], [["readonly", 0], ["complete"]], [["readonly", 1]],
                 [["readonly", 1], row], [["readonly", 1], ["complete"], ["complete"]],
                 [["readonly", 1], ["complete"], row], [["readonly", 1], row[:4], ["complete"]])
        for rows in cases:
            with self.subTest(rows=rows):
                process = FakeMysqlProcess(rows)
                with self.assertRaises(AssetError) as error:
                    self.consume(process)
                self.assertEqual(error.exception.code, "video_index_incomplete")
                self.assertTrue(process.stdout.closed)

    def test_completion_marker_with_nonzero_exit_does_not_authorize_a_snapshot(self):
        process = FakeMysqlProcess([["readonly", 1], [1, "101", "301", "444", "123"], ["complete"]], exit_code=124)
        with self.assertRaises(AssetError) as error:
            self.consume(process)
        self.assertEqual(error.exception.code, "video_index_incomplete")
        self.assertTrue(process.stdout.closed)

    def test_invalid_json_and_oversized_rows_are_rejected_and_process_is_stopped(self):
        for text in ('["readonly",1]\nnot-json\n', '["readonly",1]\n' + "x" * 16385 + "\n"):
            with self.subTest(size=len(text)):
                process = FakeMysqlProcess(text=text)
                with self.assertRaises(AssetError):
                    self.consume(process)
                self.assertTrue(process.terminated)
                self.assertTrue(process.stdout.closed)

    def test_timeout_flag_rejects_even_a_complete_stream_with_zero_exit(self):
        process = FakeMysqlProcess([["readonly", 1], ["complete"]])
        process.returncode = 0
        def timer_factory(delay, callback):
            return SimpleNamespace(daemon=False, start=callback, cancel=lambda: None)
        with patch(MODULE + ".subprocess.Popen", return_value=process), patch(MODULE + ".threading.Timer", side_effect=timer_factory):
            with self.assertRaises(AssetError) as error:
                list(self.stream()())
        self.assertEqual(error.exception.code, "video_index_incomplete")

    def test_mysql_partial_eof_never_publishes_video_index_generation(self):
        process = FakeMysqlProcess([["readonly", 1], [1, "101", "301", "444", "123"]])
        with tempfile.TemporaryDirectory() as root, patch(MODULE + ".subprocess.Popen", return_value=process), \
                patch(MODULE + ".threading.Timer"), patch(MODULE + ".shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)):
            index = VideoIndex(root, self.stream(), clock=lambda: 1000)
            with self.assertRaises(AssetError) as error:
                index.references(["123"])
            self.assertEqual(error.exception.code, "video_index_incomplete")
            self.assertFalse(index.path.exists())
            self.assertEqual(list(Path(root).glob("building-*.sqlite3*")), [])


if __name__ == "__main__":
    unittest.main()

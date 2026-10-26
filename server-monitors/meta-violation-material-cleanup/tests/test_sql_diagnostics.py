import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import test_sql_source as fixtures
from meta_violation_cleanup.sql_diagnostics import LIMIT, StderrCapture

FakeMysql, source = fixtures.FakeMysql, fixtures.source


class DiagnosticTests(unittest.TestCase):
    setUp = fixtures.SqlSourceTests.setUp
    make_source = fixtures.SqlSourceTests.make_source

    @staticmethod
    def broken_open(errno=2013):
        process = FakeMysql(lambda sql: [])
        def flush():
            process.commands.append(process.stdin.pending)
            process.stdin.pending = ''
            process.stderr.items.put('ERROR %s (HY000): MySQL initialization failed\n' % errno)
            process.returncode = 1
            process.stdout.items.put('')
        process.stdin.flush = flush
        return process

    def test_transient_open_retries_fresh_proof_before_any_business_read(self):
        adapter = self.make_source()
        failed, healthy = self.broken_open(), FakeMysql(lambda sql: [])
        with patch.object(source.subprocess, 'Popen', side_effect=[failed, healthy]) as spawn, \
                patch.object(source.time, 'sleep') as sleep:
            self.assertEqual(adapter.products(), [])
        self.assertEqual(spawn.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(adapter.safe_metrics['connection_retries'], 1)
        self.assertEqual(len(failed.commands), 1)
        self.assertIn("'read_only'", healthy.commands[0])
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', healthy.commands[1])
        self.assertIn('ads_apps_setting', healthy.commands[2])

    def test_open_retries_are_bounded_and_log_each_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'errors.jsonl'
            adapter = self.make_source(sql_diagnostic_path=str(path))
            with patch.object(source.subprocess, 'Popen', side_effect=[self.broken_open() for _ in range(3)]) as spawn, \
                    patch.object(source.time, 'sleep') as sleep:
                with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
                    adapter.products()
            self.assertEqual(spawn.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 3])
            self.assertEqual(len(path.read_text().splitlines()), 3)
            self.assertIsNone(adapter._active)

    def test_permanent_open_failure_and_missing_readonly_proof_are_not_retried(self):
        for process in (self.broken_open(1045), FakeMysql(lambda sql: [], readonly=0)):
            adapter = self.make_source()
            with patch.object(source.subprocess, 'Popen', return_value=process) as spawn:
                with self.assertRaises(source.SqlSourceError):
                    adapter.products()
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(adapter.safe_metrics['connection_retries'], 0)

    def test_business_query_disconnect_does_not_restart_existing_snapshot(self):
        adapter = self.make_source()
        process = FakeMysql(lambda sql: None)
        process.stderr.items.put('ERROR 2013 (HY000): Lost connection during query\n')
        with patch.object(source.subprocess, 'Popen', return_value=process) as spawn:
            with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
                adapter.products()
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(adapter.safe_metrics['connection_retries'], 0)

    def test_real_child_stderr_is_drained_before_eof_failure(self):
        adapter = self.make_source()
        adapter._command = [sys.executable, '-u', '-c', '''
import json,re,sys
for line in sys.stdin:
    if line.startswith("SELECT JSON_OBJECT('read_only'"):
        print('{"read_only":1}',flush=True)
    elif line.startswith('SET SESSION'):
        pass
    elif line.startswith('SELECT JSON_ARRAY'):
        marker=bytes.fromhex(re.search(r'0x([0-9a-f]+)',line).group(1)).decode()
        print(json.dumps([marker]),flush=True)
    else:
        for _ in range(20000): print('mysql diagnostic line',file=sys.stderr)
        print('ERROR 3024 (HY000): Query execution was interrupted',file=sys.stderr,flush=True)
        sys.exit(1)
''']
        with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
            adapter.products()
        self.assertEqual(adapter.last_diagnostic['mysql_errors'][0]['errno'], 3024)
        self.assertFalse(adapter.last_diagnostic['stderr_incomplete'])
        self.assertTrue(adapter.last_diagnostic['stderr_truncated'])
    def test_mysql_native_error_is_durable_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'run' / 'mysql-errors.jsonl'
            adapter = self.make_source(sql_diagnostic_path=str(path))
            process = FakeMysql(lambda sql: None)
            process.stderr.items.put('ERROR 3024 (HY000) at line 7: Query execution was interrupted, maximum statement execution time exceeded\n')
            process.stderr.items.put('password=synthetic-secret access_token=token-value "private-value" _utf8mb4 0x736563726574\n')
            with patch.object(source.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
                    adapter.credentials()
            text = path.read_text()
            record = json.loads(text)
            self.assertEqual(record['mysql_errors'], [{'errno': 3024, 'sqlstate': 'HY000'}])
            self.assertEqual(record['returncode'], 1)
            self.assertIn('maximum statement execution time exceeded', record['stderr'])
            self.assertEqual(record['partial_rows_discarded'], 0)
            self.assertFalse(record['stderr_incomplete'])
            self.assertEqual(record['id'], adapter.last_diagnostic['id'])
            self.assertTrue(record['callsite'])
            self.assertEqual(len(record['query_sha256']), 64)
            for secret in ('synthetic-secret', 'token-value', 'private-value', '736563726574'):
                self.assertNotIn(secret, text)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_partial_rows_stay_discarded_with_diagnostics(self):
        adapter = self.make_source()
        process = FakeMysql(lambda sql: None)
        def respond(sql):
            process.stdout.items.put('{"token":"must-not-log-stdout"}\n')
            return None
        process.responder = respond
        with patch.object(source.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
                adapter.credentials()
        self.assertEqual(adapter.last_diagnostic['partial_rows_discarded'], 1)
        self.assertNotIn('must-not-log-stdout', json.dumps(adapter.last_diagnostic))

    def test_failed_log_write_does_not_replace_database_error(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.make_source(sql_diagnostic_path=root)
            process = FakeMysql(lambda sql: None)
            with patch.object(source.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(source.SqlSourceError, 'sql_stream_incomplete'):
                    adapter.products()
            self.assertTrue(adapter.last_diagnostic['diagnostic_write_failed'])


class CaptureTests(unittest.TestCase):
    def test_stderr_flood_is_drained_without_unbounded_memory(self):
        stream = io.StringIO('message\n' * 20000 + 'ERROR 2013 (HY000): Lost connection to MySQL server during query\n')
        text, truncated, failed = StderrCapture(stream).finish()
        self.assertLessEqual(len(text), LIMIT)
        self.assertTrue(truncated)
        self.assertFalse(failed)
        self.assertIn('ERROR 2013', text)

    def test_oversized_secret_line_is_omitted_entirely(self):
        stream = io.StringIO('access_token=' + 'private' * 3000 + '\nERROR 2006 (HY000): MySQL server has gone away\n')
        text, truncated, failed = StderrCapture(stream).finish()
        self.assertTrue(truncated)
        self.assertFalse(failed)
        self.assertNotIn('private', text)
        self.assertIn('ERROR 2006', text)


if __name__ == '__main__':
    unittest.main()

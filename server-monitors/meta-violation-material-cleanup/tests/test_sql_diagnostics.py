import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_sql_source as fixtures
from meta_violation_cleanup.sql_diagnostics import LIMIT, StderrCapture

FakeMysql, source = fixtures.FakeMysql, fixtures.source


class DiagnosticTests(unittest.TestCase):
    setUp = fixtures.SqlSourceTests.setUp
    make_source = fixtures.SqlSourceTests.make_source
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

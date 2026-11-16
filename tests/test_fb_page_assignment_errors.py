import json
import unittest
from unittest.mock import patch

from features.fb_page_assignment.runtime import database_error
from features.fb_page_assignment.storage import ENV_PREFIX, Writer


class DatabaseDiagnosticsTests(unittest.TestCase):
    def test_initial_packet_timeout_keeps_only_fixed_labels(self):
        detail = database_error("ERROR 2013: Lost connection at 'reading initial communication packet', "
                                "timed out; password=SHOULD_NOT_LEAK")
        self.assertEqual(detail, dict(mysql_error_code=2013, mysql_phase='greeting', transport_reason='timeout'))
        self.assertNotIn('SHOULD_NOT_LEAK', json.dumps(detail))

    def test_driver_exception_preserves_numeric_code_without_payload(self):
        detail = database_error(RuntimeError(2013, 'Lost connection during query (connection reset); secret=hidden'))
        self.assertEqual(detail, dict(mysql_error_code=2013, mysql_phase='query', transport_reason='reset'))

    def test_connect_failure_is_logged_without_attempting_rollback(self):
        error = RuntimeError(2003, 'connection refused password=SHOULD_NOT_LEAK')
        def connect(**kwargs):
            raise error
        writer = Writer({ENV_PREFIX + 'USER': 'fixture', ENV_PREFIX + 'PASSWORD': 'fixture'},
                        connection_factory=connect)
        with patch('features.fb_page_assignment.storage.log') as log:
            with self.assertRaises(RuntimeError):
                writer.write_task(dict(task_id='fixture', rows=[]))
        fields = log.call_args.kwargs
        self.assertEqual((fields['role'], fields['phase'], fields['mysql_error_code']), ('writer', 'connect', 2003))
        self.assertNotIn('SHOULD_NOT_LEAK', json.dumps(fields))

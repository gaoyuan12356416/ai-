import json
import unittest
from unittest.mock import Mock, patch

from features.fb_page_assignment.runtime import database_error
from features.fb_page_assignment.storage import ENV_PREFIX, Writer
from features.fb_page_assignment.mysql_transport import connect_writer


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

    def test_compressed_transport_keeps_timeouts_and_transaction_controls(self):
        raw = Mock()
        connect = Mock(return_value=raw)
        config = dict(host='101.32.56.53', port=63353, database='ads_ai', user='fixture', password='fixture')
        connection = connect_writer(config, connect=connect)
        options = connect.call_args.kwargs
        self.assertTrue(options['compress'])
        self.assertTrue(options['buffered'])
        self.assertFalse(options['autocommit'])
        self.assertEqual((options['connection_timeout'], options['read_timeout'], options['write_timeout']), (3, 5, 5))
        connection.begin()
        connection.cursor()
        connection.commit()
        connection.rollback()
        connection.close()
        raw.start_transaction.assert_called_once_with()
        raw.commit.assert_called_once_with()
        raw.rollback.assert_called_once_with()
        raw.close.assert_called_once_with()

    def test_default_writer_uses_compressed_transport_without_changing_endpoint(self):
        writer = Writer({ENV_PREFIX + 'USER': 'fixture', ENV_PREFIX + 'PASSWORD': 'fixture'})
        with patch('features.fb_page_assignment.mysql_transport.connect_writer') as connect:
            writer._connect()
        self.assertEqual(connect.call_args.args[0], writer.config)

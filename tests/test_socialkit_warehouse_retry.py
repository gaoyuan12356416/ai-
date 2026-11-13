import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

import pymysql

from features.socialkit_analytics.storage import WarehouseWriter
from features.socialkit_analytics.collector import CollectionError
from features.socialkit_analytics.schema import EFFECT_FIELDS
from tests.test_socialkit_analytics_data import dataset


class Connection:
    def __init__(self, durable, failure=None, fail_commit=False):
        self.durable = durable
        self.failure = failure
        self.fail_commit = fail_commit
        self.pending = {}
        self.batches = []
        self.closed = False

    def cursor(self):
        return contextlib.nullcontext(self)

    def executemany(self, sql, values):
        self.batches.append((sql, list(values)))
        if self.failure and not self.fail_commit:
            raise self.failure
        self.pending.update({row[:2]: row for row in values})

    def commit(self):
        self.durable.update(self.pending)
        self.pending.clear()
        if self.failure and self.fail_commit:
            raise self.failure

    def close(self):
        self.closed = True


class WarehouseRetryTests(unittest.TestCase):
    def writer(self, connection):
        writer = WarehouseWriter.__new__(WarehouseWriter)
        writer.connection = connection
        writer.version = 'frozen-version'
        return writer

    def test_lost_commit_reply_replays_same_rows_without_double_count(self):
        durable = {}
        first = Connection(durable, pymysql.err.OperationalError(2013, 'private detail'), fail_commit=True)
        replacement = Connection(durable)
        writer = self.writer(first)
        log = io.StringIO()
        with patch.object(writer, '_connect', return_value=replacement) as reconnect, \
                patch('features.socialkit_analytics.storage.time.sleep'), contextlib.redirect_stdout(log):
            writer.write_day('frozen-version', dataset(revenue=1899))
        self.assertTrue(first.closed)
        reconnect.assert_called_once()
        self.assertEqual(first.batches[0], replacement.batches[0])
        self.assertEqual(len(durable), 2)
        effect_key = first.batches[0][1][0][:2]
        self.assertEqual(durable[effect_key][2 + EFFECT_FIELDS.index('revenue_cents')], 1899)
        self.assertNotIn('private detail', log.getvalue())

    def test_connection_loss_before_write_recovers_identical_facts(self):
        durable = {}
        first = Connection(durable, pymysql.err.OperationalError(2006, 'private detail'))
        replacement = Connection(durable)
        writer = self.writer(first)
        with patch.object(writer, '_connect', return_value=replacement), \
                patch('features.socialkit_analytics.storage.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            writer.write_day('frozen-version', dataset())
        self.assertEqual(first.batches[0], replacement.batches[0])
        self.assertEqual(len(durable), 2)

    def test_repeated_transport_loss_is_bounded_and_fails(self):
        connections = [Connection({}, pymysql.err.OperationalError(2013, 'private detail')) for _ in range(3)]
        writer = self.writer(connections[0])
        with patch.object(writer, '_connect', side_effect=connections[1:]) as reconnect, \
                patch('features.socialkit_analytics.storage.time.sleep') as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(pymysql.err.OperationalError):
                writer.write_day('frozen-version', dataset())
        self.assertEqual(reconnect.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5])

    def test_auth_schema_and_lock_errors_are_not_replayed(self):
        for code in (1045, 1064, 1142, 1205):
            with self.subTest(code=code):
                writer = self.writer(Connection({}, pymysql.err.OperationalError(code, 'private detail')))
                with patch.object(writer, '_connect') as reconnect:
                    with self.assertRaises(pymysql.err.OperationalError):
                        writer.write_day('frozen-version', dataset())
                reconnect.assert_not_called()

    def test_new_connection_revalidates_writer_identity(self):
        for identity in ({'db': 'ads_ai', 'ro': 0}, {'db': 'ads_ai', 'ro': 1}, {'db': 'other', 'ro': 0}):
            with self.subTest(identity=identity):
                connection = MagicMock()
                connection.cursor.return_value.__enter__.return_value.fetchone.return_value = identity
                writer = self.writer(None)
                writer.config = {}
                with patch('pymysql.connect', return_value=connection) as connect:
                    if identity == {'db': 'ads_ai', 'ro': 0}:
                        self.assertIs(writer._connect(), connection)
                    else:
                        with self.assertRaises(CollectionError):
                            writer._connect()
                        connection.close.assert_called_once()
                self.assertEqual(connect.call_args.kwargs['read_timeout'], 60)


if __name__ == '__main__':
    unittest.main()

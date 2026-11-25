import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from features.youtube_auto_publish.material_cards import CardSource, ROUTE, authorize, make_handler
from features.youtube_auto_publish.cache import ReadCache
from features.youtube_auto_publish.templates import WorkflowError


class TransferStatusTests(unittest.TestCase):
    def test_refresh_requested_while_workers_busy_survives_next_poll(self):
        cache = ReadCache(workers=1, clock=lambda: 10)
        cache.entries['card'] = {'at': 9, 'value': ['old']}
        cache.running.add('other')
        loaded = threading.Event()
        def load():
            loaded.set()
            return ['new']
        rows, meta = cache.read('card', load, refresh=True)
        self.assertEqual(rows, ['old'])
        self.assertTrue(meta['refreshing'])
        cache.running.remove('other')
        self.assertTrue(cache.read('card', load)[1]['refreshing'])
        self.assertTrue(loaded.wait(2))

    @staticmethod
    def encoded(**item):
        return [json.dumps(item).encode().hex()]

    def test_pending_intake_is_visible_but_never_selectable(self):
        reader = Mock(return_value=[self.encoded(id=42, name='cut', deleted=0, receipt='')])
        source = CardSource('', reader)
        with patch.object(source, '_read', return_value=[{'id': '40'}]):
            result = source._list_snapshot('SELECT 1', 'cut', '947', True)
        self.assertEqual(result['items'], [{'id': '40'}])
        self.assertEqual(result['pending_transfers'], [{'id': '42', 'name': 'cut'}])
        sql = reader.call_args.args[0]
        self.assertIn('BINARY username FROM kunlunads_dev.admin_users WHERE id=947', sql)
        self.assertIn("s.is_delete=0", sql)
        self.assertIn('0x637574', sql)

    def test_completed_intake_id_resolves_to_scoped_official_card(self):
        source = CardSource('', Mock(return_value=[self.encoded(id=42, deleted=1, receipt='46')]))
        with patch.object(source, '_read', return_value=[{'id': '46'}]) as ready:
            result = source._list_snapshot('SELECT 1', '42', '947', True)
        self.assertEqual(result['pending_transfers'], [])
        ready.assert_called_once_with('SELECT 1', search='46', uploader_id='947', compact=True)

    def test_transfer_query_failure_preserves_selectable_cards(self):
        source = CardSource('', Mock(side_effect=WorkflowError('material_query_failed', 'failed', 503)))
        with patch.object(source, '_read', return_value=[{'id': '4'}]):
            result = source._list_snapshot('SELECT 1', '', '', True)
        self.assertTrue(result['transfer_status_unavailable'])
        self.assertEqual(result['items'], [{'id': '4'}])


class CardRoutes(unittest.TestCase):
    def setUp(self):
        self.source = Mock()
        self.source.list.return_value = {'configured': True, 'items': [], 'uploaders': []}
        def auth(headers):
            if headers.get('Cookie') != 'valid-session':
                raise WorkflowError('permission_denied', 'denied', 403)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.source, auth))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def test_permission_denial_never_reads_catalog(self):
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.url + ROUTE)
        self.assertEqual(ctx.exception.code, 403)
        self.source.list.assert_not_called()

    def test_only_authenticated_cards_and_filters(self):
        req = Request(self.url + ROUTE + '?search=example&uploader_id=947&refresh=1', headers={'Cookie': 'valid-session'})
        with urlopen(req) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertTrue(json.load(response)['ok'])
        self.source.list.assert_called_once_with('example', uploader_id='947', refresh=True, compact=True)

    def test_auth_delegates_cookie_and_fails_closed(self):
        with patch('features.youtube_auto_publish.material_cards.HTTPConnection') as cls:
            conn = cls.return_value
            response = conn.getresponse.return_value
            for status, body, expected in ((200, b'{"source":{"configured":true}}', None),
                                           (401, b'{}', 401), (403, b'{}', 403),
                                           (302, b'{}', 503), (200, b'{}', 503),
                                           (503, b'{}', 503)):
                response.status = status
                response.read.return_value = body
                if expected:
                    with self.assertRaises(WorkflowError) as ctx:
                        authorize({'Cookie': 'valid-session'})
                    self.assertEqual(ctx.exception.status, expected)
                else:
                    authorize({'Cookie': 'valid-session'})
            args, kwargs = conn.request.call_args
            self.assertEqual(args[:2], ('GET', '/api/youtube-auto-publish/bootstrap?include_channels=0'))
            self.assertEqual(kwargs['headers']['Cookie'], 'valid-session')


if __name__ == '__main__':
    unittest.main()

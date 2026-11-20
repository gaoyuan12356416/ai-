import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from features.youtube_auto_publish.material_cards import ROUTE, authorize, make_handler
from features.youtube_auto_publish.templates import WorkflowError


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

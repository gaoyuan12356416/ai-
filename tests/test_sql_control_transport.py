"""HTTP transport tests: no database, live gate or production paths."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from features.gpu_offload.sql_client import Broker, AdmissionError


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(2)
        with self.server.lock:
            self.server.connections += 1
            self.number = self.server.connections

    def log_message(self, *_): pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.server.lock:
            self.server.received.append((self.number, self.path, payload, self.headers.get('Authorization')))
        if self.path.endswith('/begin'):
            # The authority may have consumed begin even if the reply is lost.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        status = 200
        body = {'allowed': True}
        if payload.get('rate_limited'):
            status, body = 429, {'error': 'query_rate', 'retry_after': .1}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        if payload.get('legacy_close'):
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)


class ControlTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.lock = threading.Lock()
        self.server.connections = 0
        self.server.received = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.broker = Broker(self.server.server_port, 'fixture', Path(self.temp.name))

    def tearDown(self):
        self.broker.close_transport()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_reuses_connection_without_bypassing_authority_or_rate_errors(self):
        self.broker.call('query', {'lease_id': 'one'})
        with self.assertRaises(AdmissionError) as error:
            self.broker.call('query', {'lease_id': 'one', 'rate_limited': True})
        self.assertEqual(error.exception.code, 'query_rate')
        self.broker.call('query', {'lease_id': 'one'})
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(len(self.server.received), 3)
        self.assertTrue(all(row[3] == 'Bearer fixture' for row in self.server.received))

    def test_unknown_begin_is_never_replayed(self):
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.broker.call('begin', {'lease_id': 'one'})
        self.assertEqual([row[1] for row in self.server.received], ['/v1/lease/begin'])
        self.assertIsNone(self.broker.local.connection)

    def test_heartbeat_has_an_independent_connection(self):
        self.broker.call('query', {'lease_id': 'one'})
        errors = []
        def heartbeat():
            try: self.broker.call('heartbeat', {'lease_id': 'one'})
            except Exception as error: errors.append(error)
            finally: self.broker.close_transport()
        thread = threading.Thread(target=heartbeat)
        thread.start(); thread.join(timeout=3)
        self.assertFalse(thread.is_alive()); self.assertFalse(errors)
        self.broker.call('query', {'lease_id': 'one'})
        self.assertEqual([row[0] for row in self.server.received], [1, 2, 1])

    def test_legacy_close_and_idle_expiry_open_fresh_connections(self):
        self.broker.call('query', {'legacy_close': True})
        self.broker.call('query', {})
        self.broker.local.used_at -= 16
        self.broker.call('query', {})
        self.assertEqual(self.server.connections, 3)


if __name__ == '__main__': unittest.main()

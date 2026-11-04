"""Broker scope, protocol bounds, WAL reads and conservative client failures."""
import contextlib
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from features.x_auto_posts import dispatch_reader as client
from features.x_auto_posts import publisher
from scripts import x_auto_dispatch_state_bridge as bridge


STATE={"source_key":"x:queue:5753","job_id":32490,"stage":"prepare","generation":1,
       "status":"failed","error_code":"x_account_not_publishable","error_message":"",
       "updated_at":"2026-11-04T02:20:48Z","known_failure":True,"has_operations":False,"has_publication":False}


class FakeSocket:
    def __init__(self,payload):self.payload=payload;self.sent=b"";self.timeouts=[]
    def settimeout(self,value):self.timeouts.append(value)
    def connect(self,path):self.path=path
    def recv(self,count):
        out=self.payload[:count];self.payload=self.payload[count:];return out
    def sendall(self,value):self.sent+=value
    def __enter__(self):return self
    def __exit__(self,*args):pass


class ProtocolTests(unittest.TestCase):
    def handle(self,payload,reader=None):
        request=FakeSocket(payload)
        reader=reader or mock.Mock(return_value=STATE.copy())
        handler=bridge.StateHandler.__new__(bridge.StateHandler)
        handler.request=request;handler.server=SimpleNamespace(state_reader=reader)
        handler.handle()
        return json.loads(request.sent),reader

    def call_client(self,reply):
        connection=FakeSocket(reply)
        with mock.patch.object(socket,"AF_UNIX",1,create=True),mock.patch.object(socket,"socket",return_value=connection):
            result=client.read_dispatch_state(5753)
        self.assertEqual(connection.sent,b'{"queue_id":5753}\n')
        self.assertEqual(connection.path,client.DEFAULT_SOCKET_PATH)
        return result

    def test_only_queue_id_is_accepted_and_response_is_allowlisted(self):
        reader=mock.Mock(return_value={**STATE,"access_token":"must-not-leak","payload_json":"private"})
        result,_=self.handle(b'{"queue_id":5753}\n',reader)
        self.assertEqual(result,{"ok":True,"state":STATE})
        reader.assert_called_once_with(5753)

    def test_malformed_large_sql_and_noninteger_requests_never_read_db(self):
        for payload in (b"not-json\n",b'{"queue_id":true}\n',b'{"queue_id":0}\n',b'{"queue_id":"5753"}\n',
                        b'{"queue_id":5753,"sql":"DELETE"}\n',b'{"queue_id":9223372036854775808}\n',
                        b"x"*1025+b"\n",b"\xff\n",b'{"queue_id":5753}\n{}\n'):
            with self.subTest(payload=payload[:50]):
                result,reader=self.handle(payload)
                self.assertFalse(result["ok"]);reader.assert_not_called()

    def test_db_errors_never_disclose_paths_or_sql(self):
        reader=mock.Mock(side_effect=sqlite3.OperationalError("secret database SQL and token"))
        result,_=self.handle(b'{"queue_id":5753}\n',reader)
        self.assertEqual(result,{"ok":False,"error":"state_unavailable"})

    def test_response_size_is_bounded(self):
        result,_=self.handle(b'{"queue_id":5753}\n',mock.Mock(return_value={**STATE,"error_message":"x"*20000}))
        self.assertEqual(result,{"ok":False,"error":"state_unavailable"})

    def test_client_validates_identity_and_never_accepts_false_known_failure(self):
        self.assertEqual(self.call_client(json.dumps({"ok":True,"state":STATE}).encode()+b"\n"),STATE)
        for state in ({**STATE,"source_key":"x:queue:999"},{**STATE,"known_failure":"true"},{**STATE,"sql":"private"}):
            with self.subTest(state=state):
                with self.assertRaises(client.DispatchStateUnavailable):
                    self.call_client(json.dumps({"ok":True,"state":state}).encode()+b"\n")

    def test_client_bounds_response_and_handles_unavailable_broker(self):
        for payload in (b"x"*17000+b"\n",b'{"ok":false,"error":"state_unavailable"}\n',b'{"ok":true}',b"\xff\n"):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(client.DispatchStateUnavailable):self.call_client(payload)

    def test_unavailable_socket_has_no_database_fallback(self):
        with mock.patch.object(socket,"AF_UNIX",1,create=True),mock.patch.object(socket,"socket",side_effect=FileNotFoundError()),mock.patch.object(sqlite3,"connect") as db:
            with self.assertRaises(client.DispatchStateUnavailable):client.read_dispatch_state(5753)
            db.assert_not_called()

    def test_slow_partial_input_has_a_total_deadline(self):
        connection=FakeSocket(b"a"*20)
        def slow_recv(count):time.sleep(0.015);return b"a"
        connection.recv=slow_recv
        started=time.monotonic()
        with self.assertRaises(client.DispatchStateUnavailable):client.receive_line(connection,1024,started+0.02)
        self.assertLess(time.monotonic()-started,0.1)

    def test_handler_concurrency_is_bounded(self):
        server=bridge.DispatchStateServer.__new__(bridge.DispatchStateServer)
        server.slots=threading.BoundedSemaphore(1);server.slots.acquire()
        server.shutdown_request=mock.Mock()
        request=object();server.process_request(request,None)
        server.shutdown_request.assert_called_once_with(request)


@unittest.skipUnless(hasattr(socket,"AF_UNIX"),"Unix socket integration requires Linux")
class UnixIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="xa-");self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/"state.sock")

    def start(self,reader):
        server=bridge.DispatchStateServer(self.path,reader)
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(timeout=2)))
        return server

    def test_real_unix_roundtrip(self):
        self.start(lambda q:STATE.copy())
        self.assertEqual(client.read_dispatch_state(5753,socket_path=self.path),STATE)

    def test_broker_reads_live_wal_while_writer_connection_remains_open(self):
        db=Path(self.tmp.name)/"dispatch.sqlite3"
        writer=sqlite3.connect(db);self.addCleanup(writer.close)
        writer.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,status TEXT,generation INTEGER,external_intent INTEGER,error_code TEXT,error_message TEXT,canonical_receipt TEXT,updated_at TEXT);
          CREATE TABLE pipeline_operation(job_id INTEGER);
          CREATE TABLE pipeline_publication(job_id INTEGER);
          CREATE TABLE pipeline_receipt(job_id INTEGER);
          INSERT INTO pipeline_job VALUES(32490,'x:queue:5753','prepare','failed',1,0,'x_account_not_publishable','','','2026-11-04T02:20:48Z');
        """);writer.commit()
        self.assertTrue(Path(str(db)+"-wal").exists())
        self.start(lambda q:publisher.read_dispatch_queue_state(db,q))
        result=client.read_dispatch_state(5753,socket_path=self.path)
        self.assertTrue(result["known_failure"])
        writer.execute("UPDATE pipeline_job SET status='running'");writer.commit()
        self.assertFalse(client.read_dispatch_state(5753,socket_path=self.path)["known_failure"])

    def test_missing_broker_fails_conservatively(self):
        with self.assertRaises(client.DispatchStateUnavailable):
            client.read_dispatch_state(5753,socket_path=self.path)


if __name__=="__main__":unittest.main()

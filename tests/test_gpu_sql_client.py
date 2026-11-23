from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.gpu_offload.sql_client import Broker,Session,AdmissionError
from features.gpu_offload.sql_leases import LeaseBook

IDENTITY=dict(server_uuid='14129611-64cd-4b66-8b8c-822d2363c0ae',connection_id=123,read_only=1,transaction_read_only=1)


class Wire:
    def __init__(self):self.quit_written=False;self.aborted=False
    def settimeout(self,_):pass
    def recv(self,_):return b''
    def shutdown(self,_):self.aborted=True
    def close(self):pass


class Cursor:
    def __init__(self,connection):self.connection=connection
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def execute(self,sql,args=None):self.connection._execute_command(3,sql)
    def fetchone(self):return dict(IDENTITY)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.now=[100.0];self.book=LeaseBook(self.root/'leases.sqlite3',clock=lambda:self.now[0])
        self.calls=[];self.sql=[];owner=self
        class LocalBroker:
            def flush_closed(self):pass
            def call(self,op,payload):
                owner.calls.append(op);book=owner.book;key=payload.get('lease_id');node='hk-read-assist'
                if op=='acquire':return book.reserve(node,payload['task'],available=lambda:14-book.occupied)
                if op=='begin':return book.begin(key,node)
                if op=='bind':return book.bind(key,node,payload['identity'])
                if op=='query':
                    owner.now[0]+=1;return book.start_query(key,node,setup=payload['setup'])
                if op=='heartbeat':return book.heartbeat(key,node)
                if op=='release':return book.release(key,node,server_eof=payload.get('server_eof',False),identity=payload.get('identity'))
                raise AssertionError(op)
            def closed(self,key,identity):return self.call('release',dict(lease_id=key,identity=identity,server_eof=True))
        self.broker=LocalBroker()
        self.patches=[]
        def initialize(connection,**kwargs):
            connection._sock=Wire();connection._rfile=None;connection._closed=False
            # PyMySQL sends initialization SQL before its constructor returns.
            connection._execute_command(3,'SET NAMES utf8mb4')
        for name,value in [('__init__',initialize),('_execute_command',lambda connection,command,sql:owner.sql.append(sql)),('cursor',lambda connection:Cursor(connection)),('_write_bytes',lambda *args:None),('_force_close',lambda *args:None)]:
            item=patch('pymysql.connections.Connection.'+name,new=value);item.start();self.patches.append(item)

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.book.close();self.temp.cleanup()

    def test_initialization_sql_is_included_in_shared_start_rate(self):
        session=Session(self.broker,'fixture')
        session.connect(user='fixture',password='fixture')
        try:
            self.assertEqual(self.calls.count('query'),6)
            self.assertIn('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ',self.sql)
            self.assertIn('START TRANSACTION READ ONLY',self.sql)
            self.assertFalse(any('WITH CONSISTENT SNAPSHOT' in sql for sql in self.sql))
            self.assertEqual(self.book.occupied,1)
            session.cursor().execute('SELECT 1')
            self.assertEqual(self.calls.count('query'),7)
            for sql in ('UPDATE table_name SET value=1','SET SESSION TRANSACTION READ WRITE','DELETE FROM table_name'):
                with self.assertRaises(AdmissionError):session.cursor().execute(sql)
            self.assertEqual(self.calls.count('query'),7)
        finally:session.close()
        self.assertEqual(self.book.occupied,0)

    def test_local_abort_is_not_mistaken_for_peer_eof(self):
        session=Session(self.broker,'fixture');session.connect(user='fixture',password='fixture')
        session.locally_aborted=True;session.close()
        self.assertEqual(self.book.occupied,1)
        self.assertNotIn('release',self.calls)
        self.now[0]+=100;self.book.expire();self.assertEqual(self.book.get(session.key)['state'],'unknown')

    def test_permission_or_deadline_loss_stops_new_sql(self):
        session=Session(self.broker,'fixture');session.connect(user='fixture',password='fixture')
        before=self.calls.count('query');session.lost.set()
        try:
            with self.assertRaises(AdmissionError):session.cursor().execute('SELECT 1')
            self.assertEqual(self.calls.count('query'),before)
        finally:session.close()

    def test_failed_close_acknowledgement_has_durable_retry_without_sql(self):
        broker=Broker(1,'fixture',self.root/'receipts');key='a'*32
        with patch.object(broker,'call',side_effect=OSError('control_link_lost')):
            with self.assertRaises(OSError):broker.closed(key,IDENTITY)
        self.assertTrue((self.root/'receipts'/(key+'.json')).exists())
        with patch.object(broker,'call',return_value={'state':'closed'}) as call:
            broker.flush_closed();self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0],'release')
        self.assertFalse(list((self.root/'receipts').glob('*.json')))


if __name__=='__main__':unittest.main()

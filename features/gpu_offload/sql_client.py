"""Read-only MySQL sessions admitted by the CPU's shared query budget."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time


class AdmissionError(RuntimeError):
    def __init__(self,code,retry_after=0):
        self.code,self.retry_after=code,retry_after
        super().__init__(code)


class Broker:
    def __init__(self,port,token,receipt_root):
        self.port,self.token,self.receipt_root=port,token,Path(receipt_root)
        # Query admission and heartbeat run on separate threads. Reuse each
        # thread's control connection, without sharing an HTTP response stream.
        self.local=threading.local()

    def close_transport(self):
        conn=getattr(self.local,'connection',None)
        if conn is not None:conn.close()
        self.local.connection=None

    def call(self,operation,payload):
        conn=getattr(self.local,'connection',None)
        if conn is not None and time.monotonic()-self.local.used_at>=15:
            self.close_transport();conn=None
        if conn is None:
            conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
            self.local.connection=conn
        try:
            body=json.dumps(payload,separators=(',',':')).encode()
            conn.request('POST','/v1/lease/'+operation,body=body,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
            response=conn.getresponse();data=response.read(16385)
            if len(data)>16384:raise AdmissionError('invalid_broker_response')
            try:value=json.loads(data)
            except (ValueError,UnicodeDecodeError):
                raise AdmissionError('invalid_broker_response_http_'+str(response.status)) from None
            if not isinstance(value,dict):raise AdmissionError('invalid_broker_response')
            self.local.used_at=time.monotonic()
            if response.will_close:self.close_transport()
        except Exception:
            # Never replay a request after an ambiguous response. In particular
            # acquire/begin must not create an unaccounted second reservation.
            self.close_transport();raise
        if response.status!=200:raise AdmissionError(value.get('error','admission_unavailable'),float(value.get('retry_after',0)))
        return value

    def flush_closed(self):
        self.receipt_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        for path in sorted(self.receipt_root.glob('*.json'))[:8]:
            payload=json.loads(path.read_text())
            if payload.get('server_eof') is not True:raise AdmissionError('invalid_close_receipt')
            self.call('release',payload);path.unlink()

    def closed(self,key,identity):
        if not re.fullmatch('[a-f0-9]{32}',key):raise AdmissionError('invalid_lease_identity')
        self.receipt_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        payload=dict(lease_id=key,identity=identity,server_eof=True)
        target=self.receipt_root/(key+'.json');temporary=target.with_suffix('.tmp')
        with temporary.open('w') as out:
            json.dump(payload,out);out.flush();os.fsync(out.fileno())
        temporary.chmod(0o600);temporary.replace(target)
        # If the acknowledgement is lost, the durable proof is retried before
        # another acquisition. It never retries a SQL statement or publication.
        self.call('release',payload);target.unlink()


class Session:
    def __init__(self,broker,task,*,budget_seconds=840,statement_timeout_ms=15000):
        if not 0<budget_seconds<=840 or not 0<statement_timeout_ms<=15000:
            raise ValueError('query_budget_cannot_exceed_existing_report_limits')
        self.broker,self.task=broker,task
        self.deadline=time.monotonic()+budget_seconds
        self.statement_timeout_ms=statement_timeout_ms
        self.key,self.identity,self.connection=None,None,None
        self.done,self.lost=threading.Event(),threading.Event()
        self.watcher=None;self.locally_aborted=False;self.connect_attempted=False

    def query_start(self):
        while True:
            if self.lost.is_set() or time.monotonic()>=self.deadline:raise AdmissionError('query_lease_or_deadline_lost')
            try:
                self.broker.call('query',dict(lease_id=self.key,setup=self.identity is None));return
            except AdmissionError as exc:
                # Only wait for admission of a statement not yet sent to MySQL.
                if exc.code!='query_rate' or exc.retry_after>2:raise
                if self.done.wait(max(0.01,exc.retry_after)):raise AdmissionError('query_session_closed')

    def admit_sql(self,sql):
        text=bytes(sql).decode('utf-8','replace') if isinstance(sql,(bytes,bytearray)) else str(sql)
        if self.identity is not None and text.lstrip().split(None,1)[0].upper() not in ('SELECT','SHOW'):
            raise AdmissionError('business_statement_must_be_read_only')
        self.query_start()

    def connect(self,*,user,password,database=None,report_options=False,compressed=False):
        # Resolve the driver before acquiring a scarce database lease.
        if compressed:
            from .sql_compressed import connection_factory
            factory=connection_factory(self)
        else:
            import pymysql
            session=self
            class Connection(pymysql.connections.Connection):
                def _execute_command(self,command,sql):
                    if command==pymysql.constants.COMMAND.COM_QUERY:session.admit_sql(sql)
                    return super()._execute_command(command,sql)
            factory=lambda **options:Connection(**options,cursorclass=pymysql.cursors.DictCursor,
                                               connect_timeout=4,program_name='ai-offload/'+self.key)
        self.broker.flush_closed()
        self.key=self.broker.call('acquire',{'task':self.task})['id']
        self.broker.call('begin',{'lease_id':self.key})
        self.connect_attempted=True
        self.connection=factory(host='127.0.0.1',port=19969,user=user,password=password,database=database,
                                charset='utf8mb4',read_timeout=60,write_timeout=15,autocommit=True)
        def watch():
            try:
                while not self.done.wait(5):
                    try:
                        if time.monotonic()>=self.deadline:raise AdmissionError('refresh_deadline')
                        self.broker.call('heartbeat',{'lease_id':self.key})
                    except Exception as exc:
                        self.lost.set();self.locally_aborted=True
                        print(json.dumps({'event':'offload_sql_lease_lost','task':self.task,'lease':self.key,
                                          'error_type':type(exc).__name__,'code':getattr(exc,'code',None)}),flush=True)
                        try:self.connection._sock.shutdown(socket.SHUT_RDWR)
                        except (OSError,AttributeError):pass
                        return
            finally:
                close=getattr(self.broker,'close_transport',None)
                if close:close()
        self.watcher=threading.Thread(target=watch,name='read-only-sql-lease',daemon=True);self.watcher.start()
        with self.connection.cursor() as cur:
            cur.execute('SET SESSION MAX_EXECUTION_TIME=%s',(self.statement_timeout_ms,))
            cur.execute('SET SESSION TRANSACTION READ ONLY')
            if report_options:
                cur.execute('SET SESSION range_optimizer_max_mem_size=67108864')
                cur.execute("SET SESSION time_zone='+08:00'")
            cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            # Keep explicit read-only mode even on the dedicated reader endpoint.
            # REPEATABLE READ keeps one snapshot from the first data read for
            # the entire refresh, with no statement-level auto-commit reads.
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SELECT @@server_uuid AS server_uuid, CONNECTION_ID() AS connection_id, '
                        '@@read_only AS read_only, @@tx_read_only AS transaction_read_only, '
                        "SUBSTRING_INDEX(CURRENT_USER(),'@',1) AS database_user")
            identity=cur.fetchone()
            self.broker.call('bind',{'lease_id':self.key,'identity':identity})
            self.identity=identity
        return self

    def cursor(self):return self.connection.cursor()

    def close(self):
        self.done.set()
        if self.watcher:self.watcher.join(timeout=5)
        eof=False
        if self.connection is not None:
            try:
                wire=self.connection._sock
                if wire is not None and not self.locally_aborted:
                    # COM_QUIT has no response packet. Peer EOF proves the server
                    # has ended this client connection; local shutdown is never
                    # accepted as that proof.
                    self.connection._write_bytes(b'\x01\x00\x00\x00\x01')
                    wire.settimeout(4);eof=wire.recv(1)==b''
            except Exception:pass  # A driver error is not proof of server EOF.
            finally:self.connection._force_close()
        if eof:
            try:self.broker.closed(self.key,self.identity)
            finally:
                close=getattr(self.broker,'close_transport',None)
                if close:close()
        elif self.key and not self.connect_attempted:
            # The server may already have issued begin permission if its reply
            # was lost. It conservatively retains that case for reconciliation.
            try:self.broker.call('release',{'lease_id':self.key})
            except Exception:pass
        if self.key and self.connect_attempted and not eof:
            print(json.dumps({'event':'offload_sql_close_unconfirmed','task':self.task,'lease':self.key,
                              'locally_aborted':self.locally_aborted}),flush=True)
        close=getattr(self.broker,'close_transport',None)
        if close:close()
        # Issued but unconfirmed sessions deliberately retain their shared slot.


@contextmanager
def read_session(broker,task,*,user,password,database=None,budget_seconds=840,statement_timeout_ms=15000,report_options=False,compressed=False):
    session=Session(broker,task,budget_seconds=budget_seconds,statement_timeout_ms=statement_timeout_ms)
    try:
        session.connect(user=user,password=password,database=database,report_options=report_options,compressed=compressed)
        yield session
        if session.lost.is_set():raise AdmissionError('query_lease_lost')
    finally:session.close()

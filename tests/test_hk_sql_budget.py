from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import multiprocessing
from pathlib import Path
import os
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from features.hk_sql_budget import BudgetError, Ledger
from features.hk_sql_budget import connection


def hold_in_child(path, ready, done):
    ledger=Ledger(path)
    token=ledger.reserve("write",time.monotonic()+10)
    ready.set()
    try:
        done.wait(8)
    finally:
        ledger.end(token,True)


class FakeCursor:
    def __init__(self, con):self.con=con
    def __enter__(self):return self
    def __exit__(self,*unused):pass
    def execute(self, sql, args=None):
        self.con.sql.append(sql)
    def fetchone(self):
        return ("server-a",51,"operator@hk",int(self.con.read),int(self.con.read))
    def fetchall(self):return [(n,) for n in self.con.visible]


class FakeWire:
    def __init__(self, eof=True):self.eof=eof;self.closed=False
    def settimeout(self,x):pass
    def shutdown(self,*a):pass
    def recv(self,n):return b"" if self.eof else b"x"
    def close(self):self.closed=True


class FakeConn:
    def __init__(self, read, wire):
        self.read=read;self._sock=wire;self.sql=[];self.visible=[];self.closed=False
    def connect(self, sock=None):
        self._sock=sock
        self.session.before_authentication()
    def cursor(self):return FakeCursor(self)
    def _force_close(self):self.closed=True
    def _write_bytes(self,b):
        if b!=b"\x01\x00\x00\x00\x01":raise AssertionError("unexpected COM_QUIT")
    def commit(self):self.sql.append("COMMIT")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.ledger=Ledger(self.t.name)

    def test_writer_contention_uses_caller_deadline_and_status_remains_read_only(self):
        blocker = sqlite3.connect(str(self.ledger.path), isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(self.ledger.status()["available"], 30)
            short = time.monotonic()
            with self.assertRaisesRegex(BudgetError, "hk_sql_budget_timeout"):
                self.ledger.reserve("read", short + 0.10)
            self.assertLess(time.monotonic() - short, 1)
            with ThreadPoolExecutor(1) as workers:
                wait = workers.submit(self.ledger.reserve, "read", time.monotonic() + 6)
                time.sleep(2.2)  # Regression: the previous local timeout was two seconds.
                self.assertFalse(wait.done())
                blocker.rollback()
                token = wait.result(timeout=2)
        finally:
            blocker.rollback()
        self.ledger.end(token, True)
        self.assertEqual((self.ledger.status()["queued"], self.ledger.status()["in_use"]), (0, 0))

    def test_30th_grant_shared_by_threads_and_processes_then_31_waits(self):
        ctx=multiprocessing.get_context("spawn")
        ready,done=ctx.Event(),ctx.Event()
        proc=ctx.Process(target=hold_in_child,args=(self.t.name,ready,done))
        proc.start()
        try:
            self.assertTrue(ready.wait(8))
            keys=[self.ledger.reserve("read",time.monotonic()+4) for _ in range(29)]
            with ThreadPoolExecutor(1) as worker:
                pending=worker.submit(self.ledger.reserve,"write",time.monotonic()+4)
                end=time.monotonic()+2
                while self.ledger.status()["queued"]==0 and time.monotonic()<end:time.sleep(.01)
                self.assertFalse(pending.done())
                self.assertEqual(self.ledger.status()["roles"],{"read":29,"write":1})
                self.ledger.end(keys.pop(),True)
                keys.append(pending.result(timeout=3))
            self.assertEqual(self.ledger.status()["in_use"],30)
            for token in keys:self.ledger.end(token, True)
        finally:
            done.set();proc.join(8)
            if proc.is_alive():proc.terminate();proc.join()
        self.assertEqual(proc.exitcode,0)
        self.assertEqual(self.ledger.status()["in_use"],0)

    def test_waiter_timeout_does_not_occupy_or_block_next(self):
        self.ledger.configure(capacity=2,legacy_reserved=1)
        token=self.ledger.reserve("read",time.monotonic()+1)
        with self.assertRaisesRegex(BudgetError,"hk_sql_budget_timeout"):
            self.ledger.reserve("read",time.monotonic()+.05)
        self.assertEqual(self.ledger.status()["queued"],0)
        self.ledger.end(token,True)
        self.ledger.configure(legacy_reserved=0)
        self.assertEqual(self.ledger.status()["available"],2)

    def test_ambiguous_and_crashed_leases_need_positive_evidence(self):
        def new_key():
            return self.ledger.reserve("read",time.monotonic()+1)
        reserved, opening, bound=new_key(),new_key(),new_key()
        self.ledger.opening(opening)
        self.ledger.opening(bound)
        self.ledger.bind(bound,("uuid",17,"user@hk"))
        with patch("features.hk_sql_budget.ledger.owner_finished",return_value=True):
            rows=self.ledger.inspect_abandoned()
        self.assertEqual({r["token"] for r in rows},{opening,bound})
        con=FakeConn(True,FakeWire())
        con.visible=[63]
        self.assertEqual(self.ledger.verify_absence(con,("other",63,"user@hk"),rows),0)
        self.assertEqual(self.ledger.verify_absence(con,("uuid",63,"other@hk"),rows),0)
        con.visible=[]
        self.assertEqual(self.ledger.verify_absence(con,("uuid",63,"user@hk"),rows),0)
        con.visible=[17,63]
        self.assertEqual(self.ledger.verify_absence(con,("uuid",63,"user@hk"),rows),0)
        con.visible=[63]
        self.assertEqual(self.ledger.verify_absence(con,("uuid",63,"user@hk"),rows),1)
        self.assertEqual(self.ledger.status()["states"],{"unknown":1})

    def test_role_and_release_require_peer_eof(self):
        for role,eof in (("read",True),("write",True),("write",False)):
            wire=FakeWire(eof); con=FakeConn(role=="read",wire)
            def factory(session,*args):
                session.connection=con
                con.session=session
                return con
            with patch.object(connection,"local_ledger",return_value=self.ledger), \
                 patch.object(connection,"_ordinary_connection",side_effect=factory), \
                 patch.object(connection.socket,"create_connection",return_value=wire):
                with connection.session_connection(role,time.monotonic()+3,user="x",password="x") as c:
                    if role=="write":c.commit()
            self.assertIn("START TRANSACTION "+("READ ONLY" if role=="read" else "READ WRITE"),con.sql)
            self.assertEqual(con.sql[-1],"ROLLBACK")
            self.assertTrue(con.closed)
        self.assertEqual(self.ledger.status()["states"],{"unknown":1})

    def test_missing_endpoint_does_not_consume_a_slot(self):
        with patch.object(connection,"local_ledger",return_value=self.ledger), \
             patch.object(connection,"_ordinary_connection",return_value=FakeConn(True,FakeWire())), \
             patch.object(connection.socket,"create_connection",side_effect=ConnectionRefusedError):
            with self.assertRaises(ConnectionRefusedError):
                with connection.session_connection("read",time.monotonic()+3,user="x",password="y"):
                    self.fail("connection unexpectedly succeeded")
        self.assertEqual(self.ledger.status()["in_use"],0)

    def test_timeout_before_server_greeting_never_acquired_an_authenticated_session(self):
        wire=FakeWire()
        fake=FakeConn(True,wire)
        def factory(s,*args):
            s.connection=fake
            return fake
        def greeting_timeout(*unused,**kwargs):
            raise TimeoutError("MySQL greeting")
        fake.connect=greeting_timeout
        with patch.object(connection,"local_ledger",return_value=self.ledger), \
             patch.object(connection,"_ordinary_connection",side_effect=factory), \
             patch.object(connection.socket,"create_connection",return_value=wire):
            with self.assertRaises(TimeoutError):
                with connection.session_connection("read",time.monotonic()+3,user="x",password="y"):
                    pass
        self.assertEqual(self.ledger.status()["in_use"],0)
        self.assertTrue(wire.closed)

    def test_scoped_adapter_survives_campaign_style_child_process(self):
        launcher=Path(__file__).resolve().parents[1]/"ops/hk-sql-budget/launch.py"
        cwd=Path(self.t.name)/"old-release"
        package=cwd/"features/gpu_offload";package.mkdir(parents=True)
        for p in (package.parent/"__init__.py",package/"__init__.py",package/"sql_client.py"):
            p.write_text("")
        code="from features.gpu_offload.sql_client import read_session; print(read_session.__module__)"
        (package/"metadata_reader.py").write_text(
            "import subprocess,sys\n"+code+"\nsubprocess.run([sys.executable,'-c',"+repr(code)+"],check=True)\n")
        proc=subprocess.run([sys.executable,str(launcher),"features.gpu_offload.metadata_reader"],
                cwd=cwd,env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1"),text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),["features.hk_sql_budget.legacy"]*2)

    def test_guards_legacy_business_reads(self):
        s=connection._Session(time.monotonic()+1,"read");s.business=True
        for sql in ("SELECT 1","SHOW VARIABLES","EXPLAIN SELECT 1","ROLLBACK"):s.admit_sql(sql)
        for sql in ("UPDATE x SET n=2","SET SESSION TRANSACTION READ WRITE"):
            with self.assertRaises(BudgetError):s.admit_sql(sql)

    def test_legacy_proof_is_per_invocation_and_first_packet(self):
        full=Path(__file__).resolve().parents[1]/"ops/hk-sql-budget/switch.py"
        spec=importlib.util.spec_from_file_location("test_sql_budget_switch",full)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        entry=dict(lease_id="a"*32,created=time.time(),state="unknown",bound=False,task="campaign-copy-report")
        lines=[json.dumps(dict(event="offload_sql_close_unconfirmed",lease=entry["lease_id"],task=entry["task"])),
               "self._do_handshake()","packet = bytes(self._socket.recv())",
               "mysql.connector.errors.ConnectionTimeoutError: 3024:",
               json.dumps(dict(status="failed",exit_code=1))]
        journal=[dict(_PID="123",_SYSTEMD_INVOCATION_ID="inv1",MESSAGE=line) for line in lines]
        def check():
            with patch.object(mod,"run",return_value="\n".join(json.dumps(row) for row in journal)):
                return mod.first_packet_failure(entry)
        self.assertTrue(check())
        journal[-1]["_SYSTEMD_INVOCATION_ID"]="other";self.assertFalse(check())
        journal[-1]["_SYSTEMD_INVOCATION_ID"]="inv1";journal[2]["MESSAGE"]="unrelated recv()"
        self.assertFalse(check())


if __name__=="__main__":unittest.main()

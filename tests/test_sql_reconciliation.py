import json
from pathlib import Path
import tempfile
import time
import unittest

from features.gpu_offload.sql_leases import LeaseBook
from features.gpu_offload.sql_reconciliation import GateReconciliation,watchdog_reconcile,reconcile_live_book

IDENTITY=dict(server_uuid='14129611-64cd-4b66-8b8c-822d2363c0ae',connection_id=123,read_only=1,transaction_read_only=1,database_user='reader')


class Cursor:
    def __init__(self,observation):self.observation=observation;self.statements=[]
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def execute(self,sql,args):self.statements.append((sql,args))
    def fetchone(self):return self.observation


class Connection:
    def __init__(self,observation):self.active_cursor=Cursor(observation)
    def cursor(self):return self.active_cursor


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.now=[time.time()-1000];self.book=LeaseBook(self.root/'book.sqlite3',clock=lambda:self.now[0])
        self.key=self.book.reserve('hk-read-assist','fixture',available=lambda:14)['id']
        self.book.begin(self.key,'hk-read-assist');self.book.bind(self.key,'hk-read-assist',IDENTITY)
        self.now[0]+=100;self.book.expire()
        self.requests,self.proofs=self.root/'requests.json',self.root/'proofs.json'
        self.gate=GateReconciliation(self.book,self.requests,self.proofs)
        self.gate.tick()

    def tearDown(self):self.book.close();self.temp.cleanup()

    def check(self,**changes):
        observation=dict(server_uuid=IDENTITY['server_uuid'],database_user='reader',session_present=0,**changes)
        conn=Connection(observation)
        watchdog_reconcile(conn,self.requests,self.proofs);self.gate.tick()
        self.assertEqual(len(conn.active_cursor.statements),1)
        sql,args=conn.active_cursor.statements[0]
        self.assertTrue(sql.startswith('SELECT'));self.assertEqual(args,(123,'reader'))

    def test_absence_on_matching_server_and_user_releases_only_unknown_lease(self):
        self.book.observe_presence(self.key,observed_server_uuid=IDENTITY['server_uuid'],observed_connection_id=123)
        self.check();self.assertEqual(self.book.occupied,0)
        self.assertEqual(self.book.get(self.key)['reason'],'verified_session_absent')

    def test_live_session_retains_occupancy(self):
        conn=Connection(dict(server_uuid=IDENTITY['server_uuid'],database_user='reader',session_present=1))
        watchdog_reconcile(conn,self.requests,self.proofs);self.gate.tick()
        self.assertEqual(self.book.occupied,1)
        self.check();self.assertEqual(self.book.occupied,0)

    def test_absence_without_verified_visibility_keeps_occupancy(self):
        self.check();self.assertEqual(self.book.occupied,1)

    def test_short_lived_session_can_be_proven_absent_by_same_account_observer(self):
        self.check(observer_session_visible=True);self.assertEqual(self.book.occupied,0)

    def test_visibility_for_a_different_account_does_not_release_unknown(self):
        conn=Connection(dict(server_uuid=IDENTITY['server_uuid'],database_user='another',session_present=0,observer_session_visible=True))
        watchdog_reconcile(conn,self.requests,self.proofs);self.gate.tick()
        self.assertEqual(self.book.occupied,1)

    def test_external_reconciler_does_not_recover_or_reset_a_live_reservation(self):
        self.book.node_limit=2
        reserved=self.book.reserve('hk-read-assist','unrelated',available=lambda:14)['id']
        conn=Connection(dict(server_uuid=IDENTITY['server_uuid'],database_user='reader',session_present=0,observer_session_visible=True))
        watchdog_reconcile(conn,self.requests,self.proofs)
        result=reconcile_live_book(self.root/'book.sqlite3',proofs=self.proofs)
        self.assertEqual(result,dict(before=2,after=1,business_queries=0))
        self.assertEqual(self.book.get(reserved)['state'],'reserved')

    def test_wrong_backend_or_user_cannot_prove_absence(self):
        for server,user in [('other','reader'),(IDENTITY['server_uuid'],'different-user')]:
            conn=Connection(dict(server_uuid=server,database_user=user,session_present=0))
            watchdog_reconcile(conn,self.requests,self.proofs);self.gate.tick()
            self.assertEqual(self.book.occupied,1)

    def test_no_pending_unknown_means_no_extra_watchdog_sql(self):
        self.requests.write_text('{"sessions":[]}')
        conn=Connection({});self.assertEqual(watchdog_reconcile(conn,self.requests,self.proofs),0)
        self.assertFalse(conn.active_cursor.statements)


if __name__=='__main__':unittest.main()

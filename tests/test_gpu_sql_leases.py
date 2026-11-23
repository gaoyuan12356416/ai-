from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from features.gpu_offload.sql_leases import LeaseBook, LeaseError

NODE='hk-read-assist'
IDENTITY=dict(server_uuid='14129611-64cd-4b66-8b8c-822d2363c0ae',connection_id=123,read_only=1,transaction_read_only=1)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'leases.sqlite3'
        self.now=[100.0]
        self.book=LeaseBook(self.path,clock=lambda:self.now[0])

    def tearDown(self):
        self.book.close();self.temp.cleanup()

    def reserve(self):
        return self.book.reserve(NODE,'campaign-copy-report',available=lambda:14-self.book.occupied)['id']

    def connected(self):
        key=self.reserve();self.book.begin(key,NODE);self.book.bind(key,NODE,IDENTITY);return key

    def test_unknown_session_keeps_capacity_across_timeout_and_restart(self):
        key=self.connected();self.now[0]+=1000;self.book.expire()
        self.assertEqual(self.book.get(key)['state'],'unknown')
        self.assertEqual(self.book.occupied,1)
        self.book.close();self.book=LeaseBook(self.path,clock=lambda:self.now[0])
        self.assertEqual(self.book.occupied,1)
        with self.assertRaises(LeaseError):self.reserve()
        with self.assertRaises(LeaseError):self.book.release(key,NODE)
        self.assertEqual(self.book.occupied,1)

    def test_permission_to_open_connection_cannot_be_reused(self):
        key=self.reserve();self.book.begin(key,NODE)
        with self.assertRaises(LeaseError):self.book.begin(key,NODE)
        self.book.close();self.book=LeaseBook(self.path,clock=lambda:self.now[0])
        self.assertEqual(self.book.get(key)['state'],'unknown')
        self.assertEqual(self.book.occupied,1)

    def test_reservation_without_connect_permission_can_be_retired_safely(self):
        key=self.reserve();self.now[0]+=100;self.book.expire()
        self.assertEqual(self.book.occupied,0)
        with self.assertRaises(LeaseError):self.book.begin(key,NODE)
        self.reserve()

    def test_wrong_replica_absence_does_not_release_unknown(self):
        key=self.connected();self.now[0]+=100;self.book.expire()
        for server,connection,present in [('other',123,False),(IDENTITY['server_uuid'],124,False),(IDENTITY['server_uuid'],123,True)]:
            with self.assertRaises(LeaseError):self.book.reconcile_absence(key,observed_server_uuid=server,observed_connection_id=connection,session_present=present)
            self.assertEqual(self.book.occupied,1)
        self.book.reconcile_absence(key,observed_server_uuid=IDENTITY['server_uuid'],observed_connection_id=123,session_present=False)
        self.assertEqual(self.book.occupied,0)

    def test_confirmed_server_eof_releases_only_matching_session(self):
        key=self.connected()
        with self.assertRaises(LeaseError):self.book.release(key,NODE,server_eof=True,identity=dict(IDENTITY,connection_id=124))
        self.assertEqual(self.book.occupied,1)
        self.book.release(key,NODE,server_eof=True,identity=IDENTITY)
        self.assertEqual(self.book.occupied,0)

    def test_read_write_backend_and_unbounded_session_are_rejected(self):
        key=self.reserve();self.book.begin(key,NODE)
        for value in [dict(IDENTITY,read_only=0),dict(IDENTITY,transaction_read_only=0),dict(IDENTITY,connection_id=0),{}]:
            with self.assertRaises(LeaseError):self.book.bind(key,NODE,value)
        self.assertEqual(self.book.occupied,1)

    def test_two_starts_per_second_includes_setup_and_reconnects(self):
        key=self.reserve();self.book.begin(key,NODE)
        self.book.start_query(key,NODE,setup=True)
        with self.assertRaises(LeaseError) as error:self.book.start_query(key,NODE,setup=True)
        self.assertAlmostEqual(error.exception.retry_after,0.5)
        self.now[0]+=0.5;self.book.bind(key,NODE,IDENTITY);self.book.start_query(key,NODE)
        self.book.release(key,NODE,server_eof=True,identity=IDENTITY)
        next_key=self.reserve();self.book.begin(next_key,NODE)
        with self.assertRaises(LeaseError):self.book.start_query(next_key,NODE,setup=True)
        self.now[0]+=0.5;self.book.start_query(next_key,NODE,setup=True)

    def test_shared_admission_never_exceeds_fourteen_with_parallel_clients(self):
        local=[]
        def cpu(_):
            with self.book.lock:
                if len(local)+self.book.occupied<14:local.append(1)
        def gpu(_):
            try:
                self.book.reserve(NODE,'metadata',available=lambda:14-len(local)-self.book.occupied)
            except LeaseError:pass
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda number:gpu(number) if number%2 else cpu(number),range(40)))
        self.assertLessEqual(self.book.occupied,1)
        self.assertEqual(len(local)+self.book.occupied,14)

    def test_lost_heartbeat_denies_new_queries_without_releasing(self):
        key=self.connected();self.now[0]+=100
        with self.assertRaises(LeaseError):self.book.start_query(key,NODE)
        self.assertEqual(self.book.occupied,1)


if __name__=='__main__':unittest.main()

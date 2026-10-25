import unittest
import sqlite3
from unittest.mock import Mock,patch
import repair_background as repair
from meta_violation_cleanup.throttle import Governor
from test_live_reconciliation import LiveReconciliationTests


class BackgroundSafetyTests(unittest.TestCase):
    def test_cpu_python_supports_rfc3339_z_timestamps(self):
        self.assertGreater(repair.ledger_age('2026-01-01T00:00:00.123456Z'),0)
        with self.assertRaises(ValueError):repair.ledger_age('2026-01-01T00:00:00')

    def test_live_service_never_allows_followup_start(self):
        for status in ('running','completed','partial','failed','interrupted'):
            self.assertFalse(repair.may_start({'status':status},
                {'ActiveState':'active','MainPID':'3031550'}))

    def test_only_natural_completion_and_no_process_allows_start(self):
        for status in ('running','failed','interrupted','previewed'):
            self.assertFalse(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'0'}))
        for status in ('completed','partial'):
            self.assertTrue(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'0'}))
            self.assertFalse(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'123'}))

    def test_transport_rejects_every_write_before_network(self):
        raw=Mock();session=repair.ReadOnlySession(raw,Governor(maximum=1,per_account=1,rps=2),lambda:False)
        for method in ('DELETE','POST','PUT','PATCH'):
            with self.assertRaisesRegex(RuntimeError,'read_only'):session.request(method,'https://example.test/')
        raw.request.assert_not_called()

    def test_transport_observes_rate_headers_on_get(self):
        raw=Mock();raw.request.return_value.status_code=200
        raw.request.return_value.headers={'x-app-usage':'{"call_count":75}'}
        raw.request.return_value.json.return_value={}
        governor=Governor(maximum=1,per_account=1,rps=2)
        session=repair.ReadOnlySession(raw,governor,lambda:False)
        session.request('GET','https://example.test/')
        self.assertEqual(governor.snapshot()['recent_usage_max_percent'],75)
        self.assertEqual(raw.request.call_count,1)


class BatchIntegrationTests(LiveReconciliationTests):
    def test_legacy_batch_updates_real_ledger_without_delete(self):
        worker=repair.Repair.__new__(repair.Repair)
        worker.store=self.store;worker.db=sqlite3.connect(':memory:');worker.db.row_factory=sqlite3.Row
        worker.db.execute('CREATE TABLE targets(job TEXT,key TEXT,kind TEXT,cohort TEXT,checks INTEGER DEFAULT 0,'
                          'last_check REAL DEFAULT 0,outcome TEXT,PRIMARY KEY(job,key))')
        worker.db.execute('INSERT INTO targets(job,key,kind,cohort) VALUES(?,?,?,?)',
                          (self.job,'ad:10','ad','legacy_9514'));worker.db.commit()
        worker.graph=Mock();worker.graph.tokens={'1':'test'};worker.http=Mock()
        worker.pressure=lambda:False;worker.checkpoint=Mock()
        with patch.object(repair,'read_status_batch',return_value={'10':self.proof}) as reader:
            self.assertTrue(worker.batch(final=True))
        self.assertEqual(reader.call_count,1)
        self.assertEqual(self.row()['status'],'already_deleted')
        self.assertEqual(worker.db.execute('SELECT outcome FROM targets').fetchone()[0],'deleted_verified')
        worker.graph.delete.assert_not_called();worker.db.close()


if __name__=='__main__':unittest.main()

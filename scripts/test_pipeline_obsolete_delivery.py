import hashlib
import json
import unittest
from scripts import test_pipeline_failed_receipt_ack as fixtures


class ObsoleteDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.FailedReceiptAckTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        with f.store.transaction() as c:
            old = dict(c.execute('SELECT * FROM pipeline_claim_history').fetchone())
            old.update(generation=2, claim_hash=hashlib.sha256(b'b'*32).hexdigest())
            cols = list(old)
            c.execute('INSERT INTO pipeline_claim_history(' + ','.join(cols) + ') VALUES(' + ','.join('?' for _ in cols) + ')', tuple(old[k] for k in cols))
            c.execute("UPDATE pipeline_job SET generation=2,claim_hash=?", (old['claim_hash'],))

    def test_old_failure_ack_does_not_change_successor_or_fences(self):
        before = self.f.snapshot()
        response = self.f.client.post('jobs/fail', self.f.receipt)
        self.assertTrue(response['superseded'])
        self.assertTrue(response['delivery_only'])
        self.assertEqual(self.f.snapshot(), before)
        self.assertEqual(self.f.native.fail_calls, 0)

    def test_old_preflight_ack_is_delivery_only(self):
        with self.f.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET stage='prepare'")
        payload = {**self.f.claim, 'checkpoint_id': 'old-probe', 'phase': 'x_preflight', 'receipt': {'artifact': {'mime': 'image/png'}}}
        before = self.f.snapshot()
        self.assertTrue(self.f.client.post('jobs/checkpoint', payload)['delivery_only'])
        self.assertEqual(self.f.snapshot(), before)

    def test_same_failed_recovery_ack_preserves_prior_platform_history(self):
        f = self.f
        with f.store.transaction() as c:
            c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      ('old', f.claim['job_id'], 1, 'cpu-main', 'create', 'a'*64, 'x:account:1', f.store.now(), f.store.now()))
        payload = dict(f.receipt, generation=2, claim_token='b'*32)
        before = f.snapshot()
        self.assertTrue(f.client.post('jobs/fail', payload)['delivery_only'])
        self.assertEqual(f.snapshot(), before)

    def test_unknown_wrong_identity_and_external_operations_are_excluded(self):
        f = self.f
        for change in ({'unknown_outcome': True}, {'claim_token': 'wrong'}, {'worker_id': 'another'}):
            p = dict(f.receipt, **change)
            self.assertIsNone(f.store.acknowledge_obsolete_no_write_delivery(p, 'id', p, 'jobs/fail'))
        with f.store.transaction() as c:
            c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      ('old-write', f.claim['job_id'], 1, 'cpu-main', 'create', 'a'*64, 'x:account:1', f.store.now(), f.store.now()))
        self.assertIsNone(f.store.acknowledge_obsolete_no_write_delivery(f.receipt, 'id', f.receipt, 'jobs/fail'))


if __name__ == '__main__':
    unittest.main()

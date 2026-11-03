"""An erased unknown outcome remains fenced without rewriting native history."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import hold_x_reset_unknown as hold


class UnknownHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.central, self.native, self.outbox = [self.root / (name + '.db') for name in ('central', 'native', 'outbox')]
        self.now = dt.datetime(2026, 11, 3, 11, tzinfo=dt.timezone.utc)
        self.token = 'test-only-claim-token-' * 3
        token_hash = hashlib.sha256(self.token.encode()).hexdigest()
        payload = {'job_id': hold.JOB, 'generation': 1, 'node_id': 'cpu-main', 'worker_id': 'publish-0',
                   'claim_token': self.token, 'receipt_id': 'unknown-1', 'unknown_outcome': True,
                   'error_code': hold.CODE}
        self.payload = payload
        job = {'id': hold.JOB, 'source_key': hold.KEY, 'source_id': str(hold.QUEUE), 'channel': 'x',
               'family': 'queue', 'stage': 'publish', 'generation': 1, 'status': 'ready',
               'hold_reason': '', 'lease_expires_at': '', 'canonical_receipt': '', 'error_code': '',
               'finished_at': '', 'updated_at': '2026-11-03T10:24:07+00:00'}
        with sqlite3.connect(self.central) as c:
            self.table(c, 'pipeline_job', job)
            c.execute('INSERT INTO pipeline_job SELECT id+1,replace(source_key,\'5452\',\'5453\'),\'5453\',channel,family,stage,generation,status,hold_reason,lease_expires_at,canonical_receipt,error_code,finished_at,updated_at FROM pipeline_job')
            self.table(c, 'pipeline_config', {'id': 1, 'config_json': '{"enabled_channels":["youtube"]}'})
            self.table(c, 'pipeline_publication', {'source_key': '', 'job_id': 0}, empty=True)
            self.table(c, 'pipeline_claim_history', {'job_id': hold.JOB, 'generation': 1,
                       'node_id': 'cpu-main', 'worker_id': 'publish-0', 'claim_hash': token_hash})
            self.table(c, 'pipeline_delivery', {'delivery_id': 'unknown-1', 'job_id': hold.JOB, 'generation': 1,
                       'claim_hash': token_hash, 'request_sha256': hold.digest(payload),
                       'response_json': '{"accepted":true,"status":"unknown"}'})
            self.table(c, 'pipeline_event', {'id': 1, 'job_id': hold.JOB, 'event_type': 'failed',
                       'details_json': hold.encoded({'unknown': True, 'code': hold.CODE}),
                       'created_at': '2026-11-03T10:20:00+00:00'}, primary='id')
            self.table(c, 'pipeline_operation', {'operation_id': 'op1', 'job_id': hold.JOB,
                       'generation': 1, 'operation': 'x:POST:/2/media/upload/initialize'})
            for table in ('pipeline_checkpoint', 'pipeline_receipt', 'pipeline_resource', 'pipeline_legacy_resource'):
                self.table(c, table, {'job_id': 0, 'phase': ''}, empty=True)
        c.close()
        with sqlite3.connect(self.native) as n:
            self.table(n, 'x_post_queue', {'id': hold.QUEUE, 'account_id': hold.ACCOUNT, 'status': 'queued'})
            self.table(n, 'x_post_publish_log', {'id': 999, 'queue_id': hold.QUEUE, 'account_id': hold.ACCOUNT,
                       'status': 'reserved', 'attempt_count': 0, 'unknown_outcome': 0, 'x_post_id': '', 'published_at': ''})
            self.table(n, 'pipeline_x_claim', {'source_key': hold.KEY, 'stage': 'publish', 'generation': 1,
                       'claim_hash': token_hash, 'queue_id': hold.QUEUE, 'log_id': 999})
            self.table(n, 'pipeline_x_operation', {'source_key': hold.KEY, 'operation_id': 'op1',
                       'generation': 1, 'operation': 'x:POST:/2/media/upload/initialize'})
            self.table(n, 'pipeline_x_checkpoint', {'source_key': '', 'phase': ''}, empty=True)
            self.table(n, 'pipeline_canonical_hold', {'source_key': '', 'cutoff': '', 'reason': ''}, empty=True, primary='source_key')
        n.close()
        with sqlite3.connect(self.outbox) as o:
            self.table(o, 'receipt_outbox', {'id': 'unknown-1', 'route': 'jobs/fail',
                       'payload': hold.encoded(payload), 'created_at': 100.0, 'acked_at': 101.0})
        o.close()
        self.audit = self.root / 'audit.json'
        self.audit.write_text(hold.encoded({'readonly': True, 'at': '2026-11-03T10:24:05+00:00',
            'items': [{'id': hold.QUEUE, 'status': 'failed', 'log_status': 'failed', 'attempt_count': 1,
                       'unknown_outcome': 1, 'job_id': hold.JOB, 'stage': 'publish', 'generation': 1,
                       'account_id': hold.ACCOUNT, 'central_status': 'unknown', 'error_code': hold.CODE,
                       'node_id': 'cpu-main'}]}))

    @staticmethod
    def table(c, table, row, empty=False, primary=None):
        definitions = [key + ' ' + ('INTEGER' if isinstance(value, int) else 'REAL' if isinstance(value, float) else 'TEXT')
                       + (' PRIMARY KEY' if key == primary else '') for key, value in row.items()]
        c.execute('CREATE TABLE ' + table + '(' + ','.join(definitions) + ')')
        if not empty:
            c.execute('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', list(row.values()))

    def plan(self):
        return hold.make_plan(self.central, self.native, self.outbox, self.audit, self.now)

    def apply(self, plan, **kw):
        return hold.apply_plan(plan, self.root / 'backup', 'test-operator', now=self.now, **kw)

    @staticmethod
    def execute(path, sql, params=()):
        c = sqlite3.connect(path)
        try:
            with c:
                return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def test_exact_hold_preserves_native_and_attempt_history_and_is_idempotent(self):
        plan = self.plan()
        self.assertNotIn(self.token, hold.encoded(plan))
        native_before = {t: self.execute(self.native, 'SELECT * FROM ' + t) for t in ('x_post_queue', 'x_post_publish_log', 'pipeline_x_claim', 'pipeline_x_operation')}
        unrelated = self.execute(self.central, 'SELECT * FROM pipeline_job WHERE id<>?', (hold.JOB,))
        first = self.apply(plan)
        second = self.apply(plan)
        self.assertFalse(first['already_applied']); self.assertTrue(second['already_applied'])
        self.assertEqual(self.execute(self.central, 'SELECT status,hold_reason FROM pipeline_job WHERE id=?', (hold.JOB,)), [('unknown', hold.REASON)])
        self.assertEqual(self.execute(self.native, 'SELECT reason FROM pipeline_canonical_hold'), [(hold.REASON,)])
        self.assertEqual(unrelated, self.execute(self.central, 'SELECT * FROM pipeline_job WHERE id<>?', (hold.JOB,)))
        for table, before in native_before.items():
            self.assertEqual(before, self.execute(self.native, 'SELECT * FROM ' + table))
        self.assertEqual(self.execute(self.central, 'SELECT count(*) FROM pipeline_event'), [(2,)])
        details = json.loads(self.execute(self.central, 'SELECT details_json FROM pipeline_event WHERE event_type=?', (hold.REASON,))[0][0])
        self.assertEqual(details['historical_audit']['row']['attempt_count'], 1)
        self.assertEqual(details['original_rows']['log']['attempt_count'], 0)
        self.assertEqual(len(first['backups']), 3)
        for path in first['backups']:
            self.assertEqual(self.execute(path, 'PRAGMA quick_check'), [('ok',)])

    def test_interruption_after_central_commit_stays_fenced_and_resumes(self):
        plan = self.plan()
        with self.assertRaisesRegex(hold.RecoveryRejected, 'injected_stop'):
            self.apply(plan, stop_after='central')
        self.assertEqual(self.execute(self.central, 'SELECT status FROM pipeline_job WHERE id=?', (hold.JOB,)), [('unknown',)])
        self.assertEqual(self.execute(self.native, 'SELECT * FROM pipeline_canonical_hold'), [])
        self.apply(plan)
        self.assertEqual(self.execute(self.central, 'SELECT count(*) FROM pipeline_event'), [(2,)])

    def test_no_running_valid_lease_or_success_is_accepted(self):
        cases = [
            ('central', "UPDATE pipeline_job SET status='running' WHERE id=32064"),
            ('central', "UPDATE pipeline_job SET lease_expires_at='2026-11-03T12:00:00+00:00' WHERE id=32064"),
            ('native', "UPDATE x_post_publish_log SET x_post_id='12345'"),
            ('central', "INSERT INTO pipeline_publication VALUES('x:queue:5452',32064)"),
            ('central', "UPDATE pipeline_config SET config_json='{\"enabled_channels\":[\"x\"]}'"),
        ]
        for path, sql in cases:
            with self.subTest(sql=sql):
                self.execute(getattr(self, path), sql)
                with self.assertRaises(hold.RecoveryRejected): self.plan()
                # A fresh fixture prevents one failed case from masking another.
                self.tearDown(); self.doCleanups(); self.setUp()

    def test_ack_and_unknown_event_and_historical_audit_are_required(self):
        self.execute(self.central, 'DELETE FROM pipeline_event')
        with self.assertRaisesRegex(hold.RecoveryRejected, 'actual_unknown_failure_event_required'): self.plan()

    def test_zero_attempt_audit_cannot_authorize_this_operation(self):
        audit = json.loads(self.audit.read_text())
        audit['items'][0]['attempt_count'] = 0
        self.audit.write_text(hold.encoded(audit))
        with self.assertRaisesRegex(hold.RecoveryRejected, 'historical_audit_identity'): self.plan()

    def test_known_failure_ack_is_not_unknown_proof(self):
        payload = dict(self.payload, unknown_outcome=False)
        self.execute(self.outbox, 'UPDATE receipt_outbox SET payload=?', (hold.encoded(payload),))
        with self.assertRaisesRegex(hold.RecoveryRejected, 'acked_unknown_receipt_required'): self.plan()

    def test_success_outbox_is_rejected_even_with_earlier_unknown(self):
        payload = dict(self.payload, receipt_id='success-2', result={'post_id': '12345'})
        self.execute(self.outbox, 'INSERT INTO receipt_outbox VALUES(?,?,?,?,?)',
                     ('success-2', 'jobs/complete', hold.encoded(payload), 200.0, 201.0))
        with self.assertRaisesRegex(hold.RecoveryRejected, 'node_success_present'): self.plan()

    def test_unknown_ack_hash_cannot_be_replaced_by_reset_attempt_zero(self):
        self.execute(self.central, "UPDATE pipeline_delivery SET request_sha256='changed'")
        with self.assertRaisesRegex(hold.RecoveryRejected, 'acked_unknown_receipt_mismatch'): self.plan()

    def test_pending_or_success_outbox_receipt_blocks(self):
        self.execute(self.outbox, 'UPDATE receipt_outbox SET acked_at=NULL')
        with self.assertRaisesRegex(hold.RecoveryRejected, 'pending_node_receipt'): self.plan()

    def test_plan_drift_does_not_change_either_database(self):
        plan = self.plan()
        self.execute(self.central, "UPDATE pipeline_job SET updated_at='changed' WHERE id=32064")
        with self.assertRaisesRegex(hold.RecoveryRejected, 'plan_state_drift'): self.apply(plan)
        self.assertEqual(self.execute(self.native, 'SELECT * FROM pipeline_canonical_hold'), [])
        self.assertEqual(self.execute(self.central, 'SELECT status FROM pipeline_job WHERE id=32064'), [('ready',)])


if __name__ == '__main__':
    unittest.main()

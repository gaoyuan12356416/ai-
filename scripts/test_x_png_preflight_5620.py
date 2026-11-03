"""Real SQLite settlement, interruption and late-claim safety tests, offline."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from features.pipeline_dispatch.core import DispatchError, DispatchStore
from scripts import settle_x_png_preflight_5620 as s
from scripts import test_pipeline_x_native as nf


def insert(c, table, values):
    c.execute('INSERT INTO '+table+' ('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')', list(values.values()))


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.base = nf.NativeTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.root = self.base.root; self.native = self.base.runtime.POST_DB_PATH
        self.central = self.root / 'central.sqlite'; self.now = dt.datetime(2026, 11, 3, 14, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(self.central, clock=lambda: self.now)
        q = self.base.queue(identity=63, random=True)
        with sqlite3.connect(self.native) as n:
            n.row_factory = sqlite3.Row
            original = dict(n.execute('SELECT * FROM x_post_queue WHERE id=?', (q['id'],)).fetchone())
            n.execute('DELETE FROM x_post_queue')
            original.update(id=5620, manual_run_id=None, schedule_run_id=1039, run_date='2026-11-03', material_id='7676040', material_url='https://media.example.test/source.png', material_language='ja')
            insert(n, 'x_post_schedule_run', {'id': 1039, 'slot_key': 'fixture-1039', 'source_type': 'material', 'run_date': '2026-11-03',
                'publish_time': '16:51', 'timezone': 'Asia/Shanghai', 'config_version': 33, 'account_ids_json': '[63]',
                'status': 'queued', 'expected_count': 2, 'queued_count': 2, 'random_template_enabled': 1,
                'duration_policy_version': original['duration_policy_version'], 'created_at': 'before', 'updated_at': 'before'})
            insert(n, 'x_post_queue', original)
            insert(n, 'x_post_publish_log', {'id': 5543, 'queue_id': 5620, 'account_id': 63, 'status': 'reserved', 'created_at': 'before', 'updated_at': 'before'})
            # A live sibling proves aggregation does not finish the whole run.
            sibling = dict(original, id=5621, account_id=64, idempotency_key='sibling', material_id='other', material_key='other', pool_item_id=None, status='publishing')
            insert(n, 'x_post_queue', sibling)
            insert(n, 'x_post_publish_log', {'id': 5544, 'queue_id': 5621, 'account_id': 64, 'status': 'media_uploading', 'attempt_count': 1, 'created_at': 'before', 'updated_at': 'before'})
        self.desc = {'kind': 'x_preflight', 'request': {'source_url': original['material_url'], 'random_request': {'job_key': 'fixed-video'}}}
        self.receipt = {'artifact': {'mime': 'image/png', 'media_kind': 'image', 'sha256': 'a'*64, 'size': 2815842}, 'source_url': original['material_url']}
        self.tokens = {g: 'test-claim-'+str(g) for g in range(1, 18)}
        self.evidence = [{'node_id': 'cpu-main', 'at': self.now.isoformat(), 'receipts': []},
                         {'node_id': 'hk-gpu', 'at': self.now.isoformat(), 'receipts': []}]
        self.raw_packets = []
        with sqlite3.connect(self.central) as c, sqlite3.connect(self.native) as n:
            insert(c, 'pipeline_job', {'id': 32153, 'source_key': s.KEY, 'source_id': '5620', 'channel': 'x', 'family': 'queue',
                'batch_key': 'x:1039', 'stage': 'prepare', 'scheduled_at': '2026-11-03T11:55:04Z', 'source_created_at': '2026-11-03T08:51:00Z',
                'status': 'queued', 'hold_reason': s.HOLD, 'generation': 17, 'node_id': 'hk-gpu', 'worker_id': 'worker-17',
                'claim_hash': s.sha(self.tokens[17]), 'lease_expires_at': '2026-11-03T12:42:12Z', 'error_code': 'lease_expired',
                'created_at': 'before', 'updated_at': 'before'})
            insert(c, 'pipeline_event', {'id': 1, 'job_id': 32153, 'event_type': 'operator_invalid_input_hold', 'details_json': '{}', 'created_at': 'before'})
            for g in range(1, 18):
                claim_sha = s.sha(self.tokens[g]); worker = 'worker-'+str(g)
                insert(c, 'pipeline_claim_history', {'job_id': 32153, 'generation': g, 'node_id': 'hk-gpu', 'worker_id': worker, 'claim_hash': claim_sha})
                insert(n, 'pipeline_x_claim', {'source_key': s.KEY, 'stage': 'prepare', 'generation': g, 'claim_hash': claim_sha,
                    'queue_id': 5620, 'log_id': 5543, 'descriptor_json': s.encoded(self.desc)})
                for route in ('jobs/checkpoint', 'jobs/fail'):
                    identity = route.replace('/', '-')+'-'+str(g)
                    payload = {'job_id': 32153, 'generation': g, 'node_id': 'hk-gpu', 'worker_id': worker}
                    if route == 'jobs/checkpoint': payload.update(checkpoint_id=identity, phase='x_preflight', receipt=self.receipt)
                    else: payload.update(receipt_id=identity, error_code='coordinator_operation_failed', unknown_outcome=False)
                    raw = s.encoded(dict(payload, claim_token=self.tokens[g]))
                    self.raw_packets.append((identity, route, raw, float(len(self.raw_packets)), None))
                    self.evidence[1]['receipts'].append({'id': identity, 'route': route, 'created_at': float(len(self.raw_packets)-1), 'acked_at': None,
                        'raw_sha256': s.sha(raw), 'claim_sha256': claim_sha, 'safe_payload': payload, 'safe_sha256': s.digest(payload)})
        self.outbox = self.root / 'receipts.sqlite'
        with sqlite3.connect(self.outbox) as o:
            o.execute('CREATE TABLE receipt_outbox (id TEXT PRIMARY KEY,route TEXT,payload TEXT,created_at REAL,acked_at REAL)')
            o.executemany('INSERT INTO receipt_outbox VALUES(?,?,?,?,?)', self.raw_packets)
        with s.read_connection(self.central) as c, s.read_connection(self.native) as n:
            self.before = s.collect(c, n)
        manifest = {'hashes': {k: s.digest(self.before[k]) for k in ('queue', 'log', 'native_claims', 'history', 'events')},
            'descriptor_sha256': s.digest(self.desc), 'receipt_sha256': s.digest(self.receipt), 'source_url_sha256': s.sha(original['material_url']),
            'receipts': sorted([{k:r[k] for k in ('id','route','raw_sha256','safe_sha256','claim_sha256')} for r in self.evidence[1]['receipts']], key=lambda r:r['id'])}
        manifest['hashes'].update(stable_job=s.digest(s.stable_job(self.before['job'])), parent_identity=s.digest(s.parent_identity(self.before['parent'])))
        patch = mock.patch.object(s, 'MANIFEST', manifest); patch.start(); self.addCleanup(patch.stop)

    def plan(self):
        return s.make_plan(self.central, self.native, self.evidence, now=self.now)

    def apply(self, plan, **kwargs):
        return s.apply(plan, self.evidence, self.root/'backup', 'operator', now=self.now, **kwargs)

    def facts(self):
        with s.read_connection(self.central) as c, s.read_connection(self.native) as n:
            return s.collect(c, n)

    def test_settle_preserves_attempts_generations_packets_siblings_and_no_network(self):
        plan = self.plan(); outbox = self.outbox.read_bytes()
        with mock.patch('requests.sessions.Session.request', side_effect=AssertionError('no platform call')):
            result = self.apply(plan)
        self.assertEqual(0, result['publish_requests']); self.assertEqual(outbox, self.outbox.read_bytes())
        facts = self.facts()
        self.assertEqual((facts['queue']['status'], facts['log']['status'], facts['job']['status']), ('failed',)*3)
        self.assertEqual((facts['log']['attempt_count'], facts['log']['unknown_outcome'], facts['job']['generation']), (0,0,17))
        self.assertEqual(facts['job']['claim_hash'], ''); self.assertEqual(facts['job']['hold_reason'], s.HOLD)
        for k in ('native_claims', 'history'): self.assertEqual(self.before[k], facts[k])
        self.assertEqual((facts['parent']['status'], facts['parent']['failed_count'], facts['parent']['finished_at']), ('running',1,''))
        with sqlite3.connect(self.native) as n:
            self.assertEqual(n.execute('SELECT status FROM x_post_queue WHERE id=5621').fetchone()[0], 'publishing')
        for path in result['backups']:
            with sqlite3.connect(path) as b: self.assertEqual(b.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_current_late_claim_rejected_by_real_dispatch_store(self):
        claim = {'job_id':32153,'generation':17,'node_id':'hk-gpu','worker_id':'worker-17','claim_token':self.tokens[17]}
        self.store.validate_receipt_claim(claim)  # Expired queued claim was otherwise still accepted.
        self.apply(self.plan(), stop_after='fenced')
        with self.assertRaisesRegex(DispatchError, 'claim_conflict'): self.store.validate_receipt_claim(claim)
        with self.assertRaisesRegex(DispatchError, 'claim_conflict'): self.store.validate_receipt_claim(dict(claim,claim_token=''))

    def test_resume_after_each_cross_database_boundary(self):
        plan = self.plan()
        self.assertEqual(self.apply(plan, stop_after='fenced')['state'], 'fenced')
        self.assertEqual(self.facts()['log']['status'], 'reserved')
        self.assertEqual(self.apply(plan, stop_after='native')['state'], 'native')
        self.assertEqual(self.facts()['job']['status'], 'queued')
        self.assertEqual(self.facts()['log']['status'], 'failed')
        self.apply(plan); self.assertEqual(self.facts()['job']['status'], 'failed')

    def test_repeat_apply_does_not_mutate_or_backup_again(self):
        plan = self.plan(); self.apply(plan)
        before = self.facts(); backups = sorted((self.root/'backup').iterdir())
        self.assertTrue(self.apply(plan)['already_applied'])
        self.assertEqual(before, self.facts()); self.assertEqual(backups, sorted((self.root/'backup').iterdir()))

    def test_different_resumption_plan_rejected(self):
        plan = self.plan(); self.apply(plan, stop_after='fenced')
        plan['created_at'] = (self.now-dt.timedelta(seconds=1)).isoformat(); plan['plan_sha256'] = s.document_hash(plan)
        with self.assertRaisesRegex(s.RecoveryRejected, 'different_plan'): self.apply(plan)

    def test_acked_unknown_hash_or_missing_packet_rejected(self):
        original = copy.deepcopy(self.evidence)
        changes = [('acked_at', 1), ('raw_sha256', 'b'*64), ('safe_sha256', 'c'*64)]
        for key, value in changes:
            self.evidence = copy.deepcopy(original); self.evidence[1]['receipts'][0][key] = value
            with self.subTest(key=key), self.assertRaises(s.RecoveryRejected): self.plan()
        self.evidence = copy.deepcopy(original); self.evidence[1]['receipts'].pop()
        with self.assertRaises(s.RecoveryRejected): self.plan()
        self.evidence = copy.deepcopy(original); packet = self.evidence[1]['receipts'][1]
        packet['safe_payload']['unknown_outcome'] = True; packet['safe_sha256'] = s.digest(packet['safe_payload'])
        with self.assertRaisesRegex(s.RecoveryRejected, 'not_known'): self.plan()

    def test_stale_node_evidence_and_unexpected_cpu_packet_rejected(self):
        self.evidence[0]['at'] = (self.now-dt.timedelta(seconds=121)).isoformat()
        with self.assertRaisesRegex(s.RecoveryRejected, 'stale'): self.plan()
        self.evidence[0]['at'] = self.now.isoformat(); self.evidence[0]['receipts'] = [self.evidence[1]['receipts'][0]]
        with self.assertRaisesRegex(s.RecoveryRejected, 'unexpected_cpu'): self.plan()

    def test_new_platform_intent_rejected_before_any_mutation(self):
        plan = self.plan()
        with sqlite3.connect(self.central) as c:
            insert(c, 'pipeline_operation', {'operation_id':'unexpected', 'job_id':32153,'generation':17,'node_id':'hk-gpu',
                'operation':'x:POST:/2/tweets','request_sha256':'a','account_resource':'x:account:63','created_at':'now','updated_at':'now'})
        with self.assertRaisesRegex(s.RecoveryRejected, 'central_execution_fact'): self.apply(plan)
        with sqlite3.connect(self.central) as c:
            self.assertEqual(c.execute('SELECT claim_hash FROM pipeline_job WHERE id=32153').fetchone()[0], s.sha(self.tokens[17]))

    def test_native_unknown_or_lease_claim_drift_rejected(self):
        plan = self.plan()
        with sqlite3.connect(self.native) as n: n.execute('UPDATE x_post_publish_log SET unknown_outcome=1 WHERE id=5543')
        with self.assertRaisesRegex(s.RecoveryRejected, 'log_changed'): self.apply(plan)
        with sqlite3.connect(self.native) as n: n.execute('UPDATE x_post_publish_log SET unknown_outcome=0 WHERE id=5543')
        with sqlite3.connect(self.central) as c: c.execute('UPDATE pipeline_job SET generation=18 WHERE id=32153')
        with self.assertRaisesRegex(s.RecoveryRejected, 'job_changed'): self.apply(plan)

    def test_audits_reject_update_delete_and_replace(self):
        self.apply(self.plan())
        for path, table in ((self.native,s.AUDIT),(self.central,s.JOURNAL)):
            with sqlite3.connect(path) as c:
                for statement in ("UPDATE "+table+" SET proof_json='{}'", 'DELETE FROM '+table, 'INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table):
                    with self.subTest(table=table, statement=statement), self.assertRaises(sqlite3.IntegrityError): c.execute(statement)

    def test_node_capture_exact_original_bytes_without_tokens(self):
        result = s.node_evidence(self.outbox,'hk-gpu',now=self.now)
        self.assertEqual(self.evidence[1], result)
        self.assertNotIn('claim_token', json.dumps(result)); self.assertNotIn('test-claim-', json.dumps(result))

    def test_parent_uses_current_sibling_success_not_snapshot_counts(self):
        plan = self.plan()
        with sqlite3.connect(self.native) as n:
            n.execute("UPDATE x_post_queue SET status='published' WHERE id=5621")
            n.execute("UPDATE x_post_publish_log SET status='published',x_post_id='sibling-post' WHERE id=5544")
        self.apply(plan); parent = self.facts()['parent']
        self.assertEqual((parent['status'],parent['published_count'],parent['failed_count']),('completed_with_errors',1,1))
        self.assertTrue(parent['finished_at'])


if __name__ == '__main__': unittest.main()

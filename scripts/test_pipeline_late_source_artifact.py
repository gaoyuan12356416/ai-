"""Late qualified X output can correct only the old completed source projection."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from features.pipeline_dispatch.core import DispatchStore, DispatchError, dumps, stamp
from features.pipeline_dispatch.service import Coordinator


class LateSourceArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / 'ledger.db', lambda: self.now)
        self.native = Mock(); self.service = Coordinator(self.store, self.native, internal_token='i'*32, master_key='m'*32)
        self.profile = 'x-random-overlay-libx264-720x1280-v1'
        self.source = {'sha256': 'a'*64, 'size': 100, 'url': 'https://cos.example/source.mp4', 'profile': 'source'}
        self.final = {'sha256': 'b'*64, 'size': 200, 'url': 'https://cos.example/final.mp4', 'profile': self.profile, 'recipe': {'asset_set_sha256': 'c'*64}}
        self.store.register_node('cpu-main', ['prepare:x']); self.store.channel_enabled('x', True)
        self.store.worker_heartbeat('cpu-main', 'prep', 'prepare')
        item = self.store.offer({'source_key': 'x:queue:1', 'source_id': '1', 'channel': 'x', 'family': 'queue', 'stage': 'prepare',
                                 'capabilities': ['prepare:x'], 'resource_keys': ['x:account:1'], 'scheduled_at': stamp(self.now), 'source_created_at': stamp(self.now)})
        self.first = self.store.claim('cpu-main', 'prep', 'prepare')['job']
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET payload_json=? WHERE id=?',
                      (dumps({'kind': 'x_preflight', 'request': {'random_request': {'profile': self.profile}}}), self.first['id']))
        self.now += dt.timedelta(seconds=121)
        self.store.register_node('cpu-main', ['prepare:x']); self.store.worker_heartbeat('cpu-main', 'prep', 'prepare')
        self.second = self.store.claim('cpu-main', 'prep', 'prepare')['job']
        self.assertEqual(self.second['generation'], 2)
        self.proof = {'source_key': item['source_key'], 'status': 'queued'}
        self.store.complete(self.second, 'native-prepared-%s-2' % item['id'], {'artifact': self.source, 'canonical_receipt': dumps(self.proof), 'dispatch_completed': True}, canonical_verified=True)
        self.native.reconcile.return_value = {'canonical_receipt': self.proof, 'dispatch_completed': True,
                                             'next_offer': {'source_key': item['source_key'], 'stage': 'publish', 'artifact': self.final}}
        self.receipt = {'job_id': item['id'], **{key: self.first[key] for key in ('generation','node_id','worker_id','claim_token')},
                        'receipt_id': 'late-final', 'metrics': {'render_seconds': 1000},
                        'result': {'artifact': self.final, 'preparation': {'source_sha256': self.source['sha256'], 'source_size': 100,
                            'output_sha256': self.final['sha256'], 'output_size': 200, 'output_url': self.final['url'], 'profile': self.profile, 'recipe': self.final['recipe']}}}

    def snapshot(self):
        with self.store.transaction() as c:
            job = dict(c.execute('SELECT * FROM pipeline_job WHERE id=?', (self.first['id'],)).fetchone())
            tables = {t: [tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('pipeline_receipt','pipeline_resource','pipeline_operation','pipeline_phase_metric')}
        return job,tables

    def deliver(self):
        return self.service.route('/internal/pipeline/jobs/complete', self.receipt, {'internal': True})

    def test_only_artifact_projection_changes_once_with_old_receipts_metrics_and_status_preserved(self):
        before, tables = self.snapshot()
        self.assertTrue(self.deliver()['historical'])
        after, kept = self.snapshot()
        self.assertEqual(json.loads(after.pop('artifact_json')), self.final); before.pop('artifact_json')
        self.assertEqual(after, before); self.assertEqual(kept, tables)
        self.assertTrue(self.deliver()['duplicate'])
        with self.store.transaction() as c:
            events = c.execute("SELECT details_json FROM pipeline_event WHERE event_type='qualified_final_artifact_corrected'").fetchall()
        self.assertEqual(len(events), 1)
        event = json.loads(events[0][0]); self.assertEqual((event['generation'], event['source_generation']), (2,1))
        self.assertNotIn('url', event)

    def test_failed_successor_is_only_acknowledged_never_recovered(self):
        with self.store.transaction() as c:c.execute("UPDATE pipeline_job SET status='failed' WHERE id=?", (self.first['id'],))
        before = self.snapshot(); self.assertTrue(self.deliver()['historical']); self.assertEqual(self.snapshot(), before)

    def test_non_source_final_or_missing_native_prepared_receipt_cannot_be_corrected(self):
        with self.store.transaction() as c:original_receipts=[tuple(r) for r in c.execute('SELECT * FROM pipeline_receipt')]
        for mutation in ('final', 'receipt', 'resource'):
            with self.subTest(mutation=mutation):
                with self.store.transaction() as c:
                    if mutation == 'final':c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?', (dumps({**self.source,'profile':self.profile}),self.first['id']))
                    elif mutation == 'receipt':
                        c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?', (dumps(self.source),self.first['id']))
                        c.execute('DELETE FROM pipeline_receipt')
                    else:
                        c.executemany('INSERT INTO pipeline_receipt VALUES(?,?,?,?,?)',original_receipts)
                        c.execute('INSERT INTO pipeline_resource VALUES(?,?,?,?,?)',('x:account:1',self.first['id'],2,1,0))
                self.receipt['receipt_id'] = 'late-' + mutation
                before = self.snapshot(); self.assertTrue(self.deliver()['historical']); self.assertEqual(self.snapshot(), before)

    def test_mismatched_canonical_final_is_rejected_before_ack_or_projection(self):
        self.receipt['result']['artifact'] = {**self.final, 'sha256': 'd'*64}
        before = self.snapshot()
        with self.assertRaisesRegex(DispatchError, 'historical_receipt_requires_reconciliation'):self.deliver()
        self.assertEqual(self.snapshot(), before)

    def test_changed_source_binding_is_acknowledged_without_projection_correction(self):
        self.receipt['result']['preparation']['source_sha256'] = 'd'*64
        before = self.snapshot(); self.assertTrue(self.deliver()['historical']); self.assertEqual(self.snapshot(), before)


class ActualNativeLateArtifactTests(unittest.TestCase):
    def test_real_native_projection_retains_recipe_and_corrects_old_completed_source(self):
        from scripts.test_pipeline_x_recovery_concurrency import XRecoveryTests
        native = XRecoveryTests('runTest'); native.setUp(); self.addCleanup(native.doCleanups)
        queue = native.queue(random=True)
        native_job, descriptor, source = native.preflight(queue)
        _, registry = native.registry()
        clock = [dt.datetime(2026,11,3,tzinfo=dt.timezone.utc)]
        store = DispatchStore(native.root/'late-artifact.db', lambda: clock[0])
        service = Coordinator(store, registry, internal_token='i'*32, master_key='m'*32)
        store.register_node('cpu-main',['prepare:x']); store.channel_enabled('x',True)
        store.worker_heartbeat('cpu-main','prepare','prepare')
        store.offer({'source_key':native_job['source_key'],'source_id':str(queue['id']),'channel':'x','family':'queue',
                     'stage':'prepare','capabilities':['prepare:x'],'resource_keys':['x:account:2'],'scheduled_at':stamp(clock[0]),'source_created_at':stamp(clock[0])})
        original=store.claim('cpu-main','prepare','prepare')['job']
        with store.transaction() as c:c.execute('UPDATE pipeline_job SET payload_json=? WHERE id=?',(dumps(descriptor),original['id']))
        clock[0]+=dt.timedelta(seconds=121)
        store.register_node('cpu-main',['prepare:x']);store.worker_heartbeat('cpu-main','prepare','prepare')
        successor=store.claim('cpu-main','prepare','prepare')['job']
        store.complete(successor,'native-prepared-%s-2'%original['id'],{'artifact':source,'canonical_receipt':'source-probe','dispatch_completed':True},canonical_verified=True)
        final=native.rendered(queue);native.bridge.complete(native_job,final,native_job)
        projected=registry.reconcile(original)['next_offer']['artifact']
        self.assertEqual(projected['recipe'],final['preparation']['recipe'])
        receipt={'job_id':original['id'],**{k:original[k] for k in ('generation','node_id','worker_id','claim_token')},'receipt_id':'native-final-late','result':final}
        before=store.get_job(original['id'])
        response=service.route('/internal/pipeline/jobs/complete',receipt,{'internal':True})
        after=store.get_job(original['id'])
        self.assertTrue(response['historical']);self.assertEqual(after['artifact'],projected)
        before.pop('artifact');after.pop('artifact');self.assertEqual(after,before)
        with store.transaction() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='qualified_final_artifact_corrected'").fetchone()[0],1)


if __name__ == '__main__':unittest.main()
